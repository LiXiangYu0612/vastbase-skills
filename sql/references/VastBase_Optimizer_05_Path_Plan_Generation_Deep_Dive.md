# VastBase/openGauss Path生成与Plan创建超详细深度分析

> 源码版本: openGauss-server-v6.0.0  
> 分析文件: path/*.cpp (20+文件), plan/createplan.cpp (7000+行)  
> 学习目标: 掌握Path节点生成、Plan节点创建的完整流程

---

## 目录

### 第六部分: Path生成与Plan创建
- [28. Path数据结构与继承关系](#28-path数据结构与继承关系)
- [29. 路径生成算法](#29-路径生成算法)
- [30. Plan节点创建](#30-plan节点创建)
- [31. 执行计划生成完整流程](#31-执行计划生成完整流程)

---

## 28. Path数据结构与继承关系

### 28.1 Path基础结构

**源码分析**:

```cpp
// src/include/nodes/relation.h
// Path节点的核心结构

typedef struct Path {
    NodeTag     type;           // 节点类型
    
    // 路径类型标识
    NodeTag     pathtype;       // T_SeqScan, T_IndexScan, T_NestLoop, etc.
    
    // 父RelOptInfo指针
    RelOptInfo* parent;         // 该路径代表的关系
    
    // 路径代价估计
    Cost        startup_cost;   // 启动代价 (输出第一行前的代价)
    Cost        total_cost;     // 总代价 (输出所有行的代价)
    
    // 行数估计
    double      rows;           // 估计输出行数
    
    // 路径键 (排序信息)
    List*       pathkeys;       // PathKey* 列表
    
    // 其他参数
    List*       pathorder;      // 路径排序信息 (仅部分路径使用)
    Cardinality limit_rows;     // 限制行数 (LIMIT子句)
    
    // 并行度
    int         parallel_degree;// 并行工作线程数
    
    // 并行安全
    bool        parallel_safe;  // 是否可以并行执行
    
    // 分区信息
    Oid         partOid;        // 分区表OID
    List*       prunedParts;    // 裁剪的分区列表
    
    // 流信息
    double      stream_cost;    // 流式传输代价
    
    // 附加信息
    Bitmapset*  parentids;      // 父关系ID集合
    
} Path;

// PathType枚举
typedef enum PathType {
    T_SeqScan,          // 顺序扫描
    T_IndexScan,        // 索引扫描
    T_IndexOnlyScan,    // 仅索引扫描
    T_BitmapScan,       // 位图扫描
    T_TidScan,          // TID扫描 (CTID扫描)
    T_SubqueryScan,     // 子查询扫描
    T_FunctionScan,     // 函数扫描
    T_ValuesScan,       // Values扫描
    T_CteScan,          // CTE扫描
    T_WorkTableScan,    // 工作表扫描 (递归查询)
    T_ForeignScan,      // 外部表扫描
    T_NestLoop,         // 嵌套循环连接
    T_MergeJoin,        // 归并连接
    T_HashJoin,         // 哈希连接
    T_Material,         // 物化节点
    T_Sort,             // 排序节点
    T_Group,            // 分组节点
    T_Agg,              // 聚合节点
    T_Window,           // 窗口函数节点
    T_Unique,           // 去重节点
    T_Gather,           // 收集节点 (并行)
    T_GatherMerge,      // 收集归并节点 (并行)
    T_Hash,             // 哈希节点
    T_SetOp,            // 集合操作节点
    T_LockRows,         // 锁行节点 (SELECT FOR UPDATE)
    T_Limit,            // 限制节点 (LIMIT/OFFSET)
    T_NestLoopPath,     // 嵌套循环路径 (未转换为Plan)
    T_MergeJoinPath,    // 归并连接路径
    T_HashJoinPath,     // 哈希连接路径
    ...
} PathType;
```

**Path类型分类**:

1. **扫描路径 (Scan Paths)**:
   - SeqScanPath: 顺序扫描路径
   - IndexPath: 索引扫描路径
   - BitmapHeapPath: 位图堆表扫描路径
   - TidPath: TID扫描路径
   - SubqueryScanPath: 子查询扫描路径

2. **连接路径 (Join Paths)**:
   - NestPath: 嵌套循环连接路径
   - MergePath: 归并连接路径
   - HashPath: 哈希连接路径

3. **修改路径 (Modification Paths)**:
   - ModifyTablePath: 修改表路径 (INSERT/UPDATE/DELETE)

### 28.2 IndexPath结构

**源码分析**:

```cpp
// src/include/nodes/relation.h
// IndexPath: 索引扫描路径

typedef struct IndexPath {
    Path        path;           // 基础Path结构
    
    // 索引信息
    IndexOptInfo* indexinfo;    // 索引优化信息
    List*       indexclauses;   // 索引子句列表
    List*       indexquals;     // 索引条件子句
    List*       indexorderbys;  // 索引排序子句
    
    // 扫描方向
    ScanDirection indexscandir; // 扫描方向 (ForwardScanDirection/BackwardScanDirection)
    
    // 索引扫描类型
    bool        isjoininner;    // 是否作为内表连接
    bool        isjoinouter;    // 是否作为外表连接
    
    // 参数化路径
    List*       indexclauses;   // 参数化条件
    
    // 索引返回条件
    List*       indexqualcols;  // 索引条件列
    
    // 位图扫描
    bool        is_bitmap;      // 是否位图扫描
    
} IndexPath;

// IndexOptInfo结构
typedef struct IndexOptInfo {
    NodeTag     type;
    
    // 索引基本信息
    Oid         indexoid;       // 索引OID
    Oid         reltablespace;  // 表空间OID
    Oid         relid;          // 表OID
    
    // 索引列信息
    int         ncolumns;       // 索引列数
    int*        indexkeys;      // 索引键 (表列号)
    Oid*        indexcollations;// 排序规则OID
    Oid*        opfamily;       // 操作符族OID
    int*        indexopcos;     // 操作符OID
    
    // 索引类型
    Oid         relam;          // 索引访问方法OID
    char*       indpred;        // 索引谓词 (部分索引)
    
    // 统计信息
    double      pages;          // 索引页数
    double      tuples;         // 索引元组数
    
    // 唯一性
    bool        unique;         // 是否唯一索引
    bool        immediate;      // 是否立即约束
    
    // 是否支持排序
    bool        amcanorder;     // 访问方法是否支持有序扫描
    
    // 是否支持返回
    bool        amcanreturn;    // 访问方法是否支持仅索引扫描
    
    // 是否支持位图扫描
    bool        amcanbitmap;    // 访问方法是否支持位图扫描
    
    // 其他
    List*       indextlist;     // 索引目标列表
    
} IndexOptInfo;
```

**实际应用**:

```sql
-- 创建测试表和索引
CREATE TABLE index_test (
    id INT PRIMARY KEY,
    name VARCHAR(100),
    age INT,
    email VARCHAR(200)
);

CREATE INDEX idx_name ON index_test(name);
CREATE INDEX idx_age ON index_test(age);

ANALYZE index_test;

-- 查询: 使用name索引
EXPLAIN
SELECT * FROM index_test WHERE name = 'John';

-- Path节点生成流程:
-- 1. 考虑所有可用索引 (idx_name, idx_age, index_test_pkey)
-- 2. 对每个索引，检查是否可用于查询条件
-- 3. 生成多个IndexPath:
--    - IndexPath1: 使用idx_name (WHERE name = 'John')
--    - IndexPath2: 使用index_test_pkey (但不匹配条件)
-- 4. 计算每个IndexPath的代价
-- 5. 选择代价最小的路径

-- 查看代价估算
EXPLAIN
SELECT * FROM index_test WHERE name = 'John';

-- 输出示例:
-- Index Scan using idx_name on index_test  (cost=0.00..8.27 rows=1)
--   Index Cond: (name = 'John'::varchar)
--
-- cost=0.00..8.27: 
--   startup_cost = 0.00 (索引扫描从索引根部开始)
--   total_cost = 8.27 (包括索引扫描 + 堆表访问)
-- rows = 1: 估计返回1行
```

### 28.3 JoinPath结构

**源码分析**:

```cpp
// src/include/nodes/relation.h
// JoinPath: 连接路径的基类

typedef struct JoinPath {
    Path        path;           // 基础Path结构
    
    // 连接类型
    JoinType    jointype;       // JOIN_INNER, JOIN_LEFT, JOIN_FULL, etc.
    
    // 外表和内表路径
    Path*       outerjoinpath;  // 外表路径
    Path*       innerjoinpath;  // 内表路径
    
    // 连接条件
    List*       joinrestrictinfo;// 连接限制信息
    
    // 其他
    Path*       outer_path;     // 外表路径 (旧字段,保留兼容)
    Path*       inner_path;     // 内表路径
    
} JoinPath;

// NestPath: 嵌套循环连接路径
typedef struct NestPath {
    JoinPath    jpath;          // 基础JoinPath结构
    
    // 内表路径 (参数化)
    Path*       inner_path;     // 可能是参数化路径
    
    // 其他
    List*       join_clause;    // 连接子句
    
} NestPath;

// MergePath: 归并连接路径
typedef struct MergePath {
    JoinPath    jpath;          // 基础JoinPath结构
    
    // 归并子句
    List*       mergeclauses;   // 归并子句
    List*       outer_pathkeys; // 外表路径键
    List*       inner_pathkeys; // 内表路径键
    
    // 排序标记
    bool        skip_mark_restore; // 是否跳过标记恢复
    
} MergePath;

// HashPath: 哈希连接路径
typedef struct HashPath {
    JoinPath    jpath;          // 基础JoinPath结构
    
    // 哈希子句
    List*       hashclauses;    // 哈希子句
    
    // 哈希表信息
    Path*       inner_path;     // 内表路径 (用于构建哈希表)
    
    // 其他
    bool        skewable;       // 是否倾斜优化
    
} HashPath;
```

**实际应用**:

```sql
-- 创建测试表
CREATE TABLE employees (
    emp_id INT PRIMARY KEY,
    emp_name VARCHAR(100),
    dept_id INT
);

CREATE TABLE departments (
    dept_id INT PRIMARY KEY,
    dept_name VARCHAR(100)
);

-- 插入数据
INSERT INTO employees 
SELECT generate_series(1, 10000), 
       'employee_' || generate_series(1, 10000),
       (random() * 100)::int;

INSERT INTO departments 
SELECT generate_series(1, 100), 
       'department_' || generate_series(1, 100);

ANALYZE employees, departments;

-- 查询: JOIN
EXPLAIN
SELECT * FROM employees e
JOIN departments d ON e.dept_id = d.dept_id;

-- Path节点生成流程:
-- 1. 生成外表路径 (employees):
--    - SeqScanPath: 顺序扫描employees
--    - IndexPath: 可能使用dept_id索引 (如果有)
--
-- 2. 生成内表路径 (departments):
--    - SeqScanPath: 顺序扫描departments
--    - IndexPath: 使用dept_id主键
--
-- 3. 生成连接路径:
--    - NestPath:
--      outer = SeqScan(employees)
--      inner = IndexScan(departments using dept_id_pkey)
--      jointype = JOIN_INNER
--
--    - MergePath:
--      outer = Sort(employees by dept_id)
--      inner = IndexScan(departments by dept_id_pkey)
--      mergeclauses = (e.dept_id = d.dept_id)
--
--    - HashPath:
--      outer = SeqScan(employees)
--      inner = SeqScan(departments)
--      hashclauses = (e.dept_id = d.dept_id)
--
-- 4. 计算每个连接路径的代价:
--    - NestPath cost = outer_cost + (outer_rows * inner_cost)
--    - MergePath cost = outer_cost + inner_cost + sort_cost
--    - HashPath cost = outer_cost + inner_cost + hash_build_cost
--
-- 5. 选择代价最小的路径

-- 查看执行计划
EXPLAIN
SELECT * FROM employees e
JOIN departments d ON e.dept_id = d.dept_id;

-- 可能的输出:
-- Hash Join  (cost=250.00..350.00 rows=10000)
--   Hash Cond: (e.dept_id = d.dept_id)
--   ->  Seq Scan on employees e  (cost=0.00..150.00 rows=10000)
--   ->  Hash  (cost=50.00..50.00 rows=100)
--        ->  Seq Scan on departments d  (cost=0.00..50.00 rows=100)
--
-- 或者:
-- Nested Loop  (cost=0.00..450.00 rows=10000)
--   Join Filter: (e.dept_id = d.dept_id)
--   ->  Seq Scan on employees e  (cost=0.00..150.00 rows=10000)
--   ->  Index Scan using departments_pkey on departments d  (cost=0.00..0.03 rows=1)
--        Index Cond: (dept_id = e.dept_id)
```

---

## 29. 路径生成算法

### 29.1 扫描路径生成

**源码分析**:

```cpp
// src/gausskernel/optimizer/path/allpaths.cpp
// add_base_rels_to_query函数

void add_base_rels_to_query(PlannerInfo* root, Node* jtnode)
{
    if (IsA(jtnode, RangeTblRef)) {
        // 基础关系
        int varno = ((RangeTblRef*) jtnode)->rtindex;
        RelOptInfo* rel = build_simple_rel(root, varno, RELOPT_BASEREL);
    }
    else if (IsA(jtnode, FromExpr)) {
        // FROM表达式
        FromExpr* f = (FromExpr*) jtnode;
        foreach(l, f->fromlist) {
            add_base_rels_to_query(root, lfirst(l));
        }
    }
    else if (IsA(jtnode, JoinExpr)) {
        // JOIN表达式
        JoinExpr* j = (JoinExpr*) jtnode;
        add_base_rels_to_query(root, j->larg);
        add_base_rels_to_query(root, j->rarg);
    }
}

// build_simple_rel函数
RelOptInfo* build_simple_rel(
    PlannerInfo* root,
    int relid,
    RelOptKind reloptkind)
{
    // 创建RelOptInfo
    RelOptInfo* rel = makeNode(RelOptInfo);
    rel->reloptkind = reloptkind;
    rel->relid = relid;
    
    // 获取RangeTblEntry
    RangeTblEntry* rte = planner_rt_fetch(relid, root);
    
    // 设置关系信息
    if (rte->rtekind == RTE_RELATION) {
        rel->reltablespace = rte->reltablespace;
        rel->relid = rte->relid;
        rel->rtekind = rte->rtekind;
    }
    
    // 添加到rel数组
    root->simple_rel_array[relid] = rel;
    root->simple_rel_array_size = Max(root->simple_rel_array_size, relid + 1);
    
    return rel;
}

// src/gausskernel/optimizer/path/allpaths.cpp
// make_rel_from_joinlist函数

void make_rel_from_joinlist(PlannerInfo* root, List* joinlist)
{
    int         levels_needed = list_length(joinlist);
    int         lev;
    RelOptInfo* rel = NULL;
    
    // 递归生成关系
    for (lev = 1; lev <= levels_needed; lev++) {
        // 生成N表关系的路径
        join_search_one_level(root, lev);
        
        // 应用约束排除
        apply_join_restriction(root);
    }
}
```

### 29.2 索引路径生成

**源码分析**:

```cpp
// src/gausskernel/optimizer/path/indxpath.cpp
// create_index_paths函数

void create_index_paths(PlannerInfo* root, RelOptInfo* rel)
{
    // 遍历所有索引
    foreach(lc, rel->indexlist) {
        IndexOptInfo* index = (IndexOptInfo*) lfirst(lc);
        
        // 1. 考虑普通索引扫描
        consider_indexed_clauses(root, rel, index, clause_groups);
        
        // 2. 考虑位图索引扫描
        consider_index_join_clauses(root, rel, index, clause_groups);
        
        // 3. 考虑仅索引扫描
        if (index->amcanreturn) {
            consider_index_only_scan(root, rel, index);
        }
    }

    // 每个索引能生成多少条路径取决于:
    // - 索引条件能匹配到第几列 (前缀匹配)
    // - 索引序能否被ORDER BY利用 (免排序的pathkeys)
    // - 是否作为参数化内表 (JOIN外表传参)
    // 生成的路径最终都要经过add_path筛选 (见29.3)
}
```

真实源码 (indxpath.cpp / create_index_paths) 的骨架与上面一致, 只是拆得更细: `match_clauses_to_index` 先把关系条件按索引列归类, `get_index_paths` → `build_index_paths` 拼出普通IndexPath并调 `create_index_path` 定稿; 仅索引扫描的可行性判定在 `check_index_only` (要求目标列全部包含在索引中且无需回表取可见性); JOIN参数化路径由 `consider_index_join_clauses` 处理。

### 29.3 add_path与路径竞争规则

路径生成的最后一步是竞争: 每条新路径都要与pathlist中的存量路径比一场, 被支配的当场淘汰, 否则这个列表会指数膨胀。

**源码分析**:

```cpp
// src/gausskernel/optimizer/util/pathnode.cpp
// 函数: add_path

void add_path(PlannerInfo* root, RelOptInfo* parent_rel, Path* new_path)
{
    bool accept_new = true;

    // 对每条旧路径做四个维度的比较:
    for (每条old_path) {
        // 维度1: 代价 (模糊比较, 容差1%)
        // FUZZY_FACTOR = 1.01: 代价差在1%以内视为"相等"
        // 目的: 抑制浮点舍入导致的计划抖动
        costcmp = compare_path_costs_fuzzily(new_path, old_path, FUZZY_FACTOR);

        // 维度2: pathkeys (排序序)
        // 注意: 参数化路径视为无pathkeys (param_info != NULL → NIL)
        keyscmp = compare_pathkeys(new_path->pathkeys, old_path->pathkeys);

        // 维度3: 参数化范围 (需要外部提供哪些表的信息)
        outercmp = bms_subset_compare(new_path的req_outer, old_path的req_outer);

        // 维度4: 行数估计
        // (选择率不同→rows不同, 行数是最后的决胜维度)

        // 支配规则:
        // ① 代价模糊相等:
        //    pathkeys/参数化/rows全面不差 → 淘汰对方
        // ② 新路径代价更优:
        //    只要pathkeys不更差、参数化不更宽、rows不更多 → 淘汰旧路径
        // ③ 新路径代价更差:
        //    对称地, 旧路径支配新路径 → 新路径被拒收
        // ④ 四个维度完全打平:
        //    用极小容差SMALL_FUZZY_FACTOR(1.0000000001)决胜,
        //    仍平则保留旧路径 (保证平台间计划稳定)
    }

    // 收留的新路径按total_cost升序插入pathlist (代价最低的永远在队首)
    // openGauss/VastBase特有:
    // - AddPathPreCheck: 建路径前先做预检, 明显没戏的直接不建
    // - hint: set_hint_value/set_index_hint_value, hint指定路径优先保留
    // - plan_mode_seed != OPTIMIZE_PLAN时(随机计划模式)不做竞争直接插入
    // - 流计划模式下分布键不同的路径都保留, 不互相淘汰
}
```

**竞争实例: 排序路径的经典对局**:

```sql
-- 场景: index_test(name上有索引 idx_name), 查询带ORDER BY + LIMIT
EXPLAIN
SELECT * FROM index_test ORDER BY name LIMIT 10;

-- 参与竞争的路径:
-- 路径A: SeqScan + Sort
--   total_cost较低? 不一定, 但排序要全量读
-- 路径B: IndexScan using idx_name
--   pathkeys有序(免Sort), LIMIT只需取前10行就停
--
-- total_cost维度A可能占优, pathkeys维度B占优 → 两条都保留
-- LIMIT节点优先看cheapest_startup_path → B几乎必然胜出
--
-- 这解释了一个常见现象: 带LIMIT的查询选中了total_cost更高的索引扫描,
-- 不是估算错了, 是startup_cost与pathkeys在起作用
```

**竞争的收口: set_cheapest**:

```text
每个RelOptInfo的pathlist稳定后, set_cheapest (pathnode.cpp) 选出:
- cheapest_startup_path: 启动代价最低 (LIMIT/EXISTS类查询优先)
- cheapest_total_path:   总代价最低 (常规选择)
上层JOIN枚举只拿这两条(及参数化路径)参与组合,
所以add_path裁剪的质量直接决定整个搜索的质量
```

与JOIN搜索的衔接: standard_join_search (allpaths.cpp) 逐层调join_search_one_level → make_join_rel → add_paths_to_joinrel (joinpath.cpp), 每个joinrel的pathlist都靠add_path收敛 — 全链路是同一套竞争规则。

---

## 总结

- Path是Plan的候选形态: 扫描路径(SeqScan/IndexPath/BitmapHeapPath)、连接路径(NestPath/MergePath/HashPath), 生成期只算代价不真正分配执行器资源。
- 扫描路径入口add_base_rels_to_query→build_simple_rel; 索引路径入口create_index_paths: match_clauses_to_index归类条件, build_index_paths/create_index_path拼路径, check_index_only判定仅索引扫描可行性。
- add_path是路径竞争的裁判, 比较四维度: 代价(1%模糊容差)、pathkeys、参数化范围、rows。
- 支配规则核心: 新路径要淘汰旧路径, 必须在所有维度不差且至少一个维度更好; 完全打平用极小容差决胜并保留旧路径(计划稳定)。
- startup_cost与total_cost是两条独立的优胜线: LIMIT/EXISTS类查询看cheapest_startup_path, 这就是"带LIMIT选中高total_cost索引扫描"的原因。
- 参数化路径视为无pathkeys; 参数化范围互不为子集的路径互不支配, 都保留。
- openGauss/VastBase扩展: AddPathPreCheck预检、hint优先、随机计划模式跳过竞争、流计划下分布键不同的路径共存。
- 搜索全链路: standard_join_search → join_search_one_level → make_join_rel → add_paths_to_joinrel → add_path, 每层joinrel的pathlist都由add_path收敛。

## 版本注记

- 2026-11-05 断尾补齐: 本篇原文在500行处截断(截于29.2节create_index_paths代码块中间一行未写完), 本次顺接补完该代码块并收尾29.2节, 按第29章脉络补写29.3节"add_path与路径竞争规则"。
- 原目录第30节(Plan节点创建)、第31节(执行计划生成完整流程)的原文未随截断保留, 不虚构重建; 29.3结尾以一段文字带过与JOIN搜索链路的衔接。

## 来源

- 源码基线: openGauss-server-v6.0.0。本篇引用: src/gausskernel/optimizer/util/pathnode.cpp(add_path / add_path_precheck / compare_path_costs / compare_path_costs_fuzzily / set_cheapest), src/include/optimizer/pathnode.h(FUZZY_FACTOR=1.01 / SMALL_FUZZY_FACTOR), src/gausskernel/optimizer/path/indxpath.cpp(create_index_paths / match_clauses_to_index / get_index_paths / build_index_paths / create_index_path / check_index_only / consider_index_join_clauses), src/gausskernel/optimizer/path/allpaths.cpp(add_base_rels_to_query / make_rel_from_joinlist / standard_join_search), src/gausskernel/optimizer/path/joinrels.cpp(join_search_one_level / make_join_rel), src/gausskernel/optimizer/path/joinpath.cpp(add_paths_to_joinrel)。
- 本篇原文: 2026-04 学习档案(VastBase/openGauss Path生成与Plan创建超详细深度分析)。
