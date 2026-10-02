"""Independent adversarial review of bounded source-only discovery audits."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import agent_packets as packets, agent_review as review, dbstore
from sekaisync import occurrence_store, source_audits as audits


class SourceAuditReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.serial = 0

    def fixture(self, version=1, count=415, target=False, repeated=False):
        self.serial += 1
        store = self.base / f"store-{version}-{self.serial}"
        if version == 1:
            dbstore.initialize(store)
        else:
            dbstore.initialize_new_store(store, target_version=version)
        terms = [f"s{number:03}" for number in range(count)]
        body = " ".join(terms)
        if repeated:
            body = "s000 " + body
        page = dict(id="web:fixture:en:event_story:21:1", source="fixture", language="en",
                    trust="B", kind="event_story", text="Narrator:" + body)
        pages = [page]
        if target:
            pages.append(dict(id="web:fixture:ja:event_story:21:1", source="fixture", language="ja",
                              trust="B", kind="event_story", text="Target:TARGET_SECRET_DO_NOT_READ"))
        dbstore.upsert_web_pages(store, "fixture", pages)
        views = [packets._view(value, [0], packets._lines(value)[1]) for value in pages]
        row = dict(story_key="event:21:1", source=views[0],
                   targets={"ja": views[1]} if target else {},
                   search_text=page["text"].casefold(), search_unwrapped=page["text"].casefold())
        row["id"] = "span:" + packets._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["ja"] if target else [], windows=[row])
        scope_id = packets._digest(scope)
        review._write_json(packets._scope_path(store, scope_id), scope)
        context = dict(schema=packets._SCHEMA, task="discovery", scope_id=scope_id,
                       source_language="en", rows=[row])
        root = packets._item("@discover:event:21:1:" + row["id"], "en", [], "discovery", context, "Fixture")
        review.enqueue(store, [root])
        return store, root, terms, page

    def submit(self, store, item, **answer):
        return review.submit_judgments(store, [dict(id=item.id, decision="accept", **answer)])

    def audit(self, store, stage="review"):
        candidates = [item for item in review.load_queue(store)
                      if audits._is_audit(item._context)
                      and item._context["source_boundary_audit"]["stage"] == stage]
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def ordinary(self, store):
        candidates = [item for item in review.load_queue(store)
                      if item._context.get("task") == "discovery" and not audits._is_audit(item._context)]
        self.assertEqual(len(candidates), 1)
        return candidates[0]

    def entries(self, item, start, end, occurrence=0):
        row = item._context["rows"][0]
        return [dict(kind="literal", evidence_id=row["id"], canonical=f"s{number:03}",
                     segments=packets._body_term_segments(row["source"]["text"], f"s{number:03}",
                                                         row["source"]["start"])[occurrence])
                for number in range(start, end)]

    def clear_pending(self, store):
        if review._uses_sqlite(store):
            with dbstore.connect(store) as conn:
                conn.execute("DELETE FROM review_queue WHERE status='queued'")
                conn.commit()
        else:
            review._write_json(review.queue_path(store), dict(items=[]))

    def forge(self, item, mutate):
        context = deepcopy(item._context)
        mutate(context)
        return packets._item(item.term, item.language, [], item.kind, context, "Rehashed adversarial fixture")

    def assert_invalid(self, store, item):
        with dbstore.connect(store) as conn:
            with self.assertRaises((ValueError, TypeError, KeyError)):
                packets._validate_answer(conn, store, item, dict(decision="accept", terms=[]))

    def test_terminal_receipts_preserve_complete_audit_ancestry_in_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, terms, _ = self.fixture(version, count=3)
                self.assertEqual(self.submit(store, root, terms=terms[:1])["errors"], [])
                child = self.audit(store)
                marker = child._context["source_boundary_audit"]
                self.assertEqual(marker["ordinary_ancestor_ids"], [])
                self.assertEqual(marker["terminal_parent_id"], root.id)
                self.assertEqual(marker["excluded_terms"], terms[:1])
                receipt = packets._receipts(store)[root.id]
                self.assertEqual(receipt["review_context"], root._context)
                self.assertEqual(receipt.get("storage"), None if version == 1 else "sqlite")
                with dbstore.connect(store) as conn:
                    self.assertEqual(audits._validate(conn, store, child), {"s000"})
                if version == 3:
                    self.assertFalse(packets._receipt_path(store).exists())

    def test_authentic_legacy_broad_packet_stays_submittable_and_auditable_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, terms, page = self.fixture(version, count=25)
                self.clear_pending(store)
                page = dict(page, text="\n".join("Narrator:" + term for term in terms))
                dbstore.upsert_web_pages(store, "fixture", [page])
                rows = []
                lines = packets._lines(page)[1]
                for number in range(25):
                    source = packets._view(page, [number], lines)
                    row = dict(story_key="event:21:1", source=source, targets={},
                               search_text=source["text"].casefold(), search_unwrapped=source["text"].casefold())
                    row["id"] = "span:" + packets._digest(row)[:24]
                    rows.append(row)
                scope = dict(source_language="en", target_languages=[], windows=rows)
                scope_id = packets._digest(scope)
                review._write_json(packets._scope_path(store, scope_id), scope)
                context = dict(schema=packets._SCHEMA, task="discovery", scope_id=scope_id,
                               source_language="en", rows=rows)
                root = packets._item("@discover:event:21:1:" + rows[0]["id"], "en", [],
                                     "discovery", context, "Authentic preexisting broad packet")
                review.enqueue(store, [root])
                self.assertGreater(len(rows), packets._DISCOVERY_ROWS)
                before = review.load_queue(store)[0].to_dict()
                review.export_for_agent(store, self.base / f"legacy-broad-before-{version}.txt", limit=0)
                self.assertEqual(review.load_queue(store)[0].to_dict(), before)
                self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
                child = self.audit(store)
                self.assertEqual(child._context["rows"], rows)
                self.assertEqual(child._context["source_boundary_audit"]["terminal_parent_id"], root.id)
                self.assertEqual(self.submit(store, child, terms=[])["errors"], [])
                self.assertEqual(review.load_queue(store), [])
                self.assertEqual(len(packets._receipts(store)), 2)

    def test_full_legacy_and_typed_chain_ends_in_one_receipt_derived_audit(self):
        store, root, terms, _ = self.fixture()
        self.assertEqual(self.submit(store, root, terms=terms[:200], subjects=self.entries(root, 0, 2))["errors"], [])
        ordinary = self.ordinary(store)
        self.assertFalse(any(audits._is_audit(item._context) for item in review.load_queue(store)))
        self.assertEqual(self.submit(store, ordinary, terms=terms[200:203],
                                     subjects=self.entries(ordinary, 2, 5))["errors"], [])
        child = self.audit(store)
        marker = child._context["source_boundary_audit"]
        self.assertEqual(marker["ordinary_ancestor_ids"], [root.id])
        self.assertEqual(marker["terminal_parent_id"], ordinary.id)
        self.assertEqual(marker["excluded_terms"], terms[:203])
        self.assertEqual(len(marker["excluded_subject_ids"]), 5)
        self.assertEqual(len(marker["inherited_subjects"]), 5)
        self.assertNotIn("continuation", child._context)
        self.assertEqual(self.submit(store, child, terms=[], subjects=[])["errors"], [])
        self.assertEqual(review.load_queue(store), [])
        self.assertEqual(audits._ensure(store)["added"], 0)

    def test_two_full_mixed_ancestors_replay_real_receipts_in_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, terms, _ = self.fixture(version)
                self.assertEqual(self.submit(store, root, terms=terms[:200],
                                             subjects=self.entries(root, 0, 2))["errors"], [])
                first = self.ordinary(store)
                self.assertEqual(self.submit(store, first, terms=terms[200:400],
                                             subjects=self.entries(first, 2, 4))["errors"], [])
                terminal = self.ordinary(store)
                self.assertEqual(self.submit(store, terminal, terms=terms[400:],
                                             subjects=self.entries(terminal, 4, 6))["errors"], [])
                child = self.audit(store)
                marker = child._context["source_boundary_audit"]
                self.assertEqual(marker["ordinary_ancestor_ids"], [root.id, first.id])
                self.assertEqual(marker["terminal_parent_id"], terminal.id)
                self.assertEqual(marker["excluded_terms"], terms)
                self.assertEqual(len(marker["excluded_subject_ids"]), 6)
                for item in (root, first, terminal):
                    self.assertEqual(packets._receipts(store)[item.id]["review_context"], item._context)
                self.assertEqual(self.submit(store, child, terms=[], subjects=[])["errors"], [])
                self.assertEqual(len(packets._receipts(store)), 4)
                self.assertEqual(audits._ensure(store)["added"], 0)
                self.assertEqual(review.load_queue(store), [])

    def test_full_typed_chain_keeps_occurrence_exclusions_through_terminal(self):
        store, root, _, _ = self.fixture(count=215)
        self.assertEqual(self.submit(store, root, subjects=self.entries(root, 0, 200))["errors"], [])
        ordinary = self.ordinary(store)
        self.assertEqual(self.submit(store, ordinary, subjects=self.entries(ordinary, 200, 215))["errors"], [])
        child = self.audit(store)
        marker = child._context["source_boundary_audit"]
        self.assertEqual(marker["excluded_terms"], [])
        self.assertEqual(marker["ordinary_ancestor_ids"], [root.id])
        self.assertEqual(marker["terminal_parent_id"], ordinary.id)
        self.assertEqual(len(marker["excluded_subject_ids"]), 215)
        self.assertEqual({item["id"] for item in marker["inherited_subjects"]}, set(marker["excluded_subject_ids"]))

    def test_empty_audit_and_parent_replays_cannot_revive_work(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _, _ = self.fixture(version, count=1)
                self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
                child = self.audit(store)
                self.assertEqual(self.submit(store, child, terms=[])["errors"], [])
                for item in (root, child):
                    self.assertEqual(self.submit(store, item, terms=[])["errors"], [])
                for _ in range(2):
                    self.assertEqual(audits._ensure(store)["added"], 0)
                    self.assertEqual(review.load_queue(store), [])
                self.assertEqual(len(packets._receipts(store)), 2)

    def test_receipt_before_queue_crash_export_recovers_child_without_parent_resurrection(self):
        store, root, _, _ = self.fixture(count=1)
        with patch.object(review, "_write_queue", side_effect=OSError("before queue publication")):
            with self.assertRaisesRegex(OSError, "before queue publication"):
                self.submit(store, root, terms=[])
        self.assertIn(root.id, packets._receipts(store))
        destination = self.base / "recovered-source-audit.txt"
        review.export_for_agent(store, destination, limit=0)
        child = self.audit(store)
        self.assertEqual(child._context["source_boundary_audit"]["terminal_parent_id"], root.id)
        self.assertEqual([item.id for item in review.load_queue(store)], [child.id])
        self.assertEqual(audits._ensure(store)["added"], 0)

    def test_lost_audit_publication_recovers_exact_id_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _, _ = self.fixture(version, count=1)
                self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
                child = self.audit(store)
                self.clear_pending(store)
                self.assertEqual(review.load_queue(store), [])
                review.export_for_agent(store, self.base / f"recover-{version}.txt", limit=0)
                self.assertEqual(self.audit(store).id, child.id)
                self.assertEqual(audits._ensure(store)["added"], 0)
                self.assertNotIn(child.id, packets._receipts(store))

    def test_legacy_sqlite_resolved_audit_without_receipt_requires_real_resubmission(self):
        for version in (2, 3):
            with self.subTest(version=version):
                store, root, _, _ = self.fixture(version, count=1)
                self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
                child = self.audit(store)
                self.assertEqual(self.submit(store, child, terms=[])["errors"], [])
                self.assertIn(child.id, packets._receipts(store))
                with dbstore.connect(store) as conn:
                    conn.execute("DELETE FROM review_decisions WHERE item_id=?", (child.id,))
                    self.assertEqual(conn.execute("SELECT status FROM review_queue WHERE item_id=?",
                                                  (child.id,)).fetchone()[0], "resolved")
                    conn.commit()
                self.assertNotIn(child.id, packets._receipts(store))
                review.export_for_agent(store, self.base / f"legacy-receipt-collision-{version}.txt", limit=0)
                self.assertEqual(self.audit(store).id, child.id)
                self.assertNotIn(child.id, packets._receipts(store))
                self.assertEqual(self.submit(store, child, terms=[])["errors"], [])
                self.assertIn(child.id, packets._receipts(store))
                self.assertEqual(review.load_queue(store), [])

    def test_full_audit_retains_named_debt_and_rejects_fake_accept_reject_replace(self):
        store, root, terms, _ = self.fixture(count=200)
        self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
        child = self.audit(store)
        self.assertEqual(self.submit(store, child, terms=terms)["errors"], [])
        debt = self.audit(store, "saturated")
        self.assertEqual(debt._context["source_boundary_audit"]["audit_parent_id"], child.id)
        self.assertEqual(debt._context["source_boundary_audit"]["excluded_terms"], terms)
        before = set(packets._receipts(store))
        for answer in (dict(decision="accept", terms=[]), dict(decision="reject", terms=[]),
                       dict(decision="replace", value="erased", terms=[])):
            with self.subTest(answer=answer):
                result = review.submit_judgments(store, [dict(id=debt.id, **answer)])
                self.assertTrue(result["errors"])
                self.assertEqual(set(packets._receipts(store)), before)
                self.assertEqual(self.audit(store, "saturated").id, debt.id)
        destination = self.base / "saturated-debt.txt"
        review.export_for_agent(store, destination, limit=0)
        self.assertIn("source_boundary_audit_saturated", destination.read_text(encoding="utf-8"))
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(occurrence_store._read_relations(conn), [])

    def test_full_typed_audit_records_observations_but_not_exhaustiveness(self):
        store, root, _, _ = self.fixture(count=200)
        self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
        child = self.audit(store)
        self.assertEqual(self.submit(store, child, subjects=self.entries(child, 0, 200))["errors"], [])
        debt = self.audit(store, "saturated")
        marker = debt._context["source_boundary_audit"]
        self.assertEqual(marker["excluded_terms"], [])
        self.assertEqual(len(marker["excluded_subject_ids"]), 200)
        self.assertEqual(len(marker["inherited_subjects"]), 200)
        self.assertEqual(self.submit(store, debt, terms=[], subjects=[])["accepted"], 0)
        self.assertNotIn(debt.id, packets._receipts(store))

    def test_existing_target_followup_order_precedes_review_and_saturation_in_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _, _ = self.fixture(version, count=201, target=True)
                self.assertEqual(self.submit(store, root, subjects=self.entries(root, 0, 1))["errors"], [])
                pending = review.load_queue(store)
                self.assertEqual(len(pending), 2)
                first_target = pending[0]
                self.assertEqual(first_target._context["task"], "occurrence")
                child = self.audit(store)
                self.assertEqual(pending[-1].id, child.id)
                self.assertEqual(self.submit(store, child, subjects=self.entries(child, 1, 201))["errors"], [])
                pending = review.load_queue(store)
                debt = self.audit(store, "saturated")
                self.assertEqual(len(pending), 202)
                self.assertEqual(pending[0].id, first_target.id)
                self.assertEqual(pending[-1].id, debt.id)
                self.assertTrue(all(item._context["task"] == "occurrence" for item in pending[:-1]))
                self.assertEqual(len({item._context["subject"]["id"] for item in pending[:-1]}), 201)
                self.assertNotIn(debt.id, packets._receipts(store))

    def test_lost_saturation_debt_recovers_without_extra_review_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, terms, _ = self.fixture(version, count=200)
                self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
                child = self.audit(store)
                self.assertEqual(self.submit(store, child, terms=terms)["errors"], [])
                debt = self.audit(store, "saturated")
                self.clear_pending(store)
                report = audits._ensure(store)
                self.assertEqual(report["added"], 1)
                self.assertEqual(self.audit(store, "saturated").id, debt.id)
                self.assertEqual(len(review.load_queue(store)), 1)
                self.assertEqual(audits._ensure(store)["added"], 0)

    def test_repeated_surfaces_nfkc_aliases_and_exact_subjects_cannot_vote_again(self):
        store, root, terms, _ = self.fixture(count=2)
        self.assertEqual(self.submit(store, root, terms=terms[:1], subjects=self.entries(root, 0, 1))["errors"], [])
        child = self.audit(store)
        for answer in (dict(terms=terms[:1]), dict(terms=["\uff53\uff10\uff10\uff10"]),
                       dict(subjects=self.entries(child, 0, 1))):
            with self.subTest(answer=answer):
                self.assertTrue(self.submit(store, child, **answer)["errors"])
                self.assertNotIn(child.id, packets._receipts(store))
                self.assertEqual(self.audit(store).id, child.id)

    def test_same_surface_different_exact_occurrence_is_not_falsely_excluded(self):
        store, root, _, _ = self.fixture(count=1, repeated=True)
        self.assertEqual(self.submit(store, root, subjects=self.entries(root, 0, 1))["errors"], [])
        child = self.audit(store)
        self.assertEqual(self.submit(store, child, subjects=self.entries(child, 0, 1, occurrence=1))["errors"], [])
        first = json.loads(packets._receipts(store)[root.id]["value"])["subjects"][0]
        second = json.loads(packets._receipts(store)[child.id]["value"])["subjects"][0]
        self.assertEqual(first["canonical"], second["canonical"])
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(review.load_queue(store), [])

    def test_rehashed_null_boolean_numeric_and_foreign_audit_contexts_are_rejected(self):
        store, root, terms, _ = self.fixture(count=2)
        self.assertEqual(self.submit(store, root, terms=terms[:1], subjects=self.entries(root, 0, 1))["errors"], [])
        child = self.audit(store)
        mutations = {
            "null marker": lambda context: context.update(source_boundary_audit=None),
            "boolean marker": lambda context: context.update(source_boundary_audit=True),
            "numeric complete flag": lambda context: context["rows"][0]["source"].update(complete=1),
            "float source offset": lambda context: context["rows"][0]["source"].update(start=0.0),
            "numeric inherited offset": lambda context: context["source_boundary_audit"]["inherited_subjects"][0]["source_segments"][0].update(start=9.0),
            "boolean excluded subject": lambda context: context["source_boundary_audit"].update(excluded_subject_ids=[True]),
            "foreign terminal": lambda context: context["source_boundary_audit"].update(terminal_parent_id="arp:" + "0" * 32),
            "invented ancestor": lambda context: context["source_boundary_audit"].update(ordinary_ancestor_ids=[root.id]),
            "forged terms": lambda context: context["source_boundary_audit"].update(excluded_terms=[]),
            "extra continuation": lambda context: context.update(continuation={}),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                self.assert_invalid(store, self.forge(child, mutate))
        self.assertNotIn(child.id, packets._receipts(store))

    def test_full_capacity_parent_cannot_be_forged_into_an_ordinary_terminal(self):
        store, root, terms, _ = self.fixture(count=200)
        self.assertEqual(self.submit(store, root, terms=terms)["errors"], [])
        context = deepcopy(root._context)
        context["source_boundary_audit"] = dict(schema="sekaisync/source-boundary-audit@1", stage="review",
                                               ordinary_ancestor_ids=[], terminal_parent_id=root.id,
                                               excluded_terms=terms, excluded_subject_ids=[], inherited_subjects=[])
        forged = packets._item(root.term, root.language, [], "discovery", context, "Skipped full ancestor")
        self.assert_invalid(store, forged)
        self.assertNotIn(forged.id, packets._receipts(store))
        self.assertFalse(any(audits._is_audit(item._context) for item in review.load_queue(store)))
        self.assertEqual(self.ordinary(store)._context["continuation"]["parent_id"], root.id)

    def test_full_ordinary_budget_does_not_bypass_strict_raw_context_types(self):
        store, root, terms, _ = self.fixture(count=200)
        forged = self.forge(root, lambda context: context["rows"][0]["source"].update(complete=1))
        with dbstore.connect(store) as conn:
            with self.assertRaises(ValueError):
                packets._validate_answer(conn, store, forged, dict(decision="accept", terms=terms))
        self.assertNotIn(forged.id, packets._receipts(store))
        self.assertEqual([item.id for item in review.load_queue(store)], [root.id])

    def test_invalid_receipt_cannot_retire_unsettled_parent_during_crash_recovery(self):
        store, root, _, _ = self.fixture(count=1)
        context = deepcopy(root._context)
        context["rows"][0]["source"]["complete"] = 1
        receipt = dict(decision="accept", scope_id=context["scope_id"], value="[]", review_context=context)
        review._write_json(packets._receipt_path(store), dict(items={root.id: receipt}))
        report = audits._ensure(store)
        self.assertEqual(report["added"], 0)
        self.assertGreater(report["invalid"], 0)
        self.assertEqual([item.id for item in review.load_queue(store)], [root.id])

    def test_saturation_marker_needs_a_real_full_audit_receipt(self):
        store, root, _, _ = self.fixture(count=1)
        self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
        child = self.audit(store)
        forged = self.forge(child, lambda context: context["source_boundary_audit"].update(
            stage="saturated", audit_parent_id=child.id))
        self.assert_invalid(store, forged)
        self.assertNotIn(forged.id, packets._receipts(store))
        self.assertEqual(self.audit(store).id, child.id)

    def test_foreign_scope_and_raw_stale_parent_receipts_cannot_close_review(self):
        store, root, _, page = self.fixture(count=1)
        self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
        child = self.audit(store)
        receipts = packets._receipts(store)
        original = deepcopy(receipts)
        receipts[root.id]["scope_id"] = "0" * 64
        review._write_json(packets._receipt_path(store), dict(items=receipts))
        self.assertTrue(self.submit(store, child, terms=[])["errors"])
        review._write_json(packets._receipt_path(store), dict(items=original))
        dbstore.upsert_web_pages(store, "fixture", [dict(page, text=page["text"] + " changed")])
        self.assertTrue(self.submit(store, child, terms=[])["errors"])
        self.assertNotIn(child.id, packets._receipts(store))
        self.assertEqual(self.audit(store).id, child.id)

    def test_tampered_receipt_context_cannot_recover_or_validate_even_same_value(self):
        store, root, _, _ = self.fixture(count=1)
        self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
        child = self.audit(store)
        receipts = packets._receipts(store)
        receipts[root.id]["review_context"]["rows"][0]["source"]["complete"] = 1
        review._write_json(packets._receipt_path(store), dict(items=receipts))
        self.assertTrue(self.submit(store, child, terms=[])["errors"])
        self.clear_pending(store)
        recovery = audits._ensure(store)
        self.assertEqual(recovery["added"], 0)
        self.assertGreater(recovery["invalid"], 0)
        self.assertEqual(review.load_queue(store), [])

    def test_real_export_is_source_only_even_with_hidden_target_body_in_scope(self):
        store, root, _, _ = self.fixture(count=3, target=True)
        self.assertEqual(self.submit(store, root, terms=[])["errors"], [])
        child = self.audit(store)
        destination = self.base / "source-only-audit.txt"
        review.export_for_agent(store, destination, limit=0)
        rendered = destination.read_text(encoding="utf-8")
        self.assertIn(child.id, rendered)
        self.assertIn("source_boundary_audit_contract", rendered)
        self.assertIn("Narrator:s000 s001 s002", rendered)
        self.assertNotIn("TARGET_SECRET_DO_NOT_READ", rendered)
        self.assertNotIn("web:fixture:ja:event_story:21:1", rendered)
        marker_json = json.dumps(child._context["source_boundary_audit"], sort_keys=True)
        self.assertNotIn("targets", marker_json)
        self.assertNotIn("review_context", marker_json)


if __name__ == "__main__":
    unittest.main()
