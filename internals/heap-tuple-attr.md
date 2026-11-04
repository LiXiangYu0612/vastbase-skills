# Heap 元组取属性语义（heap_slot_getattr 分支链）

> 服务分类：sql（结果正确性判读）、internals（工具/解析器同构参照）；
> 二进制层 Tuple 解析（23B 头/null 位图/对齐）见关联技能
> `vastbase-parse-page-tuple`——本篇讲内核取一列值时的**语义分支**，
> 与其 offset 追踪互为表里

## 概述

`heap_slot_getattr(slot, attnum, isnull)`（heaptuple.cpp:1518，6.0.0 树）
= heap_getattr 的 **TupleTableSlot 版**：执行器逐列取值的主入口
（tableam 层经 TableAmRoutine 槽位 `tslot_getattr` 直接绑定本函数，
tableam.cpp:449，调用侧内联 `tableam_tslot_getattr` tableam.h:586——
master 树的转发函数名 `tableam_tops_getattr` 在 6.0.0 树全树 grep
**0 命中**，结构差异见版本注记）。与裸 heap_getattr 的差异：
① 同一元组多次取列走 deform 缓存（`tts_values[]/tts_isnull[]`，
`tts_nvalid` 之前直接命中），避免 attcacheoff 失效时反复从头扫偏移的
O(N²)；② attnum 超出 tupdesc 列数一律按 NULL（即使物理元组更长）。

## 六分支执行链（按序短路）

| 序 | 条件 | 行为 |
|---|---|---|
| 1 | `attnum <= 0` | 系统列 → `tableam_tops_getsysattr`；虚拟/最小元组取系统列直接 ERROR〔:1533，调用 :1544〕 |
| 2 | `attnum <= tts_nvalid` | **缓存命中**：直接返回 `tts_values[attnum-1]` / `tts_isnull`〔:1550〕 |
| 3 | `attnum > tupdesc->natts` | 返回 NULL〔:1558〕 |
| 3.5 | PGXC 且 `tts_dataRow` | 分布式数据行 → `slot_deform_datarow` 解压展开后取〔:1565〕 |
| 4 | `attisdropped` | **已删列强制 NULL**（计划缓存早于 DROP COLUMN 的场景）〔:1577〕 |
| 5 | `attnum > 元组实际 natts`（短元组） | **旧版本行取列默认值**：`heapGetInitDefVal`（加列前写入的老行，读加列后的新列）〔:1605，默认值取用 :1611〕 |
| 6 | 正常 | null 位图命中→NULL；否则 `slot_deform_tuple`（压缩元组走 `slot_deform_cmprs_tuple`）展开到 attnum，从 `tts_values` 返回〔位图 :1617、deform :1626/:1628〕 |

## 判读要点

- **"老行的新列有值且等于默认值"**不是异常：分支 5 的设计行为——
  `ALTER TABLE ADD COLUMN ... DEFAULT` 不回填老元组，读取时由
  tupdesc 的 attinitdefval 补〔短元组默认值链〕
- **已删列读出 NULL 而非报错**：分支 4——旧计划/旧游标跨 DDL 的容错语义
- **attnum 超 tupdesc 当 NULL vs 超元组 natts 取默认值**是两个不同分支：
  前者（分支 3）只看 tupdesc；后者（分支 5）要求 attnum 在 tupdesc 内但
  元组更短——继承表/ALTER 场景元组与 tupdesc 长度可双向不一致
- **同表列数不一致的来源**：继承子表、ADD/DROP COLUMN 前后的行——
  内核按"tupdesc 为准 + 默认值/NULL 兜底"读取，不做版本升级

## 工具同构（解析器对齐义务）

任何旁路解析数据文件的工具（unload/恢复类）逐列展开时必须复刻同一套
规则，否则老库数据读错：null 位图先查 → offset 对齐展开 → 短元组缺列
取 attinitdefval → dropped 列出 NULL → 压缩元组先解压再 deform。
`vastbase-parse-page-tuple` 技能的对齐/位图规则即此链的分支 6 部分。

## 版本注记

- 2026-11-05 行号对齐至 6.0.0 树（原 master 树）：heap_slot_getattr
  1518-1636（原 1436/1421-1550），六分支与辅助函数行号全部本树 grep
  实取重写，分支序与语义零变化；此前"6.0.0 正式树未逐行对齐"的欠账
  就此闭合
- 唯一结构差异：master 树的 tableam 转发函数 `tableam_tops_getattr`
  （tableam.cpp:719）在 6.0.0 树**不存在**（全树 grep 0 命中，不硬凑），
  改为 TableAmRoutine 槽位 `tslot_getattr : heap_slot_getattr`
  （tableam.cpp:449；字段声明 tableam.h:204、调用侧内联
  `tableam_tslot_getattr` tableam.h:586）；`tableam_tops_getsysattr`
  仍在（tableam.h:607 内联，分支 1 调用点 heaptuple.cpp:1544）
- VastBase G100 分支未验证〔推断：同构〕
- PGXC 数据行分支（3.5）在单机形态不可达，分布式形态相关

## 来源

- openGauss 6.0.0 树（2026-10-30 master 树精读，2026-11-05 行号对齐）：
  heaptuple.cpp:1518-1636（heap_slot_getattr；六分支行号见上表）、
  :311（heapGetInitDefVal）、:1208（slot_deform_tuple）、:1409
  （slot_deform_datarow）、:3016（slot_deform_cmprs_tuple）、
  tableam.cpp:449（tslot_getattr 槽位绑定）、tableam.h:204/:586/:607
  （槽位字段/调用内联/系统列内联）、tuptable.h:244（声明与默认参）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`。
