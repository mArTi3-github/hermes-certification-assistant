#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Инвентаризация архива с материалами фаззинг-тестирования без распаковки.

Читает только central directory zip: мгновенно даже на десятках гигабайт и
показывает то, что нужно проверяющему в первую очередь:
  * объём и структуру (группы по 2 компонентам пути);
  * крупнейшие файлы (обычно это журналы фаззеров, которые не нужно распаковывать);
  * описательные документы (.md/.txt/.pdf/.docx вне каталогов журналов);
  * наличие fuzzer_stats / crashes / hangs по каждой цели;
  * дубликаты по имени файла (одни и те же журналы в нескольких местах).

Использование:
    fuzz_artifact_inventory.py <archive.zip> [--top 15]
"""
import argparse
import collections
import os
import sys
import zipfile


def human(n):
    for unit in ("Б", "КиБ", "МиБ", "ГиБ", "ТиБ"):
        if n < 1024 or unit == "ТиБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{n} Б"
        n /= 1024.0


DOC_EXT = (".md", ".txt", ".pdf", ".docx", ".odt", ".rtf", ".html", ".htm")
LOG_HINTS = ("log", "logs", "fuzzer", "validator", "stderr", "stdout", "-logs")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archive")
    ap.add_argument("--top", type=int, default=12)
    a = ap.parse_args()

    if not os.path.exists(a.archive):
        sys.exit(f"нет файла: {a.archive}")

    with zipfile.ZipFile(a.archive) as z:
        infos = [i for i in z.infolist() if not i.is_dir()]

    total = sum(i.file_size for i in infos)
    print(f"Архив: {a.archive}")
    print(f"Файлов: {len(infos)}; суммарный распакованный объём: {human(total)}; "
          f"сжатый: {human(os.path.getsize(a.archive))} "
          f"(коэффициент {total / max(os.path.getsize(a.archive), 1):.1f}×)")
    print()

    groups = collections.Counter()
    gsize = collections.Counter()
    for i in infos:
        parts = i.filename.strip("/").split("/")
        key = "/".join(parts[:2]) if len(parts) > 1 else parts[0]
        groups[key] += 1
        gsize[key] += i.file_size
    print("### Группы (2 первых компонента пути)")
    print("| Группа | Файлов | Объём |")
    print("|---|---:|---:|")
    for k, n in groups.most_common():
        print(f"| `{k}` | {n} | {human(gsize[k])} |")
    print()

    print(f"### Крупнейшие файлы (top {a.top})")
    print("| Файл | Объём |")
    print("|---|---:|")
    for i in sorted(infos, key=lambda x: -x.file_size)[: a.top]:
        print(f"| `{i.filename}` | {human(i.file_size)} |")
    print()

    docs = [i for i in infos
            if i.filename.lower().endswith(DOC_EXT)
            and not any(h in i.filename.lower() for h in LOG_HINTS)
            and "coverage" not in i.filename.lower()]
    print("### Описательные документы (кандидаты)")
    if docs:
        for i in docs:
            print(f"- `{i.filename}` — {human(i.file_size)}")
    else:
        print("- не найдено: материалы состоят только из артефактов прогона")
    print()

    print("### Признаки фаззинг-целей")
    names = [i.filename for i in infos]
    skip = {"input", "summary_coverage", "Readme", "coverage"}
    targets = sorted({p.split("/")[1] for p in names
                      if p.startswith("results/") and "/" in p[len("results/"):]
                      and p.split("/")[1] not in skip})
    for t in targets:
        prefix = f"results/{t}/"
        sub = [p for p in names if p.startswith(prefix)]
        if not any(k in " ".join(sub) for k in ("fuzzer_stats", "/corpus/", "/coverage/",
                                                "-fuzz", "/hangs/", "/crashes/")):
            continue
        stats = sum(1 for p in sub if p.endswith("fuzzer_stats"))
        crashes = sum(1 for p in sub if "/crashes/" in p)
        hangs = sum(1 for p in sub if "/hangs/" in p)
        corpus = sum(1 for p in sub if "/corpus/" in p)
        cov = sum(1 for p in sub if "/coverage/" in p)
        note = "" if stats else "  ! fuzzer_stats отсутствуют — метрики восстанавливать по журналам"
        print(f"- `{t}`: fuzzer_stats {stats}, corpus {corpus}, crashes {crashes}, "
              f"hangs {hangs}, coverage-отчёт {'есть' if cov else 'нет'}{note}")

    nested = [i for i in infos if i.filename.lower().endswith(".zip")]
    if nested:
        print("\n### Вложенные архивы (в них обычно corpus/crashes/hangs/misc)")
        for i in nested:
            print(f"- `{i.filename}` — {human(i.file_size)}")
    print()

    # дубликат = одинаковое имя И одинаковый размер (иначе это просто одноимённые
    # файлы разных инстансов, например plot_data у LA000..LA019)
    dup = collections.defaultdict(list)
    for i in infos:
        if i.file_size >= 1 << 20:
            dup[(os.path.basename(i.filename), i.file_size)].append(i.filename)
    dups = {k: v for k, v in dup.items() if len(v) > 1}
    print("### Дубликаты по имени файла")
    if dups:
        extra = 0
        for (name, size), v in sorted(dups.items(), key=lambda kv: -kv[0][1] * (len(kv[1]) - 1))[: a.top]:
            print(f"- `{name}` — {len(v)} идентичных копий по {human(size)}, напр.: "
                  + "; ".join(f"`{p}`" for p in v[:3]))
            extra += size * (len(v) - 1)
        print(f"\nИмён с крупными копиями: {len(dups)}; лишний объём от дублей: ≈ {human(extra)}")
    else:
        print("- нет")


if __name__ == "__main__":
    main()
