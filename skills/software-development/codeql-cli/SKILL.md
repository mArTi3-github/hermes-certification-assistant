---
name: codeql-cli
description: Install, verify, and run the CodeQL CLI static analyzer.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [appsec, static-analysis, codeql, sast, sarif, install]
    related_skills: [github, codebase-inspection]
---

# CodeQL CLI: install and run

## When to Use

- Installing or upgrading the CodeQL CLI / bundle on a workstation or VM
- Running CodeQL scans locally: building a database, running query suites, emitting SARIF
- Diagnosing "codeql: command not found" or empty `codeql resolve packs` output
- AppSec/SAST tasks where the CLI must feed results into another tool

## Install (recommended: the bundle, not the CLI-only ZIP)

The bundle ships CLI + query libraries + precompiled queries. The plain `github/codeql-cli-binaries` ZIP does NOT — avoid it.

```bash
# 1. find the latest bundle version
curl -sS https://api.github.com/repos/github/codeql-action/releases/latest | jq -r '.tag_name'

# 2. download the per-platform bundle (linux64 / linux-arm64 / osx64 / win64)
BASE=https://github.com/github/codeql-action/releases/download/codeql-bundle-v<VERSION>
curl -fL --retry 3 -o codeql-bundle-linux64.tar.zst "$BASE/codeql-bundle-linux64.tar.zst"
curl -fL -O "$BASE/codeql-bundle-linux64.tar.zst.checksum.txt"
sha256sum -c codeql-bundle-linux64.tar.zst.checksum.txt   # verify BEFORE extracting

# 3. extract; the tarball contains a top-level codeql/ dir
mkdir -p ~/codeql && tar --zstd -xf codeql-bundle-linux64.tar.zst -C ~/codeql
# layout: ~/codeql/codeql/codeql (binary), ~/codeql/codeql/qlpacks/ (query packs)
```

Avoid the all-platforms `codeql-bundle.tar.zst` (1 GB) — it is being retired and newer CLIs warn when run from it. `tar.gz` variants exist only for tools without zstd support.

## PATH pitfall on Ubuntu/Debian

Ubuntu's `~/.bashrc` starts with an interactive-only guard (`case $- in *i*) ;; *) return;; esac`), so a PATH export appended to the end of `~/.bashrc` is invisible to non-interactive shells — including agent tool sessions, cron, and CI. Two fixes, do both:

1. Export from `~/.profile` (covers login shells) — append an idempotent block, `export PATH` at the end.
2. Symlink into an already-on-PATH dir for immediate effect in the current session: `ln -sfn ~/codeql/codeql/codeql ~/.local/bin/codeql`.

The `codeql` launcher resolves its install root through symlinks, so the symlinked path still finds `qlpacks/` correctly (verify with `codeql resolve packs`).

## Verify the install

```bash
codeql --version                 # e.g. release 2.27.2
codeql resolve languages         # ~15 languages: java, python, javascript, cpp, csharp, go, ruby, swift, rust, actions, ...
codeql resolve packs | grep -c qlpack.yml   # ~47+ packs; paths must point INSIDE the extracted bundle
```

Empty `resolve packs` or pack paths outside `<root>/qlpacks` = you installed the CLI-only ZIP, not the bundle.

## Run an analysis (end-to-end smoke test)

```bash
cd <src-root>
codeql database create <db-dir> --language=python --source-root=. --overwrite --threads=$(nproc)
codeql database analyze <db-dir> codeql/python-queries:codeql-suites/python-security-and-quality.qls \
  --format=sarif-latest --output=results.sarif --threads=$(nproc)
jq -r '.runs[].results[] | "\(.ruleId) :: \(.message.text)"' results.sarif
```

Suite names follow `codeql/<lang>-queries:codeql-suites/<lang>-<suite>.qls` (`security-and-quality`, `security-extended`, `code-scanning`).

## Lessons

- A tiny synthetic file is not enough to trigger taint-tracking queries: `py/sql-injection` and command injection need a remote source (Flask/Django request data, `input()`, CLI args). Only non-taint queries like `py/weak-sensitive-data-hashing` fire on a bare demo script — a low result count on a toy file is not an install failure.
- Compiled languages (Java, C/C++, C#, Go) need a working build; `database create` runs the build or you pass `--command=<build cmd>`. Python/JS/Ruby need no build.
- Disk: the linux64 bundle extracts to ~1.4 GB; keep the downloaded `.tar.zst` (~530 MB) only if you plan to reinstall or copy to other hosts.
- `sudo` in agent sessions is interactive-password only and normally unavailable — install per-user under `~/codeql` unless the user says otherwise.
- Terms: free for public repos and for GitHub Code Security customers; the CLI is licensed under GitHub's CodeQL Terms and Conditions.
