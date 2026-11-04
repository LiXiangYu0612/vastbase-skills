# AWR 诊断导图（症状 → 章节 → 证据链 → 根因 → 建议）

本文件是"有什么问题看哪个章节"的总导航：从症状进，沿证据链走，到根因与建议出。
配套：章节列语义查 `awr-report-reference.md`；六步流程查 `awr-report-analysis.md`；
形态取证查 `bottleneck-patterns.md`；指标口径查 `metrics.md`。

## 总览树（入口在这里，往下钻）

```text
【怎么进来】三个入口，先对号
│
├─ 入口A 主诉"慢/卡/抖" ──────────► 症状12（总入口：先画像定方向再分流）
├─ 入口B 指标越阈（OS/DB/事件榜）─► 直接对症状1~11
└─ 入口C 报错（日志/客户端/dmesg）► 症状10（错误风暴）+ 按错误族转对应症状
        内存错→1 │ 连接错→5 │ 复制错→6 │ 死锁→4 │ 53200→1/8
│
【进去之后】每个症状必走"四问"，缺一降观察项
│
└─ 四问：①定量(event%DBtime，≥10%才溯源) ②现场(ASP/waitchain)
          ③责任(topsql占比大优先查)      ④机制(堆栈/internals/linux源码锚)
│
【十二症状】（左边=入口关键词，箭头=路径速览，细则见下文各节）
│
├─ 1 内存涨/OOM ─ MaxSessionSample谁吃 → Summary总量 → SharedΔ泄漏
│                 → GUC配比 → OS侧库内外 → 日志时刻对齐
│                 → ★OOM 先四型分诊（内核/cgroup/53200/换页前驱）→ oom-analysis.md
├─ 2 CPU高 ───── Steal先排除 → TimeModel方向(含后台线程) → ASP桶
│                 → topsql_CPU维 → 前台栈 → 库外对账 → ★自旋盲区分支
│                 → ★sys 高变体专项分流（fork 税/IPI 广播）→ cpu-sys-high.md
├─ 3 IO慢 ─┬─ 读：定量DataFileRead → Rot定介质 → topsql物理读 → 命中率
│           └─ 写：定量LogFileSync → Pagewriter behind(LSN滞后) → WAL速率
│                    → 后台栈+flush列 → 备库联查
├─ 4 锁等待 ┬─ LOCK重：定量 → waitchain谁锁谁 → 长事务 → 死锁环
│           └─ LWLOCK轻：定量 → 桶峰值(★无阻塞关系) → 前台LWLock栈
│                        → VB源码分析 → avg≈0甄别
├─ 5 连接 ───── TCP状态(time-wait/close-wait) → ListenDrops → SQL calls
│                 → conntrack+accept队列 → somaxconn
├─ 6 主备延迟 ─ lag分解(Net/Fsync/Apply) → WAL速率 → 写入SQL → slot
│                 inactive高危 → 备库三形态(串行/大事务/冲突cancel)
├─ 7 表膨胀 ── dead榜+table_age → 长事务元凶 → AV worker_cnt
│                 → ★反向anti-wraparound(20亿逼近)
├─ 8 落盘 ──── TempSpill(★只统计Sort+HashJoin) → 逻辑读 → 算子配额
│                 (operatorMaxMem非work_mem) → 换页
├─ 9 计划 ───┬─ A 解析贵：PlanTime维 → 硬解析风暴
│            └─ B 计划劣化：★昨天好今天慢 → 统计陈旧MCV滑移 → re-analyze
├─ 10 错误风暴 PANIC最优先 → 级别计数(>10/s=风暴) → 时刻对齐 → 错误族表
├─ 11 延迟抖动 per-min+ASP锁尖刺 → dmesg/THP → NUMA/Steal间歇/ckpt周期
│                 → ★Cgroup限流(nr_throttled Δ) / sirq单核饱和(矩阵%sirq列)
└─ 12 慢(总入口) AAS每核比 → 方向 → 异常时段 → 分流
                  (算→2 │ IO→3 │ 锁→4 │ 网→5 │ 内存→1 │ 突变→9B
                   │库慢但busy不高→Cgroup配额/cpuset/Steal三分流) → topsql
│
【贯穿纪律】十条（见第五节）：不过度诊断 / 单指标不定论 / 库内外对账 /
            介质分档 / 日志时刻锚 / 参数不猜 / 改SQL在前调参在后 / 根因到源码层
│
【机制锚】每个结论的源码出处见第六节「机制锚点索引」（VB internals + linux）
```

