# SQLHC深度分析规则（2026-04-17新增）

## 为什么需要这个规则？

来自真实案例的教训：

### 错误案例1：凭经验判断"缺索引"
```
分析过程：
1. 看到执行计划显示Seq Scan on ent_entity
2. 立即判断"缺少索引"
3. 建议创建复合索引：idx_ent_entity_del_type_station

用户质疑：
"索引不存在吗？有检查SQLHC中的索引信息吗？"

重新检查SQLHC报告：
发现所有索引都已存在：
- idx_ent_entity_station_code (4406 MB) ✅
- idx_ent_entity_res_type_id (1778 MB) ✅
- idx_ent_entity_state (3156 MB) ✅
- idx_glb_entity_code (5638 MB) ✅

真相：
索引已存在，只是JOIN顺序错误导致没用上！
```

### 错误案例2：忽略最终返回行数
```
分析过程：
1. 看到外表扫描105万行
2. 看到标量子查询SubPlan
3. 判断"标量子查询执行105万次，性能差"
4. 建议改为LEFT JOIN

用户质疑：
"外表真实返回行数那么少，为什么要改LEFT JOIN？"

重新检查执行计划：
Line 68: Limit rows=10（最终只返回10行）
Line 149-197: SubPlan loops=10（标量子查询只执行10次）

真相：
- 最终只返回10行（ROWNUM <= 10）
- 标量子查询只执行10次（10个 × 10行）
- 每次Index Scan，总成本<10ms
- 不需要改LEFT JOIN（过度优化）
```

### 错误案例3：误判统计信息偏差（n_live_tup vs reltuples）⭐⭐⭐⭐⭐
```
分析过程：
1. 看到n_live_tup = 10，reltuples = 1,978,161
2. 判断"统计信息严重偏差！197万倍偏差！"
3. 建议ANALYZE table

用户质疑：
"n_live_tup没有收集到，不能作为主要参考标准，这个只能判断表是否膨胀死元组是否过多的参考"

重新分析：
步骤1：理解n_live_tup的含义
└─ n_live_tup：pg_stat_user_tables的实时统计
   └─ 含义：从上次重置统计以来的存活行数（不是表的全部数据）
      ⚠️ 只是"最近变化"的统计，不是表的全部行数！

步骤2：理解reltuples的含义
└─ reltuples：pg_class的统计信息（优化器使用）
   └─ 含义：表的行数估算（用于Cost计算）

步骤3：对比执行计划验证
└─ 执行计划扫描：2,099,412行
└─ reltuples：1,978,161
└─ 结论：统计信息基本准确，不是偏差！

步骤4：检查last_analyze时间
└─ 如果last_analyze为空 → 可能需要ANALYZE
└─ 如果last_analyze很新 → 统计信息是最新的

真相：
- n_live_tup ≠ reltuples
- n_live_tup只是参考，不是判断统计信息偏差的标准
- 判断标准：对比执行计划actual rows vs 预估rows
- 本案例：统计信息准确，问题不是统计信息偏差
```

### 错误案例4：建议低选择性列加入组合索引
```
分析过程：
1. 看到WHERE：doc_id IN (2个值) AND status = 1 AND is_deleted = 0
2. 检查：doc_id无索引，需要创建
3. 建议：CREATE INDEX idx_doc_id_status_deleted ON table(doc_id, status, is_deleted)

用户质疑：
"status, is_deleted 统计信息都说全是1个值了，有必要加入组合索引吗？你参考的是skill吗？"

重新分析：
步骤1：检查列统计信息（tabstat Column Statistics）
└─ status: n_distinct=1, top3_freqs=1(1.0) → 100%都是1
└─ is_deleted: n_distinct=1, top3_freqs=0(1.0) → 100%都是0

步骤2：计算选择性
└─ status选择性 = 1.0（100%，不过滤）
└─ is_deleted选择性 = 1.0（100%，不过滤）

步骤3：判断是否需要加入组合索引
└─ ❌ status和is_deleted选择性极差，不需要加入索引
└─ ✅ 只创建doc_id单列索引即可

组合索引原则：
- 只包含高选择性列（n_distinct大，top3_freqs分布均匀）
- 低选择性列（selectivity=1.0）加入无意义，只会增加维护成本

真相：
- selectivity = 1.0 → 不需要索引
- 选择性计算：selectivity = distinct值数量 / 总行数
- top3_freqs显示100%都是单一值 → selectivity=1.0
```

