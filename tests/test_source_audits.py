"""Bounded source review through unchanged real export and submit entry points."""
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import (
    agent_packets as packets, agent_review as review, dbstore, occurrence_store,
    source_audits, span_subjects, termindex,
)


class SourceAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def fixture(self, version=1, text=None):
        store = self.path / ("store-" + str(version))
        if version == 1:
            dbstore.initialize(store)
        else:
            dbstore.initialize_new_store(store, target_version=version)
        text = text or "Host: " + " ".join("w" + str(index) for index in range(300)) + "."
        source = dict(id="web:fixture:en:event_story:999:1", source="fixture", language="en",
                      kind="event_story", canonical_key="event_story:en:999:1", trust="B", text=text)
        target = dict(id="web:fixture:ja:event_story:999:1", source="fixture", language="ja",
                      kind="event_story", canonical_key="event_story:ja:999:1", trust="B",
                      text="Guest: TARGET_SECRET not source review material.")
        dbstore.upsert_web_pages(store, "fixture", [source, target])
        groups = termindex.group_pages_by_story([source, target])
        items, _ = packets._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", ["ja"])
        review.enqueue(store, items)
        root = next(item for item in items if item.kind == "discovery")
        return store, root, source

    def submit(self, store, item, **answer):
        return review.submit_judgments(store, [dict(id=item.id, decision="accept", **answer)])

    def audits(self, store, stage="review"):
        return [item for item in review.load_queue(store)
                if item._context.get("source_boundary_audit", {}).get("stage") == stage]

    def literal(self, root, exact, start=None):
        row = root._context["rows"][0]
        position = row["source"]["text"].index(exact) + row["source"]["start"] if start is None else start
        return dict(kind="literal", evidence_id=row["id"], canonical=exact,
                    segments=[dict(start=position, end=position + len(exact), exact=exact)])

    def segmented(self, item, fragments):
        row = item._context["rows"][0]
        parts = [self.literal(item, exact)["segments"][0] for exact in fragments]
        return dict(kind="segmented", evidence_id=row["id"], segments=parts)

    def assert_subject_followup(self, store, audit, spec, page):
        row = audit._context["rows"][0]
        if spec["kind"] == "segmented":
            expected = span_subjects._segmented(row["source"], row["story_key"], spec["segments"])
        else:
            expected = span_subjects._literal(row["source"], row["story_key"],
                                              spec["canonical"], spec["segments"])
        children = [item for item in review.load_queue(store)
                    if item._context.get("task") == "occurrence"
                    and item._context.get("parent_discovery_id") == audit.id
                    and item._context.get("subject", {}).get("id") == expected["id"]]
        child, = children
        self.assertEqual(child._context["subject"], expected)
        self.assertEqual(child._context["rows"][0]["id"], row["id"])
        self.assertEqual(child._context["rows"][0]["source"], row["source"])
        self.assertEqual(expected["source"]["page_id"], page["id"])
        self.assertEqual(expected["source"]["segments"], spec["segments"])
        receipt = packets._receipts(store)[audit.id]
        self.assertIn(expected, json.loads(receipt["value"])["subjects"])
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(occurrence_store._read_relations(conn), [])
        return expected

    def test_actual_export_and_submit_preserve_unified_sequence_with_real_lexical_gaps(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, page = self.fixture(
                    version, "Tutor: First mix the paint, then test it, and finally let it dry.")
                self.assertFalse(self.submit(store, root, subjects=[self.literal(root, "paint")])["errors"])
                audit, = self.audits(store)
                exported = review.export_for_agent(store, self.path / ("frames-" + str(version) + ".txt"), limit=0)
                rendered = exported.read_text(encoding="utf-8")
                for facet in ("sequencing", "condition/concession", "comparison", "correlative", "coordination"):
                    self.assertIn(facet, rendered)
                self.assertIn("not unrelated fragments", rendered)
                self.assertIn("union of separately registered fragments", rendered)
                spec = self.segmented(audit, ["First", "then", "finally"])
                self.assertFalse(self.submit(store, audit, subjects=[spec])["errors"])
                subject = self.assert_subject_followup(store, audit, spec, page)
                self.assertEqual(subject["gap_text"], [" mix the paint, ", " test it, and "])
                self.assertEqual(subject["canonical_parts"], ["First", "then", "finally"])
                self.assertEqual(self.audits(store), [])
                self.assertEqual(len(packets._receipts(store)), 2)

    def test_actual_export_and_submit_preserve_conditional_frame_without_closing_gaps(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, page = self.fixture(
                    version, "Tutor: If you need red, then choose it; otherwise take blue.")
                self.assertFalse(self.submit(store, root, subjects=[self.literal(root, "red")])["errors"])
                audit, = self.audits(store)
                exported = review.export_for_agent(store, self.path / ("conditional-" + str(version) + ".txt"), limit=0)
                self.assertIn("one unified contextual construction", exported.read_text(encoding="utf-8"))
                spec = self.segmented(audit, ["If", "then", "otherwise"])
                self.assertFalse(self.submit(store, audit, subjects=[spec])["errors"])
                subject = self.assert_subject_followup(store, audit, spec, page)
                self.assertEqual(subject["gap_text"], [" you need red, ", " choose it; "])
                self.assertNotIn("canonical", spec)
                self.assertEqual(self.audits(store), [])

    def test_actual_export_allows_coordinated_inner_layers_but_rejects_synthesized_shared_modifier(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, page = self.fixture(version, "Tutor: the old brushes and pencils need cleaning.")
                whole = self.literal(root, "the old brushes and pencils")
                self.assertFalse(self.submit(store, root, subjects=[whole])["errors"])
                audit, = self.audits(store)
                exported = review.export_for_agent(store, self.path / ("coordination-" + str(version) + ".txt"), limit=0)
                rendered = exported.read_text(encoding="utf-8")
                self.assertIn("whole/reference/individual-conjunct inner layers", rendered)
                self.assertIn("Never synthesize omitted or shared modifiers", rendered)
                absent = self.literal(audit, "pencils")
                absent["canonical"] = "old pencils"
                rejected = self.submit(store, audit, subjects=[absent])
                self.assertTrue(rejected["errors"])
                self.assertNotIn(audit.id, packets._receipts(store))
                self.assertEqual([item.id for item in self.audits(store)], [audit.id])
                first = self.literal(audit, "the old brushes")
                second = self.literal(audit, "pencils")
                self.assertFalse(self.submit(store, audit, subjects=[first, second])["errors"])
                first_subject = self.assert_subject_followup(store, audit, first, page)
                second_subject = self.assert_subject_followup(store, audit, second, page)
                inherited, = audit._context["source_boundary_audit"]["inherited_subjects"]
                self.assertNotEqual(inherited["id"], first_subject["id"])
                self.assertNotEqual(first_subject["id"], second_subject["id"])
                self.assertEqual(self.audits(store), [])

    def test_empty_ordinary_answer_gets_exactly_one_audit_and_empty_audit_has_no_successor_all_versions(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _ = self.fixture(version)
                result = self.submit(store, root, terms=[])
                self.assertFalse(result["errors"])
                audit, = self.audits(store)
                self.assertEqual(audit._context["rows"], root._context["rows"])
                self.assertEqual(audit._context["source_boundary_audit"]["ordinary_ancestor_ids"], [])
                self.assertEqual(audit._context["source_boundary_audit"]["terminal_parent_id"], root.id)
                self.assertFalse(self.submit(store, audit, terms=[])["errors"])
                self.assertEqual(self.audits(store), [])
                before = review.queue_path(store).read_bytes() if version == 1 else None
                review.export_for_agent(store, self.path / ("export-" + str(version) + ".txt"))
                self.assertEqual(self.audits(store), [])
                if version == 1:
                    self.assertEqual(review.queue_path(store).read_bytes(), before)
                receipts = packets._receipts(store)
                self.assertEqual(receipts[root.id]["review_context"], root._context)
                self.assertEqual(receipts[audit.id]["review_context"], audit._context)

    def test_real_full_legacy_ancestor_and_typed_terminal_inherit_separate_exclusions(self):
        store, root, _ = self.fixture()
        self.assertFalse(self.submit(store, root, terms=["w" + str(index) for index in range(200)])["errors"])
        self.assertEqual(self.audits(store), [])
        continuation = next(item for item in review.load_queue(store) if item.kind == "discovery")
        self.assertIn("continuation", continuation._context)
        spec = self.literal(continuation, "w200")
        self.assertFalse(self.submit(store, continuation, subjects=[spec])["errors"])
        audit, = self.audits(store)
        marker = audit._context["source_boundary_audit"]
        self.assertEqual(marker["ordinary_ancestor_ids"], [root.id])
        self.assertEqual(marker["terminal_parent_id"], continuation.id)
        self.assertEqual(len(marker["excluded_terms"]), 200)
        self.assertEqual(len(marker["excluded_subject_ids"]), 1)
        self.assertNotIn("continuation", audit._context)
        new_typed = self.literal(audit, "w0")
        self.assertFalse(self.submit(store, audit, subjects=[new_typed])["errors"])
        self.assertEqual(self.audits(store), [])

    def test_real_full_typed_ancestor_and_legacy_terminal_keep_surface_and_occurrence_debts_distinct(self):
        store, root, _ = self.fixture()
        subjects = [self.literal(root, "w" + str(index)) for index in range(200)]
        self.assertFalse(self.submit(store, root, subjects=subjects)["errors"])
        continuation = next(item for item in review.load_queue(store) if item.kind == "discovery")
        self.assertFalse(self.submit(store, continuation, terms=["w200"])["errors"])
        audit, = self.audits(store)
        marker = audit._context["source_boundary_audit"]
        self.assertEqual(marker["excluded_terms"], ["w200"])
        self.assertEqual(len(marker["excluded_subject_ids"]), 200)
        self.assertFalse(self.submit(store, audit, terms=["w0"])["errors"])
        self.assertEqual(self.audits(store), [])

    def test_discovered_subject_does_not_suppress_repeated_or_nested_exact_typed_occurrences(self):
        store, root, _ = self.fixture(text="Host: the venue and the venue.")
        first = self.literal(root, "venue")
        self.assertFalse(self.submit(store, root, subjects=[first])["errors"])
        audit, = self.audits(store)
        second_start = root._context["rows"][0]["source"]["text"].rindex("venue")
        repeated = self.literal(audit, "venue", second_start)
        reference = self.literal(audit, "the venue")
        self.assertFalse(self.submit(store, audit, subjects=[repeated, reference])["errors"])
        self.assertEqual(len(packets._receipts(store)), 2)

    def test_existing_target_followup_order_is_preserved_before_additive_source_review_v1_v2(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, root, _ = self.fixture(version)
                self.assertFalse(self.submit(store, root, subjects=[self.literal(root, "w0")])["errors"])
                queue = review.load_queue(store)
                audit, = self.audits(store)
                target_items = [item for item in queue if item._context.get("task") != "discovery"]
                self.assertTrue(target_items)
                self.assertEqual(queue[-1].id, audit.id)
                positions = {item.id: index for index, item in enumerate(queue)}
                self.assertTrue(all(positions[item.id] < positions[audit.id] for item in target_items))

    def test_full_audit_is_visible_saturated_debt_and_empty_answer_cannot_close_it_all_versions(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _ = self.fixture(version)
                self.assertFalse(self.submit(store, root, terms=[])["errors"])
                audit, = self.audits(store)
                result = self.submit(store, audit, terms=["w" + str(index) for index in range(200)])
                self.assertFalse(result["errors"])
                self.assertEqual(self.audits(store), [])
                debt, = self.audits(store, "saturated")
                self.assertEqual(review.load_queue(store)[-1].id, debt.id)
                marker = debt._context["source_boundary_audit"]
                self.assertEqual(marker["audit_parent_id"], audit.id)
                self.assertEqual(len(marker["excluded_terms"]), 200)
                result = self.submit(store, debt, terms=[])
                self.assertTrue(result["errors"])
                self.assertIn("source_boundary_audit_saturated", result["errors"][0])
                self.assertEqual([item.id for item in self.audits(store, "saturated")], [debt.id])
                exported = review.export_for_agent(store, self.path / ("debt-" + str(version) + ".txt"), limit=0)
                text = exported.read_text(encoding="utf-8")
                self.assertIn("source_boundary_audit_saturated", text)
                self.assertIn("not a semantic judgment task", text)
                self.assertNotIn(debt.id, packets._receipts(store))

    def test_recovery_reconstructs_missing_audit_and_saturated_queue_from_actual_receipts(self):
        store, root, _ = self.fixture()
        self.assertFalse(self.submit(store, root, terms=[])["errors"])
        audit, = self.audits(store)
        review._write_json(review.queue_path(store), {"items": []})
        recovered = source_audits._ensure(store)
        self.assertEqual(recovered["added"], 1)
        self.assertEqual([item.id for item in self.audits(store)], [audit.id])
        self.assertEqual(source_audits._ensure(store)["added"], 0)
        self.assertFalse(self.submit(store, audit, terms=["w" + str(index) for index in range(200)])["errors"])
        debt, = self.audits(store, "saturated")
        review._write_json(review.queue_path(store), {"items": []})
        self.assertEqual(source_audits._ensure(store)["added"], 1)
        self.assertEqual([item.id for item in self.audits(store, "saturated")], [debt.id])
        self.assertEqual(source_audits._ensure(store)["added"], 0)

    def test_actual_v1_receipt_before_queue_crash_retires_only_real_settled_parent(self):
        store, root, _ = self.fixture()
        with patch.object(review, "_write_queue", side_effect=OSError("publication crash")):
            with self.assertRaisesRegex(OSError, "publication crash"):
                self.submit(store, root, terms=[])
        self.assertIn(root.id, packets._receipts(store))
        self.assertIn(root.id, [item.id for item in review.load_queue(store)])
        exported = review.export_for_agent(store, self.path / "crash-export.txt", limit=0)
        self.assertNotIn(root.id, [item.id for item in review.load_queue(store)])
        self.assertNotIn("id=" + root.id, exported.read_text(encoding="utf-8"))
        audit, = self.audits(store)
        self.assertEqual(audit._context["source_boundary_audit"]["terminal_parent_id"], root.id)

    def test_old_sql_resolved_audit_without_own_decision_is_reopened_not_receipted(self):
        for version in (2, 3):
            with self.subTest(version=version):
                store, root, _ = self.fixture(version)
                self.assertFalse(self.submit(store, root, terms=[])["errors"])
                audit, = self.audits(store)
                self.assertFalse(self.submit(store, audit, terms=[])["errors"])
                with dbstore.connect(store) as conn:
                    conn.execute("DELETE FROM review_decisions WHERE item_id=?", (audit.id,))
                    conn.commit()
                self.assertNotIn(audit.id, packets._receipts(store))
                result = source_audits._ensure(store)
                self.assertEqual(result["requeued"], 1)
                self.assertEqual([item.id for item in self.audits(store)], [audit.id])
                self.assertNotIn(audit.id, packets._receipts(store))
                self.assertFalse(self.submit(store, audit, terms=[])["errors"])
                self.assertIn(audit.id, packets._receipts(store))

    def test_audit_export_does_not_leak_target_in_nested_metadata(self):
        store, root, _ = self.fixture()
        self.assertFalse(self.submit(store, root, subjects=[self.literal(root, "w0")])["errors"])
        audit, = self.audits(store)
        text = "\n".join(packets._render_context(audit))
        self.assertNotIn("TARGET_SECRET", text)
        self.assertNotIn('"target"', text)
        self.assertIn("inherited_subjects", text)
        self.assertIn('"exact": "w0"', text)

    def test_rehashed_row_and_exclusion_type_aliases_are_rejected_before_empty_answer(self):
        store, root, _ = self.fixture()
        self.assertFalse(self.submit(store, root, terms=[])["errors"])
        audit, = self.audits(store)
        mutations = [lambda value: value["rows"][0]["source"].update(complete=1),
                     lambda value: value["rows"][0]["source"].update(start=0.0),
                     lambda value: value["source_boundary_audit"].update(excluded_terms=["w0"]),
                     lambda value: value.update(source_boundary_audit=None)]
        for mutate in mutations:
            context = deepcopy(audit._context)
            mutate(context)
            forged = packets._item(audit.term, audit.language, [], "discovery", context, "")
            with dbstore.connect(store) as conn:
                with self.assertRaises(ValueError):
                    source_audits._validate(conn, store, forged)

    def test_full_ordinary_answer_cannot_bypass_strict_raw_context_before_creating_capacity_child(self):
        store, root, _ = self.fixture()
        context = deepcopy(root._context)
        context["rows"][0]["source"]["complete"] = 1
        forged = packets._item(root.term, root.language, [], "discovery", context, "")
        review.enqueue(store, [forged])
        result = self.submit(store, forged, terms=["w" + str(index) for index in range(200)])
        self.assertTrue(result["errors"])
        self.assertIn("exact corpus scope", result["errors"][0])
        self.assertNotIn(forged.id, packets._receipts(store))
        self.assertFalse(any("continuation" in item._context for item in review.load_queue(store)))

    def test_preexisting_finite_broad_packet_keeps_its_exact_rows_and_id_without_new_answer_fields(self):
        store, root, _ = self.fixture(text="\n".join("Host: w" + str(index) + "." for index in range(80)))
        scope = packets._read_scope(store, root._context["scope_id"])
        self.assertEqual(len(scope["windows"]), 80)
        context = deepcopy(root._context)
        context["rows"] = deepcopy(scope["windows"])
        old = packets._item(root.term, root.language, [], "discovery", context, "Old finite broad task")
        review.enqueue(store, [old])
        self.assertFalse(self.submit(store, old, terms=[])["errors"])
        audit, = self.audits(store)
        self.assertEqual(audit._context["rows"], old._context["rows"])
        self.assertEqual(audit._context["source_boundary_audit"]["terminal_parent_id"], old.id)
        self.assertFalse(self.submit(store, audit, terms=[])["errors"])
        self.assertIn(old.id, packets._receipts(store))

    def test_raw_page_drift_rejects_empty_audit_and_no_stale_child_recovery(self):
        store, root, page = self.fixture()
        self.assertFalse(self.submit(store, root, terms=[])["errors"])
        audit, = self.audits(store)
        dbstore.upsert_web_pages(store, "fixture", [dict(page, text=page["text"] + " Changed.")])
        result = self.submit(store, audit, terms=[])
        self.assertTrue(result["errors"])
        self.assertIn("page changed", result["errors"][0])
        review._write_json(review.queue_path(store), {"items": []})
        self.assertEqual(source_audits._ensure(store)["added"], 0)
        self.assertEqual(self.audits(store), [])

    def test_old_v1_receipt_without_context_stays_compatible_and_does_not_invent_audit(self):
        store, root, _ = self.fixture()
        receipts = {root.id: dict(decision="accept", value="[]", scope_id=root._context["scope_id"])}
        review._write_json(packets._receipt_path(store), {"items": receipts})
        review._write_json(review.queue_path(store), {"items": []})
        result = source_audits._ensure(store)
        self.assertEqual(result["added"], 0)
        self.assertEqual(result["legacy_without_context"], 1)
        self.assertFalse(review.submit_judgments(store, [dict(id=root.id, decision="accept", terms=[])])["errors"])
        self.assertEqual(self.audits(store), [])


if __name__ == "__main__":
    unittest.main()
