"""STAGED B2 tests — single-writer lease, atomic publish, honest migration (P13/D13).

Held outside ``tests/`` until the implementation lands so every commit stays
green.  Move to ``tests/test_writer_lease.py`` with the P13 implementation.

Defects covered (findings from reading the source during B0/B1):

1. ``source_migrate.migrate_source_ids`` swallows a failed index rebuild:
       except Exception as exc:  # keep the migration usable
           summary["rebuild"] = {"error": str(exc)}
   A migration whose rebuild failed still returns a normal success summary, so
   a caller cannot tell a complete migration from a half-applied one. Astra
   P13 explicitly rejects "把 replace 出错降成'成功但 errors=[]'".

2. There is no writer lease at all, so two concurrent sync/crawl writers can
   interleave a read-modify-write against the same store.

3. ``write_json_atomic`` does not exist, so authoritative single-file JSON is
   written with a plain ``write_text`` — a crash mid-write leaves a truncated
   authority file with no way to tell it from a complete one.
"""

import json
import tempfile
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

from sekaisync import dbstore
from sekaisync.layout import kb_dir


class WriterLeaseTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_p13_")
        self.store = Path(self._tmp.name) / "store"
        dbstore.initialize(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lease_rejects_a_second_writer_on_the_same_store(self):
        from sekaisync.fetcher import store_writer_lock

        with store_writer_lock(self.store):
            with self.assertRaises(Exception):
                with store_writer_lock(self.store):
                    pass

    def test_different_stores_do_not_block_each_other(self):
        from sekaisync.fetcher import store_writer_lock

        other = Path(self._tmp.name) / "other"
        dbstore.initialize(other)
        with store_writer_lock(self.store):
            with store_writer_lock(other):
                pass

    def test_lease_is_released_after_the_block(self):
        from sekaisync.fetcher import store_writer_lock

        with store_writer_lock(self.store):
            pass
        # Must be re-acquirable once released.
        with store_writer_lock(self.store):
            pass

    def test_lease_is_released_on_exception(self):
        from sekaisync.fetcher import store_writer_lock

        with self.assertRaises(ValueError):
            with store_writer_lock(self.store):
                raise ValueError("boom")
        with store_writer_lock(self.store):
            pass


class WriteJsonAtomicTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_p13w_")
        self.store = Path(self._tmp.name) / "store"
        self.store.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_write_json_atomic_roundtrips(self):
        from sekaisync.filecache import write_json_atomic

        target = kb_dir(self.store) / "consent.json"
        write_json_atomic(target, {"accepted": True, "note": "中文"})
        self.assertEqual(
            json.loads(target.read_text(encoding="utf-8")),
            {"accepted": True, "note": "中文"},
        )

    def test_failed_write_leaves_the_previous_value_intact(self):
        """A crash mid-write must not truncate an authoritative file."""
        from sekaisync.filecache import write_json_atomic

        target = kb_dir(self.store) / "consent.json"
        write_json_atomic(target, {"generation": 1})
        before = target.read_bytes()

        class Boom(Exception):
            pass

        original = json.dumps

        def exploding(*args, **kwargs):
            raise Boom("serialization failed mid-write")

        json.dumps = exploding
        try:
            with self.assertRaises(Boom):
                write_json_atomic(target, {"generation": 2})
        finally:
            json.dumps = original

        self.assertEqual(
            target.read_bytes(),
            before,
            "a failed atomic write damaged the existing authority file",
        )

    def test_no_temp_files_left_behind(self):
        from sekaisync.filecache import write_json_atomic

        target = kb_dir(self.store) / "consent.json"
        write_json_atomic(target, {"a": 1})
        leftovers = [
            p.name
            for p in target.parent.iterdir()
            if p.name != target.name and p.name.startswith(target.name)
        ]
        self.assertEqual(leftovers, [], f"temp files left behind: {leftovers}")


class MigrationHonestyTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_p13m_")
        self.store = Path(self._tmp.name) / "store"
        dbstore.initialize(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_failed_rebuild_is_reported_not_swallowed(self):
        """A migration that could not rebuild must not look like a success."""
        from sekaisync import source_migrate
        from sekaisync import webindex

        original = webindex.rebuild_web_index

        def exploding(*args, **kwargs):
            raise RuntimeError("index rebuild failed")

        webindex.rebuild_web_index = exploding
        try:
            result = source_migrate.rename_legacy_source_ids(
                self.store, rebuild_index=True, dry_run=False
            )
        finally:
            webindex.rebuild_web_index = original

        # Before the fix the failure was recorded only under
        # summary["rebuild"]["error"] while `errors` stayed empty, so the
        # caller saw a normal summary for a half-applied migration.
        self.assertTrue(
            any("rebuild" in str(item.get("stage", "")) for item in result["errors"]),
            f"a failed rebuild was not surfaced in errors: {result.get('errors')!r}",
        )
        self.assertFalse(result["ok"], "a failed rebuild still reported ok=True")

    def test_clean_migration_reports_ok(self):
        """The error surface must not fire on a healthy run."""
        from sekaisync import source_migrate

        result = source_migrate.rename_legacy_source_ids(
            self.store, rebuild_index=False, dry_run=True
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["errors"], [])


class CrossProcessLeaseTest(unittest.TestCase):
    """B8 — the lease must hold against a *separate OS process*, not just a thread.

    An in-process lock proves the lock object works; it cannot prove the
    mechanism a second `sekaisync` invocation would actually hit. This runs a
    real child that acquires the lease and holds it, then asserts a second
    child is refused and that it succeeds once the holder exits.
    """

    CHILD = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from sekaisync.fetcher import store_writer_lock

with store_writer_lock(Path(sys.argv[2])):
    print("ACQUIRED", flush=True)
    import time
    time.sleep(float(sys.argv[3]))
print("RELEASED", flush=True)
'''

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_p13_xproc_")
        self.store = Path(self._tmp.name) / "store"
        dbstore.initialize(self.store)
        self.child = Path(self._tmp.name) / "holder.py"
        self.child.write_text(self.CHILD, encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, hold_seconds):
        return subprocess.Popen(
            [sys.executable, str(self.child), str(Path(__file__).resolve().parent.parent),
             str(self.store), str(hold_seconds)],
            stdout=subprocess.PIPE, text=True, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
            encoding="utf-8", errors="replace",
        )

    def test_a_second_process_is_refused_while_the_first_holds_the_lease(self):
        holder = self._run(2.0)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "ACQUIRED")
            contender = self._run(0.1)
            out, _ = contender.communicate(timeout=60)
            self.assertNotIn("ACQUIRED", out,
                             "a second process acquired the writer lease concurrently")
        finally:
            holder.wait(timeout=60)

    def test_the_lease_is_available_again_once_the_holder_exits(self):
        holder = self._run(0.1)
        self.assertEqual(holder.stdout.readline().strip(), "ACQUIRED")
        self.assertEqual(holder.stdout.readline().strip(), "RELEASED")
        holder.wait(timeout=60)
        late = self._run(0.1)
        out, _ = late.communicate(timeout=60)
        self.assertIn("ACQUIRED", out, "the lease stayed locked after the holder exited")


if __name__ == "__main__":
    unittest.main()
