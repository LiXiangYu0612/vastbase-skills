# 瓶颈形态与取证链

## 概述

从症状到根因的判定与取证方法：先用「判定特征表」把现象归入十二类形态之一
（判定与排除都要记录，排除必须带数字依据），再走该形态的取证链取证定论，
最后按 SQL 九维定位主要责任语句。配合 `awr-report-analysis.md` 第 4 步使用；
无报告的口头排查也可直接从判定特征表入手。

核心纪律：**每条发现至少两个章节的数据互证**；只有单一章节证据的降级为观察项。
置信度定义：高 = ≥3 章节互证（含事件 + 堆栈或源码）；中 = 2 章节；低 = 单点观察。

**判别性证据（正常值同样是证据，2026-10-27 立）**：命中某形态后，取证链列出的
关联指标**无论异常与否都要取数登记**——正常值用来分叉排除相邻形态。典型：形态 4
里 WAL 同步等待高而底层写等待（并行写/设备 await）正常 ⇒ 存储不慢，问题在 commit
频次或并发结构；只看异常项会把"正常但关键"的判别指标漏掉，根因分叉就断了。
"什么正常"与"什么异常"共同定根因（方法参照 DBAIOps arXiv 2508.01136 的
implicitly-correlated metric 思想；分叉结论本身以各形态取证链既有规则为准）。

## 判定特征表

| 形态 | 判定特征（锚点章节） |
|---|---|
| P1 CPU 饱和（库内） | 每核 AAS>1 或 r>N【报告头+OS CPU per-min】；OS busy 高；SQL CPU Time 维霸榜【SQL Statistics】 |
| P2 库外进程抢 CPU | r>N + OS busy/User 高 + 库内 CPU 低【Load Profile 库侧 iostat vs OS disk 对账】 |
| P3 IO 读瓶颈 | DataFileRead 前排 + 命中率真低【Cache Hit + physical reads】+ 物理 SQL 榜 + iowait/b 四点齐；**await 按介质分档判**【Disk Rot 列】 |
| P4 IO 写 / WAL / 刷脏 | WALWrite/LogFileSync/XLogFlush 前排；WAL 速率【Load Profile】；w_await 高；pagewriter behind/dirty 涨【Pagewriter】区分刷脏跟不上 vs 存储慢 |
| P5 锁与并发竞争 | LOCK/LWLOCK 族 + waitchain【ASP】+ 长事务【Long Transaction】+ 前台栈【Callstack】+ logrpt 死锁 |
| P6 临时落盘 | temp bytes>0 / Temp Spill 榜（口径：不含聚合内排/HashAgg）+ fd pgsql_tmp>0【DB Process】 |
| P7 内存压力与上下文泄漏 | MemAvail 低/pswpout/水印【OS per-min】+ Shared Context Δ 涨【Database Memory】+ 会话大户【Max Session Memory】+ dmesg OOM + logrpt 53200 |
| P8 表膨胀 / 清理不及 | dead tuples 榜【Object Statistics】+ 长事务 + AV worker_cnt=0/全忙 + vacuum 参数 |
| P9 主备复制延迟 | lag 分解（Net/Fsync/Apply）【Replication】+ 槽 inactive + WAL 速率 + 备库 IO |

> **表外形态**：sys% 高（fork 税/IPI 广播）**不入 P 枚举**——四层分诊走
> `../troubleshooting/cpu-sys-high.md`（2026-11-04 立，判例 env-E）。形态闸
> 遇"sys 高"标签按此分流，勿硬套 P1~P12。
| P10 解析 / 计划开销 | Plan Time 维靠前且总占比>30%【SQL Statistics】+ Time Model parse/plan 阶段 |
| P11 连接风暴 | DB Process TCP 状态 time-wait/close-wait 激增 + ListenDrops【Protocol Errors】+ logrpt too many clients |
| P12 错误风暴 | logrpt 级别计数；PANIC 任一条最高优先（联动 `log-analysis.md`） |

## 强制过闸（全域执行纪律，2026-10-26 泛化）

**任何诊断入口**——报告分析（健康总览表/时间轴亮出的每个 ■/▲ 项）、口头症状
（"CPU 高""卡了""延迟大"）、现场救火——定位到具体异常点后，**必须在本表
12 形态逐项登记命中/排除**，排除必须带数字依据；登记表进工作底稿（不进读者
报告）。**每个异常项的最终归宿只有两种**：形态命中→走取证链出根因（机制层
"为什么"，挂形态→机制锚索引），或全排除→显式写"根因未定 + 缺什么数据
才能定"。禁止只报数字不下根因结论。诊断三问（是什么/为什么/怎么办）见
`../SKILL.md` 诊断主流程。

