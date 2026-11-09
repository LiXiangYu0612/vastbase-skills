# 参数对照（现象 → GUC/sysctl）

## 概述

参数类结论的对照手册：现象组 → 嫌疑参数 → 判什么 → 调整方向。当前值出处：
digest 参数段（关键子集）与 `<输入名>_params.txt`（全量；GUC 来自报告渲染时 live
查询的 pg_settings，sysctl 来自 OS Kernel Parameters 段的 Begin/End 两列）。

## 使用原则

**debug/trace 类参数不在本表调参范畴**——logging_module 双闸配方、debug 系
GUC 的死旋钮清单（trace_locks 族 release 未注册等）见
`../internals/debug-logging-gucs.md`，开任何 debug 参数前先过该文件防白耗时间。

0. 诊断时先对照标准化基线（`../admin/g100-standard-params.md`）：报告当前值
   偏离 3.0.8/3.0.9 标准值的参数本身就是配置类发现；涉及高危/地雷参数
   （enable_early_free、recovery_optimization_mask 等）必核版本与 PSU
1. 参数只在**现象与机理都指向它**时立论：先有证据链，再翻本清单——
   清单是查证工具不是开药清单
2. 每条参数建议写全三件：参数名 = 当前值 → 建议值 + 理由（联动哪个现象）
3. Context 列决定改法：postmaster=重启生效 / sighup=reload / superuser·user=会话级可改；
   postmaster 类建议必须附重启窗口提醒
4. 报告未含的参数不猜默认——写"报告未含，需实机确认"进数据局限
5. sysctl 段 Begin≠End = 窗内被人改过，单列证据
6. 参数组合比单项重要：work_mem×并发、各内存池之和 vs max_process_memory
7. **不稳定特性参数一律不进建议**（2026-10-25 用户令）：cstore_buffers、
   segment_buffers、线程池族（enable_thread_pool/thread_pool_attr）、ustore/段页式/nvm
   相关——不建议启用、不建议调整、不建议关闭；仅可作环境事实记录。
   内存配比核算时这些池按当前值计入总量即可

## 1 CPU 高 / SQL 慢

| 参数 | 判什么 | 方向 |
|---|---|---|
| work_mem | 排序/哈希**按算子（计划节点）**计的内存配额：同一查询内多个排序/哈希节点各占一份，单查询总量=配额×算子数（HashJoin 上下文 GB 级即实证）；Temp Spill 榜有 SQL 且偏小 → 落盘代价 | 调大是下策：先改 SQL/计划；安全核算用 **并发查询数×每查询算子数×配额，以 operatorMaxMem 弹性上限为准（work_mem 只是 operatorMemKB 估算的兜底，带估算配额时调 work_mem 可能不生效）**，以 Max Session Memory 实测单查询内存为锚；数据倾斜下大分组排序/哈希集超配额照旧落盘，调参救不了倾斜（机制细算见 `../internals/operator-memory.md`） |
| shared_buffers | 命中率低+物理读高 且 机器有余量 | 参照 max_process_memory 配比，盲目调大挤会话内存；★fork 税场景禁用"缩 shared_buffers"治病（负优化，见 linux/memory.md 页表节） |
| random_page_cost | SSD 盘却按默认 4.0 估值 → 优化器不愿走索引（seq_scan 激增佐证） | SSD 建议调低（1.1 左右）；**先看 Rot 列确认介质**；HDD 别乱降 |
| plan_cache_mode / behavior_compat_options | Plan Time 维霸榜、硬解析高 | 参数化/prepared 语句 |
| vb_enable_int_varchar_eq_int | 2.2.15 PSU8 起 `INT列='字符串字面量'` 等值比较**两侧都转 NUMERIC** → INT 索引失效、计划翻车（升级引发的隐性回归；PSU8 以前只转字面量为 INT；308 系列无此变更） | 2.2.15 **PSU11+ 默认开启**恢复旧行为；参数 PSU11 才引入，PSU8~PSU10 无参数可救只能改写 SQL（详见 `../sql/references/VastBase_Implicit_Type_Cast_Deep_Dive.md` 场景2B） |

## 2 IO 读慢

| 参数 | 判什么 | 方向 |
|---|---|---|
| shared_buffers + effective_cache_size | 命中率、优化器估值 | effective_cache_size 只影响计划不影响运行 |
| random_page_cost | 同上（介质联动） | 见组 1 |
| （OS）read_ahead_kb | 顺扫吞吐 | 顺扫主导时查；不在 sysctl 表则需 blockdev --getra 实机查 |

## 3 IO 写慢 / WAL / checkpoint

| 参数 | 判什么 | 方向 |
|---|---|---|
| wal_buffers | WAL 写等待高且值极小 | 16MB 起步 |
| checkpoint_timeout / checkpoint_segments / max_redo_log_size | checkpoint 抖动（w_await 周期性尖峰对齐 pagewriter 波峰） | 拉长间隔摊平 IO，但重启恢复时间变长 |
| wal_writer_sleep | commit 等异步刷脏 | 小事务高频场景 |
| commit_delay / commit_siblings | LogFileSync/XLogFlush 等待高且 commits 极高 | 群提交微调（收益有限，先看批量） |
| enable_double_write | 刷脏压力 | 关它有页撕裂风险，不动 |
| （OS）vm.dirty_background_ratio / vm.dirty_ratio / vm.dirty_expire_centisecs | OS 写回节奏与库刷脏叠加 | 双层缓冲互相踩；VastBase 自管脏页时 OS 侧宜收敛 |
| advance_xlog_file_num | WAL 预扩展 | 预分配不足时写放大 |

