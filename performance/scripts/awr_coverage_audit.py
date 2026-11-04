#!/usr/bin/env python3
"""AWR 全列覆盖审计：dump 报告每一章每一表的列名，与技能域知识文件逐列对账。

用途：判"有没有/缺不缺/是不是"之前先跑这个——全量提取，不从记忆、不从半截读表。
产出两类清单：
  ① 已采未判读：列名在报告里有、但在 reference.md + metrics.md 里没被提及 → 判读缺口
  ② 判读已备：列名在报告里有、知识文件也提了 → 覆盖到位（仅计数）
用法: awr_coverage_audit.py <awrrpt.html> [skill_root]
默认 skill_root = 本脚本上两级（vastbase/performance/scripts -> vastbase）
"""
import re
import sys
import html as H
from pathlib import Path


def visible(s):
    return H.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def dump_columns(src):
    """章节 → 表 → 表头列名 的全量结构（表头 <th> 为准，无 <th> 的表记空表）。"""
    chapters = []
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", src, re.S):
        title = visible(m.group(1)).replace(" ↑ Top", "")
        chapters.append([title, []])
    if not chapters:
        return chapters
    # 按 h2 分段，把表头归到所属章
    parts = re.split(r"<h2[^>]*>", src)
    for idx, body in enumerate(parts[1:]):
        tbls = chapters[idx][1]
        for rm in re.finditer(r"<table[^>]*>(.*?)</table>", body, re.S):
            ths = [visible(t) for t in re.findall(r"<th[^>]*>(.*?)</th>", rm.group(1), re.S)]
            tbls.append([t for t in ths if t])
    return chapters


def skill_text(skill_root):
    ref = Path(skill_root) / "performance/awr-report-reference.md"
    met = Path(skill_root) / "performance/metrics.md"
    return "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in (ref, met) if p.exists())


def norm_col(c):
    """列名归一：去掉 Δ/(Begin)/(End) 等修饰，取核心词。"""
    c = re.sub(r"\((Begin|End|Δ|used, max)\)", "", c)
    c = c.replace(" per Sec", "/s").replace(" Per Second", "/s")
    return c.strip()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    report = sys.argv[1]
    root = sys.argv[2] if len(sys.argv) > 2 else str(Path(__file__).resolve().parents[2])
    src = Path(report).read_text(encoding="utf-8", errors="replace")
    kb = skill_text(root)

    chapters = dump_columns(src)
    allcols = set()
    for _, tbls in chapters:
        for ths in tbls:
            for t in ths:
                allcols.add(norm_col(t))

    # 列名是否在知识文件出现（按核心词，忽略长度 1 的噪声）
    covered, uncovered = [], []
    for c in sorted(allcols):
        if len(c) < 2 or c in ("#", "↑ Top", "Total", "Per Second"):
            continue
        if c.lower() in kb.lower():
            covered.append(c)
        else:
            uncovered.append(c)

    print(f"报告: {Path(report).name}")
    print(f"章节 {len(chapters)} 个；去重列名 {len(allcols)} 个")
    print(f"\n=== ① 已采未判读（知识文件没提的列，共 {len(uncovered)}） ===")
    for c in uncovered:
        print(f"  {c}")
    print(f"\n=== ② 判读已备（知识文件已提，共 {len(covered)}） ===")
    for c in covered:
        print(f"  {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
