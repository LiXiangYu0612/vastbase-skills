-- ============================================================================
-- VastBase Oracle GROUP BY Placement 迁移实战示例集
-- ============================================================================
-- 文档版本: v1.0
-- 创建日期: 2026-04-17
-- 适用场景: Oracle → VastBase迁移,手动实现GROUP BY Placement优化
-- ============================================================================

-- ============================================================================
-- 第一部分: 基础场景示例 (单表JOIN + GROUP BY)
-- ============================================================================

-- ============================================================================
-- 示例 1.1: 基础两表JOIN场景
-- ============================================================================
-- 业务场景: 统计各地区的销售总额
-- 问题: 先JOIN大量数据,再聚合到少量结果
-- 优化: 先聚合事实表,再JOIN维度表

-- ========== 问题SQL (Oracle 10g风格) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(s.sales_amount) as total_sales
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
  AND d.year = 2024
GROUP BY d.region_name;

-- 执行计划分析:
-- 预期问题:
-- - Hash Join: 大数据量JOIN
-- - 中间结果: 100万+ 行
-- - 最终结果: 50 行 (region_name)
-- - 性能问题: 中间结果远大于最终结果

-- ========== 优化后SQL (手动GROUP BY Placement) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales) as total_sales
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
WHERE d.year = 2024
GROUP BY d.region_name;

-- 优化原理:
-- 1. 子查询先聚合sales表: 按date_id聚合
-- 2. 聚合效果: 100万行 → 365行 (每天一条)
-- 3. JOIN数据量: 365行 × 365行 (vs 原来百万级)
-- 4. 预期性能提升: 5-10倍

-- ========== 验证结果一致性 ==========
-- 验证1: 行数对比
SELECT COUNT(*) FROM (
    SELECT d.region_name, SUM(s.sales_amount)
    FROM sales s
    JOIN dates d ON s.date_id = d.date_id
    WHERE s.sales_date >= '2024-01-01'
      AND d.year = 2024
    GROUP BY d.region_name
) original;

SELECT COUNT(*) FROM (
    SELECT d.region_name, SUM(pre_agg.daily_sales)
    FROM dates d
    JOIN (
        SELECT s.date_id, SUM(s.sales_amount) as daily_sales
        FROM sales s
        WHERE s.sales_date >= '2024-01-01'
        GROUP BY s.date_id
    ) pre_agg ON d.date_id = pre_agg.date_id
    WHERE d.year = 2024
    GROUP BY d.region_name
) rewritten;

-- 验证2: 结果集完全一致性检查
SELECT 
    o.region_name,
    o.total_sales,
    r.total_sales,
    CASE 
        WHEN o.total_sales = r.total_sales THEN 'MATCH'
        ELSE 'MISMATCH'
    END as status
FROM (
    SELECT d.region_name, SUM(s.sales_amount) as total_sales
    FROM sales s
    JOIN dates d ON s.date_id = d.date_id
    WHERE s.sales_date >= '2024-01-01'
      AND d.year = 2024
    GROUP BY d.region_name
) o
FULL OUTER JOIN (
    SELECT d.region_name, SUM(pre_agg.daily_sales) as total_sales
    FROM dates d
    JOIN (
        SELECT s.date_id, SUM(s.sales_amount) as daily_sales
        FROM sales s
        WHERE s.sales_date >= '2024-01-01'
        GROUP BY s.date_id
    ) pre_agg ON d.date_id = pre_agg.date_id
    WHERE d.year = 2024
    GROUP BY d.region_name
) r ON o.region_name = r.region_name;

-- ============================================================================
-- 示例 1.2: 多个聚合函数场景
-- ============================================================================
-- 业务场景: 统计各地区销售总额、平均值、订单数
-- 关键点: 多个聚合函数都要在子查询中计算

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(s.sales_amount) as total_sales,
    AVG(s.sales_amount) as avg_sales,
    COUNT(s.order_id) as order_count,
    MIN(s.sales_amount) as min_sales,
    MAX(s.sales_amount) as max_sales
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY d.region_name;

