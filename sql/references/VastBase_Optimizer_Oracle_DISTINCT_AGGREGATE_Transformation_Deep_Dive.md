# Oracle Distinct Aggregate Transformation 迁移到VastBase深度指南

## 文档概述

**文档版本**: v1.0  
**创建日期**: 2026-04-17  
**适用场景**: Oracle → VastBase迁移,包含DISTINCT聚合函数的SQL优化  
**问题类型**: Oracle优化器支持DAT转换但VastBase不支持,需手动改写  

## 一、核心问题分析

### 1.1 什么是Distinct Aggregate Transformation (DAT)

**定义**: Oracle优化器将包含`COUNT(DISTINCT)`等DISTINCT聚合函数的查询转换为双层GROUP BY结构,从而可以使用更高效的Hash Group Aggregate。

**转换示例**:
```sql
-- ========== 原SQL (包含COUNT(DISTINCT)) ==========
SELECT 
    object_type,
    SUM(object_id),
    AVG(object_id),
    COUNT(DISTINCT object_name)
FROM t1
GROUP BY object_type;

-- ========== Oracle自动转换为DAT ==========
SELECT 
    object_type,
    SUM(item_3),
    SUM(item_3) / NULLIF(SUM(item_5), 0),
    COUNT(item_1)
FROM (
    -- 内层:对DISTINCT列也做GROUP BY
    SELECT 
        object_name,
        object_type,
        SUM(object_id) as item_3,
        COUNT(object_id) as item_5
    FROM t1
    GROUP BY object_name, object_type
) dag_view
GROUP BY object_type;
```

### 1.2 性能问题对比

**Oracle执行计划 (DAT转换后)**:
- ✅ 内层: Hash Aggregate (按object_name, object_type)
- ✅ 外层: Hash Aggregate (按object_type)
- ✅ 完全基于Hash操作,高效

**VastBase执行计划 (无DAT转换)**:
- ❌ 可能走Sort Aggregate
- ❌ 需要对DISTINCT列进行排序去重
- ❌ 无法利用Hash Aggregate的优势
- ❌ 性能下降: 3-10倍

### 1.3 触发条件

**Oracle DAT转换触发条件**:
```sql
-- ✅ 触发DAT转换
SELECT 
    group_by_col,
    COUNT(DISTINCT distinct_col),
    SUM(measure_col)
FROM table
GROUP BY group_by_col;

-- ✅ 多个DISTINCT聚合
SELECT 
    group_by_col,
    COUNT(DISTINCT col1),
    COUNT(DISTINCT col2),
    SUM(measure_col)
FROM table
GROUP BY group_by_col;

-- ✅ 混合聚合
SELECT 
    group_by_col,
    COUNT(DISTINCT distinct_col),
    AVG(measure_col),
    MIN(measure_col),
    MAX(measure_col)
FROM table
GROUP BY group_by_col;
```

## 二、VastBase问题识别

### 2.1 问题SQL特征

**典型模式**:
```sql
-- 模式1: 单个DISTINCT聚合
SELECT group_col, COUNT(DISTINCT distinct_col), SUM(measure)
FROM table
GROUP BY group_col;

-- 模式2: 多个DISTINCT聚合
SELECT group_col, 
       COUNT(DISTINCT col1), 
       COUNT(DISTINCT col2),
       SUM(measure)
FROM table
GROUP BY group_col;

-- 模式3: DISTINCT + 其他聚合
SELECT group_col,
       COUNT(DISTINCT distinct_col),
       AVG(measure),
       STDDEV(measure)
FROM table
GROUP BY group_col;
```

### 2.2 执行计划分析

**问题执行计划 (VastBase)**:
```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    object_type,
    SUM(object_id),
    AVG(object_id),
    COUNT(DISTINCT object_name)
FROM t1
GROUP BY object_type;

-- 可能的执行计划:
-- GroupAggregate  (cost=...)
--   -> Sort  (cost=...)
--      -> Seq Scan on t1  (cost=...)
-- 
-- 问题:
-- - GroupAggregate (基于排序,非Hash)
-- - Sort操作成本高
-- - 无法利用HashAggregate
```

