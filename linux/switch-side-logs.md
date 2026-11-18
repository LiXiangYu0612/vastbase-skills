# 交换机侧日志与端口判读（VRP / 华为 S 系列框式）

## 概述

> 服务分类：linux（网络域的最下游延伸——`network.md` 管主机侧收发路径，本文管
> 主机网卡之外那一段）、performance（网络丢包分层链的最外一层）、
> troubleshooting（丢包/重传排查走到"查交换机"这一步时的操作件）。
> **范围约束**：VRP 内核源码不可得，本文日志格式字段以实拍 logbuffer 为准，
> 拥塞/丢弃机制层为通用交换架构定论 + 显式标注的推断；交换机 CPU 门限告警
> （如 TCPSECDOT1X 等其他日志族）未实拍，不收。

## 入口路由（触发条件，三选一即进本件）

- 主机侧已排查完：`network.md` 丢包三道关（softnet/backlog/网卡 ethtool -S）
  全绿，但 AWR **TCP Retrans% / RTT 仍劣化**——丢点可能在中间网络（交换机
  出口队列丢弃主机侧完全看不见，因为丢弃发生在对端发送方向或交换机内部）
- 现场直接拿到交换机 logbuffer / `display interface brief` / `display arp`
  要判读
- 交换机侧报"端口丢弃/拥塞"类告警，DBA 被拉去对账（本件 2026-11-17 S12708
  实拍即此场景：Slot 6/7 多 GE 端口出方向丢弃告警）

## display interface brief 判读

列序（VRP S 系列）：`Interface | PHY | Protocol | InUti | OutUti | inErrors | outErrors`

**PHY/Protocol 四组合**：

| PHY | Protocol | 含义 | 判读 |
|---|---|---|---|
| up | up | 有载波+协议正常 | 正常 |
| up | down | 端口有链路但 VLANIF 无成员口在 up/无 IP 语义 | VLANIF 类正常（空 VLAN） |
| down | down | 无链路（shutdown/无插线/对端关） | **需确认是否有预期**——预期在用的口 down=事件 |
| *down | up | 不出现（协议依赖物理） | — |

**利用率列**：InUti/OutUti 是采样窗内峰值利用率（按端口速率归一）。注意
**低利用率 ≠ 无丢弃**——利用率是按采样窗平均/峰值渲染的，微突发（μs~ms 级
瞬时超过出口缓冲）在 1 分钟粒度里只有几个百分点，但队列丢弃照丢。**利用率
与丢弃计数必须分开看，低利用率+有丢弃=微突发或 QoS 整形限速的特征形态**。

**错误计数列（inErrors/outErrors）**：非零只说明"有过"，**不看类型与趋势
不能定责**。下一步：

```
display interface XGigabitEthernet7/0/5 | include error
```

区分 CRC（物理/光模块/线缆）vs runts/giants（MTU/双工协商）vs帧校验错；
单次个位数（如实拍的 4 和 1）且不增长=历史残留，观察即可；持续增长才立案。

## ARP 汇总行判读

`display arp` 尾行 `Total / Dynamic / Static / Interface`：

- Dynamic 占多数、Static=0 是常态（实拍 718/628/0/90）
- Interface ARP（网关自身接口地址）数量≈三层接口数，正常
- Expire(M) 在 0~20 分钟滚动=表项正常老化刷新；** Expire 长期满值（20min
  顶格）不刷新=对端已失联但表项未到期，正常等待过期即可**
- 类型 `D-0`：D=Dynamic，-0 非标准字段位，不单独判读
- ARP 需要警惕的形态（本案未见）：同一 IP 频繁漂移 MAC（=IP 冲突或 VRRP
  主备抖动）、Total 里 Dynamic 远大于接入终端数（=扫描/泛洪）

## logbuffer 日志族判读（本件判据核心）

实拍样本：S12708-2，2026-11-17 15:48~16:09，Slot 6/7 多端口。三类日志：

### IFPDT/4/PKT_OUTDISCARD_ABN —— 出方向丢弃超阈值（异常通报）

```
IFPDT/4/PKT_OUTDISCARD_ABN(l):[Slot ID]:Slot=6,Interface output discard
  exceeded the log threshold.(Interface=GigabitEthernet6/0/12,Statistics=...,
  PortQueuePassed=511295401,PortQueueDropped=0,LogThreshold=300,
  LogInterval=300(s),PortQueueNumber=...)
```

**触发条件**：`LogInterval`（300s 统计窗）内端口出方向丢弃数超过
`LogThreshold`（300）→ 每个窗发一条。字段判读：

| 字段 | 含义 | 判据 |
|---|---|---|
| PortQueuePassed | 窗内该端口通过队列转发的包数 | 分母 |
| PortQueueDropped | 窗内队列丢弃数 | 与 Statistics 对账；=0 而 ABN 仍发=丢弃在**其他队列**（PortQueueNumber 区分），逐队列账不平 |
| Statistics | 窗口统计的丢弃计数（判读口径以设备实现为准，推断） | 超 300 即告警 |
| LogInterval/LogThreshold | 300s / 300 | 即阈值=平均 1 丢/s 持续 5 分钟才告警，**脉冲式单窗几百个丢弃不触发**——看到 ABN 说明丢弃是持续分钟级的 |

### IFPDT/4/PKT_OUTDISCARD —— 丢弃回落阈值下（恢复通报，勿当新故障）

