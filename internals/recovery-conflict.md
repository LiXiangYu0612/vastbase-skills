# 备库回放冲突（Recovery Conflict）与 hot standby 快照

## 概述

> 服务分类：ha（备库可读架构）+ performance（备库查询被 cancel 的判读）+
> troubleshooting（"terminating/canceling ... due to conflict with recovery"）。
> 回答：冲突有哪几类、snapshot 冲突为什么最深（Vacuum/Freeze 两条触发链）、
> 备库快照怎么维护、为什么会出现"备库连不上+主库 COMMIT 卡死"连锁、参数怎么控。

## 冲突五分类（根因总表）

hot standby 备库可读后，**查询与回放访问同一资源**即冲突：

| 类型 | 场景 | 典型报错 |
|---|---|---|
| Database | 备库连接的库已被主库 DROP | terminating connection due to conflict with recovery |
| Tablespace | 备库访问的表空间已被主库删除 | 同上 |
| Lock | 备库查询持 1 级锁，回放 DDL 要 8 级锁 | 同上 |
| **Snapshot** | 备库快照可见的元组被回放清理（Vacuum/Freeze） | canceling statement due to conflict with recovery |
| Bufferpin | 备库读页与回放写页的 pin 冲突 | 同上 |

解决框架（`max_standby_streaming_delay` 的两个选择）：优先查询（回放等）
或优先回放（查询被杀）。

## Snapshot 冲突深链

### 触发链一：Vacuum

死元组回收（手动 VACUUM / autovacuum / heap_page_prune）在主库清理了对备库
快照仍可见的元组 → 备库回放清理 XLOG 时冲突。

**不产生冲突的两类死元组**（判断冲突面的排除项）：abort 事务插入的元组
（无人可见）；同事务内 INSERT 后又 DELETE 的元组（备库无人可见过）。
只有"曾对其他事务可见、后变死"的元组才可能冲突——这也是
`latestRemovedXid` 取"本页清理死元组最大 xmax"的原因
（heapam.cpp:8066/8103 写入，备库回放侧 heapam.cpp:8785 调
`ResolveRecoveryConflictWithSnapshot`，standby.cpp:279）。

### 触发链二：Freeze（页面级）

**VB 64bit XID 的实现**：元组头仍 4 字节短 XID + 页头存 8 字节基准
（pd_xid_base/pd_multi_base），真实 XID = base + 短值。实例级 XID 耗尽解决，
但页面可表示范围为 [base, base+2^32]——**新事务 XID 超出范围时触发页面级
Freeze**：把页内旧元组 XID 改写为 FrozenXID（对一切快照可见），记录 Freeze
XLOG（含 cutoff_xid）。

**冲突机理**：备库快照 S 下某 INSERT 不可见（其事务未提交）→ 回放 Freeze
把该元组 XID 冻结为"全部可见" → **可见性被回放改变** = 冲突。

### 检测与 VB 的"暴力禁快照"

检测：回放 Vacuum XLOG 时遍历备库查询事务，比较其快照 xmin 与
latestRemovedXid（xmin ≤ latestRemovedXid = 该查询能看到被清理元组 = 冲突）。

**PG 与 VB 的关键差异**：PG 串行回放 + KnownXids 数组 → 备库快照 xmin 准确，
回放到 Vacuum XLOG 时"能看到死元组的事务都已结束"天然成立。VB 的
standbyXmin 由主库提交日志捎带（见下节），**可能比真实值小** → 上述判断不
充分 → VB 采用暴力补偿：回放 Vacuum/Freeze XLOG 期间更新
`standbyRedoCleanupXmin`（=latestRemovedXid/cutoff）与
`standbyRedoCleanupXminLsn`，**获取快照时若
standbyXmin ≤ standbyRedoCleanupXmin 且 standbyRedoCleanupXminLsn > redoEndLsn
则拒绝发快照**（procarray.cpp:1839-1840 实锤）。

### 连锁事故形态（2210.5 实战，核心逻辑）

