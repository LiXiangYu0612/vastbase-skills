# Linux 内存层（水位线 / kswapd / swappiness / OOM 全链路）

## 概述

> 服务分类：linux（本分支）、performance（AWR OS 段内存/free 判读）、
> troubleshooting（pswpout、OOM kill 现场）。数据库是最大的匿名页+页缓存
> 消费者，OS 内存层的每一次回收/换出/OOM 都直接打在库上。

## free 口径：只有 MemAvailable 值得看

- `free` 的 `free` 列含 ZONE_NORMAL 低位保留，**不可用**；判据列是
  **MemAvailable**（fs/proc/meminfo.c:56-62 ← `si_mem_available()` 内核估算：
  低水位之下的内存不算可用——**它是估算值不是精确余量**）
- buff/cache 含 **SReclaimable**（可回收 slab，meminfo.c:108-109）：dentry/
  inode 缓存等（见 `io-storage.md`），内存紧张时能让出来，但让的过程有成本
- swap used ≠ 坏事（历史换出未换入也算 used）；**看的是速率**：`vmstat 1`
  的 si/so 列（换入/换出页/秒），持续 >0 才是活跃换页

## 水位线：分配路径的三道闸

- 三道水位（include/linux/mmzone.h:796-799）：**WMARK_MIN / LOW / HIGH**
- 驱动水位的旋钮 `vm.min_free_kbytes`（mm/page_alloc.c:268 默认起点 1024KB；
  变更后 `setup_per_zone_wmarks` 重排三道水位 :6441）
- 分配路径行为：
  - 自由区 > HIGH：直接分配
  - LOW < 自由区 ≤ HIGH：分配继续，**同时唤醒 kswapd 后台回收**
  - ≤ LOW：**分配者自己进直接回收（同步！）**——`ps` 可见 D 状态、卡顿尖峰
    的常见来源；MIN 之下还伴随更激进的回收乃至 OOM
- **DB 判读**：突发大分配（sort/hash 之外的 brk 大段、备份进程）触发直接
  回收时，所有进程的分配都会卡——AWR 上表现为无等待事件的 latency 尖峰；
  处置 = 提高 min_free_kbytes 垫高缓冲（`parameters.md` 组 5）

## kswapd 与回收

- 内核线程 **kswapd%d**（mm/vmscan.c:7638；主函数 kswapd :7438，每 NUMA 节点
  一个），核心 `balance_pgdat`（:7108）在 LOW 唤醒后一直回收到 HIGH 才睡
- 回收对象双 LRU：**匿名页（anon，可换出到 swap）与文件页（file，可丢弃/
  回写）**；比例由 swappiness 权重 + 各自 refault 代价动态决定（下节）
- **Shmem 计入 Cached（meminfo.c:46-48 源码定论）**：
  `Cached = NR_FILE_PAGES − SwapCached − Buffers`——tmpfs/共享内存页住在
  文件 LRU 上（NR_FILE_PAGES 含它）且不在减项里，所以 **meminfo 的 Cached
  天然含 Shmem**；Shmem 行本身 = `i.sharedram`（:105）。判读后果：
  **"Cached 高=可回收"在 Shmem 大的机器上会高估**——shmem 页不能像普通
  页缓存那样直接丢弃重生，只能换出或显式释放；估可用内存看 MemAvailable
  （si_mem_available 的口径含 shmem 修正），别拿 Cached−Shmem 自己减。
  DB 关联：数据库共享内存段（/dev/shm 类）落在 Shmem，也就被 Cached 重复
  展示——AWR 判读时 Shmem 行大属正常形态，不是页缓存膨胀
- kswapd 吃 CPU（单线程回收）= 内存压力的形态之一，别只盯 IO 看

## swappiness：代价加权比例，不是硬开关

mm/vmscan.c 深读（:2388-2420 get_scan_count）：

```
ap = swappiness           × (total_cost+1) / (anon_cost+1)
fp = (MAX_SWAPPINESS − swappiness) × (total_cost+1) / (file_cost+1)
```

- **语义**：ap/fp 是两条 LRU 的扫描压力配比，**还乘着各自的回收代价**
  （refault 率、换入 vs 重读 IO 代价）；源码注释原话："at least a third of
  the pressure is applied, before swappiness"——**两条链都保底有压力，任何
  swappiness 值都不会把一条链压成 0**
