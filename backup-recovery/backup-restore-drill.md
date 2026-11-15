# 备份恢复演练与翻车形态（backup-restore-drill.md）

## 概述

> 服务分类：backup-recovery（实操层）+ troubleshooting（恢复失败救火）。
> 2026-11-15 生产测试机全流程演练：vb_probackup 2.4.2（PG9.2.4 系内核），
> 实例 7.4G/4626MB PGDATA、archive_mode=off。一条链走完
> **init→全量→增量→链恢复→起库→对账→时点恢复边界**，沿途 13 个翻车形态
> 全部实测收录；PITR 边界与"可恢复上限"判读口径在此定论。
> 演练对象是测试机；生产演练照抄配方前先核磁盘与窗口。

## 演练配方（可复制）

```bash
# ① 目录与登记（以 vastbase 用户跑；B=/备份 catalog，D=/数据目录）
vb_probackup init -B /tmp/vbdrill
vb_probackup add-instance -B /tmp/vbdrill -D $PGDATA --instance <inst>
# ② 标记表（对账基准）：插入 M1 → 全量
psql -d postgres -c "create table vbdrill_marker(id int primary key, note text, ts timestamp default now()); insert into vbdrill_marker values(1,'M1');"
vb_probackup backup -B /tmp/vbdrill --instance <inst> -b FULL -d postgres -p <port>
# ③ 插入 M2 → 增量（-b PTRACK，见形态③）
vb_probackup backup -B /tmp/vbdrill --instance <inst> -b PTRACK -d postgres -p <port>
# ④ 链恢复（-i 取增量，自动带全量）→ 本地化三改（形态⑦⑧）→ 起库 → 对账
vb_probackup restore -B /tmp/vbdrill --instance <inst> -D /tmp/vbrestore -i <增量ID> -j 4
# ⑤ 时点恢复边界实验：--time 见形态⑪⑫
```

## 十三翻车形态（按阶段全录，2026-11-15 实测）

### 备份阶段

| # | 形态 | 实锤与处置 |
|---|---|---|
| ① | **数据目录里非 vastbase 属主残留文件**（本次=root 的 postgresql.conf.ro_bak_*，历次实验遗留） | 报错真身=第一行 `Cannot open file "...": Permission denied`；**随后三连 `Interrupted during WAL streaming` 是次生噪音，极易误诊成 WAL/复制问题——判读规则：先看第一条 ERROR 再定性**。处置=chown 回实例用户 |
| ② | **vb_probackup 有内置限速**：`-r max-rate`，默认 90MB/s 已启用 | 运行 INFO：`Data transmission rate limiting has been enabled, current maxrate: 90.00 MB/s`。⚠ 推翻此前"基线无任何限速参数"推断（开源源码选项表≠交付二进制），backup-window-performance 已同步翻案。备份窗口预算按 90MB/s 默认值核，不匹配就 `-r` 显式定 |
| ③ | **增量模式只剩 FULL/PTRACK** | `-b PAGE`/`-b DELTA`/`-b INCREMENT` 全部 `invalid backup-mode`；二进制字符串表与实测一致=交付版只留两种。从开源 gs_probackup 文档平移增量模式=直接翻车 |
| ④ | 常开监控/采样器=增量体积下限 | 4.6G 实例仅插 2 行，一次 PTRACK 仍传 285MB/18s——采样器持续写脏系统表；增量体积预期别按业务写入量算 |

### 恢复阶段

| # | 形态 | 实锤与处置 |
|---|---|---|
| ⑤ | restore 自动执行 gs_guc | 输出 `Success to perform gs_guc!`——恢复产物含 GUC 改写副作用，审计时别当异常 |
| ⑥ | **log_directory 是绝对路径** | 恢复实例日志直接写进**原实例的日志目录**（/data/vblogs/.../dn_6001）——混日志+排障错位；起库前必改本地路径 |
| ⑦ | 恢复目录三处本地化必改 | `port`/`listen_addresses`/`unix_socket_directory`——**与⑨联动：每次恢复都要重打一遍**（写成脚本固化，别手敲） |
| ⑧ | **VastBase 套接字 GUC 是 `unix_socket_directory`（单数）** | PG 式 `unix_socket_directories`（复数）= `unrecognized configuration parameter` FATAL 起不来；写恢复脚本按单数 |
| ⑨ | restore 拒绝非空目标目录 | `Restore destination is not empty`——连预建 pg_log 都算非空；流程=rm -rf 全清 → restore → 再建 pg_log/改配置 |

