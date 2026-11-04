# Linux 网络层（收包路径 / 两级连接队列 / TIME_WAIT / RTT / TCP 计数器与内存）

## 概述

> 服务分类：linux（本分支）、performance（连接风暴/网络抖动判读）、
> troubleshooting（连接拒绝、丢包、假死连接现场）。数据库的"慢"有一类
> 根本不在库里——在收包队列、连接队列和重试定时器里。

## 收包全路径与丢包三道关

```
网卡 → 硬中断(登记 NAPI) → 软中断 NET_RX: net_rx_action 批量 poll
     → backlog 队列(每CPU, netdev_max_backlog) → __netif_receive_skb
     → IP/TCP 处理 → ①半连接队列(SYN) → ②全连接队列(accept) → 应用
```

- 软中断执行预算与 ksoftirqd 转移见 `cpu.md`；net_rx_action 自身还有第二层
  预算：**netdev_budget（默认 300 包/轮）+ netdev_budget_usecs（时间片）**
  （net/core/dev.c:7918-7920；sysctl_net_core.c:622）
- **/proc/net/softnet_stat 逐列判读**（渲染 net/core/net-procfs.c:150）：
  - 第 2 列 dropped：**backlog 队列满丢包**——收包快于协议栈处理；
    处置：加大 `net.core.netdev_max_backlog`（sysctl_net_core.c:464）+
    看为什么协议栈慢（软中断预算？CPU 饱和？）
  - 第 3 列 time_squeeze：**预算耗尽没处理完**（softnet_stat 渲染的就是
    sd->time_squeeze）——涨 = 网络软中断过载；处置：netdev_budget_usecs
    加大 / RPS 分散 / 换更快的软中断路径；`si` 高+ksoftirqd 高见 `cpu.md`
- 网卡自身丢包在 ethtool -S（rx_no_buffer / rx_missed 等）——先分清丢在
  **网卡、backlog、还是连接队列**，三层旋钮完全不同

## 两级连接队列（连接拒绝/慢连接的根）

**半连接队列（SYN 队列）**：收到 SYN 的 embryonic 连接，容量
`net.ipv4.tcp_max_syn_backlog`。

- **syncookies 语义（net/ipv4/tcp_input.c:7616-7618，深读）**：
  - `tcp_syncookies=1`（默认）：**半连接队列满才降级用 cookie**（不占内存，
    代价是丢失部分 TCP 选项协商）
  - `=2`：无条件 cookie
  - 逻辑原码：`if (syncookies == 2 || inet_csk_reqsk_queue_is_full(sk)) → cookie`
- SYN Flood 攻击特征：半连接堆积 + 客户端 SYN 重传（TcpExt TCPSynRetrans）

**全连接队列（accept 队列）**：三次握手完成待 accept，容量 = listen(fd,
**backlog**) 的 sk_max_ack_backlog。

- **溢出计数**：LINUX_MIB_LISTENOVERFLOWS（tcp_input.c:7576,7627）→
  `/proc/net/netstat` TcpExt 的 **ListenOverflows / ListenDrops**
  （include/net/tcp.h:2805）
- 溢出后行为由 `tcp_abort_on_overflow`（net/ipv4/tcp_minisocks.c:951）定：
  - **=0（默认）：静默丢 ACK**——客户端认为连上了，服务端没这连接 →
    客户端发数据无响应直到超时（**假死连接的经典来源**）
  - =1：回 RST 快速失败（客户端立刻报错，好排障但连接失败率显性化）
- **一手判据**：`ss -lnt` 监听行的 **Recv-Q = 当前 accept 队列长度，
  Send-Q = 容量上限**——Recv-Q 贴着 Send-Q = 应用 accept 不及时（线程池/
  线程池满），光调 backlog 没用；**DB 联动**：too many clients、连接风暴
  （`parameters.md` 组 7：somaxconn+tcp_max_syn_backlog 与应用 backlog 同步调）；
  accept 队列的库侧唯一消费者=postmaster 单线程
  （`../internals/connection-lifecycle.md`）

### listener 全队列堆积的内核级连锁（连接卡 + sys/si 高，深读）

完整 TCP 建连链路（内核侧逐步）：

```
客户端 SYN → 硬中断登记 → NET_RX 软中断 → tcp_v4_rcv → tcp_conn_request
  ├─ 半连接队列(reqsk)满：syncookies=1 降级 cookie；否则丢 SYN（ListenDrops）
  ├─ 入半连接队列（reqsk_queue_hash_req，inet_connection_sock.c:1141——
  │   每条 reqsk 挂 SYN-ACK 重传定时器）
  → SYN-ACK → 客户端 ACK → tcp_check_req（tcp_minisocks.c:951）：
  ├─ 全连接队列满：abort_on_overflow=0（默认）→ **丢这个 ACK**
  │   （ListenOverflows）——三次握手"差一口气"：客户端已 ESTABLISHED、
  │   服务端还在 SYN_RECV
  ├─ =1 → 回 RST 快失败
  → 队列不满：创建 child sock 入 accept 队列 → 唤醒 accept 等待者
  → accept()（inet_csk_accept，inet_connection_sock.c:649）持队列锁出队
```

