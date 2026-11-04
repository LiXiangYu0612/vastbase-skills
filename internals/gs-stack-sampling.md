# gs_stack 抓栈机制与 AWR Callstack 段能力边界

一个主题：内核 `gs_stack(tid)` 怎么抓到线程堆栈、什么条件下抓不到、
vb_tool awr 报告 Callstack Sampling 段的 "-" 到底意味着什么。

## 机制（源码定论）

`gs_stack(tid)`（openGauss 6.0.0 基线 `src/gausskernel/cbb/instruments/gs_stack/gs_stack.cpp:734`）：

1. **权限硬闸**（:736）：`superuser() || isMonitoradmin()`，否则
   `ERROR: Must be system admin or monitor admin to get stack` → 整条语句报错。
   对比：`pg_os_threads` / `pg_thread_wait_status` 底层函数
   （`pg_stat_get_thread`，pgstatfuncs.cpp:3890）**无任何权限检查**——
   普通监控用户能拿到线程清单却调不动 gs_stack，这种不对称会让"有 be 行
   无堆栈"看起来像抓栈坏了，实际是权限。
2. **抓法 = 信号自采**（:437-461）：查询会话拿 `GsStackLock`（LW_EXCLUSIVE）
   → 给目标线程发 `SIGURG` → 目标线程自己的信号处理器跑 glibc
   `backtrace()` 写共享内存 → 查询侧轮询等它完成。
3. **等待窗口只有 5ms**（:425 `MAX_WAIT=1000 × US_PER_WAIT=5µs`）。目标线程
   在窗口内没被调度上（深睡眠/D 态长 IO/极端饥饿）→ 超时，返回
   **文本** `"thread <tid> not available"`（不是 NULL、不是空串），并 WARNING。
4. **符号化**（:316-395）：dladdr 优先，解析不到再读 ELF 的
   SHT_SYMTAB/SHT_DYNSYM 自己查（每符号一次 24B read()，条目按 ELF 文件名
   缓存在共享内存 GsStackHashTbl，实例生命周期内只加载一次）。
   现网 vastbase 二进制 **stripped**（179MB，只有 .dynsym 2.2MB/9.3 万符号，
   --export-dynamic 构建）→ 内部符号仍可解析，解析不到的帧输出裸地址
   （正常现象，env-T 实测链里混有 `0x1d83fa8` 这类帧）。
5. 每次调用固定写 LOG：`wait %d times.`（成功也是 "wait 0/1 times"）+
   每个符号化帧一条 demangle LOG → **pg_log 是抓栈行为的天然审计日志**。

## AWR Callstack Sampling 段的覆盖面（vb_tool 侧）

- 只抓**固定后台线程**（Wal Writer ±Auxiliary/CheckPointer/全部 walsender/
  walreceiver/startup/redo）+ **最高 CPU 的那个 AutoVacWorker**（汇总成一行）
  + **每个 LWLock 等待事件上最长等待的一个前台会话**。5 分钟一采样，
  不做全前台会话堆栈。
- 前台段 "no LWLock-waiting foreground sessions" 多数时候是**真没有**：
  CPU 饱和型故障里前台都在跑（wait_event 无 LWLOCK_EVENT），不是没抓到。
- 堆栈列链路：采样器 ONE 条 psql `gs_stack(tid) ... from (values ...)`
  查所有 tid（vb_tool `_awr_lt_stack_sample`），awk 按 tid 回填 chain。

## "-" 的判读矩阵（实验定论，env-T 同 build 实测 2026-11-02）

| 报告 Stack 列形态 | 落盘 chain | 根因分支 |
|---|---|---|
| 完整符号链 | 非空 | 正常 |
| `thread <tid> not available` 文本 | 非空(该文本) | 5ms 窗口超时：目标在 D 态长 IO/深睡 |
| **裸 `-`（所有行）** | **空** | 两分支：①stacks psql 语句级失败（权限 ERROR / statement_timeout(30s) 取消 / OOM / 函数缺失）——pg_log 有对应 ERROR/cancel 行；②**psql 成功但该 build 的 gs_stack 空返回**（G100 V2.2 Build 19 实测）——pg_log 干净，版本相关 |
| 整段 "no backend-thread rows" | 无行 | 窗口内无 5 分钟采样点（如几秒冒烟窗） |

配套事实（env-T 实验，2 核 load 18.7 = 9 倍超载）：
- **纯 CPU 饱和不破坏抓栈**——忙线程立刻处理 SIGURG，链全。
- 冷加载成本上界 = .dynsym 2.2MB + 9.3 万次 24B read()，空载秒级以内；
  正常规模实例上"首次调用超 30s"不成立。
- 目标线程卡 D 态 IO（实验：spill 到盘的聚合后端）→ 空载也超时
  → "not available" 文本。

## 定位配方（现网抓到 "-" 时）

0. **先看 Build 号**（`vb_version()`）：V2.2.10 无此函数（加入点在
   2.2.11~14）；V2.2.15 可用；**V2.2.19 空返回=延续线回归**；V3.0.8 已测
   全链（env-T/env-V4）；V3.0.9 未测（同线推断正常，env-T2 可达时补）——2.2.x
   档先手工验一把再下结论，V3.0 档再往下查。
