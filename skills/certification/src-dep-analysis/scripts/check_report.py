#!/usr/bin/env python3
"""Этап 1 — правка отчёта: промежуточный `_filtered.xlsx` + HTML-отчёт с рекомендациями.

Что делает:
  1) удаляет строки, у которых severity не `high`/`critical` (лишние компоненты);
  2) правит орфографию и формат в `detail` (без согласования, с записью в «Изменения»);
  3) собирает рекомендации — смена статуса, правка обоснований, замечания к разметке —
     и складывает их в отдельный HTML-отчёт `recommendations.html`. Машиночитаемая копия
     пунктов живёт вне сдачи (`~/.cache/src-dep-analysis/work/<имя>.json`) — её читает этап 2.

Ничего, кроме орфографии и удаления строк, в отчёте не меняется: статусы, `id` и purl
остаются авторскими до согласования.

    python3 check_report.py <отчёт.xlsx|.csv> [--outdir <шара>/SRC_DEP_analys] [--no-nvd]
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import srcdep_lib as L  # noqa: E402
from openpyxl import load_workbook  # noqa: E402
from openpyxl.styles import Alignment, Font  # noqa: E402

BOLD = Font(bold=True)

# дополнения оборванных формулировок: CVE → готовый текст
COMPLETIONS = {
    "CVE-2019-8341": "Уязвимость помечена как DISPUTED (оспаривается поставщиком): по описанию "
                     "эксплуатация требует контроля над содержимым шаблона.",
    "CVE-2026-27820": "Уязвимость относится к Ruby-биндингу zlib (Zlib::GzipReader), а не к "
                      "поставленному компоненту zlib для Alpine — ложное срабатывание сканера.",
    "CVE-2022-32511": "Уязвимость относится к библиотеке JMESPath для Ruby, а не к поставленному "
                      "компоненту jmespath для Python — ложное срабатывание сканера.",
    "CVE-2020-1171": "Уязвимость относится к расширению Python для Visual Studio Code (Microsoft), "
                     "а не к поставленному интерпретатору CPython — ложное срабатывание сканера.",
    "CVE-2014-125074": "Описание CVE-2014-125074 относится к Java-приложению «Nayshlok Voyager», "
                       "а не к пакету voyager для Python — ложное срабатывание сканера.",
}


def propose_status(comp, ver, vid, status, det, intervals, notes):
    """Рекомендация по статусу или замечание. Возвращает dict|None."""
    base = {"component": comp, "vuln": vid, "status": status, "current": status}
    if intervals:
        affected = L.in_ranges(ver, intervals)
        rng = L.ranges_text(intervals)
        eff = L.effective_version(ver)
        shown = f"{eff} (в упаковке {ver})" if eff != ver else ver
        if not affected and status != "false_positive":
            return {**base, "kind": "status", "field": "status", "proposed": "false_positive",
                    "basis": f"поставленная версия {shown} вне затронутого диапазона ({rng}), "
                             f"уязвимого кода в поставке нет",
                    "source": f"NVD cpeMatch, {vid}", "strength": "сильная"}
        if affected and status == "false_positive" and not L.WRONG_PRODUCT.match(det or ""):
            return {**base, "kind": "note", "field": "",
                    "proposed": "провести дополнительный анализ",
                    "basis": f"поставленная версия {shown} входит в затронутый диапазон ({rng}), "
                             f"статус false_positive версией не обоснован",
                    "source": f"NVD cpeMatch, {vid}", "strength": "необходимо утвердить"}
    if L.WRONG_PRODUCT.match(det or "") and status != "false_positive":
        return {**base, "kind": "status", "field": "status", "proposed": "false_positive",
                "basis": f"описание {vid} относится к другому продукту",
                "source": f"описание CVE, {vid}", "strength": "сильная"}
    return None


def propose_detail(comp, ver, vid, det):
    """Рекомендация по тексту обоснования. Возвращает dict|None.

    Утверждения о неиспользуемости компонента не добавляются: «не используется» — законное
    обоснование автора отчёта, и проверяющий не должен получать выдуманные доводы.
    """
    name = comp.split("/")[-1].split("@")[0] if comp else "компонента"
    if not det:
        return {"kind": "detail", "field": "detail",
                "proposed": f"Уязвимость заявлена для {name}, поставленного в составе продукта; "
                            f"необходимо заполнить обоснование.",
                "basis": "поле пустое", "source": "правило 3", "strength": "необходимо утвердить"}
    if vid in COMPLETIONS and (L.is_broken(det) or L.WRONG_PRODUCT.match(det)):
        return {"kind": "detail", "field": "detail", "proposed": COMPLETIONS[vid],
                "basis": "формулировка оборвана или называет чужой продукт",
                "source": f"контекст {vid}", "strength": "сильная"}
    m = L.FIXED_FRAGMENT.match(det)
    if m:
        return {"kind": "detail", "field": "detail",
                "proposed": f"Уязвимость исправлена в версии {m.group(1)}; поставленная версия {ver} "
                            f"содержит исправление, уязвимого кода в поставке нет.",
                "basis": "обрывок без подлежащего: не сказано, что и где исправлено",
                "source": "контекст отчёта", "strength": "необходимо утвердить"}
    # обрыв без готового контекста не дописывается: придумывать «недостижимость» нельзя

    return None


NON_USE_PROPOSAL = ("переписать обоснование: ссылка на неиспользуемость компонента не является "
                    "проверяемым основанием")


def check_non_use(comp, vid, det):
    """«Не используется» в обосновании — ошибка разметки, а не законная позиция."""
    if det and L.NON_USE.search(det):
        return {"kind": "error", "field": "detail", "level": "error",
                "proposed": NON_USE_PROPOSAL,
                "basis": f"обоснование построено на неиспользуемости компонента: «{det[:120]}»",
                "source": "правило о неиспользуемости", "strength": "ошибка"}
    return None


def check_affirmative(comp, ver, vid, status, det):
    if det and L.AFFIRMATIVE.search(det) and status in {"false_positive", "not_affected"}:
        return {"kind": "note", "field": "detail", "proposed": "провести дополнительный анализ",
                "basis": f"обоснование утверждает применимость/возможную эксплуатацию при статусе "
                         f"{status}: «{det[:120]}»",
                "source": "правило о явном показе утверждений", "strength": "необходимо утвердить"}
    return None


def check_version_text(comp, ver, vid, det):
    """Версия в тексте не совпадает с версией компонента (сравнение по major.minor).

    `1.3.1-r2` против `1.3.1.2` — одна и та же версия, это не расхождение; `17.7-0+deb13u1`
    против `18.1-1.pgdg13+2` — расхождение, текст говорит о другой ветке.
    """
    if not det:
        return None
    # текст прямо называет версию компонента (в т.ч. в Debian-упаковке) — сравнивать нечего
    if ver and (str(ver).split("+")[0].split("-")[0] in det or L.effective_version(str(ver)) in det):
        return None
    own = L.ver_tuple(ver)
    own_variants = {own, L.ver_tuple(str(ver).split("+")[0])}   # `2.12.7+dfsg+really2.9.14`: 2.12.7 тоже верно
    if not own:
        return None
    for m in L.VERSION_IN_TEXT.finditer(det):
        cand = m.group(1)
        prefix = det[max(0, m.start() - 30):m.start()].lower()
        # «уязвимы версии до 1.6.5», «исправлено в 1.2.9» — это граница диапазона, а не версия компонента
        if any(w in prefix for w in ("до ", "ранее", "ниже", "исправлен", "затронут", "уязвим",
                                     "начиная", "версии старше", "версий до")):
            continue
        t = L.ver_tuple(cand)
        if len(t) < 2 or any(t == o for o in own_variants) or any(t[:2] == o[:2] for o in own_variants):
            continue
        return {"kind": "note", "field": "detail", "proposed": "поправить версию в тексте",
                "basis": f"в тексте версия {cand}, у компонента {ver}",
                "source": "сверка текста с колонкой version",
                "strength": "необходимо утвердить"}
    return None


def build(items, meta, journal, html_path: Path, changes=None):
    def rows(kind):
        return [i for i in items if i["kind"] == kind]

    def table(kind, head, cols, cls=""):
        data = rows(kind)
        if kind == "defect":
            if not data:
                return "<p class='empty'>Дефектов не найдено.</p>"
            body = "".join(
                f"<tr><td class='num'>{num}</td><td>{html.escape(str(i.get('basis', '').split(':')[0]))}</td>"
                f"<td>{html.escape(str(i.get('basis', '')).split(':', 1)[-1].strip())}</td></tr>"
                for num, i in enumerate(data, 1))
            return ("<div class='tablewrap'><table class='{c}'><thead><tr><th>№</th><th>что найдено</th>"
                    "<th>подробности</th></tr></thead><tbody>{b}</tbody></table></div>").format(c=cls, b=body)
        if not data:
            return f"<p class='empty'>Раздел пуст: {head.lower()} не найдено.</p>"
        th = "".join(f"<th>{html.escape(c)}</th>" for c in cols)
        body = []
        for num, i in enumerate(data, 1):
            tds = []
            for c in cols:
                key = {"№": "id", "строка": "row", "контейнер": "container",
                       "компонент": "component", "CVE": "vuln",
                       "сейчас": "current", "предлагаю": "proposed", "основание": "basis",
                       "источник": "source", "решение": "strength",
                       "пусто": "current", "что сделать": "proposed"}[c]
                v = num if c == "№" else (i.get(key, "") or "—")
                cellcls = "num" if c in ("№", "строка") else ""
                tds.append(f"<td class='{cellcls}'>{html.escape(str(v))}</td>")
            mark = " class='err'" if i.get("level") == "error" or kind == "error" else ""
            body.append(f"<tr{mark}>" + "".join(tds) + "</tr>")
        return (f"<div class='tablewrap'><table class='{cls}'><thead><tr>{th}</tr></thead>"
                f"<tbody>{''.join(body)}</tbody></table></div>")

    st = rows("status")
    det = rows("detail")
    nts = rows("note")
    errs = rows("error")
    stat = Counter(i["proposed"] for i in st)
    cards = [
        ("строк в отчёте", meta["rows_in"]),
        ("удалено (severity вне high/critical)", meta["rows_dropped"]),
        ("исправлено опечаток и формата", meta["typos"]),
        ("рекомендаций по статусу", len(st)),
        ("рекомендаций по обоснованию", len(det)),
        ("необходимо заполнить", sum(1 for i in items if i["kind"] == "fill")),
        ("ошибок разметки (красным)", len(errs)),
        ("дефектов файла", sum(1 for i in items if i["kind"] == "defect")),
        ("замечаний к разметке", len(nts)),
    ]
    cards_html = "".join(f"<div class='card'><div class='n'>{v}</div>"
                         f"<div class='l'>{html.escape(k)}</div></div>" for k, v in cards)
    stat_html = "".join(f"<li><code>{html.escape(k)}</code> — {v}</li>" for k, v in stat.most_common())
    journal_html = "".join(f"<li>{html.escape(j)}</li>" for j in journal)
    cols = ["№", "строка", "контейнер", "компонент", "CVE", "сейчас", "предлагаю", "основание",
            "решение"]
    cols_err = ["№", "строка", "контейнер", "компонент", "CVE", "сейчас", "предлагаю", "основание"]
    cols_defect = ["№", "что найдено", "подробности"]
    cols_fill = ["№", "строка", "контейнер", "компонент", "CVE", "пусто", "что сделать"]
    changes = changes or []
    if changes:
        body = "".join("<tr>" + "".join(f"<td>{html.escape(str(v))}</td>" for v in row) + "</tr>"
                       for row in changes)
        changes_html = ("<div class='tablewrap'><table><thead><tr><th>строка</th><th>поле</th>"
                        "<th>было</th><th>стало</th><th>основание</th></tr></thead>"
                        f"<tbody>{body}</tbody></table></div>")
    else:
        changes_html = "<p class='empty'>Правок нет.</p>"
    doc = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Рекомендации по отчёту {html.escape(meta['source_name'])}</title>
<style>
 :root {{ --ink:#1b1f24; --muted:#5b6570; --line:#d8dee4; --accent:#0b6bcb; --warn:#b25b00; }}
 * {{ box-sizing:border-box; }}
 body {{ margin:0; padding:32px 24px 64px; font:15px/1.5 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;
        color:var(--ink); background:#fbfcfd; }}
 main {{ max-width:1180px; margin:0 auto; }}
 h1 {{ font-size:26px; margin:0 0 6px; }}
 h2 {{ font-size:19px; margin:34px 0 10px; padding-bottom:6px; border-bottom:1px solid var(--line); }}
 .meta {{ color:var(--muted); font-size:13px; margin-bottom:22px; }}
 .cards {{ display:flex; flex-wrap:wrap; gap:12px; }}
 .card {{ flex:1 1 170px; background:#fff; border:1px solid var(--line); border-radius:10px; padding:14px 16px; }}
 .card .n {{ font-size:24px; font-weight:700; }}
 .card .l {{ color:var(--muted); font-size:12.5px; margin-top:2px; }}
 .tablewrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:10px; background:#fff; }}
 table {{ width:100%; min-width:1080px; border-collapse:collapse; font-size:12.8px; }}
 th, td {{ border-bottom:1px solid var(--line); border-right:1px solid var(--line); padding:7px 9px;
        vertical-align:top; text-align:left; }}
 th {{ background:#f1f4f7; font-size:12px; text-transform:uppercase; letter-spacing:.02em;
       color:var(--muted); white-space:nowrap; }}
 td:nth-child(3) {{ min-width:130px; }}
 td:nth-child(4) {{ min-width:230px; overflow-wrap:anywhere; }}
 td:nth-child(5) {{ white-space:nowrap; }}
 td:nth-child(6), td:nth-child(7) {{ min-width:200px; }}
 td:nth-child(8) {{ min-width:260px; }}
 tr:last-child td {{ border-bottom:none; }}
 tr.err td {{ background:#fdeef0; }}
 tr.err td:first-child {{ box-shadow: inset 3px 0 0 #b00020; font-weight:700; }}
 table.errs th {{ background:#fbe9ec; }}
 th:last-child, td:last-child {{ border-right:none; }}
 td.num {{ text-align:right; color:var(--muted); width:52px; }}
 tr:nth-child(even) td {{ background:#fcfdfe; }}
 code {{ font:12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace; background:#f1f4f7;
         padding:1px 5px; border-radius:4px; }}
 .empty {{ color:var(--muted); font-style:italic; }}
 .note {{ background:#fff8ec; border:1px solid #f0d9b5; border-radius:10px; padding:12px 16px; }}
 ul.journal {{ columns:2; column-gap:28px; font-size:13px; color:var(--muted); padding-left:18px; }}
 @media print {{ body {{ background:#fff; padding:0; }} th {{ position:static; }}
   .card {{ break-inside:avoid; }} table {{ font-size:11.5px; }} }}
</style></head><body><main>
<h1>Рекомендации по отчёту заимствованных компонентов</h1>
<div class="meta">Файл: <code>{html.escape(meta['source'])}</code> · промежуточный результат:
 <code>{html.escape(meta['filtered'])}</code> · сформировано {meta['ts']}<br>
 Статусы, идентификаторы и purl в отчёте не изменялись: всё перечисленное — предложения.</div>
<div class="cards">{cards_html}</div>
<h2>Смена статуса</h2>
<p>Предлагаемые статусы:</p><ul>{stat_html or '<li>нет</li>'}</ul>
{table("status", "Статусы к утверждению", cols)}
<h2>Правка обоснований</h2>
{table("detail", "Обоснования к уточнению",
       ["№", "строка", "контейнер", "компонент", "CVE", "сейчас", "предлагаю", "основание"])}
<h2>Необходимо заполнить</h2>
<p>Поля, пустые в исходном отчёте: без них статус разметки не подтверждён.</p>
{table("fill", "Незаполненные поля", cols_fill)}
<h2>Ошибки разметки</h2>
<p>Красным выделены строки с ошибками человека: обоснование через неиспользуемость, недопустимые
значения полей, нечитаемые строки, удалённые из отчёта.</p>
{table("error", "Строки с ошибками разметки", cols_err, "errs")}
<h2>Дефекты файла</h2>
<p>Это не ошибки разметки: особенности выгрузки — служебные строки, одноколоночный CSV, кодировка.</p>
{table("defect", "Дефекты исходного файла", cols_defect)}
<h2>Замечания к разметке</h2>
{table("note", "Замечания",
       ["№", "строка", "контейнер", "компонент", "CVE", "предлагаю", "основание"])}
<h2>Внесённые правки</h2>
<p>Правки орфографии и формата в самом отчёте. Отдельного листа в документе нет: всё, что
изменено, перечислено здесь.</p>
{changes_html}
<h2>Журнал этапа</h2>
<div class="note"><ul class="journal">{journal_html}</ul></div>
</main></body></html>"""
    html_path.write_text(doc, encoding="utf-8")
    return html_path


