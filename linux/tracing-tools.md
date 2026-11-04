# Linux 动态追踪工具链（ftrace / perf / eBPF / strace 取舍）

## 概述

> 服务分类：linux（本分支）、troubleshooting（现场取证）、performance
> （瓶颈取证链的最后一环）。静态读数（top/vmstat/iostat/ss）告诉你"哪层
> 慢"，动态追踪告诉你"**哪个函数/哪条路径**慢"——代价是开销，取舍规则
> 见各节。

## 工具分档（开销从低到高）

### ftrace（内核自带，开销最低，生产首选）

- tracefs 挂载在 `/sys/kernel/tracing`（kernel/trace/trace.c:4188 起管理
  权限语义）；事件探针以静态点注册（例：调度切换探针
  kernel/trace/trace_sched_switch.c:70 `register_trace_sched_switch`）
- 常用：调度延迟（events/sched/sched_wakeup+sched_switch）、
  `trace-cmd record/report` 留档
- 判读对接：runqueue 延迟→`cpu.md` 负载口径

### perf（采样型，on-CPU 分析主力）

- 入口 `perf_event_open` 系统调用（kernel/events/core.c:13844）
- `perf top -g`：热点函数实时看；`perf record -g -F 99 -p <pid>` +
  `perf report`/火焰图：留档分析
- **采样型不停进程**——开销≈采样率，99Hz 对库进程可接受；只看 on-CPU
  （谁在烧 CPU 见 `cpu.md`，off-CPU 要用下一档）
- 注意：库进程符号表若剥离，用户态栈只有地址——先确认符号可用性
  （dbg 包在不在）
- **离线符号化（跨机解析 perf.data，实测配方）**：backport perf（如
  CentOS7 上的 5.10）采的数据，老 perf 拒读 incompatible file format——
  换新内核机器解析。vmlinux 取自发行 debuginfo 包（CentOS7 在镜像
  centos-debuginfo 树）：`rpm2cpio kernel-debuginfo-*.rpm | cpio -idm
  '*vmlinux'`；**build-id 必须与 `perf buildid-list` 的 [kernel.kallsyms]
  逐位一致**；KASLR slide=观测地址窗(min/max)减 2MB 对齐候选映射进
  `_stext.._etext`，全帧覆盖 ≥99.9% 才收。GB 级 data 导出用
  ulimit -v + timeout + 落盘三护栏。判例：
  `../cases/case-env-E-sys-spike-archiver-fork-202611.md` 排查链第 2-5 步
  （含三个判读坑：`grep -cw FORK` 匹配不到下划线粘连的事件名、样本行
  $2 是 tid 不带 pid、COMM 合成事件混入统计）

### eBPF / BCC / bpftrace（函数级延迟直方图，off-CPU/IO 延迟神器）

- 内核侧：kernel/bpf/ + kernel/trace/bpf_trace.c；经典一击：
  `runqlat`（调度延迟直方图）、`offcputime`（off-CPU 堆栈=等待分析）、
  `biolatency`（块层延迟直方图，对接 `io-storage.md` iostat 口径）、
  `tcpconnlat`（连接建立延迟，对接 `network.md` 两级队列）
- 开销中低，但**内核版本门槛高**（见版本注记）

### strace / ptrace（最后手段，知道代价再用）

- 机制：ptrace PTRACE_SYSCALL（kernel/ptrace.c:840），**每个系统调用
  进/出两次陷入停止+信号传递**——syscall 密集的库进程被拖慢一个数量级
  以上，生产库**严禁**对主进程长时间 strace
- 替代：`perf trace`（采样型，不停进程）看 syscall 分布；确需 strace 只
  对**单个短命子进程/单个线程短窗口** attach

## 取舍规则（现场决策）

| 问题 | 首选 | 备选 |
|---|---|---|
| CPU 烧在哪（on-CPU） | perf top -g → record 火焰图 | ftrace function_graph 单函数 |
| 等在哪（off-CPU） | bpftrace offcputime | 无法用 eBPF 时：等待事件+OS 读数拼 |
| IO 延迟分布 | biolatency / iostat -x 先行 | perf trace 点块层 tracepoint |
| 调度延迟（CPU 争抢） | runqlat | PSI（4.20+，`cpu.md`） |
| syscall 频度 | perf trace -p <pid> -S | strace 仅短窗 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| strace 挂库主进程 | 每调用两次停+信号，量级拖慢（ptrace.c:840 机制） | perf trace 或短窗 attach |
| perf record 不带 -g | 只有符号没有调用路径 | -g + 确认帧指针/符号可用 |
| 3.10 机器上装 bcc 工具 | eBPF 版本门槛（版本注记） | ftrace+perf 组合覆盖 |
| 追踪输出随手写数据目录 | IO 追踪自身污染 IO 读数 | 输出定向独立目录 |

## 版本注记

- 基线 7.1-rc4：ftrace/perf/eBPF 全量可用
- 生产 **env-T = 3.10**：ftrace（基础事件）与 perf 可用；**eBPF/BCC/
  bpftrace 不可用**（需 4.x+）；`perf trace` 子命令可用性未在该档实测⚓
- PSI（4.20+）见 `cpu.md`；fgraph/fprobe（kernel/trace/）为新内核特性，
  3.10 无

## 来源

- 内核源码基线 `<linux-7.1-rc4-src>`（7.1-rc4）核对（2026-10-25）：
  kernel/trace/trace.c:4188-4190（tracefs）、kernel/trace/trace_sched_switch.c:70
  （静态探针注册样例）、kernel/events/core.c:13844（perf_event_open）、
  kernel/ptrace.c:840,991-993（PTRACE_SYSCALL 进出两次语义）、
  kernel/bpf/、kernel/trace/bpf_trace.c（存在性）
- 生产内核版本实测：env-A uname -r（2026-10-25）
- 《Linux 性能优化实战》动态追踪篇作主题框架参照（口径以源码为准）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：on/off-CPU→`cpu.md`、
块层延迟→`io-storage.md`、连接延迟→`network.md`、等待事件交叉验证→
`wait-events.md`（OS 侧证据 vs 库侧等待事件的互相印证边）。