堆积时为什么**卡**且 **sys/si 高**（机制逐条）：

1. **SYN-ACK 重传空转**：丢 ACK 后服务端认为握手未完成，按 RTO 退避
   （**TCP_TIMEOUT_INIT=1s** 起，tcp.h:169）重传 SYN-ACK 直到
   **tcp_synack_retries（默认 5）** 耗尽（旋钮 sysctl_net_ipv4.c:1033；
   重传引擎 tcp_timer.c:255-256,468）——每次重传=定时器点火+发包，**全部
   在软中断上下文执行** → si/sys 抬升；reqsk 一直占着半连接槽到超时，
   **半连接队列也被连坐拖住**
2. **客户端反向放大**：客户端 ESTABLISHED 后发数据/请求无响应，重传数据包
   ——服务端每个包都进软中断处理后丢弃，**双向空转**
3. **定时器与唤醒风暴**：每条堆积 reqsk 各自持定时器；accept 一有位置就
   唤醒消费线程，堆积时"唤醒→消费不动→再排队"循环 → 上下文切换同升
   （cswch，见 `cpu.md`）
4. **唯一消费者是应用**（库场景=postmaster 单线程 poll）：堆积根因=消费
   速率（每连接 accept+盐+线程孵化，毫秒级），**内核侧读数只是放大器**：
   ListenOverflows/ListenDrops/syn_retrans/软中断占比都是症状不是病因

**判读指纹**：连接卡 + sys/si 高 + ListenOverflows 涨 + 客户端"连上但无响应"
= 全队列堆积标准形态。处置按序：应用消费速率（连接池化/降线程孵化成本）→
backlog+somaxconn（只买缓冲时间）→ tcp_abort_on_overflow=1（快失败换
可观测性，确认瓶颈后回改）。

## TIME_WAIT / conntrack / 重试（判读"网络抖动"的三个真相）

### TIME_WAIT
- 时长 **TCP_TIMEWAIT_LEN = 60s**（include/net/tcp.h:142，编译常量，sysctl
  调不了）；只有**主动关闭方**进入 TIME_WAIT
- 大量 TIME_WAIT = **短连接风暴**的征兆（`ss -s` 汇总行）；根治 = 应用侧
  连接池长连接；`tcp_tw_reuse` 只对**本机主动发起**的新连接复用端口
  （需时间戳），治标
- **坑**：`tcp_tw_recycle` 在 4.12 已删除且 NAT 环境下必然误杀——老文档还
  在推荐，见到就毙（版本注记）

### conntrack（nf_conntrack）
- 装载防火墙/nat 模块才有这层；表容量 `nf_conntrack_max`
  （net/netfilter/nf_conntrack_core.c:205），**满时先 early_drop 淘汰旧表项
  （:1463），淘汰不出来才丢包**——文案实锤：`nf_conntrack: table full,
  dropping packet`（nf_conntrack_core.c:1674；netns 变体 :1676；expectation
  表满 :507）
- 判读：`/proc/sys/net/netfilter/nf_conntrack_count` 贴 max → 加 max 或
  规则放行数据库端口减少追踪量；纯 DB 内网机可不加载 conntrack 模块

### TCP 重试与假死连接（高价值判读）
- 重传退避：上限 **TCP_RTO_MAX = 120s**（include/net/tcp.h:162-163），
  放弃阈值 `tcp_retries2`（默认 15 次，消费点 tcp.h:271/:420）——**从第一次
  超时到内核放弃 ≈ 15 分钟量级**
- 含义：**拔网线/半开连接不会立刻死**。应用层先超时报错重连，旧连接还挂着
  ——如果旧会话持着锁，新会话会等一把"主人已断网"的锁：**阻塞链上的假死
  持锁者，先查其 TCP 状态（ss -tnp 对端 unreachable/重传计数）再杀**
  （联动 `locks.md` 阻塞链、`error-fatal-mechanics.md` 取消家族）
- keepalive 首探：内核编译默认 7200s（两层口径与实测见"客户端消失三形态表"；
  env-A 实测 sysctl=30s）——DB 场景仍应靠应用层心跳/连接池探活

## TCP 关闭链路与"客户端消失、连接还在"（长事务的网络侧根因，深读）

### 正常关闭（四次挥手）与两个 RST 变体

```
主动方 FIN → 被动方 ACK（内核立即回）
被动方 app 读到 EOF（FIN 到达 → sk_data_ready 唤醒读者，tcp_input.c:5361 族）
被动方 app close → FIN → 主动方 ACK → 主动方进 TIME_WAIT 60s（tcp.h:142）
```

- 库侧含义：客户端正常退出（进程退出时 OS 自动关 fd 发 FIN）→ DB 线程读到
  EOF → 会话清理、未提交事务回滚——**FIN 一定会到，除非下面第②种静默死**
