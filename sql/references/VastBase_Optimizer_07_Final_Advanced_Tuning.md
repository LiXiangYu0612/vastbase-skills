# VastBase/openGauss 优化器完整指南 (最终部分)

> 源码版本: openGauss-server-v6.0.0  
> 聚焦: 数据仓库优化、高并发优化、隔离级别与调优收尾

---

## 38. 数据仓库优化

### 38.1 列存优化

**行存 vs 列存**:

```sql
-- 1. 行存表 (Row-Store)
-- 默认存储方式
-- 适合: OLTP (事务处理)
CREATE TABLE orders_row (
    order_id INT,
    customer_id INT,
    product_id INT,
    order_date DATE,
    amount NUMERIC
);

-- 2. 列存表 (Column-Store)
-- openGauss特有
-- 适合: OLAP (分析处理)
CREATE TABLE orders_col (
    order_id INT,
    customer_id INT,
    product_id INT,
    order_date DATE,
    amount NUMERIC
) WITH (ORIENTATION=COLUMN);

-- 列存优势:
-- - 只读取需要的列
-- - 高压缩比
-- - 适合聚合查询
--
-- 列存劣势:
// - 不适合单行查询
// - 不适合频繁UPDATE/DELETE
// - 不适合大量INSERT
```

**列存表优化**:

```sql
-- 场景: 分析查询 (列存表优势)
CREATE TABLE sales_fact_columnar (
    sale_id INT,
    product_id INT,
    customer_id INT,
    sale_date DATE,
    sale_time TIMESTAMP,
    store_id INT,
    quantity INT,
    unit_price NUMERIC(10,2),
    total_amount NUMERIC(10,2),
    discount NUMERIC(10,2),
    net_amount NUMERIC(10,2),
    salesperson_id INT,
    region_id INT,
    channel VARCHAR(50)
) WITH (ORIENTATION=COLUMN);

-- 插入数据 (批量)
INSERT INTO sales_fact_columnar
SELECT ...;

-- 创建压缩
-- openGauss自动压缩列存数据
-- 可以指定压缩级别
ALTER TABLE sales_fact_columnar SET (compression=middle);  -- low/middle/high

-- 查询1: 只读取部分列 (列存优势)
EXPLAIN (ANALYZE)
SELECT product_id, SUM(total_amount) 
FROM sales_fact_columnar 
WHERE sale_date >= '2024-01-01'
GROUP BY product_id;

-- 执行计划:
-- CStore Scan on sales_fact_columnar
--   只读取 product_id 和 total_amount 列
--   不读取其他列 (节省I/O)

-- 查询2: 聚合查询 (列存优势)
EXPLAIN (ANALYZE)
SELECT 
    region_id,
    SUM(net_amount) AS total_sales,
    AVG(net_amount) AS avg_sales,
    COUNT(*) AS sale_count
FROM sales_fact_columnar
WHERE sale_date >= '2024-01-01'
GROUP BY region_id;

-- 列存优势:
-- - 高压缩比 → 减少I/O
-- - 列式存储 → CPU缓存友好
-- - 向量化执行 → SIMD加速

-- 查询3: 单行查询 (列存劣势)
EXPLAIN (ANALYZE)
SELECT * FROM sales_fact_columnar WHERE sale_id = 12345;

-- 行存表更优:
CREATE TABLE sales_fact_row (
    ...
) WITH (ORIENTATION=ROW);

EXPLAIN (ANALYZE)
SELECT * FROM sales_fact_row WHERE sale_id = 12345;
```

### 38.2 星型模型优化

**维度表 vs 事实表**:

```sql
-- 1. 维度表 (小表,行存)
CREATE TABLE dim_product (
    product_id INT PRIMARY KEY,
    product_name VARCHAR(200),
    category VARCHAR(100),
    subcategory VARCHAR(100),
    brand VARCHAR(100)
) WITH (ORIENTATION=ROW);

CREATE TABLE dim_customer (
    customer_id INT PRIMARY KEY,
    customer_name VARCHAR(200),
    city VARCHAR(100),
    province VARCHAR(100),
    country VARCHAR(100),
    segment VARCHAR(100)
) WITH (ORIENTATION=ROW);

CREATE TABLE dim_time (
    time_id INT PRIMARY KEY,
    date DATE,
    year INT,
    quarter INT,
    month INT,
    day INT,
    week INT,
    day_of_week VARCHAR(20)
) WITH (ORIENTATION=ROW);

-- 2. 事实表 (大表,列存)
CREATE TABLE fact_sales (
    sales_id BIGINT,
    product_id INT,
    customer_id INT,
    time_id INT,
    store_id INT,
    quantity INT,
    unit_price NUMERIC(10,2),
    total_amount NUMERIC(10,2),
    discount NUMERIC(10,2),
    net_amount NUMERIC(10,2)
) WITH (ORIENTATION=COLUMN);

-- 创建外键 (可选,数据仓库通常不用外键)
-- ALTER TABLE fact_sales ADD CONSTRAINT fk_product 
--     FOREIGN KEY (product_id) REFERENCES dim_product(product_id);

-- 3. 星型模型查询
-- 典型分析查询
SELECT 
    p.category,
    t.year,
    t.quarter,
    SUM(f.net_amount) AS total_sales,
    COUNT(*) AS sales_count
FROM fact_sales f
JOIN dim_product p ON f.product_id = p.product_id
JOIN dim_time t ON f.time_id = t.time_id
WHERE t.year = 2024
GROUP BY p.category, t.year, t.quarter
ORDER BY total_sales DESC;

-- 优化1: 维度表使用索引
CREATE INDEX idx_dim_product_category ON dim_product(category);
CREATE INDEX idx_dim_time_year ON dim_time(year);

-- 优化2: 事实表使用分区
CREATE TABLE fact_sales_partitioned (
    ...
) WITH (ORIENTATION=COLUMN)
PARTITION BY RANGE (time_id);

-- 优化3: 使用物化视图
CREATE MATERIALIZED VIEW mv_sales_category_quarter AS
SELECT 
    p.category,
    t.year,
    t.quarter,
    SUM(f.net_amount) AS total_sales,
    COUNT(*) AS sales_count
FROM fact_sales f
JOIN dim_product p ON f.product_id = p.product_id
JOIN dim_time t ON f.time_id = t.time_id
GROUP BY p.category, t.year, t.quarter;

CREATE INDEX idx_mv_category_year ON mv_sales_category_quarter(category, year);

-- 定期刷新
REFRESH MATERIALIZED VIEW CONCURRENTLY mv_sales_category_quarter;

-- 优化4: 使用列压缩
ALTER TABLE fact_sales SET (compression=high);

-- 优化5: 调整内存
SET work_mem = '1GB';  -- 大排序/大哈希
SET hash_mem = '512MB';  -- 大哈希表
```

### 38.3 聚合表优化

**预聚合策略**:

```sql
-- 1. 日级聚合表
CREATE TABLE agg_sales_daily (
    date DATE,
    product_id INT,
    customer_id INT,
    store_id INT,
    sales_count BIGINT,
    sales_amount NUMERIC(20,2),
    avg_quantity NUMERIC(10,2),
    avg_unit_price NUMERIC(10,2)
) WITH (ORIENTATION=COLUMN);

-- 创建索引
CREATE INDEX idx_agg_daily_date ON agg_sales_daily(date);
CREATE INDEX idx_agg_daily_product ON agg_sales_daily(product_id);

-- 创建物化视图 (自动刷新)
CREATE MATERIALIZED VIEW mv_agg_sales_daily AS
SELECT 
    sale_date,
    product_id,
    customer_id,
    store_id,
    COUNT(*) AS sales_count,
    SUM(net_amount) AS sales_amount,
    AVG(quantity) AS avg_quantity,
    AVG(unit_price) AS avg_unit_price
FROM sales_fact_columnar
GROUP BY sale_date, product_id, customer_id, store_id;

-- 2. 周级聚合表
CREATE MATERIALIZED VIEW mv_agg_sales_weekly AS
SELECT 
    date_trunc('week', sale_date) AS week_start,
    product_id,
    COUNT(*) AS sales_count,
    SUM(net_amount) AS sales_amount
FROM sales_fact_columnar
GROUP BY date_trunc('week', sale_date), product_id;

-- 3. 月级聚合表
CREATE MATERIALIZED VIEW mv_agg_sales_monthly AS
SELECT 
    date_trunc('month', sale_date) AS month_start,
    product_id,
    COUNT(*) AS sales_count,
    SUM(net_amount) AS sales_amount
FROM sales_fact_columnar
GROUP BY date_trunc('month', sale_date), product_id;

-- 4. 季度级聚合表
CREATE MATERIALIZED VIEW mv_agg_sales_quarterly AS
SELECT 
    date_trunc('quarter', sale_date) AS quarter_start,
    product_id,
    COUNT(*) AS sales_count,
    SUM(net_amount) AS sales_amount
FROM sales_fact_columnar
GROUP BY date_trunc('quarter', sale_date), product_id;

-- 5. 年级聚合表
CREATE MATERIALIZED VIEW mv_agg_sales_yearly AS
SELECT 
    date_trunc('year', sale_date) AS year_start,
    product_id,
    COUNT(*) AS sales_count,
    SUM(net_amount) AS sales_amount
FROM sales_fact_columnar
GROUP BY date_trunc('year', sale_date), product_id;

-- 查询路由: 根据查询粒度选择合适的聚合表
-- 日级查询
SELECT * FROM mv_agg_sales_daily 
WHERE date = '2024-01-15' AND product_id = 123;

-- 周级查询
SELECT * FROM mv_agg_sales_weekly 
WHERE week_start = '2024-01-15' AND product_id = 123;

-- 月级查询
SELECT * FROM mv_agg_sales_monthly 
WHERE month_start = '2024-01-01' AND product_id = 123;
```

