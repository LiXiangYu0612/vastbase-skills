# B-Tree 索引机制（页分裂 / dedup / 页回收 / HOT / 膨胀判读）

## 概述

> 服务分类：performance（索引膨胀/写放大判读）、sql（索引设计）。表膨胀的
> 机制在 `mvcc-visibility.md`/`autovacuum.md`；本文件补**索引侧**：页怎么
> 长大、怎么缩小、什么时候白写。

## 页分裂（nbtinsert.cpp）

- 插入满页走 `_bt_split`（nbtinsert.cpp:75）；分裂点由 `_bt_findsplitloc`
  （:77）按**空间平衡**选：先算页内可用空间（页大小−头−opaque，最右页无
  高键则右侧多一行预算），goodenough 阈值内提前收——**不是对半切**，
  目标是两侧剩余空间都够用且移动量最小
- **最右页特殊路径**（P_RIGHTMOST :666/:906）：无高键、新最大键总落最右，
  顺扫/递增键负载反复分裂最右页——**单调递增索引列（时间戳/序列）的
  分裂热点在最右端**，同时它也是最好分裂的（无搬运）
- 判读：右侧持续分裂不等于膨胀（递增键正常形态）；判膨胀看密度（下文）

## 重复键治理：dedup（nbtdedup.cpp）

- 重复索引键合并为 **posting list**（btree_dedup_form_posting :25：一条
  索引元组挂多个堆 TID）——重复率高的列（状态位/低基数）靠它压膨胀，
  是**设计层防膨胀的机制**（不用额外代码，键重复即自动触发）

## 页回收：两段式删除（nbtpage.cpp）

- 删页分两步：`_bt_mark_page_halfdead`（nbtpage.cpp:44）标半死 → 再全删
  归还 FSM——**由 vacuum 驱动**（`autovacuum.md` 联动）：删页卡住的第一
  嫌疑还是 OldestXmin/长事务，不是索引自身

## HOT：索引写放大的第一道防线（heap 侧）

- 语义（社区 Postgres 同源）：UPDATE **未改任何索引列**且旧元组所在页放得下
  新版本 → **HOT 更新**：所有索引都不写新条目，只堆内链；否则每个索引
  都要插新条目（写放大 ×索引数）
- 判定函数已锚〔✅ 2026-11-05 源码定位〕：`HeapSatisfiesHOTUpdate`
  （heapam.cpp:6029，static；heap_update :5123 在拿 buffer 排他锁后调用），
  即社区 PG13+ HeapDetermineColumnsInfo 的 openGauss 同源异名实现。
  逐列比对的子程序 `heap_tuple_attr_equals`（:5937）：heap_getattr 取新旧
  值 → datumIsEqual 二进制比较（字典压缩旧元组走 heap_getattr_with_dict
  :5981-5987；NULL 状态先比，系统列只认 OID/tableOID 等）。单次遍历同时
  产出三标志 satisfies_hot/satisfies_key/satisfies_id（hot⊇key⊇id 属性
  位图超集递进，:6048-6052）。**HOT 最终成立双条件**（heap_update
  :5670-5681）：①satisfies_hot=true——hot_attrs（RelationGetIndexAttrBitmap
  INDEX_ATTR_BITMAP_ALL，:5072-5081）覆盖的全部索引列未变；②新元组仍落
  旧页（newbuf==buffer 且非 HEAP_INSERT_SKIP_FSM）；跨页则 PageSetFull
  留 prune/defrag 提示（:5680）。成立后 :5725-5733 打标：旧元组
  HEAP_HOT_UPDATED + 新元组 HEAP_ONLY_TUPLE
- 判读：同表更新频繁 + 索引数多 + 无 HOT 条件被破坏（改了索引列/页满）→
  索引侧 IO 与 WAL 同涨——**先核有没有"索引列被无谓更新"**（如触发器
  改 update_time 而它带索引）

## 索引膨胀判定（没有 pgstatindex 工具时的做法）

- 基线 contrib 无 pgstattuple/pgstatindex〔全树检索无〕——判读用密度法：
  `pg_class.relpages` vs 实际行数估理论页数（页内可用≈8K−头−opaque，
  按平均索引元组宽折算），实际/理论 >2~3 倍才谈膨胀；配
  pg_stat_all_indexes（idx_scan 为 0 的索引另算"无用"问题）
- **膨胀源分诊**：
  | 源 | 特征 | 证据 |
  |---|---|---|
  | 非 HOT 更新 | 索引写速率≈更新速率×索引数 | 更新列核对 |
  | 删页卡 vacuum | dead 索引条目常驻、页稀疏 | OldestXmin/长事务/槽（`mvcc-visibility.md`） |
  | 大量删除后残留 | relpages 不降 | 删页两段式靠 vacuum，重度可 REINDEX |
  | 递增键右侧分裂 | 只有最右热 | 正常形态不是膨胀 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| 索引大=膨胀 | 递增键最右分裂是正常形态；密度法判 |
| 膨胀就 REINDEX | 先分诊源（vacuum 卡住时 REINDEX 完还会涨） |
| 忽略索引列被更新 | 无索引列变更才有 HOT，一行触发器毁所有 |
| dedup 当万能 | 只治重复键，不治删页卡住 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；dedup/posting 为该档具备
  （社区 PG13+ 引入族）；HOT 判定函数已锚〔✅ 2026-11-05〕：
  HeapSatisfiesHOTUpdate（heapam.cpp:6029）

## 来源

- 基线源码核对（2026-10-25）：nbtree/nbtinsert.cpp:75,77（_bt_split/
  _bt_findsplitloc 全函数头部与空间预算）、:666,906（P_RIGHTMOST 路径）、
  nbtree/nbtdedup.cpp:22-59（posting 构建）、nbtree/nbtpage.cpp:44
  （_bt_mark_page_halfdead 两段式）；contrib 无 pgstattuple（负向检索）
- HOT 判定精读（2026-11-05）：heapam.cpp:6029-6120（HeapSatisfiesHOTUpdate
  全函数：三标志超集递进）、:5937-6027（heap_tuple_attr_equals 全函数：
  压缩字典分支/系统列白名单/datumIsEqual）、:5123（heap_update 调用点，
  LockBuffer 之后）、:5072-5081（索引属性位图三取）、:5670-5681（页内
  双条件判定）、:5725-5733（HEAP_HOT_UPDATED/HEAP_ONLY_TUPLE 打标）
- 联动：`mvcc-visibility.md`（OldestXmin）、`autovacuum.md`（删页驱动）、
  `buffer-manager.md`（btree 分裂特例行）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：膨胀源→
mvcc-visibility（xmin）·autovacuum（删页驱动）；分裂特例→buffer-manager
（无页可用时的 btree 分支）。
