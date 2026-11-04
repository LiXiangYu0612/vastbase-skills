# VastBase 的 SysV 信号量使用（kernel.sem 依赖分析）

## 结论（源码 + env-T 活体双重验证，2026-11-01）

1. **Linux 构建选用 SysV 信号量**：`cmake/src/config-in/pg_config.h.in` 硬定义 `USE_SYSV_SEMAPHORES 1`（`USE_UNNAMED_POSIX_SEMAPHORES` 未定义）→ PGSemaphore = `src/common/backend/port/sysv_sema.cpp` 的 semget/semop/semtimedop。posix_sema.cpp（sem_init pshared=1，futex 型）在树中但未被主构建选中；extreme_rto/parallel_recovery 另有自带的 posix_semaphore.cpp（恢复通道专用）。
2. **env-T 实测**：vastbase 实例持有 75 个信号量数组 × 每个 17 sem（`ipcs -s`），单进程 37 线程。数组数量按 MaxBackends 等参数在启动时定死。
3. **用途**：LWLock/锁管理器的**阻塞睡眠**（`lwlock.cpp: PGSemaphoreLock(&t_thrd.proc->sem)`，每个 PROC 槽一个 sem）。快速路径全是原子操作+自旋（s_lock.h SPIN_DELAY）。
4. **热睡眠路径不走信号量**：walwriter/pagewriter 等后台线程的 latch 等待是 poll/epoll 型，与信号量无关。
5. **kernel.sem 是限额不是成本**：单次 semtimedop 的开销取决于集合大小（VB 固定 17/个）、单次 ops 数（1）、该 sem 的等待队列——与 SEMMNS/SEMMNI 配多大无关。配大无害；**配小才出事**（启动 FATAL / 高并发耗尽）。
6. **与 Oracle LMS 案例的差异**：Oracle 线程模式下按 SEMMSL 生成大集合、LMS 跨实例消息风暴高频 sem 唤醒，集合级 spinlock 竞争放大为 sys%；VB 单机无 RAC、集合小而多、热路径不经过 sem——该形态基本不会复现。
7. **诊断处方**（sys% 高时排除本嫌疑）：`perf top -k` 看 `semtimedop/do_semtimedop/ipc:*` 占比；`ipcs -s | grep vastbase | wc -l` 对比启动值、`ipcs -sl` 看 ot/cnt 未决；`strace -c -f -e trace=semtimedop,semop` 采样频率；修复方向在锁争用（pg_stat_get_backend_lwlocks 定位哪个锁），不在 kernel.sem。

## 版本注记
- 结论基于 openGauss 6.0.0 树与 env-T 活体（2026-11-01）；信号量数组数量随
  MaxBackends 等参数在启动期定值，跨大版本需复核

## 来源
- 源码：openGauss-server-v6.0.0（/mnt/d）pg_config.h.in:840、sysv_sema.cpp、posix_sema.cpp:105、lwlock.cpp:852/1102/1213、s_lock.h:160
- env-T 实测：ipcs -s（75×17）、/proc/4162/task 37 线程、kernel.sem=250 6400000 1000 25600
- 背景案例：anbob《tuning os performace kernel sem cause high sys cpu》（原文 404，机制按内核实现归类）
