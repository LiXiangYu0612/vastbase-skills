# CM 集群 CN/DN 异常人工处理清单（13 场景）

## 概述

> 服务分类：ha（CM 方案运维手册）。收录 **cm 自动机制（自动拉起/自动 rebuild/
> 自动 failover）处理不了、必须人工介入**的 6 类 13 场景。判总表先定故障域，
> 再跳场景。命令以 gs_* 示（VastBase 可用 vb_ctl/vb_guc 等价）；执行变更前
> 备份配置。HAS 方案见 `has-dcs-architecture.md`。

## 判定总表

| 故障域 | 必须人工的判定标准 | 核心命令 |
|---|---|---|
| DN 备 | 自动 build 失败 / need repair / 整机故障 / WAL 分叉 | gs_ctl build -b full |
| DN 主 | failover 卡死 / 多数派不满足 / 数据损坏起库失败 / 脑裂 | gs_ctl failover、restart -M primary、备份恢复 |
| CM 组件 | cm_server 反复 coredump / 元数据损坏 / 配置失配 | 6 步重建、gs_om -t generateconf |
| CN | 无主备机制，损坏只能人工拉起或重建 | gs_ctl start / buildcn |
| 同步保护 | 同步备不可达 + most_available_sync=off 主库阻塞 | 临时单节点启动 |

## 场景速查（现象 → 动作）

**DN 备（3）**
1. **持续 need repair / build 反复失败**（cm 自动 build 依赖 WAL 连续，落后超
   wal_keep_segments 或丢归档即失败）→ 故障备上 `gs_ctl build -D $PGDATA -b full -q`
   → query 确认 Normal → `cm_ctl start -n <nodeid>` → status --detail
2. **数据目录损坏 / WAL CRC 分叉**（switchover 后原主变备全量 build 反复失败
   是典型）→ 停节点 → 隔离损坏目录 → 全量 build → 起 cm。预防：switchover 前
   按 synchronous_commit=on 级别对齐主备（非强同步下 LSN 分叉只能 build）
3. **整机故障/换硬件** → 修硬件 → 清 cm 残留 → build 全量 → cm start →
   确认 balanced=Yes

**DN 主（4）**
4. **failover 未触发 / 卡 Wait promoting / pending-failover-primary**（cm
   前置校验不过）→ 确认主库确不可用 → 备上 `gs_ctl failover -D $PGDATA`；
   校验卡住则 `cm_ctl pause` → failover → `cm_ctl unpause` → refreshconf
5. **多数节点故障选不出主**（cm_server 多数派 ≥1/2 不满足，仲裁失效）→
   `cm_ctl pause` → 各存活节点 `pg_controldata | grep "Latest checkpoint
   location"` 取最大者起主（备则 failover，否则 restart -M primary）→ 手动
   挂 VIP → unpause
6. **集群塌缩单节点长期运行**（机房改造等）→ cm_ctl stop → 全节点注释
   om_monitor crontab + kill om_monitor → 主节点注释 replconninfo →
   `vb_ctl start -D $PGDATA -M primary` 单机模式 → 挂 VIP。回集群反向操作
   （停库→恢复 replconninfo→恢复 crontab→备全量 build→cm_ctl start）
7. **主库数据损坏起库失败** → 先试 restart -M primary（前提无严重报错且近期
   checkpoint 成功）→ 不行走备份恢复/PITR → 主起来后其余备全量 build →
   cm start

**双主脑裂（1）**
8. **两个 P 节点** → 从 VIP 位置/数据库日志/Xlog 时间线判定真主 → 停伪主 →
   **伪主上业务写入的数据先手工导出导入新主（否则丢数据）** → 伪主全量
   build 重做为备 → cm start

**CM 组件（2）**
9. **cm 元数据损坏**（cm_server 频繁 coredump，诱因常为磁盘坏 dynamic 文件）
   → 6 步：注释 om_monitor crontab → 依次 kill om_monitor/cm_agent/cm_server
   （含 fenced UDF）→ 删 cluster_dynamic_config → `gs_om -t generateconf -X
   <xml> --distribute`（--distribute 必带）→ 定向删 cm 元数据（先 cm_ctl
   view | grep cmDataPath 确认路径；rm -rf dcf_data cmserver/gstor）→
   恢复 crontab 等 om_monitor 拉起。只清 dcf 时执行 1/2/5/6 步
10. **cm 记录与实例失配**（人为改过端口等）→ cm_ctl view 对 xml 核差 →
    generateconf 对齐 → 重启集群

**CN（2）**
11. **CN 进程异常拉不起**（CN 无主备无自动 build，区别于 DN）→ 排根因（日志/
    端口/权限）→ `gs_ctl start -D <cn_dir>`；目录损坏则 `gs_om -t buildcn -X
    <xml>` 重建
12. **全部 CN 故障** → 业务完全无法接入（DN 再健康也没用）→ 修硬件 → DN 角色
    节点全量 build → CN 拉起/重建 → 全员 status 确认

**维护不当（1）**
13. **同步复制下停备机主库写 hang**（most_available_sync=off 时这是**设计
    行为**：同步目标不可达就阻塞，保数据）→ 紧急保业务：切主后注释
    replconninfo → 单节点启动挂 VIP → 业务恢复后停单节点回集群模式。
    预防：扩缩容先备后主、同一时刻只停一个节点、同步复制严禁同时停两节点

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 脑裂直接停伪主了事 | 伪主上已写入数据会丢 | 先导出导入再重做（场景 8 第 3 步） |
| 停 2 节点后反复等自动恢复 | 多数派不满足时无任何自动 failover | 场景 5 人工起主 |
| 同步 hang 时无脑改 most_available_sync | 该参数动的是数据不丢承诺 | 按"保业务/保数据"显式决策（场景 13） |

## 版本注记

- 整理归纳版手册（非原文原件；2026-09-12 版，含 MVS 工单依据场景标注）；
  gs_*/vb_* 命令按环境等价替换；工单号等内部引用不入公仓发布版
- 依据：交付应急手册 v2.2.0 + MVS 历史工单 + blog《Vastbase G100 的主备机制》
  + openGauss cm 通用运维经验（用户提供 D:\log\1，2026-11-04 入库）

## 来源

- 内部文档《故障手册-20260912-cm集群CN-DN异常人工处理清单》（用户提供，
  2026-11-04 入库）
- 联动：`failover.md`（promote 护栏——场景 4 卡的就是它）、`split-brain.md`
  （脑裂成因与防护，本文件是操作面）、`replication-slots.md`（build 失败的
  WAL 下界）、`has-dcs-architecture.md`（另一套栈）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
