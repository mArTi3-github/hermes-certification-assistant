#!/usr/bin/env python3
"""Принять накопившиеся PR: дождаться зелёного CI, смержить, выпустить один релиз.

    python3 tools/accept.py                   # все открытые PR
    python3 tools/accept.py 52 54             # только эти
    python3 tools/accept.py --bump patch      # тип релиза (по умолчанию minor)
    python3 tools/accept.py --dry-run         # показать, что было бы смержено

Что делает по каждому PR: пропускает черновики и PR не в `main`; ждёт завершения проверок
на голове ветки (успех = все завершены, ни одной с падением); мержит squash с заголовком
PR; после пачки — один релиз (`tools/release.py`) на все смерженные PR, затем `tools/sync.sh`
и `tools/publish_check.py`.

Содержательное ревью остаётся человеком: скрипт проверяет только механику — CI, базу,
мержабельность. Коды возврата: 0 — всё смержено и синхронизировано; 1 — часть PR пропущена;
2 — не найдено окружение.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SLUG = "mArTi3-github/hermes-certification-assistant"
BUMPS = ("major", "minor", "patch")


def token() -> str:
    value = os.environ.get("GITHUB_TOKEN", "").strip()
    if value:
        return value
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("GITHUB_TOKEN="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""


def git(*args: str) -> str:
    res = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    return res.stdout.strip() if res.returncode == 0 else ""


def slug() -> str:
    url = git("remote", "get-url", "origin")
    found = re.search(r"[:/]([^/:]+/[^/:]+?)(?:\.git)?$", url)
    if found and "/" in url:
        return found.group(1)
    return DEFAULT_SLUG


class Api:
    def __init__(self, repo: str, tok: str) -> None:
        self.repo = repo
        self.tok = tok

    def call(self, path: str, method: str = "GET", payload: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"https://api.github.com/repos/{self.repo}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.tok}",
                     "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read() or b"{}")
            except ValueError:
                return exc.code, {}


def checks_state(api: Api, sha: str) -> tuple[str, str]:
    """('ok'|'fail'|'pending'|'none', пояснение) по проверкам коммита."""
    _, data = api.call(f"/commits/{sha}/check-runs")
    runs = data.get("check_runs") or []
    if not runs:
        return "none", "проверок нет"
    bad = [r["name"] for r in runs if r.get("conclusion") in {"failure", "cancelled", "timed_out"}]
    if bad:
        return "fail", ", ".join(bad)
    if any(r.get("status") != "completed" for r in runs):
        return "pending", ", ".join(r["name"] for r in runs if r.get("status") != "completed")
    return "ok", f"{len(runs)} проверок"


def wait_checks(api: Api, sha: str, limit: int) -> tuple[str, str]:
    deadline = time.time() + limit
    while True:
        state, note = checks_state(api, sha)
        if state in {"ok", "fail", "none"}:
            return state, note
        if time.time() > deadline:
            return "pending", note
        time.sleep(10)


def bump(version: str, kind: str) -> str:
    parts = [int(x) for x in version.split(".")]
    while len(parts) < 3:
        parts.append(0)
    major, minor, patch = parts[:3]
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def current_version() -> str:
    text = (ROOT / "distribution.yaml").read_text(encoding="utf-8")
    found = re.search(r"^version:\s*(\S+)", text, re.M)
    return found.group(1) if found else "0.0.0"


def sh(*args: str) -> None:
    """git в клоне; ошибки не глотаем — без main релиз выпускать нельзя."""
    res = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if res.returncode:
        raise SystemExit(f"git {' '.join(args)}: {res.stderr.strip()}")


def run(*args: str) -> int:
    print(f"  $ {' '.join(args)}", flush=True)
    return subprocess.call(list(args), cwd=ROOT)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Принять накопившиеся PR")
    parser.add_argument("prs", nargs="*", type=int, help="номера PR (по умолчанию все открытые)")
    parser.add_argument("--bump", choices=BUMPS, default="minor", help="тип релиза")
    parser.add_argument("--wait", type=int, default=600, help="сколько секунд ждать CI на PR")
    parser.add_argument("--dry-run", action="store_true", help="ничего не мержить")
    parser.add_argument("--no-release", action="store_true", help="без релиза и синхронизации")
    parser.add_argument("--json", action="store_true", help="машинный вывод")
    args = parser.parse_args(argv)

    tok = token()
    if not tok and not args.dry_run:
        sys.stderr.write("нет GITHUB_TOKEN (окружение или ~/.hermes/.env)\n")
        return 2
    api = Api(slug(), tok)

    if args.prs:
        numbers = args.prs
    else:
        code, data = api.call("/pulls?state=open&per_page=50")
        if code != 200:
            sys.stderr.write(f"GitHub API ответил {code}\n")
            return 2
        numbers = [p["number"] for p in data]

    if not numbers:
        print("▪ открытых PR нет")
        return 0

    merged: list[int] = []
    skipped: list[tuple[int, str]] = []

    for number in numbers:
        code, pr = api.call(f"/pulls/{number}")
        if code != 200:
            skipped.append((number, f"API {code}"))
            continue
        title, draft = pr.get("title", ""), pr.get("draft")
        base, head = pr.get("base", {}).get("ref"), pr.get("head", {}).get("sha", "")
        print(f"▪ PR #{number}: {title}")
        if draft:
            skipped.append((number, "черновик"))
            print("  · черновик — пропущен")
            continue
        if base != "main":
            skipped.append((number, f"база {base}, а не main"))
            print(f"  · база {base} — пропущен")
            continue
        state, note = wait_checks(api, head, args.wait)
        if state == "fail":
            skipped.append((number, f"CI: {note}"))
            print(f"  ✖ CI: {note} — пропущен")
            continue
        if state == "pending":
            skipped.append((number, "CI не завершился"))
            print(f"  ⏳ CI не завершился ({note}) — пропущен")
            continue
        if state == "none":
            print("  ⚠ проверок на коммите нет — мержу без гейта (CI не запускался)")
        if args.dry_run:
            print("  · dry-run: мерж пропущен")
            continue
        code, res = api.call(f"/pulls/{number}/merge", "PUT", {
            "merge_method": "squash",
            "commit_title": f"{title} (#{number})",
            "commit_message": (pr.get("body") or "").strip().split("\n\n")[0][:1000],
        })
        if code == 200 and res.get("merged"):
            merged.append(number)
            print(f"  ✔ смержен: {res.get('sha', '')[:12]}")
        else:
            skipped.append((number, res.get("message", f"API {code}")))
            print(f"  ✖ мерж не удался: {res.get('message', code)}")

    if merged and not args.dry_run and not args.no_release:
        # release.py пушит HEAD:main — с contrib-ветки в main уехал бы её коммит.
        # Перед релизом клон обязан стоять на свежей main, иначе лучше остановиться.
        dirty = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain"],
                               capture_output=True, text=True).stdout.strip()
        if dirty:
            print("⚠ в клоне есть незакоммиченное — релиз и sync не запускаю:")
            print("\n".join(f"    {line}" for line in dirty.splitlines()[:5]))
            print("  Закоммитьте или уберите в stash и выпустите версию: "
                  f"python3 tools/release.py {bump(current_version(), args.bump)}")
            return 1
        sh("fetch", "-q", "origin")
        sh("switch", "-q", "main")
        sh("reset", "-q", "--hard", "origin/main")
        old = current_version()
        new = bump(old, args.bump)
        print(f"▪ релиз: {old} → {new} (на {len(merged)} PR)")
        if run(sys.executable, "tools/release.py", new) != 0:
            print("✖ release.py не отработал — sync не запускаю")
            return 1
        if run("tools/sync.sh") != 0:
            print("✖ sync.sh не отработал")
            return 1
        run(sys.executable, "tools/publish_check.py")

    if args.json:
        print(json.dumps({"merged": merged, "skipped": skipped}, ensure_ascii=False, indent=1))
    else:
        print()
        print(f"▪ смержено: {', '.join(f'#{n}' for n in merged) if merged else 'нет'}")
        for number, reason in skipped:
            print(f"  · #{number} пропущен: {reason}")
    return 0 if not skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
