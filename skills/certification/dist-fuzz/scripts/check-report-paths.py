#! /usr/bin/env python3
"""Сверяет упоминания файлов в отчёте с раскладкой сдаваемого комплекта.

Запуск:  check-report-paths.py <корень комплекта> <report.md | report.html>

Корень комплекта — каталог, где лежат report.md и подкаталог evidence/build/.
Пути берутся и из markdown-спанов (`...`), и из HTML (`<code>...</code>`), поэтому одна
и та же проверка годится для report.md и для собранного из него report.html.
Скрипт не правит отчёт: он перечисляет упоминания, которые не удалось разрешить
(настоящие битые пути — ошибка, шаблоны `<цель>`, `*`, `{a,b}`, `…` и имена методов
пропускаются молча). Код возврата 1 — есть битый путь, начинающийся с `evidence/`.
"""
import html as html_mod
import os
import re
import sys

SPAN = re.compile(r"`([^`\n]+)`|<code>(.*?)</code>")
SKIP = re.compile(r"[*{}<>…\s]|\.\.\.")
PATHY = re.compile(r"/|\.[A-Za-z0-9]{1,6}$")
MOUNT = "evidence/build"


def manifest_of(base):
    paths = set()
    for root, dirs, files in os.walk(base):
        rel = os.path.relpath(root, base)
        rel = "" if rel == "." else rel.replace(os.sep, "/") + "/"
        for f in files:
            paths.add(rel + f)
        for d in dirs:
            paths.add(rel + d + "/")
    return paths


def resolve(text, manifest, roots, root=None):
    t = text.strip()
    if not t or SKIP.search(t):
        return None
    # сам корень архива — законное упоминание, в манифест он не попадает
    if t.rstrip("/") == MOUNT:
        return True
    tries = []
    if t.startswith(MOUNT + "/"):
        tries.append(("rel", t[len(MOUNT) + 1:]))
    elif t.startswith("evidence/"):
        rest = t[len("evidence/"):]
        if rest:
            tries.append(("rel", rest))
    elif t.startswith("output/"):
        tries.append(("root", t[len("output/"):]))
    else:
        tries.append(("rel", t))
        tries.append(("root", t))
        tries.append(("fs", t))
    for kind, key in tries:
        key = key.strip()
        if kind == "rel" and key in manifest:
            return True
        if kind == "root" and key in roots:
            return True
        if kind == "fs" and root and os.path.exists(os.path.join(root, key)):
            return True
    return False


def main():
    if len(sys.argv) < 3:
        print("usage: check-report-paths.py <корень комплекта> <report.md>")
        return 2
    root, report = sys.argv[1], sys.argv[2]
    archive = os.path.join(root, MOUNT)
    if not os.path.isdir(archive):
        print("нет каталога %s — отчёт и архив должны лежать рядом" % archive)
        return 2
    manifest = manifest_of(archive)
    roots = {n for n in ("report.md", "report.html", "findings.md", "collections.md", "README.md")
             if os.path.exists(os.path.join(root, n))}
    # HTML-экранирование (`&lt;цель&gt;`) иначе прячет шаблоны от фильтра
    text = html_mod.unescape(open(report, encoding="utf-8").read())
    total, broken, skipped = 0, [], 0
    for m in SPAN.finditer(text):
        span = m.group(1) or m.group(2)
        if not PATHY.search(span.strip()) or span.startswith(("http", "/", "sha256")):
            continue
        total += 1
        if resolve(span, manifest, roots, root):
            continue
        if SKIP.search(span.strip()):
            skipped += 1
            continue
        broken.append(span)
    print("путеподобных упоминаний: %d, шаблонов пропущено: %d, не найдено: %d"
          % (total, skipped, len(set(broken))))
    hard = []
    for span in sorted(set(broken)):
        print("   ?", span)
        if span.startswith(("evidence/")):
            hard.append(span)
    if hard:
        print("БИТЫЕ (начинаются с evidence/): %d" % len(hard))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
