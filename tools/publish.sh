#!/usr/bin/env bash
# Публикация правок профиля одной командой.
#
#   tools/publish.sh -m "dist-tst-report-check: уточнил шаг 0"          # участник: PR и стоп
#   tools/publish.sh -m "profile-contribution: правило ревью" --accept  # владелец: PR + мерж + релиз
#   tools/publish.sh -m "..." --accept --bump patch                     # тип релиза
#   tools/publish.sh -m "..." --dry-run                                 # холостой прогон: план и линт
#
# Шаги: contribute.sh (fetch, слияние с main по трём версиям, линт, ветка, PR), затем при
# --accept — accept.py (зелёный CI, squash-мерж, один релиз, sync, publish_check).
# Мержа без --accept не бывает: участник останавливается на PR.
# --dry-run ничего не публикует: fetch, publish_plan.py и skill_lint.py по копии опубликованного
# набора скиллов профиля — без копирования в клон, коммита, ветки и PR; с --accept несовместим.
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
  sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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

if [ "$DRY_RUN" -eq 1 ] && [ "$ACCEPT" -eq 1 ]; then
  die "--dry-run несовместим с --accept: холостой прогон ничего не публикует, мержить в нём нечего"
fi

# Холостой прогон: план публикации и линт. Клон не меняется, на сервере ничего не появляется.
if [ "$DRY_RUN" -eq 1 ]; then
  say "=== холостой прогон: план и линт, публикации не будет"
  say "  изменение: $MESSAGE"
  say ""
  say "▪ клон: $(git -C "$REPO_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null) @ $(git -C "$REPO_DIR" rev-parse --short HEAD 2>/dev/null)"
  git -C "$REPO_DIR" fetch -q origin || say "  ⚠ git fetch не удался — план считается по локальному состоянию клона"
  if [ -n "$(git -C "$REPO_DIR" status --porcelain 2>/dev/null)" ]; then
    say "  ⚠ в клоне есть незакоммиченное: настоящий прогон уберёт его в stash"
  fi
  say ""
  say "▪ план публикации (база — метка синхронизации профиля):"
  PLAN_RC=0
  python3 "$REPO_DIR/tools/publish_plan.py" --profile "$PROFILE_DIR" --repo "$REPO_DIR" || PLAN_RC=$?

  # Линт идёт по копии опубликованного набора скиллов из профиля: настоящий прогон копирует
  # ровно это дерево в клон и линтует его, а линт по клону проверил бы апстрим, а не публикацию.
  LINT_DIR="$(mktemp -d)"
  trap 'rm -rf "$LINT_DIR"' EXIT
  cp -a "$REPO_DIR/tools" "$LINT_DIR/tools"
  for extra in publish.list distribution.yaml config.yaml; do
    if [ -f "$REPO_DIR/$extra" ]; then
      cp -a "$REPO_DIR/$extra" "$LINT_DIR/$extra"
    fi
  done
  mkdir -p "$LINT_DIR/skills"
  COPIED=0
  while IFS= read -r rel; do
    [ -n "$rel" ] || continue
    if [ -d "$PROFILE_DIR/skills/$rel" ]; then
      mkdir -p "$LINT_DIR/skills/$(dirname "$rel")"
      cp -a "$PROFILE_DIR/skills/$rel" "$LINT_DIR/skills/$rel"
      COPIED=$((COPIED + 1))
    else
      say "  ⚠ скилла $rel нет в профиле — в линт не попал"
    fi
  done < <(python3 "$REPO_DIR/tools/publish_policy.py" --published --profile "$PROFILE_DIR")

  say ""
  say "▪ линт опубликованного набора из профиля (скиллов $COPIED, копия в $LINT_DIR, клон не тронут):"
  LINT_RC=0
  python3 "$LINT_DIR/tools/skill_lint.py" || LINT_RC=$?

  say ""
  say "дальше в настоящем прогоне: ветка contrib/<автор>/<штамп>-<слаг>, строка в CHANGELOG.md, коммит,"
  say "push и PR, а с --accept — ожидание CI, squash-мерж, релиз и sync.sh."
  say "холостой прогон ничего не изменил: скиллы в клон не копировались, ветки и PR нет."
  if [ "$PLAN_RC" -eq 0 ] && [ "$LINT_RC" -eq 0 ]; then
    exit 0
  fi
  exit 1
fi

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
python3 "$REPO_DIR/tools/accept.py" "$PR_NUMBER" --bump "$BUMP"
