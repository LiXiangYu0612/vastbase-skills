# VastBase/openGauss 并行执行与执行器调优深度分析

> 源码版本: openGauss-server-v6.0.0  
> 聚焦: 并行执行机制、执行器调优、性能分析

---

## 46. 并行执行机制

### 46.1 并行执行架构

**并行执行框架**:

```cpp
// src/include/executor/execParallel.h
// ParallelExecutorState: 并行执行状态

typedef struct ParallelExecutorState {
    // 共享内存
    dsm_segment* dsm_segment;    // 动态共享内存段
    shm_mq_handle* tts_mq;       // 消息队列句柄
    
    // 执行器状态
    EState*   estate;            // 执行器状态
    Plan*     plan_tree;         // 计划树
    
    // Worker信息
    int       nworkers;          // Worker数量
    int       nworkers_launched; // 已启动Worker数量
    ParallelWorkerInfo* workers; // Worker信息数组
    
    // 共享状态
    SharedExecutorState* shared; // 共享执行器状态
    
} ParallelExecutorState;

// ParallelWorkerInfo: Worker信息
typedef struct ParallelWorkerInfo {
    int     worker_num;         // Worker编号
    pid_t   pid;                // 进程ID
    bool    initialized;        // 是否已初始化
} ParallelWorkerInfo;

// SharedExecutorState: 共享执行器状态
typedef struct SharedExecutorState {
    // 原子操作
    pg_atomic_uint32 next_partition;  // 下一个分区
    pg_atomic_uint32 next_tuple;      // 下一个元组
    
    // 统计信息
    uint64  tuples_processed;   // 已处理元组数
    uint64  tuples_emitted;     // 已发送元组数
    
    // 锁
    slock_t mutex;              // 互斥锁
    
} SharedExecutorState;
```

**并行执行流程**:

```cpp
// src/gausskernel/executor/execParallel.cpp
// ExecInitParallelPlan函数

void ExecInitParallelPlan(Plan* plan, EState* estate, int nworkers)
{
    ParallelExecutorState* pstate = (ParallelExecutorState*) palloc0(sizeof(ParallelExecutorState));
    
    // 1. 创建动态共享内存
    pstate->dsm_segment = dsm_create(sizeof(SharedExecutorState));
    
    // 2. 初始化共享状态
    SharedExecutorState* shared = (SharedExecutorState*) dsm_segment_address(pstate->dsm_segment);
    pg_atomic_init_u32(&shared->next_tuple, 0);
    pg_atomic_init_u32(&shared->next_partition, 0);
    shared->tuples_processed = 0;
    shared->tuples_emitted = 0;
    pstate->shared = shared;
    
    // 3. 初始化Worker信息
    pstate->nworkers = nworkers;
    pstate->nworkers_launched = 0;
    pstate->workers = (ParallelWorkerInfo*) palloc0(nworkers * sizeof(ParallelWorkerInfo));
    
    // 4. 创建消息队列 (用于Worker → Leader通信)
    pstate->tts_mq = shm_mq_create(...);
    
    // 5. 启动Worker进程
    LaunchParallelWorkers(pstate);
    
    estate->parallel_executor = pstate;
}

// LaunchParallelWorkers函数
void LaunchParallelWorkers(ParallelExecutorState* pstate)
{
    int nworkers = pstate->nworkers;
    
    for (int i = 0; i < nworkers; i++) {
        // 创建Worker进程
        pid_t pid = fork_worker();
        
        if (pid == 0) {
            // Worker进程
            ParallelWorkerMain(pstate, i);
            exit(0);
        } else {
            // Leader进程
            pstate->workers[i].worker_num = i;
            pstate->workers[i].pid = pid;
            pstate->workers[i].initialized = true;
            pstate->nworkers_launched++;
        }
    }
}

// ParallelWorkerMain函数: Worker主函数
void ParallelWorkerMain(ParallelExecutorState* pstate, int worker_num)
{
    // 1. 附加到共享内存
    dsm_attach(pstate->dsm_segment);
    
    // 2. 初始化Worker本地状态
    EState* estate = CreateExecutorState();
    estate->es_parallel_worker = true;
    estate->es_parallel_worker_number = worker_num;
    
    // 3. 初始化执行计划
    Plan* plan = pstate->plan_tree;
    ExecInitNode(plan, estate, 0);
    
    // 4. 执行计划
    TupleTableSlot* slot = NULL;
    
    while ((slot = ExecProcNode(plan)) != NULL) {
        // 发送元组到Leader
        SendTupleToLeader(slot, pstate->tts_mq);
    }
}
```

