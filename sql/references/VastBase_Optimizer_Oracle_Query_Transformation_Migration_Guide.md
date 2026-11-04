# Oracle查询转换迁移到VastBase综合指南

## 文档概述

**文档版本**: v1.0  
**创建日期**: 2026-04-17  
**适用场景**: Oracle → VastBase迁移性能优化  
**问题类型**: Oracle优化器支持的查询转换在VastBase中不支持或不完整  

## 一、Oracle查询转换技术概览

### 1.1 查询转换的定义

**查询转换 (Query Transformation)** 是Oracle优化器在生成执行计划之前,对SQL语句进行等价改写的技术,目的是找到更高效的执行路径。

**核心价值**:
- ✅ 自动优化SQL性能
- ✅ 无需手动改写SQL
- ✅ 利用物化视图、索引等对象
- ✅ 优化JOIN顺序和方式

### 1.2 Oracle主要查询转换技术

| 转换类型 | Oracle版本 | VastBase支持情况 | 迁移策略 |
|---------|-----------|----------------|---------|
| **简单视图合并** | 8i+ | ✅ 支持 | 无需改写 |
| **复杂视图合并** | 10g+ | ⚠️ 部分支持 | 需验证 |
| **子查询展开** | 8i+ | ✅ 支持 | 无需改写 |
| **子查询物化** | 8i+ | ❌ 不支持 | **需手动改写** |
| **谓词下推** | 8i+ | ✅ 支持 | 无需改写 |
| **GROUP BY Placement** | 11g+ | ❌ 不支持 | **需手动改写** |
| **Partial Join Evaluation** | 12c+ | ❌ 不支持 | **需手动改写** |
| **Join Predicate Pushdown** | 10g+ | ✅ 支持 | 无需改写 (优化器支持) |
| **OR Expansion** | 10g+ | ❌ 不支持 | **需手动改写** |
| **Star Transformation** | 8i+ | ❌ 不支持 | **需手动改写** |
| **聚合物化** | 10g+ | ❌ 不支持 | **需手动改写** |
| **Distinct消除** | 10g+ | ⚠️ 部分支持 | 需验证 |

## 二、OR Expansion (OR展开)

### 2.1 核心问题

**Oracle OR Expansion**: 将包含OR谓词的查询转换为UNION ALL查询,提升索引使用效率。

**转换示例**:
```sql
-- ========== 原SQL (Oracle自动OR Expansion) ==========
SELECT *
FROM orders
WHERE (status = 'COMPLETED' AND amount > 1000)
   OR (status = 'PENDING' AND customer_id = 100);

-- ========== Oracle自动转换为 ==========
SELECT * FROM orders WHERE status = 'COMPLETED' AND amount > 1000
UNION ALL
SELECT * FROM orders WHERE status = 'PENDING' AND customer_id = 100
-- 且确保两个子集不重复
```

**性能优势**:
- ✅ 可以使用不同索引
- ✅ 避免全表扫描
- ✅ 并行执行两个分支

**VastBase问题**:
- ❌ 优化器不支持OR Expansion
- ❌ 可能走全表扫描或低效索引

### 2.2 手动改写方法

**场景1: 简单OR条件**

```sql
-- ========== 问题SQL (VastBase性能差) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE (status = 'COMPLETED' AND amount > 1000)
   OR (status = 'PENDING' AND customer_id = 100);

-- 可能的执行计划问题:
-- - Seq Scan (全表扫描)
-- - Index Scan但过滤率低
-- - 无法使用两个不同索引

-- ========== 改写后 (手动OR Expansion) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE status = 'COMPLETED' AND amount > 1000
UNION ALL
SELECT *
FROM orders
WHERE status = 'PENDING' AND customer_id = 100;

-- 优化效果:
-- - 第一个分支: 可能使用idx_status_amount
-- - 第二个分支: 可能使用idx_status_customer
-- - 并行执行
-- 预期提升: 3-10倍
```

