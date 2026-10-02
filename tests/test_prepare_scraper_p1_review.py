"""Fail-fast checks for the isolated occurrence review preparation helper."""
from pathlib import Path
import tempfile
import unittest

from scripts import prepare_scraper_p1_review as prepare
from sekaisync import agent_packets as ap
from sekaisync import agent_review as ar


class PrepareOccurrenceReviewTests(unittest.TestCase):
    def test_repeated_surface_requires_a_semantic_phrase(self):
        view = dict(text="Speaker:\u6211\u7684\u8072\u97f3\u8ddf\u5927\u5bb6\u7684\u8072\u97f3", start=10)
        with self.assertRaisesRegex(ValueError, "explicit semantic disambiguation"):
            prepare._unique_segments(view, "\u8072\u97f3")
        self.assertEqual(prepare._unique_segments(view, "\u8072\u97f3", "\u5927\u5bb6\u7684\u8072\u97f3"),
                         [dict(start=26, end=28, exact="\u8072\u97f3")])

    def test_non_bmp_offsets_remain_code_points(self):
        view = dict(text="Speaker:\U0001f3b9\u8072\u97f3", start=100)
        self.assertEqual(prepare._unique_segments(view, "\u8072\u97f3"),
                         [dict(start=109, end=111, exact="\u8072\u97f3")])

    def test_ambiguous_semantic_phrase_is_not_implicitly_first(self):
        view = dict(text="Speaker:\u5927\u5bb6\u7684\u8072\u97f3\u4e5f\u662f\u5927\u5bb6\u7684\u8072\u97f3", start=0)
        with self.assertRaisesRegex(ValueError, "semantic phrase does not select one"):
            prepare._unique_segments(view, "\u8072\u97f3", "\u5927\u5bb6\u7684\u8072\u97f3")

    def test_speaker_label_is_not_lexical_evidence(self):
        with self.assertRaisesRegex(ValueError, "0 hits"):
            prepare._unique_segments(dict(text="Emu:Hello!", start=0), "Emu")

    def test_contextual_sound_senses_are_separate(self):
        music = prepare._review_sense("\u58f0\u97f3", "span:2ea0b789b42701efcefdabcb")
        opinion = prepare._review_sense("\u58f0\u97f3", "span:962642c0d3b928545cdc81e6")
        self.assertNotEqual(music, opinion)
        self.assertEqual(music[0], "musical_sound")
        self.assertEqual(opinion[0], "opposing_opinions")

    def test_changed_raw_context_fails_before_preparation(self):
        source = dict(source="fixture", page_id="source", language="zh_hans", sha256="a" * 64,
                      start=0, end=10, text="Speaker:\u8072\u97f3", complete=True)
        target = dict(source="fixture", page_id="target", language="en", sha256="b" * 64,
                      start=0, end=13, text="Speaker:sound", complete=True)
        row = dict(id="span:fixture", story_key="event:1:1", source=source, targets={"en": target},
                   search_text="\u8072\u97f3", search_unwrapped="\u8072\u97f3")
        scope = dict(source_language="zh_hans", windows=[row])
        scope_id = ap._digest(scope)
        item = ap._translation_item(scope_id, scope, "\u8072\u97f3", "en")
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary)
            ar._write_json(ap._scope_path(store, scope_id), scope)
            original = store / "original.txt"
            ar._atomic_write_text(original, ar.render_item(item))
            restored = prepare.restore_translation_items(store, original)
            self.assertEqual([entry.id for entry in restored], [item.id])
            ar._atomic_write_text(original, ar.render_item(item).replace("Speaker:sound", "Speaker:noise"))
            with self.assertRaisesRegex(ValueError, "regenerated evidence differs"):
                prepare.restore_translation_items(store, original)


if __name__ == "__main__":
    unittest.main()
