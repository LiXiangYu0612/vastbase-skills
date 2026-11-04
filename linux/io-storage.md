# Linux IO 存储层（页缓存 / 回写 / 块层 / iostat 口径）

## 概述

> 服务分类：linux（本分支）、performance（AWR IO 段/物理读/刷脏判读）、
> troubleshooting（IO 慢现场）。数据库的每一份数据最终都要经过
> "VFS → 页缓存 → 回写 → 块层 → 设备"这条链；判读时先定位现象落在哪一层。

## 分层总图（判读定位用）

```
 read():  syscall → VFS → 页缓存命中? ─ 是 → 拷给用户
                                 └ 否 → 预读(readahead) → 块层 → 设备 → 缓存填充
 write(): syscall → VFS → 拷入页缓存 → 标脏 → 返回(快!)
          异步: 回写线程按 阈值/周期/过期 闸门 下发 → 块层(合并/调度) → 设备
 fsync(): 强制把[该文件]脏页+journal 刷到稳定存储 → 数据库的持久化闸门
```

库侧对应：shared_buffers 命中/淘汰（`buffer-manager.md`）、后台刷脏
（`flush-checkpoint.md`）、提交 fsync（`commit-sync-events.md`）——**库和 OS
是两层缓存+两层刷脏**，慢在哪一层要分清（`kernel-data-path.md` 通路定位）。

## 读路径与预读

- 顺序读由预读放大：窗口由 backing device 的 `ra_pages` 决定
  （mm/readahead.c:144,546），用户侧旋钮 = 块设备 sysfs
  `queue/read_ahead_kb`（block/blk-sysfs.c:181，默认 128KB）
- 判读：顺扫吞吐低于盘速时核 read_ahead_kb（`parameters.md` 组 2）；
  随机读则预读无用，反而是浪费（读放大）
- inode/dentry 缓存挂在路径解析上，回收计入 SReclaimable（`memory.md`）——
  海量小文件的场景才需要关注，数据库数据文件场景影响小

## 写路径：三个闸门与双层缓冲（DB 判读核心）

mm/page-writeback.c（深读）：

| 闸门 | 默认值 | 行为 |
|---|---|---|
| dirty_background_ratio | **10%**（:75） | 脏页超阈 → **唤醒后台回写**，分配不阻塞 |
| dirty_ratio | **20%**（:92） | 脏页超阈 → **产生脏页的进程自己被限流**（balance_dirty_pages，freerun 释放线 :1067/:1336；global_dirty_limits :413-420 算双阈） |
| dirty_writeback_interval | **5s**（:103，单位 centisec） | 周期性回写扫描（wb_do_writeback，fs/fs-writeback.c:2380） |
| dirty_expire_interval | **30s**（:110） | 脏页龄超此值 → 下轮周期必写（不过 30s 的脏页不存在） |

- 回写实例按 backing device 分（/proc 里 `BdiWriteback` 可见各设备脏页量，
  mm/backing-dev.c:126）；pdflush 时代早已结束（2.6.32+ per-BDI）
- **DB 联动（双层缓冲互踩）**：VastBase 自己有 pagewriter/双写刷脏节奏，OS 层
  再叠一套 10%/20% 阈值回写 → 库刷脏高峰撞 OS 限流时，表现为刷脏 SQL/进程
  在 OS 层被 throttle（CPU 看着闲、IO 等待高）。实践惯例（非源码结论）：
  数据库机 vm.dirty_background_bytes/dirty_bytes 给小值（如 256M/1G）收敛
  OS 侧突发，把刷脏主导权留给库（`parameters.md` 组 3；`flush-checkpoint.md`
  库侧 behind 公式）

## fsync 与直接 IO：库的持久化语义

- **O_DIRECT**（fs/iomap/direct-io.c，`iomap_dio_rw` :661）：绕页缓存直达
  块层；**O_[D]SYNC 写无论缓冲与否都等设备确认**（direct-io.c:661 注释）
