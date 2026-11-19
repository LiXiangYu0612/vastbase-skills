# 交换机侧排查（VRP / 华为 S 系列框式：日志判读 / 端口健康 / 表项 / SOP）

## 概述

> 服务分类：linux（网络域的最下游延伸——`network.md` 管主机侧收发路径，本文管
> 主机网卡之外那一段）、performance（网络丢包分层链的最外一层）、
> troubleshooting（丢包/重传/断连排查走到"查交换机"这一步时的操作件）。
> **范围约束**：VRP 内核源码不可得。本文内容分三档明确标注：
> **【实锚】**=2026-11-17 S12708 实拍字段/数值；**【工程口径】**=VRP 通用命令面
> 与字段语义（命令名随机型/版本可能有差异，首次使用前 `display xxx ?` 现场核对，
> 实拍后逐条升锚）；**【推断级】**=通用交换架构推演。
>
> 2026-11-19 扩充（用户令"写交换机各种问题的排查+日志分析"）：日志族从实拍
> 三族扩到全量族表、新增端口健康/表项/性能容量层与四条场景 SOP；扩充部分以
> 【工程口径】为主，待实拍升锚。

## 入口路由（触发条件，四选一即进本件）

- 主机侧已排查完：`network.md` 丢包三道关（softnet/backlog/网卡 ethtool -S）
  全绿，但 AWR **TCP Retrans% / RTT 仍劣化**——丢点可能在中间网络（交换机
  出口队列丢弃主机侧完全看不见，因为丢弃发生在对端发送方向或交换机内部）
- 现场直接拿到交换机 logbuffer / trapbuffer / `display interface brief` /
  `display arp` 要判读
- 交换机侧报"端口丢弃/拥塞"类告警，DBA 被拉去对账（本件 2026-11-17 S12708
  实拍即此场景：Slot 6/7 多 GE 端口出方向丢弃告警）
- **库侧同秒批量断连/闪断案**：要把"交换机在案发时刻做了什么"拉出来定责
  （SOP-1）

## §1 信息中心与日志管道【工程口径】

- **logbuffer**（`display logbuffer` / `display logbuffer reverse`）：系统日志，
  含 debug→emerg 各级；默认容量有限（盒式典型 512 条），**只留最近**——案发后
  尽快取，晚了被冲掉。
- **trapbuffer**（`display trapbuffer` / `display trapbuffer reverse`）：SNMP
  trap 类告警缓冲，端口 UPDOWN/ERROR-DOWN/STP 拓扑变化这类**事件型告警必进**
  trap——定责对时首选 trapbuffer，logbuffer 做交叉。
- **等级**：0 emergency ~ 7 debug；判读优先抓 0~4 级。
- **时间戳与对时**：日志时间戳用设备自身时钟（`display clock`）——**跨设备
  定责前必须先对表**：交换机 vs DB 节点 `date` 差几分钟，日志对齐就全废。
  NTP 未同步的设备日志只能做相对时序，不能做绝对对时。
- **一键全量**：`display diagnostic-information`（日志+告警+接口+CPU/内存+
  配置打包）——适合远程让网络组一次导出，本地再慢慢筛。

## §2 取证包（十连，按序执行）【工程口径】

```bash
display clock                                  # 0 对时（一切对齐的前提）
display trapbuffer reverse                     # 1 事件型告警（倒序=最新在前）
display logbuffer reverse                      # 2 系统日志交叉
display error-down recovery                    # 3 error-down 端口/原因/恢复倒计时
display interface brief | include down         # 4 当前 down 的口（含 ERROR-DOWN）
display mac-address flapping record            # 5 MAC 漂移记录（时间+VLAN+MAC+原口→新口）
display eth-trunk                              # 6 聚合口状态/成员 Unselect 原因
display cpu-usage                              # 7 控制面压力
display device                                 # 8 板卡在位/状态
display current-configuration | include error-down|flap|storm   # 9 保护性配置面
# 10 涉事端口细查（替换 <port>）：
display interface <port>                       # 错包/速率/双工/丢弃全量
display transceiver interface <port> verbose   # DOM 光功率（收发+温度+电压）
display qos queue statistics interface <port>  # 逐队列丢弃
```

