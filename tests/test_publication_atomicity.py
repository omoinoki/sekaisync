"""P13 regression tests: SQL authority, rollback, and repairable projections."""
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sekaisync import dbstore, fetcher
from sekaisync.config import SekaiSyncConfig
from sekaisync.layout import freshness_path, factpack_path, web_pages_path
from sekaisync.source_migrate import rename_legacy_source_ids


class PublicationAtomicityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = self.root / "store"
        self.mirror = self.root / "mirror"
        (self.mirror / "master").mkdir(parents=True)
        self.config = SekaiSyncConfig(store_root=self.store, regions=("jp",))
        self.mirror_version("old")

    def mirror_version(self, version):
        (self.mirror / "versions.json").write_text(json.dumps({"dataVersion": version}), encoding="utf-8")
        (self.mirror / "master" / "gameCharacters.json").write_text(
            json.dumps([{"id": 1, "name": version}]), encoding="utf-8")

    def sync(self):
        return fetcher.sync(self.config, ("jp",), {"jp": self.mirror})

    def sql_state(self):
        with dbstore.connect(self.store) as conn:
            return (conn.execute("SELECT * FROM meta ORDER BY key").fetchall(),
                    conn.execute("SELECT * FROM entities ORDER BY id").fetchall())

    def test_late_failure_preserves_freshness_and_factpack_bytes(self):
        self.sync()
        paths = [freshness_path(self.store), factpack_path(self.store, "en")]
        before = [path.read_bytes() for path in paths]
        sql_before = self.sql_state()
        self.mirror_version("new")
        with patch.object(dbstore, "bump_revision", side_effect=RuntimeError("late failure")):
            with self.assertRaisesRegex(RuntimeError, "late failure"):
                self.sync()
        self.assertEqual(self.sql_state(), sql_before)
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_freshness_describes_prepared_generation(self):
        self.sync()
        self.mirror_version("new")
        published = self.sync()
        record = json.loads(freshness_path(self.store).read_text(encoding="utf-8"))
        self.assertEqual(record["versions"]["jp"]["dataVersion"], "new")
        self.assertEqual(record["raw_generation"], published["generation"])
        with dbstore.connect(self.store) as conn:
            stored = conn.execute("SELECT value FROM meta WHERE key='freshness'").fetchone()
            self.assertIsNotNone(stored)
            self.assertEqual(json.loads(stored[0]), record)

    def test_projection_failure_leaves_committed_record_and_can_retry(self):
        first = self.sync()["generation"]
        self.mirror_version("new")
        with patch.object(fetcher, "write_json_atomic", side_effect=OSError("projection failure")):
            with self.assertRaisesRegex(OSError, "projection failure"):
                self.sync()
        self.assertNotEqual(dbstore.active_generations(self.store)["jp"], first)
        before = self.sql_state()
        fetcher.project_publication(self.config)
        content = freshness_path(self.store).read_bytes()
        fetcher.project_publication(self.config)
        self.assertEqual(content, freshness_path(self.store).read_bytes())
        self.assertEqual(self.sql_state(), before)


class SourceMigrationAtomicityTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        dbstore.initialize(self.store)

    def put(self, source="sekai_viewer", suffix="1", text="DB authority"):
        item = {"id": f"web:{source}:jp:event_story:{suffix}:1", "source": source,
                "text": text, "language": "ja", "kind": "event_story",
                "original_text": " \t日本語\r\n\x00 ",
                "future_metadata": {"nested": [None, "e\u0301", {"v": 2}]}, "seq": 1}
        dbstore.upsert_web_pages(self.store, source, [item])
        return item

    def rows(self):
        with dbstore.connect(self.store) as conn:
            return conn.execute("SELECT * FROM web_pages ORDER BY source, id").fetchall()

    def test_db_only_and_unknown_metadata_survive_migration_and_rerun(self):
        original = self.put()
        self.assertTrue(rename_legacy_source_ids(self.store)["ok"])
        pages = dbstore.load_web_pages(self.store)["altsource_sv"]
        self.assertEqual(pages[0]["text"], original["text"])
        self.assertEqual(pages[0]["original_text"].encode(), original["original_text"].encode())
        self.assertEqual(pages[0]["future_metadata"], original["future_metadata"])
        before = self.rows()
        self.assertTrue(rename_legacy_source_ids(self.store)["ok"])
        self.assertEqual(self.rows(), before)

    def test_stale_json_never_overwrites_db(self):
        original = self.put()
        stale = dict(original, text="stale JSON")
        path = web_pages_path(self.store, "sekai_viewer")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([stale]), encoding="utf-8")
        result = rename_legacy_source_ids(self.store)
        self.assertTrue(result["ok"], result)
        self.assertEqual(dbstore.load_web_pages(self.store)["altsource_sv"][0]["text"], "DB authority")
        projected = json.loads(web_pages_path(self.store, "altsource_sv").read_text(encoding="utf-8"))
        self.assertEqual(projected[0]["text"], "DB authority")

    def test_collision_reports_conflict_without_deleting_either_record(self):
        self.put()
        self.put("altsource_sv", text="canonical authority")
        before = self.rows()
        result = rename_legacy_source_ids(self.store)
        self.assertFalse(result["ok"])
        self.assertTrue(result["conflicts"])
        self.assertEqual(self.rows(), before)

    def test_failure_after_row_updates_rolls_back_all_rows_and_files(self):
        self.put(suffix="1")
        self.put(suffix="2")
        path = web_pages_path(self.store, "sekai_viewer")
        path.parent.mkdir(parents=True)
        path.write_text("[]", encoding="utf-8")
        before = self.rows()
        with patch.object(dbstore, "bump_revision", side_effect=RuntimeError("migration failure")):
            result = rename_legacy_source_ids(self.store)
        self.assertFalse(result["ok"])
        self.assertEqual(self.rows(), before)
        self.assertEqual(path.read_bytes(), b"[]")
        self.assertFalse(web_pages_path(self.store, "altsource_sv").exists())

    def test_dry_run_reports_db_changes_without_writes(self):
        self.put()
        before = self.rows()
        result = rename_legacy_source_ids(self.store, dry_run=True)
        self.assertGreater(result["db_pages_patched"], 0)
        self.assertEqual(self.rows(), before)


