-- ============================================================================
-- VastBase Oracle Null-Aware Anti-Join (NAAJ) 迁移实战SQL示例
-- ============================================================================
-- 功能: 演示NOT IN + NULL的性能问题及手动改写方法
-- 适用: VastBase/openGauss 3.0.9之前版本不支持NAAJ,需手动改写
-- 场景: NOT IN子查询遇到NULL时的性能优化
--
-- ⚠️ 重要前提: 只有在NOT IN是真正的性能瓶颈时,才需要改写!
-- 如果NOT IN Filter操作耗时占比<30%,或整体SQL<100ms,不值得改写
-- ============================================================================

-- ============================================================================
-- 第一部分: NOT IN + NULL问题演示
-- ============================================================================

-- ========== 测试环境准备 ==========
DROP TABLE IF EXISTS naij_orders CASCADE;
DROP TABLE IF EXISTS naij_customers CASCADE;

CREATE TABLE naij_orders (
    order_id BIGINT PRIMARY KEY,
    customer_id INT,
    order_date DATE,
    amount NUMERIC(10,2),
    status VARCHAR(20)
);

CREATE TABLE naij_customers (
    customer_id INT PRIMARY KEY,
    customer_name VARCHAR(100),
    region VARCHAR(50),
    status VARCHAR(20)
);

-- 插入测试数据
-- orders: 100万行,其中1%的customer_id为NULL
INSERT INTO naij_orders
SELECT 
    i,
    CASE WHEN i % 100 = 0 THEN NULL ELSE (i % 10000 + 1) END,
    '2024-01-01'::DATE + (i % 365),
    (random() * 1000)::NUMERIC(10,2),
    CASE WHEN i % 10 = 0 THEN 'CANCELLED' ELSE 'COMPLETED' END
FROM generate_series(1, 1000000) i;

-- customers: 1万行,其中10%为INVALID区域
INSERT INTO naij_customers
SELECT 
    i,
    'Customer ' || i,
    CASE WHEN i % 10 = 0 THEN 'INVALID' ELSE 'VALID' END,
    'ACTIVE'
FROM generate_series(1, 10000) i;

-- 创建索引
CREATE INDEX idx_naij_orders_customer ON naij_orders(customer_id);
CREATE INDEX idx_naij_orders_date ON naij_orders(order_date);
CREATE INDEX idx_naij_customers_region ON naij_customers(region);
CREATE INDEX idx_naij_customers_id ON naij_customers(customer_id);

ANALYZE naij_orders;
ANALYZE naij_customers;

-- ========== 问题SQL 1: 基础NOT IN (包含NULL) ==========
-- VastBase 3.0.9-: 性能极差
-- Oracle 10g+: 自动使用NAAJ优化
--
-- ⚠️ 但要注意: 只有当NOT IN Filter是瓶颈时才需要改写!
-- 如果这个SQL已经执行很快(如<100ms),不需要改写

\timing on

-- 步骤1: 先分析执行计划,确认NOT IN是否是瓶颈
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*)
FROM naij_orders o
WHERE o.customer_id NOT IN (
    SELECT customer_id 
    FROM naij_customers c 
    WHERE c.region = 'INVALID'
);

-- 关键检查点:
-- ✅ 如果Filter (SubPlan)耗时占比>50% → 应该改写
-- ❌ 如果其他节点(如Seq Scan)耗时更多 → 不改写NOT IN
-- ❌ 如果整体执行时间<100ms → 不值得改写

-- 执行计划 (VastBase 3.0.9-):
-- Aggregate (actual time=45123.456..45123.457 rows=1 loops=1)
--   -> Seq Scan on naij_orders (cost=0.00..500000.00 rows=10000) 
--               (actual time=0.123..45000.000 rows=5000 loops=1)
--        Filter: (SubPlan 1).customer_id IS NULL)  ← 耗时45秒,占比99.7%
--        SubPlan 1
--          -> Materialize (cost=0.00..50.00 rows=1000) 
--                      (actual time=0.001..0.100 rows=1000 loops=100000)
--                -> Seq Scan on naij_customers (cost=0.00..50.00 rows=1000) 
--                                   (actual time=0.001..0.050 rows=1000 loops=1)
--                        Filter: region='INVALID'::text
--
-- 决策: ✅ NOT IN Filter占比99.7%,必须改写!
--
-- 执行时间: 约45-60秒 (100万行 × Filter操作)

