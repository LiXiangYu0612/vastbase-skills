# OOM 分析方法论（先分型，再走链）

## 概述

> 服务分类：troubleshooting。"OOM"在 VastBase 环境是**四种不同的病**——分错型
> =全套取证白做（C 型调机器内存无效、B 型查全局内存查不出）。机制（oom_kill.c
> 全链/水位/回收）见 `../linux/memory.md`，本文件只给分型判据与方法链；
> 应急止损见 `../troubleshooting/resource-exhaustion.md`。

## 第 0 步：分型（一票证据定位）

| 型 | 一票判据 | 常见误认 |
|---|---|---|
| A 内核 OOM killer | dmesg `Out of memory: Killed process`（带 oom_score 行） | 应用自己退出的锅 |
| B cgroup OOM | memory.events 的 oom_kill >0（AWR 表 D 项）；dmesg 可无全局记录 | 当成 A 查全局内存 |
| C 库侧 53200 | pg_log ERROR 53200（VB 内存管理拒绝分配），dmesg 干净 | 当成 A/B 调机器内存 |
| D 换页拖死（前驱态） | pswpout 持续+sys 高+卡顿，无 kill 记录 | 不处置会走到 A/C |

## A 型：内核 OOM killer

- **触发条件**：分配路径水位破 min → 直接回收仍不足 → oom_kill（选 score
  最高者；机制/选主算法见 `../linux/memory.md` OOM 节，不复制）
- **root cause 定位链**：
  1. dmesg oom dump 三读：**触发分配的进程 ≠ 责任者**（谁申请谁触发，谁的
     不可回收内存大谁负责）；被杀进程的 anon-rss；dump 时各进程 rss 榜
  2. **谁占着不可回收**：按 `../linux/memory.md` 全景表联动主链第 3 问——
     回收不动的类=结构性超额（无 swap 的 anon / SUnreclaim 泄漏 /
     Unevictable / PageTables 类结构开销）
  3. overcommit 核对：Committed_AS vs CommitLimit（overcommit_memory=2 时
     逼近即拒）——"内存看着没满却 OOM"多在此
  4. 进程侧对账：db_proc VmRSS 对 DB 上下文之和做差，差值大=非上下文消费
- **处置**：按 root cause 对号——加 swap / 治泄漏 / 调 overcommit / 缩上下文；
  **被杀的是 DB 主进程=事故级**，复盘 max_process_memory 防线为何没拦住（接 C 型链）
- **因果闭环**：dmesg kill 时刻 ↔ AWR per-min 水位/换页窗 ↔ LOG 侧报错，
  三源对齐才收案

## B 型：cgroup OOM

- **触发条件**：cgroup memory.max 内回收耗尽（容器/cpuset 部署典型）
- **root cause**：cgroup 限额 vs 实际需求错配——查 memory.max、memory.current、
  memory.events 的 oom（尝试）与 oom_kill（真杀）差分（口径已核：vb_tool 实采 cgroup v2 形态 memory.max/memory.events；v1 oom_control 不采）；
  **全局 MemAvailable 高≠安全**（限额在组内）
- **处置**：调 memory.max 或缩库内存（max_process_memory 配比），先定责再动

## C 型：库侧 53200

- **触发条件**：VB 内核内存管理拒绝分配（共享上下文/会话内存层限额），非内核
  内存不足——dmesg 干净是分型证据
- **root cause 链**：53200 文案分层（分层判读见 `../performance/log-analysis.md`
  与 `../internals/operator-memory.md`）→ 超的是哪层限额（max_process_memory
  池划分 / 共享上下文配额 / 会话级）→ 谁超（Database Memory 构成表 +
  Max Session Memory 榜定位大户）
- **处置**：调池划分或治大户；**别动机器内存**
- **并存规则**：53200 与 A/B 同窗出现时，先查 A/B（内核不足会伪装成一切）

## D 型：换页拖死（前驱态）

- **触发条件**：水位反复破 low、anon 开始换出（有 swap 才成立）
- **判读**：pswpout/pswpin、pgmajfault、sys 高（回收路径吃 CPU，联动
  `../troubleshooting/cpu-sys-high.md`）
- **处置**：降压或加内存，别等 A/C 兑现

## 常见误判表

| 误判 | 纠正 |
|---|---|
| "OOM 了"不分型直接加内存 | C 型无效、B 型要动限额 |
| 被杀进程=责任进程 | kill 选 score 最高≠肇事者；责任=占不可回收内存者 |
| dmesg 没记录=没 OOM | B 型在 events 计数、C 型在 pg_log |
| swap 还有余量却 OOM | 核对 overcommit 口径与 oom dump 的实际统计口径 |

## 判例与关联

- `../linux/memory.md`（OOM 全链机制、全景表、构成×水位联动）
- `../troubleshooting/resource-exhaustion.md`（应急止损）、
  `../performance/check-inventory.md` 表 E P7 行、
  `../internals/operator-memory.md`（53200 分层）
