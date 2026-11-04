---
name: vastbase-sql-optimizer-deep
version: "3.2"
description: VastBase/openGauss超深度SQL优化专家 - 源码级Cost计算、统计信息偏差定位、查询转换深度分析
category: database
tags: [sql, optimization, vastbase, opengauss, performance, cost-calculation, statistics, join-order, query-transformation]
author: lxy
created: "2026-04-17"
depends_on:
  - <vastbase-optimizer-notes>/VastBase_Optimizer_01_Query_Transformation_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_03_Join_Order_GEQO_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_04_Parameters_Index_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_05_Path_Plan_Generation_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_06_Advanced_Features_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_07_Final_Advanced_Tuning.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_08_Executor_Framework_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_09_Parallel_Executor_Tuning_Deep_Dive.md
  - <vastbase-optimizer-notes>/VastBase_Optimizer_10_SMP_Hints_Rewrite_Rules_Deep_Dive.md
---

# VastBase SQL超深度优化技能 v3.2

## 技能定位

这是一个**源码级别**的VastBase/openGauss SQL优化专家技能，提供：

### 🎯 核心能力

1. **逐行Cost计算** - 精确到每个CPU周期、每个I/O操作
2. **统计信息偏差定位** - 定位到具体表的具体字段的MCV/直方图偏差
3. **查询转换深度分析** - 解释每个转换为什么发生/不发生
4. **JOIN顺序验证** - 确保JOIN顺序是最优的
5. **JOIN方式选择** - 深度分析Nested Loop vs Hash Join vs Merge Join
6. **扫描节点分析** - Seq Scan vs Index Scan vs Bitmap Scan
7. **优化方案输出** - 分为不改写SQL和可改写SQL两种

### 📚 知识基础

基于10篇VastBase优化器源码级深度文档（共400KB+，8000+行）

---

## 第一部分：分析流程总览

### 必须的分析顺序（严格优先级）

```
用户输入: SQL + EXPLAIN (ANALYZE, BUFFERS, VERBOSE) + 统计信息
         ↓
步骤1: JOIN顺序分析 (必须优先)
         ├─ 当前JOIN顺序是什么？
         ├─ 为什么选择这个顺序？
         ├─ 是否有更好的顺序？
         └─ 如果顺序错误，这是第一瓶颈
         ↓
步骤2: JOIN方式分析 (在顺序正确后)
         ├─ 每个JOIN用了什么方式？
         ├─ Nested Loop / Hash Join / Merge Join
         ├─ Cost计算详细过程
         └─ 如果方式错误，这是第二瓶颈
         ↓
步骤3: 扫描节点分析 (最后分析)
         ├─ Seq Scan / Index Scan / Bitmap Scan
         ├─ 每个扫描的Cost计算
         └─ 如果扫描错误，这是第三瓶颈
         ↓
步骤4: 其他节点分析 (Sort, Aggregate, Limit等)
         ↓
步骤5: 统计信息验证 (贯穿全程)
         ├─ 哪个表的哪个字段偏差？
         ├─ MCV是否准确？
         ├─ 直方图是否准确？
         └─ n_distinct是否准确？
         ↓
步骤6: 查询转换验证 (贯穿全程)
         ├─ 哪些转换应该发生但没发生？
         ├─ 哪些转换不应该发生但发生了？
         └─ 为什么？
         ↓
输出: 性能瓶颈列表 (从大到小) + 优化方案
       ├─ 2.1 不改写SQL的方案 (索引/Hint)
       └─ 2.2 可改写SQL的方案
```

---

## 第二部分：JOIN顺序深度分析（第一优先级）

### 2.1 为什么要优先分析JOIN顺序

**原因**: JOIN顺序错误是**最致命**的性能问题，影响范围：

```
错误的JOIN顺序影响:
  ├─ JOIN方式选择 (Hash Join变成Nested Loop)
  ├─ 扫描节点选择 (Index Scan变成Seq Scan)
  ├─ 中间结果集大小 (可能膨胀1000倍)
  ├─ 内存使用 (可能溢出到磁盘)
  └─ 整体查询性能 (可能慢100倍)

示例:
  正确顺序: 小表(100行) × 中表(1万行) × 大表(100万行)
    → 中间结果: 100 → 1000 → 10000
    → Cost ≈ 5000
  
  错误顺序: 大表(100万行) × 中表(1万行) × 小表(100行)
    → 中间结果: 100万 → 1亿 → 10000
    → Cost ≈ 500000
    → 性能差距: 100倍
```

### 2.2 如何识别当前JOIN顺序

**从执行计划读取**:

```sql
-- 示例查询
SELECT ...
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN order_items oi ON o.order_id = oi.order_id
JOIN products p ON oi.product_id = p.product_id
WHERE ...;

-- 执行计划 (从下往上读)
Hash Join                                    ← JOIN 3 (最外层)
  Hash Cond: (oi.product_id = p.product_id)
  -> Hash Join                               ← JOIN 2
      Hash Cond: (o.order_id = oi.order_id)
      -> Hash Join                           ← JOIN 1 (最内层)
          Hash Cond: (o.customer_id = c.customer_id)
          -> Seq Scan on orders o            ← 扫描orders
          -> Hash                            ← 构建customers哈希表
              -> Seq Scan on customers c
      -> Hash
          -> Seq Scan on order_items oi
  -> Hash
      -> Seq Scan on products p
```

**解读JOIN顺序**:
```
JOIN顺序 (从内到外):
  1. orders × customers (最内层)
  2. (orders × customers) × order_items
  3. ((orders × customers) × order_items) × products (最外层)

对应物理顺序:
  customers → orders → order_items → products
```

### 2.3 JOIN顺序Cost计算

**源码位置**: `src/gausskernel/optimizer/path/joinrels.cpp`

```cpp
// JOIN顺序选择的动态规划算法
// make_rel_from_joinlists

// 状态定义
struct JoinPermutation {
    List* join_order;        // JOIN顺序 [table1, table2, table3, ...]
    double total_cost;       // 总Cost
    double intermediate_rows; // 中间结果行数
};

// 算法: 对于n个表JOIN
// 如果 n <= from_collapse_limit (默认8)
//   使用动态规划 (尝试所有排列)
// 否则
//   使用GEQO遗传算法 (不尝试所有排列)
```

**动态规划Cost计算**:

```sql
-- 3表JOIN的Cost计算示例
-- 表: orders(100万行), customers(1000行), order_items(500万行)
-- 假设JOIN选择性: 0.01

-- 顺序1: customers(1000) → orders(100万) → order_items(500万)
-- JOIN 1: customers × orders
JOIN1_rows = 1000 × 100万 × 0.01 = 10000
JOIN1_cost = hash_build(customers) + hash_probe(orders)
          = 0.0025 × 1000 + 0.0025 × 100万
          = 2.5 + 2500 = 2502.5

-- JOIN 2: (customers × orders) × order_items
JOIN2_rows = 10000 × 500万 × 0.01 = 500000
JOIN2_cost = JOIN1_cost + hash_build(JOIN1_result) + hash_probe(order_items)
          = 2502.5 + 0.0025 × 10000 + 0.0025 × 500万
          = 2502.5 + 25 + 12500 = 15027.5

-- 总Cost = 15027.5

-- 顺序2: order_items(500万) → orders(100万) → customers(1000)
-- JOIN 1: order_items × orders
JOIN1_rows = 500万 × 100万 × 0.01 = 500亿 (灾难！)
JOIN1_cost = hash_build(order_items) + hash_probe(orders)
          = 0.0025 × 500万 + 0.0025 × 100万
          = 12500 + 2500 = 15000

-- JOIN 2: (order_items × orders) × customers
JOIN2_rows = 500亿 × 1000 × 0.01 = 5000亿 (更灾难！)
JOIN2_cost = 15000 + hash_build(JOIN1_result) + hash_probe(customers)
          = 15000 + 0.0025 × 500亿 + 0.0025 × 1000
          = 15000 + 1250000 + 2.5 = 1265002.5

-- 总Cost = 1265002.5 (是顺序1的84倍！)

-- 结论: 顺序1比顺序2快84倍
```

### 2.4 JOIN顺序判断标准

**最优JOIN顺序的标准**:

```
1. 小表优先 (rows少的表先JOIN)
   ├─ 小表作为build侧 (构建哈希表)
   ├─ 大表作为probe侧 (探测哈希表)
   └─ 原因: 小表构建哈希表成本低，内存占用少

2. 高选择性JOIN优先
   ├─ JOIN后行数增长少的优先
   ├─ 避免中间结果膨胀
   └─ 保持中间结果集小

3. 索引可利用优先
   ├─ 有索引的列优先JOIN
   ├─ 可以使用Nested Loop
   └─ 避免不必要的Hash Join

4. 外连接顺序固定
   ├─ LEFT JOIN: 左表必须在前
   ├─ RIGHT JOIN: 右表必须在前
   └─ FULL JOIN: 顺序固定，无法优化
```

**JOIN顺序评估函数**:

```cpp
// src/gausskernel/optimizer/path/costsize.cpp

// 评估JOIN顺序的Cost
double cost_join_order(List* join_order)
{
    double total_cost = 0;
    double current_rows = 1;
    
    // 按顺序评估每个JOIN
    for (int i = 0; i < list_length(join_order); i++) {
        RelOptInfo* outer_rel = get_outer_rel(join_order, i);
        RelOptInfo* inner_rel = get_inner_rel(join_order, i);
        
        // 1. 估算JOIN后行数
        double join_rows = estimate_join_rows(outer_rel, inner_rel);
        
        // 2. 计算JOIN Cost
        double join_cost = calc_join_cost(outer_rel, inner_rel, join_rows);
        
        // 3. 累加
        total_cost += join_cost;
        current_rows = join_rows;
        
        // 4. 检查是否有中间结果膨胀
        if (join_rows > current_rows * 10) {
            // 警告: 中间结果膨胀10倍以上
            elog(LOG, "JOIN order warning: intermediate result explodes");
        }
    }
    
    return total_cost;
}
```

### 2.5 JOIN顺序优化实战

**案例分析**:

```sql
-- 问题SQL
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT 
    o.order_id,
    c.customer_name,
    p.product_name,
    oi.quantity,
    oi.price
FROM orders o
INNER JOIN customers c ON o.customer_id = c.customer_id
INNER JOIN order_items oi ON o.order_id = oi.order_id
INNER JOIN products p ON oi.product_id = p.product_id
WHERE o.order_date >= '2024-01-01'
  AND c.city = 'Beijing'
  AND p.category = 'Electronics';

-- 执行计划
Hash Join  (cost=0.00..1234567.89 rows=100 width=200) (actual time=1234.567..5678.901 rows=100 loops=1)
  Hash Cond: (oi.product_id = p.product_id)
  -> Hash Join  (cost=0.00..1234567.88 rows=1000 width=150)
        Hash Cond: (o.order_id = oi.order_id)
        -> Seq Scan on orders o  (cost=0.00..1000000.00 rows=1000000 width=50)
              Filter: (order_date >= '2024-01-01'::date)
        -> Hash  (cost=0.00..200000.00 rows=5000000 width=50)
              -> Seq Scan on order_items oi  (cost=0.00..100000.00 rows=5000000 width=50)
  -> Hash
      -> Seq Scan on products p  (cost=0.00..50000.00 rows=10000 width=50)
            Filter: (category = 'Electronics'::text)
```

**问题分析**:

```
步骤1: 识别当前JOIN顺序
  从下往上:
    1. orders × order_items (最内层)
    2. (orders × order_items) × products
    3. ((orders × order_items) × products) × customers (最外层，但在计划中没看到？)
  
  实际顺序: orders → order_items → products → (customers未出现？)
  
步骤2: 检查表大小
  - orders: 100万行
  - order_items: 500万行
  - products: 1万行
  - customers: 1000行 (从pg_class查询)
  
步骤3: 评估当前顺序
  问题1: orders(100万) × order_items(500万) → 50亿行 (灾难！)
  问题2: 最小的customers表(1000行)最后JOIN
  
步骤4: 计算正确顺序的Cost
  正确顺序: customers(1000) → products(1万) → orders(100万) → order_items(500万)
  
  JOIN1: customers × products
    rows = 1000 × 10000 × 0.1 = 1000 (假设city和category选择性)
    cost = 0.0025 × 1000 + 0.0025 × 10000 = 2.5 + 25 = 27.5
  
  JOIN2: (customers × products) × orders
    rows = 1000 × 100万 × 0.5 = 50万 (order_date选择性)
    cost = 27.5 + 0.0025 × 1000 + 0.0025 × 100万 = 27.5 + 2.5 + 2500 = 2530
  
  JOIN3: ((customers × products) × orders) × order_items
    rows = 50万 × 500万 × 0.1 = 2500万 (假设每个订单有5个商品)
    cost = 2530 + 0.0025 × 50万 + 0.0025 × 500万 = 2530 + 1250 + 12500 = 16280
  
  正确顺序总Cost = 16280
  
  当前顺序Cost = 1234567.89
  
  性能差距: 1234567.89 / 16280 = 75.8倍！
  
步骤5: 结论
  🔴 JOIN顺序错误是第一瓶颈
  正确顺序: customers → products → orders → order_items
  当前顺序: orders → order_items → products → customers
  性能差距: 75.8倍
```

### 2.6 JOIN顺序优化方案

**方案1: 使用LEADING Hint (不改写SQL)**

```sql
/*+ LEADING(c p o oi) */
SELECT 
    o.order_id,
    c.customer_name,
    p.product_name,
    oi.quantity,
    oi.price
FROM orders o
INNER JOIN customers c ON o.customer_id = c.customer_id
INNER JOIN order_items oi ON o.order_id = oi.order_id
INNER JOIN products p ON oi.product_id = p.product_id
WHERE o.order_date >= '2024-01-01'
  AND c.city = 'Beijing'
  AND p.category = 'Electronics';

-- 说明: LEADING(c p o ai) 强制JOIN顺序为:
--   customers × products → orders → order_items
```

**方案2: 使用CTE强制JOIN顺序 (改写SQL)**

```sql
WITH step1 AS (
    -- 先JOIN小表
    SELECT c.*, p.*
    FROM customers c
    CROSS JOIN products p
    WHERE c.city = 'Beijing' 
      AND p.category = 'Electronics'
),
step2 AS (
    -- 再JOIN orders
    SELECT s1.*, o.*
    FROM step1 s1
    JOIN orders o ON s1.customer_id = o.customer_id
    WHERE o.order_date >= '2024-01-01'
)
-- 最后JOIN order_items
SELECT s2.*, oi.quantity, oi.price
FROM step2 s2
JOIN order_items oi ON s2.order_id = oi.order_id;
```

**方案3: 调整统计信息 (让优化器自动选择正确顺序)**

```sql
-- 问题: 优化器可能低估了某些表的大小
-- 解决: 更新统计信息

ANALYZE orders;
ANALYZE customers;
ANALYZE order_items;
ANALYZE products;

-- 增加统计目标 (更精确的MCV和直方图)
ALTER TABLE orders ALTER COLUMN order_date SET STATISTICS 10000;
ALTER TABLE customers ALTER COLUMN city SET STATISTICS 10000;
ALTER TABLE products ALTER COLUMN category SET STATISTICS 10000;

-- 再次分析
ANALYZE orders, customers, products;

-- 验证统计信息
SELECT 
    schemaname,
    tablename,
    attname AS column_name,
    n_distinct,
    null_frac,
    correlation,
    avg_width
FROM pg_stats
WHERE tablename IN ('orders', 'customers', 'order_items', 'products')
  AND attname IN ('order_date', 'city', 'category')
ORDER BY tablename, attname;
```

**方案4: 调整优化器参数**

```sql
-- 启用GEQO (对大量表JOIN有效)
SET geqo = on;
SET geqo_threshold = 12;  -- 12个表以上使用GEQO
SET geqo_effort = 10;     -- GEQO优化努力程度 (1-10)
SET geqo_pool_size = 200; -- GEQO种群大小
SET geqo_generations = 0; -- 0表示自动计算

-- 调整JOIN collapse限制
SET join_collapse_limit = 8;  -- 默认8
SET from_collapse_limit = 8;  -- 默认8

-- 禁用某些JOIN方式
SET enable_nestloop = off;  -- 禁用Nested Loop (强制使用Hash Join)
SET enable_hashjoin = on;   -- 启用Hash Join
SET enable_mergejoin = on;  -- 启用Merge Join
```

