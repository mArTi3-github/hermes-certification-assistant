# Пример: Keycloak 26.5.2 + Jazzer, рабочая сборка (проверена 26.11.2026)

Это **пример применения скилла**, а не обязательный рецепт: для другого компонента меняются
репозиторий, сборочные команды, набор модулей и харнессы, а этапы, критерии и структура
материалов остаются теми же. Полезен тем, что показывает, где именно возникают
специфичные для компонента грабли (версия Maven, неполная сборка подмодулей, дрейф API
харнессов), и какие цифры считать нормальными.

Компонент: keycloak 26.5.2, коммит `4c5b05789e16ec963e9477ce1f58118ca89c4d6e`,
харнессы `cncf/cncf-fuzzing/projects/keycloak` (8 из 24), Jazzer 0.30.0.

## Образ

`ubuntu:24.04` + `openjdk-17-jdk-headless` + Maven **3.9.9** бинарём с archive.apache.org
(в apt 24.04 только 3.8.7, а Keycloak объявляет `maven.version=3.9.8` и падает на enforcer).
Jazzer и JaCoCo монтируются с хоста (`/src/tools`), в образ не запекаются.

## Сборка

```bash
MODULES="core,common,crypto,saml-core,saml-core-api,services,server-spi,server-spi-private,model,adapters/saml/core"
mvn -B -ntp package dependency:copy-dependencies -pl "$MODULES" -am \
  -Djavac.src.version=17 -Djavac.target.version=17 -DskipTests -Dmaven.test.skip=true \
  -Dgpg.skip -Dmaven.source.skip -DskipExamples -DskipTestsuite -DskipQuarkus
```

Затем jar-ы в `/out/jars` по маске `*/target/keycloak*.jar` (исключая `*/dependency/*`),
зависимости — из `*/target/dependency/*.jar`, плюс mockito/mockwebserver/okhttp/okio/
kotlin-stdlib/junit из Maven Central. Компиляция:

```bash
javac -cp "/out/jars/*:/out/fuzzer-dependencies/*:/src/tools/jazzer_standalone.jar" \
      -d /out/classes /src/harnesses/*Fuzzer.java
```

Обёртка запуска: `java -cp ... com.code_intelligence.jazzer.Jazzer --target_class=<T>
--instrumentation_includes='org.keycloak.**' -rss_limit_mb=3072 -timeout=25
-use_value_profile=1 -print_final_stats=1 -artifact_prefix=... <corpus>`.

## Цифры пилота (TokenVerifierFuzzer, 1 JWT → 190 файлов корпуса, 60 с)

`cov 107 / ft 214`, 24 360 exec/s, `peak_rss 1897 MB` при `-Xmx2g`; плато не достигалось.
JaCoCo на том же корпусе: 3653 класса проанализировано, 14 покрыто, 131 строка.

## Грабли

- **`bash script.sh` игнорирует `#!/bin/bash -eu`.** Без `set -euo pipefail` внутри скрипта
  упавший javac не останавливает сборку: Maven печатает `BUILD SUCCESS`, а `classes/` пуст.
  Запускать `bash -euo pipefail script.sh` и проверять число `.class`.
- **`-pl adapters/saml` не собирает подмодули.** Агрегатор выбирается без детей; нужен
  явный путь `adapters/saml/core`, иначе `org.keycloak.adapters.saml.config.parsers`
  не компилируется.
- **`BaseHelper.java` нужен только `AuthenticatorFuzzer`.** Для SAML/JWT/JOSE/URI/credential
  целей он лишний и тянет `model/jpa` и `model/infinispan`; без него сборка короче на
  несколько минут и ~200 МБ зависимостей.
- **`WebAuthnCredentialProvider` в 26.5.2 требует третий аргумент** — `WebAuthnMetadataService`
  (`new WebAuthnMetadataService()`); иначе `CredentialValidatorFuzzer` не компилируется.
- **Контейнер пишет файлы от root.** Логи/корпус на хосте становятся недоступны пользователю;
  запускать с `--user "$(id -u):$(id -g)"` и монтировать каталоги, принадлежащие пользователю.
- **JaCoCo не подменяет `cov`.** `jacoco.exec` 791 байт — это нормально для корпуса из
  невалидных входов (14 классов), но путать его с покрытием карты AFL/Jazzer нельзя:
  в отчёт идут оба числа, с разными формулировками.
- **Скрипт `coverage.sh` требует распакованных классов** (`classes-all`) для `jacococli report`;
  распаковка всех jar-ов в один каталог делается один раз.

## Повторная кампания на 26.8.2: что меняется

Тег `26.8.2` (коммит `e514445f9dcdabc5bdcf67cab56c1e49655de067`) собирается **теми же** скриптами,
без правок: `mvn package` — `BUILD SUCCESS` за 1:31, все шесть харнессов компилируются без
изменений (дрейфа API нет; коммит харнессов тот же — `8e95f89f`, каталог `projects/keycloak`
не менялся с 2025-02-17). Продуктовые изменения, важные для триажа прошлых находок:

- **SAML 1.1-парсеры выключены по умолчанию.** `SAMLParser` держит их за флагом
  `saml11ParsersEnabled` (`enableSaml11Parsers()`), включает его `SamlService` при
  `enableSaml11=true`, а `SamlProtocolFactory:121` читает эту настройку с дефолтом
  `Boolean.FALSE`. Поэтому документ SAML 1.1 через `SAMLRequestParser` теперь не доходит до
  SAML11-парсеров вообще («Unknown Start Element … Assertion»), а не зависает.
- **Цикл в `SAML11ParserUtil.parseSAML11SubjectConfirmation` исправлен:** на событие, отличное
  от `StartElement`/`EndElement`, бросается `ParsingException` («Unexpected XML Event Type»)
  вместо повторного `peek`. Проверять надо оба уровня — и код, и дефолт настройки.
- **`JWKParser` переписан с `JsonNode` на `RawJsonValue`**, но поведение на неполном JWK то же:
  `createRSAPublicKey` → `Base64Url.decode(jwk.path("n").asText(null))` → NPE. Сигнатуры
  прошлой кампании остаются в силе; номера строк сдвинулись, сверять по методу, а не по строке.
- **Доказательство закрытия дефекта — прогон входов, а не пустой `hangs/`.** Один вход через
  функцию продукта (26.5.2 — 30 с без ответа, 26.8.2 — 381 мс) плюс весь набор `hangs/`
  прошлой кампании, долитый в корпус: 729 входов, 0 таймаутов (`references/corpus-carryover.md`).
- Цифры пилота на 60 с для ориентира: `SamlParser` cov 384, `Jwk` 52, `KeycloakUriBuilder` 342,
  `Jose` 81, `TokenVerifier` 137, `SamlValidationUtil` 291; 18 детекторов Jazzer, 23 источника хуков.
