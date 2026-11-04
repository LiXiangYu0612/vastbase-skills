#!/usr/bin/env python3
"""vastbase performance 预提取器：awrrpt html (+ analyze_log_v2 html) → digest.txt + charts.json + params.txt

只做结构化提取、时间轴/异常时段计算与证据切片，不做诊断——
诊断由 Claude 依据技能域知识库（vastbase/performance/）完成。

用法: extract.py <awrrpt.html> [analyze_log.html ...]
输出(同目录):
  <输入名>_digest.txt    摘要（所有数值引用的唯一来源）
  <输入名>_charts.json   趋势图数据（per-minute + 5min 桶，直接内嵌报告模板 DATA）
  <输入名>_params.txt    全量 GUC + sysctl（查证用）

v4 变更（2026-10-24 用户四条批评驱动）:
  - 盘体身份(Rot/Type/Sched/QDepth)解析 + HDD/SSD 介质自适应 IO 阈值
  - Pagewriter / Long Transaction 5min 序列解析
  - Database Parameters(全量 pg_settings) + OS Kernel Parameters(sysctl) 提取
  - dmesg 分类 / Filesystem Usage / TCP RTT / Replication lag 提取
  - charts.json: OS 1min 序列与 5min 桶(ASP/pw/lt)全部落盘供画趋势图
  - 异常时段多源对齐表: OS×ASP×日志P/W×后台堆栈×pw/lt
  - VB 口径警告头: CPU_TIME 含后台线程、事件不分前台后台、%DBtime 可>100%
"""
import html as H
import json
import os
import re
import statistics
import sys
from collections import Counter, OrderedDict, defaultdict
from datetime import datetime, timedelta

# ---------------------------------------------------------------- 基础工具

def clean(text):
    t = re.sub(r"<[^>]+>", "", text)
    t = H.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def num(v):
    """'1,187.3' / '34.3%' / '1.39 MB' / '2.75K' / '-' → float 或 None"""
    if v is None:
        return None
    v = str(v).strip().replace("%", "")
    if v in ("", "-", "—", "N/A", "n/a"):
        return None
    m = re.match(r"^(-?[\d,]+(?:\.\d+)?)\s*([KMBGT]?)", v)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"": 1, "B": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2)]
    return n * mult


def num_ci(v):
    """大小写不敏感字节版: '12.3 kB'/'1.2 MB'/'345 B' → 字节数 float"""
    if v is None:
        return None
    v = str(v).strip()
    m = re.match(r"^([\d,.]+)\s*([kKmMgGtT]?)[bB]?$", v)
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    mult = {"": 1, "k": 1e3, "K": 1e3, "m": 1e6, "M": 1e6,
            "g": 1e9, "G": 1e9, "t": 1e12, "T": 1e12}[m.group(2)]
    return n * mult


def parse_num(v):
    """'28,782,610' / '46.39K(362.43 MB)' / '1.42 GB' → (数值, 字节/展示串)"""
    if v is None:
        return None, ""
    v = v.strip()
    m = re.match(r"^([\d,.]+)\s*([KMBGT]?)", v)
    if not m:
        return None, v
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None, v
    mult = {"": 1, "B": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}[m.group(2)]
    byte = ""
    bm = re.search(r"\(([\d.]+\s*[KMGTP]?B)\)", v)
    if bm:
        byte = bm.group(1)
    return n * mult, byte


def parse_dt(s):
    """解析报告里的各种时间串，返回 datetime 或 None"""
    if not s:
        return None
    s = s.strip()
    s = re.sub(r"^\[[^\]]*\]\s*", "", s)          # [local_active_session]
    s = re.sub(r"-\d{2}:\d{2}(:\d{2})?$", "", s)  # 去掉 "09:00-09:05" 的区间尾
    for fmt, ln in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d %H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:ln], fmt)
        except ValueError:
            continue
    return None