---

## 第三部分：JOIN方式深度分析（第二优先级）

### 3.1 三种JOIN方式对比

| JOIN方式 | 适用场景 | Cost公式 | 优点 | 缺点 |
|---------|---------|---------|------|------|
| Nested Loop | 小表×大表，内表有高效索引 | outer_cost + outer_rows × inner_cost | 无需内存，支持所有JOIN类型 | 内表重复扫描，外行数多时慢 |
| Hash Join | 大表×大表，等值JOIN | hash_build + hash_probe | 一次扫描，大表JOIN快 | 需要内存，不支持不等值JOIN |
| Merge Join | 大表×大表，已排序或可排序 | sort_outer + sort_inner + merge | 有序输出，支持不等值JOIN | 需要排序或索引 |

### 3.2 Nested Loop Cost计算详解

**源码位置**: `src/gausskernel/optimizer/path/joinpath.cpp`

```cpp
// cost_nestloop
void cost_nestloop(Path* path, PlannerInfo* root, Path* outer_path, Path* inner_path)
{
    // 参数提取
    double outer_rows = outer_path->rows;
    double inner_rows = inner_path->rows;
    double outer_startup_cost = outer_path->startup_cost;
    double outer_total_cost = outer_path->total_cost;
    double inner_startup_cost = inner_path->startup_cost;
    double inner_total_cost = inner_path->total_cost;
    
    // JOIN选择性
    Selectivity join_selec = clause_selectivity(root, join_clause, 0);
    double join_rows = outer_rows * inner_rows * join_selec;
    
    // Cost计算
    // startup_cost = outer_startup_cost
    // run_cost = outer_run_cost + outer_rows × inner_startup_cost + outer_rows × inner_run_cost
    
    double run_cost = outer_total_cost - outer_startup_cost;  // 外表扫描一次
    run_cost += outer_rows * inner_startup_cost;              // 内表启动outer_rows次
    run_cost += outer_rows * (inner_total_cost - inner_startup_cost); // 内表扫描outer_rows次
    run_cost += cpu_tuple_cost * join_rows;                   // 结果处理成本
    
    path->startup_cost = outer_startup_cost;
    path->total_cost = outer_startup_cost + run_cost;
    path->rows = join_rows;
}
```

**详细计算示例**:

```sql
-- 场景
-- 外表: customers (1000行)
-- 内表: orders (100万行)
-- JOIN条件: customer_id
-- 内表索引: idx_orders_customer_id

-- 执行计划
Nested Loop
  (cost=0.00..15000.00 rows=10000 width=200)
  -> Seq Scan on customers (cost=0.00..100.00 rows=1000 width=50)
  -> Index Scan using idx_orders_customer_id on orders
        (cost=0.00..15.00 rows=10 width=150)

-- Cost逐步计算:

// 步骤1: 外表扫描 (customers)
outer_rows = 1000
outer_startup_cost = 0 (Seq Scan)
outer_run_cost = seq_page_cost × pages + cpu_tuple_cost × rows
               = 1.0 × 100 + 0.01 × 1000
               = 100 + 10 = 110
outer_total_cost = 0 + 110 = 110
但执行计划显示: cost=0.00..100.00
→ 说明pages=100, tuples=1000, 但计算方式可能略有不同

// 步骤2: 内表扫描 (orders) - 每次outer_rows都要执行
inner_rows = 100万
inner_startup_cost = 索引扫描启动成本
inner_run_cost = 索引扫描运行成本

// 单次Index Scan cost:
//  假设索引高度=3, 每个customer_id平均有10个order
index_page_cost = random_page_cost × 索引高度 = 1.5 × 3 = 4.5
heap_page_cost = random_page_cost × 1 = 1.5 (假设每次读1个heap page)
cpu_index_cost = cpu_operator_cost × 索引行数 = 0.0025 × 10 = 0.025
cpu_tuple_cost = 0.01 × 10 = 0.1

单次index scan cost = 4.5 + 1.5 + 0.025 + 0.1 = 6.125

// 但是执行计划显示 cost=0.00..15.00
// 说明估算行数=10, cost计算方式:
//   startup = 0
//   total = cpu_operator_cost × inner_rows = 0.0025 × 10 = 0.025
//   这明显不对，应该还有索引和heap的I/O成本

// 让我们用更准确的公式:
//   Index Scan cost = index_pages × random_page_cost + heap_pages × random_page_cost + cpu成本
//   假设索引扫描返回10行, 需要访问10个heap page
//   index_pages = 索引高度 + (返回行数 / 索引扇出) = 3 + (10 / 100) = 3.1
//   heap_pages = 10 (最坏情况每行一个page)
//   index_cost = 3.1 × 1.5 = 4.65
//   heap_cost = 10 × 1.5 = 15
//   cpu_cost = 0.0025 × 10 + 0.01 × 10 = 0.125
//   total = 4.65 + 15 + 0.125 = 19.775

// 执行计划显示: cost=0.00..15.00
// 说明优化器估算比较乐观

// 步骤3: Nested Loop总Cost
startup_cost = outer_startup_cost = 0

run_cost = outer_run_cost + outer_rows × inner_startup_cost + outer_rows × inner_run_cost
        = (100 - 0) + 1000 × 0 + 1000 × (15 - 0)
        = 100 + 0 + 15000
        = 15100

total_cost = 0 + 15100 = 15100

执行计划显示: cost=0.00..15000.00 ✓ 匹配！

// 实际执行时间
actual_time = 每次outer行都要执行index scan
          = 1000 × (index scan time)
          = 1000 × 0.1ms (假设)
          = 100ms

// 如果inner table没有索引:
//   inner_scan = Seq Scan on orders (100万行)
//   inner_cost = 1.0 × 10000 + 0.01 × 100万 = 10000 + 10000 = 20000
//   run_cost = 100 + 1000 × 20000 = 20,000,100
//   total_cost = 20,000,100
//   → 性能灾难！
```

### 3.3 Hash Join Cost计算详解

```cpp
// cost_hashjoin
void cost_hashjoin(Path* path, PlannerInfo* root, Path* outer_path, Path* inner_path)
{
    double outer_rows = outer_path->rows;
    double inner_rows = inner_path->rows;
    double outer_width = outer_path->parent->width;
    double inner_width = inner_path->parent->width;
    
    // JOIN选择性
    Selectivity join_selec = clause_selectivity(root, join_clause, 0);
    double join_rows = outer_rows * inner_rows * join_selec;
    
    // 哈希表大小估算
    int hash_entry_size = inner_width + sizeof(HashJoinTupleData);
    double hash_table_size = inner_rows * hash_entry_size;
    
    // 哈希表是否溢出到磁盘
    bool hash_spill = (hash_table_size > work_mem * 1024);
    
    // Cost计算
    // startup_cost = outer_startup + inner_total + hash_build_cost
    // run_cost = outer_run + hash_probe_cost + result_process_cost
    
    double hash_build_cost = cpu_operator_cost * inner_rows;
    if (hash_spill) {
        // 磁盘溢出，成本×10
        hash_build_cost *= 10;
    }
    
    double startup_cost = outer_path->startup_cost + inner_path->total_cost + hash_build_cost;
    
    double run_cost = outer_path->total_cost - outer_path->startup_cost;  // 外表扫描
    run_cost += cpu_operator_cost * outer_rows;  // 哈希探测
    run_cost += cpu_tuple_cost * join_rows;      // 结果处理
    
    path->startup_cost = startup_cost;
    path->total_cost = startup_cost + run_cost;
    path->rows = join_rows;
}
```

**详细计算示例**:

```sql
-- 场景
-- 外表(构建哈希): customers (1000行)
-- 内表(探测): orders (100万行)
-- JOIN条件: customer_id

-- 执行计划
Hash Join
  (cost=110.00..1234.56 rows=10000 width=200)
  Hash Cond: (orders.customer_id = customers.customer_id)
  -> Seq Scan on orders (cost=0.00..10000.00 rows=1000000 width=100)
  -> Hash
      -> Seq Scan on customers (cost=0.00..110.00 rows=1000 width=50)

-- Cost逐步计算:

// 步骤1: 内表扫描 (customers, 构建哈希表)
inner_rows = 1000
inner_width = 50 bytes
inner_pages = 100
inner_cost = seq_page_cost × pages + cpu_tuple_cost × rows
          = 1.0 × 100 + 0.01 × 1000
          = 100 + 10 = 110
执行计划显示: cost=0.00..110.00 ✓

// 步骤2: 哈希表构建
hash_entry_size = inner_width + sizeof(HashJoinTupleData)
                = 50 + 24 = 74 bytes
hash_table_size = 1000 × 74 = 74000 bytes = 72 KB

work_mem = 4MB (默认)
hash_table_size (72KB) < work_mem (4MB) → 不会溢出 ✓

hash_build_cost = cpu_operator_cost × inner_rows
                = 0.0025 × 1000 = 2.5

// 步骤3: 外表扫描 (orders, 探测哈希表)
outer_rows = 100万
outer_pages = 10000
outer_startup_cost = 0 (Seq Scan)
outer_run_cost = seq_page_cost × pages + cpu_tuple_cost × rows
               = 1.0 × 10000 + 0.01 × 100万
               = 10000 + 10000 = 20000
outer_total_cost = 0 + 20000 = 20000
但执行计划显示: cost=0.00..10000.00
→ 可能cpu_tuple_cost计算方式不同

// 步骤4: 哈希探测
hash_probe_cost = cpu_operator_cost × outer_rows
                = 0.0025 × 100万 = 2500

// 步骤5: 结果处理
join_rows = 1000 × 100万 × 0.01 = 10000 (假设选择性0.01)
result_cost = cpu_tuple_cost × join_rows = 0.01 × 10000 = 100

// 步骤6: 总Cost
startup_cost = outer_startup_cost + inner_total_cost + hash_build_cost
            = 0 + 110 + 2.5 = 112.5

run_cost = outer_run_cost + hash_probe_cost + result_cost
        = 10000 + 2500 + 100 = 12600

total_cost = 112.5 + 12600 = 12712.5

执行计划显示: cost=110.00..1234.56
→ 数字不太匹配, 可能是简化示例

// 关键: 如果work_mem不够
work_mem = 64KB
hash_table_size = 72KB > work_mem → 溢出

hash_build_cost = 2.5 × 10 = 25 (溢出成本×10)
startup_cost = 0 + 110 + 25 = 135
run_cost = 10000 + 2500 + 100 + disk_spill_cost
         = 12600 + 磁盘I/O成本

// 磁盘溢出成本估算
// 假设需要写入100个hash partition
disk_write_cost = 100 × (random_page_cost + seq_page_cost)
                = 100 × (1.5 + 1.0) = 250

disk_read_cost = 100 × (random_page_cost + seq_page_cost)
               = 250

total_disk_cost = 250 + 250 = 500

run_cost = 12600 + 500 = 13100
total_cost = 135 + 13100 = 13235

// 结论: work_mem不足导致Cost增加约5%
```

### 3.4 Merge Join Cost计算详解

```cpp
// cost_mergejoin
void cost_mergejoin(Path* path, PlannerInfo* root, Path* outer_path, Path* inner_path)
{
    double outer_rows = outer_path->rows;
    double inner_rows = inner_path->rows;
    double outer_width = outer_path->parent->width;
    double inner_width = inner_path->parent->width;
    
    // JOIN选择性
    Selectivity join_selec = clause_selectivity(root, join_clause, 0);
    double join_rows = outer_rows * inner_rows * join_selec;
    
    // 检查是否需要排序
    List* outer_pathkeys = outer_path->pathkeys;
    List* inner_pathkeys = inner_path->pathkeys;
    List* required_pathkeys = make_pathkeys_for_mergeclauses();
    
    bool outer_needs_sort = !pathkeys_contained_in(required_pathkeys, outer_pathkeys);
    bool inner_needs_sort = !pathkeys_contained_in(required_pathkeys, inner_pathkeys);
    
    // Cost计算
    // startup_cost = outer_startup + inner_startup + sort_startup (如果需要)
    // run_cost = outer_run + inner_run + sort_run (如果需要) + merge_cost
    
    double startup_cost = outer_path->startup_cost + inner_path->startup_cost;
    double run_cost = outer_path->total_cost - outer_path->startup_cost +
                     inner_path->total_cost - inner_path->startup_cost;
    
    // 排序成本
    if (outer_needs_sort) {
        Cost outer_sort_cost = cost_sort(work_mem, outer_rows, outer_width);
        startup_cost += outer_sort_cost.startup_cost;
        run_cost += outer_sort_cost.total_cost - outer_sort_cost.startup_cost;
    }
    
    if (inner_needs_sort) {
        Cost inner_sort_cost = cost_sort(work_mem, inner_rows, inner_width);
        startup_cost += inner_sort_cost.startup_cost;
        run_cost += inner_sort_cost.total_cost - inner_sort_cost.startup_cost;
    }
    
    // Merge成本 (线性扫描)
    double merge_cost = cpu_operator_cost * (outer_rows + inner_rows);
    run_cost += merge_cost;
    
    path->startup_cost = startup_cost;
    path->total_cost = startup_cost + run_cost;
    path->rows = join_rows;
}
```

**详细计算示例**:

```sql
-- 场景
-- 外表: customers (1000行, 无序)
-- 内表: orders (100万行, 无序)
-- JOIN条件: customer_id
-- 需要排序

-- 执行计划
Merge Join
  (cost=25000.00..35000.00 rows=10000 width=200)
  Merge Cond: (orders.customer_id = customers.customer_id)
  -> Sort (cost=12345.00..12678.00 rows=1000000 width=100)
        Sort Key: orders.customer_id
        -> Seq Scan on orders (cost=0.00..10000.00 rows=1000000 width=100)
  -> Sort (cost=100.00..105.00 rows=1000 width=50)
        Sort Key: customers.customer_id
        -> Seq Scan on customers (cost=0.00..100.00 rows=1000 width=50)

// Cost逐步计算:

// 步骤1: 外表排序 (orders)
outer_rows = 100万
outer_width = 100 bytes
sort_mem = 100万 × 100 = 100MB

work_mem = 4MB
sort_mem > work_mem → 磁盘排序

outer_sort_cost = cpu_operator_cost × outer_rows × log2(outer_rows)
                = 0.0025 × 100万 × log2(100万)
                = 2500 × 19.93 = 49825

disk_sort_cost = random_page_cost × (sort_mem / work_mem) × outer_rows
               = 1.5 × (100 / 4) × 100万 = 37,500,000

// 但执行计划显示 cost=12345.00..12678.00
// 说明是简化示例, 实际计算更复杂

// 步骤2: 内表排序 (customers)
inner_rows = 1000
inner_width = 50 bytes
sort_mem = 1000 × 50 = 50KB

work_mem = 4MB
sort_mem < work_mem → 内存排序

inner_sort_cost = cpu_operator_cost × inner_rows × log2(inner_rows)
                = 0.0025 × 1000 × log2(1000)
                = 2.5 × 9.97 = 24.9

执行计划显示: cost=100.00..105.00

// 步骤3: Merge扫描
merge_cost = cpu_operator_cost × (outer_rows + inner_rows)
          = 0.0025 × (100万 + 1000) = 2502.5

// 步骤4: 总Cost
startup_cost = 12345 + 100 = 12445
run_cost = (12678 - 12345) + (105 - 100) + 2502.5 = 333 + 5 + 2502.5 = 2840.5
total_cost = 12445 + 2840.5 = 15285.5

执行计划显示: cost=25000.00..35000.00
→ 数字不太匹配, 可能是不同参数设置

// 如果有索引 (避免排序)
CREATE INDEX idx_orders_customer_id ON orders(customer_id);
CREATE INDEX idx_customers_customer_id ON customers(customer_id);

// 优化后执行计划
Merge Join
  -> Index Scan using idx_orders_customer_id on orders
  -> Index Scan using idx_customers_customer_id on customers

// 无需排序, Cost大幅降低
idx_scan_cost = 1.5 × 索引页数 + cpu成本
              ≈ 150 + 2500 = 2650 (orders)
              ≈ 15 + 2.5 = 17.5 (customers)

total_cost ≈ 2650 + 17.5 + merge_cost ≈ 2675 + 2500 = 5175

// 对比: 35000 → 5175 (减少85%)
```