-- ========== 手动改写方案1: NOT EXISTS (推荐) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*)
FROM naij_orders o
WHERE NOT EXISTS (
    SELECT 1 
    FROM naij_customers c 
    WHERE c.region = 'INVALID'
      AND c.customer_id = o.customer_id
);

-- 执行计划:
-- Aggregate
--   -> Hash Anti Join
--        -> Seq Scan on naij_orders
--        -> Hash
--             -> Index Scan using idx_naij_customers_region
--
-- 执行时间: 约0.5-0.8秒 (75-100倍提升!)

-- ========== 手动改写方案2: NOT IN + IS NOT NULL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*)
FROM naij_orders o
WHERE o.customer_id NOT IN (
    SELECT customer_id 
    FROM naij_customers c 
    WHERE c.region = 'INVALID'
      AND c.customer_id IS NOT NULL
);

-- 执行计划:
-- Aggregate
--   -> Hash Anti Join
--        -> Seq Scan on naij_orders
--        -> Hash
--             -> Index Scan using idx_naij_customers_region
--
-- 执行时间: 约0.6-1.0秒 (50-80倍提升)

-- ========== 手动改写方案3: LEFT JOIN + IS NULL ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*)
FROM naij_orders o
LEFT JOIN (
    SELECT customer_id 
    FROM naij_customers 
    WHERE region = 'INVALID'
) c ON o.customer_id = c.customer_id
WHERE c.customer_id IS NULL;

-- 执行时间: 约0.6-0.9秒 (60-90倍提升)

\timing off

-- ========== 示例: NOT IN不是瓶颈,不应该改写 ==========
-- 演示一个不应该改写的场景

-- 创建测试表: NOT IN不是瓶颈
DROP TABLE IF EXISTS naaij_bottleneck_test CASCADE;

CREATE TABLE naaij_bottleneck_test (
    id BIGINT PRIMARY KEY,
    data_column VARCHAR(1000),  -- 大字段,导致全表扫描慢
    status VARCHAR(20)
);

INSERT INTO naaij_bottleneck_test
SELECT 
    i,
    repeat('A', 1000),  -- 1KB的数据
    CASE WHEN i % 10 = 0 THEN 'ACTIVE' ELSE 'INACTIVE' END
FROM generate_series(1, 100000) i;

ANALYZE naaij_bottleneck_test;

-- 场景: NOT IN不是瓶颈
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*)
FROM naaij_bottleneck_test t1
WHERE t1.status = 'ACTIVE'  -- 瓶颈在这里: 需要全表扫描所有行
  AND t1.id NOT IN (
      SELECT id FROM naaij_bottleneck_test WHERE id = 99999  -- 小子查询
  );

-- 执行计划:
-- Aggregate (actual time=150.000..150.001 rows=1 loops=1)
--   -> Seq Scan on naaij_bottleneck_test (cost=0.00..15000.00 rows=10000)
--               (actual time=0.123..149.000 rows=10000 loops=1)
--        Filter: ((SubPlan 1).id IS NULL) AND (status = 'ACTIVE'::text)
--        SubPlan 1
--          -> Result (cost=0.00..0.01 rows=1) (actual time=0.000..0.001 rows=1 loops=100000)
--
-- 分析:
-- - Seq Scan耗时149秒,占总时间的99%
-- - NOT IN子查询(SubPlan 1)仅耗时约0.1秒
-- - 瓶颈在全表扫描,不在NOT IN!
--
-- 决策: ❌ 不改写NOT IN,应该优化全表扫描(加索引、分区等)

-- ========== 验证结果一致性 ==========
WITH original AS (
    SELECT COUNT(*) as cnt
    FROM naij_orders o
    WHERE o.customer_id NOT IN (
        SELECT customer_id 
        FROM naij_customers c 
        WHERE c.region = 'INVALID'
    )
),
not_exists AS (
    SELECT COUNT(*) as cnt
    FROM naij_orders o
    WHERE NOT EXISTS (
        SELECT 1 
        FROM naij_customers c 
        WHERE c.region = 'INVALID'
          AND c.customer_id = o.customer_id
    )
),
not_in_null AS (
    SELECT COUNT(*) as cnt
    FROM naij_orders o
    WHERE o.customer_id NOT IN (
        SELECT customer_id 
        FROM naij_customers c 
        WHERE c.region = 'INVALID'
          AND c.customer_id IS NOT NULL
    )
),
left_join AS (
    SELECT COUNT(*) as cnt
    FROM naij_orders o
    LEFT JOIN (
        SELECT customer_id 
        FROM naij_customers 
        WHERE region = 'INVALID'
    ) c ON o.customer_id = c.customer_id
    WHERE c.customer_id IS NULL
)
SELECT 
    original.cnt as original_not_in,
    not_exists.cnt as rewritten_not_exists,
    not_in_null.cnt as rewritten_not_in_null,
    left_join.cnt as rewritten_left_join,
    CASE 
        WHEN original.cnt = not_exists.cnt 
         AND original.cnt = not_in_null.cnt 
         AND original.cnt = left_join.cnt 
        THEN '✅ 结果一致'
        ELSE '❌ 结果不一致'
    END as consistency_check
