#!/usr/bin/env python3
"""vastbase performance 交付前三查：_analyze.html 报告的自评扫描（HEval 式）

参照 DBAIOps(arXiv 2508.01136, 用户提供)的人评三标准——根因召回 / 理论一致 /
证据保真——把其中可机械化的部分做成脚本，模型交付报告前必跑：

  1) 证据保真(硬边界): 正文与表格里的数值 token 逐个回查 digest/charts，
     定位不到的列 UNTRACEABLE——逐条处置(衍生值补算式/改引原值/编造即重写)
  2) 根因闭环: 健康总览表每个 ■/▲ 行，其指标名必须在正文问题卡再次出现
     (表里亮了正文不接 = 召回缺口)
  3) 渲染要素: const DATA 存在、无未替换占位符、正文无 NaN/undefined、
     无禁用词（线程池/池化——不稳定特性永不进建议；churn 等英文行话换中文）

用法: selfcheck.py <report_analyze.html> <digest.txt> <charts.json>
退出码: 硬失败(3)任一命中 = 1；只有 WARN = 0。
UNTRACEABLE 是"回查提示"不是判决——衍生计算值(如倍数、差值)只要算式在
正文可核就合法，脚本只负责把每个孤立的数字抓出来对账。
"""
import html as H
import json
import os
import re
import sys


def norm(s):
    """对账用归一化：去千分位逗号，保留数字与小数点"""
    return s.replace(",", "")


