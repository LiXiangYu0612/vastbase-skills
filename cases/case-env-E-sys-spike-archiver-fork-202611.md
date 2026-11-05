# 案例：sys 间歇性冲高——归档 fork 引发 COW+TLB 广播风暴（env-E，2026-11）

## 环境

- env-E（现场生产库）：CentOS 7.9（内核 3.10.0-1160.el7），144 核 Xeon 8374C，
  1.5TB 内存；VastBase G100 单实例（窗口内 2,953 个 comm=worker 的线程被
  采样——comm 仅识别不判读，见 stack-reading），shared_buffers 300GB+；
  归档命令疑似配 vb_probackup（形态待取证，见遗留）
- 取证材料：60 秒 on-CPU perf 采样（`-p` 主进程 `-F 99 --call-graph dwarf`），
  有效窗口 40.7s、22.9 万样本、build-id 对账的 vmlinux 全量符号化

## 症状

- **sys% 间歇性冲高**：每次数秒，周期性复发，节奏≈WAL 段归档频率
- DB 侧无对应慢 SQL；AWR 等待事件不反映该开销（全在内核态）
- 冲高窗口全体 worker 线程 sys 同涨——单线程视角看不出责任者

## 排查链（含弯路）

1. **采集**（现场，开销≈采样率，可生产用）：
   `perf record -F 99 -p <pid> --call-graph dwarf sleep 60`
   同步留 `mpstat 1`、`pidstat -wt -p <pid> 1` 对照尖峰时刻
2. **解析机选择弯路**：采集端是 perf 5.10 backport，同系发行版 perf 3.10
   报 incompatible file format 拒读——**跨机解析前先对 `perf --version` 与
   attr 格式**，换新内核机器解析即可，数据本身无问题
3. **导出护栏**：GB 级 perf.data 跑 `perf script` 用 ulimit -v + timeout +
   输出落盘三护栏，防解析机被拖死
4. **离线符号化**（采集机无 vmlinux 时）：
   a. vmlinux 取自 debuginfo 包（CentOS7 在镜像 centos-debuginfo 树）：
      `rpm2cpio kernel-debuginfo-*.rpm | cpio -idm '*vmlinux'`
   b. **build-id 对账**：`perf buildid-list` 的 [kernel.kallsyms] 与
      `readelf -n vmlinux` 逐位一致，否则符号不可信
   c. KASLR slide：观测地址窗(min/max) 减 2MB 对齐候选映射进
      `_stext.._etext`，全帧覆盖率 ≥99.9% 才收（本案 slide=0x0D200000
      一次命中，260 万帧 99.9997%）
5. **聚合判读弯路（三个坑）**：
   - `grep -cw FORK` 匹配不到 `PERF_RECORD_FORK(`（下划线粘连非词边界）
     ——一度误判"零 fork"；数事件用 `grep -c 'PERF_RECORD_FORK('`
   - perf script 样本行字段=comm、tid（**不带 pid**）；COMM 事件（ts=0
     为附加期合成）会混进按 comm 的统计——样本行判据用 `$NF=="cycles:"`
     + ts 正则，线程/进程归属看 FORK 事件的 pid:tgid
   - "NUMA 迁移"假设半对：do_numa_page/migrate_misplaced 族确实在场
     但只占 ≈3% 底噪——**按时间线对齐定性，不按"在场"定性**
6. **定性**（判据表见下）→ COW+TLB 广播 + fork 页表税 + exec 拆址空间
7. **定责**：FORK 事件时间戳 vs 爆发窗口对齐——archiver 线程 fork 出
   子进程、**5.4s 后** exec 成 vb_probackup；54 个 FORK 中唯一进程级
   （子 tgid≠主 pid），无第二源头；同窗 53 次线程创建（CLONE_VM）零税，
   构成天然对照组

## 判据表（栈指纹→结论，叶子在前）

| 栈指纹 | 结论 |
|---|---|
| `smp_call_function_many←native_flush_tlb_others←flush_tlb_page←ptep_clear_flush←wp_page_copy←do_wp_page←page_fault` | COW 写故障 + 全核 TLB 广播（本案主风暴） |
| `copy_pte_range←copy_page_range←dup_mm←copy_process←do_fork` | fork 页表复制税（发起线程被卡；**仅无 CLONE_VM 的真进程 fork 有此栈**） |
| `unmap_page_range←exit_mmap←flush_old_exec←sys_execve` | 子进程 exec 拆继承地址空间 |
| `do_numa_page←numa_migrate_prep←migrate_misplaced_page←migrate_pages←try_to_unmap` | NUMA 平衡底噪（常在、量小，勿当主因） |

## 根因

VB 线程模型下 archiver 是主进程的后台线程；执行归档命令走 `gs_popen_security`
（**经典 fork+exec shell+pclose 干等**，src/gausskernel/process/postmaster/
pgarch.cpp:853；fork 在 gs_system.cpp:128）——**每次归档都从全库最胖的 mm
里走唯一全价路径 fork**。四笔税实测（VmPTE≈5GB、144 核）：

- **①页表复制税 3.2s**：fork 系统调用**内部同步**完成（阻塞 archiver），
  PERF_RECORD_FORK 事件在 copy_process 收尾才发——所以首个 wp 故障比
  FORK 事件**早 0.2s**：copy_page_range 遍历到哪、MAP_PRIVATE 的 COW 布防
  （父子写位双清零）就布到哪，热区先燃
