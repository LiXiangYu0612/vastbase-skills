# logging_module 与 debug 系 GUC：机制、实战配方与死旋钮清单

服务分类：performance（排障开什么）+ troubleshooting（现场救火）。源码基线
openGauss 6.0.0；实机对照 env-T（VastBase G100 3.0.8，2026-11-02 实测）。

## 三条核心结论（先记这个）

1. **`logging_module` 单开没输出——必须配 `log_min_messages`**（双闸机制，下节）。
   正确配方（env-T 实证，一条两表 join 吐 197 行 DEBUG）：
   ```sql
   set logging_module = 'on(ALL)';      -- 或定向 on(OPT_JOIN)
   set log_min_messages = DEBUG5;       -- 或 DEBUG2，按噪声取舍
   ```
2. **默认 `off(ALL)` 连"裸 DEBUG"（无模块标注的消息）一起关**——只降
   log_min_messages 不开模块，一样 0 行（env-T 实证）。
3. **一批 debug GUC 是死的**（编译闸挡掉/零消费点/未注册），开之前先
   `show <参数>` 验存在性，死清单见下节——别在死旋钮上耗现场时间。

## logging_module 机制（源码定论）

- 语法 `on(MOD_A,MOD_B)` / `off(ALL)`；**模块名不带 `MOD_` 前缀**（module_map
  值，be_module.cpp:144 如 `{MOD_SCHEMA, "SCHEMA"}`——写 `on(MOD_SCHEMA)` 报
  "module name not found"，env-T 实证）；`SHOW logging_module` 列本机全表
- 默认值 `off(ALL)`（guc_storage.cpp:4792）；PGC_USERSET 会话级可设，会话结束
  即恢复，不动全局
- **双闸**：闸 1 `log_min_messages`（elog.cpp:352 `is_log_level_output`，不看
  模块）→ 闸 2 `is_errmodule_enable`（elog.h:651：
  `elevel >= LOG || module_logging_is_on(mod_id)`）——**模块开关只对 DEBUG 级
  生效**；LOG 及以上（INFO/NOTICE/WARNING/ERROR…）不受 logging_module 管
- `off(ALL)` 把 MOD_MAX（无 errmodule 标注的裸消息的桶，默认 on）也关掉
  （be_module.cpp:215 注释）——所以要裸 DEBUG 也得 `on(ALL)`
- **模块名跨版本漂移**：6.0.0 的 `MEM` 在 3.0.8 叫 `MEM_CTL`；`SCHEMA` 在
  3.0.8 模块表里存在但无对应 DEBUG 代码（6.0.0 才有 search path dump）。
  **每台机器先 `SHOW logging_module` 再选模块名，不拿清单硬套**
- 宏封装的模块日志（STREAM_LOG/OPT_LOG/MEMCTL_LOG，executor.h:77/optimizerdebug.h:43）
  自查 `is_errmodule_enable(DEBUG2, MOD_X)` 后仍走 ereport——**同样要过闸 1**，
  即模块+级别两件套缺一不可

## 实测输出与能查什么（env-T 真机，on(ALL)+DEBUG5，单条 join）

| 模块 | 行数 | 输出内容 | 能查什么 |
|---|---|---|---|
| OPT_JOIN | 48 | `A new path is accepted with cost = 58.352500`、`Initial mergejoin/nestloop cost: startup_cost…`、`Precheck drop new path: startup_cost=…`、`Try to join these rels` | **优化器逐路径决策回放**——为什么选这条路/为什么丢弃那条（代价对比逐条可见）；配 `sql/optimizer-deep.md` 代价复算 |
| INSTR | 89 | statement 历史视图逐区域 dump：`Basic Information Area(common/related to SQL)`、`Time Model Info Area`、`Row Activity And Cache IO`、`Network`、`Lock Summary`、`Events Information Area` | statement_history/pgstat 计数器口径对不上时看写入现场 |
| BACKEND | 41 | 事务/门户等内部状态 | 事务生命周期跟踪（噪声大，配合场景） |
| MEM_CTL | 4 | 查询内存核算（SetNgQueryMem 等，memctl.cpp 111 处调用） | work_mem/查询内存分配与 EarlyFree 明细 |
| SPI | 5 | `add_distribute_info execNodes is NULL` 等 | PL/SQL 内部执行上下文 |
| SCHEMA（6.0.0 才有） | — | `RelnameGetRelid search path:…`（namespace.cpp:774，DEBUG2） | search_path 解析过程/对象为什么找不到 |
| REDO（6.0.0） | — | redo 前逐页 DumpPageInfo（redo_heapam.cpp:103）+ `[REDO_LOG_TRACE]heap_xlog_update` | 崩溃恢复/PITR 回放页级追踪 |

**噪声纪律**：DEBUG5+on(ALL) 一条 join 就 197 行，生产先定向（on(OPT_JOIN)）
再降级；用完靠会话退出自然恢复；日志量瞬间 GB 级是真实风险。

## debug 系 GUC 全量清单与成色（6.0.0 源码普查 + env-T 实机）