## §3 日志族全表（按族判读）

实拍三族（IFPDT/OAMDL 族）为【实锚】核心，其余族为【工程口径】。

### 3.1 IFPDT/4/PKT_OUTDISCARD_ABN —— 出方向丢弃超阈值【实锚】

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

### 3.2 IFPDT/4/PKT_OUTDISCARD —— 丢弃回落阈值下【实锚】

同字段、文案为 `restored to the log threshold`。语义=上一窗超阈值、本窗
回落。**ABN 与恢复通报成对交替出现 = 间歇性拥塞**（实拍 16:01~16:07 六分钟
内三对交替），不是持续压满——这种形态查"什么业务在突发"，不是查"带宽满了"。

### 3.3 OAMDL/3/DISCARD_PKT —— 拥塞丢包（设备内部通道级）【实锚】

```
OAMDL/3/DISCARD_PKT(l):[Slot:6],Packets are discarded for congestion.
  (SlotID=6,UnitID=0,PortID=...,PeerSlotID=...,DiscardNumber=...)
```

带 PeerSlotID → 发生在**框内跨板/内部通道**（OAM 链路层检测到对板方向
拥塞丢弃）。框式交换机背板/跨线卡通道拥塞时出现；与端口级 OUTDISCARD
同时段出现=该线卡出方向整体有压力（实拍：Slot 6 两者同时段齐发）。

### 3.4 链路翻动族（LINK UPDOWN / LINEPROTO）【工程口径】

trap/log 文案形态 `IFNET/.../LINK_UPDOWN`、`LINEPROTO/5/UPDOWN`（接口/协议
层起落各一条）。判读：
- **单次 updown**=事件；**分钟级反复成对**=link-flap（光衰临界/线缆/模块/
  对端网卡重启），是 ERROR-DOWN(link-flap) 的前奏；
- 与 DOM 光功率联动看（§4.2）：收光贴灵敏度下限的口，翻动时刻常伴温度/光
  功率读数抖动。

### 3.5 ERROR-DOWN 族（端口被保护机制打死）【工程口径】

`display error-down recovery` 三列：接口 / 原因码 / 自动恢复剩余秒。原因码族：

| 原因码 | 触发机制 | 排查落点 |
|---|---|---|
| link-flap | 检测周期内翻动次数超阈值 | DOM 光功率/线缆/对端；§3.4 翻动记录 |
| storm-control | 广播/组播/未知单播超风暴阈值 | `display storm-control interface`；定位源头主机 |
| error-limit | CRC/错包超限 | §4.1 错包分诊（物理层问题） |
| port-security | 学到非法 MAC/超限 | `display port-security all`（trap 文本内嵌 MAC） |
| bpdu-protection | 边缘口收到 BPDU | 查是谁在发 BPDU（环路私接交换机） |
| loop-detect | 环路检测命中 | `display loop-detect`（端口/VLAN） |

**两个关键配置面**（`display current-configuration | include error-down`）：
1. `error-down auto-recovery cause <族> interval <秒>`——**没配则需人工
   `undo shutdown` 恢复**，断流时长由响应速度决定（生产上必须配自动恢复）；
2. 各保护阈值（flap 次数/周期、storm 速率）。
**业务影响**：端口一旦 ERROR-DOWN，其上所有流量（含 DB 复制流）即刻中断
——闪断案的第一嫌疑人。

### 3.6 STP/MSTP 拓扑变化族【工程口径】

logbuffer 中 MSTP 拓扑变化/根桥变化/port 角色翻转类日志 + trap。判读：
- 整网 TC（topology change）风暴：MAC 表被频繁刷老化 → 全网单播泛洪 →
  CPU/带宽双抬；**TC 频发与 MAC 漂移互为因果**（§5.1）；
- 根桥变化（root change）=二层拓扑抖动的大事件，全网端口重算，秒级闪断
  的候选根源；
- 命令：`display stp brief`（端口角色/状态）、`display stp root`（根桥
  归属——**根桥漂移到非预期设备=私接/优先级配置事故**）。

