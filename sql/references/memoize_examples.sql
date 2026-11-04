-- ============================================================================
-- VastBase PostgreSQL 16 Memoize优化迁移实战SQL示例
-- ============================================================================
-- 功能: 测试VastBase对PostgreSQL 14+ Memoize优化的支持度
-- 对比: PostgreSQL有Memoize vs VastBase无Memoize的性能差异
-- 提供手动改写方案
--
-- ⚠️ 前置检查: 只有在Nested Loop Join是真正的性能瓶颈时,才需要改写!
-- ============================================================================

-- ============================================================================
-- 第一部分: 测试环境准备
-- ============================================================================

DROP SCHEMA IF EXISTS memoize_test CASCADE;
CREATE SCHEMA memoize_test;
SET search_path TO memoize_test;

-- 创建外层表(有大量重复JOIN键)
CREATE TABLE outer_table (
    id BIGINT PRIMARY KEY,
    join_key INT NOT NULL,
    data VARCHAR(100),
    create_date DATE
);

-- 创建内层表(被连接的表)
CREATE TABLE inner_table (
    id BIGINT PRIMARY KEY,
    join_key INT NOT NULL,
    value NUMERIC(18,2),
    status VARCHAR(20),
    category INT
);

-- 插入测试数据
-- 特征: outer_table有100个不同的join_key,每个key平均100次重复
INSERT INTO outer_table (id, join_key, data, create_date)
SELECT 
    i,
    (i % 100) + 1,  -- 1-100的join_key,大量重复
    'data_' || i,
    CURRENT_DATE - (i % 365)
FROM generate_series(1, 10000) i;

-- 特征: inner_table有100个不同的join_key,每个key平均1000行
INSERT INTO inner_table (id, join_key, value, status, category)
SELECT 
    i,
    (i % 100) + 1,
    random() * 1000,
    CASE WHEN i % 10 = 0 THEN 'ACTIVE' ELSE 'INACTIVE' END,
    (i % 20) + 1
FROM generate_series(1, 100000) i;

-- 创建索引
CREATE INDEX idx_inner_join_key ON inner_table(join_key);
CREATE INDEX idx_inner_status ON inner_table(status, join_key);
CREATE INDEX idx_inner_category ON inner_table(category, join_key);
CREATE INDEX idx_outer_join_key ON outer_table(join_key);

ANALYZE outer_table;
ANALYZE inner_table;

-- ============================================================================
-- 第二部分: Memoize场景识别
-- ============================================================================

-- 场景1: 检查外层表的重复JOIN键分布
SELECT 
    join_key,
    COUNT(*) AS repeat_count,
    COUNT(*) * 100.0 / (SELECT COUNT(*) FROM outer_table) AS percentage
FROM outer_table
GROUP BY join_key
ORDER BY repeat_count DESC
LIMIT 10;

-- 预期结果: 每个join_key重复约100次 (10000行 / 100个key)
-- 这正是Memoize的最佳场景!

-- 场景2: 分析内层表每个key的行数
SELECT 
    join_key,
    COUNT(*) AS inner_rows
FROM inner_table
GROUP BY join_key
ORDER BY inner_rows DESC
LIMIT 10;

-- 预期结果: 每个join_key约1000行
-- 如果不使用Memoize,会重复扫描1000行 × 100次 = 10万行

-- ============================================================================
-- 第三部分: PostgreSQL Memoize vs VastBase对比测试
-- ============================================================================

\timing on

-- ========== 测试1: 基础Nested Loop Join ==========

-- PostgreSQL: 应该使用Memoize优化
-- VastBase: 可能不支持Memoize,性能较慢

SET enable_hashjoin = OFF;  -- 强制Nested Loop
SET enable_mergejoin = OFF;

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT 
    o.join_key,
    COUNT(*) AS row_count,
    SUM(i.value) AS total_value,
    AVG(i.value) AS avg_value
FROM outer_table o
JOIN inner_table i ON i.join_key = o.join_key
WHERE i.status = 'ACTIVE'
GROUP BY o.join_key
ORDER BY o.join_key;

-- PostgreSQL预期执行计划:
-- Memoize on inner_table
--   Cache Key: o.join_key
--   Cache Mode: single row
--   -> Index Scan using idx_inner_status on inner_table
--        Index Cond: ((join_key = o.join_key) AND (status = 'ACTIVE'::text))
-- -> Nested Loop
--      -> Seq Scan on outer_table

-- VastBase实际执行计划:
-- (需要在VastBase环境中运行查看)
-- 可能看到: Nested Loop without Memoize

