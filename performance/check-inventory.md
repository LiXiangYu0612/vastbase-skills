# 检查项总账（awrrpt × logrpt：能诊断什么 + 固定检查逻辑）

## 概述

回答两个问题：**两份报告各能诊断什么问题**（能力总账，表 A/B/C），以及**每次分析
必跑的固定检查是什么**（机械闸，表 D）。三层结构：

1. **能力总账**（表 A/B/C）——问题域 → 报告章节 → 形态归属；两报告的覆盖边界
2. **机械闸** = `scripts/overview_table.py`（表 D 全量镜像）——从 digest 程序化提取、
   阈值固定（RULES 字典）、每次分析第 2 步必产 ■异常/▲警告 表；判定零主观
3. **跨层关联组**（表 E）——机械闸是单层触发，OS↔DB↔LOG 的联系在此：
   13 个问题域的三层证据组合+合取规则（单层=怀疑/两层同向=立论/三层互证=闭环）
   +深入分析（三层分工本质、诊断与分析两类用途、硬边界）
4. **形态闸**（人工纪律兜底）——机械闸亮 ■/▲ 的项按 FORM_OF 映射进
   `bottleneck-patterns.md` P1~P12 走取证链；机械闸覆盖不到的判读（表 D 尾
   "人工项"）由六步流程纪律承接

阈值**单一来源 = overview_table.py 的 RULES**；本文件表 D 是文档镜像，改阈值
两处同步改。判读语义出处：`awr-report-reference.md`（逐节）/`metrics.md`（口径）/
`log-analysis.md`（族表）。

## 表 A：awrrpt 能诊断的问题域（17 章 × P1~P12）

| 章节 | 能诊断的问题域 | 形态归属 |
|---|---|---|
| 报告头 + Time Model | 慢在哪层（算 vs 等）、窗内重启 | P1 方向；R 标记=窗口作废 |
| OS · CPU（util/per-min r,b） | CPU 饱和、库外进程抢、steal 抢 | P1 / P2 |
| OS · Cgroup | **配额限流咬合、cpuset 给核少、cgroup OOM** | P2 / P7 |
| OS · NUMA | 跨节点访存、库内存落点失衡 | 症状 11（延迟抖动/整体慢） |
| OS · Softirq/IRQ 矩阵 | 中断/软中断风暴、**单核打满**（均值盲区） | P2 / P11 |
| OS · 内存（meminfo/per-min/碎片/THP） | 换页真不足、水位将破、碎片化、压缩停顿 | P7 |
| OS · Disk（身份列+await） | 介质档判定、读写延迟、IO 拥塞 | P3 / P4 |
| OS · Filesystem Usage | 空间/inode 逼近满（pg_xlog/pg_arch 盘满=WAL 停写高危） | P4 / P9 |
| OS · 网络（网卡/TCP/RTT/softnet/conntrack） | 丢包分层（ring/backlog/预算/队列/表满）、重传、RTT 劣化、短连接风暴、accept 队列饱和 | P11 / 症状 11 |
| OS · dmesg | 内核级硬证据（OOM/存储/文件系统） | P7 等 |
| Load Profile | 吞吐/会话/SQL 速率画像（证据层） | 全形态证据 |
| Cache Hit Ratio | 命中率真低（配物理读四点齐） | P3 |
| Database Memory | 内存池构成、会话大户、Shared Context 增长 | P7 |
| Replication | lag 三段分解、槽积压、WAL 速率 | P9 |
| Top 10 Wait Events | 定量主瓶颈（事件谱） | 全形态入口 |
| Long Tx / Callstack / Pagewriter | 锁现场、内核堆栈指纹、刷脏滞后 | P5 / P8 / P4 |
| SQL Statistics（九维，ms） | 责任 SQL（CPU/IO/内存/落盘/解析各维）、频次型 vs 单次贵 | P1 / P3 / P6 / P10 |
| Object Statistics（含系统表） | 表膨胀、死索引、seq_scan 激增、statement_history 膨胀 | P8 |
| Active Session History | 尖刺分钟回放、waitchain 谁锁谁 | P5 等现场 |
| Wait Events 全量 / SQL Text / 参数 | 佐证与深挖（折叠三节） | 证据层 |

## 表 B：logrpt 能诊断的问题域（analyze_log_v2，六部分结构）

报告本体六部分（条件渲染，全量见 2026-11-02 死锁样本 fixture）：

