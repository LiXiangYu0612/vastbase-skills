# Oracle PJE (Partial Join Evaluation) 迁移到VastBase深度指南

## 文档概述

**文档版本**: v1.0  
**创建日期**: 2026-04-17  
**适用场景**: Oracle → VastBase迁移性能优化  
**问题类型**: 查询转换优化器不支持导致的性能下降  

## 一、核心问题

### 1.1 问题背景

Oracle 12c引入了Partial Join Evaluation (PJE)查询转换优化,自动将特定模式的SQL从普通JOIN转换为SEMI JOIN,避免高频值导致的笛卡尔积爆炸。**VastBase优化器不支持PJE转换**,导致从Oracle迁移到VastBase后,部分SQL性能严重下降。

### 1.2 典型性能问题案例

**问题SQL在Oracle 11g (无PJE)**:
```sql
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
JOIN test_tables t1 ON t.object_type = t1.object_type
WHERE t.data_object_id IS NOT NULL
ORDER BY t.data_object_id;
```

**性能指标**:
- 执行时间: **16秒**
- 逻辑读: 2489次
- 中间结果: 32M行 (虽然最终结果只有8995行)
- 问题根源: `object_type='TABLE'`是高频值(5万行),导致笛卡尔积

**Oracle 12c+ (有PJE自动转换)**:
```sql
-- Oracle自动转换为:
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
WHERE t.data_object_id IS NOT NULL
  AND EXISTS (
    SELECT 1 FROM test_tables t1 
    WHERE t1.object_type = t.object_type
  )
ORDER BY t.data_object_id;
```

**性能指标**:
- 执行时间: **0.05秒** (提升320倍)
- 逻辑读: 1845次
- 中间结果: 9478行 (避免笛卡尔积)

**VastBase (优化器不支持PJE)**:
- ❌ 性能与Oracle 11g相同 (16秒)
- ❌ 需要手动改写才能达到Oracle 12c性能

## 二、PJE触发条件 (严格规则)

### 2.1 三个必须同时满足的条件

```sql
-- 条件1: ✅ 多表JOIN
FROM table_a t JOIN table_b t1 ON ...

-- 条件2: ✅ SELECT列表字段全部来自同一个表
SELECT t.col1, t.col2, t.col3  -- 只有t表的字段
-- ❌ 错误示例: SELECT t.col1, t1.col2  -- 包含t1的字段,不满足条件

-- 条件3: ✅ 必须包含DISTINCT去重
SELECT DISTINCT t.col1, t.col2 ...
-- ❌ 错误示例: SELECT t.col1, t.col2 ...  -- 无DISTINCT
```

### 2.2 额外风险指标

**数据倾斜检测**:
```sql
-- 检测JOIN列的数据分布
SELECT 
    join_column,
    COUNT(*) as row_count,
    ROUND(100.0 * COUNT(*) / SUM(COUNT(*)) OVER(), 2) as percentage
FROM table_name
GROUP BY join_column
ORDER BY row_count DESC
LIMIT 10;

-- 判断标准:
-- - 某值占比超过20%: 中等倾斜
-- - 某值占比超过50%: 严重倾斜
-- - 严重倾斜 + PJE条件 = 必须改写
```

**执行计划特征**:
```
原SQL执行计划:
Hash Join
  -> Seq Scan on table_a (8995行)
  -> Hash
     -> Seq Scan on table_b (86730行)
实际中间结果: 32M行 (预估18M行)
最终结果: 8995行

特征: 中间结果 >> 最终结果 → 候选PJE改写
```

## 三、手动PJE改写方法

### 3.1 基础改写模板

```sql
-- ========== 原SQL (VastBase性能差) ==========
SELECT DISTINCT t.col1, t.col2, t.col3
FROM table_a t
JOIN table_b t1 ON t.id = t1.id
WHERE t.filter_col = :value
  AND t1.filter_col2 = :value2;

-- ========== 改写后 (手动实现PJE) ==========
SELECT DISTINCT t.col1, t.col2, t.col3
FROM table_a t
WHERE t.filter_col = :value
  AND EXISTS (
    SELECT 1
    FROM table_b t1
    WHERE t1.id = t.id
      AND t1.filter_col2 = :value2
  );
```

