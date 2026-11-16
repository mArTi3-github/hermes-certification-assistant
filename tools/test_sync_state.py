#!/usr/bin/env python3
"""Тесты метки синхронизации: решение о направлении и отпечатки содержимого.

    python3 tools/test_sync_state.py -v

Проверяется то, на чём держится гейт `sync.sh`: отпечаток каталога не зависит от времени и мусора,
а `classify` отвечает правильно во всех комбинациях «профиль / репозиторий / метка».
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sync_state  # noqa: E402


class ClassifyTest(unittest.TestCase):
    """Направление расхождения по трём отпечаткам."""

    def test_equal(self):
        self.assertEqual(sync_state.classify("a", "a", "a", marker_present=True), sync_state.EQUAL)

    def test_profile_untouched_repo_ahead_is_stale(self):
        self.assertEqual(sync_state.classify("repo-new", "mark", "mark", marker_present=True),
                         sync_state.STALE)

    def test_profile_edited_is_diverged(self):
        self.assertEqual(sync_state.classify("repo-new", "edited", "mark", marker_present=True),
                         sync_state.DIVERGED)

    def test_profile_edited_and_repo_standing_still_is_diverged(self):
        self.assertEqual(sync_state.classify("mark", "edited", "mark", marker_present=True),
                         sync_state.DIVERGED)

    def test_no_marker_is_conservative(self):
        # Направление неизвестно: расхождение считается непубликованной правкой, а не отставанием.
        self.assertEqual(sync_state.classify("repo-new", "mark", None, marker_present=False),
                         sync_state.DIVERGED)

    def test_no_marker_but_identical_is_equal(self):
        self.assertEqual(sync_state.classify("same", "same", None, marker_present=False),
                         sync_state.EQUAL)

    def test_skill_absent_in_profile(self):
        self.assertEqual(sync_state.classify("repo", None, None, marker_present=True),
                         sync_state.MISSING_IN_PROFILE)

    def test_skill_absent_in_repo(self):
        self.assertEqual(sync_state.classify(None, "profile", None, marker_present=True),
                         sync_state.NOT_PUBLISHED)

    def test_skill_absent_everywhere(self):
        self.assertEqual(sync_state.classify(None, None, None, marker_present=True), "absent")

    def test_skill_not_in_marker_but_present_in_profile(self):
        # Скилл появился в профиле после синхронизации: в метке его нет — значит, правка в профиле.
        self.assertEqual(sync_state.classify("repo", "profile", None, marker_present=True),
                         sync_state.DIVERGED)


class TreeHashTest(unittest.TestCase):
    """Отпечаток каталога: содержимое, а не время и не мусор."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, rel: str, text: str, mtime: float | None = None) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def test_hash_ignores_mtime(self):
        self.write("SKILL.md", "тело\n", mtime=1_000_000)
        first = sync_state.tree_hash(self.root)
        os.utime(self.root / "SKILL.md", (2_000_000, 2_000_000))
        self.assertEqual(first, sync_state.tree_hash(self.root))

    def test_hash_ignores_pycache_and_pyc(self):
        self.write("SKILL.md", "тело\n")
        clean = sync_state.tree_hash(self.root)
        self.write("__pycache__/x.cpython-312.pyc", "мусор")
        self.write("scripts/helper.pyc", "мусор")
        self.assertEqual(clean, sync_state.tree_hash(self.root))

    def test_hash_detects_content_change(self):
        self.write("SKILL.md", "тело\n")
        before = sync_state.tree_hash(self.root)
        self.write("SKILL.md", "тело и правка\n")
        self.assertNotEqual(before, sync_state.tree_hash(self.root))

    def test_hash_detects_added_and_removed_file(self):
        self.write("SKILL.md", "тело\n")
        before = sync_state.tree_hash(self.root)
        self.write("references/extra.md", "дополнение\n")
        after_add = sync_state.tree_hash(self.root)
        self.assertNotEqual(before, after_add)
        (self.root / "references/extra.md").unlink()
        self.assertEqual(before, sync_state.tree_hash(self.root))

    def test_hash_depends_on_paths_not_only_content(self):
        self.write("a.md", "одинаково\n")
        one = sync_state.tree_hash(self.root)
        (self.root / "a.md").rename(self.root / "b.md")
        self.assertNotEqual(one, sync_state.tree_hash(self.root))

    def test_missing_directory(self):
        self.assertIsNone(sync_state.tree_hash(self.root / "нет-такого"))


