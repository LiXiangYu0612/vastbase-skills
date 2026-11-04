# vb_tool AWR 采集补全 + vastbase 知识库补全 —— 执行计划（2026-10-27）

用户令："不要分开做 全做了 然后测试验证"。全量实现，逐批测试。

## 背景

经 vb_tool 源码（采样器 _awr_repl_sample:38119 / 解析器 load_os_samples:31790 /
渲染器 awrrpt:33991）与 linux 知识库全链路 diff，确定缺口：

## A. vb_tool 采集补全（9 项）

原则：**新增行类型优先**（backward-compatible，旧 os_*.log 照常解析），
字节级语法与 load_os_samples 的 count 校验同步。

### A1. per-minute 新增行类型（3 个，零破坏）
| 行类型 | 语法 | 来源 |
|---|---|---|
| `thp` | `thp,<ts>,compact_stall,compact_fail,thp_fault_alloc,thp_collapse_alloc` | /proc/vmstat thp_* |
| `conntrack` | `conntrack,<ts>,count,max` | /proc/sys/net/netfilter/nf_conntrack_{count,max} |
| `lstn` | `lstn,<ts>,recvq,sendq` | `ss -lnt4 sport = :$PORT`（取 0.0.0.0 行） |

### A2. sspeer 扩展（rcv_rtt/min_rtt/rcv_space）
- ss -tin 采样 awk 增加 parse：`rcv_rtt:`、`min_rtt:`、`rcv_space:`
- sspeer 行 8→11 字段：`sspeer,<ts>,peer,lport,rtt,rttvar,cwnd,retrans,mss,sendq,rcv_rtt,min_rtt,rcv_space`
- 同步 load_os_samples 的 count 校验（7→10 逗号）、window_peer_peaks 的 len!=8→!=11

### A3. END 快照新增（bond 三态 / nvcswch）
- bond：读 /proc/net/bonding/*，采 mode + active slave + 每 slave 状态（END 一次）
- nvcswch：读 postmaster /proc/<pid>/status 的 nonvoluntary_ctxt_switches（END 一次，
  与 DB Process 段同 pid 判定）
- missed 分列：net 行保持 raw /proc/net/dev（drop 已含 missed），新增 END 快照
  读 /sys/class/net/*/statistics/rx_missed_errors 逐口分列

### A4. 渲染端（零采集）
- TcpExt 补 3 行：TCPZeroWindowDrop / AbortOnLinger / AbortFailed——从已采集的
  END net_netstat 多解析 3 个计数器

## B. vastbase 知识库补全（3 项）
- linux/memory.md：Slab 增长（dentry/inode 泄漏）判读链、THP compact_stall、内存碎片
- linux/network.md：rttvar/mss 判读、conntrack count 判读
- performance/awr-report-reference.md + metrics.md：登记全部新字段语义与阈值

## C. 测试验证
1. 采样器：本地 WSL /proc 实跑，验证 thp/conntrack/lstn/sspeer 11 字段 行输出正确
2. 语法：python 语法检查（renderer 是 python 段）
3. 渲染：构造 os_*.log 喂 load_os_samples + window_peer_peaks，验证解析
4. 知识库：check_graph.py + awr_coverage_audit.py 全绿

## 顺序
A1 采样 → A2 sspeer → A3 END → A4 渲染 → B 知识库 → C 测试
