#!/usr/bin/env python3
"""Общая часть двух этапов: чтение отчёта, версии, орфография, диапазоны NVD.

Используется `check_report.py` (этап 1) и `build_report.py` (этап 2), чтобы правила не
разъезжались между этапами. Требует openpyxl; в этом профиле — интерпретатор из
`~/.hermes/tools/python-*/bin/python3`.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from openpyxl import load_workbook
except ImportError:  # pragma: no cover
    raise SystemExit("нужен openpyxl: '<tools>/bin/python3' -m pip install openpyxl")

FIELDS = {
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

# опечатки, которые уже встречались в отчётах; ключ — как написано, значение — как надо
TYPOS = {
    "испольузется": "используется", "используеться": "используется",
    "использовуется": "используется", "неэксплуатируемость": "неэксплуатируемость",
    "уязвимостьть": "уязвимость", "уязвимсоть": "уязвимость",
    "компанента": "компонента", "компонет": "компонент",
    "библиотекa": "библиотека", "влияет на безопастность": "влияет на безопасность",
    "безопаснось": "безопасность", "уязвимостейй": "уязвимостей",
    "обоснованние": "обоснование", "эксплуатаця": "эксплуатация",
    "достижимостьть": "достижимость", "недотсупен": "недоступен",
    "следующая уязвимость": "следующая уязвимость", "проиходит": "происходит",
}
# тексты-обрывки: только имя продукта или оборванная фраза
BROKEN_TAIL = re.compile(
    r"(?:\b(?:к|по|для|в|на|с|при|из|от|или|и|а|но|чем|что|без|о|об|над|под|за)\s*[.,;]?)$|"
    r"(?:…|\.\.\.)\s*$|,\s*$", re.I)
AFFIRMATIVE = re.compile(
    r"(?<!не\s)(?<!не)(уязвимость\s+(?:актуальна|применима|подтверждена|воспроизводится|"
    r"эксплуатируется)|эксплуатация\s+возможна|содержит\s+уязвимость)", re.I)
WRONG_PRODUCT = re.compile(r"^Уязвимость\s+(?:для|в)\s+|^JMESPath for Ruby$|^Nayshlok Voyager$", re.I)
FIXED_FRAGMENT = re.compile(r"^Исправлено(?:\s+в)?\s+([0-9][0-9A-Za-z.+~:-]*)$")
VERSION_IN_TEXT = re.compile(r"\b(\d+\.\d+(?:\.\d+)*(?:[.-][0-9A-Za-z]+)*)\b")


# ---------------------------------------------------------------- версии
def component_version(purl: str) -> str:
    """Версия из purl без qualifiers: `zlib@1.3.1-r2?arch=x86_64&distro=...` → `1.3.1-r2`."""
    if "@" not in purl:
        return ""
    ver = purl.rsplit("@", 1)[1]
    for sep in ("?", "#"):
        ver = ver.split(sep)[0]
    return urllib.parse.unquote(ver).strip()


def ver_tuple(v: str):
    """`1.3.1-r2` → (1,3,1,2); `18.1-1.pgdg13+2` → (18,1,1).

    Debian пишет настоящую версию после `+really` (`2.12.7+dfsg+really2.9.14-2.1` — это 2.9.14);
    суффиксы после `+`, `~`, `-` отбрасываются.
    """
    if not v:
        return ()
    if "really" in v:
        v = v.split("really", 1)[1]
    v = v.split("+")[0]
    v = re.sub(r"-r(\d+)", r".\1", v)
    v = re.sub(r"[-~].*$", "", v)
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts[:6])


def effective_version(v: str) -> str:
    """`2.12.7+dfsg+really2.9.14-2.1` → `2.9.14` (Debian пишет настоящую версию после `really`)."""
    if v and "really" in v:
        return v.split("really", 1)[1].split("+")[0].split("-")[0]
    return v


def ver_cmp(a, b):
    ta, tb = ver_tuple(a), ver_tuple(b)
    n = max(len(ta), len(tb))
    ta, tb = ta + (0,) * (n - len(ta)), tb + (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


VERSION_RE = re.compile(r"^\d+(\.\d+)*([-.+_~][0-9A-Za-z.+~_-]+)?$")


def clean_intervals(intervals):
    """Выбросить границы-даты (`2014-11-17`) и пустые интервалы.

    В cpeMatch попадаются `versionEndExcluding` с датой вместо версии — сравнение с версией
    компонента на таком значении даёт мусорное «затронуто».
    """
    out = []
    for iv in intervals or []:
        clean = {}
        for key in ("from", "to"):
            v = iv.get(key)
            if v is None:
                continue
            if not VERSION_RE.match(str(v)) or re.match(r"^\d{4}-\d{2}-\d{2}$", str(v)):
                continue
            clean[key] = str(v)
        if not clean:
            continue
        if "to_incl" in iv:
            clean["to_incl"] = iv["to_incl"]
        out.append(clean)
    return out


def in_ranges(version: str, intervals) -> bool:
    for iv in intervals:
        lo, hi = iv.get("from"), iv.get("to")
        if lo is not None and ver_cmp(version, lo) < 0:
            continue
        if hi is not None:
            c = ver_cmp(version, hi)
            if iv.get("to_incl", True):
                if c > 0:
                    continue
            elif c >= 0:
                continue
        return True
    return False


def ranges_text(intervals) -> str:
    out = []
    for iv in intervals:
        lo, hi = iv.get("from"), iv.get("to")
        if lo and hi:
            out.append(f"{lo}–{'<=' if iv.get('to_incl', True) else '<'}{hi}")
        elif hi:
            out.append(f"{'<=' if iv.get('to_incl', True) else '<'}{hi}")
        elif lo:
            out.append(f">={lo}")
    return ", ".join(out)


_LAST_CALL = [0.0]
BUNDLED = Path(__file__).resolve().parent.parent / "references" / "version-ranges.json"


# имена, которые в NVD встречаются у разных вендоров (python — и CPython, и расширение VSCode)
AMBIGUOUS_NAMES = {"python", "go", "ruby", "node.js", "openssl", "java"}


def intervals_for(entries, component: str):
    """Диапазоны, относящиеся к нашему компоненту: продукт cpeMatch против имени purl.

    Один CVE в NVD часто описан для нескольких продуктов (CVE-2022-37434 — zlib и postgres),
    и без фильтра «в диапазоне» окажется чужая ветка версий.
    """
    if isinstance(entries, list):  # совсем старый формат — без продуктов
        return clean_intervals(entries)
    if not entries:
        return None
    if "products" in entries:      # текущий формат справочника
        entries = entries["products"]
    elif "ranges" in entries:      # плоская запись (один продукт, без имени)
        return clean_intervals(entries.get("ranges") or [])
    name = component.split("/")[-1].split("@")[0].lower() if component else ""
    if not name:
        return None
    exact, fuzzy = [], []
    for key, ivs in entries.items():
        raw = str(key)
        has_vendor = ":" in raw
        vendor, _, prod = raw.partition(":")
        prod = (prod or vendor).lower()
        vendor = vendor.lower()
        if not has_vendor and prod in AMBIGUOUS_NAMES:
            continue      # «python» без вендора может быть и CPython, и расширением VSCode
        # `ansible` в NVD и `ansible-core` в purl — один и тот же продукт
        if prod != name and not (name in AMBIGUOUS_NAMES or prod in AMBIGUOUS_NAMES) and (
                prod in name or name in prod):
            fuzzy.append((vendor, ivs))
            continue
        if prod != name:
            continue
        if vendor == name:            # python:python, zlib:zlib — тот самый продукт
            exact.extend(ivs)
        else:
            fuzzy.append((vendor, ivs))
    if exact:
        return clean_intervals(exact)
    if name in AMBIGUOUS_NAMES:
        return None                   # microsoft:python — это расширение VSCode, не CPython
    if len(fuzzy) == 1:
        return clean_intervals(fuzzy[0][1])
    return None


def bundled_intervals(cve: str):
    """Диапазоны из файла рядом со скиллом — работает без сети."""
    if not BUNDLED.exists():
        return None
    try:
        data = json.loads(BUNDLED.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if cve not in data:
        return None
    value = data[cve]
    if isinstance(value, dict) and "products" in value:
        return value["products"]
    # {"ranges": [...]} или сам список интервалов — старый формат
    return clean_intervals(value["ranges"] if isinstance(value, dict) and "ranges" in value else value)


def nvd_intervals(cve: str, cache_dir: Path, force: bool = False):
    """Диапазоны из NVD cpeMatch с кэшем на диске. None — не удалось получить.

    NVD без ключа пускает 5 запросов за 30 секунд, поэтому запросы разнесены во времени;
    при отказе сети берётся файл `references/version-ranges.json`.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{cve}.json"
    if cache.exists() and not force:
        try:
            raw = json.loads(cache.read_text(encoding="utf-8"))
            return {k: clean_intervals(v) for k, v in raw.items()} if isinstance(raw, dict) else raw
        except json.JSONDecodeError:
            pass
    url = f"https://services.nvd.nist.gov/rest/json/cves/2.0?cveId={cve}"
    req = urllib.request.Request(url, headers={"User-Agent": "src-dep-report-check/2.0"})
    for attempt in (1, 2):
        wait = 6.5 - (time.time() - _LAST_CALL[0])
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL[0] = time.time()
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.load(r)
            break
        except Exception:
            if attempt == 2:
                return bundled_intervals(cve)
            continue
    try:
        vulns = data.get("vulnerabilities") or []
        if not vulns:
            return None
        by_product = {}
        for cfg in vulns[0]["cve"].get("configurations", []):
            for node in cfg.get("nodes", []):
                for m in node.get("cpeMatch", []):
                    parts = str(m.get("criteria", "")).split(":")
                    vendor = parts[3] if len(parts) > 3 else ""
                    product = parts[4] if len(parts) > 4 else ""
                    product = f"{vendor}:{product}" if product else ""
                    iv = {}
                    if m.get("versionStartIncluding"):
                        iv["from"] = m["versionStartIncluding"]
                    elif m.get("versionStartExcluding"):
                        iv["from"] = m["versionStartExcluding"]
                        iv["from_excl"] = True
                    if m.get("versionEndExcluding"):
                        iv["to"], iv["to_incl"] = m["versionEndExcluding"], False
                    elif m.get("versionEndIncluding"):
                        iv["to"], iv["to_incl"] = m["versionEndIncluding"], True
                    if iv:
                        by_product.setdefault(product, []).append(iv)
        intervals = {prod: clean_intervals(ivs) for prod, ivs in by_product.items()}
        cache.write_text(json.dumps(intervals, ensure_ascii=False), encoding="utf-8")
        return intervals
    except Exception:  # неожиданный формат ответа — этап продолжается без диапазона
        return bundled_intervals(cve)