- **RST 变体一（abort 关闭）**：SO_LINGER timeout=0，或关闭时接收队列还有
  未读数据 → `tcp_send_active_reset`（tcp.c:3191,3264）——kill -9 走 OS 的
  FIN；应用层 abort 关闭（部分连接池销毁路径）才发 RST
- **RST 变体二（无主包）**：包到达但无 socket 匹配/撞 TIMEWAIT →
  `tcp_v4_send_reset`（tcp_ipv4.c:717,2331）

### 客户端消失三形态 → 连接/事务残留时长（判读核心表）

| 消失形态 | 网络上发生什么 | 服务端何时知道 | 残留事务寿命 |
|---|---|---|---|
| ①进程死/kill（OS 还在） | OS 关 fd → **FIN** | FIN 到达即唤醒（EOF） | 秒级；但**长语句执行中**的会话要到下次读/写才察觉——持锁照常到语句结束 |
| ②主机断电/网线拔（无包） | **什么都不发** | 只有 keepalive 能发现。**两层口径**：内核编译默认 **7200s+9 次×75s**（TCP_KEEPALIVE_TIME/PROBES/INTVL，tcp.h:180-182），但**以机器 sysctl 实测为准**——env-A 实测 30s·30·9，判死窗口≈5 分钟；tcp_keepalive_timer（tcp_timer.c:779）探测无响应→自 RST（:838） | 编译默认 **~2 小时**；sysctl 调过则=实测值（env-A≈5 分钟） |
| ③网络分区/NAT 超时（发得出收不到） | 客户端包进黑洞 | 有在途数据靠重传退避至 **tcp_retries2≈15 分钟**放弃；无在途数据退化为② | 15 分钟 ~ 2 小时 |

**危害链（"客户端不在了、长事务还在"的机制本体）**：残留会话的事务压住
OldestXmin → vacuum 清不掉死元组 → 膨胀（`mvcc-visibility.md`/`autovacuum.md`）；
持锁不放 → `blocking-live.md` 假死持锁；占用连接配额。

**库侧防线（GUC 全套在基线实锤，guc.cpp:2346,2621+）**：

- `tcp_keepalives_idle/interval/count`：把服务端 socket 的内核 keepalive 拉到
  分钟级（例：idle=300/interval=30/count=3 → ~7 分钟判死）。**GUC 默认
  0=跟随内核 sysctl**（env-A 实测）——要收紧必须显式设置 GUC 或调 sysctl，
  只依赖编译默认就是 2 小时
- `idle_in_transaction_session_timeout`：事务中空闲超时直接杀会话——②③
  形态的根治线。**默认 0=禁用**（env-A 实测）：残留长事务默认无人管，
  交付核对时确认这项要不要开
- 应用层连接池 evict+心跳仍是第一道防线（域规矩：治理走应用侧）

### 判读指纹（连接还在但客户端没了）

`pg_stat_activity`：state=idle in transaction、事务开始时间远早于客户端
存活证据；`ss -tnp` 对端无响应/重传计数——按三形态表定位 → kill 会话
（`blocking-live.md` 止损）→ 补防线（上面 GUC 组）。

### CLOSE_WAIT 堆积（"优化"的正解是会话治理，不是内核参数）

- **语义**：对端 FIN 已到、内核已代答 ACK，**本端应用还没调 close()**——
  fd 属于应用。**没有任何 sysctl 能清 CLOSE_WAIT**：tcp_tw_reuse/tw_reuse/
  fin_timeout 全是 TIME_WAIT/FIN_WAIT 的药，对 CLOSE_WAIT 无效（常被开错方子）
- **VB 侧本体**：每连接一个会话线程独占 socket fd，fd 要到会话线程退出才
  close。**空闲会话在 recv 等待中读 EOF → 毫秒级退出**，CLOSE_WAIT 几乎
  不可见 ⇒ **CLOSE_WAIT 堆积 = 会话线程忙/卡**：长 SQL 执行中、锁等待、
  IO 挂起、内核级冻结（env-W wbt 全冻 53 分钟=全量 CLOSE_WAIT 的实证形态）
- **★人口学修正（2026-11-03 用户点破）：CLOSE_WAIT 数 ≠ 线程数**。线程从
  建链协议完成才出生；三个子群：①内核 accept 队列里从未被 accept 的（客户端
  等不及已 FIN，**无线程无会话，gs_stack 永远看不见**——postmaster accept 到
  手就是尸体）②被 accept 后死在建链/认证途中（日志见 "failed to send SSL
  negotiation response: Broken pipe"=postmaster.cpp:4802，回 'N' 时对端已断；
  SSL 关着也有此问答回合）③入会话后客户端死掉（pg_stat_activity 可对账）。
  对账公式：CLOSE_WAIT 数 ≫ 线程数−会话数 → 大头在 backlog=postmaster 消费
  不动；**gs_stack 要看的是 postmaster 主线程那一帧**（TCP+unix socket 共同
  的 accept 消费者），不是找不存在的线程
