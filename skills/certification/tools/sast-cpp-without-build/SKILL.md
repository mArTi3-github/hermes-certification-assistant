---
name: sast-cpp-without-build
description: SAST C/C++ repos without a build (Semgrep, cppcheck).
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos]
metadata:
  hermes:
    tags: [appsec, sast, cpp, cppcheck, semgrep, sarif, triage, no-sudo]
    related_skills: [codeql-cli, codebase-inspection]
---

# Build-independent SAST for C/C++ repositories

## When to Use

- Asked to "scan" a large C/C++ project (Envoy, gRPC, protobuf) where a real build is impractical
  (no compiler/toolchain, tiny VM, Bazel/CMake deps in the GBs, hours of build time)
- Need a first-pass triage report fast, on a machine where you cannot install anything easily
- Deciding whether a full CodeQL build is worth it (see the `codeql-cli` skill for that path)

## Tool reality check (measured on Envoy v1.34.1, 1.2M lines C++)

| tool | needs build | C++ value | notes |
| --- | --- | --- | --- |
| CodeQL | YES for C/C++ (`--build-mode=none` works for C#/Java/JS/Python/Ruby only) | high | only path to taint/flow analysis |
| cppcheck | no (parses TUs standalone) | medium | 1351 files scanned, 43 findings; deep flow needs includes/macros |
| Semgrep | no | near-zero for C++ | registry has `p/c` (C) — **there is no `p/cpp`** (404); C++ results come only from `auto`/`p/default`. Still valuable for Python/Go/YAML/shell in the same repo |
| clang `--analyze`/clang-tidy | needs a compilation database | high if you have one | unusable without generated headers |

Install without root (works, but prefer apt when sudo exists): `apt-get download <pkg>` then `dpkg -x <pkg>.deb ~/opt/sysroot`.

## Pitfalls that cost time

- **cppcheck from a .deb extracted to a custom prefix fails** with `Failed to load library configuration file 'std.cfg'`:
  FILESDIR is compiled in (`/usr/lib/x86_64-linux-gnu/cppcheck/cfg`), and 2.19 has **no `--data-dir`** option,
  while `--library=<abs path to std.cfg>` does not override the builtin lookup. Fix: install the real package with sudo,
  or build cppcheck from source with `make install PREFIX=... FILESDIR=... CFGDIR=...`.
- Building cppcheck 2.19 with clang 21 fails out of the box (`lib/tokenize.o`, `lib/valueflow.o` errors) — not worth debugging when a package exists.
- Repos without generated protobuf (`*.pb.h` absent) cannot be compiled file-by-file: Envoy `source/common` needs
  10+ third-party header sets (absl 428 refs, quiche 129, BoringSSL 81, protobuf 24, libevent, spdlog, re2, gRPC, fmt, tcmalloc).
  Measure this BEFORE promising a "quick best-effort scan": `grep -rhoE '#include [<"]x[^">]+' <dir> | sed 's/#include [<"]//' | grep -vE '^(envoy|source|api|test|contrib)/' | awk -F/ 'NF>1{print $1}' | sort | uniq -c | sort -rn`.
- Semgrep on a huge repo: cap the work with `--max-target-bytes`, `--timeout`, `--jobs=N`, `--exclude=third_party`,
  and `--metrics=off` for deterministic runs.
- cppcheck over 1351 files with `--enable=warning,style,performance,portability --inconclusive -j4` takes ~50 min on 4 cores;
  it writes the output file only at the very end, so "is it done?" is answered by the process exit, not by the output file size.

## Procedure

1. Pin the version: `git clone --depth 1 --branch <tag> <url>`; record tag + `git rev-parse HEAD` in the report.
2. Scope production code only (`source/ contrib/`), not `test/` — tests multiply findings without security value.
3. Run the scanners, each to its own SARIF:
   - `semgrep scan --config=p/default --config=p/security-audit --config=p/secrets --config=p/c --sarif --output=semgrep.sarif --metrics=off --jobs=4 --max-target-bytes=2000000 --timeout=60 --exclude=.git --exclude=third_party .`
   - `cppcheck --enable=warning,style,performance,portability --inconclusive --std=c++20 --language=c++ -j4 --output-format=sarif --output-file=cppcheck.sarif --suppress=missingIncludeSystem --suppress=missingInclude --inline-suppr -Isource -I. source contrib`
4. Normalize all SARIF (tools disagree on severity schemes — cppcheck emits numeric CVSS-like
   `security-severity` 0.0-10.0, Semgrep emits `severity`, CodeQL emits `security-severity`/`problem.severity`)
   into one JSON with bucket mapping: >=9 critical, >=7 high, >=4 medium, >0 low.
5. Report with an explicit **Coverage and limitations** section: scope, mode (built vs not built), rules/count,
   files that failed to parse (`syntaxError`, `preprocessorErrorDirective`, `unknownMacro` findings list them),
   and what is therefore NOT covered (taint/interprocedural bugs). Never present a no-build scan as a security verdict.
6. Triage before reporting: read the code at each high/critical hit. Real examples from Envoy v1.34.1:
   - true: `envoy/filesystem/filesystem.h:148` — `FilePathAndType() = default;` leaves enum `file_type_` uninitialized.
   - false positive: `source/common/api/win32/os_sys_calls_impl.cc:83` `returnDanglingLifetime` — the pointer is into a
     vector that is `std::move`d into the returned struct, so the heap buffer outlives the local.

## Reporting artifacts (reusable)

- `findings.json` / `findings.csv` — normalized, query-able with jq (`select(.severity=="high")`)
- `report.md` — counts by tool/severity/rule/dir, hottest dirs, highest-severity table, limitations, jq cheatsheet
- keep raw `*.sarif` and `*.log` next to them

## Client triage-CSV format (engagements, PTAF-style sample)

Some customers require the findings in their own table instead of SARIF. Sample layout (match it exactly):
`;`-separated, UTF-8 **with BOM**, LF line endings, columns
`Критичность;Название;Вердикт;Комментарий;Состояние;Вхождения;Сканеры;Файлы` + extra fields to the right.

- `Критичность` — normalize scanner severities into Critical/High/Medium/Low (Semgrep `error`->High, `warning`->Medium;
  cppcheck's numeric `security-severity` 0.0-10.0 -> >=9 Critical, >=7 High, >=4 Medium; put the raw value in an extra column).
- `Название` — the finding `message`; `Файлы` — `path:line`; `Сканеры` — `Tool version`; `Вхождения` — 1 per row; `Состояние` — `open`.
- `Вердикт` — `true-positive` only when there is a real security impact; a defect without security impact is `false-positive`.
  Do NOT invent TPs: a mature project usually yields a handful (e.g. Envoy v1.34.1: 81 of 391 = unpinned GitHub Action tags).
- `Комментарий` — one substantive Russian sentence-set per rule, repeated for identical findings (allowed and expected);
  explain WHY it is FP: dev tooling with no trust boundary crossed, value not attacker-controlled, format-mandated weak hash, etc.
- Parser artefacts (`syntaxError`, `preprocessorErrorDirective`, `unknownMacro`) are not findings — keep them out of the
  table and list them in a separate note file; they mark the coverage gaps.
- Verify by re-reading the CSV with `csv.reader` (field counts per row) before delivering; sample files often carry no CRLF
  and a BOM, and their `Вердикт` column may use different vocabulary than the customer describes in words — ask.