| 部分 | 能诊断什么 | 形态/归责 |
|---|---|---|
| 汇总信息 + meta（v20.1 起带生成时间/工具版本） | 级别人数、错误最多时段 | 入口 |
| 错误日志分析（级别计数/Top15 聚合/明细） | **PANIC 任一条=实例级损伤最高优先**；FATAL=断连面；风暴识别（同码 >10/s）；10 高频族挂 AWR 影子（族表见 `log-analysis.md` 不复制） | P12 及各族 |
| **锁/死锁详情分析（条件渲染）** | **死锁环还原**：A/B 双方 sessionid/txid+Unique ID+SQL 摘要+锁类型+检测耗时（≈deadlock_timeout）+阻塞者事务与其语句；锁等待事件、锁链——**P5 最硬的直接证据，awrrpt 只有计数** | P5 |
| 慢查询分析（耗时分布+Top SQL 排名+SQL 详情列表） | 慢 SQL 清单+排名（先剔除采集自身指纹） | P1/P3/P6 佐证 |
| SQL 分时段分析（每分钟/10 分钟/小时/天 Top10） | 慢 SQL 时间分布——定位"几点开始变慢"（与 awrrpt 分钟轴对齐） | 时段定位 |
| SQL 汇总统计 | 窗口执行统计总账 | 证据层 |
| **等间隔节奏判据**（判读法） | 秒级恒定/整小时均匀=轮询器或集成服务；随机散布=人工——**awrrpt 看不到** | 归责分诊 |
| **监控/采集侧权限错**（判读法） | 等间隔 permission denied=监控一直失败→**视图残缺盲区**，最高优先修 | 独有 |
| P/W 时间切片（判读法） | 触发区 vs 症状区先后（根因在更早则前推） | 取证层 |

死锁环已入 digest（十二章"死锁环/锁事件"块，extract.py 2026-11-02 起）；digest 死锁块
有行=直接进 P5 取证链，与 awrrpt 长事务榜/waitchain 互证。

## 表 C：覆盖边界（谁负责什么，缺口显式）

| 边界 | 问题类型 | 说明 |
|---|---|---|
| **仅 awrrpt** | 计划劣化、介质档慢、NUMA、cgroup 限流、sirq 单核、命中率、刷脏滞后 | 慢性无报错形态——日志安静的病只有这里看得见 |
| **仅 logrpt** | 应用侧死轮询（认证/重试）、业务语句错、监控盲区、PANIC、语句级失败明细 | 报告数字正常但日志在喊的病 |
| **必须联动** | 死锁（日志族+长事务/锁事件）、OOM（53200+OS 换页+cgroup events）、连接风暴（too many clients+TCP 状态+ListenDrops）、no space（53100+fs 用量）、超时（57014+九维榜伴生） | 单侧结论降观察项 |
| **两报告都看不见** | 活锁/持锁不退的**现在进行时**（→ `troubleshooting/blocking-live.md` 保现场）；瞬时进程退出；内核级 hang（→ `linux/tracing-tools.md`）；实例起不来（→ startup-failure）；宕库 core（→ crash-panic） | 现场件接管，不硬凑 |
| **Callstack 段边界** | 堆栈列 `-`≠超时 | 该段只盖固定后台线程+最高 CPU 的 AutoVacWorker+每 LWLock 事件最长等待者（5 分钟一采，无全前台堆栈）；`-` 全空 = gs_stack 语句级失败、`thread N not available` = 5ms 窗口超时，判读矩阵与定位配方 → `internals/gs-stack-sampling.md`；2026-11-02 起采样器失败自动留痕（报告表底 st error 行） |

## 表 D：固定检查项总账（机械闸全量 = overview_table.py build()）

状态三态 ●正常/▲警告/■异常（阈值:异常值, 警告值；来源列=digest 章节）。
亮 ■/▲ 自动按 FORM_OF 过形态闸。

