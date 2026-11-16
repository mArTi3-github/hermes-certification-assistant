#!/usr/bin/env python3
"""Целостность и полнота комплекта материалов тестирования.

Проверяет то, что глазами не проверяется:
  * MANIFEST.sha256 — отсутствующие и изменённые файлы;
  * битые ссылки в index.html (частый след изъятого документа);
  * покрытие по профилям unit/functional/combined: итоги, минимум по файлу,
    число файлов ниже порога;
  * сводку диагностик санитайзеров (сумма occurrences против заявленной);
  * ключевые счётчики results-summary.json.

Использование:
    python3 package_audit.py <каталог> [--threshold 25]

Только stdlib. Код возврата 1, если найдены расхождения манифеста.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys


MANIFEST_RE = re.compile(r"^([0-9a-f]{64})\s+\*?(.+)$")


def sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def check_manifest(root):
    path = os.path.join(root, "MANIFEST.sha256")
    if not os.path.isfile(path):
        return None
    total, bad = 0, []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = MANIFEST_RE.match(line.strip())
            if not m:
                continue
            total += 1
            digest, rel = m.groups()
            target = os.path.join(root, rel)
            if not os.path.isfile(target):
                bad.append((rel, "отсутствует"))
            elif sha256(target) != digest:
                bad.append((rel, "хеш не совпадает"))
    return total, bad


def check_links(root):
    path = os.path.join(root, "index.html")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    links = sorted({
        l for l in re.findall(r'href="([^"#]+)"', text)
        if not l.startswith(("http://", "https://", "mailto:"))
    })
    missing = [l for l in links if not os.path.exists(os.path.join(root, l.split("?")[0]))]
    return len(links), missing


def coverage(root, threshold):
    rows = []
    for profile in ("unit", "functional", "combined"):
        path = os.path.join(root, profile, "summary.json")
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        files = data.get("files") or []
        below = [f for f in files if (f.get("line_percent") or 0.0) < threshold]
        below.sort(key=lambda f: f.get("line_percent") or 0.0)
        worst = below[0] if below else None
        rows.append({
            "profile": profile,
            "files": len(files),
            "line": (data.get("line_covered"), data.get("line_total"), data.get("line_percent")),
            "function": (data.get("function_covered"), data.get("function_total"), data.get("function_percent")),
            "branch": data.get("branch_percent"),
            "below": len(below),
            "zero": sum(1 for f in files if not (f.get("line_percent") or 0.0)),
            "worst": worst,
        })
    return rows


def findings(root):
    path = os.path.join(root, "sanitizer-findings.csv")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    events, by_detector = 0, {}
    for row in rows:
        try:
            count = int(row.get("occurrences") or 0)
        except ValueError:
            count = 0
        events += count
        det = (row.get("detector") or "?").strip()
        by_detector[det] = by_detector.get(det, 0) + count
    top = sorted(rows, key=lambda r: -int(r.get("occurrences") or 0))[:5]
    return {
        "groups": len(rows),
        "events": events,
        "by_detector": by_detector,
        "top": [((r.get("detector") or "?")
                 + " " + str(r.get("occurrences") or 0) + "× "
                 + (r.get("message") or "")[:90]) for r in top],
    }


def summary(root):
    path = os.path.join(root, "results-summary.json")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    keys = ("component", "version", "platform", "overall_status",
            "sanitizer_diagnostic_events", "distinct_diagnostic_groups",
            "asan_events", "ubsan_events", "lsan_reports",
            "unit_assertions", "unit_failures", "functional_files")
    out = {k: data[k] for k in keys if k in data}
    if "statuses" in data:
        out["statuses"] = data["statuses"]
    return out


def main():
    ap = argparse.ArgumentParser(description="Аудит комплекта материалов тестирования")
    ap.add_argument("root")
    ap.add_argument("--threshold", type=float, default=25.0,
                    help="порог покрытия на файл, %% (по умолчанию 25)")
    args = ap.parse_args()
    root = args.root
    if not os.path.isdir(root):
        ap.error(f"нет каталога: {root}")

    problems = 0

    man = check_manifest(root)
    if man is None:
        print("MANIFEST.sha256: нет")
    else:
        total, bad = man
        print(f"MANIFEST.sha256: записей {total}, расхождений {len(bad)}")
        for rel, why in bad:
            print(f"  {why}: {rel}")
        problems += len(bad)

    links = check_links(root)
    if links is None:
        print("index.html: нет")
    else:
        total, missing = links
        print(f"index.html: ссылок {total}, битых {len(missing)}")
        for rel in missing:
            print(f"  битая ссылка: {rel}")

    cov = coverage(root, args.threshold)
    if not cov:
        print("покрытие: summary.json не найден")
    for row in cov:
        covered, total, percent = row["line"]
        fcov, ftot, fpercent = row["function"]
        print(f"покрытие [{row['profile']}]: файлов {row['files']}, "
              f"строк {covered}/{total} = {percent}%, функций {fcov}/{ftot} = {fpercent}%, "
              f"ветви {row['branch']}% | ниже {args.threshold:g}%: {row['below']} (нулевых {row['zero']})")
        if row["worst"]:
            w = row["worst"]
            print(f"  худший: {w.get('line_percent')}% {w.get('filename')}")

    fin = findings(root)
    if fin:
        print(f"диагностики: групп {fin['groups']}, событий {fin['events']}, "
              f"по детекторам {fin['by_detector']}")
        for line in fin["top"]:
            print(f"  {line}")

    sm = summary(root)
    if sm:
        print("results-summary:")
        for key, value in sm.items():
            print(f"  {key}: {value}")
        declared = (sm.get("sanitizer_diagnostic_events") if isinstance(sm, dict) else None)
        if fin and declared is not None and declared != fin["events"]:
            print(f"  ! заявлено событий {declared}, по CSV {fin['events']}")
            problems += 1

    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
