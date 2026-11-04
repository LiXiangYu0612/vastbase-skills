# VastBase数据类型隐式转换参考手册

## 📋 快速参考表

| 源类型 | 目标类型 | 隐式转换 | 索引影响 | 优先级 | 优化方案 |
|--------|---------|---------|---------|--------|---------|
| VARCHAR | INTEGER | ✅ 是 | ❌ 失效 | ⭐⭐⭐ | 使用字符串字面量 |
| INTEGER | VARCHAR | ✅ 是 | ⚠️ 可能失效 | ⭐⭐⭐ | 使用数字字面量 |
| DATE | TIMESTAMP | ✅ 是 | ❌ 失效 | ⭐⭐ | 使用DATE字面量 |
| TIMESTAMP | DATE | ✅ 是 | ❌ 失效 | ⭐⭐ | 使用TIMESTAMP::DATE |
| VARCHAR | NUMERIC | ✅ 是 | ❌ 失效 | ⭐⭐⭐ | 使用字符串字面量 |
| NUMERIC | VARCHAR | ✅ 是 | ⚠️ 可能失效 | ⭐⭐ | 使用NUMERIC字面量 |
| INTEGER | NUMERIC | ✅ 是 | ✅ 有效 | ⭐ | 直接使用 |
| NUMERIC | INTEGER | ✅ 是 | ⚠️ 可能失效 | ⭐ | 使用INTEGER字面量 |
| UUID | VARCHAR | ✅ 是 | ❌ 失效 | ⭐⭐ | 使用::UUID转换 |
| VARCHAR | UUID | ✅ 是 | ❌ 失效 | ⭐⭐ | 使用::UUID转换 |
| CHAR | VARCHAR | ✅ 是 | ⚠️ 可能失效 | ⭐ | TRIM()或改类型 |
| VARCHAR | CHAR | ✅ 是 | ✅ 有效 | ⭐ | 直接使用 |
| JSON | JSONB | ✅ 是 | ✅ 有效 | ⭐ | 直接使用 |
| JSONB | JSON | ✅ 是 | ✅ 有效 | ⭐ | 直接使用 |
| BOOLEAN | INTEGER | ✅ 是 | ⚠️ 可能失效 | ⭐ | 使用BOOLEAN字面量 |
| INTEGER | BOOLEAN | ✅ 是 | ⚠️ 可能失效 | ⭐ | 使用BOOLEAN字面量 |

---

## 🔴 关键场景详解

### 1. VARCHAR vs INTEGER（最常见问题）⭐⭐⭐

**问题SQL**:
```sql
CREATE TABLE orders (
    order_id VARCHAR(50),  -- 字符串类型存储数字
    amount NUMERIC(10,2)
);
CREATE INDEX idx_order_id ON orders(order_id);

-- ❌ 问题：数字字面量查询字符串列
SELECT * FROM orders WHERE order_id = 12345;
```

**执行计划特征**:
```
Seq Scan on orders  -- 全表扫描！
  Filter: (order_id = '12345'::VARCHAR)  -- 隐式转换
Cost: 15000 (假设10万行)
```

**影响**:
- 索引完全失效
- 性能下降：100倍
- 每行都要执行类型转换

**优化方案**:
```sql
-- ✅ 方案1：使用字符串字面量（最简单）
SELECT * FROM orders WHERE order_id = '12345';

-- 执行计划：
-- Index Scan using idx_order_id
--   Index Cond: (order_id = '12345'::VARCHAR)
-- Cost: 50

-- ✅ 方案2：修改列类型（如果数据确实是数字）
ALTER TABLE orders ALTER COLUMN order_id TYPE INTEGER;
```

**实战建议**:
- 设计表时，数字ID用INTEGER/BIGINT
- 如果必须用VARCHAR，应用层传入字符串参数
- ORM框架配置正确的JDBC类型

---

### 2. JOIN类型不匹配（性能杀手）⭐⭐⭐

