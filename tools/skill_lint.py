#!/usr/bin/env python3
"""Линт дистрибутива профиля Hermes: скиллы, config.yaml, секреты в истории git.

Запускается локально (`python3 tools/skill_lint.py`) и в CI на каждый PR.
Код возврата 1 — есть ошибки, публиковать нельзя.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_policy  # noqa: E402

DESC_WARN = 60          # рекомендация skill_manage: описание короче 60 символов
DESC_MAX = 400
SIZE_WARN = 200_000
SIZE_MAX = 500_000

FORBIDDEN_TRACKED = {
    "auth.json", ".env", ".env.bak", "state.db", "state.db-wal", "state.db-shm",
    "processes.json", "gateway.pid", "gateway.lock", "settings.local.json",
}

SECRET_PATTERNS = [
    ("приватный ключ", re.compile(r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----")),
    # Реальные длины: classic PAT — ghp_ + 36, fine-grained — github_pat_ + ~82,
    # GitLab — glpat- + 20, DeepSeek/OpenAI — sk- + 32 и больше.
    ("GitHub PAT (classic)", re.compile(r"\bghp_[A-Za-z0-9]{30,}")),
    ("GitHub PAT (fine-grained)", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{30,}")),
    ("GitLab PAT", re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("DeepSeek/OpenAI ключ", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
]


def _is_placeholder(token: str) -> bool:
    """Документационные заглушки не считаем секретом: xxxx…, AKIA…EXAMPLE, <token>."""
    lowered = token.lower()
    if "example" in lowered or "xxxx" in lowered or "your" in lowered:
        return True
    body = re.sub(r"^[a-z_]+", "", lowered)
    return bool(body) and len(set(body)) == 1


def secret_hits(text: str) -> list[str]:
    """Метки найденных в тексте секретов (плейсхолдеры отбрасываются)."""
    found: list[str] = []
    for label, pattern in SECRET_PATTERNS:
        for match in pattern.finditer(text):
            if _is_placeholder(match.group(0)):
                continue
            found.append(label)
            break
    return found

SECRET_KEYS = {
    "api_key", "apikey", "token", "password", "passwd", "secret", "client_secret",
    "access_token", "refresh_token", "basic_auth", "private_key",
}
EMPTY_VALUES = {"", "null", "~", "''", '""', "[]", "{}"}

# Правило профиля: адреса стендов в скиллах не фиксируются (меняются от прогона к прогону).
PRIVATE_IP = re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b")


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, where: str, text: str) -> None:
        self.errors.append(f"{where}: {text}")

    def warn(self, where: str, text: str) -> None:
        self.warnings.append(f"{where}: {text}")


def parse_frontmatter(text: str) -> dict[str, str]:
    """Читает плоские ключи YAML-фронтматтера без зависимости от PyYAML."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for raw in text[3:end].splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#") or line[0] in " \t":
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def check_skills(report: Report) -> int:
    skill_files = sorted(SKILLS.glob("*/*/SKILL.md"))
    if not skill_files:
        report.error("skills/", "не найдено ни одного скилла по маске skills/<категория>/<имя>/SKILL.md")
        return 0

    for path in skill_files:
        rel = path.relative_to(ROOT).as_posix()
        dir_name = path.parent.name
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            report.error(rel, f"не читается: {exc}")
            continue

        size = path.stat().st_size
        if size > SIZE_MAX:
            report.error(rel, f"слишком большой файл: {size} байт (максимум {SIZE_MAX})")
        elif size > SIZE_WARN:
            report.warn(rel, f"большой SKILL.md: {size} байт — вынесите детали в references/")

        meta = parse_frontmatter(text)
        name = meta.get("name", "")
        description = meta.get("description", "")

        if not name:
            report.error(rel, "во фронтматтере нет поля name")
        elif name != dir_name:
            report.error(rel, f"name '{name}' не совпадает с именем каталога '{dir_name}'")
        elif not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name):
            report.warn(rel, f"имя '{name}' не в нижнем регистре/дефисах — ломает поиск скиллов")

        if not description:
            report.error(rel, "во фронтматтере нет поля description")
        elif len(description) > DESC_MAX:
            report.error(rel, f"description длиннее {DESC_MAX} символов ({len(description)})")
        elif len(description) > DESC_WARN:
            report.warn(rel, f"description {len(description)} символов — лучше ≤ {DESC_WARN}")

        for label in secret_hits(text):
            report.error(rel, f"похоже на секрет: {label}")

        found = PRIVATE_IP.search(text)
        if found:
            report.error(
                rel,
                f"внутренний адрес {found.group(0)}: адреса стендов в скиллах не фиксируются, "
                "используйте плейсхолдер <узел>",
            )

    return len(skill_files)


