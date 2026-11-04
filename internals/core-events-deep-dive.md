# 核心等待事件源码判读

## 概述

> 服务分类：performance（事件定性、异常判据）；工具类问题可对照 `../tools/`（规划中）

AWR 里最高频的六个事件，逐个给出：内核触发路径（file:line）→ 机制 → **正常形态 →
异常判据（数字）**，并标注计数型与真实等待的区分。判读任何报告的 Top Event 时先查本表。

基线 openGauss 6.0.0（VastBase G100 V3.0 Build 8）。**排除路径**：cstore、ustore、
段页式（storage/smgr/segment/）、nvm（storage/nvm/）、线程池——VastBase 默认不启用、
不稳定特性，精读与结论一律不含这些路径；下文凡标注〔排除〕的同名触发点仅作备忘。

事件名总表源码：`cbb/instruments/ash/wait_event_info.cpp`（se 知识库同源）——
遇到未见过的事件，先 rg 这个文件定位类别。

## 1. BufHashTableSearch —— 计数型（测量括号），永不构成瓶颈

- **触发路径**：`storage/buffer/bufmgr.cpp:3006` `BufferAlloc()` 内
  `pgstat_report_waitevent(WAIT_EVENT_BUF_HASH_SEARCH)` 包住 `BufTableLookup()`
  （缓冲区哈希表查找），下一行立即 `WAIT_EVENT_END`〔排除：nvm/nvmbuffer.cpp:289 同名〕
- **机制**：纯测量括号。waits ≈ 逻辑块访问次数；Total = 查找动作自身耗时（µs 级）。
  姊妹括号：**BufStrategyGet**（bufmgr.cpp:3077，受害者选择耗时）——同为计数型，
  但语义是**淘汰压力**而非访问量；两者判读见 `buffer-manager.md`
- **正常形态**：任意次数都正常；与 Load Profile logical reads 镜像对账
  （实测样本：事件 71.9 亿次 vs logical reads 71.6 亿次，偏差 0.4%）
- **异常判据**：**不存在等待异常**。占比再高（样本 25.98%）也只是访问量镜像，
  处置指向减少块访问/提高命中率，不是调锁。小分母窗口会放大其占比，先查分母

## 2. DataFileRead —— 真实物理读 IO

- **触发路径**：`storage/smgr/md.cpp:1270` `mdread()`：_mdfd_getseg 定位段 →
  **压缩主分支 CfsReadPage〔排除路径，默认不启用〕** → `FilePRead(...,
  WAIT_EVENT_DATA_FILE_READ)`；`md.cpp:1378` `mdreadbatch()` 批量读
  〔排除：storage/smgr/segment/data_file.cpp:663,682 段页式；verifyrepair.cpp 修复工具〕
- **机制**：真实磁盘读等待。含 OS 页缓存命中（所以 avg≈0 不代表没读，代表已在缓存）
- **正常形态**：SSD avg<2ms；物理读/逻辑读 <1%（热数据全缓存时）
- **异常判据**：**先看盘体 Rot 列定介质**，再按阈值：SSD avg>10ms 持续 = 异常；
  HDD>40ms = 异常（本行为持续**均值线**；每分钟序列**尖峰线** SSD>5/HDD>20 见
  commit-sync-events 与 check-inventory 表 D——口径不同、并行使用非矛盾）。速率持续高 + 命中率同步低 + 物理读 SQL 榜有主 + iowait/b 高
  ——四点齐才立读瓶颈（单一信号不立论）

## 3. BufFileWrite / BufFileRead —— 临时文件真实 IO（spill 的镜像）

- **触发路径**：`storage/file/buffile.cpp:518` `BufFileWrite()` →
  `FilePWrite(..., WAIT_EVENT_BUFFILE_WRITE)`；`:453` `BufFileRead()` →
  `FilePRead(..., WAIT_EVENT_BUFFILE_READ)`。**真实 IO 等待，不是计数型**（与 1 不同类）
- **机制**：临时文件读写（排序逻辑磁带、哈希 join 批次、物化节点）。写先进 OS
  页缓存故 avg≈0；调用方见 `internals/operator-memory.md`（spill 链路）