- **session_timeout 管不了它**：默认 600s（guc.cpp:2332），只杀**空闲**会话——
  忙会话不适用，其 CLOSE_WAIT 持到语句结束或人工 terminate。（★架构前提：
  用户环境线程池恒关=**每连接一个独立线程**，fd/超时判定都挂在各连接自己的
  线程上；线程池路径不作为分析前提——2026-11-03 用户令）
- **分诊序**：
  1. `ss -tnp state close-wait` 先看**归属进程**——不是 VB 的（LB/应用/
     连接池）就是那个进程没 close（经典 Java 池泄漏），库侧无责
  2. 是 VB → 按四元组 local port 反查 `pg_stat_activity.client_port`，
     看该会话 state/query 在干什么（active 长语句？锁等待走 `blocking-live.md`）
  3. 止损 `pg_terminate_backend`；根因取栈（gs_stack，见 `gs-stack-sampling.md`）
- **防线**：session_timeout（空闲死会话）；idle_in_transaction_session_timeout
  （事务中空闲）；**忙会话没有服务端超时可救**——statement_timeout 是应用/用户
  级设置，或直接治卡住根因

**对正常连接的影响面（三条真实链路 + 一条排除，2026-11-03 补）**：

| 链路 | 机制 | 何时咬人 |
|---|---|---|
| fd 预算 | socket fd 会话线程直持（不走 VFD），占 max_files_per_process/rlimit 名额 | 数万级 → EMFILE 新连接失败（联动 `fd-vfd-budget.md`） |
| TCP 内核内存 | RECV-Q 钉在 tcp_rmem，**全机 TCP 共享 tcp_mem 三档水位**——唯一能殃及正常连接的内核通道 | /proc/net/sockstat 的 tcp mem 逼近 sysctl tcp_mem pressure 档 → 全机连接丢包/零窗/重传 |
| 连接槽+线程（VB 最现实） | CLOSE_WAIT 背后=忙/卡会话：占 max_connection 槽 + 各占一个独立线程（每连接一线程，无池）；整机 CPU 饱和时新连接的 clone 线程+认证也变慢 | 槽满 → too many clients；CPU 被卡死线程群体吃满 → 新连接建立慢（TCP/unix socket 通杀） |
| ~~accept 队列~~ | **不成立**：CLOSE_WAIT 不占 SYN backlog/LISTEN | — |

- **Recv-Q 判读**：FIN 占一个序列号（tcp_fin 里 rcv_nxt++）→ 纯净 CLOSE_WAIT
  Recv-Q 也显示 1；Recv-Q 几百字节=有未读真实载荷（对端 FIN 前最后一发）。
  **不会再涨**（对端已走）——是遗照不是水位；线程退出时因收队列未读
  内核代发 RST（tcp_send_active_reset，上文 RST 变体一）
- **量级判定**：几百条且平稳+新连接正常=黄灯（查卡点比查 socket 重要）；
  还在涨或新连接慢/被拒=已踩链③，先批量 terminate 止损
- **对端分布指纹（2026-11-03 真机实例）**：CLOSE_WAIT **集中在单一对端 IP**
  （350/480 全来自一个应用，它另有少量 ESTAB）= 那台应用在**超时重试反复建断**：
  服务端慢 → 客户端超时主动断 → 服务端线程没忙完 → CLOSE_WAIT 堆 → 应用重连
  加压，正反馈。与"分散多对端=普遍冻结"区分。★配套判读：ListenOverflows 是
  **开机累计值**——计数冻结=历史风暴存量，当前未溢出（Recv-Q 未顶 Send-Q），
  somaxconn 不用碰；只有计数在动才算风暴进行中。CPU 状态属**待查项**，
  不得拿其他案场的 CPU 数字代入本案（2026-11-03 用户点名两案混线）

## TCP RTT：估计器与 ss -ti 口径（RTT avg/p95 / Worst peer / Send-Q 的本体）

**估计器**（net/ipv4/tcp_input.c:1070 `tcp_rtt_estimator`，ACK 到达时由
`tcp_ack_update_rtt`(:3492) 驱动）——Jacobson SIGCOMM'88 原版：

- `srtt_us` 内部 **×8 缩放存储**：`m -= srtt>>3; srtt += m` ⇒ 平滑式
  **srtt = 7/8·旧 + 1/8·新**；RTO = srtt + 4×mdev（1990 修正式——源码注释
  原话吐槽 VJ 原公式，妙不可读）
- `mdev`（即 rttvar）更新在 RTT **下降**方向用细增益 `m>>3`（Eifel 变体，
  tcp_input.c:1096-1102）——防 RTO 骤降
- **ss -ti 读的就是 tcp_get_info**（net/ipv4/tcp.c）：
  `rtt = srtt_us>>3`(:4303)、`rttvar = mdev_us>>2`(:4304)、
  `rcv_rtt`(:4308，**接收侧**估计)、`min_rtt`(:4324，窗口内最小值=判读地板)

**判读（AWR 的 RTT avg/p95、Worst peer、Top10 by Send-Q/RTT 即 ss -ti 每分钟
快照汇总）**——RTT 高分三个来源，处置完全不同：

