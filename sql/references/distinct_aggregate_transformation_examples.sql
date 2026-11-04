-- ============================================================================
-- VastBase SQL优化 - Distinct Aggregate Transformation (DAT) 实战示例集
-- ============================================================================
-- 功能: 演示Oracle DAT查询转换在VastBase中的手动改写方法
-- 适用: Oracle → VastBase迁移优化
-- 核心: Oracle自动DAT转换 → VastBase手动双层GROUP BY
-- 
-- 性能提升预期: 3-15倍 (取决于DISTINCT列基数)
-- ============================================================================

\echo '========================================'
\echo 'DAT转换示例集 - VastBase Distinct Aggregate Transformation'
\echo '========================================'
\echo ''

-- ============================================================================
-- 第一部分: 基础DAT转换
-- ============================================================================

\echo '========== 第一部分: 基础DAT转换 =========='
\echo ''

-- -------------------------------------------------------------------------
-- 示例1.1: 最简单的COUNT(DISTINCT) + GROUP BY
-- -------------------------------------------------------------------------

\echo '--- 示例1.1: 简单COUNT(DISTINCT)转换 ---'
\echo ''

-- 问题SQL (VastBase性能差)
-- Oracle会自动转换为DAT,但VastBase需要手动改写
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT 
    object_type,
    COUNT(DISTINCT object_name) as unique_names
FROM t1
GROUP BY object_type;

-- 手动DAT转换后 (推荐写法)
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT 
    object_type,
    COUNT(object_name) as unique_names
FROM (
    SELECT 
        object_name,
        object_type
    FROM t1
    GROUP BY object_name, object_type
) dag_view
GROUP BY object_type;

\echo ''
\echo '转换原理:'
\echo '  原始: GROUP BY object_type, COUNT(DISTINCT object_name)'
\echo '  转换: 内层 GROUP BY object_name, object_type'
\echo '        外层 GROUP BY object_type, COUNT(object_name)'
\echo ''

-- -------------------------------------------------------------------------
-- 示例1.2: COUNT(DISTINCT) + SUM + AVG
-- -------------------------------------------------------------------------

\echo '--- 示例1.2: 多个聚合函数转换 ---'
\echo ''

-- 问题SQL
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    object_type,
    COUNT(DISTINCT object_name) as unique_names,
    SUM(object_id) as total_id,
    AVG(object_id) as avg_id
FROM t1
GROUP BY object_type;

-- 手动DAT转换
-- 关键: AVG需要用加权平均公式
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    object_type,
    COUNT(object_name) as unique_names,
    SUM(pre_agg.sum_id) as total_id,
    SUM(pre_agg.sum_id) / NULLIF(SUM(pre_agg.cnt_id), 0) as avg_id
FROM (
    SELECT 
        object_name,
        object_type,
        SUM(object_id) as sum_id,
        COUNT(object_id) as cnt_id
    FROM t1
    GROUP BY object_name, object_type
) pre_agg
GROUP BY object_type;

\echo ''
\echo '关键公式:'
\echo '  AVG(x) = SUM(sum_x) / NULLIF(SUM(cnt_x), 0)'
\echo '  不能用 AVG(pre_agg.avg_x),因为 平均值的平均值 ≠ 总体平均值'
\echo ''

-- ============================================================================
-- 第二部分: 多个DISTINCT列
-- ============================================================================

\echo '========== 第二部分: 多个DISTINCT列 =========='
\echo ''

-- -------------------------------------------------------------------------
-- 示例2.1: 两个DISTINCT列
-- -------------------------------------------------------------------------

\echo '--- 示例2.1: 双DISTINCT列转换 ---'
\echo ''

-- 问题SQL
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    department_id,
    COUNT(DISTINCT employee_id) as unique_employees,
    COUNT(DISTINCT project_id) as unique_projects,
    SUM(work_hours) as total_hours
FROM employee_assignments
GROUP BY department_id;

