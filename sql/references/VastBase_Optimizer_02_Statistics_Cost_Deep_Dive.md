     1|# VastBase/openGauss 统计信息与Cost计算超详细深度分析
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 分析文件: costsize.cpp (6577行), clausesel.cpp (1289行)  
     5|> 学习目标: 给定任意执行计划，能精确计算每个节点的cost和rows
     6|
     7|---
     8|
     9|## 目录
    10|
    11|### 第二部分: 统计信息和Cost计算
    12|- [7. 统计信息系统架构](#7-统计信息系统架构)
    13|- [8. 表级统计信息](#8-表级统计信息)  
    14|- [9. 列级统计信息](#9-列级统计信息)
    15|- [10. 索引统计信息](#10-索引统计信息)
    16|- [11. 选择率计算](#11-选择率计算)
    17|- [12. Cost计算模型](#12-cost计算模型)
    18|- [13. 执行计划Cost分析实例](#13-执行计划cost分析实例)
    19|
    20|---
    21|
    22|## 7. 统计信息系统架构
    23|
    24|### 7.1 统计信息收集机制
    25|
    26|**ANALYZE命令流程**:
    27|
    28|```sql
    29|-- 手动收集统计信息
    30|ANALYZE table_name;
    31|
    32|-- 并行收集统计信息
    33|ANALYZE VERBOSE table_name;
    34|
    35|-- 收集特定列的统计信息
    36|ANALYZE table_name (column1, column2);
    37|
    38|-- 自动统计信息收集配置
    39|-- postgresql.conf
    40|autovacuum = on
    41|autovacuum_analyze_scale_factor = 0.1    -- 10%数据变化时触发
    42|autovacuum_analyze_threshold = 50        -- 至少50行变化时触发
    43|```
    44|
    45|**源码级统计信息收集**:
    46|
    47|```cpp
    48|// src/gausskernel/optimizer/commands/analyze.cpp
    49|// 主函数: analyze_rel
    50|
    51|void analyze_rel(Oid relid, VacuumStmt* stmt, AcquireSampleRowsFunc acquirefunc)
    52|{
    53|    Relation rel;
    54|    int elevel = log_min_messages;
    55|    
    56|    // 步骤1: 打开表
    57|    rel = relation_open(relid, AccessShareLock);
    58|    
    59|    // 步骤2: 确定需要分析的列
    60|    List* va_cols = NIL;  // 用户指定的列
    61|    if (stmt->va_cols)
    62|        va_cols = stmt->va_cols;
    63|    
    64|    // 步骤3: 获取表的行数估计
    65|    BlockNumber totalpages = RelationGetNumberOfBlocks(rel);
    66|    int64 totalrows = estimate_rel_size(rel, totalpages, ...);
    67|    
    68|    // 步骤4: 采样统计
    69|    // 默认采样目标: default_statistics_target = 100
    70|    int target = default_statistics_target;
    71|    int sample_rows = Min(target * 30, totalrows);  // 采样行数
    72|    
    73|    // 步骤5: 执行采样
    74|    HeapTuple* rows = (HeapTuple*) palloc(sample_rows * sizeof(HeapTuple));
    75|    int num_samples = (*acquirefunc)(
    76|        rel, 
    77|        elevel,
    78|        rows,         // 输出: 采样的行
    79|        sample_rows,  // 目标采样数
    80|        &totalrows    // 输出: 总行数估计
    81|    );
    82|    
    83|    // 步骤6: 计算统计信息
    84|    for (每个列)
    85|    {
    86|        // 6.1 计算基本统计: NULL值比例、平均宽度
    87|        compute_scalar_stats(rel, attnum, rows, num_samples, ...);
    88|        
    89|        // 6.2 如果数据可排序，计算MCV (Most Common Values)
    90|        if (列有较少的distinct值)
    91|            compute_mcv(rel, attnum, rows, num_samples, ...);
    92|        
    93|        // 6.3 如果数据是标量，计算直方图
    94|        if (列是数值类型或日期类型)
    95|            compute_histogram(rel, attnum, rows, num_samples, ...);
    96|        
    97|        // 6.4 计算相关性（如果有多列统计信息）
    98|        if (有多列统计)
    99|            compute_correlation(rel, attnum1, attnum2, ...);
   100|    }
   101|    
   102|    // 步骤7: 更新系统表 pg_statistic
   103|    update_pg_statistic(rel, ...);
   104|    
   105|    // 步骤8: 清理
   106|    relation_close(rel, AccessShareLock);
   107|}
   108|```
   109|
   110|### 7.2 统计信息存储结构
   111|
   112|**系统表结构**:
   113|
   114|```sql
   115|-- pg_statistic: 存储列级统计信息
   116|\d+ pg_statistic
   117|
   118|-- 主要字段:
   119|starelid      oid        -- 表的OID
   120|stattarget    int4       -- 统计目标值 (default_statistics_target)
   121|staopno       oid[]      -- 操作符OID数组
   122|staColl       oid[]      -- collation OID数组
   123|stanumbers    float4[]   -- 数值统计 (NULL比例、MCV频率、直方图边界)
   124|stavalues     anyarray   -- 值统计 (MCV值、直方图值)
   125|-- 查看某个表的统计信息
   126|SELECT 
   127|    s.starelid::regclass AS table_name,
   128|    a.attname AS column_name,
   129|    s.stanullfrac AS null_frac,
   130|    s.stawidth AS avg_width,
   131|    array_length(s.stavalues1, 1) AS n_distinct,
   132|    s.stadistinct AS distinct_estimate
   133|FROM pg_statistic s
   134|JOIN pg_attribute a ON s.starelid = a.attrelid 
   135|    AND s.staattnum = a.attnum
   136|WHERE s.starelid = 'your_table'::regclass;
   137|
   138|-- 查看MCV (Most Common Values)
   139|SELECT 
   140|    a.attname AS column_name,
   141|    s.stavalues1 AS mcv_values,
   142|    s.stanumbers1 AS mcv_frequencies
   143|FROM pg_statistic s
   144|JOIN pg_attribute a ON s.starelid = a.attrelid 
   145|    AND s.staattnum = a.attnum
   146|WHERE s.starelid = 'your_table'::regclass
   147|  AND s.stakind1 = 1;  -- 1 = MCV
   148|
   149|-- 查看直方图
   150|SELECT 
   151|    a.attname AS column_name,
   152|    s.stavalues1 AS histogram_bounds
   153|FROM pg_statistic s
   154|JOIN pg_attribute a ON s.starelid = a.attrelid 
   155|    AND s.staattnum = a.attnum
   156|WHERE s.starelid = 'your_table'::regclass
   157|  AND s.stakind1 = 2;  -- 2 = 直方图
   158|```
   159|
   160|**统计信息类型编码**:
   161|
   162|```cpp
   163|// src/include/catalog/pg_statistic.h
   164|#define STATISTIC_KIND_MCV        1  // Most Common Values
   165|#define STATISTIC_KIND_HISTOGRAM  2  // 直方图
   166|#define STATISTIC_KIND_CORRELATION 3  // 相关性
   167|#define STATISTIC_KIND_MCV        4  // MCV元素
   168|#define STATISTIC_KIND_HISTOGRAM  5  // 直方图边界
   169|#define STATISTIC_KIND_CORRELATION 6  // 相关性系数
   170|#define STATISTIC_KIND_EC_CLASS   7  // 等价类
   171|
   172|// 示例: 读取统计信息
   173|typedef struct StatisticExtInfo
   174|{
   175|    Oid         statOid;        // 统计信息的OID
   176|    char        kind;           // 统计类型 (见上面定义)
   177|    Oid         dataType;       // 数据类型
   178|    int32       nvalues;        // 值的数量
   179|    float4*     numbers;        // 数值统计
   180|    Datum*      values;         // 值统计
   181|} StatisticExtInfo;
   182|```
   183|
   184|---
   185|
   186|## 8. 表级统计信息
   187|
   188|### 8.1 pg_class表统计
   189|
   190|```sql
   191|-- 查看表级统计信息
   192|SELECT 
   193|    relname AS table_name,
   194|    reltuples AS row_estimate,
   195|    relpages AS page_estimate,
   196|    relallvisible AS all_visible_pages,
   197|    relkind AS relation_type
   198|FROM pg_class
   199|WHERE relname = 'your_table';
   200|
   201|-- 字段说明:
   202|-- reltuples:    表的行数估计（浮点数）
   203|-- relpages:     表占用的磁盘页数（8KB每页）
   204|-- relallvisible: 对可见性映射(VMM)可见的页数（用于优化）
   205|-- relkind:      关系类型 (r=普通表, i=索引, S=序列, v=视图, m=物化视图)
   206|
   207|-- 计算表大小
   208|SELECT 
   209|    pg_size_pretty(pg_total_relation_size('your_table')) AS total_size,
   210|    pg_size_pretty(pg_relation_size('your_table')) AS table_size,
   211|    pg_size_pretty(pg_total_relation_size('your_table') - pg_relation_size('your_table')) AS indexes_size;
   212|
   213|-- 查看表的详细统计
   214|SELECT 
   215|    schemaname,
   216|    tablename,
   217|    n_live_tup,        -- 活跃行数
   218|    n_dead_tup,        -- 死行数
   219|    last_vacuum,       -- 最后一次VACUUM时间
   220|    last_autovacuum,   -- 最后一次自动VACUUM时间
   221|    last_analyze,      -- 最后一次ANALYZE时间
   222|    last_autoanalyze,  -- 最后一次自动ANALYZE时间
   223|    vacuum_count,      -- VACUUM次数
   224|    autovacuum_count,  -- 自动VACUUM次数
   225|    analyze_count,     -- ANALYZE次数
   226|    autoanalyze_count  -- 自动ANALYZE次数
   227|FROM pg_stat_user_tables
   228|WHERE tablename = 'your_table';
   229|```
   230|
   231|### 8.2 表级统计信息的来源
   232|
   233|```sql
   234|-- 统计信息更新机制
   235|-- 1. CREATE TABLE: reltuples = 0, relpages = 0
   236|CREATE TABLE test_stats (id INT, value NUMERIC);
   237|
   238|-- 检查初始统计
   239|SELECT reltuples, relpages FROM pg_class WHERE relname = 'test_stats';
   240|-- 结果: reltuples = 0, relpages = 0
   241|
   242|-- 2. INSERT数据后立即查询: 统计信息仍为0
   243|INSERT INTO test_stats SELECT generate_series(1, 10000), random() * 1000;
   244|SELECT reltuples, relpages FROM pg_class WHERE relname = 'test_stats';
   245|-- 结果: reltuples 仍为 0（需要ANALYZE）
   246|
   247|-- 3. 执行ANALYZE: 更新统计信息
   248|ANALYZE test_stats;
   249|SELECT reltuples, relpages FROM pg_class WHERE relname = 'test_stats';
   250|-- 结果: reltuples ≈ 10000, relpages ≈ 实际页数
   251|
   252|-- 4. 大批量插入: 可能自动更新统计（autovacuum）
   253|-- postgresql.conf 配置:
   254|autovacuum = on
   255|autovacuum_analyze_scale_factor = 0.1  -- 10%变化
   256|autovacuum_analyze_threshold = 50      -- 至少50行
   257|```
   258|
   259|### 8.3 行数估计算法
   260|
   261|**源码级分析**:
   262|
   263|```cpp
   264|// src/gausskernel/optimizer/util/plancat.c
   265|// 函数: estimate_rel_size
   266|
   267|void estimate_rel_size(
   268|    Relation rel,
   269|    BlockNumber* pages,
   270|    double* tuples,
   271|    BlockNumber* allvispages)
   272|{
   273|    BlockNumber curpages;
   274|    double curtuples;
   275|    BlockNumber curallvispages;
   276|    
   277|    // 方法1: 如果有精确的统计信息，使用统计
   278|    if (rel->rd_rel->relpages > 0)
   279|    {
   280|        curpages = rel->rd_rel->relpages;
   281|        curtuples = rel->rd_rel->reltuples;
   282|        curallvispages = rel->rd_rel->relallvisible;
   283|    }
   284|    else
   285|    {
   286|        // 方法2: 没有统计信息，进行物理扫描
   287|        curpages = RelationGetNumberOfBlocks(rel);
   288|        curtuples = 0;  // 需要采样
   289|        curallvispages = curpages;  // 假设都可见
   290|        
   291|        // 采样估计行数
   292|        BlockNumber samplerows = 0;
   293|        BlockNumber samplepages = Min(curpages, 1000);  // 最多采样1000页
   294|        
   295|        for (int i = 0; i < samplepages; i++)
   296|        {
   297|            Buffer buf = ReadBuffer(rel, i);
   298|            Page page = BufferGetPage(buf);
   299|            samplerows += PageGetMaxOffsetNumber(page);
   300|            ReleaseBuffer(buf);
   301|        }
   302|        
   303|        // 外推到全表
   304|        curtuples = (double)samplerows * curpages / samplepages;
   305|    }
   306|    
   307|    // 返回结果
   308|    if (pages)
   309|        *pages = curpages;
   310|    if (tuples)
   311|        *tuples = curtuples;
   312|    if (allvispages)
   313|        *allvispages = curallvispages;
   314|}
   315|```
   316|
   317|**实际测试**:
   318|
   319|```sql
   320|-- 创建测试表
   321|CREATE TABLE size_test AS
   322|SELECT generate_series(1, 100000) AS id,
   323|       repeat('x', 100) AS data;
   324|
   325|-- 查看实际大小
   326|SELECT 
   327|    pg_relation_size('size_test') / 1024 / 1024 AS size_mb,
   328|    pg_relation_size('size_test') / 8192 AS pages_exact;
   329|
   330|-- 查看统计信息
   331|ANALYZE size_test;
   332|SELECT reltuples, relpages FROM pg_class WHERE relname = 'size_test';
   333|
   334|-- 计算行宽
   335|SELECT 
   336|    sum(pg_column_size(t)) / count(*) AS avg_row_width
   337|FROM size_test t;
   338|
   339|-- 预估页数 = 总大小 / 页大小
   340|SELECT reltuples * avg_row_width / 8192 AS estimated_pages;
   341|```
   342|
   343|---
   344|
   345|## 9. 列级统计信息
   346|
   347|### 9.1 基本列统计
   348|
   349|```sql
   350|-- 查看列级统计详细信息
   351|SELECT 
   352|    a.attname AS column_name,
   353|    a.atttypid::regtype AS data_type,
   354|    s.stanullfrac AS null_fraction,        -- NULL值比例
   355|    s.stawidth AS avg_width,               -- 平均宽度 (字节)
   356|    s.stadistinct AS n_distinct,           -- distinct值数量
   357|    CASE 
   358|        WHEN s.stadistinct > 0 THEN s.stadistinct
   359|        WHEN s.stadistinct < 0 THEN (-s.stadistinct * reltuples)
   360|        ELSE NULL
   361|    END AS distinct_estimate,
   362|    s.stakind1, s.stakind2, s.stakind3, s.stakind4  -- 统计类型
   363|FROM pg_statistic s
   364|JOIN pg_attribute a ON s.starelid = a.attrelid 
   365|    AND s.staattnum = a.attnum
   366|JOIN pg_class c ON c.oid = s.starelid
   367|WHERE c.relname = 'your_table'
   368|ORDER BY a.attnum;
   369|
   370|-- 字段详解:
   371|-- stanullfrac:  NULL值的比例 [0.0, 1.0]
   372|--                0.0 = 没有NULL, 0.1 = 10%是NULL
   373|--
   374|-- stawidth:     平均列宽 (字节)
   375|--                包括TOAST表的大字段
   376|--
   377|-- stadistinct:  distinct值的数量
   378|--                正数: 精确值
   379|--                负数: 比例 (例如 -0.5 表示 50% 的行是distinct)
   380|--                0:    未知
   381|--
   382|-- stakind1-N:   统计信息类型
   383|--                1 = MCV (Most Common Values)
   384|--                2 = Histogram (直方图)
   385|--                3 = Correlation (相关性)
   386|```
   387|
   388|### 9.2 MCV (Most Common Values) 统计
   389|
   390|**MCV的作用**:
   391|
   392|MCV用于存储出现频率最高的值及其频率。当列中某些值出现频率远高于其他值时，MCV统计非常有用。
   393|
   394|**MCV存储结构**:
   395|
   396|```cpp
   397|// src/include/catalog/pg_statistic.h
   398|// pg_statistic中的MCV数据:
   399|// stakind1 = STATISTIC_KIND_MCV
   400|// stavalues1 = {值1, 值2, 值3, ...}      // MCV值
   401|// stanumbers1 = {频率1, 频率2, 频率3, ...} // 对应的频率
   402|
   403|// 示例: 性别列的MCV
   404|// stavalues1  = ['M', 'F']
   405|// stanumbers1 = [0.51, 0.49]
   406|// 解释: 51%是男性，49%是女性
   407|
   408|// 示例: 国家代码列的MCV (前100个最常见的)
   409|// stavalues1  = ['CN', 'US', 'JP', 'UK', ...]  // 最多100个值
   410|// stanumbers1 = [0.35, 0.25, 0.15, 0.10, ...] // 对应频率
   411|```
   412|
   413|**查看MCV**:
   414|
   415|```sql
   416|-- 查看某列的MCV
   417|SELECT 
   418|    a.attname AS column_name,
   419|    s.stavalues1 AS mcv_values,
   420|    s.stanumbers1 AS mcv_frequencies
   421|FROM pg_statistic s
   422|JOIN pg_attribute a ON s.starelid = a.attrelid 
   423|    AND s.staattnum = a.attnum
   424|WHERE s.starelid = 'your_table'::regclass
   425|  AND a.attname = 'your_column'
   426|  AND s.stakind1 = 1;  -- 1 = MCV
   427|
   428|-- 解析MCV数组
   429|SELECT 
   430|    (mcv_value).value AS column_value,
   431|    (mcv_freq).num AS frequency
   432|FROM (
   433|    SELECT 
   434|        unnest(s.stavalues1) AS mcv_value,
   435|        unnest(s.stanumbers1) AS mcv_freq
   436|    FROM pg_statistic s
   437|    JOIN pg_attribute a ON s.starelid = a.attrelid 
   438|        AND s.staattnum = a.attnum
   439|    WHERE s.starelid = 'your_table'::regclass
   440|      AND a.attname = 'your_column'
   441|      AND s.stakind1 = 1
   442|) sub
   443|ORDER BY (mcv_freq).num DESC;
   444|
   445|-- MCV判断标准
   446|-- 如果某个值的出现频率 > 1 / (统计目标 * 统计采样率)
   447|-- 则该值会被记录到MCV中
   448|-- 例如: default_statistics_target = 100
   449|--       如果某值频率 > 1/100 = 1%，则记录到MCV
   450|```
   451|
   452|**MCV对选择率的影响**:
   453|
   454|```sql
   455|-- 创建有偏数据的测试表
   456|CREATE TABLE mcv_test (
   457|    id INT,
   458|    status VARCHAR(20)
   459|);
   460|
   461|-- 插入有偏数据: 90%是'active', 10%是'inactive'
   462|INSERT INTO mcv_test
   463|SELECT 
   464|    generate_series(1, 100000),
   465|    CASE WHEN random() < 0.9 THEN 'active' ELSE 'inactive' END
   466|FROM generate_series(1, 100);
   467|
   468|-- 收集统计
   469|ANALYZE mcv_test;
   470|
   471|-- 查看MCV
   472|SELECT 
   473|    unnest(stavalues1) AS value,
   474|    unnest(stanumbers1) AS frequency
   475|FROM pg_statistic s
   476|JOIN pg_attribute a ON s.starelid = a.attrelid 
   477|    AND s.staattnum = a.attnum
   478|WHERE s.starelid = 'mcv_test'::regclass
   479|  AND a.attname = 'status'
   480|  AND s.stakind1 = 1;
   481|
   482|-- 结果应该类似:
   483|-- value    | frequency
   484|-- ----------+-----------
   485|-- active   | 0.9012    (约90%)
   486|-- inactive | 0.0988    (约10%)
   487|
   488|-- 测试选择率估计
   489|EXPLAIN
   490|SELECT * FROM mcv_test WHERE status = 'active';
   491|
   492|-- 观察执行计划中的rows估计:
   493|-- 应该接近: 100000 * 0.9 = 90000
   494|
   495|EXPLAIN
   496|SELECT * FROM mcv_test WHERE status = 'inactive';
   497|
   498|-- rows估计: 100000 * 0.1 = 10000
   499|
   500|EXPLAIN
   501|

---
> ⚓ **截断注记（2026-11-05 审计）**：本篇在历史迁移时截断于 500 行，尾部
> 小节缺失（原全量件已不可寻，待重写补齐——挂账 ROADMAP）；行号式清单为
> 源码摘录，无独立 file:line 锚。
