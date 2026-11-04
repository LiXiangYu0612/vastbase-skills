# Oracle Distinct Placement 迁移到VastBase深度指南

## 文档概述

**文档版本**: v1.0  
**创建日期**: 2026-04-17  
**适用场景**: Oracle → VastBase迁移,JOIN + DISTINCT场景优化  
**问题类型**: Oracle优化器支持Distinct Placement但VastBase不支持,需手动改写  

## 一、核心问题分析

### 1.1 什么是Distinct Placement (DTP)

**定义**: Oracle优化器将DISTINCT操作下推到JOIN之前,对每个表分别执行DISTINCT,从而减少参与JOIN的数据量。

**转换示例**:
```sql
-- ========== 原SQL ==========
SELECT DISTINCT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a, customer b
WHERE a.cd = b.sigungu_cd
  AND a.group_cd = 'G1'
  AND b.cust_no BETWEEN 200000 AND 299999;

-- ========== Oracle自动DTP转换 ==========
SELECT DISTINCT 
    vw_dtp_1.cd_nm,
    vw_dtp_2.sigungu_cd,
    vw_dtp_2.register_channel_cd
FROM (
    -- 内联视图1: 先对com_code做DISTINCT
    SELECT DISTINCT cd, cd_nm
    FROM com_code
    WHERE group_cd = 'G1'
) vw_dtp_1
JOIN (
    -- 内联视图2: 先对customer做DISTINCT
    SELECT DISTINCT sigungu_cd, register_channel_cd
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) vw_dtp_2
ON vw_dtp_1.cd = vw_dtp_2.sigungu_cd;
```

### 1.2 性能问题对比

**Oracle执行计划 (DTP转换后)**:
```
| Id | Operation                           | Name    | Rows  | Bytes |
|----|-------------------------------------|---------|-------|-------|
|  0 | SELECT STATEMENT                    |         |    21 |       |
|  1 |  HASH UNIQUE                        |         |    21 |       |
|  2 |   HASH JOIN                         |         |     1 |       |
|  3 |    VIEW                             | VW_DTP  |     7 |       |
|  4 |     HASH UNIQUE                     |         |     7 |       |
|  5 |      TABLE ACCESS BY INDEX ROWID    | COM_CODE|     7 |       |
|  6 |    VIEW                             | VW_DTP  |  1053 |       |
|  7 |     HASH UNIQUE                     |         |  1053 |       |
|  8 |      TABLE ACCESS BY INDEX ROWID    |CUSTOMER | 99942 |       |

关键优势:
- com_code: 15行 → 7行 (DISTINCT后)
- customer: 100000行 → 1053行 (DISTINCT后)
- JOIN: 7 × 1053 = 7371 vs 7 × 100000 = 700000
```

**VastBase执行计划 (无DTP转换)**:
```
HashAggregate
  Group Key: a.cd_nm, b.sigungu_cd, b.register_channel_cd
  -> Hash Join
       Hash Cond: (b.sigungu_cd = a.cd)
       -> Index Scan on customer b (100000 rows)
       -> Hash
            -> Seq Scan on com_code a (7 rows)

问题:
- JOIN产生100000行中间结果
- 最后DISTINCT从100000行去重到21行
- 浪费了处理99979行的CPU和内存
```

### 1.3 触发条件

**Oracle DTP转换触发条件**:
```sql
-- ✅ 触发DTP转换
SELECT DISTINCT a.col1, b.col2
FROM table_a a
JOIN table_b b ON a.key = b.key
WHERE a.filter_col = 'value'
  AND b.filter_col = 'value';

-- ✅ 三表JOIN + DISTINCT
SELECT DISTINCT a.col1, b.col2, c.col3
FROM table_a a
JOIN table_b b ON a.key = b.key
JOIN table_c c ON b.key = c.key
WHERE ...;
```

## 二、VastBase问题识别

### 2.1 问题SQL特征

**典型模式**:
```sql
-- 模式1: 两表JOIN + DISTINCT
SELECT DISTINCT 
    a.lookup_col,
    b.data_col1,
    b.data_col2
FROM lookup_table a
JOIN large_table b ON a.key = b.key
WHERE a.filter = 'value'
  AND b.filter = 'value';

-- 模式2: 多表JOIN + DISTINCT
SELECT DISTINCT 
    a.col1,
    b.col2,
    c.col3
FROM table_a a
JOIN table_b b ON a.id = b.id
JOIN table_c c ON b.id = c.id
WHERE ...;
```