def check_tracked(report: Report) -> None:
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        report.warn("git", "не удалось получить список файлов — проверка отслеживаемых файлов пропущена")
        return

    for line in out.splitlines():
        name = Path(line).name
        if name in FORBIDDEN_TRACKED:
            report.error(line, "рантайм-файл/секрет не должен быть в репозитории")


def check_config(report: Report) -> None:
    config = ROOT / "config.yaml"
    if not config.exists():
        report.warn("config.yaml", "файл отсутствует")
        return

    text = config.read_text(encoding="utf-8", errors="replace")
    for label in secret_hits(text):
        report.error("config.yaml", f"похоже на секрет: {label}")

    for number, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition(":")
        if not sep:
            continue
        if key.strip().lower() not in SECRET_KEYS:
            continue
        value = value.split("#", 1)[0].strip()
        if value.lower() in EMPTY_VALUES:
            continue
        report.error(f"config.yaml:{number}", f"поле '{key.strip()}' содержит значение — секреты живут в .env")


def check_history(report: Report) -> None:
    try:
        log = subprocess.run(
            ["git", "-C", str(ROOT), "log", "-p", "--all", "--no-color", "--format=%H %s"],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, FileNotFoundError):
        report.warn("git", "история недоступна — проверка секретов в коммитах пропущена")
        return

    commit = "?"
    scanned = 0
    for line in log.splitlines():
        if re.fullmatch(r"[0-9a-f]{40} .*", line) or re.fullmatch(r"[0-9a-f]{64} .*", line):
            commit = line[:12]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        scanned += 1
        for label in secret_hits(line):
            report.error(f"коммит {commit}", f"секрет в истории: {label} — {line[1:120].strip()}")


def check_policy(report: Report) -> None:
    """Сверяет содержимое репозитория с publish.list и правилами категорий."""
    in_repo = {path.parent.relative_to(SKILLS).as_posix() for path in SKILLS.glob("*/*/SKILL.md")}
    state = publish_policy.classify(in_repo)

    for item in sorted(in_repo - state["published"]):
        report.error(
            f"skills/{item}",
            "скилл лежит в репозитории, но политика публикации его не включает (publish.list)",
        )

    for item in sorted(publish_policy.explicit_publish() & publish_policy.explicit_private()):
        report.error("publish.list", f"{item}: одновременно в [publish] и [private]")

    absent = publish_policy.explicit_publish() - in_repo
    for item in sorted(absent):
        report.warn("publish.list", f"{item}: указан в [publish], но в репозитории отсутствует")

    for item in sorted(in_repo):
        category, name = item.split("/", 1)
        if category == "certification" and not name.startswith("certification-"):
            report.error(
                f"skills/{item}",
                "в категории certification имя скилла должно начинаться с 'certification-'",
            )
        if name.startswith("certification-") and category != "certification":
            report.error(
                f"skills/{item}",
                "имя с префиксом 'certification-' допустимо только в категории certification",
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="Линт дистрибутива профиля Hermes")
    parser.add_argument("--history", action="store_true", help="дополнительно просканировать историю git")
    args = parser.parse_args()

    report = Report()
    count = check_skills(report)
    check_tracked(report)
    check_config(report)
    check_policy(report)
    if args.history:
        check_history(report)

    for warning in report.warnings:
        print(f"warning: {warning}")
    for error in report.errors:
        print(f"ERROR:   {error}")

    print(f"\nскиллов проверено: {count}; ошибок: {len(report.errors)}; предупреждений: {len(report.warnings)}")
    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
