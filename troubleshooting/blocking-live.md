# 锁等待/阻塞现场快速定位（blocking-live）

## 概述

> 服务分类：troubleshooting（现场救火）。**第一优先是止损与现场保全，然后
> 才是定位**。适用于：业务大面积挂起、会话堆积、疑似死锁、疑似"某个会话
> 卡住所有人"。报告驱动的锁分析走 `../performance/awr-report-analysis.md`。

## 处置流程（顺序执行，先保现场再动手）

### 1. 保全现场（30 秒内做完，改动现场前必做）

```sql
-- 等待链两表联查（快照存档，事后分析全靠它）
SELECT * FROM pg_thread_wait_status;          -- 线程级等待状态（基线视图）
SELECT pid, sessionid, usename, state,
       query_start, state_change, query
FROM pg_stat_activity
-- ★基线 pg_stat_activity 无 wait_event_type/wait_event 列（社区 PG 才有）；
--   等待详情用上面 pg_thread_wait_status（wait_status 判锁类型）
WHERE state <> 'idle' ORDER BY query_start;   -- 会话与语句现场
```

OS 侧同步留证：`ss -tnp | tee` 一份（假死连接排查，见 `../linux/network.md`）。

### 2. 判形态（三选一）

| 形态 | 判据 | 走向 |
|---|---|---|
| 行/表锁阻塞链 | wait_status 含 Lock（pg_thread_wait_status），一个源头会话后面挂一串 | 第 3 步 |
| LWLock/轻锁自旋 | wait_status 含 LWLock，CPU 还高（自旋不睡眠） | `../internals/locks.md`；VastBase 专有 `pg_stat_get_backend_lwlocks()` join sessionid 拿持锁 SQL（env-A 实测，基线源码无此函数） |
| 假死持锁 | 源头会话"消失"（应用已重连）但锁不释放 | `ss -tnp` 查对端状态/重传计数——内核要 ~15 分钟才放弃重传（`../linux/network.md` 假死连接节） |

### 3. 锁阻塞：拿持锁会话与 SQL

- prepared 事务持锁的**唯一可靠判据**：`pg_locks` 侧 `pid IS NULL` 且
  vxid 形如 `-1/<xid>`——两条件缺一不可（虚拟事务号负一是 prepared 特征，
  仅 pid IS NULL 会误伤后台进程）
- 死锁环交给内核检测器：`deadlock_timeout`（默认 1s）后自动 40P01 报错
  回滚一端——**只有硬环才报**（软环/多方阻塞不报，见
  `../internals/locks.md` 软死锁重排节）
- 取证工具：`vb_tool lockchain`（含 prepared 判据修正版）；
  `vb_tool se <event>` 查事件语义（417 事件离线库）

### 4. 止损（按破坏性从低到高）

1. `SELECT pg_cancel_backend(pid);` —— 只取消当前语句（锁通常不释放）
2. `SELECT pg_terminate_backend(pid);` —— 杀会话放锁（首选；线程模型下
   影响面=该会话自身）
3. prepared 事务：`COMMIT PREPARED/ROLLBACK PREPARED '<gid>'`（不能 kill，
   它没有进程——这就是第 3 步判据重要的原因）
4. 假死会话：terminate 即可放锁，无需等内核重传超时
5. **最后手段**才考虑重启——先确认现场三份存档已落盘

## 机制依据（一句话版，细节在对应文件）

- 行锁排队无唤醒交接，释放靠持锁者事务结束（`../internals/locks.md`）；
  行锁等待时长≈顶层事务剩余执行时间——**阻塞源头永远从"谁开事务最久"查起**
- LWLock 等待队列 FIFO，但释放不交接，且高争用下自旋烧 CPU 无等待事件
  （`../internals/wait-events.md` 自旋盲区）
- 取消与报错家族（57014/40P01）语义：`../internals/error-fatal-mechanics.md`

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 只看 pg_locks 不看事务开始时间 | 阻塞源头=最长事务 | join query_start/pg_xact_status 排序 |
| prepared 事务只判 pid IS NULL | 后台进程也 pid NULL | 加 vxid `-1/<xid>` 判据 |
| 死锁检测器没报就断定无环 | 只有硬环报 40P01 | 软环靠等待链快照人肉判 |
| 先杀会话后保全现场 | 现场一杀就没了 | 第 1 步快照永远先行 |
| 杀完源头就收工 | 源头为什么会持锁这么久没答案 | 补长事务根因（`../performance/bottleneck-patterns.md`） |

## 版本注记

- pg_thread_wait_status 为基线视图（system_views.sql 实锤）；列集随版本有
  漂移，现场以 `\d pg_thread_wait_status` 为准
- pg_stat_get_backend_lwlocks 为 VastBase 专有（env-A vastbase 用户库实测），
  开源基线无

## 来源

- 基线 openGauss 6.0.0 源码：common/backend/catalog/system_views.sql（视图定义）、
  gausskernel/process/postmaster（会话/线程模型）
- env-A vastbase 用户库实测（LWLock 视图、lockchain 配方，2026-10 系列）
- vb_tool lockchain/se 行为以 vb_tool 源码为准

## 知识网

本文件为操作层，机制边见 `../KNOWLEDGE-GRAPH.md`：locks（队列/交接）、
error-fatal（40P01/57014）、network（假死持锁）、wait-events（自旋盲区）。
