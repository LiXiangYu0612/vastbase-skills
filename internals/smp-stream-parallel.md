# SMP 并行执行（Stream 线程模型）与判读（smp-stream-parallel.md）

## 概述

> 服务分类：internals（机制层）+ performance（计划判读）。回答：query_dop 打开后
> 内核里发生了什么（Stream 线程怎么建/怎么等/怎么合）、EXPLAIN 里 Streaming 节点
> 怎么读、SMP 为什么"没生成"、SMP 与算子内存配额怎么咬合。
> 与 `executor-operators.md`（配额接线 SET_NODEMEM 已锚）和
> `sql/references/10_SMP_Hints`（外部视角：hint/query_dop 决策表）互补——本文件是
> 内核侧。〔排除〕跨机分布式形态只提一句（本域集中式不涉）。

## 1. 规划侧：dop 闸与计划形态

- **入口闸**：`query_dop > 1` 才进 SMP 规划（planner.cpp:775；结果计划记
  `result->query_dop` :1015）。
- **子计划禁并行**：规划子计划时把 query_dop 临时置 1 再复位
  （planner.cpp:3202-3206）——**子查询/子计划永远单线程跑**，判读"SMP 没吃上"
  先看计划结构里并行段是否被包进子计划。
- **EXPLAIN 形态**：`Streaming (type: GATHER)`（streampath_base.cpp:510 注释
  自带样例）。type 族 = GATHER / REDISTRIBUTE / BROADCAST / LOCAL_GATHER /
  LOCAL_REDISTRIBUTE / LOCAL_BROADCAST / HYBRID：
  - 节点内 SMP 用 **LOCAL_***（本机线程间重分布/汇聚）；
  - JOIN 侧的广播/重分布决策：`JoinPathGenBase::isReplicateJoinCanRedistribute`
    （streampath_base.cpp:561）+ `setStreamBaseInfo(STREAM_BROADCAST/...)`
    （:647-724 按复制表/内外侧选择谁广播）；
  - `parallelLocalRedistribute`（:810）：双侧都本地重分布的形态。
- 代价：stream_cost.cpp 单独核算 Stream 传输/启动成本——SMP 计划不出现多半是
  代价没算赢（单表太小/启动成本盖过收益），不是坏了。

## 2. 执行侧：Stream 线程模型

- **线程入口 `StreamMain()`**（streamMain.cpp:72；进入日志带
  StreamKey(queryId, planNodeId, smpId) :326）——每个 Stream 计划节点按 dop 起
  一组 consumer/producer 工作线程，主会话线程做协调。
- **主侧驱动 `ExecStream`**（execStream.cpp:74）：从 consumer 收流；
  **归并排序在 consumer 侧**（`InitStreamMergeSort` :488——多路有序输入的合并
  点，SMP 排序/归并 JOIN 的执行本体）。
- 流控：`InitStream`/`InitStreamFlow`（:591/:78）控制传输批量与背压。
- 全局对象 StreamNodeGroup（streamCore.cpp:1302/:1351，线程组生命周期与销毁）。

## 3. 等待事件四态（SMP 判读专属）

pgstat.h:1186-1189（streamConsumer.cpp:422,444 实报）：

| STATE_STREAM_* | 含义 | 判读 |
|---|---|---|
| WAIT_CONNECT_NODES | 等 stream 连接建立 | 大量出现=建连慢（socket/线程资源） |
| **WAIT_PRODUCER_READY** | consumer 等 producer 就绪 | **最常见**：worker 起得慢/卡在初始化（配额/内存/计划复杂） |
| WAIT_THREAD_SYNC_QUIT | 等工作线程同步退出 | 语句收尾拖长=某 worker 未退（看该 worker 堆栈） |
| WAIT_NODEGROUP_DESTROY | 等 NodeGroup 销毁 | 同上，收尾侧 |

SMP 场景 AAS 高但 DB CPU 低 → 先把这四态在 ASP 里过一遍：等待在生产者侧还是
收尾侧，方向完全不同。

## 4. SMP × 内存配额（与 executor-operators 的接缝）

- 每个 worker 的算子配额按 `SET_NODEMEM(operatorMemKB, dop)` 摊
  （executor-operators 已锚：nodeAgg.cpp:249-250 同款）——**总内存 ≈ 配额×dop×
  算子数**，query_dop 调大前先算这笔账，53200/P7 形态常由此触发。
- gs_sysmemory_busy 全局水位同样会让 SMP worker 提前落盘（spill 多≠work_mem
  小，同 operator-memory 结论）。

## 5. "SMP 没生成"排查链（整合既有配方）

1. query_dop 闸（planner.cpp:775；值>1 才有 SMP）
2. 计划里有没有 Streaming 节点（EXPLAIN type: LOCAL_*）
3. 子计划禁并行（:3202-3206）——并行段被包进子查询=白开
4. 代价没算赢（stream_cost；表太小/启动成本）
5. hint 冲突/unused 警告（sql/references/10）
6. 还要内核证据时：debug 配方 `logging_module=on(OPT,OPT_JOIN)+debug5`
   （debug-logging-gucs）回放路径决策

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| query_dop 开了就该有 SMP | 子计划禁并行+代价闸双重过滤 | §5 六步链 |
| AAS 高+CPU 低当锁问题 | SMP 等待四态不是 LOCK 账本 | §3 四态先过 |
| dop 调大不算内存账 | 配额×dop×算子数线性放大 | §4 |
| Streaming 节点当跨机分布式 | 集中式 SMP 全是 LOCAL_*/GATHER 本机线程 | §1 |

## 版本注记

- 源码基线 openGauss 6.0.0 正式树（2026-11-15 精读）：gausskernel/process/stream/
  六件 + optimizer/path/streampath_base.cpp + optimizer/plan/planner.cpp。
- 深读边界：线程建/销毁全链（streamCore StreamNodeGroup 全生命周期）、
  向量化 vecstream 路径细节维持结构级锚⚓——遇到实战再深。

## 来源

- openGauss 6.0.0 正式树精读（2026-11-15）：streampath_base.cpp:510,561,647-724,810、
  planner.cpp:775,1015,3202-3206、streamMain.cpp:72,326、execStream.cpp:74,78,591,488、
  streamConsumer.cpp:422,444、streamCore.cpp:1302,1351、pgstat.h:1186-1189。
- 联动：executor-operators（配额接线/向量化分发表）、debug-logging-gucs
  （SMP 未生成 debug 配方）、sql/references/10（hint/决策表外部视角）、
  operator-memory（53200/spill 形态）。

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