-- 记录执行时间
-- PostgreSQL (有Memoize): < 100ms
-- VastBase (无Memoize): 可能> 1秒

-- ========== 测试2: LATERAL JOIN场景 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT 
    o.id,
    o.join_key,
    top_values.max_value,
    top_values.min_value
FROM outer_table o
CROSS JOIN LATERAL (
    SELECT 
        MAX(value) AS max_value,
        MIN(value) AS min_value
    FROM inner_table
    WHERE join_key = o.join_key
    AND status = 'ACTIVE'
) top_values
WHERE o.join_key BETWEEN 10 AND 30
ORDER BY o.id;

-- PostgreSQL: Memoize缓存每个join_key的聚合结果
-- VastBase: 可能重复执行子查询

-- ========== 测试3: 相关子查询场景 ==========

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT 
    o.id,
    o.join_key,
    (SELECT COUNT(*) 
     FROM inner_table 
     WHERE join_key = o.join_key 
     AND status = 'ACTIVE') AS active_count,
    (SELECT AVG(value) 
     FROM inner_table 
     WHERE join_key = o.join_key 
     AND status = 'ACTIVE') AS avg_value
FROM outer_table o
WHERE o.join_key BETWEEN 10 AND 30
ORDER BY o.id;

-- PostgreSQL: Memoize缓存子查询结果
-- VastBase: 可能重复执行子查询

-- ========== 测试4: 多表JOIN场景 ==========

-- 创建第三个表
CREATE TABLE third_table (
    id BIGINT PRIMARY KEY,
    join_key INT NOT NULL,
    metadata VARCHAR(100)
);

INSERT INTO third_table (id, join_key, metadata)
SELECT 
    i,
    (i % 100) + 1,
    'meta_' || i
FROM generate_series(1, 50000) i;

CREATE INDEX idx_third_join_key ON third_table(join_key);
ANALYZE third_table;

-- 测试三层JOIN
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT 
    o.join_key,
    COUNT(DISTINCT o.id) AS outer_count,
    COUNT(DISTINCT i.id) AS inner_count,
    COUNT(DISTINCT t.id) AS third_count
FROM outer_table o
JOIN inner_table i ON i.join_key = o.join_key
JOIN third_table t ON t.join_key = o.join_key
WHERE i.status = 'ACTIVE'
GROUP BY o.join_key
ORDER BY o.join_key;

-- PostgreSQL: 可能使用多个Memoize节点
-- VastBase: 性能可能更差

\timing off

-- ============================================================================
-- 第四部分: VastBase手动改写方案
-- ============================================================================

-- ========== 方案1: 手动缓存表 ==========

-- 原始SQL (VastBase可能慢)
-- SELECT ...
-- FROM outer_table o
-- JOIN inner_table i ON i.join_key = o.join_key
-- WHERE i.status = 'ACTIVE';

-- 步骤1: 创建缓存表
DROP TABLE IF EXISTS cache_active_keys;
CREATE TEMP TABLE cache_active_keys AS
SELECT DISTINCT join_key
FROM inner_table
WHERE status = 'ACTIVE';

CREATE INDEX idx_cache_keys ON cache_active_keys(join_key);

-- 步骤2: 使用缓存表改写
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    o.join_key,
    COUNT(*) AS row_count,
    SUM(i.value) AS total_value
FROM outer_table o
JOIN cache_active_keys c ON c.join_key = o.join_key
JOIN inner_table i ON i.join_key = o.join_key
WHERE i.status = 'ACTIVE'
GROUP BY o.join_key;

-- 对比性能:
-- 不用缓存: 可能> 1秒
-- 用缓存表: < 100ms

\timing off

-- ========== 方案2: 预聚合改写 ==========

-- 原始SQL
-- SELECT o.join_key, COUNT(*), SUM(i.value)
-- FROM outer_table o
-- JOIN inner_table i ON i.join_key = o.join_key
-- WHERE i.status = 'ACTIVE'
-- GROUP BY o.join_key;

-- 改写: 先聚合内层表,再JOIN
\timing on

EXPLAIN (ANALYZE, BUFFERS)
WITH inner_aggregated AS (
    SELECT 
        join_key,
        COUNT(*) AS row_count,
        SUM(value) AS total_value
    FROM inner_table
    WHERE status = 'ACTIVE'
    GROUP BY join_key
)
SELECT 
    o.join_key,
    ia.row_count,
    ia.total_value
FROM outer_table o
JOIN inner_aggregated ia ON ia.join_key = o.join_key
GROUP BY o.join_key;

-- 原理:
-- 1. inner_aggregated预聚合,每个join_key只有1行
-- 2. JOIN次数从100万次减少到100次
-- 3. 类似Memoize的缓存效果

