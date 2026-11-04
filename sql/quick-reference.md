---
title: "VastBase SQL优化快速参考卡片"
description: "常用优化技巧、命令和诊断方法的快速参考"
date: 2026-04-17
category: vastbase-sql-optimization
tags: [reference, cheat-sheet, vastbase, sql-optimization]
---

# VastBase SQL优化快速参考卡片

## 🚀 快速诊断流程

```
慢查询
  ↓
1. EXPLAIN (ANALYZE, BUFFERS, VERBOSE)  ← 查看执行计划
  ↓
2. 检查JOIN顺序 (从下往上读)           ← 第一优先级
  ↓
3. 检查JOIN方式 (NL/Hash/Merge)        ← 第二优先级
  ↓
4. 检查扫描节点 (Seq/Index/Bitmap)     ← 第三优先级
  ↓
5. 验证统计信息 (pg_stats)              ← 贯穿全程
  ↓
6. 检查查询转换 (EXPLAIN VERBOSE)       ← 贯穿全程
  ↓
优化方案
```

---

## 📊 执行计划阅读

### JOIN顺序识别
```sql
-- 从下往上读，从内到外
Hash Join (最外层)
  -> Hash Join (中间层)
      -> Hash Join (最内层)
          -> Seq Scan on table1 (表1)
          -> Hash
              -> Seq Scan on table2 (表2)
      -> Hash
          -> Seq Scan on table3 (表3)
  -> Hash
      -> Seq Scan on table4 (表4)

-- JOIN顺序: table2 → table1 → table3 → table4
```

### 关键指标
```
cost=启动成本..总成本
rows=估算行数
actual time=实际时间
actual rows=实际行数
loops=循环次数

-- 性能问题信号:
- rows vs actual rows 差距>10倍 → 统计信息偏差
- 成本高但时间短 → Cost估算不准
- loops>1000 → 可能Nested Loop灾难
- Buffers: shared read>10000 → I/O瓶颈
```

---

## 🔧 常用优化Hint

### JOIN顺序Hint
```sql
-- 强制JOIN顺序
/*+ LEADING(t1 t2 t3) */
SELECT * FROM t1 JOIN t2 ON ... JOIN t3 ON ...;

-- 指定JOIN方式
/*+ HashJoin(t1 t2) */
SELECT * FROM t1 JOIN t2 ON ...;

/*+ NestLoop(t1 t2) */
SELECT * FROM t1 JOIN t2 ON ...;

/*+ MergeJoin(t1 t2) */
SELECT * FROM t1 JOIN t2 ON ...;
```

### 扫描方式Hint
```sql
-- 强制索引扫描
/*+ IndexScan(t1 idx_name) */
SELECT * FROM t1;

-- 禁用顺序扫描
/*+ NoSeqScan(t1) */
SELECT * FROM t1;
```

### 并行Hint
```sql
-- 设置并行度
/*+ Parallel(t1 4) */
SELECT * FROM t1;

-- 禁用并行
/*+ NoParallel(t1) */
SELECT * FROM t1;
```

---

## 📈 统计信息管理

### 查看统计信息
```sql
-- 表级统计
SELECT * FROM pg_stat_user_tables WHERE tablename = 'table_name';

-- 列级统计
SELECT * FROM pg_stats WHERE tablename = 'table_name';

-- 索引统计
SELECT * FROM pg_stat_user_indexes WHERE relname = 'table_name';

-- 统计信息年龄
SELECT 
    c.relname AS tablename,
    age(c.relfrozenxid) AS table_age,      -- relfrozenxid 在 pg_class，不在 pg_stat_user_tables
    s.last_analyze,
    s.last_autoanalyze
FROM pg_stat_user_tables s
JOIN pg_class c ON c.relid = s.relid;
```