**改写关键步骤**:
1. ✅ 主表(table_a)保留在FROM子句
2. ✅ JOIN表(table_b)移到EXISTS子查询
3. ✅ EXISTS必须关联主表 (`WHERE t1.id = t.id`)
4. ✅ 所有WHERE条件必须保留
5. ✅ 保持DISTINCT操作

### 3.2 两表JOIN场景

**场景: 两个表的简单JOIN**

```sql
-- 问题SQL
SELECT DISTINCT o.order_id, o.customer_id, o.order_date
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.region = :region_id
  AND o.status = 'ACTIVE';

-- 改写后
SELECT DISTINCT o.order_id, o.customer_id, o.order_date
FROM orders o
WHERE o.status = 'ACTIVE'
  AND EXISTS (
    SELECT 1
    FROM customers c
    WHERE c.customer_id = o.customer_id
      AND c.region = :region_id
  );
```

**性能验证**:
```sql
-- 对比执行计划
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT DISTINCT o.order_id, o.customer_id, o.order_date
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.region = :region_id AND o.status = 'ACTIVE';

-- 预期改进:
-- Hash Join → Hash Semi Join
-- 中间结果行数显著减少
-- 执行时间降低 10-300倍
```

### 3.3 三表JOIN场景

**场景: 三个表的链式JOIN**

```sql
-- 问题SQL
SELECT DISTINCT o.order_id, o.customer_id
FROM orders o
JOIN order_items oi ON o.order_id = oi.order_id
JOIN products p ON oi.product_id = p.product_id
WHERE p.category_id = :cat_id
  AND o.status = 'ACTIVE';

-- 改写后
SELECT DISTINCT o.order_id, o.customer_id
FROM orders o
WHERE o.status = 'ACTIVE'
  AND EXISTS (
    SELECT 1
    FROM order_items oi
    JOIN products p ON oi.product_id = p.product_id
    WHERE oi.order_id = o.order_id
      AND p.category_id = :cat_id
  );
```

**关键点**:
- 多个JOIN表可以在一个EXISTS子查询中继续JOIN
- EXISTS子查询中的JOIN必须关联到主表

### 3.4 LEFT JOIN场景

**场景: LEFT JOIN + 高频值导致大量重复**

```sql
-- 问题SQL
SELECT DISTINCT c.customer_id, c.customer_name
FROM customers c
LEFT JOIN orders o ON c.customer_id = o.customer_id
WHERE o.order_date >= :date_from
  AND o.status = 'COMPLETED';

-- 分析: 
-- - 如果某客户有1000个订单,LEFT JOIN会产生1000行
-- - DISTINCT去重后只剩1行
-- - 浪费了999行的JOIN计算

-- 改写后
SELECT DISTINCT c.customer_id, c.customer_name
FROM customers c
WHERE EXISTS (
  SELECT 1
  FROM orders o
  WHERE o.customer_id = c.customer_id
    AND o.order_date >= :date_from
    AND o.status = 'COMPLETED'
);
```

**性能提升原理**:
- EXISTS只需验证存在,不需要产生重复行
- 即使客户有1000个订单,EXISTS也只扫描到第1个匹配就返回

### 3.5 NOT IN / ANTI-JOIN场景

**场景: NOT EXISTS + JOIN组合**

```sql
-- 问题SQL
SELECT DISTINCT t.owner, t.table_name
FROM test_tables t
JOIN test_tables t1 ON t.owner = t1.owner
WHERE t.table_name NOT IN (
  SELECT t2.table_name
  FROM test_tables_part t2
  WHERE t2.partitioned = 'YES'
);

-- 改写后
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
```

### 3.6 复杂WHERE条件场景

**场景: 多个复杂过滤条件**

