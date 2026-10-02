"""Symmetric trial setup uses real packet boundaries without reading labels."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_scraper_symmetric_trial as trial
from sekaisync import agent_packets as ap, agent_review as ar, dbstore


class SymmetricTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.census = self.base / "census"
        self.census.mkdir()
        self.output = self.base / "output"

    def seal(self, manifest):
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        manifest["manifest_sha256"] = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        (self.census / "holdout-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fixture(self, lines=12):
        pages = {}
        prefixes = dict(ja="\u3042\u3044\u3046", en="English", zh_hans="\u4eca\u5929\u7684\u8bdd", zh_hant="\u4eca\u5929\u7684\u8a71", ko="\uac00\ub098\ub2e4\ub77c")
        for language in trial.LANGUAGES:
            raw = "\r\n".join(f"Speaker{index % 2}:{prefixes[language]} secret-body-token-{index}." for index in range(lines))
            filename = language + ".txt"
            (self.census / filename).write_bytes(raw.encode("utf-8"))
            page = dict(id=f"web:fixture:{language}:event_story:132:2", source="fixture",
                        language=language, trust="B", kind="event_story")
            pages[language] = dict(status="present", page_id=page["id"], source=page["source"],
                                   language=language, text_sha256=hashlib.sha256(raw.encode()).hexdigest(),
                                   local_body_file=filename, variants=[page])
        manifest = dict(candidate_free=True, gold_free=True,
                        stories=[dict(story_key="event:132:2", pages=pages),
                                 dict(story_key="event:999:9", pages={language: dict(local_body_file="forbidden-labels.json")
                                                                       for language in trial.LANGUAGES})])
        self.seal(manifest)
        return manifest

    def test_five_actual_default_packets_preserve_forty_raw_windows_and_source_only_exports(self):
        self.fixture()
        report = trial._run(self.census, self.output)
        self.assertEqual((report["imported_pages"], report["discovery_packets"], report["source_windows"]), (5, 5, 40))
        self.assertFalse(report["control_added"])
        self.assertFalse(report["labels_or_references_read"])
        self.assertFalse(report["target_views_in_source_packets"])
        items = ar.load_queue(Path(report["store"]))
        self.assertEqual({item.language for item in items}, {"ja", "en", "zh_hans", "zh_tw", "ko"})
        for item in items:
            self.assertEqual(len(item._context["rows"]), 8)
            self.assertEqual(item.id, "arp:" + ap._digest([item.term, item.language, [], item._context])[:32])
            text = (self.output / "packets" / (item.language + ".txt")).read_text(encoding="utf-8")
            self.assertNotIn('"target"', text)
            self.assertNotIn('"targets"', text)
            self.assertNotIn("candidates:", text)
            self.assertEqual(text.count("\ncontext: "), 8)
            with dbstore.connect(Path(report["store"])) as conn:
                for row in item._context["rows"]:
                    view = row["source"]
                    raw = conn.execute("SELECT text FROM web_pages WHERE source=? AND id=?",
                                       (view["source"], view["page_id"])).fetchone()[0]
                    self.assertEqual(raw[view["start"]:view["end"]], view["text"])
                    visible = [json.loads(line[len("context: "):]) for line in text.splitlines() if line.startswith("context: ")]
                    self.assertEqual(next(entry["source"]["text"] for entry in visible if entry["id"] == row["id"]), view["text"])
                    self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), view["sha256"])
            full_task = json.loads((self.output / "internal-tasks" / (item.language + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(full_task[0], item.to_dict())
            self.assertIn("targets", full_task[0]["review_context"]["rows"][0])
        for path, digest in report["artifact_sha256"].items():
            self.assertEqual(trial._hash(Path(path)), digest)

    def test_no_other_family_body_or_reviewer_labels_are_read(self):
        self.fixture()
        forbidden = self.census / "forbidden-labels.json"
        forbidden.write_text("Do not read these annotations", encoding="utf-8")
        original_bytes, original_text = Path.read_bytes, Path.read_text
        seen = []
        def read_bytes(path, *args, **kwargs):
            if path.resolve() == forbidden.resolve():
                self.fail("Trial setup read another family or reviewer labels")
            seen.append(path.resolve())
            return original_bytes(path, *args, **kwargs)
        def read_text(path, *args, **kwargs):
            if path.resolve() == forbidden.resolve():
                self.fail("Trial setup read another family or reviewer labels")
            return original_text(path, *args, **kwargs)
        with patch.object(Path, "read_bytes", read_bytes), patch.object(Path, "read_text", read_text):
            report = trial._run(self.census, self.output)
        self.assertEqual(len(report["input_sha256"]), 6)
        self.assertNotIn(forbidden.resolve(), seen)

    def test_invalid_manifest_hash_or_unblinded_flag_fails_before_output(self):
        for mode in ("hash", "candidate_flag", "gold_flag"):
            with self.subTest(mode=mode):
                manifest = self.fixture()
                if mode == "hash":
                    manifest["manifest_sha256"] = "bad"
                    (self.census / "holdout-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    manifest["candidate_free" if mode == "candidate_flag" else "gold_free"] = 1
                    self.seal(manifest)
                with self.assertRaises(ValueError):
                    trial._run(self.census, self.output)
                self.assertFalse(self.output.exists())

    def test_changed_page_hash_and_body_path_escape_fail_before_output(self):
        manifest = self.fixture()
        body = self.census / "en.txt"
        body.write_bytes(body.read_bytes() + b" changed")
        with self.assertRaisesRegex(ValueError, "page changed"):
            trial._run(self.census, self.output)
        manifest = self.fixture()
        manifest["stories"][0]["pages"]["en"]["local_body_file"] = "../outside.txt"
        self.seal(manifest)
        with self.assertRaisesRegex(ValueError, "escapes"):
            trial._run(self.census, self.output)
        self.assertFalse(self.output.exists())

    def test_missing_duplicate_family_or_ambiguous_page_metadata_fails(self):
        for mode in ("family", "duplicate", "metadata", "language", "page"):
            with self.subTest(mode=mode):
                manifest = self.fixture()
                if mode == "family":
                    manifest["stories"].pop(0)
                elif mode == "duplicate":
                    manifest["stories"].append(manifest["stories"][0])
                elif mode == "metadata":
                    variants = manifest["stories"][0]["pages"]["en"]["variants"]
                    variants.append(dict(variants[0]))
                elif mode == "language":
                    manifest["stories"][0]["pages"]["en"]["variants"][0]["language"] = "ja"
                else:
                    manifest["stories"][0]["pages"].pop("ko")
                self.seal(manifest)
                with self.assertRaises(ValueError):
                    trial._run(self.census, self.output)
                self.assertFalse(self.output.exists())

    def test_short_first_actual_packet_is_not_padded_or_replaced_with_later_work(self):
        self.fixture(lines=7)
        with self.assertRaisesRegex(ValueError, "not eight"):
            trial._run(self.census, self.output)
        self.assertFalse((self.output / "report.json").exists())
        self.assertFalse((self.output / "packets").exists())

    def test_frozen_output_cannot_be_overwritten(self):
        self.fixture()
        self.output.mkdir()
        marker = self.output / "keep.txt"
        marker.write_bytes(b"existing work")
        with self.assertRaises(FileExistsError):
            trial._run(self.census, self.output)
        self.assertEqual(marker.read_bytes(), b"existing work")

    def test_input_change_during_packet_generation_prevents_publication(self):
        self.fixture()
        original = ap._prepare_scrub_review
        def prepare(*args, **kwargs):
            result = original(*args, **kwargs)
            body = self.census / "en.txt"
            body.write_bytes(body.read_bytes() + b" mutation")
            return result
        with patch.object(ap, "_prepare_scrub_review", prepare), self.assertRaisesRegex(ValueError, "inputs changed"):
            trial._run(self.census, self.output)
        self.assertFalse((self.output / "report.json").exists())
        self.assertFalse((self.output / "packets").exists())


if __name__ == "__main__":
    unittest.main()
