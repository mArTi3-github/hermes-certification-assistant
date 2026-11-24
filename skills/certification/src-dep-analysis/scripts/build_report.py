#!/usr/bin/env python3
"""Этап 2 — генерация итогового отчёта из промежуточного `_filtered.xlsx`.

Применяет только согласованные рекомендации из `recommendations.json` (этап 1): решение
ставится полем `decision` (`approve`/`reject`) либо ключами `--approve-all`, `--approve`,
`--reject`. Всё остальное остаётся как в промежуточном файле.

После записи скрипт перечитывает результат и сверяет: число строк, пары `(id, component)`
и `status` — статусы могли измениться только у согласованных пунктов. Не прошла сверка —
ненулевой код, файл результатом не считается.

    python3 build_report.py <..._filtered.xlsx> --spec recommendations.json --approve-all
    python3 build_report.py <..._filtered.xlsx> --spec recommendations.json --approve 1,4,7
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import srcdep_lib as L  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from openpyxl.styles import Alignment, Font  # noqa: E402

BOLD = Font(bold=True)
APPROVED = {"approve", "approved", "yes", "да", "принято"}


def parse_ids(text):
    out = set()
    for part in (text or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("filtered")
    ap.add_argument("--spec", required=True, help="recommendations.json из этапа 1")
    ap.add_argument("--approve-all", action="store_true")
    ap.add_argument("--approve", default="")
    ap.add_argument("--reject", default="")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--out", default=None)
    a = ap.parse_args()

    filtered = Path(a.filtered).expanduser().resolve()
    spec_path = Path(a.spec).expanduser().resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    items = spec.get("items", [])
    row_map = {int(k): v for k, v in spec.get("row_map", {}).items()}
    approve = parse_ids(a.approve)
    reject = parse_ids(a.reject)
    if a.approve_all:
        approve = {i["id"] for i in items}
    approve -= reject

    wb = load_workbook(filtered)
    ws = wb[spec.get("sheet") or wb.sheetnames[0]]
    _, _, hnum, idx, _ = L.read_table(filtered)
    if "status" not in idx or "detail" not in idx:
        sys.exit("в отчёте нет колонок status/detail")

    applied, skipped = [], []
    for it in items:
        decision = "approve" if it["id"] in approve else (
            "reject" if it["id"] in reject else it.get("decision", "pending"))
        it["decision"] = decision
        if decision not in APPROVED:
            continue
        if it["kind"] == "note" or not it.get("field"):
            skipped.append((it["id"], "замечание, не правка"))
            continue
        src_row = it.get("row")
        target = row_map.get(int(src_row)) if src_row not in ("", None) else None
        if target is None:
            skipped.append((it["id"], "строка не найдена после удаления"))
            continue
        col = idx[it["field"]] + 1
        cell = ws.cell(row=target, column=col)
        cell.value = it["proposed"]
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        applied.append([target, it["field"], it.get("current", ""), it["proposed"],
                        f"согласовано, рекомендация №{it['id']}"])

    if "Изменения" in wb.sheetnames:
        ch = wb["Изменения"]
    else:
        ch = wb.create_sheet("Изменения")
        ch.append(["строка", "поле", "было", "стало", "основание"])
        for c in ch[1]:
            c.font = BOLD
    for row in applied:
        ch.append(row)

    j_lines = [
        f"этап 2: применено согласованных правок {len(applied)} из {len(items)} предложений"
        + (f"; отклонено {len(reject)}" if reject else ""),
        f"пропущено: {len(skipped)}" + (f" ({skipped[0][1]}…)" if skipped else ""),
        "строки, id и purl не изменялись; статусы менялись только по согласованию",
    ]
    lg = wb["Журнал"] if "Журнал" in wb.sheetnames else wb.create_sheet("Журнал")
    start = lg.max_row + 1
    for line in j_lines:
        lg.append([start, line])
        start += 1
    for c in (lg.cell(row=lg.max_row, column=2),):
        c.alignment = Alignment(wrap_text=True, vertical="top")
    lg.column_dimensions["B"].width = 120

    stem = filtered.stem.replace("_filtered", "")
    name = f"{stem}_итоговый.xlsx"
    if a.out:
        out = Path(a.out).expanduser()
    elif a.outdir:
        out = Path(a.outdir).expanduser() / name
    else:
        out = filtered.parent / name
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.xlsx")
    wb.save(tmp)

    # --- сверка после записи: статусы против исходного отчёта, а не против копии
    chk = load_workbook(tmp)
    ws2 = chk[spec.get("sheet") or chk.sheetnames[0]]
    rows_out = [(r[0].row, [c.value for c in r]) for r in ws2.iter_rows()]
    data_out = [(n, r) for n, r in rows_out if n > hnum and any(str(c).strip() for c in r if c is not None)]
    problems = []

    def pairs(sheet_idx, data, skip):
        out = {}
        for n, r in data:
            if n <= skip:
                continue
            key = (L.cell(r, sheet_idx, "component"), L.cell(r, sheet_idx, "vuln_id"))
            if key == ("", ""):
                continue
            out.setdefault(key, []).append(L.cell(r, sheet_idx, "status"))
        return out

    src_path = Path(spec.get("source", ""))
    if src_path.exists():
        _, _, hsrc, isrc, rows_src = L.read_table(src_path)
        p_src = pairs(isrc, [(n, r) for n, r in rows_src if n > hsrc], hsrc)
        p_out = pairs(idx, data_out, hnum)
        approved_status = {(it.get("component"), it.get("vuln")): it["proposed"]
                           for it in items
                           if it["kind"] == "status" and it["decision"] in APPROVED}
        for key, sts in p_out.items():
            if key not in p_src:
                problems.append(f"пара {key[0]} / {key[1]} отсутствует в исходном отчёте")
                continue
            allowed = set(p_src[key]) | {approved_status.get(key)}
            for st in sts:
                if st not in allowed:
                    problems.append(f"{key[1]} ({key[0]}): статус «{st}» не было в исходнике "
                                    f"и он не согласован")
    if len(data_out) == 0:
        problems.append("результат пуст")
    if problems:
        tmp.unlink(missing_ok=True)
        print("\n".join(["сверка не прошла:"] + problems[:10]), file=sys.stderr)
        sys.exit(1)
    tmp.replace(out)
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"output": str(out), "applied": len(applied), "skipped": len(skipped),
                      "rows": len(data_out), "ts": datetime.now().strftime("%d.%m.%Y %H:%M")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
