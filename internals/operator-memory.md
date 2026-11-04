# 算子内存机制（work_mem / operatorMemKB / spill / 弹性）

## 概述

> 服务分类：performance（内存/落盘判读、参数建议）、sql（计划内存行为）

work_mem 在 VastBase 内核里的真实语义是：**计划节点级配额 + 优化器估算优先 +
全局内存水位参与的弹性伸缩**，与社区 PostgreSQL"每算子一份静态 work_mem"有本质差异。
凡性能诊断中涉及"调 work_mem 是否安全/是否有效"，以本文件为准。

回答三个实际问题：为什么单条查询能吃数 GB 内存（配额×算子数叠加）；为什么调大
work_mem 可能根本不生效（优化器估算优先）；为什么调参救不了数据倾斜（大分组的
排序/哈希集超过任何合理配额，照旧落盘）。

## 关键结论（判读速用）

1. **配额单位是计划节点**：查询里每个排序/哈希节点各占一份配额，单查询总量 =
   配额 × 算子数。实测锚点：AWR 中 HashJoin 上下文 4.39 GB ≈ 270+ 个 16 MB 配额单位
   在同一查询内叠加（2026-10-21 现场样本）。
2. **优化器估算优先于 work_mem**：计划节点带 `operatorMemKB[]`，由内核 memctl
   子系统从优化器的 `plan->mem_info.maxMem` 赋值；该值 >0 时执行侧直接用它，
   work_mem 只是 `operatorMemKB<=0` 时的兜底（`SET_NODEMEM` 宏，executor.h:114）。
   **推论：对带估算配额的算子，调 work_mem 可能不生效**。
3. **SMP 并行分摊**：配额 > dop×64 KB 时按 dop 平分给各 worker（executor.h:116）。
4. **弹性伸缩**：算子内存可在 allowedMem → operatorMaxMem 之间自动扩展，条件是
   全局内存不紧张（`gs_sysmemory_busy` 为假）且 maxMem > allowedMem
   （tuplesort.cpp:579）；工作负载租户场景禁用 auto spread（memctl.cpp
   AdjustMemForEstimationCrisis 注释）。**真实内存上限是 operatorMaxMem，
   不是 work_mem**。
5. **全局水位参与落盘判定**：`LACKMEM` 不只看本算子 availMem<0，还看
   `gs_sysmemory_busy(usedMem×dop)`（tuplesort.cpp:554）——系统级内存紧张时，
   未超自身配额的算子也会提前落盘。**所以"spill 多"不一定等于"work_mem 小"，
   可能是全局水位压力**。
6. **哈希倾斜专项**：hash join 预留 2% 哈希表内存做倾斜桶优化
   （SKEW_WORK_MEM_PERCENT=2，hashjoin.h:99）；单算子配额上限 4 GB
   （MAX_OP_MEM，memctl.h:36）。
7. **max_process_memory 是另一层机制**：进程级记账，超限报
   53200（ERRCODE_OUT_OF_LOGICAL_MEMORY，分配层统一拦截）；
   它管"整个进程还能不能分配"，与算子 spill 决策（"这个算子要不要写临时文件"）
   是两层。调大 work_mem 挤爆的是这一层。

## 源码链路（openGauss 6.0.0 基线，行号会漂以函数名为锚）

### 配额生成 — 计划阶段（cbb/workload/memctl.cpp）

- `AssignMemOpConsumption`（约 3090 行起）遍历计划树，把优化器
  `plan->mem_info.maxMem/minMem` 写入 `node->operatorMemKB[0]`（宏
  `ASS_AND_ADJ_MEM`/`ASS_AND_ADJ_AGG_MEM`，3113/3118 行）：**乐观取 maxMem，
  因为不希望落盘**。
- 估算过大时 `AdjustMemForEstimationCrisis`（3069 行）收敛：子节点最大内存的
  一半（HALF_AMOUNT）与 STATEMENT_MIN_MEM 取大者，且 minMem 下限 =
  estOpMem ÷ HASH_MAX_DISK_SIZE（=32，relation.h:188）。
