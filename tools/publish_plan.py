#!/usr/bin/env python3
"""Трёхсторонний план публикации: профиль против `main`, база — метка синхронизации.

    python3 tools/publish_plan.py                  # только показать план
    python3 tools/publish_plan.py --apply          # разложить результат в рабочий клон
    python3 tools/publish_plan.py --json           # машинный вывод

Зачем: до этой правки `contribute.sh` копировал каталог скилла из профиля поверх клона
целиком (`rsync --delete`). Если профиль отстал от `main`, такой PR молча удалял чужие
правки, и ни линт, ни CI этого не видели. База для слияния есть — метка синхронизации
хранит коммит, с которого профиль приехал (`.sync-state.json` → `commit`).

Решение по каждому файлу скилла (b — база, o — профиль, t — рабочий клон):

    o == t    ничего делать не нужно;
    o == b    берём апстрим (t);
    t == b    наше изменение (o) — уезжает в PR;
    иначе     обе стороны двигались — `git merge-file`; конфликт останавливает публикацию.

Действия по скиллу: «совпадает», «публикуем», «сливаем», «выровнять» (профиль отстал,
публиковать нечего — приедет `sync.sh`), «конфликт».

Коды возврата: 0 — план применим; 3 — есть конфликты (в клон ничего не записано);
2 — не найдено окружение.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import sync_state  # noqa: E402
from publish_policy import classify, profile_authored, scan  # noqa: E402

ACTIONS = {
    "equal": "совпадает",
    "publish": "публикуем",
    "merge": "сливаем",
    "align": "выровнять",
    "conflict": "конфликт",
}
ICONS = {"equal": "=", "publish": "↑", "merge": "⇄", "align": "↓", "conflict": "✖"}


def git(repo: Path, *args: str) -> tuple[int, bytes]:
    res = subprocess.run(["git", "-C", str(repo), *args], capture_output=True)
    return res.returncode, res.stdout


def active_profile() -> Path:
    env = os.environ.get("HERMES_PROFILE_DIR")
    if env:
        return Path(env).expanduser()
    marker = Path.home() / ".hermes" / "active_profile"
    name = marker.read_text(encoding="utf-8").strip() if marker.is_file() else "default"
    return Path.home() / ".hermes" / "profiles" / (name or "default")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def ignored(rel: str) -> bool:
    parts = Path(rel).parts
    if any(part in sync_state.IGNORED_NAMES for part in parts):
        return True
    return Path(rel).suffix in sync_state.IGNORED_SUFFIXES


def blob_tree(repo: Path, commit: str | None, rel: str) -> dict[str, str] | None:
    """Отпечатки файлов скилла в коммите базы; None — коммита в клоне нет."""
    if not commit:
        return None
    if git(repo, "cat-file", "-e", f"{commit}^{{commit}}")[0] != 0:
        return None
    code, out = git(repo, "ls-tree", "-r", "--name-only", commit, "--", f"skills/{rel}")
    if code != 0:
        return None
    result: dict[str, str] = {}
    prefix = f"skills/{rel}/"
    for path in out.decode("utf-8", "replace").splitlines():
        if not path or not path.startswith(prefix) or ignored(path):
            continue
        code, raw = git(repo, "show", f"{commit}:{path}")
        if code == 0:
            result[path[len(prefix):]] = sha(raw)
    return result


def merge3(ours: bytes, base: bytes, theirs: bytes) -> tuple[bytes, bool]:
    """`git merge-file`; возвращает (содержимое, был_ли_конфликт)."""
    tmp = Path(tempfile.mkdtemp(prefix="plan-"))
    try:
        names = []
        for name, data in (("ours", ours), ("base", base), ("theirs", theirs)):
            file = tmp / name
            file.write_bytes(data)
            names.append(str(file))
        res = subprocess.run(["git", "merge-file", "-p", *names], capture_output=True)
        conflicted = res.returncode != 0
        return (ours if conflicted else res.stdout), conflicted
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def plan_skill(repo: Path, profile: Path, rel: str, commit: str | None) -> dict:
    """План по одному скиллу. merged — содержимое слияний, в JSON не попадает."""
    repo_skill = repo / "skills" / rel
    profile_skill = profile / "skills" / rel
    ours = sync_state.tree_files(profile_skill)
    theirs = sync_state.tree_files(repo_skill)
    base = blob_tree(repo, commit, rel)
    merged: dict[str, bytes] = {}
    ops: list[dict] = []
    conflicts: list[str] = []

    if base is None:
        # Метки нет или коммит не найден: базы для слияния нет, сравниваем напрямую.
        diff = sorted(p for p in set(ours) | set(theirs) if ours.get(p) != theirs.get(p))
        action = "publish" if diff else "equal"
        return {"skill": rel, "action": action,
                "ops": [{"path": p, "op": "ours"} for p in diff],
                "conflicts": [], "merged": {}, "base_known": False}

    for path in sorted(set(base) | set(ours) | set(theirs)):
        b, o, t = base.get(path), ours.get(path), theirs.get(path)
        if o == t:
            continue
        if o == b:
            ops.append({"path": path, "op": "upstream"})
            continue
        if t == b:
            ops.append({"path": path, "op": "ours"})
            continue
        if o is None or t is None or b is None:
            conflicts.append(path)          # добавление против удаления и подобное
            continue
        code, base_raw = git(repo, "show", f"{commit}:skills/{rel}/{path}")
        content, bad = merge3((profile_skill / path).read_bytes(),
                              base_raw if code == 0 else b"",
                              (repo_skill / path).read_bytes())
        if bad:
            conflicts.append(path)
        else:
            ops.append({"path": path, "op": "merged"})
            merged[path] = content

    mine = [op for op in ops if op["op"] in {"ours", "merged"}]
    if conflicts:
        action = "conflict"
    elif not ops:
        action = "align" if ours != theirs else "equal"
    elif any(op["op"] == "merged" for op in ops):
        action = "merge"
    elif mine:
        action = "publish"
    else:
        action = "align"
    return {"skill": rel, "action": action, "ops": ops, "conflicts": conflicts,
            "merged": merged, "base_known": True}


def apply_skill(repo: Path, profile: Path, rel: str, plan: dict) -> int:
    """Раскладывает план в рабочий клон; возвращает число записанных файлов."""
    repo_skill = repo / "skills" / rel
    profile_skill = profile / "skills" / rel
    written = 0
    for op in plan["ops"]:
        path, kind = op["path"], op["op"]
        if kind == "upstream":
            continue
        target = repo_skill / path
        if kind == "merged":
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(plan["merged"][path])
            written += 1
            continue
        source = profile_skill / path
        if source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source.read_bytes())
            written += 1
        elif target.exists():
            target.unlink()
            written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Трёхсторонний план публикации профиля")
    parser.add_argument("--profile", help="каталог профиля (по умолчанию активный)")
    parser.add_argument("--repo", default=str(ROOT), help="клон репозитория")
    parser.add_argument("--apply", action="store_true", help="разложить план в рабочий клон")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--quiet", action="store_true", help="только скиллы с действиями")
    args = parser.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve()
    profile = Path(args.profile).expanduser() if args.profile else active_profile()
    if not (profile / "skills").is_dir():
        sys.stderr.write(f"не найден профиль: {profile}\n")
        return 2

    authored, _bundled = profile_authored(profile)
    published = classify(authored)["published"] | classify(scan(repo / "skills"))["published"]

    marker = sync_state.read_marker(profile)
    base_commit = (marker or {}).get("commit")
    base_known = bool(base_commit) and git(repo, "cat-file", "-e", f"{base_commit}^{{commit}}")[0] == 0

    plans = [plan_skill(repo, profile, rel, base_commit if base_known else None)
             for rel in sorted(published)]
    conflicts = [p["skill"] for p in plans if p["action"] == "conflict"]
    todo = [p for p in plans if p["action"] != "equal"]

    if args.json:
        stripped = [{k: v for k, v in p.items() if k != "merged"} for p in plans]
        print(json.dumps({
            "profile": str(profile),
            "repo": str(repo),
            "base_commit": base_commit if base_known else None,
            "base_known": base_known,
            "plans": stripped,
            "conflicts": conflicts,
        }, ensure_ascii=False, indent=1))
    else:
        where = (f"{(base_commit or '')[:12]} (метка синхронизации)" if base_known
                 else "метки нет — публикуется как есть, без слияния")
        print(f"▪ база слияния: {where}")
        for plan in plans:
            if args.quiet and plan["action"] == "equal":
                continue
            count = len([op for op in plan["ops"] if op["op"] != "upstream"])
            extra = f" — файлов {count}" if plan["action"] in {"publish", "merge"} else ""
            print(f"  {ICONS[plan['action']]} {plan['skill']}: {ACTIONS[plan['action']]}{extra}")
            for path in plan["conflicts"]:
                print(f"      ✖ {path}")
        if not todo:
            print("  ✔ публиковать нечего: профиль совпадает с репозиторием")
        if conflicts:
            print("✖ конфликты: обе стороны правили одни и те же файлы")
            print("  Разрешите вручную (`git merge-file`) или публикуйте правку меньшим куском.")

    if conflicts:
        return 3
    if args.apply:
        written = sum(apply_skill(repo, profile, plan["skill"], plan)
                      for plan in plans if plan["action"] in {"publish", "merge"})
        if not args.json:
            print(f"▪ разложено в клон: файлов {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
