# 指标口径与判读

## 概述

vb_tool 生成的 VastBase AWR 报告中各指标的含义、口径与陷阱。判读任何占比、速率、
时间模型数字之前先读「核心口径」；章节与列的位置查 `awr-report-reference.md`；
取证路径查 `bottleneck-patterns.md`。健康参考是经验起点不是硬阈值——任何指标
都不能孤立下结论。

## 核心口径（判读前提，2026-10-24 实测定调）

**阈值双轨（2026-10-27 立）**：判读一个序列同时取两条轨——绝对轨（本文件的健康
参考/介质阈值）与相对轨（窗内中位、分位、同窗相邻桶的倍数）。绝对轨抓"到了公认
坏水位"，相对轨抓"缓变与尖刺"——绝对值不高但为窗内中位 4 倍的 await 同样是异常。
结论注明依据哪条轨（例：await=8ms 未过 SSD 绝对轨、为窗内中位 4 倍 ⇒ 相对轨异常）。
两轨都不过才算正常（方法参照 DBAIOps arXiv 2508.01136 的动态基线思想，
窗内分位是单窗报告里的可行近似）。

VastBase 时间模型未区分前后台，直接套用传统 AWR 判读会系统性误判：

1. **CPU_TIME 含后台线程**（pagewriter/walwriter/autovacuum/检查点……），
   不是纯前台执行时间；"DB CPU/DB Time" 只能当方向
2. **等待事件不分前台/后台**：后台线程的等待同样进事件榜
3. **%DBtime = 事件时间/DB_TIME，并行下可 >100%，各类相加无意义**

可靠的替代口径：AAS 与每核比、ASP 桶逐秒采样计数、OS 序列（busy/iowait/r/b、
设备 await）、库侧 iostat 与 OS 侧对账。CPU 型结论要 OS busy+r 佐证；
等待型结论要 ASP 桶计数佐证。

## 核心指标

- **DB Time**：gs_instance_time DB_TIME 窗口 Δ（μs）——一切库内比例的分母
  （含后台，见核心口径）
- **AAS = DB Time / Elapsed**；每核 = AAS / Logical CPUs
- **★AAS 与 ASP 不一致时，先怀疑 ASP 的"平均值"，别急着否定 DB Time
  （2026-10-26 定论）**：AAS 由 DB Time 派生（内核累计计数器），ASP 是独立的秒级采样。
  两者对不上时按下面顺序判：
  1. **比"量级"不比"均值"**：ASP 的 `max_active_sess` 与 AAS 同量级（样本：max 2,065
     vs AAS 1,623.5）⇒ **AAS 可信**；ASP 的 `avg_sess` 被采样能力压低——几千会话时
     单线程采样器一个周期扫不完一圈，平均值系统性偏低（同窗 avg 仅 180）
  2. **别用"每核 AAS>1"下"CPU 供不应求"**：高 AAS + 低 CPU 只说明命令内在等
  3. **DB Time 与事件的巨大缺口，先想"没埋点的等待"**：现场 RECV-Q 堆积
     1,000~2,000 时，命令内的 socket 写阻塞没有任何等待事件标签，
     这部分时间只进 DB Time、不进事件榜（样本：CPU 3.27%、事件合计 1.27%、
     时间模型各阶段 23.7%，余 76% 无归属 —— 不是记账错，是网络侧无埋点）
  4. 只有"CPU_TIME ↔ OS busy 也对不上"时才怀疑 DB Time 本身
     （CPU_TIME 来自 `getCpuTime()` 实测，与 OS busy×核数 对得上就说明它真）
- negative delta WARNING 行出现（计数器回卷：重启/instr_unique_sql_count 重置）
  ⇒ 相应 Δ 作废

## DB Time 的计时边界（源码定论，2026-10-26）

**计时区间 = 从"收到命令"到"该命令处理完"，不含会话建立与空闲。**

- 开表：`postgres.cpp PostgresMain` 主循环在 `ReadCommand()` 返回**之后**才调
  `timeInfoRecordStart()`（:8975），命令结束 `timeInfoRecordEnd()`（:8809/:8868）；
  `og_record_time.cpp start_first_record_opt()` 幂等，首条命令才真正开表
- 汇总：会话级数组经 `pgstat.cpp update_sql_state()` 累加到 `mySessionTimeEntry`
  （:8414），会话登出并入 `gInstanceTimeInfo`（:8297）；实例值 = 活跃会话条目 +
  已登出累积（`getInstanceTimeStatus` :8455）
- **不在 DB Time 内**：线程/进程派生、StartupPacket 与认证、InitPostgres 与
  catalog 预载（都在首次 `ReadCommand()` 之前）；两次命令之间的空闲；
  **客户端侧排队（RECV-Q / accept backlog 里的连接，还没被 backend 读到）**；
  会话退出清理（`sinvaladt.cpp CleanupWorkSessionInvalidation`）