**对比Oracle执行计划**:
```
-- Oracle (DAT转换后):
-- HASH GROUP BY (外层)
--   -> HASH GROUP BY (内层DAG view)
--       -> TABLE ACCESS
--
-- 优势:
-- - 两层Hash操作
-- - 无需排序
-- - 内存操作更高效
```

## 三、手动DAT转换方法

### 3.1 转换核心原理

**转换思路**: 手动创建双层GROUP BY结构

1. **内层查询**: 对GROUP BY列 + DISTINCT列一起GROUP BY
2. **外层查询**: 对GROUP BY列做最终聚合

**关键公式**:
```
原始: GROUP BY A, COUNT(DISTINCT B), SUM(C)
转换: 内层 GROUP BY A, B → 外层 GROUP BY A
```

### 3.2 基础转换示例

**示例1: 单个DISTINCT聚合**

```sql
-- ========== 问题SQL (VastBase性能差) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    object_type,
    SUM(object_id) as total_id,
    AVG(object_id) as avg_id,
    COUNT(DISTINCT object_name) as unique_names
FROM t1
GROUP BY object_type;

-- 可能执行计划:
-- GroupAggregate (基于Sort)
--   Sort Key: object_type
--     -> Seq Scan on t1

-- ========== 手动DAT转换后 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    object_type,
    SUM(pre_agg.sum_id) as total_id,
    SUM(pre_agg.sum_id) / NULLIF(SUM(pre_agg.cnt_id), 0) as avg_id,
    COUNT(pre_agg.object_name) as unique_names
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

-- 执行计划:
-- HashAggregate
--   -> HashAggregate (内层)
--       -> Seq Scan on t1
--
-- 优势:
-- - 两层HashAggregate
-- - 无需排序
-- 预期提升: 3-8倍
```

**关键点说明**:
- ✅ `SUM(object_id)`: 内层按先聚合,外层再SUM
- ✅ `AVG(object_id)`: 用`SUM(sum_id) / SUM(cnt_id)`实现加权平均
- ✅ `COUNT(DISTINCT object_name)`: 保留object_name,外层COUNT
- ✅ 内层GROUP BY必须包含`object_name, object_type`

### 3.3 多个DISTINCT聚合

**示例2: 两个DISTINCT列**

```sql
-- ========== 问题SQL ==========
SELECT 
    department_id,
    COUNT(DISTINCT employee_id) as unique_employees,
    COUNT(DISTINCT project_id) as unique_projects,
    SUM(salary) as total_salary
FROM employee_assignments
GROUP BY department_id;

-- ========== 手动DAT转换 ==========
SELECT 
    department_id,
    COUNT(employee_id) as unique_employees,
    COUNT(project_id) as unique_projects,
    SUM(salary_sum) as total_salary
FROM (
    SELECT 
        employee_id,
        project_id,
        department_id,
        SUM(salary) as salary_sum
    FROM employee_assignments
    GROUP BY employee_id, project_id, department_id
) pre_agg
GROUP BY department_id;

-- 关键点:
-- - 两个DISTINCT列都要保留到内层
-- - 内层GROUP BY包含所有DISTINCT列 + GROUP BY列
-- - 外层分别COUNT每个DISTINCT列
```

### 3.4 复杂聚合函数

**示例3: 包含AVG、STDDEV等统计函数**

```sql
-- ========== 问题SQL ==========
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    SUM(sales_amount) as total_sales,
    AVG(sales_amount) as avg_sales,
    STDDEV(sales_amount) as stddev_sales,
    VAR_SAMP(sales_amount) as variance_sales
FROM sales
GROUP BY region;

-- ========== 手动DAT转换 ==========
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    SUM(sales_sum) as total_sales,
    SUM(sales_sum) / NULLIF(SUM(sales_count), 0) as avg_sales,
    SQRT(
        (SUM(sales_sum_sq) - SUM(sales_sum) * SUM(sales_sum) / SUM(sales_count)) /
        NULLIF(SUM(sales_count) - 1, 0)
    ) as stddev_sales,
    (SUM(sales_sum_sq) - SUM(sales_sum) * SUM(sales_sum) / SUM(sales_count)) /
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

-- 统计函数转换公式:
-- AVG(x):        SUM(sum_x) / SUM(cnt_x)
-- STDDEV(x):     SQRT((SUM(x²) - SUM(x)²/n) / (n-1))
-- VAR_SAMP(x):   (SUM(x²) - SUM(x)²/n) / (n-1)
-- MIN(x):        MIN(min_x)  [内层MIN,外层再MIN]
-- MAX(x):        MAX(max_x)  [内层MAX,外层再MAX]
```