### 3.7 MAC 漂移族【工程口径】

`display mac-address flapping record`：时间 + VLAN + MAC + 原端口 → 新端口。
**二层环路的最强指纹**（同一 MAC 在两个口反复横跳）；也是"哪台机器换了口/
哪段网络在翻动"的定位器。与 §3.6 TC、风暴控制触发三证合一=环路立案。

### 3.8 eth-trunk/LACP 族【工程口径】

成员口 Unselect/Select 日志 + `display eth-trunk`：
- 成员掉线（物理/对端）→ 流量重哈希到剩余成员——**已建 TCP 不受影响**
  （重哈希只影响新流？否——ECMP/LAG 哈希按流，成员减少导致部分流换路径，
  转发层面通常无中断；但**成员翻动期间该链路承载的流会瞬时丢包**）；
- `display eth-trunk` 的 Unselect 原因列（ERROR_DOWN/对端 LACP 超时等）。

### 3.9 堆叠/CSS 与 VRRP 族【工程口径】

- 堆叠（iStack）/CSS（框式集群）：主备切换日志（`display css status` /
  `display switchover state` 按机型）——主控切换=秒级全机事件，与
  "同秒所有连接断"形态强相关；
- VRRP：Master 迁移（`display vrrp verbose`）=网关漂移，ARP 重新解析
  窗口内主机到网关流量瞬断。

### 3.10 环境/硬件族【工程口径】

`display device`（板卡 Fault/Offline）、`display environment`（温度）：
过热降频/板卡故障均可造成瞬断或错包；框式另看电源/风扇。错包型问题
（error-limit 族）排查时环境项一并过。

### §3 判读总优先级【实锚+工程口径】

1. 先分**哪个 Slot/端口**——对照业务端口规划表（哪台 DB/应用挂哪个口）
2. ABN+恢复交替 = 间歇拥塞/微突发；持续 ABN 不恢复 = 持续超订
3. OAMDL 与 OUTDISCARD 同发 = 线卡级压力；只有 OAMDL = 内部通道（与端口
   接线无关，查跨板流量分布）
4. **ERROR-DOWN/翻动族优先于丢弃族**：丢弃=性能问题，ERROR-DOWN=可用性
   问题，后者才解释"断"
5. **镜像/观测端口豁免**（2026-11-17 用户令：镜像口另说）——观测口把镜像
   流量再发出，出口速率天然高于接入业务口，其丢弃/拥塞告警只反映镜像管道
   容量，不代表业务受损；判读前先剔除镜像口

## §4 端口健康层

### 4.0 接口带宽决定链（2026-11-19 用户问，工程口径）

```
最终速率 = 硬件口规格 ∩ 光模块/线缆规格 ∩ 对端能力 ——自协商取交集
           ↑ 再被管理配置覆盖（speed 强制/eth-trunk）
```

| 层 | 决定者 | 要点 |
|---|---|---|
| ① 硬件上限 | 线卡/交换芯片（框式由线卡型号定死） | GE/10GE/25GE 物理规格 |
| ② 光模块/线缆 | 插入的模块型号 | **光口速率第一决定者**：10G 口插 1G 模块只能 1G；DAC/AOC/单多模限模块与距离 |
| ③ 自协商 | 双端能力集交集 | 10G端+1G端→1G；**强制/自协商不匹配→速率双工错位→CRC/runts/giants**（error-limit 族入口） |
| ④ 管理配置 | speed/duplex/negotiation | 强制 speed 覆盖硬件能力；eth-trunk 总带宽=活跃成员数×各自协商速率（可异速） |
| ⑤ 框式超订 | 线卡↔背板带宽比 | 48×GE 卡配 2×10G 上背板=8:1 超订——单口满速没问题，全口并发受限（§6 微突发温床） |

查询：`display interface <口>`（Speed/Duplex/Negotiation 协商终态）、
`display transceiver <口> verbose`（模块标称，对②）、接口视图 `display this`
（有无强制，对④）。