1. **路径**（物理距离/网络拥塞）：min_rtt 也高——链路本身远或堵，库侧无解，
   找网络组/挪机房
2. **接收侧**：`rcv_rtt` 显著大于 `rtt` = 应用不读 socket、数据堆在收队列
   ——DB 侧就是客户端消费慢（大结果集拉不动），联 metrics.md"DB Time 内
   无事件标签的 socket 写阻塞"
3. **缓冲膨胀**：rtt 高而丢包少——中间设备队列深

- p95 >5ms 留意、>20ms 标红；分钟级 max 过 200ms = 灾难线
- **Mathis 单流上限 ≈ 1.22·MSS·8/(RTT·√p)**（模型公式，非内核源码；适用
  稳态随机丢包、窗口不设限）：RTT 翻倍 = 单流吞吐减半——对 DB 的实际影响
  是大批量结果集拉取与往返型交互（认证/逐行取）变慢
- Send-Q 大 + RTT 高 = 对端消费慢（ss -ti 的 send-q = 未确认字节）
- **rttvar（RTT 抖动）**：`tcp_get_info` 的 `rttvar = mdev_us>>2`（tcp.c:4304），
  是 RTT 均方差的平滑估计。rttvar 高 = 路径时延不稳（抖动），对 DB 的影响是
  **往返型交互的 P99 尾巴**——即使 RTT 均值正常，抖动大也会拖慢逐行取/认证。
  判读：rttvar 与 rtt 同量级或更大 = 抖动主导，查拥塞/队列波动
- **mss（协商段长）**：sspeer 的 mss = 该连接协商的 MSS（MTU−40 头）。
  **mss 明显小于 1460（以太网标准）= MTU 被压低**——隧道/overlay（VPN/GRE/
  VXLAN）封装削减了有效 MTU，或对端 MSS 钳制。段长小 = 每字节包头开销大、
  吞吐打折，且常伴分片/PMTU 黑洞问题

## SNMP 计数器口径（/proc/net/snmp Tcp 行——Retrans%/CurrEstab/Opens 的本体）

表项渲染 net/ipv4/proc.c:140-155（`snmp4_tcp_list`）；增减点逐个对过源码：

| 计数器 | 语义 | 增/减点 |
|---|---|---|
| ActiveOpens | 发起连接数（发了 SYN） | tcp_output.c:4387（tcp_connect） |
| PassiveOpens | 被动建连完成数（三次握手成） | tcp_minisocks.c:667（建 child sock） |
| CurrEstab | **瞬时 gauge 非 Δ**：当前 ESTABLISHED 数 | tcp.c:3007/3011（tcp_set_state 入）/ :3025（出） |
| OutSegs | 发出段数（含 SYN/FIN/SYN-ACK） | tcp_output.c:1698（正常发段）+ :4051（SYN-ACK，tcp_make_synack） |
| RetransSegs | 重传段数（**数据重传 + SYN-ACK 重传**） | tcp_output.c:3644（__tcp_retransmit_skb）+ :4644（tcp_rtx_synack） |
| InErrs | 收到坏段（校验和/头错） | tcp_input.c:6422/6567/6666、tcp_ipv4.c:1894/1928 |

- **Retrans% = RetransSegs/OutSegs**：分子含 SYN-ACK 重传——**全队列堆积时
  （"SYN-ACK 重传空转"节）Retrans% 会被动抬升，不全是网络丢包**；
  阈值 >3% 加粗、>5% 标红（metrics.md）
- PassiveOpens 速率 = 服务端建连速率：短连接风暴时与 CurrEstab、TIME_WAIT
  同升（连接风暴判读指纹）

## TcpExt 高价值细分（/proc/net/netstat）

表项 proc.c:185-289。AWR Protocol Errors 章用的都在这层：

- **TCPAbortOn\*** 家族（proc.c:224-229）——连接被 RST 掐死的原因分流：
  OnTimeout（重传耗尽/严重丢包，多=网络真差）、OnMemory（**TCP 内存压力**，
  联动下节）、OnClose/OnData（应用 abort 型关闭，连接池销毁路径典型）、
  OnLinger
- **ZeroWindow 族**（proc.c:267-269/:289）：TCPToZeroWindowAdv=收窗缩到 0
  （应用不消费）、TCPWantZeroWindowAdv=还想收但窗已 0、TCPZeroWindowDrop
  (:289)=窗 0 时收包被丢——**收侧消费慢的铁证**，配 ss -ti rcv_rtt 联判
- ListenDrops/SynRetrans 呼应两级连接队列节（机制已述，不重复）

## TCP 内存与收发缓冲（tcp_mem / rmem / wmem）

- **sysctl_tcp_mem[3]**（tcp.c:305；初始化 :5191-5193 按物理内存推算）：
  min=limit×3/4（≈RAM 4.68%）/ pressure=limit（≈6.25%）/ max=min×2
  （≈9.37%）——全部 TCP socket 缓冲的总量三档
- **进入压力**：tcp_enter_memory_pressure（tcp.c:329，cmpxchg 只记进入沿）
  → netstat 的 TCPMEMORYPRESSURES；压力期内发送缓冲授予收紧 → 吞吐被压
