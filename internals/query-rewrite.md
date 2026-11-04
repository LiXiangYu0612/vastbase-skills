# 查询重写开关（rewrite_rule / sql_beta_feature 位掩码）

## 概述

> 服务分类：performance（计划突变/Plan Time 判读）、sql（查询转换开关地图）；
> 深度查询转换分析在 `../sql/optimizer-deep.md` 与 `references/` 深潜系列，本文件管**开关机制与位表**

两个 GUC 都是**位掩码**（`include/utils/guc.h:396-432`），每个选项一个位；
planner/rewriter 各处按位 gate（如 planner.cpp:1495 LAZY_AGG、:4076 PARTIAL_PUSH），
且可被 **hint 临时放行**（`permit_from_rewrite_hint`）。改一个选项 = 开/关一条
重写路径，计划形态可能整个变样——**计划突变类问题的第一排查位**
（重写发生在 parse 之后、plan 之前，管线位置见 `query-pipeline.md`）。

## rewrite_rule 位表（rewrite_param，guc.h:398-412）

| 选项 | 位 | 语义 |
|---|---|---|
| lazyagg | 0 | 允许惰性聚合（外层聚合下推/合并） |
| magicset | 1 | Magic Set 重写（子链接上拉时主查询条件下推进子查询，见下节） |
| partialpush | 2 | 部分下推 |
| sublink_pullup_with_unique_check | 3 | 子链接上拉（带唯一性检查） |
| sublink_pullup_disable_replicated | 4 | 禁止对复制表上拉子链接 |
| sublink_pullup_in_targetlist | 5 | 允许目标列中的子链接上拉 |
| predpushnormal / predpushforce | 7/8 | 谓词下推（普通/强制） |
| sublink_pullup_disable_expr | 9 | 禁止表达式中的子链接上拉 |
| **enable_any_sublink_pullup_enhanced** | 10 | 增强型 ANY 子链接上拉 |
| disablereusecte / removed_redundant_distinct_groupby | 11/12 | CTE 复用/冗余 DISTINCT-GROUP BY 消除 |

**现场实例**（2026-10 报告）：`rewrite_rule=magicset,enable_any_sublink_pullup_enhanced`
= 位 1+10，即 Magic Set 与增强 ANY 上拉开启。

## magicset 本体机制（2026-10-29 源码定论 + env-T 实测）

**rewrite_rule 默认值 = magicset（bit1 单开，默认启用）**（guc_sql.cpp:3098 默认
MAGIC_SET；文档同）。本体只有 1 条重写逻辑，gate 点仅 2 处，均在 expr 子链接
**上拉为 join 的过程中**（subselect.cpp）：

1. `convert_expr_subink_with_agg_targetlist`（:5837，gate :5876）——带聚合目标列的
   expr 子链接，原型 `t1.c < (select sum(t2.c) from t2 where t2.b = t1.b)`
2. `convert_expr_sublink_with_limit_clause`（:5640，gate :5679）——带 LIMIT 的
   expr 子链接

两处逻辑相同：子查询关联条件全为等值且 AND 连接（`get_pullUp_equal_expr`）、
jointree 无子计划 → `push_down_qual()`（:5502）：对关联键涉及的每个外层基表，
收集**只引用该表**的外层条件（非 volatile），`build_any_sublink()`（:5277）拼成
`内表键 = ANY(select 外表键 from 外表 where 外层条件)` 下推进子查询——
**先半连接收窄输入，再聚合/LIMIT**（经典 Magic Set）。

- env-T 真机（vastbase@5432）实测对比（ms_t1/ms_t2 各 1000 行，外层 filter b=10）：
  ON=子查询先 Hash Semi Join(x.a=t1.a 且 t1 过滤) → HashAggregate；OFF=全表
  HashAggregate 后再与外层 join
- 反例不改写（magic_set.out 2276 行回归）：外连接、子查询含 any_sublink、
  子查询含 left join；关联等值条件里引用了两个外层变量也不上拉
  （`bms_is_subset(level_up_varnos, available_rels)`，subselect.cpp:5659）
