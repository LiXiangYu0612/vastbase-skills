# SQL PATCH：计划固化与语句拦截（sql-patch.md）

## 概述

> 服务分类：sql（语句级）+ troubleshooting（应急拦截）。回答两个现场高频问题：
> ①"计划劣化怎么锁计划"（diagnostic-map 症状9B 的处置出口，此前只有一句
> "锁计划"）——官方机制就是 SQL PATCH：**按 unique_sql_id 锚定注入 hint**，
> 不改应用代码；②"某条问题 SQL 能不能先杀掉再说"——ABORT 补丁=命中即报错
> 拦截，应急止血用。
> 锚定键 = unique_sql_id，与 statement_history/pg_stat_instr 系视图、SQLHC
> 同键（`sqlhc-analysis.md` 的 unique_query_id 口径）——拿 id 的通道现成。

## 机制（源码基线 6.0.0）

- **目录**：`gs_sql_patch`（OID 9050，gs_sql_patch.h:33-47）——行字段：
  `patch_name` / `unique_sql_id`（锚定键）/ `owner` / `enable` / `abort`
  （true=拦截型补丁）/ `hint_string` / `hint_node`；`patched_query*` 四列
  为保留属性（查询改写型补丁的预留位，现不用）。
- **应用点**：语句解析后按 unique_sql_id 取已启用补丁，hint 注入查询——
  `AddPatchToDml`/`AddPatchToExplainQuery`（sqlpatch.cpp:54-55），取补丁
  `GetEnabledPatchBySqlId`（:50）；不支持的 hint 会被静默剔除
  （`RemovePatchUnsupportHint` :57）——**补丁写了没效果先想"hint 不被
  支持"**。
- **ABORT 补丁**：命中即拦（`CheckAbortByPatch` :56）；创建走
  `PreventCommandIfReadOnly`（:175）——**只读备库上建不了 abort 补丁**。
- 权限：`PrivCheck`（:52）——补丁属主/管理员操作。

## VastBase 实测接口（204 生产实例，2026-11-15 实测）

catalog `gs_sql_patch` 与 `dbe_sql_util` schema 全量开放，六函数：

| 函数（dbe_sql_util.） | 签名 | 用途 |
|---|---|---|
| `create_hint_sql_patch` | (name, bigint unique_sql_id, text hint, text 描述, boolean enable) | 建计划固化补丁 |
| `create_abort_sql_patch` | (name, bigint, text 描述, boolean enable) | 建拦截补丁（备库只读拒建） |
| `enable_sql_patch` / `disable_sql_patch` | (name) | 启停（灰度开关，改错可秒回） |
| `drop_sql_patch` | (name) | 删 |
| `show_sql_patch` | (patch_name) → (unique_sql_id, enable, abort, hint_str)；**缺名直接报错 "No such SQL patch"，不能当清单用** | 查单个补丁 |

★实测修正（2026-11-15 晚，204 tpcc_ts 全生命周期）：
- **hint 必须是裸文本**（`set(query_dop 1)`、`indexscan(t1)`），传 `/*+ set(...) */` 带皮形式 → `ProcessHintString` 剥不出任何 hint → **静默返回 false，无日志无 WARNING**（sqlpatch.cpp:96-102 + RemovePatchUnsupportHint 后 nall_hints==0）——"建了没反应"先查皮。
- **gs_sql_patch 是每库一份**（catalog 也随库），操作必须连对库；`dbe_perf.statement` 同样每库。
- 同 unique_sql_id 只能有一个补丁：重复建报 `Cannot accept sql patches with duplicate unique sql id`。
- SQL Patch 支持的 hint 族（源码 RemovePatchUnsupportHint）：join/leading/row/scan/predpush(+same_level)/set/cache_plan；**剔除** blockname/no_expand/no_gpc/rewrite。
- **outline hint → sqlpatch（2026-11-16 实测）**：`EXPLAIN (OUTLINE) <语句>` 输出 BEGIN/END_OUTLINE_DATA 之间的行拼一行即 hint；`INDEX(@"SEL$1" ...)` 的双引号是 Oracle 查询块语法**合法**（"双引号不行"仅限 SET() 的值）；Oracle 标记（DB_VERSION 等）引擎自剔。**手动 cp 路径**：整份 explain 输出存文件 → `vb_tool sqlpatch create -h @文件`，标记行间内容自动提取，不经 shell 无转义问题。注意 EXPLAIN 过的语句不进 tracking，取 unique_sql_id 须真执行一次。
- **引号三层规矩（2026-11-15 实测）**：set() 值带特殊字符必须**单引号** `'magicset,predpushnormal'`；双引号 `"` 语法不收（hint_gram.y 的 guc_value 只认 IDENT/SCONST/on/off/true/false）；手写 SQL 调函数时才写 `''` 双写（SQL 字面量转义），**预双写再传给工具/参数化接口必失败**（双重转义，值里真出现两个引号→词法炸→静默 false）。工具层（vb_tool sqlpatch -h）永远给真实形态单引号。
- 3.0.9（204 vb309@6432）比 2.2.x 多三个函数：`import_sql_outline(text,text,name,name,bool×3)`、`import_sql_bind(name,bigint,bigint[,text],bool)`、`drop_sql_bind(name)`——outline 导入是 3.0.9 扩展，openGauss 6.0.0 基线树无此实现（2026-11-15 用户拍板暂不做）。
- catalog 实列：patch_name, unique_sql_id, owner, enable, status, abort, hint_string, hint_node, original_query, original_query_tree, patched_query, patched_query_tree, description——清单直接查 `gs_sql_patch`，别用 show_sql_patch。

