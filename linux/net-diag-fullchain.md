# 网络全链路诊断（网卡 / 线缆光模块 / 交换机 / 主动探测 / 抓包取证）

## 概述

> 服务分类：linux（网络域的硬件与取证层）、performance（Retrans/RTT 劣化
> 定责）、troubleshooting（丢包现场逐段排查）。主机内核栈判读在 `network.md`
> （丢包三道关/两级队列/重传/conntrack），交换机 logbuffer 在
> `switch-side-logs.md`——本文不复制，只补它们之间与之外的四段：**主动探测、
> 网卡硬件、物理层（线缆/光模块）、抓包取证**，并给端到端分诊 SOP。
> **源码可得性声明**：网卡内核接口（ethtool 命令面 net/core/ethtool.c、计数器
> 语义）有内核基线可锚；**各驱动计数器名字逐驱动不同**（igb/ixgbe/virtio/
> hnvs 各一套）；光模块 DOM 遵循 SFF-8472/8636 框架（模块 EEPROM 自带告警
> 阈值）；**VRP 交换机命令与华为手册未入域**——交换机侧命令名为通用 VRP 语法
+ 推断级，待指定材料升级定论。

## 入口路由（触发条件）

- 主机内核栈已排查完（`network.md` 三道关全绿）但 AWR **Retrans%>3 / RTT
  劣化 / 丢包**仍在 → 本文全链路 SOP 逐段定责
- 换线缆/换光模块/换端口/调整交换机后的**验证**（改前改后计数器对账）
- 新装机/搬迁后的链路质量验收（打流+双端对账）
- 现场问"网络到底丢在哪一段"（要分段证据，不要拍脑袋）

## 全链路分层图（判读顺序=信号流向）

```
[对端主机]
   │ ⑦对端网卡计数器（同①，在对端做）
[线缆/光模块]  ⑥DOM 光功率/温度/告警 → CRC/LOS 指纹
[交换机端口]   ⑤display interface 计数器族 + qos 队列 → switch-side-logs
   │
[本端线缆/光模块] ④DOM 对端互读对账
[本端网卡]     ③ethtool -S 五族计数器 / -g ring / -k offload / -c 中断
   │ ②驱动/内核栈（network.md 三道关，本文不重复）
[本端内核]     ①softnet/backlog/ListenDrops/conntrack
[应用/DB]      ⓪主动探测先行：ping/mtr/iperf3 定"丢/慢/抖"哪个性质
```

## ⓪ 主动探测层（先定性，再进分段）

| 工具 | 判据 | 陷阱 |
|---|---|---|
| `ping -i 0.2 -c 100 <对端>` | 丢包率/时延均值与 max-min 抖动；DB 服务器间 RTT 局域网应 <1ms | ICMP 低优先级，**拥塞时先丢 ICMP**——ping 丢≠业务丢，反向不成立（业务丢时 ping 常常不丢，微突发 ms 级 ping 采样率太低） |
| `mtr -rwzbc 100 <对端>` | 末端 loss 最准；**中间跳 Loss%>0 但末端=0 = ICMP 限速假象**，不定责 | 只看末端行定责；中间跳仅用来定位"从第几跳开始劣化" |
| `iperf3 -c <对端> -t 30 -P 4` | 打流后对端 `iperf3 -s` 侧 Retrans 计数；带宽×重传=链路质量直接证据 | **打流会挤占业务带宽**（万兆口跑满影响 DB 流量），生产窗口做、单流小包模式更接近 DB 负载（`-l 8K`） |

**判读纪律**：ping 只能证"抖/延迟"，证不了微突发丢包；iperf3 打流才能压出
队列丢弃（等于把微突发变成持续突发）。三者都是**快照级证据**，定责必须
与计数器/抓包互证。

**★nc/ping 现场取证的四条局限（2026-11-19 实战件沉淀——"5 分钟全绿"
证明不了网络没问题）**：

1. **窗口覆盖**：间歇性故障（分钟~小时级周期）的取证窗必须覆盖**多个故障
   周期**（≥24h 落盘）才能否证；5 分钟全绿只等于"这 5 分钟没断"。