-- ========== 优化后SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(pre_agg.daily_total),
    AVG(pre_agg.daily_avg),
    SUM(pre_agg.daily_count),
    MIN(pre_agg.daily_min),
    MAX(pre_agg.daily_max)
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_total,
        AVG(s.sales_amount) as daily_avg,
        COUNT(s.order_id) as daily_count,
        MIN(s.sales_amount) as daily_min,
        MAX(s.sales_amount) as daily_max
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- 注意: 
-- - SUM/COUNT/MIN/MAX: 先在子查询中按天聚合,外层再聚合
-- - AVG: 需要特殊处理,这里用SUM(avg)可能不准确,需要用加权平均
-- - 更准确的AVG处理: SUM(daily_total) / SUM(daily_count)

-- ========== AVG函数的准确处理方式 ==========
SELECT 
    d.region_name,
    SUM(pre_agg.daily_total) as total_sales,
    SUM(pre_agg.daily_total) / NULLIF(SUM(pre_agg.daily_count), 0) as avg_sales,
    SUM(pre_agg.daily_count) as order_count,
    MIN(pre_agg.daily_min) as min_sales,
    MAX(pre_agg.daily_max) as max_sales
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_total,
        COUNT(s.order_id) as daily_count,
        MIN(s.sales_amount) as daily_min,
        MAX(s.sales_amount) as daily_max
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- ============================================================================
-- 示例 1.3: COUNT(DISTINCT)特殊处理
-- ============================================================================
-- 业务场景: 统计各地区销售额和唯一客户数
-- 关键点: COUNT(DISTINCT)不能完全在子查询中聚合

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(s.sales_amount) as total_sales,
    COUNT(DISTINCT s.customer_id) as unique_customers
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY d.region_name;

-- ========== 优化后SQL (注意COUNT(DISTINCT)的处理) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales),
    COUNT(DISTINCT pre_agg.customer_id) as unique_customers
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        s.customer_id,  -- 必须保留用于外层COUNT(DISTINCT)
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id, s.customer_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- 关键点:
-- - COUNT(DISTINCT customer_id): 需要保留customer_id到外层
-- - 子查询GROUP BY必须包含customer_id
-- - 外层仍然使用COUNT(DISTINCT)去重
-- - 性能提升主要来自SUM的预聚合,COUNT(DISTINCT)仍需完整计算

-- ============================================================================
-- 第二部分: 多表JOIN场景
-- ============================================================================

-- ============================================================================
-- 示例 2.1: 三表JOIN - 事实表JOIN两个维度表
-- ============================================================================
-- 业务场景: 统计各地区各产品类别的销售总额

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    c.country_name,
    p.product_category,
    SUM(s.sales_amount) as total_sales
FROM sales s
JOIN customers c ON s.customer_id = c.customer_id
JOIN products p ON s.product_id = p.product_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY c.country_name, p.product_category;

-- 数据量分析:
-- - sales表: 100万行
-- - customers表: 5000行 (50个国家)
-- - products表: 1000行 (10个类别)
-- - 最终结果: 50 × 10 = 500行
-- - 问题: 100万行先JOIN再聚合

-- ========== 优化策略1: 只预聚合sales表 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    c.country_name,
    p.product_category,
    SUM(pre_agg.sales_sum)
