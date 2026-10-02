"""An exact surface is not enough to locate a repeated target occurrence."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, termindex


SOURCE = "幼年咲希：（我的琴声，和大家的声音融合在了一起……！）"
REPEATED_TARGET = "年幼的咲希：（我的聲音跟大家的聲音合而為一了……！）"
UNIQUE_TARGET = "年幼的咲希：（我的琴聲跟大家的聲音合而為一了……！）"


class OccurrencePositionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    def packet(self, version, target, source=SOURCE):
        store = Path(self.temp.name) / (f"v{version}-" + str(len(target)))
        dbstore.initialize(store)
        if version == 2:
            dbstore.migrate_store(
                store, target_version=2, dry_run=False,
                backup_path=Path(self.temp.name) / "v2-position-backup.db",
            )
        pages = []
        for chapter in (1, 2):
            for language, text in (("zh_hans", source), ("zh_tw", target)):
                pages.append(dict(
                    id=f"web:altsource_ms:{language}:event_story:999:{chapter}",
                    source="altsource_ms", language=language,
                    story_key=f"event:999:{chapter}",
                    canonical_key=f"event_story:{language}:999:{chapter}",
                    kind="event_story", trust="B", text=text,
                ))
        dbstore.upsert_web_pages(store, "altsource_ms", pages)
        groups = termindex.group_pages_by_story(pages)
        items, _ = ap._prepare_scrub_review(
            store, groups, sorted(groups), ["声音"], {}, "zh_hans", ["zh_tw"],
        )
        ar.enqueue(store, items)
        item = next(i for i in items if i.kind != "discovery")
        return store, item

    def submit(self, store, item, **extra):
        return ar.submit_judgments(store, [dict(
            id=item.id, decision="replace", value="聲音",
            rationale="源文大家的声音对应目标大家的聲音，不是我的聲音。",
            evidence_ids=[row["id"] for row in item._context["rows"]],
            agent="position-regression", generalize=None,
            **extra,
        )])

    def explicit_spans(self, item):
        spans = []
        for row in item._context["rows"]:
            source, target = row["source"], row["target"]
            source_start = source["start"] + source["text"].rindex("声音")
            target_start = target["start"] + target["text"].rindex("聲音")
            spans.append(dict(
                evidence_id=row["id"],
                source_segments=[dict(start=source_start, end=source_start + 2, exact="声音")],
                target_segments=[dict(start=target_start, end=target_start + 2, exact="聲音")],
            ))
        return spans

    def test_repeated_target_without_positions_does_not_publish_a_guess(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, REPEATED_TARGET)
                self.assertEqual(SOURCE.count("声音"), 1)
                self.assertEqual(REPEATED_TARGET.count("聲音"), 2)
                result = self.submit(store, item)
                self.assertEqual(result["applied_slots"], 0)
                self.assertTrue(result["errors"])
                self.assertIn(item.id, {i.id for i in ar.load_queue(store)})

    def test_unique_target_keeps_legacy_submission_and_correct_position(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, UNIQUE_TARGET)
                result = self.submit(store, item)
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["applied_slots"], 1)
                with dbstore.connect(store) as conn:
                    evidence = dbstore._load_evidence_items(
                        conn, termindex.make_term_id("zh_hans", "声音"),
                    )
                    row = conn.execute(
                        "SELECT names_json FROM terms WHERE id=?",
                        (termindex.make_term_id("zh_hans", "声音"),),
                    ).fetchone()
                self.assertEqual(json.loads(row[0])["zh_tw"], "聲音")
                targets = [e for e in evidence if e.get("language") == "zh_tw"]
                self.assertEqual(len(targets), 2)
                for claim in targets:
                    self.assertEqual(claim["start"], UNIQUE_TARGET.index("聲音"))
                    self.assertEqual(claim["source_start"], SOURCE.index("声音"))

    def test_explicit_second_target_occurrence_is_stored_and_replay_is_idempotent(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, REPEATED_TARGET)
                result = self.submit(store, item, evidence_spans=self.explicit_spans(item))
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["applied_slots"], 1)
                with dbstore.connect(store) as conn:
                    evidence = dbstore._load_evidence_items(
                        conn, termindex.make_term_id("zh_hans", "声音"),
                    )
                targets = [e for e in evidence if e.get("language") == "zh_tw"]
                self.assertEqual(len(targets), 2)
                for claim in targets:
                    self.assertEqual(claim["start"], REPEATED_TARGET.rindex("聲音"))
                    self.assertNotEqual(claim["start"], REPEATED_TARGET.index("聲音"))
                replay = self.submit(store, item, evidence_spans=self.explicit_spans(item))
                self.assertEqual(replay["errors"], [])
                self.assertEqual(replay["applied_slots"], 0)

    def test_repeated_source_without_positions_is_not_assigned_first_source(self):
        repeated_source = "幼年咲希：（我的声音，和大家的声音融合在了一起……！）"
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, UNIQUE_TARGET, source=repeated_source)
                result = self.submit(store, item)
                self.assertEqual(result["applied_slots"], 0)
                self.assertTrue(result["errors"])
                self.assertIn(item.id, {i.id for i in ar.load_queue(store)})

    def test_explicit_exact_text_mismatch_is_rejected_without_settling_task(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, REPEATED_TARGET)
                spans = self.explicit_spans(item)
                spans[0]["target_segments"][0]["exact"] = "我的"
                result = self.submit(store, item, evidence_spans=spans)
                self.assertEqual(result["applied_slots"], 0)
                self.assertTrue(result["errors"])
                self.assertIn(item.id, {i.id for i in ar.load_queue(store)})

    def test_explicit_speaker_label_is_not_a_target_body_occurrence(self):
        target = "聲音：（我的琴聲跟大家的聲音合而為一了……！）"
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, target)
                spans = self.explicit_spans(item)
                for row, span in zip(item._context["rows"], spans):
                    start = row["target"]["start"]
                    span["target_segments"] = [dict(start=start, end=start + 2, exact="聲音")]
                result = self.submit(store, item, evidence_spans=spans)
                self.assertEqual(result["applied_slots"], 0)
                self.assertTrue(result["errors"])
                self.assertIn(item.id, {i.id for i in ar.load_queue(store)})

    def test_explicit_offsets_are_code_points_not_utf16_units(self):
        source = "幼年咲希：（\U00020000我的琴声，和大家的声音融合在了一起……！）"
        target = "年幼的咲希：（\U00020000我的聲音跟大家的聲音合而為一了……！）"
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, target, source=source)
                result = self.submit(store, item, evidence_spans=self.explicit_spans(item))
                self.assertEqual(result["errors"], [])
                with dbstore.connect(store) as conn:
                    evidence = dbstore._load_evidence_items(
                        conn, termindex.make_term_id("zh_hans", "声音"),
                    )
                for claim in (e for e in evidence if e.get("language") == "zh_tw"):
                    self.assertEqual(claim["start"], target.rindex("聲音"))
                    self.assertEqual(claim["source_start"], source.rindex("声音"))

    def test_soft_wrapped_target_uses_two_exact_raw_segments(self):
        target = "年幼的咲希：（我的琴聲跟大家的聲\n音合而為一了……！）"
        for version in (1, 2):
            with self.subTest(version=version):
                store, item = self.packet(version, target)
                spans = []
                for row in item._context["rows"]:
                    source_view, target_view = row["source"], row["target"]
                    s = source_view["start"] + source_view["text"].index("声音")
                    a = target_view["start"] + target_view["text"].rindex("聲")
                    b = target_view["start"] + target_view["text"].index("音")
                    spans.append(dict(
                        evidence_id=row["id"],
                        source_segments=[dict(start=s, end=s + 2, exact="声音")],
                        target_segments=[dict(start=a, end=a + 1, exact="聲"),
                                         dict(start=b, end=b + 1, exact="音")],
                    ))
                result = self.submit(store, item, evidence_spans=spans)
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["applied_slots"], 1)
                with dbstore.connect(store) as conn:
                    evidence = dbstore._load_evidence_items(
                        conn, termindex.make_term_id("zh_hans", "声音"),
                    )
                targets = [e for e in evidence if e.get("language") == "zh_tw"]
                self.assertEqual(len(targets), 2)
                for claim in targets:
                    self.assertEqual(claim["observed_surface"], "聲音")
                    self.assertEqual(len(claim["target_segments"]), 2)


if __name__ == "__main__":
    unittest.main()