## 〇、诊断起点：真的有问题才诊断（不过度诊断）

诊断的触发是"真的有问题"——用户投诉、指标越过硬阈值、日志报错、硬件/内核事件
（dmesg）。**不是翻遍 17 章节找点啥说**。三条底线：

1. **空载窗口不硬挑**：AAS 极小、DB Time/Elapsed <1% 时，占比结论先质疑分母，
   小分母会把无害事件放大成"高占比"（见 `metrics.md` 计数型事件节）。
2. **正常波动不叫问题**：per-min 曲线在阈值内的起伏、计数型事件的高 waits、
   命中率略低但物理读也低——这些是常态不是事故。
3. **无异常就如实说无异常**：OS 全程安静 + ASP 无尖峰 + 日志无报错 = 本快照区间
   未发现需处理的问题，一句话结论即可，不凑数、不硬挑、不把正常项写成问题。

## 一、event 与 ASP 的分工：定量靠 event，现场靠 ASP，二者互补

VastBase 的 wait event 有"定量"和"定性"两面，不能一刀切：

**event 的定量不可丢（影响面）**：Top 10 Wait Events / Wait Events 全量的
Total Wait Time、%DBtime、Avg、Waits——这是唯一能回答"这个等待吃掉多少 DB Time、
对 DB 影响多大、平均等多久"的数字。判断一个等待值不值得管、优先级多高，靠它，
不靠 ASP（ASP 给的是活跃会话数，不是时间占比）。**闸门：%DBtime ≥10% 才值得
溯源**（不足 10% 不定位为异常，最多观察项——与六步流程第 3 步一致）；过闸后
平均等待超阈（IO ≥20ms / 锁类 avg ≥100ms 警戒·≥1000ms 异常，RULES 单源）走重取证。

**event 的定性要甄别**（详见 `wait-events.md`）：
1. STATUS 类 76 个 = 执行阶段标记（HashAgg-build hash、Sort、flush data…），不是等待，不计占比。
2. IO_EVENT 里的计数型（BufHashTableSearch 家族）= 块访问量镜像，不是锁竞争。
3. 高频 avg≈0 = 测量括号（pgstat_report_waitevent 包住一次同步操作）。

**现场是 ASP 的活（event 聚合没有）**：谁在等、阻塞链（waitchain）、时段 Top SQL——
ASP 5min 桶（桶内逐秒计数）给因果现场，event 聚合给不了。

⇒ 等待问题四问齐：**影响多大（event %DBtime）→ 谁在等谁锁谁（ASP waitchain）→
谁的 SQL（topsql）→ 为什么（堆栈/源码/OS）**。event 定量 + ASP 现场互补，
丢任何一个都定不了论，缺一件就降观察项。

## 二、证据源强弱（按此分配判读权重）

| 等级 | 证据源 | 为什么 |
|---|---|---|
| 强 | **event 定量**（Total Wait Time / %DBtime / Avg） | 影响面：这个等待吃掉多少 DB Time、优先级多高——判断影响的唯一来源 |
| 强 | **ASP waitchain**（root_block_sessid + 持锁 SQL + 受害 SQL + 阻塞数） | 现场：谁在等、谁锁谁、5min 桶内逐秒计数 |
| 强 | **堆栈**（Callstack 前台 LWLOCK 栈 / 后台线程栈，符号+调用点） | 机制层"在哪发生" |
| 强 | **Max Session Memory Sample**（user·addr·PID·SQL·Top3 Contexts） | 点名谁吃内存、跑的什么 SQL |
| 强 | **库侧 iostat vs OS disk 对账** | 库内 vs 库外，唯一可靠判别 |
| 强 | **dmesg / logrpt**（时刻锚） | 硬件/内核/OOM 实锤，定位到分钟 |
| 中 | **topsql 九维** | 责任 SQL，但频次型/单次贵要分、%Total 口径要核 |
| 中 | **OS per-min 序列** | 时段定位，均值会稀释尖刺 |
| 需甄别 | **event 定性**（Waits 次数、事件类） | STATUS/计数型污染，定性要核源码再当等待 |

## 三、诊断四问（每个症状必走，缺一降观察项）

1. **定量（影响多大）**：event 的 %DBtime / Total Wait Time / Avg——回答"这个等待
   占多少 DB Time、影响多大、平均等多久"，是优先级判断的核心，不可丢。
