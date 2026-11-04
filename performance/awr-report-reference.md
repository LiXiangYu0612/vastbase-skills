# awrrpt 章节判读字典

## 概述

vb_tool `awr awrrpt` 产物的 17 章节逐节判读对照：每章给子表与列 → 语义与采集源 →
阈值与口径陷阱 → 现象入口（什么现象送你来）→ 跨章联查。分析全流程见
`awr-report-analysis.md`；指标口径详见 `metrics.md`。

除注明 gauge（瞬时值）外，报告全文数值 = 快照对 end − begin 的窗口增量。

## 报告头（Report Summary）

| 字段 | 语义 | 用法 |
|---|---|---|
| DB Role | primary / physical standby / "a -> b"=窗内发生过切换 | 备库报告里 gs_asp 是主库历史（复制来的），活动会话采样只能看 local_active_session 源 |
| DB Compatibility | A=Oracle / B,C=MySQL / Postgres | 兼容模式影响语法与部分行为判定 |
| Elapsed / DB Time / AAS | 窗长秒 / 窗口总耗时 / DB Time÷Elapsed | 整体负载画像的三个根数；DB Time 含后台线程时间（口径见 `metrics.md`） |
| Startup Time | postmaster 启动时间 | 窗内重启 ⇒ DB 口径累计计数器清零 ⇒ awrrpt 整报告被守卫拦（negative delta WARNING 同理）；**例外（2026-10-30 起）：`awr cpu/io/page` 三条 OS-only 文本命令跨重启窗口照常出表**——分钟 /proc 样本不随 DB 重启清零，crash 断档窗口的 OS 取证专用通道 |

现象入口：一切分析的起点；出现 negative delta 警告先整份降级。

## 1. OS Statistics（OS 侧全景，子节最多的一章）

