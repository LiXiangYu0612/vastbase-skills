# 统计信息 → 选择率 → 代价（selfuncs 判定链）

## 概述

> 服务分类：performance（rows 估算偏差/计划抖动分诊）、sql（统计信息调优）；
> 代价模型本身见 `../sql/optimizer-deep.md`，本文件讲**选择率从哪来**

## 等值选择率判定链（var_eq_const，selfuncs.cpp:352-470，深读）

`col = 常量` 的选择率按序判定：

1. 常量为 NULL → 0
2. **列上有 unique 索引/DISTINCT → 直接 1/reltuples**，与统计信息新旧无关——
   唯一索引等值查找的行数估算永远可靠
3. **MCV（最频值）命中 → selec = 该 MCV 的频率**（ANALYZE 算多准就多准）
4. 未命中 MCV：
   - 基线 selec = 1 − ΣMCV频率 − null 占比
   - **单机构建**：走**均衡直方图**（var_eq_const_selectivity → binary_search_hist
     → get_num_eq_slot：常量落桶则用桶内均衡假设）——openGauss 用均衡直方图
     （equi-dhist），与社区 Postgres 的等频直方图构建不同〔锚点级〕
   - 分布式构建：selec ÷ otherdistinct（经典 1/distinct）
   - **时间序列外推**（openGauss 特有）：enable_extrapolation_stats=on 且列类型为
     时间类、整体选择率近 0、MCV 无倾斜（numbers[0] ≤ numbers[last]×SKEW_RATIO）
     → 用最小 MCV 频率外推——专为"新写入的时间值不在 MCV/直方图"的递增列设计
   - 钳制：不得超过最小 MCV 的频率（防过高估计）

## ANALYZE 采样（acquire_sample_rows + compute_scalar_stats 补读，2026-10-25 二次）

- **块采样 = Knuth Algorithm S**（analyze.cpp:2202-2233 注释明言，块层随机
  抽样非蓄水池）；**行采样 = 蓄水池替换法**（acquire_sample_rows
  :2471-2725 深读）：先装满 targrows，之后
  `rowstoskip = anl_get_next_S(...)` 定跳过距离，命中即
  `k = targrows × 随机数` 随机顶替一格——注释原话"reservoir is a true
  random sample of the tuples we've passed over so far"
- openGauss 加**预取乒乓双表**（fetchlist1/fetchlist2 轮转）降采样 IO
- 采样率由目标行数控制——`default_statistics_target` 双模式与算法见下节
- **均衡直方图构建**（compute_scalar_stats :5300-5545 深读）：采样值 qsort
  （compare_scalars 借排序副产物用 tupnoLink 记重复链）→ 先提 MCV（阈值：
  频次 > 均值+25%、样本 ≥2、≥1/桶数——防桶边界重复）→ **其余值按
  pos/posfrac/delta/deltafrac 有理步进等频切块**（num_hist = ndistinct−num_mcv，
  封顶 num_bins+1）——**等频桶正是消费侧 binary_search_hist 桶内均衡假设
  的来源**；varlena 超宽值（WIDTH_THRESHOLD）被剔除不进统计
- 采样循环内每行 vacuum_delay_point——**analyze 慢可能被限流**（联动
  `autovacuum.md` 限流节）

## default_statistics_target：双模式与采样量算法（2026-11-03 源码深潜）

GUC：USERSET，**-100~10000**，默认 100（guc_sql.cpp:1890）。会话级 u_sess 存储，
THR_LOCAL 镜像双向同步（guc.cpp:7583 / threadpool_worker.cpp:360 / vacuum.h:332）；
同进 GPC env signature（globalplancache_util.cpp:164）——改值=GPC 环境分裂。

**正值 = 传统绝对模式**（analyze.cpp:8438-8483）：

- 采样量走经典论文公式（*Random sampling for histogram construction:
  how much is enough?* Chaudhuri/Motwani/Narasayya）：
  `com_size = 4 × target × ln(2N/0.01) / 0.5²`（ε=ANALYZE_RELATIVE_ERROR=0.5，:144）
