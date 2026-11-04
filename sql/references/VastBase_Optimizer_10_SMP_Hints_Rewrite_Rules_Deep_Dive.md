     1|# VastBase/openGauss 优化器高级特性深度分析 - SMP限制、Hint使用、Rewrite Rule
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 学习目标: 掌握SMP限制条件、Hint使用技能、Rewrite Rule配置
     5|
     6|---
     7|
     8|## 目录
     9|
    10|### 第一部分: SMP (Symmetric Multi-Processing) 并行执行限制条件
    11|- [1. SMP架构概述](#1-smp架构概述)
    12|- [2. SMP启用条件](#2-smp启用条件)
    13|- [3. SMP禁用场景](#3-smp禁用场景)
    14|- [4. SMP限制条件完整列表](#4-smp限制条件完整列表)
    15|
    16|### 第二部分: Hint (查询提示) 使用指南
    17|- [5. Hint系统架构](#5-hint系统架构)
    18|- [6. Hint类型详解](#6-hint类型详解)
    19|- [7. Hint实战案例](#7-hint实战案例)
    20|
    21|### 第三部分: Rewrite Rule (查询重写规则)
    22|- [8. Rewrite Rule概述](#8-rewrite-rule概述)
    23|- [9. Rewrite Rule配置](#9-rewrite-rule配置)
    24|- [10. Rewrite Rule实战案例](#10-rewrite-rule实战案例)
    25|
    26|---
    27|
    28|## 第一部分: SMP (Symmetric Multi-Processing) 并行执行限制条件
    29|
    30|### 1. SMP架构概述
    31|
    32|**SMP定义**:
    33|SMP (Symmetric Multi-Processing) 是openGauss的多线程并行执行框架,通过多个线程并行执行查询来提高性能。
    34|
    35|**核心数据结构**:
    36|
    37|```cpp
    38|// src/gausskernel/optimizer/path/streampath.cpp
    39|// SMP描述符
    40|
    41|typedef struct ParallelDesc {
    42|    int producerDop;       // 生产者并行度 (DOP: Degree of Parallelism)
    43|    int consumerDop;       // 消费者并行度
    44|    StreamType distriType; // 分布类型
    45|} ParallelDesc;
    46|
    47|// 分布类型
    48|typedef enum StreamType {
    49|    LOCAL_DISTRIBUTE,      // 本地分发 (同节点内线程间)
    50|    LOCAL_BROADCAST,       // 本地广播
    51|    LOCAL_ROUNDROBIN,      // 本地轮询
    52|    REMOTE_DISTRIBUTE,     // 远程分发 (跨节点)
    53|    REMOTE_BROADCAST,      // 远程广播
    54|    REMOTE_SPLIT_DISTRIBUTE // 远程分割分发 (SMP专用)
    55|} StreamType;
    56|
    57|// SMP上下文
    58|typedef struct {
    59|    bool smp_enabled;      // SMP是否启用
    60|    int query_dop;         // 查询并行度
    61|    double smp_thread_cost; // SMP线程成本
    62|} OptimizerContext;
    63|```
    64|
    65|**SMP执行流程**:
    66|
    67|```
    68|1. 查询解析与优化
    69|   ↓
    70|2. 检查SMP是否可用 (u_sess->opt_cxt.smp_enabled)
    71|   ↓
    72|3. 生成并行路径 (考虑SMP代价)
    73|   ↓
    74|4. 选择最优并行计划
    75|   ↓
    76|5. 创建执行计划 (包含Stream节点进行数据重分布)
    77|   ↓
    78|6. 执行器启动多个线程
    79|   ↓
    80|7. 线程间通过Stream节点交换数据
    81|   ↓
    82|8. 收集结果并返回
    83|```
    84|
    85|---
    86|
    87|### 2. SMP启用条件
    88|
    89|**全局配置参数**:
    90|
    91|```sql
    92|-- 1. 启用SMP (默认开启)
    93|SET enable_smp = on;
    94|
    95|-- 2. 设置查询并行度 (默认2)
    96|SET query_dop = 4;
    97|
    98|-- 3. 设置SMP线程数
    99|SET smp_thread_count = 16;
   100|
   101|-- 4. SMP表大小阈值
   102|SET min_parallel_table_scan_size = '8MB';
   103|SET min_parallel_index_scan_size = '512kB';
   104|
   105|-- 5. 每个Gather的Worker数
   106|SET max_parallel_workers_per_gather = 4;
   107|```
   108|
   109|**自动启用SMP的条件**:
   110|
   111|```cpp
   112|// src/gausskernel/optimizer/path/allpaths.cpp
   113|// check_smp_available函数
   114|
   115|bool check_smp_available(PlannerInfo* root, RelOptInfo* rel)
   116|{
   117|    // 1. 检查全局SMP开关
   118|    if (!u_sess->attr.attr_sql.enable_smp) {
   119|        return false;
   120|    }
   121|    
   122|    // 2. 检查表大小
   123|    if (rel->pages < min_parallel_table_scan_size) {
   124|        return false;  // 表太小,不值得并行
   125|    }
   126|    
   127|    // 3. 检查并行度
   128|    if (u_sess->opt_cxt.query_dop <= 1) {
   129|        return false;  // 并行度为1,相当于不并行
   130|    }
   131|    
   132|    // 4. 检查可用Worker数
   133|    if (get_available_smp_workers() < u_sess->opt_cxt.query_dop) {
   134|        return false;  // 可用Worker不足
   135|    }
   136|    
   137|    // 5. 检查是否在事务中
   138|    if (IsTransactionBlock()) {
   139|        return false;  // 事务块中禁用SMP
   140|    }
   141|    
   142|    return true;
   143|}
   144|```
   145|
   146|---
   147|
   148|### 3. SMP禁用场景
   149|
   150|**场景1: 事务中的查询**
   151|
   152|```sql
   153|BEGIN;
   154|-- 以下查询不会使用SMP
   155|SELECT * FROM large_table;
   156|COMMIT;
   157|
   158|-- 原因: 事务中需要保证事务隔离性和一致性,SMP线程间协调复杂
   159|```
   160|
   161|**场景2: INSERT/UPDATE/DELETE中的子查询**
   162|
   163|```sql
   164|-- 不使用SMP
   165|INSERT INTO small_table
   166|SELECT * FROM large_table;
   167|
   168|-- 原因: DML操作的子查询禁用SMP,避免数据一致性问题
   169|```
   170|
   171|**场景3: 递归CTE (WITH RECURSIVE)**
   172|
   173|```sql
   174|-- 不使用SMP
   175|WITH RECURSIVE cte AS (
   176|    SELECT 1 AS n
   177|    UNION ALL
   178|    SELECT n + 1 FROM cte WHERE n < 100
   179|)
   180|SELECT * FROM cte;
   181|
   182|-- 原因: 递归查询的迭代特性不适合并行
   183|```
   184|
   185|**场景4: 游标查询**
   186|
   187|```sql
   188|BEGIN;
   189|DECLARE cur CURSOR FOR SELECT * FROM large_table;
   190|FETCH 10 FROM cur;
   191|COMMIT;
   192|
   193|-- 原因: 游标需要保持状态,并行会破坏状态管理
   194|```
   195|
   196|**场景5: 带有volatile函数的查询**
   197|
   198|```sql
   199|-- 不使用SMP
   200|SELECT *, random() FROM large_table;
   201|
   202|-- 原因: volatile函数在并行环境下执行顺序不确定
   203|```
   204|
   205|---
   206|
   207|### 4. SMP限制条件完整列表
   208|
   209|#### 4.1 系统级限制
   210|
   211|```sql
   212|-- 1. SMP全局开关
   213|-- 参数: enable_smp
   214|-- 默认: on
   215|-- 说明: 必须为on才能启用SMP
   216|SHOW enable_smp;
   217|
   218|-- 2. 查询并行度
   219|-- 参数: query_dop
   220|-- 默认: 1
   221|-- 范围: 1-512
   222|-- 说明: query_dop > 1 才会使用SMP
   223|SHOW query_dop;
   224|
   225|-- 3. 最大SMP线程数
   226|-- 参数: max_smp_thread_count
   227|-- 默认: 系统CPU核数
   228|-- 说明: 系统级线程数上限
   229|SHOW max_smp_thread_count;
   230|
   231|-- 4. 每个查询的最大线程数
   232|-- 参数: smp_thread_count
   233|-- 默认: 4
   234|-- 说明: 单个查询最多使用的线程数
   235|SHOW smp_thread_count;
   236|
   237|-- 5. SMP线程成本
   238|-- 参数: smp_thread_cost
   239|-- 默认: 0.05
   240|-- 说明: 每个SMP线程的启动和通信成本
   241|SHOW smp_thread_cost;
   242|```
   243|
   244|#### 4.2 表级限制
   245|
   246|```sql
   247|-- 1. 最小并行扫描表大小
   248|-- 参数: min_parallel_table_scan_size
   249|-- 默认: 8MB
   250|-- 说明: 表大小 < 8MB 时不使用并行扫描
   251|SHOW min_parallel_table_scan_size;
   252|
   253|-- 示例:
   254|-- 表large_table (100MB) → 启用SMP ✓
   255|-- 表small_table (2MB)   → 禁用SMP ✗
   256|
   257|-- 2. 最小并行索引扫描大小
   258|-- 参数: min_parallel_index_scan_size
   259|-- 默认: 512kB
   260|-- 说明: 索引扫描大小 < 512kB 时不使用并行
   261|SHOW min_parallel_index_scan_size;
   262|```
   263|
   264|#### 4.3 查询级限制
   265|
   266|**限制1: 事务中的查询**
   267|
   268|```sql
   269|-- 限制条件
   270|BEGIN;
   271|SELECT * FROM large_table;  -- 不使用SMP
   272|COMMIT;
   273|
   274|-- 源码检查
   275|// src/gausskernel/optimizer/plan/planner.cpp
   276|if (IsTransactionBlock()) {
   277|    u_sess->opt_cxt.smp_enabled = false;
   278|}
   279|```
   280|
   281|**限制2: DML中的子查询**
   282|
   283|```sql
   284|-- 限制条件
   285|INSERT INTO t1 SELECT * FROM large_table;  -- 子查询不使用SMP
   286|UPDATE t1 SET col = (SELECT AVG(col) FROM large_table);
   287|DELETE FROM t1 WHERE id IN (SELECT id FROM large_table);
   288|
   289|-- 源码检查
   290|// src/gausskernel/optimizer/commands/copy.cpp
   291|bool old_smp_enabled = u_sess->opt_cxt.smp_enabled;
   292|u_sess->opt_cxt.smp_enabled = false;  // 强制禁用
   293|// ... 执行COPY/INSERT/UPDATE/DELETE
   294|u_sess->opt_cxt.smp_enabled = old_smp_enabled;
   295|```
   296|
   297|**限制3: 递归查询**
   298|
   299|```sql
   300|-- 限制条件
   301|WITH RECURSIVE cte AS (...)
   302|SELECT * FROM cte;  -- 不使用SMP
   303|
   304|-- 源码检查
   305|// src/gausskernel/optimizer/prep/prepunion.cpp
   306|// For distributed with-recursive processing we currently disable the SMP
   307|if (is_recursive_union(query)) {
   308|    u_sess->opt_cxt.smp_enabled = false;
   309|}
   310|```
   311|
   312|**限制4: 游标查询**
   313|
   314|```sql
   315|-- 限制条件
   316|DECLARE cur CURSOR FOR SELECT * FROM large_table;  -- 不使用SMP
   317|
   318|-- 原因: 游标需要保持状态,并行会破坏状态管理
   319|```
   320|
   321|**限制5: 带有volatile函数的查询**
   322|
   323|```sql
   324|-- 限制条件
   325|SELECT *, random(), now() FROM large_table;  -- 不使用SMP
   326|
   327|-- 原因: volatile函数执行顺序不确定
   328|```
   329|
   330|**限制6: FOR UPDATE/SHARE查询**
   331|
   332|```sql
   333|-- 限制条件
   334|SELECT * FROM large_table FOR UPDATE;  -- 不使用SMP
   335|
   336|-- 原因: 行锁需要顺序获取
   337|```
   338|
   339|#### 4.4 节点级限制
   340|
   341|**限制1: Stream节点数量**
   342|
   343|```sql
   344|-- 限制条件: Stream节点过多时禁用SMP
   345|-- 参数: max_stream_num
   346|-- 默认: 100
   347|-- 说明: 超过阈值时禁用SMP,避免计划过于复杂
   348|
   349|-- 检查代码
   350|// src/gausskernel/optimizer/plan/streamplan_utils.cpp
   351|if (stream_node_count > max_stream_num) {
   352|    u_sess->opt_cxt.smp_enabled = false;
   353|}
   354|```
   355|
   356|**限制2: JOIN数量**
   357|
   358|```sql
   359|-- 限制条件: JOIN过多时禁用SMP
   360|-- 参数: max_join_num
   361|-- 默认: 无限制
   362|-- 说明: 复杂多表JOIN可能禁用SMP
   363|
   364|-- 检查代码
   365|// 实际上openGauss没有这个硬限制,但复杂的JOIN可能导致Stream节点过多
   366|```
   367|
   368|#### 4.5 资源限制
   369|
   370|**限制1: 内存不足**
   371|
   372|```sql
   373|-- 限制条件: work_mem不足
   374|-- 说明: 并行执行需要更多内存,work_mem不足时禁用SMP
   375|
   376|-- 检查逻辑
   377|if (estimated_memory_per_thread * query_dop > work_mem) {
   378|    u_sess->opt_cxt.smp_enabled = false;
   379|}
   380|```
   381|
   382|**限制2: CPU核心数不足**
   383|
   384|```sql
   385|-- 限制条件: 可用CPU核心数不足
   386|-- 说明: query_dop不能超过可用CPU核心数
   387|
   388|-- 检查代码
   389|// src/gausskernel/optimizer/path/allpaths.cpp
   390|int available_workers = get_available_smp_workers();
   391|if (query_dop > available_workers) {
   392|    u_sess->opt_cxt.smp_enabled = false;
   393|}
   394|```
   395|
   396|---
   397|
   398|## 第二部分: Hint (查询提示) 使用指南
   399|
   400|### 5. Hint系统架构
   401|
   402|**Hint语法**:
   403|
   404|```sql
   405|-- Hint格式
   406|/*+ [HintKeyword] ([HintOptions]) */
   407|
   408|-- 示例
   409|SELECT /*+ HashJoin(a b) */ * FROM table1 a JOIN table2 b ON a.id = b.id;
   410|```
   411|
   412|**Hint数据结构**:
   413|
   414|```cpp
   415|// src/include/parser/parse_hint.h
   416|
   417|// Hint基类
   418|struct Hint {
   419|    NodeTag type;             // Hint类型
   420|    List* relnames;           // 关系列表
   421|    HintKeyword hint_keyword; // Hint关键字
   422|    HintStatus state;         // Hint状态 (NOTUSED/USED/DUPLICATION/ERROR)
   423|};
   424|
   425|// Hint关键字枚举
   426|typedef enum HintKeyword {
   427|    HINT_KEYWORD_NESTLOOP = 0,
   428|    HINT_KEYWORD_MERGEJOIN,
   429|    HINT_KEYWORD_HASHJOIN,
   430|    HINT_KEYWORD_LEADING,        // 指定JOIN顺序
   431|    HINT_KEYWORD_ROWS,           // 修正行数估算
   432|    HINT_KEYWORD_BROADCAST,      // 广播分布
   433|    HINT_KEYWORD_REDISTRIBUTE,   // 重分布
   434|    HINT_KEYWORD_BLOCKNAME,      // 块名称
   435|    HINT_KEYWORD_TABLESCAN,      // 扫描方法
   436|    HINT_KEYWORD_INDEXSCAN,
   437|    HINT_KEYWORD_INDEXONLYSCAN,
   438|    HINT_KEYWORD_SKEW,           // 数据倾斜
   439|    HINT_KEYWORD_PREDPUSH,       // 谓词推入
   440|    HINT_KEYWORD_PREDPUSH_SAME_LEVEL,
   441|    HINT_KEYWORD_REWRITE,        // 查询重写规则
   442|    HINT_KEYWORD_GATHER,         // 收集模式
   443|    HINT_KEYWORD_NO_EXPAND,      // 禁止扩展
   444|    HINT_KEYWORD_SET,            // 设置参数
   445|    HINT_KEYWORD_CPLAN,          // 使用自定义计划
   446|    HINT_KEYWORD_GPLAN,          // 使用通用计划
   447|    HINT_KEYWORD_IGNORE,         // 忽略错误
   448|} HintKeyword;
   449|```
   450|
   451|**Hint处理流程**:
   452|
   453|```
   454|1. 词法/语法分析
   455|   - 识别注释中的Hint
   456|   ↓
   457|2. Hint解析
   458|   - 解析Hint关键字和参数
   459|   - 验证Hint语法
   460|   ↓
   461|3. Hint转换
   462|   - 将表名转换为Relids
   463|   - 将列名转换为AttrNumber
   464|   ↓
   465|4. Hint应用
   466|   - 在优化器各个阶段应用Hint
   467|   - 记录Hint使用状态
   468|   ↓
   469|5. 执行计划生成
   470|   - 生成符合Hint的计划
   471|   ↓
   472|6. Hint状态报告
   473|   - EXPLAIN中显示Hint使用状态
   474|```
   475|
   476|---
   477|
   478|### 6. Hint类型详解
   479|
   480|#### 6.1 JOIN方法Hint
   481|
   482|**语法**:
   483|
   484|```sql
   485|-- 强制使用HashJoin
   486|/*+ HashJoin(table1 table2) */
   487|
   488|-- 禁止使用NestLoop
   489|/*+ No NestLoop(table1 table2) */
   490|```
   491|
   492|**示例**:
   493|
   494|```sql
   495|-- 示例1: 强制使用HashJoin
   496|EXPLAIN
   497|SELECT /*+ HashJoin(orders customers) */ *
   498|FROM orders o
   499|JOIN customers c ON o.customer_id = c.customer_id;
   500|
   501|