### 错误案例6：OR展开改写使用UNION ALL导致数据重复 ⭐⭐⭐⭐⭐
```
分析过程：
1. 看到OR条件：(A AND B) OR (C AND D)
2. 建议手动展开为：
   SELECT ... WHERE (A AND B)
   UNION ALL  -- ❌ 错误！
   SELECT ... WHERE (C AND D)

用户质疑：
"OR展开不能这样改写呀，这样可能多数据呀，因为存在两个分支都满足的行呀"

问题分析：
- UNION ALL：不去重，直接合并两个分支的结果
- 如果一行数据同时满足分支1和分支2，会被返回2次
- 这会导致结果集重复，数据错误！

正确做法：
方法1：使用UNION（去重）
SELECT ... WHERE (A AND B)
UNION  -- ✅ 会去重
SELECT ... WHERE (C AND D)

方法2：确保分支互斥（推荐，性能更好）⭐⭐⭐⭐⭐
如果业务上保证：
- 分支1和分支2不会同时满足（互斥）
- 可以使用UNION ALL（性能更好）
- 但必须先验证业务逻辑！
- ⚠️ 更保险的做法：在分支2添加NOT条件，明确排除分支1

实战案例：
WHERE (knowledge_type = 3 AND knowledge_id = 347)
   OR (knowledge_type = 2 AND company_id = 0)

❌ 错误改写1：
SELECT ... WHERE knowledge_type = 3 AND knowledge_id = 347
UNION ALL
SELECT ... WHERE knowledge_type = 2 AND company_id = 0
-- 问题：如果一行数据同时满足两个分支，会重复

❌ 错误改写2：
SELECT ... WHERE knowledge_type = 3 AND knowledge_id = 347
UNION
SELECT ... WHERE knowledge_type = 2 AND company_id = 0
-- 问题：UNION会去重，如果结果集中有2行相同的数据，会被合并成1行
-- 导致数据丢失！

✅ 正确改写（方法1 - 使用UNION）：
SELECT ... WHERE knowledge_type = 3 AND knowledge_id = 347
UNION  -- 使用UNION去重
SELECT ... WHERE knowledge_type = 2 AND company_id = 0
-- 适用场景：允许UNION的去重开销，确保不重复

✅ 正确改写（方法2 - 使用UNION ALL + 分支互斥）⭐⭐⭐⭐⭐：
SELECT ... WHERE knowledge_type = 3 AND knowledge_id = 347
UNION ALL  -- 使用UNION ALL（性能更好）
SELECT ... WHERE knowledge_type = 2 AND company_id = 0
  AND NOT (  -- ✅ 关键：排除满足分支1的行
      knowledge_type = 3
      AND knowledge_id = 347
  )
-- 适用场景：
-- 1. 业务上分支可能重叠（不互斥）
-- 2. 需要在分支2添加NOT条件排除分支1
-- 3. 性能比UNION好（无去重开销）

✅ 正确改写（方法3 - 业务保证互斥 + UNION ALL）：
如果业务上保证knowledge_type = 2和3不会同时满足：
SELECT ... WHERE knowledge_type = 3 AND knowledge_id = 347
UNION ALL  -- ✅ 业务保证互斥，可以安全使用
SELECT ... WHERE knowledge_type = 2 AND company_id = 0
-- 适用场景：
-- 1. 业务明确保证分支互斥（如本例knowledge_type不同）
-- 2. 性能最优
-- 3. 需要文档说明互斥假设

关键教训：
- OR展开时，必须考虑分支是否互斥
- 如果不互斥，有两个选择：
  1. 使用UNION（会去重，但有开销）
  2. 使用UNION ALL + 在分支2添加NOT条件（推荐，性能更好）
- 如果互斥，可以使用UNION ALL（性能最好）
- 不要盲目使用UNION ALL，必须分析业务逻辑
- ⚠️ 更保险的做法：总是在分支2添加NOT条件，即使业务声称互斥
```

