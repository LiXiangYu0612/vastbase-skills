-- ============================================================================
-- VastBase MIN/MAX Index Optimization实战测试 - 20个Case场景
-- ============================================================================
-- 功能: 测试VastBase对PostgreSQL 13 MIN/MAX Index Optimization的支持度
-- 借鉴: PostgreSQL 13 MIN/MAX优化技术
-- 实战: 20个不同场景的SQL优化案例
-- ============================================================================

-- ============================================================================
-- 第一部分: 测试环境准备 (5个case)
-- ============================================================================

DROP SCHEMA IF EXISTS minmax_test CASCADE;
CREATE SCHEMA minmax_test;
SET search_path TO minmax_test;

-- ========== Case 1: 创建订单表 ==========

CREATE TABLE orders (
    order_id BIGINT PRIMARY KEY,
    created_at TIMESTAMP NOT NULL,
    updated_at TIMESTAMP NOT NULL,
    amount NUMERIC(18,2),
    status VARCHAR(20),
    customer_id BIGINT,
    priority INT
);

-- 插入100万行数据
INSERT INTO orders
SELECT
    i,
    TIMESTAMP '2024-01-01' + (i % 365) * INTERVAL '1 day',
    TIMESTAMP '2024-01-01' + (i % 365) * INTERVAL '1 day' + (i % 86400) * INTERVAL '1 second',
    random() * 10000,
    CASE WHEN i % 5 = 0 THEN 'COMPLETED'
         WHEN i % 3 = 0 THEN 'PROCESSING'
         WHEN i % 2 = 0 THEN 'PENDING'
         ELSE 'CANCELLED'
    END,
    (i % 10000) + 1,
    (i % 5) + 1
FROM generate_series(1, 1000000) i;

ANALYZE orders;

-- ========== Case 2: 创建索引 ==========

CREATE INDEX idx_orders_created_at ON orders(created_at);
CREATE INDEX idx_orders_amount ON orders(amount);
CREATE INDEX idx_orders_status_created ON orders(status, created_at);
CREATE INDEX idx_orders_customer_created ON orders(customer_id, created_at);
CREATE INDEX idx_orders_priority_amount ON orders(priority, amount);

-- ========== Case 3: 检查数据分布 ==========

SELECT
    'Table statistics' AS info,
    COUNT(*) AS total_rows,
    MIN(created_at) AS min_date,
    MAX(created_at) AS max_date,
    MIN(amount) AS min_amount,
    MAX(amount) AS max_amount
FROM orders;

-- ========== Case 4: 检查索引统计信息 ==========

SELECT
    schemaname,
    tablename,
    attname AS column_name,
    n_distinct,
    correlation
FROM pg_stats
WHERE tablename = 'orders'
  AND attname IN ('created_at', 'amount', 'status', 'customer_id', 'priority')
ORDER BY attname;

-- ========== Case 5: 创建辅助表 ==========

CREATE TABLE customers (
    customer_id BIGINT PRIMARY KEY,
    name VARCHAR(100),
    region VARCHAR(20)
);

INSERT INTO customers
SELECT i, 'Customer ' || i, CASE WHEN i % 3 = 0 THEN 'EAST' ELSE 'WEST' END
FROM generate_series(1, 10000) i;

ANALYZE customers;

-- ============================================================================
-- 第二部分: 基础MIN/MAX查询 (5个case)
-- ============================================================================

\timing on

-- ========== Case 6: 简单MIN查询 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MIN(created_at) AS earliest_order
FROM orders;

-- PostgreSQL 13+预期: Index Only Scan / Incremental Sort
-- 执行时间: < 1ms
-- Buffers: 2-3个索引页

-- ========== Case 7: 简单MAX查询 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MAX(created_at) AS latest_order
FROM orders;

-- 应该同样优化

-- ========== Case 8: MIN和MAX组合 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    MIN(created_at) AS min_date,
    MAX(created_at) AS max_date
FROM orders;

-- PG 13+: 可能一次索引扫描获取MIN和MAX

-- ========== Case 9: MIN/MAX + 其他聚合 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    MIN(created_at) AS min_date,
    MAX(created_at) AS max_date,
    COUNT(*) AS total_orders,
    AVG(amount) AS avg_amount
FROM orders;

-- MIN/MAX可能优化，COUNT和AVG仍需全表扫描

-- ========== Case 10: 多个MIN/MAX查询 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    MIN(created_at) AS min_created,
    MAX(created_at) AS max_created,
    MIN(amount) AS min_amount,
    MAX(amount) AS max_amount
FROM orders;

-- PG 13+: 可以分别使用两个索引
\timing off

-- ============================================================================
-- 第三部分: MIN/MAX + WHERE条件 (5个case)
-- ============================================================================

\timing on

