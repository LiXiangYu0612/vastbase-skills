# 等待事件与源码溯源

## 概述

等待事件是数据库在"等什么"的直接证词。本文件回答三类问题：这个事件是什么
（分类语义与计数型识别）、它在内核里怎么发生（se 知识库 → 源码定位）、
采样堆栈怎么读（Callstack 判读）。既服务 AWR 报告诊断（`awr-report-analysis.md`
第 3 步），也独立回答"某事件什么意思/为什么在等它"的提问。

## 事件分类语义

417 个事件按 Class 分四类（se 知识库口径）：

| Type | 数量 | 含义 | 处置 |
|---|---|---|---|
| `LWLOCK_EVENT` | 244 | 轻量锁竞争（buffer mapping、lock manager、CLog、double write 等） | 结合前台 LWLock 栈；看热点对象/并发 |
| `IO_EVENT` | 80 | 真实 IO 等待 + 少量计数型事件 | 先判断是否计数型（见下节） |
| `LOCK_EVENT` | 17 | 重量级锁（行锁 transactionid、表锁 relation 等） | 结合长事务榜 / ASP 阻塞链 / 日志死锁 |
| `STATUS` | 76 | **执行阶段标记**（HashAgg - build hash、Sort、flush data…） | 不是等待，不计入瓶颈占比；只作执行阶段证据 |

## 计数型事件（测量括号）识别

有些 `IO_EVENT` 是**测量括号**而不是阻塞：源码用
`pgstat_report_waitevent(EVENT)` 把一次同步操作包起来，
`WAIT_EVENT_END` 立刻结束。此时 AWR 的 Total Wait Time 等于该操作本身的耗时
（通常 1µs 级），waits 次数 ≈ 调用/块访问次数。

已确认样例：**BufHashTableSearch**（`bufmgr.cpp BufferAlloc():3006`）——
`pgstat_report_waitevent(WAIT_EVENT_BUF_HASH_SEARCH)` 包住 `BufTableLookup()`，
每次 BufferAlloc 记一次，waits 数 = 逻辑块访问次数。占比再高也不是"卡在锁上"，
而是块访问量的镜像；处置指向减少块访问/提高命中率，不是调锁。

识别方法：

```bash
rg -n "WAIT_EVENT_<NAME>" <源码根>
```

看该宏是否紧贴 `pgstat_report_waitevent` / `pgstat_report_waitevent_nested`，
且下一行就是普通函数调用。空载或小分母窗口会放大这类事件的"占比"，
先查分母再定性。

## 溯源工具链

六个高频核心事件（BufHashTableSearch/DataFileRead/BufFile 读写/transactionid/
DoubleWriteLock/STATUS）已有源码级判读与异常判据，见
`../internals/core-events-deep-dive.md`——先查它，不在表内再走下面的通用链。

| 资源 | 路径 | 说明 |
|---|---|---|
| 内核源码基线 | `<openGauss-6.0.0-src>/` | G100 V3 档基线 |
| se 知识库 | `vb_tool se <event>`（或 `~/vb_tool/vb_tool se`） | 完全离线，417 事件 |
| se 模糊列表 | `vb_tool se -l [关键词]` | 如 `se procarray` |

`se` 输出五项：`Type / Meaning / Triggers（源码 file:line）/ Why / Fix`。

## 溯源工作流（每个触发事件）

1. `vb_tool se <event>` 取类型、语义与源码触发点
2. `rg -n "函数名" <源码根>` 定位函数（**行号可能随版本漂移，以函数名为锚**）
3. 读触发点，回答三个问题：
   - 前台路径还是后台路径？（例：DoubleWrite/ControlFileSyncUpdate 是后台刷脏，
     不是 commit 路径）
   - 竞争的是什么资源？（锁/设备/内存/CPU 队列）
   - 哪些 GUC、计划形态、并发度会放大它？
4. 与时间轴、ASP 时段、日志前后窗切片、Top SQL 维度对齐
5. 产出事件判定卡：
   `事件 | 类型 | %DBtime | 次数 | avg | 源码触发点 | 机制 | 为何在此刻 | 受害 | 责任`

## 源码目录速查