2. **现场（谁在等）**：ASP 采样——waitchain 给阻塞根、Top SQL 给时段 SQL
   （5min 桶、桶内逐秒计数）。event 聚合给不了现场。
3. **责任（谁的 SQL）**：topsql 九维——**对应问题维度上占比大（%Total 高）的 SQL
   必须优先查证**：它就是主要责任候选，直接拉它的执行计划/扫描/数据倾斜看是不是
   真有问题；再分型频次型（高 Exec×中 per-exec=量大成灾）vs 单次贵
   （低 Exec×高 per-exec=计划差/倾斜）。"占比大"要甄别：是真问题（计划差/大扫描/
   倾斜）还是正常大业务，靠 per-exec 分型；占比大≠一定有问题，但占比大不查=漏掉
   主要责任。
4. **机制（为什么）**：机制层证据——Callstack 堆栈（前台 LWLOCK 栈 / 后台线程栈 /
   AutoVacuum Workers）、内核机制（`internals/`）、OS 机制（`linux/`）、日志
   （logrpt/dmesg）——回答"内核/块层/协议栈哪条路径放大它、什么参数动它"。堆栈
   只是机制证据之一，按症状选对应的机制源，不局限在堆栈。

---

## 四、症状 → 诊断路径

每个症状按三层走，**OS 侧（根因）→ DB 侧（表现）→ OS↔DB 对账（因果）**：
OS 侧给"环境根因"（net/tcp/内存/磁盘/CPU 的 linux 机制），DB 侧给"库内表现"
（等待事件/ASP/topsql/日志），对账成对看——同向=实锤因果，对不上=库外因素或口径问题。
OS 侧强关联的症状（连接/IO/内存/CPU/主备延迟）三层都详细；OS 侧弱的（表膨胀/解析）
OS 侧只做排除。

### 1. 内存涨 / OOM（日志有报错）★用户点名

触发：logrpt 报 OOM / 53200、dmesg 报 Memory-OOM、MemAvailable 低、换页活跃、
客户端报 "out of memory / 内存不足"。

路径（六步，缺哪步补哪步）：

```
① 现场    Max Session Memory Sample（每5min最大会话：谁·跑的什么SQL·Top3 Contexts）
            └─ 这是动态内存满的第一现场：直接点名吃内存的会话和 SQL
② 总量    Session Memory Summary（会话数/总量/均值/最大）+ Max Session Memory 实测
            └─ 判"并发放大"：★配额单位是计划节点（算子），不是会话——单查询总量 =
               配额 × 算子数（每个排序/哈希节点各占一份）；总量风险 = 并发查询数 ×
               每查询算子数 × 配额，且以 operatorMaxMem（弹性上限）而非 work_mem 为准
               （work_mem 只是优化器估算 operatorMemKB 的兜底，见 operator-memory.md）
③ 泄漏    Top 10 Shared Memory Context（Begin/End/Δ，按 contextname）
            └─ Δ 持续涨不回落=共享上下文泄漏候选，列具体上下文名
④ 配比    Memory GUCs（max_process_memory 总封顶 vs 物理内存）
            └─ ★max_process_memory 是进程级记账（超限报 53200），与算子 spill 决策是两层：
               调大算子配额（work_mem/估算）会先打满这一层；各池之和超物理内存=配置问题，
               给量化（合计 vs 物理内存）
⑤ OS侧    OS Memory（MemAvailable/换页/Slab/THP/碎片）+ linux/memory.md 机制层
            └─ 分"库吃满"还是"别的进程吃满"（换页但库内存平稳=库外）
⑥ 日志    logrpt 53200/OOM 时刻 + dmesg Memory-OOM 时刻 → 与 ① 的会话/SQL 对齐
            └─ 报错时刻对上采样时刻=实锤因果，对不上=观察项
```

定论三分：
- **泄漏**：Shared Context Δ 单调涨 + 会话退出不回落 → 列上下文名，转内核排查
- **配置不足**：各池之和≈或>物理内存，或并发查询数×每查询算子数×配额（operatorMaxMem 为准）超物理内存 → 量化后调参数
- **库外挤占**：OS 换页但库内存平稳 → 别的进程吃，OS 层结论

建议次序：改 SQL（消大算子/降并发）在前；调 max_process_memory/work_mem 在后，
且按算子级语义核算（配额单位=计划节点、operatorMaxMem 为准，见 `internals/operator-memory.md`），不猜默认值。