# ---------------------------------------------------------------- чтение
def find_header(rows):
    for rnum, row in rows[:10]:
        keys = [str(c).strip().lower() if c is not None else "" for c in row]
        hit = sum(1 for syn in FIELDS.values() if any(s in keys for s in syn))
        if hit >= 3:
            return rnum, keys
    return None, None


def column_map(header):
    idx = {}
    for field, syn in FIELDS.items():
        for s in syn:
            if s in header:
                idx[field] = header.index(s)
                break
    return idx


def _bytes_via_cp1251(text):
    """Обратное кодирование, когда в строке остались управляющие байты (0x98 и подобные).

    Конвертер без таблицы cp1251 оставляет такие байты как есть (U+0098), и обычный
    `encode("cp1251")` падает. Здесь байты младше 0x100 переносятся как есть, остальное — cp1251.
    """
    out = bytearray()
    for ch in text:
        if ord(ch) < 0x100:
            out.append(ord(ch))
            continue
        try:
            out += ch.encode("cp1251")
        except UnicodeEncodeError:
            out += b"?"
    try:
        return bytes(out).decode("utf-8")
    except UnicodeDecodeError:
        return None


def fix_mojibake(text):
    """`РњС‹ РЅРµ` → `Мы не`: UTF-8, прочитанный как cp1251.

    Выгрузки перекодированы неровно: часть символов имеет cp1251-аналог, байты без аналога
    (0x98) остаются управляющими. Сначала обычный проход, затем побайтовый — до трёх раз.
    Настоящий русский текст в UTF-8 так не декодируется, поэтому исходное не портится.
    """
    if not isinstance(text, str) or ("Р" not in text and "С" not in text):
        return text
    cur = text
    for _ in range(3):
        try:
            nxt = cur.encode("cp1251").decode("utf-8")
        except UnicodeEncodeError:
            nxt = _bytes_via_cp1251(cur)
        except UnicodeDecodeError:
            break
        if not nxt or nxt == cur:
            break
        cur = nxt
    return cur


