# OS 资源四域诊断树（cpu / network / io / memory 跨层联判入口）

## 概述

> 定位：**四域共同的路由层**——症状进树、根因出树；深度判读一律下钻到
> `cpu.md` / `network.md` / `io-storage.md` / `memory.md`，本文件不复制内容，
> 只负责"先看哪列、走哪支、跨层怎么联判"。
> 起源：2026-11-03 实战案例（st=70% 案）——全套 socket 层症状的根因在
> 虚拟化层，围着网络调参查一天全是空的。本树就是那个教训的结构化。

## 原理基础：四域为什么必然互相关联（跨层联判的合法性）

四域不是四个独立系统，是**同一台机器的四张记账视图**，被三条纽带焊死：

1. **同一本 CPU 时间账**：us/sy/wa/st/si 是同一颗 vCPU 时间的五个科目
   （原始账本 /proc/stat 同一行）——wa 是"CPU 等 IO 域"的投影、si 是网络
   收包工作的投影、其他域的内核工作（协议栈/回收/调度）全记在 sy。
   **看五列能窥见四域 = 记账结构使然**，这就是树根用 30 秒基线的原理依据
2. **同一条数据通路的背压传播**：字节的一生穿越收包(si)→调度(sy)→计算(us)
   →缓冲(memory)→块层(IO)→中断→回写→协议栈——任一层慢必以背压上传，
   多域计数器同时留痕（"症状层≠根因层"的机制本体；库内侧同构见
   `../internals/kernel-data-path.md`，本树是其 OS 侧）
3. **内存是公共介质**：page cache=IO 缓存层（回收必写回→假 IO 瓶颈根源）、
   sk_buff/tcp_mem/conntrack=网络吃内存、线程栈反复创建也吃内存

⇒ 四域两两无"无关对"，任意单域症状其余三域基线必有话可说——
组合拳指纹表不是启发式，是结构必然。

## 三条铁律

1. **症状层 ≠ 根因层**：网络症状（CLOSE_WAIT/队列积压/重传）常是 CPU/内存/
   IO 的下游投影。定性之前先拿 30 秒基线，禁止跳过基线直接在症状层下结论
2. **五列看全：us / sy / id / wa / st**。st 在物理机恒 0、最没肌肉记忆，
   但它是"库无罪"的铁证列（cpu.md）；且 st 高时 us 反而低——只盯 us/sy
   会被"CPU 不忙"骗过（本案实况）
3. **网络计数器是累计口径**：ListenOverflows/SYN 丢弃/重传都是自开机累加，
   定性"进行中 vs 历史存量"必须隔时段采两次看增量（本案百万级溢出即存量）

## 树根：30 秒基线（任何"慢/卡/超时"的第一动作）

```bash
vmstat 1 5                 # r(运行队列) b(D状态) sy us id wa st —— 五列一次看全
top -H -p $(pidof vastbase) | head -25   # 谁在吃 CPU；线程形态 R/S/D
iostat -x 1 3              # await + IOPS（util 一列不看，见 E 支）
ss -s ; ss -tn state close-wait | head   # socket 症状面
```

## 分支表（五列 → 走支）

| 基线读数 | 走支 | 一句话定性 |
|---|---|---|
| **st > 0**（持续） | **A** | 宿主抢占，库无罪，结论上交虚拟化层 |
| us 高 | B | 用户态在烧：谁烧（会话/后台线程）+ 烧在哪（gs_stack） |
| sy 高 | C | 内核态：调度/软中断/信号量/系统调用 |
| wa 高 | D→E | 等 IO：必须 iostat await 证实，wa 单独不作数（iowait 双刃） |
| 五列都"正常"但就是慢 | G + 库内 | OS 无罪后查网络真层与库内（锁/等待事件） |

## A 支：st（steal）——虚拟化抢占

