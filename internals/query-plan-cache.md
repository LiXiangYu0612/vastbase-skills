# PBE 与计划缓存（custom ↔ generic 判定链）

## 概述

> 服务分类：performance（Plan Time/计划突变判读）、sql（绑定变量与计划稳定性）；
> 联动 `../admin/g100-standard-params.md`（enable_pbe_optimization 地雷）

prepared 语句每次执行用定制计划（代入实参）还是泛化计划（按参数典型值），
由 `common/backend/utils/cache/plancache.cpp:1712 ChooseCustomPlan()` 决定——
**14 级判定瀑布，从上到下短路**，每级都有 ReportReasonForPlanChoose 观测点。

## 判定瀑布（代码顺序）

| # | 条件 | 结果 | 观测原因码 |
|---|---|---|---|
| 1 | 共享计划缓存（GPC InShareTable） | generic | SHARED_PLANCACHE |
| 2 | upsert 带 update 参数 | custom | UPSERT_UPDATE_QUERY |
| 3 | pbe_optimization + MOT 引擎 | generic | PBE_OPT_AND_MOT_ENGINE |
| 4 | DELETE LIMIT / 参数在表达式（col=$1+$2） | custom | PARAM_EXPR |
| 5 | one-shot 计划 | custom | ONE_SHOT_PLAN |
| 6 | 无绑定参数 | generic | NO_BOUND_PARAM |
| 7 | 事务控制语句 | generic | TRANSACTION_STAT |
| 8 | 调用方 cursor_options 强制 | 按强制 | CALLER_FORCE_* |
| 9 | **含 cstore 表**（IsForceCustomplan） | custom | CSTORE_TABLE |
| 10 | hint 指定 | 按 hint | CHOOSE_BY_HINT |
| 11 | plan_cache_mode 强制 | 按强制 | SETTING_FORCE_* |
| 12 | **enable_pbe_optimization 且 gplan_is_fqs** | **generic** | PBE_OPTIMIZATION |
| 13 | **执行次数 < 5** | custom | TRY_CPLAN |
| 14 | **generic_cost ≤ 平均 custom_cost × 1.1** | generic | COST_PREFER_GPLAN |
| — | 以上都不满足 | custom | DEFAULT_CHOOSE |

## 核心语义

1. **"第 6 次换 generic"的精确条件**：前 5 次强制 custom（源码注释自认 arbitrary）；
   第 6 次起若泛化计划代价 ≤ 历史 custom 平均代价 × 1.1（10% 容差）则转 generic；
   两代价皆 0（FQS/远端 LIMIT）也偏 generic。代价未知（-1）时也偏 generic
2. **enable_pbe_optimization 的作用点 = 第 12 级**：对可 FQS（快速查询下发）的语句
   **跳过代价比较直接 generic**——这就是标准表两条 bug 记录的机制本体：
   数据倾斜时按典型参数生成的泛化计划对偏斜参数劣化（高选择率场景计划劣化 bug）。
   ★**集中式（G100 单机）退化**：`gplan_is_fqs` 初始即 true（plancache.cpp:269），
   置 false 的唯一入口在 IS_PGXC_COORDINATOR 分支（plancache.cpp:2103）——集中式无 CN
   恒不执行 ⇒ **enable_pbe_optimization=on 在 G100 = 对一切带参语句无条件强制 generic**
   （env-T 实测：第 1 次执行 reason 即 "Using pbe optimization"）。分布式里 FQS 过滤才真正生效
   ★**batch bind 内部强制开**（postgres.cpp:10567-10573）：批量绑定执行防 cplan 内存爆炸，
   临时置 true 用 gplan，执行完还原——库级/会话级 off 拦不住这条路径
3. **代价比较的盲区**（源码注释自认）：比较的是估算代价，没有把**计划生成时间**
   与运行时代价差异换算的手段——Plan Time 高的语句 custom 重建成本被低估
4. **cstore 表恒走 custom**（第 9 级）——含列存表的语句没有泛化计划选项
5. GPC（全局共享计划缓存）恒 generic——跨会话共享的前提是计划与参数无关
6. **瀑布不是终点**：判"走 generic"后还有一层分岔（plancache.cpp:1898-1906）——
   过全部门闸且带 `Choose_adaptive_gplan` hint 的语句进 gplanmgr 多候选自适应选择
   （`adaptive-plan-mgr.md`），其余走默认单 gplan。第 10 级的 hint 关键字原文即
   `Use_cplan` / `Use_gplan`（hint_scan.l:48-49），另 `Choose_adaptive_gplan` 不进
   第 10 级、直接点自适应分岔

