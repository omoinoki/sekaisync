"""P13 — immutable raw generations and atomic pointer publishing.

Astra B2/P13. `raw/` region master tables used to be replaced in place by two
directory renames, which has no portable atomicity guarantee. They are now
prepared into a new immutable generation directory and become visible only when
the SQL transaction that commits the derived indexes also moves the
active-generation pointer.

The property that matters: at every failure point a reader sees either a
complete old generation or a complete new one, never a mix.
"""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sekaisync import dbstore
from sekaisync.config import SekaiSyncConfig
from sekaisync.layout import (
    generation_master_dir,
    generations_root,
    region_master_dir,
)
from sekaisync.registry import data_files_for_region, master_dir_for


class _GenerationStore(unittest.TestCase):
    """A tiny two-file 'region' used as a local mirror, so no network runs."""

    TABLES = {"gameCharacters": [{"id": 1, "name": "A"}], "musics": [{"id": 1}]}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_gen_")
        self.root = Path(self._tmp.name)
        self.store = self.root / "store"
        self.mirror = self.root / "mirror"
        (self.mirror / "master").mkdir(parents=True)
        for name, rows in self.TABLES.items():
            (self.mirror / "master" / f"{name}.json").write_text(
                json.dumps(rows), encoding="utf-8"
            )
        self.store.mkdir(parents=True)
        self.config = SekaiSyncConfig(store_root=self.store, regions=("jp",))

    def tearDown(self):
        self._tmp.cleanup()

    def _sync(self, region="jp", mirror=None):
        from sekaisync.fetcher import sync

        return sync(
            self.config,
            [region],
            local_mirrors={region: mirror or self.mirror},
        )

    def _write_mirror(self, name="gameCharacters", rows=None):
        (self.mirror / "master" / f"{name}.json").write_text(
            json.dumps(rows if rows is not None else [{"id": 99, "name": "Z"}]),
            encoding="utf-8",
        )


class GenerationPublishTest(_GenerationStore):
    def test_publish_creates_generation_and_sets_pointer(self):
        result = self._sync()
        pointers = dbstore.active_generations(self.store)
        self.assertIn("jp", pointers)
        generation = pointers["jp"]
        self.assertEqual(result["generation"], generation)
        self.assertTrue(
            generation_master_dir(self.store, "jp", generation).exists(),
            "the published generation directory is missing",
        )

    def test_generation_contains_the_published_files(self):
        self._sync()
        generation = dbstore.active_generations(self.store)["jp"]
        published = generation_master_dir(self.store, "jp", generation) / "master"
        names = sorted(p.name for p in published.glob("*.json"))
        self.assertEqual(names, ["gameCharacters.json", "musics.json"])

    def test_readers_resolve_through_the_published_generation(self):
        self._sync()
        generation = dbstore.active_generations(self.store)["jp"]
        self.assertEqual(
            master_dir_for(self.store, "jp"),
            generation_master_dir(self.store, "jp", generation),
        )
        self.assertTrue(data_files_for_region(self.store, "jp"))

    def test_revision_and_index_commit_with_the_pointer(self):
        self._sync()
        conn = sqlite3.connect(str(dbstore.db_path(self.store)))
        try:
            self.assertGreaterEqual(dbstore.current_revision(conn), 1)
            self.assertGreater(
                conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0], 0
            )
        finally:
            conn.close()
        self.assertTrue(dbstore.active_generations(self.store))

    def test_second_publish_moves_the_pointer(self):
        first = self._sync()["generation"]
        self._write_mirror("gameCharacters", [{"id": 2, "name": "B"}])
        second = self._sync()["generation"]
        self.assertNotEqual(first, second)
        self.assertEqual(dbstore.active_generations(self.store)["jp"], second)
        # The previous generation is retained, so an in-flight reader on it
        # still has its files (Astra: old generations are not reclaimed here).
        self.assertTrue(generation_master_dir(self.store, "jp", first).exists())