---

## 39. 高并发优化

### 39.1 连接池优化

**连接池原理**:

```sql
-- 1. 连接池配置
-- postgresql.conf:
max_connections = 200  -- 最大连接数
superuser_reserved_connections = 3  -- 超级用户保留连接

-- 2. 使用连接池中间件
-- PgBouncer配置:
-- 
-- [databases]
-- mydb = host=localhost port=6666 dbname=mydb
--
-- [pgbouncer]
-- pool_mode = transaction
-- max_client_conn = 1000
-- default_pool_size = 50
-- reserve_pool_size = 10
-- reserve_pool_timeout = 3
-- server_lifetime = 3600
-- server_idle_timeout = 600

-- 3. 连接池模式
-- 
-- Session pooling (会话池):
--   客户端连接 → 服务器连接 (一对一)
--   适合: 长连接应用
--
-- Transaction pooling (事务池):
--   客户端连接 → 服务器连接 (多对多,事务结束后释放)
--   适合: 短事务应用
--
-- Statement pooling (语句池):
--   客户端连接 → 服务器连接 (多对多,语句结束后释放)
--   适合: 自动提交应用
```

### 39.2 锁优化

**锁等待分析**:

```sql
-- 1. 查看当前锁
SELECT 
    pid,
    usename,
    pg_blocking_pids(pid) AS blocked_by,
    query as blocked_query,
    state
FROM pg_stat_activity
WHERE cardinality(pg_blocking_pids(pid)) > 0;

-- 2. 查看锁等待
SELECT 
    t.pid,
    t.usename,
    t.query,
    t.state,
    l.locktype,
    l.relation::regclass,
    l.mode,
    l.granted
FROM pg_stat_activity t
JOIN pg_locks l ON t.pid = l.pid
WHERE t.state = 'active'
  AND l.granted = false
ORDER BY t.query_start;

-- 3. 查看锁持有者
SELECT 
    t.pid,
    t.usename,
    t.query,
    t.state,
    l.locktype,
    l.relation::regclass,
    l.mode,
    l.granted
FROM pg_stat_activity t
JOIN pg_locks l ON t.pid = l.pid
WHERE t.state = 'active'
  AND l.granted = true
ORDER BY t.query_start;

-- 4. 终止阻塞进程
SELECT pg_terminate_backend(pid);  -- 终止指定进程
SELECT pg_cancel_backend(pid);     -- 取消指定进程的当前查询

-- 5. 锁超时设置
SET lock_timeout = '5s';  -- 锁等待超时5秒

-- 测试
BEGIN;
SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;

-- 另一个会话:
SET lock_timeout = '5s';
SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
-- ERROR: canceling statement due to lock timeout
```

**锁优化策略**:

```sql
-- 1. 减少锁粒度
-- 错误: 锁整表
SELECT * FROM orders FOR UPDATE;

-- 正确: 只锁需要的行
SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;

-- 2. 缩短锁持有时间
-- 错误: 长事务持有锁
BEGIN;
SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
-- 执行一些耗时操作 (应用层处理)
UPDATE orders SET amount = amount + 100 WHERE order_id = 123;
COMMIT;

-- 正确: 尽快释放锁
BEGIN;
SELECT * FROM orders WHERE order_id = 123 FOR UPDATE;
UPDATE orders SET amount = amount + 100 WHERE order_id = 123;
COMMIT;
-- 应用层处理在事务外

-- 3. 避免锁升级
-- 行锁 → 页锁 → 表锁
-- 尽量避免锁升级到表锁

-- 4. 使用乐观锁
-- 版本号机制
CREATE TABLE orders_v2 (
    order_id INT PRIMARY KEY,
    amount NUMERIC,
    version INT DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 更新时检查版本号
UPDATE orders_v2 
SET amount = amount + 100,
    version = version + 1,
    updated_at = CURRENT_TIMESTAMP
WHERE order_id = 123
  AND version = 5;  -- 检查版本号

-- 如果version != 5,说明数据已被其他事务修改
-- 应用层可以决定是否重试

-- 5. 使用 Advisory Lock
-- 应用级锁
SELECT pg_advisory_lock(12345);  -- 获取锁
-- 执行业务逻辑
SELECT pg_advisory_unlock(12345);  -- 释放锁

-- 或使用事务级Advisory Lock
SELECT pg_advisory_xact_lock(12345);  -- 事务结束时自动释放
```

