#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Инвентаризация входа для проверки PREP.INFO.

Сканирует каталог с материалами и отвечает на три вопроса:
  1. есть ли PREP.INFO (проверяемый документ);
  2. какие документы ЭД приложены (руководства администратора/пользователя/
     разработчика, ТУ);
  3. можно ли делать полную сверку или только проверку внутренней
     согласованности.

Использование:
    python3 check_inputs.py [каталог]        # по умолчанию текущий
    python3 check_inputs.py --json [каталог]

Код возврата: 0 — полная сверка возможна; 1 — ЭД неполные; 2 — PREP.INFO не найден.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

GUIDE_MARKERS = {
    "adminguide": "руководство администратора",
    "admin_guide": "руководство администратора",
    "userguide": "руководство пользователя",
    "user_guide": "руководство пользователя",
    "devguide": "руководство разработчика",
    "dev_guide": "руководство разработчика",
    "installguide": "руководство по установке",
    "tu_": "технические условия",
}
VERSION_RE = re.compile(r"(\d{4}\.\d+(?:\.\d+){0,2})")
MIN_GUIDES = ("руководство администратора", "руководство пользователя")


def sha256_short(path: str, limit: int = 5_000_000) -> str:
    h = hashlib.sha256()
    read = 0
    with open(path, "rb") as fh:
        while read < limit:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            read += len(chunk)
            h.update(chunk)
    return h.hexdigest()


def pages(path: str) -> str:
    if not shutil.which("pdfinfo"):
        return "?"
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=20).stdout
        m = re.search(r"^Pages:\s+(\d+)", out, re.M)
        return m.group(1) if m else "?"
    except Exception:
        return "?"


def classify(name: str) -> str:
    low = name.lower()
    for marker, kind in GUIDE_MARKERS.items():
        if marker in low:
            return kind
    if re.search(r"(^|[_\-. ])tu([_\-.]|$)", low) or "ту " in low:
        return "технические условия"
    return "PREP.INFO (кандидат)"


def collect(folder: str) -> dict:
    result = {"folder": os.path.abspath(folder), "files": [], "missing": [], "warnings": []}
    if not os.path.isdir(folder):
        result["warnings"].append("каталог не найден")
        return result

    for name in sorted(os.listdir(folder)):
        if not name.lower().endswith(".pdf"):
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue
        kind = classify(name)
        ver = VERSION_RE.search(name)
        result["files"].append({
            "name": name,
            "kind": kind,
            "version": ver.group(1) if ver else "",
            "pages": pages(path),
            "size": os.path.getsize(path),
            "sha256_16": sha256_short(path)[:16],
        })

    kinds = {f["kind"] for f in result["files"]}
    prep = [f for f in result["files"] if f["kind"].startswith("PREP.INFO")]
    guides = [f for f in result["files"] if f["kind"].startswith("руководство")]

    if not prep:
        result["missing"].append("PREP.INFO (PDF)")
    for need in MIN_GUIDES:
        if need not in kinds:
            result["missing"].append(need)

    # соответствие версий: для каждого PREP.INFO проверяем, есть ли руководства той же версии
    guide_versions = {f["version"] for f in guides if f["version"]}
    unmatched = []
    for f in prep:
        if not f["version"]:
            continue
        same = [g for g in guides if g["version"] == f["version"] and "руководство" in g["kind"]]
        if not same:
            unmatched.append(f["name"])
    if unmatched:
        result["warnings"].append(
            "нет руководств той же версии для: " + ", ".join(unmatched)
            + " (есть версии: " + (", ".join(sorted(guide_versions)) or "не указаны") + ")"
        )
    if len(prep) > 1:
        result["warnings"].append(
            f"в каталоге {len(prep)} документа-кандидата PREP.INFO — уточнить, какой проверяется"
        )
    if "руководство разработчика" not in kinds:
        result["warnings"].append(
            "нет руководства разработчика — коды ответов API и поведение интерфейсов "
            "сверить не с чем (пп. 4, 6.2)"
        )
    if "технические условия" not in kinds:
        result["warnings"].append("ТУ не приложены — требования к среде и ФБ сверяются только по руководствам")

    if not prep:
        verdict = "PREP.INFO не найден: проверять нечего"
        code = 2
    elif not guides:
        verdict = ("ЭД отсутствуют: возможна только проверка внутренней согласованности "
                   "(2.1–2.4, 3 ↔ 5 ↔ 6.1 ↔ 6.2 ↔ 6.4 ↔ 6.5, схема ↔ порты); в шапке отчёта "
                   "указать «ЭД не представлены, сверка не выполнялась; отчёт неполный»")
        code = 1
    elif any(g in result["missing"] for g in MIN_GUIDES):
        verdict = ("ЭД неполные: сверка возможна частично — перечислить пункты без сверки "
                   "и запросить недостающие руководства")
        code = 1
    else:
        verdict = "вход полный: сверка PREP.INFO с ЭД по всем пунктам возможна"
        code = 0

    result["verdict"] = verdict
    result["exit_code"] = code
    return result


def render(data: dict) -> str:
    lines = [f"Каталог: {data['folder']}", "", "Найдено:"]
    if not data["files"]:
        lines.append("  (PDF не найдено)")
    for f in data["files"]:
        ver = f", версия {f['version']}" if f["version"] else ""
        lines.append(f"  - {f['name']} — {f['kind']}{ver}; стр. {f['pages']}; "
                     f"{f['size']} байт; sha256 {f['sha256_16']}…")
    lines += ["", "Не хватает:"]
    lines += [f"  - {m}" for m in data["missing"]] or ["  (всё необходимое есть)"]
    if data["warnings"]:
        lines += ["", "Замечания по составу:"]
        lines += [f"  - {w}" for w in data["warnings"]]
    lines += ["", f"Вывод: {data['verdict']}"]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Что есть на входе для проверки PREP.INFO")
    ap.add_argument("folder", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    data = collect(a.folder)
    print(json.dumps(data, ensure_ascii=False, indent=2) if a.json else render(data))
    return data["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
