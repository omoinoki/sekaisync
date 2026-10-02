"""Independent receipt-backed work retirement and historical replay adversaries."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import agent_packets as ap, agent_review as ar, cohesion_work as cw, dbstore, occurrence_store as oc
from sekaisync import span_subjects as ss


class CohesionWorkReviewTests(unittest.TestCase):
    def setUp(self):
        from tests.test_agent_occurrence_cohesion import OccurrenceCohesionTests
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.fixture = OccurrenceCohesionTests()
        self.fixture.base = self.base

    def scenario(self, version=1, *, kind="lexical", label="default", extra_context=0, typed=False):
        self.fixture.base = self.base / label
        store, scope_id, scope = self.fixture.fixture(version, lines=1, extra_context=extra_context)
        if typed:
            row = scope["windows"][0]
            parts = [ap._body_term_segments(row["source"]["text"], text, row["source"]["start"])[0][0]
                     for text in ("Her", "voice")]
            subject = ss._segmented(row["source"], row["story_key"], parts)
            sense = oc._sense(subject["id"], "en", "singing_voice", "singing timbre")
            with dbstore.connect(store) as conn:
                initial = ap._cohesion_focus_item(conn, scope_id, scope, subject["canonical"], "ja",
                                                   subject["source"], sense, {}, subject)
                focus = ap._cohesion_focus_item(conn, scope_id, scope, subject["canonical"], "ko",
                                                 subject["source"], sense, {}, subject)
        else:
            initial = self.fixture.item(scope_id, scope, 0, "ja")
            focus = self.fixture.item(scope_id, scope, 0, "ko")
        context = copy.deepcopy(focus._context)
        context.pop("focus")
        original = ap._item(focus.term, focus.language, [], "pending", context, "Independent receipt fixture")
        ar.enqueue(store, [initial, original])
        first = ar.submit_judgments(store, [self.answer(initial)])
        self.assertEqual(first["errors"], [])
        self.assertIn(focus.id, {item.id for item in ar.load_queue(store)})
        answer = dict(id=original.id, decision="accept", relations=[self.fixture.proposal(focus, kind)])
        result = ar.submit_judgments(store, [answer])
        self.assertEqual(result["errors"], [])
        self.assertNotIn(focus.id, {item.id for item in ar.load_queue(store)})
        return store, initial, original, focus

    def answer(self, item, kind="lexical"):
        return dict(id=item.id, decision="accept", relations=[self.fixture.proposal(item, kind)])

    def requeue(self, store, focus):
        if ar._uses_sqlite(store):
            with dbstore.connect(store) as conn:
                conn.execute("UPDATE review_queue SET status='queued' WHERE item_id=?", (focus.id,))
                conn.commit()
        else:
            ar._write_queue(store, ar.load_queue(store) + [focus])

    def proof(self, conn, focus):
        return json.loads(conn.execute(f"SELECT payload_json FROM {cw._TABLE} WHERE item_id=?",
                                       (focus.id,)).fetchone()[0])

    def mutate_proof(self, conn, focus, field, value):
        saved = self.proof(conn, focus)
        saved[field] = value
        conn.execute(f"UPDATE {cw._TABLE} SET payload_json=? WHERE item_id=?", (oc._json(saved), focus.id))
        conn.commit()

    def snapshot(self, store):
        return {str(path.relative_to(store)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in store.rglob("*") if path.is_file()}

    def resolve(self, store):
        expansion = next(item for item in ar.load_queue(store) if "expansion" in item._context)
        result = ar.submit_judgments(store, [self.answer(expansion)])
        self.assertEqual(result["errors"], [])
        return expansion

    def insert_unreceipted_conflict(self, conn, relation, *, target=False):
        grounding = copy.deepcopy(relation["grounding"])
        grounding["candidates"] = ["independent-unreceipted-observation"]
        identity = "arp:" + ap._digest([grounding["term"], relation["target_language"],
                                       grounding["candidates"], grounding["context"]])[:32]
        anchor = relation["target"]
        kind = "paraphrase"
        if target:
            row = grounding["context"]["rows"][0]
            parts = ap._body_term_segments(row["target"]["text"], "song", row["target"]["start"])[0]
            anchor = oc._anchor(row["target"], row["story_key"], parts)
            kind = "lexical"
        conflict = oc._relation(relation["source"], anchor, relation["sense"], kind, identity,
                                "Independent contradictory raw observation", "synthetic-review", grounding)
        oc._store_relations(conn, [conflict])
        conn.commit()
        return conflict

    def test_boolean_schema_alias_cannot_authenticate_a_tampered_saved_proof(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, _, focus = self.scenario(version, label=f"schema-{version}")
                with dbstore.connect(store) as conn:
                    self.mutate_proof(conn, focus, "schema", True)
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                result = ar.submit_judgments(store, [self.answer(focus)])
                self.assertEqual((result["accepted"], result["unknown"], result["errors"]), (0, 1, []))

    def test_integer_semantic_flag_cannot_authenticate_a_tampered_saved_proof(self):
        for kind, replacement in (("lexical", 1), ("unresolved", 0)):
            with self.subTest(kind=kind):
                store, _, _, focus = self.scenario(kind=kind, label=f"flag-{kind}", extra_context=10)
                with dbstore.connect(store) as conn:
                    self.mutate_proof(conn, focus, "semantic_resolved", replacement)
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))

    def test_rehashed_numeric_view_flag_is_not_the_same_immutable_focus_row(self):
        store, _, _, focus = self.scenario()
        for name in ("source", "target"):
            with self.subTest(name=name):
                context = copy.deepcopy(focus._context)
                self.assertIs(context["rows"][0][name]["complete"], True)
                context["rows"][0][name]["complete"] = 1
                forged = ap._item(focus.term, focus.language, [], "pending", context, "Numeric flag forgery")
                self.assertNotEqual(forged.id, focus.id)
                with dbstore.connect(store) as conn:
                    remaining, retired = cw._converge_items(conn, store, [forged])
                    self.assertEqual(remaining, [forged])
                    self.assertEqual(retired, [])
                    self.assertFalse(cw._is_superseded(conn, store, forged.id))

    def test_resolving_real_expansion_preserves_duplicate_focus_replay_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, original, focus = self.scenario(version, kind="unresolved",
                                                           label=f"resolve-{version}", extra_context=10)
                expansion = next(item for item in ar.load_queue(store) if "expansion" in item._context)
                with dbstore.connect(store) as conn:
                    saved = self.proof(conn, focus)
                    self.assertEqual(saved["receipt_item_id"], original.id)
                    self.assertFalse(saved["semantic_resolved"])
                result = ar.submit_judgments(store, [self.answer(expansion)])
                self.assertEqual(result["errors"], [])
                with dbstore.connect(store) as conn:
                    self.assertEqual(sum(row["kind"] == "unresolved" for row in oc._read_relations(conn)), 0)
                    self.assertEqual(len(oc._read_relations(conn)), 2)
                    self.assertEqual(len(oc._read_relations(conn, current_only=False)), 3)
                    self.assertEqual(self.proof(conn, focus), saved)
                    self.assertTrue(cw._is_superseded(conn, store, focus.id))
                    self.assertNotIn(focus.id, ap._receipts(store, conn=conn))
                before = self.snapshot(store)
                replay = ar.submit_judgments(store, [self.answer(focus, "unresolved")])
                self.assertEqual((replay["accepted"], replay["unknown"], replay["errors"], replay["followup_packets"]),
                                 (0, 0, [], 0))
                self.assertEqual(self.snapshot(store), before)

    def test_unreceipted_current_kind_or_target_conflict_preserves_ordinary_focus(self):
        for target in (False, True):
            with self.subTest(target=target):
                store, _, _, focus = self.scenario(label=f"ordinary-conflict-{target}")
                with dbstore.connect(store) as conn:
                    relation = oc._read_relations(conn, target_language="ko")[0]
                    conflict = self.insert_unreceipted_conflict(conn, relation, target=target)
                    self.assertNotIn(conflict["review_item_id"], ap._receipts(store, conn=conn))
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                    remaining, retired = cw._converge_items(conn, store, [focus])
                    self.assertEqual((remaining, retired), ([focus], []))

    def test_unreceipted_current_expansion_conflict_cannot_hide_behind_historical_alias(self):
        for target in (False, True):
            with self.subTest(target=target):
                store, _, _, focus = self.scenario(kind="unresolved", extra_context=10,
                                                    label=f"expansion-conflict-{target}")
                self.resolve(store)
                with dbstore.connect(store) as conn:
                    successor = oc._read_relations(conn, target_language="ko")[0]
                    conflict = self.insert_unreceipted_conflict(conn, successor, target=target)
                    self.assertNotIn(conflict["review_item_id"], ap._receipts(store, conn=conn))
                    self.assertEqual(len(oc._read_relations(conn, target_language="ko")), 2)
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                    remaining, retired = cw._converge_items(conn, store, [focus])
                    self.assertEqual((remaining, retired), ([focus], []))

    def test_only_exact_completed_receipts_authorize_new_work_retirement(self):
        store, _, original, focus = self.scenario()
        with dbstore.connect(store) as conn:
            relations_before = conn.execute("SELECT id,payload_json FROM scraper_relations ORDER BY id").fetchall()
            genuine = ap._receipts(store, conn=conn)
            conn.execute(f"DELETE FROM {cw._TABLE}")
            for mode in ("missing", "reject", "wrong_relation", "wrong_scope", "object", "null", "invalid"):
                with self.subTest(mode=mode):
                    receipts = copy.deepcopy(genuine)
                    if mode == "missing":
                        receipts.pop(original.id)
                    elif mode == "reject":
                        receipts[original.id]["decision"] = "reject"
                    elif mode == "wrong_scope":
                        receipts[original.id]["scope_id"] = "different-scope"
                    else:
                        value = {"wrong_relation": '["rel:unrelated"]', "object": '{}', "null": 'null',
                                 "invalid": 'not JSON'}[mode]
                        receipts[original.id]["value"] = value
                    remaining, retired = cw._converge_items(conn, store, [focus], receipts)
                    self.assertEqual((remaining, retired), ([focus], []))
                    self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {cw._TABLE}").fetchone()[0], 0)
            remaining, retired = cw._converge_items(conn, store, [focus], genuine)
            self.assertEqual((remaining, retired), ([], [focus.id]))
            saved = self.proof(conn, focus)
            self.assertEqual(saved["receipt_item_id"], original.id)
            self.assertIn(saved["relation_id"], json.loads(genuine[original.id]["value"]))
            self.assertEqual(relations_before, conn.execute("SELECT id,payload_json FROM scraper_relations ORDER BY id").fetchall())
            self.assertEqual(ap._receipts(store, conn=conn), genuine)
            self.assertNotIn(focus.id, genuine)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_typed_subject_and_same_display_legacy_lexeme_never_authorize_each_other(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, _, focus = self.scenario(version, typed=True, label=f"typed-{version}")
                with dbstore.connect(store) as conn:
                    self.assertTrue(cw._is_superseded(conn, store, focus.id))
                    self.assertEqual(len(oc._read_relations(conn)), 2)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                    variants = []
                    for mode in ("remove_subject", "change_subject", "same_display_legacy"):
                        context = copy.deepcopy(focus._context)
                        if mode == "change_subject":
                            context["subject"]["window"]["end"] -= 1
                        else:
                            context.pop("subject")
                            if mode == "same_display_legacy":
                                old = context["focus"]["sense"]
                                context["focus"]["sense"] = oc._sense(oc._identity("lex:", ["en", focus.term]),
                                                                           "en", old["key"], old["gloss"])
                        variants.append(ap._item(focus.term, focus.language, [], "pending", context, mode))
                    remaining, retired = cw._converge_items(conn, store, variants)
                    self.assertEqual((remaining, retired), (variants, []))
                    self.assertTrue(cw._is_superseded(conn, store, focus.id))

    def test_null_or_empty_debt_markers_still_prevent_retirement(self):
        store, _, _, focus = self.scenario(kind="unresolved", extra_context=10)
        expansion = next(item for item in ar.load_queue(store) if "expansion" in item._context)
        items = [expansion]
        for key in ("expansion", "scan", "fallback", "gap"):
            for value in (None, {}, False):
                context = dict(focus._context, **{key: value})
                items.append(ap._item(focus.term, focus.language, [], "pending", context, "Unfinished debt"))
        with dbstore.connect(store) as conn:
            before = conn.total_changes
            remaining, retired = cw._converge_items(conn, store, items)
            self.assertEqual((remaining, retired), (items, []))
            self.assertEqual(conn.total_changes, before)
            self.assertEqual(sum(row["kind"] == "unresolved" for row in oc._read_relations(conn)), 1)

    def test_export_recovers_stale_pending_focus_after_refinement_without_new_votes(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, _, focus = self.scenario(version, kind="unresolved", extra_context=10,
                                                    label=f"export-{version}")
                self.resolve(store)
                with dbstore.connect(store) as conn:
                    before_relations = conn.execute("SELECT id,payload_json FROM scraper_relations ORDER BY id").fetchall()
                    before_receipts = ap._receipts(store, conn=conn)
                self.requeue(store, focus)
                ar.export_for_agent(store, self.base / f"export-{version}.txt")
                self.assertEqual(ar.load_queue(store), [])
                before = self.snapshot(store)
                ar.export_for_agent(store, self.base / f"export-{version}-again.txt")
                self.assertEqual(self.snapshot(store), before)
                with dbstore.connect(store) as conn:
                    self.assertEqual(conn.execute("SELECT id,payload_json FROM scraper_relations ORDER BY id").fetchall(), before_relations)
                    self.assertEqual(ap._receipts(store, conn=conn), before_receipts)
                    self.assertNotIn(focus.id, before_receipts)

    def test_new_v1_proof_survives_interruption_before_json_drain_and_export_recovers(self):
        store, _, _, focus = self.scenario()
        with dbstore.connect(store) as conn:
            conn.execute(f"DELETE FROM {cw._TABLE}")
            conn.commit()
            before_receipts = ap._receipts(store, conn=conn)
            before_relations = conn.execute("SELECT id,payload_json FROM scraper_relations ORDER BY id").fetchall()
        self.requeue(store, focus)
        with patch.object(ar, "_write_queue", side_effect=OSError("synthetic interruption before JSON drain")):
            with self.assertRaisesRegex(OSError, "synthetic interruption"):
                cw.converge(store)
        self.assertEqual([item.id for item in ar.load_queue(store)], [focus.id])
        with dbstore.connect(store) as conn:
            self.assertTrue(cw._is_superseded(conn, store, focus.id))
        ar.export_for_agent(store, self.base / "crash-recovered.txt")
        self.assertEqual(ar.load_queue(store), [])
        self.assertEqual(cw.converge(store), dict(retired=0))
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute(f"SELECT COUNT(*) FROM {cw._TABLE}").fetchone()[0], 1)
            self.assertEqual(ap._receipts(store, conn=conn), before_receipts)
            self.assertEqual(conn.execute("SELECT id,payload_json FROM scraper_relations ORDER BY id").fetchall(), before_relations)

    def test_exact_scope_sense_target_language_and_focus_rows_cannot_be_substituted(self):
        store, _, _, focus = self.scenario()
        variants = []
        for mode in ("scope", "sense", "language", "row_id", "row_metadata", "target_bounds", "source_language"):
            context = copy.deepcopy(focus._context)
            language = focus.language
            if mode == "scope":
                context["scope_id"] = "another-immutable-scope"
            elif mode == "sense":
                old = context["focus"]["sense"]
                context["focus"]["sense"] = oc._sense(old["term_id"], "en", "other-sense", old["gloss"])
            elif mode == "language":
                language = "ja"
            elif mode == "row_id":
                context["rows"][0]["id"] = "span:foreign"
            elif mode == "row_metadata":
                context["rows"][0]["unproven_alignment"] = True
            elif mode == "target_bounds":
                target = context["rows"][0]["target"]
                target["end"] -= 1
                target["text"] = target["text"][:-1]
            else:
                context["source_language"] = "ja"
            variants.append(ap._item(focus.term, language, [], "pending", context, mode))
        with dbstore.connect(store) as conn:
            changes = conn.total_changes
            remaining, retired = cw._converge_items(conn, store, variants)
            self.assertEqual((remaining, retired), (variants, []))
            self.assertEqual(conn.total_changes, changes)

    def test_scope_body_mutation_or_removal_invalidates_saved_proof(self):
        for mode in ("changed", "missing"):
            with self.subTest(mode=mode):
                store, _, _, focus = self.scenario(label=f"scope-{mode}")
                path = ap._scope_path(store, focus._context["scope_id"])
                if mode == "changed":
                    scope = json.loads(path.read_text(encoding="utf-8"))
                    scope["target_languages"] = ["ja"]
                    ar._write_json(path, scope)
                else:
                    path.unlink()
                with dbstore.connect(store) as conn:
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                    remaining, retired = cw._converge_items(conn, store, [focus])
                    self.assertEqual((remaining, retired), ([focus], []))

    def test_multistage_authentic_expansion_replay_requires_every_intermediate_receipt(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, _, focus = self.scenario(version, kind="unresolved", extra_context=40,
                                                    label=f"multistage-{version}")
                intermediate = next(item for item in ar.load_queue(store) if "expansion" in item._context)
                result = ar.submit_judgments(store, [self.answer(intermediate, "unresolved")])
                self.assertEqual(result["errors"], [])
                final = self.resolve(store)
                self.assertGreater(final._context["expansion"]["stage"], intermediate._context["expansion"]["stage"])
                with dbstore.connect(store) as conn:
                    self.assertEqual(len(oc._read_relations(conn, current_only=False)), 4)
                    self.assertEqual(len(oc._read_relations(conn)), 2)
                    self.assertTrue(cw._is_superseded(conn, store, focus.id))
                before = self.snapshot(store)
                result = ar.submit_judgments(store, [self.answer(focus, "unresolved")])
                self.assertEqual((result["accepted"], result["unknown"], result["errors"]), (0, 0, []))
                self.assertEqual(self.snapshot(store), before)
                if ar._uses_sqlite(store):
                    with dbstore.connect(store) as conn:
                        conn.execute("DELETE FROM review_decisions WHERE item_id=?", (intermediate.id,))
                        conn.commit()
                else:
                    receipts = ap._receipts(store)
                    receipts.pop(intermediate.id)
                    ar._write_json(ap._receipt_path(store), dict(items=receipts))
                with dbstore.connect(store) as conn:
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                    remaining, retired = cw._converge_items(conn, store, [focus])
                    self.assertEqual((remaining, retired), ([focus], []))

    def test_retirement_columns_identity_or_successor_receipt_cannot_be_forged(self):
        for mode in ("column", "identity", "cycle", "foreign", "missing_successor", "receipt"):
            with self.subTest(mode=mode):
                store, initial, _, focus = self.scenario(kind="unresolved", extra_context=10,
                                                        label=f"retirement-{mode}")
                successor_item = self.resolve(store)
                with dbstore.connect(store) as conn:
                    saved = self.proof(conn, focus)
                    row = conn.execute("SELECT new_id,payload_json FROM scraper_relation_retirements WHERE old_id=?",
                                       (saved["relation_id"],)).fetchone()
                    if mode == "column":
                        conn.execute("UPDATE scraper_relation_retirements SET new_id='rel:forged' WHERE old_id=?",
                                     (saved["relation_id"],))
                    elif mode in {"identity", "cycle", "foreign"}:
                        retirement = json.loads(row[1])
                        if mode == "identity":
                            retirement["reason"] += " modified without matching hash"
                        else:
                            retirement["new_id"] = (saved["relation_id"] if mode == "cycle" else
                                                     json.loads(ap._receipts(store, conn=conn)[initial.id]["value"])[0])
                            retirement["id"] = oc._identity("ret:", {key: value for key, value in retirement.items()
                                                                         if key != "id"})
                            conn.execute("UPDATE scraper_relation_retirements SET new_id=? WHERE old_id=?",
                                         (retirement["new_id"], saved["relation_id"]))
                        conn.execute("UPDATE scraper_relation_retirements SET payload_json=? WHERE old_id=?",
                                     (oc._json(retirement), saved["relation_id"]))
                    elif mode == "missing_successor":
                        conn.execute("DELETE FROM scraper_relations WHERE id=?", (row[0],))
                    else:
                        receipts = ap._receipts(store, conn=conn)
                        receipts[successor_item.id]["value"] = '["rel:forged"]'
                        ar._write_json(ap._receipt_path(store), dict(items=receipts))
                    conn.commit()
                    self.assertFalse(cw._is_superseded(conn, store, focus.id))
                    remaining, retired = cw._converge_items(conn, store, [focus])
                    self.assertEqual((remaining, retired), ([focus], []))

    def test_later_consistent_lower_sorted_real_vote_preserves_original_alias_proof(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, original, focus = self.scenario(version, label=f"later-vote-{version}")
                with dbstore.connect(store) as conn:
                    saved = self.proof(conn, focus)
                    old = oc._read_relations(conn, target_language="ko")[0]
                    proposal = self.fixture.proposal(focus)
                    for index in range(1000):
                        new_item = ap._item(original.term, original.language, [f"equivalent-{index}"], "pending",
                                            copy.deepcopy(original._context), "Independent equivalent real vote")
                        grounding = copy.deepcopy(old["grounding"])
                        grounding["candidates"] = new_item.candidates
                        expected = oc._relation(old["source"], old["target"], old["sense"], old["kind"],
                                                new_item.id, proposal["rationale"], "host-agent", grounding)
                        if expected["id"] < old["id"]:
                            break
                    else:
                        self.fail("Could not construct deterministic lower-sorted equivalent vote")
                ar.enqueue(store, [new_item])
                result = ar.submit_judgments(store, [dict(id=new_item.id, decision="accept", relations=[proposal])])
                self.assertEqual(result["errors"], [])
                with dbstore.connect(store) as conn:
                    current = oc._read_relations(conn, target_language="ko")
                    self.assertEqual(len(current), 2)
                    self.assertEqual(current[0]["id"], expected["id"])
                    self.assertIn(expected["id"], json.loads(ap._receipts(store, conn=conn)[new_item.id]["value"]))
                    self.assertTrue(cw._is_superseded(conn, store, focus.id))
                    self.assertEqual(self.proof(conn, focus), saved)
                before = self.snapshot(store)
                replay = ar.submit_judgments(store, [self.answer(focus)])
                self.assertEqual((replay["accepted"], replay["unknown"], replay["errors"]), (0, 0, []))
                self.assertEqual(self.snapshot(store), before)
                self.requeue(store, focus)
                ar.export_for_agent(store, self.base / f"later-vote-export-{version}.txt")
                self.assertEqual(ar.load_queue(store), [])
                with dbstore.connect(store) as conn:
                    self.assertEqual(self.proof(conn, focus), saved)


if __name__ == "__main__":
    unittest.main()
