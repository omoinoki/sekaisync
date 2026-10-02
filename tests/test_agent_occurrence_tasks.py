import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as os, termindex


class OccurrenceTaskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        dbstore.initialize(self.root)

    def packets(self, version=1):
        if version > 1:
            dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                                 backup_path=Path(self.temp.name) / (str(version) + "-v2.db"))
            if version == 3:
                dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                                     backup_path=Path(self.temp.name) / "v3.db")
        pages = [dict(id="web:fixture:zh_hans:event_story:1:1", source="fixture", language="zh_hans", trust="B", kind="event_story",
                      story_key="event:1:1", text="甲：我的声音和大家的声音。", canonical_key="event_story:zh_hans:1:1"),
                 dict(id="web:fixture:zh_tw:event_story:1:1", source="fixture", language="zh_tw", trust="B", kind="event_story",
                      story_key="event:1:1", text="甲：我的聲音跟大家的聲音。", canonical_key="event_story:zh_tw:1:1")]
        dbstore.upsert_web_pages(self.root, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        items, _ = ap._prepare_scrub_review(self.root, groups, list(groups), ["声音"], {}, "zh_hans", ["zh_tw"])
        translation = next(item for item in items if item._context["task"] == "translation")
        item = ap._occurrence_item(translation)
        ar.enqueue(self.root, [item])
        return item

    def proposal(self, item, kind="lexical", last=True):
        row = item._context["rows"][0]
        return dict(evidence_id=row["id"],
                    source_segments=ap._body_term_segments(row["source"]["text"], "声音", row["source"]["start"])[int(last)],
                    target_segments=ap._body_term_segments(row["target"]["text"], "聲音", row["target"]["start"])[int(last)],
                    sense_key="opinion" if last else "music", sense_gloss="people's expressed opinion" if last else "musical sound",
                    kind=kind, rationale="Context is the people's expressed opinion")

    def test_singleton_submission_v1_v2_v3_never_publishes_global_name(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.root = Path(self.temp.name) / str(version)
                dbstore.initialize(self.root)
                item = self.packets(version)
                result = ar.submit_judgments(self.root, [dict(id=item.id, decision="accept", relations=[self.proposal(item)])])
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["applied_slots"], 0)
                self.assertNotIn(item.id, {pending.id for pending in ar.load_queue(self.root)})
                with dbstore.connect(self.root) as conn:
                    rows = os._read_relations(conn)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["source"]["segments"][0]["start"], 10)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], str(version))
                self.assertEqual(ar.submit_judgments(self.root, [dict(id=item.id, decision="accept", relations=[self.proposal(item)])])["errors"], [])

    def test_stale_or_wrong_segments_leave_task_pending_and_no_partial_write(self):
        item = self.packets()
        bad = self.proposal(item)
        bad["target_segments"][0]["start"] = 0
        result = ar.submit_judgments(self.root, [dict(id=item.id, decision="accept", relations=[self.proposal(item), bad])])
        self.assertTrue(result["errors"])
        self.assertIn(item.id, {pending.id for pending in ar.load_queue(self.root)})
        with dbstore.connect(self.root) as conn:
            self.assertEqual(os._read_relations(conn), [])

    def test_paraphrase_reference_omission_and_unresolved_are_typed_not_names(self):
        for kind in ("paraphrase", "reference", "omitted", "unresolved"):
            with self.subTest(kind=kind):
                self.root = Path(self.temp.name) / kind
                dbstore.initialize(self.root)
                item = self.packets()
                result = ar.submit_judgments(self.root, [dict(id=item.id, decision="accept", relations=[self.proposal(item, kind)])])
                self.assertEqual(result["errors"], [])
                with dbstore.connect(self.root) as conn:
                    rows = os._read_relations(conn)
                    self.assertEqual(rows[0]["kind"], kind)
                    self.assertEqual(rows[0]["publication"], "occurrence_only")
                    self.assertIsNone(os._projection_candidate(rows))

    def test_export_describes_occurrence_contract_using_existing_cli(self):
        item = self.packets()
        rendered = ar.render_item(item)
        self.assertIn("answer_contract", rendered)
        self.assertIn("target_segments", rendered)
        self.assertIn("Unicode code-point", rendered)


if __name__ == "__main__":
    unittest.main()
