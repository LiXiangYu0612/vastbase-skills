# 分区表机制与判读（partition-table.md）

## 概述

> 服务分类：internals（机制层）+ performance（AWR/计划判读）+ troubleshooting（DDL 锁与报错）。
> 本文件回答：分区在内核里怎么表示（与 Postgres 声明式分区的本质差异）、裁剪何时发生
> 何时失效、INSERT 怎么路由、interval 自动扩分区的代价、LOCAL/GLOBAL 索引的默认陷阱、
> EXCHANGE 的锁面、分区级统计在哪、超多分区的四类代价形态。
> 〔排除〕cstore/ustore/段页式/nvm 路径不含；VALUE 分区（value-partition，最多 4 键，
> partitionmap.h:63-64）是分布式形态，集中式不涉。

## 1. 对象与目录模型：分区不是 pg_class 关系

本内核是自管分区目录的老式架构，**不是** Postgres 声明式分区（分区没有独立 pg_class 行）：

- 父表：pg_class 一行，`parttype` 标记分区形态——`'p'` 分区表 / `'s'` 二级分区表 /
  `'n'` 普通表（pg_class.h:187-190，PARTTYPE_PARTITIONED_RELATION 等）。
- 每个分区：**pg_partition**（OID 9016）一行（pg_partition.h:31-65），关键列：
  - `parentid`（:37）挂父表 pg_class.oid——分区树靠它串；
  - `partstrategy`（:40）策略；`partkey`（:62 int2vector）分区键列号；
  - `relfilenode`（:41）**每分区独立物理文件**；`reltablespace`/`reltoastrelid` 各自独立；
  - `relpages`/`reltuples`/`relallvisible`（:42-44）——**分区级统计就在这里**（见 §6）；
  - `indisusable`（:50）索引分区可用性（见 §4）；
  - 索引分区也是 pg_partition 行（`indextblid` :48 注释 "index partition's table
    partition's oid"）；local 索引 = 每数据分区一行索引分区行。
- 策略枚举：RANGE/INTERVAL/HASH/LIST（partitionmap.h:71-78 PartitionType）。
- 二级分区：中间层分区自身是 PARTTYPE_SUBPARTITIONED_RELATION，裁剪走
  subpartitionpruning.cpp（CollectSubpartitionPruningResults pruning.cpp:90）。

**判读口径（与 Postgres 习惯的最大差异）**：查分区元数据/分区统计**走 pg_partition，
不走 pg_class**——`SELECT relname, reltuples, boundaries FROM pg_partition WHERE
parentid = <父表oid>`；分区数 ×（1+local 索引数）= pg_partition 行量级，超多分区
直接推高该 catalog 的体积与扫描成本。

## 2. 裁剪（pruning）：plan 期静态裁剪 + PBE 单分区捷径

- **触发点在 size 估算阶段**：`set_plain_rel_size` 对 `rte->ispartrel` 的表调
  `partitionPruningForRestrictInfo(baserestrictinfo)`（allpaths.cpp:953）；语法上
  显式指定分区（TABLE PARTITION(...)）则走 `PartitionPruningForPartitionList`
  （allpaths.cpp:955-957）。
- 机制 = 表达式树逐层判定（pruning.cpp）：AND/OR 的 BoolExpr → 结果交/并（:46-48）；
  OpExpr → 记边界（recordBoundaryFromOpExpr :51）；ScalarArrayOpExpr（IN 列表）
  → :70；NullTest → :52；常量在 plan 期求值（EvalExprValueWhenPruning :60）。
- 产物：PruningResult 两个 bitmap——`bm_rangeSelectedPartitions` +
  `intervalSelectedPartitions`；`rel->partItrs` = 选中分区数（allpaths.cpp:959-962）。
- **PBE 单分区捷径**：`isPbeSinlePartition`（allpaths.cpp:935-938）——参数化语句裁到
  单分区时 partItrs=1，按单分区生成计划。判读：同一 prepared 语句不同参数行数差异
  大是这条捷径的正常表现，不是统计错误。
- **代价估算**：GUC `partition_page_estimation` 开启时按裁剪比例
  `partItrs/nparts` 折算页数（allpaths.cpp:966-980，含二级分区分支）；关闭时按
  全表页估 → 分区表计划系统性偏悲观/偏乐观，判读计划前先看这个 GUC。
- **执行**：PartIterator 节点逐选中分区驱动同一份扫描计划（nodePartIterator.cpp:44
  ExecInitPartIterator / :155 ExecPartIterator）；向量化路径走
  vecexecutor/vectorsonic/vsonicpartition.cpp。