- 数据库 redo/数据文件持久化最终依赖 **fsync/fdatasync**（`commit-sync-events.md`
  的 fsync 语义与等待事件）；fsync 成功 = 该文件脏页+journal 落稳定存储
- 文件系统侧（ext4 为例，fs/ext4/super.c）：journal data 模式默认 **ordered**
  （:1726 数据写序保证）；**barrier 默认开**（:1909 MOPT_SET）——
  `nobarrier` 挂载会关掉 flush 指令屏障，**数据库场景绝对禁止**（掉电页撕裂/
  库损坏，比慢更贵）
- 回写限流 wbt（block/blk-wbt.c，rq_qos 框架）：只对**缓冲写**设延迟目标，
  直接 IO 不受它管——SSD 深队列设备上缓冲写被 wbt 压住属正常，别误判为盘慢

## wbt_wait 深睡与 hung task 报告形态（"blocked for more than 120 seconds"）

> Linux v4.19 源码精读（2026-11-02，现场机 Kylin 4.19.90 实战）。
> 入口症状：dmesg `INFO: task <name> blocked for more than 120 seconds`，
> 栈含 `wbt_wait → io_schedule`，线程状态 D。

### 实测判例（2026-11-01 事件，OSWbb 10s 快照全程对账）

- **死锁画像（w/s=0 形态）**：写提交全冻后 iostat 三特征 =
  `w/s 精确=0`（连 WAL/fsync 都是 0）+ `aqu≈0` + **读照常完成**
  （r_await 亚毫秒）+ 盘 idle——写从未到达设备，不是盘慢。配合
  vmstat `b` 恒定不变（本案 b=7 整整 53 分钟）即可定性
- **b 的名单闭环**：hung task dump 把 D>120s 任务全列出，本案 7 个 =
  pagewriter×4（`sync_file_range` R10=2=SYNC_FILE_RANGE_WRITE、单页 8KB）
  + WALwriter（`fdatasync`）+ kworker(xfs_log_worker→xfs_log_force→
  xlog_sync，XFS 日志本身过不了闸) + kworker(wb_workfn flush-252:0)
  ——四条独立路径同一死点 `rq_qos_throttle→wbt_wait`（闸挂 vdb 队列）
- **闸前收紧痕迹**：断崖前 dm-0 aqu 15-36 而 vdb aqu≈1——提交者已间歇
  睡在 wbt_wait（嵌在 dm 克隆→generic_make_request 嵌套路径），bio
  挂在 dm 层计在途。判读时 dm 高/底层低≠dm 慢，是限流闸在底层
- **雪断崖因果链（实测序）**：读洪峰（475MB/s）灌满 page cache →
  free 钉死 0.2G → kswapd+直接回收把 bo 推上 118MB/s → vdb 写延迟超
  2ms 目标 → wbt 收紧 → 唤醒链断裂 → 全冻；**同一时刻** us→98
  （vastbase 31.4/32 核用户态执行+会话堆积 r 69→187）——CPU 满与内存
  地板是伴生信号，"os 监控 CPU 先满"是表象，第一张多米诺是洪峰+限流
- **自愈失败机制**：无超时兜底 + 唯一唤醒=被跟踪写完成；在途数卡在
  限额上而设备零在途 ⇒ 零完成⇒零唤醒⇒永久死锁（53 分钟不自愈实证）。
  读正常+整窗 steal≈0 ⇒ 宿主通道排除，死锁在现场机内核
- **代码级三证据（openEuler 4.19.90 源码，2026-11-02 拉取）**：
  ①读对写令牌机合法隐形——wbt_wait 入口 `!(flags & WBT_TRACKED)`
  直接 return（读连令牌都不领），wbt_done 里读完成仅 `wb_timestamp`
  进延迟窗口，不触碰 inflight/等待队列 ⇒ 读再多次也救不醒睡死的写者；
  ②公平性排队——`if (!has_sleeper && rq_wait_inc_below(...)) return`
  fast path 仅对"队列无睡眠者"开放，一个等待者睡死 ⇒ 后来者全部入队
  （解释 7 任务逐个排进去）；③自持循环断裂——唤醒事件=缓冲写完成、
  写提交=来自等待者自身，燃料与火花同源，丢一次唤醒即永久自锁
  （"写停是果不是因"：竞态击发于写流最密时刻，一拍后写才停）。
  另：wbt_rqw_done 唤醒条件苛刻（wc 设备压到 limit=0 才 wake）
