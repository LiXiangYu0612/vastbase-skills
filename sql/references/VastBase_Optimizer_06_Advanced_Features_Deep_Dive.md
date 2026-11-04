     1|# VastBase/openGauss 优化器高级特性与实战完整指南
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 基于前序文档的深度分析  
     5|> 学习目标: 掌握高级优化技术、实战调优技巧
     6|
     7|---
     8|
     9|## 目录
    10|
    11|### 第七部分: 高级优化特性
    12|- [32. 并行查询优化](#32-并行查询优化)
    13|- [33. 分区表优化](#33-分区表优化)
    14|- [34. 物化视图优化](#34-物化视图优化)
    15|- [35. 查询 hint 与计划绑定](#35-查询-hint-与计划绑定)
    16|
    17|### 第八部分: 实战调优案例
    18|- [36. 慢查询诊断与优化](#36-慢查询诊断与优化)
    19|- [37. 复杂查询优化](#37-复杂查询优化)
    20|- [38. 数据仓库优化](#38-数据仓库优化)
    21|- [39. 高并发优化](#39-高并发优化)
    22|
    23|### 第九部分: 优化器内部机制
    24|- [40. 统计信息深度剖析](#40-统计信息深度剖析)
    25|- [41. 内存管理机制](#41-内存管理机制)
    26|- [42. 代价模型调优](#42-代价模型调优)
    27|
    28|---
    29|
    30|## 32. 并行查询优化
    31|
    32|### 32.1 并行查询原理
    33|
    34|**核心概念**:
    35|
    36|```sql
    37|-- 1. 并行度 (DOP: Degree of Parallelism)
    38|-- 指同时执行查询的工作线程数
    39|-- 影响因素:
    40|-- - max_parallel_workers_per_gather: 最大并行工作线程数 (默认: 2)
    41|-- - max_parallel_workers: 全局最大并行工作线程数 (默认: 8)
    42|-- - max_worker_processes: 最大工作进程数 (默认: 8)
    43|-- - 表大小: 小表不会并行扫描
    44|
    45|-- 2. 并行查询架构
    46|-- Leader进程:
    47|--   - 负责协调
    48|--   - 生成执行计划
    49|--   - 分配任务给Worker进程
    50|--   - 收集Worker结果
    51|--
    52|-- Worker进程:
    53|--   - 执行并行任务
    54|--   - 返回结果给Leader
    55|--
    56|-- 3. 并行执行节点
    57|-- - Parallel Seq Scan: 并行顺序扫描
    58|-- - Parallel Index Scan: 并行索引扫描
    59|// - Parallel Bitmap Scan: 并行位图扫描
    60|// - Parallel Hash Join: 并行哈希连接
    61|// - Parallel Aggregate: 并行聚合
    62|// - Gather: 收集Worker结果 (并行 → 串行)
    63|// - Gather Merge: 收集并合并Worker结果 (保持排序)
    64|```
    65|
    66|**源码分析**:
    67|
    68|```cpp
    69|// src/gausskernel/optimizer/path/costsize.cpp
    70|// cost_parallel_seqscan函数
    71|
    72|void cost_parallel_seqscan(
    73|    Path* path,
    74|    PlannerInfo* root,
    75|    RelOptInfo* baserel,
    76|    int dop)  // Degree of Parallelism
    77|{
    78|    Cost startup_cost = 0;
    79|    Cost run_cost = 0;
    80|    Cost cpu_per_tuple = 0;
    81|    
    82|    // 获取并行度
    83|    int parallel_workers = dop;
    84|    
    85|    // 计算每个Worker扫描的页数
    86|    double pages_per_worker = baserel->pages / parallel_workers;
    87|    
    88|    // 计算磁盘I/O成本 (并行)
    89|    run_cost += seq_page_cost * pages_per_worker;
    90|    
    91|    // 计算CPU成本 (并行)
    92|    cpu_per_tuple = cpu_tuple_cost + qpqual_cost.per_tuple;
    93|    run_cost += cpu_per_tuple * baserel->tuples / parallel_workers;
    94|    
    95|    // 计算Gather成本 (收集Worker结果)
    96|    Cost gather_cost = u_sess->opt_cxt.smp_thread_cost * (parallel_workers - 1);
    97|    startup_cost += gather_cost;
    98|    
    99|    path->startup_cost = startup_cost;
   100|    path->total_cost = startup_cost + run_cost;
   101|    path->rows = baserel->rows;
   102|    path->parallel_degree = parallel_workers;
   103|}
   104|
   105|// src/gausskernel/optimizer/path/allpaths.cpp
   106|// create_parallel_paths函数
   107|
   108|void create_parallel_paths(PlannerInfo* root, RelOptInfo* rel)
   109|{
   110|    // 检查是否可以并行扫描
   111|    if (!enable_parallel_query) {
   112|        return;
   113|    }
   114|    
   115|    // 检查表大小 (太小不值得并行)
   116|    if (rel->pages < min_parallel_table_scan_size) {
   117|        return;
   118|    }
   119|    
   120|    // 计算最大并行度
   121|    int max_parallel_workers = max_parallel_workers_per_gather;
   122|    
   123|    // 根据表大小估算合适的并行度
   124|    // 假设每个Worker至少扫描1MB数据
   125|    int table_size_mb = rel->pages * 8 / 1024;  // 假设页大小8KB
   126|    int estimated_workers = Min(table_size_mb, max_parallel_workers);
   127|    
   128|    // 生成并行路径
   129|    for (int dop = 2; dop <= estimated_workers; dop++) {
   130|        // 创建并行顺序扫描路径
   131|        ParallelPath* ppath = create_parallel_seqscan_path(root, rel, dop);
   132|        
   133|        // 添加到路径列表
   134|        add_path(rel, (Path*) ppath);
   135|    }
   136|}
   137|```
   138|
   139|### 32.2 并行查询配置与调优
   140|
   141|**基础配置**:
   142|
   143|```sql
   144|-- 1. 启用并行查询
   145|SET max_parallel_workers_per_gather = 4;  -- 会话级别
   146|-- 或
   147|ALTER DATABASE dbname SET max_parallel_workers_per_gather = 4;
   148|
   149|-- 2. 设置全局并行工作线程数
   150|SET max_parallel_workers = 16;
   151|-- 或修改 postgresql.conf:
   152|-- max_parallel_workers = 16
   153|
   154|-- 3. 设置最小并行表大小
   155|SET min_parallel_table_scan_size = '8MB';
   156|-- 表小于8MB时,不使用并行扫描
   157|
   158|-- 4. 设置最小并行索引扫描大小
   159|SET min_parallel_index_scan_size = '512kB';
   160|-- 索引小于512kB时,不使用并行索引扫描
   161|
   162|-- 5. 查看并行度
   163|SHOW max_parallel_workers_per_gather;
   164|SHOW max_parallel_workers;
   165|SHOW max_worker_processes;
   166|```
   167|
   168|**实战测试**:
   169|
   170|```sql
   171|-- 创建大表
   172|CREATE TABLE big_table (
   173|    id INT,
   174|    data TEXT,
   175|    value NUMERIC
   176|);
   177|
   178|-- 插入1亿行数据
   179|INSERT INTO big_table
   180|SELECT 
   181|    generate_series(1, 100000000),
   182|    repeat('x', 100),
   183|    (random() * 1000)::numeric;
   184|
   185|ANALYZE big_table;
   186|
   187|-- 测试1: 顺序扫描 (串行 vs 并行)
   188|EXPLAIN (ANALYZE, TIMING, BUFFERS)
   189|SELECT COUNT(*) FROM big_table;
   190|
   191|-- 串行执行 (默认):
   192|-- Seq Scan on big_table  (cost=0.00..1234567.89 rows=100000000 width=0)
   193|-- 
   194|-- 并行执行 (设置并行度):
   195|SET max_parallel_workers_per_gather = 4;
   196|
   197|EXPLAIN (ANALYZE, TIMING, BUFFERS)
   198|SELECT COUNT(*) FROM big_table;
   199|
   200|-- Gather  (cost=0.00..123456.78 rows=100000000 width=0)
   201|//   Workers Planned: 4
   202|//   Workers Launched: 4
   203|//   ->  Parallel Seq Scan on big_table  (cost=0.00..308641.97 rows=25000000 width=0)
   204|//
   205|-- 观察:
   206|// - "Workers Planned: 4": 计划使用4个Worker
   207|// - "Workers Launched: 4": 实际启动4个Worker
   208|// - "rows=25000000": 每个Worker扫描2500万行
   209|
   210|-- 测试2: 聚合查询 (串行 vs 并行)
   211|EXPLAIN (ANALYZE, TIMING, BUFFERS)
   212|SELECT value, COUNT(*) FROM big_table GROUP BY value;
   213|
   214|-- 并行聚合:
   215|-- Gather  (cost=0.00..234567.89 rows=1000 width=12)
   216|//   Workers Planned: 4
   217|//   ->  Parallel HashAggregate  (cost=0.00..234567.89 rows=1000 width=12)
   218|//         Group Key: value
   219|//         ->  Parallel Seq Scan on big_table  (cost=0.00..123456.78 rows=100000000 width=12)
   220|//
   221|-- 观察:
   222|// - "Parallel HashAggregate": 并行哈希聚合
   223|// - 每个Worker独立计算部分聚合结果
   224|// - Leader进程合并Worker的聚合结果
   225|
   226|-- 测试3: JOIN查询 (串行 vs 并行)
   227|CREATE TABLE another_big_table (
   228|    id INT,
   229|    info TEXT
   230|);
   231|
   232|INSERT INTO another_big_table
   233|SELECT generate_series(1, 50000000), 'info';
   234|
   235|ANALYZE another_big_table;
   236|
   237|EXPLAIN (ANALYZE, TIMING, BUFFERS)
   238|SELECT * FROM big_table b
   239|JOIN another_big_table a ON b.id = a.id;
   240|
   241|-- 并行JOIN:
   242|-- Gather  (cost=0.00..456789.01 rows=50000000 width=120)
   243|//   Workers Planned: 4
   244|//   ->  Parallel Hash Join  (cost=0.00..456789.01 rows=12500000 width=120)
   245|//         Hash Cond: (b.id = a.id)
   246|//         ->  Parallel Seq Scan on big_table b  (cost=0.00..123456.78 rows=25000000 width=60)
   247|//         ->  Parallel Hash  (cost=0.00..123456.78 rows=25000000 width=60)
   248|//               ->  Parallel Seq Scan on another_big_table a  (cost=0.00..123456.78 rows=25000000 width=60)
   249|//
   250|-- 观察:
   251|// - "Parallel Hash Join": 并行哈希连接
   252|// - 每个Worker构建部分哈希表
   253|// - 每个Worker探测部分外表
   254|```
   255|
   256|### 32.3 并行查询性能调优
   257|
   258|**调优策略**:
   259|
   260|```sql
   261|-- 1. 选择合适的并行度
   262|-- 原则: 并行度 ≤ CPU核心数
   263|-- 
   264|-- 查看CPU核心数:
   265|SHOW cpu_count;  -- 假设16核
   266|
   267|-- 设置并行度
   268|SET max_parallel_workers_per_gather = 8;  -- 使用一半CPU核心
   269|
   270|-- 测试不同并行度的性能
   271|SET max_parallel_workers_per_gather = 2;
   272|EXPLAIN (ANALYZE) SELECT COUNT(*) FROM big_table;
   273|
   274|SET max_parallel_workers_per_gather = 4;
   275|EXPLAIN (ANALYZE) SELECT COUNT(*) FROM big_table;
   276|
   277|SET max_parallel_workers_per_gather = 8;
   278|EXPLAIN (ANALYZE) SELECT COUNT(*) FROM big_table;
   279|
   280|-- 选择执行时间最短的并行度
   281|
   282|-- 2. 调整最小并行表大小
   283|-- 原则: 太小的表不值得并行 (并行开销 > 并行收益)
   284|--
   285|-- 查看表大小
   286|SELECT 
   287|    pg_size_pretty(pg_relation_size('big_table')) AS table_size;
   288|
   289|-- 调整最小并行表大小
   290|SET min_parallel_table_scan_size = '1MB';  -- 更小的表也可以并行
   291|SET min_parallel_table_scan_size = '100MB'; -- 更大的表才并行
   292|
   293|-- 3. 避免过度并行
   294|-- 问题: 太多并发查询,每个都用并行,会导致CPU争抢
   295|--
   296|-- 解决: 设置全局并行工作线程数上限
   297|SET max_parallel_workers = 16;
   298|
   299|-- 4. 监控并行查询
   300|-- 查看当前运行的并行查询
   301|SELECT 
   302|    pid,
   303|    query,
   304|    wait_event,
   305|    state
   306|FROM pg_stat_activity
   307|WHERE query LIKE '%Parallel%'
   308|  AND state = 'active';
   309|
   310|-- 查看并行Worker统计
   311|SELECT 
   312|    SUM(plan_parallel_workers) AS planned_workers,
   313|    SUM(plan_parallel_workers - actual_parallel_workers) AS not_launched_workers
   314|FROM pg_stat_statements;
   315|```
   316|
   317|---
   318|
   319|## 33. 分区表优化
   320|
   321|### 33.1 分区表原理
   322|
   323|**分区类型**:
   324|
   325|```sql
   326|-- 1. 范围分区 (Range Partition)
   327|-- 适合: 时间序列数据、数值范围数据
   328|--
   329|-- 示例: 按日期范围分区
   330|CREATE TABLE orders_range (
   331|    order_id INT,
   332|    order_date DATE,
   333|    customer_id INT,
   334|    amount NUMERIC
   335|) PARTITION BY RANGE (order_date);
   336|
   337|-- 创建分区
   338|CREATE TABLE orders_2024_q1 PARTITION OF orders_range
   339|    FOR VALUES FROM ('2024-01-01') TO ('2024-04-01');
   340|
   341|CREATE TABLE orders_2024_q2 PARTITION OF orders_range
   342|    FOR VALUES FROM ('2024-04-01') TO ('2024-07-01');
   343|
   344|CREATE TABLE orders_2024_q3 PARTITION OF orders_range
   345|    FOR VALUES FROM ('2024-07-01') TO ('2024-10-01');
   346|
   347|CREATE TABLE orders_2024_q4 PARTITION OF orders_range
   348|    FOR VALUES FROM ('2024-10-01') TO ('2025-01-01');
   349|
   350|-- 2. 列表分区 (List Partition)
   351|-- 适合: 枚举值、离散值
   352|--
   353|-- 示例: 按城市分区
   354|CREATE TABLE customers_list (
   355|    customer_id INT,
   356|    city VARCHAR(100),
   357|    customer_name VARCHAR(200)
   358|) PARTITION BY LIST (city);
   359|
   360|-- 创建分区
   361|CREATE TABLE customers_beijing PARTITION OF customers_list
   362|    FOR VALUES IN ('Beijing', 'Tianjin', 'Shijiazhuang');
   363|
   364|CREATE TABLE customers_shanghai PARTITION OF customers_list
   365|    FOR VALUES IN ('Shanghai', 'Hangzhou', 'Nanjing');
   366|
   367|CREATE TABLE customers_guangzhou PARTITION OF customers_list
   368|    FOR VALUES IN ('Guangzhou', 'Shenzhen', 'Foshan');
   369|
   370|CREATE TABLE customers_other PARTITION OF customers_list
   371|    DEFAULT;  -- 其他城市
   372|
   373|-- 3. 哈希分区 (Hash Partition)
   374|-- 适合: 均匀分布数据
   375|--
   376|-- 示例: 按用户ID哈希分区
   377|CREATE TABLE orders_hash (
   378|    order_id INT,
   379|    customer_id INT,
   380|    order_date DATE,
   381|    amount NUMERIC
   382|) PARTITION BY HASH (customer_id);
   383|
   384|-- 创建4个分区
   385|CREATE TABLE orders_hash_0 PARTITION OF orders_hash
   386|    FOR VALUES WITH (MODULUS 4, REMAINDER 0);
   387|
   388|CREATE TABLE orders_hash_1 PARTITION OF orders_hash
   389|    FOR VALUES WITH (MODULUS 4, REMAINDER 1);
   390|
   391|CREATE TABLE orders_hash_2 PARTITION OF orders_hash
   392|    FOR VALUES WITH (MODULUS 4, REMAINDER 2);
   393|
   394|CREATE TABLE orders_hash_3 PARTITION OF orders_hash
   395|    FOR VALUES WITH (MODULUS 4, REMAINDER 3);
   396|```
   397|
   398|### 33.2 分区裁剪 (Partition Pruning)
   399|
   400|**原理**: 优化器根据WHERE条件,只扫描相关分区,避免全表扫描
   401|
   402|```sql
   403|-- 示例: 分区裁剪
   404|CREATE TABLE orders_2024 (
   405|    order_id INT,
   406|    order_date DATE,
   407|    amount NUMERIC
   408|) PARTITION BY RANGE (order_date);
   409|
   410|-- 创建12个分区 (按月)
   411|CREATE TABLE orders_2024_01 PARTITION OF orders_2024
   412|    FOR VALUES FROM ('2024-01-01') TO ('2024-02-01');
   413|
   414|CREATE TABLE orders_2024_02 PARTITION OF orders_2024
   415|    FOR VALUES FROM ('2024-02-01') TO ('2024-03-01');
   416|
   417|-- ... (省略其他月份)
   418|
   419|CREATE TABLE orders_2024_12 PARTITION OF orders_2024
   420|    FOR VALUES FROM ('2024-12-01') TO ('2025-01-01');
   421|
   422|-- 插入数据
   423|INSERT INTO orders_2024
   424|SELECT 
   425|    generate_series(1, 1000000),
   426|    CURRENT_DATE - (random() * 365)::int,
   427|    (random() * 1000)::numeric;
   428|
   429|ANALYZE orders_2024;
   430|
   431|-- 查询1: 分区裁剪生效
   432|EXPLAIN
   433|SELECT * FROM orders_2024
   434|WHERE order_date = '2024-01-15';
   435|
   436|-- 执行计划:
   437|-- Seq Scan on orders_2024_01  (cost=0.00..123.45 rows=1000 width=16)
   438|//   Filter: (order_date = '2024-01-15'::date)
   439|//
   440|-- 观察: 只扫描orders_2024_01分区,其他11个分区被裁剪
   441|
   442|-- 查询2: 跨分区查询
   443|EXPLAIN
   444|SELECT * FROM orders_2024
   445|WHERE order_date BETWEEN '2024-03-01' AND '2024-05-31';
   446|
   447|-- 执行计划:
   448|-- Append  (cost=0.00..456.78 rows=3000 width=16)
   449|//   ->  Seq Scan on orders_2024_03  (cost=0.00..123.45 rows=1000 width=16)
   450|//         Filter: (order_date >= '2024-03-01'::date AND order_date <= '2024-05-31'::date)
   451|//   ->  Seq Scan on orders_2024_04  (cost=0.00..123.45 rows=1000 width=16)
   452|//         Filter: (order_date >= '2024-03-01'::date AND order_date <= '2024-05-31'::date)
   453|//   ->  Seq Scan on orders_2024_05  (cost=0.00..123.45 rows=1000 width=16)
   454|//         Filter: (order_date >= '2024-03-01'::date AND order_date <= '2024-05-31'::date)
   455|//
   456|-- 观察: 只扫描3个分区 (3月、4月、5月),其他9个分区被裁剪
   457|
   458|-- 查询3: 无法分区裁剪
   459|EXPLAIN
   460|SELECT * FROM orders_2024
   461|WHERE amount > 500;
   462|
   463|-- 执行计划:
   464|-- Append  (cost=0.00..1234.56 rows=10000 width=16)
   465|//   ->  Seq Scan on orders_2024_01  (cost=0.00..123.45 rows=833 width=16)
   466|//         Filter: (amount > 500::numeric)
   467|//   ->  Seq Scan on orders_2024_02  (cost=0.00..123.45 rows=833 width=16)
   468|//         Filter: (amount > 500::numeric)
   469|//   ... (扫描所有12个分区)
   470|//
   471|-- 观察: 必须扫描所有12个分区 (WHERE条件不包含分区键)
   472|```
   473|
   474|**源码分析**:
   475|
   476|```cpp
   477|// src/gausskernel/optimizer/path/allpaths.cpp
   478|// prune_partitions函数
   479|
   480|void prune_partitions(PlannerInfo* root, RelOptInfo* rel)
   481|{
   482|    // 获取分区表信息
   483|    PartitionPruneContext* context = &rel->partprune_context;
   484|    
   485|    // 获取分区约束条件
   486|    List* partition_clauses = extract_partition_clauses(root, rel);
   487|    
   488|    // 遍历所有分区
   489|    List* live_partitions = NIL;
   490|    foreach(lc, context->partitions) {
   491|        Partition* part = (Partition*) lfirst(lc);
   492|        
   493|        // 检查分区是否匹配约束条件
   494|        if (partition_matches_constraint(part, partition_clauses)) {
   495|            live_partitions = lappend(live_partitions, part);
   496|        }
   497|    }
   498|    
   499|    // 只保留匹配的分区
   500|    rel->live_partitions = live_partitions;
   501|

---
> ⚓ **截断注记（2026-11-05 审计）**：本篇在历史迁移时截断于 500 行，尾部
> 小节缺失（原全量件已不可寻，待重写补齐——挂账 ROADMAP）；行号式清单为
> 源码摘录，无独立 file:line 锚。
