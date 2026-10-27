#!/usr/bin/env python3
"""Проверка .docx на дефекты, из-за которых Word считает файл повреждённым.

LibreOffice и большинство валидаторов такие файлы открывают, поэтому проверка
нужна отдельная. Ищет:
  * нарушение порядка детей w:pPr (numPr раньше pStyle и т.п.);
  * дубли w:bookmarkStart/@w:id и @w:name (появляются при клонировании абзацев);
  * дубли wp:docPr/@id (идентификатор обязан быть уникальным в документе);
  * битые a:blip/@r:embed (ссылка на несуществующую связь);
  * нулевые wp:extent (кадр не отрисуется).

Запуск:
    python3 docx_word_hygiene_check.py file.docx [-q]

Код возврата 0 — чисто, 1 — есть проблемы, 2 — файл не читается.
"""
import collections
import sys
import zipfile

import docx
from docx.oxml.ns import qn

# порядок детей w:pPr по ECMA-376 (частично, но с все местами, где обычно ломают)
PPR_ORDER = [
    "pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl",
    "numPr", "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens",
    "kinsoku", "wordWrap", "overflowPunct", "topLinePunct", "autoSpaceDE",
    "autoSpaceDN", "bidi", "adjustRightInd", "snapToGrid", "spacing", "ind",
    "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc", "textDirection",
    "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
    "sectPr", "pPrChange",
]


def check(path):
    problems, warnings = [], []

    try:
        with zipfile.ZipFile(path) as z:
            bad = z.testzip()
            if bad:
                problems.append(f"повреждён элемент архива: {bad}")
    except zipfile.BadZipFile:
        return None, ["файл не является ZIP-пакетом"], []

    d = docx.Document(path)
    body = d.element.body

    for ppr in body.iter(qn("w:pPr")):
        names = [c.tag.split("}")[1] for c in ppr]
        idx = [PPR_ORDER.index(n) for n in names if n in PPR_ORDER]
        if idx != sorted(idx):
            problems.append(f"порядок w:pPr нарушен: {names}")

    bm_id = collections.Counter(x.get(qn("w:id")) for x in body.iter(qn("w:bookmarkStart")))
    bm_name = collections.Counter(x.get(qn("w:name")) for x in body.iter(qn("w:bookmarkStart")))
    for k, v in bm_id.items():
        if v > 1:
            problems.append(f"дубль закладки w:id={k} ({v} раз)")
    for k, v in bm_name.items():
        if v > 1:
            problems.append(f"дубль имени закладки {k} ({v} раз)")

    docpr = collections.Counter(x.get("id") for x in body.iter(qn("wp:docPr")))
    for k, v in docpr.items():
        if v > 1:
            problems.append(f"дубль wp:docPr/@id={k} ({v} раз)")

    cnv = collections.Counter(x.get("id") for x in body.iter(qn("pic:cNvPr")))
    for k, v in cnv.items():
        if v > 1:
            warnings.append(f"pic:cNvPr/@id={k} встречается {v} раз (Word обычно терпит)")

    rels = d.part.rels
    broken = 0
    for blip in body.iter(qn("a:blip")):
        if blip.get(qn("r:embed")) not in rels:
            broken += 1
    if broken:
        problems.append(f"битых a:blip/@r:embed: {broken}")

    zero = sum(1 for e in body.iter(qn("wp:extent"))
               if int(e.get("cy", "0")) <= 0 or int(e.get("cx", "0")) <= 0)
    if zero:
        problems.append(f"нулевой wp:extent у {zero} рисунков")

    stats = {
        "абзацев": len(d.paragraphs),
        "таблиц": len(d.tables),
        "рисунков": len(list(body.iter(qn("wp:extent")))),
        "закладок": sum(bm_id.values()),
        "media-частей": len([p for p in d.part.package.iter_parts()
                             if str(p.partname).startswith("/word/media/")]),
    }
    return stats, problems, warnings


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    path = argv[1]
    quiet = "-q" in argv
    stats, problems, warnings = check(path)
    if stats is None:
        print(f"{path}: не читается — {problems[0]}")
        return 2
    print(f"{path}")
    if not quiet:
        print("  " + ", ".join(f"{k}: {v}" for k, v in stats.items()))
    for w in warnings:
        print(f"  ~ {w}")
    if problems:
        print(f"  ПРОБЛЕМЫ ({len(problems)}):")
        for p in problems[:20]:
            print(f"    - {p}")
        return 1
    print("  OOXML-гигиена: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
