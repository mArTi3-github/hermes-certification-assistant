# Карта артефактов AFL++ / ptfuzz в материалах фаззинг-тестирования

Типовой архив «results/» от внутренней платформы фаззинга (ptfuzz) выглядит так:

```
results/
  Readme                       # единственный описательный файл (сводка + команда gcovr)
  input/datagram               # стартовый корпус / первый образец
  <target>/results/misc/LA0NN/ # метаданные инстансов
  <target>/results/corpus/     # итоговая коллекция (файлы с sha1-именами)
  <target>/results/crashes/    # уникальные сбои
  <target>/results/hangs/      # таймауты
  <target>/coverage/           # HTML gcovr по цели
  summary_coverage/            # HTML gcovr по всем целям
  <target>/results-<...>.zip   # копия corpus/crashes/hangs/misc
  <target>/<...>-logs.zip      # агрегированные журналы
  <target>/<...>-logs/{all,fuzzers}/*.fuzzer|*.validator
```

## Две раскладки архива

Помимо вложенной (`<target>/results/...`), встречается плоская — один компонент,
все материалы в корне:

```
<модуль>-<версия>-cert/
  misc/engine.LA000.fuzzer_stats   # статистика без каталога-инстанса
  misc/fuzzing/<...>.rc.env        # окружение задачи
  misc/logs/fuzzer-log.txt
  corpus/                          # итоговый корпус
  crashes/  hangs/                 # находки
  logs/, html-report/              # журнал и покрытие gcovr
  README.md, Readme
```

- Раскладку определять без распаковки: `unzip -Z1 <zip> | cut -d/ -f2 | sort -u`
  (`results`/`coverage` — вложенная, `misc`/`corpus` — плоская).
- `afl_stats_report.py` ждёт `<dir>/LA0NN/fuzzer_stats`; для плоской раскладки
  собрать `statview/LA000/` копиями `misc/engine.LA0NN.fuzzer_stats` и подать
  `statview`.
- `corpus/` в плоских архивах бывает больше числа загруженных seeds
  (`Loaded a total of N seeds`) — корпус пополняется в ходе прогона; для п. 2
  значимы только seeds и стартовый `input/`.
- Имена файлов в `hangs/`/`crashes/` — хеши содержимого, поэтому сумма
  `saved_hangs` по инстансам может превышать число файлов: одинаковая находка
  двух инстансов лежит одним файлом. Это объяснение для отчёта, а не расхождение.

## `fuzzer_stats` — главный источник чисел

Ключ: значение, по одному в строке. Что читать:

| Поле | Смысл |
|---|---|
| `start_time`, `last_update`, `run_time` | окно и длительность прогона (сек) |
| `execs_done`, `execs_per_sec` | объём работы, производительность инстанса |
| `corpus_count`, `corpus_found`, `corpus_imported` | размер очереди; seeds = `corpus_count − corpus_found − corpus_imported` |
| `edges_found`, `total_edges`, `bitmap_cvg` | покрытие карты (для п. 5«а» и «б») |
| `time_wo_finds`, `last_find` | время без новых путей (для п. 6) |
| `saved_crashes`, `saved_hangs`, `total_tmout` | находки |
| `stability` | детерминизм цели; 100 % — инструментирование работает корректно |
| `exec_timeout`, `slowest_exec_ms` | таймаут и фактическая длительность |
| `afl_version`, `target_mode`, `command_line` | чем и как запускали |
| `auto_dict_entries` | использовался ли автоматический словарь |

Формула seeds работает и как перекрёстная проверка с журналом
(`Loaded a total of N seeds`): расхождение означает, что часть корпуса пришла
из синхронизации и трактовать «стартовую коллекцию» надо аккуратно.

## `plot_data` — динамика

CSV без заголовка в данных, первая строка — комментарий с именами колонок:

```
# relative_time, cycles_done, cur_item, corpus_count, pending_total, pending_favs,
# map_size, saved_crashes, saved_hangs, max_depth, execs_per_sec, total_execs,
# edges_found, total_crashes, servers_count
```

- `map_size` (% карты) — покрытие на момент времени; первая точка ≈ 60 с, то есть
  **уже с мутациями**, не «стартовое покрытие».
- прирост `map_size` во времени — единственный способ увидеть плато, когда
  `fuzzer_stats` нет.

## `fuzzer_setup` — параметры запуска

