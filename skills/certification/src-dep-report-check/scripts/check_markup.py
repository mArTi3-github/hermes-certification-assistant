#!/usr/bin/env python3
"""Проверка актуальности и адекватности разметки отчёта.

Что ищет: утверждения о применимости («актуальна», «эксплуатация возможна», «подтверждена»),
статус против смысла обоснования, версию в тексте против версии компонента, короткие
обоснования без основания, пары (компонент, id) с расходящимися текстами.

    python3 check_markup.py <исходный.xlsx> <…_filtered.xlsx>
"""
import argparse
import re
import sys
from collections import defaultdict

from openpyxl import load_workbook

import argparse

AFFIRM = re.compile(
    r"(актуальн\w*|применим\w*|эксплуатация\s+возможна|эксплуатация\s+не\s+требует|"
    r"эксплуатир\w*|воспроизводится|подтвержд\w*|содержит\s+уязвимост\w*|"
    r"уязвимость\s+не\s+устранен\w*|затронут\w*\s+(?:продукт|компонент))",
    re.I,
)
NEG = re.compile(r"(не\s*актуальн|не\s*применим|неэксплуатир|не\s*эксплуатир|невозможн|"
                 r"недостижим|недоступн|не\s+воспроизвод|не\s+подтвержд|не\s+задейств|"
                 r"не\s+использ|не\s+вызыва|не\s+выполня|не\s+вел|не\s+участву|отсутству|"
                 r"не\s+содержит|не\s+затронут|вне\s+этого\s+диапазона|ложная)", re.I)
UNREACH = re.compile(r"(не\s+использ|недостижим|недоступ|не\s+участвует|не\s+вел|не\s+вызыва|"
                     r"не\s+выполня|терминир|только\s+клиент|не\s+задейств)",
                     re.I)
WRONGID = re.compile(r"(относится\s+к\s+|а\s+не\s+к\s+|ложное\s+срабатывание|содержит\s+исправление|"
                     r"уязвимая\s+версия\s+не\s+поставляется|версий\s+до)", re.I)

CLAIMS = [
    ("fixed_in", re.compile(r"Исправлено(?:\s+в)?\s+([0-9][0-9A-Za-z.+~:-]*)", re.I)),
    ("fixed_for", re.compile(r"Исправлено\s+для\s+([0-9][0-9A-Za-z.+~:-]*)", re.I)),
    ("not_actual", re.compile(r"Неактуально\s+для\s+[^\s@]+@([^\s,;]+)", re.I)),
    ("vuln_below", re.compile(r"уязвимы\s+версии\s+до\s+([0-9][^\s,]*)", re.I)),
    ("used", re.compile(r"используется\s+([0-9][^\s,]*)", re.I)),
]


def num(v):
    return tuple(int(x) for x in (re.findall(r"\d+", v or "") or [])[:3])


def comp_version(purl):
    core = (purl or "").split("?")[0]
    if "@" not in core:
        return ""
    return core.rsplit("@", 1)[1].replace("%2B", "+").replace("%3A", ":")


def load(path):
    wb = load_workbook(path, read_only=True)
    ws = wb.worksheets[0]
    out = []
    for i, r in enumerate(ws.iter_rows(min_row=3, values_only=True), start=3):
        if not any(r):
            continue
        out.append((i, ["" if v is None else str(v) for v in r]))
    return out


def sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?;])\s+", text) if s.strip()]


def main():
    ap = argparse.ArgumentParser(description="Проверка актуальности и адекватности разметки в готовом отчёте")
    ap.add_argument("source", nargs="?", help="исходный отчёт (для сравнения состава)")
    ap.add_argument("filtered", nargs="?", help="проверяемый отчёт (_filtered)")
    a = ap.parse_args()
    if not (a.source and a.filtered):
        sys.exit("укажите два пути: <исходный.xlsx> <…_filtered.xlsx>")
    src_path, out_path = a.source, a.filtered
    src, out = load(src_path), load(out_path)
    print(f"строк: исходник {len(src)}, результат {len(out)}\n")

    affirm, mismatch, weak, ver_claim = [], [], [], []
    for i, r in out:
        comp, vid, status, detail = r[3], r[4], (r[6] or "").lower(), (r[7] or "").strip()
        for s in sentences(detail):
            probe = re.sub(r"актуальн\w*\s+верси\w*", "", s, flags=re.I)
            if AFFIRM.search(probe) and not NEG.search(probe):
                affirm.append((i, vid, comp, status, s))
                break
        un, wi = bool(UNREACH.search(detail)), bool(WRONGID.search(detail))
        if status == "false_positive" and un and not wi:
            mismatch.append((i, vid, comp, "fp, обоснование про недостижимость", detail[:120]))
        if status == "not_affected" and wi:
            mismatch.append((i, vid, comp, "na, обоснование про ошибочную идентификацию", detail[:120]))
        if len(detail) < 40 and not re.search(r"Исправлено|уязвимы версии|дефект до", detail, re.I):
            weak.append((i, vid, comp, status, detail))
        cv = comp_version(comp)
        for kind, rx in CLAIMS:
            m = rx.search(detail)
            if not m:
                continue
            claimed = m.group(1)
            if num(cv) and num(claimed) and num(cv)[:2] != num(claimed)[:2]:
                ver_claim.append((i, vid, comp, cv, f"{kind}: {claimed}"))

    def show(title, items, f):
        print(f"{title}: {len(items)}")
        for x in items[:12]:
            print("   ", f(x))
        if len(items) > 12:
            print(f"    … ещё {len(items) - 12}")
        print()

    show("УТВЕРЖДЕНИЕ О ПРИМЕНИМОСТИ (показать человеку)", affirm,
         lambda x: f"r{x[0]} {x[1]} {x[2][:30]} [{x[3]}] «{x[4][:150]}»")
    show("СТАТУС ПРОТИВ СМЫСЛА ОБОСНОВАНИЯ", mismatch,
         lambda x: f"r{x[0]} {x[1]} {x[2][:30]} — {x[3]}")
    show("ВЕРСИЯ В ТЕКСТЕ ≠ ВЕРСИЯ КОМПОНЕНТА", ver_claim,
         lambda x: f"r{x[0]} {x[1]} {x[2][:34]} — компонент {x[3]}, текст: {x[4]}")
    show("КОРОТКО И БЕЗ ОСНОВАНИЯ", weak, lambda x: f"r{x[0]} {x[1]} {x[2][:30]} [{x[3]}] «{x[4][:70]}»")

    groups = defaultdict(list)
    for i, r in out:
        groups[(r[3], r[4])].append((i, (r[6] or "").lower(), (r[7] or "").strip()))
    dup = {k: v for k, v in groups.items() if len({t for _, _, t in v}) > 1}
    print(f"ПАРЫ (компонент, id) с разными обоснованиями: {len(dup)}")
    for (comp, vid), v in list(dup.items())[:5]:
        kinds = defaultdict(list)
        for n, s, t in v:
            kinds[s].append(n)
        print(f"   {vid} {comp[:36]}: " + "; ".join(f"{s}×{len(ns)}" for s, ns in kinds.items()))


INPUT = [""]


if __name__ == "__main__":
    main()
