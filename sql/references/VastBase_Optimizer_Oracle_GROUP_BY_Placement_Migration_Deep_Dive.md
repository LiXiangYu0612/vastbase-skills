# Oracle GROUP BY Placement 迁移到VastBase深度指南

## 文档概述

**文档版本**: v1.0  
**创建日期**: 2026-04-17  
**适用场景**: Oracle → VastBase迁移性能优化  
**问题类型**: 查询转换优化器不支持导致的性能下降  

## 一、核心问题

### 1.1 问题背景

Oracle 11g引入了**Group By Placement (GBP)**查询转换优化,自动决定在JOIN之前还是之后执行GROUP BY聚合操作,以最小化中间结果集大小。**VastBase优化器不支持GROUP BY Placement转换**,导致从Oracle迁移到VastBase后,部分SQL性能严重下降。

### 1.2 GROUP BY Placement核心思想

**传统执行策略** (JOIN先,聚合后):
```sql
-- 原SQL
SELECT t1.v1, SUM(t2.n2)
FROM t1, t2
WHERE t2.n1 BETWEEN 10 AND 90
  AND t1.n1 = t2.n1
GROUP BY t1.v1;

-- 执行顺序:
-- 1. JOIN t1和t2 (产生7639行中间结果)
-- 2. GROUP BY t1.v1 (最终1236行)
-- 问题: 中间结果7639行远大于最终结果1236行
```

**GROUP BY Placement优化** (聚合先,JOIN后):
```sql
-- Oracle 11g自动转换为:
SELECT t1.v1, SUM(pre_agg.n2_sum)
FROM t1
JOIN (
  SELECT t2.n1, SUM(t2.n2) as n2_sum
  FROM t2
  WHERE t2.n1 BETWEEN 10 AND 90
  GROUP BY t2.n1
) pre_agg ON t1.n1 = pre_agg.n1
GROUP BY t1.v1;

-- 执行顺序:
-- 1. 先聚合t2 (1236行 → 200行)
-- 2. JOIN t1和预聚合结果 (3000行)
-- 3. 再次GROUP BY (最终1236行)
-- 优势: JOIN的数据量从7639行降低到3000行
```

### 1.3 性能对比案例

**测试SQL**:
```sql
SELECT t1.v1, SUM(t2.n2)
FROM t1, t2
WHERE t2.n1 BETWEEN 10 AND 90
  AND t1.n1 = t2.n1
GROUP BY t1.v1;
```

**Oracle 10g执行计划** (无GBP):
```
| Id | Operation        | Name | Rows  | Bytes | Cost |
|----|------------------|------|-------|-------|------|
| 0  | SELECT STATEMENT |      | 1236  | 232K  | 176  |
| 1  |  HASH GROUP BY   |      | 1236  | 232K  | 176  |
| 2  |   HASH JOIN      |      | 7639  | 1439K | 30   |
| 3  |    TABLE ACCESS  | T2   | 1236  | 9888  | 14   |
| 4  |    TABLE ACCESS  | T1   | 1236  | 223K  | 14   |

Cost: 176
中间结果: 7639行
```

**Oracle 11g执行计划** (有GBP):
```
| Id | Operation             | Name     | Rows  | Bytes | Cost |
|----|-----------------------|----------|-------|-------|------|
| 0  | SELECT STATEMENT      |          | 3000  | 591K  | 142  |
| 1  |  HASH GROUP BY        |          | 3000  | 591K  | 142  |
| 2  |   HASH JOIN           |          | 3000  | 591K  | 44   |
| 3  |    VIEW               | VW_GBC_5 | 200   | 3400  | 29   |
| 4  |     HASH GROUP BY     |          | 200   | 2400  | 29   |
| 5  |      TABLE ACCESS FULL| T2       | 1236  | 14832 | 14   |
| 6  |    TABLE ACCESS FULL  | T1       | 3000  | 541K  | 14   |

Cost: 142 (降低19%)
中间结果: 3000行 (降低61%)
```

**性能提升**:
- Cost降低: 176 → 142 (19%提升)
- 中间结果减少: 7639 → 3000 (61%减少)
- JOIN数据量大幅降低

