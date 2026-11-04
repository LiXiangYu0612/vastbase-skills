# PITR 机制（归档拉取与恢复目标）

## 概述

> 服务分类：backup-recovery（开张件）、troubleshooting（恢复决策）。
> **初版（2026-10-25，源码先行）**：恢复侧内核机制；全备/归档侧工具与
> 策略（轮转/保留/验证）后续补案例与工具件。

## PITR 恢复侧机制（xlog.cpp 实锚）

- **归档段拉取优先**：回放需要的 WAL 段本地 `pg_xlog` 没有时走
  `RestoreArchivedFile`（xlog.cpp:4819，调 restore_command 从归档拉段；
  备库常驻模式未配 restore_command 则返回 :4833 注释明言）；`XLogFileReadAnyTLI`
  （:4655）按时间线候选顺序找段——**恢复时"找不到段"先查 restore_command
  与归档完整性，不是直接判损坏**
- **恢复读取链**：本地 pg_xlog → 归档（RestoreArchivedFile :4591 置
  restoredFromArchive）→ 都没有才报缺失
- **恢复目标与终点动作**：recovery_target 族（time/xid/name/lsn + inclusive）
  与终点动作（promote/pause）〔GUC 表未在 guc.cpp 检出——openGauss 的恢复
  目标配置通道与社区 Postgres 不同，以部署文档与实测为准⚓〕
- **回放引擎**：与备库回放同源（StartupXLOG 循环 → rm_redo 分派，全机制见
  `../internals/wal-replay.md`）——PITR 的"慢"=回放慢（三来源同判）
- **时间线切换**：恢复到目标/promote 时换新时间线、写历史文件
  （timeline history 拉取也走归档 :5829,5927）

## 备份侧事实

- 备份工具 **vb_probackup**（交付名；源码基线名 pg_probackup，bin/ 实锚）
  与 vb_basebackup 同理
  （bin/）——物理备份/增量/校验的工具面
- 归档产出侧 archive_command 属外部命令职责（内核只消费 restore_command）
  〔工具操作细节后续独立成篇〕

## 恢复决策速查

| 场景 | 决策链 |
|---|---|
| 误删数据要找回 | 有全备+归档 → PITR 到误操作前瞬间（目标定位靠日志时间/xid）；无备份 → 停写入评估（vbdul 文件级恢复属另一条产品线） |
| 恢复卡在拉段 | restore_command 路径/归档轮转把需要的段删了（保留期核算） |
| 恢复慢 | 回放三来源判读（wal-replay.md）+ 归档拉取速率 |
| 恢复到目标后行为 | 终点动作配置核对（promote/pause）〔⚓通道待核〕 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 归档只配 archive 不验证 restore | 恢复时才发现段缺失 | 定期演练拉段（DR 演练清单） |
| 保留期拍脑袋 | 回放跨度大时段早被轮转 | 按 RPO/RTO 反推保留期 |
| 恢复目标点当唯一手段 | 目标配置通道未锚⚓ | 实测确认配置通道再演练 |

## 版本注记

- 初版 2026-10-25；基线 openGauss 6.0.0；recovery_target GUC 通道、
  终点动作默认值未锚⚓（以实测+文档补）
- 基线近似（2026-10-25 用户校准）：VastBase 内核源码不可得，以开源 openGauss
  6.0.0 为基线，差异实测对账

## 来源

- 基线源码核对（2026-10-25）：xlog.cpp:391,4591,4655,4819-4833,5142
  （RestoreArchivedFile/XLogFileReadAnyTLI/恢复中断语义）、:5829,5927
  （timeline history 归档拉取）；bin/pg_probackup（=交付 vb_probackup）、bin/pg_basebackup 存在性
- 联动：`../internals/wal-replay.md`（回放引擎）、`../troubleshooting/startup-failure.md`
  （恢复中失败分支）

## 知识网

见 `../KNOWLEDGE-GRAPH.md`；备份边：归档产出（外部）→ restore 拉取（内核）
→ 目标点 → 回放（同源）→ 终点动作。