### 2. CPU 高

触发：busy 高、r>N、DB Time 高、CPU_TIME/DB_TIME 高。

```
① 排除    CPU utilization 先看 Steal：>0 = 虚拟化层抢 CPU，与库无关直接报宿主侧
② 方向    Time Model（CPU_TIME/DB_TIME：≥70%偏计算/<40%偏等待）——★CPU_TIME 含后台线程
            （pagewriter/walwriter/autovacuum 全算），只作方向；CPU 型结论要 OS busy+r 佐证
③ 现场    ASP 桶（max_active_sess 峰值 + 时段 Top SQL）
④ 责任    topsql CPU Time 维（频次型 vs 单次贵）
⑤ 机制    Callstack 前台采样热点函数 + OS busy/r（linux/cpu.md 调度/运行队列；
            sys 高专项四层分诊→troubleshooting/cpu-sys-high.md）
⑥ 库外    OS busy 高但库内 CPU_TIME 低 → 库外进程（点名对齐备份/ETL/杀毒窗口）；
            库侧 iostat vs OS disk 对账（差额=库外负载）
```

定论：r>核数 + SQL CPU 榜占比高 + CPU_TIME 高 = SQL 拖 CPU（形态1）；
OS 高库内低 = 库外进程（形态2）；CPU 高但 iowait 也高 = 不是 CPU 不足，转形态3。
**★自旋盲区分支**：CPU 高但等待事件全低/占比极小——自旋不睡眠、不记等待事件，
竞争代价只表现为 CPU（如 WAL 插入跟随者 sched_yield 自旋，locks.md：跟随者自旋
大头不进任何等待事件账本）；靠 Callstack 堆栈定位自旋点，别因"没等待"就断无问题。
建议：改 SQL/计划在前；CPU 参数（并行度/SMP）在后。

### 3. IO 慢（读 / 写分）

读触发：physical reads 高、DataFileRead 靠前、iowait 高、b 高。
写触发：LogFileSync/WALWrite 靠前、w_await 高、pagewriter behind 涨。

读路径：
```
① 定量    Top 10 Wait Events 的 DataFileRead %DBtime → 读等待吃多少 DB Time、影响多大
② 介质    Disk 表 Rot 定介质（util 永不判读，看 await 分档：SSD>5/HDD>20）
③ 责任    topsql Physical Reads 维 + Object Reads 榜（打哪个表哪个索引）
④ 命中    Cache Hit（低命中+低物理读=伪问题）
⑤ OS侧    per-min await + MemAvailable（换页挤缓存）
```

写路径：
```
① 定量    Top 10 Wait Events 的 LogFileSync/WALWrite %DBtime → 写等待吃多少 DB Time、影响多大
② 现场    Pagewriter（remain_dirty + behind xlog_insert）
            └─ ★behind = current_xlog_insert_lsn − queue_head_page_rec_lsn，是滞后距离
               （LSN 差），不是脏页体积；分两支：behind 涨+remain_dirty 涨=刷脏吞吐
               跟不上；behind 涨+remain_dirty 稳=队首老页被 pin 刷不动（长查询持钉）
③ 责任    Load Profile（commits/s 频次 + WAL written 体积 + WAL write time）
④ 机制    后台堆栈 WalWriter/CheckPointer + diskstats flush 列（fsync 实锤）
⑤ 联查    Replication 延迟（同步提交被备库拖？）
```

定论：低命中+IO 等待+物理读 SQL+iowait 四点齐=真读瓶颈（形态3）；
behind 涨+remain_dirty 涨=刷脏能力不足 vs behind 涨+dirty 稳=老页被 pin（查长查询）
vs dirty 稳+w_await 高=存储慢（形态4）；w_await 周期性尖峰对齐 pagewriter 波峰=
checkpoint 抖动，不是持续慢盘。
建议：读=shared_buffers/预热/改扫描；写=checkpoint 族/批量提交/备库，先改 SQL。

### 4. 锁等待（LOCK 重量级 vs LWLOCK 轻量级）★用户点名

触发：Top Event 里 LOCK/LWLOCK 靠前、ctxt/s 高、客户端卡住、日志死锁/锁超时。

