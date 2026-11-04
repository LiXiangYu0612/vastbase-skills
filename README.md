# vastbase 技能域

VastBase（G100）数据库运维诊断知识库。一个技能域 = 一个路由 `SKILL.md` +
按主题拆分的知识文件；AWR 报告自动分析是 `performance/` 分支，等待事件、
指标口径、参数对照等知识组件同时服务域内其他问题。

## 组织原则

- **知识 / 工具 / 运行时数据三分离**：`performance/*.md` 是知识；`scripts/` 与
  `report-template.html` 是工具；`workspace/` 是运行时数据（输入与源码根）；
  `evals/` 是测试资产。四者不混放
- **一个文件一个主题**：文件名小写连字符；主题文件可独立引用，不依赖阅读顺序
- **来源纪律**：每个文件以「来源」收尾；结论性内容可追溯（源码精读 / 内核源码基线 /
  实测样本）；推断性内容显式标注「推断」
- **版本基线**：内核结论统一基于 openGauss 6.0.0 树（G100 V3 档），差异在「版本注记」说明
- **有内容才进路由**：分类目录可以先建（README 标注计划主题），但知识内容落地之前
  不进路由表、不进 description——路由只承诺真实存在的内容

## 分类

- `performance/` — 性能：AWR 诊断全流程及其知识组件（**已有内容**）
- `troubleshooting/` — 故障诊断：现场救火（起不来/连接拒绝/崩溃/锁死现场）
- `ha/` — 高可用：主备架构与切换、集群管理、脑裂防护
- `backup-recovery/` — 备份恢复：vb_probackup、归档、PITR、恢复演练
- `sql/` — SQL 优化：语句级（SQLHC 分析流程、代价复算、查询转换深潜、
  隐式转换 30 场景、Oracle 迁移优化——原独立技能已并入，2026-10-26）
- `admin/` — 日常运维：用户权限、升级、参数管理、巡检
- `migrations/` — Oracle → VastBase 迁移适配
- `tools/` — 自研工具使用：vb_tool / vbdul（发版流程类技能不进域，保持独立）

## 如何扩充

新增主题：在对应分类目录加 `主题名.md`，按 `SKILL_AUTHORING_GUIDE.md` 的骨架写，
然后把 `SKILL.md` 路由表加一行、description 补一句触发语境。新分类：先建目录 +
README 计划清单，内容落地后再进路由。

## 维护纪律

- 修正知识错误时必须同时核对来源（源码或实测），不允许只改结论不改依据
- extract.py 行为变更时，同步更新 `awr-report-analysis.md` 的产物说明与 evals
- 本目录不存放设计历史与过程记录，那些在个人记忆库里

## 安装

```bash
# 方式一：Claude Code 插件市场
/plugin marketplace add LiXiangYu0612/vastbase
/plugin install vastbase@vastbase-skills

# 方式二：直接克隆为技能
git clone https://github.com/LiXiangYu0612/vastbase.git ~/.claude/skills/vastbase
```

安装后对 Claude 说“分析这份 AWR 报告”“实例起不来帮我排查”即可触发。
本域知识结论全部可溯源（file:line + 校验脚本），维护规约见
`SKILL_AUTHORING_GUIDE.md` 与 `PUBLISHING.md`。
