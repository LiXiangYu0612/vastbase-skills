-- ============================================================================
-- VastBase Oracle查询转换迁移实战示例集
-- ============================================================================
-- 文档版本: v1.0
-- 创建日期: 2026-04-17
-- 适用场景: Oracle → VastBase迁移,手动实现Oracle查询转换优化
-- ============================================================================

-- ============================================================================
-- 第一部分: OR Expansion (OR展开) 改写示例
-- ============================================================================

-- ============================================================================
-- 示例 1.1: 基础OR条件改写
-- ============================================================================

-- ========== 问题SQL (可能全表扫描) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE (status = 'COMPLETED' AND amount > 1000)
   OR (status = 'PENDING' AND customer_id = 100);

-- ========== 改写后 (手动OR Expansion) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE status = 'COMPLETED' AND amount > 1000
UNION ALL
SELECT *
FROM orders
WHERE status = 'PENDING' AND customer_id = 100
  AND status != 'COMPLETED';  -- 避免重复

-- 性能对比:
-- 原SQL可能: Seq Scan on orders (100万行)
-- 改写后可能:
--   - Index Scan using idx_status_amount (1000行)
--   - Index Scan using idx_status_customer (10行)
-- 预期提升: 10-50倍

-- ============================================================================
-- 示例 1.2: 多个OR条件
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE (status = 'COMPLETED' AND amount > 1000)
   OR (status = 'PENDING' AND amount < 100)
   OR (status = 'CANCELLED' AND customer_id IN (1, 2, 3));

-- ========== 改写后 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT * FROM orders WHERE status = 'COMPLETED' AND amount > 1000
UNION ALL
SELECT * FROM orders WHERE status = 'PENDING' AND amount < 100
  AND status != 'COMPLETED'
UNION ALL
SELECT * FROM orders WHERE status = 'CANCELLED' AND customer_id IN (1, 2, 3)
  AND status NOT IN ('COMPLETED', 'PENDING');

-- ============================================================================
-- 示例 1.3: OR条件带子查询
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT o.*
FROM orders o
WHERE o.status = 'COMPLETED'
   OR o.customer_id IN (SELECT customer_id FROM vip_customers);

-- ========== 改写后 (方法1: 子查询展开) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT o.*
FROM orders o
WHERE o.status = 'COMPLETED'
UNION ALL
SELECT o.*
FROM orders o
WHERE o.customer_id IN (SELECT customer_id FROM vip_customers)
  AND o.status != 'COMPLETED';

-- ========== 改写后 (方法2: JOIN) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT o.*
FROM orders o
LEFT JOIN vip_customers v ON o.customer_id = v.customer_id
WHERE o.status = 'COMPLETED' OR v.customer_id IS NOT NULL;

-- ============================================================================
-- 示例 1.4: 复杂OR条件 (AND嵌套OR)
-- ============================================================================

-- ========== 问题SQL ==========
SELECT *
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE (o.status = 'COMPLETED' AND o.amount > 1000)
   OR (c.region = 'ASIA' AND o.status = 'PENDING');

-- ========== 改写后 ==========
SELECT o.*, c.*
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE o.status = 'COMPLETED' AND o.amount > 1000
UNION ALL
SELECT o.*, c.*
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.region = 'ASIA' 
  AND o.status = 'PENDING'
  AND o.status != 'COMPLETED';  -- 避免与第一个分支重复

-- ============================================================================
-- 示例 1.5: OR条件带聚合
-- ============================================================================

-- ========== 问题SQL ==========
SELECT c.customer_name, SUM(o.amount)
FROM customers c
JOIN orders o ON c.customer_id = o.customer_id
WHERE (c.region = 'ASIA' AND o.status = 'COMPLETED')
   OR (c.region = 'EUROPE' AND o.status = 'PENDING')
GROUP BY c.customer_name;

-- ========== 改写后 ==========
SELECT customer_name, SUM(amount) FROM (
  SELECT c.customer_name, o.amount
  FROM customers c
  JOIN orders o ON c.customer_id = o.customer_id
  WHERE c.region = 'ASIA' AND o.status = 'COMPLETED'
  
  UNION ALL
  
  SELECT c.customer_name, o.amount
  FROM customers c
  JOIN orders o ON c.customer_id = o.customer_id
  WHERE c.region = 'EUROPE' AND o.status = 'PENDING'
    AND NOT (c.region = 'ASIA' AND o.status = 'COMPLETED')
) combined
GROUP BY customer_name;

-- ============================================================================
-- 第二部分: Star Transformation (星型转换) 改写示例
-- ============================================================================