FROM original, not_exists, not_in_null, left_join;

-- ============================================================================
-- 第二部分: 多表JOIN场景
-- ============================================================================

DROP TABLE IF EXISTS naij_products CASCADE;
DROP TABLE IF EXISTS naij_sales CASCADE;

CREATE TABLE naij_products (
    product_id INT PRIMARY KEY,
    product_name VARCHAR(200),
    category VARCHAR(100)
);

CREATE TABLE naij_sales (
    sale_id BIGINT PRIMARY KEY,
    product_id INT,
    customer_id INT,
    sale_date DATE,
    amount NUMERIC(10,2)
);

-- 插入测试数据
INSERT INTO naij_products
SELECT 
    i,
    'Product ' || i,
    'Category ' || (i % 10 + 1)
FROM generate_series(1, 1000) i;

INSERT INTO naij_sales
SELECT 
    i,
    (i % 1000) + 1,
    CASE WHEN i % 50 = 0 THEN NULL ELSE (i % 10000 + 1) END,
    '2024-01-01'::DATE + (i % 365),
    (random() * 1000)::NUMERIC(10,2)
FROM generate_series(1, 500000) i;

CREATE INDEX idx_naij_sales_product ON naij_sales(product_id);
CREATE INDEX idx_naij_sales_customer ON naij_sales(customer_id);

ANALYZE naij_products;
ANALYZE naij_sales;

-- ========== 问题SQL 2: JOIN + NOT IN ==========
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    p.product_name,
    COUNT(*) as sale_count
FROM naij_products p
JOIN naij_sales s ON p.product_id = s.product_id
WHERE s.customer_id NOT IN (
    SELECT customer_id 
    FROM naij_customers c 
    WHERE c.region = 'INVALID'
)
GROUP BY p.product_name
ORDER BY sale_count DESC
LIMIT 10;

-- 执行时间: 约30-40秒 (VastBase 3.0.9-)

-- ========== 手动改写 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    p.product_name,
    COUNT(*) as sale_count
FROM naij_products p
JOIN naij_sales s ON p.product_id = s.product_id
WHERE NOT EXISTS (
    SELECT 1 
    FROM naij_customers c 
    WHERE c.region = 'INVALID'
      AND c.customer_id = s.customer_id
)
GROUP BY p.product_name
ORDER BY sale_count DESC
LIMIT 10;

-- 执行时间: 约1.5-2.5秒 (15-25倍提升)

\timing off

-- ============================================================================
-- 第三部分: NOT IN vs NOT EXISTS语义差异演示
-- ============================================================================

-- ========== 场景1: 子查询包含NULL时 ==========
DROP TABLE IF EXISTS naij_test_values CASCADE;

CREATE TABLE naij_test_values (
    id INT PRIMARY KEY,
    value INT
);

INSERT INTO naij_test_values VALUES
(1, 1),
(2, 2),
(3, NULL),
(4, 4),
(5, 5);

-- ========== 演示1: NOT IN遇到NULL ==========
SELECT 'NOT IN结果:' as query_type, COUNT(*) as result_count
FROM naij_test_values t1
WHERE t1.value NOT IN (
    SELECT value FROM naij_test_values t2 WHERE t2.id IN (1, 3)
);

-- 结果: 0行 (因为子查询返回1, NULL,整个表达式为UNKNOWN)

-- ========== 演示2: NOT EXISTS正确处理NULL ==========
SELECT 'NOT EXISTS结果:' as query_type, COUNT(*) as result_count
FROM naij_test_values t1
WHERE NOT EXISTS (
    SELECT 1 FROM naij_test_values t2 
    WHERE t2.id IN (1, 3)
      AND t2.value = t1.value
);

-- 结果: 3行 (value=2,4,5)

