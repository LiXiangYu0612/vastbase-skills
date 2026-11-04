# 崩溃与 PANIC 现场（crash-panic）

## 概述

> 服务分类：troubleshooting。主进程退出/反复重启/PANIC 刷屏的现场处置。
> 铁律：**先保现场、后拉起**——拉起之后一半的证据就没了。

## 现场保全三件套（拉起前必做）

```bash
# 1. 日志：pg_log 最新文件整份拷走（不要只截尾，第一因在最前面的 PANIC/FATAL）
# 2. core：确认 core 落盘位置与存在
cat /proc/sys/kernel/core_pattern; ulimit -c        # 没配=这次没有 core，记下教训
ls -lh <core目录>                                   # 段错误才有 core；OOM kill 没有
# 3. 控制文件快照
pg_controldata <数据目录> > /tmp/controldata_$(date +%s).txt
# 4. OS 侧：dmesg 尾部（区分 OOM kill：被杀进程无 core，dmesg 有 oom 记录）
dmesg -T | tail -100 > /tmp/dmesg_$(date +%s).txt
```

## 崩溃源分诊（三选一）

| 形态 | 判据 | 走向 |
|---|---|---|
| OS 杀的 | dmesg "Out of memory: Killed process … <库进程>"，无 core | `../linux/memory.md` OOM 全链路 + `resource-exhaustion.md` 第 2 节 |
| 自己 PANIC 的 | pg_log 有 PANIC/FATAL（elog.cpp:2649 起 PANIC 不可恢复路径），段错误则有 core | 第一条 PANIC 往前找根因；core 用 gdb 看栈 |
| 空间/硬件拖死的 | ENOSPC/IO error 文案；或恢复循环 | `startup-failure.md` 第 4/6 步 |

## PANIC 机制一句话版

- PANIC = 不可恢复错误级别：当前线程走退出路径（处理点 elog.cpp:2649/
  :3604/:4365），主进程检测后**整个实例重启自愈**：崩溃恢复=从最近
  checkpoint 重放 WAL（机制与备库回放同源，`../internals/wal-replay.md`）
- **报错家族语义与抛出点**：`../internals/error-fatal-mechanics.md`
  （读 logrpt 判"哪一族"用）

## 反复崩溃（crash loop）

特征：起→恢复中再崩→再起。三种常见：

1. **恢复中撞坏页/坏 WAL**：每次回放到同一位点崩——控制文件里上次
   checkpoint 位点 + 日志崩溃点对齐确认；处置走备份/PITR（别反复硬启
   磨盘）
2. **起因未除**：ENOSPC/权限/OOM 内存配置未改——崩溃只是症状
3. **硬件**：core 栈每次不同 + 磁盘/内存报错——`dmesg` 找 MCE/IO 错误，
   换件前别相信任何数据结论

## 止损与拉起

- 三件套落盘后才拉起；crash loop 时**停自动拉起**，改从备份恢复决策
- 拉起后第一件事：确认恢复完成的日志标志，再放业务流量（半恢复状态
  接流量=二次事故）

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 先重启再排查 | 日志/core/控制文件现场一去不返 | 三件套先行 |
| 只看最后一条日志 | 崩溃第一因在最早的 PANIC/FATAL | 整份日志归档+从第一条报错读 |
| OOM 崩溃当数据库 bug | 无 core + dmesg oom 记录即 OS 层 | 分诊表第一行 |
| core_pattern 没预配 | 事发后想抓 core 已迟 | 部署期标配（运维清单项） |
| crash loop 反复硬启 | 磨盘+扩大损坏面 | 停自动拉起走备份恢复 |

## 版本注记

- PANIC 处理路径锚点为基线源码（elog.cpp 三处 case PANIC）；崩溃自愈
  （主进程重拉+回放）机制跨版本稳定，具体拉起策略（自动/手动）以部署
  方式为准⚓
- core 行为是内核层（kernel.core_pattern），3.10/7.1 一致

## 来源

- 基线 openGauss 6.0.0 源码：common/backend/utils/error/elog.cpp:2649,3604,4365
  （PANIC 分派）、崩溃恢复=checkpoint 起回放（与 wal-replay 同源）
- 内核层：core_pattern/dmesg（`../linux/` 各文件来源节）
- 实测：env-A ENOSPC 压垮案例（2026-10，内部环境）

## 知识网

本文件为操作层：error-fatal（报错家族）、wal-replay（回放机制）、
memory（OOM）、startup-failure（起不来的另一半）、log-analysis（×AWR 联动）。
