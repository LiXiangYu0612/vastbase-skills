# 双写机制（Double Write）

## 概述

> 服务分类：performance（刷脏链路/等待事件判读）+ backup-recovery（崩溃恢复）+
> troubleshooting（页面撕裂取证）。回答：双写为什么存在、文件怎么组织、
> 何时写、检查点时怎么回收、崩溃恢复怎么用它救页、已知缺陷在哪。

**要解决的问题——Partial Write（PW，部分写）**：数据页（默认 8KB）写盘不 fsync，
磁盘 IO 单元与页大小不等 + OS Write Back 策略，掉电时页面可能只写了一半。
WAL 恢复依赖 pd_lsn 判重放（页 LSN ≥ 日志 LSN 则跳过），**页面半写导致 pd_lsn
本身损坏时恢复算法失效**——这是双写存在的唯一理由。

**两阶段写方案（W1 副本 + W2 原位）**：先把页面副本写到别处（W1，fsync 保持久），
再写原位置（W2）。W1 失败→原数据未动；W2 失败→用 W1 副本恢复。
工程取舍：若每页每次刷脏都 fsync 副本则 IO 不可接受——VB 以**检查点为回收边界**
（检查点把脏页 fsync 后，对应 DW 副本即失效），摊薄 fsync 频率。

| 方案 | 副本去处 | 优点 | 缺点 |
|---|---|---|---|
| Full Page Write（PostgreSQL） | WAL 文件（借 WAL fsync 与主备传输） | 无新增设施 | WAL 膨胀 |
| **Double Write（VastBase）** | 固定 DW 文件 | 文件大小固定可控 | 新增设施与回收逻辑 |

## DW 文件组织（$PGDATA/global/）

| 文件 | 用途 | 结构 |
|---|---|---|
| pg_dw_meta | 元数据 | `dw_batch_meta_file` 结构体（dw_file_size/dw_file_num/record_state），3 块各存一份（三副本防破坏） |
| pg_dw_{n}（batch dw） | **PageWriter 批量刷脏**副本 | 0 号块=file header（头/中/尾写 3 次）；每批 = 1 块 batch header（`dw_batch_nohbkt`，含 page_num 与 BufferTag 数组）+ 最多 409 块数据（每 BufferTag 20B，(8192-头-尾)/20 取整 409） |
| pg_dw_single（single dw） | **页面淘汰**（前台/缓冲淘汰路径）副本 | 固定 32768 块（8KB 块即 256MB，不受 GUC 控制）；1 号区起存 heap 页，段页式区不在此讨论；副本页在 pd_lower/pd_upper 之间嵌 `dw_single_flush_item`（物理位置+版本号） |

结构体锚点：`src/include/access/double_write.h:45`（dw_batch_nohbkt）、
`:58`（dw_single_flush_item）、`double_write_basic.h:93`（dw_page_head_t）；
实现在 `src/gausskernel/storage/access/transam/double_write.cpp`。

关键线程状态 `ThrdDwCxt`：dw_buf（(409+409+3)×BLCKSZ 本地缓冲）、write_pos
（本轮计数）、**dw_page_idx**（-1=本轮无 DW 写入，否则=该线程在 DW 文件中的
偏移块号——truncate 计算 min_idx 就取它）、is_new_relfilenode（段页式标记）。
单轮最多刷 818 页（409+409），超出分多轮。

## 写入与回收

**batch 常规写入**（每轮）：脏页拷入 dw_buf → 统计最大 pd_lsn → **先等对应 WAL
落盘**（脏页先于 WAL 落盘是死罪）→ 补 batch header（起始块号/页数/checksum）→
写入 batch dw 文件。

**truncate（推进恢复起点，检查点时）**：检查点把脏页 fsync 后调
`dw_batch_file_recycle`（double_write.cpp:767）：取所有 PageWriter 的
`get_dw_page_min_idx`（:718）最小值 min_idx → 释放 flush lock → PageWriterSync
请求 MainPageWriter fsync → 重取锁 → file header 的 start 改为 min_idx
（:829-834）。**恢复时只从 start 开始扫**——start 推进错则恢复出错。