def neutralise_query_tables(wb):
    """Снять признак запроса у таблиц, сохранив сами таблицы.

    Выгрузка — это `queryTable`: Excel на открытии ругается на внешние данные, если части запроса
    не перенесены. Но таблица даёт цветные полосы и кнопки фильтра, поэтому её нельзя просто
    удалять: убираем `tableType="queryTable"`, ссылки на поля запроса и скрытые имена
    `ExternalData_*`. Цвета, фильтры и ширины колонок остаются как в источнике.
    """
    touched = 0
    for ws in wb.worksheets:
        for table in ws.tables.values():
            if getattr(table, "tableType", None):
                touched += 1
            table.tableType = None
            for col in table.tableColumns:
                if getattr(col, "queryTableFieldId", None) is not None:
                    col.queryTableFieldId = None
        for name in list(getattr(ws.defined_names, "keys", lambda: [])()):
            if name.lower().startswith("externaldata"):
                del ws.defined_names[name]
    for name in list(getattr(wb.defined_names, "keys", lambda: [])()):
        if name.lower().startswith("externaldata"):
            del wb.defined_names[name]
    return touched


def audit_book(wb, idx):
    """Дефекты исходной книги по всем листам: выгрузки, мусор, недопустимые значения."""
    out = []
    VALID_SEV = {"critical", "high", "medium", "low", "none", "info", "unknown", "negligible"}
    VALID_ST = {"false_positive", "not_affected"}

    def audit_rows(title, rws, h, cidx, note=""):
        rows_all = [(n, r) for n, r in rws if n > h and any(str(v or "").strip() for v in r)]
        junk = [n for n, _ in rws[:max(0, h - 1)] if any(str(v or "").strip() for v in _)]
        no_purl = [n for n, r in rows_all if L.cell(r, cidx, "component")
                   and not L.cell(r, cidx, "component").startswith("pkg:")]
        no_name = [n for n, r in rows_all if not L.cell(r, cidx, "product_name")]
        no_comp = [n for n, r in rows_all if not L.cell(r, cidx, "component")
                   and not L.cell(r, cidx, "vuln_id")]
        sevs = [L.cell(r, cidx, "severity").lower() for _, r in rows_all]
        sts = [L.cell(r, cidx, "status").lower() for _, r in rows_all]
        bad_sev = sorted({v for v in sevs if v and v not in VALID_SEV})
        bad_st = sorted({v for v in sts if v and v not in VALID_ST})
        empty = sum(1 for v in sts if not v)
        t = f"лист «{title}»{note}"
        if junk:
            out.append((f"{t}: служебные строки Column над шапкой",
                        f"строки {', '.join(f'r{x}' for x in junk[:6])} удалены из результата; "
                        f"шапка отчёта была в строке {h}"))
        # уровни и статусы вне словаря перечисляются отдельными пунктами с номерами строк

    for ws in wb.worksheets:
        rws = [(r[0].row, [c.value for c in r]) for r in ws.iter_rows()]
        vals = [v for _, r in rws for v in r if v is not None]
        moji = sum(1 for v in vals if isinstance(v, str) and ("РЎ" in v or "Рњ" in v))
        width = max((sum(1 for v in r if v is not None) for _, r in rws), default=0)
        h, hd = L.find_header(rws)
        if hd is not None:
            audit_rows(ws.title, rws, h, L.column_map(hd))
            continue
        parsed = L._csv_cells(rws)
        ph, phd = L.find_header(parsed) if parsed else (None, None)
        if phd:
            out.append((f"лист «{ws.title}» — выгрузка CSV в одной колонке",
                        f"{len(vals)} непустых ячеек, ширина {width}; cp1251-мojibake в {moji} ячейках — "
                        f"текст восстановлен и перенесён в лист «Отчёт»"))
            audit_rows(ws.title, parsed, ph, L.column_map(phd), note=" (выгрузка)")
            continue
        out.append((f"лист «{ws.title}» без табличной шапки",
                    f"{len(vals)} непустых ячеек в {width} колонке(ах) — данные не читаются"))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", nargs="?")
    ap.add_argument("--outdir", default=None, help="каталог результата (по умолчанию SRC_DEP_analys)")
    ap.add_argument("--no-nvd", action="store_true", help="не ходить в NVD за диапазонами версий")
    ap.add_argument("--cache-dir", default=str(Path.home() / ".cache" / "src-dep-analysis"))
    ap.add_argument("--workdir", default=None,
                    help="где держать машиночитаемую спецификацию (по умолчанию ~/.cache/src-dep-analysis/work)")
    a = ap.parse_args()
    if not a.source:
        sys.exit("укажите отчёт: check_report.py <файл.xlsx|.csv>")

    src = Path(a.source).expanduser().resolve()
    outdir = Path(a.outdir).expanduser() if a.outdir else src.parent / "SRC_DEP_analys"
    outdir.mkdir(parents=True, exist_ok=True)
    filtered = outdir / f"{src.stem}_filtered.xlsx"
    html_out = outdir / f"{src.stem}_recommendations.html"

    wb, sheet, hnum, idx, rows = L.read_table(src)
    if wb is None:  # csv: собираем книгу в памяти, чтобы правки шли тем же путём
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "csv"
        sheet = "csv"
        for _, r in rows:
            ws.append(["" if c is None else c for c in r])
        rows = [(i, [c.value for c in row]) for i, row in enumerate(ws.iter_rows(), 1)]
    audit = audit_book(wb, idx)
    external = neutralise_query_tables(wb)
    src_ws = wb[sheet]
    hdr_cells = sum(1 for c in next(src_ws.iter_rows(min_row=hnum, max_row=hnum), []) if c.value not in (None, ""))
    if hdr_cells < 4 and (idx.get("detail") or 0) >= 1:
        # лист-выгрузка: правки по индексам колонок в нём невозможны — переносим в чистую таблицу
        from openpyxl import Workbook
        clean = Workbook()
        cws = clean.active
        cws.title = "Отчёт"
        for _, r in rows:
            cws.append(["" if c is None else c for c in r])
        wb, sheet = clean, "Отчёт"
        rows = [(i, [c.value for c in row]) for i, row in enumerate(cws.iter_rows(), 1)]
        audit.append(("данные взяты из выгрузки CSV в одной колонке",
                      f"лист «{src_ws.title}» шириной {src_ws.max_column} колонка; текст в cp1251-мojibake "
                      f"восстановлен, перенесён в чистый лист «Отчёт», исходный лист в результат не входит"))
    # --- удаление строк вне high/critical
    ws = wb[sheet]
    # служебная строка выгрузки `Column1 … Column8` над шапкой: в отчёте ей делать нечего
    import re as _re
    service = [n for n, r in rows if n < hnum
               and any(str(v or "").strip() for v in r)
               and all(not str(v or "").strip() or _re.match(r"^column\s*\d+$", str(v).strip(), _re.I)
                       for v in r)]
    if service:
        for n in sorted(service, reverse=True):
            ws.delete_rows(n, 1)
        hnum -= len(service)
        rows = [(r[0].row, [c.value for c in r]) for r in ws.iter_rows()]

    missing = [f for f in L.FIELDS if f not in idx]
    data = [(n, r) for n, r in rows if n > hnum and any(str(c).strip() for c in r if c is not None)]

    VALID_SEVERITY = {"critical", "high", "medium", "low", "none", "info", "unknown", "negligible"}
    VULN_RE = __import__("re").compile(r"^(CVE-\d{4}-\d+|GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4})", __import__("re").I)

    def unreadable(row):
        """Строка без данных: ни продукта, ни purl-компонента, ни идентификатора уязвимости."""
        comp_i, vid_i, name_i = idx.get("component"), idx.get("vuln_id"), idx.get("product_name")
        comp = L.cell(row, idx, "component")
        vid = L.cell(row, idx, "vuln_id")
        name = L.cell(row, idx, "product_name")
        if not comp and not vid:
            return "нет ни компонента, ни идентификатора"
        if comp and not comp.lower().startswith("pkg:"):
            return "компонент не в формате purl"
        if vid and not VULN_RE.match(vid):
            return "идентификатор уязвимости не CVE/GHSA"
        if not name and not comp:
            return "нет названия продукта"
        return ""
    drop = [n for n, r in data
            if L.cell(r, idx, "severity").lower() in VALID_SEVERITY
            and L.cell(r, idx, "severity").lower() not in L.ALLOWED_SEVERITY]
    # нечитаемые строки удаляются целиком — «просто набор букв» в отчёте оставаться не должен
    junk = []
    for n, r in data:
        why = unreadable(r)
        if why:
            junk.append((n, why, " | ".join(L.cell(r, idx, f)[:40] for f in
                                            ("product_name", "component", "vuln_id", "severity", "status")
                                            if L.cell(r, idx, f))))
    # полностью пустые строки в диапазоне — тоже не данные: в выгрузках между блоками их сотни
    empty = [n for n, r in rows if n > hnum and not any(str(c or "").strip() for c in r)]
    for n in sorted(set(drop) | set(empty) | {x[0] for x in junk}, reverse=True):
        ws.delete_rows(n, 1)
    junk_rows = {x[0] for x in junk}
    kept = [(n, r) for n, r in data if n not in set(drop) and n not in junk_rows]

    # --- орфография и формат
    changes, typos = [], 0
    det_i = idx.get("detail")
    removed = set(drop) | set(empty) | junk_rows
    shift = {n: n - sum(1 for d in removed if d < n) for n, _ in kept}
    for n, r in kept:
        if det_i is None:
            continue
        old = L.cell(r, idx, "detail")
        new, notes = L.fix_text(old)
        if notes and new != old:
            c = ws.cell(row=shift[n], column=det_i + 1)
            c.value = new
            c.alignment = Alignment(wrap_text=True, vertical="top")
            typos += 1
            changes.append([shift[n], "detail", old, new, "; ".join(notes)])

    # --- рекомендации
    items, seen = [], set()
    unique = {L.cell(r, idx, "vuln_id") for _, r in kept}
    ranges = {}
    if not a.no_nvd:
        for vid in sorted(v for v in unique if v.upper().startswith("CVE-")):
            ranges[vid] = L.nvd_intervals(vid, Path(a.cache_dir))
    agg = {}          # (что не так, контейнер, значение) → (описание, [строки])

    def mark(key, row, desc):
        d, rws = agg.setdefault(key, (desc, []))
        rws.append(row)

    for n, r in kept:
        comp, vid = L.cell(r, idx, "component"), L.cell(r, idx, "vuln_id")
        status, det = L.cell(r, idx, "status").lower(), L.cell(r, idx, "detail")
        sev = L.cell(r, idx, "severity").lower()
        container = L.cell(r, idx, "product_name") or L.cell(r, idx, "product_purl")
        if not comp or not vid:
            mark(("структура", container, "без компонента" if not comp else "без id"), n,
                 "строка без компонента" if not comp else "строка без идентификатора уязвимости")
            continue
        if sev not in VALID_SEVERITY:
            mark(("уровень", container, sev), n,
                 f"недопустимый уровень опасности: {'пусто' if not sev else '«' + sev + '»'}")
        empty_fields = [f for f in ("severity", "status", "detail") if not L.cell(r, idx, f)]
        if empty_fields:
            items.append({"id": len(items) + 1, "row": n, "container": container,
                          "component": comp, "vuln": vid, "kind": "fill", "field": "fill",
                          "current": ", ".join(empty_fields),
                          "proposed": "необходимо заполнить: " + ", ".join(empty_fields),
                          "basis": f"поля пусты в исходнике: {', '.join(empty_fields)}",
                          "source": "проверка заполненности", "strength": "необходимо утвердить"})
        if status not in ("false_positive", "not_affected"):
            mark(("статус", container, status), n,
                 f"статус вне словаря fp/na: {'пусто' if not status else '«' + status + '»'}")
        ver = L.component_version(comp)
        for cand in (None if status not in ("false_positive", "not_affected") else
                     propose_status(comp, ver, vid, status, det,
                                    L.intervals_for(ranges.get(vid), comp), []),
                     check_non_use(comp, vid, det),
                     propose_detail(comp, ver, vid, det),
                     check_affirmative(comp, ver, vid, status, det),
                     check_version_text(comp, ver, vid, det)):
            if not cand:
                continue
            key = (container, cand["kind"], comp, vid, cand.get("field"), cand["proposed"])
            if key in seen:
                continue
            seen.add(key)
            items.append({"id": len(items) + 1, "row": n, "container": container,
                          "component": comp, "vuln": vid,
                          "current": cand.get("current", det if cand["field"] == "detail"
                                              else status), **cand})

    by_reason = {}
    for n, why, snippet in junk:
        by_reason.setdefault(why, []).append((n, snippet))
    for why, rows_ in by_reason.items():
        items.append({"id": len(items) + 1, "row": f"{len(rows_)} стр.", "container": "—",
                      "component": "—", "vuln": "—", "kind": "error", "field": "удаление",
                      "level": "error", "current": "—",
                      "proposed": "строка удалена из отчёта",
                      "basis": f"удалены нечитаемые строки ({why}): "
                               + "; ".join(f"исходник r{n}: «{sn[:70]}»" for n, sn in rows_[:6])
                               + (" …" if len(rows_) > 6 else ""),
                      "source": "построчная проверка", "strength": "ошибка"})

    for what, desc in audit:
        items.append({"id": len(items) + 1, "row": "—", "container": "—", "component": "—",
                      "vuln": "—", "kind": "defect", "field": "источник", "level": "info",
                      "current": "—", "proposed": "исправить исходный файл",
                      "basis": f"{what}: {desc}", "source": "аудит файла", "strength": "ошибка"})

    for (what, container, value), (desc, rws) in agg.items():
        sample = ", ".join(f"r{x}" for x in rws[:8]) + (" …" if len(rws) > 8 else "")
        items.append({"id": len(items) + 1, "row": f"{len(rws)} стр.", "container": container,
                      "component": "—", "vuln": "—", "kind": "error", "field": what,
                      "level": "error", "current": value or "—",
                      "proposed": f"исправить поле «{what}» в этих строках",
                      "basis": f"{desc}; строки исходника: {sample}",
                      "source": "валидация полей", "strength": "ошибка"})

    # лишних листов в документе быть не должно: правки и журнал живут только в HTML
    for extra in (f"{src.stem}_filtered", "Изменения", "Журнал"):
        if extra in wb.sheetnames:
            del wb[extra]

    journal = [
        f"вход: {src.name}, лист «{sheet}», шапка в строке {hnum}, строк данных {len(data)}",
        f"поля отчёта: {', '.join(sorted(idx))}"
        + (f"; отсутствуют: {', '.join(missing)}" if missing else "; все обязательные поля есть"),
        f"удалены служебные строки Column над шапкой: {len(service)}",
        f"удалено пустых строк в диапазоне: {len(empty)} (в рекомендации не описываются)",
        f"сняты внешние данные и служебные структуры: {external}",
        f"оставляю только high и critical — удалено {len(drop)} из {len(data)} "
        f"(critical {sum(1 for _, r in kept if L.cell(r, idx, 'severity').lower() == 'critical')}, "
        f"high {sum(1 for _, r in kept if L.cell(r, idx, 'severity').lower() == 'high')})",
        f"проверяю орфографию — правлено ячеек: {typos} (опечатки и формат)",
        f"статусы, id и purl не изменялись: предложения вынесены в {html_out.name}",
        f"удалено нечитаемых строк: {len(junk)}",
        f"рекомендаций: статус {sum(1 for i in items if i['kind'] == 'status')}, "
        f"обоснование {sum(1 for i in items if i['kind'] == 'detail')}, "
        f"замечания {sum(1 for i in items if i['kind'] == 'note')}, "
        f"ошибки разметки {sum(1 for i in items if i['kind'] == 'error')}",
    ]
    # таблица и автофильтр должны указывать на новый диапазон, иначе Excel их отбросит
    from openpyxl.utils import get_column_letter
    last_row, last_col = ws.max_row, ws.max_column
    rng = f"A{hnum}:{get_column_letter(last_col)}{last_row}"
    header_vals = []
    for c in range(1, last_col + 1):
        v = ws.cell(row=hnum, column=c).value
        header_vals.append(str(v).strip() if v not in (None, "") else f"Column{c}")
    for table in ws.tables.values():
        table.ref = rng
        if getattr(table, "autoFilter", None) is not None:
            table.autoFilter.ref = rng
        # имена колонок таблицы должны совпадать с шапкой, иначе Excel переспросит
        for i, col in enumerate(table.tableColumns):
            if i < len(header_vals):
                col.name = header_vals[i]
    if ws.auto_filter and ws.auto_filter.ref:
        ws.auto_filter.ref = rng

    wb.save(filtered)

    meta = {"source": str(src), "source_name": src.name, "filtered": str(filtered),
            "rows_in": len(data), "rows_dropped": len(drop), "typos": typos,
            "ts": datetime.now().strftime("%d.%m.%Y %H:%M")}
    workdir = Path(a.workdir).expanduser() if a.workdir else Path(a.cache_dir) / "work"
    workdir.mkdir(parents=True, exist_ok=True)
    json_path = workdir / f"{src.stem}.json"
    payload = {"source": str(src), "filtered": str(filtered), "meta": meta, "items": items,
               "journal": journal, "changes": changes, "sheet": sheet,
               "row_map": {str(n): shift[n] for n, _ in kept}}
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    html_path = build(items, meta, journal, html_out, changes)
    print(json.dumps({"filtered": str(filtered), "html": str(html_path), "json": str(json_path),
                      "rows_in": len(data), "rows_dropped": len(drop), "typos": typos,
                      "status_recs": sum(1 for i in items if i["kind"] == "status"),
                      "detail_recs": sum(1 for i in items if i["kind"] == "detail"),
                      "notes": sum(1 for i in items if i["kind"] == "note"),
                      "errors": sum(1 for i in items if i["kind"] == "error"),
                      "defects": sum(1 for i in items if i["kind"] == "defect"),
                      "fills": sum(1 for i in items if i["kind"] == "fill")},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