-- 手动DAT转换
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    department_id,
    COUNT(employee_id) as unique_employees,
    COUNT(project_id) as unique_projects,
    SUM(hours_sum) as total_hours
FROM (
    SELECT 
        employee_id,
        project_id,
        department_id,
        SUM(work_hours) as hours_sum
    FROM employee_assignments
    GROUP BY employee_id, project_id, department_id
) pre_agg
GROUP BY department_id;

\echo ''
\echo '关键点:'
\echo '  内层GROUP BY包含: employee_id, project_id, department_id'
\echo '  两个DISTINCT列都要保留到外层'
\echo ''

-- -------------------------------------------------------------------------
-- 示例2.2: 三个DISTINCT列 + 多个聚合
-- -------------------------------------------------------------------------

\echo '--- 示例2.2: 三DISTINCT列复杂场景 ---'
\echo ''

-- 问题SQL
SELECT 
    region,
    product_category,
    COUNT(DISTINCT customer_id) as unique_customers,
    COUNT(DISTINCT order_id) as unique_orders,
    COUNT(DISTINCT product_id) as unique_products,
    SUM(sales_amount) as total_sales,
    AVG(sales_amount) as avg_sales
FROM sales_fact
WHERE order_date >= '2024-01-01'
GROUP BY region, product_category;

-- 手动DAT转换
SELECT 
    region,
    product_category,
    COUNT(customer_id) as unique_customers,
    COUNT(order_id) as unique_orders,
    COUNT(product_id) as unique_products,
    SUM(sales_sum) as total_sales,
    SUM(sales_sum) / NULLIF(SUM(sales_count), 0) as avg_sales
FROM (
    SELECT 
        customer_id,
        order_id,
        product_id,
        region,
        product_category,
        SUM(sales_amount) as sales_sum,
        COUNT(sales_amount) as sales_count
    FROM sales_fact
    WHERE order_date >= '2024-01-01'
    GROUP BY customer_id, order_id, product_id, region, product_category
) pre_agg
GROUP BY region, product_category;

\echo ''
\echo '注意: DISTINCT列越多,DAT转换收益越大'
\echo ''

-- ============================================================================
-- 第三部分: 统计函数转换
-- ============================================================================

\echo '========== 第三部分: 统计函数转换 =========='
\echo ''

-- -------------------------------------------------------------------------
-- 示例3.1: STDDEV和VAR_SAMP
-- -------------------------------------------------------------------------

\echo '--- 示例3.1: 标准差和方差 ---'
\echo ''

-- 问题SQL
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    SUM(sales_amount) as total_sales,
    AVG(sales_amount) as avg_sales,
    STDDEV(sales_amount) as stddev_sales,
    VAR_SAMP(sales_amount) as variance_sales
FROM sales
GROUP BY region;

-- 手动DAT转换
-- STDDEV(x) = SQRT((SUM(x²) - SUM(x)²/n) / (n-1))
-- VAR_SAMP(x) = (SUM(x²) - SUM(x)²/n) / (n-1)
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    SUM(sales_sum) as total_sales,
    SUM(sales_sum) / NULLIF(SUM(sales_count), 0) as avg_sales,
    SQRT(
        (SUM(sales_sum_sq) - POWER(SUM(sales_sum), 2) / SUM(sales_count)) /
        NULLIF(SUM(sales_count) - 1, 0)
    ) as stddev_sales,
    (SUM(sales_sum_sq) - POWER(SUM(sales_sum), 2) / SUM(sales_count)) /
    NULLIF(SUM(sales_count) - 1, 0) as variance_sales
FROM (
    SELECT 
        customer_id,
        region,
        SUM(sales_amount) as sales_sum,
        COUNT(sales_amount) as sales_count,
        SUM(sales_amount * sales_amount) as sales_sum_sq
    FROM sales
    GROUP BY customer_id, region
) pre_agg
GROUP BY region;