| 事件族 | 源码位置 |
|---|---|
| 缓冲/哈希/淘汰 | `storage/buffer/bufmgr.cpp`、`nvm/nvmbuffer.cpp` |
| 数据文件读写 | `storage/smgr/md.cpp`、`storage/smgr/cfs/*` |
| WAL 写/刷 | `storage/access/transam/xlog.cpp`（XLogWrite / XLogFlushCore） |
| 双写 | `storage/access/transam/double_write.cpp`（dw_batch_flush） |
| 行锁/表锁 | `storage/access/heap/heapam.cpp`、`storage/lmgr/lmgr.cpp` |
| 轻量锁 | `storage/lmgr/lwlock.cpp` 及各模块 LWLockAssign |
| 临时文件/落盘 | `storage/file/buffile.cpp`、`runtime/executor/nodeHashjoin.cpp`、`vecexecutor/*` |
| 复制 | `replication/*`（walsender/walreceiver） |
| 后台线程入口 | `postmaster/*`（checkpointer/walwriter/autovacuum/pagewriter） |

范围约定：cstore（含 cfs）、ustore、段页式、nvm 路径默认不启用（VastBase 不稳定特性），
默认环境排查时跳过。

## 堆栈判读（Callstack Sampling）

### 数据形态

- **Background Threads**：`Sample Time | Thread | PID | State | Wait Event | CPU% | Stack`，
  约 1 采样/5 分钟/线程。常见线程：CheckPointer、Wal Writer、Wal Writer Auxiliary、
  WalSender、AutoVacuum（独立子表：Worker Cnt/CPU%/Stack）
- **Foreground Threads (LWLock waits)**：`Sample Time | Wait Event | PID | unique_sql_id |
  event_cnt | Stack`。只采到"正在等轻锁的前台会话"；整段为空 ≠ 无锁问题（可能没采到），
  表述写"全窗无前台轻锁等待采样"
- 栈串形如 `FuncA() + 0x1ec <- CallerB() + 0x4e4 <- ... <- start_thread <- clone`：
  **只有符号+偏移没有行号**，源码检索用函数名；偏移每次构建不同，归并指纹必须剥掉

### 指纹化（extract.py 已实现）

按 `<-` 切帧 → 去偏移/裸地址/参数类型 → 取前 5 帧做键
（线程 | 事件 | SQL Id | 前 5 帧）→ 输出指纹、样本数、首末时间、所属异常时段。
空闲基线折叠为一行，不把上百条原始栈灌进 digest。

### 后台线程空闲基线

| 线程 | 空闲栈特征 |
|---|---|
| CheckPointer | `WaitLatchOrSocket <- CheckpointerMain` |
| Wal Writer | `pg_usleep <- XLogBackgroundFlush <- WalWriterMain` 或 `WaitLatchOrSocket` |
| Wal Writer Auxiliary | `PGSemaphoreLock <- WalWriterAuxiliaryMain` |
| WalSender | `WaitLatchOrSocket <- WalSenderMain` |
| AutoVacuum | `Worker Cnt=0`，Stack=`-` |

判读规则：

- 栈顶落在空闲基线且样本覆盖全窗 → 该线程**排除**，不写"疑似卡住"
- 出现工作函数（XLogWrite / issue_xlog_fsync / comm_send / secure_raw_write /
  SyncRepReleaseWaiters / CheckPointGuts / FileSync / dw_batch_flush）→ 活动栈，
  结合时段与 CPU% 判读
- 同一线程长期停在某个非空闲函数 → 后台路径积压（WAL 刷盘、双写、复制发送）

### 三路对齐

```text
ASP/等待事件:  transactionid / BufMappingLock / DoubleWriteLock ...
        │
前台堆栈:      Wait Event + unique_sql_id + 前 5 帧
               → 谁、从哪条调用路径进来
se:            该事件的 Triggers（file:line）与语义
        │
源码:          读函数 → 锁保护的数据结构、持锁范围、竞争资源
        │
结论:          锁名 + 调用路径 + 持锁代码 + 时段/受害 SQL
```

三路一致（数字 + 堆栈 + 源码）= 高置信；只有数字无堆栈无源码 = 最多观察项。

示例：栈 `BufTableLookup <- BufferAlloc <- ReadBuffer_common` + 事件
`BufHashTableSearch` → 缓冲区映射表查找，先按计数型识别法核源码；
栈 `LockBufHdr <- ...` + 事件 `BufferContent`/`BufMappingLock` → 热点块竞争，
用对象/SQL 维度定位热表。

### 后台线程 → 源码入口

