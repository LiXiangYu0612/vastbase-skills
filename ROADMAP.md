# 知识网覆盖度账本（v1 完整版，2026-10-25）

> v1 收口标准：**◻ 空白清零**——每项要么 ✅深（函数体读完）要么 ⚓锚点（触发点/
> 结构实锤 + 精确线索）。⚓ 项遇到实战问题按循环升级为 ✅。状态变更随手更新本表。
> ⛔禁入：cstore、ustore、段页式、nvm、线程池。

## 存储与执行层

| 主题 | 状态 | 文件 |
|---|---|---|
| BufferAlloc 全流程 | ✅深 | buffer-manager.md |
| 缓冲哈希表内部 | ⚓ buf_table.cpp:86（dynahash 共享表） | buffer-manager.md |
| clock sweep 后半 / pin-refcount | ✅深（ClockSweepTick 原子推进/候选链 usage 老化/备库 fraction/全池无页升级路径；候选链双计数器无视图出口=负向实锤） | buffer-manager.md |
| 行锁协议 / LWLock / 死锁（软硬边递归） | ✅深 | core-events §4、locks.md |
| WAL 组插入 / XLogWrite 群提交 / 提交同步 | ✅深 | locks.md、commit-sync-events.md |
| WAL fsync 各模式 | ✅深（get_sync_bit/O_DIRECT 条件/显式 fsync 分派/例外路径） | commit-sync-events.md |
| checkpoint 生命周期 / pagewriter behind | ✅深 | flush-checkpoint.md |
| 双写 | ✅深 | core-events §5 |
| 算子内存 / tuplesort | ✅深 | operator-memory.md |
| hash join nbatch 加倍 | ✅深（nodeHash.cpp:994-1140：倍增+全量重分布自搬运代价） | operator-memory.md |
| MemoryContext 记账 / 53200 拦截点 | ⚓ 两层语义✅；拦截函数未定位 | operator-memory.md §7 |
| MVCC 可见性（CSN） | ✅深 | mvcc-visibility.md |
| ProcArray 快照共享获取 | ✅深（procarray.cpp:2018-2400：共享锁/CSN/备库等回放/组获取/清理边界） | locks.md 深读注 |
| DataFileRead / BufFile | ✅（含 cfs 排除分支注） | core-events §2/§3 |

## 查询处理层

| 主题 | 状态 | 文件 |
|---|---|---|
| 管线骨架（parse→rewrite→plan→exec） | ✅入口/算子级（planner:380→standard_planner:530→make_one_rel allpaths:186；exec 段算子级=executor-operators；parse 段判读价值低不展开） | query-pipeline.md + executor-operators.md |
| 执行器算子（NL/MA/AGG/Window/Material） | ✅深（NESTLOOP_PARAM 执行本体/MJ 状态机/Agg 四策略+配额接线/Window 三向游标/Material tuplestore 配额；⚓=SMP 调度与向量化算子族） | executor-operators.md |
| rewrite_rule/sql_beta_feature 位表 | ✅表级；消费点：LAZY_AGG→planner.cpp、MAGIC_SET→subselect.cpp、PARTIAL_PUSH→pgxcplan_single.cpp；余位⚓ | query-rewrite.md |
| PBE 计划缓存 | ✅深（14 级瀑布；11-03 补：10 参数全景/集中式 pbe_optimization 无条件退化/batch bind 强制开/C/G 观测配方 client_min_messages=debug2/plan_cache_type_validation；自适应分岔另立专篇） | query-plan-cache.md |
| 自适应计划选择（gplanmgr/PMGR） | ✅深（11-03 新立：五步框架/CachedPlanInfo 结构/CI 学习/淘汰赛/启用七闸/Choose_adaptive_gplan hint；env-T 全生命周期实测） | adaptive-plan-mgr.md |
| 统计→选择率 | ✅深；采样：块=Algorithm S、行=蓄水池替换、MCV 阈值+等频直方图构建全深（2026-10-25 二次） | stats-to-cost.md |
| plan 生成（枚举/DP/geqo/参数化） | ⚓ 目录级（geqo 存在、参数化路径有 beta 位） | query-pipeline.md |
| opfusion / 向量化 / SMP | opfusion ✅深（11-03 成专篇：参数族/双入口/bypass 分型/观测配方；向量化与 SMP 仍 ⚓ 骨架级） | internals/opfusion.md + query-pipeline.md |
| 代价模型/SQLHC 分析/查询转换深潜 | ✅已并入（原独立技能 8 文件迁至 sql/：deep 56KB+sqlhc×3+usage+quickref+references 深潜系列；2026-10-26 用户令"完全并入删源"） | sql/optimizer-deep.md 等 6 件 |

## 运维域