- **收窗自动调优**：tcp_moderate_rcvbuf（sysctl_net_ipv4.c:1347）+
  tcp_rcv_space_adjust（调用点 tcp.c:1733/1819/2332/2884，读路径驱动）——
  按实测交付速率×RTT 扩收窗；**上限被 net.core.rmem_max 摁住**
- 判读链：TCPMEMORYPRESSURES 涨 / TCPAbortOnMemory>0 → 查 tcp_mem 三档与
  rmem_max；**长 RTT 大流量场景收窗不够 = 吞吐被 窗口×RTT（带宽积）限死**
  （rmem 必须 ≥ BDP=带宽×RTT）；sysctl 参数族对照 `../performance/parameters.md` 组 7

## 网卡层（errs / 协商 / Ring / 带宽饱和）

- **/proc/net/dev 渲染口径**（net/core/net-procfs.c:44-53
  dev_seq_printf_stats）：`rx_errors` 单列；**drop 列 = rx_dropped +
  rx_missed_errors 合并**（:53）——协议栈丢与 **ring 满丢（rx_missed）在
  这个文件里分不开**
- **权限口径（源码双版本实锤 + WSL 实测，2026-10-27）**：`ethtool -S`（
  ETHTOOL_GSTRINGS/GSTATS）、`-g`（GRINGPARAM）、`-i`/速率（GSET/GDRVINFO）
  都在内核 "Allow some commands to be done by anyone" 白名单里——
  **v3.10（CentOS 7 生产档）:1432-1446、7.1-rc4 net/ethtool/ioctl.c:3264-3278
  两版都在**，不需要 root。执行不了的常见真因：ethtool 未安装（CentOS
  minimal）或设备不支持（bond 口/部分 virtio → EOPNOTSUPP，不是权限）
- **非 root、无 ethtool 依赖的等效口径**（vb_tool 采集首选）：
  `ip -s -s link show <dev>`——rtnetlink GETSTATS 无 capable 闸，RX 的
  **missed 与 dropped 分列** = ring 满与栈丢直接分层；或逐字段读
  `/sys/class/net/<dev>/statistics/rx_missed_errors`（net-sysfs.c:854）
- errs = 协商错/驱动错（半双工误协商、CRC）——先 `ethtool <nic>` 核
  Speed/Duplex（ETHTOOL_GLINKSETTINGS 路径 net/ethtool/ioctl.c:439/:545）
- Ring 深度 `ethtool -g`；**RX/TX Util% = 流量 B/s ÷ 协商速率**：>70% 带宽
  近饱和（1G≈125MB/s、10G≈1.25GB/s 换算）
- 判读次序 = "三道关"的网卡层入口：ip -s -s / ethtool -S（网卡/ring）→
  softnet_stat（backlog）→ netstat（连接队列）

### 业务网卡定位与 bond 口径（采集器落地必读）

**判哪个口承载业务（三步）**：

1. **监听 IP → 接口**：`ss -tlnp` 拿 DB 监听地址 → `ip addr show` 反查接口
   名（AWR 侧=报告头实例 IP）；主备环境**业务口与复制口是两个口**，都要收
2. **流量校验**：/proc/net/dev 逐口排序——业务口必然是流量大头；虚拟口
   排除（lo / veth\* / docker\* / br-\* / virbr\* / tunl\*），零流量口跳过
3. **是 bond 就展开 slave**（下）

**bond 口径（源码实锤，2026-10-27）**：

- `ethtool -S bond0` = **EOPNOTSUPP，确实查不了**：bond_ethtool_ops 只实现
  get_drvinfo / get_link / get_link_ksettings / get_ts_info 四项
  （bond_main.c:5925-5930）——**驱动私有计数必须下到 slave 物理口查**
- **流量统计 bond 口是聚合的、可用的**：bond_get_stats 遍历 slave 做增量
  fold（7.1-rc4 bond_main.c:4499-4525；3.10 :3511 同为遍历聚合）——
  /proc/net/dev 与 `ip -s` 的 bond0 数字 = 全 slave 合计，**判带宽饱和/
  总量就用 bond 口数字**，不用自己加 slave
- **slave 展开命令（全部普通用户可读）**：`cat /proc/net/bonding/bond0`
  （0444 创建，bond_procfs.c:291——**模式、active slave、链路状态一步
  拿全**）或 `/sys/class/net/bond0/bonding/slaves`；然后逐 slave 查
  ethtool -S / statistics/rx_missed_errors / ethtool -g
- **模式语义陷阱**：active-backup 只有一个 active slave——**备口零流量属
  正常，别当链路故障**；802.3ad / balance-xor 两口分摊流量都要看；
  Util% 分母按模式折算（802.3ad=聚合速率、active-backup=单口速率）

### 虚拟机的返回形态（采集器三态设计，2026-10-27 WSL2 实测）

