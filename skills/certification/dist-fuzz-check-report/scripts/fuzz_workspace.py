#!/usr/bin/env python3
"""Управление рабочими каталогами проверки фаззинг-материалов.

Материалы проверки (распакованные архивы, выборки, промежуточные файлы) живут
только в рабочем каталоге под базой $TMPDIR — Hermes вычищает простаивающие
записи сам, а каталог с маркером `.fuzzcheck-workspace` можно удалить одной
командой, не рискуя снести чужое.

Подкоманды:
  create  создать рабочий каталог, проверив свободное место
  list    показать существующие каталоги: объём, возраст, метка
  clean   удалить каталог (только если в нём есть маркер)
  purge   удалить все помеченные каталоги старше N дней

Примеры:
  fuzz_workspace.py create --need-gib 23 --label sflow-28.1 --archive sflow-28.1-cert.zip
  fuzz_workspace.py list --older-than 1
  fuzz_workspace.py clean /path/to/fuzzcheck/sflow-28.1-20261027-2115
  fuzz_workspace.py purge --older-than 1 --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

MARKER = ".fuzzcheck-workspace"
GIB = 1024 ** 3
MIB = 1024 ** 2


def human(n: float) -> str:
    for unit, div in (("ГиБ", GIB), ("МиБ", MIB), ("КиБ", 1024)):
        if abs(n) >= div:
            return f"{n / div:.1f} {unit}"
    return f"{int(n)} Б"


def resolve_base(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("FUZZCHECK_WORKSPACE_BASE")
    if env:
        return Path(env).expanduser()
    tmpdir = os.environ.get("TMPDIR")
    if tmpdir:
        return Path(tmpdir).expanduser() / "fuzzcheck"
    return Path("/var/tmp/fuzzcheck")


def free_bytes(path: Path) -> int:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free


def read_marker(path: Path) -> dict | None:
    m = path / MARKER
    if not m.is_file():
        return None
    try:
        return json.loads(m.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"label": "?", "created": None, "broken_marker": True}


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def age_days(created: str | None, path: Path) -> float:
    ts = None
    if created:
        try:
            ts = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
        except ValueError:
            ts = None
    if ts is None:
        ts = path.stat().st_mtime
    return max(0.0, (time.time() - ts) / 86400)


def safe_to_delete(path: Path) -> tuple[bool, str]:
    """Удалять можно только помеченный каталог, не корень и не домашний каталог."""
    try:
        path = path.resolve()
    except OSError as exc:
        return False, f"не удалось разрешить путь: {exc}"
    if path.is_symlink():
        return False, "путь является символической ссылкой"
    if not path.is_dir():
        return False, "путь не является каталогом"
    if path in (Path("/"), Path.home().resolve(), Path.cwd().resolve()):
        return False, "это корневой, домашний или текущий каталог"
    if not (path / MARKER).is_file():
        return False, f"нет маркера {MARKER} — каталог создан не этим скриптом, не удаляю"
    return True, ""


def iter_workspaces(base: Path):
    if not base.is_dir():
        return
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / MARKER).is_file():
            yield child


def cmd_create(args) -> int:
    base = resolve_base(args.base)
    need = args.need_gib * GIB
    base.mkdir(parents=True, exist_ok=True)

    free = free_bytes(base)
    required = int(need * 1.15) + int(args.headroom_gib * GIB)
    if not args.quiet:
        print(f"база:            {base}")
        print(f"нужно (с запасом 15 % + {args.headroom_gib:g} ГиБ): {human(required)}")
        print(f"свободно:        {human(free)}")
    if required > free and not args.force:
        sys.stdout.flush()
        print(
            "\nНЕДОСТАТОЧНО МЕСТА. Варианты:\n"
            "  1) распаковывать по группам: сначала метаданные и misc/, затем по одной\n"
            "     цели, удаляя группу после разбора (шаг 3 скилла);\n"
            "  2) указать другую базу: --base <каталог> с большим объёмом;\n"
            "  3) освободить место: python3 fuzz_workspace.py purge --older-than 1\n"
            "Переопределить проверку можно флагом --force (на свой риск).",
            file=sys.stderr,
        )
        return 2

    label = args.label or "check"
    stamp = time.strftime("%Y%m%d-%H%M")
    work = base / f"{label}-{stamp}"
    suffix = 1
    while work.exists():
        suffix += 1
        work = base / f"{label}-{stamp}-{suffix}"
    work.mkdir(parents=True)

    marker = {
        "schema": 1,
        "label": label,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "archive": args.archive or "",
        "need_gib": args.need_gib,
        "free_gib_at_creation": round(free / GIB, 1),
        "host": os.uname().nodename,
        "user": os.environ.get("USER", ""),
        "note": "временные материалы проверки; удалить: fuzz_workspace.py clean <путь>",
    }
    (work / MARKER).write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.quiet:
        print(work)
    else:
        print(f"\nрабочий каталог: {work}")
        print(f"для shell:       WORKSPACE={work}")
        print(f"\nв конце проверки: python3 {Path(__file__).name} clean {work}")
    return 0


def cmd_list(args) -> int:
    base = resolve_base(args.base)
    rows = []
    for path in iter_workspaces(base):
        meta = read_marker(path) or {}
        size = dir_size(path)
        days = age_days(meta.get("created"), path)
        rows.append((path, meta, size, days))
    if not rows:
        print(f"рабочих каталогов нет (база: {base})")
        return 0

    print(f"база: {base}  |  свободно: {human(free_bytes(base))}\n")
    print(f"{'возраст':>8}  {'объём':>10}  {'метка':<24} каталог")
    for path, meta, size, days in rows:
        flag = "  ← старше порога" if args.older_than is not None and days > args.older_than else ""
        print(f"{days:>7.1f}д  {human(size):>10}  {meta.get('label', '?'):<24} {path.name}{flag}")
    total = sum(r[2] for r in rows)
    print(f"\nвсего: {len(rows)} каталог(ов), {human(total)}")
    if args.older_than is not None:
        stale = [r for r in rows if r[3] > args.older_than]
        if stale:
            print(
                f"старше {args.older_than:g} дн.: {len(stale)} — "
                f"убрать: fuzz_workspace.py purge --older-than {args.older_than:g}"
            )
    return 0


def cmd_clean(args) -> int:
    path = Path(args.path).expanduser()
    ok, why = safe_to_delete(path)
    if not ok:
        print(f"ОТКАЗ: {why} ({path})", file=sys.stderr)
        return 3
    path = path.resolve()
    free_before = free_bytes(path.parent)
    size = dir_size(path)
    shutil.rmtree(path)
    free_after = free_bytes(path.parent)
    print(f"удалён: {path}")
    print(f"освобождено: {human(size)} (свободно на ФС: {human(free_before)} → {human(free_after)})")
    return 0


def cmd_purge(args) -> int:
    base = resolve_base(args.base)
    candidates = []
    for path in iter_workspaces(base):
        meta = read_marker(path) or {}
        days = age_days(meta.get("created"), path)
        if days > args.older_than:
            candidates.append((path, days, dir_size(path)))

    if not candidates:
        print(f"нечего удалять: помеченных каталогов старше {args.older_than:g} дн. нет (база: {base})")
        return 0

    total = 0
    for path, days, size in candidates:
        prefix = "бы удалён" if args.dry_run else "удалён"
        ok, why = safe_to_delete(path)
        if not ok:
            print(f"ОТКАЗ: {why} ({path})", file=sys.stderr)
            continue
        if args.dry_run:
            print(f"{prefix}: {path} (возраст {days:.1f} дн., {human(size)})")
        else:
            shutil.rmtree(path.resolve())
            print(f"{prefix}: {path} (возраст {days:.1f} дн., освобождено {human(size)})")
        total += size
    print(f"\nитого: {len(candidates)} каталог(ов), {human(total)}" + (" (пробный прогон)" if args.dry_run else ""))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Рабочие каталоги проверки фаззинг-материалов (база по умолчанию: $TMPDIR/fuzzcheck).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("create", help="создать рабочий каталог с проверкой свободного места")
    p.add_argument("--need-gib", type=float, default=0.0, help="ожидаемый объём распаковки, ГиБ")
    p.add_argument("--label", help="метка проверки (компонент/версия), попадает в имя каталога")
    p.add_argument("--archive", help="имя исходного архива — для маркера")
    p.add_argument("--base", help="база рабочих каталогов (по умолчанию $TMPDIR/fuzzcheck)")
    p.add_argument("--headroom-gib", type=float, default=2.0, help="обязательный запас свободного места, ГиБ (по умолчанию 2)")
    p.add_argument("--force", action="store_true", help="создать даже при нехватке места")
    p.add_argument("--quiet", action="store_true", help="напечатать только путь")
    p.set_defaults(func=cmd_create)

    p = sub.add_parser("list", help="показать рабочие каталоги")
    p.add_argument("--base", help="база рабочих каталогов")
    p.add_argument("--older-than", type=float, default=None, help="пометить каталоги старше N дней")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("clean", help="удалить рабочий каталог (по маркеру)")
    p.add_argument("path", help="путь рабочего каталога")
    p.set_defaults(func=cmd_clean)

    p = sub.add_parser("purge", help="удалить помеченные каталоги старше N дней")
    p.add_argument("--base", help="база рабочих каталогов")
    p.add_argument("--older-than", type=float, default=1.0, help="порог возраста, дней (по умолчанию 1)")
    p.add_argument("--dry-run", action="store_true", help="только показать, что будет удалено")
    p.set_defaults(func=cmd_purge)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
