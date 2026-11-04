# 执行器算子模型（NestLoop / MergeJoin / Agg / WindowAgg / Material）

## 概述

> 服务分类：performance（SQL 时间分解列归因）、sql（计划形态解读）。HashJoin
> 与 sort/落盘在 `operator-memory.md`（不复制）；本文件补其余主力算子的
> 执行模型与**内存配额接线**。

## 内存配额接线（全部算子的共同底座）

`SET_NODEMEM(operatorMemKB, dop)` + `operatorMaxMem` 弹性上限——已实锚三处：
**Agg**（nodeAgg.cpp:249-250 workMem/maxMem）、**Material**（nodeMaterial.cpp:54
`tuplestore_begin_heap(…, operator_mem, max_mem, …, SET_DOP(dop))`）、
Sort/Hash（operator-memory.md）。**推论**：AWR 里"调 work_mem 不生效"的机制
出口在各算子都一样——优化器估算配额（operatorMemKB>0）优先，work_mem 只是
兜底（`operator-memory.md` §2）。

## Join 三算法执行模型

### NestLoop（nodeNestloop.cpp）
- 外层逐行；内层**每外层行重扫**（:201 `ExecReScan(inner_plan)`）
- **NESTLOOP_PARAM 机制**（:180-197 深读）：外层当前行的值写入 ExecState
  参数（`prm->value = …`）→ 标记 `inner_plan->chgParam` → 重扫内层时内层
  索引按新参数**点查**——这就是"参数化内表索引扫"的执行本体（planner 侧
  对应参数化路径，query-pipeline plan 段）
- **判读**：内层 index scan 的执行次数 ≈ 外层返回行数（索引扫描次数暴涨
  而 plan 显示 NL 是正常形态）；无参数化的整表内层重扫是灾难形态（planner
  会自动插 Material 缓存——非参数化 NL+无 Material=计划走形信号）
- semi/anti 提前退出；注释实证 mergejoin 内层不缓存数据的特殊分支（⚓文件名待补 :208+）

### MergeJoin（nodeMergejoin.cpp）
- **状态机**（:608-686+）：EXEC_MJ_INITIALIZE_OUTER → INITIALIZE_INNER →
  SKIP_TEST → JOINTUPLES / SKIPOUTER / SKIPINNER / ENDOUTER / ENDINNER …
  ——双侧各持游标**只前进**（有序性换零重扫）；mark/restore 支持唯一连接回退
- 双侧有序是前提（planner 前置 Sort——**mergejoin 的排序成本先看 sort_time
  列**，join 本体不吃 sort_time）
- 判读：大偏斜分布下 SKIP 单边狂奔形态（一侧游标长跳）＝数据分布问题非算子问题

### HashJoin
- 见 `operator-memory.md`（build/probe/倾斜桶/batch 加倍全深读）——引用。

## Agg 四策略（nodeAgg.cpp）

策略分派（:1601 switch；:113/:1148 约束）：

| 策略 | 机制 | 判读 |
|---|---|---|
| AGG_PLAIN | 无分组或输入已聚成单组 | — |
| AGG_SORTED | 输入按组键有序，**边流边聚**（不建哈希表） | 内存最省；代价在输入排序（sort_time） |
| AGG_HASHED | 哈希表装组键（hashcontext） | **hash_time 列主体**；内存=配额接线（:249） |
| AGG_MIXED | 分组集（GROUPING SETS）混合 | 复杂 SQL 才见 |

## WindowAgg（nodeWindowAgg.cpp）

- 分区缓冲走 tuplestore + 三向游标 **WINDOW_SEEK_CURRENT/HEAD/TAIL**
  （:1818-1906）——窗口函数"回看不回算"的机制本体
- 排序前置（PARTITION BY/ORDER BY 的 Sort 是主要成本）；缓冲超配额走
  tuplesort 落盘（operator-memory 同族）

## Material（nodeMaterial.cpp）

- `tuplestore_begin_heap(…, operator_mem, max_mem, …, SET_DOP(dop))`（:54）——
  物化=配额内的输入缓存（给无参数化 NL 内层兜底、CTE 物化等）

## SMP/stream（向量化路径）

- 本基线 stream 节点在 **vecexecutor/vecnode/vecstream.cpp**——SMP 并行在
  向量化执行器路径上（dop 分摊配额即 SET_NODEMEM 的 dop 除法，见
  `operator-memory.md` §3）；调度细节⚓（目录级）

## AWR 判读映射

| 特征 | 归因 |
|---|---|
| hash_time 高 | AGG_HASHED/HashJoin 的 build 侧（含倾斜桶） |
| sort_time 高 | 前置 Sort（MergeJoin/Sorted Agg/WindowAgg/OrderBy）——先看 plan 里有谁在吃排序 |
| 索引扫次数≈外层行数 | NestLoop 参数化内表（正常形态） |
| 无参数化 NL 且无 Material | 计划走形信号 |
| 内存涨但 work_mem 调大无效 | 配额接线（operatorMemKB 优先）——`operator-memory.md` |

## 常见错误

| 错误 | 纠正 |
|---|---|
| NL 重扫一律当病 | 参数化点查是设计形态，看次数≈外层行数即健康 |
| MergeJoin 慢怪 join | 本体只前进，成本多半在双侧前置 Sort |
| 聚合内存调 work_mem 不动 | 走配额接线判读（本文件+operator-memory） |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8），2026-10-25 深读：
  nodeNestloop.cpp:180-210（NESTLOOP_PARAM/重扫/mergejoin 分支注释）、
  nodeMergejoin.cpp:608-686+（EXEC_MJ 状态机）、nodeAgg.cpp:53-113,249-250,
  1144-1248,1601（策略与配额接线）、nodeWindowAgg.cpp:1818-1906（三向游标）、
  nodeMaterial.cpp:54-77（tuplestore+配额）、vecexecutor/vecnode/vecstream.cpp
  （SMP 位置）
- ⚓：SMP 调度细节、向量化算子族差异

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：配额接线→
operator-memory（同一 SET_NODEMEM 底座）；NESTLOOP_PARAM→query-pipeline
plan 段（参数化路径的执行侧本体）；sort/hash 列归因→metrics（SQL 维度列）。