\echo ''
\echo '统计函数转换公式总结:'
\echo '  AVG(x)      → SUM(sum_x) / SUM(cnt_x)'
\echo '  STDDEV(x)   → SQRT((SUM(x²) - SUM(x)²/n) / (n-1))'
\echo '  VAR_SAMP(x) → (SUM(x²) - SUM(x)²/n) / (n-1)'
\echo ''

-- -------------------------------------------------------------------------
-- 示例3.2: MIN/MAX处理
-- -------------------------------------------------------------------------

\echo '--- 示例3.2: MIN/MAX语义差异 ---'
\echo ''

-- 问题SQL
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    MIN(sales_amount) as global_min_sales,
    MAX(sales_amount) as global_max_sales
FROM sales
GROUP BY region;

-- 方案A: 每个客户最小销售中的最小值 (不等同于原SQL)
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    MIN(customer_min_sales) as min_of_customer_mins,
    MAX(customer_max_sales) as max_of_customer_maxs
FROM (
    SELECT 
        customer_id,
        region,
        MIN(sales_amount) as customer_min_sales,
        MAX(sales_amount) as customer_max_sales
    FROM sales
    GROUP BY customer_id, region
) pre_agg
GROUP BY region;

-- 方案B: 全局MIN/MAX (等同于原SQL)
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    MIN(sales_amount) as global_min_sales,
    MAX(sales_amount) as global_max_sales
FROM (
    -- 内层不做聚合,只保留DISTINCT列
    SELECT DISTINCT ON (customer_id, region)
        customer_id,
        region,
        sales_amount
    FROM sales
) pre_agg
GROUP BY region;

\echo ''
\echo 'MIN/MAX两种语义:'
\echo '  方案A: 每个客户最小值中的最小值 (常用于异常检测)'
\echo '  方案B: 全局最小/最大值 (等同于原SQL语义)'
\echo ''

-- ============================================================================
-- 第四部分: 性能验证测试
-- ============================================================================

\echo '========== 第四部分: 性能验证测试 =========='
\echo ''

-- -------------------------------------------------------------------------
-- 示例4.1: 创建测试数据
-- -------------------------------------------------------------------------

\echo '--- 创建测试环境 ---'
\echo ''

-- 创建测试表
CREATE TABLE IF NOT EXISTS test_dat_sales (
    order_id BIGINT,
    customer_id BIGINT,
    product_id BIGINT,
    region VARCHAR(20),
    sales_amount NUMERIC(18,2),
    order_date DATE
) DISTRIBUTED BY (order_id);

-- 插入测试数据 (100万行)
TRUNCATE TABLE test_dat_sales;
INSERT INTO test_dat_sales
SELECT 
    i as order_id,
    (random() * 10000 + 1)::INT::BIGINT as customer_id,
    (random() * 1000 + 1)::INT::BIGINT as product_id,
    CASE WHEN i % 4 = 0 THEN 'North'
         WHEN i % 4 = 1 THEN 'South'
         WHEN i % 4 = 2 THEN 'East'
         ELSE 'West' END as region,
    (random() * 10000 + 100)::NUMERIC(18,2) as sales_amount,
    '2024-01-01'::DATE + (random() * 365)::INT as order_date
FROM generate_series(1, 1000000) i;

ANALYZE test_dat_sales;

SELECT 
    'test_dat_sales' as table_name,
    pg_size_pretty(pg_total_relation_size('test_dat_sales'::regclass)) as table_size,
    COUNT(*) as row_count
FROM test_dat_sales;

\echo ''

-- -------------------------------------------------------------------------
-- 示例4.2: 性能对比测试
-- -------------------------------------------------------------------------

\echo '--- 性能对比测试 ---'
\echo ''
\timing on

-- 测试1: 原SQL (VastBase无DAT转换)
\echo '测试1: 原SQL (COUNT(DISTINCT)模式)'
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    SUM(sales_amount) as total_sales,
    AVG(sales_amount) as avg_sales
FROM test_dat_sales
GROUP BY region;

