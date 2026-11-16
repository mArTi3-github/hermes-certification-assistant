#!/usr/bin/env python3
"""Метка синхронизации: каким профиль был в момент последнего приёма обновлений.

Зачем. Профиль и клон расходятся в две стороны, а построчная разница файлов направления не
показывает: скилл, поправленный в профиле, и скилл, который в репозитории ушёл вперёд, выглядят
одинаково. Гейт `sync.sh` из-за этого останавливался на любом смерженном PR коллеги и требовал
`--force`, а `--force` заодно снимает защиту от потери правок.

Почему не по mtime. Время файла не описывает состояние: `cp -a` и `rsync` его сохраняют, правка
через инструменты ставит «сейчас», `hermes profile update` заменяет каталог целиком, часы на ВМ
коллег расходятся. После любой синхронизации mtime всех файлов профиля новее любого коммита —
наивное «что новее, то и правка» всегда отвечает «профиль новее» и блокирует обновление, то есть
даёт ровно противоположный ответ.

Что вместо. Метка хранит отпечатки содержимого, которые профиль получил при последней
синхронизации. Отпечаток каталога скилла — sha256 по отсортированным строкам «путь + sha256
файла», поэтому он не зависит от mtime, порядка обхода и путей вне скилла. Решение трёхстороннее:

    профиль == репозиторий                       → =   синхронно
    профиль == метка, репозиторий другой          → ↓   профиль не трогали, приедет обновление
    профиль != метка                             → ≠   правка в профиле после синхронизации
    метки нет (профиль старше этого инструмента)  → ≠   консервативно: сначала публикация

Время в метке остаётся, но только для объяснения: «правка в профиле новее синхронизации» —
подсказка в отчёте, а не критерий решения.

Файл метки: `<профиль>/.sync-state.json`, вне `skills/`, поэтому в публикацию не попадает.

    python3 tools/sync_state.py --write      # записать метку по текущему состоянию профиля
    python3 tools/sync_state.py --show       # что в метке
    python3 tools/sync_state.py --profile D  # другой профиль
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = 1
MARKER_NAME = ".sync-state.json"

# Мусор, который не публикуется и не должен менять отпечаток скилла.
IGNORED_NAMES = {"__pycache__", ".DS_Store", ".pytest_cache", ".dist_swap"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}

ROOT = Path(__file__).resolve().parent.parent


# --- отпечатки ---------------------------------------------------------------

def tree_files(root: Path) -> dict[str, str]:
    """Относительный путь → sha256 файла, без мусора, который не публикуется."""
    result: dict[str, str] = {}
    if not root.is_dir():
        return result
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if path.name in IGNORED_NAMES or path.suffix in IGNORED_SUFFIXES:
            continue
        if any(part in IGNORED_NAMES for part in parts[:-1]):
            continue
        result["/".join(parts)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def tree_hash(root: Path) -> str | None:
    """Отпечаток каталога скилла; None, если каталога нет."""
    if not root.is_dir():
        return None
    digest = hashlib.sha256()
    for name, file_hash in sorted(tree_files(root).items()):
        digest.update(f"{name}\0{file_hash}\n".encode())
    return digest.hexdigest()


def newest_mtime(root: Path) -> datetime | None:
    """Самое свежее время правки в каталоге — только для пояснений в отчёте."""
    if not root.is_dir():
        return None
    stamps = [p.stat().st_mtime for p in root.rglob("*") if p.is_file()
              and p.name not in IGNORED_NAMES and p.suffix not in IGNORED_SUFFIXES]
    return datetime.fromtimestamp(max(stamps), tz=timezone.utc) if stamps else None


# --- решение о направлении расхождения ---------------------------------------

EQUAL = "equal"
STALE = "stale"                      # профиль не трогали, репозиторий ушёл вперёд
DIVERGED = "diverged"                # правка в профиле после синхронизации
NOT_PUBLISHED = "not_published"      # скилла нет в репозитории
MISSING_IN_PROFILE = "missing_in_profile"


def classify(repo_hash: str | None, profile_hash: str | None, marker_hash: str | None,
             *, marker_present: bool) -> str:
    """Направление расхождения по трём отпечаткам.

    marker_hash — отпечаток, который профиль получил при последней синхронизации. Если метки нет,
    направление неизвестно, и решение консервативное: считаем расхождение непубликованной правкой.
    """
    if repo_hash is None:
        return NOT_PUBLISHED if profile_hash is not None else "absent"
    if profile_hash is None:
        return MISSING_IN_PROFILE
    if repo_hash == profile_hash:
        return EQUAL
    if marker_present and marker_hash is not None and profile_hash == marker_hash:
        return STALE
    return DIVERGED


def edited_after_sync(profile_skill: Path, marker: dict | None) -> bool | None:
    """Правка новее синхронизации? None — метки нет, судить не о чем.

    Это подсказка для отчёта: решение принимается по отпечаткам, а не по времени.
    """
    if not marker or not marker.get("synced_at"):
        return None
    newest = newest_mtime(profile_skill)
    if newest is None:
        return None
    synced_at = parse_time(marker["synced_at"])
    return None if synced_at is None else newest > synced_at


def parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# --- чтение и запись метки ---------------------------------------------------

def marker_path(profile: Path) -> Path:
    return profile / MARKER_NAME


def read_marker(profile: Path) -> dict | None:
    """Метка профиля или None, если её нет либо она от другой схемы."""
    path = marker_path(profile)
    if not path.is_file():
        return None
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("schema") != SCHEMA:
        return None
    if not isinstance(marker.get("skills"), dict):
        return None
    return marker


def marker_hash(marker: dict | None, rel: str) -> str | None:
    """Отпечаток скилла из метки; терпит и старый формат `{"hash": ...}`."""
    if not marker:
        return None
    value = (marker.get("skills") or {}).get(rel)
    if isinstance(value, dict):
        value = value.get("hash")
    return value if isinstance(value, str) else None


def git(repo: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else ""


def distribution_version(path: Path) -> str:
    if not path.is_file():
        return "?"
    match = re.search(r"^version:\s*(\S+)", path.read_text(encoding="utf-8", errors="replace"), re.M)
    return match.group(1) if match else "?"


def build_marker(profile: Path, repo: Path, skills: list[str]) -> dict:
    return {
        "schema": SCHEMA,
        "synced_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "repo": str(repo),
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD") or "?",
        "commit": git(repo, "rev-parse", "HEAD") or "?",
        "commit_subject": git(repo, "log", "-1", "--format=%s") or "",
        "version": distribution_version(repo / "distribution.yaml"),
        "skills": {rel: tree_hash(profile / "skills" / rel) for rel in sorted(skills)},
    }


def write_marker(profile: Path, repo: Path, skills: list[str]) -> dict:
    marker = build_marker(profile, repo, skills)
    path = marker_path(profile)
    path.write_text(json.dumps(marker, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return marker


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Метка синхронизации профиля Hermes")
    parser.add_argument("--profile", metavar="КАТАЛОГ", help="каталог профиля")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--write", action="store_true", help="записать метку текущим состоянием")
    action.add_argument("--show", action="store_true", help="показать метку")
    parser.add_argument("--repo", metavar="КАТАЛОГ", default=str(ROOT), help="клон репозитория")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    profile = Path(args.profile).expanduser().resolve() if args.profile else default_profile()
    if not (profile / "skills").is_dir():
        sys.stderr.write(f"не найден профиль: {profile} (нет каталога skills)\n")
        return 2

    if args.show:
        marker = read_marker(profile)
        if marker is None:
            print(f"метки нет: {marker_path(profile)}")
            return 1
        skills = len(marker.get("skills") or {})
        print(f"метка: {marker_path(profile)}")
        print(f"  синхронизировано: {marker.get('synced_at')} (UTC)")
        print(f"  коммит: {marker.get('commit', '?')[:7]} {marker.get('branch', '')} "
              f"{('«' + marker['commit_subject'] + '»') if marker.get('commit_subject') else ''}".rstrip())
        print(f"  версия: {marker.get('version', '?')}; скиллов в метке: {skills}")
        return 0

    published = published_skills()
    marker = write_marker(profile, repo, published)
    local = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"▪ метка синхронизации: {marker['commit'][:7]} {marker['branch']}, "
          f"{len(marker['skills'])} скиллов, {local}")
    return 0


def published_skills() -> list[str]:
    """Публикуемые скиллы — те же, что копирует sync.sh."""
    sys.path.insert(0, str(ROOT / "tools"))
    from publish_policy import classify as policy_classify, scan  # локальный импорт: без цикла

    return sorted(policy_classify(scan(ROOT / "skills"))["published"])


def default_profile() -> Path:
    active = Path.home() / ".hermes/active_profile"
    name = active.read_text(encoding="utf-8").strip() if active.exists() else "hermes-certification-assistant"
    return Path.home() / ".hermes/profiles" / name


if __name__ == "__main__":
    sys.exit(main())