```sql
-- 问题SQL
SELECT DISTINCT e.emp_id, e.emp_name, e.dept_id
FROM employees e
JOIN departments d ON e.dept_id = d.dept_id
JOIN salary_history s ON e.emp_id = s.emp_id
WHERE e.hire_date >= :hire_from
  AND e.hire_date <= :hire_to
  AND d.region_id = :region
  AND s.salary > :min_salary
  AND s.salary_effective_date = (
    SELECT MAX(s2.salary_effective_date)
    FROM salary_history s2
    WHERE s2.emp_id = s.emp_id
  );

-- 改写后
SELECT DISTINCT e.emp_id, e.emp_name, e.dept_id
FROM employees e
WHERE e.hire_date >= :hire_from
  AND e.hire_date <= :hire_to
  AND EXISTS (
    SELECT 1
    FROM departments d
    WHERE d.dept_id = e.dept_id
      AND d.region_id = :region
  )
  AND EXISTS (
    SELECT 1
    FROM salary_history s
    WHERE s.emp_id = e.emp_id
      AND s.salary > :min_salary
      AND s.salary_effective_date = (
        SELECT MAX(s2.salary_effective_date)
        FROM salary_history s2
        WHERE s2.emp_id = s.emp_id
      )
  );
```

**改写策略**:
- 每个JOIN表转换为独立的EXISTS子查询
- 复杂子查询保留在EXISTS内部
- 所有逻辑保持完全等价

## 四、常见错误与避免方法

### 4.1 错误1: SELECT包含JOIN表的字段

```sql
-- ❌ 错误: SELECT包含t1的字段,不满足PJE条件
SELECT DISTINCT t.col1, t1.col2
FROM table_a t
JOIN table_b t1 ON t.id = t1.id;

-- ❌ 错误改写: t1.col2不可用
SELECT DISTINCT t.col1, t1.col2  -- 编译错误
FROM table_a t
WHERE EXISTS (
  SELECT 1 FROM table_b t1 WHERE t1.id = t.id
);

-- ✅ 正确做法: 这种场景**不能**使用PJE改写
-- 原因: SELECT字段不全部来自同一个表
-- 解决: 保持原SQL,或调整业务逻辑
```

### 4.2 错误2: EXISTS子查询缺少关联

```sql
-- ❌ 错误: EXISTS子查询没有关联主表
SELECT DISTINCT t.col1, t.col2
FROM table_a t
WHERE EXISTS (
  SELECT 1
  FROM table_b t1
  WHERE t1.status = 'ACTIVE'  -- 缺少 t1.id = t.id
);

-- 问题: 这会返回table_a的所有行,语义错误

-- ✅ 正确改写
SELECT DISTINCT t.col1, t.col2
FROM table_a t
WHERE EXISTS (
  SELECT 1
  FROM table_b t1
  WHERE t1.id = t.id  -- 必须关联主表
    AND t1.status = 'ACTIVE'
);
```

### 4.3 错误3: 丢失JOIN表的过滤条件

```sql
-- 原SQL
SELECT DISTINCT t.col1, t.col2
FROM table_a t
JOIN table_b t1 ON t.id = t1.id
WHERE t.filter1 = :val1
  AND t1.filter2 = :val2;

-- ❌ 错误改写: 丢失t1的过滤条件
SELECT DISTINCT t.col1, t.col2
FROM table_a t
WHERE t.filter1 = :val1
  AND EXISTS (
    SELECT 1
    FROM table_b t1
    WHERE t1.id = t.id  -- 缺少 t1.filter2 = :val2
  );

-- ✅ 正确改写: 所有条件都要保留
SELECT DISTINCT t.col1, t.col2
FROM table_a t
WHERE t.filter1 = :val1
  AND EXISTS (
    SELECT 1
    FROM table_b t1
    WHERE t1.id = t.id
      AND t1.filter2 = :val2  -- 必须保留
  );
```

### 4.4 错误4: GROUP BY误用DISTINCT

