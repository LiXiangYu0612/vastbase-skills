# 连接生命周期（postmaster 接受 → 线程孵化 → 启动包 → 认证 → 会话就绪）

## 概述

> 服务分类：troubleshooting（连接慢/连不上定位的内核侧）、performance（连接
> 建立成本）、linux（TCP 层联动）。操作手册走
> `../troubleshooting/connection-refused.md`（四道闸）；本文件讲闸门背后的
> **库内核怎么处理一条新连接**，以及它与 Linux 两级连接队列的交界。

## 全链路总图（一次 TCP 连接的库侧旅程）

```
客户端 SYN → OS 半连接队列 → SYN-ACK → 全连接队列(accept 队列)   ← Linux 层
   ↑ 界面：postmaster 是唯一的 accept 消费者（单线程 poll）
postmaster ServerLoop (postmaster.cpp:3870) poll 到达
 → ConnCreate (:5559)：accept(StreamConnection pqcomm.cpp:1005)
    + 预生成 md5Salt（RAND_priv_bytes，必须在孵化线程前——保证每连接盐不同）
 → BackendStartup (:9086)：准入闸 canAcceptConnections → CAC_TOOMANY/CAC_OOM
    直接拒绝（accept 成功≠连得上！）→ initialize_worker_thread(WORKER, port)
    ＝每连接一个工作线程
 → 新线程：ProcessStartupPacket (:4639) 解析启动包（协议版本/user/database/
    运行参数；SSL 请求在此协商重入 :4827）
 → ClientAuthentication（common/backend/libpq/auth.cpp：盐质询-应答，
    pg_hba 规则匹配）
 → InitPostgres/会话初始化（打开库、进程注册、syscache、GUC 会话级）
 → ReadyForQuery —— 客户端才真正"连上"
```

## 准入闸：accept 成功也可能被拒（判读关键）

BackendStartup 的 `canAcceptConnections` 闸（postmaster.cpp:9086-9135，深读）：

| 状态 | 日志文案（源码原文） | TCP 层表现 |
|---|---|---|
| CAC_TOOMANY | "could not fork new process for connection due to **too many connections**" | accept 已完成（3 次握手成功）后关闭——客户端先连上再被断，与四道闸的"库上限"对应 |
| CAC_OOM | "The server is cleaning connection to reduce memory usage, please retry later" | **内核在内存紧张时主动清连接+拒新连接**（openGauss 自保机制）；与 OS 层 OOM kill 是两个层面（见 `memory.md` 两层闸门） |
| CAC_SHUTDOWN | "…postmaster is shutting down" | 关停期新连接拒绝 |

**判读**：客户端"连上又掉"先查服务端 WARNING 这三条文案，别只盯 TCP。

## postmaster 单线程 accept 的 TCP 联动（与 network.md 的接缝）

- **accept 队列的唯一消费者是 postmaster 主循环**（ServerLoop poll →
  ConnCreate 逐个 accept）——连接风暴时它的消费速率就是全连接队列的排空
  速率：`ss -lnt` Recv-Q 堆高 + ListenOverflows 涨 = postmaster 吃不动
  （每连接成本：accept + 盐生成 + 线程孵化），而**不是网络问题**
  （`../linux/network.md` 两级队列节的"应用 accept 不及时"在库里就是这一条）；
  全队列堆积时内核侧 SYN-ACK 重传空转推高 sys/si 的完整连锁见
  `../linux/network.md` 全队列堆积节
- 每连接一个工作线程（initialize_worker_thread；fork 失败有
  "could not fork new process" WARNING + report_fork_failure_to_client 通知
  客户端）——线程孵化是毫秒级成本，连接风暴下与 OS 队列双重放大
- 线程池（enable_thread_pool）是另一种会话-线程映射，属不稳定特性：仅作
  环境事实记录，治理一律走应用侧连接池（域规矩）

## 认证与会话初始化的成本点

- **盐质询-应答**：ConnCreate 预生成 md5Salt（postmaster.cpp 注释：必须在
  孵化前生成，否则所有线程共享同一盐）——认证至少一个额外往返；认证方式
  由 pg_hba.conf 行决定（reject/trust/md5/sha256 质询…）
- **ClientAuthentication 结构**（auth.cpp:350，深读）：hba_getauthmethod 匹配
  pg_hba 行 → clientcert 证书预校验（要求证书而无证书 → FATAL
  "connection requires a valid client certificate"）→ 按方法进质询-应答
