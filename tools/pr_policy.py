#!/usr/bin/env python3
"""Политика PR: версия скилла выросла, строка журнала про скилл есть.

    python3 tools/pr_policy.py --base origin/main          # как в CI
    python3 tools/pr_policy.py --base HEAD~1 --head HEAD   # локально

Проверки для каждого скилла, у которого в PR изменились файлы:

* `version:` во фронтматтере SKILL.md обязан вырасти (semver: major.minor.patch);
* в `CHANGELOG.md` должна появиться добавленная строка, где скилл назван по имени
  (имя каталога) — иначе правка уезжает без записи в журнале и с тем же номером версии.

Версию дистрибутива в `distribution.yaml` по-прежнему поднимает только владелец при релизе —
эту проверку делает отдельный шаг workflow.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORED = {"__pycache__", ".DS_Store", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def git(*args: str) -> tuple[int, str]:
    res = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return res.returncode, res.stdout


def frontmatter_version(text: str) -> str | None:
    found = re.search(r"^version:\s*(\S+)\s*$", text, re.M)
    return found.group(1) if found else None


def parse_version(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value.strip())
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def version_at(ref: str, rel: str) -> str | None:
    code, out = git("show", f"{ref}:skills/{rel}/SKILL.md")
    return frontmatter_version(out) if code == 0 else None


def changed_skills(base: str, head: str) -> dict[str, set[str]]:
    code, out = git("diff", "--name-only", f"{base}...{head}", "--", "skills/")
    if code != 0:
        raise SystemExit(f"не удалось сравнить {base}...{head}")
    skills: dict[str, set[str]] = {}
    for path in out.splitlines():
        parts = path.split("/")
        if len(parts) < 4:
            continue
        if any(part in IGNORED for part in parts) or Path(path).suffix in IGNORED_SUFFIXES:
            continue
        rel = "/".join(parts[1:3])
        skills.setdefault(rel, set()).add(path)
    return skills


def changelog_added_lines(base: str, head: str) -> str:
    code, out = git("diff", f"{base}...{head}", "--", "CHANGELOG.md")
    if code != 0:
        return ""
    lines = [line[1:] for line in out.splitlines()
             if line.startswith("+") and not line.startswith("+++")]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Политика PR: версии скиллов и журнал")
    parser.add_argument("--base", default="origin/main", help="база сравнения")
    parser.add_argument("--head", default="HEAD", help="голова (по умолчанию HEAD)")
    args = parser.parse_args(argv)

    skills = changed_skills(args.base, args.head)
    if not skills:
        print("▪ файлов в skills/ PR не меняет — проверять нечего")
        return 0

    journal = changelog_added_lines(args.base, args.head)
    problems: list[str] = []

    for rel, files in sorted(skills.items()):
        name = rel.split("/")[-1]
        head_file = ROOT / "skills" / rel / "SKILL.md"
        if not head_file.is_file():
            if f"/{name}" not in journal and name not in journal:
                problems.append(f"skills/{rel}: каталог удалён, а в CHANGELOG про «{name}» ни слова")
            continue

        current = frontmatter_version(head_file.read_text(encoding="utf-8"))
        base_version = version_at(args.base, rel)
        if current is None:
            problems.append(f"skills/{rel}: во фронтматтере нет version")
            continue
        if base_version is not None:
            old, new = parse_version(base_version), parse_version(current)
            if old is None or new is None:
                problems.append(f"skills/{rel}: версия «{base_version}» → «{current}» не semver")
            elif new <= old:
                problems.append(
                    f"skills/{rel}: версия не выросла ({base_version} → {current}); "
                    "изменение скилла без bump версии")
        if name not in journal and rel not in journal:
            problems.append(
                f"skills/{rel}: в CHANGELOG нет добавленной строки про «{name}» "
                f"({len(files)} файлов изменено)")

    if problems:
        for text in problems:
            print(f"::error title=Политика PR::{text}")
        print(f"\n✖ политика PR: {len(problems)} замечаний")
        return 1
    print(f"✔ политика PR: скиллов {len(skills)}, версии подняты, журнал заполнен")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