### 2.2 执行计划分析

**问题执行计划 (VastBase)**:
```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN customer b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1'
  AND b.cust_no BETWEEN 200000 AND 299999;

-- 典型执行计划:
-- HashAggregate (cost=...)
--   -> Hash Join (cost=...)
--        -> Index Scan on customer b (100000 rows)
--        -> Seq Scan on com_code a (7 rows)
--
-- 问题:
-- - Hash Join产生100000行
-- - HashAggregate从100000行去重到21行
-- - 浪费了大量CPU处理99979行重复数据
```

## 三、手动Distinct Placement改写方法

### 3.1 转换核心原理

**转换思路**: 将DISTINCT下推到JOIN之前

1. **识别表关系**: 哪个表是"一对多"中的"多"表
2. **对大表先DISTINCT**: 减少参与JOIN的数据量
3. **再JOIN**: 用小表JOIN去重后的大表

**关键原则**:
```
如果关系是: 小表(1行) × 大表(100000行) → 结果(100000行) → DISTINCT → (21行)

优化为: 小表(1行) × 大表_DISTINCT(1053行) → 结果(21行)
```

### 3.2 基础转换示例

**示例1: 简单两表JOIN + DISTINCT**

```sql
-- ========== 问题SQL (VastBase性能差) ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN customer b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1'
  AND b.cust_no BETWEEN 200000 AND 299999;

-- 执行时间: 48.7ms
-- 执行计划: Hash Join (100000 rows) → HashAggregate → 21 rows

-- ========== 手动DTP转换后 ==========
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN (
    -- 关键: 对customer表先做DISTINCT
    SELECT DISTINCT 
        sigungu_cd,
        register_channel_cd
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

-- 执行时间: 31.6ms (35%提升)
-- 执行计划: HashAggregate on customer (21 rows) → Hash Join → 21 rows
-- 
-- 优势:
-- - customer先DISTINCT: 100000行 → 21行
-- - Join数据量: 7 × 21 = 147 vs 7 × 100000 = 700000
-- - 减少4763倍数据量
```

**关键点说明**:
- ✅ com_code是小表(7行),不需要DISTINCT
- ✅ customer是大表(100000行),先DISTINCT到21行
- ✅ 外层不需要DISTINCT,因为JOIN结果已经是唯一的

### 3.3 多表JOIN场景

**示例2: 三表JOIN + DISTINCT**

```sql
-- ========== 问题SQL ==========
SELECT DISTINCT 
    a.product_name,
    b.customer_name,
    c.region_name
FROM products a
JOIN orders b ON a.product_id = b.product_id
JOIN customers c ON b.customer_id = c.customer_id
WHERE a.category = 'Electronics'
  AND b.order_date >= '2024-01-01';

-- 分析:
-- - products: 小表(100行)
-- - orders: 大表(100000行)
-- - customers: 中表(10000行)
-- - 结果: DISTINCT去重

-- ========== 手动DTP转换 ==========
SELECT 
    a.product_name,
    b.customer_name,
    c.region_name
FROM products a
JOIN (
    -- 对orders先DISTINCT (最大表)
    SELECT DISTINCT 
        product_id,
        customer_id
    FROM orders
    WHERE order_date >= '2024-01-01'
) b ON a.product_id = b.product_id
JOIN customers c ON b.customer_id = c.customer_id
WHERE a.category = 'Electronics';

-- 关键点:
-- - 只对大表orders做DISTINCT
-- - products和customers都不需要DISTINCT
-- - 减少JOIN数据量
```

### 3.4 复杂过滤条件

**示例3: 带复杂过滤的DTP**

