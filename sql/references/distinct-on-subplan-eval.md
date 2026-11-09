# DISTINCT ON 查询里 targetlist 标量子查询的求值位置（VB 与 PG 相反）

**结论**：`SELECT DISTINCT ON(k) ..., (SELECT ...) FROM (SELECT ..., (SELECT ...) FROM ...joins...) sub ORDER BY k, v` 形状（HIS 类视图常见：内层子查询 targetlist 挂标量子查询、外层 DISTINCT ON 去重）中，标量子查询在哪层求值，两系引擎**相反**：

| 引擎 | 求值位置 | 执行次数 | 证据 |
|---|---|---|---|
| VastBase G100 3.0.9（openGauss 系） | Unique（去重）**之上**的 Result 节点 | = 去重后行数 | env204 vb309@6432，EXPLAIN ANALYZE 字典探测 `loops=2`（去重前 3 行） |
| PostgreSQL 17.10 | join/Sort 层（去重**前**） | = 去重前展开行数 | env204 v5@9432，`SubPlan ... loops=3` |

VB 计划形状：`Result(Output 含 (SubPlan)) ← Unique ← Sort ← joins`——Sort/Unique 只搬运裸列，子查询结果不进排序载荷。

## 改写含义（等价性/收益判定）

1. **子查询外提**（把标量子查询从内层子查询 targetlist 提到 DISTINCT ON 同层）：子查询 pull-up 后与原版是**同一棵查询树**，计划逐节点相同（实测 cost 一致）——在 VB 上**零性能收益**（原版已后置）；在原生 PG 上有真实收益（loops 从展开行数降到去重后行数）。
2. **DISTINCT ON 沉入子查询的双层改写**：两系都不改变子查询后置；但内层 DISTINCT ON 子查询不可 pull-up，形成屏障 → 优化器路径探索不同 → join 顺序可能不同 → **tie 行选择可能不同**，表现为"行数相同、部分列值不同"。无收益且有翻面风险。
3. **DISTINCT ON 的 tie**（同组排序键并列多行，来自 1:N 表扇出，如联系人/诊断×同一床位行）：保留哪行无 SQL 语义保证（计划产物）。要确定性必须在 ORDER BY 尾部补 tiebreaker，使排序键能唯一确定行。

## 诊断配方

```sql
explain (analyze, verbose) <DISTINCT ON 查询>;
```
- SubPlan 节点 `loops` = 标量子查询实际执行次数，对照去重前/后行数即知在哪层求值；
- SubPlan 挂载层级：`Result-above-Unique`（后置）vs join/Sort 的 Output（前置）；
- 附带看字典探测 Index Scan vs Seq Scan——`col::text = col::text` 双侧转换在列类型不一致时废掉索引，18 个字典探测 × 全表扫是这类视图的常见真瓶颈。

## 来源

2026-11-08 实测：env204 双实例（vb309@6432 = VastBase 3.0.9；v5@9432 = PG17.10），最小复现（2 病人/1 无床位 2 联系人 tie 组）；起因 = hihis.v_his_pats_in_hospital 视图等价改写争议（客户生产 = **VB 3.0.8**，用户 2026-11-08 晚更正）。loops 定论证据出自 3.0.9；3.0.8 未直接实测 loops（手头无 3.0.8 实例：204/156=3.0.9、116=2.2.10），判据仍用本文件配方；3.0.8 侧仅客户回告"原版与改写均走 SubPlan、无性能差"作旁证。

加固轮（同日，全部指向同一结论——VB 侧子查询后置）：200 病人 × 展开 3800 行（SubPlan `loops=201`=去重后）；`query_dop=2`（SMP）不变；含 e 表 + 外层 WHERE 的完全同构形状（Sort rows=2540 → Unique rows=135 → SubPlan `loops=135`）；`rewrite_rule` 无 `disablepullup` 位（3.0.9 报 invalid value）。客户侧实测回告同日确认：原版与改写均走 SubPlan，无性能差——结构改写在此形状上无收益。

**判读陷阱**：EXPLAIN 输出里 `SubPlan 1` 段打印在计划末尾、缩进最深，视觉上像"在最底层执行"，但它归属**最顶层 Result 节点**的投影；不带 ANALYZE 时没有 loops，凭缩进判位置必错。唯一可靠判据 = `explain (analyze)` 里 SubPlan 的 `loops` 对比 Unique/Sort 的 actual rows。