def _csv_cells(rows):
    """Лист-одна-колонка с CSV-текстом (выгрузка в ячейку): разбираем в таблицу."""
    import csv
    import io
    vals = [fix_mojibake(str(v)) for _, row in rows for v in row if v not in (None, "")]
    if not vals:
        return None
    head = vals[0]
    if head.count(",") + head.count(";") + head.count("\t") < 3:
        return None
    delim = max((",", ";", "\t"), key=head.count)
    parsed = [r for r in csv.reader(io.StringIO("\n".join(vals)), delimiter=delim)]
    return [(i + 1, [fix_mojibake(c) for c in r]) for i, r in enumerate(parsed)]


def read_table(path: Path):
    """Самая содержательная таблица книги: (workbook, sheetname, header_row, colmap, rows).

    Лист ищется по числу строк данных, а не по порядку: в выгрузках первый лист часто
    оказывается обрывком, а полный отчёт лежит на следующем. Лист из одной колонки с CSV-текстом
    (типичная выгрузка и она же в cp1251) разбирается как таблица.
    """
    if path.suffix.lower() == ".csv":
        import csv
        # csv не несёт кодировку: пробуем utf-8 (с BOM и без), затем cp1251/cp1252,
        # а двойное перекодирование («РњС‹ РЅРµ») снимаем поячеечно — иначе обоснование
        # триажа уезжает в xlsx мойибаке
        raw = path.read_bytes()
        text = None
        for enc in ("utf-8-sig", "utf-8", "cp1251", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            text = raw.decode("cp1251", errors="replace")
        delim = max((";", ",", "\t"), key=lambda d: text.split("\n", 1)[0].count(d))
        rows = [(i + 1, [fix_mojibake(c) for c in r])
                for i, r in enumerate(csv.reader(text.splitlines(), delimiter=delim))]
        hnum, hdr = find_header(rows)
        if not hdr:
            raise SystemExit("шапка отчёта не найдена")
        return None, "csv", hnum, column_map(hdr), rows

    wb = load_workbook(path)
    best = None
    for ws in wb.worksheets:
        rows = [(r[0].row, [c.value for c in r]) for r in ws.iter_rows()]
        cands = []
        hnum, hdr = find_header(rows)
        if hdr:
            cands.append((rows, hnum, hdr))
        if not hdr:      # лист-выгрузка в одну колонку
            parsed = _csv_cells(rows)
            if parsed:
                ph, phdr = find_header(parsed)
                if phdr:
                    cands.append((parsed, ph, phdr))
        for rws, h, hd in cands:
            filled = sum(1 for n, r in rws if n > h and any(str(v).strip() for v in r if v is not None))
            if best is None or filled > best[0]:
                best = (filled, ws.title, h, column_map(hd), rws)
    if best is None:
        raise SystemExit("шапка отчёта не найдена ни на одном листе")
    _, title, hnum, idx, rows = best
    return wb, title, hnum, idx, rows


def cell(row, idx, field):
    i = idx.get(field)
    if i is None or i >= len(row):
        return ""
    v = row[i]
    return "" if v is None else str(v).strip()


# ---------------------------------------------------------------- орфография
def fix_text(text: str):
    """Только орфография и лишние пробелы. Формулировка не переписывается.

    Возвращает (новый текст, список оснований). Пунктуация и смысловые правки здесь
    запрещены: «формулировки в рамках правки отчёта не меняются» — они идут в рекомендации.
    """
    if not text:
        return text, []
    fixed, notes = text, []
    for bad, good in TYPOS.items():
        if bad in fixed:
            fixed = fixed.replace(bad, good)
            notes.append(f"опечатка: «{bad}» → «{good}»")
    new = re.sub(r"[ \t]{2,}", " ", fixed).strip()
    if new != fixed:
        notes.append("формат: лишние пробелы")
        fixed = new
    return fixed, notes


# утверждения о неиспользуемости компонента — законное обоснование автора отчёта, не дефект
NON_USE = re.compile(r"не\s+(?:использ|примен|задейств|эксплуатир|подключ|вызыва)", re.I)


def is_broken(text: str) -> bool:
    """Обрыв или мусор в формулировке. Короткое «не используется» обрывом не считается."""
    t = (text or "").strip()
    if not t:
        return False
    if BROKEN_TAIL.search(t) or FIXED_FRAGMENT.match(t) or WRONG_PRODUCT.match(t):
        return True
    return False
