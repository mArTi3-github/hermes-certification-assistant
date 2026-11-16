#!/usr/bin/env python3
"""Строки профиля, которых нет ни в одной версии файла в истории main.

Отвечает на вопрос «профиль просто отстал или в нём правка?» без метки синхронизации.

    python3 scripts/unexplained_lines.py <клон> <каталог-скилла-профиля> <путь-скилла-в-репозитории>

Пример:
    python3 <скилл>/scripts/unexplained_lines.py ~/work/hermes-certification-assistant \\
        "$HERMES_HOME/skills/certification/stand-ftest" certification/stand-ftest

Коды возврата: 0 — строк, которых не было ни в одной версии, нет (скилл отстал, публиковать
нечего); 1 — такие строки есть (это правка); 2 — аргументы или репозиторий не в порядке.
"""

import pathlib
import subprocess
import sys

MAX_SHOWN = 12


def git(repo: pathlib.Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def history_lines(repo: pathlib.Path, rel: str) -> set[str]:
    """Все строки файла во всех коммитах, где он существовал (любая ветка)."""
    revs = git(repo, "log", "--all", "--format=%H", "--", rel).stdout.split()
    seen: set[str] = set()
    for rev in revs:
        out = git(repo, "show", f"{rev}:{rel}")
        if out.returncode == 0:
            seen |= {line.rstrip() for line in out.stdout.splitlines()}
    return seen


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__.strip())
        return 2
    repo = pathlib.Path(sys.argv[1]).expanduser()
    prof = pathlib.Path(sys.argv[2]).expanduser()
    rel = sys.argv[3].strip("/")

    if not (repo / ".git").exists():
        print(f"✖ не git-репозиторий: {repo}")
        return 2
    if not prof.is_dir():
        print(f"✖ нет каталога скилла в профиле: {prof}")
        return 2

    total = 0
    only_in_profile = []
    for path in sorted(prof.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        sub = path.relative_to(prof).as_posix()
        rel_file = f"{rel}/{sub}"
        if not (repo / rel_file).exists():
            only_in_profile.append(sub)
            continue

        known = history_lines(repo, rel_file)
        for line in (repo / rel_file).read_text(encoding="utf-8").splitlines():
            known.add(line.rstrip())

        profile_lines = [l.rstrip() for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        unexplained = [l for l in profile_lines if l not in known]
        total += len(unexplained)
        print(f"  {sub}: строк {len(profile_lines)}, необъяснённых {len(unexplained)}")
        for line in unexplained[:MAX_SHOWN]:
            print(f"     · {line[:110]}")
        if len(unexplained) > MAX_SHOWN:
            print(f"     … ещё {len(unexplained) - MAX_SHOWN}")

    for sub in only_in_profile:
        print(f"  ТОЛЬКО В ПРОФИЛЕ (файл): {sub}")

    if total or only_in_profile:
        print(f"→ это правка: необъяснённых строк {total}, файлов только-в-профиле {len(only_in_profile)}")
        return 1
    print("→ скилл отстал: строк, которых не было ни в одной версии, нет — публиковать нечего")
    return 0


if __name__ == "__main__":
    sys.exit(main())