### 46.2 并行扫描节点

**ParallelSeqScan节点**:

```cpp
// src/gausskernel/executor/nodeParallelseqscan.cpp
// ParallelSeqScanState节点数据结构

typedef struct ParallelSeqScanState {
    SeqScanState   ss;          // 基础SeqScanState
    
    // 并行信息
    int            ps_startblock;  // 起始块号
    int            ps_nblocks;     // 块数量
    int            ps_worker_num;  // Worker编号
    int            ps_nworkers;    // Worker总数
    
    // 共享状态
    ParallelBlockTableShared* ps_shared; // 共享块表
    
} ParallelSeqScanState;

// ExecInitParallelSeqScan函数
ParallelSeqScanState* ExecInitParallelSeqScan(SeqScan* node, EState* estate, int eflags)
{
    ParallelSeqScanState* psstate = makeNode(ParallelSeqScanState);
    
    // 1. 初始化基础SeqScanState
    ExecInitSeqScan((SeqScan*) node, estate, eflags);
    
    // 2. 计算块范围
    Relation rel = psstate->ss.ss_currentRelation;
    int nblocks = RelationGetNumberOfBlocks(rel);
    
    int worker_num = estate->es_parallel_worker_number;
    int nworkers = estate->es_parallel_nworkers;
    
    // 计算Worker负责的块范围
    int blocks_per_worker = nblocks / nworkers;
    int start_block = worker_num * blocks_per_worker;
    int end_block = (worker_num == nworkers - 1) ? nblocks : start_block + blocks_per_worker;
    
    psstate->ps_startblock = start_block;
    psstate->ps_nblocks = end_block - start_block;
    psstate->ps_worker_num = worker_num;
    psstate->ps_nworkers = nworkers;
    
    return psstate;
}

// ExecParallelSeqScan函数
TupleTableSlot* ExecParallelSeqScan(PlanState* pstate)
{
    ParallelSeqScanState* node = (ParallelSeqScanState*) pstate;
    Relation relation = node->ss.ss_currentRelation;
    TupleTableSlot* slot = node->ss.ss_ScanTupleSlot;
    
    // 循环扫描分配的块
    for (int block = node->ps_startblock; block < node->ps_startblock + node->ps_nblocks; block++) {
        // 1. 读取块
        Buffer buffer = ReadBuffer(relation, block);
        
        // 2. 扫描块中的元组
        Page page = BufferGetPage(buffer);
        OffsetNumber maxoffset = PageGetMaxOffsetNumber(page);
        
        for (OffsetNumber offset = 1; offset <= maxoffset; offset++) {
            ItemId itemid = PageGetItemId(page, offset);
            
            if (!ItemIdIsNormal(itemid)) {
                continue;
            }
            
            HeapTuple tuple = (HeapTuple) PageGetItem(page, itemid);
            
            // 3. 存储元组到槽
            ExecStoreTuple(tuple, slot, buffer, false);
            
            // 4. 执行限定条件
            ExprContext* econtext = node->ss.ps.ps_ExprContext;
            econtext->ecxt_scantuple = slot;
            
            if (ExecQual(node->ss.ps.qual, econtext, false)) {
                // 条件满足,返回元组
                return slot;
            }
        }
        
        // 5. 释放缓冲区
        ReleaseBuffer(buffer);
    }
    
    // 扫描结束
    return NULL;
}
```

**ParallelHashJoin节点**:

```cpp
// src/gausskernel/executor/nodeParallelhashjoin.cpp
// ParallelHashJoinState节点数据结构

typedef struct ParallelHashJoinState {
    HashJoinState   hjstate;    // 基础HashJoinState
    
    // 并行哈希表
    ParallelHashTable* phtable;  // 并行哈希表
    
    // 分区信息
    int            npartitions;    // 分区数
    int            my_partition;   // 当前Worker负责的分区
    
} ParallelHashJoinState;

// ExecInitParallelHashJoin函数
ParallelHashJoinState* ExecInitParallelHashJoin(HashJoin* node, EState* estate, int eflags)
{
    ParallelHashJoinState* phjstate = makeNode(ParallelHashJoinState);
    
    // 1. 初始化基础HashJoinState
    ExecInitHashJoin((HashJoin*) node, estate, eflags);
    
    // 2. 创建并行哈希表
    int npartitions = estate->es_parallel_nworkers;
    ParallelHashTable* phtable = CreateParallelHashTable(npartitions);
    
    phjstate->phtable = phtable;
    phjstate->npartitions = npartitions;
    
    return phjstate;
}

// ExecParallelHashJoin函数
TupleTableSlot* ExecParallelHashJoin(PlanState* pstate)
{
    ParallelHashJoinState* node = (ParallelHashJoinState*) pstate;
    ExprContext* econtext = node->hjstate.js.ps.ps_ExprContext;
    
    // 1. 构建并行哈希表 (如果还未构建)
    if (node->phtable == NULL) {
        BuildParallelHashTable(node);
    }
    
    // 2. 循环执行哈希连接
    for (;;) {
        // 获取外表元组
        TupleTableSlot* outerTupleSlot = ExecProcNode(node->hjstate.js.ps.lefttree);
        
        if (TupIsNull(outerTupleSlot)) {
            return NULL;  // 外表扫描结束
        }
        
        econtext->ecxt_outertuple = outerTupleSlot;
        
        // 3. 计算哈希值
        uint32 hashvalue = ExecHashGetHashValue(node, outerTupleSlot);
        
        // 4. 在当前分区中查找
        int partition = hashvalue % node->npartitions;
        
        // 5. 检查是否是当前Worker负责的分区
        int my_partition = node->my_partition;
        
        if (partition != my_partition) {
            continue;  // 不是当前Worker负责的分区,跳过
        }
        
        // 6. 在哈希表中查找
        HashJoinTuple hashTuple = ExecScanHashTablePartition(node->phtable, partition, hashvalue);
        
        while (hashTuple != NULL) {
            // 设置内表元组到表达式上下文
            TupleTableSlot* innerTupleSlot = ExecHashTupleSlot(hashTuple);
            econtext->ecxt_innertuple = innerTupleSlot;
            
            // 执行连接条件
            if (ExecQual(node->hjstate.js.joinqual, econtext, false)) {
                if (ExecQual(node->hjstate.js.ps.qual, econtext, false)) {
                    return ExecProject(node->hjstate.js.ps.ps_ResultTupleSlot);
                }
            }
            
            hashTuple = hashTuple->next;
        }
    }
}

// BuildParallelHashTable函数
void BuildParallelHashTable(ParallelHashJoinState* node)
{
    // 1. 扫描内表
    PlanState* innerNode = node->hjstate.js.ps.righttree;
    TupleTableSlot* innerTupleSlot = NULL;
    
    // 2. 为每个分区创建哈希表
    for (int i = 0; i < node->npartitions; i++) {
        CreateHashPartition(node->phtable, i);
    }
    
    // 3. 填充哈希表
    while ((innerTupleSlot = ExecProcNode(innerNode)) != NULL) {
        // 计算哈希值
        uint32 hashvalue = ExecHashGetHashValue(node, innerTupleSlot);
        
        // 确定分区
        int partition = hashvalue % node->npartitions;
        
        // 插入到对应分区
        ExecHashBuildTuplePartition(node->phtable, partition, innerTupleSlot, hashvalue);
    }
}
```

### 46.3 Gather节点

**Gather节点数据结构**:

```cpp
// src/include/nodes/plannodes.h
// Gather节点

typedef struct Gather {
    Plan        plan;
    int         num_workers;      // Worker数量
    bool        single_copy;      // 是否单拷贝
} Gather;

// src/gausskernel/executor/nodeGather.cpp
// GatherState节点数据结构

typedef struct GatherState {
    PlanState   ps;               // 基础PlanState
    int         num_workers;      // Worker数量
    bool        single_copy;      // 是否单拷贝
    
    // Worker管理
    ParallelExecutorState* pe;    // 并行执行状态
    bool        need_to_scan_locally; // 是否需要本地扫描
    
    // 元组收集
    TupleTableSlot* result_slot;  // 结果槽
    
} GatherState;

// ExecInitGather函数
GatherState* ExecInitGather(Gather* node, EState* estate, int eflags)
{
    GatherState* gstate = makeNode(GatherState);
    
    // 1. 初始化基础PlanState
    ExecInitResultTupleSlot(estate, &gstate->ps);
    
    // 2. 初始化子计划
    gstate->ps.lefttree = ExecInitNode(node->plan.lefttree, estate, eflags);
    
    // 3. 设置Worker数量
    gstate->num_workers = node->num_workers;
    
    // 4. 检查是否可以并行
    if (gstate->num_workers > 0) {
        // 初始化并行执行
        gstate->pe = ExecInitParallelPlan(node->plan.lefttree, estate, gstate->num_workers);
        gstate->need_to_scan_locally = false;
    } else {
        // 单进程执行
        gstate->need_to_scan_locally = true;
    }
    
    // 5. 创建结果槽
    gstate->result_slot = MakeTupleTableSlot();
    
    return gstate;
}

// ExecGather函数
TupleTableSlot* ExecGather(PlanState* pstate)
{
    GatherState* node = (GatherState*) pstate;
    
    if (node->need_to_scan_locally) {
        // 单进程执行
        return ExecProcNode(node->ps.lefttree);
    }
    
    // 并行执行
    ParallelExecutorState* pe = node->pe;
    
    // 1. 从Worker收集元组
    for (int i = 0; i < pe->nworkers_launched; i++) {
        // 从消息队列读取元组
        TupleTableSlot* slot = ReceiveTupleFromWorker(pe, i);
        
        if (!TupIsNull(slot)) {
            // 返回元组
            return slot;
        }
    }
    
    // 2. 所有Worker都结束
    return NULL;
}
```

---

## 47. 执行器性能调优

### 47.1 执行器统计信息

**Instrumentation机制**:

```cpp
// src/include/nodes/execnodes.h
// Instrumentation: 执行统计信息

typedef struct Instrumentation {
    // 时间统计
    instr_time  starttime;         // 开始时间
    instr_time  counter;           // 累计时间
    double      totaltime;         // 总时间
    
    // 元组统计
    uint64      ntuples;           // 元组数
    uint64      ntuples2;          // 元组数2
    
    // 缓冲区统计
    uint64      nfiltered1;        // 过滤元组数1
    uint64      nfiltered2;        // 过滤元组数2
    
    // I/O统计
    uint64      nblocks;           // 块数
    uint64      nloops;            // 循环次数
    
    // 缓冲区
    BufferAccess* bufusage;        // 缓冲区使用
    
    // 标志位
    bool        need_timer;        // 是否需要计时
    bool        need_bufusage;     // 是否需要缓冲区统计
    
} Instrumentation;

// BufferAccess: 缓冲区访问统计
typedef struct BufferAccess {
    uint64  shared_blks_hit;      // 共享缓冲区命中
    uint64  shared_blks_read;     // 共享缓冲区读取
    uint64  shared_blks_dirtied;  // 共享缓冲区脏页
    uint64  shared_blks_written;  // 共享缓冲区写回
    
    uint64  local_blks_hit;       // 本地缓冲区命中
    uint64  local_blks_read;      // 本地缓冲区读取
    uint64  local_blks_dirtied;   // 本地缓冲区脏页
    uint64  local_blks_written;   // 本地缓冲区写回
    
    uint64  temp_blks_read;       // 临时缓冲区读取
    uint64  temp_blks_written;    // 临时缓冲区写入
} BufferAccess;
```

**说明**: 上面 `BufferAccess` 是教学模型命名。openGauss 6.0.0 真实结构叫 `BufferUsage`,定义在 `src/include/executor/instrument.h`,字段语义一致,并额外带有块读写耗时:

```cpp
// src/include/executor/instrument.h
// BufferUsage结构 (真实源码节选)

typedef struct BufferUsage {
    long shared_blks_hit;      /* # of shared buffer hits */
    long shared_blks_read;     /* # of shared disk blocks read */
    long shared_blks_dirtied;  /* # of shared blocks dirtied */
    long shared_blks_written;  /* # of shared disk blocks written */
    long local_blks_hit;       /* # of local buffer hits */
    long local_blks_read;      /* # of local disk blocks read */
    long local_blks_dirtied;   /* # of shared blocks dirtied */
    long local_blks_written;   /* # of local disk blocks written */
    long temp_blks_read;       /* # of temp blocks read */
    long temp_blks_written;    /* # of temp blocks written */
    instr_time blk_read_time;  /* time spent reading */
    instr_time blk_write_time; /* time spent writing */
} BufferUsage;

// 同文件 InstrumentOption枚举 (真实源码):
// INSTRUMENT_NONE / INSTRUMENT_TIMER / INSTRUMENT_BUFFERS
// / INSTRUMENT_ROWS / INSTRUMENT_CPUS(单机版) / INSTRUMENT_ALL
// InstrAlloc按这些位掩码决定每个节点收集哪些统计
```

**统计如何呈现**:

```sql
-- 执行器统计通过EXPLAIN (ANALYZE, BUFFERS)呈现
-- BUFFERS选项解析: explain.cpp; "Rows Removed by Filter"
-- 等行由show_instrumentation_count输出 (explain.cpp)

EXPLAIN (ANALYZE, BUFFERS)
SELECT region_id, SUM(net_amount)
FROM fact_sales
GROUP BY region_id;

-- 执行计划:
-- HashAggregate  (actual time=1234.56..2345.67 rows=50 loops=1)
//   Group Key: region_id
//   Buffers: shared hit=45678 read=1234 dirtied=12
//   ->  Seq Scan on fact_sales  (actual time=0.05..678.90 rows=1000000 loops=1)
//         Filter: ...
//         Rows Removed by Filter: 50000
//         Buffers: shared hit=45678 read=1234
//
-- 观察:
// - loops: 该节点被驱动执行了几次(嵌套循环内层会大于1)
// - actual time × loops 才是该节点的累计耗时
// - shared hit/read: 命中shared_buffers与读盘的块数,read偏高=IO压力
// - temp read/written: 排序/哈希落盘的块数,非零说明work_mem不够
// - Rows Removed by Filter: 被过滤掉的行数,过高说明扫描方式或过滤顺序不当
```

### 47.2 SMP并行与并行度调优决策

**openGauss单机并行的真实形态**:

> 46节中的Gather/Worker进程模型是按PostgreSQL风格简化的教学模型。
> openGauss 6.0.0 单机源码树中未检出 execParallel.cpp、nodeGather.cpp、nodeParallelseqscan.cpp(已在本文源码树全量检索确认;其他版本是否存在对应实现⚓待锚),
> 真实的并行执行是SMP流(Stream)模型: 计划里插入Stream节点,由同一进程内的stream线程分工,
> 生产者/消费者经 `src/gausskernel/process/stream/` 下的机制传递数据
> (execStream.cpp的ExecInitStream/ExecStream、streamMain.cpp的StreamMain、streamProducer.cpp/streamConsumer.cpp)。

**query_dop语义**:

```sql
-- query_dop: SMP并行度总开关 (guc_sql.cpp注册)
SET query_dop = 1;    -- 默认,关闭SMP
SET query_dop = 4;    -- 静态SMP: 固定4路并行
SET query_dop = -4;   -- 动态SMP: 4为并行度上限,由优化器按代价决定
-- 取值范围: [-64, 64] (miscadmin.h MAX_QUERY_DOP=64)
```

