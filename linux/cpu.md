# Linux CPU 层（负载口径 / 软中断 / 上下文切换 / 调度）

## 概述

> 服务分类：linux（本分支）、performance（AWR OS 段 CPU 列判读）、
> troubleshooting（现场 CPU 高/负载高分诊）。数据库进程跑在 Linux 调度器上，
> us/sy/si/wa/load 的每一个口径都对应一段内核代码——本文件给判读依据。

## 平均负载：本质是 nr_running + nr_uninterruptible

内核定义（kernel/sched/loadavg.c:17-25，深读）：

> "The global load average is an exponentially decaying average of
> **nr_running + nr_uninterruptible**."

- 每 **LOAD_FREQ = 5*HZ+1**（loadavg.h:20）采样一次，1/5/15 分钟三个
  指数衰减平均（avenrun，loadavg.c:321-323；定点数 FSHIFT=11，loadavg.h:18）
- **关键口径**：把 **D 状态（TASK_UNINTERRUPTIBLE，sched.h:109）也计入**——
  load 高 ≠ CPU 忙。IO 卡死、NFS 卡住、驱动 hang 的 D 状态进程全部推高 load
- 已知实现注记：nr_uninterruptible 跨 CPU 记账不精确，**只有总和可靠**
  （loadavg.c:49-53 注释自认）
- loadavg.c 头注释原话："Its a silly number but people think its important"

**判读**：load 高先拆两半——`vmstat 1` 的 `r`（可运行队列≈CPU 争抢）与
`b`（阻塞等待 IO 的进程≈D 状态）；`ps -eo state | grep D` 直接数 D。

## us/sy/si/wa：/proc/stat 四个口径与陷阱

- 口径来源 fs/proc/stat.c；iowait 的记账条件（kernel/sched/core.c:8089 注释）：
  任务即将睡眠等 IO 时 `rq->nr_iowait++`，**CPU 空闲且 nr_iowait>0 时的空闲
  时钟记为 iowait**（stat.c:41-54 读 cpustat[CPUTIME_IOWAIT]）
- **iowait 双刃陷阱（源码直证）**：CPU 忙时不产生 iowait——同一 IO 等待在
  忙 CPU 上被计入 busy（通常是 sy/us）。**iowait=0 完全不代表 IO 没问题**；
  高 iowait 只说明"既有 IO 等待、CPU 又闲"。IO 健康度判据去 `iostat -x`
  看 await（见 `io-storage.md`；util 弃用不判）
- si = softirq 执行时间（下节）；steal/guest/gnice 为虚拟化口径，物理机为 0

## 软中断：2ms/10 次预算与 ksoftirqd

kernel/softirq.c（深读）：

- 中断返回后 `__do_softirq` 在进程上下文批量执行 pending 软中断，预算：
  **MAX_SOFTIRQ_TIME = 2ms**（:543）、**MAX_SOFTIRQ_RESTART = 10 次**（:544）
- 超预算 → `wakeup_softirqd()`（:645）把余量甩给内核线程 **ksoftirqd/%u**
  （:1120 线程名；spawn :1165）——它按普通调度类跑，**与业务进程抢 CPU**

**判读**：
- `top` 里 ksoftirqd 占 CPU 高 = 软中断过载。数据库机器上第一大来源是
  NET_RX/NET_TX（网络收包，判读与七层排查见 `network.md`）；BLOCK 软中断
  少见（现代内核 IO 完成走 irq/线程化）
- AWR OS 段 `si` 高 + ksoftirqd 高 → 查 `/proc/net/softnet_stat`（丢包列）
  而不是先查数据库
- si 的时间也计入 "CPU 使用率"——**DB 侧 SQL 不变但 CPU 涨，先拆 us/sy/si
  再下结论**（联动 `metrics.md` 口径节）

## 上下文切换：自愿/非自愿与 ctxt 计数

- 切换点 `prepare_task_switch` / `finish_task_switch`（core.c:5170/5184）；
  全局计数 `nr_context_switches()`（core.c:5434）→ /proc/stat `ctxt`
  （stat.c:174-179）
- **自愿切换**（等锁/等 IO/等数据，cswch）vs **非自愿切换**（时间片耗尽/
  被抢占，nvcswch）——`pidstat -w/-wt` 分列。总量 `vmstat` cs
- DB 判读：VastBase 线程进程（1 进程多线程）高 nvcswch = CPU 争抢实锤；
  高 cswch 伴 IO 等待属正常等待切换，单看 cs 总量会误判

## sys 间歇性冲高：进程 fork 税与 COW+TLB 广播（案例锚点）

VastBase 内核为线程模型（每会话与后台角色各为线程，**单进程单 mm**，含全部
shared_buffers 页表+私有可写集——线程模型即如此，与连接组织方式无关）。
任何**进程级 fork**（归档命令/备份工具等外部命令）都要复制整套页表并把
父进程 MAP_PRIVATE 页全部改只读，后果三连（144 核/300G 共享区实测）：

