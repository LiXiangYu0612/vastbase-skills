# 日志消息级别：log_min_messages/client_min_messages 两条排序线（PG 源码定论）

## 概述

> 服务分类：admin（GUC 语义）；troubleshooting（"不想打 log 怎么办"降噪决策）。
> 起源：2026-11-25 用户问"级别哪个高？看不出来"——`set log_min_messages` 的
> ERROR HINT 列表（debug...info, notice, warning, error, log, fatal, panic）
> **只是枚举列举，不是严重度排序**。本文以 PG17.10 源码钉死两条排序线与
> LOG 的乱序特判（VB/openGauss 同血统同源；VB 侧如需可 204 实测复核）。

## 级别常量（elog.h:26-56，数值低=级别低）

DEBUG5=10 < DEBUG4 < DEBUG3 < DEBUG2 < DEBUG1=14 < **LOG=15** <
LOG_SERVER_ONLY=16 < INFO=17 < NOTICE=18 < WARNING=19 < WARNING_CLIENT_ONLY=20
< ERROR=21 < FATAL=22 < PANIC=23。

## 两条排序线（同一组名字，顺序不同）

| 线 | 排序（高→低） | 依据 |
|---|---|---|
| **log_min_messages**（进服务日志） | PANIC > FATAL > **LOG > ERROR** > WARNING > NOTICE > INFO > DEBUG1~5 | elog.c:204-225 `is_log_level_output` 特判，注释原文 *"LOG to sort out-of-order, between ERROR and FATAL"* |
| **client_min_messages**（发客户端） | PANIC > FATAL > ERROR > WARNING > NOTICE > INFO > DEBUG1~5（**LOG 数值序排最底**，比 INFO 低） | elog.h 数值常量直接 >= 比较 |

特判逻辑（elog.c:204-225）：
- LOG/LOG_SERVER_ONLY 消息：`log_min_level == LOG` **或** `log_min_level <= ERROR` 才进日志
- 其他消息：纯数值 `elevel >= log_min_level`
- 设 log_min_messages=LOG 时只有 FATAL+PANIC 过（LOG 乱序档的镜像）
- WARNING_CLIENT_ONLY 永不进日志

**默认值 WARNING**（guc_tables.c:4866）——所以默认下 LOG 消息（checkpoint
起停/autovacuum 等）进日志：LOG 比设定的 warning"高"。合法值枚举表在
guc_tables.c:150（"debug" 是 debug2 的别名，hidden=true）。

## "不想打 log"降噪决策（2026-11-25 用户问）

| 诉求 | 设法 | 结果 |
|---|---|---|
| 压 LOG 级运营消息、保留 ERROR | **log_min_messages 单独做不到**（特判：设 ≤error 时 LOG 恒进日志） | — |
| 真全静音 | `log_min_messages='fatal'` | 只剩 FATAL/PANIC，ERROR 盲区，不建议 |
| **正确降噪** | log_min_messages 不动，**关来源开关**：log_checkpoints=off / log_autovacuum_min_duration=-1 / log_lock_waits=off / log_min_duration_statement=-1 / log_connections=off | 精准不丢错误 |
| 冷知识档 | `log_min_messages='log'` | **只留 LOG/FATAL/PANIC、压掉 ERROR**（镜像副作用，别误用） |

## 误区表

| 误区 | 定论 |
|---|---|
| 按 HINT 列表顺序记级别 | HINT 是枚举列举非排序；真实序见上表 |
| "设 error 就能压掉 LOG 消息" | 压不掉——is_log_level_output 特判 log_min_level≤ERROR 时 LOG 恒真 |
| LOG 在两条线里位置相同 | 不同：日志线 LOG>ERROR，客户端线 LOG<INFO |
| 越安静的 log_min_messages 越好 | 设 fatal=ERROR 盲区；降噪先关来源开关 |

## 来源

- PG17.10（/home/xiang/pg/postgresql-17.10，2026-11-25 精读）：
  src/include/utils/elog.h:26-56（级别常量）、
  src/backend/utils/error/elog.c:196-225（is_log_level_output 乱序特判）、
  src/backend/utils/misc/guc_tables.c:150-164（合法值枚举）/:4860-4866
  （定义与默认 WARNING）
- VB/openGauss 同血统（分叉自 PG12 族，elog 语义同源）；VB 侧实测复核可选