FROM customers c
JOIN products p ON 1=1  -- 产生笛卡尔积: 50 × 10 = 500行
JOIN (
    SELECT 
        s.customer_id,
        s.product_id,
        SUM(s.sales_amount) as sales_sum
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND p.product_id = pre_agg.product_id
GROUP BY c.country_name, p.product_category;

-- 优化效果:
-- - sales预聚合: 100万行 → (customer_id, product_id)组合数
-- - 维度表笛卡尔积: 50 × 10 = 500行 (小数据量)
-- - JOIN: 500行 × 预聚合结果 (vs 原来100万行JOIN)
-- - 预期提升: 5-8倍

-- ========== 优化策略2: 分别预聚合sales和products ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    c.country_name,
    pre_prod.product_category,
    SUM(pre_agg.sales_sum)
FROM customers c
JOIN (
    SELECT 
        p.product_id,
        p.product_category
    FROM products p
    GROUP BY p.product_id, p.product_category
) pre_prod ON 1=1
JOIN (
    SELECT 
        s.customer_id,
        s.product_id,
        SUM(s.sales_amount) as sales_sum
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND pre_prod.product_id = pre_agg.product_id
GROUP BY c.country_name, pre_prod.product_category;

-- 这种策略意义不大,因为products表本身就小

-- ============================================================================
-- 示例 2.2: 星型模型 - 事实表JOIN多个维度表
-- ============================================================================
-- 业务场景: 数据仓库典型星型模型查询

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.year,
    d.month,
    c.country_name,
    p.product_category,
    SUM(s.sales_amount) as total_sales,
    COUNT(s.order_id) as order_count
FROM sales s
JOIN dates d ON s.date_id = d.date_id
JOIN customers c ON s.customer_id = c.customer_id
JOIN products p ON s.product_id = p.product_id
WHERE s.sales_date BETWEEN '2024-01-01' AND '2024-12-31'
  AND c.region = 'ASIA'
  AND p.category_level_1 = 'ELECTRONICS'
GROUP BY d.year, d.month, c.country_name, p.product_category;

-- ========== 优化后SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.year,
    d.month,
    c.country_name,
    p.product_category,
    SUM(pre_agg.sales_sum),
    SUM(pre_agg.order_count)
FROM (
    SELECT date_id, year, month
    FROM dates
    WHERE year = 2024
) d
JOIN (
    SELECT customer_id, country_name
    FROM customers
    WHERE region = 'ASIA'
) c ON 1=1
JOIN (
    SELECT product_id, product_category
    FROM products
    WHERE category_level_1 = 'ELECTRONICS'
) p ON 1=1
JOIN (
    SELECT 
        s.date_id,
        s.customer_id,
        s.product_id,
        SUM(s.sales_amount) as sales_sum,
        COUNT(s.order_id) as order_count
    FROM sales s
    WHERE s.sales_date BETWEEN '2024-01-01' AND '2024-12-31'
    GROUP BY s.date_id, s.customer_id, s.product_id
) pre_agg ON d.date_id = pre_agg.date_id
  AND c.customer_id = pre_agg.customer_id
  AND p.product_id = pre_agg.product_id
GROUP BY d.year, d.month, c.country_name, p.product_category;

-- ============================================================================
-- 第三部分: 层次聚合场景
-- ============================================================================

-- ============================================================================
-- 示例 3.1: GROUP BY列层次高于JOIN列
-- ============================================================================
-- 业务场景: 按月份聚合,但JOIN键是日期

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.month,
    SUM(s.sales_amount) as monthly_sales
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE d.year = 2024
GROUP BY d.month;

-- 数据分析:
-- - sales表: 100万行 (2024年366天)
-- - dates表: 366行
-- - JOIN列: date_id (366个值)
-- - GROUP BY列: month (12个值)
-- - 问题: 100万行JOIN后只聚合到12行

-- ========== 优化后SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.month,
    SUM(pre_agg.daily_sales) as monthly_sales
FROM (
    SELECT date_id, month
    FROM dates
    WHERE year = 2024
) d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
      AND s.sales_date < '2025-01-01'
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.month;

-- 优化原理:
-- 1. 先按date_id聚合: 100万行 → 366行
-- 2. JOIN: 366行 × 366行 (vs 原来100万行)
-- 3. 再按month聚合: 366行 → 12行
-- 预期提升: 8-15倍

-- ============================================================================
-- 示例 3.2: 多层聚合 - 日期层次结构
-- ============================================================================
-- 业务场景: 按季度聚合,JOIN键是日期

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.year,
    d.quarter,
    SUM(s.sales_amount) as quarterly_sales,
    COUNT(DISTINCT s.customer_id) as unique_customers
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE d.year BETWEEN 2020 AND 2024
GROUP BY d.year, d.quarter;

-- ========== 优化后SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.year,
    d.quarter,
    SUM(pre_agg.daily_sales),
    COUNT(DISTINCT pre_agg.customer_id)
FROM (
    SELECT date_id, year, quarter
    FROM dates
    WHERE year BETWEEN 2020 AND 2024
) d
JOIN (
    SELECT 
        s.date_id,
        s.customer_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2020-01-01'
      AND s.sales_date < '2025-01-01'
    GROUP BY s.date_id, s.customer_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.year, d.quarter;

-- ============================================================================
-- 第四部分: HAVING和WHERE条件复杂场景
-- ============================================================================

-- ============================================================================
-- 示例 4.1: GROUP BY + HAVING
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(s.sales_amount) as total_sales
FROM sales s
JOIN dates d ON s.date_id = d.date_id
WHERE s.sales_date >= '2024-01-01'
GROUP BY d.region_name
HAVING SUM(s.sales_amount) > 1000000;

-- ========== 优化后SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales) as total_sales
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name
HAVING SUM(pre_agg.daily_sales) > 1000000;

-- ============================================================================
-- 示例 4.2: 复杂WHERE条件
-- ============================================================================

-- ========== 问题SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    c.country_name,
    p.product_category,
    SUM(s.sales_amount)
FROM sales s
JOIN customers c ON s.customer_id = c.customer_id
JOIN products p ON s.product_id = p.product_id
WHERE s.sales_date BETWEEN '2024-01-01' AND '2024-03-31'
  AND c.region = 'ASIA'
  AND p.category_level_1 IN ('ELECTRONICS', 'FURNITURE')
  AND s.sales_amount > 100
GROUP BY c.country_name, p.product_category;

-- ========== 优化后SQL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    c.country_name,
    p.product_category,
    SUM(pre_agg.sales_sum)
FROM (
    SELECT customer_id, country_name
    FROM customers
    WHERE region = 'ASIA'
) c
JOIN (
    SELECT product_id, product_category
    FROM products
    WHERE category_level_1 IN ('ELECTRONICS', 'FURNITURE')
) p ON 1=1
JOIN (
    SELECT 
        s.customer_id,
        s.product_id,
        SUM(s.sales_amount) as sales_sum
    FROM sales s
    WHERE s.sales_date BETWEEN '2024-01-01' AND '2024-03-31'
      AND s.sales_amount > 100
    GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND p.product_id = pre_agg.product_id
GROUP BY c.country_name, p.product_category;

-- ============================================================================
-- 第五部分: 常见错误示例与修正
-- ============================================================================

-- ============================================================================
-- 示例 5.1: 错误 - JOIN列遗漏
-- ============================================================================

-- ❌ 错误改写
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales)
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    -- 缺少 GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- 错误: 子查询缺少GROUP BY,导致语法错误

-- ✅ 正确改写
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales)
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id  -- 必须包含
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- ============================================================================
-- 示例 5.2: 错误 - WHERE条件丢失
-- ============================================================================

-- ❌ 错误改写
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales)
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
-- 缺少 d.year = 2024 的过滤
GROUP BY d.region_name;

