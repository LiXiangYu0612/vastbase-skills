     1|# VastBase/openGauss JOIN顺序评估与GEQO超详细深度分析
     2|
     3|> 源码版本: openGauss-server-v6.0.0  
     4|> 分析文件: allpaths.cpp, joinrels.cpp, geqo_*.cpp (30+文件)  
     5|> 学习目标: 理解JOIN顺序评估算法、GEQO遗传算法、参数调优
     6|
     7|---
     8|
     9|## 目录
    10|
    11|### 第三部分: JOIN顺序评估
    12|- [14. JOIN顺序评估基础](#14-join顺序评估基础)
    13|- [15. 动态规划算法](#15-动态规划算法)
    14|- [16. join_collapse_limit参数](#16-join_collapse_limit参数)
    15|- [17. from_collapse_limit参数](#17-from_collapse_limit参数)
    16|- [18. GEQO遗传算法](#18-geqo遗传算法)
    17|- [19. JOIN顺序优化实战](#19-join顺序优化实战)
    18|
    19|---
    20|
    21|## 14. JOIN顺序评估基础
    22|
    23|### 14.1 为什么JOIN顺序很重要
    24|
    25|**JOIN顺序对性能的影响**:
    26|
    27|```sql
    28|-- 创建测试场景
    29|CREATE TABLE join_order_test (
    30|    id INT PRIMARY KEY,
    31|    value INT
    32|);
    33|
    34|-- 大表
    35|CREATE TABLE large_table AS
    36|SELECT generate_series(1, 100000) AS id,
    37|       (random() * 1000)::int AS value;
    38|
    39|-- 中表
    40|CREATE TABLE medium_table AS
    41|SELECT generate_series(1, 10000) AS id,
    42|       (random() * 1000)::int AS value;
    43|
    44|-- 小表
    45|CREATE TABLE small_table AS
    46|SELECT generate_series(1, 1000) AS id,
    47|       (random() * 1000)::int AS value;
    48|
    49|ANALYZE large_table, medium_table, small_table;
    50|
    51|-- 查询1: 小表在前 (好的JOIN顺序)
    52|EXPLAIN
    53|SELECT COUNT(*)
    54|FROM small_table s
    55|JOIN medium_table m ON s.id = m.id
    56|JOIN large_table l ON m.id = l.id;
    57|
    58|-- 查询2: 大表在前 (坏的JOIN顺序)
    59|-- 使用CTE强制JOIN顺序
    60|WITH joined AS (
    61|    SELECT l.*, m.*
    62|    FROM large_table l
    63|    JOIN medium_table m ON l.id = m.id
    64|)
    65|SELECT COUNT(*)
    66|FROM joined j
    67|JOIN small_table s ON j.id = s.id;
    68|
    69|-- 对比Cost:
    70|-- 查询1 (小→中→大): Cost ≈ 1000 (低)
    71|-- 查询2 (大→中→小): Cost ≈ 100000 (高)
    72|```
    73|
    74|**JOIN顺序选择原理**:
    75|
    76|```cpp
    77|// JOIN顺序选择的目标:
    78|// min_cost = min(所有可能的JOIN顺序的cost)
    79|
    80|// 示例: 3个表JOIN
    81|// 可能的顺序: 3! = 6种
    82|// 1. A × B × C
    83|// 2. A × C × B
    84|// 3. B × A × C
    85|// 4. B × C × A
    86|// 5. C × A × B
    87|// 6. C × B × A
    88|
    89|// 每种顺序的成本不同
    90|// 优化器需要找到成本最低的顺序
    91|```
    92|
    93|### 14.2 JOIN顺序评估的数据结构
    94|
    95|**RelOptInfo结构**:
    96|
    97|```cpp
    98|// src/include/nodes/relation.h
    99|// RelOptInfo: 表示一个关系(表或JOIN结果)
   100|
   101|typedef struct RelOptInfo
   102|{
   103|    NodeTag     type;
   104|    
   105|    // 基本属性
   106|    Index       relid;          /* Relation ID (如果是基础表) */
   107|    Oid         reltablespace;  /* 表空间OID */
   108|    AttrNumber  max_attr;       /* 最大属性号 */
   109|    
   110|    // 统计信息
   111|    double      rows;           /* 估计行数 */
   112|    int         width;          /* 平均行宽(字节) */
   113|    BlockNumber pages;          /* 磁盘页数 */
   114|    
   115|    // Cost信息
   116|    Cost        cheapest_startup_cost;   /* 最低启动成本 */
   117|    Cost        cheapest_total_cost;     /* 最低总成本 */
   118|    
   119|    // 路径信息
   120|    List*       pathlist;       /* Path结构列表 */
   121|    Path*       cheapest_startup_path;   /* 最低启动成本路径 */
   122|    Path*       cheapest_total_path;     /* 最低总成本路径 */
   123|    
   124|    // JOIN信息
   125|    Relids      relids;         /* 包含的RelOptInfo ID集合 */
   126|    List*       joininfo;       /* JOIN条件信息 */
   127|    List*       restrictinfo;   /* 限制条件信息 */
   128|    
   129|    // 其他信息
   130|    bool        consider_startup;   /* 是否考虑启动成本 */
   131|    bool        consider_param;     /* 是否考虑参数化路径 */
   132|    
   133|} RelOptInfo;
   134|
   135|// 示例: 表示表A
   136|RelOptInfo* relA = makeNode(RelOptInfo);
   137|relA->relid = 1;           /* 表A的OID */
   138|relA->rows = 10000;        /* 估计10000行 */
   139|relA->pages = 100;         /* 占用100页 */
   140|relA->width = 100;         /* 平均行宽100字节 */
   141|
   142|// 示例: 表示 A JOIN B
   143|RelOptInfo* relAB = makeNode(ReloptInfo);
   144|relAB->relids = bms_union(relA->relids, relB->relids);  /* {1, 2} */
   145|relAB->rows = 1000;        /* JOIN后估计1000行 */
   146|relAB->cheapest_total_cost = 150.0;  /* 最低成本150 */
   147|```
   148|
   149|**JoinRelInfo结构**:
   150|
   151|```cpp
   152|// src/include/nodes/relation.h
   153|// JoinRelInfo: 表示JOIN关系的信息
   154|
   155|typedef struct JoinRelInfo
   156|{
   157|    NodeTag     type;
   158|    
   159|    Relids      join_relids;    /* JOIN后的Relids */
   160|    
   161|    RelOptInfo* jointype;       /* JOIN类型 */
   162|    
   163|    RelOptInfo* outer_rel;      /* 外表RelOptInfo */
   164|    RelOptInfo* inner_rel;      /* 内表RelOptInfo */
   165|    
   166|    List*       joinclauses;    /* JOIN条件列表 */
   167|    List*       otherclauses;   /* 其他条件列表 */
   168|    
   169|} JoinRelInfo;
   170|
   171|// 示例: A JOIN B
   172|JoinRelInfo* joinAB = makeNode(JoinRelInfo);
   173|joinAB->join_relids = bms_union(relA->relids, relB->relids);
   174|joinAB->jointype = JOIN_INNER;
   175|joinAB->outer_rel = relA;
   176|joinAB->inner_rel = relB;
   177|joinAB->joinclauses = list_make1(equal_expr);
   178|```
   179|
   180|### 14.3 JOIN顺序枚举算法
   181|
   182|**暴力枚举 (Brute Force)**:
   183|
   184|```cpp
   185|// 对于N个表，枚举所有可能的JOIN顺序
   186|// 时间复杂度: O(N!)
   187|
   188|void enumerate_join_orders_bruteforce(List* rels, int n)
   189|{
   190|    if (n == 1) {
   191|        // 只有一个表，直接返回
   192|        evaluate_cost(rels);
   193|        return;
   194|    }
   195|    
   196|    // 尝试每个表作为第一个表
   197|    for (int i = 0; i < n; i++) {
   198|        // 选择表i作为第一个表
   199|        RelOptInfo* first_rel = list_nth(rels, i);
   200|        List* remaining_rels = list_delete_ptr(rels, first_rel);
   201|        
   202|        // 递归枚举剩余的表
   203|        enumerate_join_orders_bruteforce(remaining_rels, n - 1);
   204|    }
   205|}
   206|
   207|// 问题: 对于10个表，10! = 3,628,800 种顺序
   208|//      优化器无法在合理时间内枚举完所有顺序
   209|```
   210|
   211|**动态规划 (Dynamic Programming)**:
   212|
   213|```cpp
   214|// 使用动态规划避免重复计算
   215|// 时间复杂度: O(N^2 * 2^N)
   216|
   217|// dp[S] = 表集合S的最优JOIN成本
   218|// dp[S] = min(dp[T] + dp[S-T] + cost(T × (S-T)))
   219|
   220|#define MAX_RELATIONS 32
   221|
   222|double dp[1 << MAX_RELATIONS];  // dp[mask] = 该mask的最优成本
   223|Path* best_path[1 << MAX_RELATIONS];
   224|
   225|void dp_join_ordering(List* rels)
   226|{
   227|    int n = list_length(rels);
   228|    RelOptInfo** rel_array = (RelOptInfo**) palloc(n * sizeof(RelOptInfo*));
   229|    
   230|    // 初始化: 将rels转换为数组
   231|    int i = 0;
   232|    foreach(rel, rels) {
   233|        rel_array[i++] = (RelOptInfo*) rel;
   234|    }
   235|    
   236|    // 初始化单个表的成本
   237|    for (i = 0; i < n; i++) {
   238|        int mask = 1 << i;
   239|        dp[mask] = 0;  // 单表没有JOIN成本
   240|        best_path[mask] = rel_array[i]->cheapest_total_path;
   241|    }
   242|    
   243|    // 枚举所有子集
   244|    for (int mask = 1; mask < (1 << n); mask++) {
   245|        if (power_of_two(mask)) continue;  // 跳过单表
   246|        
   247|        dp[mask] = INFINITY;
   248|        
   249|        // 尝试将mask分成两个子集 left | right = mask
   250|        for (int left = (mask - 1) & mask; left > 0; left = (left - 1) & mask) {
   251|            int right = mask ^ left;
   252|            
   253|            // 只枚举 left < right 的情况，避免重复
   254|            if (left >= right) continue;
   255|            
   256|            // 尝试所有JOIN顺序
   257|            double cost_lr = dp[left] + dp[right] + 
   258|                            estimate_join_cost(left, right);
   259|            double cost_rl = dp[right] + dp[left] + 
   260|                            estimate_join_cost(right, left);
   261|            
   262|            if (cost_lr < dp[mask]) {
   263|                dp[mask] = cost_lr;
   264|                best_path[mask] = make_join_path(
   265|                    best_path[left], best_path[right], JOIN_LEFT
   266|                );
   267|            }
   268|            
   269|            if (cost_rl < dp[mask]) {
   270|                dp[mask] = cost_rl;
   271|                best_path[mask] = make_join_path(
   272|                    best_path[right], best_path[left], JOIN_RIGHT
   273|                );
   274|            }
   275|        }
   276|    }
   277|    
   278|    // 最终结果: dp[(1 << n) - 1] 是最优成本
   279|    // best_path[(1 << n) - 1] 是最优路径
   280|}
   281|
   282|// 时间复杂度分析:
   283|// - 外层循环: 2^n 个mask
   284|// - 内层循环: 平均 2^(n-1) 次分割
   285|// - 总时间: O(2^n * 2^(n-1)) = O(4^n)
   286|//
   287|// 优化:
   288|// - 只枚举包含最低位元的left子集
   289|// - 时间复杂度降至: O(n^2 * 2^n)
   290|//
   291|// 实际应用:
   292|// - n = 10: 10^2 * 2^10 ≈ 10,000 次计算 (可行)
   293|// - n = 20: 20^2 * 2^20 ≈ 400,000,000 次计算 (太慢)
   294|```
   295|
   296|**贪心算法 (Greedy)**:
   297|
   298|```cpp
   299|// 贪心算法: 每次选择成本最低的JOIN
   300|// 时间复杂度: O(N^2)
   301|
   302|void greedy_join_ordering(List* rels)
   303|{
   304|    int n = list_length(rels);
   305|    List* joined_rels = NIL;
   306|    List* remaining_rels = list_copy(rels);
   307|    
   308|    // 选择最小的表作为起点
   309|    RelOptInfo* smallest_rel = find_smallest_rel(remaining_rels);
   310|    joined_rels = lappend(joined_rels, smallest_rel);
   311|    remaining_rels = list_delete_ptr(remaining_rels, smallest_rel);
   312|    
   313|    // 贪心地选择下一个表
   314|    while (remaining_rels != NIL) {
   315|        RelOptInfo* best_rel = NULL;
   316|        Cost best_cost = INFINITY;
   317|        
   318|        // 尝试每个剩余的表
   319|        foreach(rel, remaining_rels) {
   320|            // 计算将该表JOIN到已JOIN集合的成本
   321|            Cost cost = estimate_join_cost(joined_rels, rel);
   322|            
   323|            if (cost < best_cost) {
   324|                best_cost = cost;
   325|                best_rel = (RelOptInfo*) rel;
   326|            }
   327|        }
   328|        
   329|        // 选择成本最低的表
   330|        joined_rels = lappend(joined_rels, best_rel);
   331|        remaining_rels = list_delete_ptr(remaining_rels, best_rel);
   332|    }
   333|    
   334|    // 最终结果: joined_rels 包含所有表，顺序由贪心策略决定
   335|}
   336|
   337|// 时间复杂度: O(N^2)
   338|// 优点: 速度快
   339|// 缺点: 不保证全局最优
   340|```
   341|
   342|---
   343|
   344|## 15. 动态规划算法
   345|
   346|### 15.1 make_rel_from_joinrels函数
   347|
   348|**源码分析**:
   349|
   350|```cpp
   351|// src/gausskernel/optimizer/path/allpaths.cpp
   352|// 函数: make_rel_from_joinrels
   353|// 作用: 使用动态规划生成所有可能的JOIN关系
   354|
   355|static void make_rel_from_joinrels(PlannerInfo* root, int level)
   356|{
   357|    List* joinrels = NIL;
   358|    
   359|    // level表示当前JOIN的表数量
   360|    // level = 2: 生成所有2表JOIN
   361|    // level = 3: 生成所有3表JOIN
   362|    // ...
   363|    // level = N: 生成最终N表JOIN
   364|    
   365|    // 获取所有level-1的JOIN关系
   366|    List* lower_rels = root->join_rel_level[level - 1];
   367|    
   368|    // 遍历所有可能的组合
   369|    foreach(rel1, lower_rels) {
   370|        RelOptInfo* rel1 = (RelOptInfo*) rel1;
   371|        
   372|        // 寻找能与rel1 JOIN的rel2
   373|        // 要求: rel1和rel2没有重叠的relids
   374|        foreach(rel2, lower_rels) {
   375|            RelOptInfo* rel2 = (RelOptInfo*) rel2;
   376|            
   377|            // 检查是否可以JOIN
   378|            if (!bms_overlap(rel1->relids, rel2->relids)) {
   379|                // 检查是否有JOIN条件
   380|                bool have_join = have_join_order_restrictions(
   381|                    root, rel1->relids, rel2->relids
   382|                );
   383|                
   384|                if (have_join) {
   385|                    // 尝试JOIN
   386|                    RelOptInfo* joinrel = make_join_rel(
   387|                        root, rel1, rel2
   388|                    );
   389|                    
   390|                    if (joinrel) {
   391|                        joinrels = lappend(joinrels, joinrel);
   392|                    }
   393|                }
   394|            }
   395|        }
   396|    }
   397|    
   398|    // 将生成的JOIN关系添加到当前level
   399|    root->join_rel_level[level] = joinrels;
   400|}
   401|```
   402|
   403|### 15.2 join_search_one_level函数
   404|
   405|**源码分析**:
   406|
   407|```cpp
   408|// src/gausskernel/optimizer/path/joinrels.cpp
   409|// 函数: join_search_one_level
   410|// 作用: 在一个level内搜索所有可能的JOIN
   411|
   412|static void join_search_one_level(PlannerInfo* root, int level)
   413|{
   414|    List* joinrels = NIL;
   415|    Relids empty_relids;
   416|    
   417|    // 获取之前的level
   418|    List* previous_level = root->join_rel_level[level - 1];
   419|    
   420|    // 策略1: JOIN两个level-1的关系 (1 + (level-1))
   421|    foreach(rel1, previous_level) {
   422|        RelOptInfo* old_rel = (RelOptInfo*) rel1;
   423|        
   424|        // 尝试与每个基础表JOIN
   425|        for (int i = 0; i < root->simple_rel_array_size; i++) {
   426|            RelOptInfo* new_rel = root->simple_rel_array[i];
   427|            
   428|            if (new_rel == NULL) continue;
   429|            
   430|            // 检查是否可以JOIN
   431|            if (!bms_is_member(i, old_rel->relids)) {
   432|                // 尝试 old_rel × new_rel
   433|                RelOptInfo* joinrel = make_join_rel(
   434|                    root, old_rel, new_rel
   435|                );
   436|                
   437|                if (joinrel) {
   438|                    joinrels = lappend(joinrels, joinrel);
   439|                }
   440|            }
   441|        }
   442|    }
   443|    
   444|    // 策略2: JOIN两个较小的关系 (k + (level-k))
   445|    // 注意: 避免重复计算 (A×B) 和 (B×A)
   446|    for (int k = 2; k <= level / 2; k++) {
   447|        List* level_k = root->join_rel_level[k];
   448|        List* level_level_minus_k = root->join_rel_level[level - k];
   449|        
   450|        foreach(rel1, level_k) {
   451|            RelOptInfo* rel1 = (RelOptInfo*) rel1;
   452|            
   453|            foreach(rel2, level_level_minus_k) {
   454|                RelOptInfo* rel2 = (RelOptInfo*) rel2;
   455|                
   456|                // 确保rel1和rel2没有重叠
   457|                if (bms_overlap(rel1->relids, rel2->relids)) {
   458|                    continue;
   459|                }
   460|                
   461|                // 只枚举 rel1 < rel2 的情况
   462|                if (bms_compare_memberships(rel1->relids, rel2->relids) >= 0) {
   463|                    continue;
   464|                }
   465|                
   466|                // 尝试JOIN
   467|                RelOptInfo* joinrel = make_join_rel(
   468|                    root, rel1, rel2
   469|                );
   470|                
   471|                if (joinrel) {
   472|                    joinrels = lappend(joinrels, joinrel);
   473|                }
   474|            }
   475|        }
   476|    }
   477|    
   478|    // 去重并保存
   479|    root->join_rel_level[level] = joinrels;
   480|}
   481|```
   482|
   483|### 15.3 make_join_rel函数
   484|
   485|**源码分析**:
   486|
   487|```cpp
   488|// src/gausskernel/optimizer/path/joinrels.cpp
   489|// 函数: make_join_rel
   490|// 作用: 创建一个JOIN关系
   491|
   492|RelOptInfo* make_join_rel(
   493|    PlannerInfo* root,
   494|    RelOptInfo* rel1,
   495|    RelOptInfo* rel2)
   496|{
   497|    RelOptInfo* joinrel;
   498|    List* restrictlist = NIL;
   499|    JoinType jointype;
   500|    SpecialJoinInfo sjinfo;
   501|

---
> ⚓ **截断注记（2026-11-05 审计）**：本篇在历史迁移时截断于 500 行，尾部
> 小节缺失（原全量件已不可寻，待重写补齐——挂账 ROADMAP）；行号式清单为
> 源码摘录，无独立 file:line 锚。