**先分型**：LOCK_EVENT（重量级：行锁 transactionid/表锁 relation，17 类）还是
LWLOCK_EVENT（轻量级：buffer mapping/lock manager/CLog 等，244 类）——两者现场
完全不同：**waitchain 只对 LOCK 有效，LWLOCK 在 ASP 里没有阻塞关系**
（机制根源：gs_asp 的 block_sessionid 只在重量级锁等待时填充，vb_tool
run_asp_waitchain_query 只取它非空的行；LWLock 持有者要靠专有视图
pg_stat_get_backend_lwlocks() 或前台堆栈——见 `wait-events.md` waitchain 适用范围节）。

LOCK（重量级）：
```
① 定量    Top 10 Wait Events 的 %DBtime / Total Wait Time → 这个锁等待吃多少 DB Time、影响多大
② 现场    ASP waitchain（root_block_sessid 直给持锁会话+持锁SQL+受害SQL+阻塞数）
            └─ waitchain 是重量级锁的阻塞现场，直给"谁锁谁"
③ 现场    ASP 桶 wait_lock 逐秒计数峰值（时段定位）
④ 责任    Long Transaction Monitor（谁持锁久）+ topsql 同表 DML
⑤ 佐证    logrpt 死锁详情
```

LWLOCK（轻量级）：
```
① 定量    Top 10 Wait Events 的 %DBtime / Total Wait Time → LWLOCK 等待影响多大
② 现场    ASP 桶 wait_lwlock 逐秒计数峰值（时段定位；★waitchain 不管用，LWLOCK 无阻塞关系）
③ 机制    Callstack「Foreground Threads (LWLock waits)」：Wait Event+PID+SQL+Stack
            └─ LWLOCK 严重就看等待 LWLOCK 的线程堆栈，定位具体 LWLOCK 和调用点
④ 根因    走 VB 源码分析路径（wait-events.md 溯源工作流）：se 取语义+源码触发点 →
            堆栈定位调用点 → 源码函数核对，回答前台/后台、争什么资源、谁放大它
⑤ 甄别    avg≈0 且总占比低=高频临界区正常态（自旋拿锁极快，不构成影响）；
            总占比高才值得查；高频 IO 族事件先按计数型识别法核源码（BufHashTableSearch
            =块访问量镜像，锚 bufmgr.cpp BufferAlloc():3006）
⑥ 死锁    logrpt 死锁详情（deadlock detected 给出环的两条边）→ 两边 SQL/会话
            对照 waitchain 定主责方
```

定论：LOCK=长事务+热点对象+持锁三合=业务长事务持锁；LWLOCK=内核级热点（如
BufferContent 热点块，配 Object Activity 定位热块），根因必须到源码层。
建议：改业务持锁逻辑在前；lockwait_timeout 类参数只止血不根治。

### 5. 连接问题（报错 / 风暴 / 认证慢）

```
① 现场    DB Process TCP 状态（time-wait 海量=短连接风暴；close-wait 堆积=应用不关；
            Syn-Recv 堆积=半连接队列满）
② 现场    Network Protocol Errors（ListenDrops/Overflows>0=accept 队列溢出）
③ 责任    Load Profile（SQL calls/会话速率）+ topsql 高频短查询
④ OS侧    conntrack fill + listener Recv-Q/Send-Q + 后台堆栈 postmaster 准入
⑤ 参数    OS Kernel Parameters（somaxconn/tcp_max_syn_backlog）+ logrpt too many clients
```

定论：短连接风暴 / 应用不关连接 / accept 队列溢出，三者处置不同（连接池/应用修复/内核参数）。
建议：连接池复用在前；内核参数在后。

### 6. 主备延迟（含备库侧三形态）

```
① 分解    Replication lag 分解（Net=网络+备端写 / Fsync=备端刷盘 / Apply=备端回放）
② 源头    Load Profile WAL 速率（写风暴源头）
③ 责任    topsql 写入型 SQL + Long Transaction（备库回放被大事务挡——Apply 滞后
            常是主库大事务的影子）
④ 高危    Replication Slots inactive（active=f）=无限保 WAL → 主库磁盘涨，单独立条
⑤ 备库侧  Apply 大的三来源（wal-replay 源码）：单回放线程串行（备库 CPU：回放线程
            吃满？）/ 大事务回放 / 回放冲突等待（指数退避→max_standby_streaming_delay
            →cancel 备库查询，logrpt 找 conflict cancel 记录）；★备库查询慢的另一形态：
            取快照要等回放过清理点——备库查询延迟尖刺先看回放位点差，不是备库 SQL 本身
```

### 7. 表膨胀