### 错误案例6.5：OR展开改写时UNION导致数据丢失 ⭐⭐⭐⭐⭐
```
分析过程：
1. 看到OR条件：(A AND B) OR (C AND D)
2. 建议手动展开为：
   SELECT ... WHERE (A AND B)
   UNION  -- ❌ 错误！会导致数据丢失
   SELECT ... WHERE (C AND D)

用户质疑：
"OR展开改写不能union呀，union会少数据呀，加入有2两行一样的"

问题分析：
- UNION：会去重，合并相同的数据行
- 如果结果集中有2行完全相同的数据，会被合并成1行
- 这会导致数据丢失！

示例说明：
假设原始结果：
Row1: id=1, name='Alice', age=25
Row2: id=2, name='Bob', age=30
Row3: id=3, name='Charlie', age=25  -- 与Row1的age相同

使用UNION去重后：
Row1: id=1, name='Alice', age=25
Row2: id=2, name='Bob', age=30
Row3: id=3, name='Charlie', age=25  -- 如果Row1和Row3完全相同，会被合并

⚠️ 关键问题：
- 即使id不同，如果所有列的值都相同，UNION也会去重
- 这会导致数据丢失！
- OR条件不会去重，但UNION会！

正确做法：
推荐：UNION ALL + 分支互斥（方法2）⭐⭐⭐⭐⭐
SELECT ... WHERE (A AND B)
UNION ALL  -- ✅ 不去重，保留所有数据
SELECT ... WHERE (C AND D)
  AND NOT (  -- ✅ 排除满足分支1的行
      A AND B
  )

关键对比：
方法        | 是否去重 | 是否重复 | 是否丢失数据 | 性能
------------|---------|---------|-------------|------
UNION       | ✅ 是   | ❌ 否   | ✅ 会丢失   | 慢（去重开销）
UNION ALL   | ❌ 否   | ✅ 会重复 | ❌ 不丢失   | 快
UNION ALL+NOT| ❌ 否 | ❌ 否   | ❌ 不丢失   | 快（推荐）

关键教训：
- UNION会去重，导致数据丢失！❌
- OR条件不会去重，改写时必须保留这个特性
- 必须使用UNION ALL + NOT条件，确保：
  1. 不去重（保留所有数据）
  2. 不重复（分支互斥）
  3. 性能最优（无去重开销）
- 这才是OR展开的正确改写方法
```

### 错误案例7：建议低选择性列加入组合索引 ⭐⭐⭐⭐⭐
```
分析过程：
1. 看到WHERE：doc_id IN (2个值) AND status = 1 AND is_deleted = 0
2. 检查：doc_id无索引，需要创建
3. 建议：CREATE INDEX idx_doc_id_status_deleted ON table(doc_id, status, is_deleted)

用户质疑：
"status, is_deleted 统计信息都说全是1个值了，有必要加入组合索引吗？你参考的是skill吗？"

重新分析：
步骤1：检查列统计信息（tabstat Column Statistics）
└─ status: n_distinct=1, top3_freqs=1(1.0) → 100%都是1
└─ is_deleted: n_distinct=1, top3_freqs=0(1.0) → 100%都是0

步骤2：计算选择性
└─ status选择性 = 1.0（100%，不过滤）
└─ is_deleted选择性 = 1.0（100%，不过滤）

步骤3：判断是否需要加入组合索引
└─ ❌ status和is_deleted选择性极差，不需要加入索引
└─ ✅ 只创建doc_id单列索引即可

组合索引原则：
- 只包含高选择性列（n_distinct大，top3_freqs分布均匀）
- 低选择性列（selectivity=1.0）加入无意义，只会增加维护成本

真相：
- selectivity = 1.0 → 不需要索引
- 选择性计算：selectivity = distinct值数量 / 总行数
- top3_freqs显示100%都是单一值 → selectivity=1.0
- 必须检查n_distinct和top3_freqs，不能凭直觉
```

### 错误案例8：输出格式不符合用户要求 ⭐⭐⭐⭐⭐
```
用户要求：按照技能要求的输出格式分析

实际行为：
- 没有输出"一、性能瓶颈（源码级分析）"
- 没有输出"二、优化方案 2.1不改写SQL 2.2可改写SQL"
- 输出了大量分析过程，但没有按要求的结构输出

用户反馈：
"重新分析，按要求给输出，怎么输出又不是我要求的样子呢？"

正确行为：
严格按技能输出格式：

一、性能瓶颈（源码级展开costsize.c/indxpath.c）
├─ P0致命瓶颈：xxx问题（源码级分析）
├─ P1严重瓶颈：xxx问题
└─ P2中等瓶颈：xxx问题

二、优化方案
├─ 2.1 不改写SQL（推荐）
│   ├─ 完整可执行SQL（CREATE INDEX ...）
│   ├─ 预期效果（执行时间、扫描行数）
│   └─ 实施步骤（验证命令）
└─ 2.2 可改写SQL（可选）
    ├─ 完整可执行SQL（改写后的SQL）
    ├─ 预期效果
    └─ 对比分析

关键教训：
- 必须严格按照技能输出格式
- 不要输出分析过程，直接输出结果
- 输出格式是用户明确要求的，必须遵守
- 如果用户要求重新按格式输出，立即纠正，不要再分析过程
```