-- ========== Case 11: MIN + 单个WHERE条件 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MIN(created_at)
FROM orders
WHERE status = 'COMPLETED';

-- 如果有索引 (status, created_at)
-- → 可以优化

-- ========== Case 12: MIN + 多个WHERE条件 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MIN(created_at)
FROM orders
WHERE status = 'COMPLETED'
  AND customer_id BETWEEN 1000 AND 2000;

-- 需要组合索引 (status, customer_id, created_at)

-- ========== Case 13: MAX + WHERE ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MAX(amount)
FROM orders
WHERE status = 'COMPLETED';

-- 使用索引 (status, amount) 如果存在

-- ========== Case 14: MIN/MAX + BETWEEN ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MIN(created_at), MAX(created_at)
FROM orders
WHERE created_at BETWEEN '2024-01-01' AND '2024-06-30';

-- WHERE条件就是索引列，可能优化

-- ========== Case 15: MIN/MAX + IN条件 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MIN(created_at)
FROM orders
WHERE status IN ('COMPLETED', 'PROCESSING');
\timing off

-- ============================================================================
-- 第四部分: MIN/MAX + GROUP BY (5个case)
-- ============================================================================

\timing on

-- ========== Case 16: 分组MIN/MAX ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    status,
    MIN(created_at) AS earliest,
    MAX(created_at) AS latest,
    COUNT(*) AS order_count
FROM orders
GROUP BY status
ORDER BY earliest;

-- 如果有索引 (status, created_at)
-- → PG 13+ 可能优化

-- ========== Case 17: 多列分组MIN/MAX ==========

CREATE TABLE orders_category (
    order_id BIGINT PRIMARY KEY,
    category VARCHAR(20),
    subcategory VARCHAR(20),
    created_at TIMESTAMP,
    amount NUMERIC(18,2)
);

INSERT INTO orders_category
SELECT
    i,
    'CAT' || ((i % 5) + 1),
    'SUB' || ((i % 10) + 1),
    TIMESTAMP '2024-01-01' + (i % 365) * INTERVAL '1 day',
    random() * 10000
FROM generate_series(1, 100000) i;

CREATE INDEX idx_category_sub_created ON orders_category(category, subcategory, created_at);
ANALYZE orders_category;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    category,
    subcategory,
    MIN(created_at) AS first_order,
    MAX(created_at) AS last_order
FROM orders_category
GROUP BY category, subcategory
ORDER BY category, subcategory;

-- ========== Case 18: 分组MIN/MAX + HAVING ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    status,
    MIN(created_at) AS earliest
FROM orders
GROUP BY status
HAVING MIN(created_at) >= '2024-01-01'
ORDER BY earliest;

-- ========== Case 19: 多个分组MIN/MAX ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    status,
    MIN(created_at) AS min_created,
    MAX(created_at) AS max_created,
    MIN(amount) AS min_amount,
    MAX(amount) AS max_amount
FROM orders
GROUP BY status
ORDER BY min_created;

-- ========== Case 20: 分组MIN/MAX + JOIN ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    o.status,
    MIN(o.created_at) AS earliest,
    c.region
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
GROUP BY o.status, c.region
ORDER BY earliest;
\timing off

-- ============================================================================
-- 第五部分: VastBase替代方案测试 (5个case)
-- ============================================================================

\timing on

-- ========== Case 21: LIMIT 1改写 (MIN) ==========

-- 原始查询
EXPLAIN (ANALYZE, BUFFERS)
SELECT MIN(created_at) FROM orders;

-- 改写方案
EXPLAIN (ANALYZE, BUFFERS)
SELECT created_at
FROM orders
ORDER BY created_at ASC
LIMIT 1;

-- 对比性能

-- ========== Case 22: LIMIT 1改写 (MAX) ==========

-- 原始查询
EXPLAIN (ANALYZE, BUFFERS)
SELECT MAX(created_at) FROM orders;

-- 改写方案
EXPLAIN (ANALYZE, BUFFERS)
SELECT created_at
FROM orders
ORDER BY created_at DESC
LIMIT 1;

-- ========== Case 23: LIMIT 1 + DISTINCT ON (分组MIN) ==========

-- 原始查询
EXPLAIN (ANALYZE, BUFFERS)
SELECT
    status,
    MIN(created_at) AS earliest
FROM orders
GROUP BY status;

-- 改写方案: 使用DISTINCT ON
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT ON (status)
    status,
    created_at AS earliest
FROM orders
ORDER BY status, created_at ASC;

-- ========== Case 24: LATERAL改写 (关联MIN/MAX) ==========

-- 原始查询: 关联子查询
EXPLAIN (ANALYZE, BUFFERS)
SELECT
    c.customer_id,
    c.name,
    (
        SELECT MIN(o.created_at)
        FROM orders o
        WHERE o.customer_id = c.customer_id
    ) AS first_order_date