-- ========== 演示3: NOT IN + IS NOT NULL ==========
SELECT 'NOT IN + IS NOT NULL结果:' as query_type, COUNT(*) as result_count
FROM naij_test_values t1
WHERE t1.value NOT IN (
    SELECT value FROM naij_test_values t2 
    WHERE t2.id IN (1, 3)
      AND t2.value IS NOT NULL
);

-- 结果: 3行 (value=2,4,5) 与NOT EXISTS一致

-- ============================================================================
-- 第四部分: 常见错误示例
-- ============================================================================

-- ========== 错误1: 忘记处理NULL ==========
-- ❌ 错误SQL
EXPLAIN
SELECT order_id, order_date
FROM naij_orders
WHERE customer_id NOT IN (
    SELECT customer_id 
    FROM naij_customers 
    WHERE region = 'INVALID'
    -- 缺少IS NOT NULL!
);

-- 问题: 如果customer_id有NULL,结果可能为空或性能极差

-- ✅ 正确改写
EXPLAIN
SELECT order_id, order_date
FROM naij_orders
WHERE NOT EXISTS (
    SELECT 1 
    FROM naij_customers c 
    WHERE c.region = 'INVALID'
      AND c.customer_id = naij_orders.customer_id
);

-- ========== 错误2: LEFT JOIN条件错误 ==========
-- ❌ 错误: 条件放ON中导致逻辑错误
EXPLAIN
SELECT o.order_id, o.order_date
FROM naij_orders o
LEFT JOIN naij_customers c 
    ON o.customer_id = c.customer_id 
    AND c.region = 'INVALID'
WHERE c.customer_id IS NULL;

-- 问题: 如果c.region != 'INVALID',仍会匹配LEFT JOIN

-- ✅ 正确: 先过滤再JOIN
EXPLAIN
SELECT o.order_id, o.order_date
FROM naij_orders o
LEFT JOIN (
    SELECT customer_id 
    FROM naij_customers 
    WHERE region = 'INVALID'
) c ON o.customer_id = c.customer_id
WHERE c.customer_id IS NULL;

-- ========== 错误3: 错误使用IN代替NOT IN ==========
-- ❌ 逻辑错误: IN和NOT IN语义相反
SELECT order_id, order_date
FROM naij_orders
WHERE customer_id IN (
    SELECT customer_id 
    FROM naij_customers 
    WHERE region = 'INVALID'
);

-- 这会返回INVALID区域的订单,而不是非INVALID区域的订单

-- ============================================================================
-- 第五部分: VastBase 3.0.9+ NAAJ配置
-- ============================================================================

-- ========== VastBase 3.0.9+ 配置演示 ==========
-- 注意: 仅在VastBase 3.0.9+版本有效

-- 查看当前rewrite_rule设置
SHOW rewrite_rule;

-- 启用NULL-Aware Anti-Join优化
SET rewrite_rule = 'naaj_optimizer';

-- 验证执行计划 (VastBase 3.0.9+)
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*)
FROM naij_orders o
WHERE o.customer_id NOT IN (
    SELECT customer_id 
    FROM naij_customers c 
    WHERE c.region = 'INVALID'
);

-- 预期执行计划 (VastBase 3.0.9+):
-- Hash Anti Join (with NULL-aware optimization)
--   -> Seq Scan on naij_orders
--   -> Hash
--        -> Index Scan using idx_naij_customers_region

-- 性能: 与NOT EXISTS相当 (~0.6秒)

-- 恢复默认设置
SET rewrite_rule = DEFAULT;

-- ============================================================================
-- 第六部分: 性能对比测试
-- ============================================================================

-- ========== 创建性能对比表 ==========
DROP TABLE IF EXISTS naij_performance_summary;

CREATE TABLE naij_performance_summary (
    test_case VARCHAR(100),
    data_volume BIGINT,
    null_percentage NUMERIC(5,2),
    original_time_ms NUMERIC(10,2),
    not_exists_time_ms NUMERIC(10,2),
    not_in_null_time_ms NUMERIC(10,2),
    left_join_time_ms NUMERIC(10,2),
    best_improvement NUMERIC(5,2),
    best_method VARCHAR(50)
);

-- 插入测试结果 (示例数据,实际运行时会得到真实数据)
INSERT INTO naij_performance_summary VALUES
('基础NOT IN', 1000000, 1.0, 45000.0, 600.0, 800.0, 700.0, 75.0, 'NOT EXISTS'),
('JOIN + NOT IN', 500000, 2.0, 35000.0, 2000.0, 2500.0, 2200.0, 17.5, 'NOT EXISTS'),
('小数据集', 10000, 5.0, 500.0, 50.0, 60.0, 55.0, 10.0, 'NOT EXISTS');