- 默认 **60**（vmscan.c:201 `vm_swappiness = 60`）；memcg 可独立设置
  （sc_swappiness → mem_cgroup_swappiness，:244-248）——独立值仅 cgroup
  **v1** 的 memory.swappiness 旋钮（memcontrol-v1.c:1988 为其 write 侧），
  cgroup v2 已移除该旋钮
- DB 惯例 vm.swappiness=1~10（**实践惯例，非源码结论**）：目标是让 file 页
  （页缓存可快速重生）优先让路，少碰匿名页；**=0 不是"禁用 swap"**，极端
  匿名压力下照样换出甚至先 OOM
- **版本注记**：3.x/4.x 的 get_scan_count 有"无 file 页可收时强制扫 anon"的
  特例分支，swappiness=0 时仍可能换出；7.1 是纯比例模型。跨版本别把一个
  结论当普适

## OOM 全链路（mm/oom_kill.c）

触发 → `dump_tasks`（:413-422，`sysctl_oom_dump_tasks=1` :58 默认开，dmesg
里的全进程内存表就是它打的）→ `select_bad_process`（:362）按 **oom_badness**
（:191-199）打分选受害者 → kill + **oom_reaper**（:508-511）异步收割地址空间
加速释放。

- **打分口径**：badness ≈ 进程驻留页数 + 基础分，再被 oom_score_adj 线性平移：
  - 现代接口 `/proc/<pid>/oom_score_adj` 取值 **-1000..1000**
    （include/uapi/linux/oom.h:9-10）；**-1000 = 免死**（oom.h:6 注释明言）
  - 旧接口 oom_adj（-16..15，uapi/oom.h:18-19）**已弃用**——
    《实战》笔记里"-17~15"是旧口径混写，以源码为准
- **DB 联动（高价值）**：vastbase 主进程被 OOM kill 的现场特征 =
  dmesg "Out of memory: Killed process … vastbase" + log_analysis 侧实例
  突然重启记录（`log-analysis.md`）；**分层判**：OS 层 OOM 说明
  max_process_memory+共享内存+页缓存总账超了物理内存（`parameters.md` 组 5
  的配比核算），而 53200 是 DB 自己的限额拦截——**两者是两层闸门**
  （DB 限额 → 53200，见 `operator-memory.md`；OS 物理耗尽 → OOM kill）
- oom_reaper 只收割地址空间不等退出：**老内核（3.10）没有 oom_reaper**，
  kill 后释放慢、二次 OOM 概率高——env-A 档注意

分诊入口：OOM 四型（内核 killer/cgroup/库侧 53200/换页前驱）见
`../troubleshooting/oom-analysis.md`。

## 页缓存与数据库：双缓存结构

- VastBase 的 shared_buffers 是**用户态**缓存（见 `buffer-manager.md`），OS 页缓存
  是**内核态**第二层——数据文件读路径两次缓存命中判定不同层；AWR 命中率
  低但物理读也低时，考虑 OS 页缓存在兜底
- 文件页脏写回不是即时：write 进 page cache 即返回，真正落盘由回写线程异步
  完成（全链路见 `io-storage.md`）——**fsync 才是库的持久化闸门**
  （`commit-sync-events.md`）
- 缓存抖动判据：workingset refault（mm/workingset.c:546 workingset_refault；
  驱逐后很快又被读回=工作集大于缓存），/proc/vmstat 的 workingset_refault
  计数持续涨 = 页缓存容量与工作集不匹配，加内存或收数据量，调参救不了

## 透明大页（THP）

- 开关族 `transparent_hugepage_flags`（mm/huge_memory.c:59）：always/madvise/
  never + defrag 策略位；**khugepaged** 后台线程负责合并
- DB 场景的实践惯例（非源码结论）：THP=always 会带来分配时同步碎片整理
  （defrag）导致的延迟尖峰与内存放大（2MB 粒度占用），数据库机器常配
  **never 或 madvise**；判读现场：`cat /sys/kernel/mm/transparent_hugepage/enabled`
- **compact_stall 判读（AWR THP 表新增）**：/proc/vmstat `compact_stall`=直接
  压缩停顿次数（khugepaged/alloc 触发的同步压缩），**>0 且增长 = 碎片化严重、
  分配卡顿的元凶**——DB 延迟尖刺与它同窗，几乎可定位到 THP=always + 内存
  碎片；`compact_fail`=压缩失败、`thp_fault_alloc`=fault 路径大页分配、
  `thp_collapse_alloc`=khugepaged 合并
