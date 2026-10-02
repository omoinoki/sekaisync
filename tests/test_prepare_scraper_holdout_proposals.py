"""Host-authored observations must ground in exact frozen raw windows."""
import hashlib
import unittest

from scripts import prepare_scraper_holdout_proposals as prep


class HoldoutProposalPreparationTests(unittest.TestCase):
    def row(self, text, offset=0, identifier="row:one"):
        return dict(id=identifier, story_key="event:999:1", source=dict(
            text=text, start=offset, end=offset + len(text), source="fixture",
            page_id="fixture:en:event_story:999:1", language="en",
            sha256=hashlib.sha256(text.encode()).hexdigest(), complete=True))

    def proposal(self, clause="lending her a hand", parts=("lending", "a hand")):
        return dict(segmented_observations=[dict(language="en", clause=clause, parts=list(parts))])

    def test_exact_case_literal_does_not_accept_lowercase_alias_or_speaker(self):
        view = self.row("Lending: Take a look;\nbuild confidence.")["source"]
        self.assertTrue(prep._literal_present(view, "Take a look"))
        self.assertFalse(prep._literal_present(view, "take a look"))
        self.assertFalse(prep._literal_present(view, "Lending"))
        self.assertTrue(prep._literal_present(view, "build confidence"))

    def test_soft_wrap_literal_retains_legacy_whitespace_selection(self):
        view = self.row("A: build\nconfidence today.")["source"]
        self.assertTrue(prep._literal_present(view, "build confidence"))
        self.assertTrue(prep._literal_present(view, "buildconfidence"))
        self.assertFalse(prep._literal_present(view, "build confidently"))

    def test_exact_typed_segments_keep_offsets_and_never_invent_canonical(self):
        rows = [self.row("A: lending her a hand.", offset=100)]
        used = set()
        entries = prep._typed_observations(self.proposal(), "en", rows, used)
        self.assertEqual(entries, [dict(kind="segmented", evidence_id="row:one", segments=[
            dict(start=103, end=110, exact="lending"), dict(start=115, end=121, exact="a hand")])])
        self.assertEqual(len(used), 1)
        self.assertNotIn("canonical", entries[0])
        self.assertEqual(prep._typed_observations(self.proposal(), "en", rows, used), [])

    def test_literal_looking_display_still_keeps_two_typed_parts(self):
        rows = [self.row("A: build confidence.")]
        entries = prep._typed_observations(self.proposal("build confidence", ("build", "confidence")),
                                           "en", rows, set())
        self.assertEqual(len(entries[0]["segments"]), 2)

    def test_repeated_and_overlapping_clause_are_not_first_match(self):
        for clause, text, fragments in (("lending her a hand", "A: lending her a hand; lending her a hand.",
                                         ("lending", "a hand")), ("aaa", "A: aaaa", ("a", "aa"))):
            with self.subTest(clause=clause), self.assertRaisesRegex(ValueError, "unambiguous"):
                prep._typed_observations(self.proposal(clause, fragments), "en", [self.row(text)], set())

    def test_out_of_order_or_invented_fragment_is_not_reordered(self):
        for fragments in (("a hand", "lending"), ("lend", "a palm")):
            with self.subTest(fragments=fragments), self.assertRaises(ValueError):
                prep._typed_observations(self.proposal(parts=fragments), "en",
                                         [self.row("A: lending her a hand.")], set())

    def test_absent_clause_and_other_language_do_not_consume_identity(self):
        used = set()
        self.assertEqual(prep._typed_observations(self.proposal(), "en", [self.row("A: another turn.")], used), [])
        self.assertEqual(prep._typed_observations(self.proposal(), "ja", [self.row("A: lending her a hand.")], used), [])
        self.assertEqual(used, set())

    def test_discovery_does_not_allow_cross_turn_or_metadata_fragments(self):
        for text, clause, parts in (("A: lending\nB: a hand.", "lending\nB: a hand", ("lending", "a hand")),
                                    ("Alice: lending her a hand.", "Alice: lending her a hand", ("Alice", "a hand"))):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, "utterance body"):
                prep._typed_observations(self.proposal(clause, parts), "en", [self.row(text)], set())


if __name__ == "__main__":
    unittest.main()