### 3.5 JOIN方式选择决策树

```cpp
// src/gausskernel/optimizer/path/joinpath.cpp
// add_paths_to_joinrel

// JOIN方式选择逻辑
void choose_join_method(
    RelOptInfo* joinrel,
    RelOptInfo* outer_rel,
    RelOptInfo* inner_rel,
    List* restrictlist)
{
    // 1. 检查是否可以使用Nested Loop
    if (内表有高效索引 || 外表行数很少) {
        // 评估Nested Loop Cost
        Cost nl_cost = cost_nestloop(outer_rel, inner_rel);
        add_nestloop_path(joinrel, nl_cost);
    }
    
    // 2. 检查是否可以使用Hash Join
    if (JOIN条件是等值 && 内存足够) {
        // 评估Hash Join Cost
        Cost hj_cost = cost_hashjoin(outer_rel, inner_rel);
        add_hashjoin_path(joinrel, hj_cost);
    }
    
    // 3. 检查是否可以使用Merge Join
    if (可以排序 || 已有索引) {
        // 评估Merge Join Cost
        Cost mj_cost = cost_mergejoin(outer_rel, inner_rel);
        add_mergejoin_path(joinrel, mj_cost);
    }
    
    // 4. 选择Cost最低的方式
    Path* best_path = min_cost_path(joinrel->pathlist);
}
```

**决策示例**:

```sql
-- 场景1: 小表×大表,内表有索引 → Nested Loop
-- 外表: customers (1000行)
-- 内表: orders (100万行), 有idx_orders_customer_id
-- JOIN条件: customer_id

Nested Loop Cost = 100 + 1000 × 15 = 15100
Hash Join Cost = 110 + 20000 + 2500 = 22610
Merge Join Cost = 12445 + 2840 = 15285

→ 选择Nested Loop (Cost最低)

-- 场景2: 大表×大表,无索引 → Hash Join
-- 外表: orders (100万行)
-- 内表: order_items (500万行), 无索引
-- JOIN条件: order_id

Nested Loop Cost = 10000 + 100万 × 50000 = 50,000,010,000 (灾难)
Hash Join Cost = 10000 + 50000 + 12500 = 72500
Merge Join Cost = 排序成本巨大 > 100000

→ 选择Hash Join (Cost最低)

-- 场景3: 大表×大表,有序 → Merge Join
-- 外表: orders (100万行), 已按order_id排序
-- 内表: order_items (500万行), 已按order_id排序
-- JOIN条件: order_id

Nested Loop Cost = 灾难
Hash Join Cost = 72500
Merge Join Cost = 0 (无需排序) + merge_cost = 12500

→ 选择Merge Join (Cost最低)
```

---

## 第四部分：扫描节点深度分析（第三优先级）

### 4.1 Seq Scan Cost计算

```cpp
// cost_seqscan
void cost_seqscan(Path* path, PlannerInfo* root, RelOptInfo* baserel)
{
    BlockNumber pages = baserel->pages;
    double tuples = baserel->tuples;
    
    // Cost计算
    // startup_cost = 0 (顺序扫描无需启动)
    // run_cost = seq_page_cost × pages + cpu_tuple_cost × tuples
    
    double run_cost = seq_page_cost * pages + cpu_tuple_cost * tuples;
    
    path->startup_cost = 0;
    path->total_cost = run_cost;
    path->rows = tuples;
}
```

**详细计算示例**:

```sql
-- 场景: Seq Scan on orders
-- 表信息:
--   pages = 10000
--   tuples = 100万

-- 参数值:
--   seq_page_cost = 1.0 (默认)
--   cpu_tuple_cost = 0.01 (默认)

-- Cost计算:
startup_cost = 0
run_cost = 1.0 × 10000 + 0.01 × 100万
        = 10000 + 10000 = 20000
total_cost = 20000

-- 执行计划
Seq Scan on orders  (cost=0.00..20000.00 rows=1000000 width=100) ✓

// 如果有Filter条件
WHERE customer_id = 12345

// 选择率计算
n_distinct(customer_id) = 10000
selectivity = 1 / 10000 = 0.0001
estimated_rows = 100万 × 0.0001 = 100

// Cost不变 (仍然扫描全表)
// 但rows=100

// 如果有统计信息偏差
// 实际上customer_id的分布不均匀
// customer_id = 12345实际有10000行 (占1%)

// 统计信息问题:
//   n_distinct = 10000 (假设均匀分布)
//   实际上customer_id = 12345有10000行
//   估算rows = 100
//   实际rows = 10000
//   偏差 = 100倍！

// 优化:
// 1. 增加统计目标
ALTER TABLE orders ALTER COLUMN customer_id SET STATISTICS 10000;
ANALYZE orders;

// 2. 检查MCV
SELECT most_common_vals, most_common_freqs FROM pg_stats
WHERE tablename = 'orders' AND attname = 'customer_id';

// 如果customer_id = 12345在MCV中
//   selectivity = mcv_frequency = 0.01
//   estimated_rows = 100万 × 0.01 = 10000 ✓

// 3. 创建索引 (避免Seq Scan)
CREATE INDEX idx_orders_customer_id ON orders(customer_id);

// 优化后
Index Scan using idx_orders_customer_id on orders
  (cost=150.00..152.50 rows=10000 width=100)
  Index Cond: (customer_id = 12345)

// Cost对比: 20000 → 152.5 (减少99.2%)
```

### 4.2 Index Scan Cost计算

```cpp
// cost_index
void cost_index(Path* path, PlannerInfo* root, RelOptInfo* baserel, IndexOptInfo* index)
{
    // 参数提取
    BlockNumber index_pages = index->pages;
    double index_tuples = index->tuples;
    BlockNumber heap_pages = baserel->pages;
    double heap_tuples = baserel->tuples;
    
    // 选择率计算
    Selectivity index_sel = clause_selectivity(root, index_clause, 0);
    double index_rows = index_tuples * index_sel;
    
    // 索引相关性 (物理顺序与逻辑顺序的相关性)
    double index_correlation = index->index_correlation;
    
    // Cost计算
    // startup_cost = 索引扫描启动成本
    // run_cost = 索引扫描运行成本 + heap扫描成本 + CPU成本
    
    // 1. 索引扫描成本
    double index_startup_cost = index_page_cost * index_pages * index_sel;
    double index_run_cost = cpu_index_tuple_cost * index_rows;
    
    // 2. heap扫描成本
    // 考虑索引相关性: 相关性越高,随机I/O越少
    double pages_fetched = heap_pages * index_sel;
    double heap_fetch_cost = random_page_cost * pages_fetched;
    if (index_correlation > 0) {
        // 相关性降低随机I/O
        heap_fetch_cost *= (1.0 - index_correlation);
    }
    
    // 3. CPU处理成本
    double cpu_run_cost = cpu_tuple_cost * index_rows;
    
    path->startup_cost = index_startup_cost;
    path->total_cost = index_startup_cost + index_run_cost + heap_fetch_cost + cpu_run_cost;
    path->rows = index_rows;
}
```

**详细计算示例**:

```sql
-- 场景: Index Scan on orders using idx_orders_customer_id
-- 索引信息:
--   index_pages = 100
--   index_tuples = 100万
--   index_correlation = 0.8 (高相关性)
-- 表信息:
--   heap_pages = 10000
--   heap_tuples = 100万

-- 查询条件:
--   WHERE customer_id = 12345

-- 参数值:
--   random_page_cost = 1.5 (SSD默认)
--   cpu_index_tuple_cost = 0.005
--   cpu_tuple_cost = 0.01

-- Cost逐步计算:

// 步骤1: 选择率计算
n_distinct(customer_id) = 10000
index_sel = 1 / 10000 = 0.0001
index_rows = 100万 × 0.0001 = 100

// 步骤2: 索引扫描成本
index_startup_cost = 1.5 × 100 × 0.0001 = 0.015
index_run_cost = 0.005 × 100 = 0.5

// 步骤3: heap扫描成本
pages_fetched = 10000 × 0.0001 = 1
heap_fetch_cost = 1.5 × 1 = 1.5

// 考虑相关性
heap_fetch_cost *= (1.0 - 0.8) = 1.5 × 0.2 = 0.3

// 步骤4: CPU成本
cpu_run_cost = 0.01 × 100 = 1

// 步骤5: 总Cost
startup_cost = 0.015
total_cost = 0.015 + 0.5 + 0.3 + 1 = 1.815

// 但实际执行计划显示更大的数字
// 说明参数设置不同或计算方式有差异

// 实际执行计划
Index Scan using idx_orders_customer_id on orders
  (cost=0.00..152.50 rows=100 width=100)
  Index Cond: (customer_id = 12345)

// 重新计算 (可能是不同的参数)
// 假设:
//   index_page_cost = 1.5 (每次index scan都要访问index root)
//   index_pages_to_fetch = 索引高度 = 3

index_startup_cost = 1.5 × 3 = 4.5 (从root到leaf)
pages_fetched = 100 (最坏情况,每行不同page)
heap_fetch_cost = 1.5 × 100 = 150
total_cost = 4.5 + 0.5 + 150 + 1 = 156

// 接近152.50 ✓

// 如果相关性=0 (随机分布)
heap_fetch_cost = 1.5 × 100 = 150 (不变)

// 如果相关性=1 (完美相关,物理顺序=逻辑顺序)
//   → 可能都是相邻的100行,只需读取几个page
//   pages_fetched = ceil(100 / 100) = 1 (假设每页100行)
//   heap_fetch_cost = 1.5 × 1 = 1.5
//   total_cost = 4.5 + 0.5 + 1.5 + 1 = 7.5

// 结论: 相关性对Cost影响巨大！
```

### 4.3 Bitmap Scan Cost计算

```cpp
// cost_bitmap_heap_scan
void cost_bitmap_heap_scan(Path* path, PlannerInfo* root, RelOptInfo* baserel, Path* bitmapqualpath)
{
    // 参数提取
    BlockNumber heap_pages = baserel->pages;
    double heap_tuples = baserel->tuples;
    double bitmap_rows = bitmapqualpath->rows;
    
    // Cost计算
    // startup_cost = bitmap构建成本
    // run_cost = heap扫描成本 + CPU成本
    
    // 1. bitmap构建成本
    double bitmap_build_cost = cpu_operator_cost * bitmap_rows;
    
    // 2. heap扫描成本
    // bitmap扫描是按顺序的,不是随机的
    double pages_fetched = heap_pages * (bitmap_rows / heap_tuples);
    double heap_fetch_cost = seq_page_cost * pages_fetched; // 顺序扫描成本
    
    // 3. CPU成本
    double cpu_run_cost = cpu_tuple_cost * bitmap_rows;
    
    // 4. recheck cost (如果需要recheck)
    double bitmap_recheck_cost = 0;
    if (need_recheck) {
        bitmap_recheck_cost = cpu_operator_cost * bitmap_rows * 0.5;
    }
    
    path->startup_cost = bitmapqualpath->total_cost + bitmap_build_cost;
    path->total_cost = path->startup_cost + heap_fetch_cost + cpu_run_cost + bitmap_recheck_cost;
    path->rows = bitmap_rows;
}
```

**详细计算示例**:

```sql
-- 场景: Bitmap Scan on orders
-- 查询条件:
--   WHERE customer_id IN (1, 2, 3, ..., 1000)
-- 表信息:
--   heap_pages = 10000
--   heap_tuples = 100万

-- Cost逐步计算:

// 步骤1: Bitmap Index Scan
// 1000个值,每个值的selectivity = 1/10000 = 0.0001
// 总selectivity = 1000 × 0.0001 = 0.1
bitmap_rows = 100万 × 0.1 = 10万

// Bitmap Index Scan Cost
index_pages = 100
bitmap_index_cost = 1.5 × 100 + 0.005 × 10万 = 150 + 500 = 650

// 步骤2: Bitmap Heap Scan
bitmap_build_cost = 0.0025 × 10万 = 250

pages_fetched = 10000 × 0.1 = 1000 (需要读取1000个page)
heap_fetch_cost = 1.0 × 1000 = 1000 (顺序扫描)

cpu_run_cost = 0.01 × 10万 = 1000

// 步骤3: 总Cost
startup_cost = 650 + 250 = 900
total_cost = 900 + 1000 + 1000 = 2900

// 执行计划
Bitmap Heap Scan on orders
  (cost=900.00..2900.00 rows=100000 width=100)
  Recheck Cond: (customer_id = ANY ('{1,2,3,...,1000}'::integer[]))
  -> Bitmap Index Scan on idx_orders_customer_id
        (cost=0.00..650.00 rows=100000)
        Index Cond: (customer_id = ANY ('{1,2,3,...,1000}'::integer[]))

// 对比Index Scan:
//   如果使用Index Scan:
//   cost = 150 + 1.5 × 10万 = 150150 (每次都是随机I/O)
//   
//   Bitmap Scan:
//   cost = 2900 (bitmap构建后顺序扫描)
//   
//   性能差距: 150150 / 2900 = 51.8倍！

// 结论: 对于中等选择性(1-10%),Bitmap Scan最优
```

---

## 第五部分：统计信息偏差深度分析

### 5.1 统计信息组成

每个字段的统计信息包含：

```sql
-- 查看统计信息
SELECT 
    attname AS column_name,
    n_distinct,           -- 不同值数量 (负数=比例,正数=绝对值)
    null_frac,            -- NULL值比例
    avg_width,            -- 平均宽度
    correlation,          -- 物理与逻辑顺序的相关性 (-1到1)
    most_common_vals,     -- MCV (最常见值)
    most_common_freqs,    -- MCV频率
    histogram_bounds      -- 直方图边界
FROM pg_stats
WHERE tablename = 'orders' AND attname = 'customer_id';
```

**统计信息示例**:

```sql
column_name: customer_id
n_distinct: -0.0001 (表示总数的0.01%,即100万×0.0001=100个不同值)
null_frac: 0.0 (没有NULL)
avg_width: 4 (4字节,INT类型)
correlation: 0.8 (高相关性)
most_common_vals: {1, 2, 3, 10, 100}
most_common_freqs: {0.15, 0.12, 0.10, 0.08, 0.05}
histogram_bounds: {1, 10, 50, 100, 500, 1000, 5000, 10000}
```

### 5.2 选择率计算逻辑

**源码位置**: `src/backend/utils/adt/selfuncs.cpp`

```cpp
// variable_selectivity
// 根据统计信息计算选择率

Selectivity var_eq_sel(Variable* var, Datum constval, bool is_default)
{
    // 1. 检查是否在MCV中
    if (constval IN most_common_vals) {
        // 使用MCV频率
        int idx = position(constval, most_common_vals);
        return most_common_freqs[idx];
    }
    
    // 2. 检查是否有MCV
    if (most_common_vals != NULL) {
        // 从MCV频率计算平均频率
        double sum_mcv_freq = sum(most_common_freqs);
        double avg_mcv_freq = sum_mcv_freq / length(most_common_vals);
        
        // 剩余值的选择率
        double remaining_frac = 1.0 - sum_mcv_freq;
        double remaining_vals = n_distinct - length(most_common_vals);
        double selectivity = remaining_frac / remaining_vals;
        
        return selectivity;
    }
    
    // 3. 没有MCV,使用n_distinct
    double ndistinct = fabs(n_distinct);
    if (n_distinct < 0) {
        // 负数表示比例
        ndistinct = -n_distinct * total_rows;
    }
    
    return 1.0 / ndistinct;
}

Selectivity var_range_sel(Variable* var, Datum minval, Datum maxval)
{
    // 范围查询 (BETWEEN, >, <)
    
    // 1. 如果有直方图
    if (histogram_bounds != NULL) {
        // 计算minval和maxval在直方图中的位置
        int min_pos = position_in_histogram(minval, histogram_bounds);
        int max_pos = position_in_histogram(maxval, histogram_bounds);
        
        // 选择率 = (max_pos - min_pos) / (histogram_size - 1)
        double selectivity = (max_pos - min_pos) / (length(histogram_bounds) - 1);
        
        // 调整: 如果MCV覆盖了部分范围,需要减去MCV频率
        if (most_common_vals != NULL) {
            double mcv_in_range_freq = sum_mcv_freq_in_range(minval, maxval);
            selectivity -= mcv_in_range_freq;
        }
        
        return selectivity;
    }
    
    // 2. 没有直方图,使用默认估算
    // 假设均匀分布
    return 0.5; // 默认50%
}
```