- **在 DB Time 内但没有等待事件标签**：命令处理中的结果集发送/`write()` 阻塞
  （客户端消费慢、接收窗口满时 backend 会阻塞在 socket 写上；`NET_SEND_TIME`
  需 `enable_record_nettime`+驱动支持才计）。这是"高 AAS + 低 CPU + 低事件占比"
  最常见的组合来源
- 三条推论：
  1. **连接风暴不抬 DB Time**——被准入闸拒绝的连接全程 0，成功建立的会话其初始化
     成本也不计
  2. 初始化/退出的 CPU 是真实消耗，但只落在 OS 侧（busy/ctxt），与 DB CPU 不同分母，
     不能直接对账
  3. 会话退出持锁造成的等待，记在**等锁的那条命令**上（受害者），所以 DB Time 里
     看得到等待、看不到持锁

## OS Statistics

每行的"机制"列挂 `../linux/` 对应文件——口径争议到机制层定论，不在此层争（2026-10-27 补链；10-28 按 vb_tool master 采集清单收口：27 项 OS 采集文件逐一双向对账，softnet/RX Missed/bond/Slab/db-process/filesystem/sysctl/file-nr/load+intr 全部入表；2026-10-30 复对账 master 6b17d22：快照文件仍 27 项不变，**每分钟行 14 类——新增 dbfd**（DB Process 折表 fd 三桶），TCP Sockets/Conntrack/RX Missed 独立呈现取消、并入 DB Process 折表与网卡表，TCP Statistics 独立成章；2026-11-01 复对账 master de5d3f0/v2.5.20：**快照文件 31 项**（27 基线 + irq_affinity/irq_node + db_numa_maps/db_cgroup），Cgroup 新子节、NUMA 深补、Softirq/IRQ per-CPU 转置矩阵入表）。