## 核心规则（必须遵守）

### 规则1：必须先深入分析SQLHC报告 ⭐⭐⭐⭐⭐

**如果输入是SQLHC文件，必须按以下步骤分析**：

```
步骤0: SQLHC深度分析（最高优先级）
  │
  ├─ 0.0 【P0级】统计信息偏差检查（最高优先级，2026-04-17新增）
  │   ├─ 对比：reltuples（统计信息行数）vs n_live_tup（实际存活行数）
  │   ├─ 检查：last_analyze是否为空（从未收集统计信息）
  │   ├─ 判断：偏差>10倍需要ANALYZE
  │   └─ 实战案例：
  │       ├─ reltuples=1,978,161（统计信息）
  │       ├─ n_live_tup=10（实际存活）
  │       ├─ 偏差=197,000倍！
  │       ├─ 优化器基于错误统计选择Seq Scan
  │       └─ 解决方案：ANALYZE（788倍提升）
  │
  ├─ 0.1 检查Index Statistics章节

```
步骤0: SQLHC深度分析（最高优先级）
  │
  ├─ 0.0 【P0级】统计信息偏差检查（最高优先级，2026-04-17更新）
  │   ├─ ⚠️ 警告：n_live_tup ≠ reltuples
  │   │   ├─ n_live_tup：pg_stat实时统计（从上次重置统计以来的存活行数）
  │   │   │   └─ 只是"最近变化"的统计，**不是表的全部行数**
  │   │   │   └─ 用途：判断表是否膨胀、死元组是否过多
  │   │   ├─ reltuples：pg_class统计信息（优化器使用）
  │   │   │   └─ 表的行数估算（用于Cost计算）
  │   │   │   └─ 这是优化器依赖的数据
  │   │   └─ ❌ 错误做法：用n_live_tup判断统计信息偏差
  │   │       ├─ 反例：n_live_tup=10，reltuples=1,978,161
  │   │       ├─ 判断："197万倍偏差！" → ❌ 错误！
  │   │       ├─ 原因：n_live_tup只统计了最近的数据变化
  │   │       └─ 正确：表中确实有约197万行数据
  │   │
  │   ├─ ✅ 正确判断方法：对比执行计划
  │   │   ├─ 步骤1：查看执行计划actual rows vs 预估rows
  │   │   │   └─ 如果接近 → 统计信息准确
  │   │   │   └─ 如果偏差大（>10倍）→ 统计信息偏差
  │   │   ├─ 步骤2：检查last_analyze时间
  │   │   │   └─ 如果last_analyze为空 → 可能需要ANALYZE
  │   │   │   └─ 如果last_analyze很新 → 统计信息是最新的
  │   │   └─ 步骤3：对比reltuples vs actual rows
  │   │       ├─ 例1：reltuples=1,978,161，actual=2,099,412
  │   │       │   └─ 接近（1.06倍），统计信息准确 ✅
  │   │       └─ 例2：reltuples=100，actual=1,000,000
  │   │           └─ 偏差10,000倍，统计信息偏差 ❌
  │   │
  │   └─ 实战案例对比：
  │       ├─ 案例A（真正偏差）：
  │       │   ├─ reltuples=1,978,161（统计信息）
  │       │   ├─ actual rows=21（执行计划）
  │       │   ├─ last_analyze：空（从未收集）
  │       │   ├─ 偏差：94,195倍（1,978,161 / 21）
  │       │   ├─ 判断：统计信息严重偏差 ❌
  │       │   └─ 解决：ANALYZE table
  │       │
  │       └─ 案例B（误判偏差）：
  │           ├─ n_live_tup=10（pg_stat统计）
  │           ├─ reltuples=1,978,161（pg_class统计）
  │           ├─ actual rows=2,099,412（执行计划）
  │           ├─ 判断："n_live_tup vs reltuples偏差197万倍！"
  │           ├─ ❌ 错误：n_live_tup只是最近统计，不是全部
  │           ├─ ✅ 正确：reltuples vs actual接近（1.06倍），统计准确
  │           └─ 结论：问题不是统计信息偏差，而是其他原因
  │
  ├─ 0.1 检查Index Statistics章节
  │   ├─ 搜索关键字："Index"、"idx_"、"btree"、"bitmap"
  │   ├─ 列出所有表的所有现有索引
  │   │   表名 | 索引名 | 索引类型 | 索引列 | 索引大小
  │   │   ent_entity | idx_ent_entity_station_code | btree local | station_code | 4406 MB
  │   │   ent_entity | idx_ent_entity_res_type_id | btree local | res_type_id | 1778 MB
  │   │   ...
  │   ├─ 判断：需要的索引是否已存在？
  │   │   JOIN条件列的索引？
  │   │   WHERE条件列的索引？
  │   │   ORDER BY列的索引？
  │   └─ 输出：索引清单表格
  │
  ├─ 0.2 检查Table Statistics章节
  │   ├─ 搜索关键字："pg_class"、"reltuples"、"relpages"
  │   ├─ 提取关键信息：
  │   │   表名 | 行数(reltuples) | 大小 | 分区数
  │   │   ent_entity | 78876337 | 57 GB | 15
  │   │   sta_biz_info | 6428373 | 4.7 GB | 0
  │   │   ...
  │   └─ 判断：哪些表是大表？哪些是小表？
  │
  ├─ 0.3 检查Column Statistics章节（如果有）
  │   ├─ 列选择性：n_distinct
  │   ├─ 列相关性：correlation
  │   ├─ MCV值：most common values
  │   ├─ NULL值比例：null_frac
  │   └─ 判断：统计信息是否准确？
  │
  ├─ 0.4 执行计划分析
  │   ├─ JOIN顺序：谁驱动谁？
  │   │   小表驱动大表 ✅
  │   │   大表驱动小表 ❌
  │   ├─ JOIN方式：Nested Loop / Hash / Merge
  │   ├─ 扫描节点：Seq Scan / Index Scan / Bitmap
  │   ├─ 关键性能指标：
  │   │   - 执行时间：actual time
  │   │   - 返回行数：rows
  │   │   - 循环次数：loops（关键！）
  │   │   - 过滤行数：Rows Removed by Filter
  │   └─ Limit节点：最终返回多少行？
  │
  ├─ 0.5 关键判断
  │   ├─ 【优先级0】统计信息是否准确？（最高优先级）
  │   │   ├─ reltuples vs n_live_tup偏差>10倍？
  │   │   ├─ last_analyze是否为空？
  │   │   ├─ 预估行数 vs 实际行数差距>10倍？
  │   │   └─ ⚠️ 统计信息偏差是P0级瓶颈！
  │   │
  │   ├─ 索引没用上 ≠ 缺少索引
  │   │   ├─ 可能原因1：JOIN顺序错误 ⭐先检查这个
  │   │   ├─ 可能原因2：统计信息偏差
  │   │   ├─ 可能原因3：索引选择性差
  │   │   └─ 可能原因4：缺少索引（最后才判断）
  │   │
  │   ├─ Seq Scan ≠ 缺少索引
  │   │   ├─ 小表全表扫描可能更快
  │   │   ├─ 高选择性过滤条件才需要索引
  │   │   ├─ JOIN顺序正确后，索引可能自动被使用
  │   │   └─ 先修正JOIN顺序，再判断是否需要索引
  │   │
  │   ├─ SubPlan loops ≠ 外表行数
  │   │   ├─ loops = 最终返回行数（如果有Limit）
  │   │   ├─ 不是中间扫描行数！
  │   │   ├─ 本案例：loops=10（最终10行），不是105万
  │   │   └─ 标量子查询成本 = loops × 次数 × 单次成本
  │   │
  │   └─ 统计信息偏差检查
  │       ├─ 预估行数 vs 实际行数（差距>10倍需关注）
  │       ├─ 预估Cost vs 实际时间（差距>10倍需关注）
  │       └─ 需要ANALYZE收集统计信息
  │
  └─ 0.6 输出SQLHC分析结果
      ├─ 表清单：表名、行数、大小、分区数
      ├─ 索引清单：表名、索引名、索引列、索引大小
      ├─ 关键发现：
      │   ├─ ✅ 哪些索引已存在
      │   ├─ ❌ 哪些索引缺失（最后才判断）
      │   ├─ ⚠️ 哪些统计信息可能偏差
      │   └─ 🔴 JOIN顺序是否错误
      └─ 建议：
          ├─ 是否需要创建索引（必须基于现有索引判断）
          ├─ 是否需要修正JOIN顺序
          └─ 是否需要收集统计信息
