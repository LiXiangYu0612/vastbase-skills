# VastBase SQL优化常见错误陷阱

**创建日期**: 2026-04-17
**重要性**: ⭐⭐⭐⭐⭐
**来源**: 真实案例分析中的反复错误

---

## 概述

本文档总结了在VastBase SQL优化分析中常见的致命错误，这些错误都是在实际案例中反复出现的，必须牢记避免。

**为什么需要这个文档**：
- 这些错误会误导优化方向
- 浪费大量时间在无用的优化上
- 导致错误的分析结论
- 降低用户对分析结果的信任

---

## 错误1: 混淆n_live_tup与reltuples ❌

### 错误理解

```sql
-- 从pg_stat_user_tables看到的
n_live_tup: 10

-- 从pg_class看到的
reltuples: 1978161

-- 错误结论: 统计信息偏差197万倍！需要立即ANALYZE！
```

### 正确理解

- **reltuples**: 优化器使用的统计信息估计值（通过ANALYZE收集，存储在pg_class）
- **n_live_tup**: pg_stat_user_tables的实时计数（仅用于监控，优化器完全不用）
- **判断统计信息偏差**: 应该用 `SELECT COUNT(*) FROM table` 与 reltuples 对比

### 正确验证方法

```sql
-- 方法1: 整表行数对比
SELECT 
    COUNT(*) AS actual_rows,
    (SELECT reltuples FROM pg_class WHERE relname = 'ai_knowledge_vector')::bigint AS estimated_rows,
    COUNT(*)::float / NULLIF((SELECT reltuples FROM pg_class WHERE relname = 'ai_knowledge_vector'), 0) AS ratio
FROM ai_knowledge_vector;

-- 判断标准:
-- ratio ≈ 1.0 (0.8-1.2): 统计信息准确
-- ratio > 3.0 或 < 0.33: 统计信息偏差较大，建议ANALYZE

-- 方法2: 特定条件的选择性验证
SELECT 
    COUNT(*) AS actual_filtered_rows,
    1978161 * (2.0/69559) AS estimated_filtered_rows  -- 基于统计信息估计
FROM ai_knowledge_vector
WHERE doc_id IN ('Pj9kbJUBaYknx-i7ZZdN', 'nWPCGpMBoSPn0UAXEGnL');

-- 方法3: 使用SQLHC验证
vb_tool tabstat -d database -s schema -t table
-- 查看 reltuples vs 实际COUNT(*)
```

### 为什么这个错误致命

1. **误导优化方向**: 认为需要ANALYZE，实际可能是其他问题
2. **忽略真正瓶颈**: 浪费时间在统计信息上，忽略索引缺失
3. **降低分析可信度**: 用户验证后发现结论错误

### 实际案例

```sql
-- 错误分析过程:
1. 看到n_live_tup=10, reltuples=1978161
2. 结论: 统计信息偏差197万倍！
3. 建议: 立即ANALYZE
4. 用户反馈: ANALYZE后性能无改善

-- 正确分析过程:
1. 看到执行计划: Seq Scan 209万行，返回21行
2. 计算选择性: 21/2099412 = 0.001%
3. 检查索引: doc_id索引不存在
4. 结论: 缺少doc_id索引导致全表扫描
5. 建议: CREATE INDEX idx_doc_id
6. 结果: 性能提升100倍
```

---

## 错误2: 认为IN查询不能走索引 ❌

### 错误理解

```sql
-- 错误认知: IN查询不能走索引，必须改写SQL
WHERE doc_id IN ('Pj9kbJUBaYknx-i7ZZdN', 'nWPCGpMBoSPn0UAXEGnL')

-- 错误建议: 改为UNION ALL或其他复杂改写
WHERE doc_id = 'Pj9kbJUBaYknx-i7ZZdN' OR doc_id = 'nWPCGpMBoSPn0UAXEGnL'
```

### 正确理解

- **IN查询完全可以走索引**
- `col IN (v1, v2, ...)` 优化器会展开为 `col = v1 OR col = v2 OR ...`
- 对于B-tree索引，IN查询和等值查询效率相同