### 活的、有用的

| GUC | 级别 | 机制与输出 | 何时开 |
|---|---|---|---|
| logging_module | USERSET | 上节 | 优化器/内存/统计口径深挖 |
| debug_print_plan（/parse/rewritten） | **SIGHUP——SET 报 "cannot be changed now"（env-T 实证），须 gs_guc reload** | 计划树 dump（postgres.cpp:1429，条件含 `log_min_messages <= LOG`）；GPC 场景另有消费（globalplancache_view.cpp:104） | 计划结构留档；日常优先 explain（可读性），真正价值在自动场景/GPC 排查 |
| debug_pretty_print | USERSET | 计划/树输出美化（print.cpp） | 配合上面 |
| memory_tracking_mode | USERSET，`none/peak/normal/executor`（guc_memory.cpp:169-172） | memtrack.cpp:341/384/457：normal=内存上下文级追踪（配 session_memory 系视图）、executor=**算子级**（SQL 自调优口径）、peak=峰值 | 会话内存涨/算子内存归属；注意开销（normal 以上逐上下文记账） |
| memory_detail_tracking | USERSET，格式 `<计数>:<计划节点id>`（MemoryTrackingParseGUC，memtrack.cpp:100） | 定位到具体计划节点的内存明细；消费在 MEMORY_CONTEXT_CHECKING 编译域 | 算子内存精确定位（确认编译闸后用） |
| enable_memory_context_check_debug | g_instance 级 | mcxt.cpp:708 内存上下文一致性检查 | 内存上下文损坏类问题 |
| enable_startwith_debug / enable_nodegroup_debug | USERSET | WITH RECURSIVE 5 站/分布式 nodegroup 2 站 | 递归 CTE 调试；单机场景基本用不上 |

### 死的/坑（开了分析不出问题——重点）

| GUC | 死因（源码实锤） | 实机表现 |
|---|---|---|
| trace_locks / trace_userlocks / trace_lwlocks / debug_deadlocks | GUC 表整体在 `#ifdef LOCK_DEBUG` 内（guc_storage.cpp:509）；LOCK_PRINT/PRINT_LWDEBUG 空宏（lock.cpp:233/lwlock.cpp:258）——**release 编译不注册** | SET 报 `unrecognized configuration parameter`（env-T 实证×4）。**锁问题走锁等待视图/pg_stat_get_backend_lwlocks()，不走这些参数** |
| trace_lock_oidmin / trace_lock_table | 注册，但唯一消费 LOCK_DEBUG_ENABLED 在 release 走 `&Dummy_trace`（lock.cpp:104-115），且 LOCK_PRINT 本体是空宏 | SET 成功、**零输出**——比报错更坑 |
| comm_debug_mode | 6.0.0 全库 **0 个消费点**（libcomm 调试实际靠编译宏） | SHOW 有值、开=无效 |
| enable_adio_debug | ADIO_LOG_DB 宏（elog.h:487）定义后 **0 次调用** | 同上 |
| enable_debug_vacuum | 唯一消费点在 `#ifdef USE_ASSERT_CHECKING`（heapam_visibility.cpp:196） | release 死 |
| logging_module 的 11 个零调用模块名（6.0.0 普查：94/107 有调用） | TRANS_CLOG、PARTITION、HEARTBEAT、PARQUET、CARBONDATA、SEC_SDD、SEC_LEGER、RESOWNER、OPT_SETOP、DSS、COMM_POOLER | `on()` 它们白开——模块在表里≠有日志 |
| trace_notify / trace_sort / trace_recovery_messages / debug_assertions | **PG 有、本库无**（GUC 表不存在） | 禁拿 PG 文档知识硬套（收录标准第 0 条同源纪律） |

### 普查口径教训（方法论）

单行 grep `ereport(DEBUGn, (errmodule(MOD_X` 数出"DEBUG 级≈0"是**假零**——
ereport 多行书写骗过了单行匹配；6.0.0 optimizer 内 OPT_JOIN 的 DEBUG 级
errmodule 实为 82 处。跨行普查用 `-B 3` 回看级别再计数。

## 症状 → 开什么（处方速查）

| 症状 | 处方 | 预期得到 |
|---|---|---|
| 计划差：为什么不用某连接方式/为什么弃路径 | `on(OPT_JOIN)`+DEBUG5 | 逐路径 cost 接受/丢弃回放 |
| **设了 SMP 但计划没并行** | **`set query_dop=N` + `set log_min_messages='debug5'` + `set logging_module='on(OPT,OPT_JOIN)'`（2026-11-02 用户授）** | 不能生成/没选 SMP 的原因与逐路径 `stream_cost` 回放（专题见下节） |
| statement_history/计数器数字对不上 | `on(INSTR)`+DEBUG5 | 视图各区域写入 dump |
| 查询内存核算/work_mem 出入 | `on(MEM_CTL)`+DEBUG5；或 memory_tracking_mode=executor | 内存分配明细/算子级归属 |
| 会话内存持续涨 | memory_tracking_mode=normal + session_memory 视图 | 上下文级明细（不动日志） |
| 对象找不到/search_path 行为怪 | `on(SCHEMA)`+DEBUG2（6.0.0 档） | 每次解析的查找路径全 dump |
| 崩溃恢复/PITR 页级疑案 | `on(REDO)`（6.0.0 档，慎用——逐页 dump 极重） | 每条 redo 前页内容 |
| 计划缓存/GPC 疑案 | debug_print_plan（gs_guc reload 级） | 计划树结构留档 |
| 锁/死锁 | **不开 debug 参数**——log_lock_waits（正式功能）+锁等待视图+logrpt 死锁环 | 阻塞链与死锁双方 |

