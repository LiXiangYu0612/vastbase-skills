# 中间层网络诊断（二层交换 / 三层路径 / 中间设备 / 虚拟化）

## 概述

> 服务分类：linux（网络域）、performance（抖动/断连定责）、
> troubleshooting（间歇断连/连接被掐现场）。主机内核栈（`network.md`）、
> 网卡/物理层/抓包（`net-diag-fullchain.md`）、交换机 logbuffer（
> `switch-side-logs.md`）之外，**主机↔对端之间还有四层**：交换机 L2 机制、
> L3 路径、中间设备（NAT/防火墙/LB/DNS）、虚拟化与云——间歇性、规律性
> 断连的根因大头在这四层，且**主机计数器全绿**，必须按本文判据排查。
> **源码可得性声明**：主机侧 bonding/rp_filter/veth 有内核路径锚
> （`<linux-7.1-rc4-src>` 树**未落地本地，行级精读待做**，本文标锚点级）；
> VRP 侧（STP/堆叠/Eth-Trunk 细节）为通用工程口径**推断级**，待华为手册
> 升级；中间设备（防火墙/LB）行为判据为现场工程定论+指纹法。

## 入口路由（触发条件）

- **间歇性断连/重连**，且时间上**有规律**（整点/固定分钟间隔）→ 中间设备
  idle 掐断（本文 §III）
- 双端主机计数器全绿、三道关全绿，业务仍抖 → 中间层逐段（本文）
- 主备网卡切换后**短暂断流**（1~3s）→ bonding MAC 表老化（§I.1）
- 聚合口带宽只有一半/单成员流量 → LACP 落单/hash 极化（§I.2）
- 网关层面抖动/双活 → VRRP/堆叠（§I.4/§I.5）
- 大包不通小包通、复制/大结果集卡 → PMTUD 黑洞（§II）
- 虚拟化环境网络抖、迁移窗口断连 → 虚机 stun/virtio（§IV）

## §I 二层交换层

### 1. 主机侧 bonding（多网卡绑定，DB 常见双网卡 bond0）

判读件：`/proc/net/bonding/bond0`（运行态真相）。

| 模式 | 语义 | 排查点 |
|---|---|---|
| 1 active-backup | 主备，单口流量 | Currently Active Slave 行；切主后**上游 MAC 表老化延迟→1~3s 断流**（工程定论），业务重连即可，不是故障 |
| 4 802.3ad | LACP 聚合 | Aggregator/Partner 段：Partner MAC 全零=**LACP 没协商上（落单）**——交换机侧 eth-trunk 没配 mode lacp 或 miimon 没起；落单=带宽减半+单口过载 |
| 5/6 tlb/alb | 出/双向负载均衡 | 依赖 ARP 应答操纵，**别在 DB 双活链路用**（ARP 漂移会让交换机 MAC 摇摆） |

- `miimon` 只看链路电口/光口状态，**看不到对端不可达**（网关挂了链路还是
  up 不切换）；要真探测配 `arp_interval+arp_ip_target=<网关>`
- 内核锚（v7.1-rc4，2026-11-17 精读）：模式表 bond_options.c:96-99
  （balance-rr/active-backup/xor…）；miimon 链路监控 bond_main.c:2880
  `bond_mii_monitor`（状态机 bond_miimon_link_change :2754）；arp 目标探测
  bond_main.c:3879 `bond_arp_monitor`；**主备切换时 bond 与 slave 地址交换
  bond_main.c:928 `bond_hw_addr_swap`**——上游交换机看到的源 MAC 变了，
  MAC 表老化窗的 1~3s 断流即由此来（机制闭环锚）
- 2026-10 env-D 判例即 bond0 环境（`../cases/case-envD-errstorm-202610.md`）

### 2. 交换机侧 Eth-Trunk（聚合口；2026-11-17 S12708 实拍 ARP 表即见
### Eth-Trunk1——上游走聚合）

- `display eth-trunk <id>`：member 状态、Operate status、LACP 协商——
  **member 不一致/单口 up = 聚合劣化**；判读纪律同上（落单=过载+丢包）
- **hash 极化**（工程定论）：聚合按 MAC/IP 哈希分流到成员口——少数大流量
  会话全落同一成员 → **聚合总利用率低但单成员口打满丢包**。指纹：端口级
  丢弃+聚合整体低利用率 → 看逐成员口利用率分布（与 switch-side-logs
  "低利用率丢包"指纹合并判读）

