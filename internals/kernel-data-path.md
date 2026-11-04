# 内核数据通路总图（知识网枢纽）

## 概述

> 服务分类：全部（本文件是 internals/ 各机制文件的枢纽，也是判读 AWR 现象时的
> "定位地图"——任何现象先在这里找到它在通路上的位置，再沿链接下钻）

一条语句在内核里走的完整通路：**读路径**（扫描→缓冲→物理读→算子）与
**写路径**（行锁→WAL→脏页→双写→刷脏→存储）。通路上每个节点挂四样东西：
等待事件、参数、AWR 章节现象、关联报错——这就是各知识文件之间的"边"。

源码基线 openGauss 6.0.0（G100 V3 Build 8）；〔排除〕段页式/nvm/cstore/ustore 路径不含。

## 读路径（SELECT / 扫描）

```text
语句 → 计划算子（每节点内存配额 operatorMemKB）
        │  超配额 → 落盘排序/哈希 ──────────────→ 事件: BufFileWrite/BufFileRead
        │                                           （buffile.cpp:518/453，真实 temp IO）
        ↓
表扫描 → 读缓冲 ReadBuffer_common
        │  查缓冲哈希表 BufferAlloc:3006 ───────→ 事件: BufHashTableSearch（计数型=访问量镜像）
        │  命中 → 逻辑读 +1
        │  未命中 → smgrread
        ↓
物理读 md.cpp:1270 mdread / :1378 mdreadbatch ─→ 事件: DataFileRead（真实 IO，按介质判）
        ↓
算子执行（Sort/HashAgg 阶段标记）──────────────→ 事件: STATUS 类（工作量证据非等待）
        ↓
返回行 rows returned(顺扫)/fetched(索引) ──────→ AWR: Load Profile
```

## 写路径（UPDATE / INSERT / DELETE）

```text
语句 → 行锁检查 heap_update (heapam.cpp:4653)
        │  同行并发 → XactLockTableWait ────────→ 事件: transactionid（avg=对方事务剩余时长）
        │  等锁超时 lockwait_timeout ───────────→ 报错: 57014 canceling (lock wait timeout)
        │  互等成环 → DeadLockReport ───────────→ 报错: 40P01 deadlock detected
        ↓
WAL: XLogInsert → XLogWrite (xlog.cpp:2797) ───→ 事件: WALWrite
        │  commit → XLogFlush → fsync ─────────→ 事件: LogFileSync/XLogFlush
        │  （synchronous_commit=on 时 commit 等这里）
        ↓
缓冲标脏（shared_buffers）→ pagewriter 批量刷脏
        │  pagewriter.cpp:2160 → dw_perform_batch_flush
        │  → dw_batch_flush (double_write.cpp:2366)
        │  持 flush_lock（batch:1012 / single:1823,1864）─→ 事件: DoubleWriteLock
        │  刷脏追不上写入 → behind xlog_insert 涨 ─→ AWR: Pagewriter 章节
        ↓
存储 w_await（介质分档阈值）────────────────→ AWR: Disk per-minute
        盘满 ─────────────────────────────────→ 报错: 53000/53100
```

## 横切层：内存记账（两条路径共用）

任何内存分配都过进程记账层（max_process_memory）：超限 → **报错 53200
memory is temporarily unavailable**；算子层配额管的是"要不要落盘"，
记账层管的是"还能不能分配"——两层独立，见 `operator-memory.md` 第 7 条。
全局水位（gs_sysmemory_busy）紧张时算子会提前落盘（operator-memory 第 5 条）。

## 节点挂载表（知识网的边）

