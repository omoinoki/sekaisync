"""P17 regressions: all publications use temporary stores and fake transports."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sekaisync import dbstore, news
from sekaisync.config import SiteSettings, ViewerSettings


def record(**changes):
    item = dict(source="altsource_sv", source_type="sekai_viewer", source_id="1",
                language="ja", title="Same title", text="old", body_available=False)
    item.update(changes)
    return item


class NewsRevisionTests(unittest.TestCase):
    def assert_newer(self, old, new):
        for items in ([old, new], [new, old]):
            self.assertEqual(news.merge_news(items)[0]["text"], "new")

    def test_fetch_time_beats_unchanged_publication_time(self):
        self.assert_newer(record(published_at="2026-09-01", fetched_at="2026-09-10"),
                          record(text="new", published_at="2026-09-01", fetched_at="2026-09-11"))

    def test_new_fetch_beats_changed_attendance_dates(self):
        self.assert_newer(record(published_at="2027-01-01", start_at="2027-01-01", fetched_at="2026-09-10"),
                          record(text="new", published_at="2020-01-01", start_at="2020-01-01", fetched_at="2026-09-11"))

    def test_missing_fetch_time_does_not_beat_known_time(self):
        self.assert_newer(record(), record(text="new", fetched_at="2026-09-11"))

    def test_older_upstream_revision_cannot_win_by_refetch(self):
        self.assert_newer(record(updated_at="2020-01-01", fetched_at="2026-09-12"),
                          record(text="new", updated_at="2026-09-11", fetched_at="2026-09-11"))

    def test_offsets_are_compared_as_instants(self):
        self.assert_newer(record(updated_at="2026-09-11T10:00:00+09:00"),
                          record(text="new", updated_at="2026-09-11T02:00:00Z"))

    def test_numeric_timestamps_and_versions(self):
        self.assert_newer(record(updated_at="9"), record(text="new", updated_at="10"))
        self.assert_newer(record(source_version="9"), record(text="new", source_version="10"))
        self.assert_newer(record(updated_at=1750000000000),
                          record(text="new", updated_at="2026-09-11T02:00:00Z"))

    def test_invalid_revision_falls_back_to_fetch_time(self):
        self.assert_newer(record(updated_at="invalid", fetched_at="2026-09-10"),
                          record(text="new", updated_at="invalid", fetched_at="2026-09-11"))

    def test_backend_equality_is_not_identity_evidence(self):
        self.assertEqual(len(news.merge_news([record(), record(source="another_viewer")])), 2)

    def test_explicit_namespace_proves_mirror_identity(self):
        items = [record(upstream_namespace="official:jp"),
                 record(source="another_viewer", upstream_namespace="official:jp")]
        self.assertEqual(len(news.merge_news(items)), 1)

    def test_identity_key_cannot_collide_on_delimiters(self):
        self.assertNotEqual(news.news_identity_key(record(upstream_namespace="a|b", source_id="c")),
                            news.news_identity_key(record(upstream_namespace="a", source_id="b|c")))


class NewsPublicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"

    def test_absent_read_does_not_create_store(self):
        self.assertEqual(news.load_news(self.root), [])
        self.assertFalse(self.root.exists())

    def test_load_news_is_newest_first_regardless_of_store_order(self):
        """[:limit] consumers (HTTP/MCP news, `news list`) must see the newest
        records first even though the store's physical order is insertion
        order; records without a timestamp sort last."""
        older = record(source_id="1", published_at="2026-01-01T00:00:00+00:00")
        newer = record(source_id="2", language="en", published_at="2026-09-20T03:00:00+00:00")
        undated = record(source_id="3", language="ko")
        news.save_news([older, undated, newer], self.root)
        self.assertEqual(
            [r["source_id"] for r in news.load_news(self.root)],
            ["2", "1", "3"],
            "news must read newest-first; undated records go last",
        )

    def test_snapshot_replace_removes_old_and_empty_languages(self):
        news.save_news([record(), record(language="en")], self.root)
        news.save_news([record(text="new")], self.root)
        self.assertEqual([r["language"] for r in news.load_news(self.root)], ["ja"])
        news.save_news([], self.root)
        self.assertEqual(news.load_news(self.root), [])

    def test_explicit_empty_language_replace_preserves_other_languages(self):
        news.save_news([record(), record(language="en")], self.root)
        news.save_news([], self.root, languages=("ja",))
        self.assertEqual([r["language"] for r in news.load_news(self.root)], ["en"])
        news.save_news([], self.root, languages=())
        self.assertEqual([r["language"] for r in news.load_news(self.root)], ["en"])
        with self.assertRaises(ValueError):
            news.save_news([record()], self.root, languages=("en",))

    def test_publication_has_immutable_manifest_and_sql_revision(self):
        news.save_news([record(), record(language="en")], self.root)
        pointer = news.active_news_generation(self.root)
        manifest = self.root / "kb/news/generations" / pointer["generation"] / "manifest.json"
        before = manifest.read_bytes()
        with dbstore.connect(self.root) as conn:
            revision = dbstore.current_revision(conn)
        news.save_news([record(text="new")], self.root)
        self.assertNotEqual(pointer, news.active_news_generation(self.root))
        self.assertEqual(manifest.read_bytes(), before)
        with dbstore.connect(self.root) as conn:
            self.assertEqual(dbstore.current_revision(conn), revision + 1)

    def test_bound_reader_stays_on_old_generation(self):
        news.save_news([record()], self.root)
        with dbstore.connect(self.root) as reader:
            reader.execute("BEGIN")
            old_revision = dbstore.current_revision(reader)
            news.save_news([record(text="new"), record(language="en")], self.root)
            with dbstore.read_connection(self.root, reader):
                self.assertEqual(news.load_news(self.root), [record()])
                self.assertEqual(news.news_summary(self.root)["count"], 1)
                self.assertEqual(dbstore.current_revision(reader), old_revision)
                with self.assertRaises(RuntimeError):
                    news.save_news([], self.root)
        self.assertEqual(len(news.load_news(self.root)), 2)

    def test_failure_before_pointer_commit_keeps_complete_previous_generation(self):
        news.save_news([record(), record(language="en")], self.root)
        before = news.load_news(self.root)
        with patch.object(dbstore, "bump_revision", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                news.save_news([record(text="new")], self.root)
        self.assertEqual(news.load_news(self.root), before)

    def test_corrupt_generation_fails_closed_not_legacy_fallback(self):
        news.save_news([record()], self.root)
        pointer = news.active_news_generation(self.root)
        root = self.root / "kb/news/generations" / pointer["generation"]
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        filename = next(iter(manifest["files"]))
        (root / filename).write_text('{"news": []}', encoding="utf-8")
        with self.assertRaises(ValueError):
            news.load_news(self.root)

    def test_legacy_import_then_generation_ignores_stale_files(self):
        root = self.root / "kb/news"
        root.mkdir(parents=True)
        (root / "ja.json").write_text(json.dumps({"news": [record()]}), encoding="utf-8")
        self.assertEqual(news.load_news(self.root), [record()])
        news.save_news([], self.root)
        self.assertEqual(news.load_news(self.root), [])

    def test_manifest_is_bound_to_sql_pointer(self):
        news.save_news([record()], self.root)
        pointer = news.active_news_generation(self.root)
        path = self.root / "kb/news/generations" / pointer["generation"] / "manifest.json"
        data = json.loads(path.read_bytes())
        data["files"] = {}
        path.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(ValueError):
            news.load_news(self.root)

    def test_empty_scope_rejects_records_without_creating_store(self):
        with self.assertRaises(ValueError):
            news.save_news([record()], self.root, languages=())
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            news.save_news([record(language="")], self.root, languages=("ja",))
        self.assertFalse(self.root.exists())

    def test_generator_input_and_language_labels_are_contained(self):
        news.save_news((item for item in [record(language="../outside")]), self.root)
        self.assertEqual(news.load_news(self.root)[0]["language"], "../outside")
        self.assertFalse((self.root / "kb/outside.json").exists())

    def test_file_write_failure_does_not_publish_partial_generation(self):
        news.save_news([record()], self.root)
        pointer = news.active_news_generation(self.root)
        with patch.object(news, "_write_generation_json", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                news.save_news([record(text="new")], self.root)
        self.assertEqual(news.active_news_generation(self.root), pointer)
        self.assertEqual(news.load_news(self.root), [record()])

    def test_legacy_read_is_side_effect_free(self):
        root = self.root / "kb/news"
        root.mkdir(parents=True)
        (root / "ja.json").write_text(json.dumps({"news": [record()]}), encoding="utf-8")
        self.assertEqual(news.load_news(self.root), [record()])
        self.assertFalse(dbstore.db_file(self.root).exists())

    def test_summary_can_use_already_loaded_records(self):
        self.assertEqual(news.news_summary(self.root, records=[record()])["count"], 1)
        self.assertFalse(self.root.exists())

    def test_sync_counts_only_fetched_and_stamps_only_fresh_records(self):
        news.save_news([record(source_id="stored", fetched_at="2020-01-01")], self.root)
        sites = (SiteSettings(id="altsource_sv", backend="sekai_viewer",
                              viewer=ViewerSettings(master_base="https://test.invalid")),)
        result = news.sync_news(self.root, regions=iter(("jp",)), sites=sites,
                                sources=("altsource_sv",), fetcher=lambda url: json.dumps([
                                    {"id": 1, "title": "fresh", "startAt": 1000, "updatedAt": 2000}]))
        self.assertEqual(result["fetched"], 1)
        records = {r["source_id"]: r for r in news.load_news(self.root)}
        self.assertEqual(records["stored"]["fetched_at"], "2020-01-01")
        self.assertTrue(records["1"]["fetched_at"])
        self.assertTrue(records["1"]["source_updated_at"])


if __name__ == "__main__":
    unittest.main()