```
① 现场    Top Objects by Dead Tuples（dead 高 + table_age）
② 元凶    Long Transaction Monitor（卡 vacuum 的最老事务 + OldestXmin）
③ 机制    Callstack AutoVacuum Workers（worker_cnt 恒0=真空没跑；全忙=吞吐不足）
④ 佐证    logrpt autovacuum 跳过/防抖 warning + vacuum 参数族
⑤ 反向    ★anti-wraparound：table_age 逼近 20 亿（2^31 余量）→ 强制激进 vacuum
            （防 xid 回卷，与业务争 IO/CPU——"IO 突飙但写入 SQL 没涨"的隐蔽根因，
            autovacuum.md：回收边界由 OldestXmin 决定，被长事务/复制槽压住时加剧）
```

### 8. 落盘 / 排序溢出

```
① 现场    topsql Temp Spill 维 + Load Profile temp bytes（>0 即落盘）
            └─ ★Temp Spill 只统计 Sort 算子+HashJoin；聚合内排序（string_agg ORDER BY）/
               HashAgg 不计 → "没落盘记录"≠真没落盘，temp bytes 才是不漏的总账
② 责任    topsql Logical Reads（扫描大）+ Memory GUCs（work_mem / operatorMemKB 估算）
③ 总量    Session Memory Summary + Max Session Memory（并发查询×每查询算子数×配额，
            以 operatorMaxMem 弹性上限为准）+ fd pgsql_tmp
④ OS侧    换页 / MemAvailable（落盘是否已挤到内存）
```

定论：Temp Spill 的原因要分清，**不能一律归"work_mem 小"**：
- **配额不足**：算子配额（operatorMemKB 估算值，或兜底 work_mem）小于工作集
- **全局水位**：gs_sysmemory_busy 时未超自身配额的算子也提前落盘（系统内存紧张）
- **数据倾斜**：大分组排序/哈希集 > 任何合理配额，调参无效只能改写 SQL
- ★注意：对带优化器估算配额（operatorMemKB>0）的算子，**调 work_mem 可能根本不生效**（估算优先）
建议：先改 SQL/计划消扫描、消倾斜；必须调就按"并发查询数×每查询算子数×配额"核算，
以 operatorMaxMem（弹性上限）而非 work_mem 为准（见 `internals/operator-memory.md`）。

### 9. 解析贵 / 计划劣化（两种不同的病）

触发 A（解析贵）：topsql Plan Time 维靠前（总占比>30% 才值得立论）、
Time Model parse/plan 阶段高。
触发 B（**计划劣化**）："昨天还好好的今天突然慢"、seq_scan 激增、同 SQL 执行时间
突变（无 DDL 也无数据量变化）。

```
A 解析贵：
① 责任    topsql Plan Time 维 + Executions 频次
② 现场    Time Model parse/plan 阶段耗时
③ 机制    query-plan-cache / query-rewrite（计划缓存失效、重写开销）
④ 佐证    logrpt 计划缓存失效迹象
定论：高频+贵计划=硬解析风暴（绑定变量缺失）；低频+贵计划=复杂 SQL 计划生成贵。

B 计划劣化（★DBA 最高频场景之一）：
① 现场    Object Statistics 的 seq_scan 激增 + 同 SQL 计划前后对比
② 根因    统计陈旧：数据漂移未 re-analyze → rows 估算稳定偏差 → 计划翻转
            （stats-to-cost：MCV 边界滑移分诊——同一值在/不在 MCV 边界）
③ 佐证    reltuples 与实际行数差、analyze 蓄水池是否轮到该表（大表轮换慢）
④ 机制    stats-to-cost（采样→MCV/直方图→选择率→代价）+ 计划缓存被失效重生成
定论：计划劣化=re-analyze/锁定计划（hint/plan hint）；不是改 SQL 的病。
```
建议：A=参数化/prepared 在前；B=先 re-analyze 看计划回不回来，别急着改 SQL。

### 10. 错误风暴

触发：logrpt 级别计数异常；**PANIC 任一条最高优先**。

```
① 现场    logrpt 级别计数（★先按快照区间时长归一成 条/小时 再下结论）
            + 同码错误 >10 次/秒=风暴
② 时刻    dmesg + logrpt 时间戳 → 与 ASP/OS 异常时段对齐
③ 责任    topsql 报错 SQL + 认证失败族；慢 SQL 分桶先剔采集指纹
④ 机制    log-analysis.md 错误族关联表找 AWR 侧影子
```

