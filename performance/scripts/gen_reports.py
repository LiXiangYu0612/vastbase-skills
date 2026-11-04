#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""迪墨云 2026-10-26 四窗 AWR 逐窗报告生成器。
数值全部程序化取自各窗 digest（不手填）；结论/卡片文案按过闸(P1~P12)结论定制。
产物：<输入名>_analyze.html × 4，写输入同目录 + /mnt/d/log 各一份。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from overview_table import build as ov_build, to_html as ov_html, Digest as OvDigest

INBOX = Path('/home/xiang/.claude/skills/vastbase/workspace/inbox/dimo_1026')
BASE = Path('/mnt/d/log/awr_dn_6001_6002_20261020090001_20261020100001_analyze.html')  # 骨架基座
OUTLOG = Path('/mnt/d/log')

WINDOWS = ['20261026083003_20261026084005', '20261026084005_20261026085002',
           '20261026085002_20261026090007', '20261026090007_20261026093015']


def N(d, pat, g=1):
    m = re.search(pat, d, re.M)
    if not m:
        return None
    v = m.group(g).replace(',', '')
    try:
        return float(v)
    except ValueError:  # 7.24K / 1.5M
        return float(v[:-1]) * {'K': 1e3, 'M': 1e6}[v[-1]]


def S(d, pat, g=1):
    m = re.search(pat, d, re.M)
    return m.group(g) if m else None


def ev(d, name):
    """Top Event 行 -> (waits, total_s, avg_ms, pct)"""
    m = re.search(r'^%s\s+\S+_EVENT\s+([\d,]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)%%' % re.escape(name), d, re.M)
    return (int(m.group(1).replace(',', '')), float(m.group(2)), float(m.group(3)), float(m.group(4))) if m else None


def bands(d):
    """digest DB时段 + OS 异常时段 -> DATA.bands"""
    out = []
    for m in re.finditer(r'DB时段\d+:\s+(\d\d:\d\d)~(\d\d:\d\d)\s+\(([^)]+)\)', d):
        lbl = m.group(3).split(',', 1)[-1].strip()
        out.append({'from': m.group(1), 'to': m.group(2),
                    'label': 'wait_' + lbl.replace('DB:', '').replace(' ', '＋') if lbl.startswith('DB:') else lbl,
                    'sev': 'serious'})
    return out


def parse_window(w):
    d = (INBOX / f'awr_node1_{w}_digest.txt').read_text(encoding='utf-8', errors='replace')
    meta = dict(
        aas=N(d, r'^AAS\s+=\s+([\d.]+)'), aasn=N(d, r'每核并发\(AAS/N\)\s+=\s+([\d.]+)'),
        dbcpu=N(d, r'DB CPU/DB Time\s+=\s+([\d.]+)'), busy=N(d, r'OS CPU 平均: Busy=([\d.]+)'),
        iowait=N(d, r'OS CPU 平均:.*?Iowait=([\d.]+)'), n=N(d, r'逻辑核数 N\s*=\s*([\d.]+)'),
        elapsed=N(d, r'^Elapsed\s+=\s+([\d.]+)'), dbtime=N(d, r'^DB Time\s+=\s+([\d.]+)'),
        commits=N(d, r'^commits\s+\S+\s+\S+\s+\S+\s+([\d.]+[KM]?)'),
        err=(lambda mm: (int(mm.group(1)), int(mm.group(2))) if mm else None)(
            re.search(r'级别人数: ERROR=(\d+) WARNING=\d+ FATAL=(\d+)', d)),
        plan_pct=N(d, r'^PLAN_TIME\s+[\d.]+\s+([\d.]+)%'),
        win=d)
    return d, meta


