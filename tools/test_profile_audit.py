#!/usr/bin/env python3
"""Тесты сканера аудита профиля (skills/software-development/profile-audit).

Проверяют на синтетическом комплекте, что каждая проверка находит свой тип проблемы,
не срабатывает на чистых материалах и что режим «инкрементально» уважает журнал сессий.

    python3 tools/test_profile_audit.py -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def scanner_path() -> Path:
    """Сканер ищется в репозитории, а при локальной разработке — в профиле."""
    local = ROOT / "skills/software-development/profile-audit/scripts/audit_scan.py"
    if local.is_file():
        return local
    for candidate in sorted((Path.home() / ".hermes/profiles").glob(
            "*/skills/software-development/profile-audit/scripts/audit_scan.py")):
        return candidate
    raise SystemExit(f"не найден audit_scan.py: ни в {local}, ни в профилях ~/.hermes/profiles")


SCANNER = scanner_path()


def load_scanner():
    spec = importlib.util.spec_from_file_location("audit_scan", SCANNER)
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_scan"] = module  # dataclasses ищут модуль в sys.modules
    spec.loader.exec_module(module)
    return module


scan = load_scanner()


# --- синтетический комплект ---------------------------------------------------

DEMO_SKILL = """---
name: demo-skill
description: "Демо-скилл: пять видов проблем для проверки аудита."
version: 1.0.0
author: Demo Author, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [demo]
    related_skills: [other-skill]
---

# Демо-скилл

## When to Use

- Когда нужен демонстрационный материал.

## Prerequisites

- Прочитать `references/old-guide.md` перед началом.

## Procedure

1. Открыть `references/exists.md`.
2. Запустить `tools/sync.sh --check`.
3. Сверить вывод.
4. Записать результат.

**Режим аудита** — это последовательная проверка материалов профиля с вопросами пользователю.

**Метка синхронизации** — это файл с отпечатками содержимого в каталоге профиля.

Как правило, агент сам догадывается, что метку синхронизации надо обновлять после правок.

Интервал опроса — 30 минут при работе через SSH-туннель к стенду.

Текущая версия схемы — 2.3.1, менять её без релиза нельзя.
"""

OTHER_SKILL = """---
name: other-skill
description: "Второй демо-скилл для проверки перекрёстных ссылок."
version: 1.0.0
author: Demo Author, Hermes Agent
license: MIT
platforms: [linux]
metadata:
  hermes:
    tags: [demo]
---

# Второй скилл

**Режим аудита** — это единичный прогон сканера по одному каталогу без диалога.

Интервал опроса — 60 минут при работе через HTTPS-прокси к стенду.

**Маркер синхронизации** — это отметка о последнем обновлении профиля в каталоге `.sync-state.json`.
"""

README = """# Демо-дистрибутив

Текущая версия — 1.9.0, пример вызова: `tools/sync.sh --check`.

В репозитории 7 скиллов, все они описаны ниже.

Путь к профилю: `/home/demo/.hermes/profiles/demo`.
"""

CHANGELOG = """# Журнал изменений

## 2.3.1 — 2026-11-16

- Первый выпуск демо-комплекта.

## 2.3.0 — 2026-11-01