命名口径：开源基线内部函数名 `create_sql_patch_by_id_hint`（sqlpatch.cpp:60），
VastBase 暴露名是 `create_hint_sql_patch`——查源码时按内部名，调用按暴露名。

## 使用配方（全周期）

1. **拿 unique_sql_id**：statement_history / pg_stat_activity 的
   query_id（与补丁键同源）；参数化语句的 id 是归一化文本的指纹——同一
   prepared 语句不同参数共享一个 id，补丁对其全部生效。
2. `SELECT * FROM dbe_sql_util.show_sql_patch();` 先看现状（避免同名覆盖）。
3. 建：`SELECT dbe_sql_util.create_hint_sql_patch('p1', <id>, '/*+ index(t idx_x) */', 'plan fix 2026-11', true);`
   hint 语法同域内 hint 体系（SMP/hint 篇）。
4. 验证：EXPLAIN 同语句看计划是否翻转；补丁不生效三查——enable 位、
   unique_sql_id 是否对（归一化指纹）、hint 是否被
   RemovePatchUnsupportHint 剔除。
5. 应急拦截：`create_abort_sql_patch('kill1', <id>, 'stop the storm', true)`
   ——命中语句直接报错，风暴止血；风暴停后 disable/drop。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 补丁建了计划不变 | enable=false / id 不匹配 / hint 不支持被静默剔除 | 三查（配方第 4 步） |
| abort 补丁当永久方案 | 拦截=报错，业务仍失败 | 止血用，根因走 9B 链 |
| 备库建 abort 补丁失败当 bug | PreventCommandIfReadOnly 设计行为 | sqlpatch.cpp:175 |
| 期望补丁改写 SQL 文本 | patched_query* 是保留属性，现版本只有 hint 注入 | 改写走应用/视图 |
| 与自适应计划混为一谈 | 补丁是外部钉死，gplanmgr 是内部学习 | adaptive-plan-mgr |

## 版本注记

- 源码基线 openGauss 6.0.0 正式树（src/gausskernel/optimizer/sqlpatch/
  sqlpatch.cpp + src/include/catalog/gs_sql_patch.h）；VastBase 接口实测于
  204 生产实例（2026-11-15，catalog/六函数/schema 三点全过）。
- 与 pin 计划类参数（plan_cache/adaptive 族）的关系：SQL PATCH 在解析层
  注入 hint，优先级高于优化器自选——两套机制可叠加，判读计划突变时先
  `show_sql_patch()` 排除人为补丁。

## 来源

- openGauss 6.0.0 正式树精读（2026-11-15）：gs_sql_patch.h:33-47（目录）、
  sqlpatch.cpp:49-57,60,167-175,252,262-383（UDF 族与应用点）。
- 204 实测（2026-11-15）：gs_sql_patch catalog 存在性、dbe_sql_util 六函数
  签名、schema 存在性。
- 联动：diagnostic-map 症状9B（计划劣化处置出口落地）、sqlhc-analysis
  （unique_query_id 同键）、adaptive-plan-mgr（对照：学习 vs 钉死）、
  query-plan-cache（PBE 归一化文本与 id 指纹）。

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