- **上游对账结论（2026-11-02，torvalds/linux 提交记录）**：4.19 后
  `blk-rq-qos.c` 有同族四连修（均 2019-07-16）：
  **`545fbd0775ba` "fix missed wake-ups in rq_qos_throttle"**（原文自述
  "hang in production with WBT, only one waiter in the throttle path and
  **no outstanding IO**... never be woken up because nobody is doing IO"——
  与实测签名逐项吻合：等待者睡死+令牌空闲+设备零在途+零完成零唤醒）、
  `64e7ea875ef6`（has_sleepers 虚假唤醒重置）、`d14a9b389a86`
  （虚假唤醒后忘设 TASK_UNINTERRUPTIBLE→busy wait）、`ac38297f7038`
  （got_token 缺内存屏障）。时间线：v4.19(2018-10)→2018-12 重构进
  rq_qos_wait→2019-07 才修 missed wakeup 族；v4.19 原版 wbt_wait
  （wait_index 双队列乒乓）同样只有"写完成→唤醒"单条路径无超时。
  **判读含义**：任一 4.19 血统内核（含厂商回合树）都可能带此缺陷族；
  `wbt_wait+0x1a4/0x370` 的大函数体积指向原版实现
- **缺陷族完整版图（2026-11-02 补全）**：除 2019-07 四连修外还有两个
  后发现的 hang 模式——上游 `11c7aa0ddea8`（2021-06，"missed wake-ups
  try two"，原文自认 545fbd0775ba 修复不完整，三 CPU 竞态
  waiter1/waiter2/waker；stable 回合 8cc58a6e2c394 进 5.10.51）与
  openEuler 自研 `02512915b5c2`（2021-10-20，"fix IO hang due to
  negative inflight counter"，bugzilla 182135，fsstress 实锤 vmcore
  inflight=-1，唤醒算术永久错乱）。openEuler-1.0-LTS 回合时间：
  四修=2019-12-27（f42073eeb92f/3888f36e36d2/5e30d9a40391/0e906fa7b87f），
  try two=2021-11-22（fbf4e285fb0c），negative inflight=2021-10-20。
  **判读含义**：带四修的内核仍可 hang（try two 竞态 + inflight 下溢）；
  Kylin v2101（~2021-01 构建）时间线上必无后两修 ⇒ 这两个是更新后的
  主嫌疑；定案=现场机 `rpm -q --changelog kernel-* | grep -iE
  "wbt|rq-qos|wake|inflight"` 对账回合记录
- **厂商回合对账（2026-11-02 实拉源码）**：openEuler-1.0-LTS（4.19.90
  线，Kylin V10 血统近似基线）的 `__wbt_wait` **已回合 2019-07 修复组**：
  `has_sleeper = !__prepare_to_wait_exclusive(...)`（用返回值）、循环内
  `has_sleeper = true; set_current_state(TASK_UNINTERRUPTIBLE);`、
  `smp_rmb()` 补屏障——与纯 v4.19 原版（`has_sleeper = false`、无屏障）
  逐行可 diff（两版源码存档 ~/windmc/blk-wbt_*.c）。**判读含义**：
  openEuler 4.19 系大概率不受此缺陷影响；Kylin V10（.ky10 自维护树）
  是否回合**待查证**（比对源码 __wbt_wait 或反汇编 vmlinux）——若麒麟
  已回合，则本签名需另找机理（flush 记账/在途记账泄漏），客户表述
  降级为"高度疑似待验证"