| 线程 | 主函数 | 源码 |
|---|---|---|
| CheckPointer | CheckpointerMain | `postmaster/checkpointer.cpp` |
| Wal Writer | WalWriterMain | `postmaster/walwriter.cpp`、`access/transam/xlog.cpp` |
| Wal Writer Auxiliary | WalWriterAuxiliaryMain | 同上 |
| WalSender | WalSenderMain | `replication/walsender.cpp` |
| AutoVacuum | AutoVacuumMain / worker | `postmaster/autovacuum.cpp` |
| PageWriter | PageWriterMain | 以实际 `rg` 结果为准 |

### 与时段对齐

每个异常时段统计：落在窗内的后台采样条数、线程分布、活动栈指纹。某线程的活动栈
只出现在异常时段 → 提升置信度；异常时段内后台采样为空（采样间隔大于时段）→
写"采样未覆盖"，不推断。

## waitchain 的适用范围（只对重量级 LOCK 有效，2026-10-28 定论）

- **数据源**：vb_tool `run_asp_waitchain_query` 从 gs_asp 取
  `block_sessionid IS NOT NULL` 的行构 root_blockers（递归到根阻塞者）。
  **gs_asp 的 block_sessionid 只在重量级锁（LOCK_EVENT：行锁/表锁/事务锁）等待时
  被填充**——这就是 waitchain 只对 LOCK 有效的机制根源。
- **LWLOCK 无阻塞关系**：轻量锁没有锁管理器级的持有者-等待者记录进 ASP，
  waitchain 查不到；要看 LWLock 持有者得用 VB 专有视图
  `pg_stat_get_backend_lwlocks()`（join sessionid 拿 SQL），或走 Callstack
  前台 LWLock 栈（AWR 现成）→ se/源码定位。
- **判读后果**：锁问题先分型——LOCK 走 waitchain（谁锁谁直给）；
  LWLOCK 走 wait_lwlock 桶峰值 + 前台 LWLock 堆栈 + 源码分析。

## gs_asp 锁三列的归属（2026-11-05 源码定论）

- **locktag / lockmode = 请求者（等待者）自己的**，不是持有者的。证据链：
  会话开始等重量级锁时 `WaitOnLock`（`lock.cpp:1703`）调
  `ReportWaitLockInfo`（`lock.cpp:1655`），把**本会话自己的**
  `locallock->tag`（LOCALLOCKTAG `{lock 对象, mode}`，`lock.h:471-474`，
  mode 即本 backend 申请的锁模式）memcpy 进自己的 BackendStatus
  （`lock.cpp:1689-1691`）；ASP 采样从每个会话自己的 beentry 取走
  （`ash.cpp:800-803`）。
- **block_sessionid 才是持有者**（`lock.cpp:1684-1688`，经 blockProcLock
  链取到持有 proc 的 sessionid）。一行等待记录的完整语义 = 这个会话在等
  哪把锁（locktag）、以什么模式申请（lockmode）、被谁挡（block_sessionid）。
- **持有者的持有模式不在等待行里**：持有者自己那行采样若没在等锁，
  locktag/lockmode 为 NULL；等别的锁时显示的是它自己的请求。要还原
  "持有者以什么模式持有"，须结合锁冲突矩阵从请求模式反推（如等待者
  RowExclusiveLock 被挡 ⇒ 持有者至少持 Share/更高不相容模式）。
- **waitchain 根阻塞者标注语义**：`sessid(a/iit(事务开始时间))`，
  a=active、iit=idle in transaction，时间=根阻塞者事务的 xact_start_time；
  两列齐全（G100 3.0.x，las_flag=3）才有完整标注。**G100 2.2.10 的
  local_active_session/gs_asp 两列皆无**（事件列是 cur_event，las_flag=2），
  此类内核上标注退化为裸 sessid——判读时不要把缺失当异常。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 只有事件数字就下机制结论 | 数字不区分阻塞与计数 | 走 se → 堆栈 → 源码三路；只有数字=观察项 |
| 把 STATUS 事件计入等待占比 | STATUS 是执行阶段标记 | 只作阶段证据 |
| 高频零延迟事件当锁瓶颈 | 计数型事件是访问量镜像 | 按识别法核源码后改判访问量 |
| 栈顶在空闲基线仍写"疑似卡住" | 空闲基线是正常驻留 | 覆盖全窗的基线栈=排除 |
| 前台轻锁表为空就断言无锁问题 | 5 分钟采样可能整个错过 | 写"未采到"，结合 ASP waitchain |
| 用行号定位源码 | 栈无行号，版本间行号漂移 | 函数名为锚 |
| 堆栈用于分钟级因果论证 | 采样间隔大、秒级时间戳 | 堆栈定"在干什么"，不定"何时开始" |