## 4 锁 / 并发

| 参数 | 判什么 | 方向 |
|---|---|---|
| deadlock_timeout | 死锁检测频率 | logrpt 有 deadlock 且业务可容忍时再动 |
| lockwait_timeout | 锁等待上限 | 行锁排队场景的业务语义问题，参数只是止血 |
| max_connections | too many clients 错误、Threads 涨 | 配合**应用侧**连接池或砍连接数 |

> 线程池（enable_thread_pool / thread_pool_attr）：VastBase 不稳定特性——源码不精读，
> **分析建议中永不出现**（仅可作为环境事实记录）。连接类治理一律走应用侧连接池。
| default_transaction_isolation | repeatable read 长快照 → 膨胀+锁面 | 与长事务榜联判 |

## 5 内存

| 参数 | 判什么 | 方向 |
|---|---|---|
| max_process_memory | VastBase 总内存封顶；Shared Context 涨+OOM 类错误 | 各分项之和 vs 物理内存给量化 |
| shared_buffers / cstore_buffers / segment_buffers / temp_buffers / local_syscache_threshold | 各池占比 | syscache 涨=会话数×阈值 |
| maintenance_work_mem | vacuum 慢（dead tuples 高+workers 忙） | 单独给 vacuum 更大配额 |
| （OS）vm.swappiness / vm.min_free_kbytes / vm.overcommit_memory | pswpout>0、WM Free<Min | swappiness 调低；min_free 兜突发 |

## 6 膨胀 / vacuum

| 参数 | 判什么 | 方向 |
|---|---|---|
| autovacuum(=enable_autovacuum) / autovacuum_max_workers | dead tuples 涨 + worker_cnt 恒 0 或全忙 | 开启/加 worker（看 IO 余量） |
| autovacuum_vacuum_scale_factor / threshold | 大表触发太晚（n_dml_tup 已巨大才清） | 大表调低阈值 |
| autovacuum_naptime / autovacuum_timeout | 清理节奏 | |
| vacuum_cost_delay / vacuum_cost_limit | vacuum 被限流饿死 | IO 有余量时放松限流 |

## 7 连接 / 网络

| 参数 | 判什么 | 方向 |
|---|---|---|
| max_connections | too many clients 错误、Threads 涨 | 配合池化 |
| （OS）net.core.somaxconn + net.ipv4.tcp_max_syn_backlog | ListenDrops/ListenOverflows>0、SYN 重传 | 两项一起调，应用侧 backlog 同步 |
| （OS）net.core.netdev_max_backlog | Softnet dropped>0 | 调大+加队列 |
| （OS）net.core.rmem_max/wmem_max | 大批量传输 RTT 正常但吞吐低 | |
| （OS）net.ipv4.ip_local_port_range | 端口耗尽（time-wait 海量+建连失败） | 扩段+tcp_tw_reuse |
| （OS）fs.file-max / fs.nr_open | fd Total 逼近上限 | |
| （OS）kernel.sem | 信号量上限（连接建立失败+semget 报错） | |

## 8 复制

| 参数 | 判什么 | 方向 |
|---|---|---|
| synchronous_commit / sync_config_hostname / most_available_syncmode | Apply lag 高拖主库 commit（LogFileSync 高+备 lag 高） | 语义权衡：异步/多数派，先和业务确认 |
| wal_level / enable_stream_replication | 槽 inactive、复制断 | |

## 9 采集开关（影响报告视野，分析前先看）

| 参数 | 影响 |
|---|---|
| enable_asp / asp_sample_interval | off ⇒ 无 ASP 桶（数据库侧时段不可判定） |
| instr_unique_sql_count | 满/重置 ⇒ SQL 榜缺项或负 Δ（报告有 negative SQL calls 警告） |
| logging_collector / log_min_duration_statement | off/过大 ⇒ logrpt 慢 SQL 盲区 |
| enable_thread_pool | Threads 语义（池化下后台线程形态不同） |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 无证据链先开参数 | 清单会匹配任何现象 | 先机理后参数 |
| 参数建议无当前值/建议值 | 无法执行无法复核 | 三件套：参数名=当前值→建议值+理由 |
| 忽略 Context 直接建议 reload | postmaster 类 reload 不生效 | 标注生效方式与重启窗口 |
| 忽略 sysctl Begin/End 两值 | 窗内改动被当成常态 | 两值不同单列证据 |
| 单项看参数不看组合 | 内存类参数相互挤占 | 配比总量一起核 |

## 版本注记

- 参数名与语义基于 G100 V3 档（advance_xlog_file_num、enable_double_write、
  local_syscache_threshold、thread_pool_attr 等为 VastBase 特有）
- 其他版本（Postgres 血统直系）参数集有差异，套用前核对
- vb_enable_int_varchar_eq_int 为 **2.2.15 PSU11 引入**（对应 PSU8 起的隐式转换行为变更）；
  308 系列（3.0.8）无此行为变更、无此问题，套用前先核版本线

## 来源

- 内核源码基线 `<openGauss-6.0.0-src>/`（GUC 定义）
- vb_tool Database Parameters / OS Kernel Parameters 渲染源码（2026-10-24）
- 实测样本核对（2026-10-21 报告参数段）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
