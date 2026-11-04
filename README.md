# vastbase-skills — VastBase（G100）运维诊断知识库

VastBase（G100）数据库的运维诊断知识库，打包成 Claude Code 技能。DBA 排查
VastBase 性能问题或故障时让 Claude 带着这套知识干活：判读 AWR 报告、解释
等待事件、定位 CPU/IO/锁/内存瓶颈、处理实例故障、优化 SQL。结论都标注来源
（源码 file:line 或实测数据），不凭印象说话。

适用内核：VastBase G100（openGauss 系）。内核机制结论统一基于 openGauss
6.0.0 源码（G100 V3 档），版本差异在各文件的「版本注记」中说明。

## 能做什么

- 分析 AWR 报告（vb_tool 产出的 awrrpt HTML），给出瓶颈定位与处置建议
- 性能问题：CPU 高（含 sys 高专项分诊）、IO 慢、锁等待、内存压力、表膨胀、
  主备延迟
- 故障现场：实例起不来、宕库 PANIC、连接拒绝、会话挂起、fd/内存/信号量耗尽
- 备份恢复：vb_probackup、归档、PITR 机制
- SQL 优化：语句级分析、执行计划判读、代价复算、Oracle 迁移改写
- 解读堆栈：日志里的 BACKTRACELOG、perf 采样调用栈

## 目录

| 目录 | 内容 |
|------|------|
| performance/ | AWR 分析流程、瓶颈形态判定、指标口径、等待事件 |
| troubleshooting/ | 现场故障分诊（含 sys 高四层分诊法） |
| internals/ | 内核机制：等待事件溯源、MVCC、锁、WAL、缓冲区管理等 |
| linux/ | OS 层判读：CPU/内存/IO/网络、动态追踪工具链 |
| ha/ | 主备架构、复制延迟分解 |
| backup-recovery/ | 归档、PITR |
| sql/ | SQL 优化与 Oracle 迁移 |
| cases/ | 真实案例：症状→排查→根因→预防 |

## 安装

```bash
# Claude Code 插件方式
/plugin marketplace add LiXiangYu0612/vastbase-skills
/plugin install vastbase@vastbase-skills

# 或直接克隆为技能
git clone https://github.com/LiXiangYu0612/vastbase-skills.git ~/.claude/skills/vastbase
```

装好后直接说事，例如：

```
分析这份 AWR 报告，看看有什么问题
实例起不来，帮我排查
这台机器 sys 间歇性冲高，怎么定位
```

## 内容可信度

- 每个知识文件结尾有「来源」：源码 file:line、内核源码基线或实测样本；
  推断性内容显式标注「推断」
- 自带校验脚本：`scripts/check_graph.py`（知识链接完整性）、
  `scripts/verify_anchors.py`（源码锚点核对）
