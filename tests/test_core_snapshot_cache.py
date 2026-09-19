"""0.4.0 perf remediation: revision-keyed snapshot cache + O(1) ready().

The 0.4.0-alpha refactor made every ``request_scoped`` method re-read the SQL
projections (~70k entities) and deep-copy them per access, pushing ``lookup``
past 20s and ``/health`` past 18s on the real store. These tests pin the new
invariants without weakening the P02 isolation contracts:

- the SQL projections load once per (revision, data version), not per request;
- a committed revision bump, or ``refresh()``, reloads within one process;
- the fact-pack file is never pinned by the projection cache (it is external
  and not revision-atomic);
- ``ready()`` answers without entering a request view;
- lookup results never alias the shared snapshot state.

No real store, no sleeps, deterministic.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sekaisync import dbstore
from sekaisync.core import SekaiSyncCore
from sekaisync.factpacks import save_fact_packs
from sekaisync.layout import factpack_path
from sekaisync.models import Entity, FactPack
from sekaisync.termindex import TermRecord


def _seed_entity(facts: dict) -> Entity:
    return Entity(
        id="character:1", type="character", region="jp", regions=["jp"],
        names={"en": "Needle"}, facts=facts, source="master_db:jp",
    )


class SnapshotProjectionCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test_snapcache_")
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        dbstore.initialize(self.store)
        dbstore.save_entities(self.store, [_seed_entity({"value": "old"})])
        dbstore.upsert_terms(self.store, [TermRecord(
            id="term:1", canonical="Needle", source_language="en", names={"en": "Needle"},
            evidence=[])])
        self.core = SekaiSyncCore(self.store)
        self.loads = 0
        real_load = dbstore.load_entities

        def counting_load(store_root):
            self.loads += 1
            return real_load(store_root)

        patcher = patch.object(dbstore, "load_entities", side_effect=counting_load)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_projection_loads_once_per_revision(self):
        with self.core.request_view():
            pass
        first = self.loads
        self.assertGreaterEqual(first, 1)
        with self.core.request_view():
            pass
        with self.core.request_view():
            pass
        self.assertEqual(
            self.loads, first,
            "a second view at the same revision re-read the SQL projections",
        )

    def test_revision_bump_reloads_and_is_served(self):
        with self.core.request_view():
            pass
        first = self.loads
        with dbstore.connect(self.store) as conn:
            conn.execute(
                "UPDATE entities SET facts_json=? WHERE id=?",
                ('{"value": "new"}', "character:1"),
            )
            dbstore.bump_revision(conn)
            conn.commit()
        with self.core.request_view() as view:
            pass
        self.assertGreater(
            self.loads, first, "a committed revision bump did not reload the projections"
        )
        self.assertEqual(view.snapshot.raw("registry")[0].facts, {"value": "new"})
        self.assertEqual(self.core.lookup("Needle")[0]["facts"]["value"], "new")

    def test_refresh_forces_reload_at_same_revision(self):
        with self.core.request_view():
            pass
        first = self.loads
        self.core.refresh()
        self.assertGreater(
            self.loads, first, "refresh() did not force a projection reload"
        )

    def test_factpack_file_change_is_seen_without_revision_bump(self):
        def pack(text: str) -> FactPack:
            return FactPack(
                entity_id="character:1", entity_type="character", language="en",
                text=text, raw_json_tokens=1, fact_pack_tokens=1,
            )

        path = factpack_path(self.store, "en")
        save_fact_packs([pack("v1")], path)
        self.core.refresh()
        with self.core.request_view():
            self.assertEqual(self.core.factpacks[0].text, "v1")
        save_fact_packs([pack("v2")], path)  # external file: no revision bump
        with self.core.request_view():
            self.assertEqual(
                self.core.factpacks[0].text, "v2",
                "the projection cache pinned the external fact-pack file",
            )


class ReadyO1Test(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test_ready_o1_")
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "store"
        dbstore.initialize(self.store)
        dbstore.save_entities(self.store, [_seed_entity({"value": "old"})])
        self.core = SekaiSyncCore(self.store)

    def test_ready_does_not_enter_a_request_view(self):
        with patch.object(
            SekaiSyncCore, "load_snapshot",
            side_effect=AssertionError("ready() must not load a snapshot"),
        ):
            self.assertTrue(self.core.ready())

    def test_ready_falls_back_to_sql_for_late_synced_store(self):
        late = Path(self.tmp.name) / "late_store"
        dbstore.initialize(late)
        core = SekaiSyncCore(late)  # started against an empty store
        self.assertFalse(core.ready())
        dbstore.save_entities(late, [_seed_entity({"value": "late"})])
        with dbstore.connect(late) as conn:
            dbstore.bump_revision(conn)
            conn.commit()
        self.assertTrue(
            core.ready(),
            "a store synced after server start must report ready without refresh()",
        )


class SharedSnapshotDetachTest(unittest.TestCase):
    """The internal shared-collection path must still never alias results."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test_shared_detach_")
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name)
        dbstore.initialize(self.store)
        dbstore.save_entities(self.store, [_seed_entity({"value": "old"})])
        self.core = SekaiSyncCore(self.store)

    def test_lookup_results_do_not_alias_snapshot_state(self):
        row = self.core.lookup("Needle")[0]
        row["names"]["en"] = "mutated"
        row["facts"]["value"] = "mutated"
        with self.core.request_view() as view:
            self.assertEqual(view.snapshot.raw("registry")[0].names["en"], "Needle")
            self.assertEqual(view.snapshot.raw("registry")[0].facts, {"value": "old"})
        self.assertEqual(self.core.lookup("Needle")[0]["names"]["en"], "Needle")


if __name__ == "__main__":
    unittest.main()
