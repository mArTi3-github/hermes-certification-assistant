#!/usr/bin/env python3
"""Кто автор неопубликованных правок: агент или фоновая ревизия (actor `curator`).

    python3 tools/attribution.py                    # активный профиль, человекочитаемо
    python3 tools/attribution.py --json             # машинный вывод
    python3 tools/attribution.py --profile DIR      # конкретный профиль
    python3 tools/attribution.py --repo DIR         # другой клон

Зачем: в профиль пишут двое — агент и фоновая ревизия Hermes (`background review`). Её правки
в журнале мутаций идут с `actor: "curator"`, правки агента через `patch`/`write_file` в журнал
не попадают вовсе (`skill_ledger.py` в каталоге `tools/` установки Hermes, функция `derive_actor`).
Из-за этого `sync.sh` возвращает 3
(«правка в профиле новее метки») независимо от того, кто писал, а `contribute.sh` уносит в PR
чужие правки с авторством того, кто запустил команду.

Правило авторства: файл принадлежит куратору, если в `<профиль>/skills/.curator_ledger.jsonl`
есть запись, у которой в `after` этот файл с текущим sha256 (для удалённых — в `before` записи
`delete`/`remove_file`). Иначе автор — агент.

Что считается неопубликованным: файлы публикуемых скиллов (`publish.list`), отличающиеся от
клона. Клон считается опубликованным состоянием: после мержа и `sync.sh` он равен профилю.

Коды возврата: 0 — публиковать нечего; 1 — есть правки; 2 — не найдено окружение.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import sync_state  # noqa: E402
from publish_policy import classify, profile_authored, scan  # noqa: E402

LEDGER = "skills/.curator_ledger.jsonl"
DELETE_ACTIONS = {"delete", "remove_file"}


def active_profile() -> Path:
    marker = Path.home() / ".hermes/active_profile"
    name = marker.read_text(encoding="utf-8").strip() if marker.exists() else ""
    return Path.home() / ".hermes/profiles" / (name or "hermes-certification-assistant")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ledger_index(profile: Path) -> tuple[dict[tuple[str, str], dict], dict[tuple[str, str], dict]]:
    """Индексы «(путь, sha256) → запись журнала»: по after (файл в этой редакции) и по before
    для удалений. Побеждает самая свежая запись."""
    after: dict[tuple[str, str], dict] = {}
    removed: dict[tuple[str, str], dict] = {}
    path = profile / LEDGER
    if not path.exists():
        return after, removed
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in entry.get("after") or []:
            after[(item["path"], item["sha256"])] = entry
        if entry.get("action") in DELETE_ACTIONS:
            for item in entry.get("before") or []:
                removed[(item["path"], item["sha256"])] = entry
    return after, removed


def collect(profile: Path, repo: Path) -> list[dict]:
    """Неопубликованные файлы публикуемых скиллов с атрибуцией автора."""
    authored, _bundled = profile_authored(profile)
    published = classify(authored)["published"] | classify(scan(repo / "skills"))["published"]
    after, removed = ledger_index(profile)

    rows: list[dict] = []
    for rel in sorted(published):
        profile_skill, repo_skill = profile / "skills" / rel, repo / "skills" / rel
        if not profile_skill.is_dir():
            continue
        profile_files = sync_state.tree_files(profile_skill)
        repo_files = sync_state.tree_files(repo_skill)

        for name, digest in profile_files.items():
            if repo_files.get(name) == digest:
                continue
            entry = after.get((str(profile_skill / name), digest))
            rows.append({
                "skill": rel,
                "file": name,
                "change": "modified" if name in repo_files else "added",
                "author": entry.get("actor", "curator") if entry else "agent",
                "session_id": (entry.get("evidence") or {}).get("session_id") if entry else None,
                "ledger_ts": entry.get("ts") if entry else None,
            })

        for name, digest in repo_files.items():
            if name in profile_files:
                continue
            entry = removed.get((str(repo_skill / name), digest))
            rows.append({
                "skill": rel,
                "file": name,
                "change": "deleted",
                "author": entry.get("actor", "curator") if entry else "agent",
                "session_id": (entry.get("evidence") or {}).get("session_id") if entry else None,
                "ledger_ts": entry.get("ts") if entry else None,
            })
    return rows


def group(rows: list[dict]) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        out[row["author"]].setdefault(row["skill"], []).append(f'{row["file"]} ({row["change"]})')
    return {author: dict(sorted(skills.items())) for author, skills in sorted(out.items())}


def render(rows: list[dict], profile: Path, repo: Path) -> str:
    if not rows:
        return "Публиковать нечего: профиль и клон совпадают."
    grouped = group(rows)
    sessions = {row["session_id"] for row in rows if row["author"] == "curator" and row["session_id"]}
    lines = [f"Неопубликовано: {len(rows)} файлов в {len({r['skill'] for r in rows})} скиллах",
             f"  профиль: {profile}", f"  клон:    {repo}", ""]
    titles = {"curator": "фоновая ревизия (background review)", "agent": "агент", "user": "пользователь"}
    for author, skills in grouped.items():
        total = sum(len(files) for files in skills.values())
        head = f"[{author}] {total} файлов — {titles.get(author, author)}"
        if author == "curator" and sessions:
            head += f", сессия {' '.join(sorted(sessions))}"
        lines.append(head)
        for rel, files in skills.items():
            lines.append(f"  {rel}: {', '.join(files)}")
        lines.append("")
    if "curator" in grouped:
        curator_skills = ", ".join(grouped["curator"])
        lines.append("Коммит для кураторских правок:")
        lines.append(f'  profile-contribution (или скилл-владелец): заметки куратора — {curator_skills}'
                     f'{" (background review, сессия " + " ".join(sorted(sessions)) + ")" if sessions else ""}')
    if "agent" in grouped:
        lines.append("Коммит для своих правок:")
        lines.append(f'  {", ".join(grouped["agent"])}')
    return "\n".join(lines)


def git(repo: Path, *args: str) -> tuple[int, str]:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    return res.returncode, (res.stdout + res.stderr).strip()


def split_head(profile: Path, repo: Path) -> int:
    """Разложить последний коммит на два по авторам: агент и фоновая ревизия.

    Вызывается из `contribute.sh` сразу после коммита и до push, поэтому переписывания
    отправленной ветки не происходит. Если автор один — коммит остаётся как был.
    """
    rc, listed = git(repo, "show", "--pretty=format:", "--name-only", "HEAD")
    if rc != 0:
        print(f"атрибуция пропущена: {listed}", file=sys.stderr)
        return 0
    files = [name for name in listed.splitlines() if name.strip()]
    if not files:
        return 0

    after, _removed = ledger_index(profile)
    mine: list[str] = []
    theirs: list[str] = []
    sessions: set[str] = set()
    for name in files:
        target = next((path for path in (profile / name, repo / name) if path.is_file()), None)
        entry = after.get((str(target), sha256(target))) if target else None
        if entry and entry.get("actor") == "curator":
            theirs.append(name)
            if (entry.get("evidence") or {}).get("session_id"):
                sessions.add(entry["evidence"]["session_id"])
        else:
            mine.append(name)

    if not theirs:
        print("▪ атрибуция: все файлы — правки агента, коммит один")
        return 0
    if not mine:
        print("▪ атрибуция: все файлы — заметки фоновой ревизии, коммит один")
        return 0

    rc, message = git(repo, "log", "-1", "--pretty=%B")
    if rc != 0:
        print(f"атрибуция пропущена: {message}", file=sys.stderr)
        return 0
    message = message.strip()

    if git(repo, "reset", "--soft", "HEAD~1")[0] != 0 or git(repo, "reset")[0] != 0:
        print("атрибуция пропущена: не удалось снять коммит", file=sys.stderr)
        return 0
    session = f", сессия {' '.join(sorted(sessions))}" if sessions else ""
    note = (f"заметки куратора{ session} (background review): {len(theirs)} файлов\n\n"
            "Перенесено из общего коммита по журналу мутаций скиллов\n"
            "(`.curator_ledger.jsonl`, actor: curator); содержимое — правки фоновой ревизии,\n"
            "не правки агента.")
    # Сначала чужие заметки, сверху — своя правка: заголовок PR остаётся про смысл изменения.
    if git(repo, "add", "-A", "--", *theirs)[0] != 0 or git(repo, "commit", "-q", "-m", note)[0] != 0:
        print("атрибуция пропущена: коммит куратора не создан", file=sys.stderr)
        return 0
    if git(repo, "add", "-A", "--", *mine)[0] != 0 or git(repo, "commit", "-q", "-m", message)[0] != 0:
        print("атрибуция пропущена: коммит агента не создан", file=sys.stderr)
        return 0

    _, short = git(repo, "log", "-2", "--pretty=▪ %h — %s")
    print("▪ атрибуция: коммит разложен по авторам")
    for line in short.splitlines():
        print(f"  {line}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Атрибуция неопубликованных правок скиллов")
    parser.add_argument("--profile", type=Path, help="каталог профиля")
    parser.add_argument("--repo", type=Path, default=ROOT, help="клон репозитория")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--split-head", action="store_true",
                        help="разложить последний коммит по авторам (вызывает contribute.sh)")
    args = parser.parse_args()

    profile = (args.profile or active_profile()).expanduser()
    repo = args.repo.expanduser()
    if not (profile / "skills").is_dir() or not (repo / "skills").is_dir():
        print("нет каталога скиллов в профиле или клоне", file=sys.stderr)
        return 2

    if args.split_head:
        return split_head(profile, repo)

    rows = collect(profile, repo)
    if args.json:
        print(json.dumps({
            "profile": str(profile),
            "repo": str(repo),
            "total": len(rows),
            "rows": rows,
            "authors": group(rows),
        }, ensure_ascii=False, indent=1))
    else:
        print(render(rows, profile, repo))
    return 1 if rows else 0


if __name__ == "__main__":
    sys.exit(main())