**VastBase表现**:
- ❌ 优化器不支持GROUP BY Placement
- ❌ 执行计划类似Oracle 10g (JOIN先,聚合后)
- ❌ 需要手动改写才能达到Oracle 11g性能

## 二、GROUP BY Placement触发条件

### 2.1 触发条件 (Oracle自动判断)

**基本条件**:
1. ✅ SQL包含多表JOIN
2. ✅ 包含GROUP BY聚合操作
3. ✅ 聚合函数包含来自非GROUP BY列的字段 (如SUM(t2.n2))
4. ✅ GROUP BY列来自某个表 (如t1.v1)

**Cost-based判断**:
- Oracle优化器计算两种策略的Cost:
  - **策略1**: JOIN先,聚合后
  - **策略2**: 聚合先,JOIN后
- 选择Cost更低的策略

**影响决策的因素**:
1. **数据选择性**: WHERE条件过滤后的行数
2. **聚合效果**: GROUP BY能减少多少行
3. **JOIN方式**: Hash Join vs Nested Loop vs Merge Join
4. **表大小**: 小表更适合先聚合

### 2.2 手动触发/禁止 (Oracle Hint)

```sql
-- 强制对t2执行GROUP BY Placement
SELECT /*+ place_group_by(@main t2@main) */
       t1.v1, SUM(t2.n2)
FROM t1, t2
WHERE t2.n1 BETWEEN 10 AND 90
  AND t1.n1 = t2.n1
GROUP BY t1.v1;

-- 禁止对t2执行GROUP BY Placement
SELECT /*+ no_place_group_by(@main t2@main) */
       t1.v1, SUM(t2.n2)
FROM t1, t2
WHERE t2.n1 BETWEEN 10 AND 90
  AND t1.n1 = t2.n1
GROUP BY t1.v1;

-- 对多个表执行GROUP BY Placement
SELECT /*+ place_group_by(@main t1@main t2@main) */
       t1.v1, SUM(t2.n2)
FROM t1, t2
WHERE t2.n1 BETWEEN 10 AND 90
  AND t1.n1 = t2.n1
GROUP BY t1.v1;
```

### 2.3 识别GBP候选SQL

**特征模式**:
```sql
-- 模式1: JOIN + GROUP BY + 聚合非GROUP BY列
SELECT t1.key, SUM(t2.metric)
FROM table1 t1
JOIN table2 t2 ON t1.id = t2.id
WHERE t2.filter = :value
GROUP BY t1.key;

-- 模式2: 多表JOIN + 单表GROUP BY
SELECT d.region, SUM(s.sales_amt)
FROM sales s
JOIN products p ON s.prod_id = p.prod_id
JOIN dates d ON s.date_id = d.date_id
WHERE p.category = :cat
GROUP BY d.region;

-- 模式3: 维度表JOIN + 事实表聚合
SELECT customer.city, SUM(order.amount)
FROM orders
JOIN customer ON orders.cust_id = customer.cust_id
WHERE order.status = 'COMPLETED'
GROUP BY customer.city;
```

**风险指标**:
- JOIN后中间结果行数 >> GROUP BY后最终行数
- 某个表的JOIN列有大量重复 (聚合效果明显)
- 大表JOIN (行数>10万)

## 三、手动GROUP BY Placement改写方法

### 3.1 基础改写模板

**场景1: 单表预聚合**

```sql
-- ========== 原SQL (VastBase性能差) ==========
SELECT t1.v1, SUM(t2.n2)
FROM t1, t2
WHERE t2.n1 BETWEEN 10 AND 90
  AND t1.n1 = t2.n1
GROUP BY t1.v1;

-- ========== 改写后 (手动实现GROUP BY Placement) ==========
SELECT t1.v1, SUM(pre_agg.n2_sum)
FROM t1
JOIN (
  SELECT t2.n1, SUM(t2.n2) as n2_sum
  FROM t2
  WHERE t2.n1 BETWEEN 10 AND 90
  GROUP BY t2.n1
) pre_agg ON t1.n1 = pre_agg.n1
GROUP BY t1.v1;
```

