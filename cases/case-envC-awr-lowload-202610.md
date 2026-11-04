# 案例：库空载但 OS 忙 / 残留长事务 / HDD 双写慢（envC，2026-10-21 窗）

## 环境

- env-C 主库（primary，Oracle 兼容 A；实例名已代号化）；G100 V3 Build 8。
  〔环境注记：该样本实为**测试机自采**（与 env-A 同机：2 核/7.8G/CentOS 7），
  非现场生产——2026-10-26 复核核实并如实标注；机制结论不受影响〕
- 2 核 CentOS 7（3.10.0-1160），7.8G 内存，**vda=HDD**（mq-deadline）
- 采样窗 456s（2026-10-21 16:52:40–17:00:16，两快照窗）；
  同批另有 4 个连窗样本未展开〔⚓〕
- 参数面（与本域知识对得上的）：somaxconn=65535、**tcp_keepalive_time=30**
  （与 env-A 同款——交付标准化 sysctl）、vm.dirty_background_ratio=5/
  dirty_ratio=15（**OS 回写已收敛**）、swappiness=10、autovacuum 系默认

## 症状

- **AAS=0.00**（DB Time 2.11s / 456s，DB 资源占用 0.23%）——库近乎空载
- **但 OS busy=57%（user=51.2）**——CPU 消耗不在数据库前台执行
- 两个尖刺分钟：16:55 **mem.avail=8.1%**（<10% 硬阈值）；17:00
  **cpu.r=9（2 核超核排队）+ vda w_await=462ms**（超 HDD 均值线 40ms 十一倍、尖峰线 20ms 逾二十倍——双线口径见表 D，
  util 仅 17.4%）

## 排查链（六步流程实战版）

1. **预检口径**：digest 头部五条 VastBase 口径警告先行——CPU_TIME 含后台线程、
   %DBtime 并行下 >100% 相加无意义（本窗 %DBtime 就出现了 480%/532% 就是
   这个口径的实证）——**先校准再读数**
2. **画像**：每核并发 0.002（有余量）；DB CPU/DBTime 94%（计算型方向参考）
3. **时间轴**：仅 2 个异常分钟，主导轴 mem / cpu+disk
4. **溯源（事件谱）**：
   - **DoubleWriteFileWrite avg=307.9ms×33**（480%DBtime）——HDD 上的
     双写批次慢；走 `buffer-manager.md` 前台脏受害者自刷链 +
     `io-storage.md`（HDD 阈值档、OS dirty 已收敛 5/15 说明不是 OS 互踩）
   - WALFlushWait avg=798ms×3——同 HDD 形态（WAL 与双写抢同一块盘）
   - BufHashTableSearch 60 万次 avg≈0——计数型括号，访问量镜像，不作瓶颈
     （`core-events §1` 判读的直接演示）
5. **定性（长事务×膨胀勾稽，本案例最有价值的一段）**：
   Long Transaction 榜：**同一会话 5.8 天**（自 10-15 21:17，active，
   unique_sql_id=2129122731=带 pg_sleep 的 count 查询）——OldestXmin=528193
   被它钉死 → 对象榜 **gs_asp 表 dead 232,706 = live 86,120 的 2.7 倍**——
   `autovacuum.md`/`mvcc-visibility.md` 的"vaccum 在跑也清不掉"判定链的
   活样本（autovacuum_mode=mix、naptime=20s 都正常，卡的是 xmin 边界）
6. **多源对齐**：日志（logrpt）窗内 ERROR/WARNING/FATAL 全 0——排除报错面；
   ASP 桶 wait_io 峰值 1——与 DoubleWrite 事件同源不矛盾

**另一个方法论发现**：本窗 SQL 榜 Top10 **全部是 AWR 采集自身的 SQL**
（COPY pg_stat_*、gs_asp 分析查询、pg_settings 巡检、gs_stack 采样）——
**空载库的 SQL 榜主体=监控自身开销**，判读第一步先认出采集指纹再谈业务 SQL
（已进 `awr-report-analysis.md` 应继承的判读点）。

## 根因（三条并列，非单一）

1. 库空载属实：AAS≈0，"OS 忙"来自采集与构造负载痕迹（pg_sleep 查询），
   **不是数据库瓶颈**
2. **5.8 天残留长事务**（测试遗留）钉死 OldestXmin → gs_asp 膨胀——真隐患
3. HDD vda 上双写/WAL 单次延迟高（308ms/798ms）——介质形态，OS 侧参数已
   收敛，属容量/介质边界

## 处置与结果

- 杀残留长事务会话（`blocking-live.md` 止损链）→ vacuum 后 gs_asp dead 回收
  〔结果未复测⚓——本窗为样本复盘，非现场处置〕
- 采集开销评估：空载库上采集 SQL 占榜，核对采样频率与窗口策略

## 预防

- **idle_in_transaction_session_timeout 默认 0=禁用**（env-A 实测同款默认）
  ——残留长事务无人管，按环境定夺开启
- 长事务巡检常态化（vb_tool ltx 榜已有采样）；测试负载会话退出前收尾

## 关联知识文件

`../performance/metrics.md`（口径与%DBtime 陷阱）、`awr-report-analysis.md`
（六步流程）、`../internals/autovacuum.md`+`mvcc-visibility.md`（xmin 勾稽链）、
`../internals/buffer-manager.md`（双写前台链）、`../linux/io-storage.md`
（HDD 阈值/OS 回写收敛对照）、`../troubleshooting/blocking-live.md`（止损）

## 遗留

- 同批 4 个连窗未展开（趋势面）；长事务来源会话细节未存档；
  处置后复测样本缺失——三件都标"待补"
