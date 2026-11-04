---
name: vastbase-sql-optimizer
version: "3.0"
description: VastBase SQL优化器 - 生产级SQL性能分析与优化工具
category: database
tags: [sql, optimization, vastbase, opengauss, performance, cost-calculation, statistics]
author: xiang
created: "2026-04-17"
depends_on:
  - <vastbase-optimizer-notes>/optimizer-deep.md
---

# VastBase SQL优化器使用手册

## 快速开始

### 1. 分析SQL性能

**输入要求**:
```sql
-- 必须包含EXPLAIN
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT ...;

-- 或者
EXPLAIN PERFORMANCE
SELECT ...;
```

**分析流程**:
```
用户输入: SQL + EXPLAIN输出 + 表统计信息
    ↓
步骤1: JOIN顺序分析 (第一优先级)
    ↓
步骤2: JOIN方式分析 (第二优先级)
    ↓
步骤3: 扫描节点分析 (第三优先级)
    ↓
步骤4: 统计信息验证
    ↓
步骤5: 查询转换验证
    ↓
输出: 性能瓶颈 + 优化方案
```

### 2. 获取表统计信息

**必需的统计信息查询**:
```sql
-- 1. 表级统计
SELECT 
    schemaname,
    tablename,
    n_live_tup,              -- 活行数
    n_dead_tup,              -- 死行数
    last_vacuum,             -- 最后VACUUM时间
    last_autovacuum,         -- 最后自动VACUUM时间
    last_analyze,            -- 最后ANALYZE时间
    last_autoanalyze,        -- 最后自动ANALYZE时间
    vacuum_count,            -- VACUUM次数
    autovacuum_count,        -- 自动VACUUM次数
    analyze_count,           -- ANALYZE次数
    autoanalyze_count        -- 自动ANALYZE次数
FROM pg_stat_user_tables
WHERE tablename = 'your_table';

-- 2. 列级统计
SELECT 
    attname AS column_name,
    n_distinct,             -- 不同值数量
    null_frac,              -- NULL值比例
    avg_width,              -- 平均宽度
    correlation,            -- 相关性
    most_common_vals,       -- MCV (最常见值)
    most_common_freqs,      -- MCV频率
    histogram_bounds        -- 直方图边界
FROM pg_stats
WHERE tablename = 'your_table'
  AND attname IN ('col1', 'col2', 'col3');

-- 3. 索引统计
SELECT 
    indexrelname AS index_name,
    idx_scan,               -- 索引扫描次数
    idx_tup_read,           -- 索引读取行数
    idx_tup_fetch,          -- 索引获取行数
    indexrelsize            -- 索引大小
FROM pg_stat_user_indexes
WHERE schemaname = 'your_schema'
  AND relname = 'your_table';

-- 4. 表大小和行数
SELECT 
    pg_size_pretty(pg_total_relation_size('your_table')) AS total_size,
    pg_size_pretty(pg_relation_size('your_table')) AS table_size,
    pg_size_pretty(pg_total_relation_size('your_table') - pg_relation_size('your_table')) AS indexes_size,
    pg_total_relation_size('your_table') AS total_bytes,
    pg_relation_size('your_table') AS table_bytes;
```

### 3. 分析输出格式

**标准输出结构**:
```markdown
## 🔴 性能瓶颈列表 (从大到小)

### 瓶颈1: JOIN顺序错误
**严重程度**: 🔴 严重

**问题描述**:
当前JOIN顺序: orders → order_items → products → customers
正确JOIN顺序: customers → products → orders → order_items
性能差距: 75.8倍

**详细分析**:
- orders(100万行) × order_items(500万行) → 50亿行 (灾难！)
- 正确顺序可以避免中间结果膨胀

## 🛠️ 优化方案

### 2.1 不改写SQL的方案

#### 方案1: 使用LEADING Hint
```sql
/*+ LEADING(c p o oi) */
SELECT ...
```

**预期效果**: Cost从1234567降到16280 (减少98.7%)

### 2.2 可改写SQL的方案

#### 方案1: 使用CTE强制JOIN顺序
```sql
WITH step1 AS (
    SELECT c.*, p.*
    FROM customers c
    CROSS JOIN products p
    WHERE ...
),
...
```

**预期效果**: Cost从1234567降到16280 (减少98.7%)
```

---

## 高级功能

### 1. JOIN顺序Cost计算器