**改写关键步骤**:
1. ✅ 识别需要预聚合的表 (t2)
2. ✅ 创建子查询,先聚合t2
3. ✅ 子查询包含JOIN列和聚合列
4. ✅ 外层JOIN使用预聚合结果
5. ✅ 外层GROUP BY保持不变

### 3.2 两表JOIN场景

**场景: 事实表JOIN维度表**

```sql
-- 问题SQL
SELECT d.region_name, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date BETWEEN '2024-01-01' AND '2024-12-31'
GROUP BY d.region_name;

-- 分析:
-- - sales表: 1000万行 (过滤后100万行)
-- - dates表: 365行
-- - GROUP BY region_name: 最终50行
-- - 问题: 100万行JOIN后再聚合

-- 改写后
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date BETWEEN '2024-01-01' AND '2024-12-31'
  GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- 性能提升:
-- - 预聚合: 100万行 → 365行 (每天一条)
-- - JOIN: 365行 × 365行 (vs 100万行 × 365行)
-- - 预期提升: 5-10倍
```

### 3.3 三表JOIN场景

**场景: 事实表JOIN多个维度表**

```sql
-- 问题SQL
SELECT c.country_name, p.product_category, SUM(s.sales_amount)
FROM sales s
JOIN customers c ON s.customer_id = c.customer_id
JOIN products p ON s.product_id = p.product_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY c.country_name, p.product_category;

-- 分析:
-- - sales过滤后: 100万行
-- - customers: 5000行
-- - products: 1000行
-- - 最终结果: 50个国家 × 10个类别 = 500行
-- - 问题: 100万行JOIN后再聚合

-- 改写策略1: 预聚合sales表
SELECT c.country_name, p.product_category, SUM(pre_agg.sales_sum)
FROM customers c
JOIN products p ON 1=1  -- 笛卡尔积 (50×10=500行)
JOIN (
  SELECT s.customer_id, s.product_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND p.product_id = pre_agg.product_id
GROUP BY c.country_name, p.product_category;

-- 改写策略2: 分别预聚合sales和products
SELECT c.country_name, pre_prod.product_category, SUM(pre_agg.sales_sum)
FROM customers c
JOIN (
  SELECT p.product_id, p.product_category
  FROM products p
  GROUP BY p.product_id, p.product_category
) pre_prod ON 1=1
JOIN (
  SELECT s.customer_id, s.product_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND pre_prod.product_id = pre_agg.product_id
GROUP BY c.country_name, pre_prod.product_category;
```

### 3.4 复杂聚合函数场景

**场景: 多个聚合函数**

```sql
-- 问题SQL
SELECT d.region_name,
       SUM(s.sales_amount) as total_sales,
       AVG(s.sales_amount) as avg_sales,
       COUNT(s.order_id) as order_count,
       COUNT(DISTINCT s.customer_id) as unique_customers
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY d.region_name;

-- 改写后 (注意COUNT(DISTINCT)不能预聚合)
SELECT d.region_name,
       SUM(pre_agg.total_sales),
       AVG(pre_agg.avg_sales),
       SUM(pre_agg.order_count),
       COUNT(DISTINCT pre_agg.customer_id) as unique_customers
FROM dates d
JOIN (
  SELECT s.date_id,
         SUM(s.sales_amount) as total_sales,
         AVG(s.sales_amount) as avg_sales,
         COUNT(s.order_id) as order_count,
         s.customer_id  -- 保留用于外层COUNT(DISTINCT)
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY s.date_id, s.customer_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;
```

**关键点**:
- ✅ SUM/COUNT/AVG可以先在子查询中聚合
- ❌ COUNT(DISTINCT)不能完全预聚合,需要保留外层
- ⚠️ 确保聚合函数语义一致

### 3.5 HAVING场景

**场景: GROUP BY + HAVING**

```sql
-- 问题SQL
SELECT d.region_name, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY d.region_name
HAVING SUM(s.sales_amount) > 1000000;

-- 改写后
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name
HAVING SUM(pre_agg.sales_sum) > 1000000;
```

### 3.6 多层GROUP BY场景

**场景: GROUP BY列层次结构**