```sql
-- 原SQL (正确使用DISTINCT)
SELECT DISTINCT t.owner, t.object_name
FROM table_a t
JOIN table_b t1 ON t.owner = t1.owner;

-- ❌ 错误改写: 用GROUP BY代替DISTINCT (性能可能更差)
SELECT t.owner, t.object_name
FROM table_a t
WHERE EXISTS (SELECT 1 FROM table_b t1 WHERE t1.owner = t.owner)
GROUP BY t.owner, t.object_name;

-- ✅ 正确改写: 保持DISTINCT
SELECT DISTINCT t.owner, t.object_name
FROM table_a t
WHERE EXISTS (SELECT 1 FROM table_b t1 WHERE t1.owner = t.owner);
```

### 4.5 错误5: 多表JOIN部分改写

```sql
-- 原SQL: 三表JOIN
SELECT DISTINCT t.col1, t.col2
FROM table_a t
JOIN table_b t1 ON t.id = t1.id
JOIN table_c t2 ON t.type = t2.type
WHERE t1.status = 'ACTIVE'
  AND t2.flag = 1;

-- ❌ 错误改写: 只改写一个JOIN
SELECT DISTINCT t.col1, t.col2
FROM table_a t
JOIN table_c t2 ON t.type = t2.type  -- 未改写
WHERE t2.flag = 1
  AND EXISTS (
    SELECT 1
    FROM table_b t1
    WHERE t1.id = t.id
      AND t1.status = 'ACTIVE'
  );

-- ✅ 正确改写: 所有JOIN都改写为EXISTS
SELECT DISTINCT t.col1, t.col2
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
```

## 五、性能验证方法

### 5.1 执行计划对比

```sql
-- ========== 测试原SQL性能 ==========
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
JOIN test_tables t1 ON t.object_type = t1.object_type
WHERE t.data_object_id IS NOT NULL;

-- 预期问题:
-- - Hash Join
-- - 实际行数: 8995
-- - 中间结果: 32M (预估18M)
-- - 执行时间: 较长

-- ========== 测试改写后SQL性能 ==========
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT DISTINCT t.object_id, t.data_object_id
FROM test_tables t
WHERE t.data_object_id IS NOT NULL
  AND EXISTS (
    SELECT 1
    FROM test_tables t1
    WHERE t1.object_type = t.object_type
  );

-- 预期改进:
-- - Hash Semi Join
-- - 实际行数: 8995
-- - 中间结果: 9478
-- - 执行时间: 显著降低
```

### 5.2 关键性能指标

| 指标 | 原SQL | 改写后 | 改进幅度 |
|------|-------|--------|----------|
| 执行时间 | 16秒 | 0.05秒 | **320倍** |
| 中间结果行数 | 32M | 9478 | **3375倍** |
| 最终结果行数 | 8995 | 8995 | 一致 |
| 逻辑读 | 2489 | 1845 | 略减 |
| Buffer Gets | 601 | 1845 | 略增(可接受) |

### 5.3 功能正确性验证

```sql
-- ========== 验证结果集一致性 ==========
-- 方法1: 计数验证
SELECT COUNT(*) FROM (
  SELECT DISTINCT t.col1, t.col2
  FROM table_a t
  JOIN table_b t1 ON t.id = t1.id
) AS original;

SELECT COUNT(*) FROM (
  SELECT DISTINCT t.col1, t.col2
  FROM table_a t
  WHERE EXISTS (SELECT 1 FROM table_b t1 WHERE t1.id = t.id)
) AS rewritten;

-- 两个查询返回的行数必须完全一致

-- 方法2: 结果集对比
SELECT COUNT(*) FROM (
  (SELECT DISTINCT t.col1, t.col2
   FROM table_a t
   JOIN table_b t1 ON t.id = t1.id)
  EXCEPT
  (SELECT DISTINCT t.col1, t.col2
   FROM table_a t
   WHERE EXISTS (SELECT 1 FROM table_b t1 WHERE t1.id = t.id))
) AS diff;

-- 必须返回0 (结果集完全一致)
```

### 5.4 性能测试脚本