- **碎片状态（/proc/buddyinfo，AWR Memory fragmentation 节）**：buddy 分配器
  每 zone 每 order 的空闲页计数。判读：**碎片率 = 1 − (order≥9 空闲页 / 总
  空闲页)**，即"空闲内存里拿不出 2MB 连续块的比例"——**碎片率>50% 且总空闲
  足够 = 碎片化**（order0 单页满天飞、连 2MB 都拼不出），THP/大页分配失败
  的前兆；此时 compact_stall 会同步涨（碎片触发直接压缩）。碎片率低但
  compact_stall 高 = 分配突发而非持续碎片

## 页表（PageTables / VmPTE）：fork 税与大页收益的读数

- 口径：`/proc/meminfo` 的 `PageTables`（全机已分配页表页）、
  `/proc/<pid>/status` 的 `VmPTE`（单进程）；渲染同 fs/proc/meminfo.c
  锚（见来源）
- 数学：每 2MB 已映射地址空间占 1 张 4KB PTE 页 → **页表量 ≈ 活跃映射量/512**。
  例：300G 全触达共享内存 ≈600MB 页表
- **判读"过大"**：VmPTE/PageTables 明显超过"映射量/512"的应有量，或单进程
  VmPTE 达 GB 级且 HugePages 未启用 → 页表肥大。危害不在驻留（几百 MB 无妨），
  在 **fork 税**：`copy_page_range`（kernel/fork.c）按页表总量整拷——GB 级页表
  = 秒级单核停摆，且遍历中把父进程 MAP_PRIVATE 页改只读，引发 COW 风暴 +
  全核 TLB 广播（env-E 实测：fork 停摆 3.2s + 4.5s 风暴；VmPTE GB 级读数为
  **现场口述待取数确认⚓**，成分拆解见判例遗留，见
  `../cases/case-env-E-sys-spike-archiver-fork-202611.md` 与
  `../troubleshooting/cpu-sys-high.md` fork 税节）
- 处置：`enable_huge_pages`（HugeTLB）——共享区份额归零：fork 停摆与 exec
  拆除消除；**私有集 COW 风暴不归大页治**（分工见 cpu-sys-high）；
  THP 不解决此问题（私有匿名页动态合并，另有拆分坑）
- 采集现状：PageTables 已在 AWR OS Memory Usage 表；VmPTE（进程级）未采集
  ——诊断 fork 税时手工 `grep VmPTE /proc/<主进程pid>/status`

## Slab 增长与内核对象泄漏（dentry/inode，区别于 fd 泄漏）

- /proc/meminfo 三值：**Slab**（总量）= SReclaimable + SUnreclaim
  - **SReclaimable**：可回收 slab（dentry/inode 缓存主体）——高但能回收=正常
    （元数据页缓存），压力大时 kswapd 会回收
  - **SUnreclaim**：不可回收 slab（内核对象，无法释放）——**单调增长不回落 =
    泄漏**（dentry/inode 泄漏典型，也有网络/文件系统结构泄漏）
- 与 fd 泄漏的区别（关键分诊）：`file-descriptors.md` 的 fd 泄漏 = 进程没关
  描述符（fd 表涨）；**slab 泄漏 = fd 关了但内核对象没释放**（引用计数漏减，
  dentry/inode 残留）——fd 数不涨而 SUnreclaim 涨，就是这一类
- 判读现场：`cat /proc/meminfo | grep -E 'Slab|SReclaim|SUnreclaim'` 多时点
  采样看 SUnreclaim 斜率；`slabtop -s c` 或 `cat /proc/slabinfo` 定位具体
  对象类（dentry/inode/xfs_inode 等）

## NUMA 拓扑与平衡（AWR NUMA 三表）

多路机的内存分节点，**跨节点访问 = 远程内存，延迟 1.5~2×、走 UPI/QPI 互联**
——AWR 的 NUMA 三表（节点拓扑 / numastat 平衡 / 每节点 MemTotal-Free）就是
判 DB 内存布局是否健康的一手证据。

**numastat 计数器语义**（渲染 drivers/base/node.c:572-582 `node_read_numastat`；
计数点 mm/page_alloc.c:3193-3198 分配时 `__count_numa_event`）：