### 3. STP/生成树与 MAC 漂移（VRP 判据，推断级）

- **拓扑变化（TC）**：STP 重收敛/端口 flap → 交换机 MAC 表刷新 → 短暂洪泛/
  断流 30~50s（经典收敛窗）。指纹：业务抖动时刻交换机日志有 TC/拓扑变化
  事件；DB 长连接被断=TCP 收到 RST 或静默丢后重传超时
- **MAC 漂移（flapping）**：同一源 MAC 在两端口间跳 = 环路/VRRP 抖/双网卡
  乱入/安全设备注入。判据：交换机 MAC 漂移告警族；与 §II VRRP 互证
- MAC 表满：泛洪仿冒，罕见但表满后洪泛即丢包

### 4. 堆叠/集群（CSS/iStack）

实拍设备名 **S12708-2 的 "-2" 后缀=堆叠系统成员号**（端口编号跨 7/8 两框
即两成员），判读含义：

- 跨成员口流量走**堆叠链路**：堆叠口带宽/拥塞是跨框流量的瓶颈——实拍
  OAMDL `PeerSlotID` 跨板通道拥塞与堆叠口压力方向一致（**推断级关联**）
- 堆叠分裂=双活（数据链路层版 split-brain，参照 `../ha/split-brain.md`
  的处置思想）：MAC/ARP 漂移大规模出现+两台都自称同一系统=分裂指纹
- `display stack` / `display css status`（推断级命令名）看角色与堆叠口状态

## §II 三层路径层

### 网关与 VRRP

- 网关 VRRP 主备抖动 → 网关虚拟 MAC 漂移 → 业务间歇断；指纹：抖动时刻
  交换机日志 VRRP 状态翻转/优先级变化；主机侧 `arping <网关>` 两次应答
  MAC 不同=漂移实锤
- 网关挂而链路 up：`ping <网关>` 断但 `ethtool` Link up —— 与 bonding
  miimon 盲区同源（§I.1）

### 路由与回程

- **回程不对称 + rp_filter**：进出台数不同路时，strict rp_filter（=1）
  直接丢回程包——指纹：单方向通、握手完成但数据断；判读
  `sysctl net.ipv4.conf.all.rp_filter`、双网卡主机 `ip route get <对端>`
  与 `ip route get <从对端来的源IP>` 对比进出路径是否同路（内核锚 v7.1-rc4
  精读：net/ipv4/fib_frontend.c:429 `fib_validate_source`，:433 读
  IN_DEV_RPFILTER 即 rp_filter 取值点，:428 IPsec 保护的包豁免 rp_filter）
- 多网卡多默认路由：第二网卡默认路由抢流量 → 半通；`ip rule` 策略路由
  核对

### MTU / PMTUD 黑洞（大结果集/复制断的头号嫌疑）

- 指纹：**大包断小包通**——`ping -M do -s 1472 <对端>`（1500 MTU 极限
  探测，逐档降到通为止）与 `tracepath <对端>`（PMTUD 实测路径最小 MTU）
- 成因：路径上某段（隧道/VPN/云 overlay/中间设备）MTU 更小且**不回
  ICMP Frag Needed**（黑洞）→ 发送端 DF 位大包静默丢弃 → TCP 依赖
  重传降级，表现为慢/卡而非报错。源码闭环锚（v7.1-rc4，2026-11-17
  精读，详见 `net-diag-fullchain.md` ③）：DF 检查 include/net/ip.h:438
  + ip_output.c:581；ICMP Frag Needed 消费点 tcp_ipv4.c:577 `tcp_v4_err`
  ——黑洞=该 ICMP 被吞且 tcp_mtu_probing=0，MTU 永不更新
- DB 联动：复制流/大结果集是大包主体，PMTUD 黑洞时**主备复制断、全表
  扫描卡**而小查询正常——与库侧"慢"形态区分的关键指纹
- 隧道 overhead 常见扣减（工程参考，逐环境实测）：VXLAN ≈50B、GRE ≈24B、
  IPsec 视算法 ≈50~100B——虚机环境 MTU 必须端到端核对

## §III 中间设备层（断连规律性判据的富矿区）

### 防火墙/安全设备 idle 会话掐断（DB 空闲连接消失的头号根因）

