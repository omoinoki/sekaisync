"""Synthetic finite five-language source-only freeze and exact-vector scoring."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import evaluate_scraper_symmetric_source as ev
from scripts.prepare_scraper_focused_discovery import _prepare
from sekaisync import agent_packets as ap


class SymmetricSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.census = self.base / "census"
        self.census.mkdir()
        self.tasks_paths, self.packet_paths, self.proposals, self.judgments = {}, {}, {}, {}
        self.tasks, self.host = {}, {}
        self.setup_path = self.base / "setup.json"
        self.selection_path = self.base / "selection.json"
        self.reference_path = self.base / "reference.json"
        self.body = "Ada:some apples grow.\nAda:We are lifting her spirits today.\nAda:No other expression."
        body_sha = ev.digest(self.body.encode("utf-8"))
        manifest = dict(stories=[dict(story_key="event:999:2", pages={})])
        self.setup = dict(schema="sekaisync/symmetric-source-trial@1", story_key="event:999:2",
                          labels_or_references_read=False, candidates_in_source_packets=False,
                          target_views_in_source_packets=False, source_windows=15, languages=[], artifact_sha256={})
        self.selection = dict(reviewer="independent synthetic fixture", story_key="event:999:2",
                              scope="Only three frozen source windows per language; not exhaustive source vocabulary",
                              proposal_or_tokenizer_candidates_read=False, machine_reference_not_human_gold=True,
                              full_story_semantically_read=False, exhaustive_reference=False, languages={})
        for language in ev.LANGUAGES:
            page = dict(id=f"web:fixture:{language}:event_story:999:2", source="fixture", language=language,
                        text=self.body, kind="event_story")
            body_path = self.census / f"{language}.txt"
            body_path.write_bytes(self.body.encode("utf-8"))
            manifest["stories"][0]["pages"][language] = dict(page_id=page["id"], source="fixture",
                                                                 text_sha256=body_sha, local_body_file=body_path.name)
            rows = [dict(id=f"span:{language}:{index}", story_key="event:999:2",
                         source=ap._view(page, [index], ap._lines(page)[1]), targets={}) for index in range(3)]
            context = dict(schema=ap._SCHEMA, task="discovery", scope_id=f"scope:{language}",
                           source_language=language, rows=rows)
            item = ap._item(f"@discover:{language}", language, [], "discovery", context, "Synthetic source packet")
            self.tasks[language] = [item.to_dict()]
            self.tasks_paths[language] = self.base / f"{language}-internal.json"
            self.write(self.tasks_paths[language], self.tasks[language])
            self.packet_paths[language] = self.base / f"{language}-source.txt"
            source_rows = [dict(id=row["id"], story_key=row["story_key"], source=row["source"]) for row in rows]
            lines = [f"## id={item.id} kind=discovery", "task_context: " + json.dumps(dict(
                schema=ap._SCHEMA, task="discovery", scope_id=context["scope_id"], source_language=language))]
            lines += ["context: " + json.dumps(dict(row, source={key: value for key, value in row["source"].items()
                                                                       if key != "sha256"})) for row in source_rows]
            self.packet_paths[language].write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.setup["languages"].append(dict(language=language, corpus_language=language,
                                                 item_id=item.id, scope_id=context["scope_id"], selected_windows=3,
                                                 source_only_rows_sha256=ap._digest(source_rows)))
            for path in (self.tasks_paths[language], self.packet_paths[language]):
                self.setup["artifact_sha256"][str(path.resolve())] = ev.digest(path.read_bytes())
            units = [dict(evidence_id=rows[0]["id"], kind="literal", surface="apples", category="noun_head", meaning="apples"),
                     dict(evidence_id=rows[0]["id"], kind="literal", surface="some apples", category="modified_nominal", meaning="a quantity of apples"),
                     dict(evidence_id=rows[1]["id"], kind="literal", surface="are lifting her spirits", category="complete_predicate", meaning="improving her mood"),
                     dict(evidence_id=rows[1]["id"], kind="segmented", parts=["lifting", "spirits"], category="discontinuous", meaning="raising someone's mood"),
                     dict(evidence_id=rows[2]["id"], kind="literal", surface="No", category="function_expression", meaning="negative determiner")]
            self.selection["languages"][language] = dict(source_windows_read=3, units=units, empty_categories={})
            turns = [dict(evidence_id=row["id"], units=[{key: value for key, value in unit.items() if key != "evidence_id"}
                                                       for unit in units if unit["evidence_id"] == row["id"]]) for row in rows]
            self.host[language] = dict(language=language, story_key="event:999:2", agent="synthetic-host",
                                       provenance="Synthetic per-turn judgments", turns=turns)
            self.proposals[language] = self.base / f"{language}-proposal.json"
            self.judgments[language] = self.base / f"{language}-judgments.json"
        manifest["manifest_sha256"] = ev.canonical(manifest, "manifest_sha256")
        self.setup["manifest_sha256"] = manifest["manifest_sha256"]
        self.write(self.census / "holdout-manifest.json", manifest)
        self.write(self.setup_path, self.setup)
        self.write(self.selection_path, self.selection)
        self.prepare()

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def prepare(self):
        for language in ev.LANGUAGES:
            answers, _ = _prepare(self.tasks[language], self.host[language])
            self.write(self.proposals[language], self.host[language])
            self.write(self.judgments[language], answers)

    def freeze(self):
        return ev.freeze(self.census, self.packet_paths, self.setup_path, self.selection_path)

    def score(self, output=None, expected_reference=None):
        return ev.score(self.census, self.packet_paths, self.setup_path, self.tasks_paths, self.reference_path,
                        self.proposals, self.judgments, output or self.base / "score.json",
                        expected_reference or ev.digest(self.reference_path.read_bytes()),
                        {language: ev.digest(path.read_bytes()) for language, path in self.proposals.items()},
                        {language: ev.digest(path.read_bytes()) for language, path in self.judgments.items()})

    def test_freeze_and_five_language_exact_score_reproduce_without_fixed_24_window_denominator(self):
        frozen = self.freeze()
        self.assertEqual((frozen["source_windows"], frozen["source_units"]), (15, 25))
        self.assertEqual(self.freeze(), frozen)
        result = self.score()
        self.assertEqual(result["summary"]["strict_primary_exact"], 25)
        self.assertEqual(result["per_type"]["segmented"]["strict_primary_exact"], 5)
        self.assertTrue(all(value["strict_primary_exact"] == 5 for value in result["per_language"].values()))
        self.assertIsNone(result["semantic_precision"])
        self.assertIsNone(result["exhaustive_vocabulary_recall"])
        self.assertFalse(result["category_labels_are_semantic_truth"])
        self.assertEqual(self.score(), result)

    def test_freeze_never_reads_internal_tasks_or_host_proposals(self):
        forbidden = {path.resolve() for path in (*self.tasks_paths.values(), *self.proposals.values(), *self.judgments.values())}
        read = ev._read
        def guard(path, frozen):
            self.assertNotIn(Path(path).resolve(), forbidden)
            return read(path, frozen)
        with patch.object(ev, "_read", side_effect=guard):
            self.assertEqual(self.freeze()["source_units"], 25)

    def test_contamination_exhaustiveness_and_full_story_claims_fail_before_freeze(self):
        for field in ("proposal_or_tokenizer_candidates_read", "full_story_semantically_read", "exhaustive_reference"):
            with self.subTest(field=field):
                selection = copy.deepcopy(self.selection)
                selection[field] = True
                self.write(self.selection_path, selection)
                with self.assertRaisesRegex(ValueError, "candidate-free"):
                    self.freeze()
        self.assertFalse(self.reference_path.exists())

    def test_empty_category_requires_an_explicit_finite_scope_reason(self):
        self.selection["languages"]["ko"]["units"] = [unit for unit in self.selection["languages"]["ko"]["units"]
                                                        if unit["category"] != "discontinuous"]
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "empty reasons"):
            self.freeze()
        self.selection["languages"]["ko"]["empty_categories"] = {"discontinuous": "No such selected unit in these three windows"}
        self.write(self.selection_path, self.selection)
        self.assertEqual(self.freeze()["source_units"], 24)

    def test_speaker_metadata_and_duplicate_raw_vectors_are_rejected(self):
        for surface in ("Ada", "apples"):
            selection = copy.deepcopy(self.selection)
            selection["languages"]["en"]["units"].append(dict(evidence_id="span:en:0", kind="literal",
                                                                surface=surface, category="noun_head", meaning="invalid"))
            self.write(self.selection_path, selection)
            with self.subTest(surface=surface), self.assertRaises(ValueError):
                self.freeze()
        self.assertFalse(self.reference_path.exists())

    def test_unknown_or_unread_window_cannot_enter_denominator(self):
        self.selection["languages"]["en"]["units"][0]["evidence_id"] = "span:outside-first-packet"
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "unreviewed"):
            self.freeze()

    def test_numeric_bounds_and_completeness_flags_cannot_alias_frozen_raw_views(self):
        for field, value in (("start", False), ("end", 20.0), ("complete", 1)):
            path = self.packet_paths["en"]
            original = path.read_text(encoding="utf-8")
            lines = original.splitlines()
            row = json.loads(lines[2].removeprefix("context: "))
            row["source"][field] = value
            lines[2] = "context: " + json.dumps(row)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            self.setup["artifact_sha256"][str(path.resolve())] = ev.digest(path.read_bytes())
            self.write(self.setup_path, self.setup)
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "bounds"):
                self.freeze()
            path.write_text(original, encoding="utf-8")

    def test_frozen_packet_text_or_page_body_drift_is_rejected(self):
        self.freeze()
        body = self.census / "en.txt"
        body.write_text(self.body.replace("apples", "oranges"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "body hash"):
            self.score()
        self.assertFalse((self.base / "score.json").exists())

    def test_modified_frozen_reference_hash_cannot_be_substituted(self):
        report = self.freeze()
        reference = json.loads(self.reference_path.read_bytes())
        reference["annotations"].pop()
        reference["reference_sha256"] = ev.canonical(reference, "reference_sha256")
        self.write(self.reference_path, reference)
        with self.assertRaisesRegex(ValueError, "reference file hash"):
            self.score(expected_reference=report["reference_file_sha256"])

    def test_judgment_must_reproduce_the_exact_frozen_host_proposal(self):
        self.freeze()
        answers = json.loads(self.judgments["en"].read_bytes())
        answers[0]["subjects"].pop()
        self.write(self.judgments["en"], answers)
        with self.assertRaisesRegex(ValueError, "reproduce"):
            self.score()

    def test_fragment_union_and_envelope_containment_never_receive_strict_hit_credit(self):
        self.freeze()
        for language in ev.LANGUAGES:
            units = self.host[language]["turns"][1]["units"]
            units[:] = [unit for unit in units if unit["kind"] != "segmented"] + [
                dict(kind="literal", surface=surface, category="noun_head", meaning="raw component") for surface in ("lifting", "spirits")]
        self.prepare()
        result = self.score()
        self.assertEqual(result["summary"]["strict_primary_exact"], 20)
        self.assertEqual(result["summary"]["diagnostic_fragment_union_only"], 5)
        self.assertEqual(result["summary"]["diagnostic_overbroad_only"], 5)
        self.assertEqual(result["per_type"]["segmented"]["strict_primary_exact"], 0)

    def test_midrun_input_mutation_blocks_score_publication(self):
        self.freeze()
        prepare = ev._prepare
        def mutate(tasks, proposal):
            result = prepare(tasks, proposal)
            path = self.proposals["en"]
            path.write_bytes(path.read_bytes() + b" ")
            return result
        with patch.object(ev, "_prepare", side_effect=mutate), self.assertRaisesRegex(ValueError, "hash|changed"):
            self.score()
        self.assertFalse((self.base / "score.json").exists())


if __name__ == "__main__":
    unittest.main()
