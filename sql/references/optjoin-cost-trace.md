# OPT/OPT_JOIN 代价日志判读手册（逐 path 复算方法 + 实测样本）

## 概述

> 服务分类：sql（优化器决策复算）。输入=pglog 里的 `[OPT]/[OPT_JOIN] DEBUG`
> 行 + pg_stats 统计；输出=每个 path 的 sel/rows/cost 复算表与选择过程。
> 采样配方：会话 `SET logging_module='on(OPT,OPT_JOIN)'; SET log_min_messages=debug4;`
> （debug2 已够主链，debug4 额外给逐子句 s1；用完 RESET，防日志爆量）。
> 本手册全部数字来自 env-T vb309（V3.0 Build 9 **patch:1**）实测样本
> （2026-11-04，fixture 见 `evals/fixtures/optjoin_309psu1/`）。

## 输出行格式 → 计算点对照（file:line 实锚）

| 日志行 | 源码 | 算什么 |
|---|---|---|
| `A new path is accepted/not accepted with cost = S..T; rows = N` | pathnode.cpp:1867/:1849 | add_path 支配比较结论（N=该 path 的 rows） |
| `An old path is removed with cost = …` | pathnode.cpp | 新 path 支配掉旧 path |
| `--Precheck drop new path: startup_cost=…; total_cost=…` | pathnode.cpp:1937 add_path_precheck | 廉价预筛丢弃（见选择规则） |
| `The detail information of the new path:`（多行缩进树） | optimizerdebug.cpp:359/447/536 | path 树：pathtype/relids/rows/multiple/每条 restrict clause 的 norm_selec·outer_selec |
| `rel: (b N) / joinrel: (b N M)` + `cheapest startup/total: S, T` | 同上 | 单表/join 后的最便宜路径快照 |
| `-----------------------Try to join these rels---…` | joinpath.cpp:92 | 一对 join 方向开始 |
| `Initial nestloop cost: S, T` / `final cost nest loop: …` | costsize.cpp:3064/:3176 | NL 初估/终估 |
| `Initial mergejoin cost` / `final cost merge join: stream_cost/startup/total` | costsize.cpp:3416/:3681 | MJ（initial 的 startup 含排序成本；final 含重扫） |
| `outer: S,T` `inner: S,T` `Source data cost` `Add hash function cost` `Initial hashjoin cost: …, work_mem, esti_work_mem` | costsize.cpp:3785-3924 | HJ 分解：内外成本→建哈希→初估 |
| `hashjointuples=N, outer_tuples, inner_tuples, …, selec=S, multiple` | costsize.cpp | **join 结果行数与选择率直接打印**（rows=outer×inner×selec） |
| `[ES]…` `thisbucketsize/innerdistinct` `outerdistinct, outer_scan_ratio` `innerbucketsize` | costsize.cpp:4041-4145 compute_bucket_size | ES 等价类桶大小：innerbucketsize=1/virtualbuckets 缩放；outer_scan_ratio=clamp(innerdistinct/outerdistinct,1) |
| `Add cpu cost` / `Add restriction clauses cost` / `Add cpu cost: stream_cost…total_cost` | costsize.cpp:4230 区 | 桶扫描 cpu→join 结果 cpu（hashjointuples×cpu_tuple）→终值 |
| `The origin distinct value is N. After multiply by selectivity…` / `After using possion model with ntuples…ration…` | selfuncs/ES | 过滤后 ndistinct 修正（乘 sel / Poisson 模型） |
| `To estimate local numdistinct…relrows/reltuples` / `ndistinct, global_ndistinct, avgfreq, mcvfreq, estfract` / `rows=, tuples=, multiple=` | selfuncs（estimate_array_length 族） | ndistinct 缩放与 ES 判定链 |
| `Computing IndexScanCost: tuples_fetched/indexSelectivity/indexTotalCost` + `max/min_pages_fetched, IO_cost` + `csquared, IO_run_cost` + `cpu_per_tuple` + `IndexScan Cost startup/total, pages_fetched` | costsize.cpp cost_index | **Mackert-Lohman 相关性模型**（见下公式） |
| `clause_selectivity: s1 %f` | clausesel.cpp:788（DEBUG4） | 单条子句选择率 |

## 计算公式（与样本对账）

**常量**（本实例）：seq_page_cost=1，random_page_cost=1.1，cpu_tuple_cost=0.01，
cpu_index_tuple_cost=0.005，cpu_operator_cost=0.0025。

**Scan**：
- SeqScan total = relpages×seq + reltuples×cpu_tuple（+Filter 每行 cpu_operator）
  样本：t_opt2 = 4×1+500×0.01 = **9.00**；t_opt1(Filter city) = 7×1+1000×0.01+1000×0.0025 = **19.50**，rows = 1000×0.5(MCV BJ) = **500**
- IndexScan（Mackert-Lohman）：pages_fetched 按 T=relpages 与 sel 的相关曲线；
  max_IO = pages×random；min_IO = (pages−1)×seq+random；
  IO_run = max_IO + csquared×(min_IO−max_IO)；total = indexTotalCost + IO_run + tuples×cpu_per_tuple
  样本全扫：4.4/4.1/0.011664 → 4.3965；+7.15+5 = **16.5465**
  样本参数化（sel=0.02 MCV，loop>1 摊销）：0.3338+0.0088+0.125 = **0.4676**