- 机制：状态防火墙/LB 对空闲会话有 idle timeout（常见 300~3600s，
  逐设备配置），超时**静默删会话**——后续包到达即丢，两端都不知道，
  直到某端重传超时或另一端发数据收到 RST
- **指纹（高置信）**：断连时刻**规律**（整 30min/60min 等）+ 服务器侧
  无 FIN/RST 记录 + 应用报 "connection reset"/"broken pipe"；与
  `internals/connection-lifecycle.md` 假死连接形态互证
- 处置：应用侧保活（JDBC 池 testWhileIdle / TCP keepalive——注意内核
  默认 tcp_keepalive_time=7200s **比多数中间设备 idle timeout 长**，
  对网络设备保活必须把 keepalive 降到设备超时的一半以内，
  旋钮见 `../performance/parameters.md` 组 7）或网络侧调大 idle timeout
- 安全设备 RST 注入（IPS 拦截）：指纹=连接被 RST 且 RST 的 TTL/IP 特征
  与正常对端不符，抓包可证（`net-diag-fullchain.md` ⑦）

### 中间设备杀连接四形态（2026-11-19 实战问出来的原理定论）

杀 TCP 连接≠必发 RST——**"不发 RST"反而是多数设备的默认杀法**。四形态
与双端死亡签名（现场按日志文本对号，可反推设备行为）：

| 形态 | 典型场景 | 对端A | 对端B | 判读锚 |
|---|---|---|---|---|
| ① 静默丢弃（默认） | **会话表老化/被清除后老流量包被丢**（五元组不匹配且非 SYN）；IPS inline drop；LB 超时静默删 | 重传→**timeout** | timeout | ★清会话表≠发 RST：`reset session table` 类操作默认只删表项，老连接落黑洞，两端靠超时自灭 |
| ② 双向注 RST（显式动作） | IPS block/reset、防火墙"会话复位"带 rst、抗D 拦截 | **reset by peer** | **reset by peer** | 只有配了 reset 动作的设备才产 RST 风暴 |
| ③ 单向注入 | 只复位一侧 | RST | timeout/无感 | 一端 reset 一端 timeout 的分叉判据 |
| ④ 发 FIN（伪优雅） | 上网行为管理/DLP"优雅复位"、部分 LB idle 超时 | EOF（似正常 close） | EOF | 日志只见"连接关闭"无任何错误 |

**双签名交叉验证法**（reset 风暴案的定性利器）：同窗数两族日志——
`reset by peer` 条数 vs `timeout` 条数。只有 RST 洪峰→注 RST 型设备或
端点自杀；只有 timeout→静默黑洞型；**两族同时爆发→设备混合动作，或
网络坑(timeout 族)+端点自杀(RST 族)两因叠加**。库侧配合：
ESTABLISHED 态收 RST 每条必 +1 EstabResets（tcp.c:3016 状态直跳计数，
先于日志，无时延——计数器长在产生日志的同一条 softirq 路径上，
详见 `network.md` 断开路径节）。

### NAT / conntrack

- 表满 early_drop → 丢包：判读与旋钮在 `network.md` conntrack 节（不复制）
- SNAT 源端口耗尽：大量短连接经同一 SNAT 出口 → 端口重用冲突 → 偶发
  SYN 丢；指纹：连不上与连接风暴时间重合、`conntrack -C` 高位运行

### 负载均衡/代理（client→LB→DB 架构）

- DBA 必须知道连接拓扑里有几跳：`netstat` 对端 IP 是 LB 还是真实
  DB——LB 下源 IP 全是 LB 地址，ACL/审计全部变形（判读纪律）
- LB 健康检查误判摘流 → 瞬断池化；LB 自身 SNAT 端口耗尽同上
- VIP 漂移（keepalived）：主备切换窗口断流 1~3s+ARP 通告延迟；指纹同
  VRRP/bonding 切换

### DNS（连接串走域名的首连延迟）

- 指纹：**首连固定 5s/10s 延迟、之后连接正常**——resolv.conf 第一个
  nameserver 不可达，resolver 按 timeout（默认 5s）× attempts 逐个试
  （glibc resolver 行为，工程定论）；`time getent hosts <域名>` 直接
  实测解析耗时
- 处置：修 NS 顺序/上本地缓存；DB 层无旋钮，别在库里找根因

## §IV 虚拟化与云层

### 虚机在线迁移（vMotion/热迁移）——间歇断连经典根因