2. **nc 默认用法只验"新建连接"，验不到"存量连接被杀"**：每条 Connected
   后 0 字节立即退出——而 idle 掐断/中间设备会话复位/conntrack 老化只杀
   **存量/空闲**连接，新建永远成功（`net-midpath.md` idle 掐断指纹正是
   "新建全绿+长连接周期性断"）。要测断连须长连接模式：
   ```bash
   while :; do date '+%F %T'; timeout 300 nc <对端> <端口> </dev/null \
     || echo "FAIL rc=$?"; sleep 1; done >> /tmp/nc_probe.log &
   # ping 同步提密度+丢包打点落盘：
   ping -i 0.2 -O <对端> | while read l; do echo "$(date '+%F %T') $l"; done \
     >> /tmp/ping_probe.log &
   ```
3. **ping 密度**：默认 ~1s 一探，3-4s 闪断窗内只有 3~4 个采样点且总量仅
   几百包——漏检概率高；`-i 0.2` + `-O`（丢包即打点带时刻）是取证最低配。
4. **同窗对账物必须配套**：无交换机日志/无主机 Retrans·drop 计数器/无库侧
   断联日志时间戳对齐的"全绿"是孤证——定责要求与
   `switch-side-logs.md` SOP-1 的 trapbuffer 对时联动。

## ③ 网卡硬件层（ethtool 判读）

### 计数器五族分类学（`ethtool -S <口>`）

**名字逐驱动不同**，按语义归五族（括号=常见名字，仅举例非全集）：

| 族 | 语义 | 定责方向 |
|---|---|---|
| ①缺缓冲/未及时取 | rx_missed_errors、rx_no_buffer、rx_fifo_errors | **本机取包慢或 ring 小**：加 ring（-G）、中断亲和到业务核、多队列；与 softnet dropped 联判 |
| ②CRC/物理错 | rx_crc_errors、rx_pcs_errors | **物理层**：线缆/光模块/端口/对端——进 ④ DOM；CRC 持续增长=换件 |
| ③长度/帧错 | rx_length_errors、runt、giant、rx_align_errors | MTU 不一致 / 双工协商错 / 坏帧源 |
| ④冲突/双工 | collisions、late_collisions | **半双工误协商指纹**（全双工链路不该有冲突）——查 Speed/Duplex |
| ⑤软件丢弃 | rx_dropped（非 ring 满） | 协议栈原因（`network.md` 三道关）或无接收者（组播未订阅类） |

注意 `/proc/net/dev` 的 drop 列把 rx_missed 合并渲染，分不清族——**逐族定责
必须用 ethtool -S**（此口径已在 `../performance/metrics.md` 立账）。

### 网卡配置四查

```
ethtool -i <口>   # 驱动/firmware 版本——固件 bug 是"莫名 CRC/掉链路"的排查位；换件前先对版本
ethtool <口>      # Speed/Duplex/Link——降速(100M)/half=协商错；Link partner 行看对端宣告
ethtool -g <口>   # ring 预设/最大——Ring max 远大于 preset 时可 -G 加大（缺缓冲族①的旋钮）
ethtool -k <口>   # offload：GRO/LRO/TSO/checksum——判 MTU/分片问题；
                  # tcpdump 抓到 >MTU 超大包=offload 渲染伪象（抓包层陷阱，非真实）
ethtool -l/-L <口> # 队列数（channels）；/proc/interrupts 看 IRQ 是否散开
```

**中断亲和**：`cat /proc/interrupts | grep <驱动名>`——所有网卡 IRQ 落在同一
CPU=单核软中断瓶颈（`cpu.md` %sirq 单核打满形态）；旋钮= irqbalance 或手工
affinity 铺开 + RSS 队列数（-L）。

### 五族分类学落到具体驱动（v7.1-rc4 行级精读，2026-11-17）