-- ============================================================================
-- 示例 2.1: 基础星型模型查询
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT d.year, d.quarter, r.region_name, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
JOIN regions r ON s.region_id = r.region_id
WHERE d.year = 2024
  AND r.region_name IN ('North', 'South')
GROUP BY d.year, d.quarter, r.region_name;

-- ========== 改写后 (维度表预过滤) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT d.year, d.quarter, r.region_name, SUM(s.sales_amount)
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
JOIN sales s ON s.date_id = d.date_id AND s.region_id = r.region_id
GROUP BY d.year, d.quarter, r.region_name;

-- ============================================================================
-- 示例 2.2: 多维度星型查询
-- ============================================================================

-- ========== 问题SQL ==========
SELECT d.year, r.region_name, c.category_name, SUM(s.sales_amount)
FROM sales s
JOIN dates d ON s.date_id = d.date_id
JOIN regions r ON s.region_id = r.region_id
JOIN categories c ON s.category_id = c.category_id
WHERE d.year = 2024
  AND r.region_name IN ('North', 'South')
  AND c.category_level_1 = 'Electronics'
GROUP BY d.year, r.region_name, c.category_name;

-- ========== 改写后 (多维度预过滤 + GROUP BY Placement) ==========
SELECT d.year, r.region_name, c.category_name, SUM(pre_agg.sales_sum)
FROM (
  SELECT date_id, year
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
GROUP BY d.year, r.region_name, c.category_name;

-- ============================================================================
-- 第三部分: 聚合物化改写示例
-- ============================================================================

-- ============================================================================
-- 示例 3.1: 标量子查询每行执行
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.department_name,
    (SELECT AVG(e.salary) 
     FROM employees e 
     WHERE e.department_id = d.department_id) as avg_salary,
    (SELECT COUNT(*) 
     FROM employees e 
     WHERE e.department_id = d.department_id) as emp_count
FROM departments d;

-- 问题: 每个department都执行两次子查询
-- 如果有100个部门,则执行200次子查询

-- ========== 改写后 (使用JOIN) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.department_name,
    dept_stats.avg_salary,
    dept_stats.emp_count
FROM departments d
JOIN (
  SELECT 
    department_id,
    AVG(salary) as avg_salary,
    COUNT(*) as emp_count
  FROM employees
  GROUP BY department_id
) dept_stats ON d.department_id = dept_stats.department_id;

-- 优化效果:
-- - 子查询只执行一次
-- - 100个部门只需1次聚合计算
-- 预期提升: 10-100倍 (部门数量越多,提升越大)

-- ============================================================================
-- 示例 3.2: 多个标量子查询
-- ============================================================================

-- ========== 问题SQL ==========
SELECT 
    c.customer_name,
    (SELECT SUM(o.amount) FROM orders o WHERE o.customer_id = c.customer_id) as total_orders,
    (SELECT COUNT(o.order_id) FROM orders o WHERE o.customer_id = c.customer_id) as order_count,
    (SELECT MAX(o.amount) FROM orders o WHERE o.customer_id = c.customer_id) as max_order
FROM customers c
WHERE c.region = 'ASIA';

-- ========== 改写后 ==========
SELECT 
    c.customer_name,
    order_stats.total_orders,
    order_stats.order_count,
    order_stats.max_order
FROM customers c
JOIN (
  SELECT 
    customer_id,
    SUM(amount) as total_orders,
    COUNT(order_id) as order_count,
    MAX(amount) as max_order
  FROM orders
  GROUP BY customer_id
) order_stats ON c.customer_id = order_stats.customer_id
WHERE c.region = 'ASIA';

-- ============================================================================
-- 第四部分: Distinct消除改写示例
-- ============================================================================

-- ============================================================================
-- 示例 4.1: JOIN + DISTINCT (可以消除)
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT d.department_name
FROM departments d
JOIN employees e ON d.department_id = e.department_id
WHERE e.salary > 5000;

-- 分析: department_id是主键,DISTINCT可以消除

-- ========== 改写后 (方法1: EXISTS) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT d.department_name
FROM departments d
WHERE EXISTS (
  SELECT 1 FROM employees e 
  WHERE e.department_id = d.department_id
    AND e.salary > 5000
);

-- ========== 改写后 (方法2: INNER JOIN + GROUP BY) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT d.department_name
FROM departments d
JOIN employees e ON d.department_id = e.department_id
WHERE e.salary > 5000
GROUP BY d.department_name;

-- ============================================================================
-- 示例 4.2: 半连接场景 (IN/EXISTS)
-- ============================================================================

-- ========== 问题SQL ==========
SELECT DISTINCT c.customer_name
FROM customers c
JOIN orders o ON c.customer_id = o.customer_id
WHERE o.order_date >= '2024-01-01';

-- ========== 改写后 (使用EXISTS) ==========
SELECT c.customer_name
FROM customers c
WHERE EXISTS (
  SELECT 1 FROM orders o 
  WHERE o.customer_id = c.customer_id
    AND o.order_date >= '2024-01-01'
);

-- ============================================================================
-- 第五部分: Join Predicate Pushdown改写示例
-- ============================================================================

-- ============================================================================
-- 示例 5.1: 视图JOIN条件下推
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM (
  SELECT *
  FROM orders
  WHERE amount > 100
) o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.region = 'ASIA';

-- 分析: c.region = 'ASIA'应该下推到子查询

-- ========== 改写后 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT o.*, c.*
FROM (
  SELECT o.*
  FROM orders o
  JOIN customers c ON o.customer_id = c.customer_id
  WHERE c.region = 'ASIA'
    AND o.amount > 100
) o
JOIN customers c ON o.customer_id = c.customer_id;

-- ============================================================================
-- 示例 5.2: CTE (WITH) 改写
-- ============================================================================

-- ========== 问题SQL ==========
WITH order_summary AS (
  SELECT customer_id, SUM(amount) as total_amount
  FROM orders
  GROUP BY customer_id
)
SELECT c.*, o.total_amount
FROM customers c
JOIN order_summary o ON c.customer_id = o.customer_id
WHERE c.region = 'ASIA';

-- ========== 改写后 (WHERE条件下推) ==========
WITH order_summary AS (
  SELECT o.customer_id, SUM(o.amount) as total_amount
  FROM orders o
  JOIN customers c ON o.customer_id = c.customer_id
  WHERE c.region = 'ASIA'
  GROUP BY o.customer_id
)
SELECT c.*, o.total_amount
FROM customers c
JOIN order_summary o ON c.customer_id = o.customer_id
WHERE c.region = 'ASIA';

-- ============================================================================
-- 第六部分: 综合实战案例
-- ============================================================================

-- ============================================================================
-- 示例 6.1: OR Expansion + GROUP BY Placement
-- ============================================================================

-- ========== 问题SQL ==========
SELECT d.region, SUM(o.amount)
FROM orders o
JOIN dates d ON o.date_id = d.date_id
WHERE (o.status = 'COMPLETED' AND o.amount > 1000)
   OR (o.status = 'PENDING' AND d.year = 2024)
GROUP BY d.region;

-- ========== 改写后 ==========
SELECT region, SUM(amount) FROM (
  SELECT d.region, o.amount
  FROM orders o
  JOIN dates d ON o.date_id = d.date_id
  WHERE o.status = 'COMPLETED' AND o.amount > 1000
  
  UNION ALL
  
  SELECT d.region, o.amount
  FROM orders o
  JOIN dates d ON o.date_id = d.date_id
  WHERE o.status = 'PENDING' AND d.year = 2024
    AND NOT (o.status = 'COMPLETED')
) combined
GROUP BY region;

-- 进一步优化: 如果每个分支数据量都大,可以分别聚合
SELECT d.region, SUM(pre_agg.amount_sum)
FROM dates d
JOIN (
  SELECT o.date_id, SUM(o.amount) as amount_sum
  FROM orders o
  WHERE o.status = 'COMPLETED' AND o.amount > 1000
  GROUP BY o.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region

UNION ALL

SELECT d.region, SUM(pre_agg.amount_sum)
FROM dates d
JOIN (
  SELECT o.date_id, SUM(o.amount) as amount_sum
  FROM orders o
  WHERE o.status = 'PENDING' AND o.date_id IN (
    SELECT date_id FROM dates WHERE year = 2024
  )
  GROUP BY o.date_id
) pre_agg ON d.date_id = pre_agg.date_id
WHERE d.year = 2024
GROUP BY d.region;

-- ============================================================================
-- 示例 6.2: 复杂数据仓库查询
-- ============================================================================

-- ========== 问题SQL (多个优化机会) ==========
SELECT 
    d.year,
    d.month,
    r.region_name,
    c.category_name,
    SUM(s.sales_amount) as total_sales,
    COUNT(DISTINCT s.customer_id) as unique_customers
FROM sales s
JOIN dates d ON s.date_id = d.date_id
JOIN regions r ON s.region_id = r.region_id
JOIN categories c ON s.category_id = c.category_id
WHERE (d.year = 2024 AND r.region_name IN ('North', 'South'))
   OR (d.year = 2023 AND r.region_name = 'East')
   AND c.category_level_1 = 'Electronics'
GROUP BY d.year, d.month, r.region_name, c.category_name;

-- 改写分析:
-- 1. OR条件 → OR Expansion
-- 2. 大表JOIN → GROUP BY Placement
-- 3. 多维度过滤 → 维度表预过滤

-- ========== 改写后 (综合优化) ==========
SELECT year, month, region_name, category_name, 
       SUM(sales_sum), COUNT(DISTINCT customer_id)
FROM (
  -- 2024年 North/South地区
  SELECT 
    d.year, d.month, r.region_name, c.category_name,
    pre_agg.sales_sum, pre_agg.customer_id
  FROM (
    SELECT date_id, year, month
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
    SELECT s.date_id, s.region_id, s.category_id, s.customer_id,
           SUM(s.sales_amount) as sales_sum
    FROM sales s
    WHERE s.date_id IN (SELECT date_id FROM dates WHERE year = 2024)
      AND s.region_id IN (SELECT region_id FROM regions WHERE region_name IN ('North', 'South'))
      AND s.category_id IN (SELECT category_id FROM categories WHERE category_level_1 = 'Electronics')
    GROUP BY s.date_id, s.region_id, s.category_id, s.customer_id
  ) pre_agg ON d.date_id = pre_agg.date_id
    AND r.region_id = pre_agg.region_id
    AND c.category_id = pre_agg.category_id
  
  UNION ALL
  
  -- 2023年 East地区
  SELECT 
    d.year, d.month, r.region_name, c.category_name,
    pre_agg.sales_sum, pre_agg.customer_id
  FROM (
    SELECT date_id, year, month
    FROM dates
    WHERE year = 2023
  ) d
  JOIN (
    SELECT region_id, region_name
    FROM regions
    WHERE region_name = 'East'
  ) r ON 1=1
  JOIN (
    SELECT category_id, category_name
    FROM categories
    WHERE category_level_1 = 'Electronics'
  ) c ON 1=1
  JOIN (
    SELECT s.date_id, s.region_id, s.category_id, s.customer_id,
           SUM(s.sales_amount) as sales_sum
    FROM sales s
    WHERE s.date_id IN (SELECT date_id FROM dates WHERE year = 2023)
      AND s.region_id IN (SELECT region_id FROM regions WHERE region_name = 'East')
      AND s.category_id IN (SELECT category_id FROM categories WHERE category_level_1 = 'Electronics')
    GROUP BY s.date_id, s.region_id, s.category_id, s.customer_id
  ) pre_agg ON d.date_id = pre_agg.date_id
    AND r.region_id = pre_agg.region_id
    AND c.category_id = pre_agg.category_id
) combined
GROUP BY year, month, region_name, category_name;

-- ============================================================================
-- 第七部分: 性能验证与测试
-- ============================================================================

-- ============================================================================
-- 示例 7.1: 创建测试数据
-- ============================================================================

-- 创建测试表
CREATE TABLE IF NOT EXISTS test_orders (
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT,
    status VARCHAR(20),
    amount NUMERIC(18,2),
    order_date DATE
) DISTRIBUTED BY (order_id);

CREATE TABLE IF NOT EXISTS test_customers (
    customer_id BIGINT PRIMARY KEY,
    customer_name VARCHAR(100),
    region VARCHAR(20)
) DISTRIBUTED BY (customer_id);

-- 插入测试数据
INSERT INTO test_customers
SELECT i, 'Customer ' || i, 
       CASE WHEN i % 3 = 0 THEN 'ASIA' 
            WHEN i % 3 = 1 THEN 'EUROPE' 
            ELSE 'AMERICA' END
FROM generate_series(1, 1000) i;

INSERT INTO test_orders
SELECT 
    i,
    (random() * 999 + 1)::INT::BIGINT,
    CASE WHEN random() < 0.33 THEN 'COMPLETED'
         WHEN random() < 0.66 THEN 'PENDING'
         ELSE 'CANCELLED' END,
    (random() * 5000 + 100)::NUMERIC(18,2),
    '2024-01-01'::DATE + (random() * 365)::INT
FROM generate_series(1, 100000) i;

ANALYZE test_orders;
ANALYZE test_customers;

-- ============================================================================
-- 示例 7.2: 性能对比测试
-- ============================================================================

\timing on

-- 测试1: OR Expansion
-- 原SQL
SELECT *
FROM test_orders
WHERE (status = 'COMPLETED' AND amount > 2000)
   OR (status = 'PENDING' AND customer_id < 100);

-- 改写后
SELECT * FROM test_orders WHERE status = 'COMPLETED' AND amount > 2000
UNION ALL
SELECT * FROM test_orders WHERE status = 'PENDING' AND customer_id < 100
  AND status != 'COMPLETED';

\timing off

-- ============================================================================
-- 示例 7.3: 结果一致性验证
-- ============================================================================

-- 验证1: 行数对比
SELECT COUNT(*) FROM (
  SELECT *
  FROM test_orders
  WHERE (status = 'COMPLETED' AND amount > 2000)
     OR (status = 'PENDING' AND customer_id < 100)
) original;

SELECT COUNT(*) FROM (
  SELECT * FROM test_orders WHERE status = 'COMPLETED' AND amount > 2000
  UNION ALL
  SELECT * FROM test_orders WHERE status = 'PENDING' AND customer_id < 100
    AND status != 'COMPLETED'
) rewritten;

-- 验证2: 结果集完全一致性
SELECT COUNT(*) FROM (
  (SELECT * FROM test_orders WHERE (status = 'COMPLETED' AND amount > 2000)
                                OR (status = 'PENDING' AND customer_id < 100))
   EXCEPT
  (SELECT * FROM test_orders WHERE status = 'COMPLETED' AND amount > 2000
   UNION ALL
   SELECT * FROM test_orders WHERE status = 'PENDING' AND customer_id < 100
     AND status != 'COMPLETED')
) diff;

-- 应该返回0 (完全一致)

-- ============================================================================
-- 第八部分: 自动检测工具
-- ============================================================================

-- 创建Oracle查询转换机会检测函数
CREATE OR REPLACE FUNCTION detect_transformation_opportunities()
RETURNS TABLE(
    query_id BIGINT,
    sql_text TEXT,
    has_or_condition BOOLEAN,
    has_group_by_join BOOLEAN,
    has_scalar_subquery BOOLEAN,
    has_distinct_join BOOLEAN,
    optimization_potential TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        ss.query_id,
        ss.query,
        ss.query LIKE '% OR %' AS has_or_condition,
        ss.query LIKE '%GROUP BY%' AND ss.query LIKE '%JOIN%' AS has_group_by_join,
        ss.query LIKE '%SELECT%SELECT%FROM%' AS has_scalar_subquery,
        ss.query LIKE '%DISTINCT%' AND ss.query LIKE '%JOIN%' AS has_distinct_join,
        CASE 
            WHEN ss.query LIKE '% OR %' THEN 'OR Expansion'
            WHEN ss.query LIKE '%GROUP BY%' AND ss.query LIKE '%JOIN%' THEN 'GROUP BY Placement'
            WHEN ss.query LIKE '%SELECT%SELECT%FROM%' THEN 'Subquery Materialization'
            WHEN ss.query LIKE '%DISTINCT%' AND ss.query LIKE '%JOIN%' THEN 'Distinct Elimination'
            ELSE 'Other'
        END as optimization_potential
    FROM pg_stat_statements ss
    WHERE ss.calls > 10
      AND ss.mean_exec_time > 100000
    ORDER BY ss.mean_exec_time DESC
    LIMIT 50;
END;
$$ LANGUAGE plpgsql;

-- 使用检测函数
SELECT * FROM detect_transformation_opportunities();

-- ============================================================================
-- 第九部分: 清理测试数据
-- ============================================================================

DROP TABLE IF EXISTS test_orders CASCADE;
DROP TABLE IF EXISTS test_customers CASCADE;
DROP FUNCTION IF EXISTS detect_transformation_opportunities();

-- ============================================================================
-- 总结
-- ============================================================================
--
-- Oracle查询转换手动改写核心要点:
--
-- 1. OR Expansion:
--    - 识别OR条件导致全表扫描
--    - 改写为UNION ALL
--    - 注意处理重复问题
--
-- 2. GROUP BY Placement:
--    - 识别JOIN后中间结果>>最终结果
--    - 先聚合大表,再JOIN
--    - 注意COUNT(DISTINCT)处理
--
-- 3. Star Transformation:
--    - 识别星型模型查询
--    - 维度表预过滤
--    - 结合GROUP BY Placement
--
-- 4. 聚合物化:
--    - 识别标量子查询每行执行
--    - 改写为JOIN
--    - 子查询只执行一次
--
-- 5. Distinct消除:
--    - 识别可以消除的DISTINCT
--    - 改写为EXISTS或GROUP BY
--
-- 6. Join Predicate Pushdown:
--    - 识别JOIN条件下推机会
--    - 将过滤条件下推到子查询
--
-- 典型性能提升: 3-50倍
--
-- ============================================================================
