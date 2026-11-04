# troubleshooting/ — 故障诊断（现场救火）

五件套已落地（2026-10-25），机制依据一律引用 `../internals/` 与
`../linux/` 不复制；与 `performance/` 的分工：那边是报告驱动的性能诊断
（有数据，坐下来分析），这边是现场救火——**第一优先是止损与现场保全，
然后才是定位**。

| 文件 | 场景 |
|---|---|
| `blocking-live.md` | 业务挂起/会话堆积/疑似死锁/假死持锁 |
| `connection-refused.md` | 连接拒绝/超时/连上就断/too many clients |
| `startup-failure.md` | 实例起不来（按序排查清单） |
| `crash-panic.md` | 主进程退出/反复重启/PANIC 现场 |
| `resource-exhaustion.md` | fd/内存/信号量/端口耗尽应急 |
| `cpu-sys-high.md` | sys% 高四层分诊（IPI 广播/缺页/fork 税/切换风暴） |
| `oom-analysis.md` | OOM 四型分诊（内核/cgroup/库侧 53200/换页前驱），触发条件+root cause+闭环对账 |

入口顺序：文件内"处置流程/分诊表"先行；要函数级证据时进
`../linux/tracing-tools.md`。
