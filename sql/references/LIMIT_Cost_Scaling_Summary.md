# LIMIT Cost缩放机制 - 总结文档

## 📋 完成时间

**日期**: 2026-04-17  
**版本**: v3.5  
**状态**: ✅ 已完成并同步到hermes系统技能

---

## 🎯 核心发现

### 1. LIMIT Cost缩放公式（源码级）

**源码位置**: `openGauss-server-v6.0.0/src/gausskernel/optimizer/path/costsize.cpp:2945-2946`

```cpp
// LIMIT Cost缩放的核心公式
if (plan->plan_rows > 0)
    plan->total_cost = 
        plan->startup_cost + 
        (plan->total_cost - plan->startup_cost) * count_rows / plan->plan_rows;
```

**关键点**:
- 只按比例缩放run_cost部分
- startup_cost不受影响
- Cost可能被大幅低估（10倍甚至更多）

### 2. default_limit_rows参数的两种模式

**源码位置**: `costsize.cpp:2877-2884`

**模式1: 百分比模式（default_limit_rows < 0）**
```cpp
// 例如 -10 表示 10%
count_rows = lefttree_rows × 10 / 100 = lefttree_rows × 0.1
```

**模式2: 直接限制模式（default_limit_rows > 0）**
```cpp
// 例如 100 表示限制为100行
count_rows = Min(100, lefttree_rows)
```

### 3. Cost被低估的数学原理

**示例: default_limit_rows = -10（10%模式）**

```
原始查询:
  startup_cost = 1000
  total_cost = 50000
  plan_rows = 1000000

应用LIMIT缩放:
  count_rows = 100000
  total_cost = 1000 + (50000 - 1000) × 0.1 = 5900

结果: Cost从50000降低到5900（降低8.5倍！）
```

**关键洞察**:
```
total_cost降低倍数 = 1 / (default_limit_rows的百分比)
本案例: 1 / 0.1 = 10倍
```

### 4. 优化器选择错误的根本原因

**问题链**:
```
1. IN子查询转换为Semi Join
2. 优化器应用default_limit_rows缩放
3. total_cost被大幅低估（10倍）
4. Nested Loop的Cost看起来很低
5. 优化器选择Nested Loop
6. 实际执行需要扫描全部外表
7. 性能极差（225秒 vs 0.478秒）
```

### 5. rows Hint的真正作用

**不是**: 调整LIMIT Cost缩放  
**而是**: 调整JOIN行数估算，引导正确的驱动顺序

```sql
SELECT /*+rows(sta_biz_info #2)*/ * FROM ...;

作用机制:
  1. 告诉优化器：JOIN后只有2行
  2. 优化器重新评估驱动顺序
  3. 选择小表驱动大表
  4. 自动触发HashAggregate去重
  5. LIMIT Cost缩放的基数更准确
```

---

## 📚 更新的文件

### 1. 核心技能文件

**文件**: `optimizer-deep.md`  
**版本**: v3.4 → **v3.5**  
**大小**: 67KB → **73KB**  
**行数**: 2387行 → **2619行** (+232行)

**新增内容**:
- 第三部分补充：LIMIT Cost缩放机制（232行）
- 包含核心公式、参数说明、数学效果分析
- IN/EXISTS子查询中的LIMIT陷阱
- 检测和优化方法
- 适用场景对比
- 源码位置索引

### 2. 案例分析文件

**文件**: `cases/limit_cost_scaling_analysis.md`  
**大小**: 13KB  
**内容**: 完整的LIMIT Cost缩放深度分析

**包含**:
- 源码级分析（cost_limit函数）
- default_limit_rows参数详解
- Cost缩放的数学原理
- 优化案例中的实际影响
- 适用场景和陷阱
- 优化建议

### 3. hermes系统技能

**文件**: `/home/xiang/.hermes/skills/vastbase-sql-optimizer/SKILL.md`  
**状态**: ✅ 已同步  
**大小**: 73KB  
**行数**: 2619行

---

## 🎓 核心知识点

### LIMIT Cost缩放原理

```
核心公式:
  total_cost = startup_cost + (total_cost - startup_cost) × (limit行数 / 原始plan_rows)

default_limit_rows参数:
  - 负数: 百分比模式（如-10表示10%）
  - 正数: 直接限制（如100表示100行）
  - -1: 100%（禁用缩放）
```

### Cost缩放效果

```
假设 default_limit_rows = -10 (10%模式)
原始total_cost = 50000
缩放后 ≈ 5000 (降低10倍)

影响:
  优化器看到的Cost严重偏低
  导致选择错误的执行计划
```

### 检测方法

