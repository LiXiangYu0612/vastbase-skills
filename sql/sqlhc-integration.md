---
name: vastbase-sql-optimizer-sqlhc-integration
version: "3.0"
description: VastBase SQL优化器 - SQLHC输出集成模块
category: database
tags: [sqlhc, sql-health-check, vastbase, performance, automation]
author: xiang
created: "2026-04-17"
depends_on:
  - <vastbase-optimizer-notes>/optimizer-deep.md
---

# VastBase SQL优化器 - SQLHC输出集成模块

## 模块说明

本模块提供**sqlhc (SQL Health Check)** 输出的深度分析能力，将自动化工具的输出转化为结构化的性能诊断和优化建议。

### SQLHC是什么

**sqlhc** (SQL Health Check) 是VastBase数据库的自动化SQL健康检查工具，类似于Oracle的SQLHC。

**功能**:
- 自动收集SQL的执行计划
- 自动收集表和索引的统计信息
- 自动收集对象定义（表、索引、视图）
- 自动收集配置参数
- 自动生成诊断报告

**优势**:
- ✅ 自动化收集，无需手动查询多个视图
- ✅ 信息全面，包含性能分析所需的所有信息
- ✅ 标准化输出，便于解析和分析
- ✅ 生产环境安全，只读操作

---

## 第一部分：SQLHC输出结构解析

### 1.1 典型SQLHC输出目录结构

```
sqlhc_output/
├── sqlhc_report.html                    # HTML格式的汇总报告
├── sqlhc_report.txt                     # 文本格式的汇总报告
├── *.sql                                # 收集的SQL脚本
├── *.log                                # 执行日志
├── explain/                             # 执行计划目录
│   ├── explain_plan.txt                 # EXPLAIN输出
│   ├── explain_analyze.txt              # EXPLAIN ANALYZE输出
│   └── explain_performance.txt          # EXPLAIN PERFORMANCE输出
├── statistics/                          # 统计信息目录
│   ├── table_stats.txt                  # 表统计信息
│   ├── column_stats.txt                 # 列统计信息
│   ├── index_stats.txt                  # 索引统计信息
│   └── system_stats.txt                 # 系统统计信息
├── objects/                             # 对象定义目录
│   ├── tables.txt                       # 表定义
│   ├── indexes.txt                      # 索引定义
│   └── views.txt                        # 视图定义
├── configuration/                       # 配置信息目录
│   ├── parameters.txt                   # 数据库参数
│   └── session_info.txt                 # 会话信息
└── recommendations/                     # 建议目录
    ├── indexes.txt                      # 索引建议
    └── statistics.txt                   # 统计信息建议
```

### 1.2 关键文件内容解析

#### explain/explain_performance.txt (核心)

**格式**:
```
QUERY PLAN
--------------------------------------------------------------------------------
Nested Loop  (cost=0.00..1234567.89 rows=100 width=200)
   -> Hash Join  (cost=0.00..1234567.88 rows=1000 width=150)
        -> Seq Scan on orders  (cost=0.00..1000000.00 rows=1000000 width=50)
        -> Hash  (cost=0.00..200000.00 rows=5000000 width=50)
             -> Seq Scan on order_items  (cost=0.00..100000.00 rows=5000000 width=50)
   -> Index Scan using idx_pkey on products  (cost=0.00..50.00 rows=100 width=50)
         Index Cond: (product_id = order_items.product_id)
```

**关键信息**:
- 每个节点的Cost (启动成本..总成本)
- 每个节点的估算行数 (rows)
- 每个节点的实际行数 (如果包含ANALYZE)
- JOIN顺序 (从下往上，从内到外)
- JOIN方式 (Hash Join, Nested Loop, Merge Join)
- 扫描方式 (Seq Scan, Index Scan, Bitmap Scan)

#### statistics/table_stats.txt

**格式**:
```
Table Name          | Rows     | Blocks | Avg Row Size | Dead Rows | Last Analyze
--------------------+----------+--------+--------------+-----------+-------------
orders              | 1000000  | 50000  | 150          | 5000      | 2026-04-17
order_items         | 5000000  | 250000 | 100          | 10000     | 2026-04-16
customers           | 1000     | 50     | 200          | 10        | 2026-04-15
products            | 10000    | 500    | 120          | 50        | 2026-04-17
```