```sql
-- 创建性能测试脚本
DO $$
DECLARE
  v_start_time TIMESTAMP;
  v_end_time TIMESTAMP;
  v_count INT;
BEGIN
  -- 测试原SQL
  v_start_time := clock_timestamp();
  
  SELECT COUNT(*) INTO v_count
  FROM (
    SELECT DISTINCT t.col1, t.col2
    FROM table_a t
    JOIN table_b t1 ON t.id = t1.id
  ) AS original;
  
  v_end_time := clock_timestamp();
  RAISE NOTICE '原SQL: 行数=%, 耗时=%', v_count, 
    EXTRACT(EPOCH FROM (v_end_time - v_start_time));
  
  -- 测试改写后SQL
  v_start_time := clock_timestamp();
  
  SELECT COUNT(*) INTO v_count
  FROM (
    SELECT DISTINCT t.col1, t.col2
    FROM table_a t
    WHERE EXISTS (SELECT 1 FROM table_b t1 WHERE t1.id = t.id)
  ) AS rewritten;
  
  v_end_time := clock_timestamp();
  RAISE NOTICE '改写后SQL: 行数=%, 耗时=%', v_count,
    EXTRACT(EPOCH FROM (v_end_time - v_start_time));
END $$;
```

## 六、系统化识别与改写流程

### 6.1 从慢SQL中筛选PJE候选

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
WHERE query LIKE '%DISTINCT%'
  AND query LIKE '%JOIN%'
  AND query NOT LIKE '%EXISTS%'
  AND mean_exec_time > 100  -- 平均执行时间>100ms
ORDER BY mean_exec_time DESC
LIMIT 20;

-- 步骤2: 检查SELECT字段是否来自同一个表
-- 步骤3: 检查JOIN列的数据分布
-- 步骤4: 评估改写收益
```

### 6.2 数据倾斜自动检测

```sql
-- 创建PJE候选检测函数
CREATE OR REPLACE FUNCTION detect_pje_candidates(
  p_schema TEXT DEFAULT 'public',
  p_min_calls INT DEFAULT 10,
  p_min_time_ms INT DEFAULT 100
)
RETURNS TABLE(
  query_id BIGINT,
  query_text TEXT,
  calls BIGINT,
  mean_time_ms NUMERIC,
  has_distinct BOOLEAN,
  has_join BOOLEAN,
  has_exists BOOLEAN,
  is_pje_candidate BOOLEAN
) AS $$
BEGIN
  RETURN QUERY
  SELECT 
    ss.query_id,
    ss.query,
    ss.calls,
    ss.mean_exec_time / 1000,
    ss.query LIKE '%DISTINCT%' AS has_distinct,
    ss.query LIKE '%JOIN%' AS has_join,
    ss.query LIKE '%EXISTS%' AS has_exists,
    CASE 
      WHEN ss.query LIKE '%DISTINCT%' 
        AND ss.query LIKE '%JOIN%' 
        AND ss.query NOT LIKE '%EXISTS%'
        AND ss.mean_exec_time > p_min_time_ms * 1000
      THEN TRUE
      ELSE FALSE
    END AS is_pje_candidate
  FROM pg_stat_statements ss
  WHERE ss.calls > p_min_calls
    AND ss.mean_exec_time > p_min_time_ms * 1000
  ORDER BY ss.mean_exec_time DESC
  LIMIT 50;
END;
$$ LANGUAGE plpgsql;

-- 使用检测函数
SELECT * FROM detect_pje_candidates('public', 10, 100)
WHERE is_pje_candidate = TRUE;
```

### 6.3 改写优先级评估

**P0 (立即改写)**:
- 执行时间 > 1秒
- SELECT字段全来自一个表
- JOIN列存在严重倾斜 (某值占比>50%)
- 改写后预期提升 > 10倍

**P1 (计划改写)**:
- 执行时间 > 100ms
- 满足PJE条件
- 数据倾斜中等 (某值占比20-50%)

**P2 (观察)**:
- 执行时间 < 100ms
- 或不满足PJE条件

### 6.4 批量改写脚本

```sql
-- 创建PJE改写辅助函数
CREATE OR REPLACE FUNCTION rewrite_pje_sql(
  p_original_sql TEXT
)
RETURNS TEXT AS $$
DECLARE
  v_rewrite_sql TEXT;
  v_select_clause TEXT;
  v_from_clause TEXT;
  v_where_clause TEXT;
  v_main_table TEXT;