| 指标 | 含义 | 关注点 | 机制 |
|------|------|--------|------|
| CPU Model / Sockets / Cores / Threads / **Platform** | 硬件身份（/proc/cpuinfo + DMI） | **Platform 字段=显式虚拟化判定**（Physical / Virtual(<vendor>) / Virtual(hypervisor)，见 cpu.md 虚拟化节）；超线程=逻辑核 2×物理核，这是 AAS/N 的分母 | `../linux/cpu.md`（CPU 身份与虚拟化判定） |
| Busy/User/Nice/System/Iowait/IRQ/Softirq/Steal/Idle | 窗口 CPU 分布（九列） | User+System 持续>80 且库内 CPU 低 → 非库进程吃 CPU；**Nice=低优先后台**（备份/ETL/杀毒）；**IRQ+Softirq 高=中断/软中断风暴**（收包过载）；**Steal>0=虚拟化层抢 CPU，不是库的问题** | `../linux/cpu.md`（四口径+软中断预算+虚拟化判定） |
| Iowait | IO 等待 CPU 占比 | >10% 且 physical reads 高 → 存储慢（与设备 await 联查） | `../linux/cpu.md`（wa 双刃口径） |
| r（运行队列） | 等 CPU 任务数 | 持续>N=CPU 饱和 | `../linux/cpu.md`（负载本质 nr_running、调度类） |
| b（阻塞队列） | D 状态任务 | b 高+Iowait 高=存储层拥塞 | `../linux/cpu.md`（nr_uninterruptible）+ `../linux/io-storage.md` |
| NUMA Local Ratio | 每节点本地内存访问占比（numastat，2026-10-31 起与拓扑合并单表+DB Pages/DB Share 列=postmaster numa_maps 每节点 faulted 页聚合） | 多路机必看：**<90% 且 miss 持续涨=跨节点访问**（远程内存延迟 1.5~2×）；dimo 实例 Node0 86.15% vs Node1 99.60%=失衡（见 memory.md NUMA 节）；**配 DB Share 列看库内存落点**（字节加权口径；DB Pages 列数原始页数，混页大小实例两列不等价）；Auto NUMA Balancing 表 hint_faults 大=内核在搬页；单节点机无 Auto Balancing/Placement 表 | `../linux/memory.md`（NUMA 拓扑与平衡） |
| Cgroup 配额/限流（2026-11-01 新子节） | Metric/Begin/End/Delta 单表：配置行（cpu.max/quota、memory.limit、memsw.limit、pids.max、cpuset.*）+ 累计行（cpuacct.usage、memory.usage、memory.failcnt、pids.current）+ cpu.stat 行 + memory.events 行（v2：low/high/max/oom/oom_kill）+ blkio 行 | **cpu.stat 的 nr_throttled/throttled_time Δ>0 = CPU 配额真咬**——库慢/延迟抖但 OS busy 不高的容器化交付经典根因（P2 必查）；**memory.events oom_kill 是绝对值口径**（OOM 后无 End 快照、重启后 cgroup 重建清零，Δ 会埋掉事件），>0 即发生过 cgroup OOM kill；memory.usage 逼近 memory.limit=限流前兆；计数器倒退=`reset`=cgroup 重建 | `../linux/cpu.md`（cgroup 配额调度）+ `../linux/memory.md`（memcg 两层闸） |
| MemAvailable | 可用内存 gauge | <10% 危；配 WM Free<WM Min=kswapd 前兆 | `../linux/memory.md`（free 口径、水位线三道闸） |
| pswpout / pgmajfault 每秒 | 换出/缺页 | >0.5/s、>50/s=内存真不足（pgpgin/pgfault 是正常负载代理不算异常） | `../linux/memory.md`（kswapd 回收、OOM 全链路） |
| Dirty（meminfo） | 脏页 gauge | 持续大=OS 写回压力 | `../linux/io-storage.md`（写路径三闸门/双层缓冲） |
| Slab / SReclaimable / SUnreclaim | 内核对象内存三值 | **SUnreclaim 单调涨不回落=内核对象泄漏**（fd 关了对象没释放，与 fd 泄漏是两层）；SReclaimable 高=可回收（dentry/inode 缓存），正常 | `../linux/memory.md`（Slab 增长与内核对象泄漏） |
| Buffers / Cached / Active(anon/file) / Inactive | 页缓存与匿名页构成 | 判读看构成不看总量：file 占大头=页缓存（可回收）；anon 涨=进程真吃内存（不可回收只能换出）；**Cached 天然含 Shmem**（源码定论），Shmem 大时别把 Cached 全当可回收 | `../linux/memory.md`（回收对象双 LRU、Shmem 计入 Cached 的口径） |
| 设备 r/w_await | 每次 IO 平均等待 ms | **按 Rot 介质分档（2026-10-27 用户令收紧）**：SSD>5 异常、HDD>20 异常；绝对 IOPS 辅助（HDD 100+ IOPS 接近极限） | `../linux/io-storage.md`（iostat 口径） |
| avgqu-sz | 设备队列深 | 判盘只看 await（介质分档）与绝对 IOPS；avgqu-sz 辅助；**%util 永不作判读标准（2026-10-28 用户令：这参数没啥价值）** | `../linux/io-storage.md`（util 口径陷阱） |
| Rot / Sched / QDepth / WC | 盘体身份 | Rot 0=SSD 1=HDD；判 IO 前先定介质 | `../linux/io-storage.md`（块层 multiqueue/调度器） |
| ctxt/s | 上下文切换 | >100K 且同步等待高=锁竞争 | `../linux/cpu.md`（自愿/非自愿切换与 ctxt 计数） |
| interrupts/s + Softirq/IRQ per-CPU 矩阵 | System Activity 全局行 + 两块 per-CPU 转置矩阵（2026-10-31 定形：CPU 行×活跃类别编号列，全零类别不占列，类型对应表给名称/设备，每核 %sirq/%irq 饱和列，Total/Top 列；宽机活跃列压缩；默认折叠） | interrupts/s 突增=网卡/磁盘中断风暴；**%sirq 单核逼近 100%=ksoftirqd 打满一核**（机器级 si 均值会掩盖——1/64 核满载均值才 1.4%），处置=RPS/中断亲和；矩阵定向 NET_RX/TIMER/RCU、IRQ 列定位到设备。**loadavg 不在报告**（用户令 2026-10-30：r/b 即 load 原料——nr_running+nr_uninterruptible，loadavg 只是 EWMA 平滑无增量；loadavg 快照文件仍采集供现场用） | `../linux/cpu.md`（负载本质、调度类、软中断预算）+ `../linux/network.md`（NET_RX 归宿） |
| Retrans% | 重传率 | 加粗>3% 标红>5%；单流上限≈1.22·MSS·8/(RTT·√loss) | `../linux/network.md`（TCP 重试与假死连接） |
| ListenDrops/Overflows | accept 队列溢出 | >0 配 somaxconn/tcp_max_syn_backlog | `../linux/network.md`（两级连接队列） |
| TCP 状态每分钟（DB Process 板块 Per-minute detail 折叠表） | `ss -tanH sport=:PGPORT` 每分钟（awr 采样器 `db_tcp_states` 与 vtop 同源同序）——**只统计 DB 监听端口**：listener+已建连，出向 walrcv/dblink 按 dport 天然排除；双栈 listener 各计一（listen=2）；内核视图非 fd 计数（time-wait/close-wait 只在这看得到）；ss 失效=`-`（未知）不是零。**折表列（2026-10-30 定形）**：LISTEN Recv-Q、TCP Total（端口 socket 总数含监听）、ESTAB/SYN-RECV/TIME-WAIT/CLOSE-WAIT 四态固定、FIN-WAIT-1/2 记 FW1/FW2 双数、罕见态窗口内非零才动态加列；Conntrack Used/Limit 同折表（有采样才出列）；四态持平 Total 跳升=罕见态堆积 | time-wait 海量=短连接风暴；close-wait 堆积=应用不关；Syn-Recv 堆积=半连接队列满（配 ListenDrops）；ESTAB≈活跃 TCP 会话数（与会话视图对账） | `../linux/network.md`（TIME_WAIT/conntrack、关闭链路、两级连接队列） |
| rcv_rtt / min_rtt（ss -ti） | 接收侧 RTT / 路径地板 | **rcv_rtt ≫ rtt = 接收侧不消费**（大结果集拉不动）；**rtt ≫ min_rtt = 本地排队**而非距离 | `../linux/network.md`（RTT 估计器） |
| listener Recv-Q / Send-Q | VB 端口 accept 队列水位 | Recv-Q 逼近 Send-Q = accept 消费不足（溢出前兆），配 ListenOverflows | `../linux/network.md`（两级连接队列） |
| THP / compact_stall | 透明大页与直接压缩停顿 | **compact_stall>0 = 直接压缩停顿（DB 延迟尖刺源）**；thp_* = 大页活动 | `../linux/memory.md`（THP）+ 压缩停顿 |
| 内存碎片率（buddyinfo） | 空闲内存中 2MB+ 连续块占比 | **碎片率>50% 且空闲足 = 碎片化**（THP/大页分配失败前兆，配 compact_stall） | `../linux/memory.md`（THP/碎片） |
| conntrack count/max | netfilter 连接追踪表 | count 逼近 max = 表满 early_drop→丢包 | `../linux/network.md`（conntrack） |
| Softnet（Packets / Dropped Backlog / Time Squeeze） | 软中断收包三计数 | **Dropped=backlog 满**（处置=加大 netdev_max_backlog+RPS）；**Time Squeeze=预算耗尽没处理完**（处置=netdev_budget_usecs/分散软中断）——两者旋钮不同，先分清 | `../linux/network.md`（收包路径三道关：网卡→backlog→软中断预算）+ `../linux/cpu.md` |
| RX Missed / 网卡 errs-drop | 网卡层丢包（ethtool -S） | **RX Missed>0=ring 满丢在网卡**（加大 ring/队列数）；errs=协商错/驱动错（半双工误协商、CRC）；注意 /proc/net/dev 的 drop 列把 rx_missed 合并渲染，分不开 | `../linux/network.md`（ring 满丢 vs 栈丢分层、丢包三道关） |
| Bond（Mode / Active Slave / Slaves） | 聚合口身份 | 流量统计 bond 口是聚合值可用；**ethtool -S bond0 查不了（EOPNOTSUPP）**，驱动计数必须下到 slave 物理口查；active slave 换人=链路抖动证据 | `../linux/network.md`（bond 口径：流量聚合可读、驱动计数下 slave） |
| fd 分类 | postmaster 打开文件树 + **每分钟 fd 三桶**（DB Process 折表：DBFile/TempFile/XLog，postmaster 在数据目录下持有的 reg fd，live(deleted) 格式，'-'=该分钟未采样；老快照自动降级 '-'） | pgsql_tmp>0=活跃落盘；version.cfg 增长=已知泄漏；总数逼近 rlimit=fd 泄漏；**TempFile 桶>0=pgsql_tmp 活跃落盘的分钟级形态**（与 SQL 榜 temp bytes 同窗对账） | `../linux/file-descriptors.md`（fdtable/RLIMIT_NOFILE/proc 渲染） |
| File Handles（System Activity 行，进程口径 2026-10-30 改） | used=postmaster fd 总数（db_fd_types 各桶 live+deleted 之和），max=/proc/\<pid\>limits 的 Max open files soft/hard（**soft 是 alloc_fd 实际闸**；与 shell `ulimit -n` 无关，以运行进程实测为准） | used 逼近 soft=进程 fd 泄漏（EMFILE 前兆）；系统级 file-nr/fs.file-max 已撤出报告（数据仍在快照，供现场 OS 层排查：已分配逼近 file-max=全系统耗尽） | `../linux/file-descriptors.md`（fdtable/RLIMIT_NOFILE/proc 渲染） |
| DB Process（RSS / VSZ / Threads / Cpuset / rlimits） | postmaster 进程画像 | **Threads 对不上连接数=线程泄漏方向**（每连接一线程基线）；RSS 是 OOM 打分的驻留页口径；**VSZ 虚拟大≠吃内存**（保留地址空间）；rlimits 里 Max Open Files 对照 fd 总数 | `../internals/connection-lifecycle.md`（每连接一线程）+ `../linux/memory.md`（oom_badness 按驻留页打分） |
| Filesystem Δ inode / Δ space | 文件系统用量增量 | Δ 单列窗口变化；空间逼近满→WAL/临时文件写入全堵；inode 满→建新文件失败（日志轮转假死） | `../linux/io-storage.md` + `../cases/INDEX.md`（ENOSPC 判例：根分区满压垮实例） |
| OS Kernel Parameters（sysctl 全量 + THP 开关） | sysctl -a 快照 Begin/End | **Begin≠End=窗内被人改过参数，单列证据**；transparent_hugepage 行=THP 生效态（sysfs 非真 sysctl，enabled/disabled 归一）；逐参数现象对照走参数表 | `../performance/parameters.md`（现象→GUC/sysctl 对照）；THP 机制见 `../linux/memory.md` |

