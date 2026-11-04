# autovacuum 机制（触发判定 / 让路 / 限流）

## 概述

> 服务分类：performance（膨胀形态判读的"另一半"——死元组侧见 mvcc-visibility.md）

## 触发判定（autovacuum.cpp:3171 relation_needs_vacanalyze，深读）

```
vacthresh = autovacuum_vacuum_threshold + autovacuum_vacuum_scale_factor × reltuples
anlthresh = autovacuum_analyze_threshold + autovacuum_analyze_scale_factor × reltuples
dovacuum  = n_dead_tuples       > vacthresh
doanalyze = changes_since_analyze > anlthresh
```

- 死元组/变更计数来自 pgstat（tabentry.n_dead_tuples / changes_since_analyze）
- **need_freeze 强制真空**：表年龄逼近 freeze_max_age 时 force_vacuum，无视阈值
  （防 xid 回卷；回收边界由 OldestXmin 决定——被长事务/复制槽压住时 vacuum
  跑了也清不掉，见 `mvcc-visibility.md`、`replication-slots.md`）
- **autovacuum_mode 门控**（:1652-1758）：mix（vacuum+analyze 都允许，默认语义）/
  vacuum / analyze / none——标准参数表的 `autovacuum_mode=mix`
- **★analyze 资格三条件与门**（DO_ANALYZE 宏 :1650-1653，2026-11-03 补）：
  mode 允许 analyze **且** enable_global_stats=on **且** `default_statistics_target > 0`。
  **负值/0 = autoanalyze 全库关停**（enable_analyze 掰 false，:1699；官方文档同义
  "负数则 autoanalyze 不生效"）——负值本是百分比采样模式，后台嫌太低效不做；
  auto**vacuum** 不受影响，手工 ANALYZE 照常。门读 worker 会话的值（conf/库级；
  ALTER SYSTEM 后须 reload）。target 双模式算法详见 `stats-to-cost.md`
- 表级覆盖：determine_vacuum_params 先取 storage 参数里的表级 reloptions 覆盖
  全局 GUC（大表单独调阈值的机制入口）
- delta 表门槛（delta_rows_threshold，ustore 配套）〔排除路径相关，仅记录〕

## 让路机制（proc.cpp:1652-1686，深读）

后台会话被 autovacuum 阻塞（死锁检查发现 blocking_autovacuum_proc）时，
**主动向阻塞的 vacuum worker 发 cancel**（"sending cancel to blocking autovacuum
PID"，allow_autovacuum_cancel 控制）——vacuum 让路重跑，业务不等。
**判读**：logrpt 里 "canceling autovacuum task" = 业务与 vacuum 撞锁被让路，
属设计行为；频繁出现说明 vacuum 窗口与业务冲突（考虑 naptime/限流调优）。

## 限流与调度（vacuum.cpp:2945 vacuum_delay_point 深读，2026-10-25 补）

- **激活条件**：VacuumCostActive = vacuum_cost_delay > 0（vacuum.cpp:322）
- **睡眠公式**：VacuumCostBalance ≥ VacuumCostLimit 时睡
  **msec = vacuum_cost_delay × Balance / Limit**（比例睡眠：超多少睡多久），
  上限 **4×vacuum_cost_delay**；**睡完 Balance 清零重计**（:2952-2962）；
  AutoVacuumUpdateDelay 同步 worker；睡眠中仍 CHECK_FOR_INTERRUPTS（可取消）
- **判读**：vacuum/analyze 慢先算"睡了多久"——Balance 由页成本记账累计
  （命中/未命中/弄脏三档代价），IO 有余量时放松 vacuum_cost_limit 即缩小
  睡眠比例；**analyze 每采样行也打 vacuum_delay_point**（analyze.cpp 采样
  循环内），所以 analyze 慢同样可能是限流而非表大
- naptime 轮询周期内每库派 worker（autovacuum_max_workers 并发上限）
- 表级跳过日志实锤=**权限族文案** `skipping "<表>" --- only … can vacuum it`
  （vacuum.cpp:2055-2093 五种变体）+ 分区跳过（autovacuum.cpp:2143）；锁不可
  得类跳过文案未抓到〔⚓〕

## AWR 判读映射

| 现象 | 机制解释 |
|---|---|
| dead tuples 常年停在 ~5% 附近 | 触发公式就是 base+0.05×N（默认 scale_factor）——清理后在阈值附近震荡是**公式行为不是故障**；大表想要更积极需表级调低 scale_factor |
| dead 高但 worker_cnt=0 | 阈值未到（公式算一遍）或 mode 门控/naptime 未到；真没跑再看 autovacuum=on |
| vacuum 在跑但 dead 不降 | OldestXmin 被长事务/复制槽压住，回收边界没到（`mvcc-visibility.md`、`replication-slots.md`） |
| changes_since_analyze 高而 dead 低 | analyze 该跑没跑：anlthresh 0.02×N 大表偏松，或 autoanalyze_timeout 被掐（标准 900s，现场样本 300s 偏紧） |
| "canceling autovacuum task" 频发 | 让路机制在干活：vacuum 与业务撞锁，调窗口/限流 |
| need_freeze 真空出现 | 表年龄逼近 freeze_max_age 的强制行为，优先级高于一切 |

## 常见错误

| 错误 | 纠正 |
|---|---|
| dead 5% 当膨胀故障 | 默认公式行为；膨胀判据是 dead/active 比与持续增长形态 |
| vacuum 慢怪线程少 | 先看 cost 限流预算与 IO 余量 |

## 版本注记 / 来源

- 基线 openGauss 6.0.0（G100 V3 Build 8）
- 源码精读（2026-10-25）：autovacuum.cpp:3171-3312（relation_needs_vacanalyze
  全函数：阈值公式/mode 门控/freeze 强制）、:1652-1758（mode 宏）、
  proc.cpp:1652-1686（让路 cancel）
- 二次补读（2026-10-25）：vacuum.cpp:2945-2967（vacuum_delay_point 全函数：
  比例睡眠/4 倍封顶/清零重计）、:322-323（激活与初始化）；三次补读：
  vacuum.cpp:2055-2093（权限族跳过文案）、autovacuum.cpp:2143（分区跳过）
- 2026-11-03 补：DO_ANALYZE 宏（:1650-1653）三条件与门精读 +
  default_statistics_target 负值关停 autoanalyze（与官方文档一致；
  阈值公式与 target 无关已核）。env-T 负值红绿实测未完成（ALTER SYSTEM 后
  漏 pg_reload_conf，worker 未看到 -10，结果作废；用户令停测，以源码+文档双源定论）
- ⚓：锁不可得类跳过文案

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