## 形态 → 机制锚索引（取证链定论后挂机制解释）

| 形态 | 机制文件（"为什么"层） |
|---|---|
| P1 | `internals/query-pipeline.md`（执行侧成本）+ `linux/cpu.md`（调度/运行队列） |
| P2 | `linux/cpu.md`（cgroup/调度竞争） |
| P3 | `internals/buffer-manager.md`（缓存命中机制）+ `linux/io-storage.md`（队列/介质） |
| P4 | `internals/flush-checkpoint.md` + `internals/commit-sync-events.md`（fsync 模式族）+ `linux/io-storage.md` |
| P5 | `internals/locks.md`（LWLock/行锁/死锁）+ `internals/mvcc-visibility.md` |
| P6 | `internals/operator-memory.md`（tuplesort/hash 配额与溢出代价） |
| P7 | `linux/memory.md`（水印/换页/OOM）+ `internals/operator-memory.md`（MemoryContext 53200） |
| P8 | `internals/autovacuum.md` + `internals/mvcc-visibility.md`（OldestXmin 钉住） |
| P9 | `ha/replication-lag.md`（三段分解）+ `internals/wal-replay.md` + `internals/replication-slots.md` |
| P10 | `internals/query-plan-cache.md`（14 级瀑布）+ `internals/query-pipeline.md` |
| P11 | `internals/connection-lifecycle.md`（postmaster 准入闸/CAC_TOOMANY/每连接一线程）+ `linux/network.md` |
| P12 | 按下方错误类细分表逐类挂钩 |

## P12 错误类 → 内核链路细分

| 错误类 | 内核链路（机制解释入口） |
|---|---|
| 认证失败（Invalid username/password） | `internals/connection-lifecycle.md`：ClientAuthentication（auth.cpp:350）失败即断链，**基线无认证失败延迟/锁定 GUC（负向实锤）**——应用侧重试即风暴，库侧无防线 |
| 唯一键冲突（duplicate key） | `internals/index-btree.md`：唯一索引插入路径检查；应用侧批量重试/无去重逻辑时放大 |
| 权限错误（permission denied / superuser） | `internals/error-fatal-mechanics.md`（报错家族）+ `performance/parameters.md`；多为监控账号配置缺口=监控盲区，优先修 |
| 会话超时被踢（Session unused timeout） | `internals/connection-lifecycle.md`（session_timeout 生命周期）+ `admin/g100-standard-params.md`（标准 30min 对照） |
| 连接被管理命令断开（terminating connection） | `internals/connection-lifecycle.md`；与管理员操作/超时策略关联取证 |
| PANIC / FATAL 宕库级 | `internals/error-fatal-mechanics.md`：PANIC 分派点（elog.cpp）→ 最高优先，一切分析让位 |

## 多源时间轴对齐

时段性结论要求五源至少两源同向（五源：OS 分钟序列 × ASP 5min 桶 × 日志切片 ×
后台堆栈 × 刷脏/长事务序列；digest 已自动拼好对齐表）。单源尖刺降为观察项。
日志切片口径：每个异常时段 P=[t0−10min, t0) 触发区、W=[t0, t1] 症状区，对比谁先堆积。

## 逐形态取证链

### 1. CPU 饱和（库内）

- 判定特征：DB CPU 高或 OS busy 高，r 队列持续>N
- 取证：OS r vs 逻辑核数 → Time Model CPU_TIME/DB_TIME（方向）→ SQL ordered by
  CPU Time → Callstack 前台采样热点 → Load Profile executions（频次型还是单次贵）
  → db_proc nvcswch（CPU 争抢实锤佐证，判读见 linux/cpu.md 切换节）
- 定论：r>核数 + CPU 榜 SQL 占比高 + CPU_TIME/DB_TIME 高 = SQL 拖 CPU
- 误判防范：CPU 高但 Iowait 也高 → 不是 CPU 不足，是 IO 慢拖高 sys（转形态 3）

### 2. 库外进程抢 CPU

- 判定特征：r>N + OS User 高，而库内 CPU_TIME 低、库侧 iostat 低
- 取证：Load Profile 库侧 read/write iops 与 OS disk 表对账（差额=库外读写）→
  DB Process 线程数/RSS（库自身在不在涨）→ dmesg 进程类
- 定论：OS busy 高 + 库内各项平稳 = 备份/ETL/杀毒扫描等库外负载抢 CPU，
  结论落在 OS 层并给出对账数字

