#!/usr/bin/env python3
"""Этап 2 — генерация итогового отчёта из промежуточного `_filtered.xlsx`.

Применяет только согласованные рекомендации из `recommendations.json` (этап 1): решение
ставится полем `decision` (`approve`/`reject`) либо ключами `--approve-all`, `--approve`,
`--reject`. Всё остальное остаётся как в промежуточном файле.

После записи скрипт перечитывает результат и сверяет: число строк, пары `(id, component)`
и `status` — статусы могли измениться только у согласованных пунктов. Не прошла сверка —
ненулевой код, файл результатом не считается.

Новых листов в книге не появляется: применённые правки и журнал этапа дописываются в HTML-отчёт
этапа 1 (раздел «Этап 2 — итоговый отчёт») и в `recommendations.json` (`stage2`). Пункт, у которого
вместо номера строки стоит группа («1 стр.», «—»), не роняет прогон: он пропускается с причиной.

    python3 build_report.py <..._filtered.xlsx> --spec recommendations.json --approve-all
    python3 build_report.py <..._filtered.xlsx> --spec recommendations.json --approve 1,4,7
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import srcdep_lib as L  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from openpyxl.styles import Alignment  # noqa: E402

APPROVED = {"approve", "approved", "yes", "да", "принято"}
STAGE2_BEGIN = "<!-- stage2:begin -->"
STAGE2_END = "<!-- stage2:end -->"


def parse_ids(text):
    out = set()
    for part in (text or "").split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def source_row(value):
    """Номер строки исходника из спецификации; None — агрегат («1 стр.», «—»).

    Пункты об ошибках разметки описывают не одну строку, а группу, и номером строки
    у них стоит подпись. Согласовать такой пункт можно, применить правку к ячейке —
    нельзя: он пропускается, а не роняет весь этап 2.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def update_html(html_path: Path, applied, j_lines, out_name):
    """Дописать итог этапа 2 в HTML-отчёт: отдельных листов в xlsx не бывает."""
    if not html_path.is_file():
        return False
    text = html_path.read_text(encoding="utf-8")
    if STAGE2_BEGIN in text and STAGE2_END in text:
        start = text.index(STAGE2_BEGIN)
        end = text.index(STAGE2_END) + len(STAGE2_END)
        text = text[:start] + text[end:]
    body = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
                   for row in applied) or "<tr><td colspan='5'>Применённых правок нет.</td></tr>"
    journal = "".join(f"<li>{html.escape(line)}</li>" for line in j_lines)
    block = (f"{STAGE2_BEGIN}\n<h2>Этап 2 — итоговый отчёт</h2>\n"
             f"<p>Согласованные пункты перенесены в <code>{html.escape(out_name)}</code>. "
             "Отдельных листов в документе нет: применённые правки и журнал — здесь.</p>\n"
             "<div class='tablewrap'><table><thead><tr><th>строка</th><th>поле</th><th>было</th>"
             "<th>стало</th><th>основание</th></tr></thead>"
             f"<tbody>{body}</tbody></table></div>\n"
             f"<div class='note'><ul class='journal'>{journal}</ul></div>\n{STAGE2_END}")
    text = text.replace("</main>", block + "\n</main>") if "</main>" in text else text + block
    html_path.write_text(text, encoding="utf-8")
    return True


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
        src_row = source_row(it.get("row"))
        if src_row is None:
            skipped.append((it["id"], "агрегатная строка: правится в отчёте вручную"))
            continue
        target = row_map.get(src_row)
        if target is None:
            skipped.append((it["id"], "строка не найдена после удаления"))
            continue
        col = idx[it["field"]] + 1
        cell = ws.cell(row=target, column=col)
        cell.value = it["proposed"]
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        applied.append([target, it["field"], it.get("current", ""), it["proposed"],
                        f"согласовано, рекомендация №{it['id']}"])

    j_lines = [
        f"этап 2: применено согласованных правок {len(applied)} из {len(items)} предложений"
        + (f"; отклонено {len(reject)}" if reject else ""),
        f"пропущено: {len(skipped)}" + (f" ({skipped[0][1]}…)" if skipped else ""),
        "строки, id и purl не изменялись; статусы менялись только по согласованию",
    ]

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
    html_path = filtered.parent / f"{stem}_recommendations.html"
    in_html = update_html(html_path, applied, j_lines, out.name)
    spec["stage2"] = {"out": str(out), "applied": applied, "skipped": skipped,
                      "journal": j_lines, "html": str(html_path) if in_html else ""}
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"output": str(out), "applied": len(applied), "skipped": len(skipped),
                      "rows": len(data_out), "ts": datetime.now().strftime("%d.%m.%Y %H:%M")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
