# 查询处理管线骨架（parse → rewrite → plan → exec）

## 概述

> 服务分类：sql（管线总览）；各环节深读件：query-rewrite.md（重写开关）、
> query-plan-cache.md（PBE 计划选择）、stats-to-cost.md（选择率）

一条 SQL 在内核查询侧的四段旅程与各自的判读入口：

```text
词法/语法（flex/bison → 解析树）
   → 语义分析（analyze.cpp：解析树→查询树，视图展开、类型解析）
   → 重写（prepjointree/subselect 等按 rewrite_rule 位门控）
   → 计划（standard_planner：make_one_rel 路径枚举 → DP/GEQO 连接顺序 → 代价比较）
       ↘ 计划缓存（ChooseCustomPlan 14 级瀑布）
   → 执行（opfusion 点查快速路径 / 常规算子 / 向量化 vecexecutor / SMP stream）
```

## 各段锚点与判读

| 段 | 锚点 | 已核细节 | 判读入口 |
|---|---|---|---|
| parse/analyze | common/backend/parser（scan.l + gram.y；语义侧 analyze.cpp 别混同名文件） | 词法/语法深读（2026-11-04 入库，见下节） | 语法错误 42601 一族 |
| rewrite | rewrite_rule 位门控：**LAZY_AGG→planner.cpp、MAGIC_SET→subselect.cpp、PARTIAL_PUSH→pgxcplan_single.cpp**（已核消费文件）；SUBLINK_PULLUP_ENHANCED / REMOVE_REDUNDANT_* 消费点未定位〔⚓线索：subselect.cpp/prepjointree.cpp〕 | 位表 ✅（query-rewrite.md） | 计划突变第一排查位 |
| plan | 入口锚点✅：planner():380 → standard_planner():530（planner.cpp）→ make_one_rel（allpaths.cpp:186，含 non_keypreserved 扩展参数）；GEQO 目录实存（optimizer/geqo/geqo_copy.cpp…）；参数化路径=sql_beta_feature PARAM_PATH_* 位 + **执行侧本体=NESTLOOP_PARAM**（见 `executor-operators.md`） | 入口锚点级✅ | Plan Time 高：重写组合爆炸/统计缺失/PBE 摇摆 |
| exec | opfusion 快速路径（error-fatal §3 重复键 WARNING 变体实证）；向量化=vecexecutor 目录；**SMP stream=vecexecutor/vecnode/vecstream.cpp（向量化路径）**+dop 配额分摊；算子执行模型深读件=`executor-operators.md`（NL/MJ/Agg/Window/Material+配额接线） | 算子级✅ | STATUS 执行阶段标记（core-events §6） |

## 词法/语法解析深读（XS-DB-64）

**入口**：`raw_parser`（parser.c）调 Flex/Bison 生成的 `base_yyparse`——词法
scan.l 识别记号（token=类型+semantic value，经 yylval 回传；yylloc 位置、
yyextra 传 core_yy_extra_type），语法 gram.y 按文法归约构建解析树。
关键约定：`%name-prefix="base_yy"`（生成符号 base_yy*）、`%pure-parser`
+parse/lex-param（可重入、传 yyscanner）、内存接管（palloc/repalloc/pfree 替换
默认 malloc 族）、`%left/%right` 定优先级与结合性。

**移进/规约（LALR 行为）**：读到不能结束规则的记号先压栈（shift）；栈顶已
构成某规则右部则弹出压入左部（reduce）并执行动作码（`$$`/`$1`）。
**冲突两族**：shift/reduce（同一输入两种分析，一个收尾一个不收尾）、
reduce/reduce（同一记号可结束两条规则）。解法：改文法（引入新非终结符/
调产生式）或声明优先级与结合性；**排查配方：`bison a.y -d -v --debug`
生成 .output 文件看冲突状态与执行流程**。

判读价值：语法报错定位到 gram.y 规则、扩展语法（新关键字）引发的冲突分析、
42601 家族区分词法（scan.l 模式）与语法（gram.y 规则）侧。
路径注记：内部文档写 `src\backend\parser`（PG 路径），本基线实际在
`src/common/backend/parser/`（差异记录）。

## 常见错误

| 错误 | 纠正 |
|---|---|
| 计划差只查统计 | 管线分段：重写位/计划缓存/执行路径三处都可能 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）；本文件为骨架级（各段深读件见上表），
  位号与函数名以实读为准，2026-10-25 建档

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