```
# environment variables:
AFL_IMPORT_FIRST=1
AFL_FINAL_SYNC=1
AFL_NO_STARTUP_CALIBRATION=1
ASAN_OPTIONS=alloc_dealloc_mismatch=0:...:abort_on_error=1
MSAN_OPTIONS=symbolize=0:exit_code=86
# command line:
'/usr/local/bin/afl-fuzz' '-t' '1000' '-m' '10G' '-i' '/corpus' '-o' '/work' \
  '-p' 'fast' '-M' 'LA000' '--' '/tgt/<target>_fuzz'
```

Читать: `-t` (таймаут), `-m` (память), `-p` (power schedule), `-M`/`-S`
(master/secondary), `-i` (стартовый корпус), наличие `ASAN_OPTIONS`.

## Журналы

- `engine.LA0NN.stdout.txt` — вывод `afl-fuzz`: стартовая диагностика
  (`Loaded a total of N seeds`, `Target map size`, `Using ... persistent-mode`),
  затем поток строк `[*] Fuzzing test case #N (...) run_time=D:HH:MM:SS, cvg=X%`,
  в конце — `+++ Testing aborted by user +++` и `Statistics: ...`.
- `engine.LA0NN.stderr.txt` — `[+] Enabled environment variable ...` для всех
  переменных: здесь видно `ASAN_OPTIONS`/`MSAN_OPTIONS`.
- `main.log...fuzzer` — трейс shell-обвязки (`set -x`): сколько ядер, какая
  длительность задачи (`FUZZER_TIMEOUT`), как считается покрытие из `/status/coverage`,
  когда принимается решение о завершении.
- `validator.log...validator` — журнал оркестратора: сколько инстансов поднялось,
  какие образцы сохранялись в `corpus/` и `hangs/` и когда (по этим временам
  видно, что находки продолжались и на поздней стадии прогона). **Протоколом
  триажа он не является**: классификации находок и воспроизведения в нём нет.

Журналы ptfuzz склеены из нескольких инстансов: блоки разделены строками
`==> /work/engine.LA000.stdout.txt <==`, внутри блока строки уже могут быть
перемешаны. Поэтому «последнее изменение `cvg`» считается по `run_time` внутри
строки, а не по позиции строки в файле.

## Признаки конфигурации

| Признак в журнале | Что означает |
|---|---|
| `[+] Loaded a total of 1 seeds` | стартовая коллекция вырождена (п. 2) |
| `new fork server model v1`, `Target map size: 65536` | компиляторное инструментирование |
| `Using SHARED MEMORY FUZZING feature`, `futex persistent-mode synchronization` | persistent mode |
| `QEMU mode` / `FRIDA` / `unicorn` | инструментирование по бинарю (в материалах не встречалось) |
| `No auto-generated dictionary tokens to reuse` | словарь не применялся |
| `Testing aborted by user` | штатная остановка по решению оператора |
| отсутствие `Statistics: ...` | прогон прерван (kill), финальных чисел нет |
| `[ERROR] ...` / `FATAL` | сбой запуска, материал непригоден |

## Формулы для отчёта

- seeds = `corpus_count − corpus_found − corpus_imported` (или из журнала).
- п. 5«а»: `edges_found ≥ 2 × seeds` (при `seeds ≤ 1` — отметить вырожденность).
- п. 5«б»: `bitmap_cvg(финал) / map_size(первая точка plot_data) ≥ 2`;
  вторая оценка — `bitmap_cvg(финал) / cvg(первая запись журнала)`.
- п. 6: `min(time_wo_finds)/3600 ≥ 2` по инстансам; иначе — плато по журналу:
  `(общее время − время последнего изменения cvg) ≥ 2 ч`.
- медианное время исполнения — медиана `exec_us=` из строк журнала (мкс);
  сравнение с `-t` (мс) даёт порядок величины риска ложных таймаутов.

## gcovr

`index.html` в каждом каталоге покрытия — самостоятельный отчёт с `index.css`.
Итоги читаются по шаблону
`Lines: <exec> <total> <pct>% Functions: ... Branches: ...`, детализация — по
строкам вида `<файл> <вердикт> <pct>% <exec> / <total>`.

- «Branches» ≠ «базовые блоки» Методики: по gcovr вторую часть льготы
  (80 % блоков) не оценить.
- Суммарный отчёт — объединение целей; льготу 90 %/80 % применять к каждой цели
  отдельно.
- Файлы с 0 % (`stats.cpp` и подобные) перечислять в отчёте: это видимая дыра
  в покрытии, даже если формально она не мешает выполнению пункта.
