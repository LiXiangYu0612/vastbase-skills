# Linux 文件描述符层（fdtable / rlimit / proc 渲染）

## 概述

> 服务分类：linux（本分支）。AWR DB Process 章"fd 分类树、rlimit 逼近、
> pgsql_tmp"判读的内核层依据。DB 场景 fd 池与连接数基本线性绑定：
> 每连接一个 socket fd + 数据文件 fd + 临时文件 fd，共用同一张进程 fd 表
> 和同一个 RLIMIT_NOFILE 上限。

## fdtable：进程 fd 表的结构与扩容

- 每进程 `files_struct` → `fdtable`（include/linux/fdtable.h）：fd 指针数组 +
  open_fds 位图；内嵌 `fd_array[NR_OPEN_DEFAULT]`，**NR_OPEN_DEFAULT =
  BITS_PER_LONG（64 位机=64 个）**（fdtable.h:24）——超过才动态扩表
- **扩容 alloc_fdtable**（fs/file.c:162）：fdarray 按 **2 的幂次、1KB 档**
  扩（64 位最小有效 128 槽，file.c:166-180 源码注释）；上限被
  `sysctl_nr_open`（fs.file-max 之外的单进程顶，默认 1048576 档）向下取整
  到 BITS_PER_LONG 倍数（:182-196）
- **分配 alloc_fd**（fs/file.c:570）：从 start 起找首个空位；`fd >=
  fdt->max_fds` 走 expand_files 扩表再重试（:594-600）；**硬顶由调用方传入
  的 end=rlimit(RLIMIT_NOFILE)**——`get_unused_fd_flags →
  __get_unused_fd_flags(flags, nofile) → alloc_fd(0, nofile, flags)`
  （file.c:615-622）
- **/proc/\<pid\>/fd 就是 fdtable 的目录渲染**（fs/proc/fd.c:202
  proc_fd_instantiate，readlink 指向打开目标）——AWR 的 fd 分类树
  （socket / pgsql_tmp / 数据文件 / version.cfg）= 对这个目录逐类计数

## rlimit 与系统层上限

- **RLIMIT_NOFILE**：进程级（软/硬）。发行版默认差异大（1024 传统 vs
  systemd 时代 524288+）——**一律以 `/proc/<pid>/limits` 实测为准**，
  不猜默认值；**fd 总数逼近 Max open files = 泄漏或连接涨爆的前兆**
  （EMFILE 报错前 AWR 先看到）
- 系统层：`fs.file-max`（全系统打开文件总数）、`fs.file-nr` 三列
  （已分配 / 已分配未用 / 上限）——差值即余量
- 打开的文件本体在 file_table（fs/file_table.c），fdtable 只是引用数组；
  dup/继承共享同一 file，重复 open 各建 file——fd 计数≠文件计数

## DB 侧判读（fd 分类树 → 结论）

- **pgsql_tmp > 0 = 有活跃落盘**（排序/哈希溢出）——联 `performance/
  bottleneck-patterns.md` P6 取证链与 `../internals/operator-memory.md`；
  文件带数字后缀，作业完成即消失，**长时间驻留 = 大溢出作业或卡死作业**
- **version.cfg 计数增长 = 已知泄漏**（域内判例口径）：该文件计数应恒定，
  增长即 bug 证据
- socket fd 数 ≈ 连接数（每连接 1 个）+ 监听 fd——与 Threads、CurrEstab
  对账；**fd 涨而连接不涨 = 文件类泄漏方向；连接涨而 fd 同涨 = 容量方向**

## 排查命令速查

| 现象 | 命令 | 机制指向 |
|---|---|---|
| fd 逼近 rlimit | `cat /proc/<pid>/limits`；`ls /proc/<pid>/fd \| wc -l` | alloc_fd 的 end=RLIMIT_NOFILE 硬顶（file.c:615-622） |
| fd 泄漏定位 | `ls -l /proc/<pid>/fd \| awk` 按目标分类计数 | fd 分类树：哪类在涨（socket=容量 / 文件=泄漏） |
| pgsql_tmp 驻留 | `ls -l <data>/base/*/pgsql_tmp*` | P6 落盘作业（operator-memory.md 算子配额） |
| 系统级余量 | `cat /proc/sys/fs/file-nr` | file-max vs 已分配 |
| fdtable 扩容锁热 | perf 看 files->file_lock | 十万级 fd 频繁开合（alloc_fdtable/expand_files） |

## 版本注记

- 基线 7.1-rc4 精读（2026-10-27）；alloc_fdtable 的 2 的幂/1KB 档策略与
  CentOS 3.10 老内核同源；RLIMIT_NOFILE 默认值发行版相关，判读一律实测

## 来源

- 内核源码基线 /mnt/d/Obsidian/linux-master（7.1-rc4）精读（2026-10-27）：
  include/linux/fdtable.h:24,56（NR_OPEN_DEFAULT=BITS_PER_LONG、内嵌数组）、
  fs/file.c:156-196（alloc_fdtable 扩容策略与 sysctl_nr_open 封顶）、
  :570-600（alloc_fd 与 expand 重试）、:615-622（RLIMIT_NOFILE 传入链）、
  fs/proc/fd.c:202,312（proc_fd_instantiate / proc_readfd 渲染）
- vb_tool AWR DB Process 章 fd 分类树口径（`../performance/awr-report-reference.md`）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：pgsql_tmp→
`operator-memory.md`（P6）、连接数对账→`network.md` CurrEstab、
rlimit 参数→`../performance/parameters.md` 组 2。