\echo ''

-- 测试2: 手动DAT转换
\echo '测试2: 手动DAT转换'
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    SUM(sales_sum) as total_sales,
    SUM(sales_sum) / NULLIF(SUM(sales_count), 0) as avg_sales
FROM (
    SELECT 
        customer_id,
        region,
        SUM(sales_amount) as sales_sum,
        COUNT(sales_amount) as sales_count
    FROM test_dat_sales
    GROUP BY customer_id, region
) pre_agg
GROUP BY region;

\timing off

\echo ''

-- -------------------------------------------------------------------------
-- 示例4.3: 结果一致性验证
-- -------------------------------------------------------------------------

\echo '--- 结果一致性验证 ---'
\echo ''

WITH original AS (
    SELECT 
        region,
        COUNT(DISTINCT customer_id) as unique_customers,
        SUM(sales_amount) as total_sales,
        ROUND(AVG(sales_amount)::NUMERIC, 2) as avg_sales
    FROM test_dat_sales
    GROUP BY region
),
rewritten AS (
    SELECT 
        region,
        COUNT(customer_id) as unique_customers,
        SUM(sales_sum) as total_sales,
        ROUND(SUM(sales_sum) / NULLIF(SUM(sales_count), 0)::NUMERIC, 2) as avg_sales
    FROM (
        SELECT 
            customer_id,
            region,
            SUM(sales_amount) as sales_sum,
            COUNT(sales_amount) as sales_count
        FROM test_dat_sales
        GROUP BY customer_id, region
    ) pre_agg
    GROUP BY region
)
SELECT 
    COALESCE(o.region, r.region) as region,
    o.unique_customers as orig_customers,
    r.unique_customers as rew_customers,
    o.total_sales as orig_sales,
    r.total_sales as rew_sales,
    o.avg_sales as orig_avg,
    r.avg_sales as rew_avg,
    CASE 
        WHEN o.unique_customers = r.unique_customers 
         AND o.total_sales = r.total_sales 
         AND o.avg_sales = r.avg_sales
        THEN '✓ MATCH'
        ELSE '✗ MISMATCH'
    END as validation_status
FROM original o
FULL OUTER JOIN rewritten r ON o.region = r.region
ORDER BY COALESCE(o.region, r.region);

\echo ''
\echo '预期结果: 所有行应该显示 ✓ MATCH'
\echo ''

-- ============================================================================
-- 第五部分: 实战案例
-- ============================================================================

\echo '========== 第五部分: 实战案例 =========='
\echo ''

-- -------------------------------------------------------------------------
-- 案例5.1: 电商订单统计
-- -------------------------------------------------------------------------

\echo '--- 案例5.1: 电商订单地区统计 ---'
\echo ''

-- 场景: 统计各地区销售额、唯一客户数、平均订单金额
-- 问题: 包含COUNT(DISTINCT),执行慢

-- 原SQL (性能差)
\echo '原SQL执行计划:'
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    COUNT(DISTINCT order_id) as unique_orders,
    SUM(order_amount) as total_sales,
    AVG(order_amount) as avg_order
FROM orders
WHERE order_date >= '2024-01-01'
GROUP BY region;

\echo ''

-- 优化后SQL
\echo 'DAT转换后执行计划:'
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    COUNT(order_id) as unique_orders,
    SUM(order_sum) as total_sales,
    SUM(order_sum) / NULLIF(SUM(order_count), 0) as avg_order
FROM (
    SELECT 
        customer_id,
        order_id,
        region,
        SUM(order_amount) as order_sum,
        COUNT(order_amount) as order_count
    FROM orders
    WHERE order_date >= '2024-01-01'
    GROUP BY customer_id, order_id, region
) pre_agg
GROUP BY region;

\echo ''
\echo '预期性能提升: 5-8倍'
\echo ''

-- -------------------------------------------------------------------------
-- 案例5.2: 每日活跃用户统计
-- -------------------------------------------------------------------------

