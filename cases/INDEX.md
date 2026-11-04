# 案例索引（MOS 目录）

| 案例 | 症状关键词 | 根因一句话 | 关联知识文件 |
|---|---|---|---|
| [case-env-A-enospc-202610](case-env-A-enospc-202610.md) | 实例被压垮、日志写不进、疑似崩溃 | 根分区 ENOSPC 挡住日志/xlog 写路径 | `../troubleshooting/crash-panic.md`、`../troubleshooting/startup-failure.md`、`../troubleshooting/resource-exhaustion.md` |
| [case-envC-awr-lowload-202610](case-envC-awr-lowload-202610.md) | 库空载但 OS 忙、%DBtime>100、gs_asp 膨胀、HDD 双写慢 | 三因并列：空载属实；5.8 天残留长事务钉 OldestXmin；HDD 介质形态 | `../performance/metrics.md`、`../internals/autovacuum.md`、`../internals/mvcc-visibility.md`、`../internals/buffer-manager.md`、`../linux/io-storage.md` |
| [case-envD-errstorm-202610](case-envD-errstorm-202610.md) | AWR 全绿但业务说慢；日志 ERROR 3500+/h、FATAL 840+/h | 三场错误风暴全在应用/监控侧：认证失败循环 600/h、撞唯一键 2770/h、监控账号权限错 1100/h | `../performance/log-analysis.md`、`../internals/error-fatal-mechanics.md`、`../internals/connection-lifecycle.md`、`../linux/network.md` |
| [case-env-odbc-idle-in-txn-202603](case-env-odbc-idle-in-txn-202603.md) | ODBC 长连接随机断连（重试掩盖规律）、会话曾处 idle in transaction | 非自动提交下只读 SELECT 显式开事务无人提交，idle_in_transaction_session_timeout=1800s 杀会话；ODBC 驱动 compatOracleTxnControl 默认关（JDBC/Python 默认开） | `../internals/connection-lifecycle.md`、`../troubleshooting/connection-refused.md` |
| [case-env-E-sys-spike-archiver-fork-202611](case-env-E-sys-spike-archiver-fork-202611.md) | sys% 间歇性冲高数秒、周期≈归档节奏、AWR 无异常、worker 全体同涨 | 主进程（线程模型单 mm）的 archiver 线程 fork（归档命令走 gs_popen_security，pgarch.cpp:853）触发：页表复制 3.2s + 283MB COW 风暴 + 144 核 TLB 广播 + exec 拆址 5.4s | `../linux/cpu.md`、`../linux/tracing-tools.md`、`../backup-recovery/pitr-mechanism.md` |
