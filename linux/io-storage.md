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

## 块层：multiqueue 与调度器（7.1-rc4 行级精读，2026-11-19）

**两级队列结构**：每 CPU 一个软件队列（`blk_mq_ctx`）→ 若干硬件队列
（`blk_mq_hw_ctx`，数量=min(设备 hw 队列, nr_hw_queues)；NVMe 多队列常见
读/写/POLL 分型 hctx）。请求进队时**按当前 CPU 的 ctx 定 hctx**：
`__blk_mq_alloc_requests`（blk-mq.c:537）里 `blk_mq_get_ctx(q)` +
`blk_mq_map_queue(cmd_flags, ctx)`（:551-553）——**请求类型（读/写/轮询）
决定走哪个 hctx**，不是简单 CPU 哈希。

**提交→tag→派发主干（判读 await 的排队段在哪儿产生）**：

| 步骤 | 锚点 | 机制/判读 |
|---|---|---|
| bio 进块层 | blk_mq_submit_bio（blk-mq.c:3141）→ blk_mq_get_new_requests（:3046） | plug（批量合并）在这里起作用 |
| 请求+tag 分配 | __blk_mq_alloc_requests（:537） | plug 场景 nr_tags>1 走**批量 tag**（__blk_mq_alloc_requests_batch :464，一次拿一批） |
| tag 池 | blk_mq_get_tag（blk-mq-tag.c:137）→ 快路径 __blk_mq_get_tag（:109，sbitmap） | **tag 耗尽**：NOWAIT 直接失败；普通路径先 kick 在途 IO 再睡等 sbitmap（完成端唤醒）——queue 深度打满的 await 涨主要涨在这段 |
| 分配失败重试 | blk-mq.c:576-587 | tag=NO_TAG 且非 NOWAIT → **msleep(3) 后重选 hctx 重试**（CPU 热插拔迁移场景）；hctx 已不在线则换队 |
| 派发 | blk_mq_run_hw_queue（:2352） | **本 CPU 不在 hctx->cpumask → 异步延迟派发**（:2376-2378）；本 CPU 才同步跑 |
| 调度器派发 | __blk_mq_sched_dispatch_requests（blk-mq-sched.c:268） | 先清 hctx->dispatch 残留（公平），再向调度器要请求；★"低 queue_depth 时只派一部分、剩余留在调度器里继续可合并排序"（:282-291 注释）——**浅队列设备的合并收益设计** |
| 交驱动 | blk_mq_dispatch_rq_list（blk-mq.c:2116） | 逐个交驱动；budget/流控（rq_qos done 回调在完成路径） |

**调度器**（block/：none / mq-deadline / kyber / bfq）：
- ★调度器选择是**设备队列拓扑决定的，不是"DB 调优项"**（源码定论，
  elevator_set_default，elevator.c:729）：**多 hw 队列（NVMe 类）默认 none**——
  `BLK_MQ_F_NO_SCHED_BY_DEFAULT` 直接跳过（:740，blk-mq.h:709），或因多队列
  不落单队列分支（:744-747 注释原话：单队列设备才默认 mq-deadline，多队列
  或不可用则 none）。**给 NVMe 上 mq-deadline/bfq 是 HDD 时代残留的负优化**
  （把并行提交强行过一道排序软件层）；mq-deadline 只对单 hw 队列设备
  （SATA/SCSI 路径、部分云盘 virtio）有防饥饿意义。★`elevator=` 内核启动参数
  已死（elevator.c:888-895 只打印 warning 不生效）——只能 sysfs 按设备设；
  bfq 面向交互公平，吞吐型 DB 不合适；
- ★**在线切换 = 冻结窗口**：elevator_switch（elevator.c:562）先
  `blk_mq_quiesce_queue(q)`（elevator.c:576）冻结整队列再换调度器实例——**生产在线
  echo 切 scheduler 会造成短暂全队列停顿**，放维护窗口做；切到 none 时
  `nr_requests = queue_depth`（:591，无调度器时队列深度即 tag 深度）。
- queue_depth 语义：每 hctx 的 **tag 数**（driver_tags），不是"待处理 IO 上限"
  的全部——nr_requests 只在有调度器时另有账。

**CPU 离线/cpuset 收缩不断 IO**：blk_mq_hctx_notify_dead（blk-mq.c:3844）——
死核的 ctx 残留请求**搬进 hctx->dispatch 立即重派**（:3865-3870），丢核不丢
请求；在线核继续服务。判读：缩容/热插拔瞬间 IO 抖一下是重映射，不是盘病。

**wbt/cgroup 与本层的次序**：throtl 在 submit_bio_noacct（blk-core.c:882）
先卡、rq_qos（wbt/iocost）在 blk_mq_submit_bio 内 :3063/:3110——详见
cgroup.md「io 控制器」节（两层叠加判读）。

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

## 现代 SSD/NVMe 判读域（HDD 经验失效清单，2026-11-19 立）

