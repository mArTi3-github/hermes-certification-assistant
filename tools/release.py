#!/usr/bin/env python3
"""Релиз одной командой: версия → CHANGELOG → коммит → тег → GitHub Release.

    python3 tools/release.py 1.7.0            # полный релиз
    python3 tools/release.py 1.7.0 --dry-run  # показать план, ничего не менять

Что делает:
  1. переносит накопленное из `## Unreleased` в раздел `## <версия> — <дата>`;
  2. поднимает `version` в `distribution.yaml`;
  3. коммитит, ставит аннотированный тег `v<версия>`;
  4. пушит `main` и тег, создаёт GitHub Release с текстом раздела.

Токен берётся из окружения Hermes и в вывод не попадает. Без токена шаги 1–3
выполняются, а релиз на GitHub пропускается с подсказкой.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CHANGELOG = REPO / "CHANGELOG.md"
INDEX = REPO / "distribution.yaml"
SLUG = "mArTi3-github/hermes-certification-assistant"


def sh(*args: str, check: bool = True) -> str:
    res = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True)
    if check and res.returncode:
        sys.exit(f"git {' '.join(args)}: {(res.stdout + res.stderr).strip()[-400:]}")
    return (res.stdout + res.stderr).strip()


def token() -> str | None:
    for path in (Path.home() / ".hermes/.env",
                 Path.home() / ".hermes/profiles/hermes-certification-assistant/.env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s*(?:export\s+)?GITHUB_TOKEN\s*=\s*['\"]?([^'\"\s]+)", line)
            if m:
                return m.group(1)
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Релиз дистрибутива")
    ap.add_argument("version", help="новая версия, например 1.7.0")
    ap.add_argument("--dry-run", action="store_true", help="только показать план")
    ap.add_argument("--no-release", action="store_true", help="не создавать GitHub Release")
    args = ap.parse_args()

    version = args.version.lstrip("v")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        sys.exit("версия должна быть вида 1.7.0")

    current = re.search(r"^version:\s*(\S+)", INDEX.read_text(encoding="utf-8"), re.M)
    current = current.group(1) if current else "?"
    if current == version:
        sys.exit(f"версия {version} уже стоит в distribution.yaml")

    def as_tuple(v: str) -> tuple[int, ...]:
        return tuple(int(part) for part in v.split("."))

    try:
        if as_tuple(version) < as_tuple(current):
            sys.exit(f"версия {version} меньше текущей {current}: теги неизменяемы, "
                     f"ошибочный релиз исправляется следующей версией")
    except ValueError:
        sys.exit(f"не удалось сравнить версии {current} и {version}")
    if sh("status", "--porcelain"):
        sys.exit("рабочее дерево грязное — закоммитьте правки до релиза")
    if sh("tag", "-l", f"v{version}"):
        sys.exit(f"тег v{version} уже существует")

    text = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(r"^## Unreleased[^\n]*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    if not match:
        sys.exit("в CHANGELOG нет раздела `## Unreleased`")
    body = match.group(1).strip()
    if not body:
        sys.exit("`## Unreleased` пуст — нечего выпускать")

    today = dt.date.today().isoformat()
    new_text = (text[:match.start()]
                + f"## Unreleased\n\n## {version} — {today}\n\n{body}\n\n"
                + text[match.end():])
    index_text = re.sub(r"^version:\s*\S+", f"version: {version}",
                        INDEX.read_text(encoding="utf-8"), count=1, flags=re.M)

    if args.dry_run:
        print(f"версия: {current} → {version}")
        print(f"раздел CHANGELOG: ## {version} — {today}, пунктов {body.count(chr(10) + '- ')+1}")
        print(f"тег: v{version}; коммит: «release {version}»")
        return 0

    CHANGELOG.write_text(new_text, encoding="utf-8")
    INDEX.write_text(index_text, encoding="utf-8")
    sh("add", "CHANGELOG.md", "distribution.yaml")
    sh("-c", "user.name=amaryan", "-c", "user.email=amaryan@localhost",
       "commit", "-m", f"release {version}")
    summary = next((line.strip("- ").strip() for line in body.splitlines()
                    if line.strip().startswith("-")), version)
    sh("tag", "-a", f"v{version}", "-m", summary[:800])
    print(f"▪ коммит и тег: {sh('log', '--oneline', '-1')}")

    tok = token()
    remote = f"https://x-access-token:{tok}@github.com/{SLUG}.git" if tok else "origin"
    out = subprocess.run(["git", "-C", str(REPO), "push", remote, "HEAD:main",
                          f"refs/tags/v{version}"], capture_output=True, text=True)
    print((out.stdout + out.stderr).replace(tok or "", "***").strip()[-300:])
    if out.returncode:
        return 1

    if args.no_release or not tok:
        print("GitHub Release не создан: нет токена или задан --no-release")
        return 0
    payload = {"tag_name": f"v{version}", "name": f"v{version}", "body": body,
               "draft": False, "prerelease": False, "make_latest": "true"}
    req = urllib.request.Request(
        f"https://api.github.com/repos/{SLUG}/releases", data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            print(f"▪ релиз: {json.load(resp)['html_url']}")
    except urllib.error.HTTPError as exc:
        print("▪ API GitHub:", exc.status, exc.read().decode()[:300])
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
