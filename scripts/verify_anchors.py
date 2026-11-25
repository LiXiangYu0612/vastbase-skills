#!/usr/bin/env python3
"""源码锚点复核器：对知识文件引用的关键断言逐条回源码对账。

用法：python3 scripts/verify_anchors.py
每条断言 = (源码树, 文件, 匹配串, 声称行号)。输出 PASS(±容差内)/DRIFT(实际行号)/
FAIL(找不到)。行号漂移只改知识文件行号不判错；匹配串找不到才是 FAIL（读错源码）。
"""
import os
import subprocess
import sys
from pathlib import Path

TREES = {
    "og": os.environ.get("VASTBASE_OG_SRC", "/mnt/d/openGauss-server-v6.0.0/openGauss-server-v6.0.0/src"),
    "lk": os.environ.get("VASTBASE_LINUX_SRC", "/mnt/d/Obsidian/linux-master"),
    "cm": os.environ.get("VASTBASE_CM_SRC", "/mnt/d/og-cm-src/src"),
    "fio": os.environ.get("VASTBASE_FIO_SRC", "/home/xiang/kernel-src/fio-3.7-mirror"),
}

# (树, 相对路径, 必须存在的串, 声称行号, 出处知识文件)
CLAIMS = [
    ("lk", "kernel/sched/loadavg.c", "exponentially decaying average of nr_running +", 17, "cpu.md"),
    ("lk", "kernel/sched/loadavg.c", "avenrun[0] = calc_load_n(avenrun[0], EXP_1", 321, "cpu.md"),
    ("lk", "include/linux/sched/loadavg.h", "#define LOAD_FREQ", 20, "cpu.md"),
    ("lk", "kernel/softirq.c", "#define MAX_SOFTIRQ_TIME", 543, "cpu.md"),
    ("lk", "kernel/softirq.c", "#define MAX_SOFTIRQ_RESTART", 544, "cpu.md"),
    ("lk", "kernel/sched/core.c", "about to go to sleep on IO", 8089, "cpu.md"),
    ("lk", "include/linux/sched.h", "#define TASK_UNINTERRUPTIBLE", 109, "cpu.md"),
    ("lk", "kernel/sched/fair.c", "static struct sched_entity *pick_eevdf", 1136, "cpu.md"),
    ("lk", "mm/page-writeback.c", "int dirty_background_ratio = 10", 75, "io-storage.md"),
    ("lk", "mm/page-writeback.c", "static int vm_dirty_ratio = 20", 92, "io-storage.md"),
    ("lk", "mm/page-writeback.c", "dirty_writeback_interval = 5 * 100", 103, "io-storage.md"),
    ("lk", "mm/page-writeback.c", "dirty_expire_interval = 30 * 100", 110, "io-storage.md"),
    ("lk", "include/uapi/linux/oom.h", "#define OOM_SCORE_ADJ_MIN", 9, "memory.md"),
    ("lk", "mm/vmscan.c", "int vm_swappiness = 60", 201, "memory.md"),
    ("lk", "mm/oom_kill.c", "static void dump_tasks", 422, "memory.md"),
    ("lk", "include/net/tcp.h", "#define TCP_TIMEWAIT_LEN", 142, "network.md"),
    ("lk", "include/net/tcp.h", "#define TCP_TIMEOUT_INIT", 169, "network.md"),
    ("lk", "net/ipv4/tcp_minisocks.c", "sysctl_tcp_abort_on_overflow", 951, "network.md"),
    ("lk", "net/netfilter/nf_conntrack_core.c", 'table full, dropping packet', 1674, "network.md"),
    ("lk", "include/net/tcp.h", "#define TCP_KEEPALIVE_TIME", 180, "network.md"),
    ("lk", "net/ipv4/inet_connection_sock.c", "static void reqsk_timer_handler", 1024, "network.md"),
    ("lk", "kernel/events/core.c", "SYSCALL_DEFINE5(perf_event_open", 13844, "tracing-tools.md"),
    ("lk", "kernel/ptrace.c", "PTRACE_SYSCALL", 840, "tracing-tools.md"),
    ("og", "gausskernel/storage/ipc/procarray.cpp", "Snapshot GetSnapshotData(Snapshot snapshot, bool force_local_snapshot, bool forHSFeedBack)", 2018, "locks.md"),
    ("og", "common/backend/utils/error/elog.cpp", "case PANIC:", 2649, "crash-panic.md"),
    ("og", "common/backend/utils/misc/guc.cpp", "max_files_per_process", 2290, "resource-exhaustion.md"),
    ("og", "gausskernel/process/postmaster/postmaster.cpp", "static int BackendStartup(Port* port", 9086, "connection-lifecycle.md"),
    ("og", "gausskernel/process/postmaster/postmaster.cpp", "cleaning connection to reduce memory usage", 9116, "connection-lifecycle.md"),
    ("og", "gausskernel/process/postmaster/postmaster.cpp", "int ProcessStartupPacket(Port* port", 4639, "connection-lifecycle.md"),
    ("og", "common/backend/libpq/pqcomm.cpp", "address already in use", 729, "startup-failure.md"),
    ("og", "gausskernel/storage/access/transam/xlog.cpp", "static int get_sync_bit(int method)", 14504, "commit-sync-events.md"),
    ("og", "gausskernel/storage/buffer/freelist.cpp", "BufferDesc* StrategyGetBuffer(", 180, "buffer-manager.md"),
    ("og", "gausskernel/storage/buffer/freelist.cpp", "ClockSweepTick(int max_nbuffer_can_use)", 109, "buffer-manager.md"),
    ("og", "gausskernel/runtime/executor/nodeHash.cpp", "static void ExecHashIncreaseNumBatches(HashJoinTable hashtable)", 998, "operator-memory.md"),
    ("og", "gausskernel/optimizer/commands/vacuum.cpp", "void vacuum_delay_point(void)", 2945, "autovacuum.md"),
    ("og", "gausskernel/optimizer/commands/analyze.cpp", "static int64 acquire_sample_rows(", 2471, "stats-to-cost.md"),
    ("og", "gausskernel/storage/access/transam/xlog.cpp", "static bool RestoreArchivedFile(", 4819, "pitr-mechanism.md"),
    ("og", "gausskernel/storage/access/transam/xlog.cpp", "bool CheckPromoteSignal(void)", 18341, "failover.md"),
    ("cm", "cm_server/cms_arbitrate_cluster.cpp", "static bool IsNodeRhbAlive(", 455, "failover.md"),
    ("cm", "cm_server/cms_global_params.cpp", "instance_heartbeat_timeout = 6", 147, "failover.md"),
    ("cm", "cm_agent/cma_global_params.cpp", "agent_heartbeat_timeout = 8", 179, "failover.md"),
    # 2026-10-26 全链路复核扩容（新机制件）
    ("og", "gausskernel/runtime/executor/nodeNestloop.cpp", "prm->isChanged = true", 196, "executor-operators.md"),
    ("og", "gausskernel/runtime/executor/nodeAgg.cpp", "int64 workMem = SET_NODEMEM(plan->operatorMemKB[0], plan->dop)", 249, "executor-operators.md"),
    ("og", "gausskernel/runtime/executor/nodeMaterial.cpp", "tuplestore_begin_heap(true, false, operator_mem", 54, "executor-operators.md"),
    ("og", "gausskernel/storage/access/nbtree/nbtinsert.cpp", "static OffsetNumber _bt_findsplitloc(Relation rel", 77, "index-btree.md"),
    ("og", "gausskernel/storage/access/nbtree/nbtdedup.cpp", "IndexTuple btree_dedup_form_posting(", 25, "index-btree.md"),
    ("og", "common/backend/libpq/auth.cpp", "void ClientAuthentication(Port* port)", 350, "connection-lifecycle.md"),
    ("og", "gausskernel/storage/access/transam/xlog.cpp", "void CreateCheckPoint(int flags)", 12149, "flush-checkpoint.md"),
    ("og", "gausskernel/storage/access/transam/xlog.cpp", "bool CheckPromoteSignal(void)", 18341, "failover.md"),
    ("cm", "cm_server/cms_arbitrate_cluster.cpp", "static bool IsNodeRhbAlive(", 455, "failover.md"),
    ("lk", "net/ipv4/tcp_timer.c", "static void tcp_keepalive_timer(struct timer_list *t)", 779, "network.md"),
    # 2026-11-19 cgroup.md 立件登记（7.1-rc4 行级）
    ("lk", "kernel/sched/fair.c", "static bool throttle_cfs_rq(struct cfs_rq *cfs_rq)", 6239, "cgroup.md"),
    ("lk", "kernel/sched/fair.c", "static int do_sched_cfs_period_timer(struct cfs_bandwidth *cfs_b, int overrun", 6491, "cgroup.md"),
    ("lk", "kernel/sched/fair.c", "cfs_b->nr_throttled += overrun;", 6520, "cgroup.md"),
    ("lk", "kernel/sched/fair.c", "static unsigned int sysctl_sched_cfs_bandwidth_slice", 125, "cgroup.md"),
    ("lk", "kernel/sched/core.c", "static int tg_cfs_schedulable_down(struct task_group *tg, void *data)", 9910, "cgroup.md"),
    ("lk", "kernel/sched/core.c", "static int cpu_extra_stat_show(struct seq_file *sf,", 10301, "cgroup.md"),
    ("lk", "kernel/sched/core.c", "static int cpu_local_stat_show(struct seq_file *sf,", 10329, "cgroup.md"),
    ("lk", "kernel/cgroup/cpuset.c", "void rebuild_sched_domains(void)", 1027, "cgroup.md"),
    ("lk", "kernel/cgroup/pids.c", "static int pids_try_charge(struct pids_cgroup *pids, int num", 166, "cgroup.md"),
    ("lk", "mm/memcontrol.c", "static unsigned long reclaim_high(struct mem_cgroup *memcg,", 2300, "cgroup.md"),
    ("lk", "mm/memcontrol.c", "memcg_memory_event(memcg, MEMCG_HIGH);", 2313, "cgroup.md"),
    ("lk", "mm/memcontrol.c", "void __mem_cgroup_handle_over_high(gfp_t gfp_mask)", 2477, "cgroup.md"),
    ("lk", "block/blk-throttle.c", "static ssize_t tg_set_limit(struct kernfs_open_file *of,", 1527, "cgroup.md"),
    ("lk", "block/blk-throttle.c", "#define DFL_THROTL_SLICE (HZ / 10)", 24, "cgroup.md"),
    ("lk", "block/blk-core.c", "if (blk_throtl_bio(bio))", 882, "cgroup.md"),
    ("lk", "block/blk-mq.c", "rq_qos_throttle(q, bio);", 3063, "cgroup.md"),
    # 2026-11-19 io-storage blk-mq 升行级登记
    ("lk", "block/blk-mq.c", "static struct request *__blk_mq_alloc_requests(struct blk_mq_alloc_data *data)", 537, "io-storage.md"),
    ("lk", "block/blk-mq.c", "tag = blk_mq_get_tag(data);", 576, "io-storage.md"),
    ("lk", "block/blk-mq.c", "void blk_mq_run_hw_queue(struct blk_mq_hw_ctx *hctx, bool async)", 2352, "io-storage.md"),
    ("lk", "block/blk-mq.c", "static int blk_mq_hctx_notify_dead(unsigned int cpu, struct hlist_node *node)", 3844, "io-storage.md"),
    ("lk", "block/blk-mq-tag.c", "unsigned int blk_mq_get_tag(struct blk_mq_alloc_data *data)", 137, "io-storage.md"),
    ("lk", "block/blk-mq-tag.c", "static int __blk_mq_get_tag(struct blk_mq_alloc_data *data,", 109, "io-storage.md"),
    ("lk", "block/blk-mq-sched.c", "static int __blk_mq_sched_dispatch_requests(struct blk_mq_hw_ctx *hctx)", 268, "io-storage.md"),
    ("lk", "block/elevator.c", "static int elevator_switch(struct request_queue *q, struct elv_change_ctx *ctx)", 562, "io-storage.md"),
    ("lk", "block/elevator.c", "blk_mq_quiesce_queue(q);", 576, "io-storage.md"),
    # 2026-11-19 现代 SSD/NVMe 域登记（HDD 残留清理, 用户令）
    ("lk", "block/elevator.c", "void elevator_set_default(struct request_queue *q)", 729, "io-storage.md"),
    ("lk", "block/elevator.c", "if (q->tag_set->flags & BLK_MQ_F_NO_SCHED_BY_DEFAULT)", 740, "io-storage.md"),
    ("lk", "include/linux/blk-mq.h", "BLK_MQ_F_NO_SCHED_BY_DEFAULT", 709, "io-storage.md"),
    ("lk", "drivers/nvme/host/core.c", "info->is_rotational = id->nsfeat & NVME_NS_ROTATIONAL;", 1681, "io-storage.md"),
    ("lk", "drivers/nvme/host/core.c", "lim.features |= BLK_FEAT_WRITE_CACHE | BLK_FEAT_FUA;", 2429, "io-storage.md"),
    ("lk", "drivers/nvme/host/core.c", "BUILD_BUG_ON(sizeof(struct nvme_smart_log) != 512);", 5394, "io-storage.md"),
    ("lk", "include/linux/nvme.h", "struct nvme_smart_log {", 742, "io-storage.md"),
    ("lk", "include/linux/nvme.h", "__u8\t\t\tpercent_used;", 747, "io-storage.md"),
    ("lk", "include/linux/nvme.h", "__u8\t\t\tdata_units_written[16];", 751, "io-storage.md"),
    # 2026-11-25 fio.md 立件登记（fio-3.7 源码镜像 + 内核侧）
    ("fio", "diskutil.c", "agg->ios[0] / agg->slavecount", 507, "fio.md"),
    ("fio", "diskutil.c", "static void aggregate_slaves_stats(struct disk_util *masterdu)", 528, "fio.md"),
    ("fio", "diskutil.c", "agg->slavecount++;", 548, "fio.md"),
    ("fio", "engines/sync.c", "static enum fio_q_status fio_syncio_queue(struct thread_data *td,", 194, "fio.md"),
    ("fio", "engines/sync.c", "ret = read(f->fd, io_u->xfer_buf, io_u->xfer_buflen);", 203, "fio.md"),
    ("fio", "engines/sync.c", ".flags\t\t= FIO_SYNCIO,", 411, "fio.md"),
    ("fio", "filesetup.c", "log_info(\"%s: Laying out IO file%s (%u file%s / %s%lluMiB)\\n\",", 1098, "fio.md"),
    ("lk", "fs/iomap/direct-io.c", "iomap_dio_rw() always completes O_[D]SYNC writes regardless of whether the IO", 661, "fio.md"),
    ("lk", "block/blk-sysfs.c", "QUEUE_LIM_RW_ENTRY(queue_wc, \"write_cache\");", 660, "fio.md"),
    # 2026-11-19 reset 风暴案判读锚（network.md EstabResets 四律）
    ("lk", "net/ipv4/tcp.c", "TCP_INC_STATS(sock_net(sk), TCP_MIB_ESTABRESETS);", 3016, "network.md"),
    ("lk", "net/ipv4/tcp.c", "TCP_INC_STATS(sock_net(sk), TCP_MIB_ATTEMPTFAILS);", 5096, "network.md"),
    ("lk", "net/ipv4/tcp_input.c", "void tcp_reset(struct sock *sk, struct sk_buff *skb)", 4852, "network.md"),
    ("lk", "net/ipv4/tcp_input.c", "void tcp_done_with_error(struct sock *sk, int err)", 4838, "network.md"),
    # 2026-11-20 page-cache.md 立件登记
    ("lk", "include/linux/fs.h", "struct address_space {", 473, "page-cache.md"),
    ("lk", "include/linux/fs.h", "struct address_space_operations {", 401, "page-cache.md"),
    ("lk", "include/linux/fs.h", "#define PAGECACHE_TAG_DIRTY", 495, "page-cache.md"),
    ("lk", "include/linux/mm_types.h", "struct folio {", 401, "page-cache.md"),
    ("lk", "include/linux/mmzone.h", "enum lru_list {", 386, "page-cache.md"),
    ("lk", "mm/filemap.c", "struct folio *__filemap_get_folio_mpol(struct address_space *mapping,", 1941, "page-cache.md"),
    ("lk", "mm/readahead.c", "static void do_page_cache_ra(struct readahead_control *ractl,", 315, "page-cache.md"),
    ("lk", "mm/swap.c", "void folio_add_lru(struct folio *folio)", 506, "page-cache.md"),
    ("lk", "mm/page-writeback.c", "void __folio_mark_dirty(struct folio *folio, struct address_space *mapping,", 2670, "page-cache.md"),
    ("lk", "mm/page-writeback.c", "bool filemap_dirty_folio(struct address_space *mapping, struct folio *folio)", 2710, "page-cache.md"),
    # 2026-11-25 process-states.md 立件登记（v7.1 行级）
    ("lk", "include/linux/sched.h", "#define TASK_RUNNING", 107, "process-states.md"),
    ("lk", "include/linux/sched.h", "#define TASK_KILLABLE", 137, "process-states.md"),
    ("lk", "include/linux/sched.h", "#define TASK_IDLE", 141, "process-states.md"),
    ("lk", "include/linux/sched.h", "#define TASK_NORMAL", 144, "process-states.md"),
    ("lk", "include/linux/sched.h", "#define set_current_state(state_value)", 247, "process-states.md"),
    ("lk", "include/linux/sched.h", "static const char state_char[] = \"RSDTtXZPI\";", 1699, "process-states.md"),
    ("lk", "include/linux/sched.h", "if ((tsk_state & TASK_IDLE) == TASK_IDLE)", 1677, "process-states.md"),
    ("lk", "fs/proc/array.c", "static const char * const task_state_array[] = {", 127, "process-states.md"),
    ("lk", "fs/proc/array.c", "\"I (idle)\"", 140, "process-states.md"),
    ("lk", "fs/proc/array.c", "seq_puts(m, get_task_state(p));", 181, "process-states.md"),
    ("lk", "kernel/hung_task.c", "sysctl_hung_task_timeout_secs = CONFIG_DEFAULT_HUNG_TASK_TIMEOUT", 53, "process-states.md"),
    ("lk", "kernel/hung_task.c", "sysctl_hung_task_warnings = 10", 60, "process-states.md"),
    ("lk", "kernel/hung_task.c", "static bool task_is_hung", 103, "process-states.md"),
    ("lk", "kernel/hung_task.c", "Future hung task reports are suppressed", 268, "process-states.md"),
    ("lk", "lib/Kconfig.debug", "default 120", 1287, "process-states.md"),
    ("lk", "kernel/sched/core.c", "static void block_task(struct rq *rq, struct task_struct *p, int flags)", 2226, "process-states.md"),
    ("lk", "kernel/sched/core.c", "rq->nr_uninterruptible--;", 3713, "process-states.md"),
    ("lk", "kernel/sched/core.c", "int try_to_wake_up(struct task_struct *p, unsigned int state, int wake_flags)", 4152, "process-states.md"),
    ("lk", "kernel/sched/core.c", "static bool try_to_block_task(struct rq *rq, struct task_struct *p,", 6582, "process-states.md"),
    ("lk", "kernel/sched/core.c", "p->sched_contributes_to_load =", 6606, "process-states.md"),
    ("lk", "kernel/sched/core.c", "long __sched io_schedule_timeout(long timeout)", 8092, "process-states.md"),
    ("lk", "kernel/sched/sched.h", "static inline void __block_task(struct rq *rq, struct task_struct *p)", 2978, "process-states.md"),
    ("lk", "kernel/sched/sched.h", "rq->nr_uninterruptible++;", 2981, "process-states.md"),
    ("lk", "kernel/signal.c", "void signal_wake_up_state(struct task_struct *t, unsigned int state)", 721, "process-states.md"),
    ("lk", "kernel/signal.c", "wake_up_state(t, state | TASK_INTERRUPTIBLE)", 734, "process-states.md"),
    ("lk", "kernel/exit.c", "tsk->exit_state = EXIT_ZOMBIE;", 750, "process-states.md"),
    ("lk", "fs/proc/base.c", "static int proc_pid_wchan(struct seq_file *m, struct pid_namespace *ns,", 421, "process-states.md"),
    ("lk", "kernel/sched/loadavg.c", "nr_active += (long)this_rq->nr_uninterruptible;", 85, "process-states.md"),
    # 2026-11-25 D 连续性判别节（top 点采样 vs 计数器定论）
    ("lk", "fs/proc/array.c", "seq_put_decimal_ull(m, \"voluntary_ctxt_switches:\\t\", p->nvcsw);", 400, "process-states.md"),
    ("lk", "fs/proc/array.c", "nonvoluntary_ctxt_switches:", 401, "process-states.md"),
    ("lk", "include/trace/events/sched.h", "TRACE_EVENT(sched_switch,", 220, "process-states.md"),
    ("lk", "include/trace/events/sched.h", "__entry->prev_state\t= __trace_sched_switch_state(preempt, prev_state, prev);", 243, "process-states.md"),
    # 2026-11-25 b 列口径节（procs_blocked=nr_iowait 非数 D）
    ("lk", "fs/proc/stat.c", "nr_iowait());", 183, "process-states.md"),
    ("lk", "kernel/sched/core.c", "unsigned int nr_iowait_cpu(int cpu)", 5452, "process-states.md"),
    ("lk", "kernel/sched/core.c", "unsigned int nr_iowait(void)", 5487, "process-states.md"),
    ("lk", "kernel/sched/core.c", "current->in_iowait = 1;", 8078, "process-states.md"),
    ("lk", "kernel/sched/sched.h", "atomic_inc(&rq->nr_iowait);", 2983, "process-states.md"),
    # 2026-11-25 线程级判读节（thread=task，ps 默认漏线程 D）
    ("lk", "kernel/hung_task.c", "for_each_process_thread(g, t) {", 194, "process-states.md"),
    ("lk", "fs/proc/base.c", 'ONE("status",     S_IRUGO, proc_pid_status),', 3323, "process-states.md"),
    ("lk", "fs/proc/base.c", 'ONE("status",    S_IRUGO, proc_pid_status),', 3669, "process-states.md"),
]


def find_lines(path: Path, needle: str):
    """返回匹配串的全部行号（定义可能晚于前向声明，取全部再判）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [i for i, line in enumerate(text, 1) if needle in line]


def main():
    tol = 15  # 行号容差：跨小版本 rebase 漂移
    bad = drift = ok = 0
    findings = []
    for tree, rel, needle, claimed, src in CLAIMS:
        path = Path(TREES[tree]) / rel
        hits = find_lines(path, needle)
        if not hits:
            bad += 1
            findings.append(f"FAIL  {rel}:{claimed} 找不到串→{src}（读错源码或文件已变）: {needle[:50]}")
        elif not any(abs(h - claimed) <= tol for h in hits):
            drift += 1
            findings.append(f"DRIFT {rel} 声称:{claimed} 实际:{hits} →{src}")
        else:
            ok += 1
    print(f"锚点复核：{len(CLAIMS)} 条 | PASS {ok} | DRIFT {drift} | FAIL {bad}")
    for f in findings:
        print("  " + f)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
