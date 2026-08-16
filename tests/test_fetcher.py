"""Regression tests for the destructive-update safety fixes in fetcher.py."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sekaisync.config import SekaiSyncConfig
from sekaisync.fetcher import fetch_region_from_local, fetch_region_from_tarball
from sekaisync.layout import region_source_dir


class FetcherSafetyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        self.root.mkdir()
        self.config = SekaiSyncConfig(store_root=self.root, regions=("jp",))

    def _write_old_data(self, region: str) -> Path:
        source_dir = region_source_dir(self.root, region)
        source_dir.mkdir(parents=True, exist_ok=True)
        (source_dir / "events.json").write_text(
            json.dumps([{"id": 1, "name": "old"}], ensure_ascii=False),
            encoding="utf-8",
        )
        return source_dir

    def test_tarball_fetch_failure_keeps_existing_data(self):
        source_dir = self._write_old_data("jp")
        with mock.patch("sekaisync.fetcher.download_file", side_effect=OSError("offline")):
            with self.assertRaises(OSError):
                fetch_region_from_tarball("jp", self.config)
        self.assertTrue(source_dir.exists())
        self.assertTrue((source_dir / "events.json").exists())
        self.assertEqual(
            json.loads((source_dir / "events.json").read_text(encoding="utf-8"))[0]["name"],
            "old",
        )
        # No staging/backup leftovers beside the source dir.
        parent = source_dir.parent
        self.assertFalse([p for p in parent.iterdir() if p.name.endswith(".staging")])
        self.assertFalse([p for p in parent.iterdir() if p.name.endswith(".bak")])

    def test_local_fetch_missing_mirror_keeps_existing_data(self):
        source_dir = self._write_old_data("jp")
        with self.assertRaises(FileNotFoundError):
            fetch_region_from_local("jp", self.root / "does-not-exist", self.config)
        self.assertTrue(source_dir.exists())
        self.assertTrue((source_dir / "events.json").exists())

    def test_local_fetch_success_replaces_data(self):
        source_dir = self._write_old_data("jp")
        mirror = self.root / "mirror"
        mirror.mkdir()
        (mirror / "events.json").write_text(
            json.dumps([{"id": 2, "name": "new"}], ensure_ascii=False),
            encoding="utf-8",
        )
        fetch_region_from_local("jp", mirror, self.config)
        self.assertTrue(source_dir.exists())
        self.assertEqual(
            json.loads((source_dir / "events.json").read_text(encoding="utf-8"))[0]["name"],
            "new",
        )


if __name__ == "__main__":
    unittest.main()
