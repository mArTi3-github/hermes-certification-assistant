#!/usr/bin/env bash
# Приём обновлений из репозитория в живой профиль Hermes.
#
#   tools/sync.sh              # обновить скиллы и SOUL.md
#   tools/sync.sh --check      # только показать, что изменится (код 3 — есть непубликованное)
#   tools/sync.sh --force      # перезаписать, даже если в профиле есть неопубликованные правки
#   tools/sync.sh --config     # дополнительно перезаписать config.yaml
#   tools/sync.sh --prune      # удалить из профиля скиллы, которые политика больше не публикует
#
# Личные данные (.env, auth.json, memories, sessions, state.db) не затрагиваются.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ACTIVE_PROFILE="$(cat "$HOME/.hermes/active_profile" 2>/dev/null || echo hermes-certification-assistant)"
PROFILE_DIR="${PROFILE_DIR:-${HERMES_HOME:-$HOME/.hermes/profiles/$ACTIVE_PROFILE}}"
MANAGED="$PROFILE_DIR/skills/.distribution_managed"
COPY_CONFIG=0
PRUNE=0
CHECK_ONLY=0
FORCE=0

for arg in "$@"; do
  case "$arg" in
    --config) COPY_CONFIG=1 ;;
    --prune)  PRUNE=1 ;;
    --check)  CHECK_ONLY=1 ;;
    --force)  FORCE=1 ;;
    -h|--help) sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) printf 'неизвестный аргумент: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

[ -d "$PROFILE_DIR" ] || { printf '✖ не найден профиль: %s\n' "$PROFILE_DIR" >&2; exit 1; }
git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1 || { printf '✖ не git-репозиторий: %s\n' "$REPO_DIR" >&2; exit 1; }

echo "▪ git pull --ff-only"
if git -C "$REPO_DIR" remote get-url origin >/dev/null 2>&1; then
  git -C "$REPO_DIR" pull --ff-only
else
  echo "  remote origin не настроен — беру локальное состояние клона"
fi

NEW_LIST="$(python3 "$REPO_DIR/tools/publish_policy.py" --published)"

# --- предполётная сверка: не потерять неопубликованные правки ---------------
# Обновление копирует скиллы репозитория поверх профиля: локальная правка исчезнет.
# Поэтому сначала спрашиваем publish_check, и при неопубликованных правках останавливаемся.
set +e
python3 "$REPO_DIR/tools/publish_check.py" --profile "$PROFILE_DIR"
CHECK_RC=$?
set -e

if [ "$CHECK_ONLY" = "1" ]; then
  if [ "$CHECK_RC" = "0" ]; then
    echo ""
    echo "✔ ничего не потеряется: профиль совпадает с репозиторием"
    echo "  Запустите без --check, чтобы применить обновление."
    exit 0
  fi
  echo ""
  echo "⚠ обновление перезапишет неопубликованные правки (см. ≠ и + выше)"
  echo "  Опубликуйте их (tools/contribute.sh -m \"…\") и повторите, либо запустите sync.sh --force."
  exit 3
fi

if [ "$CHECK_RC" != "0" ] && [ "$FORCE" != "1" ]; then
  cat <<'EOF'

✖ обновление остановлено: в профиле есть неопубликованные правки.
   sync.sh копирует скиллы репозитория поверх профиля, то есть эти правки исчезнут
   (копия профиля уйдёт в .backup/pre-sync-*, но вспоминать про неё придётся вручную).

   Как поступить — выберите одно:

     1) опубликовать, потом обновить (обычный путь):
          tools/contribute.sh -m "что и зачем"
          # PR смержен → main = профиль → повторный sync.sh ничего не потеряет

     2) сохранить копию и всё же обновить:
          python3 tools/publish_check.py --save .backup/pre-update-$(date +%Y%m%d-%H%M%S)
          tools/sync.sh --force
          # вернуть правку: скопировать каталог скилла из копии обратно в профиль

     3) осознанно перезаписать правки:
          tools/sync.sh --force
EOF
  exit 3
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$REPO_DIR/.backup/pre-sync-$STAMP"
mkdir -p "$BACKUP"
echo "▪ бэкап профиля: $BACKUP"

if command -v rsync >/dev/null 2>&1; then
  rsync -a "$PROFILE_DIR/skills/" "$BACKUP/skills/" || true
  if [ -f "$PROFILE_DIR/SOUL.md" ]; then cp -a "$PROFILE_DIR/SOUL.md" "$BACKUP/SOUL.md"; fi
else
  cp -a "$PROFILE_DIR/skills/." "$BACKUP/" || true
fi

# --- удаление скиллов, которые политика перестала публиковать ---------------
if [ "$PRUNE" = "1" ] && [ -f "$MANAGED" ]; then
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    if printf '%s\n' "$NEW_LIST" | grep -qxF "$rel"; then
      continue
    fi
    if [ -d "$PROFILE_DIR/skills/$rel" ]; then
      mkdir -p "$BACKUP/removed"
      mv "$PROFILE_DIR/skills/$rel" "$BACKUP/removed/"
      echo "  − убран из профиля: $rel (копия в бэкапе)"
    fi
  done < "$MANAGED"
fi

# --- копирование актуальных скиллов ---------------------------------------
if command -v rsync >/dev/null 2>&1; then
  rsync -a "$REPO_DIR/skills/" "$PROFILE_DIR/skills/"
else
  cp -a "$REPO_DIR/skills/." "$PROFILE_DIR/skills/"
fi
if [ -f "$REPO_DIR/SOUL.md" ]; then cp -a "$REPO_DIR/SOUL.md" "$PROFILE_DIR/SOUL.md"; fi

if [ "$COPY_CONFIG" = "1" ] && [ -f "$REPO_DIR/config.yaml" ]; then
  cp -a "$PROFILE_DIR/config.yaml" "$BACKUP/config.yaml" 2>/dev/null || true
  cp -a "$REPO_DIR/config.yaml" "$PROFILE_DIR/config.yaml"
  echo "▪ config.yaml перезаписан (локальные правки лежат в бэкапе)"
fi

printf '%s\n' "$NEW_LIST" > "$MANAGED"

echo "▪ версия дистрибутива: $(grep -m1 '^version:' "$REPO_DIR/distribution.yaml" | awk '{print $2}')"
echo "✔ профиль обновлён: $PROFILE_DIR"
echo "  Новые скиллы подхватятся в следующей сессии Hermes (перезапуск не нужен)."