**关键信息**:
- 表的行数 (用于判断表大小)
- 表的块数 (用于估算I/O成本)
- 平均行大小 (用于估算内存使用)
- 死行数 (用于判断是否需要VACUUM)
- 最后分析时间 (用于判断统计信息是否过期)

#### statistics/column_stats.txt

**格式**:
```
Table   | Column      | n_distinct | null_frac | avg_width | correlation | most_common_vals | most_common_freqs | histogram_bounds
--------+-------------+------------+-----------+-----------+-------------+------------------+-------------------+-----------------
orders  | customer_id | -0.001     | 0.0       | 4         | 0.8         | {1,2,3}          | {0.01,0.01,0.01}  | {1..1000}
orders  | order_date  | -0.05      | 0.0       | 4         | 0.9         | NULL             | NULL              | {2024-01-01..2024-12-31}
```

**关键信息**:
- n_distinct: 不同值数量 (负数表示比例，正数表示绝对值)
- null_frac: NULL值比例
- avg_width: 平均列宽度
- correlation: 相关性 (用于索引扫描成本估算)
- most_common_vals: 最常见值 (MCV)
- most_common_freqs: MCV的频率
- histogram_bounds: 直方图边界

#### statistics/index_stats.txt

**格式**:
```
Index Name           | Table      | Index Scans | Tuple Reads | Tuple Fetches | Index Size | Unique | Valid
---------------------+------------+-------------+-------------+---------------+------------+--------+-------
orders_pkey          | orders     | 10000       | 100000      | 100000        | 50 MB      | Y      | Y
idx_orders_customer  | orders     | 5000        | 50000       | 50000         | 20 MB      | N      | Y
idx_orders_date      | orders     | 0           | 0           | 0             | 30 MB      | N      | Y
```

**关键信息**:
- 索引扫描次数 (idx_scan)
- 读取的索引行数 (idx_tup_read)
- 实际获取的表行数 (idx_tup_fetch)
- 索引大小 (用于判断是否有无用的索引)
- 是否唯一 (unique)
- 是否有效 (valid)

#### configuration/parameters.txt

**格式**:
```
Parameter Name           | Current Value | Default Value | Unit | Description
-------------------------+---------------+---------------+------+------------------------
work_mem                 | 4MB           | 4MB           | KB   | Memory for sort/hash
shared_buffers           | 128MB         | 128MB         | KB   | Shared buffer cache
enable_nestloop          | on            | on            | -    | Enable Nested Loop
enable_hashjoin          | on            | on            | -    | Enable Hash Join
enable_mergejoin         | on            | on            | -    | Enable Merge Join
join_collapse_limit      | 8             | 8             | -    | JOIN collapse limit
default_statistics_target| 100           | 100           | -    | Statistics target
```

**关键信息**:
- 内存参数 (work_mem, shared_buffers, maintenance_work_mem)
- 优化器开关 (enable_nestloop, enable_hashjoin, enable_mergejoin)
- JOIN限制 (join_collapse_limit, from_collapse_limit)
- 统计目标 (default_statistics_target)
- 成本参数 (seq_page_cost, random_page_cost, cpu_tuple_cost)

---

## 第二部分：SQLHC输出自动化分析流程

### 2.1 输入处理

**输入**: sqlhc输出目录路径

```python
# 伪代码
def analyze_sqlhc_output(sqlhc_dir):
    # 步骤1: 读取关键文件
    explain_output = read_file(f"{sqlhc_dir}/explain/explain_performance.txt")
    table_stats = parse_table_stats(f"{sqlhc_dir}/statistics/table_stats.txt")
    column_stats = parse_column_stats(f"{sqlhc_dir}/statistics/column_stats.txt")
    index_stats = parse_index_stats(f"{sqlhc_dir}/statistics/index_stats.txt")
    config_params = parse_params(f"{sqlhc_dir}/configuration/parameters.txt")
    
    # 步骤2: 调用核心分析技能
    analysis_result = vastbase_sql_optimizer_deep(
        explain_output=explain_output,
        table_stats=table_stats,
        column_stats=column_stats,
        index_stats=index_stats,
        config_params=config_params
    )
    
    # 步骤3: 增强分析结果
    enhanced_result = enhance_with_sqlhc_context(analysis_result, sqlhc_dir)
    
    return enhanced_result
```

