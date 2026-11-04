-- ============================================================================
-- VastBase BTree Index Skip Scan实战测试 - 25个Case场景
-- ============================================================================
-- 功能: 测试VastBase对PostgreSQL 13 Skip Scan的支持度
-- 借鉴: PostgreSQL 13 Index Skip Scan优化技术
-- 实战: 25个不同场景的SQL优化案例
-- ============================================================================

-- ============================================================================
-- 第一部分: 测试环境准备 (5个case)
-- ============================================================================

DROP SCHEMA IF EXISTS skip_scan_test CASCADE;
CREATE SCHEMA skip_scan_test;
SET search_path TO skip_scan_test;

-- ========== Case 1: 创建订单表 (经典Skip Scan场景) ==========

CREATE TABLE orders (
    order_id BIGINT PRIMARY KEY,
    status VARCHAR(20) NOT NULL,           -- 低基数前导列: 5个值
    created_at TIMESTAMP NOT NULL,
    customer_id BIGINT,
    amount NUMERIC(18,2),
    priority INT
);

-- 插入100万行数据
INSERT INTO orders
SELECT
    i,
    CASE WHEN i % 10 = 0 THEN 'CANCELLED'
         WHEN i % 5 = 0 THEN 'PENDING'
         WHEN i % 3 = 0 THEN 'PROCESSING'
         WHEN i % 2 = 0 THEN 'COMPLETED'
         ELSE 'ACTIVE'
    END,
    TIMESTAMP '2024-01-01' + (random() * 365)::int * INTERVAL '1 day',
    (i % 10000) + 1,
    random() * 10000,
    (i % 5) + 1
FROM generate_series(1, 1000000) i;

ANALYZE orders;

-- ========== Case 2: 检查数据分布 ==========

SELECT
    status,
    COUNT(*) AS row_count,
    COUNT(*) * 100.0 / (SELECT COUNT(*) FROM orders) AS percentage,
    MIN(created_at) AS min_date,
    MAX(created_at) AS max_date
FROM orders
GROUP BY status
ORDER BY row_count DESC;

-- 预期结果: 5个status值，分布较均匀

-- ========== Case 3: 创建复合索引 ==========

CREATE INDEX idx_orders_status_created ON orders(status, created_at);

-- ========== Case 4: 创建反向索引 (对比测试) ==========

CREATE INDEX idx_orders_created_status ON orders(created_at, status);

-- ========== Case 5: 收集统计信息 ==========

ANALYZE orders;

-- 检查统计信息
SELECT
    schemaname,
    tablename,
    attname AS column_name,
    n_distinct,
    most_common_vals,
    most_common_freqs
FROM pg_stats
WHERE tablename = 'orders'
  AND attname IN ('status', 'created_at')
ORDER BY attname;

-- ============================================================================
-- 第二部分: 基础Skip Scan场景 (5个case)
-- ============================================================================

\timing on

-- ========== Case 6: 基础Skip Scan查询 ==========

-- 问题查询: 缺少前导列status
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT *
FROM orders
WHERE created_at >= '2024-06-01'
ORDER BY created_at
LIMIT 100;

-- PostgreSQL 13+预期: Skip Scan
-- VastBase: 待验证

-- ========== Case 7: 带排序的Skip Scan ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT order_id, status, created_at, amount
FROM orders
WHERE created_at BETWEEN '2024-01-01' AND '2024-03-31'
ORDER BY created_at DESC, order_id
LIMIT 50;

-- ========== Case 8: Skip Scan + 聚合 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    status,
    COUNT(*) AS order_count,
    SUM(amount) AS total_amount
FROM orders
WHERE created_at >= '2024-06-01'
GROUP BY status
ORDER BY order_count DESC;

-- ========== Case 9: Skip Scan + JOIN ==========

CREATE TABLE customers (
    customer_id BIGINT PRIMARY KEY,
    name VARCHAR(100),
    region VARCHAR(20)
);

INSERT INTO customers
SELECT i, 'Customer ' || i, CASE WHEN i % 3 = 0 THEN 'EAST' ELSE 'WEST' END
FROM generate_series(1, 10000) i;

ANALYZE customers;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT
    o.order_id,
    o.status,
    o.created_at,
    c.name,
    c.region
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
WHERE o.created_at >= '2024-06-01'
ORDER BY o.created_at
LIMIT 100;

-- ========== Case 10: Skip Scan + DISTINCT ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT DISTINCT status, DATE(created_at)
FROM orders
WHERE created_at >= '2024-06-01'
ORDER BY DATE(created_at);

\timing off

-- ============================================================================
-- 第三部分: 不同基数对比 (5个case)
-- ============================================================================

-- ========== Case 11: 极低基数 (2个值) ==========