- **未定论点（收窄后）**：①Kylin V10 是否回合修复（源码/反汇编定）
  ②若已回合，备选机理=FLUSH/FUA 记账不对称或 wbt 在途记账泄漏。
  定案动作=查厂商源码 block/blk-wbt.c 的 __wbt_wait；复发时
  `echo 0 > /sys/block/<dev>/queue/wbt_lat_usec` 解冻即实锤

- **wbt 限额动力学**（blk-wbt.c）：默认目标延迟 SSD 2ms / HDD 75ms
  （wbt_default_latency）；blk_stat 每 100ms 窗口统计完成延迟，超目标 →
  scale_step++ → max_depth 减半（最低 1），wb_normal=(max+1)/2、
  wb_background=(max+3)/4
- **阻塞点**：提交缓冲写时在途数 ≥ 限额 → `__wbt_wait` 以
  **TASK_UNINTERRUPTIBLE**（prepare_to_wait_exclusive）+ io_schedule 睡眠；
  **唯一唤醒路径** = 被跟踪写完成 → wbt_done → wbt_rqw_done → inflight
  降到限额下 → wake_up_all。**无超时兜底**
- **hung task 判据**（kernel/hung_task.c）：khungtaskd 周期扫描，报告条件 =
  state==D **且** nvcsw+nivcsw 整窗未变（一次都没被调度过）。所以
  "blocked 120s + 栈在 wbt_wait" = **该设备缓冲写完成停滞了整整 120s**
  （或限额收到 1、单 IO 卡死、后队全睡）——设备侧证据，不是 CPU 证据
- **CPU 满不产生此报告的原理**：①等 CPU 是 R 状态，检测器只看 D（醒后
  排队也会重置切换计数）；②D 任务不在运行队列，不需要 CPU；③完成唤醒链
  （blk-softirq.c）三层抗压——硬中断投递抢占任意负载；中断返回就地跑软中断
  （2ms/10 次预算，非调度决策）；溢出才给 ksoftirqd（CFS nice 0，调度延迟
  保证必跑）。**唯一真实通道** = si 风暴/RT 霸核把 BLOCK_SOFTIRQ+ksoftirqd
  同时饿死 120s+，且必有伴随症状（多写者同报/RCU stall/softnet 丢包）；
  反证：khungtaskd 自己跑出来打了报告
- **取证**：报告窗 iostat -x w_await + 绝对 IOPS（util 弃用）；dmesg 同窗
  SCSI timeout/reset/multipath 切换；`/sys/block/<dev>/queue/wbt_lat_usec`
- **虚拟机判读**：guest 内 wbt hang 有两条宿主侧通道——①宿主 CPU 超售/
  配额：vCPU 不被调度 ⇒ 中断注入与完成软中断全部停摆 ⇒ 零唤醒 ⇒ D 卡
  成立（此时"CPU 导致 hang"成立，**主语是宿主**）；判据 = 该窗 steal>0
  （/proc/stat / AWR OS 段 Steal 列）。**us 与 steal 同 CPU 记账互斥
  （us+st≤100）**：整窗分钟级 us≈98% 且 steal≈0 ⇒ vCPU 一直在跑，宿主
  饿死通道基本排除；但**突发稀释**陷阱——120s 级 steal 突发可藏在粗粒度
  均值里，取报告窗前后 15 分钟最细粒度序列找"us 下跌+steal/wa 跳变"的
  分钟。②虚拟盘宿主侧路径（宿主存储/iothread/qcow2 链/共享后端慢）⇒
  guest w_await 高而 steal=0。**VMware 陷阱：guest steal 恒 0，须查宿主
  esxtop %RDY**；KVM 系（Fusion/CAS/ZStack 等）steal 口径有效。四象限：
  steal 高→宿主超售（找宿主/迁移）；steal=0+w_await 高→宿主存储路径；
  皆正常→查 dmesg guest 时钟跳变/soft lockup。**时序判读（2026-11-02）**：
  "CPU 先满"支持负载驱动链（us 满→脏页/写洪峰→虚拟盘宿主侧拥塞→wbt
  收紧→hang），同时否掉反向链（hang→淘汰锁争用自旋→CPU 满）；CPU 是
  负载的影子不是机制成因——扩 CPU 不救 IO，定案靠三线对齐（us 满/
  w_await 爬升/写吞吐）+ steal 曲线找突发
