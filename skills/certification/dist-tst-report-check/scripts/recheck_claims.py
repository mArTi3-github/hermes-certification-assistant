#!/usr/bin/env python3
"""Сверка числовых заявлений readme.txt DIST.TST с первичными артефактами.

Использование:
    python3 scripts/tst_dataset.py <каталог DIST.TST> --out <work>/dataset.json
    python3 scripts/recheck_claims.py <work> [--exclude КОМПОНЕНТ ...] [--threshold 25]

Ожидает в <work>: dataset.json и подкаталог mat/ с копией материалов.
Сверяет по каждому компоненту набора: тесты / успешные / упавшие /
пропущенные, покрытие строк (covered/total/%), ветвлений, функций — заявление
readme.txt против junit.xml и coverage-summary.csv/.json. Печатает расхождения
и компактные итоги по набору.
"""
import argparse
import json
import os
import re

ap = argparse.ArgumentParser(description='Сверка заявлений readme с артефактами')
ap.add_argument('work', nargs='?', default='.', help='каталог с dataset.json и mat/')
ap.add_argument('--exclude', action='append', default=[],
                help='компонент, не входящий в проверяемый объём (можно повторять)')
ap.add_argument('--threshold', type=float, default=25.0, help='порог покрытия в процентах')
a = ap.parse_args()
W, EXCLUDE, THRESH = a.work, set(a.exclude), a.threshold
D = json.load(open(os.path.join(W, 'dataset.json'), encoding='utf-8'))


def rd(p):
    try:
        return open(p, encoding='utf-8', errors='replace').read()
    except OSError:
        return ''


def g(pat, t, cast=float):
    """Первое число по шаблону; None, если не найдено или не число."""
    m = re.search(pat, t, re.I)
    if not m:
        return None
    try:
        return cast(m.group(1).strip().replace(' ', '').replace(',', '.'))
    except (TypeError, ValueError):
        return None


def pj(d, k):
    """Процент метрики: словарь {'pct': ..} (dataset.json вручную) или число
    (dataset.json от scripts/tst_dataset.py)."""
    v = (d or {}).get(k)
    return v.get('pct') if isinstance(v, dict) else v


def close(a, b, tol=0.05):
    return a is None or b is None or abs(float(a) - float(b)) <= tol


issues, no_claim = [], []
for m, r in D['modules'].items():
    if m in EXCLUDE:
        continue
    t = rd(os.path.join(W, 'mat', m, 'readme.txt'))
    j = r['junit'] or {}
    total = (r['cov_csv'] or {}).get('total') or {}
    base = r['baseline_covjson'] or {}
    sens = r['sensors_covjson'] or {}
    root = r['root_covjson'] or {}

    # --- заявления readme (формулировки у компонентов разные)
    c_tests = g(r'Тестов выполнено:\s*([\d\s]+)', t, int)
    c_fail = g(r'упавших\s*:\s*(\d+)', t, int)
    c_skip = g(r'пропущен\w*\s*:\s*(\d+)', t, int)   # «пропущено» и «пропущенных»
    m_lines = re.search(r'Покрытие строк\s*:\s*(\d+)\s*из\s*(\d+)\s*=\s*([\d,.]+)\s*%', t, re.I)
    c_cov = c_covd = c_covt = None
    if m_lines:
        c_covd, c_covt = int(m_lines.group(1)), int(m_lines.group(2))
        c_cov = float(m_lines.group(3).replace(',', '.'))
    else:
        c_cov = g(r'(?:Итоговое\s+)?покрытие строк[^\n]*?([\d]+[.,]\d+)\s*%', t)
    # ВАЖНО: шаблон якорится на название метрики. Общий «покрытие … %»
    # подхватывает «Покрытие ветвлений» и даёт ложное расхождение.
    c_br = g(r'Покрытие ветвлен\w*\s*:\s*([\d,.]+)\s*%', t)
    c_fn = g(r'Покрытие функций\s*:\s*([\d,.]+)\s*%', t)

    if c_tests is None:
        no_claim.append(f"{m}: «Тестов выполнено» в readme не найдено в ожидаемой формулировке")

    # --- факты
    f_tests, f_fail, f_skip = j.get('tests'), j.get('failures'), j.get('skipped')
    f_cov = total.get('pct')
    f_br = pj(base or root, 'branches') or pj(sens, 'branches')
    f_fn = pj(base or root, 'functions') or pj(sens, 'functions')

    if c_tests is not None and f_tests is not None and c_tests != f_tests:
        issues.append(f"{m}: readme «Тестов выполнено: {c_tests}» != junit <testcase>: {f_tests}")
    if c_fail is not None and f_fail is not None and c_fail != f_fail:
        issues.append(f"{m}: readme «упавших: {c_fail}» != junit <failure>: {f_fail}")
    if c_skip is not None and f_skip is not None and c_skip != f_skip:
        issues.append(f"{m}: readme «пропущено: {c_skip}» != junit <skipped>: {f_skip}")
    if not close(c_cov, f_cov):
        issues.append(f"{m}: readme строк {c_cov} % != coverage-summary.csv {f_cov} %")
    if c_covd is not None and (c_covd, c_covt) != (total.get('covered'), total.get('total')):
        issues.append(f"{m}: readme строк {c_covd}/{c_covt} != csv {total.get('covered')}/{total.get('total')}")
    if not close(c_br, f_br):
        issues.append(f"{m}: readme ветвлений {c_br} % != json {f_br} %")
    if not close(c_fn, f_fn):
        issues.append(f"{m}: readme функций {c_fn} % != json {f_fn} %")

# --- итоги по набору (агрегат по компоненту = критерий)
cov = tot = files = tests = fc = ft = 0
worst = (101.0, None)
for m, r in D['modules'].items():
    if m in EXCLUDE:
        continue
    c = (r['cov_csv'] or {}).get('total') or {}
    cov += c.get('covered') or 0
    tot += c.get('total') or 0
    files += len((r['cov_csv'] or {}).get('files') or [])
    tests += (r['junit'] or {}).get('tests') or 0
    src = r['baseline_covjson'] or r['root_covjson'] or r['sensors_covjson'] or {}
    fc += ((src.get('functions') or {}).get('covered') or 0)
    ft += ((src.get('functions') or {}).get('total') or 0)
    if c.get('pct') is not None and c['pct'] < worst[0]:
        worst = (c['pct'], m)

print('=== РАСХОЖДЕНИЯ readme <-> артефакты ===')
for i in issues or [' - нет']:
    print(' -', i)
if no_claim:
    print('\n=== ЗАЯВЛЕНИЕ НЕ РАСПОЗНАНО (проверить руками) ===')
    for i in no_claim:
        print(' -', i)
print(f"\n=== ИТОГО по набору ==={'' if not EXCLUDE else ' (не входят: ' + ', '.join(sorted(EXCLUDE)) + ')'}")
print(f'файлов {files}, тестов {tests}, строк {cov}/{tot} = {cov / tot * 100:.2f} %, '
      f'функции {fc}/{ft} = {fc / ft * 100:.2f} %')
print(f'минимум по компоненту {worst[0]:.2f} % ({worst[1]}), '
      f'запас к {THRESH:g} % = {worst[0] / THRESH:.2f}x')