| 子表 | 关键列 | 语义/来源 | 阈值与陷阱 |
|---|---|---|---|
| CPU Info | Model / Sockets / Cores per Socket / Total Physical Cores / Threads per Core / Logical CPUs / Freq / **Platform** | /proc/cpuinfo + DMI | N=Logical CPUs；超线程机物理核×2；这是 AAS/N 的分母；**Platform=显式虚拟化判定**（Physical / Virtual(<vendor>) / Virtual(hypervisor)，vb_tool platform_label 三级） |
| CPU utilization | Busy/User/System/Iowait/IRQ/Softirq/Steal/Idle（窗口均值） | /proc/stat Δ | busy>80 高、iowait>10 存储慢；**steal>0=虚拟化抢 CPU，不是库的问题** |
| CPU per-minute detail | 同上 + **r**（运行队列）+ **b**（D 状态阻塞） | os_*.log 1min | r>N=CPU 饱和；b 高+iowait 高=存储拥塞；r/b 阈值=核数/5%核数 |
| NUMA（2026-10-31 深补） | **拓扑+numastat 合并单表**（列：Node、CPU List、MemTotal/MemFree、Distance、Numa Hit/Miss、Interleave Hit、Local Node/Other Node、**Local Ratio**、**DB Pages/DB Share**=postmaster numa_maps 每节点 faulted 页聚合）+ **Auto NUMA Balancing 表**（Metric/Delta 两列：numa_pte_updates/huge_pte_updates/hint_faults/hint_faults_local/pages_migrated Δ，仅多节点且计数器存在；单侧缺计数器=`?` 不是 0）+ **DB Memory Placement top10**（Pages+每节点列 N0/N1…+Mapping=VMA 路径与 flags，按 faulted 页排序；unfaulted VMAs 行） | /sys/devices/system/node + /proc/\<pid\>numa_maps + /proc/vmstat numa_* | Local Ratio<90%+DB Share 节点失衡=库内存跨节点（远程延迟 1.5~2×）；hint_faults 大=内核在搬页（balancing 开销本身）；**DB Share=kernelpagesize_kB 加权（混页大小实例），DB Pages 列数原始页数**——两列口径不同；单节点机无 Auto Balancing/Placement 表 |
| System Activity | 主表：Context Switches/s、Interrupts/s、Forks/s、TCP Opens/Retrans/Retrans%、CurrEstab(B/E)、**File Handles（进程口径：postmaster fd 总数 vs /proc/\<pid\>/limits Max open files soft/hard，soft=alloc_fd 实际闸）**；主表后随 **Softirq 与 IRQ 两块 per-CPU 转置矩阵**（2026-10-31 定形）：CPU 行×活跃类别编号列（全零类别不占列；列名在**类型对应表**：No./Category/Meaning/Delta 或 IRQ/Device/Delta）+ 每核 **%sirq / %irq 饱和列** + Total/Top 列；宽机（>16 核）活跃列压缩；默认折叠 | /proc/stat、net/snmp、softirqs、interrupts、/proc/\<pid\>/limits | ctxt>100K 且同步等待高=锁竞争；Retrans% 加粗>3% 标红>5%；**%sirq 单核逼近 100%=ksoftirqd 打满一核（RPS/中断亲和方向），机器级 si% 均值会掩盖它**；矩阵定向 NET_RX/TIMER/RCU；IRQ 矩阵列定位到设备 |
| DB Process | Postmaster PID/RSS/VSZ/**Threads**/Cpuset/**nvcswch(非自愿切换)** + rlimits(soft/hard) + **fd 分类树** + **TCP 状态**（本端口 listen/estab/time-wait/close-wait...）+ **Per-minute detail 折叠表**（默认收起）：Time、LISTEN Recv-Q、TCP Total（端口 socket 总数含监听套接字）、ESTAB/SYN-RECV/TIME-WAIT/CLOSE-WAIT 四常见态固定列、FIN-WAIT-1/2（FW1/FW2 双数）、Conntrack Used/Limit（窗口有采样才出列）、fd 三桶 DBFile/TempFile/XLog（live(deleted)，'-'=该分钟未采样；罕见 TCP 态窗口内非零才动态加列） | /proc/\<pid\> + os_*.log 分钟行（lstn/lstnsock/conntrack/dbfd） | Threads 增长=连接涨；RSS 涨=内存累积；**time-wait 激增=短连接风暴；close-wait 堆积=应用不关连接**；**四态持平而 Total 跳升=罕见态堆积（看动态加列）**；TempFile 桶>0=活跃落盘（pgsql_tmp）；nvcswch Δ=窗口内 CPU 抢占次数（被踢出 CPU）；fd pgsql_tmp>0=有活跃落盘；version.cfg 计数增长=已知泄漏；fd 总数逼近 Max open files=fd 泄漏 |
| Cgroup（2026-11-01 新，DB Process 之后） | 单表 Begin/End/Delta 三列：**配置行**（cpu.quota/period/max、memory.limit、memsw.limit、pids.max、cpuset.cpus/effective/mems——Begin/End 各自快照值，Δ=`-`；`unlimited` 归一）+ **累计行**（cpuacct.usage、memory.usage、memory.failcnt、pids.current）+ **cpu.stat 行**（nr_periods/nr_throttled/throttled_time/throttled_usec/usage_usec）+ **memory.events 行**（low/high/max/oom/oom_kill，v2）+ **blkio 行**（io.service 每设备 Read/Write） | /proc/\<pid\>/cgroup 定位路径 + cgroup v1/v2 文件（非 root 可读） | **nr_throttled/throttled_time Δ>0=CPU 配额真咬**（库内表现慢/延迟抖但 OS busy 不高——P2 必查位）；**memory.events oom_kill 绝对值>0=cgroup OOM kill 过**（绝对值口径不是 Δ：OOM 后实例没了取不到 End 快照、重启后 cgroup 重建计数器清零，Δ 会埋掉事件）；计数器倒退=`reset`=cgroup 重建；memory.usage 逼近 memory.limit=限流前兆 |
| OS Memory Usage | MemTotal/Free/**MemAvailable**/Buffers/Cached/Active(anon/file)/Slab/Dirty/SwapFree 等 21 项 Begin/End/Δ gauge | /proc/meminfo | MemAvailable<10% 危；Dirty 持续大=写回压力；Active(anon) 涨=进程内存涨 |
| THP / Compaction | compact_stall/fail、thp_fault_alloc/collapse_alloc 窗口 Δ | /proc/vmstat | **compact_stall>0=直接压缩停顿（DB 延迟尖刺源）**；thp_*=大页活动 |
| Memory fragmentation | 空闲页总数 + 2MB+ 连续块 + 碎片率（窗口 max） | /proc/buddyinfo | **碎片率>50% 且空闲足=碎片化**；2MB+ 连续块少=THP/shared_buffers 拿不到大块 |
| Memory / Paging per-minute | pgpgin/out、pswpin/out、pgfault、**pgmajfault**/s + MemUsed/MemAvail + zone 水印(Free/Min/Low/High) | /proc/vmstat | **pswpout>0.5/s、pgmajfault>50/s=内存真不足**；WM Free<WM Min=kswapd 即将介入 |
| Disk I/O per device | **Size/Type/Rot/Sched/QDepth/NR_RQ/WC** 身份列 + r/w iops、r/w B/s、avg r/w_await、avgqu-sz、%util（窗口） | /proc/diskstats + blockdev_meta | **Rot: 0=SSD 1=HDD —— IO 阈值按介质分档**：SSD await>5ms 异常、HDD>20ms 异常；**util 永不作判读标准（2026-10-28 用户令，任何介质）——看 await+绝对 IOPS**；WC=写缓存；Sched=io 调度器 |
| Disk per-minute detail | 每设备每分钟 r/s w/s B/s r_await w_await %util | os_*.log | 时段定位看这里；分区已折叠到整盘 |
| Filesystem Usage | 每挂载点空间+inode | df -TPk/-TPi | pg_xlog/pg_arch 所在盘满=WAL 停写高危 |
| Network per interface | rx/tx B/s、pkt/s、errs/drop Δ + **RX missed Δ**（ring 满）+ Speed/Duplex/Queues/Ring + RX/TX Util% + **bond 脚注**(mode/active slave；无 bond 设备标注 no bond devices) | /proc/net/dev + sysfs rx_missed_errors + ethtool + /proc/net/bonding | drop>0=ring/backlog 尽（**drop 内核合并值=栈丢+missed**，missed>0=ring 满）；errs=协商/驱动；Util%>70% 带宽近饱和 |
| TCP Statistics（每分钟，独立章默认折叠） | Time、Opens/s、Retrans/s、**Retrans %**、CurrEstab + 有 RTT 采样时：RTT Conns/avg/p95、Worst Peer + 有 tcpext 采样时：ListenDrops/s、ListenOverflows/s、Timeouts/s、SynRetrans/s | os_*.log 分钟行（tcp/tcpext/ssrtt） | **主机聚合口径（全连接，非 per-interface）**；Retrans% 加粗>3% 标红>5%；RTT=内核平滑 RTT（established 非 loopback），avg/p95 加粗>5ms 标红>20ms，'-'=该分钟无合格连接；**Retrans% 与 RTT 并排读：重传高+RTT 高=真网络劣化，重传高+RTT 平=丢在别处**；ListenDrops/Overflows=accept 队列溢出；Timeouts=RTO 到期（重丢）；SynRetrans=建连降级；无 tcp 行整章隐藏 |
| TCP RTT / Connections | Conns avg/max、RTT avg/p95、Worst peer、Mathis 单流上限 + Top10 by Send-Q / by RTT（含 **rcv_rtt/min_rtt**、**Cwnd/RTTVar**、Local Port）+ **Listener accept queue**（Recv-Q/Send-Q） | ss -ti 每分钟 + END 快照 + ss -lnt4 | RTT 加粗>5ms 标红>20ms；**rcv_rtt≫rtt=接收侧不消费、rtt≫min_rtt=本地排队**；Cwnd 小=内核拥塞控制在限速（配重传看）；RTTVar 大=RTT 抖动（比均值更能暴露间歇劣化）；**listener Recv-Q 逼近 Send-Q=accept 队列饱和（溢出前兆）**；Mathis=该 RTT×丢包下单条 TCP 流的理论上限 |
| Softnet | packets、**dropped(backlog full)**、**time_squeeze** | /proc/net/softnet_stat | dropped→调 netdev_max_backlog/加队列；squeezed→RPS/中断亲和 |
| Network Protocol Errors | InErrs/RetransSegs/ListenDrops/ListenOverflows/Timeouts/AbortOn*(含 Linger/Failed)/ZeroWindow(含 Drop) 等 Δ + **conntrack count/max** | /proc/net/netstat + /proc/sys/net/netfilter | ListenDrops/Overflows>0=accept 队列溢出（somaxconn/backlog）；AbortOnTimeout 多=严重丢包；**conntrack count 逼近 max=表满丢包** |
| Kernel Messages (dmesg) | Time/Category/Message，九类（Kernel crash/Memory-OOM/Process crash/Storage/Multipath/Filesystem/CPU-Sched/Network/Power-HW） | 增量 dmesg，模式分类 | **Storage/OOM/Filesystem 任一条都是高危证据**；时间与异常时段对齐 |