- **处置**：治本 = 修设备写延迟；临时 = `echo 0 > .../queue/wbt_lat_usec`
  关 wbt（代价 = 失去缓冲写延迟保护，洪峰直打设备）；
  `hung_task_timeout_secs=0` 仅消音；**hung_task_warnings 默认 10 次后
  静默，"后来不报了"≠自愈**（实测判例：env-W 案 07:08:56 报 7 条 +
  07:10:57 报 3 条=恰好 10 条，第二批中途截断只剩前 3 个任务、
  之后永久静音，而病程持续到 2h39m 后硬重启——期间内核零消息、
  用户态 cron/qemu-ga 照常=半死形态）
- 库侧连锁：pagewriter 停在 wbt_wait → 脏页 behind 涨 → checkpoint 停滞/
  前台刷脏（flush-checkpoint.md behind 三形态）。VastBase pagewriter 经
  sync_file_range(SYNC_FILE_RANGE_WRITE) 踢写回为定制路径——OG 6.0 基线
  pg_flush_data 仅有 copydir/段页式两处调用（fd.cpp:437-475，注释自认
  "this call might block if too much dirty data exists in the range"）；
  SYNC_FILE_RANGE_WRITE 的"异步"只免等完成，bio 提交仍可睡在 wbt

## 块层：multiqueue 与调度器

- 现代 IO 走 blk-mq（软件队列/CPU → 硬件队列），合并（merge）与调度器
  （block/：**none / mq-deadline / kyber / bfq**）在请求进入硬件队列前
- 判读：`cat /sys/block/<dev>/queue/scheduler`；数据库常用 **none 或
  mq-deadline**（降低调度延迟）；bfq 面向交互公平，吞吐型 DB 不合适
- io_uring（io_uring/ 目录，5.1+）目前 VastBase 未用，仅备版本注记

## iostat 口径（/proc/diskstats，block/genhd.c 渲染）

- `await` = (读+写总耗时) / IO 数，**含队列排队时间**——它是"每个 IO 从进队
  到完成"的延迟，不是设备服务时间；`r_await/w_await` 分列更可判
- `%util` = io_ticks/采样窗——**multiqueue/SSD 并行设备上并行度>1，
  %util≈100% 不等于饱和**（老口径是单队列时代的）；这正是不把 util 当
  判读标准的机制根因（2026-10-28 用户令：util 永不作判读标准）；饱和判据用
  await（介质分档）+ 绝对 IOPS，队列深度 avgqu-sz 辅助
- `svctm` 列已无意义（现代内核不提供每 IO 服务时间），忽略
- 新内核 diskstats 含 discard/flush 统计列——**flush 列是 fsync 类负载的
  设备侧对账证据**（库 LogFileSync 等待高 → 设备 flush_ios/flush_ticks 同涨
  即实锤，`wait-events.md` 溯源联动）
- 基准工具 fio：`direct=1`（绕页缓存）+ 指定 bs/iodepth/numjobs 才是盘的
  真实能力；默认参数（buffered, bs=4k, iodepth=1）测出来的是页缓存+单队列，
  没有意义

## 判读速查（现象 → 命令 → 机制）