- **bond 探测判据 = 目录存在性，零歧义**：`/proc/net/bonding/` 或
  `/sys/class/net/<dev>/bonding/` 不存在即没配 bond。**宿主机侧的 bond 对
  guest 完全不可见**（bond 是宿主层软件聚合，VM 拿到的是单块虚拟网卡）——
  虚拟机上探测不到 bond 是常态；guest 里有 bond 说明是客户自配的双 vNIC
  聚合，照常按上节展开
- **虚拟化平台由驱动名暴露**（`ethtool -i`）：virtio_net=KVM、vmxnet3=
  VMware、hv_netvsc=Hyper-V（实测 WSL2 即此）、xen_netfront=Xen——报告
  标注"虚拟机网卡"，判读语境随之：虚拟网卡背后是宿主 vSwitch，guest 内
  missed/drop 高不只代表本机 ring，**可能是宿主侧挤兑**，配 OS steal>0
  （虚拟化层抢 CPU，metrics.md OS 表）联判"宿主机邻居噪声"
- **速率三态**：虚拟网卡可能正常报速率（实测 hv_netvsc 报 10000Mb/s），
  **也可能 Unknown/-1（virtio 老驱动常见）**——Util% 分母不可得时置空并
  注记，判读降级为绝对 B/s + missed/drop 分层

## 判读速查（现象 → 命令 → 机制）

| 现象 | 命令 | 机制指向 |
|---|---|---|
| 连接建立慢/时通时断 | `ss -lnt`（Recv-Q vs Send-Q）；`nstat -az \| grep -E 'ListenOverflow\|ListenDrops\|SynRetrans'` | 两级队列节：accept 不及时 vs backlog 小 |
| 客户端"连上但无响应" | `nstat -az` ListenOverflows + 对端抓包 | tcp_abort_on_overflow=0 的静默丢 ACK（假死连接） |
| 网络吞吐低 CPU si 高 | `/proc/net/softnet_stat` 第 2/3 列；`top` 看 ksoftirqd | backlog 满 vs 预算耗尽（收包路径节） |
| 海量 TIME_WAIT | `ss -s`；应用连接模式 | 60s 常量 + 短连接风暴（TIME_WAIT 节） |
| 锁等在一个"消失"的会话上 | `ss -tnp \| grep <对端IP>`；重传计数 | retries2≈15 分钟才放弃（假死连接节）→ 杀持锁会话 |
| 客户端消失、事务还挂着（膨胀/阻塞根因） | `pg_stat_activity`（idle in transaction + 事务开始时间）+ ss 对端 | 残留三形态表；防线=keepalive GUC 组 + idle_in_transaction_session_timeout |
| 间歇性丢包 | `ethtool -S <nic>` + softnet_stat + conntrack_count | 分层定位：网卡→backlog→连接队列三道关 |
| RTT p95 高 / 批量拉取慢 | `ss -ti`（rtt / rcv_rtt / min_rtt 三值对读） | RTT 节：路径 vs 接收侧 vs 带宽积；Mathis |
| 重传率 3~5% | `nstat -az`（RetransSegs / OutSegs） | SNMP 节：分子含 SYN-ACK 重传，先排全队列堆积再定网络 |
| 连接被 RST 掐断 | `nstat -az` TCPAbortOn* 家族 | TcpExt 节：按 abort 原因分流（超时/内存/应用关闭） |
| 大流量吞吐上不去 | `nstat` TCPMEMORYPRESSURES + `ss -ti` rcv_space | TCP 内存节：窗口×RTT 带宽积限死 |
| 不知道该查哪块网卡 | `ss -tlnp`+`ip addr`（IP→接口）→ `/proc/net/bonding/`（bond 展开 slave） | 网卡层·bond 节：业务口定位三步 + active-backup 备口零流量属正常 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| ListenOverflows 只调大 backlog | 队列满常因应用 accept 慢（线程池满） | 先 ss -lnt 看消费速度，backlog 与 somaxconn 只是缓冲 |
| TIME_WAIT 多调 tw_recycle | 4.12 已删且 NAT 下必然误杀 | 连接池化长连接是根治；tw_reuse 仅出站 |
| 假死连接当数据库 hang | 内核 15 分钟才放弃重传（tcp.h:162-163,271） | ss 看对端状态+重传计数，杀会话放锁 |
| 丢包只查数据库参数 | 丢包在网卡/backlog/连接队列三处，旋钮各不同 | ethtool -S → softnet_stat → nstat 分层 |
| syncookies=2 当"更安全" | 无条件 cookie 丢 TCP 选项（tcp_input.c:7616） | 默认 1（满才降级）即可 |
| conntrack 满当防火墙拦截 | 先淘汰后丢包，是容量问题（nf_conntrack_core.c:205,1463） | count vs max 对账 |
| RetransSegs 全当网络丢包 | SYN-ACK 重传也计入（tcp_output.c:4644），全队列堆积会抬重传率 | 配 ZeroWindow/rcv_rtt/ListenOverflows 分流真因 |
| /proc/net/dev 的 drop 归因 backlog | drop 列=rx_dropped+rx_missed_errors 合并渲染（net-procfs.c:53），ring 满混在里面 | ethtool -S 分层定位（网卡层节） |

