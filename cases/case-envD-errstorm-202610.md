# 案例：库健康但日志炸锅——三场错误风暴（env-D，2026-10-20/21）

## 环境

- env-D 生产主库 dn_6001_6002（双实例聚合，192 逻辑核，primary）
- 库：dsdb（ds_* 8 个业务 schema 族）+ postgres（monitor_vem 监控账号）
- 样本：五窗（10-20 09:00~12:00、10-21 12:00~14:00），成对 awrrpt+logrpt
- 采集：**老版 vb_tool**（版本号未印，无 Pagewriter Status/Softnet 章节——
  数据局限）

## 症状

业务侧感觉"库有点慢"？而 AWR 全绿：每核并发 0.038、busy 4.5%、iowait=0、
五类等待计数几乎全零——**库本身无瓶颈**。真问题在 logrpt：每窗 ERROR 3500+ /
FATAL 840+。

## 排查链（六步流程）

1. 预检：双实例聚合口径声明；老版采集=刷脏章节缺（behind 判读不可用）
2. 画像：极闲（AAS 7.4→5.8 / 192 核）——**画像健康时重心立即转向日志轴**，
   这一步就决定了本案例的结论方向
3. 时间轴：异常时段主导轴=net（bond0 丢包尖刺）；DB 侧仅 09:50 一次等待尖峰
4. 事件：Top 事件只有 BufHashTableSearch 71 亿次/窗 avg=0——**计数型括号**
   （访问量镜像，≈200 万逻辑读/秒），排除锁/IO
5. 形态：排除法全过（CPU/IO/内存/锁/主备延迟均带数字排除）；判入
   **错误风暴形态**（logrpt 主导）
6. 定性：三场风暴+两个观察项（见根因）

## 根因（三场风暴，全是应用/监控侧）

1. **● dsicp 认证失败 600/h 整点连续**——凭据错误的重试循环（库侧无痕：
   认证在 ClientAuthentication 即拒不占会话资源）
2. **▲ dsecs 撞唯一键 2770/h**（插了再错错了再插）+ value too long×30——
   应用写入路径缺陷
3. **▲ monitor_vem 监控账号权限配错 ≈1100/h**（4 类 permission denied+GUC
   off+superuser，每 15 秒一轮全失败）——**监控满负荷报错=监控盲区**，
   最优先修
4. ◆ bond0 网卡层丢包：中位 12/min+09:31 尖刺 936（成员口 f0）；
   tcp.ovf=0+retrans 0.02% 排除队列/质量层——ethtool -S 方向
5. ◆ session_timeout 成批踢 8 应用空闲连接（121 对/h）——正常机制，
   连接风暴潜在种子
6. ◆ **DB 侧 5 个等待时段（含两个 15 分钟持续带）**：等待指定事务完成带
   （09:50、12:20~12:35，计数 8~10 伴轻锁行锁）——按 commit-sync 规则属
   罕见形态，首选核查 gs_prepared_xacts 两阶段残留；行锁带×2+IO 带各在
   阈值边缘且与整点批量吻合；**每小时整点活跃会话规律冲高**（37~52，
   平时 12）。〔判读修正：初版误判"五窗唯一一次尖峰"——只扫了首窗的
   ASP 分段；全量重扫后纠正。教训已入 awr-report-analysis 铁律〕

## 处置与结果

应用/监控侧三修（凭据/插入语句/授权）+ ethtool 核网卡〔结果未回访⚓〕

## 预防

- 监控账号接入验收清单加一条"接入后 1 小时 logrpt 零 permission denied"
- 认证失败持续告警阈值（连续 10min 同用户失败）
- 用**当前版 vb_tool** 重采基线（补 Pagewriter Status/Softnet 章节）

## 关联知识文件

`../performance/log-analysis.md`（×AWR 联动）、`../internals/error-fatal-mechanics.md`
（FATAL 族/认证）、`../internals/connection-lifecycle.md`（认证段不占会话）、
`../linux/network.md`（丢包三道关）、`../performance/wait-events.md`（计数型
识别）、`../performance/awr-report-analysis.md`（画像健康→重心转日志轴）

## 遗留

- 处置后复测样本缺失；09:50 等待尖峰窗（观察项）未单独深挖；
  dn_6005 Potential 备配置核对未回〔⚓〕
