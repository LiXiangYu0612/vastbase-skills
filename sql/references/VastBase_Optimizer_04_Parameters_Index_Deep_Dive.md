# VastBase/openGauss 优化器参数与索引优化超详细深度分析

> 源码版本: openGauss-server-v6.0.0  
> 分析文件: guc.cpp, costsize.cpp, indxpath.cpp (20+文件)  
> 学习目标: 掌握所有优化器参数的作用、调优方法、索引选择策略

---

## 目录

### 第四部分: 优化器参数
- [20. Cost配置参数](#20-cost配置参数)
- [21. 规划器开关参数](#21-规划器开关参数)
- [22. 其他优化器参数](#22-其他优化器参数)
- [23. 参数调优实战](#23-参数调优实战)

### 第五部分: 索引优化
- [24. 索引类型对比](#24-索引类型对比)
- [25. 索引选择算法](#25-索引选择算法)
- [26. 索引优化实战](#26-索引优化实战)
- [27. 索引维护成本](#27-索引维护成本)

---

## 20. Cost配置参数

### 20.1 seq_page_cost (顺序页成本)

**参数定义**:

```sql
-- 查看当前值
SHOW seq_page_cost;  -- 默认: 1.0

-- 参数含义:
-- 顺序读取一个磁盘页的成本
-- 作为其他成本参数的基准单位

-- 调整语法
SET seq_page_cost = 1.0;  -- 会话级别
-- 或
ALTER DATABASE dbname SET seq_page_cost = 1.0;  -- 数据库级别
```

**源码分析**:

```cpp
// src/backend/utils/misc/guc.c
// seq_page_cost的定义

{
    {"seq_page_cost",
        PGC_USERSET,
        QUERY_TUNING_COST,
        gettext_noop("Sets the planner's estimate of the cost of "
                     "a sequentially fetched disk page."),
        NULL
    },
    &u_sess->attr.attr_sql.seq_page_cost,
    DEFAULT_SEQ_PAGE_COST,  // 默认值: 1.0
    0.0,                     // 最小值
    DBL_MAX,                 // 最大值
    NULL, NULL, NULL
}

// 使用位置
// src/gausskernel/optimizer/path/costsize.cpp
// cost_seqscan函数

void cost_seqscan(Path* path, PlannerInfo* root, 
                 RelOptInfo* baserel, ParamPathInfo* param_info)
{
    // 获取表空间的seq_page_cost
    double spc_seq_page_cost;
    get_tablespace_page_costs(baserel->reltablespace, 
                             NULL, &spc_seq_page_cost);
    
    // 计算磁盘I/O成本
    run_cost += spc_seq_page_cost * baserel->pages;
    
    // baserel->pages: 表的页数
    // spc_seq_page_cost: 通常是1.0
    // 结果: 顺序扫描的磁盘成本
}
```

**调整场景**:

```sql
-- 场景1: SSD存储
-- SSD的顺序读取速度比HDD快
-- 可以降低seq_page_cost

-- 检查存储类型
SHOW data_directory;  -- 查看数据目录

-- 调整 (如果是SSD)
SET seq_page_cost = 0.5;

-- 测试影响
EXPLAIN
SELECT * FROM large_table;

-- 观察cost变化

-- 场景2: 网络存储 (NFS, SAN)
-- 网络存储的延迟可能更高
-- 可以提高seq_page_cost

SET seq_page_cost = 2.0;

EXPLAIN
SELECT * FROM large_table;

-- 场景3: 不同表空间
-- 创建表空间 (不同存储)
CREATE TABLESPACE ssd_space LOCATION '/mnt/ssd/opengauss';
CREATE TABLESPACE hdd_space LOCATION '/mnt/hdd/opengauss';

-- 为表空间设置不同的page cost (需要修改配置文件)
-- postgresql.conf:
-- ssd_space.seq_page_cost = 0.5
-- hdd_space.seq_page_cost = 1.0

-- 在不同表空间创建表
CREATE TABLE table_on_ssd (id INT) TABLESPACE ssd_space;
CREATE TABLE table_on_hdd (id INT) TABLESPACE hdd_space;

-- 优化器会根据表空间选择不同的扫描方式
EXPLAIN
SELECT * FROM table_on_ssd;
EXPLAIN
SELECT * FROM table_on_hdd;
```

### 20.2 random_page_cost (随机页成本)

**参数定义**:

```sql
-- 查看当前值
SHOW random_page_cost;  -- 默认: 4.0

-- 参数含义:
-- 随机读取一个磁盘页的成本
-- 通常是seq_page_cost的4倍 (反映HDD的寻道时间)

-- 调整语法
SET random_page_cost = 4.0;
```

**源码分析**:

```cpp
// src/backend/utils/misc/guc.c
{
    {"random_page_cost",
        PGC_USERSET,
        QUERY_TUNING_COST,
        gettext_noop("Sets the planner's estimate of the cost of "
                     "a non-sequentially fetched disk page."),
        NULL
    },
    &u_sess->attr.attr_sql.random_page_cost,
    DEFAULT_RANDOM_PAGE_COST,  // 默认值: 4.0
    0.0,
    DBL_MAX,
    NULL, NULL, NULL
}

// 使用位置
// cost_index函数

void cost_index(Path* path, PlannerInfo* root, IndexPath* ipath,
               bool is_indexjoin, List* indexQuals, 
               List* indexConditionals)
{
    // 计算索引扫描的随机I/O成本
    Cost index_scan_cost = 
        random_page_cost * numIndexPages;  // 索引页
        + random_page_cost * numHeapPages; // 堆表页
    
    // random_page_cost影响索引扫描的吸引力
    // random_page_cost越高 → 索引扫描越不划算
}
```

**调整场景**:

```sql
-- 场景1: SSD存储
-- SSD的随机读取速度接近顺序读取
-- 可以降低random_page_cost

SET seq_page_cost = 1.0;
SET random_page_cost = 1.1;  -- 接近seq_page_cost

-- 测试
EXPLAIN
SELECT * FROM large_table WHERE id = 12345;

-- 观察是否更倾向于使用索引

-- 场景2: 大内存 (大部分数据在缓存中)
-- 如果effective_cache_size很大
-- 随机I/O实际上是从内存读取
-- 可以降低random_page_cost

SET random_page_cost = 1.5;
SET effective_cache_size = '64GB';

EXPLAIN
SELECT * FROM large_table WHERE id = 12345;

-- 场景3: RAID10存储
-- RAID10的随机I/O性能较好
-- 可以降低random_page_cost

SET seq_page_cost = 1.0;
SET random_page_cost = 2.0;

-- 经验公式:
-- HDD: random_page_cost = 4.0
-- SSD: random_page_cost = 1.1 ~ 1.5
-- RAID10: random_page_cost = 2.0
-- NFS: random_page_cost = 3.0 ~ 5.0

-- 测试最优值
EXPLAIN (ANALYZE)
SELECT * FROM large_table WHERE indexed_column = value;

-- 尝试不同的random_page_cost
-- 选择使实际执行时间最接近预估的值
```

### 20.3 cpu_tuple_cost (CPU元组成本)

**参数定义**:

```sql
-- 查看当前值
SHOW cpu_tuple_cost;  -- 默认: 0.01

-- 参数含义:
-- 处理一个tuple的CPU成本
-- 包括: 解析、投影、约束检查等

-- 调整语法
SET cpu_tuple_cost = 0.01;
```

**源码分析**:

```cpp
// src/backend/utils/misc/guc.c
{
    {"cpu_tuple_cost",
        PGC_USERSET,
        QUERY_TUNING_COST,
        gettext_noop("Sets the planner's estimate of the cost of "
                     "processing each row."),
        NULL
    },
    &u_sess->attr.attr_sql.cpu_tuple_cost,
    DEFAULT_CPU_TUPLE_COST,  // 默认值: 0.01
    0.0,
    DBL_MAX,
    NULL, NULL, NULL
}

// 使用位置
// cost_seqscan函数

void cost_seqscan(Path* path, PlannerInfo* root, 
                 RelOptInfo* baserel, ParamPathInfo* param_info)
{
    // CPU处理成本
    cpu_per_tuple = cpu_tuple_cost + qpqual_cost.per_tuple;
    run_cost += cpu_per_tuple * baserel->tuples;
    
    // cpu_tuple_cost影响:
    // 1. 顺序扫描 vs 索引扫描的选择
    // 2. JOIN方法的选择 (Hash Join vs Nested Loop)
    // 3. 排序 vs 索引扫描的选择
}
```

**调整场景**:

```sql
-- 场景1: CPU密集型查询
-- 如果查询包含大量计算 (函数、表达式)
-- 可以提高cpu_tuple_cost

SET cpu_tuple_cost = 0.02;

EXPLAIN
SELECT 
    expensive_function(column1),
    another_expensive_function(column2)
FROM large_table;

-- 优化器可能更倾向于减少行数的操作 (如先过滤)

-- 场景2: IO密集型查询
-- 如果IO是瓶颈 (大表扫描)
-- 可以降低cpu_tuple_cost

SET cpu_tuple_cost = 0.005;

EXPLAIN
SELECT * FROM large_table;

-- 优化器可能更倾向于顺序扫描 (避免CPU开销)

-- 场景3: 复杂JOIN
-- cpu_tuple_cost影响Hash Join的选择
-- Hash Join的CPU成本较高

SET cpu_tuple_cost = 0.01;  -- 默认
EXPLAIN
SELECT * FROM table1 t1 JOIN table2 t2 ON t1.id = t2.id;

SET cpu_tuple_cost = 0.02;  -- 提高
EXPLAIN
SELECT * FROM table1 t1 JOIN table2 t2 ON t1.id = t2.id;

-- 观察: 提高cpu_tuple_cost后，可能更倾向于Merge Join
```

### 20.4 cpu_index_tuple_cost (索引元组成本)

**参数定义**:

```sql
-- 查看当前值
SHOW cpu_index_tuple_cost;  -- 默认: 0.005

-- 参数含义:
-- 处理一个索引条目的CPU成本
-- 通常是cpu_tuple_cost的一半

-- 调整语法
SET cpu_index_tuple_cost = 0.005;
```

**源码分析**:

```cpp
// src/backend/utils/misc/guc.c
{
    {"cpu_index_tuple_cost",
        PGC_USERSET,
        QUERY_TUNING_COST,
        gettext_noop("Sets the planner's estimate of the cost of "
                     "processing each index entry."),
        NULL
    },
    &u_sess->attr.attr_sql.cpu_index_tuple_cost,
    DEFAULT_CPU_INDEX_TUPLE_COST,  // 默认值: 0.005
    0.0,
    DBL_MAX,
    NULL, NULL, NULL
}

// 使用位置
// cost_index函数

void cost_index(Path* path, PlannerInfo* root, IndexPath* ipath,
               bool is_indexjoin, List* indexQuals, 
               List* indexConditionals)
{
    // 索引扫描的CPU成本
    Cost indexCorrelation = 0.0;
    Cost run_cost = 0.0;
    
    // 处理索引条目的成本
    run_cost += cpu_index_tuple_cost * numIndexTuples;
    
    // cpu_index_tuple_cost影响索引扫描的吸引力
    // 较低的cpu_index_tuple_cost → 更倾向于索引扫描
}
```

**调整场景**:

```sql
-- 场景1: 复合索引
-- 复合索引的CPU成本较高 (需要处理多个字段)
-- 可以提高cpu_index_tuple_cost

SET cpu_index_tuple_cost = 0.01;

EXPLAIN
SELECT * FROM table_with_composite_index 
WHERE col1 = 1 AND col2 = 2;

-- 场景2: 函数索引
-- 函数索引的CPU成本更高 (需要计算函数)

CREATE INDEX idx_function ON table_with_function_index 
    (lower(column_name));

SET cpu_index_tuple_cost = 0.015;

EXPLAIN
SELECT * FROM table_with_function_index 
WHERE lower(column_name) = 'value';

-- 场景3: 表达式索引
-- 表达式索引的CPU成本最高

CREATE INDEX idx_expression ON table_with_expression_index 
    ((column1 * column2));

SET cpu_index_tuple_cost = 0.02;

EXPLAIN
SELECT * FROM table_with_expression_index 
WHERE column1 * column2 > 100;

-- 调优建议:
-- - 复杂索引 (表达式、函数): cpu_index_tuple_cost = 0.015 ~ 0.02
-- - 复合索引: cpu_index_tuple_cost = 0.01
-- - 单列索引: cpu_index_tuple_cost = 0.005 (默认)
```

### 20.5 cpu_operator_cost (操作符成本)

**参数定义**:

```sql
-- 查看当前值
SHOW cpu_operator_cost;  -- 默认: 0.0025

-- 参数含义:
-- 执行一个操作符或函数的CPU成本
-- 用于: WHERE条件、JOIN条件、ORDER BY等

-- 调整语法
SET cpu_operator_cost = 0.0025;
```

**源码分析**:

```cpp
// src/backend/utils/misc/guc.c
{
    {"cpu_operator_cost",
        PGC_USERSET,
        QUERY_TUNING_COST,
        gettext_noop("Sets the planner's estimate of the cost of "
                     "processing each operator or function."),
        NULL
    },
    &u_sess->attr.attr_sql.cpu_operator_cost,
    DEFAULT_CPU_OPERATOR_COST,  // 默认值: 0.0025
    0.0,
    DBL_MAX,
    NULL, NULL, NULL
}

// 使用位置
// cost_qual_eval函数

void cost_qual_eval(QualCost* cost, List* quals, PlannerInfo* root)
{
    cost->startup = 0.0;
    cost->per_tuple = 0.0;
    
    // 评估每个条件
    foreach qual, quals) {
        QualCost qual_cost;
        cost_qual_eval_node(&qual_cost, qual, root);
        cost->startup += qual_cost.startup;
        cost->per_tuple += qual_cost.per_tuple;
    }
}

void cost_qual_eval_node(QualCost* cost, Node* qual, PlannerInfo* root)
{
    if (IsA(qual, OpExpr)) {
        // 操作符表达式
        cost->per_tuple += cpu_operator_cost;
        
        // 递归评估操作数
        cost_qual_eval_node(cost, (Node*) opexpr->args, root);
    }
    else if (IsA(qual, FuncExpr)) {
        // 函数表达式
        cost->per_tuple += cpu_operator_cost * funcexpr->args_length;
        
        // 递归评估参数
        cost_qual_eval_node(cost, (Node*) funcexpr->args, root);
    }
    // ... 其他节点类型
}
```

**调整场景**:

```sql
-- 场景1: WHERE条件中操作符/函数密集
-- 每个操作符/函数调用都按cpu_operator_cost计费
-- 条件越多, 提高该参数越能抑制"先扫后滤"的计划

SET cpu_operator_cost = 0.005;  -- 默认的2倍

EXPLAIN
SELECT * FROM large_table
WHERE col1 + col2 > 100         -- 1个操作符
  AND lower(col3) = 'abc'       -- 1个函数
  AND length(col4) > 5;         -- 再1个函数

-- 观察: 优化器更倾向先用索引/其他手段缩减行数再求值复杂条件

-- 场景2: JOIN条件被函数包裹
-- Nested Loop的内层每行都要重复求值JOIN条件, 对该参数最敏感

SET cpu_operator_cost = 0.0025;  -- 默认
EXPLAIN
SELECT * FROM t1 JOIN t2 ON lower(t1.key) = lower(t2.key);

SET cpu_operator_cost = 0.005;   -- 提高
EXPLAIN
SELECT * FROM t1 JOIN t2 ON lower(t1.key) = lower(t2.key);

-- 观察: 提高后更倾向Hash Join (连接条件每行只求值一次, 建表/探测各一次)

-- 场景3: 复杂表达式投影
-- 目标列里的表达式同样进入代价模型 (pathtarget成本)

EXPLAIN
SELECT id, md5(col1) || '-' || col2 FROM large_table;

-- 调优建议:
-- - cpu_operator_cost与cpu_tuple_cost的默认比例是1:4 (0.0025 vs 0.01)
-- - 调整时保持这个量级关系, 不要单独放大一个数量级
-- - 函数密集场景可到 0.005 ~ 0.01
```

---

## 23. 参数调优实战

### 23.1 调优决策表

五个cost参数与规划器开关的作用最终都体现在计划选择上, 现场按下表对症下药:

| 现场症状 | 首选参数 | 调整方向 | 验证方法 |
|---------|---------|---------|---------|
| 索引明明可用却走全表扫描 | random_page_cost | 降(SSD 1.1~1.5) | EXPLAIN对比计划是否换索引扫描 |
| 低选择率查询选了低效索引 | random_page_cost | 升 | EXPLAIN (ANALYZE)对比实际耗时 |
| 明明该走Hash Join却选了Nested Loop | cpu_operator_cost / cpu_tuple_cost | 升 | 观察连接方式切换点 |
| Nested Loop内层反复全扫 | enable_nestloop | off(仅诊断) | 确认是否缺内层索引/统计信息 |
| 排序与索引扫描之间摇摆 | effective_cache_size | 调整到真实缓存规模 | 对比Sort+SeqScan与IndexScan的取舍 |
| 走了Index Only Scan却更慢 | enable_indexonlyscan | off(仅诊断) | 检查可见性映射(all-visible)比例 |
| 多表连接计划不稳定 | geqo_threshold | 升(避开GEQO) | 数FROM项数是否越过12 |
| 所有参数调完计划仍差 | — | 先ANALYZE | 统计信息过期时调参数没有意义(见第02篇) |

### 23.2 enable_*开关的代价惩罚机制

```cpp
// src/gausskernel/optimizer/path/costsize.cpp
// 函数: cost_seqscan (其余cost_*函数同理)

if (!u_sess->attr.attr_sql.enable_seqscan)
    startup_cost += disable_cost;   // disable_cost = 1.0e10

// openGauss/VastBase在标准PG基础上加了第二重惩罚:
if (!u_sess->attr.attr_sql.enable_seqscan)
    path->total_cost *= disable_cost_enlarge_factor的平方;
```

```sql
-- 惩罚效果: 关闭的路径类型cost直接上1.0e10量级, 除非别无选择才会被选中
SET enable_seqscan = off;
EXPLAIN SELECT * FROM large_table;
-- Seq Scan on large_table  (cost=10000000000.00 .. 1000022736.00 ...)
--                                            ^^^^^^^^^^ 惩罚代价, 不是真实开销

-- 注意: enable_*只用于诊断和强制对比, 不要作为长期方案
-- 计划被强制后, 数据分布变化时不会自适应
```

### 23.3 调优流程

```sql
-- 五步流程:

-- 1. 固定基线: 记录当前计划与参数
EXPLAIN (ANALYZE) SELECT ...;
SELECT name, setting FROM pg_settings
WHERE name IN ('seq_page_cost', 'random_page_cost', 'cpu_tuple_cost',
               'cpu_operator_cost', 'effective_cache_size');

-- 2. 对症: 按上面决策表定位参数

-- 3. 会话级试调 (先不动全局)
SET random_page_cost = 1.1;

-- 4. 对比: 计划是否改变、估算与实际是否更接近
EXPLAIN (ANALYZE) SELECT ...;

-- 5. 固化: 确认有效后再落到数据库/用户级
ALTER DATABASE dbname SET random_page_cost = 1.1;
-- 或只对特定应用账号生效
ALTER USER app_user SET random_page_cost = 1.1;

-- 原则:
-- - 参数解决的是"代价模型失真", 统计信息的问题先ANALYZE
-- - 一次只调一个参数, 否则归因不清
-- - openGauss/VastBase另有allocate_mem_cost(内存分配计费)等扩展参数,
--   注册同样在guc/guc_sql.cpp, 现场按需查pg_settings确认
```

---

## 总结

- 五个cost参数(seq_page_cost=1.0 / random_page_cost=4.0 / cpu_tuple_cost=0.01 / cpu_index_tuple_cost=0.005 / cpu_operator_cost=0.0025)默认值定义在cost.h, GUC注册在guc/guc_sql.cpp, 全部会话级可调。
- 磁盘参数决定"扫描方式之争", CPU参数决定"连接方式与过滤顺序之争"; cpu_operator_cost与cpu_tuple_cost默认保持1:4量级关系。
- random_page_cost是现场最常动的一个: SSD降到1.1~1.5, 大缓存可再降; 调整后必须用EXPLAIN (ANALYZE)验证估算与实际的偏差是否缩小。
- enable_*开关的机制是disable_cost(1.0e10)惩罚而非删除路径, openGauss还叠加了放大因子双重惩罚; 只用于诊断对比, 不作长期方案。
- effective_cache_size不影响分配, 只影响代价模型对缓存命中的假设, 应设为真实缓存规模的参考值。
- 调优五步: 基线 → 对症(决策表) → 会话级SET → EXPLAIN (ANALYZE)对比 → ALTER DATABASE/USER固化。
- 参数调优的前提是统计信息准确: 先ANALYZE再谈参数(见第02篇)。

## 版本注记

- 2026-11-05 断尾补齐: 本篇原文在500行处截断(截于20.5节cpu_operator_cost的"调整场景"标题后), 本次顺接补完该场景示例, 按目录中已有的第23节标题补写"参数调优实战"一节(调优决策表)。
- 原目录第21节(规划器开关参数)、第22节(其他优化器参数)的原文未随截断保留, 其核心(enable_*惩罚机制、effective_cache_size)已并入23.1/23.2; 第五部分索引优化(第24~27节)原文未保留, 不虚构重建。

## 来源

- 源码基线: openGauss-server-v6.0.0。本篇引用: src/common/backend/utils/misc/guc/guc_sql.cpp(InitSqlConfigureNamesReal注册seq_page_cost / random_page_cost / cpu_tuple_cost / allocate_mem_cost / cpu_index_tuple_cost / cpu_operator_cost, InitSqlConfigureNamesBool注册enable_*开关, effective_cache_size注册), src/include/optimizer/cost.h(默认值常量与DEFAULT_EFFECTIVE_CACHE_SIZE=16384页), src/gausskernel/optimizer/path/costsize.cpp(cost_seqscan / cost_index / cost_qual_eval及disable_cost惩罚), knl_instance.cpp(disable_cost = 1.0e10初始化)。
- 本篇原文: 2026-04 学习档案(VastBase/openGauss 优化器参数与索引优化超详细深度分析)。