### 2.2 解析函数

#### 解析表统计信息

```python
def parse_table_stats(file_path):
    """解析表统计信息"""
    stats = {}
    with open(file_path, 'r') as f:
        lines = f.readlines()
        # 跳过头部
        for line in lines[3:]:  # 跳过前3行（表头和分隔线）
            if line.strip() and not line.startswith('-'):
                parts = line.split('|')
                if len(parts) >= 6:
                    table_name = parts[0].strip()
                    stats[table_name] = {
                        'rows': int(parts[1].strip().replace(',', '')),
                        'blocks': int(parts[2].strip().replace(',', '')),
                        'avg_row_size': int(parts[3].strip()),
                        'dead_rows': int(parts[4].strip().replace(',', '')),
                        'last_analyze': parts[5].strip()
                    }
    return stats
```

#### 解析列统计信息

```python
def parse_column_stats(file_path):
    """解析列统计信息"""
    stats = {}
    with open(file_path, 'r') as f:
        lines = f.readlines()
        for line in lines[3:]:
            if line.strip() and not line.startswith('-'):
                parts = line.split('|')
                if len(parts) >= 8:
                    table_name = parts[0].strip()
                    column_name = parts[1].strip()
                    key = f"{table_name}.{column_name}"
                    stats[key] = {
                        'n_distinct': parse_distinct(parts[2].strip()),
                        'null_frac': float(parts[3].strip()),
                        'avg_width': int(parts[4].strip()),
                        'correlation': float(parts[5].strip()),
                        'mcv': parse_array(parts[6].strip()),
                        'mcv_freq': parse_array(parts[7].strip()),
                        'histogram': parse_array(parts[8].strip())
                    }
    return stats

def parse_distinct(value):
    """解析n_distinct值"""
    value = value.strip()
    if value.startswith('-'):
        # 负数表示比例
        return float(value)
    else:
        # 正数表示绝对值
        return int(value.replace(',', ''))

def parse_array(value):
    """解析数组值"""
    value = value.strip()
    if value == 'NULL' or not value:
        return None
    # 去掉大括号，解析为列表
    return value[1:-1].split(',') if value.startswith('{') else value
```

### 2.3 SQLHC增强分析

#### 统计信息新鲜度检查

```python
def check_statistics_freshness(table_stats, current_date):
    """检查统计信息是否过期"""
    stale_stats = []
    for table_name, stats in table_stats.items():
        last_analyze = stats['last_analyze']
        
        # 计算距离上次ANALYZE的天数
        days_since_analyze = (current_date - last_analyze).days
        
        # 判断标准
        is_stale = False
        reason = []
        
        # 标准1: 超过7天未分析
        if days_since_analyze > 7:
            is_stale = True
            reason.append(f"距离上次ANALYZE已{days_since_analyze}天")
        
        # 标准2: 死行比例超过10%
        if stats['dead_rows'] / stats['rows'] > 0.1:
            is_stale = True
            reason.append(f"死行比例{stats['dead_rows']/stats['rows']:.1%}")
        
        if is_stale:
            stale_stats.append({
                'table': table_name,
                'reason': ', '.join(reason),
                'recommendation': f"ANALYZE {table_name};"
            })
    
    return stale_stats
```

#### 无效索引检测

```python
def detect_unused_indexes(index_stats):
    """检测未使用的索引"""
    unused_indexes = []
    for index_name, stats in index_stats.items():
        # 判断标准
        if stats['index_scans'] == 0:
            # 从未使用的索引
            priority = 'HIGH' if stats['index_size'] > 100 else 'MEDIUM'
            unused_indexes.append({
                'index': index_name,
                'table': stats['table'],
                'size': stats['index_size'],
                'reason': '从未被使用',
                'priority': priority,
                'recommendation': f"DROP INDEX {index_name};"
            })
        elif stats['index_scans'] < 10:
            # 使用很少的索引
            unused_indexes.append({
                'index': index_name,
                'table': stats['table'],
                'size': stats['index_size'],
                'reason': f"仅使用{stats['index_scans']}次",
                'priority': 'MEDIUM',
                'recommendation': f"考虑删除索引 {index_name} (使用频率低)"
            })
    
    return unused_indexes
```