- Stream 节点配额置 0 特殊处理；工作负载组可整体覆盖算子配额
  （consumer/producer maxMemKB，1601 行；ng_operatorMemKBArray，2169 行）。

### 配额合成 — 执行侧（include/executor/executor.h:114）

```c
#define SET_NODEMEM(opmem, dop) \
    ((opmem > 0 ? opmem : u_sess->attr.attr_memory.work_mem) / \
        ((opmem > 0 ? opmem : u_sess->attr.attr_memory.work_mem) > (SET_DOP(dop) * 64) ? SET_DOP(dop) : 1))
```

读法：opmem>0（有估算）用 opmem，否则回退 work_mem；配额够大（>dop×64KB）才按
dop 平分，否则每个 worker 各给全额。

### 哈希算子（runtime/executor/nodeHash.cpp）

- 317 行：`local_work_mem = SET_NODEMEM(node->plan.operatorMemKB[0], node->plan.dop)`
- 388 行：`hashtable->spaceAllowed = local_work_mem * 1024L`（字节级上限）
- `ExecChooseHashTableMaxTuples`（523 行）按 tupwidth 推桶数；
  538 行：`skew_table_bytes = hash_table_bytes * SKEW_WORK_MEM_PERCENT / 100`
  （倾斜优化占 2%）；超配额走 batch 加倍 + 落临时文件。

### 哈希 batch 加倍（ExecHashIncreaseNumBatches，nodeHash.cpp:994-1140，深读）

- 触发：内表装载量超 spaceAllowed；`growEnabled` 门控 + 溢出保护
  （oldnbatch 上限检查，超限直接放弃加倍）
- 动作：**nbatch×2**；首次落盘 lazily 建内外 batch 文件数组 +
  `PrepareTempTablespaces`；顺带把 nbuckets 升到 optimal（只升不降）
- **重分布代价（判读核心）**：清空桶数组 → **全量扫旧 chunk，逐条按
  ExecHashGetBucketAndBatch 重算归属**——留在 curbatch 的继续内存态，其余
  写进对应 batch 文件。即**每次加倍=当前批全量重写一遍**（内存→临时文件
  的自搬运计入 BufFileWrite）
- 判读：哈希键劣/数据倾斜导致 nbatch 连环翻倍时，BufFileWrite 里相当部分
  是"重分布自搬运"而非真数据量，IO 放大随翻倍级联——**先看键分布再调配额**

### 排序算子（runtime/executor/nodeSort.cpp + common/backend/utils/sort/tuplesort.cpp）

- nodeSort.cpp:76-78：`sort_mem = SET_NODEMEM(operatorMemKB[0], dop)`，同时取
  `operatorMaxMem` 作弹性上限传入 tuplesort。
- tuplesort.cpp 记账模型：`allowedMem/availMem`（248 行注释块），每次缓存元组
  USEMEM、释放 FREEMEM；`LACKMEM`（550 行）=
  `availMem<0 || gs_sysmemory_busy(usedMem×dop)`；`CHECK_FOR_EARLY_FREE`/
  弹性扩展（579 行）= 全局不忙且 maxMem>allowedMem 时上调。
- 落盘物化走逻辑磁带（logtape.cpp）→ BufFile 临时文件——即 AWR 里的
  **BufFileWrite / BufFileRead 事件**与 fd 分类树里的 pgsql_tmp。

## 排序落盘生命周期（tuplesort 状态机，深读）

排序算子从内存到落盘的完整状态机（`common/backend/utils/sort/tuplesort.cpp`）：

```text
TSS_INITIAL（装元组，记账 availMem）
   └─ LACKMEM（availMem<0 或 memtupcount≥memtupsize）→ inittapes
TSS_BUILDRUNS（ Algorithm D：Knuth 多相归并的生成阶段 ）
   ├─ 每次触发 dumptuples：先对内存元组快排（形成有序 run）→ 写入逻辑磁带
   │  （此处报 STATE_EXEC_SORT_WRITE_FILE，即 AWR 的 "Sort - write file"）
   └─ 输入耗尽 → mergeruns
终态三选一：
   TSS_SORTEDINMEM  —— 从未超配额，全内存完成（无 temp）
   TSS_SORTEDONTAPE —— 仅 1 个 run：直接当结果，不归并（源码注释：数据量在
                        1X~2X 配额之间时常见；Knuth 算法外的优化）
   TSS_FINALMERGE   —— 多 run：边归并边出（"Sort - fetch tuple" 即归并读回；
                        归并阶段换 slab 分配器、禁用缩写键）
```

