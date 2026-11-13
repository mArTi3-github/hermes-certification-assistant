#!/usr/bin/env bash
# Публикация правок профиля: копирует скиллы из живого профиля в клон репозитория,
# прогоняет линт, коммитит в отдельную ветку и (если есть GITHUB_TOKEN) открывает PR.
#
#   tools/contribute.sh -m "certification-stand-ftest: уточнил проверку блокировок"
#   tools/contribute.sh -i security/my-new-skill -m "новый скилл: ..."
#
# Переменные окружения:
#   PROFILE_DIR  — каталог профиля (по умолчанию $HERMES_HOME или профиль по умолчанию)
#   OWNER_REPO   — owner/repo на GitHub
#   GITHUB_TOKEN — PAT для авто-создания PR (необязательно; можно положить в ~/.hermes/.env)
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OWNER_REPO="${OWNER_REPO:-mArTi3-github/hermes-certification-assistant}"
PROFILE_DIR="${PROFILE_DIR:-${HERMES_HOME:-$HOME/.hermes/profiles/hermes-certification-assistant}}"
BRANCH_PREFIX="${BRANCH_PREFIX:-contrib}"
MESSAGE=""
BRANCH=""
INCLUDE=""
BASE="${BASE:-main}"

die() { printf '\n✖ %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    -m|--message) MESSAGE="${2:-}"; shift 2 ;;
    -b|--branch)  BRANCH="${2:-}";  shift 2 ;;
    -p|--profile) PROFILE_DIR="${2:-}"; shift 2 ;;
    -i|--include) INCLUDE="${2:-}"; shift 2 ;;
    -h|--help)    usage ;;
    *) die "неизвестный аргумент: $1 (см. --help)" ;;
  esac
done

[ -n "$MESSAGE" ] || die "нужно описание изменения: -m \"что и зачем\""
[ -d "$PROFILE_DIR/skills" ] || die "не найден профиль: $PROFILE_DIR"
git -C "$REPO_DIR" rev-parse --git-dir >/dev/null 2>&1 || die "не git-репозиторий: $REPO_DIR"

if [ -n "$INCLUDE" ]; then
  [ -f "$PROFILE_DIR/skills/$INCLUDE/SKILL.md" ] || die "в профиле нет скилла: $INCLUDE"
  python3 "$REPO_DIR/tools/publish_policy.py" --include "$INCLUDE"
fi

token_from_file() {
  [ -f "$1" ] || return 0
  sed -n 's/^GITHUB_TOKEN=//p' "$1" 2>/dev/null | head -1 | tr -d '"' | tr -d "'" || true
}

if [ -z "${GITHUB_TOKEN:-}" ]; then
  for candidate in "$HOME/.hermes/.env" "${HERMES_HOME:+$HERMES_HOME/.env}" "$PROFILE_DIR/.env"; do
    GITHUB_TOKEN="$(token_from_file "$candidate")"
    [ -n "$GITHUB_TOKEN" ] && break
  done
fi

copy_tree() { # copy_tree SRC DST
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete "$1/" "$2/"
  else
    mkdir -p "$2"; cp -a "$1/." "$2/"
  fi
}

# --- бэкап текущего состояния скиллов профиля -------------------------------
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$REPO_DIR/.backup/$STAMP/skills"
mkdir -p "$BACKUP"
if command -v rsync >/dev/null 2>&1; then
  rsync -a "$PROFILE_DIR/skills/" "$BACKUP/" || true
else
  cp -a "$PROFILE_DIR/skills/." "$BACKUP/" || true
fi
say "▪ бэкап скиллов профиля: $BACKUP"

# --- сбор набора скиллов по политике publish.list --------------------------
# Публикуется ровно то, что перечислено в [publish]; всё прочее остаётся в профиле.
SYNCED=0
while IFS= read -r rel; do
  [ -n "$rel" ] || continue
  if [ -d "$PROFILE_DIR/skills/$rel" ]; then
    copy_tree "$PROFILE_DIR/skills/$rel" "$REPO_DIR/skills/$rel"
    say "  ↻ $rel"
    SYNCED=$((SYNCED + 1))
  else
    say "  · $rel — в профиле отсутствует, в репозитории остаётся текущая версия"
  fi
done < <(python3 "$REPO_DIR/tools/publish_policy.py" --published --profile "$PROFILE_DIR")

