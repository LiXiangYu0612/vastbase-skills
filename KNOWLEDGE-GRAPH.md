# 知识网总图（节点 · 边 · 导航路径）

> 本文件是知识网的**关系层**：ROADMAP 管"覆盖了没有"，kernel-data-path 管"机制在
> 通路哪里"，本图管"知识点之间怎么互相解释"。遇到问题从导航路径进网，
> 顺着边走；每条边的方向就是推理方向。

## 节点清单

- **机制层（internals/，31）**：operator-memory、buffer-manager、core-events、
  commit-sync、error-fatal、kernel-data-path（枢纽）、locks、query-rewrite、
  query-plan-cache、flush-checkpoint、stats-to-cost、mvcc-visibility、autovacuum、
  opfusion（点查融合：参数族/EXPLAIN [Bypass] 观测/GPC 签名耦合）、
  adaptive-plan-mgr（gplanmgr 多候选自适应选择：置信区间匹配/淘汰赛/
  Choose_adaptive_gplan hint，PBE 唯一学习型算法）、
  replication-slots、wal-replay（含并行回放/极致 RTO：依赖封装 redo_item.h/
  IOS 取舍）、**recovery-conflict（回放冲突五分类+snapshot 深链+禁快照连锁
  事故）**、**double-write（半写防护/DW 组织/检查点回收边界/PR 6671 竞态）**、
  **mergejoin-cost-estimation-risk（估价 NOW 快照扫索引抢 ProcArrayLock：
  Plant Time 独大/卡死两形态）**、query-pipeline（含词法/语法深读）、
  connection-lifecycle（连接生命周期：postmaster/准入闸/认证/compat_oracle_
  txn_control）、index-btree（分裂/dedup/页回收/HOT）、
  executor-operators（NL/MJ/Agg/Window/Material+配额接线）、fd-vfd-budget
  （fd 三层闸：rlimit/VFD 预算/AllocateDesc）、heap-tuple-attr（取属性六分支：
  短元组默认值/dropped 列/deform 缓存）、sysv-semaphore-usage（SysV 信号量：
  kernel.sem 是限额非成本/热睡眠路径不走 sem）、debug-logging-gucs（logging_module
  双闸配方+debug GUC 死旋钮清单：优化器路径回放/内存核算可开，trace_locks 族
  release 未注册开=无效）、gs-stack-sampling（抓栈机制：5ms SIGURG 窗口/权限硬闸/
  AWR Callstack 段边界）、partition-table（分区目录/裁剪/路由/GPI/EXCHANGE：
  分区统计在 pg_partition、默认索引 GLOBAL、interval 自动扩=插入路径造 DDL）、
  smp-stream-parallel（Stream 线程模型：子计划禁并行/STATE_STREAM 四态/
  配额×dop 内存账/Streaming 计划判读）
- **操作层（performance/，10）**：awr-report-analysis、awr-report-reference、awr-principles、
  metrics、wait-events、bottleneck-patterns、parameters、log-analysis、diagnostic-map、
  check-inventory（检查项总账：两报告能力边界+44 项机械闸镜像，阈值同步
  overview_table.py RULES）
