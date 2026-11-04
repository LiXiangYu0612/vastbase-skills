# 锁与轻锁机制（LWLock / 重量级锁队列 / 死锁检测）

## 概述

> 服务分类：performance（244 个 LWLOCK 事件与 17 个 LOCK 事件的共性判读、
> 死锁与排队分析）

个体事件的锚点见 `core-events-deep-dive.md`；本文件讲三类锁机制的**共性底座**。

## LWLock 机制（lwlock.cpp）

1. **两段获取**：`LWLockAttemptLock`（spinlock + CAS 快路径）失败 → 挂入该锁的
   PGPROC 等待队列 → PGSemaphore 睡眠。LWLockAcquire 入口（:1295）
2. **释放不交接、唤醒后重试**（LWLockAcquire 注释，判读级知识）：LWLockRelease
   不把锁直接授给等待者，只唤醒；等待者醒来**重试 CAS**，失败再睡。设计理由
   （源码注释原文语义）：竞争下等待者可在单个 CPU 时间片内反复重取，省一次
   进程切换；代价是被唤醒者可能空转一个调度周期。**判读**：LWLock 等待的
   avg 里含"唤醒→重试失败→再睡"的调度空转，不全是持锁时长
3. **持锁数上限**：`MAX_SIMUL_LWLOCKS`，超出报
   `too many LWLocks taken`（ERRCODE_LOCK_NOT_AVAILABLE）——路径异常时才可能见到
4. **临界区禁中断**：持 LWLock 期间 `HOLD_INTERRUPTS`——cancel/terminate 信号
   不打断临界区。**判读**：会话"卡在轻锁里 cancel 不掉"是设计行为，
   等临界区退出才处理信号
5. **tranche/分区**：高竞争锁按分区拆（BufMappingLock 按 buffer 哈希分区，
   见 `buffer-manager.md`）——分区锁整体排队=竞争已越过分区粒度

## 重量级锁队列（lock.cpp）

- 每个锁对象一条 `waitProcs`（PROC_QUEUE）等待队列：FIFO，弱锁（共享）请求
  相邻成组；强锁插入组边界——队头语义即"下一个获得者"
- `lockwait_timeout` 预算在 LockAcquire 层施加（lock.cpp:4060，
  见 `core-events-deep-dive.md` §4 行锁协议）

## 死锁检测（deadlock.cpp:191 DeadLockCheck）

1. **触发与上下文**：deadlock_timeout 到期（SIGALRM）→ DeadLockCheck 在信号
   处理上下文运行；死锁细节刻意延后到 DeadLockReport 打印（不持 LWLock 做
   输出——源码注释原意）
2. **软死库先重排队（判读级知识）**：FindLockCycle 家族（FindLockCycleRecurse/
   RecurseMember，deadlock.cpp:68-71）**递归走等待图**，硬边（必然等待）成环即死锁；
   软边（仅因**等待队列顺序**才会成环）收集进 softEdges 数组——软环的解法是**重排
   waitProcs 队列**（waitOrders 数组重插），不报死锁；只有无重排可解才
   DS_HARD_DEADLOCK → DeadLockReport → 40P01。**判读**：logrpt 里看到的死锁对
   都是硬死锁；"偶发自愈"的等锁异常多为软环被自动重排消化
3. **autovacuum 特例**：`blocking_autovacuum_proc_num`——被 autovacuum 阻塞的
   会话可触发 vacuum 主动让路（联动取消家族的
   "canceling autovacuum task"，postgres.cpp 统一取消处理器）
4. 死锁对报告（DeadLockReport）给出两个会话各自的持锁与等待 SQL——
   logrpt 判读直接用

## 数据通路上的 LWLock → 结构映射（与体系结构的挂接）

每把高频轻锁保护哪个共享结构、在 `kernel-data-path.md` 的哪个节点、竞争时什么形态：

| 轻锁 | 保护结构 | 源码锚 | 通路位置 | 竞争时的 AWR 形态 |
|---|---|---|---|---|
| BufMappingLock（**4096 分区**，lwlock.h:62） | 缓冲哈希表项（换 tag） | bufmgr.cpp:3212 双分区锁序 | 读路径 miss 换 tag | 高淘汰率+热点块时排队；分区化后整体排队=竞争越过分区粒度 |
| buffer content_lock（每缓冲一把） | 页内容与头部 | bufmgr.cpp:3128 条件共享；heapam 各处 LockBuffer | 读写页全程（含脏受害者自刷） | BufferContent 类事件=热点块；配对象榜定位热表 |
| WALInsertLock（**组插入**，xloginsert_locks=8） | WAL 插入槽 + 组长链 | xlog.cpp:660-760 组插入协议全文；NUMA 倍数启动校验 :6686 | 写路径 WAL 插入 | **见下方深读注**：跟随者不拿锁，竞争大部分不进等待事件账本 |
| CLog control_lock（**每页**） | 事务状态 SLRU 页 | clog.cpp:286,465,694 | 提交写状态/可见性查询 | CLogControlLock 族=热点状态页（大 xid 段集中提交） |
| ProcArrayLock | 进程数组（快照一致性） | procarray.cpp:353 独占（注册/退出/清理）；快照=共享获取（见下深读注） | 每条查询取快照 | 高连接+高频短查询时快照竞争；清理路径独占会全局排队 |
| XidGenLock | xid 计数器 | varsup.cpp:143 GetNewTransactionId | 每个写事务开始 | 罕见；极端写并发才可见 |
| LockHashPartitionLock | 重量级锁表哈希分区 | lock.cpp:472,874 | 每次重量级锁操作 | 行锁高频场景锁表分区竞争（与 transactionid 联判） |
| DoubleWriteLock | 双写文件上下文 | core-events §5（深读） | 刷脏 | 见该文件 |