| 计数器 | 语义 |
|---|---|
| numa_hit | 任务在本节点分配内存成功（按偏好） |
| numa_miss | 任务**想**在本节点分配，实际落到**别的**节点 |
| numa_foreign | 别的节点的任务分配到**本**节点来了（=别的节点的 miss 落到这） |
| interleave_hit | interleave 策略在本节点命中 |
| local_node | 本节点任务分配在本节点（本地） |
| other_node | 本节点任务分配在**别的**节点（远程） |

**Local Ratio = local_node/(local_node+other_node)**——本节点任务的本地分配占比。
判读线：

- **≥95% 健康**；**<90% 且 miss/foreign 持续涨 = 失衡**：该节点的任务大面积
  用远程内存，远程延迟放大 + 互联带宽被挤占
- **失衡的方向（关键）**：local 低 + MemFree 也低 = 本节点内存不够用（
  shared_buffers 太大或单节点绑死，任务被迫溢出）；local 低 + MemFree 不低
  = **内存布局错配**（shared_buffers 跨节点 interleave，但线程绑单节点跑，
  读另一半必远程——最常见）
- **env-M 实例实测（2026-10-26 报告）**：Node0 local 40.23M / other 6.47M
  = **86.15%**，Node1 99.60%——Node0 失衡，13.85% 远程访问；MemFree
  Node0 14GB > Node1 9.4GB，方向指向"布局错配"而非"内存不足"

**排查与处置（非源码结论，操作层；一律走内核内建路径，不依赖 numactl 包）**：

1. **判读优先用 AWR 已采的 NUMA 三表**（numastat 数据 = `/sys/devices/system/
   node/node*/numastat` 内核渲染，不依赖 numactl）；node 拓扑与距离矩阵：
   `/sys/devices/system/node/node*/cpulist` + `/sys/devices/system/node/node*/distance`
2. `cat /proc/<postmaster>/numa_maps`（内核内建）看 shared_buffers 大段落
   在哪个节点
3. shared_buffers 跨节点时：`numactl --interleave=all` 启动 DB 让缓冲均匀落地
   （**若装了 numactl**），或确认线程亲和（taskset/cpuset）与内存节点一致——
   numactl 是 numactl 包，未装时上述 /sys、/proc 路径全可替代
4. 内核自动迁移 `zone_reclaim_mode`/`numa_balancing` 会引入迁移抖动，DB
   场景多关自动、手工定布局。**实测更正（2026-11-04）**：3.10.0-1160.el7
   NUMA 平衡在跑（env-E 判例实测 do_numa_page/migrate_misplaced_page 栈），
   早期"3.10 无 numa_balancing"注记作废——两口径以此为准统一

## 内存构成全景：meminfo 逐键判读（"哪里异常"对号表）

> 先定性后处置，**不预设结论**——每行异常有各自的去向，大页只是"页表肥大"
> 这一个分支的处置。口径全部出自 fs/proc/meminfo.c（同来源锚）；采集现状
> 见文末缺口。

