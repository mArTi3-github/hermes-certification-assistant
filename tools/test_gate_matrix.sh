#!/usr/bin/env bash
# Матрица состояний гейта sync.sh: каждая строка — отдельное состояние профиля и клона.
#
#   tools/test_gate_matrix.sh          # все сценарии, код 0 — все ожидания совпали
#
# Работает на копиях: клон собирается из рабочего дерева (без .git и .backup) и заново
# инициализируется, профиль — копия skills/ и distribution.yaml. Сеть не нужна, живой профиль
# и настоящий клон не трогаются. Смысл — проверить не «код запускается», а направление
# расхождения: правка в профиле останавливает обновление, отставший профиль обновляется без
# --force, и ни один сценарий не теряет правок молча.
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/gate-matrix-XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
PASS=0
FAIL=0

ok()   { PASS=$((PASS + 1)); printf '  ✔ %s\n' "$1"; }
bad()  { FAIL=$((FAIL + 1)); printf '  ✖ %s\n' "$1"; }
expect() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (ожидалось: $3, получено: $2)"; fi; }

# --- стенд ------------------------------------------------------------------
mkdir -p "$WORK/repo" "$WORK/profile/skills"
if command -v rsync >/dev/null 2>&1; then
  rsync -a --exclude .git --exclude .backup --exclude __pycache__ "$SRC/" "$WORK/repo/"
else
  (cd "$SRC" && tar -cf - --exclude=.git --exclude=.backup --exclude=__pycache__ .) | (cd "$WORK/repo" && tar -xf -)
fi
git -C "$WORK/repo" init -q -b main
git -C "$WORK/repo" -c user.name=matrix -c user.email=matrix@example.invalid add -A
git -C "$WORK/repo" -c user.name=matrix -c user.email=matrix@example.invalid commit -qm "стенд: исходное состояние"

cp -a "$WORK/repo/skills/." "$WORK/profile/skills/"
cp -a "$WORK/repo/distribution.yaml" "$WORK/profile/distribution.yaml"

REPO="$WORK/repo"
PROFILE="$WORK/profile"
PUBLISHED="$(python3 "$REPO/tools/publish_policy.py" --published | grep '^certification/' | head -1)"
[ -n "$PUBLISHED" ] || { printf 'нет публикуемых скиллов категории certification — стенд не собран\n' >&2; exit 2; }

