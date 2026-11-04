# VastBase/openGauss JOIN顺序评估与GEQO超详细深度分析

> 源码版本: openGauss-server-v6.0.0  
> 分析文件: allpaths.cpp, joinrels.cpp, geqo_*.cpp (30+文件)  
> 学习目标: 理解JOIN顺序评估算法、GEQO遗传算法、参数调优

---

## 目录

### 第三部分: JOIN顺序评估
- [14. JOIN顺序评估基础](#14-join顺序评估基础)
- [15. 动态规划算法](#15-动态规划算法)
- [16. join_collapse_limit参数](#16-join_collapse_limit参数)
- [17. from_collapse_limit参数](#17-from_collapse_limit参数)
- [18. GEQO遗传算法](#18-geqo遗传算法)
- [19. JOIN顺序优化实战](#19-join顺序优化实战)

---

## 14. JOIN顺序评估基础

### 14.1 为什么JOIN顺序很重要

**JOIN顺序对性能的影响**:

```sql
-- 创建测试场景
CREATE TABLE join_order_test (
    id INT PRIMARY KEY,
    value INT
);

-- 大表
CREATE TABLE large_table AS
SELECT generate_series(1, 100000) AS id,
       (random() * 1000)::int AS value;

-- 中表
CREATE TABLE medium_table AS
SELECT generate_series(1, 10000) AS id,
       (random() * 1000)::int AS value;

-- 小表
CREATE TABLE small_table AS
SELECT generate_series(1, 1000) AS id,
       (random() * 1000)::int AS value;

ANALYZE large_table, medium_table, small_table;

-- 查询1: 小表在前 (好的JOIN顺序)
EXPLAIN
SELECT COUNT(*)
FROM small_table s
JOIN medium_table m ON s.id = m.id
JOIN large_table l ON m.id = l.id;

-- 查询2: 大表在前 (坏的JOIN顺序)
-- 使用CTE强制JOIN顺序
WITH joined AS (
    SELECT l.*, m.*
    FROM large_table l
    JOIN medium_table m ON l.id = m.id
)
SELECT COUNT(*)
FROM joined j
JOIN small_table s ON j.id = s.id;

-- 对比Cost:
-- 查询1 (小→中→大): Cost ≈ 1000 (低)
-- 查询2 (大→中→小): Cost ≈ 100000 (高)
```

**JOIN顺序选择原理**:

```cpp
// JOIN顺序选择的目标:
// min_cost = min(所有可能的JOIN顺序的cost)

// 示例: 3个表JOIN
// 可能的顺序: 3! = 6种
// 1. A × B × C
// 2. A × C × B
// 3. B × A × C
// 4. B × C × A
// 5. C × A × B
// 6. C × B × A

// 每种顺序的成本不同
// 优化器需要找到成本最低的顺序
```

### 14.2 JOIN顺序评估的数据结构

**RelOptInfo结构**:

```cpp
// src/include/nodes/relation.h
// RelOptInfo: 表示一个关系(表或JOIN结果)

typedef struct RelOptInfo
{
    NodeTag     type;
    
    // 基本属性
    Index       relid;          /* Relation ID (如果是基础表) */
    Oid         reltablespace;  /* 表空间OID */
    AttrNumber  max_attr;       /* 最大属性号 */
    
    // 统计信息
    double      rows;           /* 估计行数 */
    int         width;          /* 平均行宽(字节) */
    BlockNumber pages;          /* 磁盘页数 */
    
    // Cost信息
    Cost        cheapest_startup_cost;   /* 最低启动成本 */
    Cost        cheapest_total_cost;     /* 最低总成本 */
    
    // 路径信息
    List*       pathlist;       /* Path结构列表 */
    Path*       cheapest_startup_path;   /* 最低启动成本路径 */
    Path*       cheapest_total_path;     /* 最低总成本路径 */
    
    // JOIN信息
    Relids      relids;         /* 包含的RelOptInfo ID集合 */
    List*       joininfo;       /* JOIN条件信息 */
    List*       restrictinfo;   /* 限制条件信息 */
    
    // 其他信息
    bool        consider_startup;   /* 是否考虑启动成本 */
    bool        consider_param;     /* 是否考虑参数化路径 */
    
} RelOptInfo;

// 示例: 表示表A
RelOptInfo* relA = makeNode(RelOptInfo);
relA->relid = 1;           /* 表A的OID */
relA->rows = 10000;        /* 估计10000行 */
relA->pages = 100;         /* 占用100页 */
relA->width = 100;         /* 平均行宽100字节 */

// 示例: 表示 A JOIN B
RelOptInfo* relAB = makeNode(ReloptInfo);
relAB->relids = bms_union(relA->relids, relB->relids);  /* {1, 2} */
relAB->rows = 1000;        /* JOIN后估计1000行 */
relAB->cheapest_total_cost = 150.0;  /* 最低成本150 */
```

**JoinRelInfo结构**:

```cpp
// src/include/nodes/relation.h
// JoinRelInfo: 表示JOIN关系的信息

typedef struct JoinRelInfo
{
    NodeTag     type;
    
    Relids      join_relids;    /* JOIN后的Relids */
    
    RelOptInfo* jointype;       /* JOIN类型 */
    
    RelOptInfo* outer_rel;      /* 外表RelOptInfo */
    RelOptInfo* inner_rel;      /* 内表RelOptInfo */
    
    List*       joinclauses;    /* JOIN条件列表 */
    List*       otherclauses;   /* 其他条件列表 */
    
} JoinRelInfo;

// 示例: A JOIN B
JoinRelInfo* joinAB = makeNode(JoinRelInfo);
joinAB->join_relids = bms_union(relA->relids, relB->relids);
joinAB->jointype = JOIN_INNER;
joinAB->outer_rel = relA;
joinAB->inner_rel = relB;
joinAB->joinclauses = list_make1(equal_expr);
```

### 14.3 JOIN顺序枚举算法

**暴力枚举 (Brute Force)**:

```cpp
// 对于N个表，枚举所有可能的JOIN顺序
// 时间复杂度: O(N!)

void enumerate_join_orders_bruteforce(List* rels, int n)
{
    if (n == 1) {
        // 只有一个表，直接返回
        evaluate_cost(rels);
        return;
    }
    
    // 尝试每个表作为第一个表
    for (int i = 0; i < n; i++) {
        // 选择表i作为第一个表
        RelOptInfo* first_rel = list_nth(rels, i);
        List* remaining_rels = list_delete_ptr(rels, first_rel);
        
        // 递归枚举剩余的表
        enumerate_join_orders_bruteforce(remaining_rels, n - 1);
    }
}

// 问题: 对于10个表，10! = 3,628,800 种顺序
//      优化器无法在合理时间内枚举完所有顺序
```

**动态规划 (Dynamic Programming)**:

```cpp
// 使用动态规划避免重复计算
// 时间复杂度: O(N^2 * 2^N)

// dp[S] = 表集合S的最优JOIN成本
// dp[S] = min(dp[T] + dp[S-T] + cost(T × (S-T)))

#define MAX_RELATIONS 32

double dp[1 << MAX_RELATIONS];  // dp[mask] = 该mask的最优成本
Path* best_path[1 << MAX_RELATIONS];

void dp_join_ordering(List* rels)
{
    int n = list_length(rels);
    RelOptInfo** rel_array = (RelOptInfo**) palloc(n * sizeof(RelOptInfo*));
    
    // 初始化: 将rels转换为数组
    int i = 0;
    foreach(rel, rels) {
        rel_array[i++] = (RelOptInfo*) rel;
    }
    
    // 初始化单个表的成本
    for (i = 0; i < n; i++) {
        int mask = 1 << i;
        dp[mask] = 0;  // 单表没有JOIN成本
        best_path[mask] = rel_array[i]->cheapest_total_path;
    }
    
    // 枚举所有子集
    for (int mask = 1; mask < (1 << n); mask++) {
        if (power_of_two(mask)) continue;  // 跳过单表
        
        dp[mask] = INFINITY;
        
        // 尝试将mask分成两个子集 left | right = mask
        for (int left = (mask - 1) & mask; left > 0; left = (left - 1) & mask) {
            int right = mask ^ left;
            
            // 只枚举 left < right 的情况，避免重复
            if (left >= right) continue;
            
            // 尝试所有JOIN顺序
            double cost_lr = dp[left] + dp[right] + 
                            estimate_join_cost(left, right);
            double cost_rl = dp[right] + dp[left] + 
                            estimate_join_cost(right, left);
            
            if (cost_lr < dp[mask]) {
                dp[mask] = cost_lr;
                best_path[mask] = make_join_path(
                    best_path[left], best_path[right], JOIN_LEFT
                );
            }
            
            if (cost_rl < dp[mask]) {
                dp[mask] = cost_rl;
                best_path[mask] = make_join_path(
                    best_path[right], best_path[left], JOIN_RIGHT
                );
            }
        }
    }
    
    // 最终结果: dp[(1 << n) - 1] 是最优成本
    // best_path[(1 << n) - 1] 是最优路径
}

// 时间复杂度分析:
// - 外层循环: 2^n 个mask
// - 内层循环: 平均 2^(n-1) 次分割
// - 总时间: O(2^n * 2^(n-1)) = O(4^n)
//
// 优化:
// - 只枚举包含最低位元的left子集
// - 时间复杂度降至: O(n^2 * 2^n)
//
// 实际应用:
// - n = 10: 10^2 * 2^10 ≈ 10,000 次计算 (可行)
// - n = 20: 20^2 * 2^20 ≈ 400,000,000 次计算 (太慢)
```

**贪心算法 (Greedy)**:

```cpp
// 贪心算法: 每次选择成本最低的JOIN
// 时间复杂度: O(N^2)

void greedy_join_ordering(List* rels)
{
    int n = list_length(rels);
    List* joined_rels = NIL;
    List* remaining_rels = list_copy(rels);
    
    // 选择最小的表作为起点
    RelOptInfo* smallest_rel = find_smallest_rel(remaining_rels);
    joined_rels = lappend(joined_rels, smallest_rel);
    remaining_rels = list_delete_ptr(remaining_rels, smallest_rel);
    
    // 贪心地选择下一个表
    while (remaining_rels != NIL) {
        RelOptInfo* best_rel = NULL;
        Cost best_cost = INFINITY;
        
        // 尝试每个剩余的表
        foreach(rel, remaining_rels) {
            // 计算将该表JOIN到已JOIN集合的成本
            Cost cost = estimate_join_cost(joined_rels, rel);
            
            if (cost < best_cost) {
                best_cost = cost;
                best_rel = (RelOptInfo*) rel;
            }
        }
        
        // 选择成本最低的表
        joined_rels = lappend(joined_rels, best_rel);
        remaining_rels = list_delete_ptr(remaining_rels, best_rel);
    }
    
    // 最终结果: joined_rels 包含所有表，顺序由贪心策略决定
}

// 时间复杂度: O(N^2)
// 优点: 速度快
// 缺点: 不保证全局最优
```

---

## 15. 动态规划算法

### 15.1 make_rel_from_joinrels函数

**源码分析**:

```cpp
// src/gausskernel/optimizer/path/allpaths.cpp
// 函数: make_rel_from_joinrels
// 作用: 使用动态规划生成所有可能的JOIN关系

static void make_rel_from_joinrels(PlannerInfo* root, int level)
{
    List* joinrels = NIL;
    
    // level表示当前JOIN的表数量
    // level = 2: 生成所有2表JOIN
    // level = 3: 生成所有3表JOIN
    // ...
    // level = N: 生成最终N表JOIN
    
    // 获取所有level-1的JOIN关系
    List* lower_rels = root->join_rel_level[level - 1];
    
    // 遍历所有可能的组合
    foreach(rel1, lower_rels) {
        RelOptInfo* rel1 = (RelOptInfo*) rel1;
        
        // 寻找能与rel1 JOIN的rel2
        // 要求: rel1和rel2没有重叠的relids
        foreach(rel2, lower_rels) {
            RelOptInfo* rel2 = (RelOptInfo*) rel2;
            
            // 检查是否可以JOIN
            if (!bms_overlap(rel1->relids, rel2->relids)) {
                // 检查是否有JOIN条件
                bool have_join = have_join_order_restrictions(
                    root, rel1->relids, rel2->relids
                );
                
                if (have_join) {
                    // 尝试JOIN
                    RelOptInfo* joinrel = make_join_rel(
                        root, rel1, rel2
                    );
                    
                    if (joinrel) {
                        joinrels = lappend(joinrels, joinrel);
                    }
                }
            }
        }
    }
    
    // 将生成的JOIN关系添加到当前level
    root->join_rel_level[level] = joinrels;
}
```

### 15.2 join_search_one_level函数

**源码分析**:

```cpp
// src/gausskernel/optimizer/path/joinrels.cpp
// 函数: join_search_one_level
// 作用: 在一个level内搜索所有可能的JOIN

static void join_search_one_level(PlannerInfo* root, int level)
{
    List* joinrels = NIL;
    Relids empty_relids;
    
    // 获取之前的level
    List* previous_level = root->join_rel_level[level - 1];
    
    // 策略1: JOIN两个level-1的关系 (1 + (level-1))
    foreach(rel1, previous_level) {
        RelOptInfo* old_rel = (RelOptInfo*) rel1;
        
        // 尝试与每个基础表JOIN
        for (int i = 0; i < root->simple_rel_array_size; i++) {
            RelOptInfo* new_rel = root->simple_rel_array[i];
            
            if (new_rel == NULL) continue;
            
            // 检查是否可以JOIN
            if (!bms_is_member(i, old_rel->relids)) {
                // 尝试 old_rel × new_rel
                RelOptInfo* joinrel = make_join_rel(
                    root, old_rel, new_rel
                );
                
                if (joinrel) {
                    joinrels = lappend(joinrels, joinrel);
                }
            }
        }
    }
    
    // 策略2: JOIN两个较小的关系 (k + (level-k))
    // 注意: 避免重复计算 (A×B) 和 (B×A)
    for (int k = 2; k <= level / 2; k++) {
        List* level_k = root->join_rel_level[k];
        List* level_level_minus_k = root->join_rel_level[level - k];
        
        foreach(rel1, level_k) {
            RelOptInfo* rel1 = (RelOptInfo*) rel1;
            
            foreach(rel2, level_level_minus_k) {
                RelOptInfo* rel2 = (RelOptInfo*) rel2;
                
                // 确保rel1和rel2没有重叠
                if (bms_overlap(rel1->relids, rel2->relids)) {
                    continue;
                }
                
                // 只枚举 rel1 < rel2 的情况
                if (bms_compare_memberships(rel1->relids, rel2->relids) >= 0) {
                    continue;
                }
                
                // 尝试JOIN
                RelOptInfo* joinrel = make_join_rel(
                    root, rel1, rel2
                );
                
                if (joinrel) {
                    joinrels = lappend(joinrels, joinrel);
                }
            }
        }
    }
    
    // 去重并保存
    root->join_rel_level[level] = joinrels;
}
```

### 15.3 make_join_rel函数

**源码分析**:

```cpp
// src/gausskernel/optimizer/path/joinrels.cpp
// 函数: make_join_rel
// 作用: 创建一个JOIN关系

RelOptInfo* make_join_rel(
    PlannerInfo* root,
    RelOptInfo* rel1,
    RelOptInfo* rel2)
{
    RelOptInfo* joinrel;
    List* restrictlist = NIL;
    JoinType jointype;
    SpecialJoinInfo sjinfo;

    // 步骤1: 构造JOIN结果的relids (两个输入relids的并集)
    // 这组relids就是JOIN结果在搜索空间中的"坐标"
    Relids joinrelids = bms_union(rel1->relids, rel2->relids);

    // 步骤2: 检查这个JOIN是否合法
    // 外连接/半连接的关联顺序有语义限制
    // (例如LEFT JOIN的右边不能先与无关表JOIN)
    // 不合法直接返回NULL, 等价于剪掉这个搜索分支
    if (!join_is_legal(root, rel1, rel2, joinrelids, &sjinfo, ...))
        return NULL;

    // 步骤3: 普通内连接在join_info_list里找不到对应的SpecialJoinInfo
    // 现场构造一个, 让选择率估计函数知道当前在评估什么样的JOIN
    if (sjinfo == NULL) {
        sjinfo.min_lefthand = rel1->relids;
        sjinfo.min_righthand = rel2->relids;
        sjinfo.jointype = JOIN_INNER;
    }

    // 步骤4: 查找或创建JOIN结果的RelOptInfo
    // 同一组relids的JOIN结果只建一份:
    // (A×B)×C 与 A×(B×C) 共用 {A,B,C} 的RelOptInfo
    // 同时算出这对输入对应的restrictlist (连接条件列表)
    joinrel = build_join_rel(root, joinrelids, rel1, rel2, &sjinfo, &restrictlist);

    // 如果已经证明这个JOIN结果为空, 不再生成路径
    if (is_dummy_rel(joinrel))
        return joinrel;

    // 步骤5: 生成JOIN路径 —— 双向都尝试!
    // rel1做外表、rel2做内表一次:
    add_paths_to_joinrel(root, joinrel, rel1, rel2, jointype, &sjinfo, restrictlist);
    // rel2做外表、rel1做内表再一次:
    add_paths_to_joinrel(root, joinrel, rel2, rel1, jointype, &sjinfo, restrictlist);

    return joinrel;
}
```

**make_join_rel的关键设计**:

1. **relids并集定位**: 搜索空间以relids集合为坐标, 不论按什么分组顺序到达 {A,B,C}, build_join_rel 都命中同一个RelOptInfo, 这是动态规划去重的基础。
2. **join_is_legal挡非法顺序** (joinrels.cpp / join_is_legal): 外连接的关联顺序有语义约束, 违反的直接剪掉。
3. **双向尝试**: 内连接没有方向语义, 内外表两个方向各调一次 add_paths_to_joinrel; 外连接按类型分派, JOIN_LEFT 会同时尝试 JOIN_RIGHT 方向 (openGauss/VastBase还有 JOIN_LEFT_ANTI_FULL / JOIN_RIGHT_ANTI_FULL 扩展类型)。
4. **add_paths_to_joinrel** (joinpath.cpp) 内部生成 NestLoop / MergeJoin / HashJoin 三族路径, 每条都经 add_path 参与 pathlist 竞争 (竞争规则见第05篇29.3节)。

---

## 18. GEQO遗传算法

### 18.1 GEQO的触发条件

```sql
-- 连接搜索算法的切换点在 allpaths.cpp / make_rel_from_joinlist:
--   连接项数 levels_needed >= geqo_threshold 且 enable_geqo = on
--     → geqo()                 遗传算法
--   否则
--     → standard_join_search() 纯动态规划

-- 相关参数 (注册于 guc/guc_sql.cpp):
SHOW enable_geqo;           -- 默认 on
SHOW geqo_threshold;        -- 默认 12: FROM项达到12个才切GEQO
SHOW geqo_effort;           -- 默认 5 (1~10), 搜索投入
SHOW geqo_pool_size;        -- 默认 0 = 自动
SHOW geqo_generations;      -- 默认 0 = 自动
SHOW geqo_selection_bias;   -- 默认 2.0 (1.5~2.0), 选择压力
SHOW geqo_seed;             -- 默认 0.0 (0~1), 随机种子

-- 观察切换: 构造12表与13表的JOIN对比搜索行为
-- 注意: GEQO下计划可能每次不同 (默认seed=0, 每次随机初始化)
```

**GEQO参数一览**:

| 参数 | 默认值 | 含义 |
|------|--------|------|
| enable_geqo | on | 是否启用GEQO |
| geqo_threshold | 12 | FROM项数达到该值才用GEQO |
| geqo_effort | 5 | 1~10, 越大种群越大、代数越多 |
| geqo_pool_size | 0 (自动) | 种群规模, 自动=2^(n+1)并按effort钳制 |
| geqo_generations | 0 (自动) | 迭代代数, 自动=种群规模 |
| geqo_selection_bias | 2.0 | 线性选择偏置 |
| geqo_seed | 0.0 | 随机种子, 固定值可复现 |

### 18.2 遗传算法主循环

**源码分析**:

```cpp
// src/gausskernel/optimizer/geqo/geqo_main.cpp
// 函数: geqo_main
// 一条"染色体" = n张表的一个排列 (Gene序列), 即一个候选连接顺序

RelOptInfo* geqo_main(PlannerInfo* root, int number_of_rels, List* initial_rels)
{
    // 1. 初始化随机数发生器 (geqo_seed)
    geqo_set_seed(root, Geqo_seed);

    // 2. 推算种群规模与迭代代数
    pool_size = gimme_pool_size(number_of_rels);
    //   自动值 = 2^(n+1), 但按effort钳制:
    //   下限 = 10 × effort, 上限 = 50 × effort (effort=5时即50~250)
    number_generations = gimme_number_generations(pool_size);
    //   自动值 = pool_size (保证劣质个体在结束前被挤出去)

    // 3. 随机初始化种群, 按适应度排序
    random_init_pool(root, pool);   // 每条染色体的适应度都调geqo_eval
    sort_pool(root, pool);

    // 4. 进化主循环
    for (generation = 0; generation < number_generations; generation++) {
        // 选择: 线性偏置法选出一对父母 (Geqo_selection_bias)
        geqo_selection(root, momma, daddy, pool, Geqo_selection_bias);

        // 交叉: 默认编译开关走ERX边缘重组交叉 (geqo_erx.cpp)
        // ERX专为"排列"型染色体设计, 尽量保留父母的邻接关系
        gimme_edge_table(root, momma->string, daddy->string, ...);
        gimme_tour(root, edge_table, kid->string, ...);

        // 适应度 = 按kid的连接顺序能构造出的最优计划的总代价
        kid->worth = geqo_eval(root, kid->string, pool->string_length);

        // 淘汰: kid挤进种群, 排名最差的个体被挤出
        spread_chromo(root, kid, pool);
    }

    // 5. 用最优染色体 (种群第1名) 重建连接树返回
    best_tour = pool->data[0].string;
    return gimme_tree(root, best_tour, pool->string_length);
}
```

### 18.3 geqo_eval与gimme_tree: 染色体如何变成计划

**geqo_eval** (geqo_eval.cpp) 做两件事:

```cpp
// src/gausskernel/optimizer/geqo/geqo_eval.cpp
// 函数: geqo_eval

Cost geqo_eval(PlannerInfo* root, Gene* tour, int num_gene)
{
    // 评估会在root->join_rel_list里堆积大量中间JOIN结果
    // 先记下原长度, 评估完截断回去, 下次评估从干净状态开始
    savelength = list_length(root->join_rel_list);

    // 按tour指定的顺序构造连接树
    joinrel = gimme_tree(root, tour, num_gene);

    // 适应度 = 这棵连接树的最优路径总代价
    fitness = joinrel->cheapest_total_path->total_cost;

    // 还原join_rel_list与join_rel_hash, 丢弃中间结果
    root->join_rel_list = list_truncate(root->join_rel_list, savelength);
    return fitness;
}
```

**gimme_tree** 的关键: tour只是"指导", 不是死顺序:

```text
把tour里的表逐个合并成"clump"(已连接的块):
- 新表优先并入已有的clump, 且只做"理想"的JOIN
- 理想JOIN的判据 desirable_join (geqo_eval.cpp):
  有可用连接条件(have_relevant_joinclause), 或有连接顺序约束(have_join_order_restriction)
- 合不进的先自成一块, tour扫完后强制(force)合并剩余块

这样设计的原因: 按tour硬性左深连接无法表达bushy计划,
而某些外连接场景下唯一合法的计划恰恰是bushy的
```

### 18.4 GEQO与动态规划对比

| 维度 | 动态规划 (standard_join_search) | GEQO (geqo_main) |
|------|--------------------------------|------------------|
| 触发条件 | 连接项数 < geqo_threshold (12) | 连接项数 >= 12 且 enable_geqo=on |
| 搜索空间 | 枚举全部合法的子集划分(含bushy) | 每条染色体一条连接顺序 |
| 最优性 | 代价模型准确时全局最优 | 不保证最优 |
| 计算量 | 随表数指数增长, 20表以上不可行 | 固定: 种群规模×代数×单次评估 |
| 可复现性 | 确定 | 受geqo_seed影响, 默认每次随机 |
| 中间结果 | 全部保留在join_rel_level[] | 评估完即丢弃, 只留最终树 |

**现场判断**:

```sql
-- 场景1: 表数略超阈值但结构简单, 想要更优计划
-- 调大阈值继续用DP
SET geqo_threshold = 20;

-- 场景2: 计划抖动排查
-- 先数FROM/JOIN项数是否踩过了geqo_threshold
-- GEQO的随机初始化是计划抖动的常见来源
SELECT ... FROM t1 JOIN t2 ON ... JOIN t13 ON ...;  -- 13项, 已进GEQO

-- 场景3: 需要复现某个GEQO计划
SET geqo_seed = 0.5;  -- 固定种子后在同一数据集上重放
```

---

## 总结

- JOIN顺序搜索 = 对2^n个relids子集做动态规划: standard_join_search (allpaths.cpp) 逐层调join_search_one_level, 第k层拼出所有k表JOIN。
- make_join_rel是搜索单元: relids并集定位 → join_is_legal合法性检查 → build_join_rel复用同集合结果 → 双向add_paths_to_joinrel生成三族JOIN路径。
- 内连接无方向语义, 内外表两个方向都要尝试; 外连接语义由SpecialJoinInfo携带, 非法顺序被join_is_legal剪掉。
- 连接项数 >= geqo_threshold (默认12) 时切换GEQO: 用固定的"种群×代数"预算替换指数级枚举。
- GEQO染色体=表排列; 适应度=geqo_eval按该顺序建树的最优总代价; 默认ERX边缘重组交叉; spread_chromo淘汰最差个体。
- gimme_tree把tour当指导而非死顺序: desirable_join启发式推迟无连接条件的JOIN, 必要时产生bushy计划; 每次评估后截断join_rel_list丢弃中间结果。
- DP确定且全局最优但指数复杂度; GEQO可控但不保证最优且默认随机 — 多表查询计划抖动时先查是否踩进GEQO。
- join_collapse_limit / from_collapse_limit (默认8) 决定显式JOIN构造与子查询是否被拉平参与全局顺序搜索 (对应目录第16/17节, 原文截断未及展开, 参数注册于guc/guc_sql.cpp)。

## 版本注记

- 2026-11-05 断尾补齐: 本篇原文在500行处截断(截于15.3节make_join_rel代码块中间), 本次顺接补完该代码块并收尾15.3节, 按目录中已有的第18节标题补写"GEQO遗传算法"一节(含GEQO与DP对比)。
- 原目录第16节(join_collapse_limit)、第17节(from_collapse_limit)、第19节(JOIN顺序优化实战)的原文未随截断保留, 仅在总结中带过其要点, 不虚构重建。

## 来源

- 源码基线: openGauss-server-v6.0.0。本篇引用: src/gausskernel/optimizer/geqo/geqo_main.cpp(geqo_main / gimme_pool_size / gimme_number_generations / random_init_pool / sort_pool / geqo_selection / spread_chromo), src/gausskernel/optimizer/geqo/geqo_eval.cpp(geqo_eval / gimme_tree / merge_clump / desirable_join), src/gausskernel/optimizer/path/joinrels.cpp(join_search_one_level / make_join_rel / join_is_legal), src/gausskernel/optimizer/path/allpaths.cpp(make_rel_from_joinlist / standard_join_search), src/gausskernel/optimizer/path/joinpath.cpp(add_paths_to_joinrel), src/common/backend/utils/misc/guc/guc_sql.cpp(GEQO参数与collapse_limit参数注册), src/include/optimizer/geqo.h(effort/bias常量)。
- 本篇原文: 2026-04 学习档案(VastBase/openGauss JOIN顺序评估与GEQO超详细深度分析)。