```sql
-- 方法1: 查看Cost分布异常
-- 如果total_cost异常低，但被驱动表很大，可能是LIMIT缩放问题

-- 方法2: 临时禁用LIMIT缩放测试
SET default_limit_rows = -1;  -- 100%，即不缩放
EXPLAIN (ANALYZE, BUFFERS) your_slow_query;
-- 对比Cost变化
```

### 优化方案

**方案1: 使用rows Hint** ⭐推荐
```sql
SELECT /*+rows(sta_biz_info #2)*/ * FROM ent_entity
WHERE station_code IN (SELECT station_code FROM sta_biz_info WHERE ...);
```

**方案2: 改写为JOIN + DISTINCT**
```sql
SELECT EE.*
FROM ent_entity EE
JOIN (
  SELECT DISTINCT station_code FROM sta_biz_info WHERE ...
) ST ON EE.station_code = ST.station_code;
```

**方案3: 使用CTE**
```sql
WITH STATION_LIST AS (
  SELECT DISTINCT station_code FROM sta_biz_info WHERE ...
)
SELECT EE.*
FROM ent_entity EE
JOIN STATION_LIST ST ON EE.station_code = ST.station_code;
```

---

## 📖 相关源码位置

| 文件 | 行号 | 函数/内容 | 说明 |
|------|------|----------|------|
| costsize.cpp | 2877-2884 | `adjust_limit_row_count()` | 计算LIMIT行数 |
| costsize.cpp | 2893-2951 | `cost_limit()` | LIMIT Cost计算主函数 |
| costsize.cpp | 2945-2946 | LIMIT Cost缩放公式 | **核心公式** |
| guc.cpp | - | `default_limit_rows`参数 | 参数定义 |
| subselect.c | - | IN子查询转Semi Join | 查询转换 |
| joinpath.cpp | - | JOIN路径选择 | Cost比较 |

---

## 🔍 实战案例

### 优化前（错误选择）

```sql
-- 原SQL
SELECT * FROM ent_entity EE
WHERE EE.STATION_CODE IN (
  SELECT station_code FROM sta_biz_info
  WHERE REGION_ID IN (...)
)

执行计划:
  Nested Loop  (cost=0.00..14582.29 rows=14)
  ->  HashAggregate (rows=23)
  ->  Index Scan on ent_entity

问题:
  - Cost被低估10倍（14582）
  - 实际执行时间: 225秒
  - 优化器错误选择Nested Loop
```

### 优化后（rows Hint）

```sql
-- 使用rows Hint
SELECT /*+rows(sta_biz_info #2)*/ * FROM ent_entity
WHERE EE.STATION_CODE IN (
  SELECT station_code FROM sta_biz_info
  WHERE REGION_ID IN (...)
)

执行计划:
  Hash Join  (cost=4582.29..14642.83 rows=23)
  ->  Hash (23行)
     ->  HashAggregate (Group By Key: station_code)
  ->  Seq Scan on ent_entity

效果:
  - 正确选择Hash Join
  - HashAggregate自动去重: 28万行→23行
  - 执行时间: 0.478秒
  - 性能提升: 471倍
```

---

## ✅ 总结

### 关键洞察

1. **LIMIT Cost缩放是双刃剑**
   - ✅ 适用于真正的LIMIT查询（分页、TOP N）
   - ❌ 不适用于IN/EXISTS子查询（导致Cost低估）

2. **Cost低估的数学原理**
   - total_cost降低倍数 = 1 / (default_limit_rows的百分比)
   - default_limit_rows = -10 → Cost降低10倍

3. **优化器选择错误的根本原因**
   - IN子查询转换为Semi Join
   - 优化器应用default_limit_rows缩放
   - Cost被低估10倍
   - 选择Nested Loop而不是Hash Join

4. **rows Hint的真正作用**
   - 不调整LIMIT Cost缩放
   - 而是调整JOIN行数估算
   - 引导正确的驱动顺序

### 最佳实践

**推荐做法**:
- ✅ 优先使用rows Hint（不改写SQL）
- ✅ 考虑改写为JOIN + DISTINCT
- ✅ 使用CTE提高可读性
- ✅ 创建合适的索引

**避免做法**:
- ❌ 不要盲目禁用default_limit_rows
- ❌ 不要依赖LIMIT Cost缩放来优化IN子查询
- ❌ 不要忽略执行计划中的actual rows

### 学习资源

**已更新的文档**:
1. `optimizer-deep.md` - 核心技能文件（v3.5）
2. `cases/limit_cost_scaling_analysis.md` - 深度分析
3. `/home/xiang/.hermes/skills/vastbase-sql-optimizer/SKILL.md` - 系统技能

**源码分析**:
- openGauss v6.0.0源码
- costsize.cpp:2877-2951行
- LIMIT Cost缩放的完整实现

---

**完成时间**: 2026-04-17  
**作者**: xiang  
**基于**: openGauss v6.0.0源码分析 + 真实优化案例