| 现象 | 命令 | 机制指向 |
|---|---|---|
| await 高（分介质：SSD>5 / HDD>20） | `iostat -x 1`；`cat /sys/block/*/queue/nr_requests` | 真设备瓶颈；对照 fio 基线，盘速/调度器 |
| await 周期尖峰对齐库 checkpoint | AWR Pagewriter/Checkpoint 段 + iostat 时间轴 | 双层刷脏互踩（写路径闸门表） |
| 物理读高且慢 | iostat r_await + read_ahead_kb | 预读不足或随机化（`parameters.md` 组 2） |
| 写延迟高但盘闲 | vmstat w/b 列 + BdiWriteback | 可能卡在 OS 限流或 fsync 语义而非盘（`commit-sync-events.md`） |
| 库报 fsync 慢 | diskstats flush 列 + 日志设备 iostat | 设备 flush 队列；电池保护 cache 掉线常是元凶 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 拿 %util 判饱和/慢盘 | **util 永不作判读标准（2026-10-28 用户令：这参数没啥价值）**；multiqueue 并行下 io_ticks 不分并行度 | 只看 await（介质分档）+ 绝对 IOPS；avgqu-sz 辅助 |
| 看 svctm 定位设备慢 | 内核早已不提供真实服务时间 | 忽略该列 |
| fio 默认参数测库盘 | buffered+iodepth=1 测的是页缓存 | direct=1+明确 bs/iodepth |
| vm.dirty_* 全默认不核 | OS 回写与库刷脏双层互踩（page-writeback.c 阈值表） | 收敛 OS 侧（组 3） |
| nobarrier 提速 | 关 flush 屏障=掉电数据损坏风险（ext4 super.c:1909 默认开的原因） | 数据库绝对禁止 |
| 把 OS 页缓存命中当库命中 | 两层缓存两个命中率（buffer-manager.md） | 分层判读（kernel-data-path.md） |
| 拿"CPU 满"解释 hung task 报告 | 检测器只报 D 且整窗零切换，等 CPU 是 R 状态；CFS 满载压不死完成链（硬中断→就地软中断→ksoftirqd 三层，blk-softirq.c） | 栈在 wbt_wait = 设备写完成停滞，查 iostat w_await + dmesg；仅 si 风暴/RT 霸核是 CPU 侧真实通道（mpstat si 列 + softnet_stat） |

## 版本注记

- 基线 7.1-rc4 精读；生产 **env-A = 3.10.0-1160**（CentOS 7）：**旧请求层+
  CFQ 默认**（blk-mq 3.13+，CentOS 仅 scsi_mq 可选）、**无 wbt**（4.10+）、
  无 io_uring；page-writeback 阈值/周期/过期四闸门跨版本成立
- 现场机档 = Kylin 4.19.90-24.4.v2101（openEuler 4.19 系，2026-11-02）：
  wbt 默认启用（mq 调度下；bfq 会禁用）；hung task 判据与 120s 默认阈值同
  基线
- %util 并行陷阱主要影响 SSD/NVMe；但机械盘也不拿 util 判（2026-10-28 用户令：任何介质都弃用）
- per-BDI 回写 2.6.32+ 全档成立，pdflush 表述已过时（《实战》笔记旧口径之一）

## 来源

- 内核源码基线 `<linux-7.1-rc4-src>`（7.1-rc4）精读（2026-10-25）：
  mm/page-writeback.c:75,92,103,110,413-420,813,1067,1336、
  fs/fs-writeback.c:2380,2428、mm/backing-dev.c:126、mm/readahead.c:144,546、
  block/blk-sysfs.c:181、block/genhd.c（diskstats）、fs/iomap/direct-io.c:661、
  fs/ext4/super.c:1726,1909、block/blk-wbt.c、block/ 调度器清单
- 生产内核版本实测：env-A uname -r（2026-10-25）
- Linux v4.19 kernel/hung_task.c、block/blk-wbt.c、block/blk-rq-qos.h 全文
  对读（2026-11-02，git.kernel.org raw 取源）；OG 6.0 基线
  gausskernel/storage/file/fd.cpp:437-475（pg_flush_data 全调用点全量提取）
- 《Linux 性能优化实战》IO 篇作主题框架参照（口径以源码为准）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：双缓存→`buffer-manager.md`、
双层刷脏→`flush-checkpoint.md`、fsync 语义→`commit-sync-events.md`、
落盘通路→`kernel-data-path.md`、介质/预读参数→`parameters.md`、
OS 内存回收→`memory.md`。

案例锚点：`../cases/case-env-C-awr-lowload-202610.md`（HDD 介质形态判定）