FROM customers c
WHERE c.customer_id <= 100
ORDER BY first_order_date;

-- 改写: 使用LATERAL
EXPLAIN (ANALYZE, BUFFERS)
SELECT
    c.customer_id,
    c.name,
    o.first_order_date
FROM customers c
CROSS JOIN LATERAL (
    SELECT MIN(o2.created_at) AS first_order_date
    FROM orders o2
    WHERE o2.customer_id = c.customer_id
) o
WHERE c.customer_id <= 100
ORDER BY o.first_order_date;

-- ========== Case 25: 性能对比测试 ==========

-- 测试1: Seq Scan (禁用索引)
SET enable_indexscan = OFF;
SET enable_bitmapscan = OFF;

\timing on
EXPLAIN (ANALYZE, BUFFERS)
SELECT MIN(created_at) FROM orders;
\timing off

-- 测试2: Index Scan (启用索引)
SET enable_indexscan = ON;
SET enable_bitmapscan = ON;

\timing on
EXPLAIN (ANALYZE, BUFFERS)
SELECT MIN(created_at) FROM orders;
\timing off

-- 测试3: LIMIT 1改写
\timing on
EXPLAIN (ANALYZE, BUFFERS)
SELECT created_at FROM orders
ORDER BY created_at ASC
LIMIT 1;
\timing off

-- 测试4: 对比实际执行时间
-- 记录各场景的执行时间并对比

-- ============================================================================
-- 第六部分: 复杂场景MIN/MAX
-- ============================================================================

-- ========== Case 26: MIN/MAX + 窗口函数 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    order_id,
    created_at,
    amount,
    MIN(created_at) OVER (PARTITION BY status) AS group_min,
    MAX(created_at) OVER (PARTITION BY status) AS group_max
FROM orders
WHERE status IN ('COMPLETED', 'PROCESSING')
ORDER BY created_at
LIMIT 100;

-- ========== Case 27: MIN/MAX + CASE表达式 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    status,
    CASE
        WHEN MIN(created_at) >= '2024-06-01' THEN 'Recent'
        ELSE 'Old'
    END AS age_category
FROM orders
GROUP BY status;

-- ========== Case 28: MIN/MAX + 子查询 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    c.customer_id,
    c.name,
    (
        SELECT MIN(o.created_at)
        FROM orders o
        WHERE o.customer_id = c.customer_id
          AND o.status = 'COMPLETED'
    ) AS first_completed_order
FROM customers c
WHERE c.customer_id <= 50;

-- ========== Case 29: 多个MIN/MAX子查询 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    c.customer_id,
    (SELECT MIN(o.created_at) FROM orders o WHERE o.customer_id = c.customer_id) AS min_date,
    (SELECT MAX(o.created_at) FROM orders o WHERE o.customer_id = c.customer_id) AS max_date,
    (SELECT MIN(o.amount) FROM orders o WHERE o.customer_id = c.customer_id) AS min_amount,
    (SELECT MAX(o.amount) FROM orders o WHERE o.customer_id = c.customer_id) AS max_amount
FROM customers c
WHERE c.customer_id <= 20;

-- ========== Case 30: MIN/MAX + UNION ALL ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT MIN(created_at), MAX(created_at)
FROM (
    SELECT created_at FROM orders WHERE status = 'COMPLETED'
    UNION ALL
    SELECT created_at FROM orders WHERE status = 'PROCESSING'
    UNION ALL
    SELECT created_at FROM orders WHERE status = 'PENDING'
) combined;

-- ============================================================================
-- 第七部分: 实用查询示例
-- ============================================================================

-- ========== 查询1: 识别可能受益于MIN/MAX优化的查询 ==========

-- 从pg_stat_statements查找 (如果扩展已安装)
-- SELECT query, calls, mean_exec_time
-- FROM pg_stat_statements
-- WHERE query LIKE '%MIN(%' OR query LIKE '%MAX(%'
-- ORDER BY mean_exec_time DESC
-- LIMIT 20;

-- ========== 查询2: 检查MIN/MAX列的索引 ==========

SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE schemaname = 'minmax_test'
  AND (indexdef LIKE '%created_at%'
       OR indexdef LIKE '%amount%')
ORDER BY tablename, indexname;

-- ========== 查询3: 分析MIN/MAX查询性能 ==========