★**对账坑**：`display interface brief` 的 InUti/OutUti 按**当前协商速率**归一
——GE 被降速协商到 100M 时跑 90M 显示 90%（不是 9%）。与主机侧流量对账前
**必看 Speed 列**；降速排查=②模块规格/③双端能力/④强制配置三查。

### 4.1 display interface 错包分诊【工程口径+实锚摘要】

非零错误**只说明"有过"，类型+趋势才能定责**：

| 计数 | 方向 | 含义 | 指向 |
|---|---|---|---|
| CRC | 收 | 帧校验错 | 物理层：光模块/光纤/接头/对端发送 |
| runts/giants | 收 | <64B / >MTU 帧 | 双工不匹配/MTU 配置/网卡异常 |
| aligns/jabbers | 收 | 对齐错/超长错帧 | 物理层 |
| output discard | 发 | 出队丢弃 | 拥塞/QoS/微突发（§3.1-3.3 + 处置矩阵） |

单次个位数且不增长=历史残留；**隔窗双采样看增量**才是立案依据。
`reset counters interface <port>` 清零重测（取证完成后再清，清了历史就没了）。

### 4.2 DOM 光功率（收发光功率对端互读）【工程口径】

`display transceiver interface <port> verbose`：Tx/Rx 光功率、温度、电压、
偏置电流。判读：
- **Rx 接近模块灵敏度下限**（规格内标）=光衰临界——link-flap 的头号根因；
- 对端读 Tx、本端读 Rx 互相对账：差值异常=链路（光纤/接头）损耗；
- 温度告警段与光功率同步抖动=模块老化。与 `net-diag-fullchain.md` DOM 节
  同一套方法（主机侧网卡 DOM 与交换机侧 DOM 互为对端证据）。

## §5 表项层

### 5.1 MAC 地址表【工程口径】

`display mac-address`（按 VLAN/端口过滤）；`display mac-address flapping
record`（§3.7）。警惕形态：表项规模逼近规格上限（学习爆炸）、同 MAC 多口
漂移、黑洞 MAC（安全策略命中）。

### 5.2 STP 拓扑【工程口径】

`display stp brief` / `display stp root`（§3.6）。DBA 视角只判两件事：
根桥是否在预期设备、自己业务口的角色（Desigenated/Forwarding）是否稳定。

## §6 性能与容量层【工程口径】

- **CPU**（`display cpu-usage`）：TC 风暴/广播风暴/攻击面会抬控制面 CPU；
  CPU 高**不一定**影响转发（框式转发在 ASIC），但 STP/协议报文处理延迟
  会放大拓扑抖动。
- **风暴控制**（`display storm-control interface`）：配置阈值+动作
  （trap/error-down）——与 §3.5 storm-control 原因码闭环。
- **队列丢弃**（`display qos queue statistics interface`）：丢在哪个 COS
  队列 → QoS 配置/微突发定位（处置矩阵）。

## §7 场景 SOP（四条，每条串 §1-§6）

### SOP-1 闪断案（库侧 3-4s 断联，本案型）

```
0 对时：display clock vs 库侧 date（差>1min 先修对齐）
1 display trapbuffer reverse → 案发时刻窗（如 18:50:3x）内逐条筛：
   有 ERROR-DOWN/翻动/STP/堆叠/VRRP 事件？
   ├─ 有 ERROR-DOWN：display error-down recovery 拉原因码
   │   ├─ link-flap → §4.2 DOM + §3.4 翻动记录（光衰临界线）
   │   ├─ error-limit → §4.1 错包（物理层）
   │   └─ storm/loop/bpdu → §6/§5.1 环路与风暴线
   ├─ 只有 LINK-UPDOWN 翻动未到 error-down → 光衰临界/线缆抖动
   └─ 完全无事件 → 交换机出罪：转中间设备四形态判读
       （`net-midpath.md` 杀连接四形态——注意 L2 交换机不产 RST）
2 结论分流：交换机实锤（事件时刻=断联时刻）或出罪（找端点/中间设备）
```

### SOP-2 慢/丢包案（主机 Retrans 高、主机侧三道关全绿）

