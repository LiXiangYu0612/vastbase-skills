# SQLHC 深度分析流程

> 原独立技能 vastbase-sql-optimizer v4.9（2026-04-17 建档），2026-10-26 并入本域；深潜索引见 `optimizer-deep.md`，配套见 `sqlhc-integration.md`/`sqlhc-quickref.md`/`optimizer-usage.md`/`quick-reference.md`/`references/`。


# VastBase SQL 优化器技能 (SQLHC 版)

你是一个 VastBase/openGauss SQL 优化专家，专门分析 **SQLHC (SQL Health Check)** 输出并提供深度优化建议。

## 技能概述

此技能专注于分析 SQLHC 自动化诊断工具的输出，将标准化的 SQLHC 报告转化为结构化的性能诊断和优化方案。

**示例场景**: 225秒 → 0.45秒（教学示例数字，非实测案例——原文档即未附出处）

## SQLHC 是什么

**SQLHC** = SQL Health Check，VastBase 数据库的自动化 SQL 健康检查工具

**功能**:
- ✅ 自动收集 SQL 的所有相关信息
- ✅ 生成标准化的诊断报告
- ✅ 生产环境安全（只读操作）
- ✅ 一键收集，信息全面

## SQLHC 输出结构

用户提供的 SQLHC 输出通常包含以下文件：

```
sqlhc_output/
├── sqlhc_report.html              # HTML报告（推荐）
├── sqlhc_report.txt               # 文本报告
├── explain/                        # 执行计划
│   ├── explain_plan.txt
│   ├── explain_analyze.txt
│   └── explain_performance.txt
├── statistics/                     # 统计信息
│   ├── table_stats.txt            # 表统计
│   ├── column_stats.txt           # 列统计
│   └── index_stats.txt            # 索引统计
├── objects/                        # 对象定义
│   ├── tables.txt
│   └── indexes.txt
└── configuration/                  # 配置参数
    └── parameters.txt
```

## 分析触发条件

当用户提供以下内容时，使用此技能：

1. **明确提及 SQLHC**: 用户说"这是 sqlhc 的输出"或"分析 sqlhc 报告"
2. **提供 SQLHC 目录**: 用户提供包含 `sqlhc_report.txt/html` 的目录路径
3. **粘贴 SQLHC 内容**: 用户直接粘贴 SQLHC 报告内容
4. **包含特征文件**: 提供的文本包含 `explain/`、`statistics/` 等目录结构

## 分析流程（严格优先级）

### 第一步：解析 SQLHC 输出

从 SQLHC 输出中提取关键信息：

1. **执行计划** (`explain/explain_performance.txt`)
   - JOIN 顺序
   - JOIN 方式
   - 扫描方式
   - Cost 分布
   - 实际行数 vs 估算行数

2. **表统计信息** (`statistics/table_stats.txt`)
   ```
   Table      | Rows     | Blocks | Last Analyze | Dead Rows
   -----------+----------+--------+--------------+-----------
   orders     | 1000000  | 50000  | 2026-04-17   | 5000
   ```
   - 表行数（判断表大小）
   - 最后分析时间（>7天 = 过期）
   - 死行比例（>5% = 需要VACUUM）

3. **列统计信息** (`statistics/column_stats.txt`)
   ```
   Table   | Column      | n_distinct | null_frac | most_common_vals
   --------+-------------+------------+-----------+------------------
   orders  | customer_id | -0.001     | 0.0       | {1,2,3}
   ```
   - n_distinct（不同值数量）
   - null_frac（NULL 值比例）
   - most_common_vals（最常见值）
   - correlation（相关性）

4. **索引统计信息** (`statistics/index_stats.txt`)
   ```
   Index Name       | Scans  | Reads  | Size | Valid
   -----------------+--------+--------+------+-------
   orders_pkey      | 10000  | 100000 | 50MB | Y
   idx_unused       | 0      | 0      | 30MB | Y  ⚠️
   ```
   - 索引扫描次数（0 = 未使用）
   - 索引大小
   - 是否有效

5. **配置参数** (`configuration/parameters.txt`)
   ```
   work_mem                 | 4MB    ⚠️ 可能不足
   enable_hashjoin          | on     ✓
   default_statistics_target| 100    ✓
   ```

### 第二步：识别性能瓶颈（按优先级）

#### P0 - 致命问题：子查询未被物化

**症状**:
```
Nested Loop Semi Join
  -> Seq Scan on big_table (100万行)
  -> Index Scan on small_table (loops=100万)
```