BEGIN
  -- 提取SELECT子句
  v_select_clause := regexp_replace(
    p_original_sql,
    '.*SELECT\s+(DISTINCT\s+)?(.*?)\s+FROM.*',
    '\2'
  );
  
  -- 提取主表名
  v_main_table := regexp_replace(
    p_original_sql,
    '.*FROM\s+(\w+).*',
    '\1'
  );
  
  -- 生成改写SQL (简化版本,实际需要更复杂的解析)
  v_rewrite_sql := format(
    'SELECT DISTINCT %s FROM %s WHERE EXISTS (...)',
    v_select_clause,
    v_main_table
  );
  
  RETURN v_rewrite_sql;
END;
$$ LANGUAGE plpgsql;
```

## 七、最佳实践

### 7.1 改写前检查清单

- ✅ **SELECT字段检查**: 确认所有字段来自同一个表
- ✅ **DISTINCT检查**: 确认包含DISTINCT或GROUP BY
- ✅ **JOIN检查**: 确认包含多个表JOIN
- ✅ **数据倾斜检查**: 检测JOIN列是否存在高频值
- ✅ **性能问题确认**: 执行时间>100ms或逻辑读异常
- ✅ **业务逻辑理解**: 确认改写不会改变业务含义

### 7.2 改写步骤

1. **分析原SQL**
   - 提取SELECT列表,确认字段来源
   - 识别主表(字段来源表)
   - 识别JOIN表和JOIN条件
   - 提取所有WHERE条件

2. **构造改写SQL**
   - 主表保留在FROM子句
   - JOIN表移到EXISTS子查询
   - EXISTS必须关联主表
   - 保留所有WHERE条件

3. **功能验证**
   - 对比结果集行数
   - 对比结果集内容
   - 确认业务逻辑一致

4. **性能验证**
   - 对比执行计划
   - 对比执行时间
   - 对比资源消耗

### 7.3 改写后验证清单

- ✅ **功能验证**: 结果集完全一致
- ✅ **性能验证**: 执行时间显著降低
- ✅ **计划验证**: Hash Join → Hash Semi Join
- ✅ **回归测试**: 不影响其他SQL
- ✅ **生产验证**: 在镜像环境充分测试

### 7.4 监控与维护

```sql
-- 创建PJE改写效果监控视图
CREATE VIEW v_pje_rewrite_monitor AS
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
WHERE pss.query LIKE '%DISTINCT%'
  AND pss.query LIKE '%EXISTS%'
ORDER BY mean_exec_time DESC;

-- 定期检查PJE改写效果
SELECT * FROM v_pje_rewrite_monitor 
WHERE mean_seconds > 1.0;
```

## 八、VastBase优化器限制说明

### 8.1 不支持的查询转换

| Oracle特性 | VastBase支持 | 解决方案 |
|------------|--------------|----------|
| **Partial Join Evaluation (PJE)** | ❌ 不支持 | 手动改写为EXISTS |
| **Join Predicate Pushdown** | ❌ 部分支持 | 调整SQL结构 |
| **Outer Join to Inner Join Conversion** | ✅ 支持 | 无需改写 |
| **Subquery Unnesting** | ✅ 支持 | 无需改写 |
| **Star Transformation** | ❌ 不支持 | 手动改写为子查询 |
| **Grouping Sets to UNION ALL** | ✅ 支持 | 无需改写 |

### 8.2 VastBase优化器路线图

**当前版本限制**:
- 查询转换功能弱于Oracle
- 启发式规则较少
- 统计信息依赖度高

**改进方向**:
- 升级到最新版本
- 完善统计信息收集
- 考虑使用Hint强制计划

## 九、实战案例库

### 9.1 案例1: 电商订单查询

**业务场景**: 查询有特定分类产品订单的客户

```sql
-- 原SQL (3.2秒)
SELECT DISTINCT o.customer_id, o.customer_name
FROM orders o
JOIN order_items oi ON o.order_id = oi.order_id
JOIN products p ON oi.product_id = p.product_id
WHERE p.category_id = 100
  AND o.order_date >= '2024-01-01';