**场景2: 带子查询的OR**

```sql
-- ========== 问题SQL ==========
SELECT *
FROM orders o
WHERE o.status = 'COMPLETED'
   OR o.customer_id IN (SELECT customer_id FROM vip_customers);

-- ========== 改写后 ==========
SELECT o.*
FROM orders o
WHERE o.status = 'COMPLETED'
UNION ALL
SELECT o.*
FROM orders o
WHERE o.customer_id IN (SELECT customer_id FROM vip_customers)
  AND o.status != 'COMPLETED';  -- 确保不重复
```

**场景3: 多个OR条件**

```sql
-- ========== 问题SQL ==========
SELECT *
FROM orders
WHERE (status = 'COMPLETED' AND amount > 1000)
   OR (status = 'PENDING' AND amount < 100)
   OR (status = 'CANCELLED' AND customer_id IN (1, 2, 3));

-- ========== 改写后 ==========
SELECT * FROM orders WHERE status = 'COMPLETED' AND amount > 1000
UNION ALL
SELECT * FROM orders WHERE status = 'PENDING' AND amount < 100
UNION ALL
SELECT * FROM orders WHERE status = 'CANCELLED' AND customer_id IN (1, 2, 3);
```

### 2.3 避免重复的关键技巧

**问题**: UNION ALL可能产生重复行

**解决方案**:

**方法1: 添加互斥条件**
```sql
SELECT * FROM orders WHERE status = 'COMPLETED' AND amount > 1000
UNION ALL
SELECT * FROM orders 
WHERE status = 'PENDING' AND customer_id = 100
  AND status != 'COMPLETED';  -- 确保与第一个分支不重复
```

**方法2: 使用UNION (去重但性能稍差)**
```sql
SELECT * FROM orders WHERE status = 'COMPLETED' AND amount > 1000
UNION  -- 自动去重
SELECT * FROM orders WHERE status = 'PENDING' AND customer_id = 100;
```

**方法3: 使用ROWNUM或ROW_ID去重**
```sql
SELECT DISTINCT * FROM (
  SELECT * FROM orders WHERE status = 'COMPLETED' AND amount > 1000
  UNION ALL
  SELECT * FROM orders WHERE status = 'PENDING' AND customer_id = 100
);
```

### 2.4 性能验证

```sql
-- ========== 验证改写前后性能对比 ==========
-- 原SQL
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE (status = 'COMPLETED' AND amount > 1000)
   OR (status = 'PENDING' AND customer_id = 100);

-- 改写后
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM orders WHERE status = 'COMPLETED' AND amount > 1000
UNION ALL
SELECT * FROM orders WHERE status = 'PENDING' AND customer_id = 100;

-- 对比指标:
-- - 执行时间
-- - 扫描行数
-- - 是否使用索引
-- - 缓存命中率
```

## 三、Star Transformation (星型转换)

### 3.1 核心问题

**Oracle Star Transformation**: 针对星型模型(事实表JOIN多个维度表)的优化,使用位图索引进行高效过滤。

**典型场景**: 数据仓库查询
```sql
SELECT d.region, p.category, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
JOIN products p ON s.product_id = p.product_id
WHERE d.year = 2024
  AND p.brand = 'Apple'
GROUP BY d.region, p.category;
```

**Oracle优化**:
- 生成位图索引路径
- 先过滤维度表,再用位图索引过滤事实表
- 避免大表JOIN

**VastBase问题**:
- ❌ 不支持Star Transformation
- ❌ 可能JOIN大量数据后再过滤

### 3.2 手动改写方法

**策略1: 维度表预过滤**