## OS 指标关联判读（成组读，禁单指标定论）

单指标只能产生**怀疑**，定论必须走关联链。四种关联手法（2026-10-28 立）：

1. **同向链**——一个指标歧义，2~3 个独立来源同向才成立（如 iowait 单看有假象分支）
2. **分流链**——一高一低分岔排除：靠"另一边低"砍掉一个分支（如 load 高但 busy 低）
3. **分层链**——同一症状在栈内分层，先定层再谈旋钮（丢包/延迟类最典型）
4. **时间对齐（同分钟横切）**——均值掩盖形态；异常分钟 T 必须把**所有 per-minute 通道并排成一张当时 OS 快照**再读：busy/iowait/r/b、MemAvail/pswpout、最忙盘 w_await、网络 RX/drop、retrans/rtt95、conntrack fill、TCP 状态（ESTAB/TW/CW/SR）——digest「时段明细」表已 18 列全通道同分钟横切（2026-10-28 扩），配 charts 1min 曲线看拐点先后

| 关联组 | 成组判读（箭头=证据链走向） | 为什么单看不行 |
|---|---|---|
| iowait → await + b + physical reads | iowait+await+b+物理读**四者同涨**=真存储慢；iowait 高但 await/b 低=**假象**（空闲核上的等待计数，忙起来反而降） | iowait 是双刃口径，idle 才累计（`../linux/cpu.md` wa 节） |
| load → busy + r / b | load 高+busy 低+**b 高**=D 状态抬的 load（IO 等待，不是 CPU 不足）；load 高+busy 高+r 高=真 CPU 饱和 | load 含 nr_uninterruptible，IO 挂死也抬（`../linux/cpu.md` 负载本质） |
| User+System 高 → 库内 DB CPU | 库内 CPU 低=**库外进程**吃 CPU（方向分流）；库内同高才查 SQL | OS busy 不分进程归属 |
| Steal 高 → 独走 | Steal>0 与库内一切指标无因果=虚拟化层抢 CPU，直接上报宿主侧 | 唯一不需要成组的指标（本身无歧义分支） |
| **库慢/busy 不高 → nr_throttled + cpuset.effective + Steal（三分流）** | Cgroup 章 nr_throttled/throttled_time Δ>0=**配额限流**（调 cpu.max）；cpuset 生效核数 < 机器核=**给少了**；Steal>0=**宿主抢**——三者都不占才回库内找 | "CPU 不够"有三层闸（cgroup 配额/cpuset/虚拟化），报告各有一列，单看 busy 定不了层 |
| ctxt/s → 非自愿切换 + sys + 等锁事件 | 三者同向才是锁/并发竞争； ctxt 高但切换自愿型=业务小事务多 | 切换数不分自愿/非自愿，成因相反 |
| si(Softirq) 高 → softnet 明细 + ksoftirqd | NET_RX/TIMER 定向；配 softnet dropped/time_squeeze 分层（见下行） | si 是软中断总账，收包/定时混在一起 |
| **丢包分层链**：RX Missed → errs → softnet dropped → time_squeeze → ListenDrops → conntrack fill | 同是"丢/拒"，层不同旋钮完全不同：ring 满加队列、backlog 满加 netdev_max_backlog、预算尽加 RPS、连接队满调 somaxconn、表满调 conntrack_max——**先定层再动参数** | 每层单看都像"网络有问题"（`../linux/network.md` 三道关） |
| Retrans% → rtt / rcv_rtt / min_rtt | rtt≫min_rtt=**本地排队**；rcv_rtt≫rtt=**接收侧不消费**（大结果集拉不动）；都小而重传高=真网络丢 | 重传率分不清丢在哪段（`../linux/network.md` RTT 估计器） |
| time-wait 海量 → 连接速率 + ListenDrops | 与 conn/s 同涨=短连接风暴；conntrack fill 同涨=连接风暴的全链证据 | time-wait 也可能是 tcp_tw_reuse 缺失的老内核常态 |
| MemAvailable 低 → pswpout + pgmajfault + Slab | 低+换出/缺页>0=**真不足**；低但换出=0+SReclaimable 高=可回收占位（**不是不足**）；配 WM Free<WM Min=kswapd 已在干活 | 可回收与不可用是两回事（`../linux/memory.md` 双 LRU/Slab） |
| pswpout > 0 → anon 涨 / file 落 | 换出的必须是匿名页才指向进程吃内存；文件页活动是正常回收 | 换出方向不分（`../linux/memory.md`） |
| Dirty 高 → w_await + 写吞吐 + pagewriter | 三者同向=写回拥塞；Dirty 高但 w_await 平=写回跟得上 | 脏页是瞬时库存不是积压证明（`../linux/io-storage.md` 写路径三闸门） |
| THP enabled → compact_stall | 开了且 stall>0 才是大页压缩停顿（延迟尖刺源）；开且 stall=0=无害 | 开关态不是症状 |
| 碎片率高 → MemAvailable + compact_stall | 碎片>50%+空闲足+stall 涨=大页分配失败前兆链 | 碎片本身不直接伤业务（`../linux/memory.md`） |
| await 高 → Rot 介质分档 + 绝对 IOPS | HDD 20ms 与 SSD 5ms 不是一个判读；IOPS 逼近介质上限时 await 升属正常 | 阈值不分介质必误判（用户令 10-27） |
| Threads 数 → 连接数 | 对不上=线程泄漏方向（每连接一线程基线） | 无基线对不上账（`../internals/connection-lifecycle.md`） |
| fd 总数 → rlimit + 系统 file-nr | 进程层逼近 Max Open Files=进程 fd 泄漏；系统层 file-nr 逼近 file-max=全系统耗尽——**两层分开看** | 层不对会修错地方（`../linux/file-descriptors.md`） |

