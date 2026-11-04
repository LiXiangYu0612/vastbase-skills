# fd 三层闸与 VFD 预算（max_files_per_process 生效链）

> 服务分类：troubleshooting（资源耗尽）、performance（File Handles 判读）；
> OS 层 fdtable/RLIMIT_NOFILE 机制见 `../linux/file-descriptors.md`（分层：
> 那篇讲内核怎么看 fd，本篇讲数据库自己在内核闸下怎么自我设限）

## 概述

"数据库打开文件数"有**三层闸**，从硬到软：进程 rlimit → 线程 VFD 预算 →
会话 AllocateDesc 配额。GUC `max_files_per_process` 的生效链是：
**postmaster 启动时换算一次 → 每个线程创建时以快照继承**——所以它真的是
"per process"语义（2026-10-30 源码定论；同日曾误判"线程恒 32"，经复核
推翻，见版本注记的教训记录）。

## 三层闸模型

| 层 | 闸 | 作用域 | 超限行为 |
|---|---|---|---|
| 1 内核 | RLIMIT_NOFILE（soft） | **进程**（fdtable 全线程共享） | open() 返回 EMFILE → 语句级 ERROR（"Too many open files" 族） |
| 2 VFD | `t_thrd.storage_cxt.max_safe_fds` | **每线程一份预算**（值同源，见生效链） | 静默 LRU 换出：关最久未用的内核 fd（VFD 结构保留），下次访问重新 open+seek——只有性能损耗，不报错 |
| 3 AllocateDesc | `numAllocatedDescs` vs `maxAllocatedDescs` / `max_safe_fds - 1` | **每会话**（AllocateFile/AllocateDir/OpenTransientFile 族：临时文件、COPY、目录扫描） | `ereport ERROR`：exceeded maxAllocatedDescs / MAX_ALLOCATED_DESCS，语句失败事务回滚 |

没有"线程挂/实例崩"路径；最坏是语句级 ERROR，层 2 甚至只是慢。

## max_files_per_process 生效链（源码定论）

- GUC 定义：默认 **1000**、最小 25、PGC_POSTMASTER（重启生效）；描述为
  "for each server process"（guc.cpp，master 树 :1954-1961）
- **换算点唯一**：`set_max_safe_fds()` 全源码树只在 `PostmasterMain`
  （postmaster.cpp:2003）调用一次：
  `max_safe_fds = Min(实测进程可用 fd, max_files_per_process − 已打开) − NUM_RESERVED_FDS`
  （fd.cpp:713-750）——此后 postmaster 主线程的该值不再变
- **线程继承（关键链，2026-10-30 复核定论）**：每个线程（业务 worker 与
  pagewriter/autovacuum/walwriter 等全部后台线程）创建时走
  `initialize_util_thread` / `initialize_worker_thread`（postmaster.cpp:
  12775/:12818）→ `save_backend_variables` 把 **postmaster 当前 t_thrd 快照**
  （含 GUC 派生的 max_safe_fds，:9890）写进线程参数 save_para → 线程入口
  `GaussDbThreadMain`（:12019）→ `PortInitialize`（:7910）→
  `read_backend_variables`（:10004）→ `restore_backend_variables`（:10012）
  把 `t_thrd.storage_cxt.max_safe_fds = param->max_safe_fds`（:10049）恢复进
  本线程。`knl_t_storage_init` 的 **32 只是线程骨架初值**（knl_thread.cpp:1402），
  在任何 fd 使用前就被上述恢复覆盖——**不是运行时闸**
- 推论：**改 GUC（重启）会改所有线程的 VFD 预算**——线程预算 = postmaster
  启动时刻的 Min(实测可用 fd, GUC − 已开) − 预留。~~G100 交付值 100000~~
  **标准已改 10000~20000（2026-11-02 用户令）**：现场 200000 出过多次问题
  （预算过大——fd 常开不收，内核内存与全系统 file-max 压力放大）；
  prlimit 才是最终截断
- **调低方向的代价（用户实战提示，2026-11-02）**：分区表分区特别多、
  local index 也多时（每分区每索引各是独立段文件），预算偏低 → VFD LRU
  频繁换出重开 → **频繁 open/close fd 造成性能衰减**——此类实例取标准区间
  上限（20000），并以 fd 抖动（open/close 速率）为观察项；判读见本文件
  "EMFILE/VFD 抖动分诊"节
- 第二生效点：Shared FileId hash 上限 = `Max(max_files_per_process,
  max_userdatafiles=8192−1000)`（fd.cpp:302/:324）——共享文件 id 表容量

