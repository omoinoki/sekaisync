"""Raw case and focused-window boundaries for task-wide control scoring."""
import json
import unittest

from scripts import evaluate_scraper_source_control as sc


class SourceControlTests(unittest.TestCase):
    def rows(self):
        return [{"id": "first", "source": {"start": 0, "text": "Ada\uff1aApples are good."}},
                {"id": "later", "source": {"start": 100, "text": "Ada\uff1aapples are better."}}]

    def test_case_mismatch_does_not_count_at_first_window(self):
        rows = self.rows()
        self.assertEqual([], sc.exact_candidates(rows[:1], ["apples"]))
        self.assertEqual(["later"], [item["evidence_id"] for item in sc.exact_candidates(rows, ["apples"])])

    def test_later_window_only_type_has_no_focused_support(self):
        rows = self.rows()
        self.assertEqual([], sc.exact_candidates(rows[:1], ["better"]))
        self.assertEqual(1, len(sc.exact_candidates(rows, ["better"])))

    def test_speaker_labels_and_boundary_substrings_are_excluded(self):
        self.assertEqual([], sc.exact_candidates(self.rows(), ["Ada", "apple"]))

    def test_original_task_hash_is_exact_object_substring(self):
        obj = '{\n  "id": "task:1", "review_context": {"rows": []}\n}'
        raw = ('[  ' + obj + ', {"id":"task:2"}]').encode()
        value, sha = sc.selected_task(raw, "task:1")
        self.assertEqual("task:1", value["id"])
        self.assertEqual(sc.digest(obj.encode()), sha)

    def test_segmented_observation_does_not_become_enclosing_literal(self):
        rows = [{"id": "one", "source": {"start": 10, "text": "Ada\uff1amake it easier"}}]
        groups, report = sc.segmented_candidates(rows, [{"clause": "make it easier", "parts": ["make", "easier"]}])
        self.assertEqual(1, len(groups))
        self.assertEqual(["make", "easier"], [part["exact"] for part in groups[0]["segments"]])
        self.assertEqual(1, report[0]["exact_part_groups"])


if __name__ == "__main__":
    unittest.main()