与形态文件的关系：本节**自下而上**（OS 指标成组→指向问题方向），`bottleneck-patterns.md` **自上而下**（问题形态→取证链），两端在取证链汇合；同向链/分流链过不了的，不许直接套形态结论。

## OS ↔ DB 关联（跨层对齐，2026-10-28 立）

OS 是**环境/根因侧**，DB 是**症状/影响侧**。任何 OS 异常都要落到 DB 侧"谁在受影响"
（哪些等待事件 / 哪些 SQL / 多少 DB Time）；任何 DB 侧异常都要去 OS 侧"环境有没有配合"
（佐证根因）。**两侧对不上账 = 口径问题或库外因素**，不能视而不见。

### 方向 A：OS 异常 → 查 DB 侧影

DB 侧影**不只看 event**（event 薄弱且只做入口，STATUS/计数型要甄别）：先 ASP 桶
现场（waitchain + 时段 Top SQL），再 topsql 责任（占比大的 SQL），event 只作方向提示。

| OS 侧异常 | DB 侧应看 | 对不上说明 |
|---|---|---|
| busy / r 高 | CPU_TIME/DB_TIME 方向、SQL ordered by CPU Time、Callstack 前台热点 | 库内 CPU 低 = 库外进程抢 CPU（形态 2） |
| iowait / await 高 | physical reads 速率 + topsql Physical Reads 维 + ASP 桶 IO 等待类（event 只作入口） | 库内 IO 等待低 = 库外 IO（备份/ETL） |
| w_await / flush 尖峰 | pagewriter behind + WAL 写速率 + 后台堆栈 WalWriter/CheckPointer + ASP 桶 WAL 同步等待类 | flush 高但库内无 WAL 活动 = 非库写入抢盘 |
| 丢包 / Retrans% / rcv_rtt 高 | 客户端消费慢（大结果集拉不动）+ ASP 桶网络相关等待 + 连接卡顿 | 网络丢但 DB 无感 = 非库流量 |
| MemAvailable 低 / 换页活跃 | 53200/OOM 日志、shared_buffers 被挤、长事务、Session Memory 大户 | 换页但库内存平稳 = 别的进程吃内存 |
| conntrack 满 / accept 队列溢出 | 连接风暴、too many clients、认证失败族、CurrEstab 涨 | 队列满但库连接平稳 = 非库连接/扫描 |