现象入口：OS 侧任何异常（CPU/IO/内存/网络/内核）的定位与时段切分；IO 延迟判读
前先看 Rot 列定介质；短连接/accept 问题看 DB Process 的 TCP 状态行。

## 2. Time Model System Stats

- gs_instance_time 各 stat 的窗口 Δ（秒 + %DB time）。关键：DB_TIME、CPU_TIME、其余阶段项。
- 口径陷阱：VastBase 时间模型未区分前后台——CPU_TIME 含后台线程
  （pagewriter/walwriter/autovacuum 全算进去）；DB CPU/DB Time 只作方向参考，
  CPU 型结论必须用 OS busy + r 队列佐证（详见 `metrics.md`）

现象入口：CPU 型/等待型方向判定的第一入口（拿方向），最终定论去 OS 序列。

## 3. Load Profile（每秒速率总账）

| 行 | 来源/语义 | 关注 |
|---|---|---|
| logical reads | blks_hit+blks_read（含缓存命中） | 高而 physical 低=正常热 |
| physical reads | blks_read | 与命中率/iowait 三角验证 |
| rows returned / fetched | returned=顺扫读出、fetched=索引取得（pgstat 语义，非"回客户端"） | return>>fetch=顺扫主导 |
| rows ins/upd/del | DML 行 | 联动 WAL 速率/dead tuples |
| commits / rollbacks | 事务速率 | 回滚率>5%+错误日志；高频小事务+log sync 等待 |
| SQL calls | statement.n_calls（QPS） | |
| deadlocks / conflicts | pg_stat_database | >0 必查（死锁详情在 logrpt） |
| temp bytes | temp_files 写入 | >0 即有落盘，必查 Temp Spill 榜 |
| WAL written | xlog insert LSN 位移 | MB/s 级=写入风暴 |
| WAL writes/blocks/write time(ms) | file_redo_iostat=pg_stat_get_redo_stat | writetim/phywrts=WAL 平均写延迟 |
| pagewriter flushed | pw 日志端点合成（带重启守卫） | 刷脏总量 |
| read/write iops、read/write data | get_local_rel_iostat 内核直采（无 2000 槽 LRU 截断） | 与 OS disk 表对账：库侧物理读 << OS 侧读 ⇒ 差额是**库外进程**在读 |