- 判据：vmstat/top st 列持续 >0（cpu.md：**steal>0 = 不是库的问题的铁证**；
  域内不设分级阈值，>0 即归因宿主侧——本案 70% 为灾难级实例）
- **下游投影全链（本案实证）**：vCPU 欠载 → postmaster accept 消费慢
  （TCP+unix socket 通杀）→ listen Recv-Q 积压 → 客户端建链头几毫秒超时
  （日志 `failed to send SSL negotiation response: Broken pipe`）→
  单一应用 IP 的 CLOSE_WAIT 堆积（大量躺在内核 backlog，无线程无会话）→
  客户端重试风暴正反馈
- 处置：迁实例/找虚拟化平台（带 st 曲线+时间窗开单）；短期掐客户端重试
  （指数退避）；**库内调参全部无效**
- 下钻：`cpu.md` Steal 节 ↔ `network.md` CLOSE_WAIT 节（指纹互见）

## B 支：us 高——用户态

- 谁在烧：`top -H` 定线程 → 库内会话线程（长 SQL/自旋/排序聚合）vs
  常驻后台线程（pagewriter/ckpt/autovacuum/WLM，名字可辨）
- 烧在哪：gs_stack 取栈（执行器算子？锁自旋？内存回收路径？）
- **对网络的投影**：CPU 满时 accept+认证+协议栈全部降速，建链慢/超时/
  CLOSE_WAIT 反复建断是伴生（与 A 支同形，区别在 st=0）
- 特殊形态：锁自旋（`locks.md`——自旋不交接；`sysv-semaphore-usage.md`
  区分热睡眠路径）；脏页淘汰压力路径（`buffer-manager.md`）

## C 支：sy 高——内核态

| sy 高伴随 | 定性 | 下钻 |
|---|---|---|
| semtimedop 可见 | SysV 信号量等待（kernel.sem 是限额非成本） | `sysv-semaphore-usage.md` |
| si 高 / ksoftirqd 打满 | 收包软中断——**表面网络慢实际 CPU 被软中断吃** | `cpu.md` si 节 + `network.md` 收包路径 |
| cs + nivcswch 飙 | 非自愿切换=CPU 配额不够/挤兑（vmstat 无 PSI 用 r+nvcswch） | `cpu.md` 切换节 |
| 系统调用风暴 | strace/perf 定（短连接=accept/close 密集时合理） | `tracing-tools.md` |

## D→E 支：wa 高 → IO 判读（铁律版）

1. **util 永不作为判据**（2026-10-28 用户令）：SSD 多通道/NCQ 下 util 100%
   仍有大量余量，多队列设备 util 没有意义——判盘**只看 await + 绝对
   IOPS/吞吐**，avgqu-sz 辅助
2. **await 分介质**：SSD >5ms 异常 / HDD >20ms 异常；混盘分别说（io-storage.md）
3. await 高 + avgqu 低 = 设备侧慢（远端/虚拟盘/宿主挤兑——虚拟机语境先回 A 支
   对 st！）；await 高 + avgqu 高 = 排队（并发压满）
4. wa 高但 await 不高 = iowait 双刃口径假象（分摊空闲），不是 IO 瓶颈
5. dm/多路径设备要穿透到物理盘看；库侧 DataFileRead 高必核 shared_buffers
   三层内存对账（AWR 版式令——很多"IO 慢"其实是缓冲不够）

## F 支：memory

- **只有 MemAvailable 值得看**（free 别的列都口径陷阱，memory.md）；
  真换出看 vmstat si/so 或 pswpout（不是 swappiness 配置）
- **两层闸门**：OS 层（水位/kswapd/OOM kill）vs 库内层（53200/逻辑内存上限）
  ——分别归因，别混
- **跨层陷阱**：内存压力 → direct reclaim → IO 风暴 → wa 高（**假 IO 瓶颈**，
  先看 pswpout/reclaim 再判盘）；反向：socket 缓冲/tcp_mem 也能吃内存