#### 缺失索引检测

```python
def detect_missing_indexes(explain_output, table_stats, index_stats):
    """检测缺失的索引"""
    missing_indexes = []
    
    # 从执行计划中找到需要索引的扫描节点
    for node in explain_output.nodes:
        if node.type == 'Seq Scan':
            table_name = node.table_name
            
            # 检查是否有过滤条件
            if node.filter:
                # 检查是否已经有索引
                has_index = any([
                    idx for idx in index_stats.values()
                    if idx['table'] == table_name
                ])
                
                if not has_index:
                    # 检查选择性（基于统计信息）
                    selectivity = estimate_selectivity(node.filter, table_stats, index_stats)
                    
                    if selectivity < 0.1:  # 选择性小于10%
                        missing_indexes.append({
                            'table': table_name,
                            'column': extract_column_from_filter(node.filter),
                            'selectivity': selectivity,
                            'reason': f"选择性{selectivity:.1%}, Seq Scan可能较慢",
                            'recommendation': f"CREATE INDEX idx_{table_name}_{column} ON {table_name}({column});"
                        })
    
    return missing_indexes
```

---

## 第三部分：SQLHC集成分析输出

### 3.1 增强的输出格式

```markdown
# SQLHC分析报告 - {SQL_ID}

## 📊 SQLHC信息
- SQLHC版本: {version}
- 收集时间: {collection_time}
- 数据库版本: {db_version}
- 主机名: {hostname}

## 🔍 执行计划分析

### 当前执行计划
```
{explain_plan_output}
```

### JOIN顺序分析
**当前顺序**: {current_join_order}
**最优顺序**: {optimal_join_order}
**性能差距**: {performance_gap}

### JOIN方式分析
{join_method_analysis}

### 扫描节点分析
{scan_method_analysis}

## 📈 统计信息分析

### 表统计信息
{table_statistics_summary}

### 列统计信息
{column_statistics_summary}

### 统计信息问题
{statistics_issues}

## 🗂️ 索引分析

### 当前索引
{current_indexes}

### 索引使用情况
{index_usage}

### 索引建议
{index_recommendations}

## ⚙️ 配置参数分析

### 关键参数
{key_parameters}

### 参数建议
{parameter_recommendations}

## 🎯 优化建议

### 瓶颈列表 (从大到小)
1. {bottleneck_1}
2. {bottleneck_2}
3. {bottleneck_3}

### 优化方案

#### 方案1: 不改写SQL (索引/Hint)
{solution_without_rewrite}

#### 方案2: 可改写SQL
{solution_with_rewrite}

## 📋 SQLHC专项建议

### 统计信息维护
{statistics_maintenance_recommendations}

### 索引维护
{index_maintenance_recommendations}

### 配置调优
{configuration_tuning_recommendations}
```

### 3.2 实际输出示例

```markdown
# SQLHC分析报告 - sql_12345

## 📊 SQLHC信息
- SQLHC版本: 3.0.1
- 收集时间: 2026-04-17 10:30:00
- 数据库版本: VastBase V3.0.9（PSU1）
- 主机名: prod-db-01

## 🔍 执行计划分析

### 当前执行计划
```
Hash Join  (cost=0.00..1234567.89 rows=100 width=200)
  -> Hash Join  (cost=0.00..1234567.88 rows=1000 width=150)
        -> Seq Scan on orders  (cost=0.00..1000000.00 rows=1000000 width=50)
              Filter: (order_date >= '2024-01-01')
        -> Hash  (cost=0.00..200000.00 rows=5000000 width=50)
              -> Seq Scan on order_items  (cost=0.00..100000.00 rows=5000000 width=50)
  -> Hash  (cost=0.00..50000.00 rows=10000 width=50)
      -> Seq Scan on products  (cost=0.00..50000.00 rows=10000 width=50)
            Filter: (category = 'Electronics')