## plan_cache_type_validation（结果类型校验开关，≠ plan_cache_mode）

两个参数名相近，**管的完全是两回事**：

| 参数 | 管什么 | 形态 |
|---|---|---|
| `plan_cache_mode` | custom↔generic 选哪类计划（上表第 11 级） | enum（auto/force_generic_plan/force_custom_plan），**user 级**，会话可改 |
| `plan_cache_type_validation` | 缓存计划的**结果 tupdesc 是否允许中途变化** | bool，**postmaster 级（改须重启实例）**，实例级全局，默认 on |

机制（定义 guc_sql.cpp:1224 → 宏 `FORCE_VALIDATE_PLANCACHE_RESULT` guc.h:484）：

1. **on（默认）**：PREPARE / 扩展协议 Parse 建计划时 `fixed_result=true`
   （prepare.cpp:202、postgres.cpp:3560 → CreateCachedPlan 落位 plancache.cpp:544）。
   后续执行触发 RevalidateCachedQuery 重分析，结果 tupdesc 与缓存不一致
   （plancache.cpp:1059）→ **ERROR "cached plan must not change result type"**
2. **off（须重启生效）**：`fixed_result=false`，tupdesc 漂移不再报错——只发
   NOTICE 后就地更新 plansource->resultDesc（plancache.cpp:1062-1075）；
   FetchPreparedStatementResultDesc 每次取描述前强制 Revalidate（prepare.cpp:1104），
   保证 portal 拿到最新列描述
3. EXECUTE / EXPLAIN EXECUTE 对 variable-result 计划有防御性拒绝
   （prepare.cpp:385、1433）；Bind/Describe 对应 Assert（postgres.cpp:5825/12118）

触发场景：长连接绑定变量（JDBC/驱动默认走 PBE 扩展协议）+ 语句缓存期间对象结构被
DDL（改列类型、加/删列作用于 SELECT * 或目标列）、视图重定义、search_path 换表——
重建计划时输出结构对不上即报上述 ERROR，业务语句失败。关掉该参数 = 兼容性逃生门
（放行并按新结构继续执行），代价是结果列描述可漂移、须重启才能切换。

判读：日志见 "cached plan must not change result type" → 先查**谁 DDL 了被缓存语句
引用的对象**；处置 = 业务侧 DEALLOCATE/重连重建语句，或（审慎、须重启）关本参数。
**不要去动 plan_cache_mode**——那是计划选择问题，不是结果类型问题。

## PBE 参数全景（10 参数，2026-11-03 源码+env-T 实测）

| 参数 | 级别 | 默认(源码) | 一句话 |
|---|---|---|---|
| plan_cache_mode | USERSET | auto | 第 11 级强制 custom/generic（auto/force_generic_plan/force_custom_plan） |
| plan_cache_type_validation | POSTMASTER | on | 结果 tupdesc 校验（见上节） |
| enable_pbe_optimization | SUSET | true | 第 12 级强制 generic；**集中式=无条件**（见核心语义 2）；VB 标准基线 off；★env-T 配置文件 on（踩雷态） |
| enable_global_plancache | POSTMASTER | false | GPC 跨会话共享计划缓存：开启时第 1 级 InShareTable 恒 generic；check hook 自动抬 local_syscache_threshold≥16MB（guc.cpp:11834）；restore 模式强制关（guc.cpp:11849） |
| enable_cachedplan_mgr | POSTMASTER | true(单机) | 自适应计划选择（gplanmgr）基础设施闸：与 GPC **互斥**；算法本身还需 `/*+ Choose_adaptive_gplan */` hint——专篇 `adaptive-plan-mgr.md` |
| enable_opfusion | USERSET | true | 点查融合执行总开关（专篇 `opfusion.md`）；env-T 配置文件 off |
| enable_partition_opfusion | USERSET | false | 分区表 opfusion 门（GUC+beta 位双开关） |
| enable_opfusion_reuse | USERSET | false | 复用会话内 fusion 对象（仅 DML 同表） |
| enable_beta_opfusion | USERSET | false | beta opfusion 特性包 |
| opfusion_debug_mode | USERSET | off | opfusion 观测（off/log） |

