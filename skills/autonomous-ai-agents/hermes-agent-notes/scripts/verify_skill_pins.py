#!/usr/bin/env python3
"""Симулировать детерминированный свип куратора на N дней вперёд по указанным скиллам
и показать, пометит ли он их stale/archived.

Запуск (интерпретатор, из которого импортируется исходник Hermes):
    cd ~/.hermes/hermes-agent
    python3 <path>/scripts/verify_skill_pins.py --days 400 skill-a skill-b

Ожидаемый результат для защищённых скиллов: marked_stale: 0, archived: 0,
состояние active до и после. Скрипт трогает только перечисленные записи: список строк
подменяется, остальные скиллы свип не видит.

Почему не `hermes curator prune --dry-run`: когда managed-скиллов нет, он пуст при любых
пинах и ничего не доказывает.
"""

import argparse
import os
import sys
from datetime import UTC, datetime, timedelta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("skills", nargs="+")
    ap.add_argument("--days", type=int, default=400)
    ap.add_argument("--source", default="~/.hermes/hermes-agent")
    args = ap.parse_args()

    src = os.path.expanduser(args.source)
    if not os.path.isdir(src):
        print(f"источник Hermes не найден: {src}", file=sys.stderr)
        return 2
    sys.path.insert(0, src)

    from tools import skill_usage as su  # noqa: E402
    from agent import curator  # noqa: E402

    data = su.load_usage()
    missing = [n for n in args.skills if n not in data]
    if missing:
        print(f"нет записи в skills/.usage.json: {', '.join(missing)}", file=sys.stderr)
        return 2

    unpinned = [n for n in args.skills if not data[n].get("pinned")]
    for n in unpinned:
        print(f"ВНИМАНИЕ: '{n}' не закреплён — свип его рассмотрит", file=sys.stderr)

    rows = [su._report_row(n, data[n], _persisted=True, provenance=su.provenance(n)) for n in args.skills]
    before = {n: data[n].get("state") for n in args.skills}
    real = su.curated_report
    su.curated_report = lambda: rows
    try:
        counts = curator.apply_automatic_transitions(now=datetime.now(UTC) + timedelta(days=args.days))
    finally:
        su.curated_report = real
    after = {n: su.load_usage().get(n, {}).get("state") for n in args.skills}

    print(f"{'skill':40} {'pinned':7} {'before':8} after")
    for n in args.skills:
        print(f"{n:40} {str(data[n].get('pinned')):7} {str(before[n]):8} {after[n]}")
    print(f"свип на +{args.days} дн: {counts}")
    return 0 if counts.get("archived", 0) == 0 and counts.get("marked_stale", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
