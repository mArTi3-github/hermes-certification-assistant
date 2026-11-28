#!/bin/bash -eu
# Пул фаззинг-кампаний: N слотов берут цели из общего списка; цель, выполнившая
# формальные критерии (плато PLATEAU_STOP по cov/ft), останавливается и её слот
# немедленно уходит следующей цели. Волн нет: простой слота на границе волны
# недопустим — ядро простаивает, пока три цели ждут четвёртую.
#
# Запуск на машине прогонов: setsid nohup bash queue-pool.sh > queue.out 2>&1 < /dev/null
# Переменные: BASE (каталог кампании), IMAGE, TARGETS (список целей или файл),
#             WORKERS, SECS (страховочный бюджет цели), PLATEAU_STOP, MODE=real|smoke.
# Файлы: queue.log (append-only), queue.state, queue.pending, queue.lock.
BASE=${BASE:-$PWD}
IMAGE=${IMAGE:?IMAGE=keycloak-fuzz:<версия>}
MODE=${MODE:-real}
SECS=${SECS:-18000}
SMOKE=${SMOKE:-90}
WORKERS=${WORKERS:-3}
KEEP_GOING=${KEEP_GOING:-100000}
PLATEAU_STOP=${PLATEAU_STOP:-7200}
# список целей: TARGETS="A B C" либо TARGETS=@файл (по одной в строке)
if [ "${TARGETS:-}" = "" ]; then echo "TARGETS не задан" >&2; exit 2; fi
case "${TARGETS}" in @*) mapfile -t ORDER < "${TARGETS#@}" ;; *) read -r -a ORDER <<< "$TARGETS" ;; esac
DUR=$([ "$MODE" = "smoke" ] && echo "$SMOKE" || echo "$SECS")
LOG=$BASE/queue.log; STATE=$BASE/queue.state; PENDING=$BASE/queue.pending; TOOLS=$BASE/src/tools
say() { echo "[$(date -u +%FT%TZ)] $*" >> "$LOG"; }
state() { echo "$*" >> "$STATE"; }

# переопределяемые хуки под конкретный компонент
mem_for() { echo "${MEM_GENERIC:-3g}"; }
tmo_for() { echo "${TIMEOUT_GENERIC:-60}"; }
dict_for() { echo ""; }

claim() {
  local T; exec 8>"$BASE/queue.claim.lock"; flock 8
  T=$(head -1 "$PENDING" 2>/dev/null || true)
  if [ -n "$T" ]; then tail -n +2 "$PENDING" > "$PENDING.tmp"; mv "$PENDING.tmp" "$PENDING"; fi
  exec 8>&-; echo "$T"
}