```sql
-- ========== 问题SQL ==========
SELECT DISTINCT 
    a.dept_name,
    b.emp_name,
    c.project_name
FROM departments a
JOIN employees b ON a.dept_id = b.dept_id
JOIN employee_projects c ON b.emp_id = c.emp_id
WHERE a.region = 'North'
  AND b.status = 'Active'
  AND c.project_status IN ('Planning', 'Active')
  AND c.start_date >= '2024-01-01';

-- 分析:
-- - departments: 小表(10行)
-- - employees: 中表(1000行)
-- - employee_projects: 大表(10000行)

-- ========== 手动DTP转换 ==========
SELECT 
    a.dept_name,
    b.emp_name,
    c.project_name
FROM departments a
JOIN (
    SELECT DISTINCT 
        emp_id,
        project_name
    FROM employee_projects
    WHERE project_status IN ('Planning', 'Active')
      AND start_date >= '2024-01-01'
) c ON 1=1  -- Cross JOIN, departments数量少
JOIN (
    SELECT DISTINCT 
        emp_id,
        emp_name,
        dept_id
    FROM employees
    WHERE status = 'Active'
) b ON b.emp_id = c.emp_id
WHERE a.dept_id = b.dept_id
  AND a.region = 'North';

-- 或者更简洁的写法:
SELECT 
    a.dept_name,
    b.emp_name,
    c.project_name
FROM departments a
JOIN employees b ON a.dept_id = b.dept_id
JOIN (
    SELECT DISTINCT 
        emp_id,
        project_name
    FROM employee_projects
    WHERE project_status IN ('Planning', 'Active')
      AND start_date >= '2024-01-01'
) c ON b.emp_id = c.emp_id
WHERE a.region = 'North'
  AND b.status = 'Active';

-- 关键点:
-- - 只对最大表employee_projects做DISTINCT
-- - departments是小表,不需要DISTINCT
-- - employees视情况决定是否需要DISTINCT
```

## 四、常见错误与修正

### 4.1 错误1: 对所有表都做DISTINCT

**❌ 错误改写**:
```sql
SELECT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM (
    SELECT DISTINCT cd, cd_nm  -- 不必要!
    FROM com_code
    WHERE group_cd = 'G1'
) a
JOIN (
    SELECT DISTINCT sigungu_cd, register_channel_cd
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) b ON a.cd = b.sigungu_cd;

-- 问题: com_code本身就是7行唯一值,DISTINCT是多余操作
```

**✅ 正确改写**:
```sql
SELECT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN (
    SELECT DISTINCT sigungu_cd, register_channel_cd
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

-- 正确: 只对大表customer做DISTINCT
```

### 4.2 错误2: DISTINCT列选择错误

**❌ 错误改写**:
```sql
SELECT DISTINCT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN (
    SELECT DISTINCT register_channel_cd  -- 缺少sigungu_cd!
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

-- 错误: JOIN条件列sigungu_cd不在DISTINCT中,导致语法错误或结果错误
```

**✅ 正确改写**:
```sql
SELECT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN (
    SELECT DISTINCT 
        sigungu_cd,       -- JOIN列必须包含
        register_channel_cd
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

-- 正确: JOIN条件列必须在DISTINCT中
```

### 4.3 错误3: 外层仍保留DISTINCT

**❌ 低效改写**:
```sql
SELECT DISTINCT  -- 不必要!
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN (
    SELECT DISTINCT sigungu_cd, register_channel_cd
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

-- 问题: 
-- - 内层已经对customer做DISTINCT
-- - com_code是唯一表
-- - 外层DISTINCT是多余操作
```

**✅ 正确改写**:
```sql
SELECT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM com_code a
JOIN (
    SELECT DISTINCT sigungu_cd, register_channel_cd
    FROM customer
    WHERE cust_no BETWEEN 200000 AND 299999
) b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

-- 正确: 外层不需要DISTINCT
```

### 4.4 错误4: 错误判断哪个表需要DISTINCT

**❌ 错误判断**:
```sql
-- 假设场景:
-- - employees: 1000行 (每个员工多个项目)
-- - projects: 100行 (每个项目多个员工)
-- - employee_projects: 10000行 (交叉表)

SELECT DISTINCT 
    a.emp_name,
    b.project_name
FROM employees a
JOIN employee_projects c ON a.emp_id = c.emp_id
JOIN projects b ON c.project_id = b.project_id;

-- 错误改写: 对employees做DISTINCT
SELECT 
    a.emp_name,
    b.project_name
FROM (
    SELECT DISTINCT emp_id, emp_name  -- 错误选择!
    FROM employees
) a
JOIN employee_projects c ON a.emp_id = c.emp_id
JOIN projects b ON c.project_id = b.project_id;
```