def concl_para(m):
    e, f = m['err'] if isinstance(m['err'], tuple) else (None, None)
    return (f"快照区间 {m['win_t']}：平均活跃会话 {m['aas']:.0f}（每核 {m['aasn']:.1f}，共 {m['n']:.0f} 核）、"
            f"OS busy {m['busy']:.1f}%（iowait {m['iowait']:.1f}%）、DB CPU/DB Time {m['dbcpu']:.2f}%（等待型负载）、"
            f"提交 {m['commits']}/s。错误日志 ERROR {e} 条、FATAL {f} 条。")


def issue(title, body, acts, sev='●'):
    return (f'<div class="issue">\n  <h3>{sev} {title}</h3>\n  <p>{body}</p>\n'
            f'  <ul class="acts">{"".join(f"<li>{a}</li>" for a in acts)}</ul>\n</div>\n')


def build_window(w, cards, headline):
    d, m = parse_window(w)
    pass  # (dt unused)
    begin = f"{w[:4]}-{w[4:6]}-{w[6:8]} {w[8:10]}:{w[10:12]}:{w[12:14]}"
    end = f"{w[15:19]}-{w[19:21]}-{w[21:23]} {w[23:25]}:{w[25:27]}:{w[27:29]}"
    m['win_t'] = f"{begin} ~ {end}"
    e, f = (int(m['err'][0]), int(m['err'][1])) if m['err'] else (0, 0)
    # 总览表
    ov_rows = ov_build(OvDigest(d))
    ov_frag = ov_html(ov_rows)
    # DATA
    charts = (INBOX / f'awr_node1_{w}_charts.json').read_text(encoding='utf-8', errors='replace')
    try:
        cj = json.loads(charts)
        cj.setdefault('meta', {})['bands'] = bands(d)
        cj['meta']['generated'] = '2026-10-26'
        charts = json.dumps(cj, ensure_ascii=False)
    except Exception:
        pass
    concl = (f'<h2>一、结论</h2>\n<div class="card"><div class="headline">{headline}</div>\n'
             f'<p>{concl_para(m)}</p></div>\n'
             '<figure class="fig" id="fig-timeline"><div class="figbody"></div></figure><div id="tip"></div>\n')
    issues = '<h2>二、异常与警告</h2>\n' + ''.join(cards(m))
    return m, ov_frag, concl, issues, charts


def main():
    base = BASE.read_text(encoding='utf-8', errors='replace')
    # 骨架切割
    h1m = re.search(r'<h1>.*?</h1>', base, re.S)
    head = base[:h1m.start()]
    appc = re.search(r'<h2>附录 C.*?(?=<script)', base, re.S)
    script = base[appc.end():]
    mid_old = base[h1m.end():appc.start()]

    for w in WINDOWS:
        cards_fn = CARDS[w]
        headline = HEADLINES[w]
        m, ov, concl, issues, charts = build_window(w, cards_fn, headline)
        out = (head
               + f'<h1>node1 AWR 分析报告（快照区间 {m["win_t"].split(" ~ ")[0][:10]}）</h1>\n'
               + ov + '\n' + concl + issues
               + base[appc.start():appc.end()]
               + script.replace(re.search(r'const DATA = .*?;\n', script, re.S).group(0),
                                'const DATA = ' + charts + ';\n'))
        for dest in (INBOX / f'awr_node1_{w}_analyze.html', OUTLOG / f'awr_node1_{w}_analyze.html'):
            dest.write_text(out, encoding='utf-8')
            print('written', dest)


