# sys 高现场分诊（system time 去向追踪）

## 概述

> 服务分类：troubleshooting。sys=内核态时间（cpustat[CPUTIME_SYSTEM]，口径
> 见 `../linux/cpu.md`），si 单列不混入。本文件给"sys 高"的四层分诊法：
> 静态定形态 → 计数器对齐 → 采样定性 → 定责对账。工具档与离线符号化配方见
> `../linux/tracing-tools.md`；DB 侧 CPU 高对账（形态 P1/P2）见
> `../performance/diagnostic-map.md` §2。

## 第一层：静态分诊（30 秒定形态）

top + vmstat + mpstat + pidstat 四件套，按读数组合路由：

| 形态 | 域 | 去向 |
|---|---|---|
| sys 高 + si 高 + ksoftirqd 可见 | 软中断 | `../linux/network.md`（NET_RX 七层） |
| sys 高 + wa 高 + b 队列涨 | IO 路径内核时间 | 不是 CPU 不足，转 IO 形态 |
| sys 高 + cs 暴涨 | 上下文切换风暴 | pidstat -w 分列 cswch/nvcswch |
| sys 高 + 某进程 sys 列独高 | 该进程内核时间（syscall/缺页） | 第三层 `-p` 定性 |
| sys 高 + kswapd 忙/直接回收 | 内存回收 | 内存压力形态（MemAvailable/水位） |
| sys 高 + **各进程都不高 + 无内核线程热点** | **全局事件（IPI 广播）** | 第二层 /proc/interrupts |

## 第二层：计数器对齐（免费证据，先于 perf）

- `cat /proc/interrupts; sleep 10; cat /proc/interrupts` 差分：**CAL（函数
  调用 IPI）/TLB 行暴涨 = smp_call_function / TLB shootdown 的直接信号**——
  sys 高而进程侧无热点时首先怀疑此域（多核广播：一次 flush × 核数的 IPI）
- `/proc/vmstat`：pgfault/pgmajfault（缺页速率）、thp_*、numa_*（平衡
  活跃度）、compact_*
- `sar -B` minflt/majflt；`/proc/<pid>/status` 的 **VmPTE**（GB 级=页表
  肥大——fork 税读数、大页化收益的直接依据）
- `/proc/softirqs` 差分；D 状态进程点名

## 第三层：采样定性（perf + 栈指纹判据表）

场景 A（责任进程已知）：
`perf record -F 99 -p <pid> --call-graph dwarf sleep 60`
要抓 fork/exec 事件加 `--show-task-events`；导出与符号化见 tracing-tools.md。

**★`-p` 三盲区（判例教训）**：
1. 覆盖面=该进程**全部线程**+窗口内新生线程/子进程（inherit 跟随）——
   线程级元凶可见（判例：archiver 线程 do_fork 栈、其子进程 execve 栈均
   `-p` 抓到）；但**责任方不是它时全盲**——采样干净≠没病，只排除它；
   换 pidstat 点名或场景 B
2. **观察者效应**：`-p` 逐线程事件让每次调度切入重装 PMU，线程越多越重
   （判例 36% 样本是 perf 自身路径）——报告里当背景扣除，勿当热点追
3. cycles 只见 on-CPU：烧 sys 的时刻必然 on-CPU，故够用；"闲时在等什么"
   是 off-CPU 域（offcputime），另案

场景 B（责任方未知）：pidstat -t 全局点名后再 `-p`；或
`perf record -a -F 99 -g --call-graph fp sleep 30` 系统级采样（`-a` 含
全部进程与内核线程；dwarf 全系统太贵用 fp 栈；99Hz 开销可接受）。

**栈指纹判据表**（符号化后按签名聚合，叶子在前）：

| 栈指纹族 | 结论 | 下一步 |
|---|---|---|
| sys_read/sys_write/sys_pselect/sys_futex…（sys_* 入口族） | syscall 风暴 | perf trace -S 看分布，对齐业务/驱动 |
| page_fault→do_page_fault→do_wp_page→wp_page_copy | COW 写故障 | 对齐 FORK 事件；找 MAP_PRIVATE 写者 |
| do_numa_page→migrate_misplaced_page→migrate_pages→try_to_unmap | NUMA 平衡迁移 | 多为底噪；numa_balancing=0 验证 |
| _raw_spin_lock* / osq_lock 多核热点 | 自旋争抢 | 找持锁者；对照 `../internals/locks.md` 自旋盲区 |
| native_flush_tlb_others←flush_tlb_page / smp_call_function_many | TLB/函数广播 IPI | 看 caller 帧定性：wp_page_copy=COW、zap_page_range=munmap/exit、try_to_unmap=migration |
| copy_pte_range←copy_page_range←dup_mm←copy_process←do_fork | fork 页表税 | 谁在 fork（comm+FORK 事件）；大页化 |
| unmap_page_range←exit_mmap←flush_old_exec（sys_execve） | exec/exit 拆址空间税 | 同上 |
| __schedule / pick_next_task 高占比 | 切换风暴 | pidstat -w 分列；唤醒源分析 |
| shrink_page_list / shrink_node | 直接回收 | 转内存压力形态 |
| copy_page_rep 高占比 | 整页拷贝本体 | 上层帧分 COW vs migration |

## 第四层：定责与对账

- comm/tid 聚合（哪个线程）+ FORK/EXEC 事件时间戳 **vs 尖峰窗口对齐**
  （哪个事件；子 tgid≠主 pid 才是进程级 fork）
- **周期性尖峰的节奏 = 触发源频率**（WAL 段归档/定时作业/备份窗口）
- DB 侧对账：归档日志时间戳、作业调度记录。**库内 AWR 无异常 ≠ 无病**——
  内核态开销不进等待事件账本

## VastBase 特有放大器（线程池=单进程单 mm）

- 任何**进程级 fork**（归档命令/备份工具等外部命令）都要复制整套页表并把
  MAP_PRIVATE 页全改只读：页表复制税 + COW 风暴 + 全核 TLB 广播 + 子进程
  exec 拆除税。判例（144 核/300G shared_buffers 实测）：3.2s 页表复制 +
  4.5s 内 283MB COW 重拷 + 每页一次 144 核广播 + 5.4s exec 拆除
- 归档命令执行链：`gs_popen_security` = fork+exec shell+pclose 干等
  （src/gausskernel/process/postmaster/pgarch.cpp:853）
- shared_buffers 越大页表越大（fork 税线性放大）；**HugeTLB 大页=根治共享区
  份额**（enable_huge_pages，src/common/backend/port/sysv_shmem.cpp；
  2MB 大页使 300G 共享区页表 614MB→1.2MB）——★私有映射的页表份额大页
  治不了，收益占比以 VmPTE 实测 + pmap 拆解后再判；减 shared_buffers=
  负优化（只缩页表税、不治 COW/广播、伤命中率）

## 判例与关联

- `../cases/case-env-E-sys-spike-archiver-fork-202611.md`（完整判例：采集
  配方、符号化、判读三坑、修复方案）
- `../linux/cpu.md`（sys 口径与"sys 间歇性冲高"机制节）
- `../linux/tracing-tools.md`（perf 档与离线符号化配方）
- `../performance/diagnostic-map.md` §2（DB 侧 CPU 高对账）
