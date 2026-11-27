#!/usr/bin/env python3
"""Сводка покрытия по LCOV .info: строки/функции/ветви, подытоги по каталогам,
агрегат и зазор до порога «90 % строк» Методики ВУ.

Проценты в genhtml/LCOV печатаются с округлением до одного знака, поэтому
долю 1124/1249 (89,99 %) видно как «90.0 %». Считать надо по счётчикам
LF/LH/FNF/FNH/BRF/BRH из .info, а не по HTML: таблицы genhtml вложенные, и
наивный разбор <tr> даёт имена файлов без чисел.

  python3 coverage_info_summary.py <coverage_dir|file.info> [--markdown]

--markdown печатает готовую таблицу для раздела 5 заключения (без подытогов).
Код возврата 1, если агрегат по строкам ниже порога.
"""
import argparse
import math
import os
import sys

COUNTERS = ("LF", "LH", "FNF", "FNH", "BRF", "BRH")


def parse_info(path):
    files, cur = [], None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("SF:"):
                cur = {"name": line[3:], **{k: 0 for k in COUNTERS}}
                files.append(cur)
                continue
            if cur is None:
                continue
            if line == "end_of_record":
                cur = None
                continue
            key, sep, value = line.partition(":")
            if sep and key in COUNTERS and value.isdigit():
                cur[key] = int(value)
    return files


def pct(hit, total):
    return 100.0 * hit / total if total else 0.0


def aggregate(rows):
    return {k: sum(r[k] for r in rows) for k in COUNTERS}


def short(path):
    for prefix in ("/src/src/", "/src/", "src/"):
        if path.startswith(prefix):
            return path[len(prefix):]
    return os.path.basename(path)


def md_cell(hit, total):
    if not total:
        return "—"
    return "%d/%d (%.1f %%)" % (hit, total, pct(hit, total))


def print_table(rows, markdown=False):
    if markdown:
        print("| Файл | Строки | Функции | Ветви |")
        print("|---|---:|---:|---:|")
    for r in rows:
        name = short(r["name"]) if markdown else r["name"]
        cells = [md_cell(r["LH"], r["LF"]),
                 md_cell(r["FNH"], r["FNF"]),
                 md_cell(r["BRH"], r["BRF"]) if r["BRF"] else "нет данных"]
        if markdown:
            print("| `%s` | %s | %s | %s |" % (name, *cells))
        else:
            print("%-45s %-22s %-16s %s" % (name, *cells))
    tot = aggregate(rows)
    if markdown:
        print("| **Итого** | **%s** | **%s** | **%s** |" % (
            md_cell(tot["LH"], tot["LF"]),
            md_cell(tot["FNH"], tot["FNF"]),
            md_cell(tot["BRH"], tot["BRF"]) if tot["BRF"] else "нет данных"))
    else:
        print("-" * 100)
        print("АГРЕГАТ строк %d/%d = %.4f %% (в HTML печатается как %.1f %%)" % (
            tot["LH"], tot["LF"], pct(tot["LH"], tot["LF"]),
            round(pct(tot["LH"], tot["LF"]), 1)))
        if tot["FNF"]:
            print("АГРЕГАТ функций %d/%d = %.1f %%" % (
                tot["FNH"], tot["FNF"], pct(tot["FNH"], tot["FNF"])))
        if tot["BRF"]:
            print("АГРЕГАТ ветвей %d/%d = %.1f %%" % (
                tot["BRH"], tot["BRF"], pct(tot["BRH"], tot["BRF"])))
        else:
            print("Ветви: данных в .info нет — вторую половину льготы "
                  "(80 % базовых блоков) по такому отчёту оценить нельзя")
    return tot


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("src", help="каталог с *.info или один файл .info")
    ap.add_argument("--markdown", action="store_true",
                    help="печатать таблицу в markdown-разметке")
    ap.add_argument("--threshold", type=float, default=90.0,
                    help="порог покрытия строк, %% (по умолчанию 90)")
    args = ap.parse_args()

    if os.path.isdir(args.src):
        infos = [os.path.join(args.src, n) for n in sorted(os.listdir(args.src))
                 if n.endswith(".info")]
    else:
        infos = [args.src]
    if not infos:
        sys.exit("файлов .info не найдено: %s" % args.src)

    worst = None
    for path in infos:
        rows = parse_info(path)
        if not rows:
            print("!! %s: ни одной записи SF — формат не LCOV" % path)
            continue
        if len(infos) > 1:
            print("\n### %s" % os.path.basename(path))
        tot = print_table(rows, markdown=args.markdown)
        val = pct(tot["LH"], tot["LF"])
        worst = val if worst is None else min(worst, val)
        if args.markdown or not tot["LF"]:
            continue

        # подытоги по каталогам: показывают, чем закрывается льгота (src vs src/posix)
        dirs = {}
        for r in rows:
            dirs.setdefault(os.path.dirname(r["name"]) or ".", []).append(r)
        if len(dirs) > 1:
            print("\nПодытоги по каталогам:")
            for d in sorted(dirs):
                sub = aggregate(dirs[d])
                print("  %-40s %d/%d = %.2f %% строк" % (
                    d, sub["LH"], sub["LF"], pct(sub["LH"], sub["LF"])))

        # порог: сколько покрытых строк нужно, чтобы «не менее threshold %» выполнилось
        needed_total = math.ceil(tot["LF"] * args.threshold / 100.0 - 1e-9)
        need = needed_total - tot["LH"]
        if need > 0:
            print("\nДО ПОРОГА %.0f %% НЕ ХВАТАЕТ: нужно %d/%d (%.2f %%), "
                  "т.е. ещё %d" % (args.threshold, needed_total, tot["LF"],
                                    pct(needed_total, tot["LF"]), need))
        else:
            print("\nПорог %.0f %% по строкам выполнен с запасом %d строк."
                  % (args.threshold, -need))

    sys.exit(1 if worst is not None and worst < args.threshold else 0)


if __name__ == "__main__":
    main()