| 组 | 检查项 | 异常条件（阈值） | 形态 |
|---|---|---|---|
| 环境 | 逻辑核数/内存/盘体 | —（定 IO 阈值档） | REF |
| OS 资源 | CPU busy max | ≥80 / ≥60 | P1 |
| OS 资源 | iowait max | ≥20 / ≥10 | P1 |
| OS 资源 | 运行队列 max/核 | >2 / >1；**守卫：busy max<70%=无饱和佐证不判**（瞬时尖峰） | P1 |
| OS 资源 | 内存可用 min | ≤10 / ≤20 | P7 |
| OS 资源 | swap 换出 max（页/min） | ≥10000 / ≥1000 | P7 |
| OS 资源 | 数据盘 await max（按介质） | SSD>5 / HDD>20（util 永不判读） | P3/P4 |
| OS 资源 | Cgroup 配额咬合（nr_throttled Δ） | Δ>0 即异常（digest 旗标） | P2 |
| OS 资源 | cgroup memory.events OOM | 绝对值>0 即异常 | P7 |
| OS 资源 | Softirq %sirq 单核峰值 | ≥80 / ≥50 | P2 |
| OS 资源 | IRQ %irq 单核峰值 | ≥80 / ≥50 | P2 |
| OS 资源 | NUMA Local Ratio 最低节点 | ≤90 / ≤95（单节点 REF 不判） | 症状 11 |
| 负载与命中 | 每核并发 AAS/N | >1 / >0.5 | P1 |
| 负载与命中 | DB CPU / DB Time | 方向值 | REF |
| 负载与命中 | 物理读/逻辑读 | >5% / >2%；**守卫：窗口逻辑读<1 万块不判** | P3 |
| 负载与命中 | 缓冲命中率（最低库） | 口径见 metrics；**守卫：同逻辑读分母地板** | P3 |
| 负载与命中 | Plan Time 占 DB Time | >10 / >5（闸）；立论线 30%；**守卫：DB Time<10s 或<1% Elapsed 不判** | P10 |
| 负载与命中 | 死锁区间增量 | >0 关注（环详情看 digest 死锁块/logrpt 锁章） | P5 |
| 等待事件 | BufHashTableSearch 甄别 | 计数型测量括号非锁 | P3 甄别 |
| 等待事件 | 锁类事件 avg（lock/lwlock 族） | ≥1000ms / ≥100ms | P5 |
| 活跃会话 | ASP 桶 wait lock/lwlock | 峰值 ≥2×6 / >6（桶内秒峰） | P5 |
| 活跃会话 | ASP 桶 wait io | ≥2×6 / >6 | P3 |
| 活跃会话 | ASP 桶 wait wal_sync | ≥2×4 / >4 | P4/P9 |
| 活跃会话 | ASP 桶 wait trans_sync | ≥2×10 / >10 | P5/P9 |
| 活跃会话 | autovacuum worker | 全 0=清理停摆 | P8 |
| 活跃会话 | Pagewriter 脏页峰值/behind xlog | 落后=LSN 滞后 | P4 |
| 活跃会话 | 长事务 ltx60 峰值 | >60s 个数 | P5/P8 |
| SQL | 慢 SQL 分桶行（logrpt） | 剔采集指纹后计数 | P1/P3 |
| SQL | Top SQL 五维 top1 | 维度占比 | 各形态 |
| SQL | TempFile fds 峰值 | >0 即警告（pgsql_tmp 落盘） | P6 |
| 参数 | GUC 基线初筛（G100） | 偏离计数 | 参数层 |
| 参数 | sysctl 已采集 | Begin≠End=窗内改过 | 证据 |
| 对象与访问 | 死元组率最高表（含系统表） | ≥20 / ≥10；高比率+长事务=vacuum 被卡；**守卫：live=0 计数失真 / 死<200 / 活+死<1000 比率不判** | P8 |
| 对象与访问 | 顺序扫/索引扫行数最高 Δ | seq 激增=索引失效方向 | P3 |
| 对象与访问 | statio Logical/Physical Top 表 | — | REF/P3 |
| 日志 | ERROR+FATAL 总量（窗归一） | ≥1000/h / ≥100/h | P12 |
| 日志 | 五族分账（认证/唯一键/权限/被踢/超长） | 单族 ≥100 / ≥10；**权限错最高优先=盲区** | P12 |
| 日志 | 其他低频错误类数 | <10 正常 | P12 |
| OS 附属 | dmesg 内核错误 | 任一条 Storage/OOM/FS=高危证据 | P7 等 |
| OS 附属 | 主备复制延迟 total 峰值 | ≥1024MB / ≥100MB | P9 |
| OS 附属 | TCP RTT p95 | >5 加粗 >20 红（渲染同款） | P11 |
| OS 附属 | TCP Retrans% max | >5 / >3 | P11 |
| OS 附属 | TCP TIME-WAIT 峰值 | ≥5000 / ≥1000（样例待校） | P11 |
| OS 附属 | TCP CLOSE-WAIT 峰值 | ≥500 / ≥100（样例待校） | P11 |
| OS 附属 | TCP SYN-RECV 峰值 | ≥100 / ≥30（样例待校，配 ListenDrops） | P11 |

**人工项**（知识文件有阈值、尚未机械化——六步流程人工判读承接，候选后续入闸）：
ListenDrops/Overflows>0、File Handles used 逼近 Max open files soft、
compact_stall>0、内存碎片率>50%、pgmajfault>50/s、conntrack count 逼近 max、
softnet dropped/time_squeeze、zone 水位 WM Free<WM Min、listener Recv-Q 逼近
Send-Q、Retrans%×RTT 并排读（真劣化 vs 丢在别处）。

