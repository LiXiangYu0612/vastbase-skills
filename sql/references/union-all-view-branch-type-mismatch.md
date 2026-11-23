# UNION ALL 视图分支列类型不一致 → 整视图不可展平、外层谓词完全不推

本文件帮你处理：UNION ALL 大视图外面挂等值条件却推不进去（计划里出现 `Subquery Scan on <视图>`
+ 顶层 Filter + 全部分支全表扫）时，30 秒判定是不是"某个分支基表列类型与其他分支不一致"，
并给出不改动表结构的修复路径。典型案例：分支基表列是 int、其余分支是 numeric，且该列在视图里
是**别名**进来的（列名都不叫视图列名）。

## 机制（源码级，PG 树与 openGauss 树双侧引证）

三步链条，每步都有源码/实测锚点：

1. **解析器只记公共类型，不改写分支表达式**。UNION 类型解析对分支输出列只"验证可
   coercibility"，**不把 cast 插进分支 targetlist**——分支裸列保持原类型，解析出的公共类型
   只记在 `SetOperationStmt.colTypes`；视图列类型 = 公共类型。
   - PG 17：`src/backend/parser/analyze.c:2210-2229`（注释原文 "we don't modify the
     child's expression, for fear of changing the child query's semantics"）
   - openGauss 6.0.0：`src/common/backend/parser/analyze.cpp:4097`（同款注释）
   - 副作用：typmod 解析失败时视图列显示**裸 numeric**（无 typmod）。
2. **优化器展平闸门按"叶子实际输出类型 == 解析类型"判定**。视图子查询上拉
   （pull-up 成 appendrel，外层谓词才可能翻译进分支）要求 `is_simple_union_all`：每个叶子
   `tlist_same_datatypes(leaf.targetList, colTypes)` 必须全同（**比基类型，不比 typmod**）。
   有一个分支基类型不同 → 判 false → 视图**整体**不上拉 → 无 appendrel → 外层谓词无翻译通道，
   只能挂在 Subquery Scan 顶层当 Filter。
   - PG 17：`src/backend/optimizer/prep/prepjointree.c:2067-2069`（注释 "there can't be
     any datatype coercions involved, ie, all the leaf queries must emit the same
     datatypes"）+ `:2115`（判定行）
   - openGauss 6.0.0：`src/gausskernel/optimizer/prep/prepjointree.cpp:1820,1864`
3. **代价是全体分支的**：谓词在顶层过滤 → 所有分支（包括类型完全正常、有索引的）全部
   全表扫，行数整包上抛再滤掉。**不是只坏那一个分支**。

### 实测矩阵（2026-11-22，同脚本三引擎同形）

| 形态 | 视图列类型 | 计划 |
|---|---|---|
| 两支同型 numeric(20,0) | numeric(20,0) | Append → 两支 Index Cond（绿） |
| 一支 int 裸选（隐式解析） | **裸 numeric** | `Subquery Scan on <视图>` 顶层 Filter + 两支全表扫（红）；int 支再被包一层 `Subquery Scan on "*SELECT* n"` |
| 一支 int **显式写** `::numeric(20,0)` | numeric(20,0) | 可展平可推：正常支 Index Cond，cast 支 Filter（`col::numeric = const` 不可索引化） |
| 单表 cast 视图（无 union） | — | 谓词可推到子查询层，cast 列 Filter |

实测环境：PG 17.10（204 v5@9432）、VastBase 3.0.8（204 vastbase@5432）、VastBase 3.0.9
（204 vb309@6432），行为逐节点一致。

## 判据（30 秒定位）

1. **计划指纹**：`Subquery Scan on <视图>` + 顶层 `Filter: (<视图>.<列> = <常量>)` +
   `Rows Removed by Filter` 巨大 + Append 下全分支 Seq Scan。
2. **裸 numeric 信号**：视图列是裸 numeric 而基表都是 numeric(p,0)。注意：这只能证明
   "分支类型不齐"——**仅 typmod 不齐是无害的**（判定不比 typmod，照样展平下推），基类型
   不齐才致命。区分手段见下条。
