# backup-recovery/ — 备份恢复

`pitr-mechanism.md` 已落地（2026-10-25 初版，源码先行：恢复侧内核机制，
基线 openGauss 6.0.0，VastBase 源码不可得以开源近似、实测对账）。

| 文件 | 场景 |
|---|---|
| `pitr-mechanism.md` | PITR：归档拉取优先级/恢复读取链/回放同源/恢复决策 |

计划主题（落地后删除本清单）：
- `backup-strategy.md` 备份策略（全备/增量/轮转保留期——按 RPO/RTO 反推，
  案例积累后成篇）
- `probackup-ops.md` vb_probackup 操作件（交付名 vb_；基线源码名 pg_）
- 恢复演练清单（DR 演练：restore_command 拉段验证、目标点核对）

内容未落地前，涉及未收录主题的问题按实情说明知识未收录。