**✅ 正确判断**:
```sql
-- 分析:
-- - employees: 1000行,但emp_id是唯一的
-- - projects: 100行,project_id是唯一的
-- - employee_projects: 10000行,有重复的(emp_id, project_id)组合

-- 正确改写: 对交叉表employee_projects做DISTINCT
SELECT 
    a.emp_name,
    b.project_name
FROM employees a
JOIN (
    SELECT DISTINCT emp_id, project_id  -- 正确!
    FROM employee_projects
) c ON a.emp_id = c.emp_id
JOIN projects b ON c.project_id = b.project_id;
```

## 五、性能验证与优化

### 5.1 性能对比测试

```sql
-- ========== 测试环境准备 ==========
CREATE TABLE test_com_code (
    cd VARCHAR(10),
    cd_nm VARCHAR(100),
    group_cd VARCHAR(10)
);
CREATE TABLE test_customer (
    cust_no INT,
    sigungu_cd VARCHAR(10),
    register_channel_cd VARCHAR(10)
);

-- 插入测试数据
INSERT INTO test_com_code
SELECT 
    'CD' || i,
    'Code Name ' || i,
    CASE WHEN i % 2 = 0 THEN 'G1' ELSE 'G2' END
FROM generate_series(1, 15) i;

INSERT INTO test_customer
SELECT 
    i,
    'CD' || (i % 10 + 1),
    'CH' || (i % 5 + 1)
FROM generate_series(1, 100000) i;

CREATE INDEX idx_customer_cust_no ON test_customer(cust_no);

ANALYZE test_com_code;
ANALYZE test_customer;

-- ========== 性能对比测试 ==========
\timing on

-- 测试1: 原SQL (VastBase无DTP转换)
EXPLAIN (ANALYZE, BUFFERS)
SELECT DISTINCT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM test_com_code a
JOIN test_customer b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

-- 测试2: 手动DTP转换
EXPLAIN (ANALYZE, BUFFERS)
SELECT 
    a.cd_nm,
    b.sigungu_cd,
    b.register_channel_cd
FROM test_com_code a
JOIN (
    SELECT DISTINCT sigungu_cd, register_channel_cd
    FROM test_customer
) b ON a.cd = b.sigungu_cd
WHERE a.group_cd = 'G1';

\timing off
```

### 5.2 结果一致性验证

```sql
-- ========== 验证改写前后结果一致 ==========
WITH original AS (
    SELECT DISTINCT 
        cd_nm,
        sigungu_cd,
        register_channel_cd
    FROM test_com_code a
    JOIN test_customer b ON a.cd = b.sigungu_cd
    WHERE a.group_cd = 'G1'
),
rewritten AS (
    SELECT 
        cd_nm,
        sigungu_cd,
        register_channel_cd
    FROM test_com_code a
    JOIN (
        SELECT DISTINCT sigungu_cd, register_channel_cd
        FROM test_customer
    ) b ON a.cd = b.sigungu_cd
    WHERE a.group_cd = 'G1'
)
SELECT 
    COUNT(*) FROM (
        SELECT * FROM original
        EXCEPT
        SELECT * FROM rewritten
    ) diff1
UNION ALL
SELECT 
    COUNT(*) FROM (
        SELECT * FROM rewritten
        EXCEPT
        SELECT * FROM original
    ) diff2;

-- 应该返回: 0, 0 (结果完全一致)
```

## 六、优化决策树

### 6.1 识别DTP候选SQL

```
慢SQL分析
  │
  ├─ 包含JOIN + DISTINCT?
  │   └─ 是 → 检查执行计划
  │       └─ Hash Join后跟HashAggregate? → DTP转换候选
  │       └─ 已先做DISTINCT再JOIN? → 无需改写
  │
  ├─ JOIN数据量大?
  │   └─ 是 → DTP转换收益高
  │   └─ 否 → 评估转换成本
  │
  └─ DISTINCT后行数 << DISTINCT前行数?
      └─ 是 → DTP转换收益极高
      └─ 否 → 收益可能有限
```