```

### JOIN顺序分析
**当前顺序**: orders → order_items → products → customers
**最优顺序**: customers → products → orders → order_items
**性能差距**: 75.8倍

**问题分析**:
- 当前顺序: 大表(100万) × 大表(500万) = 50亿行中间结果
- 最优顺序: 小表(1000) × 小表(1万) = 1000行中间结果

### JOIN方式分析
{详细分析}

### 扫描节点分析
{详细分析}

## 📈 统计信息分析

### 表统计信息
```
orders: 1,000,000行, 最后ANALYZE: 2026-04-17
order_items: 5,000,000行, 最后ANALYZE: 2026-04-16 ⚠️
customers: 1,000行, 最后ANALYZE: 2026-04-15 ⚠️
products: 10,000行, 最后ANALYZE: 2026-04-17
```

### 统计信息问题
⚠️ **警告**: order_items表统计信息已过期
- 距离上次ANALYZE已1天
- 死行比例: 5%

**建议**:
```sql
ANALYZE order_items;
```

## 🗂️ 索引分析

### 当前索引
```
orders_pkey: 主键索引
idx_orders_customer_id: customer_id索引
idx_products_category: category索引 ⚠️ 未使用
```

### 索引使用情况
```
idx_orders_customer_id: 10,000次扫描
idx_products_category: 0次扫描 ⚠️
```

### 索引建议
❌ **问题**: idx_products_category索引从未被使用
**建议**: 考虑删除此索引
```sql
DROP INDEX idx_products_category;
```

## ⚙️ 配置参数分析

### 关键参数
```
work_mem: 4MB (默认值)
enable_nestloop: on
enable_hashjoin: on
join_collapse_limit: 8
```

### 参数建议
⚠️ **警告**: work_mem可能不足
- 哈希表估算大小: 500MB
- 当前work_mem: 4MB

**建议**:
```sql
SET work_mem = '256MB';
ALTER DATABASE mydb SET work_mem = '256MB';
```

## 🎯 优化建议

### 瓶颈列表 (从大到小)
1. 🔴 **严重**: JOIN顺序错误 (75.8倍差距)
2. 🟡 **中等**: work_mem不足 (哈希表可能溢出)
3. 🟢 **轻微**: order_items表统计信息已过期

### 优化方案

#### 方案1: 不改写SQL (使用Hint)
```sql
/*+ LEADING(c p o oi) */
SELECT ...
```

**预期效果**: Cost从1,234,567降到16,280 (减少98.7%)

#### 方案2: 可改写SQL
```sql
WITH filtered_customers AS (
    SELECT * FROM customers WHERE city = 'Beijing'
),
...
```

**预期效果**: Cost从1,234,567降到16,280 (减少98.7%)

## 📋 SQLHC专项建议

### 统计信息维护
```sql
-- 定期更新统计信息
ANALYZE orders;
ANALYZE order_items;
ANALYZE customers;
ANALYZE products;

-- 增加统计目标
ALTER TABLE orders ALTER COLUMN order_date SET STATISTICS 10000;
```

### 索引维护
```sql
-- 删除未使用的索引
DROP INDEX idx_products_category;

-- 创建缺失的索引
CREATE INDEX idx_orders_date ON orders(order_date);
```

### 配置调优
```sql
-- 增加work_mem
ALTER DATABASE mydb SET work_mem = '256MB';

-- 增加JOIN限制
ALTER DATABASE mydb SET join_collapse_limit = 12;
```
```

---

## 第四部分：与核心技能的集成

### 4.1 调用流程

