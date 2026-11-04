     1|# VastBase/openGauss 查询转换机制深度分析
     2|
     3|> 学习时间: 2026-04-16  
     4|> 源码版本: openGauss-server-v6.0.0  
     5|> 分析深度: 源码级别  
     6|> 状态: 理论学习 + 源码分析 + 实践验证
     7|
     8|## 目录
     9|1. [查询转换概述](#查询转换概述)
    10|2. [Sublink提升机制](#sublink提升机制)
    11|3. [Subquery提升机制](#subquery提升机制)
    12|4. [谓词推入](#谓词推入)
    13|5. [视图展开](#视图展开)
    14|6. [表达式优化](#表达式优化)
    15|7. [常量表达式折叠](#常量表达式折叠)
    16|
    17|---
    18|
    19|## 查询转换概述
    20|
    21|### 转换层次结构
    22|
    23|```
    24|SQL查询
    25|   ↓
    26|Parser (解析器) → Parse Tree
    27|   ↓
    28|Analyzer (分析器) → Query Tree  
    29|   ↓
    30|Rewrite (重写器) ← 查询转换主要发生地
    31|   ├─ Rule System (规则系统)
    32|   ├─ View Expansion (视图展开)
    33|   └─ RLS Policy (行级安全策略)
    34|   ↓
    35|Prep (预处理)
    36|   ├─ Sublink Pullup (子链接提升)
    37|   ├─ Subquery Pullup (子查询提升)
    38|   ├─ Predicate Pushdown (谓词推入)
    39|   └─ Constant Folding (常量折叠)
    40|   ↓
    41|Planner (优化器) → Optimized Plan
    42|```
    43|
    44|### 核心源码模块
    45|
    46|```
    47|src/gausskernel/optimizer/
    48|├── rewrite/           # 查询重写模块
    49|│   ├── rewriteHandler.cpp    (5301行, 主要重写逻辑)
    50|│   ├── rewriteManip.cpp      # 表达式操作
    51|│   ├── rewriteDefine.cpp     # 规则定义
    52|│   └── rewriteRlsPolicy.cpp  # RLS策略
    53|│
    54|├── prep/              # 查询预处理模块  
    55|│   ├── prepjointree.cpp      (3881行, 连接树预处理)
    56|│   ├── prepqual.cpp          (659行, 条件表达式预处理)
    57|│   ├── preptlist.cpp         # 目标列表预处理
    58|│   ├── prepunion.cpp         # UNION处理
    59|│   ├── prepnonjointree.cpp   # 非连接树预处理
    60|│   └── preprownum.cpp        # ROWNUM处理
    61|│
    62|└── plan/              # 计划生成模块
    63|    └── subselect.cpp   # 子查询处理
    64|```
    65|
    66|---
    67|
    68|## Sublink提升机制
    69|
    70|### 核心概念
    71|
    72|**Sublink** 是查询树中表示子查询表达式的节点类型，通常出现在WHERE子句、SELECT列表等位置。
    73|
    74|#### Sublink类型定义 (从源码提取)
    75|
    76|```cpp
    77|// src/include/nodes/parsenodes.h
    78|typedef enum SubLinkType {
    79|    EXISTS_SUBLINK,      // EXISTS子查询
    80|    ALL_SUBLINK,         // quantified ALL subquery
    81|    ANY_SUBLINK,         // quantified ANY subquery  
    82|    ROWCOMPARE_SUBLINK,  // row-wise comparison
    83|    EXPR_SUBLINK,        // 无相关性的标量子查询
    84|    ARRAY_SUBLINK        // ARRAY(...) subquery
    85|} SubLinkType;
    86|
    87|typedef struct SubLink {
    88|    NodeTag     type;
    89|    SubLinkType subLinkType;     // 子链接类型
    90|    Node*       testexpr;        // 外层表达式,用于ANY/ALL/ROWCOMPARE
    91|    List*       operName;        // 操作符名称
    92|    Node*       lefthand;        // 左操作数
    93|    Node*       subselect;       // 子查询的Query节点
    94|    int         location;        // token位置
    95|} SubLink;
    96|```
    97|
    98|### Sublink提升策略
    99|
   100|#### 1. EXISTS_SUBLINK 提升
   101|
   102|**适用场景**:
   103|- EXISTS子查询可以转换为SEMI JOIN
   104|- 消除子查询，提高join效率
   105|
   106|**转换示例**:
   107|
   108|```sql
   109|-- 转换前 (Sublink形式)
   110|SELECT * FROM orders o
   111|WHERE EXISTS (SELECT 1 FROM customers c 
   112|              WHERE c.id = o.customer_id 
   113|              AND c.country = 'CN');
   114|
   115|-- 转换后 (SEMI JOIN)
   116|SELECT o.* FROM orders o
   117|SEMI JOIN customers c 
   118|  ON c.id = o.customer_id 
   119| WHERE c.country = 'CN';
   120|```
   121|
   122|**源码位置**: `src/gausskernel/optimizer/prep/prepjointree.cpp`
   123|**核心函数**: `pull_up_sublinks()`
   124|
   125|**提升条件**:
   126|1. 子查询不包含聚合函数
   127|2. 子查询不包含GROUP BY/HAVING
   128|3. 子查询不包含LIMIT/OFFSET
   129|4. 子查询只返回一列(或EXISTS)
   130|5. 相关性条件可以直接转换为JOIN条件
   131|
   132|#### 2. EXPR_SUBLINK (标量子查询) 提升
   133|
   134|**适用场景**:
   135|- 无相关性的标量子查询
   136|- 可以提前计算，常量折叠
   137|
   138|**转换示例**:
   139|
   140|```sql
   141|-- 转换前
   142|SELECT id, (SELECT MAX(amount) FROM orders WHERE customer_id = 10) 
   143|FROM customers 
   144|WHERE id = 10;
   145|
   146|-- 转换后 (子查询独立执行)
   147|SELECT id, max_amount 
   148|FROM customers, 
   149|     (SELECT MAX(amount) AS max_amount FROM orders WHERE customer_id = 10) subq
   150|WHERE id = 10;
   151|```
   152|
   153|**提升限制**:
   154|- 如果子查询结果超过1行，会报错
   155|- 标量子查询通常无法提升，需要特殊处理
   156|
   157|#### 3. ANY_SUBLINK / ALL_SUBLINK 提升
   158|
   159|**适用场景**:
   160|- 可以转换为JOIN或Semi/Anti Join
   161|- `WHERE col > ANY (subquery)` → JOIN + 条件
   162|- `WHERE col > ALL (subquery)` → 聚集子查询
   163|
   164|**转换示例**:
   165|
   166|```sql
   167|-- ANY转换
   168|-- 转换前
   169|SELECT * FROM products p
   170|WHERE p.price > ANY (SELECT avg_price FROM category_prices);
   171|
   172|-- 转换后 (SEMI JOIN)
   173|SELECT DISTINCT p.* FROM products p
   174|JOIN category_prices cp ON p.price > cp.avg_price;
   175|
   176|-- ALL转换  
   177|-- 转换前
   178|SELECT * FROM products p
   179|WHERE p.price > ALL (SELECT price FROM competitor_prices);
   180|
   181|-- 转换后 (聚合子查询)
   182|SELECT * FROM products p
   183|WHERE p.price > (SELECT MAX(price) FROM competitor_prices);
   184|```
   185|
   186|### Sublink提升的核心算法
   187|
   188|**源码位置**: `prepjointree.cpp:pull_up_sublinks_jointree_recurse()`
   189|
   190|**算法流程**:
   191|
   192|```cpp
   193|// 伪代码逻辑
   194|Node* pull_up_sublinks_jointree_recurse(PlannerInfo* root, Node* jtnode, ...) {
   195|    // 1. 遍历连接树，寻找可提升的Sublink
   196|    if (IsA(jtnode, JoinExpr)) {
   197|        // 处理JOIN节点
   198|        JoinExpr* j = (JoinExpr*)jtnode;
   199|        j->larg = pull_up_sublinks_jointree_recurse(root, j->larg, ...);
   200|        j->rarg = pull_up_sublinks_jointree_recurse(root, j->rarg, ...);
   201|        
   202|        // 尝试从quals中提取Sublink
   203|        if (j->quals) {
   204|            j->quals = pull_up_sublinks_qual_recurse(root, j->quals, ...);
   205|        }
   206|    } else if (IsA(jtnode, RangeTblRef)) {
   207|        // 基表引用，无需处理
   208|    }
   209|    
   210|    return jtnode;
   211|}
   212|
   213|Node* pull_up_sublinks_qual_recurse(PlannerInfo* root, Node* node, ...) {
   214|    // 2. 递归查找表达式中的Sublink
   215|    if (IsA(node, SubLink)) {
   216|        SubLink* sublink = (SubLink*)node;
   217|        
   218|        // 3. 检查是否可提升
   219|        if (convert_testexpr_needed) {
   220|            // 转换为JOIN形式
   221|            return convert_sublink_to_join(sublink, available_rels);
   222|        } else {
   223|            // 不可提升，保持原样
   224|            return node;
   225|        }
   226|    }
   227|    
   228|    // 4. 递归处理表达式树的其他节点
   229|    return expression_tree_mutator(node, pull_up_sublinks_qual_recurse, context);
   230|}
   231|```
   232|
   233|### 实践验证示例
   234|
   235|```sql
   236|-- 测试表
   237|CREATE TABLE customers (
   238|    id INT PRIMARY KEY,
   239|    name VARCHAR(100),
   240|    country VARCHAR(50)
   241|);
   242|CREATE TABLE orders (
   243|    id INT PRIMARY KEY,
   244|    customer_id INT,
   245|    amount NUMERIC(10,2),
   246|    order_date DATE
   247|);
   248|
   249|INSERT INTO customers VALUES 
   250|    (1, 'Alice', 'CN'), (2, 'Bob', 'US'), (3, 'Charlie', 'CN');
   251|INSERT INTO orders VALUES
   252|    (1, 1, 100.00, '2024-01-01'),
   253|    (2, 1, 200.00, '2024-01-15'),
   254|    (3, 2, 150.00, '2024-01-10');
   255|
   256|-- EXISTS提升测试
   257|EXPLAIN 
   258|SELECT * FROM customers c
   259|WHERE EXISTS (SELECT 1 FROM orders o 
   260|              WHERE o.customer_id = c.id 
   261|              AND o.amount > 100);
   262|
   263|-- 预期结果: 应该看到 Hash Semi Join 或者类似的Semi Join执行计划
   264|-- 未提升前: 子查询独立执行，可能效率较低
   265|-- 提升后:   SEMI JOIN，一次扫描完成
   266|
   267|-- 标量子查询测试
   268|EXPLAIN  
   269|SELECT c.name, 
   270|       (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id) AS order_count
   271|FROM customers c;
   272|
   273|-- 观察执行计划，看是否提升为JOIN
   274|-- 标量子查询通常不会提升，而是通过InitPlan执行
   275|```
   276|
   277|---
   278|
   279|## Subquery提升机制
   280|
   281|### 核心概念
   282|
   283|**Subquery (子查询)** 指出现在FROM子句中的查询，也称为**Derived Table**或**Subquery in FROM clause**。
   284|
   285|#### Subquery提升的类型
   286|
   287|1. **简单子查询提升** (`pull_up_simple_subquery`)
   288|   - 不包含聚合、GROUP BY、HAVING
   289|   - 不包含LIMIT/OFFSET  
   290|   - 不包含集合操作(UNION等)
   291|   - 不包含OUTER JOIN的相关性
   292|
   293|2. **UNION ALL提升** (`pull_up_simple_union_all`)
   294|   - 纯UNION ALL查询
   295|   - 可以拆分为多个独立的查询
   296|
   297|3. **LATERAL子查询提升**
   298|   - LATERAL子查询特殊处理
   299|   - 依赖于前面表的相关性
   300|
   301|### Subquery提升的条件
   302|
   303|**源码位置**: `prepjointree.cpp:is_simple_subquery()`
   304|
   305|```cpp
   306|static bool is_simple_subquery(Query* subquery, RangeTblEntry* rte, 
   307|                               JoinExpr* lowest_outer_join)
   308|{
   309|    // 检查1: 不包含聚合
   310|    if (subquery->hasAggs)
   311|        return false;
   312|    
   313|    // 检查2: 不包含窗口函数
   314|    if (subquery->hasWindowFuncs)
   315|        return false;
   316|    
   317|    // 检查3: 不包含GROUP BY / HAVING
   318|    if (subquery->groupClause || subquery->havingQual)
   319|        return false;
   320|    
   321|    // 检查4: 不包含LIMIT/OFFSET
   322|    if (subquery->limitCount || subquery->limitOffset)
   323|        return false;
   324|    
   325|    // 检查5: 不包含集合操作
   326|    if (subquery->setOperations)
   327|        return false;
   328|    
   329|    // 检查6: 不包含CTE (WITH子句)
   330|    if (subquery->cteList)
   331|        return false;
   332|    
   333|    // 检查7: 不包含DISTINCT (可能转换)
   334|    if (subquery->distinctClause)
   335|        return false;
   336|    
   337|    // 检查8: 目标列表中不能包含 volatile 函数
   338|    // ... 省略详细检查
   339|    
   340|    return true;
   341|}
   342|```
   343|
   344|### Subquery提升示例
   345|
   346|#### 场景1: 简单子查询提升
   347|
   348|```sql
   349|-- 转换前
   350|SELECT c.name, o.order_count
   351|FROM customers c,
   352|     (SELECT customer_id, COUNT(*) AS order_count
   353|      FROM orders
   354|      GROUP BY customer_id) o
   355|WHERE c.id = o.customer_id;
   356|
   357|-- 转换后 (子查询提升失败，因为有GROUP BY)
   358|-- 保持不变
   359|
   360|-- 另一个例子
   361|-- 转换前
   362|SELECT c.name, o.amount
   363|FROM customers c,
   364|     (SELECT customer_id, amount 
   365|      FROM orders
   366|      WHERE amount > 100) o
   367|WHERE c.id = o.customer_id;
   368|
   369|-- 转换后 (提升成功，简单子查询)
   370|SELECT c.name, o.amount
   371|FROM customers c
   372|JOIN orders o ON c.id = o.customer_id
   373|WHERE o.amount > 100;
   374|```
   375|
   376|#### 场景2: UNION ALL提升
   377|
   378|```sql
   379|-- 转换前
   380|SELECT * FROM (
   381|    SELECT id, name FROM customers_cn
   382|    UNION ALL
   383|    SELECT id, name FROM customers_us
   384|) combined_customers;
   385|
   386|-- 转换后 (拆分为两个查询)
   387|-- 优化器可以为每个分支选择最优计划
   388|```
   389|
   390|#### 场景3: LATERAL子查询处理
   391|
   392|```sql
   393|-- LATERAL子查询不能随意提升
   394|-- 转换前 (已经是JOIN形式，无法进一步提升)
   395|SELECT c.name, o.order_stats
   396|FROM customers c
   397|LEFT JOIN LATERAL (
   398|    SELECT customer_id, 
   399|           COUNT(*) AS total_orders,
   400|           SUM(amount) AS total_amount
   401|    FROM orders o
   402|    WHERE o.customer_id = c.id
   403|) o ON true;
   404|```
   405|
   406|### Subquery提升的限制
   407|
   408|1. **聚合函数**: 包含GROUP BY的子查询不能提升
   409|2. **DISTINCT**: 可能影响语义，通常不提升
   410|3. **LIMIT/OFFSET**: 改变行数，不能提升
   411|4. **OUTER JOIN的相关性**: 复杂相关性，提升困难
   412|5. **集合操作**: UNION/INTERSECT/EXCEPT通常不提升
   413|
   414|### 实践验证
   415|
   416|```sql
   417|-- 测试表
   418|CREATE TABLE customers_cn AS 
   419|    SELECT 1 AS id, 'Alice_CN' AS name
   420|    UNION ALL
   421|    SELECT 2, 'Bob_CN';
   422|
   423|CREATE TABLE customers_us AS
   424|    SELECT 3 AS id, 'Charlie_US' AS name
   425|    UNION ALL  
   426|    SELECT 4, 'David_US';
   427|
   428|-- 简单子查询提升测试
   429|EXPLAIN
   430|SELECT * FROM customers,
   431|     (SELECT id, name FROM customers_cn) cn
   432|WHERE customers.id = cn.id;
   433|
   434|-- 预期: 可以看到子查询被提升为 JOIN
   435|
   436|-- UNION ALL提升测试
   437|EXPLAIN
   438|SELECT * FROM (
   439|    SELECT id, name FROM customers_cn
   440|    UNION ALL
   441|    SELECT id, name FROM customers_us
   442|) combined;
   443|
   444|-- 预期: 可以看到 Append 节点，表示UNION ALL被优化
   445|```
   446|
   447|---
   448|
   449|## 谓词推入 (Predicate Pushdown)
   450|
   451|### 核心概念
   452|
   453|**谓词推入** 将过滤条件尽可能推到查询树的底部，尽早过滤数据，减少后续处理的数据量。
   454|
   455|### 谓词推入的类型
   456|
   457|1. **简单谓词推入**: 直接下推到基表
   458|2. **JOIN谓词推入**: 推过JOIN节点
   459|3. **视图谓词推入**: 推入视图内部
   460|4. **UNION谓词推入**: 推入UNION各个分支
   461|
   462|### 源码分析
   463|
   464|**核心函数**: `prepqual.cpp` 中的条件表达式处理
   465|
   466|主要操作:
   467|1. **规范化表达式**: 将AND/OR转换为平坦形式
   468|2. **常量折叠**: 预计算常量表达式
   469|3. **子查询平化**: 展开嵌套的AND/OR
   470|4. **NOT下推**: 德摩根定律转换
   471|
   472|```cpp
   473|// 示例: NOT (A AND B) → (NOT A) OR (NOT B)
   474|Node* negate_clause(Node* node) {
   475|    switch (nodeTag(node)) {
   476|        case T_BoolExpr: {
   477|            BoolExpr* expr = (BoolExpr*)node;
   478|            if (expr->boolop == AND_EXPR) {
   479|                // 应用德摩根定律: NOT (A AND B) → (NOT A) OR (NOT B)
   480|                return make_orclause(negate_clause(expr->args));
   481|            } else if (expr->boolop == OR_EXPR) {
   482|                // NOT (A OR B) → (NOT A) AND (NOT B)
   483|                return make_andclause(negate_clause(expr->args));
   484|            }
   485|        }
   486|        // ... 其他情况
   487|    }
   488|}
   489|```
   490|
   491|### 谓词推入示例
   492|
   493|#### 场景1: 基本谓词推入
   494|
   495|```sql
   496|-- 转换前
   497|SELECT * FROM (
   498|    SELECT * FROM orders
   499|) sub
   500|WHERE amount > 100;
   501|