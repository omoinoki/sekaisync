"""Regression coverage for persisted fact packs in Core request snapshots."""

import tempfile
import unittest
from pathlib import Path

from sekaisync.core import SekaiSyncCore
from sekaisync.factpacks import save_fact_packs
from sekaisync.layout import factpack_path
from sekaisync.models import FactPack


class FactPackSnapshotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="test_core_factpacks_")
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name)
        self.pack = FactPack(
            entity_id="character:1", entity_type="character", language="en",
            text="Stored character pack", raw_json_tokens=20, fact_pack_tokens=5,
        )
        self.path = factpack_path(self.store, "en")
        save_fact_packs([self.pack], self.path)
        self.core = SekaiSyncCore(self.store)

    def test_stored_packs_are_present_in_request_and_stats(self):
        self.assertEqual(self.core.factpacks, [self.pack])
        with self.core.request_view() as view:
            self.assertEqual(view.factpacks, (self.pack,))
            self.assertEqual(self.core.factpacks, (self.pack,))
            self.assertEqual(self.core.store_stats()["fact_packs"], 1)
        self.assertEqual(self.core.refresh()["factpacks"], 1)

    def test_packs_are_detached_and_fixed_until_next_request(self):
        with self.core.request_view() as view:
            self.assertEqual(view.factpacks, (self.pack,))
            view.factpacks[0].text = "caller mutation"
            save_fact_packs([], self.path)
            self.assertEqual(view.factpacks, (self.pack,))
            self.assertEqual(self.core.store_stats()["fact_packs"], 1)
        self.assertEqual(self.core.store_stats()["fact_packs"], 0)

    def test_missing_pack_file_remains_empty(self):
        self.path.unlink()
        with self.core.request_view() as view:
            self.assertEqual(view.factpacks, ())
            self.assertEqual(self.core.store_stats()["fact_packs"], 0)


if __name__ == "__main__":
    unittest.main()