if __name__ == "__main__":
    unittest.main()


class ProcessCrashTest(unittest.TestCase):
    """B8 — a real process killed mid-publication must leave a coherent store.

    The other tests in this file fail a publication from inside the same
    process, which cannot show what happens when the OS takes the process away
    before COMMIT: no ``finally`` runs, no connection is closed cleanly. That
    is the case a reader actually cares about, so it is exercised in a child
    process that dies with ``os._exit`` inside the write path.
    """

    CHILD = r'''
import contextlib
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import sekaisync.dbstore as dbstore
from sekaisync.fetcher import sync
from sekaisync.config import SekaiSyncConfig

real_connect = dbstore.connect

@contextlib.contextmanager
def dying_connect(root):
    """Hand the connection to the caller, then die before it can commit."""
    with real_connect(root) as conn:
        yield conn
    print("STAGED", flush=True)
    os._exit(9)

dbstore.connect = dying_connect
sync(SekaiSyncConfig(store_root=Path(sys.argv[2]), regions=("jp",), demo=False),
     ("jp",), {"jp": Path(sys.argv[3])})
print("COMPLETED", flush=True)
'''

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = self.root / "store"
        self.mirror = self.root / "mirror"
        (self.mirror / "master").mkdir(parents=True)
        self._mirror_version("old")
        self.child = self.root / "crash_child.py"
        self.child.write_text(self.CHILD, encoding="utf-8")

    def _mirror_version(self, version):
        (self.mirror / "versions.json").write_text(
            json.dumps({"dataVersion": version}), encoding="utf-8")
        (self.mirror / "master" / "gameCharacters.json").write_text(
            json.dumps([{"id": 1, "name": version}]), encoding="utf-8")

    def _sync(self):
        from sekaisync.fetcher import sync

        return sync(SekaiSyncConfig(store_root=self.store, regions=("jp",)),
                    ("jp",), {"jp": self.mirror})

    def _revision(self):
        with dbstore.connect(self.store) as conn:
            return dbstore.current_revision(conn)

    def test_process_killed_mid_publish_leaves_a_readable_store(self):
        first = self._sync()
        revision_before = self._revision()

        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run(
            [sys.executable, str(self.child), str(Path(__file__).resolve().parent.parent),
             str(self.store), str(self.mirror)],
            capture_output=True, text=True, env=env, timeout=180,
        )
        # 9 is the deliberate os._exit; anything else means the child failed
        # for an unrelated reason and this test proved nothing.
        self.assertEqual(proc.returncode, 9, f"child did not die as intended: {proc.stderr[-400:]}")
        self.assertIn("STAGED", proc.stdout)

        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(dbstore.current_revision(conn), revision_before,
                             "a killed process left a partially committed revision")
        self.assertEqual(dbstore.active_generations(self.store)["jp"], first["generation"])

    def test_a_successful_sync_still_works_after_the_crash(self):
        self._sync()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        subprocess.run(
            [sys.executable, str(self.child), str(Path(__file__).resolve().parent.parent),
             str(self.store), str(self.mirror)],
            capture_output=True, text=True, env=env, timeout=180,
        )
        # No stale lock, no half-written generation: the next publication
        # commits and moves the generation forward.
        after = self._sync()
        self.assertTrue(after["generation"])
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