- 库内配比：work_mem 是**算子级**配额非会话级（mechanism-source-first 案）；
  内存池之和 vs max_process_memory 对账（parameters.md 四查）

## G 支：network 真层（OS 无罪后才轮到）

| 症状 | 语义 | 判读 |
|---|---|---|
| listen Recv-Q 积压 | accept 消费不动 | 先回 A/B 支（消费者是 postmaster，CPU 供不上是主因） |
| CLOSE_WAIT 堆积 | 本端没 close | **单端集中=对端反复建断+本端慢**（本案指纹）；分散多端=普遍冻结；人口学三子群见 network.md |
| TIME_WAIT 多 | 主动关方正常形态 | 高并发短连的常态；量大才看复用/应用侧长连 |
| Retrans% 抬升 | 数据+SYN-ACK 重传（含口径） | st/await 都正常才认真查网络层；看趋势对比基线 |
| conntrack 满 | 新建连接被丢 | nf_conntrack 计数与上限对账（network.md） |
| ListenOverflows 动 | accept 队列溢出进行中 | 累计口径！两次采样看增量；动=风暴进行中，冻结=历史疤 |

## CPU ↔ Network 关联详析（本案沉淀的通路表）

| 通路 | 机制 | 表面症状 |
|---|---|---|
| ① st/欠 CPU → accept | postmaster 单线程消费 accept，CPU 供不上 → 建链全套 | Recv-Q 积压/溢出/建链超时/SSL 应答 EPIPE |
| ② 欠 CPU → 认证慢 | clone 线程+认证要 CPU | "能连但卡"（TCP/unix socket 通杀） |
| ③ us 满 → 软中断饿 | 协议栈处理降速 | 重传/收包丢——"网络抖"实为 CPU 抖 |
| ④ si 高 | 收包软中断吃 CPU | 看着网络慢，实为 softirq 单核打满 |
| ⑤ CPU 慢 → 应用侧超时反复建断 | 客户端心跳/连接超时 → 断连重连反压 | 单端 CLOSE_WAIT 堆积+重试风暴，倒过来再压垮 DB |

（反向关联：E 支 IO 慢 → D 状态抬 load、wa 高；F 支 reclaim → 假 IO；
G 支 tcp_mem → 内存。四域两两都有投影，**看到任何单域症状，先扫其余三域基线**）

## 组合拳指纹速查

| 指纹组合 | 根因指向 |
|---|---|
| CLOSE_WAIT 单端堆积 + listen 积压 + unix socket 卡 + st>0 | **A 支：宿主抢占**（本案） |
| CLOSE_WAIT 分散多端 + 会话集体 active 挂死 | 单点冻结：锁/IO/wbt（blocking-live + gs_stack） |
| wa 高 + await 高 + pswpout>0 | F→E：内存回收型假 IO 瓶颈 |
| sy 高 + semtimedop | SysV 信号量（kernel.sem 限额） |
| si 高 + 单核打满 + 丢包 | 软中断收包（RPS/网卡队列核查） |
| Retrans% 高 + st/id/await 全正常 | 才是真网络层（G 支认真查） |
| "慢"但五列全正常 + 库内等待集中 | 库内问题（等待事件/锁），OS 出局 |

## 版本注记 / 来源

- 阈值与口径全部引自域内已核文件：await 分档（io-storage.md:224）、util 禁令
  （feedback_util_never_judge_io）、steal 铁证（cpu.md）、MemAvailable/pswpout
  （memory.md）、semtimedop（sysv-semaphore-usage.md）、nvcswch（cpu.md）、
  CLOSE_WAIT 人口学（network.md，2026-11-03 实战）
- 本文件 2026-11-03 立（st=70% 案结案后沉淀）；结构=路由层不复制内容，
  各支下钻目标文件为准
- 实证锚：st=70% 案例（socket 全套症状一根到底）；历次 AWR 案例见 `../cases/`

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（OS 四域枢纽节点）。
