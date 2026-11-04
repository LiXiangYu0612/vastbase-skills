# 主备流复制架构与 LSN 位点语义

## 概述

> 服务分类：ha（架构底座）、performance（lag 判读的前置语义）。**初版
> （2026-10-25，源码先行**：复制线程对与位点语义，案例待补）。

## 复制线程对与双流

基线源码 `gausskernel/storage/replication/`：

- **WAL 流**：主库 walsender 线程（walsender.cpp：`WalSndWaitForWal` :233
  等新 WAL、`WalSndKeepalive` :217 心跳）→ 备库 walreceiver 线程写本地 WAL
- **数据流（openGauss 特有）**：datasender/datareceiver + dataqueue.cpp——
  备库**并行回放**的数据队列通道（与回放优化 recovery_optimization_mask
  联动，见 `../internals/wal-replay.md`）；catchup.cpp 追赶状态管理
- 每对主备一条 walsender；replconninfo 定义通道拓扑（级联/多备）〔连接串
  语义以部署文档为准⚓〕

## LSN 位点语义（lag 分解的四个刻度）

```
主库: write(写缓冲) → flush(落盘) → send(walsender sentPtr)
备库: receive(写本地WAL) → write → flush → replay(回放)
```

- walsender 侧 `sentPtr`（walsender.cpp:288,480-483——**挂复制槽时与
  restart_lsn 联动** :480），keepalive 消息携带 walEnd 位点
- 备库把 write/flush/apply 三位点**周期回传**主库（反馈消息）——主库 AWR
  Replication 段的 lag 三段分解（Net/Fsync/Apply）全部由这些位点差算出
  （段定义见 `commit-sync-events.md`）
- 同步复制等待目标 = `WalSndCtl->lsn[mode]`（mode 由 synchronous_standby_names
  决定 sync/flush/replay——等哪个位点就是哪种一致性等级）

## 模式状态机（postmaster.cpp:1992-2004）

`HaShmData->current_mode`：**PENDING（初始仲裁中）→ PRIMARY / STANDBY /
NORMAL**（NORMAL=单机无复制）。PENDING 态不接受业务连接——启动后先由
集群组件裁决角色再开放（见 `failover.md` 判定归属）。

## 同步语义（synchronous_commit 取值 × 备库位点，第9课入库）

| 取值 | 方式 | 返回客户端成功的条件 | 持久化级别 |
|---|---|---|---|
| off/false/no/0 | 异步 | WAL 写入主库 wal_buffer | 0 |
| local | 异步 | WAL 写入主库磁盘 | 1 |
| remote_receive | 同步 | 备库**收到** WAL（写入备库缓存） | 1 |
| on/true/yes/1 | 同步 | 备库收到并**落盘** WAL | 2 |
| remote_apply | 同步 | 备库**完成回放**该部分 | 2 |

性能序：remote_apply > on > remote_receive > off。

**synchronous_standby_names 形态**（决定 walsender 等谁、等几个）：
- `S1,S2`：S1 同步，S2 潜在（S1 故障时升同步）
- `FIRST n (…)`：优先级制，前 n 个同步；`ANY n (…)`：quorum 仲裁，任意 n 个
- `*`（默认）：任一备库 sync、其余 potential
- sync_state 四态：sync / potential（潜在同步）/ quorum（仲裁候选）/ async；
  仅同步备库的 ACK 让后端事务完成，potential/async 的 ACK 不影响提交

**most_available_sync=on**：同步备故障时主库不再阻塞（等其余正常备库即返回）；
默认 off=阻塞保护（见 `cm-cluster-manual-ops.md` 场景 13 的停备 hang）。关停/
扩缩容纪律：同步复制下严禁同时停两个节点，先备后主。

