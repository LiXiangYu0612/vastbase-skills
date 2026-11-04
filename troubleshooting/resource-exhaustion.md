# 资源耗尽应急（fd / 内存 / 信号量 / 端口）

## 概述

> 服务分类：troubleshooting。四类 OS 级资源耗尽的判据与应急；深层机制在
> `../linux/` 对应文件。共同铁律：**先看限额、再看持有者、最后谈调额**。

## 1. 文件描述符（fd）

**判据**：

```bash
cat /proc/<pid>/limits | grep 'open files'   # 进程级限额（软/硬）
ls /proc/<pid>/fd | wc -l                    # 当前持有
cat /proc/sys/fs/file-nr                     # 系统级：已分配(含未用) 已用上限
```

- 库侧限额 GUC `max_files_per_process`（**默认 1000**，最小 25，
  guc.cpp:2290-2298）；进程报 "Too many open files" = 撞进程软限额，
  没到系统上限
- ★分层判读（2026-10-30 源码定论+当日复核修正，机制全貌见
  `../internals/fd-vfd-budget.md`）：**EMFILE 报错的闸=进程 rlimit**（全
  线程共享 fdtable）；**VFD 换出抖动（strace 密集 open/close 无报错）的
  闸=每线程 max_safe_fds**，该值由 GUC 经 postmaster 快照继承到全部线程
  ——调 GUC（重启生效）能抬所有线程的 VFD 预算
- 持有者画像：`ls -l /proc/<pid>/fd | awk -F'-> ' '{print $2}' | sed
  's/[0-9]\+//g' | sort | uniq -c | sort -rn` —— socket 海量=连接泄漏
  （→ `connection-refused.md` 闸 4）；数据文件海量=fd 复用异常/表爆炸

**处置**：泄漏类杀会话释放；真需求类提高进程软限额（`/proc/<pid>/limits`
  实测为准，不超 fs.nr_open）+ `max_files_per_process`（postmaster 级，
  重启后经快照链生效到所有线程——EMFILE 靠前者解，VFD 抖动靠后者解）。

## 2. 内存

**判据**（详见 `../linux/memory.md` 判读速查）：

- `free -m` 看 **available** 列；`vmstat 1` si/so 持续 >0 = 活跃换页
- 分层定责：**53200 = DB 自己的限额拦截**（max_process_memory 体系，
  `../internals/operator-memory.md`）；**OOM kill = OS 物理耗尽**（dmesg
  "Out of memory: Killed process"）——两层闸门，先分层再动参数

**处置**：OOM 已杀主进程 → 先取证（dmesg + dump_tasks 全表）再拉起，否则
  现场丢失；库内存配比核算走 `../performance/parameters.md` 组 5。

## 3. 信号量

**判据**：

```bash
ipcs -ls                    # 数组/每数组信号量/每进程上限
ipcs -s | wc -l             # 当前信号量数组数
dmesg | grep -i 'sem'       # "No space left on device" 伴随信号量=SEMMSL/SEMMNI 满
```

- 残留信号量数组（上次异常退出的尸体）会顶满 SEMMNI：`ipcs -s` 里
  **nsems 与库配置一致且属主为库用户**的旧数组，确认实例未运行后可
  `ipcrm -s <id>` 清理——**清前必须确认没有活实例在用**（对照
  `startup-failure.md` 锁文件与进程清单）

## 4. 端口（客户端侧出口耗尽）

**判据**：应用报连接失败 + `ss -s` 里 TIME-WAIT 海量（出站短连接风暴，
  60s 常量堆积，`../linux/network.md` TIME_WAIT 节）+ `ip_local_port_range`
  用尽（`ss -tn state time-wait | wc -l` 对 range 容量）。

**处置**：应用连接池长连接是根治；临时扩 ip_local_port_range +
  tcp_tw_reuse=2（仅出站）。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| fd 满直接调 fs.file-max | 进程软限额/max_files_per_process 才是闸门（1000 默认） | /proc/<pid>/limits 对账 |
| 分不清 EMFILE 与 VFD 抖动该调哪个 | EMFILE=层 1（prlimit），抖动=层 2（GUC 派生的线程预算） | 先分层再调参（`../internals/fd-vfd-budget.md` 三层闸表） |
| OOM 后立即重启拉起 | 现场（dmesg dump_tasks 表）一去不返 | 先取证后拉起 |
| ipcrm 清信号量不看属主/实例 | 误删活实例的 IPC = 二次事故 | 进程清单+锁文件确认后清 |
| TIME_WAIT 海量调内核回收 | 出站短连接才是根 | 连接池根治（network.md） |

## 版本注记

- max_files_per_process 默认 1000 为基线源码值（guc.cpp:2297）；信号量/
  端口/内存判据为 Linux 层，3.10 与 7.1 一致（各 linux/ 文件版本注记）

## 来源

- 基线 openGauss 6.0.0 源码：common/backend/utils/misc/guc.cpp:2290-2298
  （max_files_per_process）
- Linux 层机制：`../linux/memory.md`、`../linux/network.md` 来源节（7.1-rc4 精读）
- env-A/env-B 实战配方（2026-10 系列）

## 知识网

本文件为操作层：memory（水位/OOM）、network（TIME_WAIT/端口）、
operator-memory（53200 分层）、parameters（组 5/7 旋钮）。
