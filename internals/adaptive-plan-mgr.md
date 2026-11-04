# 自适应计划选择（gplanmgr / PMGR 多候选算法）

## 概述

> 服务分类：performance（倾斜数据计划劣化对策）、sql（绑定变量计划稳定性）；
> 联动 `query-plan-cache.md`（C/G 判定瀑布——本模块是瀑布判"走 generic"之后的
> 下一层分岔）；本模块 = openGauss 内核里 PBE 体系唯一的**学习型算法**

`enable_pbe_optimization` 本体没有算法——它是瀑布第 12 级的策略开关（force
generic）。真正的算法在 **gplanmgr（Plan Manager，PMGR）**：为一条命名 prepared
语句维护**多个候选 generic 计划**，按查询特征（各 baserel 选择率向量+索引选择率）
做**置信区间匹配**选计划，倾斜数据下避免"一个 gplan 打天下"的劣化。源码自述
（gplanmgr.cpp:1164）："caches k feasible plans and picks the best one by matching
query-plan selectivities iteratively… avoid execution regression on heavily skewed data"。

## 启用条件（全部满足才走）

| 闸 | 内容 | 出处 |
|---|---|---|
| 编译/实例闸 | `ENABLE_CACHEDPLAN_MGR = IS_SINGLE_NODE && enable_cachedplan_mgr && !GPC` | plancache.h:39 |
| GUC | **enable_cachedplan_mgr**：POSTMASTER 级 bool，**默认 true**（单机构建，非 LITE） | guc.cpp:1739 |
| 语句闸 | 命名 prepared 语句（stmt_name 非空；unnamed/one-shot 不配） | gplanmgr.cpp ChooseAdaptivePlan |
| 参数闸 | 有绑定参数且 **numParams < 50** | 同上 |
| 语句型 | 仅 SELECT；is_saved；无子查询 | 同上 |
| 版本闸 | workingVersionNum ≥ PLAN_SELECT_VERSION_NUM | GetHintSelectionMethod |
| **hint 闸** | 语句带 `/*+ Choose_adaptive_gplan */`（无 hint 恒走默认 gplan） | hint_gram.y:303 |

hint 三兄弟（hint_scan.l:48-50 / parse_hint.h:59-61）：
- `Use_cplan` / `Use_gplan` → 瀑布第 10 级（CHOOSE_BY_HINT），只定 C/G
- `Choose_adaptive_gplan` → method=CHOOSE_ADAPTIVE_GPLAN，启动本算法

## 位置：GetCachedPlan 主流程（plancache.cpp:1898-1906）

```
customplan = ChooseCustomPlan(...)        // 14 级瀑布先判 C/G
if (!customplan) {                        // 定了走 generic
    if (ChooseAdaptivePlan(...))          // 上表全部闸过
        plan = GetAdaptGenericPlan(...)   // ← 自适应多候选
    else
        plan = GetDefaultGenericPlan(...) // ← 默认单 gplan（pbe_optimization 的 FQS 标记在此）
}
```
★交互：`enable_pbe_optimization=on`（集中式=无条件 force generic）会把语句**从第 1 次
执行**就推进 generic→自适应分岔——pbe_opt 强推 generic 的倾斜劣化，恰是本算法的解药，
两者是"制造问题/解决问题"的对偶。

## 五步框架（源码注释原文归纳，gplanmgr.cpp:1226-1258）

1. **计划探索（explore）**：带真实参数跑 planner 产出候选计划，记录平均执行代价
   作为该语句的 **benchmark**（假设 planner 足够聪明）；PMGR_ExplorePlan 里
   ADAPTIVE 方法置 `uParamInfo=PARAM_VAL_SELECTIVITY_INFO`——**参数值级**匹配
   （能识别"age>30 的部分索引不适用于青少年查询"这类语义）
2. **计划评估（evaluate）**：每个候选取 k 次执行的平均运行时代价
   （sample_exec_costs/sample_times；MIN_EVAL_TIMES=3）
3. **方法选择**：若最优计划 P 满足 `runtime(P) ≤ factor × benchmark` → 选
   **mincost 方法**（P 一把梭，mini_cost_plan）；否则转自适应（MIN_EXPL_TIMES=5）
