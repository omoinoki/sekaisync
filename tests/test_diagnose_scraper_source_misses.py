"""Structural source-miss diagnostics must not invent causal denominators."""
import unittest

from scripts import diagnose_scraper_source_misses as dm


class SourceMissDiagnosticTests(unittest.TestCase):
    def data(self):
        parts = [{"start": 2, "end": 5, "exact": "one"}, {"start": 8, "end": 11, "exact": "two"}]
        row = {"annotation_id": "x:en", "language": "en", "reference_surface": "one ... two", "primary_segments": parts,
               "discontinuous": True, "categories": ["predicate", "inflected_verb"], "pure_terms_strict_primary_exact": False,
               "typed_subjects_strict_primary_exact": False,
               "diagnostic_fragments": [{"surface": "one", "segments": parts[:1]}],
               "diagnostic_overbroad_containment": [{"surface": "one - two", "segments": [{"start": 2, "end": 11, "exact": "one - two"}]}]}
        tasks = [{"language": "en", "review_context": {"rows": [{"id": "window", "source": {
                  "start": 0, "end": 11, "text": "A\uff1aone - two"}}]}}]
        return {"assessments": [row]}, tasks

    def test_primary_classes_partition_but_tags_overlap(self):
        score, tasks = self.data()
        report = dm.analyze(score, tasks)
        self.assertEqual({"genuine_discontinuity": 1}, report["primary_structural_counts"])
        self.assertEqual(1, report["narrower_and_wider_support_overlap"])
        self.assertEqual(1, report["overlapping_tag_counts"]["wider_literal_boundary_instead_of_selected_unit"])

    def test_packet_exposure_is_not_claimed_reading_or_morphology_cause(self):
        score, tasks = self.data()
        report = dm.analyze(score, tasks)
        self.assertEqual(1, report["selected_spans_in_one_provided_window"])
        self.assertEqual(0, report["single_character_reference_denominator"])
        self.assertFalse(report["unread_host_cognition_measured"])
        self.assertFalse(report["synthetic_lemma_erasure_as_cause_measured"])

    def test_unreproducible_raw_packet_is_rejected(self):
        score, tasks = self.data()
        tasks[0]["review_context"]["rows"][0]["source"]["text"] = "fabricated"
        with self.assertRaisesRegex(ValueError, "not reproducible"):
            dm.analyze(score, tasks)


if __name__ == "__main__":
    unittest.main()
