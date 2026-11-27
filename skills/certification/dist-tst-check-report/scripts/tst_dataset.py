#!/usr/bin/env python3
"""Сбор первичных данных DIST.TST в dataset.json + сводная таблица.

Разбор без доверия readme.txt: JUnit по элементам <testcase>, покрытие из
coverage-summary.csv и coverage-summary.json, среда из environment.txt,
происхождение кода из source.txt / tarball-vs-tag.txt / built-vs-published.txt /
embedded-vs-source.txt, неизменность — git-status-* и source-files*.sha256.

Использование:
    tst_dataset.py <каталог DIST.TST> [--report] [--out dataset.json]
"""
import csv, json, os, re, sys, glob
from xml.etree import ElementTree as ET


def rd(p):
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            return f.read()
    except OSError:
        return ''


def junit(path):
    """Число тестов — по элементам <testcase>; атрибуты tests у вложенных suites
    дают двойной счёт и иногда ошибаются на единицу."""
    if not os.path.exists(path):
        return None
    try:
        r = ET.parse(path).getroot()
    except ET.ParseError as e:
        return {'error': str(e)}
    cases = list(r.iter('testcase'))
    # атрибуты tests считаем только по верхнему уровню: вложенные suites входят
    # в атрибут родителя и при суммировании дают двойной счёт
    top = [r] if r.tag == 'testsuite' else [c for c in r if c.tag == 'testsuite']
    suites = top or list(r.iter('testsuite'))
    return {'tests': len(cases),
            'failures': sum(1 for c in cases if c.find('failure') is not None),
            'errors': sum(1 for c in cases if c.find('error') is not None),
            'skipped': sum(1 for c in cases if c.find('skipped') is not None),
            'suites': len(suites),
            'declared_tests': sum(int(s.get('tests')) for s in suites
                                  if (s.get('tests') or '').isdigit())}


def cov_csv(path):
    if not os.path.exists(path):
        return None
    rows = []
    for row in csv.reader(open(path, encoding='utf-8'), delimiter=';'):
        if len(row) < 4 or row[0] == 'Файл':
            continue
        try:
            rows.append({'file': row[0], 'covered': int(row[1]), 'total': int(row[2]),
                         'pct': float(row[3].replace(',', '.'))})
        except ValueError:
            continue
    files = [r for r in rows if r['file'] != 'ИТОГО']
    return {'files': files,
            'total': next((r for r in rows if r['file'] == 'ИТОГО'), None),
            'min_pct': min((f['pct'] for f in files), default=None),
            'below25': [f['file'] for f in files if f['pct'] < 25.0]}


def cov_json(path):
    if not os.path.exists(path):
        return None
    try:
        d = json.load(open(path, encoding='utf-8'))
    except Exception as e:
        return {'error': str(e)}
    t = d.get('total') or {}
    out: dict = {'n_files': len(d) - (1 if t else 0)}
    for k in ('statements', 'branches', 'functions', 'lines'):
        v = t.get(k) or {}
        out[k] = {'pct': v.get('pct'), 'covered': v.get('covered'), 'total': v.get('total')}
    return out


def discover(root):
    mods = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if not os.path.isdir(d) or name.startswith('_'):
            continue
        if os.path.isdir(os.path.join(d, 'results')):
            mods.append(name)
    return mods


def main():
    root = sys.argv[1]
    rep = '--report' in sys.argv
    out = os.path.join(os.path.dirname(os.path.abspath(root)), 'dataset.json')
    if '--out' in sys.argv:
        out = sys.argv[sys.argv.index('--out') + 1]
    data = {'root': os.path.abspath(root), 'modules': {}}
    for m in discover(root):
        res = os.path.join(root, m, 'results')
        env = rd(os.path.join(res, 'environment.txt'))
        rec = {
            'arch': (re.search(r'Архитектура:\s*(\S+)', env) or [None, None])[1],
            'os': (re.search(r'ОС:\s*(.+)', env) or [None, None])[1],
            'date': (re.search(r'Дата запуска:\s*(\S+)', env) or [None, None])[1],
            'attempt_dirs': sorted(os.path.basename(x) for x in glob.glob(os.path.join(root, m, '_*'))),
            'source': rd(os.path.join(res, 'source.txt')) or rd(os.path.join(res, 'host-git.txt')),
            'provenance': rd(os.path.join(res, 'tarball-vs-tag.txt'))
                          or rd(os.path.join(res, 'source-unchanged.txt')),
            'git_before': rd(os.path.join(res, 'git-status-before.txt')).strip(),
            'git_after': rd(os.path.join(res, 'git-status-after.txt')).strip(),
            'junit': junit(os.path.join(res, 'junit.xml')),
            'cov_csv': cov_csv(os.path.join(res, 'coverage-summary.csv')),
        }
        for sub in ('sensors', 'baseline'):
            rec[sub + '_covjson'] = cov_json(os.path.join(res, sub, 'coverage', 'coverage-summary.json'))
            rec[sub + '_exit'] = rd(os.path.join(res, sub, 'exit-code.txt')).strip() or None
        rec['root_covjson'] = cov_json(os.path.join(res, 'coverage', 'coverage-summary.json'))
        for f in ('test-summary.txt', 'failures.csv', 'coverage-below-threshold.txt',
                  'embedded-vs-source.txt', 'built-vs-published.txt', 'duration.txt'):
            p = os.path.join(res, f)
            if os.path.exists(p):
                rec[f] = rd(p)
        data['modules'][m] = rec
    json.dump(data, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('dataset:', out)

    hdr = f"{'компонент':28}{'тестов':>8}{'сбои':>6}{'проп':>6}{'покр.%':>8}{'мин%':>7}{'<25':>5}{'функц.%':>9}  среда"
    print(hdr)
    for m, r in data['modules'].items():
        j = r['junit'] or {}
        c = r['cov_csv'] or {}
        t = c.get('total') or {}
        fj = r['sensors_covjson'] or r['root_covjson'] or {}
        print(f"{m:28}{j.get('tests') or 0:>8}{j.get('failures') or 0:>6}{j.get('skipped') or 0:>6}"
              f"{str(t.get('pct')):>8}{str(c.get('min_pct')):>7}{len(c.get('below25') or []):>5}"
              f"{str((fj.get('functions') or {}).get('pct')):>9}  {r['arch']} {r['date']}")
        if rep and j.get('declared_tests') is not None and j.get('declared_tests') != j.get('tests'):
            print(f"   ! атрибут tests={j['declared_tests']} при {j['tests']} <testcase>")
        if rep and r['provenance']:
            first = r['provenance'].strip().split('\n')[-1][:110]
            print(f"   провенанс: {first}")


if __name__ == '__main__':
    main()