- 关联键 hash 化失败（`CanExprHashable`）或跨版本号过低时回退为不上拉
- 提示词级控制：`/*+ rewrite_rule(magicset) */`；会话级 `set rewrite_rule=none` 关

## sql_beta_feature 位表（sql_beta_param，guc.h:414-432，姊妹开关）

| 选项 | 位 | 语义（节选判读高频项） |
|---|---|---|
| sel_semi_poisson | 0 | 半连接选择性用泊松模型 |
| param_path_gen / opt | 2/4 | 参数化路径生成/优化 |
| rand_cost_opt | 3 | sc_random_page_cost 优化 |
| page_est_opt | 5 | 行存索引页估算更准 |
| partition_opfusion | 10 | 分区 opfusion 点查 |
| predpush_same_level | 13 | 同层谓词下推 |
| **partition_fdw_on** | 14 | 分区外表支持（标准表问题记录项） |
| disable_bitmap_cost_with_lossy_pages | 15 | lossy 页不计位图代价 |
| extract_pushdown_or_clause | 16 | 抽取 OR 限制条件下推 |

## 判读应用

1. **计划突变**：sysctl/参数段发现 rewrite_rule 或 sql_beta_feature 被改 →
   直接对照位表定位开/关了哪条重写路径（比 hint/环境差异先查——改动可见性高）
2. **Plan Time 高**：多位重写全开时上拉/下推组合爆炸是来源之一
3. **标准基线联动**：`admin/g100-standard-params.md` 地雷清单——标准表
   2026-06-24 从 rewrite_rule 移除 naaj_optimizer（按需配置项）
4. 逐位的精确重写代码路径：subselect.cpp / prepjointree.cpp / planner.cpp
   各 gate 点〔锚点级，未逐行核——遇到具体问题再深读对应位〕
5. **子查询内层 ORDER BY 的存亡 + 分页正确性**（2026-10-29 源码定论 +
   env-T vastbase 实测）：
   - SQL 语义：子查询的 ORDER BY 只约束自己那层，对外层**无任何顺序承诺**；
     计划里保留与否纯属改写路径差异，"合规"不等于"结果对"
   - 拉平守卫：`prepjointree.cpp:1755`（is_simple_subquery，PG12 同款
     prepjointree.c:1472）——带 sortClause 的子查询**拒绝拉平**，作为独立
     计划层保住 Sort（平凡 Subquery Scan 层执行时被消掉，Sort 直接挂父层）；
     连 `count(*) from (… order by …)` 这种无意义排序都保留
   - 但守卫只挡"直接子查询"：外层再包一层普通子查询（join 层）或 ROWNNUM
     外层引用，改写中内层 ORDER BY 照样被丢弃——`NUM<=20` 写在 ROWNNUM
     所在层外一层时，ROWNUM 跨层求值强制合并两层（planner.cpp:1806 注释：
     rownum assigned to tuples before filtered），合并吞掉有序子查询边界；
     `WHERE ROWNUM<=20` 与 ROWNUM 同层则 Sort 保留（preprownum.cpp:56
     守卫：所在层含 order by/group/offset 不改写）
   - **分页正确性**：`select * from (… order by …) a join … limit 20` 的
     Sort 若落在 hash join **build 边**，输出顺序跟 probe 边走——返回行
     不按序，翻页重叠/漏行。正解 = ORDER BY 与 LIMIT **同层**（env-T 实测
     Merge Left Join 双边有序）

## 基线差异记录

- `naaj_optimizer` 选项**不在开源 6.0.0 枚举**（rewrite_param 仅 12 位）——
  VastBase 定制分支选项，与 LogFileSync/XLogFlush 事件名同属基线差异类

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）
- 源码核对（2026-10-25）：guc.h:396-432 两张位表、planner.cpp:1495,4076 gate 样例
- 联动：`../admin/g100-standard-params.md`（地雷）、`../sql/optimizer-deep.md`（深潜）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