-- 创建性能测试函数
CREATE OR REPLACE FUNCTION test_minmax_performance()
RETURNS TABLE(
    test_name TEXT,
    execution_time NUMERIC
) AS $$
BEGIN
    -- 测试1: MIN
    DECLARE
        start_time TIMESTAMP;
        end_time TIMESTAMP;
        min_result TIMESTAMP;
    BEGIN
        start_time := clock_timestamp();
        SELECT MIN(created_at) INTO min_result FROM orders;
        end_time := clock_timestamp();
        RETURN QUERY SELECT 'MIN(created_at)'::TEXT,
                            EXTRACT(EPOCH FROM (end_time - start_time)) * 1000;
    END;

    -- 测试2: MAX
    BEGIN
        start_time := clock_timestamp();
        SELECT MAX(created_at) INTO min_result FROM orders;
        end_time := clock_timestamp();
        RETURN QUERY SELECT 'MAX(created_at)'::TEXT,
                            EXTRACT(EPOCH FROM (end_time - start_time)) * 1000;
    END;

    -- 测试3: LIMIT 1 (MIN替代)
    BEGIN
        start_time := clock_timestamp();
        SELECT created_at INTO min_result
        FROM orders ORDER BY created_at ASC LIMIT 1;
        end_time := clock_timestamp();
        RETURN QUERY SELECT 'LIMIT 1 ASC'::TEXT,
                            EXTRACT(EPOCH FROM (end_time - start_time)) * 1000;
    END;

    -- 测试4: LIMIT 1 (MAX替代)
    BEGIN
        start_time := clock_timestamp();
        SELECT created_at INTO min_result
        FROM orders ORDER BY created_at DESC LIMIT 1;
        end_time := clock_timestamp();
        RETURN QUERY SELECT 'LIMIT 1 DESC'::TEXT,
                            EXTRACT(EPOCH FROM (end_time - start_time)) * 1000;
    END;
END;
$$ LANGUAGE plpgsql;

-- 执行性能测试
SELECT * FROM test_minmax_performance();

-- ============================================================================
-- 第八部分: MIN/MAX优化检查清单
-- ============================================================================

-- ========== 检查1: 表是否有MIN/MAX查询 ==========

-- 查找包含MIN/MAX的查询
-- (需要pg_stat_statements扩展)

-- ========== 检查2: MIN/MAX列是否有索引 ==========

SELECT
    'MIN/MAX columns index check' AS check_type,
    tablename,
    attname AS column_name,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM pg_indexes
            WHERE tablename = pg_stats.tablename
              AND indexdef LIKE '%' || pg_stats.attname || '%'
        ) THEN 'Has index'
        ELSE 'NO index - consider creating one'
    END AS index_status
FROM pg_stats
WHERE schemaname = 'minmax_test'
  AND attname IN ('created_at', 'amount', 'updated_at')
ORDER BY tablename, attname;

-- ========== 检查3: 识别优化机会 ==========

SELECT
    'Optimization opportunity' AS info,
    'MIN/MAX on column without index' AS issue,
    'Create index on column' AS recommendation
WHERE NOT EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'minmax_test'
      AND tablename = 'orders'
      AND indexdef LIKE '%created_at%'
)
UNION ALL
SELECT
    'Optimization opportunity',
    'Consider rewriting MIN/MAX as LIMIT 1',
    'Improves compatibility across databases'
WHERE EXISTS (
    SELECT 1 FROM pg_indexes
    WHERE schemaname = 'minmax_test'
      AND tablename = 'orders'
      AND indexdef LIKE '%created_at%'
);

-- ============================================================================
-- 第九部分: 清理
-- ============================================================================

-- 保留测试表用于验证
-- DROP SCHEMA IF EXISTS minmax_test CASCADE;

-- ============================================================================
-- 总结
-- ============================================================================
--
-- 本文件包含30个MIN/MAX Index Optimization测试场景:
--
-- 第一部分: 环境准备 (5个case)
--   - 创建测试表和索引
--   - 数据分布检查
--
-- 第二部分: 基础MIN/MAX (5个case)
--   - MIN查询
--   - MAX查询
--   - 组合查询
--
-- 第三部分: MIN/MAX + WHERE (5个case)
--   - 单条件
--   - 多条件
--   - BETWEEN/IN
--
-- 第四部分: MIN/MAX + GROUP BY (5个case)
--   - 分组极值
--   - HAVING
--   - JOIN
--
-- 第五部分: VastBase替代方案 (5个case)
--   - LIMIT 1改写
--   - DISTINCT ON
--   - LATERAL
--
-- 第六部分: 复杂场景 (5个case)
--   - 窗口函数
--   - 子查询
--   - UNION ALL
--
-- 第七部分: 实用工具
--   - 性能测试函数
--   - 检查清单
--
-- 关键学习点:
-- 1. MIN/MAX优化可提升100-1000倍性能
-- 2. 必须有对应索引才能优化
-- 3. WHERE条件必须在索引中
-- 4. LIMIT 1 + ORDER BY是通用改写方案
-- 5. 分组MIN/MAX也能优化 (每个分组独立)
--
-- ============================================================================