def iter_blocks(fragment):
    """按文档顺序产出 ('sub', 名称) / ('table', header, rows)"""
    items = []
    for m in re.finditer(r'<div[^>]*class="sub"[^>]*>(.*?)</div>', fragment, re.S | re.I):
        items.append((m.start(), "sub", clean(m.group(1))))
    for m in re.finditer(r"<h[34][^>]*>(.*?)</h[34]>", fragment, re.S | re.I):
        items.append((m.start(), "sub", clean(m.group(1))))
    for m in re.finditer(r"<table\b[^>]*>(.*?)</table>", fragment, re.S | re.I):
        rows = []
        for rm in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
            cells = [clean(c) for c in
                     re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", rm.group(1), re.S | re.I)]
            if cells and any(cells):
                rows.append(cells)
        if not rows:
            continue
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        items.append((m.start(), "table", (rows[0], rows[1:])))
    items.sort(key=lambda x: x[0])
    for _, kind, payload in items:
        yield kind, payload


def hm(t):
    """时间戳安全渲染：None（首列时间解析失败的老/异形报告）→ '?' 不崩"""
    return t.strftime("%H:%M") if t else "?"


def iter_tables(fragment):
    for kind, payload in iter_blocks(fragment):
        if kind == "table":
            yield payload


def fmt_row(cells, widths):
    return "  ".join(str(c)[:w].ljust(w) for c, w in zip(cells, widths))


def render_table(hdr, rows, max_rows=20, max_cell=40):
    rows = rows[:max_rows]
    width = len(hdr)
    colw = []
    for i in range(width):
        vals = [hdr[i]] + [r[i] if i < len(r) else "" for r in rows]
        colw.append(max(3, min(max_cell, max(len(v) for v in vals))))
    out = [fmt_row(hdr, colw)]
    out.append("  ".join("-" * w for w in colw))
    for r in rows:
        out.append(fmt_row(r, colw))
    return "\n".join(out)


# ---------------------------------------------------------------- 分节

def split_sections(html):
    """三格式分节：
       new:   <div class="sec" id=...><h2>Title</h2>
       old:   <div class="sec" id=...>Title</div>
       fold:  <details class="secfold" id=...><summary>Title ...
    """
    marks = []
    for m in re.finditer(r'<div[^>]*id="([a-z0-9-]+)"[^>]*>\s*<h([23])[^>]*>(.*?)</h\2>',
                         html, re.S | re.I):
        title = clean(m.group(3)).replace("↑ Top", "").strip()
        if title:
            marks.append((m.start(), m.end(), title))
    for m in re.finditer(r'<details[^>]*id="([a-z0-9-]+)"[^>]*>\s*<summary[^>]*>(.*?)</summary>',
                         html, re.S | re.I):
        title = clean(m.group(2)).replace("↑ Top", "").strip()
        if title:
            marks.append((m.start(), m.end(), title))
    for m in re.finditer(r'<div[^>]*class="sec"[^>]*id="[a-z0-9-]+"[^>]*>(.*?)</div>',
                         html, re.S | re.I):
        title = clean(m.group(1)).strip()
        if title:
            marks.append((m.start(), m.end(), title))
    marks.sort(key=lambda x: x[0])
    # 去重（同一位置的新旧格式标记）
    dedup = []
    for mk in marks:
        if dedup and abs(mk[0] - dedup[-1][0]) < 2:
            continue
        dedup.append(mk)
    secs = OrderedDict()
    for i, (s, e, title) in enumerate(dedup):
        end = dedup[i + 1][0] if i + 1 < len(dedup) else len(html)
        secs.setdefault(title, html[e:end])
    return secs


def get_sec(secs, *names):
    for want in names:
        if want in secs:
            return secs[want]
    for title, frag in secs.items():
        for want in names:
            if want.lower() in title.lower():
                return frag
    return ""


# ---------------------------------------------------------------- 元数据

def parse_meta(html, out):
    """解析报告头：多个小表（实例/主机/快照），标签单元格以冒号结尾"""
    head = html[:html.find('<div class="toc"')] if '<div class="toc"' in html else html[:60000]
    meta = {}
    rows_all = []
    for tm in re.finditer(r"<table\b[^>]*>(.*?)</table>", head, re.S | re.I):
        rows = []
        for rm in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", tm.group(1), re.S | re.I):
            cells = [clean(c) for c in
                     re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", rm.group(1), re.S | re.I)]
            if cells and any(cells):
                rows.append(cells)
        if rows:
            rows_all.append(rows)
            out.append("\n".join(" | ".join(r) for r in rows[:6]))

    def find_after(label):
        """在行里找 label 后面的第一个非空单元格"""
        for rows in rows_all:
            for ri, r in enumerate(rows):
                for ci, c in enumerate(r):
                    if c.strip().lower().startswith(label.lower()):
                        for v in r[ci + 1:]:
                            if v.strip():
                                return v.strip()
                        # 标签在行尾 → 取下一行同列
                        if ri + 1 < len(rows) and ci < len(rows[ri + 1]):
                            return rows[ri + 1][ci].strip()
        return None

    # 实例/主机表：表头行（Instance Name/DB Role/DB Compatibility）→ 值行
    for rows in rows_all:
        if rows and any("Instance Name" in c for c in rows[0]):
            hdr, val = rows[0], rows[1] if len(rows) > 1 else []
            for h, v in zip(hdr, val):
                if h.strip():
                    meta[h.strip()] = v.strip()
        if rows and any("Host Name" in c for c in rows[0]):
            hdr, val = rows[0], rows[1] if len(rows) > 1 else []
            for h, v in zip(hdr, val):
                if h.strip():
                    meta[h.strip()] = v.strip()
    # 快照表
    for label in ("Begin Snap", "End Snap", "Elapsed", "DB Time", "AAS"):
        v = find_after(label)
        if v:
            meta[label] = v
    # 版本串：头表里含 Vastbase/G100 的单元格
    for rows in rows_all:
        for r in rows:
            for c in r:
                if ("Vastbase" in c or "G100" in c or "openGauss" in c) and len(c) < 300:
                    meta.setdefault("DB Version", c[:160])
                    break
    for k in ("Instance Name", "DB Role", "DB Compatibility", "Host Name", "OS Version",
              "Port", "Begin Snap", "End Snap", "Elapsed", "DB Time", "AAS", "DB Version"):
        if k in meta:
            out.append(f"{k:<18} {meta[k]}")
    # 归一
    meta["_elapsed"] = num(meta.get("Elapsed"))
    meta["_dbtime"] = num(meta.get("DB Time"))
    meta["_aas"] = num(meta.get("AAS"))
    m = re.match(r"\s*(\d+)", meta.get("Begin Snap", ""))
    meta["_begin_snap"] = m.group(1) if m else ""
    return meta


# ---------------------------------------------------------------- 盘体身份（介质自适应阈值）

def parse_disk_meta(os_frag):
    """Disk I/O per device 窗口表的身份列 → {dev: {size,type,rot,sched,qdepth,wc}}
    Rot: 0=SSD(非旋转) 1=HDD(旋转)。旧报告无身份列 → 空 dict。"""
    meta = {}
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if not low or low[0] != "device":
            continue
        if "rot" not in low:
            continue
        i = {name: low.index(name) for name in
             ("device", "size", "type", "rot", "sched", "qdepth", "wc") if name in low}
        for r in rows:
            dev = r[i["device"]] if i["device"] < len(r) else ""
            if not dev:
                continue
            meta[dev] = {
                "size": r[i["size"]] if "size" in i and i["size"] < len(r) else "",
                "type": r[i["type"]] if "type" in i and i["type"] < len(r) else "",
                "rot": r[i["rot"]] if "rot" in i and i["rot"] < len(r) else "?",
                "sched": r[i["sched"]] if "sched" in i and i["sched"] < len(r) else "",
                "qdepth": r[i["qdepth"]] if "qdepth" in i and i["qdepth"] < len(r) else "",
                "wc": r[i["wc"]] if "wc" in i and i["wc"] < len(r) else "",
            }
        break
    return meta


def media_of(dev, disk_meta):
    d = disk_meta.get(dev) or {}
    rot = str(d.get("rot", "?")).strip()
    if rot == "0":
        return "SSD"
    if rot == "1":
        return "HDD"
    return "?"


# 介质自适应 IO 硬阈值：r/w_await 毫秒
MEDIA_IO_THR = {
    "SSD": {"rawait": 10, "wawait": 10, "rel_floor": 3},
    "HDD": {"rawait": 40, "wawait": 40, "rel_floor": 15},
    "?":   {"rawait": 20, "wawait": 20, "rel_floor": 5},
}


# ---------------------------------------------------------------- OS 时间序列

SERIES_SPECS = {
    "cpu": {
        "require": ["Busy", "Idle"],
        "cols": {"Busy": "busy", "Iowait": "iowait", "r": "r", "b": "b",
                 "User": "user", "System": "system", "Idle": "idle"},
        "hard": {"busy": 80, "iowait": 10},
        "rel": {"r": (2.0, 5), "b": (2.0, 2)},
    },
    "mem": {
        "require": ["MemAvail (MB)"],
        "cols": {"MemAvail (MB)": "avail", "MemUsed (MB)": "used",
                 "pswpin/s": "swpin", "pswpout/s": "swpout",
                 "pgmajfault/s": "majflt", "pgpgin/s": "pgin"},
        "hard": {"swpout": 0.5, "majflt": 50},
        "rel": {},
    },
    "disk": {
        "require": ["Device", "r_await (ms)"],
        "cols": {"Device": "device", "r/s": "rs", "w/s": "ws",
                 "Read B/s": "rbytes", "Write B/s": "wbytes",
                 "r_await (ms)": "rawait", "w_await (ms)": "wawait",
                 "avgqu-sz": "aqu"},
        "hard": {},
        "rel": {},
        "keyed": "device",
    },
    "net": {
        "require": ["Interface", "RX B/s"],
        "cols": {"Interface": "iface", "RX B/s": "rx", "TX B/s": "tx",
                 "rx/tx drop (Δ)": "drop"},
        "hard": {},
        "rel": {},
        "keyed": "iface",
    },
    "tcp": {
        "require": ["Retrans %", "RTT p95 (ms)"],
        "cols": {"Retrans %": "retrans", "RTT p95 (ms)": "rtt95",
                 "CurrEstab": "estab", "Opens/s": "opens",
                 "ListenOverflows/s": "ovf", "Timeouts/s": "tmo"},
        "hard": {"retrans": 1.0, "rtt95": 200},
        "rel": {"estab": (1.5, 100)},
    },
    # v2.5.17 新增 per-minute 通道（2026-10-28 并入时间轴：异常分钟全通道同分钟横切）
    "conntrack": {
        "require": ["count", "limit"],
        "cols": {"count": "count", "limit": "limit", "fill": "fill"},
        "hard": {"fill": 80},
        "rel": {},
    },
    "tcpstate": {
        "require": ["LISTEN"],
        "cols": {"LISTEN": "listen", "ESTAB": "estab", "SYN-RECV": "synrecv",
                 "TIME-WAIT": "timewait", "CLOSE-WAIT": "closewait",
                 "LAST-ACK": "lastack"},
        "hard": {},
        "rel": {"timewait": (2.0, 100), "closewait": (2.0, 20),
                "synrecv": (2.0, 10)},
    },
}


def series_from_tables(tables, spec):
    """从 (hdr, rows) 列表里抽时间序列；按分钟归并（同分钟多表/多行合并）"""
    result = OrderedDict()
    keyed = spec.get("keyed")
    req = spec.get("require", [])
    for hdr, rows in tables:
        if not hdr or hdr[0] not in ("Time", "Sample Time"):
            continue
        if any(not any(rq.lower() in h.strip().lower() for h in hdr) for rq in req):
            continue
        idx = {}
        # 两遍匹配：先精确命中（防 "b" 前缀误配 "Busy"），再前缀补位，已占用列不重复用
        for want, short in spec["cols"].items():
            for i, h in enumerate(hdr):
                if h.strip().lower() == want.lower() and i not in idx.values():
                    idx[short] = i
                    break
        for want, short in spec["cols"].items():
            if short in idx:
                continue
            for i, h in enumerate(hdr):
                if i in idx.values():
                    continue
                if h.strip().lower().startswith(want.lower()):
                    idx[short] = i
                    break
        if keyed and keyed not in idx:
            continue
        if not idx:
            continue
        for r in rows:
            t = parse_dt(r[0] if r else "")
            if not t:
                continue
            t = t.replace(second=0)          # 统一到分钟，供时段查找
            item = {}
            if keyed:
                item["_key"] = r[idx[keyed]] if idx[keyed] < len(r) else ""
            for short, i in idx.items():
                raw = r[i] if i < len(r) else ""
                if short == "drop":
                    dm = re.match(r"^\s*(\d+)\s*/\s*(\d+)", str(raw))
                    item[short] = (int(dm.group(1)) + int(dm.group(2))) if dm else num(raw)
                else:
                    item[short] = num(raw)
            result.setdefault(t, []).append(item)
    return result


def series_stats(values):
    vs = [v for v in values if v is not None]
    if not vs:
        return None
    vs_sorted = sorted(vs)
    def pct(p):
        k = (len(vs_sorted) - 1) * p
        lo, hi = int(k), min(int(k) + 1, len(vs_sorted) - 1)
        return vs_sorted[lo] + (vs_sorted[hi] - vs_sorted[lo]) * (k - lo)
    return {
        "n": len(vs), "min": min(vs), "med": statistics.median(vs),
        "p95": pct(0.95), "max": max(vs),
    }


def detect_os_windows(series_map, ncores, memtotal, disk_meta, out):
    """双轨判定：硬阈值 + 相对基线；磁盘 await 阈值按介质(Rot)分档。返回 merged windows"""
    flags = defaultdict(list)   # datetime -> [(axis, metric, value, reason)]
    stats_lines = []
    for axis, spec in SERIES_SPECS.items():
        data = series_map.get(axis)
        if not data:
            continue
        # 按 key（设备/网卡）拆分；无 key 的全局序列直接聚合
        by_key = defaultdict(dict)
        for t, items in data.items():
            for it in items:
                by_key[it.get("_key", "-")].setdefault(t, {}).update(it)
        for key, ts in sorted(by_key.items()):
            media = media_of(key, disk_meta) if axis == "disk" else "?"
            for short in spec["cols"].values():
                if short in ("device", "iface"):
                    continue
                vals = [it.get(short) for it in ts.values()]
                st = series_stats(vals)
                if not st:
                    continue
                label = f"{axis}.{short}" + (f"[{key}]" if key != "-" else "")
                # 内存可用率
                if axis == "mem" and short == "avail" and memtotal:
                    vals = [(v / memtotal * 100) if v is not None else None for v in vals]
                    ts = {t: dict(it, avail=(it.get("avail") / memtotal * 100
                                             if it.get("avail") is not None else None))
                          for t, it in ts.items()}
                    st = series_stats(vals)
                    label = "mem.avail%"
                stat = (f"  {label:<22} n={st['n']:<4} min={st['min']:.3g} "
                        f"med={st['med']:.3g} p95={st['p95']:.3g} max={st['max']:.3g}")
                if axis == "disk":
                    stat += f" [{media}]"
                # 峰值时刻
                peak_t = max(((t, it.get(short)) for t, it in ts.items()
                              if it.get(short) is not None),
                             key=lambda x: x[1], default=None)
                if peak_t:
                    stat += f"  max@{peak_t[0].strftime('%H:%M')}"
                stats_lines.append(stat)
                hard = spec["hard"].get(short)
                rel = spec["rel"].get(short)
                if axis == "disk" and short in ("rawait", "wawait"):
                    hard = MEDIA_IO_THR[media][short]
                    rel = (2.0, MEDIA_IO_THR[media]["rel_floor"])
                for t, it in ts.items():
                    v = it.get(short)
                    if v is None:
                        continue
                    # 特殊列
                    if axis == "mem" and short == "avail" and memtotal:
                        if v < 10:
                            flags[t].append((axis, "mem.avail%", v, "硬阈值<10%"))
                        continue
                    if axis == "net" and short == "drop":
                        if v > 100:
                            flags[t].append((axis, f"net.drop[{key}]", v, "硬阈值>100/min"))
                        continue
                    h = hard
                    if short == "r":
                        h = ncores
                    elif short == "b":
                        h = max(2, ncores * 0.05)
                    if h is not None and v > h:
                        flags[t].append((axis, f"{label}", v, f"硬阈值>{h:g}" + (f"[{media}]" if axis == "disk" else "")))
                        continue
                    if rel:
                        factor, floor = rel
                        if st["med"] > 0 and v > st["med"] * factor and v > floor:
                            flags[t].append((axis, label, v,
                                             f"相对基线>{factor:g}×中位({st['med']:.3g})"))
    out.append("### 序列统计（1min 采样；磁盘 await 阈值已按介质分档）")
    out.extend(stats_lines)

    # 合并异常分钟
    times = sorted(flags)
    windows = []
    cur = []
    for t in times:
        if cur and (t - cur[-1]).total_seconds() > 180:   # 间隔>3min 断开
            windows.append(cur); cur = []
        cur.append(t)
    if cur:
        windows.append(cur)
    merged = []
    for w in windows:
        if len(w) == 1:
            merged.append({"start": w[0], "end": w[0], "minutes": 1, "type": "尖刺"})
        else:
            merged.append({"start": w[0], "end": w[-1], "minutes": len(w), "type": "持续型"})
    out.append("")
    out.append(f"### 异常时段（硬阈值+相对基线，共 {len(flags)} 个异常分钟 / {len(merged)} 个时段）")
    for i, w in enumerate(merged, 1):
        axis_cnt = Counter()
        for t in [w["start"] + timedelta(minutes=k)
                  for k in range(int((w["end"] - w["start"]).total_seconds() // 60) + 1)]:
            for a, _, _, _ in flags.get(t, []):
                axis_cnt[a] += 1
        w["dominant"] = ",".join(a for a, _ in axis_cnt.most_common(2)) or "-"
        w["axis_cnt"] = dict(axis_cnt)
        out.append(f"  时段{i}: {w['start']:%H:%M}~{w['end']:%H:%M} "
                   f"({w['minutes']}分钟, {w['type']}, 主导轴={w['dominant']})")
        # 该时段内被触发的指标
        hits = Counter()
        peaks = {}
        for t in [w["start"] + timedelta(minutes=k)
                  for k in range(int((w["end"] - w["start"]).total_seconds() // 60) + 1)]:
            for a, label, v, reason in flags.get(t, []):
                hits[(label, reason)] += 1
                if label not in peaks or v > peaks[label][0]:
                    peaks[label] = (v, t)
        w["trig"] = sorted({lab for (lab, _r) in hits})
        for (label, reason), cnt in hits.most_common(8):
            v, t = peaks[label]
            out.append(f"      {label:<26} {cnt:>2}次  峰值 {v:.3g}@{t:%H:%M}  [{reason}]")
    return merged, flags


def window_minute_detail(merged, series_map, ncores, out, max_windows=5):
    """异常时段前后各2分钟的逐分钟明细，供报告引用"""
    for i, w in enumerate(merged[:max_windows], 1):
        lo = w["start"] - timedelta(minutes=2)
        hi = w["end"] + timedelta(minutes=2)
        out.append(f"  时段{i} 明细 {lo:%H:%M}~{hi:%H:%M}:")
        cpu = series_map.get("cpu", {})
        mem = series_map.get("mem", {})
        disk = series_map.get("disk", {})
        tcp = series_map.get("tcp", {})
        net = series_map.get("net", {})
        ct = series_map.get("conntrack", {})
        ts = series_map.get("tcpstate", {})
        rows = []
        t = lo
        while t <= hi:
            c = (cpu.get(t) or [{}])[0]
            m = (mem.get(t) or [{}])[0]
            k = (tcp.get(t) or [{}])[0]
            ctv = (ct.get(t) or [{}])[0]
            tsv = (ts.get(t) or [{}])[0]
            # 该分钟网络：全部接口流量合计 + drop 合计
            nrx = sum((it.get("rx") or 0) for it in net.get(t, []))
            ndrop = sum((it.get("drop") or 0) for it in net.get(t, []))
            # 选该分钟最忙的磁盘（util 弃用，按 await 和 → IOPS 和 选）
            best = None
            for it in disk.get(t, []):
                u = ((it.get("rawait") or 0) + (it.get("wawait") or 0),
                     (it.get("rs") or 0) + (it.get("ws") or 0))
                if best is None or u > best[0]:
                    best = (u, it)
            d = best[1] if best else {}
            rows.append([
                f"{t:%H:%M}",
                f"{c.get('busy', '')}", f"{c.get('iowait', '')}",
                f"{c.get('r', '')}", f"{c.get('b', '')}",
                f"{m.get('avail', '')}", f"{m.get('swpout', '')}",
                f"{d.get('_key', '')}", f"{d.get('wawait', '')}",
                f"{nrx/1e3:.0f}K" if nrx else "",
                f"{ndrop:g}" if ndrop else "",
                f"{k.get('retrans', '')}", f"{k.get('rtt95', '')}",
                f"{ctv.get('fill', '')}",
                f"{tsv['estab']:g}" if tsv.get("estab") is not None else "",
                f"{tsv['timewait']:g}" if tsv.get("timewait") is not None else "",
                f"{tsv['closewait']:g}" if tsv.get("closewait") is not None else "",
                f"{tsv['synrecv']:g}" if tsv.get("synrecv") is not None else "",
            ])
            t += timedelta(minutes=1)
        hdr = ["time", "busy", "iowait", "r", "b", "MemAvailMB", "pswpout",
               "disk", "w_await", "netRX", "drop", "retrans%", "rtt95",
               "ctfill%", "ESTAB", "TW", "CW", "SR"]
        out.append(render_table(hdr, rows, max_rows=20, max_cell=14))


# ---------------------------------------------------------------- ASP

def parse_asp(frag):
    asp = {"buckets": ([], []), "events": ([], []),
           "queries": ([], []), "blocked": ([], [])}
    for kind, payload in iter_blocks(frag):
        if kind != "table":
            continue
        hdr, rows = payload
        h = [x.lower() for x in hdr]
        if h[:2] == ["date_time", "max_sess"]:
            asp["buckets"] = (hdr, rows)
        elif h[:3] == ["date_time", "event_type", "event"]:
            asp["events"] = (hdr, rows)
        elif h[:3] == ["date_time", "unique_query_id", "unique_query"]:
            asp["queries"] = (hdr, rows)
        elif h[:2] == ["date_time", "root_block_sessid"]:
            asp["blocked"] = (hdr, rows)
    return asp


def asp_bucket_window(label):
    """[local_active_session] 2026-10-20 09:00:00-09:05:00 → (dt, dt)"""
    s = clean(label)
    m = re.search(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})-(\d{2}:\d{2}:\d{2})", s)
    if not m:
        return None, None
    a = parse_dt(m.group(1))
    b = parse_dt(m.group(1)[:11] + m.group(2))
    return a, b


def bucket_key(label):
    a, b = asp_bucket_window(label)
    return (a, b) if a else (None, None)


def detect_asp_windows(asp, aas, out):
    """DB 侧时间轴：ASP 5分钟桶里 active session / wait_* 的异常桶"""
    hdr, rows = asp["buckets"]
    if not hdr:
        out.append("### DB 侧时段（ASP）")
        out.append("  (无 ASP 桶数据，DB 侧时间轴不可用)")
        return []
    def col(name):
        try:
            return hdr.index(name)
        except ValueError:
            return None
    iact = col("max_active_sess")
    vals = [num(r[iact]) for r in rows if iact is not None and iact < len(r)]
    vals = [v for v in vals if v is not None]
    if not vals:
        return []
    med = statistics.median(vals)
    # 活跃会话阈值：本窗中位数 1.3 倍、且不低于 AAS 的 1.5 倍
    thr = max(1.3 * med, 1.5 * aas, 10)
    # 等待类计数阈值（5分钟桶合计）：超过即视为数据库侧异常
    WAIT_THR = {"wait_lock": 3, "wait_lwlock": 3, "wait_io": 3,
                "wait_wal_sync": 2, "wait_trans_sync": 5}
    flagged = []
    for r in rows:
        a, b = asp_bucket_window(r[0])
        if not a:
            continue
        act = num(r[iact]) if iact is not None and iact < len(r) else None
        waits = {}
        for ci, h in enumerate(hdr):
            if h.startswith("wait_") and ci < len(r):
                wv = num(r[ci])
                if wv is not None and wv >= WAIT_THR.get(h, 10**9):
                    waits[h] = wv
        if (act is not None and act >= thr) or waits:
            flagged.append((a, b, act, waits, r[0]))
    out.append("### DB 侧时段（ASP 5分钟桶：max_active_sess≥"
               f"{thr:.1f} 或等待计数超阈值 {WAIT_THR}）")
    if not flagged:
        out.append("  (无异常桶)")
        return []
    # 合并相邻桶
    windows = []
    cur = []
    for f in flagged:
        if cur and (f[0] - cur[-1][1]).total_seconds() > 600:
            windows.append(cur); cur = []
        cur.append(f)
    if cur:
        windows.append(cur)
    res = []
    for i, w in enumerate(windows, 1):
        start, end = w[0][0], w[-1][1]
        peak = max(w, key=lambda x: x[2] or 0)
        waits = Counter()
        for x in w:
            for k, v in x[3].items():
                waits[k] += v
        typ = "持续型" if (end - start).total_seconds() > 300 else "尖刺"
        dom = "DB:" + ",".join(k.replace("wait_", "") for k, _ in waits.most_common(2)) if waits else "DB:active_sess"
        out.append(f"  DB时段{i}: {start:%H:%M}~{end:%H:%M} ({typ}, {dom}) "
                   f"峰值活跃={peak[2]:.0f}@{peak[0]:%H:%M} "
                   f"等待计数={dict(waits)}")
        res.append({"start": start, "end": end, "minutes": int((end - start).total_seconds() // 60),
                    "type": "DB侧-" + typ, "dominant": dom})
    return res


def summarize_asp(asp, merged, out):
    hdr, buckets = asp["buckets"]
    out.append("### ASP 5分钟桶")
    if not hdr:
        out.append("  (无 ASP 桶数据)")
        return
    # 输出桶表（全量，最多 288 桶 = 24h）
    widths = [56, 9, 9, 9, 9, 9, 9, 9, 9]
    out.append(fmt_row([hdr[i] if i < len(hdr) else "" for i in range(9)], widths))
    for r in buckets[:288]:
        out.append(fmt_row([r[i] if i < len(r) else "" for i in range(9)], widths))
    # 峰值桶
    def maxcol(name):
        try:
            i = hdr.index(name)
        except ValueError:
            return None
        best = None
        for r in buckets:
            v = num(r[i]) if i < len(r) else None
            if v is not None and (best is None or v > best[0]):
                best = (v, r[0])
        return best
    peak = maxcol("max_active_sess")
    if peak:
        out.append(f"  峰值 active session = {peak[0]:.0f} @ {peak[1]}")
    for col in hdr:
        if col.startswith("wait_"):
            b = maxcol(col)
            if b and b[0] > 0:
                out.append(f"  {col:<16} 峰值 {b[0]:.0f} @ {b[1]}")

    # 按时段切 ASP 事件/query
    ev_hdr, ev_rows = asp["events"]
    q_hdr, q_rows = asp["queries"]
    b_hdr, b_rows = asp["blocked"]
    for wi, w in enumerate(merged[:8], 1):
        lo = w["start"] - timedelta(minutes=10)
        hi = w["end"]
        out.append(f"### 时段{wi} ASP 切片 {lo:%H:%M}~{hi:%H:%M}（含前置10分钟）"
                   f" [来源:{w.get('dominant', 'OS')}]")
        ev = Counter()
        for r in ev_rows:
            a, b = bucket_key(r[0])
            if not a or not (lo <= a <= hi):
                continue
            cnt = (num(r[3]) or 0) if len(r) > 3 else 0
            ev[(r[1], r[2])] += cnt
        if ev:
            out.append("  事件 Top8（按 max_cnt 累加）:")
            for (etype, name), cnt in ev.most_common(8):
                out.append(f"    {etype:<12} {name:<40} {cnt:.0f}")
        else:
            out.append("  (该切片无 ASP 事件行)")
        q = Counter()
        qtext = {}
        for r in q_rows:
            a, b = bucket_key(r[0])
            if not a or not (lo <= a <= hi):
                continue
            cnt = (num(r[3]) or 0) if len(r) > 3 else 0
            q[r[1]] += cnt
            qtext[r[1]] = r[2]
        if q:
            out.append("  Query Top5（按 max_cnt 累加）:")
            for uid, cnt in q.most_common(5):
                out.append(f"    {uid:<12} {cnt:>5.0f}  {qtext.get(uid, '')[:70]}")
        bl = []
        for r in b_rows:
            a, b = bucket_key(r[0])
            if a and lo <= a <= hi and len(r) > 2 and (num(r[2]) or 0) > 0:
                bl.append(r)
        if bl:
            out.append(f"  阻塞链 {len(bl)} 条，Top3:")
            for r in bl[:3]:
                out.append(f"    {r[0][-16:]} root={r[1][:46]} max_b={r[2]} "
                           f"w_sql={(r[4] if len(r) > 4 else '')[:60]}")


# ---------------------------------------------------------------- Pagewriter / Long Transaction（5min 序列）

def parse_pagewriter(frag):
    """Pagewriter Status 表 → [(ts, flush_delta, pages_s, last_flush, dirty, behind, restart)]"""
    rows = []
    for hdr, body in iter_tables(frag):
        low = [h.strip().lower() for h in hdr]
        if not low or low[0] != "sample time" or "remain_dirty_page_num" not in low:
            continue
        idx = {}
        for want, key in (("sample time", "ts"),
                          ("flush delta (pgwr_actual_flush_total_num)", "delta"),
                          ("pages/s", "rate"),
                          ("pgwr_last_flush_num", "last"),
                          ("remain_dirty_page_num", "dirty"),
                          ("behind xlog_insert (mb)", "behind")):
            for i, h in enumerate(low):
                if h == want or h.startswith(want):
                    idx[key] = i
                    break
        for r in body:
            t = parse_dt(r[0] if r else "")
            if not t:
                continue
            def g(k):
                i = idx.get(k)
                return r[i] if i is not None and i < len(r) else ""
            rows.append({"ts": t, "delta_raw": g("delta"), "delta": num(g("delta")),
                        "rate": num(g("rate")), "last": num(g("last")),
                        "dirty": num(g("dirty")), "behind": num(g("behind")),
                        "restart": g("delta").strip().upper() == "R"})
        break
    return rows


def parse_longtxn(frag):
    """Long Transaction Monitor 表 → [(ts, cnt, itx, act, oldest_dur_s, oldest_state, wait, usi)]"""
    rows = []
    for hdr, body in iter_tables(frag):
        low = [h.strip().lower() for h in hdr]
        if not low or low[0] != "sample time" or "ltx60_cnt" not in low:
            continue
        idx = {}
        for want, key in (("sample time", "ts"), ("ltx60_cnt", "cnt"),
                          ("ltx60_itx_cnt", "itx"), ("ltx60_active_cnt", "act"),
                          ("duration", "dur"), ("state", "state"),
                          ("wait event", "wait"), ("unique_sql_id", "usi")):
            for i, h in enumerate(low):
                if h == want:
                    idx[key] = i
                    break
        def dur_s(v):
            m = re.match(r"^\s*(?:(\d+)d)?\s*(?:(\d+):)?(\d+):(\d+)(?:\.(\d+))?\s*$", str(v or ""))
            if not m:
                return None
            d, hh, mm, ss, _ = m.groups()
            return int(d or 0) * 86400 + int(hh or 0) * 3600 + int(mm) * 60 + int(ss)
        for r in body:
            if r and "sample error" in " ".join(r):
                continue
            t = parse_dt(r[0] if r else "")
            if not t:
                continue
            def g(k, default=""):
                i = idx.get(k)
                return r[i] if i is not None and i < len(r) else default
            rows.append({"ts": t, "cnt": num(g("cnt")) or 0, "itx": num(g("itx")) or 0,
                        "act": num(g("act")) or 0, "dur": dur_s(g("dur")),
                        "state": g("state"), "wait": g("wait"), "usi": g("usi")})
        break
    return rows


def summarize_pw_lt(pw, lt, merged, out):
    out.append("### Pagewriter 5min 序列")
    if not pw:
        out.append("  (无 pagewriter 采样)")
    else:
        dirty = [p["dirty"] for p in pw if p["dirty"] is not None]
        rates = [p["rate"] for p in pw if p["rate"] is not None]
        behind = [p["behind"] for p in pw if p["behind"] is not None]
        restarts = sum(1 for p in pw if p["restart"])
        flush_total = sum(p["delta"] for p in pw if p["delta"] is not None)
        out.append(f"  采样 {len(pw)} 个；flush 合计 {flush_total:,.0f} 页；重启标记 R {restarts} 个")
        if dirty:
            pk = max(pw, key=lambda p: p["dirty"] or 0)
            out.append(f"  remain_dirty 峰值 {max(dirty):,.0f} @{pk['ts']:%H:%M}，均值 {statistics.mean(dirty):,.0f}")
        if rates:
            pk = max(pw, key=lambda p: p["rate"] or 0)
            out.append(f"  pages/s 峰值 {max(rates):,.1f} @{pk['ts']:%H:%M}")
        if behind:
            pk = max(pw, key=lambda p: p["behind"] or 0)
            out.append(f"  behind xlog 峰值 {max(behind):,.1f} MB @{pk['ts']:%H:%M}"
                       f"（脏页队列头落后插入点=刷脏跟不上写入）")
    out.append("### Long Transaction 5min 序列")
    if not lt:
        out.append("  (无长事务采样)")
    else:
        cnts = [l["cnt"] for l in lt]
        pk = max(lt, key=lambda l: l["cnt"])
        out.append(f"  采样 {len(lt)} 个；ltx60_cnt 峰值 {max(cnts):.0f} @{pk['ts']:%H:%M}，非零采样 {sum(1 for c in cnts if c > 0)} 个")
        wd = [l for l in lt if l["dur"] is not None]
        if wd:
            worst = max(wd, key=lambda l: l["dur"])
            out.append(f"  最老事务龄 峰值 {worst['dur']:,.0f}s @{worst['ts']:%H:%M}"
                       f" state={worst['state']} wait={worst['wait']} sql={worst['usi']}")


# ---------------------------------------------------------------- 等待事件

def parse_wait_events(frag):
    rows = []
    for kind, payload in iter_blocks(frag):
        if kind != "table":
            continue
        hdr, body = payload
        if not hdr:
            continue
        if hdr[0].strip().lower() == "event" and any("Class" in h for h in hdr):
            rows = body
            break
    return rows


def summarize_events(rows, merged, out):
    out.append("### 事件谱（按 Class 聚合）")
    if not rows:
        out.append("  (无等待事件表)")
        return
    hdr = ["Event", "Class", "Waits", "Total Wait Time (s)", "Avg wait (ms)", "% DB time"]
    ci = {name: i for i, name in enumerate(hdr)}
    cls = defaultdict(lambda: [0, 0.0, 0.0])   # count, total s, pct sum
    parsed = []
    for r in rows:
        if len(r) < 6:
            continue
        ev = r[0]
        klass = r[1]
        waits = num(r[2]) or 0
        total = num(r[3]) or 0
        avg = num(r[4])
        pct = (num(r[5]) or 0) / 100.0
        parsed.append((ev, klass, waits, total, avg, pct))
        cls[klass][0] += 1
        cls[klass][1] += total
        cls[klass][2] += pct
    for klass, (cnt, total, pct) in sorted(cls.items(), key=lambda x: -x[1][1]):
        tag = "  ← 非等待状态量,不计入瓶颈" if klass.upper() == "STATUS" else ""
        out.append(f"  {klass:<14} 事件数={cnt:<4} 总等待={total:>10.1f}s  %DBtime合计={pct*100:>6.2f}%{tag}")
    # Top 事件（真实等待，排除 STATUS）
    out.append("### Top 事件（排除 STATUS）")
    real = [p for p in parsed if p[1].upper() != "STATUS"]
    real.sort(key=lambda x: -x[3])
    out.append(fmt_row(["Event", "Class", "Waits", "Total(s)", "Avg(ms)", "%DBtime"],
                       [44, 13, 16, 12, 10, 9]))
    for ev, klass, waits, total, avg, pct in real[:15]:
        out.append(fmt_row([ev, klass, f"{waits:,.0f}", f"{total:.1f}",
                            f"{avg:.2f}" if avg is not None else "-", f"{pct*100:.2f}%"],
                           [44, 13, 16, 12, 10, 9]))
    out.append("### STATUS 类（执行阶段标记，不得当瓶颈）")
    stat = [p for p in parsed if p[1].upper() == "STATUS"]
    stat.sort(key=lambda x: -x[3])
    for ev, klass, waits, total, avg, pct in stat[:10]:
        out.append(f"  {ev:<46} waits={waits:,.0f} total={total:.1f}s pct={pct*100:.2f}%")
    # 需要 se 溯源的事件
    need = []
    for ev, klass, waits, total, avg, pct in real[:8]:
        if pct >= 0.05 or (avg is not None and avg >= 5):
            need.append((ev, klass, f"{pct*100:.2f}%", f"avg={avg}ms", f"waits={waits:,.0f}"))
    # 异常时段内 ASP 出现的事件
    if merged:
        lo = merged[0]["start"] - timedelta(minutes=10)
        hi = merged[-1]["end"]
        out.append(f"### 需 vb_tool se 溯源的事件（%DBtime≥5% 或 avg≥5ms；时段 {lo:%H:%M}~{hi:%H:%M}）")
    else:
        out.append("### 需 vb_tool se 溯源的事件（%DBtime≥5% 或 avg≥5ms）")
    for ev, klass, pct, avg, waits in need:
        out.append(f"  {ev:<46} [{klass}] {pct:>8} {avg:<12} {waits}")
    if not need:
        out.append("  (无触发事件)")
    # 高频轻量事件提示
    out.append("### 高频轻量事件（waits 极高且 avg≈0，先查是否记账型）")
    for ev, klass, waits, total, avg, pct in real:
        if waits > 1e6 and (avg is None or avg < 0.01):
            out.append(f"  {ev:<46} waits={waits:,.0f} avg={avg}ms pct={pct*100:.2f}% "
                       f"→ 与 Load Profile 块访问速率对账")


# ---------------------------------------------------------------- 堆栈

IDLE_MARKERS = ("WaitLatchOrSocket", "PGSemaphoreLock", "pg_usleep",
                "epoll_wait", "WaitEventSetWait", "SemaphoreWait", "sched_yield")


def norm_stack(stack):
    """去掉 +0x偏移/裸地址，取前5帧函数名"""
    frames = [f.strip() for f in stack.split("<-")]
    out = []
    for f in frames:
        f = re.sub(r"\s*\+\s*0x[0-9a-fA-F]+", "", f)
        f = re.sub(r"^0x[0-9a-fA-F]+$", "", f).strip()
        f = re.sub(r"\(.*\)", "()", f)          # 去掉参数类型，只留函数名
        f = re.sub(r"\s+", " ", f)
        if f and f not in ("start_thread", "clone"):
            out.append(f)
        if len(out) >= 5:
            break
    return out


def parse_callstack(frag):
    """返回 background / autovac / foreground 三组"""
    res = {"background": [], "autovac": [], "foreground": []}
    sub = ""
    for kind, payload in iter_blocks(frag):
        if kind == "sub":
            sub = payload.lower()
            continue
        hdr, rows = payload
        if not hdr or hdr[0] not in ("Sample Time",):
            continue
        if "worker cnt" in [x.lower() for x in hdr]:
            res["autovac"].extend(rows)
        elif any(x.lower() == "stack" for x in hdr) and any(
                x.lower() == "thread" for x in hdr):
            res["background"].extend(rows)
        elif any(x.lower() == "stack" for x in hdr):
            res["foreground"].extend(rows)
    return res


def summarize_callstack(cs, merged, out):
    out.append("### Background 线程堆栈指纹")
    bg = cs["background"]
    if not bg:
        out.append("  (无后台线程采样)")
    else:
        # 列: Sample Time, Thread, PID, State, Wait Event, CPU%, Stack
        groups = OrderedDict()
        threads = Counter()
        for r in bg:
            if len(r) < 7:
                continue
            t = parse_dt(r[0])
            thread = r[1]
            threads[thread] += 1
            sig = " <- ".join(norm_stack(r[6]))
            key = (thread, sig)
            g = groups.setdefault(key, {"n": 0, "first": t, "last": t,
                                        "cpu": [], "event": Counter()})
            g["n"] += 1
            if t:
                g["first"] = min(g["first"], t) if g["first"] else t
                g["last"] = max(g["last"], t) if g["last"] else t
            cv = num(r[5])
            if cv is not None:
                g["cpu"].append(cv)
            g["event"][r[4]] += 1
        out.append("  线程采样数: " + ", ".join(f"{k}={v}" for k, v in threads.items()))
        for (thread, sig), g in sorted(groups.items(), key=lambda x: -x[1]["n"]):
            idle = any(m in sig for m in IDLE_MARKERS)
            cpu = f"{max(g['cpu']):.0f}%" if g["cpu"] else "-"
            tag = "空闲基线" if idle else "★活动栈"
            out.append(f"  [{tag}] {thread:<22} n={g['n']:<3} CPUmax={cpu:<5} "
                       f"{hm(g['first'])}~{hm(g['last'])}  {sig[:150]}")
    out.append("### AutoVacuum Workers 采样")
    av = cs["autovac"]
    if not av:
        out.append("  (无采样)")
    else:
        cnts = []
        for r in av:
            if len(r) >= 3:
                cnts.append(num(r[1]) or 0)
        out.append(f"  采样 {len(av)} 次，worker_cnt: min={min(cnts):.0f} max={max(cnts):.0f} "
                   f"非零采样={sum(1 for c in cnts if c > 0)} 次")
        for r in av[:5]:
            if len(r) >= 4:
                out.append(f"    {r[0]} worker={r[1]} cpu={r[2]} stack={r[3][:120]}")
    out.append("### Foreground LWLock 堆栈")
    fg = cs["foreground"]
    real = [r for r in fg if r and "no LWLock-waiting" not in " ".join(r)]
    if not real:
        out.append("  (全窗无前台 LWLock 等待采样)")
    else:
        groups = OrderedDict()
        for r in real:
            if len(r) < 6:
                continue
            sig = " <- ".join(norm_stack(r[5]))
            key = (r[1], r[3], sig)
            g = groups.setdefault(key, {"n": 0, "cnt": 0,
                                        "first": parse_dt(r[0]), "last": parse_dt(r[0])})
            g["n"] += 1
            g["cnt"] += num(r[4]) or 0
            t = parse_dt(r[0])
            if t:
                g["first"] = min(g["first"], t) if g["first"] else t
                g["last"] = max(g["last"], t) if g["last"] else t
        for (ev, sid, sig), g in sorted(groups.items(), key=lambda x: -x[1]["n"]):
            out.append(f"  {ev:<40} sql={sid:<12} 样本={g['n']} event_cnt累加={g['cnt']:.0f} "
                       f"{hm(g['first'])}~{hm(g['last'])}")
            out.append(f"      {sig[:220]}")
    # 对齐异常时段
    if merged:
        out.append("### 堆栈×异常时段对齐")
        for i, w in enumerate(merged[:5], 1):
            inwin = []
            for r in bg:
                t = parse_dt(r[0]) if r else None
                if t and w["start"] <= t <= w["end"]:
                    inwin.append(r[1])
            out.append(f"  时段{i} {w['start']:%H:%M}~{w['end']:%H:%M}: "
                       f"后台采样 {len(inwin)} 条 {dict(Counter(inwin))}")


# ---------------------------------------------------------------- SQL

def parse_sql_stats(frag, topn=10):
    dims = OrderedDict()
    cur = ""
    for kind, payload in iter_blocks(frag):
        if kind == "sub":
            cur = payload
            continue
        hdr, rows = payload
        if not cur or not hdr:
            continue
        low = [h.lower() for h in hdr]
        if "sql id" not in low:
            continue
        if cur in dims:
            continue
        dims[cur] = (hdr, rows[:topn])
    return dims


def summarize_sql(dims, sqltexts, out):
    out.append("### 各维度 Top10")
    matrix = defaultdict(list)
    for dim, (hdr, rows) in dims.items():
        out.append(f"## {dim}")
        out.append(render_table(hdr, rows, max_rows=10, max_cell=26))
        low = [h.lower() for h in hdr]
        try:
            sid_i = low.index("sql id")
        except ValueError:
            continue
        # 维度主指标是否全 0（AWR 采集缺口）
        metric = dim.replace("SQL ordered by ", "").lower()
        mi = None
        for i, h in enumerate(low):
            if h == metric or h.startswith(metric):
                mi = i
                break
        all_zero = True
        if mi is not None:
            for r in rows:
                if mi < len(r) and (num(r[mi]) or 0) > 0:
                    all_zero = False
                    break
            if all_zero:
                out.append("  (本维度指标全为 0 —— AWR 采集缺口，不参与矩阵)")
                continue
        for r in rows:
            if sid_i < len(r):
                matrix[r[sid_i]].append(dim)
    out.append("### 跨维度矩阵（同一 SQL 出现在 ≥2 个维度）")
    multi = {k: v for k, v in matrix.items() if len(v) >= 2}
    for sid, ds in sorted(multi.items(), key=lambda x: -len(x[1]))[:25]:
        short = "; ".join(d.replace("SQL ordered by ", "") for d in ds)
        out.append(f"  {sid:<12} {len(ds)}维: {short}")
    if not multi:
        out.append("  (无跨维多榜 SQL)")
    out.append("### SQL 文本（Top 维度涉及的 SQL）")
    shown = 0
    for sid in list(matrix)[:50]:
        txt = sqltexts.get(sid)
        if txt:
            out.append(f"  {sid}: {txt[:220]}")
            shown += 1
    if not shown:
        out.append("  (未取到 SQL 文本)")


def parse_sql_text(frag):
    texts = {}
    for kind, payload in iter_blocks(frag):
        if kind != "table":
            continue
        hdr, rows = payload
        if [h.lower() for h in hdr[:2]] != ["sql id", "sql text"]:
            continue
        for r in rows:
            if len(r) >= 2:
                texts[r[0]] = r[1]
    return texts


# ---------------------------------------------------------------- 参数（GUC + sysctl）

# 关键 GUC 清单：现象 → 参数分组见 performance/parameters.md；此处为 digest 摘录子集
KEY_GUCS = [
    # 连接/线程池
    "max_connections", "enable_thread_pool", "thread_pool_attr", "listen_addresses",
    "password_encryption_type",
    # 内存
    "shared_buffers", "work_mem", "maintenance_work_mem", "max_process_memory",
    "temp_buffers", "cstore_buffers", "segment_buffers", "wal_buffers",
    "effective_cache_size", "local_syscache_threshold", "syscache_threshold",
    # WAL / checkpoint / 双写
    "checkpoint_timeout", "checkpoint_segments", "max_redo_log_size",
    "wal_writer_sleep", "advance_xlog_file_num", "enable_double_write",
    "commit_delay", "commit_siblings", "sync_commit", "remotereadmode",
    # 锁 / 事务
    "deadlock_timeout", "lockwait_timeout", "default_transaction_isolation",
    "max_prepared_transactions", "statement_timeout", "idle_in_transaction_timeout",
    # vacuum / 膨胀
    "autovacuum", "enable_autovacuum", "autovacuum_max_workers",
    "autovacuum_vacuum_threshold", "autovacuum_vacuum_scale_factor",
    "autovacuum_analyze_threshold", "autovacuum_analyze_scale_factor",
    "autovacuum_naptime", "autovacuum_timeout", "autovacuum_mode",
    "vacuum_cost_delay", "vacuum_cost_limit",
    # 计划 / SQL
    "plan_cache_mode", "enable_seqscan", "enable_opfusion", "behavior_compat_options",
    "sql_compatibility", "enable_bitmapscan", "random_page_cost",
    # 采集开关（AWR 自身视野）
    "enable_asp", "asp_sample_interval", "instr_unique_sql_count",
    "enable_instr_track_percent", "track_activity_query_size",
    # 日志
    "logging_collector", "log_min_duration_statement", "log_statement",
    # 网络/复制
    "wal_level", "synchronous_commit", "most_available_syncmode",
    "sync_config_hostname", "enable_stream_replication",
]

KEY_SYSCTLS = [
    "net.core.somaxconn", "net.core.netdev_max_backlog", "net.core.rmem_max",
    "net.core.wmem_max", "net.core.netdev_budget",
    "net.ipv4.tcp_max_syn_backlog", "net.ipv4.ip_local_port_range",
    "net.ipv4.tcp_tw_reuse", "net.ipv4.tcp_syncookies", "net.ipv4.tcp_retries2",
    "net.ipv4.tcp_keepalive_time", "net.ipv4.tcp_timestamps",
    "vm.swappiness", "vm.overcommit_memory", "vm.min_free_kbytes",
    "vm.dirty_background_ratio", "vm.dirty_ratio", "vm.dirty_expire_centisecs",
    "fs.file-max", "fs.aio-max-nr", "fs.nr_open",
    "kernel.sem", "kernel.shmmax", "kernel.shmall",
    "net.ipv4.tcp_mem", "net.core.somaxconn",
]


def parse_params(secs, out, base):
    """Database Parameters(folded, 全量 pg_settings) + OS Kernel Parameters(sysctl)。
    digest 摘关键子集；全量写 <base>_params.txt。返回 (guc_dict, sysctl_dict)。"""
    guc = {}
    sysctl = {}
    frag = get_sec(secs, "Database Parameters")
    for hdr, rows in iter_tables(frag):
        low = [h.strip().lower() for h in hdr]
        if low[:2] != ["name", "setting"]:
            continue
        for r in rows:
            if len(r) >= 2:
                guc[r[0].strip()] = r[1].strip()
        break
    sfrag = get_sec(secs, "OS Kernel Parameters")
    for hdr, rows in iter_tables(sfrag):
        low = [h.strip().lower() for h in hdr]
        if low[:2] != ["parameter", "begin"]:
            continue
        for r in rows:
            if len(r) >= 3:
                # 中途变更显示 "old -> new"：取 End 列（无则 Begin）
                sysctl[r[0].strip()] = r[2].strip() if r[2].strip() not in ("", "-") else r[1].strip()
        break
    out.append(f"### GUC 关键参数（全量 {len(guc)} 个在 {os.path.basename(base)}_params.txt）")
    hit = 0
    for k in KEY_GUCS:
        for gk, gv in guc.items():
            if gk.lower() == k:
                out.append(f"  {gk:<38} = {gv}")
                hit += 1
                break
    if not hit:
        out.append("  (报告无 Database Parameters 段或解析失败)")
    out.append(f"### sysctl 关键参数（全量 {len(sysctl)} 个在 {os.path.basename(base)}_params.txt）")
    hit = 0
    for k in KEY_SYSCTLS:
        if k in sysctl:
            out.append(f"  {k:<38} = {sysctl[k]}")
            hit += 1
    if not hit:
        out.append("  (报告无 OS Kernel Parameters 段或解析失败)")
    # 全量落盘
    with open(base + "_params.txt", "w", encoding="utf-8") as f:
        f.write(f"# 全量参数提取 来源: 报告内 Database Parameters + OS Kernel Parameters 段\n")
        f.write(f"# 提取时间: {datetime.now():%Y-%m-%d %H:%M:%S}\n\n")
        f.write("== GUC (pg_settings, live at render) ==\n")
        for k in sorted(guc):
            f.write(f"{k} = {guc[k]}\n")
        f.write("\n== sysctl (Begin 列为空时取 End) ==\n")
        for k in sorted(sysctl):
            f.write(f"{k} = {sysctl[k]}\n")
    return guc, sysctl


def std_check(meta, guc, out):
    """对照 G100 标准化参数基线（g100-standard-baseline.json，由公司标准化参数表 xlsx 生成）。
    版本取报告头 Build 号；仅初筛：单位/格式差异（10min vs 600s、大小写、引号）需人工核。
    基线更新契约：用户通知新版本 xlsx → 替换 workspace 原件 → 重生成 json 与 admin/g100-standard-params.md。"""
    bpath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "g100-standard-baseline.json")
    if not os.path.exists(bpath):
        return
    with open(bpath, encoding="utf-8") as f:
        baseline = json.load(f)
    ver_str = " ".join(str(v) for v in meta.values() if v)
    m = re.search(r"Build\s*(\d+)", ver_str)
    ver = "3.0." + m.group(1) if m else None

    _sec = {"ms": 1e-3, "s": 1, "min": 60, "h": 3600, "d": 86400}
    _byt = {"B": 1, "kB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4}
    _bool = {"true": "on", "false": "off", "yes": "on", "no": "off"}

    def norm(s):
        s = str(s).strip().strip("'\"")
        low = s.lower()
        if low in _bool:
            return _bool[low]
        if low in ("on", "off"):
            return low
        m = re.fullmatch(r"(-?[\d.]+)\s*(ms|s|min|h|d|B|kB|MB|GB|TB)", s)
        if m:
            u = m.group(2)
            return str(round(float(m.group(1)) * (_sec.get(u) or _byt[u]), 3))
        if re.fullmatch(r"-?[\d.]+", s):
            return str(float(s))
        return re.sub(r"\s+", "", s)  # 选项列表：仅去空白

    out.append("")
    out.append("### 标准化参数对照（G100 标准基线初筛；全表见 admin/g100-standard-params.md）")
    if not ver or ver not in baseline:
        out.append(f"  版本 {ver or '未识别'} 无对应基线（现有: {' / '.join(baseline)}），跳过对照")
        return
    std = baseline[ver]
    dev, same, miss = [], 0, []

    def _hit(got, want):
        """基线值三种形制：区间 'A~B'（数值闭区间）/ 枚举 'A|B'（任一命中）/
        子字段 'KEY=V'（复合串参数只判 KEY 子字段，如 num_internal_lock_partitions）"""
        got, want = str(got), str(want)
        m = re.fullmatch(r"([A-Za-z0-9_]+)=([^\s]+)", want)
        if m and "=" in got:  # 复合串（CLOG_PART=256,...,FASTPATH_PART=100）取子字段
            mm = re.search(rf"(?:^|,){m.group(1)}=([^,\s]+)", got)
            if mm:
                got, want = mm.group(1), m.group(2)
        if "|" in want:  # 枚举：分支可再为区间/标量
            return any(_hit(got, w) for w in want.split("|"))
        m = re.fullmatch(r"(-?[\d.]+)~(-?[\d.]+)", want)
        if m and re.fullmatch(r"-?[\d.]+", got.strip()):
            return float(m.group(1)) <= float(got) <= float(m.group(2))
        return norm(got) == norm(want)

    for k, want in sorted(std.items()):
        got = guc.get(k)
        if got is None:
            miss.append(k)
        elif _hit(got, want):
            same += 1
        else:
            dev.append((k, want, got))
    out.append(f"  基线 {ver}：偏离 {len(dev)} / 一致 {same} / 报告未含 {len(miss)}"
               "（初筛口径：单位与格式差异需人工核，如 10min vs 600s）")
    for k, want, got in dev:
        out.append(f"  [偏离] {k:<38} 标准={want}  报告={got}")
    if miss:
        out.append(f"  [报告未含 {len(miss)} 项] {' '.join(miss[:20])}{' …' if len(miss) > 20 else ''}")


# ---------------------------------------------------------------- dmesg / df / RTT / 复制延迟

def summarize_dmesg(os_frag, out):
    cats = []
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if low[:2] != ["time", "category"]:
            continue
        cats = rows
        break
    out.append("### 内核消息（dmesg，已分类）")
    if not cats:
        out.append("  (无内核错误记录 —— 'no kernel errors in this window' 或段缺失)")
        return
    cnt = Counter(r[1] for r in cats if len(r) >= 2)
    for cat, n in cnt.most_common():
        first = next(r[0] for r in cats if len(r) >= 2 and r[1] == cat)
        out.append(f"  {cat:<14} {n} 条（首条 {first}）")
    for r in cats[:10]:
        out.append(f"    {r[0]} [{r[1]}] {(r[2] if len(r) > 2 else '')[:110]}")


def summarize_fs(os_frag, out):
    out.append("### 文件系统使用（Filesystem Usage）")
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if not low or "mounted on" not in " ".join(low):
            continue
        out.append(render_table(hdr, rows[:20], max_cell=26))
        break
    else:
        out.append("  (无 df 数据段)")


def summarize_rtt(os_frag, out):
    out.append("### TCP RTT 汇总")
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if low[:2] != ["metric", "value"]:
            continue
        for r in rows:
            if len(r) >= 2:
                out.append(f"  {r[0]:<52} {r[1]}")
        break
    else:
        out.append("  (无 RTT 段)")


# ----------------- OS 新节（两代兼容：v2.5.17 五节 / v2.5.18 DB Process 折表 + TCP Statistics 独立章）

def _peak(rows, idx, tidx=0):
    """rows 第 idx 列的 (峰值, 峰值时刻)；无法解析返回 None"""
    best = None
    for r in rows:
        if idx >= len(r):
            continue
        v = parse_num(r[idx])[0]
        if v is not None and (best is None or v > best[0]):
            best = (v, r[tidx] if tidx < len(r) else "")
    return best

def summarize_os_new(os_frag, out):
    # Bond：/proc/net/bonding 存在才有表，否则条件态
    out.append("### Bond 设备")
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if low[:2] != ["bond", "mode"]:
            continue
        out.append(render_table(hdr, rows[:10], max_cell=30))
        break
    else:
        out.append("  (无 bond 设备 —— /proc/net/bonding 不存在)")

    # RX Missed：v2.5.18 起并入网卡表当列（RX Missed (Delta)）；旧版独立表 interface|missed
    out.append("### RX Missed（网卡 ring 溢出丢包）")
    got_missed = False
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        mi = next((i for i, h in enumerate(low) if h.startswith("rx missed")), None)
        if mi is None or not rows:
            continue
        hits = [(r[0], r[mi]) for r in rows
                if mi < len(r) and parse_num(r[mi])[0] not in (None, 0)]
        if hits:
            got_missed = True
            for ifn, v in hits[:8]:
                out.append(f"  {ifn:<10} Δmissed={v}")
        break
    if not got_missed:
        for hdr, rows in iter_tables(os_frag):
            low = [h.strip().lower() for h in hdr]
            if low[:2] != ["interface", "missed"]:
                continue
            for r in rows:
                if len(r) >= 2:
                    out.append(f"  {r[0]:<10} missed={r[1]}")
            break
        else:
            out.append("  (无 RX Missed 数据 —— 窗口无 netmiss 采样或全 0)")

    # TCP 状态 + conntrack + fd 三桶：
    # v2.5.18 = DB Process 折表一张表提全（LISTEN Recv-Q|TCP Total|四态|FW1/FW2|Conntrack|fd 三桶）
    # v2.5.17 = TCP Sockets（time|listen|estab…）与 Conntrack（time|count…）两张独立表
    fold_hit = False
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if "tcp total" not in low:
            continue
        fold_hit = True
        out.append(f"### DB Process 每分钟折表（{len(rows)} 分钟；TCP 状态/conntrack/fd 三桶）")
        def _col(name):
            return low.index(name) if name in low else None
        for label, col in (("TCP Total", _col("tcp total")), ("ESTAB", _col("estab")),
                           ("SYN-RECV", _col("syn-recv")), ("TIME-WAIT", _col("time-wait")),
                           ("CLOSE-WAIT", _col("close-wait"))):
            if col is None:
                continue
            p = _peak(rows, col)
            if p:
                out.append(f"  {label:<11} 峰值 {p[0]:<8.0f} @ {p[1]}")
        fw = _col("fin-wait-1/2")
        if fw is not None:
            fwmax = max((parse_num(r[fw].split("/")[0])[0] for r in rows
                         if fw < len(r) and parse_num(r[fw].split("/")[0])[0] is not None),
                        default=None)
            if fwmax:
                out.append(f"  FIN-WAIT-1/2 峰值 FW1={fwmax:.0f}（FW1/FW2 双数格式）")
        cu, cl = _col("conntrack used"), _col("conntrack limit")
        if cu is not None:
            used = [parse_num(r[cu])[0] for r in rows if cu < len(r)]
            used = [u for u in used if u is not None]
            lim = next((parse_num(r[cl])[0] for r in rows
                        if cl is not None and cl < len(r) and parse_num(r[cl])[0] is not None), None)
            if used:
                tail = f" | limit {lim:.0f}（fill max {max(used)/lim*100:.1f}%）" if lim else ""
                out.append(f"  conntrack  {len(used)} 分钟 | used {used[0]:.0f}→{used[-1]:.0f}"
                           f"（max {max(used):.0f}）{tail}")
        for label, col in (("DBFile", _col("dbfile fds")), ("TempFile", _col("tempfile fds")),
                           ("XLog", _col("xlog fds"))):
            if col is None:
                continue
            p = _peak(rows, col)
            if p:
                delm = 0
                for r in rows:
                    if col < len(r):
                        m = re.match(r"(\d+)\((\d+) deleted\)", r[col].strip())
                        if m:
                            delm = max(delm, int(m.group(2)))
                note = f"，deleted 峰值 {delm}" if delm else ""
                out.append(f"  fd {label:<9} 峰值 {p[0]:<6.0f} @ {p[1]}{note}")
        break
    if not fold_hit:
        # v2.5.17 回退：TCP Sockets + Conntrack 两张独立表
        out.append("### TCP Sockets（每分钟状态计数峰值）")
        states = ("listen", "estab", "syn-sent", "syn-recv", "fin-wait-1",
                  "fin-wait-2", "time-wait", "close-wait", "closing", "last-ack")
        legacy_tcp = False
        for hdr, rows in iter_tables(os_frag):
            low = [h.strip().lower() for h in hdr]
            if not low or low[0] != "time" or "listen" not in low:
                continue
            legacy_tcp = True
            idx = {s: low.index(s) for s in states if s in low}
            peaks = {}
            for r in rows:
                for s, i in idx.items():
                    v = parse_num(r[i])[0] if i < len(r) else None
                    if v is not None and v > peaks.get(s, (-1, ""))[0]:
                        peaks[s] = (v, r[0])
            for s in states:
                if s in peaks:
                    v, t = peaks[s]
                    out.append(f"  {s:<11} 峰值 {v:<8.0f} @ {t}")
            break
        if not legacy_tcp:
            out.append("  (无 TCP 状态每分钟数据)")
        out.append("### Conntrack（每分钟 count / limit / fill）")
        for hdr, rows in iter_tables(os_frag):
            low = [h.strip().lower() for h in hdr]
            if low[:2] != ["time", "count"]:
                continue
            cnts = [parse_num(r[1])[0] for r in rows if len(r) >= 2]
            fills = [parse_num(r[3])[0] for r in rows if len(r) >= 4]
            cnts = [c for c in cnts if c is not None]
            fills = [f for f in fills if f is not None]
            if cnts and fills:
                out.append(f"  {len(rows)} 分钟 | count {cnts[0]:.0f}→{cnts[-1]:.0f}（max {max(cnts):.0f}）"
                           f" | fill max {max(fills):.2f}%")
            out.append(render_table(hdr, rows, max_cell=14))
            break
        else:
            out.append("  (无 Conntrack 段 —— nf_conntrack 模块未加载或采样器未运行)")

    # TCP Statistics 独立章（v2.5.18）：主机 L4 计数器 + RTT + accept queue 同分钟轴
    out.append("### TCP Statistics（主机 L4 每分钟）")
    tcpstat_hit = False
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if not low or low[0] != "time" or "opens/s" not in low or "retrans %" not in low:
            continue
        tcpstat_hit = True
        def _c(name):
            return low.index(name) if name in low else None
        rp = _c("retrans %")
        if rp is not None:
            vals = [parse_num(r[rp].rstrip("%"))[0] for r in rows if rp < len(r)]
            vals = [v for v in vals if v is not None]
            if vals:
                out.append(f"  Retrans%  max {max(vals):.3f}%（加粗>3% 红标>5%）")
        for label, col in (("RTT avg(ms)", _c("rtt avg (ms)")), ("RTT p95(ms)", _c("rtt p95 (ms)"))):
            if col is None:
                continue
            p = _peak(rows, col)
            if p:
                out.append(f"  {label:<11} 峰值 {p[0]:.3f} @ {p[1]}（加粗>5ms 红标>20ms）")
        for label, col in (("CurrEstab", _c("currestab")), ("ListenDrops/s", _c("listendrops/s")),
                           ("Timeouts/s", _c("timeouts/s")), ("SynRetrans/s", _c("synretrans/s"))):
            if col is None:
                continue
            p = _peak(rows, col)
            if p and p[0]:
                out.append(f"  {label:<13} 峰值 {p[0]:.2f} @ {p[1]}")
        break
    if not tcpstat_hit:
        out.append("  (无 TCP Statistics 段 —— 旧版报告或窗口无 tcp 采样行)")

    # Memory Fragmentation：buddyinfo 分 zone 订单分布（Begin/End/delta）
    out.append("### 内存碎片（buddyinfo 分 zone 订单分布）")
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if low[:2] != ["zone", "phase"]:
            continue
        out.append(render_table(hdr, rows[:12], max_cell=24))
        break
    else:
        out.append("  (无碎片段)")

    # ---- Cgroup 资源包络（v2.5.20 / 2026-11-01 起）：Metric|Begin|End|Delta 单表 ----
    out.append("### Cgroup（配额/限流包络；口径见 metrics.md Cgroup 行）")
    cg = None
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if not low or not low[0].startswith("metric"):
            continue
        keys = [str(r[0]).strip().lower() for r in rows if r]
        if any(k.split(".")[0] in ("cpu", "memory", "pids", "cpuset", "cpuacct",
                                   "memsw", "io") for k in keys):
            cg = (low, rows)
            break
    if cg is None:
        out.append("  (无 Cgroup 表 —— 老快照或进程不在 cgroup 限层)")
    else:
        low, rows = cg
        idx = {w: low.index(w) for w in ("begin", "end", "delta") if w in low}
        def _cg(r, w):
            i = idx.get(w)
            if i is None or i >= len(r):
                return "-"
            return str(r[i]).strip() or "-"

        def _cgnum(s):
            # cgroup Δ 列带前导 '+'，'reset'/'unlimited' 非数值
            return num(str(s).lstrip("+"))
        cfg_rows, thr_rows, ev_rows = [], [], []
        for r in rows:
            k = str(r[0]).strip().lower()
            if not k:
                continue
            if "throttled" in k or k == "cpu.nr_periods":
                thr_rows.append((k, _cg(r, "begin"), _cg(r, "end"), _cg(r, "delta")))
            elif k.startswith("memory.events"):
                ev_rows.append((k, _cg(r, "begin"), _cg(r, "end"), _cg(r, "delta")))
            elif k.split(".")[-1] in ("max", "limit", "effective", "cpus", "mems",
                                      "quota", "period", "current", "usage", "failcnt"):
                cfg_rows.append((k, _cg(r, "begin"), _cg(r, "end"), _cg(r, "delta")))
        if cfg_rows:
            out.append("  配置/水位（begin→end / Δ）：")
            for k, b, e, d in cfg_rows:
                out.append(f"    {k:<22} {b} → {e}   Δ={d}")
        if thr_rows:
            out.append("  cpu.stat 限流行：")
            for k, b, e, d in thr_rows:
                out.append(f"    {k:<22} {b} → {e}   Δ={d}")
        if ev_rows:
            out.append("  memory.events（v2，绝对值口径——Δ 只在单调时有效）：")
            for k, b, e, d in ev_rows:
                out.append(f"    {k:<22} {b} / {e}   Δ={d}")
        # 判读旗标（数字命中才出声）
        thr_hit = [t for t in thr_rows
                   if "nr_throttled" in t[0] or "throttled_time" in t[0] or "throttled_usec" in t[0]]
        if any((_cgnum(t[3]) or 0) > 0 for t in thr_hit):
            out.append("  ★ CPU 配额咬合：nr_throttled/throttled_time Δ>0 —— "
                       "库慢但 OS busy 不高的第一分流位（P2）")
        if any((_cgnum(e[2]) or 0) > 0 or (_cgnum(e[1]) or 0) > 0
               for e in ev_rows if e[0].endswith(("oom", "oom_kill"))):
            out.append("  ★ cgroup OOM 记录：memory.events oom/oom_kill 绝对值>0")
        if any("reset" in str(t[3]) for t in thr_rows + ev_rows):
            out.append("  计数器 reset（cgroup 重建）—— 窗内实例重启方向，对齐 db_start_time")

    # ---- NUMA 深补（2026-10-31 起）：合并表 + Auto Balancing + Placement ----
    out.append("### NUMA（拓扑+numastat 合并表 + Auto Balancing + Placement）")
    got_numa = False
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if low[:2] != ["node", "cpu list"]:
            continue
        got_numa = True
        out.append(render_table(hdr, rows[:8], max_cell=16))
        break
    if not got_numa:
        out.append("  (无 numastat 合并表 —— 老快照或无 /sys node 信息)")
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if low != ["metric", "delta"]:
            continue
        if not any("numa_" in str(r[0]) for r in rows):
            continue
        out.append("  Auto NUMA Balancing（vmstat numa_* Δ；? =单侧缺计数器）：")
        for r in rows[:6]:
            out.append(f"    {str(r[0]):<26} Δ={r[1] if len(r) > 1 else '-'}")
        break
    for hdr, rows in iter_tables(os_frag):
        low = [h.strip().lower() for h in hdr]
        if not low or low[0] != "pages" or "mapping" not in low:
            continue
        out.append("  DB Memory Placement（faulted 页 top5 + 每节点拆分）：")
        for r in rows[:5]:
            out.append("    " + " | ".join(str(c)[:44] for c in r))
        break

    # ---- Softirq/IRQ per-CPU 矩阵（2026-10-31 定形）----
    out.append("### Softirq/IRQ（对应表 Top5 by Δ + 每核饱和峰值）")
    got_si = False
    for label, first2, pct in (("Softirq", ("no.", "category"), "%sirq"),
                               ("IRQ", ("irq", "device"), "%irq")):
        for hdr, rows in iter_tables(os_frag):
            low = [h.strip().lower() for h in hdr]
            if tuple(low[:2]) != first2:
                continue
            di = low.index("delta") if "delta" in low else len(low) - 1
            entries = []
            for r in rows:
                d = num(r[di]) if di < len(r) else None
                entries.append((d or 0, r[0], r[1] if len(r) > 1 else ""))
            entries.sort(reverse=True)
            if entries:
                got_si = True
                top = "  ".join(f"{a}/{b}" for _, a, b in entries[:5])
                out.append(f"  {label} 对应表 Top5(Δ)：{top}")
            break
        for hdr, rows in iter_tables(os_frag):
            low = [h.strip().lower() for h in hdr]
            if not low or low[0] != "cpu" or pct not in low:
                continue
            ci = low.index(pct)
            vals = []
            for r in rows:
                v = num(r[ci]) if ci < len(r) else None
                if v is not None:
                    vals.append((v, str(r[0])))
            if vals:
                got_si = True
                vmax = max(vals)
                flag = "  ★ 单核饱和(ksoftirqd/irq 打满一核——RPS/亲和方向)" if vmax[0] >= 80 else ""
                out.append(f"  {label} 每核{pct}饱和峰值: {vmax[0]:.1f}% @ CPU {vmax[1]}{flag}")
            break
    if not got_si:
        out.append("  (无 Softirq/IRQ 矩阵 —— 老快照)")


def summarize_replication(secs, out):
    frag = get_sec(secs, "Replication")
    out.append("### Replication lag（per-minute 采样，已隐藏健康分钟）")
    if not frag:
        out.append("  (无 Replication 段)")
        return
    rows = []
    for hdr, body in iter_tables(frag):
        low = [h.strip().lower() for h in hdr]
        if not low or low[0] != "sample time" or "total lag" not in low:
            continue
        i = {name: low.index(name) for name in
             ("net lag", "fsync lag", "apply lag", "total lag") if name in low}
        for r in body:
            if len(r) < 2 or "no standby" in " ".join(r) or "sample error" in " ".join(r):
                continue
            rows.append((r[0], {k: num_ci(r[j]) for k, j in i.items() if j < len(r)}))
        break
    if not rows:
        out.append("  (窗内无 lag 数据行 —— 可能全程健康已折叠或无备库)")
        return
    out.append(f"  lag 行数 {len(rows)}")
    for k in ("net lag", "fsync lag", "apply lag", "total lag"):
        vals = [r[1][k] / 1e6 for r in rows if r[1].get(k) is not None]
        if not vals:
            continue
        pk = max(range(len(vals)), key=lambda i: vals[i])
        out.append(f"  {k:<10} 峰值 {vals[pk]:.2f} MB @ {rows[pk][0]}")


# ---------------------------------------------------------------- 时段多源对齐

def window_alignment(merged, asp, cs, pw, lt, log_err_times, out):
    """每个异常时段：OS 轴 × ASP 桶 × 日志P/W × 后台堆栈落窗 × pw/lt 落窗"""
    out.append("### 时段多源对齐（OS×ASP×日志×堆栈×pw/lt）")
    if not merged:
        out.append("  (无异常时段)")
        return
    buckets = []
    hdr, rows = asp.get("buckets", ([], []))
    for r in rows:
        a, b = bucket_key(r[0])
        if a:
            buckets.append((a, b, r))
    bg = cs.get("background", [])
    for i, w in enumerate(merged[:10], 1):
        out.append(f"  时段{i} {w['start']:%H:%M}~{w['end']:%H:%M} [{w.get('dominant','')}]"
                   f" {w.get('type','')}")
        # ASP（桶与时段有交集即算：单分钟尖刺落进其起始桶）
        ov = [(a, r) for a, b, r in buckets if a <= w["end"] and b > w["start"]]
        if ov:
            act = [num(r[asp["buckets"][0].index("max_active_sess")])
                   for _, r in ov if asp["buckets"][0].index("max_active_sess") < len(r)]
            act = [v for v in act if v is not None]
            waits = Counter()
            for _, r in ov:
                for ci, h in enumerate(asp["buckets"][0]):
                    if h.startswith("wait_") and ci < len(r):
                        v = num(r[ci])
                        if v:
                            waits[h] += v
            out.append(f"    ASP桶 {len(ov)} 个：active峰值 {max(act):.0f}；"
                       f"等待计数 {dict(waits) if waits else '无'}")
        else:
            out.append("    ASP桶 0 个（DB侧无覆盖或无采样）")
        # 日志
        if log_err_times:
            p = sum(1 for t in log_err_times
                    if w["start"] - timedelta(minutes=10) <= t < w["start"])
            ww = sum(1 for t in log_err_times if w["start"] <= t <= w["end"])
            out.append(f"    日志错误时刻：P(前10min)={p} 条，W(窗内)={ww} 条"
                       + ("  ← P>W 触发在前" if p > ww else ""))
        # 堆栈
        inwin = [r[1] for r in bg if r and parse_dt(r[0])
                 and w["start"] <= parse_dt(r[0]) <= w["end"]]
        out.append(f"    后台堆栈采样 {len(inwin)} 条 {dict(Counter(inwin)) if inwin else ''}")
        # pw / lt
        pwin = [p for p in pw if w["start"] <= p["ts"] <= w["end"]]
        if pwin:
            dmax = max((p["dirty"] for p in pwin if p["dirty"] is not None), default=None)
            out.append(f"    pagewriter 采样 {len(pwin)} 个，remain_dirty 峰值 "
                       f"{dmax:,.0f}" if dmax is not None else
                       f"    pagewriter 采样 {len(pwin)} 个")
        lwin = [l for l in lt if w["start"] <= l["ts"] <= w["end"]]
        if lwin:
            cmax = max(l["cnt"] for l in lwin)
            out.append(f"    长事务采样 {len(lwin)} 个，ltx60 峰值 {cmax:.0f}")


# ---------------------------------------------------------------- 日志

def parse_log_log_entries(html):
    """解析 analyze_log_v2：汇总、错误聚合、错误明细、分时段慢SQL"""
    res = {
        "summary": {}, "err_groups": [], "err_detail": [],
        "period": defaultdict(list),   # minute/10minute/hour/day -> rows
        "slow": [], "deadlock": [],
    }
    body = clean(html)
    m = re.search(r"ERROR\s*/\s*WARNING\s*/\s*FATAL[^\d]{0,12}"
                  r"([\d,]+)\s*/\s*([\d,]+)\s*/\s*([\d,]+)", body)
    if m:
        res["summary"]["级别人数"] = (f"ERROR={m.group(1)} WARNING={m.group(2)} "
                                    f"FATAL={m.group(3)}")
    m = re.search(r"错误最多时段\s*\|?\s*([^|]{1,40})", body)
    if m:
        res["summary"]["错误最多时段"] = m.group(1).strip()
    # 错误聚合表
    for m in re.finditer(r"<table\b[^>]*>(.*?)</table>", html, re.S | re.I):
        frag = m.group(1)
        rows = []
        for rm in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", frag, re.S | re.I):
            cells = [clean(c) for c in
                     re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", rm.group(1), re.S | re.I)]
            if cells and any(cells):
                rows.append(cells)
        if not rows:
            continue
        hdr = rows[0]
        low = [h.lower() for h in hdr]
        if "错误消息(归一化)" in hdr or ("级别" in hdr and "次数" in hdr and "错误消息" in "".join(hdr)):
            res["err_groups"].extend(rows[1:])
        if hdr[:3] == ["时间段", "Unique ID", "执行次数"] or (
                hdr[:2] == ["时间段", "Unique ID"]):
            res["period"]["bucket"].extend(rows[1:])
        if "Unique ID" in hdr and "Duration (s)" in hdr:
            res["slow"].extend(rows[1:])
    # 错误明细表（保留全量行）
    m = re.search(r'id="error-detail-table"[^>]*>(.*?)</table>', html, re.S | re.I)
    if m:
        for rm in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", m.group(1), re.S | re.I):
            cells = [clean(c) for c in
                     re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", rm.group(1), re.S | re.I)]
            if cells and cells[0] not in ("#", ""):
                res["err_detail"].append(cells)
    # 死锁环/锁事件章（锁/死锁详情分析——条件渲染，无死锁时是空注释）
    # 表列：排名|发生时间|会话信息(sessionid/txid)|Unique ID|SQL 锁目标|等待锁类型|
    # 锁定耗时(s)|连接标识|相关日志；其后还挂锁事件/锁链表，一并收进行池
    m = re.search(r'锁/死锁详情分析</h2>(.*?)(?:</section>|<h2)', html, re.S | re.I)
    if m:
        for tm in re.finditer(r"<table\b[^>]*>(.*?)</table>", m.group(1), re.S | re.I):
            for rm in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", tm.group(1), re.S | re.I):
                cells = [clean(c) for c in
                         re.findall(r"<t[hd]\b[^>]*>(.*?)</t[hd]>", rm.group(1), re.S | re.I)]
                if cells and any(cells) and cells[0] not in ("排名", "#", ""):
                    res["deadlock"].append(cells[:9])
    return res


def summarize_log(log, merged, out):
    out.append("### 汇总")
    for k, v in log["summary"].items():
        out.append(f"  {k}: {v}")
    out.append(f"  错误消息组: {len(log['err_groups'])} 类；错误明细行: {len(log['err_detail'])} 条；"
               f"慢SQL分桶行: {len(log['period']['bucket'])} 条")
    out.append("### 错误消息聚合（Top15）")
    if not log["err_groups"]:
        out.append("  (无)")
    for r in log["err_groups"][:15]:
        out.append("  " + " | ".join(x[:70] for x in r[:7]))
    out.append("### 错误明细（全量，最多 60 条）")
    for r in log["err_detail"][:60]:
        out.append("  " + " | ".join(x[:100] for x in r[:7]))
    out.append("### 慢SQL分桶（每分钟/10分钟，最多 120 条）")
    for r in log["period"]["bucket"][:120]:
        out.append("  " + " | ".join(x[:70] for x in r[:4]))
    out.append("### 死锁环/锁事件（logrpt 锁/死锁详情分析章；P5 直接证据）")
    dl = log.get("deadlock") or []
    if not dl:
        out.append("  (无死锁章内容 —— 窗内无死锁/锁事件，或旧版 logrpt)")
    else:
        out.append(f"  共 {len(dl)} 行（死锁环=双方 sessionid/txid+Unique ID+锁类型+耗时；含锁事件/锁链行）")
        for r in dl[:12]:
            out.append("  " + " | ".join(x[:56] for x in r))
    if not merged:
        return
    out.append("### 异常时段日志切片（P=前置10分钟，W=窗内）")
    for i, w in enumerate(merged[:6], 1):
        lo = w["start"] - timedelta(minutes=10)
        hi = w["end"]
        out.append(f"## 时段{i} P={lo:%H:%M}~{w['start']:%H:%M}  W={w['start']:%H:%M}~{w['end']:%H:%M}")
        p_e, w_e = [], []
        for r in log["err_detail"]:
            t = parse_dt(r[1]) if len(r) > 1 else None
            if not t:
                continue
            if lo <= t < w["start"]:
                p_e.append(r)
            elif w["start"] <= t <= hi:
                w_e.append(r)
        out.append(f"  前置窗错误明细 {len(p_e)} 条:")
        for r in p_e[:12]:
            out.append("    " + " | ".join(x[:90] for x in r[:7]))
        out.append(f"  窗内错误明细 {len(w_e)} 条:")
        for r in w_e[:12]:
            out.append("    " + " | ".join(x[:90] for x in r[:7]))
        # 错误组覆盖（首末时间与切片相交）
        cover = []
        for r in log["err_groups"]:
            if len(r) < 7:
                continue
            first, last = parse_dt(r[5]), parse_dt(r[6])
            if first and last and first <= hi and last >= lo:
                cover.append(r)
        out.append(f"  首末时间与切片相交的错误组 {len(cover)} 类（次数为全报告窗总数）:")
        for r in cover[:10]:
            out.append("    " + " | ".join(x[:70] for x in r[:7]))
        # 慢SQL 分桶
        slow_p, slow_w = [], []
        for r in log["period"]["bucket"]:
            t = parse_dt(r[0]) if r else None
            if not t:
                continue
            if lo <= t < w["start"]:
                slow_p.append(r)
            elif w["start"] <= t <= hi:
                slow_w.append(r)
        out.append(f"  前置窗慢SQL桶 {len(slow_p)} 行 / 窗内 {len(slow_w)} 行")
        for tag, rows in (("P", slow_p), ("W", slow_w)):
            agg = Counter()
            for r in rows:
                if len(r) >= 3:
                    agg[r[1]] += num(r[2]) or 0
            for uid, cnt in agg.most_common(5):
                out.append(f"    {tag} {uid:<12} 次数={cnt:.0f}")


# ---------------------------------------------------------------- charts.json

# 已验证分类色板（明度带/彩度/CVD 相邻对全过；浅色槽 3/4/5 对比度<3:1，图例+悬停兜底）
PALETTE = ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767']  # 暗面分类色 8 槽(dataviz 校验: 相邻CVD≥8.4/对比度≥3:1)  # 暗面分类色(dataviz 校验过: 相邻CVD≥8.4/对比度≥3:1)


def build_charts(meta, merged, series_map, memtotal, disk_meta, asp, pw, lt, path):
    """把 per-minute 序列 + 5min 桶全部落成 charts.json（报告模板直接内嵌 DATA）。"""
    panels = []

    # 标签格式：窗口跨天(>20h)时带日期，避免 %H:%M 跨午夜回卷画反 X 轴
    all_times = []
    for axis in ("cpu", "mem", "net", "tcp"):
        all_times += list(series_map.get(axis, {}).keys())
    for t in series_map.get("disk", {}):
        all_times.append(t)
    if pw:
        all_times += [p["ts"] for p in pw]
    if lt:
        all_times += [l["ts"] for l in lt]
    span_hours = ((max(all_times) - min(all_times)).total_seconds() / 3600.0
                  if all_times else 0)
    tfmt = "%m-%d %H:%M" if span_hours > 20 else "%H:%M"

    def tlabel(t):
        return t.strftime(tfmt)

    def add_panel(pid, title, gran, series):
        series = [s for s in series if s.get("data")]
        if series:
            panels.append({"id": pid, "title": title, "gran": gran, "series": series})

    def sdata(series_map_key, field, pick=None):
        out = []
        for t, items in sorted(series_map.get(series_map_key, {}).items()):
            if pick is not None:
                it = next((i for i in items if pick(i)), None)
            else:
                it = items[0] if items else None
            if it is None:
                continue
            v = it.get(field)
            if v is not None:
                out.append([tlabel(t), round(v, 3)])
        return out

    cpu = series_map.get("cpu", {})
    add_panel("os_cpu", "OS CPU（1 分钟粒度，user/sys/iowait 分解）%", "1min", [
        {"name": "user%", "color": PALETTE[0], "data": sdata("cpu", "user")},
        {"name": "sys%", "color": PALETTE[1], "data": sdata("cpu", "system")},
        {"name": "iowait%", "color": PALETTE[2], "data": sdata("cpu", "iowait")},
    ])
    add_panel("os_runq", "运行/阻塞队列（1 分钟）", "1min", [
        {"name": "r", "color": PALETTE[0], "data": sdata("cpu", "r")},
        {"name": "b", "color": PALETTE[1], "data": sdata("cpu", "b")},
    ])
    if memtotal:
        mems = []
        for t, items in sorted(series_map.get("mem", {}).items()):
            it = items[0] if items else None
            if it is not None and it.get("avail") is not None:
                mems.append([tlabel(t), round(it["avail"] / memtotal * 100, 2)])
        swp = sdata("mem", "swpout")
        add_panel("os_mem", "内存可用 % / 每秒换出页", "1min", [
            {"name": "MemAvail%", "color": PALETTE[0], "data": mems},
            {"name": "pswpout/s", "color": PALETTE[5], "data": swp},
        ])
    # 磁盘：取活跃度最高的前 4 个设备（util 已弃用，排序只看 await 和 + IOPS 和）
    disk_by_dev = defaultdict(list)
    for t, items in series_map.get("disk", {}).items():
        for it in items:
            disk_by_dev[it.get("_key", "?")].append((t, it))
    def dev_score(dev, lst):
        s = 0.0
        for _, it in lst:
            s += (it.get("wawait") or 0) + (it.get("rawait") or 0)
            s += min(((it.get("rs") or 0) + (it.get("ws") or 0)) / 100.0, 50.0)
        return s
    top_devs = sorted(disk_by_dev.items(),
                      key=lambda kv: -dev_score(kv[0], kv[1]))[:4]
    # 读写延迟 / IOPS / 吞吐：活跃度前 2 的设备，读/写各一条序列（同单位才进同一面板）
    # dm-* 映射层与某物理盘全序列相同（线性映射镜像计数）时只画物理盘一份，避免同线画两遍
    def _close(a, b):
        # 相对 0.5% 或绝对 0.5（await 抖动）双轨：dm 镜像计数与物理盘有亚毫秒/亚千分之几差异
        if a is None or b is None:
            return a is None and b is None
        return abs(a - b) <= max(0.005 * max(abs(a), abs(b)), 0.5)
    def dev_sig(lst):
        return [(t,) + tuple(it.get(k) for k in
                             ("rawait", "wawait", "rs", "ws", "rbytes", "wbytes"))
                for t, it in sorted(lst)]
    def sig_match(s1, s2):
        return (len(s1) == len(s2) and all(
            p[0] == q[0] and all(_close(a, b) for a, b in zip(p[1:], q[1:]))
            for p, q in zip(s1, s2)))
    uniq = []
    for dev, lst in top_devs:
        sig = dev_sig(lst)
        twin = next((d for d, s2, _l in uniq if sig_match(s2, sig)), None)
        if twin is None:
            uniq.append((dev, sig, lst))
        elif dev.startswith("dm-"):
            continue  # dm 别名让位于物理盘
        else:  # 物理盘后到、dm 在前：物理盘顶替 dm
            uniq = [u for u in uniq if u[0] != twin]
            uniq.append((dev, sig, lst))
    chart_devs = [(d, l) for d, _s, l in uniq[:2]]
    lat_series, iops_series, mbps_series = [], [], []
    for di, (dev, lst) in enumerate(chart_devs):
        media = media_of(dev, disk_meta)
        c = (PALETTE[di % len(PALETTE)], PALETTE[(di + 2) % len(PALETTE)])
        sr = {"name": f"{dev}[{media}] r_await(ms)", "color": c[0], "data": []}
        sw = {"name": f"{dev}[{media}] w_await(ms)", "color": c[1], "data": []}
        ir = {"name": f"{dev} r/s", "color": c[0], "data": []}
        iw = {"name": f"{dev} w/s", "color": c[1], "data": []}
        mr = {"name": f"{dev} r MB/s", "color": c[0], "data": []}
        mw = {"name": f"{dev} w MB/s", "color": c[1], "data": []}
        for t, it in sorted(lst):
            tl = tlabel(t)
            if it.get("rawait") is not None:
                sr["data"].append([tl, round(it["rawait"], 2)])
            if it.get("wawait") is not None:
                sw["data"].append([tl, round(it["wawait"], 2)])
            if it.get("rs") is not None:
                ir["data"].append([tl, round(it["rs"], 1)])
            if it.get("ws") is not None:
                iw["data"].append([tl, round(it["ws"], 1)])
            if it.get("rbytes") is not None:
                mr["data"].append([tl, round(it["rbytes"] / 1e6, 1)])
            if it.get("wbytes") is not None:
                mw["data"].append([tl, round(it["wbytes"] / 1e6, 1)])
        lat_series += [sr, sw]
        iops_series += [ir, iw]
        mbps_series += [mr, mw]
    dev_tag = ",".join(f"{d}[{media_of(d, disk_meta)}]" for d, _ in chart_devs)
    add_panel("os_disk_latency", f"磁盘读写延迟 await ms（1 分钟）— {dev_tag}", "1min", lat_series)
    add_panel("os_disk_iops", f"磁盘读写 IOPS（1 分钟）— {dev_tag}", "1min", iops_series)
    add_panel("os_disk_mbps", f"磁盘读写吞吐 MB/s（1 分钟）— {dev_tag}", "1min", mbps_series)
    netr = []
    for t, items in sorted(series_map.get("net", {}).items()):
        for it in items:
            if (it.get("rx") or 0) + (it.get("tx") or 0) > 0:
                netr.append([tlabel(t), round((it.get("rx") or 0) + (it.get("tx") or 0), 0)])
                break
    add_panel("os_net", "网络流量 RX+TX B/s（1 分钟）", "1min", [
        {"name": "rx+tx B/s", "color": PALETTE[0], "data": netr}])
    tcps, retr = [], []
    for t, items in sorted(series_map.get("tcp", {}).items()):
        it = items[0] if items else None
        if it is None:
            continue
        if it.get("estab") is not None:
            tcps.append([tlabel(t), round(it["estab"], 0)])
        if it.get("retrans") is not None:
            retr.append([tlabel(t), round(it["retrans"], 3)])
    add_panel("os_tcp", "TCP 连接数 / 重传率%", "1min", [
        {"name": "CurrEstab", "color": PALETTE[0], "data": tcps},
        {"name": "Retrans%", "color": PALETTE[5], "data": retr},
    ])

    # ---- 5min 桶 ----
    hdr, rows = asp.get("buckets", ([], []))
    if hdr:
        def bcol(name):
            try:
                return hdr.index(name)
            except ValueError:
                return None
        ts_i = 0
        act_i, avg_i = bcol("max_active_sess"), bcol("avg_sess")
        wcols = {h: bcol(h) for h in hdr if h.startswith("wait_")}
        act_d, avg_d = [], []
        wd = {h: [] for h in wcols}
        for r in rows:
            a, b = bucket_key(r[0])
            if not a:
                continue
            lab = tlabel(a)
            if act_i is not None and act_i < len(r) and num(r[act_i]) is not None:
                act_d.append([lab, num(r[act_i])])
            if avg_i is not None and avg_i < len(r) and num(r[avg_i]) is not None:
                avg_d.append([lab, num(r[avg_i])])
            for h, i in wcols.items():
                if i is not None and i < len(r) and num(r[i]):
                    wd[h].append([lab, num(r[i])])
        add_panel("asp_sess", "活跃会话（5 分钟桶，max/avg）", "5min", [
            {"name": "max_active_sess", "color": PALETTE[0], "data": act_d},
            {"name": "avg_sess", "color": PALETTE[3], "data": avg_d},
        ])
        wnames = {"wait_lock": ("行锁等待", PALETTE[5]), "wait_lwlock": ("轻锁等待", PALETTE[1]),
                  "wait_io": ("IO等待", PALETTE[2]), "wait_wal_sync": ("WAL同步等待", PALETTE[4]),
                  "wait_trans_sync": ("事务同步等待", PALETTE[6])}
        add_panel("asp_wait", "等待类会话计数（5 分钟桶峰值）", "5min", [
            {"name": wnames.get(h, (h, PALETTE[7]))[0], "color": wnames.get(h, (h, PALETTE[7]))[1],
             "data": d} for h, d in wd.items()])
    if pw:
        add_panel("pw", "Pagewriter（5 分钟）", "5min", [
            {"name": "remain_dirty 页", "color": PALETTE[0],
             "data": [[tlabel(p["ts"]), p["dirty"]] for p in pw if p["dirty"] is not None]},
            {"name": "pages/s", "color": PALETTE[1],
             "data": [[tlabel(p["ts"]), p["rate"]] for p in pw if p["rate"] is not None]},
        ])
    if lt:
        add_panel("lt", "60秒+长事务数（5 分钟）", "5min", [
            {"name": "ltx60_cnt", "color": PALETTE[4],
             "data": [[tlabel(l["ts"]), l["cnt"]] for l in lt]},
        ])

    # 事件/计数器一律原始英文名（2026-10-26 用户令：event 用英文）
    band_lbl = {"DB:lock": "wait_lock", "DB:active_sess": "active_sess 峰值",
                "DB:io": "wait_io", "DB:lwlock": "wait_lwlock",
                "DB:trans_sync": "wait_trans_sync", "DB:walsync": "wait_wal_sync",
                "net": "网络丢包", "mem": "内存换出", "cpu": "CPU 高",
                "disk": "磁盘延迟超阈", "swap": "换页"}

    def band_label(w):
        """按该时段真实触发的指标措辞：busy 硬阈值才叫"CPU 高"，
        运行队列相对基线抬升写"运行队列抬升"；mem 轴区分换出/可用低。"""
        dom = w.get("dominant", "")
        trig = set(w.get("trig", []))
        parts = []
        for x in dom.split(","):
            if x == "cpu":
                if "cpu.busy" in trig:
                    parts.append("CPU 高")
                elif "cpu.r" in trig or "cpu.b" in trig:
                    # b=阻塞队列(等IO的D态线程) 与 r=运行队列 分开措辞，不混称
                    q = (["阻塞队列抬升"] if "cpu.b" in trig else []) + \
                        (["运行队列抬升"] if "cpu.r" in trig else [])
                    parts.append("/".join(q))
                elif any(s.startswith("cpu.iowait") for s in trig):
                    parts.append("iowait 高")
                else:
                    parts.append("CPU 异常")
            elif x == "mem":
                if "mem.swpout" in trig or "mem.swpin" in trig:
                    parts.append("内存换出")
                elif "mem.avail%" in trig:
                    parts.append("内存可用低")
                else:
                    parts.append("内存异常")
            else:
                parts.append(band_lbl.get(x, x))
        return "＋".join(parts)

    bands = []
    for w in merged[:12]:
        bands.append({"from": tlabel(w["start"]), "to": tlabel(w["end"]),
                      "label": band_label(w),
                      "sev": "serious" if w.get("minutes", 1) >= 2 else "warn"})
    data = {
        "meta": {"begin": str(meta.get("Begin Snap", "")), "end": str(meta.get("End Snap", "")),
                 "ncores": None, "aas": meta.get("_aas"), "generated": datetime.now().strftime("%Y-%m-%d %H:%M")},
        "bands": bands,
        "panels": panels,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    return data


# ---------------------------------------------------------------- 主流程

CALIBER_WARNING = """[VB 口径警告（判读前必读）]
  1. gs_instance_time 的 CPU_TIME 含后台线程（pagewriter/walwriter/autovacuum...），
     不是纯前台执行时间 —— DB CPU/DB Time 只能当方向参考。
  2. 等待事件不分前台/后台：后台线程的等待也计入事件榜。
  3. %DBtime = 事件时间/DB_TIME，并行会话下可 >100%，各类相加无意义。
  4. 可靠的替代口径：AAS(=DBTime/Elapsed) 与每核比、ASP 桶逐秒采样计数、
     OS 侧 busy/iowait/r/b、per-minute 设备 await。
  5. CPU 型结论必须 OS busy+r 队列佐证；等待型结论必须 ASP 桶计数佐证。"""


def digest_awr(path, out, log_paths):
    html = open(path, encoding="utf-8", errors="ignore").read()
    out.append("=" * 78)
    out.append("一、报告元数据")
    out.append("=" * 78)
    meta = parse_meta(html, out)
    secs = split_sections(html)

    out.append("")
    out.append("=" * 78)
    out.append("二、L1 整体负载")
    out.append("=" * 78)
    elapsed = meta.get("_elapsed")
    dbtime = meta.get("_dbtime")

    # 逻辑核数
    os_frag = get_sec(secs, "OS Statistics")
    ncores = None
    phys = threads = None
    for kind, payload in iter_blocks(os_frag):
        if kind != "table":
            continue
        hdr, rows = payload
        if [h.lower() for h in hdr[:2]] != ["item", "value"]:
            continue
        kv = {r[0]: r[1] for r in rows if len(r) >= 2}
        for k, v in kv.items():
            if k.lower() == "logical cpus":
                ncores = num(v)
            elif k.lower() == "total physical cores":
                phys = num(v)
            elif k.lower() == "threads per core":
                threads = num(v)
    if not ncores and phys:
        ncores = phys * (threads or 1)
    aas = (dbtime / elapsed) if (dbtime and elapsed) else None
    out.append(f"逻辑核数 N        = {ncores if ncores else '(未取到)'}")
    out.append(f"Elapsed           = {elapsed if elapsed else '?'} s")
    out.append(f"DB Time           = {dbtime if dbtime else '?'} s")
    if aas:
        out.append(f"AAS               = {aas:.2f}")
        if ncores:
            out.append(f"每核并发(AAS/N)   = {aas/ncores:.3f}   ( >1=并发超核: 须与 ASP avg_sess 交叉校验, 差>2x 即口径冲突, 见 metrics.md )")
            out.append(f"DB资源占用(DBTime/(Elapsed*N)) = {dbtime/(elapsed*ncores)*100:.2f}%")
    # DB CPU 占比（Time Model: CPU_TIME / DB_TIME）—— 注意口径警告
    tm = get_sec(secs, "Time Model System Stats")
    dbcpu_pct = None
    for kind, payload in iter_blocks(tm):
        if kind != "table":
            continue
        hdr, rows = payload
        kv_tm = {}
        for r in rows:
            if len(r) >= 2:
                kv_tm[r[0].upper()] = num(r[1])
        if kv_tm.get("DB_TIME") and kv_tm.get("CPU_TIME") is not None:
            dbcpu_pct = kv_tm["CPU_TIME"] / kv_tm["DB_TIME"] * 100
            out.append(f"DB CPU/DB Time   = {dbcpu_pct:.2f}%  "
                       f"(CPU_TIME {kv_tm['CPU_TIME']:.2f}s / DB_TIME {kv_tm['DB_TIME']:.2f}s)"
                       f"  → {'计算型' if dbcpu_pct >= 70 else '等待型' if dbcpu_pct < 40 else '混合型'}"
                       f"（CPU_TIME 含后台线程，仅方向参考）")
    # OS 平均
    for kind, payload in iter_blocks(os_frag):
        if kind != "table":
            continue
        hdr, rows = payload
        low = [h.lower() for h in hdr]
        if low and low[0] in ("busy",) or (len(hdr) > 2 and hdr[0].lower() == "busy"):
            out.append("OS CPU 平均: " + " | ".join(f"{h}={v}" for h, v in zip(hdr, rows[0])) if rows else "")
        if low[:2] == ["time", "busy"]:
            continue
    out.append("(OS 平均见下方序列统计；L1 判定句由分析阶段撰写)")

    # ---------------- 盘体身份 ----------------
    out.append("")
    out.append("=" * 78)
    out.append("二B、盘体身份与介质阈值（Rot: 0=SSD 1=HDD）")
    out.append("=" * 78)
    disk_meta = parse_disk_meta(os_frag)
    if disk_meta:
        out.append(render_table(
            ["Device", "Size", "Type", "Rot", "Sched", "QDepth", "WC"],
            [[d, v["size"], v["type"], v["rot"] + ("=SSD" if v["rot"] == "0" else "=HDD" if v["rot"] == "1" else ""),
              v["sched"], v["qdepth"], v["wc"]] for d, v in sorted(disk_meta.items())],
            max_rows=30, max_cell=20))
        out.append("IO 判读阈值按介质分档：SSD r/w_await>5ms 异常；HDD>20ms 异常；未识别按 20ms（缺盘体身份表属数据局限）")
    else:
        out.append("(报告无 Rot/Type 身份列（旧版 vb_tool）—— IO 阈值用默认 20ms，介质未知需人工确认)")

    # ---------------- OS 时间序列 ----------------
    out.append("")
    out.append("=" * 78)
    out.append("三、OS 1分钟时间序列与异常时段（L2-a）")
    out.append("=" * 78)
    tables = [(p[0], p[1]) for k, p in iter_blocks(os_frag) if k == "table"]
    series_map = {}
    for axis, spec in SERIES_SPECS.items():
        s = series_from_tables(tables, spec)
        if s:
            series_map[axis] = s
    # MemTotal（OS Memory Usage 表 Begin MB 列）
    memtotal = None
    for hdr, rows in tables:
        if [h.lower() for h in hdr[:2]] == ["metric", "begin mb"]:
            for r in rows:
                if r and r[0].lower() == "memtotal":
                    memtotal = num(r[1])
    if memtotal:
        out.append(f"MemTotal = {memtotal:.0f} MB")
    merged, flags = detect_os_windows(series_map, ncores or 1, memtotal, disk_meta, out)
    window_minute_detail(merged, series_map, ncores or 1, out)

    # ---------------- Load Profile ----------------
    out.append("")
    out.append("=" * 78)
    out.append("四、Load Profile 与派生指标")
    out.append("=" * 78)
    lp = get_sec(secs, "Load Profile")
    kv = {}
    for kind, payload in iter_blocks(lp):
        if kind != "table":
            continue
        hdr, rows = payload
        if hdr and hdr[0] == "Metric":
            out.append(render_table(hdr, rows, max_rows=40, max_cell=30))
            for r in rows:
                if len(r) >= 5:
                    kv[r[0]] = r[4]
    def gl(*names):
        for n in names:
            if n in kv:
                return kv[n]
        return None
    lr, lrb = parse_num(gl("logical reads"))
    pr, prb = parse_num(gl("physical reads"))
    temp, tempb = parse_num(gl("temp bytes"))
    wal, walb = parse_num(gl("WAL written", "wal generated"))
    cm, _ = parse_num(gl("commits"))
    rb, _ = parse_num(gl("rollbacks"))
    if lr is not None and pr is not None and lr:
        out.append(f"物理读/逻辑读 = {pr/lr*100:.3f}%  (物理 {pr:,.0f}/s, 逻辑 {lr:,.0f}/s)")
    temp_raw = gl("temp bytes", "temp written", "temp spill")
    wal_raw = gl("WAL written", "wal generated", "WAL generated")
    if temp_raw:
        out.append(f"temp 写入 = {temp_raw}/s")
    if wal_raw:
        out.append(f"WAL 生成 = {wal_raw}/s")
    if cm and rb is not None:
        out.append(f"commit/rollback = {cm:,.0f}/{rb:,.0f} 每秒，回滚占比 {rb/(cm+rb)*100:.2f}%")
    notes = []
    if aas and ncores:
        percore = aas / ncores
        if percore > 1:
            notes.append(f"每核并发={percore:.2f}>1 并发超核（★不等于 CPU 供不应求：须核 DB CPU/DB Time 与 ASP avg_sess）")
        elif percore > 0.5:
            notes.append(f"每核并发={percore:.2f} 接近饱和")
        else:
            notes.append(f"每核并发={percore:.2f} 有余量（机器未饱和）")
        if dbcpu_pct is not None and dbcpu_pct >= 70:
            notes.append(f"DB CPU/DB Time={dbcpu_pct:.1f}% 计算型（瓶颈在 CPU 执行而非等待）")
    if lr and pr is not None and pr/lr > 0.05:
        notes.append(f"物理读占比 {pr/lr*100:.2f}%>5%")
    if cm and rb is not None and rb/(cm+rb) > 0.05:
        notes.append(f"回滚占比 {rb/(cm+rb)*100:.2f}%>5%")
    if tempb and any(u in tempb for u in ("M", "G")):
        notes.append(f"temp 写入 {tempb}/s 存在落盘")
    if walb and "G" in walb:
        notes.append(f"WAL {walb}/s 写入风暴")
    out.append("自动标注: " + ("; ".join(notes) if notes else "(无)"))

    # ---------------- Top Event ----------------
    out.append("")
    out.append("=" * 78)
    out.append("五、Top Event 事件谱（L3-a）")
    out.append("=" * 78)
    we = parse_wait_events(get_sec(secs, "Top 10 Wait Events by Total Wait Time"))
    summarize_events(we, merged, out)

    # ---------------- ASP ----------------
    out.append("")
    out.append("=" * 78)
    out.append("六、Active Session History（L2-b）")
    out.append("=" * 78)
    asp = parse_asp(get_sec(secs, "Active Session History"))
    asp_windows = detect_asp_windows(asp, aas or 1, out)
    # 合并 OS 时段 + DB 侧时段（去重按起止时间）
    seen = {(w["start"], w["end"]) for w in merged}
    for w in asp_windows:
        if (w["start"], w["end"]) not in seen:
            merged.append(w)
            seen.add((w["start"], w["end"]))
    merged.sort(key=lambda w: w["start"])
    summarize_asp(asp, merged, out)

    # ---------------- Callstack / pw / lt ----------------
    out.append("")
    out.append("=" * 78)
    out.append("七、Callstack 堆栈指纹 + Pagewriter + 长事务（L3-c）")
    out.append("=" * 78)
    cs = parse_callstack(get_sec(secs, "Callstack Sampling"))
    summarize_callstack(cs, merged, out)
    pw = parse_pagewriter(get_sec(secs, "Pagewriter Status"))
    lt = parse_longtxn(get_sec(secs, "Long Transaction Monitor"))
    summarize_pw_lt(pw, lt, merged, out)

    # ---------------- SQL ----------------
    out.append("")
    out.append("=" * 78)
    out.append("八、SQL Statistics 维度与跨维矩阵（L5）")
    out.append("=" * 78)
    dims = parse_sql_stats(get_sec(secs, "SQL Statistics"))
    sqltexts = parse_sql_text(get_sec(secs, "SQL Text"))
    summarize_sql(dims, sqltexts, out)

    # ---------------- 参数 ----------------
    out.append("")
    out.append("=" * 78)
    out.append("九、参数（GUC + sysctl；现象→参数对照见 performance/parameters.md）")
    out.append("=" * 78)
    base = os.path.splitext(path)[0]
    guc, sysctl_d = parse_params(secs, out, base)
    try:
        std_check(meta, guc, out)
    except Exception as e:  # 基线对照失败不阻断提取
        out.append(f"  （标准化对照异常：{e}）")

    # ---------------- OS 附属段 ----------------
    out.append("")
    out.append("=" * 78)
    out.append("十、OS 附属证据（dmesg / 文件系统 / RTT / 复制延迟 / OS 新节：折表+TCP+Cgroup+NUMA+Softirq/IRQ）")
    out.append("=" * 78)
    summarize_dmesg(os_frag, out)
    summarize_fs(os_frag, out)
    summarize_rtt(os_frag, out)
    summarize_os_new(os_frag, out)
    summarize_replication(secs, out)

    # ---------------- 其他章节 ----------------
    out.append("")
    out.append("=" * 78)
    out.append("十一、其他章节原始数据")
    out.append("=" * 78)
    for title in ("Time Model System Stats", "Cache Hit Ratio", "Database Memory",
                  "Replication", "Long Transaction Monitor", "Object Statistics"):
        frag = get_sec(secs, title)
        if not frag:
            continue
        out.append(f"## {title}")
        n = 0
        for kind, payload in iter_blocks(frag):
            if kind != "table":
                continue
            hdr, rows = payload
            out.append(render_table(hdr, rows, max_rows=20, max_cell=34))
            n += 1
            if n >= 6:
                break

    # ---------------- 日志 ----------------
    log_err_times = []
    if log_paths:
        out.append("")
        out.append("=" * 78)
        out.append("十二、日志分析（analyze_log_v2 = pg_log 扫描结果，L2-c）")
        out.append("=" * 78)
        for lpth in log_paths:
            log_html = open(lpth, encoding="utf-8", errors="ignore").read()
            out.append(f"## 日志源: {os.path.basename(lpth)}")
            log = parse_log_log_entries(log_html)
            summarize_log(log, merged, out)
            for r in log["err_detail"]:
                t = parse_dt(r[1]) if len(r) > 1 else None
                if t:
                    log_err_times.append(t)

    # ---------------- 时段多源对齐 ----------------
    out.append("")
    out.append("=" * 78)
    out.append("十三、时段多源对齐（异常时间 × ASP × 日志 × 后台堆栈 × pw/lt）")
    out.append("=" * 78)
    window_alignment(merged, asp, cs, pw, lt, log_err_times, out)

    out.append("")
    out.append("[提取说明] 数值必须引用本 digest；表格已截断；堆栈已指纹化；"
               "异常时段由硬阈值+相对基线生成（磁盘阈值按 Rot 介质分档），"
               "STATUS 类事件不计入等待瓶颈。趋势图数据在 _charts.json。")
    return meta, merged, series_map, memtotal, disk_meta, asp, pw, lt


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    files = sys.argv[1:]
    for f in files:
        if not os.path.exists(f):
            sys.exit(f"file not found: {f}")
    out = []
    out.append(f"AWR DIGEST v4  来源: {files}")
    out.append("(extract.py v4：元数据→L1负载→盘体身份→OS时间轴→LoadProfile→TopEvent→"
               "ASP→堆栈/pw/lt→SQL维度→参数→OS附属→日志→多源对齐)")
    out.append("")
    out.append(CALIBER_WARNING)
    out.append("")
    res = digest_awr(files[0], out, files[1:])
    meta, merged, series_map, memtotal, disk_meta, asp, pw, lt = res
    base = os.path.splitext(files[0])[0]
    open(base + "_digest.txt", "w", encoding="utf-8").write("\n".join(out) + "\n")
    charts = build_charts(meta, merged, series_map, memtotal, disk_meta, asp, pw, lt,
                          base + "_charts.json")
    npts = sum(len(s["data"]) for p in charts["panels"] for s in p["series"])
    print(f"digest  -> {base}_digest.txt  ({len(out)} 行)")
    print(f"charts  -> {base}_charts.json  ({len(charts['panels'])} 面板, {npts} 数据点)")
    print(f"params  -> {base}_params.txt")


if __name__ == "__main__":
    main()
