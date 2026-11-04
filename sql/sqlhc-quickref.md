# SQLHC快速参考指南

## 🚀 快速开始

### SQLHC是什么

**SQLHC** = SQL Health Check，VastBase数据库的自动化SQL健康检查工具。

**功能**:
- ✅ 自动收集SQL的所有相关信息
- ✅ 生成标准化的诊断报告
- ✅ 提供优化建议
- ✅ 生产环境安全（只读操作）

---

## 📥 使用SQLHC收集信息

### 步骤1: 运行SQLHC

```bash
# 基本用法
sqlhc -d database_name -u username -p password -q "SELECT ..."

# 完整示例
sqlhc \
    -d mydb \
    -u vastbase \
    -p vastbase \
    -q "SELECT o.order_id, c.customer_name FROM orders o JOIN customers c ON o.customer_id = c.customer_id WHERE o.order_date >= '2024-01-01'" \
    -o /path/to/output/
```

### 步骤2: 查看输出

```
sqlhc_output/
├── sqlhc_report.html              # HTML报告（推荐）
├── sqlhc_report.txt               # 文本报告
├── explain/                       # 执行计划
├── statistics/                    # 统计信息
├── objects/                       # 对象定义
└── configuration/                 # 配置参数
```

### 步骤3: 分析报告

```bash
# 用浏览器打开HTML报告
firefox sqlhc_output/sqlhc_report.html

# 或查看文本报告
cat sqlhc_output/sqlhc_report.txt
```

---

## 📊 SQLHC报告解读

### 1. 执行计划部分

**关键指标**:
```
Hash Join  (cost=0.00..1234567.89 rows=100 width=200)
            ↑         ↑              ↑           ↑       ↑
          启动成本   总成本          估算行数    宽度   JOIN方式
```

**快速诊断**:
- **Cost过高** (> 1,000,000): 可能是JOIN顺序问题
- **估算行数不准** (偏差>10倍): 统计信息问题
- **多次Hash Join**: 检查work_mem是否足够

### 2. 统计信息部分

**关键指标**:
```
Table      | Rows     | Last Analyze | Dead Rows | % Dead
-----------+----------+--------------+-----------+--------
orders     | 1000000  | 2026-04-17   | 5000      | 0.5%
customers | 1000     | 2026-04-10   | 100       | 10% ⚠️
```

**快速诊断**:
- **Last Analyze > 7天**: 统计信息可能过期
- **Dead Rows > 5%**: 需要VACUUM
- **Rows = 0**: 统计信息未收集

### 3. 索引部分

**关键指标**:
```
Index Name           | Scans  | Reads  | Size | Valid
---------------------+--------+--------+------+-------
orders_pkey          | 10000  | 100000 | 50MB | Y
idx_orders_status    | 0      | 0      | 30MB | Y ⚠️
```

**快速诊断**:
- **Scans = 0**: 索引未使用，考虑删除
- **Scans < 10**: 索引使用率低，评估必要性
- **Valid = N**: 索引失效，需要REINDEX

### 4. 配置参数部分

**关键参数**:
```
work_mem                 | 4MB    ⚠️ 可能不足
shared_buffers           | 128MB  ✓ 正常
enable_hashjoin          | on     ✓ 正常
default_statistics_target| 100    ✓ 正常
```

**快速诊断**:
- **work_mem < 16MB**: 大查询可能溢出
- **enable_hashjoin = off**: JOIN性能差
- **statistics_target < 100**: 统计信息不精确

---

## 🎯 常见问题诊断

### 问题1: 查询很慢，Cost很高

**检查**:
1. JOIN顺序是否合理？
2. 中间结果集是否过大？
3. 是否有合适的索引？

**SQLHC诊断**:
```bash
# 查看执行计划中的Cost分布
cat sqlhc_output/explain/explain_performance.txt | grep "cost="
```

**优化**:
```sql
-- 使用Hint强制JOIN顺序
/*+ LEADING(c p o oi) */
SELECT ...

-- 或增加work_mem
SET work_mem = '256MB';
```

### 问题2: 估算行数和实际行数偏差很大

**检查**:
1. 统计信息是否过期？
2. 是否有MCV或直方图？
3. n_distinct是否准确？

**SQLHC诊断**:
```bash
# 查看统计信息新鲜度
cat sqlhc_output/statistics/table_stats.txt

# 查看列统计信息
cat sqlhc_output/statistics/column_stats.txt
```

**优化**:
```sql
-- 更新统计信息
ANALYZE table_name;

-- 增加统计目标
ALTER TABLE table_name ALTER COLUMN column_name SET STATISTICS 10000;
```

### 问题3: Hash Join很慢

**检查**:
1. work_mem是否足够？
2. 哈希表大小是多少？
3. 是否有更好的JOIN方式？

