# Публикация, когда профиль отстал по остальным скиллам

Ситуация: правка сделана в одном-двух скиллах, а профиль в целом старее `main`
(после обновлений у коллег и мержей). `contribute.sh` копирует каталог из профиля
целиком, поэтому отставшие скиллы приезжают в PR **откатом** уже смерженного.
Публиковать только затронутое — дешевле, чем выравнивать весь профиль.

## 1. Преflight: профиль — надмножество `main`, а не наоборот

Перед копированием убедиться, что из проверяемого скилла ничего не теряется:

```bash
R=~/work/hermes-certification-assistant-github; S=certification/<скилл>; H="$HERMES_HOME/skills/$S"
git -C "$R" fetch --all --prune -q
diff -u "$R/skills/$S/SKILL.md" "$H/SKILL.md" | grep '^-' | grep -v '^---' | wc -l
diff -u "$R/skills/$S/SKILL.md" "$H/SKILL.md" | grep '^-' | grep -v '^---' | cut -c1-110
```

Ожидаемая норма — единицы строк: `version:` и фразы, которые ты осознанно
переписал. Десятки удалённых строк означают, что профиль отстал по содержанию:
его нельзя копировать целиком, дельта переносится портом по строкам в `main`.

## 2. Копировать только затронутые каталоги

```bash
STAMP=$(date +%Y%m%d-%H%M); BR="contrib/<автор>/${STAMP}-<слаг>"
git -C "$R" switch main -q && git -C "$R" pull --ff-only -q && git -C "$R" switch -c "$BR" -q
rsync -a --exclude __pycache__ "$H/skills/$S/" "$R/skills/$S/"
# файлы, где репозиторий новее, оставить его версией — иначе PR везёт откат
git -C "$R" checkout origin/main -- "skills/$S/scripts/<файл>"
```

Проверить, что расхождение осмысленно: `git -C "$R" status --porcelain` — в списке
только те файлы, которые действительно менял, плюс новые файлы `references/`.

Строку в `CHANGELOG.md` при таком способе дописывают руками сразу после
`## Unreleased` в формате `- <ГГГГ-ММ-ДД> — <суть> (<git user.name>)`:
`contribute.sh` её не создаёт, потому что не запускался. Затем `tools/skill_lint.py`,
`git add -A skills CHANGELOG.md`, коммит и `git push -u origin HEAD` (у коллеги
remote `git@` — ключ уходит сам).

## 3. Проверка результата без PAT

Когда PR открывается вручную (у коллеги нет `GITHUB_TOKEN`), убедиться, что ветка
и дифф на сервере такие, как ожидалось, можно публичным API без авторизации:

```bash
O=mArTi3-github/hermes-certification-assistant; BR=<ветка>
curl -s "https://api.github.com/repos/$O/commits/$BR" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['sha'][:12], d['commit']['message'].splitlines()[0])"
curl -s "https://api.github.com/repos/$O/compare/main...$BR" | python3 -c "import sys,json;d=json.load(sys.stdin);print(d['status'], d['ahead_by']);[print(' ',f['status'],f['filename'],'+%d/-%d'%(f['additions'],f['deletions'])) for f in d['files']]"
curl -s "https://api.github.com/repos/$O/pulls?state=open" | python3 -c "import sys,json;print('открытых PR:',len(json.load(sys.stdin)))"
curl -s -o /dev/null -w 'compare: %{http_code}\n' "https://github.com/$O/compare/main...$BR?expand=1"
```

Список файлов из `compare` — доказательство для отчёта («5 файлов, +236/−8»),
а `pulls?state=open` показывает, создан ли PR (у контрибьютора он ждёт клика по
ссылке `compare`, это штатный режим, а не сбой публикации).

Ссылку с заполненными заголовком и телом собирают через `urllib.parse.quote` и
кладут файлом (`~/work/pr-<скилл>-url.txt`): с кириллицей она весит килобайты,
в чат её не выкладывают — дают короткую `…?expand=1` и блок тела для копирования.