- fork 侧：`copy_page_range` 复制 GB 级页表（总量以 VmPTE 实测为准；
  300G 共享区份额 ≈600MB）→ 发起线程卡 **3.2s**（实测）
- 业务侧：worker 写私有页全走 `wp_page_copy` 整页重拷（283MB/4.5s），
  每次 COW `flush_tlb_page→smp_call_function_many` 广播全核 → **sys 冲高主体**
- 子进程侧：execve 拆继承址空间 `exit_mmap` 再 **5.4s**；共享内存
  MAP_SHARED 不参与 COW（fork 税在页表复制，不在拷页）

判读：sys 间歇冲高 + 周期≈归档/作业节奏 + AWR 无异常 → 先查窗口内有无
进程级 fork（perf script --show-task-events 的 `PERF_RECORD_FORK` 且
子 tgid≠主 pid）。根治=2MB HugeTLB 大页（共享区页表 ÷500）+ 降频；
减 shared_buffers 是负优化。判例：
`../cases/case-env-E-sys-spike-archiver-fork-202611.md`（archiver 线程
fork 链，pgarch.cpp:853）；现场分诊四层法与 `-p` 三盲区见
`../troubleshooting/cpu-sys-high.md`

## 调度类与优先级（判读 top 优先级列用）

- 调度类优先序（sched.h:2737-2741）：**stop > deadline > rt > fair > idle**；
  rt 区间 prio<100（MAX_RT_PRIO，prio.h:16），普通任务 prio=120+nice
- nice 范围 **-20..19**（prio.h:5-6）
- fair 类 = **EEVDF**（7.1 内核，fair.c:1136 `pick_eevdf`，虚期限
  `vd = ve + r/w` :1252）——6.6 起替代 CFS；老内核（env-A 的 3.10）仍是 CFS
  时间片模型，但**对判读无实质影响**（负载/切换/软中断口径跨版本一致）