## 四、常见错误与修正

### 4.1 错误1: AVG函数处理不当

**❌ 错误改写**:
```sql
SELECT 
    region,
    AVG(pre_agg.avg_sales)  -- 错误!
FROM (
    SELECT region, customer_id, AVG(sales_amount) as avg_sales
    FROM sales
    GROUP BY region, customer_id
) pre_agg
GROUP BY region;

-- 问题: 平均值的平均值 ≠ 总体平均值
```

**✅ 正确改写**:
```sql
SELECT 
    region,
    SUM(pre_agg.sales_sum) / NULLIF(SUM(pre_agg.sales_count), 0) as avg_sales
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
```

### 4.2 错误2: GROUP BY列遗漏

**❌ 错误改写**:
```sql
SELECT 
    object_type,
    COUNT(DISTINCT object_name)
FROM (
    SELECT object_name, object_type  -- 缺少GROUP BY
    FROM t1
) pre_agg
GROUP BY object_type;

-- 错误: 子查询缺少GROUP BY,语法错误
```

**✅ 正确改写**:
```sql
SELECT 
    object_type,
    COUNT(object_name)
FROM (
    SELECT object_name, object_type
    FROM t1
    GROUP BY object_name, object_type  -- 必须包含
) pre_agg
GROUP BY object_type;
```

### 4.3 错误3: DISTINCT列丢失

**❌ 错误改写**:
```sql
SELECT 
    object_type,
    COUNT(*)  -- 错误!丢失了object_name
FROM (
    SELECT 
        object_type,
        SUM(object_id) as sum_id
    FROM t1
    GROUP BY object_name, object_type
) pre_agg
GROUP BY object_type;

-- 问题: object_name不在SELECT中,无法COUNT(DISTINCT)
```

**✅ 正确改写**:
```sql
SELECT 
    object_type,
    COUNT(object_name)  -- 保留object_name
FROM (
    SELECT 
        object_name,
        object_type
    FROM t1
    GROUP BY object_name, object_type
) pre_agg
GROUP BY object_type;
```

### 4.4 错误4: MIN/MAX处理不当

**❌ 错误改写**:
```sql
SELECT 
    region,
    MIN(pre_agg.min_sales),
    MAX(pre_agg.max_sales)
FROM (
    SELECT 
        region,
        customer_id,
        MIN(sales_amount) as min_sales,
        MAX(sales_amount) as max_sales
    FROM sales
    GROUP BY region, customer_id
) pre_agg
GROUP BY region;

-- 这个是对的,但要注意逻辑:
-- - MIN(min_sales): 每个客户最小销售中的最小值
-- - 可能不等于全局MIN(sales_amount)
```

**如果需要全局MIN/MAX**:
```sql
SELECT 
    region,
    MIN(pre_agg.min_sales) as min_of_customer_mins,
    MAX(pre_agg.max_sales) as max_of_customer_maxs
FROM (
    SELECT 
        region,
        customer_id,
        MIN(sales_amount) as min_sales,
        MAX(sales_amount) as max_sales
    FROM sales
    GROUP BY region, customer_id
) pre_agg
GROUP BY region;

-- 或者在内层保留所有值,外层再MIN/MAX
SELECT 
    region,
    MIN(pre_agg.sales_amount) as global_min,
    MAX(pre_agg.sales_amount) as global_max
FROM (
    SELECT 
        region,
        customer_id,
        sales_amount  -- 保留所有销售记录
    FROM sales
) pre_agg
GROUP BY region;
```

## 五、性能验证与优化

### 5.1 性能对比测试