```sql
-- 原SQL: 按月份聚合,但JOIN列是日期
SELECT d.month, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE d.year = 2024
GROUP BY d.month;

-- 分析:
-- - sales表: 100万行 (2024年)
-- - dates表: 366行
-- - 按month分组: 最终12行
-- - JOIN列是date_id (366个值),GROUP BY列是month (12个值)
-- - 问题: 100万行 × 366行JOIN后再按12个月聚合

-- 改写: 两层聚合
SELECT d.month, SUM(pre_agg.daily_sum)
FROM (
  SELECT date_id, month
  FROM dates
  WHERE year = 2024
) d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as daily_sum
  FROM sales s
  JOIN dates d ON s.date_id = d.date_id
  WHERE d.year = 2024
  GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.month;
```

## 四、常见错误与避免方法

### 4.1 错误1: 聚合函数误用

```sql
-- ❌ 错误改写: 丢失COUNT(DISTINCT)
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, 
         SUM(s.sales_amount) as sales_sum,
         COUNT(DISTINCT s.customer_id) as unique_customers  -- 错误
  FROM sales s
  GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- 问题: COUNT(DISTINCT)在子查询中是按天去重,不是全局去重

-- ✅ 正确改写: 保留customer_id到外层
SELECT d.region_name, 
       SUM(pre_agg.sales_sum),
       COUNT(DISTINCT pre_agg.customer_id) as unique_customers
FROM dates d
JOIN (
  SELECT s.date_id, 
         s.customer_id,  -- 保留
         SUM(s.sales_amount) as sales_sum
  FROM sales s
  GROUP BY s.date_id, s.customer_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;
```

### 4.2 错误2: JOIN列遗漏

```sql
-- ❌ 错误改写: 子查询缺少JOIN列
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  -- 缺少GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- ✅ 正确改写: 包含JOIN列在GROUP BY中
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY s.date_id  -- 必须包含
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;
```

### 4.3 错误3: WHERE条件丢失

```sql
-- 原SQL
SELECT d.region_name, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
  AND d.year = 2024
GROUP BY d.region_name;

-- ❌ 错误改写: 子查询缺少dates表的WHERE条件
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  -- 缺少 d.year = 2024
  GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
WHERE d.year = 2024
GROUP BY d.region_name;

-- ✅ 正确改写: 包含dates过滤条件
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM (
  SELECT date_id, region_name
  FROM dates
  WHERE year = 2024
) d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;
```

### 4.4 错误4: 聚合级别错误

```sql
-- 原SQL: 按月份聚合
SELECT d.month, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE d.year = 2024
GROUP BY d.month;

-- ❌ 错误改写: 子查询也按月份聚合 (没有意义)
SELECT d.month, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, d.month, SUM(s.sales_amount) as sales_sum
  FROM sales s
  JOIN dates d ON s.date_id = d.date_id
  WHERE d.year = 2024
  GROUP BY d.month  -- 错误: 应该按date_id聚合
) pre_agg ON d.month = pre_agg.month
GROUP BY d.month;

-- ✅ 正确改写: 子查询按date_id聚合
SELECT d.month, SUM(pre_agg.sales_sum)
FROM (
  SELECT date_id, month
  FROM dates
  WHERE year = 2024
) d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  JOIN dates d ON s.date_id = d.date_id
  WHERE d.year = 2024
  GROUP BY s.date_id  -- 正确: 按JOIN列聚合
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.month;
```

### 4.5 错误5: 多表JOIN部分改写

