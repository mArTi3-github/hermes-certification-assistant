#!/usr/bin/env python3
"""Сверка профиля с клоном: что ещё не опубликовано и что разошлось после публикации.

Вопросы, на которые отвечает одна команда:

    python3 tools/publish_check.py                 # активный профиль
    python3 tools/publish_check.py --profile DIR   # конкретный профиль
    python3 tools/publish_check.py --json          # машинный вывод

Что печатает:

  1. состояние клона: ветка, чистота дерева, расхождение с `origin/main`;
  2. по каждому публикуемому скиллу — совпадает ли профиль с репозиторием, а если нет,
     какие именно файлы отличаются, есть только в профиле или только в репозитории;
  3. скиллы репозитория, которых нет в профиле (профиль отстал — нужен `sync.sh`);
  4. авторские скиллы профиля вне политики публикации (их не публикуют, но и молча
     потерять нельзя — раньше это всплывало только при запуске `contribute.sh`);
  5. версии дистрибутива: в репозитории и в профиле.

Зачем нужен отдельный шаг: `contribute.sh` копирует профиль в клон один раз и коммитит.
Между копированием и мержем профиль может измениться — правку дописывает агент, куратор
скиллов Hermes, другой сеанс на том же хосте. Такой слой не попадает в уже созданный PR,
и публикация тихо отстаёт от профиля. Проверка перед мержем и после него ловит это одной
командой вместо `diff -rq` по каждому скиллу вручную.

Коды возврата: 0 — всё синхронно; 1 — есть расхождения; 2 — не найдено окружение
(нет профиля, каталог не git-репозиторий).
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from publish_policy import classify, profile_authored, scan  # noqa: E402

IGNORED_NAMES = {"__pycache__", ".DS_Store", ".pytest_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def git(*args: str) -> tuple[int, str]:
    res = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return res.returncode, (res.stdout + res.stderr).strip()


def tree_files(root: Path) -> dict[str, str]:
    """Относительный путь → отпечаток файла, без мусора, который не публикуется."""
    import hashlib

    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name in IGNORED_NAMES or path.suffix in IGNORED_SUFFIXES:
            continue
        if any(part in IGNORED_NAMES for part in path.relative_to(root).parts[:-1]):
            continue
        result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def compare(repo_skill: Path, profile_skill: Path) -> dict[str, list[str]]:
    repo_files, profile_files = tree_files(repo_skill), tree_files(profile_skill)
    return {
        "changed": sorted(name for name in repo_files.keys() & profile_files.keys()
                          if repo_files[name] != profile_files[name]),
        "only_profile": sorted(profile_files.keys() - repo_files.keys()),
        "only_repo": sorted(repo_files.keys() - profile_files.keys()),
    }


def distribution_version(path: Path) -> str:
    if not path.exists():
        return "?"
    match = re.search(r"^version:\s*(\S+)", path.read_text(encoding="utf-8", errors="replace"), re.M)
    return match.group(1) if match else "?"


def active_profile() -> Path:
    active = Path.home() / ".hermes/active_profile"
    name = active.read_text(encoding="utf-8").strip() if active.exists() else "hermes-certification-assistant"
    return Path.home() / ".hermes/profiles" / name


def main() -> int:
    parser = argparse.ArgumentParser(description="Сверка профиля с клоном репозитория")
    parser.add_argument("--profile", metavar="КАТАЛОГ", help="каталог профиля")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--quiet", action="store_true", help="только итог и расхождения")
    args = parser.parse_args()

    profile = Path(args.profile).expanduser() if args.profile else active_profile()
    if not (profile / "skills").is_dir():
        sys.stderr.write(f"не найден профиль: {profile} (нет каталога skills)\n")
        return 2
    code, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if code:
        sys.stderr.write(f"клон не git-репозиторий: {ROOT}\n")
        return 2

    repo_skills = sorted(scan(ROOT / "skills"))
    policy = classify(set(repo_skills))
    published = sorted(policy["published"])
    authored, _bundled = profile_authored(profile)
    unclassified = sorted(classify(authored)["unclassified"])

    _, porcelain = git("status", "--porcelain")
    _, counts = git("rev-list", "--left-right", "--count", "HEAD...origin/main")
    ahead, behind = (counts.split() + ["?", "?"])[:2] if counts else ("?", "?")

    diverged: dict[str, dict[str, list[str]]] = {}
    missing_in_profile: list[str] = []
    for rel in published:
        repo_skill, profile_skill = ROOT / "skills" / rel, profile / "skills" / rel
        if not profile_skill.is_dir():
            missing_in_profile.append(rel)
            continue
        delta = compare(repo_skill, profile_skill)
        if any(delta.values()):
            diverged[rel] = delta

    report = {
        "repo": str(ROOT),
        "branch": branch,
        "dirty": [line for line in porcelain.splitlines() if line.strip()],
        "ahead": ahead,
        "behind": behind,
        "profile": str(profile),
        "published": published,
        "diverged": diverged,
        "missing_in_profile": missing_in_profile,
        "unclassified": unclassified,
        "version_repo": distribution_version(ROOT / "distribution.yaml"),
        "version_profile": distribution_version(profile / "distribution.yaml"),
        "in_sync": not diverged and not missing_in_profile and not report_dirty(porcelain),
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"▪ клон:    {ROOT} ({branch} @ {short_head()}, "
              f"{'чисто' if not porcelain else 'есть незакоммиченное'}, "
              f"{ahead}↑/{behind}↓ к origin/main)")
        print(f"▪ профиль: {profile}")
        print(f"▪ публикуемых скиллов: {len(published)}")
        if not args.quiet:
            for rel in published:
                print(f"  {'≠' if rel in diverged or rel in missing_in_profile else '='} {rel}")
        for rel in missing_in_profile:
            print(f"\n  ≠ {rel}: скилла нет в профиле — профиль отстал, нужен tools/sync.sh")
        for rel, delta in diverged.items():
            print(f"\n  ≠ {rel}")
            for key, label in (("changed", "изменён"), ("only_profile", "только в профиле"),
                               ("only_repo", "только в репозитории")):
                for name in delta[key]:
                    print(f"      {label}: {name}")
        if unclassified:
            print("\n▪ авторские скиллы вне политики (не публикуются, решать через --classify):")
            for rel in unclassified:
                print(f"    {rel}")
        print(f"\n▪ версия: репозиторий {report['version_repo']}, профиль {report['version_profile']}"
              + ("" if report["version_repo"] == report["version_profile"]
                 else "  ← расходятся: профиль отстал или клон не обновлён"))
        if porcelain and not args.quiet:
            print(f"▪ незакоммиченное в клоне: {len(porcelain.splitlines())} путей")
        print("\n" + ("✔ профиль и репозиторий синхронны" if report["in_sync"]
                      else "⚠ есть расхождения — публикация не завершена или профиль отстал"))

    return 0 if report["in_sync"] else 1


def report_dirty(porcelain: str) -> bool:
    return bool(porcelain.strip())


def short_head() -> str:
    _, out = git("rev-parse", "--short", "HEAD")
    return out or "?"


if __name__ == "__main__":
    sys.exit(main())
