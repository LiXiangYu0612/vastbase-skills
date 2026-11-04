-- ============================================================================
-- VastBase PJE (Partial Join Evaluation) 改写示例集合
-- Oracle 12c+的PJE优化在VastBase中不支持,需要手动改写
-- ============================================================================

-- ============================================================================
-- 场景1: 基础PJE改写 - 高频值JOIN导致笛卡尔积
-- ============================================================================
-- 问题SQL (VastBase中性能差):
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
JOIN test_tables t1 ON t.object_type = t1.object_type
WHERE t.data_object_id IS NOT NULL
ORDER BY t.data_object_id;

-- 改写后 (手动实现PJE逻辑):
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
WHERE t.data_object_id IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM test_tables t1
    WHERE t1.object_type = t.object_type
  )
ORDER BY t.data_object_id;

-- 性能对比:
-- 原SQL: HASH JOIN → 高频值产生32M行中间结果 (假设object_type='TABLE'有5万行)
-- 改写: SEMI JOIN → 每个t行只需验证在t1中存在,避免笛卡尔积

-- ============================================================================
-- 场景2: 三表JOIN - 选择列表字段来自主表
-- ============================================================================
-- 问题SQL:
SELECT DISTINCT o.order_id, o.customer_id, o.order_date
FROM orders o
JOIN order_items oi ON o.order_id = oi.order_id
JOIN products p ON oi.product_id = p.product_id
WHERE p.category_id = :cat_id
  AND o.status = 'ACTIVE';

-- 改写后 (PJE手动实现):
SELECT DISTINCT o.order_id, o.customer_id, o.order_date
FROM orders o
WHERE o.status = 'ACTIVE'
  AND EXISTS (
    SELECT 1
    FROM order_items oi
    JOIN products p ON oi.product_id = p.product_id
    WHERE oi.order_id = o.order_id
      AND p.category_id = :cat_id
  );

-- 关键点:
-- 1. SELECT字段全部来自orders表 (满足PJE条件2)
-- 2. 有DISTINCT (满足PJE条件3)
-- 3. 多表JOIN (满足PJE条件1)
-- 4. 改写后用EXISTS避免高频category_id导致的笛卡尔积

-- ============================================================================
-- 场景3: LEFT JOIN变体 - PJE适用场景
-- ============================================================================
-- 问题SQL (某些高频值导致大量重复):
SELECT DISTINCT c.customer_id, c.customer_name
FROM customers c
LEFT JOIN orders o ON c.customer_id = o.customer_id
WHERE c.region = :region
  AND o.order_date >= :date_from;

-- 改写后:
SELECT DISTINCT c.customer_id, c.customer_name
FROM customers c
WHERE c.region = :region
  AND EXISTS (
    SELECT 1
    FROM orders o
    WHERE o.customer_id = c.customer_id
      AND o.order_date >= :date_from
  );

-- 性能提升原因:
-- 如果某客户有1000个订单,原SQL会产生1000行,DISTINCT去重
-- 改写后EXISTS只需验证存在,避免产生1000行中间结果

-- ============================================================================
-- 场景4: 带聚合的PJE变体 (需要特殊处理)
-- ============================================================================
-- 问题SQL:
SELECT DISTINCT t.owner, t.object_name
FROM test_tables t
JOIN test_tables t1 ON t.object_type = t1.object_type
WHERE t.table_name LIKE 'T_%';

-- 改写后:
SELECT t.owner, t.object_name
FROM test_tables t
WHERE t.table_name LIKE 'T_%'
  AND EXISTS (
    SELECT 1
    FROM test_tables t1
    WHERE t1.object_type = t.object_type
  )
GROUP BY t.owner, t.object_name;  -- 用GROUP BY代替DISTINCT

-- ============================================================================
-- 场景5: 嵌套视图中的PJE机会
-- ============================================================================
-- 原始问题SQL (包含视图):
SELECT DISTINCT v.emp_id, v.emp_name
FROM emp_dept_view v
JOIN salary_history s ON v.emp_id = s.emp_id
WHERE s.salary > 10000;

-- 如果视图定义为: CREATE VIEW emp_dept_view AS SELECT * FROM emp e JOIN dept d ON e.dept_id = d.dept_id

-- 改写策略1 - 展开视图:
SELECT DISTINCT e.emp_id, e.emp_name
FROM emp e
JOIN dept d ON e.dept_id = d.dept_id
WHERE EXISTS (
  SELECT 1
  FROM salary_history s
  WHERE s.emp_id = e.emp_id
    AND s.salary > 10000
);

-- 改写策略2 - 保持视图结构:
SELECT DISTINCT v.emp_id, v.emp_name
FROM emp_dept_view v
WHERE EXISTS (
  SELECT 1
  FROM salary_history s
  WHERE s.emp_id = v.emp_id
    AND s.salary > 10000
);