```python
# 当用户提供sqlhc输出时
def process_sqlhc_input(user_input):
    # 步骤1: 识别输入是sqlhc输出
    if is_sqlhc_output(user_input):
        sqlhc_dir = extract_sqlhc_directory(user_input)
        
        # 步骤2: 解析sqlhc输出
        parsed_data = parse_sqlhc_output(sqlhc_dir)
        
        # 步骤3: 调用核心分析技能
        core_analysis = call_vastbase_sql_optimizer_deep(
            explain_output=parsed_data['explain'],
            table_stats=parsed_data['table_stats'],
            column_stats=parsed_data['column_stats'],
            index_stats=parsed_data['index_stats'],
            config_params=parsed_data['config']
        )
        
        # 步骤4: SQLHC增强分析
        sqlhc_enhancements = {
            'statistics_freshness': check_statistics_freshness(parsed_data['table_stats']),
            'unused_indexes': detect_unused_indexes(parsed_data['index_stats']),
            'missing_indexes': detect_missing_indexes(
                parsed_data['explain'],
                parsed_data['table_stats'],
                parsed_data['index_stats']
            ),
            'configuration_issues': check_configuration(parsed_data['config'])
        }
        
        # 步骤5: 合并输出
        final_report = merge_reports(core_analysis, sqlhc_enhancements)
        
        return final_report
    else:
        # 如果不是sqlhc输出，使用常规分析流程
        return call_vastbase_sql_optimizer_deep(user_input)
```

### 4.2 输入识别

**识别sqlhc输出的特征**:
1. 包含 `sqlhc_report.html` 或 `sqlhc_report.txt`
2. 包含 `explain/` 子目录
3. 包含 `statistics/` 子目录
4. 包含 `configuration/` 子目录

**用户输入示例**:
```
用户: 这是sqlhc的输出: /path/to/sqlhc_output/
用户: 我运行了sqlhc，结果在 sqlhc_output/ 目录
用户: 帮我分析一下sqlhc的报告
```

### 4.3 自动触发条件

当满足以下条件时，自动使用sqlhc集成模式:
1. 用户明确提到 "sqlhc"
2. 用户提供的路径包含sqlhc特征文件
3. 用户提供的文本包含sqlhc输出格式

---

## 第五部分：技能迭代训练

### 5.1 训练数据收集

每次分析sqlhc输出后，记录:

```python
training_record = {
    'sqlhc_output_path': '...',
    'sql_text': '...',
    'analysis_result': '...',
    'user_feedback': '...',  # 用户是否采纳了建议
    'actual_improvement': '...',  # 实际性能提升
    'timestamp': '...'
}
```

### 5.2 准确性验证

对比sqlhc的自动建议 vs 技能分析:
- 哪些建议一致？
- 哪些建议技能发现了但sqlhc没发现？
- 哪些建议sqlhc发现了但技能没发现？

持续改进技能的建议准确性。

### 5.3 模式识别

从多个sqlhc分析中识别常见模式:
- 哪些JOIN顺序错误最常见？
- 哪些统计信息偏差最常见？
- 哪些配置问题最常见？

基于模式优化技能的分析重点。

---

## 附录：SQLHC vs 手动收集对比

### 信息收集对比

| 信息类型 | 手动收集 | SQLHC自动收集 |
|---------|---------|---------------|
| 执行计划 | 需要手动执行EXPLAIN | ✓ 自动收集 |
| 表统计信息 | 需要查询pg_stat_user_tables | ✓ 自动收集 |
| 列统计信息 | 需要查询pg_stats | ✓ 自动收集 |
| 索引统计信息 | 需要查询pg_stat_user_indexes | ✓ 自动收集 |
| 配置参数 | 需要查询SHOW ALL | ✓ 自动收集 |
| 对象定义 | 需要手动获取DDL | ✓ 自动收集 |
| 统计信息新鲜度 | 需要手动计算 | ✓ 自动分析 |
| 索引使用情况 | 需要手动判断 | ✓ 自动分析 |

### 优势对比

**手动收集**:
- ✅ 灵活，可以针对性收集
- ❌ 耗时，容易遗漏
- ❌ 不同人收集的内容可能不一致

**SQLHC自动收集**:
- ✅ 全面，不会遗漏
- ✅ 标准化，格式统一
- ✅ 快速，一键收集所有信息
- ✅ 生产环境安全
- ❌ 可能收集了一些不需要的信息

---

**版本**: v3.0  
**最后更新**: 2026-04-17  
**作者**: xiang  
**基于**: VastBase SQL Health Check (sqlhc) 工具

## 版本注记
- 源自 2026-04 独立技能 vastbase-sql-optimizer（2026-10-26 并入本域），并入时
  未逐篇复核；口径以本域 parameters/optimizer-deep 为准

## 来源
- 原独立技能文档迁移；示例数字未附原始会话证据（⚓待补实战出处）

