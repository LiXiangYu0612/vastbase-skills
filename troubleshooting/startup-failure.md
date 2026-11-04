# 实例启动失败排查（startup-failure）

## 概述

> 服务分类：troubleshooting。实例起不来时的**按序排查清单**——顺序本身
> 就是经验：先便宜后昂贵、先软后硬。数据库启动 = 写锁文件 → 绑端口 →
> 挂共享内存 → 恢复 WAL → 上线，每一步都可能死，**死在哪一步日志说了算**。

## 第 0 步：先读日志（别猜）

`pg_log/`（数据目录下）最新日志**尾部**——失败原因 90% 在最后 20 行。
没有日志文件生成 = 死在更早的路径（权限/目录/参数文件解析），看启动
命令的标准错误输出。

## 排查清单（按序）

### 1. 锁文件残留（最常见）

- 判定：日志报 "Is another postmaster ... running?"（锁文件判定在
  miscinit.cpp；pid 文件由 postmaster.cpp:1105 起写入数据目录）
- 确认无活实例：`kill -0 <pid文件里的pid>` 返回非 0 = 残留尸体 →
  删 `postmaster.pid` 再启动；**pid 还活着千万别删**（真有实例在跑，
  先搞清是谁的）
- 上次异常退出（crash/kill -9）没清锁文件是残留主因

### 2. 端口占用

- 日志 "address already in use"（pqcomm.cpp:729 注释明言 stop/crash 后
  TIME_WAIT 窗口内重启会撞）；`ss -lnp | grep <端口>` 找占用者
- TIME_WAIT 撞车等 60s 再启即解（`../linux/network.md`）

### 3. 共享内存 / 信号量残留

- `ipcs -m` / `ipcs -s`：上次异常退出留下的段/数组，属主为库用户且
  确认无活实例后清（判据见 `resource-exhaustion.md` 第 3 节）

### 4. 磁盘满（ENOSPC）

- 日志/数据/xlog 任一分区满都会启动失败或起半截；实测环境出现过根分区
  ENOSPC 把活实例压垮（先清空间再谈恢复）
- `df -h` 数据目录、xlog 目录、pg_log 所在分区全查

### 5. 权限与属主

- 数据目录属主必须是库系统用户（root 拥有的目录/文件残留=启动直接拒）
- 命令铁律：**切对系统用户再启动**（域规矩：直连/启停都走库用户）

### 6. 控制文件 / WAL 损坏（贵的那头）

- `pg_controldata <数据目录>` 存档一份输出（后面每步对照基准）
- CHECKPOINT 状态异常/上次异常停机标志 → 启动走崩溃恢复；WAL 损坏在
  恢复中报 FATAL/PANIC → **先全量拷贝数据目录再动**；恢复手段按序：
  备份/PITR 恢复（首选）→ probackup 恢复；`pg_resetxlog` 是**最后手段**
  （跳过 WAL=数据可能不一致，必须先备份、留档、走变更流程）

### 7. License（VastBase 特有）

- 交付包带 `license_checker`（基线源码 bin/ 实锤）：授权到期/BETA 版
  自过期会拦启动——日志会有对应文案；处理走授权续期，不是技术排障
  〔license 机制细节以交付包为准⚓〕

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 上来就删 pid 文件 | pid 活着=真有实例 | kill -0 确认后再删 |
| 崩溃恢复失败直接 resetxlog | 跳 WAL=放弃一致性 | 先拷目录+走备份恢复 |
| 只看启动命令回显不看 pg_log | 真因在日志里 | 第 0 步先行 |
| root 直启"顺手" | 目录属主被改=二次故障 | 永远库用户启停 |

## 版本注记

- 锁文件/端口/控制文件行为以基线源码为准（postmaster.cpp/pqcomm.cpp/
  miscinit.cpp）；pg_ctl 为基线 bin 工具（交付包实际以环境为准）
- TIME_WAIT 撞车窗口 60s 为内核常量（`../linux/network.md`）

## 来源

- 基线 openGauss 6.0.0 源码：gausskernel/process/postmaster/postmaster.cpp:1105+（pid 文件写入）、
  common/backend/libpq/pqcomm.cpp:729（stop 后 TIME_WAIT 撞车注释）、
  common/backend/utils/init/miscinit.cpp（锁文件判定）、bin/pg_controldata、
  bin/license_checker
- 实测案例：ENOSPC 压垮实例（2026-10，内部环境）

## 知识网

本文件为操作层：crash-panic（异常停机的另一半）、resource-exhaustion
（IPC 残留清理判据）、wal-replay（崩溃恢复=回放，机制同源）、network（60s 撞车）。