| 驱动 | 缺缓冲族① | CRC/物理族② | 长度/帧族③ | 备注 |
|---|---|---|---|---|
| igb | rx_no_buffer_count / rx_missed_errors | rx_crc_errors | rx_short/long_length_errors / rx_align_errors | 双工指纹=tx_carrier_errors |
| ixgbe | rx_no_buffer_count / rx_missed_errors / **alloc_rx_buff_failed** | rx_crc_errors | rx_short/long_length_errors / rx_fifo_errors | rx_dropped/tx_dropped=族⑤ |
| i40e | **port.rx_discards** / rx_alloc_fail | port.rx_crc_errors | port.rx_undersize/oversize/jabber / rx_length_errors | port.tx_dropped_link_down=链路 down 丢（链路对账位） |
| virtio_net | 每队列 **drops**（virtnet_rq_stats_desc，virtio_net.c:159-160） | — | — | 无 CRC 族；xdp_drops/XDP 相关 |
| vmxnet3 | 驱动级仅 "tx timeout count"（vmxnet3_global_stats）；其余并入标准族 get_stats64 | — | — | 判读主要看标准族（/proc/net/dev 口径） |

锚文件全部在镜像 `/home/xiang/kernel-src/linux-7.1-rc4-mirror/` 对应
`*_ethtool.c`（igb :igb_gstrings_stats、ixgbe/i40e 同名表、
vmxnet3_ethtool.c）。**用法**：现场 `ethtool -S` 输出先按本表归族，
归不进五族的按驱动源码表对账。

### 三个源码闭环锚（同为 2026-11-17 精读）

- **GSO 抓包伪象闭环**：抓包 tap 交付点 net/core/dev.c:2548
  `dev_queue_xmit_nit`（调用点 :3884）在 **:4019 `validate_xmit_skb` →
  :4036 `netif_needs_gso` 分段之前**——tcpdump 挂在协议栈上游，GSO 超大
  包是"分段发生前"的真实视图，不是线上物理包（源码证实伪象成因）
- **PMTUD 黑洞闭环**：DF 位检查 include/net/ip.h:438 `ip_dont_fragment`
  / ip_output.c:581；ICMP Frag Needed 的消费点 net/ipv4/tcp_ipv4.c:577
  `tcp_v4_err`（RFC1191，:366 注释）——**中间设备吞掉该 ICMP 且
  tcp_mtu_probing=0（默认）时发送端永远收不到 MTU 更新**，DF 大包无限
  重传卡死，即"大包断小包通"的源码根
- **DNS 首连 5s 指纹闭环**（glibc 精读，镜像 glibc-mirror/）：
  resolv/resolv.h:68 `RES_TIMEOUT=5` / :72 `RES_DFLRETRY=2`；默认值应用
  res_init.c:194-195（`template.retrans = RES_TIMEOUT`）；resolv.conf
  `timeout:/attempts:` 解析与上限（30s/5 次）res_init.c:663-676——第一个
  nameserver 不可达时对该 NS 的查询等满 5s 才轮下一个，首连 5~10s 延迟
  即此（5s×不可达 NS 数）

**PCIe 总线层（网卡之下的排查位）**：网卡无故 reset/链路 flap 而驱动与
物理层全绿时查总线——`dmesg | grep -iE "pcie|aer"`（AER corrected/
uncorrected 错误）、`lspci -vvv -s <网卡BDF>` 的 CeSta/UESta 寄存器；
指纹=AER 计数与网络抖动时刻重合 → 插槽/riser/供电硬件问题（换槽位），
不是网络配置问题。中间层（STP/聚合/网关/VRRP/中间设备/虚拟化）判读在
`net-midpath.md`。

**双端对时（取证的前置条件）**：双端计数器对账/抓包 SEQ 对账/AWR 同分钟
横切全部依赖对时——`chronyc tracking`（NTP 偏移应在 ms 级）；**NTP 跳变/
未同步时段的时间对账结论作废**，跨主机比对日志前先核这一步。

## ④ 物理层：线缆 / 光模块（DOM 判读）

**DOM/DDM**（SFF-8472/8636：模块 EEPROM 自带数字诊断与告警阈值）：

```
ethtool -m <口>                      # Linux 侧读 DOM
display transceiver diagnosis interface <口>   # VRP 侧读（推断级命令名）
display transceiver verbose interface <口>     # VRP 侧模块型号/序列号
```

