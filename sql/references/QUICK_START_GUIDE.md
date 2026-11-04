# VastBase数据类型隐式转换 - 快速使用指南

## 🎯 5分钟速成

### 最常见的3个问题（90%的场景）

#### 1. VARCHAR列 vs INTEGER字面量 ⭐⭐⭐

**问题**:
```sql
-- ❌ 错误写法
SELECT * FROM orders WHERE order_id = 12345;
```

**执行计划特征**:
```
Seq Scan on orders  -- 全表扫描！
  Filter: (order_id = '12345'::VARCHAR)
```

**解决方案**:
```sql
-- ✅ 正确写法
SELECT * FROM orders WHERE order_id = '12345';
```

**影响**: 索引失效，性能下降100倍

---

#### 2. JOIN类型不匹配 ⭐⭐⭐

**问题**:
```sql
-- ❌ 类型不匹配的JOIN
CREATE TABLE orders (order_id VARCHAR(50), ...);
CREATE TABLE order_items (order_id INTEGER, ...);

SELECT * FROM orders o
JOIN order_items i ON o.order_id = i.order_id;
```

**执行计划特征**:
```
Hash Join
  Hash Cond: ((o.order_id)::INTEGER = i.order_id)  -- 隐式转换！
  -> Seq Scan on orders o  -- 无法使用索引
```

**解决方案**:
```sql
-- ✅ 方案1: 显式转换
SELECT * FROM orders o
JOIN order_items i ON o.order_id = i.order_id::VARCHAR;

-- ✅ 方案2: 统一类型（推荐）
ALTER TABLE order_items ALTER COLUMN order_id TYPE VARCHAR(50);
```

**影响**: JOIN性能下降50倍

---

#### 3. 函数包裹列 ⭐⭐⭐

**问题**:
```sql
-- ❌ 函数包裹列
SELECT * FROM users WHERE LOWER(email) = 'user@example.com';
SELECT * FROM users WHERE SUBSTR(username, 1, 5) = 'admin';
```

**执行计划特征**:
```
Seq Scan on users
  Filter: (lower(email) = 'user@example.com'::VARCHAR)
```

**解决方案**:
```sql
-- ✅ 方案1: 创建函数索引
CREATE INDEX idx_email_lower ON users(LOWER(email));
SELECT * FROM users WHERE LOWER(email) = 'user@example.com';

-- ✅ 方案2: 改写SQL（如果可能）
SELECT * FROM users WHERE email = 'user@example.com';

-- ✅ 方案3: LIKE前缀匹配
SELECT * FROM users WHERE username LIKE 'admin%';
```

**影响**: 索引失效，性能下降100倍

---

## 🔍 如何诊断

### 步骤1: 查看执行计划
```sql
EXPLAIN (VERBOSE, ANALYZE, BUFFERS)
SELECT * FROM table WHERE ...;
```

### 步骤2: 检查关键标记

#### 类型转换标记
```sql
-- 看到这些标记说明有隐式转换：
::VARCHAR
::INTEGER
::NUMERIC
::DATE
::UUID
(column)::type
```

#### 函数调用标记
```sql
-- 看到这些函数说明列被包裹：
LOWER(column)
SUBSTR(column, ...)
SUBSTRING(column, ...)
TRIM(column)
UPPER(column)
```

#### 索引使用标记
```sql
-- 索引失效
Seq Scan on table
Bitmap Heap Scan (没有Bitmap Index Scan)

-- 索引有效
Index Scan using idx_xxx on table
Bitmap Index Scan using idx_xxx
```

### 步骤3: 验证列类型
```sql
SELECT
    a.attname AS column_name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS column_type
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
WHERE c.relname = 'your_table'
  AND a.attnum > 0;
```

### 步骤4: 验证JOIN列类型匹配
```sql
SELECT
    t1.relname AS table1,
    a1.attname AS column1,
    pg_catalog.format_type(a1.atttypid, a1.atttypmod) AS type1,
    t2.relname AS table2,
    a2.attname AS column2,
    pg_catalog.format_type(a2.atttypid, a2.atttypmod) AS type2,
    CASE
        WHEN a1.atttypid = a2.atttypid THEN '✅ MATCH'
        ELSE '❌ MISMATCH'
    END AS type_match
FROM pg_catalog.pg_attribute a1
JOIN pg_catalog.pg_class t1 ON a1.attrelid = t1.oid
JOIN pg_catalog.pg_attribute a2 ON a1.attname = a2.attname
JOIN pg_catalog.pg_class t2 ON a2.attrelid = t2.oid
WHERE t1.relname = 'table1'
  AND t2.relname = 'table2'
  AND a1.attnum > 0 AND a2.attnum > 0;
```

---

## 📊 快速参考表

| 源类型 | 目标类型 | 隐式转换 | 索引失效 | 优先级 |
|--------|---------|---------|---------|--------|
| VARCHAR列 | INTEGER字面量 | ✅ | ❌ | ⭐⭐⭐ |
| INTEGER列 | VARCHAR字面量 | ✅ | ❌ | ⭐⭐⭐ |
| DATE列 | TIMESTAMP字面量 | ✅ | ❌ | ⭐⭐ |
| UUID列 | VARCHAR字面量 | ✅ | ❌ | ⭐⭐ |
| JOIN类型不匹配 | 任意 | ✅ | ❌ | ⭐⭐⭐ |
| 函数包裹列 | 任意 | ✅ | ❌ | ⭐⭐⭐ |