## 专题：设了 SMP 但执行计划不并行的分析（query_dop 生效但无 Stream 节点）

**诊断配方（2026-11-02 用户授，env-T 实证）**：

```sql
set query_dop = 4;                        -- 先设目标 dop
set log_min_messages = 'debug5';
set logging_module = 'on(OPT,OPT_JOIN)';  -- 优化器会打印不能生成/没选 SMP 的原因
```

**读法**：日志里逐路径 `final cost <算子>: stream_cost: X, startup_cost: …, total_cost: …`
——**stream_cost 是 SMP 参与与否的直接读数**（env-T 实证样例：
`final cost merge join: stream_cost: 0.000000, total_cost: 123.605000`）：

- 全程 stream_cost=0 → **SMP 路径根本没生成**——查下面闸门清单（语句形态被禁）
- 有 stream_cost>0 的行但最终计划无 Stream 节点 → **生成了但代价输了**（小表/倾斜时
  正常行为；结合行数评估是否值得 dop）

**闸门清单（6.0.0 源码定论，按命中概率排序）**——SMP 三条件
`is_stream = query_dop>1 && smp_enabled && stream_cxt.global_obj==NULL`
（streamplan_single.cpp:76-90 set_default_stream）：

| 闸门 | 源码位置 | 说明 |
|---|---|---|
| **语句在函数/存储过程里（SPI）** | spi.cpp:1669/2150/2719 CloseSmp | PL/pgSQL 体内 SQL 一律不并行（pl_handler/pl_exec 另 5 处）——**最常见暗坑** |
| **cursor** | parse_expr.cpp:3932（cursor 内表达式）+ createplan.cpp:6204 ReBuildNonSmpPlanForCursorExpr | DECLARE CURSOR 走非 SMP 计划重建 |
| 嵌套 stream | set_default_stream 第三条件 | 外层已建 stream 对象，内层子查询不再并行 |
| COPY | copy.cpp:2591 | COPY 语句禁 |
| 内部任务 | autovacuum.cpp:1360 / job_worker.cpp:218 / wdr/snapshot.cpp:1501 / index_advisor / db4ai | 后台任务不走 SMP |
| initdb | streamplan_single.cpp:80 | 安装期 |
| dop 会话没真生效 | `show query_dop` 在**同一会话**验；PBE/计划缓存：plancache.cpp:121 IsStreamSupport 参与 GPC 兼容判断，**PREPARE 时 dop=1 的语句后改 dop 不重规划**——别在 dop=1 会话里 prepare 再 SET | |
| 代价竞争 | stream 路径按 cost 与串行路径同台竞争 | 小表不并行是正确行为，不是 bug |

注意：单机版 `enable_stream_operator` GUC **不被读取**（IsStreamSupport 在
非 ENABLE_MULTIPLE_NODES 分支只看 query_dop+smp_enabled，plancache.cpp:116-124）。

## 版本注记

- 源码结论基于 openGauss 6.0.0 树（≈G100 V3 档）；env-T 实机=VastBase G100 3.0.8
  （更早内核）：模块名 MEM_CTL/MEM 漂移、SCHEMA 无 dump 代码、trace 族未注册
  ——**结论跨版本前先在目标机 `SHOW logging_module` / `SHOW <guc>` 验一遍**

## 来源

- 6.0.0 源码：guc_storage.cpp:4788-4795（默认 off(ALL)）/:509（LOCK_DEBUG 块）；
  elog.h:651（is_errmodule_enable）/:487（ADIO_LOG_DB）；elog.cpp:352/685/2013
  （双闸）；be_module.cpp:144/:201-262；lock.cpp:104-115/:233；lwlock.cpp:232-258；
  heapam_visibility.cpp:196；namespace.cpp:771-778；redo_heapam.cpp:103；
  memtrack.cpp:100-127/:341-457；guc_memory.cpp:169-172
- errmodule 全库普查：107 模块名中 94 有调用、11 零调用（2026-11-02）
- env-T 实测（2026-11-02）：GUC 存在性逐个 SHOW；双闸坑逐级复现（只开模块 0 行→
  只降级别 0 行→on(ALL)+DEBUG5 197 行）；on(MOD_SCHEMA) not found；
  debug_print_plan SET 报 cannot be changed now；模块分布 OPT_JOIN 48/INSTR 89/
  BACKEND 41/MEM_CTL 4

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
