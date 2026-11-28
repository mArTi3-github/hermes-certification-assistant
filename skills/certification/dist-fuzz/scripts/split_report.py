#!/usr/bin/env python3
"""Делит отчёт на публичную и внутреннюю части.

Публичная: 1 Итог, 2 Проверка требований, 3 Технические доказательства (3.1–3.7),
4 Устранение, приложения А–В.
Внутренняя: замечания, что запросить у разработчика, область и ограничения кампании,
перечень внутренних материалов.

usage: split_report.py <report.md> <out-public.md> <out-internal.md>
Разделы исходника берутся по заголовкам, а не по номерам строк: номера меняются,
когда отчёт правят.
"""
import re
import sys


def blocks(text):
    """Делит markdown на (заголовок-файла, [(уровень, заголовок, тело)])."""
    lines = text.split("\n")
    head, sections = [], []
    current = None
    for line in lines:
        m = re.match(r"^(#{2,3})\s+(.*)$", line)
        if m:
            current = [m.group(1), m.group(2), []]
            sections.append(current)
            continue
        if current is None:
            head.append(line)
        else:
            current[2].append(line)
    return head, sections


def find(sections, prefix):
    for level, title, body in sections:
        if title.startswith(prefix):
            return level, title, "\n".join(body).strip("\n")
    raise SystemExit("не найден раздел: %s" % prefix)


def retitle(title, new_number):
    """5.3 Триаж находок -> 3.3 Триаж находок; 5. Технические -> 3. Технические."""
    return re.sub(r"^\d+(\.\d+)?", new_number, title, count=1)


def renumber_refs(text):
    mapping = {"5.1": "3.1", "5.2": "3.2", "5.3": "3.3", "5.5": "3.4",
               "5.6": "3.5", "5.7": "3.6", "5.8": "3.7"}
    for old, new in mapping.items():
        text = re.sub(r"(раздел[ае]?\s)%s\b" % re.escape(old), r"\g<1>%s" % new, text)
        text = re.sub(r"(§\s*)%s\b" % re.escape(old), r"\g<1>%s" % new, text)
        text = re.sub(r"(раздел[ае]?\s)%s\b" % re.escape(old), r"\g<1>%s" % new, text)
    return text


src, out_public, out_internal = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src, encoding="utf-8").read()
head, sections = blocks(text)
head_text = "\n".join(head).strip("\n")

summary = find(sections, "1. Итог")[2]
requirements = find(sections, "2. Проверка требований")[2]
remarks = find(sections, "3. Замечания")[2]
requests = find(sections, "4. Что запросить у разработчика")[2]
evidence_intro = find(sections, "5. Технические доказательства")[2]
s51 = find(sections, "5.1")[2]
s52 = find(sections, "5.2")[2]
s53 = find(sections, "5.3")[2]
s54 = find(sections, "5.4")[2]
s55 = find(sections, "5.5")[2]
s56 = find(sections, "5.6")[2]
s57 = find(sections, "5.7")[2]
s58 = find(sections, "5.8")[2]
fix = find(sections, "6. Устранение")[2]
app_a = find(sections, "Приложение А")[2]
app_b = find(sections, "Приложение Б")[2]
app_c = find(sections, "Приложение В")[2]

# --- публичная часть -------------------------------------------------------
s55 = s55.replace("Сводка — `triage-baseline-2652.md`.", "Сводка — таблица ниже.")
s57 = s57.replace("журнала `evidence/build/queue.log`", "журнала запуска целей")
app_a = app_a.replace("`evidence/build/targets/<цель>/plateau_seconds`, `evidence/build/queue.log`, `evidence/build/queue-continuation2.log`",
                      "`evidence/build/targets/<цель>/plateau_seconds`, `evidence/build/targets/<цель>/log.txt`")
