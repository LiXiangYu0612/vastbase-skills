# cgroup：CPU 配额 / cpuset / pids / memory / io 控制器（内核源码定论）

面向 DB 判读的五个控制器，全部行级锚点基线 **v7.1-rc4**（2026-11-19 精读，文件级镜像
`/home/xiang/kernel-src/linux-7.1-rc4-mirror`：kernel/sched/fair.c 14312 行、
kernel/sched/core.c 11236 行、kernel/cgroup/cpuset.c 4390 行、kernel/cgroup/pids.c
460 行、mm/memcontrol.c 5958 行、block/blk-throttle.c 1849 行、block/blk-core.c
1294 行、block/blk-mq.c 5349 行）。判读对接：AWR Cgroup 章（awr-report-reference.md）、
OOM 四型（troubleshooting/oom-analysis.md）、wbt 深睡案（io-storage.md）。

## 概述与 v1/v2 判别

- **v1**：每控制器一个挂载（/sys/fs/cgroup/cpu、/memory、/blkio…），进程可同时在
  多个层级；文件名带控制器前缀（cpu.cfs_quota_us、memory.limit_in_bytes、
  blkio.throttle.read_bps_device）。
- **v2**：单挂载 /sys/fs/cgroup（unified），文件扁平（cpu.max、memory.max、io.max）；
  **no-internal-process 约束**：非根 cgroup 有进程时不能开子控制器目录（DB 直挂
  根下的部署方式要过这一关）。
- 判别：`cat /proc/<pid>/cgroup` —— v2 只有 `0::/path` 一行；v1 是多行
  `<n>:<controller>:/path`。`stat -fc %T /sys/fs/cgroup` = `cgroup2fs` 即 v2。
- 生产环境口径：env-A = CentOS 7 **纯 v1**（3.10.0-1160）；openEuler/Kylin 4.19+
  混合（cgroup2fs 可能未挂）；K8S 节点多 v2 或 hybrid。

## cpu.max → CFS bandwidth：配额怎么发、怎么掐（源码主线）

**写入路径**：`cpu.max "$MAX $PERIOD"` → cpu_max_write（core.c:10452）→
tg_set_cfs_bandwidth（core.c:9779）→ **__cfs_schedulable → tg_cfs_schedulable_down
（core.c:9910）层级校验**（见下节）→ 生效。

**运行时账本（fair.c，全在 CFS 带 CONFIG_CFS_BANDWIDTH 的路径）**：

| 步骤 | 函数 | 锚点 | 机制 |
|---|---|---|---|
| 周期充值 | __refill_cfs_bandwidth_runtime | fair.c:5893 | 每 period：`runtime += quota`，**封顶 quota+burst**（burst 是允许滚存的余量） |
| 每 CPU 领款 | assign_cfs_rq_runtime | fair.c:5940 | cfs_rq 按 **slice 批量领**（默认 **5ms**，sysctl `sched_cfs_bandwidth_slice_us`，fair.c:125/137）——不是每 tick 去全局池扣 |
| 消耗记账 | __account_cfs_rq_runtime | fair.c:5959 | `runtime_remaining -= delta_exec`（先扣后判，可跨周期透支成负） |
| 掐判定 | check_cfs_rq_runtime | fair.c:6711 | 余量 ≤0 且未在 throttled → **throttle_cfs_rq** |
| 掐动作 | throttle_cfs_rq | fair.c:6239 | 挂入 cfs_b->throttled_cfs_rq 链表，整组从 rq 摘出（dequeue）——**组内所有任务原地失明** |
| 周期到点 | do_sched_cfs_period_timer | fair.c:6491 | **nr_periods += overrun**（:6501）；期间有 throttled → **nr_throttled += overrun**（:6520）+ distribute_cfs_runtime 按链表序发还 |
| 发还 | distribute_cfs_runtime | fair.c:6403 | 从池里给每个 throttled cfs_rq 补到恰好 >0（补 `-runtime_remaining+1`），**池空即止**——同 period 内先到先得 |
| 解掐 | unthrottle_cfs_rq | fair.c:6280 | **throttled_time += now - throttled_clock**（:6304，被掐时长累计）；重新 enqueue |

**判读要点**：
- throttling 的粒度是**周期边界**：一个 period 内配额提前烧完 → 整组掐到下个 period
  开头（最坏掐满一个 period）。cpu.max=200000/100000（2 核）多线程场景：8 线程并发
  可在 25ms 内烧光 200ms 预算 → **每个 100ms 周期掐 ~75ms**——库内表现=周期性批量卡顿。
- slice=5ms 意味着单 CPU 每次最多透支 5ms 才被发现——`throttled_usec` 里包含这部分。

