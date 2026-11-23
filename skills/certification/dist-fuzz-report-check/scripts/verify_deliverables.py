#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Контроль целостности сдаточных материалов перед отправкой.

Проверяет каталог с отчётом и приложениями: zip-целостность DOCX, соответствие
структуры DOCX исходному MD (число разделов и таблиц), дубли заголовков, ширину
строк таблиц в MD, санитарность HTML, наличие устаревших чисел и посторонних
файлов ("~$", ".tmp"). Печатает отчёт и код возврата 1, если что-то не так.

Примеры:

    python3 verify_deliverables.py output/
    python3 verify_deliverables.py output/ --marker "716" --marker "14 042" \\
        --stale "14 050 ответов" --stale "47 мс"
    python3 verify_deliverables.py output/ --json

Зависимость python-docx опциональна: без неё структурная сверка DOCX
пропускается, zip-целостность и MD/HTML проверяются всегда.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import zipfile
from collections import Counter


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def md_outline(text):
    heads = [h.strip() for h in re.findall(r"^#{1,3} .+$", text, re.M)]
    tables, block, bad = 0, [], []
    for line in text.split("\n") + [""]:
        if line.startswith("|"):
            block.append(line)
            continue
        if block:
            tables += 1
            widths = {len(b.strip().strip("|").split("|")) for b in block}
            if len(widths) > 1:
                bad.append(tables)
            block = []
    return {
        "headings": len(heads),
        "duplicate_headings": [h for h, n in Counter(heads).items() if n > 1],
        "tables": tables,
        "ragged_tables": bad,
        "unpaired_bold": text.count("**") % 2,
    }


def docx_outline(path):
    try:
        from docx import Document
    except ImportError:
        return {"skipped": "python-docx не установлен в этом интерпретаторе"}
    doc = Document(path)
    heads = [p.text.strip() for p in doc.paragraphs if p.style.name.startswith("Heading")]
    empty_cells = sum(
        1 for t in doc.tables for r in t.rows for c in r.cells if not c.text.strip()
    )
    return {
        "headings": len(heads),
        "duplicate_headings": [h for h, n in Counter(heads).items() if n > 1],
        "tables": len(doc.tables),
        "tables_without_body": [i + 1 for i, t in enumerate(doc.tables) if len(t.rows) < 2],
        "empty_cells": empty_cells,
    }


def html_checks(text):
    ext = re.findall(
        "(?:src|href)=[" + chr(34) + chr(39) + "]https?://", text
    )
    return {
        "html_tags": text.count("<html"),
        "body_tags": text.count("<body"),
        "tables": text.count("<table"),
        "external_refs": len(ext),
        "closed": text.rstrip().endswith("</html>"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--marker", action="append", default=[],
                    help="строка, которая обязана встречаться в MD")
    ap.add_argument("--stale", action="append", default=[],
                    help="устаревшая строка, которой быть не должно")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(args.dir)
    files = sorted(f for f in os.listdir(root) if os.path.isfile(os.path.join(root, f)))
    report, errors = {"dir": root, "files": {}, "errors": []}, []

    jokes = [f for f in files if f.startswith("~$") or f.endswith((".tmp", ".crdownload"))]
    if jokes:
        errors.append("посторонние файлы: " + ", ".join(jokes))

    md_struct = {}
    for name in files:
        path = os.path.join(root, name)
        info = {
            "size": os.path.getsize(path),
            "mtime_ns": os.stat(path).st_mtime_ns,
            "sha256": sha256(path)[:16] + "…",
        }
        if name.endswith(".docx"):
            try:
                with zipfile.ZipFile(path) as z:
                    info["zip_broken"] = z.testzip()
                    info["has_document_xml"] = "word/document.xml" in z.namelist()
                if info["zip_broken"] or not info["has_document_xml"]:
                    errors.append(f"{name}: битый zip или нет word/document.xml")
                info.update(docx_outline(path))
                if info.get("duplicate_headings"):
                    errors.append(f"{name}: дубли заголовков {info['duplicate_headings']}")
                if info.get("tables_without_body"):
                    errors.append(f"{name}: таблицы без тела {info['tables_without_body']}")
            except zipfile.BadZipFile:
                errors.append(f"{name}: не открывается как zip")
        elif name.endswith(".md"):
            text = open(path, encoding="utf-8").read()
            info.update(md_outline(text))
            md_struct[name] = info
            if info["duplicate_headings"]:
                errors.append(f"{name}: дубли заголовков {info['duplicate_headings']}")
            if info["ragged_tables"]:
                errors.append(f"{name}: таблицы с разной шириной строк {info['ragged_tables']}")
            if info["unpaired_bold"]:
                errors.append(f"{name}: непарная разметка **")
            for m in args.marker:
                if m not in text:
                    errors.append(f"{name}: нет обязательного маркера {m!r}")
            for s in args.stale:
                if s in text:
                    errors.append(f"{name}: осталось устаревшее {s!r}")
        elif name.endswith(".html"):
            text = open(path, encoding="utf-8").read()
            info.update(html_checks(text))
            if info["html_tags"] != 1 or info["body_tags"] != 1:
                errors.append(f"{name}: ожидался один <html>/<body>")
            if not info["closed"]:
                errors.append(f"{name}: документ не закрыт </html>")
            if info["external_refs"]:
                errors.append(f"{name}: внешних ссылок {info['external_refs']}")
        report["files"][name] = info

    # сверка DOCX с одноимённым MD: разделы и таблицы должны совпадать
    for name, info in report["files"].items():
        if not name.endswith(".docx") or "headings" not in info:
            continue
        stem = name[:-5]
        src = md_struct.get(stem + ".md")
        if not src:
            continue
        if (info["headings"], info["tables"]) != (src["headings"], src["tables"]):
            errors.append(
                f"{name}: структура не совпадает с {stem}.md "
                f"(docx {info['headings']}/{info['tables']}, md {src['headings']}/{src['tables']})"
            )

    report["errors"] = errors
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"каталог: {root}")
        for name, info in report["files"].items():
            tail = ", ".join(
                f"{k}={v}" for k, v in info.items()
                if k in ("headings", "tables", "empty_cells", "html_tags", "external_refs")
            )
            print(f"  {name:44} {info['size']:>8} байт  sha256={info['sha256']}  {tail}")
        print("\nзамечания:")
        for e in errors:
            print("  ✗", e)
        if not errors:
            print("  замечаний нет")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