**判读优先级**：①**模块 EEPROM 自带 Alarm/Warning 阈值优先**——ethtool -m
输出即含 High/Low Alarm 框，flag 置位（Rx power low alarm 等）= 硬判据；
②通用经验锚做旁证（**推断级，以模块自带阈值为准**）：接收功率比链路对端
Tx 低得多=链路衰减大（尾纤脏/弯折/距离超型号）；温度持续偏高=模块老化/机框
散热；电压偏离标称=背板供电问题。

**对端互读对账（硬机制，双端各读一次 DOM）**：A 端 Tx power ≈ B 端 Rx power
（差值=链路衰减，正常 1~3dB 内）——不对称=读数侧衰减异常；两侧都正常但
CRC 涨=端口/模块接触，**换件对账法**：与备用口/备用模块互换，计数器跟不跟
着走是物理层定责的终审证据。

**故障指纹速查**：

| 指纹 | 定责 |
|---|---|
| CRC 增长 + DOM Rx 功率临界/告警 | 光衰/尾纤——清洁/换尾纤/换模块 |
| CRC 增长 + DOM 全正常 | 端口接触/模块本身/对端口——互换对账 |
| Rx LOS / Tx fault flag 置位 | 模块硬故障或未插好，直接换 |
| 链路 flap（up/down 抖）+ 温度高 | 模块老化/散热——先降环境再换件 |
| 双绞线口 Speed 降速/collisions | 线缆/水晶头/协商——查 -i Speed 与线序 |

## ⑤ 交换机端口层（判读入口在本文，展开在 switch-side-logs）

- logbuffer 丢弃/拥塞告警族、镜像口豁免 → `switch-side-logs.md`（不复制）
- 端口计数器族（VRP 推断级）：`display interface <口>` 关注 **CRC/错误帧/
  output discard** 三个数并与 Linux 侧 ethtool -S 对账——**同一条链路两端
  计数器对账是分段定责的通用手法**：本端 Rx CRC 涨+对端 Tx 无错=衰减在本端
  接收侧；对端 Tx 错+本端 Rx CRC 涨=同一物理问题两端各自记账（判 CRC 共性）
- `display qos queue statistics interface <口>` 逐队列丢弃 → 哪类业务
  （COS 映射）被丢

## ⑦ 抓包取证层（分段定责的终审手段）

### 双端 SEQ 对账法（丢在中间段的唯一直接证据）

同一条流**两端同时抓包**（NTP 对齐到 ms 级），对 TCP SEQ：
- A 端发出（ SYN/数据包带 SEQ=N ）而 B 端没抓到 → **丢在中间网络**——
  这是"丢在中间"的终审证据，计数器只能旁证
- 两端都抓到但应用没收到 → 丢在本端内核栈（回 `network.md` 三道关）
- 只在抓包工具里看到丢（tcpdump 报 packets dropped by kernel）→ **抓包机
  自身缓冲不足**，非网络丢（加大缓冲/-s 缩 snaplen 重抓）

### Wireshark/tshark 指纹

| 指纹 | 语义 |
|---|---|
| TCP Retransmission | 真丢包后的重传——SEQ 缺口定位丢的段 |
| DUP ACK（≥3 个） | 接收侧发现缺口要求重传（快速重传路径） |
| Lost Segment（ACK 指向未捕获 SEQ） | 抓包窗口断/丢——先排除抓包自身问题再定责 |
| ZeroWindow | 接收侧应用消费不动（对端慢，不是网络丢） |
| >MTU 超大帧 | GSO/TSO offload 渲染伪象（ethtool -k 已核），非真实线上包 |

### 生产抓包纪律

`-c` 限量、`-s 96` 缩 snaplen（只留头）、写 `-w` 文件离线分析（Wireshark
GUI 不直接挂生产口）；高流量口抓包本身引入延迟，DB 主库口抓包前先想
RTT 抖动代价；镜像口抓包要交换机配 observe 口（`switch-side-logs.md` 豁免
口径同理：镜像管道容量不代表业务受损）。

## 端到端分诊 SOP（从症状到定责）