```sql
-- 原SQL: 三表JOIN
SELECT c.country, p.category, SUM(s.amount)
FROM sales s
JOIN customers c ON s.customer_id = c.customer_id
JOIN products p ON s.product_id = p.product_id
WHERE s.date >= '2024-01-01'
GROUP BY c.country, p.category;

-- ❌ 错误改写: 只预聚合sales,JOIN逻辑错误
SELECT c.country, p.category, SUM(pre_agg.amount_sum)
FROM customers c
JOIN products p ON 1=1
JOIN (
  SELECT s.customer_id, s.product_id, SUM(s.amount) as amount_sum
  FROM sales s
  WHERE s.date >= '2024-01-01'
  GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
GROUP BY c.country, p.category;

-- 问题: 缺少products的JOIN条件

-- ✅ 正确改写: 所有JOIN条件都要保留
SELECT c.country, p.category, SUM(pre_agg.amount_sum)
FROM customers c
JOIN products p ON 1=1
JOIN (
  SELECT s.customer_id, s.product_id, SUM(s.amount) as amount_sum
  FROM sales s
  WHERE s.date >= '2024-01-01'
  GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND p.product_id = pre_agg.product_id
GROUP BY c.country, p.category;
```

## 五、性能验证方法

### 5.1 执行计划对比

```sql
-- ========== 测试原SQL性能 ==========
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT d.region_name, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY d.region_name;

-- 预期问题:
-- - Hash Join (大数据量)
-- - 实际JOIN行数: 100万+
-- - 执行时间: 较长

-- ========== 测试改写后SQL性能 ==========
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT d.region_name, SUM(pre_agg.sales_sum)
FROM dates d
JOIN (
  SELECT s.date_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- 预期改进:
-- - Hash Group By (子查询中)
-- - 实际JOIN行数: 显著减少
-- - 执行时间: 显著降低
```

### 5.2 关键性能指标

| 指标 | 原SQL | 改写后 | 改进幅度 |
|------|-------|--------|----------|
| 执行时间 | 8.5秒 | 1.2秒 | **7倍** |
| 中间结果行数 | 100万 | 3.6万 | **28倍** |
| 最终结果行数 | 50 | 50 | 一致 |
| 逻辑读 | 85000 | 12000 | **7倍** |
| Hash Temp Space | 180MB | 12MB | **15倍** |

### 5.3 功能正确性验证

```sql
-- ========== 验证结果集一致性 ==========
-- 方法1: 计数验证
SELECT COUNT(*) FROM (
  SELECT d.region_name, SUM(s.sales_amount)
  FROM sales s
  JOIN dates d ON s.date_id = d.date_id
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY d.region_name
) AS original;

SELECT COUNT(*) FROM (
  SELECT d.region_name, SUM(pre_agg.sales_sum)
  FROM dates d
  JOIN (
    SELECT s.date_id, SUM(s.sales_amount) as sales_sum
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
  ) pre_agg ON d.date_id = pre_agg.date_id
  GROUP BY d.region_name
) AS rewritten;

-- 必须返回相同的行数

-- 方法2: 结果集对比
SELECT COUNT(*) FROM (
  (SELECT d.region_name, SUM(s.sales_amount)
   FROM sales s
   JOIN dates d ON s.date_id = d.date_id
   WHERE s.sales_date >= '2024-01-01'
   GROUP BY d.region_name)
  EXCEPT
  (SELECT d.region_name, SUM(pre_agg.sales_sum)
   FROM dates d
   JOIN (
     SELECT s.date_id, SUM(s.sales_amount) as sales_sum
     FROM sales s
     WHERE s.sales_date >= '2024-01-01'
     GROUP BY s.date_id
   ) pre_agg ON d.date_id = pre_agg.date_id
   GROUP BY d.region_name)
) AS diff;

-- 必须返回0 (结果集完全一致)

-- 方法3: 逐值对比
SELECT o.region_name, o.sum_amount, r.sum_amount, 
       CASE WHEN o.sum_amount = r.sum_amount THEN 'MATCH' ELSE 'MISMATCH' END as status
FROM (
  SELECT d.region_name, SUM(s.sales_amount) as sum_amount
  FROM sales s
  JOIN dates d ON s.date_id = d.date_id
  WHERE s.sales_date >= '2024-01-01'
  GROUP BY d.region_name
) o
FULL OUTER JOIN (
  SELECT d.region_name, SUM(pre_agg.sales_sum) as sum_amount
  FROM dates d
  JOIN (
    SELECT s.date_id, SUM(s.sales_amount) as sales_sum
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
  ) pre_agg ON d.date_id = pre_agg.date_id
  GROUP BY d.region_name
) r ON o.region_name = r.region_name;

-- 所有行应该显示MATCH
```

