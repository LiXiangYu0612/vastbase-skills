# fio 压测设计与结果判读（缓存链 size 设计 / 输出逐段读法）

## 概述

> 服务分类：linux（本分支）、performance（盘基线验收/IO 瓶颈定位）、
> troubleshooting（"盘到底行不行"取证）。本文件回答两件事：
> **怎么设计一次测到真盘能力的 fio**（size/引擎/时长按缓存链算），
> **拿到输出怎么判读**（逐字段 + "好到不真实"假高指纹表）。
> await 三档与 util 禁令的判读口径单源在 `io-storage.md`，本文件不复制数字。

## 判读前提：先知道自己测的是哪一层

fio 打下来的对象可能是：盘 → RAID 卡 → 多路径 dm → LVM dm → 文件系统文件，
**每一层的结果口径不同**。测前四步：

1. `lsblk` / `multipath -ll` 定拓扑：目标卷跨几个 PV、每个 LUN 几条路径
2. 阵列卡缓存模式问清（write back / write through，BBU/电容状态；
   storcli/arcconf 可查）——模式不同，写测试测的根本不是同一条路径
3. 按"缓存链 size 设计"定数据集
4. 只读测试可直接打块设备；写测试必须走隔离空间（见安全红线）

## IO 路径缓存链（size 设计的机制依据）

```
fio → syscall → 页缓存 ──direct=1 绕过──┐
              （buffered 测试=测页缓存，作废）
                ↓ 块层 → LVM/多路径 dm（纯转发，无缓存）
                ↓ RAID 卡 [读缓存 | 写缓存(write back↔through, BBU/电容保护)]
                ↓ HBA → 盘 [盘内 DRAM | SSD SLC cache / PLP | 二级 SSD tier | SMR CMR 区]
                ↓ 介质
```

关键机制点（为什么 direct=1 不等于测到盘）：

- **direct=1 只绕页缓存**。RAID 卡缓存、盘内缓存在 OS 视界之外，
  **任何 fio 参数都绕不过**——唯一手段是数据集压满 + 时长拉够，
  让缓存的进出达到稳态，测出来的是"缓存灌满之后"的介质能力
- **RAID 写缓存 write back**：IO 到卡即回报完成。数据集 ≤ 写缓存时整场测试
  的写都停在缓存里，测的是缓存带宽；缓存灌满开始回刷后，稳态吞吐才=盘介质。
  卡缓存无 BBU/电容保护时现场常被改成 write through（写直通，写延迟翻倍）——
  基线必须记下卡的当前模式，否则基线与生产路径不一致
- **RAID 读缓存**：小数据集 randread 会大量命中读缓存，数字与盘无关
- **盘内 DRAM cache**（HDD 64~512MB/盘，工程口径）：RAID 卷跨 N 盘合计约 N×，
  数据集几十 GB 以上时占比可忽略；几百 MB 的测试必中
- **SSD SLC cache**（消费级，10~200GB 动态，工程口径）：空盘测试全在 SLC 里，
  数字是"出厂神话"；SLC 写满后直写 TLC，可跌一个数量级（write cliff，
  形态判读见 `io-storage.md` 域 B）。企业盘 PLP 无此 cliff，但同样需预热到稳态
- **二级缓存/分层（HDD+SSD）**：阵列卡 SmartCache/FAST 类（OS 完全看不见）、
  主机侧 dm-cache/bcache/LVM cache（`lvs` 可见 cache-pool，`dmsetup ls` 可见）。
  tier 容量几十 GB~TB 级——**fio 数据集必须远大于 tier 容量**，否则随机读全落
  SSD 层，测出的是 SSD 不是 HDD；阵列侧 tier 只能靠大数据集+多次翻写把热数据
  挤出 tier。SMR 盘同理：CMR 缓存区写满后延迟暴涨，必须长压

## size 设计规则

铁律（工程口径，非源码推导）：**数据集 size ≥ 10 × 路径上最大一层缓存**。
按"最大"算不是求和——各层缓存不会叠加命中，盖过最大层后其余层自然覆盖。

| 层 | 典型容量（工程口径） | 数据集下限 |
|---|---|---|
| 页缓存 | =全部 RAM | direct=1 已绕，无需考虑 |
| RAID 卡缓存 | 4~64GB（看卡） | ≥ 卡缓存 10 倍 |
| 盘内 DRAM | 64~512MB/盘 | 被 RAID 层覆盖 |
| 消费级 SSD SLC cache | 10~200GB（动态） | precondition + 足够时长 |
| 二级 SSD tier | 50GB~1TB | ≥ 2× tier（最贵的一层） |
| 企业 NVMe（PLP） | 写缓存几 GB，无 cliff | 正常规则 |

