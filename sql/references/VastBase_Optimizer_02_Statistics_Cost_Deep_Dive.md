# VastBase/openGauss 统计信息与Cost计算超详细深度分析

> 源码版本: openGauss-server-v6.0.0  
> 分析文件: costsize.cpp (6577行), clausesel.cpp (1289行)  
> 学习目标: 给定任意执行计划，能精确计算每个节点的cost和rows

---

## 目录

### 第二部分: 统计信息和Cost计算
- [7. 统计信息系统架构](#7-统计信息系统架构)
- [8. 表级统计信息](#8-表级统计信息)  
- [9. 列级统计信息](#9-列级统计信息)
- [10. 索引统计信息](#10-索引统计信息)
- [11. 选择率计算](#11-选择率计算)
- [12. Cost计算模型](#12-cost计算模型)
- [13. 执行计划Cost分析实例](#13-执行计划cost分析实例)

---

## 7. 统计信息系统架构

### 7.1 统计信息收集机制

**ANALYZE命令流程**:

```sql
-- 手动收集统计信息
ANALYZE table_name;

-- 并行收集统计信息
ANALYZE VERBOSE table_name;

-- 收集特定列的统计信息
ANALYZE table_name (column1, column2);

-- 自动统计信息收集配置
-- postgresql.conf
autovacuum = on
autovacuum_analyze_scale_factor = 0.1    -- 10%数据变化时触发
autovacuum_analyze_threshold = 50        -- 至少50行变化时触发
```

**源码级统计信息收集**:

```cpp
// src/gausskernel/optimizer/commands/analyze.cpp
// 主函数: analyze_rel

void analyze_rel(Oid relid, VacuumStmt* stmt, AcquireSampleRowsFunc acquirefunc)
{
    Relation rel;
    int elevel = log_min_messages;
    
    // 步骤1: 打开表
    rel = relation_open(relid, AccessShareLock);
    
    // 步骤2: 确定需要分析的列
    List* va_cols = NIL;  // 用户指定的列
    if (stmt->va_cols)
        va_cols = stmt->va_cols;
    
    // 步骤3: 获取表的行数估计
    BlockNumber totalpages = RelationGetNumberOfBlocks(rel);
    int64 totalrows = estimate_rel_size(rel, totalpages, ...);
    
    // 步骤4: 采样统计
    // 默认采样目标: default_statistics_target = 100
    int target = default_statistics_target;
    int sample_rows = Min(target * 30, totalrows);  // 采样行数
    
    // 步骤5: 执行采样
    HeapTuple* rows = (HeapTuple*) palloc(sample_rows * sizeof(HeapTuple));
    int num_samples = (*acquirefunc)(
        rel, 
        elevel,
        rows,         // 输出: 采样的行
        sample_rows,  // 目标采样数
        &totalrows    // 输出: 总行数估计
    );
    
    // 步骤6: 计算统计信息
    for (每个列)
    {
        // 6.1 计算基本统计: NULL值比例、平均宽度
        compute_scalar_stats(rel, attnum, rows, num_samples, ...);
        
        // 6.2 如果数据可排序，计算MCV (Most Common Values)
        if (列有较少的distinct值)
            compute_mcv(rel, attnum, rows, num_samples, ...);
        
        // 6.3 如果数据是标量，计算直方图
        if (列是数值类型或日期类型)
            compute_histogram(rel, attnum, rows, num_samples, ...);
        
        // 6.4 计算相关性（如果有多列统计信息）
        if (有多列统计)
            compute_correlation(rel, attnum1, attnum2, ...);
    }
    
    // 步骤7: 更新系统表 pg_statistic
    update_pg_statistic(rel, ...);
    
    // 步骤8: 清理
    relation_close(rel, AccessShareLock);
}
```

### 7.2 统计信息存储结构

**系统表结构**:

```sql
-- pg_statistic: 存储列级统计信息
\d+ pg_statistic

-- 主要字段:
starelid      oid        -- 表的OID
stattarget    int4       -- 统计目标值 (default_statistics_target)
staopno       oid[]      -- 操作符OID数组
staColl       oid[]      -- collation OID数组
stanumbers    float4[]   -- 数值统计 (NULL比例、MCV频率、直方图边界)
stavalues     anyarray   -- 值统计 (MCV值、直方图值)
-- 查看某个表的统计信息
SELECT 
    s.starelid::regclass AS table_name,
    a.attname AS column_name,
    s.stanullfrac AS null_frac,
    s.stawidth AS avg_width,
    array_length(s.stavalues1, 1) AS n_distinct,
    s.stadistinct AS distinct_estimate
FROM pg_statistic s
JOIN pg_attribute a ON s.starelid = a.attrelid 
    AND s.staattnum = a.attnum
WHERE s.starelid = 'your_table'::regclass;

-- 查看MCV (Most Common Values)
SELECT 
    a.attname AS column_name,
    s.stavalues1 AS mcv_values,
    s.stanumbers1 AS mcv_frequencies
FROM pg_statistic s
JOIN pg_attribute a ON s.starelid = a.attrelid 
    AND s.staattnum = a.attnum
WHERE s.starelid = 'your_table'::regclass
  AND s.stakind1 = 1;  -- 1 = MCV

-- 查看直方图
SELECT 
    a.attname AS column_name,
    s.stavalues1 AS histogram_bounds
FROM pg_statistic s
JOIN pg_attribute a ON s.starelid = a.attrelid 
    AND s.staattnum = a.attnum
WHERE s.starelid = 'your_table'::regclass
  AND s.stakind1 = 2;  -- 2 = 直方图
```

**统计信息类型编码**:

```cpp
// src/include/catalog/pg_statistic.h
#define STATISTIC_KIND_MCV        1  // Most Common Values
#define STATISTIC_KIND_HISTOGRAM  2  // 直方图
#define STATISTIC_KIND_CORRELATION 3  // 相关性
#define STATISTIC_KIND_MCV        4  // MCV元素
#define STATISTIC_KIND_HISTOGRAM  5  // 直方图边界
#define STATISTIC_KIND_CORRELATION 6  // 相关性系数
#define STATISTIC_KIND_EC_CLASS   7  // 等价类

// 示例: 读取统计信息
typedef struct StatisticExtInfo
{
    Oid         statOid;        // 统计信息的OID
    char        kind;           // 统计类型 (见上面定义)
    Oid         dataType;       // 数据类型
    int32       nvalues;        // 值的数量
    float4*     numbers;        // 数值统计
    Datum*      values;         // 值统计
} StatisticExtInfo;
```

---

## 8. 表级统计信息

### 8.1 pg_class表统计

```sql
-- 查看表级统计信息
SELECT 
    relname AS table_name,
    reltuples AS row_estimate,
    relpages AS page_estimate,
    relallvisible AS all_visible_pages,
    relkind AS relation_type
FROM pg_class
WHERE relname = 'your_table';

-- 字段说明:
-- reltuples:    表的行数估计（浮点数）
-- relpages:     表占用的磁盘页数（8KB每页）
-- relallvisible: 对可见性映射(VMM)可见的页数（用于优化）
-- relkind:      关系类型 (r=普通表, i=索引, S=序列, v=视图, m=物化视图)

-- 计算表大小
SELECT 
    pg_size_pretty(pg_total_relation_size('your_table')) AS total_size,
    pg_size_pretty(pg_relation_size('your_table')) AS table_size,
    pg_size_pretty(pg_total_relation_size('your_table') - pg_relation_size('your_table')) AS indexes_size;

-- 查看表的详细统计
SELECT 
    schemaname,
    tablename,
    n_live_tup,        -- 活跃行数
    n_dead_tup,        -- 死行数
    last_vacuum,       -- 最后一次VACUUM时间
    last_autovacuum,   -- 最后一次自动VACUUM时间
    last_analyze,      -- 最后一次ANALYZE时间
    last_autoanalyze,  -- 最后一次自动ANALYZE时间
    vacuum_count,      -- VACUUM次数
    autovacuum_count,  -- 自动VACUUM次数
    analyze_count,     -- ANALYZE次数
    autoanalyze_count  -- 自动ANALYZE次数
FROM pg_stat_user_tables
WHERE tablename = 'your_table';
```

### 8.2 表级统计信息的来源

```sql
-- 统计信息更新机制
-- 1. CREATE TABLE: reltuples = 0, relpages = 0
CREATE TABLE test_stats (id INT, value NUMERIC);

-- 检查初始统计
SELECT reltuples, relpages FROM pg_class WHERE relname = 'test_stats';
-- 结果: reltuples = 0, relpages = 0

-- 2. INSERT数据后立即查询: 统计信息仍为0
INSERT INTO test_stats SELECT generate_series(1, 10000), random() * 1000;
SELECT reltuples, relpages FROM pg_class WHERE relname = 'test_stats';
-- 结果: reltuples 仍为 0（需要ANALYZE）

-- 3. 执行ANALYZE: 更新统计信息
ANALYZE test_stats;
SELECT reltuples, relpages FROM pg_class WHERE relname = 'test_stats';
-- 结果: reltuples ≈ 10000, relpages ≈ 实际页数

-- 4. 大批量插入: 可能自动更新统计（autovacuum）
-- postgresql.conf 配置:
autovacuum = on
autovacuum_analyze_scale_factor = 0.1  -- 10%变化
autovacuum_analyze_threshold = 50      -- 至少50行
```

### 8.3 行数估计算法

**源码级分析**:

```cpp
// src/gausskernel/optimizer/util/plancat.c
// 函数: estimate_rel_size

void estimate_rel_size(
    Relation rel,
    BlockNumber* pages,
    double* tuples,
    BlockNumber* allvispages)
{
    BlockNumber curpages;
    double curtuples;
    BlockNumber curallvispages;
    
    // 方法1: 如果有精确的统计信息，使用统计
    if (rel->rd_rel->relpages > 0)
    {
        curpages = rel->rd_rel->relpages;
        curtuples = rel->rd_rel->reltuples;
        curallvispages = rel->rd_rel->relallvisible;
    }
    else
    {
        // 方法2: 没有统计信息，进行物理扫描
        curpages = RelationGetNumberOfBlocks(rel);
        curtuples = 0;  // 需要采样
        curallvispages = curpages;  // 假设都可见
        
        // 采样估计行数
        BlockNumber samplerows = 0;
        BlockNumber samplepages = Min(curpages, 1000);  // 最多采样1000页
        
        for (int i = 0; i < samplepages; i++)
        {
            Buffer buf = ReadBuffer(rel, i);
            Page page = BufferGetPage(buf);
            samplerows += PageGetMaxOffsetNumber(page);
            ReleaseBuffer(buf);
        }
        
        // 外推到全表
        curtuples = (double)samplerows * curpages / samplepages;
    }
    
    // 返回结果
    if (pages)
        *pages = curpages;
    if (tuples)
        *tuples = curtuples;
    if (allvispages)
        *allvispages = curallvispages;
}
```

**实际测试**:

```sql
-- 创建测试表
CREATE TABLE size_test AS
SELECT generate_series(1, 100000) AS id,
       repeat('x', 100) AS data;

-- 查看实际大小
SELECT 
    pg_relation_size('size_test') / 1024 / 1024 AS size_mb,
    pg_relation_size('size_test') / 8192 AS pages_exact;

-- 查看统计信息
ANALYZE size_test;
SELECT reltuples, relpages FROM pg_class WHERE relname = 'size_test';

-- 计算行宽
SELECT 
    sum(pg_column_size(t)) / count(*) AS avg_row_width
FROM size_test t;

-- 预估页数 = 总大小 / 页大小
SELECT reltuples * avg_row_width / 8192 AS estimated_pages;
```

---

## 9. 列级统计信息

### 9.1 基本列统计

```sql
-- 查看列级统计详细信息
SELECT 
    a.attname AS column_name,
    a.atttypid::regtype AS data_type,
    s.stanullfrac AS null_fraction,        -- NULL值比例
    s.stawidth AS avg_width,               -- 平均宽度 (字节)
    s.stadistinct AS n_distinct,           -- distinct值数量
    CASE 
        WHEN s.stadistinct > 0 THEN s.stadistinct
        WHEN s.stadistinct < 0 THEN (-s.stadistinct * reltuples)
        ELSE NULL
    END AS distinct_estimate,
    s.stakind1, s.stakind2, s.stakind3, s.stakind4  -- 统计类型
FROM pg_statistic s
JOIN pg_attribute a ON s.starelid = a.attrelid 
    AND s.staattnum = a.attnum
JOIN pg_class c ON c.oid = s.starelid
WHERE c.relname = 'your_table'
ORDER BY a.attnum;

-- 字段详解:
-- stanullfrac:  NULL值的比例 [0.0, 1.0]
--                0.0 = 没有NULL, 0.1 = 10%是NULL
--
-- stawidth:     平均列宽 (字节)
--                包括TOAST表的大字段
--
-- stadistinct:  distinct值的数量
--                正数: 精确值
--                负数: 比例 (例如 -0.5 表示 50% 的行是distinct)
--                0:    未知
--
-- stakind1-N:   统计信息类型
--                1 = MCV (Most Common Values)
--                2 = Histogram (直方图)
--                3 = Correlation (相关性)
```

### 9.2 MCV (Most Common Values) 统计

**MCV的作用**:

MCV用于存储出现频率最高的值及其频率。当列中某些值出现频率远高于其他值时，MCV统计非常有用。

**MCV存储结构**:

```cpp
// src/include/catalog/pg_statistic.h
// pg_statistic中的MCV数据:
// stakind1 = STATISTIC_KIND_MCV
// stavalues1 = {值1, 值2, 值3, ...}      // MCV值
// stanumbers1 = {频率1, 频率2, 频率3, ...} // 对应的频率

// 示例: 性别列的MCV
// stavalues1  = ['M', 'F']
// stanumbers1 = [0.51, 0.49]
// 解释: 51%是男性，49%是女性

// 示例: 国家代码列的MCV (前100个最常见的)
// stavalues1  = ['CN', 'US', 'JP', 'UK', ...]  // 最多100个值
// stanumbers1 = [0.35, 0.25, 0.15, 0.10, ...] // 对应频率
```

**查看MCV**:

```sql
-- 查看某列的MCV
SELECT 
    a.attname AS column_name,
    s.stavalues1 AS mcv_values,
    s.stanumbers1 AS mcv_frequencies
FROM pg_statistic s
JOIN pg_attribute a ON s.starelid = a.attrelid 
    AND s.staattnum = a.attnum
WHERE s.starelid = 'your_table'::regclass
  AND a.attname = 'your_column'
  AND s.stakind1 = 1;  -- 1 = MCV

-- 解析MCV数组
SELECT 
    (mcv_value).value AS column_value,
    (mcv_freq).num AS frequency
FROM (
    SELECT 
        unnest(s.stavalues1) AS mcv_value,
        unnest(s.stanumbers1) AS mcv_freq
    FROM pg_statistic s
    JOIN pg_attribute a ON s.starelid = a.attrelid 
        AND s.staattnum = a.attnum
    WHERE s.starelid = 'your_table'::regclass
      AND a.attname = 'your_column'
      AND s.stakind1 = 1
) sub
ORDER BY (mcv_freq).num DESC;

-- MCV判断标准
-- 如果某个值的出现频率 > 1 / (统计目标 * 统计采样率)
-- 则该值会被记录到MCV中
-- 例如: default_statistics_target = 100
--       如果某值频率 > 1/100 = 1%，则记录到MCV
```

**MCV对选择率的影响**:

```sql
-- 创建有偏数据的测试表
CREATE TABLE mcv_test (
    id INT,
    status VARCHAR(20)
);

-- 插入有偏数据: 90%是'active', 10%是'inactive'
INSERT INTO mcv_test
SELECT 
    generate_series(1, 100000),
    CASE WHEN random() < 0.9 THEN 'active' ELSE 'inactive' END
FROM generate_series(1, 100);

-- 收集统计
ANALYZE mcv_test;

-- 查看MCV
SELECT 
    unnest(stavalues1) AS value,
    unnest(stanumbers1) AS frequency
FROM pg_statistic s
JOIN pg_attribute a ON s.starelid = a.attrelid 
    AND s.staattnum = a.attnum
WHERE s.starelid = 'mcv_test'::regclass
  AND a.attname = 'status'
  AND s.stakind1 = 1;

-- 结果应该类似:
-- value    | frequency
-- ----------+-----------
-- active   | 0.9012    (约90%)
-- inactive | 0.0988    (约10%)

-- 测试选择率估计
EXPLAIN
SELECT * FROM mcv_test WHERE status = 'active';

-- 观察执行计划中的rows估计:
-- 应该接近: 100000 * 0.9 = 90000

EXPLAIN
SELECT * FROM mcv_test WHERE status = 'inactive';

-- rows估计: 100000 * 0.1 = 10000

EXPLAIN
SELECT * FROM mcv_test WHERE status = 'unknown';

-- rows估计: 'unknown'不在MCV里，直方图里也没有这个值
-- 回退到默认等值选择率 DEFAULT_EQ_SEL = 0.005
-- rows ≈ 100000 * 0.005 = 500

-- 三种情况的对比:
-- 值          选择率来源               rows估计
-- ----------  ----------------------  ---------
-- 'active'    MCV频率 0.90            90000
-- 'inactive'  MCV频率 0.10            10000
-- 'unknown'   DEFAULT_EQ_SEL 0.005    500
```

**MCV命中与未命中的源码逻辑**:

```cpp
// src/common/backend/utils/adt/selfuncs.cpp
// 函数: eqsel → var_eq_const (var = const 的选择率)

double var_eq_const(...)
{
    // 步骤1: 先查MCV
    // 在stavalues(MCV值数组)中查找常量值
    if (常量值命中MCV) {
        // 命中: 直接返回stanumbers中对应的频率
        // 这就是上面 'active' → 0.90 的来源
        return mcv频率;
    }

    // 步骤2: 未命中MCV，走直方图折算
    // 把MCV已经占掉的概率剔除，剩余概率按直方图中的
    // distinct数(otherdistinct)分摊
    // (源码: var_eq_const_histogram / var_eq_const_selectivity 辅助函数) ⚓待锚(精确归一化公式)

    // 步骤3: 连直方图都没有(列没有统计信息)
    // 退回保守默认值
    return DEFAULT_EQ_SEL;  // 0.005
}
```

这就是统计信息与选择率的全部关系: **MCV负责高频值的精确估计, 直方图负责剩余分布的粗估, 两者都没有时用保守默认值**。多个条件的行数估计聚合入口在 clausesel.cpp: `clauselist_selectivity` 把各条件的选择率相乘, `clause_selectivity` 按条件类型分派到 eqsel / ineqlsel 等具体函数。

---

## 12. Cost计算模型

前面各节回答的是"rows怎么来", 这一节回答"cost怎么来"。所有公式出自 costsize.cpp, 与第11节(选择率)衔接: 选择率决定rows, rows代入代价公式得到cost。

### 12.1 代价模型的统一形态

```text
total_cost = startup_cost + run_cost
startup_cost: 输出第一行之前必须花掉的成本 (排序、建哈希表、索引树下降)
run_cost:     输出剩余所有行的成本

成本单位是无量纲的: seq_page_cost = 1.0 是基准
一切磁盘成本和CPU成本最终都折算成"顺序读一页"的倍数
```

### 12.2 三条核心公式 (源码走读)

**公式1: 顺序扫描 cost_seqscan**:

```cpp
// src/gausskernel/optimizer/path/costsize.cpp
// 函数: cost_seqscan

startup_cost = qpqual_cost.startup;                     // 过滤条件的固定成本
run_cost     = spc_seq_page_cost × baserel->pages        // 磁盘: 顺序读完所有页
             + (cpu_tuple_cost + qpqual_cost.per_tuple)  // CPU: 每行 = 基础元组成本
             × baserel->tuples;                          //       + 每行过滤条件成本
total_cost   = startup_cost + run_cost;

// openGauss/VastBase的补充项:
// - SMP并行时: pages与tuples按dop均摊, 每个并行工作线程再加一笔smp_thread_cost
// - enable_seqscan = off: startup_cost += disable_cost (1.0e10)
//   并且total_cost再乘放大因子的平方 (双重惩罚, 见cost_seqscan收尾)
```

**公式2: 索引扫描 cost_index**:

```cpp
// src/gausskernel/optimizer/path/costsize.cpp
// 函数: cost_index
// 索引本身的访问成本由访问方法的估算函数给出 (B树: btcostestimate_internal)

// 步骤1: 索引访问成本 + 选择率 + 相关性
btcostestimate_internal(root, path, loop_count,
    &indexStartupCost, &indexTotalCost,     // 遍历索引页的成本
    &indexSelectivity, &indexCorrelation);  // 索引选择率 + 索引序与表序的相关性

// 步骤2: 估计要抓多少堆表行、对应多少堆表页
tuples_fetched = clamp_row_est(indexSelectivity × baserel->tuples);
pages_fetched  = index_pages_fetched(tuples_fetched, ...);  // Mackert-Lohman公式
                                                             // 把缓冲区命中的折减算进去

// 步骤3: 两个极端的堆表I/O成本
max_IO_cost = pages_fetched × random_page_cost;    // 完全不相关: 全是随机读
min_IO_cost = random_page_cost                     // 完全相关: 1次随机 + 其余顺序
            + (pages_fetched - 1) × seq_page_cost;

// 步骤4: 按 indexCorrelation的平方 在两个极端之间线性插值
csquared  = indexCorrelation × indexCorrelation;
run_cost += max_IO_cost + csquared × (min_IO_cost - max_IO_cost);

// 步骤5: CPU成本
run_cost += cpu_index_tuple_cost × 索引条目数
          + (cpu_tuple_cost + qpqual_cost.per_tuple) × tuples_fetched;

// 注: 作为JOIN内表被反复扫描时(loop_count > 1), 页数按扫描次数放大后
// 再按loop_count均摊, 全部按随机读计价
```

**公式3: 嵌套循环 final_cost_nestloop**:

```cpp
// src/gausskernel/optimizer/path/costsize.cpp
// 函数: final_cost_nestloop

// 处理的元组对数(不是输出行数!)
ntuples = outer_path_rows × inner_path_rows;

cpu_per_tuple = cpu_tuple_cost + 连接条件的per_tuple成本;
run_cost     += cpu_per_tuple × ntuples;

// 内层若是参数化索引扫描, 内层成本已按loop_count折算(见公式2的loop_count分支)
// SEMI/ANTI JOIN: 执行器命中第一个匹配就停,
// 用outer_matched_rows × inner_scan_frac折减ntuples
```

### 12.3 手工复算示例

**场景**: 表 t 有100万行、10000页(每页约100行), id列上有B树唯一索引:

```sql
-- 第1步: 查表级统计
SELECT reltuples, relpages FROM pg_class WHERE relname = 't';
-- reltuples = 1000000, relpages = 10000

-- 第2步: 算选择率与rows
-- id是唯一索引, 等值查询选择率 = 1/ndistinct = 1/1000000
-- rows = 1000000 × (1/1000000) = 1

-- 第3步: 复算顺序扫描成本 (假设被强制全表扫)
-- 磁盘 = seq_page_cost × pages = 1.0 × 10000 = 10000
-- CPU   = (cpu_tuple_cost + 1个等值操作符) × tuples
--       = (0.01 + 0.0025) × 1000000 = 12500
-- total ≈ 10000 + 12500 = 22500

-- 第4步: 复算索引扫描成本
-- 选择率 = 1/1000000 → tuples_fetched = 1, pages_fetched = 1
-- 唯一索引相关性接近1 (csquared ≈ 1, 走min_IO分支)
-- 堆表I/O ≈ 1 × random_page_cost = 4.0
-- 加上索引树下降(约3~4层, 每层随机读)与少量CPU成本
-- total ≈ 16 ~ 20

-- 结论: 两个数量级的差距, 优化器必然选索引扫描
-- 这就是EXPLAIN输出中 cost=0.43..8.29 这类数字的来源
```

### 12.4 rows与cost的传播链

```text
统计信息 (pg_class + pg_statistic)
    → clauselist_selectivity 算出各条件选择率          (clausesel.cpp)
    → set_baserel_size_estimates 设定 baserel->rows     (costsize.cpp)
    → cost_*函数族把rows/tuples代入公式, 得到每条Path的startup/total_cost
    → set_rel_path_rows 把行数挂到path->rows            (每条路径与其父关系同基数)
    → JOIN层: approx_tuple_count用连接条件选择率继续折算 (selfuncs.cpp)
    → 自底向上: 每个计划节点的cost = 子节点cost + 本节点增量成本
```

EXPLAIN 中每个节点显示的 rows 是该节点的输出行数, cost 是"从该节点的输入起累计到输出完成"的总代价, 所以子节点的cost必然不超过父节点, 计划树上cost单调递增。

---

## 总结

- 统计信息分两层: pg_class 的 reltuples/relpages/relallvisible 是表级基数; pg_statistic 的 stanullfrac/stawidth/stadistinct 加 stakind槽位(MCV/直方图/相关性)是列级分布。ANALYZE 采样行数约为 default_statistics_target × 30。
- 等值条件的选择率先查MCV(命中直接用频率), 未命中用直方图折算, 都没有退到 DEFAULT_EQ_SEL = 0.005 (selfuncs.cpp / eqsel → var_eq_const)。
- 行数估计 = 表行数 × 各条件选择率连乘, 聚合入口是 clausesel.cpp / clauselist_selectivity。
- 代价模型 = 磁盘页成本 + CPU成本, 以 seq_page_cost = 1.0 为基准单位; 五个cost参数默认值定义在 cost.h, GUC注册在 guc/guc_sql.cpp。
- cost_seqscan: 页数×seq_page_cost + (cpu_tuple_cost+条件成本)×行数; cost_index: btcostestimate估索引成本, Mackert-Lohman公式估堆表页数, 按indexCorrelation平方在max_IO/min_IO之间插值; final_cost_nestloop: 按外层×内层元组对数计CPU。
- 手工复算四步: 查统计 → 算选择率 → 算rows → 套公式; 子节点cost是父节点cost的组成部分。
- openGauss/VastBase在标准模型上加了SMP并行(页数与行数按dop均摊)、enable_*关闭时的disable_cost(1.0e10)双重惩罚等扩展。

## 版本注记

- 2026-11-05 断尾补齐: 本篇原文在500行处截断(截于9.2节MCV的EXPLAIN示例中间), 本次顺接补完该示例并收尾9.2节, 按目录中已有的第12节标题补写"Cost计算模型"一节。
- 原目录中第10节(索引统计信息)、第11节(选择率计算)、第13节(执行计划Cost分析实例)的原文未随截断保留, 不虚构重建, 其核心内容已并入第12节。

## 来源

- 源码基线: openGauss-server-v6.0.0。本篇引用: src/gausskernel/optimizer/path/costsize.cpp(cost_seqscan / cost_index / btcostestimate_internal / final_cost_nestloop / cost_qual_eval / set_baserel_size_estimates / clamp_row_est), src/gausskernel/optimizer/path/clausesel.cpp(clauselist_selectivity / clause_selectivity), src/common/backend/utils/adt/selfuncs.cpp(eqsel / var_eq_const), src/include/optimizer/cost.h(默认值常量), src/include/utils/selfuncs.h(DEFAULT_EQ_SEL)。
- 本篇原文: 2026-04 学习档案(VastBase/openGauss 统计信息与Cost计算超详细深度分析)。