## cpu.stat 口径（判读主数据源）

v2 `cpu.stat`（core.c cpu_extra_stat_show :10301-10327 + cpu_local_stat_show
:10329-10342）：

```
usage_usec      # 本组+子孙累计 CPU 时间
nr_periods      # 经历周期数（含 overrun 补记）
nr_throttled    # 有被掐经历的周期数          ← 含子孙（cfs_b 是组级账本）
throttled_usec  # ★两处同名不同义，见下
nr_bursts / burst_usec  # burst 动用次数/时长
```

**★同名陷阱（源码坐实）**：cpu.stat 里的 `throttled_usec` 来自
**cpu_local_stat_show（:10329）= throttled_time_self = 只算本组自身、不含子组**；
而 nr_periods/nr_throttled 来自组级 cfs_bandwidth 账本（**含子孙**）。K8S podcg
嵌套（pod.slice → container subgroup）读父层 throttled_usec=0 但子组在掐——
**读 throttling 必须下钻到叶子组，或在父层只看 nr_throttled**。

v1 文件名（core.c:10197 cpu_legacy_files）：`cpu.cfs_quota_us`（**-1 = unlimited**，
不是 0）、`cpu.cfs_period_us`、`cpu.cfs_burst_us`（3.10 无 burst，v5.4+ 才有）、
`cpu.stat`（nr_periods/nr_throttled/**throttled_time 纳秒**，无 usec 换算——比对
脚本别拿 v1 的 ns 当 v2 的 usec）。

**AWR Cgroup 章对接判读**（已实采 Begin/End/Δ）：
- **nr_throttled/throttled_time Δ>0 = CPU 配额真咬**——库内"慢但 OS busy 不高"的
  P2 必查位（bottleneck-patterns 形态表分流）；
- Δ 口径失效场景：**cgroup 重建**（容器重启/K8S 重建 pod）计数器清零 → Δ 为负或
  空洞，判 `reset` 重锚，别当"没掐"。

## 层级与嵌套（K8S/systemd）：子配额为什么"写大也没用"

tg_cfs_schedulable_down（core.c:9910-9945）**逐层向下收敛 hierarchical_quota**：

- **v2**：子未设 quota（RUNTIME_INF）→ **继承父 quota**；子设了 → `min(子, 父)`
  （:9936-9938）——**子配额永远不可能超过最近设了限额的祖先**；
- **v1**：子设的 quota > 父的 quota → 写入直接 **-EINVAL 拒绝**（:9941）。

判读：QoS 划了 slice（systemd `CPUMax=`）再在 DB 进程组上放宽 cpu.max **不会生效**
（被上层 min 钳住）——必须从设置了限制的最高层往下捋（`cat /proc/<pid>/cgroup`
逐层看 cpu.max，找到真正咬人的那一层）。

## cpuset：CPU/内存节点绑定与调度域代价

（kernel/cgroup/cpuset.c；v1 目录 cpu.cpuset，v2 文件 cpuset.cpus）

- `cpuset.cpus` vs `cpuset.cpus.effective`：**effective 是全链交集后的真值**
  （父层改窄/热插拔删核 → 子层申请不再被满足，effective 收缩但申请值不动）——
  **判读永远看 effective**；`cpuset.mems.effective` 同理（NUMA 内存节点绑定，
  与 memory.md NUMA 章联动）。
- validate_change（cpuset.c:707）：不能出现空集（除非 cgroup 无任务）；
  **cpuset_update_tasks_cpumask（:1042）**：改 cpus 后把组内所有任务的
  affinity 改写（DB 后端线程会被批量迁移——生产高峰改 cpuset = 全组线程大迁徙）。
- **rebuild_sched_domains（cpuset.c:1027 → core.c）**：exclusive/隔离拓扑变化时
  **全系统调度域重建**——负载均衡短暂停摆，多核机有感（判读干扰项，别当库故障）。
- v2 `cpuset.cpus.partition`（PRS_* 状态机，cpuset.c:237-264）：partition root
  把 CPU 从全局调度域摘出（隔离），DB 专用核场景用；partition 变更同样触发调度域
  重建。
- 与 cpu.max 的关系：cpuset 管你在**哪些核**上跑（硬裁），cpu.max 管**总共用多少**
  （时间预算）——两者叠加时 throttling 判读先看 cpuset（核不够 ≠ 配额咬，
  表现完全不同：核不够=持续排队无 throttle 计数）。

## pids 控制器：连接/进程数限额

（kernel/cgroup/pids.c，v1 cgroup.procs 同名文件）

- **pids_try_charge（pids.c:166）**：fork 时**从叶到根逐级祖先 charge**
  （每级 counter += 1 并对各自 limit 检查），任何一级超限 → 全链 revert →
  **fork 返回 -EAGAIN**（与 RLIMIT_NPROC 同错误码：`Resource temporarily
  unavailable` / PG 报 `could not fork new process`）。
- **pids_event（pids.c:243）**：每级**首次**撞限打一条内核日志
  `cgroup: fork rejected by pids controller in <path>`（journalctl -k 可查）；
  v2 events 沿父链累加（PIDCG_MAX）——**父层 pids.events 里的 max 可能是子孙撞的**。
- 文件：pids.max（"max"=无限，pids.c:39）、pids.current、pids.peak（高水位）。
- DB 场景：session 风暴/并行 worker 爆炸撞 pids.max 的形态=**业务报连接失败但库
  内无等待事件**（fork 都没成功，ASR/ASP 里看不到）——AWR Cgroup 章 pids.current
  行对 pids.max 的逼近度是前兆指标。

## memory 控制器要害（判读向；swappiness 终局见 memory.md）

（mm/memcontrol.c）

**memory.high = 软限（回收+罚睡），memory.max = 硬限（回收+OOM kill）**——机制
分叉点都在 charge 路径 try_charge_memcg（:2567）：

| 限额 | 源码路径 | 行为 |
|---|---|---|
| memory.high | reclaim_high（:2300）：超 high → **MEMCG_HIGH 事件（:2313）** + 同步回收 try_to_free_mem_cgroup_pages + **psi_memstall_enter**（★PSI memory stall 的来源之一，cpu.md PSI 节联动）→ 沿父链逐级（父也超也记事件）；回收跟不上 → __mem_cgroup_handle_over_high（:2477）**按超额比例罚睡 allocating 任务**（calculate_high_delay，上限 MEMCG_MAX_HIGH_DELAY_JIFFIES = 2×HZ，:2340） | **不杀进程**：表现为周期性停顿/延迟抖动，库内无错误 |
| memory.max | try_charge_memcg 重试回收 → mem_cgroup_oom（:1918）→ **MEMCG_OOM（:1925）** → cgroup 内 OOM kill（挑组内最大 RSS 进程杀） | **杀进程**：库进程被杀 + dmesg `Memory cgroup out of memory`；v1=OOM 被 cgroup 化，全局 OOM killer 不介入 |

- **memory.events（v2）**：`__memcg_memory_event（:1854）`——`events.local` 只记
  本组、`events` 含子孙沿链传播；判 **oom_kill 绝对值>0（不看 Δ）**——OOM 后进程
  没了取不到 End 快照、cgroup 重建计数器清零，**Δ 口径会埋掉事件**（AWR 章已按
  绝对值采）。low/high/max/oom/oom_kill 五键（low/max 是事件计数，high 同）。
- v1：memory.failcnt（memcontrol.c:1801 附近读出）= 撞 limit_in_bytes 次数，
  memsw.limit_in_bytes = swap 计入版硬限（v2 无 memsw，swap 用 memory.swap.max）。
- swappiness：**v2 无 per-memcg 旋钮（恒全局 vm_swappiness）**，v1 有
  memory.swappiness——完整源码终局与四文件锚点在 memory.md「swappiness」节，此处
  不重复。

## io 控制器：io.max / io.cost / io.latency 与 wbt 的分层

v2 三件 + v1 blkio，**全部在 submit 路径上，与 wbt 是两层不同机制**：

| 控制器 | 内核实体 | 写入口 | 锚点 | 机制 |
|---|---|---|---|---|
| io.max（v1: blkio.throttle.*_bps_device） | blk-throttle | tg_set_limit | blk-throttle.c:1527 | **rbps/wbps/riops/wiops 四键**（:1571-1584 解析；**写 0 拒绝 -ERANGE** :1567，解限=删键或 "max"）；按 **throtl slice（HZ/10=100ms，:24 DFL_THROTL_SLICE）** 窗口内令牌化排队 |
| io.cost | blk-iocost | ioc_qos_write / ioc_cost_model_write | blk-iocost.c:3225/:3417 | 基于成本模型的加权带宽分配（v2 only） |
| io.latency | blk-iolatency | blk-iolatency.c（1068 行） | — | 延迟目标驱动反压 |

**★hook 次序（源码坐实，两层叠加判读的根）**：

```
submit_bio_noacct（blk-core.c:882）
  └─ blk_throtl_bio()   ← io.max 在这里：超限 bio 直接滞留（__blk_throtl_bio
                           blk-throttle.c:1730 起，排队不报错），过了才放行
      └─ blk_mq_submit_bio（blk-mq.c:3141）
          └─ rq_qos_throttle（blk-mq.c:3063/:3110） ← wbt/iocost/iolatency
              在这层（rq_qos 家族，blk-rq-qos.c）
```

- **io.max 与 wbt 是先后两层**：io.max 卡在 cgroup 层（bio 级滞留，iostat 上表现为
  该 cgroup 的 IOPS 被削平在限额值）；wbt 卡在队列深度层（rq 级深睡，
  io-storage.md wbt 深睡案）。**双层都配时先撞 io.max 的看不到 wbt**——排查 IO 慢
  先查 `/sys/fs/cgroup/*/io.max` 再看 wbt，别把 io.max 的削顶当 wbt 病。
- iocost 与 wbt **同为 rq_qos，互斥启用**（一个队列只能一个主人）；blk-throttle
  与二者可叠加。

## 判读速查（现象 → 查什么 → 机制）

| 现象 | 查 | 机制锚 |
|---|---|---|
| 库慢/延迟抖，OS busy 不高 | cpu.stat nr_throttled/throttled Δ | CFS bandwidth 掐（fair.c:6711） |
| throttled_usec=0 但行为像被掐 | **下钻子组**看（本组口径不含子孙） | core.c:10329 throttled_time_self |
| 子层放宽 cpu.max 无效 | 逐层看 cpu.max（v2 min 继承/v1 EINVAL） | core.c:9910 |
| 业务连不上但库内无等待 | pids.current→pids.max、journalctl -k "fork rejected" | pids.c:256/:243 |
| 周期性停顿、PSI memory 涨、无报错 | memory.high、memory.events high 行 | memcontrol.c:2299/:2477 |
| 进程被杀、dmesg "Memory cgroup out of memory" | memory.events oom_kill **绝对值**、memory.max | memcontrol.c:1918/:1925 |
| IO 慢且 IOPS 被削平在某整数 | io.max（rbps/riops） | blk-throttle.c:1527 |
| 改 cpuset 后全机短暂抖 | 调度域重建 + 线程大迁徙 | cpuset.c:1027/:1042 |

## 常见错误

- 把 v1 `cpu.cfs_quota_us = 0` 当 unlimited——**是 -1**；0 会因层级校验语义混乱。
- 把 v1 cpu.stat 的 throttled_time（**ns**）当 v2 throttled_usec（µs）比——差
  1000 倍。
- 在父层读 throttled_usec=0 就断言"没被掐"（不含子孙，core.c:10329）。
- 看到 memory.events 里有 high 就当出事——high 是软限正常工作痕迹，**只有
  oom_kill>0 或 max 持续涨才升级**。
- cgroup 重建后拿旧 Begin 快照算 Δ（计数器清零=负 Δ，判 reset 重锚）。
- io.max 限额写 0 想禁用（-ERANGE 拒绝）；删键才是解限。

## 版本注记

- 基线 7.1-rc4 行级精读（2026-11-19，文件级镜像）；生产 env-A = CentOS 7
  3.10.0-1160 **纯 v1**：有 CFS bandwidth（3.2+）、有 memory.high/max、
  **无 cpu burst**（v5.4+）、无 v2 io.*、无 pids.peak；3.10 的
  sched_cfs_bandwidth_slice 同为 5ms 可调。
- cgroup v2 swappiness 终局（v2 无旋钮恒全局）：memory.md「swappiness」节，
  四文件锚点在彼处。
- K8S 限额映射（判读辅助，工程口径）：requests → cpu.weight/内存无硬件；
  limits.cpu → cpu.max（quota=limits×100000µs）；limits.memory → memory.max。
  QoS class 体现在同层竞争权重，不体现在 throttle 行为差异。

## 来源

- v7.1-rc4 源码行级精读（2026-11-19）：kernel/sched/fair.c（throttle/distribute/
  period_timer/账本全链）、kernel/sched/core.c（cpu_max/tg_set_cfs_schedulable/
  cpu.stat 双 show）、kernel/cgroup/cpuset.c、kernel/cgroup/pids.c、
  mm/memcontrol.c（high/max 分叉/events）、block/blk-throttle.c、block/blk-core.c
  :882、block/blk-mq.c:3063/3110、block/blk-iocost.c、block/blk-iolatency.c、
  block/blk-wbt.c（rq_qos 家族比对）——镜像 /home/xiang/kernel-src/linux-7.1-rc4-mirror
- AWR Cgroup 章口径（vb_tool 侧 Begin/End/Δ 采集，2026-11-01 落地）：
  performance/awr-report-reference.md Cgroup 行
- 交叉引用：memory.md（swappiness v1/v2 终局、NUMA 章 mems.effective）、
  cpu.md（PSI 节 memstall）、io-storage.md（wbt 深睡案）、
  troubleshooting/oom-analysis.md（OOM 四型之 cgroup 型）