关键细节：

1. **磁带数由配额决定**：`maxTapes = tuplesort_merge_order(state->allowedMem) + 1`
   （inittapes:2345）——配额越大归并路数越多、归并轮数越少
2. **Algorithm D 斐波那契分布**：tp_fib/tp_dummy 数组按 D3/D4 步推进
   （selectnewtape:2429），run 分布按斐波那契数列预置以最小化归并轮数
3. **磁带缓冲计入配额**：tapeSpace 从 availMem 扣除（inittapestate:2398-2401）——
   实际可用于元组的内存略小于配额
4. **dump 前快排**：每批先 quicksort 再写带（dumpbatch "starting quicksort of
   run N"）——temp 里的每个 run 都有序，归并只做路数合并
5. **run 数上限**：INT_MAX，超出报 PROGRAM_LIMIT_EXCEEDED（理论值，实际先撞
   磁盘/时间墙）

**判读增量**：AWR 出现 "Sort - write file" + "Sort - fetch tuple" 成对且量大 =
进入了 TSS_BUILDRUNS→TSS_FINALMERGE 全流程（真落盘归并）；只有 write 没有
fetch = 单 run 直读（TSS_SORTEDONTAPE，temp 量 ≈ 1~2×配额的形态）；
两者皆无而 CPU 高 = TSS_SORTEDINMEM 纯内存排序（调 work_mem 无收益）。

## 对 AWR 判读的映射

| AWR 现象 | 机制解释 |
|---|---|
| HashJoin_<tid> 上下文 GB 级 | 配额 × 算子数叠加（HashJoin 上下文 = 该查询全部哈希节点） |
| BufFileWrite/Read 高频 | 算子落盘的物理路径；量 = spill 规模 |
| spill 多但 work_mem 不小 | 检查两件事：算子是否吃的是估算配额而非 work_mem；gs_sysmemory_busy 是否因全局水位提前触发 |
| string_agg(DISTINCT) 大分组 | 倾斜分组排序集 > 任何合理配额，调参无效，只能改写 |
| 53200 报错 | max_process_memory 进程记账层，与算子配额联动：调大算子配额会先打满这层 |

调参安全核算公式：**并发查询数 × 每查询算子数 × 配额**，且以
`operatorMaxMem`（弹性上限）而非 work_mem 为准；以 AWR
Max Session Memory 实测值为锚反推每查询算子数。

## 源码阅读范围约定

默认排除 cstore、ustore、段页式存储引擎与 nvm 路径——VastBase 默认不启用，
属不稳定特性；仅当交付环境确认启用时才纳入精读范围。

## 版本注记

- 基线 openGauss 6.0.0（VastBase G100 V3.0 Build 8）；社区 Postgres 的
  work_mem 语义（无 operatorMemKB/弹性/全局水位）不适用于本文件结论
- memctl 的工作负载覆盖路径（consumer/producer、ng_ 数组）随 WLM 配置启用，
  默认环境通常走优化器估算路径

## 来源

- `include/executor/executor.h:114` SET_NODEMEM 宏（2026-10-25 精读）
- `gausskernel/runtime/executor/nodeHash.cpp:317,388,523,538`；二次补读
  ：994-1140（ExecHashIncreaseNumBatches 全函数：倍增/重分布/批文件惰性建立）
- `gausskernel/runtime/executor/nodeSort.cpp:76`
- `common/backend/utils/sort/tuplesort.cpp:248,550-579`
- `gausskernel/cbb/workload/memctl.cpp:1286,1601,2169,3060-3140`
- `include/workload/memctl.h:36`、`include/executor/hashjoin.h:99`、
  `include/nodes/relation.h:188`
- 实测样本：2026-10-21 现场 AWR（HashJoin 4.39 GB 上下文 / 75 GB 每小时 temp）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