| 通路节点 | 源码锚 | 等待事件 | 关键参数 | AWR 现象/章节 | 关联报错 | 下钻文件 |
|---|---|---|---|---|---|---|
| 算子内存配额 | executor.h:114 SET_NODEMEM；memctl.cpp | —（ spills 见下） | work_mem、operatorMaxMem | Max Session Memory、Temp Spill 榜 | 53200 | operator-memory.md |
| 缓冲哈希查找 | bufmgr.cpp:3006 | BufHashTableSearch（计数型） | shared_buffers | Top Event 榜首常见、logical reads 镜像 | — | core-events §1 |
| 物理读 | md.cpp:1270,1378 | DataFileRead | random_page_cost、effective_cache_size、（OS）read_ahead_kb | physical reads、Cache Hit、Disk 表 Rot/await | — | core-events §2 |
| 临时文件 | buffile.cpp:453,518；tuplesort 状态机 | BufFileWrite/BufFileRead（真实 IO）；STATUS: Sort - write file（落盘批）/ Sort - fetch tuple（归并读回） | work_mem、temp_tablespaces | temp bytes、fd pgsql_tmp | 53100（temp 满） | core-events §3 + operator-memory（含排序生命周期深读） |
| 行锁 | heapam.cpp:4653；lmgr.cpp:641/锁等待；lock.cpp:4060 超时 | transactionid（队首）；元组锁队列（队尾） | lockwait_timeout、deadlock_timeout | waitchain、长事务榜 | 57014、40P01、40001 | core-events §4（完整协议） + error-fatal §3,6,7 |
| 轻锁/死锁底座 | lwlock.cpp:1295；deadlock.cpp:191 | 244 LWLOCK 族共性；死锁=软环重排+硬环报错；**各通路节点挂哪把轻锁见 locks.md 映射表**（BufMapping/content/WALInsert/CLog/ProcArray/XidGen/LockHashPartition） | deadlock_timeout | Top Event LWLOCK 类、logrpt 死锁对 | 40P01（仅硬环） | locks.md |
| WAL 写/刷 | xlog.cpp:2797 | WALWrite、LogFileSync、XLogFlush | wal_buffers、synchronous_commit、commit_delay、checkpoint 族 | Load Profile WAL 速率、write time | 58000（日志写失败） | error-fatal §10 |
| 同步复制确认 | syncrep.cpp:311（SyncRepWaitForLSN）；Paxos 变体 :1722 | **wait wal sync**（STATUS 通道但真等待） | synchronous_standby_names、most_available_syncmode、sync_config_hostname | ASP 桶 wait_wal_sync + Replication lag 分解（Net/Fsync/Apply） | — | commit-sync-events.md |
| 等指定事务完成 | procarray.cpp:4542 SyncLocalXidWait | **wait transaction sync**（ASP 串带 xid） | transaction_sync_naptime、transaction_sync_timeout | ASP 桶 wait_trans_sync（默认环境罕见=异常） | — | commit-sync-events.md |
| 双写 | double_write.cpp:2366（批量，pagewriter.cpp:2160 驱动）；单页 1823/1864 | DoubleWriteLock（锁内含整批 pwrite=DW_WRITE）；锁前 XLogWaitFlush | enable_double_write（默认开，**勿动**） | Pagewriter behind/dirty | — | core-events §5（深读版） |
| 刷脏调度 | pagewriter.cpp:2160 | — | checkpoint_timeout/segments、max_redo_log_size | Pagewriter 章节 | — | parameters.md 组3 |
| 语句取消 | postgres.cpp 取消路径 | — | statement_timeout、lockwait_timeout | 超时 SQL 在九维榜的位置 | 57014 | error-fatal §7 |
| 认证/连接 | auth.cpp:289、procarray.cpp:363 | — | max_connections、pg_hba | DB Process TCP/Threads | 28P01、53300、57P01 | error-fatal §1,4,5 |
| 死锁检测 | deadlock.cpp DeadLockReport | — | deadlock_timeout | logrpt 死锁对 | 40P01 | error-fatal §6 |

## 用法（判读定位法）

拿到任何 AWR 现象/事件/报错，先在上表找行 → 沿"下钻文件"列进对应机制文件拿
异常判据 → 沿"参数"列进 `../performance/parameters.md` 拿处置 → 沿"AWR 章节"列
回报告取证。**跨行组合即诊断**：如"transactionid 高 + 长事务榜有人" = 行锁排队
（第 4 条钥匙）；"BufFile 高 + w_await 正常 + work_mem 不小" = 查全局水位/倾斜
（operator-memory §5/§关键结论 5）。

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；锚点行号会漂，函数名为锚
- pagewriter 驱动批量双写（pagewriter.cpp:2160）为 V3 形态；checkpointer 参与
  checkpoint 批次，单页 flush 走 single_cxt 路径

## 来源

- 源码核对（2026-10-25）：pagewriter.cpp:2160（双写调用方）、xlog.cpp:2797（WALWrite）、
  bufmgr.cpp:3006、md.cpp:1270/1378（mdreadbatch）、heapam.cpp:4653、
  double_write.cpp:1012/1823/1864（flush_lock 持有）、buffile.cpp:453/518
- 其余锚点继承自：operator-memory.md、core-events-deep-dive.md、error-fatal-mechanics.md

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