### IN索引使用原理

```sql
-- SQL:
WHERE doc_id IN ('val1', 'val2', 'val3')

-- 优化器展开为:
WHERE doc_id = 'val1' OR doc_id = 'val2' OR doc_id = 'val3'

-- 执行计划:
Index Scan using idx_doc_id on table
  Index Cond: (doc_id = ANY ('{val1,val2,val3}'::text[]))

-- 索引扫描方式:
1. 在B-tree中查找val1 → 如果存在，返回
2. 在B-tree中查找val2 → 如果存在，返回
3. 在B-tree中查找val3 → 如果存在，返回
```

### 验证IN走索引

```sql
-- 步骤1: 创建索引
CREATE INDEX idx_doc_id ON ai_knowledge_vector(doc_id);

-- 步骤2: 验证执行计划
EXPLAIN ANALYZE
SELECT * FROM ai_knowledge_vector
WHERE doc_id IN ('Pj9kbJUBaYknx-i7ZZdN', 'nWPCGpMBoSPn0UAXEGnL');

-- 期望结果:
-- Index Scan using idx_doc_id on ai_knowledge_vector
--   Index Cond: (doc_id = ANY ('{...}'::text[]))
--   Rows: 21 (vs 之前209万行)
--   Cost: 大幅降低

-- 步骤3: 验证索引使用统计
SELECT 
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
WHERE tablename = 'ai_knowledge_vector'
  AND indexname = 'idx_doc_id';
```

### IN索引的使用条件

**适合使用索引的情况**:
- 列选择性高 (n_distinct大)
- IN列表值少 (1-100个值最优)
- 返回行数少 (<总行数5%)
- 存在B-tree索引

**不适合使用索引的情况**:
- 列选择性低 (如性别、状态)
- IN列表值过多 (>1000个值)
- 返回行数多 (>总行数20%)
- 无索引或索引不匹配

### 实际案例

```sql
-- 案例背景:
-- 表: ai_knowledge_vector (197万行)
-- 查询: WHERE doc_id IN (2个值)
-- 问题: 全表扫描209万行，耗时7.9秒

-- 错误分析:
1. 看到IN查询
2. 结论: IN不能走索引，建议改写SQL
3. 建议: 使用UNION ALL改写
4. 结果: 改写后仍慢，用户不满意

-- 正确分析:
1. 看到doc_id.n_distinct = 69559 (高选择性)
2. 看到IN只有2个值
3. 检查: doc_id索引不存在
4. 建议: CREATE INDEX idx_doc_id
5. 结果: 性能提升100倍 (7.9s → 79ms)
```

---

## 错误3: 向量索引无法解决WHERE过滤问题 ❌

### 错误理解

```sql
-- 错误认知: 向量索引能直接解决所有性能问题
SELECT * FROM ai_knowledge_vector
WHERE status = 1 AND is_deleted = 0
  AND doc_id IN ('val1', 'val2')
ORDER BY (doc_vector <=> ARRAY[...])
LIMIT 20;

-- 错误建议: 创建向量索引即可，无需其他优化
```

### 正确理解

**向量索引的困境**:
1. 向量索引找到最相似的Top-20行
2. 应用WHERE条件过滤这20行
3. **可能全部被过滤掉，返回0行！**

**正确执行顺序**:
- 先用WHERE条件过滤出候选集
- 再在候选集中做向量排序
- 不是: 向量Top-20 → WHERE过滤

### 向量相似度搜索的正确模式

#### 错误模式

```sql
-- 错误: 向量Top-20可能被WHERE完全过滤
SELECT * FROM ai_knowledge_vector
WHERE status = 1 AND is_deleted = 0
  AND doc_id IN ('val1', 'val2')
ORDER BY (doc_vector <=> ARRAY[...])
LIMIT 20;

-- 执行流程:
-- 1. 向量索引找到全局最相似的20行
-- 2. 应用WHERE条件
-- 3. 可能20行都不满足doc_id条件
-- 4. 返回0行或行数不足20行
```