class GenerationFailurePointTest(_GenerationStore):
    """At every failure point the store must stay on a complete generation."""

    def _break(self, module_attr, error):
        import sekaisync.fetcher as fetcher

        original = getattr(fetcher, module_attr)

        def boom(*args, **kwargs):
            raise RuntimeError(error)

        setattr(fetcher, module_attr, boom)
        return fetcher, original

    def test_failure_during_prepare_leaves_pointer_unchanged(self):
        first = self._sync()["generation"]
        import sekaisync.fetcher as fetcher

        original = fetcher._fetch_region_into

        def boom(*args, **kwargs):
            raise RuntimeError("simulated fetch failure")

        fetcher._fetch_region_into = boom
        try:
            with self.assertRaises(RuntimeError):
                self._sync()
        finally:
            fetcher._fetch_region_into = original

        self.assertEqual(dbstore.active_generations(self.store)["jp"], first)
        # The partially prepared generation must be cleaned up.
        survivors = sorted(
            p.name for p in generations_root(self.store).glob("*") if p.is_dir()
        )
        self.assertEqual(survivors, [first])
        # And readers still see complete data.
        self.assertTrue(data_files_for_region(self.store, "jp"))

    def test_failure_before_commit_rolls_back_pointer_and_index(self):
        first = self._sync()["generation"]
        entities_before = self._entity_count()

        original_bump = dbstore.bump_revision

        def boom(conn):
            raise RuntimeError("simulated failure after index write")

        dbstore.bump_revision = boom
        try:
            with self.assertRaises(RuntimeError):
                self._sync()
        finally:
            dbstore.bump_revision = original_bump

        self.assertEqual(dbstore.active_generations(self.store)["jp"], first)
        # The index write happened inside the transaction, so it rolled back too.
        self.assertEqual(self._entity_count(), entities_before)
        self.assertTrue(data_files_for_region(self.store, "jp"))

    def _entity_count(self):
        conn = sqlite3.connect(str(dbstore.db_path(self.store)))
        try:
            return conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        finally:
            conn.close()


class LegacyLayoutCompatibilityTest(_GenerationStore):
    """A store that never published still reads the in-place layout."""

    def test_no_pointer_falls_back_to_legacy_dir(self):
        legacy = region_master_dir(self.store, "jp")
        (legacy / "master").mkdir(parents=True, exist_ok=True)
        (legacy / "master" / "gameCharacters.json").write_text(
            json.dumps([{"id": 1, "name": "A"}]), encoding="utf-8"
        )
        self.assertEqual(dbstore.active_generations(self.store), {})
        self.assertEqual(master_dir_for(self.store, "jp"), legacy)
        self.assertTrue(data_files_for_region(self.store, "jp"))

    def test_reading_pointer_never_creates_the_database(self):
        """Asking 'which generation?' must not create a store as a side effect."""
        self.assertFalse(dbstore.db_path(self.store).exists())
        self.assertEqual(dbstore.active_generations(self.store), {})
        self.assertFalse(
            dbstore.db_path(self.store).exists(),
            "reading the generation pointer created the database; a store that "
            "exists but has no meta table is then unopenable",
        )

    def test_unprepared_region_keeps_its_own_data_after_a_publish(self):
        """Publishing one region must not blank another region's files."""
        other = region_master_dir(self.store, "en")
        (other / "master").mkdir(parents=True, exist_ok=True)
        (other / "master" / "musics.json").write_text(
            json.dumps([{"id": 1}]), encoding="utf-8"
        )
        self._sync("jp")
        generation = dbstore.active_generations(self.store)["jp"]
        # en was not part of this publish, so it must still resolve to its own
        # tree rather than to an empty directory inside the new generation.
        self.assertEqual(master_dir_for(self.store, "en"), other)
        self.assertTrue(data_files_for_region(self.store, "en"))
        self.assertNotEqual(
            master_dir_for(self.store, "en"),
            generation_master_dir(self.store, "en", generation),
        )


if __name__ == "__main__":
    unittest.main()