class MarkerTest(unittest.TestCase):
    """Метка: чтение, запись, терпимость к форматам и битым файлам."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.profile = Path(self._tmp.name) / "profile"
        (self.profile / "skills").mkdir(parents=True)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "skills").mkdir(parents=True)
        (self.repo / "distribution.yaml").write_text("version: 1.11.0\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def write_marker(self, payload: dict) -> None:
        sync_state.marker_path(self.profile).write_text(json.dumps(payload), encoding="utf-8")

    def test_no_marker(self):
        self.assertIsNone(sync_state.read_marker(self.profile))

    def test_broken_marker_is_treated_as_absent(self):
        sync_state.marker_path(self.profile).write_text("{не json", encoding="utf-8")
        self.assertIsNone(sync_state.read_marker(self.profile))

    def test_foreign_schema_is_treated_as_absent(self):
        self.write_marker({"schema": 99, "skills": {}})
        self.assertIsNone(sync_state.read_marker(self.profile))

    def test_roundtrip(self):
        skill = self.profile / "skills/certification/demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("тело\n", encoding="utf-8")
        marker = sync_state.write_marker(self.profile, self.repo, ["certification/demo"])
        read = sync_state.read_marker(self.profile)
        self.assertIsNotNone(read)
        self.assertEqual(read["version"], "1.11.0")
        self.assertEqual(read["skills"]["certification/demo"],
                         sync_state.tree_hash(skill))
        self.assertEqual(marker["schema"], sync_state.SCHEMA)
        self.assertIn("synced_at", marker)

    def test_marker_hash_accepts_flat_and_dict(self):
        self.write_marker({"schema": sync_state.SCHEMA, "skills": {"a": "hash-a",
                                                                   "b": {"hash": "hash-b"}}})
        marker = sync_state.read_marker(self.profile)
        assert marker is not None
        self.assertEqual(sync_state.marker_hash(marker, "a"), "hash-a")
        self.assertEqual(sync_state.marker_hash(marker, "b"), "hash-b")
        self.assertIsNone(sync_state.marker_hash(marker, "c"))
        self.assertIsNone(sync_state.marker_hash(None, "a"))


class EditedAfterSyncTest(unittest.TestCase):
    """Время — подсказка в отчёте, а не критерий решения."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.skill = Path(self._tmp.name) / "skill"
        self.skill.mkdir()

    def tearDown(self):
        self._tmp.cleanup()

    def marker(self, when: datetime) -> dict:
        return {"schema": sync_state.SCHEMA, "synced_at": when.isoformat(), "skills": {}}

    def touch(self, moment: datetime) -> None:
        path = self.skill / "SKILL.md"
        path.write_text("тело\n", encoding="utf-8")
        stamp = moment.timestamp()
        os.utime(path, (stamp, stamp))

    def test_edit_after_sync(self):
        now = datetime.now(timezone.utc)
        self.touch(now)
        self.assertTrue(sync_state.edited_after_sync(self.skill, self.marker(now - timedelta(hours=1))))

    def test_edit_before_sync(self):
        now = datetime.now(timezone.utc)
        self.touch(now - timedelta(hours=2))
        self.assertFalse(sync_state.edited_after_sync(self.skill, self.marker(now)))

    def test_no_marker_means_no_opinion(self):
        self.touch(datetime.now(timezone.utc))
        self.assertIsNone(sync_state.edited_after_sync(self.skill, None))

    def test_naive_timestamp_is_read_as_utc(self):
        parsed = sync_state.parse_time("2026-11-16T12:00:00")
        assert parsed is not None
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_garbage_timestamp(self):
        self.assertIsNone(sync_state.parse_time("позавчера"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
