import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.layout import (
    news_file_path,
    web_consent_path,
    web_index_path,
    web_pages_path,
)
from sekaisync.source_migrate import rename_legacy_source_ids
from sekaisync.sources import SOURCE_MS, SOURCE_MS_TRANSLATION, SOURCE_SV, SOURCE_SV_I18N


def _page(page_id: str, source: str, kind: str = "event_story", auxiliary: bool = False) -> dict:
    return {
        "id": page_id,
        "source": source,
        "url": "https://example.invalid/x",
        "title": "t",
        "language": "ja",
        "kind": kind,
        "text": "hello world",
        "crawled_at": "2026-08-14T00:00:00+00:00",
        "hash": "abc",
        "tos_accepted": True,
        "auxiliary": auxiliary,
        "overlay": auxiliary,
        "translation_source": "i18n" if auxiliary else "",
    }


def _write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


class SourceMigrateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        self.store.mkdir()
        _write(self.store / "manifest.json", {"layout": "v2", "version": 1})
        _write(
            self.store / "kb" / "web" / "altsource" / "pages.json",
            [
                _page("web:altsource:zh-cn:event_story:1:1", "altsource"),
                _page("web:altsource:zh-cn:unit_story:ln_01_01", "altsource"),
            ],
        )
        _write(
            self.store / "cache" / "web" / "altsource" / "02_event_story.json",
            [{"id": "web:altsource:zh-cn:event_story:1:1", "source": "altsource"}],
        )
        _write(
            self.store / "kb" / "web" / "sekai_viewer" / "pages.json",
            [_page("web:sekai_viewer:jp:event_story:2:1", "sekai_viewer")],
        )
        _write(
            self.store / "kb" / "web" / "sekai_viewer_i18n" / "pages.json",
            [_page("web:sekai_viewer_i18n:zh_hans:event_name", "sekai_viewer_i18n", auxiliary=True)],
        )
        _write(
            self.store / "kb" / "web" / "altsource_translation" / "pages.json",
            [_page("web:altsource_translation:zh-cn:event_story:1:1:ja", "altsource_translation", auxiliary=True)],
        )
        _write(
            self.store / "kb" / "web" / "consent.json",
            {"altsource": {"accepted": True}, "sekai_viewer": {"accepted": True}},
        )
        _write(
            self.store / "kb" / "news" / "all.json",
            {
                "version": 1,
                "news": [
                    {
                        "id": "news:ja:sekai_viewer:99",
                        "source": "sekai_viewer",
                        "language": "ja",
                        "title": "t",
                        "text": "t",
                        "url": "https://example.invalid",
                    },
                    {
                        "id": "news:zh_hans:altsource:1",
                        "source": "altsource",
                        "language": "zh_hans",
                        "title": "t",
                        "text": "t",
                        "url": "https://example.invalid",
                    },
                ],
            },
        )

    def test_migration_renames_dirs_records_news_and_consent(self):
        result = rename_legacy_source_ids(self.store)

        self.assertEqual(result["layout"], "v2")
        self.assertEqual(
            result["renamed_dirs"].get("altsource"), SOURCE_MS
        )
        self.assertEqual(
            result["renamed_dirs"].get("sekai_viewer"), SOURCE_SV
        )

        # Dirs renamed, legacy dirs gone.
        self.assertTrue(web_pages_path(self.store, SOURCE_MS).exists())
        self.assertTrue(web_pages_path(self.store, SOURCE_SV).exists())
        self.assertTrue(web_pages_path(self.store, SOURCE_SV_I18N).exists())
        self.assertTrue(web_pages_path(self.store, SOURCE_MS_TRANSLATION).exists())
        self.assertFalse((self.store / "kb" / "web" / "altsource").exists())
        self.assertFalse((self.store / "kb" / "web" / "sekai_viewer").exists())

        # Page records rewritten.
        ms_pages = json.loads(web_pages_path(self.store, SOURCE_MS).read_text(encoding="utf-8"))
        self.assertEqual(ms_pages[0]["id"], "web:altsource_ms:zh-cn:event_story:1:1")
        self.assertEqual(ms_pages[0]["source"], SOURCE_MS)
        sv_pages = json.loads(web_pages_path(self.store, SOURCE_SV).read_text(encoding="utf-8"))
        self.assertEqual(sv_pages[0]["id"], "web:altsource_sv:jp:event_story:2:1")
        self.assertEqual(sv_pages[0]["source"], SOURCE_SV)

        # News records rewritten.
        news = json.loads(news_file_path(self.store).read_text(encoding="utf-8"))["news"]
        by_id = {record["id"]: record for record in news}
        self.assertIn("news:ja:altsource_sv:99", by_id)
        self.assertEqual(by_id["news:ja:altsource_sv:99"]["source"], SOURCE_SV)
        self.assertIn("news:zh_hans:altsource_ms:1", by_id)
        self.assertEqual(by_id["news:zh_hans:altsource_ms:1"]["source"], SOURCE_MS)

        # Consent keys rewritten.
        consent = json.loads(web_consent_path(self.store).read_text(encoding="utf-8"))
        self.assertIn(SOURCE_MS, consent)
        self.assertIn(SOURCE_SV, consent)
        self.assertNotIn("altsource", consent)

        # Index rebuilt under canonical source names.
        index = json.loads(web_index_path(self.store).read_text(encoding="utf-8"))
        self.assertIn(SOURCE_MS, index.get("sources", {}))
        self.assertIn(SOURCE_SV, index.get("sources", {}))

        # Category artifacts regenerated under cache/web/<source>.
        self.assertTrue((self.store / "cache" / "web" / SOURCE_MS / "02_event_story.json").exists())

    def test_migration_is_idempotent(self):
        rename_legacy_source_ids(self.store)
        second = rename_legacy_source_ids(self.store)
        self.assertEqual(second["renamed_dirs"], {})
        self.assertTrue(web_pages_path(self.store, SOURCE_MS).exists())

    def test_migration_patches_single_source_store(self):
        _write(
            self.store / "kb" / "web" / "sekai_viewer" / "pages.json",
            [_page("web:sekai_viewer:jp:event_story:3:1", "sekai_viewer")],
        )
        _write(
            self.store / "kb" / "news" / "ja.json",
            {
                "version": 1,
                "language": "ja",
                "news": [
                    {
                        "id": "news:ja:sekai_viewer:5",
                        "source": "sekai_viewer",
                        "language": "ja",
                        "title": "t",
                        "text": "t",
                        "url": "https://example.invalid",
                    }
                ],
            },
        )
        _write(self.store / "kb" / "web" / "consent.json", {"sekai_viewer": {"accepted": True}})

        result = rename_legacy_source_ids(self.store)

        self.assertEqual(result["layout"], "v2")
        self.assertTrue(web_pages_path(self.store, SOURCE_SV).exists())
        pages = json.loads(web_pages_path(self.store, SOURCE_SV).read_text(encoding="utf-8"))
        self.assertEqual(pages[0]["id"], "web:altsource_sv:jp:event_story:3:1")
        self.assertIn("news:ja:altsource_sv:5", [
            record["id"] for record in json.loads(
                news_file_path(self.store, "ja").read_text(encoding="utf-8")
            )["news"]
        ])


if __name__ == "__main__":
    unittest.main()