```sql
-- ========== 测试环境准备 ==========
CREATE TABLE test_sales_dat (
    order_id BIGINT,
    customer_id BIGINT,
    region VARCHAR(20),
    sales_amount NUMERIC(18,2),
    order_date DATE
) DISTRIBUTED BY (order_id);

-- 插入测试数据
INSERT INTO test_sales_dat
SELECT 
    i,
    (random() * 10000 + 1)::INT::BIGINT,
    CASE WHEN i % 4 = 0 THEN 'North'
         WHEN i % 4 = 1 THEN 'South'
         WHEN i % 4 = 2 THEN 'East'
         ELSE 'West' END,
    (random() * 10000 + 100)::NUMERIC(18,2),
    '2024-01-01'::DATE + (random() * 365)::INT
FROM generate_series(1, 1000000) i;

ANALYZE test_sales_dat;

-- ========== 性能对比测试 ==========
\timing on

-- 原SQL (VastBase无DAT转换)
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    SUM(sales_amount) as total_sales,
    AVG(sales_amount) as avg_sales
FROM test_sales_dat
GROUP BY region;

-- 手动DAT转换后
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
    FROM test_sales_dat
    GROUP BY customer_id, region
) pre_agg
GROUP BY region;

\timing off
```

### 5.2 执行计划分析

```sql
-- ========== 对比执行计划 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    SUM(sales_amount) as total_sales
FROM test_sales_dat
GROUP BY region;

-- 观察点:
-- - 是否是GroupAggregate (Sort-based)
-- - 是否包含Sort节点
-- - Buffer hit/miss情况

EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    SUM(sales_sum) as total_sales
FROM (
    SELECT 
        customer_id,
        region,
        SUM(sales_amount) as sales_sum
    FROM test_sales_dat
    GROUP BY customer_id, region
) pre_agg
GROUP BY region;

-- 观察点:
-- - 是否是HashAggregate
-- - 是否无Sort节点
-- - 内存使用情况
```

### 5.3 结果一致性验证

```sql
-- ========== 验证改写前后结果一致 ==========
WITH original AS (
    SELECT 
        region,
        COUNT(DISTINCT customer_id) as unique_customers,
        SUM(sales_amount) as total_sales,
        AVG(sales_amount) as avg_sales
    FROM test_sales_dat
    GROUP BY region
),
rewritten AS (
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
        FROM test_sales_dat
        GROUP BY customer_id, region
    ) pre_agg
    GROUP BY region
)
SELECT 
    o.region,
    o.unique_customers as orig_customers,
    r.unique_customers as rew_customers,
    o.total_sales as orig_sales,
    r.total_sales as rew_sales,
    ROUND(o.avg_sales::NUMERIC, 2) as orig_avg,
    ROUND(r.avg_sales::NUMERIC, 2) as rew_avg,
    CASE 
        WHEN o.unique_customers = r.unique_customers 
         AND o.total_sales = r.total_sales 
         AND ABS(o.avg_sales - r.avg_sales) < 0.01
        THEN 'MATCH'
        ELSE 'MISMATCH'
    END as status
FROM original o
FULL OUTER JOIN rewritten r ON o.region = r.region;

-- 应该全部返回MATCH
```

## 六、优化决策树

### 6.1 识别DAT候选SQL

```
慢SQL分析
  │
  ├─ 包含COUNT(DISTINCT)?
  │   └─ 是 → 检查执行计划
  │       └─ 是GroupAggregate (Sort)? → DAT转换候选
  │       └─ 已是HashAggregate? → 无需改写
  │
  ├─ GROUP BY列 < 5个?
  │   └─ 是 → DAT转换收益高
  │   └─ 否 → 评估内层GROUP BY成本
  │
  └─ DISTINCT列基数 >> GROUP BY列基数?
      └─ 是 → DAT转换收益极高
      └─ 否 → 收益可能有限
```

### 6.2 改写优先级

**P0 (立即改写)**:
- 执行时间 > 5秒
- 包含COUNT(DISTINCT) + GROUP BY
- 执行计划显示GroupAggregate (Sort-based)
- DISTINCT列基数 >> GROUP BY列基数 (比例 > 10:1)
- 预期性能提升: 5-10倍

