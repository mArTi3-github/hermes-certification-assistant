# Где искать готовые харнессы компонента

Цель шага: до написания своего харнесса выяснить, что уже сделано, на какой версии это
собирается и какие корпуса/словари прилагаются. Клонировать репозиторий для этого не нужно.

## Порядок поиска

1. **Дерево репозитория на нужном теге** — есть ли фаззинг внутри продукта:
   ```bash
   curl -sS "https://api.github.com/repos/<org>/<repo>/git/trees/<tag>?recursive=1" -o tree.json
   python3 - <<'PY'
   import json,re
   t=json.load(open('tree.json'))
   paths=[e['path'] for e in t['tree'] if e['type']=='blob']
   print(len(paths), 'blobs; truncated:', t.get('truncated'))
   for pat in ('fuzz','jazzer','oss-fuzz','sanit'):
       print(pat, [p for p in paths if pat in p.lower()][:10])
   print([p for p in paths if p.startswith('.github/workflows/')])
   PY
   ```
   Ноль совпадений + отсутствие фаззинг-workflow = в самом продукте наработок нет. Это ответ,
   а не тупик: следующий шаг обычно и есть основной.
2. **OSS-Fuzz-проект**: `GET /repos/google/oss-fuzz/contents/projects/<name>` — `project.yaml`
   (`language`, `fuzzing_engines`, `sanitizers`, `main_repo`), `Dockerfile` (какой образ и какие
   зависимости), `build.sh` (как собирается продукт и где лежат харнессы).
3. **Харнессы**: у JVM-проектов — `cncf/cncf-fuzzing/projects/<name>` (там же `peeds/` с корпусами
   и словарями); у C/C++ — каталог OSS-Fuzz-проекта или `fuzzing/` в самом продукте.
4. **Риск дрейфа**: `GET /repos/cncf/cncf-fuzzing/commits?path=projects/<name>` — дата последней
   правки харнессов против даты тега продукта. Разрыв в год и больше — ждать ошибок компиляции.
5. **Фаззер**: релизы `CodeIntelligenceTesting/jazzer` (Linux x86-64 тарбол: `jazzer_driver`,
   `jazzer_agent_deploy.jar`, `jazzer-api.jar`). Версия фиксируется в материалах.

## Проверка, что харнесс доходит до кода (аудит достижимости)

Найденный харнесс может быть **мёртвым**: компилируется, крутится часами, даёт `cov` и
«0 находок» — и при этом ни разу не вызывает тестируемый код. Проверять до бюджета кампании,
а не после.

Признак в цифрах: покрытие seeds и покрытие финального корпуса совпадают не просто «близко»,
а в единицах — десятки инструкций в одном-двух классах, и фаззер за часы не добавляет ни
одного нового класса. Это не «насыщение», а неработающая цель: насыщение бывает при сотнях
инструкций и десятках классов, где новые входы упираются в потолок достижимого кода.

Механизм, который к этому приводит (типично для харнессов с моками из cncf-fuzzing):
`randomizeMockData` рандомизирует каждое поле мока через
`data.consumeString(data.remainingBytes() / 2)`, и таких вызовов на одну модель — десяток.
Каждый забирает половину остатка, последовательность вычерпывает буфер до нуля, после чего
`consumeString` для самого тестируемого документа возвращает пустую строку. Разбор падает на
первой проверке (`validInput(...) = false`), верификатор/парсер не вызывается ни разу.

Проверка (рефлексия по вложенному классу харнесса, отдельная проба — не правка продукта):

```java
// <T>$MockObject — вложенный класс конкретного харнесса
Class<?> mockCls = Class.forName("<T>$MockObject");
Constructor<?> ctor = mockCls.getDeclaredConstructor(); ctor.setAccessible(true);
Object mock = ctor.newInstance();
Method init = mockCls.getDeclaredMethod("mockInstance"); init.setAccessible(true); init.invoke(mock);
Method rnd = mockCls.getDeclaredMethod("randomizeMockData", FuzzedDataProvider.class);
int before = provider.remainingBytes(); rnd.invoke(mock, provider);
System.out.println("mock съел " + (before - provider.remainingBytes()) + " из " + before);
```