- **正常形态**：次数与 temp bytes、Sort/Hash 落盘量成比例
- **异常判据**：avg 升高 = temp 所在盘慢；量激增 = spill 负载变大，对照
  operator-memory 的三层原因（配额不足 / 倾斜 / 全局水位提前触发）
- **归类提示**：事件名表里 BufFileWrite 挂 "Pg_control"、BufFileRead 挂 "Temp File"
  （历史遗留分类），按本文件机制理解，不要被类别名带偏

## 4. transactionid —— 行锁等待（完整协议，深读版）

- **触发路径**：`storage/access/heap/heapam.cpp:4653`（heap_update）/:5332（heap_lock_tuple）；
  等待本体 `storage/lmgr/lmgr.cpp` XactLockTableWait()；超时施加 `storage/lmgr/lock.cpp:4060`
- **完整协议**（读代码顺序即执行顺序）：
  1. **锁对象本体**：事务获得 xid 时即对自己 xid 持重量级排他锁
     （XactLockTableInsert，`lmgr.cpp:641`），commit/abort 时随进程锁清理释放——
     "行锁等待"等的就是这把
  2. **元组锁=排队优先级**：更新者发现 xmax 是别人后，先取该行 TID 的重量级元组锁
     （heapam.h 宏 `LOCK_TUPLE_TUP_LOCK` → LockTuple）——拿到=排到该行队首，
     后续等待者排在这把锁的队列上；被迫"start over"重查时**元组锁不释放**，
     保持队首（heapam.cpp 注释原文语义）
  3. **队首才等 transactionid**：`XactLockTableWait(xwait)` 对 TRANS 锁标签取
     ShareLock，阻塞在持有者 xid 锁之后
  4. **醒来必重查**：xmax 变了（别人抢先）→ goto 重走；对方提交且 ctid 已变 →
     序列化失败 `40001 abort transaction due to concurrent update`（可重试错误，
     与超时 57014 语义不同）
  5. **子事务链上爬**：XactLockTableWait 循环里 `TransactionIdIsInProgress` +
     `SubTransGetParent`——**子事务提交不解锁，等到顶层事务结束**。判读含义：
     avg 包含的是对方顶层事务的剩余时长
  6. **超时在锁管理器层**：`lock.cpp:4060` `needWaitMilliSec = waitSec 或
     lockwait_timeout(GUC)`——元组锁排队与 xid 等待两段共用这个预算，到期报
     57014 canceling due to lock wait timeout
  7. 共享锁者在场走 MultiXactIdWait（multixact 路径，SELECT FOR SHARE 与更新冲突时）
- **判读（精确化）**：avg transactionid = 队首等待者从开始等到**对方顶层事务结束**的
  时长——不是"锁慢"；同一行多等待者时，**队首等 transactionid、队尾等在元组锁上**，
  waitchain root 之后的链就是元组锁队列。样本：414 次 avg 659.65ms = t_flow_rwzxz
  同键并发更新
- **正常形态**：OLTP 毫秒级、零星
- **异常判据**：avg>100ms 且成规模 = 行锁排队；持锁方查长事务榜（idle in
  transaction 或大事务）
- **处置方向**：业务消除同键并发在前；lockwait_timeout 只是止血（且它在两段等待
  上都生效，调小会更容易 57014，调大拖长队列——先改业务）

## 5. DoubleWriteLock —— 双写队列轻锁（深读版）

- **触发路径**：批量 `dw_batch_flush()`（double_write.cpp:2366 起，由 pagewriter 驱动：
  pagewriter.cpp:2160 → dw_perform_batch_flush）；单页 `single_cxt->flush_lock`
  （:1823/1864，前台脏受害者自刷走此路，链路见 `buffer-manager.md`）；
  锁名注册：`{"DoubleWrite","LWLOCK_EVENT","DoubleWriteLock"}`