### 起库与时点恢复

| # | 形态 | 实锤与处置 |
|---|---|---|
| ⑩ | gs_ctl 单机模式不认 `-Z datanode` | 报 `--coordinator and --datanode option are not supported on single node mode`；单机直接 `gs_ctl start -D <dir>` |
| ⑪ | **`--time` 时点恢复只从归档区 `wal/<inst>/` 连续取段** | STREAM 备份的 WAL 躺在各备份目录 database/pg_xlog/ 内；archive_mode=off 时归档区空 → `WAL segment ... is absent` 断粮。**救急路径：把备份内 pg_xlog 段手工 cp 进 wal/<inst>/ 可推进**（本次实测 3A/3C 补入后继续走，直至再缺备份后的段） |
| ⑫ | **可恢复上限权威口径** | 报错尾行：`WARNING: Recovery can be done up to time <t>, xid <x> and LSN <lsn>`——时间/xid/LSN 三元组，上限=最后一个备份的 stop 位点。**现场"能恢复到几点"先看这句，不用猜** |
| ⑬ | 对账配方 | 标记表双时点+链恢复行数核对：`-i` 恢复到备份时点=备份内全部标记在（本次 M1~M3 全在，正确）；目标时刻落在备份链间隙或之后=⑫ 句式判不可达 |

## PITR 边界定论（本次实测）

- **archive_mode=off 的实例：只有"恢复到备份时点"（`-i`）可用；任意时点恢复（`--time`）结构性不可用**——WAL 回放需要归档区连续段，而备份只自带自身窗口。
- 要任意时点恢复：开归档 + `archive_command='vb_probackup archive-push ...'` 进归档区（重启级变更，需窗口）；
  事后救急=⑪ 手工补段（能补到备份 stop 为止）。
- 恢复上限=最后备份 stop 位点（⑫ 三元组句式）。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 见"WAL streaming 中断"就查复制/WAL | ①号形态里它是次生噪音 | 第一条 ERROR 定性 |
| 照开源 gs_probackup 文档用 PAGE/DELTA | 交付版只有 FULL/PTRACK | ③ |
| 恢复起不来逐个猜配置 | ⑧单复数/⑥日志路径/⑦三改是一组固定动作 | 固化脚本一次打全 |
| archive off 还想要时点恢复 | 结构性不可用 | ⑪⑫ + PITR 边界定论 |
| 备份窗口按存储带宽预算 | 默认限速 90MB/s 在先 | ② `-r` 定值 |

## 版本注记

- 实测环境：生产测试机（2026-11-15），vb_probackup 2.4.2、内核 PG9.2.4 系、
  单机模式、archive_mode=off、实例 7.4G。其他版本/形态（集中式带 replconninfo、
  开归档实例）行为待遇到时回灌⚓。
- 与 `pitr-mechanism.md`（机制层：RestoreArchivedFile/时间线/回放同源）互补：
  本文件是工具层实操与翻车形态。

## 来源

- 生产测试机全流程演练（2026-11-15）：两次失败全量（形态①）+ 成功全量
  TOJR1N + PTRACK TOJR7F + 链恢复 + 15432 起库对账 + 三轮 --time 实验
  （间隙时刻/备份后时刻/补段救急）+ `Recovery can be done up to` 上限句式。
- 联动：`backup-window-performance.md`（限速结论已按②翻案同步）、
  `pitr-mechanism.md`（机制层）、`../troubleshooting/startup-failure.md`
  （配置 FATAL 族）。

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