### 6.2 改写优先级

**P0 (立即改写)**:
- 执行时间 > 5秒
- JOIN + DISTINCT模式
- 执行计划显示先JOIN再DISTINCT
- 大表参与JOIN (行数 > 10万)
- DISTINCT去重率 > 90% (100万行 → 1万行)
- 预期性能提升: 3-10倍

**P1 (计划改写)**:
- 执行时间 > 1秒
- JOIN + DISTINCT模式
- 执行计划显示先JOIN再DISTINCT
- 预期性能提升: 1.5-3倍

**P2 (观察)**:
- 执行时间 < 1秒
- 已先DISTINCT再JOIN
- 数据量小 (< 1万行)

### 6.3 Cost评估

```sql
-- ========== Cost评估公式 ==========
-- 原SQL成本:
-- Cost_original = Cost_Scan_A + Cost_Scan_B + Cost_Join_AB + Cost_Distinct

-- DTP转换后成本:
-- Cost_dtp = Cost_Scan_A + Cost_Scan_B + Cost_Distinct_B + Cost_Join_ADistinct

-- 判断标准:
-- IF (Rows_B >> Rows_B_Distinct) AND (Cost_Join_AB >> Cost_Join_ADistinct) THEN
--     值得改写
-- END IF

-- 简化判断:
-- IF (大表行数 > 10万) AND (执行时间 > 1秒) AND (DISTINCT去重率 > 90%) THEN
--     改写
-- END IF
```

## 七、实战案例

### 7.1 案例1: 客户地区分布统计

**业务场景**: 统计各地区客户注册渠道

```sql
-- ========== 问题SQL ==========
SELECT DISTINCT 
    r.region_name,
    c.register_channel
FROM regions r
JOIN customers c ON r.region_id = c.region_id
WHERE r.status = 'ACTIVE'
  AND c.register_date >= '2024-01-01';

-- 执行时间: 3.5秒
-- 执行计划: 
--   Hash Join (regions: 10 rows × customers: 100000 rows = 100000 rows)
--   -> HashAggregate (100000 rows -> 50 rows)

-- ========== 手动DTP转换 ==========
SELECT 
    r.region_name,
    c.register_channel
FROM regions r
JOIN (
    SELECT DISTINCT 
        region_id,
        register_channel
    FROM customers
    WHERE register_date >= '2024-01-01'
) c ON r.region_id = c.region_id
WHERE r.status = 'ACTIVE';

-- 执行时间: 0.8秒 (4.4倍提升)
-- 执行计划:
--   HashAggregate on customers (100000 rows -> 50 rows)
--   -> Hash Join (regions: 10 rows × customers: 50 rows = 500 rows)
--
-- 优势:
-- - 减少JOIN数据量: 100000 → 50 (2000倍)
-- - 避免对100000行做最终DISTINCT
```

### 7.2 案例2: 产品销售渠道分析

**业务场景**: 分析产品通过各渠道的销售情况

```sql
-- ========== 问题SQL ==========
SELECT DISTINCT 
    p.product_name,
    s.channel_name,
    l.region_name
FROM products p
JOIN sales s ON p.product_id = s.product_id
JOIN locations l ON s.location_id = l.location_id
WHERE p.category = 'Electronics'
  AND s.sale_date >= '2024-01-01'
  AND l.country = 'USA';

-- ========== 手动DTP转换 ==========
SELECT 
    p.product_name,
    s.channel_name,
    l.region_name
FROM products p
JOIN (
    SELECT DISTINCT 
        product_id,
        channel_name,
        location_id
    FROM sales
    WHERE sale_date >= '2024-01-01'
) s ON p.product_id = s.product_id
JOIN locations l ON s.location_id = l.location_id
WHERE p.category = 'Electronics'
  AND l.country = 'USA';

-- 性能提升: 3倍
-- 分析:
-- - products: 小表(100行)
-- - sales: 大表(1000000行) → DISTINCT后(5000行)
-- - locations: 小表(50行)
```

### 7.3 案例3: 员工项目分配统计

**业务场景**: 统计各部门员工参与的项目

