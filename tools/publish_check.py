#!/usr/bin/env python3
"""Сверка профиля с клоном: что не опубликовано, что разошлось, что отстало.

    python3 tools/publish_check.py                      # активный профиль
    python3 tools/publish_check.py --profile DIR        # конкретный профиль
    python3 tools/publish_check.py --save DIR           # плюс копия непубликованных версий
    python3 tools/publish_check.py --json               # машинный вывод

Состояния публикуемого скилла:

    =  совпадает            профиль и репозиторий идентичны;
    ≠  правка в профиле     профиль изменён после синхронизации и не опубликован;
    ↓  профиль отстал       профиль не менялся, репозиторий ушёл вперёд (приедет при sync.sh);
    +  не опубликован       скилл есть в профиле, в репозитории его нет;
    -  нет в профиле        скилла нет в профиле вовсе.

Направление расхождения (`≠` против `↓`) определяется меткой синхронизации — см.
`tools/sync_state.py`: она хранит отпечатки содержимого, которые профиль получил при последней
синхронизации. Без метки направление неизвестно, и решение консервативное: расхождение считается
непубликованной правкой. Время правки используется только в пояснениях, а не в решении.

Зачем отдельный инструмент: `contribute.sh` копирует профиль в клон один раз и коммитит.
Между копированием и мержем профиль может измениться — правку дописывает агент, куратор
скиллов Hermes, другой сеанс на том же хосте. Такой слой не попадает в уже созданный PR,
и публикация тихо отстаёт от профиля.

Вторая задача — защита от потери при обновлении. `hermes profile update` заменяет каталог
скилла целиком, `tools/sync.sh` копирует поверх; неопубликованная правка при этом исчезает.
Поэтому `sync.sh` вызывает эту проверку до копирования и отказывается работать, пока есть
состояния `≠` или `+`. Флаг `--save DIR` кладёт копию непубликованных версий в каталог,
чтобы правку можно было вернуть даже после выполненного обновления.

Коды возврата: 0 — всё синхронно; 1 — есть неопубликованное или расхождения; 2 — не найдено
окружение (нет профиля, клон не git-репозиторий).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import sync_state  # noqa: E402
from publish_policy import classify, profile_authored, scan  # noqa: E402

STATUS_LABEL = {
    "equal": "=",
    "diverged": "≠",
    "stale": "↓",
    "not_published": "+",
    "missing_in_profile": "-",
}


def git(*args: str) -> tuple[int, str]:
    res = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return res.returncode, (res.stdout + res.stderr).strip()


def tree_files(root: Path) -> dict[str, str]:
    """Относительный путь → отпечаток файла; правила отбора — общие с меткой синхронизации."""
    return sync_state.tree_files(root)


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
    text = path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"^version:\s*(\S+)", text, re.M)
    return match.group(1) if match else "?"


def active_profile() -> Path:
    active = Path.home() / ".hermes/active_profile"
    name = active.read_text(encoding="utf-8").strip() if active.exists() else "hermes-certification-assistant"
    return Path.home() / ".hermes/profiles" / name


def collect(profile: Path) -> dict:
    """Состояние всех публикуемых скиллов: профиль против клона, направление — по метке."""
    repo_skills = set(scan(ROOT / "skills"))
    authored, _bundled = profile_authored(profile)
    policy = classify(authored)

    published_repo = classify(repo_skills)["published"]
    published_profile = policy["published"]

    marker = sync_state.read_marker(profile)
    marker_present = marker is not None

    skills: dict[str, dict] = {}
    for rel in sorted(published_repo | published_profile):
        if rel not in published_repo:
            skills[rel] = {"status": "not_published"}
            continue
        repo_skill = ROOT / "skills" / rel
        profile_skill = profile / "skills" / rel
        repo_hash = sync_state.tree_hash(repo_skill)
        profile_hash = sync_state.tree_hash(profile_skill)
        marked_hash = sync_state.marker_hash(marker, rel)
        status = sync_state.classify(repo_hash, profile_hash, marked_hash,
                                     marker_present=marker_present)
        info: dict = {
            "status": status,
            "repo_hash": repo_hash,
            "profile_hash": profile_hash,
            "marker_hash": marked_hash,
        }
        if status in {"diverged", "stale"}:
            info["edited_after_sync"] = sync_state.edited_after_sync(profile_skill, marker)
        if status == "diverged":
            info.update(compare(repo_skill, profile_skill))
        skills[rel] = info

    return {
        "skills": skills,
        "unclassified": sorted(policy["unclassified"]),
        "marker": marker,
        "marker_present": marker_present,
    }


def save_copies(profile: Path, skills: dict[str, dict], dest: Path) -> list[str]:
    """Копирует непубликованные версии скиллов: восстановление — обратным копированием."""
    saved: list[str] = []
    for rel, info in skills.items():
        if info["status"] not in {"diverged", "not_published"}:
            continue
        src = profile / "skills" / rel
        if not src.is_dir():
            continue
        target = dest / rel
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, target)
        saved.append(rel)
    return saved


def main() -> int:
    parser = argparse.ArgumentParser(description="Сверка профиля с клоном репозитория")
    parser.add_argument("--profile", metavar="КАТАЛОГ", help="каталог профиля")
    parser.add_argument("--save", metavar="КАТАЛОГ", help="сохранить копию непубликованных версий")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    parser.add_argument("--quiet", action="store_true", help="только расхождения и итог")
    args = parser.parse_args()

    profile = Path(args.profile).expanduser() if args.profile else active_profile()
    if not (profile / "skills").is_dir():
        sys.stderr.write(f"не найден профиль: {profile} (нет каталога skills)\n")
        return 2
    code, branch = git("rev-parse", "--abbrev-ref", "HEAD")
    if code:
        sys.stderr.write(f"клон не git-репозиторий: {ROOT}\n")
        return 2
    _, head = git("rev-parse", "--short", "HEAD")

    state = collect(profile)
    skills = state["skills"]
    _, porcelain = git("status", "--porcelain")
    _, counts = git("rev-list", "--left-right", "--count", "HEAD...origin/main")
    ahead, behind = (counts.split() + ["?", "?"])[:2] if counts else ("?", "?")

    pending = [rel for rel, info in skills.items() if info["status"] in {"diverged", "not_published"}]
    lagging = [rel for rel, info in skills.items()
               if info["status"] in {"missing_in_profile", "stale"}]
    stale = [rel for rel, info in skills.items() if info["status"] == "stale"]
    in_sync = not pending and not lagging
    marker = state["marker"]
    marker_info = {
        "present": state["marker_present"],
        "path": str(sync_state.marker_path(profile)),
        "synced_at": (marker or {}).get("synced_at"),
        "commit": (marker or {}).get("commit"),
        "commit_subject": (marker or {}).get("commit_subject"),
        "version": (marker or {}).get("version"),
        "skills": len((marker or {}).get("skills") or {}),
    }

    saved: list[str] = []
    if args.save and pending:
        saved = save_copies(profile, skills, Path(args.save).expanduser())

    report = {
        "repo": str(ROOT),
        "branch": branch,
        "head": head,
        "dirty": [line for line in porcelain.splitlines() if line.strip()],
        "ahead": ahead,
        "behind": behind,
        "profile": str(profile),
        "skills": skills,
        "sync_state": marker_info,
        "pending": pending,
        "lagging": lagging,
        "stale": stale,
        "unclassified": state["unclassified"],
        "saved": saved,
        "version_repo": distribution_version(ROOT / "distribution.yaml"),
        "version_profile": distribution_version(profile / "distribution.yaml"),
        "in_sync": in_sync,
    }

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(f"▪ клон:    {ROOT} ({branch} @ {head}, "
              f"{'чисто' if not porcelain else 'есть незакоммиченное'}, "
              f"{ahead}↑/{behind}↓ к origin/main)")
        print(f"▪ профиль: {profile}")
        if marker_info["present"]:
            synced = sync_state.parse_time(marker_info["synced_at"] or "")
            when = synced.astimezone().strftime("%Y-%m-%d %H:%M") if synced else "?"
            subject = f" «{marker_info['commit_subject']}»" if marker_info["commit_subject"] else ""
            print(f"▪ метка синхронизации: {(marker_info['commit'] or '?')[:7]} @ {when}, "
                  f"версия {marker_info['version']}, скиллов {marker_info['skills']}{subject}")
        else:
            print("▪ метки синхронизации нет — направление расхождения неизвестно, "
                  "расхождения считаются непубликованными правками")
        print(f"▪ публикуемых скиллов: {len(skills)}")
        if not args.quiet:
            for rel, info in skills.items():
                print(f"  {STATUS_LABEL.get(info['status'], '?')} {rel}")
        for rel in pending + lagging:
            info = skills[rel]
            print(f"\n  {STATUS_LABEL[info['status']]} {rel}")
            if info["status"] == "not_published":
                print("      скилла нет в репозитории — правка не опубликована")
                continue
            if info["status"] == "missing_in_profile":
                print("      скилла нет в профиле — профиль отстал, нужен tools/sync.sh")
                continue
            if info["status"] == "stale":
                print("      профиль не менялся с синхронизации, в репозитории версия новее — "
                      "приедет при tools/sync.sh (перезаписывать нечего)")
                continue
            if info.get("edited_after_sync") is True:
                print("      правка в профиле новее метки синхронизации — публикуйте, "
                      "обновление профиля её затрёт")
            elif marker_info["present"]:
                print("      профиль изменён после синхронизации — публикуйте, "
                      "обновление профиля правку затрёт")
            for key, label in (("changed", "изменён"), ("only_profile", "только в профиле"),
                               ("only_repo", "только в репозитории")):
                for name in info.get(key, []):
                    print(f"      {label}: {name}")
        if state["unclassified"]:
            print("\n▪ авторские скиллы вне политики (не публикуются, решать через --classify):")
            for rel in state["unclassified"]:
                print(f"    {rel}")
        if saved:
            print(f"\n▪ копии непубликованных версий сохранены в {Path(args.save).expanduser()}:")
            for rel in saved:
                print(f"    {rel}")
        print(f"\n▪ версия: репозиторий {report['version_repo']}, профиль {report['version_profile']}"
              + ("" if report["version_repo"] == report["version_profile"]
                 else "  ← расходятся: профиль отстал или клон не обновлён"))
        if porcelain and not args.quiet:
            print(f"▪ незакоммиченное в клоне: {len(porcelain.splitlines())} путей")
        if in_sync:
            print("\n✔ профиль и репозиторий синхронны")
        else:
            parts = []
            if pending:
                parts.append(f"непубликовано: {len(pending)} (публикуйте через contribute.sh)")
            if lagging:
                parts.append(f"профиль отстал: {len(lagging)} (обновите через sync.sh)")
            print("\n⚠ " + "; ".join(parts))

    return 0 if in_sync else 1


if __name__ == "__main__":
    sys.exit(main())