## VFD 账本口径（nfile 在哪记账）

`GetVfdNfile()`（knl_localsysdbcache.cpp:435-442）：

- 开本地系统缓存（LSC）时 = `t_thrd.lsc_cxt.lsc->nfile`——**线程私有**计数
- 否则 = `u_sess->storage_cxt.nfile`——**会话级**计数

即 VFD 层的账本 granularity 是线程/会话，不是进程；换出检查
`ReleaseLruFiles`（fd.cpp:1016-1021）用当前线程自己的 max_safe_fds 对
当前账本计数。

## 超限三命运（判读速查）

| 现象 | 层 | 定性 | 处置方向 |
|---|---|---|---|
| strace 见密集 open/close 循环、吞吐缓降无报错 | 层 2（VFD 换出抖动） | 线程预算不够当前工作集 | 确认 GUC 值与 prlimit 余量；数据文件面大（分区/文件多）时可提 GUC（重启生效） |
| 语句报 exceeded MAX_ALLOCATED_DESCS / maxAllocatedDescs | 层 3 | 会话临时文件/目录句柄过多 | 查该会话是否巨量临时文件/COPY；层 3 上限跟随该线程 max_safe_fds |
| 语句报 Too many open files（EMFILE） | 层 1 | **进程 fd 撞 prlimit soft**——全线程共享池被吃满 | 看 `/proc/<pid>/limits` 与 fd 总数（vb_tool AWR 的 File Handles 行）；提 soft 限额或找 fd 大户 |

## 与监控的对接

- vb_tool AWR System Activity 的 **File Handles (used, Max open files
  soft/hard)** 行 = 层 1 的进程口径实况（used=postmaster fd 总数，max=
  /proc/\<pid\>/limits 实测，非 shell ulimit 理论值）
- DB Process 章的 fd 分类树 + 每分钟 fd 三桶（DBFile/TempFile/XLog）=
  层 1 的构成分解；TempFile 桶>0 即层 3 的主要消耗源在活跃落盘

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| fd 紧张只调 fs.file-max | 进程 rlimit 与库内 VFD 闸都在它之下 | 先 `/proc/<pid>/limits` 对账 |
| 拿 shell `ulimit -n` 当进程上限 | systemd/继承在进程启动时已定局，shell 值可以不同 | 以 `/proc/<pid>/limits` 实测为准 |
| 以为 VFD 预算进程共享一份 | t_thrd 线程本地（值同源但各自持有）；LSC 模式下 nfile 账本也线程私有 | 按每线程预算理解换出行为 |

## 版本注记

- 行号基于 openGauss master 快照（本地 gdb&stap 树，2026-10-30 精读）；
  6.0.0 正式树 src/gausskernel 不完整未能逐行对齐，结构与函数名同源，
  **行号在正式版可能有偏移**；VastBase G100（V3 档）内核分支未逐行验证，
  机制同源推断〔标注为推断〕
- ★复核教训（2026-10-30，用户令"再仔细看看"）：首版曾从"set_max_safe_fds
  唯一调用点 + knl_t_storage_init 默认 32"推"业务/后台线程恒 32、调 GUC
  对线程无效"——**漏了线程创建的 save/restore_backend_variables 快照链**
  （initialize_util/worker_thread → GaussDbThreadMain → PortInitialize →
  read_backend_variables），32 只是骨架初值被覆盖。教训：线程级 t_thrd
  变量除"谁写它"外必须核"**线程诞生时从哪继承**"——openGauss 线程创建
  有整套 BackendParameters 快照机制，grep 唯一调用点不等于唯一赋值路径

## 来源

- openGauss master 树源码精读（2026-10-30）：fd.cpp（set_max_safe_fds/
  ReleaseLruFiles/OpenTransientFile/Shared FileId）、postmaster.cpp
  （PostmasterMain:2003 调用、save_backend_variables:9852/:9890、
  restore_backend_variables:10012/:10049、read_backend_variables:10004、
  initialize_util_thread:12775、initialize_worker_thread:12818、
  GaussDbThreadMain:12019/:12082、PortInitialize:7910/:7929、
  initialize_worker_thread 调用点:7585）、knl_thread.cpp:1402（骨架初值）、
  knl_localsysdbcache.cpp（GetVfdNfile）、guc.cpp（GUC 定义）
- 交叉验证：troubleshooting/resource-exhaustion.md 的 EMFILE 现场判据；
  env-T 实例 postmaster `Max open files = 1000000/1000000`、fd 实测 135

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`。
