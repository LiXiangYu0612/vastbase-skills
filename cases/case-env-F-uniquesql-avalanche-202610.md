# 案例：外部监控工具打崩库——UniqueSQLMappingLock 全分区雪崩（env-F，2026-10-26）

## 环境

- env-F 生产库 node1（144 逻辑核，primary）；库：yundocdb
- 样本：四窗（10-26 08:30~09:30），awrrpt；样本目录 workspace/inbox/dimo_1026/
- 采集版：有 statio/Softnet、无 Pagewriter 章（总览按规则不出该行）

## 症状

等待型重载：每核并发 7.2 / 24.0 / 9.7 / 11.3（四窗），而 DB CPU/DB Time 仅
2~5%——**会话全在等，不在算**。08:40 窗 AAS 雪崩到 3456（ASP 桶峰值 1122、
wait_lwlock 桶 92），业务高峰期雪上加霜。

## 排查链（六步流程 + 三次源码定论）

1. 画像：等待型重载定性——排除 CPU 型，进锁/LWLOCK 分型
2. 事件：Top 等待三连——UniqueSQLMappingLock / SInvalWriteLock /
   BufferContentLock，全部 LWLock（ASP waitchain 对 LWLock 无阻塞关系——
   走事件定量+源码，wait-events 分型规则）
3. **逐锁源码定论（知识库当时零覆盖，当日回灌 wait-events"实测新锚"节）**：
   - UniqueSQLMappingLock=instr_unique_sql 哈希分区锁（instr_unique_sql.cpp:126），
     最重路径 **reset_unique_sql()（:2616）→ CleanupAllUniqueSqlEntry（:2328）
     一次拿全部分区 EXCLUSIVE + InitUniqueSQL 重建**——08:40 窗
     avg 6.87s×5184 次=35,607s，与 AAS 雪崩 3456 同窗
   - SInvalWriteLock=共享失效队列写锁（sinvaladt.cpp:84-97/:282）——失效消息
     洪流，与 pg_sqlplan 物理读王 + Plan Time 占比 9~14% 同链
     （w1 4749.6s/1,517 次/avg 3130.89ms）
   - BufferContentLock=热页争用（122.9ms×139,570 次=17,159s，与逻辑读王
     app_company 26.8M 块/窗呼应）
4. **谁在调 reset_unique_sql**：vb_tool 源码全量 grep 0 命中=不是自家采集工具；
   同窗 SQL 榜抓到一台外部监控机在查**昨日 ASP 的递归 COPY**——
   工具指纹反查（监控工具周期性清 UniqueSQL 统计重建）
5. 次级证据链：WALFlushWait 139,285 次×3.45ms=commit 风暴（7.9K commits/s）；
   死锁 1/0/0/4；docadmin 应用侧错误（唯一键 292→2290、权限族、被踢 183）
6. 定性：主因=外部工具 reset_unique_sql 雪崩；次因=计划缓存失效洪流/
   热页/commit 风暴/应用侧错误（四个独立改进项）

## 根因

外部监控工具周期性触发 reset_unique_sql（全分区 EXCLUSIVE 重建 UniqueSQL
哈希），持有期全部活跃语句的 instr 写路径排队——AAS 雪崩。库本身配置无异常。

## 处置与结果

建议优先级：**停用外部工具的 reset_unique_sql 行为（最优先）→
instr_unique_sql_count 核对收敛 → app_company 热页 → 计划缓存收敛（SInval 链）
→ docadmin 应用侧三修**。产物：四窗逐窗报告（D:\log+输入同目录各一份）。
〔处置结果未回访⚓〕

## 预防

- 监控工具接入清单加两条：**不触发 UniqueSQL 重置类操作**；接入后观察
  一个高峰窗的 wait_lwlock 桶
- 事件定量+源码定论是 LWLock 雪崩的标准路径（waitchain 帮不上 LWLock）——
  见 wait-events 分型规则

## 关联知识文件

`../performance/wait-events.md`（三锁"实测新锚"节即本案例回灌）、
`../performance/awr-report-analysis.md`（等待型重载画像）、
`../performance/diagnostic-map.md`（症状4 LWLOCK 分型：堆栈+源码路线）、
`../performance/log-analysis.md`（docadmin 应用侧归责）、
`../performance/metrics.md`（DB Time 口径/每核并发判读）

## 遗留

- 处置后复测样本缺失〔⚓〕
- app_company 热页优化与计划缓存收敛未单独复盘
- 外部监控工具身份（厂商/版本）未落档——只锁定了行为指纹