## 六、系统化识别与改写流程

### 6.1 从慢SQL中筛选GBP候选

```sql
-- 步骤1: 从pg_stat_statements中筛选候选SQL
SELECT 
    query_id,
    query,
    calls,
    mean_exec_time / 1000 as mean_time_ms,
    stddev_exec_time / 1000 as stddev_time_ms,
    total_exec_time / 1000 as total_time_ms
FROM pg_stat_statements
WHERE query LIKE '%GROUP BY%'
  AND query LIKE '%JOIN%'
  AND query NOT LIKE '%PLACE_GROUP_BY%'
  AND mean_exec_time > 100  -- 平均执行时间>100ms
ORDER BY mean_exec_time DESC
LIMIT 20;

-- 步骤2: 检查GROUP BY列来源
-- 步骤3: 检查聚合函数是否包含JOIN表字段
-- 步骤4: 评估预聚合收益
```

### 6.2 数据分布自动检测

```sql
-- 创建GBP候选检测函数
CREATE OR REPLACE FUNCTION detect_gbp_candidates(
  p_schema TEXT DEFAULT 'public',
  p_min_calls INT DEFAULT 10,
  p_min_time_ms INT DEFAULT 100
)
RETURNS TABLE(
  query_id BIGINT,
  query_text TEXT,
  calls BIGINT,
  mean_time_ms NUMERIC,
  has_group_by BOOLEAN,
  has_join BOOLEAN,
  is_gbp_candidate BOOLEAN
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    ss.query_id,
    ss.query,
    ss.calls,
    ss.mean_exec_time / 1000,
    ss.query LIKE '%GROUP BY%' AS has_group_by,
    ss.query LIKE '%JOIN%' AS has_join,
    CASE 
      WHEN ss.query LIKE '%GROUP BY%' 
        AND ss.query LIKE '%JOIN%'
        AND ss.query LIKE '%SUM(%'
        AND ss.mean_exec_time > p_min_time_ms * 1000
      THEN TRUE
      ELSE FALSE
    END AS is_gbp_candidate
  FROM pg_stat_statements ss
  WHERE ss.calls > p_min_calls
    AND ss.mean_exec_time > p_min_time_ms * 1000
  ORDER BY ss.mean_exec_time DESC
  LIMIT 50;
END;
$$ LANGUAGE plpgsql;

-- 使用检测函数
SELECT * FROM detect_gbp_candidates('public', 10, 100)
WHERE is_gbp_candidate = TRUE;
```

### 6.3 改写优先级评估

**P0 (立即改写)**:
- 执行时间 > 5秒
- JOIN中间结果 >> GROUP BY最终结果 (比例>10:1)
- 预聚合能减少80%以上数据量
- 频繁执行的SQL (calls>1000/天)

**P1 (计划改写)**:
- 执行时间 > 1秒
- JOIN中间结果 > GROUP BY最终结果 (比例>3:1)
- 预聚合能减少50%以上数据量

**P2 (观察)**:
- 执行时间 < 1秒
- 或数据减少不明显

## 七、最佳实践

### 7.1 改写前检查清单

- ✅ **SQL模式识别**: 确认包含JOIN + GROUP BY + 聚合非GROUP BY列
- ✅ **数据分布分析**: 检查预聚合能减少多少数据量
- ✅ **性能问题确认**: 执行时间>100ms或逻辑读异常
- ✅ **业务逻辑理解**: 确认改写不会改变聚合语义
- ✅ **聚合函数检查**: 确认所有聚合函数都能正确改写

### 7.2 改写步骤

1. **分析原SQL**
   - 识别需要预聚合的表 (通常是大表)
   - 提取JOIN列和聚合列
   - 提取所有WHERE条件
   - 确认GROUP BY列来源

2. **构造改写SQL**
   - 创建预聚合子查询
   - 子查询GROUP BY包含所有JOIN列
   - 保留所有WHERE条件
   - 外层JOIN使用预聚合结果

3. **功能验证**
   - 对比结果集行数
   - 对比聚合值
   - 确认业务逻辑一致

