---
name: vastbase
description: VastBase（G100）数据库技能域。覆盖：AWR 报告（vb_tool awrrpt 产物）自动分析与诊断、等待事件语义与内核源码溯源、性能瓶颈形态判定与取证、监控指标口径与陷阱、GUC/sysctl 参数对照、analyze_log 错误日志关联。当用户提供 awrrpt/awr html/awr tar 要求分析、诊断、看看有什么问题；遇到 CPU 高、IO 慢、锁等待、内存压力、表膨胀、主备延迟、连接风暴等性能问题；询问某等待事件什么意思、某参数管什么；或需要解读 vb_tool AWR 系列产物时，使用本技能域。SQL 优化（语句级/计划判读/SQLHC 分析）走本域 sql/ 分类；页/Tuple 二进制解析用 vastbase-parse-page-tuple。
---

# VastBase 技能域

面向 VastBase（G100）的运维诊断知识库。知识按主题文件组织，每个文件只讲一个主题、
可独立引用；所有结论性内容在文末「来源」中可追溯（vb_tool 源码精读、内核源码基线、
实测样本），推断性内容显式标注。版本适用范围见各文件「版本注记」。

## 使用方法（持续学习循环）

1. **遇到问题，先查本域知识库**（下方分类路由）：命中即按该文件判读，不重复读源码
2. **知识库未覆盖的未知问题**：读内核源码定论（基线路径与阅读范围约定见
   `SKILL_AUTHORING_GUIDE.md`），然后把结论固化进对应分类或 `internals/`
   （file:line + 版本基线 + 精读日期）并更新路由表——**知识库靠每次实战扩充，
   这就是本域的持续学习方式**。**禁止自行上网检索文章/博客当知识来源**
   （网络参照仅限用户明确提供的清单，见收录标准第 0 条）
3. **主动补网，不等点名**：空闲推进 `ROADMAP.md` 里的高优先级空白项
   （账本状态：✅深 / ⚓锚点 / ◻空白 / ⛔禁入），补完更新账本
4. **跨文件推理走知识网**：`KNOWLEDGE-GRAPH.md` 是关系层（节点 · 带语义的边 ·
   十条"从问题进网"导航路径）——机制互证与分诊优先顺着边走
5. 只读当前任务需要的文件，不要全量加载；多步骤任务按「常用多步流程」组合阅读

## 诊断主流程（所有诊断类问题统一入口，2026-10-26 立）

任何诊断类问题（报告分析 / 口头症状 / 现场救火）**先过下面的分诊表**，不允许
跳过分诊直接凭经验下结论：

| 输入 / 症状 | 走哪 |
|---|---|
| 有 awrrpt / logrpt / awr tar 产物 | `performance/awr-report-analysis.md` 六步（digest → 健康总览 → 异常项过 12 形态闸） |
| CPU 高 / 系统慢 | 形态 P1/P2 → `performance/bottleneck-patterns.md` + `linux/cpu.md` |
| sys 高（内核态）/ 周期性 sys 尖峰 / IPI 广播 | 四层分诊 → `troubleshooting/cpu-sys-high.md` + `linux/cpu.md`（判例 `cases/case-env-E-sys-spike-archiver-fork-202611.md`） |
| IO 慢 / 读写延迟高 | 形态 P3/P4 → 同上 + `linux/io-storage.md` |
| 卡住 / 锁等待 / 死锁 | 形态 P5 → `bottleneck-patterns.md` + `troubleshooting/blocking-live.md` |
| 表膨胀 / 清理不及 | 形态 P8 → `internals/autovacuum.md` |
| 主备延迟 / 同步等待 | 形态 P9 → `ha/replication-lag.md` |
| 连不上 / 被踢 / too many clients | 形态 P11 → `troubleshooting/connection-refused.md` + `internals/connection-lifecycle.md` |
| 实例起不来 | `troubleshooting/startup-failure.md` |
| 宕库 / PANIC / core | `troubleshooting/crash-panic.md` + `internals/error-fatal-mechanics.md`（一切让位） |
| 日志里的 BACKTRACELOG / 调用栈要解读 | `troubleshooting/stack-reading.md`（机制：`internals/error-fatal-mechanics.md` BACKTRACELOG 节） |
| 崩溃 coredump / 宕机（先对已知缺陷） | `troubleshooting/crash-panic.md`（现场保全）+ `troubleshooting/crash-case-index.md`（栈指纹对 55 条已知缺陷族） |
| Page LSN 报错（lsn check error / bigger than want set LSN） | `troubleshooting/page-lsn-mismatch.md`（双链回溯分叉定责） |
| 内存耗尽 / OOM / 53200 | 形态 P7 → `troubleshooting/resource-exhaustion.md` + `linux/memory.md` |
| 单条 SQL 慢 / 计划差 | `sql/sqlhc-analysis.md` |
| 某等待事件 / 参数什么意思 | 知识查询（不过闸）：`wait-events.md` / `parameters.md` / `internals/` |