### 方向 B：DB 侧异常 → 查 OS 侧根因

| DB 侧异常 | OS 侧查证 | 定性 |
|---|---|---|
| DB Time 高 | busy/r 高（=算）还是 OS 安静（=等） | CPU 型 vs 等待型，第 1 步方向判定 |
| DataFileRead 高 | await（分介质）+ 物理读 + 命中率 | 真读瓶颈 vs 分母小伪问题 |
| LogFileSync / WALWrite 高 | diskstats flush 列 + w_await + commits/s | fsync 慢 vs 高频小事务 |
| lock / LWLOCK 高 | ctxt/s（非自愿切换）+ si | 业务锁竞争 vs 内核级热点 |
| Temp Spill / temp bytes | 换页 + MemAvailable + 并发查询×每查询算子数×配额（operatorMaxMem 为准） | 配额不足 vs 全局水位 vs 内存真不足 |
| 连接被拒 / 认证慢 | accept 队列水位 + conntrack fill + somaxconn | 队列溢出 vs 认证慢 |

### 参数层（AWR 自含，参数对照不靠实机）

- **OS 侧参数**：AWR「OS Kernel Parameters」节 = `sysctl -a` 全量快照（Begin/End 两列，
  **Begin≠End = 窗内被人改过参数，单列证据**）；THP 开关也在这节（sysfs 归一成 enabled/disabled）。
- **DB 侧参数**：AWR「Database Parameters」节 = pg_settings 全量。
- 现象→参数对照统一走 `parameters.md`（机制查 / 配比查 / 介质查 / OS 查）；本文关联表
  点到的 sysctl/GUC 名（somaxconn、tcp_max_syn_backlog、netdev_max_backlog、shared_buffers、
  work_mem、max_process_memory…）一律以这两节为准取值，报告落成"参数名=当前值→建议值"，
  不猜默认值。