1. 找采样时刻的 pg_log（`$PGDATA/../vblogs/vastbase/pg_log/dn_6001/` 布局
   或 `show log_directory`），grep 采样分钟 `:00/:05/...`：
   - `canceling statement due to statement timeout` → 30s 取消（饥饿/IO 停滞）
   - `Must be system admin or monitor admin` → 采样会话权限
   - `out of shared memory` → GsStackContext 分配失败
   - 干净 + 全空链 → 该 build 空返回（上表分支②）
   - 有 `wait N times.` → gs_stack 跑了，问题在 vb_tool 回填侧（另查）
2. 手工复跑（vastbase 用户）：采样器同款 SQL 单 tid 验证
   （`select gs_stack(<tid>) is null, length(gs_stack(<tid>))`）。
3. 2026-11-02 起 vb_tool 采样器双形态留痕：语句失败 → `ts|st|error|<首行
   psql 错误>`；rc=0 全空链 → `ts|st|error|gs_stack returned empty chain
   for every tid (rc=0; kernel build behavior?)`。报告 Background Threads
   表底显示 "N stack-fetch error(s); last error: ..."——新故障自解释。

## 版本注记

- 源码基线 openGauss 6.0.0（5.0.0 分支同文件逐字节一致，已对账）；
  实测环境 env-T / env-V4 = Vastbase G100 **V3.0 Build 8**（commit 29407）。
- **★版本相关实测分层（2026-11-02 定案）**：
  - **V3.0 Build 8**（env-T x86_64、env-V4 aarch64）——gs_stack 存在且全链正常
    （env-T 现网 687/687 采样行全链）。
  - **V2.2 Build 10**（116）——**函数不存在**（2026-11-02 用户确认），
    调用即 `ERROR: function gs_stack(bigint) does not exist`；旧采样器
    （≤手改前）吞错 → 全空链。
  - **V2.2.15（LTS）——函数存在且实际出链可用**（2026-11-02 用户确认）；
    加入点在 2.2.11~14（DMR）之间，具体哪个未钉死。
    **⇒ 2.2.19 空返回定性为 2.2.16~19 延续线回归**（2.2.15 可用 + 3.0.8
    可用作对照，证据链可报厂商：rc=0 + 日志 GUC 必留错 + 两窗零
    ERROR/WARNING + 全 tid 空返回）。
  - **V2.2 Build 19**（客户 env-W2，Kylin x86_64，B 兼容，
    vb_tool v2.5.17；2026-01-19 编译，**谱系外 build**——官方版本树无
    2.2.19，≠ 3.0.8，见 `admin/version-map.md`）——**函数存在但对每个
    tid 返回空**（rc=0）。**其 vb_version() 自报 `openGauss version:6.0.0`
    + patch:3** ⇒ 与本文源码基线同源；原版 OG 6.0 的 gs_stack 不存在
    空返回路径（超时→文本+WARNING；成功→链/裸地址）⇒ 空返回与原版行为
    直接矛盾 = **VastData fork 改动所致**，回归定性加硬。脚本侧已三层
    排除（v2.5.17↔HEAD 采样器+加载器+渲染器 diff 仅差留痕补丁；同字节
    在 3.0.8/v2.5.14 出全链）。GUC 证据
    链：该机 `log_min_messages=warning` + `log_min_error_statement=error` +
    `logging_collector=on`（AWR 参数表实测）⇒ 缺函数的 ERROR、超时的
    WARNING 都必然落 pg_log；两窗（11-01 01:00 平静 + 06-07 事件）logrpt
    零 ERROR/WARNING ⇒ 语句从未报错 ⇒ 空返回。内核内部走哪条路
    （NULL vs 空文本）源码不可得，属推断；可观测签名是定论。
- ⚠ 同名实例陷阱：`dn_6001_6002` 实例名在客户多台机器复用（env-V1/env-V4/
  env-W 机），对账必须看 Host Name + Build 号，不能只看实例名。
- env-V1（CentOS 7，V3.0B8，1 核）= 冒烟环境，与本案无关。

## 来源

- 源码精读：`<openGauss-6.0.0-src>/src/gausskernel/cbb/instruments/gs_stack/gs_stack.cpp`
  （全文 754 行，2026-11-02）；`pgstatfuncs.cpp:3890`（pg_stat_get_thread 无权限检查）；
  `system_views.sql:2722`（pg_os_threads 定义）
- vb_tool 源码：`_awr_lt_stack_sample`（stacks SQL/回填/error 行）、
  `load_stack_samples`/`callstack_section_html`（渲染）
- env-T 实测（2026-11-02）：687 行 be 样本全链；18 spinner 饱和实验；
  D 态目标超时实验；stripped 二进制 readelf；首调用 pg_log 时间戳；
  gs_stack_bogus 沙箱红绿（st|error 行）
- 样本：env-W 现场机（V2.2B19）AWR×2 全空链 + pg_log 干净窗
  （D:\log\WXWork\...\2026-11\awr_dn_6001_6002_{01,06}*.html 及同窗 logrpt）；
  env-V4（V3.0B8 aarch64）同窗全链（2026-10 两份）；env-V1（V3.0B8）
  6 秒冒烟窗无采样点