-- ✅ 正确改写
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales)
FROM (
    SELECT date_id, region_name
    FROM dates
    WHERE year = 2024
) d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    WHERE s.sales_date >= '2024-01-01'
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- ============================================================================
-- 示例 5.3: 错误 - 多表JOIN条件遗漏
-- ============================================================================

-- ❌ 错误改写
SELECT 
    c.country_name,
    p.product_category,
    SUM(pre_agg.sales_sum)
FROM customers c
JOIN products p ON 1=1
JOIN (
    SELECT 
        s.customer_id,
        s.product_id,
        SUM(s.sales_amount) as sales_sum
    FROM sales s
    GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
-- 缺少 p.product_id = pre_agg.product_id
GROUP BY c.country_name, p.product_category;

-- ✅ 正确改写
SELECT 
    c.country_name,
    p.product_category,
    SUM(pre_agg.sales_sum)
FROM customers c
JOIN products p ON 1=1
JOIN (
    SELECT 
        s.customer_id,
        s.product_id,
        SUM(s.sales_amount) as sales_sum
    FROM sales s
    GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND p.product_id = pre_agg.product_id  -- 必须包含
GROUP BY c.country_name, p.product_category;

-- ============================================================================
-- 示例 5.4: 错误 - COUNT(DISTINCT)处理不当
-- ============================================================================

-- ❌ 错误改写: 丢失customer_id
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales),
    COUNT(DISTINCT pre_agg.customer_id) as unique_customers
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        SUM(s.sales_amount) as daily_sales,
        COUNT(DISTINCT s.customer_id) as unique_customers  -- 错误
    FROM sales s
    GROUP BY s.date_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- 问题: COUNT(DISTINCT)在子查询中按天去重,不是全局去重

-- ✅ 正确改写: 保留customer_id
SELECT 
    d.region_name,
    SUM(pre_agg.daily_sales),
    COUNT(DISTINCT pre_agg.customer_id) as unique_customers
FROM dates d
JOIN (
    SELECT 
        s.date_id,
        s.customer_id,  -- 保留
        SUM(s.sales_amount) as daily_sales
    FROM sales s
    GROUP BY s.date_id, s.customer_id
) pre_agg ON d.date_id = pre_agg.date_id
GROUP BY d.region_name;

-- ============================================================================
-- 第六部分: 性能测试与验证
-- ============================================================================

-- ============================================================================
-- 示例 6.1: 创建测试数据
-- ============================================================================

-- 创建测试表
CREATE TABLE IF NOT EXISTS test_sales (
    order_id BIGINT,
    customer_id BIGINT,
    product_id BIGINT,
    date_id INT,
    sales_amount NUMERIC(18,2),
    sales_date DATE
) DISTRIBUTED BY (order_id);

CREATE TABLE IF NOT EXISTS test_customers (
    customer_id BIGINT PRIMARY KEY,
    country_name VARCHAR(50),
    region VARCHAR(20)
) DISTRIBUTED BY (customer_id);

CREATE TABLE IF NOT EXISTS test_products (
    product_id BIGINT PRIMARY KEY,
    product_category VARCHAR(50)
) DISTRIBUTED BY (product_id);

CREATE TABLE IF NOT EXISTS test_dates (
    date_id INT PRIMARY KEY,
    date DATE,
    year INT,
    month INT,
    region_name VARCHAR(50)
) DISTRIBUTED BY (date_id);

-- 插入测试数据
INSERT INTO test_dates
SELECT 
    i,
    '2024-01-01'::DATE + (i - 1),
    2024,
    EXTRACT(MONTH FROM ('2024-01-01'::DATE + (i - 1))),
    CASE WHEN i % 5 = 0 THEN 'NORTH'
         WHEN i % 5 = 1 THEN 'SOUTH'
         WHEN i % 5 = 2 THEN 'EAST'
         WHEN i % 5 = 3 THEN 'WEST'
         ELSE 'CENTRAL'
    END
FROM generate_series(1, 366) i;

INSERT INTO test_customers
SELECT 
    i,
    CASE WHEN i % 10 = 0 THEN 'USA'
         WHEN i % 10 = 1 THEN 'CHINA'
         WHEN i % 10 = 2 THEN 'JAPAN'
         WHEN i % 10 = 3 THEN 'GERMANY'
         ELSE 'UK'
    END,
    CASE WHEN i % 2 = 0 THEN 'ASIA' ELSE 'EUROPE' END
FROM generate_series(1, 1000) i;

INSERT INTO test_products
SELECT 
    i,
    CASE WHEN i % 5 = 0 THEN 'ELECTRONICS'
         WHEN i % 5 = 1 THEN 'FURNITURE'
         WHEN i % 5 = 2 THEN 'CLOTHING'
         WHEN i % 5 = 3 THEN 'FOOD'
         ELSE 'BOOKS'
    END
FROM generate_series(1, 100) i;

INSERT INTO test_sales
SELECT 
    i,
    (random() * 999 + 1)::INT::BIGINT,
    (random() * 99 + 1)::INT::BIGINT,
    (random() * 365 + 1)::INT,
    (random() * 1000 + 100)::NUMERIC(18,2),
    '2024-01-01'::DATE + (random() * 365)::INT
FROM generate_series(1, 100000) i;

ANALYZE test_sales;
ANALYZE test_customers;
ANALYZE test_products;
ANALYZE test_dates;

-- ============================================================================
-- 示例 6.2: 性能对比测试
-- ============================================================================

-- 测试1: 基础GROUP BY Placement
\timing on

-- 原SQL
SELECT c.region, SUM(s.sales_amount)
FROM test_sales s
JOIN test_customers c ON s.customer_id = c.customer_id
GROUP BY c.region;

-- 优化后SQL
SELECT c.region, SUM(pre_agg.sales_sum)
FROM test_customers c
JOIN (
    SELECT s.customer_id, SUM(s.sales_amount) as sales_sum
    FROM test_sales s
    GROUP BY s.customer_id
) pre_agg ON c.customer_id = pre_agg.customer_id
GROUP BY c.region;

\timing off

-- 测试2: 三表JOIN
\timing on

-- 原SQL
SELECT c.country_name, p.product_category, SUM(s.sales_amount)
FROM test_sales s
JOIN test_customers c ON s.customer_id = c.customer_id
JOIN test_products p ON s.product_id = p.product_id
GROUP BY c.country_name, p.product_category;

-- 优化后SQL
SELECT c.country_name, p.product_category, SUM(pre_agg.sales_sum)
FROM test_customers c
JOIN test_products p ON 1=1
JOIN (
    SELECT s.customer_id, s.product_id, SUM(s.sales_amount) as sales_sum
    FROM test_sales s
    GROUP BY s.customer_id, s.product_id
) pre_agg ON c.customer_id = pre_agg.customer_id
  AND p.product_id = pre_agg.product_id
GROUP BY c.country_name, p.product_category;

\timing off

-- ============================================================================
-- 示例 6.3: 自动检测GROUP BY Placement候选SQL
-- ============================================================================

-- 创建检测函数
CREATE OR REPLACE FUNCTION detect_gbp_candidates()
RETURNS TABLE(
    sql_text TEXT,
    has_group_by BOOLEAN,
    has_join BOOLEAN,
    has_aggregation BOOLEAN,
    is_gbp_candidate BOOLEAN
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        ss.query,
        ss.query LIKE '%GROUP BY%' AS has_group_by,
        ss.query LIKE '%JOIN%' AS has_join,
        ss.query LIKE '%SUM(%' OR ss.query LIKE '%COUNT(%' OR ss.query LIKE '%AVG(%' AS has_aggregation,
        CASE 
            WHEN ss.query LIKE '%GROUP BY%' 
              AND ss.query LIKE '%JOIN%'
              AND (ss.query LIKE '%SUM(%' OR ss.query LIKE '%COUNT(%' OR ss.query LIKE '%AVG(%')
              AND ss.mean_exec_time > 100000
            THEN TRUE
            ELSE FALSE
        END AS is_gbp_candidate
    FROM pg_stat_statements ss
    WHERE ss.calls > 10
    ORDER BY ss.mean_exec_time DESC
    LIMIT 50;
END;
$$ LANGUAGE plpgsql;

-- 使用检测函数
SELECT * FROM detect_gbp_candidates()
WHERE is_gbp_candidate = TRUE;

-- ============================================================================
-- 第七部分: 清理测试数据
-- ============================================================================

-- 清理测试表
DROP TABLE IF EXISTS test_sales CASCADE;
DROP TABLE IF EXISTS test_customers CASCADE;
DROP TABLE IF EXISTS test_products CASCADE;
DROP TABLE IF EXISTS test_dates CASCADE;

-- 删除检测函数
DROP FUNCTION IF EXISTS detect_gbp_candidates();

-- ============================================================================
-- 总结
-- ============================================================================
-- 
-- GROUP BY Placement优化核心要点:
-- 
-- 1. 识别场景: JOIN + GROUP BY + 聚合非GROUP BY列
-- 2. 评估收益: 预聚合能减少多少数据量
-- 3. 改写方法: 创建子查询先聚合大表
-- 4. 关键点:
--    - 子查询GROUP BY必须包含所有JOIN列
--    - 保留所有WHERE条件到相应位置
--    - COUNT(DISTINCT)需要特殊处理
--    - AVG函数需要用加权平均公式
-- 5. 验证: 功能正确性 + 性能提升
-- 
-- 典型性能提升: 3-10倍
-- 极端案例: 50倍以上 (大表小维度)
-- 
-- ============================================================================