现象入口：负载画像与"库内 vs 库外"判别（库侧 iostat 与 /proc/diskstats 对账是利器）。

## 4. Cache Hit Ratio

- 每库 Begin/End/Window 命中率（窗口率用 Δ 算，不被历史稀释）。
- 陷阱：命中率低才需要看，且必须同时 physical reads 速率高才成立；
  低命中+低物理读=工作集已缓存，伪问题。

## 5. Database Memory

| 子表 | 内容 | 关注 |
|---|---|---|
| Memory GUCs | shared_buffers/work_mem/maintenance_work_mem/max_process_memory/wal_buffers/cstore_buffers/segment_buffers/temp_buffers/local_syscache_threshold/max_connections/effective_cache_size（带单位解码） | 参数嫌疑第一入口；max_process_memory 是 VastBase 总封顶 |
| Total Memory by Type | memory_node_detail 按 memorytype | 各类内存总量 Δ |
| Top 10 Shared Memory Context | gs_shared_memory_detail 按 contextname（Begin/End/Δ+cnt） | **持续增长不回落=泄漏候选**（列具体上下文名） |
| Top 10 Session Memory Context | gs_session_memory_detail，动态上下文已归并族名（Hash_xxx→Hash 族） | 操作符内存大户；Hash/Sort 族大=大算子 |
| Session Memory Summary | 会话数/总量/均值/最大 | work_mem 为算子级配额（单查询=配额×算子数），总量风险=并发查询×每查询算子数×配额，以 operatorMaxMem 弹性上限为准（work_mem 只是 operatorMemKB 估算的兜底）；以 Max Session Memory 实测为锚，非会话数×work_mem |
| Max Session Memory Sample | 每 5min 最大会话：client_info(user\|addr\|port)、PID、User、State、Memsize、Top3 Contexts、unique_sql_id | **直接点名谁吃内存、跑的什么 SQL** |

