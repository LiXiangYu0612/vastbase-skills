# 错误日志与 AWR 关联（analyze_log_v2）

## 概述

analyze_log_v2（vb_tool，`-m error/slow/all`）独立扫描 pg_log 产出 HTML 报告；
`awr awrrpt` 默认配对生成并打包为 `awr_<b><e>.tar`。预提取器已聚合错误码分布、
级别计数、错误明细、慢 SQL 分桶与前后窗切片。

核心立场：**错误从来不是孤立事件——每类错误在 AWR 里都有影子，找到影子才算闭环**。
错误的地位 = 佐证与时刻锚，不做孤证定论。各错误家族的源码机理与排查法见
`../internals/error-fatal-mechanics.md`（谁抛的、什么条件、怎么排）——先定性质再走本表联动。

## logrpt 判读流程（五步，与 awrrpt 六步对偶）

产物与输入：

- logrpt = `analyze_log_{db}_{b}_{e}.html`（vb_tool `awr awrrpt` 默认配对生成并打包
  `awr_<b><e>.tar`；也可单独 `analyze_log_v2 -b <起> -e <止> -m error|slow|all` 生成）
- 报告六部分：汇总信息/meta、错误日志分析、**锁/死锁详情分析（条件渲染：死锁环
  A/B 双方 sessionid/txid+SQL+锁类型+检测耗时+阻塞者事务语句、锁等待事件、锁链
  ——P5 最硬直接证据）**、慢查询分析（耗时分布/Top SQL/详情）、SQL 分时段
  （分钟/10min/时/天）、SQL 汇总统计
- digest 已自动聚合（extract.py 十二章）：汇总（级别人数 / 错误最多时段 / 慢 SQL
  触发计数）、错误消息聚合 Top15、错误明细（≤60 条）、慢 SQL 分桶（≤120 条）、
  **死锁环/锁事件块（2026-11-02 起）**、异常时段日志切片（P/W 两区）

**第 1 步 摘要画像归一化**

- 错误计数一律除以快照区间时长（条/小时）再下结论——4.6 分钟 3 条与 1 小时
  300 条不是一回事
- 级别人数：**PANIC>0 单独最高优先**（实例级损伤）；FATAL 计数=断连面
  （认证失败 / 被踢 / OOM kill / admin shutdown 都在这族）

**第 2 步 错误族分诊 + 三归责**

按错误消息聚合 Top15 逐族定性（谁抛的 / 什么条件 / 怎么排 →
`../internals/error-fatal-mechanics.md`），再归责三分：

| 归责 | 典型族 | 处置位 |
|---|---|---|
| **监控/采集侧** | 监控账号权限被拒（等间隔 permission denied） | **最高优先**：权限错=对应采集一直失败，监控视图可能残缺（盲区）——先修盲区再谈别的 |
| **应用侧** | 认证失败死轮询（等间隔 FATAL）、唯一键重试风暴、业务 SQL 错 | 修一次消一整类日志噪声：凭据修正 / 改幂等 / 上游去重 |
| **库侧** | OOM、no space、WAL 段缺失、死锁、lock timeout | 才走下方 AWR 联动表找数据库侧影子 |

节奏判据：**等间隔（秒级恒定、整小时均匀）= 轮询器 / 集成服务；随机散布 = 人工操作**。

**第 3 步 每分钟时间轴与风暴识别**

- 同码错误 >10 次/秒 = 风暴，先报风暴再查根因（阈值与切片口径见下"时间窗对齐"）
- digest 异常时段切片：P=[t0−10min, t0) 触发区 vs W=[t0, t1] 症状区——P 区已有
  同族错误 = 根因在更早，切片往前推

**第 4 步 同分钟横切对账**

- 错误时刻对 digest 十三章多源对齐行与 19 列时段明细（含 ctfill% / ESTAB /
  TW / CW / SR）同分钟核：认证风暴应配 TCP 建连尖刺（ESTAB/SYN-RECV），
  被踢族配 session_timeout 时长，唯一键重试配业务写吞吐

**第 5 步 慢 SQL 分桶对账**（`-m all` 才有）

- 分桶行先剔除**采集 SQL 指纹**（vb_tool / 监控的周期查询：固定分钟间隔 +
  系统对象名可辨；空载库慢 SQL 榜 Top 全是采集自身是常态）
