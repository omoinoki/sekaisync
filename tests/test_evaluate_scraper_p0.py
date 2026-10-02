"""P0 scorer regressions: exhaustive accounting, grounding and label isolation."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import agent_packets as ap, dbstore, termindex as ti, zhfirst

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_scraper_p0.py"
spec = importlib.util.spec_from_file_location("evaluate_scraper_p0", SCRIPT)
p0 = importlib.util.module_from_spec(spec)
spec.loader.exec_module(p0)


class P0EvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.evaluation = Path(self.temp.name) / "evaluation"
        self.store = self.evaluation / "evaluation-store"
        dbstore.initialize(self.store)
        self.pages = []
        names = dict(ja="月虹音楽祭", en="Moonbow Festival", zh_hans="月虹音乐节",
                     zh_tw="月虹音樂節", ko="달무리 음악제")
        bodies = dict(ja="話者：今日は{}に行こう。友達も行く。",
                      en="Speaker: Today we are attending {}. Friends are going too.",
                      zh_hans="说话人：今天去参加{}。朋友也去。",
                      zh_tw="說話人：今天去參加{}。朋友也去。",
                      ko="화자：오늘은 {}에 가자. 친구도 간다.")
        for chapter in range(2):
            for language, name in names.items():
                self.pages.append(dict(id=f"fixture:{language}:event_story:999:{chapter}", source="fixture",
                                       story_key=f"event:999:{chapter}", language=language, kind="event_story",
                                       canonical_key=f"event_story:{language}:999:{chapter}", trust="B",
                                       text=bodies[language].format(name)))
        dbstore.upsert_web_pages(self.store, "fixture", self.pages)
        groups = ti.group_pages_by_story(self.pages)
        items, meta = ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, "zh_hans",
                                              [language for language in names if language != "zh_hans"])
        self.tasks = [item.to_dict() for item in items]
        self.scope_id = meta["scope_id"]
        self.source_pages = {story: ti._group_page(by, "zh_hans") for story, by in groups.items()}
        self.annotations = {story: ["月虹音乐节", "朋友"] for story in groups}

    def receipt(self, task, terms):
        return dict(decision="accept", value=json.dumps(terms), scope_id=self.scope_id)

    def test_exhaustive_score_does_not_ignore_new_nonlegacy_receipt(self):
        receipts = {task["id"]: self.receipt(task, ["月虹音乐节"] if i == 0 else ["朋友"])
                    for i, task in enumerate(self.tasks)}
        statuses = p0.assess_tasks(self.tasks, receipts)
        report, residuals = p0.score_labels(self.tasks, statuses, [], self.source_pages,
                                           self.annotations, [self.tasks[0]["id"]])
        self.assertEqual(p0.task_group(statuses, [task["id"] for task in self.tasks])["completed"], 2)
        self.assertEqual(report["legacy_six_sample"]["combined_found"], 1)
        self.assertEqual(report["legacy_compatible"]["combined_found"], 2)
        self.assertEqual(report["legacy_compatible"]["combined_story_term_pairs"], 2)
        self.assertEqual(report["missing_story_term_pairs"], 2)
        self.assertEqual(len(residuals), 2)

    def test_missing_stale_invalid_and_completed_are_separate(self):
        first, second = self.tasks
        receipts = {first["id"]: self.receipt(first, [])}
        statuses = p0.assess_tasks(self.tasks, receipts)
        self.assertEqual([row["status"] for row in statuses], ["completed", "missing"])
        receipts[second["id"]] = dict(decision="accept", scope_id="another-scope", value="[]")
        self.assertEqual(p0.assess_tasks(self.tasks, receipts)[1]["status"], "stale")
        receipts[second["id"]] = self.receipt(second, ["不存在"])
        self.assertEqual(p0.assess_tasks(self.tasks, receipts)[1]["status"], "invalid")
        receipts[second["id"]] = self.receipt(second, [])
        receipts[second["id"]]["value"] = "not-json"
        self.assertEqual(p0.assess_tasks(self.tasks, receipts)[1]["status"], "invalid")

    def test_changed_page_invalidates_completed_and_missing_tasks(self):
        receipts = {task["id"]: self.receipt(task, ["朋友"]) for task in self.tasks}
        with p0._readonly(self.store) as conn:
            self.assertTrue(all(row["status"] == "completed" for row in
                                p0.assess_tasks(self.tasks, receipts, conn=conn, store=self.store)))
        changed = dict(self.pages[2], text=self.pages[2]["text"] + "修改")
        dbstore.upsert_web_pages(self.store, "fixture", [changed])
        with p0._readonly(self.store) as conn:
            self.assertEqual(p0.assess_tasks(self.tasks, receipts, conn=conn, store=self.store)[0]["status"], "stale")
            self.assertEqual(p0.assess_tasks(self.tasks, {}, conn=conn, store=self.store)[0]["status"], "stale")

    def test_duplicate_ids_and_tampered_context_fail_fast(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            p0.assess_tasks([self.tasks[0], self.tasks[0]], {})
        changed = json.loads(json.dumps(self.tasks[0]))
        changed["review_context"]["rows"][0]["source"]["text"] = "forged"
        self.assertEqual(p0.assess_tasks([changed], {})[0]["status"], "invalid")
        with self.assertRaisesRegex(ValueError, "outside"):
            p0.task_group(p0.assess_tasks(self.tasks, {}), ["unknown"])

    def test_body_spans_exclude_speaker_and_preserve_all_raw_offsets(self):
        text = "朋友：月虹音乐节和月虹音乐节\nA：Ｍｏｏｎ\nFestival"
        spans = p0.body_spans(text, "月虹音乐节")
        self.assertEqual([text[start:end] for start, end in spans], ["月虹音乐节", "月虹音乐节"])
        self.assertEqual(p0.body_spans(text, "朋友"), [])
        self.assertEqual([text[start:end] for start, end in p0.body_spans(text, "Moon Festival")],
                         ["Ｍｏｏｎ\nFestival"])
        self.assertEqual(p0.body_spans("Party Art RADICAL", "Art"), [(6, 9)])

    def test_residual_causes_are_observations_not_guessed_root_causes(self):
        statuses = p0.assess_tasks(self.tasks, {self.tasks[0]["id"]: self.receipt(self.tasks[0], [])})
        report, residuals = p0.score_labels(self.tasks, statuses, [], self.source_pages, self.annotations)
        self.assertEqual(report["unknown_root_cause_story_term_pairs"], 4)
        self.assertTrue(all(row["root_cause"] == "unknown" for row in residuals))
        self.assertIn("completed_covering_task_did_not_emit_label", residuals[0]["observations"])
        self.assertTrue(any("covered_by_missing_task" in row["observations"] for row in residuals))

    def test_ledger_is_blind_and_distinguishes_legacy_new_and_baseline(self):
        receipts = {task["id"]: self.receipt(task, ["朋友"]) for task in self.tasks}
        statuses = p0.assess_tasks(self.tasks, receipts)
        automatic = [dict(canonical="朋友", stories=list(self.source_pages))]
        ledger = p0.candidate_ledger(self.tasks, statuses, automatic, self.source_pages,
                                   [self.tasks[0]["id"]])
        self.assertEqual(len(ledger), 2)
        self.assertEqual(ledger[0]["origins"], ["automatic_baseline", "legacy_six_agent_discovery"])
        self.assertEqual(ledger[1]["origins"], ["automatic_baseline", "new_agent_discovery"])
        self.assertTrue(all(row["source_spans"] and row["adjudication"] == "unreviewed" for row in ledger))
        self.assertFalse(any("gold" in key and key != "judgment_is_independent_gold" for row in ledger for key in row))

    def test_cli_run_is_readonly_and_writes_scorer_only_outputs(self):
        manifest = dict(schema="sekaisync/p0-discovery-manifest@1", tasks=self.tasks,
                        legacy_sampled_ids=[self.tasks[0]["id"]], corpus_sha256=p0.hashlib.sha256(json.dumps(
                            [[p["id"], p["text"]] for p in sorted(self.pages, key=lambda p: p["id"])],
                            ensure_ascii=False).encode()).hexdigest())
        self.evaluation.mkdir(parents=True, exist_ok=True)
        (self.evaluation / "discovery-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        annotations = self.evaluation / "labels.json"
        annotations.write_text(json.dumps(dict(stories=self.annotations)), encoding="utf-8")
        database = self.store / "kb/sekaisync.db"
        before = database.read_bytes()
        output = self.evaluation / "evaluator"
        with patch.object(p0, "automatic_baseline", return_value=([], "fixture")):
            report = p0.run(self.evaluation, output, baseline_store=self.store, annotations_path=annotations)
        self.assertEqual(database.read_bytes(), before)
        self.assertEqual(report["discovery"]["all_tasks"]["missing"], 2)
        self.assertFalse(report["zero_errors_demonstrated"])
        self.assertIsNone(report["independent_semantic_accuracy"])
        self.assertTrue((output / "gold-residuals-scorer-only.json").exists())
        self.assertTrue((output / "candidate-review-ledger.jsonl").exists())

    def test_baseline_uses_actual_zhfirst_record_shape_and_cache(self):
        output = self.evaluation / "evaluator"
        record = zhfirst.ZhFirstTerm(canonical="朋友", stories={"event:999:0"})
        with patch.object(p0.zh, "extract_terms_zhfirst", return_value=[record]) as extract:
            records, digest = p0.automatic_baseline(self.pages, [], output)
            again, other_digest = p0.automatic_baseline(self.pages, [], output)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(records, again)
        self.assertEqual(digest, other_digest)
        self.assertEqual(records[0]["source_language"], "zh_hans")

    def test_frozen_baseline_drift_fails_instead_of_changing_denominator(self):
        manifest = dict(schema="sekaisync/p0-discovery-manifest@1", tasks=self.tasks,
                        legacy_sampled_ids=[self.tasks[0]["id"]])
        self.evaluation.mkdir(parents=True, exist_ok=True)
        (self.evaluation / "discovery-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (self.evaluation / "scoring-state.json").write_text(json.dumps(dict(automatic=["朋友"])), encoding="utf-8")
        with patch.object(p0, "automatic_baseline", return_value=([], "fixture")):
            with self.assertRaisesRegex(ValueError, "differs from frozen"):
                p0.run(self.evaluation, self.evaluation / "evaluator", baseline_store=self.store)

    def test_frozen_corpus_drift_fails_before_baseline_or_label_scoring(self):
        manifest = dict(schema="sekaisync/p0-discovery-manifest@1", tasks=self.tasks,
                        legacy_sampled_ids=[self.tasks[0]["id"]], corpus_sha256="not-the-current-hash")
        self.evaluation.mkdir(parents=True, exist_ok=True)
        (self.evaluation / "discovery-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with patch.object(p0, "automatic_baseline") as baseline:
            with self.assertRaisesRegex(ValueError, "current corpus differs"):
                p0.run(self.evaluation, self.evaluation / "evaluator", baseline_store=self.store)
        baseline.assert_not_called()
        self.assertFalse((self.evaluation / "evaluator/evaluation-report.json").exists())

    def test_continuation_receipts_count_separately_and_contribute_all_terms(self):
        terms = ["词" + str(i).zfill(3) for i in range(215)]
        page = dict(self.pages[2], text="说话人：" + "，".join(terms))
        dbstore.upsert_web_pages(self.store, "fixture", [page])
        groups = ti.group_pages_by_story([page])
        items, meta = ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, "zh_hans", [])
        original = items[0].to_dict()
        receipts = {original["id"]: dict(decision="accept", value=json.dumps(terms[:200]),
                                         scope_id=meta["scope_id"])}
        expanded = p0.expand_continuations([original], receipts)
        self.assertEqual(len(expanded), 2)
        self.assertEqual(p0.assess_tasks(expanded, receipts)[1]["status"], "missing")
        receipts[expanded[1]["id"]] = dict(decision="accept", value=json.dumps(terms[200:]),
                                           scope_id=meta["scope_id"])
        expanded = p0.expand_continuations([original], receipts)
        statuses = p0.assess_tasks(expanded, receipts)
        report, _ = p0.score_labels(expanded, statuses, [], {"event:999:0": page},
                                    {"event:999:0": terms}, [original["id"]])
        self.assertEqual(p0.task_group(statuses, [original["id"]])["total"], 1)
        self.assertEqual(p0.task_group(statuses, [expanded[1]["id"]])["completed"], 1)
        self.assertEqual(report["legacy_six_sample"]["combined_found"], 200)
        self.assertEqual(report["legacy_compatible"]["combined_found"], 215)
        ledger = p0.candidate_ledger(expanded, statuses, [], {"event:999:0": page}, [original["id"]])
        self.assertEqual(sum("agent_discovery_continuation" in row["origins"] for row in ledger), 15)

    def test_public_report_contains_no_gold_surfaces(self):
        report, residuals = p0.score_labels(self.tasks, p0.assess_tasks(self.tasks, {}), [],
                                           self.source_pages, self.annotations)
        self.assertNotIn("朋友", json.dumps(report, ensure_ascii=False))
        self.assertTrue(any(row["label"] == "朋友" for row in residuals))


if __name__ == "__main__":
    unittest.main()