配套三条：

- **写测试先 precondition**：`--rw=write` 全量翻写一遍再测——既灌满写缓存，
  也把 SSD 打进稳态。测空盘 = 测出厂态，不是稳态
- **runtime ≥ 60s 起步**（写测试/分层环境更长），`ramp_time=10` 把预热段
  挑出统计窗；必须配 `--time_based`，否则 size 跑完即停、时长不可控
- **数据集够不够的实证法**：同参数 size 翻倍重跑，IOPS/BW 掉 >10% →
  原数据集落在某层缓存里，继续翻倍直到收敛

## 参数表（fio-3.7 现场对账）

| 参数 | 作用 | 要点 |
|---|---|---|
| direct=1 | O_DIRECT 绕页缓存 | 必带；不带的结果是页缓存测试，作废 |
| ioengine | sync/libaio/io_uring | **sync/psync 引擎 iodepth 恒 1**：配 iodepth=8 不报错但深度还是 1（实测 2026-11-25：sync 跑出 IO depths 1=100%）；要打深度必须 libaio（io_uring 需内核 5.1+，现场 3.10/4.19 用 libaio） |
| iodepth | 每 job 在飞 IO 数 | 只对异步引擎生效；贴近 OLTP（每连接一队列）用 numjobs×iodepth 1~小值 |
| numjobs | 并发进程数 | 显式 `--filename` 时**全部 job 共享同一个文件**（实测：numjobs=8 输出 "Laying out IO file (1 file / 512MiB)"）；每 job 独立文件用 filename_format（默认不带 filename 时即按 job 拆） |
| size | 数据集大小 | 按缓存链规则定；共享文件时为单文件总大小 |
| rw / rwmixread | 负载形态 | randrw rwmixread=70 是 OLTP 常用口径 |
| bs | 块大小 | DB 8k/16k；顺扫 1M；与 RAID 条带不对齐会打折 |
| runtime + time_based | 压测时长 | 见上节 |
| ramp_time | 预热剔除 | 统计窗跳过前 N 秒 |
| invalidate=1 | 重跑前丢页缓存 | 默认开；**只管页缓存，不管 RAID 卡** |
| group_reporting | 多 job 汇总 | 多 job 必须，否则一 job 一段 |
| latency_percentiles | 尾延迟 | p99/p999 是闪存病征的主战场（`io-storage.md` 域 B） |
| fsync=N / fdatasync=N | 模拟提交路径 | 每 N 次 IO 发一次 sync；测 WAL 盘用 fdatasync=1 直接对应提交语义 |

## 结果判读（输出逐段）

- **slat/clat/lat 三层**：slat=提交耗时，clat=设备侧完成耗时，
  lat=端到端。iodepth>1 时 clat 含排队时间，判读看 lat 分布不看 clat 均值
- **IO depths distribution = 队列深度是否生效的实证**：配了 iodepth=8
  却跑出 1=100% → 引擎没吃深度（十有八九 ioengine=sync）
- **lat (usec)/(msec) 覆盖面行**与 clat percentiles 对读：
  percentiles 给分位，覆盖面行给整体形态；p99 尖刺而均值不动 = 闪存病征
- **cpu/ctx 段**：深度上去了而 sys% 高、ctx 巨大 → 主机侧瓶颈
  （中断/锁），不是盘慢
- **Disk stats 段两张用法**：
  1. *拓扑反推*：`aggrios` = 从属设备 IO 的平均值（总和÷从属数）。
     用自己 ios ÷ aggrios 反推从属数——dm-5 ios=790916、aggrios=159452、
     比值≈5 → 跨 5 个 PV；dm-1 底下 16 块 sd 各 ~2769 → 单 LUN 16 条路径
     （实测判例 2026-11-25：5 LUN × 16 路径的卷，不查 sysfs 直接从输出读出）。
     正式拓扑仍以 lsblk/multipath -ll 为准，fio 段作旁证
  2. *util 只作旁证*——util 禁令与 await 三档判读单源见 `io-storage.md`
- **与 `iostat -x 1` 同窗互证**：fio 报完成侧延迟，iostat 看设备侧；
  RAID 卡聚合会让两边对不上——以 fio lat 为准判测试质量，
  以 iostat await+绝对 IOPS 判盘是否成为瓶颈