#### 正确模式

```sql
-- 正确: 先过滤后排序
WITH filtered_docs AS (
  SELECT id, doc_vector, title, content, ...
  FROM ai_knowledge_vector
  WHERE doc_id IN ('Pj9kbJUBaYknx-i7ZZdN', 'nWPCGpMBoSPn0UAXEGnL')
    AND status = 1 
    AND is_deleted = 0 
    AND (
      (knowledge_type = 3 AND publish_time >= '2022-01-01' AND knowledge_id IN (347))
      OR
      (knowledge_type = 2 AND company_id IN (0))
    )
)
SELECT * FROM filtered_docs
ORDER BY (doc_vector <=> ARRAY[-0.004518222,-0.02167824,...])
LIMIT 20;

-- 执行流程:
-- 1. WHERE过滤出候选集（假设21行）
-- 2. 对21行做向量距离计算
-- 3. 返回最相似的20行
-- 4. 保证返回的20行都满足WHERE条件
```

### 性能分析

```sql
-- 错误模式的性能:
-- 1. 向量索引扫描: 假设返回20行 (cost=50)
-- 2. WHERE过滤20行: 可能全部过滤 (返回0行)
-- 3. 总耗时: 但没找到满足条件的行

-- 正确模式的性能:
-- 1. WHERE过滤(可能全表扫描): 209万行 → 21行 (cost=500000)
-- 2. 向量排序21行: 21次距离计算 (cost=21)
-- 3. 总耗时: 7.9秒，但保证返回正确结果

-- 优化后的正确模式:
-- 1. WHERE过滤使用doc_id索引: 快速定位21行 (cost=100)
-- 2. 向量排序21行: 21次距离计算 (cost=21)
-- 3. 总耗时: <100ms，返回正确结果
```

### 实际案例

```sql
-- 案例背景:
-- 表: ai_knowledge_vector (197万行)
-- 查询: 向量相似度搜索 + 复杂WHERE条件
-- 执行: 全表扫描7.9秒，返回21行，排序后取20行

-- 错误分析:
1. 看到ORDER BY向量距离
2. 结论: 创建向量索引即可解决
3. 建议: CREATE INDEX idx_doc_vector_l2
4. 结果: 索引创建后，性能未改善（因为仍需全表扫描做WHERE过滤）

-- 正确分析:
1. 看到执行计划: Seq Scan 209万行，返回21行
2. 理解: 21行是满足WHERE条件的全部行
3. 结论: 先WHERE过滤，再向量排序
4. 建议: CREATE INDEX idx_doc_id (解决WHERE过滤)
5. 结果: WHERE过滤从7.9秒 → 10ms，向量排序21行可忽略
```

---

## 错误4: 过度推荐组合索引 ❌

### 错误理解

```sql
-- 看到WHERE条件就建议组合索引
WHERE status = 1 AND is_deleted = 0 AND doc_id IN ('val1', 'val2')

-- 错误建议:
CREATE INDEX idx_bad ON ai_knowledge_vector(status, is_deleted, doc_id);

-- 问题: status和is_deleted几乎无选择性，建索引无用
```

### 正确理解

**组合索引设计原则**:
1. **前导列必须高选择性** - 否则无法有效过滤
2. **后续列可以是常量** - 等值过滤的列可以放在后面
3. **避免低选择性列** - 如status=1, is_deleted=0这种常量

### 索引选择性分析

```sql
-- 从pg_stats查看选择性
SELECT 
    attname,
    n_distinct,
    most_common_vals,
    most_common_freqs
FROM pg_stats
WHERE tablename = 'ai_knowledge_vector'
  AND attname IN ('status', 'is_deleted', 'doc_id');

-- 结果分析:
-- status: n_distinct=1, most_common_vals={1}, freq={1.0}
--   选择性: 1:1 (无选择性，建索引无用)
   
-- is_deleted: n_distinct=1, most_common_vals={0}, freq={1.0}
--   选择性: 1:1 (无选择性，建索引无用)
   
-- doc_id: n_distinct=69559, most_common_freqs={0.0003, 0.0003, 0.0003}
--   选择性: 69559:1 (高选择性，建索引有效)
```

