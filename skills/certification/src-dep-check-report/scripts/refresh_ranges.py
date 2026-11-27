#!/usr/bin/env python3
"""Собрать диапазоны уязвимых версий по всем CVE отчёта в `references/version-ranges.json`.

Зачем: прогон этапа 1 не должен зависеть от доступности NVD. Файл — офлайн-страховка;
он же переиспользуется при следующем отчёте с теми же CVE.

NVD без ключа пускает 5 запросов за 30 секунд, поэтому запросы идут с паузой через
`srcdep_lib.nvd_intervals` (кэш `~/.cache/src-dep-check-report`). На отчёт с 70+ CVE — несколько
минут: запускать в фоне (`terminal(background=true, notify=True)`), а не в foreground-таймауте.

    python3 refresh_ranges.py <отчёт.xlsx> [--force]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SKILL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SKILL / "scripts"))
import srcdep_lib as L  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source")
    ap.add_argument("--force", action="store_true", help="перезапросить уже закэшированные CVE")
    ap.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "src-dep-check-report"))
    a = ap.parse_args()

    src = Path(a.source).expanduser()
    _, _, hnum, idx, rows = L.read_table(src)
    cves = sorted({L.cell(r, idx, "vuln_id") for n, r in rows if n > hnum
                   and L.cell(r, idx, "vuln_id").upper().startswith("CVE-")})
    if not cves:
        sys.exit("в отчёте нет CVE")

    out = SKILL / "references" / "version-ranges.json"
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    cache = Path(a.cache_dir)

    for cve in cves:
        if cve in data and not a.force and (cache / f"{cve}.json").exists():
            continue
        intervals = L.nvd_intervals(cve, cache, force=a.force)
        data[cve] = {"ranges": intervals or [], "source": "NVD cpeMatch",
                     "checked": date.today().isoformat()}
        print(cve, len(intervals or []), flush=True)

    out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    empty = sum(1 for v in data.values() if not v.get("ranges"))
    print(json.dumps({"cves": len(cves), "записей": len(data), "без диапазонов": empty,
                      "output": str(out)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