- Начальная версия.
"""

MEMORY = """Демо-пользователь руководит командой из 10 человек и ведёт три проекта.
§
Демо-пользователь руководит командой из 12 человек, включая подрядчиков.
§
Рабочий каталог проверок — `$TMPDIR/demo`, ссылка на `/home/demo/work` устарела.
§
Рабочий каталог проверок — `$TMPDIR/demo`, ссылка на `/home/demo/work` устарела.
"""

USER = "Демо-пользователь пишет по-русски и ждёт ответы на русском.\n"


def build_fixture(base: Path, *, clean: bool = False) -> tuple[Path, Path]:
    repo = base / "repo"
    profile = base / "profile"
    (repo / "tools").mkdir(parents=True)
    (repo / "skills/certification/demo-skill/references").mkdir(parents=True)
    (repo / "skills/software-development/other-skill").mkdir(parents=True)
    (profile / "skills").mkdir(parents=True)
    (profile / "memories").mkdir(parents=True)

    (repo / "distribution.yaml").write_text("name: demo\nversion: 2.3.1\n", encoding="utf-8")
    (repo / "publish.list").write_text("certification/demo-skill\n", encoding="utf-8")
    (repo / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (repo / "tools/sync.sh").write_text(
        "#!/usr/bin/env bash\n# --check --force\nset -euo pipefail\necho sync\n", encoding="utf-8")
    (repo / "tools/publish_check.py").write_text("print('ok')\n", encoding="utf-8")

    if clean:
        readme = "# Демо-дистрибутив\n\nВерсия — 2.3.1, пример вызова: `tools/sync.sh --check`.\n"
        demo = DEMO_SKILL.replace("- Прочитать `references/old-guide.md` перед началом.",
                                  "- Прочитать `references/exists.md` перед началом.")
        demo = demo.replace(
            "**Метка синхронизации** — это файл с отпечатками содержимого в каталоге профиля.\n\n"
            "Как правило, агент сам догадывается, что метку синхронизации надо обновлять после правок.\n",
            "Метку синхронизации обновляет sync.sh после копирования файлов.\n")
        demo = demo.replace("Интервал опроса — 30 минут", "Интервал опроса — 15 минут")
        demo = demo.replace("**Режим аудита**", "**Режим проверки**")
        demo = demo.replace("Текущая версия схемы — 2.3.1", "Версия схемы — 2.3.1")
        other = OTHER_SKILL.replace("Интервал опроса — 60 минут", "Интервал опроса — 15 минут")
        other = other.replace(
            "**Режим проверки** — это единичный прогон сканера по одному каталогу без диалога.\n\n", "")
        other = other.replace("**Режим аудита** — это единичный прогон сканера по одному каталогу без диалога.\n\n", "")
        other = other.replace("**Маркер синхронизации** — это отметка о последнем обновлении профиля "
                              "в каталоге `.sync-state.json`.",
                              "**Метка синхронизации** — это файл с отпечатками, его пишет sync.sh.")
        memory = "Демо-пользователь пишет по-русски.\n"
        user = USER
    else:
        readme, demo, other, memory, user = README, DEMO_SKILL, OTHER_SKILL, MEMORY, USER

    (repo / "README.md").write_text(readme, encoding="utf-8")
    (repo / "skills/certification/demo-skill/SKILL.md").write_text(demo, encoding="utf-8")
    (repo / "skills/certification/demo-skill/references/exists.md").write_text(
        "# Существующий файл\n\n- Пункт.\n", encoding="utf-8")
    (repo / "skills/software-development/other-skill/SKILL.md").write_text(other, encoding="utf-8")

    (profile / "config.yaml").write_text(
        "memory:\n  memory_char_limit: 300\n  user_char_limit: 400\n", encoding="utf-8")
    (profile / "memories/MEMORY.md").write_text(memory, encoding="utf-8")
    (profile / "memories/USER.md").write_text(user, encoding="utf-8")
    return repo, profile


def run_scan(repo: Path, profile: Path, *extra: str) -> tuple[dict, str]:
    out = repo / "audit.json"
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = scan.main(["--repo", str(repo), "--profile", str(profile), "--json",
                          "--out", str(out), *extra])
    payload = json.loads(out.read_text(encoding="utf-8"))
    payload["_code"] = code
    return payload, stderr.getvalue()


def titles(payload: dict, check: str | None = None, cls: str | None = None) -> list[str]:
    return [f["title"] for f in payload["findings"]
            if (check is None or f["check"] == check) and (cls is None or f["cls"] == cls)]


class AuditScanTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- пять типов проблем из постановки -------------------------------------

    def test_factual_contradiction_detected(self) -> None:
        repo, profile = build_fixture(self.base, clean=False)
        payload, _ = run_scan(repo, profile)
        self.assertEqual(payload["_code"], 1)
        self.assertTrue(titles(payload, "counts"), "не найдено расхождение в количестве")
        self.assertTrue(any("1.9.0" in t for t in titles(payload, "versions")),
                        "не найдена устаревшая версия в примере")
        self.assertIn("factual", {f["cls"] for f in payload["findings"]})

    def test_missing_reference_detected(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        self.assertTrue(any("old-guide.md" in t for t in titles(payload, "paths")))
        self.assertFalse(any("exists.md" in t for t in titles(payload, "paths")),
                         "существующий файл помечен отсутствующим")

    def test_contextual_divergence_detected(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        duplicates = [f for f in payload["findings"] if f["check"] == "duplicates"]
        self.assertTrue(duplicates, "не найдено расхождение в похожих правилах")
        self.assertEqual(duplicates[0]["cls"], "contextual")
        self.assertIn("30", " ".join(f["question"] for f in duplicates) + " ".join(
            e["quote"] for f in duplicates for e in f["evidence"]))

    def test_hedged_assumption_detected(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        self.assertTrue(any("как правило" in t.lower() for t in titles(payload, "hedges")),
                        "не найдена осторожная формулировка")
        self.assertIn("assumption", {f["cls"] for f in payload["findings"]})

    def test_terminology_ambiguity_detected(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        found = [t for t in titles(payload, "terminology")]
        self.assertTrue(any("Режим аудита" in t for t in found),
                        f"не найдено двойное определение термина: {found}")
        self.assertTrue(any("метка" in t.lower() and "маркер" in t.lower() for t in found),
                        f"не найдено расхождение названий: {found}")

    def test_temporal_claim_detected(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        self.assertTrue(titles(payload, "temporal"), "не найдено «текущее» рядом с версией")

    def test_machine_local_path_detected(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        self.assertTrue(any("/home/demo" in e["quote"] for f in payload["findings"]
                            for e in f["evidence"]))

    # -- память ----------------------------------------------------------------

    def test_memory_conflict_and_duplicate(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile, "--memory")
        memory_titles = titles(payload, "memory")
        self.assertTrue(any("разными числами" in t for t in memory_titles),
                        f"не найдено расхождение чисел в памяти: {memory_titles}")
        self.assertTrue(any("дважды" in t for t in memory_titles),
                        f"не найден дубль записи: {memory_titles}")
        self.assertTrue(any("символов" in t for t in memory_titles),
                        "не найдено предупреждение о бюджете памяти")

    def test_memory_not_scanned_without_flag(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        self.assertFalse(titles(payload, "memory"))

    # -- режимы ----------------------------------------------------------------

    def test_topic_filter(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile, "--topic", "метк")
        self.assertTrue(payload["findings"])
        for finding in payload["findings"]:
            haystack = finding["title"] + " ".join(e["quote"] for e in finding["evidence"])
            self.assertIn("метк", haystack.lower())

    def test_incremental_skips_resolved(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        first = payload["findings"][0]
        ledger = profile / ".audit/ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": "2026-11-16T12:00:00+00:00", "fingerprint": first["fingerprint"],
                  "status": "resolved", "answer": "решено в прошлой сессии"}
        ledger.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        payload2, _ = run_scan(repo, profile, "--mode", "incremental")
        self.assertNotIn(first["fingerprint"],
                         [f["fingerprint"] for f in payload2["findings"]])

    def test_incremental_shows_previous_answer(self) -> None:
        repo, profile = build_fixture(self.base)
        payload, _ = run_scan(repo, profile)
        target = payload["findings"][0]
        ledger = profile / ".audit/ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": "2026-11-16T12:00:00+00:00", "fingerprint": target["fingerprint"],
                  "status": "open", "answer": "ждём решения владельца"}
        ledger.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

        payload2, _ = run_scan(repo, profile, "--mode", "incremental")
        match = [f for f in payload2["findings"] if f["fingerprint"] == target["fingerprint"]]
        self.assertTrue(match, "находка с открытым вопросом пропала")
        self.assertEqual(match[0]["previously_resolved"]["answer"], "ждём решения владельца")

    # -- отпечатки находок -----------------------------------------------------

    def test_fingerprint_independent_of_line_number(self) -> None:
        repo, profile = build_fixture(self.base)
        before, _ = run_scan(repo, profile)
        target = [f for f in before["findings"]
                  if f["check"] == "paths" and "old-guide" in f["title"]][0]
        skill = repo / "skills/certification/demo-skill/SKILL.md"
        skill.write_text("# Заголовок\n\nНовый абзац.\n\n" + skill.read_text(encoding="utf-8"),
                         encoding="utf-8")
        after, _ = run_scan(repo, profile)
        match = [f for f in after["findings"] if "old-guide" in f["title"]][0]
        self.assertEqual(target["fingerprint"], match["fingerprint"],
                         "сдвиг строк меняет отпечаток: разобранная находка вернётся")
        self.assertNotEqual(target["evidence"][0]["line"], match["evidence"][0]["line"],
                            "строки не сдвинулись — тест ничего не проверяет")

    def test_fingerprint_changes_when_evidence_changes(self) -> None:
        repo, profile = build_fixture(self.base)
        before, _ = run_scan(repo, profile)
        target = [f for f in before["findings"]
                  if f["check"] == "paths" and "old-guide" in f["title"]][0]
        skill = repo / "skills/certification/demo-skill/SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8").replace("old-guide.md", "older-guide.md"),
                         encoding="utf-8")
        after, _ = run_scan(repo, profile)
        match = [f for f in after["findings"] if "older-guide" in f["title"]][0]
        self.assertNotEqual(target["fingerprint"], match["fingerprint"],
                            "изменившаяся улика сохранила отпечаток — вопрос не зададут заново")

    def test_incremental_survives_unrelated_edits(self) -> None:
        repo, profile = build_fixture(self.base)
        before, _ = run_scan(repo, profile)
        target = [f for f in before["findings"]
                  if f["check"] == "paths" and "old-guide" in f["title"]][0]
        ledger = profile / ".audit/ledger.jsonl"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps({"ts": "2026-11-16T12:00:00+00:00",
                                      "fingerprint": target["fingerprint"],
                                      "status": "resolved", "answer": "переименован"},
                                     ensure_ascii=False) + "\n", encoding="utf-8")
        skill = repo / "skills/certification/demo-skill/SKILL.md"
        skill.write_text("\n\n" + skill.read_text(encoding="utf-8"), encoding="utf-8")
        after, _ = run_scan(repo, profile, "--mode", "incremental")
        self.assertNotIn(target["fingerprint"], {f["fingerprint"] for f in after["findings"]},
                         "после правки выше по файлу разобранная находка вернулась")

    # -- ложные срабатывания ---------------------------------------------------

    def test_clean_materials_have_no_findings(self) -> None:
        repo, profile = build_fixture(self.base, clean=True)
        payload, _ = run_scan(repo, profile, "--memory")
        self.assertEqual(payload["findings"], [],
                         f"на чистых материалах есть находки: {titles(payload)}")
        self.assertEqual(payload["_code"], 0)

    def test_markdown_report_renders(self) -> None:
        repo, profile = build_fixture(self.base)
        out = repo / "audit.md"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            scan.main(["--repo", str(repo), "--profile", str(profile), "--out", str(out)])
        text = out.read_text(encoding="utf-8")
        self.assertIn("# Аудит материалов профиля", text)
        self.assertIn("## Проверки без находок", text)
        self.assertIn("## Ограничения", text)
        self.assertIn("**Вопрос.**", text)

    def test_missing_repo_exit_code(self) -> None:
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = scan.main(["--repo", str(self.base / "нет"), "--profile", str(self.base)])
        self.assertEqual(code, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
