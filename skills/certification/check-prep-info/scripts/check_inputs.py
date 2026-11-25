#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Инвентаризация входа для проверки PREP.INFO.

Сканирует каталог с материалами и отвечает на три вопроса:
  1. есть ли PREP.INFO (проверяемый документ);
  2. какие документы ЭД приложены (руководства администратора/пользователя/
     разработчика, ТУ);
  3. можно ли делать полную сверку или только проверку внутренней
     согласованности.

Тип документа определяется по содержимому первой страницы (титул), имя файла —
только подсказка: имена у всех разные, а титул называет документ своим именем.
Если имя и содержимое расходятся, принимается содержимое, а расхождение
попадает в предупреждения. Что за файлы поданы, при постановке задачи обычно
сказано словами — этот перечень главнее и скрипта, и имени файла.

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

# Маркеры титула: по ним документ и опознаётся. PREP.INFO проверяется первым,
# потому что его титул не содержит названий руководств, а руководства не
# называют себя «информацией об изделии».
CONTENT_MARKERS = (
    ("prep.info", "PREP.INFO (кандидат)"),
    ("информация об изделии", "PREP.INFO (кандидат)"),
    ("сведения об изделии", "PREP.INFO (кандидат)"),
    ("руководство администратора", "руководство администратора"),
    ("руководство пользователя", "руководство пользователя"),
    ("руководство разработчика", "руководство разработчика"),
    ("руководство по программированию", "руководство разработчика"),
    ("руководство по установке", "руководство по установке"),
    ("технические условия", "технические условия"),
)

# Подсказки в имени файла — только когда титул не прочитался.
NAME_MARKERS = (
    ("adminguide", "руководство администратора"),
    ("admin_guide", "руководство администратора"),
    ("администратор", "руководство администратора"),
    ("userguide", "руководство пользователя"),
    ("user_guide", "руководство пользователя"),
    ("пользовател", "руководство пользователя"),
    ("devguide", "руководство разработчика"),
    ("dev_guide", "руководство разработчика"),
    ("разработчик", "руководство разработчика"),
    ("installguide", "руководство по установке"),
    ("по установке", "руководство по установке"),
    ("prep.info", "PREP.INFO (кандидат)"),
    ("prep_info", "PREP.INFO (кандидат)"),
    ("prepinfo", "PREP.INFO (кандидат)"),
    ("prep", "PREP.INFO (кандидат)"),
    ("анкет", "PREP.INFO (кандидат)"),
)
VERSION_RE = re.compile(r"(?<!\d)(\d+\.\d+(?:\.\d+){0,2})(?!\.?\d)")
VERSION_IN_TEXT_RE = re.compile(r"верси[яи][^\d\n]{0,12}(\d+\.\d+(?:\.\d+){0,2})", re.I)
MIN_GUIDES = ("руководство администратора", "руководство пользователя")


