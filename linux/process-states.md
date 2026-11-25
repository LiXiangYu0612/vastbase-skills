# 进程/线程状态（R/S/D/T/t/Z/X/P/I）：状态位全枚举、生命周期与取证（内核源码定论）

面向 DB 判读的任务状态一页通：`ps STAT`/`top -H`/vmstat `b` 列里每个字母是什么
位、怎么进、怎么出、怎么取证。行级锚点基线 **v7.1 树**（2026-11-25 精读，
`/mnt/d/Obsidian/linux-master`，VERIFY_ANCHORS 全 PASS）；版本对照 **v3.10 /
v4.19**（2026-11-25 实拉 torvalds plain 存
`~/kernel-src/version-variants/`：array_v3.10.c 722 行、array_v4.19.c 757 行、
schedh_v4.19.h 1893 行、hungtask_v3.10.c 228 行、hungtask_v4.19.c 282 行）。
判读对接：loadavg 口径（cpu.md）、D 深睡实战案（io-storage.md wbt_wait 节）、
诊断树树根（os-triage.md `top -H` 线程形态）。

## 概述：状态机的三条不变式

1. **状态就是一个 32 位掩码** `task_struct->__state`（include/linux/sched.h），
   "running" 是 **0**——不是专门的一位，所以 `state==0` 才叫 TASK_RUNNING，
   其余态用位测试组合（fs/proc/array.c:120-124 注释原文："strange bitmap of
   reasons to sleep"）。位值与字母的权威对照见下表。
2. **睡 = set_current_state(D/S) + schedule() 两步**，中间夹着条件复查；
   **醒 = 唯一入口 try_to_wake_up**，位匹配（`if (state & p->__state)`）才动。
   set_current_state 带内存屏障（sched.h:204 注释），作用是把"条件检查"和
   "真正睡着"焊成原子序列——**先设状态再查条件**是丢唤醒问题的总答案
   （io-storage.md wbt missed-wakeup 缺陷族全是这里出错）。
3. **谁也杀不死纯 D**：信号投递只走 `wake_up_state(t, state |
   TASK_INTERRUPTIBLE)`（signal.c:734）——只能吵醒可中断态或带 WAKEKILL 位的
   复合态；纯 TASK_UNINTERRUPTIBLE 的退出只有两条路：被唤醒路径救活，或
   hung task panic/整机重启。kill -9 杀不掉 D 是结构必然，不是权限问题。

## 状态位全枚举（字母 ↔ 位值 ↔ 语义）

ps 字母的权威来源是内核 `task_state_array`（fs/proc/array.c:127-143），
`/proc/<pid>/status` 的 State 字段就是它（array.c:181）：

| 位值 | 宏（sched.h 锚） | ps 字母 | 语义 | 进入 → 退出 |
|---|---|---|---|---|
| 0x00000000 | TASK_RUNNING（:107） | **R** | 在 CPU 上跑**或**在运行队列排队（:152 `task_is_running` 只认 ==0） | wake → schedule() 让出 |
| 0x00000001 | TASK_INTERRUPTIBLE（:108） | **S** | 可中断睡眠：信号能吵醒 | wait/futex/锁等待 → 唤醒或信号 |
| 0x00000002 | TASK_UNINTERRUPTIBLE（:109） | **D** | 不可中断睡眠：**只有唤醒路径能救**（"disk sleep" 名字误导，等锁/驱动一样是 D） | io_schedule/互斥等 → 对端 wake_up |
| 0x00000004 | __TASK_STOPPED（:110） | **T** | 收到 SIGSTOP/SIGTSTP 停住 | signal.c:904 收 SIGCONT 才走 |
| 0x00000008 | __TASK_TRACED（:111） | **t** | 被 ptrace 停住（gdb/strace 挂着的进程就是它） | PTRACE_CONT/DETACH |
| 0x00000010 | EXIT_DEAD（:113） | **X** | 已死、正在被回收的瞬态（父进程已确认，即将释放） | 瞬态 |
| 0x00000020 | EXIT_ZOMBIE（:114） | **Z** | 已死等父进程 wait 收尸（exit.c:750；do_notify_parent :755 通知父进程） | 父进程 wait/reap → X |
| 0x00000040 | TASK_PARKED（:117） | **P** | kworker 停泊（CPU 热插拔对应的 worker 停用） | unpark |
| TASK_REPORT 之外 | TASK_IDLE = UNINTERRUPTIBLE \| TASK_NOLOAD（:141） | **I** | **空闲内核线程深睡**（空闲 kworker）；NOLOAD 位使它**不计入 loadavg**（core.c:6602-6606 显式排除 NOLOAD） | 有活派给它 → wake |

