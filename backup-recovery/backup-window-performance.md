# 备份窗口对业务性能的影响与资源控制（备份期业务变慢四形态 / 时间轴定责）

## 概述

> 服务分类：backup-recovery（备份侧开张件之二）、performance（备份窗业务变慢判读）、
> troubleshooting（"一备份就慢"现场分诊）。回答三件事：备份期间业务变慢是四种形态
> 里的哪一种（各有确认动作）、慢窗怎么与备份作业时间轴对齐定责、资源控制在哪一层
> 拧哪个旋钮。**备份读不写业务表——凡备份窗内的业务变慢，都是共享资源（进程
> mm / 设备带宽 / 刷脏节奏 / 锁与快照）被挤占的传导**；先分形态，再对时间轴，
> 最后才谈限速。归档/恢复侧机制见同目录 `pitr-mechanism.md`。

## 四形态判读总表

| 形态 | 一句话机制 | 证据强度 | 怎么确认（下文展开） |
|---|---|---|---|
| ① 进程级 fork 税 | 归档命令从全库最胖的 mm 里 fork+exec 执行 | **判例定论**（env-E）+源码双锚 | sys 尖峰周期 vs 归档时间戳；perf 栈指纹 |
| ② IO 带宽挤占 | 备份顺序读与业务读/两层刷脏抢设备 | 方向性（判读口径成熟） | iostat r/w_await 介质分档+绝对 IOPS |
| ③ 启动 checkpoint 与刷脏交互 | 备份启动强制 checkpoint，备份窗 WAL/刷脏行为改变 | **源码锚**（影响量级方向性） | AWR Pagewriter 段+备份启动时刻对齐 |
| ④ 锁/快照保持 | 长备份若持快照/锁则压 vacuum 回收边界、挡 DDL | ⚓待锚（长事务效应本体已锚） | 备份窗长会话/锁等待现场核对 |

## 形态一：进程级 fork 税（判例定论）

**机制**：VastBase 线程模型下 archiver 是主进程的后台线程，执行归档命令走
`gs_popen_security`（**fork + exec + pclose 干等**）——每次归档都从全库最胖的
mm 里 fork。单进程大 mm（数百 GB 共享内存）下一次 fork = 页表整拷 + 私有集
COW 风暴 + 全核 TLB 广播三税并发，业务全体 worker 同窗受累。

**触发条件**：`archive_command` 配了外部命令。**源码分派细节（2026-11-05 核）**：
pgarch.cpp:795-798 `archive_dest is preferred over archive_command`——配了
`archive_dest` 时走 `PgarchArchiveXlogToDest`（pgarch.cpp:608，archiver 线程
**进程内 read/write 循环拷贝，不经 fork**）；只有 archive_command 形态才到达
调用点 **pgarch.cpp:853**（`rc = gs_popen_security(xlogarchcmd);`，位于
`pgarch_archiveXlog()` :783——行号 2026-11-05 亲核未漂）。调用链实锚：
gs_system.cpp:204（gs_popen_security 封装）→ :115 popen_security（pipe+
fork :130、子进程 dup2 stderr 后 execvp）→ :182 pclose_security（fclose+
waitpid 阻塞干等）。同族调用点还有恢复侧 restore_command（xlog.cpp:4971）与
recovery_end_command（xlog.cpp:5138）——PITR 回放窗有同构暴露（方向性，
无判例）。