**输入**:
- 表列表: [(表名, 行数), ...]
- JOIN条件: [(表1.字段 = 表2.字段), ...]
- 选择性: [0.01, 0.5, ...]

**输出**:
- 最优JOIN顺序
- 每种顺序的Cost
- Cost对比表

**示例**:
```python
# 输入
tables = [
    ('customers', 1000),
    ('products', 10000),
    ('orders', 1000000),
    ('order_items', 5000000)
]

join_conditions = [
    ('customers.customer_id', 'orders.customer_id', 0.01),
    ('products.product_id', 'order_items.product_id', 0.1),
    ('orders.order_id', 'order_items.order_id', 0.5)
]

# 输出
最优顺序: ['customers', 'products', 'orders', 'order_items']
总Cost: 16280

所有顺序对比:
1. customers→products→orders→order_items: Cost=16280 ⭐
2. customers→orders→products→order_items: Cost=18500
3. products→customers→orders→order_items: Cost=20300
...
24. order_items→orders→products→customers: Cost=1265002 (78倍)
```

### 2. 统计信息偏差检测器

**自动检测**:
```sql
-- 执行查询
EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
SELECT * FROM orders WHERE customer_id = 12345;

-- 输出: rows=100 (估算) vs actual rows=10000 (实际)
-- 检测到100倍偏差

-- 自动诊断
SELECT check_statistical_bias('orders', 'customer_id', 12345);

-- 输出:
偏差类型: n_distinct错误
当前值: -0.000001 (1个不同值)
正确值: -0.0001 (100个不同值)
偏差倍数: 100倍
修正方案: 
  1. ALTER TABLE orders ALTER COLUMN customer_id SET STATISTICS 10000;
  2. ANALYZE orders;
```

### 3. 查询转换分析器

**检测转换**:
```sql
-- 检查Sublink是否提升
EXPLAIN VERBOSE
SELECT *, (SELECT name FROM customers WHERE customer_id = o.customer_id)
FROM orders o;

-- 如果看到"SubPlan"，表示未提升
-- 如果看到"Hash Join"，表示已提升

-- 检查谓词是否推入
EXPLAIN VERBOSE
SELECT * FROM (SELECT * FROM orders) o WHERE o.order_date >= '2024-01-01';

-- 如果看到"Subquery Scan"，表示未推入
-- 如果看到直接"Seq Scan"加Filter，表示已推入
```

---

## 优化检查清单

### JOIN顺序优化
- [ ] 小表(行数少)是否在前？
- [ ] 高选择性JOIN是否在前？
- [ ] 索引可利用的表是否在前？
- [ ] 外连接顺序是否正确？

### JOIN方式优化
- [ ] Nested Loop: 内表是否有高效索引？
- [ ] Hash Join: work_mem是否足够？
- [ ] Merge Join: 是否利用了索引顺序？

### 扫描节点优化
- [ ] Seq Scan: 是否应该创建索引？
- [ ] Index Scan: 索引是否最优？
- [ ] Bitmap Scan: 选择性是否适合？

### 统计信息优化
- [ ] n_distinct是否准确？
- [ ] MCV是否捕获了热点值？
- [ ] 直方图是否精确？
- [ ] 相关性是否准确？
- [ ] 统计信息是否过期？

### 查询转换优化
- [ ] Sublink是否提升为JOIN？
- [ ] 谓词是否推入到子查询？
- [ ] 视图是否展开？
- [ ] 外连接是否消除？
- [ ] 表达式是否简化？

---

## 常见问题

### Q1: 为什么优化器选择了错误的JOIN顺序？

**A**: 可能的原因:
1. 统计信息不准确
2. Cost参数配置错误
3. JOIN数量超过join_collapse_limit
4. GEQO算法选择了次优解

**解决方案**:
```sql
-- 1. 更新统计信息
ANALYZE;

-- 2. 增加统计目标
ALTER TABLE table_name ALTER COLUMN column_name SET STATISTICS 10000;

-- 3. 调整JOIN限制
SET join_collapse_limit = 12;

-- 4. 禁用GEQO
SET geqo = off;

-- 5. 使用Hint强制顺序
/*+ LEADING(t1 t2 t3) */
```

### Q2: Hash Join很慢怎么办？

**A**: 检查work_mem
```sql
-- 查看当前work_mem
SHOW work_mem;

-- 计算哈希表大小
SELECT 
    pg_size_pretty(sum(reltuples * relpagewidth)) AS estimated_hash_size
FROM (
    SELECT reltuples, relpages / NULLIF(reltuples, 0) AS relpagewidth
    FROM pg_class
    WHERE relname = 'your_table'
) t;

-- 增加work_mem
SET work_mem = '256MB';

-- 永久设置
ALTER DATABASE your_db SET work_mem = '256MB';
```

