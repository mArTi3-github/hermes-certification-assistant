#!/usr/bin/env python3
"""Тесты трёхстороннего плана публикации (`tools/publish_plan.py`).

Сценарии — ровно те, что встречались при ревью: обычная правка профиля, отставший профиль,
правки с обеих сторон, конфликт, отсутствие метки синхронизации и удаление файла в апстриме.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "publish_plan.py"
SKILL = "skills/certification/demo"
FAILED: list[str] = []


def sh(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError(f"{' '.join(args)}: {res.stderr}")
    return res.stdout


def make_repo(base: Path, files: dict[str, str]) -> str:
    repo = base / "repo"
    (repo / SKILL).mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main", str(repo))
    sh("git", "-C", str(repo), "config", "user.name", "test")
    sh("git", "-C", str(repo), "config", "user.email", "test@example.invalid")
    for rel, text in files.items():
        target = repo / SKILL / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    sh("git", "-C", str(repo), "add", "-A")
    sh("git", "-C", str(repo), "commit", "-q", "-m", "base")
    return sh("git", "-C", str(repo), "rev-parse", "HEAD").strip()


def make_profile(base: Path, files: dict[str, str], commit: str | None) -> Path:
    profile = base / "profile"
    (profile / "skills" / "certification").mkdir(parents=True)
    (profile / "skills").mkdir(exist_ok=True)
    for rel, text in files.items():
        target = profile / SKILL / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if commit is not None:
        marker = {"schema": 1, "synced_at": "2026-11-19T00:00:00+00:00", "commit": commit,
                  "version": "1.0.0", "skills": {}}
        (profile / ".sync-state.json").write_text(json.dumps(marker), encoding="utf-8")
    return profile


def plan(repo: Path, profile: Path, *extra: str) -> tuple[int, dict]:
    res = subprocess.run([sys.executable, str(TOOL), "--repo", str(repo), "--profile", str(profile),
                          "--json", *extra], capture_output=True, text=True)
    return res.returncode, json.loads(res.stdout or "{}")


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ✔ {name}")
    else:
        print(f"  ✖ {name}{' — ' + detail if detail else ''}")
        FAILED.append(name)


BASE_FILES = {"SKILL.md": "alpha\nbeta\ngamma\n", "references/x.md": "ref\n"}


def scenario(name: str, mutate_repo, mutate_profile, marker: bool, *,
             expect: str, apply_extra: tuple[str, ...] = ()) -> None:
    print(f"▪ {name}")
    tmp = Path(tempfile.mkdtemp(prefix="plan-test-"))
    try:
        commit = make_repo(tmp, BASE_FILES)
        repo = tmp / "repo"
        if mutate_repo:
            mutate_repo(repo)
        profile = make_profile(tmp, BASE_FILES, commit if marker else None)
        if mutate_profile:
            mutate_profile(profile)
        code, data = plan(repo, profile)
        actions = {p["skill"]: p["action"] for p in data.get("plans", [])}
        skill = SKILL[len("skills/"):]
        if expect == "conflict":
            check(f"код возврата 3 (конфликт), получено {code}", code == 3)
            before = (repo / SKILL / "SKILL.md").read_text(encoding="utf-8")
            plan(repo, profile, "--apply")
            after = (repo / SKILL / "SKILL.md").read_text(encoding="utf-8")
            check("в клон ничего не записано", before == after)
            return
        check(f"действие «{expect}», получено «{actions.get(skill)}»", actions.get(skill) == expect)
        code2, _ = plan(repo, profile, "--apply")
        check(f"--apply завершился кодом 0, получено {code2}", code2 == 0)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def edit(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    scenario("профиль совпадает с репозиторием", None, None, marker=True, expect="equal")
    scenario("правка только в профиле",
             None, lambda p: edit(p / SKILL / "SKILL.md", "alpha\nbeta\ngamma\nнаше\n"),
             marker=True, expect="publish")
    scenario("репозиторий ушёл вперёд, профиль не трогали",
             lambda r: edit(r / SKILL / "SKILL.md", "alpha\nbeta\ngamma\nupstream\n"),
             None, marker=True, expect="align")
    scenario("обе стороны правили разные строки",
             lambda r: edit(r / SKILL / "SKILL.md", "ALPHA\nbeta\ngamma\n"),
             lambda p: edit(p / SKILL / "SKILL.md", "alpha\nbeta\nGAMMA\n"),
             marker=True, expect="merge")
    scenario("конфликт: обе стороны правили одну строку",
             lambda r: edit(r / SKILL / "SKILL.md", "alpha\nours\ntheirs\n"),
             lambda p: edit(p / SKILL / "SKILL.md", "alpha\nours\nнаше\n"),
             marker=True, expect="conflict")
    scenario("метки синхронизации нет — публикуем как есть",
             None, lambda p: edit(p / SKILL / "SKILL.md", "alpha\nbeta\ngamma\nнаше\n"),
             marker=False, expect="publish")
    scenario("файл удалён в апстриме, профиль не трогали",
             lambda r: (r / SKILL / "references/x.md").unlink(),
             None, marker=True, expect="align")

    # Отдельно: удаление файла в профиле при правке его же в апстриме — конфликт
    scenario("удаление в профиле против правки в апстриме",
             lambda r: edit(r / SKILL / "references/x.md", "ref changed\n"),
             lambda p: (p / SKILL / "references/x.md").unlink(),
             marker=True, expect="conflict")

    # Главный сценарий инцидента: профиль отстал, но в нём есть своя правка.
    print("▪ инцидент: профиль отстал и правился — слияние сохраняет обе стороны")
    tmp = Path(tempfile.mkdtemp(prefix="plan-test-"))
    try:
        commit = make_repo(tmp, BASE_FILES)
        repo = tmp / "repo"
        edit(repo / SKILL / "SKILL.md", "alpha\nbeta\ngamma\nабзац коллеги\n")
        profile = make_profile(tmp, BASE_FILES, commit)
        edit(profile / SKILL / "SKILL.md", "alpha\nbeta наша правка\ngamma\n")
        code, data = plan(repo, profile, "--apply")
        text = (repo / SKILL / "SKILL.md").read_text(encoding="utf-8")
        check(f"код 0 при слиянии, получено {code}", code == 0)
        check("в клоне остался абзац коллеги", "абзац коллеги" in text)
        check("в клоне осталась наша правка", "наша правка" in text)
        check("действие «merge»", {p["skill"]: p["action"] for p in data["plans"]}.get(
            SKILL[len("skills/"):]) == "merge")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILED:
        print(f"✖ провалов: {len(FAILED)} — {', '.join(FAILED)}")
        return 1
    print("✔ все сценарии плана публикации пройдены")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