CREATE TABLE orders_binary (
    order_id BIGINT PRIMARY KEY,
    is_active BOOLEAN NOT NULL,         -- 只有2个值
    created_at TIMESTAMP NOT NULL,
    amount NUMERIC(18,2)
);

INSERT INTO orders_binary
SELECT
    i,
    i % 2 = 0,  -- true/false
    TIMESTAMP '2024-01-01' + (random() * 365)::int * INTERVAL '1 day',
    random() * 10000
FROM generate_series(1, 1000000) i;

CREATE INDEX idx_binary_status_created ON orders_binary(is_active, created_at);
ANALYZE orders_binary;

EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders_binary
WHERE created_at >= '2024-06-01'
ORDER BY created_at
LIMIT 100;

-- ========== Case 12: 低基数 (5个值) ==========

-- 已经有orders表，status有5个值
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*), SUM(amount)
FROM orders
WHERE created_at >= '2024-06-01';

-- ========== Case 13: 中等基数 (20个值) ==========

CREATE TABLE orders_20status (
    order_id BIGINT PRIMARY KEY,
    status_code INT NOT NULL,           -- 1-20
    created_at TIMESTAMP NOT NULL,
    amount NUMERIC(18,2)
);

INSERT INTO orders_20status
SELECT
    i,
    (i % 20) + 1,
    TIMESTAMP '2024-01-01' + (random() * 365)::int * INTERVAL '1 day',
    random() * 10000
FROM generate_series(1, 1000000) i;

CREATE INDEX idx_20status_created ON orders_20status(status_code, created_at);
ANALYZE orders_20status;

EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders_20status
WHERE created_at >= '2024-06-01'
ORDER BY created_at
LIMIT 100;

-- ========== Case 14: 高基数 (100个值) ==========

CREATE TABLE orders_100status (
    order_id BIGINT PRIMARY KEY,
    region_code INT NOT NULL,           -- 1-100
    created_at TIMESTAMP NOT NULL,
    amount NUMERIC(18,2)
);

INSERT INTO orders_100status
SELECT
    i,
    (i % 100) + 1,
    TIMESTAMP '2024-01-01' + (random() * 365)::int * INTERVAL '1 day',
    random() * 10000
FROM generate_series(1, 1000000) i;

CREATE INDEX idx_100status_created ON orders_100status(region_code, created_at);
ANALYZE orders_100status;

EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders_100status
WHERE created_at >= '2024-06-01'
ORDER BY created_at
LIMIT 100;

-- ========== Case 15: 极高基数 (1000个值) ==========

CREATE TABLE orders_1000status (
    order_id BIGINT PRIMARY KEY,
    category_id INT NOT NULL,           -- 1-1000
    created_at TIMESTAMP NOT NULL,
    amount NUMERIC(18,2)
);

INSERT INTO orders_1000status
SELECT
    i,
    (i % 1000) + 1,
    TIMESTAMP '2024-01-01' + (random() * 365)::int * INTERVAL '1 day',
    random() * 10000
FROM generate_series(1, 1000000) i;

CREATE INDEX idx_1000status_created ON orders_1000status(category_id, created_at);
ANALYZE orders_1000status;

EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders_1000status
WHERE created_at >= '2024-06-01'
ORDER BY created_at
LIMIT 100;

-- ============================================================================
-- 第四部分: 反向索引优化方案 (5个case)
-- ============================================================================

-- ========== Case 16: 反向索引基础测试 ==========

-- 使用反向索引 (created_at, status)
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT *
FROM orders
WHERE created_at >= '2024-06-01'
ORDER BY created_at
LIMIT 100;

-- 对比: 使用idx_orders_created_status

-- ========== Case 17: 反向索引 + 前导列条件 ==========

EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE created_at >= '2024-06-01'
  AND status = 'ACTIVE'
ORDER BY created_at;

-- 反向索引仍然可用 (前缀匹配)

-- ========== Case 18: 反向索引 + 范围查询 ==========

EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE created_at BETWEEN '2024-01-01' AND '2024-06-30'
  AND status IN ('ACTIVE', 'COMPLETED')
ORDER BY created_at;

-- ========== Case 19: 反向索引 + 排序 ==========

EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM orders
WHERE created_at >= '2024-06-01'
ORDER BY created_at DESC, status
LIMIT 50;

-- ========== Case 20: 反向索引 + 覆盖查询 ==========

-- 如果VastBase支持INCLUDE (PG 11+)
-- CREATE INDEX idx_orders_created_covering
-- ON orders(created_at) INCLUDE (status, order_id, amount);

-- 或者使用传统复合索引
CREATE INDEX idx_orders_created_status_covering
ON orders(created_at, status, order_id, amount);

ANALYZE orders;

EXPLAIN (ANALYZE, BUFFERS)
SELECT order_id, status, created_at, amount
FROM orders
WHERE created_at >= '2024-06-01'
ORDER BY created_at
LIMIT 100;