**复合态（都是位或，sched.h:137-141）**：
- `TASK_KILLABLE = TASK_WAKEKILL | TASK_UNINTERRUPTIBLE`（:137）——现代内核
  NFS/驱动用的"可被致命信号的 D"：平时表现与 D 一样拒一切普通信号，但
  SIGKILL 能通过 WAKEKILL 位把它从睡眠里拽出来走死亡流程（signal.c:728
  注释原文）。ps 里显示 **D**（TASK_REPORT 不含 WAKEKILL 位，:147-150）。
- `TASK_STOPPED = TASK_WAKEKILL | __TASK_STOPPED`、`TASK_NORMAL =
  TASK_INTERRUPTIBLE | TASK_UNINTERRUPTIBLE`（wait 队列的默认匹配集，:144）。

**用户态可见性的两处"内核说谎"**（`__task_state_index`，sched.h:1670-1694）：
- TASK_IDLE 单独映射成字母 **I**（:1677 → TASK_REPORT_IDLE），不和 D 混。
- `TASK_RTLOCK_WAIT` 与 `TASK_FROZEN` **谎报成 D**（:1684-1690，注释原文
  "We're lying here"）——cgroup freezer 冻住的任务在 ps 里也是 D，判读
  cgroup 冻结案（cgroup.md）时别当成 IO 卡。

**版本注记（实拉对账）**：v3.10 的 `task_state_array` 是 **R S D T t Z X x K
W P**——**没有 I**，且 Z/X 顺序反着、K(wakekill)/W(waking) 老字母还在
（array_v3.10.c:135-147）；**I 字母 v4.19 起才有**（array_v4.19.c:143，
TASK_NOLOAD 位 v4.19 sched.h:84）。CentOS 7（3.10）机器上 ps 看不到 I 是
内核太老不是异常。

## 生命周期骨架：睡与醒的四段源码主线

```text
睡：schedule()（core.c:7081 注释区）
 ├─ try_to_block_task（core.c:6582）
 │   ├─ signal_pending_state？→ 有挂起信号且状态可中断 → 置回 RUNNING 不睡（:6586-6592）
 │   │    （S 态睡不成的兜底；纯 D 走不到这——没信号位参与）
 │   ├─ sched_contributes_to_load = UNINTERRUPTIBLE && !NOLOAD && !FROZEN
 │   │    （core.c:6602-6606 ←—— loadavg 计 D 不计 I 的判据在这行）
 │   └─ block_task（core.c:2226）→ __block_task（kernel/sched/sched.h:2978）
 │        → rq->nr_uninterruptible++（sched.h:2981）
 └─ 切下一个任务

醒：唯一入口 try_to_wake_up（core.c:4152）
 ├─ 位不匹配（waker 给的 state ∩ p->__state == 0）→ 直接返回，睡者继续睡
 ├─ ttwu_do_activate → rq->nr_uninterruptible--（core.c:3713）
 │   （nr_uninterruptible 是 rq 上的滚动计数，跨 CPU 迁移只挪不差，但
 │    per-CPU 读数不可靠，**只有全树总和可靠**——loadavg.c:49-53 注释原文；
 │    cpu.md 的"nr_uninterruptible 记账只有总和可靠"锚在这里）
 ├─ p->__state = TASK_WAKING（:4249 区）过渡态 → 入队 → R
 └─ io_schedule 的 in_iowait 标记在 __block_task/ttwu_do_activate 两端
    记账（rq->nr_iowait）→ iowait 投影（cpu.md wa 列）
```

