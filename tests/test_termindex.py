import tempfile
import unittest
from pathlib import Path

from sekaisync.cli import create_demo_store
from sekaisync.config import SekaiSyncConfig
from sekaisync.fetcher import sync
from sekaisync.crawler import parse_altsource_ms_overlay_pages
from sekaisync.webindex import save_web_pages
from sekaisync.termindex import (
    TermRecord,
    extract_terms,
    extract_terms_local,
    build_translation_memory,
    load_pages,
    load_terms,
    lookup_terms,
    make_term_id,
    merge_terms,
    page_story_key,
    save_terms,
    seed_from_glossary,
    _coined_candidate_acceptable,
    term_status,
)


class FakeLLM:
    def chat_json(self, system: str, user: str):
        if "terminology extractor" in system:
            return {
                "terms": [
                    {
                        "term": "ネットパラダイス",
                        "kind": "location",
                        "confidence": 0.95,
                    }
                ]
            }
        if "Target language: zh_hans" in user:
            return {
                "translations": [
                    {"term": "ネットパラダイス", "translation": "网络天堂"}
                ]
            }
        if "Target language: en" in user:
            return {
                "translations": [
                    {"term": "ネットパラダイス", "translation": "NetParadise"}
                ]
            }
        return {"translations": []}


class TermIndexTest(unittest.TestCase):
    def test_page_story_key_parses_event_episode(self):
        page = {
            "id": "web:altsource_ms:event_story:174:1",
            "url": "https://pjsk.moe/zh-cn/story/event/174/1/",
            "kind": "event_story",
        }
        self.assertEqual(page_story_key(page), "event:174:1")

    def test_extract_terms_and_translate_across_languages(self):
        pages = [
            {
                "id": "web:altsource_ms:event_story:174:1:ja",
                "source": "altsource_ms",
                "url": "https://pjsk.moe/ja/story/event/174/1/",
                "title": "活动174 第1话",
                "language": "ja",
                "kind": "event_story",
                "text": "遥：ネットパラダイスに行こう！\n遥：楽しみ！",
            },
            {
                "id": "web:altsource_ms:event_story:174:1:zh",
                "source": "altsource_ms",
                "url": "https://pjsk.moe/zh-cn/story/event/174/1/",
                "title": "活动174 第1话",
                "language": "zh_hans",
                "kind": "event_story",
                "text": "遥：去网络天堂吧！\n遥：真期待！",
            },
            {
                "id": "web:altsource_ms:event_story:174:1:en",
                "source": "altsource_ms",
                "url": "https://pjsk.moe/en/story/event/174/1/",
                "title": "Event 174 Episode 1",
                "language": "en",
                "kind": "event_story",
                "text": "Haruka: Let's go to NetParadise!\nHaruka: I can't wait!",
            },
        ]
        records = extract_terms(pages, "ja", ["zh_hans", "en"], FakeLLM())
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].canonical, "ネットパラダイス")
        self.assertEqual(records[0].names.get("zh_hans"), "网络天堂")
        self.assertEqual(records[0].names.get("en"), "NetParadise")
        self.assertEqual(records[0].evidence[0]["story_key"], "event:174:1")
        self.assertEqual(records[0].trust, "C")

        results = lookup_terms(
            records,
            "ネットパラダイス",
            source_language="ja",
            languages=["ja", "zh_hans", "en"],
        )
        self.assertEqual(results[0]["names"]["zh_hans"], "网络天堂")
        self.assertEqual(results[0]["names"]["en"], "NetParadise")

    def test_save_load_and_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "terms.json"
            records = extract_terms(
                [
                    {
                        "id": "web:altsource_ms:event_story:174:1:ja",
                        "url": "https://pjsk.moe/ja/story/event/174/1/",
                        "language": "ja",
                        "kind": "event_story",
                        "text": "ネットパラダイス",
                    }
                ],
                "ja",
                [],
                FakeLLM(),
            )
            save_terms(records, path)
            loaded = load_terms(path)
            self.assertEqual(loaded[0].canonical, "ネットパラダイス")
            self.assertEqual(loaded[0].trust, "C")
            status = term_status(loaded)
            self.assertEqual(status["terms"], 1)
            self.assertEqual(status["languages"]["ja"], 1)

    def test_load_pages_can_include_overlay_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = parse_altsource_ms_overlay_pages(
                {
                    "meta": {"source": "official_cn", "version": "1.0"},
                    "episodes": {
                        "1": {
                            "scenarioId": "event_174_01",
                            "title": "",
                            "talkData": {
                                "\u30cd\u30c3\u30c8\u30d1\u30e9\u30c0\u30a4\u30b9\u306b\u884c\u3053\u3046": "\u53bb\u7f51\u7edc\u5929\u5802\u5427",
                            },
                        }
                    },
                },
                174,
                "zh-cn",
                "https://translation.exmeaning.com/translation/eventStory/event_174.json",
            )
            save_web_pages(store_root, "altsource_ms_translation", pages)

            pages = load_pages(store_root, include_overlay=True)
            self.assertEqual(len(pages), 2)
            ja_pages = [page for page in pages if page["language"] == "ja"]
            zh_pages = [page for page in pages if page["language"] == "zh_hans"]
            self.assertTrue(ja_pages)
            self.assertTrue(zh_pages)
            self.assertIn("\u30cd\u30c3\u30c8\u30d1\u30e9\u30c0\u30a4\u30b9", ja_pages[0]["text"])
            self.assertIn("\u7f51\u7edc\u5929\u5802", zh_pages[0]["text"])
    def test_seed_from_glossary_marks_official(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            create_demo_store(store_root)
            sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
            seeded = seed_from_glossary(store_root)
            self.assertTrue(seeded)
            self.assertTrue(all(term.official for term in seeded))
            self.assertTrue(all(term.trust in {"A", "D"} for term in seeded))
            names = {name for term in seeded for name in term.names.values()}
            self.assertIn("星乃一歌", names)

    def test_load_pages_uses_store_index(self):
        from sekaisync.models import WebPage
        from sekaisync.webindex import save_web_pages

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:event_story:174:1",
                        source="altsource_ms",
                        url="https://pjsk.moe/zh-cn/story/event/174/1/",
                        title="Event 174",
                        language="zh_hans",
                        kind="event_story",
                        text="网络天堂",
                        crawled_at="2026-08-09T00:00:00+00:00",
                        hash="abc",
                        tos_accepted=True,
                    )
                ],
            )
            pages = load_pages(store_root)
            self.assertEqual(page_story_key(pages[0]), "event:174:1")

    def test_storage_keeps_full_context_but_lookup_truncates(self):
        record = TermRecord(
            id=make_term_id("ja", "ネットパラダイス"),
            canonical="ネットパラダイス",
            source_language="ja",
            kind="location",
            names={"ja": "ネットパラダイス", "zh_hans": "网络天堂"},
            evidence=[{"story_key": "event:174:1", "language": "ja", "sentence": "s", "context": "x" * 1000}],
            source="llm",
            created_at="2026-08-10T00:00:00+00:00",
            confidence=0.9,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "terms.json"
            save_terms([record], path)
            loaded = load_terms(path)
            self.assertEqual(len(loaded[0].evidence[0]["context"]), 1000)

            result = lookup_terms([record], "ネットパラダイス")[0]
            self.assertLessEqual(len(result["evidence"][0]["context"]), 503)

    def test_local_extraction_and_same_position_alignment(self):
        pages = [
            {
                "id": "web:altsource_ms:event_story:174:1:ja",
                "url": "https://pjsk.moe/ja/story/event/174/1/",
                "language": "ja",
                "kind": "event_story",
                "text": "遥：「ネットパラダイス」に行こう！\n遥：楽しみ！",
            },
            {
                "id": "web:altsource_ms:event_story:174:1:zh",
                "url": "https://pjsk.moe/zh-cn/story/event/174/1/",
                "language": "zh_hans",
                "kind": "event_story",
                "text": "遥：去“网络天堂”吧！\n遥：真期待！",
            },
            {
                "id": "web:altsource_ms:event_story:174:1:en",
                "url": "https://pjsk.moe/en/story/event/174/1/",
                "language": "en",
                "kind": "event_story",
                "text": "Haruka: Let's go to NetParadise!\nHaruka: I can't wait!",
            },
        ]
        records = extract_terms_local(pages, "ja", ["zh_hans", "en"])
        by_term = {record.canonical: record for record in records}
        self.assertIn("ネットパラダイス", by_term)
        self.assertEqual(by_term["ネットパラダイス"].names.get("zh_hans"), "网络天堂")
        self.assertEqual(by_term["ネットパラダイス"].names.get("en"), "NetParadise")

    def test_local_coined_alignment_rejects_sentence_phrases(self):
        pages = [
            {
                "id": "web:altsource_ms:event_story:174:1:ja",
                "url": "https://pjsk.moe/ja/story/event/174/1/",
                "language": "ja",
                "kind": "event_story",
                "text": "遥：あのネットパラダイスでも配信されるから。",
            },
            {
                "id": "web:altsource_ms:event_story:174:1:en",
                "url": "https://pjsk.moe/en/story/event/174/1/",
                "language": "en",
                "kind": "event_story",
                "text": "Staff: Thank you all very much for gathering here so early.",
            },
        ]
        records = extract_terms_local(pages, "ja", ["en"])
        by_term = {record.canonical: record for record in records}
        self.assertIn("ネットパラダイス", by_term)
        self.assertNotIn("en", by_term["ネットパラダイス"].names)

    def test_merge_terms_removes_reciprocal_duplicates(self):
        zh_record = TermRecord(
            id="term:zh_hans:今天晚上会吃汉堡肉",
            canonical="今天晚上会吃汉堡肉",
            source_language="zh_hans",
            kind="coined_term",
            names={"zh_hans": "今天晚上会吃汉堡肉", "ja": "ハンバーグ"},
            evidence=[{"story_key": "event:1:2", "language": "zh_hans", "sentence": "s"}],
            source="local",
        )
        ja_record = TermRecord(
            id="term:ja:ハンバーグ",
            canonical="ハンバーグ",
            source_language="ja",
            kind="coined_term",
            names={"ja": "ハンバーグ", "zh_hans": "今天晚上会吃汉堡肉"},
            evidence=[{"story_key": "event:1:2", "language": "ja", "sentence": "s"}],
            source="local",
        )
        merged = merge_terms([zh_record, ja_record])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].source_language, "ja")
        self.assertEqual(len(merged[0].evidence), 2)

    def test_local_extraction_skips_story_index_pages(self):
        pages = [
            {
                "id": "web:altsource_ms:story:index",
                "url": "https://pjsk.moe/zh-cn/story/",
                "language": "zh_hans",
                "kind": "story",
                "text": "浏览 Project SEKAI 活动剧情与卡牌剧情。 Altsource",
            }
        ]
        records = extract_terms_local(pages, "zh_hans", [])
        self.assertEqual(records, [])


    def test_local_alignment_uses_zh_hant_page_for_zh_tw(self):
        pages = [
            {
                "id": "web:altsource_ms:event_story:174:1:ja",
                "url": "https://pjsk.moe/ja/story/event/174/1/",
                "language": "ja",
                "kind": "event_story",
                "text": "遥：ネットパラダイスに行こう！",
            },
            {
                "id": "web:altsource_ms:event_story:174:1:tc",
                "url": "https://pjsk.moe/zh-tw/story/event/174/1/",
                "language": "zh_hant",
                "kind": "event_story",
                "text": "遙：去Net Paradise吧！",
            },
        ]
        records = extract_terms_local(pages, "ja", ["zh_tw"])
        by_term = {record.canonical: record for record in records}
        self.assertEqual(by_term["ネットパラダイス"].names.get("zh_tw"), "Net Paradise")

    def test_coined_candidate_acceptance_rejects_possessive_phrase(self):
        self.assertFalse(_coined_candidate_acceptable("NetParadise's support", "en"))
        self.assertTrue(_coined_candidate_acceptable("NetParadise", "en"))
        self.assertTrue(_coined_candidate_acceptable("Net Paradise", "en"))

    def test_build_translation_memory_maps_network_paradise(self):
        pages = []
        for episode in (1, 2):
            pages.extend([
                {
                    "id": f"web:altsource_ms:event_story:174:{episode}:ja",
                    "url": f"https://pjsk.moe/ja/story/event/174/{episode}/",
                    "language": "ja",
                    "kind": "event_story",
                    "text": "遥：ネットパラダイスに行こう！",
                },
                {
                    "id": f"web:altsource_ms:event_story:174:{episode}:zh",
                    "url": f"https://pjsk.moe/zh-cn/story/event/174/{episode}/",
                    "language": "zh_hans",
                    "kind": "event_story",
                    "text": "遥：去网络天堂吧！",
                },
                {
                    "id": f"web:altsource_ms:event_story:174:{episode}:en",
                    "url": f"https://pjsk.moe/en/story/event/174/{episode}/",
                    "language": "en",
                    "kind": "event_story",
                    "text": "Go to NetParadise!",
                },
            ])
        memory = build_translation_memory(pages, "ja", ["zh_hans", "en"])
        self.assertEqual(memory.get(("ネットパラダイス", "zh_hans")), "网络天堂")
        self.assertEqual(memory.get(("ネットパラダイス", "en")), "NetParadise")


if __name__ == "__main__":
    unittest.main()
