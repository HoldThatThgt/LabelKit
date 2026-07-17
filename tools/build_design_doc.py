#!/usr/bin/env python3
"""docs/design/ 设计文档构建脚本（stdlib-only，确定性）。

把 spec/*.md（v1.9 现行设计规格，实现级、与代码同步）汇编为单文件自包含
HTML 设计说明书 docs/design/labelkit-design-v1.html：

  封面（版本/修订历史取自 spec/00-frontmatter.md）→ 目录（h1/h2 自动生成）
  → 正文（ch1–ch9 + 模块 3.1–3.16 + 附录 A，Markdown 子集确定性转换）
  → 插图（tools/design_figures/fig-*.svg 按「图 N-N」标题行内联注入）。

样式沿用 v1.4 原版 CSS（tools/design_figures/_style.css）。PDF 由 Chrome
headless 从该 HTML 打印（见本文件 --pdf 说明）。

用法：
  uv run python tools/build_design_doc.py            # 生成 HTML
  uv run python tools/build_design_doc.py --pdf      # 生成 HTML + PDF（需 Chrome）
"""
from __future__ import annotations

import html as _html
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "spec"
FIG_DIR = ROOT / "tools" / "design_figures"
OUT_HTML = ROOT / "docs" / "design" / "labelkit-design-v1.html"
OUT_PDF = ROOT / "docs" / "design" / "labelkit-design-v1.pdf"

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 章节装配顺序（00-frontmatter 单独消费为封面）。
ORDER = [
    "10-ch1-overview", "20-ch2-overall-design", "30-ch3-modules-intro",
    "301-m1-config", "302-m2-ingest", "303-m3-dedup",
    "304-m4-qualityqurating", "305-m5-annotate", "306-m6-generate",
    "307-m7-verify", "308-m8-schema-engine", "309-m9-llm-client",
    "310-m10-orchestrator", "311-m11-emitter", "312-m12-logging",
    "313-m13-classify", "314-m14-segment", "315-m15-extract",
    "316-m16-stitch",
    "40-ch4-data-structures", "50-ch5-config-spec", "60-ch6-io-formats",
    "70-ch7-logging", "80-ch8-nongoals-roadmap", "85-ch9-references",
    "90-appendix-a-rubrics",
]

# 特殊段落 → 样式盒（v1.4 原版的三类盒子）。
_BOUNDARY_HEADS = ("**做：**", "**不做：**", "**工具级边界：**")
_BACKING_HEADS = ("**背书：**",)
_NOTE_HEADS = ("**提示：**", "**关键限制", "**风险明示：**",
               "**与「工具不存储数据」原则的关系：**")

_FIG_RE = re.compile(r"^图 (\d+-\d+)\s")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

_injected_figs: set[str] = set()


def esc(text: str) -> str:
    return _html.escape(text, quote=False)


def inline(text: str) -> str:
    """行内 Markdown 子集：HTML 转义 → `code` → **bold**。"""
    # 特例：308 的「````json`」（反引号围栏字面量）——整体占位，避免其
    # 内部反引号参与 code span 配对。
    text = text.replace("````json`", "\x02")
    out: list[str] = []
    for i, seg in enumerate(_INLINE_CODE_RE.split(esc(text))):
        if i % 2 == 1:                      # code span（转义后内容原样保留）
            out.append(f"<code>{seg}</code>")
        else:
            out.append(seg)
    joined = "".join(out)
    joined = _BOLD_RE.sub(r"<b>\1</b>", joined)
    return joined.replace("\x02", "<code>```json</code>")


def split_row(line: str) -> list[str]:
    """表格行按未转义 | 切分（\\| 与 code span 内的 | 为字面量管道）。"""
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    # code span 内的管道先遮蔽（`fn(a | b)` 之类的签名单元格）。
    line = _INLINE_CODE_RE.sub(lambda m: "`" + m.group(1).replace("|", "\x01") + "`",
                               line)
    cells = re.split(r"(?<!\\)\|", line)
    return [c.replace("\\|", "|").replace("\x01", "|").strip() for c in cells]


def heading_ids(text: str, level: int) -> str | None:
    """锚点：h1 → s1..s9/sA；h2 → s{c}-{n} / m{n}（模块）/ sA-{n}。"""
    if level == 1:
        m = re.match(r"(\d+)\.", text)
        if m:
            return f"s{m.group(1)}"
        if text.startswith("附录 A"):
            return "sA"
    if level == 2:
        m = re.match(r"3\.\d+ M(\d+)\b", text)
        if m:
            return f"m{m.group(1)}"
        m = re.match(r"(\d+)\.(\d+)\s", text)
        if m:
            return f"s{m.group(1)}-{m.group(2)}"
        m = re.match(r"A\.(\d+)\s", text)
        if m:
            return f"sA-{m.group(1)}"
    return None


