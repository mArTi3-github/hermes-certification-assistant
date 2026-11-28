#!/usr/bin/env python3
"""Тесты политики PR (`tools/pr_policy.py`): какие скиллы PR меняет и что с версией.

Сценарии — те, что встречались на живых PR: обычный скилл `категория/имя`, служебный
инструмент во вложенной подкатегории (`certification/tools/<имя>`), удаление скилла
(плоского и вложенного), и файлы, которые к скиллам не относятся (`skills/DESCRIPTION.md`).
"""
from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "pr_policy.py"
FAILED: list[str] = []


def sh(*args: str, cwd: Path | None = None) -> str:
    res = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if res.returncode:
        raise RuntimeError(f"{' '.join(args)}: {res.stderr}")
    return res.stdout


def load_tool():
    spec = importlib.util.spec_from_file_location("pr_policy_under_test", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # dataclasses ищут модуль в sys.modules
    spec.loader.exec_module(module)
    return module


def write(repo: Path, rel: str, text: str) -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit_all(repo: Path, message: str) -> str:
    sh("git", "-C", str(repo), "add", "-A")
    sh("git", "-C", str(repo), "commit", "-q", "-m", message)
    return sh("git", "-C", str(repo), "rev-parse", "HEAD").strip()


def skill_md(name: str, version: str) -> str:
    return f"---\nname: {name}\ndescription: \"тестовый скилл\"\nversion: {version}\n---\n\n# {name}\n"


def case_flat_and_nested(tmp: Path) -> None:
    repo = tmp / "repo1"
    repo.mkdir()
    sh("git", "init", "-q", "-b", "main", str(repo))
    sh("git", "-C", str(repo), "config", "user.name", "test")
    sh("git", "-C", str(repo), "config", "user.email", "test@example.invalid")
    write(repo, "skills/certification/stand-ftest/SKILL.md", skill_md("stand-ftest", "1.0.0"))
    write(repo, "skills/certification/tools/docx-ordered/SKILL.md", skill_md("docx-ordered", "1.0.0"))
    write(repo, "skills/certification/tools/docx-ordered/scripts/read.py", "print('x')\n")
    write(repo, "skills/DESCRIPTION.md", "# Категории\n")
    base = commit_all(repo, "base")

    write(repo, "skills/certification/stand-ftest/SKILL.md", skill_md("stand-ftest", "1.0.1"))
    write(repo, "skills/certification/tools/docx-ordered/scripts/read.py", "print('y')\n")
    write(repo, "skills/DESCRIPTION.md", "# Категории\n\nновая строка\n")
    head = commit_all(repo, "change")

    tool = load_tool()
    tool.ROOT = repo
    changed = tool.changed_skills(base, head)
    if set(changed) != {"certification/stand-ftest", "certification/tools/docx-ordered"}:
        FAILED.append(f"плоский и вложенный скилл: получено {sorted(changed)}")
    if changed.get("certification/tools/docx-ordered") != {
        "skills/certification/tools/docx-ordered/scripts/read.py"
    }:
        FAILED.append("вложенный скилл: файл приписан не к нему")
    if "skills/DESCRIPTION.md" in str(changed):
        FAILED.append("файл вне каталога скилла попал в разбор")


def case_deleted_nested(tmp: Path) -> None:
    repo = tmp / "repo2"
    repo.mkdir()
    sh("git", "init", "-q", "-b", "main", str(repo))
    sh("git", "-C", str(repo), "config", "user.name", "test")
    sh("git", "-C", str(repo), "config", "user.email", "test@example.invalid")
    write(repo, "skills/certification/tools/old-tool/SKILL.md", skill_md("old-tool", "1.0.0"))
    write(repo, "skills/certification/tools/old-tool/scripts/run.py", "print('x')\n")
    write(repo, "skills/certification/stand-ftest/SKILL.md", skill_md("stand-ftest", "1.0.0"))
    base = commit_all(repo, "base")

    shutil.rmtree(repo / "skills/certification/tools/old-tool")
    write(repo, "skills/certification/stand-ftest/SKILL.md", skill_md("stand-ftest", "1.0.1"))
    head = commit_all(repo, "delete nested")

    tool = load_tool()
    tool.ROOT = repo
    changed = tool.changed_skills(base, head)
    if "certification/tools/old-tool" not in changed:
        FAILED.append(f"удалённый вложенный скилл не распознан: {sorted(changed)}")
    if "certification/tools" in changed:
        FAILED.append("удалённый вложенный скилл разобран как подкатегория `certification/tools`")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pr-policy-"))
    try:
        case_flat_and_nested(tmp)
        case_deleted_nested(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if FAILED:
        for item in FAILED:
            print(f"✖ {item}")
        print(f"\n{len(FAILED)} ✖")
        return 1
    print("✔ политика PR: плоские и вложенные скиллы, удаления")
    return 0


if __name__ == "__main__":
    sys.exit(main())