- **认证超时**：`authentication_timeout` 存在，**env-A 实测默认 1min**
  （2026-10-25 真机；此前按 guc.cpp 检索判"无此 GUC"是**错的**——其定义
  不在 guc.cpp，该负向结论已被实测推翻并修正）——半开认证连接有库层
  超时兜底
- **别名穷查终局（⚓收口，2026-11-05，6.0.0 树 grep 实取）**：
  AuthenticationTimeout **不是别名——它直接存在**。GUC 名
  `authentication_timeout` 注册于 **misc/guc/guc_security.cpp:439-452**
  （类别 PGC_SIGHUP、组 CONN_AUTH_SECURITY、默认 **60s**=env-A 实测
  1min 吻合、范围 1–600s；变量行 :446，声明 knl_session_attr_security.h:51）。
  此前检索落空的正因：6.0.0 树 GUC 表已拆为 guc/ 下六个分表，单查
  guc.cpp 必漏（authentication_timeout 恰在 guc_security.cpp）。执行点
  common/backend/utils/init/postinit.cpp:301 PerformAuthentication——
  认证交换前 `enable_sig_alarm(AuthenticationTimeout×1000)`（:315）覆盖
  fn_authenticate 全程，借 statement_timeout 基础设施兜底。**穷查范围**：
  guc/ 六分表+guc.cpp 共 **37 个**名字含 timeout 的 GUC 条目逐项判层
  ——①PGC_POSTMASTER 类 3 项（streaming_batch_timeout=流水线微批接收、
  dcf_socket_timeout/dcf_connect_timeout=paxos 复制层）；②CONN_AUTH 组
  仅 authentication_timeout 1 项；③connect 族 5 项全非端侧建连层
  （pooler_timeout/pooler_connect_timeout/pooler_cancel_timeout=CN→DN
  节点间通信、wal_receiver_connect_timeout=备机连主机、
  gtm_connect_timeout=连 GTM）；④ssl 家族 16 项 GUC 名零 timeout 命中
  （最接近的 ssl_renegotiation_limit 是流量阈值且描述明言 renegotiation
  已废弃）；⑤tcp_user_timeout=已建连的 TCP 重传层——**结论：除
  authentication_timeout 外，37 项中无一作用于建连/认证/SSL 握手层**
- 启动包（ProcessStartupPacket:4639）：协议协商、SSL 请求重入（:4827）、
  user/database 与运行参数；参数在此即进入会话 GUC——"连上就慢"排查时
  先核启动包里应用带了什么参数
- 会话初始化（InitPostgres 族）：打开库 + 进程注册 + syscache 装载——
  大量短连接的固定成本来源；根治=应用侧连接池复用会话

## 只读 SELECT 自动提交与 compat_oracle_txn_control（2026-11-04 入库）

**背景语义**：Oracle 默认非自动提交且 SELECT 不开事务；PG/VastBase 是强事务
模型——**驱动设为非自动提交时，所有语句（含只读 SELECT）都显式开启事务**，
不手动提交就会话停在 idle in transaction，最终被
idle_in_transaction_session_timeout 杀连接（应用侧表现为 has lost connection
类断连，且程序内置重试会掩盖规律）。

**compat_oracle_txn_control**（VastBase 定制 GUC，3.0.8.4+；**开源 openGauss
6.0.0 基线无此参数——定制分支差异，源码以 VastBase 分支为准**）：驱动非自动
提交时，只读 SELECT 隐式开启并提交（不落显式事务）；DML 之后已进显式事务则
不再自动提交。**驱动默认值差异是事故点**：JDBC/Python 默认开，**ODBC 默认关**
——ODBC+非自动提交+无手动提交 = 必然 idle in transaction。

驱动参数 compatOracleTxnControl 开启后的自适应：自动提交模式下若内核
behavior_compat_options 已含 compat_oracle_txn_control 选项，驱动自动擦除；
手动提交模式下若不含则自动添加（驱动查内核选项后双向对齐）。配套：`SET
autocommit=off` 语句仅 B 兼容模式生效；RR 隔离级别下隐式提交行为受隔离语义
影响。排查配方：`select name,setting from pg_settings where name ~ 'timeout'`
先核超时参数族，再看会话状态 idle in transaction 与驱动类型。

