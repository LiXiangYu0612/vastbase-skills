# VastBase/openGauss 查询转换机制深度分析

> 学习时间: 2026-04-16  
> 源码版本: openGauss-server-v6.0.0  
> 分析深度: 源码级别  
> 状态: 理论学习 + 源码分析 + 实践验证

## 目录
1. [查询转换概述](#查询转换概述)
2. [Sublink提升机制](#sublink提升机制)
3. [Subquery提升机制](#subquery提升机制)
4. [谓词推入](#谓词推入)
5. [视图展开](#视图展开)
6. [表达式优化](#表达式优化)
7. [常量表达式折叠](#常量表达式折叠)

---

## 查询转换概述

### 转换层次结构

```
SQL查询
   ↓
Parser (解析器) → Parse Tree
   ↓
Analyzer (分析器) → Query Tree  
   ↓
Rewrite (重写器) ← 查询转换主要发生地
   ├─ Rule System (规则系统)
   ├─ View Expansion (视图展开)
   └─ RLS Policy (行级安全策略)
   ↓
Prep (预处理)
   ├─ Sublink Pullup (子链接提升)
   ├─ Subquery Pullup (子查询提升)
   ├─ Predicate Pushdown (谓词推入)
   └─ Constant Folding (常量折叠)
   ↓
Planner (优化器) → Optimized Plan
```

### 核心源码模块

```
src/gausskernel/optimizer/
├── rewrite/           # 查询重写模块
│   ├── rewriteHandler.cpp    (5301行, 主要重写逻辑)
│   ├── rewriteManip.cpp      # 表达式操作
│   ├── rewriteDefine.cpp     # 规则定义
│   └── rewriteRlsPolicy.cpp  # RLS策略
│
├── prep/              # 查询预处理模块  
│   ├── prepjointree.cpp      (3881行, 连接树预处理)
│   ├── prepqual.cpp          (659行, 条件表达式预处理)
│   ├── preptlist.cpp         # 目标列表预处理
│   ├── prepunion.cpp         # UNION处理
│   ├── prepnonjointree.cpp   # 非连接树预处理
│   └── preprownum.cpp        # ROWNUM处理
│
└── plan/              # 计划生成模块
    └── subselect.cpp   # 子查询处理
```

---

## Sublink提升机制

### 核心概念

**Sublink** 是查询树中表示子查询表达式的节点类型，通常出现在WHERE子句、SELECT列表等位置。

#### Sublink类型定义 (从源码提取)

```cpp
// src/include/nodes/parsenodes.h
typedef enum SubLinkType {
    EXISTS_SUBLINK,      // EXISTS子查询
    ALL_SUBLINK,         // quantified ALL subquery
    ANY_SUBLINK,         // quantified ANY subquery  
    ROWCOMPARE_SUBLINK,  // row-wise comparison
    EXPR_SUBLINK,        // 无相关性的标量子查询
    ARRAY_SUBLINK        // ARRAY(...) subquery
} SubLinkType;

typedef struct SubLink {
    NodeTag     type;
    SubLinkType subLinkType;     // 子链接类型
    Node*       testexpr;        // 外层表达式,用于ANY/ALL/ROWCOMPARE
    List*       operName;        // 操作符名称
    Node*       lefthand;        // 左操作数
    Node*       subselect;       // 子查询的Query节点
    int         location;        // token位置
} SubLink;
```

### Sublink提升策略

#### 1. EXISTS_SUBLINK 提升

**适用场景**:
- EXISTS子查询可以转换为SEMI JOIN
- 消除子查询，提高join效率

**转换示例**:

```sql
-- 转换前 (Sublink形式)
SELECT * FROM orders o
WHERE EXISTS (SELECT 1 FROM customers c 
              WHERE c.id = o.customer_id 
              AND c.country = 'CN');

-- 转换后 (SEMI JOIN)
SELECT o.* FROM orders o
SEMI JOIN customers c 
  ON c.id = o.customer_id 
 WHERE c.country = 'CN';
```

**源码位置**: `src/gausskernel/optimizer/prep/prepjointree.cpp`
**核心函数**: `pull_up_sublinks()`

**提升条件**:
1. 子查询不包含聚合函数
2. 子查询不包含GROUP BY/HAVING
3. 子查询不包含LIMIT/OFFSET
4. 子查询只返回一列(或EXISTS)
5. 相关性条件可以直接转换为JOIN条件

#### 2. EXPR_SUBLINK (标量子查询) 提升

**适用场景**:
- 无相关性的标量子查询
- 可以提前计算，常量折叠

**转换示例**:

```sql
-- 转换前
SELECT id, (SELECT MAX(amount) FROM orders WHERE customer_id = 10) 
FROM customers 
WHERE id = 10;

-- 转换后 (子查询独立执行)
SELECT id, max_amount 
FROM customers, 
     (SELECT MAX(amount) AS max_amount FROM orders WHERE customer_id = 10) subq
WHERE id = 10;
```

**提升限制**:
- 如果子查询结果超过1行，会报错
- 标量子查询通常无法提升，需要特殊处理

#### 3. ANY_SUBLINK / ALL_SUBLINK 提升

**适用场景**:
- 可以转换为JOIN或Semi/Anti Join
- `WHERE col > ANY (subquery)` → JOIN + 条件
- `WHERE col > ALL (subquery)` → 聚集子查询

**转换示例**:

```sql
-- ANY转换
-- 转换前
SELECT * FROM products p
WHERE p.price > ANY (SELECT avg_price FROM category_prices);

-- 转换后 (SEMI JOIN)
SELECT DISTINCT p.* FROM products p
JOIN category_prices cp ON p.price > cp.avg_price;

-- ALL转换  
-- 转换前
SELECT * FROM products p
WHERE p.price > ALL (SELECT price FROM competitor_prices);

-- 转换后 (聚合子查询)
SELECT * FROM products p
WHERE p.price > (SELECT MAX(price) FROM competitor_prices);
```

### Sublink提升的核心算法

**源码位置**: `prepjointree.cpp:pull_up_sublinks_jointree_recurse()`

**算法流程**:

```cpp
// 伪代码逻辑
Node* pull_up_sublinks_jointree_recurse(PlannerInfo* root, Node* jtnode, ...) {
    // 1. 遍历连接树，寻找可提升的Sublink
    if (IsA(jtnode, JoinExpr)) {
        // 处理JOIN节点
        JoinExpr* j = (JoinExpr*)jtnode;
        j->larg = pull_up_sublinks_jointree_recurse(root, j->larg, ...);
        j->rarg = pull_up_sublinks_jointree_recurse(root, j->rarg, ...);
        
        // 尝试从quals中提取Sublink
        if (j->quals) {
            j->quals = pull_up_sublinks_qual_recurse(root, j->quals, ...);
        }
    } else if (IsA(jtnode, RangeTblRef)) {
        // 基表引用，无需处理
    }
    
    return jtnode;
}

Node* pull_up_sublinks_qual_recurse(PlannerInfo* root, Node* node, ...) {
    // 2. 递归查找表达式中的Sublink
    if (IsA(node, SubLink)) {
        SubLink* sublink = (SubLink*)node;
        
        // 3. 检查是否可提升
        if (convert_testexpr_needed) {
            // 转换为JOIN形式
            return convert_sublink_to_join(sublink, available_rels);
        } else {
            // 不可提升，保持原样
            return node;
        }
    }
    
    // 4. 递归处理表达式树的其他节点
    return expression_tree_mutator(node, pull_up_sublinks_qual_recurse, context);
}
```

### 实践验证示例

```sql
-- 测试表
CREATE TABLE customers (
    id INT PRIMARY KEY,
    name VARCHAR(100),
    country VARCHAR(50)
);
CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT,
    amount NUMERIC(10,2),
    order_date DATE
);

INSERT INTO customers VALUES 
    (1, 'Alice', 'CN'), (2, 'Bob', 'US'), (3, 'Charlie', 'CN');
INSERT INTO orders VALUES
    (1, 1, 100.00, '2024-01-01'),
    (2, 1, 200.00, '2024-01-15'),
    (3, 2, 150.00, '2024-01-10');

-- EXISTS提升测试
EXPLAIN 
SELECT * FROM customers c
WHERE EXISTS (SELECT 1 FROM orders o 
              WHERE o.customer_id = c.id 
              AND o.amount > 100);

-- 预期结果: 应该看到 Hash Semi Join 或者类似的Semi Join执行计划
-- 未提升前: 子查询独立执行，可能效率较低
-- 提升后:   SEMI JOIN，一次扫描完成

-- 标量子查询测试
EXPLAIN  
SELECT c.name, 
       (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count
FROM customers c;

-- 观察执行计划，看是否提升为JOIN
-- 标量子查询通常不会提升，而是通过InitPlan执行
```

---

## Subquery提升机制

### 核心概念

**Subquery (子查询)** 指出现在FROM子句中的查询，也称为**Derived Table**或**Subquery in FROM clause**。

#### Subquery提升的类型

1. **简单子查询提升** (`pull_up_simple_subquery`)
   - 不包含聚合、GROUP BY、HAVING
   - 不包含LIMIT/OFFSET  
   - 不包含集合操作(UNION等)
   - 不包含OUTER JOIN的相关性

2. **UNION ALL提升** (`pull_up_simple_union_all`)
   - 纯UNION ALL查询
   - 可以拆分为多个独立的查询

3. **LATERAL子查询提升**
   - LATERAL子查询特殊处理
   - 依赖于前面表的相关性

### Subquery提升的条件

**源码位置**: `prepjointree.cpp:is_simple_subquery()`

```cpp
static bool is_simple_subquery(Query* subquery, RangeTblEntry* rte, 
                               JoinExpr* lowest_outer_join)
{
    // 检查1: 不包含聚合
    if (subquery->hasAggs)
        return false;
    
    // 检查2: 不包含窗口函数
    if (subquery->hasWindowFuncs)
        return false;
    
    // 检查3: 不包含GROUP BY / HAVING
    if (subquery->groupClause || subquery->havingQual)
        return false;
    
    // 检查4: 不包含LIMIT/OFFSET
    if (subquery->limitCount || subquery->limitOffset)
        return false;
    
    // 检查5: 不包含集合操作
    if (subquery->setOperations)
        return false;
    
    // 检查6: 不包含CTE (WITH子句)
    if (subquery->cteList)
        return false;
    
    // 检查7: 不包含DISTINCT (可能转换)
    if (subquery->distinctClause)
        return false;
    
    // 检查8: 目标列表中不能包含 volatile 函数
    // ... 省略详细检查
    
    return true;
}
```

### Subquery提升示例

#### 场景1: 简单子查询提升

```sql
-- 转换前
SELECT c.name, o.order_count
FROM customers c,
     (SELECT customer_id, COUNT(*) AS order_count
      FROM orders
      GROUP BY customer_id) o
WHERE c.id = o.customer_id;

-- 转换后 (子查询提升失败，因为有GROUP BY)
-- 保持不变

-- 另一个例子
-- 转换前
SELECT c.name, o.amount
FROM customers c,
     (SELECT customer_id, amount 
      FROM orders
      WHERE amount > 100) o
WHERE c.id = o.customer_id;

-- 转换后 (提升成功，简单子查询)
SELECT c.name, o.amount
FROM customers c
JOIN orders o ON c.id = o.customer_id
WHERE o.amount > 100;
```

#### 场景2: UNION ALL提升

```sql
-- 转换前
SELECT * FROM (
    SELECT id, name FROM customers_cn
    UNION ALL
    SELECT id, name FROM customers_us
) combined_customers;

-- 转换后 (拆分为两个查询)
-- 优化器可以为每个分支选择最优计划
```

#### 场景3: LATERAL子查询处理

```sql
-- LATERAL子查询不能随意提升
-- 转换前 (已经是JOIN形式，无法进一步提升)
SELECT c.name, o.order_stats
FROM customers c
LEFT JOIN LATERAL (
    SELECT customer_id, 
           COUNT(*) AS total_orders,
           SUM(amount) AS total_amount
    FROM orders o
    WHERE o.customer_id = c.id
) o ON true;
```

### Subquery提升的限制

1. **聚合函数**: 包含GROUP BY的子查询不能提升
2. **DISTINCT**: 可能影响语义，通常不提升
3. **LIMIT/OFFSET**: 改变行数，不能提升
4. **OUTER JOIN的相关性**: 复杂相关性，提升困难
5. **集合操作**: UNION/INTERSECT/EXCEPT通常不提升

### 实践验证

```sql
-- 测试表
CREATE TABLE customers_cn AS 
    SELECT 1 AS id, 'Alice_CN' AS name
    UNION ALL
    SELECT 2, 'Bob_CN';

CREATE TABLE customers_us AS
    SELECT 3 AS id, 'Charlie_US' AS name
    UNION ALL  
    SELECT 4, 'David_US';

-- 简单子查询提升测试
EXPLAIN
SELECT * FROM customers,
     (SELECT id, name FROM customers_cn) cn
WHERE customers.id = cn.id;

-- 预期: 可以看到子查询被提升为 JOIN

-- UNION ALL提升测试
EXPLAIN
SELECT * FROM (
    SELECT id, name FROM customers_cn
    UNION ALL
    SELECT id, name FROM customers_us
) combined;

-- 预期: 可以看到 Append 节点，表示UNION ALL被优化
```

---

## 谓词推入 (Predicate Pushdown)

### 核心概念

**谓词推入** 将过滤条件尽可能推到查询树的底部，尽早过滤数据，减少后续处理的数据量。

### 谓词推入的类型

1. **简单谓词推入**: 直接下推到基表
2. **JOIN谓词推入**: 推过JOIN节点
3. **视图谓词推入**: 推入视图内部
4. **UNION谓词推入**: 推入UNION各个分支

### 源码分析

**核心函数**: `prepqual.cpp` 中的条件表达式处理

主要操作:
1. **规范化表达式**: 将AND/OR转换为平坦形式
2. **常量折叠**: 预计算常量表达式
3. **子查询平化**: 展开嵌套的AND/OR
4. **NOT下推**: 德摩根定律转换

```cpp
// 示例: NOT (A AND B) → (NOT A) OR (NOT B)
Node* negate_clause(Node* node) {
    switch (nodeTag(node)) {
        case T_BoolExpr: {
            BoolExpr* expr = (BoolExpr*)node;
            if (expr->boolop == AND_EXPR) {
                // 应用德摩根定律: NOT (A AND B) → (NOT A) OR (NOT B)
                return make_orclause(negate_clause(expr->args));
            } else if (expr->boolop == OR_EXPR) {
                // NOT (A OR B) → (NOT A) AND (NOT B)
                return make_andclause(negate_clause(expr->args));
            }
        }
        // ... 其他情况
    }
}
```

### 谓词推入示例

#### 场景1: 基本谓词推入

```sql
-- 转换前
SELECT * FROM (
    SELECT * FROM orders
) sub
WHERE amount > 100;

-- 转换后 (谓词推入 + 子查询提升合并)
SELECT * FROM orders
WHERE amount > 100;
```

**EXPLAIN 形态对比** (示意):

```sql
-- 不可推入时 (例如子查询带 OFFSET, 见场景5): 谓词留在上层 Subquery Scan
EXPLAIN SELECT * FROM (
    SELECT * FROM orders OFFSET 0
) sub WHERE amount > 100;

 Subquery Scan on sub
   Filter: (sub.amount > '100'::numeric)
   ->  Seq Scan on orders

-- 可推入时: 谓词直接落到基表扫描节点上
EXPLAIN SELECT * FROM (
    SELECT * FROM orders
) sub WHERE amount > 100;

 Seq Scan on orders
   Filter: (amount > '100'::numeric)
```

#### 场景2: 聚合子查询推入 (推入 HAVING)

外层谓词引用聚合输出列时, 谓词被推入子查询的 HAVING 而不是 WHERE——因为该谓词语义上作用于分组结果行。

```sql
-- 转换前
SELECT * FROM (
    SELECT customer_id, SUM(amount) AS total
    FROM orders
    GROUP BY customer_id
) t
WHERE total > 500;

-- 推入后 (等价形态)
SELECT customer_id, SUM(amount) AS total
FROM orders
GROUP BY customer_id
HAVING SUM(amount) > 500;
```

**EXPLAIN 形态** (示意): Filter 出现在聚合节点上, 而不是上层扫描之后再过滤——先聚合后过滤变成聚合内部过滤, 通常行数在聚合阶段就被砍掉:

```sql
 HashAggregate
   Filter: (sum(amount) > '500'::numeric)
   ->  Seq Scan on orders
```

#### 场景3: UNION 谓词推入

子查询含集合操作时, 谓词被复制后**分别推入每个分支**。

```sql
-- 转换前
SELECT * FROM (
    SELECT id, amount FROM orders_2023
    UNION ALL
    SELECT id, amount FROM orders_2024
) history
WHERE amount > 100;

-- 推入后 (每个分支各得一份谓词副本)
SELECT id, amount FROM orders_2023 WHERE amount > 100
UNION ALL
SELECT id, amount FROM orders_2024 WHERE amount > 100;
```

**EXPLAIN 形态** (示意): 每个 Append 分支自带 Filter, 而不是 Append 之上统一过滤:

```sql
 Append
   ->  Seq Scan on orders_2023
         Filter: (amount > '100'::numeric)
   ->  Seq Scan on orders_2024
         Filter: (amount > '100'::numeric)
```

#### 场景4: security_barrier 视图的阻断

带 `security_barrier` 选项的视图 (以及行级安全策略注入的条件) 出于防信息泄露考虑, 对**含泄露型函数 (leaky functions)** 的谓词禁止推入; 不含泄露型函数的普通谓词仍可推入。

```sql
CREATE VIEW v_sec WITH (security_barrier) AS
    SELECT * FROM orders;

-- amount > 100 不含泄露型函数, 可以推入
SELECT * FROM v_sec WHERE amount > 100;

-- 谓词里出现用户自定义函数时, 视为潜在泄露型, 不推入
SELECT * FROM v_sec WHERE my_func(amount) > 100;
```

#### 场景5: 不可推入的典型形态

- 子查询带 **LIMIT/OFFSET**: 推入会改变返回行集 (即使 `OFFSET 0` 也一概拒绝)
- 子查询含**窗口函数**: 过滤发生在窗口计算之后, 推入改变窗口帧
- 子查询含 **EXCEPT/EXCEPT ALL**: 左右分支推入不等价
- 谓词含 **volatile 函数**: 多次求值结果不稳定
- 子查询含 **ROWNUM** (openGauss/VastBase 兼容语法): ROWNUM 与过滤顺序强相关
- 谓词引用的输出列在 `unsafeColumns[]` 中被标记为不安全 (如 DISTINCT ON 的非保留列)

### 源码走读: 真实执行链

openGauss 6.0.0 中, 面向子查询的谓词推入**不在 prep 阶段**, 而发生在**路径生成阶段**——为 RTE_SUBQUERY 构建访问路径时执行。文件为 `src/gausskernel/optimizer/path/allpaths.cpp` (概述一节流程图中归入 Prep 是简化画法)。

**调用链**:

```
set_subquery_pathlist()                     (allpaths.cpp)
    ├─ copyObject(subquery)                 -- 在副本上推入, 不改写原查询树
    ├─ subquery_is_pushdown_safe()          -- 整个子查询级安全检查
    │    ├─ recurse_pushdown_safe()         -- 递归集合操作树各分支
    │    ├─ check_output_expressions()      -- 标记 unsafeColumns[]: 集合函数/
    │    │                                     volatile/DISTINCT ON 非保留列
    │    └─ compare_tlist_datatypes()       -- 集合操作分支输出类型对账
    ├─ [rewrite_rule 开启时] judge_predpush_subquery()
    │    └─ predpush_subquery()             -- 增强推入 (见下节)
    └─ 对 rel->baserestrictinfo 逐条:
         can_push_qual_into_subquery()      -- 单条谓词级检查
              ├─ rinfo->pseudoconstant      -- 伪常量留在上层 gating 节点
              ├─ rte->security_barrier
              │   && contain_leaky_functions(clause)
              ├─ qual_is_pushdown_safe()    -- 含子计划/volatile/EXCEPT/PHV 检查
              ├─ qual_pushdown_in_partialpush()
              └─ has_rownum(subquery)
              ├─ 通过 → subquery_push_qual()  -- 执行推入
              └─ 不通过 → 留在 rel->baserestrictinfo,
                          执行期作为 SubqueryScan 的 qpquals
```

**subquery_push_qual()** (allpaths.cpp) 的三个关键动作:

```cpp
// 伪代码逻辑 (依据源码整理)
static void subquery_push_qual(Query* subquery, RangeTblEntry* rte,
                               Index rti, Node* qual, int levelsup)
{
    if (subquery->setOperations != NULL) {
        // 集合操作: 递归推入每个分支, 每个分支得到独立副本
        recurse_push_qual(subquery->setOperations, subquery, rte, rti, qual, levelsup);
    } else {
        // 1. 变量替换: 外层对子查询输出列的引用 (Var)
        //    替换为子查询 targetlist 中的实际表达式
        //    (ReplaceVarsFromTargetlist; predpush 路径走 trans_lateral_vars)
        qual = replace_vars_with_tlist_exprs(qual);

        // 2. 挂接位置: 有聚合/GROUP BY/HAVING → HAVING; 否则 → WHERE
        if (subquery->hasAggs || subquery->groupClause || subquery->havingQual)
            subquery->havingQual = make_and_qual(subquery->havingQual, qual);
        else
            subquery->jointree->quals = make_and_qual(subquery->jointree->quals, qual);
    }
}
```

两个容易忽略的细节:

1. **推在副本上**: `set_subquery_pathlist` 先 `copyObject(subquery)` 再推入, 不同计划候选共享同一子查询 RTE 时互不污染。
2. **HAVING 判定以分组存在性为准**: 即使外层写的是 WHERE, 只要子查询有 GROUP BY, 推入目标就是 HAVING——这正是场景2 的机制来源。

### predpush: rewrite_rule 控制的增强推入

openGauss 在通用推入之外提供了一族把**连接谓词**也参数化推入子查询的能力, 由 `rewrite_rule` 开关控制 (GUC 定义见 `guc.h: rewrite_param` 枚举, 选项名见 `guc_sql.cpp: rewrite_options`):

| rewrite_rule 取值 | 行为 |
|---|---|
| `predpush` | 基础增强推入, 连接谓词以 Param 形式注入子查询 |
| `predpushnormal` | 推入后为子查询生成参数化路径 (SUBQUERY_RESULT) |
| `predpushforce` | 强制推入 subplanrestrictinfo 中的谓词 (SUBQUERY_PARAM) |
| `partialpush` | 与部分推入相关的安全性配合检查 (qual_pushdown_in_partialpush) |

**源码走读** (均在 allpaths.cpp):

- `judge_predpush_subquery()`: 入口闸门——要求 rel 为基表级 (RELOPT_BASEREL/RELOPT_OTHER_MEMBER_REL)、非递归 CTE 场景、流引擎可用 (check_stream_support) 等
- `extract_predpush_equivclause()`: 从等价类中提取可推入的候选谓词
- `predpush_subquery()`: 把 joininfo/baserestrictinfo 中的等值连接谓词经 `collect_lateral_vars` + `trans_lateral_vars` (变量层级转换) 后以 `levelsup=1` 推入子查询
- `build_alternative_rel()`: 为"推入/不推入"两种形态各建一个 RelOptInfo, 让代价模型自行选择

### 与社区 PG 的差异点

1. **位置相同, 名字可对齐**: 通用推入主链 (set_subquery_pathlist / subquery_push_qual / qual_is_pushdown_safe) 与社区 PG 的 allpaths.c 一一对应, 行为基本一致。
2. **predpush 族是 openGauss 独有**: 社区 PG 没有 rewrite_rule=predpush 这条增强路径, 也没有 build_alternative_rel 的双形态竞争机制; 社区最近似的手段是 LATERAL 参数化路径, 但语义与开关粒度不同。
3. **ROWNUM 门槛**: openGauss 支持 ROWNUM 兼容语法 (另有 prep/preprownum.cpp 做预处理), `can_push_qual_into_subquery` 因此多了 has_rownum 拒绝分支——社区 PG 无此检查。
4. **security_barrier + 泄露型函数的判定**与社区 PG 同源 (contain_leaky_functions), 但 VastBase 现场常见把行级安全策略与 barrier 视图叠加使用, 排查推入失效时这两个来源都要看。

### 实践验证

```sql
-- 场景1/2/3 建表同前 (customers/orders)

-- 1. 聚合推入验证: 看 Filter 是否落在聚合节点上
EXPLAIN (COSTS OFF)
SELECT * FROM (
    SELECT customer_id, SUM(amount) AS total
    FROM orders GROUP BY customer_id
) t WHERE total > 500;

-- 2. UNION 分支推入验证: 看每个分支是否各自带 Filter
EXPLAIN (COSTS OFF)
SELECT * FROM (
    SELECT customer_id, amount FROM orders WHERE customer_id < 3
    UNION ALL
    SELECT customer_id, amount FROM orders WHERE customer_id >= 3
) h WHERE amount > 100;

-- 3. LIMIT 阻断验证: Subquery Scan 上留 Filter
EXPLAIN (COSTS OFF)
SELECT * FROM (
    SELECT * FROM orders LIMIT 100
) sub WHERE amount > 100;

-- 4. security_barrier 验证
CREATE VIEW v_sec WITH (security_barrier) AS SELECT * FROM orders;
EXPLAIN (COSTS OFF) SELECT * FROM v_sec WHERE amount > 100;   -- 仍可推入

-- 5. predpush 开关验证 (需对应权限, 在现场按需执行)
SET rewrite_rule = 'predpushforce';
EXPLAIN (COSTS OFF)
SELECT * FROM customers c,
    (SELECT * FROM orders WHERE amount > 100) o
WHERE c.id = o.customer_id;
RESET rewrite_rule;
-- 观察: 开启后子查询内部出现对 c.id 的 Param 引用而非上层 Join 过滤
```

---

## 视图展开 (View Expansion)

### 核心概念

视图在存储层不是独立数据, 而是 `pg_rewrite` 里一条 **ON SELECT 规则** (CREATE VIEW 自动生成, 规则名固定为 `_RETURN`)。重写阶段把查询中对该视图的 RTE_RELATION 引用整体替换为规则体中的 Query——视图由此变成一个普通的 **RTE_SUBQUERY** 子查询, 交给后续的子查询提升/谓词推入继续加工。

这一步发生在 **Rewrite (重写器)**, 早于 Prep 和 Planner, 因此视图几乎从不构成优化边界——除非显式设置 security_barrier 或利用 `OFFSET 0` 等不可推入形态。

### 源码走读

**文件**: `src/gausskernel/optimizer/rewrite/rewriteHandler.cpp`

**调用链**:

```
QueryRewrite()                          -- 重写器主入口
    ├─ Step 1: RewriteQuery()           -- 先应用所有非 SELECT 规则
    │       (INSERT/UPDATE/DELETE 的 INSTEAD 规则, 可裂变出多个查询)
    └─ Step 2: fireRIRrules()           -- 对每个查询应用 RIR 规则
             (RIR = Retrieve-Instead-of-Retrieve, 即 ON SELECT 规则)
```

`fireRIRrules()` 的遍历逻辑:

1. 逐个扫描 `rtable` (范围表)。注意循环用 `while` 而非 foreach——展开规则会向 rtable 追加新条目, 列表在迭代中变长
2. `RTE_SUBQUERY` 条目: 递归进入其子查询继续找视图 (嵌套展开的来源之一)
3. `RTE_RELATION` 且 relkind 为普通视图: 收集其 CMD_SELECT 规则
4. 物化视图 (`RELKIND_MATVIEW`) 直接跳过——它按表对待, 不展开
5. **递归防护**: `activeRIRs` 链表记录展开路径上的 relid, 视图自引用 (直接或间接) 时报 "infinite recursion detected in rules"
6. 此外递归处理 cteList (WITH 内的查询) 与 hasSubLinks 的子链接 (经 fireRIRonSubLink)

`ApplyRetrieveRule()` 的核心动作 (视图真正被替换的地方):

```cpp
// 伪代码逻辑 (依据源码整理)
static Query* ApplyRetrieveRule(Query* parsetree, RewriteRule* rule,
                                int rt_index, ..., Relation relation, ...)
{
    // 1. 校验: ON SELECT 规则必须无条件、关系级 (actions 恰一条, qual 为空)
    // 2. 非系统视图展开受限检查 (restrict_nonsystem_relation_kind, 见差异点)
    // 3. 视图作为 UPDATE/DELETE 目标: 先复制一份 RTE 作结果关系,
    //    原 RTE 继续展开为数据来源 (配合 rewriteTargetView 处理可更新视图)
    // 4. copyObject 规则体 + AcquireRewriteLocks 加锁
    // 5. fireRIRrules(rule_action) —— 递归展开视图中的视图 (嵌套视图!)
    // 6. 关键替换:
    rte = rt_fetch(rt_index, parsetree->rtable);
    rte->rtekind = RTE_SUBQUERY;                    // 关系 → 子查询
    rte->relid = InvalidOid;
    rte->security_barrier = RelationIsSecurityView(relation);
    rte->subquery = rule_action;                    // 视图定义体接入
}
```

**展开后的接力**: 视图变 RTE_SUBQUERY 后, `prepjointree.cpp: pull_up_simple_subquery` (见第 3 节) 通常会把它向上合并进外层查询, 于是 EXPLAIN 里直接看到基表扫描——"视图像宏一样内联" 的观感由**重写展开 + 子查询提升**两步接力完成。若视图带 LIMIT/聚集/窗口函数等不可提升形态, 则 Subquery Scan 保留, 再由谓词推入一节的规则决定过滤条件去向。

### 可验证示例

```sql
CREATE VIEW v_big_orders AS
SELECT id, customer_id, amount
FROM orders
WHERE amount > 1000;

-- 完全内联: 计划里只有基表, 两个 WHERE 条件合并
EXPLAIN (COSTS OFF)
SELECT * FROM v_big_orders WHERE customer_id = 1;

-- 预期形态:
--  Seq Scan on orders
--    Filter: ((amount > '1000'::numeric) AND (customer_id = 1))

-- 嵌套视图: v2 引用 v1, 递归展开后同样完全内联
CREATE VIEW v_big_orders_2024 AS
SELECT * FROM v_big_orders WHERE id > 0;

EXPLAIN (COSTS OFF)
SELECT * FROM v_big_orders_2024 WHERE customer_id = 1;

-- 展开受阻的形态: 视图带 LIMIT, 子查询提升与谓词推入双双失效
CREATE VIEW v_top10 AS
SELECT * FROM orders ORDER BY amount DESC LIMIT 10;

EXPLAIN (COSTS OFF)
SELECT * FROM v_top10 WHERE customer_id = 1;

-- 预期形态: 保留 Subquery Scan, Filter 在其上
--  Subquery Scan on v_top10
--    Filter: (customer_id = 1)
--    ->  Limit
--          ->  Sort ... Seq Scan on orders
```

### 与社区 PG 的差异点

1. **B 兼容模式 (Oracle 语法) 的 SQL Security 选项**: openGauss 6.0.0 的 `ApplyRetrieveRule` 对 `DB_IS_CMPT(B_FORMAT)` 的视图检查 definer/invoker 选项 (`RelationHasViewSecurityDefinerOption` / `RelationHasViewSecurityInvokerOption`), B 兼容库中视图默认按 definer 权限检查, 并经 `viewSecurityPassDown` 把 checkAsUser 传递给展开后的全部 RTE。社区 PG 只有 `security_invoker` 一个视图选项且默认 definer, 没有这条 B_FORMAT 分支。
2. **非系统视图展开闸门**: `restrict_nonsystem_relation_kind` 打开后, 非 system catalog 范围的视图直接拒绝展开 (报 "Access to non-system view ... is restricted")——这是 openGauss 的安全加固项, 社区 PG 无对应行为, 升级/迁移现场遇到该报错优先查这个 GUC。
3. **分布式形态下的 RLS 绑定**: `ENABLE_MULTIPLE_NODES` 编译宏下行级安全策略仅在 CN 上绑定 (IS_PGXC_COORDINATOR 判定); 单机版 (含 VastBase 单机) 行为与社区一致。
4. 展开顺序与递归防护逻辑 (activeRIRs) 与社区 PG 同源, 嵌套视图、视图自引用报错行为一致。

---

## 表达式优化 (Expression Optimization)

### 核心概念

表达式优化指在计划生成前对表达式树做**保语义化简**: 布尔代数化简、NOT 消解、AND/OR 平坦化、布尔等式改写、OR 子句拆取等。它不改变查询的表级结构 (那是提升/推入的职责), 只让每个表达式本身更"干净"——更少的求值次数、更早的常量确定、更规范的可交换形式。

openGauss 中这一步集中在两个位置:

- `planner.cpp: preprocess_expression` —— 每个表达式进入计划器的统一预处理入口
- `prepqual.cpp` + `util/clauses.cpp` —— 具体化简的实现

### 源码走读

**planner.cpp: preprocess_expression** 的处理序列 (每个表达式必经):

```
preprocess_expression(root, expr, kind)
    ├─ flatten_join_alias_vars()      -- 消解 JOIN 输出列别名变量
    ├─ eval_const_expressions()       -- 常量折叠 + 布尔化简 + AND/OR 平坦化
    │                                   (第 7 节专题, 同时承担布尔化简)
    ├─ canonicalize_qual()            -- [仅 EXPRKIND_QUAL] qual 规范化
    ├─ SS_process_sublinks()          -- SubLink → SubPlan
    └─ make_ands_implicit()           -- [仅 EXPRKIND_QUAL] 显式 AND → 隐式 AND 列表
```

`prepqual.cpp: canonicalize_qual` 做的是**规范化**而非分配律展开: 嵌套 AND/OR 拍平 (AND 下不再有 AND)、子句排序定序。它明确**不做** OR-of-AND 到 AND-of-OR 的分配转换——文件头注释说明这类转换已交给 eval_const_expressions 在更合适的上下文处理。

**prepqual.cpp: negate_clause** —— NOT 消解 (第 4 节谓词推入源码分析里已展示德摩根分支), 除布尔德摩根外还处理:

```sql
-- NOT 消解的典型改写
NOT (a = b)          →  a <> b
NOT (a IS NULL)      →  a IS NOT NULL
NOT (a BETWEEN b AND c)  →  a < b OR a > c
NOT NOT x            →  x
```

**util/clauses.cpp: eval_const_expressions_mutator** 的 `T_BoolExpr` 分支承担布尔代数化简:

- `simplify_or_arguments()`: 参数逐一化简, 常量 true 短路整个 OR (forceTrue), 常量 false 剔除, 重复参数去重, 全空 → false
- `simplify_and_arguments()`: 对称地, 常量 false 短路 (forceFalse), 常量 true 剔除
- `NOT_EXPR`: 先化简参数再交给 `negate_clause()` 尝试消掉 NOT
- 单一剩余参数时脱掉 AND/OR 外壳直接返回内层

**util/clauses.cpp: simplify_boolean_equality** —— 布尔等式/不等式改写: `x = true → x`, `x = false → NOT x` (对 `=`/`<>` 且操作数为布尔常量时)。

**util/orclauses.cpp: extract_restriction_or_clauses** —— 从**连接级 OR 子句**中拆取单表限制子句: `(a.x=1 AND b.y=2) OR (a.x=3 AND b.y=4)` 这种 OR-of-AND 连接条件, 每个同表子合取可以抽成 `a.x IN (1,3)` / `b.y IN (2,4)` 形式的限制 OR 子句 (extract_or_clause / consider_new_or_clause), 为索引扫描开路。这是与谓词推入互补的"OR 分解"通道。

### 可验证示例

```sql
-- 1. 常量真值剔除: Filter 只剩有效条件
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE 1 = 1 AND amount > 100;
-- 预期: Filter: (amount > '100'::numeric)

-- 2. 整个谓词折叠为常量: 出现 One-Time Filter
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE 1 = 2;
-- 预期: Result  (One-Time Filter: false)

-- 3. NOT 消解: Filter 显示反操作符
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE NOT (customer_id = 1);
-- 预期: Filter: (customer_id <> 1)

-- 4. 双重否定
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE NOT NOT (amount > 100);
-- 预期: Filter: (amount > '100'::numeric)

-- 5. 布尔等式改写
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE (amount > 100) = true;
-- 预期: Filter: (amount > '100'::numeric)

-- 6. OR 中常量 false 剔除 / true 短路
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE amount > 100 OR 1 = 2;
-- 预期: Filter: (amount > '100'::numeric)
```

### 与社区 PG 的差异点

1. 主体化简链 (canonicalize_qual / negate_clause / simplify_*_arguments / simplify_boolean_equality) 与社区 PG 同源同行为, 社区对应文件为 src/backend/optimizer/prep/prepqual.c 与 util/clauses.c。
2. openGauss 的 `orclauses.cpp` 在社区中同样存在 (extract_restriction_or_clauses), 但 openGauss 另以 `sql_beta_param` 的 `EXTRACT_PUSHDOWN_OR_CLAUSE` 位 (guc.h) 控制相关增强, 开关粒度不同。
3. 涉及 stable/volatile 函数的化简边界, openGauss 有独家白名单 (见第 7 节 pre_evaluate_func), 因此"哪些函数调用会在计划期被吃掉"两边结论不同, 不能按社区 PG 的经验照搬。

---

## 常量表达式折叠 (Constant Expression Folding)

### 核心概念

常量折叠指: **全常量输入**的表达式在**计划期**直接求值, 用一个 Const 节点替换整个表达式子树。它把运行期重复求值变成一次性求值, 并触发连锁效应——折叠结果作为新常量参与上层布尔化简 (1=1 消解的前提就是两边都先折成了常量)。

判定的安全边界:

| 场景 | 是否折叠 |
|---|---|
| 全常量输入 + IMMUTABLE 函数/操作符 | 折叠 (计划期求值) |
| strict 函数 + 任一常量 NULL 输入 | 折叠为 NULL 常量 (无需执行) |
| 全常量输入 + STABLE 函数 | 常规计划**不折叠**; 估算模式 (estimate) 可折叠 |
| 含 volatile 函数 | 不折叠 |
| PARAM_EXTERN 且绑定值带 PARAM_FLAG_CONST | 替换为 Const |
| 函数返回集合 (SRF) / 返回 RECORD | 不折叠 |

### 源码走读

**文件**: `src/gausskernel/optimizer/util/clauses.cpp`

**入口族** (同一 mutator 的四种上下文包装):

- `eval_const_expressions()`: 标准入口, `estimate=false`, 只做安全转换——由 preprocess_expression 对所有表达式调用
- `eval_const_expressions_params()`: 调用方自带绑定参数
- `estimate_expression_value()`: `estimate=true` 估算模式, 放宽两点——非 CONST 标记的绑定 Param 也代入首值、STABLE 函数也折叠, 仅用于选择率估算, 不进最终计划
- `eval_const_expression_value()`: openGauss 独有, 服务 set 用户自定义变量场景

**核心 mutator**: `eval_const_expressions_mutator()`, 按节点类型分派:

- `T_Param`: 绑定参数代入 (PARAM_EXTERN + pflags 带 PARAM_FLAG_CONST 时换成 Const; openGauss 的 ENABLE_CACHEDPLAN_MGR 惰性绑定路径在此短路, 见差异点)
- `T_FuncExpr` / `T_OpExpr`: 统一交给 `simplify_function()` 处理
- `T_BoolExpr`: 布尔代数化简 (第 6 节已述)
- `T_DistinctExpr` / `T_NullTest` / `T_BooleanTest` / `T_CaseExpr` / `T_CoalesceExpr` 等: 各自的 NULL 语义专项化简 (如 CoalesceExpr 首个非 NULL 常量参数直接成为结果)

**simplify_function()** 的三条化简策略 (函数级):

1. **执行求值**: `evaluate_function()` —— 满足上表安全边界时, 经 `evaluate_expr()` 启动一次真实的函数执行, 结果封成 Const。拦截条件包括: proretset (集合返回)、返回 RECORD、层级查询函数 (IsHierarchicalQueryFuncOid)、strict + NULL 输入 (直接得 NULL Const)、provolatile 检查
2. **支持函数改写**: pg_proc 的 protransform 挂了 SupportRequestSimplify 支持函数时, 请求其生成等价替换树 (如时间函数的区间改写)
3. **函数体内联**: `inline_function()` —— 单条 `SELECT expr` 形体的 SQL 函数, 直接把函数体表达式替换进调用点, 消除一次函数调用开销; 递归内联由 active_fns 列表防环

### 可验证示例

```sql
-- 1. 算术折叠: 计划里看到的是结果值
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE id = 1 + 1;
-- 预期: Filter: (id = 2)

-- 2. 字符串拼接折叠 (|| 为 IMMUTABLE)
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE customer_name = 'Ali' || 'ce';
-- 预期: Filter: (customer_name = 'Alice'::text)

-- 3. 计划期求值的副作用——错误提前到计划期暴露:
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE id = 1/0;
-- 预期: 直接报除零错误, 而不是返回 0 行后无感知
-- (现场可用这个特性快速判断一个谓词是否被整体折叠)

-- 4. stable 函数不折叠: now() 保留在计划里
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE order_date > now() - interval '7 days';
-- 预期: Filter 里保留表达式原样 (now() 为 STABLE, 不在预求值白名单)

-- 5. strict 函数 + NULL 常量输入 → NULL 常量, 谓词整体走 NULL 语义
EXPLAIN (COSTS OFF)
SELECT * FROM orders WHERE amount > NULL::numeric;
-- 预期: 不会出现扫描节点层面的比较, NULL 语义在计划期定案

-- 6. prepared statement 参数代入 (PSC 内 PARAM_FLAG_CONST 场景):
PREPARE p(int) AS SELECT * FROM orders WHERE id = $1;
EXPLAIN (COSTS OFF) EXECUTE p(2);
-- 通用计划生成后观察 $1 是否保留; 首几次执行走定制计划时常量已代入
```

### 与社区 PG 的差异点

1. **STABLE 函数预求值白名单 (openGauss 独有)**: `clauses.cpp` 中的 `pre_evaluate_func[]` 数组 + `is_safe_simplify_func()` 把 current_schema / current_user / session_user / current_database / pg_client_encoding 这组 **STABLE 甚至 VOLATILE** 标记的函数在常规计划期就预求值成常量, 另对 concat/concat_ws 做了参数类型相关的特判。社区 PG 中 STABLE 函数只有估算模式才折叠——这意味着 openGauss/VastBase 上**计划缓存 (prepared statement 复用) 遇到这组函数时, 折叠值固化在通用计划里**, 现场排查"改了 search_path 但计划没变"类问题时先想到这条。
2. **惰性参数绑定**: `T_Param` 分支里 `ENABLE_CACHEDPLAN_MGR` + `params_lazy_bind` 时直接返回 Param 副本不做代入——openGauss 计划缓存子系统为了跨参数复用刻意保留参数节点, 社区 PG 无此路径。
3. `eval_const_expression_value()` 与 `simplify_select_into_expression()` 为 openGauss 独有入口, 服务用户自定义变量与 SELECT INTO 变量绑定场景。
4. 折叠的安全边界主表 (IMMUTABLE/strict+NULL/SRF/RECORD 拒绝) 与社区 PG 完全一致, 两者对 `WHERE 1/0` 报错时机、`now()` 不折叠等可观察行为相同。

---

## 版本注记

- 2026-11-05: 补写第 4~7 节 (谓词推入 / 视图展开 / 表达式优化 / 常量表达式折叠) 及本注记; 原文在约 500 行处截断, 谓词推入一节 "WHERE amount > 100;" 断例一并补齐。
- 源码基线: openGauss-server-v6.0.0, 路径前缀 `src/gausskernel/optimizer/` (openGauss 实际布局; 社区 PG 对应 `src/backend/optimizer/`, 两者目录结构不同, 文件名以本文标注为准)。
- 事实勘正: 谓词推入的主链在本版本位于 `path/allpaths.cpp` (与社区 PG 的 allpaths.c 对应), 而非 `prep/prepjointree.cpp`; prepjointree.cpp 承担的是 Sublink/Subquery 提升与连接树预处理。概述一节流程图把谓词推入画在 Prep 框内为简化表达, 精确时序见第 4 节源码走读。
- 社区 PG 中常见的函数名 `simplify_expression` 在 openGauss 6.0.0 树中不存在, 对应逻辑内联于 `clauses.cpp: eval_const_expressions_mutator`; 本文引用的函数名均已在源码树中逐一核实, 无 ⚓待锚 项。

## 总结

1. 查询转换发生在 Rewrite（视图/规则）与 Prep（子链接/子查询提升、谓词推入、
   常量折叠）两层；Sublink/Subquery 提升是 openGauss 继承自 PG 并在 predpush
   上做了增强扩展
2. 谓词推入主链在 allpaths.cpp: set_subquery_pathlist 一族（subquery_push_qual/
   recurse_push_qual/predpush_subquery），推入安全性由 qual_is_pushdown_safe
   与输出表达式检查共同把关；security_barrier 视图阻断推入
3. 视图展开=ON SELECT 规则（_RETURN）→RTE_SUBQUERY 替换，fireRIRrules 递归
   遍历、activeRIRs 防环；UPDATE/DELETE 目标视图走 rewriteTargetView
4. 表达式规范化只做合取范式化（canonicalize_qual），不做分配律展开；OR 子句
   拆取走 orclauses.cpp（EXTRACT_PUSHDOWN_OR_CLAUSE，beta 开关）
5. 常量折叠的安全边界=IMMUTABLE/strict+NULL/STABLE 白名单（pre_evaluate_func）；
   计划缓存场景慎用 STABLE 预折叠（固化风险）
6. 与社区 PG 差异集中在 predpush 增强、B_FORMAT definer/invoker、RLS 位置

## 来源

- 源码实读（openGauss-server-v6.0.0，2026-11-05）：gausskernel/optimizer/
  path/allpaths.cpp、rewrite/rewriteHandler.cpp、plan/planner.cpp、
  prep/prepqual.cpp、util/clauses.cpp、util/orclauses.cpp；GUC 侧
  common/backend/utils/misc/guc/guc_sql.cpp（rewrite_rule: predpush 族）
- 原文 2026-04-16 学习档案（前 500 行）；2026-11-05 补写第 4~7 节与断例
