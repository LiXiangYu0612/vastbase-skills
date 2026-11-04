# 连接类故障（connection-refused）

## 概述

> 服务分类：troubleshooting。症状谱：连接拒绝/连接超时/连上就断/
> "sorry, too many clients already"。定位口诀：**从外向里四道闸**——
> 网络可达 → 监听 → 内核队列 → 库的连接上限。

## 四道闸定位法

### 闸 1：网络可达（OS 层）

```bash
ping / tcpping <主机>          # 先分 ICMP 通不通与端口通不通（ICMP 可被禁）
telnet <主机> <端口> 或 nc -zv  # 端口层探活
```

不通 → 防火墙/路由/VLAN，与库无关（`../linux/network.md` 丢包三道关）。

### 闸 2：监听（库层）

```bash
ss -lnt | grep <端口>          # 进程在不在听
```

- 没在听：实例没起来（→ `startup-failure.md`）或 `listen_addresses`/
  `port` 配置不符（postgresql.conf；直连铁律：切对系统用户+默认端口，见域规矩）
- 报 "could not bind"（日志原文，pqcomm.cpp:774）：端口被占——`ss -lnp`
  找占用者；同一机双实例（常见 5432/9432）别把端口配串

### 闸 3：内核两级连接队列（OS 层，最隐蔽）

`ss -lnt` 监听行 **Recv-Q 贴着 Send-Q** = accept 队列满，或
`nstat -az | grep -E 'ListenOverflows|ListenDrops'` 在涨：

- 全连接队列满 → 默认**静默丢 ACK**（tcp_abort_on_overflow=0）——客户端
  表现"连上但无响应/超时"，服务端毫无报错（机制：`../linux/network.md`
  两级队列节）
- 处置顺序：先看应用/库为什么 accept 慢（会话堆积→闸 4），再谈加大
  `net.core.somaxconn` 与监听 backlog（`../performance/parameters.md` 组 7）；
  库内核侧机制见 `../internals/connection-lifecycle.md`（postmaster 单线程
  accept+每连接一线程孵化），堆积时 SYN-ACK 重传空转推高 sys 的内核连锁见
  `../linux/network.md` 全队列堆积节

### 闸 4：库的连接上限（DB 层）

- 报 **"sorry, too many clients already"**（原文）：活动会话 ≥
  max_connections——先查是谁吃光：

```sql
SELECT usename, client_addr, count(*) FROM pg_stat_activity
WHERE backend_type = 'client backend' GROUP BY 1,2 ORDER BY 3 DESC;
-- 基线以 state 分布看：idle 占大头 = 应用不还连接（连接池缺失/泄漏）
```

- idle 海量 → 根因在应用侧连接管理（池化/超时归还），**砍 max_connections
  或加内存不是根治**；突发风暴配 `timeout`/连接池上限
- 认证失败（密码/hba）与授权失败报错是明文的，按报错对 pg_hba.conf 与
  角色权限处理，不在这四道闸里

## 常见错误

| 错误 | 为什么错 | 纠正 |
|---|---|---|
| 连接超时全怪数据库慢 | 闸 1~3 任一都会超时，多数与库无关 | 四道闸从外向里过一遍 |
| 静默丢 ACK 当网络抖动 | 全连接队列满的默认行为（network.md） | ss -lnt Recv-Q + nstat 对账 |
| too many clients 就调大上限 | idle 泄漏时调大=延迟爆炸+内存翻倍 | 先按 client_addr 归因，池化根治 |
| 双实例端口配串 | 同机多库常见 | ss -lnp + postgresql.conf 对账 |

## 版本注记

- 报错文案与监听行为以基线源码为准（pqcomm.cpp 绑定/监听路径）；
  tcp_abort_on_overflow、somaxconn 语义跨内核版本一致（3.10/7.1 同判）

## 来源

- 基线 openGauss 6.0.0 源码：common/backend/libpq/pqcomm.cpp:662,729,774
  （绑定/监听报错路径）、"sorry, too many clients already"（报错原文）
- 连接队列机制：内核 7.1-rc4 精读（`../linux/network.md` 来源节）
- 实战配方：env-A 双实例环境（2026-10 系列）

## 知识网

本文件为操作层：network（两级队列/丢包）、parameters（组 7 sysctl）、
startup-failure（实例不在）、metrics（Threads/会话口径）。
