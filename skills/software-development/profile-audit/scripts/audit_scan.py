#!/usr/bin/env python3
"""Сканер материалов профиля: собирает улики для аудита противоречий.

Инструмент ничего не меняет и ничего не решает: он находит подозрительные места, показывает
цитаты с номерами строк, предлагает классификацию по семи классам и готовый вопрос пользователю.
Решение принимает человек в диалоге — см. SKILL.md.

    python3 scripts/audit_scan.py                      # материалы клона и профиля
    python3 scripts/audit_scan.py --topic sync        # только то, что касается темы
    python3 scripts/audit_scan.py --memory             # плюс MEMORY.md и USER.md
    python3 scripts/audit_scan.py --json --out audit.json

Что проверяется (каждая проверка либо даёт находки, либо попадает в список «проверено, чисто»):

    paths            ссылки на файлы и скрипты, которых нет; машинно-локальные пути
    counts           «N шагов/скиллов/инструментов» против фактического количества
    step_refs        «шаги 1–3 и 5–7» против реальной нумерации раздела
    duplicates       почти одинаковые правила в разных местах, расходящиеся в цифрах/отрицаниях
    hedges           «обычно», «как правило», «должно работать» в нормативном тексте
    versions         версии в примерах, которых нет ни в манифесте, ни в истории тегов
    temporal         «сейчас/текущий» рядом с версией или датой
    memory           дубли, расходящиеся числа и переполнение в MEMORY.md / USER.md

Коды возврата: 0 — находок нет; 1 — есть находки; 2 — не найдено окружение.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# --- классификация ------------------------------------------------------------

CLASSES = {
    "factual": "Фактическое противоречие",
    "temporal": "Временное противоречие",
    "contextual": "Контекстное противоречие",
    "terminology": "Терминологическая неоднозначность",
    "stale": "Устаревшая информация",
    "underdetermined": "Недостаточная определённость",
    "assumption": "Неподтверждённое предположение",
}

SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}

# Где вообще имеет смысл искать нормативные утверждения.
DOC_GLOBS = ("skills/**/SKILL.md", "skills/**/references/*.md", "skills/**/templates/*.md",
             "README.md", "CHANGELOG.md", "distribution.yaml", "publish.list", "SOUL.md")

# Только те оговорки, которые выдают непроверенное утверждение за правило. Описательные
# «обычно лежит на NFS» — это знание о среде, а не дыра в документе, и сюда не входят.
HEDGES = (
    "как правило", "по идее", "должно работать", "должно быть достаточно", "скорее всего",
    "предположительно", "вероятно", "вроде бы", "кажется", "по-видимому",
    "should work", "probably", "presumably", "i think",
)

PRESENT_MARKERS = ("сейчас", "текущ", "на данный момент", "в настоящий момент", "теперь",
                   "актуальн", "по состоянию на")

MACHINE_PATH = re.compile(r"(~?/home/[^\s`'\"()]+|~/[^\s`'\"()]+|~?/Users/[^\s`'\"()]+"
                          r"|[A-Z]:\\\\[^\s`'\"]+)")
BACKTICK = re.compile(r"`([^`\n]{2,120})`")
VERSION = re.compile(r"\bv?(\d+\.\d+\.\d+)\b")
DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2}|\d{2}\.\d{2}\.20\d{2})\b")
STEP_RANGE = re.compile(r"шаг[аио]?\s+([\d\s,и–—-]+?)(?=[.,;:)\s]|$)")
COUNT_CLAIM = re.compile(r"\b(\d+)\s+(шаг|пункт|скилл|инструмент|файл|запис)\w*", re.I)
NUMBERED = re.compile(r"^\s*(\d+)[.)]\s+\S")
HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

SKIP_DIRS = {"__pycache__", ".git", ".backup", "node_modules", ".pytest_cache"}


# --- модель находки -----------------------------------------------------------

@dataclass
class Evidence:
    path: str
    line: int
    quote: str


@dataclass
class Finding:
    check: str
    cls: str
    severity: str
    title: str
    why: str
    question: str
    evidence: list[Evidence] = field(default_factory=list)
    options: list[str] = field(default_factory=list)
    fingerprint: str = ""
    previously_resolved: dict | None = None

    def finalize(self) -> "Finding":
        payload = "|".join([self.check, *[f"{e.path}:{e.line}:{e.quote[:200]}"
                                        for e in self.evidence]])
        self.fingerprint = hashlib.sha256(payload.encode()).hexdigest()[:16]
        return self


# --- общие утилиты ------------------------------------------------------------

def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def quote(text: str, index: int, width: int = 140) -> str:
    start = text.rfind("\n", 0, index) + 1
    end = text.find("\n", index)
    line = text[start:end if end != -1 else len(text)].strip()
    return line if len(line) <= width else line[:width - 1] + "…"


def section_of(text: str, index: int) -> tuple[str, int, int]:
    """Заголовок раздела, в котором стоит позиция, и границы раздела."""
    start = 0
    title = ""
    for match in HEADING.finditer(text, 0, index):
        start, title = match.start(), match.group(2).strip()
    nxt = HEADING.search(text, index)
    return title, start, nxt.start() if nxt else len(text)


def numbered_items(chunk: str) -> list[int]:
    return [int(m.group(1)) for m in NUMBERED.finditer(chunk)]


def tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[\w\-]{4,}", text.lower())}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def config_memory_limits(profile: Path) -> dict[str, int]:
    """Лимиты памяти из config.yaml; без парсера YAML — регуляркой по двум ключам."""
    text = read(profile / "config.yaml")
    limits: dict[str, int] = {}
    for key, name in (("memory_char_limit", "MEMORY.md"), ("user_char_limit", "USER.md")):
        match = re.search(rf"^\s*{key}:\s*(\d+)", text, re.M)
        if match:
            limits[name] = int(match.group(1))
    return limits


# --- проверки материалов ------------------------------------------------------

def collect_docs(repo: Path, extra: list[Path]) -> list[Path]:
    docs: list[Path] = []
    for pattern in DOC_GLOBS:
        docs.extend(sorted(repo.glob(pattern)))
    for path in extra:
        if path.is_dir():
            docs.extend(sorted(p for p in path.rglob("*.md")
                               if not any(part in SKIP_DIRS for part in p.parts)))
        elif path.is_file():
            docs.append(path)
    return [d for d in dict.fromkeys(docs) if d.is_file()]


def extract_paths(text: str) -> list[tuple[str, int]]:
    """Ссылки на конкретные файлы: глобы, каталоги и плейсхолдеры сюда не относятся."""
    found: list[tuple[str, int]] = []
    for match in BACKTICK.finditer(text):
        token = match.group(1).strip()
        head = token.split()[0] if token.split() else ""
        head = head.rstrip(".,;:")
        if not head or any(ch in head for ch in "*?<>{}()$"):
            continue
        if head.endswith("/") or "." not in head.split("/")[-1]:
            continue
        if head.startswith(("references/", "scripts/", "templates/", "assets/", "tools/",
                            "skills/", "tests/", ".github/", "docs/")):
            found.append((head, match.start()))
    return found


def basename_index(roots: list[Path]) -> dict[str, list[Path]]:
    """Имена всех файлов материалов: нужно, чтобы отличить «нет файла» от «файл в другом скилле»."""
    names: dict[str, list[Path]] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for item in root.rglob("*"):
            if item.is_file() and not any(part in SKIP_DIRS for part in item.parts):
                names.setdefault(item.name, []).append(item)
    return names


def skill_root_of(doc: Path) -> Path | None:
    """Каталог скилла, к которому относится документ (для относительных ссылок)."""
    for parent in list(doc.parents)[:4]:
        if (parent / "SKILL.md").is_file():
            return parent
    return None


def locate_basename(index: dict[str, list[Path]], name: str) -> list[Path]:
    return index.get(name, [])


def check_paths(docs: list[Path], repo: Path, profile: Path) -> list[Finding]:
    findings: list[Finding] = []
    index = basename_index([repo / "skills", profile / "skills", repo / "tools"])
    for path in docs:
        text = read(path)
        root = skill_root_of(path)
        for token, index_pos in extract_paths(text):
            skill_relative = token.startswith(("references/", "scripts/", "templates/", "assets/"))
            candidates: list[Path] = []
            if skill_relative and root:
                candidates = [root / token]
            elif skill_relative:
                candidates = [path.parent / token, repo / token]
            candidates += [repo / token, profile / token]
            if any(c.exists() for c in candidates):
                continue

            elsewhere = locate_basename(index, token.split("/")[-1])
            rel = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
            if elsewhere:
                where = ", ".join(str(p.parent) for p in elsewhere[:2])
                findings.append(Finding(
                    check="paths", cls="stale", severity="medium",
                    title=f"Ссылка ведёт не туда: {token}",
                    why="Файл с таким именем в материалах есть, но не рядом с этим документом: "
                        "ссылка разрешается относительно каталога скилла, поэтому она не откроется.",
                    question=f"Файл «{token}» лежит в {where}. Исправить путь на "
                             f"`skills/.../{token}` или добавить копию в этот скилл?",
                    evidence=[Evidence(rel, line_of(text, index_pos), quote(text, index_pos))],
                    options=["исправить путь на абсолютный от корня репозитория",
                             "добавить файл в этот скилл"],
                ).finalize())
                continue

            findings.append(Finding(
                check="paths", cls="factual", severity="high",
                title=f"Ссылка на отсутствующий путь: {token}",
                why="Документ ссылается на файл, которого нет ни в этом скилле, ни в репозитории: "
                    "читатель (человек или агент) пойдёт по ссылке и не найдёт её.",
                question=f"Файл «{token}» упомянут как существующий, но его нет. Он переименован, "
                         "удалён или ссылку нужно заменить?",
                evidence=[Evidence(rel, line_of(text, index_pos), quote(text, index_pos))],
                options=["исправить путь на актуальный", "убрать ссылку", "создать файл"],
            ).finalize())

        hits = list(MACHINE_PATH.finditer(text))
        if hits:
            rel = str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path)
            samples = [Evidence(rel, line_of(text, h.start()), quote(text, h.start()))
                       for h in hits[:3]]
            findings.append(Finding(
                check="paths", cls="stale", severity="low",
                title=f"Машинно-локальные пути: {len(hits)} в {path.name}",
                why="Путь вида /home/... или ~/... верен только при одинаковой раскладке машин; "
                    "после переноса профиля инструкция ломается. Одна находка на файл — чтобы "
                    "не заваливать сессию однотипными пунктами.",
                question="Эти пути переносимы (у коллег та же раскладка) или их стоит заменить "
                         "относительными?",
                evidence=samples,
                options=["оставить: раскладка машин одинаковая", "заменить на относительные пути"],
            ).finalize())
    return findings


def check_counts(docs: list[Path], repo: Path, skill_count: int, tool_count: int) -> list[Finding]:
    findings: list[Finding] = []
    for path in docs:
        text = read(path)
        for match in COUNT_CLAIM.finditer(text):
            claimed = int(match.group(1))
            noun = match.group(2).lower()
            title, actual = None, None
            if noun.startswith("шаг") or noun.startswith("пункт"):
                _, start, end = section_of(text, match.start())
                items = numbered_items(text[start:end])
                if items:
                    title, actual = "шагов в разделе", max(items)
            elif noun.startswith("скилл"):
                title, actual = "скиллов в репозитории", skill_count
            elif noun.startswith("инструмент"):
                title, actual = "файлов в tools/", tool_count
            if actual is None or claimed == actual:
                continue
            findings.append(Finding(
                check="counts", cls="factual", severity="medium",
                title=f"Число не совпадает с фактическим: {claimed} против {actual} ({title})",
                why="Утверждение о количестве проверяемо и уже неверно: это прямой конфликт текста "
                    "с состоянием репозитория.",
                question=f"В тексте «{quote(text, match.start(), 90)}», фактически {title} — {actual}. "
                         "Исправить число или изменить структуру, чтобы число стало верным?",
                evidence=[Evidence(str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path),
                                   line_of(text, match.start()), quote(text, match.start()))],
                options=[f"поставить {actual}", "переписать без числа"],
            ).finalize())
    return findings


def check_step_refs(docs: list[Path], repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in docs:
        text = read(path)
        for match in STEP_RANGE.finditer(text):
            raw = match.group(1)
            nums = [int(n) for n in re.findall(r"\d+", raw)]
            if len(nums) < 1:
                continue
            title, start, end = section_of(text, match.start())
            items = numbered_items(text[start:end])
            if not items:
                continue
            top = max(items)
            if max(nums) <= top:
                continue
            findings.append(Finding(
                check="step_refs", cls="factual", severity="medium",
                title=f"Ссылка на несуществующий шаг: {raw.strip()} при {top} шагах",
                why="Нумерация в соседнем месте документа другая: ссылка ведёт в пустоту и читается "
                    "как ошибка в тексте, а не в нумерации.",
                question=f"В разделе «{title}» {top} шагов, а ссылка указывает до {max(nums)}. "
                         "Пересчитать номера в ссылке или в разделе?",
                evidence=[Evidence(str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path),
                                   line_of(text, match.start()), quote(text, match.start()))],
                options=["исправить ссылку", "перестроить нумерацию раздела"],
            ).finalize())
    return findings


def check_duplicates(docs: list[Path], repo: Path) -> list[Finding]:
    """Похожие правила в разных местах: одинаковый смысл, разные цифры или отрицания."""
    lines: list[tuple[Path, int, str, set[str]]] = []
    for path in docs:
        in_code = False
        for number, raw in enumerate(read(path).splitlines(), start=1):
            line = raw.strip()
            if line.startswith("```"):
                in_code = not in_code
                continue
            if in_code or len(line) < 45 or line.startswith(("|", "#", "- [ ]")):
                continue
            if any(mark in line for mark in ("<", "w:", "http://", "https://")):
                continue
            lines.append((path, number, line, tokens(line)))

    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for i, (path_a, line_a, text_a, toks_a) in enumerate(lines):
        for path_b, line_b, text_b, toks_b in lines[i + 1:]:
            if text_a == text_b:
                continue
            similarity = jaccard(toks_a, toks_b)
            if similarity < 0.72:
                continue
            digits_a = set(re.findall(r"\d+", text_a))
            digits_b = set(re.findall(r"\d+", text_b))
            if digits_a == digits_b:
                continue
            key = (min(text_a[:60], text_b[:60]), max(text_a[:60], text_b[:60]))
            if key in seen:
                continue
            seen.add(key)
            if len(findings) >= 20:
                return findings
            rel = lambda p: str(p.relative_to(repo)) if p.is_relative_to(repo) else str(p)
            findings.append(Finding(
                check="duplicates", cls="contextual", severity="medium",
                title="Одно правило в двух местах расходится в цифрах",
                why="Сходство формулировок высокое, а числа разные: либо это разные условия "
                    "применения (контекстное расхождение), либо одна из записей устарела.",
                question="Эти два утверждения относятся к разным условиям или одно из них надо "
                         "привести к другому?",
                evidence=[Evidence(rel(path_a), line_a, text_a), Evidence(rel(path_b), line_b, text_b)],
                options=["свести к одной формулировке", "явно развести по условиям",
                         "удалить устаревшее"],
            ).finalize())
    return findings


def check_hedges(docs: list[Path], repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in docs:
        if path.name not in {"SKILL.md", "README.md"}:
            continue
        text = read(path)
        for word in HEDGES:
            for match in re.finditer(rf"(?i)\b{re.escape(word)}\b", text):
                line = quote(text, match.start())
                if line.startswith(("|", ">", "```")):
                    continue
                # Цитата чужой формулировки («наверное, собирали») — это пример ошибки, а не правило.
                if "«" in line[:line.find(word) + len(word)] and "»" in line[line.find(word):]:
                    continue
                findings.append(Finding(
                    check="hedges", cls="assumption", severity="low",
                    title=f"Осторожная формулировка «{word}» в нормативном тексте",
                    why="Нормативный документ читается как правило: оговорка означает, что автор не "
                        "проверил границы применимости, а читатель примет утверждение за факт.",
                    question=f"«{line[:120]}» — это верно всегда или только при определённых "
                             "условиях? Если при условиях, их стоит назвать.",
                    evidence=[Evidence(str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path),
                                       line_of(text, match.start()), line)],
                    options=["уточнить условие применимости", "переформулировать как проверенный факт",
                             "оставить, это осознанное обобщение"],
                ).finalize())
    return findings


def frontmatter_span(text: str) -> tuple[int, int]:
    """Границы YAML-фронтматтера: его поля — атрибуты файла, а не утверждения о мире."""
    if not text.startswith("---"):
        return (0, 0)
    end = text.find("\n---", 3)
    return (0, end + 4) if end != -1 else (0, 0)


def known_versions(repo: Path) -> set[str]:
    versions = set(VERSION.findall(read(repo / "distribution.yaml")))
    changelog = read(repo / "CHANGELOG.md")
    versions |= set(re.findall(r"^##\s+v?(\d+\.\d+\.\d+)", changelog, re.M))
    versions |= set(re.findall(r"\bv(\d+\.\d+\.\d+)\b", changelog))
    return versions


def check_versions(docs: list[Path], repo: Path) -> list[Finding]:
    known = known_versions(repo)
    findings: list[Finding] = []
    reported: set[tuple[str, str]] = set()
    for path in docs:
        text = read(path)
        head_end = frontmatter_span(text)[1]
        for match in VERSION.finditer(text):
            version = match.group(1)
            if version in known:
                continue
            if match.start() < head_end:
                continue
            line = quote(text, match.start())
            if re.match(r"\s*version\s*:", line):
                continue
            if not (re.search(r"(?i)верси|релиз", line) or match.group(0).startswith("v")):
                continue
            if (str(path), version) in reported:
                continue
            reported.add((str(path), version))
            findings.append(Finding(
                check="versions", cls="stale", severity="low",
                title=f"Версия {version} вне манифеста и истории",
                why="Номер версии в примере или тексте не встречается ни в distribution.yaml, "
                    "ни в разделах CHANGELOG: либо это выдуманный пример, либо устаревшая версия.",
                question=f"Версия {version} в «{quote(text, match.start(), 80)}» — это пример "
                         "или актуальное значение?",
                evidence=[Evidence(str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path),
                                   line_of(text, match.start()), quote(text, match.start()))],
                options=["заменить на актуальную версию", "оставить как пример с пометкой"],
            ).finalize())
    return findings


def check_temporal(docs: list[Path], repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in docs:
        if path.name == "CHANGELOG.md":
            continue  # журнал по определению говорит о прошлых состояниях
        text = read(path)
        for marker in PRESENT_MARKERS:
            for match in re.finditer(rf"(?i)\b{re.escape(marker)}", text):
                line = quote(text, match.start())
                if not (DATE.search(line) or VERSION.search(line)):
                    continue
                findings.append(Finding(
                    check="temporal", cls="temporal", severity="medium",
                    title="Утверждение «сейчас» привязано к версии или дате",
                    why="Слова «текущий/сейчас» рядом с версией или датой устаревают молча: "
                        "после следующего релиза утверждение станет ложным без правки текста.",
                    question=f"«{line[:120]}» — привязать к версии/дате явно или переписать так, "
                             "чтобы утверждение не устаревало?",
                    evidence=[Evidence(str(path.relative_to(repo)) if path.is_relative_to(repo) else str(path),
                                       line_of(text, match.start()), line)],
                    options=["указать версию/дату явно", "переписать без привязки ко времени"],
                ).finalize())
    return findings


DEFINITION = re.compile(r"^\s*(?:[-*]\s*)?(?:\*\*(?P<bold>[^*]{2,60})\*\*|`(?P<code>[^`]{2,60})`)"
                        r"\s*(?:[—–-]|:)\s+(?P<rest>\S.*)$")
PHRASE = re.compile(r"`([^`\n]{4,60})`")
BOLD_PHRASE = re.compile(r"\*\*([^*\n]{4,60})\*\*")


def check_terminology(docs: list[Path], repo: Path) -> list[Finding]:
    """Один термин с разными определениями; одно понятие под разными именами."""
    definitions: list[tuple[str, str, Path, int, str]] = []
    phrases: dict[str, list[tuple[Path, int, str]]] = {}

    for path in docs:
        text = read(path)
        for number, raw in enumerate(text.splitlines(), start=1):
            match = DEFINITION.match(raw)
            if match:
                subject = (match.group("bold") or match.group("code") or "").strip().lower()
                rest = match.group("rest").strip()
                if len(subject) >= 4 and len(rest) >= 20:
                    definitions.append((subject, rest, path, number, raw.strip()))
            if raw.lstrip().startswith("|"):
                continue  # ячейки таблиц — это значения, а не определения терминов
            for pattern in (PHRASE, BOLD_PHRASE):
                for phrase in pattern.finditer(raw):
                    value = phrase.group(1).strip()
                    words = value.split()
                    if not 2 <= len(words) <= 4 or not re.search(r"[а-яa-z]", value, re.I):
                        continue
                    if any(ch in value for ch in ",.;:«»()/"):
                        continue
                    # Команды и идентификаторы (hermes skills sync, npm ci) — не термины.
                    if words[0].isascii() and all(w.isascii() for w in words):
                        continue
                    phrases.setdefault(value.lower(), []).append((path, number, raw.strip()))

    findings: list[Finding] = []

    seen_subjects: set[str] = set()
    for subject, rest_a, path_a, line_a, raw_a in definitions:
        if subject in seen_subjects:
            continue
        same = [d for d in definitions if d[0] == subject and d[4] != raw_a]
        if not same:
            continue
        distinct = [(d[1], d[2], d[3], d[4]) for d in same
                    if jaccard(tokens(rest_a), tokens(d[1])) < 0.65]
        if not distinct:
            continue
        seen_subjects.add(subject)
        rest_b, path_b, line_b, raw_b = distinct[0]
        rel = lambda p: str(p.relative_to(repo)) if p.is_relative_to(repo) else str(p)
        findings.append(Finding(
            check="terminology", cls="terminology", severity="medium",
            title=f"Термин «{subject[0].upper() + subject[1:]}» определён дважды по-разному",
            why="Два определения одного термина читаются как два разных правила; агент будет "
                "выбирать то, что попадёт в контекст.",
            question=f"«{subject}» — это одно понятие? Тогда какое определение верное, а какое "
                     "устарело?",
            evidence=[Evidence(rel(path_a), line_a, raw_a[:200]), Evidence(rel(path_b), line_b, raw_b[:200])],
            options=["оставить одно определение", "развести: это разные понятия"],
        ).finalize())

    names = list(phrases)
    for i, first in enumerate(names):
        for second in names[i + 1:]:
            if first == second or " " not in first or " " not in second:
                continue
            # Термин, а не файл/флаг: имена файлов, ключи и пути сюда не годятся.
            if any(ch in first + second for ch in ("/", ".", "--", "`", "(", ")")):
                continue
            # Сравнение по грубым основам: «метка» и «маркер» — одна предметная область.
            words_a = {w[:4] for w in tokens(first)}
            words_b = {w[:4] for w in tokens(second)}
            similarity = jaccard(words_a, words_b)
            if similarity < 0.3 or words_a == words_b:
                continue
            differing = words_a ^ words_b
            if len(differing) != 2:
                continue
            path_a, line_a, raw_a = phrases[first][0]
            path_b, line_b, raw_b = phrases[second][0]
            rel = lambda p: str(p.relative_to(repo)) if p.is_relative_to(repo) else str(p)
            findings.append(Finding(
                check="terminology", cls="terminology", severity="low",
                title=f"Похожие термины: «{first}» и «{second}»",
                why="Две формулировки различаются одним словом: либо это одно понятие с "
                    "разъехавшимся названием, либо два разных, и тогда это надо сказать явно.",
                question=f"«{first}» и «{second}» — одно и то же или разные вещи?",
                evidence=[Evidence(rel(path_a), line_a, raw_a[:200]), Evidence(rel(path_b), line_b, raw_b[:200])],
                options=["свести к одному названию", "оставить оба и различить определениями"],
            ).finalize())
            if sum(1 for f in findings if f.check == "terminology" and "Похожие термины" in f.title) >= 5:
                return findings
    return findings


# --- память -------------------------------------------------------------------

def memory_entries(path: Path) -> list[tuple[int, str]]:
    """Записи памяти Hermes разделяются строкой «§»."""
    entries: list[tuple[int, str]] = []
    buffer: list[str] = []
    start = 1
    for number, raw in enumerate(read(path).splitlines(), start=1):
        if raw.strip() == "§":
            if any(chunk.strip() for chunk in buffer):
                entries.append((start, " ".join(chunk.strip() for chunk in buffer).strip()))
            buffer, start = [], number + 1
        else:
            buffer.append(raw)
    if any(chunk.strip() for chunk in buffer):
        entries.append((start, " ".join(chunk.strip() for chunk in buffer).strip()))
    return entries


def check_memory(profile: Path, repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    limits = config_memory_limits(profile)
    for name in ("MEMORY.md", "USER.md"):
        path = profile / "memories" / name
        if not path.is_file():
            continue
        text = read(path)
        entries = memory_entries(path)
        limit = limits.get(name)
        if limit and len(text) > limit * 0.9:
            spare = limit - len(text)
            question = ((f"В {name} свободно {spare} символов." if spare >= 0
                         else f"В {name} превышен предел на {-spare} символов.")
                        + " Какие записи устарели и могут быть сняты?")
            findings.append(Finding(
                check="memory", cls="underdetermined", severity="medium",
                title=f"{name}: занято {len(text)} из {limit} символов",
                why="Почти предел: следующая запись не поместится, и агент начнёт вытеснять "
                    "существующие факты, а не уточнять их.",
                question=question,
                evidence=[Evidence(f"memories/{name}", 1, f"{len(text)}/{limit} символов")],
                options=["снять устаревшие записи", "объединить близкие записи"],
            ).finalize())

        for i, (line_a, entry_a) in enumerate(entries):
            for line_b, entry_b in entries[i + 1:]:
                if entry_a == entry_b:
                    findings.append(Finding(
                        check="memory", cls="terminology", severity="low",
                        title=f"{name}: одна и та же запись дважды",
                        why="Дубль занимает бюджет и создаёт риск правки только одной копии.",
                        question="Это одна запись, попавшая дважды, или два разных факта?",
                        evidence=[Evidence(f"memories/{name}", line_a, entry_a[:140]),
                                  Evidence(f"memories/{name}", line_b, entry_b[:140])],
                        options=["удалить дубль", "объединить"],
                    ).finalize())
                    continue
                words_a, words_b = tokens(entry_a), tokens(entry_b)
                similarity = max(jaccard(words_a, words_b),
                                 len(words_a & words_b) / min(len(words_a), len(words_b))
                                 if words_a and words_b else 0.0)
                if similarity < 0.55:
                    continue
                digits_a = set(re.findall(r"\d+", entry_a))
                digits_b = set(re.findall(r"\d+", entry_b))
                if not digits_a or not digits_b or digits_a == digits_b:
                    continue
                findings.append(Finding(
                    check="memory", cls="factual", severity="high",
                    title=f"{name}: похожие записи с разными числами",
                    why="Две записи об одном и том же с разными числами: одновременно верными они "
                        "быть не могут, а агент будет выбирать ту, что попадёт в контекст.",
                    question="Какое число актуально и что случилось с прежним — изменение условия "
                             "или ошибка в одной из записей?",
                    evidence=[Evidence(f"memories/{name}", line_a, entry_a[:140]),
                              Evidence(f"memories/{name}", line_b, entry_b[:140])],
                    options=["оставить актуальное, прежнее снять", "развести по условиям"],
                ).finalize())

        seen_entries: set[str] = set()
        for line, entry in entries:
            if entry in seen_entries:
                continue
            seen_entries.add(entry)
            for match in MACHINE_PATH.finditer(entry):
                findings.append(Finding(
                    check="memory", cls="stale", severity="low",
                    title=f"{name}: машинно-локальный путь в памяти",
                    why="Память переживает перенос профиля, а путь — нет: после переустановки "
                        "запись станет ложной.",
                    question="Путь в записи нужен как факт или как пример?",
                    evidence=[Evidence(f"memories/{name}", line, entry[:140])],
                    options=["заменить на относительный", "оставить"],
                ).finalize())
    return findings


# --- сборка отчёта ------------------------------------------------------------

def load_ledger(path: Path) -> dict[str, dict]:
    resolved: dict[str, dict] = {}
    if not path.is_file():
        return resolved
    for raw in read(path).splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if record.get("fingerprint"):
            resolved[record["fingerprint"]] = record
    return resolved


def filter_suppressed(findings: list[Finding], profile: Path, repo: Path) -> list[Finding]:
    """Осознанные исключения: строка с `audit:ignore` и шаблоны из .audit/ignore.txt.

    Исключение — тоже утверждение: его видно в файле, оно ревьюится в PR и объясняется
    комментарием. Молчаливых «выключателей» в коде проверок нет.
    """
    patterns: list[re.Pattern[str]] = []
    for ignore_file in (repo / ".audit/ignore.txt", profile / ".audit/ignore.txt"):
        for raw in read(ignore_file).splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                try:
                    patterns.append(re.compile(line, re.I))
                except re.error:
                    continue
    kept: list[Finding] = []
    for finding in findings:
        haystack = " ".join([finding.check, finding.title, *[e.path for e in finding.evidence]])
        if any(pattern.search(haystack) for pattern in patterns):
            continue
        if any("audit:ignore" in e.quote for e in finding.evidence):
            continue
        kept.append(finding)
    return kept


def authored_skill_dirs(repo: Path, profile: Path) -> list[Path]:
    """Авторские скиллы профиля (не bundled): политика публикации знает разницу, если доступна."""
    tools = repo / "tools"
    if (tools / "publish_policy.py").is_file():
        sys.path.insert(0, str(tools))
        try:
            from publish_policy import profile_authored  # type: ignore

            authored, _bundled = profile_authored(profile)
            return [profile / "skills" / rel for rel in sorted(authored)]
        except Exception:
            pass
    # Без политики: всё, что лежит в профиле и отсутствует в репозитории, — точно своё, локальное.
    repo_skills = repo / "skills"
    local: list[Path] = []
    for skill in sorted((profile / "skills").glob("*/*/SKILL.md")):
        if not (repo_skills / skill.parent.relative_to(profile / "skills")).is_dir():
            local.append(skill.parent)
    return local


def render_markdown(findings: list[Finding], checks: dict[str, int], profile: Path,
                    repo: Path, mode: str, topic: str | None) -> str:
    by_class: dict[str, list[Finding]] = {}
    for finding in findings:
        by_class.setdefault(finding.cls, []).append(finding)

    lines = [
        "# Аудит материалов профиля",
        "",
        f"- профиль: `{profile}`",
        f"- материалы: `{repo}`",
        f"- режим: {mode}" + (f", тема: `{topic}`" if topic else ""),
        f"- находок: {len(findings)}",
        "",
        "| Класс | Находок |",
        "|---|---|",
    ]
    for code, label in CLASSES.items():
        if code in by_class:
            lines.append(f"| {label} (`{code}`) | {len(by_class[code])} |")

    lines += ["", "## Находки", ""]
    for number, finding in enumerate(sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity],
                                                                    f.cls)), start=1):
        lines.append(f"### {number}. [{finding.cls}/{finding.severity}] {finding.title}")
        lines.append("")
        for ev in finding.evidence:
            lines.append(f"- `{ev.path}:{ev.line}` — {ev.quote}")
        lines += ["", f"**Почему проблема.** {finding.why}", "",
                  f"**Вопрос.** {finding.question}", ""]
        if finding.options:
            lines.append("**Варианты.** " + " · ".join(finding.options))
            lines.append("")
        if finding.previously_resolved:
            previous = finding.previously_resolved
            lines += [f"**Уже разбирали** ({previous.get('ts', '?')}): "
                      f"{previous.get('answer', '—')}", ""]
        lines.append(f"<sub>проверка `{finding.check}`, отпечаток `{finding.fingerprint}`</sub>")
        lines.append("")

    lines += ["## Проверки без находок", ""]
    clean = [name for name, count in checks.items() if count == 0]
    lines.append(", ".join(f"`{name}`" for name in clean) + "." if clean else "—")
    lines += ["", "## Ограничения", "",
              "- Сканер не решает, а указывает: класс — гипотеза, подтверждает её человек.",
              "- Числовые проверки сравнивают только то, что можно посчитать в репозитории; "
              "смысловые противоречия без чисел находятся чтением находок и разбором в диалоге.",
              "- Штатные (bundled) скиллы Hermes не аудируются: они не наши и перезаписываются "
              "при обновлении.", ""]
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Сканер материалов профиля для аудита противоречий")
    parser.add_argument("--repo", metavar="КАТАЛОГ", help="клон дистрибутива (по умолчанию — родитель скрипта)")
    parser.add_argument("--profile", metavar="КАТАЛОГ", help="каталог профиля (по умолчанию активный)")
    parser.add_argument("--extra", metavar="ПУТЬ", action="append", default=[],
                        help="дополнительные файлы/каталоги (например, локальные скиллы профиля)")
    parser.add_argument("--memory", action="store_true", help="включить MEMORY.md и USER.md")
    parser.add_argument("--topic", metavar="REGEX", help="только находки по теме")
    parser.add_argument("--mode", choices=["full", "topic", "incremental"], default="full")
    parser.add_argument("--ledger", metavar="ФАЙЛ", help="журнал прошлых сессий")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--out", metavar="ФАЙЛ", help="записать отчёт в файл")
    args = parser.parse_args(argv)

    if args.repo:
        repo = Path(args.repo).expanduser().resolve()
    else:
        found = default_repo()
        if found is None:
            sys.stderr.write("не найден клон дистрибутива: укажите --repo <каталог>\n")
            return 2
        repo = found
    if not (repo / "skills").is_dir():
        sys.stderr.write(f"не найден репозиторий материалов: {repo}\n")
        return 2
    profile = Path(args.profile).expanduser().resolve() if args.profile else default_profile()
    if not (profile / "skills").is_dir():
        sys.stderr.write(f"не найден профиль: {profile}\n")
        return 2
    ledger_path = Path(args.ledger).expanduser() if args.ledger else profile / ".audit" / "ledger.jsonl"
    ledger = load_ledger(ledger_path)

    extra: list[Path] = []
    for rel in ("README.md", "SOUL.md"):
        candidate = profile / rel
        twin = repo / rel
        if not candidate.is_file():
            continue
        # Копия, совпадающая с репозиторием, не даёт новых находок — только удваивает отчёт.
        if twin.is_file() and read(candidate) == read(twin):
            continue
        extra.append(candidate)
    for item in args.extra:
        extra.append(Path(item).expanduser().resolve())
    # Профильные скиллы добавляются только те, которых нет в репозитории: иначе один и тот же
    # текст попадёт в проверки дважды и даст двойные находки на пустом месте.
    repo_skills = repo / "skills"
    for skill_dir in authored_skill_dirs(repo, profile):
        rel = skill_dir.relative_to(profile / "skills")
        if not (repo_skills / rel).is_dir():
            extra.append(skill_dir)

    docs = collect_docs(repo, extra)
    skill_count = len(list((repo / "skills").glob("*/**/SKILL.md")))
    tool_count = len([p for p in (repo / "tools").glob("*") if p.is_file()])

    checks: dict[str, int] = {}
    findings: list[Finding] = []

    def run(name: str, produced: list[Finding]) -> None:
        checks[name] = len(produced)
        findings.extend(produced)

    run("paths", check_paths(docs, repo, profile))
    run("counts", check_counts(docs, repo, skill_count, tool_count))
    run("step_refs", check_step_refs(docs, repo))
    run("duplicates", check_duplicates(docs, repo))
    run("hedges", check_hedges(docs, repo))
    run("versions", check_versions(docs, repo))
    run("temporal", check_temporal(docs, repo))
    run("terminology", check_terminology(docs, repo))
    if args.memory:
        run("memory", check_memory(profile, repo))
    else:
        checks.setdefault("memory", 0)

    findings = filter_suppressed(findings, profile, repo)

    if args.topic:
        pattern = re.compile(args.topic, re.I)
        findings = [f for f in findings
                    if pattern.search(f.title) or pattern.search(" ".join(e.quote for e in f.evidence))
                    or any(pattern.search(e.path) for e in f.evidence)]

    if args.mode == "incremental" and ledger:
        pending: list[Finding] = []
        for finding in findings:
            record = ledger.get(finding.fingerprint)
            if record and record.get("status") == "resolved":
                continue
            if record:
                finding.previously_resolved = record
            pending.append(finding)
        findings = pending

    # Дедупликация одинаковых находок из разных проверок.
    unique: dict[str, Finding] = {}
    for finding in findings:
        unique.setdefault(finding.fingerprint, finding)
    findings = list(unique.values())

    if args.json:
        payload = {
            "profile": str(profile),
            "repo": str(repo),
            "mode": args.mode,
            "topic": args.topic,
            "ledger": str(ledger_path),
            "checks": checks,
            "findings": [asdict(f) for f in findings],
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        }
        output = json.dumps(payload, ensure_ascii=False, indent=1)
    else:
        output = render_markdown(findings, checks, profile, repo, args.mode, args.topic)

    if args.out:
        Path(args.out).expanduser().write_text(output + "\n", encoding="utf-8")
        sys.stderr.write(f"отчёт: {Path(args.out).expanduser()}\n")
    else:
        print(output)
    return 1 if findings else 0


def default_repo() -> Path | None:
    """Клон дистрибутива: текущий каталог, если он похож на клон, иначе привычный путь."""
    for candidate in (Path.cwd(), Path.home() / "work/hermes-certification-assistant"):
        if (candidate / "skills").is_dir() and (candidate / "publish.list").is_file():
            return candidate.resolve()
    return None


def default_profile() -> Path:
    active = Path.home() / ".hermes/active_profile"
    name = read(active).strip() if active.is_file() else "hermes-certification-assistant"
    return Path.home() / ".hermes/profiles" / name


if __name__ == "__main__":
    sys.exit(main())