```

### 规则2：严格优先级分析 ⭐⭐⭐⭐⭐

```
P0: JOIN顺序（10-1000倍影响）⭐⭐⭐⭐⭐
  ├─ 小表驱动大表 ✅
  ├─ 大表驱动小表 ❌ ← 最致命！
  └─ 案例：大表驱动小表导致226倍性能损失

P1: JOIN方式（2-50倍影响）⭐⭐⭐⭐
  ├─ Nested Loop（小表+索引）
  ├─ Hash Join（大表+无索引）
  └─ Merge Join（排序+排序）

P2: 扫描节点（1.1-10倍影响）⭐⭐⭐
  ├─ Seq Scan（全表扫描）
  ├─ Index Scan（索引扫描）
  └─ Bitmap Scan（位图扫描）

P3: 其他（1-2倍影响）⭐⭐
  ├─ 标量子查询
  ├─ 排序
  └─ 聚合
```

**关键要点**：
- 优先级不能乱！先看JOIN顺序，再看其他
- JOIN顺序错误，索引再多也没用
- 必须严格按照：P0 → P1 → P2 → P3

### 规则3：不要凭直觉，要基于数据 ⭐⭐⭐⭐⭐

**错误思维**：
```
❌ "看到Seq Scan就判断缺索引"
❌ "看到SubPlan就判断性能差"
❌ "凭经验建议创建复合索引"
❌ "假设可能缺某个索引"
```

**正确思维**：
```
✅ 先检查SQLHC报告的Index Statistics章节
✅ 列出所有现有索引
✅ 分析为什么索引没用上
✅ 对比预估行数vs实际行数
✅ 分析JOIN顺序是否正确
✅ 基于数据判断是否需要新索引
```

## 实战案例验证

### 案例1：JOIN顺序错误导致索引失效

**SQLHC报告发现**：
```
现有索引：
- idx_ent_entity_station_code (4406 MB) ✅
- idx_sta_biz_info_region_station (415 MB) ✅
- 所有需要的索引都已存在

