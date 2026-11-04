# 缓冲管理器：BufferAlloc 全流程

## 概述

> 服务分类：performance（读路径与淘汰判读、IO 事件定性）

一次"读块请求"在缓冲管理器里的完整旅程，及每一步对应的等待事件、轻锁、AWR 现象。
这是读路径的中枢：BufHashTableSearch / BufStrategyGet / BufMappingLock /
DataFileRead / 前台 DoubleWriteLock 五个现象共同体的机制母体。

精读范围：`storage/buffer/bufmgr.cpp:2976-3260`（BufferAlloc 全函数）+
`storage/buffer/freelist.cpp:104-330`（ClockSweepTick+StrategyGetBuffer 全函数，
2026-10-25 补全后半）。基线 openGauss 6.0.0
（G100 V3 Build 8）。〔排除〕nvm 分支（bufmgr.cpp:2980 直接转出）、segment 路径。

## 完整流程（代码顺序即执行顺序）

```text
INIT_BUFFERTAG → BufTableHashCode
 ── 哈希查找（无锁）────────────── 事件: BufHashTableSearch（计数括号 :3006）
     ├─ 命中 → PinBuffer（钉住防淘汰）
     │    ├─ tag 竞态比对：不等 → goto retry（无锁查找的竞态处理 :3019）
     │    └─ !valid（别人正在读/上次读失败）→ StartBufferIO 排队等在途 IO；
     │         前次失败者获得重读权（*found=FALSE）
     └─ 未命中 → 受害者循环：
          ── 选受害者 ──────────── 事件: BufStrategyGet（计数括号 :3077）
          │    三路：①策略 ring（大批量扫描自循环用，不冲刷全池）
          │        ②候选链（增量检查点+多 pagewriter 时优先，pagewriter 预清）
          │        ③clock sweep（兜底；必要时唤醒 bgwriter :230）
          ├─ 受害者是脏页 → 前台自刷（见下节）→ 刷完换 tag
          ── BufMapping 双分区锁 ─── 事件: BufMappingLock（低号先锁防死锁 :3212）
          ├─ BufTableInsert 装入新 tag；撞车=别人先装入同块 → 弃受害者当作命中(:3230)
          └─ StartBufferIO → mdread ── 事件: DataFileRead（真读盘）
```

## 关键机制逐条

1. **两个计数型括号**：BUF_HASH_SEARCH（:3006，哈希查找）与 BUF_STRATEGY_GET
   （:3077，受害者选择）是姊妹测量括号——前者=访问量镜像，后者=**淘汰策略耗时**
   （候选链扫描/clock 转圈），两者都是"操作自身耗时"不是阻塞，但含义完全不同
2. **前台脏页自刷 + 单页双写（:3103-3176）**：pagewriter 存活且版本支持
   （DW_SUPPORT_NEW_SINGLE_FLUSH）时，backend 淘汰到脏页**自己刷**：
   条件取 content_lock 共享锁（**防死锁设计**：注释实证了两个 backend 同时
   btree 分裂各持对方脏页的死锁场景）→ `first_version_dw_single_flush`（:3169，
   单页双写，持 single_cxt->flush_lock）→ FlushBuffer → 完成。
   **这就是 DoubleWriteLock 出现在前台会话的完整因果链**——不是异常形态，
   是"淘汰到脏页且 pagewriter 跟不上"的正常代价放大
3. **双写空间与淘汰耦合（:3163）**：`free_space_enough` 不满足 → 弃受害者换下一个
   ——双写文件空间紧张时的表现是**淘汰效率下降**（BufStrategyGet 变长）而非直接报错
4. **BufMappingLock 双分区锁序（:3212）**：换 tag 需同时持新旧两个哈希分区锁，
   统一"低号先锁"防死锁；热点块+高淘汰率时该锁排队
5. **候选链优先（freelist.cpp:201）**：增量检查点（ENABLE_INCRE_CKPT）且
   pagewriter 数>1 时优先从候选链拿干净页（最多重扫 5 轮），clock 兜底——
   V3 的淘汰主力是 pagewriter 预清，clock 高转说明 pagewriter 供给不足