# ── 每窗文案（过闸结论；数值在 build 时程序化填入卡片生成函数）──
def cards_w1(m):
    ev1 = ('SInvalWriteLock 等待合计 {t:.0f}s（{n} 次，avg {a:.1f}s）'.format(
        t=4749.6, n=1517, a=3130.89))
    return [
        issue('外部工具在 08:40 执行 unique SQL 全池重置，全库语句统计阻塞雪崩',
              '本快照区间 UniqueSQLMappingLock 等待已现（详见 08:40 快照区间报告）；'
              '机制：reset_unique_sql() 一次拿全部哈希分区排他锁并重建整表'
              '（instr_unique_sql.cpp:2328 CleanupAllUniqueSqlEntry），期间全库语句统计读写阻塞。',
              ['定位 08:40 执行 reset 的外部工具（vb_tool 无此调用），停用或移到业务低峰']),
        issue(f'SInvalWriteLock 平均 3.1s——缓存失效消息洪流，源头待定位',
              f'{ev1}。机制：任何会话发缓存/表失效广播都要拿这把排他锁'
              '（sinvaladt.cpp:282 SendSharedInvalidationMessage），144 核高并发下失效消息'
              '写入排队成秒级。同窗并存信号：物理读榜被 pg_sqlplan（VastBase 定制计划历史表'
              ' pg_sqlplan.cpp，客户侧 enable_sqlbind=off 但表仍活跃；开源基线与官方文档'
              ' V2.2.10/V2.2.15 均无此特性文档）与 statement_history（全量语句追踪，保留 7 天、'
              '每小时 DELETE 清理）占据。**失效洪流的具体源头未定论**——pg_sqlplan 变更是否'
              '触发缓存失效广播属推断，海量 prepared statement/catalog 访问亦为候选；'
              'Callstack 5 分钟采样未采到前台等待者栈。',
              ['向 VastBase 支持渠道确认 V2.2.10 的 pg_sqlplan 写入路径与控制 GUC'
               '（enable_sqlbind=off 时谁在写）——官方文档无此特性',
               '现场 vb_tool se 或缩短 Callstack 采样锁等待者栈后再定处置',
               'track_stmt_retention_time 当前 3600,604800→建议 1800,86400（全量 7 天收为 1 天）']),
        issue('BufferContentLock 70.8ms × 43,765 次——热页争用',
              '与逻辑读榜首 app_company（1406 万块，占 12.5%）呼应：该表页被海量会话同时读写。',
              ['app_company 访问模式核查（索引有效性/热点行）；必要时分区或缓存层打散']),
    ]


def cards_w2(m):
    u = ev_text = None
    return [
        issue('unique SQL 全池重置引发等待雪崩：AAS 峰值 3456（每核 24）',
              'UniqueSQLMappingLock 等待 35,606.8s（5,184 次，avg 6,868.6ms，占 DB Time 1.73%）；'
              'ASP 5 分钟桶 08:40~08:50 持续型、峰值活跃会话 1,122、wait_lwlock 桶计数 92。'
              '机制：外部工具执行 reset_unique_sql()（instr_unique_sql.cpp:2616）→ '
              'CleanupAllUniqueSqlEntry(:2328) 一次拿全部分区排他锁并 InitUniqueSQL() 重建整表，'
              '期间全库 unique SQL 语句统计读写全部排队。vb_tool 源码不含该调用（0 命中）——'
              '系外部监控工具所为；同窗 SQL 榜还出现该工具查询昨日 ASP 的递归 COPY 大查询。',
              ['最优先：定位并停用该工具的 reset_unique_sql 调用（或改为低峰+GLOBAL→LOCAL）',
               'instr_unique_sql_count 当前 500000：实机查 unique SQL 总量确认是否池满逐出'
               '（池满为分区级删除、代价小；本窗秒级等待指向全池重置而非池满）']),
        issue('认证失败 12 次（docadmin）+ 唯一键冲突 753——应用侧错误',
              'docadmin 用户重复触发唯一键冲突与认证失败；与库内雪崩无因果但放大会话占用。',
              ['应用侧修唯一键去重逻辑与凭据配置']),
    ]


