# Перекрёстные проверки отчёта

Срезы ниже находят настоящие дефекты. Выполнять по данным, прочитанным
`scripts/src_dep_probe.py` (в сниппетах `data` — список пар `(номер строки, ячейки)`, индексы
колонок соответствуют порядку `name, version, purl, component, id, severity, status, detail`;
при другой шапке брать индексы из неё).

## Дубли и конфликт статусов на паре (компонент, id)

Один и тот же компонент законно встречается в разных модулях и контейнерах продукта, поэтому
разные обоснования у одной пары — норма, а не дефект: `dup` и `pure` только для сведения,
их не показывают человеку как находку. Замечанием остаётся **конфликт статусов**: одна пара
не может быть одновременно `false_positive` и `not_affected`.

```python
import collections
g = collections.defaultdict(list)
for n, r in data:
    g[(r[3], r[4])].append((n, r[6].lower(), (r[7] or '').strip()))
conflicts = {k: v for k, v in g.items() if len({s for _, s, _ in v}) > 1}       # разные статусы
```

## Версия внутри обоснования против версии компонента

```python
import re, urllib.parse
def comp_name_version(purl):
    body = (purl or '').split('?')[0].rsplit('/', 1)[-1]
    name, _, ver = body.partition('@')
    return urllib.parse.unquote(name), urllib.parse.unquote(ver)

for n, r in data:
    m = re.search(r'([\w.\-+/]+?)@([0-9][\w.\-+~:%]*)', (r[7] or ''))
    if not m:
        continue
    name, _, ver = comp_name_version(r[3])
    if m.group(1).split('/')[-1].lower() == name.lower() and m.group(2) != ver:
        print(f'строка {n}: в тексте {m.group(2)}, компонент {ver}')
```

Находка означает, что обоснование скопировано с другой версии и к этой строке не относится.

## Статус против смысла обоснования

```python
import re
reach = re.compile(r'(не\s+использ|не\s+задейств|не\s+вызыва|недостижим|отсутству)', re.I)
proof = re.compile(r'(исправлен|другой\s+продукт|относится\s+к)', re.I)
for n, r in data:
    det, st = (r[7] or ''), (r[6] or '').lower()
    if reach.search(det) and st == 'false_positive':
        print(f'строка {n}: статус fp, обоснование про недостижимость — кандидат на na')
    if proof.search(det) and st == 'not_affected':
        print(f'строка {n}: статус na, обоснование про версию/другой продукт — кандидат на fp')
```

Это подсказка для разбора, а не вердикт: статус ставится по правилу из
`references/vex-status-fp-na.md`, а спорное согласуется с человеком.

## Орфография, пунктуация и формат

```python
import re
CHECKS = {
    'пробел перед знаком': r'\s,',
    'двойной пробел': r'  +',
    'приклеенный уровень опасности': r'\s(high|critical|medium|low)\s*$',
}
for label, pat in CHECKS.items():
    rows = [n for n, r in data if re.search(pat, r[7] or '', re.I)]
    print(label, len(rows), rows[:10])
```

Отдельные опечатки ищутся по списку слов конкретного отчёта — их быстрее найти глазами по
уникальным текстам. Правило правки — в SKILL.md: орфография и формат без согласования,
содержание — только через человека.