同字段、文案为 `restored to the log threshold`。语义=上一窗超阈值、本窗
回落。**ABN 与恢复通报成对交替出现 = 间歇性拥塞**（实拍 16:01~16:07 六分钟
内三对交替），不是持续压满——这种形态查"什么业务在突发"，不是查"带宽满了"。

### OAMDL/3/DISCARD_PKT —— 拥塞丢包（设备内部通道级）

```
OAMDL/3/DISCARD_PKT(l):[Slot:6],Packets are discarded for congestion.
  (SlotID=6,UnitID=0,PortID=...,PeerSlotID=...,DiscardNumber=...)
```

带 PeerSlotID → 发生在**框内跨板/内部通道**（OAM 链路层检测到对板方向
拥塞丢弃）。框式交换机背板/跨线卡通道拥塞时出现；与端口级 OUTDISCARD
同时段出现=该线卡出方向整体有压力（实拍：Slot 6 两者同时段齐发）。

### 判读优先级

1. 先分**哪个 Slot/端口**——对照业务端口规划表（哪台 DB/应用挂哪个口）
2. ABN+恢复交替 = 间歇拥塞/微突发；持续 ABN 不恢复 = 持续超订
3. OAMDL 与 OUTDISCARD 同发 = 线卡级压力；只有 OAMDL = 内部通道（与端口
   接线无关，查跨板流量分布）
4. **镜像/观测端口豁免**（2026-11-17 用户令：镜像口另说）——观测口把镜像
   流量再发出，出口速率天然高于接入业务口，其丢弃/拥塞告警只反映镜像管道
   容量，不代表业务受损；判读前先剔除镜像口

## root cause 链（为什么低利用率也丢）

```
出方向丢弃
 ├─ 真拥塞：下游总带宽 < 上游汇聚流量（超订）→ 利用率也会高 → 持续 ABN
 ├─ 微突发：均值低但 μs 级突发超过出口缓冲 → 利用率低 + 间歇 ABN ← 实拍形态
 ├─ QoS 整形/CAR 限速：配置了 shaping/car 的口，超限即丢，利用率被限在
 │   阈值下 → 利用率永远低 + 持续丢
 └─ 队列调度配置：低优先级队列在拥塞时被饿死（PortQueueNumber 维度看）
```

机制层为通用交换架构定论；VRP 具体缓冲划分/队列数（每口 8 COS 队列）未
实机核实，标注**推断级**。

**与库侧的因果闭环**：交换机出口丢弃 → 下游/上游主机看到 TCP 丢段 →
主机侧 Retrans%↑、RTT 抖动（`network.md` RTT 估计器与 Mathis 上限）→
DB 侧表现为主备复制 Net 段延迟（`../ha/replication-lag.md`）、应用侧
语句毛刺。**主机永远看不见交换机丢的这一段，所以交换机丢弃计数是主机
Retrans 归因链上"丢在中间网络"的唯一直接证据**。

## 处置矩阵

| 判定 | 动作 |
|---|---|
| 确认丢弃类型与队列 | `display qos queue statistics interface <口>` 逐队列看丢在哪个 COS 队列 |
| 确认丢弃计数趋势 | `display interface <口>` 的 output discard 列，隔窗采样两次比增量 |
| 微突发定位 | 有 burst 检测特性的机型开微突发检测；无则错峰业务/换聚合链路 |
| QoS 嫌疑 | `display qos configuration interface <口>` 看有无 shaping/car |
| 端口错误计数增长 | CRC 增长→换光模块/线缆/清洁光纤；错包类型按 verbose 输出分诊 |
| 涉及 DB 流量的口 | 与主机侧联动验证：vb_tool AWR 同窗 Retrans%/RTT、复制 lag Net 段——**交换机丢弃窗与主机 Retrans 窗时间对齐=归因闭合**（同分钟横切铁律，`../performance/metrics.md`） |
| ABN 持续不恢复 | 按真拥塞处理：扩链路/负载分担/流量调度，临时止损可先保关键队列 |

## 与库侧双向锚点

- `network.md` 丢包三道关：主机侧三层丢完才轮到本件；本件是其丢包分层链的
  最外一层（对端与中间网络分支的展开）
- `../ha/replication-lag.md`：Net 段大的中间网络可能根因即本件的出口丢弃
- `../performance/check-inventory.md` 网络丢包分层链"对端与中间网络劣化"
  分支：人工链走到交换机的落地件
- `../performance/awr-report-analysis.md` 网络闸（drop/Retrans%/RTT）劣化
  时，交换机侧对账是证据链第三站（主机→对端→中间）

## 版本注记

- 设备：华为 S12708（框式，VRP），2026-11-17 实拍 logbuffer + interface
  brief + arp；`display qos queue statistics`/微突发检测等命令名为 VRP S
  系列通用语法，**具体机型支持矩阵未逐一核实（推断级）**
- 机制层（出口队列/背板拥塞）为通用交换架构；VRP 源码不可得，字段级语义
  中 `Statistics` 与 `PortQueueDropped` 的确切记账差异未定论

## 来源

- 实拍样本：S12708-2 logbuffer / interface brief / arp（2026-11-17，
  用户微信图片两张，本件所有日志族字段文案、阈值 300/300s、Slot 6/7 事实
  均出自该样本）
- `镜像口另说` 豁免口径：2026-11-17 用户令
