"""Synthetic full protocol replay, never reading actual target labels."""
from contextlib import redirect_stdout
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_scraper_blind_target_pilot as prepare
from scripts import run_scraper_blind_target_pilot as replay
from sekaisync import agent_packets as ap, dbstore, termindex


class BlindTargetReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.targets = ["ja", "zh_hans", "zh_tw", "ko"]
        pages = []
        for language in ["en", *self.targets]:
            lines = []
            for index in range(12):
                number = str(1000 + index)
                if language == "en":
                    text = ("lending her a hand for source06" if index == 6 else "source" + f"{index:02}")
                    line = "Alice: " + number + " " + text + "."
                elif language == "ja":
                    line = "人物：" + number + " 用語" + f"{index:02}" + "の語です。"
                elif language == "zh_hans":
                    line = "人物：" + number + " 词语" + f"{index:02}" + "在这里。"
                elif language == "zh_tw":
                    line = "人物：" + number + " 詞語" + f"{index:02}" + "在這裡。"
                else:
                    line = "인물: " + number + " 용어" + f"{index:02}" + ("\r\n계속입니다." if index == 5 else " 입니다.")
                lines.append(line)
            pages.append(dict(source="fixture", id="web:fixture:" + language + ":event_story:999:1",
                              language=language, kind="event_story", trust="B", text="\n".join(lines)))
        self.pages = {page["language"]: page for page in pages}
        store = self.base / "fixture-store"
        dbstore.initialize(store)
        dbstore.upsert_web_pages(store, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        tasks, meta = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", self.targets)
        rows = ap._read_scope(store, meta["scope_id"])["windows"]
        self.assertEqual(len(rows), 12)
        items, proposals = [], []
        for index, row in enumerate(rows):
            self.assertEqual(set(row["targets"]), set(self.targets))
            identity = "synthetic-source:" + str(index)
            surface = "lending a hand" if index == 6 else "source" + f"{index:02}"
            fragments = ["lending", "a hand"] if index == 6 else [surface]
            segments, cursor = [], 0
            for exact in fragments:
                start = row["source"]["text"].index(exact, cursor)
                segments.append(dict(start=row["source"]["start"] + start,
                                     end=row["source"]["start"] + start + len(exact), exact=exact))
                cursor = start + len(exact)
            items.append(dict(source_annotation_id=identity, source_surface=surface,
                source_meaning="Synthetic contextual meaning " + str(index), source_categories=["fixture"],
                source_segments=segments, source_evidence_id=row["id"], source_context=row["source"], target_contexts=row["targets"]))
            kind = {1: "paraphrase", 2: "reference", 3: "omitted", 4: "unresolved"}.get(index, "lexical")
            observations = {}
            for language in self.targets:
                head = {"ja": "用語", "zh_hans": "词语", "zh_tw": "詞語", "ko": "용어"}[language] + f"{index:02}"
                parts = [] if kind in {"omitted", "unresolved"} else [head]
                if index == 5 and language == "ko":
                    parts.append("계속입니다")
                observations[language] = dict(kind=kind, parts=parts, reason="Synthetic structural replay only, not semantic scoring.")
            proposals.append(dict(source_annotation_id=identity, targets=observations))
        self.packet = dict(schema="sekaisync/p0-fixed-source-blind-target-pilot@1", story_key="event:999:1",
            source_language="en", target_languages=self.targets, source_units=12, directed_requirements=48,
            target_spans_or_labels_in_packet=False, judgment_contract=dict(schema="sekaisync/p0-blind-target-pilot-judgments@1"), items=items)
        self.seal()
        self.proposal = dict(agent="synthetic-review", target_reference_read=False, items=proposals)
        self.judgments = prepare._prepare(self.packet, self.proposal)
        metadata = {}
        for language, page in self.pages.items():
            path = self.base / ("body-" + language + ".txt")
            path.write_bytes(page["text"].encode())
            metadata[language] = dict(local_body_file=path.name, text_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                page_id=page["id"], source=page["source"], variants=[{key: value for key, value in page.items() if key != "text"}])
        self.manifest = dict(stories=[dict(story_key="event:999:1", pages=metadata)])
        self.paths = [self.base / name for name in ("manifest.json", "packet.json", "proposal.json", "judgments.json")]
        self.output = self.base / "replay"
        self.save_inputs()

    def seal(self):
        canonical = json.dumps({key: value for key, value in self.packet.items() if key != "packet_sha256"},
                               ensure_ascii=False, sort_keys=True).encode()
        self.packet["packet_sha256"] = hashlib.sha256(canonical).hexdigest()

    def save_inputs(self):
        for path, value in zip(self.paths, (self.manifest, self.packet, self.proposal, self.judgments)):
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def run_replay(self):
        return replay._run(*self.paths, self.output)

    def test_real_twelve_by_four_export_submit_and_both_consumers_without_global_names(self):
        report = self.run_replay()
        self.assertEqual(report["source_units"], 12)
        self.assertEqual(report["directed_requirements"], 48)
        self.assertEqual(report["accepted_relations"], 48)
        self.assertEqual(report["global_terms"], 0)
        self.assertEqual(report["consumer_failures"], [])
        self.assertTrue(report["source_oracle_provided"])
        self.assertFalse(report["target_reference_read"])
        self.assertIsNone(report["semantic_score"])
        self.assertEqual(report["relation_kinds"], dict(lexical=32, paraphrase=4, reference=4, omitted=4, unresolved=4))
        self.assertTrue(report["pending_tasks"])
        for name in ("discovery-export.txt", "discovery-judgments.json", "occurrence-export.txt", "occurrence-judgments.json", "report.json"):
            self.assertTrue((self.output / name).exists())
        self.assertEqual(report["replay"]["errors"], [])
        split = next(item for item in report["details"] if item["source_annotation_id"] == "synthetic-source:6")
        self.assertEqual(split["penetration"]["term"]["names"], {})
        ko = next(check for item in report["details"] if item["source_annotation_id"] == "synthetic-source:5"
                  for check in item["checks"] if check["target_language"] == "ko")
        self.assertEqual(ko["expected_raw_scalar"], "용어05\r\n계속입니다")
        self.assertEqual(ko["query_position"]["term"], ko["expected_raw_scalar"])

    def test_changed_judgments_or_packet_raw_page_binding_fails_before_store_creation(self):
        self.judgments["records"][0]["reason"] += " changed"
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, "no longer replay"):
            self.run_replay()
        self.assertFalse(self.output.exists())
        self.judgments = prepare._prepare(self.packet, self.proposal)
        view = self.packet["items"][0]["target_contexts"]["ja"]
        view["sha256"] = "0" * 64
        self.seal()
        self.judgments = prepare._prepare(self.packet, self.proposal)
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, "actual frozen page"):
            self.run_replay()
        self.assertFalse(self.output.exists())

    def test_body_hash_and_path_escape_are_rejected_without_creating_replay_store(self):
        body = self.base / self.manifest["stories"][0]["pages"]["ja"]["local_body_file"]
        original = body.read_bytes()
        body.write_bytes(original + "変更。".encode())
        with self.assertRaisesRegex(ValueError, "body hash"):
            self.run_replay()
        self.assertFalse(self.output.exists())
        body.write_bytes(original)
        outside = self.base.parent / (self.base.name + "-outside.txt")
        outside.write_bytes(original)
        self.addCleanup(lambda: outside.unlink(missing_ok=True))
        metadata = self.manifest["stories"][0]["pages"]["ja"]
        metadata["local_body_file"] = "../" + outside.name
        self.save_inputs()
        with self.assertRaisesRegex(ValueError, "corpus path"):
            self.run_replay()
        self.assertFalse(self.output.exists())

    def test_existing_directory_is_never_reused_or_overwritten(self):
        self.output.mkdir()
        marker = self.output / "report.json"
        marker.write_bytes(b"frozen existing replay")
        with self.assertRaises(FileExistsError):
            self.run_replay()
        self.assertEqual(marker.read_bytes(), b"frozen existing replay")

    def test_consumer_failure_still_publishes_a_fresh_diagnostic_report_and_cli_fails(self):
        argv = ["replay"]
        for name, path in zip(("manifest", "packet", "proposal", "judgments", "output-directory"), [*self.paths, self.output]):
            argv.extend(["--" + name, str(path)])
        with patch.object(replay.SekaiSyncCore, "term_penetrate", return_value=None), patch.object(replay.sys, "argv", argv):
            with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as failure:
                replay.main()
        self.assertEqual(failure.exception.code, 1)
        report = json.loads((self.output / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(len(report["consumer_failures"]), 12)
        self.assertEqual(report["accepted_relations"], 48)
        self.assertTrue((self.output / "store" / "kb" / "sekaisync.db").exists())

    def test_mid_run_input_or_corpus_mutation_blocks_report_but_retains_debug_artifacts(self):
        original = replay._write
        changed = False

        def mutate(path, value):
            nonlocal changed
            original(path, value)
            if path.name == "discovery-judgments.json" and not changed:
                self.paths[2].write_bytes(self.paths[2].read_bytes() + b" ")
                changed = True

        with patch.object(replay, "_write", side_effect=mutate), self.assertRaisesRegex(RuntimeError, "changed"):
            self.run_replay()
        self.assertTrue((self.output / "store" / "kb" / "sekaisync.db").exists())
        self.assertTrue((self.output / "occurrence-judgments.json").exists())
        self.assertFalse((self.output / "report.json").exists())

    def test_evaluated_manifest_bytes_match_the_reported_input_hash_even_if_file_changes_then_reverts(self):
        original_raw = self.paths[0].read_bytes()
        altered = deepcopy(self.manifest)
        for metadata in altered["stories"][0]["pages"].values():
            metadata["variants"][0]["trust"] = "A"
        altered_raw = (json.dumps(altered, ensure_ascii=False, indent=2) + "\n").encode()
        original_hash, original_write = replay._hash, replay._write
        changed = False

        def change_after_hash(path):
            nonlocal changed
            value = original_hash(path)
            if path == self.paths[0] and not changed:
                self.paths[0].write_bytes(altered_raw)
                changed = True
            return value

        def restore_after_loading(path, value):
            original_write(path, value)
            if path.name == "discovery-judgments.json":
                self.paths[0].write_bytes(original_raw)

        with patch.object(replay, "_hash", side_effect=change_after_hash), patch.object(replay, "_write", side_effect=restore_after_loading):
            report = self.run_replay()
        manifest_key = str(self.paths[0].resolve())
        self.assertEqual(report["input_sha256"][manifest_key], hashlib.sha256(original_raw).hexdigest())
        self.assertTrue(all(detail["penetration"]["term"]["trust"] == "B" for detail in report["details"]))

    def test_import_time_code_drift_blocks_any_replay_output(self):
        with patch.object(replay, "_code_hashes", return_value={"fake": "changed"}), self.assertRaisesRegex(RuntimeError, "after import"):
            self.run_replay()
        self.assertFalse(self.output.exists())

    def test_new_package_dependency_is_included_in_mid_run_closure_freeze(self):
        root = self.base / "code"
        package, scripts = root / "sekaisync", root / "scripts"
        package.mkdir(parents=True)
        scripts.mkdir()
        script = scripts / "run.py"
        helper = scripts / "prepare_scraper_blind_target_pilot.py"
        script.write_text("# synthetic runner\n", encoding="utf-8")
        helper.write_text("# synthetic helper\n", encoding="utf-8")
        (package / "existing.py").write_text("# synthetic existing\n", encoding="utf-8")
        original = replay._write

        def mutate(path, value):
            original(path, value)
            if path.name == "discovery-judgments.json":
                (package / "created_during_run.py").write_text("# new synthetic dependency\n", encoding="utf-8")

        with patch.object(replay, "ROOT", root), patch.object(replay, "__file__", str(script)):
            hashes = replay._code_hashes()
            with patch.object(replay, "_IMPORT_CODE_HASHES", hashes), patch.object(replay, "_write", side_effect=mutate):
                with self.assertRaisesRegex(RuntimeError, "changed"):
                    self.run_replay()
        self.assertTrue(self.output.exists())
        self.assertFalse((self.output / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
