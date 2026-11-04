# 案例：ODBC 非自动提交长连接随机断连（SELECT idle in transaction 被超时杀）

## 环境（中性化）

Vastbase G100 A 兼容模式（3.0.8.x 档）；应用 ODBC 驱动默认参数、长连接、
不定期执行 SELECT；业务代码非自动提交且无手动提交逻辑。2026-03 内部分享案例
（用户提供 D:\二线团队，2026-11-04 入库）。

## 症状

- 业务侧无固定频率报连接丢失（部分为业务自定义日志包装），程序内置重试
  （自动重建连接再查）掩盖规律
- 数据库侧无崩溃；被杀会话此前处于 idle in transaction

## 排查链

1. 报错特征收集：核心为连接被服务端终止；触发无规律 → 先怀疑被库主动 kill
2. `select name,setting from pg_settings where name ~ 'timeout'` 核超时参数族
   → **idle_in_transaction_session_timeout=1800s**（现场配置），其余无特殊
3. 报错日志特征对上"SELECT 执行完处于事务中" → 定位：SELECT 开了显式事务，
   30 分钟无提交被参数杀
4. 驱动差异核对（关键分叉）：JDBC/Python 驱动 compatOracleTxnControl 默认开，
   **ODBC 默认关** → 只有 ODBC 业务踩坑的原因
5. 复现（reproduce_issue1.c，ODBC + SQL_AUTOCOMMIT_OFF + SET
   idle_in_transaction_session_timeout='10s' + SELECT + sleep 25s）：报错与
   现场一致
6. 参数验证：连接串加 CompatOracleTxnControl=1 → 无报错
7. 后续（保留弯路）：客户不接受手工配置 → 研发出临时驱动包；首版仍有同报错
   → 开 ODBC debug=3 + CommLog=1 定位到"驱动获取内核参数配置分割符"的驱动
   bug → 修复后正常

## 根因

驱动非自动提交 + ODBC 默认 compatOracleTxnControl=off → 只读 SELECT 显式开
事务且无人提交 → idle in transaction 超 1800s 被
idle_in_transaction_session_timeout 终止（机制：
`../internals/connection-lifecycle.md` 只读 SELECT 自动提交一节；
compat_oracle_txn_control 为 VastBase 定制 GUC，开源基线无）。

## 处置与结果

- 临时：ODBC ini 连接串加 `CompatOracleTxnControl=1`（步骤 6 验证有效）
- 根治：驱动内核适配（默认自动处理 SELECT 事务）+ 修复驱动读取内核
  behavior_compat_options 的解析 bug
- 长期：推动 JDBC/Python/ODBC 三驱动参数默认值统一

## 预防

- ODBC + 非自动提交业务：上线清单必查 compatOracleTxnControl 与
  idle_in_transaction_session_timeout 的组合
- 监控项：idle in transaction 会话年龄（超阈值告警先于被杀）
- 迁移自 Oracle 的应用注意：PG 系强事务模型下 SELECT 也占事务（语义差异清单）

## 关联知识文件

- `../internals/connection-lifecycle.md`（compat_oracle_txn_control 机制+
  判读行；本案例为其案例锚点）
- `../troubleshooting/connection-refused.md`（连接被踢族分诊）
- `../performance/log-analysis.md`（会话终止类日志归族）

## 遗留

- RR 隔离级别下隐式提交的边界行为仅图证未复演⚓
- 驱动解析 bug 的技术细节（配置分割符）无源码锚，仅文档记录⚓
