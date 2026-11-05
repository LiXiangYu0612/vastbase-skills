# VastBase/openGauss 优化器高级特性深度分析 - SMP限制、Hint使用、Rewrite Rule

> 源码版本: openGauss-server-v6.0.0  
> 学习目标: 掌握SMP限制条件、Hint使用技能、Rewrite Rule配置

---

## 目录

### 第一部分: SMP (Symmetric Multi-Processing) 并行执行限制条件
- [1. SMP架构概述](#1-smp架构概述)
- [2. SMP启用条件](#2-smp启用条件)
- [3. SMP禁用场景](#3-smp禁用场景)
- [4. SMP限制条件完整列表](#4-smp限制条件完整列表)

### 第二部分: Hint (查询提示) 使用指南
- [5. Hint系统架构](#5-hint系统架构)
- [6. Hint类型详解](#6-hint类型详解)
- [7. Hint实战案例: SMP计划下的Hint生效语义与验证](#7-hint实战案例-smp计划下的hint生效语义与验证)

### 第三部分: Rewrite Rule (查询重写规则)
*(原文8~10节在500行截断处丢失,未单独重建;改写规则的GUC位表/REWRITE约束Hint/双门验证方法已并入第7节7.5)*

---

## 第一部分: SMP (Symmetric Multi-Processing) 并行执行限制条件

### 1. SMP架构概述

**SMP定义**:
SMP (Symmetric Multi-Processing) 是openGauss的多线程并行执行框架,通过多个线程并行执行查询来提高性能。

**核心数据结构**:

```cpp
// src/gausskernel/optimizer/path/streampath.cpp
// SMP描述符

typedef struct ParallelDesc {
    int producerDop;       // 生产者并行度 (DOP: Degree of Parallelism)
    int consumerDop;       // 消费者并行度
    StreamType distriType; // 分布类型
} ParallelDesc;

// 分布类型
typedef enum StreamType {
    LOCAL_DISTRIBUTE,      // 本地分发 (同节点内线程间)
    LOCAL_BROADCAST,       // 本地广播
    LOCAL_ROUNDROBIN,      // 本地轮询
    REMOTE_DISTRIBUTE,     // 远程分发 (跨节点)
    REMOTE_BROADCAST,      // 远程广播
    REMOTE_SPLIT_DISTRIBUTE // 远程分割分发 (SMP专用)
} StreamType;

// SMP上下文
typedef struct {
    bool smp_enabled;      // SMP是否启用
    int query_dop;         // 查询并行度
    double smp_thread_cost; // SMP线程成本
} OptimizerContext;
```

**SMP执行流程**:

```
1. 查询解析与优化
   ↓
2. 检查SMP是否可用 (u_sess->opt_cxt.smp_enabled)
   ↓
3. 生成并行路径 (考虑SMP代价)
   ↓
4. 选择最优并行计划
   ↓
5. 创建执行计划 (包含Stream节点进行数据重分布)
   ↓
6. 执行器启动多个线程
   ↓
7. 线程间通过Stream节点交换数据
   ↓
8. 收集结果并返回
```

---

### 2. SMP启用条件

**全局配置参数**:

```sql
-- 1. 启用SMP (默认开启)
SET enable_smp = on;

-- 2. 设置查询并行度 (默认2)
SET query_dop = 4;

-- 3. 设置SMP线程数
SET smp_thread_count = 16;

-- 4. SMP表大小阈值
SET min_parallel_table_scan_size = '8MB';
SET min_parallel_index_scan_size = '512kB';

-- 5. 每个Gather的Worker数
SET max_parallel_workers_per_gather = 4;
```

**自动启用SMP的条件**:

```cpp
// src/gausskernel/optimizer/path/allpaths.cpp
// check_smp_available函数

bool check_smp_available(PlannerInfo* root, RelOptInfo* rel)
{
    // 1. 检查全局SMP开关
    if (!u_sess->attr.attr_sql.enable_smp) {
        return false;
    }
    
    // 2. 检查表大小
    if (rel->pages < min_parallel_table_scan_size) {
        return false;  // 表太小,不值得并行
    }
    
    // 3. 检查并行度
    if (u_sess->opt_cxt.query_dop <= 1) {
        return false;  // 并行度为1,相当于不并行
    }
    
    // 4. 检查可用Worker数
    if (get_available_smp_workers() < u_sess->opt_cxt.query_dop) {
        return false;  // 可用Worker不足
    }
    
    // 5. 检查是否在事务中
    if (IsTransactionBlock()) {
        return false;  // 事务块中禁用SMP
    }
    
    return true;
}
```

---

### 3. SMP禁用场景

**场景1: 事务中的查询**

```sql
BEGIN;
-- 以下查询不会使用SMP
SELECT * FROM large_table;
COMMIT;

-- 原因: 事务中需要保证事务隔离性和一致性,SMP线程间协调复杂
```

**场景2: INSERT/UPDATE/DELETE中的子查询**

```sql
-- 不使用SMP
INSERT INTO small_table
SELECT * FROM large_table;

-- 原因: DML操作的子查询禁用SMP,避免数据一致性问题
```

**场景3: 递归CTE (WITH RECURSIVE)**

```sql
-- 不使用SMP
WITH RECURSIVE cte AS (
    SELECT 1 AS n
    UNION ALL
    SELECT n + 1 FROM cte WHERE n < 100
)
SELECT * FROM cte;

-- 原因: 递归查询的迭代特性不适合并行
```

**场景4: 游标查询**

```sql
BEGIN;
DECLARE cur CURSOR FOR SELECT * FROM large_table;
FETCH 10 FROM cur;
COMMIT;

-- 原因: 游标需要保持状态,并行会破坏状态管理
```

**场景5: 带有volatile函数的查询**

```sql
-- 不使用SMP
SELECT *, random() FROM large_table;

-- 原因: volatile函数在并行环境下执行顺序不确定
```

---

### 4. SMP限制条件完整列表

#### 4.1 系统级限制

```sql
-- 1. SMP全局开关
-- 参数: enable_smp
-- 默认: on
-- 说明: 必须为on才能启用SMP
SHOW enable_smp;

-- 2. 查询并行度
-- 参数: query_dop
-- 默认: 1
-- 范围: 1-512
-- 说明: query_dop > 1 才会使用SMP
SHOW query_dop;

-- 3. 最大SMP线程数
-- 参数: max_smp_thread_count
-- 默认: 系统CPU核数
-- 说明: 系统级线程数上限
SHOW max_smp_thread_count;

-- 4. 每个查询的最大线程数
-- 参数: smp_thread_count
-- 默认: 4
-- 说明: 单个查询最多使用的线程数
SHOW smp_thread_count;

-- 5. SMP线程成本
-- 参数: smp_thread_cost
-- 默认: 0.05
-- 说明: 每个SMP线程的启动和通信成本
SHOW smp_thread_cost;
```

#### 4.2 表级限制

```sql
-- 1. 最小并行扫描表大小
-- 参数: min_parallel_table_scan_size
-- 默认: 8MB
-- 说明: 表大小 < 8MB 时不使用并行扫描
SHOW min_parallel_table_scan_size;

-- 示例:
-- 表large_table (100MB) → 启用SMP ✓
-- 表small_table (2MB)   → 禁用SMP ✗

-- 2. 最小并行索引扫描大小
-- 参数: min_parallel_index_scan_size
-- 默认: 512kB
-- 说明: 索引扫描大小 < 512kB 时不使用并行
SHOW min_parallel_index_scan_size;
```

#### 4.3 查询级限制

**限制1: 事务中的查询**

```sql
-- 限制条件
BEGIN;
SELECT * FROM large_table;  -- 不使用SMP
COMMIT;

-- 源码检查
// src/gausskernel/optimizer/plan/planner.cpp
if (IsTransactionBlock()) {
    u_sess->opt_cxt.smp_enabled = false;
}
```

**限制2: DML中的子查询**

```sql
-- 限制条件
INSERT INTO t1 SELECT * FROM large_table;  -- 子查询不使用SMP
UPDATE t1 SET col = (SELECT AVG(col) FROM large_table);
DELETE FROM t1 WHERE id IN (SELECT id FROM large_table);

-- 源码检查
// src/gausskernel/optimizer/commands/copy.cpp
bool old_smp_enabled = u_sess->opt_cxt.smp_enabled;
u_sess->opt_cxt.smp_enabled = false;  // 强制禁用
// ... 执行COPY/INSERT/UPDATE/DELETE
u_sess->opt_cxt.smp_enabled = old_smp_enabled;
```

**限制3: 递归查询**

```sql
-- 限制条件
WITH RECURSIVE cte AS (...)
SELECT * FROM cte;  -- 不使用SMP

-- 源码检查
// src/gausskernel/optimizer/prep/prepunion.cpp
// For distributed with-recursive processing we currently disable the SMP
if (is_recursive_union(query)) {
    u_sess->opt_cxt.smp_enabled = false;
}
```

**限制4: 游标查询**

```sql
-- 限制条件
DECLARE cur CURSOR FOR SELECT * FROM large_table;  -- 不使用SMP

-- 原因: 游标需要保持状态,并行会破坏状态管理
```

**限制5: 带有volatile函数的查询**

```sql
-- 限制条件
SELECT *, random(), now() FROM large_table;  -- 不使用SMP

-- 原因: volatile函数执行顺序不确定
```

**限制6: FOR UPDATE/SHARE查询**

```sql
-- 限制条件
SELECT * FROM large_table FOR UPDATE;  -- 不使用SMP

-- 原因: 行锁需要顺序获取
```

#### 4.4 节点级限制

**限制1: Stream节点数量**

```sql
-- 限制条件: Stream节点过多时禁用SMP
-- 参数: max_stream_num
-- 默认: 100
-- 说明: 超过阈值时禁用SMP,避免计划过于复杂

-- 检查代码
// src/gausskernel/optimizer/plan/streamplan_utils.cpp
if (stream_node_count > max_stream_num) {
    u_sess->opt_cxt.smp_enabled = false;
}
```

**限制2: JOIN数量**

```sql
-- 限制条件: JOIN过多时禁用SMP
-- 参数: max_join_num
-- 默认: 无限制
-- 说明: 复杂多表JOIN可能禁用SMP

-- 检查代码
// 实际上openGauss没有这个硬限制,但复杂的JOIN可能导致Stream节点过多
```

#### 4.5 资源限制

**限制1: 内存不足**

```sql
-- 限制条件: work_mem不足
-- 说明: 并行执行需要更多内存,work_mem不足时禁用SMP

-- 检查逻辑
if (estimated_memory_per_thread * query_dop > work_mem) {
    u_sess->opt_cxt.smp_enabled = false;
}
```

**限制2: CPU核心数不足**

```sql
-- 限制条件: 可用CPU核心数不足
-- 说明: query_dop不能超过可用CPU核心数

-- 检查代码
// src/gausskernel/optimizer/path/allpaths.cpp
int available_workers = get_available_smp_workers();
if (query_dop > available_workers) {
    u_sess->opt_cxt.smp_enabled = false;
}
```

---

## 第二部分: Hint (查询提示) 使用指南

### 5. Hint系统架构

**Hint语法**:

```sql
-- Hint格式
/*+ [HintKeyword] ([HintOptions]) */

-- 示例
SELECT /*+ HashJoin(a b) */ * FROM table1 a JOIN table2 b ON a.id = b.id;
```

**Hint数据结构**:

```cpp
// src/include/parser/parse_hint.h

// Hint基类
struct Hint {
    NodeTag type;             // Hint类型
    List* relnames;           // 关系列表
    HintKeyword hint_keyword; // Hint关键字
    HintStatus state;         // Hint状态 (NOTUSED/USED/DUPLICATION/ERROR)
};

// Hint关键字枚举
typedef enum HintKeyword {
    HINT_KEYWORD_NESTLOOP = 0,
    HINT_KEYWORD_MERGEJOIN,
    HINT_KEYWORD_HASHJOIN,
    HINT_KEYWORD_LEADING,        // 指定JOIN顺序
    HINT_KEYWORD_ROWS,           // 修正行数估算
    HINT_KEYWORD_BROADCAST,      // 广播分布
    HINT_KEYWORD_REDISTRIBUTE,   // 重分布
    HINT_KEYWORD_BLOCKNAME,      // 块名称
    HINT_KEYWORD_TABLESCAN,      // 扫描方法
    HINT_KEYWORD_INDEXSCAN,
    HINT_KEYWORD_INDEXONLYSCAN,
    HINT_KEYWORD_SKEW,           // 数据倾斜
    HINT_KEYWORD_PREDPUSH,       // 谓词推入
    HINT_KEYWORD_PREDPUSH_SAME_LEVEL,
    HINT_KEYWORD_REWRITE,        // 查询重写规则
    HINT_KEYWORD_GATHER,         // 收集模式
    HINT_KEYWORD_NO_EXPAND,      // 禁止扩展
    HINT_KEYWORD_SET,            // 设置参数
    HINT_KEYWORD_CPLAN,          // 使用自定义计划
    HINT_KEYWORD_GPLAN,          // 使用通用计划
    HINT_KEYWORD_IGNORE,         // 忽略错误
} HintKeyword;
```

**Hint处理流程**:

```
1. 词法/语法分析
   - 识别注释中的Hint
   ↓
2. Hint解析
   - 解析Hint关键字和参数
   - 验证Hint语法
   ↓
3. Hint转换
   - 将表名转换为Relids
   - 将列名转换为AttrNumber
   ↓
4. Hint应用
   - 在优化器各个阶段应用Hint
   - 记录Hint使用状态
   ↓
5. 执行计划生成
   - 生成符合Hint的计划
   ↓
6. Hint状态报告
   - EXPLAIN中显示Hint使用状态
```

---

### 6. Hint类型详解

#### 6.1 JOIN方法Hint

**语法**:

```sql
-- 强制使用HashJoin
/*+ HashJoin(table1 table2) */

-- 禁止使用NestLoop
/*+ No NestLoop(table1 table2) */
```

**示例**:

```sql
-- 示例1: 强制使用HashJoin
EXPLAIN
SELECT /*+ HashJoin(orders customers) */ *
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id;

-- 计划节选 (连接节点被锁定为Hash Join):
--  id |                operation
-- ----+---------------------------------
--   1 | ->  Hash Join
--   2 |      ->  Seq Scan on orders o
--   3 |      ->  Hash
--   4 |            ->  Seq Scan on customers c
--
-- 括号里写的是这一次连接的两侧表 (别名或表名均可),
-- 只圈定 o JOIN c 这个连接对, 不影响查询里其他连接

-- 示例2: 禁止NestLoop (No前缀 = 负向Hint)
EXPLAIN
SELECT /*+ No NestLoop(orders customers) */ o.order_id, c.customer_name
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.region_id = 'EAST';

-- 哪怕代价模型认定 NestLoop+IndexScan 最便宜, 该连接也被压到 HashJoin/MergeJoin;
-- 机制: No类Hint命中的路径 hint_value 减分, 见 7.1
```

**JOIN方法Hint匹配规则** (hint_gram.y: `No_P join_method_hint` 统一支持No前缀; parse_hint.cpp 负责匹配):

```cpp
// src/gausskernel/optimizer/path/joinpath.cpp
// 连接路径生成时按 joinrelids + inner_relids 匹配Hint:
hint = find_specific_join_hint(hstate, joinrelids, inner_path->parent->relids, keyWord);
// 命中后由 pathnode.cpp set_join_hint 置 HINT_STATE_USED 并给该路径 hint_value 加/减分
```

- 同一关键字重复写: drop_duplicate_join_hint 在转换阶段去重, 状态置 HINT_STATE_DUPLICATION, 不会重复计分;
- 表出现在子查询块内时需用块名限定 (HINT_KEYWORD_BLOCKNAME 配套), 具体限定语法⚓待锚;
- Hint是否真正吃到, 以EXPLAIN的unused hint警告为准, 见7.2。

---

### 7. Hint实战案例: SMP计划下的Hint生效语义与验证

前6节把Hint当作"告诉优化器用哪个连接/扫描方法"的单机手段。现场真正高频的三个问题是:
Hint写了不生效怎么定位 (7.1/7.2)、SMP计划里Hint怎么改流形态 (7.3)、并行度与Hint谁说了算 (7.4)。
原文第三部分 (Rewrite Rule) 被截断丢失, 其主干 — GUC位表、REWRITE约束Hint、双门验证方法 — 收在7.5补齐。

#### 7.1 Hint生效机制: hint_value优先于代价

**核心机制**: openGauss的Hint不改代价, 而是给路径打hint_value分, 路径比较时hint_value先于startup/total cost:

```cpp
// src/gausskernel/optimizer/util/pathnode.cpp
// set_hint_value: 每条新路径创建时调用, 逐类打分:
//   set_scan_hint / set_join_hint / set_stream_hint / set_predpush_same_level_hint
//   命中正向Hint      → path->hint_value++
//   命中No类负向Hint  → path->hint_value--

// compare_path_costs_fuzzily: add_path的路径淘汰入口
if (path1->hint_value > path2->hint_value)
    return COSTS_BETTER1;
else if (path1->hint_value < path2->hint_value)
    return COSTS_BETTER2;
// hint_value分出高下就直接定胜负, 代价根本不参与比较;
// cheapest路径挑选一路的 obtain_cheaper_path 同样先比hint_value
```

**三条推论**:

1. Hint是"钦点"不是"调价": 与 enable_nestloop=off 这类调代价开关本质不同, 命中Hint的路径哪怕代价更高也胜出;
2. 前提是路径客观存在: 索引不存在时 IndexScan Hint 不会无中生有, 只落进unused报告 (见7.2)。所以Hint修不了"缺索引/统计信息失真"这类根因;
3. SMP场景Hint的正确定位: 在并行计划内部钦点流形态与连接方法, 或用Rows Hint修正基数让并行计划在代价层胜出 — 而不是用Hint"打开"并行 (并行开关只有query_dop, 见7.4)。

#### 7.2 生效状态验证: unused hint警告链路

**警告链路**:

```cpp
// parse_hint.cpp  desc_hint_in_state  ← planner.cpp 子查询规划收尾处调用
//   遍历 join/row/rewrite/gather/stream/scan/blockname/set/no_gpc/
//        predpush_same_level/skew 全部Hint列表 (find_unused_hint_to_buf)
//   仅 HINT_STATE_NOTUSED 的进报告, 拼成 "unused hint: xxx" 挂到 glob->hint_warning
// planner.cpp  标准规划收尾: 拷入 PlannedStmt.plan_hint_warning
// explain.cpp  EXPLAIN收尾: output_hint_warning(..., WARNING) 逐条上抛
// postgres.cpp 普通执行路径: 同一份警告以 DEBUG1 级输出 (默认看不见)
```

**验证配方**:

```sql
-- 写Hint必配EXPLAIN对答案: WARNING比计划形状更早告诉你Hint没吃到
EXPLAIN
SELECT /*+ IndexScan(orders) */ * FROM orders WHERE customer_id = 42;

-- 情形A: 计划走Index Scan且无WARNING → Hint生效 (HINT_STATE_USED)
-- 情形B: WARNING:  unused hint: IndexScan(orders)
--        → 路径不存在或被前置条件过滤: 索引缺失/表达式不匹配/SMP禁用清单命中
-- 情形C: WARNING:  SetHint is invalid. Parameter [xxx] is not in whitelist.
--        → SET Hint写了白名单外参数, 解析期即丢弃 (见7.4)

-- HintStatus三态 (parse_hint.h): NOTUSED / USED / DUPLICATION
-- 重复Hint由 drop_duplicate_* 系列在转换阶段去重并标DUPLICATION
```

#### 7.3 SMP计划对照: Stream Hint与Rows Hint

SMP计划里多出来的Streaming节点就是SMP的"形态", 两类Hint直接作用其上:

- `/*+ Broadcast(t) */` / `/*+ Redistribute(t) */` → StreamHint (stream_type + negative),
  pathnode.cpp set_stream_hint 在新建StreamPath类型与Hint一致时置USED并加hint_value分;
- 计划里流类型标签 LOCAL REDISTRIBUTE / LOCAL BROADCAST / LOCAL ROUNDROBIN 来自
  stream_cost.cpp 的distriType映射 (LOCAL GATHER与LOCAL ROUNDROBIN以consumerDop==1分界, execStream.cpp);
  EXPLAIN的Streaming节点把 `(type: ...)` 拆进options列 (explain.cpp PlanTable::set_plan_table_streaming_ops);
- `/*+ Rows(t1 t2 #N) */` → relnode.cpp adjust_rows_according_to_hint, 四种运算
  (#绝对值 / +加 / -减 / *乘, parse_hint.h RowsValueType) 直接改rel->rows。

```sql
-- 对照实验: 同一查询三份计划
-- ① 串行基线
SET query_dop = 1;
EXPLAIN SELECT c.region_id, SUM(o.net_amount)
FROM orders o JOIN customers c ON o.customer_id = c.customer_id
GROUP BY c.region_id;
-- Hash Join → HashAggregate, 无Streaming节点

-- ② 开SMP (query_dop正负值语义/代价模型见09篇, 本文不重复)
SET query_dop = 4;
EXPLAIN SELECT c.region_id, SUM(o.net_amount)
FROM orders o JOIN customers c ON o.customer_id = c.customer_id
GROUP BY c.region_id;
-- 计划插入Streaming节点, 典型形态:
--  id | operation
--   1 | ->  Streaming (type: LOCAL GATHER)          ← 多路producer收拢到1路consumer
--   2 |     ->  HashAggregate
--   3 |         ->  Hash Join
--   4 |             ->  Streaming (type: LOCAL REDISTRIBUTE)  ← 按连接键重分布

-- ③ 钦点流形态 + 修基数
EXPLAIN SELECT /*+ Broadcast(customers) Rows(orders customers #5000000) */
       c.region_id, SUM(o.net_amount)
FROM orders o JOIN customers c ON o.customer_id = c.customer_id
GROUP BY c.region_id;
-- customers小表走广播, 避免orders大表被重分布;
-- Rows把该连接的估算行数钉成500万, 流代价随基数变化, 并行计划更容易胜出
```

**判读要点**:

- Streaming行出现即SMP已启用; 标签前缀LOCAL=同节点内线程间分发, 无LOCAL前缀=跨节点;
- Broadcast Hint通常配"小表广播、大表不动"; Redistribute反之;
- 基数失真是SMP计划"选错流形态"的第一大原因: Rows Hint是现场A/B的捷径, 根治靠ANALYZE;
- Skew Hint (文法 `Skew_P '(' 表 列表 值列表 ')'`) 治流数据倾斜, 倾斜值单独走优化路径;
  其transform与执行侧细节⚓待锚。

#### 7.4 与query_dop的交互决策表

**SET Hint与白名单**: Hint关键字枚举 (parse_hint.h HintKeyword) 里没有DOP专用项, 单查询调DOP只能走SET Hint, 且SET Hint受白名单硬约束:

```sql
-- SET Hint语法: /*+ SET(param value) */, 只认白名单
-- (hint_gram.y解析期调 check_set_hint_in_white_list, 二分查找 G_SET_HINT_WHITE_LIST)
-- 与SMP/改写直接相关的在册参数: query_dop / rewrite_rule / enable_stream_operator
-- 其余在册: enable_seqscan/enable_indexscan/enable_hashjoin/... 路径开关类,
--           seq_page_cost/random_page_cost/cpu_tuple_cost/... 代价类

-- 单条查询开SMP, 会话其余语句不受影响:
SELECT /*+ SET(query_dop 4) */ c.region_id, SUM(o.net_amount)
FROM orders o JOIN customers c ON o.customer_id = c.customer_id
GROUP BY c.region_id;

-- 白名单外参数: WARNING: SetHint is invalid. Parameter [xxx] is not in whitelist.
-- 该Hint整体作废, 不进unused报告 — 解析期就已丢弃
```

**交互决策表**:

| 现场目标 | 用什么 | 依据/边界 |
|---------|--------|----------|
| 会话级固定并行度 | `SET query_dop = N` | 正值静态/负值动态语义见09篇 |
| 单条查询开/关SMP, 会话其余不动 | `/*+ SET(query_dop N) */` | 白名单放行; 无DOP专用Hint关键字 |
| 并行计划总输给串行 (基数失真) | `/*+ Rows(...) */` 修基数后A/B | 并行与否在代价层裁决, Hint只在路径形态层 |
| 并行计划流形态不对 (大表被重分布) | `/*+ Broadcast(小表) */` 等 | 钦点StreamPath, 见7.3 |
| Hint全到位仍是串行 | 查第一部分禁用清单 | 事务块/递归/游标/DML子查询/volatile优先级高于任何Hint |
| 验证某改写是否参与计划 | `/*+ REWRITE(rule) */` 反证 | 点名即禁用, 对照计划diff, 见7.5 |

#### 7.5 改写规则 (Rewrite Rule) 的验证方法

**GUC位表**: rewrite_rule 是PGC_USERSET + GUC_LIST_INPUT的整型位标志GUC
(guc_sql.cpp注册, 逗号名单经 rewrite_options[] 映射到位; 位定义 guc.h rewrite_param):

| 规则名 | 位标志 | 作用 (源码注释口径) |
|--------|--------|--------------------|
| lazyagg | LAZY_AGG | allow lazy agg (惰性聚合) |
| magicset | MAGIC_SET | allow query qual push (子查询qual注入) |
| partialpush | PARTIAL_PUSH | allow partial push (部分下推) |
| uniquecheck | SUBLINK_PULLUP_WITH_UNIQUE_CHECK | 子链接上提+唯一性校验 |
| disablerep | SUBLINK_PULLUP_DISABLE_REPLICATED | 禁止上提到复制表 |
| intargetlist | SUBLINK_PULLUP_IN_TARGETLIST | 目标列子链接上提 |
| predpush | PRED_PUSH | 谓词推入子查询块 |
| predpushnormal / predpushforce | PRED_PUSH_NORMAL / FORCE | 谓词推入的普通档/强制档 |
| disable_pullup_expr_sublink | SUBLINK_PULLUP_DISABLE_EXPR | 禁止表达式子链接上提 |
| enable_sublink_pullup_enhanced | SUBLINK_PULLUP_ENHANCED | 增强子链接上提 |
| remove_redundant_distinct_group_by | REMOVE_REDUNDANT_DISTINCT_GROUP_BY | 去冗余DISTINCT/GROUP BY |

注意: **源码默认值 = MAGIC_SET, 即默认只有magicset位开** — lazyagg/predpush等默认全关。
与多数现场预期相反, 动手前先 `SHOW rewrite_rule;` 看底。

**双门条件**: 每个改写点都长成同一个样子, GUC位与REWRITE约束Hint缺一不可:

```cpp
// GUC位 && REWRITE约束Hint 双门 (planner.cpp / subselect.cpp / prepjointree.cpp)
if ((LAZY_AGG & u_sess->attr.attr_sql.rewrite_rule) && permit_from_rewrite_hint(root, LAZY_AGG)) {
    // 真正做lazyagg改写
}
// permit_from_rewrite_hint (parse_hint.cpp): /*+ REWRITE(...) */ 点名的规则 → return false
// 即 REWRITE Hint 是"点名即禁用"的约束语义, 不是"点名即启用"!
```

REWRITE Hint支持的规则名 (get_rewrite_rule_bits, 名单外报WARNING):
lazyagg, magicset, partialpush, uniquecheck, disablerep, intargetlist,
disable_pullup_expr_sublink, enable_sublink_pullup_enhanced。
predpush不在此名单 — 它有独立的PREDPUSH Hint:
`/*+ predpush(表1 表2) */` 定向推入, `/*+ No Predpush */` 整体禁用
(permit_predpush只看首个predpush hint的negative位, 所以No类必须写在前);
文法还支持 `No_P rewrite_hint` 负向形式, 其反向语义细节⚓待锚。

**验证配方**:

```sql
-- 三份计划diff法: 改写是否参与, 一眼可辨
-- ① 默认底座
SHOW rewrite_rule;                  -- 默认仅magicset位
EXPLAIN SELECT ...;                 -- 记下计划形状

-- ② 会话开目标规则
SET rewrite_rule = 'lazyagg';       -- 只留lazyagg位
EXPLAIN SELECT ...;                 -- 聚合节点位置变化 = lazyagg参与

-- ③ 单查询反证 (REWRITE约束Hint)
EXPLAIN SELECT /*+ REWRITE(lazyagg) */ ...;
-- 点名即禁用 → 若计划退回①的形状, 证明②的差异确由该规则引起

-- predpush同理: /*+ predpush(子查询侧 外层侧) */ 钦点推入位置;
--               整体禁用用 No Predpush (REWRITE名单不含predpush)
```

---

## 总结

- SMP启用是"禁用条件清单+代价"双闸, Hint不是第三闸: 第一部分的事务块/递归CTE/游标/DML子查询/volatile函数等条件优先级高于任何Hint, 命中即降串行, Hint救不回。
- Hint的生效机制是hint_value而非代价: pathnode.cpp set_hint_value给路径打分, compare_path_costs_fuzzily与obtain_cheaper_path先比hint_value再比代价 — 命中Hint的路径无条件胜出, 与enable_xxx=off调代价法本质不同。
- 写Hint必配EXPLAIN对答案: 未生效Hint以"unused hint:"WARNING上抛 (explain.cpp output_hint_warning), 普通执行时仅DEBUG1级; HintStatus三态 NOTUSED/USED/DUPLICATION, 重复Hint由drop_duplicate_*去重。
- SMP计划里Hint改的是流形态: Broadcast/Redistribute作用于StreamPath (set_stream_hint), 计划标签LOCAL REDISTRIBUTE/LOCAL BROADCAST/LOCAL ROUNDROBIN (stream_cost.cpp映射); Rows Hint四种运算(#/+/−/*)直接改rel->rows (adjust_rows_according_to_hint), 是并行计划A/B的基数修正捷径。
- 并行度没有专用Hint关键字: HintKeyword枚举无DOP项, 单查询调DOP只能走 `/*+ SET(query_dop N) */`, 且SET Hint受G_SET_HINT_WHITE_LIST白名单硬约束 (query_dop/rewrite_rule/enable_stream_operator在册), 白名单外参数解析期即WARNING作废。
- 改写规则是"GUC位 && REWRITE约束Hint"双门: rewrite_rule源码默认仅MAGIC_SET位 (lazyagg/predpush默认关); `/*+ REWRITE(rule) */` 是点名即禁用的约束语义 (permit_from_rewrite_hint命中返回false), 与直觉相反; predpush不进REWRITE名单, 由独立PREDPUSH Hint控制。
- Hint修不了根因: 缺路径(缺索引)、基数失真(统计信息陈旧)、SMP禁用条件, 这三类问题的正解分别是建索引、ANALYZE、改写语句/会话参数; Hint定位是验证与临时纠偏。

## 版本注记

- 2026-11-05 断尾补齐, 原文500行截断。原文断于6.1节JOIN方法Hint示例1的SQL代码块内 (HashJoin示例未闭合): 本次补完该示例与示例2 (No前缀负向Hint) 及匹配规则要点, 并顺势补写第7节"SMP计划下的Hint生效语义与验证" — 生效机制 (7.1 hint_value)、状态验证 (7.2 unused警告链路)、SMP计划对照 (7.3 Stream/Rows Hint)、query_dop交互决策表 (7.4 SET Hint白名单)、改写规则验证方法 (7.5 GUC位表+REWRITE约束Hint+双门)。
- 原文目录承诺的8~10节 (Rewrite Rule概述/配置/实战案例) 在截断处丢失, 未单独重建; 其主干已并入7.5, 目录第三部分已改为注明。
- 前文 (1~6节) 所引check_smp_available、ParallelDesc等结构与部分GUC默认值为原文教学写法, 与6.0.0源码实体未逐项对账; 本次补写部分源码引用一律为 文件名+函数名, 基于源码树逐项核对, 未核实处标⚓待锚。

## 来源

- 源码基线: openGauss-server-v6.0.0 — src/common/backend/parser/parse_hint.cpp (desc_hint_in_state/find_unused_hint_to_buf/output_hint_warning/permit_from_rewrite_hint/get_rewrite_rule_bits/permit_predpush/G_SET_HINT_WHITE_LIST/check_set_hint_in_white_list)、src/common/backend/parser/hint_gram.y (No_P前缀文法/rewrite_hint/set_hint白名单拦截/skew_hint)、src/include/parser/parse_hint.h (HintKeyword枚举/HintStatus三态/JoinMethodHint/RowsHint/StreamHint/SkewHint/RewriteHint/SetHint)、src/gausskernel/optimizer/util/pathnode.cpp (set_hint_value/set_scan_hint/set_join_hint/set_stream_hint/compare_path_costs_fuzzily/obtain_cheaper_path/find_hinted_path)、src/gausskernel/optimizer/util/relnode.cpp (adjust_rows_according_to_hint)、src/gausskernel/optimizer/path/joinpath.cpp (find_specific_join_hint)、src/gausskernel/optimizer/plan/planner.cpp (desc_hint_in_state调用点/plan_hint_warning传递/LAZY_AGG与PARTIAL_PUSH改写门)、src/gausskernel/optimizer/plan/subselect.cpp 与 prep/prepjointree.cpp (改写点双门)、src/gausskernel/optimizer/commands/explain.cpp (output_hint_warning/PlanTable::set_plan_table_streaming_ops)、src/gausskernel/process/tcop/postgres.cpp (DEBUG1输出plan_hint_warning)、src/gausskernel/process/stream/stream_cost.cpp 与 execStream.cpp (distriType→标签映射)、src/include/optimizer/stream_cost.h (StreamType)、src/include/utils/guc.h (rewrite_param位标志/ENABLE_PRED_PUSH宏)、src/common/backend/utils/misc/guc/guc_sql.cpp (rewrite_options[]/rewrite_rule注册默认MAGIC_SET)
- 原文 2026-04 学习档案 (优化器系列第十部分, 500行截断版)
- 关联: VastBase_Optimizer_09_Parallel_Executor_Tuning_Deep_Dive.md (query_dop语义/SMP代价模型/流执行机制, 本文Hint侧不重复)