```sql
-- ========== 问题SQL ==========
SELECT d.region, p.category, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
JOIN products p ON s.product_id = p.product_id
WHERE d.year = 2024
  AND p.brand = 'Apple'
GROUP BY d.region, p.category;

-- ========== 改写后 ==========
SELECT d.region, p.category, SUM(s.sales_amount)
FROM (
  SELECT date_id, region
  FROM dates
  WHERE year = 2024
) d
JOIN (
  SELECT product_id, category
  FROM products
  WHERE brand = 'Apple'
) p ON 1=1
JOIN sales s 
  ON s.date_id = d.date_id 
  AND s.product_id = p.product_id
GROUP BY d.region, p.category;
```

**策略2: 事实表子查询**

```sql
-- ========== 改写后 (结合GROUP BY Placement) ==========
SELECT d.region, p.category, SUM(pre_agg.sales_sum)
FROM (
  SELECT date_id, region
  FROM dates
  WHERE year = 2024
) d
JOIN (
  SELECT product_id, category
  FROM products
  WHERE brand = 'Apple'
) p ON 1=1
JOIN (
  SELECT s.date_id, s.product_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.date_id IN (SELECT date_id FROM dates WHERE year = 2024)
    AND s.product_id IN (SELECT product_id FROM products WHERE brand = 'Apple')
  GROUP BY s.date_id, s.product_id
) pre_agg ON d.date_id = pre_agg.date_id
  AND p.product_id = pre_agg.product_id
GROUP BY d.region, p.category;
```

## 四、聚合物化 (Aggregation Subquery)

### 4.1 核心问题

**Oracle聚合物化**: 将子查询中的聚合操作外推,避免重复计算。

**示例**:
```sql
-- ========== 原SQL ==========
SELECT d.department_name,
       (SELECT AVG(salary) FROM employees e WHERE e.department_id = d.department_id) as avg_salary
FROM departments d;
```

**Oracle优化**: 子查询只执行一次,缓存结果

**VastBase问题**: 可能每行都执行子查询

### 4.2 手动改写方法

```sql
-- ========== 问题SQL ==========
SELECT d.department_name,
       (SELECT AVG(e.salary) 
        FROM employees e 
        WHERE e.department_id = d.department_id) as avg_salary
FROM departments d;

-- ========== 改写后 (使用JOIN) ==========
SELECT d.department_name, dept_avg.avg_salary
FROM departments d
JOIN (
  SELECT department_id, AVG(salary) as avg_salary
  FROM employees
  GROUP BY department_id
) dept_avg ON d.department_id = dept_avg.department_id;
```

## 五、Distinct消除

### 5.1 核心问题

**Oracle Distinct消除**: 当SQL语义保证结果唯一时,自动消除DISTINCT操作。

**示例**:
```sql
-- ========== 原SQL ==========
SELECT DISTINCT d.department_name
FROM departments d
JOIN employees e ON d.department_id = e.department_id;
```

**Oracle优化**: 如果department_id是主键,自动消除DISTINCT

**VastBase问题**: 可能仍然执行DISTINCT操作

### 5.2 手动改写方法

```sql
-- ========== 问题SQL ==========
SELECT DISTINCT d.department_name
FROM departments d
JOIN employees e ON d.department_id = e.department_id;

-- ========== 改写后 (手动消除DISTINCT) ==========
SELECT d.department_name
FROM departments d
WHERE EXISTS (
  SELECT 1 FROM employees e WHERE e.department_id = d.department_id
);

-- 或使用INNER JOIN
SELECT d.department_name
FROM departments d
JOIN employees e ON d.department_id = e.department_id
GROUP BY d.department_name;
```

## 六、Join Predicate Pushdown (连接谓词下推)

### 6.1 核心问题

**Oracle Join Predicate Pushdown**: 将JOIN条件下推到视图或子查询内部,提前过滤数据。

**示例**:
```sql
-- ========== 原SQL ==========
SELECT *
FROM (
  SELECT * FROM orders WHERE amount > 100
) o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.region = 'ASIA';
```

**Oracle优化**: 将c.region = 'ASIA'下推到子查询

**VastBase问题**: 可能先生成完整子查询结果再JOIN