### 更新统计信息
```sql
-- 基础分析
ANALYZE table_name;

-- 详细分析
ANALYZE VERBOSE table_name;

-- 分析特定列
ANALYZE table_name (column1, column2);

-- 增加统计目标
ALTER TABLE table_name ALTER COLUMN column_name SET STATISTICS 10000;

-- 并行分析
SET max_parallel_workers_per_gather = 4;  -- 机制演示；生产按基线 0
ANALYZE table_name;
```

### 统计目标设置
```sql
-- 当前统计目标
SHOW default_statistics_target;  -- 默认100

-- 临时设置
SET default_statistics_target = 10000;

-- 永久设置
ALTER DATABASE db_name SET default_statistics_target = 10000;

-- 列级别设置
ALTER TABLE table_name ALTER COLUMN column_name SET STATISTICS 10000;
```

---

## 🗂️ 索引优化

### 创建索引
```sql
-- B-Tree索引 (默认)
CREATE INDEX idx_name ON table_name(column_name);

-- 复合索引 (注意顺序)
CREATE INDEX idx_name ON table_name(col1, col2, col3);

-- 唯一索引
CREATE UNIQUE INDEX idx_name ON table_name(column_name);

-- 表达式索引
CREATE INDEX idx_name ON table_name((expression));

-- 部分索引
CREATE INDEX idx_name ON table_name(column_name) WHERE condition;

-- 并行创建索引
CREATE INDEX CONCURRENTLY idx_name ON table_name(column_name);
```

### 索引诊断
```sql
-- 查看索引使用情况
SELECT 
    schemaname,
    tablename,
    indexrelname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
ORDER BY idx_scan DESC;

-- 查找未使用的索引
SELECT 
    schemaname,
    tablename,
    indexrelname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS index_size
FROM pg_stat_user_indexes
WHERE idx_scan = 0
ORDER BY pg_relation_size(indexrelid) DESC;

-- 查看索引大小
SELECT 
    indexrelname,
    pg_size_pretty(pg_relation_size(indexrelid)) AS size
FROM pg_stat_user_indexes
WHERE relname = 'table_name';
```

### 索引优化建议
```sql
-- 检查缺失的索引 (外键)
SELECT 
    con.relname AS table_name,
    con2.relname AS referenced_table,
    att.attname AS column_name
FROM pg_constraint c
JOIN pg_class con ON c.conrelid = con.oid
JOIN pg_class con2 ON c.confrelid = con2.oid
CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum)
JOIN pg_attribute att ON att.attrelid = con.oid AND att.attnum = k.attnum
WHERE c.contype = 'f'
  AND NOT EXISTS (
    SELECT 1 FROM pg_index i
    WHERE i.indrelid = con.oid
      AND i.indkey[0] = k.attnum
  );
```

---

## ⚙️ 参数调优

### 内存参数
```sql
-- work_mem: 单个操作内存 (排序, 哈希)
SHOW work_mem;  -- 默认4MB
SET work_mem = '256MB';
SET work_mem = '1GB';

-- shared_buffers: 共享缓冲区
SHOW shared_buffers;  -- 默认128MB
SET shared_buffers = '4GB';

-- maintenance_work_mem: 维护操作内存
SHOW maintenance_work_mem;  -- 默认64MB
SET maintenance_work_mem = '512MB';
```

### 优化器参数
```sql
-- 启用/禁用特定JOIN方式
SET enable_nestloop = on;    -- Nested Loop
SET enable_hashjoin = on;    -- Hash Join
SET enable_mergejoin = on;   -- Merge Join

-- JOIN折叠限制
SHOW join_collapse_limit;    -- 默认8
SET join_collapse_limit = 12;

-- FROM折叠限制
SHOW from_collapse_limit;    -- 默认8
SET from_collapse_limit = 12;

-- GEQO遗传算法
SHOW geqo;                   -- 默认on (表数>12时)
SHOW geqo_threshold;         -- 默认12
SET geqo = off;
SET geqo_threshold = 20;
```

