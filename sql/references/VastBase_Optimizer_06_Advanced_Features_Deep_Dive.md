# VastBase/openGauss 优化器高级特性与实战完整指南

> 源码版本: openGauss-server-v6.0.0  
> 基于前序文档的深度分析  
> 学习目标: 掌握高级优化技术、实战调优技巧

---

## 目录

### 第七部分: 高级优化特性
- [32. 并行查询优化](#32-并行查询优化)
- [33. 分区表优化](#33-分区表优化)
- [34. 物化视图优化](#34-物化视图优化)
- [35. 查询 hint 与计划绑定](#35-查询-hint-与计划绑定)

### 第八部分: 实战调优案例
- [36. 慢查询诊断与优化](#36-慢查询诊断与优化)
- [37. 复杂查询优化](#37-复杂查询优化)
- [38. 数据仓库优化](#38-数据仓库优化)
- [39. 高并发优化](#39-高并发优化)

### 第九部分: 优化器内部机制
- [40. 统计信息深度剖析](#40-统计信息深度剖析)
- [41. 内存管理机制](#41-内存管理机制)
- [42. 代价模型调优](#42-代价模型调优)

---

## 32. 并行查询优化

### 32.1 并行查询原理

**核心概念**:

```sql
-- 1. 并行度 (DOP: Degree of Parallelism)
-- 指同时执行查询的工作线程数
-- 影响因素:
-- - max_parallel_workers_per_gather: 最大并行工作线程数 (默认: 2)
-- - max_parallel_workers: 全局最大并行工作线程数 (默认: 8)
-- - max_worker_processes: 最大工作进程数 (默认: 8)
-- - 表大小: 小表不会并行扫描

-- 2. 并行查询架构
-- Leader进程:
--   - 负责协调
--   - 生成执行计划
--   - 分配任务给Worker进程
--   - 收集Worker结果
--
-- Worker进程:
--   - 执行并行任务
--   - 返回结果给Leader
--
-- 3. 并行执行节点
-- - Parallel Seq Scan: 并行顺序扫描
-- - Parallel Index Scan: 并行索引扫描
// - Parallel Bitmap Scan: 并行位图扫描
// - Parallel Hash Join: 并行哈希连接
// - Parallel Aggregate: 并行聚合
// - Gather: 收集Worker结果 (并行 → 串行)
// - Gather Merge: 收集并合并Worker结果 (保持排序)
```

**源码分析**:

```cpp
// src/gausskernel/optimizer/path/costsize.cpp
// cost_parallel_seqscan函数

void cost_parallel_seqscan(
    Path* path,
    PlannerInfo* root,
    RelOptInfo* baserel,
    int dop)  // Degree of Parallelism
{
    Cost startup_cost = 0;
    Cost run_cost = 0;
    Cost cpu_per_tuple = 0;
    
    // 获取并行度
    int parallel_workers = dop;
    
    // 计算每个Worker扫描的页数
    double pages_per_worker = baserel->pages / parallel_workers;
    
    // 计算磁盘I/O成本 (并行)
    run_cost += seq_page_cost * pages_per_worker;
    
    // 计算CPU成本 (并行)
    cpu_per_tuple = cpu_tuple_cost + qpqual_cost.per_tuple;
    run_cost += cpu_per_tuple * baserel->tuples / parallel_workers;
    
    // 计算Gather成本 (收集Worker结果)
    Cost gather_cost = u_sess->opt_cxt.smp_thread_cost * (parallel_workers - 1);
    startup_cost += gather_cost;
    
    path->startup_cost = startup_cost;
    path->total_cost = startup_cost + run_cost;
    path->rows = baserel->rows;
    path->parallel_degree = parallel_workers;
}

// src/gausskernel/optimizer/path/allpaths.cpp
// create_parallel_paths函数

void create_parallel_paths(PlannerInfo* root, RelOptInfo* rel)
{
    // 检查是否可以并行扫描
    if (!enable_parallel_query) {
        return;
    }
    
    // 检查表大小 (太小不值得并行)
    if (rel->pages < min_parallel_table_scan_size) {
        return;
    }
    
    // 计算最大并行度
    int max_parallel_workers = max_parallel_workers_per_gather;
    
    // 根据表大小估算合适的并行度
    // 假设每个Worker至少扫描1MB数据
    int table_size_mb = rel->pages * 8 / 1024;  // 假设页大小8KB
    int estimated_workers = Min(table_size_mb, max_parallel_workers);
    
    // 生成并行路径
    for (int dop = 2; dop <= estimated_workers; dop++) {
        // 创建并行顺序扫描路径
        ParallelPath* ppath = create_parallel_seqscan_path(root, rel, dop);
        
        // 添加到路径列表
        add_path(rel, (Path*) ppath);
    }
}
```

### 32.2 并行查询配置与调优

**基础配置**:

```sql
-- 1. 启用并行查询
SET max_parallel_workers_per_gather = 4;  -- 会话级别
-- 或
ALTER DATABASE dbname SET max_parallel_workers_per_gather = 4;

-- 2. 设置全局并行工作线程数
SET max_parallel_workers = 16;
-- 或修改 postgresql.conf:
-- max_parallel_workers = 16

-- 3. 设置最小并行表大小
SET min_parallel_table_scan_size = '8MB';
-- 表小于8MB时,不使用并行扫描

-- 4. 设置最小并行索引扫描大小
SET min_parallel_index_scan_size = '512kB';
-- 索引小于512kB时,不使用并行索引扫描

-- 5. 查看并行度
SHOW max_parallel_workers_per_gather;
SHOW max_parallel_workers;
SHOW max_worker_processes;
```

**实战测试**:

```sql
-- 创建大表
CREATE TABLE big_table (
    id INT,
    data TEXT,
    value NUMERIC
);

-- 插入1亿行数据
INSERT INTO big_table
SELECT 
    generate_series(1, 100000000),
    repeat('x', 100),
    (random() * 1000)::numeric;

ANALYZE big_table;

-- 测试1: 顺序扫描 (串行 vs 并行)
EXPLAIN (ANALYZE, TIMING, BUFFERS)
SELECT COUNT(*) FROM big_table;

-- 串行执行 (默认):
-- Seq Scan on big_table  (cost=0.00..1234567.89 rows=100000000 width=0)
-- 
-- 并行执行 (设置并行度):
SET max_parallel_workers_per_gather = 4;

EXPLAIN (ANALYZE, TIMING, BUFFERS)
SELECT COUNT(*) FROM big_table;

-- Gather  (cost=0.00..123456.78 rows=100000000 width=0)
//   Workers Planned: 4
//   Workers Launched: 4
//   ->  Parallel Seq Scan on big_table  (cost=0.00..308641.97 rows=25000000 width=0)
//
-- 观察:
// - "Workers Planned: 4": 计划使用4个Worker
// - "Workers Launched: 4": 实际启动4个Worker
// - "rows=25000000": 每个Worker扫描2500万行

-- 测试2: 聚合查询 (串行 vs 并行)
EXPLAIN (ANALYZE, TIMING, BUFFERS)
SELECT value, COUNT(*) FROM big_table GROUP BY value;

-- 并行聚合:
-- Gather  (cost=0.00..234567.89 rows=1000 width=12)
//   Workers Planned: 4
//   ->  Parallel HashAggregate  (cost=0.00..234567.89 rows=1000 width=12)
//         Group Key: value
//         ->  Parallel Seq Scan on big_table  (cost=0.00..123456.78 rows=100000000 width=12)
//
-- 观察:
// - "Parallel HashAggregate": 并行哈希聚合
// - 每个Worker独立计算部分聚合结果
// - Leader进程合并Worker的聚合结果

-- 测试3: JOIN查询 (串行 vs 并行)
CREATE TABLE another_big_table (
    id INT,
    info TEXT
);

INSERT INTO another_big_table
SELECT generate_series(1, 50000000), 'info';

ANALYZE another_big_table;

EXPLAIN (ANALYZE, TIMING, BUFFERS)
SELECT * FROM big_table b
JOIN another_big_table a ON b.id = a.id;

-- 并行JOIN:
-- Gather  (cost=0.00..456789.01 rows=50000000 width=120)
//   Workers Planned: 4
//   ->  Parallel Hash Join  (cost=0.00..456789.01 rows=12500000 width=120)
//         Hash Cond: (b.id = a.id)
//         ->  Parallel Seq Scan on big_table b  (cost=0.00..123456.78 rows=25000000 width=60)
//         ->  Parallel Hash  (cost=0.00..123456.78 rows=25000000 width=60)
//               ->  Parallel Seq Scan on another_big_table a  (cost=0.00..123456.78 rows=25000000 width=60)
//
-- 观察:
// - "Parallel Hash Join": 并行哈希连接
// - 每个Worker构建部分哈希表
// - 每个Worker探测部分外表
```

### 32.3 并行查询性能调优

**调优策略**:

```sql
-- 1. 选择合适的并行度
-- 原则: 并行度 ≤ CPU核心数
-- 
-- 查看CPU核心数:
SHOW cpu_count;  -- 假设16核

-- 设置并行度
SET max_parallel_workers_per_gather = 8;  -- 使用一半CPU核心

-- 测试不同并行度的性能
SET max_parallel_workers_per_gather = 2;
EXPLAIN (ANALYZE) SELECT COUNT(*) FROM big_table;

SET max_parallel_workers_per_gather = 4;
EXPLAIN (ANALYZE) SELECT COUNT(*) FROM big_table;

SET max_parallel_workers_per_gather = 8;
EXPLAIN (ANALYZE) SELECT COUNT(*) FROM big_table;

-- 选择执行时间最短的并行度

-- 2. 调整最小并行表大小
-- 原则: 太小的表不值得并行 (并行开销 > 并行收益)
--
-- 查看表大小
SELECT 
    pg_size_pretty(pg_relation_size('big_table')) AS table_size;

-- 调整最小并行表大小
SET min_parallel_table_scan_size = '1MB';  -- 更小的表也可以并行
SET min_parallel_table_scan_size = '100MB'; -- 更大的表才并行

-- 3. 避免过度并行
-- 问题: 太多并发查询,每个都用并行,会导致CPU争抢
--
-- 解决: 设置全局并行工作线程数上限
SET max_parallel_workers = 16;

-- 4. 监控并行查询
-- 查看当前运行的并行查询
SELECT 
    pid,
    query,
    wait_event,
    state
FROM pg_stat_activity
WHERE query LIKE '%Parallel%'
  AND state = 'active';

-- 查看并行Worker统计
SELECT 
    SUM(plan_parallel_workers) AS planned_workers,
    SUM(plan_parallel_workers - actual_parallel_workers) AS not_launched_workers
FROM pg_stat_statements;
```

---

## 33. 分区表优化

### 33.1 分区表原理

**分区类型**:

```sql
-- 1. 范围分区 (Range Partition)
-- 适合: 时间序列数据、数值范围数据
--
-- 示例: 按日期范围分区
CREATE TABLE orders_range (
    order_id INT,
    order_date DATE,
    customer_id INT,
    amount NUMERIC
) PARTITION BY RANGE (order_date);

-- 创建分区
CREATE TABLE orders_2024_q1 PARTITION OF orders_range
    FOR VALUES FROM ('2024-01-01') TO ('2024-04-01');

CREATE TABLE orders_2024_q2 PARTITION OF orders_range
    FOR VALUES FROM ('2024-04-01') TO ('2024-07-01');

CREATE TABLE orders_2024_q3 PARTITION OF orders_range
    FOR VALUES FROM ('2024-07-01') TO ('2024-10-01');

CREATE TABLE orders_2024_q4 PARTITION OF orders_range
    FOR VALUES FROM ('2024-10-01') TO ('2025-01-01');

-- 2. 列表分区 (List Partition)
-- 适合: 枚举值、离散值
--
-- 示例: 按城市分区
CREATE TABLE customers_list (
    customer_id INT,
    city VARCHAR(100),
    customer_name VARCHAR(200)
) PARTITION BY LIST (city);

-- 创建分区
CREATE TABLE customers_beijing PARTITION OF customers_list
    FOR VALUES IN ('Beijing', 'Tianjin', 'Shijiazhuang');

CREATE TABLE customers_shanghai PARTITION OF customers_list
    FOR VALUES IN ('Shanghai', 'Hangzhou', 'Nanjing');

CREATE TABLE customers_guangzhou PARTITION OF customers_list
    FOR VALUES IN ('Guangzhou', 'Shenzhen', 'Foshan');

CREATE TABLE customers_other PARTITION OF customers_list
    DEFAULT;  -- 其他城市

-- 3. 哈希分区 (Hash Partition)
-- 适合: 均匀分布数据
--
-- 示例: 按用户ID哈希分区
CREATE TABLE orders_hash (
    order_id INT,
    customer_id INT,
    order_date DATE,
    amount NUMERIC
) PARTITION BY HASH (customer_id);

-- 创建4个分区
CREATE TABLE orders_hash_0 PARTITION OF orders_hash
    FOR VALUES WITH (MODULUS 4, REMAINDER 0);

CREATE TABLE orders_hash_1 PARTITION OF orders_hash
    FOR VALUES WITH (MODULUS 4, REMAINDER 1);

CREATE TABLE orders_hash_2 PARTITION OF orders_hash
    FOR VALUES WITH (MODULUS 4, REMAINDER 2);

CREATE TABLE orders_hash_3 PARTITION OF orders_hash
    FOR VALUES WITH (MODULUS 4, REMAINDER 3);
```

### 33.2 分区裁剪 (Partition Pruning)

**原理**: 优化器根据WHERE条件,只扫描相关分区,避免全表扫描

```sql
-- 示例: 分区裁剪
CREATE TABLE orders_2024 (
    order_id INT,
    order_date DATE,
    amount NUMERIC
) PARTITION BY RANGE (order_date);

-- 创建12个分区 (按月)
CREATE TABLE orders_2024_01 PARTITION OF orders_2024
    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');

CREATE TABLE orders_2024_02 PARTITION OF orders_2024
    FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');

-- ... (省略其他月份)

CREATE TABLE orders_2024_12 PARTITION OF orders_2024
    FOR VALUES FROM ('2024-12-01') TO ('2025-01-01');

-- 插入数据
INSERT INTO orders_2024
SELECT 
    generate_series(1, 1000000),
    CURRENT_DATE - (random() * 365)::int,
    (random() * 1000)::numeric;

ANALYZE orders_2024;

-- 查询1: 分区裁剪生效
EXPLAIN
SELECT * FROM orders_2024
WHERE order_date = '2024-01-15';

-- 执行计划:
-- Seq Scan on orders_2024_01  (cost=0.00..123.45 rows=1000 width=16)
//   Filter: (order_date = '2024-01-15'::date)
//
-- 观察: 只扫描orders_2024_01分区,其他11个分区被裁剪

-- 查询2: 跨分区查询
EXPLAIN
SELECT * FROM orders_2024
WHERE order_date BETWEEN '2024-03-01' AND '2024-05-31';

-- 执行计划:
-- Append  (cost=0.00..456.78 rows=3000 width=16)
//   ->  Seq Scan on orders_2024_03  (cost=0.00..123.45 rows=1000 width=16)
//         Filter: (order_date >= '2024-03-01'::date AND order_date <= '2024-05-31'::date)
//   ->  Seq Scan on orders_2024_04  (cost=0.00..123.45 rows=1000 width=16)
//         Filter: (order_date >= '2024-03-01'::date AND order_date <= '2024-05-31'::date)
//   ->  Seq Scan on orders_2024_05  (cost=0.00..123.45 rows=1000 width=16)
//         Filter: (order_date >= '2024-03-01'::date AND order_date <= '2024-05-31'::date)
//
-- 观察: 只扫描3个分区 (3月、4月、5月),其他9个分区被裁剪

-- 查询3: 无法分区裁剪
EXPLAIN
SELECT * FROM orders_2024
WHERE amount > 500;

-- 执行计划:
-- Append  (cost=0.00..1234.56 rows=10000 width=16)
//   ->  Seq Scan on orders_2024_01  (cost=0.00..123.45 rows=833 width=16)
//         Filter: (amount > 500::numeric)
//   ->  Seq Scan on orders_2024_02  (cost=0.00..123.45 rows=833 width=16)
//         Filter: (amount > 500::numeric)
//   ... (扫描所有12个分区)
//
-- 观察: 必须扫描所有12个分区 (WHERE条件不包含分区键)
```

**源码分析**:

```cpp
// src/gausskernel/optimizer/path/allpaths.cpp
// prune_partitions函数

void prune_partitions(PlannerInfo* root, RelOptInfo* rel)
{
    // 获取分区表信息
    PartitionPruneContext* context = &rel->partprune_context;
    
    // 获取分区约束条件
    List* partition_clauses = extract_partition_clauses(root, rel);
    
    // 遍历所有分区
    List* live_partitions = NIL;
    foreach(lc, context->partitions) {
        Partition* part = (Partition*) lfirst(lc);
        
        // 检查分区是否匹配约束条件
        if (partition_matches_constraint(part, partition_clauses)) {
            live_partitions = lappend(live_partitions, part);
        }
    }
    
    // 只保留匹配的分区
    rel->live_partitions = live_partitions;
}
```

**说明**: 上面 `prune_partitions` 是简化教学模型(函数名与字段按 PostgreSQL 风格示意)。openGauss/VastBase 真实实现中没有 `rel->live_partitions` 字段: 裁剪结果挂在 `RelOptInfo->pruning_result`(`PruningResult*`),裁剪后剩余分区数记录在 `rel->partItrs`。

**真实源码实现**:

```cpp
// src/gausskernel/optimizer/path/allpaths.cpp
// set_plain_rel_size函数 (真实源码节选)

static void set_plain_rel_size(PlannerInfo* root, RelOptInfo* rel, RangeTblEntry* rte)
{
    set_rel_bucketinfo(root, rel, rte);
    if (rte->ispartrel) {
        Relation relation = heap_open(rte->relid, NoLock);

        /* get pruning result */
        if (rte->partitionOidList == NIL) {
            // 有WHERE约束: 用基表约束做静态裁剪
            rel->pruning_result = partitionPruningForRestrictInfo(
                root, rte, relation, rel->baserestrictinfo);
        } else {
            // 查询显式点名分区: 按分区列表直取
            rel->pruning_result = PartitionPruningForPartitionList(rte, relation);
        }

        // 剩余分区数 = 范围分区位图成员数 + interval分区位图成员数
        rel->partItrs = bms_num_members(rel->pruning_result->bm_rangeSelectedPartitions) +
                        bms_num_members(rel->pruning_result->intervalSelectedPartitions);
        // ...
    }
    // ...
}

// src/include/utils/partitionmap_gs.h
// PruningResult结构 (真实源码节选)

typedef struct PruningResult {
    NodeTag type;
    PruningResultState state;
    PruningBoundary* boundary;              // 分区键边界收敛结果
    Bitmapset* bm_rangeSelectedPartitions;  // 选中的范围分区位图
    int intervalOffset;
    Bitmapset* intervalSelectedPartitions;  // 选中的interval分区位图
    List* ls_rangeSelectedPartitions;       // 选中分区(列表形式)
    List* ls_selectedPartitionnos;
    Param* paramArg;                        // PBE运行时裁剪的参数
    OpExpr* exprPart;
    Expr* expr;                             // 保留到执行期的裁剪表达式
    bool isPbeSinlePartition = false;       // PBE单分区特判标志
} PruningResult;
```

**裁剪函数调用链**:

| 函数 | 源码文件 | 作用 |
|------|----------|------|
| partitionPruningForRestrictInfo | gausskernel/optimizer/util/pruning.cpp | 优化器入口,把基表约束交给裁剪器 |
| partitionPruningForExpr | 同上 | 单条表达式的裁剪入口 |
| partitionPruningWalker | 同上 | 递归下推AND/OR,收敛PruningBoundary |
| partitionEqualPruningWalker | 同上 | LIST/HASH分区的等值语义裁剪 |
| GetPartitionInfo | 同上 | 执行器侧入口,代入PBE参数重算裁剪 |
| GetPartitionPruningResultInInitScanRelation | gausskernel/runtime/executor/nodeSeqscan.cpp | 扫描初始化时决定用静态结果还是重算 |

```cpp
// src/gausskernel/optimizer/util/pruning.cpp
// GetPartitionInfo函数 (真实源码节选)

PruningResult* GetPartitionInfo(PruningResult* result, EState* estate, Relation current_relation)
{
    PruningResult* resPartition = NULL;
    PruningContext context;

    context.pruningType = PruningPartition;
    context.estate = estate;
    context.relation = current_relation;

    if (current_relation->partMap->type == PART_TYPE_LIST ||
        current_relation->partMap->type == PART_TYPE_HASH) {
        // LIST/HASH分区: 等值语义裁剪
        resPartition = partitionEqualPruningWalker(
            current_relation->partMap->type, result->expr, &context);
    } else {
        // RANGE分区: 边界收敛裁剪
        resPartition = partitionPruningWalker(result->expr, &context);
    }
    partitionPruningFromBoundary(&context, resPartition);

    // 收敛不动 → 回退为全分区
    if (PruningResultIsFull(resPartition) || ...) {
        destroyPruningResult(resPartition);
        resPartition = getFullPruningResult(current_relation);
        return resPartition;
    }

    // 位图 → 分区列表,交给扫描迭代
    generateListFromPruningBM(resPartition, current_relation->partMap);
    // ...
    return resPartition;
}
```

**静态裁剪 vs 运行时裁剪**:

```sql
-- 静态裁剪 (计划期完成):
-- 常量条件在生成计划时就把分区集合收敛死
EXPLAIN
SELECT * FROM orders_2024 WHERE order_date = '2024-01-15';
-- 计划中直接出现 orders_2024_01,其余11个分区不进入计划

-- 运行时裁剪 (执行期完成):
-- PBE的参数在绑定前不知道值,计划期保留裁剪表达式
PREPARE p1(DATE) AS
    SELECT * FROM orders_2024 WHERE order_date = $1;

EXPLAIN EXECUTE p1('2024-01-15');
-- pruningInfo->expr 非空 → InitScanRelation时走GetPartitionInfo重算

-- 观察:
-- - 常量SQL: 计划即裁剪,EXPLAIN直接可见
-- - PBE参数SQL: 计划保留表达式,执行期代入参数再裁剪
-- - 这就是绑定变量SQL要EXPLAIN EXECUTE才能看到实际裁剪效果的原因
```

```cpp
// src/gausskernel/runtime/executor/nodeSeqscan.cpp
// GetPartitionPruningResultInInitScanRelation函数 (真实源码节选)

static PruningResult* GetPartitionPruningResultInInitScanRelation(
    SeqScan* plan, EState* estate, Relation current_relation)
{
    PruningResult* resultPlan = NULL;
    if (plan->pruningInfo->expr != NULL) {
        // 计划期保留了裁剪表达式(PBE参数) → 执行期重新求值
        resultPlan = GetPartitionInfo(plan->pruningInfo, estate, current_relation);
    } else {
        // 计划期已静态裁剪完成 → 直接使用
        resultPlan = plan->pruningInfo;
    }
    return resultPlan;
}
```

### 33.3 分区裁剪失效场景与参数开关

**典型失效场景**:

```sql
-- 场景1: 分区键被函数包裹 → 边界无法收敛
EXPLAIN
SELECT * FROM orders_2024
WHERE to_char(order_date, 'YYYY-MM-DD') = '2024-01-15';
-- 观察: 全部分区进入计划,裁剪完全失效

-- 场景2: 分区键侧发生隐式类型转换
EXPLAIN
SELECT * FROM orders_2024
WHERE order_date = '2024-01-15'::timestamp;
-- 观察: date与timestamp比较时分区键侧被转换,裁剪退化
-- 正确写法: WHERE order_date = DATE '2024-01-15'

-- 场景3: 谓词不含分区键 → 天然不裁剪
EXPLAIN
SELECT * FROM orders_2024
WHERE amount > 500;
-- 观察: 12个分区全扫,只能靠分区内的普通索引或并行扫描补救

-- 场景4: OR跨分区 → 各分支分别收敛再取并集
EXPLAIN
SELECT * FROM orders_2024
WHERE order_date = '2024-01-15' OR order_date = '2024-06-15';
-- 观察: 保留1月/6月两个分区,其余裁剪
```

**参数开关**:

| 参数 | 默认 | 作用 | 源码锚点 |
|------|------|------|----------|
| partition_page_estimation | off | 裁剪后按选中分区实际页数估计表大小,不再沿用父表整表页数 | guc_sql.cpp(GUC注册); allpaths.cpp set_plain_rel_size; costsize.cpp |
| partition_iterator_elimination | off | 裁剪到单分区时消除分区迭代器,扫描直连唯一分区 | guc_sql.cpp(GUC注册); createplan.cpp isPartiteratorElimination |

```sql
-- 单分区直通: 打开迭代器消除
SET partition_iterator_elimination = on;

EXPLAIN
SELECT MIN(amount) FROM orders_2024
WHERE order_date = '2024-01-15';
-- 观察: 分区迭代层级被消除,扫描直接落在唯一分区
-- 与MIN/MAX优化(只取分区索引首尾)叠加时收益最大
```

```cpp
// src/gausskernel/optimizer/plan/createplan.cpp
// isPartiteratorElimination函数 (真实源码节选)

static bool isPartiteratorElimination(PlannerInfo* root, RelOptInfo* relopt, Plan* plan)
{
    if (!u_sess->attr.attr_sql.partition_iterator_elimination) {
        return false;
    }
    if (!IsPlanForPartitionScan(plan->lefttree)) {
        return false;
    }
    // ...
    if (RelationIsCommonPartitioned(rel) && relopt->partItrs == 1) {
        result = true;  // 裁剪后只剩1个分区 → 允许消除迭代器
    }
    // ...
}
```

**调优要点**:

- 分区键与高频谓词对齐: RANGE分区对齐时间/数值范围条件,LIST/HASH分区只有等值/IN类条件能裁剪。
- PBE场景保持参数类型与分区键严格一致,否则计划期保留的表达式在执行期也难以收敛。
- 分区数量适度: 裁剪收益在分区数为几十到几百时最明显;分区过多时Append子计划与分区元数据打开的固定开销会摊薄收益(⚓待锚: 分区数上限与计划生成耗时的量化关系)。

---

## 总结

- 并行查询的核心旋钮是并行度: `max_parallel_workers_per_gather` 控制单个Gather的Worker上限,小于 `min_parallel_table_scan_size` 的表不触发并行;调优路径是"串行基线 → 逐步加并行度 → 取拐点"。
- 分区表选型: 时间序列用RANGE,枚举离散值用LIST,均匀打散用HASH;分区键必须与高频谓词对齐,否则裁剪无从谈起。
- openGauss/VastBase分区裁剪的真实入口是 `allpaths.cpp` 的 `set_plain_rel_size`: 裁剪结果挂 `rel->pruning_result`,剩余分区数 `rel->partItrs` 由范围分区位图与interval分区位图成员数相加得出。
- 裁剪分两级: 计划期静态裁剪(常量条件)与执行期运行时裁剪(PBE参数走 `GetPartitionInfo`);LIST/HASH走等值语义的 `partitionEqualPruningWalker`,RANGE走边界收敛的 `partitionPruningWalker`。
- 裁剪失效的典型形态: 分区键被函数包裹、分区键侧隐式类型转换、谓词不含分区键;OR条件按分支分别收敛再取并集。
- `partition_page_estimation` 修正裁剪后的页数估计,`partition_iterator_elimination` 在单分区时消除迭代层级;两者默认关闭,分区表密集的现场可逐项评估。
- 分区裁剪与并行扫描可叠加: 先把扫描集缩小到少量分区,再对剩余分区并行扫描,收益相乘。

## 版本注记

- 2026-11-05 断尾补齐,原文 500 行截断。原文截断于"33.2 分区裁剪"的源码分析代码块中(`rel->live_partitions = live_partitions;` 处);本次补完该代码块,并顺势补写分区裁剪收尾(两级裁剪机制、失效场景、参数开关)。
- 原文目录中的34~42节(物化视图、hint与计划绑定、实战调优案例、统计信息/内存/代价模型内部机制)未出现在截断前的正文中,不在本次补写范围内。
- 源码引用一律为 文件名+函数名,基于 openGauss-server-v6.0.0 源码树逐项核对;未核实处标注⚓待锚。

## 来源

- 源码基线: openGauss-server-v6.0.0 — src/gausskernel/optimizer/path/allpaths.cpp(set_plain_rel_size)、src/gausskernel/optimizer/util/pruning.cpp(partitionPruningForRestrictInfo/partitionPruningWalker/partitionEqualPruningWalker/GetPartitionInfo)、src/include/utils/partitionmap_gs.h(PruningResult)、src/gausskernel/optimizer/plan/createplan.cpp(isPartiteratorElimination)、src/gausskernel/runtime/executor/nodeSeqscan.cpp(GetPartitionPruningResultInInitScanRelation)、src/common/backend/utils/misc/guc/guc_sql.cpp(partition_page_estimation/partition_iterator_elimination注册)
- 原文 2026-04 学习档案(优化器系列第七~九部分,500行截断版)