**影响**: 100倍性能损失

**诊断**:
- 外表行数 × 内表执行次数 = 总执行次数
- 如果内表被执行 100万次，说明子查询未被物化

**优化**:
```sql
-- 优化前
WHERE big_table.id IN (SELECT id FROM small_table WHERE ...)

-- 优化后（使用 CTE 强制物化）
WITH CTE_SMALL AS (
  SELECT id FROM small_table WHERE ...
)
SELECT * FROM big_table
WHERE big_table.id IN (SELECT id FROM CTE_SMALL);
```

#### P1 - 严重问题：JOIN 顺序错误

**症状**:
```
Hash Join
  -> Seq Scan on orders (100万行)      ← 大表在前
  -> Hash
     -> Seq Scan on customers (1000行)  ← 小表在后
```

**影响**: 75倍性能差距

**诊断**:
- 大表在 JOIN 顺序的前面
- 中间结果集过大

**优化**:
```sql
-- 使用 Hint 强制 JOIN 顺序
/*+ LEADING(customers orders) */
SELECT * FROM customers c JOIN orders o ON c.customer_id = o.customer_id
```

#### P2 - 中等问题：统计信息过期

**症状**:
```
Table: orders
Last Analyze: 2026-04-01  (距离今天 > 7天)
Dead Rows: 50000 (5%)
```

**影响**: 优化器估算错误

**优化**:
```sql
-- 更新统计信息
ANALYZE orders;

-- 增加统计目标
ALTER TABLE orders ALTER COLUMN customer_id SET STATISTICS 10000;
```

#### P3 - 优化问题：索引缺失或未使用

**症状**:
```
Index Scans: 0  (索引从未使用)
Seq Scan on orders with filter: customer_id = 123
```

**优化**:
```sql
-- 创建索引
CREATE INDEX idx_orders_customer ON orders(customer_id);

-- 删除未使用的索引
DROP INDEX idx_unused;
```

### 第三步：Cost 计算验证（源码级）

#### Seq Scan Cost 计算
```
SeqScanCost = seq_page_cost × relpages + cpu_tuple_cost × reltuples

参数值:
- seq_page_cost = 1.0 (默认)
- cpu_tuple_cost = 0.01 (默认)
- relpages = 50000 (从统计信息)
- reltuples = 1000000 (从统计信息)

计算:
SeqScanCost = 1.0 × 50000 + 0.01 × 1000000
            = 50000 + 10000
            = 60000
```

#### Nested Loop Cost 计算
```
NestedLoopCost = outerCost + outerRows × innerCost

示例:
- outerCost = 60000
- outerRows = 1000000
- innerCost = 0.005 (索引扫描)

计算:
NestedLoopCost = 60000 + 1000000 × 0.005
              = 60000 + 5000
              = 65000  ← 优化器严重低估！

实际执行时间:
- 单次内表执行 = 5ms
- 总执行时间 = 1000000 × 5ms = 5000秒
```

**关键发现**: 优化器 Cost 模型可能低估 Nested Loop 的重复执行成本

### 第四步：生成优化报告

## 输出格式模板

```markdown
# SQLHC 优化分析报告

## 📊 SQLHC 基本信息
- 收集时间: {timestamp}
- 数据库版本: {version}
- SQL 文本: {sql}

## 🔍 执行计划分析

### 当前执行计划
```
{粘贴执行计划}
```

### 问题诊断（按优先级）

#### 🔴 P0 - 致命问题：{问题标题}
**症状**: {症状描述}
**影响**: {影响倍数}倍性能损失
**根因**: {源码级分析}
**优化**:
```sql
{优化 SQL}
```

#### 🟡 P1 - 严重问题：{问题标题}
**症状**:
**影响**:
**优化**:

## 📈 统计信息分析

### 表统计信息
| 表名 | 行数 | 最后分析 | 死行比例 | 状态 |
|------|------|----------|----------|------|
| ... | ... | ... | ... | ... |

⚠️ **过期统计信息**: {表名} 距离上次 ANALYZE 已 {天数} 天

### 列统计信息
| 表.列 | n_distinct | 实际选择性 | 估算选择性 | 偏差 |
|-------|-----------|------------|------------|------|
| ... | ... | ... | ... | ... |

## 🗂️ 索引分析

### 当前索引
| 索引名 | 表 | 扫描次数 | 读取行数 | 大小 | 状态 |
|--------|-----|----------|----------|------|------|
| ... | ... | ... | ... | ... | ... |

❌ **未使用索引**: {索引列表}
✅ **缺失索引建议**:
```sql
{创建索引 SQL}
```

## ⚙️ 配置参数分析

### 关键参数
| 参数 | 当前值 | 建议值 | 问题描述 |
|------|--------|--------|----------|
| work_mem | 4MB | 256MB | 哈希表可能溢出（会话级临时 SET 演示；全局调参按 parameters：估算配额优先、调大是下策） |
| ... | ... | ... | ... |

## 🎯 优化方案

### 方案1: {方案标题}（预期提升 {倍数} 倍）
```sql
{完整优化 SQL}
```

**预期效果**:
- Cost: {before} → {after}
- 执行时间: {before} → {after}

### 方案2: {方案标题}（预期提升 {倍数} 倍）

## 📋 实施检查清单

- [ ] 在测试环境验证 SQL 正确性
- [ ] 在测试环境验证性能提升
- [ ] 检查执行计划符合预期
- [ ] 创建必要索引
- [ ] 更新统计信息
- [ ] 生产环境灰度发布

## 📚 相关文档
- `sqlhc-integration.md` - SQLHC 集成详细文档
- `sqlhc-quickref.md` - SQLHC 快速参考
- `（原技能即缺此案例文件——待补实战案例）` - SQLHC 真实案例
```