双向各列出的「对不上」就是跨层歧义的排除器：OS 高 DB 低=库外，OS 低 DB 高=库内
资源被隔离/记账失真（如 %DBtime 可 >100、CPU_TIME 含后台线程）。判读动作落在
`awr-report-analysis.md` 第 2 步「OS 根因下机制层」+ 第 4 步「形态过闸」之间——
OS 异常先在方向 A 落 DB 侧影，DB 异常先在方向 B 去 OS 侧查证，两向都对完再进形态取证链。

## Load Profile（每秒速率）

| 指标 | 含义 | 关联 |
|------|------|------|
| logical reads | blks_hit+blks_read | 高而 physical 低=正常热数据 |
| physical reads | 磁盘读 | 命中率+iowait+IO 事件三角 |
| rows returned/fetched | returned=顺扫读出、fetched=索引取得（pgstat 语义） | return>>fetch → 顺扫主导，配 SQL 榜找全扫者 |
| rows ins/upd/del | DML 行 | WAL 速率、dead tuples |
| commits/rollbacks | 事务速率 | 回滚率>5% 配 logrpt；高频+log sync 等待=小事务风暴 |
| SQL calls | statement.n_calls=QPS | |
| deadlocks/conflicts | 死锁/恢复冲突 | >0 必查 logrpt 死锁详情 |
| temp bytes | 临时文件写 | >0 即落盘；口径：SQL 榜的 spill 只含 Sort+HashJoin |
| WAL written | xlog insert LSN 位移 | MB/s 级=写风暴 |
| WAL write time(ms) | redo 写耗时 | writetim/phywrts=WAL 平均写延迟 |
| pagewriter flushed | 端点合成（dbstart 一致才有效） | 刷脏总量 |
| read/write iops·data | get_local_rel_iostat 内核直采 | **与 OS disk 表对账：库侧<<OS 侧 ⇒ 库外进程在读写** |

## Cache Hit Ratio

- 窗口率用 Δ 算。命中率低才需要看，且必须同时 physical reads 速率高；
  低命中+低物理读=工作集已缓存=伪问题。

## Database Memory

- Memory GUCs：shared_buffers/work_mem/maintenance_work_mem/max_process_memory
  （VastBase 总封顶，超限报错）/wal_buffers/cstore_buffers/segment_buffers/temp_buffers/local_syscache_threshold
- Shared Context Δ 持续增长不回落=泄漏嫌疑（点名上下文）
- Session Context 已按族归并（Hash_4728…→Hash 族；内核每个计划节点一个
  `<NodeTag>_<tid>` 上下文，SIRW=ShareInputScan，PL/pgSQL 按函数）
- Max Session Memory Sample：直接给会话（client_info）/Top3 上下文/unique_sql_id

## Replication

- lag 语义：Net=sent−write（网络+备端写）/ Fsync=write−flush / Apply=flush−replay /
  Total=sent−replay；备库侧 walreceiver 视角同名镜像
- slot active=f 恒 ⇒ 无限保 WAL → 主库磁盘涨（单独高危）
- 窗内角色切换（db_role "a -> b"）⇒ LSN Δ 作废

## 等待事件

- Class：LWLOCK_EVENT（轻锁）/ IO_EVENT（真 IO+少数计数型）/ LOCK_EVENT（行锁表锁）/
  **STATUS（执行阶段标记，不是等待——例外：wait wal sync / wait transaction sync /
  wait data sync 是真实等待，见 `../internals/commit-sync-events.md`）**
- 高频 avg≈0 → 计数型嫌疑（BufHashTableSearch=每次 BufferAlloc 记一次，
  waits≈逻辑块访问次数；占比再高也是"访问量"镜像，不是卡锁）。识别法与
  完整机制见 `wait-events.md`

## 长事务 / 堆栈 / 刷脏

- 长事务三宗罪：卡 vacuum、放大锁、保持快照；最老事务龄加粗>5min 标红>30min
- 后台线程空闲基线与活动栈判读见 `wait-events.md`
- Pagewriter：remain_dirty 持续涨 + behind xlog_insert(MB) 涨 = 刷脏跟不上写入；
  R=计数器重启该间隔作废；behind 在队列空时无意义（显示 '-'）

## SQL Statistics（9 维）

- 维度：DB Time / CPU Time / IO Time(data_io_time) / Plan Time / Logical Reads /
  Physical Reads((fetched−hit)) / Memory Used(sort+hash mem) / Temp Spill / Executions
- **单位（2026-10-30 起）：4 个时间维表的窗口总量与 /exec 列均 ms**（旧版秒）；
  静态尾列本就 ms——整节时间列一律 ms 读
- **mem/spill 口径：只计 Sort 算子+HashJoin；聚合内部排序与 HashAgg 不计**
- 高 Exec×中 per-exec=频次型；低 Exec×高 per-exec=单次贵（计划差/倾斜）；
  Plan Time 占比>30% 才值得立论（参数化问题）

## 对象统计

- **系统表在榜（2026-11-01 起，pg_toast 永除外防双计）**：statement_history 等
  catalog 行数/dead/DML 可见——上榜先按已知形态判（statement_history 膨胀=常见），
  不当业务表问题
