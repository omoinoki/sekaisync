"""Resource caches must follow semantic input changes, not corpus length."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sekaisync.termindex import build_alignment_resources


class AlignmentResourceCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.groups = {"story": {"ja": {"text": "旧本文"},
                                 "zh_hans": {"text": "旧词库"}}}

    def test_same_input_reuses_resources_but_equal_length_edit_rebuilds(self):
        with patch("sekaisync.termindex.build_alignment_vocab",
                   side_effect=[{"ja": {"old"}}, {"ja": {"new"}}]) as build, \
             patch("sekaisync.termindex.compute_lang_idf", return_value={}) as idf:
            first = build_alignment_resources(self.groups, ["ja"], [], self.root)
            self.assertEqual(build_alignment_resources(self.groups, ["ja"], [], self.root), first)
            self.groups["story"]["ja"]["text"] = "新本文"
            self.assertEqual(build_alignment_resources(self.groups, ["ja"], [], self.root)[0],
                             {"ja": {"new"}})
            self.assertEqual(build.call_count, 2)
            self.assertEqual(idf.call_count, 2)

    def test_changing_targets_cannot_return_previous_language_subset(self):
        with patch("sekaisync.termindex.build_alignment_vocab",
                   side_effect=lambda groups, targets, glossary: {t: {t} for t in targets}), \
             patch("sekaisync.termindex.compute_lang_idf", return_value={}):
            build_alignment_resources(self.groups, ["en"], [], self.root)
            self.assertEqual(set(build_alignment_resources(
                self.groups, ["ja", "zh_hant"], [], self.root)[0]), {"ja", "zh_hant"})

    def test_glossary_generator_is_hashed_and_still_reaches_builder(self):
        seen = []

        def build(groups, targets, glossary):
            words = {g.names["en"] for g in glossary}
            seen.append(words)
            return {"en": words}

        with patch("sekaisync.termindex.build_alignment_vocab", side_effect=build), \
             patch("sekaisync.termindex.compute_lang_idf", return_value={}):
            for name in ("Moon Hall", "Star Hall"):
                entries = (SimpleNamespace(kind="area", names={"en": name}) for _ in range(1))
                actual = build_alignment_resources(self.groups, ["en"], entries, self.root)
                self.assertEqual(actual[0], {"en": {name}})
            self.assertEqual(seen, [{"Moon Hall"}, {"Star Hall"}])

    def test_story_boundaries_and_usability_affect_resources(self):
        with patch("sekaisync.termindex.build_alignment_vocab", return_value={"ja": set()}) as build, \
             patch("sekaisync.termindex.compute_lang_idf", return_value={}):
            build_alignment_resources(self.groups, ["ja"], [], self.root)
            self.groups["renamed"] = self.groups.pop("story")
            build_alignment_resources(self.groups, ["ja"], [], self.root)
            self.groups["renamed"]["ja"]["untranslated"] = True
            build_alignment_resources(self.groups, ["ja"], [], self.root)
            self.assertEqual(build.call_count, 3)

    def test_exact_target_alias_content_is_not_hidden_by_canonical_page(self):
        self.groups["story"].update(zh_tw={"text": "原頁"}, zh_hant={"text": "舊頁"})
        with patch("sekaisync.termindex.build_alignment_vocab", return_value={"zh_hant": set()}) as build, \
             patch("sekaisync.termindex.compute_lang_idf", return_value={}):
            build_alignment_resources(self.groups, ["zh_hant"], [], self.root)
            self.groups["story"]["zh_hant"]["text"] = "新頁"
            build_alignment_resources(self.groups, ["zh_hant"], [], self.root)
            self.assertEqual(build.call_count, 2)

    def test_corrupt_or_incomplete_cache_rebuilds_without_raising(self):
        path = self.root / "alignment_resources.json"
        with patch("sekaisync.termindex.build_alignment_vocab", return_value={"en": {"name"}}) as build, \
             patch("sekaisync.termindex.compute_lang_idf", return_value={("en", "name"): 1.0}):
            expected = build_alignment_resources(self.groups, ["en"], [], self.root)
            valid = json.loads(path.read_text(encoding="utf-8"))
            for corrupt in ([], {**valid, "vocab": {}}, {**valid, "idf": {"bad": 1}},
                            {**valid, "idf": {"en\u0000name": float("nan")}},
                            {**valid, "version": 0}):
                path.write_text(json.dumps(corrupt), encoding="utf-8")
                self.assertEqual(build_alignment_resources(self.groups, ["en"], [], self.root), expected)
            self.assertEqual(build.call_count, 6)
            self.assertEqual(list(self.root.glob(".alignment-*.tmp")), [])

    def test_conflicting_glossary_alias_order_invalidates_cache(self):
        first = SimpleNamespace(kind="area", names={"zh_tw": "月虹祭", "zh_hant": "星虹祭"})
        changed = SimpleNamespace(kind="area", names={"zh_hant": "星虹祭", "zh_tw": "月虹祭"})
        before = build_alignment_resources({}, ["zh_tw"], [first], self.root)
        after = build_alignment_resources({}, ["zh_tw"], [changed], self.root)
        rebuilt = build_alignment_resources({}, ["zh_tw"], [changed])
        self.assertNotEqual(before, rebuilt)
        self.assertEqual(after, rebuilt)


if __name__ == "__main__":
    unittest.main()