现象入口：OOM/内存涨/上下文泄漏/会话内存大户。

## 6. Replication（角色自适应）

- 主库侧：Replication Stat（每分钟采样，Sent/Write/Flush/Replay LSN + Net/Fsync/Apply/Total lag 字节；健康分钟已隐藏）+ Replication Slots（active/restart_lsn/xmin/confirmed_flush）。
- 备库侧：Wal Receiver（sender 视角镜像，sync_percent/channel）。
- lag 语义：Net=sent−write（网络+备端写）、Fsync=write−flush（备端刷盘）、
  Apply=flush−replay（备端回放）、Total=sent−replay。
- **slot inactive（active=f）= 无限保 WAL → 主库磁盘涨，单独一条高危**。
- 窗内 db_role 带 "->" = 切换过，LSN 类 Δ 作废。

现象入口：主备延迟投诉、备库 IO、复制槽积压、WAL 磁盘增长。
（复制架构与切换本身属 ha/ 分类，落地后互链。）

## 7. Top 10 Wait Events by Total Wait Time

- 列：Event/Class(LWLOCK_EVENT|IO_EVENT|LOCK_EVENT|**STATUS**)/Waits/Total/Avg/%DBtime。
- **STATUS=执行阶段标记不是等待，绝不计入瓶颈占比**（例外：wait wal sync /
  wait transaction sync / wait data sync 是真实等待，判读见
  `../internals/commit-sync-events.md`）。
- %DBtime 并行下可 >100，各类不可相加（口径见 `metrics.md`）。
- 高频 avg≈0 → 先查计数型事件（BufHashTableSearch 家族：pgstat_report_waitevent
  包住一次同步操作，waits≈块访问次数），识别法见 `wait-events.md`。

## 8. Long Transaction Monitor（5min 采样）

- ltx60_cnt / ltx60_itx_cnt（含闲置事务）/ ltx60_active_cnt、最老事务 PID/起点/
  duration（加粗>5min 标红>30min）/State/Wait Event/unique_sql_id/OldestXmin。
- 长事务三宗罪：卡 vacuum（dead tuples 榜联查）、持锁放大等待（阻塞链联查）、快照保持。

## 9. Callstack Sampling（5min 采样）

- Background Threads：Thread/PID/State/Wait Event/CPU%/Stack。空闲基线与判读规则见
  `wait-events.md` 堆栈一节。**停在基线=排除，偏离基线=后台嫌疑**。
- AutoVacuum Workers：Worker Cnt/CPU% 合计/最高 worker 栈。**worker_cnt 恒 0=真空没跑**
  （与 dead tuples 联判）。
- Foreground Threads (LWLock waits)：Wait Event/PID/unique_sql_id/event_cnt/Stack。
  为空≠无锁问题（只是没采到）。
- 栈串无行号只有符号+偏移 → 源码按函数名检索。

## 10. Pagewriter Status（5min 采样）

- flush delta（cumulative 差分，**R=计数器重启该间隔作废**）/ pages/s / last_flush /
  **remain_dirty_page_num** / queue_head_page_rec_lsn / **behind xlog_insert (MB)**
  （最老脏页 rec_lsn 落后插入点=刷脏追不上写入）/ ckpt_redo_point。
- 判读：dirty 持续涨+behind 涨=刷脏能力不足 → 查 shared_buffers/批量写入/checkpoint
  参数；behind 巨大且 dirty=0 时队列空显示 '-'。

现象入口：写延迟类问题（w_await 高/WAL 卡/检查点抖动）区分"刷脏跟不上"还是"存储慢"。

## 11. SQL Statistics（9 维同一批 SQL）