> HDD 时代的判读直觉在闪存上大面积失效。本节按"域"列失效点与替代判读；
> 源码锚点 = NVMe 驱动与块层（7.1-rc4）。**rot 标志本身不可信**（见域 I），
> 判介质先看型号/传输（nvme list / lsblk -d -o RO,TRAN），别拿 rotational 分档。

**域 A｜调度器与排序——已死**：见块层节（多队列默认 none，elevator.c:744-747）。
排序（寻道优化）在闪存无意义；合并（plug merge）仍活——减少的是请求开销不是寻道。

**域 B｜延迟量级与形态**：
- 量级：NVMe 单次 4K 随机读 **50-200µs**、写 20-100µs（SLC cache 内）；SATA SSD
  ~100-500µs；HDD 寻道+旋转 5-15ms。**await 判读三档：NVMe>1ms / SATA SSD>5ms /
  HDD>20ms**（判读速查表已改三档）。
- 形态：**均值正常 + 尾部爆炸是闪存的病征**——SLC 写缓存写满（write cliff）、
  GC、FTL 映射表冷启动都会打 p99/p999 尖刺而 await 均值不动。iostat 均值判不出，
  用 `iostat -x 1` 连续采样看 await 尖刺列 + 应用侧 p99 互证（无 eBPF 时）。
- 消费级盘无 PLP 时 fsync 延迟可到几十 ms 且随脏量波动（域 E）。

**域 E｜写缓存与掉电保护（fsync 延迟的分水岭）**：
- NVMe 的 volatile write cache 由**控制器 VWC 位申报**（core.c:2428-2431：
  `vwc & NVME_CTRL_VWC_PRESENT` → 挂 BLK_FEAT_WRITE_CACHE|FUA），sysfs 的
  wcache 列即此位；企业盘带 PLP（电容）= 写缓存安全且 fsync 快；消费盘
  FTL 全刷 = fsync 抖动大。
- 判读：`cat /sys/block/nvme*/queue/write_cache`（write back / write through）；
  DB 丢数据风险线在**无 PLP + write back** 组合——fsync 语义靠 FUA/flush 走
  完整链路，延迟形态见域 B。
- SATA `hdparm -W0/1` 同域；virtio 云盘的 wcache 由宿主实现，客户机不可见。

**域 F｜TRIM/Deallocate 与衰减**：不做 TRIM → 盘内有效页比例高 → GC 搬运量
大 → 写放大（WAF）升 → 吞吐/延迟衰减。ext4/xfs 默认 discard=async 或
periodic fstrim；判读衰减看 smart-log 的 data_units_written 对比应用写入量
（WAF≈倍数）。NVMe 1.0d 之后的 DEAC/通杀指令细节不展开，判读不依赖。

**域 G｜寿命与健康（nvme smart-log，判读主数据源）**：
`nvme smart-log /dev/nvmeX`（结构 = include/linux/nvme.h:742
`struct nvme_smart_log`，512B，core.c:5394 有 BUILD_BUG_ON 校验）：

| 字段（nvme.h 行号） | 判读 |
|---|---|
| critical_warning(:743) ≠0 | 立刻处理：任一置位都是硬件级告警（备件/温度/介质/易失备份失效） |
| temperature(:744) | 复合温度 Kelvin（-273 得 °C）；逼近 critical 温度 = 降速前兆（域 H） |
| avail_spare(:745) < spare_thresh(:746) | 备用块低于阈值 = 寿命尽头的硬信号 |
| percent_used(:747) | 生命用量估 >100% 不是立刻死，但与 media_errors 同看要警惕 |
| media_errors(:759) ≠0 且涨 | 介质错误在积累——换盘信号（个别坏块可被重映射掩盖，涨势才关键） |
| data_units_written(:751) ×512KB | 判 WAF 与寿命消耗速率的分母 |

**域 H｜温度降速（throttle）**：NVMe 过热自动降速是生产真实问题（高队列深度
持续写 + 机箱风道差）。判读链：smart-log temperature 逼近阈值 → await 整体抬升
（非尖刺是台阶）→ `cat /sys/class/nvme/nvme*/hwmon*/temp*_critical` 看阈值。
形态区分：GC=写时尖刺，温度=读写同时台阶式抬升。

**域 I｜rotational 标志不可信（源码坐实）**：NVMe 的 rot 位是**盘固件自己在
identify namespace NSFEAT bit5 申报的**（`info->is_rotational = id->nsfeat &
NVME_NS_ROTATIONAL`，core.c:1681；:2433-2434 据此挂 BLK_FEAT_ROTATIONAL）——
虚拟盘/云盘固件乱报则 /sys 的 rotational=1（SSD 被标成 HDD），任何"按 rot
自动选分档/调度策略"的工具在虚拟环境必错。判介质：`nvme list` 型号 /
`lsblk -d -o NAME,ROTA,TRAN,MODEL`，TRAN=nvme/sata 直接定域。