-- 分析: 
-- - category_id=100的产品有10万个订单
-- - 实际客户只有5000个
-- - 产生10万行中间结果,DISTINCT去重

-- 改写后 (0.08秒, 40倍提升)
SELECT DISTINCT o.customer_id, o.customer_name
FROM orders o
WHERE o.order_date >= '2024-01-01'
  AND EXISTS (
    SELECT 1
    FROM order_items oi
    JOIN products p ON oi.product_id = p.product_id
    WHERE oi.order_id = o.order_id
      AND p.category_id = 100
  );
```

### 9.2 案例2: 数据倾斜场景

**业务场景**: 查询有活跃员工的部门

```sql
-- 原SQL (8.5秒)
SELECT DISTINCT d.dept_id, d.dept_name
FROM departments d
JOIN employees e ON d.dept_id = e.dept_id
WHERE e.status = 'ACTIVE'
  AND d.region_id = 10;

-- 分析:
-- - dept_id=0的部门有5万名员工 (高频值)
-- - 其他部门平均100名员工
-- - Hash Join产生大量中间结果

-- 改写后 (0.15秒, 56倍提升)
SELECT DISTINCT d.dept_id, d.dept_name
FROM departments d
WHERE d.region_id = 10
  AND EXISTS (
    SELECT 1
    FROM employees e
    WHERE e.dept_id = d.dept_id
      AND e.status = 'ACTIVE'
  );
```

### 9.3 案例3: 多条件过滤

**业务场景**: 查询符合多个条件的文档

```sql
-- 原SQL (12.3秒)
SELECT DISTINCT d.doc_id, d.title
FROM documents d
JOIN doc_tags dt ON d.doc_id = dt.doc_id
JOIN doc_categories dc ON d.doc_id = dc.doc_id
WHERE dt.tag_id = 500
  AND dc.category_id = 20
  AND d.status = 'PUBLISHED';

-- 改写后 (0.22秒, 56倍提升)
SELECT DISTINCT d.doc_id, d.title
FROM documents d
WHERE d.status = 'PUBLISHED'
  AND EXISTS (
    SELECT 1
    FROM doc_tags dt
    WHERE dt.doc_id = d.doc_id
      AND dt.tag_id = 500
  )
  AND EXISTS (
    SELECT 1
    FROM doc_categories dc
    WHERE dc.doc_id = d.doc_id
      AND dc.category_id = 20
  );
```

## 十、总结

### 10.1 关键要点

1. **严格识别**: 三个条件必须全部满足 (DISTINCT + JOIN + SELECT单表)
2. **数据倾斜**: 高频值JOIN是PJE改写的核心场景
3. **手工改写**: JOIN → EXISTS,保持语义等价
4. **充分验证**: 功能 + 性能双重验证
5. **优先级**: P0立即改写, P1计划改写, P2观察

### 10.2 性能预期

- **典型提升**: 10-300倍
- **极端案例**: 1000倍以上 (严重数据倾斜)
- **小幅提升**: 2-5倍 (轻微数据倾斜)

### 10.3 注意事项

⚠️ **不适用场景**:
- SELECT包含JOIN表的字段
- 无DISTINCT的JOIN查询
- 小数据量场景 (行数<1000)

⚠️ **改写风险**:
- 语义等价性必须保证
- 需要充分测试验证
- 生产环境灰度发布

---

**文档维护**: 本文档随着VastBase版本更新和实战案例积累持续优化
**反馈渠道**: 遇到问题请记录到vastbase-sql-cases目录供后续分析