维度：DB Time / CPU Time / IO Time(data_io_time) / Plan Time / Logical Reads /
Physical Reads / **Memory Used**(sort+hash mem) / **Temp Spill**(sort+hash spill) / Executions。
公共列：SQL Id（点开全文）/User/Executions/%Total/维度值/exec、CPU·IO Time/exec、
logical·physical reads/exec、rows/exec、plan/sort/hash_time/exec。
**单位（2026-10-30 起）：4 个时间维表（DB/CPU/IO/Plan Time）的窗口总量列与 /exec 列均 ms**
（旧版秒；千分位渲染）；静态尾列 CPU·IO Time/exec、plan/sort/hash_time/exec 本就是 ms——
整节时间列一律按 ms 读，别把 1234ms 读成秒。

- **Memory/Temp Spill 只统计 Sort 算子+HashJoin**：聚合内部排序（如 string_agg ORDER BY）
  与 HashAgg 不计 → "没落盘记录"≠真没落盘
- physical reads/exec=(fetched−hit)/calls 推导值
- 定责三步：按形态选维度 → 与 ASP 桶 query 榜对齐 unique_query_id → 跨维矩阵
  （同一 SQL 霸 CPU+IO+Temp=主要责任；单维=受害/局部；维度全 0=采集缺口标不可用）
- 高 Exec×中 per-exec=频次型（量大成灾）；低 Exec×高 per-exec=单次贵（计划差/倾斜）

## 12. Object Statistics（全库）

- **系统表在榜（2026-11-01 起）**：object_tables 采集纳入系统 catalog
  （pg_stat_all_tables 去掉 pg_toast——toast 块 DML 双计父表）；**statement_history
  常膨胀变慢，其行数/dead/DML 必须可见**——系统表上榜先按已知形态判，不当业务表问题；
  pg_toast 永不出榜（防双计）。
- Top Objects by Dead Tuples：table_age(=age(relfrozenxid64))、relpages、reltuples、
  n_dml_tup、live/dead（Begin+End gauge）。
- Top Tables by Activity：seq_scan/seq_tup_read/idx_scan/idx_tup_fetch/DML/HOT upd Δ。
- Top Tables by Logical/Physical Reads：heap/index/toast 分解 + First/Second/Third index(n)
  （该表 Top3 索引用量）。
- 分区父表=内核侧聚合行；toast 列父层 NULL→0。
- 判读：dead 高+长事务在榜=vacuum 被卡；seq_scan 激增=索引失效/新查询；某索引恒 0=死索引。

## 13. Active Session History（5min 桶，双源 g/l）

