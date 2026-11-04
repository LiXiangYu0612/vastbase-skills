# ERROR/FATAL 报错家族机理与排查

## 概述

> 服务分类：performance（logrpt 判读）；troubleshooting（现场排查，落地后互链）

analyze_log_v2 报告里高频错误家族的源码级机理：**谁抛的、什么条件触发、级别语义、
怎么排查**。读 logrpt 时先查本表定性质，再走 `../performance/log-analysis.md` 的
AWR 联动表找数据库侧影子。

级别语义（内核行为）：
- **ERROR** = 当前语句失败，事务回滚到语句前（连接保持）
- **FATAL** = 会话级死亡，连接断开（客户端看到 connection terminated）
- **WARNING** = 不失败但是隐患信号

标注约定：〔已核〕= 本基线源码逐行核对过；〔未逐行核〕= 机制明确（标准路径），
文本组装点未逐行定位。

## 1. Invalid username/password, login denied —— FATAL

- 抛出点：〔已核〕`common/backend/libpq/auth.cpp:289`（认证握手层，FATAL 断连）；
  变体：〔已核〕`gausskernel/optimizer/commands/user.cpp:5740`（ERROR，
  ERRCODE_UNDEFINED_OBJECT）
- 触发：密码错 / 用户不存在（出于安全不区分提示）/ pg_hba 策略不匹配
- 排查：看**节奏**——固定间隔（如样本每 6 秒一次、整小时 600 次）= 某集成服务
  凭据错误的死轮询；随机散布 = 用户手误。核对来源 IP、pg_hba.conf、账号状态
- logrpt 要点：这种"整点均匀、间隔恒定"的错误源修一次就消失一整类日志噪声

## 2. permission denied for database / relation / tablespace —— ERROR 42501

- 抛出点：〔未逐行核〕权限检查（aclcheck 家族）按对象类型组装消息
- 触发：账号无对象权限；监控类账号最常见（样本 monitor_vem：库 CONNECT、
  表空间、pg_user_status 视图三类全拒）
- 排查：对账号逐一 GRANT，或确认该采集该不该在；**注意此类错误往往意味着
  对应监控采集一直失败——监控视图可能是残缺的**
- logrpt 要点：固定间隔重复 = 采集器轮询；三类 permission denied 同源

## 3. duplicate key value violates unique constraint —— ERROR 23505

- 抛出点：〔已核〕两条路径——索引唯一性检查（消息**带约束名**，如样本
  "t_jqyj_uniq_sjdbh_yjlx"，executor/indexam 路径，nodeModifyTable 家族）；
  opfusion 快速路径变体（〔已核〕`runtime/opfusion/opfusion_insert.cpp:353`，
  消息不带约束名、WARNING 级）——同一业务错误两条路径，看消息形态可判走了哪条
- 触发：并发插入同键 / 重试风暴 / 上游重复推送
- 排查：数频率（样本每 1.3 秒一次、持续整点 = 慢性重试）；改写为幂等
  （INSERT … ON CONFLICT DO NOTHING/UPDATE）或上游去重；确认约束本身是否该存在
- logrpt 要点：ERROR 大户；看首末时间与间隔规律比看单条有用

## 4. terminating connection due to administrator command —— FATAL 57P01

- 抛出点：〔已核〕`gausskernel/process/tcop/postgres.cpp:6635`
  （ERRCODE_ADMIN_SHUTDOWN）
- 触发：pg_terminate_backend / 运维 kill / 会话超时清理
- 排查：与 **Session unused timeout** 联判——〔已核〕
  `gausskernel/storage/lmgr/proc.cpp:3039` 先出 WARNING "Session unused timeout."，
  随后清理动作产生 FATAL terminating，两号成对出现 = session_timeout（VastBase 特有，
  杀空闲会话）在正常工作，不是事故
- logrpt 要点：谁在杀会话查 log_detail 里的执行者；空闲超时对（WARNING+FATAL
  同用户同时刻）直接归入配置行为

## 5. sorry, too many clients already —— FATAL 53300

- 抛出点：〔已核〕`gausskernel/storage/ipc/procarray.cpp:363`（会话注册时）与
  `process/postmaster/postmaster.cpp:4738`（fork/线程创建时）
- 触发：连接数达 max_connections / reserve 门槛
- 排查：连接来源分布（DB Process 的 TCP 状态与 Threads 增长）、应用侧连接池
  配置、泄漏；**不要用调大 max_connections 当第一反应**——先看真实并发
  （AAS vs 连接数差距 = 大量空闲连接）

## 6. deadlock detected —— ERROR 40P01

- 抛出点：〔已核〕`gausskernel/storage/lmgr/deadlock.cpp` DeadLockReport()
  （行号未逐行核）
- 触发：两会话互相持锁等待，deadlock_timeout 检测周期发现硬环
- 排查：日志 Detail 给出两个会话与两条 SQL（谁持什么等什么）；业务侧统一
  加锁顺序/缩短事务；deadlock_timeout 只影响检测频率不解决问题

## 7. canceling statement due to statement timeout / lock wait timeout —— ERROR 57014

- 抛出点：〔已核〕**统一取消处理器** `process/tcop/postgres.cpp:6694-6737`——
  定时器置 `cancel_from_timeout` 后走同一 ERROR 出口，消息按来源拼后缀：
  statement timeout / **autovacuum task timeout / txnsnap / cfs shrinker /
  WLM 异常 / space limit / default_transaction_read_only / 恢复冲突(:6778) /
  failover(:6811) / 协调端或用户取消(:6815)**——logrpt 判读看后缀分诊。
  **锁等待超时**：等待预算施加点已核 `storage/lmgr/lock.cpp:4060`（waitSec
  未指定时取 GUC lockwait_timeout，覆盖元组锁排队+xid 等待两段）；其消息
  拼装点在开源基线未定位（推断属同族或 VastBase 分支）
