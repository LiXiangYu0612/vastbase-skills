     1|# VastBase/openGauss 优化器参数与索引优化超详细深度分析
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 分析文件: guc.cpp, costsize.cpp, indxpath.cpp (20+文件)  
     5|> 学习目标: 掌握所有优化器参数的作用、调优方法、索引选择策略
     6|
     7|---
     8|
     9|## 目录
    10|
    11|### 第四部分: 优化器参数
    12|- [20. Cost配置参数](#20-cost配置参数)
    13|- [21. 规划器开关参数](#21-规划器开关参数)
    14|- [22. 其他优化器参数](#22-其他优化器参数)
    15|- [23. 参数调优实战](#23-参数调优实战)
    16|
    17|### 第五部分: 索引优化
    18|- [24. 索引类型对比](#24-索引类型对比)
    19|- [25. 索引选择算法](#25-索引选择算法)
    20|- [26. 索引优化实战](#26-索引优化实战)
    21|- [27. 索引维护成本](#27-索引维护成本)
    22|
    23|---
    24|
    25|## 20. Cost配置参数
    26|
    27|### 20.1 seq_page_cost (顺序页成本)
    28|
    29|**参数定义**:
    30|
    31|```sql
    32|-- 查看当前值
    33|SHOW seq_page_cost;  -- 默认: 1.0
    34|
    35|-- 参数含义:
    36|-- 顺序读取一个磁盘页的成本
    37|-- 作为其他成本参数的基准单位
    38|
    39|-- 调整语法
    40|SET seq_page_cost = 1.0;  -- 会话级别
    41|-- 或
    42|ALTER DATABASE dbname SET seq_page_cost = 1.0;  -- 数据库级别
    43|```
    44|
    45|**源码分析**:
    46|
    47|```cpp
    48|// src/backend/utils/misc/guc.c
    49|// seq_page_cost的定义
    50|
    51|{
    52|    {"seq_page_cost",
    53|        PGC_USERSET,
    54|        QUERY_TUNING_COST,
    55|        gettext_noop("Sets the planner's estimate of the cost of "
    56|                     "a sequentially fetched disk page."),
    57|        NULL
    58|    },
    59|    &u_sess->attr.attr_sql.seq_page_cost,
    60|    DEFAULT_SEQ_PAGE_COST,  // 默认值: 1.0
    61|    0.0,                     // 最小值
    62|    DBL_MAX,                 // 最大值
    63|    NULL, NULL, NULL
    64|}
    65|
    66|// 使用位置
    67|// src/gausskernel/optimizer/path/costsize.cpp
    68|// cost_seqscan函数
    69|
    70|void cost_seqscan(Path* path, PlannerInfo* root, 
    71|                 RelOptInfo* baserel, ParamPathInfo* param_info)
    72|{
    73|    // 获取表空间的seq_page_cost
    74|    double spc_seq_page_cost;
    75|    get_tablespace_page_costs(baserel->reltablespace, 
    76|                             NULL, &spc_seq_page_cost);
    77|    
    78|    // 计算磁盘I/O成本
    79|    run_cost += spc_seq_page_cost * baserel->pages;
    80|    
    81|    // baserel->pages: 表的页数
    82|    // spc_seq_page_cost: 通常是1.0
    83|    // 结果: 顺序扫描的磁盘成本
    84|}
    85|```
    86|
    87|**调整场景**:
    88|
    89|```sql
    90|-- 场景1: SSD存储
    91|-- SSD的顺序读取速度比HDD快
    92|-- 可以降低seq_page_cost
    93|
    94|-- 检查存储类型
    95|SHOW data_directory;  -- 查看数据目录
    96|
    97|-- 调整 (如果是SSD)
    98|SET seq_page_cost = 0.5;
    99|
   100|-- 测试影响
   101|EXPLAIN
   102|SELECT * FROM large_table;
   103|
   104|-- 观察cost变化
   105|
   106|-- 场景2: 网络存储 (NFS, SAN)
   107|-- 网络存储的延迟可能更高
   108|-- 可以提高seq_page_cost
   109|
   110|SET seq_page_cost = 2.0;
   111|
   112|EXPLAIN
   113|SELECT * FROM large_table;
   114|
   115|-- 场景3: 不同表空间
   116|-- 创建表空间 (不同存储)
   117|CREATE TABLESPACE ssd_space LOCATION '/mnt/ssd/opengauss';
   118|CREATE TABLESPACE hdd_space LOCATION '/mnt/hdd/opengauss';
   119|
   120|-- 为表空间设置不同的page cost (需要修改配置文件)
   121|-- postgresql.conf:
   122|-- ssd_space.seq_page_cost = 0.5
   123|-- hdd_space.seq_page_cost = 1.0
   124|
   125|-- 在不同表空间创建表
   126|CREATE TABLE table_on_ssd (id INT) TABLESPACE ssd_space;
   127|CREATE TABLE table_on_hdd (id INT) TABLESPACE hdd_space;
   128|
   129|-- 优化器会根据表空间选择不同的扫描方式
   130|EXPLAIN
   131|SELECT * FROM table_on_ssd;
   132|EXPLAIN
   133|SELECT * FROM table_on_hdd;
   134|```
   135|
   136|### 20.2 random_page_cost (随机页成本)
   137|
   138|**参数定义**:
   139|
   140|```sql
   141|-- 查看当前值
   142|SHOW random_page_cost;  -- 默认: 4.0
   143|
   144|-- 参数含义:
   145|-- 随机读取一个磁盘页的成本
   146|-- 通常是seq_page_cost的4倍 (反映HDD的寻道时间)
   147|
   148|-- 调整语法
   149|SET random_page_cost = 4.0;
   150|```
   151|
   152|**源码分析**:
   153|
   154|```cpp
   155|// src/backend/utils/misc/guc.c
   156|{
   157|    {"random_page_cost",
   158|        PGC_USERSET,
   159|        QUERY_TUNING_COST,
   160|        gettext_noop("Sets the planner's estimate of the cost of "
   161|                     "a non-sequentially fetched disk page."),
   162|        NULL
   163|    },
   164|    &u_sess->attr.attr_sql.random_page_cost,
   165|    DEFAULT_RANDOM_PAGE_COST,  // 默认值: 4.0
   166|    0.0,
   167|    DBL_MAX,
   168|    NULL, NULL, NULL
   169|}
   170|
   171|// 使用位置
   172|// cost_index函数
   173|
   174|void cost_index(Path* path, PlannerInfo* root, IndexPath* ipath,
   175|               bool is_indexjoin, List* indexQuals, 
   176|               List* indexConditionals)
   177|{
   178|    // 计算索引扫描的随机I/O成本
   179|    Cost index_scan_cost = 
   180|        random_page_cost * numIndexPages;  // 索引页
   181|        + random_page_cost * numHeapPages; // 堆表页
   182|    
   183|    // random_page_cost影响索引扫描的吸引力
   184|    // random_page_cost越高 → 索引扫描越不划算
   185|}
   186|```
   187|
   188|**调整场景**:
   189|
   190|```sql
   191|-- 场景1: SSD存储
   192|-- SSD的随机读取速度接近顺序读取
   193|-- 可以降低random_page_cost
   194|
   195|SET seq_page_cost = 1.0;
   196|SET random_page_cost = 1.1;  -- 接近seq_page_cost
   197|
   198|-- 测试
   199|EXPLAIN
   200|SELECT * FROM large_table WHERE id = 12345;
   201|
   202|-- 观察是否更倾向于使用索引
   203|
   204|-- 场景2: 大内存 (大部分数据在缓存中)
   205|-- 如果effective_cache_size很大
   206|-- 随机I/O实际上是从内存读取
   207|-- 可以降低random_page_cost
   208|
   209|SET random_page_cost = 1.5;
   210|SET effective_cache_size = '64GB';
   211|
   212|EXPLAIN
   213|SELECT * FROM large_table WHERE id = 12345;
   214|
   215|-- 场景3: RAID10存储
   216|-- RAID10的随机I/O性能较好
   217|-- 可以降低random_page_cost
   218|
   219|SET seq_page_cost = 1.0;
   220|SET random_page_cost = 2.0;
   221|
   222|-- 经验公式:
   223|-- HDD: random_page_cost = 4.0
   224|-- SSD: random_page_cost = 1.1 ~ 1.5
   225|-- RAID10: random_page_cost = 2.0
   226|-- NFS: random_page_cost = 3.0 ~ 5.0
   227|
   228|-- 测试最优值
   229|EXPLAIN (ANALYZE)
   230|SELECT * FROM large_table WHERE indexed_column = value;
   231|
   232|-- 尝试不同的random_page_cost
   233|-- 选择使实际执行时间最接近预估的值
   234|```
   235|
   236|### 20.3 cpu_tuple_cost (CPU元组成本)
   237|
   238|**参数定义**:
   239|
   240|```sql
   241|-- 查看当前值
   242|SHOW cpu_tuple_cost;  -- 默认: 0.01
   243|
   244|-- 参数含义:
   245|-- 处理一个tuple的CPU成本
   246|-- 包括: 解析、投影、约束检查等
   247|
   248|-- 调整语法
   249|SET cpu_tuple_cost = 0.01;
   250|```
   251|
   252|**源码分析**:
   253|
   254|```cpp
   255|// src/backend/utils/misc/guc.c
   256|{
   257|    {"cpu_tuple_cost",
   258|        PGC_USERSET,
   259|        QUERY_TUNING_COST,
   260|        gettext_noop("Sets the planner's estimate of the cost of "
   261|                     "processing each row."),
   262|        NULL
   263|    },
   264|    &u_sess->attr.attr_sql.cpu_tuple_cost,
   265|    DEFAULT_CPU_TUPLE_COST,  // 默认值: 0.01
   266|    0.0,
   267|    DBL_MAX,
   268|    NULL, NULL, NULL
   269|}
   270|
   271|// 使用位置
   272|// cost_seqscan函数
   273|
   274|void cost_seqscan(Path* path, PlannerInfo* root, 
   275|                 RelOptInfo* baserel, ParamPathInfo* param_info)
   276|{
   277|    // CPU处理成本
   278|    cpu_per_tuple = cpu_tuple_cost + qpqual_cost.per_tuple;
   279|    run_cost += cpu_per_tuple * baserel->tuples;
   280|    
   281|    // cpu_tuple_cost影响:
   282|    // 1. 顺序扫描 vs 索引扫描的选择
   283|    // 2. JOIN方法的选择 (Hash Join vs Nested Loop)
   284|    // 3. 排序 vs 索引扫描的选择
   285|}
   286|```
   287|
   288|**调整场景**:
   289|
   290|```sql
   291|-- 场景1: CPU密集型查询
   292|-- 如果查询包含大量计算 (函数、表达式)
   293|-- 可以提高cpu_tuple_cost
   294|
   295|SET cpu_tuple_cost = 0.02;
   296|
   297|EXPLAIN
   298|SELECT 
   299|    expensive_function(column1),
   300|    another_expensive_function(column2)
   301|FROM large_table;
   302|
   303|-- 优化器可能更倾向于减少行数的操作 (如先过滤)
   304|
   305|-- 场景2: IO密集型查询
   306|-- 如果IO是瓶颈 (大表扫描)
   307|-- 可以降低cpu_tuple_cost
   308|
   309|SET cpu_tuple_cost = 0.005;
   310|
   311|EXPLAIN
   312|SELECT * FROM large_table;
   313|
   314|-- 优化器可能更倾向于顺序扫描 (避免CPU开销)
   315|
   316|-- 场景3: 复杂JOIN
   317|-- cpu_tuple_cost影响Hash Join的选择
   318|-- Hash Join的CPU成本较高
   319|
   320|SET cpu_tuple_cost = 0.01;  -- 默认
   321|EXPLAIN
   322|SELECT * FROM table1 t1 JOIN table2 t2 ON t1.id = t2.id;
   323|
   324|SET cpu_tuple_cost = 0.02;  -- 提高
   325|EXPLAIN
   326|SELECT * FROM table1 t1 JOIN table2 t2 ON t1.id = t2.id;
   327|
   328|-- 观察: 提高cpu_tuple_cost后，可能更倾向于Merge Join
   329|```
   330|
   331|### 20.4 cpu_index_tuple_cost (索引元组成本)
   332|
   333|**参数定义**:
   334|
   335|```sql
   336|-- 查看当前值
   337|SHOW cpu_index_tuple_cost;  -- 默认: 0.005
   338|
   339|-- 参数含义:
   340|-- 处理一个索引条目的CPU成本
   341|-- 通常是cpu_tuple_cost的一半
   342|
   343|-- 调整语法
   344|SET cpu_index_tuple_cost = 0.005;
   345|```
   346|
   347|**源码分析**:
   348|
   349|```cpp
   350|// src/backend/utils/misc/guc.c
   351|{
   352|    {"cpu_index_tuple_cost",
   353|        PGC_USERSET,
   354|        QUERY_TUNING_COST,
   355|        gettext_noop("Sets the planner's estimate of the cost of "
   356|                     "processing each index entry."),
   357|        NULL
   358|    },
   359|    &u_sess->attr.attr_sql.cpu_index_tuple_cost,
   360|    DEFAULT_CPU_INDEX_TUPLE_COST,  // 默认值: 0.005
   361|    0.0,
   362|    DBL_MAX,
   363|    NULL, NULL, NULL
   364|}
   365|
   366|// 使用位置
   367|// cost_index函数
   368|
   369|void cost_index(Path* path, PlannerInfo* root, IndexPath* ipath,
   370|               bool is_indexjoin, List* indexQuals, 
   371|               List* indexConditionals)
   372|{
   373|    // 索引扫描的CPU成本
   374|    Cost indexCorrelation = 0.0;
   375|    Cost run_cost = 0.0;
   376|    
   377|    // 处理索引条目的成本
   378|    run_cost += cpu_index_tuple_cost * numIndexTuples;
   379|    
   380|    // cpu_index_tuple_cost影响索引扫描的吸引力
   381|    // 较低的cpu_index_tuple_cost → 更倾向于索引扫描
   382|}
   383|```
   384|
   385|**调整场景**:
   386|
   387|```sql
   388|-- 场景1: 复合索引
   389|-- 复合索引的CPU成本较高 (需要处理多个字段)
   390|-- 可以提高cpu_index_tuple_cost
   391|
   392|SET cpu_index_tuple_cost = 0.01;
   393|
   394|EXPLAIN
   395|SELECT * FROM table_with_composite_index 
   396|WHERE col1 = 1 AND col2 = 2;
   397|
   398|-- 场景2: 函数索引
   399|-- 函数索引的CPU成本更高 (需要计算函数)
   400|
   401|CREATE INDEX idx_function ON table_with_function_index 
   402|    (lower(column_name));
   403|
   404|SET cpu_index_tuple_cost = 0.015;
   405|
   406|EXPLAIN
   407|SELECT * FROM table_with_function_index 
   408|WHERE lower(column_name) = 'value';
   409|
   410|-- 场景3: 表达式索引
   411|-- 表达式索引的CPU成本最高
   412|
   413|CREATE INDEX idx_expression ON table_with_expression_index 
   414|    ((column1 * column2));
   415|
   416|SET cpu_index_tuple_cost = 0.02;
   417|
   418|EXPLAIN
   419|SELECT * FROM table_with_expression_index 
   420|WHERE column1 * column2 > 100;
   421|
   422|-- 调优建议:
   423|-- - 复杂索引 (表达式、函数): cpu_index_tuple_cost = 0.015 ~ 0.02
   424|-- - 复合索引: cpu_index_tuple_cost = 0.01
   425|-- - 单列索引: cpu_index_tuple_cost = 0.005 (默认)
   426|```
   427|
   428|### 20.5 cpu_operator_cost (操作符成本)
   429|
   430|**参数定义**:
   431|
   432|```sql
   433|-- 查看当前值
   434|SHOW cpu_operator_cost;  -- 默认: 0.0025
   435|
   436|-- 参数含义:
   437|-- 执行一个操作符或函数的CPU成本
   438|-- 用于: WHERE条件、JOIN条件、ORDER BY等
   439|
   440|-- 调整语法
   441|SET cpu_operator_cost = 0.0025;
   442|```
   443|
   444|**源码分析**:
   445|
   446|```cpp
   447|// src/backend/utils/misc/guc.c
   448|{
   449|    {"cpu_operator_cost",
   450|        PGC_USERSET,
   451|        QUERY_TUNING_COST,
   452|        gettext_noop("Sets the planner's estimate of the cost of "
   453|                     "processing each operator or function."),
   454|        NULL
   455|    },
   456|    &u_sess->attr.attr_sql.cpu_operator_cost,
   457|    DEFAULT_CPU_OPERATOR_COST,  // 默认值: 0.0025
   458|    0.0,
   459|    DBL_MAX,
   460|    NULL, NULL, NULL
   461|}
   462|
   463|// 使用位置
   464|// cost_qual_eval函数
   465|
   466|void cost_qual_eval(QualCost* cost, List* quals, PlannerInfo* root)
   467|{
   468|    cost->startup = 0.0;
   469|    cost->per_tuple = 0.0;
   470|    
   471|    // 评估每个条件
   472|    foreach qual, quals) {
   473|        QualCost qual_cost;
   474|        cost_qual_eval_node(&qual_cost, qual, root);
   475|        cost->startup += qual_cost.startup;
   476|        cost->per_tuple += qual_cost.per_tuple;
   477|    }
   478|}
   479|
   480|void cost_qual_eval_node(QualCost* cost, Node* qual, PlannerInfo* root)
   481|{
   482|    if (IsA(qual, OpExpr)) {
   483|        // 操作符表达式
   484|        cost->per_tuple += cpu_operator_cost;
   485|        
   486|        // 递归评估操作数
   487|        cost_qual_eval_node(cost, (Node*) opexpr->args, root);
   488|    }
   489|    else if (IsA(qual, FuncExpr)) {
   490|        // 函数表达式
   491|        cost->per_tuple += cpu_operator_cost * funcexpr->args_length;
   492|        
   493|        // 递归评估参数
   494|        cost_qual_eval_node(cost, (Node*) funcexpr->args, root);
   495|    }
   496|    // ... 其他节点类型
   497|}
   498|```
   499|
   500|**调整场景**:
   501|

---
> ⚓ **截断注记（2026-11-05 审计）**：本篇在历史迁移时截断于 500 行，尾部
> 小节缺失（原全量件已不可寻，待重写补齐——挂账 ROADMAP）；行号式清单为
> 源码摘录，无独立 file:line 锚。
