import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import prepare_scraper_focused_discovery as prep, run_scraper_focused_discovery as runner
from sekaisync import agent_packets as packets, dbstore, termindex


class FocusedDiscoveryRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "original"
        dbstore.initialize(self.source)
        pages = [dict(source="fixture", id="fixture:en:event_story:35:5", kind="event_story", language="en",
                      trust="B", text="A: a wet towel."),
                 dict(source="fixture", id="fixture:ja:event_story:35:5", kind="event_story", language="ja",
                      trust="B", text="甲：濡れたタオル。")] 
        dbstore.upsert_web_pages(self.source, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        tasks, _ = packets._prepare_scrub_review(self.source, groups, list(groups), [], {}, "en", ["ja"])
        self.tasks = [task.to_dict() for task in tasks]
        row = self.tasks[0]["review_context"]["rows"][0]
        judgments, _ = prep._prepare(self.tasks, dict(language="en", story_key="event:35:5", agent="fixture",
            provenance="Independent raw source fixture", turns=[dict(evidence_id=row["id"], units=[
                dict(kind="literal", surface="wet towel", category="modified_nominal")])]))
        self.tasks_path, self.judgments_path = self.root / "tasks.json", self.root / "judgments.json"
        self.tasks_path.write_text(json.dumps(self.tasks, ensure_ascii=False), encoding="utf-8")
        self.judgments_path.write_text(json.dumps(judgments, ensure_ascii=False), encoding="utf-8")

    def test_real_isolated_submit_replay_has_no_global_names_and_source_store_unchanged(self):
        with dbstore.connect(self.source) as conn:
            revision = dbstore.current_revision(conn)
        report = runner._run(self.source, self.tasks_path, self.judgments_path, self.root / "trial")
        self.assertEqual(report["source_units"], 1)
        self.assertEqual(report["subject_kinds"], dict(literal=1))
        self.assertEqual(report["followup_tasks"], dict(occurrence=1, discovery=1))
        self.assertEqual(report["global_terms"], 0)
        self.assertEqual(report["replay"]["accepted"], 0)
        self.assertIsNone(report["blind_recall"])
        with dbstore.connect(self.source) as conn:
            self.assertEqual(dbstore.current_revision(conn), revision)
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_relations'").fetchone())
        with self.assertRaises(FileExistsError):
            runner._run(self.source, self.tasks_path, self.judgments_path, self.root / "trial")

    def test_task_judgment_id_mismatch_fails_before_output_creation(self):
        self.judgments_path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "cover"):
            runner._run(self.source, self.tasks_path, self.judgments_path, self.root / "trial")
        self.assertFalse((self.root / "trial").exists())

    def test_input_race_does_not_publish_report(self):
        original = runner.review.submit_judgments
        calls = 0

        def changed(store, judgments):
            nonlocal calls
            calls += 1
            result = original(store, judgments)
            if calls == 2:
                self.judgments_path.write_text("[]", encoding="utf-8")
            return result

        with patch.object(runner.review, "submit_judgments", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                runner._run(self.source, self.tasks_path, self.judgments_path, self.root / "trial")
        self.assertFalse((self.root / "trial" / "report.json").exists())


if __name__ == "__main__":
    unittest.main()
