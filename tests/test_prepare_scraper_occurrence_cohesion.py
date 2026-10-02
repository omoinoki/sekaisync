import unittest

from scripts import prepare_scraper_occurrence_cohesion as cohesion


class CohesionPreparationTests(unittest.TestCase):
    def test_context_selects_semantic_second_occurrence(self):
        view = dict(text="Speaker:the first meeting, then your meeting with Keisuke and Shosuke", start=100)
        selected = cohesion._segments_in_context(view, "meeting", "your meeting with Keisuke and Shosuke")
        self.assertEqual(selected, [dict(start=137, end=144, exact="meeting")])

    def test_repeated_context_is_not_first_match(self):
        with self.assertRaisesRegex(ValueError, "one exact local occurrence"):
            cohesion._segments_in_context(dict(text="your meeting and your meeting", start=0), "meeting", "your meeting")

    def test_context_without_exact_lexical_body_match_fails(self):
        with self.assertRaisesRegex(ValueError, "one lexical body occurrence"):
            cohesion._segments_in_context(dict(text="meeting:your conversation", start=0), "meeting", "your conversation")

    def test_language_alias_preserves_local_surface(self):
        self.assertEqual(cohesion._surface({"surfaces": {"zh_hant": "\u958b\u6703"}}, "zh_tw"), "\u958b\u6703")

    def test_coverage_is_per_exact_focus_not_aggregate_public_missing_targets(self):
        source = dict(id="occ:later")
        sense = dict(id="sense:meeting")
        early_tw = dict(source=dict(id="occ:earlier"), sense=sense, target_language="zh_tw",
                        kind="lexical", target=dict(segments=[dict(exact="\u958b\u6703")]))
        later_en = dict(source=source, sense=sense, target_language="en", kind="lexical",
                        target=dict(segments=[dict(exact="meeting")]))
        current = [early_tw, later_en, later_en]
        self.assertTrue(cohesion._target_is_covered(current, source, sense, "en", "meeting"))
        self.assertFalse(cohesion._target_is_covered(current, source, sense, "zh_tw", "\u958b\u6703"))
        self.assertFalse(cohesion._target_is_covered(current, source, dict(id="sense:other"), "en", "meeting"))
        self.assertFalse(cohesion._target_is_covered(current, source, sense, "en", "conference"))


if __name__ == "__main__":
    unittest.main()
