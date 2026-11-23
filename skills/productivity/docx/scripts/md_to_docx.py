#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Markdown report -> .docx with real Word structure (headings, tables, lists).

Designed for long analytical reports written as Markdown (a heading tree, GFM
tables, bold/code inline markup, numbered findings, bullet evidence lists) that
must be handed over as an editable Word document.

    python3 md_to_docx.py report.md report.docx [--landscape] [--font "Times New Roman"]

Supported syntax: `#`/`##`/`###` headings, `| ... |` tables with a `---`
separator row, `-`/`*` bullets, `1.` numbered items, fenced code blocks,
`> ` quotes, horizontal rules (dropped), inline `**bold**`, `*italic*` and
`` `code` `` including code nested inside bold.

Key behaviours that make the output usable instead of merely generated:
  * headings become real Heading 1-3 paragraphs, so the navigation pane and a
    TOC field work, and the numbers stay editable text;
  * numbered items are numbered by this script per contiguous block, not by the
    `List Number` style (that style keeps counting across the whole document);
  * table columns get explicit widths proportional to their content, otherwise
    Word splits the page evenly and long cells wrap mid-token;
  * the header row is marked as a repeating table header (`w:tblHeader`).
"""
import argparse
import re
import sys

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

INLINE = re.compile(r"(\*\*.+?\*\*|`[^`]+`|(?<!\*)\*[^*\n]+\*(?!\*))")
NUM_ITEM = re.compile(r"^(\d+)[.)]\s+")


def add_runs(p, text, bold=False, code=False, italic=False):
    """Inline markdown with nesting (**bold with `code` inside**)."""
    for part in INLINE.split(text):
        if not part:
            continue
        if len(part) > 4 and part.startswith("**") and part.endswith("**"):
            add_runs(p, part[2:-2], bold=True, code=code, italic=italic)
        elif len(part) > 2 and part.startswith("`") and part.endswith("`"):
            add_runs(p, part[1:-1], bold=bold, code=True, italic=italic)
        elif len(part) > 2 and part.startswith("*") and part.endswith("*"):
            add_runs(p, part[1:-1], bold=bold, code=code, italic=True)
        else:
            r = p.add_run(part)
            if bold:
                r.bold = True
            if italic:
                r.italic = True
            if code:
                # monospace at body size: shrinking code runs inside headings
                # produces a visibly broken heading
                r.font.name = "Consolas"


def set_table(rows, avail_cm, font_pt=9):
    header, body = rows[0], rows[1:]
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False  # required, otherwise Word ignores the widths below

    def fill(cells, values, is_header):
        for i, val in enumerate(values[: len(header)]):
            c = cells[i]
            c.text = ""
            p = c.paragraphs[0]
            p.paragraph_format.space_after = Pt(0)
            add_runs(p, val)
            for r in p.runs:
                r.font.size = Pt(font_pt)
                if is_header:
                    r.bold = True

    fill(t.rows[0].cells, header, True)
    for row in body:
        fill(t.add_row().cells, row, False)

    # proportional widths, min 1.7 cm, remainder taken from the widest columns
    weights = []
    for i in range(len(header)):
        lens = [len(header[i])] + [len(r[i]) for r in body if i < len(r)]
        weights.append(max(6, max(lens)))
    total = sum(weights)
    widths = [max(1.7, avail_cm * w / total) for w in weights]
    excess = sum(widths) - avail_cm
    while excess > 0.01:
        growable = [i for i, w in enumerate(widths) if w > 1.75]
        pool = sum(widths[i] - 1.7 for i in growable)
        if not pool:
            break
        for i in growable:
            widths[i] -= excess * (widths[i] - 1.7) / pool
        excess = sum(widths) - avail_cm
    for row in t.rows:
        for i, c in enumerate(row.cells[: len(widths)]):
            c.width = Cm(widths[i])

    tr_pr = t.rows[0]._tr.get_or_add_trPr()
    th = OxmlElement("w:tblHeader")
    th.set(qn("w:val"), "true")
    tr_pr.append(th)
    doc.add_paragraph().paragraph_format.space_after = Pt(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--landscape", action="store_true", help="wide tables (6+ columns)")
    ap.add_argument("--font", default="Times New Roman")
    args = ap.parse_args()

    global doc
    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = args.font
    normal.font.size = Pt(11)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), args.font)
    normal.paragraph_format.space_after = Pt(4)
    normal.paragraph_format.line_spacing = 1.15
    for name, size in (("Heading 1", 15), ("Heading 2", 13), ("Heading 3", 11.5)):
        s = doc.styles[name]
        s.font.name = args.font
        s.font.size = Pt(size)
        s.font.bold = True
        s.font.color.rgb = RGBColor(0x1F, 0x2D, 0x3D)
        s.paragraph_format.space_before = Pt(10)
        s.paragraph_format.space_after = Pt(4)

    sec = doc.sections[0]
    if args.landscape:
        sec.orientation = WD_ORIENT.LANDSCAPE
        sec.page_width, sec.page_height = sec.page_height, sec.page_width
    sec.left_margin = sec.right_margin = Cm(1.5)
    sec.top_margin = sec.bottom_margin = Cm(1.8)
    avail_cm = (sec.page_width - sec.left_margin - sec.right_margin) / 360000.0

    lines = open(args.src, encoding="utf-8").read().split("\n")
    num = 0            # counter of the current numbered block
    in_code = False
    i = 0
    while i < len(lines):
        line = lines[i].rstrip()

        if line.strip().startswith("```"):
            in_code = not in_code
            i += 1
            continue
        if in_code:
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.5)
            p.paragraph_format.space_after = Pt(0)
            r = p.add_run(line if line.strip() else " ")
            r.font.name = "Consolas"
            r.font.size = Pt(9)
            i += 1
            continue
        if not line.strip():
            i += 1
            continue
        if line.strip() in ("---", "***", "___"):
            # a horizontal rule is a rule, not a paragraph: printing it leaves
            # literal '---' lines all over the finished document
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{3,}:?", c) for c in cells):
                    rows.append(cells)
                i += 1
            if rows:
                set_table(rows, avail_cm)
            num = 0
            continue
        if line.startswith("### "):
            num = 0
            add_runs(doc.add_heading("", level=3), line[4:].strip())
        elif line.startswith("## "):
            num = 0
            add_runs(doc.add_heading("", level=2), line[3:].strip())
        elif line.startswith("# "):
            num = 0
            add_runs(doc.add_heading("", level=1), line[2:].strip())
        elif line.startswith("> "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(1.0)
            add_runs(p, line[2:])
            for r in p.runs:
                r.italic = True
        elif NUM_ITEM.match(line):
            num += 1
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Cm(0.75)
            p.paragraph_format.first_line_indent = Cm(-0.5)
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT  # justify + long paths = rivers
            p.add_run(f"{num}. ")
            add_runs(p, NUM_ITEM.sub("", line))
        elif re.match(r"^[-*+]\s+", line):
            add_runs(doc.add_paragraph(style="List Bullet"), re.sub(r"^[-*+]\s+", "", line))
        else:
            num = 0
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            add_runs(p, line)
        i += 1

    doc.save(args.dst)
    print(f"saved {args.dst}: {len(doc.paragraphs)} paragraphs, {len(doc.tables)} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