**join sel 与 rows（ES 等价类）**：均匀分布双侧 ndistinct 相等时
selec = 1/max(nd1,nd2) = 1/50 = **0.02**（日志 `selec=0.0199999995` 直印）；
hashjointuples = outer×inner×selec = 500×500×0.02 = **5000**（实际 count=5000）。
过滤侧 ndistinct 修正：乘 sel（k 与 city 独立 → 50×1）或 Poisson 模型
（ntuples=1000, ratio=0.5 → 49.9977）；哈希内桶内侧再按 relrows/reltuples
缩放（50×500/1000=25 → innerbucketsize=0.04）。

**Join 成本分解（样本 HJ，A 外 B 内建哈希）**：
| 步骤 | 日志值 | 公式 |
|---|---|---|
| Source data | startup 9.00 / run 19.50 | startup=inner total；run=outer total |
| Add hash function | **15.25** / 20.75 | startup += (cpu_op×子句数+cpu_tuple+allocate_mem)×inner行 = (0.0025+0.01)×500；run += cpu_op×子句数×outer行 = 0.0025×500 |
| Initial hashjoin | 15.25..36.00 | startup+run 累计 |
| 桶参数 | 0.02/50/200/**0.25** | innerbucketsize=1/50；outer_scan_ratio=innerdistinct/outerdistinct=50/200 |
| Add cpu | run 22.3125 | 桶扫描项（源码 0.05 系数公式） |
| Add restriction clauses | run 72.3125 | +hashjointuples×cpu_tuple = 5000×0.01 |
| **final** | **15.25..87.5625** | startup+run |

NL：total = outer + outer行×inner成本。样本 19.5+500×0.4676=**253.30**；
19.5+500×9=**4519.50**；反向 9+500×19.5=**9759**。
MJ：initial startup 含两侧排序（73.33），final 加重扫/输出 cpu → **150.83**。

## 选择规则（add_path，pathnode.cpp）

1. **Precheck 预筛**（:1937）：新 total ≥ 在场某 path total 且（新 startup ≥ 对方
   startup **或 新 path 是参数化路径**）且 pathkeys 不更优且参数化同型 → 直接丢
   （不建节点）。样本 7 个 NL 变体全部死在这。
2. **add_path 支配比较**（:1849/:1867）：对 pathlist 逐个比 (startup, total,
   pathkeys, 参数化, rows)；一方双指标都不劣且有一项严格优 → 支配。双向都不
   支配 → 共存；**总代价差落在模糊因子内（样本 87.55 vs 87.5625，差 0.014%）
   保旧路径**——反向 HJ 就这样输的。
3. 挤掉：新 path 支配旧 → `An old path is removed`（样本 HJ 挤掉 MJ）。
4. joinrel 终态 `cheapest startup/total`（样本 15.25..**87.5625**）= 最终计划
   的 cost（与 EXPLAIN 一致）。

## 整理 SOP（拿到日志+统计后的五步）

1. 切片：按 query_id 或时间戳圈一次查询的 `[OPT]/[OPT_JOIN]` 行
2. 基表段：每个 rel 的 accepted/not accepted path（cost+rows）→ 用 relpages/
   reltuples/MCV 复算 seq 与 index 成本
3. join 段：每个 `Try to join` 方向列 MJ/NL/HJ 的 initial→final；用
   `hashjointuples/selec` 行直取 rows，用 MCV/ndistinct 复算 selec
4. 选择段：precheck drop 清单 + accepted/removed/not accepted 序列 → 还原
   add_path 支配过程
5. 对账：joinrel cheapest vs EXPLAIN cost；hashjointuples vs 实际行数

## 常见错误

| 错误 | 纠正 |
|---|---|
| 只看 total 不看 startup | add_path 是双指标支配，反向 HJ 输在 startup（87.55<87.5625 仍被拒） |
| 把 initial cost 当最终 | MJ initial 75.83 → final 150.83（重扫+输出）；HJ initial 36 → 87.56 |
| 忽略 Poisson/relrows 的 ndistinct 修正 | 过滤后 ndistinct 变（50→25 决定 innerbucketsize 0.04） |
| 忘了核 enable_* 开关与实例版本 | 样本实例 enable_nestloop=off；309 PSU1 起 mergejoin 估价快照已改型（见 mergejoin-cost-estimation-risk.md） |

## 版本注记

- 实测环境：env-T vb309 用户（V3.0 Build 9 patch:1，openGauss 6.0.0 基线同源）；
  源码锚点取自 openGauss 6.0.0 树，与该实例同代
- DEBUG 级别：主链 DEBUG1/DEBUG2，逐子句 s1 需 DEBUG4；log 行格式带
  `[模块名] DEBUG:` 前缀与 query_id

## 来源

- 源码（2026-11-04 核实）：pathnode.cpp:1849/1867/1906-1975（add_path/
  precheck 与输出）、joinpath.cpp:92/603-616（Try to join/precheck 调用）、
  costsize.cpp:3064/3176/3416/3681/3785-3924/4041-4145/4230 区（NL/MJ/HJ
  全部打印点）、optimizerdebug.cpp:359/447/536、clausesel.cpp:788
- 实测样本：evals/fixtures/optjoin_309psu1/（137 行日志+统计快照+建表 SQL+
  EXPLAIN，数字全部逐位对账）
- 关联：`../../internals/query-plan-cache.md`（PBE 下这些 path 何时重算）、
  `../../internals/mergejoin-cost-estimation-risk.md`（MJ 估价风险）、
  `../../internals/debug-logging-gucs.md`（采样配方与噪声纪律）、
  `../../internals/stats-to-cost.md`（sel 判定链）、`../optimizer-deep.md`（公式总纲）

## 知识网

本节点的关系与导航路径见 `../../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