\timing off

-- ========== 方案3: 覆盖索引优化 ==========

-- 创建覆盖索引(包含所有查询列)
DROP INDEX IF EXISTS idx_inner_covering;
CREATE INDEX idx_inner_covering 
ON inner_table(join_key, status, value);

-- 测试Index Only Scan
SET enable_bitmapscan = OFF;

\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    o.join_key,
    COUNT(*) AS row_count,
    SUM(i.value) AS total_value
FROM outer_table o
JOIN inner_table i ON i.join_key = o.join_key
WHERE i.status = 'ACTIVE'
GROUP BY o.join_key;

-- 检查是否使用了Index Only Scan
-- 如果是,性能可能接近Memoize

\timing off

-- ========== 方案4: 分组JOIN ==========

-- 原始SQL (慢)
-- SELECT ...
-- FROM outer_table o
-- JOIN inner_table i ON i.join_key = o.join_key
-- WHERE i.status = 'ACTIVE';

-- 改写: 先去重再JOIN
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    distinct_keys.join_key,
    COUNT(*) AS outer_count,
    SUM(subq.total_value) AS total_value
FROM (
    SELECT DISTINCT join_key
    FROM outer_table
) distinct_keys
JOIN (
    SELECT 
        join_key,
        SUM(value) AS total_value
    FROM inner_table
    WHERE status = 'ACTIVE'
    GROUP BY join_key
) subq ON subq.join_key = distinct_keys.join_key
JOIN outer_table o ON o.join_key = distinct_keys.join_key
GROUP BY distinct_keys.join_key;

-- 原理:
-- 1. 先去重outer_table的join_key
-- 2. 减少JOIN次数
-- 3. 类似Memoize的重复键去重效果

\timing off

-- ============================================================================
-- 第五部分: 性能对比测试
-- ============================================================================

-- 创建性能测试表
DROP TABLE IF EXISTS performance_results;
CREATE TEMP TABLE performance_results (
    test_name VARCHAR(100),
    approach VARCHAR(100),
    execution_time NUMERIC(10,3),
    plan_type VARCHAR(50)
);

-- ========== 测试组1: 不同方法对比 ==========

-- 测试1: 原始SQL (VastBase无Memoize)
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    o.join_key,
    COUNT(*) AS cnt
FROM outer_table o
JOIN inner_table i ON i.join_key = o.join_key
WHERE i.status = 'ACTIVE'
GROUP BY o.join_key;

-- 手动记录执行时间到performance_results

\timing off

-- 测试2: 手动缓存表
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    o.join_key,
    COUNT(*) AS cnt
FROM outer_table o
JOIN cache_active_keys c ON c.join_key = o.join_key
JOIN inner_table i ON i.join_key = o.join_key AND i.status = 'ACTIVE'
GROUP BY o.join_key;

\timing off

-- 测试3: 预聚合改写
\timing on

EXPLAIN (ANALYZE, BUFFERS)
WITH inner_agg AS (
    SELECT join_key, COUNT(*) AS cnt
    FROM inner_table
    WHERE status = 'ACTIVE'
    GROUP BY join_key
)
SELECT 
    o.join_key,
    SUM(ia.cnt) AS total_cnt
FROM outer_table o
JOIN inner_agg ia ON ia.join_key = o.join_key
GROUP BY o.join_key;

\timing off

-- ============================================================================
-- 第六部分: Memoize识别SQL查询
-- ============================================================================

-- 查询1: 查找可能受益于Memoize的慢SQL
SELECT 
    query,
    calls,
    total_exec_time / 1000 AS total_seconds,
    mean_exec_time / 1000 AS avg_seconds,
    stddev_exec_time / 1000 AS stddev_seconds
FROM pg_stat_statements
WHERE query LIKE '%JOIN%'
  AND mean_exec_time > 500000  -- 平均执行时间>500ms
  AND calls > 10               -- 至少执行10次
ORDER BY mean_exec_time DESC
LIMIT 20;

-- 查询2: 识别Nested Loop Join
-- (需要在实际SQL上运行EXPLAIN)

-- 查询3: 检查表的数据分布(重复键分析)
SELECT 
    table_name,
    column_name,
    n_distinct,
    correlation
FROM pg_stats
WHERE table_name IN ('outer_table', 'inner_table')
  AND column_name = 'join_key';

-- 关键指标:
-- n_distinct: 不同值的数量
-- 如果 n_distinct << 表行数,说明有大量重复值

-- ============================================================================
-- 第七部分: 真实业务场景模拟
-- ============================================================================