### 正确的组合索引设计

#### 错误示例

```sql
-- 错误: 前导列无选择性
CREATE INDEX idx_bad ON ai_knowledge_vector(status, is_deleted, doc_id);

-- 分析:
-- status = 1: 过滤后仍是197万行 (无过滤效果)
-- is_deleted = 0: 过滤后仍是197万行 (无过滤效果)
-- doc_id IN (2值): 过滤后21行 (有效过滤)
-- 
-- 问题: 前两列无法过滤，索引退化为doc_id单列索引
-- 索引大小: (status + is_deleted + doc_id) = 很大
-- 实际效果: 相当于doc_id单列索引，但浪费存储
```

#### 正确示例

```sql
-- 正确: 前导列高选择性
CREATE INDEX idx_good ON ai_knowledge_vector(doc_id, status, is_deleted);

-- 分析:
-- doc_id IN (2值): 快速定位到21行
-- status = 1: 在21行中过滤 (几乎无影响)
-- is_deleted = 0: 在21行中过滤 (几乎无影响)
--
-- 优势: doc_id前导，快速定位目标行
-- 索引大小: 仍然较大，但有效
-- 实际效果: 显著提升性能
```

#### 最优示例

```sql
-- 最优: 只索引高选择性列
CREATE INDEX idx_optimal ON ai_knowledge_vector(doc_id);

-- 分析:
-- doc_id IN (2值): 快速定位到21行
-- status = 1 和 is_deleted = 0: 在21行中内存过滤
--
-- 优势: 索引最小，性能相同
-- 索引大小: 只有doc_id一列
-- 实际效果: 性能与组合索引相同，但存储开销最小
```

### 判断是否需要组合索引的决策树

```
是否有多个等值条件？
├─ 是 → 检查列选择性
│   ├─ 所有列都高选择性 → 考虑组合索引
│   ├─ 部分列高选择性 → 只对高选择性列建单列索引
│   └─ 所有列低选择性 → 不建索引
├─ 否 → 检查是否有范围/排序条件
│   ├─ 是 → 考虑 (等值列, 范围列, 排序列) 组合索引
│   └─ 否 → 不建索引或只建单列索引
```

### 实际案例

```sql
-- 案例背景:
-- 查询: WHERE status = 1 AND is_deleted = 0 AND doc_id IN ('val1', 'val2')

-- 错误建议:
CREATE INDEX idx_bad ON ai_knowledge_vector(status, is_deleted, doc_id);
-- 问题: 索引大，性能差

-- 正确建议:
CREATE INDEX idx_good ON ai_knowledge_vector(doc_id);
-- 或
CREATE INDEX idx_good ON ai_knowledge_vector(doc_id, status, is_deleted);
-- 优势: 索引小，性能好

-- 性能对比:
-- idx_bad: Index Scan (仍需扫描大量页)
-- idx_good: Index Scan (快速定位21行)
```

---

## 错误5: 向量索引操作符不匹配 ⚠️

### 错误示例

```sql
-- 索引定义
CREATE INDEX idx_doc_vector ON ai_knowledge_vector 
USING graph_index (doc_vector floatvector_cosine_ops);

-- SQL使用
SELECT * FROM ai_knowledge_vector
ORDER BY (doc_vector <=> ARRAY[-0.004518222,-0.02167824,...])
LIMIT 20;

-- 问题: <=> 默认使用欧几里得距离，但索引是余弦距离
-- 结果: 索引不生效，全表扫描
```

### 正确理解

**VastBase向量索引操作符类**:
- `floatvector_cosine_ops`: 余弦距离 (cosine distance)
- `floatvector_l2_ops`: 欧几里得距离 (L2 distance)
- `floatvector_ip_ops`: 内积 (inner product)

**操作符 `<=>` 的含义**:
- 默认: 欧几里得距离 (L2)
- 需要根据索引操作符类确定实际使用的距离函数

### 验证向量索引

