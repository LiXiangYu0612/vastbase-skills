     1|# VastBase/openGauss 优化器执行器接口与并行执行深度分析
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 聚焦: 执行器接口、并行执行机制、执行节点详解
     5|
     6|---
     7|
     8|## 目录
     9|
    10|### 第八部分: 执行器架构
    11|- [43. 执行器框架与接口](#43-执行器框架与接口)
    12|- [44. 执行节点详解](#44-执行节点详解)
    13|- [45. 表达式计算与求值](#45-表达式计算与求值)
    14|- [46. 并行执行机制](#46-并行执行机制)
    15|
    16|---
    17|
    18|## 43. 执行器框架与接口
    19|
    20|### 43.1 执行器整体架构
    21|
    22|**核心数据结构**:
    23|
    24|```cpp
    25|// src/include/nodes/execnodes.h
    26|// EState: 执行器状态
    27|
    28|typedef struct EState {
    29|    NodeTag     type;
    30|    
    31|    // 查询计划
    32|    Plan*       es_plannedstmt;    // 查询计划树
    33|    
    34|    // 查询描述符
    35|    QueryDesc*  es_queryDesc;      // 查询描述符
    36|    
    37|    // 结果元组
    38|    TupleTableSlot* es_result_tuple_slot;  // 结果元组槽
    39|    
    40|    // 目标列表
    41|    List*       es_expr_ctx;       // 表达式上下文列表
    42|    
    43|    // 内存上下文
    44|    MemoryContext es_query_cxt;    // 查询内存上下文
    45|    MemoryContext es_per_tuple_memory_context;  // 每元组内存上下文
    46|    
    47|    // 执行状态
    48|    int         es_direction;      // 扫描方向
    49|    bool        es_instrument;     // 是否 instrumentation
    50|    
    51|    // 统计信息
    52|    Instrumentation* es_instrument; // 执行统计信息
    53|    
    54|    // 其他
    55|    void*       es_junkFilter;     // junk filter
    56|    List*       es_trig_target_relids;  // 触发器目标关系
    57|    
    58|} EState;
    59|
    60|// src/include/nodes/execnodes.h
    61|// TupleTableSlot: 元组槽 (元组容器)
    62|
    63|typedef struct TupleTableSlot {
    64|    NodeTag     type;
    65|    
    66|    // 元组数据
    67|    HeapTuple   tts_tuple;         // HeapTuple (row-store)
    68|    MinimalTuple tts_mintuple;     // MinimalTuple (compressed)
    69|    
    70|    // 元组描述符
    71|    TupleDesc   tts_tupleDescriptor; // 元组描述符
    72|    
    73|    // 值数组
    74|    Datum*      tts_values;        // Datum数组
    75|    bool*       tts_isnull;        // NULL标记数组
    76|    
    77|    // 标志位
    78|    bool        tts_shouldFree;    // 是否应该释放元组
    79|    bool        tts_shouldFreeMin; // 是否应该释放MinimalTuple
    80|    bool        tts_isempty;       // 是否为空
    81|    
    82|    // 缓冲区
    83|    Buffer      tts_buffer;        // 磁盘缓冲区
    84|    
    85|    // 虚拟元组
    86|    HeapTuple   tts_tupleHeader;   // 元组头部
    87|    
    88|} TupleTableSlot;
    89|
    90|// ExprContext: 表达式上下文
    91|typedef struct ExprContext {
    92|    NodeTag     type;
    93|    
    94|    // 元组槽
    95|    TupleTableSlot* ecxt_scantuple;    // 扫描元组
    96|    TupleTableSlot* ecxt_innertuple;   // 内表元组
    97|    TupleTableSlot* ecxt_outertuple;   // 外表元组
    98|    
    99|    // 值数组
   100|    Datum*      ecxt_per_tuple_memory; // 每元组内存
   101|    MemoryContext ecxt_per_tuple_memory_context;
   102|    
   103|    // 参数
   104|    ParamListInfo ecxt_param_list_info; // 参数列表
   105|    
   106|    // 其他
   107|    Datum       ecxt_aggvalues;     // 聚合值
   108|    bool*       ecxt_aggnulls;      // 聚合NULL标记
   109|    
   110|} ExprContext;
   111|```
   112|
   113|**执行流程**:
   114|
   115|```cpp
   116|// src/gausskernel/executor/execMain.cpp
   117|// standard_ExecutorStart函数
   118|
   119|void standard_ExecutorStart(QueryDesc* queryDesc, int eflags)
   120|{
   121|    EState* estate = NULL;
   122|    Plan* plan = queryDesc->plannedstmt->planTree;
   123|    
   124|    // 1. 创建EState
   125|    estate = CreateExecutorState();
   126|    estate->es_plannedstmt = queryDesc->plannedstmt;
   127|    estate->es_queryDesc = queryDesc;
   128|    
   129|    // 2. 初始化表达式上下文
   130|    estate->es_expr_ctx = CreateExprContext(estate);
   131|    
   132|    // 3. 初始化计划节点
   133|    ExecInitNode(plan, estate, eflags);
   134|    
   135|    // 4. 初始化结果元组槽
   136|    estate->es_result_tuple_slot = MakeTupleTableSlot();
   137|    set_slot_tts_tupleDescriptor(estate->es_result_tuple_slot, queryDesc->tupDesc);
   138|    
   139|    queryDesc->estate = estate;
   140|}
   141|
   142|// src/gausskernel/executor/execMain.cpp
   143|// standard_ExecutorRun函数
   144|
   145|void standard_ExecutorRun(QueryDesc* queryDesc, ScanDirection direction, uint64 count)
   146|{
   147|    EState* estate = queryDesc->estate;
   148|    Plan* plan = queryDesc->plannedstmt->planTree;
   149|    
   150|    // 设置执行方向
   151|    estate->es_direction = direction;
   152|    
   153|    // 执行计划节点
   154|    ExecutePlan(estate, plan, direction, count);
   155|}
   156|
   157|// src/gausskernel/executor/execMain.cpp
   158|// ExecutePlan函数
   159|
   160|void ExecutePlan(EState* estate, Plan* plan, ScanDirection direction, uint64 count)
   161|{
   162|    TupleTableSlot* slot = NULL;
   163|    uint64 current_count = 0;
   164|    
   165|    // 循环执行计划节点
   166|    for (;;) {
   167|        // 1. 重置每元组内存
   168|        ResetExprContext(estate->es_expr_ctx);
   169|        
   170|        // 2. 执行计划节点,获取一个元组
   171|        slot = ExecProcNode(plan);
   172|        
   173|        // 3. 检查是否结束
   174|        if (TupIsNull(slot)) {
   175|            break;
   176|        }
   177|        
   178|        // 4. 处理元组
   179|        if (estate->es_result_relation != NULL) {
   180|            // DML操作 (INSERT/UPDATE/DELETE)
   181|            Insert/Delete/Update...
   182|        } else {
   183|            // SELECT操作
   184|            SendResultToClient(slot);
   185|        }
   186|        
   187|        // 5. 检查计数限制
   188|        current_count++;
   189|        if (count != 0 && current_count >= count) {
   190|            break;
   191|        }
   192|    }
   193|}
   194|
   195|// src/gausskernel/executor/execMain.cpp
   196|// standard_ExecutorEnd函数
   197|
   198|void standard_ExecutorEnd(QueryDesc* queryDesc)
   199|{
   200|    EState* estate = queryDesc->estate;
   201|    Plan* plan = queryDesc->plannedstmt->planTree;
   202|    
   203|    // 1. 释放计划节点
   env-T|    ExecEndNode(plan);
   205|    
   206|    // 2. 释放表达式上下文
   207|    FreeExprContext(estate->es_expr_ctx);
   208|    
   209|    // 3. 释放EState
   210|    FreeExecutorState(estate);
   211|    
   212|    queryDesc->estate = NULL;
   213|}
   214|```
   215|
   216|### 43.2 执行节点初始化
   217|
   218|**ExecInitNode函数**:
   219|
   220|```cpp
   221|// src/gausskernel/executor/execProcnode.cpp
   222|// ExecInitNode函数
   223|
   224|PlanNode* ExecInitNode(Plan* node, EState* estate, int eflags)
   225|{
   226|    PlanNode* result = NULL;
   227|    
   228|    // 根据节点类型初始化
   229|    switch (nodeTag(node)) {
   230|        case T_SeqScan:
   231|            result = (PlanNode*) ExecInitSeqScan((SeqScan*) node, estate, eflags);
   232|            break;
   233|        
   234|        case T_IndexScan:
   235|            result = (PlanNode*) ExecInitIndexScan((IndexScan*) node, estate, eflags);
   236|            break;
   237|        
   238|        case T_BitmapScan:
   239|            result = (PlanNode*) ExecInitBitmapScan((BitmapScan*) node, estate, eflags);
   240|            break;
   241|        
   242|        case T_TidScan:
   243|            result = (PlanNode*) ExecInitTidScan((TidScan*) node, estate, eflags);
   244|            break;
   245|        
   246|        case T_SubqueryScan:
   247|            result = (PlanNode*) ExecInitSubqueryScan((SubqueryScan*) node, estate, eflags);
   248|            break;
   249|        
   250|        case T_FunctionScan:
   251|            result = (PlanNode*) ExecInitFunctionScan((FunctionScan*) node, estate, eflags);
   252|            break;
   253|        
   254|        case T_ValuesScan:
   255|            result = (PlanNode*) ExecInitValuesScan((ValuesScan*) node, estate, eflags);
   256|            break;
   257|        
   258|        case T_CteScan:
   259|            result = (PlanNode*) ExecInitCteScan((CteScan*) node, estate, eflags);
   260|            break;
   261|        
   262|        case T_WorkTableScan:
   263|            result = (PlanNode*) ExecInitWorkTableScan((WorkTableScan*) node, estate, eflags);
   264|            break;
   265|        
   266|        case T_NestLoop:
   267|            result = (PlanNode*) ExecInitNestLoop((NestLoop*) node, estate, eflags);
   268|            break;
   269|        
   270|        case T_MergeJoin:
   271|            result = (PlanNode*) ExecInitMergeJoin((MergeJoin*) node, estate, eflags);
   272|            break;
   273|        
   274|        case T_HashJoin:
   275|            result = (PlanNode*) ExecInitHashJoin((HashJoin*) node, estate, eflags);
   276|            break;
   277|        
   278|        case T_Material:
   279|            result = (PlanNode*) ExecInitMaterial((Material*) node, estate, eflags);
   280|            break;
   281|        
   282|        case T_Sort:
   283|            result = (PlanNode*) ExecInitSort((Sort*) node, estate, eflags);
   284|            break;
   285|        
   286|        case T_Group:
   287|            result = (PlanNode*) ExecInitGroup((Group*) node, estate, eflags);
   288|            break;
   289|        
   290|        case T_Agg:
   291|            result = (PlanNode*) ExecInitAgg((Agg*) node, estate, eflags);
   292|            break;
   293|        
   294|        case T_Window:
   295|            result = (PlanNode*) ExecInitWindow((Window*) node, estate, eflags);
   296|            break;
   297|        
   298|        case T_Unique:
   299|            result = (PlanNode*) ExecInitUnique((Unique*) node, estate, eflags);
   300|            break;
   301|        
   302|        case T_Hash:
   303|            result = (PlanNode*) ExecInitHash((Hash*) node, estate, eflags);
   304|            break;
   305|        
   306|        case T_SetOp:
   307|            result = (PlanNode*) ExecInitSetOp((SetOp*) node, estate, eflags);
   308|            break;
   309|        
   310|        case T_Limit:
   311|            result = (PlanNode*) ExecInitLimit((Limit*) node, estate, eflags);
   312|            break;
   313|        
   314|        case T_LockRows:
   315|            result = (PlanNode*) ExecInitLockRows((LockRows*) node, estate, eflags);
   316|            break;
   317|        
   318|        case T_Gather:
   319|        case T_GatherMerge:
   320|            result = (PlanNode*) ExecInitGather((Gather*) node, estate, eflags);
   321|            break;
   322|        
   323|        default:
   324|            ereport(ERROR,
   325|                    (errmsg("unrecognized node type: %d", nodeTag(node))));
   326|            break;
   327|    }
   328|    
   329|    return result;
   330|}
   331|```
   332|
   333|---
   334|
   335|## 44. 执行节点详解
   336|
   337|### 44.1 扫描节点
   338|
   339|**SeqScan节点**:
   340|
   341|```cpp
   342|// src/gausskernel/executor/nodeSeqscan.cpp
   343|// SeqScan节点数据结构
   344|
   345|typedef struct SeqScanState {
   346|    ScanState   ss;               // 基础ScanState
   347|    Relation    ss_currentRelation; // 当前关系
   348|    TableScanDesc ss_currentScanDesc; // 扫描描述符
   349|    HeapTuple   ss_ScanTupleSlot; // 扫描元组槽
   350|} SeqScanState;
   351|
   352|// ExecInitSeqScan函数
   353|SeqScanState* ExecInitSeqScan(SeqScan* node, EState* estate, int eflags)
   354|{
   355|    SeqScanState* scanstate = makeNode(SeqScanState);
   356|    
   357|    // 1. 初始化基础ScanState
   358|    ExecInitScanTupleSlot(estate, &scanstate->ss);
   359|    ExecInitResultTupleSlot(estate, &scanstate->ss.ps);
   360|    
   361|    // 2. 打开表
   362|    Relation relation = ExecOpenScanRelation(estate, node->scanrelid, eflags);
   363|    scanstate->ss_currentRelation = relation;
   364|    
   365|    // 3. 初始化扫描描述符
   366|    scanstate->ss_currentScanDesc = 
   367|        table_scan_begin(relation, 
   368|                        GetActiveSnapshot(), 
   369|                        0,  // 无并行
   370|                        NULL);
   371|    
   372|    // 4. 初始化表达式
   373|    ExecInitExpr(node->plan.qual, (PlanState*) scanstate);
   374|    
   375|    return scanstate;
   376|}
   377|
   378|// ExecSeqScan函数
   379|TupleTableSlot* ExecSeqScan(PlanState* pstate)
   380|{
   381|    SeqScanState* node = (SeqScanState*) pstate;
   382|    Relation relation = node->ss_currentRelation;
   383|    TableScanDesc scandesc = node->ss_currentScanDesc;
   384|    TupleTableSlot* slot = node->ss.ss_ScanTupleSlot;
   385|    ExprContext* econtext = node->ss.ps.ps_ExprContext;
   386|    
   387|    // 循环扫描
   388|    for (;;) {
   389|        // 1. 从表扫描一个元组
   390|        ExecScanFetch(node, direction, slot);
   391|        
   392|        // 2. 检查是否结束
   393|        if (TupIsNull(slot)) {
   394|            return NULL;  // 扫描结束
   395|        }
   396|        
   397|        // 3. 执行限定条件
   398|        econtext->ecxt_scantuple = slot;
   399|        if (ExecQual(node->ss.ps.qual, econtext, false)) {
   400|            // 条件满足,返回元组
   401|            return slot;
   402|        }
   403|        // 条件不满足,继续扫描下一个元组
   404|    }
   405|}
   406|
   407|// ExecEndSeqScan函数
   408|void ExecEndSeqScan(SeqScanState* node)
   409|{
   410|    // 1. 释放扫描描述符
   411|    table_scan_end(node->ss_currentScanDesc);
   412|    
   413|    // 2. 关闭表
   414|    ExecCloseScanRelation(node->ss_currentRelation);
   415|}
   416|```
   417|
   418|**IndexScan节点**:
   419|
   420|```cpp
   421|// src/gausskernel/executor/nodeIndexscan.cpp
   422|// IndexScanState节点数据结构
   423|
   424|typedef struct IndexScanState {
   425|    ScanState   ss;               // 基础ScanState
   426|    List*       indexqual;        // 索引条件
   427|    IndexRuntimeKeyInfo* index_runtime_keys;  // 运行时键
   428|    
   429|    // 索引扫描状态
   430|    IndexScanDesc iss_ScanDesc;   // 索引扫描描述符
   431|    bool        iss_Reversed;     // 是否反向扫描
   432|    bool        iss_KeysOK;       // 键是否OK
   433|    
   434|    // 多索引扫描
   435|    int         iss_NumIndices;   // 索引数量
   436|    IndexScanDesc* iss_Scans;     // 索引扫描描述符数组
   437|    ScanKey     iss_ScanKeys;     // 扫描键
   438|    
   439|} IndexScanState;
   440|
   441|// ExecInitIndexScan函数
   442|IndexScanState* ExecInitIndexScan(IndexScan* node, EState* estate, int eflags)
   443|{
   444|    IndexScanState* indexstate = makeNode(IndexScanState);
   445|    
   446|    // 1. 初始化基础ScanState
   447|    ExecInitScanTupleSlot(estate, &indexstate->ss);
   448|    ExecInitResultTupleSlot(estate, &indexstate->ss.ps);
   449|    
   450|    // 2. 打开表
   451|    Relation relation = ExecOpenScanRelation(estate, node->scan.scanrelid, eflags);
   452|    indexstate->ss.ss_currentRelation = relation;
   453|    
   454|    // 3. 打开索引
   455|    List* indexqual = node->indexqual;
   456|    List* indexid = node->indexid;
   457|    
   458|    // 4. 初始化索引扫描描述符
   459|    indexstate->iss_ScanDesc = 
   460|        index_beginscan(relation, 
   461|                       indexid, 
   462|                       estate->es_snapshot, 
   463|                       0,    // 无并行
   464|                       NULL);
   465|    
   466|    // 5. 初始化表达式
   467|    ExecInitExpr(node->indexqual, (PlanState*) indexstate);
   468|    ExecInitExpr(node->scan.plan.qual, (PlanState*) indexstate);
   469|    
   470|    return indexstate;
   471|}
   472|
   473|// ExecIndexScan函数
   474|TupleTableSlot* ExecIndexScan(PlanState* pstate)
   475|{
   476|    IndexScanState* node = (IndexScanState*) pstate;
   477|    EState* estate = node->ss.ps.state;
   478|    ExprContext* econtext = node->ss.ps.ps_ExprContext;
   479|    TupleTableSlot* slot = node->ss.ss_ScanTupleSlot;
   480|    
   481|    // 循环扫描
   482|    for (;;) {
   483|        // 1. 从索引扫描一个元组
   484|        HeapTuple tuple = index_getnext(node->iss_ScanDesc, 
   485|                                       ForwardScanDirection);
   486|        
   487|        // 2. 检查是否结束
   488|        if (tuple == NULL) {
   489|            return NULL;  // 扫描结束
   490|        }
   491|        
   492|        // 3. 存储元组到槽
   493|        ExecStoreTuple(tuple, slot, InvalidBuffer, false);
   494|        
   495|        // 4. 执行限定条件
   496|        econtext->ecxt_scantuple = slot;
   497|        if (ExecQual(node->ss.ps.qual, econtext, false)) {
   498|            // 条件满足,返回元组
   499|            return slot;
   500|        }
   501|