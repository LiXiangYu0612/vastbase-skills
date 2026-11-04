# HAS + DCS 高可用组件（架构/选举/pause/运维）

## 概述

> 服务分类：ha。VastBase G100 的两套 HA 栈之一：**HAS（python 编写的集群
> 管理组件）+ DCS（分布式配置存储）+ VIP**，与 CM 方案（`cm-cluster-manual-ops.md`）
> 并列。回答：HAS 四模块怎么决策、DCS 存什么、选举参数与最健康节点判定、
> pause 维护态语义、启停顺序、DCS 故障两形态。

## 组件分工

| 组件 | 职责 |
|---|---|
| VIP | 对应用提供连接端点，切换时由 HAS 回调脚本漂移 |
| DCS | 集群关键信息存储（见下）；自带多数派仲裁 |
| HAS | 接管数据库启停、检测主备状态、自动故障切换、节点异常自动拉起、统一模板管参数、在线加节点、异步复制最小丢失量配置 |

## DCS 关键键值（/service/vastbase/）

| key | 内容 |
|---|---|
| initialize | 数据库 system identifier |
| members | 全员连接信息/状态/角色/同步位置/ttl |
| config | YAML 配置参数 |
| leader | 当前 leader 节点名+过期时间+ttl（**超 ttl 未更新即被 DCS 删除→触发选举**） |
| optime/leader | leader 节点最新 WAL LSN 位 |

## HAS 四模块（决策树）

- **run_cycle**（主循环，间隔 loop_wait）：加载 DCS 集群信息 → 数据目录空走
  Node bootstrap；节点处于开始/停止/重启/初始化中：leader 更新 Leader key、
  非_leader 持久化状态到 DCS；恢复失败：leader 移除 Leader key；正常则按
  有无 leader 分流 healthy/unhealthy
- **Node bootstrap**：$PGDATA 为空触发——leader 走 initdb、非 leader 走
  vb_basebackup 部备
- **process_healthy_cluster**：非 leader 跟随；leader 且库以主跑→持久化；
  leader 但库不是主→提升；**Leader 锁更新失败（DCS 写不进）→降级**
- **process_unhealthy_cluster**：非最健康节点跟随；最健康节点抢 Leader 锁，
  抢到且是 Master→保持；抢到但不是主→提升为主

## 选举（参数与判定）

| 参数 | 默认 | 含义 |
|---|---|---|
| loop_wait | 10s | 主循环间隔（每轮更新 Leader key+TTL） |
| retry_timeout | 10s | 失联后重试窗口（2 次尝试） |
| ttl | 30s | Leader key 生存期，超时即触发选举 |

**最健康节点判定（按序）**：①HAS 非 pause；②tag 中 nofailover=false；
③synchronous_mode=true 时=原同步备库；④未设 maximum_lag_on_failover 直接过，
设了则**备库 WAL 位点与 DCS 中 leader 最后注册位点差值超限者出局**；⑤
WAL 写入位点最大者；⑥并列时先抢到 Leader key 者。

**synchronous_mode**：选一个同步副本，只有 leader 与同步副本可参与选举
（成功提交事务不丢）；会置 synchronous_standby_name='*'（仅一个同步副本）。
代价：同步备必须完成全部日志同步才能升主——主备同时故障时其余异步备不可接
（可用性换不丢）。

**maximum_lag_on_failover**：允许丢的量（字节），控制可参选的事务量。

## pause 维护态（hasctl pause/resume）

保留 HAS 与 DCS 通信、与集群"分离"：暂停自动 failover。语义要点：
- 有 Leader 锁的节点：HAS 更新锁；若该节点已手动降级，HAS **释放锁而不提升**
- 允许手动重启/重初始化/指定目标节点的手动 failover（无需运行中的主）
- 检测到"并行主库"（双主）只告警不降级
- 完全无 Leader 时不提升任何副本（例外：手动升主场景只有指定候选可拿锁）
- VB 停着 HAS 不拉起；HAS 停着不停所管实例

## 集群启停与监控

- 启动：先 DCS（第一个节点会阻塞，第二个起来后解除）→ 先主库 HAS → 备库 HAS
  （`systemctl start dcs` → `systemctl start has`）
- 停止：**先备库 HAS 再主库 HAS**（防先停主库触发不必要的自动切换）→ 停 DCS
- `hasctl -c <yml> list`：Group/Xlog Location/Member/Host/Role（Leader/
  Sync standby/空=异步）/State/TL 时间线/Lag in MB（主 current − 备 received）
- `hasctl switchover`：交互确认主库与候选库；切换=备升主拿 Leader key +
  回调脚本漂 VIP，原主降备释放锁
- `hasctl edit-config/show-config`：不需重启的参数任意节点改、全员异步生效；
  需重启的打 pending_restart 标记逐点重启或窗口期整体重启

## DCS 故障两形态

| 形态 | 行为 | 影响 |
|---|---|---|
| DCS 主缺失（少数节点停） | 备发起新选举；HAS 查询失败时随机挑节点读，恢复后回主 | 业务基本无感 |
| DCS 过半节点不可用（含主） | 选举一直超时循环，DCS 集群非健康 | **即使数据库正常，VIP 也不可用**——HAS 拿不到集群状态 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 停集群先停主库 HAS | 触发不必要的自动切换 | 先备后主 |
| 数据库正常但 VIP 不通当数据库故障 | DCS 过半不可用时 HAS 无法工作 | 先查 DCS 集群健康 |
| pause 后以为切换还能自动 | pause 就是为禁止自动 failover | 维护完 resume |
| synchronous_mode 当免费保险 | 主备同故障时其余备库不可接（可用性代价） | 按业务一致性需求选 |

## 版本注记

- 内容源为内部课件《第11课：高可用原理（三）》（HAS 组件+组件协同，用户提供
  D:\log\1，2026-11-04 入库）；流程类知识以文档为源，字段与参数待实测对账⚓
- 与 CM 方案判读区分：命令族 hasctl/has 进程 vs cm_ctl/cm_server；两套不混部

## 来源

- 内部课件《第11课：高可用原理（三）》（2026-11-04 用户提供 D:\log\1）
- 联动：`replication-architecture.md`（流复制底座——HAS 管的就是它）、
  `failover.md`（提升护栏）、`split-brain.md`（并行主库只告警不降级的语义）、
  `cm-cluster-manual-ops.md`（另一套栈的人工处理清单）、`read-write-splitting.md`
  （HAS 信号联动读写分离中间件）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