## 版本注记

- 基线 7.1-rc4 精读；生产 **env-A = 3.10.0-1160**（CentOS 7）：无
  netdev_budget_usecs（4.5+，只有 budget=300 口径）、tw_recycle 还存在但
  同样不能开（NAT 有毒）、syncookies/两级队列/TIME_WAIT=60s/retries2 跨版本
  一致
- tcp_tw_recycle 于 4.12 移除；netdev_budget_usecs 于 4.5 加入

## 来源

- 内核源码基线 `<linux-7.1-rc4-src>`（7.1-rc4）精读（2026-10-25）：
  net/core/dev.c:7918-7920（net_rx_action 预算）、net/core/sysctl_net_core.c:
  464,622-627、net/core/net-procfs.c:150,332（softnet_stat）、
  net/ipv4/tcp_input.c:7568-7627（syncookies/半连接/LISTENOVERFLOWS）、
  include/net/tcp.h:142,162-163,271,2805、net/ipv4/tcp_minisocks.c:951
  （abort_on_overflow）、net/netfilter/nf_conntrack_core.c:205,1418,1463、
  net/ipv4/tcp.c:3779-3790（keepalive per-sock）
- 生产内核版本实测：env-A uname -r（2026-10-25）
- 二次补读（2026-10-25，全队列堆积节）：include/net/tcp.h:169
  （TCP_TIMEOUT_INIT=1s）、net/ipv4/sysctl_net_ipv4.c:1033
  （tcp_synack_retries 旋钮）、net/ipv4/tcp_timer.c:255-256,468（重传引擎）、
  net/ipv4/inet_connection_sock.c:625-630,649（accept 队列等待与出队）、
  :1141-1166（reqsk 入队挂时器，回调=reqsk_timer_handler :1024,1150 TIMER_PINNED）、
  include/net/tcp.h:180-182（keepalive 默认 7200s/9 次/75s）、
  net/netfilter/nf_conntrack_core.c:1674,1676,507（表满丢包文案）
- 三次补读（2026-10-25，关闭链路节）：net/ipv4/tcp.c:3191,3264
  （SO_LINGER 0/未读数据 → active RST）、net/ipv4/tcp_ipv4.c:717,2331
  （无主包回 RST）、net/ipv4/tcp_timer.c:779,838,842（keepalive 定时器与
  探测）、net/ipv4/tcp_input.c:5361 族（sk_data_ready 唤醒）；openGauss
  基线 guc.cpp:2346（idle_in_transaction_session_timeout）、:2621+
  （tcp_keepalives_idle/interval/count）
- 四次补读（2026-10-27，OS 指标↔源码对账战役）：net/ipv4/tcp_input.c:1070-1130
  （tcp_rtt_estimator Jacobson 算法与 Eifel 变体）、:3492（tcp_ack_update_rtt）、
  net/ipv4/tcp.c:4303-4324（tcp_get_info：rtt/rttvar/rcv_rtt/min_rtt）、
  :305,329-340,5191-5193（tcp_mem 三档与 enter_memory_pressure）、
  :1733,1819,2332,2884（tcp_rcv_space_adjust 调用点）、:3007-3025（CurrEstab
  增减=tcp_set_state）、net/ipv4/tcp_output.c:1698,3644,4051,4387,4644
  （OutSegs/RetransSegs/ActiveOpens 增点）、net/ipv4/tcp_minisocks.c:667
  （PassiveOpens）、net/ipv4/proc.c:140-155,168-289（snmp4_tcp_list 与
  TcpExt 表）、net/ipv4/tcp_input.c:6422,6567,6666 + net/ipv4/tcp_ipv4.c:
  1894,1928（InErrs）、net/core/net-procfs.c:44-53（dev 统计：drop 列合并
  渲染）、net/ethtool/ioctl.c:439,545（link settings）；权限白名单双版本
  对账（v3.10 net/core/ethtool.c:1432-1446 经 git.kernel.org 拉取核对、
  7.1-rc4 net/ethtool/ioctl.c:3264-3278）；WSL2 普通用户实测 ethtool -S /
  ip -s -s link / sysfs statistics 三路全通（2026-10-27）
- 五次补读（2026-10-27，bond 口径）：drivers/net/bonding/bond_main.c:
  5925-5930（bond_ethtool_ops 仅四项→ethtool -S bond 口 EOPNOTSUPP）、
  :4499-4525（bond_get_stats 逐 slave 增量 fold 聚合）、bond_procfs.c:291
  （/proc/net/bonding 0444 普通用户可读）；v3.10 bond_main.c:3511 经
  git.kernel.org 拉取核对（同为遍历聚合）
- 《Linux 性能优化实战》网络篇作主题框架参照（口径以源码为准）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：软中断预算→`cpu.md`、
连接风暴参数→`parameters.md`、假死持锁→`locks.md`/`error-fatal-mechanics.md`、
备库同步网络→`wal-replay.md`、主备延迟分诊→`commit-sync-events.md`。