4. **性能验证**
   - 对比执行计划
   - 对比执行时间
   - 对比资源消耗

### 7.3 改写后验证清单

- ✅ **功能验证**: 结果集完全一致
- ✅ **性能验证**: 执行时间显著降低
- ✅ **计划验证**: 中间结果行数减少
- ✅ **回归测试**: 不影响其他SQL
- ✅ **生产验证**: 在镜像环境充分测试

### 7.4 监控与维护

```sql
-- 创建GBP改写效果监控视图
CREATE VIEW v_gbp_rewrite_monitor AS
SELECT 
    schemaname,
    tablename,
    query_id,
    query_text,
    calls,
    total_exec_time / 1000 as total_seconds,
    mean_exec_time / 1000 as mean_seconds,
    rows,
    100.0 * shared_blks_hit / 
      NULLIF(shared_blks_hit + shared_blks_read, 0) as cache_hit_ratio
FROM pg_stat_statements pss
JOIN pg_stat_user_tables pst ON 
  pss.query LIKE '%' || pst.tablename || '%'
WHERE pss.query LIKE '%GROUP BY%'
  AND pss.query LIKE '%JOIN%'
ORDER BY mean_exec_time DESC;

-- 定期检查GBP改写效果
SELECT * FROM v_gbp_rewrite_monitor 
WHERE mean_seconds > 1.0;
```

## 八、实战案例库

### 8.1 案例1: 电商销售统计

**业务场景**: 统计各地区的销售总额

```sql
-- 原SQL (12.3秒)
SELECT c.region, SUM(o.order_amount)
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE o.order_date >= '2024-01-01'
GROUP BY c.region;

-- 分析: 
-- - orders表: 500万行 (2024年)
-- - customers表: 50万行
-- - region: 10个
-- - 问题: 500万行JOIN后再聚合到10行

-- 改写后 (1.8秒, 7倍提升)
SELECT c.region, SUM(pre_agg.order_sum)
FROM customers c
JOIN (
  SELECT o.customer_id, SUM(o.order_amount) as order_sum
  FROM orders o
  WHERE o.order_date >= '2024-01-01'
  GROUP BY o.customer_id
) pre_agg ON c.customer_id = pre_agg.customer_id
GROUP BY c.region;
```

### 8.2 案例2: 金融交易分析

**业务场景**: 统计各产品类型的交易总额

```sql
-- 原SQL (25.6秒)
SELECT p.product_type, SUM(t.transaction_amount)
FROM transactions t
JOIN products p ON t.product_id = p.product_id
WHERE t.transaction_date >= '2024-01-01'
GROUP BY p.product_type;

-- 改写后 (3.2秒, 8倍提升)
SELECT p.product_type, SUM(pre_agg.trans_sum)
FROM products p
JOIN (
  SELECT t.product_id, SUM(t.transaction_amount) as trans_sum
  FROM transactions t
  WHERE t.transaction_date >= '2024-01-01'
  GROUP BY t.product_id
) pre_agg ON p.product_id = pre_agg.product_id
GROUP BY p.product_type;
```

## 九、总结

### 9.1 关键要点

1. **识别场景**: JOIN + GROUP BY + 聚合非GROUP BY列
2. **评估收益**: 预聚合能减少多少数据量
3. **手工改写**: 创建子查询先聚合大表
4. **充分验证**: 功能 + 性能双重验证
5. **优先级**: P0立即改写, P1计划改写, P2观察

### 9.2 性能预期

- **典型提升**: 3-10倍
- **极端案例**: 50倍以上 (大表小维度)
- **小幅提升**: 1.5-3倍 (中等数据量)

### 9.3 注意事项

⚠️ **不适用场景**:
- GROUP BY列包含多个表的字段
- 聚合函数全是COUNT(DISTINCT)
- 小数据量场景 (行数<10000)

⚠️ **改写风险**:
- 聚合语义必须保持一致
- COUNT(DISTINCT)需要特殊处理
- 需要充分测试验证

---

**文档维护**: 本文档随着VastBase版本更新和实战案例积累持续优化
**反馈渠道**: 遇到问题请记录到vastbase-sql-cases目录供后续分析
