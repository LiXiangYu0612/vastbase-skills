# 刷脏、behind 与检查点生命周期（pagewriter / 脏页队列 / CreateCheckPoint）

## 概述

> 服务分类：performance（Pagewriter 章节、checkpoint 抖动、写延迟类判读）

## 脏页队列架构（pagewriter.cpp）

- 脏页按**首次变脏顺序**进 `dirty_page_queue` 环形队列（头指针原子推进，
  :408/:611），入队时记 `rec_lsn` = 该页变脏时刻的 WAL LSN
- pagewriter 从队首成批取页 → 单页/批量双写（链路见 `core-events-deep-dive.md` §5）
  → 刷数据文件；增量检查点按队列推进 redo point（候选链三路见 `buffer-manager.md`）

## behind xlog_insert (MB) 的精确语义（两端实锤）

```
behind = current_xlog_insert_lsn − queue_head_page_rec_lsn
       （vb_tool:31367；内核侧 = ckpt_view_get_current_xlog_insert_lsn()
         − ckpt_get_min_rec_lsn()，pagewriter.cpp:158/:132）
```

即**最老未刷脏页的 rec_lsn 落后 WAL 插入点多远**——与复制 lag 同构的滞后量，
度量的是"写入吞吐领先刷脏吞吐多少 WAL 体积"。

## 判读规则

| 形态 | 含义 | 方向 |
|---|---|---|
| behind 涨 + remain_dirty 涨 | 刷脏吞吐跟不上写入（能力不足） | pagewriter 数量/IO 能力/批量写入洪峰 |
| behind 涨 + remain_dirty 稳 | 队首个别老页刷不动——**老页被 pin/反复跳过**（长查询持钉、candidate 未命中换人；推断，锚点级） | 查长查询 pin（buffer-manager 淘汰段同源） |
| behind 受 WAL 制约 | WAL-before-data：页刷盘前其 rec_lsn 之前的 WAL 必须已落盘——WAL 刷盘慢会卡刷脏（双写批次的 XLogWaitFlush 同边） | 查 WALWrite/本地盘 |
| 队列空显示 '-' | 无脏页，behind 无意义 | 正常 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| behind 当"脏页体积" | 是**滞后距离**（LSN 差），不是 dirty 数量 |
| behind 大只怪刷脏线程 | 先分三形态：能力/pin 住/WAL 制约 |

## checkpoint 生命周期（CreateCheckPoint，xlog.cpp:12149-12570，深读）

**增量 vs 全量的本质差异（V3 默认 enableIncrementalCheckpoint=on）**：

- **增量检查点**：redo point 不取当前插入点，而是**随脏页队列推进**——pagewriter
  刷掉队首脏页，redo 前移；定时触发（CHECKPOINT_CAUSE_TIME）只执行
  `update_dirty_page_queue_rec_lsn(curInsert)` 更新队列水位。**checkpoint 记录
  本身写得很轻，刷脏负担在平时被 pagewriter 摊销**——这就是增量模式的意义
- **强制全量**（满足特定条件时）：`full_ckpt_redo_ptr=curInsert` +
  `full_ckpt_expected_flush_loc=队列尾` + `flush_all_dirty_page=true`
  （日志 "will do full checkpoint, need flush N pages"）——此刻要求刷光脏页，
  表现为 checkpoint 尖峰
- **写 checkpoint 记录前的三件事**：
  1. `StopSuspendWalInsert` 拿 WAL 插入一致性位点
  2. **等正在提交临界区的事务**（delayChkpt 机制，10ms 轮询）——提交记录与
     clog 更新必须同在 redo 的一侧，否则 crash 恢复会漏放
  3. 取 nextXid/oldestXid/nextMulti（XidGenLock 共享）；oldestActiveXid 在
     定 redo 点**之前**取（Hot Standby 起始快照用）
- **增量 redo 的精确取法与三道闸**（:12310-12380，2026-10-25 二次深读）：
  curMinRecLSN = `ckpt_get_min_rec_lsn()`（脏页队列最老未刷页 rec_lsn）；
  队列空则取队列 rec lsn 兜底。三道闸：与上次 redo 相同或 ≤ remaincommit
  → **跳过本次**（LOG "Checkpoint meets prev checkpoint lsn…"）；**倒退
  → PANIC**（一致性防御，见 panics 就此链查）；`update_dirty_page_queue_rec_lsn`
  仅 time 触发与全量时执行
- **空闲跳过**（:12284）：非 shutdown/FORCE 且 curInsert 仍指上一条 checkpoint
  记录尾、redo 自指 → 直接返回——空闲系统不产生重复 checkpoint（源码注释：
  避免双记录同页崩溃丢失风险）
- **触发面**：checkpointer 线程 timeout（CHECKPOINT_CAUSE_TIME，
  checkpointer.cpp:459）、XLOG 量（:2885/:3164 CHECKPOINT_CAUSE_XLOG）、
  手动、shutdown（ControlFile 状态先置 DB_SHUTDOWNING 落盘）
- **尾部维护**：WAL 段回收以 redo 为界（KeepLogSeg :12607）——**长事务/
  复制槽压 redo 时 WAL 堆积的机制出口**；SyncPostCheckpoint（:12759）
- **CSNLOG 截断节流**：仅全量或 fullCheckPointTimeout 间隔到才截断
  （增量模式不频繁截断，减少锁竞争——源码注释原意）
- **CheckPointGuts 刷子系统顺序**：CLOG → CSNLOG → MultiXact → Predicate →
  RelationMap → ReplicationSlots → SnapBuild → **Buffers（全部 fsync）** →
  ReplicationOrigin → 2PC 最后

**判读闭环**：增量模式下 checkpoint 抖动的第一嫌疑是 **pagewriter 供给不足**
（behind/dirty 同涨，见上节三形态），而不是 checkpoint 参数本身；全量 checkpoint
尖峰先查触发原因（切换日志/手动/条件满足）。vb_tool AWR 的 Pagewriter Status
章节字段（remain_dirty/queue_head_lsn/behind_mb/restarted/ckpt_redo）正对这套机制。

## 常见错误（checkpoint）

| 错误 | 纠正 |
|---|---|
| checkpoint 慢先调 checkpoint_timeout | 增量模式下 redo 平时已被 pagewriter 推进，先查 behind 三形态 |
| 全量尖峰当异常 | 查触发因：条件强制全量会打日志，正常行为 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）
- 源码核对（2026-10-25）：pagewriter.cpp:128-160（min_rec_lsn/insert_lsn 可观测件）、
  :408,549,611（队列游标）；vb_tool:31356-31368（公式与语义注释）；
  xlog.cpp:12149-12570（CreateCheckPoint 核心：增量/全量分叉、delayChkpt 等待、
  CSNLOG 节流）、:12805-12875（CheckPointGuts 子系统顺序）；二次深读
  （同日）：xlog.cpp:12260-12380（oldestActiveXid 时序/空闲跳过/增量三道闸
  与倒退 PANIC/全量降级路径）、:2885,3164（CAUSE_XLOG 触发点）、:12607
  （KeepLogSeg redo 界）、:12759（SyncPostCheckpoint）、
  checkpointer.cpp:459（timeout 触发）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