### Q3: 为什么索引没被使用？

**A**: 可能的原因:
1. 选择性太低(>10%)
2. 统计信息偏差
3. 索引列被函数包裹
4. 隐式类型转换

**解决方案**:
```sql
-- 1. 检查选择性
SELECT 
    count(DISTINCT column_name) / count(*) AS selectivity
FROM table_name;

-- 2. 更新统计信息
ANALYZE table_name;

-- 3. 避免函数包裹
-- ❌ WHERE date(order_date) = '2024-01-01'
-- ✅ WHERE order_date >= '2024-01-01' AND order_date < '2024-01-02'

-- 4. 避免隐式转换
-- ❌ WHERE varchar_column = 123
-- ✅ WHERE varchar_column = '123'
```

### Q4: 如何监控慢查询？

**A**: 启用日志
```sql
-- 1. 启用慢查询日志
ALTER DATABASE your_db SET log_min_duration_statement = 1000; -- 1秒

-- 2. 查看慢查询日志
SELECT * FROM pg_stat_statements 
WHERE mean_exec_time > 1000
ORDER BY mean_exec_time DESC;

-- 3. 分析TOP慢查询
SELECT 
    query,
    calls,
    total_exec_time,
    mean_exec_time,
    max_exec_time
FROM pg_stat_statements
ORDER BY total_exec_time DESC
LIMIT 10;
```

---

## 性能基准

### Cost-时间换算

**经验公式**:
```
执行时间(ms) ≈ Cost × Cost-To-Time比率

不同操作的Cost-To-Time比率:
- Seq Scan: 1 Cost ≈ 0.1ms (SSD) / 1ms (HDD)
- Index Scan: 1 Cost ≈ 0.01ms (SSD) / 0.1ms (HDD)
- Hash Join: 1 Cost ≈ 0.05ms (内存) / 0.5ms (磁盘)
- Nested Loop: 1 Cost ≈ 0.001ms
```

**示例**:
```
Cost = 10000
如果是Seq Scan: 时间 ≈ 10000 × 0.1ms = 1秒 (SSD)
如果是Index Scan: 时间 ≈ 10000 × 0.01ms = 0.1秒 (SSD)
```

### 性能目标

**优化目标**:
- 毫秒级查询: Cost < 100
- 秒级查询: Cost < 10000
- 大数据查询: Cost < 1000000

**优化效果评估**:
```
优化前Cost: 100000
优化后Cost: 10000
减少: 90%
预期提升: 9倍 (100000/10000 = 10, 但由于其他开销,实际约9倍)
```

---

## 参考资源

### 核心文档
1. `references/VastBase_Optimizer_01_Query_Transformation_Deep_Dive.md`
2. `references/VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md`
3. `references/VastBase_Optimizer_03_Join_Order_GEQO_Deep_Dive.md`
4. `references/VastBase_Optimizer_04_Parameters_Index_Deep_Dive.md`
5. `references/VastBase_Optimizer_05_Path_Plan_Generation_Deep_Dive.md`
6. `references/VastBase_Optimizer_06_Advanced_Features_Deep_Dive.md`
7. `references/VastBase_Optimizer_07_Final_Advanced_Tuning.md`
8. `references/VastBase_Optimizer_08_Executor_Framework_Deep_Dive.md`
9. `references/VastBase_Optimizer_09_Parallel_Executor_Tuning_Deep_Dive.md`
10. `references/VastBase_Optimizer_10_SMP_Hints_Rewrite_Rules_Deep_Dive.md`

### 源码位置
- 查询转换: `src/gausskernel/optimizer/plan/`
- Cost计算: `src/gausskernel/optimizer/path/costsize.cpp`
- JOIN顺序: `src/gausskernel/optimizer/path/allpaths.cpp`
- 统计信息: `src/gausskernel/optimizer/util/plancat.c`

### 系统表和视图
- `pg_stat_user_tables` - 表统计
- `pg_stat_user_indexes` - 索引统计
- `pg_stats` - 列统计
- `pg_class` - 表和索引元数据
- `pg_stat_statements` - 查询性能统计

---

**版本**: v3.0  
**最后更新**: 2026-04-17  
**作者**: xiang