| 键 | 是什么 | 何时可疑 | 下一步 |
|---|---|---|---|
| MemAvailable | 可用水位（非 free，见上节） | ≤10%/20% | P7 水位链 |
| Buffers/Cached | 页缓存（DB=双缓存结构，见下文专节） | Cached 高但读仍走物理 IO | buffer-manager 命中率对账 |
| Active(file) / Inactive(file) | 页缓存冷热两链（近似 LRU 新旧代） | Cached 大但 Inactive(file) 占比小=热集大，压力下**可回收余量小**；只涨不降=工作集真在长 | 与 Cached 对账；压力窗看 pgsteal 来源 |
| Active(anon) / Inactive(anon) | 匿名页冷热两链 | 同上口径；注意 anon 的回收**只有换出一条路**（无 swap 则整类不可回收） | 换页链（P7） |
| Unevictable | 不可回收驻留（mlock/特殊映射） | 存在量可观即永久挤压 | 找 mlock 持有者（`/proc/<pid>/smaps` grep locked） |
| SwapCached / SwapTotal | 换页与换出缓存 | pswpout 持续>0 | P7 回收链 |
| AnonPages | 进程私有匿名页（堆/jemalloc/线程栈） | 持续涨且 DB 上下文同步涨=真消费；DB 侧不涨=库外进程 | 对 Database Memory / Max Session Memory 对账；库外→进程 RSS 榜 |
| Shmem | 共享内存（含 tmpfs） | 超出 DB 共享区应有量 | `df -h /dev/shm`、`ipcs -m` 找 tmpfs/SysV 挂账 |
| SReclaimable | 可回收 slab（dentry/inode 缓存） | 大而无妨（压力下自动回收，已计入 Available） | 与 SUnreclaim 分开看，别合并判 |
| SUnreclaim | 不可回收 slab | **持续涨=内核对象泄漏** | `slabtop` 定对象；见下文 Slab 泄漏节 |
| KernelStack | 每线程一份内核栈 | 线程数巨大时线性放大（万线程=百 MB 量级；单份大小=THREAD_SIZE，按内核版本⚓） | 对线程数（`ps -eLf` 计数 / db_proc Threads） |
| PageTables | 页表 | 超"活跃映射量/512"应有量；GB 级+未用大页 | 上文页表节（fork 税链） |
| Committed_AS / CommitLimit | 已承诺分配 vs 限额 | overcommit_memory=2 时比值逼近 1 → 新分配被拒（库内 OOM 报错的库外成因之一） | `cat /proc/sys/vm/overcommit_*`；=0 时该比值仅记账 |
| VmallocUsed | vmalloc 区（模块/大块内核分配） | 异常增长 | `/proc/vmallocinfo` 按调用者聚合 |
| HugePages_Total/Free/Rsvd | 大页预留与用量 | Reserved 高而 Free/Rsvd 恒定=**预留浪费（白占内存）** | 校 `vm.nr_hugepages` 与实际用量 |

进程级三键（主进程 `/proc/<pid>/status`）：**VmRSS** 对 DB 上下文之和做差——
差值大=非上下文消费（库自身结构/分配器碎片/库外借宿）；**VmPTE** 见页表节；
**VmSwap** 显著=主进程被换出（数据库主进程被换出本身即事故级异常，先查
swap 诱因再谈别的）。

### 构成 × 水位联动（判读主链，机制详见"水位线/kswapd"两节）

可回收性从高到低 ≈ 干净 Inactive(file) → SReclaimable → 脏页（先写回）→
anon（有 swap 才可能）→ **不可回收**（SUnreclaim、Unevictable、无 swap 时的
全部 anon、PageTables 等结构性开销）。据此三问：

1. **涨的类可回收吗？**——涨 Cached/SReclaimable 而水位稳、无换页=正常缓存
   增长，不处置；涨 SUnreclaim/Shmem 锁定/Unevictable=水位迟早破，提前挂账
2. **水位将破时谁挡路？**——pgscan 高而 pgsteal 低=回收空转，此时占量大且
   不可回收的那一类就是异常类（联动 per-min 的 pgscan/pgsteal/pswpin/pswpout）
3. **到 OOM 时谁幸存？**——被 kill 前回收不动的那块=结构性超额（无 swap 的
   anon / SUnreclaim 泄漏 / Committed_AS 顶死 CommitLimit）

跨表对账：per-min 回收计数 × meminfo 构成 × 水位三线（/proc/zoneinfo 的
min/low/high）——**三源同向才定论**，单看"内存用了多少"不构成判断。

★采集与渲染现状（2026-11-04 三轮核实终版）：采集层全量 `cat /proc/meminfo`
+ `/proc/zoneinfo`（水位）+ buddyinfo（碎片）+ postmaster 的
`/proc/<pid>/status`（VmSize/VmRSS/Threads/cpuset/上下文切换，db_proc 段）。
渲染已覆盖：OS Memory Usage 表 20 键（含 PageTables/LRU 四链/Slab 两半/
Mapped/Dirty）、zone 水位/碎片表、postmaster 进程段。真缺口收敛为两条：
①渲染缺 6 键——KernelStack/AnonPages/Committed_AS/Unevictable/AnonHugePages/
VmallocUsed（数据已在采集包，渲染侧补齐即可）；②进程级 VmPTE/VmSwap 未采
（db_proc 的 awk 补两个匹配即得；诊断 fork 税/主进程被换出前手工
`grep -E 'VmPTE|VmSwap' /proc/<主pid>/status`）。

⚠核实教训（两次翻车后立此存照）：判"报告缺不缺某指标"，必须**先把报告
全量提取再 diff**（枚举渲染键 `>Key<`、section 标题、zone 表行），不许拿
局部 grep 的"没搜到"直接下"没采集"结论——本节曾先后误判"meminfo 只采
4 键""zoneinfo/进程级未采集"（grep 形态错/没查就断），2026-11-04 更正。