**SQLHC诊断**:
```bash
# 查看work_mem配置
cat sqlhc_output/configuration/parameters.txt | grep work_mem

# 估算哈希表大小
# 哈希表大小 ≈ 内表行数 × 行大小
```

**优化**:
```sql
-- 增加work_mem
SET work_mem = '256MB';

-- 强制使用Nested Loop
/*+ NestLoop(t1 t2) */
SELECT ...
```

### 问题4: Seq Scan很慢

**检查**:
1. 过滤条件的选择性如何？
2. 是否有相关索引？
3. 为什么索引没被使用？

**SQLHC诊断**:
```bash
# 查看索引信息
cat sqlhc_output/statistics/index_stats.txt

# 查看列统计信息（选择性）
cat sqlhc_output/statistics/column_stats.txt | grep column_name
```

**优化**:
```sql
-- 创建索引
CREATE INDEX idx_table_column ON table(column);

-- 如果选择性很低，使用Bitmap Scan
SET enable_bitmapscan = on;
```

---

## 💡 SQLHC优化建议

### 建议类型1: 统计信息维护

```sql
-- 定期更新统计信息
ANALYZE table_name;

-- 增加统计目标（提高精度）
ALTER TABLE table_name ALTER COLUMN column_name SET STATISTICS 10000;

-- 批量更新
ANALYZE;
```

### 建议类型2: 索引优化

```sql
-- 创建缺失的索引
CREATE INDEX idx_table_column ON table(column);

-- 删除未使用的索引
DROP INDEX IF EXISTS idx_unused;

-- 重建索引
REINDEX INDEX idx_name;
```

### 建议类型3: 配置调优

```sql
-- 增加work_mem（适合大查询）
ALTER DATABASE mydb SET work_mem = '256MB';

-- 增加shared_buffers（适合高并发）
ALTER DATABASE mydb SET shared_buffers = '4GB';

-- 调整成本参数
ALTER DATABASE mydb SET random_page_cost = 1.5;

-- 增加统计目标
ALTER DATABASE mydb SET default_statistics_target = 1000;
```

### 建议类型4: SQL改写

```sql
-- 原始SQL
SELECT * FROM orders WHERE customer_id IN (
    SELECT customer_id FROM customers WHERE city = 'Beijing'
);

-- 改写为JOIN
SELECT o.* 
FROM orders o
JOIN customers c ON o.customer_id = c.customer_id
WHERE c.city = 'Beijing';
```

---

## 📋 SQLHC检查清单

使用SQLHC分析SQL时，确保检查：

### ✅ 执行计划检查
- [ ] JOIN顺序是否合理？
- [ ] JOIN方式是否合适？
- [ ] 扫描方式是否合适？
- [ ] Cost分布是否合理？

### ✅ 统计信息检查
- [ ] 统计信息是否过期？（>7天）
- [ ] 死行比例是否过高？（>5%）
- [ ] n_distinct是否准确？
- [ ] MCV和直方图是否准确？

### ✅ 索引检查
- [ ] 是否有缺失的索引？
- [ ] 是否有未使用的索引？
- [ ] 索引是否有效？
- [ ] 索引大小是否合理？

### ✅ 配置检查
- [ ] work_mem是否足够？
- [ ] shared_buffers是否合理？
- [ ] 优化器参数是否合理？
- [ ] Cost参数是否匹配硬件？

---

## 🔧 SQLHC命令参考

### 基本命令

```bash
# 基本用法
sqlhc -d db_name -u user -p password -q "SELECT ..." -o output_dir/

# 指定端口
sqlhc -d db_name -U user -P port -h host -q "SELECT ..." -o output_dir/

# 分析SQL文件
sqlhc -d db_name -u user -p password -f query.sql -o output_dir/

# 详细模式
sqlhc -d db_name -u user -p password -q "SELECT ..." -o output_dir/ -v

# 仅收集统计信息
sqlhc -d db_name -u user -p password -q "SELECT ..." -o output_dir/ --stats-only
```

### 高级选项

```bash
# 不生成HTML报告
sqlhc ... --no-html

# 超时设置（秒）
sqlhc ... --timeout 300

# 并行度
sqlhc ... --parallel 4

# 排除某些信息
sqlhc ... --no-config --no-objects
```

---

## 📚 相关文档

- `optimizer-deep.md` - 核心优化技能
- `sqlhc-integration.md` - SQLHC集成详细文档
- `optimizer-usage.md` - 使用手册
- `quick-reference.md` - 快速参考

---

**版本**: v3.0  
**最后更新**: 2026-04-17  
**作者**: xiang

## 版本注记
- 源自 2026-04 独立技能 vastbase-sql-optimizer（2026-10-26 并入本域），并入时
  未逐篇复核；口径以本域 parameters/optimizer-deep 为准

## 来源
- 原独立技能文档迁移；示例数字未附原始会话证据（⚓待补实战出处）

