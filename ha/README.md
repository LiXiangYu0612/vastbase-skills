# ha/ — 高可用

四件已落地（2026-10-25 初版，**源码先行**：内核侧锚开源 openGauss 6.0.0 基线，
CM 侧以开源 openGauss CM（gitee.com/opengauss/CM）为近似原理——**VastBase 内核与
VastBase CM 源码不可得**，差异以实测对账，见各文件版本注记）。

| 文件 | 场景 |
|---|---|
| `replication-lag.md` | lag 偏大：Net/Fsync/Apply 三段定位与处置矩阵 |
| `replication-architecture.md` | 流复制架构/LSN 四位点语义/模式状态机 |
| `failover.md` | promote 护栏/term 任期/RTO 构成/旧主 rewind |
| `split-brain.md` | 双主三来源/防护锚点/降级窗口的数据代价/处置原则 |

计划主题（落地后删除本清单）：
- `slots.md` 复制槽运维（机制底座已在 `../internals/replication-slots.md`）
- 级联复制与多中心形态（案例积累后成篇）

内容未落地前，涉及未收录主题的问题按实情说明知识未收录。