- 剩余与 awrrpt 九维 SQL 榜按时间对账；statement timeout（57014）≠ 慢，
  但同 SQL 同分钟在九维榜=伴生证据

**出声规则**：每族给三问答案（是什么族 / 谁在产生 / 怎么修）+ 归责标签；
库侧族必须在 AWR 找到影子（下方联动表）才算闭环——找不到要么数据不足要么
库外因素，显式说，不硬凑。

## 日志级别语义

- **PANIC**：进程/实例级损伤（WAL 损坏、共享内存崩溃）——任何 1 条都是最高优先发现
- **FATAL**：连接/会话级死亡（认证失败、OOM kill、admin shutdown）
- **ERROR**：语句级失败（约束、锁超时、语法、资源不足）
- **WARNING**：隐患信号（不致命但常是慢性病前兆）

## 高频错误族与 AWR 关联

| 日志错误 | 典型 SQLSTATE | AWR 里找什么 | 对应形态 |
|---|---|---|---|
| deadlock detected | 40P01 | 长事务榜、锁等待事件、Object Activity 热点表 | 锁与并发竞争 |
| lock wait timeout / canceling statement due to lock timeout | 55P03/57014 | 同上；ASP 尖刺时刻 | 锁与并发竞争 |
| out of memory / memory is temporarily unavailable | 53200 | Memory GUCs、Session Memory 大户、Shared Context 增长、OS 换页 | 内存压力 |
| could not extend file / no space left on device | 53100/53000 | WAL 速率、复制槽 inactive 积压、temp 写入 | IO 写 / 复制延迟 |
| too many clients already / remaining connection slots | 53300 | Load Profile 会话指标、TCP CurrEstab、上下文切换 | 连接风暴 |
| connection reset / auth failed（FATAL 族） | 28P01/08006 | TCP Retrans、客户端网段；认证风暴也会拉高 CPU | 连接风暴 / OS 层 |
| canceling statement due to statement timeout | 57014 | 该 SQL 在九维 SQL 榜的位置（超时≠慢，但常伴生）；ASP 该时刻等待 | CPU / IO 读 |
| WAL 相关（requested WAL segment ... already removed 等） | — | Replication 段、归档延迟 | 复制延迟 |
| could not write to log file / archive | 58000 系 | 磁盘空间、Iowait | IO 写 |
| autovacuum 跳过/防抖 warning | — | dead tuples 榜、长事务 | 表膨胀 |

（形态取证链见 `bottleneck-patterns.md` 对应小节。）

## 时间窗对齐

1. 只把**落在 AWR 快照窗内**的错误纳入联动（digest 的 Begin/End）；窗外的列入
   "窗口外补充观察"，不算发现证据
2. 错误时刻与 ASP 采样尖刺重合 → 置信度升一档
3. 错误计数本身也是负载信号：同码错误每秒 >10 次 = 风暴，先报风暴再查根因
4. 时段切片口径：P=[t0−10min, t0) 触发区、W=[t0, t1] 症状区，分别计数对比先后

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 错误直接定根因 | 错误是症状不是机制 | 找到 AWR 侧影子（章节+数值）再定论 |
| 窗外错误算进本窗证据 | 与本窗数据无因果 | 移入窗口外补充观察 |
| WARNING 全忽略 | 常是慢性病前兆（autovacuum 防抖等） | 归观察项并点名 |
| 错误计数不按时长归一 | 4.6 分钟 3 条被当风暴、1 小时 300 条被轻描 | 先算条/小时 |
| 采集 SQL 指纹当业务慢 SQL | 空载库慢 SQL 榜 Top 全是采集自身 | 先剔指纹再对账 |
| 监控账号权限错当普通噪声 | 权限错=对应采集一直失败=监控盲区 | 升最高优先：修权限或确认采集去留 |

## 版本注记

- analyze_log_v2 输出格式基于 vb_tool 2026-10 版；digest 的错误聚合随其变化

## 来源

- vb_tool analyze_log_v2 源码（错误分类与分桶规则）
- 实测样本：2026-10-21 成对报告（`../evals/fixtures/`）
- `../cases/case-envD-errstorm-202610.md`（三归责方法论来源：画像健康→重心转
  日志轴，三场错误风暴全在应用/监控侧）
- 2026-10-26 现场样本（错误族节奏判据、采集指纹剔除）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
