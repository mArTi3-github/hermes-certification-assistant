#!/usr/bin/env python3
"""Политика публикации скиллов: что уходит в GitHub, а что остаётся локальным.

Источник истины — `publish.list` в корне репозитория. Модуль без внешних зависимостей,
поэтому одинаково работает и локально, и в CI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "publish.list"
SKILLS = ROOT / "skills"
SECTIONS = ("publish", "private")


def load(path: Path = POLICY) -> dict[str, list[str]]:
    """Разбирает publish.list в {'publish': [...], 'private': [...]}."""
    result: dict[str, list[str]] = {name: [] for name in SECTIONS}
    section: str | None = None
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            name = line[1:-1].strip()
            section = name if name in SECTIONS else None
            continue
        if section is None:
            continue
        item = line.split("#", 1)[0].strip().rstrip("/")
        if item:
            result[section].append(item)
    return result


def published(path: Path = POLICY) -> set[str]:
    return set(load(path)["publish"])


def private(path: Path = POLICY) -> set[str]:
    return set(load(path)["private"])


def repo_skills() -> set[str]:
    return {path.parent.relative_to(SKILLS).as_posix() for path in SKILLS.glob("*/*/SKILL.md")}


def include(skill: str, path: Path = POLICY) -> None:
    """Добавляет скилл в раздел [publish], сохраняя структуру файла."""
    skill = skill.strip().rstrip("/")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["[publish]", "", "[private]"]
    if skill in {line.split("#", 1)[0].strip().rstrip("/") for line in lines}:
        return
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "[publish]")
    except StopIteration:
        lines = ["[publish]", ""] + lines
        start = 0
    insert_at = start + 1
    while insert_at < len(lines) and (not lines[insert_at].strip() or lines[insert_at].lstrip().startswith("#")):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].strip() and not lines[insert_at].startswith("["):
        insert_at += 1
    lines.insert(insert_at, skill)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def profile_authored(profile: Path) -> tuple[set[str], set[str]]:
    """Авторские (не bundled) скиллы профиля и множество имён bundled."""
    manifest_file = profile / "skills" / ".bundled_manifest"
    bundled = set()
    if manifest_file.exists():
        for line in manifest_file.read_text(encoding="utf-8", errors="replace").splitlines():
            if ":" in line:
                bundled.add(line.split(":", 1)[0])
    authored = {
        path.parent.relative_to(profile / "skills").as_posix()
        for path in (profile / "skills").glob("*/*/SKILL.md")
        if path.parent.name not in bundled
    }
    return authored, bundled


def status(profile: Path | None) -> int:
    policy = load()
    pub, priv = set(policy["publish"]), set(policy["private"])
    problems = 0

    print("▪ Политика (publish.list):")
    print(f"    в публикацию: {len(pub)} | только локально: {len(priv)}")
    for item in sorted(pub & priv):
        print(f"    ✗ {item}: одновременно в [publish] и [private]")
        problems += 1

    in_repo = repo_skills()
    print(f"▪ Скиллы в репозитории: {len(in_repo)}")
    for item in sorted(in_repo - pub):
        print(f"    ✗ {item}: лежит в репозитории, но не в [publish]")
        problems += 1
    for item in sorted(pub - in_repo):
        print(f"    ✗ {item}: указан в [publish], но в репозитории отсутствует")
        problems += 1

    if profile and (profile / "skills").is_dir():
        authored, bundled = profile_authored(profile)
        print(f"▪ Профиль {profile.name}: авторских {len(authored)}, штатных {len(bundled)}")
        unclassified = authored - pub - priv
        for item in sorted(authored & pub):
            print(f"    публикуется:      {item}")
        for item in sorted(authored & priv):
            print(f"    только локально:  {item}")
        for item in sorted(unclassified):
            print(f"    ⚠ не классифицирован: {item} — строка в publish.list отсутствует")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Политика публикации скиллов")
    parser.add_argument("--published", action="store_true", help="напечатать пути из [publish]")
    parser.add_argument("--private", action="store_true", help="напечатать пути из [private]")
    parser.add_argument("--include", metavar="СКИЛЛ", help="добавить скилл в [publish]")
    parser.add_argument("--profile", metavar="КАТАЛОГ", help="каталог профиля для --status/--unclassified")
    parser.add_argument("--status", action="store_true", help="сверить политику, репозиторий и профиль")
    parser.add_argument("--unclassified", action="store_true",
                        help="напечатать авторские скиллы профиля вне политики (нужен --profile)")
    args = parser.parse_args()

    if args.published:
        for item in load()["publish"]:
            print(item)
        return 0
    if args.private:
        for item in load()["private"]:
            print(item)
        return 0
    if args.include:
        include(args.include)
        print(f"добавлено в [publish]: {args.include}")
        return 0
    if args.unclassified:
        if not args.profile:
            print("нужен --profile", file=sys.stderr)
            return 2
        authored, _ = profile_authored(Path(args.profile).expanduser())
        for item in sorted(authored - published() - private()):
            print(item)
        return 0
    return status(Path(args.profile).expanduser() if args.profile else None)


if __name__ == "__main__":
    sys.exit(main())
