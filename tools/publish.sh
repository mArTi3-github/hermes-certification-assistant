#!/usr/bin/env bash
# Публикация правок профиля одной командой.
#
#   tools/publish.sh -m "dist-tst-report-check: уточнил шаг 0"          # участник: PR и стоп
#   tools/publish.sh -m "profile-contribution: правило ревью" --accept  # владелец: PR + мерж + релиз
#   tools/publish.sh -m "..." --accept --bump patch                     # тип релиза
#
# Шаги: contribute.sh (fetch, слияние с main по трём версиям, линт, ветка, PR), затем при
# --accept — accept.py (зелёный CI, squash-мерж, один релиз, sync, publish_check).
# Мержа без --accept не бывает: участник останавливается на PR.
set -euo pipefail

REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ACTIVE_PROFILE="$(cat "$HOME/.hermes/active_profile" 2>/dev/null || echo hermes-certification-assistant)"
PROFILE_DIR="${PROFILE_DIR:-${HERMES_HOME:-$HOME/.hermes/profiles/$ACTIVE_PROFILE}}"
MESSAGE=""
ACCEPT=0
BUMP="minor"
DRY_RUN=0

die() { printf '\n✖ %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message)  MESSAGE="${2:-}"; shift 2 ;;
    --accept)      ACCEPT=1; shift ;;
    --dry-run)     DRY_RUN=1; shift ;;
    --bump)        BUMP="${2:-minor}"; shift 2 ;;
    -p|--profile)  PROFILE_DIR="${2:-}"; shift 2 ;;
    -h|--help)     usage ;;
    *) die "неизвестный аргумент: $1 (см. --help)" ;;
  esac
done

[ -n "$MESSAGE" ] || die "нужно описание изменения: -m \"что и зачем\""
case "$BUMP" in major|minor|patch) ;; *) die "--bump major|minor|patch, получено: $BUMP" ;; esac

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

say "=== публикация: $MESSAGE"
RC=0
REPO_DIR="$REPO_DIR" PROFILE_DIR="$PROFILE_DIR" \
  "$REPO_DIR/tools/contribute.sh" -m "$MESSAGE" | tee "$LOG" || RC=$?
if [ "$RC" -ne 0 ]; then
  die "contribute.sh завершился с кодом $RC — смотрите вывод выше"
fi

PR_URL="$(sed -n 's/^PR_URL=//p' "$LOG" | tail -1)"
PR_NUMBER="$(printf '%s' "$PR_URL" | sed -n 's#.*/pull/\([0-9]\+\).*#\1#p')"

if [ -z "$PR_NUMBER" ]; then
  say ""
  say "PR не создан (нет токена или API отказал) — дальше идти некуда:"
  sed -n 's/^  Откройте PR вручную: //p' "$LOG" | tail -1
  exit 0
fi

if [ "$ACCEPT" -eq 0 ]; then
  say ""
  say "✔ PR #$PR_NUMBER готов: $PR_URL"
  say "  Ревью владельцем: tools/accept.py $PR_NUMBER   (или tools/accept.py — все открытые)"
  exit 0
fi

say ""
say "=== приём PR #$PR_NUMBER"
ARGS=("$PR_NUMBER" --bump "$BUMP")
[ "$DRY_RUN" -eq 1 ] && ARGS+=(--dry-run)
python3 "$REPO_DIR/tools/accept.py" "${ARGS[@]}"
