# hermes-certification-assistant

Дистрибутив профиля Hermes Agent для подготовки сертификационной тестовой
документации ФСТЭК России (приказ № 76) на веб-СЗИ: прогон проверок функций
безопасности, сбор свидетельств, отчёт `.docx` со скриншотами и сводка `.md`.

## Установка

```bash
# 1. Установить профиль из репозитория
hermes profile install <git-url-этого-репозитория> --alias

# 2. Заполнить ключ
cp ~/.hermes/profiles/hermes-certification-assistant/.env.EXAMPLE \
   ~/.hermes/profiles/hermes-certification-assistant/.env
# вписать в .env: DEEPSEEK_API_KEY=...

# 3. Запуск
hermes -p hermes-certification-assistant chat
# или, если создан alias:
hermes-certification-assistant chat
```

Требуется `git` и Hermes `>=0.12.0`.

## Что внутри

| Путь | Назначение |
|---|---|
| `SOUL.md` | персона агента: прямой ответ, отчёт о сделанном, без пересказа процесса |
| `config.yaml` | полный конфиг профиля: `deepseek-flash` как модель по умолчанию, лимиты контекста, тулсеты, делегирование |
| `skills/` | 6 скиллов, которые несут специфику работы (см. ниже) |
| `distribution.yaml` | манифест дистрибутива: версия, требования к окружению, distribution-owned пути |

### Скиллы в репозитории

| Скилл | Зачем |
|---|---|
| `security/certification-stand-ftest` | методика тестовой документации по приказу № 76: разделы, функции безопасности, структура отчёта, сбор свидетельств |
| `security/cve-sbom-vex-mapping` | сопоставление списков CVE с таблицами SBOM/VEX |
| `software-development/codeql-cli` | установка, проверка и запуск CodeQL CLI |
| `software-development/sast-cpp-without-build` | SAST для C/C++ репозиториев без сборки (Semgrep, cppcheck) |
| `productivity/docx` | работа с `.docx`; **отличается от стокового** — добавлен `scripts/docx_read_ordered.py` (чтение в порядке документа) |
| `autonomous-ai-agents/hermes-agent` | работа с самим Hermes; **отличается от стокового** — расширенный набор reference-файлов |

Остальные штатные скиллы Hermes (56 штук) в репозиторий намеренно не вложены:
они приезжают в профиль сами при первом запуске (`sync_skills` вызывается на
старте CLI/TUI/гейтвея) и остаются в актуальной для вашей версии Hermes
редакции.

### Про два изменённых штатных скилла

`docx` и `hermes-agent` отличаются от стоковых версий Hermes. Синхронизатор
скиллов такие копии не перезаписывает — при `hermes update` он оставит вашу
версию и напечатает предупреждение. Вернуть стоковую редакцию (и потерять
правки) можно командой:

```bash
hermes skills reset docx
hermes skills reset hermes-agent
```

## Модель и секреты

- Модель по умолчанию: `deepseek-flash`, провайдер `deepseek`.
- Ключ живёт только в `.env` профиля; в репозитории его нет и быть не должно.
- Секция `dashboard.basic_auth` из `config.yaml` удалена при публикации: хеш
  пароля веб-панели и ключ подписи сессий каждому свои, задаются локально.

## Что не попадает в репозиторий

`memories/`, `sessions/`, `state.db*`, `logs/`, `.env`, `auth.json`, `vault/`,
`cron/`, кеши, `tools/`, `hermes-agent/` — см. `.gitignore`. Это пользовательские
данные и состояние, а не часть дистрибутива.

## Обновление у получателя

```bash
hermes profile update hermes-certification-assistant
```

Обновляются `SOUL.md`, `config.yaml` и 6 скиллов из репозитория. Память, сессии,
ключи и локально добавленные скиллы не трогаются; `config.yaml` по умолчанию
тоже сохраняется (перезаписать — `--force-config`).

## Выпуск новой версии (для автора)

1. Поправить `SOUL.md` / `config.yaml` / скиллы.
2. Поднять `version:` в `distribution.yaml`.
3. Закоммитить, поставить тег, запушить:

```bash
git commit -am "v1.1.0: <что изменилось>"
git tag v1.1.0
git push origin main --tags
```