6. **bgwriter 唤醒（freelist.cpp:230）**：backend 在分配紧张时按提示唤醒 bgwriter
   （bgprocno 竞态处理：可能唤错进程，注释明示无害）

## clock sweep 深读补全（freelist.cpp:104-330，2026-10-25）

- **ClockSweepTick 无锁推进**：nextVictimBuffer 原子 fetch_add，多进程并发
  推手（注释自认"slightly out of apparent order"，公平但乱序）；绕圈归零者
  持 buffer_strategy_lock CAS 收敛并 completePasses+1（供 StrategySyncStart
  一致读）
- **候选链 usage_count 老化**（get_buf_from_candidate_list :745）：候选页
  usage_count>0 先减一让过、归零且非脏才收——**热页不会被候选链淘汰**；
  主循环受害者条件 = refcount==0 且 !BM_IS_META 且
  （backend_can_flush_dirty_page() || !BM_DIRTY）——**前台能否收下脏受害者
  受刷脏门控**
- **候选链取空重试**：最多 5 轮，退避 1ms 倍增至 BgWriterDelay 封顶
  （ENABLE_INCRE_CKPT+多 pagewriter 时）；**双计数器
  get_buf_num_candidate_list / get_buf_num_clock_sweep = "pagewriter 供给 vs
  前台自扫"占比**〔负向实锤：全树检索无 SQL/视图出口，仅内核内部原子计数——
  AWR 不直出，实机观察需 perf/gdb〕
- **备库限制**：standby 只在 shared_buffers_fraction×NBuffers 范围内转时钟
  （避免扫全池）；扫不到时份额 +0.1 动态上调——防 Startup 进程内 ERROR
  升 FATAL（源码注释明言该意图）
- **全池无可用页的升级路径**（try_counter 耗尽后四选一）：备库调份额重试 /
  双写运行中 LOG+退避重试 / btree 分裂特例重试 / 否则 ERROR
  "no unpinned buffers available"——各自日志文案即定位证据

## AWR 判读映射

| 现象 | 机制解释 | 下一步 |
|---|---|---|
| BufHashTableSearch 高频 avg≈0 | 访问量镜像 | 只作访问量代理（core-events §1） |
| **BufStrategyGet 高且 avg 明显>0** | **淘汰压力大**：clock 转圈/候选链扫不满 | 查 shared_buffers 周转与 pagewriter 供给，区别于访问量信号 |
| DoubleWriteLock 出现在前台 | BufferAlloc 脏受害者自刷（:3169 链路） | 联查 dirty 率、pagewriter behind/候选链——本质是刷脏跟不上淘汰 |
| BufMappingLock 排队 | 高淘汰下的分区锁竞争 | 同上，先解决淘汰压力 |
| DataFileRead 正常而淘汰压力大 | miss 与淘汰是两个环节 | 分别看物理读速率与 BufStrategyGet |
| 淘汰效率差但磁盘不忙 | （推断）候选链耗尽/dw 空间门槛 | 标推断，实机核对 dw 空间与 pagewriter 状态 |

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| BufStrategyGet 当锁瓶颈 | 计数括号（策略耗时） | 与 BufHashSearch 同法处理，但语义=淘汰压力 |
| 前台 DoubleWriteLock 当异常 | 是脏受害者自刷的正常代价放大 | 查刷脏供给，不是调锁 |
| 高物理读直接加 shared_buffers | 淘汰压力与池大小未必相关（候选链/pagewriter 供给） | 先按上表定位环节 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 Build 8）；增量检查点候选链为 V3 形态，
  非增量检查点版本以 clock 为主
- 行号会漂，函数名为锚

## 来源

- 源码精读（2026-10-25）：bufmgr.cpp:2976-3260（BufferAlloc 全函数含注释）、
  freelist.cpp:180-260（StrategyGetBuffer：ring/候选链/clock/bgwriter 唤醒）
- 联动：`core-events-deep-dive.md`（§1/§2/§5）、`operator-memory.md`（淘汰与
  算子内存共用水位）、`kernel-data-path.md`（读路径）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
