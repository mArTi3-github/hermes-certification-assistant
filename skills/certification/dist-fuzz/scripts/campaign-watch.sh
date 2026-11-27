#!/bin/bash
# Сторож за длинной фаззинг-кампанией на удалённой ВМ: выходит по СОБЫТИЮ и
# перевооружается. Подпись состояния содержит только значимые поля, поэтому
# штатная работа не будит агента.
#
# usage: campaign-watch.sh <ssh-host> <base-dir> [interval_s] [max_s]
#   ssh-host  — administrator@host (ключ берётся из ~/.ssh/config или ssh-agent)
#   base-dir  — каталог кампании на ВМ, внутри: evidence/build/targets/<T>/log.txt
#               (append-only журнал), live.txt (снимок cron), queue.state, tools/{plateau.py,snapshot.sh}
#   interval  — период опроса, по умолчанию 180 с
#   max       — максимум ожидания, по умолчанию 10800 с
#
# События: новый класс исключения у цели; прирост уникальных дефектов с шагом 10;
# достижение плато 2 ч; застой (журнал цели не растёт > 30 мин при живой очереди);
# конец или смерть очереди; свободной памяти < 700 МБ.
# НЕ события: перезапуск контейнера супервизором (2-3 с), новые артефакты того же дефекта.
set -u
HOST=${1:?ssh-host}
BASE=${2:?base-dir на ВМ}
INTERVAL=${3:-180}
MAX=${4:-10800}
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 $HOST"

signature() {
  $SSH "BASE=$BASE bash -s" <<'EOF' 2>/dev/null
B=$BASE
running=$(docker ps --format '{{.Names}}' 2>/dev/null | grep '^kcf-' | sed 's/^kcf-//' | sort)
echo "qproc_alive $(pgrep -cf 'bash tools/queue.sh' | awk '{print ($1>0)?"yes":"no"}')"
echo "qdone $(grep -q '^done' $B/queue.state 2>/dev/null && echo yes || echo no)"
for d in "$B"/evidence/build/targets/*/; do
  t=$(basename "$d"); [ -f "$d/log.txt" ] || continue
  src(){ cat "$d/log.txt" 2>/dev/null; docker logs -t "kcf-$t" 2>/dev/null; }
  toks=$(src | grep -a DEDUP_TOKEN | sed 's/.*DEDUP_TOKEN: //' | tr -d '\r' | sort -u | wc -l)
  cls=$(src | grep -a 'Java Exception' | sed 's/.*Java Exception: //' | tr -d '\r' \
        | sed 's/^\([A-Za-z0-9_.]*\).*/\1/' | sort -u | tr '\n' ',' | md5sum | cut -c1-8)
  pl=$(python3 "$B/tools/plateau.py" "$d/log.txt" 2>/dev/null || echo 0)
  pd=no; [ "$pl" -ge 7200 ] && pd=yes
  echo "t $t tokens10=$((toks / 10)) classes=$cls plateau_done=$pd"
  # застой проверяется только у работающей цели и по самому свежему из двух файлов:
  # log.txt дописывается лишь при остановке отрезка, живой прогон иначе «стареет»
  if echo "$running" | grep -qx "$t" && [ "$pd" = "no" ]; then
    age=$(python3 - "$d/log.txt" "$d/live.txt" <<'PY' 2>/dev/null
import os, sys, time
ages = [int((time.time() - os.path.getmtime(p)) / 60) for p in sys.argv[1:] if os.path.exists(p)]
print(min(ages) if ages else 999)
PY
)
    if [ "${age:-999}" -gt 30 ]; then echo "STALL $t age_min=${age}"; fi
  fi
done
free -m | awk '/Mem:/{print "memfree_mb="$7}'
EOF
}

base=$(signature)
if [ -z "$base" ]; then echo "STOP: ВМ недоступна"; exit 2; fi
echo "$base" | sed 's/^/baseline: /'
echo "== сторож запущен: интервал ${INTERVAL}s, максимум ${MAX}s, $(date -u +%H:%M:%SZ)"
t0=$(date +%s)
while [ $(( $(date +%s) - t0 )) -lt "$MAX" ]; do
  sleep "$INTERVAL"
  cur=$(signature)
  if [ -z "$cur" ]; then echo "STOP: SSH перестал отвечать"; exit 2; fi
  events=""
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in memfree*) continue ;; esac
    key=$(echo "$line" | cut -d' ' -f1-2)
    before=$(echo "$base" | awk -v k="$key" 'index($0,k)==1' | head -1)
    [ "$before" = "$line" ] && continue
    events="${events}ИЗМЕНЕНИЕ: ${before:-нет} -> ${line}\n"
  done <<< "$cur"
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    case "$line" in memfree*) continue ;; esac
    key=$(echo "$line" | cut -d' ' -f1-2)
    echo "$cur" | awk -v k="$key" 'index($0,k)==1' | grep -q . || events="${events}ЗАВЕРШИЛОСЬ: ${line}\n"
  done <<< "$base"
  mem=$(echo "$cur" | sed -n 's/^memfree_mb=//p')
  if [ -n "$mem" ] && [ "$mem" -lt 700 ]; then events="${events}ПАМЯТЬ: свободно ${mem} МБ\n"; fi
  if [ -n "$events" ]; then
    printf "%b" "$events"
    echo "== событие $(date -u +%H:%M:%SZ), снимок:"
    echo "$cur"
    exit 0
  fi
done
echo "== тихий период ${MAX}s без событий, $(date -u +%H:%M:%SZ); снимок:"
echo "$cur"
exit 0
