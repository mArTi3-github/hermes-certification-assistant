#!/usr/bin/env python3
"""Политика публикации скиллов.

Правила (в порядке приоритета):
1. `[private]` — скилл никогда не публикуется, что бы ни говорила категория.
2. `[publish]` — публикуется, даже если его категория не в списке по умолчанию.
3. `[defaults] publish_categories` — категории, все скиллы которых публикуются по умолчанию.
4. Всё остальное — локально: в репозиторий не попадает и коллегам не уезжает.

Источник истины — `publish.list` в корне репозитория; модуль без внешних зависимостей,
поэтому одинаково работает локально и в CI.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY = ROOT / "publish.list"
SKILLS = ROOT / "skills"
SECTIONS = ("defaults", "publish", "private")


def load(path: Path = POLICY) -> dict[str, object]:
    """Разбирает publish.list: категории по умолчанию и явные списки."""
    result: dict[str, object] = {"publish_categories": [], "publish": [], "private": []}
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
        item = line.split("#", 1)[0].strip()
        if not item:
            continue

        if section == "defaults":
            key, sep, value = item.partition(":")
            if sep and key.strip() == "publish_categories":
                parts = [part.strip().rstrip("/") for part in value.replace(",", " ").split()]
                result["publish_categories"] = [part for part in parts if part]
            continue

        result[section].append(item.rstrip("/"))  # type: ignore[index]
    return result


def publish_categories(path: Path = POLICY) -> set[str]:
    return set(load(path)["publish_categories"])  # type: ignore[arg-type]


def explicit_publish(path: Path = POLICY) -> set[str]:
    return set(load(path)["publish"])  # type: ignore[arg-type]


def explicit_private(path: Path = POLICY) -> set[str]:
    return set(load(path)["private"])  # type: ignore[arg-type]


def scan(root: Path) -> set[str]:
    """Скиллы в каталоге вида <root>/<категория>/<имя>/SKILL.md."""
    if not root.is_dir():
        return set()
    return {path.parent.relative_to(root).as_posix() for path in root.glob("*/*/SKILL.md")}


def classify(skills: set[str], path: Path = POLICY) -> dict[str, set[str]]:
    """Делит набор скиллов на публикуемые, локальные и неклассифицированные."""
    categories = publish_categories(path)
    published_explicit = explicit_publish(path)
    private_explicit = explicit_private(path)

    published = {
        skill for skill in skills
        if skill in published_explicit or skill.split("/", 1)[0] in categories
    }
    published -= private_explicit

    local = skills - published
    return {
        "published": published,
        "local": local,
        "private": local & private_explicit,
        "unclassified": local - private_explicit,
    }


def _append(section: str, item: str, path: Path = POLICY) -> None:
    """Дописывает строку в раздел, сохраняя структуру файла."""
    item = item.strip().rstrip("/")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    if not lines:
        lines = ["[defaults]", "publish_categories: certification", "", "[publish]", "", "[private]"]
    if item in {line.split("#", 1)[0].strip().rstrip("/") for line in lines}:
        return

    header = f"[{section}]"
    if header not in [line.strip() for line in lines]:
        lines += ["", header]
    start = next(i for i, line in enumerate(lines) if line.strip() == header)

    insert_at = start + 1
    while insert_at < len(lines) and (not lines[insert_at].strip() or lines[insert_at].lstrip().startswith("#")):
        insert_at += 1
    while insert_at < len(lines) and lines[insert_at].strip() and not lines[insert_at].startswith("["):
        insert_at += 1

    lines.insert(insert_at, item)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _drop(item: str, path: Path = POLICY) -> None:
    """Убирает строку из всех разделов."""
    item = item.strip().rstrip("/")
    if not path.exists():
        return
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines()
        if line.split("#", 1)[0].strip().rstrip("/") != item
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def profile_authored(profile: Path) -> tuple[set[str], set[str]]:
    """Авторские (не bundled) скиллы профиля и множество имён bundled."""
    manifest_file = profile / "skills" / ".bundled_manifest"
    bundled: set[str] = set()
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
    categories = set(policy["publish_categories"])  # type: ignore[arg-type]
    explicit_pub = set(policy["publish"])  # type: ignore[arg-type]
    explicit_priv = set(policy["private"])  # type: ignore[arg-type]
    problems = 0

    print("▪ Политика (publish.list):")
    print(f"    категории по умолчанию: {', '.join(sorted(categories)) or '—'}")
    print(f"    явные исключения: публикуется {len(explicit_pub)}, приватно {len(explicit_priv)}")
    for item in sorted(explicit_pub & explicit_priv):
        print(f"    ✗ {item}: одновременно в [publish] и [private]")
        problems += 1
    for item in sorted(explicit_pub):
        if item.split("/", 1)[0] in categories:
            print(f"    · {item}: в [publish], хотя категория и так публикуется — строка лишняя")

    in_repo = scan(SKILLS)
    repo_state = classify(in_repo)
    print(f"▪ Скиллы в репозитории: {len(in_repo)}")
    for item in sorted(in_repo - repo_state["published"]):
        print(f"    ✗ {item}: лежит в репозитории, но политикой не публикуется")
        problems += 1

    if profile and (profile / "skills").is_dir():
        authored, bundled = profile_authored(profile)
        state = classify(authored)
        print(f"▪ Профиль {profile.name}: авторских {len(authored)}, штатных {len(bundled)}")
        for item in sorted(state["published"]):
            print(f"    публикуется:      {item}")
        for item in sorted(state["private"]):
            print(f"    только локально:  {item}")
        for item in sorted(state["unclassified"]):
            print(f"    ⚠ не классифицирован: {item} — решите: certify / local")
    return 1 if problems else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Политика публикации скиллов")
    parser.add_argument("--published", action="store_true", help="напечатать публикуемые скиллы")
    parser.add_argument("--private", action="store_true", help="напечатать явно приватные")
    parser.add_argument("--unclassified", action="store_true", help="напечатать скиллы вне политики")
    parser.add_argument("--include", metavar="СКИЛЛ", help="добавить скилл в [publish]")
    parser.add_argument("--classify", nargs=2, metavar=("СКИЛЛ", "publish|private"),
                        help="зафиксировать решение по скиллу")
    parser.add_argument("--profile", metavar="КАТАЛОГ", help="каталог профиля для сканирования")
    parser.add_argument("--status", action="store_true", help="сверить политику, репозиторий и профиль")
    args = parser.parse_args()

    profile = Path(args.profile).expanduser() if args.profile else None
    skills = scan(profile / "skills") if profile else scan(SKILLS)
    state = classify(skills)

    if args.published:
        for item in sorted(state["published"]):
            print(item)
        return 0
    if args.private:
        for item in sorted(explicit_private()):
            print(item)
        return 0
    if args.unclassified:
        for item in sorted(state["unclassified"]):
            print(item)
        return 0
    if args.include:
        _append("publish", args.include)
        print(f"добавлено в [publish]: {args.include}")
        return 0
    if args.classify:
        skill, decision = args.classify
        if decision == "private":
            _drop(skill)
            _append("private", skill)
            print(f"{skill}: локально (добавлено в [private])")
        elif decision == "publish":
            _drop(skill)
            _append("publish", skill)
            print(f"{skill}: публикуется (добавлено в [publish])")
        else:
            print("решение должно быть publish или private", file=sys.stderr)
            return 2
        return 0
    return status(profile)


if __name__ == "__main__":
    sys.exit(main())