**域 J｜队列深度量级**：NVMe 每核一对 SQ/CQ（中断向量亲和），queue_depth
默认 1024 级；IOPS 量级 NVMe 10^5~10^6/s vs HDD 10^2/s——绝对 IOPS 判读先对
量级（IOPS 300/s 对 NVMe 是闲置、对 HDD 是打满，别用同一把尺）。

**企业存储注记（无环境，仅存目）**：NVMe-oF/multipath/RAID 卡 BBU 属另一层
（fabric/控制器冗余），判读走存储厂商工具，本域不展开。



| 现象 | 命令 | 机制指向 |
|---|---|---|
| await 高（三档介质：NVMe>1 / SATA·SAS SSD>5 / HDD>20 ms） | `iostat -x 1`；`cat /sys/block/*/queue/nr_requests` | 真设备瓶颈；对照 fio 基线，盘速/调度器；**NVMe 正常延迟 50-200µs，await 破 1ms 就要查**（见下节 SSD/NVMe 域） |
| await 周期尖峰对齐库 checkpoint | AWR Pagewriter/Checkpoint 段 + iostat 时间轴 | 双层刷脏互踩（写路径闸门表） |
| 物理读高且慢 | iostat r_await + read_ahead_kb | 预读不足或随机化（`parameters.md` 组 2） |
| 写延迟高但盘闲 | vmstat w/b 列 + BdiWriteback | 可能卡在 OS 限流或 fsync 语义而非盘（`commit-sync-events.md`） |
| 库报 fsync 慢 | diskstats flush 列 + 日志设备 iostat | 设备 flush 队列；电池保护 cache 掉线常是元凶 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 拿 %util 判饱和/慢盘 | **util 永不作判读标准（2026-10-28 用户令：这参数没啥价值）**；multiqueue 并行下 io_ticks 不分并行度 | 只看 await（介质分档）+ 绝对 IOPS；avgqu-sz 辅助 |
| **给 NVMe 上 mq-deadline/bfq 调优** | HDD 时代残留：多队列设备默认 none（elevator.c:740/:744-747），软件排序层在闪存上是纯开销 | NVMe 保持 none；只有单队列设备才考虑 mq-deadline |
| **拿 await 均值判闪存健康** | SLC cliff/GC 打的是 p99 尖刺，均值不动（域 B/F） | 连续采样看尖刺列 + smart-log data_units_written 对 WAF |
| **信 /sys 的 rotational 判介质** | rot 位由盘固件申报（core.c:1681），虚拟盘/云盘错标常见（域 I） | `nvme list`/`lsblk -o TRAN,MODEL` 定介质 |
| **消费级 SSD 上期望稳定 fsync** | 无 PLP，FTL 全刷随脏量抖到几十 ms（域 E） | 企业盘（PLP）或接受抖动设计；wcache 位只反映 VWC 申报 |
| 看 svctm 定位设备慢 | 内核早已不提供真实服务时间 | 忽略该列 |
| fio 默认参数测库盘 | buffered+iodepth=1 测的是页缓存 | direct=1+明确 bs/iodepth |
| vm.dirty_* 全默认不核 | OS 回写与库刷脏双层互踩（page-writeback.c 阈值表） | 收敛 OS 侧（组 3） |
| nobarrier 提速 | 关 flush 屏障=掉电数据损坏风险（ext4 super.c:1909 默认开的原因） | 数据库绝对禁止 |
| 把 OS 页缓存命中当库命中 | 两层缓存两个命中率（buffer-manager.md） | 分层判读（kernel-data-path.md） |
| 拿"CPU 满"解释 hung task 报告 | 检测器只报 D 且整窗零切换，等 CPU 是 R 状态；CFS 满载压不死完成链（硬中断→就地软中断→ksoftirqd 三层，blk-softirq.c） | 栈在 wbt_wait = 设备写完成停滞，查 iostat w_await + dmesg；仅 si 风暴/RT 霸核是 CPU 侧真实通道（mpstat si 列 + softnet_stat） |

## 版本注记

- 基线 7.1-rc4 精读（2026-11-19 增补：blk-mq 行级 + 现代 SSD/NVMe 判读域，
  NVMe 驱动源码 core.c/pci.c/nvme.h + include/linux/nvme.h 锚点）；生产
  **env-A = 3.10.0-1160**（CentOS 7）：**旧请求层+
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
- **2026-11-19 行级增补**（文件级镜像 `/home/xiang/kernel-src/linux-7.1-rc4-mirror`）：
  block/blk-mq.c:537,576,2352,3844 + blk-mq-tag.c:109,137 + blk-mq-sched.c:268 +
  elevator.c:562,576,729,740,744-747,888-895；NVMe 侧
  drivers/nvme/host/core.c:1681,2428-2434,5394、
  include/linux/nvme.h:742-759（smart_log 结构）、include/linux/blk-mq.h:709
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
