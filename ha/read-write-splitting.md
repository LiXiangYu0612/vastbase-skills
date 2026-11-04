# 读写分离（Hydra-JDBC 驱动层路由）

## 概述

> 服务分类：ha（接入层）+ performance（读扩展与备库利用）。Hydra-JDBC 是
> 驱动层读写分离组件：应用连"集群地址"，驱动解析 SQL 把写发主、读分发只读
> 节点，应用无感知（像用单点库）。回答：什么语句发哪、负载均衡算法、
> 主备切换时驱动怎么感知、备库冲突报错怎么被兜住。

## 转发规则（核心逻辑）

**只发主节点**：全部 DML（含 SELECT FOR UPDATE）、全部 DDL、用户自定义函数、
存储过程、EXECUTE、用到临时表的请求、用户变量查询/更改、SQL 内 KILL。

**可发只读节点**：
- 事务间：单语句事务、多语句纯读事务
- **事务内**：第一条 DML **之前**的读语句可发备，遇到写语句后全部回主机。
  原理边界：三种隔离级别（RR/RC/序列化）严格说都不允许事务内读发备
  （不可重复读/读不到已提交），"写前读发备"是驱动做的工程折衷——强一致性
  事务要评估此语义
- 系统变量更改发所有节点

## 负载均衡

轮询（默认，按连接发起时间依次分发）/ 按 CPU 负载 / 权重（按节点配置比例）
三种算法，可配主节点是否参与读均衡与各节点权重，可叠加生效。

## 切换感知（HAS 联动）

- **switchover**：HAS 发信号给中间件；切换期间**用户连接保持在连接池**（应用
  不报错，配超时=正常切换时长，超时才报错）；完成后写请求转新主、读请求转
  新同步备
- **failover（主宕）**：同步备升主，同款连接缓存+转发切换
- **同步备启停**：读写请求暂时全发主，备起来后读再分流；异步备启停不引起变化
- 角色自动识别：配置全部节点 IP，驱动自动判断谁可写可读（switchover 后
  应用无感知）

## 备库兜底（与回放冲突的联动）

1. **备库查询报错转发主库**：备库读被 `canceling statement due to conflict
   with recovery` 杀掉时（机制见 `../internals/recovery-conflict.md`），驱动
   内部把该 SQL 重新转发主库执行，应用无感知
2. **备库故障**：读请求全部转主库
3. 报错转发是备库冲突的应用层对策之一（另两条：调 delay 预算/开
   hot_standby_feedback，见 recovery-conflict 参数表）

## 推荐参数组合（内部测试口径）

```
wal_level=hot_standby   hot_standby=on
synchronous_commit=on / remote_apply
hot_standby_feedback=on            # 备机最小事务号反馈主机
enable_defer_calculate_snapshot=off # 不推迟获取快照
recovery_max_workers=8  recovery_parallelism=1  -- 内部测试口径；生产基线=串行回放（1），见 admin/g100-standard-params
recovery_parse_workers=1  recovery_redo_workers=1
```

判读注记：该组合把备库回放并行度开到 8 而 parse/redo 保持 1（普通并行回放
形态，未开极致 RTO——极致 RTO 不支持备机读，与读写分离互斥）。

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 读写分离当零成本扩展 | 事务内写后读全部回主机；强一致事务还有语义折衷 | 按转发规则评估业务读占比 |
| 备库被冲突杀当备库故障 | 驱动会转发主库重试，表象是备库流量下降 | 查 recovery conflict 日志与参数 |
| 读写分离 + 极致 RTO 一起上 | 极致 RTO 不支持备机读 | 参数组合互斥，二选一 |

## 版本注记

- 内容源：内部课件《XS-DB-102 读写分离》（用户提供 D:\二线团队 初级课程，
  2026-11-04 入库）；标注"内部资料严禁外传"——公仓发布时本文件须脱敏重写或排除
- ShardingSphere 原生事务/负载均衡/读写分离为上层集成项，不在本域展开

## 来源

- 内部课件《XS-DB-102 读写分离》
- 联动：`../internals/recovery-conflict.md`（备库冲突机制——报错转发的对象）、
  `has-dcs-architecture.md`（切换信号来源）、`../internals/wal-replay.md`
  （回放并行参数语义）

## 知识网

本节点的关系与导航路径见 `../KNOWLEDGE-GRAPH.md`（节点 · 边 · 从问题进网）。
