import tempfile
import unittest
from pathlib import Path

from sekaisync.layout import (
    cache_dir,
    events_archive_path,
    factpack_path,
    freshness_path,
    glossary_path,
    kb_dir,
    load_manifest,
    news_dir,
    news_file_path,
    progress_path,
    raw_dir,
    registry_path,
    region_master_dir,
    seed_glossary_path,
    terms_path,
    web_consent_path,
    web_index_path,
    web_pages_path,
    write_manifest,
)


class LayoutTest(unittest.TestCase):
    def test_v2_is_the_only_layout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            store.mkdir(parents=True)
            self.assertEqual(kb_dir(store), store / "kb")
            self.assertEqual(cache_dir(store), store / "cache")
            self.assertEqual(raw_dir(store), store / "raw")
            self.assertEqual(registry_path(store), store / "kb" / "registry.json")
            self.assertEqual(glossary_path(store), store / "kb" / "glossary.json")
            self.assertEqual(terms_path(store), store / "kb" / "terms.json")
            self.assertEqual(seed_glossary_path(store), store / "kb" / "seed_glossary.json")
            self.assertEqual(events_archive_path(store), store / "kb" / "events" / "archive.json")
            self.assertEqual(news_dir(store), store / "kb" / "news")
            self.assertEqual(news_file_path(store), store / "kb" / "news" / "all.json")
            self.assertEqual(news_file_path(store, "ja"), store / "kb" / "news" / "ja.json")
            self.assertEqual(web_consent_path(store), store / "kb" / "web" / "consent.json")
            self.assertEqual(web_index_path(store), store / "cache" / "web" / "index.json")
            self.assertEqual(
                web_pages_path(store, "altsource_sv"),
                store / "kb" / "web" / "altsource_sv" / "pages.json",
            )
            self.assertEqual(factpack_path(store, "ja"), store / "cache" / "factpacks" / "ja.json")
            self.assertEqual(freshness_path(store), store / "cache" / "freshness.json")
            self.assertEqual(progress_path(store), store / "cache" / "progress.json")
            self.assertEqual(region_master_dir(store, "jp"), store / "raw" / "jp" / "source")
            self.assertEqual(region_master_dir(store, "demo"), store / "raw" / "demo" / "source")

    def test_write_manifest_records_v2(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            store.mkdir(parents=True)
            path = write_manifest(store, counts={"registry": 3}, notes="n")
            manifest = load_manifest(store)
            self.assertEqual(path, store / "manifest.json")
            self.assertEqual(manifest["layout"], "v2")
            self.assertEqual(manifest["counts"], {"registry": 3})
            self.assertEqual(manifest["notes"], "n")


if __name__ == "__main__":
    unittest.main()