def visible_text(src):
    """去掉 script/style 与标签，留可见文本(含表格)"""
    t = re.sub(r"<script\b.*?</script>", " ", src, flags=re.S | re.I)
    t = re.sub(r"<style\b.*?</style>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return H.unescape(t)


def load_haystack(digest_path, charts_path):
    parts = []
    with open(digest_path, encoding="utf-8", errors="replace") as f:
        parts.append(norm(f.read()))
    if charts_path and os.path.exists(charts_path):
        with open(charts_path, encoding="utf-8", errors="replace") as f:
            parts.append(norm(f.read()))
    return "".join(parts)


# ---------------------------------------------------------------- 1) 证据保真

# 不参与对账的结构性数字（形态编号/日期/时刻/百分比号/十六进制/序号）
SKIP_PATTERNS = [
    r"P\d{1,2}\b",              # P1~P12 形态编号
    r"\d{4}-\d{2}-\d{2}",        # 日期
    r"\d{1,2}:\d{2}(:\d{2})?",   # 时刻
    r"\d{14}",                   # 快照号(YYYYMMDDHHMMSS)
    r"0x[0-9a-fA-F]+",           # 十六进制
    r"[\w.]+\.\w+:\d+",          # 源码 file:line（如 instr_unique_sql.cpp:2616）
    r"\(:\d+\)",                 # 裸行号引用（如 CleanupAllUniqueSqlEntry(:2328)）
    r"dn_\d+_\d+",               # 节点名 dn_6001_6002
]
NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def check_numbers(body_text, haystack):
    """正文数值 token 逐个回查；返回 (traceable, untraceable) 计数与清单"""
    masked = body_text
    for p in SKIP_PATTERNS:
        masked = re.sub(p, " ", masked)
    # “N 倍”“N×”这类相对量与“第 N/表 N”序号不查
    masked = re.sub(r"第\s*\d+|表\s*\d+|\d+\s*[×x]\s*|\d+\s*倍", " ", masked)

    untraceable = []
    total = 0
    seen = set()
    # digest/charts 全量数字串（前缀对账预提取一次）
    digest_nums = {norm(m.group(0)) for m in NUM_RE.finditer(haystack)}
    for m in NUM_RE.finditer(masked):
        raw = m.group(0)
        val = norm(raw)
        if len(val.lstrip("0.").rstrip(".")) < 2:  # 单位数(0~9)噪声太大，跳过
            continue
        total += 1
        if val in haystack or raw in haystack:
            continue
        # 展示截断可对账（报告 8ms / digest 8.03、89.4 / 89.448）：数值串须是
        # digest 某个完整数字的前缀（只丢小数尾）；四舍五入改写值
        # （509.753→509.8、1.68→1.7）不是前缀必被逮。
        # 2026-11-04 收紧：原"整数部分子串命中"让改写值全部漏过
        if any(n.startswith(val) for n in digest_nums):
            continue
        if val in seen:
            continue
        seen.add(val)
        ctx = masked[max(0, m.start() - 30):m.end() + 20].strip()
        untraceable.append((val, re.sub(r"\s+", " ", ctx)))
    return total, untraceable


# ---------------------------------------------------------------- 2) 根因闭环

def split_overview(src):
    """健康总览表(含 ■/▲ 的 <tr>)与正文分开。只剥总览行本身——
    正文问题卡里的证据表也是 <tr>，全剥会把真正的复现误判为未闭环"""
    rows = []

    def _keep(m):
        tr = m.group(0)
        if ("■" in tr or "▲" in tr) and "<td" in tr.lower():
            rows.append(re.sub(r"\s+", " ", visible_text(tr)).strip())
            return " "
        return tr  # 非总览行(证据表等)原样保留

    rest = visible_text(re.sub(r"<tr\b.*?</tr>", _keep, src, flags=re.S | re.I))
    return rows, rest


def row_keywords(row_text):
    r"""行文本 → 复现检查候选词(指标名 + 行内数值)。P\d 形态编号每张问题卡
    都会出现，拿它当候选等于不查，剔除；正文有数值或指标名复现即算闭环"""
    t = re.sub(r"[■▲●◆]|严重|需要处理|留意", " ", row_text)
    words = [w for w in re.split(r"[\s/|、,，;；()（）]+", t)
             if len(w) >= 2 and not re.fullmatch(r"P\d+", w)]
    nums = [m.group(0) for m in re.finditer(r"\d+\.\d+|\d{2,}", t)]
    words.sort(key=len, reverse=True)
    return words[:4] + nums[:3]


# ---------------------------------------------------------------- 3) 渲染要素

def check_render(src):
    fails = []
    if "const DATA" not in src:
        fails.append("缺 `const DATA`（模板渲染数据块被删，图全空）")
    if re.search(r"\{\{|\}\}|__+", src):
        fails.append("存在未替换的模板占位符")
    body = visible_text(src)
    if re.search(r"\bNaN\b|\bundefined\b", body):
        fails.append("正文出现 NaN/undefined")
    # 对外语与特性红线：线程池是不稳定特性，分析/建议里永不出现(用户令)；英文行话不进客户报告
    banned = re.findall(r"线程池|thread_pool|池化|churn", src, flags=re.I)
    if banned:
        fails.append(f"出现禁用词 {sorted(set(banned))}（线程池系不稳定特性永不建议；英文行话换中文）")
    return fails


# ---------------------------------------------------------------- main

def main():
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    report, digest = sys.argv[1], sys.argv[2]
    charts = sys.argv[3] if len(sys.argv) > 3 else ""
    with open(report, encoding="utf-8", errors="replace") as f:
        src = f.read()
    haystack = load_haystack(digest, charts)

    hard_fail, warns = [], []

    # 3) 渲染要素(硬失败)
    hard_fail += check_render(src)

    # 1) 证据保真
    body = visible_text(src)
    total, untraceable = check_numbers(body, haystack)
    for val, ctx in untraceable:
        warns.append(f"[证据] 数值 {val} 在 digest/charts 定位不到 | 上下文: …{ctx}…")

    # 2) 根因闭环
    rows, rest = split_overview(src)
    unclosed = 0
    for row in rows:
        kws = row_keywords(row)
        if kws and not any(k in rest for k in kws):
            unclosed += 1
            warns.append(f"[闭环] 总览行未在正文复现: {row[:80]}")

    print(f"三查报告: {os.path.basename(report)}")
    print(f"  渲染要素: {'FAIL: ' + '; '.join(hard_fail) if hard_fail else 'PASS'}")
    print(f"  证据保真: 正文数值 token {total} 个，定位不到 {len(untraceable)} 个")
    print(f"  根因闭环: 总览 ■/▲ 行 {len(rows)} 行，未闭环 {unclosed} 行")
    if hard_fail:
        print("结论: 硬失败，修复后再交付")
    else:
        print(f"结论: {'PASS' if not warns else f'PASS with {len(warns)} 条 WARN'}"
              f"{'——逐条处置' if warns else ''}")
    for w in warns:
        print("  - " + w)
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())