**信号打断的语义**（signal.c:721-736）：`signal_wake_up_state` 用
`wake_up_state(t, state | TASK_INTERRUPTIBLE)` 尝试唤醒——S 态被吵醒、
T/t 态被 CONT/致命信号拽起（signal.c:904）、KILLABLE 态被致命信号拽起；
**纯 D 三种都点不着**，这就是"kill -9 杀不掉 D"的源码出处。

**S 态醒来后的 -ERESTARTSYS 框架**：可中断睡眠者醒来第一件事查
`signal_pending()`，有则放弃本次等待、返回 -ERESTARTSYS/-EINTR 让系统调用
重启或中止——所以 DB 后台线程的 S 堆积是**正常空闲形态**（等客户端、等锁、
等定时器），与 D 的"根本回不来"有本质区别。

## D 深睡：三型与 hung task 三版判据

**三型**（死点靠 wchan/stack 定位，字母 D 本身只说"不可中断"）：
1. **IO 提交等**：io_schedule 睡在块层闸/驱动——wbt_wait 案全套在
   io-storage.md（wbt_wait 深睡节，勿复制）
2. **锁/驱动等**：互斥量、NFS、SCSI timeout 链——wchan 给函数名
3. **唤醒链断裂**：对端该 wake 的没 wake（missed wakeup 缺陷族，wbt 四连修
   那节就是标本）

**hung task 检测器判据的版本分叉（实拉对账，判读老内核必须分档）**：

| 版本 | 判据 | 出处 | 含义 |
|---|---|---|---|
| v3.10（CentOS 7） | `t->state == TASK_UNINTERRUPTIBLE` **等值比较**（hungtask_v3.10.c:168） | 纯 D 才报；**KILLABLE 态饿死不报** | NFS 用 KILLABLE 的任务 hang 了检测器沉默 |
| v4.19（openEuler/Kylin 系） | 同上等值 + 注释原文 "use \\"==\\" to skip the TASK_KILLABLE tasks waiting on NFS"（hungtask_v4.19.c:195-196） | 同上 | 同上，作者自认 |
| v7.1 | 位测 `(state & TASK_UNINTERRUPTIBLE)` 但**显式跳过 WAKEKILL/NOLOAD/FROZEN**（hung_task.c:113-115） | KILLABLE 仍不报，I 态不报，冻结不报；新增 blocker 报告（CONFIG_DETECT_HUNG_TASK_BLOCKER，:136，把互斥持有者一起打出来） | 同样不报 KILLABLE |

共同不变量（三版一致）：`switch_count = nvcsw + nivcsw` 与上次比对、一次都
没切才报（v3.10:87-92 / v4.19:97-104 / v7.1 task_is_hung :103-128）——
**"blocked 120s" = 该任务整窗零切换**，等 CPU 的 R 态不会中招（会切）。
阈值默认 **120s**（lib/Kconfig.debug:1287）；**hung_task_warnings 默认 10
次后永久静默**（hung_task.c:60；:268 原文 "Future hung task reports are
suppressed"；"后来不报了"≠自愈，实证案见 io-storage.md env-W 案）。

## 取证命令族（按信息量排序）

```bash
ps -eo stat,wchan:32,pid,cmd | grep -E '^[RD]'   # 字母+wchan 一步到位
cat /proc/<pid>/status | grep State              # 同字母（task_state_array）
cat /proc/<pid>/wchan                            # 睡点函数名（base.c:421；
                                                 #  get_wchan core.c:2134——
                                                 #  R 态恒空，只对睡着的准）
cat /proc/<pid>/stack                            # 完整内核栈，root 限定——D 定死点的终极手段
cat /proc/<pid>/syscall                          # 正卡在哪个系统调用
echo t > /proc/sysrq-trigger; dmesg | tail       # 现场无 perf 时的全任务栈快照
cat /proc/pressure/io /proc/pressure/memory      # PSI some/full，I 态/D 态不分的
                                                 # 群体卡滞率（4.20+，3.10 无）
```

**ps STAT 修饰符**（`< N L s l +` 等）由 procps-ng 用户态从
/proc 的 Nice/TracerPid/SigBlk/Threads 等字段拼接，内核数组只有主字母；
procps 源码未落本地，对照表按【工程口径】给出，不逐条立锚。

## 判读误区表