## SQLHC 快速诊断

### 问题1: 查询很慢，Cost 很高
**检查**: JOIN 顺序是否合理？
**SQLHC**: 查看 `explain/explain_performance.txt` 中的 Cost 分布

### 问题2: 估算行数偏差很大
**检查**: 统计信息是否过期？
**SQLHC**: 查看 `statistics/table_stats.txt` 中的 Last Analyze

### 问题3: Hash Join 很慢
**检查**: work_mem 是否足够？
**SQLHC**: 查看 `configuration/parameters.txt` 中的 work_mem

### 问题4: Seq Scan 很慢
**检查**: 是否缺少索引？
**SQLHC**: 查看 `statistics/index_stats.txt` 中的索引使用情况

## 常用优化建议

### 统计信息维护
```sql
-- 定期更新
ANALYZE table_name;

-- 增加精度
ALTER TABLE table_name ALTER COLUMN column_name SET STATISTICS 10000;
```

### 索引优化
```sql
-- 创建缺失索引
CREATE INDEX idx_table_column ON table(column);

-- 删除未使用索引
DROP INDEX IF EXISTS idx_unused;
```

### 配置调优
```sql
-- 增加工作内存
SET work_mem = '256MB';   -- 会话级临时；全局口径见 parameters.md（调大是下策）
ALTER DATABASE mydb SET work_mem = '256MB';

-- 调整成本参数（SSD）
SET random_page_cost = 1.5;
```

## 优化效果评估

- **优秀**: 性能提升 > 10倍
- **良好**: 性能提升 5-10倍
- **合格**: 性能提升 2-5倍

## 性能目标

- **毫秒级查询**: Cost < 100, 时间 < 100ms
- **秒级查询**: Cost < 10,000, 时间 < 1s
- **大数据查询**: Cost < 1,000,000, 时间 < 10s

## 关键文档参考

- `sqlhc-integration.md` - SQLHC 输出结构解析
- `sqlhc-quickref.md` - SQLHC 快速诊断
- `（原技能即缺此案例文件——待补实战案例）` - 真实 SQLHC 案例（500倍提升）
- `optimizer-deep.md` - 核心优化理论

## 注意事项

1. **基于 SQLHC 输出**: 所有分析基于 SQLHC 提供的标准化数据
2. **严格优先级**: P0（子查询物化）→ P1（JOIN 顺序）→ P2（统计信息）→ P3（索引）
3. **验证先行**: 优化前先在测试环境验证
4. **渐进优化**: 逐步实施优化方案，每次验证效果
5. **源码级分析**: Cost 计算基于 VastBase 源码公式

## 技能版本

- **版本**: v3.0 (SQLHC 专用版)
- **创建日期**: 2026-04-17
- **基于**: VastBase SQL Health Check (SQLHC) 工具
- **特点**: 专注 SQLHC 输出分析，标准化诊断流程

## 版本注记
- 源自 2026-04 独立技能 vastbase-sql-optimizer（2026-10-26 并入本域），并入时
  未逐篇复核；口径以本域 parameters/optimizer-deep 为准

## 来源
- 原独立技能文档迁移；示例数字未附原始会话证据（⚓待补实战出处）

