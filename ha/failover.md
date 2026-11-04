# 失败判定与切换（failover/promote）

## 概述

> 服务分类：ha。**初版（2026-10-25，源码先行）**：内核侧 promote 语义与
> 护栏；**失败检测/仲裁归属集群管理组件**（基线源码树不含 CM——判定算法
> 与仲裁参数以部署环境的组件文档为准，这里只讲内核配合面）。

## promote 的内核侧护栏与流程（源码实锚）

- **角色守卫**（postmaster.cpp:9781-9785）：非 STANDBY_MODE 拒绝 promote——
  日志文案三连："Instance can't be promoted in none standby mode" /
  "…in standby cluster" / "…in hadr_main_standby mode"——**promote 前角色
  校验失败看这三条**
- **触发链**：集群组件写 term file + 发信号 → `CheckPromoteSignal`
  （xlog.cpp:18341-18387，**term file 里的 term 号即任期**——CM 选举的
  单调递增任期，防旧主复活抢位）→ PMSIGNAL_PROMOTE_STANDBY（:18379）
  → postmaster :10749 响应
- **promote 本体 = 结束恢复 + 升主**：回放到 WAL 尾 → 写 end-of-recovery
  checkpoint → 切换时间线 → 开放写入。**RTO ≈ 失败检测时延 + promote
  （剩余回放量）**——回放落后越多，切换窗口越大（这是 lag 治理的 ha 价值）
- 级联备库随主 promote 联动（:10261 "promote cascade standby"）

## CM 检测与仲裁（开源 CM 源码实锚，2026-10-25 clone 至 <openGauss-CM-src>）

检测/仲裁参数从"近似"升实锚（组件形态：cm_agent 每机一进程 ↔ cm_server
主备仲裁，仲裁按部署形态分片）：

- **两级心跳**：agent↔cm_server 断链判定 `agent_heartbeat_timeout`（**默认
  8s**，cma_global_params.cpp:179；超时文案 "connection to cm_server %u
  seconds timeout expired" cma_process_messages.cpp:2298）；实例级判定
  `instance_heartbeat_timeout`（**默认 6s**，cms_global_params.cpp:147，
  参数校验范围 ctl_param_check.cpp:82）
- **节点存活判定**：`IsNodeRhbAlive`（cms_arbitrate_cluster.cpp:455-465）——
  心跳计数 > 阈值即判死（DEBUG1 日志带 nodeIdx/心跳值/阈值）——**failover
  检测时延 ≈ 该超时 + 仲裁决策**，RTO 公式的第一项从此有了数字
- **仲裁文件族按形态分片**：cms_arbitrate_datanode_single（主备）/
  _psd/_pms（共享存储形态）/cluster/synclist——判读仲裁行为先对形态
- 选主依赖 DCC+DCF（多数派），term file 任期号即与内核 promote 的接缝
  （`split-brain.md`）

## 切换后的旧主处置

旧主在分叉点后可能有本地写入（脑裂窗口产物）：

- **pg_rewind 回退**（bin/pg_rewind 源码实锚：datapagemap/fetch 等）——
  按新主时间线回卷差异块，再作为备库追平；**前提是旧主确实停写且分叉可
  计算**
- 分叉过大/不可回卷 → 重建备库（全量+追平）
- 判定"能不能 rewind"：分叉只发生在 promote 之后旧主的写入；同步复制环境
  分叉量小，异步环境可能大（见 `split-brain.md` 一致性权衡）

## 判读速查

| 现象 | 指向 |
|---|---|
| promote 被拒 | 三条角色守卫文案（本文件护栏节）——先核对角色模式 |
| 切换慢 | RTO 构成：检测时延（组件参数）+ 剩余回放量（lag 治理） |
| 旧主追不上/反复断 | 分叉处理：rewind 失败→重建；查 term file 任期 |
| 切换后应用连旧 VIP | VIP 漂移属组件层，内核无感知——组件配置对账 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| promote 当即成功 | 剩余 WAL 还要回放完（RTO 公式） | 切换前看 lag |
| 旧主直接拉起当备库 | 本地分叉写入未回卷 → 追平失败 | rewind 或重建 |
| 检测慢怪数据库 | 检测归集群组件（基线树无 CM） | 组件参数与心跳配置 |

## 版本注记

- 初版 2026-10-25；基线 openGauss 6.0.0；end-of-recovery checkpoint 细节⚓
- **基线近似（2026-10-25 用户校准）**：VastBase 内核与 VastBase CM 源码不可得——内核侧
  以开源 openGauss 为基线，**CM 侧以开源 openGauss CM 为近似源（已 clone
  `<openGauss-CM-src>`，检测/仲裁参数升级为实锚，见上节）**；VastBase 侧差异以实测对账

## 来源

- 基线源码核对（2026-10-25）：postmaster.cpp:9781-9785（角色守卫三文案）、
  :10749（promote 响应）、:10261（级联 promote）、xlog.cpp:18341-18387
  （CheckPromoteSignal/term file 读取与 PMSIGNAL_PROMOTE_STANDBY）、
  bin/pg_rewind（源码存在性：datapagemap.cpp/fetch.cpp）
- 联动：`replication-lag.md`（RTO 的 lag 项）、`split-brain.md`（分叉与仲裁）

## 知识网

见 `../KNOWLEDGE-GRAPH.md`；切换边：检测（组件）→ promote（内核护栏）→
旧主 rewind（bin 工具）→ 追平（回放）。
