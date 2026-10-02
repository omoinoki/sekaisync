"""Duplicate work retires only through reproducible actual review receipts."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import agent_packets as ap, agent_review as ar, cohesion_work as cw, dbstore, occurrence_store as oc


class CohesionWorkTests(unittest.TestCase):
    def setUp(self):
        from tests.test_agent_occurrence_cohesion import OccurrenceCohesionTests
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = OccurrenceCohesionTests()
        self.fixture.base = self.base

    def packets(self, version=1, extra_context=0):
        store, scope_id, scope = self.fixture.fixture(version, lines=1, extra_context=extra_context)
        initial = self.fixture.item(scope_id, scope, 0, "ja")
        focus = self.fixture.item(scope_id, scope, 0, "ko")
        context = {key: value for key, value in focus._context.items() if key != "focus"}
        original = ap._item(focus.term, focus.language, [], "pending", context, "Independent original work")
        return store, initial, original, focus

    def judgment(self, item, proposal):
        return dict(id=item.id, decision="accept", relations=[proposal])

    def submit_original(self, store, initial, original, focus, kind="lexical", batch=False):
        judgments = [self.judgment(initial, self.fixture.proposal(initial)),
                     self.judgment(original, self.fixture.proposal(focus, kind))]
        ar.enqueue(store, [initial, original])
        if batch:
            result = ar.submit_judgments(store, judgments)
        else:
            ar.submit_judgments(store, judgments[:1])
            self.assertIn(focus.id, {item.id for item in ar.load_queue(store)})
            result = ar.submit_judgments(store, judgments[1:])
        self.assertEqual(result["errors"], [])
        return result

    def requeue(self, store, focus):
        if ar._uses_sqlite(store):
            with dbstore.connect(store) as conn:
                conn.execute("UPDATE review_queue SET status='queued' WHERE item_id=?", (focus.id,))
                conn.commit()
        else:
            ar._write_queue(store, ar.load_queue(store) + [focus])

    def test_original_answers_retire_identical_context_focus_v1_v2_v3(self):
        for version in (1, 2, 3):
            for batch in (False, True):
                with self.subTest(version=version, batch=batch):
                    self.fixture.base = self.base / f"{version}-{batch}"
                    store, initial, original, focus = self.packets(version)
                    result = self.submit_original(store, initial, original, focus, batch=batch)
                    self.assertEqual(result["remaining"], 0)
                    if version > 1:
                        self.assertEqual(result["queue_superseded"], 1)
                    else:
                        self.assertNotIn("queue_superseded", result)
                    with dbstore.connect(store) as conn:
                        self.assertEqual(len(oc._read_relations(conn)), 2)
                        self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {cw._TABLE}").fetchone()[0], 1)
                        proof = json.loads(conn.execute(f"SELECT payload_json FROM {cw._TABLE}").fetchone()[0])
                        self.assertEqual(proof["receipt_item_id"], original.id)
                        self.assertNotIn(focus.id, ap._receipts(store, conn=conn))
                        self.assertTrue(cw._is_superseded(conn, store, focus.id))
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                    replay = ar.submit_judgments(store, [self.judgment(focus, self.fixture.proposal(focus))])
                    self.assertEqual((replay["accepted"], replay["unknown"], replay["errors"], replay.get("queue_superseded", 0)),
                                     (0, 0, [], 0))

    def test_unresolved_duplicate_work_retires_but_real_expansion_and_semantic_debt_survive(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.fixture.base = self.base / str(version)
                store, initial, original, focus = self.packets(version, extra_context=10)
                result = self.submit_original(store, initial, original, focus, kind="unresolved", batch=True)
                if version > 1:
                    self.assertEqual(result["queue_superseded"], 1)
                pending = ar.load_queue(store)
                self.assertEqual(len(pending), 1)
                self.assertIn("expansion", pending[0]._context)
                with dbstore.connect(store) as conn:
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {cw._TABLE}").fetchone()[0], 1)
                    self.assertEqual(sum(record["kind"] == "unresolved" for record in oc._read_relations(conn)), 1)
                    proof = json.loads(conn.execute(f"SELECT payload_json FROM {cw._TABLE}").fetchone()[0])
                    self.assertEqual(proof["observed_kind"], "unresolved")
                    self.assertFalse(proof["semantic_resolved"])
                self.assertEqual(ap.ensure_occurrence_cohesion(store)["retired"], 0)
                self.assertEqual(len(ar.load_queue(store)), 1)

    def test_all_real_outcome_kinds_can_supersede_work_without_new_votes(self):
        for kind in ("lexical", "paraphrase", "reference", "omitted", "unresolved"):
            with self.subTest(kind=kind):
                self.fixture.base = self.base / kind
                store, initial, original, focus = self.packets(extra_context=10)
                result = self.submit_original(store, initial, original, focus, kind=kind)
                self.assertEqual(len(ap._receipts(store)), 2)
                with dbstore.connect(store) as conn:
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {cw._TABLE}").fetchone()[0], 1)
                    self.assertTrue(cw._is_superseded(conn, store, focus.id))

    def test_replay_changes_no_database_receipt_or_queue_bytes(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.fixture.base = self.base / str(version)
                store, initial, original, focus = self.packets(version)
                self.submit_original(store, initial, original, focus)
                paths = [store / "kb" / "sekaisync.db", ap._receipt_path(store), ar.queue_path(store)]
                before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.exists()}
                result = ar.submit_judgments(store, [self.judgment(focus, self.fixture.proposal(focus))])
                self.assertEqual(result["unknown"], 0)
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["accepted"], 0)
                self.assertEqual(before, {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in before})

    def test_recovery_after_proof_commits_before_json_queue_drain(self):
        store, initial, original, focus = self.packets()
        self.submit_original(store, initial, original, focus)
        self.requeue(store, focus)
        with patch.object(ar, "_write_queue", side_effect=OSError("interrupted drain")):
            with self.assertRaises(OSError):
                cw.converge(store)
        self.assertEqual([item.id for item in ar.load_queue(store)], [focus.id])
        self.assertEqual(cw.converge(store), dict(retired=1))
        self.assertEqual(ar.load_queue(store), [])
        self.assertEqual(cw.converge(store), dict(retired=0))
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {cw._TABLE}").fetchone()[0], 1)
            self.assertTrue(cw._is_superseded(conn, store, focus.id))

    def test_export_recovers_stale_pending_work_without_new_semantic_writes(self):
        store, initial, original, focus = self.packets()
        self.submit_original(store, initial, original, focus)
        self.requeue(store, focus)
        ar.export_for_agent(store, self.base / "recovered.txt")
        self.assertEqual(ar.load_queue(store), [])
        ar.export_for_agent(store, self.base / "again.txt")
        with dbstore.connect(store) as conn:
            self.assertEqual(len(oc._read_relations(conn)), 2)
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {cw._TABLE}").fetchone()[0], 1)

    def test_missing_wrong_rejected_or_foreign_scope_receipt_cannot_retire_focus(self):
        for mode in ("missing", "wrong_relation", "reject", "wrong_scope"):
            with self.subTest(mode=mode):
                self.fixture.base = self.base / mode
                store, initial, original, focus = self.packets()
                self.submit_original(store, initial, original, focus)
                receipts = ap._receipts(store)
                if mode == "missing":
                    receipts.pop(original.id)
                elif mode == "wrong_relation":
                    receipts[original.id]["value"] = receipts[initial.id]["value"]
                elif mode == "reject":
                    receipts[original.id]["decision"] = "reject"
                else:
                    receipts[original.id]["scope_id"] = "another-scope"
                ar._write_json(ap._receipt_path(store), dict(items=receipts))
                self.requeue(store, focus)
                self.assertEqual(cw.converge(store)["retired"], 0)
                with dbstore.connect(store) as conn:
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                self.assertEqual([item.id for item in ar.load_queue(store)], [focus.id])

    def test_other_source_sense_subject_scope_and_raw_context_are_not_interchangeable(self):
        store, initial, original, focus = self.packets()
        self.submit_original(store, initial, original, focus)
        variants = []
        for mode in ("sense", "scope", "target_view", "source_view", "subject", "source"):
            context = copy.deepcopy(focus._context)
            if mode == "sense":
                old = context["focus"]["sense"]
                context["focus"]["sense"] = oc._sense(old["term_id"], old["source_language"], "another", "another sense")
            elif mode == "scope":
                context["scope_id"] = "another-scope"
            elif mode in {"target_view", "source_view"}:
                view = context["rows"][0][mode.split("_")[0]]
                view["end"] -= 1
                view["text"] = view["text"][:-1]
            elif mode == "subject":
                context["subject"] = dict(id="subject:foreign")
            else:
                context["focus"]["source"]["id"] = "occ:foreign"
            variants.append(ap._item(focus.term, focus.language, [], "pending", context, mode))
        with dbstore.connect(store) as conn:
            remaining, retired = cw._converge_items(conn, store, variants)
            self.assertEqual(remaining, variants)
            self.assertEqual(retired, [])

    def test_stale_page_cannot_retire_or_recognize_saved_alias(self):
        for language in ("en", "ko"):
            with self.subTest(language=language):
                self.fixture.base = self.base / language
                store, initial, original, focus = self.packets()
                self.submit_original(store, initial, original, focus)
                self.requeue(store, focus)
                with dbstore.connect(store) as conn:
                    conn.execute("UPDATE web_pages SET text=text||' changed' WHERE language=?", (language,))
                    conn.commit()
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                self.assertEqual(cw.converge(store)["retired"], 0)

    def test_scan_gap_expansion_and_fallback_debt_are_never_retired(self):
        store, initial, original, focus = self.packets(extra_context=10)
        self.submit_original(store, initial, original, focus, kind="unresolved")
        expansion = ar.load_queue(store)[0]
        debts = [expansion]
        for field in ("scan", "fallback", "gap"):
            context = dict(focus._context, **{field: {"reason": "unreviewed region"}})
            debts.append(ap._item(focus.term, focus.language, [], "pending", context, "Debt"))
        gap_context = dict(focus._context, task="subject_gap")
        debts.append(ap._item(focus.term, focus.language, [], "pending", gap_context, "Subject gap"))
        with dbstore.connect(store) as conn:
            remaining, retired = cw._converge_items(conn, store, debts)
            self.assertEqual(remaining, debts)
            self.assertEqual(retired, [])

    def test_conflicting_current_observation_preserves_pending_work(self):
        store, initial, original, focus = self.packets()
        self.submit_original(store, initial, original, focus)
        with dbstore.connect(store) as conn:
            relation = oc._read_relations(conn, target_language="ko")[0]
            grounding = copy.deepcopy(relation["grounding"])
            grounding["candidates"] = ["singing"]
            other_id = "arp:" + ap._digest([grounding["term"], "ko", ["singing"], grounding["context"]])[:32]
            conflict = oc._relation(relation["source"], relation["target"], relation["sense"], "paraphrase",
                                    other_id, "Conflicting semantic classification", "test", grounding)
            oc._store_relations(conn, [conflict])
            conn.commit()
            self.assertFalse(cw._is_superseded(conn, store, focus.id))
        self.requeue(store, focus)
        self.assertEqual(cw.converge(store)["retired"], 0)

    def test_forged_ledger_payload_or_columns_cannot_skip_replay(self):
        for mode in ("payload", "receipt_column", "relation_column", "packet_identity"):
            with self.subTest(mode=mode):
                self.fixture.base = self.base / mode
                store, initial, original, focus = self.packets()
                self.submit_original(store, initial, original, focus)
                with dbstore.connect(store) as conn:
                    saved = json.loads(conn.execute(f"SELECT payload_json FROM {cw._TABLE}").fetchone()[0])
                    if mode == "receipt_column":
                        conn.execute(f"UPDATE {cw._TABLE} SET receipt_item_id='fabricated'")
                    elif mode == "relation_column":
                        conn.execute(f"UPDATE {cw._TABLE} SET relation_id='fabricated'")
                    else:
                        if mode == "payload":
                            saved["receipt_item_id"] = "fabricated"
                        else:
                            saved["item"]["id"] = "arp:another"
                        conn.execute(f"UPDATE {cw._TABLE} SET payload_json=?", (oc._json(saved),))
                    conn.commit()
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                self.requeue(store, focus)
                self.assertEqual(cw.converge(store)["retired"], 0)
                if ar._uses_sqlite(store):
                    with dbstore.connect(store) as conn:
                        conn.execute("UPDATE review_queue SET status='superseded' WHERE item_id=?", (focus.id,))
                        conn.commit()
                else:
                    ar._write_queue(store, [])
                result = ar.submit_judgments(store, [self.judgment(focus, self.fixture.proposal(focus))])
                self.assertEqual(result["unknown"], 1)
                self.assertEqual(result["accepted"], 0)

    def test_read_only_missing_proof_does_not_create_a_ledger(self):
        store, initial, original, focus = self.packets()
        with dbstore.connect(store) as conn:
            before = conn.total_changes
            self.assertFalse(cw._is_superseded(conn, store, focus.id))
            self.assertEqual(conn.total_changes, before)
            self.assertFalse(cw._has_ledger(conn))

    def test_old_unresolved_alias_replay_survives_authentic_refinement_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                self.fixture.base = self.base / str(version)
                store, initial, original, focus = self.packets(version, extra_context=10)
                self.submit_original(store, initial, original, focus, kind="unresolved")
                expansion = ar.load_queue(store)[0]
                result = ar.submit_judgments(store, [self.judgment(expansion, self.fixture.proposal(expansion))])
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["remaining"], 0)
                with dbstore.connect(store) as conn:
                    self.assertTrue(cw._is_superseded(conn, store, focus.id))
                    self.assertEqual(len(oc._read_relations(conn)), 2)
                    self.assertEqual(sum(record["kind"] == "unresolved" for record in oc._read_relations(conn)), 0)
                before = (store / "kb" / "sekaisync.db").read_bytes()
                replay = ar.submit_judgments(store, [self.judgment(focus, self.fixture.proposal(focus, "unresolved"))])
                self.assertEqual((replay["accepted"], replay["unknown"], replay["errors"]), (0, 0, []))
                self.assertEqual(before, (store / "kb" / "sekaisync.db").read_bytes())
                self.requeue(store, focus)
                self.assertEqual(cw.converge(store)["retired"], 1)
                self.assertEqual(ar.load_queue(store), [])

    def test_refined_alias_checks_actual_successor_receipt_and_authentic_retirement(self):
        for mode in ("receipt_missing", "receipt_wrong", "successor_missing", "retirement_identity", "retirement_column"):
            with self.subTest(mode=mode):
                self.fixture.base = self.base / mode
                store, initial, original, focus = self.packets(extra_context=10)
                self.submit_original(store, initial, original, focus, kind="unresolved")
                expansion = ar.load_queue(store)[0]
                ar.submit_judgments(store, [self.judgment(expansion, self.fixture.proposal(expansion))])
                if mode.startswith("receipt"):
                    receipts = ap._receipts(store)
                    if mode == "receipt_missing":
                        receipts.pop(expansion.id)
                    else:
                        receipts[expansion.id]["value"] = receipts[initial.id]["value"]
                    ar._write_json(ap._receipt_path(store), dict(items=receipts))
                else:
                    with dbstore.connect(store) as conn:
                        if mode == "successor_missing":
                            conn.execute("DELETE FROM scraper_relations WHERE review_item_id=?", (expansion.id,))
                        elif mode == "retirement_identity":
                            saved = json.loads(conn.execute("SELECT payload_json FROM scraper_relation_retirements").fetchone()[0])
                            saved["reason"] += " tampered"
                            conn.execute("UPDATE scraper_relation_retirements SET payload_json=?", (oc._json(saved),))
                        else:
                            conn.execute("UPDATE scraper_relation_retirements SET new_id='fabricated'")
                        conn.commit()
                with dbstore.connect(store) as conn:
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))

    def test_refined_history_cannot_create_a_new_supersession_without_saved_proof(self):
        store, initial, original, focus = self.packets(extra_context=10)
        self.submit_original(store, initial, original, focus, kind="unresolved")
        expansion = ar.load_queue(store)[0]
        ar.submit_judgments(store, [self.judgment(expansion, self.fixture.proposal(expansion))])
        with dbstore.connect(store) as conn:
            conn.execute(f"DELETE FROM {cw._TABLE}")
            conn.commit()
            remaining, retired = cw._converge_items(conn, store, [focus])
            self.assertEqual(remaining, [focus])
            self.assertEqual(retired, [])

    def test_replay_proof_uses_strict_json_types_not_python_bool_numeric_equality(self):
        for field, value in (("schema", True), ("semantic_resolved", 1)):
            with self.subTest(field=field):
                self.fixture.base = self.base / field
                store, initial, original, focus = self.packets()
                self.submit_original(store, initial, original, focus)
                with dbstore.connect(store) as conn:
                    saved = json.loads(conn.execute(f"SELECT payload_json FROM {cw._TABLE}").fetchone()[0])
                    saved[field] = value
                    conn.execute(f"UPDATE {cw._TABLE} SET payload_json=?", (oc._json(saved),))
                    conn.commit()
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))

    def test_later_identical_real_vote_keeps_the_original_superseding_receipt(self):
        store, initial, original, focus = self.packets()
        self.submit_original(store, initial, original, focus)
        with dbstore.connect(store) as conn:
            saved = conn.execute(f"SELECT payload_json FROM {cw._TABLE}").fetchone()[0]
            relation = oc._read_relations(conn, target_language="ko")[0]
        for index in range(100):
            new_item = ap._item(original.term, original.language, [f"candidate{index}"], "pending", original._context, "Same outcome")
            proof = copy.deepcopy(relation["grounding"])
            proof["candidates"] = new_item.candidates
            later = oc._relation(relation["source"], relation["target"], relation["sense"], relation["kind"],
                                 new_item.id, relation["rationale"], relation["agent"], proof)
            if later["id"] < relation["id"]:
                break
        else:
            self.fail("Could not construct deterministic ordering counterexample")
        ar.enqueue(store, [new_item])
        result = ar.submit_judgments(store, [self.judgment(new_item, self.fixture.proposal(focus))])
        self.assertEqual(result["errors"], [])
        with dbstore.connect(store) as conn:
            self.assertTrue(cw._is_superseded(conn, store, focus.id))
            self.assertEqual(conn.execute(f"SELECT payload_json FROM {cw._TABLE}").fetchone()[0], saved)
        self.requeue(store, focus)
        self.assertEqual(cw.converge(store)["retired"], 1)
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute(f"SELECT payload_json FROM {cw._TABLE}").fetchone()[0], saved)


if __name__ == "__main__":
    unittest.main()