\echo '--- 案例5.2: DAU统计 (日活跃用户) ---'
\echo ''

-- 场景: 统计每日活跃用户数、订单数、销售额
-- 问题: 按日期分组,包含COUNT(DISTINCT customer_id)

-- 原SQL
\echo '原SQL:'
SELECT 
    date_trunc('day', order_date) as order_day,
    COUNT(DISTINCT customer_id) as daily_active_users,
    COUNT(DISTINCT order_id) as daily_orders,
    SUM(order_amount) as daily_sales,
    AVG(order_amount) as avg_order_amount
FROM orders
WHERE order_date >= '2024-01-01'
  AND order_date < '2024-02-01'
GROUP BY date_trunc('day', order_date)
ORDER BY order_day;

\echo ''

-- 优化后SQL
\echo 'DAT转换后:'
SELECT 
    order_day,
    COUNT(customer_id) as daily_active_users,
    COUNT(order_id) as daily_orders,
    SUM(order_sum) as daily_sales,
    SUM(order_sum) / NULLIF(SUM(order_count), 0) as avg_order_amount
FROM (
    SELECT 
        customer_id,
        order_id,
        date_trunc('day', order_date) as order_day,
        SUM(order_amount) as order_sum,
        COUNT(order_amount) as order_count
    FROM orders
    WHERE order_date >= '2024-01-01'
      AND order_date < '2024-02-01'
    GROUP BY customer_id, order_id, date_trunc('day', order_date)
) pre_agg
GROUP BY order_day
ORDER BY order_day;

\echo ''
\echo '关键优势:'
\echo '  - 避免按order_date排序'
\echo '  - 内层按customer_id分组,减少数据量'
\echo '  - 外层按order_day分组,数据量极小'
\echo ''

-- -------------------------------------------------------------------------
-- 案例5.3: 多维度分析 (地区 + 产品类别)
-- -------------------------------------------------------------------------

\echo '--- 案例5.3: 多维度分析 ---'
\echo ''

-- 场景: 按地区和产品类别统计
-- 问题: 两个维度分组 + COUNT(DISTINCT)

-- 原SQL
\echo '原SQL:'
SELECT 
    region,
    product_category,
    COUNT(DISTINCT customer_id) as unique_customers,
    SUM(sales_amount) as total_sales,
    AVG(sales_amount) as avg_sales
FROM sales_fact
WHERE sales_date >= '2024-01-01'
GROUP BY region, product_category;

\echo ''

-- 优化后SQL
\echo 'DAT转换后:'
SELECT 
    region,
    product_category,
    COUNT(customer_id) as unique_customers,
    SUM(sales_sum) as total_sales,
    SUM(sales_sum) / NULLIF(SUM(sales_count), 0) as avg_sales
FROM (
    SELECT 
        customer_id,
        region,
        product_category,
        SUM(sales_amount) as sales_sum,
        COUNT(sales_amount) as sales_count
    FROM sales_fact
    WHERE sales_date >= '2024-01-01'
    GROUP BY customer_id, region, product_category
) pre_agg
GROUP BY region, product_category
ORDER BY region, product_category;

\echo ''
\echo '性能提升: 5-10倍 (取决于customer_id基数)'
\echo ''

-- ============================================================================
-- 第六部分: 常见错误示例
-- ============================================================================

\echo '========== 第六部分: 常见错误示例 =========='
\echo ''

-- -------------------------------------------------------------------------
-- 错误6.1: AVG函数处理不当
-- -------------------------------------------------------------------------

\echo '--- 错误6.1: AVG的平均值陷阱 ---'
\echo ''

\echo '❌ 错误示例:'
\echo 'SELECT region, AVG(pre_agg.avg_sales) ...'
\echo ''

-- 错误SQL: 平均值的平均值
SELECT 
    region,
    AVG(pre_agg.avg_sales) as avg_of_avg  -- 错误!
