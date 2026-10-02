import hashlib
import unittest

from scripts import prepare_scraper_focused_discovery as focused


class FocusedDiscoveryPreparationTests(unittest.TestCase):
    def row(self, text="A: Wringing out a wet towel is harder than I expected.", identifier="row:one"):
        return dict(id=identifier, story_key="event:35:5", source=dict(
            source="fixture", page_id="fixture:en:event_story:35:5", language="en",
            sha256=hashlib.sha256(text.encode()).hexdigest(), start=100, end=100 + len(text),
            text=text, complete=True))

    def tasks(self, rows):
        return [dict(id="task:one", review_context=dict(task="discovery", source_language="en", rows=rows))]

    def proposal(self, turns):
        return dict(language="en", story_key="event:35:5", agent="host", provenance="Raw source only", turns=turns)

    def literal(self, text, **extra):
        return dict(kind="literal", surface=text, category="nominal", **extra)

    def test_nested_units_preserve_independent_exact_subjects_and_old_judgment_shape(self):
        row = self.row()
        turns = [dict(evidence_id=row["id"], units=[self.literal("towel"), self.literal("wet towel"),
                                                 self.literal("Wringing out a wet towel")])]
        judgments, coverage = focused._prepare(self.tasks([row]), self.proposal(turns))
        self.assertEqual(judgments[0]["terms"], [])
        self.assertEqual(len(judgments[0]["subjects"]), 3)
        self.assertEqual(judgments[0]["subjects"][0]["segments"][0]["start"], 122)
        self.assertEqual(coverage[0]["observations"], 3)

    def test_every_window_must_have_explicit_review_even_when_empty(self):
        rows = [self.row(), self.row("A: ...", "row:two")]
        for turns in ([dict(evidence_id="row:one", units=[])],
                      [dict(evidence_id=row["id"], units=[]) for row in rows]):
            with self.assertRaises(ValueError):
                focused._prepare(self.tasks(rows), self.proposal(turns))
        turns = [dict(evidence_id=row["id"], units=[], empty_reason="No selected lexical content") for row in rows]
        self.assertEqual(len(focused._prepare(self.tasks(rows), self.proposal(turns))[1]), 2)

    def test_duplicate_subject_cannot_count_as_new_evidence(self):
        row = self.row()
        with self.assertRaisesRegex(ValueError, "duplicate"):
            focused._prepare(self.tasks([row]), self.proposal([
                dict(evidence_id=row["id"], units=[self.literal("towel"), self.literal("towel")])]))

    def test_literal_repeat_requires_explicit_unique_clause_not_first_match(self):
        row = self.row("A: wet towel; dry towel.")
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            focused._entry(row, self.literal("towel"))
        entry, _ = focused._entry(row, self.literal("towel", clause="dry towel"))
        self.assertEqual(entry["segments"][0]["start"], 118)

    def test_long_literals_preserve_exact_raw_no_newline_lf_and_crlf(self):
        for separator in ("", "\n", "\r\n"):
            surface = "a" * 900 + separator + "b" * (900 - len(separator))
            row = self.row("A: " + surface)
            with self.subTest(separator=repr(separator)):
                self.assertEqual(len(surface), 1800)
                self.assertFalse(focused.packets._valid_surface(surface))
                entry, subject = focused._entry(row, self.literal(surface))
                self.assertEqual(entry["canonical"], surface)
                self.assertEqual(entry["segments"], [dict(start=103, end=1903, exact=surface)])
                self.assertEqual(subject["canonical"], surface)
                self.assertEqual(subject["source"]["segments"], entry["segments"])
                judgments, coverage = focused._prepare(self.tasks([row]), self.proposal([
                    dict(evidence_id=row["id"], units=[self.literal(surface)])]))
                self.assertEqual(judgments[0]["terms"], [])
                self.assertEqual(judgments[0]["subjects"], [entry])
                self.assertEqual(coverage[0]["source_sha256"], row["source"]["sha256"])

    def test_literal_length_1801_and_controls_fail(self):
        invalid = ["a" * 900 + separator + "b" * (901 - len(separator))
                   for separator in ("", "\n", "\r\n")]
        invalid.extend("a" * 81 + control + "b" * 81
                       for control in ("\x00", "\t", "\r", "\x1f"))
        for surface in invalid:
            with self.subTest(surface=repr(surface)), self.assertRaises(ValueError):
                focused._entry(self.row("A: " + surface), self.literal(surface))

    def test_long_literal_repeats_and_speaker_boundaries_stay_protected(self):
        surface = "a" * 81
        repeated = self.row("A: " + surface + "; " + surface + ".")
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            focused._entry(repeated, self.literal(surface))
        entry, _ = focused._entry(repeated, self.literal(surface, clause=surface + "."))
        self.assertEqual(entry["segments"][0]["start"], 186)
        for text, selected in (("A: " + surface, "A: " + surface),
                               ("A: " + surface + "\nB: " + surface,
                                surface + "\nB: " + surface)):
            with self.subTest(text=text), self.assertRaises(ValueError):
                focused._entry(self.row(text), self.literal(selected))

    def test_typed_raw_fragments_keep_intervening_slot_and_case(self):
        row = self.row("A: Wait for her in line.")
        entry, subject = focused._entry(row, dict(kind="segmented", clause="Wait for her in line",
                                                  parts=["Wait", "in line"]))
        self.assertNotIn("canonical", entry)
        self.assertEqual(subject["gap_text"], [" for her "])
        self.assertEqual(subject["canonical_parts"], ["Wait", "in line"])

    def test_metadata_cross_turn_case_and_invented_parts_fail(self):
        for row, observation in ((self.row(), self.literal("wringing out")),
                                  (self.row(), self.literal("Imaginary towel")),
                                  (self.row(), self.literal("A")),
                                  (self.row("A: Wait\nB: in line"), dict(kind="segmented", parts=["Wait", "in line"]))):
            with self.subTest(observation=observation), self.assertRaises(ValueError):
                focused._entry(row, observation)

    def test_mixed_story_language_duplicate_rows_or_task_types_fail(self):
        row = self.row()
        proposal = self.proposal([dict(evidence_id=row["id"], units=[self.literal("towel")])])
        for tasks in (self.tasks([row, row]), self.tasks([dict(row, story_key="event:other")]),
                      [dict(id="task:one", review_context=dict(task="occurrence", source_language="en", rows=[row]))],
                      [dict(id="task:one", review_context=dict(task="discovery", source_language="ja", rows=[row]))]):
            with self.subTest(tasks=tasks), self.assertRaises(ValueError):
                focused._prepare(tasks, proposal)

    def test_duplicate_task_ids_fail_even_with_different_valid_windows(self):
        first, second = self.row(), self.row("A: ...", "row:two")
        tasks = self.tasks([first]) + self.tasks([second])
        with self.assertRaisesRegex(ValueError, "identities"):
            focused._prepare(tasks, self.proposal([
                dict(evidence_id=first["id"], units=[self.literal("towel")]),
                dict(evidence_id=second["id"], units=[], empty_reason="Ellipsis only")]))

    def test_code_closure_hashes_nested_dependency_creation_and_changes(self):
        from pathlib import Path
        import tempfile
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            nested = root / "sekaisync" / "nested"
            nested.mkdir(parents=True)
            dependency = nested / "fixture.py"
            with patch.object(Path, "read_bytes", autospec=True, side_effect=lambda path: b"version1"):
                dependency.touch()
                first = focused._code_hashes(root)
                self.assertIn(str(dependency.resolve()), first)
            with patch.object(Path, "read_bytes", autospec=True, side_effect=lambda path: b"version2"):
                changed = focused._code_hashes(root)
            self.assertNotEqual(first, changed)


if __name__ == "__main__":
    unittest.main()