---

## 🛠️ 优化方案速查

| 问题 | 优化方案 | 代码示例 |
|------|---------|---------|
| VARCHAR列 = INTEGER | 使用字符串字面量 | `WHERE col = '123'` |
| INTEGER列 = VARCHAR | 使用数字字面量 | `WHERE col = 123` |
| JOIN类型不匹配 | 统一类型或显式转换 | `ON a.col = b.col::VARCHAR` |
| DATE列 = TIMESTAMP | 使用DATE字面量 | `WHERE date_col = '2026-04-18'::DATE` |
| LOWER(column) | 创建函数索引 | `CREATE INDEX ON table(LOWER(col))` |
| UUID列 = VARCHAR | 使用::UUID转换 | `WHERE uuid_col = 'xxx'::UUID` |

---

## 💡 最佳实践

### 设计阶段
1. **数字ID用INTEGER/BIGINT**，不要用VARCHAR
2. **外键列类型必须一致**
3. **日期字段用DATE**，不要用VARCHAR
4. **避免使用CHAR类型**，优先VARCHAR

### 开发阶段
1. **应用传入正确类型的参数**
   - Java: `PreparedStatement.setString()` for VARCHAR
   - Java: `PreparedStatement.setInt()` for INTEGER
2. **使用预编译语句**
3. **ORM配置正确的JDBC类型**
   - Hibernate: `@Column(columnDefinition = "VARCHAR")`
   - MyBatis: `jdbcType VARCHAR`

### 测试阶段
1. **使用EXPLAIN ANALYZE验证**
2. **关注执行计划中的类型转换**
3. **监控Buffer命中率**
4. **对比优化前后的Cost和实际时间**

### 生产环境
1. **设置慢查询日志**
   ```sql
   ALTER DATABASE SET log_min_duration_statements = 1000; -- 1秒
   ```
2. **监控pg_stat_statements**
3. **定期检查pg_stat_user_tables的seq_scan**
4. **使用自动EXPLAIN工具**

---

## 📚 相关文档

### 完整参考手册
```bash
/home/xiang/vastbase-sql-cases/VASTBASE_TYPE_CAST_REFERENCE.md
```
- 30个场景详解
- 完整诊断流程
- 深度分析

### 测试SQL
```bash
/home/xiang/vastbase-sql-cases/vastbase_type_cast_simple_test.sql
```
- 5个核心场景
- 可直接执行
- 包含执行计划对比

### 技能包
```bash
~/.hermes/skills/vastbase-sql-optimizer/SKILL.md
```
- 步骤0: 自动检查隐式转换
- 集成诊断工具
- 参考案例链接

---

## 🎓 学习路径

### 第1天: 掌握核心（1小时）
1. ✅ 理解VARCHAR vs INTEGER场景
2. ✅ 理解JOIN类型不匹配场景
3. ✅ 学会查看执行计划
4. ✅ 记住3个优化方案

### 第2天: 深度学习（2小时）
1. 📖 阅读完整参考手册
2. 📖 理解诊断检查清单
3. 📖 学习所有优化方案
4. 📖 在测试环境验证

### 第3天: 实战应用（持续）
1. 🔧 在实际SQL分析中应用
2. 🔧 积累新场景
3. 🔧 更新参考手册
4. 🔧 分享经验

---

## ⚡ 常见误区

### 误区1: "数据库会自动优化"
❌ **错误**: 数据库不会自动优化隐式转换
✅ **正确**: 必须手动修改SQL或创建函数索引

### 误区2: "性能影响不大"
❌ **错误**: 隐式转换影响很小
✅ **正确**: 可能导致100倍性能下降

### 误区3: "只有VARCHAR有问题"
❌ **错误**: 只有VARCHAR类型有问题
✅ **正确**: DATE、UUID、函数包裹都有问题

### 误区4: "创建索引就行了"
❌ **错误**: 有索引就能用
✅ **正确**: 隐式转换会导致索引失效

---

## 🔗 快速链接

- **查看参考手册**: `cat /home/xiang/vastbase-sql-cases/VASTBASE_TYPE_CAST_REFERENCE.md`
- **执行测试**: `sudo -u <vbuser> <vb-home>/bin/psql -d <db> -p <port> -f <脚本路径>`
- **查看技能包**: `cat ~/.hermes/skills/vastbase-sql-optimizer/SKILL.md`

---

## 🏆 总结

### 记住这3点

1. **VARCHAR列 vs INTEGER字面量** → 用字符串字面量
2. **JOIN类型不匹配** → 统一类型
3. **函数包裹列** → 创建函数索引

### 检查清单

分析SQL时，检查：
- [ ] 是否有 `::类型` 操作符？
- [ ] 是否有函数包裹列？
- [ ] JOIN条件类型是否匹配？
- [ ] 扫描类型是Index Scan还是Seq Scan？
- [ ] Cost是否异常高？

### 下一步行动

1. ✅ 查看参考手册
2. ✅ 在测试环境验证
3. ✅ 在实际SQL分析中应用
4. ✅ 积累新场景

---

**版本**: v1.0
**更新时间**: 2026-04-18
**维护者**: VastBase SQL优化技能包