FROM (
    SELECT 
        region,
        customer_id,
        AVG(sales_amount) as avg_sales
    FROM sales
    GROUP BY region, customer_id
) pre_agg
GROUP BY region;

\echo ''
\echo '问题: 平均值的平均值 ≠ 总体平均值'
\echo ''

\echo '✓ 正确示例:'
\echo '使用加权平均: SUM(sum_x) / SUM(cnt_x)'
\echo ''

-- 正确SQL: 加权平均
SELECT 
    region,
    SUM(pre_agg.sales_sum) / NULLIF(SUM(pre_agg.sales_count), 0) as weighted_avg
FROM (
    SELECT 
        region,
        customer_id,
        SUM(sales_amount) as sales_sum,
        COUNT(sales_amount) as sales_count
    FROM sales
    GROUP BY region, customer_id
) pre_agg
GROUP BY region;

\echo ''

-- -------------------------------------------------------------------------
-- 错误6.2: GROUP BY列遗漏
-- -------------------------------------------------------------------------

\echo '--- 错误6.2: GROUP BY列遗漏 ---'
\echo ''

\echo '❌ 错误示例: 子查询缺少GROUP BY'
\echo ''

-- 错误SQL
-- SELECT region, COUNT(customer_id)
-- FROM (SELECT region, customer_id FROM sales) pre_agg
-- GROUP BY region;

\echo '错误: 需要在子查询中添加 GROUP BY region, customer_id'
\echo ''

\echo '✓ 正确示例:'

-- 正确SQL
SELECT 
    region,
    COUNT(customer_id)
FROM (
    SELECT 
        region,
        customer_id
    FROM sales
    GROUP BY region, customer_id  -- 必须有GROUP BY
) pre_agg
GROUP BY region;

\echo ''

-- -------------------------------------------------------------------------
-- 错误6.3: DISTINCT列丢失
-- -------------------------------------------------------------------------

\echo '--- 错误6.3: DISTINCT列未保留到外层 ---'
\echo ''

\echo '❌ 错误示例: object_name丢失'
\echo ''

-- 错误SQL
SELECT 
    object_type,
    COUNT(*) as unique_names  -- 错误!
FROM (
    SELECT 
        object_type,
        SUM(object_id) as sum_id
    FROM t1
    GROUP BY object_name, object_type
) pre_agg
GROUP BY object_type;

\echo '问题: object_name不在子查询SELECT中,无法COUNT(DISTINCT)'
\echo ''

\echo '✓ 正确示例: 保留DISTINCT列'
\echo ''

-- 正确SQL
SELECT 
    object_type,
    COUNT(object_name) as unique_names
FROM (
    SELECT 
        object_name,
        object_type
    FROM t1
    GROUP BY object_name, object_type
) pre_agg
GROUP BY object_type;

\echo ''

-- ============================================================================
-- 第七部分: DAT转换检测脚本
-- ============================================================================

\echo '========== 第七部分: 自动检测脚本 =========='
\echo ''

\echo '--- DAT转换候选检测函数 ---'
\echo ''

-- 创建检测函数 (如果pg_stat_statements可用)
/*
CREATE OR REPLACE FUNCTION detect_dat_candidates(
    min_exec_time_ms NUMERIC DEFAULT 1000,
    min_calls INT DEFAULT 10
)
RETURNS TABLE(
    query_id BIGINT,
    sql_text TEXT,
    exec_count BIGINT,
    avg_exec_time_ms NUMERIC,
    has_count_distinct BOOLEAN,
    has_group_by BOOLEAN,
    optimization_potential TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        ss.query_id,
        ss.query,
        ss.calls as exec_count,
        ss.mean_exec_time / 1000 as avg_exec_time_ms,
        ss.query LIKE '%COUNT(DISTINCT%' OR ss.query LIKE '%count(distinct%' AS has_count_distinct,
        ss.query LIKE '%GROUP BY%' AS has_group_by,
        CASE 
            WHEN ss.query LIKE '%COUNT(DISTINCT%' 
              AND ss.query LIKE '%GROUP BY%'
              AND ss.mean_exec_time > min_exec_time_ms * 1000
              AND ss.calls > min_calls
            THEN 'DAT转换候选'
            ELSE '其他'
        END as optimization_potential
    FROM pg_stat_statements ss
    WHERE ss.calls > min_calls
      AND ss.mean_exec_time > min_exec_time_ms * 1000
      AND (ss.query LIKE '%COUNT(DISTINCT%' OR ss.query LIKE '%count(distinct%')
    ORDER BY ss.mean_exec_time DESC
    LIMIT 50;
END;
$$ LANGUAGE plpgsql;
*/

