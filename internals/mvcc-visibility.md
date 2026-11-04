# MVCC 可见性（CSN 快照判定链）

## 概述

> 服务分类：performance（死元组/膨胀/长事务判读）、troubleshooting（可见性异常）；
> 联动 `locks.md`（行锁xmax）、`flush-checkpoint.md`（CSNLOG 截断）

openGauss/VastBase 的 MVCC 用 **CSN（提交序号）快照**而非社区 Postgres 的 xid 数组：
pg_csnlog（SLRU）为每个事务记提交序号，快照只存一个 `snapshotcsn` 视界——
可见性判定 O(1)，无需扫描 in-progress xid 列表。

## 核心公式（snapmgr.cpp `XidVisibleInSnapshot`）

```
元组可见  ⇔ xmin 已提交 且  CSN(xmin) < snapshotcsn     （严格小于）
元组已删  ⇔ xmax 已提交 且  CSN(xmax) < snapshotcsn
```

严格小于意味着**恰好在快照边界提交的事务不可见**——串行化语义的根基。
CSN 取自 `TransactionIdGetCommitSeqNo`（查 pg_csnlog）。

## 判定链（heapam_visibility.cpp:1026 HeapTupleSatisfiesMVCC）

1. **hint bits 快捷**：XMIN_COMMITTED/INVALID 标志直接出结果；**冻结位跳过提交
   检查**（freeze 后可见性与事务状态解耦——这就是 table_age/relfrozenxid 的意义）
2. **自己的事务**：cmin/cmax 对比快照 curcid（命令级可见性），含 multixact 与
   combocid 分支
3. **他人事务**：走核心公式；首次检查到提交/中止会 **SetHintBits 写回页**
   （MarkBufferDirtyHint）——**只读查询也会弄脏页**，这是纯读负载仍有写 IO 的
   来源之一（判读：DB 侧 iostat 有写、dirty 少量涨的正常成分）
4. xmax 侧：`HEAP_XMAX_IS_LOCKED_ONLY`（仅锁未删）不影响可见性；multixact 取
   实际更新者
5. **防御性一致性检查**（openGauss 特有）：hint 位与 CSN 状态矛盾时报
   "set HEAP_XMIN_INVALID xid don't abort" 类错误——出现即可见性数据不一致
   现场（升级/崩溃恢复后偶发），属诊断金标而非普通业务错误

文件头注释的经典竞态说明仍适用：**必须先查 IsInProgress 再查 CLOG 提交状态**，
顺序颠倒会把"正在提交"误判为中止（xact.c 先写 clog 再清 PGXACT 的顺序保证）。

## OldestXmin 语义（VB 与 PG 关键差异，2026-11-04 源码定论）

**PG 语义**：GetOldestXmin 遍历全部 proc 取 min(pgxact->xmin)——idle in
transaction 会话持 xmin 到事务结束，**必然钉住 OldestXmin 卡 vacuum**。

**VB（openGauss）语义**：`GetOldestXmin`（procarray.cpp:1585 起）**不扫 proc**，
返回 **recentGlobalXmin 水位线**：
- 水位线由快照计算时的 `UpdateRecentGlobalXmin`（:1524，CAS 单调）推进；
- 下界取 `GetMultiSnapshotOldestXmin()`（:5200）= 多版本快照结构
  `g_snap_current->localxmin`——**所有当前有效快照版本的最小 xmin**；
- 只读事务语句结束后不持有效快照版本 → **长 idle in transaction 不钉
  OldestXmin**；配 `enable_defer_calculate_snapshot=on`（快照延迟计算）更甚；
  `ProcArrayResetXmin`（:5207，清 pgxact->xmin）是同套"空闲不占 xmin"实现件。
- 根基：VB 快照=CSN 三元组（xmin/xmax/csn），**无 xip 活跃列表**——不需要
  圈住全部未提交事务，水位线机制才可行（与 recovery-conflict.md 备库快照
  三要素同源）。

**判读铁律**：AWR 长事务表（ltx60）出现数小时 idle in transaction 而
OldestXmin 列仍在推进=**正常机制，不是渲染 bug**；反过来，带 XID/持有效
快照的长事务仍会钉（10-26 envC 案例 5.8 天长事务钉 OldestXmin 即此）。
钉与不钉的分界=是否持有效快照版本/已分配 XID，不是事务年龄。
实战样本：迪墨云 20261104（3h idle in txn + OldestXmin 每采样推进 10 万+）。

## AWR 判读应用

| 现象 | 机制解释 |
|---|---|
| 长事务卡 vacuum | 长事务的 snapshotcsn 老 → 其后所有提交的删除对该快照"不可见" → dead 元组不能回收（OldestXmin 不前移）——膨胀形态的机制根源 |
| 只读负载有写 IO | hint bits 写回（判定链第 3 步） |
| 备库可见性延迟 | 回放到某 CSN，备库快照视界受回放进度限制（联动 wait transaction sync 场景） |
| 40001 concurrent update | 对方提交且本快照看不到其新版本——快照语义与行锁协议的交界（core-events §4 第 4 步） |
| 序列化失败偶发 | 快照边界严格小于语义的边界效应 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| 死元组多=autovacuum 没跑 | 先查 OldestXmin 被谁的快照压住（长事务/复制槽） |
| CSN 当时间戳 | 是提交序号（逻辑时钟），只比大小不换算时间 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）；GTM/DMS/分布式分支〔排除〕
- 源码精读（2026-10-25）：heapam_visibility.cpp:1026-1250（判定链全程）、
  common/backend/utils/time/snapmgr.cpp（XidVisibleInSnapshot：CSN 严格小于比较）、
  文件头注释（IsInProgress/CLOG 检查顺序的竞态论证）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