start_target() {
  local T=$1 dur=$2 OUT=$BASE/evidence/build/targets/$1 DICT DICTARG=""
  mkdir -p "$OUT/corpus" "$OUT/crashes" "$OUT/hangs" "$BASE/evidence/build/findings/$T"
  [ -z "$(ls -A "$OUT/corpus" 2>/dev/null)" ] && cp -n "$BASE"/evidence/build/seeds/"$T"/* "$OUT/corpus"/ 2>/dev/null || true
  docker rm -f "kcf-$T" >/dev/null 2>&1 || true
  DICT=$(dict_for "$T"); [ -n "$DICT" ] && DICTARG="-dict=$DICT"
  local attempt=0
  while :; do
    # -t обязателен: без TTY JVM буферизует stdout и хвост журнала теряется
    if docker run -dt --name "kcf-$T" --network none -m "$(mem_for "$T")" \
        -v "$BASE/src":/src -v "$BASE/evidence/build":/out "$IMAGE" "/out/bin/$T.sh" \
        -max_total_time="$dur" -artifact_prefix="/out/targets/$T/crashes/" \
        -ignore_crashes=1 -ignore_timeouts=1 "--keep_going=$KEEP_GOING" \
        "-timeout=$(tmo_for "$T")" $DICTARG "/out/targets/$T/corpus" >>"$LOG" 2>&1; then break; fi
    attempt=$((attempt+1)); [ "$attempt" -ge 3 ] && { say "start FAILED $T"; return 1; }
    say "start retry $T"; sleep 5
  done
  say "start $T dur=${dur}s timeout=$(tmo_for "$T")"
}

defuse_timeout() {   # вход зависания вынести из корпуса, иначе следующий отрезок начнётся с него же
  local T=$1 OUT=$BASE/evidence/build/targets/$1 f h
  for f in "$OUT"/crashes/timeout-*; do
    [ -e "$f" ] || continue
    h=$(sha1sum "$f" | cut -d' ' -f1); rm -f "$OUT/corpus/$h"
    mv "$f" "$OUT/hangs/" 2>/dev/null || true; say "hang $T sha1=$h -> hangs/"
  done
}

run_target() {
  local T=$1 OUT=$BASE/evidence/build/targets/$1
  local until_epoch=$(( $(date +%s) + DUR )) code remaining pl
  mkdir -p "$OUT"; date -u +%FT%TZ > "$OUT/campaign_start"
  while :; do
    remaining=$(( until_epoch - $(date +%s) ))
    # порог «слишком короткий отрезок» — секунды, а не длительность smoke:
    # иначе проверочный прогон на 60 с не запускает ни одного контейнера
    [ "$remaining" -le 10 ] && break
    start_target "$T" "$remaining" || { sleep 15; continue; }
    rm -f "$OUT/plateau_stop"
    while docker inspect -f '{{.State.Running}}' "kcf-$T" 2>/dev/null | grep -q true; do
      sleep 60
      docker logs -t --tail 20000 "kcf-$T" > "$OUT/live.txt" 2>/dev/null || true
      pl=$(python3 "$TOOLS/plateau.py" "$OUT/log.txt" 2>/dev/null || echo 0)
      if [ "${pl:-0}" -ge "$PLATEAU_STOP" ]; then
        say "plateau $T = ${pl}s: стоп, слот свободен"
        touch "$OUT/plateau_stop"; echo "$pl" > "$OUT/plateau_seconds"
        docker stop "kcf-$T" >/dev/null 2>&1 || true; break
      fi
    done
    docker wait "kcf-$T" >/dev/null 2>&1 || true
    code=$(docker inspect -f '{{.State.ExitCode}}' "kcf-$T" 2>/dev/null || echo "?")
    docker logs -t "kcf-$T" >> "$OUT/log.txt" 2>&1 || true
    defuse_timeout "$T"
    say "stop $T exit=$code corpus=$(ls -1 "$OUT/corpus" 2>/dev/null | wc -l)"
    docker rm -f "kcf-$T" >/dev/null 2>&1 || true
    if [ "$code" = "0" ] || [ -f "$OUT/plateau_stop" ]; then break; fi
    sleep 3
  done
  [ -f "$OUT/plateau_stop" ] || python3 "$TOOLS/plateau.py" "$OUT/log.txt" > "$OUT/plateau_seconds" 2>/dev/null || echo 0 > "$OUT/plateau_seconds"
}

worker() {
  local T
  while :; do
    T=$(claim); [ -z "$T" ] && break
    say "worker $1 claim $T"; state "run $T"
    run_target "$T"
    if [ "$MODE" = "real" ]; then
      state "coverage seeds $T"; bash "$TOOLS/coverage-local.sh" "$T" seeds seeds >> "$LOG" 2>&1 || say "coverage seeds FAILED $T"
      state "coverage final $T"; bash "$TOOLS/coverage-local.sh" "$T" final final >> "$LOG" 2>&1 || say "coverage final FAILED $T"
    fi
    python3 "$TOOLS/fuzz_status.py" --root "$BASE/evidence/build" >>"$LOG" 2>&1 || true
  done
  say "worker $1 exit"
}

exec 9>"$BASE/queue.lock"
flock -n 9 || { echo "queue already running" >> "$LOG"; exit 1; }
: > "$STATE"; printf '%s\n' "${ORDER[@]}" > "$PENDING"
say "pool start mode=$MODE dur=${DUR}s workers=$WORKERS plateau_stop=${PLATEAU_STOP}s"
for i in $(seq 1 "$WORKERS"); do worker "$i" & done
wait || true
state done; say "pool done"