## 版本注记

- se 知识库 417 事件；未收录的事件写"se 未收录，仅按 AWR/ASP 数据判定"，不猜触发点
- 源码结论标注基线（openGauss 6.0.0 树，G100 V3 档）；VastBase 定制分支可能有差异
- PageWriter 源码入口按实际检索为准（版本间有独立采样/状态段差异）

## 实测新锚（2026-10-26 env-M样本三锁，源码当日定论）

### SInvalWriteLock — 共享失效队列写锁（重等待形态）

- 机制：共享失效数组双锁之一（`sinvaladt.cpp:84-97`）。**任何 backend 发失效
  消息（缓存/表失效广播）都要拿 SInvalWriteLock 排他**——写路径
  `SendSharedInvalidationMessage :282`、`SICleanupQueue :354/:454`、`:494`；
  消息源头 = `inval.cpp CacheInvalidateHeapTuple :1276`（DML 命中带 syscache
  缓存的对象即广播）。
- 判读：avg 到**百毫秒~秒级** = 失效消息洪流。追发送方：高频写"被缓存"的系统
  表（catalog）与海量 prepared statement 访问均为候选——**statio 物理读王出现
  pg_sqlplan 类表只是同窗并存信号，不构成同链证据**（pg_sqlplan=VastBase 定制
  计划历史表 pg_sqlplan.cpp，enable_sqlbind=off 时仍可能活跃，写入路径未定论：
  开源基线无此表、官方文档 V2.2.10/V2.2.15 无专章，2026-10-26 env-M样本教训）。
  锁源三选一法：se 溯源等待者 / Callstack 前台栈（缩短采样间隔）/ 双点采样
  嫌疑表体量增速。
- 排除：单纯 waits 高而 avg≈0 按计数型处理，不算瓶颈。

### UniqueSQLMappingLock — unique SQL 追踪哈希分区锁（工具触发形态）

- 机制：unique SQL 追踪哈希按哈希分区加锁（`instr_unique_sql.cpp:126/134`）。
  ** 最重路径 = 全池重置**：`reset_unique_sql()` 系统函数（`:2616`，可由
  SQL 触发）→ `CleanupInstrUniqueSqlEntry(ALL)` → `CleanupAllUniqueSqlEntry
  :2328` **一次拿全部分区 EXCLUSIVE** 并 `InitUniqueSQL()` 重建整表——期间
  全库 unique SQL 语句统计读写全部阻塞。
- 判读：avg **秒级** = 有东西在业务期反复 reset 全局 unique SQL 池（监控/巡检
  工具常见嫌疑）。取证：等待高峰对齐 AWR 快照/工具执行时刻；查 pg_log 里
  `reset_unique_sql` 调用记录。另一诱因 `instr_unique_sql_count` 池满逐出
  （GetRemoveEntryList 分区级删除，代价远小于 ALL）。
- GUC 关联：`instr_unique_sql_count`（池容量）、statement 追踪开关族。

### BufferContentLock — 缓冲页内容锁（热页争用形态）

- 机制：BufferDesc content lock（PG buffer content lock 同源），读写页内容前
  排他/共享获取。**waits 十万级 × avg 百毫秒** = 热页争用——配 statio 逻辑读
  王表定热点对象（该表页被海量会话同时碰）。
- 判读：与逻辑读王表同名即定位到表；缓解走索引优化/分区打散/热点行更新模式，
  不走调参。

## 来源

- se 知识库（vb_tool 内置，417 事件，源码 file:line 触发点）
- 内核源码基线 `<openGauss-6.0.0-src>/`
  （BufHashTableSearch 计数型机制核于 `bufmgr.cpp BufferAlloc()`；
  SInvalWriteLock/UniqueSQLMappingLock/BufferContentLock 三条核于
  sinvaladt.cpp / instr_unique_sql.cpp / inval.cpp，2026-10-26 env-M样本实战回灌）
- vb_tool Callstack 采样器源码（指纹化规则与空闲基线归纳）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。

案例锚点：`../cases/case-envD-errstorm-202610.md`（计数型事件×错误风暴对账）
