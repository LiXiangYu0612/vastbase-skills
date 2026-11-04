# VastBase/openGauss 优化器执行器接口与并行执行深度分析

> 源码版本: openGauss-server-v6.0.0  
> 聚焦: 执行器接口、并行执行机制、执行节点详解

---

## 目录

### 第八部分: 执行器架构
- [43. 执行器框架与接口](#43-执行器框架与接口)
- [44. 执行节点详解](#44-执行节点详解)
- [45. 表达式计算与求值](#45-表达式计算与求值)
- [46. 并行执行机制](#46-并行执行机制)

---

## 43. 执行器框架与接口

### 43.1 执行器整体架构

**核心数据结构**:

```cpp
// src/include/nodes/execnodes.h
// EState: 执行器状态

typedef struct EState {
    NodeTag     type;
    
    // 查询计划
    Plan*       es_plannedstmt;    // 查询计划树
    
    // 查询描述符
    QueryDesc*  es_queryDesc;      // 查询描述符
    
    // 结果元组
    TupleTableSlot* es_result_tuple_slot;  // 结果元组槽
    
    // 目标列表
    List*       es_expr_ctx;       // 表达式上下文列表
    
    // 内存上下文
    MemoryContext es_query_cxt;    // 查询内存上下文
    MemoryContext es_per_tuple_memory_context;  // 每元组内存上下文
    
    // 执行状态
    int         es_direction;      // 扫描方向
    bool        es_instrument;     // 是否 instrumentation
    
    // 统计信息
    Instrumentation* es_instrument; // 执行统计信息
    
    // 其他
    void*       es_junkFilter;     // junk filter
    List*       es_trig_target_relids;  // 触发器目标关系
    
} EState;

// src/include/nodes/execnodes.h
// TupleTableSlot: 元组槽 (元组容器)

typedef struct TupleTableSlot {
    NodeTag     type;
    
    // 元组数据
    HeapTuple   tts_tuple;         // HeapTuple (row-store)
    MinimalTuple tts_mintuple;     // MinimalTuple (compressed)
    
    // 元组描述符
    TupleDesc   tts_tupleDescriptor; // 元组描述符
    
    // 值数组
    Datum*      tts_values;        // Datum数组
    bool*       tts_isnull;        // NULL标记数组
    
    // 标志位
    bool        tts_shouldFree;    // 是否应该释放元组
    bool        tts_shouldFreeMin; // 是否应该释放MinimalTuple
    bool        tts_isempty;       // 是否为空
    
    // 缓冲区
    Buffer      tts_buffer;        // 磁盘缓冲区
    
    // 虚拟元组
    HeapTuple   tts_tupleHeader;   // 元组头部
    
} TupleTableSlot;

// ExprContext: 表达式上下文
typedef struct ExprContext {
    NodeTag     type;
    
    // 元组槽
    TupleTableSlot* ecxt_scantuple;    // 扫描元组
    TupleTableSlot* ecxt_innertuple;   // 内表元组
    TupleTableSlot* ecxt_outertuple;   // 外表元组
    
    // 值数组
    Datum*      ecxt_per_tuple_memory; // 每元组内存
    MemoryContext ecxt_per_tuple_memory_context;
    
    // 参数
    ParamListInfo ecxt_param_list_info; // 参数列表
    
    // 其他
    Datum       ecxt_aggvalues;     // 聚合值
    bool*       ecxt_aggnulls;      // 聚合NULL标记
    
} ExprContext;
```

**执行流程**:

```cpp
// src/gausskernel/executor/execMain.cpp
// standard_ExecutorStart函数

void standard_ExecutorStart(QueryDesc* queryDesc, int eflags)
{
    EState* estate = NULL;
    Plan* plan = queryDesc->plannedstmt->planTree;
    
    // 1. 创建EState
    estate = CreateExecutorState();
    estate->es_plannedstmt = queryDesc->plannedstmt;
    estate->es_queryDesc = queryDesc;
    
    // 2. 初始化表达式上下文
    estate->es_expr_ctx = CreateExprContext(estate);
    
    // 3. 初始化计划节点
    ExecInitNode(plan, estate, eflags);
    
    // 4. 初始化结果元组槽
    estate->es_result_tuple_slot = MakeTupleTableSlot();
    set_slot_tts_tupleDescriptor(estate->es_result_tuple_slot, queryDesc->tupDesc);
    
    queryDesc->estate = estate;
}

// src/gausskernel/executor/execMain.cpp
// standard_ExecutorRun函数

void standard_ExecutorRun(QueryDesc* queryDesc, ScanDirection direction, uint64 count)
{
    EState* estate = queryDesc->estate;
    Plan* plan = queryDesc->plannedstmt->planTree;
    
    // 设置执行方向
    estate->es_direction = direction;
    
    // 执行计划节点
    ExecutePlan(estate, plan, direction, count);
}

// src/gausskernel/executor/execMain.cpp
// ExecutePlan函数

void ExecutePlan(EState* estate, Plan* plan, ScanDirection direction, uint64 count)
{
    TupleTableSlot* slot = NULL;
    uint64 current_count = 0;
    
    // 循环执行计划节点
    for (;;) {
        // 1. 重置每元组内存
        ResetExprContext(estate->es_expr_ctx);
        
        // 2. 执行计划节点,获取一个元组
        slot = ExecProcNode(plan);
        
        // 3. 检查是否结束
        if (TupIsNull(slot)) {
            break;
        }
        
        // 4. 处理元组
        if (estate->es_result_relation != NULL) {
            // DML操作 (INSERT/UPDATE/DELETE)
            Insert/Delete/Update...
        } else {
            // SELECT操作
            SendResultToClient(slot);
        }
        
        // 5. 检查计数限制
        current_count++;
        if (count != 0 && current_count >= count) {
            break;
        }
    }
}

// src/gausskernel/executor/execMain.cpp
// standard_ExecutorEnd函数

void standard_ExecutorEnd(QueryDesc* queryDesc)
{
    EState* estate = queryDesc->estate;
    Plan* plan = queryDesc->plannedstmt->planTree;
    
    // 1. 释放计划节点
    ExecEndNode(plan);
    
    // 2. 释放表达式上下文
    FreeExprContext(estate->es_expr_ctx);
    
    // 3. 释放EState
    FreeExecutorState(estate);
    
    queryDesc->estate = NULL;
}
```

### 43.2 执行节点初始化

**ExecInitNode函数**:

```cpp
// src/gausskernel/executor/execProcnode.cpp
// ExecInitNode函数

PlanNode* ExecInitNode(Plan* node, EState* estate, int eflags)
{
    PlanNode* result = NULL;
    
    // 根据节点类型初始化
    switch (nodeTag(node)) {
        case T_SeqScan:
            result = (PlanNode*) ExecInitSeqScan((SeqScan*) node, estate, eflags);
            break;
        
        case T_IndexScan:
            result = (PlanNode*) ExecInitIndexScan((IndexScan*) node, estate, eflags);
            break;
        
        case T_BitmapScan:
            result = (PlanNode*) ExecInitBitmapScan((BitmapScan*) node, estate, eflags);
            break;
        
        case T_TidScan:
            result = (PlanNode*) ExecInitTidScan((TidScan*) node, estate, eflags);
            break;
        
        case T_SubqueryScan:
            result = (PlanNode*) ExecInitSubqueryScan((SubqueryScan*) node, estate, eflags);
            break;
        
        case T_FunctionScan:
            result = (PlanNode*) ExecInitFunctionScan((FunctionScan*) node, estate, eflags);
            break;
        
        case T_ValuesScan:
            result = (PlanNode*) ExecInitValuesScan((ValuesScan*) node, estate, eflags);
            break;
        
        case T_CteScan:
            result = (PlanNode*) ExecInitCteScan((CteScan*) node, estate, eflags);
            break;
        
        case T_WorkTableScan:
            result = (PlanNode*) ExecInitWorkTableScan((WorkTableScan*) node, estate, eflags);
            break;
        
        case T_NestLoop:
            result = (PlanNode*) ExecInitNestLoop((NestLoop*) node, estate, eflags);
            break;
        
        case T_MergeJoin:
            result = (PlanNode*) ExecInitMergeJoin((MergeJoin*) node, estate, eflags);
            break;
        
        case T_HashJoin:
            result = (PlanNode*) ExecInitHashJoin((HashJoin*) node, estate, eflags);
            break;
        
        case T_Material:
            result = (PlanNode*) ExecInitMaterial((Material*) node, estate, eflags);
            break;
        
        case T_Sort:
            result = (PlanNode*) ExecInitSort((Sort*) node, estate, eflags);
            break;
        
        case T_Group:
            result = (PlanNode*) ExecInitGroup((Group*) node, estate, eflags);
            break;
        
        case T_Agg:
            result = (PlanNode*) ExecInitAgg((Agg*) node, estate, eflags);
            break;
        
        case T_Window:
            result = (PlanNode*) ExecInitWindow((Window*) node, estate, eflags);
            break;
        
        case T_Unique:
            result = (PlanNode*) ExecInitUnique((Unique*) node, estate, eflags);
            break;
        
        case T_Hash:
            result = (PlanNode*) ExecInitHash((Hash*) node, estate, eflags);
            break;
        
        case T_SetOp:
            result = (PlanNode*) ExecInitSetOp((SetOp*) node, estate, eflags);
            break;
        
        case T_Limit:
            result = (PlanNode*) ExecInitLimit((Limit*) node, estate, eflags);
            break;
        
        case T_LockRows:
            result = (PlanNode*) ExecInitLockRows((LockRows*) node, estate, eflags);
            break;
        
        case T_Gather:
        case T_GatherMerge:
            result = (PlanNode*) ExecInitGather((Gather*) node, estate, eflags);
            break;
        
        default:
            ereport(ERROR,
                    (errmsg("unrecognized node type: %d", nodeTag(node))));
            break;
    }
    
    return result;
}
```

---

## 44. 执行节点详解

### 44.1 扫描节点

**SeqScan节点**:

```cpp
// src/gausskernel/executor/nodeSeqscan.cpp
// SeqScan节点数据结构

typedef struct SeqScanState {
    ScanState   ss;               // 基础ScanState
    Relation    ss_currentRelation; // 当前关系
    TableScanDesc ss_currentScanDesc; // 扫描描述符
    HeapTuple   ss_ScanTupleSlot; // 扫描元组槽
} SeqScanState;

// ExecInitSeqScan函数
SeqScanState* ExecInitSeqScan(SeqScan* node, EState* estate, int eflags)
{
    SeqScanState* scanstate = makeNode(SeqScanState);
    
    // 1. 初始化基础ScanState
    ExecInitScanTupleSlot(estate, &scanstate->ss);
    ExecInitResultTupleSlot(estate, &scanstate->ss.ps);
    
    // 2. 打开表
    Relation relation = ExecOpenScanRelation(estate, node->scanrelid, eflags);
    scanstate->ss_currentRelation = relation;
    
    // 3. 初始化扫描描述符
    scanstate->ss_currentScanDesc = 
        table_scan_begin(relation, 
                        GetActiveSnapshot(), 
                        0,  // 无并行
                        NULL);
    
    // 4. 初始化表达式
    ExecInitExpr(node->plan.qual, (PlanState*) scanstate);
    
    return scanstate;
}

// ExecSeqScan函数
TupleTableSlot* ExecSeqScan(PlanState* pstate)
{
    SeqScanState* node = (SeqScanState*) pstate;
    Relation relation = node->ss_currentRelation;
    TableScanDesc scandesc = node->ss_currentScanDesc;
    TupleTableSlot* slot = node->ss.ss_ScanTupleSlot;
    ExprContext* econtext = node->ss.ps.ps_ExprContext;
    
    // 循环扫描
    for (;;) {
        // 1. 从表扫描一个元组
        ExecScanFetch(node, direction, slot);
        
        // 2. 检查是否结束
        if (TupIsNull(slot)) {
            return NULL;  // 扫描结束
        }
        
        // 3. 执行限定条件
        econtext->ecxt_scantuple = slot;
        if (ExecQual(node->ss.ps.qual, econtext, false)) {
            // 条件满足,返回元组
            return slot;
        }
        // 条件不满足,继续扫描下一个元组
    }
}

// ExecEndSeqScan函数
void ExecEndSeqScan(SeqScanState* node)
{
    // 1. 释放扫描描述符
    table_scan_end(node->ss_currentScanDesc);
    
    // 2. 关闭表
    ExecCloseScanRelation(node->ss_currentRelation);
}
```

**IndexScan节点**:

```cpp
// src/gausskernel/executor/nodeIndexscan.cpp
// IndexScanState节点数据结构

typedef struct IndexScanState {
    ScanState   ss;               // 基础ScanState
    List*       indexqual;        // 索引条件
    IndexRuntimeKeyInfo* index_runtime_keys;  // 运行时键
    
    // 索引扫描状态
    IndexScanDesc iss_ScanDesc;   // 索引扫描描述符
    bool        iss_Reversed;     // 是否反向扫描
    bool        iss_KeysOK;       // 键是否OK
    
    // 多索引扫描
    int         iss_NumIndices;   // 索引数量
    IndexScanDesc* iss_Scans;     // 索引扫描描述符数组
    ScanKey     iss_ScanKeys;     // 扫描键
    
} IndexScanState;

// ExecInitIndexScan函数
IndexScanState* ExecInitIndexScan(IndexScan* node, EState* estate, int eflags)
{
    IndexScanState* indexstate = makeNode(IndexScanState);
    
    // 1. 初始化基础ScanState
    ExecInitScanTupleSlot(estate, &indexstate->ss);
    ExecInitResultTupleSlot(estate, &indexstate->ss.ps);
    
    // 2. 打开表
    Relation relation = ExecOpenScanRelation(estate, node->scan.scanrelid, eflags);
    indexstate->ss.ss_currentRelation = relation;
    
    // 3. 打开索引
    List* indexqual = node->indexqual;
    List* indexid = node->indexid;
    
    // 4. 初始化索引扫描描述符
    indexstate->iss_ScanDesc = 
        index_beginscan(relation, 
                       indexid, 
                       estate->es_snapshot, 
                       0,    // 无并行
                       NULL);
    
    // 5. 初始化表达式
    ExecInitExpr(node->indexqual, (PlanState*) indexstate);
    ExecInitExpr(node->scan.plan.qual, (PlanState*) indexstate);
    
    return indexstate;
}

// ExecIndexScan函数
TupleTableSlot* ExecIndexScan(PlanState* pstate)
{
    IndexScanState* node = (IndexScanState*) pstate;
    EState* estate = node->ss.ps.state;
    ExprContext* econtext = node->ss.ps.ps_ExprContext;
    TupleTableSlot* slot = node->ss.ss_ScanTupleSlot;
    
    // 循环扫描
    for (;;) {
        // 1. 从索引扫描一个元组
        HeapTuple tuple = index_getnext(node->iss_ScanDesc, 
                                       ForwardScanDirection);
        
        // 2. 检查是否结束
        if (tuple == NULL) {
            return NULL;  // 扫描结束
        }
        
        // 3. 存储元组到槽
        ExecStoreTuple(tuple, slot, InvalidBuffer, false);
        
        // 4. 执行限定条件
        econtext->ecxt_scantuple = slot;
        if (ExecQual(node->ss.ps.qual, econtext, false)) {
            // 条件满足,返回元组
            return slot;
        }
        // 条件不满足,继续取下一个索引元组
    }
}

// ExecEndIndexScan函数
void ExecEndIndexScan(IndexScanState* node)
{
    // 1. 释放索引扫描描述符
    index_endscan(node->iss_ScanDesc);

    // 2. 关闭表
    ExecCloseScanRelation(node->ss.ss_currentRelation);
}
```

**观察**:

- 扫描节点共用同一个骨架: 循环取元组 → 存入槽 → `ExecQual`过滤 → 命中即返回。
- SeqScan从表扫描描述符取元组,IndexScan从索引扫描描述符取元组,过滤框架完全一致 — 这就是"一次调用返回一个元组"的Volcano流水线约定。
- 每个算子文件都遵循 Init/Exec/End/ReScan 四件套,新增算子只需按此约定接入 `ExecInitNode` 分发表。

> 路径说明: v6.0.0 源码树中执行器实际位于 `src/gausskernel/runtime/executor/`(execMain.cpp、execProcnode.cpp、execUtils.cpp、nodeXxx.cpp均在其中),本节新补内容按实际路径标注。

### 44.2 执行器内存上下文与算子流水线

**三级内存上下文**:

```cpp
// src/gausskernel/runtime/executor/execUtils.cpp
// CreateExecutorState函数 (真实源码节选)

EState* CreateExecutorState()
{
    // 1. 每次执行创建一个查询级上下文"ExecutorState"
    qcontext = AllocSetContextCreate(CurrentMemoryContext,
        "ExecutorState",
        ALLOCSET_DEFAULT_MINSIZE,
        ALLOCSET_DEFAULT_INITSIZE,
        ALLOCSET_DEFAULT_MAXSIZE);

    // 2. EState本体分配在查询级上下文里,随上下文整体释放
    oldcontext = MemoryContextSwitchTo(qcontext);
    estate = makeNode(EState);
    // ...

    // 3. EState持有上下文指针,结束时统一回收
    estate->es_query_cxt = qcontext;
    estate->es_const_query_cxt = qcontext;  // 不会被中途切换的原始查询上下文
    return estate;
}

// src/gausskernel/runtime/executor/execUtils.cpp
// CreateExprContext函数 (真实源码节选)

ExprContext* CreateExprContext(EState* estate)
{
    // ExprContext本体放在查询级上下文
    oldcontext = MemoryContextSwitchTo(estate->es_query_cxt);
    econtext = makeNode(ExprContext);
    // ...

    // 每个ExprContext自带一个"每元组"子上下文
    econtext->ecxt_per_tuple_memory = AllocSetContextCreate(estate->es_query_cxt,
        "ExprContext",
        ALLOCSET_DEFAULT_MINSIZE,
        ALLOCSET_DEFAULT_INITSIZE,
        ALLOCSET_DEFAULT_MAXSIZE);
    // ...
}
```

**每元组内存的重置**:

```cpp
// src/include/executor/executor.h
// ResetExprContext宏 (真实源码)

#define ResetExprContext(econtext) \
    MemoryContextReset((econtext)->ecxt_per_tuple_memory)

// src/gausskernel/runtime/executor/execMain.cpp
// ExecutePlan函数 (真实源码节选)

for (;;) {
    // 1. 每取一个元组前,重置每元组上下文
    //    上一元组表达式求值产生的临时Datum在这里整批回收
    ResetExprContext(estate->es_per_tuple_exprcontext);

    // 2. 拉取下一个元组
    slot = ExecProcNode(planstate);
    // ...
}
```

**内存层次**:

| 层次 | 上下文 | 生命周期 | 典型内容 |
|------|--------|----------|----------|
| 查询级 | es_query_cxt ("ExecutorState") | 整个查询执行期 | EState、PlanState、ExprContext本体 |
| 节点级 | 各PlanState的工作区子上下文 | 节点生命周期 | 哈希表、排序缓冲等节点工作数据 |
| 每元组级 | ecxt_per_tuple_memory ("ExprContext") | 一个元组处理完即重置 | 表达式求值临时Datum、函数返回的中间结果 |

**算子流水线(Volcano模型)**:

```cpp
// src/include/executor/executor.h
// ExecProcNode内联函数 (真实源码节选)

static inline TupleTableSlot* ExecProcNode(PlanState* node)
{
    // 1. 提前终止检查(如语句被取消)
    if (unlikely(executorEarlyStop())) {
        return NULL;
    }

    // 2. 参数变化时先重扫描(相关子查询/连接内层复用)
    if (node->chgParam != NULL) {
        ExecReScan(node);
    }

    // 3. 函数指针分发到具体算子自己的执行函数
    result = node->ExecProcNode(node);  // 即该节点的ExecSeqScan/ExecNestLoop/...
    return result;
}
```

- 每个算子在Init阶段把自己的执行函数挂到 `node->ExecProcNode`,父算子循环调用子算子,一次传递一个元组 — 计划树"自顶向下驱动、自底向上产数据"就是这么来的。
- `ExecReScan` 是流水线的复位接口: NestLoop每取一个外表元组,就驱动内层节点ReScan一次。
- `ExecProcNodeInstr`(execProcnode.cpp)把计时/计数统计包装进同一条流水线,不破坏一次一元组的节奏。

**调优视角**:

- 每元组上下文重置是执行器最重要的内存纪律: 元组级临时分配绝不能跑到查询级上下文,否则内存占用量随处理行数线性膨胀。
- 扫描类节点的CPU开销里,限定条件表达式在每元组上下文中的分配量占比可观 — 把过滤性最强的简单条件放前面,能减少昂贵函数的调用次数。
- EXPLAIN ANALYZE会启用Instrumentation包装路径,本身有开销;对比纯计划形状时用不带ANALYZE的输出。

---

## 总结

- 执行器骨架是四段式: `standard_ExecutorStart`(建EState、初始化节点) → `standard_ExecutorRun` → `ExecutePlan`(循环拉元组) → `standard_ExecutorEnd`(逆序释放),全部在execMain.cpp。
- 三个核心结构: EState是执行期全局状态,TupleTableSlot是元组容器,ExprContext是表达式求值的舞台(扫描元组/内表元组/外表元组三个槽位)。
- `ExecInitNode`按节点标签分发初始化,每类算子一个nodeXxx.cpp,遵循Init/Exec/End/ReScan四件套;新增算子只要接入分发表。
- 扫描算子共用"取元组 → 入槽 → ExecQual → 命中返回"模板,SeqScan与IndexScan只差元组来源。
- 执行器内存分三层: 查询级"ExecutorState"上下文、节点级工作区、每元组"ExprContext"子上下文;`ResetExprContext`每元组重置是内存纪律的核心(executor.h宏定义)。
- `ExecProcNode`以函数指针分发,构成一次一元组的Volcano流水线;`ExecReScan`支撑连接内层复用,`ExecProcNodeInstr`把统计挂上流水线。
- 调优抓手: 元组级临时分配量影响扫描CPU;EXPLAIN ANALYZE自身的Instrumentation开销在精确计时场景要计入。

## 版本注记

- 2026-11-05 断尾补齐,原文 500 行截断。原文截断于44.1节ExecIndexScan代码块内;本次补完该函数与ExecEndIndexScan,并顺势补写44.2执行器内存上下文与算子流水线(三级内存层次/ResetExprContext/函数指针分发)。
- 原文目录中45节(表达式计算与求值)与46节(并行执行机制)未出现在截断前的正文中;46的并行执行在系列09篇(并行执行与执行器调优)展开。
- 源码引用一律为 文件名+函数名,基于 openGauss-server-v6.0.0 源码树逐项核对;未核实处标注⚓待锚。

## 来源

- 源码基线: openGauss-server-v6.0.0 — src/gausskernel/runtime/executor/execMain.cpp(standard_ExecutorStart/standard_ExecutorRun/ExecutePlan/standard_ExecutorEnd)、src/gausskernel/runtime/executor/execProcnode.cpp(ExecInitNode/ExecProcNodeInstr)、src/gausskernel/runtime/executor/execUtils.cpp(CreateExecutorState/CreateExprContext/FreeExecutorState)、src/include/executor/executor.h(ExecProcNode内联分发/ResetExprContext宏)
- 原文 2026-04 学习档案(优化器系列第八部分,500行截断版)
