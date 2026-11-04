# linux/ — Linux 层性能与故障（规划中）

数据库部署在 Linux 上：OS 层的 CPU/内存/IO/网络既是 AWR 判读的另一半，也是
troubleshooting 现场救火的必经层。本分类从 **DBA 视角**讲 Linux——不追求
泛 Linux 运维大全，只收"数据库现象会落到哪个内核机制、哪个指标、哪个工具"
的知识。

## 方法论（铁律）

- **课程表**：《Linux 性能优化实战》体系（博客笔记版
  https://fanlv.fun/2020/09/13/linux-optimize/ 可直读），只当**主题框架与案例
  参照**，博客结论**不直接入知识文件**
- **判卷人**：内核源码定论。本地基线 `<linux-7.1-rc4-src>`
  （7.1-rc4 全树，78G）；在线辅助 https://deepwiki.com/gregkh/linux 。
  每条机制结论 file:line + 精读日期 + 版本注记；**生产内核版本老得多，
  行为差异必须标版本**（重点：writeback/内存水位/调度器差异）
- 已知博客旧口径（证明"源码对账"必要性，写文件时修正）：
  `oom_adj` 范围是旧内核口径（新内核已弃用，改 `oom_score_adj`）；
  `pdflush` 在新内核已并入 per-BDI flusher 线程

## 已落地文件（2026-10-25 五件 + 2026-10-27 补 file-descriptors）

- ✅ `cpu.md` — 平均负载/上下文切换/D 状态/软中断；mpstat·vmstat·pidstat·perf；
  内核线程识别（ksoftirqd/kworker/kswapd）；us/sy/si/wa 与 VastBase 现象联判
- ✅ `memory.md` — 虚拟内存布局/伙伴+slab/page cache/水位与 swappiness/OOM kill
  全链路（kswapd·oom_reaper·oom_score_adj）；free 口径；与 53200/OOM 联判
- ✅ `io-storage.md` — IO 栈三层（VFS→page cache→块层）/inode·dentry 缓存/
  四种 IO 形态/调度器/iostat 口径；与库侧刷脏双层缓冲联判
- ✅ `network.md` — 收发两流程/半全连接队列/丢包七层排查/conntrack/TIME_WAIT/
  软中断；与连接风暴、主备同步网络联判
- ✅ `tracing-tools.md` — ftrace/perf/eBPF-BCC 取证链；火焰图；
  strace 对数据库的代价与替代
- ✅ `file-descriptors.md` — fdtable/RLIMIT_NOFILE//proc/<pid>/fd 渲染；DB 侧 fd 分类树（pgsql_tmp 活跃落盘、version.cfg 泄漏、rlimit 逼近）判读依据

## 与现有域的互喂边（落地时进 KNOWLEDGE-GRAPH）

- kernel-data-path ↔ io-storage·memory（数据落盘全路径：库缓冲→write→
  page cache→块层）
- flush-checkpoint ↔ io-storage（vm.dirty_* 双层缓冲互相踩）
- commit-sync-events ↔ io-storage（fsync 语义与块层刷新）
- buffer-manager ↔ memory（shared_buffers 与 page cache 双缓存）
- metrics ↔ 全部（iostat/vmstat/softirq 口径对账）
- log-analysis ↔ memory·network（OOM kill 记录、连接失败）
- parameters 组 3/5/7 ↔ io-storage·memory·network（vm.\*/net.\* sysctl）

内容未落地前，涉及本分类的问题按实情说明知识未收录。