- Count：max_sess/avg_sess/**max_active_sess** + wait_lock/lwlock/io/wal_sync/trans_sync
  （**桶内逐秒计数的 max**，非合计）。
- Event / Top SQL（每桶 Top5，unique_query_id+文本前 50 字）/ Waitchain
  （root_block_sessid+持锁 SQL+受害 SQL+阻塞数）。
- 激活条件：enable_asp=on；备库只有 l 源。**waitchain 空行=窗内无阻塞（正常）**。
- g/l 双源接缝：同桶 g 优先，l 只补 g 缺的桶。

现象入口：数据库侧时段定位、行锁队列（waitchain 直给根会话）、时段内 Top SQL。

## 14. Wait Events（全量，折叠）

同 Top10 但全量行——算占比/对账用这里。

## 15. SQL Text（折叠）

涉及 SQL 的归一化全文；"(text not collected...)"=共享 sql_text store 丢失，数字不受影响。

## 16. Database Parameters（折叠，渲染时 live 查询 pg_settings 全量）

Name/Setting（8kB 已解码）/Context/short_desc。**参数嫌疑查这里**（digest 关键子集 +
`<名>_params.txt` 全量）。Context 决定改法：postmaster=要重启、sighup=reload、user=会话级。

## 17. OS Kernel Parameters（折叠，sysctl -a 全量）

Begin/End 两列，**中途改过显示两值**。重点：somaxconn/tcp_max_syn_backlog（连接风暴）、
netdev_max_backlog（丢包）、vm.swappiness、vm.min_free_kbytes、fs.file-max、kernel.sem。

## 现象 → 章节速查（反向索引）

| 现象 | 先看 | 再联查 |
|---|---|---|
| 慢，不知道哪慢 | 报告头 AAS + Time Model + OS CPU | ASP 桶、Top Event |
| CPU 高 | OS CPU per-min(r) + SQL CPU Time 维 | 库外进程：库侧 iostat vs OS disk 对账、DB Process 线程数 |
| **慢但 OS CPU 不高** | **Cgroup 章 nr_throttled/throttled_time Δ** | cpuset.cpus 生效核数、memory.usage→limit、Steal（虚拟化同类分流） |
| IO 读慢 | **Disk 表 Rot 定介质** → per-min await（util 永不判读） | 命中率+物理读、SQL Physical Reads 维、Object Reads 榜 |
| IO 写慢/WAL 卡 | Pagewriter(behind/dirty) + WAL write time | Load Profile WAL 速率、后台堆栈 WalWriter/CheckPointer |
| 锁等待 | Top Event 分型：LOCK(重量级)→ASP waitchain；LWLOCK(轻量级)→Callstack 前台 LWLock 栈 | 长事务榜、logrpt 死锁 |
| 内存涨/OOM | OS Memory + Shared Context Δ | **Cgroup memory.events oom_kill（绝对值）**、memory.usage→limit、Max Session Memory Sample、logrpt OOM 错误、dmesg Memory/OOM |
| 膨胀 | Dead Tuples 榜 | 长事务、AutoVacuum worker_cnt、vacuum 参数 |
| 延迟抖动 | OS per-min 全轴 + ASP 桶 | dmesg、TCP RTT/Retrans、日志前后窗切片 |
| 连接报错/风暴 | DB Process TCP 状态(time-wait/close-wait) + Protocol Errors(ListenDrops) | somaxconn/tcp_max_syn_backlog、logrpt too many clients |
| 主备延迟 | Replication lag 分解(Net/Fsync/Apply) | 备库 IO、Slots、WAL 速率 |
| 落盘/排序溢出 | SQL Temp Spill 维 + temp bytes | work_mem、fd pgsql_tmp、Session Memory |
| 疑参数问题 | Database Parameters + OS Kernel Parameters | `parameters.md` 分组清单 |

## 采集侧说明（判读时要知道数据怎么来的）

- 快照存 `~/.vb_tool/awrs/<port>/snap_<id>/{os,db}`；采样日志
  os_/repl_/slot_/walrcv_/lt_/stack_/sessmem_/pw_<日>.log[.gz] 与快照同根。
- `awr awrrpt` 默认连跑 analyze_log_v2 并打 tar：**awr_<b><e>.tar = awrrpt+logrpt 两份 html**。
- per-minute OS 序列 = 窗口内 os 日志的逐分钟 Δ；ASP = 每次快照冲刷 [上次快照,本次] 的
  1 秒采样、5 分钟桶化；长事务/堆栈/会话内存/刷脏 = 5 分钟节奏。
- 重启守卫（两层，2026-10-30 起）：**awrrpt 与 DB 口径节**（topsql/event/summary/
  tabstat/replstat/replslot/replay/ltx/pagew/sqlhist）窗内重启即作废——dbstart /
  pagewriter R 标记 / 负 Δ 警告任一出现即该计数作废；**`awr cpu/io/page` 三条 OS-only
  命令跳过守卫**（分钟 /proc 样本不随 DB 重启清零）——实例 crash 断档窗口的 OS 取证
  专用通道，两窗夹住重启点也能出表。

## 版本注记

- 章节 17 个（顶层不变）、列语义基于 vb_tool v2.5.20（2026-11-01 复对账 master
  de5d3f0；OS 快照文件 31 项 = 27 基线 + irq_affinity/irq_node(10-31) +
  db_numa_maps/db_cgroup(11-01)）。vb_tool 升级增删章节时本文件同步更新
- 本轮对齐新增：Cgroup 章、NUMA 深补（拓扑/numastat 合并+Placement）、Softirq/IRQ
  per-CPU 转置矩阵、SQL Statistics 时间维 ms 化、Object Statistics 系统表入榜、
  重启守卫两层（cpu/io/page 放开）
- 备库报告的 ASP 语义（gs_asp 为主库历史）实测于主备环境

## 来源

- vb_tool AWR 采集/渲染源码精读（41954 行，2026-10-24；2026-10-30 复对账 master
  6b17d22；2026-11-01 复对账 master de5d3f0/v2.5.20：cgroup 渲染块、numa_html_table
  placement、Softirq/IRQ 矩阵、ms_of_g、object_tables 系统表、重启守卫 case 分支）
- 实测样本：2026-10-21 / 2026-10-28 成对报告（`../evals/fixtures/`）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