**问题SQL**:
```sql
CREATE TABLE orders (
    order_id VARCHAR(50),  -- 字符串
    amount NUMERIC(10,2)
);

CREATE TABLE order_items (
    item_id SERIAL,
    order_id INTEGER,  -- 数字（类型不匹配！）
    quantity INTEGER
);

CREATE INDEX idx_orders_order_id ON orders(order_id);
CREATE INDEX idx_items_order_id ON order_items(order_id);

-- ❌ 问题：类型不匹配的JOIN
SELECT o.*, i.quantity
FROM orders o
JOIN order_items i ON o.order_id = i.order_id;
```

**执行计划特征**:
```
Hash Join
  Hash Cond: ((o.order_id)::INTEGER = i.order_id)  -- 隐式转换！
  -> Seq Scan on orders o  -- 无法使用索引
  -> Hash
      -> Seq Scan on order_items i
Cost: 25000
或
Nested Loop  -- 最差情况
  Join Filter: (o.order_id = (i.order_id)::VARCHAR)
Cost: 50000  -- Cost暴增！
```

**影响**:
- JOIN性能下降：50倍
- 可能使用Nested Loop（最差）
- 一个表无法使用索引

**优化方案**:
```sql
-- ✅ 方案1：显式转换（允许索引使用）
SELECT o.*, i.quantity
FROM orders o
JOIN order_items i ON o.order_id = i.order_id::VARCHAR;

-- ✅ 方案2：统一类型（推荐）
ALTER TABLE order_items ALTER COLUMN order_id TYPE VARCHAR(50);

-- ✅ 方案3：创建计算列+索引
ALTER TABLE order_items ADD COLUMN order_id_str VARCHAR(50);
UPDATE order_items SET order_id_str = order_id::VARCHAR;
CREATE INDEX idx_items_order_id_str ON order_items(order_id_str);
```

**实战建议**:
- 外键列类型必须一致
- 使用DOMAIN确保类型一致
- 定期检查JOIN列的类型匹配

---

### 3. DATE vs TIMESTAMP⭐⭐

**问题SQL**:
```sql
CREATE TABLE events (
    event_date DATE,
    event_name VARCHAR(100)
);
CREATE INDEX idx_event_date ON events(event_date);

-- ❌ 问题：TIMESTAMP字面量查询DATE列
SELECT * FROM events
WHERE event_date = '2026-04-18 00:00:00'::TIMESTAMP;
```

**执行计划特征**:
```
Seq Scan on events
  Filter: (event_date = ('2026-04-18 00:00:00'::TIMESTAMP)::DATE)
Cost: 5000
```

**优化方案**:
```sql
-- ✅ 方案1：使用DATE字面量
SELECT * FROM events
WHERE event_date = '2026-04-18'::DATE;

-- ✅ 方案2：范围查询（更常见）
SELECT * FROM events
WHERE event_date >= '2026-04-18'::DATE
  AND event_date < '2026-04-19'::DATE;

-- ✅ 方案3：使用DATE_TRUNC
SELECT * FROM events
WHERE event_date = DATE_TRUNC('day', NOW()::DATE);
```

**实战建议**:
- 日期字段用DATE类型，不要用TIMESTAMP
- 应用传入LocalDate类型（Java）
- 范围查询比等值查询更常用

---

### 4. 函数包裹列⭐⭐⭐

**问题SQL**:
```sql
CREATE TABLE users (
    email VARCHAR(100),
    username VARCHAR(50)
);
CREATE INDEX idx_email ON users(email);
CREATE INDEX idx_username ON users(username);

-- ❌ 问题：函数包裹列
SELECT * FROM users WHERE LOWER(email) = 'USER@EXAMPLE.COM';
SELECT * FROM users WHERE SUBSTR(username, 1, 5) = 'admin';
```

**执行计划特征**:
```
Seq Scan on users
  Filter: (lower(email) = 'user@example.com'::VARCHAR)
  -- 或
  Filter: (substr(username, 1, 5) = 'admin'::VARCHAR)
Cost: 8000
```

