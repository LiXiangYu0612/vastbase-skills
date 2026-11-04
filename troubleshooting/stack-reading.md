# 堆栈解读现场（stack-reading）

## 概述

> 服务分类：troubleshooting。拿到日志里的 **BACKTRACELOG 回溯**或任何一条内核调用栈，
> 读出"哪个线程、在干什么、错在哪一步"。操作方法论 + 帧速查 + 现场四步。
> 机制层（errfinish 为什么打回溯）见 `../internals/error-fatal-mechanics.md` BACKTRACELOG 节；
> AWR 采样 Callstack 的判读（数据形态完全不同）见 `../performance/wait-events.md`。

## 三种栈来源（先分清手里的是哪种）

| 来源 | 形态 | 回答的问题 |
|---|---|---|
| **BACKTRACELOG**（本文主对象） | 日志里 `tid[N]'s backtrace:` + 函数帧列表，紧跟某条 ERROR/WARNING | 这条报错是从哪条调用路径抛出来的 |
| AWR Callstack 采样 | `FuncA()+0x1ec <- CallerB()+0x4e4`，1 采样/5 分钟/线程 | 那段时间线程在干什么（等待/忙） |
| core / gdb | 崩溃现场全量栈 | 段错误死在哪一行 |

## 通用读法五步

1. **自底向上读**（clone 起步 → 线程入口 → 主循环 → 查询入口 → 执行器 → 出事点）。
   底部几帧回答"谁"（什么角色线程、什么协议入口），顶部几帧回答"错在哪"
2. **顶部是 errfinish / output_backtrace_to_log** ⇒ 这是报错回溯，不是卡住也不是崩溃；
   真正的报错点 = errfinish **下面**那一帧（ereport 的调用者）
3. **匿名帧 `vastbase() [0x...]`** 三种身份：static 函数（无导出符号，如 exec_simple_query）、
   执行器钩子（hook 指针调用）、模板/内联展开。按上下文夹层推断身份，别跳过
4. **mangled 名还原**：`_Z9heap_openjis` = `heap_open(unsigned int/*Oid*/, int/*LOCKMODE*/,
   short/*int2 bucketid*/)`——参数类型串（j/i/s/l/P…）能直接读出函数签名，VB 特有的
   第三参 bucketid（默认 -1）就是这么认出来的
5. **偏移不跨版本比对**：`+0x222` 只在本二进制内有意义，归并/检索一律剥掉；要行号用
   现场同版本二进制 `addr2line -e vastbase -f -C 0x...`

## 帧速查

**GaussDbThreadMain\<N\>（gs_thread.h:56 枚举，N=角色）**：

| N | 角色 | 含义 |
|---|---|---|
| 0 | MASTER_THREAD | 主进程 |
| 1 | WORKER | **前台会话线程**（每连接一线程）——业务 SQL 报错都在这 |
| 2 | THREADPOOL_WORKER | 线程池工作者（⚠️ 源码不精读，仅识别用） |
| 6 | STREAM_WORKER | 并行流算子工作者 |
| 7/8 | AUTOVACUUM_LAUNCHER/WORKER | autovacuum |

**常见夹层**（自底向上典型次序）：

| 帧 | 含义 |
|---|---|
| `PostgresMain` | 会话主循环 |
| 匿名帧 → `PortalStart` | 多为 exec_simple_query（postgres.cpp:2414，static）——简单协议收到一条 SQL |
| `ExecutorStart` → 匿名帧 → `standard_ExecutorStart` | 匿名帧 = ExecutorStart_hook（execMain.cpp:221 分派钩子后再落 standard_；VB 内建钩子，具体身份推断） |
| `InitPlan` → `ExecInitNode` 递归 | 执行器初始化阶段，还没取第一行数据；**嵌套次序就是计划树形状** |
| `ExecInitRecursiveUnion / SubqueryScan / HashJoin / SeqScan…` | 算子初始化链，按栈读出查询形态（例：递归 CTE 包子查询包哈希连接内层全表扫） |
| `ExecOpenScanRelation → heap_open → relation_open` | 初始化时打开扫描目标表（默认 AccessShareLock，execUtils.cpp:1105） |

## BACKTRACELOG 现场四步

1. **先找同时间戳相邻的那条 ERROR/WARNING 行**——回溯是附着在真实报错消息上打的，
   errmsg 一锤定音；只看堆栈永远只能猜
