# Opfusion 点查快速路径（参数族与观测）

## 概述

> 服务分类：performance（点查延迟/OLTP 路径判读）、sql（绑定变量优化）；
> 联动 `query-plan-cache.md`（PBE 计划缓存；opfusion 是其执行侧加速层）

opfusion = operator fusion：对**简单点操作**（索引点查/点插/点改/点删）把"计划→执行"
融合成一条快速路径，绕过常规执行器的算子机。OLTP 高频点查的主要加速器。
**它不做计划选择**——custom/generic 由 `query-plan-cache.md` 判定瀑布决定，
opfusion 拿到哪个计划就融合哪个；它跳过的是执行器。

## 生效路径

| 通路 | 入口 | 门条件 |
|---|---|---|
| PBE Bind | exec_bind_message → getFusionType | enable_opfusion（postgres.cpp:2600 族） |
| 简单查询 | exec_simple_query → runOpfusionCheck | 单语句 + 查询串长 < SECUREC_MEM_MAX_LEN |
| 资格判定 | opfusion.cpp:235 `OpFusion::getFusionType` | 非 initdb；单条 PlannedStmt；无 subplan/initPlan；非 utility/DDL；非版本表扫描；再按 SELECT/INSERT/UPDATE/DELETE 分型（各 getXXXFusionType，要求索引点访问形态） |

集中式（G100 单机）IS_PGXC_DATANODE 恒 true → 两条通路对等开放。

## 参数族（定义 guc_sql.cpp）

| 参数 | 级别 | 默认(源码) | 作用 |
|---|---|---|---|
| enable_opfusion | USERSET | **true** | 总开关。★env-T 配置文件**显式 off**（2026-11-03 实测 source=configuration file） |
| enable_opfusion_reuse | USERSET | false | 复用会话内 fusion 对象：仅 INSERT/DELETE/UPDATE 且同表同型（opfusion.cpp:656-680），cacheplan 场景不支持；命中免重建 |
| enable_partition_opfusion | USERSET | false | 分区表点查融合门：RELATION_IS_PARTITIONED 且关=NOBYPASS（opfusion_util.cpp:1015）。另有 sql_beta_feature 位 PARTITION_OPFUSION（guc_sql.cpp:324），且受 NUMA 节点数/enable_global_syscache 约束（guc.h:478 宏）——beta 位与 GUC 是两个开关 |
| enable_beta_opfusion | USERSET（NODE_SINGLENODE） | false | beta 特性包 |
| opfusion_debug_mode | USERSET | off（可 log） | 观测开关（下节） |

## 观测配方（2026-11-03 env-T 实测）

1. **EXPLAIN 看 `[Bypass]`**：`set enable_opfusion=on; explain <点查>;`
   计划上方打 `[Bypass]` = 运行时可融合（explain.cpp:1144，需 TEXT 格式且
   enable_hypo_index=off）。实测 PK 等值点查与 EXPLAIN EXECUTE（gplan）都打
2. **看不能融合的原因**：`set opfusion_debug_mode=log;` 后 EXPLAIN 打
   `[No Bypass]reason: <字符串>`；运行期未融合按 DEBUG4+MOD_OPFUSION 记
   `"<原因>: <查询串>"`（opfusion_util.cpp:321）——该通道要 log_min_messages=debug4
   且 logging_module 含 MOD_OPFUSION（VB 构建该模块是否注册未核，优先用 EXPLAIN 通道）

## 判读应用

| 现象 | 查向 |
|---|---|
| OLTP 点查延迟高、执行器开销占比大 | 确认 enable_opfusion 未被配置文件关掉；EXPLAIN 复核 `[Bypass]` |
| 点查计划好但仍不快 | opfusion 只覆盖"简单点操作"：带 subplan/initPlan、版本表扫描等形态 NOBYPASS，回落常规执行器是设计行为不是 bug |
| 分区表点查不融合 | enable_partition_opfusion（GUC+beta 位）未开 |
| GPC 复用率低 | enable_opfusion/enable_partition_opfusion 进 GPC env signature（bit17/18，globalplancache_util.cpp:126-127），会话间取值不同=环境分裂不复用 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| "opfusion 管 custom/generic 选择" | 计划选择是 plan_cache_mode/判定瀑布的事；opfusion 跳过的是执行器机 |
| EXPLAIN 有 [Bypass] 就万事大吉 | 运行期带真实参数会再判一次型；观测用 EXPLAIN + opfusion_debug_mode=log 双通道 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（=G100 V3 Build 8 档）；行号会漂，函数名为锚
- 源码精读+env-T（G100 V3.0 Build 8）实测（2026-11-03）：参数 pg_settings 全景
  （enable_opfusion 在 env-T=配置文件 off）；PK 点查 EXPLAIN `[Bypass]` 实见
- 联动：`query-plan-cache.md`（PBE 参数全景与 C/G 判定瀑布）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`。