```
症状：Retrans%↑ / RTT 抖 / 丢包 / 链路 flap
 1. 定性质：ping/mtr/iperf3（⓪）——丢 vs 抖 vs 带宽
 2. 主机内核栈：network.md 三道关 + softnet/backlog/conntrack ——绿了才往下
 3. 本端网卡：ethtool -S 五族——族①走配置旋钮；族②③④进物理层
 4. 本端 DOM：ethtool -m 阈值/告警 flag + 对端互读对账（④）
 5. 交换机：端口计数器+队列+logbuffer（⑤，switch-side-logs），剔除镜像口
 6. 对端镜像 3~4 步（在对端主机做）
 7. 仍定不了责：双端抓包 SEQ 对账（⑦）——中间段终审
 8. 换件验证：换线缆/模块/端口前后计数器清零对账（改前记底数！）
```

每步产出要么**定责到段**，要么明确"本段全绿、疑点在下一段"——禁止跳步
（特别是没查 3~4 就去动交换机配置）。

## 处置矩阵

| 判定 | 处置 |
|---|---|
| 缺缓冲族① | ethtool -G 加 ring、-L 加队列、IRQ 铺核；仍丢=CPU 慢，回 cpu.md |
| CRC+光衰 | 清洁/换尾纤/换模块，换后底数对账 |
| 半双工/降速 | 强制速率双工或查线缆线序 |
| 交换机队列丢 | switch-side-logs 处置矩阵（qos 统计/微突发/限速配置） |
| 双端 SEQ 实证中间丢 | 拿证据找网络组定责——本文链的终态出口 |
| 全链路全绿但业务仍抖 | 疑点回到主机/应用（os-triage 四域联判），别死磕网络 |

## 与库侧双向锚点

- `network.md` 三道关：本件 SOP 第 2 步的展开本体（内核栈判读不复制）
- `switch-side-logs.md`：第 5 步交换机 logbuffer/队列的展开本体
- `../ha/replication-lag.md`：Net 段大的全链路排查入口=本文 SOP
- `../performance/metrics.md`：Retrans%/RTT/rcv_rtt 口径与"丢包分层链"最外段的落地
- `../performance/check-inventory.md` 网络丢包分层人工链：RX Missed→errs→
  softnet→ListenDrops→conntrack 之后的"对端与中间网络"分支=本文

## 版本注记

- 内核命令面：ethtool 命令的内核实现在 **net/ethtool/ioctl.c**（v7.1-rc4
  精读实证：`net/core/ethtool.c` 在该版本**已不存在**，ioctl 分派迁至
  net/ethtool/ioctl.c——ring/coalesce/channels/flags 的 G/S case 锚：
  GRINGPARAM :3271 / SRINGPARAM :3380 / GCOALESCE :3270 / SCOALESCE :3105 /
  GCHANNELS :3295 / SCHANNELS :3485 / GFLAGS :3285 / SFLAGS :3408；
  文件级镜像 `/home/xiang/kernel-src/linux-7.1-rc4-mirror/`）；计数器
  **名字与渲染逐驱动不同**，五族分类法为跨驱动归纳，具体名字以现场
  `ethtool -S` 输出为准
- DOM：SFF-8472（SFP）/8636（QSFP+）框架，"模块 EEPROM 自带阈值优先"为
  框架定论；经验功率带为**推断级**
- VRP 命令名（display transceiver/qos queue statistics 等）：通用 VRP 语法
  **推断级**，待指定材料（华为手册/实机输出）升级；镜像口豁免=2026-11-17
  用户令
- mtr 中间跳 ICMP 限速假象、GSO 抓包伪象：通用工程定论（机制根：中间路由
  对 ICMP 生成限速 / GSO 在协议栈后段才分段，抓包点在其上游）

## 来源

- 内核基线：ethtool 接口层 net/ethtool/ioctl.c（v7.1-rc4，2026-11-17
  行级精读；文件级镜像 /home/xiang/kernel-src/linux-7.1-rc4-mirror/；
  各驱动计数器未逐驱动精读，以分类学定责）
- SFF-8472/8636 框架为光模块数字诊断的行业规范定论；未持有规范文本，阈值
  判读以模块 EEPROM 自带值为准（框架级引用）
- 交换机侧 2026-11-17 S12708 实拍（switch-side-logs.md 同源）
- 主动探测/抓包纪律为通用工程口径（无源码无手册，标注推断级）