## 是否已测到盘的极限（极限判定法）

"这轮 fio 到顶了吗"按三步判，缺一不可：

**第一步：阶梯加压找膝盖点**。同一数据集，iodepth（或 numjobs）逐级翻倍
（1→4→8→16→32），每级 ≥60s：

- IOPS 增幅 <5~10% 而延迟持续上涨 → 已过膝盖点，到卷的极限
- IOPS 还在近线性涨 → 远没到极限，继续加压
- IOPS 掉头向下、延迟暴涨 → 过饱和拥塞段，取前一级为极限

**第二步：Little's law 对账（判瓶颈在设备侧还是主机侧）**。
极限态下应有：`实测 IOPS ≈ 在飞 IO 总数 (numjobs×iodepth) ÷ 平均 lat`——
设备侧排队饱和时公式成立。**判饱和不拿 util**：multiqueue 并行度>1 时
util 100% 只是"窗口内有 IO 在飞的时间占比"，不是饱和证据（util 禁令，
任何介质，单源 `io-storage.md`）。对不上时的分诊：

| 形态 | 结论 |
|---|---|
| 实测 ≈ 公式值，lat 高而稳定 | 设备侧真饱和，IOPS 即极限能力 |
| 实测 << 公式值，**slat 涨** | 提交侧堵（引擎/中断/锁），盘没喂饱，先修主机侧再测 |
| 实测 << 公式值，sys%/ctx 高 | 主机 CPU 侧瓶颈（软中断单核打满常见） |
| 实测 << 公式值，iostat 平均 qu-sz 很低 | 请求根本没堆到设备，主机侧没喂饱 |

**第三步：对介质理论上限**（工程口径）：HDD 单盘随机 ~150-200 IOPS、
SATA/SAS SSD 数万~十万、NVMe 数十万~百万；RAID 卷 ≈ 盘数×单盘（随机写
受写惩罚/回刷拖累另算）。实测离介质档位差一个数量级以上 → 路径上有瓶颈
（缓存模式/条带/深度/主机侧），不是盘到顶。

注意：读和写**分开判极限**——混合负载（rwmixread=70）测到的不是读极限
也不是写极限；极限能力用纯 randread / 纯 randwrite 各测一轮。

## "好到不真实"检查表（数字假高指纹）

| 形态 | 指纹 | 结论 |
|---|---|---|
| HDD 卷 randread 数万 IOPS、延迟 <1ms | 小 size 数据集 | 全打在 RAID 读缓存，数字与盘无关 |
| 写成绩开局高、随 runtime 线性衰减后走平 | size ≤ 写缓存，或无 precondition | 前段=写缓存排空，走平段才是盘 |
| SSD 写成绩数倍于型号稳态标称 | 空盘直测 | SLC cache 出厂神话，须 precondition |
| 同参数 size 翻倍后成绩明显掉 | — | 原数据集落在某层缓存，size 不够 |
| 配 iodepth=8，IO depths 仍 1=100% | ioengine=sync | 引擎没吃深度，换 libaio |
| numjobs 加倍总 IOPS 不变、单 job 延迟不变 | 深度 1 多路争用 | 已到卷随机 IOPS 上限（或引擎单队列） |

## 与库负载映射

| 场景 | 配方 |
|---|---|
| OLTP 混合 | `--rw=randrw --rwmixread=70 --bs=8k`，numjobs≈业务连接数，iodepth 1~8 |
| WAL/提交 | `--rw=write --fdatasync=1`，bs 对齐 WAL 写粒度 |
| 全库扫描 | `--rw=read --bs=1M`，iodepth 4~8 |
| 验收判法 | 业务峰值（AWR 物理读写段）对照 fio 基线，留 30% 余量 |

## 最佳实践

- 稳态三件套：precondition + ramp_time + runtime≥60s
- 基线必须记环境：卡型号/缓存模式/条带/盘数/路径数/内核——
  没有环境注记的基线等于没测（换台机器数字不可比）
- 测单 LUN：直接打该 LUN 的多路径 dm 设备（16 条路径聚合）；
  单路径（/dev/sdX）只反映 1/N 能力，仅用于路径对比
