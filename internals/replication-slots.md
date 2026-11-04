# 复制槽机制（restart_lsn / WAL 保留 / xmin 反馈）

## 概述

> 服务分类：performance（AWR Replication Slots 段判读）、ha（分类落地后互链）

## 核心机制（slot.cpp，深读要点）

1. **WAL 保留下界**：`ReplicationSlotsComputeRequiredLSN()`（:860 起）在全部槽上
   聚合 **min(restart_lsn)**（含备库槽/归档槽/工具槽各自取 min/max）——WAL 段回收
   不越过最老槽的 restart_lsn。**槽无消费者（active=f）→ restart_lsn 不再推进 →
   WAL 无限保留 → 主库磁盘涨**（AWR 高危项的机制本体）
2. **物理/逻辑槽语义**：物理槽只用 restart_lsn；逻辑槽另有 confirmed_flush 与
   **xmin 反馈**（槽 xmin 也压主库 OldestXmin——逻辑槽inactive 一样卡 vacuum）
3. **VastBase 特有：xmin 反馈忽略窗**（`ignore_feedback_xmin_window`，slot.cpp
   IfIgnoreStandbyXmin）：备库反馈的 xmin 若比 recentGlobalXmin 还老且超窗，
   **忽略该反馈**——防止"备库上一条陈旧 xmin"永久卡主库 vacuum 的保护机制
4. **持久性等级**：RS_EPHEMERAL（RS_DROP_ON_ERROR，崩溃即失）→
   `ReplicationSlotPersist()` 转 RS_PERSISTENT 并立即落盘；**槽状态随
   checkpoint 持久化**（CheckPointReplicationSlots，flush-checkpoint 的 guts
   子系统之一）
5. **candidate_restart_lsn**（:560）：发送过程中记录的候选推进点，确认后转正

## AWR 判读映射

| 现象 | 机制解释 | 方向 |
|---|---|---|
| slot active=f + pg_xlog 盘涨 | 无消费者钉住 min restart_lsn | 删槽/恢复消费——磁盘高危第一条 |
| 逻辑槽 xmin 老 | 槽 xmin 压 OldestXmin 卡 vacuum（与长事务同效） | 推进消费或核对 ignore 窗 |
| 备库xmin 反馈异常陈旧 | ignore_feedback_xmin_window 会丢弃陈旧反馈 | 核对该 GUC 与 recentGlobalXmin |
| 槽重启后消失 | RS_EPHEMERAL 崩溃即失 | 需要持久就 Persist（建槽 persistent） |
| 延迟与槽 restart_lsn 距离大 | 消费慢但槽在推进 | 消费端问题，非槽机制 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| 只盯物理槽 | 逻辑槽 xmin 同样卡 vacuum/保留 WAL |
| inactive 槽只当"状态显示" | 它是 WAL 无限保留的直接机制 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）
- 源码精读（2026-10-25）：slot.cpp:860-940（RequiredLSN 聚合）、
  :943-968（Persist）、:1337+（checkpoint 持久化）、:970-990（xmin 忽略窗）
- 联动：`mvcc-visibility.md`（OldestXmin）、`flush-checkpoint.md`（guts 子系统）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