### 5.3 统计信息偏差定位

**偏差检测流程**:

```sql
-- 步骤1: 执行查询并收集实际行数
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT * FROM orders WHERE customer_id = 12345;

-- 输出示例:
Seq Scan on orders  (cost=0.00..20000.00 rows=100 width=100) (actual time=... rows=10000 loops=1)
  Filter: (customer_id = 12345)

-- 估算rows = 100
-- 实际rows = 10000
-- 偏差 = 100倍！

-- 步骤2: 检查统计信息
SELECT 
    n_distinct,
    most_common_vals,
    most_common_freqs,
    histogram_bounds
FROM pg_stats
WHERE tablename = 'orders' AND attname = 'customer_id';

-- 可能的情况:
n_distinct: -0.0001 (假设100个不同值)
most_common_vals: NULL (没有MCV)
histogram_bounds: {1, 1000, 5000, 10000, ...}

-- 步骤3: 计算选择率
-- customer_id = 12345
-- 12345不在MCV中
-- 选择率 = 1 / 100 = 0.01
-- 估算rows = 100万 × 0.01 = 10000 ✓

-- 等等,估算应该是10000,为什么执行计划显示100?

-- 重新检查
SELECT n_distinct FROM pg_stats WHERE tablename = 'orders' AND attname = 'customer_id';
-- n_distinct: -0.000001 (错误的！应该是-0.0001)

-- 问题: n_distinct设置错误
-- -0.000001 表示只有1个不同值
-- 选择率 = 1 / 1 = 1.0 (100%)
-- 但实际上n_distinct计算方式:
--   实际不同值 = 100万 × 0.000001 = 1
--   这明显不对！

-- 步骤4: 定位偏差原因
-- 原因1: 统计信息过期
SELECT last_analyze FROM pg_stat_user_tables WHERE tablename = 'orders';
-- last_analyze: 2024-01-01 (很久以前)

-- 原因2: 采样不足
SHOW default_statistics_target;
-- default_statistics_target: 100 (默认值,可能不够)

-- 原因3: 数据分布不均匀
SELECT customer_id, COUNT(*) FROM orders GROUP BY customer_id ORDER BY 2 DESC LIMIT 10;
-- 输出:
-- customer_id | count
-- 12345       | 10000
-- 67890       | 8000
-- ...
-- 数据严重倾斜,但统计信息没有捕获

-- 步骤5: 修正统计信息
-- 5.1 增加统计目标
ALTER TABLE orders ALTER COLUMN customer_id SET STATISTICS 10000;

-- 5.2 立即分析
ANALYZE orders;

-- 5.3 验证统计信息
SELECT 
    n_distinct,
    most_common_vals,
    most_common_freqs
FROM pg_stats
WHERE tablename = 'orders' AND attname = 'customer_id';

-- 输出:
n_distinct: -0.0001 (正确了)
most_common_vals: {12345, 67890, ...}
most_common_freqs: {0.01, 0.008, ...}

-- 步骤6: 重新执行查询
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT * FROM orders WHERE customer_id = 12345;

-- 输出:
Seq Scan on orders  (cost=0.00..20000.00 rows=10000 width=100) (actual time=... rows=10000 loops=1)
  Filter: (customer_id = 12345)

-- 估算rows = 10000 ✓ 正确！
-- 实际rows = 10000 ✓ 匹配！

-- 现在可以创建索引
CREATE INDEX idx_orders_customer_id ON orders(customer_id);

-- 执行计划:
Index Scan using idx_orders_customer_id on orders
  (cost=150.00..152.50 rows=10000 width=100)
  Index Cond: (customer_id = 12345)

-- 性能提升: 20000 → 152.5 (减少99.2%)
```

### 5.4 统计信息偏差类型总结

| 偏差类型 | 检测方法 | 影响 | 解决方案 |
|---------|---------|------|---------|
| n_distinct错误 | 估算rows vs 实际rows | Cost计算错误 | ANALYZE + 增加统计目标 |
| MCV缺失 | 查询MCV未覆盖的值 | 选择率偏差 | 增加统计目标,捕获MCV |
| 直方图不准 | 范围查询偏差大 | 范围选择率错误 | 增加统计目标,更精细直方图 |
| 相关性错误 | 索引扫描Cost不准 | 扫描方式选择错误 | ANALYZE,重建表(CLUSTER) |
| 统计信息过期 | last_analyze很久以前 | 所有计算都不准 | 定期ANALYZE |

---

## 第六部分：查询转换深度分析

### 6.1 查询转换检查清单

**转换类型**:

1. **Sublink提升** (相关子查询→JOIN)
2. **谓词推入** (条件下推到子查询)
3. **视图展开** (视图→原表)
4. **JOIN重排序** (优化JOIN顺序)
5. **外连接消除** (不必要的OUTER JOIN→INNER JOIN)
6. **表达式简化** (复杂表达式→简单表达式)

### 6.2 为什么转换不发生

**转换阻止条件**:

```sql
-- 转换1: Sublink提升

-- ✅ 可以提升
SELECT *, (SELECT name FROM customers WHERE customer_id = o.customer_id)
FROM orders o;

-- 转换为:
SELECT o.*, c.name
FROM orders o
LEFT JOIN customers c ON o.customer_id = c.customer_id;

-- ❌ 不能提升 (多层相关)
SELECT *, (
    SELECT COUNT(*)
    FROM order_items oi
    WHERE oi.order_id = o.order_id
    AND oi.product_id IN (
        SELECT product_id FROM products WHERE category = 'Electronics'
    )
)
FROM orders o;

-- 原因: 多层相关性,内层子查询引用了中层,中层引用了外层
-- 优化器无法提升

-- ❌ 不能提升 (易变性函数)
SELECT *, (
    SELECT MAX(amount) FROM orders WHERE customer_id = o.customer_id
) * random() AS random_max_amount
FROM orders o;

-- 原因: random()是易变性函数,每次调用返回不同值
-- 提升后会改变语义

-- ❌ 不能提升 (聚合函数+GROUP BY)
SELECT *, (
    SELECT department_id, AVG(salary)
    FROM employees
    WHERE manager_id = e.manager_id
    GROUP BY department_id
)
FROM employees e;

-- 原因: 子查询返回多列多行,无法提升为标量

-- 转换2: 谓词推入

-- ✅ 可以推入
SELECT * FROM (
    SELECT * FROM orders
) o
WHERE o.order_date >= '2024-01-01';

-- 推入后:
SELECT * FROM orders WHERE order_date >= '2024-01-01';

-- ❌ 不能推入 (UNION)
SELECT * FROM (
    SELECT * FROM orders
    UNION ALL
    SELECT * FROM archived_orders
) o
WHERE o.order_date >= '2024-01-01';

-- 原因: UNION是隐式去重,需要先合并再过滤
-- 但UNION ALL可以推入:
SELECT * FROM (
    SELECT * FROM orders
    UNION ALL
    SELECT * FROM archived_orders
) o
WHERE o.order_date >= '2024-01-01';

-- 推入后:
SELECT * FROM orders WHERE order_date >= '2024-01-01'
UNION ALL
SELECT * FROM archived_orders WHERE order_date >= '2024-01-01';

-- ❌ 不能推入 (聚合)
SELECT * FROM (
    SELECT customer_id, COUNT(*) AS order_count
    FROM orders
    GROUP BY customer_id
) o
WHERE o.order_count > 10;

-- 原因: 聚合后再过滤,推入会改变语义
-- 但可以改为HAVING:
SELECT customer_id, COUNT(*) AS order_count
FROM orders
GROUP BY customer_id
HAVING COUNT(*) > 10;

-- 转换3: 视图展开

-- ✅ 可以展开
CREATE VIEW customer_orders AS
SELECT o.*, c.customer_name
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id;

SELECT * FROM customer_orders WHERE order_date >= '2024-01-01';

-- 展开后:
SELECT o.*, c.customer_name
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE o.order_date >= '2024-01-01';

-- ❌ 不能展开 (递归视图)
CREATE RECURSIVE VIEW emp_hierarchy AS
SELECT employee_id, name, manager_id, 1 AS level
FROM employees
WHERE manager_id IS NULL
UNION ALL
SELECT e.employee_id, e.name, e.manager_id, h.level + 1
FROM employees e
JOIN emp_hierarchy h ON e.manager_id = h.employee_id;

-- 原因: 递归视图不能展开,需要特殊处理

-- ❌ 不能展开 (有副作用的函数)
CREATE VIEW side_effects AS
SELECT *, notify_user(user_id) AS notification
FROM users;

SELECT * FROM side_effects WHERE user_id = 123;

-- 原因: notify_user()有副作用,展开可能改变执行次数
```

