import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.crawler import (
    _crawl_altsource_ms_translation_events,
    _crawl_altsource_sv_i18n,
    parse_altsource_ms_overlay_pages,
    parse_altsource_sv_i18n_pages,
)
from sekaisync.sources import SOURCE_MS_TRANSLATION, SOURCE_SV_I18N
from sekaisync.termindex import load_pages
from sekaisync.webindex import (
    load_web_pages,
    auxiliary_page_summary,
    save_web_pages,
    web_search,
)


def sample_overlay_data(translation_source: str = "official_cn") -> dict:
    return {
        "meta": {"source": translation_source, "version": "1.0"},
        "episodes": {
            "1": {
                "scenarioId": "event_174_01",
                "title": "",
                "talkData": {
                    "\u307f\u306e\u308a": "\u5b9e\u4e43\u7406",
                    "\u3042\u306e\u30cd\u30c3\u30c8\u30d1\u30e9\u30c0\u30a4\u30b9\u3067\u3082\u914d\u4fe1\u3055\u308c\u308b\u304b\u3089": "\u8fd8\u4f1a\u5728\u7f51\u7edc\u5929\u5802\u8fdb\u884c\u76f4\u64ad",
                },
            }
        },
    }


class OverlayCrawlTest(unittest.TestCase):
    def test_altsource_overlay_pages_trust_official_cn_as_b(self):
        pages = parse_altsource_ms_overlay_pages(
            sample_overlay_data(),
            174,
            "zh-cn",
            "https://translation.exmeaning.com/translation/eventStory/event_174.json",
            source_hash="abc",
        )
        self.assertEqual(len(pages), 2)
        by_language = {page.language: page for page in pages}
        self.assertIn("ja", by_language)
        self.assertIn("zh_hans", by_language)
        for page in pages:
            self.assertTrue(page.auxiliary)
            self.assertTrue(page.overlay)
            self.assertEqual(page.source, SOURCE_MS_TRANSLATION)
            self.assertEqual(page.source_language, "ja")
            self.assertEqual(page.translation_source, "official_cn")
            self.assertEqual(page.trust, "B")
            self.assertEqual(page.event_id, 174)
            self.assertEqual(page.episode_no, 1)
        self.assertIn("\u30cd\u30c3\u30c8\u30d1\u30e9\u30c0\u30a4\u30b9", by_language["ja"].text)
        self.assertIn("\u7f51\u7edc\u5929\u5802", by_language["zh_hans"].text)

    def test_altsource_overlay_llm_is_low_trust(self):
        pages = parse_altsource_ms_overlay_pages(
            sample_overlay_data("llm"),
            175,
            "zh-cn",
            "https://translation.exmeaning.com/translation/eventStory/event_175.json",
        )
        self.assertEqual(pages[0].translation_source, "llm")
        self.assertEqual(pages[0].trust, "C")

    def test_crawl_altsource_ms_translation_events_fetches_and_saves(self):
        def fake_fetch(url: str) -> str:
            if "event_174.json" in url:
                return json.dumps(sample_overlay_data(), ensure_ascii=False)
            return "404 page not found"

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = []
            remaining = _crawl_altsource_ms_translation_events(
                "zh-cn",
                fake_fetch,
                pages,
                None,
                0,
                set(),
                [174, 175],
            )
            self.assertIsNone(remaining)
            self.assertEqual(len(pages), 2)
            save_web_pages(store_root, SOURCE_MS_TRANSLATION, pages)
            saved = load_web_pages(store_root)[SOURCE_MS_TRANSLATION]
            self.assertEqual(len(saved), 2)
            self.assertTrue(all(page["auxiliary"] for page in saved))

    def test_crawl_altsource_ms_translation_events_resumes(self):
        def fake_fetch(url: str) -> str:
            if "event_174.json" in url:
                raise AssertionError(f"Resume should skip: {url}")
            return "404 page not found"

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = parse_altsource_ms_overlay_pages(
                sample_overlay_data(),
                174,
                "zh-cn",
                "https://translation.exmeaning.com/translation/eventStory/event_174.json",
            )
            save_web_pages(store_root, SOURCE_MS_TRANSLATION, pages)
            known_ids = {page["id"] for page in load_web_pages(store_root)[SOURCE_MS_TRANSLATION]}
            remaining = _crawl_altsource_ms_translation_events(
                "zh-cn",
                fake_fetch,
                [],
                None,
                0,
                known_ids,
                [174, 175],
            )
            self.assertIsNone(remaining)

    def test_altsource_sv_i18n_pages_are_auxiliary(self):
        pages = parse_altsource_sv_i18n_pages(
            {"1-1": "\u8e3d\u8e3d\u72ec\u884c\uff0c\u4e0d\u89c1\u7e41\u661f", "1-2": "\u548c\u5927\u5bb6\u4e00\u8d77"},
            "zh-CN",
            "event_story_episode_title",
            "https://i18n-json.sekai.best/zh-CN/event_story_episode_title.json",
            source_hash="abc",
        )
        self.assertEqual(len(pages), 1)
        page = pages[0]
        self.assertTrue(page.auxiliary)
        self.assertTrue(page.overlay)
        self.assertEqual(page.source, SOURCE_SV_I18N)
        self.assertEqual(page.language, "zh_hans")
        self.assertEqual(page.kind, "title_overlay")
        self.assertEqual(page.trust, "C")
        self.assertIn("\u8e3d\u8e3d\u72ec\u884c", page.text)

    def test_crawl_altsource_sv_i18n_fetches_and_saves(self):
        def fake_fetch(url: str) -> str:
            if "event_story_episode_title.json" in url:
                return json.dumps(
                    {"1-1": "\u8e3d\u8e3d\u72ec\u884c\uff0c\u4e0d\u89c1\u7e41\u661f"},
                    ensure_ascii=False,
                )
            return "404 page not found"

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = _crawl_altsource_sv_i18n(store_root, fake_fetch, 0, resume=False)
            self.assertEqual(result["source"], SOURCE_SV_I18N)
            self.assertEqual(result["pages"], 5)
            saved = load_web_pages(store_root)[SOURCE_SV_I18N]
            self.assertEqual(len(saved), 5)
            self.assertTrue(all(page["auxiliary"] for page in saved))

    def test_auxiliary_pages_hidden_from_default_search_and_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = parse_altsource_ms_overlay_pages(
                sample_overlay_data(),
                174,
                "zh-cn",
                "https://translation.exmeaning.com/translation/eventStory/event_174.json",
            )
            save_web_pages(store_root, SOURCE_MS_TRANSLATION, pages)
            self.assertEqual(web_search(store_root, "\u7f51\u7edc\u5929\u5802"), [])
            matches = web_search(store_root, "\u7f51\u7edc\u5929\u5802", include_overlay=True)
            self.assertEqual(len(matches), 1)
            self.assertTrue(matches[0]["auxiliary"])
            summary = auxiliary_page_summary(store_root)
            self.assertEqual(summary["count"], 2)
            self.assertEqual(summary["sources"][SOURCE_MS_TRANSLATION], 2)
            self.assertEqual(summary["trust"]["B"], 2)

    def test_load_pages_default_excludes_auxiliary(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = parse_altsource_ms_overlay_pages(
                sample_overlay_data(),
                174,
                "zh-cn",
                "https://translation.exmeaning.com/translation/eventStory/event_174.json",
            )
            save_web_pages(store_root, SOURCE_MS_TRANSLATION, pages)
            self.assertEqual(load_pages(store_root), [])
            self.assertEqual(len(load_pages(store_root, include_overlay=True)), 2)


if __name__ == "__main__":
    unittest.main()