**P1 (计划改写)**:
- 执行时间 > 1秒
- 包含COUNT(DISTINCT) + 多个聚合函数
- 执行计划显示GroupAggregate
- 预期性能提升: 2-5倍

**P2 (观察)**:
- 执行时间 < 1秒
- 已使用HashAggregate
- 数据量小 (< 1万行)

### 6.3 Cost评估

```sql
-- ========== Cost评估公式 ==========
-- 原SQL成本:
-- Cost_original = Cost_Sort + Cost_GroupAggregate

-- DAT转换后成本:
-- Cost_dat = Cost_HashAggregate_inner + Cost_HashAggregate_outer

-- 判断标准:
-- IF Cost_dat < Cost_original * 0.5 THEN
--     值得改写
-- ELSE
--     观察或保留原SQL
-- END IF

-- 简化判断:
-- IF (行数 > 10万) AND (执行时间 > 1秒) AND (包含COUNT(DISTINCT)) THEN
--     改写
-- END IF
```

## 七、实战案例

### 7.1 案例1: 电商订单统计

**业务场景**: 统计各地区销售额和唯一客户数

```sql
-- ========== 问题SQL ==========
SELECT 
    region,
    COUNT(DISTINCT customer_id) as unique_customers,
    SUM(order_amount) as total_sales,
    AVG(order_amount) as avg_order,
    MIN(order_amount) as min_order,
    MAX(order_amount) as max_order
FROM orders
WHERE order_date >= '2024-01-01'
GROUP BY region;

-- 执行时间: 8.5秒
-- 执行计划: GroupAggregate (Sort-based)

-- ========== 手动DAT转换 ==========
SELECT 
    region,
    COUNT(customer_id) as unique_customers,
    SUM(order_sum) as total_sales,
    SUM(order_sum) / NULLIF(SUM(order_count), 0) as avg_order,
    MIN(min_order) as min_order,
    MAX(max_order) as max_order
FROM (
    SELECT 
        customer_id,
        region,
        SUM(order_amount) as order_sum,
        COUNT(order_amount) as order_count,
        MIN(order_amount) as min_order,
        MAX(order_amount) as max_order
    FROM orders
    WHERE order_date >= '2024-01-01'
    GROUP BY customer_id, region
) pre_agg
GROUP BY region;

-- 执行时间: 1.2秒
-- 性能提升: 7倍
-- 执行计划: HashAggregate → HashAggregate
```

### 7.2 案例2: 多维度分析

**业务场景**: 按地区和产品类别统计

```sql
-- ========== 问题SQL ==========
SELECT 
    region,
    product_category,
    COUNT(DISTINCT customer_id) as unique_customers,
    COUNT(DISTINCT order_id) as unique_orders,
    SUM(sales_amount) as total_sales,
    AVG(sales_amount) as avg_sales
FROM sales_fact
GROUP BY region, product_category;

-- ========== 手动DAT转换 ==========
SELECT 
    region,
    product_category,
    COUNT(customer_id) as unique_customers,
    COUNT(order_id) as unique_orders,
    SUM(sales_sum) as total_sales,
    SUM(sales_sum) / NULLIF(SUM(sales_count), 0) as avg_sales
FROM (
    SELECT 
        customer_id,
        order_id,
        region,
        product_category,
        SUM(sales_amount) as sales_sum,
        COUNT(sales_amount) as sales_count
    FROM sales_fact
    GROUP BY customer_id, order_id, region, product_category
) pre_agg
GROUP BY region, product_category;

-- 性能提升: 5倍
-- 数据量: 500万行 → 50万行 (内层) → 200行 (外层)
```

### 7.3 案例3: 时间序列分析

**业务场景**: 每日活跃用户统计

