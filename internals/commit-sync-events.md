# 提交同步事件（wait wal sync / wait transaction sync）

## 概述

> 服务分类：performance（ASP 桶 wait_wal_sync/wait_trans_sync 判读、commit 延迟诊断）；
> 判读必须联动 AWR Replication 段的 lag 分解（Net/Fsync/Apply）

这两个 ASP 桶计数器（vb_tool 采集 `event='wait wal sync'` /
`event='wait transaction sync'` 的逐秒采样计数）是 commit 延迟类问题的主线索，
且**名字与直觉相反**：wait wal sync 不是本地刷盘，wait transaction sync 不是等提交。

## 前置架构事实：内核有两套等待上报通道

1. **pgstat_report_waitevent**（细粒度）：IO_EVENT / LWLOCK_EVENT / LOCK_EVENT 家族
   （WALWrite、DataFileRead、BufFileWrite…）
2. **pgstat_report_waitstatus**（WaitState 粗粒度状态，`include/pgstat.h:1159` WaitState
   枚举）：显示名由 `wait_event_info.cpp` 的 257 项表按枚举索引给出——AWR Top Event
   里的 **STATUS 类全部来自这一通道**（"Sort - fetch tuple"=STATE_EXEC_SORT_FETCH_TUPLE、
   "HashAgg - build hash"=STATE_EXEC_HASHAGG_BUILD_HASH、"analyze"=STATE_ANALYZE…）

由此修正一个旧结论的绝对化表述：**STATUS 类多数是执行阶段标记，但
wait wal sync / wait transaction sync / wait data sync 这几个是真实等待**——
它们恰好登记在 WaitState 枚举里、被显示为 STATUS 类而已。
（2026-10-25 补：**STATE_STANDBY_GET_SNAPSHOT** 备库取快照也是真等待——
等回放推进过清理点才放行，见 `locks.md` ProcArray 深读注。）

## 本地刷盘层（WALWrite 与群提交架构，深读）

- **XLogFlushCore**（xlog.cpp:3544）= 组装 WriteRqst → **XLogWrite 同步写** →
  唤醒 walsender → **flushResult 原子发布** → `WakeupWalSemaphore(walFlushWaitLock)`
  唤醒 commit agent。即本地 commit 刷盘不只在"等 IO"，还在等**发布与唤醒链**
- **群提交架构（V3 形态）**：walInsertStatusTable 按 LRC 序号记录每条插入的
  COPIED/NOT_COPIED 状态；后台 XLogBackgroundFlush 周期性沿表推进刷写到最新
  COPIED 项；commit agent 池在 walFlushWaitLock 信号量上等 flushResult 前进——
  高并发提交的摊销来自"多人插入、一次成批刷"
- **WALWrite 事件**（xlog.cpp:2797 / :3113）= XLogWrite 内 pwrite WAL 段文件的
  真实 IO——本地层唯一该看的 IO 事件
- **基线差异（重要）**：se 知识库中的 LogFileSync / XLogFlush 事件名
  **在开源 6.0.0 基线枚举中不存在**（该枚举只有 WAL_WRITE 与
  WAL_SYNC_METHOD_* 家族）——这两个名字来自 VastBase 定制分支。
  判读语义仍按 se 库（commit 等 fsync / 等 flush），源码锚点以 VastBase 分支为准，
  开源基线对不上不算矛盾
