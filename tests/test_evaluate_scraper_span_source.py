"""Frozen strict source-span and typed-subject scoring regressions."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import evaluate_scraper_span_source as evaluator
from scripts.census_scraper_corpus import LANGUAGES


class StrictSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reference_path = self.root / "reference.json"
        self.proposal_path = self.root / "proposal.json"
        self.judgments_path = self.root / "judgments.json"
        self.tasks_path = self.root / "tasks.json"
        self.output = self.root / "result.json"
        self.body = "Person：build his confidence."
        self.segments = [{"start": self.body.index(word), "end": self.body.index(word) + len(word), "exact": word}
                         for word in ("build", "confidence")]
        pages, annotations, tasks, judgments = {}, [], [], []
        for language in LANGUAGES:
            path = self.root / (language + ".txt")
            path.write_text(self.body, encoding="utf-8")
            page = {"page_id": "page:" + language, "source": "fixture", "language": language,
                    "text_sha256": evaluator.digest(path.read_bytes()), "local_body_file": path.name}
            pages[language] = page
            annotations.append({"id": "gain:" + language, "concept_id": "gain", "language": language,
                "surface": "build ... confidence", "surface_is_contiguous": False, "categories": ["predicate"],
                "primary_occurrence_index": 0, "span_groups": [{"segments": self.segments}]})
            view = {"page_id": page["page_id"], "source": "fixture", "language": language,
                    "sha256": page["text_sha256"], "start": 0, "end": len(self.body), "text": self.body}
            tasks.append({"id": language, "language": language, "review_context": {"task": "discovery", "rows": [
                {"id": "evidence:" + language, "story_key": "event:999:7", "source": view}]}})
            judgments.append({"id": language, "decision": "accept", "terms": ["build", "confidence"]})
        manifest = {"stories": [{"story_key": "event:999:7", "pages": pages}]}
        manifest["manifest_sha256"] = evaluator.digest(json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode())
        self.write(self.root / "holdout-manifest.json", manifest)
        reference = {"schema": "sekaisync/p0-independent-machine-span-reference@2", "story_key": "event:999:7",
            "holdout_manifest_sha256": manifest["manifest_sha256"], "machine_reference_not_human_gold": True,
            "proposal_or_tokenizer_candidates_read": False, "annotations": annotations}
        reference["reference_sha256"] = evaluator.digest(json.dumps(reference, ensure_ascii=False, sort_keys=True).encode())
        self.write(self.reference_path, reference)
        self.proposal = {"story_key": "event:999:7", "surfaces": {language: ["build", "confidence"] for language in LANGUAGES}}
        self.judgments, self.tasks = judgments, tasks
        self.flush()

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def flush(self):
        self.write(self.proposal_path, self.proposal)
        self.write(self.judgments_path, self.judgments)
        self.write(self.tasks_path, self.tasks)

    def score(self, source_hash=None):
        return evaluator.score(self.root, self.reference_path, self.proposal_path, self.judgments_path, self.tasks_path,
                               self.output, source_hash or evaluator.digest(self.proposal_path.read_bytes()),
                               evaluator.digest(self.judgments_path.read_bytes()))

    def test_independent_fragments_do_not_count_as_discontinuous_expression(self):
        result = self.score()
        self.assertEqual(result["summary"]["pure_terms_strict_primary_exact"], 0)
        self.assertEqual(result["summary"]["diagnostic_fragment_union_only"], 5)
        self.assertEqual(result["summary"]["typed_subjects_strict_primary_exact"], 0)
        self.assertEqual(result, self.score())

    def test_exact_typed_subject_is_separate_from_pure_literal_score(self):
        self.judgments[1]["subjects"] = [{"kind": "segmented", "evidence_id": "evidence:en", "segments": self.segments}]
        self.flush()
        result = self.score()
        self.assertEqual(result["summary"]["pure_terms_strict_primary_exact"], 0)
        self.assertEqual(result["summary"]["typed_subjects_strict_primary_exact"], 1)
        self.assertEqual(result["summary"]["typed_incremental_exact"], 1)
        self.assertEqual(result["host_typed_observation_count"], 1)

    def test_contiguous_enclosing_expression_does_not_count_as_exact_group(self):
        self.proposal["surfaces"] = {language: ["build his confidence"] for language in LANGUAGES}
        for item in self.judgments:
            item["terms"] = ["build his confidence"]
        self.flush()
        result = self.score()
        self.assertEqual(result["summary"]["pure_terms_strict_primary_exact"], 0)
        self.assertEqual(result["summary"]["diagnostic_overbroad_only"], 5)

    def test_duplicates_preserve_record_counts_without_inflating_span_hits(self):
        self.proposal["surfaces"]["en"].append("build")
        self.judgments[1]["terms"].append("build")
        self.flush()
        result = self.score()
        self.assertEqual(result["host_surface_table_records"], 11)
        self.assertEqual(result["host_distinct_literal_surface_types"], 10)
        self.assertEqual(result["summary"]["pure_terms_strict_primary_exact"], 0)

    def test_frozen_hash_mismatch_fails_before_output(self):
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            self.score("0" * 64)
        self.assertFalse(self.output.exists())

    def test_typed_subject_rejects_shifted_segments_and_unknown_evidence(self):
        wrong = [dict(part, start=part["start"] + 1) for part in self.segments]
        self.judgments[1]["subjects"] = [{"kind": "segmented", "evidence_id": "evidence:en", "segments": wrong}]
        self.flush()
        with self.assertRaises(ValueError):
            self.score()
        self.judgments[1]["subjects"][0].update(evidence_id="not-exported", segments=self.segments)
        self.flush()
        with self.assertRaisesRegex(ValueError, "existing exact source evidence"):
            self.score()

    def test_literal_table_cannot_fabricate_a_contiguous_segment_canonical(self):
        self.proposal["surfaces"]["en"] = ["build confidence"]
        self.judgments[1]["terms"] = ["build confidence"]
        self.flush()
        with self.assertRaisesRegex(ValueError, "ungrounded literal"):
            self.score()

    def test_changed_frozen_body_and_packet_anchor_are_not_scored(self):
        (self.root / "en.txt").write_text(self.body + " changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "raw body hash mismatch"):
            self.score()
        (self.root / "en.txt").write_text(self.body, encoding="utf-8")
        self.tasks[1]["review_context"]["rows"][0]["source"]["page_id"] = "wrong-page"
        self.judgments[1]["subjects"] = [{"kind": "segmented", "evidence_id": "evidence:en", "segments": self.segments}]
        self.flush()
        with self.assertRaisesRegex(ValueError, "raw-page identity mismatch"):
            self.score()


if __name__ == "__main__":
    unittest.main()