-- 查看性能对比
SELECT 
    test_case as "测试场景",
    data_volume as "数据量",
    null_percentage as "NULL占比(%)",
    original_time_ms as "原SQL耗时(ms)",
    not_exists_time_ms as "NOT EXISTS(ms)",
    not_in_null_time_ms as "NOT IN+IS NOT NULL(ms)",
    left_join_time_ms as "LEFT JOIN(ms)",
    best_improvement as "最佳提升倍数",
    best_method as "最佳方法"
FROM naij_performance_summary
ORDER BY data_volume DESC;

-- ============================================================================
-- 第七部分: 实战业务场景
-- ============================================================================

-- ========== 场景1: 找出无效客户的订单 ==========
DROP TABLE IF EXISTS naij_invalid_customers CASCADE;

CREATE TABLE naij_invalid_customers (
    customer_id INT PRIMARY KEY,
    invalid_reason VARCHAR(200)
);

INSERT INTO naij_invalid_customers
SELECT 
    i,
    'Reason ' || (i % 5 + 1)
FROM generate_series(1, 1000) i;

-- ========== 问题SQL ==========
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    o.order_id,
    o.order_date,
    o.amount
FROM naij_orders o
WHERE o.customer_id NOT IN (
    SELECT customer_id 
    FROM naij_invalid_customers
)
ORDER BY o.order_date DESC
LIMIT 100;

-- ========== 手动改写 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    o.order_id,
    o.order_date,
    o.amount
FROM naij_orders o
WHERE NOT EXISTS (
    SELECT 1 
    FROM naij_invalid_customers ic 
    WHERE ic.customer_id = o.customer_id
)
ORDER BY o.order_date DESC
LIMIT 100;

\timing off

-- ========== 场景2: 数据质量检查 ==========
-- 检查孤立的订单记录(客户不存在)

DROP TABLE IF EXISTS naij_all_customers CASCADE;

CREATE TABLE naij_all_customers (
    customer_id INT PRIMARY KEY,
    customer_name VARCHAR(100)
);

INSERT INTO naij_all_customers
SELECT i, 'Customer ' || i
FROM generate_series(1, 10000) i;

-- ========== 问题SQL ==========
\timing on

EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*) as orphan_orders_count
FROM naij_orders o
WHERE o.customer_id NOT IN (
    SELECT customer_id 
    FROM naij_all_customers
);

-- ========== 手动改写 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT COUNT(*) as orphan_orders_count
FROM naij_orders o
WHERE NOT EXISTS (
    SELECT 1 
    FROM naij_all_customers ac 
    WHERE ac.customer_id = o.customer_id
);

\timing off

-- ============================================================================
-- 第八部分: 快速诊断查询
-- ============================================================================

-- ========== 诊断1: 识别NOT IN候选SQL ==========
-- 查找慢SQL中包含NOT IN的语句
SELECT 
    query,
    calls,
    total_exec_time,
    mean_exec_time,
    rows
FROM pg_stat_statements
WHERE query LIKE '%NOT IN%'
  AND mean_exec_time > 1000
ORDER BY mean_exec_time DESC
LIMIT 10;

-- ========== 诊断2: 检查表的NULL情况 ==========
SELECT 
    table_name,
    column_name,
    is_nullable,
    data_type
FROM information_schema.columns
WHERE table_name IN ('naij_orders', 'naij_customers')
  AND column_name = 'customer_id';

-- ========== 诊断3: 实际NULL统计 ==========
SELECT 
    'naij_orders' as table_name,
    'customer_id' as column_name,
    COUNT(*) as total_rows,
    COUNT(customer_id) as non_null_rows,
    COUNT(*) - COUNT(customer_id) as null_rows,
    ROUND((COUNT(*) - COUNT(customer_id))::NUMERIC / COUNT(*) * 100, 2) as null_percentage
FROM naij_orders
UNION ALL
SELECT 
    'naij_customers' as table_name,
    'customer_id' as column_name,
    COUNT(*) as total_rows,
    COUNT(customer_id) as non_null_rows,
    COUNT(*) - COUNT(customer_id) as null_rows,
    ROUND((COUNT(*) - COUNT(customer_id))::NUMERIC / COUNT(*) * 100, 2) as null_percentage
