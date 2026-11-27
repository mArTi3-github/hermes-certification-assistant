#!/usr/bin/env python3
"""Независимые сверки между артефактами одного компонента DIST.TST.

Использование:
    python3 scripts/tst_dataset.py <каталог DIST.TST> --out <work>/dataset.json
    python3 scripts/crosscheck_artifacts.py <work> [--exclude КОМПОНЕНТ ...] [--threshold 25]

Проверяет по каждому компоненту набора:
  * environment.txt: дата запуска и архитектура по компонентам;
  * junit.xml: атрибуты tests по каждому набору против своих <testcase>
    (родительский набор может объявлять число вложенных наборов, а не тестов);
  * coverage-summary.csv (ИТОГО) против coverage-summary.json (lines);
  * baseline против sensors по покрытию строк;
  * файлы ниже 25 % и с нулевым покрытием.
Печатает расхождения; всё сходится — печатает «нет».
"""
import argparse
import json
import os
import re
from xml.etree import ElementTree as ET

ap = argparse.ArgumentParser(description='Независимые сверки между артефактами компонента')
ap.add_argument('work', nargs='?', default='.', help='каталог с dataset.json и mat/')
ap.add_argument('--exclude', action='append', default=[],
                help='компонент, не входящий в проверяемый объём (можно повторять)')
ap.add_argument('--threshold', type=float, default=25.0, help='порог покрытия в процентах')
a = ap.parse_args()
W, EXCLUDE, THRESH = a.work, set(a.exclude), a.threshold
# материалы могут лежать как <work>/mat/<компонент>, так и прямо в <work>
MAT = os.path.join(W, 'mat') if os.path.isdir(os.path.join(W, 'mat')) else W
D = json.load(open(os.path.join(W, 'dataset.json'), encoding='utf-8'))


def rd(p):
    try:
        return open(p, encoding='utf-8', errors='replace').read()
    except OSError:
        return ''


def lines(d):
    """(covered, total, pct) для метрики lines в любом из двух форматов dataset.json."""
    v = (d or {}).get('lines')
    if isinstance(v, dict):
        return v.get('covered'), v.get('total'), v.get('pct')
    return None, None, v


def pj(d, k):
    v = (d or {}).get(k)
    return v.get('pct') if isinstance(v, dict) else v


print(f"{'компонент':22}{'запуск':>12}{'арх':>9}{'junit т/с/п':>14}{'csv covered/total':>20}"
      f"{'base%':>8}{'sens%':>7}{'<%g' % THRESH:>5}{'=0':>4}")
bad = []
for m, r in D['modules'].items():
    if m in EXCLUDE:
        continue
    env = rd(os.path.join(MAT, m, 'results', 'environment.txt'))
    date = (re.search(r'Дата запуска:\s*(\S+)', env) or [None, '—'])[1][:10]
    arch = (re.search(r'Архитектура:\s*(\S+)', env) or [None, '—'])[1]
    j = r['junit'] or {}
    csv_t = (r['cov_csv'] or {}).get('total') or {}
    bc, sc = r['baseline_covjson'] or {}, r['sensors_covjson'] or {}

    bc_cov, bc_tot, bc_pct = lines(bc)
    sc_pct = lines(sc)[2]
    if bc_pct is not None and csv_t and (bc_cov, bc_tot) != (csv_t.get('covered'), csv_t.get('total')):
        bad.append(f"{m}: csv {csv_t.get('covered')}/{csv_t.get('total')} != json baseline {bc_cov}/{bc_tot}")
    if bc_pct is not None and sc_pct is not None and bc_pct != sc_pct:
        bad.append(f"{m}: baseline {bc_pct} % != sensors {sc_pct} %")

    # junit: атрибуты tests по каждому набору против СВОИХ <testcase>
    p = os.path.join(MAT, m, 'results', 'junit.xml')
    if os.path.exists(p):
        try:
            root = ET.parse(p).getroot()
        except ET.ParseError as e:
            bad.append(f"{m}: junit.xml не разобран ({e})")
            root = None
        if root is not None:
            for s in root.iter('testsuite'):
                own = len([c for c in s if c.tag == 'testcase'])
                kids = [x for x in s if x.tag == 'testsuite']
                names = s.get('tests')
                if names is None or not names.isdigit():
                    continue
                if kids and int(names) != own + sum(len(list(x.iter('testcase'))) for x in kids):
                    bad.append(f"{m}: набор «{s.get('name')}» tests={names}, фактически "
                               f"{own + sum(len(list(x.iter('testcase'))) for x in kids)}"
                               f" (своих {own}, вложенных наборов {len(kids)})")
                elif not kids and int(names) != own:
                    bad.append(f"{m}: набор «{s.get('name')}» tests={names}, фактически {own}")

    c = r['cov_csv'] or {}
    jc = f"{j.get('tests')}/{j.get('failures')}/{j.get('skipped')}"
    cs = f"{csv_t.get('covered')}/{csv_t.get('total')}"
    print(f"{m:22}{date:>12}{arch:>9}{jc:>14}{cs:>20}"
          f"{str(bc_pct):>8}{str(sc_pct):>7}{len(c.get('below25') or []):>5}{len(c.get('zero') or []):>4}")

print('\n=== РАСХОЖДЕНИЯ МЕЖДУ АРТЕФАКТАМИ ===')
if EXCLUDE:
    print(' (не входят в проверяемый объём:', ', '.join(sorted(EXCLUDE)) + ')')
for b in bad or [' - нет']:
    print(' -', b)