- **与复制层分界**：本地层信号 = WALWrite（IO 通道）+ fsync 族事件 + OS 每分钟
  IO 延迟；复制层 = SyncRepWaitForLSN（wait wal sync）。commit 慢先分层，
  **本地层排除必须三方对账，单看 WALWrite avg 不够（2026-11-04 用户点名修正）**：
  ① WALWrite avg/waits——只覆盖 XLogWrite 内 pwrite，**不含 fsync**；
  ② fsync 族事件（LogFileSync/XLogFlush，VastBase 定制名）——WALWrite 正常
     但 fsync 族高 = 落盘阶段慢，pwrite 快不等于 commit 快；
  ③ **AWR 的 Disk I/O per device 每分钟 await 序列**（先看 Rot 列定介质，SSD>5ms/
     HDD>20ms **尖峰线**；持续**均值线**为 SSD avg>10/HDD>40，见
     core-events-deep-dive——两线统计口径不同、并行使用非矛盾）+ iowait%——设备侧真相，库内事件账正常而 w_await 高 =
     设备/队列层问题（记账盲区）；反之 WALWrite 高而 w_await 正常 = 组提交摊销
     不足（高频小事务，commits/s 佐证）或发布唤醒链，不是盘慢。await 序列还要
     与 wait_wal_sync 桶尖峰**同分钟对齐**（对上了才谈因果，见 metrics.md 时间对齐）。
  三方都健康才把矛头指向备库链路（wait wal sync 计数 + lag 三段分解）——
  两层用药完全不同。lag 分解的 Fsync 段是**备库侧** flush 盘 IO，备库侧 await
  同样按介质判
- **跨域边**：双写批次拿锁前 XLogWaitFlush（见 core-events §5）——本地 WAL 慢
  会拖刷脏链路

### WAL 同步方法族（get_sync_bit 与 O_DIRECT 优化，xlog.cpp:244-257/14504，深读）

- GUC `sync_method` 枚举：fsync / fsync_writethrough / fdatasync / open_sync /
  open_datasync（平台条件编译，:244-257）
- **fsync 族 → 打开标志 0，写完后显式 fsync**（openLogFile 路径 :7130）；
  **open_sync/open_datasync → O_SYNC/O_DSYNC 打开，每次 write 自带落盘**
- **O_DIRECT 优化**（get_sync_bit :14504）：仅 open_* 族、且
  **!XLogIsNeeded()（无归档/流复制）且非 walreceiver** 时叠加——WAL 写绕
  内核页缓存；walreceiver 因非对齐写+立即重读被显式排除
- fill/patch 段文件路径**不用** get_sync_bit（:4061 注释：只在结尾 fsync 一次）；
  enableFsync=off 时 get_sync_bit 返回 0（fsync 全关的打开语义）
- **判读**：open_sync 的收益前提是无归档/流复制；有流复制的环境 WAL 段过
  内核 page cache 是设计行为，不是"没优化到"

## wait wal sync（STATE_WAIT_WALSYNC）= 同步复制等待

- **上报点**：`storage/replication/syncrep.cpp:311`（SyncRepWaitForLSN——commit 后
  等同步备库确认 LSN；ps 显示会追加 "waiting for X/X"；等待目标 =
  `WalSndCtl->lsn[mode]`，mode 由 synchronous_standby_names 决定 sync/flush/replay）
  ；`:1722` Paxos 一致性变体（DCF/Paxos 部署）
- **语义**：名字里的 "wal sync" 指 **WAL 在备库的同步确认**，不是本地刷盘。
  本地 commit 刷盘走 IO 通道事件（WALWrite，xlog.cpp:2797）——两层别找错
- **综合判读（配 AWR Replication 段 lag 分解，这是本事件的标准动作）**：
  wait_wal_sync 计数高 + lag 分解定位拖累环节——
  - **Net 大**（sent−write）= 网络/备端接收写慢 → 查网络（Retrans/带宽）与备库写
  - **Fsync 大**（write−flush）= 备库刷盘慢 → 备库存储/检查点参数
  - **Apply 大**（flush−replay）= 备库回放慢（单回放线程/CPU/大事务回放）→ 备库侧
  - lag 全小但等待仍高 = 突发提交洪峰瞬时排队，看 wait 计数的时间分布
- **参数链**（处置按序）：业务语义权衡 synchronous_commit（异步/本地/多数派）→
  most_available_syncmode（故障时降级可用性）→ sync_config_hostname → 备库侧
  IO/回放配置。同步复制被拖的主库 LogFileSync/XLogFlush 也会同步抬升——
  三角互证（trans 桶 + lag 分解 + 事件榜）