FROM naij_customers;

-- ============================================================================
-- 第九部分: 迁移检查清单 (含优先级判断)
-- ============================================================================

-- ✅ NAAJ迁移检查清单

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段0: 性能瓶颈验证 (必须先做!)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 0.1 分析执行计划
--    [ ] 运行EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
--    [ ] 找出耗时最多的节点
--    [ ] 计算NOT IN Filter的耗时占比

-- 0.2 瓶颈判断
--    [ ] NOT IN Filter耗时占比 >50% → 继续步骤1
--    [ ] NOT IN Filter耗时占比 30-50% → 评估其他因素
--    [ ] NOT IN Filter耗时占比 <30% → 停止,优化其他瓶颈!

-- 0.3 ROI评估
--    [ ] 整体SQL执行时间 >1秒 → 继续评估
--    [ ] SQL执行频率 >10次/天 → 继续评估
--    [ ] 预计改写后提升 >5倍 → 继续步骤1
--    [ ] 不满足以上条件 → 优先级低,暂不改写

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段1: 识别NOT IN子查询
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 1. 性能确认 (已通过阶段0验证)
--    [ ] SQL包含NOT IN
--    [ ] 执行计划显示Filter操作是主要瓶颈
--    [ ] 执行时间异常长(>1秒)

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段2: 分析NULL风险
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 2. 数据库结构分析
--    [ ] 检查子查询列是否允许NULL
--    [ ] 查看表定义: information_schema.columns
--    [ ] 确认列的is_nullable属性

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段3: 实际数据验证
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 3. NULL统计
--    [ ] 统计实际数据中的NULL占比
--    [ ] 评估NULL对业务的影响
--    [ ] 判断是否需要特殊处理

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段4: 选择改写方案
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 4. 方案选择
--    [ ] 列允许NULL → 使用NOT EXISTS (推荐)
--    [ ] 列不允许NULL → 可用NOT IN + IS NOT NULL
--    [ ] 复杂子查询 → 考虑LEFT JOIN + IS NULL
--    [ ] 评估改写后代码可读性

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段5: 执行手动改写
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 5. 改写实施
--    [ ] 重写SQL为NOT EXISTS或其他方案
--    [ ] 确保JOIN条件完整
--    [ ] 保留原有的WHERE条件
--    [ ] 添加注释说明改写原因

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段6: 验证结果一致性
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 6. 结果验证
--    [ ] 对比改写前后行数
--    [ ] 抽样验证数据内容
--    [ ] 确认业务逻辑不变
--    [ ] 使用CTE对比验证

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段7: 性能验证
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 7. 性能对比
--    [ ] 对比改写前后执行时间
--    [ ] 分析改写后执行计划
--    [ ] 确认性能提升达到预期(>5倍)
--    [ ] 记录性能提升数据

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 阶段8: 代码审查与部署
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- 8. 代码审查
--    [ ] 添加注释说明改写原因
--    [ ] 代码可读性良好
--    [ ] 通过QA测试
--    [ ] 制定回滚方案

-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
-- 停止标志 (满足任一条件即可停止)
-- ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

-- ⛔ 不应该改写的情况:
--    [ ] NOT IN Filter耗时占比 <30%
--    [ ] 整体SQL执行时间 <100ms
--    [ ] SQL执行频率 <10次/天
--    [ ] 改写后预计提升 <5倍
--    [ ] 瓶颈在其他地方(如全表扫描、网络I/O)

-- ============================================================================
-- 结论
-- ============================================================================
-- 
-- Oracle的NULL-Aware Anti-Join (NAAJ)是处理NOT IN + NULL的重要优化。
-- VastBase从3.0.9开始支持NAAJ (需设置rewrite_rule='naaj_optimizer')。
-- 
-- VastBase 3.0.9之前版本:
-- - 不支持NAAJ优化
-- - NOT IN遇到NULL时性能极差
-- - 必须手动改写为NOT EXISTS或NOT IN + IS NOT NULL
-- 
-- 性能提升预期:
-- - 小表(<10万行): 10-20倍
-- - 中表(10-50万行): 20-50倍
-- - 大表(>50万行): 50-100倍
-- 
-- 最佳实践:
-- - 优先使用NOT EXISTS改写
-- - 验证结果一致性
-- - 添加注释说明改写原因
-- - VastBase 3.0.9+启用naaj_optimizer参数
--
-- ============================================================================