**全域根因闭环纪律（诊断三问，缺一不可）**：
1. **是什么**——归入 12 形态（P1~P12）或救火件之一；排除也要留痕带数字依据
2. **为什么**——挂机制锚（`bottleneck-patterns.md` 形态→机制锚索引表），机制层解释成因；
   知识库没有的读源码定论后回灌（持续学习循环第 2 条）
3. **怎么办**——处置/参数/SQL 改造，可执行且说明为什么

三问答不齐时：**要么继续取证，要么显式写"根因未定 + 缺什么数据才能定"**。
禁止只报数字不下根因结论。每条结论至少两个数据源互证（详见 `bottleneck-patterns.md`）。

## 目录结构

```text
vastbase/
├── SKILL.md                  本路由
├── README.md                 域说明（给人看）
├── SKILL_AUTHORING_GUIDE.md  收录与写作标准
├── performance/              【已有内容】性能：AWR 诊断及其知识组件
│   ├── awr-report-analysis.md
│   ├── awr-report-reference.md
│   ├── metrics.md
│   ├── wait-events.md
│   ├── bottleneck-patterns.md
│   ├── parameters.md
│   ├── log-analysis.md
│   ├── diagnostic-map.md
│   ├── scripts/extract.py
│   └── report-template.html
├── troubleshooting/          【已有内容】现场救火五件套（清单见路由表）
├── ha/                       【已有内容】高可用（lag / 架构 / 切换 / 脑裂 四件，初版源码先行）
├── backup-recovery/          【已有内容】备份恢复（pitr-mechanism 初版·源码先行）
├── sql/                      【已有内容】SQL 优化（sqlhc-analysis / optimizer-deep 等 6 件 + references 深潜系列）
├── admin/                    【规划中】日常运维
├── migrations/               【规划中】Oracle → VastBase 迁移
├── tools/                    【规划中】工具使用：vb_tool / vbdul
├── internals/                【已有内容】内核机制（源码精读，服务各分类；清单见路由表）
├── linux/                    【已有内容】Linux 层（cpu / memory / io-storage / network / file-descriptors / tracing-tools，内核源码定论）
├── KNOWLEDGE-GRAPH.md        知识网关系层（节点 · 边 · 导航路径）
├── ROADMAP.md                覆盖账本（✅深/⚓锚点/◻空白/⛔禁入）
├── scripts/                  域级脚本（check_graph.py 知识网校验）
├── cases/                    【已有内容】案例库（判例层：INDEX 目录 + 案例文件）
├── workspace/                运行时工作区（与知识分离）
│   ├── inbox/                输入投放点
│   └── src/                  内核源码根
└── evals/                    测试样本与用例
```

规划中的分类只有目录与计划清单（各分类 README），没有知识内容；问题落在这些域时
如实说明知识未收录，不硬答。内容落地后才加进路由表与 description。

## 分类路由