## 表 E：OS↔DB↔LOG 跨层关联组（合取立论——检查项之间的联系）

**机械闸本身是单层触发的：亮灯只产生"怀疑"。** 跨层联系在这里（2026-11-02 立，
合取规则源自 metrics.md 关联判读：**单层=怀疑，两层同向=立论，三层互证=闭环**）。
用法是双向的：任何一层亮 ■/▲，拉本行另外两层的项同分钟横切；机制出处挂
`bottleneck-patterns.md` 对应形态取证链。

| 问题域 | OS 侧（表D 项） | DB 侧（表D 项） | LOG 侧（表D 项） | 合取→能下的结论 | 边界（看不到什么） |
|---|---|---|---|---|---|
| CPU 饱和（库内） | busy■、运行队列■、iowait▲ | AAS/N■、Top SQL·DB/CPU Time■ | 常安静；超时族偶伴生 | 哪些 SQL 把 CPU 吃满（霸榜+高频型分型） | 分不出库外份额——走下一行分流 |
| 库外抢 CPU（P2） | busy■+r■、Cgroup 配额■、单核饱和■ | **DB CPU/DB Time ○（低=分流证据）** | 无 | 定责层：cgroup 配额/cpuset/steal/库外进程 | 具体哪个进程要 ps/perf（现场件） |
| IO 读慢 | 数据盘读延迟■+iowait▲（Rot 定档） | 物理/逻辑读■、缓冲命中▲、Top SQL·IO Time■ | 超时族 | 介质慢 vs 缓存小 vs SQL 全扫，三者分开 | 阵列/SAN 内部（OS 层 tracing 接手） |
| IO 写/WAL/刷脏 | 数据盘写延迟■ | Pagewriter behind■、ASP wal_sync 桶▲ | no space/归档族 | 刷脏跟不上 vs 存储慢 vs WAL 风暴 | 双写×checkpoint 交互下钻 internals |
| 锁/死锁（P5） | ctxt 高（辅助） | ASP lock/lwlock 桶■、长事务■、waitchain | **死锁环块（族计数▲）** | 谁锁谁、哪条 SQL 改锁序/缩事务 | 活锁与已结束的锁（blocking-live 现场） |
| 连接风暴（P11） | TIME-WAIT■/CLOSE-WAIT■/SYN-RECV■、Retrans▲ | CurrEstab、Threads→连接对账 | too many clients/认证族 | 短连接风暴 vs 应用不关 vs 认证轮询，三分 | 客户端侧与中间网络（抓包/客户端日志） |
| 内存压力/OOM（P7） | 内存可用■、swap 换出■、dmesg■、cgroup OOM■ | 会话大户/Shared Context（人工项） | 53200 族 | 真不足 vs 可回收占位；进程侧 vs cgroup 侧 | 哪个上下文吃的（Session Memory 榜+内核） |
| 表膨胀（P8） | —（OS 无关） | 死元组率■+长事务■+AV worker○ | autovacuum 防抖 WARNING | vacuum 被谁卡（长事务/阈值/worker 停摆） | 索引膨胀单独算（index-btree 形态） |
| 复制延迟（P9） | 备库盘延迟/网络（证据） | lag 峰值■、槽状态、WAL 速率 | WAL 缺失族 | Net/Fsync/Apply 分段定责 | 中间网络设备（OS 抓包） |
| 网络丢包分层 | RX Missed→errs→softnet→ListenDrops→conntrack（人工链） | Retrans▲、RTT（伴生） | reset 族 | 丢在哪一层（ring/backlog/预算/队列/表满）——**层不同旋钮完全不同** | 对端与中间网络劣化 |
| 计划/解析（P10） | — | Plan Time▲、Top SQL·Plan Time | 慢SQL桶节奏（硬解析等间隔） | 解析贵 vs 计划突变（配 query-plan-cache 判定链） | "昨天好今天慢"要历史报告对照 |
| 临时落盘（P6） | TempFile fds▲、盘写压力 | Temp Spill 维、work_mem（参数组） | — | 哪些 SQL 落盘+值不值得调 work_mem（算子配额口径） | 口径不含聚合内排/HashAgg（metrics） |
| 错误风暴（P12） | 多为果不是因 | 按族找影子（各族行） | 级别计数■+族计数▲ | 风暴源归责三分（监控盲区/应用/库侧） | WARNING 级慢性病要看趋势不看计数 |