- 备库报告：Wal Receiver 段是 sender 视角镜像，同步复制结论要用主库侧数据

## wait transaction sync（STATE_WAIT_XACTSYNC）= 等指定事务完成

- **上报点**：`storage/ipc/procarray.cpp:4542` SyncLocalXidWait——循环
  `ConditionalXactLockTableWait(xid)` 等指定 xid 结束；超过
  **transaction_sync_naptime** 唤醒两阶段清理进程（vb_clean（源码基线名 gs_clean） 方向），
  **transaction_sync_timeout** 为总预算
- **ASP 显示带 xid**（`ash.cpp GetWaitstatusXid` 拼接）：事件串形如
  `wait transaction sync: 621697868`——**直接给出在等哪个事务**，
  拿 xid 去长事务/会话现场定位
- **调用方现状（重要）**：本基线仅两处调用——DSS 共享存储 adapter
  （ss_dms_callback.cpp:171）与 **ustore** 可见性（knl_uvisibility.cpp:1413），
  **均在排除路径**（默认不启用）。默认环境出现该事件 = 罕见形态：
  优先怀疑客户启用了 ustore 表或 DSS，按环境事实核实，不当常规等待消化

## 判读速查

| 现象组合 | 结论方向 |
|---|---|
| wait_wal_sync 高 + 本地三方对账健康（WALWrite 正常 + fsync 族正常 + OS w_await 正常）+ Net lag 大 | 网络/备端写 |
| wait_wal_sync 高 + 本地三方健康 + Fsync lag 大 | 备库刷盘（备库侧 IO，按介质判） |
| wait_wal_sync 高 + 本地三方健康 + Apply lag 大 | 备库回放 |
| wait_wal_sync 高 + lag 全小 | 提交洪峰瞬时排队（commits/s 佐证） |
| WALWrite 正常 + fsync 族/OS w_await 高 | 本地落盘慢（pwrite 快≠commit 快），先修本地再谈备库 |
| WALWrite 高 + OS w_await 正常 | 组提交摊销不足/发布唤醒链，不是盘慢 |
| wait_trans_sync 出现（默认环境） | 罕见：查 ASP 串里的 xid + 核实 ustore/DSS |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| wait wal sync 当本地刷盘慢去调 wal_buffers | 该事件在同步复制路径 | 配 lag 分解定位备库/网络环节 |
| 分层时只看 WALWrite avg 正常就排除本地盘、直跳备库链路 | WALWrite 只覆盖 pwrite，fsync 与组提交发布链不在其账内；库内事件正常≠盘正常（2026-11-04 用户点名） | 本地排除必须三方对账：WALWrite + fsync 族事件 + OS w_await（按介质分档）+ iowait |
| 认为 STATUS 类全不是等待 | wal/trans/data sync 三者是真实等待 | 按本文件区分 |
| wait_trans_sync 当常规等待消化 | 调用方在 ustore/DSS 路径 | 默认环境出现即深查环境 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；WaitState 枚举含 GTM/STREAM 等分布式
  状态，本文件只覆盖同步类三枚举
- 行号会漂，函数名为锚

## 来源

- 源码精读（2026-10-25）：syncrep.cpp:295-318/1700-1725、procarray.cpp:4534-4555、
  pgstat.h WaitState（1159-1240）、wait_event_info.cpp:30-60（表结构与索引语义）、
  ash.cpp:438-448（xid 拼接）；二次补读：xlog.cpp:244-257（sync_method 枚举）、
  :14504-14540（get_sync_bit 全函数含 O_DIRECT 条件）、:7130（openLogFile fsync）、
  :4029,4061,4189（段文件打开与 fill 路径例外注释）
- vb_tool 桶定义：vb_tool:7866-7894（`event='wait wal sync'` /
  `event='wait transaction sync'` 逐秒计数）
- 联动：`kernel-data-path.md`（写路径 WAL 分支）、`core-events-deep-dive.md` §6 修正

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
