import json
import tempfile
import unittest
from pathlib import Path

from sekaisync import termindex as ti
from sekaisync.termindex import (
    ExtractionContext,
    TermRecord,
    extract_terms,
    extract_terms_local,
    make_term_id,
    merge_terms,
)


def page(language="ja", text="テストです。", episode=1, **extra):
    return dict(id=f"web:fixture:{language}:event_story:1:{episode}",
                kind="event_story", language=language, text=text, **extra)


class PageEligibilityTest(unittest.TestCase):
    def test_first_invalid_page_is_excluded(self):
        for flags in ({"asset_mismatch": True},
                      {"content_language_mismatch": True},
                      {"untranslated": True}, {"text": "  "}):
            with self.subTest(flags=flags):
                candidate = page()
                candidate.update(flags)
                self.assertEqual(ti.group_pages_by_story([candidate]), {})


class ExtractionContextTest(unittest.TestCase):
    def test_context_requires_explicit_glossary_or_store(self):
        """Pure computation must not read Path('store'): no implicit fallback."""
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            empty = ExtractionContext(store_root=None, glossary=[])
            self.assertEqual(empty.lexicon, {})
            self.assertEqual(empty.character_names, set())

    def test_context_from_store_loads_lexicon(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            from sekaisync.cli import create_demo_store

            create_demo_store(store)
            from sekaisync.config import SekaiSyncConfig
            from sekaisync.fetcher import sync
            sync(SekaiSyncConfig(store_root=store, regions=("demo",), demo=True), ["demo"])
            context = ExtractionContext(store_root=store)
            self.assertTrue(context.lexicon)
            names = {entry["surface"] for entry in context.lexicon.values()}
            self.assertTrue(any("一歌" in n or "星乃" in n for n in names))


class PartialExtractionIsolationTest(unittest.TestCase):
    """Selecting a subset of stories must not rewrite records whose stories
    were not part of the run (P09: 局部操作伤及全库)."""

    def _existing(self):
        return TermRecord(
            id=make_term_id("ja", "未処理用語"),
            canonical="未処理用語",
            source_language="ja",
            kind="term",
            names={"ja": "未処理用語", "zh_hans": "未处理用语", "en": "Untouched"},
            evidence=[
                {"story_key": "event:1:1", "language": "ja",
                 "sentence": "未処理用語だ。", "context": "未処理用語だ。"}
            ],
            source="local",
            trust="C",
            confidence=0.7,
        )

    def test_local_partial_run_keeps_unprocessed_slots(self):
        ja_page = page(text="未処理用語と新しい用語。\n新しい用語はここ。", episode=1)
        records = extract_terms_local(
            [ja_page],
            "ja",
            ["zh_hans"],
            existing=[self._existing()],
            glossary=[],
            do_align=False,
        )
        by_id = {r.id: r for r in records}
        untouched = by_id[make_term_id("ja", "未処理用語")]
        self.assertEqual(untouched.names.get("zh_hans"), "未处理用语")

    def test_llm_partial_run_keeps_unprocessed_slots(self):
        class FakeLLM:
            def chat_json(self, system, user):
                if "terminology extractor" in system:
                    return {"terms": [{"term": "新しい用語", "confidence": 0.9}]}
                return {"translations": []}

        ja_page = page(text="新しい用語について。", episode=9)
        records = extract_terms(
            [ja_page], "ja", [], FakeLLM(), existing=[self._existing()],
        )
        by_id = {r.id: r for r in records}
        untouched = by_id[make_term_id("ja", "未処理用語")]
        self.assertEqual(untouched.names.get("zh_hans"), "未处理用语")
        self.assertEqual(untouched.names.get("en"), "Untouched")

    def test_no_implicit_store_read(self):
        """Running extraction with cwd lacking any store must not crash or
        silently read a store from another working directory."""
        class FakeLLM:
            def chat_json(self, system, user):
                return {"terms": []}

        with tempfile.TemporaryDirectory() as tmp:
            ja_page = page(text="新しい用語です。", episode=2)
            records = extract_terms([ja_page], "ja", [], FakeLLM())
            self.assertEqual(records, [])


class ProposalAndMergeTest(unittest.TestCase):
    def test_invalid_llm_terms(self):
        class Fake:
            def chat_json(self, *args):
                return payload
        for payload in ({"terms": None}, {"terms": {}}, {"terms": "bad"},
                        {"terms": [{"term": "不存在", "confidence": .9}]},
                        *({"terms": [{"term": "テスト", "confidence": c}]} for c in
                          (0, -1, 2, None, True, "0.9", float("nan")))):
            with self.subTest(payload=payload):
                self.assertEqual(ti.extract_terms_from_text("テストです。", "s", "ja", Fake()), [])

    def test_invalid_translations(self):
        class Fake:
            def chat_json(self, *args):
                return payload
        source = TermRecord(id="t", canonical="テスト", source_language="ja",
                            names={"ja": "テスト"}, evidence=[{
                                "story_key": "s", "language": "ja", "sentence": "テストです。"}])
        for payload in ({"translations": None}, {"translations": {}},
                        {"translations": [{"term": "テスト", "languages": [1]}]},
                        {"translations": [{"term": "テスト", "translation": "Invented", "confidence": .9}]},
                        *({"translations": [{"term": "テスト", "translation": "Test", "confidence": c}]}
                          for c in (0, -1, 2, None, True, "0.9", float("nan")))):
            with self.subTest(payload=payload):
                self.assertEqual(ti.translate_terms_for_story([source], "en", "Test here.", Fake()), {})

    def test_merge_leftovers_and_missing_original_evidence(self):
        import copy
        old = TermRecord(id="t", canonical="テスト", source_language="ja",
                         names={"ja": "テスト", "en": "Wrong", "ko": "미확인"},
                         source="llm", trust="C", confidence=.5)
        official = TermRecord(id="t", canonical="テスト", source_language="ja",
                              names={"ja": "テスト", "en": "Correct"},
                              source="official_db", official=True, trust="A")
        for records in ([old, official], [official, old]):
            before = copy.deepcopy(records)
            merged = merge_terms(records)[0]
            self.assertEqual(records, before)
            self.assertEqual(merged.names["en"], "Correct")
            self.assertEqual(merged.names["ko"], "미확인")
            self.assertEqual(merged.trust, "C")
            self.assertFalse(merged.official)
            pending = [ev for ev in merged.evidence if ev.get("status") == "pending"]
            self.assertIn("Wrong", json.dumps(pending))
            self.assertIn("Correct", json.dumps(pending))

    def test_partial_empty_run_does_not_touch_curated_or_official(self):
        import copy
        records = [TermRecord(id="a", canonical="ネットパラダイス", source_language="ja",
                              names={"ja": "ネットパラダイス", "en": "Original"}),
                   TermRecord(id="b", canonical="公式", source_language="ja",
                              names={"ja": "公式", "en": "Official"}, official=True, trust="A")]
        before = copy.deepcopy(records)
        for align in (True, False):
            result = extract_terms_local([], "ja", ["en"], existing=records, glossary=[], do_align=align)
            self.assertEqual(result, before)
            self.assertEqual(records, before)

    def test_context_selection_revision_and_two_story_gate(self):
        class Fake:
            def chat_json(self, system, user):
                if "terminology extractor" in system:
                    return {"terms": [{"term": "アストラタウン", "confidence": .9}]}
                return {"translations": [{"term": "アストラタウン", "translation": "AstraTown", "confidence": .9}]}
        pages = [p for ep in (1, 2) for p in (
            page(text='「アストラタウン」へ行こう。', episode=ep),
            page("en", 'Visit "AstraTown" today.', ep))]
        context = ExtractionContext(glossary=[], source_language="ja", target_languages=("en",),
                                    selected_story_keys=frozenset({"event:1:1"}), input_revision=7)
        one = extract_terms(pages, llm=Fake(), context=context)[0]
        self.assertNotIn("en", one.names)
        self.assertTrue(any(e.get("status") == "pending" for e in one.evidence))
        self.assertTrue(all(e.get("input_revision") == 7 for e in one.evidence))
        two = extract_terms(pages, "ja", ["en"], Fake())[0]
        self.assertEqual(two.names["en"], "AstraTown")
        self.assertEqual({e["story_key"] for e in two.evidence if e.get("language") == "en"},
                         {"event:1:1", "event:1:2"})


if __name__ == "__main__":
    unittest.main()
