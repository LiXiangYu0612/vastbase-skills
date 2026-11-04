     1|# VastBase/openGauss 并行执行与执行器调优深度分析
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 继续前序文档...  
     5|> 聚焦: 并行执行机制、执行器调优、性能分析
     6|
     7|---
     8|
     9|## 46. 并行执行机制
    10|
    11|### 46.1 并行执行架构
    12|
    13|**并行执行框架**:
    14|
    15|```cpp
    16|// src/include/executor/execParallel.h
    17|// ParallelExecutorState: 并行执行状态
    18|
    19|typedef struct ParallelExecutorState {
    20|    // 共享内存
    21|    dsm_segment* dsm_segment;    // 动态共享内存段
    22|    shm_mq_handle* tts_mq;       // 消息队列句柄
    23|    
    24|    // 执行器状态
    25|    EState*   estate;            // 执行器状态
    26|    Plan*     plan_tree;         // 计划树
    27|    
    28|    // Worker信息
    29|    int       nworkers;          // Worker数量
    30|    int       nworkers_launched; // 已启动Worker数量
    31|    ParallelWorkerInfo* workers; // Worker信息数组
    32|    
    33|    // 共享状态
    34|    SharedExecutorState* shared; // 共享执行器状态
    35|    
    36|} ParallelExecutorState;
    37|
    38|// ParallelWorkerInfo: Worker信息
    39|typedef struct ParallelWorkerInfo {
    40|    int     worker_num;         // Worker编号
    41|    pid_t   pid;                // 进程ID
    42|    bool    initialized;        // 是否已初始化
    43|} ParallelWorkerInfo;
    44|
    45|// SharedExecutorState: 共享执行器状态
    46|typedef struct SharedExecutorState {
    47|    // 原子操作
    48|    pg_atomic_uint32 next_partition;  // 下一个分区
    49|    pg_atomic_uint32 next_tuple;      // 下一个元组
    50|    
    51|    // 统计信息
    52|    uint64  tuples_processed;   // 已处理元组数
    53|    uint64  tuples_emitted;     // 已发送元组数
    54|    
    55|    // 锁
    56|    slock_t mutex;              // 互斥锁
    57|    
    58|} SharedExecutorState;
    59|```
    60|
    61|**并行执行流程**:
    62|
    63|```cpp
    64|// src/gausskernel/executor/execParallel.cpp
    65|// ExecInitParallelPlan函数
    66|
    67|void ExecInitParallelPlan(Plan* plan, EState* estate, int nworkers)
    68|{
    69|    ParallelExecutorState* pstate = (ParallelExecutorState*) palloc0(sizeof(ParallelExecutorState));
    70|    
    71|    // 1. 创建动态共享内存
    72|    pstate->dsm_segment = dsm_create(sizeof(SharedExecutorState));
    73|    
    74|    // 2. 初始化共享状态
    75|    SharedExecutorState* shared = (SharedExecutorState*) dsm_segment_address(pstate->dsm_segment);
    76|    pg_atomic_init_u32(&shared->next_tuple, 0);
    77|    pg_atomic_init_u32(&shared->next_partition, 0);
    78|    shared->tuples_processed = 0;
    79|    shared->tuples_emitted = 0;
    80|    pstate->shared = shared;
    81|    
    82|    // 3. 初始化Worker信息
    83|    pstate->nworkers = nworkers;
    84|    pstate->nworkers_launched = 0;
    85|    pstate->workers = (ParallelWorkerInfo*) palloc0(nworkers * sizeof(ParallelWorkerInfo));
    86|    
    87|    // 4. 创建消息队列 (用于Worker → Leader通信)
    88|    pstate->tts_mq = shm_mq_create(...);
    89|    
    90|    // 5. 启动Worker进程
    91|    LaunchParallelWorkers(pstate);
    92|    
    93|    estate->parallel_executor = pstate;
    94|}
    95|
    96|// LaunchParallelWorkers函数
    97|void LaunchParallelWorkers(ParallelExecutorState* pstate)
    98|{
    99|    int nworkers = pstate->nworkers;
   100|    
   101|    for (int i = 0; i < nworkers; i++) {
   102|        // 创建Worker进程
   103|        pid_t pid = fork_worker();
   104|        
   105|        if (pid == 0) {
   106|            // Worker进程
   107|            ParallelWorkerMain(pstate, i);
   108|            exit(0);
   109|        } else {
   110|            // Leader进程
   111|            pstate->workers[i].worker_num = i;
   112|            pstate->workers[i].pid = pid;
   113|            pstate->workers[i].initialized = true;
   114|            pstate->nworkers_launched++;
   115|        }
   116|    }
   117|}
   118|
   119|// ParallelWorkerMain函数: Worker主函数
   120|void ParallelWorkerMain(ParallelExecutorState* pstate, int worker_num)
   121|{
   122|    // 1. 附加到共享内存
   123|    dsm_attach(pstate->dsm_segment);
   124|    
   125|    // 2. 初始化Worker本地状态
   126|    EState* estate = CreateExecutorState();
   127|    estate->es_parallel_worker = true;
   128|    estate->es_parallel_worker_number = worker_num;
   129|    
   130|    // 3. 初始化执行计划
   131|    Plan* plan = pstate->plan_tree;
   132|    ExecInitNode(plan, estate, 0);
   133|    
   134|    // 4. 执行计划
   135|    TupleTableSlot* slot = NULL;
   136|    
   137|    while ((slot = ExecProcNode(plan)) != NULL) {
   138|        // 发送元组到Leader
   139|        SendTupleToLeader(slot, pstate->tts_mq);
   140|    }
   141|}
   142|```
   143|
   144|### 46.2 并行扫描节点
   145|
   146|**ParallelSeqScan节点**:
   147|
   148|```cpp
   149|// src/gausskernel/executor/nodeParallelseqscan.cpp
   150|// ParallelSeqScanState节点数据结构
   151|
   152|typedef struct ParallelSeqScanState {
   153|    SeqScanState   ss;          // 基础SeqScanState
   154|    
   155|    // 并行信息
   156|    int            ps_startblock;  // 起始块号
   157|    int            ps_nblocks;     // 块数量
   158|    int            ps_worker_num;  // Worker编号
   159|    int            ps_nworkers;    // Worker总数
   160|    
   161|    // 共享状态
   162|    ParallelBlockTableShared* ps_shared; // 共享块表
   163|    
   164|} ParallelSeqScanState;
   165|
   166|// ExecInitParallelSeqScan函数
   167|ParallelSeqScanState* ExecInitParallelSeqScan(SeqScan* node, EState* estate, int eflags)
   168|{
   169|    ParallelSeqScanState* psstate = makeNode(ParallelSeqScanState);
   170|    
   171|    // 1. 初始化基础SeqScanState
   172|    ExecInitSeqScan((SeqScan*) node, estate, eflags);
   173|    
   174|    // 2. 计算块范围
   175|    Relation rel = psstate->ss.ss_currentRelation;
   176|    int nblocks = RelationGetNumberOfBlocks(rel);
   177|    
   178|    int worker_num = estate->es_parallel_worker_number;
   179|    int nworkers = estate->es_parallel_nworkers;
   180|    
   181|    // 计算Worker负责的块范围
   182|    int blocks_per_worker = nblocks / nworkers;
   183|    int start_block = worker_num * blocks_per_worker;
   184|    int end_block = (worker_num == nworkers - 1) ? nblocks : start_block + blocks_per_worker;
   185|    
   186|    psstate->ps_startblock = start_block;
   187|    psstate->ps_nblocks = end_block - start_block;
   188|    psstate->ps_worker_num = worker_num;
   189|    psstate->ps_nworkers = nworkers;
   190|    
   191|    return psstate;
   192|}
   193|
   194|// ExecParallelSeqScan函数
   195|TupleTableSlot* ExecParallelSeqScan(PlanState* pstate)
   196|{
   197|    ParallelSeqScanState* node = (ParallelSeqScanState*) pstate;
   198|    Relation relation = node->ss.ss_currentRelation;
   199|    TupleTableSlot* slot = node->ss.ss_ScanTupleSlot;
   200|    
   201|    // 循环扫描分配的块
   202|    for (int block = node->ps_startblock; block < node->ps_startblock + node->ps_nblocks; block++) {
   203|        // 1. 读取块
   204|        Buffer buffer = ReadBuffer(relation, block);
   205|        
   206|        // 2. 扫描块中的元组
   207|        Page page = BufferGetPage(buffer);
   208|        OffsetNumber maxoffset = PageGetMaxOffsetNumber(page);
   209|        
   210|        for (OffsetNumber offset = 1; offset <= maxoffset; offset++) {
   211|            ItemId itemid = PageGetItemId(page, offset);
   212|            
   213|            if (!ItemIdIsNormal(itemid)) {
   214|                continue;
   215|            }
   216|            
   217|            HeapTuple tuple = (HeapTuple) PageGetItem(page, itemid);
   218|            
   219|            // 3. 存储元组到槽
   220|            ExecStoreTuple(tuple, slot, buffer, false);
   221|            
   222|            // 4. 执行限定条件
   223|            ExprContext* econtext = node->ss.ps.ps_ExprContext;
   224|            econtext->ecxt_scantuple = slot;
   225|            
   226|            if (ExecQual(node->ss.ps.qual, econtext, false)) {
   227|                // 条件满足,返回元组
   228|                return slot;
   229|            }
   230|        }
   231|        
   232|        // 5. 释放缓冲区
   233|        ReleaseBuffer(buffer);
   234|    }
   235|    
   236|    // 扫描结束
   237|    return NULL;
   238|}
   239|```
   240|
   241|**ParallelHashJoin节点**:
   242|
   243|```cpp
   244|// src/gausskernel/executor/nodeParallelhashjoin.cpp
   245|// ParallelHashJoinState节点数据结构
   246|
   247|typedef struct ParallelHashJoinState {
   248|    HashJoinState   hjstate;    // 基础HashJoinState
   249|    
   250|    // 并行哈希表
   251|    ParallelHashTable* phtable;  // 并行哈希表
   252|    
   253|    // 分区信息
   254|    int            npartitions;    // 分区数
   255|    int            my_partition;   // 当前Worker负责的分区
   256|    
   257|} ParallelHashJoinState;
   258|
   259|// ExecInitParallelHashJoin函数
   260|ParallelHashJoinState* ExecInitParallelHashJoin(HashJoin* node, EState* estate, int eflags)
   261|{
   262|    ParallelHashJoinState* phjstate = makeNode(ParallelHashJoinState);
   263|    
   264|    // 1. 初始化基础HashJoinState
   265|    ExecInitHashJoin((HashJoin*) node, estate, eflags);
   266|    
   267|    // 2. 创建并行哈希表
   268|    int npartitions = estate->es_parallel_nworkers;
   269|    ParallelHashTable* phtable = CreateParallelHashTable(npartitions);
   270|    
   271|    phjstate->phtable = phtable;
   272|    phjstate->npartitions = npartitions;
   273|    
   274|    return phjstate;
   275|}
   276|
   277|// ExecParallelHashJoin函数
   278|TupleTableSlot* ExecParallelHashJoin(PlanState* pstate)
   279|{
   280|    ParallelHashJoinState* node = (ParallelHashJoinState*) pstate;
   281|    ExprContext* econtext = node->hjstate.js.ps.ps_ExprContext;
   282|    
   283|    // 1. 构建并行哈希表 (如果还未构建)
   284|    if (node->phtable == NULL) {
   285|        BuildParallelHashTable(node);
   286|    }
   287|    
   288|    // 2. 循环执行哈希连接
   289|    for (;;) {
   290|        // 获取外表元组
   291|        TupleTableSlot* outerTupleSlot = ExecProcNode(node->hjstate.js.ps.lefttree);
   292|        
   293|        if (TupIsNull(outerTupleSlot)) {
   294|            return NULL;  // 外表扫描结束
   295|        }
   296|        
   297|        econtext->ecxt_outertuple = outerTupleSlot;
   298|        
   299|        // 3. 计算哈希值
   300|        uint32 hashvalue = ExecHashGetHashValue(node, outerTupleSlot);
   301|        
   302|        // 4. 在当前分区中查找
   303|        int partition = hashvalue % node->npartitions;
   304|        
   305|        // 5. 检查是否是当前Worker负责的分区
   306|        int my_partition = node->my_partition;
   307|        
   308|        if (partition != my_partition) {
   309|            continue;  // 不是当前Worker负责的分区,跳过
   310|        }
   311|        
   312|        // 6. 在哈希表中查找
   313|        HashJoinTuple hashTuple = ExecScanHashTablePartition(node->phtable, partition, hashvalue);
   314|        
   315|        while (hashTuple != NULL) {
   316|            // 设置内表元组到表达式上下文
   317|            TupleTableSlot* innerTupleSlot = ExecHashTupleSlot(hashTuple);
   318|            econtext->ecxt_innertuple = innerTupleSlot;
   319|            
   320|            // 执行连接条件
   321|            if (ExecQual(node->hjstate.js.joinqual, econtext, false)) {
   322|                if (ExecQual(node->hjstate.js.ps.qual, econtext, false)) {
   323|                    return ExecProject(node->hjstate.js.ps.ps_ResultTupleSlot);
   324|                }
   325|            }
   326|            
   327|            hashTuple = hashTuple->next;
   328|        }
   329|    }
   330|}
   331|
   332|// BuildParallelHashTable函数
   333|void BuildParallelHashTable(ParallelHashJoinState* node)
   334|{
   335|    // 1. 扫描内表
   336|    PlanState* innerNode = node->hjstate.js.ps.righttree;
   337|    TupleTableSlot* innerTupleSlot = NULL;
   338|    
   339|    // 2. 为每个分区创建哈希表
   340|    for (int i = 0; i < node->npartitions; i++) {
   341|        CreateHashPartition(node->phtable, i);
   342|    }
   343|    
   344|    // 3. 填充哈希表
   345|    while ((innerTupleSlot = ExecProcNode(innerNode)) != NULL) {
   346|        // 计算哈希值
   347|        uint32 hashvalue = ExecHashGetHashValue(node, innerTupleSlot);
   348|        
   349|        // 确定分区
   350|        int partition = hashvalue % node->npartitions;
   351|        
   352|        // 插入到对应分区
   353|        ExecHashBuildTuplePartition(node->phtable, partition, innerTupleSlot, hashvalue);
   354|    }
   355|}
   356|```
   357|
   358|### 46.3 Gather节点
   359|
   360|**Gather节点数据结构**:
   361|
   362|```cpp
   363|// src/include/nodes/plannodes.h
   364|// Gather节点
   365|
   366|typedef struct Gather {
   367|    Plan        plan;
   368|    int         num_workers;      // Worker数量
   369|    bool        single_copy;      // 是否单拷贝
   370|} Gather;
   371|
   372|// src/gausskernel/executor/nodeGather.cpp
   373|// GatherState节点数据结构
   374|
   375|typedef struct GatherState {
   376|    PlanState   ps;               // 基础PlanState
   377|    int         num_workers;      // Worker数量
   378|    bool        single_copy;      // 是否单拷贝
   379|    
   380|    // Worker管理
   381|    ParallelExecutorState* pe;    // 并行执行状态
   382|    bool        need_to_scan_locally; // 是否需要本地扫描
   383|    
   384|    // 元组收集
   385|    TupleTableSlot* result_slot;  // 结果槽
   386|    
   387|} GatherState;
   388|
   389|// ExecInitGather函数
   390|GatherState* ExecInitGather(Gather* node, EState* estate, int eflags)
   391|{
   392|    GatherState* gstate = makeNode(GatherState);
   393|    
   394|    // 1. 初始化基础PlanState
   395|    ExecInitResultTupleSlot(estate, &gstate->ps);
   396|    
   397|    // 2. 初始化子计划
   398|    gstate->ps.lefttree = ExecInitNode(node->plan.lefttree, estate, eflags);
   399|    
   400|    // 3. 设置Worker数量
   401|    gstate->num_workers = node->num_workers;
   402|    
   403|    // 4. 检查是否可以并行
   404|    if (gstate->num_workers > 0) {
   405|        // 初始化并行执行
   406|        gstate->pe = ExecInitParallelPlan(node->plan.lefttree, estate, gstate->num_workers);
   407|        gstate->need_to_scan_locally = false;
   408|    } else {
   409|        // 单进程执行
   410|        gstate->need_to_scan_locally = true;
   411|    }
   412|    
   413|    // 5. 创建结果槽
   414|    gstate->result_slot = MakeTupleTableSlot();
   415|    
   416|    return gstate;
   417|}
   418|
   419|// ExecGather函数
   420|TupleTableSlot* ExecGather(PlanState* pstate)
   421|{
   422|    GatherState* node = (GatherState*) pstate;
   423|    
   424|    if (node->need_to_scan_locally) {
   425|        // 单进程执行
   426|        return ExecProcNode(node->ps.lefttree);
   427|    }
   428|    
   429|    // 并行执行
   430|    ParallelExecutorState* pe = node->pe;
   431|    
   432|    // 1. 从Worker收集元组
   433|    for (int i = 0; i < pe->nworkers_launched; i++) {
   434|        // 从消息队列读取元组
   435|        TupleTableSlot* slot = ReceiveTupleFromWorker(pe, i);
   436|        
   437|        if (!TupIsNull(slot)) {
   438|            // 返回元组
   439|            return slot;
   440|        }
   441|    }
   442|    
   443|    // 2. 所有Worker都结束
   444|    return NULL;
   445|}
   446|```
   447|
   448|---
   449|
   450|## 47. 执行器性能调优
   451|
   452|### 47.1 执行器统计信息
   453|
   454|**Instrumentation机制**:
   455|
   456|```cpp
   457|// src/include/nodes/execnodes.h
   458|// Instrumentation: 执行统计信息
   459|
   460|typedef struct Instrumentation {
   461|    // 时间统计
   462|    instr_time  starttime;         // 开始时间
   463|    instr_time  counter;           // 累计时间
   464|    double      totaltime;         // 总时间
   465|    
   466|    // 元组统计
   467|    uint64      ntuples;           // 元组数
   468|    uint64      ntuples2;          // 元组数2
   469|    
   470|    // 缓冲区统计
   471|    uint64      nfiltered1;        // 过滤元组数1
   472|    uint64      nfiltered2;        // 过滤元组数2
   473|    
   474|    // I/O统计
   475|    uint64      nblocks;           // 块数
   476|    uint64      nloops;            // 循环次数
   477|    
   478|    // 缓冲区
   479|    BufferAccess* bufusage;        // 缓冲区使用
   480|    
   481|    // 标志位
   482|    bool        need_timer;        // 是否需要计时
   483|    bool        need_bufusage;     // 是否需要缓冲区统计
   484|    
   485|} Instrumentation;
   486|
   487|// BufferAccess: 缓冲区访问统计
   488|typedef struct BufferAccess {
   489|    uint64  shared_blks_hit;      // 共享缓冲区命中
   490|    uint64  shared_blks_read;     // 共享缓冲区读取
   491|    uint64  shared_blks_dirtied;  // 共享缓冲区脏页
   492|    uint64  shared_blks_written;  // 共享缓冲区写回
   493|    
   494|    uint64  local_blks_hit;       // 本地缓冲区命中
   495|    uint64  local_blks_read;      // 本地缓冲区读取
   496|    uint64  local_blks_dirtied;   // 本地缓冲区脏页
   497|    uint64  local_blks_written;   // 本地缓冲区写回
   498|    
   499|    uint64  temp_blks_read;       // 临时缓冲区读取
   500|    uint64  temp_blks_written;    // 临时缓冲区写入
   501|

---
> ⚓ **截断注记（2026-11-05 审计）**：本篇在历史迁移时截断于 500 行，尾部
> 小节缺失（原全量件已不可寻，待重写补齐——挂账 ROADMAP）；行号式清单为
> 源码摘录，无独立 file:line 锚。