### 6.2 手动改写方法

```sql
-- ========== 问题SQL ==========
SELECT *
FROM (
  SELECT * FROM orders WHERE amount > 100
) o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.region = 'ASIA';

-- ========== 改写后 ==========
SELECT o.*, c.*
FROM (
  SELECT o.*
  FROM orders o
  JOIN customers c ON o.customer_id = c.customer_id
  WHERE c.region = 'ASIA'
    AND o.amount > 100
) o
JOIN customers c ON o.customer_id = c.customer_id;
```

## 七、综合实战案例

### 7.1 案例1: 电商订单查询

**业务场景**: 查询特定状态和金额范围的订单

```sql
-- ========== 问题SQL ==========
SELECT o.*, c.customer_name, p.product_name
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN products p ON o.product_id = p.product_id
WHERE (o.status = 'COMPLETED' AND o.amount > 1000)
   OR (o.status = 'PENDING' AND o.customer_id IN (SELECT customer_id FROM vip_customers));

-- ========== 改写后 (OR Expansion + 子查询展开) ==========
SELECT o.*, c.customer_name, p.product_name
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN products p ON o.product_id = p.product_id
WHERE o.status = 'COMPLETED' AND o.amount > 1000
UNION ALL
SELECT o.*, c.customer_name, p.product_name
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
JOIN products p ON o.product_id = p.product_id
WHERE o.status = 'PENDING'
  AND o.customer_id IN (SELECT customer_id FROM vip_customers)
  AND o.status != 'COMPLETED';  -- 避免重复
```

### 7.2 案例2: 数据仓库星型查询

**业务场景**: 销售数据多维分析

```sql
-- ========== 问题SQL ==========
SELECT d.year, d.quarter, r.region_name, c.category_name, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
JOIN regions r ON s.region_id = r.region_id
JOIN categories c ON s.category_id = c.category_id
WHERE d.year = 2024
  AND r.region_name IN ('North', 'South')
  AND c.category_level_1 = 'Electronics'
GROUP BY d.year, d.quarter, r.region_name, c.category_name;

-- ========== 改写后 (Star Transformation + GROUP BY Placement) ==========
SELECT d.year, d.quarter, r.region_name, c.category_name, SUM(pre_agg.sales_sum)
FROM (
  SELECT date_id, year, quarter
  FROM dates
  WHERE year = 2024
) d
JOIN (
  SELECT region_id, region_name
  FROM regions
  WHERE region_name IN ('North', 'South')
) r ON 1=1
JOIN (
  SELECT category_id, category_name
  FROM categories
  WHERE category_level_1 = 'Electronics'
) c ON 1=1
JOIN (
  SELECT s.date_id, s.region_id, s.category_id, SUM(s.sales_amount) as sales_sum
  FROM sales s
  WHERE s.date_id IN (SELECT date_id FROM dates WHERE year = 2024)
    AND s.region_id IN (SELECT region_id FROM regions WHERE region_name IN ('North', 'South'))
    AND s.category_id IN (SELECT category_id FROM categories WHERE category_level_1 = 'Electronics')
  GROUP BY s.date_id, s.region_id, s.category_id
) pre_agg ON d.date_id = pre_agg.date_id
  AND r.region_id = pre_agg.region_id
  AND c.category_id = pre_agg.category_id
GROUP BY d.year, d.quarter, r.region_name, c.category_name;
```

## 八、改写优先级与决策树

### 8.1 改写优先级

**P0 (立即改写)**:
- OR Expansion: OR条件导致全表扫描
- GROUP BY Placement: 中间结果>>最终结果
- Star Transformation: 星型模型大表查询
- 执行时间 > 5秒

**P1 (计划改写)**:
- 聚合物化: 标量子查询每行执行
- Join Predicate Pushdown: 子查询数据量过大
- 执行时间 > 1秒

**P2 (观察)**:
- Distinct消除: 性能影响通常较小
- 执行时间 < 1秒