**税单数字（判例权威出处，完整栈指纹判据表必读原案）**：
`../cases/case-env-E-sys-spike-archiver-fork-202611.md`——300G shared_buffers、
144 核实测：fork 复制页表 **3.2s**（archiver 线程停摆=归档延迟）；worker 私有页
COW 整页重拷 **4.5s**/**283MB**（72,646 次 wp_page_copy）；子进程 execve 拆
继承址空间再 **5.4s**；每次 COW 触发全核 TLB 广播、合计千万级 IPI（sys 冲高主体）。

**怎么确认**：
- mpstat/pidstat：尖峰窗全体线程 sys 同涨（单线程视角看不出责任者）
- 尖峰节奏 ≈ WAL 段归档频率，与归档成功时间戳逐个对齐（归档目录 mtime/pg_log）
- perf on-CPU 采样三件套栈指纹（copy_page_range / wp_page_copy+flush_tlb_page /
  exit_mmap←execve）——判据表照判例原文，勿凭记忆复述
- `/proc/<pid>/status` 的 VmPTE（GB 级=页表肥大）
- 配置面核对：当前是 archive_command 还是 archive_dest 形态（决定本形态是否在场）

**处置**（方案与预期分项见判例"处置与结果/预防"节）：快速缓解=`wal_segment_size`
调大降频（16M→256M 风暴次数÷16）；根治=2MB HugeTLB 大页（治页表复制与 exec
拆除两项；COW 广播只随降频变稀）；**反面教材=减 shared_buffers**（只线性缩一
项、伤命中率，负优化）。本件补两条源码级根治线索：**posix_spawn 化**（popen_security 现用经典 fork——gs_system.cpp:128 实锚；Linux fork+exec 有零页表拷贝路径 posix_spawn=clone(CLONE_VM|CLONE_VFORK)，两笔税为实现选择非内核必然，详见 ../troubleshooting/cpu-sys-high.md fork 税节）；**archive_dest 形态不 fork**
（pgarch.cpp:795-798/:608）——代价是归档退化为纯拷贝落目录，不能再挂外部
归档命令，属备份体系设计取舍。

## 形态二：IO 带宽挤占（方向性，判读口径成熟）

**机制**：备份/归档的顺序大读与业务读、库侧 pagewriter 刷脏、OS 层回写在同一
设备上排队。**备份读的路径事实（2026-11-05 核）**：pg_probackup 文件拷贝是
工具进程本地 `fread`/`fio_pread`（data.cpp:520/:1048/:1585），**不经库内共享
缓冲**——所以备份窗常见"AWR 缓冲命中率不坏、物理读不涨，而 iostat 在变差"，
挤占发生在 OS 页缓存/设备带宽层，不在 buffer pool。

**怎么确认**（口径全文照 `../linux/io-storage.md`，此处只列入口）：
- `iostat -x 1`：r_await/w_await 分列按介质分档（SSD>5 / HDD>20 ms 即异常）+
  绝对 IOPS；**%util 永不作判读标准**（2026-10-28 用户令）
- 备份读打 r_await、业务刷脏打 w_await——分列对齐能分清谁挤谁
- 周期尖峰对齐 checkpoint 节奏的判读走 io-storage.md 判读速查表"双层刷脏互踩"行
- OS 回写三闸门（dirty_background_ratio/dirty_ratio）与库刷脏的双层缓冲互踩
  判读同文件写路径节；缓冲写被 wbt 压住属正常，勿误判盘慢

**处置**：窗口错峰（业务低谷）、工具并行度调小（见资源控制节）、OS 层限外层
进程 IO（cgroup blkio，通用 Linux 手段）。介质档与绝对能力对账用 fio 基线
（direct=1）。

## 形态三：启动 checkpoint 与刷脏交互（源码锚；影响量级方向性）

**机制（2026-11-05 实锚，xlog.cpp do_pg_start_backup :14766）**：在线备份启动
时为防撕裂页做两件事——先 `RequestXLogSwitch`（:14892），再**强制 checkpoint**
（:14917 `RequestCheckpoint(CHECKPOINT_FORCE | CHECKPOINT_WAIT | (fast ?
CHECKPOINT_IMMEDIATE : 0))`）。CHECKPOINT_WAIT 意味着备份会话**阻塞到
checkpoint 完成**——备份启动延迟与当时脏页量直接挂钩。非增量检查点模式下同时
开 forcePageWrites（:14850-14854）——备份窗内每页首次修改写全页镜像，WAL 体积
放大；**增量检查点模式下 forcePageWrites=false（同处源码分叉），该放大不点火**，
但强制 checkpoint 照做。V3 默认增量检查点（`flush-checkpoint.md`）。

**怎么确认**：AWR Pagewriter Status 段（remain_dirty/behind_mb）与 checkpoint
记录、对照备份作业启动时刻；备份窗内 behind 三形态（能力不足/pin 住/WAL 制约）
的判读规则**照 `../internals/flush-checkpoint.md`**（本文不复制其结论）；
pg_log 中 checkpoint 触发日志与 "will do full checkpoint" 文案。

**处置**：`-C/--smooth-checkpoint`（见下节）把启动 checkpoint 从 immediate 摊开
（源码注释原话：不摊开 "this can take awhile"）；避免在业务写入高峰起全备。

## 形态四：锁/快照保持（⚓待锚）

**已锚的长事务效应本体**（引用，不重述）：vacuum 回收边界被长事务/复制槽压住
——vacuum 跑了也清不掉、膨胀积累（`../internals/autovacuum.md` OldestXmin 边界
与 need_freeze；`../internals/mvcc-visibility.md`）；备库长查询与回放的冲突
族（`../internals/recovery-conflict.md`）。

**备份持锁/持快照形态：源码无据，⚓待锚，不编**。已核的边界事实（2026-11-05）：
pg_probackup 物理备份的 SQL 会话只承担 pg_start_backup/pg_stop_backup
（backup.cpp:706/:1204），文件拷贝在工具进程本地（data.cpp）——"物理备份=压
OldestXmin 的长事务"在此形态**不成立或未证**；逻辑导出类长快照成立但属另一条
产品线。备份与 DDL 的锁交互未核⚓；唯一已锚的互斥是 exclusive 备份二次启动
直接报错（do_pg_start_backup 内 "a backup is already in progress"）——备份
自身互斥，不是业务 DDL 阻塞。

**怎么确认**：备份窗内现场核长会话/锁等待（有备份会话身影+等待链才算数，
推演不算）；膨胀归因先按 autovacuum.md 判读映射走长事务/槽核对。

## 资源控制手段（源码锚 + ⚓）

工具级旋钮以**基线 pg_probackup 选项表全量提取为准**（pg_probackup.cpp:190-251，
2026-11-05 核；交付名 vb_probackup）：

| 手段 | 基线源码事实 | 备注 |
|---|---|---|
| IO 限速 | **基线选项表无任何限速参数**（threads/timeout/batch-size 等，无 rate/throttle/bandwidth 类字样） | 交付版 vb_probackup 若提供限速参数属交付侧扩展——**参数名以官方手册为准⚓，禁凭记忆写**；基线可用的限速=OS 层 cgroup blkio 套在工具进程外 |
| 并行度 | `-j/--threads`（:194；默认 1 :74；上限校验 :790-799，超 nprocs×10 拒绝） | 备份 IO 并发第一旋钮；业务同窗时压小，独占窗口可放大 |
| checkpoint 形态 | `-C/--smooth-checkpoint`（:202 → backup.cpp:1218 `smooth ? "false" : "true"` 映射 pg_start_backup 的 fast 实参） | 控制**备份启动** checkpoint 是 immediate 还是摊开（形态三） |
| 读写超时 | `-t/--rw-timeout`（:231） | 是超时保护，**不是限速**，勿混用 |
| 窗口选择 | 调度层（crontab/备份平台） | 与业务峰错开；增量+merge 降低全备频率 |

库级与 OS 级：归档侧降频（`wal_segment_size`，形态一）与 archive_dest 形态
（免 fork）；`read_ahead_kb` 顺扫预读调优（io-storage.md，备份吞吐与业务延迟
的折点）；cgroup blkio/io 设备限流。

## 诊断路径：慢窗 ↔ 备份作业时间轴三层对齐（表 E 式）

**合取规则**（同 `../performance/check-inventory.md` 表 E）：单层=怀疑，两层
同向=立论，三层互证=闭环。用法：把业务慢窗分钟序列拉出来，横切下面三层同分钟
证据。

| 层 | 取证 | 备份窗特有的看点 |
|---|---|---|
| OS 侧 | iostat -x（r/w_await 分列分档+IOPS）、mpstat（sys/cs）、`/proc/<pid>/status` VmPTE | 备份读打 r_await；fork 税打 sys/cs 而 IO 完全正常——**两个形态在 OS 层就分开** |
| 作业侧 | 归档成功时间戳（pg_log/归档目录 mtime）、备份工具自身日志与起止时刻、调度器时间表 | 尖峰周期=段归档节奏（≈wal_segment_size/WAL 产速）还是=作业起止，直接分流形态①与②③ |
| DB 侧 | AWR 尖峰窗（DB Time/AAS/事件谱）、Pagewriter behind、checkpoint 记录 | 缓冲命中率不坏+物理读不涨=挤占不在 buffer pool（备份读不进共享缓冲，形态二的旁证） |

判读顺序：四形态总表定嫌疑 → 本表三层对齐定责 → 资源控制节选旋钮。

## 常见错误

| 错误 | 纠正 |
|---|---|
| 备份窗业务慢一律归 IO 抢占 | 先看 sys/cs：fork 税在 IO 完全正常时也能卡全体 worker（形态一判例） |
| 减 shared_buffers 治备份窗变慢 | env-E 反面教材：只线性缩页表税、不治 COW/TLB、伤命中率 |
| 拿 %util 判备份把盘打满 | util 永不作判读标准；await 分档+绝对 IOPS（io-storage.md） |
| 凭记忆写 vb_probackup 限速参数 | 基线选项表无 rate 类参数（:190-251 全量提取）；交付版以官方手册为准⚓ |
| 物理备份当长事务防膨胀核查项 | 文件拷贝在工具进程本地，SQL 会话只管 start/stop；压 OldestXmin 未锚⚓，现场核对长会话再定 |
| 备份窗缓冲命中率不降就排除 IO | 备份读不进共享缓冲——命中率不坏恰是 OS 层挤占的旁证，看 iostat |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 档；VastBase 内核源码不可得以开源近似、实测
  对账——同 `pitr-mechanism.md` 基线声明）
- 判例 env-E 2026-11（fork 税定论）；本件源码核对 2026-11-05
- 基线 pg_probackup 子命令枚举**无 archive-push**（2026-11-05 核）——env-E
  "归档命令疑似 vb_probackup（形态待取证）"的 ⚓ 与此一致，交付版是否带该
  形态以官方手册为准⚓

## 来源

- 源码核对（2026-11-05，openGauss 6.0.0 树）：
  `src/gausskernel/process/postmaster/pgarch.cpp`：783（pgarch_archiveXlog）、
  795-798（archive_dest 优先分派）、853（gs_popen_security 调用点，行号亲核
  未漂）、608（PgarchArchiveXlogToDest 进程内拷贝循环）；
  `src/common/port/gs_system.cpp`：115-167（popen_security：pipe+fork+execvp）、
  182-202（pclose_security：waitpid 干等）、204-243（gs_popen_security 封装）；
  `src/gausskernel/storage/access/transam/xlog.cpp`：14766（do_pg_start_backup）、
  14850-14854（ENABLE_INCRE_CKPT→forcePageWrites 分叉）、14892（RequestXLogSwitch）、
  14917（强制 checkpoint，fast?CHECKPOINT_IMMEDIATE）、4971/5138（restore_command/
  recovery_end_command 同族 fork 调用点）；
  `src/bin/pg_probackup/pg_probackup.cpp`：190-251（选项表全量提取）、74（threads
  默认 1）、790-799（threads 上限校验）；`backup.cpp`：117/706/1204-1227
  （smooth→fast 映射与调用链）；`data.cpp`：520/1048/1585（本地 fread/fio_pread）
- 判例（权威出处，本文只引用）：`../cases/case-env-E-sys-spike-archiver-fork-202611.md`
  （3.2s/4.5s/5.4s/283MB 税单、栈指纹判据表、处置预期分项）
- 引用件：`../linux/io-storage.md`（await 介质分档/双层刷脏/wbt）、
  `../internals/flush-checkpoint.md`（behind 三形态/checkpoint 生命周期）、
  `../internals/recovery-conflict.md`（长查询与回放冲突族）、
  `../internals/autovacuum.md`（长事务压回收边界）、
  `../internals/mvcc-visibility.md`（OldestXmin）、
  `../backup-recovery/pitr-mechanism.md`（归档/恢复侧同目录开张件）
- ⚓清单：①交付版 vb_probackup 限速参数（基线无，以官方手册为准）②物理备份
  会话是否压 OldestXmin（未逐行核会话生命周期）③备份与 DDL 锁交互形态
  ④env-E 归档命令是否 vb_probackup 形态（判例自带；基线无 archive-push）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`。互喂边：fork 税判例→
`../cases/case-env-E-sys-spike-archiver-fork-202611.md`；sys 冲高分诊→
`../troubleshooting/cpu-sys-high.md`；盘延迟判读→`../linux/io-storage.md`；
checkpoint/behind→`../internals/flush-checkpoint.md`；长事务效应→
`../internals/autovacuum.md`、`../internals/recovery-conflict.md`、
`../internals/mvcc-visibility.md`；表 E 合取规则→
`../performance/check-inventory.md`；归档/恢复侧→`pitr-mechanism.md`（同目录）。