3. **`\d` 与 `pg_get_viewdef()` 都不显示这个隐式 cast**（三引擎实测）——不能靠它们排除。
4. **定论手段 = 逐分支类型对账**（判"有没有"必须全量）：
   ```sql
   -- 视图列类型
   select attname, format_type(atttypid,atttypmod) from pg_attribute
   where attrelid='<视图>'::regclass and attnum>0 and not attisdropped;
   -- 每个 union 分支基表的源列类型（从 viewdef 逐支映射，注意别名）
   select c.relname, a.attname, format_type(a.atttypid,a.atttypmod)
   from pg_attribute a join pg_class c on a.attrelid=c.oid
   where c.relname in ('<分支基表...>') and a.attnum>0 and not a.attisdropped;
   ```
   任何"分支源列**基类型** ≠ 视图列基类型"= 元凶。
5. 分支被单独包 `Subquery Scan on "*SELECT* n"` 是线索不是铁证（带 join/子查询的分支本来
   也被包；但类型不一致的分支一定被包）。

## 触发条件

UNION/UNION ALL 视图 + 任一分支输出列基类型与其余分支不同（哪怕同族 int vs numeric）+
外层有该列谓词。分支列名与视图列名不一致（别名）时最隐蔽。

## 处置（按侵入性排序）

- **A 根治**：把不一致的基表列 ALTER 成与其余分支同基类型（typmod 无需强求一致）。
  注意：该列被视图引用时 ALTER COLUMN TYPE 会被依赖检查拒绝，需先删视图重建；列上若有
  PK/FK/索引/其他 int 侧 join 依赖，评估后再动。
- **B 不动表结构（推荐起步）**：视图该分支显式写 cast（`col::numeric`）→ 叶子输出类型
  与解析类型一致 → 整个视图恢复展平与下推：其余分支回到 Index Cond，cast 分支为 Filter。
  cast 分支的行如果也大，配 C。
- **C cast 分支也要索引**：建与 Filter 表达式**逐字一致**（含 typmod）的表达式索引
  `create index on t((col::numeric(20,0)))` → 该支走 Index/Bitmap Index Cond。
  实测 PG 17.10 与 VB 3.0.9 均生效（Bitmap Index Scan，Index Cond 命中 cast 表达式）。
  表达式不逐字匹配（如索引不带 typmod 而谓词带）则不命中。
- **D 应用侧**：绕开大视图按 table_name 直查对应基表（此类视图常带 table_name 列）。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 只看 `\d 视图` 列类型就说类型没问题 | 显示的是解析后公共类型，会掩盖分支差异 | 逐分支对源列做 format_type 对账 |
| 拿 `pg_get_viewdef` 找 cast | 隐式 cast 不打印（实测） | 对账基表列类型，不指望 viewdef |
| 按视图列名去基表查列类型 | 分支常用别名（`project_id AS order_id`），按名查=查空 | 从 viewdef 逐支映射真实源列名 |
| 以为只坏类型不一致那一支 | 判定闸门作用于整个视图，所有分支一起失去下推 | 预期修复收益按全部分支算 |

## 版本注记

行为实测：PG 17.10、VastBase 3.0.8、3.0.9（均 2026-11-22）。源码引证：PG 17.10 官方树 +
openGauss 6.0.0 树（本域内核基线）。更老版本未实测；闸门逻辑在 PG 9.5+ 一脉相承
（`tlist_same_datatypes` 判定历史悠久），判据通用。

## 来源

- 生产案例：hbwis/misdb `v_flow_auditing_order_list`（SQLHC `1838071829.sqlhc.small.log`，
  2026-11-22）——`Subquery Scan on l` 顶层 `Filter: (l.order_id = 35544979::numeric)`、
  Rows Removed by Filter **11,209,322**、31s；视图列 order_id = 裸 numeric；全日志列清单
  **唯一** int 列 = `mat_project_info.project_id (integer)`，经 `*SELECT* 21` 分支以别名进
  union（其余分支 order_id 全为 numeric 族）——用户先定位到"某分支变成 int"，本文补齐机制
  与全量对账。
- 红绿矩阵：204 三实例（v5@9432 / vastbase@5432 / vb309@6432），2026-11-22，脚本随文件
  （vc1~vc4 / vf2+vf3 形态）。
- 源码：PG 17.10 `analyze.c:2210-2229`、`prepjointree.c:2067-2069,2115`；openGauss 6.0.0
  `analyze.cpp:4097`、`prepjointree.cpp:1820,1864`。