## 深入分析：这套检查项到底能诊断什么、能分析什么

**三层的分工本质**（为什么必须联）：

- **OS 项答"环境有没有配合"**——根因侧。CPU/内存/IO/网络/cgroup/NUMA 是库外
  约束，库内指标永远解释不了自己被限流、被换页、被丢包
- **DB 项答"谁在受影响、谁负责"**——症状与责任侧。九维 SQL 榜+ASP+事件谱把
  影响精确落到 SQL/会话/对象；没有 DB 侧，OS 异常只是环境报告不是诊断
- **LOG 项答"用户已感受到什么、谁在制造"**——影响与归责侧。错误是用户视角的
  事实；等间隔节奏与权限错这两类判读 **awrrpt 完全看不见**

**两类用途**（检查项不只用来"找病"）：

1. **诊断**：表 E 的 13 个问题域剧本——异常触发→三层合取→形态定论→处置
2. **分析（非异常类）**：REF/画像项同样构成分析能力——
   容量余量（AAS/N<0.5、内存可用水位、IO 距介质上限的距离）、
   参数合规（GUC 基线初筛偏离清单=交付核对）、
   负载画像（读写比、会话模型、频次型 vs 单次贵 SQL 结构）、
   介质与平台判定（Rot/Platform=虚拟化，决定一切阈值档）
   ——健康报告的"分析"产出的是容量与风险预告，不是"无异常"三个字

**能诊断的硬边界**（表 C 已列，此处从检查项角度复述）：进行时锁现场、瞬时进程、
内核 hang、实例起不来——检查项全绿也答不了，必须现场件。

## 流程接线（六步流程里的位置）

```
digest(extract.py) → ①第2步 健康总览 = 表D 机械闸产物(overview_table.py)
                   → ②每个 ■/▲ 按 FORM_OF → P1~P12 形态闸(bottleneck-patterns)
                   → ③logrpt 族分诊三归责(log-analysis 五步) 与 AWR 影子闭环
                   → ④交付 selfcheck.py 三查(数值回溯 digest/图表有数据/四要素)
```

## 版本注记

- 机械闸 47 项 = 2026-10-24 版 33 项 + 2026-11-01 对齐 v2.5.20 新节 8 项
  （Cgroup×2 / sirq×2 / NUMA / Retrans / TempFile / 形态映射扩展）
  + 2026-11-02 三轮 TCP 状态峰值 3 项（TW/CW/SR，样例阈值待用户校）
- 2026-11-02 二轮完善：表 B 按 logrpt 报告本体重写（六部分结构；补**锁/死锁详情
  章**=死锁环还原——digest 十二章新增"死锁环/锁事件"块供数）；表 A 补
  Filesystem Usage 行；死锁样本 fixture 入库（env-T 真死锁 2026-11-02）
- 2026-11-02 三轮：**表 E 跨层关联组（13 问题域×三层证据×合取规则×边界）**+
  深入分析节（三层分工本质/诊断与分析两类用途/硬边界）——机械闸单层触发、
  跨层联系在此表，与 metrics.md 关联节/log-analysis 族表/形态取证链互为索引
- 2026-11-04：**分母/佐证守卫 5 项**（用户令"非问题不立卡"，同类审计后落闸）：
  运行队列（busy<70% 佐证）、Plan Time（DB Time<10s 或<1% Elapsed）、死元组率
  （live=0/死<200/分母<1000）、物理读占比与缓冲命中率（窗口逻辑读<1 万块）——
  全量审计结论：其余检查项均为绝对量/真实事件类（fire 即真问题）不加守卫；
  挂账待校准：swap WARN=1000 页/min（≈4MB/min 绝对量过小）、Retrans% 小流量放大、
  TCP 三状态峰值样例口径
- 表 A/B 随 awrrpt/logrpt 章节变化同步（vb_tool 升级 → `awr-report-reference.md`
  /`log-analysis.md` 先行，本表跟随）

## 来源

- `scripts/overview_table.py`（RULES/build/FORM_OF，2026-11-02 复核两份 fixture 全量行）
- `awr-report-reference.md` 17 章判读 + `log-analysis.md` 族表 + `bottleneck-patterns.md` P1~P12
- 实测：evals/fixtures 2026-10-28（TCP/折表形态）与 2026-11-01（v2.5.20 三新节）digest
- logrpt 结构解剖：analyze_log_v2 渲染源码（generate_deadlock/lock_event/lock_chain
  section，17256~17700 行区）+ 2026-11-02 真死锁样本（env-T，deadlock_timeout≈1s 检测耗时实证）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