-- ============================================================================
-- 第五部分: UNION ALL改写方案 (5个case)
-- ============================================================================

-- ========== Case 21: 手动UNION ALL改写 ==========

-- 原始查询
-- SELECT * FROM orders WHERE created_at >= '2024-06-01';

-- 改写: 手动展开每个status
\timing on

EXPLAIN (ANALYZE, BUFFERS)
WITH active_orders AS (
    SELECT * FROM orders
    WHERE status = 'ACTIVE' AND created_at >= '2024-06-01'
),
completed_orders AS (
    SELECT * FROM orders
    WHERE status = 'COMPLETED' AND created_at >= '2024-06-01'
),
pending_orders AS (
    SELECT * FROM orders
    WHERE status = 'PENDING' AND created_at >= '2024-06-01'
),
processing_orders AS (
    SELECT * FROM orders
    WHERE status = 'PROCESSING' AND created_at >= '2024-06-01'
),
cancelled_orders AS (
    SELECT * FROM orders
    WHERE status = 'CANCELLED' AND created_at >= '2024-06-01'
)
SELECT * FROM active_orders
UNION ALL
SELECT * FROM completed_orders
UNION ALL
SELECT * FROM pending_orders
UNION ALL
SELECT * FROM processing_orders
UNION ALL
SELECT * FROM cancelled_orders
ORDER BY created_at
LIMIT 100;

\timing off

-- ========== Case 22: UNION ALL + 聚合 ==========

EXPLAIN (ANALYZE, BUFFERS)
SELECT status, COUNT(*), SUM(amount)
FROM (
    SELECT * FROM orders WHERE status = 'ACTIVE' AND created_at >= '2024-06-01'
    UNION ALL
    SELECT * FROM orders WHERE status = 'COMPLETED' AND created_at >= '2024-06-01'
    UNION ALL
    SELECT * FROM orders WHERE status = 'PENDING' AND created_at >= '2024-06-01'
    UNION ALL
    SELECT * FROM orders WHERE status = 'PROCESSING' AND created_at >= '2024-06-01'
    UNION ALL
    SELECT * FROM orders WHERE status = 'CANCELLED' AND created_at >= '2024-06-01'
) all_orders
GROUP BY status;

-- ========== Case 23: 动态生成UNION ALL (PL/pgSQL) ==========

-- 自动获取所有status值，生成UNION ALL
DO $$
DECLARE
    v_status TEXT;
    sql TEXT := '';
BEGIN
    FOR v_status IN
        SELECT DISTINCT status FROM orders ORDER BY status
    LOOP
        IF sql != '' THEN
            sql := sql || E'\nUNION ALL\n';
        END IF;

        sql := sql || format(
            '(SELECT * FROM orders WHERE status = %L AND created_at >= %L)',
            v_status, '2024-06-01'
        );
    END LOOP;

    RAISE NOTICE 'Generated SQL: %', sql;

    -- 执行生成的SQL
    EXECUTE format(
        'EXPLAIN (ANALYZE, BUFFERS) %s ORDER BY created_at LIMIT 100',
        sql
    );
END $$;

-- ========== Case 24: UNION ALL + JOIN ==========

EXPLAIN (ANALYZE, BUFFERS)
WITH active_orders AS (
    SELECT o.*, c.name, c.region
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE o.status = 'ACTIVE' AND o.created_at >= '2024-06-01'
),
completed_orders AS (
    SELECT o.*, c.name, c.region
    FROM orders o
    JOIN customers c ON c.customer_id = o.customer_id
    WHERE o.status = 'COMPLETED' AND o.created_at >= '2024-06-01'
)
SELECT * FROM active_orders
UNION ALL
SELECT * FROM completed_orders
ORDER BY created_at
LIMIT 50;

-- ========== Case 25: 性能对比测试 ==========

-- 测试1: Seq Scan (禁用索引)
SET enable_indexscan = OFF;
SET enable_bitmapscan = OFF;

\timing on
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*), SUM(amount)
FROM orders
WHERE created_at >= '2024-06-01';
\timing off

-- 测试2: 使用索引
SET enable_indexscan = ON;

\timing on
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*), SUM(amount)
FROM orders
WHERE created_at >= '2024-06-01';
\timing off

-- 测试3: 使用反向索引
SET enable_indexscan = ON;

-- 强制使用反向索引 (如果支持)
\timing on
EXPLAIN (ANALYZE, BUFFERS)
SELECT /*+ IndexScan(orders idx_orders_created_status) */
COUNT(*), SUM(amount)
FROM orders
WHERE created_at >= '2024-06-01';
\timing off