json() { python3 "$REPO/tools/publish_check.py" --profile "$PROFILE" --json; }
field() { json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(eval(sys.argv[1]))' "$1"; }
status_of() { json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["skills"].get(sys.argv[1],{}).get("status","?"))' "$1"; }
check_rc() { set +e; PROFILE_DIR="$PROFILE" "$REPO/tools/sync.sh" --check >"$WORK/last.log" 2>&1; echo $?; set -e; }
sync_rc() { set +e; PROFILE_DIR="$PROFILE" "$REPO/tools/sync.sh" "$@" >>"$WORK/last.log" 2>&1; echo $?; set -e; }
commit_repo() { git -C "$REPO" -c user.name=matrix -c user.email=matrix@example.invalid add -A >/dev/null; git -C "$REPO" -c user.name=matrix -c user.email=matrix@example.invalid commit -qm "$1"; }
write_marker() { python3 "$REPO/tools/sync_state.py" --write --profile "$PROFILE" --repo "$REPO" >/dev/null; }

# --- A: синхронное состояние, метки нет --------------------------------------
printf '\nA. метки нет, профиль совпадает с клоном\n'
expect "скилл $PUBLISHED считается равным" "$(status_of "$PUBLISHED")" "equal"
expect "гейт не останавливается без метки, если всё равно" "$(check_rc)" "0"
expect "метки после --check нет" "$(test -f "$PROFILE/.sync-state.json" && echo да || echo нет)" "нет"
write_marker
expect "метка записана" "$(test -f "$PROFILE/.sync-state.json" && echo да || echo нет)" "да"

# --- B: репозиторий ушёл вперёд (смержен PR коллеги) ------------------------
printf '\nB. репозиторий ушёл вперёд, профиль не трогали\n'
printf '\nдобавлено репозиторием\n' >> "$REPO/skills/$PUBLISHED/SKILL.md"
commit_repo "репозиторий: правка в $PUBLISHED"
expect "состояние ↓ (профиль отстал)" "$(status_of "$PUBLISHED")" "stale"
expect "непубликованных правок нет" "$(field 'len(d["pending"])')" "0"
expect "отставших один" "$(field 'len(d["lagging"])')" "1"
expect "--check разрешает обновление" "$(check_rc)" "0"
expect "строка про обновления" "$(grep -c 'терять нечего' "$WORK/last.log")" "1"
expect "обычный прогон без --force" "$(sync_rc)" "0"
expect "правка репозитория в профиле" "$(grep -c 'добавлено репозиторием' "$PROFILE/skills/$PUBLISHED/SKILL.md")" "1"
expect "после синхронизации всё равно" "$(status_of "$PUBLISHED")" "equal"

# --- C: правка в профиле после синхронизации --------------------------------
printf '\nC. правка в профиле после синхронизации\n'
printf '\nнеопубликованная правка профиля\n' >> "$PROFILE/skills/$PUBLISHED/SKILL.md"
expect "состояние ≠ (правка в профиле)" "$(status_of "$PUBLISHED")" "diverged"
expect "--check останавливает" "$(check_rc)" "3"
expect "обычный прогон останавливает" "$(sync_rc)" "3"
expect "правка на месте после отказа" "$(grep -c 'неопубликованная правка профиля' "$PROFILE/skills/$PUBLISHED/SKILL.md")" "1"
expect "подсказка про метку" "$(grep -q 'новее метки синхронизации' "$WORK/last.log" && echo да || echo нет)" "да"
expect "--force перезаписывает" "$(sync_rc --force)" "0"
expect "правка исчезла после --force" "$(grep -c 'неопубликованная правка профиля' "$PROFILE/skills/$PUBLISHED/SKILL.md")" "0"

# --- D: правка и откат (содержимое вернулось к метке) -----------------------
printf '\nD. правка с откатом — содержимое как в метке\n'
cp -a "$PROFILE/skills/$PUBLISHED/SKILL.md" "$WORK/skill.bak"
printf '\nвременная правка\n' >> "$PROFILE/skills/$PUBLISHED/SKILL.md"
expect "пока правили — расхождение" "$(status_of "$PUBLISHED")" "diverged"
cp -a "$WORK/skill.bak" "$PROFILE/skills/$PUBLISHED/SKILL.md"
expect "содержимое совпало с клоном" "$(status_of "$PUBLISHED")" "equal"
expect "гейт не мешает" "$(check_rc)" "0"

# --- E: профиль трогали, репозиторий тоже (обе стороны) ---------------------
printf '\nE. обе стороны изменились независимо\n'
printf '\nправка профиля\n' >> "$PROFILE/skills/$PUBLISHED/SKILL.md"
printf '\nправка репозитория\n' >> "$REPO/skills/$PUBLISHED/SKILL.md"
commit_repo "репозиторий: вторая правка в $PUBLISHED"
expect "состояние ≠" "$(status_of "$PUBLISHED")" "diverged"
expect "остановка" "$(sync_rc)" "3"
expect "правка профиля цела" "$(grep -c '^правка профиля$' "$PROFILE/skills/$PUBLISHED/SKILL.md")" "1"
expect "осознанная перезапись двумя сторонами разрешается --force" "$(sync_rc --force)" "0"
expect "после --force состояние = (репозиторий выиграл)" "$(status_of "$PUBLISHED")" "equal"

# --- F: скилл появился в репозитории, в профиле его нет ---------------------
printf '\nF. скилл появился в репозитории\n'
NEW="$(printf '%s' "$PUBLISHED" | cut -d/ -f1)/gate-matrix-new"
mkdir -p "$REPO/skills/$NEW"
printf -- '---\nname: gate-matrix-new\nversion: 1.0.0\ndescription: Стенд гейта: новый скилл из репозитория.\n---\n\nТело скилла для проверки обновления.\n' > "$REPO/skills/$NEW/SKILL.md"
commit_repo "репозиторий: новый скилл $NEW"
expect "скилла нет в профиле" "$(status_of "$NEW")" "missing_in_profile"
expect "--check разрешает обновление" "$(check_rc)" "0"
expect "обычный прогон без --force" "$(sync_rc)" "0"
expect "скилл приехал в профиль" "$(test -d "$PROFILE/skills/$NEW" && echo да || echo нет)" "да"
expect "теперь синхронно" "$(status_of "$NEW")" "equal"

# --- G: скилл есть только в профиле -----------------------------------------
printf '\nG. скилл есть только в профиле (не опубликован)\n'
LOCAL="$(printf '%s' "$PUBLISHED" | cut -d/ -f1)/gate-matrix-local"
mkdir -p "$PROFILE/skills/$LOCAL"
printf -- '---\nname: gate-matrix-local\nversion: 1.0.0\ndescription: Стенд гейта: скилл только в профиле.\n---\n\nТело скилла.\n' > "$PROFILE/skills/$LOCAL/SKILL.md"
expect "состояние + (не опубликован)" "$(status_of "$LOCAL")" "not_published"
expect "остановка" "$(sync_rc)" "3"
expect "скилл в профиле цел" "$(test -d "$PROFILE/skills/$LOCAL" && echo да || echo нет)" "да"
rm -rf "$PROFILE/skills/$LOCAL"

# --- H: метки нет (профиль старше инструмента) ------------------------------
printf '\nH. метки нет, расхождение есть — консервативное решение\n'
rm -f "$PROFILE/.sync-state.json"
write_marker >/dev/null          # восстановить, затем снова снять метку после правки
printf '\nправка без метки\n' >> "$PROFILE/skills/$PUBLISHED/SKILL.md"
rm -f "$PROFILE/.sync-state.json"
expect "без метки расхождение — правка в профиле" "$(status_of "$PUBLISHED")" "diverged"
expect "остановка" "$(sync_rc)" "3"
expect "--force проходит и пишет метку" "$(sync_rc --force)" "0"
expect "метка восстановлена" "$(test -f "$PROFILE/.sync-state.json" && echo да || echo нет)" "да"
expect "после --force синхронно" "$(status_of "$PUBLISHED")" "equal"

printf '\nитого: ✔ %d, ✖ %d\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ]
