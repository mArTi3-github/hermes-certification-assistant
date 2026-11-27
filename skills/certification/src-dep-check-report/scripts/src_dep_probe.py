#!/usr/bin/env python3
"""Разбор отчёта по уязвимостям заимствованных компонентов.

Читает xlsx (stdlib: zipfile + ElementTree, внешних библиотек не требует) или csv
(разделитель `;`, `,` или таб; BOM и cp1251), находит шапку по словарю синонимов колонок
и печатает только агрегаты и находки — без вывода всех строк.

    python3 src_dep_probe.py <файл> [--json <путь>]
"""
import csv
import json
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
REQUIRED = {
    "product_name": ["name", "product"],
    "product_version": ["version"],
    "product_purl": ["purl", "ancestor_component_purl"],
    "component": ["component", "component_purl"],
    "vuln_id": ["id", "vuln_id", "cve"],
    "severity": ["severity"],
    "status": ["status"],
    "detail": ["detail", "обоснование"],
}
ALLOWED_STATUS = {"false_positive", "not_affected"}
ALLOWED_SEVERITY = {"high", "critical"}
PURL_RE = re.compile(r"^pkg:[a-z0-9.+-]+/[^@\s]+@\S+$", re.I)
AFFIRMATIVE = re.compile(
    r"(?<!не\s)(?<!не)("
    r"уязвимость\s+(актуальна|применима|подтверждена|воспроизводится|эксплуатируется)"
    r"|является\s+актуальной|применима|актуальна|подтверждено|подтверждена"
    r"|эксплуатация\s+возможна|содержит\s+уязвимость"
    r")",
    re.I,
)


def cells_from_xlsx(path):
    z = zipfile.ZipFile(path)
    shared = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(NS + "t")) for si in root.findall(NS + "si")]
    sheets = []
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    rel_map = {r.get("Id"): r.get("Target") for r in rels}
    rid_attr = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    for sh in wb.iter(NS + "sheet"):
        target = rel_map.get(sh.get(rid_attr), "")
        target = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if target not in z.namelist():
            continue
        root = ET.fromstring(z.read(target))
        rows = []
        for row in root.iter(NS + "row"):
            out = []
            for c in row.findall(NS + "c"):
                v = c.find(NS + "v")
                if c.get("t") == "s" and v is not None:
                    out.append(shared[int(v.text)])
                elif c.get("t") == "inlineStr":
                    out.append("".join(t.text or "" for t in c.iter(NS + "t")))
                else:
                    out.append(v.text if v is not None else "")
            rows.append((int(row.get("r")), out))
        sheets.append((sh.get("name"), rows))
    return sheets


def cells_from_csv(path):
    enc = "utf-8-sig"
    try:
        open(path, encoding=enc).read(4096)
    except UnicodeDecodeError:
        enc = "cp1251"
    text = open(path, encoding=enc, newline="").read()
    delim = max((";", ",", "\t"), key=lambda d: text.split("\n", 1)[0].count(d))
    rows = [(i + 1, r) for i, r in enumerate(csv.reader(text.splitlines(), delimiter=delim))]
    return [("csv", rows)]


def find_header(rows):
    """Шапка — строка, где не менее трёх ячеек совпали со словарём колонок."""
    for rnum, row in rows[:10]:
        keys = [str(c).strip().lower() for c in row]
        hit = sum(1 for syn in REQUIRED.values() if any(s in keys for s in syn))
        if hit >= 3:
            return rnum, keys
    return None, None


def main():
    path = sys.argv[1]
    sheets = cells_from_xlsx(path) if path.lower().endswith((".xlsx", ".xlsm")) else cells_from_csv(path)
    report = {"file": path, "sheets": []}
    for name, rows in sheets:
        hnum, hdr = find_header(rows)
        entry = {"sheet": name, "header_row": hnum, "rows_total": len(rows)}
        if not hdr:
            entry["skipped"] = "шапка не найдена — лист не табличный"
            report["sheets"].append(entry)
            continue
        idx = {}
        for field, syn in REQUIRED.items():
            for s in syn:
                if s in hdr:
                    idx[field] = hdr.index(s)
                    break
        entry["columns"] = {f: hdr[i] for f, i in idx.items()}
        entry["missing_columns"] = [f for f in REQUIRED if f not in idx]
        data = [(rnum, r) for rnum, r in rows if rnum > hnum and any(str(c).strip() for c in r)]

        def get(r, field):
            i = idx.get(field)
            if i is None or i >= len(r):
                return ""
            return str(r[i]).strip()

        findings = {"empty": {}, "bad_purl": [], "detail_empty": [], "detail_affirmative": []}
        sev_c, st_c = {}, {}
        for rnum, r in data:
            sev, st = get(r, "severity").lower(), get(r, "status").lower()
            sev_c[sev] = sev_c.get(sev, 0) + 1
            st_c[st] = st_c.get(st, 0) + 1
        entry["rows_data"] = len(data)
        entry["severity_counts"] = sev_c
        entry["status_counts"] = st_c
        for field in REQUIRED:
            if field in idx:
                empty = [rnum for rnum, r in data if not get(r, field)]
                if empty:
                    findings["empty"][field] = empty
        scope = [(rnum, r) for rnum, r in data if get(r, "severity").lower() in ALLOWED_SEVERITY]
        entry["rows_in_scope"] = len(scope)
        entry["rows_dropped_by_severity"] = len(data) - len(scope)
        for rnum, r in scope:
            comp = get(r, "component")
            if comp and not PURL_RE.match(comp):
                findings["bad_purl"].append({"row": rnum, "component": comp})
            det = get(r, "detail")
            if not det:
                findings["detail_empty"].append({"row": rnum, "id": get(r, "vuln_id"),
                                                 "component": comp, "status": get(r, "status")})
            elif AFFIRMATIVE.search(det):
                findings["detail_affirmative"].append({"row": rnum, "id": get(r, "vuln_id"),
                                                       "component": comp, "status": get(r, "status"),
                                                       "detail": det[:200]})
        findings["status_outside_vocabulary"] = {k: v for k, v in st_c.items() if k not in ALLOWED_STATUS}
        findings["severity_outside_scope"] = {k: v for k, v in sev_c.items() if k not in ALLOWED_SEVERITY}
        entry["findings"] = findings
        report["sheets"].append(entry)
    out = json.dumps(report, ensure_ascii=False, indent=2)
    if "--json" in sys.argv:
        dest = sys.argv[sys.argv.index("--json") + 1]
        open(dest, "w", encoding="utf-8").write(out)
    print(out)


if __name__ == "__main__":
    main()
