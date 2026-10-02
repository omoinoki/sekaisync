"""Public-query evaluation keeps missing requirements and immutable provenance."""
from contextlib import contextmanager, redirect_stdout
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import dbstore, occurrence_store as ledger
from tests import test_occurrence_penetrate_review as consumer_fixture


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/evaluate_scraper_occurrence_queries.py"
spec = importlib.util.spec_from_file_location("evaluate_scraper_occurrence_queries", SCRIPT)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class OccurrenceQueryEvaluatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        dbstore.initialize(self.store)
        with dbstore.connect(self.store) as conn:
            ledger._ensure_schema(conn)
            conn.commit()
        self.proposal = dict(story_key="event:999:1", concepts=[dict(
            key=f"concept-{index}", surfaces={language: f"surface-{index}-{language}"
                                             for language in ("ja", "en", "zh_hans", "zh_hant", "ko")},
            relations={"en": "paraphrase"} if index == 16 else {}) for index in range(17)])

    def fake_core(self, *, missing=False):
        proposal = self.proposal
        instances = []

        class FakeCore:
            def __init__(self, store):
                self.calls = []
                self.view_entries = 0
                self.active = False
                instances.append(self)

            @contextmanager
            def request_view(self):
                self.view_entries += 1
                self.active = True
                try:
                    yield self
                finally:
                    self.active = False

            def term_penetrate(self, query, story_key=None, languages=None):
                if not self.active:
                    raise AssertionError("public queries must share one outer read snapshot")
                self.calls.append((query, story_key, languages))
                if missing:
                    return None
                concept, source = next((concept, source) for concept in proposal["concepts"]
                                       for source, surface in concept["surfaces"].items() if surface == query)
                entries = {}
                for target, surface in concept["surfaces"].items():
                    nonlexical = source != target and "paraphrase" in {
                        concept.get("relations", {}).get(source), concept.get("relations", {}).get(target)}
                    entries[target] = dict(term="" if nonlexical else surface,
                                           missing=nonlexical, note="occurrence relation: paraphrase" if nonlexical else "lexical")
                return dict(term=dict(source_language=source), per_language=entries)

        return FakeCore, instances

    def test_seventeen_five_language_concepts_keep_340_requirements_and_eight_paraphrases(self):
        core, instances = self.fake_core()
        with patch.object(evaluator, "SekaiSyncCore", core):
            report = evaluator._evaluate(self.store, self.proposal)
        self.assertEqual(report["requirements"], 340)
        self.assertEqual(report["lexical_requirements"], 332)
        self.assertEqual(report["nonlexical_requirements"], 8)
        self.assertEqual(report["covered"], 332)
        self.assertEqual(report["typed_nonlexical"], 8)
        self.assertEqual(report["unavailable"], 0)
        self.assertFalse(report["semantic_gold"])
        self.assertEqual(len(instances[0].calls), 85)
        self.assertEqual(instances[0].view_entries, 1)
        self.assertTrue(all(len(call[2]) == 5 for call in instances[0].calls))

    def test_none_results_are_missing_denominator_members_not_discarded_queries(self):
        core, _ = self.fake_core(missing=True)
        with patch.object(evaluator, "SekaiSyncCore", core):
            report = evaluator._evaluate(self.store, self.proposal)
        self.assertEqual(report["source_queries"], 85)
        self.assertEqual(report["requirements"], 340)
        self.assertEqual(report["unavailable"], 340)
        self.assertEqual(len(report["requirements_detail"]), 340)
        self.assertEqual(report["covered"], 0)
        self.assertEqual(report["typed_nonlexical"], 0)

    def test_actual_public_query_does_not_count_two_source_anchors_as_full_coverage(self):
        fixture = consumer_fixture.OccurrencePenetrateReviewTests("test_same_story_same_sense_different_sources_do_not_fill_each_others_languages")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        items = fixture.packets()
        fixture.submit(items["en"], [fixture.proposal(items["en"], "sound", source_index=0, target_index=0)])
        fixture.submit(items["zh_tw"], [fixture.proposal(items["zh_tw"], "聲音", source_index=1, target_index=1)])
        report = evaluator._evaluate(fixture.store, dict(story_key="event:999:1", concepts=[dict(
            key="sound", surfaces={"zh_hans": "声音", "en": "sound", "zh_tw": "聲音"})]))
        chinese = next(row["result"] for row in report["queries"] if row["source"] == "zh_hans")
        self.assertEqual(sum(bool(chinese["per_language"][language]["term"]) for language in ("en", "zh_tw")), 1)
        self.assertEqual(report["requirements"], 6)
        self.assertEqual(report["covered"], 1)
        self.assertEqual(report["unavailable"], 5)

    def main_fixture(self):
        source_root = self.root / "source"
        (source_root / "sekaisync").mkdir(parents=True)
        (source_root / "scripts").mkdir(parents=True)
        for name in ("core.py", "termindex.py", "occurrence_store.py"):
            (source_root / "sekaisync" / name).write_bytes(b"# immutable fixture\n")
        (source_root / "scripts/evaluate_scraper_occurrence_queries.py").write_bytes(b"# scorer fixture\n")
        proposal = self.root / "proposal.json"
        proposal.write_text(json.dumps(self.proposal), encoding="utf-8")
        output = self.root / "output.json"
        argv = [str(SCRIPT), "--store", str(self.store), "--proposal", str(proposal), "--output", str(output)]
        return source_root, proposal, output, argv

    def invoke(self, source_root, argv, evaluate):
        with patch.object(evaluator, "ROOT", source_root), patch.object(evaluator, "_evaluate", evaluate), \
                patch("sys.argv", argv), redirect_stdout(io.StringIO()):
            evaluator.main()

    def test_preexisting_frozen_output_is_not_overwritten_or_even_recomputed(self):
        root, proposal, output, argv = self.main_fixture()
        output.write_bytes(b"original frozen bytes")
        with patch.object(evaluator, "_evaluate") as evaluate:
            with self.assertRaises(FileExistsError):
                self.invoke(root, argv, evaluate)
            evaluate.assert_not_called()
        self.assertEqual(output.read_bytes(), b"original frozen bytes")

    def test_racing_output_creation_is_exclusive_and_preserves_the_other_writer(self):
        root, proposal, output, argv = self.main_fixture()

        def evaluate(*args):
            output.write_bytes(b"racing writer's frozen report")
            return {"requirements": 340}

        with self.assertRaises(FileExistsError):
            self.invoke(root, argv, evaluate)
        self.assertEqual(output.read_bytes(), b"racing writer's frozen report")

    def test_code_changed_during_evaluation_has_no_published_mixed_code_report(self):
        root, proposal, output, argv = self.main_fixture()

        def evaluate(*args):
            (root / "sekaisync/core.py").write_bytes(b"# modified during evaluation\n")
            return {"requirements": 340}

        with self.assertRaisesRegex(RuntimeError, "changed"):
            self.invoke(root, argv, evaluate)
        self.assertFalse(output.exists())

    def test_code_changed_after_import_is_rejected_before_evaluation(self):
        root, proposal, output, argv = self.main_fixture()
        imported = evaluator._code_hashes(root)
        (root / "sekaisync/core.py").write_bytes(b"# modified after import\n")
        with patch.object(evaluator, "_IMPORT_ROOT", root), \
                patch.object(evaluator, "_IMPORT_HASHES", imported), \
                patch.object(evaluator, "_evaluate") as evaluate:
            with self.assertRaisesRegex(RuntimeError, "after import"):
                self.invoke(root, argv, evaluate)
            evaluate.assert_not_called()
        self.assertFalse(output.exists())

    def test_scorer_changed_during_evaluation_has_no_published_report(self):
        root, proposal, output, argv = self.main_fixture()

        def evaluate(*args):
            (root / "scripts/evaluate_scraper_occurrence_queries.py").write_bytes(b"# modified scorer\n")
            return {"requirements": 340}

        with self.assertRaisesRegex(RuntimeError, "changed during evaluation"):
            self.invoke(root, argv, evaluate)
        self.assertFalse(output.exists())

    def test_new_span_dependency_created_during_evaluation_has_no_published_report(self):
        root, proposal, output, argv = self.main_fixture()

        def evaluate(*args):
            (root / "sekaisync/span_subjects.py").write_bytes(b"# new runtime dependency\n")
            return {"requirements": 340}

        with self.assertRaisesRegex(RuntimeError, "changed during evaluation"):
            self.invoke(root, argv, evaluate)
        self.assertFalse(output.exists())

    def test_span_dependency_modified_during_evaluation_has_no_published_report(self):
        root, proposal, output, argv = self.main_fixture()
        dependency = root / "sekaisync/span_subjects.py"
        dependency.write_bytes(b"# immutable span dependency\n")

        def evaluate(*args):
            dependency.write_bytes(b"# modified span dependency\n")
            return {"requirements": 340}

        with self.assertRaisesRegex(RuntimeError, "changed during evaluation"):
            self.invoke(root, argv, evaluate)
        self.assertFalse(output.exists())

    def test_packet_dependency_modified_during_evaluation_has_no_published_report(self):
        root, proposal, output, argv = self.main_fixture()
        dependency = root / "sekaisync/agent_packets.py"
        dependency.write_bytes(b"# immutable packet dependency\n")

        def evaluate(*args):
            dependency.write_bytes(b"# modified packet dependency\n")
            return {"requirements": 340}

        with self.assertRaisesRegex(RuntimeError, "changed during evaluation"):
            self.invoke(root, argv, evaluate)
        self.assertFalse(output.exists())

    def test_proposal_changed_during_evaluation_has_no_published_report(self):
        root, proposal, output, argv = self.main_fixture()

        def evaluate(*args):
            proposal.write_text(json.dumps(dict(self.proposal, story_key="event:999:2")), encoding="utf-8")
            return {"requirements": 340}

        with self.assertRaisesRegex(RuntimeError, "frozen proposal changed"):
            self.invoke(root, argv, evaluate)
        self.assertFalse(output.exists())

    def test_report_hashes_the_actual_proposal_bytes_it_evaluated(self):
        root, proposal, output, argv = self.main_fixture()
        original = proposal.read_bytes()
        captured = []

        def evaluate(store, payload):
            captured.append(payload)
            return {"requirements": 340}

        self.invoke(root, argv, evaluate)
        report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(captured, [self.proposal])
        self.assertEqual(report["proposal_sha256"], hashlib.sha256(original).hexdigest())
        self.assertIn("scripts/evaluate_scraper_occurrence_queries.py", report["code_sha256"])
        for name, digest in report["code_sha256"].items():
            self.assertEqual(digest, hashlib.sha256((root / name).read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
