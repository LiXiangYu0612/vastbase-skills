#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""健康总览表：从 awrrpt digest + logrpt 扫描节程序化提取关键统计项，
逐项判定 正常/警告/异常/参考，输出可插入分析报告开头的 HTML 表。

★2026-10-26 用户令：报告只报问题。HTML 片段只列 ■异常/▲警告 行，正常与参考项
一律不展开，只在汇总句里给个数；全部正常时不出表，只留一句"全部正常"。
用法：
  python3 overview_table.py <digest.txt>                 # 打印表格文本 + 写 overview_fragment.html
  python3 overview_table.py <digest.txt> --insert <报告.html> <输出.html>
判定阈值与章节出处见每行"依据"列；阈值修改只改 RULES。"""
import re
import sys
from pathlib import Path

# ---------- 状态 ----------
OK, WARN, BAD, REF = 'ok', 'warn', 'bad', 'ref'
SYM = {OK: '● 正常', WARN: '▲ 警告', BAD: '■ 异常', REF: '○ 参考'}
CLS = {OK: 'ok', WARN: 'warn', BAD: 'bad', REF: 'ref'}


def fnum(v):
    """数字人性化：2770→2,770；0.038 保留"""
    if isinstance(v, float) and v == int(v) and abs(v) < 1e15:
        v = int(v)
    if isinstance(v, int):
        return f'{v:,}'
    return str(v)


class Digest:
    def __init__(self, text):
        self.t = text
        self.n = lambda p, g=1, d=None: (lambda m: float(m.group(g).replace(',', '')) if m else d)(re.search(p, text, re.M))
        self.s = lambda p, g=1, d=None: (lambda m: m.group(g) if m else d)(re.search(p, text))

    def series(self, key):
        """序列统计行 -> dict(min, med, p95, max)"""
        m = re.search(r'^\s*' + re.escape(key) + r'\s+n=\d+\s+min=(\S+)\s+med=(\S+)\s+p95=(\S+)\s+max=(\S+)',
                      self.t, re.M)
        if not m:
            return None
        return dict(zip(('min', 'med', 'p95', 'max'), (float(x) for x in m.groups())))

    def disks(self):
        """盘体介质映射 {dev: 'SSD'|'HDD'}——只认二B 盘体身份表"""
        frag = re.search(r'二B、盘体身份.*?(?=\n三、)', self.t, re.S)
        out = {}
        if frag:
            for m in re.finditer(r'^\s*(\S+)\s.*?\b(\d)=(SSD|HDD)', frag.group(0), re.M):
                out[m.group(1)] = 'SSD' if m.group(2) == '0' else 'HDD'
        return out


# ---------- 判定规则（阈值集中在此，样例口径，待用户校） ----------
RULES = {
    'busy_max':    (80, 60),      # OS busy% max ≥80 异常 / ≥60 警告
    'iowait_max':  (20, 10),
    'memavail_min': (10, 20),     # 可用% min ≤10 异常 / ≤20 警告（越低越差）
    'swpout_max':  (10000, 1000),
    'aas_per_core': (1.0, 0.5),   # 每核并发 >1 异常 / 0.5~1 警告
    'phys_logical': (5.0, 2.0),   # 物理/逻辑读 % >
    'await':       {'SSD': (5, 5), 'HDD': (20, 20)},  # 2026-10-27 用户令: SSD>5ms/HDD>20ms 即异常; util 不判读
    'lock_avg_ms': (1000, 100),   # 锁类事件 avg ≥1000ms 异常 / ≥100ms 警告
    'err_total':   (1000, 100),   # ERROR+FATAL 每小时
    'err_class':   (100, 10),     # 单类错误条数
    'lag_mb':      (1024, 100),   # total lag MB
    # ASP 桶（对齐 extract.py detect_asp_windows 阈值：超阈值=警告，≥2×=异常）
    'trans_sync_bucket': (10, 5), 'lock_bucket': (6, 3), 'lwlock_bucket': (6, 3),
    'io_bucket': (6, 3), 'wal_sync_bucket': (4, 2),
    # v2.5.20 新节闸（2026-11-01 对齐；口径出处见 check-inventory.md 表D）
    'sirq_core': (80, 50),        # 每核 %sirq/%irq 饱和 ≥80 异常 / ≥50 警告
    'numa_localratio': (90, 95),  # 最低节点 Local Ratio ≤90 异常 / ≤95 警告（lvl_lo）
    'retrans_pct': (5, 3),        # TCP Retrans% max（报告渲染阈值同款）
    # TCP 每分钟状态峰值（样例口径，待用户校；v2.5.17 小写表/v2.5.18+ 折表同判）
    'tcp_tw': (5000, 1000),       # TIME-WAIT 峰值=短连接风暴
    'tcp_cw': (500, 100),         # CLOSE-WAIT 峰值=应用不关连接
    'tcp_sr': (100, 30),          # SYN-RECV 峰值=半连接堆积（配 ListenDrops）
    # 分母/佐证守卫（2026-11-04 用户令：非问题项在闸层抑制，报告正文不立"非问题卡"）
    'r_busy_core': 70,            # r>核数但 busy max <70% = 瞬时尖峰无饱和佐证，不判
    'plantime_dbt_min': 10,       # Plan Time 占比：DB Time 绝对量 <10s
    'plantime_dbt_frac': 0.01,    #   或 DB Time/Elapsed <1% → 占比不判（小分母失真）
    'dead_min_abs': 200,          # 死元组绝对量 <200（autovacuum 触发量级）→ 比率不判
    'dead_min_denom': 1000,       #   活+死 <1000 比率无意义；live=0 = 计数失真分母塌缩
    'logic_rd_min': 10000,        # 窗口逻辑读累计 <1 万块（≈78MB）→ 读占比/命中率类不判
}


def lvl_hi(v, bad, warn):
    """数值越高越差"""
    return BAD if v >= bad else WARN if v >= warn else OK


def lvl_lo(v, bad, warn):
    """数值越低越差"""
    return BAD if v <= bad else WARN if v <= warn else OK


def build(d):
    R = RULES
    rows = []  # (组, 项, 数值, 状态, 依据)

    # ① 环境基准
    cores = d.n(r'逻辑核数 N\s*=\s*([\d.]+)')
    memtot = d.n(r'MemTotal = ([\d.]+)')
    disks = d.disks()
    media = ' + '.join(sorted(set(disks.values()))) or '未识别'
    if cores:
        rows.append(('环境', '逻辑核数 / 内存 / 盘体', f"{fnum(cores)} 核 / {(memtot or 0)/1024:.0f} GB / {media}",
                     REF, '报告元数据 + 盘体身份表（决定 IO 阈值档）'))

    # ② OS 资源（OS Statistics 章）
    busy = d.series('cpu.busy')
    if busy:
        rows.append(('OS 资源', 'CPU busy（med / max）', f"{busy['med']:.1f}% / {busy['max']:.1f}%",
                     lvl_hi(busy['max'], *R['busy_max']), 'OS 1min 序列统计；≥80% 异常 / ≥60% 警告'))
    s = d.series('cpu.iowait')
    if s:
        rows.append(('OS 资源', 'iowait（max）', f"{s['max']:.1f}%",
                     lvl_hi(s['max'], *R['iowait_max']), 'OS 1min 序列统计'))
    s = d.series('cpu.r')
    if s and cores:
        st, note = lvl_hi(s['max'] / cores, 2.0, 1.0), 'cpu.r max ÷ 逻辑核；>1 即排队'
        if st != OK and busy and busy['max'] < R['r_busy_core']:
            # 2026-11-04 用户令：瞬时尖峰无饱和佐证（busy 不同步高）不立异常，正文不立卡
            st = OK
            note = (f"r 尖峰但 busy max {busy['max']:.1f}%<{R['r_busy_core']}%：瞬时采样无饱和佐证，"
                    "不判（非问题不立卡）")
        rows.append(('OS 资源', '运行队列（max / 核数）', f"{fnum(s['max'])} / {fnum(cores)}", st, note))
    s = d.series('mem.avail%')
    if s:
        rows.append(('OS 资源', '内存可用（min）', f"{s['min']:.1f}%",
                     lvl_lo(s['min'], *R['memavail_min']), 'OS 1min 序列统计'))
    s = d.series('mem.swpout')
    if s:
        rows.append(('OS 资源', 'swap 换出（med / max 页每分钟）', f"{fnum(s['med'])} / {fnum(s['max'])}",
                     lvl_hi(s['max'], *R['swpout_max']), 'OS 1min 序列统计'))
    # 数据盘 await（取每盘 max，按介质阈值）
    worst = None
    for dev, md in disks.items():
        if md not in ('SSD', 'HDD'):
            continue
        for rw in ('rawait', 'wawait'):
            s2 = d.series('disk.%s[%s]' % (rw, dev))
            if s2 and (worst is None or s2['max'] > worst[0]):
                worst = (s2['max'], dev, rw, md)
    if worst:
        v, dev, rw, md = worst
        b, w = R['await'][md]
        rw_cn = '读' if rw == 'rawait' else '写'
        rows.append(('OS 资源', f'数据盘 {rw_cn}延迟 max（{dev}，{md} 阈 {b}ms）', f"{v:.1f} ms",
                     lvl_hi(v, b, w), f'OS 序列 disk.{rw}；{md} 档阈值'))

    # ③ 负载与吞吐（Load Profile / Cache Hit 章）
    aas_pc = d.n(r'每核并发\(AAS/N\)\s*=\s*([\d.]+)')
    if aas_pc is not None:
        rows.append(('负载与命中', '每核并发（AAS/N）', f"{aas_pc:.3f}",
                     lvl_hi(aas_pc, *R['aas_per_core']), 'L1 整体负载；>1 并发超核（须与 ASP avg_sess 交叉校验，差>2×=口径冲突）'))
    dbc = d.n(r'DB CPU/DB Time\s*=\s*([\d.]+)')
    aas = d.n(r'\bAAS\s*=\s*([\d.]+)')
    if dbc is not None:
        rows.append(('负载与命中', 'DB CPU / DB Time', f"{dbc:.1f}%（AAS {fnum(aas or 0)}）",
                     REF, '画像项：计算型/等待型归属，非对错'))
    pl = d.n(r'物理读/逻辑读 = ([\d.]+)')
    # 比率类分母地板（2026-11-04 用户令同类审计）：窗口逻辑读累计太小 → 占比/命中率失真
    lrd = d.n(r'^logical reads\s+\S+\s+\S+\s+([\d,]+)')
    lrd = float(str(lrd).replace(',', '')) if lrd is not None else None
    ratio_ok = lrd is None or lrd >= R['logic_rd_min']  # 无口径时不拦（老报告）
    if pl is not None:
        st, note = lvl_hi(pl, *R['phys_logical']), 'Load Profile 派生'
        if st != OK and not ratio_ok:
            st = OK
            note = f"分母不足不判：窗口逻辑读 {fnum(lrd)} 块 <{R['logic_rd_min']}（小分母，非问题不立卡）"
        rows.append(('负载与命中', '物理读 / 逻辑读', f"{pl:.2f}%", st, note))
    # Cache Hit 原值（取窗口命中率最低的库）
    hits = re.findall(r'^(\S+)\s+[\d.]+%\s+[\d.]+%\s+([\d.]+)%', d.t, re.M)
    if hits:
        worst_db, worst = min(hits, key=lambda x: float(x[1]))
        v = float(worst)
        st, note = lvl_lo(v, 90, 95), 'Cache Hit Ratio；<95% 警告 / <90% 异常'
        if st != OK and not ratio_ok:
            st = OK
            note = (f"分母不足不判：窗口逻辑读 {fnum(lrd)} 块 <{R['logic_rd_min']}"
                    "（读量太小命中率无意义，非问题不立卡）")
        rows.append(('负载与命中', '缓冲命中率（最低库·窗口值）', f"{v:.2f}%（{worst_db}）", st, note))
    pt = d.n(r'^PLAN_TIME\s+([\d.]+)\s+([\d.]+)%', 2)
    if pt is not None:
        st, note = lvl_hi(pt, 10, 5), 'Time Model System Stats；计划开销占比'
        dbt, el = d.n(r'DB Time\s*=\s*([\d.]+)'), d.n(r'Elapsed\s*=\s*([\d.]+)')
        if st != OK and dbt is not None and (dbt < R['plantime_dbt_min']
                                             or (el and dbt / el < R['plantime_dbt_frac'])):
            # 2026-11-04 用户令：分母太小占比失真，不立警告，正文不立卡
            st = OK
            frac = f"（DB Time {dbt:g}s，占 Elapsed {dbt / el * 100:.1f}%）" if el else f"（DB Time {dbt:g}s）"
            note = f"分母不足不判{frac}：小分母占比失真，非问题不立卡"
        rows.append(('负载与命中', 'Plan Time 占 DB Time', f"{pt:.2f}%", st, note))
    dl = d.n(r'^deadlocks\s+\S+\s+\S+\s+(\S+)', 1)
    if dl is not None:
        v = dl if isinstance(dl, (int, float)) else float(str(dl).replace(',', '') or 0)
        rows.append(('负载与命中', '死锁（区间增量）', fnum(v),
                     BAD if v > 0 else OK, 'Load Profile deadlocks Δ'))

    # ④ 等待事件（Top Wait Events 章）
    for m in re.finditer(r'^(\S+)\s+(LOCK_EVENT|LWLOCK_EVENT)\s+([\d,]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)%',
                         d.t, re.M):
        ev, cls, waits, tot, avg, pct = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), m.group(6)
        avg = float(avg)
        # 必要条件闸门（2026-10-27 用户令）：占 DB Time <10% 的等待事件不定异常，
        # 不论 avg 多高（avg 阶梯只对过闸事件生效）
        st = REF if float(pct) < 10 else lvl_hi(avg, *R['lock_avg_ms'])
        rows.append(('等待事件', f'{ev}（{cls.split("_")[0].lower()}，avg / waits）',
                     f"{avg:.1f} ms / {waits}",
                     st, f'Top Event 谱，占 DB Time {pct}%；占比<10% 不立异常'))
    m = re.search(r'^(BufHashTableSearch)\s+IO_EVENT\s+([\d,]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)%', d.t, re.M)
    if m:
        rows.append(('等待事件', 'BufHashTableSearch（%DB Time / avg）', f"{m.group(5)}% / {m.group(4)} ms",
                     OK, '计数型事件：avg≈0，与块访问速率对账，不当瓶颈'))

    # ⑤ 活跃会话与后台（ASP / Pagewriter / Callstack / 长事务）
    buckets = {'wait_lock': 'lock_bucket', 'wait_lwlock': 'lwlock_bucket', 'wait_io': 'io_bucket',
               'wait_wal_sync': 'wal_sync_bucket', 'wait_trans_sync': 'trans_sync_bucket'}
    bucket_max = {k: 0 for k in buckets}
    for m in re.finditer(r'^\[local_active_session\].*?\s(?:\d+\s+){3}(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s*$',
                         d.t, re.M):
        for i, name in enumerate(buckets):
            bucket_max[name] = max(bucket_max[name], int(m.group(i + 1)))
    for name, rule in buckets.items():
        v = bucket_max[name]
        b, w = R[rule]
        rows.append(('活跃会话与后台', f'ASP 5min 桶峰值 · {name.replace("wait_", "wait ")}', fnum(v),
                     lvl_hi(v, b, w) if v else OK, 'Active Session History 桶计数'))
    av = re.search(r'采样 \d+ 次，worker_cnt: min=0 max=0 非零采样=0 次', d.t)
    if av:
        rows.append(('活跃会话与后台', 'autovacuum worker（非零采样）', '0',
                     REF, 'Callstack 采样；本窗无 AV 活动'))
    # Pagewriter（按最新版 vb_tool 规格；老版无此章=不写行）
    pw_frag = re.search(r'### Pagewriter 5min 序列\n(.*?)(?=\n###|\n=)', d.t, re.S)
    if pw_frag and '无 pagewriter 采样' not in pw_frag.group(1):
        ft = pw_frag.group(1)
        md_ = re.search(r'remain_dirty 峰值 ([\d,.]+)', ft)
        mb_ = re.search(r'behind xlog 峰值 ([\d,.]+)', ft)
        if md_:
            bv = float(mb_.group(1).replace(',', '')) if mb_ else 0.0
            rows.append(('活跃会话与后台', 'Pagewriter 脏页峰值 / behind xlog',
                         f"{md_.group(1)} 页 / {bv:.1f} MB",
                         lvl_hi(bv, 200, 50), 'Pagewriter 5min 序列；behind>0=刷脏落后于写入'))
    # 长事务
    m = re.search(r'ltx60_cnt 峰值 (\d+)', d.t)
    if m:
        v = int(m.group(1))
        rows.append(('活跃会话与后台', '长事务 ltx60 峰值（>60s 个数）', fnum(v),
                     BAD if v >= 50 else WARN if v >= 10 else OK, 'Long Transaction Monitor 5min 采样'))

    # ⑥ SQL（SQL Statistics 各维度 Top1 + logrpt 慢 SQL）
    def sql_top1(dim, has_total=True):
        """解析 '## SQL ordered by <dim>' 的 Top1 数据行 -> (sqlid, user, execs, total, pct)"""
        core = r'\s*1\s+(\S+)\s+(\S+)\s+([\d,]+)\s+' + (r'([\d.]+)\s+([\d.]+)%' if has_total else r'([\d.]+)%')
        m = re.search(r'## SQL ordered by ' + dim + r'\n#[^\n]*\n-{3,}[^\n]*\n' + core, d.t)
        if not m:
            return None
        g = m.groups()
        return g[0], g[1], int(g[2].replace(',', '')), *( (float(g[3]), float(g[4])) if has_total else (None, float(g[3])) )

    top_specs = [  # 维度, has_total
        ('DB Time', True), ('CPU Time', True), ('IO Time', True),
        ('Plan Time', True), ('Executions', False),
        ('Logical Reads', True), ('Physical Reads', True),
    ]
    for dim, has_total in top_specs:
        t1 = sql_top1(dim, has_total)
        if not t1:
            continue
        sqlid, user, execs, total, pct = t1
        if (total or 0) == 0 and has_total:
            continue  # 维度全 0（SQL 级读计数缺口）无判读价值，不写行
        # 判定：占比 ≥40% 异常；≥20% 且绝对量 ≥100s 警告；绝对量 <50s 只算参考
        st = REF if (total or 0) < 50 else (BAD if pct >= 40 else WARN if pct >= 20 else OK)
        val = (f"{fnum(sqlid)}（{user}）{pct}% · {fnum(execs)} 次"
               if not has_total or dim == 'Executions' else
               f"{fnum(sqlid)}（{user}）{pct}% · {fnum(total)}s / {fnum(execs)} 次")
        rows.append(('SQL', f'Top SQL · {dim}', val, st, f'SQL ordered by {dim} 维度 Top1'))
    slow = d.n(r'慢SQL分桶行: (\d+) 条')
    if slow is not None:
        rows.append(('SQL', '慢 SQL 分桶行（logrpt）', fnum(slow),
                     REF, 'analyze_log 慢 SQL 统计'))

    # ⑥B 参数（GUC 标准化对照 + sysctl 覆盖）
    m = re.search(r'基线 [\d.]+：偏离 (\d+) / 一致 (\d+) / 报告未含 (\d+)', d.t)
    if m:
        dev, same, miss = (int(x) for x in m.groups())
        rows.append(('参数', 'GUC 标准化对照（G100 基线初筛）',
                     f'偏离 {dev} / 一致 {same} / 未含 {miss}',
                     WARN if dev else OK, '标准化参数对照；初筛口径：单位与格式差异需人工核'))
    n_sysctl = len(re.findall(r'^  (?:net|vm|fs|kernel)\.\S+\s*=', d.t, re.M))
    if n_sysctl:
        rows.append(('参数', 'sysctl 关键参数（已采集）', f'{n_sysctl} 项', REF,
                     'OS Kernel Parameters；标准基线仅覆盖 GUC，sysctl 无对照标准'))

    # ⑥C Object Statistics（死元组率 + 顺序扫热点）
    frag = re.search(r'## Object Statistics\n(.*?)(?=\n={10,})', d.t, re.S)
    if frag:
        ft = frag.group(1)
        worst_dead, worst_seq, worst_idx = None, None, None  # (ratio,obj,dead,live)/(tuples,obj,scans)/(fetch,obj)
        worst_any = None  # 不设守卫的最高比率（恒显示口径：样本全被守卫拦时也写行）
        for line in ft.split('\n'):
            c = line.split()
            if len(c) == 10 and c[3].replace(',', '').isdigit():  # 死元组表
                live, dead = int(c[7].replace(',', '')), int(c[9].replace(',', ''))
                if live + dead > 0:
                    r_ = dead / (live + dead) * 100
                    if worst_any is None or r_ > worst_any[0]:
                        worst_any = (r_, c[2], dead, live)
                    # 分母守卫（2026-11-04 用户令）：live=0 计数失真 / 死元组绝对量
                    # <200（autovacuum 触发量级）/ 活+死 <1000 → 比率不判，正文不立卡
                    if live > 0 and dead >= R['dead_min_abs'] and live + dead >= R['dead_min_denom']:
                        if worst_dead is None or r_ > worst_dead[0]:
                            worst_dead = (r_, c[2], dead, live)
            elif len(c) == 14 and c[3].replace(',', '').isdigit() \
                    and c[4].replace(',', '').isdigit():  # 访问统计表（c[4] 必须整数，防 statio 行误抓）
                st_ = int(c[4].replace(',', ''))
                if worst_seq is None or st_ > worst_seq[0]:
                    worst_seq = (st_, c[2], int(c[3].replace(',', '')))
                if_ = int(c[6].replace(',', ''))
                if worst_idx is None or if_ > worst_idx[0]:
                    worst_idx = (if_, c[2])
        if worst_dead:
            r_, obj, dead, live = worst_dead
            rows.append(('对象与访问', '死元组率最高表', f'{obj}：{r_:.1f}%（{fnum(dead)}/{fnum(live+dead)}）',
                         lvl_hi(r_, 20, 10), 'Object Statistics；≥10% 提示 vacuum 滞后'))
        elif worst_any:
            r_, obj, dead, live = worst_any
            rows.append(('对象与访问', '死元组率最高表', f'{obj}：{r_:.1f}%（{fnum(dead)}/{fnum(live+dead)}）',
                         OK, '样本全未过分母守卫（活计数 0 或绝对量过小），比率失真不判（非问题不立卡）'))
        if worst_seq and worst_seq[0] > 0:
            tups, obj, scans = worst_seq
            rows.append(('对象与访问', '顺序扫行数最高表（Δ）',
                         f'{obj}：{fnum(tups)} 行 / {fnum(scans)} 次',
                         lvl_hi(tups, 1e10, 1e9), 'Object Statistics seq_tup_read Δ；≥10 亿行提示缺索引'))
        if worst_idx and worst_idx[0] > 0:
            rows.append(('对象与访问', '索引扫行数最高表（Δ）',
                         f'{worst_idx[1]}：{fnum(worst_idx[0])} 行',
                         REF, 'Object Statistics idx_tup_fetch Δ'))
        # 新版 vb_tool 两个 statio 子章节（Top Tables by Logical/Physical Reads）；
        # 老版无此表=不写行
        lines = ft.split('\n')
        for i, line in enumerate(lines):
            c = line.split()
            if len(c) >= 5 and c[3] in ('Logical', 'Physical') and c[4] == 'Reads':
                dim = c[3]
                for j in range(i + 1, min(i + 4, len(lines))):
                    cc = lines[j].split()
                    if len(cc) >= 6 and cc[0] != 'dbname' and not cc[0].startswith('-'):
                        rows.append(('对象与访问', f'Top 表 · {dim} Reads（statio）',
                                     f'{cc[2]}：{fnum(int(float(cc[3].replace(",", "") or 0)))} 块（{cc[4]}）',
                                     REF, f'Object Statistics / Top Tables by {dim} Reads'))
                        break

    # ⑦ 日志（logrpt：错误聚合）
    e, w_, f_ = (d.n(r'级别人数: ERROR=(\d+) WARNING=(\d+) FATAL=(\d+)', g) for g in (1, 2, 3))
    if e is not None:
        tot = (e or 0) + (f_ or 0)
        rows.append(('日志', 'ERROR + FATAL 总量', f"{fnum(tot)}（E {fnum(e)} / F {fnum(f_)}）",
                     lvl_hi(tot, *R['err_total']), 'analyze_log 汇总；按小时窗'))
    # 族定义：正则 -> 族名（同性质错误合族，避免 Top15 逐条刷屏）
    fams = [
        (r'Invalid username/password', '认证失败（login denied）'),
        (r'duplicate key value violates', '唯一键冲突（duplicate key）'),
        (r'permission denied|must be superuser', '权限错误（permission denied / superuser）'),
        (r'Session unused timeout|terminating connection due to administrator', '会话超时被踢（timeout）'),
        (r'value too long', '字段值超长（value too long）'),
    ]
    fam_cnt = {name: 0 for _, name in fams}
    fam_users = {name: set() for _, name in fams}
    rest_cnt, rest_cls = 0, set()
    # 只取首个"错误消息聚合（Top15）"区——异常时段切片区里同一张表会重复出现（双计坑）
    agg = re.search(r'### 错误消息聚合（Top15）\n(.*?)(?=\n###|\n##)', d.t, re.S)
    for m in re.finditer(r'^\s*(ERROR|FATAL|WARNING)\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*([^|]+?)\s*\|',
                         agg.group(1) if agg else d.t, re.M):
        lvl, cnt, msg, raw_users = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        ulist = [u.strip() for u in raw_users.split(',')]
        user = ulist[0] + (f' 等{len(ulist)}用户' if len(ulist) > 1 else '') if ulist else ''
        hit = False
        for pat, name in fams:
            if re.search(pat, msg):
                fam_cnt[name] += cnt
                fam_users[name].add(user)
                hit = True
                break
        if not hit:
            rest_cnt += cnt
            rest_cls.add(msg[:24])
    for _, name in fams:
        c = fam_cnt[name]
        if not c:
            continue
        users = '/'.join(sorted(fam_users[name]))
        rows.append(('日志', name, fnum(c) + (f'（{users}）' if users else ''),
                     lvl_hi(c, *R['err_class']), 'analyze_log 错误消息聚合（同性质合族）'))
    if rest_cnt:
        rows.append(('日志', f'其他低频错误（{len(rest_cls)} 类）', fnum(rest_cnt),
                     OK if rest_cnt < R['err_class'][1] else WARN, 'analyze_log 错误消息聚合（未成族部分）'))

    # ⑧ OS 附属（dmesg / 复制 / RTT）
    if '无内核错误记录' in d.t:
        rows.append(('OS 附属', 'dmesg 内核错误', '0 条', OK, 'OS 附属证据'))
    lag = d.n(r'total lag\s+峰值 ([\d.]+) MB')
    if lag is not None:
        rows.append(('OS 附属', '主备复制延迟（total 峰值）', f"{lag:.2f} MB",
                     lvl_hi(lag, *R['lag_mb']), 'Replication lag per-minute'))
    rtt = d.n(r'RTT p95 \(max of minutes, ms\)\s+([\d.]+)')
    if rtt is not None:
        rows.append(('OS 附属', 'TCP RTT p95', f"{rtt:.1f} ms", REF, 'TCP RTT 汇总'))

    # ⑧-b v2.5.20 新节机械闸（2026-11-01 对齐；digest 第十节新块供数）
    # Cgroup：extract.py 已按口径打旗——旗标行直接命中；无旗且表在 = 正常
    if '### Cgroup' in d.t and '(无 Cgroup 表' not in d.t:
        thr_bad = bool(re.search(r'★ CPU 配额咬合', d.t))
        rows.append(('OS 资源', 'Cgroup CPU 配额（nr_throttled/throttled_time Δ）',
                     'Δ>0 咬合' if thr_bad else 'Δ=0 未咬合',
                     BAD if thr_bad else OK,
                     'digest Cgroup 块；咬合=库慢但 OS busy 不高的第一分流位'))
        oom_bad = bool(re.search(r'★ cgroup OOM 记录', d.t))
        rows.append(('OS 资源', 'cgroup memory.events OOM（绝对值）',
                     'oom/oom_kill >0' if oom_bad else '0',
                     BAD if oom_bad else OK, 'digest Cgroup 块；绝对值口径——Δ 会埋事件'))
    # Softirq/IRQ 每核饱和（机器级 si 均值会掩盖单核打满）
    for label, pat in (('Softirq %sirq 单核饱和峰值', r'Softirq 每核%sirq饱和峰值:\s*([\d.]+)%\s*@\s*CPU\s*(\S+)'),
                       ('IRQ %irq 单核饱和峰值', r'IRQ 每核%irq饱和峰值:\s*([\d.]+)%\s*@\s*CPU\s*(\S+)')):
        m = re.search(pat, d.t)
        if m:
            rows.append(('OS 资源', f'{label}（{m.group(2)}）', f'{float(m.group(1)):.1f}%',
                         lvl_hi(float(m.group(1)), *R['sirq_core']),
                         'digest Softirq/IRQ 块；单核打满=均值看不见的形态'))
    # TCP Retrans%（主机聚合口径，与 RTT p95 并排读）
    v = d.n(r'Retrans%\s+max ([\d.]+)%')
    if v is not None:
        rows.append(('OS 附属', 'TCP Retrans% max', f'{v:.3f}%',
                     lvl_hi(v, *R['retrans_pct']), 'TCP Statistics 每分钟；>3% 加粗 >5% 红'))
    # TCP 每分钟状态峰值（(?i) 兼容 v2.5.17 小写独立表与 v2.5.18+ 折表两种标签）
    for label, key, meaning in (('TIME-WAIT', 'tcp_tw', '短连接风暴'),
                                ('CLOSE-WAIT', 'tcp_cw', '应用不关连接'),
                                ('SYN-RECV', 'tcp_sr', '半连接堆积配 ListenDrops')):
        m = re.search(r'(?i)%s\s+峰值\s+([\d,.]+)' % label, d.t)
        if m:
            tv = float(m.group(1).replace(',', ''))
            rows.append(('OS 附属', f'TCP {label} 峰值（{meaning}）', fnum(tv),
                         lvl_hi(tv, *R[key]),
                         'DB Process 折表/TCP Sockets 分钟峰值；样例阈值待用户校'))
    # NUMA Local Ratio（多节点才判；单节点恒 ~100% 只作参考）
    numa = re.search(r'### NUMA[^\n]*\n(.*?)(?=\n###|\n##)', d.t, re.S)
    if numa:
        nlines = numa.group(1).splitlines()
        hi = next((i for i, l in enumerate(nlines) if 'Local Ratio' in l), None)
        if hi is not None:
            cols = re.split(r'\s{2,}', nlines[hi].strip())
            try:
                li = cols.index('Local Ratio')
            except ValueError:
                li = None
            ratios = []
            if li is not None:
                for l in nlines[hi + 2:]:
                    c = re.split(r'\s{2,}', l.strip())
                    if len(c) > li:
                        m2 = re.match(r'([\d.]+)%$', c[li])
                        if m2:
                            ratios.append((c[0], float(m2.group(1))))
            if ratios:
                node, lo = min(ratios, key=lambda x: x[1])
                st = REF if len(ratios) < 2 else lvl_lo(lo, *R['numa_localratio'])
                rows.append(('OS 资源', f'NUMA Local Ratio 最低（node {node}）', f'{lo:.2f}%',
                             st, 'numastat 合并表；<90% 跨节点访存 1.5~2× 延迟；单节点不判'))
    # 临时落盘分钟形态（DB Process 折表 TempFile fds 峰值；与 SQL Temp Spill 维互证）
    tf = d.s(r'TempFile\s+峰值 ([\d,.]+)')
    if tf is not None:
        tfn = float(tf.replace(',', ''))
        if tfn > 0:
            rows.append(('SQL', 'TempFile fds 峰值（pgsql_tmp 活跃落盘）', fnum(tfn),
                         WARN, 'DB Process 折表；与 Temp Spill 维同窗对账'))
    return rows


# 项名 → 瓶颈形态编号（bottleneck-patterns.md P1~P12）；表行亮 ■/▲ 时
# 必须按编号过形态闸走取证链（SKILL.md 诊断主流程）
FORM_OF = [
    (r'CPU busy|iowait|运行队列|每核并发|DB CPU / DB Time|Top SQL · (DB Time|CPU Time|Executions)', 'P1'),
    (r'Cgroup CPU 配额|单核饱和峰值', 'P2'),
    (r'内存可用|swap 换出|dmesg|memory.events', 'P7'),
    (r'数据盘|物理读 / 逻辑读|缓冲命中率|Top SQL · IO Time|顺序扫|索引扫|Top 表 · |BufHashTableSearch|wait io', 'P3'),
    (r'Plan Time|Top SQL · Plan Time', 'P10'),
    (r'死锁|transactionid|DoubleWriteLock|wait lock|wait lwlock|wait trans_sync', 'P5'),
    (r'wait wal_sync|主备复制延迟', 'P9'),
    (r'autovacuum|长事务|死元组率', 'P8'),
    (r'Pagewriter', 'P4'),
    (r'TCP RTT|Retrans|TIME-WAIT|CLOSE-WAIT|SYN-RECV', 'P11'),
    (r'TempFile', 'P6'),
    (r'ERROR \+ FATAL|认证失败|唯一键|权限错误|超时被踢|值超长|其他低频', 'P12'),
]


def form_of(item):
    for pat, p in FORM_OF:
        if re.search(pat, item):
            return p
    return None


def to_html(rows, title='健康总览'):
    # ★只报问题：异常在前、警告其次；正常/参考项不进表，只计个数
    probs = [r for r in rows if r[3] in (BAD, WARN)]
    probs.sort(key=lambda r: 0 if r[3] == BAD else 1)   # 稳定排序，组内保持原序
    nbad = sum(1 for r in probs if r[3] == BAD)
    nwarn = sum(1 for r in probs if r[3] == WARN)
    nrest = len(rows) - len(probs)
    if not probs:
        return f'''<style>
.ov-sum{{font-size:13px;margin:2px 0 8px;color:#3b3a38}}
</style>
<h2>健康总览</h2>
<p class="ov-sum">本快照区间 {len(rows)} 项关键统计<b>全部正常</b>，无需要处理的项。</p>'''
    groups = []
    for r in probs:
        if not groups or groups[-1][0] != r[0]:
            groups.append((r[0], []))
        groups[-1][1].append(r)
    trs = []
    for gname, grows in groups:
        trs.append(f'<tr class="ov-g"><td colspan="4">{gname}</td></tr>')
        for _, item, val, st, basis in grows:
            p = form_of(item)
            basis = (p + ' · ' if p else '') + basis
            trs.append(
                f'<tr><td class="ov-item">{item}</td><td class="ov-val">{val}</td>'
                f'<td class="ov-st {CLS[st]}">{SYM[st]}</td><td class="ov-basis">{basis}</td></tr>')
    head = f'异常 {nbad} 项 · 警告 {nwarn} 项'
    return f'''<style>
.ov table{{width:100%;border-collapse:collapse;font-size:12.5px;margin:6px 0 14px}}
.ov th{{text-align:left;color:#52514e;font-weight:600;border-bottom:2px solid #d8d6d2;padding:5px 8px}}
.ov td{{border-bottom:1px solid #eceae6;padding:4.5px 8px;vertical-align:top}}
.ov-g td{{background:#f7f6f4;font-weight:600;color:#52514e;border-top:2px solid #d8d6d2}}
.ov-item{{width:34%}}.ov-val{{width:17%;font-variant-numeric:tabular-nums;white-space:nowrap}}
.ov-st{{width:9%;white-space:nowrap;font-weight:600}}
.ov-basis{{color:#7a7875;font-size:11.5px}}
.ov .ok{{color:#2e7d32}}.ov .warn{{color:#b26a00}}.ov .bad{{color:#c62828}}.ov .ref{{color:#8a8886}}
.ov-sum{{font-size:13px;margin:2px 0 8px;color:#3b3a38}}
</style>
<h2>健康总览</h2>
<p class="ov-sum">本快照区间 {len(rows)} 项关键统计：<b>{head}</b>{f'，其余 {nrest} 项正常/参考（不展开）' if nrest else ''}。下表只列问题项，判定口径见"依据"列；正文按严重度逐项给根因与处置。</p>
<div class="ov"><table>
<tr><th>统计项</th><th>数值</th><th>状态</th><th>判定依据 / 出处</th></tr>
{''.join(trs)}
</table></div>'''


def main():
    digest = Path(sys.argv[1]).read_text(encoding='utf-8', errors='replace')
    rows = build(Digest(digest))
    frag = to_html(rows)
    Path('overview_fragment.html').write_text(frag, encoding='utf-8')
    # 文本预览
    cur = None
    for g, item, val, st, _ in rows:
        if g != cur:
            print(f'\n[{g}]')
            cur = g
        print(f'  {SYM[st]:6} {item}  =  {val}')
    npr = sum(1 for r in rows if r[3] in (BAD, WARN))
    print(f'\n共 {len(rows)} 项 | 异常 {sum(1 for r in rows if r[3]==BAD)} / 警告 {sum(1 for r in rows if r[3]==WARN)}'
          f' | 正常/参考 {len(rows)-npr} 项不进报告表')
    if len(sys.argv) >= 5 and sys.argv[2] == '--insert':
        src = Path(sys.argv[3]).read_text(encoding='utf-8', errors='replace')
        out = re.sub(r'(<h1>.*?</h1>)', r'\1\n' + frag.replace('\\', '\\\\'), src, count=1, flags=re.S)
        Path(sys.argv[4]).write_text(out, encoding='utf-8')
        print(f'已插入 -> {sys.argv[4]}')


if __name__ == '__main__':
    main()