三归责（log-analysis.md 五步判读）：**监控/采集侧最高优先**（监控账号权限错 =
对应采集一直失败 = 监控盲区）→ 应用侧（认证死轮询 / 唯一键重试，修一次消一整类）
→ 库侧（OOM / no space / WAL 段缺失 / 死锁，才走关联表找 AWR 影子）。

定论：错误是佐证与时刻锚，不孤证定论；先报风暴再查根因（错误族关联表走
`log-analysis.md`）。

### 11. 延迟抖动（时快时慢）

```
① 时段    OS per-min 全轴（busy/iowait/r/b/await/丢包/RTT）+ ASP 桶 → 锁尖刺分钟
② 现场    ASP 桶尖刺分钟的 waitchain + Top SQL
③ 机制    dmesg（compact_stall/OOM/Storage 事件时刻）+ THP/碎片
④ 佐证    logrpt 前后窗切片（P 触发区 vs W 症状区谁先谁后）
⑤ 易漏    NUMA 跨节点（Local Ratio<90% 时远程访存 1.5~2× 延迟，CPU 不高但整体慢）、
            Steal 间歇（宿主负载时段性）、checkpoint 周期抖动（对齐 pagewriter 波峰）、
            **Cgroup 配额限流（Cgroup 章 nr_throttled/throttled_time Δ>0，容器化交付
            必查——库内一切正常只是被按住）**、sirq 单核饱和（Softirq 矩阵 %sirq 列单核
            逼近 100%，机器级 si 均值掩盖）
```

### 12. 慢（不知道哪慢）——总入口