```sql
-- 步骤1: 检查索引定义
SELECT 
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'ai_knowledge_vector'
  AND indexname LIKE '%doc_vector%';

-- 期望看到:
-- "CREATE INDEX idx_doc_vector ON ai_knowledge_vector 
--  USING graph_index (doc_vector floatvector_l2_ops)"

-- 步骤2: 验证执行计划
EXPLAIN ANALYZE
SELECT * FROM ai_knowledge_vector
ORDER BY (doc_vector <=> ARRAY[-0.004518222,-0.02167824,...])
LIMIT 20;

-- 期望看到:
-- Index Scan using idx_doc_vector (cost=...)
-- 而不是:
-- Seq Scan on ai_knowledge_vector (cost=...)

-- 步骤3: 检查索引使用统计
SELECT 
    indexname,
    idx_scan,
    idx_tup_read,
    idx_tup_fetch
FROM pg_stat_user_indexes
WHERE tablename = 'ai_knowledge_vector'
  AND indexname = 'idx_doc_vector';

-- 如果idx_scan=0或很少，说明索引未被使用
```

### 正确的向量索引创建

```sql
-- 场景1: 使用欧几里得距离
CREATE INDEX idx_doc_vector_l2 ON ai_knowledge_vector 
USING graph_index (doc_vector floatvector_l2_ops);

SELECT * FROM ai_knowledge_vector
ORDER BY (doc_vector <=> ARRAY[...])  -- 默认L2距离
LIMIT 20;

-- 场景2: 使用余弦距离
CREATE INDEX idx_doc_vector_cosine ON ai_knowledge_vector 
USING graph_index (doc_vector floatvector_cosine_ops);

SELECT * FROM ai_knowledge_vector
ORDER BY (doc_vector <=> ARRAY[...])  -- 需要确认是余弦
LIMIT 20;

-- 注意: 不同版本VastBase的<=>默认含义可能不同
-- 必须查看文档确认当前版本的默认行为
```

### 实际案例

```sql
-- 案例背景:
-- 索引: idx_doc_vector (floatvector_cosine_ops)
-- 查询: ORDER BY doc_vector <=> ARRAY[...]
-- 问题: 索引9GB，但查询仍全表扫描7.9秒

-- 错误分析:
1. 看到向量索引存在
2. 结论: 索引应该生效，问题在其他地方
3. 建议: 检查统计信息、内存配置等
4. 结果: 检查一圈后问题仍存在

-- 正确分析:
1. 检查索引定义: floatvector_cosine_ops
2. 检查SQL操作符: <=> (默认L2距离)
3. 结论: 操作符不匹配，索引无法使用
4. 建议: 重建索引为floatvector_l2_ops
5. 结果: 索引生效，性能提升100倍
```

---

## 综合案例: 向量搜索优化完整流程

### 问题

```sql
-- 查询: 向量相似度搜索 + 复杂WHERE条件
SELECT id, doc_id, title, content, doc_vector, ...
FROM ai_knowledge_vector
WHERE status = 1 
  AND is_deleted = 0 
  AND (
    (knowledge_type = 3 AND publish_time >= '2022-01-01' AND knowledge_id IN (347))
    OR
    (knowledge_type = 2 AND company_id IN (0))
  )
  AND doc_id IN ('Pj9kbJUBaYknx-i7ZZdN', 'nWPCGpMBoSPn0UAXEGnL')
ORDER BY (doc_vector <=> ARRAY[...1024维向量...])
LIMIT 20;

-- 性能: 7.9秒，全表扫描209万行
```

### 错误分析过程

```
步骤1: 看到ORDER BY向量距离
结论: 需要向量索引
建议: CREATE INDEX idx_doc_vector_l2

步骤2: 检查统计信息
看到: n_live_tup=10, reltuples=1978161
结论: 统计信息偏差197万倍！
建议: 立即ANALYZE

步骤3: 检查WHERE条件
看到: status, is_deleted, doc_id
结论: 需要组合索引
建议: CREATE INDEX idx (status, is_deleted, doc_id)

结果: 
- 向量索引创建后性能未改善
- ANALYZE后性能未改善
- 组合索引创建后性能未改善
- 用户不满意，分析失败
```

