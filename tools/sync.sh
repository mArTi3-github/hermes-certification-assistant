#!/usr/bin/env bash
# Приём обновлений из репозитория в живой профиль Hermes.
#
#   tools/sync.sh              # обновить скиллы и SOUL.md
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

for arg in "$@"; do
  case "$arg" in
    --config) COPY_CONFIG=1 ;;
    --prune)  PRUNE=1 ;;
    -h|--help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