| 主题 | 文件 | 何时读 |
|---|---|---|
| AWR 报告诊断全流程 | `performance/awr-report-analysis.md` | 拿到 awrrpt 报告要产出诊断报告 |
| ★诊断导图（症状→章节） | `performance/diagnostic-map.md` | 有什么问题该看哪个章节、证据链怎么走、三件套定论、不过度诊断 |
| ★检查项总账（awrrpt×logrpt 能诊断什么+固定检查逻辑） | `performance/check-inventory.md` | 两报告能力总账/覆盖边界/44 项机械闸全量镜像（阈值与 overview_table.py RULES 同步）、人工项清单 |
| awrrpt 17 章节逐节判读 | `performance/awr-report-reference.md` | 不确定某章节/某列什么意思；某现象该看哪节 |
| 指标口径与陷阱 | `performance/metrics.md` | 判读涉及占比、速率、时间模型的数字 |
| AWR 记账原理 | `performance/awr-principles.md` | 三本账模型、DB Time 边界、事件账本三类污染与自旋盲区——数字为什么能当证据 |
| 等待事件与源码溯源 | `performance/wait-events.md` | 事件定性、查内核机制、堆栈判读 |
| 瓶颈形态与取证链 | `performance/bottleneck-patterns.md` | 从症状到根因的取证路径 |
| 参数对照 | `performance/parameters.md` | 参数类结论与建议（GUC/sysctl） |
| 错误日志关联 | `performance/log-analysis.md` | logrpt 五步判读（摘要归一/族分诊三归责/时间轴/同分钟横切/慢SQL对账）与 AWR 联动 |
| 算子内存机制（work_mem/spill/弹性） | `internals/operator-memory.md` | 判读内存类参数与落盘现象；评估调参安全性与有效性 |
| B-Tree 索引机制（分裂/dedup/页回收/HOT/膨胀判定） | `internals/index-btree.md` | 索引膨胀分诊、索引写放大（HOT 条件被破坏）、递增键形态 |
| 执行器算子模型（NestLoop/MergeJoin/Agg/WindowAgg/Material） | `internals/executor-operators.md` | sort/hash_time 归因、NL 重扫形态、聚合策略、配额接线（调 work_mem 不生效的机制出口） |
| SQLHC 深度分析流程 | `sql/sqlhc-analysis.md` | 拿到 SQLHC 输出做语句级诊断：P0~P3 优先级、源码级 Cost 复算、报告模板 |
| SQL 优化深潜总纲 | `sql/optimizer-deep.md` | 代价复算/连接顺序 GEQO/LIMIT 缩放/Function×SMP/隐式转换/Oracle 迁移优化（references 深潜系列索引） |
| ★OPT_JOIN 代价日志判读 | `sql/references/optjoin-cost-trace.md` | 拿到 pglog 的 OPT/OPT_JOIN debug + 统计信息要复算逐 path sel/rows/cost 与选择过程：行格式→源码计算点对照表、公式代入实测样本（137 行全对账）、add_path/precheck 支配规则、整理五步 SOP |
| SQLHC 配套与速查 | `sql/sqlhc-integration.md` · `sql/sqlhc-quickref.md` · `sql/optimizer-usage.md` · `sql/quick-reference.md` | SQLHC 集成细节、速查表、使用指引（并入件的配套四件） |
| 核心等待事件源码判读 | `internals/core-events-deep-dive.md` | Top Event 定性：触发路径、正常形态、异常判据（六事件） |
| 缓冲管理器全流程 | `internals/buffer-manager.md` | BufferAlloc 读取/淘汰/脏页自刷；BufStrategyGet 淘汰压力信号；前台双写因果链 |
| ERROR/FATAL 家族机理与排查 | `internals/error-fatal-mechanics.md` | 读 logrpt：每族错误的抛出点、触发条件、排查路径 |
| 内核数据通路总图（枢纽） | `internals/kernel-data-path.md` | 判读定位：任何现象/事件/报错先在通路上找位置，再沿链接下钻 |
| 提交同步事件（wait wal/trans sync） | `internals/commit-sync-events.md` | ASP 桶 wait_wal_sync/wait_trans_sync 判读；**必配 Replication lag 分解**；两套等待通道架构 |
| 锁与轻锁机制 | `internals/locks.md` | LWLOCK/LOCK 事件共性底座：自旋/等待队列、释放不交接、软死锁重排 |
| SysV 信号量机制（kernel.sem） | `internals/sysv-semaphore-usage.md` | kernel.sem 是限额非成本（配大无害配小出事）；sys% 高时排除 semtimedop 嫌疑；启动 FATAL 信号量耗尽 |
| logging_module 与 debug 系 GUC | `internals/debug-logging-gucs.md` | 要开内核级 debug 日志/优化器路径回放；**双闸配方（必须配 log_min_messages）**；死旋钮清单（trace_locks 族 release 未注册等）——开参数前先查此文件防白耗时间 |
| 查询重写开关位表 | `internals/query-rewrite.md` | rewrite_rule/sql_beta_feature 位掩码全表；计划突变第一排查位 |
| PBE 计划缓存判定链 | `internals/query-plan-cache.md` | custom↔generic 14 级瀑布；"第 6 次换计划"机制与 pbe_optimization 地雷本体（集中式=无条件强制 generic）；plan_cache_type_validation（结果类型校验，≠plan_cache_mode）与 "cached plan must not change result type"；PBE 10 参数全景；C/G 观测配方 client_min_messages=debug2 |
| 自适应计划选择（gplanmgr） | `internals/adaptive-plan-mgr.md` | PBE 唯一学习型算法：多候选 gplan+选择率置信区间匹配+淘汰赛；Choose_adaptive_gplan hint 启用；与 GPC 互斥；倾斜数据对策 |
| Opfusion 点查快速路径 | `internals/opfusion.md` | enable_opfusion 族四参数+opfusion_debug_mode；EXPLAIN `[Bypass]` 观测配方；GPC env signature 耦合 |
| 刷脏 behind 语义 | `internals/flush-checkpoint.md` | Pagewriter behind 公式与三形态判读；checkpoint 生命周期（增量/全量/三道闸/触发面） |
| 统计信息→选择率 | `internals/stats-to-cost.md` | MCV/均衡直方图/外推统计判定链；rows 偏差分诊表；default_statistics_target 双模式（正值论文公式/负值百分比+扩展统计/负值关停 autoanalyze） |
| MVCC 可见性（CSN 快照） | `internals/mvcc-visibility.md` | 可见性核心公式与判定链；长事务卡 vacuum 的机制根源 |
| gs_stack 抓栈机制与 AWR Callstack 段边界 | `internals/gs-stack-sampling.md` | 报告堆栈列 "-" / "not available" 判读矩阵；抓栈 5ms SIGURG 窗口、权限硬闸、语句级失败定位配方 |
| fd 三层闸与 VFD 预算 | `internals/fd-vfd-budget.md` | "Too many open files"/fd 抖动分诊；max_files_per_process 生效链（postmaster 换算→线程创建快照继承到全部线程）；EMFILE=prlimit 层、VFD 抖动=GUC 层 |
| Heap 元组取属性语义 | `internals/heap-tuple-attr.md` | 老行新列默认值/已删列 NULL 的机制；元组与 tupdesc 长度不一致的读取规则；解析工具同构义务 |
| autovacuum 机制 | `internals/autovacuum.md` | 触发阈值公式/让路 cancel/mode 门控；★default_statistics_target≤0=autoanalyze 全库关停；膨胀判读 |
| 复制槽机制 | `internals/replication-slots.md` | restart_lsn=WAL 下界/逻辑槽 xmin/反馈忽略窗 |
| WAL 回放与备库 | `internals/wal-replay.md` | apply 循环/并行回放与极致 RTO（依赖封装 redo_item.h/IOS 取舍）/Apply lag 三来源 |
| 备库回放冲突与 hot standby 快照 | `internals/recovery-conflict.md` | 冲突五分类/snapshot 深链（Vacuum/Freeze+64bit XID 页级冻结）/禁快照连锁事故（备库连不上+主库 COMMIT 卡死）/参数控制面 |
| 双写机制（Double Write） | `internals/double-write.md` | 半写防护/DW 文件组织与版本号/检查点回收边界/崩溃恢复/PR 6671 truncate 竞态 |
| MergeJoin 估价风险 | `internals/mergejoin-cost-estimation-risk.md` | 估价阶段 NOW 快照扫索引抢 ProcArrayLock；Plant Time 独大/整体卡死两形态；关 mergejoin 止损与版本演进 |
| 查询处理管线骨架 | `internals/query-pipeline.md` | parse→rewrite→plan→exec 总览与各段锚点；词法/语法深读（scan.l/gram.y/移进规约冲突排查） |
| G100 标准化参数基线 | `admin/g100-standard-params.md` | 交付核对与诊断对照：3.0.8/3.0.9 标准值、54 项高危、参数地雷清单 |
| 版本谱系与官方文档站地图 | `admin/version-map.md` | LTS/DMR 版本对表、docs 站 API 抓取配方、已知问题无公开源的负向结论 |
| ★OS 四域诊断树（跨层联判入口） | `linux/os-triage.md` | 任何"慢/卡/超时"的第一站：30 秒基线五列（us/sy/id/wa/st）→ A~G 支分诊；CPU↔Network 五通路；组合拳指纹表；util 禁令/await 分档/累计口径铁律都在这入口 |
| Linux CPU 层（负载/软中断/切换/iowait 口径） | `linux/cpu.md` | 判读 OS 段 CPU 列：load 含 D 状态、iowait 双刃、ksoftirqd、nvcswch |
| Linux 内存层（水位/kswapd/swappiness/OOM 全链路） | `linux/memory.md` | pswpout、MemAvailable、OOM kill 现场；与 53200 两层闸门分层 |
| Linux IO 存储层（回写四闸门/块层/iostat 口径） | `linux/io-storage.md` | iostat %util 陷阱、vm.dirty_* 与库刷脏互踩、fsync 设备侧对账 |
| Linux 网络层（收包路径/两级连接队列/假死连接） | `linux/network.md` | 连接拒绝/慢、ListenOverflows、TIME_WAIT、假死持锁会话、CLOSE_WAIT 堆积（=忙/卡会话，无 sysctl 可清） |
| Linux 文件描述符层（fdtable/rlimit/proc 渲染） | `linux/file-descriptors.md` | fd 分类树判读：pgsql_tmp 活跃落盘、version.cfg 泄漏、fd 逼近 rlimit |
| Linux 动态追踪工具链（ftrace/perf/eBPF/strace 取舍） | `linux/tracing-tools.md` | 要定位到函数/路径级：on/off-CPU、IO 延迟、syscall，含生产开销取舍 |
| 锁等待/阻塞现场 | `troubleshooting/blocking-live.md` | 业务挂起、会话堆积、疑似死锁、假死持锁：保现场→分诊→止损 |
| 连接类故障 | `troubleshooting/connection-refused.md` | 连接拒绝/超时/连上就断/too many clients：四道闸从外向里 |
| 连接生命周期内核侧（postmaster/准入闸/认证/会话初始化） | `internals/connection-lifecycle.md` | 连接慢的库内机制：CAC_TOOMANY/OOM 拒绝、每连接一线程、启动包与认证成本；compat_oracle_txn_control 只读 SELECT 事务（ODBC 默认关=idle in txn 被杀） |
| 主备延迟诊断（lag 三段分解） | `ha/replication-lag.md` | Replication lag 偏大：Net/Fsync/Apply 分段定位与处置矩阵（初版） |
| 流复制架构与 LSN 位点 | `ha/replication-architecture.md` | walsender/walreceiver 双流、四位点语义、模式状态机、同步语义表（synchronous_commit×standby_names）/级联/延迟回放/build 约束/vb_ctl query 状态机 |
| HAS+DCS 高可用组件 | `ha/has-dcs-architecture.md` | 另一套 HA 栈：四模块决策树/DCS 键值/选举参数与最健康节点判定/pause 维护态/启停顺序/DCS 故障两形态 |
| CM 集群人工处理清单 | `ha/cm-cluster-manual-ops.md` | cm 自动机制管不了的 6 类 13 场景：备 build/主 failover/脑裂/CM 元数据重建/CN/同步停备 hang |
| 读写分离（Hydra-JDBC） | `ha/read-write-splitting.md` | 驱动层读写路由规则/负载均衡/主备切换感知/备库冲突报错转发主库 |
| 失败判定与切换 | `ha/failover.md` | promote 护栏文案、term 任期、RTO 构成、旧主 rewind（初版·源码先行） |
| 脑裂成因与防护 | `ha/split-brain.md` | 双主三来源、term 护栏、most_available_sync 降级窗口的数据代价、处置原则 |
| PITR 机制 | `backup-recovery/pitr-mechanism.md` | 归档拉取/恢复目标/回放同源/恢复决策（初版·源码先行） |
| 案例库（判例层） | `cases/INDEX.md` | 找"上次遇到这样解的"：症状关键词→根因→关联知识文件（真实案例+脱敏铁律） |
| 实例启动失败 | `troubleshooting/startup-failure.md` | 起不来：锁文件残留/端口/IPC/磁盘/权限/损坏/license 按序排查 |
| 崩溃与 PANIC 现场 | `troubleshooting/crash-panic.md` | 主进程退出/反复重启/PANIC：三件套保全→分诊→crash loop 处置 |
| Page LSN 类报错 | `troubleshooting/page-lsn-mismatch.md` | lsn check error / bigger than want set LSN：XLOG 解剖+双链回溯分叉定责（回放逻辑错 vs 写入逻辑错） |
| 已知崩溃案例索引 | `troubleshooting/crash-case-index.md` | 55 条内部缺陷库判例（栈指纹对号）；★含内网链接，公仓发布前整文件排除 |
| 堆栈/BACKTRACELOG 解读 | `troubleshooting/stack-reading.md` | 拿到日志堆栈/BACKTRACELOG/调用栈要读出谁在干什么、错在哪；帧速查与现场四步 |
| 资源耗尽应急 | `troubleshooting/resource-exhaustion.md` | fd/内存/信号量/端口四类耗尽的判据与应急 |