备库回放 Freeze XLOG 更新上述两变量 → 查询长事务不结束 → 回放等它
（max_standby_streaming_delay=1h）→ 条件无法打破 → **备库拒绝发快照**：
1. 新连接建立需快照访问数据字典 → **备库连不上**；
2. 备库 walreceiver 应答主库前要取一次快照（拿最新 xmin）→ 取不到 →
   不应答 → **主库同步复制 COMMIT 卡死**（synchronous_commit=on）。

处置要点：打破任一条件即可——杀备库长查询（让回放推进过
standbyRedoCleanupXminLsn）或临时放开查询限制。根治方向（文档提案）：
用 **CSN 比较**替代 xmin 比较（回放 Vacuum 时按 latestRemovedXid 查 CSN，
与各查询快照 CSN 比较）即可免禁快照；并行回放下需控制 Vacuum XLOG 在
其目标事务 COMMIT 回放完成后再回放（VACUUM XLOG 无事务边界，可能与事务
XLOG 交织——边缘触发探测不到回放期间并发获取的快照）。
XLOG_BTREE_REUSE_PAGE 只记 cutoff_xid 无 CSN，不可套用；
UStore 的回滚段回收冲突是另一族（undo 空间，默认环境不启用）。

## 备库快照的维护（三变量）

备库只有读事务，ProcArray 非真实事务，快照三要素全靠回放维护：

| 变量 | 推进方式 | 用途 |
|---|---|---|
| standbyXmin | 主库提交时把快照最小事务 ID（RecentXmin）记入 XLOG；备库回放取 max 推进 | 快照 xmin |
| latestCompletedXid | 回放事务结束日志取更大 XID 推进 | 快照 xmax |
| nextCommitSeqNo | 回放提交日志取更大 CSN 推进 | 快照 csn |

standbyXmin 滞后（可能比真实小）是禁快照暴力法的根因（见上节）。

## 参数控制面（判读速查）

| 参数 | 作用 | 代价/坑 |
|---|---|---|
| hot_standby_feedback | 备库定期（wal_receiver_status_interval）上报最小 XID，主库 Vacuum 让步 | 主库膨胀/计划受影响；延迟回放备库同时开启更放大 |
| vacuum_defer_cleanup_age | 延迟 N 个**事务**再清理（非时间） | 量纲反直觉，效果难预期 |
| max_standby_streaming_delay | -1 无限等查询；≥0 等满即杀查询 | 流复制场景；过大=备库延迟与连锁事故窗口（上文 1h 案例） |
| max_standby_archive_delay | 同上 | 归档恢复场景 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 看到 conflict with recovery 就调大 delay 一刀切 | 长查询 + Freeze 回放的组合会把"等查询"变成全局禁快照连锁 | 先找长查询与冲突类型；1h 级配置属事故配置 |
| 把备库连不上只当网络/监听问题 | 禁快照时建连需快照访问字典，直接拒绝 | 连锁第一环，配 max_standby 配置与长查询现场核对 |
| 主库 COMMIT 卡只盯主库 | 同步复制把备库"不能应答"灌回主库 | 查备库快照服务状态（本文连锁链） |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 档）；procarray.cpp:1839-1840 为禁快照双条件锚点，
  standby.cpp:279/183 为冲突解决族锚点，heapam.cpp:8066/8103/8785 为
  latestRemovedXid 写读锚点
- 2210.5 连锁案例为 VastBase 现场实录（内部文档《主备 hot standby 特性思考》）

## 来源

- 内部文档《RecoveryConflict》（2026-01）与《主备 hot standby 特性思考》
  （2026-05，含 2210.5 现场还原与 CSN 修复提案；2026-11-04 用户提供 D:\log\1）
- 源码核实（2026-11-04，openGauss 6.0.0 树）：procarray.cpp:1839-1840（禁快照
  双条件）、standby.cpp:40/43/147/183/279（冲突解决族）、heapam.cpp:8066/8103/8785
  （latestRemovedXid）
- 关联：`wal-replay.md`（回放架构与并行回放——冲突探测的边缘/水平触发差异）、
  `mvcc-visibility.md`（快照可见性公式）、`replication-lag.md`（Apply 段含
  conflict cancel）、`read-write-splitting.md`（驱动层把冲突报错转发主库）、
  `../linux/network.md`（客户端消失三形态——备库长查询来源之一）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