2. **栈顶定族**：errfinish 下一帧 = 报错点函数，对照该函数源码里所有 ereport 路径收窄
   （例：relation_open 共三条 ERROR 路径，见判例）
3. **按消息取证**：OID 查 pg_class / statement_history 找对应 SQL / tid 关联 pg_os_threads
4. **口径核对**：`backtrace_min_messages` 发布版默认 PANIC（guc.cpp:4418）——现场能打出
   回溯说明该 GUC 被 DBA 调低过（SUSET），或走了强制路径（见机制节）；回溯本身≠故障级别

## 判例：递归 CTE 初始化期打不开表（2026-10-29 现场生产，脱敏）

栈（自底向上）：`GaussDbThreadMain<1>`（前台会话）→ `PostgresMain` → 匿名帧
（exec_simple_query）→ `PortalStart` → `ExecutorStart` → 钩子 → `standard_ExecutorStart`
→ `InitPlan` → `ExecInitNode` ↘ `ExecInitRecursiveUnion`（WITH RECURSIVE）↘
`ExecInitSubqueryScan` ↘ `ExecInitHashJoin` ↘ `ExecInitSeqScan` → `ExecOpenScanRelation`
→ `heap_open(j,i,s)` → `relation_open` → `errfinish` → `output_backtrace_to_log`

定性：**执行器初始化阶段（未出第一行数据）打开 seq scan 目标表时抛 ERROR**，
回溯由 errfinish 附带。relation_open（heapam.cpp:1451-1506）仅三条 ERROR 路径：

| 源码行 | errmsg | 典型成因 |
|---|---|---|
| :1457 | current transaction is aborted… | 事务前面已失败还继续跑（PBE 复用整批语句） |
| :1473 | could not open relation with OID %u | **计划生成后、执行前表被删/重建（DDL 竞态、plan cache 失效）——最典型** |
| :1479 | current snapshot is invalid for this relation | relcache 记录的 xmin_csn 晚于活快照 |

锁路径排除法：加锁在 relation_open 内是独立函数 LockRelationOid（:1468），死锁/锁超时
会在栈里多出 `LockRelationOid → LockAcquire` 帧——本栈 errfinish 直接挂在 relation_open
上，排除锁问题。定论以相邻 ERROR 行的 errmsg 与 OID 为准。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 把 BACKTRACELOG 当崩溃读 | 它是报错的附带取证，线程还活着 | 先找相邻 ERROR/WARNING 行定级别 |
| 只看栈顶就下根因 | 同一函数多条 ereport 路径 | 报错点函数的所有 ereport 逐条对消息 |
| 拿偏移跨版本比对 | 偏移每次构建不同 | 剥偏移归并；行号用同版本 addr2line |
| 匿名帧跳过不认 | 匿名帧常是 SQL 入口/钩子，丢了链路 | 按上下文夹层推身份 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 档）。`force_backtrace_messages` 在基线中**只置位、
  无任何读取点**（写而不读的死标志，guc.cpp:877 定义；置位点 heapam.cpp:1458/1474、
  catcache.cpp:1263、slru.cpp:881、md.cpp:1341/1398 等）；VastBase 定制分支是否消费
  未证实——判例现场若 GUC 为默认值仍出回溯，即 VB 分支差异的实证
- 判例二进制为 VastBase 定制（heap_open 三参、内建 ExecutorStart 钩子均在栈中可见）

## 来源

- 源码核对（2026-11-02）：elog.cpp:583-598（errfinish 回溯闸门）、guc.cpp:4418-4432
  （backtrace_min_messages，发布版默认 PANIC）、heapam.cpp:1451-1506（relation_open
  三条 ERROR 路径+LockRelationOid 调用位）、heapam.h:285（heap_open 三参）、
  gs_thread.h:56-68（角色枚举）、postgres.cpp:2414（exec_simple_query static）、
  execMain.cpp:221-232（ExecutorStart 钩子分派）、execUtils.cpp:1105
- 判例：2026-10-29 现场生产 BACKTRACELOG（环境与主机名脱敏）

## 知识网

机制层：`../internals/error-fatal-mechanics.md`（BACKTRACELOG 节）；采样栈：
`../performance/wait-events.md`；崩溃真现场：`crash-panic.md`；执行器算子：
`../internals/executor-operators.md`