### 裁剪失效/全分区迭代五形态（判读表）

| 形态 | 机制 | 计划/现象特征 |
|---|---|---|
| 分区键上套函数/表达式 | OpExpr 左侧不是裸分区键列，记不出边界 | PartIterator 子扫描份数=全分区数 |
| 分区键与常量类型不一致（隐式转换） | 同上，且随类型族变体翻车（INT 列='串' PSU8 行为变更是同类场景，见 parameters 场景2B） | 索引失效+裁剪失效叠加 |
| OR 跨分区条件 | BoolExpr 并集（pruning.cpp:48） | 并集不小=接近全迭代 |
| 排序/聚合键非分区键 | 全分区归并，裁剪帮不上 | Sort/MergeAppend 归并节点 |
| 参数化且值分布未知 | plan 期求不出值 → 保底全分区 | PBE 首执行慢、行数估算按全表 |

## 3. INSERT 分区路由与 interval 自动扩分区

- 逐元组路由：`heapTupleGetPartitionOid`（partrouting.cpp:73）——
  `partitionRoutingForTuple` 定位；命中返回 partOid+partSeq。
- **未命中分三型**（partrouting.cpp:97-110）：
  - PART_AREA_INTERVAL → **`AddNewIntervalPartition`（:102）自动建分区**——
    **插入路径里做 DDL**：目录写、锁、relcache 失效。判读：interval 表写入吞吐
    周期性抖动、awr 里偶发目录类等待，先想这里；
  - RANGE/LIST/HASH → 报错
    `inserted partition key does not map to any table partition`（:106-110，
    高频报错文案，按此句搜日志定位分区边界缺口）；
  - canIgnore 通道把 ERROR 降为 WARNING（批量路径容忍跳行）。
- interval 键类型有白名单（CheckIntervalPartitionKeyType tablecmds.cpp:667）；
  手工 ADD PARTITION 时原 interval 尾分区转正为 range
  （UpdatePrevIntervalPartToRange tablecmds.cpp:705）——"interval 分区变 range"
  不是异常，是显式加分区的设计行为。

## 4. 分区索引：LOCAL vs GLOBAL（GPI）与默认陷阱

- local：每分区一份索引分区（pg_partition 索引分区行）；global（GPI）：全表单索引，
  **内核自动附加 tableoid 隐列**（indexcmds.cpp:456-457，AddIndexColumnForGpi）。
- **默认是 GLOBAL**：GUC `default_index_kind` 默认 DEFAULT_INDEX_KIND_GLOBAL
  （guc.cpp:4934；注册 guc_storage.cpp:3800）。CREATE INDEX 未写 LOCAL/GLOBAL 时：
  索引列含分区键 → LOCAL，否则 → GLOBAL（indexcmds.cpp:521-540；宏
  DEFAULT_CREATE_LOCAL_INDEX knl_instance.h:1484）。
- **判读陷阱**：客户在分区表上"顺手建索引"（不含分区键的普通二级索引）默认就是
  GPI——带 tableoid 隐列、分区 DDL 维护面更大。看到分区表上的单索引先问
  LOCAL/GLOBAL，再谈维护代价。
- `indisusable`（pg_partition.h:50）：索引分区不可用时**扫描静默跳过该索引**
  （消费点 nodeIndexscan/nodeBitmapIndexscan/nodeIndexonlyscan/execUtils/cluster
  各扫描初始化路径）——"某天突然全表扫"的候选根因之一，查
  pg_partition.indisusable 而不是只看索引定义。

## 5. EXCHANGE PARTITION：锁面与校验

ATExecExchangePartition（tablecmds.cpp:28050）：

- **锁**：普通表以 AccessExclusiveLock 解析打开（:28060）；分区级
  `LockPartition(..., AccessExclusiveLock, PARTITION_LOCK)`（:28137）；interval
  父表额外拿分区序列锁防并发自动扩（:28068-28071，INTERVAL_PARTITION_LOCK_SDEQUENCE）。
  判读：exchange 窗口 = 普通表+目标分区双 AELock，阻塞面=该分区读写+父表元数据；
  与写入侧 interval 自动扩分区互斥。
- **前置检查族**（:28077-28135）：目标必须是普通表（分区表/临时表/UNLOGGED 拒绝，
  :28077-28084）、RLS 不支持（:28090）、列数列型/约束/压缩/存储类型一致、
  **索引匹配**（checkIndexForExchange :28128——双方索引要配套，交换连索引一起换）。