- 机制：迁移切换瞬间网络 stun 数秒 → DB 长连接断、复制重连、AWR 出
  单点抖动；**主机计数器全绿**（包没丢，是停了）
- 指纹：抖动时刻与虚拟化平台操作记录对时；单次而非持续；迁移窗口内
  多台 DB 同时抖
- 处置：与虚机管理员对迁移窗口，DB 前置规避（或接受复制重连代价）

### virtio / 半虚拟化网卡

- 吞吐顶到 vhost 线程：host 侧 qemu vhost-worker 线程 CPU 饱和 → 虚机
  内怎么看都不丢但吞吐上不去——host 侧 `ps`/`top` 看占核，与宿主机
  管理员对账（判据在 host，不在虚机内）
- **虚机内外计数器对账**：虚机内 `ethtool -S` 与宿主机对应 virtio 口
  对账——虚机内全绿宿主机丢=宿主机层（ring/物理）问题；反向=虚机栈
- vmxnet3/emi 等其他半虚拟驱动同理：**判读位永远在宿主机**

### 容器 / 云网络

- veth 对端丢包：容器内全绿但宿主机 veth/tc 丢 → CNI/宿主机层
- 云安全组/NACL：状态化表满、ACL 变更窗口断连；变更记录与抖动对时
- 弹性网卡带宽上限：**云限速不是协商速率**，ethtool 显示的 Speed 是假的
  ——打流顶上限即丢，`iperf3` 压测即可证（`net-diag-fullchain.md` ⓪）
- OVS 流表/丢包在宿主机 `ovs-dpctl show`（存在时），判读位同上

## 分诊速查表（症状→本文落点）

| 症状指纹 | 落点 |
|---|---|
| 断连时刻有规律（整点/固定间隔） | §III idle 掐断 |
| 断连 1~3s、主备切换相关 | §I.1 bonding / VIP 漂移 |
| 单成员口满聚合整体空 | §I.2 hash 极化 |
| 网关 MAC 漂移/arping 异答 | §II VRRP / §I.3 MAC 漂移 |
| 大包断小包通/复制断小查询正常 | §II PMTUD 黑洞 |
| 握手成数据断、单方向通 | §II 回程不对称 rp_filter |
| 首连固定 5s 延迟 | §III DNS |
| 多台 DB 同时抖、单次 | §IV 迁移 stun |
| 虚机内绿宿主机丢 | §IV 虚机内外对账 |
| 带宽永远上不去、ethtool 正常 | §IV vhost 线程/云限速 |

## 与库侧双向锚点

- `network.md`：conntrack/keepalive 默认值/三道关——本文 idle 掐断与
  NAT 节的旋钮本体
- `internals/connection-lifecycle.md`：idle 掐断的库侧下游=假死连接/
  客户端消失三形态；compat_oracle_txn_control 只读事务被杀与本文中间
  设备掐断是**两种"连接消失"**，按指纹区分（有无 FIN/RST）
- `../ha/replication-lag.md`：PMTUD 黑洞/迁移 stun 的下游=复制断连重连
- `net-diag-fullchain.md`：八步 SOP 的中间层展开=本文；双端抓包 SEQ
  对账可把本文各层嫌疑坐实
- `switch-side-logs.md`：Eth-Trunk/堆叠/MAC 漂移的交换机 logbuffer 侧证

## 版本注记

- bonding/miimon/arp_ip_target：v7.1-rc4 行级精读（2026-11-17），文件级
  镜像 `/home/xiang/kernel-src/linux-7.1-rc4-mirror/`（plain 抓取，全树
  未落地）；rp_filter 同（fib_frontend.c 精读）；resolver timeout=5s
  默认值为 glibc 工程定论（glibc 树未落地，标注）
- VRP 侧（STP 收敛窗/堆叠/Eth-Trunk 命令名/VRRP 日志族）：通用工程口径
  **推断级**，待华为手册/实机输出升级
- 中间设备 idle timeout 常见区间、迁移 stun、vhost 判读位：工程定论
  （无源码无手册，指纹法可实证）

## 来源

- 2026-11-17 S12708 实拍（Eth-Trunk1/堆叠 -2 后缀/OAMDL PeerSlotID——
  本文 §I.2/I.4 的事实锚）
- env-D bond0 判例（`../cases/case-envD-errstorm-202610.md`）
- 内核路径级锚点（树未落地，2026-11-17 标注待精读）