- 与各列 300×attstattarget 传统量取大，下限 DEFAULT_SAMPLE_ROWCNT=30000
  （parsenodes.h:1699），上限 work_mem 允许行数（get_workmem_allow_rows 按列宽折算）
- 默认 100 + N=100 万行 → 1600×ln(2×10⁸)≈3.06 万 ≈ 30000 下限（"从没改过
  默认 target 就是 30000" 的出处，:8452 注释）

**负值 = 百分比模式**（-N = 采 N%，analyze.cpp:8492-8498）：

- `com_size = ceil(N × |target|/100)`；**折算行数 < 30000 时直接整表当样本**
  （com_size=totalRowCnts；samptable 模式开关 :5748）
- **统计槽从实际采样行数反推**：attstattarget = 采样行数/300，钳 1~10000
  （caculate_target_rows :704-717）→ 大表低百分比 = 更密的 MCV/直方图
- 负值同时是**多列扩展统计的收集开关**（:1299）；正值收集时打 INFO
  "Please set default_statistics_target to a negative value to collect
  extended statistics"（:1647）

**★负值/0 关停 autoanalyze**（autovacuum.cpp:1650-1653 DO_ANALYZE 宏，三条件与门）：

```
DO_ANALYZE = autovacuum_mode ∈ {analyze, analyze_vacuum}
          && enable_global_stats && 0 < default_statistics_target
```

不满足 → relation_support_autoavac 置 enable_analyze=false（:1699）→ autoanalyze
全库不触发；auto**vacuum** 不受影响（DO_VACUUM 独立）；手工 ANALYZE 照常走百分比。
源码注释给的理由：percent 采样太低效，后台只支持传统采样。**门读 worker 会话的值**
（conf/库级设置才有效；应用会话 SET 影响不到 worker；ALTER SYSTEM 后须 reload 才
到 worker）。触发阈值公式（anl_base_thresh + scale×reltuples）本身与 target 无关。

**官方文档"样本≥2% 且表<160 万行时变慢"的数学根源**（推导闭合）：默认 100 的
论文公式采样量 = 1600×ln(200N)；解 2%×N = 1600×ln(200N) 得 N≈155 万 → 文档取整
160 万。即表小于 ~160 万行时，-2 折算行数 < 30000 下限 → **整表采样** → 比
30000 行默认采样慢。

## AWR/计划判读分诊表

| 症状 | 判定链位置 | 方向 |
|---|---|---|
| rows 估算稳定偏差（同一值） | 该值在/不在 MCV 边界滑移（数据漂移未 re-analyze） | re-analyze / 提高 target |
| 唯一索引等值查询估算也错 | 不是统计信息问题（链第 2 级恒准） | 查代价模型/索引选择 |
| 时间递增列新值估算崩 | 链第 4 级外推分支 | enable_extrapolation_stats（时间列） |
| 分布式 vs 单机估算不同 | 第 4 级两分支算法不同 | 环境 δ，非 bug |
| custom 快 generic 慢 | 选择率随参数摆动 → 代价比较（`query-plan-cache.md` 第 14 级）失真 | 绑定变量改写/force_custom |

## 常见错误

| 错误 | 纠正 |
|---|---|
| 行数偏差一律 re-analyze | 先分诊：unique 索引链第 2 级与统计无关 |
| 外推统计当普适开关 | 仅时间类+无倾斜+近零选择率场景触发 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）
- 源码精读（2026-10-25）：selfuncs.cpp:352-470（var_eq_const 全函数）、
  :166-206（直方图族函数声明）；二次补读：analyze.cpp:2471-2725
  （蓄水池主循环）、:5300-5545（MCV 提取+等频直方图构建）、:2202-2233
  （块采样 Algorithm S）
- 联动：`query-plan-cache.md`（第 14 级代价比较吃的就是这些估算）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
