"""Fail-fast fixtures for fixed-window independent source scoring."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts import evaluate_scraper_focused_source as fs


class FocusedSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.census = self.root / "census"
        self.census.mkdir()
        self.body = "\n".join(["Ada\uff1asome apples grow." for _ in range(24)])
        (self.census / "body.txt").write_bytes(self.body.encode())
        body_sha = fs.digest(self.body.encode())
        self.page = {"page_id": "web:altsource_ms:en-us:event_story:35:5", "source": "altsource_ms",
                     "text_sha256": body_sha, "local_body_file": "body.txt"}
        manifest = {"stories": [{"story_key": "event:35:5", "pages": {"en": self.page}}]}
        manifest["manifest_sha256"] = fs.canonical(manifest, "manifest_sha256")
        self.write(self.census / "holdout-manifest.json", manifest)
        self.rows = []
        start = 0
        for index, text in enumerate(self.body.splitlines()):
            self.rows.append({"id": f"evidence:{index}", "story_key": "event:35:5", "source": {
                "page_id": self.page["page_id"], "source": self.page["source"], "language": "en", "sha256": body_sha,
                "start": start, "end": start + len(text), "text": text, "complete": True}})
            start += len(text) + 1
        self.tasks = [{"id": f"task:{index}", "language": "en", "review_context": {
                      "task": "discovery", "rows": self.rows[index * 8:(index + 1) * 8]}} for index in range(3)]
        self.tasks_path = self.root / "tasks.json"
        self.write(self.tasks_path, self.tasks)
        self.selection = {"reviewer": "fixture", "story_key": "event:35:5", "language": "en", "scope": "fixed-window fixture",
            "proposal_or_tokenizer_candidates_read": False, "machine_reference_not_human_gold": True, "source_windows_read": 24,
            "units": [{"row": index, "surface": surface, "categories": [category], "meaning": surface}
                      for index in range(24) for surface, category in (("apples", "noun_head"), ("some apples", "modified_nominal"))]}
        self.selection_path = self.root / "selection.json"
        self.write(self.selection_path, self.selection)
        self.reference_path = self.root / "reference.json"
        self.proposal_path = self.root / "proposal.json"
        self.write(self.proposal_path, {"story_key": "event:35:5", "language": "en", "fixture": True})
        self.judgments_path = self.root / "judgments.json"
        self.judgments = []
        for task in self.tasks:
            entries = []
            for row in task["review_context"]["rows"]:
                for surface in ("apples", "some apples"):
                    local_start = row["source"]["text"].index(surface)
                    entries.append({"kind": "literal", "canonical": surface, "evidence_id": row["id"], "segments": [{
                        "start": row["source"]["start"] + local_start,
                        "end": row["source"]["start"] + local_start + len(surface), "exact": surface}]})
            self.judgments.append({"id": task["id"], "decision": "accept", "subjects": entries})
        self.write(self.judgments_path, self.judgments)

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def score(self, out=None):
        return fs.score(self.census, self.tasks_path, self.reference_path, self.proposal_path, self.judgments_path,
                        out or self.root / "score.json", fs.digest(self.proposal_path.read_bytes()), fs.digest(self.judgments_path.read_bytes()))

    def test_freeze_and_typed_exact_score_reproduce(self):
        first = fs.freeze(self.census, self.tasks_path, self.selection_path)
        self.assertEqual(48, first["source_units"])
        self.assertEqual(first, fs.freeze(self.census, self.tasks_path, self.selection_path))
        result = self.score()
        self.assertEqual(48, result["summary"]["typed_subjects_strict_primary_exact"])
        self.assertEqual(0, result["summary"]["pure_terms_strict_primary_exact"])
        self.assertEqual(result, self.score())

    def test_boundary_substring_is_rejected_before_freeze(self):
        self.selection["units"][0]["surface"] = "apple"
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "absent"):
            fs.freeze(self.census, self.tasks_path, self.selection_path)

    def test_candidate_contamination_declaration_is_rejected(self):
        self.selection["proposal_or_tokenizer_candidates_read"] = True
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "candidate-free"):
            fs.freeze(self.census, self.tasks_path, self.selection_path)

    def test_denominator_cannot_change_after_freeze(self):
        fs.freeze(self.census, self.tasks_path, self.selection_path)
        self.selection["units"].pop()
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "Frozen scoring output differs"):
            fs.freeze(self.census, self.tasks_path, self.selection_path)

    def test_packet_text_drift_is_rejected(self):
        fs.freeze(self.census, self.tasks_path, self.selection_path)
        self.tasks[0]["review_context"]["rows"][0]["source"]["text"] = "fabricated"
        self.write(self.tasks_path, self.tasks)
        with self.assertRaisesRegex(ValueError, "raw-page identity"):
            self.score()

    def test_shifted_typed_offset_is_not_accepted(self):
        fs.freeze(self.census, self.tasks_path, self.selection_path)
        self.judgments[0]["subjects"][0]["segments"][0]["start"] += 1
        self.write(self.judgments_path, self.judgments)
        with self.assertRaises(ValueError):
            self.score()

    def test_literal_term_hits_are_separate_from_typed(self):
        fs.freeze(self.census, self.tasks_path, self.selection_path)
        for judgment in self.judgments:
            judgment["subjects"] = []
            judgment["terms"] = ["apples", "some apples"]
        self.write(self.judgments_path, self.judgments)
        result = self.score()
        self.assertEqual(48, result["summary"]["pure_terms_strict_primary_exact"])
        self.assertEqual(0, result["summary"]["typed_subjects_strict_primary_exact"])


if __name__ == "__main__":
    unittest.main()