```
1 §2 取证包 3-7 + 涉事口 display interface（错包/丢弃增量）
2 OUTDISCARD_ABN/恢复交替（§3.1-3.2）→ display qos queue statistics 定队列
   → 微突发/QoS/真拥塞三分（root cause 链）
3 镜像口豁免先行剔除；与 vb_tool AWR 同窗 Retrans%/RTT 对时闭环
   （处置矩阵末行）
```

### SOP-3 断连风暴案（同秒大量连接断，reset/timeout 混发）

```
1 trapbuffer 案发秒窗：堆叠/CSS 切换？VRRP 迁移？STP 根桥变化？
2 无上述 → display mac-address flapping record（环路/翻动）
3 仍无 → 交换机出罪；按 net-midpath 四形态表对 RST/timeout 双签名分责
```

### SOP-4 环路案（泛洪/TC 风暴/MAC 漂移）

```
1 §3.7 MAC 漂移 record（MAC+两端口）+ §3.6 TC 频发 + §3.5 loop-detect
   命中——三证合一立案
2 风暴控制是否兜底（§6）；定位私接/误接线端口，按端口处置
```

## root cause 链（为什么低利用率也丢）【实锚+推断级】

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
| link-flap 命中 | §4.2 DOM 对端互读；光衰临界→换模块/清洁；确认 auto-recovery 已配 |
| ERROR-DOWN 无自恢复配置 | `error-down auto-recovery cause <族> interval <秒>` 补配（维护窗） |
| 涉及 DB 流量的口 | 与主机侧联动验证：vb_tool AWR 同窗 Retrans%/RTT、复制 lag Net 段——**交换机丢弃窗与主机 Retrans 窗时间对齐=归因闭合**（同分钟横切铁律，`../performance/metrics.md`） |
| ABN 持续不恢复 | 按真拥塞处理：扩链路/负载分担/流量调度，临时止损可先保关键队列 |

## 与库侧双向锚点

- `network.md` 丢包三道关：主机侧三层丢完才轮到本件；本件是其丢包分层链的
  最外一层（对端与中间网络分支的展开）
- `net-midpath.md`：SOP-1/SOP-3 出罪后的下一站——中间设备杀连接四形态
  （L2 交换机无 TCP 状态不产 RST，是本件 §3.5 出罪判据的机制背书）
- `net-diag-fullchain.md`：§4.2 DOM 对端互读与该件 DOM 节同一套方法；
  换件验证底数对账
- `../ha/replication-lag.md`：Net 段大的中间网络可能根因即本件的出口丢弃
- `../performance/check-inventory.md` 网络丢包分层链"对端与中间网络劣化"
  分支：人工链走到交换机的落地件
- `../performance/awr-report-analysis.md` 网络闸（drop/Retrans%/RTT）劣化
  时，交换机侧对账是证据链第三站（主机→对端→中间）

## 版本注记

- 设备：华为 S12708（框式，VRP），2026-11-17 实拍 logbuffer + interface
  brief + arp；`display qos queue statistics`/微突发检测等命令名为 VRP S
  系列通用语法，**具体机型支持矩阵未逐一核实（推断级）**
- 2026-11-19 扩充的 §1/§2/§3.4-§3.10/§4-§7 为【工程口径】（VRP 通用
  命令面），**未经本项目实拍**：首次现场使用时逐条 `display xxx ?` 核对
  命令存在性与输出列名，拿到实拍输出后按实锚纪律逐条升锚并回写本注记
- 机制层（出口队列/背板拥塞）为通用交换架构；VRP 源码不可得，字段级语义
  中 `Statistics` 与 `PortQueueDropped` 的确切记账差异未定论

## 来源

- 实拍样本：S12708-2 logbuffer / interface brief / arp（2026-11-17，
  用户微信图片两张，本文 §3.1-§3.3 所有日志族字段文案、阈值 300/300s、
  Slot 6/7 事实均出自该样本）
- `镜像口另说` 豁免口径：2026-11-17 用户令
- §1/§2/§3.4-§3.10/§4-§7：2026-11-19 用户令"写交换机各种问题的排查+
  日志分析"——按 VRP 通用命令面收录为【工程口径】，升锚方式见版本注记