执行计划：
- Seq Scan on ent_entity（105万行）
- 大表ent_entity驱动小表sta_biz_info

性能问题：
- 执行时间：225秒
- CTE重复执行：105万次
- 索引没用上
```

**错误分析**：
```
❌ 看到Seq Scan，就说"缺少索引"
❌ 建议创建：idx_ent_entity_del_type_station
```

**正确分析**：
```
✅ 检查SQLHC：所有索引都已存在
✅ 发现JOIN顺序错误：大表驱动小表
✅ 结论：不需要创建索引
✅ 建议：修正JOIN顺序（rows Hint）
```

**优化效果**：
```
rows Hint（5分钟）：
- 执行时间：225秒 → <1秒（226倍提升）
- 不需要创建任何索引
- JOIN顺序正确后，索引自动被使用
```

### 案例2：标量子查询被误判

**执行计划显示**：
```
Line 68: Limit rows=10（最终只返回10行）
Line 90: Seq Scan on ent_entity rows=1054453（扫描105万行）
Line 149-197: SubPlan 1-10（标量子查询）
```

**错误分析**：
```
❌ 看到外表扫描105万行
❌ 判断"标量子查询执行105万次"
❌ 建议改为LEFT JOIN
```

**正确分析**：
```
✅ 检查SubPlan的loops字段：loops=10
✅ 发现最终只返回10行（Limit）
✅ 标量子查询只执行10次（10个 × 10行）
✅ 每次Index Scan，总成本<10ms
✅ 结论：不需要改LEFT JOIN
```

**关键要点**：
```
- SubPlan loops = 最终返回行数（不是中间扫描行数）
- 有Limit时，标量子查询只执行Limit次数
- 成本<10ms时，不需要优化
```

## SQLHC深度分析检查清单

分析SQLHC文件时，必须完成以下检查：

- [ ] 1. 【P0级】统计信息偏差检查（最高优先级）
  - [ ] 对比：reltuples vs n_live_tup
  - [ ] 检查：last_analyze是否为空
  - [ ] 判断：偏差>10倍需要ANALYZE
  - [ ] 评估：统计信息偏差对执行计划的影响
  - [ ] 判断：这是否是根本原因？
  - [ ] 0.0 Index Statistics章节检查
- [ ] 2. Index Statistics章节检查
  - [ ] 标记索引类型（btree/hash/unique）
  - [ ] 标记索引列（单列/复合）
  - [ ] 标记索引大小（MB）
  - [ ] 判断：JOIN条件列是否有索引？

- [ ] 2. Index Statistics章节检查
  - [ ] 列出所有表的所有索引
  - [ ] 标记索引类型（btree/hash/unique）
  - [ ] 标记索引列（单列/复合）
  - [ ] 标记索引大小（MB）
  - [ ] 判断：JOIN条件列的索引？
  - [ ] 判断：WHERE条件列的索引？

- [ ] 3. Table Statistics章节检查
- [ ] 3. Table Statistics章节检查
  - [ ] 列出所有表的行数
  - [ ] 列出所有表的大小
  - [ ] 标记大表（>100万行）
  - [ ] 标记小表（<1万行）
  - [ ] 判断：哪些表应该当驱动表？

- [ ] 4. Column Statistics章节检查（如果有）
  - [ ] 列选择性（n_distinct）
  - [ ] 列相关性（correlation）
  - [ ] MCV值（most common values）
  - [ ] NULL值比例（null_frac）
  - [ ] 判断：统计信息是否准确？

- [ ] 4. 执行计划深度分析
  - [ ] JOIN顺序：谁驱动谁？
  - [ ] 小表驱动大表？✅ 大表驱动小表？❌
  - [ ] JOIN方式：Nested Loop / Hash / Merge
  - [ ] 扫描节点：Seq Scan / Index Scan / Bitmap
  - [ ] 关键指标：
    - [ ] 执行时间（actual time）
    - [ ] 返回行数（rows）
    - [ ] 循环次数（loops）← 关键！
    - [ ] 过滤行数（Rows Removed by Filter）
  - [ ] Limit节点：最终返回多少行？

- [ ] 5. SubPlan/CTE分析
  - [ ] SubPlan的loops字段（真实执行次数）
  - [ ] CTE Scan的loops字段（是否重复执行）
  - [ ] 标量子查询执行次数 = loops × 次数
  - [ ] 递归CTE执行次数

- [ ] 6. 索引使用判断
  - [ ] 索引没用上的原因？
    - [ ] JOIN顺序错误？⭐ 先检查这个
    - [ ] 统计信息偏差？
    - [ ] 索引选择性差？
    - [ ] 缺少索引？← 最后才判断
  - [ ] 是否需要创建新索引？
    - [ ] 基于现有索引判断
    - [ ] 不要凭经验建议

- [ ] 7. 输出分析结果
  - [ ] 表清单表格
  - [ ] 索引清单表格
  - [ ] 关键发现：
    - [ ] ✅ 哪些索引已存在
    - [ ] ❌ 哪些索引缺失
    - [ ] 🔴 JOIN顺序是否错误
    - [ ] ⚠️ 统计信息是否偏差
  - [ ] 优化建议：
    - [ ] 基于现有索引
    - [ ] 按优先级P0→P1→P2→P3
    - [ ] 量化性能提升

## 核心原则总结

1. **必须先检查SQLHC报告**（特别是Index Statistics）
2. **严格优先级分析**（P0→P1→P2→P3）
3. **索引没用上 ≠ 缺少索引**（可能是JOIN顺序错误）
4. **不要凭直觉，要基于数据**（SQLHC报告数据）
5. **SubPlan loops = 最终行数**（不是中间扫描行数）
6. **先修正JOIN顺序，再判断是否需要索引**
7. **输出文件命名格式** ⭐⭐⭐⭐⭐（2026-04-17新增）
   - 格式：`vastbase_sql_tuning_YYYY-MM-DD_HH-mm-ss.md`
   - 位置：`<vastbase-optimizer-notes>/cases/`
   - 示例：`vastbase_sql_tuning_2026-04-17_14-30-25.md`
   - ❌ 不要使用：`ai_knowledge_vector_优化分析_2026-04-17.md`
   - ❌ 不要使用：`vastbase_ai_sql_analysis_2026-04-17_14-30-25.md`（错误前缀）
   - ❌ 不要使用：`analysis.md`
   - ✅ 必须使用：`vastbase_sql_tuning_`（标准前缀）

8. **分析方法：SQLHC + 源码深度分析** ⭐⭐⭐⭐⭐（2026-04-17新增）
   - 步骤1：**SQLHC深度分析**（必须）
     - 分析索引统计信息（Index Statistics）
     - 分析表统计信息（Table Statistics）
     - 分析列统计信息（Column Statistics）
     - 分析执行计划（Execution Plan）
   - 步骤2：**源码级分析**（根据问题类型查阅**所有相关**文档）
     - ⚠️ **重要**：不是只看一篇文章，而是查阅**所有相关文章**
     - JOIN顺序问题 → 查阅以下文档：
       - `VastBase_Optimizer_03_Join_Order_GEQO_Deep_Dive.md`（JOIN顺序核心）
       - `VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md`（Cost计算影响JOIN顺序）
       - `VastBase_Optimizer_05_Path_Plan_Generation_Deep_Dive.md`（路径生成）
       - `VastBase_Optimizer_01_Query_Transformation_Deep_Dive.md`（查询转换可能影响JOIN）
     - 统计信息问题 → 查阅以下文档：
       - `VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md`（统计信息核心）
       - `VastBase_Optimizer_05_Path_Plan_Generation_Deep_Dive.md`（Cost计算）
       - `VastBase_Optimizer_04_Parameters_Index_Deep_Dive.md`（参数影响统计）
     - Sublink问题 → 查阅以下文档：
       - `VastBase_Optimizer_01_Query_Transformation_Deep_Dive.md`（Sublink转换）
       - `VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md`（Cost计算）
       - `VastBase_Optimizer_05_Path_Plan_Generation_Deep_Dive.md`（路径生成）
       - `vastbase-sublink-to-join-pattern`（Sublink转JOIN模式技能）
       - `vastbase-sublink-optimization`（Sublink优化技能）
     - LIMIT Cost问题 → 查阅以下文档：
       - `LIMIT_Cost_Scaling_Summary.md`（LIMIT Cost核心）
       - `VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md`（Cost计算）
       - `VastBase_Optimizer_05_Path_Plan_Generation_Deep_Dive.md`（路径生成）
     - 索引问题 → 查阅以下文档：
       - `VastBase_Optimizer_04_Parameters_Index_Deep_Dive.md`（索引核心）
       - `VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md`（统计信息影响索引选择）
       - `SQLHC_DEEP_ANALYSIS_RULES.md`（索引使用规则）
     - Function属性问题 → 查阅以下文档：
       - `VastBase_Optimizer_06_Advanced_Features_Deep_Dive.md`（Function属性）
       - `VastBase_Optimizer_01_Query_Transformation_Deep_Dive.md`（查询转换）
       - `VastBase_Optimizer_09_Parallel_Executor_Tuning_Deep_Dive.md`（SMP并行）
     - SMP并行问题 → 查阅以下文档：
       - `VastBase_Optimizer_09_Parallel_Executor_Tuning_Deep_Dive.md`（SMP核心）
       - `VastBase_Optimizer_10_SMP_Hints_Rewrite_Rules_Deep_Dive.md`（SMP Hint）
       - `VastBase_Optimizer_06_Advanced_Features_Deep_Dive.md`（Function属性影响SMP）
     - 查询转换问题 → 查阅以下文档：
       - `VastBase_Optimizer_01_Query_Transformation_Deep_Dive.md`（查询转换核心）
       - `VastBase_Optimizer_05_Path_Plan_Generation_Deep_Dive.md`（路径生成）
       - `VastBase_Optimizer_02_Statistics_Cost_Deep_Dive.md`（Cost计算）
   - 步骤3：**结合分析**（SQLHC数据 + 所有相关源码知识）
     - 用源码解释优化器行为
     - 用SQLHC数据验证源码分析
     - 给出源码级别的优化建议

---

**创建日期**：2026-04-17
**创建原因**：基于真实优化案例的教训总结
**适用场景**：当用户传入SQLHC文件进行分析时

## 输出格式风格指南（重要用户体验）

### 简洁专业原则（2026-04-17新增）

来自用户体验反馈的重要改进：

**核心原则**：
- ✅ 推荐：用"源码分析"而非"源码级分析"
- ✅ 推荐：章节标题简洁，如"## 二、性能瓶颈"
- ❌ 避免：过度强调标签，如"（源码级深度分析）"
- ❌ 避免：浮夸表述，如"超深度"、"顶级"等

**用户体验黄金法则**：
- 📝 **核心原则**：**做了事情但不一定要标签着重说明**
- 🎯 **输出目标**：专业但不浮夸，深度但不冗余
- 💡 **实践指导**：
  - 证明能力靠内容质量，不是靠标签强调
  - 用户更关心实际优化效果，不是分析级别的词汇
  - 低调专业比高调炫耀更有价值

**示例对比**：

❌ **过度强调**（不推荐）：
```markdown
## 一、性能瓶颈（源码级深度分析）
**源码级分析**（VastBase源码位置）：
超深度Cost计算分析（顶级专家级）
```

✅ **简洁专业**（推荐）：
```markdown
## 一、性能瓶颈
**源码分析**（VastBase源码位置）：
Cost计算分析（costsize.c）
```

**为什么这样改进**：
- 用户反馈："咋还没记住呢 刚刚才说的" - 说明之前过度强调"源码级"标签
- 专业性的证明应该体现在分析质量上，而不是重复强调"级别"词汇
- 简洁的格式让用户更容易聚焦到实际的优化方案上

---

**最后更新**：2026-04-17
**更新原因**：基于用户体验反馈，去除过度强调的"级别"标签
**适用场景**：所有SQL优化分析报告的输出格式