```cpp
// src/common/backend/utils/misc/guc/guc_sql.cpp
// parse_query_dop函数 (真实源码节选)

static bool parse_query_dop(int* newval, void** extra, GucSource source)
{
    int dop_mark = *newval;
    // ...
    if (dop_mark >= MIN_QUERY_DOP && dop_mark <= MAX_QUERY_DOP) {
        // 正值: 静态SMP,启用线程代价惩罚,拒绝低收益并行
        u_sess->opt_cxt.smp_thread_cost = DEFAULT_SMP_THREAD_COST;
        u_sess->opt_cxt.parallel_debug_mode = DEFAULT_MODE;
    } else if (dop_mark >= MIN_QUERY_DOP && dop_mark <= 0) {
        // 负值: 动态SMP,|值|为并行度上限
        u_sess->opt_cxt.max_query_dop = abs(dop_mark);
        u_sess->opt_cxt.smp_thread_cost = 0;
    } else {
        ereport(ERROR, (errmsg(
            "Current degree of parallelism can only be set within [-64,64]")));
    }
    // ...
}

// src/include/optimizer/cost.h
#define DEFAULT_SMP_THREAD_COST 1000  /* The cost for add a new thread */
```

**SMP的代价与开关条件**:

```cpp
// src/gausskernel/optimizer/path/costsize.cpp
// cost_seqscan函数 (真实源码节选)

// 并行扫描: 磁盘与CPU成本按并行度均摊
run_cost += u_sess->opt_cxt.smp_thread_cost * (dop - 1);
if (u_sess->attr.attr_sql.enable_seqscan_dopcost)
    run_cost += spc_seq_page_cost * baserel->pages / dop;
else
    run_cost += spc_seq_page_cost * baserel->pages;  // 不均摊页成本

// src/gausskernel/optimizer/plan/streamplan_single.cpp
// set_default_stream函数 (真实源码节选)

void set_default_stream()
{
    // 单机: query_dop > 1 且当前查询允许SMP 且非子查询内层时,才启用流计划
    u_sess->opt_cxt.is_stream = (u_sess->opt_cxt.query_dop > 1 &&
                                 u_sess->opt_cxt.smp_enabled &&
                                 u_sess->stream_cxt.global_obj == NULL);
    u_sess->opt_cxt.is_stream_support = u_sess->opt_cxt.is_stream;
}
```

- SMP路径生成: `JoinPathGen::addJoinParallelInfo` / `JoinPathGen::setStreamParallelInfo`(streampath_single.cpp)在连接路径上附加并行流信息,流代价统一走 `compute_stream_cost`(streamplan_utils.cpp),其中接收端代价含 `(receiveDop - 1) * smp_thread_cost`。
- 动态SMP的单机实现: dynsmp_single.cpp 中 `InitDynamicSmp`、`ChooseStartQueryDop`、`OptimizePlanDop` 均为 `DISTRIBUTED_FEATURE_NOT_SUPPORTED()` 空壳,只有 `IsDynamicSmpEnabled()` 有实体 — 单机版实际以静态query_dop为主。

**并行度调优决策表**:

| 场景 | 建议并行度 | 依据 |
|------|-----------|------|
| 短查询OLTP(<1s) | 1 (关闭) | 线程启停与数据交换的固定开销超过收益 |
| 中等分析查询(1~10s, 顺序扫描为主) | 2~4 | 顺序扫描成本按dop均摊,cost_seqscan直接受益 |
| 大表聚合/哈希连接(>10s) | 4~8 | 哈希构建分摊,但work_mem按dop倍增,先评估内存 |
| 并发很高的系统整体 | 1~2 | 并发×并行度≤CPU核数,否则线程间争抢反噬吞吐 |
| 无法判断 | 负值动态SMP | 让优化器按代价自行取舍(注意单机版实际生效路径,见上) |

**SMP相关GUC速查**:

| 参数 | 默认 | 作用 | 源码锚点 |
|------|------|------|----------|
| query_dop | 1 | SMP并行度;正值静态/负值动态,[-64,64] | guc_sql.cpp注册,parse_query_dop |
| enable_seqscan_dopcost | on | 顺序扫描页成本是否按dop均摊 | guc_sql.cpp注册; costsize.cpp cost_seqscan |
| enable_stream_operator | on | 计划器是否使用流算子(分布式节点) | guc_sql.cpp注册(NODE_DISTRIBUTE) |
| smp_thread_cost | (内部值1000) | 每增加一个并行线程的代价惩罚 | cost.h DEFAULT_SMP_THREAD_COST; 由parse_query_dop设置 |