### 39.3 MVCC优化

**MVCC (Multi-Version Concurrency Control)**:

```sql
-- 1. MVCC原理
-- 读不阻塞写,写不阻塞读
-- 
-- 事务1 (读):
BEGIN;
SELECT * FROM orders WHERE order_id = 123;
-- 看到的是事务开始时的快照

-- 事务2 (写):
BEGIN;
UPDATE orders SET amount = amount + 100 WHERE order_id = 123;
COMMIT;
-- 事务2提交后,事务1仍然看到旧数据

-- 事务1:
SELECT * FROM orders WHERE order_id = 123;
-- 仍然看到旧数据 (快照隔离)

-- 提交事务1
COMMIT;

-- 新开事务3:
BEGIN;
SELECT * FROM orders WHERE order_id = 123;
-- 看到新数据 (READ COMMITTED: 每条语句获取新快照)
COMMIT;

-- 2. 版本链: xmin/xmax
-- 每个元组版本头部记录:
-- - xmin: 插入该版本的事务ID
-- - xmax: 删除/更新该版本的事务ID
--   (UPDATE = 旧版本标记xmax + 插入新版本)
--
-- 可见性判断入口: heapam_visibility.cpp HeapTupleSatisfiesMVCC函数

-- 3. 快照从哪里来
-- snapmgr.cpp GetTransactionSnapshot函数:
-- - READ COMMITTED: 每条语句重新获取快照
-- - REPEATABLE READ: 事务内首条语句的快照一直用到事务结束

-- 4. MVCC对性能的影响
-- (1) 长事务持有旧快照 → 死元组无法回收 → 表膨胀 → 扫描页数上升
-- (2) 热点行频繁更新 → 版本链变长 → 回表后可见性判断的CPU上升
-- (3) 高并发短事务 + 及时提交 = 对MVCC最友好的形态

-- 查看当前最老的快照(决定死元组回收水位):
SELECT backend_xmin, state, xact_start, query
FROM pg_stat_activity
WHERE backend_xmin IS NOT NULL
ORDER BY backend_xmin;
-- 观察: backend_xmin最小的会话就是回收水位
-- 一个跑数小时的长事务会拖住全库的死元组清理
```

### 39.4 隔离级别与优化器交互

**隔离级别**:

```sql
-- openGauss默认: READ COMMITTED
SHOW default_transaction_isolation;

-- 会话级切换
SET default_transaction_isolation = 'repeatable read';
-- 或
SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ;
```

**GUC注册(源码分析)**:

```cpp
// src/common/backend/utils/misc/guc.cpp
// default_transaction_isolation注册项 (真实源码节选)

{{"default_transaction_isolation",
    PGC_USERSET,
    NODE_ALL,
    CLIENT_CONN_STATEMENT,
    gettext_noop("Sets the transaction isolation level of each new transaction."),
    NULL},
    &u_sess->attr.attr_common.DefaultXactIsoLevel,
    XACT_READ_COMMITTED,               // 默认: READ COMMITTED
    isolation_level_options,
    check_default_transaction_isolation,
    NULL,
    NULL},

// 同文件 isolation_level_options枚举:
// {"serializable", XACT_SERIALIZABLE}
// {"repeatable read", XACT_REPEATABLE_READ}
// {"read committed", XACT_READ_COMMITTED}
// {"read uncommitted", XACT_READ_UNCOMMITTED}

// check_default_transaction_isolation钩子:
// 仅在DMS+DSS存储模式下拒绝非READ COMMITTED,其余场景放行
```

**快照获取逻辑**:

```cpp
// src/common/backend/utils/time/snapmgr.cpp
// GetTransactionSnapshot函数 (真实源码节选)

Snapshot GetTransactionSnapshot(bool force_local_snapshot)
{
    // 事务内第一次取快照?
    if (!u_sess->utils_cxt.FirstSnapshotSet) {
        if (IsolationUsesXactSnapshot()) {
            // REPEATABLE READ / SERIALIZABLE:
            // 取事务级快照并拷贝保存,整个事务复用
            u_sess->utils_cxt.CurrentSnapshot = CopySnapshot(...);
            u_sess->utils_cxt.FirstXactSnapshot = u_sess->utils_cxt.CurrentSnapshot;
        } else {
            // READ COMMITTED: 每条语句现取新快照
            u_sess->utils_cxt.CurrentSnapshot =
                GetSnapshotData(u_sess->utils_cxt.CurrentSnapshotData, force_local_snapshot);
        }
        u_sess->utils_cxt.FirstSnapshotSet = true;
    }
    // ...
}
```

**与优化器的交互**:

```sql
-- 1. 计划与数据可见性解耦:
--    代价估算读pg_statistic统计信息,不走事务数据快照
--    (⚓待锚: pg_statistic读取所用快照策略的具体代码位置)

-- 2. 同一事务内重复执行同一语句:
--    RC: 每语句新快照 → 能看到别人新提交的数据
--    RR: 同一快照 → 结果稳定,但看不到别人提交

-- 3. 隔离级别不改变计划形状:
--    优化器出计划时不区分隔离级别;
--    真正影响并发吞吐的是锁持有时间与死元组回收水位
```

**高并发调优参数速查表**:

| 参数 | 默认值 | 作用 | 源码锚点 |
|------|--------|------|----------|
| max_connections | 200 | 最大并发连接数(重启生效) | guc_network.cpp(GUC注册) |
| default_transaction_isolation | read committed | 新事务默认隔离级别 | guc.cpp(GUC注册) |
| session_timeout | 600s | 空闲会话超时自动断开,0为关闭 | guc.cpp(GUC注册) |
| deadlock_timeout | 1s | 死锁检测周期 | guc.cpp(GUC注册) |
| work_mem | 64MB | 单个排序/哈希工作区内存上限 | guc_memory.cpp(GUC注册) |

> 注: 原文39.2节示例中的 `SET lock_timeout = '5s';` 在 openGauss 6.0.0 的GUC注册表中未见同名参数(⚓待锚),现场使用前先以 `SHOW lock_timeout;` 验证所支持版本。

---

## 总结

- 列存表(ORIENTATION=COLUMN)适合OLAP: 只读需要的列、高压缩比、向量化执行;单行点查与频繁UPDATE/DELETE仍应使用行存。
- 星型模型优化组合拳: 维度表行存加索引,事实表列存加分区,汇总层用物化视图按日/周/月/季/年分层预聚合,查询按粒度路由到对应聚合表。
- 连接层用中间件承压: 事务级复用把大量前端连接收敛到少量服务端连接,数据层只看到稳定并发。
- 锁优化四板斧: 缩小锁粒度、缩短持有时间、乐观版本号、Advisory Lock;排查用pg_locks配合pg_stat_activity,处置用pg_cancel_backend/pg_terminate_backend。
- MVCC读不阻塞写,代价是死元组: 长事务拖住backend_xmin回收水位,表膨胀最终反噬扫描与统计信息质量。
- 隔离级别默认READ COMMITTED(guc.cpp注册,枚举含四档);RR/SERIALIZABLE走事务级快照,入口在snapmgr.cpp的GetTransactionSnapshot。
- 高并发参数先盯max_connections、session_timeout、work_mem三个,全部有GUC注册锚点,改前先对照速查表确认默认值与生效方式。

## 版本注记

- 2026-11-05 断尾补齐,原文 500 行截断(残卷实际499行)。原文截断于39.3节MVCC示例SQL块中("-- 仍然看到旧数据 (快照隔离)"处);本次补完MVCC示例(版本链/快照来源/性能影响),并顺势补写39.4隔离级别与优化器交互、高并发参数速查表。
- 原文头部"继续前序文档..."占位行一并清理,改为与系列文档一致的版本/聚焦说明。
- 源码引用一律为 文件名+函数名,基于 openGauss-server-v6.0.0 源码树逐项核对;未核实处标注⚓待锚。

## 来源

- 源码基线: openGauss-server-v6.0.0 — src/common/backend/utils/misc/guc.cpp(default_transaction_isolation注册、isolation_level_options、check_default_transaction_isolation、session_timeout、deadlock_timeout)、src/common/backend/utils/misc/guc/guc_network.cpp(max_connections)、src/common/backend/utils/misc/guc/guc_memory.cpp(work_mem)、src/common/backend/utils/time/snapmgr.cpp(GetTransactionSnapshot)、src/gausskernel/storage/access/heap/heapam_visibility.cpp(HeapTupleSatisfiesMVCC)
- 原文 2026-04 学习档案(优化器系列最终部分,500行截断版)
