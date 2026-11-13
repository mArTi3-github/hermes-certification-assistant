#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Реконструкция динамики покрытия по журналу фаззера, когда fuzzer_stats нет.

AFL++ пишет строки вида
    [*] Fuzzing test case #12 (152 total, 0 crashes saved, ...) ... run_time=0:00:00:10, cvg=3.05%
Журнал ptfuzz склеен из stdout нескольких инстансов, каждый блок начинается
заголовком "==> /work/engine.LA000.stdout.txt <==". Скрипт режет журнал на блоки,
для каждого считает максимальное покрытие, время последнего прироста и плато —
это и есть проверка пункта 6, когда fuzzer_stats не приложены.

Использование:
    afl_log_timeline.py <engine.log> [--section-regex REGEX] [--min-plateau 2]

Код возврата 1, если хотя бы у одного блока плато меньше --min-plateau часов.
"""
import argparse
import re
import sys

DEFAULT_SECTION = r"==>\s*\S*?(LA\d+)\S*\s*<=="
LINE_PAT = re.compile(r"run_time=(\d+):(\d+):(\d+):(\d+).*?cvg=([\d.]+)%")


def secs(d, h, m, s):
    return int(d) * 86400 + int(h) * 3600 + int(m) * 60 + int(s)


def ru(x, nd=2):
    return f"{x:.{nd}f}".replace(".", ",")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--section-regex", default=DEFAULT_SECTION)
    ap.add_argument("--min-plateau", type=float, default=2.0)
    a = ap.parse_args()

    sec = re.compile(a.section_regex)
    curves = {}
    cur = None
    with open(a.log, encoding="utf-8", errors="replace") as f:
        for line in f:
            m = sec.search(line)
            if m:
                cur = m.group(1)
                curves.setdefault(cur, [])
                continue
            m2 = LINE_PAT.search(line)
            if m2 and cur is not None:
                curves[cur].append((secs(*m2.group(1, 2, 3, 4)), float(m2.group(5))))

    curves = {k: v for k, v in curves.items() if v}
    if not curves:
        sys.exit("не найдено ни блоков-секций, ни строк с run_time=... cvg=... "
                 "— проверьте --section-regex")

    print("| Инстанс | Записей | Покрытие в конце, % | Максимум, % | Время, ч | Плато, ч |")
    print("|---|---:|---:|---:|---:|---:|")
    bad = []
    for k in sorted(curves):
        v = curves[k]
        maxt = max(t for t, _ in v)
        prev, last_change = None, None
        for t, c in v:
            if prev is not None and c != prev:
                last_change = t
            prev = c
        plateau = (maxt - (last_change or 0)) / 3600
        if plateau < a.min_plateau:
            bad.append((k, plateau))
        print(f"| {k} | {len(v)} | {ru(v[-1][1])} | {ru(max(c for _, c in v))} | "
              f"{ru(maxt / 3600)} | {ru(plateau)} |")

    total = max(max(t for t, _ in v) for v in curves.values()) / 3600
    print(f"\nОбщее время работы: {ru(total)} ч; блоков: {len(curves)}; "
          f"порог плато: {ru(a.min_plateau, 1)} ч")
    if bad:
        print("Пункт 6 НЕ выполняется для: " + ", ".join(f"{k} ({ru(p)} ч)" for k, p in bad))
        sys.exit(1)
    print("Пункт 6: у всех блоков плато без прироста покрытия не меньше порога")


if __name__ == "__main__":
    main()