- 内核线程识别（top/pidstat 里别当业务进程分析）：ksoftirqd（软中断）、
  kswapd0（回收，见 `memory.md`）、kworker/*（工作队列）、migration/*、
  oom_reaper、irq/*（线程化中断）——与 `pg_os_threads` 前后台判据
  （client_port）在 OS 侧对账：内核线程 comm 都带方括号/无用户态内存

## PSI 压力接口（4.20+）

kernel/sched/psi.c：把任务延迟聚合成 0..100 的压力值，分 **some**（至少一
个任务受阻）与 **full**（全部任务受阻，CPU 资源无 full）。`/proc/pressure/cpu`
是判"CPU 争抢 vs CPU 独占"最快的一手证据；**3.10 内核（env-A/CentOS7）没有
PSI**，用 vmstat r 与 pidstat nvcswch 替代。

## CPU 硬件身份与虚拟化判定（AWR CPU Info 章 + Steal 列）

**判物理/虚拟——第一手是 CPU Info 表的 `Platform` 字段（显式，别去猜）**：

vb_tool `platform_label()` 三级判定（源码 vb_tool:32575-32602）：① DMI 的
vendor/product 命中 VM_KEYWORDS（kvm/qemu/vmware/xen/hyper-v/virtualbox 等
17 词）→ `Virtual (<vendor+product>)`；② DMI 有值但无关键词 → `Physical`；
③ DMI 缺失时看 /proc/cpuinfo `hypervisor` flag → `Virtual (hypervisor)`；
④ 都没有 → 字段空。env-M 实例 `x86_64 / Physical` = DMI 实锤物理机。

**两条辅助判据（Platform 为空或需交叉验证时）**：

1. **Model 字符串**（/proc/cpuinfo `model name`）——VM 的 vCPU 名一眼可辨：
   QEMU Virtual CPU、Common KVM processor、VMware Virtual Platform、Virtual
   CPU（Amazon EC2）；物理机是真实型号（Intel Xeon Platinum 8374C）
2. **Steal 列**（/proc/stat `steal`，OS Statistics 每分钟一列）——只有虚拟化
   环境才有。记账点 kernel/sched/core.c:825：`steal = paravirt_steal_clock
   (cpu_of(rq))`，即 vCPU 已就绪但被 hypervisor 抢走去跑别家 vCPU 的时间。
   **steal>0 = "不是库的问题"的铁证**——这段时间库的 vCPU 根本没在跑，
   busy 低但 DB 慢时先看它
   **★st 大值的下游症状链（2026-11-03 实证案例：st=70%）**：CPU 被宿主抢走 →
   postmaster 消费不动 accept（TCP+unix socket 通杀变慢）→ listen 队列积压、
   历史百万级 SYN 丢弃 → 客户端建链头几毫秒超时（日志 "failed to send SSL
   negotiation response: Broken pipe"）→ 单一应用 IP 的 CLOSE_WAIT 堆积
   （大量尸体躺在内核 backlog，无线程无会话）→ 客户端重试风暴正反馈放大。
   **看到这套 socket 组合拳，先查 st 再查库内**——指纹互见 `network.md`
   CLOSE_WAIT 节；处置=迁实例/找虚拟化平台，库侧调参无解

**VM 判读语境（Platform=Virtual 后，整套 OS 判读都要打折）**：

- 虚拟网卡背后是宿主 vSwitch，guest 内 missed/drop 高可能是**宿主机邻居
  挤兑**（联动 `network.md` 网卡层虚拟机节）
- CPU 时间片被宿主机按配额调度，`cpu.max`/cgroup 限制下 top 里的 CPU 是
  配额的百分比，不是物理核的
- **steal 高 + guest 内一切正常 ⇒ 宿主超售/邻居抢占**，结论落在"迁实例/
  找运维"，不在库侧调优

**Nice/IRQ/Softirq 三列判读**（/proc/stat，AWR OS 分钟表）：

- **Nice**：低优先级用户态（nice>0 的进程）——持续高=后台任务（备份/ETL/
  杀毒/批处理）挤前台，用 `top` 的 NI 列找谁在 nice 运行
- **IRQ**：硬中断处理 CPU——高=网卡/存储中断风暴（配 `softnet_stat`、
  `ethtool -S` 定位中断来源）
- **Softirq**：软中断 CPU——**ksoftirqd 高 + si 高 = 收包软中断过载**
  （NET_RX 预算耗尽，见下方"软中断"节；netdev_budget 耗尽时 si 抬升且
  ksoftirqd 单线程跑满）

## sys 高·IPI 风暴：TLB shootdown × 多核扇出（2026-11-04 首案）

**指纹（perf top 全内核符号、kernel 占比 ~97%）**：`llist_add_batch` 登顶（>15%）
+ `smp_call_function_many` / `flush_smp_call_function_queue` / `llist_reverse_order` /
`native_write_msr_safe` / `__x2apic_send_IPI_mask` / `call_function_interrupt` /
`flush_tlb_func` / `native_flush_tlb_single`(INVLPG) 成族出现，头部 irqs/sec 五六位数。
同图若有 `copy_pte_range`/`copy_page_rep`/`handle_mm_fault`/memcg charge 族 + `unmap_page_range`
= fork/exit 生产者在场。

**机制链**：短命进程（或 munmap 风暴）每次改地址空间 → TLB 必须在所有可能缓存过该 mm
的 CPU 上失效 → `smp_call_function_many` 给每个目标 CPU 的 `call_single_queue` 投递
（`llist_add_batch`，一次广播=N-1 次入队）→ x2apic 模式逐 CPU 发 IPI（每 IPI 一条
WRMSR 写 ICR——`native_write_msr_safe` 高占比的真相）→ 接收端中断上下文
`flush_smp_call_function_queue`（`llist_reverse_order` 保序出队）执行
`flush_tlb_func`→INVLPG；发送方 `csd_lock_wait` 自旋等确认。**全部五段（发送/入队/
WRMSR/接收/自旋等待）都记 sy，且并行摊到所有核**——核越多放大越狠（N 核单次扇出 N-1）。
发送方等待与接收方执行两端都在烧，所以一台机器上"所有核 sy 一起涨"。

**定位**：`grep -w processes /proc/stat` 每秒差值=fork 速率；`perf record -F 99 -a -g
--call-graph fp -- sleep 30` 抓父帧找谁在 fork（与 cpu-sys-high/tracing-tools 统一
99Hz+fp；dwarf 栈全系统开销大仅定点短窗用；perf top 无 -g 只有
内核符号，看不到生产者）；短命进程 `pidstat -t 1`。DB 本体可先排除：VastBase
单进程多线程共享 mm，不产生此形态——嫌疑=k8s exec 探针/秒级脚本/cron 风暴/每次连接
起一个 psql 的循环。

**处置**：①掐 fork 源（探针 exec→HTTP/TCP、脚本常驻化）治本；②高频 fork 负载
`taskset` 绑少数核，mm_cpumask 收窄→广播面收缩；③`grep -o 'pcid\|invpcid'
/proc/cpuinfo | sort -u` 确认 PCID（降单次成本，不降次数）；④内核升级取 shootdown
批处理优化。

**来源**：2026-11-04 现场机 perf top 火焰图（144 CPU、kernel 97%、228865 irqs/sec，
llist_add_batch 17.89%）；函数族语义=kernel/smp.c 与 arch/x86 TLB flush 路径
（本机无 Linux 树，llist_reverse_order 位于 flush 队列保序、WRMSR=x2apic ICR 写
两处为标注推断，待 7.1-rc4 树恢复后补 file:line）。首案样本截图存
`evals/` 未录——后续同类先落 fixture。


## 判读速查（现象 → 命令 → 机制）

| 现象 | 命令 | 机制指向 |
|---|---|---|
| load 高但 CPU 低 | `vmstat 1` 看 r/b；`ps -eo stat,pid,cmd \| grep ^D` | b 列高 = D 状态（loadavg 口径）；IO/驱动卡 → `io-storage.md` |
| steal>0 / 虚拟机判型 | `cat /proc/cpuinfo \| grep model`；`top` 看 st | 虚拟化判定节：vCPU 名 + paravirt_steal_clock（sched/core.c:825） |
| si/IRQ 高 | `cat /proc/net/softnet_stat`；`mpstat -I` 看中断 | 软中断预算耗尽 / 中断风暴（软中断节） |
| us 高 | `pidstat -u -t`；`perf top -g` | 用户态计算；**含 LWLock 自旋**（自旋不产生等待事件——见 `wait-events.md` 自旋盲区节） |
| sy 高 | `perf top -g`；`pidstat -w` | 系统调用/锁/切换频繁；内核态放大器常是短路径高频调用；`llist_add_batch`/`smp_call_function` 族登顶=IPI 风暴（见 sys 高·IPI 风暴节） |
| si 高 | `top` 看 ksoftirqd；`cat /proc/net/softnet_stat` | 软中断过载，绝大多数是网络 → `network.md` |
| wa 高但说不清 | `iostat -x 1` await | iowait 只是"闲着等"，真判据在块设备列 → `io-storage.md` |
| CPU 莫名涨 | 先拆 us/sy/si 三列 | si 涨=网络/软中断；sy 涨=系统路径；与 `bottleneck-patterns.md` 形态对照 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| iowait=0 判 IO 健康 | CPU 忙时不记 iowait（core.c:8089 语义） | iostat -x await 才是判据 |
| load < 核数 = 安全 | load 含 D 状态（loadavg.c:17-25） | 拆 vmstat r/b；b 高是 IO/驱动问题不是 CPU 问题 |
| ksoftirqd 高当应用异常 | 内核线程（softirq.c:1120），杀不掉也调不了优先级 | 顺着软中断来源查，DB 机器=网络 |
| cs 高就怀疑锁 | 自愿切换含正常 IO 等待 | pidstat -w 拆 cswch/nvcswch，nvcswch 才是抢占 |
| 在 3.10 机器找 /proc/pressure | PSI 是 4.20+ | 老内核用 vmstat r + nvcswch |
| 单看 top CPU% 定罪一个会话 | us 里混自旋、si 混软中断 | perf top -g + pidstat -t 按线程对账 |

## 版本注记

- 基线 7.1-rc4；生产机实测：**env-A = CentOS 7, 3.10.0-1160**（2026-10-25 现查，
  2 核）——该档：CFS（非 EEVDF）、无 PSI、无 sched_ext；softirq 2ms/10 次
  预算、loadavg 口径、状态位定义与 7.1 一致，判读可平移
- sched_ext（BPF 可编程调度类，sched.h:1840/ext.c）6.12+，生产未见，仅备版本注记
- openEuler 4.19/5.10 档介于两者之间：有 PSI（4.20+）、CFS、softirq 同

## 来源

- 内核源码基线 `<linux-7.1-rc4-src>`（7.1-rc4）精读（2026-10-25）：
  kernel/sched/loadavg.c:17-53,321-323（负载定义与采样）、
  include/linux/sched/loadavg.h:18-20、kernel/sched/core.c:5170,5184,5434,
  5452-5492,8089（切换/iowait 记账）、kernel/softirq.c:543-544,581,645,1120,
  1165、include/linux/sched.h:107-114（状态位）、kernel/sched/sched.h:2737-2741
  （调度类序）、include/linux/sched/prio.h:5-16、kernel/sched/fair.c:1121-1252
  （EEVDF）、kernel/sched/psi.c、fs/proc/stat.c:41-54,174-179
- 二次补读（2026-10-27，虚拟化判定节）：kernel/sched/core.c:825
  （paravirt_steal_clock 记 steal）；/proc/cpuinfo model name 字段语义；
  env-M 实例 awrrpt CPU Info 章实测（Intel 8374C 2×36×2=144 逻辑核，物理机）
- 生产内核版本实测：env-A uname -r（2026-10-25）
- 《Linux 性能优化实战》CPU 篇作主题框架参照（口径以源码为准）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：softirq→`network.md`、
iowait→`io-storage.md`、us 自旋→`wait-events.md`、CPU 形态→`bottleneck-patterns.md`。