- dead tuples 榜（table_age=age(relfrozenxid64)；n_dml_tup）+长事务=vacuum 被卡
- statio 榜 heap/index/toast 分解+Top3 索引；分区父行为内核聚合行；
  某索引恒 0 用量=死索引候选

## 活动会话历史（5min 桶，g/l 双源）

- Count：max/avg_sess、max_active_sess、wait_*（**桶内逐秒计数 max**——单秒峰值，非 5min 合计）
- Top SQL 每桶 Top5（unique_query_id）；waitchain 给 root_block_sessid+持锁/受害 SQL
- g=gs_asp（落盘流）、l=local_active_session（活流）；同桶 g 优先、l 补缺；
  **备库上 gs_asp 是主库复制来的历史，只看 l 源**

## 会话级视图与备库口径（既往实测固化，2026-10-25 入库）

- **session_stat/time 家族口径**（env-A 实测定论）：stat 计数器**执行中实时**
  累加；time 字段**语句结束才刷**（µs 单位）——执行中会话的 time 列看不到
  进行时成本；**会话退出视图即清**（事后取证要靠 ASP/AWR，不能事后查视图）
- **名字陷阱**：`n_physical_read` ≡ **逻辑读**（名字骗人）；真物理读 =
  `shared_blocks_read`〔该族无 rows/redo/网络字节计数〕
- **sid 与 pid 对账**：sid = `epoch.threadid`，与 pid join 要
  `split_part(sid, '.', 2)::bigint`；内核版本间该族列名有漂移，先 `\d` 对列
- **备库口径**（225-227 集群实测）：**备库 LAS 恒 0**（备库不采 ASP 快照）
  ——备库报告的会话历史段不判读；**gs_asp 满/冻结时 SQL 榜缺项**，
  见采集开关节同族问题
- statio 归属：分区表父表行=叶子之和；TOAST 列恒 NULL（无
  pg_statio_all_partitions）〔env-A 实测〕

## 采集开关（影响报告视野）

- enable_asp=off ⇒ 无 ASP 桶；instr_unique_sql_count 满/重置 ⇒ SQL 榜缺项或负 Δ
- logging_collector / log_min_duration_statement off/过大 ⇒ logrpt 慢 SQL 盲区
- enable_thread_pool 影响线程数语义
- **跨 DB 重启窗口（2026-10-30 定）**：屏幕报告 cpu/io/page/mem/net/tcp/tcpstat/fd
  八命令为纯 OS 每分钟口径，**跨重启窗口可用**（crash 断档期 OS 采样不中断——实例挂
  psql 秒败零损失，实例 hang 才会 flock 跳分钟）；DB 口径 9 节（topsql/event/summary/
  tabstat/replstat/replslot/replay/ltx/pagew/sqlhist）两端须同 uptime，跨重启被守卫拦

## 健康参考速查（仅起点，结论必须走取证链）

- AAS<2 轻载 / 2~4 中 / >4 重（再除以核数）
- 物理/逻辑读 <1% 优 / 1~5% 关注 / >5% 差
- 回滚率 <1% 优 / >5% 关注；r<N；Iowait<5%
- Buffer hit >99% 常态（低分母无效警告见 Cache Hit 节）
- SSD：r/w_await 正常 <2ms，>5ms 异常；HDD：正常 5~15ms（随机 15~30），>20ms 异常；
  **util 永不作判读标准（2026-10-28 用户令）**
- pswpout=0、pgmajfault≈0、ListenDrops=0、deadlocks=0

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 拿 %DBtime 当账本算分摊 | 并行下可 >100%，各类不可加 | 占比只表方向，另给 AAS/ASP/OS 口径佐证 |
| 用命中率低直接定缓存不足 | 低命中+低物理读=伪问题 | 同时看 physical reads 速率 |
| 拿 util 判饱和/慢盘 | **util 永不作判读标准（2026-10-28 用户令）** | 只看 await（介质分档）+ 绝对 IOPS |
| pswpout=0 就断言内存无压力 | gauge 类（MemAvailable/水印）先于换出发信号 | MemAvailable/WM Free 一起看 |
| ASP wait_* 当 5 分钟合计 | 是桶内逐秒计数 max（单秒峰值） | 表述为"峰值秒并发" |

## 版本注记

- 口径结论（CPU_TIME 含后台等）基于 2026-10-24 实测与内核源码核对（G100 V3 档）
- ASP 双源（gs_asp/local_active_session）行为基于当前 vb_tool 采集实现

## 来源

- 内核源码基线 `<openGauss-6.0.0-src>/`
  （gs_instance_time / pgstat_report_waitevent / get_local_rel_iostat 等）
- vb_tool AWR 采集/渲染源码精读（2026-10-24；2026-10-30 复对账 master 6b17d22：dbfd 分钟行、DB Process 折表、TCP Statistics 独立章；2026-11-01 复对账 master de5d3f0/v2.5.20：Cgroup 章、NUMA 深补、Softirq/IRQ 矩阵、SQL ms 化、object_tables 系统表、重启守卫两层）
- 实测样本：2026-10-21 / 2026-10-22 报告

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