**级联复制**：级联备库既收又发 WAL（转发器），目前**只能异步**；作用=卸主库
带宽。**延迟回放**（recovery_min_apply_delay，备库设置，按主库事务提交时间戳
与备库当前时间差计算，**要求主备时钟一致**）：提供"可查的历史副本"纠误操作；
与 remote_apply 组合会阻塞主库（延迟备库必须异步）；与 hot_standby_feedback
叠加放大主库膨胀。

**build 模式**（备机重建）：full（全量镜像）/ incremental（解析 XLOG 找差异
修复，前提 WAL 连续）/ auto（先增量失败转全量，默认）。约束：**一主一备且未开
最大高可用时增量不可用**（需全量或先开最大高可用）；数据文件损坏/目录丢失只能
全量。级联备库重建需 -M cascade_standby。

**vb_ctl query 字段速查**（DB 状态机）：db_state = Normal / Need repair /
Starting / **Waiting（Wait promoting 升主等待）** / Demoting / Promoting /
Building / Catchup / Coredump；local_role = Normal / Standby / Primary /
Cascade Standby。sync_percent = (receiver flush − syncStart)/(sender send −
syncStart)。state（walsender）= start-up → catch-up → streaming（+backup）。

**failover vs switchover**：failover 备机独立完成（vb_ctl 发信号 → PM 置
failover_triggered → walreceiver 退出 → startup 完成回放退出 → PM 改 Primary）；
switchover 需主备交互（备机发起 Demote 请求 → 主库等日志传完降备 → 备机升主），
**必须在升主的备机上执行**；上次切换未完成禁止再次切换；build 进行中切换会拖长
（主库要等日志发完才降）。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| lag 当单一名词 | 四位点三段差，器官不同 | 三段分解（`replication-lag.md`） |
| 备库只有 WAL 流一条道 | openGauss 双流（WAL+数据队列并行回放） | 回放优化判读先分通道 |
| 主备角色当配置常量 | 启动期状态机裁决（PENDING→角色） | 启动问题查角色判定日志 |
| synchronous_commit=on 当"备库已回放" | on 只到备库落盘；要回放完成是 remote_apply | 按上表取值核对一致性需求 |
| 延迟回放备库配 remote_apply | 延迟备库必须异步，否则主库被阻塞 | 组合前查本文约束 |

## 版本注记

- 初版 2026-10-25；基线 openGauss 6.0.0；walreceiver 反馈函数名/位点上报
  细节未逐行锚⚓；DCF 部署形态见 `split-brain.md`
- 同步语义/级联/延迟回放/build/vb_ctl query/failover-switchover 流程
  （2026-11-04 入库）：内部课件《第9课：高可用原理（一）》（用户提供 D:\log\1）
- **基线近似（2026-10-25 用户校准）**：VastBase 内核与 VastBase CM 源码不可得——本域
  机制以**开源 openGauss 内核（6.0.0）为基线，CM 侧以开源 openGauss CM
  （gitee.com/opengauss/CM）为近似源**；原理近似，差异以实测对账

## 来源

- 基线源码核对（2026-10-25）：walsender.cpp:217,233,288,480-483（keepalive/
  WaitForWal/sentPtr/槽联动）、syncrep.cpp:715-739（most_available_sync 与
  keep_sync_window 降级窗口）、postmaster.cpp:1992-2004（模式状态机）、
  storage/replication/ 目录（dataqueue/datasender/catchup/dcf/heartbeat 存在性）
- 内部课件《第9课：高可用原理（一）》（2026-11-04 用户提供）：同步语义表/
  standby_names 形态/级联/延迟回放/build 约束/vb_ctl query 字段/failover-
  switchover 流程（流程类课件内容，按收录标准以文档为源、待实测对账）
- 联动：`replication-lag.md`（三段分解）、`commit-sync-events.md`（同步等待）、
  `has-dcs-architecture.md` / `cm-cluster-manual-ops.md`（两套 HA 栈）

## 知识网

见 `../KNOWLEDGE-GRAPH.md`；本文件是 ha 层底座边：lag→位点语义→walsender/
walreceiver 线程对。