- **行级校验可控**：`cmd->check_validation`（:28140）→ checkValidationForExchange
  （:733，校验换入表行不越分区边界，verbose 可选）。跳过校验=换入越界行后查询
  按边界错分区出数——事后几乎无感，风险自负。

## 6. 统计与 ANALYZE：分层采样，分区统计在 pg_partition

- ANALYZE 父表走 `acquirePartitionedSampleRows`（analyze.cpp:204）汇总采样；
  vacuumRelation / vacuumMainPartition 旗标区分父/分区两级操作（:309-313）。
- 分区级行数/页数落在 pg_partition.reltuples/relpages（§1）——**单分区统计陈旧 →
  该分区计划劣化**，与 diagnostic-map 症状9B（计划劣化）同链。
- 分区表 ANALYZE 显著慢于同量级非分区表——analyze.cpp 源注释自带对照数据
  （DFS 表分区 7197s vs 非分区 4421s，:133-138）：分区数大时 analyze 窗口要预算，
  AWR 里 analyze 时段的长事务/IO 尖刺先对分区数。

## 7. 超多分区的四类代价形态（汇总）

| 代价 | 机制 | 出口 |
|---|---|---|
| fd churn | 每分区独立 relfilenode，local 索引再乘一档 | fd-vfd-budget（max_files_per_process 取区间上限的官方场景） |
| 规划开销 | 裁剪 bitmap/PartIterator 装配随分区数线性 | 计划时间占比高时看分区数，不只是表大小 |
| 锁/打开放大 | PartIterator 逐分区打开+取锁 | 大分区数+高频短查询 → 锁表项/目录压力 |
| DDL 频率 | interval 高速写入 → AddNewIntervalPartition 逐段造分区 | 写入吞吐锯齿（§3） |

## 最佳实践

- 分区表上的索引一律**显式写 LOCAL/GLOBAL**，不留给 default_index_kind 决断。
- 判读分区表计划先看三件事：PartIterator 子扫描份数（裁剪效果）、
  partition_page_estimation（页数口径）、目标分区 pg_partition.reltuples（统计新鲜度）。
- interval 表写入抖动排查顺序：自动扩分区 DDL（§3）→ 分区序列锁（§5）→ 普通锁等待。
- EXCHANGE 生产操作：先校验后交换（保留 check_validation），窗口选写入低谷——
  双 AELock 阻塞面不小。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 用 pg_class 查分区统计 | 分区无 pg_class 行 | pg_partition（parentid 挂父表 oid） |
| 分区表顺手建索引当 local | 默认 default_index_kind=GLOBAL，非分区键索引落 GPI | 显式 LOCAL/GLOBAL |
| "interval 分区变 range 了"当故障 | UpdatePrevIntervalPartToRange 是显式加分区的设计行为 | tablecmds.cpp:705 |
| 突然全表扫只查索引定义 | indisusable 分区被静默跳过 | 查 pg_partition.indisusable |
| 分区数多只怪表大 | fd/规划/锁/DDL 四路代价与分区数线性 | §7 表逐路对账 |
| 参数化语句行数忽大忽小当统计错 | PBE 单分区捷径按参数裁剪 | allpaths.cpp:935-938 |

## 版本注记

- 源码基线：openGauss 6.0.0 正式树（=G100 V3 Build 8 近似基线），路径注意本树
  重排：catalog 层在 `src/common/backend/catalog/pg_partition.cpp`，DDL 在
  `src/gausskernel/optimizer/commands/tablecmds.cpp`，裁剪在
  `src/gausskernel/optimizer/util/`，路由在 `src/gausskernel/cbb/utils/partition/partrouting.cpp`。
- ⚠ 待实测对账（VastBase 侧）：default_index_kind 默认值是否与开源基线一致、
  EXCHANGE 语法变体（WITH/ WITHOUT VALIDATION 措辞）、GPI 维护命令面——
  现场遇到时在 204/env-T 补一手实测再回灌本注记。

## 来源

- openGauss 6.0.0 正式树精读（2026-11-15）：pg_partition.h / partitionmap.h /
  pruning.cpp / allpaths.cpp:930-985 / partrouting.cpp:60-110 / tablecmds.cpp
  （:667-705 interval、:28050-28144 exchange）/ indexcmds.cpp（:456-546 GPI）/
  analyze.cpp（:133-138、:204、:309-313）/ nodePartIterator.cpp。
- 联动：fd-vfd-budget（超多分区 fd 预算）、metrics（statio 分区归属=父表行是
  叶子和）、parameters（场景2B 隐式转换 PSU8 行为变更——裁剪失效同族）、
  diagnostic-map 症状9B（分区统计陈旧 → 计划劣化）。

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