### 6.3 查询转换验证方法

**使用EXPLAIN VERBOSE**:

```sql
-- 检查是否发生转换
EXPLAIN VERBOSE
SELECT *, (SELECT name FROM customers WHERE customer_id = o.customer_id)
FROM orders o;

-- 输出1: 未转换
Seq Scan on orders o
  SubPlan 1
    -> Result
      -> Index Scan using idx_customers_pk on customers

-- 输出2: 已转换
Hash Join
  -> Seq Scan on orders o
  -> Hash
      -> Seq Scan on customers

-- 区别: 
--   SubPlan表示未转换
--   Hash Join表示已转换为JOIN

-- 检查谓词推入
EXPLAIN VERBOSE
SELECT * FROM (
    SELECT * FROM orders
) o
WHERE o.order_date >= '2024-01-01';

-- 输出1: 未推入
Subquery Scan on o
  -> Seq Scan on orders
  Filter: (order_date >= '2024-01-01'::date)

-- 输出2: 已推入
Seq Scan on orders
  Filter: (order_date >= '2024-01-01'::date)

-- 区别: 
--   Subquery Scan表示未推入
--   直接Seq Scan表示已推入
```

---

## 第七部分：完整分析输出格式（强制标准）

### ⚠️ 输出格式要求（必须严格遵守）

**用户要求的输出格式**:

```
# VastBase SQL性能诊断报告

## 一、性能瓶颈（源码级深度分析）

### 🔴 致命瓶颈1：[瓶颈名称]（X倍性能损失）

**问题现象**：
[从执行计划中提取的关键问题]

**执行计划分析**：
```
[关键执行计划片段]
```

**源码级分析**（VastBase源码位置）：
```c
[源码片段展示优化器错误决策过程]
```

**Cost计算验证**：
```
[详细的Cost计算过程，对比预估vs实际]
```

**根本原因**：
- [具体原因1]
- [具体原因2]

**性能损失量化**：
- [具体指标1]: [数值]
- [具体指标2]: [数值]

---

### 🟡 严重瓶颈2：[瓶颈名称]（X倍性能损失）

[同上结构]

---

### 🟢 中等瓶颈3：[瓶颈名称]（X倍性能损失）

[同上结构]

---

## 二、优化方案

### 2.1 不改写SQL的情况（索引+Hint）

#### ✅ 方案A：[方案名称]（预期提升X倍）

**创建索引**：
```sql
[具体的DDL语句]
```

**执行Hint**：
```sql
/*+ [具体的Hint] */
[原SQL]
```

**预期执行计划**：
```
[优化后的执行计划]
```

**验证SQL**：
```sql
[验证方案效果的SQL]
```

---

#### ✅ 方案B：[方案名称]（预期提升X倍）

[同上结构]

---

#### ✅ 方案C：[方案名称]（预期提升X倍）

[同上结构]

---

### 2.2 可改写SQL的情况（SQL重构）

#### ✅ 方案1：[方案名称]（预期提升X倍）

**优化前SQL**：
```sql
[原SQL]
```

**优化后SQL**：
```sql
[优化后的SQL]
```

**执行计划变化**：
```
[对比优化前后的执行计划]
```

**核心改进**：
1. ✅ [改进点1]
2. ✅ [改进点2]

---

#### ✅ 方案2：[方案名称]（预期提升X倍）

[同上结构]

---

#### ✅ 方案3：综合优化方案（预期提升X倍）

**完整优化SQL**：
```sql
[完整优化后的SQL]
```

**配套索引**：
```sql
[配套的索引DDL]
```

**预期执行计划**：
```
[优化后的完整执行计划]
```

**性能对比表**：

| 指标 | 优化前 | 优化后 | 提升倍数 |
|------|--------|--------|----------|
| [指标1] | [值] | [值] | [倍数] |
| [指标2] | [值] | [值] | [倍数] |

**验证步骤**：
```sql
-- 步骤1: 验证[具体内容]
[验证SQL]
-- 预期: [预期结果]

-- 步骤2: 验证[具体内容]
[验证SQL]
-- 预期: [预期结果]
```

---

## 总结

### 性能瓶颈优先级
1. **P0**: [瓶颈1]（X倍损失）→ [解决方案]
2. **P1**: [瓶颈2]（X倍损失）→ [解决方案]
3. **P2**: [瓶颈3]（X倍损失）→ [解决方案]
4. **P3**: [瓶颈4]（X倍损失）→ [解决方案]

### 优化方案选择
- **不改写SQL**: 方案A（索引）+ 方案B（Hint）+ 方案C（分区裁剪）= X倍提升
- **可改写SQL**: 方案1（CTE物化）+ 方案2（消除子查询）+ 方案3（综合优化）= X倍提升
```

---

## 第八部分：技能迭代训练

### 使用此技能分析查询后，请更新以下部分

1. **添加新的实战案例**到案例库
2. **修正Cost计算公式**如果发现不准确
3. **添加新的统计信息偏差模式**
4. **记录新的查询转换规则**
5. **更新优化方案**如果发现更好的方案

### 持续改进

- 每次分析后，记录结果
- 对比预期vs实际
- 调整技能参数
- 提升准确性

---

## 第九部分：SQLHC集成支持

本技能已集成 **SQLHC (SQL Health Check)** 输出分析能力。

### 什么是SQLHC

SQLHC是VastBase数据库的自动化SQL健康检查工具，能够：
- 自动收集执行计划
- 自动收集统计信息
- 自动收集对象定义
- 自动收集配置参数
- 生成标准化诊断报告

### 如何使用SQLHC输入

**方式1: 提供SQLHC输出目录**
```
用户: 这是sqlhc的输出: /path/to/sqlhc_output/
```

**方式2: 直接粘贴SQLHC报告内容**
```
用户: 这是sqlhc的报告:
[粘贴sqlhc输出内容]
```

**方式3: 描述SQLHC问题**
```
用户: 我运行了sqlhc，发现这个查询有JOIN顺序问题
```

### SQLHC增强分析

当检测到SQLHC输入时，技能会额外分析：

1. **统计信息新鲜度检查**
   - 距离上次ANALYZE的时间
   - 死行比例是否过高
   - 是否需要更新统计信息

2. **无效索引检测**
   - 从未被使用的索引
   - 使用频率很低的索引
   - 建议删除以节省空间

3. **缺失索引检测**
   - 基于执行计划的扫描节点
   - 结合统计信息的选择性
   - 建议创建缺失的索引

4. **配置参数问题**
   - work_mem是否足够
   - 优化器参数是否合理
   - Cost参数是否匹配硬件

5. **对象定义分析**
   - 表的分区策略
   - 索引的类型和属性
   - 约束的定义

### SQLHC vs 手动输入

| 信息类型 | 手动输入 | SQLHC输入 |
|---------|---------|-----------|
| 执行计划 | 需要手动提供 | ✓ 自动解析 |
| 表统计信息 | 需要手动查询 | ✓ 自动解析 |
| 列统计信息 | 需要手动查询 | ✓ 自动解析 |
| 索引统计信息 | 需要手动查询 | ✓ 自动解析 |
| 配置参数 | 需要手动查询 | ✓ 自动解析 |
| 统计信息新鲜度 | 需要手动计算 | ✓ 自动分析 |
| 无效索引检测 | 需要手动判断 | ✓ 自动检测 |

**建议**: 生产环境推荐使用SQLHC收集信息，确保信息全面和准确。

### SQLHC详细文档

详细的SQLHC集成说明请参考:
- `sqlhc-integration.md`

---

**版本**: v3.0  
**创建日期**: 2026-04-17  
**作者**: xiang  
**基于**: 10篇VastBase优化器深度文档 + SQLHC集成支持
**特点**: 源码级Cost计算、统计信息偏差定位、查询转换深度分析