**openGauss 高并发三板斧**（判读背景）：锁分区化（BufMapping 4096 区、锁表哈希分区）、
WAL 组插入（组长一次插整组，摊销插入成本）、SLRU 页粒度锁——LWLock 事件真正排队的
前提是竞争已越过分区/分组/页粒度，**先想"竞争为什么越过粒度"再谈调优**。

**WAL 组插入深读注（判读盲区级，xlog.cpp:660-760）**：插入者把自身 rdata/时间线等状态
写进自己 PGPROC，CAS 挂上 xlogGroupFirst 链即返回等待——**链非空=跟随者，全程不取
WALInsertLock**，单机构建下靠 `sched_yield` 自旋（每 N 圈 usleep 一次）等组长清
xlogGroupMember 标志；只有摘到空链头的成为**组长**，进临界区先插自己、再整链插入
全部跟随者的 WAL。组号按 NUMA 节点分配（pgprocno/numaNodeNum）。
**判读推论：WAL 插入竞争的代价大头不进任何等待事件账本**——跟随者自旋只表现为
sys CPU 与上下文切换上升。写洪峰的完整证据链 = WAL 速率 + sys CPU/ctxt + WALWrite
IO 延迟，单看 WALInsertLock 事件会漏判。另注 ARM 弱内存序屏障注释（鲲鹏环境相关）。

**ProcArray 快照获取深读注（procarray.cpp:2018-2400，2026-10-25 补）**：

- **共享锁够用**：取快照只需 ProcArrayLock **LW_SHARED**，即使要写自己的
  MyPgXact->xmin（源码注释明言）——独占留给注册/退出/xid 清理路径
- **CSN 快照**：snapshotcsn = nextCommitSeqNo 原子读——可见性判定主体是 CSN
  比较；循环扫出的 xmin/xmax/globalxmin 主要供 vacuum 边界用
  （GetOldestXmin :1588）
- **主循环**：扫 pgprocnos 跳过 PROC_IN_LOGICAL_DECODING；**globalxmin 忽略
  PROC_IN_VACUUM（lazy vacuum 不推进全局 xmin）**；无 xid 线程回退取 CN 下发
  的 next_xid；自己的 xid 不进快照但计 xmin
- **备库快照等回放**：takenDuringRecovery 时上报 STATE_STANDBY_GET_SNAPSHOT
  （**又一个"STATUS 类但真等待"**，已补进 commit-sync-events 清单）；等回放
  推进过清理点（standbyRedoCleanupXminLsn ≤ redoEndLsn）才放行——**回放慢
  在备库表现为取快照慢**；重试 usleep 100µs，每 0x7FFFF 次打一条 LOG
- **组获取**：群竞争下 GroupGetSnapshot——等待者注册 snapshotGroup，由一人
  代取全组结果回填（xminGroup 等五个字段）
- **分布式形态**：CN/DN/GTM_LITE 分派变体（GetPGXCSnapshotData 全局快照 vs
  GetLocalSnapshotData 本地多版本），单机环境走本地路径

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| LWLock avg 全当持锁时长 | 含唤醒-重试-再睡的调度空转 | avg 只作方向 |
| 死锁必报 40P01 | 软环被队列重排自动消化 | logrpt 死锁=硬环 |
| 卡轻锁的会话反复 cancel | 临界区 HOLD_INTERRUPTS | 等临界区退出；查持锁者在干什么 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；行号会漂，函数名为锚

## 来源

- 源码精读（2026-10-25）：lwlock.cpp:1295-1365（LWLockAcquire 全程含设计注释）、
  deadlock.cpp:191-252（DeadLockCheck 主循环、waitOrders 重排、autovacuum 特例）；
  二次补读：procarray.cpp:2018-2400（GetSnapshotData 全函数）、:1588（GetOldestXmin）
- 联动：`core-events-deep-dive.md`（个体事件/行锁协议）、
  `error-fatal-mechanics.md` §6（40P01）、`buffer-manager.md`（分区锁）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
