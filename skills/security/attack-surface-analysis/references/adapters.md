# Адаптеры: другие языки и большие объёмы

## Как добавить веб-стек

Экстрактор разделён на три шага, менять нужно только первые два:

1. **Извлечение маршрутов** (`extract_routes`) — найти регистрации и вернуть записи
   `{method, path, handler{qual,file,line}, params[], kind}`. Аннотации фреймворков ищите регулярным
   выражением по декораторам/атрибутам, но **обязательно** сверяйте результат с независимым grep.
2. **Разрешение вызовов** (`Repo.resolve_call` + `_infer_local_types`) — для нового языка нужен свой
   резолвер имён: класс-квалифицированные имена, типы локальных переменных, DI-контейнеры.
3. **Сигналы валидации/стоков** (`VALIDATION_PATTERNS`, `SINK_PATTERNS`) — общие, добавляйте правила
   под фреймворк (например, `@Valid`/`@Validated` для Java, `[FromBody]`/ModelState для ASP.NET,
   `zod`/`joi`/`celebrate` для Node).

Практичные источники маршрутов по стекам:

| Стек | Где искать |
|---|---|
| ASP.NET Core | `[HttpGet("...")]`, `[Route]`, `MapGet/MapPost` в `Program.cs`, `.csproj` для состава |
| Java/Spring | `@RequestMapping`, `@GetMapping`, `@RestController`, `@PathVariable`/`@RequestBody` |
| Go | `http.HandleFunc`, `mux.Handle`, `gin/echo/chi` регистрации, `.proto` для gRPC |
| Node/TS | `app.get/post`, `router.route`, `fastify.route`, NestJS `@Get/@Post` |
| Erlang/Elixir | `cowboy` маршруты, `Plug.Router`, Phoenix `scope/get/post` |
| Rust | `actix-web`/`axum` `route(...)`, `#[get("...")]` |
| C/C++ | экспортируемые символы (`nm -D`), `main`/`argv`, сокеты (`bind/listen/accept`), IPC, парсеры файлов |

Если для языка нет готового парсера в stdlib, не тащите тяжёлый рантайм в отчёт: используйте
`tree-sitter` (библиотека или CLI) либо Semgrep с набором правил под фреймворк, а результат
приводите к тому же JSON.

## Замена AST-BFS на индекс при 1M+ строк

`codebase-memory-mcp` даёт локальный граф (162 грамматики, Hybrid LSP для 12 языков, `CALLS`,
`DATA_FLOWS`, `Route`-узлы для HTTP/gRPC/GraphQL/tRPC, `trace_path` до глубины 15, Cypher-подмножество).
Он подключается к Hermes как MCP-сервер (`mcp_servers` в `config.yaml`, инструменты `mcp_cbm_*`) и
подходит как источник достижимости там, где AST-обход по файлам уже не масштабируется.

Порядок действий:

1. `index_repository(async: true)` → `status: true` до `succeeded`.
2. `get_architecture(aspects: routes)` — маршруты; сверить с грепом по декораторам.
3. `trace_path(mode: calls|data_flow, depth: 1..15)` от обработчика — достижимость.
4. `query_graph` для собственных запросов (например, «функции с неразрешёнными вызывающими»).
5. Результат нормализовать в `surface.json`; HTML не менять.

Ограничения, которые надо переносить в отчёт: `Route`-узлы покрывают веб-протоколы, но не CLI,
IPC, файловые парсеры; `DATA_FLOWS` — синтаксические рёбра, а не taint; граф эвристический.

## Настоящий taint

| Инструмент | Когда | Ограничение |
|---|---|---|
| CodeQL | есть сборка (C/C++, Go) или язык без сборки (C#, Java, JS, Python, Ruby) | без сборки C/C++ не анализируется |
| Joern | C/C++, Java, JS/TS, Python, PHP, Go, C#, Kotlin, Swift без сборки | JVM, заметный heap на миллионах строк |
| Semgrep | быстрые правила и taint внутри функции | межфайловый taint — платный `--pro` |

При наличии taint-отчёта заказчика (HTML/JSON) его полезно не пересчитывать, а импортировать:
наложить помеченные модули на достижимое множество и повысить приоритет пересечений.
