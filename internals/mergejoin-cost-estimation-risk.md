# MergeJoin 代价评估的隐藏 IO 与 ProcArrayLock 风险

## 概述

> 服务分类：sql（计划生成侧风险）+ performance（CPU 高/Plant Time 异常/
> ProcArrayLock 争用判读）+ troubleshooting（数据库整体卡死+连不上的罕见根因）。
> 回答：为什么"生成计划"这个纯 CPU 阶段会去扫索引、会抢全局锁、会在死元组
> 多的库上把 Plant Time 拖到天文数字。

## 机制链（源码实锚）

优化器估 MergeJoin 成本时，统计信息里**没有列的 min/max**，于是现场扫索引：

```
initial_cost_mergejoin (costsize.cpp:3246 起)
 └→ cached_scansel (costsize.cpp:3690)        # 同一 PathKey 只算一次
     └→ mergejoinscansel (costsize.cpp:3709)
         └→ get_actual_variable_range (selfuncs.cpp:188/956)
             └→ index_beginscan + 索引两端游走，找可见的 min/max 元组
```

**要害：用的是 NOW 快照**。NOW 快照判可见性时，每个 t_infomask 未标
HEAP_XMIN_COMMITTED 的元组都要调 `TransactionIdIsInProgress`；其快速排除
（xid < RecentXmin / 已知完成 / 本事务）都失败时，**S 模式请求 ProcArrayLock
遍历 ProcArray**。

两个放大器：
1. **叶子两端大量死元组/未提交元组**：一直找不到"可见"的端点元组 → 沿叶子链
   无限下探，逐元组做上述判断——估价变成全索引扫描；
2. **并发 + 连接风暴**：估价线程反复 S 持锁，与 ProcArrayAdd/Remove 的 X 请求
   （进程加入/退出 ProcArray，加入满即 FATAL "too many clients"）互踩。

## 两个实战形态（核心逻辑）

**形态一：整体卡死 + 连不上（未提交大事务触发）**
两个长事务持续批量 INSERT 表 A（未提交）→ 大量 JOIN 查询估价 mergejoinscansel
以 NOW 快照扫 A 索引找 min/max → 未提交元组不可见，逐个 TransactionIdIsInProgress
频繁 S/X 争 ProcArrayLock → 堆栈见 2199 线程卡 ProcArrayAdd（X）等锁，同时
估价线程长持不走 → 新连接进不来（no free proc slot 形态）。**关 enable_mergejoin
立即恢复**。

**形态二：CPU 持续高 + 个别表慢（死元组触发）**
7 天前长事务卡死 OldestXmin → 表 1900 万死元组无法 vacuum → 估价扫索引端点
全是死元组，沿叶子链一路下探 → **Plant Time 占满 DB Time（9s 窗口 999s）**，
等待落在 buffer 读（PlanTime 阶段做 IO，表面自相矛盾，本文件解释）。
关 mergejoin 恢复。

## 判读与处置

| 现象组合 | 指向 |
|---|---|
| Plant Time 独大 + 等待在 buffer + 堆栈 mergejoinscansel/get_actual_variable_range | 本风险（查该表死元组/长事务） |
| 大量 ProcArrayLock S 等待 + 建连 FATAL too many clients + 估价堆栈 | 本风险并发形态 |
| 关 enable_mergejoin 立即恢复 | 强佐证（紧急 workaround） |

- 紧急：enable_mergejoin=off（注意与 G100 基线 on 的偏离记录，恢复后回开）
- 根治：清长事务（推进 OldestXmin）→ vacuum 死元组大户；避免大事务批量插索引两端
- 版本演进：VastBase 3.0.9 起估价快照 NOW→MVCC（基本消除锁争用），
  3.0.9 PSU1 进一步改 ANY 快照（基本规避整个风险）——低版本环境优先升级路径

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| "Plant Time 是纯 CPU 阶段不可能有 IO 等待" | get_actual_variable_range 真实扫索引 | 本文件两形态，先看堆栈 |
| CPU 高/卡死只查业务 SQL 执行 | 卡死点在计划生成阶段，执行榜看不到 | pstack 找 mergejoinscansel 指纹 |
| 死元组多只担心扫描膨胀 | 估价阶段就会受害（本文件），先于执行 | 死元组大户治理优先级上调 |

## 版本注记

- 基线 openGauss 6.0.0：costsize.cpp:3246/3690/3709、selfuncs.cpp:188/956 锚点
- 快照改型（NOW→MVCC→ANY）为 VastBase 定制演进，开源基线仍 NOW——低版本判读按本文

## 来源

- 内部文档《优化器 mergejoin 代价评估的风险》（含两实战堆栈与处置；
  2026-11-04 用户提供 D:\二线团队）
- 源码核实（2026-11-04）：估价链四锚点如上；ProcArray 语义见 `locks.md`
- 关联：`locks.md`（ProcArrayLock S/X 与 GetSnapshotData）、
  `stats-to-cost.md`（统计缺失的代价出口又一例）、`autovacuum.md`（死元组根因）、
  `executor-operators.md`（MergeJoin 执行本体）、`query-pipeline.md`（plan 段位置）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
