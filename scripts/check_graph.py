#!/usr/bin/env python3
"""知识网校验：断链 / 登记缺口 / 孤儿。

兑现 KNOWLEDGE-GRAPH.md 维护规则 3 的承诺。域根运行：

    python3 scripts/check_graph.py

三类检查（发现即打印，退出码 1；全绿退出码 0）：
  1. 断链      —— 所有 .md 引用（带路径或裸文件名）指向的文件必须存在
  2. 登记缺口  —— 每个内容文件必须在 SKILL.md（路由表）与 KNOWLEDGE-GRAPH.md
                  （节点清单，短名前缀匹配）各登记一次
  3. 孤儿      —— 内容文件未被 SKILL/知识网/任何其他内容文件引用（含裸名）
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT_DIRS = ("performance", "internals", "admin", "linux",
                "troubleshooting", "ha", "backup-recovery", "cases", "sql")
META_FILES = ("SKILL.md", "README.md", "SKILL_AUTHORING_GUIDE.md",
              "KNOWLEDGE-GRAPH.md", "ROADMAP.md")
REF_RE = re.compile(r"[A-Za-z0-9_./-]+\.md")


def load():
    content = {}          # "dir/name.md" -> path（README 计划清单不算内容）
    for d in CONTENT_DIRS:
        base = ROOT / d
        if not base.is_dir():
            continue
        for p in sorted(base.glob("*.md")):
            if p.name not in ("README.md", "INDEX.md"):
                content[f"{d}/{p.name}"] = p
    meta = {n: ROOT / n for n in META_FILES if (ROOT / n).is_file()}
    return content, meta


def resolve(ref, content, meta):
    """把引用解析成域内文件；返回 ('content'|'meta', key) 或 None。"""
    ref = ref.lstrip("./")
    while ref.startswith("../"):
        ref = ref[3:]
    if ref in meta:
        return ("meta", ref)
    if ref in content:
        return ("content", ref)
    base = ref.rsplit("/", 1)[-1]          # 裸文件名（无目录或目录写法不同）
    hits = [k for k in content if k.rsplit("/", 1)[-1] == base]
    if len(hits) == 1:
        return ("content", hits[0])
    if len(hits) > 1:
        return ("ambiguous", base)
    if (ROOT / ref).is_file():             # 目录 README 等非内容文件：按存在性放行
        return ("readme", ref)
    return None


def main():
    content, meta = load()
    texts = {k: p.read_text(encoding="utf-8") for k, p in {**meta, **content}.items()}
    idx_path = ROOT / "cases" / "INDEX.md"
    if idx_path.is_file():
        texts["cases/INDEX.md"] = idx_path.read_text(encoding="utf-8")
    skill_txt = texts.get("SKILL.md", "")
    graph_txt = texts.get("KNOWLEDGE-GRAPH.md", "")
    findings = []

    # 1. 断链：只查像域内引用的 token（裸名或含内容目录名）
    for src, txt in texts.items():
        for ref in REF_RE.findall(txt):
            if "/" in ref and not any(f"/{d}/" in f"/{ref}" or ref.startswith(d + "/")
                                      for d in CONTENT_DIRS):
                continue                     # 外部路径引用，不管
            r = resolve(ref, content, meta)
            if r is None:
                findings.append(f"[断链] {src} -> {ref}")
            elif r[0] == "ambiguous":
                findings.append(f"[歧义] {src} -> {ref}（多文件同名，需写全路径）")

    # 2. 登记缺口：cases/ 登记 INDEX.md；其余登记 SKILL 路由 + 知识网节点
    index_txt = (ROOT / "cases" / "INDEX.md").read_text(encoding="utf-8") \
        if (ROOT / "cases" / "INDEX.md").is_file() else ""
    for rel in content:
        if rel.startswith("cases/"):
            base = rel.rsplit("/", 1)[-1]
            if rel not in index_txt and base not in index_txt:
                findings.append(f"[登记缺口] {rel} 未出现在 cases/INDEX.md")
            continue
        if rel not in skill_txt:
            findings.append(f"[登记缺口] {rel} 未出现在 SKILL.md 路由")
        stem = rel.rsplit("/", 1)[-1][:-3]
        short = "-".join(stem.split("-")[:2])   # commit-sync / error-fatal / core-events
        if stem not in graph_txt and short not in graph_txt:
            findings.append(f"[登记缺口] {rel} 未出现在 KNOWLEDGE-GRAPH.md")

    # 4. 图结构检查（2026-10-25 逻辑令：有理有据、走得通）
    #    4a. 节点清单声称的层数量 vs 目录实际文件数
    by_dir = {}
    for rel in content:
        by_dir[rel.split("/")[0]] = by_dir.get(rel.split("/")[0], 0) + 1
    for m in re.finditer(r"（([a-z\-]+)/，(\d+)", graph_txt):
        layer_dir, claimed_n = m.group(1), int(m.group(2))
        if layer_dir in by_dir and by_dir[layer_dir] != claimed_n:
            findings.append(
                f"[图结构] {layer_dir} 声称 {claimed_n} 个文件，实际 {by_dir[layer_dir]}")
    #    4b. 图中带连字符的名字必须落在真实文件/目录上（防边指向不存在的节点）
    #        约定短名（如 commit-sync → commit-sync-events）按"唯一前缀"等价放行
    stems = {Path(k).stem for k in content}
    names_ok = stems | set(CONTENT_DIRS) | {"SKILL", "ROADMAP"}

    def node_exists(tok):
        if tok in names_ok:
            return True
        hits = [s for s in stems if s.startswith(tok + "-")]
        return len(hits) == 1

    for line in graph_txt.splitlines():
        if "→" not in line and "↔" not in line:
            continue
        for tok in re.findall(r"\b[a-z][a-z0-9]+(?:-[a-z0-9]+)+\b", line):
            if not node_exists(tok):
                findings.append(f"[图结构] 节点名不存在: {tok}（行: {line.strip()[:50]}…）")

    # 3. 孤儿：SKILL / 知识网 / 其他内容文件 都没引用它
    others = {k: v for k, v in texts.items()}
    for rel in content:
        stem = rel.rsplit("/", 1)[-1][:-3]
        referenced = False
        for src, txt in others.items():
            if src == rel:
                continue
            if rel in txt or stem + ".md" in txt:
                referenced = True
                break
        if not referenced:
            findings.append(f"[孤儿] {rel} 未被任何文件/路由/知识网引用")

    n = len(content)
    if findings:
        print(f"FAIL：{len(findings)} 项发现（内容文件 {n} 个）")
        for f in findings:
            print("  " + f)
        sys.exit(1)
    print(f"PASS：断链 0 / 登记缺口 0 / 孤儿 0（内容文件 {n} 个，"
          f"元文件 {len(meta)} 个）")


if __name__ == "__main__":
    main()
