# Автозапуск Hermes на ВМ: systemd user-юниты

Применять, когда просят «поднимать Hermes при старте машины», бэкенд не должен умирать вместе
с терминалом, или сервис Hermes сломался/крутится в рестарт-цикле.

## Что чем поднимается

| Что | Юнит | Кто создаёт |
|---|---|---|
| gateway (Telegram/Discord/Slack, cron, WS для TUI/Desktop) | `hermes-gateway.service`, `ExecStart=… gateway run` | сам Hermes (ставится при настройке сервиса) |
| веб-бэкенд (dashboard + встроенный чат, порт 9119) | `hermes-dashboard.service` | **вручную**: отдельной команды установки нет |

Оба — user-scope (`~/.config/systemd/user/`), `WantedBy=default.target`. Чтобы они стартовали
при загрузке ВМ **без входа в графическую сессию**, должен быть включён linger:
`loginctl show-user <user> | grep Linger` → `Linger=yes`. Маркер — пустой файл
`/var/lib/systemd/linger/<user>` (пустой — это норма, а не ошибка). Включается
`loginctl enable-linger <user>` (нужен sudo).

## Шаблон юнита (проверен на стенде)

```ini
[Unit]
Description=Hermes Agent Web Dashboard - machine-level backend
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=%h/.hermes/hermes-agent/.hermes/bin/hermes dashboard --host 0.0.0.0 --port 9119 --no-open
WorkingDirectory=%h
Environment="HERMES_HOME=/home/<user>/.hermes"
Environment="PATH=/home/<user>/.hermes/tools/node-<ver>-linux-x64/bin:/home/<user>/.hermes/tools/npm-<ver>-linux-x64/bin:/home/<user>/.hermes/tools/ripgrep-<ver>-linux-x64:/home/<user>/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
Restart=always
RestartSec=5
RestartPreventExitStatus=75 78
KillMode=mixed
KillSignal=SIGTERM
TimeoutStopSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

Решения в шаблоне:

- **`ExecStart` — прибитый лаунчер** `~/.hermes/hermes-agent/.hermes/bin/hermes`, а не
  `~/.local/bin/hermes`: ровно так сделан gateway-юнит, и этот путь обновляется вместе с
  `hermes update` без переписывания юнита.
- **`HERMES_HOME` — корень машины** (`~/.hermes`), не каталог профиля. `hermes dashboard` —
  machine-level сервер: он сам переезжает в `-p default`, добавляет
  `--open-profile <активный профиль>` и пиннит `HERMES_HOME` к корню. Профиль в юните не
  хардкодить — лаунчер берёт активный.
- **`PATH` с node/npm** из `~/.hermes/tools`: в boot-окружении нет интерактивного PATH, а
  dashboard при старте может пересобирать веб-UI (vite → `hermes_cli/web_dist`). Без node
  сборка провалится. `--skip-build` добавлять только осознанно — иначе после `hermes update`
  останется старый UI.
- **`RestartPreventExitStatus=75 78`** — иначе рестарт-цикл: 75 = `PORT_IN_USE_EXIT_CODE`
  (`EX_TEMPFAIL`, «порт занят другим бэкендом»), 78 = `EX_CONFIG` («хост уже обслуживается
  другим PID»). В доке упомянут только 78 — этого мало, порт-конфликт даёт именно 75.
- `EnvironmentFile=%h/.hermes/.env` не обязателен: Hermes читает `.env` сам. Добавлять его
  стоит только если нужны OAuth-переменные в окружении — и тогда следить, чтобы systemd
  смог разобрать каждую строку файла (иначе сервис не стартует).

## Порядок установки

1. Написать юнит в `~/.config/systemd/user/<name>.service`.
2. `systemctl --user daemon-reload && systemctl --user enable --now <name>.service`.
3. Если порт занят ручным процессом — освободить: `HERMES_HOME=~/.hermes hermes dashboard --stop`.
   Из сессии профиля `hermes dashboard --stop` видит только процессы **своего** профиля и
   ответит «No hermes dashboard processes running for this profile», а machine-level процесс
   останется держать порт.
4. Проверить (см. ниже) и только потом говорить «готово».

## Проверка — то, что реально доказывает работу

```bash
systemctl --user is-enabled hermes-dashboard.service      # enabled
systemctl --user status  hermes-dashboard.service         # active (running) + Main PID
journalctl --user -u hermes-dashboard -n 40 --no-pager    # HERMES_DASHBOARD_READY port=9119
ss -tlnp | grep 9119                                      # 0.0.0.0:9119, процесс из cgroup юнита
curl -s http://127.0.0.1:9119/api/status | jq '.auth_required, .auth_providers'
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9119/   # 302 на логин при basic auth
systemd-analyze --user verify ~/.config/systemd/user/hermes-dashboard.service
kill -9 $(systemctl --user show -p MainPID --value hermes-dashboard.service)   # живучеть: PID сменится, /api/status снова 200
```

`systemd-analyze verify` может ругаться на чужие юниты (например `spice-vdagent`) — смотреть
только строки про свой файл.

## Честная формулировка про «при запуске ВМ»

`enabled` + linger — это механизм, а не проверка. Холодный старт (`systemctl --user start`
из остановленного состояния) и выживание `kill -9` доказаны, реальная загрузка — нет.
В отчёте отдельно: что проверено командами (с выводом), и что не проверялось (перезагрузка ВМ)
— и предложить перезагрузку как финальную проверку, а не выдавать готовность за факт.

## Управление

```bash
systemctl --user restart hermes-dashboard   # после смены конфига/обновления
systemctl --user status  hermes-dashboard
journalctl --user -u hermes-dashboard -f
git -C ~/.hermes/hermes-agent log -1 --format=%h   # какая версия реально запущена
```

Если пользователь по привычке всё равно набирает `hermes dashboard --host 0.0.0.0`, при живом
юните команда не поднимет второй сервер, а приаттачится к нему и откроет браузер — так и
объяснить, чтобы он не искал «второй порт».
