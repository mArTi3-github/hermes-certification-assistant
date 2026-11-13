# Автоматизация веб-интерфейса XSpider PRO и съём свидетельств через API

Проверено на стенде XSpider PRO 1.2.x (микрофронтенды `xpro-shell`). Приёмы
применимы к любой Angular-оболочке с вложенным shadow DOM.

## 1. Где живёт DOM

Цепочка хостов: `document` → `xpro-shell-login-page` (форма входа) и
`xpro-shell-shell-remote-app` → `xpro-scanning` (основное приложение).
`document.querySelectorAll('input')`, `document.body.innerText` — пусты.

Универсальный обход (вставлять в страницу один раз, дальше пользоваться):

```js
window.__deep = function(sel, root){
  root = root || document; const found = [];
  const walk = (r) => {
    try { r.querySelectorAll(sel).forEach(e => found.push(e)); } catch(e){}
    r.querySelectorAll('*').forEach(e => { if (e.shadowRoot) walk(e.shadowRoot); });
  };
  walk(root); return found;
};
```

- **Текст:** у `ShadowRoot` нет `innerText`; `textContent` корневого хоста
  возвращает содержимое `<style>` (мегабайты CSS). Собирать текст по каждому
  корню отдельно (`r.body ? r.body.innerText : r.innerText`) **и не считать это
  надёжным источником** для таблиц: виртуализированные строки в текст не попадают.
  Для таблиц — скриншот и зрение.
- **Координаты:** скриншот отдаётся в вьюпорт 1600×900 и показывается вдвое
  меньше — координата клика = 2 × координата на просматриваемой картинке.

## 2. Поиск кликабельных элементов

- `Accessibility.getFullAXTree` → пары (role, name, `backendDOMNodeId`). Хорошо
  работает для тулбара (`button`, `link`, `columnheader`), но **строки таблиц в
  виртуализированном гриде там часто отсутствуют** — не делать вывод «элемента
  нет», если его видно глазами.
- Клик по AX-узлу: `DOM.getBoxModel(backendNodeId=...)` → центр `content` →
  `Input.dispatchMouseEvent` (mousePressed + mouseReleased).
- Если AX пуст, а элемент видно: `window.__deep('button').find(e => /Текст/.test(e.innerText))`
  → `getBoundingClientRect()` → клик по центру. Для ссылок/кнопок внутри shadow
  root часто надёжнее `element.click()`, чем координатный клик.

## 3. Кастомные контролы Angular (mc-*)

- **`mc-select`** (выпадающие списки): координатный клик по видимой части поля
  открывает overlay, но клик по пункту списка часто промахивается. Надёжно:
  открыть, затем `window.__deep('mc-option,[role=option],li')` → найти по тексту →
  `el.scrollIntoView({block:'center'})` + `el.click()`. Пункты ниже 900 px
  (за пределами вьюпорта) координатно недоступны вообще.
- **`mc-tag-list-input-<n>`** (цели сбора данных: «Включить»/«Исключить»): текст,
  вставленный в поле, не коммитится. Нужно: `el.focus()` → `Input.insertText` →
  `Input.dispatchKeyEvent(rawKeyDown/keyUp, windowsVirtualKeyCode=13, key='Enter',
  code='Enter')`. После этого появляется чип с «×».
- **Split-кнопки** (`Запустить ▾`, `Выпустить отчёт ▾`): нажатие на левую часть
  открывает меню, а не запускает. Кликать по триггеру целиком/по правому краю,
  затем по пункту («Запустить сбор данных»).
- **Тулбар активен только при выбранной строке**: `button.disabled` проверять
  через `window.__deep('button')`, а не по цвету.

## 4. Устойчивость сессии

- Частые `goto_url` подряд (в цикле ожидания статуса) рвут CDP-соединение
  (`no close frame received or sent`) и уводят вкладку на `about:blank`. Лечится
  `ensure_real_tab()` → заново `Security.setIgnoreCertificateErrors` → повторная
  навигация; если сессия потеряна — логин заново тем же мостом через vault.
- Ожидание статуса делать одним `sleep` + один переход, а не десятком перезагрузок.

## 5. Свидетельства через API (точнее скриншотов)

Реальные эндпоинты берутся из загруженных ресурсов, а не угадываются:

```js
performance.getEntriesByType('resource').map(e => e.name).filter(n => /\/api\//.test(n))
```

Затем `fetch(url)` в контексте страницы (cookie сессии уже есть) и сохранение
ответа в файл приложения. Проверенные эндпоинты:

| Эндпоинт | Что даёт |
| --- | --- |
| `GET /api/scanning/v2/scanner_tasks/<taskId>/runs` | запуски: `status`, `startedAt`/`finishedAt`, `errorStatus` (`green`/`red`) |
| `GET /api/scanning/v3/scanner_profiles` | профили: `name`, `moduleName`, `output` |
| `GET /api/reports/issues` | список отчётов: `state` (`completed`/`error`), `errorReason` |
| `GET /api/reports/issues/<id>/download` | файл выпущенного отчёта |
| `GET /api/reports/issues/<id>` | карточка отчёта |
| `GET /api/hosts/v1/limits` | `{current, max}` — учёт узлов в лицензии |
| `GET /api/licensing/v1/product`, `/api/licensing/v4/licenses/<id>` | признаки лицензии (`host_count`, `audit`, `auditplc`, `pentest`) |
| `GET /api/v1/scanner_modules` | модули сканирования |
| `GET /api/scopes/v2/scopes`, `/api/groups/v2/hierarchies/tasks` | области и группы задач |
| `GET /api/health_monitoring/v2/total_status`, `/api/deployment-target/v1/deployments/<id>` | состояние стенда и развёртывания |

Формат файла свидетельства — шапка (дата, стенд, учётная запись, способ снятия),
запрос, код ответа, тело ответа как есть.

## 6. Домен: почему отчёты падают и что они содержат

- **`hostNotLicensedError`** — отчёт по задаче, чей узел не добавлен в лицензию.
  Узел попадает в лицензию после успешного **обнаружения** (HostDiscovery):
  сначала задача обнаружения по IP/FQDN, потом отчёты по этому узлу. Проверять
  `/api/hosts/v1/limits` (`current` = 0 значит лицензированных узлов нет).
- **Отчёт «Инвентаризация узлов» (`mpxsnapshot`)** заполняет `OsName`, `OsVersion`,
  `Name`, `Version`, `Vendor` только если исходная задача сбора данных шла
  профилем модуля **Audit**. По задаче HostDiscovery в отчёте остаются лишь
  `IpAddress` и `ScanTime` — требование инвентаризации ОС/ПО этим не закрывается.
- **Профили по задачам:** обнаружение — `HostDiscovery`; аудит с уязвимостями —
  `Unix SSH Audit`, `Unix Audit Vulnerabilities Discovery`, `Windows Audit`;
  pentest — `Safe PenTest`, `Fast PenTest`, `Full PenTest`. Отчёты об уязвимостях
  требуют задачи, реально собравшей уязвимости, и лицензированного узла.
- **Наличие признака `pentest` в лицензии не гарантирует работу профиля**: запуск
  может завершиться за 0 с с `errorStatus: red`, при этом журнал запуска
  недоступен (кнопка «Скачать журнал» неактивна). Тогда честная формулировка —
  «запуск завершён с ошибкой, причина в интерфейсе не отображается».
- **Колонка «Статус» в списке задач — не результат.** Запуск с ошибкой за 0 с
  показывается как «Завершена» с предупреждающим значком. Всегда подтверждать
  через `runs.errorStatus`.