| 主题 | 状态 | 文件 |
|---|---|---|
| 报错家族 / 取消家族 | ✅ | error-fatal-mechanics.md |
| BACKTRACELOG / 堆栈解读 | ✅（errfinish 闸门+GUC 默认值+force 标志死代码实证；relation_open 三路径判例入 stack-reading） | error-fatal-mechanics.md、troubleshooting/stack-reading.md |
| autovacuum（触发/让路/限流） | ✅深；cost 限流比例睡眠已深、表级跳过=权限族文案实锚；锁不可得跳过文案⚓ | autovacuum.md |
| 复制槽 | ✅深 | replication-slots.md |
| WAL 回放 / 备库 / 回放冲突 | ✅深（并行回放/极致 RTO 已并入：依赖封装 redo_item.h:42-43 实锚、IOS 取舍、极致 RTO 三约束） | wal-replay.md |
| 备库回放冲突与 hot standby 快照 | ✅深（2026-11-04 内部文档双篇合流+源码核实：五分类/Vacuum+Freeze 双链/64bit XID 页级冻结/禁快照双条件 procarray.cpp:1839 实锚/2210.5 连锁案例/CSN 修复提案） | internals/recovery-conflict.md |
| 双写机制（DW 文件/truncate/恢复） | ✅深（2026-11-04 内部文档+源码核实 double_write.cpp 全锚；PR 6671 truncate 竞态/single 恢复无版本校验取舍入档；⚓段页式区） | internals/double-write.md |
| MergeJoin 估价风险 | ✅深（NOW 快照扫索引+ProcArrayLock 争用两实战形态；309 快照改型版本演进；costsize/selfuncs 锚） | internals/mergejoin-cost-estimation-risk.md |
| 词法/语法解析（scan.l/gram.y） | ✅文档级（XS-DB-64 入库：移进规约/冲突排查配方 bison -v；OG 路径差异记录；深入判读价值有限维持骨架级） | internals/query-pipeline.md |
| 连接生命周期（postmaster/准入闸/认证/会话初始化） | ✅深（CAC_TOOMANY/OOM 拒绝文案、盐预生成、每连接一线程、启动包/SSL 重入；ClientAuthentication auth.cpp:350 结构+基线无认证超时 GUC 负向实锤；AuthenticationTimeout 别名是否存在仍开⚓；compat_oracle_txn_control 只读 SELECT 事务+ODBC 驱动默认差异【VastBase 定制 GUC 开源基线无】） | internals/connection-lifecycle.md |
| 主备延迟诊断（lag 三段分解） | ✅初版（操作层合成，机制底座=internals 三文件+linux 两文件） | ha/replication-lag.md |
| 流复制架构/切换/脑裂 | ✅初版源码先行（四位点/term 任期/most_available_sync 窗口/DCF 目录实锚；recovery_target 通道⚓；**VastBase 内核+CM 源码不可得→开源 OG 内核+CM 近似基线** gitee.com/opengauss/CM；同步语义表/级联/延迟回放/build 约束/vb_ctl query 已并入 replication-architecture） | ha/ 三件 |
| HAS+DCS 高可用组件 | ✅文档级（第11课课件入库：四模块/选举/pause/DCS 故障两形态；待实测对账⚓） | ha/has-dcs-architecture.md |
| CM 集群人工处理清单 | ✅操作级（13 场景手册，含工单依据；工单号发布脱敏） | ha/cm-cluster-manual-ops.md |
| 读写分离（Hydra-JDBC） | ✅文档级（XS-DB-102：转发规则/切换感知/冲突报错转发；标注内部资料——公仓发布需脱敏重写或排除⚓） | ha/read-write-splitting.md |
| Page LSN 报错分析法 | ✅方法论级（双链回溯分叉定责+hash 索引实战范式） | troubleshooting/page-lsn-mismatch.md |
| 已知崩溃案例索引 | ✅索引级（55 条判例速查；含内网 TAPD 链接——公仓发布前整文件排除⚓） | troubleshooting/crash-case-index.md |
| 内部资料批次入库（D:\log\1 + D:\二线团队） | ✅2026-11-04 完成（14 份原件入 workspace/inbox/dlog1_20261104；9 新文件+5 扩展；核心断言逐条源码核实，文档↔源码差异记录在案：gplan_is_fqs 前置/compat GUC 定制/解析器路径） | 本行+workspace/inbox |
| 标准化参数基线 + 自动对照 | ✅ | admin/g100-standard-params.md + extract.py |
| 现场救火五件套（阻塞/连接/启动/崩溃/资源） | ✅操作级（机制引用 internals/linux 不复制；pid 锁文件/端口绑定/PANIC 分派/max_files_per_process 均源码锚点） | troubleshooting/*.md |
| B-Tree 索引机制（分裂点/最右路径/dedup/两段式删页/HOT） | ✅深（findsplitloc 空间平衡+goodenough；HOT 判定函数名⚓；contrib 无 pgstattuple 负向实锤→密度法判读） | internals/index-btree.md |
| fd 三层闸与 VFD 预算 | ✅深（2026-10-30 master 树：rlimit/VFD/AllocateDesc 三层；★生效链=set_max_safe_fds 唯一调用点 PostmasterMain→**线程创建时 save/restore_backend_variables 快照继承到全部线程**（首版误判"恒 32"经用户复核推翻，32=骨架初值被覆盖）；SharedFileId 第二生效面；nfile 账本 LSC 线程私有/否则会话级；超限三命运；⚓=6.0.0 正式树行号对齐） | internals/fd-vfd-budget.md |
| Heap 元组取属性语义 | ✅深（2026-10-30 master 树：heap_slot_getattr 六分支=系统列/deform 缓存/超 tupdesc NULL/PGXC datarow/dropped NULL/短元组 attinitdefval 默认值；工具同构义务入篇；⚓=正式树行号对齐） | internals/heap-tuple-attr.md |
| checkpoint 生命周期 | ✅深（域重建时已深+今日二次：三道闸/倒退 PANIC/空闲跳过/触发面/KeepLogSeg redo 界；SKILL 路由陈旧"待补"标注已修） | internals/flush-checkpoint.md |
| 会话视图/备库口径（记忆遗产固化） | ✅固化（n_physical_read≡逻辑读/time 语句末刷/会话退出即清/sid join/备库 LAS 恒 0/gs_asp 冻结/statio 归属——多环境既往实测） | performance/metrics.md |
| 备份/PITR | ✅初版机制件（RestoreArchivedFile/时间线/回放同源；recovery_target GUC 通道⚓；策略/轮转/演练件待案例） | backup-recovery/pitr-mechanism.md |
| 案例库（MOS 式判例层） | ✅框架落地（cases/ 三件套：README 规范+INDEX 目录+首例 env-A-ENOSPC；check_graph 对 cases 特殊登记规则）；回填源=报告样本/历史战役/VastBase 文档已知问题/OG tracker | cases/ |
| 已知问题清单（已知 bug 对照） | ◻ 待补——**VastBase 官方文档为主**（docs.vastdata.com.cn 各版已知问题，2026-10-25 用户令：OG docs 不拉）；OG gitee tracker 仅近似参考：API 端点已验证可用但匿名限流（issues?state=all&per_page=100 分页+过滤 6.0，2026-10-25 全量拉取未成，配方在此） | — |

## OS 层（linux/，2026-10-25 立）

| 主题 | 状态 | 文件 |
|---|---|---|
| CPU 层（loadavg/softirq 预算/iowait 记账/切换/EEVDF/PSI） | ✅深（loadavg.c/softirq.c/core.c/stat.c 全核；EEVDF/PSI 锚点） | linux/cpu.md |
| ★四域诊断树（跨层联判入口） | ✅路由级（2026-11-03 立，st=70% 案沉淀：五列基线→A~G 支/CPU↔Network 五通路/组合拳指纹；只路由不复制，阈值全部引自四域文件） | linux/os-triage.md |
| 内存层（水位/kswapd/swappiness 比例模型/OOM 全链路/workingset/THP） | ✅深；THP 惯例注记、memcg swappiness=v1 专属实锚、vm.rst 未引 | linux/memory.md |
| IO 层（page-writeback 四闸门/readahead/iostat 口径/ext4 默认） | ✅深；blk-mq 目录级、wbt/iomap 锚点 | linux/io-storage.md |
| 网络层（收包预算/两级连接队列/syncookies/TIME_WAIT/retries2/conntrack/关闭链路/客户端消失三形态） | ✅深；keepalive 默认 7200s·9·75s 实锚、conntrack 文案实锚、reqsk 回调名实锚 | linux/network.md |
| 动态追踪（ftrace/perf/eBPF-BCC/火焰图） | ✅入口锚点级（perf_event_open/ptrace/tracefs/探针注册定位） | linux/tracing-tools.md |

## 欠账速查（⚓→✅ 的升级线索）

六笔主欠账 + 七笔边角锚点 2026-10-25 全部收口（含两个**负向实锤**：
候选链双计数器无视图出口、基线无认证超时 GUC）。仍开的小锚：锁不可得
跳过文案（autovacuum）· AuthenticationTimeout 是否以别名存在
（connection-lifecycle）· cgroup v2 swappiness 语义细节（linux/memory）

## 存量审计挂账（2026-11-05 全量 77 文件四路审计，P1 已修 19 项，遗留如下）

- [ ] 案例反向锚点：env-C（7 个机制文件无一回挂）、envD（wait-events）、
      env-odbc（connection-lifecycle）
- [ ] sql/references Optimizer 三篇恰 500 行截断（01 缺视图展开/表达式优化/
      常量折叠三节，06/09 代码块未闭合）+ 全篇无 file:line + 09 头部占位语；
      optimizer-deep 同查
- [ ] sqlhc-analysis "225s→0.45s" 案例出处缺；sql/ 六文件缺「来源」「版本注记」
- [ ] sqlhc-integration "3.0.9.1" 版本记法与 version-map 统一
- [ ] cases 命名统一（env-C/env-odbc 连字符风格对齐 env-A/env-E）
- [ ] cpu.md 补"判读速查"节标题；os-triage/file-descriptors 补「常见错误」；
      flush-checkpoint 版本注记/来源分立
- [ ] read-write-splitting.md:71 自标"公仓发布须脱敏重写或排除"——下次发布核对
- [ ] OOM B 型 cgroup v1/v2 口径按 vb_tool 实采定稿；env-E VmPTE 取数确认⚓