### 8.2 决策树

```
慢SQL分析
  |
  ├─ 包含OR条件?
  │   └─ 是 → 手动OR Expansion (UNION ALL)
  │
  ├─ 包含JOIN + GROUP BY?
  │   └─ 是 → 检查中间结果 vs 最终结果
  │       └─ 比例>3:1 → GROUP BY Placement
  │
  ├─ 星型模型 (事实表JOIN多个维度表)?
  │   └─ 是 → 维度表预过滤 + GROUP BY Placement
  │
  ├─ 标量子查询?
  │   └─ 是 → 改写为JOIN
  │
  └─ 视图/子查询?
      └─ 是 → 检查JOIN条件下推机会
```

## 九、系统化识别与改写流程

### 9.1 自动检测函数

```sql
-- 创建Oracle查询转换候选检测函数
CREATE OR REPLACE FUNCTION detect_oracle_transformation_candidates()
RETURNS TABLE(
    sql_text TEXT,
    transformation_type TEXT,
    priority TEXT,
    reason TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        ss.query,
        CASE 
            WHEN ss.query LIKE '% OR %' THEN 'OR Expansion'
            WHEN ss.query LIKE '%GROUP BY%' AND ss.query LIKE '%JOIN%' THEN 'GROUP BY Placement'
            WHEN ss.query LIKE '%SELECT%SELECT%FROM%' THEN 'Subquery Materialization'
            ELSE 'Other'
        END as transformation_type,
        CASE 
            WHEN ss.mean_exec_time > 5000000 THEN 'P0'
            WHEN ss.mean_exec_time > 1000000 THEN 'P1'
            ELSE 'P2'
        END as priority,
        CASE 
            WHEN ss.query LIKE '% OR %' THEN 'OR condition may prevent index usage'
            WHEN ss.query LIKE '%GROUP BY%' AND ss.query LIKE '%JOIN%' THEN 'JOIN before aggregation'
            ELSE 'Review query transformation opportunities'
        END as reason
    FROM pg_stat_statements ss
    WHERE ss.calls > 10
      AND ss.mean_exec_time > 500000
    ORDER BY ss.mean_exec_time DESC
    LIMIT 100;
END;
$$ LANGUAGE plpgsql;

-- 使用检测函数
SELECT * FROM detect_oracle_transformation_candidates();
```

## 十、总结

### 10.1 关键要点

1. **识别转换类型**: 理解Oracle支持但VastBase不支持的转换
2. **评估性能影响**: 不是所有SQL都需要改写
3. **手工改写策略**: 掌握各种转换的手动实现方法
4. **验证正确性**: 确保改写后功能完全一致
5. **性能验证**: 对比改写前后的性能提升

### 10.2 典型性能提升

| 转换类型 | 典型提升 | 极端案例 |
|---------|---------|---------|
| OR Expansion | 3-10倍 | 100倍 (多索引场景) |
| GROUP BY Placement | 3-10倍 | 50倍 (大表小维度) |
| Star Transformation | 5-15倍 | 100倍 (星型模型) |
| 聚合物化 | 2-5倍 | 20倍 (多行子查询) |

### 10.3 注意事项

⚠️ **不适用场景**:
- 小数据量 (行数<10000)
- 执行时间<100ms
- 改写后性能提升不明显

⚠️ **改写风险**:
- 必须确保功能完全一致
- UNION ALL需要处理重复问题
- 复杂SQL改写难度大

⚠️ **维护成本**:
- 改写后的SQL可读性降低
- 需要充分测试
- 建议添加注释说明改写原因

---

**文档维护**: 本文档随着Oracle迁移经验积累持续优化
**参考文档**: 
- VastBase_Optimizer_Oracle_PJE_Migration_Deep_Dive.md
- VastBase_Optimizer_Oracle_GROUP_BY_Placement_Migration_Deep_Dive.md