def sha256_of(path: str) -> str:
    """Полный SHA-256 файла: он идёт в шапку отчёта и должен перепроверяться `sha256sum`."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pdfinfo_field(path: str, field: str) -> str:
    if not shutil.which("pdfinfo"):
        return "?"
    try:
        out = subprocess.run(["pdfinfo", path], capture_output=True, text=True, timeout=20).stdout
        m = re.search(rf"^{field}:\s+(.+)$", out, re.M)
        return m.group(1).strip() if m else "?"
    except Exception:
        return "?"


def first_page_text(path: str) -> str | None:
    """Текст титула. None — pdftotext недоступен, "" — текста извлечь не удалось."""
    if not shutil.which("pdftotext"):
        return None
    try:
        out = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-layout", path, "-"],
            capture_output=True, text=True, timeout=30,
        )
        return out.stdout or ""
    except Exception:
        return ""


def classify_content(text: str | None) -> str:
    if not text:
        return ""
    low = text.lower()
    for marker, kind in CONTENT_MARKERS:
        if marker in low:
            return kind
    return ""


def classify_name(name: str) -> str:
    low = name.lower()
    for marker, kind in NAME_MARKERS:
        if marker in low:
            return kind
    if re.search(r"(^|[_\-. ])tu([_\-.]|$)", low) or re.search(r"(^|\s)ту(\s|$)", low):
        return "технические условия"
    return ""


def version_of(name: str, text: str | None) -> tuple[str, str, str]:
    """Версия из имени файла, из титула и расхождение между ними (если есть)."""
    by_name = VERSION_RE.search(name)
    by_text = VERSION_IN_TEXT_RE.search(text) if text else None
    name_ver = by_name.group(1) if by_name else ""
    text_ver = by_text.group(1) if by_text else ""
    if name_ver and text_ver and name_ver != text_ver:
        return name_ver, "имя файла", f"имя файла: версия {name_ver}, титул: версия {text_ver}"
    if name_ver:
        return name_ver, "имя файла", ""
    if text_ver:
        return text_ver, "титул", ""
    return "", "", ""


def collect(folder: str) -> dict:
    result = {
        "folder": os.path.abspath(folder),
        "files": [],
        "missing": [],
        "warnings": [],
        "verdict": "",
        "exit_code": 2,
    }
    if not os.path.isdir(folder):
        result["warnings"].append(f"каталог не найден: {result['folder']}")
        result["verdict"] = "каталог не найден: проверять нечего"
        return result

    try:
        names = sorted(os.listdir(folder))
    except OSError as exc:
        result["warnings"].append(f"каталог не читается: {exc}")
        result["verdict"] = "каталог не читается: проверять нечего"
        return result

    if not shutil.which("pdftotext"):
        result["warnings"].append(
            "pdftotext не найден — тип документа определён только по имени файла, "
            "титулы не прочитаны (поставить poppler-utils)"
        )

    skipped = []
    for name in names:
        if not name.lower().endswith(".pdf"):
            if os.path.isfile(os.path.join(folder, name)) and not name.startswith("."):
                skipped.append(name)
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path):
            continue

        text = first_page_text(path)
        kind_text = classify_content(text)
        kind_name = classify_name(name)
        if kind_text:
            kind, source = kind_text, "титулу"
            if kind_name and kind_name != kind_text:
                result["warnings"].append(
                    f"{name}: имя файла указывает на «{kind_name}», титул — на «{kind_text}»; "
                    "принято по титулу"
                )
        elif kind_name:
            kind, source = kind_name, "имени файла"
        else:
            kind, source = "не распознано", ""

        version, version_source, version_conflict = version_of(name, text)
        if version_conflict:
            result["warnings"].append(f"{name}: {version_conflict} — уточнить, какая версия верна")
        result["files"].append({
            "name": name,
            "kind": kind,
            "kind_source": source,
            "version": version,
            "version_source": version_source,
            "pages": pdfinfo_field(path, "Pages"),
            "size": os.path.getsize(path),
            "sha256": sha256_of(path),
        })

    if skipped:
        result["warnings"].append("не PDF, не учитывались: " + ", ".join(skipped))
    unknown = [f["name"] for f in result["files"] if f["kind"] == "не распознано"]
    if unknown:
        result["warnings"].append(
            "тип не определён ни по титулу, ни по имени: " + ", ".join(unknown)
            + " — спросить, что это за документ, и переименовать по существу"
        )

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
    for f in (prep if guides else []):
        if not f["version"]:
            continue
        same = [g for g in guides if g["version"] == f["version"]]
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
        result["verdict"] = "PREP.INFO не найден: проверять нечего"
        code = 2
    elif not guides:
        result["verdict"] = ("ЭД отсутствуют: возможна только проверка внутренней согласованности "
                             "(2.1–2.4, 3 ↔ 5 ↔ 6.1 ↔ 6.2 ↔ 6.4 ↔ 6.5, схема ↔ порты); в шапке отчёта "
                             "указать «ЭД не представлены, сверка не выполнялась; отчёт неполный»")
        code = 1
    elif any(g in result["missing"] for g in MIN_GUIDES):
        result["verdict"] = ("ЭД неполные: сверка возможна частично — перечислить пункты без сверки "
                             "и запросить недостающие руководства")
        code = 1
    elif unmatched:
        result["verdict"] = ("вход полный, но ЭД другой версии: сверка возможна с той, что дали; "
                             "версии PREP.INFO и ЭД указать в шапке отчёта")
        code = 0
    else:
        result["verdict"] = "вход полный: сверка PREP.INFO с ЭД по всем пунктам возможна"
        code = 0

    result["exit_code"] = code
    return result


def render(data: dict) -> str:
    lines = [f"Каталог: {data['folder']}", "", "Найдено:"]
    if not data["files"]:
        lines.append("  (PDF не найдено)")
    for f in data["files"]:
        kind = f["kind"]
        if f.get("kind_source"):
            kind += f" (по {f['kind_source']})"
        ver = f", версия {f['version']}" if f["version"] else ""
        lines.append(f"  - {f['name']} — {kind}{ver}; стр. {f['pages']}; "
                     f"{f['size']} байт; sha256 {f['sha256']}")
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