UNCLASSIFIED="$(python3 "$REPO_DIR/tools/publish_policy.py" --profile "$PROFILE_DIR" --unclassified 2>/dev/null || true)"
if [ -n "$UNCLASSIFIED" ]; then
  say ""
  say "⚠ скиллы вне политики публикации:"
  printf '%s\n' "$UNCLASSIFIED" | sed 's/^/    /'

  if [ -t 0 ] && [ -r /dev/tty ]; then
    say ""
    say "  [c] — процедура сертификации: перенести в certification/ и опубликовать"
    say "  [t] — служебный инструмент: перенести в certification/tools/ и опубликовать"
    say "  [l] — оставить локально (записать в [private])"
    say "  [Enter] — пропустить, решить позже"
    while IFS= read -r rel; do
      [ -n "$rel" ] || continue
      name="${rel##*/}"
      printf '  %s → [c/t/l/Enter]: ' "$rel"
      answer=""
      read -r answer < /dev/tty || answer=""
      case "${answer:-}" in
        c|C|с|С)
          subdir="certification"
          ;;
        t|T)
          subdir="certification/tools"
          ;;
        l|L|л|Л)
          python3 "$REPO_DIR/tools/publish_policy.py" --classify "$rel" private >/dev/null
          say "    ▸ отмечено локальным ([private])"
          continue
          ;;
        *)
          say "    · пропущено: останется вне политики, пока не решите"
          continue
          ;;
      esac
      mkdir -p "$PROFILE_DIR/skills/$subdir"
      if [ -e "$PROFILE_DIR/skills/$subdir/$name" ]; then
        say "    ✖ $subdir/$name уже существует — пропущено"
        continue
      fi
      mv "$PROFILE_DIR/skills/$rel" "$PROFILE_DIR/skills/$subdir/$name"
      copy_tree "$PROFILE_DIR/skills/$subdir/$name" "$REPO_DIR/skills/$subdir/$name"
      say "    ▸ перенесено в $subdir/$name и добавлено в публикацию"
      SYNCED=$((SYNCED + 1))
    done <<< "$UNCLASSIFIED"
  else
    say "  неинтерактивный режим: спросите пользователя, затем"
    say "  tools/publish_policy.py --classify <категория/имя> publish|private"
  fi
fi

[ "$SYNCED" -gt 0 ] || die "нечего публиковать: в профиле нет изменённых скиллов"

# --- линт -------------------------------------------------------------------
say ""
say "▪ линт:"
if ! python3 "$REPO_DIR/tools/skill_lint.py"; then
  git -C "$REPO_DIR" reset -q
  git -C "$REPO_DIR" checkout -q -- skills CHANGELOG.md 2>/dev/null || true
  die "линт не прошёл — правки откатаны в клоне; исходник цел в профиле (бэкап: $BACKUP)"
fi

# --- есть ли что публиковать ----------------------------------------------
git -C "$REPO_DIR" add -A skills
if git -C "$REPO_DIR" diff --cached --quiet; then
  git -C "$REPO_DIR" reset -q
  die "изменений нет: содержимое скиллов в профиле совпадает с репозиторием"
fi

# --- ветка, коммит, CHANGELOG ----------------------------------------------
if [ -z "$BRANCH" ]; then
  author="$(git -C "$REPO_DIR" config user.name || true)"
  author_slug="$(printf '%s' "$author" | tr -c 'A-Za-z0-9_-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//' | cut -c1-20)"
  [ -n "$author_slug" ] || author_slug="contributor"
  slug="$(printf '%s' "$MESSAGE" | tr -cs '[:alnum:]-' '-' | cut -c1-40 | sed 's/^-//; s/-$//')"
  BRANCH="$BRANCH_PREFIX/${author_slug}/${STAMP}-${slug:-change}"
fi

git -C "$REPO_DIR" switch -c "$BRANCH" >/dev/null 2>&1 \
  || git -C "$REPO_DIR" switch "$BRANCH" >/dev/null

AUTHOR="$(git -C "$REPO_DIR" config user.name || echo unknown)"
python3 - "$REPO_DIR/CHANGELOG.md" "$MESSAGE" "$AUTHOR" <<'PY'
import sys
from datetime import date
from pathlib import Path

path, message, author = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
entry = f"- {date.today().isoformat()} — {message} ({author})"

if path.exists():
    lines = path.read_text(encoding="utf-8").splitlines()
else:
    lines = ["# Журнал изменений", "", "## Unreleased", ""]

heading = next((i for i, line in enumerate(lines) if line.strip() == "## Unreleased"), None)
if heading is None:
    first_section = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    block = ["## Unreleased", "", entry, ""]
    lines = lines + [""] + block if first_section is None else lines[:first_section] + block + lines[first_section:]