Если «съедено = весь файл» — харнесс мёртвый. Лечение — минимальная правка с diff в материалах:
тестируемый документ читается ДО рандомизации моков (исходник сохраняется рядом как
`harnesses-orig/`, разница — `harness-diff-<цель>.patch`), а в seeds добавляется 4-байтовый
хвост под расход провайдера. После правки покрытие seeds должно вырасти по классам, иначе
искать следующую поломку.

Вторая поломка того же рода: моки не заглушают провайдера, до которого доходит цепочка
(например, `PublicKeyStorageProvider` → `NullPointerException: ... "keyStorage" is null`).
Такое исключение — дефект харнесса, а не продукта: фаззинг на такой цели будет находить
только его, сколько бы часов ни шёл. Цель помечается в отчёте как непригодная для зачёта
находок, с указанием, до какого шага цепочки она доходит и что именно не заглушено.

| Что видно | Вердикт | Действие |
|---|---|---|
| seeds и final: десятки инструкций, классы не растут | харнесс мёртвый | проба на расход мока, правка чтения документа, повторный замер seeds |
| seeds и final: сотни инструкций, классы растут | цель живая, плато реальное | закрывать п. 5 по «а» |
| после правки доходит до NPE в мок-слое | харнесс непригоден для зачёта | не тратить бюджет, зафиксировать в отчёте |
| после правки покрытие выросло в разы | правка оправдана | кампания + отдельный срез покрытия |

## Разобранный пример: Keycloak 26.5.2

- В репозитории (тег `26.5.2`, 11 402 blob, 17 workflow) фаззинг-артефактов нет вовсе.
- OSS-Fuzz-проект: `language: jvm`, `fuzzing_engines: [libfuzzer]`, `sanitizers: [address]`,
  `main_repo: keycloak/keycloak`; `Dockerfile` — `base-builder-jvm` + Maven 3.9 +
  `git clone --depth 1` продукта и `cncf-fuzzing`.
- Харнессы: `cncf/cncf-fuzzing/projects/keycloak` — 24 `*Fuzzer.java` + `BaseHelper.java` (общая
  обвязка: мок-сессии, инициализация моделей, ключи) + `build.sh`.
- `build.sh`: Maven с исключением ненужных модулей (`-pl "!docs,…,!quarkus,…,!tests,…"`),
  докрутка mockito/mockwebserver/okhttp/bc-fips из Maven Central, затем на каждый харнесс —
  обёртка `jazzer_driver --target_class=<T> --cp=… --agent_path=… --jvm_args=…`.
- Корпуса: только у 3 целей из 24 (`SamlParser` — 4 образца, `JwkParser` — 1, `JoseParser` —
  `json.seed`); словари `saml.dict`, `json.dict`. Остальные цели — без seeds, корпуса
  формируются на шаге 6.
- Обёртка ставит JDK внутри себя (`apt install openjdk-17-jdk`) и требует строку «openjdk 17»:
  в образе это вырезать, JDK задавать `JAVA_HOME`-ом.
- `ClientSignatureVerifierProviderFuzzer` — пример мёртвого харнесса: моки вычерпывают буфер,
  JWS всегда пустой, покрытие 59 инструкций в 8 классах при нуле находок; после правки чтения
  документа — 557 инструкций в 20 классах, но NPE на незаглушённом `PublicKeyStorageProvider`.

## Что даёт такой набор

Раскладка харнессов по поверхностям (для отбора целей):

| Область | Внешний вход |
|---|---|
| SAML-парсеры/валидация | `POST /realms/{realm}/protocol/saml`, SAMLResponse/Assertion |
| JWT/JOSE/JWK/JWS | bearer-токены, JWKS клиента, JWE, подписи |
| Крипто/сертификаты | клиентские сертификаты (mTLS), cert-based client auth |
| Аутентификация | формы логина: пароль, OTP, recovery codes, WebAuthn |
| Авторизация | UMA-запросы к token/authorization endpoint |
| URI/утилиты | `redirect_uri`, построение URL, разбор параметров |

Правило отбора: цель берётся, если (а) вход доступен без аутентификации, (б) обрабатываются
недоверенные данные, (в) есть рабочий харнесс. 6–8 целей из такого набора — достаточный объём
для «приемлемого качества на критичных участках».