### 并行查询参数
```sql
-- 并行度
SHOW max_parallel_workers_per_gather;  -- 默认2；★G100 基线=0（高危项），生产按基线
SET max_parallel_workers_per_gather = 4;

-- 并行工作进程
SHOW max_worker_processes;             -- 默认8
SET max_worker_processes = 16;

-- 并行线程数
SHOW max_parallel_workers;             -- 默认8
SET max_parallel_workers = 16;

-- 并行阈值
SHOW min_parallel_table_scan_size;     -- 默认8MB
SHOW parallel_setup_cost;              -- 默认1000
SHOW parallel_tuple_cost;              -- 默认0.1
```

### Cost参数
```sql
-- seq_page_cost: 顺序页成本
SHOW seq_page_cost;  -- 默认1.0 (SSD) / 1.0 (HDD)
SET seq_page_cost = 0.5;  -- SSD

-- random_page_cost: 随机页成本
SHOW random_page_cost;  -- 默认4.0 (HDD) / 1.5 (SSD)
SET random_page_cost = 1.5;  -- SSD

-- cpu_tuple_cost: 处理行成本
SHOW cpu_tuple_cost;  -- 默认0.01

-- cpu_operator_cost: 操作符成本
SHOW cpu_operator_cost;  -- 默认0.0025
```

---

## 🔍 问题诊断SQL

### 慢查询TOP10
```sql
SELECT 
    query,
    calls,
    total_exec_time / 1000 AS total_sec,
    mean_exec_time AS avg_ms,
    max_exec_time AS max_ms,
    stddev_exec_time AS stddev_ms
FROM pg_stat_statements
ORDER BY mean_exec_time DESC
LIMIT 10;
```

### 统计信息偏差检测
```sql
-- 比较估算vs实际
SELECT 
    schemaname,
    tablename,
    n_live_tup AS estimated_rows,
    (SELECT reltuples::bigint FROM pg_class WHERE relname = pg_stat_user_tables.tablename) AS actual_rows,
    abs(n_live_tup - (SELECT reltuples::bigint FROM pg_class WHERE relname = pg_stat_user_tables.tablename))::float / 
        NULLIF(n_live_tup, 0) AS deviation_ratio
FROM pg_stat_user_tables
WHERE abs(n_live_tup - (SELECT reltuples::bigint FROM pg_class WHERE relname = pg_stat_user_tables.tablename))::float / 
      NULLIF(n_live_tup, 0) > 0.1  -- 偏差>10%
ORDER BY deviation_ratio DESC;
```

### 索引效率分析
```sql
SELECT 
    schemaname,
    tablename,
    indexrelname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch,
    CASE 
        WHEN idx_scan = 0 THEN 'UNUSED'
        WHEN idx_tup_fetch::float / idx_tup_read > 0.9 THEN 'HIGH EFFICIENCY'
        WHEN idx_tup_fetch::float / idx_tup_read < 0.1 THEN 'LOW EFFICIENCY'
        ELSE 'NORMAL'
    END AS efficiency
FROM pg_stat_user_indexes
ORDER BY idx_scan ASC;
```

### 表膨胀检测
```sql
SELECT 
    schemaname,
    tablename,
    n_live_tup,
    n_dead_tup,
    n_dead_tup::float / NULLIF((n_live_tup + n_dead_tup), 0) AS dead_ratio,
    last_vacuum,
    last_autovacuum
FROM pg_stat_user_tables
WHERE n_dead_tup > 1000
  AND n_dead_tup::float / NULLIF((n_live_tup + n_dead_tup), 0) > 0.1
ORDER BY dead_ratio DESC;
```

---

## 📋 优化检查清单

### 执行计划检查
- [ ] JOIN顺序: 小表在前？
- [ ] JOIN方式: 选择最优方式？
- [ ] 扫描节点: 使用索引？
- [ ] 过滤条件: 是否推入？
- [ ] 排序: 是否利用索引？
- [ ] 聚合: 是否优化？