### 3. IO 读瓶颈

- 判定特征：physical reads 速率高 / DataFileRead 靠前 / Iowait 高 / b 队列高
- 取证：**先看 Disk 表 Rot 定介质再取阈值** → Cache Hit（命中是否真低）→
  SQL ordered by Physical Reads → Object Reads 榜（打谁）→ OS MemAvailable
  （是否换页挤压缓存）
- 定论：四点齐（低命中+IO 等待+物理读 SQL+iowait）= 真读瓶颈；低命中+物理读低 =
  分母小伪问题；物理读高+命中也高 = 大冷数据扫描
- 建议配套：命中低→shared_buffers/预热；扫描型 SQL→执行计划与 Temp Spill

### 4. IO 写 / WAL / 刷脏

- 判定特征：WALWrite / LogFileSync / XLogFlush 靠前；w_await 高或周期性尖峰
- 取证：Load Profile commits/s（频次）+ WAL written 速率（体积）→ Pagewriter
  remain_dirty 与 behind xlog_insert（刷脏追没追上）→ OS write 占用与 Iowait →
  Replication 延迟（同步提交被备库拖？）→ 后台堆栈（WalWriter/CheckPointer 活动栈）
- 定论：commits 高+WAL 小 → 小事务高频，考虑批量/异步提交；WAL MB/s 级 → 写入风暴，
  看写入型 SQL 榜；dirty+behind 同涨 → 刷脏能力不足（查 checkpoint 族参数）；
  dirty 稳+w_await 高 → 存储慢；同步复制+备库 replay lag 高 → 主库 commit 被 hold，
  根因在备库
- 误判防范：w_await 周期性尖峰对齐 pagewriter 波峰 = checkpoint 抖动，不是持续慢盘

### 5. 锁与并发竞争

- 判定特征：LWLOCK 族 / LockRelation / transactionid 等待靠前，或 ctxt/s 异常高
- 取证（先分型：重量级 LOCK vs 轻量级 LWLOCK，两者现场不同）：
  - **重量级（行锁/表锁/事务锁）**：ASP waitchain（root_block_sessid 直给持锁者与
    受害 SQL）→ Long Transaction Monitor（谁持锁久）→ SQL 榜同表 DML → Object
    Activity 热点对象 → logrpt 死锁/锁超时
  - **轻量级（LWLOCK）**：ASP 无阻塞关系（waitchain 不管用）→ Callstack 前台 LWLock
    栈（Wait Event+PID+SQL+Stack）→ se/源码函数核对争什么资源、谁放大它
- 定论：长事务+热点对象+锁等待三合 = 业务长事务持锁（重量级）；无长事务但轻锁高 =
  内核级热点（如 BufferContent=热点块，配对象榜定位热块），根因到源码层
- 建议次序：改业务持锁逻辑在前，lockwait_timeout 类参数只是止血

### 6. 临时落盘（排序/哈希溢出）

- 判定特征：temp bytes>0 或 Temp Spill 榜非空
- 取证：该 SQL 是否同时高 Logical Reads（扫描大）→ Memory GUCs（work_mem +
  operatorMemKB 估算）→ Session Memory Summary + Max Session Memory → OS 换页
- 定论：Temp Spill 的原因要分清，**不能一律归"work_mem 小"**（机制见
  `internals/operator-memory.md`）：①配额不足（算子配额 < 工作集）；②全局水位
  （gs_sysmemory_busy 时未超配额也提前落盘）；③数据倾斜（大分组排序/哈希集 >
  任何合理配额，调参无效只能改写）。★对带估算配额（operatorMemKB>0）的算子，
  **调 work_mem 可能不生效**（估算优先）。**调大 work_mem 是下策**：先改 SQL/
  计划消扫描、消倾斜；必须调就按算子级核算——配额单位=计划节点（算子），安全边界
  = 并发查询数×每查询算子数×配额，以 operatorMaxMem（弹性上限）而非 work_mem 为准
  （以 Max Session Memory 实测单查询内存为锚反推算子数）

### 7. 内存压力与上下文泄漏

- 判定特征：MemAvailable 低 / 换页活跃 / Session Memory 大户
- 取证：Memory GUCs（max_process_memory 与机器配比）→ Shared Context Δ 增长
  （点名上下文）→ 并发查询×每查询算子数×配额（operatorMaxMem 为准）→ logrpt
  53200/OOM → dmesg Memory-OOM 类
- 定论：MemAvail 低+换页+共享上下文持续增长不回落 = 上下文泄漏候选（列具体
  上下文名）；各池之和超物理内存 = 配置问题，给量化（合计 vs 物理内存）