### 正确分析过程

```
步骤1: 分析执行计划
看到: Seq Scan 209万行 → 返回21行 → Sort 21行
结论: 21行是满足WHERE条件的全部行

步骤2: 理解执行顺序
当前: 全表扫描(209万行) → WHERE过滤(21行) → 向量排序(21行)
正确: WHERE过滤(21行) → 向量排序(21行)

步骤3: 定位性能瓶颈
瓶颈1: 全表扫描209万行找21行 (WHERE过滤)
瓶颈2: 向量排序21行 (可忽略)

步骤4: 确定优化方案
方案1: 创建doc_id索引 (解决WHERE过滤)
验证: doc_id.n_distinct=69559, IN(2值), 高选择性
效果: WHERE过滤 7.9秒 → 10ms

方案2: 检查向量索引 (次要问题)
检查: 索引操作符是否匹配
当前: floatvector_cosine_ops vs <=> L2距离
建议: 如果仍慢，考虑重建向量索引

结果: 
- doc_id索引创建后性能提升100倍
- 用户满意，问题解决
```

### 最终优化方案

```sql
-- P0方案: 创建doc_id索引
CREATE INDEX idx_doc_id ON ai_knowledge_vector(doc_id);

-- 验证效果
EXPLAIN ANALYZE [原SQL];
-- 期望: Index Scan using idx_doc_id, cost大幅降低

-- P1方案: 如果向量排序仍慢，检查向量索引
-- 检查操作符匹配
SELECT indexdef FROM pg_indexes 
WHERE tablename = 'ai_knowledge_vector' AND indexname LIKE '%doc_vector%';

-- 如果不匹配，重建索引
DROP INDEX idx_doc_vector;
CREATE INDEX idx_doc_vector_l2 ON ai_knowledge_vector 
USING graph_index (doc_vector floatvector_l2_ops);

-- P2方案: 调整SQL，明确执行顺序
WITH filtered_docs AS (
  SELECT ... FROM ai_knowledge_vector
  WHERE doc_id IN (...) AND status = 1 AND is_deleted = 0 ...
)
SELECT * FROM filtered_docs
ORDER BY (doc_vector <=> ARRAY[...]) LIMIT 20;
```

---

## 关键经验总结

### 必须牢记的原则

1. **统计信息验证**: 用COUNT(*) vs reltuples，不是n_live_tup
2. **IN查询优化**: 完全可以走索引，优先创建单列索引
3. **向量搜索陷阱**: 向量Top-20可能被WHERE完全过滤，需先过滤后排序
4. **组合索引原则**: 前导列必须高选择性，避免低选择性常量列
5. **向量索引操作符**: 必须匹配SQL实际使用的操作符类型

### 优化分析的正确流程

```
1. 分析执行计划 (不是猜测)
   - 理解实际执行顺序
   - 识别真正的性能瓶颈

2. 查看统计信息 (pg_stats, not pg_stat_user_tables)
   - n_distinct, most_common_vals, most_common_freqs
   - 判断列的选择性

3. 检查现有索引 (pg_indexes, pg_stat_user_indexes)
   - 索引是否存在
   - 索引是否被使用
   - 索引定义是否匹配查询

4. 设计优化方案 (优先级排序)
   - P0: 解决主要瓶颈
   - P1: 次要优化
   - P2: 长期改进

5. 验证优化效果 (EXPLAIN ANALYZE)
   - 执行计划是否改变
   - 性能是否提升
   - 实际运行时间
```

### 避免错误的关键

- **不凭印象**: 查看实际数据，不靠记忆
- **不假设**: 验证每个结论
- **不盲目**: 理解原理再建议
- **不傲慢**: 承认错误，及时纠正

---

**文档版本**: v1.0
**最后更新**: 2026-04-17
**维护者**: xiang
**重要性**: 每次分析前必须回顾本文档，避免重复错误！
