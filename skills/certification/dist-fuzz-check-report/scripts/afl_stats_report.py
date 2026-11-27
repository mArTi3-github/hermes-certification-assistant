#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сводка по инстансам AFL++ из каталога misc/<ID>/ (fuzzer_stats + plot_data).

Печатает Markdown-таблицу и расчёт критериев завершения по Методике ВУ
(пп. 5 «а»/«б», 6) для одной фаззинг-цели.

Использование:
    afl_stats_report.py <misc_dir> [--seeds N | --engine-log FILE] [--json OUT]

--engine-log читает строки "Loaded a total of N seeds" из журнала фаззера,
чтобы взять число стартовых образцов без ручного ввода.
"""
import argparse
import glob
import json
import os
import re
import sys


def read_stats(path):
    st = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if ":" in line:
                k, v = line.split(":", 1)
                st[k.strip()] = v.strip()
    return st


def read_setup(path):
    sched, role = "—", "secondary"
    if not os.path.exists(path):
        return sched, role
    text = open(path, encoding="utf-8", errors="replace").read()
    m = re.search(r"'-p' '([\w-]+)'", text)
    if m:
        sched = m.group(1)
    if "'-M'" in text:
        role = "master"
    return sched, role


def first_plot_cvg(path):
    """Первая точка plot_data: (секунды, покрытие %)."""
    if not os.path.exists(path):
        return None, None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = [p.strip() for p in line.split(",")]
            try:
                return int(parts[0]), float(parts[6].rstrip("%"))
            except (ValueError, IndexError):
                return None, None
    return None, None


def seeds_from_log(path):
    if not path or not os.path.exists(path):
        return None
    nums = re.findall(r"Loaded a total of (\d+) seeds", open(path, encoding="utf-8", errors="replace").read())
    return int(nums[0]) if nums else None


def ru(x, nd=2):
    return f"{x:.{nd}f}".replace(".", ",")


def fmt(n):
    return f"{int(n):,}".replace(",", " ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("misc_dir")
    ap.add_argument("--seeds", type=int, default=None, help="число стартовых образцов (если нет журнала)")
    ap.add_argument("--engine-log", default=None, help="журнал фаззера для автоопределения seeds")
    ap.add_argument("--json", default=None, help="сохранить собранные данные в JSON")
    a = ap.parse_args()

    dirs = sorted(d for d in glob.glob(os.path.join(a.misc_dir, "*")) if os.path.isdir(d)
                  and os.path.exists(os.path.join(d, "fuzzer_stats")))
    if not dirs:
        sys.exit(f"нет каталогов с fuzzer_stats в {a.misc_dir}")

    rows = []
    for d in dirs:
        st = read_stats(os.path.join(d, "fuzzer_stats"))
        sched, role = read_setup(os.path.join(d, "fuzzer_setup"))
        t0, cvg0 = first_plot_cvg(os.path.join(d, "plot_data"))
        rows.append(dict(id=os.path.basename(d), sched=sched, role=role, stats=st,
                         first_t=t0, first_cvg=cvg0))

    seeds = a.seeds if a.seeds is not None else seeds_from_log(a.engine_log)

    print("| Инстанс | Роль | Power schedule | Время, ч | Исполнений | Корпус | Путей | Покрытие | Без новых путей, ч | Сбои | Зависания |")
    print("|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for r in rows:
        s = r["stats"]
        print("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            r["id"], r["role"], r["sched"],
            ru(int(s.get("run_time", 0)) / 3600),
            fmt(s.get("execs_done", 0)),
            fmt(s.get("corpus_count", 0)),
            fmt(s.get("edges_found", 0)),
            s.get("bitmap_cvg", "—").replace(".", ","),
            ru(int(s.get("time_wo_finds", 0)) / 3600),
            s.get("saved_crashes", "—"), s.get("saved_hangs", "—")))

    execs = sum(int(r["stats"].get("execs_done", 0)) for r in rows)
    edges = max(int(r["stats"].get("edges_found", 0)) for r in rows)
    run_h = [int(r["stats"].get("run_time", 0)) / 3600 for r in rows]
    twf_h = [int(r["stats"].get("time_wo_finds", 0)) / 3600 for r in rows]
    crashes = sum(int(r["stats"].get("saved_crashes", 0)) for r in rows)
    hangs = sum(int(r["stats"].get("saved_hangs", 0)) for r in rows)
    tmouts = sum(int(r["stats"].get("total_tmout", 0)) for r in rows)

    print()
    print(f"Инстансов: {len(rows)}; исполнений всего: {fmt(execs)}; "
          f"время работы: {ru(min(run_h))}–{ru(max(run_h))} ч")
    print(f"Максимум уникальных путей: {fmt(edges)}; сбоев: {crashes}; "
          f"зависаний: {hangs}; таймаутов (total_tmout): {tmouts}")
    print()
    print("**Пункт 6 (≥ 2 ч без новых путей):** "
          + ("выполняется" if min(twf_h) >= 2 else "НЕ выполняется")
          + f" — time_wo_finds {ru(min(twf_h))}–{ru(max(twf_h))} ч")

    if seeds:
        ok = edges >= 2 * seeds
        print(f"**Пункт 5 «а»:** путей {fmt(edges)} против {fmt(seeds)} стартовых образцов → "
              f"{'выполняется' if ok else 'НЕ выполняется'} "
              f"(порог {fmt(2 * seeds)}; кратность {edges / seeds:.1f}×)")
        if seeds <= 1:
            print("  ! критерий вырожден: при одном стартовом образце «а» выполняется всегда — "
                  "проверять достаточность фаззинга по «б» и абсолютному покрытию")
    else:
        print("**Пункт 5 «а»:** число стартовых образцов неизвестно "
              "(укажите --seeds или --engine-log)")

    ratios = []
    for r in rows:
        final = r["stats"].get("bitmap_cvg", "").rstrip("%")
        if r["first_cvg"] and final:
            try:
                ratios.append(float(final) / r["first_cvg"])
            except ValueError:
                pass
    if ratios:
        print(f"**Пункт 5 «б»:** отношение «финал / первая точка plot_data» = "
              f"{ru(min(ratios))}–{ru(max(ratios))}× → "
              f"{'выполняется' if min(ratios) >= 2 else 'НЕ подтверждено (порог 2×)'}")
        print("  оговорка: первая точка plot_data уже включает результаты мутаций; покрытие "
              "строго от стартовых образцов надо требовать у разработчика")

    if a.json:
        out = dict(seeds=seeds, instances=rows, execs_total=execs, edges_max=edges,
                   crashes=crashes, hangs=hangs, time_wo_finds_h=[min(twf_h), max(twf_h)])
        json.dump(out, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\nJSON: {a.json}")


if __name__ == "__main__":
    main()