| 误区 | 源码定论 | 判读 |
|---|---|---|
| "D = 盘慢" | D 只是"不可中断睡"，等互斥量/驱动/RTLock 全是 D（字母表注释只说 disk sleep 是历史名） | 用 wchan + `/proc/<pid>/stack` 定死点，再分派 io-storage / 锁 / 驱动 |
| "I 状态一大堆 = 有问题" | TASK_IDLE 的 NOLOAD 位显式不进 loadavg（core.c:6602）也不进 hung task（hung_task.c:115） | I 是空闲 kworker 的常态，与 D 区分后再看 |
| "kill -9 能杀掉 D 卡住的任务" | 信号只走 `wake_up_state(t, state \| TASK_INTERRUPTIBLE)`（signal.c:734），纯 D 位不匹配 | 只能等唤醒或 hung task panic/重启；KILLABLE 态除外 |
| "KILLABLE 任务卡死会报 hung task" | 三版判据全部排除 KILLABLE（4.19 注释原文点名 NFS） | NFS 卡死而 dmesg 静默时先怀疑这一条 |
| "hung task 不再报了 = 自愈了" | warnings 默认 10 次后永久静默（hung_task.c:60,268） | 对照 io-storage.md env-W 案：静默期病程持续 2h39m |
| "cgroup 冻结的任务显示 D 是 IO 卡" | TASK_FROZEN 谎报成 D（sched.h:1684-1690） | 对照 cgroup.md 冻结案 |
| "R 多 = CPU 不够" | R = 在跑或在队列；r 列 = nr_running 口径 | 对照 cpu.md loadavg 与 CFS 压测判读 |
| CentOS 7 ps 出现 K/W 字母 | v3.10 老字母表（wakekill/waking），v4.19 起废弃 | 老版本正常形态，不另立新病 |

## 与库侧联判（路由级）

- `top -H -p $(pidof vastbase)` 里 pagewriter/walwriter/wb kworker 长驻 D、
  wchan 落在 wbt_wait/io_schedule → io-storage.md wbt 深睡节
- 大量 backend 常驻 S 是**空闲形态**不是病；S 态突然转 D 且 wchan 进块层
  才升级为 IO 案
- 整机 b 列（vmstat）>0 且持续不消 → 先 `ps grep ^D` 拿名单，再 hung task
  报告对账（io-storage.md 的"b 的名单闭环"配方）
- 基线版本差异：env-A（CentOS 7, 3.10）无 I 字母、hung 判据是等值比较、
  无 PSI；openEuler 4.19 档有 I 无 blocker 报告；v7.1 档全量（cgroup.md
  同款版本分档法）

## 来源

- 内核源码基线 v7.1 树（/mnt/d/Obsidian/linux-master，2026-11-25 精读）：
  include/linux/sched.h:107-152,1670-1703（状态位/字母映射）、
  fs/proc/array.c:120-147,181（字母表与 status 渲染）、
  kernel/sched/core.c:2226,3712-3713,4152,6582-6626,8092（睡醒主线）、
  kernel/sched/sched.h:2978-2983（nr_uninterruptible++）、
  kernel/signal.c:721-736,904（信号唤醒面）、kernel/exit.c:750,755（僵尸）、
  kernel/hung_task.c:53-60,103-128,268、lib/Kconfig.debug:1284-1287（120s）、
  kernel/sched/loadavg.c:19-53,85（D 计 load 口径）、fs/proc/base.c:418-440
  （wchan）
- 版本对照实拉（2026-11-25，torvalds plain，存 ~/kernel-src/version-variants/）：
  v3.10 array.c:135-147（无 I）、v4.19 array.c:130-144 + sched.h:84,89,93、
  v3.10 hung_task.c:168 / v4.19 hung_task.c:195-196（等值判据+KILLABLE 排除注释）
- wbt D 深睡实战案与 missed-wakeup 缺陷族：io-storage.md（2026-11-02 精读，
  本文件不复制）
- ps STAT 修饰符为【工程口径】（procps-ng 源码未落本地，待补证）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：loadavg→`cpu.md`、
D 深睡案→`io-storage.md`、冻结谎报→`cgroup.md`、线程形态→os-triage 树根、
块层死点→`tracing-tools.md`（perf/ftrace 取证链）。