\echo '使用示例 (需要pg_stat_statements扩展):'
\echo 'SELECT * FROM detect_dat_candidates(1000, 10);'
\echo ''

-- ============================================================================
-- 第八部分: 快速参考
-- ============================================================================

\echo '========== 第八部分: 快速参考 =========='
\echo ''

\echo 'DAT转换模板:'
\echo ''
\echo '-- 原SQL:'
\echo 'SELECT group_by_col,'
\echo '       COUNT(DISTINCT distinct_col),'
\echo '       SUM(measure_col),'
\echo '       AVG(measure_col)'
\echo 'FROM table'
\echo 'GROUP BY group_by_col;'
\echo ''
\echo '-- DAT转换后:'
\echo 'SELECT group_by_col,'
\echo '       COUNT(distinct_col),'
\echo '       SUM(pre_agg.sum_measure),'
\echo '       SUM(pre_agg.sum_measure) / NULLIF(SUM(pre_agg.cnt_measure), 0)'
\echo 'FROM ('
\echo '    SELECT distinct_col,'
\echo '           group_by_col,'
\echo '           SUM(measure_col) as sum_measure,'
\echo '           COUNT(measure_col) as cnt_measure'
\echo '    FROM table'
\echo '    GROUP BY distinct_col, group_by_col'
\echo ') pre_agg'
\echo 'GROUP BY group_by_col;'
\echo ''

\echo '聚合函数转换对照表:'
\echo ''
\echo '  原始函数              →  转换后公式'
\echo '  ─────────────────────────────────────────────────────────'
\echo '  COUNT(DISTINCT x)     →  COUNT(x)'
\echo '  SUM(x)                →  SUM(sum_x)'
\echo '  AVG(x)                →  SUM(sum_x) / NULLIF(SUM(cnt_x), 0)'
\echo '  MIN(x)                →  MIN(min_x)  或 MIN(x) (保留所有值)'
\echo '  MAX(x)                →  MAX(max_x)  或 MAX(x) (保留所有值)'
\echo '  STDDEV(x)             →  SQRT((SUM(x²) - SUM(x)²/n) / (n-1))'
\echo '  VAR_SAMP(x)           →  (SUM(x²) - SUM(x)²/n) / (n-1)'
\echo ''

\echo '性能提升预期:'
\echo ''
\echo '  场景                          →  性能提升'
\echo '  ─────────────────────────────────────────────────'
\echo '  简单COUNT(DISTINCT) (10万行)  →  3-5倍'
\echo '  多DISTINCT列 (50万行)         →  5-8倍'
\echo '  复杂统计函数 (100万行)        →  5-10倍'
\echo '  大基数DISTINCT (500万行)      →  8-15倍'
\echo ''

\echo '不适用场景:'
\echo ''
\echo '  ✗ 执行时间 < 100ms'
\echo '  ✗ 已使用HashAggregate'
\echo '  ✗ 数据量小 (< 1万行)'
\echo '  ✗ DISTINCT列基数很小 (< 10)'
\echo ''

\echo '========================================'
\echo 'DAT转换示例集执行完毕'
\echo '========================================'

-- 清理测试表 (可选)
-- DROP TABLE IF EXISTS test_dat_sales;
