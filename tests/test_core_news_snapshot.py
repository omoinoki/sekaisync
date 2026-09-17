"""News responses must use one publication for items and summary."""

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sekaisync import dbstore, news
from sekaisync.core import SekaiSyncCore


def record(language="ja", text="old"):
    return {"source": "altsource_sv", "source_id": "1", "language": language,
            "title": "Announcement", "text": text}


class CoreNewsSnapshotTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="core_news_")
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name)
        news.save_news([record()], self.store)
        self.core = SekaiSyncCore(self.store)

    def publish(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(news.save_news, [record(text="new"), record("en", "new")],
                        self.store).result(timeout=10)

    def test_direct_news_binds_reader_and_loads_once(self):
        original = news.load_news
        bindings = []

        def read(root):
            bindings.append(dbstore._bound_connection(root) is not None)
            records = original(root)
            self.publish()
            return records

        with patch.object(news, "load_news", side_effect=read) as loader:
            response = self.core.news(language="ja")
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(bindings, [True])
        self.assertEqual(response["count"], 1)
        self.assertEqual(response["matched"], 1)
        self.assertEqual(response["items"][0]["text"], "old")
        self.assertEqual(self.core.news()["count"], 2)

    def test_nested_request_keeps_old_news_until_exit(self):
        with self.core.request_view():
            self.publish()
            response = self.core.news()
            self.assertEqual(response["count"], 1)
            self.assertEqual(response["items"][0]["text"], "old")
        self.assertEqual(self.core.news()["count"], 2)

    def test_summary_uses_unfiltered_records_and_items_are_detached(self):
        self.publish()
        response = self.core.news(limit=1, language="ja")
        self.assertEqual(response["count"], 2)
        self.assertEqual(response["matched"], 1)
        response["items"][0]["text"] = "mutated"
        self.assertEqual(self.core.news(language="ja")["items"][0]["text"], "new")


if __name__ == "__main__":
    unittest.main()
