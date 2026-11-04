     1|# VastBase/openGauss 优化器完整指南 (最终部分)
     2|
     3|> 继续前序文档...  
     4|> 完成数据仓库优化、高并发优化、内部机制剖析
     5|
     6|---
     7|
     8|## 38. 数据仓库优化
     9|
    10|### 38.1 列存优化
    11|
    12|**行存 vs 列存**:
    13|
    14|```sql
    15|-- 1. 行存表 (Row-Store)
    16|-- 默认存储方式
    17|-- 适合: OLTP (事务处理)
    18|CREATE TABLE orders_row (
    19|    order_id INT,
    20|    customer_id INT,
    21|    product_id INT,
    22|    order_date DATE,
    23|    amount NUMERIC
    24|);
    25|
    26|-- 2. 列存表 (Column-Store)
    27|-- openGauss特有
    28|-- 适合: OLAP (分析处理)
    29|CREATE TABLE orders_col (
    30|    order_id INT,
    31|    customer_id INT,
    32|    product_id INT,
    33|    order_date DATE,
    34|    amount NUMERIC
    35|) WITH (ORIENTATION=COLUMN);
    36|
    37|-- 列存优势:
    38|-- - 只读取需要的列
    39|-- - 高压缩比
    40|-- - 适合聚合查询
    41|--
    42|-- 列存劣势:
    43|// - 不适合单行查询
    44|// - 不适合频繁UPDATE/DELETE
    45|// - 不适合大量INSERT
    46|```
    47|
    48|**列存表优化**:
    49|
    50|```sql
    51|-- 场景: 分析查询 (列存表优势)
    52|CREATE TABLE sales_fact_columnar (
    53|    sale_id INT,
    54|    product_id INT,
    55|    customer_id INT,
    56|    sale_date DATE,
    57|    sale_time TIMESTAMP,
    58|    store_id INT,
    59|    quantity INT,
    60|    unit_price NUMERIC(10,2),
    61|    total_amount NUMERIC(10,2),
    62|    discount NUMERIC(10,2),
    63|    net_amount NUMERIC(10,2),
    64|    salesperson_id INT,
    65|    region_id INT,
    66|    channel VARCHAR(50)
    67|) WITH (ORIENTATION=COLUMN);
    68|
    69|-- 插入数据 (批量)
    70|INSERT INTO sales_fact_columnar
    71|SELECT ...;
    72|
    73|-- 创建压缩
    74|-- openGauss自动压缩列存数据
    75|-- 可以指定压缩级别
    76|ALTER TABLE sales_fact_columnar SET (compression=middle);  -- low/middle/high
    77|
    78|-- 查询1: 只读取部分列 (列存优势)
    79|EXPLAIN (ANALYZE)
    80|SELECT product_id, SUM(total_amount) 
    81|FROM sales_fact_columnar 
    82|WHERE sale_date >= '2024-01-01'
    83|GROUP BY product_id;
    84|
    85|-- 执行计划:
    86|-- CStore Scan on sales_fact_columnar
    87|--   只读取 product_id 和 total_amount 列
    88|--   不读取其他列 (节省I/O)
    89|
    90|-- 查询2: 聚合查询 (列存优势)
    91|EXPLAIN (ANALYZE)
    92|SELECT 
    93|    region_id,
    94|    SUM(net_amount) AS total_sales,
    95|    AVG(net_amount) AS avg_sales,
    96|    COUNT(*) AS sale_count
    97|FROM sales_fact_columnar
    98|WHERE sale_date >= '2024-01-01'
    99|GROUP BY region_id;
   100|
   101|-- 列存优势:
   102|-- - 高压缩比 → 减少I/O
   103|-- - 列式存储 → CPU缓存友好
   104|-- - 向量化执行 → SIMD加速
   105|
   106|-- 查询3: 单行查询 (列存劣势)
   107|EXPLAIN (ANALYZE)
   108|SELECT * FROM sales_fact_columnar WHERE sale_id = 12345;
   109|
   110|-- 行存表更优:
   111|CREATE TABLE sales_fact_row (
   112|    ...
   113|) WITH (ORIENTATION=ROW);
   114|
   115|EXPLAIN (ANALYZE)
   116|SELECT * FROM sales_fact_row WHERE sale_id = 12345;
   117|```
   118|
   119|### 38.2 星型模型优化
   120|
   121|**维度表 vs 事实表**:
   122|
   123|```sql
   124|-- 1. 维度表 (小表,行存)
   125|CREATE TABLE dim_product (
   126|    product_id INT PRIMARY KEY,
   127|    product_name VARCHAR(200),
   128|    category VARCHAR(100),
   129|    subcategory VARCHAR(100),
   130|    brand VARCHAR(100)
   131|) WITH (ORIENTATION=ROW);
   132|
   133|CREATE TABLE dim_customer (
   134|    customer_id INT PRIMARY KEY,
   135|    customer_name VARCHAR(200),
   136|    city VARCHAR(100),
   137|    province VARCHAR(100),
   138|    country VARCHAR(100),
   139|    segment VARCHAR(100)
   140|) WITH (ORIENTATION=ROW);
   141|
   142|CREATE TABLE dim_time (
   143|    time_id INT PRIMARY KEY,
   144|    date DATE,
   145|    year INT,
   146|    quarter INT,
   147|    month INT,
   148|    day INT,
   149|    week INT,
   150|    day_of_week VARCHAR(20)
   151|) WITH (ORIENTATION=ROW);
   152|
   153|-- 2. 事实表 (大表,列存)
   154|CREATE TABLE fact_sales (
   155|    sales_id BIGINT,
   156|    product_id INT,
   157|    customer_id INT,
   158|    time_id INT,
   159|    store_id INT,
   160|    quantity INT,
   161|    unit_price NUMERIC(10,2),
   162|    total_amount NUMERIC(10,2),
   163|    discount NUMERIC(10,2),
   164|    net_amount NUMERIC(10,2)
   165|) WITH (ORIENTATION=COLUMN);
   166|
   167|-- 创建外键 (可选,数据仓库通常不用外键)
   168|-- ALTER TABLE fact_sales ADD CONSTRAINT fk_product 
   169|--     FOREIGN KEY (product_id) REFERENCES dim_product(product_id);
   170|
   171|-- 3. 星型模型查询
   172|-- 典型分析查询
   173|SELECT 
   174|    p.category,
   175|    t.year,
   176|    t.quarter,
   177|    SUM(f.net_amount) AS total_sales,
   178|    COUNT(*) AS sales_count
   179|FROM fact_sales f
   180|JOIN dim_product p ON f.product_id = p.product_id
   181|JOIN dim_time t ON f.time_id = t.time_id
   182|WHERE t.year = 2024
   183|GROUP BY p.category, t.year, t.quarter
   184|ORDER BY total_sales DESC;
   185|
   186|-- 优化1: 维度表使用索引
   187|CREATE INDEX idx_dim_product_category ON dim_product(category);
   188|CREATE INDEX idx_dim_time_year ON dim_time(year);
   189|
   190|-- 优化2: 事实表使用分区
   191|CREATE TABLE fact_sales_partitioned (
   192|    ...
   193|) WITH (ORIENTATION=COLUMN)
   194|PARTITION BY RANGE (time_id);
   195|
   196|-- 优化3: 使用物化视图
   197|CREATE MATERIALIZED VIEW mv_sales_category_quarter AS
   198|SELECT 
   199|    p.category,
   200|    t.year,
   201|    t.quarter,
   202|    SUM(f.net_amount) AS total_sales,
   203|    COUNT(*) AS sales_count
   204|FROM fact_sales f
   205|JOIN dim_product p ON f.product_id = p.product_id
   206|JOIN dim_time t ON f.time_id = t.time_id
   207|GROUP BY p.category, t.year, t.quarter;
   208|
   209|CREATE INDEX idx_mv_category_year ON mv_sales_category_quarter(category, year);
   210|
   211|-- 定期刷新
   212|REFRESH MATERIALIZED VIEW CONCURRENTLY mv_sales_category_quarter;
   213|
   214|-- 优化4: 使用列压缩
   215|ALTER TABLE fact_sales SET (compression=high);
   216|
   217|-- 优化5: 调整内存
   218|SET work_mem = '1GB';  -- 大排序/大哈希
   219|SET hash_mem = '512MB';  -- 大哈希表
   220|```
   221|
   222|### 38.3 聚合表优化
   223|
   224|**预聚合策略**:
   225|
   226|```sql
   227|-- 1. 日级聚合表
   228|CREATE TABLE agg_sales_daily (
   229|    date DATE,
   230|    product_id INT,
   231|    customer_id INT,
   232|    store_id INT,
   233|    sales_count BIGINT,
   234|    sales_amount NUMERIC(20,2),
   235|    avg_quantity NUMERIC(10,2),
   236|    avg_unit_price NUMERIC(10,2)
   237|) WITH (ORIENTATION=COLUMN);
   238|
   239|-- 创建索引
   240|CREATE INDEX idx_agg_daily_date ON agg_sales_daily(date);
   241|CREATE INDEX idx_agg_daily_product ON agg_sales_daily(product_id);
   242|
   243|-- 创建物化视图 (自动刷新)
   244|CREATE MATERIALIZED VIEW mv_agg_sales_daily AS
   245|SELECT 
   246|    sale_date,
   247|    product_id,
   248|    customer_id,
   249|    store_id,
   250|    COUNT(*) AS sales_count,
   251|    SUM(net_amount) AS sales_amount,
   252|    AVG(quantity) AS avg_quantity,
   253|    AVG(unit_price) AS avg_unit_price
   254|FROM sales_fact_columnar
   255|GROUP BY sale_date, product_id, customer_id, store_id;
   256|
   257|-- 2. 周级聚合表
   258|CREATE MATERIALIZED VIEW mv_agg_sales_weekly AS
   259|SELECT 
   260|    date_trunc('week', sale_date) AS week_start,
   261|    product_id,
   262|    COUNT(*) AS sales_count,
   263|    SUM(net_amount) AS sales_amount
   264|FROM sales_fact_columnar
   265|GROUP BY date_trunc('week', sale_date), product_id;
   266|
   267|-- 3. 月级聚合表
   268|CREATE MATERIALIZED VIEW mv_agg_sales_monthly AS
   269|SELECT 
   270|    date_trunc('month', sale_date) AS month_start,
   271|    product_id,
   272|    COUNT(*) AS sales_count,
   273|    SUM(net_amount) AS sales_amount
   274|FROM sales_fact_columnar
   275|GROUP BY date_trunc('month', sale_date), product_id;
   276|
   277|-- 4. 季度级聚合表
   278|CREATE MATERIALIZED VIEW mv_agg_sales_quarterly AS
   279|SELECT 
   280|    date_trunc('quarter', sale_date) AS quarter_start,
   281|    product_id,
   282|    COUNT(*) AS sales_count,
   283|    SUM(net_amount) AS sales_amount
   284|FROM sales_fact_columnar
   285|GROUP BY date_trunc('quarter', sale_date), product_id;
   286|
   287|-- 5. 年级聚合表
   288|CREATE MATERIALIZED VIEW mv_agg_sales_yearly AS
   289|SELECT 
   290|    date_trunc('year', sale_date) AS year_start,
   291|    product_id,
   292|    COUNT(*) AS sales_count,
   293|    SUM(net_amount) AS sales_amount
   294|FROM sales_fact_columnar
   295|GROUP BY date_trunc('year', sale_date), product_id;
   296|
   297|-- 查询路由: 根据查询粒度选择合适的聚合表
   298|-- 日级查询
   299|SELECT * FROM mv_agg_sales_daily 
   300|WHERE date = '2024-01-15' AND product_id = 123;
   301|
   302|-- 周级查询
   303|SELECT * FROM mv_agg_sales_weekly 
   304|WHERE week_start = '2024-01-15' AND product_id = 123;
   305|
   306|-- 月级查询
   307|SELECT * FROM mv_agg_sales_monthly 
   308|WHERE month_start = '2024-01-01' AND product_id = 123;
   309|```
   310|
   311|---
   312|
   313|## 39. 高并发优化
   314|
   315|### 39.1 连接池优化
   316|
   317|**连接池原理**:
   318|
   319|```sql
   320|-- 1. 连接池配置
   321|-- postgresql.conf:
   322|max_connections = 200  -- 最大连接数
   323|superuser_reserved_connections = 3  -- 超级用户保留连接
   324|
   325|-- 2. 使用连接池中间件
   326|-- PgBouncer配置:
   327|-- 
   328|-- [databases]
   329|-- mydb = host=localhost port=6666 dbname=mydb
   330|--
   331|-- [pgbouncer]
   332|-- pool_mode = transaction
   333|-- max_client_conn = 1000
   334|-- default_pool_size = 50
   335|-- reserve_pool_size = 10
   336|-- reserve_pool_timeout = 3
   337|-- server_lifetime = 3600
   338|-- server_idle_timeout = 600
   339|
   340|-- 3. 连接池模式
   341|-- 
   342|-- Session pooling (会话池):
   343|--   客户端连接 → 服务器连接 (一对一)
   344|--   适合: 长连接应用
   345|--
   346|-- Transaction pooling (事务池):
   347|--   客户端连接 → 服务器连接 (多对多,事务结束后释放)
   348|--   适合: 短事务应用
   349|--
   350|-- Statement pooling (语句池):
   351|--   客户端连接 → 服务器连接 (多对多,语句结束后释放)
   352|--   适合: 自动提交应用
   353|```
   354|
   355|### 39.2 锁优化
   356|
   357|**锁等待分析**:
   358|
   359|```sql
   360|-- 1. 查看当前锁
   361|SELECT 
   362|    pid,
   363|    usename,
   364|    pg_blocking_pids(pid) AS blocked_by,
   365|    query as blocked_query,
   366|    state
   367|FROM pg_stat_activity
   368|WHERE cardinality(pg_blocking_pids(pid)) > 0;
   369|
   370|-- 2. 查看锁等待
   371|SELECT 
   372|    t.pid,
   373|    t.usename,
   374|    t.query,
   375|    t.state,
   376|    l.locktype,
   377|    l.relation::regclass,
   378|    l.mode,
   379|    l.granted
   380|FROM pg_stat_activity t
   381|JOIN pg_locks l ON t.pid = l.pid
   382|WHERE t.state = 'active'
   383|  AND l.granted = false
   384|ORDER BY t.query_start;
   385|
   386|-- 3. 查看锁持有者
   387|SELECT 
   388|    t.pid,
   389|    t.usename,
   390|    t.query,
   391|    t.state,
   392|    l.locktype,
   393|    l.relation::regclass,
   394|    l.mode,
   395|    l.granted
   396|FROM pg_stat_activity t
   397|JOIN pg_locks l ON t.pid = l.pid
   398|WHERE t.state = 'active'
   399|  AND l.granted = true
   400|ORDER BY t.query_start;
   401|
   402|-- 4. 终止阻塞进程
   403|SELECT pg_terminate_backend(pid);  -- 终止指定进程
   404|SELECT pg_cancel_backend(pid);     -- 取消指定进程的当前查询
   405|
   406|-- 5. 锁超时设置
   407|SET lock_timeout = '5s';  -- 锁等待超时5秒
   408|
   409|-- 测试
   410|BEGIN;
   411|SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
   412|
   413|-- 另一个会话:
   414|SET lock_timeout = '5s';
   415|SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
   416|-- ERROR: canceling statement due to lock timeout
   417|```
   418|
   419|**锁优化策略**:
   420|
   421|```sql
   422|-- 1. 减少锁粒度
   423|-- 错误: 锁整表
   424|SELECT * FROM orders FOR UPDATE;
   425|
   426|-- 正确: 只锁需要的行
   427|SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
   428|
   429|-- 2. 缩短锁持有时间
   430|-- 错误: 长事务持有锁
   431|BEGIN;
   432|SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
   433|-- 执行一些耗时操作 (应用层处理)
   434|UPDATE orders SET amount = amount + 100 WHERE order_id = 123;
   435|COMMIT;
   436|
   437|-- 正确: 尽快释放锁
   438|BEGIN;
   439|SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
   440|UPDATE orders SET amount = amount + 100 WHERE order_id = 123;
   441|COMMIT;
   442|-- 应用层处理在事务外
   443|
   444|-- 3. 避免锁升级
   445|-- 行锁 → 页锁 → 表锁
   446|-- 尽量避免锁升级到表锁
   447|
   448|-- 4. 使用乐观锁
   449|-- 版本号机制
   450|CREATE TABLE orders_v2 (
   451|    order_id INT PRIMARY KEY,
   452|    amount NUMERIC,
   453|    version INT DEFAULT 0,
   454|    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
   455|);
   456|
   457|-- 更新时检查版本号
   458|UPDATE orders_v2 
   459|SET amount = amount + 100,
   460|    version = version + 1,
   461|    updated_at = CURRENT_TIMESTAMP
   462|WHERE order_id = 123
   463|  AND version = 5;  -- 检查版本号
   464|
   465|-- 如果version != 5,说明数据已被其他事务修改
   466|-- 应用层可以决定是否重试
   467|
   468|-- 5. 使用 Advisory Lock
   469|-- 应用级锁
   470|SELECT pg_advisory_lock(12345);  -- 获取锁
   471|-- 执行业务逻辑
   472|SELECT pg_advisory_unlock(12345);  -- 释放锁
   473|
   474|-- 或使用事务级Advisory Lock
   475|SELECT pg_advisory_xact_lock(12345);  -- 事务结束时自动释放
   476|```
   477|
   478|### 39.3 MVCC优化
   479|
   480|**MVCC (Multi-Version Concurrency Control)**:
   481|
   482|```sql
   483|-- 1. MVCC原理
   484|-- 读不阻塞写,写不阻塞读
   485|-- 
   486|-- 事务1 (读):
   487|BEGIN;
   488|SELECT * FROM orders WHERE order_id = 123;
   489|-- 看到的是事务开始时的快照
   490|
   491|-- 事务2 (写):
   492|BEGIN;
   493|UPDATE orders SET amount = amount + 100 WHERE order_id = 123;
   494|COMMIT;
   495|-- 事务2提交后,事务1仍然看到旧数据
   496|
   497|-- 事务1:
   498|SELECT * FROM orders WHERE order_id = 123;
   499|-- 仍然看到旧数据 (快照隔离)
   500|
   501|