- **深读结论（dw_batch_flush 函数体）**：
  1. **锁内包着整批 pwrite**：拿 `flush_lock` 排他锁 → 合并各线程批次缓冲 →
     文件环形回收 → 组装 → **锁内 pwrite 整批页到 dw 文件**（此处报
     `DW_WRITE` 事件）→ 记账 → 放锁。**持锁时长 ≈ dw 文件整批写延迟**——
     DoubleWriteLock 的 avg 直接跟随 dw 文件所在盘的速度，锁本身没有"慢"可言
  2. **拿锁前先 `XLogWaitFlush(latest_lsn)`**（WAL-before-data 规则）：最老脏页
     LSN 未落盘则先等 WAL——**WAL 刷盘慢会拖慢双写批次**，连锁表现为
     DoubleWriteLock 排队 + Pagewriter behind 涨（跨域边：WAL→双写→刷脏）
  3. 备份/快照特性会冻结 dw IO（snapshotBlockLock 排他 + 等全部批次完成，
     dw_blocked_for_snapshot）——备份窗口内刷脏停摆是设计行为
  4. 关联事件：`DW_WRITE`（整批写 IO 本体）、`DW_READ`、`DW_SINGLE_POS`
     （枚举见 pgstat.h；单页路径语义未深读，遇到再补）
- **正常形态**：低频、avg 个位数 ms。样本：2803 次 avg 6.82ms、0.07% —— 正常
- **异常判据**：avg 持续抬升 = dw 文件所在盘慢（查 Disk 表该盘 await，而非调锁）；
  同步查 WAL 刷盘是否是上游拖累（XLogWaitFlush 链）；前台出现 = 脏受害者自刷
  （buffer-manager.md 链路），本质是 pagewriter 供给不足
- **辨析（深读修正）**：后台批次刷脏（pagewriter.cpp:2160 驱动）是常态；
  **前台出现的完整因果链已在 `buffer-manager.md` 实锤**——BufferAlloc 淘汰到脏页
  且 pagewriter 供给不足时，backend 自刷走单页双写（bufmgr.cpp:3169
  first_version_dw_single_flush，持 single_cxt->flush_lock）。前台出现 = 刷脏跟不上
  淘汰，查 Pagewriter behind/候选链，不是锁本身的问题

## 6. STATUS 类（Sort / HashAgg - build hash / flush data / analyze 等）

- **机制**：执行阶段标记（WaitState 枚举经 pgstat_report_waitstatus 上报——
  两套等待通道的架构事实见 `commit-sync-events.md`），**绝大多数**不是等待、
  不计入瓶颈占比；**例外**：wait wal sync / wait transaction sync /
  wait data sync 三个是真实等待（同步复制/等指定事务），详见该文件
- **正常形态**：与算子工作量成比例
- **异常判据**：只有"工作量异常"没有"等待异常"——
  `Sort - write file` 多 → spill 负载（对照 3 与 operator-memory）；
  `analyze` 出现且总耗时长 → autovacuum analyze 在跑大表（样本：453 次 total
  295.7s ≈ 0.65s/次，后台统计采集，属正常但要意识到其在吃 IO/CPU）；
  `flush data` 多 → 结果集回写/物化大

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| BufHashTableSearch 当锁瓶颈 | 计数型事件是访问量镜像 | 与 logical reads 对账后只作访问量代理 |
| DataFileRead avg=0 当"没读" | 含 OS 缓存命中 | 看速率与命中率，不看 avg 单项 |
| transactionid avg 大当"锁慢" | 等的是对方事务结束 | avg=持锁方剩余时长，去 waitchain/长事务榜找人 |
| DoubleWriteLock 前台出现仍当后台噪声 | 前台单页双写被卡是少见形态 | 必查 Callstack 前台栈 |
| STATUS 计入等待占比 | 执行阶段标记 | 只作工作量证据 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；行号会漂，以函数名为锚
- 现场实例数字（7.19B/414 次/659.65ms/2803 次/6.82ms）取自 2026-10-21 真实报告，
  作为判读示范锚点

## 来源

- 源码精读（2026-10-25）：bufmgr.cpp:3006、md.cpp:1270,1378、buffile.cpp:453,518、
  heapam.cpp:4653,5332,6683、double_write.cpp:2321,2366、
  wait_event_info.cpp（事件名表）
- 实测样本：2026-10-21 现场 AWR（五窗；样本未随仓发布）
- 机制联动：`operator-memory.md`（spill 三层原因）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