```
① 画像    报告头 AAS + 每核比（>1 超核/0.5~1 接近/<0.5 余量）
② 方向    Time Model CPU_TIME/DB_TIME（算 vs 等）——★CPU_TIME 含后台线程，只作方向，
            下钻用 OS busy/r + ASP 桶佐证
③ 定位    OS per-min 序列 + ASP 桶 → 异常时段
④ 分流    按方向进对应症状：偏算→症状2、偏 IO 等→症状3、偏锁→症状4、偏网络→症状5、
            偏内存（换页/挤压）→症状1（内存问题不一定有报错，换页本身就是慢因）；
            ★库慢但 OS busy 不高→先三分流：Cgroup nr_throttled Δ（配额咬合）/
            cpuset.effective（给核少）/Steal（宿主抢）——都不占才回库内找
⑤ 佐证    topsql 九维找主要责任 SQL；★"突然变慢"另查症状9B 计划劣化
```
```

---

## 五、跨症状通用诊断手法（顶级专家经验）

1. **四问缺一降观察项**：定量(event %DBtime)+现场(ASP)+责任(topsql)+机制(堆栈/linux)，单源定论=危险。
2. **event 定量必看、定性甄别**：%DBtime/Total Time 判影响（不可丢）；STATUS 不计等待、计数型先识别、高频 avg≈0 是测量括号。
3. **topsql 占比大优先查**：对应问题维度上 %Total 高的 SQL 是主要责任候选，必查其
   计划/扫描/倾斜；占比大要甄别真问题 vs 正常大业务，但占比大不查=漏掉主要责任。
4. **库内 vs 库外**：库侧 iostat 与 OS disk 对账是判别器；对不上账=库外因素。
5. **介质分档**：IO 结论先看 Rot，SSD/HDD 阈值不同；util 永不判读。
6. **时段不是均值**：per-min 序列 + ASP 桶锁尖刺，均值稀释事故。
7. **日志做时刻锚**：报错时刻与采样时刻对齐才叫因果，对不上降观察项。
8. **参数不猜默认**：Database Parameters + OS Kernel Parameters 两节自含，落成
   "参数名=当前值→建议值+理由"。
9. **建议次序**：改 SQL/计划/业务逻辑在前，调参数在后（参数是止血不是根治）。
10. **根因到机制层**：不许停在数字层，挂下面锚点索引对应的源码结论说"为什么"。

## 六、机制锚点索引（每个结论可追溯到源码）

本导图的机制结论逐条挂源码锚（行号会漂以函数/文件为锚；无锚的如实标"启发式"）：

| 症状/结论 | 源码锚 | 知识文件 |
|---|---|---|
| work_mem=算子配额；operatorMemKB 估算优先；operatorMaxMem 弹性上限；gs_sysmemory_busy 提前落盘；倾斜调参无效 | `SET_NODEMEM` executor.h:114；memctl.cpp AssignMemOpConsumption ~3090；tuplesort.cpp:550-579；hashjoin.h:99 | `../internals/operator-memory.md` |
| max_process_memory 进程记账、超限报 53200、与 spill 两层 | ERRCODE_OUT_OF_LOGICAL_MEMORY 分配层拦截 | `../internals/operator-memory.md` |
| Sort 落盘状态机（write file/fetch tuple 成对=真归并；只 write=单 run） | tuplesort.cpp TSS_*；inittapes:2345 maxTapes=merge_order+1 | `../internals/operator-memory.md` |
| Temp Spill 榜只统计 Sort+HashJoin（聚合内排序/HashAgg 不计） | vb_tool 采集侧口径（statement 视图列），temp bytes 才是不漏总账 | `../awr-report-reference.md` |
| waitchain 只对 LOCK；LWLOCK 无阻塞关系 | gs_asp.block_sessionid 仅重量级锁填充；vb_tool run_asp_waitchain_query 只取非空行；LWLock 持有者=pg_stat_get_backend_lwlocks() 专有视图 | `../wait-events.md`（waitchain 适用范围节） |
| 事件四类 244/17/80/76；STATUS=阶段标记；BufHashTableSearch=计数型 | se 知识库 417 事件；bufmgr.cpp BufferAlloc():3006 | `../wait-events.md` |
| CPU_TIME 含后台线程 | getCpuTime() 实测口径，与 OS busy×核数对账法 | `../metrics.md` 核心口径节 |
| behind=LSN 滞后距离非脏页量；behind 涨+dirty 稳=老页被 pin；WAL-before-data 卡刷脏 | behind = current_xlog_insert_lsn − queue_head_page_rec_lsn；双写批次 XLogWaitFlush 同边 | `../internals/flush-checkpoint.md` |
| lag 分解 Net/Fsync/Apply（sent−write / write−flush / flush−replay） | sender 视图 LSN 四点 | `../internals/commit-sync-events.md` |
| slot inactive → restart_lsn 不推进 → WAL 无限保留 | ReplicationSlotsComputeRequiredLSN()（:860 起）聚合 min(restart_lsn) | `../internals/replication-slots.md` |
| vacuum 触发阈值 / worker 让路 / 限流 | autovacuum.cpp:3171 relation_needs_vacanalyze；:1652-1758 mode 门控 | `../internals/autovacuum.md` |
| 每连接一线程（Threads 对连接数基线） | openGauss 6.0.0 基线默认形态 | `../internals/connection-lifecycle.md` |
| 准入闸 too many clients / 认证慢 | BackendStartup 准入闸 CAC_TOOMANY/CAC_OOM | `../internals/connection-lifecycle.md` |
| time-wait=短连接风暴 / close-wait=应用不关 / 两级连接队列溢出=ListenDrops | TCP 关闭链路 tcp.c:3191,3264；两级队列（syn backlog+accept 队列） | `../linux/network.md` |
| conntrack 表满 early_drop 丢包 | nf_conntrack_core.c:1674 | `../linux/network.md` |
| softnet dropped=backlog 满 / time_squeeze=预算耗尽 | net-procfs.c:150 渲染；netdev_max_backlog sysctl_net_core.c:464 | `../linux/network.md` |
| compact_stall>0=直接压缩停顿（延迟尖刺源） | /proc/vmstat compact_stall=同步压缩次数 | `../linux/memory.md` |
| iowait 双刃（idle 才累计，忙时不计） | core.c:8089 语义 | `../linux/cpu.md` |
| load 含 D 状态（nr_running+nr_uninterruptible） | 调度器负载口径 | `../linux/cpu.md` |
| await 含排队时间、介质分档 SSD>5/HDD>20；util 永不判读 | multiqueue 下 io_ticks 不分并行度 | `../linux/io-storage.md` |
| MemAvailable 分流真不足 vs 可回收占位；Slab 三值 | 水位线/kswapd/双 LRU（meminfo.c 口径） | `../linux/memory.md` |
| AAS 每核比、CPU/等待方向（≥70%/40%） | **启发式经验阈值，非源码结论**——方向判定用，不作定论 | 本文第 2/12 节 |
| 命中率低+低物理读=伪问题；四点齐 | **启发式**（判读纪律） | `../bottleneck-patterns.md` |

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
