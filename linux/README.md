# linux/ — Linux 层性能与故障（规划中）

数据库部署在 Linux 上：OS 层的 CPU/内存/IO/网络既是 AWR 判读的另一半，也是
troubleshooting 现场救火的必经层。本分类从 **DBA 视角**讲 Linux——不追求
泛 Linux 运维大全，只收"数据库现象会落到哪个内核机制、哪个指标、哪个工具"
的知识。

## 方法论（铁律）

- **课程表**：《Linux 性能优化实战》体系（博客笔记版
  https://fanlv.fun/2020/09/13/linux-optimize/ 可直读），只当**主题框架与案例
  参照**，博客结论**不直接入知识文件**
- **判卷人**：内核源码定论。**全树未落地本地**；文件级镜像
  `/home/xiang/kernel-src/linux-7.1-rc4-mirror/`（v7.1-rc4 tag 按 plain
  接口逐文件抓取；2026-11-17 已 21 文件：bonding 两件/fib_frontend/ethtool
  ioctl/tcp 四件/tcp_input/ip_output/include 三件/net-procfs/sysctl/dev.c/
  nf_conntrack_core/snmp.h + 网卡驱动 8 件 igb·ixgbe·i40e·virtio_net·
  vmxnet3）；glibc 侧镜像 `/home/xiang/kernel-src/glibc-mirror/resolv/`
  （resolv.h/res_init.c 等，DNS 首连 5s 锚）——精读某子系统时先往镜像里
  补该文件再下结论；**大文件 60s 超时会拉成 0 字节空文件，落盘必须 wc -l
  验证**；在线辅助 https://deepwiki.com/gregkh/linux 。
  每条机制结论 file:line + 精读日期 + 版本注记；**生产内核版本老得多，
  行为差异必须标版本**（重点：writeback/内存水位/调度器差异）
- 已知博客旧口径（证明"源码对账"必要性，写文件时修正）：
  `oom_adj` 范围是旧内核口径（新内核已弃用，改 `oom_score_adj`）；
  `pdflush` 在新内核已并入 per-BDI flusher 线程

## 已落地文件（2026-10-25 五件 + 2026-10-27 补 file-descriptors + 2026-11-15 network 补 firewalld reload 节）

- ✅ `cpu.md` — 平均负载/上下文切换/D 状态/软中断；mpstat·vmstat·pidstat·perf；
  内核线程识别（ksoftirqd/kworker/kswapd）；us/sy/si/wa 与 VastBase 现象联判
- ✅ `process-states.md` — 任务状态位全枚举（R/S/D/T/t/Z/X/P/I ↔ TASK_* 位，
  task_state_array 权威字母表）；状态机睡醒主线（set_current_state+schedule /
  try_to_wake_up 唯一入口 / nr_uninterruptible 记账位）；D 深睡三型与 hung
  task 判据三版分叉（3.10 等值 / 4.19 点名 skip KILLABLE / 7.1 位测跳
  NOLOAD）；信号杀不死纯 D 的源码出处；取证命令族（wchan/stack/sysrq t）；
  版本对照 v3.10/v4.19 实拉存档（~/kernel-src/version-variants/）
- ✅ `memory.md` — 虚拟内存布局/伙伴+slab/page cache/水位与 swappiness/OOM kill
  全链路（kswapd·oom_reaper·oom_score_adj）；free 口径；与 53200/OOM 联判
- ✅ `io-storage.md` — IO 栈三层（VFS→page cache→块层）/inode·dentry 缓存/
  四种 IO 形态/调度器/iostat 口径；与库侧刷脏双层缓冲联判
- ✅ `network.md` — 收发两流程/半全连接队列/丢包七层排查/conntrack/TIME_WAIT/
  软中断/firewalld reload 断连判定（master+0.9.3 源码）；与连接风暴、
  主备同步网络联判
- ✅ `switch-side-logs.md` — 交换机侧（VRP S 系列）logbuffer 丢弃/拥塞日志族
  （PKT_OUTDISCARD_ABN/恢复通报/OAMDL）判读、interface brief 列语义、ARP
  汇总行；主机 Retrans 归因"中间网络"分支的落地件（2026-11-17 S12708 实拍
  锚；镜像口豁免=用户令）
- ✅ `net-diag-fullchain.md` — 网络全链路诊断：主动探测（ping/mtr/iperf3 及
  ICMP 限速假象）→网卡 ethtool -S 五族分类学/ring/offload/中断亲和→DOM 光
  功率对端互读对账→交换机端口计数器→双端抓包 SEQ 对账定责；端到端八步
  SOP；PCIe/AER 总线层；双端对时前置；物理层经验值为推断级（待华为手册/
  实机输出升级）
- ✅ `net-midpath.md` — 中间层网络诊断：交换机 L2 机制（bonding/LACP 落单/
  hash 极化/STP/MAC 漂移/堆叠）、L3 路径（VRRP/回程不对称 rp_filter/
  PMTUD 黑洞）、中间设备（防火墙 idle 掐断规律性指纹/NAT 源端口耗尽/LB/
  DNS 首连 5s）、虚拟化与云（迁移 stun/virtio 宿主机判读位/云限速假速率）；
  症状→落点速查表；VRP 侧推断级（内核树未落地，行级精读待做）
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