-- 测试4: UNION ALL改写
\timing on
EXPLAIN (ANALYZE, BUFFERS)
WITH active_orders AS (
    SELECT COUNT(*), SUM(amount) FROM orders
    WHERE status = 'ACTIVE' AND created_at >= '2024-06-01'
),
completed_orders AS (
    SELECT COUNT(*), SUM(amount) FROM orders
    WHERE status = 'COMPLETED' AND created_at >= '2024-06-01'
),
pending_orders AS (
    SELECT COUNT(*), SUM(amount) FROM orders
    WHERE status = 'PENDING' AND created_at >= '2024-06-01'
),
processing_orders AS (
    SELECT COUNT(*), SUM(amount) FROM orders
    WHERE status = 'PROCESSING' AND created_at >= '2024-06-01'
),
cancelled_orders AS (
    SELECT COUNT(*), SUM(amount) FROM orders
    WHERE status = 'CANCELLED' AND created_at >= '2024-06-01'
)
SELECT
    SUM(cnt) AS total_count,
    SUM(total_amount) AS grand_total
FROM (
    SELECT * FROM active_orders
    UNION ALL
    SELECT * FROM completed_orders
    UNION ALL
    SELECT * from pending_orders
    UNION ALL
    SELECT * FROM processing_orders
    UNION ALL
    SELECT * FROM cancelled_orders
) results;
\timing off

-- ============================================================================
-- 第六部分: 性能基准测试
-- ============================================================================

-- 创建性能对比表
DROP TABLE IF EXISTS performance_comparison;
CREATE TEMP TABLE performance_comparison (
    test_case INT,
    approach VARCHAR(50),
    execution_time NUMERIC(10,3),
    plan_type VARCHAR(50),
    buffers_hit BIGINT,
    buffers_read BIGINT
);

-- 记录各场景性能
INSERT INTO performance_comparison VALUES
(6, 'Skip Scan候选', NULL, NULL, NULL, NULL),
(16, '反向索引', NULL, NULL, NULL, NULL),
(21, 'UNION ALL改写', NULL, NULL, NULL, NULL);

-- ============================================================================
-- 第七部分: Skip Scan识别查询
-- ============================================================================

-- ========== 识别可能受益于Skip Scan的查询 ==========

-- 查询1: 查找复合索引
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE schemaname = 'skip_scan_test'
  AND indexdef LIKE '%,%'  -- 复合索引
ORDER BY tablename, indexname;

-- 查询2: 检查前导列基数
SELECT
    s.schemaname,
    s.tablename,
    s.attname AS leading_column,
    s.n_distinct,
    CASE
        WHEN ABS(s.n_distinct) <= 10 THEN 'Excellent for Skip Scan'
        WHEN ABS(s.n_distinct) <= 50 THEN 'Good for Skip Scan'
        WHEN ABS(s.n_distinct) <= 100 THEN 'Maybe Skip Scan'
        ELSE 'Use reverse index instead'
    END AS skip_scan_potential
FROM pg_stats s
JOIN pg_indexes i ON s.tablename = i.tablename
WHERE s.schemaname = 'skip_scan_test'
  AND s.attname = SUBSTRING(i.indexdef, POSITION('(' IN i.indexdef) + 1,
                            POSITION(',' IN i.indexdef) - POSITION('(' IN i.indexdef) - 1)
ORDER BY ABS(s.n_distinct) ASC;

-- 查询3: 分析查询模式 (从pg_stat_statements)
-- 需要pg_stat_statements扩展

-- ============================================================================
-- 第八部分: 清理
-- ============================================================================

-- 保留测试表用于后续验证
-- DROP SCHEMA IF EXISTS skip_scan_test CASCADE;

-- ============================================================================
-- 总结
-- ============================================================================
--
-- 本文件包含25个BTree Index Skip Scan测试场景:
--
-- 第一部分: 环境准备 (5个case)
--   - 创建测试表
--   - 数据分布检查
--   - 索引创建
--
-- 第二部分: 基础Skip Scan场景 (5个case)
--   - 基础查询
--   - 排序、聚合、JOIN、DISTINCT
--
-- 第三部分: 不同基数对比 (5个case)
--   - 极低基数 (2值)
--   - 低基数 (5值)
--   - 中等基数 (20值)
--   - 高基数 (100值)
--   - 极高基数 (1000值)
--
-- 第四部分: 反向索引优化 (5个case)
--   - 基础测试
--   - 前导列条件
--   - 范围查询
--   - 排序
--   - 覆盖查询
--
-- 第五部分: UNION ALL改写 (5个case)
--   - 手动改写
--   - 聚合查询
--   - 动态生成
--   - JOIN优化
--   - 性能对比
--
-- 关键学习点:
-- 1. Skip Scan适用于低基数前导列 (2-100个不同值)
-- 2. VastBase可能不支持Skip Scan，需要替代方案
-- 3. 反向索引是最通用的替代方案
-- 4. UNION ALL可手动实现Skip Scan逻辑
-- 5. 性能提升取决于前导列基数和数据分布
--
-- ============================================================================