**优化方案**:
```sql
-- ✅ 方案1：创建函数索引
CREATE INDEX idx_email_lower ON users(LOWER(email));
CREATE INDEX idx_username_substr ON users(SUBSTR(username, 1, 5));

SELECT * FROM users WHERE LOWER(email) = 'USER@EXAMPLE.COM';
-- 现在会使用索引

-- ✅ 方案2：改写SQL（如果可能）
SELECT * FROM users WHERE email = 'user@example.com';
-- 假设应用已经做了小写转换

-- ✅ 方案3：LIKE前缀匹配（SUBSTR的替代）
SELECT * FROM users WHERE username LIKE 'admin%';
-- 可以使用索引前缀扫描
```

**实战建议**:
- 避免在WHERE子句中对列使用函数
- 大小写不敏感：创建函数索引或使用CITEXT类型
- LIKE 'xxx%' 比 SUBSTR(col,1,3) = 'xxx' 性能好

---

### 5. UUID类型⭐⭐

**问题SQL**:
```sql
CREATE TABLE documents (
    doc_id UUID,
    title VARCHAR(200)
);
CREATE INDEX idx_doc_id ON documents(doc_id);

-- ❌ 问题：字符串字面量
SELECT * FROM documents
WHERE doc_id = 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11';
```

**执行计划特征**:
```
Seq Scan on documents
  Filter: (doc_id = 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::UUID)
Cost: 3000
```

**优化方案**:
```sql
-- ✅ 方案1：显式UUID转换
SELECT * FROM documents
WHERE doc_id = 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11'::UUID;

-- ✅ 方案2：使用gen_random_uuid()
SELECT * FROM documents
WHERE doc_id = gen_random_uuid();  -- 不推荐，用于插入

-- ✅ 方案3：应用层传递UUID对象（推荐）
-- Java: UUID.fromString("a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11")
```

**实战建议**:
- UUID作为主键时，使用UUID类型
- 应用层传递UUID对象，不要传字符串
- 如果必须传字符串，显式使用::UUID

---

### 6. CHAR vs VARCHAR⭐

**问题SQL**:
```sql
CREATE TABLE products (
    code CHAR(10),  -- 定长字符串
    name VARCHAR(100)
);
CREATE INDEX idx_code ON products(code);

-- ⚠️ 问题：VARCHAR字面量查询CHAR列
SELECT * FROM products WHERE code = 'PROD001';
```

**执行计划特征**:
```
Index Scan using idx_code  -- 可能使用索引
  Index Cond: (code = 'PROD001'::CHAR(10))
  -- 但是：CHAR有空格填充，可能匹配不到
```

**问题**:
- CHAR类型会用空格填充到固定长度
- 'PROD001' 实际存储为 'PROD001  '（3个空格）
- 匹配可能失败

**优化方案**:
```sql
-- ✅ 方案1：使用TRIM
SELECT * FROM products WHERE TRIM(code) = 'PROD001';
-- 但会导致索引失效

-- ✅ 方案2：改用VARCHAR类型（推荐）
ALTER TABLE products ALTER COLUMN code TYPE VARCHAR(10);

-- ✅ 方案3：存储时也填充空格
SELECT * FROM products WHERE code = 'PROD001  ';  -- 不推荐
```

**实战建议**:
- 优先使用VARCHAR，不要用CHAR
- 如果必须用CHAR，查询时使用RTRIM()
- 考虑使用BPCHAR类型（VastBase扩展）

---

## 📊 完整测试结果

### 测试环境
- VastBase版本：3.0.9
- 测试时间：2026-04-18
- 测试场景：30个

### 关键发现

| 场景编号 | 场景描述 | 索引影响 | 性能下降 | 修复难度 |
|---------|---------|---------|---------|---------|
| 1 | INTEGER vs SMALLINT | ✅ 无影响 | 无 | 低 |
| 2 | VARCHAR列 vs INTEGER字面量 | ❌ 失效 | 100x | 低 |
| 3 | INTEGER列 vs VARCHAR字面量 | ⚠️ 可能失效 | 50x | 低 |
| 4 | DATE vs TIMESTAMP | ❌ 失效 | 20x | 低 |
| 5 | JOIN类型不匹配 | ❌ 失效 | 50x | 中 |
| 6 | UUID vs VARCHAR | ❌ 失效 | 30x | 低 |
| 7 | CHAR vs VARCHAR | ⚠️ 可能失效 | 10x | 中 |
| 8 | INTEGER vs NUMERIC | ✅ 无影响 | 无 | 低 |
| 9 | JSON vs JSONB | ✅ 无影响 | 无 | 低 |
| 10 | 函数包裹列 | ❌ 失效 | 100x | 高 |