- 多轮对比时固定全部参数只动一个变量，否则差异归因不了

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| buffered 测试当盘能力 | 无 direct=1 测的是页缓存 | direct=1 重测，旧结果作废 |
| iodepth 配 sync 引擎 | sync 引擎不吃 iodepth，恒 1 | 换 libaio |
| size 用默认（几 GB）测 RAID 卷 | 数据集整场落在写缓存里 | 按缓存链规则定 size |
| 拿第一轮数字当稳态 | 空盘+空缓存，全是缓存神话 | precondition+ramp_time |
| 单路径成绩当盘能力 | 单路径只是 1/N 路径 | 走多路径设备测 |
| util=100% 判饱和 | util 禁令（并行度>1 不等饱和） | await 三档+绝对 IOPS（io-storage.md） |
| 单轮低深度数字当盘极限 | depth=1 时 IOPS 由延迟决定，不是能力（实测判例：depth=1 全卷 7k，depth=8 到 103k，差 15 倍） | 阶梯加压到膝盖点才算极限（见上节） |
| 生产卷上 randwrite/randrw | 直接毁卷上数据；直写 sd 还与 multipathd 抢设备 | 写测试走隔离空间 |

## 安全红线

- 只读测试（randread/read）可直接打生产块设备（/dev/sdX、/dev/dm-N）
- **任何写测试禁打生产卷/生产盘**。写测试走专用测试 LV（限定单 PV）或
  独立测试 LUN：
  ```bash
  pvs                                            # 找有空闲 extent 的 PV
  lvcreate -n fiotest -L 10G <vg名> /dev/dm-1    # 强制只在该 PV 上建
  fio --filename=/dev/<vg名>/fiotest ...          # 在测试 LV 上跑
  lvremove -f /dev/<vg名>/fiotest                # 测完清理
  ```
- fio 对已存在文件直接覆写且默认不确认，跑前 `ls -l` 核对目标文件

## 版本注记

- 实测基线：现场机 fio-3.7（2026-11-25，多路径 LVM 卷，三轮对比：
  libaio depth=1 / sync / libaio depth=8）——sync 引擎 iodepth 行为、
  显式 filename 多 job 共享单文件、aggrios 平均口径均在 3.7 验证
- 缓存容量量级（卡缓存/盘内 DRAM/SLC cache/tier）为**工程口径**，
  随厂商型号变化，现场以 storcli/arcconf/smartctl/lvs 实查为准
- io_uring 引擎需内核 5.1+；现场老内核（3.10/4.19）一律 libaio

## 来源

- **fio-3.7 源码精读（2026-11-25，GitHub tag fio-3.7，关键件镜像
  `/home/xiang/kernel-src/fio-3.7-mirror/`，锚点经 verify_anchors.py 回源）**：
  - `diskutil.c:507-508`：`agg->ios[0] / agg->slavecount`——**aggrios=从属 IO
    总和÷从属数（平均值）**，拓扑反推法的源码依据；从属求和与计数在
    `aggregate_slaves_stats`（diskutil.c:528，slavecount++ :548）
  - `engines/sync.c:194-214`（fio_syncio_queue）：read()/write() 阻塞完成
    后 `fio_io_end` 就地收尾，队列里永远只有 1 个 io_u——**sync 引擎
    iodepth 恒 1 的源码定论**；FIO_SYNCIO 标志 :411
  - `filesetup.c:1098`："Laying out IO file (%u file%s / %lluMiB)"——
    显式 filename + numjobs=8 实测打印 "(1 file / 512MiB)"（need_extend=1）
    即全部 job 共享同一文件
- 内核侧锚点（linux-master 树）：`direct=1` 的 O_DIRECT 语义
  `fs/iomap/direct-io.c:661`（iomap_dio_rw，O_[D]SYNC 无条件等设备确认；
  与 io-storage.md 同锚）；盘写缓存可见性 `block/blk-sysfs.c:660`
  （queue_wc /sysfs "write_cache" 节点）
- 实测：2026-11-25 现场 fio-3.7 三轮输出（拓扑反推判例 5 LUN×16 路径、
  sync 引擎深度行为、"1 file / 512MiB" 共享文件实证）
- 判读口径单源：await 三档 / util 禁令 → `io-storage.md`
  （2026-10-28 用户令）
- RAID/SSD/tier 缓存量级为工程经验值，未逐型号核证，标"实践口径"
  （RAID 卡缓存位于 OS 视界之下，内核源码不可证，只能现场 storcli/arcconf
  实查）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：
IO 栈机制/await 口径→`io-storage.md`、分层介质参数→`parameters.md`、
IO 慢分诊→`os-triage.md`、盘瓶颈形态→`bottleneck-patterns.md`。