- **②COW 数据拷贝 4.5s**：worker 写私有页触发 wp_page_copy 整页重拷
  72,646 次（283MB）。共享内存 MAP_SHARED 不参与 COW（35s 基线 wp≈0
  自证）；每页只拷一次（引用=1 后 wp_page_reuse 免拷）——风暴自熄的机制
- **③TLB 广播**：每次 COW 的 ptep_clear_flush→smp_call_function_many
  广播 144 核、合计千万级 IPI——**sys 冲高主体**（③随②逐次发生）
- **④exec 拆除税 5.4s**：子进程 execve→exit_mmap 拆整套继承地址空间
  （共享区+私有区的页表与映射，非数据拷贝）
- 对照：PG 原版 archiver 是独立小进程（不碰 buffer 页、页表稀疏、fork
  便宜）；线程模型把 archiver 并进主进程 mm 是放大器——**架构税非代码 bug**

### 机制边界（详账见关联文件，此处只留判读要点）

- **拷贝时机**：页表拷贝发生在 fork 系统调用内、命令字符串被解析**之前**
  ——税与命令内容无关（archive_command 用 cp 与用 vb_probackup 同税；
  sh 起来后再 fork cp 那层是瘦 mm 免税）
- **拷什么**：拷**地图**（页表/映射记录）不拷**领土**（数据页）——fork 时
  一页数据都不拷；数据拷贝由"写"惰性触发且只属 MAP_PRIVATE（读永不拷、
  SHARED 连写都不拷）
- **谁交税**：只看 clone flags——子 tgid≠父 pid（无 CLONE_VM 真进程）全税；
  线程（CLONE_VM 共享 mm）零税（本案 53:1 对照）；posix_spawn
  （CLONE_VM|CLONE_VFORK）零税=代码级根治路径
- **减免账**：HugeTLB 区无 PTE 层（每 2MB 一个 PMD 表项，主力减免）；
  未触达空洞无 PTE 可拷；swap 占位 PTE **不算洞**（照拷）；THP 不减免
  反重（2MB 粒度 COW）；客户端工具（gs_dump/pg_basebackup/vb_probackup
  直连）不经此路径（独立进程+服务端会话线程，源码核 pg_dump.cpp:587）

## 处置与结果

- 根治方案已定（见预防），**实施与复测未完成⚓**——修复后同窗口复测的
  **分项预期**：archive_dest/posix_spawn 则①②③④全消；仅大页则①④消除、**②③不归零**
  （私有集 COW+广播是 fork 布防的固有税，只随降频变稀）
- 快速缓解（不重启）：`wal_segment_size` 调大（16M→256M）降归档频率，
  整包税少交 1/16

## 预防（2026-11-05 按成分未明重排：回避路径优先于大页）

1. **archive_dest 形态**（首选，零成本）：归档走 archive_dest 即**不经
   fork**（pgarch.cpp:608 进程内拷贝）——①②③④全消，无需重启窗口
2. **posix_spawn 化**（厂商诉求）：popen_security 现用经典 fork
   （gs_system.cpp:128 实锚），换 posix_spawn=零页表拷贝，①④双消、②③随布防消失而消
   （②随 fork 消失而消）
3. **2MB HugeTLB 大页**：共享区份额 614MB→1.2MB——但 VmPTE 总量 ≈5GB
   中共享区理论份额仅 ~0.6G，**成分拆解未明前收益占比不定**（见遗留）；
   openGauss `enable_huge_pages`/`huge_page_size` GUC
   （src/common/backend/port/sysv_shmem.cpp），内核预留
   `vm.nr_hugepages`，重启生效。THP≠大页，保持 never
4. **降频**：`wal_segment_size` 调大（同税少交几次）
5. `kernel.numa_balancing=0`（el7 数据库惯例，省 ≈3% 底噪）
6. **反面教材：减 shared_buffers** ——只线性缩①、不治②③④、伤命中率，
   负优化
7. 巡检项：sys 尖峰周期 vs 归档日志时间戳对齐；`/proc/<pid>/status`
   的 **VmPTE**（GB 级=页表肥大读数）

## 关联知识文件

- `../troubleshooting/cpu-sys-high.md`（sys 高四层分诊 + fork 税完整边界账：
  路径/映射两张表、税与命令无关、客户端免税对照）
- `../linux/memory.md`（页表节：fork 三层账——拷地图不拷领土/COW 惰性拷）
- `../linux/cpu.md`（sys 口径与"sys 间歇性冲高"判读节）
- `../linux/tracing-tools.md`（perf 节"离线符号化"配方）
- `../backup-recovery/backup-window-performance.md`（备份窗口四形态与
  archive_dest 回避路径）、`../backup-recovery/pitr-mechanism.md`

## 遗留

- **页表总量已确认**：≈5GB（现场实查 2026-11-05；★口径护栏：按进程级
  VmPTE 理解，若实为 /proc/meminfo PageTables（全机）则需以命令①输出
  复核——fork 只拷进程自己的页表）；
  **成分拆解⚓仍开**：共享区理论份额仅 ~614MB@300G，5G 大头若在私有/换出
  区（swap 项 PTE 常驻可解释超物理内存的等价映射量），则大页收益占比有限，
  **archive_dest（不 fork）与 posix_spawn（零拷贝）为首选处置**，
  大页降级为部分缓解——待 pmap 拆解定占比
- 修复实施与复测数据⚓；归档命令配置原文未取证⚓（确认是否
  vb_probackup archive-push 形态）
- 用户态 dwarf 栈未解开（需生产二进制）——fork 的用户态发起函数名
  未符号化，内核侧链路已闭环
