#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Профиль коллекции входных данных (corpus/) фаззера.

Отвечает на вопрос пункта 2 Методики ВУ: разнообразна ли коллекция по существу,
или это мусор от мутаций. Считает размеры, версии формата и распределение
структурных полей; с --sflow разбирает заголовки sFlow v4/v5.

Использование:
    corpus_profile.py <corpus_dir> [--sflow] [--top 15]
"""
import argparse
import collections
import glob
import os
import struct
import sys


def human(n):
    for u in ("Б", "КБ", "МБ"):
        if n < 1024 or u == "МБ":
            return f"{n:.0f} {u}" if u != "Б" else f"{n} Б"
        n /= 1024.0


def sflow_header(data):
    """(версия, тип адреса агента, num_samples) или None, если не заголовок sFlow."""
    if len(data) < 8:
        return None
    ver = struct.unpack(">I", data[0:4])[0]
    atype = struct.unpack(">I", data[4:8])[0]
    alen = {1: 4, 2: 16}.get(atype)
    if alen is None or len(data) < 8 + alen + 16:
        return None
    ns = struct.unpack(">I", data[8 + alen + 12: 8 + alen + 16])[0]
    return ver, atype, ns


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("corpus_dir")
    ap.add_argument("--sflow", action="store_true", help="разбирать заголовки sFlow v4/v5")
    ap.add_argument("--top", type=int, default=15)
    a = ap.parse_args()

    files = [f for f in glob.glob(os.path.join(a.corpus_dir, "*")) if os.path.isfile(f)]
    if not files:
        sys.exit(f"в {a.corpus_dir} нет файлов")

    sizes = sorted(os.path.getsize(f) for f in files)
    print(f"Файлов: {len(files)}; размеры: min {human(sizes[0])}, "
          f"медиана {human(sizes[len(sizes) // 2])}, max {human(sizes[-1])}; "
          f"уникальных размеров: {len(set(sizes))}")
    hist = collections.Counter()
    for s in sizes:
        edge = 0 if s <= 64 else (1 if s <= 512 else (2 if s <= 4096 else (3 if s <= 65536 else 4)))
        hist[edge] += 1
    names = {0: "≤ 64 Б", 1: "65–512 Б", 2: "513 Б – 4 КБ", 3: "4–64 КБ", 4: "> 64 КБ"}
    print("Распределение по размеру: " + ", ".join(f"{names[k]}: {hist[k]}" for k in sorted(hist)))

    if not a.sflow:
        return

    vers = collections.Counter()
    atypes = collections.Counter()
    nsamp = collections.Counter()
    bad = 0
    for f in files:
        try:
            data = open(f, "rb").read()
        except OSError:
            bad += 1
            continue
        h = sflow_header(data)
        if h is None:
            bad += 1
            continue
        v, at, ns = h
        vers[v] += 1
        atypes[at] += 1
        nsamp[ns] += 1

    print(f"\nsFlow: распознано {len(files) - bad} из {len(files)}, "
          f"нераспознанных/битых {bad} (битые — норма для корпуса фаззера)")
    print("Версии датаграммы: " + ", ".join(f"v{v}: {n}" for v, n in vers.most_common(6)))
    print("Тип адреса агента (1=IPv4, 2=IPv6): " + ", ".join(
        f"{k}: {n}" for k, n in atypes.most_common(6)))
    print("num_samples (верхушка): " + ", ".join(
        f"{k}: {n}" for k, n in nsamp.most_common(a.top)))
    if nsamp and max(nsamp) > 10000:
        print(f"! есть датаграммы с num_samples до {max(nsamp)} — кандидаты на зависание "
              f"из-за неконтролируемого обхода заявленного числа записей")
    if len(vers) <= 2 and 0 not in vers:
        print("! в корпусе всего одна-две версии формата: проверить, не вырождена ли "
              "функциональная область (пункт 2 требует разные возможности интерфейса)")


if __name__ == "__main__":
    main()