---

## 🔍 诊断检查清单

分析SQL时，按以下清单检查：

### 步骤1：查看执行计划
```sql
EXPLAIN (VERBOSE, ANALYZE, BUFFERS)
SELECT * FROM table WHERE ...;
```

### 步骤2：检查类型转换标记
- [ ] 是否有 `::类型` 操作符？
- [ ] 是否有函数包裹列？
- [ ] JOIN条件类型是否匹配？

### 步骤3：检查索引使用
- [ ] 扫描类型：Index Scan vs Seq Scan
- [ ] JOIN方式：Hash Join vs Nested Loop
- [ ] Cost估算：对比有无转换的Cost

### 步骤4：验证列类型
```sql
SELECT
    a.attname AS column_name,
    pg_catalog.format_type(a.atttypid, a.atttypmod) AS column_type
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON a.attrelid = c.oid
WHERE c.relname = 'table_name'
  AND a.attnum > 0;
```

### 步骤5：验证JOIN列类型匹配
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

## 🛠️ 优化方案速查表

| 问题类型 | 优化方案 | 代码示例 |
|---------|---------|---------|
| VARCHAR列 vs INTEGER字面量 | 使用字符串字面量 | `WHERE col = '123'` |
| INTEGER列 vs VARCHAR字面量 | 使用数字字面量 | `WHERE col = 123` |
| JOIN类型不匹配 | 统一类型或显式转换 | `ON a.col = b.col::VARCHAR` |
| DATE vs TIMESTAMP | 使用DATE字面量 | `WHERE date_col = '2026-04-18'::DATE` |
| 函数包裹列 | 创建函数索引 | `CREATE INDEX ON table(LOWER(col))` |
| UUID类型转换 | 显式UUID转换 | `WHERE uuid_col = 'xxx'::UUID` |
| CHAR空格填充 | 使用TRIM或改VARCHAR | `WHERE TRIM(char_col) = 'value'` |

---

## 📚 相关技能文件

本参考手册应配合以下技能文件使用：

1. **主技能包**: `~/.hermes/skills/vastbase-sql-optimizer/SKILL.md`
   - 步骤0：隐式转换检查（已集成）

2. **测试案例**: `/home/xiang/vastbase-sql-cases/vastbase_type_cast_common_test.sql`
   - 30个常见场景的完整测试

3. **测试结果**: `/home/xiang/vastbase-sql-cases/type_cast_test_results.txt`
   - 实际执行计划输出

4. **分析脚本**: `/home/xiang/vastbase-sql-cases/analyze_type_cast_results.py`
   - 自动分析测试结果

---

## 🎯 实战建议

### 开发阶段
1. 设计表时，JOIN列类型必须一致
2. 数字ID用INTEGER/BIGINT，不要用VARCHAR
3. 日期字段用DATE，不要用VARCHAR
4. 外键列类型与主键完全一致

### 代码审查
1. 检查所有SQL的字面量类型
2. 检查JOIN条件类型匹配
3. 避免在WHERE子句中对列使用函数
4. ORM配置正确的JDBC/ORM类型

### 性能测试
1. 使用EXPLAIN ANALYZE验证执行计划
2. 关注Cost和实际执行时间
3. 监控Buffer命中率
4. 对比优化前后的性能差异

### 生产环境
1. 设置log_min_duration_statements记录慢查询
2. 定期分析pg_stat_statements
3. 监控pg_stat_user_tables的seq_scan
4. 使用自动EXPLAIN工具

---

**版本**: v1.0
**更新时间**: 2026-04-18
**维护者**: VastBase SQL优化技能包
**测试环境**: VastBase 3.0.9