else:
    insert_at = heading + 1
    if insert_at < len(lines) and lines[insert_at].strip() == "":
        insert_at += 1
    lines.insert(insert_at, entry)
    if insert_at + 1 >= len(lines) or lines[insert_at + 1].strip() != "":
        lines.insert(insert_at + 1, "")

path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY

git -C "$REPO_DIR" add -A skills CHANGELOG.md
if git -C "$REPO_DIR" diff --cached --quiet; then
  die "изменений нет после подготовки коммита"
fi
git -C "$REPO_DIR" commit -q -m "$MESSAGE"
say ""
say "▪ коммит: $(git -C "$REPO_DIR" rev-parse --short HEAD) в ветке $BRANCH"

# --- push -------------------------------------------------------------------
REMOTE_URL="$(git -C "$REPO_DIR" remote get-url origin 2>/dev/null || true)"
if [ -z "$REMOTE_URL" ]; then
  say ""
  say "⚠ remote origin не настроен — push пропущен."
  say "  Сначала: git -C \"$REPO_DIR\" remote add origin git@github.com:$OWNER_REPO.git"
  say "  Затем:   git -C \"$REPO_DIR\" push -u origin $BRANCH"
  exit 0
fi

if [ -n "${GITHUB_TOKEN:-}" ] && printf '%s' "$REMOTE_URL" | grep -q '^https://'; then
  # push по HTTPS: токен подставляется через GIT_ASKPASS, в .git/config и в argv он не попадает
  if [ -z "${GITHUB_USER:-}" ]; then
    GITHUB_USER="$(curl -sS -H "Authorization: Bearer $GITHUB_TOKEN" \
      -H "Accept: application/vnd.github+json" https://api.github.com/user 2>/dev/null \
      | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("login", "") or "")
except Exception:
    print("")')"
  fi
  [ -n "${GITHUB_USER:-}" ] || GITHUB_USER="git"
  export GITHUB_USER GITHUB_TOKEN

  ASKPASS="$(mktemp)"
  cat > "$ASKPASS" <<'ASKPASS_EOF'
#!/bin/sh
case "$1" in
  *[Uu]sername*) printf '%s\n' "${GITHUB_USER:-git}" ;;
  *)             printf '%s\n' "${GITHUB_TOKEN}" ;;
esac
ASKPASS_EOF
  chmod 700 "$ASKPASS"

  say "▪ push по HTTPS с токеном (пользователь: $GITHUB_USER)"
  if ! GIT_ASKPASS="$ASKPASS" GIT_TERMINAL_PROMPT=0 git -C "$REPO_DIR" push -u origin "$BRANCH"; then
    rm -f "$ASKPASS"
    die "push не удался: проверьте права токена (Contents: Read and write) и что репозиторий уже создан"
  fi
  rm -f "$ASKPASS"
else
  GIT_TERMINAL_PROMPT=0 git -C "$REPO_DIR" push -u origin "$BRANCH"
fi
say "▪ ветка отправлена: $BRANCH"

# --- pull request -----------------------------------------------------------
COMPARE_URL="https://github.com/$OWNER_REPO/compare/$BASE...$BRANCH?expand=1"

if [ -z "${GITHUB_TOKEN:-}" ]; then
  say ""
  say "PR можно открыть в браузере одной кнопкой:"
  say "  $COMPARE_URL"
  say "Для автоматического создания PR добавьте PAT в ~/.hermes/.env как GITHUB_TOKEN."
  exit 0
fi

payload="$(python3 -c 'import json,sys; print(json.dumps({"title":sys.argv[1],"head":sys.argv[2],"base":sys.argv[3]}))' "$MESSAGE" "$BRANCH" "$BASE")"
response="$(curl -sS -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -d "$payload" \
  "https://api.github.com/repos/$OWNER_REPO/pulls")"

pr_url="$(printf '%s' "$response" | python3 -c 'import json,sys
try:
    data = json.load(sys.stdin)
except Exception:
    print("")
else:
    print(data.get("html_url") or data.get("message", ""))')"

if [ -n "$pr_url" ] && printf '%s' "$pr_url" | grep -q '^https://'; then
  say ""
  say "▪ PR создан: $pr_url"
else
  say ""
  say "⚠ GitHub API ответил: ${pr_url:-нет ответа}"
  say "  Откройте PR вручную: $COMPARE_URL"
fi
say "  Напоминание: мержит только владелец репозитория (main защищён)."