- **OS 层（linux/，16）**：**os-triage（四域诊断树：五列基线→A~G 支分诊，
  CPU↔Network 五通路/组合拳指纹——"症状层≠根因层"的路由枢纽）**、cpu、memory、
  **process-states（任务状态位全枚举 R/S/D/T/t/Z/X/P/I：状态机睡醒主线[set_current_state+schedule/try_to_wake_up 唯一入口]/
  D 深睡三型与 hung task 判据三版分叉[3.10 等值/4.19 注释点名 KILLABLE/7.1 位测跳 NOLOAD]/信号杀不死纯 D/
  I 态不进 loadavg——字母与位值的源码定论件）**、
  **page-cache（页缓存数据结构主线：address_space→XArray→folio→LRU→回写/截断——io-storage 与 memory 的底座件）**、io-storage、
  **fio（压测设计与结果判读：IO 路径缓存链[页缓存/RAID 卡读·写缓存/盘内 DRAM/SLC cache/二级 SSD tier]→size≥10× 最大缓存铁律/
  precondition+ramp 稳态三件/极限判定[阶梯加压膝盖点+Little's law 对账]/假高指纹表/aggrios 拓扑反推/写测试隔离红线）**、**cgroup（五控制器源码层：cpu.max 配额咬/throttled_usec 本组vs子孙/
  memory.high 罚睡 vs max OOM/pids fork EAGAIN/io.max×wbt 两层——"库慢 OS 不慢"
  与容器/K8S 限额判读的根）**、network、
  **switch-side-logs（交换机侧排查全景：日志族全表[实拍三族+工程口径七族]/
  ERROR-DOWN 六原因码/DOM 光衰/STP TC/MAC 漂移/堆叠 VRRP/端口错包分诊/
  闪断·丢包·断连风暴·环路四条 SOP——主机 Retrans 归因"中间网络"的
  唯一直接证据，镜像口豁免）**、**net-diag-fullchain（网络全链路定责：探测/网卡五族/DOM/抓包
  SEQ 对账，八步 SOP+PCIe+对时）**、**net-midpath（中间层四段：L2 机制/L3 路径/
  中间设备/虚拟化云，间歇规律性断连与"计数器全绿仍抖"的判据富矿）**、
  **xfs（XFS 机制与删除恢复：AG/SB/AGF/AGFL/双空闲 btree/大端；删除三阶段
  BMX 残留=恢复窗口；五级恢复路径）**、**ext4（组结构/删除即清 extent
  [与 XFS 相反]→恢复正道=free 块内容扫描；JBD2 双层字节序）**、
  file-descriptors、tracing-tools
  （内核源码基线 7.1-rc4，生产 3.10 差异见各文件版本注记）
- **救火操作层（troubleshooting/，10）**：startup-failure、connection-refused、
  crash-panic、blocking-live、resource-exhaustion、stack-reading、
  **page-lsn-mismatch（lsn check error/bigger than want set LSN：XLOG 解剖+
  双链回溯分叉定责）**、**crash-case-index（55 条内部缺陷库判例速查，栈指纹
  对号；含内网链接，公仓发布前排除）**、**cpu-sys-high（sys% 高四层分诊：
  静态形态→计数器对齐（/proc/interrupts CAL/TLB 行=广播 IPI 直接信号）→
  perf 栈指纹判据表→定责对账；含 `-p` 三盲区与单进程大 mm fork 税放大器）**、
  **oom-analysis（OOM 四型分诊：内核 killer/cgroup/库侧 53200/换页前驱，
  每型触发条件+root cause 链+三源闭环对账）**
  （止损流程层：先保全后定位，机制一律引用 internals/linux 不复制；
  stack-reading=BACKTRACELOG/调用栈解读，机制挂 error-fatal）
- **高可用层（ha/，7）**：replication-lag（三段分解出口）、
  replication-architecture（线程对/四位点/模式状态机/同步语义表/级联/延迟
  回放/build 约束）、failover（promote/term 任期/rewind）、split-brain
  （双主三来源/降级窗口代价）、**has-dcs-architecture（HAS+DCS 栈：四模块/
  选举/pause/DCS 故障两形态）**、**cm-cluster-manual-ops（CM 栈 13 场景人工
  处理清单）**、**read-write-splitting（Hydra-JDBC 读写路由/切换感知/备库
  冲突报错转发）**
- **SQL 层（sql/，7+references）**：sqlhc-analysis（SQLHC 诊断流程）、
  optimizer-deep（代价/查询转换/迁移优化深潜总纲）、sqlhc-integration、
  sqlhc-quickref、optimizer-usage、quick-reference、**sql-patch（计划固化
  与语句拦截：按 unique_sql_id 注入 hint/ABORT 补丁应急杀；204 实测六函数）**
  + references 深潜系列（原独立技能并入，2026-10-26）
- **备份恢复层（backup-recovery/，3）**：pitr-mechanism、**backup-window-performance（备份窗口性能四形态：fork 税判例锚/IO 挤占/备份启动强制 checkpoint(xlog.cpp:14766 实锚)/锁形态⚓；★vb_probackup `-r max-rate` 限速实测翻案——默认 90MB/s 已启用）**、
  **backup-restore-drill（演练配方+13 翻车形态：root 残留文件→WAL streaming 误诊/增量只剩 FULL+PTRACK/恢复三改本地化/socket GUC 单数/--time 断粮与补段救急/可恢复上限三元组）**
  （归档拉取/恢复目标/回放与备库同源）
- **案例层（cases/）**：判例集（`cases/INDEX.md` 为检索目录）——机制文件
  的实证锚点与判读经验来源，逐案例挂边到对应机制文件
- **基线层（admin/）**：g100-standard-params（+extract.py 自动对照）、
  version-map（版本谱系 LTS/DMR + 官方文档站 API 抓取配方 + 已知问题无公开
  源的负向实锤）、log-message-levels（日志级别两线排序：log_min_messages
  LOG 乱序排在 ERROR 之上[elog.c 特判]/client 线数值序排最底；HINT 列表
  非排序；降噪=关来源开关非调 min_messages）
- **元层**：SKILL（路由）、ROADMAP（覆盖账本）、本图（关系）

## 核心边（按语义分组，箭头=推理方向）

**执行流主链**（与 kernel-data-path 同构；写事务视角）：
query-pipeline →（产出算子）operator-memory →（读）buffer-manager →（miss）
core-events§2 →（执行写：拿行锁/页锁）locks →（WAL）commit-sync

本地落盘支线（与主链在"脏页"处分叉）：commit-sync 后缓冲页已脏 →
flush-checkpoint（pagewriter 刷脏）→ core-events§5（双写，防页撕裂）——
**支线到此为止**。

复制支线（与主链在"WAL 已刷"处分叉，不经双写）：WAL 流 → walsender/
walreceiver（replication-architecture 的线程对）→ wal-replay（备库回放）→
备库可读后接 recovery-conflict（查询×回放冲突面，含禁快照连锁）→
read-write-splitting（驱动层把备库冲突报错转发主库兜底）。
HA 管理面：replication-architecture ↔ has-dcs-architecture / cm-cluster-manual-ops
（两套栈管同一底座）↔ failover / split-brain（判定与操作面分工）。
双写支线补边：flush-checkpoint ↔ double-write（检查点=DW 回收边界）。
估价风险边：query-pipeline（plan 段）→ mergejoin-cost-estimation-risk →
locks（ProcArrayLock）/ stats-to-cost / autovacuum（死元组根因）。
崩溃判例边：crash-panic → crash-case-index（保全后先对已知缺陷族）；
page-lsn-mismatch ↔ double-write / wal-replay（pd_lsn 判据同源）。
双写与复制互不依赖：双写保本地页完整，复制传的是 WAL 不是页。

**判读依赖边**：
- 任何 AWR 现象 → **diagnostic-map（症状→章节导航+三件套）** → awr-report-reference
  （在哪章）→ bottleneck-patterns（什么形态）→ 对应机制文件（为什么）→ parameters
  （怎么办）——这是网的主干用法
- check-inventory ↔ diagnostic-map·bottleneck-patterns·log-analysis·
  awr-report-analysis：同一诊断能力的四个视角（能诊断什么总账 ↔ 症状进网 ↔
  形态取证 ↔ 错误族联动 ↔ 流程接线）——机械闸（overview_table.py）是表 D 的
  执行体，阈值改动两处同步
- error-fatal ↔ log-analysis：报错机理 ↔ 报错×AWR 联动
- core-events ↔ wait-events：个体事件深读 ↔ 溯源方法论
- metrics ↔ 全部机制文件：口径警告是读数的前提
- awr-principles ↔ awr-report-analysis/metrics：记账原理（三本账/DB Time 边界/
  事件账本污染）↔ 流程与口径——判读前先问"这数哪本账记的" 

**机制互证边**（跨文件的关键勾稽）：
- mvcc-visibility ↔ autovacuum：OldestXmin 决定回收边界 ↔ 触发公式决定何时清
- mvcc-visibility ↔ replication-slots：槽 xmin 同样压 OldestXmin（逻辑槽卡 vacuum）
- replication-slots ↔ wal-replay：xmin 反馈/忽略窗 ↔ 备库反馈冲突
- commit-sync ↔ locks：**wait wal sync（同步复制）与 WAL 组插入（本地）共用
  commit 路径但分属两层**——判读分层的依据边
- commit-sync ↔ wal-replay：STATE_STANDBY_READ_RECOVERY_CONFLICT 补进"STATUS 但
  真等待"清单
- operator-memory ↔ buffer-manager：gs_sysmemory_busy 全局水位同时作用于
  spill 决策与淘汰压力
- io-storage ↔ fio：IO 栈机制与 await 三档/util 禁令的单源在 io-storage，
  fio 是压测操作件（size 设计吃缓存链、判读回指 io-storage 口径不复制数字）
- os-triage ↔ cpu/memory/io-storage/network：四域枢纽——单域症状先扫其余三域
  基线（socket 症状常是 st/欠CPU 的下游投影，st=70% 案实证）
- flush-checkpoint ↔ locks：dw 批次拿锁前 XLogWaitFlush（WAL 慢拖刷脏）
- buffer-manager ↔ locks：前台脏受害者自刷=DoubleWriteLock 前台形态的因果链
- stats-to-cost ↔ query-plan-cache：custom↔generic 代价比较吃的就是选择率估算
- query-plan-cache ↔ opfusion：判定链选完 custom/generic 后，opfusion 决定要不要
  绕过执行器做点查融合；enable_pbe_optimization/opfusion 族参数同进 GPC env
  signature（取值不同=环境分裂）
- query-plan-cache ↔ adaptive-plan-mgr：瀑布判"走 generic"后进 gplanmgr 分岔——
  多候选按选择率置信区间匹配；pbe_optimization 强推 generic 的倾斜劣化正是
  adaptive 的解药（问题/解药对偶）；stats-to-cost 的选择率估算即其匹配特征
- query-rewrite ↔ query-pipeline：位表 ↔ 消费点
- g100-standard-params ↔ query-rewrite/query-plan-cache：naaj_optimizer 与
  pbe_optimization 两个地雷的机制本体
- core-events§6 ↔ commit-sync：STATUS 类的例外清单
- index-btree ↔ mvcc-visibility·autovacuum：膨胀源分诊边（删页两段式靠
  vacuum 驱动、OldestXmin 卡死索引条目常驻）——表膨胀与索引膨胀同根不同叶
- executor-operators ↔ operator-memory·query-pipeline·stats-to-cost：配额
  接线同一 SET_NODEMEM 底座（调 work_mem 不生效的机制出口）；NESTLOOP_PARAM
  =参数化路径的执行侧本体；sort/hash_time 列归因
- fd-vfd-budget ↔ linux/file-descriptors·troubleshooting/resource-exhaustion：
  同一 fd 的两层视角（内核 fdtable/rlimit ↔ 数据库 VFD 自我设限）；EMFILE
  现场判读从救火件下沉到机制层定论
- heap-tuple-attr ↔ mvcc-visibility·executor-operators：读同一 HeapTuple 的
  两个正交轴（可见性判过才轮到取值分支链）；slot deform 缓存是执行器
  逐列消费的底座
- partition-table ↔ fd-vfd-budget·metrics·diagnostic-map 症状9B：超多分区
  的 fd 预算出口（local 索引翻倍）；statio 分区归属（父表行=叶子和）的
  目录侧根源；分区统计陈旧→该分区计划劣化同链
- smp-stream-parallel ↔ executor-operators·debug-logging-gucs·operator-memory：
  同一配额底座的 dop 维度（SET_NODEMEM 摊派）；SMP 未生成的内核证据链
  （debug 配方出口）；AAS 高 CPU 低时四态先于 LOCK 判读
- debug-logging-gucs ↔ optimizer-deep·operator-memory·wait-events·check-inventory：
  同一组"深入取证"需求的参数出口（计划路径回放配 optimizer-deep 的代价复算、
  memory_tracking 配 operator-memory 的配额口径、锁问题明确不走 debug 参数而走
  等待视图）——诊断流程要开内核 debug 前先过此文件的死旋钮清单
- sysv-semaphore-usage ↔ locks·troubleshooting/resource-exhaustion·parameters：
  同一"阻塞睡眠"的机制层（LWLock/锁管理器 semtimedop 睡在 PROC 槽 sem 上）↔
  两端出口（sys% 高排除 semtimedop 嫌疑→锁争用定位；启动 FATAL/并发耗尽→
  kernel.sem 限额）；"kernel.sem 配大无害配小出事"的参数对照锚
- sql 层 ↔ query-pipeline·query-rewrite·query-plan-cache·stats-to-cost：
  机制底座（位表/14 级瀑布/选择率链）↔ 语句级实操（代价复算/SQLHC）——
  计划突变类问题两头进网：先查机制位，再进深潜改写

**OS 层互喂边（linux/ ↔ 库内，2026-10-25 立）**：
- kernel-data-path ↔ io-storage·memory：数据落盘全路径=两层缓存两层刷脏
  （shared_buffers vs page cache；pagewriter vs vm.dirty_* 四闸门）
- flush-checkpoint ↔ io-storage：库刷脏节奏撞 OS dirty 阈值限流=刷脏卡顿
  但盘不忙的形态
- commit-sync ↔ io-storage：fsync 语义与设备侧 flush 列对账（diskstats）
- buffer-manager ↔ memory：双缓存命中率分层判读
- wait-events ↔ cpu：us 高含 LWLock 自旋——自旋不产生等待事件的 CPU 盲区
- locks ↔ network：假死连接持锁（tcp_retries2 ≈15 分钟才放弃）→ 阻塞链
  持锁者先查 TCP 状态再杀
- network ↔ switch-side-logs：丢包分层链最外一段——主机侧三道关全绿而
  Retrans%/RTT 仍劣化时，交换机出口队列丢弃是"丢在中间网络"的直接证据；
  replication-lag Net 段与 awr 网络闸劣化的第三站（主机→对端→中间）
- network ↔ net-diag-fullchain：内核栈三道关=全链路 SOP 第 2 步的本体——
  硬件/物理/交换机/抓包各段全绿时疑点回灌 network 与 os-triage（别死磕网络）
- metrics ↔ net-diag-fullchain：Retrans%/RTT 口径边——awr 网络闸劣化数字
  是全链路 SOP 的触发入口，SOP 产出回填"丢在哪一段"的归因
- net-midpath ↔ network：conntrack/keepalive 旋钮本体在 network——idle 掐断
  的保活参数与 NAT 表满判读同源（7 200s 默认比中间设备 idle timeout 长的
  错配是高价值联判点）
- net-midpath ↔ connection-lifecycle：两种"连接消失"按指纹区分——中间设备
  idle 掐断（无 FIN/RST+规律时刻）vs 库侧被杀（有日志记录）；
  connection-lifecycle 假死连接形态是 idle 掐断的库侧下游
- net-midpath ↔ switch-side-logs：Eth-Trunk/堆叠/MAC 漂移/VRRP 的交换机
  logbuffer 侧证边——主机侧指纹（断流 1~3s/MAC 异答）去交换机日志找时对齐
- log-analysis ↔ memory：OOM kill ↔ 实例消失；53200（DB 限额）与 OOM kill
  （OS 耗尽）是两层闸门，分层定责
- metrics ↔ cpu·io-storage：iowait 双刃 / %util 并行陷阱——读数口径对账边
- parameters ↔ linux 全部：组 2/3/5/7 的 vm.\*/net.\* sysctl 机制本体在 linux/

**救火层接线（troubleshooting/ ↔ 机制/OS 层，2026-10-25 立）**：
- blocking-live ↔ locks·network·error-fatal：等待链快照→假死持锁→取消家族
- crash-panic ↔ error-fatal·wal-replay·memory：PANIC 家族→回放自愈→OOM 分诊
- startup-failure ↔ wal-replay·resource-exhaustion：崩溃恢复起点→IPC 残留清理
- connection-refused ↔ network·parameters：两级连接队列→组 7 旋钮
- connection-refused ↔ network（firewalld 节）：firewalld reload 丢临时
  放行→新连接被 REJECT；established 不 reset（conntrack 不清+新规则集
  established 兜底；iptables 后端仅断流窗靠重传扛）
- resource-exhaustion ↔ memory·network·operator-memory：四类耗尽→两层闸门分层
- tracing-tools ↔ wait-events：OS 侧函数级证据 × 库侧等待事件的互证边
- connection-lifecycle ↔ network·connection-refused·memory·locks：postmaster
  单线程 accept=两级队列唯一消费者（全队列堆积→sys/si 连锁在 network 深读节）；
  CAC_OOM×内存自保；客户端消失三形态→假死长事务→OldestXmin 边
- replication-lag ↔ wal-replay·commit-sync·replication-slots·network：lag
  三段分解的操作层出口（Net 段吃 network 丢包链；同步复制把备库慢灌回主库）
- replication-architecture→failover→split-brain→pitr-mechanism：ha 层内部链
  （位点语义→切换护栏/term→分叉处置→归档恢复）；pitr-mechanism↔wal-replay：
  恢复与备库回放同源（rm_redo 分派同引擎）

## 导航路径（从问题进网，形态编号对齐 SKILL.md 分诊表）

| 问题 | 形态 | 进网点 | 顺边走 |
|---|---|---|---|
| CPU 高/库内慢 | P1 | query-pipeline | →executor-operators（算子热点/配额）→linux/cpu（r 队列/调度）→wait-events（自旋盲区）→bottleneck-patterns P1 取证链 |
| 库外进程抢 CPU | P2 | linux/cpu | （调度/cgroup 竞争）→Load Profile 库侧 iostat 对账（bottleneck-patterns P2：差额=库外负载）→OS 层互喂边回库侧 |
| IO 读慢 | P3 | buffer-manager | →core-events§1/§2（计数型 vs 真 IO）→metrics（Rot 介质阈值）→index-btree（密度法判膨胀的读放大）→bottleneck-patterns P3 |
| 写延迟/刷脏/WAL 卡 | P4 | flush-checkpoint | →behind 三形态→core-events§5（双写盘速）→locks（WAL 组插入）→commit-sync（fsync） |
| 锁等待/死锁 | P5 | locks | →core-events§4（行锁协议：avg=顶层事务剩余）→error-fatal（40P01 仅硬环/57014）→awr-report-reference§13（waitchain） |
| 临时落盘/spill | P6 | operator-memory | （tuplesort/hash 算子配额）→executor-operators（SET_NODEMEM 接线："调 work_mem 不生效"出口）→parameters（work_mem 安全公式） |
| 内存涨/53200/OOM | P7 | operator-memory | （MemoryContext 记账）→buffer-manager（全局水位）→linux/memory（水印/oom_adj）→log-analysis（OOM 联动） |
| 表膨胀/dead tuples | P8 | mvcc-visibility | →autovacuum（触发公式：~5% 是公式行为）→replication-slots（谁压 OldestXmin）→flush-checkpoint（CSNLOG 截断） |
| 主备延迟 | P9 | replication-lag | 三段分解→Net 段吃 network→Fsync 段吃 flush-checkpoint/io-storage→Apply 段→wal-replay（三来源）→replication-slots（槽积压连锁）→commit-sync（同步复制灌回主库） |
| 计划突变/解析贵/SQL 变慢 | P10 | query-pipeline | →query-rewrite（位）→query-plan-cache（第 6 次切换）→stats-to-cost（选择率）→g100-standard-params（地雷） |
| 连接风暴/连不上/连接慢 | P11 | connection-refused | 四道闸从外向里→闸 3 进 network 全队列堆积节（sys/si 连锁）→闸 4 进 connection-lifecycle（准入闸/postmaster 单线程 accept） |
| 报错风暴 | P12 | error-fatal | →log-analysis（×AWR 联动）→节奏判读（轮询器）→connection-lifecycle（认证失败类链路）→g100-standard-params（版本地雷核对） |
| commit 慢/延迟抖动 | P4/P9 | commit-sync | →Replication lag 分解→wal-replay（Apply）→replication-slots（xmin/槽）→locks（组插入/sys CPU 盲区） |
| 参数要不要调 | — | parameters | →g100-standard-params（基线对照）→机制文件（为什么）→禁入清单（永不建议项） |
| OS 层症状（si 高/丢包/盘慢） | — | linux/ 对应文件 | 按各文件判读速查表进网，沿互喂边回库侧：cpu→wait-events 自旋盲区、io→flush-checkpoint 互踩、net→locks 假死持锁、memory→log-analysis OOM 联动 |
| 现场救火（挂起/起不来/崩了/耗尽） | — | troubleshooting/ 对应文件 | 先止损后定位：保全现场→文件内分诊表→机制文件下钻；要函数级证据再进 tracing-tools |

> 形态列 = `performance/bottleneck-patterns.md` P1~P12；先过形态闸取证，机制解释
> 按该文件"形态→机制锚索引"挂文件。"—"= 非瓶颈形态入口（知识查询/OS 侧/救火）。

## 网络维护规则

1. 新文件入库 = 新节点：在 SKILL 路由 + ROADMAP + 本图各登记一行，并在文件尾
   指回本图；新知识若勾稽了旧文件，**旧文件也要补边**（互证是网的价值所在）
2. 边必须带语义标签（解释"为什么相关"），裸链接不算边
3. 断链/孤儿由校验脚本把关：`scripts/check_graph.py`（域根运行；断链/登记
   缺口/孤儿三类，退出码非零即红，每次改图改路由后必跑）
4. **逻辑三查（2026-10-25 用户令：有理有据、走得通）**：
   ①每条边两端文件正文必须真有该机制承接——边是正文机制点的登记，
   不是愿望（加边前先确认两端文件里写了这条机制）
   ②每条导航路径逐步可走——进网点存在、每一跳落在真实节点且该跳的
   边/机制承接存在
   ③主链与支线不混——因果链上一跳必须有直接因果关系（双写≠复制的前置
   就是反例教训）；check_graph.py 的图结构检查负责机械兜底