### 统计信息检查
- [ ] n_distinct: 准确？
- [ ] MCV: 捕获热点值？
- [ ] 直方图: 精确？
- [ ] 相关性: 准确？
- [ ] 过期: last_analyze多久前？

### 索引检查
- [ ] 外键: 有索引？
- [ ] 查询条件: 有索引？
- [ ] JOIN条件: 有索引？
- [ ] 索引使用: 被使用？
- [ ] 索引大小: 合理？

### 配置检查
- [ ] work_mem: 足够？
- [ ] shared_buffers: 合理？
- [ ] 并行度: 启用？
- [ ] Cost参数: 匹配硬件？
- [ ] GEQO: 合理阈值？

---

## 🚨 常见问题速解

### 问题: 索引没被使用
**可能原因**:
1. 选择性太低 (>10%)
2. 统计信息偏差
3. 函数包裹
4. 隐式类型转换

**解决方案**:
```sql
-- 1. 检查选择性
SELECT count(DISTINCT col) / count(*) FROM table_name;

-- 2. 更新统计信息
ANALYZE table_name;

-- 3. 避免函数包裹
-- ❌ WHERE date(col) = '2024-01-01'
-- ✅ WHERE col >= '2024-01-01' AND col < '2024-01-02'

-- 4. 避免隐式转换
-- ❌ WHERE varchar_col = 123
-- ✅ WHERE varchar_col = '123'
```

### 问题: Hash Join很慢
**可能原因**:
work_mem不足，哈希表溢出到磁盘

**解决方案**:
```sql
-- 1. 计算哈希表大小
SELECT pg_size_pretty(sum(reltuples * 100)) AS estimated_hash_size
FROM pg_class WHERE relname = 'table_name';

-- 2. 增加work_mem
SET work_mem = '256MB';

-- 3. 永久设置
ALTER DATABASE db_name SET work_mem = '256MB';
```

### 问题: Nested Loop很慢
**可能原因**:
外行数太多，内表无索引

**解决方案**:
```sql
-- 1. 检查外行数
EXPLAIN SELECT ... FROM outer_table;

-- 2. 创建内表索引
CREATE INDEX idx_inner ON inner_table(join_column);

-- 3. 强制Hash Join
/*+ HashJoin(outer inner) */
SELECT * FROM outer_table o JOIN inner_table i ON ...;
```

### 问题: 统计信息不准
**可能原因**:
采样不足，数据倾斜

**解决方案**:
```sql
-- 1. 增加统计目标
ALTER TABLE table_name ALTER COLUMN column_name SET STATISTICS 10000;

-- 2. 手动分析
ANALYZE VERBOSE table_name;

-- 3. 检查MCV
SELECT most_common_vals, most_common_freqs 
FROM pg_stats 
WHERE tablename = 'table_name' AND attname = 'column_name';
```

---

## 🎯 性能目标

### 响应时间目标
- 毫秒级查询: < 100ms
- 秒级查询: < 1s
- 大数据查询: < 10s

### Cost目标
- 简单查询: Cost < 100
- 中等查询: Cost < 10,000
- 复杂查询: Cost < 1,000,000

### 优化效果评估
```
优化前: Cost=100,000, Time=10s
优化后: Cost=10,000, Time=1s

性能提升:
  - Cost减少: 90%
  - 时间减少: 90%
  - 提升倍数: 10倍
```

---

**版本**: v3.0  
**最后更新**: 2026-04-17  
**作者**: xiang

## 版本注记
- 源自 2026-04 独立技能 vastbase-sql-optimizer（2026-10-26 并入本域），并入时
  未逐篇复核；口径以本域 parameters/optimizer-deep 为准

## 来源
- 原独立技能文档迁移；示例数字未附原始会话证据（⚓待补实战出处）

