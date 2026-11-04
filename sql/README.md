# sql/ — SQL 优化（语句级）

原独立技能 `vastbase-sql-optimizer`（v4.9，2026-04 建）已于 2026-10-26
**整体并入本域并删源**（用户令：不留双层）。文件未拆分重构——按域规矩的
"一主题一文件"拆分挂 ROADMAP。

| 文件 | 内容 |
|---|---|
| `sqlhc-analysis.md` | SQLHC 深度分析流程（P0~P3 优先级、源码级 Cost 复算、报告模板） |
| `optimizer-deep.md` | 深潜总纲（56KB：代价复算/GEQO/LIMIT 缩放/Function×SMP/Oracle 迁移优化系列索引） |
| `sqlhc-integration.md` / `sqlhc-quickref.md` | SQLHC 集成与速查 |
| `optimizer-usage.md` / `quick-reference.md` | 使用指引与速查表 |
| `references/` | 深潜系列（查询转换/统计代价/GEQO/路径生成/SMP/PJE 迁移/隐式转换等） |

机制底座在 `../internals/`（query-pipeline/query-rewrite 位表/query-plan-cache
14 级/stats-to-cost 选择率链/executor-operators 算子与配额）——本分类管
**语句级实操与深潜**，两边互链不复制。