def para_class(raw: str) -> str | None:
    if raw.startswith(_BOUNDARY_HEADS):
        return "boundary"
    if raw.startswith(_BACKING_HEADS):
        return "backing"
    if raw.startswith(_NOTE_HEADS):
        return "note"
    return None


def figure_html(num: str, caption: str) -> str:
    svg_path = FIG_DIR / f"fig-{num}.svg"
    if not svg_path.exists():
        raise SystemExit(f"missing figure svg: {svg_path}")
    _injected_figs.add(num)
    return (f"<figure>{svg_path.read_text().strip()}"
            f"<figcaption>{inline(caption)}</figcaption></figure>")


def convert(md: str, *, ref_list: bool = False,
            headings: list[tuple[int, str, str | None]] | None = None) -> str:
    """Markdown 子集 → HTML。headings 非 None 时收集 (level, text, id)。"""
    out: list[str] = []
    lines = md.split("\n")
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        # ── 代码围栏 ───────────────────────────────────────────────
        if stripped.startswith("```"):
            body: list[str] = []
            i += 1
            while i < n:
                cur = lines[i]
                if cur.strip() == "```":
                    # 唯一嵌套特例：紧邻两条裸围栏 ⇒ 前者是内容、后者收栏。
                    if i + 1 < n and lines[i + 1].strip() == "```":
                        body.append(cur)
                        i += 2
                    else:
                        i += 1
                    break
                body.append(cur)
                i += 1
            out.append(f"<pre><code>{esc(chr(10).join(body))}</code></pre>")
            continue
        # ── 空行 ──────────────────────────────────────────────────
        if not stripped:
            i += 1
            continue
        # ── 标题 ──────────────────────────────────────────────────
        m = re.match(r"^(#{1,4}) (.+)$", line)
        if m:
            level = len(m.group(1))
            text = m.group(2).strip()
            hid = heading_ids(text, level)
            if headings is not None and level <= 2:
                headings.append((level, text, hid))
            id_attr = f' id="{hid}"' if hid else ""
            out.append(f"<h{level}{id_attr}>{inline(text)}</h{level}>")
            i += 1
            continue
        # ── 表格 ──────────────────────────────────────────────────
        if stripped.startswith("|") and i + 1 < n and \
                re.match(r"^\s*\|[\s:|-]+\|?\s*$", lines[i + 1]):
            header = split_row(lines[i])
            i += 2
            rows: list[list[str]] = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            t = ["<table>", "<tr>" + "".join(f"<th>{inline(c)}</th>" for c in header) + "</tr>"]
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
            t.append("</table>")
            out.append("".join(t))
            continue
        # ── 列表（无序 / 有序，单层，支持折行续接） ─────────────────
        if re.match(r"^- ", stripped) or re.match(r"^\d+\. ", stripped):
            ordered = bool(re.match(r"^\d+\. ", stripped))
            items: list[str] = []
            while i < n:
                cur = lines[i]
                cs = cur.strip()
                if (not ordered and cs.startswith("- ")) or \
                        (ordered and re.match(r"^\d+\. ", cs)):
                    items.append(re.sub(r"^(- |\d+\. )", "", cs))
                    i += 1
                elif cs and cur.startswith(("  ", "\t")) and items:
                    items[-1] += "\n" + cs        # 续接行
                    i += 1
                else:
                    break
            tag = "ol" if ordered else "ul"
            cls = ' class="ref-list"' if (ref_list and not ordered) else ""
            out.append(f"<{tag}{cls}>" + "".join(
                f"<li>{inline(it)}</li>" for it in items) + f"</{tag}>")
            continue
        # ── 插图标题行 ─────────────────────────────────────────────
        fig = _FIG_RE.match(stripped)
        if fig:
            out.append(figure_html(fig.group(1), stripped))
            i += 1
            continue
        # ── 段落（聚合到空行/结构行；行尾双空格 = 硬换行） ──────────
        para_lines: list[str] = []
        while i < n:
            cur = lines[i]
            cs = cur.strip()
            if (not cs or cs.startswith(("#", "```", "|")) or
                    re.match(r"^- ", cs) or re.match(r"^\d+\. ", cs) or
                    _FIG_RE.match(cs)):
                break
            para_lines.append(cur.rstrip("\n"))
            i += 1
        raw = para_lines[0].strip()
        cls = para_class(raw)
        parts: list[str] = []
        for ln in para_lines:
            parts.append(inline(ln.strip()))
            parts.append("<br>" if ln.endswith("  ") else "\n")
        body = "".join(parts[:-1])
        if cls:
            out.append(f'<div class="{cls}">{body}</div>')
        else:
            out.append(f"<p>{body}</p>")
    return "\n".join(out)