## 常用多步流程

| 任务 | 阅读顺序 |
|---|---|
| 诊断一份 AWR 报告 | `awr-report-analysis.md`（主流程）→ 按流程节点引用 `awr-report-reference.md`、`metrics.md`、`wait-events.md`、`bottleneck-patterns.md`、`parameters.md`、`log-analysis.md` |
| 只问某等待事件含义 | `wait-events.md` → 必要时查源码（workspace/src） |
| 只问某参数效果 | `parameters.md` |
| 无报告的口头性能排查 | `bottleneck-patterns.md` 判定特征表 → 建议采集 AWR 再走全流程 |

## 关联技能与工具

- `vastbase-parse-page-tuple` — Heap 页/Tuple 二进制解析
- `vastbase-sandbox` — 验证沙箱运行器（独立 skill）：知识结论真机实测的
  执行层；evals/知识文件引用沙箱名（env-A 等）
- `vb_tool se <event>`（或 `~/vb_tool/vb_tool se`）— 417 个等待事件离线知识库；
  `se -l <关键词>` 模糊列表。输出五项：Type / Meaning / Triggers（源码 file:line）/ Why / Fix

## 版本基线

- 内核源码基线：openGauss 6.0.0 树（对应 VastBase G100 V3 档），位于
  `<openGauss-6.0.0-src>/`；
  源码级结论均标注该基线，VastBase 定制分支可能有差异
