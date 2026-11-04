# evals 结果（2026-10-25 首轮）

| 用例 | 验证方式 | 结果 |
|---|---|---|
| 1 · AWR 报告分析全流程 | 管线验证：extract.py 可导入、std_check 存在、基线 JSON 含 3.0.8/3.0.9 两键；端到端实跑见 2026-10-25 现场案例（五窗 v2 报告） | PASS |
| 2 · BufHashTableSearch 定性 | 知识链核对：core-events-deep-dive.md 含 bufmgr.cpp:3006 锚点、计数型定性、访问量镜像处置 | PASS |
| 3 · IO 读瓶颈四点齐 | 知识链核对：bottleneck-patterns.md 含"先看 Rot 定介质"与四点齐判定 | PASS |

说明：首轮为知识链+机器冒烟级验证；独立子agent端到端对比跑留待下一轮
（现场案例已作为事实上的端到端验证，产出 v2 报告）。

## 复核与真机抽验（2026-10-25 第二轮）

| 项 | 验证方式 | 结果 |
|---|---|---|
| 源码锚点复核 | `scripts/verify_anchors.py`：41 条断言回三棵源码树对账 | PASS 41/41（首轮 5 条 DRIFT 均为匹配到前向声明的误报，核器已修为全命中判） |
| 真机抽验 env-A | keepalive GUC 真值 / authentication_timeout / bgwriter 列 / 监听队列语义 / nstat 基线 | **抓出并修正 2 处知识错误**：① authentication_timeout 存在默认 1min（推翻"无此 GUC"负向结论）② keepalive GUC 默认 0=跟随内核（env-A sysctl 实测 30/30/9，非编译默认 7200）；另实测确认：pg_stat_bgwriter 无 candidate 列（负向结论机器双确认）、ss -lnt Send-Q=3000（backlog 实值）、idle_in_txn_timeout 默认 0、max_connections=500 |
| 沙箱 keepalive 实证（第二轮补） | env-A：`ss -tno` 可见 4 条 established 连接带 keepalive 定时器 | PASS——GUC=0 跟随内核语义的 socket 层实证（服务端连接确带 SO_KEEPALIVE，用内核 sysctl 30/30/9） |
| 第二案例复盘 | evals/fixtures 16:52 窗 digest 全读 | cases/case-envC-awr-lowload-202610：三因并列（空载属实/5.8 天长事务钉 OldestXmin→gs_asp 膨胀/HDD 双写 308ms）+ 方法论发现"空载库 SQL 榜=采集自身" |

## 全链路复核轮（2026-10-26 深夜，用户令"逻辑严密正确、顶级专家水平"）

| 项 | 结果 |
|---|---|
| 判读自纠 | 抓出并修正 1 处实错：五窗 ASP"唯一尖峰"结论（实际 5 个等待时段含两个 15 分钟带）——根因=只扫首窗分段，铁律已入 awr-report-analysis |
| 模板渲染 bug | bands 画满每个面板（合并 22 带放大成灾）——渲染器改 for 循环+bands 只画首面板，node --check 语法过；根因=多窗合并（用户令：永远一组一组） |
| 跨文件一致性 | 裸 PG 简称×12 文件→Postgres；network.md keepalive 陈旧行对齐两层口径；behind 公式/15 分钟表述单源一致 ✓ |
| 语义抽查 | 备库取快照等待方向 ✓ / most_available_sync 方向 ✓（且精确化为 DelayIntoMostAvaSync）/ O_DIRECT 条件 ✓ / lag 三段公式 ✓ / NESTLOOP_PARAM ✓ |
| 边抽查 | log-analysis↔memory、replication-lag↔network、executor↔query-pipeline 三组两端均有正文承接 ✓ |
| 诚实性 | case-envC 环境核实为测试机自采（与 env-A 同机）已如实标注 |
| 复核器 | verify_anchors 41→51 条（执行器/索引/连接/CM/checkpoint 新锚），51/51 PASS |

## fixture 更新轮（2026-10-28，用户令"去 env-T 生成一个真实的报告"）

| 项 | 结果 |
|---|---|
| 新 fixture | env-T 真机（vastbase@5432，vb_tool master acd4017a）窗口 2026-10-28 08:26:25~08:31:01（4.6min 混合负载）：`awr_dn_6001_20261028082625_20261028083101.html` + analyze_log 成对 + extract 三件套（digest 409 行/13 面板 75 点） |
| v2.5.17 五新节样本 | **Bond（条件态：无 bond 设备）/ RX Missed（eth0 missed=0+每分钟明细）/ Conntrack（每分钟 count/limit/fill，5 行）/ TCP Sockets（全状态每分钟）/ Memory Fragmentation（分 zone 订单 Begin/End/Δ）全部有数据**——补上旧 fixture（10-21/10-26 产物）缺新节的空白 |
| extract.py 补齐 | 新增 `summarize_os_new()`（五节入 digest"十、OS 附属证据"；TCP Sockets 取窗口峰值、Conntrack 取 count 走势+fill max、碎片取分 zone 表）；老 fixture 回归=优雅回退（恒写"(无…段)"行，非静默缺失） |
| 老样本顺带刷新 | 10-21 fixture digest 原是旧版脚本产物（五联对齐/10ms 阈值陈旧口径），重跑后刷新到当前脚本口径 |
| env-T 采样器 | `awr enable` 常开（用户令 2026-10-28，永不 disable）；本窗快照 1990/1991 |

## v2.5.18 结构对账轮（2026-10-30，用户令"按当前 awrrpt 看采集值"）

| 项 | 结果 |
|---|---|
| 采集面复对账 | master 6b17d22：快照文件 27 项不变；**每分钟行 14 类——新增 dbfd**（DB Process 折表 fd 三桶）；TCP Sockets/Conntrack/RX Missed 独立呈现取消，并入 DB Process 折表与网卡表；TCP Statistics 独立成章（默认折叠） |
| 新 fixture | env-T 真机窗口 2026-10-30 01:00:02~01:10:32（快照 2033→2035，12 分钟）：`awr_dn_6001_20261030010002_20261030011032.html` + analyze_log 成对；折表列（LISTEN Recv-Q/TCP Total/四态/FW1-FW2/Conntrack Used-Limit/DBFile-TempFile-XLog）全在，**fd 三桶 45/0/1 与 env-T /proc 独立对账逐位一致**；dbfd 采样行 839 行/天 |
| extract.py 两代兼容 | `summarize_os_new()` 重写：v2.5.18 折表一张表提全（TCP 状态峰值+FW 双数+conntrack used→last/max+fill+fd 三桶峰值/deleted 峰值）+ 新增 TCP Statistics 段（Retrans% max/RTT avg-p95 峰值/CurrEstab/ListenDrops/Timeouts/SynRetrans）+ RX Missed 走网卡表列；v2.5.17 回退（TCP Sockets/Conntrack 独立表）保留 |
| 双向回归 | 10-30 新 fixture 全出数；10-28 旧 fixture 走回退且**TCP Statistics 亦出数**（该表 10-28 就在 HTML 里只是埋在折表内无 TOC，现在也能解析）；10-21 fixture 差异全部为 10-28 未重跑的口径收敛（util 删除/18 列横切），零回归 |
| 知识文件同步 | `metrics.md`（TCP 状态行改锚 DB Process 折表+新列形、fd 行补 dbfd 三桶、采集开关补 OS-only 八命令跨重启豁免、概述/来源补 10-30 复对账）；`awr-report-reference.md`（DB Process 行补折表、插 TCP Statistics 子表行、bond 空节标注） |
