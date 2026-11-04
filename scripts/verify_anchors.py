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
