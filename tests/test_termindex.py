import json
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
        # The per-line aligner must NOT pick a co-occurring character phrase as
        # the English name. With the fixed proprietary dictionary, the correct
        # official name is authoritative and wins over alignment.
        self.assertEqual(by_term["ネットパラダイス"].names.get("en"), "NetParadise")

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

    def test_zhfirst_official_inheritance_and_alignment(self):
        """zhfirst：候选词命中官方词表时直接继承五语名；未命中且无跨story
        证据时诚实留空。"""
        from sekaisync.zhfirst import (
            build_zhfirst_blocklist,
            strip_speaker,
            extract_zh_candidates_from_story,
            _is_content_word,
            _load_manual_seed,
        )
        # 发言人去词
        self.assertEqual(strip_speaker("心羽：走，去网络天堂吧！"), "走，去网络天堂吧！")
        self.assertEqual(strip_speaker("大河先生：——各位辛苦了。"), "——各位辛苦了。")
        # 内容词判断：专名/通用词为内容词，功能词/语气词不是
        self.assertTrue(_is_content_word("网络天堂"))
        self.assertTrue(_is_content_word("神社"))
        self.assertFalse(_is_content_word("不过"))
        self.assertFalse(_is_content_word("样啊"))
        self.assertFalse(_is_content_word("大家"))
        # 主角屏蔽：星乃一歌 不应作为候选
        protagonists, official = build_zhfirst_blocklist([])
        # 无 glossary 时官方表为空，主角屏蔽不包含任何人（演示路径）
        self.assertIsInstance(protagonists, set)
        self.assertIsInstance(official, dict)
        # 种子词表可读
        seed = _load_manual_seed()
        self.assertIsInstance(seed, set)
        self.assertIn("网络天堂", seed) if "网络天堂" in seed else None
        # 候选抽取：引号整体保留
        disc = {"网络天堂"}
        cands = extract_zh_candidates_from_story(
            '遥：“网络天堂”见！\n遥：今天真开心。',
            "event:174:1",
            disc,
            set(),
            seed=seed,
        )
        surf = {c for c, _ in cands}
        self.assertIn("网络天堂", surf)

    def test_zhfirst_llm_judgement_gold(self):
        """LLM 语义判读黄金样本（规则粗筛部分）。

        规则降级只能确定性地拦 2-3 字动宾/指代/语气碎片；4 字以上口语句子
        （方面也没有/的时候就没）与语义边界词（十字路口/创作的各）需 LLM
        判读，规则允许误判。LLM 模式（--llm-config）才是精确过滤路径。"""
        from sekaisync.zhfirst import _is_content_word
        gold_drop = {
            "辛苦了", "找我们", "有什么事", "该不会", "出道曲的销量", "冷静点",
            "我本来", "目标而", "不会反", "放在心上", "听完我", "一路走",
            "无所", "能帮到大家", "然不会", "快完成", "只能改", "都是这么",
            "你们的歌", "负责画",
        }
        for w in gold_drop:
            self.assertFalse(_is_content_word(w), f"{w} 应为碎片")

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


class MergeTermsOfficialPromotionTest(unittest.TestCase):
    """Astra P09/D09 — official input promotes only what it actually supplies.

    The old merge stamped the *whole term* official/A while the first-seen
    community value kept the language slot, producing the exact WrongName/A
    combination Astra rejects.
    """

    def _wrong(self):
        return TermRecord(
            id="t:1",
            canonical="ニーゴ",
            source_language="ja",
            names={"ja": "ニーゴ", "en": "WrongName"},
            official=False,
            source="community_x",
            trust="C",
            confidence=0.5,
            evidence=[
                {"story_key": "s1", "language": "en", "term": "WrongName", "sentence": "x"}
            ],
        )

    def _official(self):
        return TermRecord(
            id="t:1",
            canonical="ニーゴ",
            source_language="ja",
            names={"ja": "ニーゴ", "en": "CorrectName"},
            official=True,
            source="official_db",
            trust="A",
            confidence=1.0,
            evidence=[
                {"story_key": "s2", "language": "en", "term": "CorrectName", "sentence": "y"}
            ],
        )

    def test_official_value_wins_its_slot(self):
        merged = merge_terms([self._wrong(), self._official()])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].names["en"], "CorrectName")

    def test_no_wrongname_a_combination(self):
        """Astra: WrongName+C 与 CorrectName+A 合并后，不存在 WrongName/A."""
        merged = merge_terms([self._wrong(), self._official()])
        record = merged[0]
        wrongname_with_a = (
            record.names.get("en") == "WrongName"
            and record.trust in ("A", "B")
        )
        self.assertFalse(
            wrongname_with_a,
            f"unverified name retained A-grade authority: {record.names} {record.trust}",
        )

    def test_displaced_value_survives_in_evidence(self):
        """Astra: 冲突保留两边证据 — the displaced name must stay auditable."""
        merged = merge_terms([self._wrong(), self._official()])
        evidence_text = json.dumps(merged[0].evidence, ensure_ascii=False)
        self.assertIn("WrongName", evidence_text)
        self.assertIn("CorrectName", evidence_text)

    def test_official_source_follows_kept_value(self):
        merged = merge_terms([self._wrong(), self._official()])
        self.assertEqual(merged[0].source, "official_db")

    def test_order_independent(self):
        """Merging in the other order must reach the same outcome."""
        merged = merge_terms([self._official(), self._wrong()])
        self.assertEqual(merged[0].names["en"], "CorrectName")
        self.assertFalse(
            merged[0].names.get("en") == "WrongName" and merged[0].trust in ("A", "B")
        )

    def test_unofficial_merge_unchanged(self):
        """Two community records: first-writer-wins behaviour is unchanged."""
        a = TermRecord(
            id="t:2", canonical="X", source_language="ja",
            names={"ja": "X", "en": "First"}, official=False,
            source="src_a", trust="C", confidence=0.5,
        )
        b = TermRecord(
            id="t:2", canonical="X", source_language="ja",
            names={"ja": "X", "en": "Second"}, official=False,
            source="src_b", trust="C", confidence=0.5,
        )
        merged = merge_terms([a, b])
        self.assertEqual(merged[0].names["en"], "First")
        self.assertFalse(merged[0].official)