### 8. 表膨胀 / 清理不及

- 判定特征：Object Statistics dead tuples 榜高
- 取证：Long Transaction（卡 vacuum 的元凶，看最老事务与 OldestXmin）→
  Callstack AutoVacuum Workers（worker_cnt=0=真空没跑；全忙=吞吐不足）→
  logrpt autovacuum 跳过/防抖 warning → vacuum 参数族
- 定论：dead 高+长事务在榜 = 长事务卡清理；dead 高+workers 忙 = 清理吞吐不足

### 9. 主备复制延迟

- 判定特征：Replication lag 数字大
- 取证：WAL 速率（源头是不是写风暴）→ lag 分解：Net 大=网络/备端写慢，
  Fsync 大=备端刷盘慢，Apply 大=备库回放慢 → TCP Retrans → 复制槽状态
  （inactive=断连积压，主库磁盘高危，单独立条）
- 定论：按分解定位到网络/备端 IO/备端回放，复制架构问题转 ha/ 分类

### 10. 解析 / 计划开销

- 判定特征：Plan Time 维靠前，总占比>30% 才值得立论
- 取证：Time Model parse/plan 阶段耗时 → executions 频次（高频+贵计划=绑定变量缺失）→
  logrpt 计划缓存失效迹象
- 定论与建议：参数化/prepared 语句改造

### 11. 连接风暴

- 判定特征：TCP time-wait 海量 / close-wait 堆积【DB Process】+ ListenDrops>0
- 取证：Load Profile 会话/连接指标 + TCP CurrEstab(B/E) → logrpt too many clients /
  认证失败族 → somaxconn/tcp_max_syn_backlog 当前值【OS Kernel Parameters】
- 定论：短连接风暴（time-wait）还是应用不关连接（close-wait）还是 accept 队列溢出，
  三者处置不同（连接池/应用修复/内核参数）

### 12. 错误风暴

- 判定特征：logrpt 级别计数异常；PANIC 任一条最高优先
- 取证与定论：按 `log-analysis.md` 的错误族关联表找 AWR 侧影子；错误地位 =
  佐证与时刻锚，不孤证定论；同码错误 >10 次/秒 = 风暴，先报风暴再查根因

## SQL 定位（九维）

1. 按形态选维度：CPU Time→CPU / IO Time+Physical Reads→IO / Temp Spill→落盘 /
   Memory Used→内存 / Plan Time→计划 / Executions→频次 / DB Time→总账
2. 与 ASP 桶 query 榜对齐 unique_query_id（时段内谁在跑）
3. 跨维矩阵定责：同一 SQL ≥2 维霸榜 = 主要责任；单维 = 受害/局部；
   维度全 0 = 采集缺口，标不可用
4. 频次型（高 Exec×中 per-exec，量大成灾）与单次贵（低 Exec×高 per-exec，
   计划差/倾斜）分开表述，处置路径不同
5. 对象侧【Object Statistics】：热表/死索引/seq_scan 激增与 SQL 榜 join

## 通用纪律

1. 发现按严重度排序；每条带置信度（见概述定义）
2. 数据不支持的结论不写，宁缺毋滥；窗短（Elapsed<60s）在报告声明
3. 建议落到具体动作（参数/SQL/索引/流程），拒绝"建议优化"空话
4. 参数类建议对照 `parameters.md` 落成 参数名=当前值→建议值+理由

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 单章节证据直接立论 | 采样偏差/记账失真风险 | 两章节互证，否则降观察项 |
| 排除的形态不留痕 | 下次复查重复劳动，报告缺完整性 | 排除也记录并带数字依据 |
| IO 高低不先定介质 | SSD/HDD 阈值差 4 倍 | 先看 Rot 列 |
| 把受害 SQL 当责任 SQL | 等待者往往不是肇事者 | waitchain 定持锁者，跨维矩阵定责 |
| w_await 尖峰直接判慢盘 | 可能是 checkpoint 周期性抖动 | 与 pagewriter 波峰对齐后再判 |
| 复制延迟只看 Total | 掩盖真实环节 | Net/Fsync/Apply 分解定位 |

## 版本注记

- 形态判定基于 vb_tool 2026-10 版章节结构；章节增删时锚点同步更新
- 内核机制结论基于 openGauss 6.0.0 源码基线（G100 V3 档）

## 来源

- vb_tool AWR 采集/渲染源码精读（2026-10-24）
- 内核源码基线 `<openGauss-6.0.0-src>/`
- 实测样本：2026-10-21 成对报告（`../evals/fixtures/`）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
