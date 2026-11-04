# 案例：sys 间歇性冲高——归档 fork 引发 COW+TLB 广播风暴（env-E，2026-11）

## 环境

- env-E（现场生产库）：CentOS 7.9（内核 3.10.0-1160.el7），144 核 Xeon 8374C，
  1.5TB 内存；VastBase G100 单实例，线程池模式，shared_buffers 300GB+；
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
   子进程、5.5s 后 exec 成 vb_probackup；全部 54 个 FORK 中唯一进程级
   （子 tgid≠主 pid），无第二源头

## 判据表（栈指纹→结论，叶子在前）

| 栈指纹 | 结论 |
|---|---|
| `smp_call_function_many←native_flush_tlb_others←flush_tlb_page←ptep_clear_flush←wp_page_copy←do_wp_page←page_fault` | COW 写故障 + 全核 TLB 广播（本案主风暴） |
| `copy_pte_range←copy_page_range←dup_mm←copy_process←do_fork` | fork 页表复制税（发起线程被卡） |
| `unmap_page_range←exit_mmap←flush_old_exec←sys_execve` | 子进程 exec 拆继承地址空间 |
| `do_numa_page←numa_migrate_prep←migrate_misplaced_page←migrate_pages←try_to_unmap` | NUMA 平衡底噪（常在、量小，勿当主因） |

## 根因

线程池模型下 archiver 是主进程线程；执行归档命令走 `gs_popen_security`
（**fork+exec shell+pclose 干等**，src/gausskernel/process/postmaster/
pgarch.cpp:853）——**每次归档都从全库最胖的 mm 里 fork**。实测税单
（300G shared_buffers、144 核）：

- ① fork 复制页表 → **3.2s**（实测）；页表量以 `/proc/<pid>/status` 的
  VmPTE 为准（300G shared_buffers 份额估算 ≈600MB，全进程实测读数更大，
  成分拆解见遗留）
  （发起线程停摆=归档延迟）；遍历中渐进把父进程 MAP_PRIVATE PTE 改只读，
  风暴先于 fork 完成点燃
- ② worker 写私有页 72,646 次 `wp_page_copy` 整页重拷（283MB）/4.5s；
  共享内存 MAP_SHARED 不参与 COW（35s 基线 wp≈0 自证）
- ③ 每次 COW `flush_tlb_page→smp_call_function_many` 广播 144 核，
  合计千万级 IPI——**sys 冲高的主体**
- ④ 子进程 execve 拆继承的 300GB 址空间（exit_mmap）再 **5.4s**
- 对照：PG 原版 archiver 是独立小进程（不碰 buffer 页、页表稀疏、fork
  便宜）；线程池把 archiver 并进主进程 mm 是放大器——**架构税非代码 bug**

## 处置与结果

- 根治方案已定（见预防），**实施与复测未完成⚓**——修复后同窗口复采，
  判据表①③④帧数应归零、sys 尖峰消失
- 快速缓解（不重启）：`wal_segment_size` 调大（16M→256M）降归档频率，
  风暴次数÷16

## 预防

1. **2MB HugeTLB 大页**（治①④）：共享区页表 600MB→1.2MB（÷500），fork
   与 exec 拆除双双归零；openGauss `enable_huge_pages`/`huge_page_size`
   GUC（src/common/backend/port/sysv_shmem.cpp），内核预留
   `vm.nr_hugepages`，重启生效。THP≠大页（动态合并私有匿名页，另有
   拆分风暴坑），保持 never
2. **降频**：`wal_segment_size` 调大；或归档命令换轻量 cp
3. `kernel.numa_balancing=0`（el7 数据库惯例，省 ≈3% 底噪）
4. **反面教材：减 shared_buffers** ——只线性缩①、不治②③、伤命中率，
   负优化
5. 巡检项：sys 尖峰周期 vs 归档日志时间戳对齐；`/proc/<pid>/status`
   的 **VmPTE**（GB 级=页表肥大，大页化收益的直接读数）

## 关联知识文件

- `../linux/cpu.md`（sys 口径与"sys 间歇性冲高"判读节）
- `../linux/tracing-tools.md`（perf 节"离线符号化"配方）
- `../backup-recovery/pitr-mechanism.md`（归档链路上下文）

## 遗留

- **页表成分拆解⚓**：VmPTE 实测读数（GB 级）> shared_buffers 份额估算
  （614MB@300G），需 `pmap -x <pid> | sort -k3 -rn | head` 拆共享/私有
  成分后修订大页收益占比
- 修复实施与复测数据⚓；归档命令配置原文未取证⚓（确认是否
  vb_probackup archive-push 形态）
- 用户态 dwarf 栈未解开（需生产二进制）——fork 的用户态发起函数名
  未符号化，内核侧链路已闭环
