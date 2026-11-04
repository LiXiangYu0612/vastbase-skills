# 脑裂成因与防护

## 概述

> 服务分类：ha、troubleshooting。**初版（2026-10-25，源码先行）**：双主的
> 三个来源、内核侧防护锚点、处置原则。

## 双主怎么发生（三个来源）

1. **网络分区 + 自动提升**：仲裁组件与部分节点失联，两组各选出一个主
   （无仲裁的自动 failover 是脑裂的温床）
2. **误操作 promote**：备库被手动/脚本提升时旧主还在服务（护栏文案见
   `failover.md`：非 standby 拒绝——但**对"另一个主还活着"本身内核无感知**）
3. **旧主复活抢位**：切换后旧主恢复上线，试图以原角色继续——term file
   任期号防的就是这个（CheckPromoteSignal 读 term，xlog.cpp:18374——
   低任期不获提升权）

## 内核侧防护锚点（源码实锚）

- **仲裁/共识组件在基线树内**：`gausskernel/storage/replication/dcf/`
  （DCF/Paxos 确定性共识目录）与 heartbeat/——DCF 部署形态下选举走多数派，
  分区少数派组**不产生新主**，脑裂从机制上消除〔DCF 参数与部署形态以
  组件文档为准⚓〕
- **term 任期护栏**：promote 消费 term file（xlog.cpp:18374,18387），
  任期单调——旧主低任期无法合法升主
- **一致性等级权衡**（syncrep.cpp:715-760 深读）：`most_available_sync` +
  `keep_sync_window`——**同步复制失联时延迟进入降级模式的时间窗**（函数名即语义：DelayIntoMostAvaSync，syncrep.cpp:719-760；窗口内提交继续等同步备，窗口耗尽才进入 most_available_sync 降级）：
  未开 → 同步备全挂则主库提交阻塞（保一致，牺牲可用）；开了 → 窗口后
  降级继续提交（保可用，**此时切走旧主=已提交事务在旧主未同步到新主——
  脑裂处置的数据代价**）。keep_sync_window_start/out_keep_sync_window
  即窗口计时状态位
- VIP/接入层防双写：内核无感知，组件/接入层职责（对账清单项）

## 处置原则（判读级）

1. **先定谁是保留主**：以任期（term）+ 已同步位点为序，通常保留多数派侧/
   高任期侧
2. **分叉侧绝不强合**：rewind（`failover.md`）失败即重建，**不要把两边数据
   "合并"**——时间线分叉后合并无一致性保证
3. 复盘三问：检测时延（为何触发提升）？降级窗口配置（most_available_sync
   开没开）？仲裁组件为何没拦（多数派条件）？

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 脑裂当网络抖动处理 | 双主窗口内的双写分叉是数据问题 | 先停一侧、定保留主 |
| 两边数据手工合并 | 分叉后合并无一致性保证 | rewind 或重建 |
| most_available_sync=on 无感 | 可用性换来的降级窗口=切换时的数据代价 | 配置评审时显式权衡 |

## 版本注记

- 初版 2026-10-25；基线 openGauss 6.0.0；DCF 选举细节、keep_sync_window
  默认值未逐行锚⚓
- **基线近似（2026-10-25 用户校准）**：VastBase 内核与 VastBase CM 源码不可得——内核侧
  （term/降级窗口/DCF 目录）以开源 openGauss 为基线；仲裁组件以开源
  openGauss CM（gitee.com/opengauss/CM，选主走 DCC+DCF 多数派）为近似原理，
  差异以实测对账

## 来源

- 基线源码核对（2026-10-25）：syncrep.cpp:715-739（most_available_sync/
  keep_sync_window 降级窗口全逻辑）、xlog.cpp:18374,18387（term file 任期）、
  storage/replication/dcf、heartbeat/ 目录（共识组件存在性）
- 联动：`failover.md`（promote/rewind）、`commit-sync-events.md`
  （同步复制等待语义）、`replication-lag.md`（位点与追平）

## 知识网

见 `../KNOWLEDGE-GRAPH.md`；脑裂边：分区/误提升→双主→term 护栏→降级窗口
代价→rewind/重建。
