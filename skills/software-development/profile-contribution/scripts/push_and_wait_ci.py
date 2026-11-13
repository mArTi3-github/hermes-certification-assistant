#!/usr/bin/env python3
"""Push ветки в GitHub и ожидание CI по SHA головы.

    python3 push_and_wait_ci.py <каталог-репозитория> <owner/repo> [ветка] [--timeout СЕК]

Токен берётся из GITHUB_TOKEN, затем из файла окружения Hermes, и никогда не печатается.
Успех = workflow на новый SHA завершился со success; иначе код возврата не ноль.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

TOKEN_KEYS = ("GITHUB_TOKEN", "GH_TOKEN")


def die(msg: str, code: int = 1) -> None:
    print(msg, file=sys.stderr)
    raise SystemExit(code)


def find_token() -> str:
    for key in TOKEN_KEYS:
        value = os.environ.get(key, "").strip()
        if value:
            return value
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    for path in (hermes_home / ".env", Path.home() / ".hermes" / ".env"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            match = re.match(r"\s*(?:export\s+)?(GITHUB_TOKEN|GH_TOKEN)\s*=\s*['\"]?([^'\"\s]+)", line)
            if match:
                return match.group(2)
    die("токен не найден: нет GITHUB_TOKEN в окружении и в .env")
    raise AssertionError


def git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)


def head_sha(repo: Path) -> str:
    result = git(repo, "rev-parse", "HEAD")
    if result.returncode:
        die(f"git rev-parse: {result.stderr.strip()}")
    return result.stdout.strip()


def api(slug: str, token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{slug}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    timeout = 600
    for raw in sys.argv[1:]:
        if raw.startswith("--timeout"):
            timeout = int(raw.split("=", 1)[1]) if "=" in raw else timeout
    if len(args) < 2:
        die(__doc__ or "нужны каталог репозитория и owner/repo")

    repo, slug = Path(args[0]).expanduser().resolve(), args[1]
    branch = args[2] if len(args) > 2 else "main"
    token = find_token()

    sha = head_sha(repo)
    url = f"https://x-access-token:{token}@github.com/{slug}.git"
    pushed = subprocess.run(
        ["git", "-C", str(repo), "push", url, f"HEAD:{branch}"], capture_output=True, text=True
    )
    output = (pushed.stdout + pushed.stderr).replace(token, "***").strip()
    if output:
        print(output)
    if pushed.returncode:
        return pushed.returncode

    deadline = time.time() + timeout
    while time.time() < deadline:
        runs = api(slug, token, f"/actions/runs?branch={branch}&per_page=10").get("workflow_runs", [])
        run = next((r for r in runs if r.get("head_sha") == sha), None)
        if run and run.get("status") == "completed":
            print(f"CI: {run.get('name')} — {run.get('conclusion')} {run.get('html_url')}")
            return 0 if run.get("conclusion") == "success" else 1
        if run:
            print(f"CI: {run.get('name')} — {run.get('status')}…")
        time.sleep(10)
    die(f"CI не завершился за {timeout} с ({slug}@{sha[:7]})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
