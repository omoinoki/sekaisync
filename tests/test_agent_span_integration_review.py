"""Independent real-queue adversaries for additive typed span discovery."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger, termindex as ti


class AgentSpanIntegrationReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.source = dict(id="web:fixture:en:event_story:999:1", source="fixture", kind="event_story",
                           language="en", trust="B", canonical_key="event_story:en:999:1",
                           text="Futaba: We were lending her a hand, then lending him a hand.\nHonami: Build confidence.")
        self.target = dict(id="web:fixture:zh_hans:event_story:999:1", source="fixture", kind="event_story",
                           language="zh_hans", trust="B", canonical_key="event_story:zh_hans:999:1",
                           text="二叶：我们向她伸出了援手，后来又向他伸出了援手。\n穗波：建立信心。")
        dbstore.upsert_web_pages(self.store, "fixture", [self.source, self.target])
        groups = ti.group_pages_by_story([self.source, self.target])
        items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, "en", ["zh_hans"])
        self.root = next(item for item in items if item._context["task"] == "discovery")
        ar.enqueue(self.store, [self.root])
        self.row = next(row for row in self.root._context["rows"] if "lending her" in row["source"]["text"])

    def parts(self, text, fragments, cursor=0, base=0):
        result = []
        for exact in fragments:
            start = text.index(exact, cursor)
            end = start + len(exact)
            result.append(dict(start=base + start, end=base + end, exact=exact))
            cursor = end
        return result

    def spec(self, *, second=False):
        cursor = self.source["text"].index("lending him") if second else 0
        return dict(kind="segmented", evidence_id=self.row["id"],
                    segments=self.parts(self.source["text"], ("lending", "a hand"), cursor))

    def submit(self, specs, terms=None):
        return ar.submit_judgments(self.store, [dict(id=self.root.id, decision="accept",
                                                   terms=[] if terms is None else terms, subjects=specs,
                                                   rationale="Grounded raw source fragments; contextual meaning is reviewed separately.")])

    def assert_pending_without_partial_discovery(self):
        self.assertNotIn(self.root.id, ap._receipts(self.store))
        self.assertEqual([item.id for item in ar.load_queue(self.store)], [self.root.id])
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(ledger._read_relations(conn), [])

    def occurrence_items(self):
        return [item for item in ar.load_queue(self.store) if item._context.get("task") == "occurrence"
                and item._context.get("subject")]

    def relation(self, item, *, source_segments=None):
        row = item._context["rows"][0]
        return dict(evidence_id=row["id"],
                    source_segments=source_segments or item._context["subject"]["source"]["segments"],
                    target_segments=self.parts(row["target"]["text"], ("伸出了援手",), base=row["target"]["start"]),
                    sense_key="helping-in-this-turn", sense_gloss="Helping this beneficiary in the utterance",
                    kind="lexical", rationale="The exact source expression corresponds to this target expression.")

    def test_actual_export_describes_additive_subject_specs_and_raw_source(self):
        path = ar.export_for_agent(self.store, Path(self.temp.name) / "agent-export.txt", limit=10)
        exported = path.read_text(encoding="utf-8")
        self.assertIn("subjects", exported)
        self.assertIn("segmented", exported)
        self.assertIn("lending her a hand", exported)
        self.assertIn(self.row["id"], exported)
        self.assertIn("terms", exported)

    def test_true_submission_creates_occurrence_only_subjects_with_raw_gaps(self):
        result = self.submit([self.spec()])
        self.assertEqual(result["errors"], [])
        items = self.occurrence_items()
        self.assertEqual(len(items), 1)
        subject = items[0]._context["subject"]
        self.assertEqual(subject["kind"], "segmented")
        self.assertEqual(subject["canonical_parts"], ["lending", "a hand"])
        self.assertEqual(subject["gap_text"], [" her "])
        self.assertEqual(subject["source"]["segments"], self.spec()["segments"])
        self.assertEqual(items[0].term, "lending a hand")
        receipt = json.loads(ap._receipts(self.store)[self.root.id]["value"])
        self.assertEqual(receipt["terms"], [])
        self.assertEqual(receipt["subjects"], [subject])
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(ledger._read_relations(conn), [])

    def test_same_display_two_occurrences_remain_two_distinct_subject_packets(self):
        result = self.submit([self.spec(), self.spec(second=True)])
        self.assertEqual(result["errors"], [])
        items = self.occurrence_items()
        self.assertEqual(len(items), 2)
        subjects = [item._context["subject"] for item in items]
        self.assertEqual({subject["canonical"] for subject in subjects}, {"lending a hand"})
        self.assertEqual(len({subject["id"] for subject in subjects}), 2)
        self.assertEqual({tuple(subject["gap_text"]) for subject in subjects}, {(" her ",), (" him ",)})

    def test_old_string_discovery_still_cannot_drop_lexical_gap(self):
        result = self.submit([], terms=["lending a hand"])
        self.assertTrue(result["errors"])
        self.assert_pending_without_partial_discovery()

    def test_valid_and_invalid_subject_in_one_judgment_do_not_partially_resolve(self):
        invalid = dict(kind="segmented", evidence_id=self.row["id"],
                       segments=self.parts(self.source["text"], ("Futaba", "a hand")))
        result = self.submit([self.spec(), invalid], terms=["lending"])
        self.assertTrue(result["errors"])
        self.assert_pending_without_partial_discovery()

    def test_unknown_evidence_and_segments_outside_exported_source_row_are_rejected(self):
        unknown = dict(self.spec(), evidence_id="not-an-exported-evidence-row")
        outside = dict(kind="segmented", evidence_id=self.row["id"],
                       segments=self.parts(self.source["text"], ("Build", "confidence")))
        for invalid in (unknown, outside):
            with self.subTest(invalid=invalid):
                self.assertTrue(self.submit([invalid])["errors"])
                self.assert_pending_without_partial_discovery()

    def test_forged_segmented_canonical_and_preconstructed_subject_are_not_trusted(self):
        invalid = dict(self.spec(), canonical="lend a hand")
        full_subject = dict(self.spec(), schema="sekaisync/span-subject@1", id="subject:segmented:forged",
                            canonical_parts=["lend", "a hand"], gap_text=[" "])
        for value in (invalid, full_subject):
            with self.subTest(value=value):
                self.assertTrue(self.submit([value])["errors"])
                self.assert_pending_without_partial_discovery()

    def test_bad_quotes_order_overlap_and_boolean_offsets_are_rejected(self):
        valid = self.spec()
        quotes = deepcopy(valid)
        quotes["segments"][0]["exact"] = "lend"
        order = dict(valid, segments=list(reversed(valid["segments"])))
        overlap = deepcopy(valid)
        overlap["segments"][1] = dict(start=valid["segments"][0]["start"] + 1,
                                      end=valid["segments"][0]["end"], exact="ending")
        boolean = deepcopy(valid)
        boolean["segments"][0]["start"] = True
        for invalid in (quotes, order, overlap, boolean):
            with self.subTest(invalid=invalid):
                self.assertTrue(self.submit([invalid])["errors"])
                self.assert_pending_without_partial_discovery()

    def test_rehashed_packet_cannot_widen_immutable_discovery_source_range(self):
        context = deepcopy(self.root._context)
        row = next(row for row in context["rows"] if row["id"] == self.row["id"])
        row["source"].update(start=0, end=len(self.source["text"]), text=self.source["text"])
        forged = ap._item(self.root.term, self.root.language, [], "discovery", context, "")
        spec = dict(kind="segmented", evidence_id=self.row["id"],
                    segments=self.parts(self.source["text"], ("Build", "confidence")))
        with dbstore.connect(self.store) as conn, self.assertRaisesRegex(ValueError, "scope"):
            ap._validate_answer(conn, self.store, forged, dict(decision="accept", terms=[], subjects=[spec]))
        self.assert_pending_without_partial_discovery()

    def test_changed_source_page_cannot_close_typed_discovery(self):
        dbstore.upsert_web_pages(self.store, "fixture", [dict(self.source, text=self.source["text"] + " Changed.")])
        self.assertTrue(self.submit([self.spec()])["errors"])
        self.assert_pending_without_partial_discovery()

    def test_true_occurrence_submission_uses_typed_subject_identity_not_display_lex_id(self):
        self.assertEqual(self.submit([self.spec()])["errors"], [])
        item = self.occurrence_items()[0]
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", relations=[self.relation(item)])])
        self.assertEqual(result["errors"], [])
        with dbstore.connect(self.store) as conn:
            rows = ledger._read_relations(conn)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["sense"]["term_id"], item._context["subject"]["id"])
            self.assertEqual(rows[0]["source"], item._context["subject"]["source"])
            self.assertNotEqual(rows[0]["sense"]["term_id"], ledger._identity("lex:", ["en", item.term]))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_occurrence_cannot_switch_to_another_source_occurrence_with_same_display(self):
        self.assertEqual(self.submit([self.spec()])["errors"], [])
        item = self.occurrence_items()[0]
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", relations=[
            self.relation(item, source_segments=self.spec(second=True)["segments"])])])
        self.assertTrue(result["errors"])
        self.assertIn(item.id, {pending.id for pending in ar.load_queue(self.store)})
        with dbstore.connect(self.store) as conn:
            self.assertEqual(ledger._read_relations(conn), [])

    def test_changed_target_page_cannot_accept_typed_occurrence_relation(self):
        self.assertEqual(self.submit([self.spec()])["errors"], [])
        item = self.occurrence_items()[0]
        dbstore.upsert_web_pages(self.store, "fixture", [dict(self.target, text=self.target["text"] + "变化。")])
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", relations=[self.relation(item)])])
        self.assertTrue(result["errors"])
        self.assertIn(item.id, {pending.id for pending in ar.load_queue(self.store)})
        with dbstore.connect(self.store) as conn:
            self.assertEqual(ledger._read_relations(conn), [])


if __name__ == "__main__":
    unittest.main()
