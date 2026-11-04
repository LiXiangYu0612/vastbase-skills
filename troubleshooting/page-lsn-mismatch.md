# Page LSN 类报错分析（lsn check error / bigger than want set LSN）

## 概述

> 服务分类：troubleshooting（崩溃/回放类报错的定位方法论）。两类报错：
> `lsn check error` 与 `The Page's LSN[x] bigger than want set LSN`。
> 先说结论性前提：**页面/WAL 损坏基本可以排除**（损坏走 checksum error 报错，
> 页面与 WAL 都有 CRC）——这两类报错指向**写逻辑或回放逻辑不正确**。

## 背景机制

正常修改页面的顺序：LockBuffer(content) → 改 shared buffer → XLogInsert
（写 wal buffer）→ PageSetLSN → 解锁；脏页落盘前保证 WAL 先落盘。
两条铁律：
1. **页 LSN 单调前进**：每次修改页面的 XLOG LSN 必大于页当前 pd_lsn。
   写入/回放时发现 page LSN ≥ XLOG LSN → 报 bigger than want set LSN
2. **XLOG 记录页面前后 LSN**：blkref 的 lastlsn=修改前 LSN；回放先按
   （pd_lsn < xlog LSN）判是否需要回放，再核一致性，不一致报 lsn check error

## XLOG 解剖（pg_xlogdump 输出读法）

```
REDO @ 0/2A6C3310; LSN 0/2A6C33D0: prev 0/2A6C32C0; xid 14237; term 1;
  len 5; total 187; crc ...; desc: Heap - XLOG_HEAP_UPDATE ...
  blkref #0: rel 1663/15790/16388/-1/0, forknum:0 ... blk 1538 lastlsn 0/2A6C3118
```
- **REDO**=本条 XLOG 的 LSN；**LSN**=本条写入后的系统 LSN；**prev**=上一条 REDO
- **blkref #n**=XLogRegisterBuffer 注册的第 n 个块；rel=tablespaceOid/databaseOid/
  relfilenode；blk=块号；**lastlsn=该页面修改前的 pd_lsn**
- 物理定位：段文件编号 N = blk/(1G/8K)，偏移 = blk%(1G/8K)，路径
  base/<dboid>/<relfilenode>[.N]；forknum 2=vm 等分支文件

## 双链回溯定位法（核心逻辑）

报错后从两条线索各自重建"该页面的修改历史"，比较两条链：

**链 A（页面侧）**：coredump 拿问题页地址 → pagehack 看页
（`pagehack -f <relfilenode> -t heap -s <blk> -n 1`，hash 索引也能用 heap
打开——页头通用）→ 取 pd_lsn → `pg_xlogdump -s <pd_lsn> --limit 1` 查到
REDO 点后**再看前一条（prev）**才是真正改这页的 XLOG（pd_lsn 对应的正是
那条的 LSN）→ 用 lastlsn 链继续向前回溯。

**链 B（XLOG 侧）**：coredump/日志拿当前回放或写入的 XLOG LSN → 同法回溯。

**分叉判定**：
- **两条链一致** → 回放逻辑问题（例：社区 PR 4527 一族）
- **两条链不一致** → 写入逻辑问题（例：并发修改没按规范顺序/content lock 失效/
  池化集群脑裂；TAPD 1160475194001047669 一族）
- 修改时（非回放）报 bigger：content lock 有问题/未上锁并发改/改页代码不规范/
  脑裂；**回放时**报 bigger（宕机重启或流复制）：写入侧已保证单调，基本是
  回放逻辑错

**实战范式**（hash 索引 PANIC 案例）：coredump 定位 block_id=0 页 → pagehack
取页 → xlog 回溯到 XLOG_HASH_SQUEEZE_PAGE → 两条链对上 → 按 xid 查全操作
（大 insert + hash 索引）→ generate_series 复现闭环 → 对社区 issue（I89N9K）。

## 处置

- 先保全再动手：全页备份 + pg_control + 对应日志窗口归档
- 有备份/PITR：时间点恢复回退优先于原地修页
- 疑内部缺陷：栈指纹对内部缺陷库并上报，保留现场件

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 先怀疑磁盘/页面损坏 | 损坏走 checksum error，不走 LSN 报错 | 直接进写/回放逻辑排查 |
| pd_lsn 直接当"改这页的 XLOG"查 | pd_lsn 是修改**后**的 LSN，要再往前看一条 | prev 链回溯法（本文） |
| 两条链只看一条 | 单链只能证伪自己 | 双链比对才分叉定责 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 档）；段页式页面寻址不同（另见段页式资料），
  方法论同构
- 工具：pg_xlogdump -f <seg> -s <lsn> --limit 1；pagehack -f/-t/-s/-n

## 来源

- 内部文档《如何分析 Page LSN 相关问题》（2026-01，用户提供 D:\log\1，
  2026-11-04 入库；含 hash 索引实战与社区案例链接）
- 关联：`../internals/double-write.md`（半写页的另一半保护）、
  `../internals/wal-replay.md`（回放判据 pd_lsn<xlog LSN）、
  `stack-reading.md`（coredump 堆栈读法）、`crash-panic.md`（PANIC 现场保全）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
