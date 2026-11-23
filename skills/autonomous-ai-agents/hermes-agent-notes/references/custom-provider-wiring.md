# Кастомный (корпоративный) провайдер: конфиг, ключ, переключение в клиенте

Две разные сущности, которые пользователь путает:

- `model.aliases.<name>` — ярлык для `/model <name>`. В настройках клиента его нет и не будет:
  страница «Провайдеры» алиасы не показывает вообще.
- `providers.<id>` — именованный провайдер. Именно он появляется в
  Настройки → Провайдеры → Custom endpoints: серверный список
  `GET /api/providers/custom-endpoints` строится из записей `providers`, у которых есть
  `base_url`.

Отсюда типовой тупик: алиас добавили, пользователь ищет провайдера в настройках клиента и не
находит, и спрашивает, не нужно ли перезагрузить клиент. Перезагрузка не поможет — нужна
запись `providers.<id>`.

## Запись провайдера

```yaml
providers:
  positivellm:                     # <id>: латиница, без пробелов
    name: Positive LLM
    base_url: https://api-llm.example.com/v1
    model: deepseek                # только имя, увиденное в /v1/models или /actual_models
    key_env: POSITIVE_LLM_API_KEY  # ключ живёт в .env, не здесь
    api_mode: chat_completions     # пиновать транспорт: у chat-only хостов auto-detect даёт 404
    context_length: 512000
    discover_models: true
```

Собирать точечными ключами — `config set` создаёт вложенные словари и сохраняет bool как `true`:

```bash
P=<profile>
hermes -p $P config set providers.<id>.name "Positive LLM"
hermes -p $P config set providers.<id>.base_url "https://api-llm.example.com/v1"
hermes -p $P config set providers.<id>.model deepseek
hermes -p $P config set providers.<id>.key_env POSITIVE_LLM_API_KEY
hermes -p $P config set providers.<id>.api_mode chat_completions
hermes -p $P config set providers.<id>.context_length 512000
hermes -p $P config set providers.<id>.discover_models true
hermes -p $P config get providers        # проверить, что дерево собралось
```

`api_key` в `config.yaml` не пишем никогда — ни руками, ни через UI: и `config_set`,
и панель кладут секрет в `.env` и ссылаются на него через `key_env`.

## Ключ в `.env`

```bash
hermes -p <profile> config env-path          # точный путь профиля
hermes -p <profile> config env-path | xargs git check-ignore   # .env должен быть вне git
```

- Одна строка `NAME=value`: без `export`, без кавычек и пробелов вокруг `=`.
- Имя переменной обязано совпадать с `key_env` записи, иначе Hermes ключ не найдёт.
- Ввод без попадания в историю: `read -rsp 'Token: ' K` затем
  `printf 'NAME=%s\n' "$K" >> <path>`; `unset K` после.
- Проверка без печати значения: `grep -c '^NAME=' <path>` → 1, плюс авторизованный
  `GET /v1/models` (200) или `/whoami`.

## Проверка без UI

```bash
hermes -p <profile> chat -q "Ответь ровно: OK" --provider <id> -m <model> --max-turns 1
```

Печатает `OK` ⇒ base_url и key_env разрешаются, значит проблема (если она есть) в UI-слое,
а не в креденшелах. Это дешевле, чем гонять пользователя по настройкам.

## Что видно в клиенте и когда

- Провайдеры: подразделы accounts / keys / **custom-endpoints** / local. Записи из
  `providers` с `base_url` попадают в custom-endpoints.
- Панель запрашивает конфиг при открытии: достаточно закрыть и снова открыть настройки.
  Перезапуск клиента нужен только когда бэкенд-процесс держит устаревший env (ключ добавлен,
  а запись жалуется на отсутствие ключа).
- Кнопка активации записи пишет `model.provider` + `model.default` (+ `context_length`), то есть
  делает провайдера основным; отдельно от неё в разделе «Модель» можно выбрать провайдера
  вручную.
- Поле API-ключа в форме endpoint оставлять пустым, если ключ уже лежит в `.env` и запись
  ссылается на него через `key_env`: иначе появится вторая переменная, и при инциденте
  непонятно, какую отзывать.
- Сохранение из панели **мержится** в существующую запись `providers.<id>`, а не
  пересобирает её: рукописные поля (`key_env`, `extra_headers`, `request_overrides`)
  переживают правку через UI. Поэтому запись, собранную `config set`, безопасно открыть и
  пересохранить в клиенте.

## Семантика переключения

- Правка конфига не меняет модель текущей сессии. `/model <alias>` переключает текущую сессию,
  `config set model.default <alias>` — будущие.
- Переключать в начале сессии: смена провайдера посреди диалога обнуляет prefix-кэш, и первые
  запросы идут по полной цене TPM.
- На вопрос «теперь ты отвечаешь новой моделью?» отвечать фактом, а не намерением:
  `model` + `billing_provider` текущей сессии в `state.db` и `config get model.default`.
- В десктопе, подключённом к удалённому бэкенду, правки конфига серверные: убедиться, что в
  клиенте выбран тот же профиль, который правили.