**GPC 环境签名耦合**：enable_pbe_optimization/enable_opfusion/enable_partition_opfusion
都进 GPC env signature（bit14/17/18，globalplancache_util.cpp:121-127）——会话间这些
参数取值不同 = GPC 环境分裂不复用；诊断"GPC 命中率低"时先对账 PBE 族参数一致性。

## AWR 判读应用

| 现象 | 查向 |
|---|---|
| 同一 SQL 前几次快、第 6 次起慢 | 第 13→14 级切换：custom→generic 计划劣化（参数倾斜典型）；对策 plan_cache_mode=force_custom_plan（会话级）或改写消除倾斜 |
| Plan Time 占比高 + 高频执行 | 第 14 级反复 custom/generic 摇摆或失代价比较资格 |
| enable_pbe_optimization=on 且 FQS 语句计划差 | 第 12 级：按标准基线应 off（地雷），回归后关闭 |
| 观测接口（配方已核） | `set client_min_messages=debug2;` 后每次执行打 `[Choosing C/G Plan]: GPlan/CPlan, reason: …`（plancache.cpp:3373）。NOTICE 分支需 logging_module on(MOD_OPT_CHOICE)，但 **VB 构建未注册该模块**（env-T 实测 module not found）→ 实用配方就是 client_min_messages=debug2 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| "绑定变量必走 generic" | 前 5 次 custom；此后按 1.1 倍代价容差决定 |
| 计划变差只盯统计信息 | 先查本瀑布：是否刚跨过 5 次阈值或 pbe_optimization/FQS 路径 |
| "plan_cache_type_validation 是计划缓存模式" | 它是**结果类型校验**开关（postmaster 级 bool，默认 on）；模式选择是 plan_cache_mode（user 级 enum） |
| 见 "cached plan must not change result type" 去调 plan_cache_mode | 该错误=结果 tupdesc 漂移（DDL 动了对象）；对策在 plan_cache_type_validation 或业务侧重建语句 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）；行号会漂，函数名为锚
- 源码精读（2026-10-25）：plancache.cpp:1712-1833（ChooseCustomPlan 全函数含注释）
- plan_cache_type_validation 源码+实测（2026-11-03）：guc_sql.cpp:1224（PGC_POSTMASTER/
  XC_HOUSEKEEPING/默认 true/g_instance 实例级）＋ 全部 8 处消费点精读；
  env-T（G100 V3.0 Build 8，= 源码基线同 build）实测：pg_settings 值 on/context=postmaster/
  source=default；PREPARE→ALTER TABLE 改列类型→EXECUTE 复现
  "cached plan must not change result type"（默认路径）。off 路径未实测（须重启），源码定论
- PBE 参数全景+瀑布实证（2026-11-03）：9 参数 pg_settings 全景；三 reason 码 env-T 实证——
  默认瀑布 5×"First 5 times using CPlan"→"GPlan's cost is less than 10%…"（六次切换实锤）、
  pbe_optimization=on 首次执行即 "Using pbe optimization"（集中式退化实锤）、
  force_custom_plan→"Settings force the decision"；观测配方定型 client_min_messages=debug2。
  ★env-T 配置文件 enable_pbe_optimization=on（与标准基线 off 相反）——env-T 上绑定参数语句
  默认全 generic，任何计划类测试前先核该参数；opfusion 族细节见 `opfusion.md`
- enable_pbe_optimization 算法定论（2026-11-03 追问补全）：**该参数本体无算法**——
  6 处消费点全读，全部是策略门（MOT 分支/第 12 级门/集中式退化标记/批量绑定强制开/
  GPC 签名位）；PBE 体系真正的算法在 gplanmgr（PMGR 多候选自适应选择），已立专篇
  `adaptive-plan-mgr.md`（env-T 实测全生命周期跑通）
- 内部文档交叉验证（2026-11-04 入库）：《plan_cache_mode 和 enable_pbe_optimization
  参数分析》（用户提供 D:\二线团队）六参数组合实测与本文件瀑布一致；决策码源码锚
  plancache.cpp:1800（isPbeAndFqs）/ :1807（num_custom_plans<5）/ :1826
  （generic_cost ≤ avg×1.1）。注意该文档行文"enable_pbe_optimization=on 永远
  gplan"**不严谨**——代码有 `plansource->gplan_is_fqs` 前置（本文件核心语义 2
  的集中式退化推论即由此而来），以本文件为准
- 联动：`query-rewrite.md`（重写开关）、`../sql/optimizer-deep.md`（代价模型深潜）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
