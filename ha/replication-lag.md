# 主备延迟诊断（lag 三段分解）

## 概述

> 服务分类：ha（延迟定位与处置）、performance（AWR Replication 段判读）。
> **初版（2026-10-25）**：操作层合成文件——机制结论全部来自
> `../internals/`（wal-replay / commit-sync-events / replication-slots）与
> `../linux/network.md`，本文件不复制机制只做诊断流程编排。

## 唯一正确入口：三段分解（不看总数的 lag 都是猜）

主库 AWR Replication 段按 LSN 差拆三段（段定义与 vb_tool 口径见
`commit-sync-events.md`）：

```
主库 flush ──sent──→ 备库 write ──flush──→ 备库 flush ──replay──→ 备库 replay
            └─ Net 段 ─┘        └─ Fsync 段 ─┘         └─ Apply 段 ┘
```

**哪段大就查哪段**，三段的病灶器官完全不同：

## 分段处置矩阵

| 偏大段 | 机制指向（深读文件） | 现场动作 |
|---|---|---|
| **Net 大**（sent−write） | walsender 发送/网络/备端接收写：丢包与重传在内核（`../linux/network.md` 丢包三道关/全队列连锁）；带宽不足 | 备库主机 `softnet_stat`、nstat 重传统计、专线带宽；与 DB 参数无关先别动库 |
| **Fsync 大**（write−flush） | 备库刷盘慢：`../internals/flush-checkpoint.md` + `../linux/io-storage.md`（设备 flush 列/vm.dirty_* 互踩） | 备库 iostat -x 与 flush 列；journal/RAID 卡电池；备库刷脏参数 |
| **Apply 大**（flush−replay） | 回放吞吐三来源（`../internals/wal-replay.md` ✅深读）：**单回放线程串行**（recovery_optimization_mask 部分并行项，标准表两次收紧的地雷）、**大事务回放**、**回放冲突等待**（指数退避 1s 封顶 → max_standby_streaming_delay → cancel 备库查询） | 备库 CPU（回放线程吃满？）、logrpt 找 conflict cancel 记录、主库大事务榜（Apply 滞后常是主库大事务的影子） |

## 三条连锁（判读时顺边走）

1. **同步复制放大链**：synchronous_commit=on 时主库 commit 等
   wait wal sync（`commit-sync-events.md`）——**备库任何一段慢都变成主库
   commit 延迟**；主库 LogFileSync 高 + lag 高 → 先修备库/网络，不是主库盘
2. **槽积压连锁**：延迟大 → restart_lsn 推不动 → WAL 堆积 → 主库磁盘
   （`../internals/replication-slots.md`：restart_lsn=WAL 下界；逻辑槽另压
   xmin 卡 vacuum）——**磁盘涨先查槽**，别急着删 WAL
3. **备库查询慢的另一形态**：回放慢时备库取快照要等回放过清理点
   （STATE_STANDBY_GET_SNAPSHOT，真等待，`locks.md` ProcArray 深读注）——
   备库查询延迟尖峰先看回放位点差，不是备库 SQL 本身

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 只看 lag 总数调参 | 三段病灶器官完全不同 | 三段分解先行 |
| Apply 大调主库参数 | 回放瓶颈在备库（串行/冲突/大事务） | 备库侧取证 |
| Net 大当数据库问题 | 是内核网络层/带宽 | softnet_stat+nstat+带宽 |
| 主库 commit 慢只盯主库 | 同步复制把备库/网络延迟灌回主库 | 配 wait wal sync 计数联判 |
| 磁盘涨就删 WAL | 槽 restart_lsn 压着下界，删了会积压更狠 | 查槽位与延迟根因 |

## 版本注记

- 初版（2026-10-25）：诊断流程与分段矩阵成稿；机制结论逐条挂源码锚在三个
  internals 文件，本文件改动只需跟随它们
- 基线 openGauss 6.0.0（G100 V3 Build 8）；recovery_optimization_mask 地雷
  对照 `../admin/g100-standard-params.md`

## 来源

- 机制来源（不复制，按 file:line 溯源）：`../internals/wal-replay.md`
  （回放循环/冲突指数退避）、`../internals/commit-sync-events.md`（lag 段
  定义/wait wal sync）、`../internals/replication-slots.md`（restart_lsn）、
  `../internals/locks.md`（STATE_STANDBY_GET_SNAPSHOT）、
  `../linux/network.md`、`../linux/io-storage.md`
- 初版日期 2026-10-25；网上公开案例仅作交叉参照，机制口径以源码为准

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；ha 层出口：延迟→三段→
机制文件；切换/脑裂/级联主题落地见 `README.md` 计划清单。