- CM（集群管理）侧近似基线：开源 openGauss CM 源码，已 clone 至
  本地克隆（2026-10-25，depth1 master；上游
  https://gitee.com/opengauss/CM ，选主依赖 DCC+DCF）——**VastBase 内核与 VastBase CM
  源码不可得**（2026-10-25 用户校准），ha/ 结论以开源 OG 内核+CM 原理近似，
  差异以实测对账
- 官方文档：https://docs.vastdata.com.cn/zh_CN（重点：G100 全版本 + 高可用全版本）——
  按需查阅，分层规则见 `SKILL_AUTHORING_GUIDE.md`（机制类源码定论、文档交叉验证，
  不一致以源码为准并记录差异；流程类文档为主）
- se 知识库：417 事件（LWLOCK 244 / IO 80 / STATUS 76 / LOCK 17）
- AWR 产物：vb_tool `awr awrrpt`（17 章节版，v2.5.20 窗口=OS 章 Cgroup/NUMA 深补/
  Softirq-IRQ per-CPU 矩阵/TCP Statistics 新子节齐备，2026-11-01 对账）；`awr awrrpt`
  默认连跑 analyze_log_v2 并打包为 `awr_<b><e>.tar`（内含 awrrpt + logrpt 两份 html）

## 来源

- vb_tool AWR 采集/渲染源码精读（41954 行，2026-10-24）
- 内核源码基线 `<openGauss-6.0.0-src>/`
- 实测样本：2026-10-21 成对报告（`evals/fixtures/`）