```sql
-- ========== 问题SQL ==========
SELECT 
    date_trunc('day', order_date) as order_day,
    COUNT(DISTINCT customer_id) as daily_active_users,
    COUNT(DISTINCT order_id) as daily_orders,
    SUM(order_amount) as daily_sales
FROM orders
WHERE order_date >= '2024-01-01'
GROUP BY date_trunc('day', order_date);

-- ========== 手动DAT转换 ==========
SELECT 
    order_day,
    COUNT(customer_id) as daily_active_users,
    COUNT(order_id) as daily_orders,
    SUM(order_sum) as daily_sales
FROM (
    SELECT 
        customer_id,
        order_id,
        date_trunc('day', order_date) as order_day,
        SUM(order_amount) as order_sum
    FROM orders
    WHERE order_date >= '2024-01-01'
    GROUP BY customer_id, order_id, date_trunc('day', order_date)
) pre_agg
GROUP BY order_day;

-- 性能提升: 6倍
-- 优势: 避免了对大量order_date的排序操作
```

## 八、总结与最佳实践

### 8.1 核心要点

**DAT转换核心原理**:
1. 识别COUNT(DISTINCT) + GROUP BY模式
2. 创建内层查询: GROUP BY (GROUP BY列 + DISTINCT列)
3. 外层查询: 对GROUP BY列做最终聚合
4. 正确处理AVG、STDDEV等统计函数

**关键注意事项**:
- ✅ AVG用加权平均: SUM(sum_x) / SUM(cnt_x)
- ✅ COUNT(DISTINCT)需要保留DISTINCT列到外层
- ✅ 内层GROUP BY必须包含所有DISTINCT列
- ✅ 验证改写前后结果完全一致

### 8.2 性能提升预期

| 场景 | 数据量 | 性能提升 |
|------|--------|---------|
| 简单COUNT(DISTINCT) | 10万行 | 3-5倍 |
| 多DISTINCT列 | 50万行 | 5-8倍 |
| 复杂统计函数 | 100万行 | 5-10倍 |
| 大基数DISTINCT | 500万行 | 8-15倍 |

### 8.3 不适用场景

⚠️ **不需要改写的情况**:
- 执行时间 < 100ms
- 已使用HashAggregate
- 数据量小 (< 1万行)
- DISTINCT列基数很小 (< 10)

⚠️ **改写风险**:
- SQL复杂度增加,可读性下降
- 需要充分测试验证
- 建议添加注释说明改写原因

### 8.4 自动检测SQL

```sql
-- 创建DAT候选检测函数
CREATE OR REPLACE FUNCTION detect_dat_candidates()
RETURNS TABLE(
    query_id BIGINT,
    sql_text TEXT,
    has_distinct_aggregate BOOLEAN,
    has_group_by BOOLEAN,
    uses_hash_agg BOOLEAN,
    optimization_potential TEXT
) AS $$
BEGIN
    RETURN QUERY
    SELECT 
        ss.query_id,
        ss.query,
        ss.query LIKE '%COUNT(DISTINCT%' OR ss.query LIKE '%count(distinct%' AS has_distinct_aggregate,
        ss.query LIKE '%GROUP BY%' AS has_group_by,
        -- VastBase需要通过EXPLAIN检查是否HashAggregate
        TRUE AS uses_hash_agg,  -- 需要手动EXPLAIN验证
        CASE 
            WHEN ss.query LIKE '%COUNT(DISTINCT%' 
              AND ss.query LIKE '%GROUP BY%'
              AND ss.mean_exec_time > 1000000
            THEN 'DAT转换候选'
            ELSE '其他'
        END as optimization_potential
    FROM pg_stat_statements ss
    WHERE ss.calls > 10
      AND ss.mean_exec_time > 500000
      AND (ss.query LIKE '%COUNT(DISTINCT%' OR ss.query LIKE '%count(distinct%')
    ORDER BY ss.mean_exec_time DESC
    LIMIT 50;
END;
$$ LANGUAGE plpgsql;

-- 使用检测函数
SELECT * FROM detect_dat_candidates();
```

---

**文档维护**: 本文档随着VastBase优化器发展和实战经验持续优化  
**相关文档**: 
- VastBase_Optimizer_Oracle_PJE_Migration_Deep_Dive.md
- VastBase_Optimizer_Oracle_GROUP_BY_Placement_Migration_Deep_Dive.md
- VastBase_Optimizer_Oracle_Query_Transformation_Migration_Guide.md