- 触发：statement_timeout 到期（语句执行超时）/ lockwait_timeout 到期（等锁超时，
  VastBase 特有）
- 排查：超时 ≠ 慢——是"超了配置的阈值被掐"。对照该 SQL 在 AWR 榜单的量级：
  真慢（单次成本高）调 SQL；假慢（阈值过紧）调参数语义。lock wait timeout
  与 transactionid 完整协议联判（`core-events-deep-dive.md` §4 深读版——
  注意还有 40001 concurrent update 可重试错误，语义不同勿混）

## 8. value too long for type character(n)/varchar(n) —— ERROR 22001

- 抛出点：〔已核〕`common/backend/utils/adt/varchar.cpp:153`
- 触发：输入超列宽。迁移自 Oracle 的系统常见（VARCHAR2 语义差异：VastBase 默认
  报错不静默截断，除非开兼容选项）
- 排查：定位列与来源值；要么修数据要么扩列宽，不建议开静默截断兼容（掩盖问题）

## 9. out of memory / memory is temporarily unavailable —— ERROR 53200

- 机制：进程级内存记账层拦截（ERRCODE_OUT_OF_LOGICAL_MEMORY，分配层多处）；
  与算子配额是两层（算子配额管 spill 决策，这层管还能不能分配）——完整机制见
  `operator-memory.md`
- 排查：出现即看 Memory GUCs 配比、会话内存大户、并发×算子配额总量；
  峰值并发时段的 Max Session Memory 是第一现场
- 〔排除提示〕cstore 压缩与 aes 加密路径有同名消息，默认环境不会走到

## 10. 磁盘/WAL 类：no space left / could not extend file —— ERROR 53000/53100

- 触发：数据/日志盘满、表空间满、temp 满
- 排查：AWR Filesystem Usage（pg_xlog/pg_arch 所在盘优先）；**复制槽 inactive
  会无限保 WAL 导致主库磁盘涨**（Replication Slots 段 active=f 即单独立案）；
  temp 满 → spill 负载（operator-memory.md）

## 11. relation "xxx" does not exist —— ERROR 42P01

- 触发：搜索路径不对 / 大小写（未加引号的标识符折小写）/ 表真没建 /
  跨库访问配错
- 排查：样本里 dsicp 报 t_yybb_match 不存在 = 集成侧引用了未部署的对象，
  与其认证失败同源（配置烂的集成）；确认 search_path 与对象存在性

## 判读总则（logrpt 通用方法）

1. **先看聚合节奏再看单条**：固定间隔恒定 = 轮询器/重试器问题，修配置一次清一类；
   随机散布 = 业务路径问题
2. **风暴阈值**：同码 >10 次/秒先报风暴再查根因
3. **FATAL 族先分三类**：认证类（1）/ 被杀类（4、5）/ 资源类——处置路径完全不同
4. **WARNING 不是噪音的同义词**：Session unused timeout、autovacuum 防抖这类
   WARNING 是配置行为的证词
5. 窗外错误移入"窗口外补充观察"，不作本窗证据

## BACKTRACELOG：报错附带回溯的机制（源码定论 2026-11-02）

日志里的 `BACKTRACELOG: tid[N]'s backtrace:` 是 **errfinish 的附带取证，不是崩溃**：

- 闸门（elog.cpp:583-598）：`elevel >= backtrace_min_messages` 时 errfinish 调
  `output_backtrace_to_log` 抓当前调用栈挂到消息上；递归输出中不重复抓
- GUC `backtrace_min_messages`（guc.cpp:4418，PGC_SUSET）：**发布版默认 PANIC**
  （assert 构建才是 ERROR）→ 现场打出回溯 = 该参数被调低过（如 WARNING/ERROR）；
  回溯存在≠故障级别高，级别看它附着的那条消息
- `force_backtrace_messages`（guc.cpp:877）："不该发生"路径在 ereport 前置位
  （heapam.cpp:1458/1474、catcache.cpp:1263、slru.cpp:881、md.cpp:1341/1398、
  postmaster.cpp:10866、postgres.cpp:6568/6679）——但**基线 6.0.0 中无任何读取点**
  （写而不读的死标志）；VB 定制分支是否消费未证实
- 解读操作与帧速查：`../troubleshooting/stack-reading.md`（含 relation_open 三路径判例）

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 单条 ERROR 逐条排查 | 同族错误几百条通常一个根因 | 按聚合组看节奏与首末 |
| 把 session_timeout 清理当事故 | WARNING+FATAL 成对是配置行为 | 联判归配置 |
| 超时错误当慢 SQL | 超时是被掐不是执行慢 | 对照 AWR 单次成本定真假 |
| 只修不问监控采集失败 | 监控半失效会掩盖真问题 | permission denied 类必查监控完整性 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；〔未逐行核〕条目待后续精读补锚
- VastBase 特有项：lockwait_timeout、session_timeout（Session unused timeout）

## 来源

- 源码核对（2026-10-25）：auth.cpp:289、user.cpp:5740、proc.cpp:3039、
  postgres.cpp:6635、procarray.cpp:363、postmaster.cpp:4738、
  opfusion_insert.cpp:353、varchar.cpp:153、deadlock.cpp（DeadLockReport）
- 实测样本：2026-10-20/21 现场 logrpt（重复键/认证/权限三台"错误机器"）
- 机制联动：`operator-memory.md`（53200）、`core-events-deep-dive.md`（transactionid）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
