"""Bounded candidate memory and safe projection publication regressions."""

import contextlib
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sekaisync import dbstore, searchindex
from sekaisync.webindex import save_web_pages
from tests.test_searchindex import _page


class CandidateBudgetTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        if not searchindex.fts5_trigram_available(self.conn):
            self.skipTest("SQLite has no FTS5 trigram tokenizer")
        self.conn.execute("CREATE TABLE pages(rowid INTEGER PRIMARY KEY, key_len INTEGER)")
        self.conn.execute(
            "CREATE VIRTUAL TABLE keys USING fts5(key, content='', detail='none', tokenize='trigram')"
        )

    def add_rows(self, values):
        self.conn.executemany("INSERT INTO pages VALUES(?,?)", [(i, len(s)) for i, s in values])
        self.conn.executemany("INSERT INTO keys(rowid, key) VALUES(?,?)", values)

    def counting_connection(self):
        owner = self
        self.visited = 0

        class Connection:
            def execute(self, sql, params):
                cursor = owner.conn.execute(sql, params)

                class Cursor:
                    def __iter__(self):
                        for row in cursor:
                            owner.visited += 1
                            yield row

                    def close(self):
                        cursor.close()

                return Cursor()

        return Connection()

    def test_broad_trigram_stops_after_budget_plus_one(self):
        self.add_rows([(i, "commontrigram" + str(i)) for i in range(10000)])
        found = searchindex._candidate_rowids(
            self.counting_connection(), "commontrigram", max_candidates=10
        )
        self.assertIsNone(found, "A truncated set would silently lose matching rows")
        self.assertEqual(self.visited, 11)

    def test_broad_length_window_stops_after_budget_plus_one(self):
        self.add_rows([(i, "xy") for i in range(10000)])
        self.assertIsNone(searchindex._candidate_rowids(
            self.counting_connection(), "abcdef", max_candidates=10
        ))
        self.assertEqual(self.visited, 11)

    def test_union_limit_counts_distinct_rows_and_keeps_exact_limit(self):
        self.add_rows([(1, "abcdef"), (2, "abcxyz"), (3, "xy")])
        self.assertEqual(searchindex._candidate_rowids(self.conn, "abcdef", max_candidates=3), {1, 2, 3})
        self.assertIsNone(searchindex._candidate_rowids(self.conn, "abcdef", max_candidates=2))

    def test_zero_budget_is_empty_or_fallback_never_truncated(self):
        self.assertEqual(searchindex._candidate_rowids(self.conn, "abcdef", max_candidates=0), set())
        self.add_rows([(1, "abcdef")])
        self.assertIsNone(searchindex._candidate_rowids(self.conn, "abcdef", max_candidates=0))


class IndexPublicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test_searchindex_bounds_")
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "store"
        dbstore.initialize(self.store)
        save_web_pages(self.store, "altsource_ms", [_page(1, "firststar", "songtext")])
        with sqlite3.connect(":memory:") as probe:
            if not searchindex.fts5_trigram_available(probe):
                self.skipTest("SQLite has no FTS5 trigram tokenizer")
        searchindex.build(self.store)
        self.target = searchindex.index_path(self.store)

    def assert_no_staging(self):
        self.assertEqual(list(self.target.parent.glob(self.target.name + ".*.building")), [])

    def test_failed_rebuild_keeps_previous_index_and_cleans_own_staging(self):
        previous = self.target.read_bytes()
        with patch.object(searchindex, "fts5_trigram_available", return_value=False):
            with self.assertRaises(searchindex.IndexUnavailable):
                searchindex.build(self.store)
        self.assertEqual(self.target.read_bytes(), previous)
        self.assertTrue(searchindex.is_current(self.store)[0])
        self.assert_no_staging()

    def test_failed_replace_never_unlinks_published_index(self):
        previous = self.target.read_bytes()

        def refuse_replace(source, target):
            self.assertTrue(Path(source).is_file())
            self.assertEqual(Path(target).read_bytes(), previous)
            raise PermissionError("target is held by a reader")

        with patch.object(searchindex.os, "replace", side_effect=refuse_replace):
            with self.assertRaises(PermissionError):
                searchindex.build(self.store)
        self.assertEqual(self.target.read_bytes(), previous)
        self.assert_no_staging()

    def test_interleaved_builders_have_independent_staging_files(self):
        staging_paths = []
        mkstemp = tempfile.mkstemp

        def capture_staging(*args, **kwargs):
            result = mkstemp(*args, **kwargs)
            staging_paths.append(result[1])
            return result

        def build_another(_written):
            searchindex.build(self.store)

        with patch.object(searchindex.tempfile, "mkstemp", side_effect=capture_staging):
            searchindex.build(self.store, batch_size=1, progress=build_another)
        self.assertEqual(len(staging_paths), 2)
        self.assertEqual(len(set(staging_paths)), 2)
        self.assertTrue(searchindex.is_current(self.store)[0])
        self.assert_no_staging()

    def test_build_pins_revision_fingerprint_and_pages_during_a_write(self):
        fingerprint = searchindex._page_fingerprint

        def write_after_fingerprint(conn):
            result = fingerprint(conn)
            save_web_pages(self.store, "altsource_ms", [_page(2, "sakurafuture", "new text")])
            return result

        with patch.object(searchindex, "_page_fingerprint", side_effect=write_after_fingerprint):
            built = searchindex.build(self.store)
        self.assertEqual(built["rows"], 1)
        self.assertEqual(built["fingerprint_rows"], 1)
        self.assertFalse(searchindex.is_current(self.store)[0])
        self.assertIsNone(searchindex.candidates(self.store, "sakurafuture"))

    def test_empty_folded_keys_are_counted_without_disabling_index(self):
        save_web_pages(self.store, "altsource_ms", [_page(1, "firststar", "songtext"), _page(2, "", "")])
        result = searchindex.build(self.store)
        self.assertEqual(result["rows"], 1)
        self.assertEqual(result["skipped_empty_keys"], 1)
        self.assertTrue(searchindex.is_current(self.store, deep=False)[0])
        self.assertTrue(searchindex.is_current(self.store)[0])
        self.assertEqual(searchindex.candidates(self.store, "firststar"), {("altsource_ms", "web:altsource_ms:1")})

    def test_folded_empty_long_vowel_prefix_keeps_recall(self):
        save_web_pages(self.store, "altsource_ms", [_page(2, "ー", "")])
        searchindex.build(self.store)
        self.assertTrue(searchindex.is_current(self.store)[0])
        self.assertIsNone(searchindex.candidates(self.store, "ーabcd"))
        from sekaisync.webindex import web_search
        self.assertEqual([row["id"] for row in web_search(self.store, "ーabcd")], ["web:altsource_ms:2"])

    def test_old_handle_is_not_blessed_by_newly_published_header(self):
        old_path = Path(self.tmp.name) / "old_index.db"
        shutil.copyfile(self.target, old_path)
        save_web_pages(self.store, "altsource_ms", [_page(2, "sakurafuture", "new text")])
        searchindex.build(self.store)
        original_open = searchindex._open
        opened = []

        @contextlib.contextmanager
        def open_old_then_current(store):
            opened.append(True)
            if len(opened) == 1:
                conn = sqlite3.connect(str(old_path))
                try:
                    yield conn
                finally:
                    conn.close()
            else:
                with original_open(store) as conn:
                    yield conn

        with patch.object(searchindex, "_open", open_old_then_current):
            self.assertIsNone(searchindex.candidates(self.store, "sakurafuture"))
        self.assertEqual(len(opened), 1)

    def test_index_path_with_uri_characters_is_opened_correctly(self):
        special_store = Path(self.tmp.name) / "store # unicode あ"
        dbstore.initialize(special_store)
        save_web_pages(special_store, "altsource_ms", [_page(3, "startrack", "sample")])
        searchindex.build(special_store)
        self.assertTrue(searchindex.is_current(special_store)[0])
        self.assertEqual(searchindex.candidates(special_store, "startrack"), {("altsource_ms", "web:altsource_ms:3")})


if __name__ == "__main__":
    unittest.main()
