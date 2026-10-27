#!/usr/bin/env python3
"""Dump a .docx body in true document order (paragraphs and tables interleaved).

Why: python-docx exposes Document.paragraphs and Document.tables as two
separate lists, so a reader that uses only those loses the position of every
table relative to the headings and text around it. Specs, requirement
documents, and test reports bind a table to the paragraph directly above it
("Checked requirements" heading -> the requirement table), so the order is
information you cannot recover from the split lists.

Usage:
    python docx_read_ordered.py file.docx                     # text dump
    python docx_read_ordered.py file.docx --json              # blocks as JSON
    python docx_read_ordered.py file.docx --empty             # keep blank paras
    python docx_read_ordered.py file.docx --no-headers
    python docx_read_ordered.py file.docx --start 4.1.1 --end 4.1.2

Text format:
    [P12] (Heading 3) 4.1.1 Verification of authentication
    [T13] table rows=2 cols=1
      | Authentication with login and password (IAF.1.1)
      | Unique identification and authentication (IAF.5.1)

Merged cells: python-docx returns the same <w:tc> element for every grid slot
a merged cell spans, so a naive per-row loop repeats the text and shifts the
extracted columns. Repeated slots are printed as markers instead:
    (=) same row, horizontally merged with the cell to the left
    (^) vertically merged with a cell from an earlier row
Nested tables inside cells are not expanded (cell.text skips them).

Indices are stable: P/T counters increase for every block, including skipped
blank paragraphs, so [P42] means the same block on every run of an unchanged
file.
"""
from __future__ import annotations

import argparse
import json
import sys

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph


def iter_blocks(doc):
    """Yield ('p', Paragraph) and ('tbl', Table) in body order."""
    body = doc.element.body
    for child in body.iterchildren():
        if child.tag == qn('w:p'):
            yield 'p', Paragraph(child, doc)
        elif child.tag == qn('w:tbl'):
            yield 'tbl', Table(child, doc)


def table_rows(table):
    """Rows as lists of cell text, merged grid slots collapsed to markers."""
    seen_tc = set()
    rows = []
    for row in table.rows:
        row_tc = set()
        cells = []
        for cell in row.cells:
            tc = cell._tc
            if tc in row_tc:
                cells.append('(=)')
                continue
            row_tc.add(tc)
            if tc in seen_tc:
                cells.append('(^)')
            else:
                seen_tc.add(tc)
                cells.append(' '.join(cell.text.split()))
        rows.append(cells)
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('path', help='input .docx')
    ap.add_argument('--json', action='store_true', help='emit JSON blocks')
    ap.add_argument('--empty', action='store_true', help='keep blank paragraphs')
    ap.add_argument('--no-headers', action='store_true',
                    help='skip header/footer text')
    ap.add_argument('--start', help='emit only from the paragraph containing this text')
    ap.add_argument('--end', help='stop before the paragraph containing this text')
    args = ap.parse_args(argv)

    doc = Document(args.path)

    if not args.no_headers:
        for s in doc.sections:
            for kind, part in (('header', s.header), ('footer', s.footer)):
                text = ' / '.join(p.text.strip() for p in part.paragraphs if p.text.strip())
                if text:
                    print(f'[{kind}] {text}')

    p_i = t_i = 0
    in_range = args.start is None
    blocks = []
    for kind, block in iter_blocks(doc):
        if kind == 'p':
            idx, p_i = p_i, p_i + 1
            text = block.text.strip()
            if args.end and text and args.end in text and in_range:
                break
            if args.start and not in_range and text and args.start in text:
                in_range = True
            if not in_range:
                continue
            if not text and not args.empty:
                continue
            blocks.append({'kind': 'p', 'index': idx,
                           'style': block.style.name if block.style is not None else '',
                           'text': text})
        else:
            idx, t_i = t_i, t_i + 1
            if not in_range:
                continue
            rows = table_rows(block)
            blocks.append({'kind': 'table', 'index': idx, 'rows': rows})

    if args.json:
        json.dump({'blocks': blocks}, sys.stdout, ensure_ascii=False, indent=1)
        print()
        return 0

    for b in blocks:
        if b['kind'] == 'p':
            print(f"[P{b['index']}] ({b['style']}) {b['text']}")
        else:
            rows = b['rows']
            cols = max((len(r) for r in rows), default=0)
            print(f"[T{b['index']}] table rows={len(rows)} cols={cols}")
            for r in rows:
                print('  | ' + ' | '.join(r))
    return 0


if __name__ == '__main__':
    sys.exit(main())