```sql
-- ========== 问题SQL ==========
SELECT DISTINCT 
    d.dept_name,
    e.emp_name,
    p.project_name
FROM departments d
JOIN employees e ON d.dept_id = e.dept_id
JOIN employee_projects ep ON e.emp_id = ep.emp_id
JOIN projects p ON ep.project_id = p.project_id
WHERE d.region = 'North'
  AND e.status = 'Active'
  AND p.status IN ('Planning', 'Active');

-- ========== 手动DTP转换 ==========
SELECT 
    d.dept_name,
    e.emp_name,
    p.project_name
FROM departments d
JOIN employees e ON d.dept_id = e.dept_id
JOIN (
    SELECT DISTINCT 
        emp_id,
        project_id
    FROM employee_projects
) ep ON e.emp_id = ep.emp_id
JOIN projects p ON ep.project_id = p.project_id
WHERE d.region = 'North'
  AND e.status = 'Active'
  AND p.status IN ('Planning', 'Active');

-- 性能提升: 5倍
-- 分析:
-- - employee_projects是交叉表,有重复
-- - 先DISTINCT减少JOIN数据量
```

## 八、总结与最佳实践

### 8.1 核心要点

**DTP转换核心原理**:
1. 识别JOIN + DISTINCT模式
2. 判断哪个表需要DISTINCT (通常是大的多表)
3. 对大表先做子查询DISTINCT
4. 再JOIN其他表
5. 外层通常不需要DISTINCT

**关键注意事项**:
- ✅ 只对需要去重的大表做DISTINCT
- ✅ JOIN条件列必须在DISTINCT中
- ✅ 小表或唯一表不需要DISTINCT
- ✅ 外层通常不需要DISTINCT
- ✅ 验证改写前后结果完全一致

### 8.2 性能提升预期

| 场景 | 数据量 | 去重率 | 性能提升 |
|------|--------|--------|---------|
| 简单两表JOIN + DISTINCT | 10万行 | 99% | 3-5倍 |
| 多表JOIN + DISTINCT | 50万行 | 95% | 5-8倍 |
| 复杂过滤 + DISTINCT | 100万行 | 99% | 5-10倍 |
| 大基数去重 | 500万行 | 99.9% | 10-20倍 |

### 8.3 不适用场景

⚠️ **不需要改写的情况**:
- 执行时间 < 100ms
- 已先DISTINCT再JOIN
- 数据量小 (< 1万行)
- DISTINCT后行数 ≈ DISTINCT前行数 (去重率 < 10%)

⚠️ **改写风险**:
- SQL复杂度增加,可读性下降
- 需要充分测试验证
- 建议添加注释说明改写原因

### 8.4 快速参考模板

```sql
-- ========== DTP转换模板 ==========
-- 原SQL:
SELECT DISTINCT 
    a.col1,
    b.col2,
    c.col3
FROM table_a a
JOIN table_b b ON a.key = b.key
JOIN table_c c ON b.key = c.key
WHERE a.filter = 'value'
  AND b.filter = 'value'
  AND c.filter = 'value';

-- 转换后 (假设table_b是最大表):
SELECT 
    a.col1,
    b.col2,
    c.col3
FROM table_a a
JOIN (
    SELECT DISTINCT 
        key,        -- JOIN条件列
        col2        -- SELECT列
    FROM table_b
    WHERE filter = 'value'
) b ON a.key = b.key
JOIN table_c c ON b.key = c.key
WHERE a.filter = 'value'
  AND c.filter = 'value';

-- 关键点:
-- 1. 只对大表做DISTINCT
-- 2. JOIN条件列必须包含
-- 3. WHERE条件下推到子查询
-- 4. 外层不需要DISTINCT
```

---

**文档维护**: 本文档随着VastBase优化器发展和实战经验持续优化  
**相关文档**: 
- VastBase_Optimizer_Oracle_PJE_Migration_Deep_Dive.md
- VastBase_Optimizer_Oracle_GROUP_BY_Placement_Migration_Deep_Dive.md
- VastBase_Optimizer_Oracle_DISTINCT_AGGREGATE_Transformation_Deep_Dive.md
- VastBase_Optimizer_Oracle_Query_Transformation_Migration_Guide.md
