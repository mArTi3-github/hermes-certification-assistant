# -*- coding: utf-8 -*-
"""Конвертация отчёта MD -> автономный HTML.

Стилизация под заключение по сертификации: титульный блок из строк-метаданных,
боковое оглавление, таблицы с прокруткой, подсветка вердиктов
("Соответствует" / "частично" / "Не соответствует" / "Не подтверждено")
и правила печати. Внешних ресурсов не требует.
"""
import os
import re
import markdown

import argparse

ap = argparse.ArgumentParser(description="Markdown-отчёт -> автономный HTML с оглавлением и подсветкой вердиктов")
ap.add_argument("src")
ap.add_argument("dst", nargs="?")
a = ap.parse_args()
SRC = a.src
DST = a.dst or (os.path.splitext(SRC)[0] + ".html")

text = open(SRC, encoding="utf-8").read()

# титульный блок вырезаем из MD (первые строки с метаданными) и рисуем отдельно
lines = text.split("\n")
title = lines[0].lstrip("# ").strip()
meta = [l.strip() for l in lines[1:8] if l.strip().startswith("**")]
body_md = "\n".join(lines[8:])

md = markdown.Markdown(extensions=["tables", "fenced_code", "sane_lists", "attr_list", "toc"],
                       extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
body = md.convert(body_md)
toc = md.toc

# раскраска вердиктов в сводной таблице
def paint(m):
    verdict = m.group(1)
    if verdict == "Соответствует частично":
        cls = "v-warn"
    elif verdict == "Соответствует":
        cls = "v-good"
    elif verdict == "Не соответствует":
        cls = "v-bad"
    elif verdict == "Не подтверждено":
        cls = "v-warn"
    else:
        cls = ""
    return '<td class="%s"><strong>%s' % (cls, verdict)

body = re.sub(r'<td><strong>(Соответствует частично|Соответствует|Не соответствует|Не подтверждено)',
              paint, body)
# смешанный вердикт по п.5
body = re.sub(r'(<td class="v-good"><strong>Соответствует по подпункту «а»</strong>[^<]*?по подпункту «б» не подтверждено)',
              r'<td class="v-mixed"><strong>Соответствует по подпункту «а»</strong> (по подпункту «б» не подтверждено)',
              body)

pairs = []
for item in meta:
    mm = re.match(r"\*\*(.+?):\*\*\s*(.*)", item)
    if mm:
        label, value = mm.group(1), mm.group(2)
    else:
        mm = re.match(r"\*\*(.+?)\*\*\s*(.*)", item)
        label, value = (mm.group(1), mm.group(2)) if mm else ("", item)
    value = re.sub(r"`([^`]+)`", r"<code>\1</code>", value)
    pairs.append("<dt>%s</dt><dd>%s</dd>" % (label, value))
meta_html = "<dl class=\"meta\">" + "".join(pairs) + "</dl>"

# числовые ячейки -> выравнивание вправо
def num_cell(m):
    inner = m.group(1)
    plain = re.sub(r"<[^>]+>", "", inner).strip()
    if re.fullmatch(r"[\d\s.,:%×()/–—-]+", plain) and re.search(r"\d", plain):
        return '<td class="num">' + inner + '</td>'
    return m.group(0)
body = re.sub(r"<td>(.*?)</td>", num_cell, body, flags=re.S)

# обёртка таблиц: горизонтальная прокрутка для широких таблиц
def wrap_table(m):
    tbl = m.group(1)
    ncols = len(re.findall(r"<th[ >]", tbl.split("</tr>")[0]))
    cls = ' class="wide"' if ncols >= 8 else ''
    tbl = tbl.replace("<table>", "<table%s>" % cls, 1)
    return '<div class="tw">' + tbl + '</div>'
body = re.sub(r"(<table>.*?</table>)", wrap_table, body, flags=re.S)

CSS = """
:root{
  --ink:#1b2430; --muted:#5f6b7a; --line:#d8dee7; --bg:#ffffff; --soft:#f4f7fa;
  --accent:#1f4e79; --good:#1c6b3c; --goodbg:#e8f4ec; --warn:#8a5a00; --warnbg:#fdf3e2;
  --bad:#a3231c; --badbg:#fbeceb; --mixed:#5b4593; --mixedbg:#f0ecf9;
}
*{box-sizing:border-box}
body{margin:0;background:#eef1f5;color:var(--ink);overflow-x:hidden;
  font:16px/1.62 -apple-system,BlinkMacSystemFont,"Segoe UI","PT Sans","Liberation Sans",Arial,sans-serif;}
.page{max-width:1180px;margin:0 auto;background:var(--bg);
  box-shadow:0 1px 3px rgba(20,30,45,.12),0 12px 40px rgba(20,30,45,.08);}
header.doc{padding:44px 56px 30px;border-bottom:3px solid var(--accent);background:linear-gradient(180deg,#fbfcfe,#fff);}
header.doc .kicker{font-size:12px;letter-spacing:.14em;text-transform:uppercase;color:var(--muted);margin:0 0 10px;}
header.doc h1{font-size:30px;line-height:1.25;margin:0 0 18px;font-weight:700;color:#152233;}
header.doc dl.meta{display:grid;grid-template-columns:186px 1fr;gap:9px 18px;margin:0;font-size:14px;}
header.doc dl.meta dt{color:var(--muted);font-size:13px;}
header.doc dl.meta dd{margin:0;color:#26384d;min-width:0;overflow-wrap:anywhere;}
header.doc dl.meta code{font-size:12.5px;background:var(--soft);border:1px solid var(--line);border-radius:4px;padding:1px 6px;color:#2b3949;overflow-wrap:anywhere;}
.wrap{display:grid;grid-template-columns:262px 1fr;gap:0;}
nav.toc{border-right:1px solid var(--line);background:#fafbfd;padding:30px 22px 60px;font-size:13.5px;}
nav.toc h2{font-size:11.5px;letter-spacing:.13em;text-transform:uppercase;color:var(--muted);margin:0 0 14px;}
nav.toc ul{list-style:none;margin:0;padding:0;}
nav.toc li{margin:0 0 7px;}
nav.toc ul ul{margin:7px 0 0 12px;border-left:1px solid var(--line);padding-left:10px;}
nav.toc a{color:#35485e;text-decoration:none;display:block;}
nav.toc a:hover{color:var(--accent);text-decoration:underline;}
nav.toc > ul > li > a{font-weight:600;color:#1f2c3c;}
main{padding:34px 56px 76px;min-width:0;}
main > h2{font-size:23px;margin:44px 0 16px;padding-bottom:10px;border-bottom:2px solid var(--line);font-weight:700;color:#152233;}
main > hr:first-child{display:none;}
main > h2:first-child{margin-top:6px;}
main h3{font-size:18.5px;margin:30px 0 10px;color:var(--accent);font-weight:700;}
p{margin:10px 0;}
strong{font-weight:700;color:#12202f;}
hr{border:0;border-top:1px solid var(--line);margin:34px 0;}
ul,ol{margin:10px 0 10px 22px;padding:0;}
li{margin:5px 0;}
code{font-family:"JetBrains Mono","SFMono-Regular",Consolas,"Liberation Mono",monospace;overflow-wrap:anywhere;
  font-size:13px;background:#f2f5f9;border:1px solid #e1e7ef;border-radius:4px;padding:1px 5px;color:#26384d;}
pre{background:#1f2937;color:#e6edf5;border-radius:8px;padding:16px 18px;overflow:auto;font-size:12.8px;line-height:1.55;}
pre code{background:none;border:0;color:inherit;padding:0;font-size:12.8px;}
.tw{overflow-x:auto;margin:18px 0 8px;border:1px solid var(--line);border-radius:6px;background:#fff;}
.tw table{margin:0;border:0;}
table{border-collapse:collapse;width:100%;margin:18px 0 8px;font-size:14.2px;background:#fff;}
table.wide{font-size:12.3px;}
table.wide th,table.wide td{padding:6px 7px;}
td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}
th,td{border:1px solid var(--line);padding:9px 11px;vertical-align:top;text-align:left;}
th{background:#eef3f8;font-weight:700;color:#1b2a3b;font-size:13.4px;}
tbody tr:nth-child(even){background:#fafcfe;}
td[align="right"],th[align="right"],td:has(> :only-child) {font-variant-numeric:tabular-nums;}
td.v-good{background:var(--goodbg);border-left:4px solid var(--good);}
td.v-good strong{color:var(--good);}
td.v-warn{background:var(--warnbg);border-left:4px solid var(--warn);}
td.v-warn strong{color:var(--warn);}
td.v-bad{background:var(--badbg);border-left:4px solid var(--bad);}
td.v-bad strong{color:var(--bad);}
td.v-mixed{background:var(--mixedbg);border-left:4px solid var(--mixed);}
td.v-mixed strong{color:var(--mixed);}
blockquote{margin:14px 0;padding:10px 16px;border-left:4px solid var(--accent);background:var(--soft);color:#33465c;}
footer{padding:22px 56px 34px;border-top:1px solid var(--line);color:var(--muted);font-size:12.5px;background:#fafbfd;}
@media print{
  body{background:#fff;font-size:11pt;}
  .page{box-shadow:none;max-width:none;}
  .wrap{display:block;}
  nav.toc{display:none;}
  main{padding:0;}
  header.doc{padding:0 0 18px;}
  main > h2{page-break-after:avoid;}
  table{page-break-inside:avoid;font-size:9.5pt;}
  pre{background:#f4f6f8;color:#1b2430;border:1px solid var(--line);}
}
@media (max-width:900px){
  .wrap{grid-template-columns:1fr;}
  nav.toc{border-right:0;border-bottom:1px solid var(--line);}
  header.doc,main,footer{padding-left:22px;padding-right:22px;}
}
"""

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body>
<div class="page">
  <header class="doc">
    <p class="kicker">Проверка материалов фаззинг-тестирования · Методика ВУ</p>
    <h1>{title}</h1>
    {meta}
  </header>
  <div class="wrap">
    <nav class="toc"><h2>Содержание</h2>{toc}</nav>
    <main>{body}</main>
  </div>
  <footer>Отчёт сформирован автоматически по материалам архива <code>sflow-28.1-cert.zip</code>; все числовые значения получены разбором файлов материалов (fuzzer_stats, plot_data, журналы фаззеров, HTML-отчёты gcovr, образцы зависаний), а не перенесены из сопроводительного Readme.</footer>
</div>
</body>
</html>
"""

out = HTML.format(title=title, css=CSS, meta=meta_html, toc=toc, body=body)
open(DST, "w", encoding="utf-8").write(out)
print("written:", DST, len(out), "chars")