## 判读速查（现象 → 证据 → 机制指向）

| 现象 | 证据 | 机制指向 |
|---|---|---|
| 连上又立刻断 | 服务端 WARNING 三条文案 | 准入闸表（CAC_TOOMANY/OOM/SHUTDOWN） |
| Recv-Q 堆高 + ListenOverflows 涨 | ss -lnt + nstat | postmaster accept 消费速率（本文件） |
| 认证慢/超时 | 日志认证失败记录 + 网络往返 | pg_hba 方式与认证超时；网络 RTT |
| 建连延迟稳定几十毫秒 | 应用侧连接模式 | 每连接固定成本：线程孵化+会话初始化 |
| 内存紧张期拒连 | CAC_OOM 文案 | 内核自保清连接；配 `memory.md` 两层闸门 |
| 长连接业务随机断连+重试掩盖 | 会话曾处 idle in transaction + 驱动类型 | 超时参数杀会话；ODBC 默认关 compatOracleTxnControl（本节） |

| 现象 | 证据 | 机制指向 |
|---|---|---|
| 连上又立刻断 | 服务端 WARNING 三条文案 | 准入闸表（CAC_TOOMANY/OOM/SHUTDOWN） |
| Recv-Q 堆高 + ListenOverflows 涨 | ss -lnt + nstat | postmaster accept 消费速率（本文件） |
| 认证慢/超时 | 日志认证失败记录 + 网络往返 | pg_hba 方式与认证超时；网络 RTT |
| 建连延迟稳定几十毫秒 | 应用侧连接模式 | 每连接固定成本：线程孵化+会话初始化 |
| 内存紧张期拒连 | CAC_OOM 文案 | 内核自保清连接；配 `memory.md` 两层闸门 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| accept 成功=连接没问题 | 准入闸在 accept 之后（CAC_TOOMANY 等） | 查 postmaster WARNING 文案 |
| Recv-Q 堆高当网络故障 | 唯一消费者是 postmaster 单线程循环 | 库侧成本（线程孵化）+ 应用池化 |
| 认证慢只查密码对错 | 质询-应答有额外网络往返+超时预算 | 分层：RTT/方式/超时 |
| 把 CAC_OOM 当数据库 bug | 是内核内存自保机制 | 查内存压力源（`memory.md`） |
| SELECT 断连当网络/驱动 bug | 非自动提交下 SELECT 也开事务，超时被杀是设计 | compat_oracle_txn_control 一节；核驱动类型默认值 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；每连接一线程为该基线默认形态；
  判读以源码锚点为准，行号会漂
- 线程池形态（会话-线程解耦）在不稳定特性清单内，不作判读主体

## 来源

- 基线 openGauss 6.0.0 源码精读（2026-10-25）：postmaster.cpp:3870
  （ServerLoop）、:4090-4128（ConnCreate/BackendStartup 调用与失败清理）、
  :5559-5612（ConnCreate：accept+盐预生成注释）、:9086-9145（BackendStartup：
  准入闸三态+initialize_worker_thread）、:4639,4827,5152（ProcessStartupPacket）；
  pqcomm.cpp:1005（StreamConnection=accept）；common/backend/libpq/auth.cpp:350-395
  （ClientAuthentication：hba 匹配+证书预校验+FATAL 文案）
- 真机实测（2026-10-25 env-A）：authentication_timeout 默认 **1min**；
  tcp_keepalives_idle/interval/count 默认 **0=跟随内核**；
  idle_in_transaction_session_timeout 默认 **0=禁用**；max_connections=500
- GUC 表全量穷查（2026-11-05，6.0.0 树）：misc/guc/ 六分表+guc.cpp 共
  37 个 timeout 条目逐项判层（正文"别名穷查终局"节）；
  authentication_timeout 注册 guc_security.cpp:439-452、执行点
  postinit.cpp:301（enable_sig_alarm :315）
- TCP 层：内核 7.1-rc4 精读结论复用（`../linux/network.md` 来源节）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`；互喂边：network（两级队列
唯一消费者）、connection-refused（操作层四道闸）、memory（CAC_OOM×两层闸门）、
metrics（会话/Threads 口径）。

案例锚点：`../cases/case-env-odbc-idle-in-txn-202603.md`（idle in transaction 超时杀会话判例）
