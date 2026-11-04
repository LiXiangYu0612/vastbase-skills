# WAL 回放与备库（apply 循环 / 回放冲突 / 并行回放）

## 概述

> 服务分类：ha（核心）、performance（Replication lag 的 Apply 段解释）

## 回放循环（StartupXLOG，xlog.cpp:9250 主循环）

- startup 线程 ReadRecord 逐条读 WAL → **RmgrTable[rmid].rm_redo 分派**到各资源
  管理器重放（Heap/Btree/Transaction/CLog…），页面修改先进缓冲池再按脏页链路落盘
  ——所以备库也有 Pagewriter/双写行为，备库 IO 判读与主库同构
- 回放位点（replay LSN）即 AWR Replication 段 **Apply lag** 的分母端：
  Apply lag = flush − replay，回放吞吐不足的三大来源：**单回放线程串行**
  （recovery_optimization_mask 部分并行项）、**大事务回放**、**回放冲突等待**
- 恢复模型：VastBase 只有 REDO 无 UNDO（ARIES 变体），未提交事务的 XLOG 也会
  回放（差异不影响物理一致性，MVCC 层面由 CSN 可见性兜底）

## 回放冲突（standby.cpp:147-245 ResolveRecoveryConflictWithVirtualXIDs，深读）

- 冲突类型与 snapshot 冲突深链（Vacuum/Freeze/禁快照连锁/参数控制面）已独立成
  `recovery-conflict.md`——判读先查该文件
- 处理：收集冲突 backend → **指数退避等待**（standbyWait_us 从 100ms 起倍增、
  **1s 封顶**）→ 超过 max_standby_streaming_delay（GetStandbyLimitTime）后
  **cancel 备库查询**（"canceling statement due to conflict with recovery"，
  统一取消家族 :6778）
- 等待期间上报 **STATE_STANDBY_READ_RECOVERY_CONFLICT**——又一个"STATUS 类但
  是真等待"（真等待清单再 +1，见 `commit-sync-events.md` 修正条）
- **hot_standby_feedback**：备库回传 xmin 给主库，主库 vacuum 避开→冲突消失但
  主库膨胀风险转移（联动 `replication-slots.md` 的 xmin 反馈/忽略窗）

## 并行回放（XS-DB-79 深读）

**为什么能并行**：XLOG 按产生顺序落 WAL，串行回放=重演主库时序。不相关 XLOG
（不同物理文件）交换顺序不影响最终一致性；有依赖的（同页面、create 后 insert、
事务提交须等本事务全部日志）必须保序。

**架构**：WalRcvWriter 写盘 → Startup 读 XLOG 调 **DispatchRedoRecord**
（xlog.cpp:10547）分发：堆表 XLOG 按文件唯一标识（db oid/ts oid/relfilenode/
flags）hash 取余入 SPSC 队列；事务类日志不分发由 Startup 串行回放；
PageRedoWorker 消费。

**依赖同步封装（redo_item.h:42-43 实锤）**：
- `sharewithtrxn`（true=PageRedoWorker 回放它并等 Startup 完成才继续）：如事务
  内建多表后回滚，清无效页由 worker 做、unlink 由 Startup 做，需同步
- `blockbytrxn`（true=Startup 等对应 worker 回放完才推进最大连续 LSN）：
  hash/gin/spgist/gist 索引在 hot standby 下需此处理
- `shareCount`/`refCount`：广播型日志的分发计数与"最后持有者负责释放"

**已知取舍（判读价值）**：索引元组不含事务信息且索引/堆是不同文件——
"先插堆后插索引、先清索引后清堆"的依赖**并行回放并未保证**，备库
IndexOnlyScan 不回表结果可能错；主线已强制回表规避。见到备库 IOS 结果
可疑先想到这条。

**极致 RTO**（更细粒度流水线，原理同并行回放）：XLogReadWorker（优先缓存读）
→ XLogReadManager（decode 成 RedoItem）→ Startupxlog 按 relfilenode 分发 →
ParseRedoRecord（按页粒度拆分）→ PageRedoManager（哈希表按页组织、同页按 LSN
保序）→ PageRedoWorker 批量回放；TrxnManager/TrxnWorker（单线程）处理事务类。
**三约束**：不支持备库读、不支持主备从（replication_type=0）、不支持列存；
与并行回放互斥（同时开 recovery_parse_workers 和 recovery_max_workers 则
并行回放失效）。

**参数**：recovery_max_workers（并行回放 worker，0~20 默认 1）、
recovery_parse_workers（1~16 默认 1）、recovery_redo_workers（每解析线程的
回放 worker，1~8 默认 1）、recovery_parallelism（实际回放线程数查询口）。

## AWR 判读映射

| 现象 | 机制解释 |
|---|---|
| Apply lag 大 | 回放吞吐：单线程串行/大事务/冲突等待三选一（结合备库 CPU 与冲突日志） |
| 备库查询被 recovery conflict cancel | delay 预算不足或 hot_standby_feedback 未开（深链查 recovery-conflict.md） |
| 备库也有刷脏 IO | 回放写缓冲池，同主库链路 |
| 备库回放宕库风险 | recovery_optimization_mask 地雷（标准表两次收紧，见 admin 基线） |
| 备库 IndexOnlyScan 结果可疑 | 并行回放不保证堆/索引依赖，强制回表规避（本文） |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 极致 RTO 当"更强的并行回放"随便叠开 | 与并行回放互斥；不支持备读/主备从/列存 | 按三约束先核对形态 |
| 备库数据问题先怀疑网络 | 并行回放有已知依赖取舍（IOS） | 排除表先核对回放形态与版本 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）；rm_redo 分派锚点（xlog.cpp RmgrTable）
- 源码精读（2026-10-25）：standby.cpp:147-245（指数退避+超时 cancel）、
  ResolveRecoveryConflictWithSnapshot :279；回放主循环 StartupXLOG :9250〔锚点〕
- 并行回放/极致 RTO（2026-11-04 入库）：内部文档《XS-DB-79 数据恢复过程》
  （依赖性分析/sharewithtrxn）+《第9课：高可用原理（一）》（极致 RTO 管线与
  参数约束）；源码核实 redo_item.h:42-43、DispatchRedoRecord xlog.cpp:10547

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