4. **自适应选择**：按选择率区间匹配挑计划；先做回归检查，检出回归退回步骤 3
5. **计划验证**：验证选中计划，无效则重新探索（回步骤 1）；
   状态机 `AdaptCachedPlanStat`：UNCHECKED → SURVIVAL / KNOCKOUT（**淘汰赛**）

主循环：`do { ActionHandle(...) } while (action->type != PMGR_FINISH)`，
plansource 持 LW_SHARED 锁防并发选择；结束再把选中计划代价与 avg custom cost
对比，不划算则返回 NULL 回退 custom 计划。

## 核心算法件

| 件 | 机制 | 出处 |
|---|---|---|
| 查询特征 | 各 baserel 选择率向量 + 各索引选择率（IndexCI 带 indexoid/indexquals/is_partial） | gplanmgr.h RelSelec/IndexCI |
| 候选记录 CachedPlanInfo | relCis + indexCis + offsetCi（LIMIT offset 区间）+ learningRate + planHashkey + 采样账本 + 状态 | plancache.h:470-486 |
| 计划去重键 | `nodeToString(planTree) → hash_any`，接受哈希碰撞换效率 | GeneratePlanStmtKey |
| 匹配 | 每个特征维都 FallIntoCI 才选中候选 | FallIntoCI |
| 区间学习 | miss 时扩张：`bound ± min(dampfactor, gap×1.1)`（阻尼防激进）；LIMIT offset 区间直接全拥抱 | UpdateCI / UpdateOffsetCI |
| 探索标记 | is_plan_exploration + params_lazy_bind，探索期不绑参数 | PMGR_ExplorePlan |

## 观测配方（2026-11-03 env-T 实测）

`set client_min_messages=debug2;` 后看 MOD_OPT 模块 DEBUG2 串：
`Initialize adaptive plan selection context` → `begin plan selection` →
`Explore a plan` → `add explored plan into cache` → `End plan selection`。

env-T（G100 V3.0 Build 8）实测（倾斜表 100 行 v=1 / 其余唯一值，7 次执行）：
前 5 次 CPlan（First 5 times）→ 第 6 次瀑布判 generic 后 PMGR 全生命周期实见 →
第 7 次因选中计划不比 avg custom cost 便宜回退 CPlan（Default choosing）——
**机制活着 + 退出条件真实生效**。多候选分叉（两个计划各自 CI 生效）需更多
差异化执行次数才能观察到〔未实测〕。

## 判读应用

| 现象 | 查向 |
|---|---|
| 倾斜数据 + 绑定变量 gplan 劣化 | 语句加 `/*+ Choose_adaptive_gplan */`（确认 enable_cachedplan_mgr=on、GPC 关、命名语句、SELECT、参数<50、无子查询） |
| 加了 hint 但没生效 | 按启用条件表逐闸排查；GPC on 时整模块关闭 |
| debug2 日志见 Explore/plan selection | 正常：该语句走了自适应选择，不是故障 |
| custom/generic 日志与 plan selection 交错 | 设计如此：瀑布先判方向，PMGR 只管 generic 侧选哪个 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| "enable_pbe_optimization 有学习算法" | 它只是策略开关；算法在本模块（gplanmgr），靠 hint 启用 |
| "自适应默认开启" | 基础设施（GUC）默认 on，但**算法本身要 hint 显式点名**，无 hint 恒默认单 gplan |
| GPC 场景期望自适应 | ENABLE_CACHEDPLAN_MGR 要求 !GPC，两者互斥 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（=G100 V3 Build 8 档）；行号会漂，函数名为锚
- 源码精读（2026-11-03）：gplanmgr.cpp 全文件结构 + ChooseAdaptivePlan/
  GetAdaptGenericPlan/PMGR_ExplorePlan/UpdateCI/FallIntoCI/GeneratePlanStmtKey/
  SearchCandidatePlan/InsertPlan；hint_gram.y:283-311 + hint_scan.l:48-50 +
  parse_hint.h:59-61；plancache.h:39/470-510；GetCachedPlan 分岔 plancache.cpp:1898-1906
- env-T 实测（2026-11-03）：倾斜表 7 执行全生命周期 DEBUG 串实见 + 回退 custom 实证
- 联动：`query-plan-cache.md`（瀑布与 PBE 参数全景）、`../sql/optimizer-deep.md`

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`。