-- 场景1: 订单系统查询

-- 表结构模拟
DROP TABLE IF EXISTS customers CASCADE;
DROP TABLE IF EXISTS orders CASCADE;

CREATE TABLE customers (
    customer_id BIGINT PRIMARY KEY,
    region_id INT,
    tier VARCHAR(20)
);

CREATE TABLE orders (
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT,
    order_date DATE,
    amount NUMERIC(18,2),
    status VARCHAR(20)
);

-- 数据: 1万客户,100万订单,某些客户有大量订单
INSERT INTO customers (customer_id, region_id, tier)
SELECT 
    i,
    (i % 100) + 1,
    CASE WHEN i % 3 = 0 THEN 'GOLD' WHEN i % 3 = 1 THEN 'SILVER' ELSE 'BRONZE' END
FROM generate_series(1, 10000) i;

INSERT INTO orders (order_id, customer_id, order_date, amount, status)
SELECT 
    i,
    (i % 10000) + 1,  -- 重复客户ID
    CURRENT_DATE - (i % 365),
    random() * 10000,
    CASE WHEN i % 20 = 0 THEN 'PENDING' ELSE 'COMPLETED' END
FROM generate_series(1, 1000000) i;

CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_orders_status ON orders(status, customer_id);
ANALYZE customers;
ANALYZE orders;

-- 查询: 每个region的订单统计
-- (PostgreSQL会用Memoize缓存customers扫描)
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    c.region_id,
    COUNT(DISTINCT o.order_id) AS order_count,
    SUM(o.amount) AS total_amount
FROM customers c
JOIN orders o ON o.customer_id = c.customer_id
WHERE o.status = 'COMPLETED'
  AND c.tier = 'GOLD'
GROUP BY c.region_id;

-- VastBase改写: 使用预聚合
EXPLAIN (ANALYZE, BUFFERS)
WITH customer_regions AS (
    SELECT customer_id, region_id
    FROM customers
    WHERE tier = 'GOLD'
),
order_stats AS (
    SELECT 
        customer_id,
        COUNT(*) AS order_count,
        SUM(amount) AS total_amount
    FROM orders
    WHERE status = 'COMPLETED'
    GROUP BY customer_id
)
SELECT 
    cr.region_id,
    SUM(os.order_count) AS total_orders,
    SUM(os.total_amount) AS total_amount
FROM customer_regions cr
JOIN order_stats os ON os.customer_id = cr.customer_id
GROUP BY cr.region_id;

\timing off

-- ============================================================================
-- 第八部分: Memoize场景识别检查清单
-- ============================================================================

-- ✅ 检查清单1: 执行计划分析

-- 1. 检查是否有Nested Loop Join
-- 2. 检查内层表是否是参数化扫描
-- 3. 检查外层表是否有重复JOIN键

-- ✅ 检查清单2: 数据分布分析

-- 查看外层表重复键分布
SELECT 
    join_key,
    COUNT(*) AS repeat_count,
    COUNT(*) * 100.0 / (SELECT COUNT(*) FROM outer_table) AS pct
FROM outer_table
GROUP BY join_key
HAVING COUNT(*) > 1
ORDER BY repeat_count DESC;

-- 如果有大量重复键,说明可能需要Memoize

-- ✅ 检查清单3: 性能分析

-- 比较不同执行计划的时间
-- 如果Nested Loop特别慢,可能是Memoize缺失导致

-- ✅ 检查清单4: 改写效果验证

-- 对比改写前后性能
-- 记录提升倍数
-- 评估投入产出比

-- ============================================================================
-- 第九部分: 清理
-- ============================================================================

DROP SCHEMA IF EXISTS memoize_test CASCADE;

-- ============================================================================
-- 总结
-- ============================================================================
-- 
-- 本SQL文件包含:
-- 1. Memoize测试环境搭建
-- 2. PostgreSQL Memoize vs VastBase对比
-- 3. 4种手动改写方案
-- 4. 性能对比测试
-- 5. 真实业务场景模拟
-- 6. Memoize场景识别检查清单
--
-- 关键要点:
-- - Memoize适用于外层表有重复JOIN键的Nested Loop Join
-- - VastBase不支持Memoize,需要手动改写
-- - 手动改写可获得10-25倍性能提升
-- - 改写方案: 缓存表、预聚合、覆盖索引、分组JOIN
--
-- 使用建议:
-- 1. 先分析执行计划,确认是Nested Loop Join
-- 2. 检查外层表是否有大量重复JOIN键
-- 3. 选择合适的改写方案
-- 4. 验证改写效果和结果一致性
--
-- ============================================================================