def parse_frontmatter() -> tuple[str, list[list[str]], list[list[str]]]:
    """00-frontmatter.md → (标题行, 文档元信息表行, 修订历史表行)。"""
    lines = (SPEC / "00-frontmatter.md").read_text().split("\n")
    title_line = lines[0].strip()
    tables: list[list[list[str]]] = []
    i = 0
    while i < len(lines):
        if lines[i].strip().startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|[\s:|-]+\|?\s*$", lines[i + 1]):
            rows = [split_row(lines[i])]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(split_row(lines[i]))
                i += 1
            tables.append(rows)
        else:
            i += 1
    if len(tables) < 2:
        raise SystemExit("frontmatter: expected meta + revision tables")
    return title_line, tables[0], tables[1]


def build_cover(meta_rows: list[list[str]], rev_rows: list[list[str]]) -> str:
    kv = "".join(f"<tr><td>{inline(k)}</td><td>{inline(v)}</td></tr>"
                 for k, v in meta_rows)
    rev_head, rev_body = rev_rows[0], rev_rows[1:]
    rev = ("<tr>" + "".join(
        f'<th{" style=\"width:12%\"" if j == 0 else (" style=\"width:20%\"" if j == 1 else "")}>'
        f"{inline(c)}</th>" for j, c in enumerate(rev_head)) + "</tr>")
    rev += "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>"
                   for r in rev_body)
    return f"""
<!-- ═══════════════════ 封面 ═══════════════════ -->
<div class="cover">
  <h1 class="no-break">LabelKit<br>采集数据自动标注工具</h1>
  <div class="sub">产品设计说明书（Product Design Specification）</div>
  <table class="meta-table kv">{kv}</table>
  <table class="meta-table" style="width:76%; margin-top:24px; font-size:8.5pt;">{rev}</table>
</div>
"""


def build_toc(headings: list[tuple[int, str, str | None]]) -> str:
    out = ['<!-- ═══════════════════ 目录 ═══════════════════ -->',
           "<h1>目录</h1>", '<div class="toc">', "<ul>"]
    open_sub = False
    for level, text, hid in headings:
        link = (f'<a href="#{hid}">{inline(text)}</a>' if hid else inline(text))
        if level == 1:
            if open_sub:
                out.append("</ul></li>")
                open_sub = False
            out.append(f'<li class="l1">{link}')
            out.append("<ul>")
            open_sub = True
        else:
            out.append(f"<li>{link}</li>")
    if open_sub:
        out.append("</ul></li>")
    out.append("</ul></div>")
    return "\n".join(out)


def main() -> None:
    title_line, meta_rows, rev_rows = parse_frontmatter()
    headings: list[tuple[int, str, str | None]] = []
    sections: list[str] = []
    for stem in ORDER:
        md = (SPEC / f"{stem}.md").read_text()
        sections.append(convert(md, ref_list=(stem == "85-ch9-references"),
                                headings=headings))
    missing = {"2-1", "2-2", "3-1", "3-2", "3-3", "3-4", "7-1"} - _injected_figs
    if missing:
        raise SystemExit(f"figures not injected (caption line not found): {missing}")
    css = (FIG_DIR / "_style.css").read_text()
    body = build_cover(meta_rows, rev_rows) + build_toc(headings) + "\n" + "\n".join(sections)
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>{esc(title_line)}</title>
<style>{css}</style>
</head>
<!-- 本文件由 tools/build_design_doc.py 从 spec/*.md 汇编生成；请修改 spec 源后重新构建，勿直接编辑。 -->
<body>
{body}
</body>
</html>
"""
    OUT_HTML.write_text(html_doc)
    print(f"wrote {OUT_HTML} ({len(html_doc):,} bytes; figures: "
          f"{sorted(_injected_figs)})")
    if "--pdf" in sys.argv:
        cmd = [CHROME, "--headless", "--disable-gpu",
               f"--user-data-dir={ROOT / '.chrome-pdf-profile'}",
               "--no-pdf-header-footer",
               f"--print-to-pdf={OUT_PDF}", OUT_HTML.as_uri()]
        subprocess.run(cmd, check=True)
        print(f"wrote {OUT_PDF} ({OUT_PDF.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
