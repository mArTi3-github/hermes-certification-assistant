#!/usr/bin/env python3
"""Синхронизация кэшированных номеров страниц в оглавлении .docx.

Word пересчитывает оглавление при открытии, если в settings.xml стоит
`w:updateFields`, а LibreOffice и большинство просмотрщиков показывают
кэшированные значения. После удаления или вставки содержимого эти значения
указывают на несуществующие страницы, и отчёт выглядит испорченным.

Скрипт рендерит документ в PDF через soffice, находит фактическую страницу
каждого заголовка и правит ТОЛЬКО число в строке оглавления: табуляции,
отточия и гиперссылки не затрагиваются.

Использование:
    python3 fix_toc_pages.py file.docx [--skip 2] [--min-hits 3]

--skip      сколько первых страниц не рассматривать (титул + оглавление)
--min-hits  со скольких заголовков на странице считать её «перечнем» и
            пропускать (таблица плана тестирования перечисляет все разделы)
Код возврата 1 — часть заголовков не найдена в PDF, номера остались прежними.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import docx
from docx.oxml.ns import qn


def norm(text):
    return re.sub(r"\s+", " ", text).strip()


def page_texts(pdf):
    info = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True).stdout
    pages = int(re.search(r"Pages:\s+(\d+)", info).group(1))
    out = []
    for i in range(1, pages + 1):
        txt = subprocess.run(
            ["pdftotext", "-layout", "-f", str(i), "-l", str(i), str(pdf), "-"],
            capture_output=True, text=True).stdout
        out.append(norm(txt))
    return out


def toc_entries(doc):
    """Строки оглавления: (абзац, заголовок, кэшированная страница)."""
    out = []
    for par in doc.paragraphs:
        if not par.style.name.lower().startswith("toc"):
            continue
        parts = par.text.split("\t")
        if len(parts) < 3 or not parts[-1].strip().isdigit():
            continue
        title = norm(parts[-2])
        if len(title) < 8:
            continue
        out.append((par, title, int(parts[-1].strip())))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("path")
    ap.add_argument("--skip", type=int, default=2)
    ap.add_argument("--min-hits", type=int, default=3)
    args = ap.parse_args()

    path = Path(args.path)
    tmpdir = Path(tempfile.mkdtemp())
    try:
        subprocess.run(["soffice", "--headless", "--convert-to", "pdf",
                        "--outdir", str(tmpdir), str(path)],
                       check=True, capture_output=True)
        pdf = next(tmpdir.glob("*.pdf"))
        pages = page_texts(pdf)
        print(f"страниц в PDF: {len(pages)}")

        doc = docx.Document(str(path))
        entries = toc_entries(doc)
        if not entries:
            print("строк оглавления с номерами страниц не найдено")
            return 0

        # страницы-«перечни» (таблица плана тестирования, само оглавление)
        # содержат сразу много заголовков — за фактическое место не годятся
        probes = [t[:60] for _, t, _ in entries]
        index_like = {i for i, text in enumerate(pages, 1)
                      if sum(1 for p in probes if p in text) >= args.min_hits}
        if index_like:
            print("страницы-перечни пропущены:", sorted(index_like))

        fixed, missing = 0, 0
        for par, title, cached in entries:
            probe = title[:60]
            found = next((i for i, text in enumerate(pages, 1)
                          if i > args.skip and i not in index_like and probe in text), None)
            if found is None:
                print("  ! заголовок не найден в PDF:", title[:60])
                missing += 1
                continue
            if found == cached:
                continue
            numbers = [t for t in par._p.iter(qn("w:t")) if t.text and t.text.strip().isdigit()]
            if not numbers:
                continue
            numbers[-1].text = str(found)
            fixed += 1
            print(f"  {title[:45]:45} {cached} -> {found}")

        doc.save(str(path))
        print(f"обновлено записей: {fixed}, не найдено: {missing}")
        return 1 if missing else 0
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
