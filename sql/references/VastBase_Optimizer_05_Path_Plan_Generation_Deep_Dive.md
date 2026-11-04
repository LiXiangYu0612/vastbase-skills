     1|# VastBase/openGauss Path生成与Plan创建超详细深度分析
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 分析文件: path/*.cpp (20+文件), plan/createplan.cpp (7000+行)  
     5|> 学习目标: 掌握Path节点生成、Plan节点创建的完整流程
     6|
     7|---
     8|
     9|## 目录
    10|
    11|### 第六部分: Path生成与Plan创建
    12|- [28. Path数据结构与继承关系](#28-path数据结构与继承关系)
    13|- [29. 路径生成算法](#29-路径生成算法)
    14|- [30. Plan节点创建](#30-plan节点创建)
    15|- [31. 执行计划生成完整流程](#31-执行计划生成完整流程)
    16|
    17|---
    18|
    19|## 28. Path数据结构与继承关系
    20|
    21|### 28.1 Path基础结构
    22|
    23|**源码分析**:
    24|
    25|```cpp
    26|// src/include/nodes/relation.h
    27|// Path节点的核心结构
    28|
    29|typedef struct Path {
    30|    NodeTag     type;           // 节点类型
    31|    
    32|    // 路径类型标识
    33|    NodeTag     pathtype;       // T_SeqScan, T_IndexScan, T_NestLoop, etc.
    34|    
    35|    // 父RelOptInfo指针
    36|    RelOptInfo* parent;         // 该路径代表的关系
    37|    
    38|    // 路径代价估计
    39|    Cost        startup_cost;   // 启动代价 (输出第一行前的代价)
    40|    Cost        total_cost;     // 总代价 (输出所有行的代价)
    41|    
    42|    // 行数估计
    43|    double      rows;           // 估计输出行数
    44|    
    45|    // 路径键 (排序信息)
    46|    List*       pathkeys;       // PathKey* 列表
    47|    
    48|    // 其他参数
    49|    List*       pathorder;      // 路径排序信息 (仅部分路径使用)
    50|    Cardinality limit_rows;     // 限制行数 (LIMIT子句)
    51|    
    52|    // 并行度
    53|    int         parallel_degree;// 并行工作线程数
    54|    
    55|    // 并行安全
    56|    bool        parallel_safe;  // 是否可以并行执行
    57|    
    58|    // 分区信息
    59|    Oid         partOid;        // 分区表OID
    60|    List*       prunedParts;    // 裁剪的分区列表
    61|    
    62|    // 流信息
    63|    double      stream_cost;    // 流式传输代价
    64|    
    65|    // 附加信息
    66|    Bitmapset*  parentids;      // 父关系ID集合
    67|    
    68|} Path;
    69|
    70|// PathType枚举
    71|typedef enum PathType {
    72|    T_SeqScan,          // 顺序扫描
    73|    T_IndexScan,        // 索引扫描
    74|    T_IndexOnlyScan,    // 仅索引扫描
    75|    T_BitmapScan,       // 位图扫描
    76|    T_TidScan,          // TID扫描 (CTID扫描)
    77|    T_SubqueryScan,     // 子查询扫描
    78|    T_FunctionScan,     // 函数扫描
    79|    T_ValuesScan,       // Values扫描
    80|    T_CteScan,          // CTE扫描
    81|    T_WorkTableScan,    // 工作表扫描 (递归查询)
    82|    T_ForeignScan,      // 外部表扫描
    83|    T_NestLoop,         // 嵌套循环连接
    84|    T_MergeJoin,        // 归并连接
    85|    T_HashJoin,         // 哈希连接
    86|    T_Material,         // 物化节点
    87|    T_Sort,             // 排序节点
    88|    T_Group,            // 分组节点
    89|    T_Agg,              // 聚合节点
    90|    T_Window,           // 窗口函数节点
    91|    T_Unique,           // 去重节点
    92|    T_Gather,           // 收集节点 (并行)
    93|    T_GatherMerge,      // 收集归并节点 (并行)
    94|    T_Hash,             // 哈希节点
    95|    T_SetOp,            // 集合操作节点
    96|    T_LockRows,         // 锁行节点 (SELECT FOR UPDATE)
    97|    T_Limit,            // 限制节点 (LIMIT/OFFSET)
    98|    T_NestLoopPath,     // 嵌套循环路径 (未转换为Plan)
    99|    T_MergeJoinPath,    // 归并连接路径
   100|    T_HashJoinPath,     // 哈希连接路径
   101|    ...
   102|} PathType;
   103|```
   104|
   105|**Path类型分类**:
   106|
   107|1. **扫描路径 (Scan Paths)**:
   108|   - SeqScanPath: 顺序扫描路径
   109|   - IndexPath: 索引扫描路径
   110|   - BitmapHeapPath: 位图堆表扫描路径
   111|   - TidPath: TID扫描路径
   112|   - SubqueryScanPath: 子查询扫描路径
   113|
   114|2. **连接路径 (Join Paths)**:
   115|   - NestPath: 嵌套循环连接路径
   116|   - MergePath: 归并连接路径
   117|   - HashPath: 哈希连接路径
   118|
   119|3. **修改路径 (Modification Paths)**:
   120|   - ModifyTablePath: 修改表路径 (INSERT/UPDATE/DELETE)
   121|
   122|### 28.2 IndexPath结构
   123|
   124|**源码分析**:
   125|
   126|```cpp
   127|// src/include/nodes/relation.h
   128|// IndexPath: 索引扫描路径
   129|
   130|typedef struct IndexPath {
   131|    Path        path;           // 基础Path结构
   132|    
   133|    // 索引信息
   134|    IndexOptInfo* indexinfo;    // 索引优化信息
   135|    List*       indexclauses;   // 索引子句列表
   136|    List*       indexquals;     // 索引条件子句
   137|    List*       indexorderbys;  // 索引排序子句
   138|    
   139|    // 扫描方向
   140|    ScanDirection indexscandir; // 扫描方向 (ForwardScanDirection/BackwardScanDirection)
   141|    
   142|    // 索引扫描类型
   143|    bool        isjoininner;    // 是否作为内表连接
   144|    bool        isjoinouter;    // 是否作为外表连接
   145|    
   146|    // 参数化路径
   147|    List*       indexclauses;   // 参数化条件
   148|    
   149|    // 索引返回条件
   150|    List*       indexqualcols;  // 索引条件列
   151|    
   152|    // 位图扫描
   153|    bool        is_bitmap;      // 是否位图扫描
   154|    
   155|} IndexPath;
   156|
   157|// IndexOptInfo结构
   158|typedef struct IndexOptInfo {
   159|    NodeTag     type;
   160|    
   161|    // 索引基本信息
   162|    Oid         indexoid;       // 索引OID
   163|    Oid         reltablespace;  // 表空间OID
   164|    Oid         relid;          // 表OID
   165|    
   166|    // 索引列信息
   167|    int         ncolumns;       // 索引列数
   168|    int*        indexkeys;      // 索引键 (表列号)
   169|    Oid*        indexcollations;// 排序规则OID
   170|    Oid*        opfamily;       // 操作符族OID
   171|    int*        indexopcos;     // 操作符OID
   172|    
   173|    // 索引类型
   174|    Oid         relam;          // 索引访问方法OID
   175|    char*       indpred;        // 索引谓词 (部分索引)
   176|    
   177|    // 统计信息
   178|    double      pages;          // 索引页数
   179|    double      tuples;         // 索引元组数
   180|    
   181|    // 唯一性
   182|    bool        unique;         // 是否唯一索引
   183|    bool        immediate;      // 是否立即约束
   184|    
   185|    // 是否支持排序
   186|    bool        amcanorder;     // 访问方法是否支持有序扫描
   187|    
   188|    // 是否支持返回
   189|    bool        amcanreturn;    // 访问方法是否支持仅索引扫描
   190|    
   191|    // 是否支持位图扫描
   192|    bool        amcanbitmap;    // 访问方法是否支持位图扫描
   193|    
   194|    // 其他
   195|    List*       indextlist;     // 索引目标列表
   196|    
   197|} IndexOptInfo;
   198|```
   199|
   200|**实际应用**:
   201|
   202|```sql
   203|-- 创建测试表和索引
   env-T|CREATE TABLE index_test (
   205|    id INT PRIMARY KEY,
   206|    name VARCHAR(100),
   207|    age INT,
   208|    email VARCHAR(200)
   209|);
   210|
   211|CREATE INDEX idx_name ON index_test(name);
   212|CREATE INDEX idx_age ON index_test(age);
   213|
   214|ANALYZE index_test;
   215|
   216|-- 查询: 使用name索引
   217|EXPLAIN
   218|SELECT * FROM index_test WHERE name = 'John';
   219|
   220|-- Path节点生成流程:
   221|-- 1. 考虑所有可用索引 (idx_name, idx_age, index_test_pkey)
   222|-- 2. 对每个索引，检查是否可用于查询条件
   223|-- 3. 生成多个IndexPath:
   224|--    - IndexPath1: 使用idx_name (WHERE name = 'John')
   225|--    - IndexPath2: 使用index_test_pkey (但不匹配条件)
   226|-- 4. 计算每个IndexPath的代价
   227|-- 5. 选择代价最小的路径
   228|
   229|-- 查看代价估算
   230|EXPLAIN
   231|SELECT * FROM index_test WHERE name = 'John';
   232|
   233|-- 输出示例:
   234|-- Index Scan using idx_name on index_test  (cost=0.00..8.27 rows=1)
   235|--   Index Cond: (name = 'John'::varchar)
   236|--
   237|-- cost=0.00..8.27: 
   238|--   startup_cost = 0.00 (索引扫描从索引根部开始)
   239|--   total_cost = 8.27 (包括索引扫描 + 堆表访问)
   240|-- rows = 1: 估计返回1行
   241|```
   242|
   243|### 28.3 JoinPath结构
   244|
   245|**源码分析**:
   246|
   247|```cpp
   248|// src/include/nodes/relation.h
   249|// JoinPath: 连接路径的基类
   250|
   251|typedef struct JoinPath {
   252|    Path        path;           // 基础Path结构
   253|    
   254|    // 连接类型
   255|    JoinType    jointype;       // JOIN_INNER, JOIN_LEFT, JOIN_FULL, etc.
   256|    
   257|    // 外表和内表路径
   258|    Path*       outerjoinpath;  // 外表路径
   259|    Path*       innerjoinpath;  // 内表路径
   260|    
   261|    // 连接条件
   262|    List*       joinrestrictinfo;// 连接限制信息
   263|    
   264|    // 其他
   265|    Path*       outer_path;     // 外表路径 (旧字段,保留兼容)
   266|    Path*       inner_path;     // 内表路径
   267|    
   268|} JoinPath;
   269|
   270|// NestPath: 嵌套循环连接路径
   271|typedef struct NestPath {
   272|    JoinPath    jpath;          // 基础JoinPath结构
   273|    
   274|    // 内表路径 (参数化)
   275|    Path*       inner_path;     // 可能是参数化路径
   276|    
   277|    // 其他
   278|    List*       join_clause;    // 连接子句
   279|    
   280|} NestPath;
   281|
   282|// MergePath: 归并连接路径
   283|typedef struct MergePath {
   284|    JoinPath    jpath;          // 基础JoinPath结构
   285|    
   286|    // 归并子句
   287|    List*       mergeclauses;   // 归并子句
   288|    List*       outer_pathkeys; // 外表路径键
   289|    List*       inner_pathkeys; // 内表路径键
   290|    
   291|    // 排序标记
   292|    bool        skip_mark_restore; // 是否跳过标记恢复
   293|    
   294|} MergePath;
   295|
   296|// HashPath: 哈希连接路径
   297|typedef struct HashPath {
   298|    JoinPath    jpath;          // 基础JoinPath结构
   299|    
   300|    // 哈希子句
   301|    List*       hashclauses;    // 哈希子句
   302|    
   303|    // 哈希表信息
   304|    Path*       inner_path;     // 内表路径 (用于构建哈希表)
   305|    
   306|    // 其他
   307|    bool        skewable;       // 是否倾斜优化
   308|    
   309|} HashPath;
   310|```
   311|
   312|**实际应用**:
   313|
   314|```sql
   315|-- 创建测试表
   316|CREATE TABLE employees (
   317|    emp_id INT PRIMARY KEY,
   318|    emp_name VARCHAR(100),
   319|    dept_id INT
   320|);
   321|
   322|CREATE TABLE departments (
   323|    dept_id INT PRIMARY KEY,
   324|    dept_name VARCHAR(100)
   325|);
   326|
   327|-- 插入数据
   328|INSERT INTO employees 
   329|SELECT generate_series(1, 10000), 
   330|       'employee_' || generate_series(1, 10000),
   331|       (random() * 100)::int;
   332|
   333|INSERT INTO departments 
   334|SELECT generate_series(1, 100), 
   335|       'department_' || generate_series(1, 100);
   336|
   337|ANALYZE employees, departments;
   338|
   339|-- 查询: JOIN
   340|EXPLAIN
   341|SELECT * FROM employees e
   342|JOIN departments d ON e.dept_id = d.dept_id;
   343|
   344|-- Path节点生成流程:
   345|-- 1. 生成外表路径 (employees):
   346|--    - SeqScanPath: 顺序扫描employees
   347|--    - IndexPath: 可能使用dept_id索引 (如果有)
   348|--
   349|-- 2. 生成内表路径 (departments):
   350|--    - SeqScanPath: 顺序扫描departments
   351|--    - IndexPath: 使用dept_id主键
   352|--
   353|-- 3. 生成连接路径:
   354|--    - NestPath:
   355|--      outer = SeqScan(employees)
   356|--      inner = IndexScan(departments using dept_id_pkey)
   357|--      jointype = JOIN_INNER
   358|--
   359|--    - MergePath:
   360|--      outer = Sort(employees by dept_id)
   361|--      inner = IndexScan(departments by dept_id_pkey)
   362|--      mergeclauses = (e.dept_id = d.dept_id)
   363|--
   364|--    - HashPath:
   365|--      outer = SeqScan(employees)
   366|--      inner = SeqScan(departments)
   367|--      hashclauses = (e.dept_id = d.dept_id)
   368|--
   369|-- 4. 计算每个连接路径的代价:
   370|--    - NestPath cost = outer_cost + (outer_rows * inner_cost)
   371|--    - MergePath cost = outer_cost + inner_cost + sort_cost
   372|--    - HashPath cost = outer_cost + inner_cost + hash_build_cost
   373|--
   374|-- 5. 选择代价最小的路径
   375|
   376|-- 查看执行计划
   377|EXPLAIN
   378|SELECT * FROM employees e
   379|JOIN departments d ON e.dept_id = d.dept_id;
   380|
   381|-- 可能的输出:
   382|-- Hash Join  (cost=250.00..350.00 rows=10000)
   383|--   Hash Cond: (e.dept_id = d.dept_id)
   384|--   ->  Seq Scan on employees e  (cost=0.00..150.00 rows=10000)
   385|--   ->  Hash  (cost=50.00..50.00 rows=100)
   386|--        ->  Seq Scan on departments d  (cost=0.00..50.00 rows=100)
   387|--
   388|-- 或者:
   389|-- Nested Loop  (cost=0.00..450.00 rows=10000)
   390|--   Join Filter: (e.dept_id = d.dept_id)
   391|--   ->  Seq Scan on employees e  (cost=0.00..150.00 rows=10000)
   392|--   ->  Index Scan using departments_pkey on departments d  (cost=0.00..0.03 rows=1)
   393|--        Index Cond: (dept_id = e.dept_id)
   394|```
   395|
   396|---
   397|
   398|## 29. 路径生成算法
   399|
   400|### 29.1 扫描路径生成
   401|
   402|**源码分析**:
   403|
   404|```cpp
   405|// src/gausskernel/optimizer/path/allpaths.cpp
   406|// add_base_rels_to_query函数
   407|
   408|void add_base_rels_to_query(PlannerInfo* root, Node* jtnode)
   409|{
   410|    if (IsA(jtnode, RangeTblRef)) {
   411|        // 基础关系
   412|        int varno = ((RangeTblRef*) jtnode)->rtindex;
   413|        RelOptInfo* rel = build_simple_rel(root, varno, RELOPT_BASEREL);
   414|    }
   415|    else if (IsA(jtnode, FromExpr)) {
   416|        // FROM表达式
   417|        FromExpr* f = (FromExpr*) jtnode;
   418|        foreach(l, f->fromlist) {
   419|            add_base_rels_to_query(root, lfirst(l));
   420|        }
   421|    }
   422|    else if (IsA(jtnode, JoinExpr)) {
   423|        // JOIN表达式
   424|        JoinExpr* j = (JoinExpr*) jtnode;
   425|        add_base_rels_to_query(root, j->larg);
   426|        add_base_rels_to_query(root, j->rarg);
   427|    }
   428|}
   429|
   430|// build_simple_rel函数
   431|RelOptInfo* build_simple_rel(
   432|    PlannerInfo* root,
   433|    int relid,
   434|    RelOptKind reloptkind)
   435|{
   436|    // 创建RelOptInfo
   437|    RelOptInfo* rel = makeNode(RelOptInfo);
   438|    rel->reloptkind = reloptkind;
   439|    rel->relid = relid;
   440|    
   441|    // 获取RangeTblEntry
   442|    RangeTblEntry* rte = planner_rt_fetch(relid, root);
   443|    
   444|    // 设置关系信息
   445|    if (rte->rtekind == RTE_RELATION) {
   446|        rel->reltablespace = rte->reltablespace;
   447|        rel->relid = rte->relid;
   448|        rel->rtekind = rte->rtekind;
   449|    }
   450|    
   451|    // 添加到rel数组
   452|    root->simple_rel_array[relid] = rel;
   453|    root->simple_rel_array_size = Max(root->simple_rel_array_size, relid + 1);
   454|    
   455|    return rel;
   456|}
   457|
   458|// src/gausskernel/optimizer/path/allpaths.cpp
   459|// make_rel_from_joinlist函数
   460|
   461|void make_rel_from_joinlist(PlannerInfo* root, List* joinlist)
   462|{
   463|    int         levels_needed = list_length(joinlist);
   464|    int         lev;
   465|    RelOptInfo* rel = NULL;
   466|    
   467|    // 递归生成关系
   468|    for (lev = 1; lev <= levels_needed; lev++) {
   469|        // 生成N表关系的路径
   470|        join_search_one_level(root, lev);
   471|        
   472|        // 应用约束排除
   473|        apply_join_restriction(root);
   474|    }
   475|}
   476|```
   477|
   478|### 29.2 索引路径生成
   479|
   480|**源码分析**:
   481|
   482|```cpp
   483|// src/gausskernel/optimizer/path/indxpath.cpp
   484|// create_index_paths函数
   485|
   486|void create_index_paths(PlannerInfo* root, RelOptInfo* rel)
   487|{
   488|    // 遍历所有索引
   489|    foreach(lc, rel->indexlist) {
   490|        IndexOptInfo* index = (IndexOptInfo*) lfirst(lc);
   491|        
   492|        // 1. 考虑普通索引扫描
   493|        consider_indexed_clauses(root, rel, index, clause_groups);
   494|        
   495|        // 2. 考虑位图索引扫描
   496|        consider_index_join_clauses(root, rel, index, clause_groups);
   497|        
   498|        // 3. 考虑仅索引扫描
   499|        if (index->amcanreturn) {
   500|            consider_index_only_scan(root, rel, index);
   501|