-- ============================================================================
-- 场景6: 多DISTINCT列的PJE
-- ============================================================================
-- 问题SQL:
SELECT DISTINCT t.col1, t.col2, t.col3, t.col4
FROM table_a t
JOIN table_b t1 ON t.id = t1.id
JOIN table_c t2 ON t.type = t2.type
WHERE t1.status = 'ACTIVE'
  AND t2.flag = 1;

-- 改写后:
SELECT DISTINCT t.col1, t.col2, t.col3, t.col4
FROM table_a t
WHERE EXISTS (
  SELECT 1
  FROM table_b t1
  WHERE t1.id = t.id
    AND t1.status = 'ACTIVE'
)
AND EXISTS (
  SELECT 1
  FROM table_c t2
  WHERE t2.type = t.type
    AND t2.flag = 1
);

-- ============================================================================
-- 性能验证SQL - 测试PJE改写效果
-- ============================================================================
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
JOIN test_tables t1 ON t.object_type = t1.object_type
WHERE t.data_object_id IS NOT NULL;

-- 预期问题:
-- Hash Join
-- -> Seq Scan on test_tables t (实际行数: 8995)
-- -> Hash
--    -> Seq Scan on test_tables t1 (实际行数: 86730)
-- 问题: 如果object_type='TABLE'是高频值,JOIN产生大量中间行

EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
WHERE t.data_object_id IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM test_tables t1
    WHERE t1.object_type = t.object_type
  );

-- 预期优化:
-- Hash Semi Join
-- -> Seq Scan on test_tables t
-- -> Hash
--    -> Seq Scan on test_tables t1
-- 优势: Semi Join避免笛卡尔积,每个t行只需验证在t1中存在

-- ============================================================================
-- 数据倾斜检测 - 识别PJE候选SQL
-- ============================================================================
-- 检测JOIN列的数据分布:
SELECT 
    t1.object_type,
    COUNT(*) as cnt,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 2) as percentage
FROM test_tables t1
GROUP BY t1.object_type
ORDER BY cnt DESC;

-- 如果某值占比超过20%,且该列用于JOIN + SELECT DISTINCT + SELECT字段全来自一个表
-- 则是PJE改写的理想候选

-- ============================================================================
-- NOT IN vs NOT EXISTS 的PJE变体
-- ============================================================================
-- 问题SQL (anti-join场景):
SELECT DISTINCT t.owner, t.table_name
FROM test_tables t
JOIN test_tables t1 ON t.owner = t1.owner
WHERE t.table_name NOT IN (
  SELECT t2.table_name
  FROM test_tables_part t2
  WHERE t2.partitioned = 'YES'
);

-- 改写为NOT EXISTS:
SELECT DISTINCT t.owner, t.table_name
FROM test_tables t
WHERE NOT EXISTS (
  SELECT 1
  FROM test_tables_part t2
  WHERE t2.table_name = t.table_name
    AND t2.partitioned = 'YES'
)
AND EXISTS (
  SELECT 1
  FROM test_tables t1
  WHERE t1.owner = t.owner
);

-- ============================================================================
-- 注意事项和限制
-- ============================================================================
-- 1. PJE改写仅适用于SELECT字段全部来自一个表的场景
-- 2. 如果SELECT包含JOIN表的字段,不能改写为EXISTS
-- 3. EXISTS子查询必须关联主表
-- 4. 确保统计信息准确,优化器才能选择最佳计划
-- 5. 对于小数据量,原SQL可能更快(改写有开销)

-- 错误改写示例:
-- ❌ SELECT包含t1的字段,不能PJE改写
SELECT DISTINCT t.col1, t1.col2  -- 包含t1的字段
FROM table_a t
JOIN table_b t1 ON t.id = t1.id;

-- ✅ 正确做法: 保持原SQL或调整业务逻辑

-- ============================================================================
-- 批量检测PJE机会的SQL
-- ============================================================================
-- 查找VastBase中可能需要PJE改写的SQL (从历史SQL中)
-- 条件:
-- 1. 包含DISTINCT
-- 2. 包含JOIN
-- 3. JOIN条件列存在数据倾斜
-- 4. SELECT列表字段来自同一表

-- 示例查询 (需要结合实际SQL审计表):
SELECT 
    query_text,
    query_id,
    calls,
    total_time,
    mean_time
FROM pg_stat_statements
WHERE query_text LIKE '%DISTINCT%'
  AND query_text LIKE '%JOIN%'
  AND query_text NOT LIKE '%EXISTS%'
ORDER BY mean_time DESC
LIMIT 20;

-- ============================================================================
-- 总结
-- ============================================================================
-- Oracle PJE自动处理的场景,在VastBase中需要手动改写:
-- 1. 识别: 多表JOIN + DISTINCT + SELECT字段全来自一个表
-- 2. 改写: 将JOIN部分转换为EXISTS子查询
-- 3. 验证: 对比执行计划和时间,确保性能提升
-- 4. 测试: 在生产镜像环境中充分测试