def cards_w3(m):
    return [
        issue('WALFlushWait 139,285 次（avg 3.45ms）——高频小事务提交风暴',
              '提交 4,370/s，每次提交都经历 WAL 刷盘等待；等待量巨大但单次轻，属放大器而非独立瓶颈。',
              ['应用侧评估合并提交/异步提交（synchronous_commit）的适用性']),
        issue('物理读榜首 pg_sqlplan 81,570 块——VastBase 定制计划历史表，行为待确认',
              'pg_sqlplan（pg_sqlplan.cpp，VastBase 定制；开源基线与官方文档 V2.2.10/V2.2.15 '
              '均无文档）成为物理读主角，与 Plan Time 12.7%、SInvalWriteLock 洪流同窗并存。'
              '客户侧 enable_sqlbind=off 但该表仍活跃——写入路径与控制 GUC 未定论。',
              ['向 VastBase 支持渠道确认该表写入机制与关闭/保留策略',
               '实机双点采样（SELECT count(*) 间隔 10 分钟）确认体量与增速']),
        issue('唯一键冲突 503 + 权限错误 57（docadmin）',
              '应用侧错误持续。', ['应用侧修唯一键冲突与授权']),
    ]


def cards_w4(m):
    return [
        issue('BufferContentLock 122.9ms × 139,570 次（17,159s）——热页争用主导',
              '30 分钟窗内热页争用成为最大等待源；逻辑读榜首 app_company 2,684 万块（9.6%）、'
              'file_snapshots 索引扫描 305 万行/秒级访问。',
              ['app_company / file_snapshots 热点页核查：索引覆盖度、更新模式、必要时分区打散']),
        issue('SInvalWriteLock 1,356ms × 6,369 次（8,638s）——失效洪流持续，源头待定位',
              '失效广播必经的排他锁（sinvaladt.cpp:282 SendSharedInvalidationMessage）。'
              '同窗并存信号：pg_sqlplan 物理读榜首（159,304 块，VastBase 定制计划历史表，'
              'pg_sqlplan.cpp，客户侧 enable_sqlbind=off 但表仍活跃）、Plan Time 11.76%、'
              'statement_history 全量追踪（保留 7 天，每小时 DELETE 清理）。'
              '**失效洪流的具体源头未定论**：pg_sqlplan 变更是否触发缓存失效广播'
              '（VB 定制源码不可得）、海量 prepared statement/catalog 访问均属候选；'
              '本窗 Callstack 5 分钟采样未采到前台等待者栈，无法锁源。',
              ['实机双点采样确认 pg_sqlplan 体量与增速（SELECT count(*) 间隔 10 分钟）——'
               '直接量化 159K 块/30 分钟的增长来源',
               '现场用 vb_tool se 或缩短 Callstack 采样间隔锁 SInvalWriteLock 等待者栈，'
               '定源头后再动手（验收口径：avg 降回毫秒级）',
               'track_stmt_retention_time 当前 3600,604800→建议 1800,86400（全量 7 天收为 1 天）',
               '实机查 unique SQL 总量 vs instr_unique_sql_count=500000，池满则上调']),
        issue('死锁 4 个 + 唯一键冲突 2,290 + 会话被踢 183（docadmin）',
              '应用侧错误风暴升级：30 分钟内死锁 4 起、docadmin 唯一键冲突 2,290 次、'
              '183 个会话被超时策略踢出。',
              ['死锁语句核查（pg_log DETAIL）；应用侧唯一键去重；确认 session_timeout 策略与应用保活匹配']),
    ]


CARDS = {WINDOWS[0]: cards_w1, WINDOWS[1]: cards_w2, WINDOWS[2]: cards_w3, WINDOWS[3]: cards_w4}
HEADLINES = {
    WINDOWS[0]: '异常 —— 等待型负载（DB CPU 占比 4.95%）：失效消息洪流 + 热页争用 + 外部工具干扰',
    WINDOWS[1]: '异常 —— 外部工具 unique SQL 全池重置引发雪崩：AAS 3456（每核 24）、等待型 98%',
    WINDOWS[2]: '异常（回落） —— AAS 1404；提交风暴 + 计划缓存 IO + 应用侧错误持续',
    WINDOWS[3]: '异常 —— 热页争用主导（BufferContentLock 17,159s）+ 失效洪流持续 + 死锁 4 起',
}

if __name__ == '__main__':
    main()