app_a = app_a.replace(", `evidence/build/pilot/corpus-<цель>.log`", "")
app_c = app_c.replace("Отчёт: итог, проверка требований, замечания, доказательства, устранение",
                      "Отчёт: итог, проверка требований, доказательства, устранение")
app_c = "\n".join(line for line in app_c.split("\n")
                  if "corpus-injection.txt" not in line and "triage-baseline-2652.md" not in line)

public = "\n".join([
    head_text,
    "",
    "## 1. Итог", summary,
    "",
    "## 2. Проверка требований", requirements,
    "",
    "## 3. Технические доказательства", evidence_intro,
    "",
    "### " + retitle(find(sections, "5.1")[1], "3.1"), s51,
    "",
    "### " + retitle(find(sections, "5.2")[1], "3.2"), s52,
    "",
    "### " + retitle(find(sections, "5.3")[1], "3.3"), s53,
    "",
    "### " + retitle(find(sections, "5.5")[1], "3.4"), s55,
    "",
    "### " + retitle(find(sections, "5.6")[1], "3.5"), s56,
    "",
    "### " + retitle(find(sections, "5.7")[1], "3.6"), s57,
    "",
    "### " + retitle(find(sections, "5.8")[1], "3.7"), s58,
    "",
    "## 4. Устранение", fix,
    "",
    "## Приложение А. Прослеживаемость требований", app_a,
    "",
    "## Приложение Б. Состав и целостность комплекта", app_b,
    "",
    "## Приложение В. Что в комплекте и зачем", app_c,
    "",
])
public = renumber_refs(public)
open(out_public, "w", encoding="utf-8").write(public)

# --- внутренняя часть ------------------------------------------------------
remarks = remarks.replace("раздел 5.6", "раздел 3.5 публичного отчёта")
requests = requests.replace("раздел 5.6", "раздел 3.5 публичного отчёта")
requests = requests.replace("раздел 5.5", "раздел 3.4 публичного отчёта")
s54 = s54.replace("`corpus-injection.txt`", "`internal/corpus-injection.txt`")

internal = "\n".join([
    "# Keycloak 26.8.2: внутренняя часть отчёта",
    "",
    "**Объект:** Keycloak 26.8.2, коммит `e514445f9dcdabc5bdcf67cab56c1e49655de067`",
    "**Публичная часть:** `report.md` / `report.html` того же комплекта (разделы 1–4 и приложения)",
    "**Назначение:** замечания к материалам, запросы к разработчику и ограничения кампании. "
    "Документ для внутреннего пользования, внешним проверяющим не передаётся.",
    "",
    "## 1. Замечания", remarks,
    "",
    "## 2. Что запросить у разработчика", requests,
    "",
    "## 3. Область и ограничения кампании", s54,
    "",
    "## Приложение. Внутренние материалы комплекта",
    "",
    "| Файл или каталог | Зачем нужен | Раздел |",
    "|---|---|---|",
    "| `internal/corpus-injection.txt` | что перенесено из кампании 26.5.2 и с каким эффектом | 3 |",
    "| `internal/triage-baseline-2652.md` | база триажа: вердикты 26.5.2 и их перепроверка на 26.8.2 | 1 |",
    "| `internal/compare-2652-signatures.txt` | пересчёт сигнатур 26.5.2 тем же скриптом | 1 |",
    "| `internal/queue.log`, `internal/queue-continuation2.log` | журнал супервизора: решения об остановке целей и плато | 1, 3 |",
    "| `internal/pilot/` | пилотные прогоны до кампании (проверка обвязки) | 3 |",
    "| `internal/seeds-2652-carry/` | стартовый корпус прошлой кампании в исходном виде | 3 |",
    "",
])
internal = re.sub(r"в разделе 5\.4\b", "в разделе 3 настоящего документа", internal)
open(out_internal, "w", encoding="utf-8").write(internal)
print("публичная часть: %d строк, внутренняя: %d строк" % (
    len(public.split("\n")), len(internal.split("\n"))))
