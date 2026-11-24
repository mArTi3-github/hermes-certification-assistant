#!/usr/bin/env python3
"""Поиск отчёта по заимствованным компонентам в шаренных папках.

Пользователь просит «проведи анализ уязвимостей заимствованных компонентов» — файл обычно
лежит в шаренной папке хоста (VMware: `/mnt/hgfs/...`). Скрипт находит точки монтирования
шары, перебирает xlsx/csv и оценивает, похож ли файл на отчёт: по именам колонок
(`component, purl, severity, status, detail, vuln_id`) и по имени файла.

    python3 find_report.py                 # поиск и таблица кандидатов
    python3 find_report.py --json out.json # то же машинночитаемо
    python3 find_report.py --root /mnt/hgfs/<шара>

Коды выхода: 0 — кандидаты найдены, 2 — шара не смонтирована, 3 — смонтирована, отчёта нет.
Ищет только чтением: ничего не создаёт и не меняет в найденных каталогах.
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

HEADER_WORDS = {
    "component", "component_purl", "ancestor_component_purl", "purl", "severity", "status",
    "detail", "vuln_id", "id", "cve", "name", "version",
}
STRONG = {"component", "component_purl", "ancestor_component_purl", "severity", "status", "detail"}
NAME_HINTS = ("sbom", "vex", "src.dep", "src-dep", "src_dep", "уязвим", "заимствован",
              "package", "components", "otchet", "отчёт", "report")
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".Trash", "$RECYCLE.BIN",
             "SRC_DEP_analys"}
MAX_DEPTH = 4
SIZE_MIN = 256
SIZE_MAX = 200 * 1024 * 1024


def mounts():
    """Точки монтирования сетевых/шаренных ФС."""
    roots = []
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as f:
            for line in f:
                dev, mnt, fstype = line.split()[:3]
                if fstype in ("fuse.vmhgfs-fuse", "vmhgfs", "9p", "cifs", "fuse.sshfs",
                              "fuse.rclone", "nfs", "nfs4", "vboxsf"):
                    roots.append((fstype, mnt))
    except OSError:
        pass
    return roots


def xlsx_headers(path):
    """Имена колонок без openpyxl: sharedStrings, иначе inline-строки первого листа."""
    names, hits = [], 0
    try:
        with zipfile.ZipFile(path) as z:
            names = []
            if "xl/sharedStrings.xml" in z.namelist():
                raw = z.read("xl/sharedStrings.xml").decode("utf-8", "replace")
                names = [re.sub(r"<[^>]+>", "", s).strip()
                         for s in re.findall(r"<t[^>]*>(.*?)</t>", raw, re.S)[:400]]
            if "xl/workbook.xml" in z.namelist():
                wb = z.read("xl/workbook.xml").decode("utf-8", "replace")
                sheets = re.findall(r'<sheet[^>]*name="([^"]+)"', wb)
            else:
                sheets = []
            if not names:
                # inline-строки: значения лежат прямо в листе
                sheet = next((n for n in sorted(z.namelist())
                              if n.startswith("xl/worksheets/sheet")), None)
                if sheet:
                    raw = z.read(sheet).decode("utf-8", "replace")
                    names = [re.sub(r"<[^>]+>", "", s).strip()
                             for s in re.findall(r"<t[^>]*>(.*?)</t>", raw, re.S)[:400]]
            tokens = set()
            for n in names:
                for tok in re.split(r"[,;\t|]", n):
                    tok = tok.strip().strip('"').lower()
                    if tok:
                        tokens.add(tok)
            hits = len(tokens & HEADER_WORDS)
    except (zipfile.BadZipFile, KeyError, OSError):
        return 0, [], []
    return hits, names, sheets


def csv_headers(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as f:
            head = f.read(8192)
    except OSError:
        return 0, [], []
    sample = head.splitlines()[:3]
    for delim in (";", ",", "\t"):
        if not sample:
            break
        try:
            rows = list(csv.reader(io.StringIO("\n".join(sample)), delimiter=delim))
        except csv.Error:
            continue
        flat = [c.strip().lower() for r in rows for c in r]
        hits = len(set(flat) & HEADER_WORDS)
        if hits:
            return hits, flat[:40], ["(csv)"]
    return 0, [], []


def scan(roots, depth=MAX_DEPTH):
    found = []
    derived = []
    for root in roots:
        base = Path(root)
        if not base.exists():
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            if len(Path(dirpath).parts) - base_depth >= depth:
                dirnames[:] = []
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in filenames:
                low = fn.lower()
                if not low.endswith((".xlsx", ".csv")) or low.startswith("~$"):
                    continue
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                if not (SIZE_MIN <= st.st_size <= SIZE_MAX):
                    continue
                hits, names, sheets = (xlsx_headers(p) if low.endswith(".xlsx")
                                       else csv_headers(p))
                if not hits:
                    continue
                name_hits = sum(1 for h in NAME_HINTS if h in low)
                toks = {t.strip().strip('"').lower() for n in names for t in re.split(r"[,;\t|]", n)}
                strong = len(toks & STRONG)
                if "_filtered" in low:
                    derived.append(str(p))
                    continue
                score = hits * 2 + strong * 3 + name_hits * 2
                found.append({
                    "path": str(p), "score": score, "header_hits": hits,
                    "strong_columns": strong, "name_hits": name_hits,
                    "size_kb": round(st.st_size / 1024), "sheets": sheets[:6],
                    "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
    globals()["DERIVED"] = derived
    found.sort(key=lambda x: (-x["score"], -datetime.strptime(x["modified"], "%Y-%m-%d %H:%M").timestamp()))
    return found


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", action="append", help="каталог поиска (можно несколько)")
    ap.add_argument("--json", help="сохранить результат в файл")
    ap.add_argument("--depth", type=int, default=MAX_DEPTH)
    a = ap.parse_args()

    roots = a.root or [m for _, m in mounts()]
    if not roots:
        print("шара не смонтирована: в /proc/mounts нет vmhgfs/9p/cifs/vboxsf\n"
              "VMware: sudo vmhgfs-fuse .host:/ /mnt/hgfs -o allow_other\n"
              "VirtualBox: sudo mount -t vboxsf <имя_шары> /mnt/<точка>", file=sys.stderr)
        sys.exit(2)

    found = scan(roots, a.depth)
    payload = {"roots": roots, "candidates": found}
    if a.json:
        Path(a.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    if not found:
        print("отчёт не найден в каталогах: " + ", ".join(roots), file=sys.stderr)
        sys.exit(3)

    print(f"каталоги поиска: {', '.join(roots)}")
    print(f"кандидатов: {len(found)}\n")
    for f in found[:10]:
        print(f"  [{f['score']:>2}] {f['path']}")
        print(f"       {f['size_kb']} КБ, изменён {f['modified']}, колонок-признаков "
              f"{f['strong_columns']}/{f['header_hits']}, листы: {', '.join(f['sheets'][:3])}")
    if globals().get("DERIVED"):
        print("\nпроизводные файлы (наши результаты, в работу не берутся): "
              + ", ".join(globals()["DERIVED"]))
    if len(found) > 1:
        print("\nкандидатов больше одного — уточнить у человека, какой файл брать "
              "(по умолчанию первый в списке: свежий и с наибольшим числом колонок-признаков)")
    sys.exit(0)


if __name__ == "__main__":
    main()