## 判读速查（现象 → 命令 → 机制）

| 现象 | 命令 | 机制指向 |
|---|---|---|
| MemAvailable 低但无换页 | `vmstat 1` si/so；`cat /proc/vmstat \| grep -E 'pswpin\|pswpout'` | 页缓存挤占，kswapd 在工作；先核 DB 内存配比（`parameters.md` 组 5） |
| si/so 持续 >0 | `vmstat 1`；`smem -rk` 找匿名页大户 | 匿名压力实锤；swappiness 与容量核算 |
| 分配卡顿尖峰（无 DB 等待事件） | `ps -eo stat,pid \| grep ^D`；核 min_free_kbytes | LOW 之下直接回收同步卡（水位线节） |
| dmesg "Out of memory: Killed process" | `dmesg -T \| grep -i oom`；`journalctl -k` | OOM 全链路节；对照 dump_tasks 表找谁吃内存；VastBase 联动 `log-analysis.md` |
| kswapd 长期吃 CPU | `top -H` | 回收跟不上分配；先减压力源 |
| workingset_refault 涨 | `grep workingset /proc/vmstat` | 页缓存容量 < 工作集（workingset.c:546） |
| NUMA Local Ratio 低 | `cat /proc/<pid>/numa_maps`；`/sys/devices/system/node/node*/`（numactl 未装也可） | NUMA 节：local/(local+other)<90% 失衡，分"内存不足 vs 布局错配" |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 看 free 列判余量 | 低水位保留不可用 | 看 MemAvailable（meminfo.c:56 估算口径） |
| swap used > 0 当故障 | 历史换出残留也算 | 看 si/so 速率（活跃换页才是） |
| swappiness=0 当禁 swap | 比例模型保底+版本特例（vmscan.c:2388-2420） | 容量核算才是根治；1~10 是权衡惯例 |
| OOM 只看被杀进程 | 受害者是打分最高者，不一定是元凶 | dump_tasks 全表 + 总账核算（DB 配比） |
| 53200 与 OOM kill 混为一谈 | DB 限额拦截 vs OS 物理耗尽，两层闸门 | 先分层再定责（operator-memory.md / 本文件） |
| buff/cache 高当内存不足 | 绝大部分是可回收页缓存/SReclaimable | 看 MemAvailable 与回收速率 |

## 版本注记

- 基线 7.1-rc4 精读；生产 **env-A = 3.10.0-1160**（CentOS 7）：无 oom_reaper
  （4.5+）、get_scan_count 是旧模型、无 PSI 内存项（4.20+）、THP 有但
  defrag 行为老——水位线/kswapd/OOM 打分框架跨版本成立
- oom_score_adj 自 2.6.36；memcg 独立 swappiness 需 memcg 开启

## 来源

- 内核源码基线 `<linux-7.1-rc4-src>`（7.1-rc4）精读（2026-10-25）：
- 二次补读（2026-10-27，NUMA 节）：drivers/base/node.c:572-582
  （node_read_numastat 六计数器渲染）、mm/page_alloc.c:3193-3198
  （NUMA_HIT/MISS/FOREIGN 分配计数）、include/linux/vmstat.h:240-248
  （__count_numa_event）；env-M 实例 awrrpt NUMA 表实测（Node0 86.15% vs
  Node1 99.60% Local Ratio）
  fs/proc/meminfo.c:56-62,108-109、include/linux/mmzone.h:796-799、
  mm/page_alloc.c:268,6441、mm/vmscan.c:201,244-248,2388-2420,7108,7438,7638、
  mm/oom_kill.c:58,191-199,339,362,413-422,508-511、
  include/uapi/linux/oom.h:6-19、mm/workingset.c:491,538,546、
  mm/huge_memory.c:59；补锚：mm/memcontrol-v1.c:1988（memory.swappiness
  write 侧，v1 专属）
- 生产内核版本实测：env-A uname -r（2026-10-25）
- 《Linux 性能优化实战》内存篇作主题框架参照（口径以源码为准）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：双缓存→`buffer-manager.md`、
脏页回写→`io-storage.md`、fsync 闸门→`commit-sync-events.md`、OOM 联动→
`log-analysis.md`、53200 分层→`operator-memory.md`、配比核算→`parameters.md`。
