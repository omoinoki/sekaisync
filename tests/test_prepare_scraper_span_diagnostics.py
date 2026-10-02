"""Frozen diagnostic inputs and real typed queue/public-consumer plumbing."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/prepare_scraper_span_diagnostics.py"
spec = importlib.util.spec_from_file_location("prepare_scraper_span_diagnostics", SCRIPT)
diagnostic = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostic)


class SpanDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        lines = [f"Speaker{index}: left{index}" + (" inserted " if index < 6 else " ") + f"right{index}."
                 for index in range(8)]
        self.source_text = "\n".join(lines)
        self.target_text = "\n".join(f"人物{index}：訳語{index}を使いました。" for index in range(8))
        self.pages = {}
        for language, text in (("en", self.source_text), ("ja", self.target_text)):
            path = self.root / (language + ".txt")
            path.write_bytes(text.encode("utf-8"))
            self.pages[language] = dict(status="present", page_id=f"web:fixture:{language}:event_story:999:1",
                                        source="fixture", language=language, text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                        local_body_file=path.name)
        self.manifest = dict(stories=[dict(story_key="event:999:1", pages=self.pages, release_status="fixture-only")])
        annotations, reviews = [], []
        for index in range(8):
            parts = []
            for exact in (f"left{index}", f"right{index}"):
                start = self.source_text.index(exact)
                parts.append(dict(start=start, end=start + len(exact), exact=exact, physical_line=index + 1))
            identity = "fixture:" + str(index)
            annotations.append(dict(id=identity, language="en", source="fixture", page_id=self.pages["en"]["page_id"],
                                    text_sha256=self.pages["en"]["text_sha256"], primary_occurrence_index=0,
                                    span_groups=[dict(segments=parts)]))
            reviews.append(dict(annotation_id=identity, target_language="ja", target_turn=f"人物{index}：訳語{index}を使いました。",
                                target_parts=[f"訳語{index}"], kind="lexical", sense_key="fixture-" + str(index),
                                sense_gloss="An explicit synthetic fixture interpretation.", rationale="Grounded plumbing fixture, not semantic gold."))
        self.reference = dict(story_key="event:999:1", annotations=annotations)
        self.review = dict(story_key="event:999:1", cases=reviews)
        self.manifest_path = self.root / "manifest.json"
        self.reference_path = self.root / "reference.json"
        self.review_path = self.root / "review.json"
        self.write_inputs()

    def write_inputs(self):
        for path, value in ((self.manifest_path, self.manifest), (self.reference_path, self.reference),
                            (self.review_path, self.review)):
            path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def load(self):
        return diagnostic._load_inputs(self.manifest_path, self.reference_path, self.review_path)

    def test_real_eight_case_pipeline_keeps_types_global_names_and_frozen_inputs_separate(self):
        inputs = self.load()
        before = {name: path.read_bytes() for name, path in inputs["input_paths"].items()}
        output = self.root / "diagnostic"
        report = diagnostic._run(inputs, output)
        self.assertEqual(report["source_units"], 8)
        self.assertEqual(report["formerly_unsupported"], 6)
        self.assertEqual(report["already_legacy_representable"], 2)
        self.assertEqual(report["accepted_relations"], 8)
        self.assertEqual(report["selected_targets_consumed"], 8)
        self.assertEqual(report["source_scalar_names"], 0)
        self.assertEqual(report["global_terms"], 0)
        self.assertEqual(report["pending_followups"], 1)
        from sekaisync import agent_review as review
        pending = review.load_queue(output / "store")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]._context["task"], "discovery")
        self.assertEqual(pending[0]._context["source_boundary_audit"]["stage"], "review")
        self.assertEqual(len(pending[0]._context["source_boundary_audit"]["inherited_subjects"]), 8)
        self.assertTrue(report["label_assisted"])
        self.assertFalse(report["semantic_gold"])
        self.assertIsNone(report["blind_accuracy_gain"])
        self.assertEqual(json.loads((output / "report.json").read_text(encoding="utf-8")), report)
        for name, original in before.items():
            self.assertEqual(inputs["input_paths"][name].read_bytes(), original)
            self.assertEqual(report["input_sha256"][name], hashlib.sha256(original).hexdigest())
        self.assertTrue(all(not detail["public_penetration"]["term"]["names"] for detail in report["details"]))
        self.assertTrue(all(detail["generic_core_query"]["query"] == detail["subject"]["canonical"]
                            for detail in report["details"]))

    def test_raw_body_hash_change_is_rejected_before_a_store_is_created(self):
        (self.root / "en.txt").write_bytes(b"changed body")
        with self.assertRaisesRegex(ValueError, "body hash changed"):
            self.load()
        self.assertFalse((self.root / "diagnostic").exists())

    def test_reference_primary_index_is_used_instead_of_first_group(self):
        annotation = self.reference["annotations"][0]
        primary = deepcopy(annotation["span_groups"][0])
        annotation["span_groups"] = [dict(segments=[primary["segments"][0]]), primary]
        annotation["primary_occurrence_index"] = 1
        self.write_inputs()
        source = self.load()["sources"][0]
        self.assertEqual(source["segments"], [{key: part[key] for key in ("start", "end", "exact")}
                                              for part in primary["segments"]])

    def test_missing_or_duplicate_target_review_cannot_change_the_source_denominator(self):
        original = deepcopy(self.review)
        for cases in (original["cases"][:-1], original["cases"] + [original["cases"][0]]):
            self.review["cases"] = cases
            self.write_inputs()
            with self.subTest(cases=len(cases)), self.assertRaisesRegex(ValueError, "exactly the eight"):
                self.load()

    def test_target_selection_rejects_missing_repeated_and_overlapping_first_match_guesses(self):
        page = dict(text="Speaker: aaa aa.")
        for review in (dict(target_turn="Missing turn", target_parts=["aa"]),
                       dict(target_turn=page["text"], target_parts=["aa"]),
                       dict(target_turn="aaa", target_parts=["aa"])):
            with self.subTest(review=review), self.assertRaisesRegex(ValueError, "unique complete raw"):
                diagnostic._target_parts(page, review)

    def test_target_fragments_cannot_reverse_raw_order(self):
        page = dict(text="Speaker: left then right.")
        with self.assertRaisesRegex(ValueError, "raw order"):
            diagnostic._target_parts(page, dict(target_turn=page["text"], target_parts=["right", "left"]))

    def test_existing_diagnostic_directory_is_never_reused_or_overwritten(self):
        output = self.root / "diagnostic"
        output.mkdir()
        sentinel = output / "report.json"
        sentinel.write_bytes(b"previous frozen report")
        with self.assertRaises(FileExistsError):
            diagnostic._run(self.load(), output)
        self.assertEqual(sentinel.read_bytes(), b"previous frozen report")
        self.assertFalse((output / "store").exists())

    def test_input_mutation_during_execution_does_not_publish_a_report(self):
        inputs = self.load()
        original_query = diagnostic.SekaiSyncCore.query

        def changed_query(core, *args, **kwargs):
            result = original_query(core, *args, **kwargs)
            self.review_path.write_text("{}", encoding="utf-8")
            return result

        output = self.root / "diagnostic"
        with patch.object(diagnostic.SekaiSyncCore, "query", changed_query):
            with self.assertRaisesRegex(RuntimeError, "frozen diagnostic input changed"):
                diagnostic._run(inputs, output)
        self.assertFalse((output / "report.json").exists())

    def test_dependency_change_during_execution_does_not_publish_a_report(self):
        output = self.root / "diagnostic"
        with patch.object(diagnostic, "_code_hashes", side_effect=[{"snapshot": "before"}, {"snapshot": "after"}]):
            with self.assertRaisesRegex(RuntimeError, "code changed"):
                diagnostic._run(self.load(), output)
        self.assertFalse((output / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