```sql
-- 验证SMP是否生效
SET query_dop = 4;
EXPLAIN SELECT region_id, SUM(net_amount) FROM fact_sales GROUP BY region_id;
-- 观察: 计划中出现Stream(redistribute)节点即SMP已启用;
-- 无Stream节点时按streamplan_single.cpp set_default_stream的三个条件排查:
-- query_dop是否>1、该查询是否允许SMP、是否处于外层流计划内层
```

---

## 总结

- openGauss单机并行的真实形态是SMP流模型: 计划插入Stream节点,同进程内stream线程分工,执行机制在 `src/gausskernel/process/stream/`(ExecInitStream/ExecStream/StreamMain/StreamProducer/StreamConsumer);46节的Gather/Worker进程模型是PostgreSQL风格教学简化。
- `query_dop` 是总开关: 1关闭;正值静态固定并行,负值|值|为动态上限(单机动态SMP主体为分布式空壳,以静态为主);取值[-64,64]。
- SMP代价模型: `cost_seqscan` 把 `smp_thread_cost*(dop-1)` 计入运行成本并按dop均摊页/CPU成本;`compute_stream_cost` 给流接收端加 `(receiveDop-1)*smp_thread_cost`;DEFAULT_SMP_THREAD_COST=1000。
- 执行器统计真实载体是 `instrument.h` 的 `Instrumentation`+`BufferUsage`+`CPUUsage`,按 `InstrumentOption` 位掩码收集;EXPLAIN (ANALYZE, BUFFERS) 呈现,BUFFERS选项与"Rows Removed by Filter"均在explain.cpp。
- 读执行统计的三个要点: actual time要乘loops、temp read/written非零即落盘、Rows Removed by Filter过高说明扫描方式不当。
- 并行度决策基线: 短查询关、扫描型中查询2~4、大聚合4~8(先算work_mem×dop的内存账)、高并发系统总开关压低;并发×并行度不超过CPU核数。
- SMP路径由 `JoinPathGen::addJoinParallelInfo`/`setStreamParallelInfo` 生成,是否启用流计划由 `set_default_stream` 三条件门控。

## 版本注记

- 2026-11-05 断尾补齐,原文 500 行截断。原文截断于47.1节Instrumentation代码块内(BufferAccess结构体字段处);本次补完该结构体,给出真实对应(BufferUsage/instrument.h),补写统计呈现方式,并顺势补写47.2 SMP并行与并行度调优决策(GUC语义/代价模型/决策表)。
- 原文头部"继续前序文档..."占位行一并清理。
- 46节引用的execParallel.cpp/nodeGather.cpp/nodeParallelseqscan.cpp等文件在openGauss 6.0.0源码树中不存在,已在47.2开头明确说明并给出真实的流执行源码位置;源码引用一律为 文件名+函数名,基于源码树逐项核对,未核实处标注⚓待锚。

## 来源

- 源码基线: openGauss-server-v6.0.0 — src/common/backend/utils/misc/guc/guc_sql.cpp(query_dop注册/parse_query_dop/enable_seqscan_dopcost/enable_stream_operator)、src/include/miscadmin.h(MAX_QUERY_DOP)、src/include/optimizer/cost.h(DEFAULT_SMP_THREAD_COST)、src/gausskernel/optimizer/path/costsize.cpp(cost_seqscan)、src/gausskernel/optimizer/plan/streamplan_utils.cpp(compute_stream_cost)、src/gausskernel/optimizer/path/streampath_single.cpp(JoinPathGen::addJoinParallelInfo/setStreamParallelInfo)、src/gausskernel/optimizer/plan/streamplan_single.cpp(set_default_stream)、src/gausskernel/optimizer/plan/dynsmp_single.cpp(IsDynamicSmpEnabled)、src/include/optimizer/streamplan.h(IS_STREAM_PLAN)、src/gausskernel/process/stream/(execStream.cpp/streamMain.cpp/streamProducer.cpp)、src/include/executor/instrument.h(BufferUsage/CPUUsage/InstrumentOption)、src/gausskernel/optimizer/commands/explain.cpp(BUFFERS选项/show_instrumentation_count)
- 原文 2026-04 学习档案(优化器系列第九部分,500行截断版)
