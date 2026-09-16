import unittest

from sekaisync.glossary import GlossaryTerm, merge_glossary, resolve_name
from sekaisync.models import Entity


class GlossaryTest(unittest.TestCase):
    def test_merge_and_resolve(self):
        entity = Entity(
            id="character:1",
            type="character",
            region="demo",
            regions=["demo"],
            names={"en": "Hoshino Ichika", "zh_tw": "星乃一歌"},
            source="master_db:demo",
            demo=True,
        )
        terms = merge_glossary([entity])
        self.assertEqual(len(terms), 1)
        results = resolve_name(terms, "Hoshino Ichika", target_language="zh_tw")
        self.assertEqual(results[0]["target_name"], "星乃一歌")

    def test_seed_official_flag(self):
        seed = [
            {
                "id": "game_title",
                "kind": "game",
                "canonical": "Hatsune Miku: Colorful Stage!",
                "names": {"en": "Hatsune Miku: Colorful Stage!"},
                "official": True,
                "source": "official_game_title",
            }
        ]
        terms = merge_glossary([], seed)
        self.assertTrue(terms[0].official)


class ResolveNameUnknownTest(unittest.TestCase):
    """P04 — a missing target language is reported, not filled in.

    Before the fix, ``target_name`` fell back to ``term.canonical``, so a
    Korean request against a Japanese-only term returned the Japanese name in
    the Korean slot.  Consumers could not distinguish "here is the Korean
    name" from "no Korean name exists".
    """

    def _terms(self):
        entity = Entity(
            id="character:jp_only",
            type="character",
            region="demo",
            regions=["demo"],
            names={"ja": "JapaneseOnly", "en": "Japanese Only"},
            source="master_db:demo",
            demo=True,
        )
        return merge_glossary([entity])

    def test_missing_target_language_returns_null(self):
        results = resolve_name(self._terms(), "JapaneseOnly", target_language="ko")
        self.assertEqual(len(results), 1)
        self.assertIsNone(
            results[0]["target_name"],
            "missing translation was filled with a fallback name instead of null",
        )
        self.assertEqual(results[0]["translation_status"], "missing")

    def test_canonical_name_still_available_for_display(self):
        """A display spelling is still offered — but in its own field.

        ``canonical_name`` is the term's canonical spelling, NOT a claim about
        the target language.  It stays separate from ``target_name`` so that
        "here is something to show" never becomes "here is the Korean name".
        """
        results = resolve_name(self._terms(), "JapaneseOnly", target_language="ko")
        self.assertTrue(results[0]["canonical_name"])
        self.assertNotEqual(
            results[0]["translation_status"],
            "available",
            "a display-only canonical name must not be reported as a "
            "translation being available",
        )

    def test_available_target_language_is_unchanged(self):
        results = resolve_name(self._terms(), "JapaneseOnly", target_language="ja")
        self.assertEqual(results[0]["target_name"], "JapaneseOnly")
        self.assertEqual(results[0]["translation_status"], "available")

    def test_existing_target_language_still_resolves(self):
        """The ordinary path must not regress."""
        entity = Entity(
            id="character:1",
            type="character",
            region="demo",
            regions=["demo"],
            names={"en": "Hoshino Ichika", "zh_tw": "星乃一歌"},
            source="master_db:demo",
            demo=True,
        )
        results = resolve_name(merge_glossary([entity]), "Hoshino Ichika", target_language="zh_tw")
        self.assertEqual(results[0]["target_name"], "星乃一歌")
        self.assertEqual(results[0]["translation_status"], "available")

    def test_requested_source_language_missing_is_distinguished(self):
        """A requested-but-absent source slot is 'missing', not a silent fallback."""
        results = resolve_name(
            self._terms(),
            "JapaneseOnly",
            target_language="ja",
            source_language="ko",
        )
        self.assertIsNone(results[0]["source_name"])
        self.assertEqual(results[0]["source_status"], "missing")

    def test_unrequested_source_language_is_not_missing(self):
        """Not asking for a source language is not the same as it being absent."""
        results = resolve_name(self._terms(), "JapaneseOnly", target_language="ja")
        self.assertEqual(results[0]["source_status"], "not_requested")


if __name__ == "__main__":
    unittest.main()
