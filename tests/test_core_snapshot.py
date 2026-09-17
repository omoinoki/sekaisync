"""STAGED B2 tests — request-scoped read snapshot + cache revision (Astra P02/D02).

Held outside ``tests/`` until the implementation lands so every commit stays
green (Astra: no failing tests on a releasable branch, no skip/expectedFailure).
Move to ``tests/test_core_snapshot.py`` together with the P02 implementation.

Reproduced defect (dynamic, this machine):

    second result : {'value': 'old'}   <- stale compute stamped with the NEW version

``_cached_aggregate`` captured ``_data_version`` AFTER ``compute()`` returned,
so a computation that began before a write was filed under the post-write
version. Every later request for that key hit the stale entry and got the old
value — the cache "solidifies" a pre-write answer under a post-write revision.

These tests use ``threading.Event`` to make the interleaving deterministic;
none of them sleeps and hopes (Astra: 不得用 sleep 碰运气验证竞态).
"""

import tempfile
import threading
import unittest
from pathlib import Path

from sekaisync.core import SekaiSyncCore


class CacheRevisionRaceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_p02_")
        self.store = Path(self._tmp.name) / "store"
        self.store.mkdir(parents=True, exist_ok=True)
        self.core = SekaiSyncCore(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_stale_compute_is_not_stamped_with_new_version(self):
        """A compute that started pre-write must not be cached post-write."""
        started = threading.Event()
        release = threading.Event()

        def slow_compute():
            started.set()
            release.wait(5)
            return {"value": "old"}

        out = {}

        def run():
            out["result"] = self.core._cached_aggregate("k", slow_compute)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(started.wait(5), "compute never started")

        # A writer commits while the old computation is still in flight.
        self.core._bump_data_version()
        release.set()
        worker.join(5)

        self.assertEqual(out["result"], {"value": "old"})

        # The next request must observe new facts, not the in-flight result
        # filed under the new version.
        second = self.core._cached_aggregate("k", lambda: {"value": "new"})
        self.assertEqual(
            second,
            {"value": "new"},
            "a stale computation was cached under the new version, so the "
            "cache kept serving pre-write data",
        )

    def test_bump_clears_previous_entries(self):
        self.core._cached_aggregate("k", lambda: {"value": 1})
        self.core._bump_data_version()
        seen = []
        self.core._cached_aggregate("k", lambda: seen.append(1) or {"value": 2})
        self.assertEqual(seen, [1], "cache was not invalidated by a version bump")

    def test_cache_entry_limit_is_bounded(self):
        """Astra P02: 64 aggregate keys, so the cache cannot grow without bound."""
        limit = getattr(self.core, "_cache_max_entries", None)
        self.assertIsNotNone(limit, "no cache bound is declared")
        self.assertEqual(limit, 64)


class ReadSnapshotIsolationTest(unittest.TestCase):
    """P02 — one request fixes one read view."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_p02v_")
        self.store = Path(self._tmp.name) / "store"
        self.store.mkdir(parents=True, exist_ok=True)
        self.core = SekaiSyncCore(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_request_view_exposes_a_fixed_revision(self):
        self.assertTrue(
            hasattr(self.core, "request_view"),
            "request_view() is the P02 entry point for a fixed read snapshot",
        )

    def test_returned_aggregate_is_not_shared_cache_state(self):
        """Mutating a returned dict must not corrupt the cached copy."""
        first = self.core._cached_aggregate("k", lambda: {"nested": {"a": 1}})
        first["nested"]["a"] = 999
        second = self.core._cached_aggregate("k", lambda: {"nested": {"a": 1}})
        self.assertEqual(
            second["nested"]["a"],
            1,
            "callers can mutate cached state through the returned object",
        )


class PublicRequestIsolationTest(unittest.TestCase):
    def setUp(self):
        from sekaisync import dbstore
        from sekaisync.models import Entity
        from sekaisync.termindex import TermRecord
        self.tmp = tempfile.TemporaryDirectory(prefix="test_public_view_")
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        dbstore.initialize(self.store)
        dbstore.save_entities(self.store, [Entity(
            id="character:1", type="character", region="jp", regions=["jp"],
            names={"en": "Needle"}, facts={"value": "old"}, source="master_db:jp")])
        dbstore.upsert_terms(self.store, [TermRecord(
            id="term:1", canonical="Needle", source_language="en", names={"en": "Needle"},
            evidence=[{"story_key": "story:1", "language": "en", "sentence": "old"}])],
            evidence_updates={"term:1": dbstore.EvidenceUpdate(
                mode="replace", items=[{"story_key": "story:1", "language": "en", "sentence": "old"}])})
        self.core = SekaiSyncCore(self.store)

    def test_public_query_is_fixed_across_thread_commit(self):
        from unittest.mock import patch
        from sekaisync import dbstore
        import json
        started, committed = threading.Event(), threading.Event()
        errors = []
        original = self.core.lookup

        def writer():
            try:
                if not started.wait(5):
                    raise AssertionError("reader never reached barrier")
                with dbstore.connect(self.store) as conn:
                    conn.execute("UPDATE entities SET facts_json=?", (json.dumps({"value": "new"}),))
                    conn.execute("UPDATE term_evidence SET sentence='new'")
                    dbstore.bump_revision(conn)
                    conn.commit()
            except BaseException as exc:
                errors.append(exc)
            finally:
                committed.set()

        def blocked_lookup(*args, **kwargs):
            result = original(*args, **kwargs)
            started.set()
            self.assertTrue(committed.wait(5))
            return result

        worker = threading.Thread(target=writer)
        worker.start()
        try:
            with patch.object(self.core, "lookup", side_effect=blocked_lookup):
                old = self.core.query("Needle", include_web=False)
        finally:
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(old["metadata"][0]["facts"]["value"], "old")
        self.assertEqual(old["terms"][0]["evidence"][0]["sentence"], "old")
        new = self.core.query("Needle", include_web=False)
        self.assertEqual(new["metadata"][0]["facts"]["value"], "new")
        self.assertEqual(new["terms"][0]["evidence"][0]["sentence"], "new")

    def test_snapshot_and_returned_mutations_are_detached(self):
        with self.core.request_view() as view:
            view.registry[0].names["en"] = "corrupted"
            view.snapshot.registry[0].facts["value"] = "corrupted"
            row = self.core.lookup("Needle")[0]
            row["names"]["en"] = "corrupted"
            row["facts"]["value"] = "corrupted"
            self.assertEqual(self.core.lookup("Needle")[0]["facts"]["value"], "old")
        self.assertEqual(self.core.lookup("Needle")[0]["names"]["en"], "Needle")

    def test_cross_core_cache_revision(self):
        from sekaisync import dbstore
        other = SekaiSyncCore(self.store)
        self.assertEqual(other.trust_summary()["totals"]["registry"], 1)
        with dbstore.connect(self.store) as conn:
            conn.execute("DELETE FROM entities")
            dbstore.bump_revision(conn)
            conn.commit()
        self.assertEqual(other.trust_summary()["totals"]["registry"], 0)
        self.assertEqual(self.core.lookup("Needle"), [])

    def test_read_entry_skips_legacy_import_and_all_writes(self):
        from sekaisync import dbstore
        from unittest.mock import patch
        with patch.object(dbstore, "import_legacy_domains", side_effect=AssertionError("hidden import")), \
             patch.object(dbstore, "initialize", side_effect=AssertionError("hidden initialization")):
            self.core.lookup("Needle")
            self.core.term_lookup("Needle")
            self.core.refresh()


if __name__ == "__main__":
    unittest.main()