**reset（整文件回收）**：文件写满时，持 flush lock → start 重置为 1 →
PageWriterSync（双序号轮询：old_fsync_start/new_fsync_start/new_fsync_done）→
更新 0 号块。

**single dw 写入**（页面淘汰路径）：拷页 → 等 WAL fsync → 取 write_pos →
在 pd_lower/pd_upper 间写 dw_single_flush_item → checksum → 写入。
truncate/reset 逻辑同族（逐块状态位找 max_idx）。

**版本号 dwn（区分新旧数据）**：文件头 head.dwn 随文件头变化递增（当前代码
**仅在 reset 场景递增**，:825 注释：truncate 且 flush_page≠0 时不能加）；
批次头记录写入时的文件头版本。恢复时版本不一致=旧轮残留，跳过。

## 崩溃恢复

batch：读 0 号块 file header → 从 start 逐批读 → 批版本 vs 文件头版本
（+checksum）→ 每页按 BufferTag 定位堆文件：**heap 页损坏或
heap.pd_lsn < dw.pd_lsn 则用 DW 页覆盖回写**（dw_recover_all_partial_write_batch
:998 / dw_recover_partial_write_batch :1020 / dw_recover_pages :611）。

single：从 start 逐块 → pd_lower 读 item → 同款覆盖判据。
**注意差异：single 恢复不校验与文件头版本**——可能处理无需恢复的旧页，
磁盘差时拖长启动时间（已知取舍）。

## 已确认缺陷：truncate 竞态（社区 PR 6671）

`dw_batch_file_recycle` truncate 路径：:790 记录 min_idx → :791 **释放
flush lock** → :800 PageWriterSync → 重新取锁。释放窗口内 SubPageWriter 刷脏
更新 flush_page，导致 :833 的 `last_flush_page = min_idx - start` 计算错误 →
start 推进错 → 恢复出错。源码注释自认设计（"flush lock is not held during
smgrsync"）。排查 DW 相关恢复异常时先对版本（3.0.8 是否含 PR 6671 修复）。

## 工具

```bash
cd $PGDATA/global
pagehack -f pg_dw_0 -t double_write        # 批量 DW：版本/恢复起点/待恢复页
pagehack -f pg_dw_single -t dw_single_flush_file   # 单页 DW：版本/起点/页列表
```

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 关双写省 IO | 半写页会让 WAL 恢复算法失效（pd_lsn 损坏无法判重放） | enable_double_write 保持 on（标准基线也是 on） |
| DW 文件大想清掉 | 固定大小循环使用，start 推进后旧块自然失效 | 不需要人工清理；异常膨胀先查检查点频率 |
| 把 DW 当备份 | DW 只在"数据页尚未 fsync"窗口内有意义 | 恢复链以 WAL 为准，DW 只救撕裂页 |

## 版本注记

- 基线 openGauss 6.0.0（G100 V3 档）；dw_file_size（最小 32MB）/dw_file_num
  （运行时不超过 PageWriter 线程数）GUC 可调
- 段页式存储的 DW 数据区不在本文件展开（不稳定特性，按域约定不深入）

## 来源

- 内部文档《Vastbase 的 Double Write机制》（2026-11-04 用户提供，D:\log\1）
- 源码核实（2026-11-04，openGauss 6.0.0 树）：double_write.cpp:767/790-800/818-834
  （recycle 竞态与 start 推进）、:718（get_dw_page_min_idx）、:998/:1020/:611
  （恢复链）、:825（dwn 递增约束注释）；double_write.h:45/58、
  double_write_basic.h:93（结构体）
- 关联：`buffer-manager.md`（淘汰路径双写因果链）、`flush-checkpoint.md`
  （检查点是 DW 回收边界）、`page-lsn-mismatch.md`（pd_lsn 判据的另一面）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
