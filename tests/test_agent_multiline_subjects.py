"""Exact raw multiline typed subjects survive ordinary discovery and review."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest


from sekaisync import agent_packets as ap, agent_review as ar, dbstore
from sekaisync import occurrence_store as ledger, span_subjects, termindex
from sekaisync.core import SekaiSyncCore


ORIGINAL_SURFACE = ap._valid_surface
TARGET = "\u589e\u5f3a\u4fe1\u5fc3"
SOURCE_ID = "web:fixture:en:event_story:999:1"


class AgentMultilineSubjectTests(unittest.TestCase):
    STORE_VERSION = 1

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multiline-subject-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sequence = 0

    def fixture(self, source_text):
        self.sequence += 1
        store = self.base / str(self.sequence) / "store"
        self.assertFalse(store.exists())
        dbstore.initialize(store)
        for version in range(2, self.STORE_VERSION + 1):
            dbstore.migrate_store(store, target_version=version, dry_run=False,
                                 backup_path=store.parent / f"pre-migration-{version}.db")
        pages = [dict(id=f"web:fixture:{language}:event_story:999:1", source="fixture",
                      kind="event_story", language=language, trust="B", text=text,
                      canonical_key=f"event_story:{language}:999:1")
                 for language, text in (("en", source_text),
                                        ("zh_hans", "B: " + TARGET + " together."))]
        dbstore.upsert_web_pages(store, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        items, _metadata = ap._prepare_scrub_review(
            store, groups, sorted(groups), [], {}, "en", ["zh_hans"])
        discoveries = [item for item in items if item._context.get("task") == "discovery"]
        self.assertEqual(len(discoveries), 1)
        item = discoveries[0]
        ar.enqueue(store, [item])
        exported = ar.export_for_agent(store, store.parent / "discovery.txt", limit=0)
        packet = exported.read_text(encoding="utf-8")
        visible = [json.loads(line[len("context: "):]) for line in packet.splitlines()
                   if line.startswith("context: ")]
        row = item._context["rows"][0]
        self.assertEqual(visible[0]["source"], {key: value for key, value in row["source"].items() if key != "sha256"})
        self.assertEqual(visible[0]["id"], row["id"])
        self.assertIn("discovery_contract:", packet)
        self.assertIn("200", next(line for line in packet.splitlines()
                                  if line.startswith("discovery_contract:")))
        return store, item, row

    @staticmethod
    def segments(view, fragments):
        result, cursor = [], 0
        for exact in fragments:
            start = view["text"].index(exact, cursor)
            end = start + len(exact)
            result.append(dict(start=view["start"] + start, end=view["start"] + end, exact=exact))
            cursor = end
        return result

    def literal(self, row, canonical, fragments=None):
        return dict(kind="literal", evidence_id=row["id"], canonical=canonical,
                    segments=self.segments(row["source"], fragments or [canonical]))

    @staticmethod
    def submit(store, item, **answer):
        serialized = json.dumps(dict(id=item.id, decision="accept", **answer))
        return ar.submit_judgments(store, ar.parse_judgments_text(serialized))

    @staticmethod
    def counts(store):
        with dbstore.connect(store) as conn:
            return dict(terms=conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0],
                        relations=len(ledger._read_relations(conn)))

    def reject(self, store, item, **answer):
        before = self.counts(store)
        result = self.submit(store, item, **answer)
        self.assertTrue(result["errors"], result)
        self.assertEqual(result["accepted"], 0)
        self.assertEqual(self.counts(store), before)
        self.assertIn(item.id, {queued.id for queued in ar.load_queue(store)})
        self.assertNotIn(item.id, ap._receipts(store))
        return result

    def occurrence(self, store, discovery, entry):
        result = self.submit(store, discovery, subjects=[entry], rationale="Synthetic mechanical fixture")
        self.assertEqual(result["errors"], [])
        children = [item for item in ar.load_queue(store) if item._context.get("task") == "occurrence"]
        self.assertEqual(len(children), 1)
        child = children[0]
        subject = child._context["subject"]
        self.assertEqual(child.term, subject["canonical"])
        self.assertEqual(child.id, "arp:" + ap._digest([
            child.term, child.language, sorted(set(child.candidates)), child._context])[:32])
        constructed = ap._item(child.term, child.language, child.candidates, child.kind,
                               child._context, "Raw transport roundtrip")
        self.assertEqual(constructed.term, child.term)
        self.assertEqual(constructed.id, child.id)
        reloaded = ar.ReviewItem.from_dict(child.to_dict())
        self.assertEqual(reloaded.term, child.term)
        self.assertEqual(reloaded._context, child._context)
        self.assertEqual(reloaded.id, child.id)
        exported = ar.export_for_agent(store, store.parent / "occurrence.txt", limit=0)
        self.assertIn("answer_contract: decision=accept; relations=", exported.read_text(encoding="utf-8"))
        row = child._context["rows"][0]
        relation = dict(evidence_id=row["id"], source_segments=entry["segments"],
                        target_segments=self.segments(row["target"], [TARGET]),
                        sense_key="confidence-building", sense_gloss="Synthetic contextual interpretation",
                        kind="lexical", rationale="Mechanical fixture, not semantic validation")
        result = self.submit(store, child, relations=[relation])
        self.assertEqual(result["errors"], [], json.dumps(dict(
            child_term=child.term, raw_canonical=subject["canonical"],
            actual_id=child.id, replay_id="arp:" + ap._digest([
                child.term, child.language, sorted(set(child.candidates)), child._context])[:32],
            public_submit=result), ensure_ascii=True))
        with dbstore.connect(store) as conn:
            relations = ledger._read_relations(conn)
            self.assertEqual(len(relations), 1)
            self.assertEqual(relations[0]["source"]["segments"], entry["segments"])
            self.assertFalse(relations[0]["semantic_guarantee"])
            self.assertEqual(relations[0]["grounding"]["context"]["subject"], subject)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
        return subject

    def consumers(self, store, subject, expected_scalar):
        core = SekaiSyncCore(store)
        canonical = subject["canonical"]
        before = self.counts(store)
        with core.request_view():
            penetration = core.term_penetrate(canonical, story_key="event:999:1", languages=["en", "zh_hans"])
            generic = core.query(canonical, include_web=False)
        self.assertIsNotNone(penetration)
        self.assertEqual(len(generic["terms"]), 1)
        positions = {position["language"]: position for position in generic["terms"][0]["positions"]}
        for entry in (penetration["per_language"]["en"], positions["en"]):
            self.assertEqual(entry["term"], expected_scalar)
            if expected_scalar:
                self.assertFalse(entry.get("missing"))
            else:
                self.assertTrue(entry["missing"])
                label = "; raw selected fragments (Unicode code points, end-exclusive): "
                self.assertEqual(json.loads(entry["note"].split(label, 1)[1]), subject["source"]["segments"])
        for entry in (penetration["per_language"]["zh_hans"], positions["zh_hans"]):
            self.assertEqual(entry["term"], TARGET)
        expected_names = {"en": canonical} if subject["kind"] == "literal" else {}
        self.assertEqual(penetration["term"]["names"], expected_names)
        self.assertEqual(generic["terms"][0]["names"], expected_names)
        self.assertEqual(self.counts(store), before)

    def test_exact_raw_typed_gate_preserves_source_geometry(self):
        for newline in ("\n", "\r\n"):
            for fragments in (["build" + newline + "confidence"], ["build", "confidence"]):
                with self.subTest(newline=repr(newline), fragments=fragments):
                    canonical = "build" + newline + "confidence"
                    store, _item, row = self.fixture("A: We should " + canonical + " together.")
                    entry = self.literal(row, canonical, fragments)
                    subject = span_subjects._literal(row["source"], row["story_key"], canonical, entry["segments"])
                    with dbstore.connect(store) as conn:
                        self.assertEqual(ap._validate_subject_in_rows(conn, subject, [row]), subject)
                    self.assertEqual(subject["canonical"], canonical)
                    self.assertEqual(subject["source"]["segments"], entry["segments"])

    def test_exact_lf_crlf_complete_normal_pipeline_and_both_consumers(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                canonical = "build" + newline + "confidence"
                store, item, row = self.fixture("A: We should " + canonical + " together.")
                entry = self.literal(row, canonical)
                subject = span_subjects._literal(row["source"], row["story_key"], canonical, entry["segments"])
                with dbstore.connect(store) as conn:
                    self.assertEqual(span_subjects._validate(conn, subject), subject)
                    self.assertEqual(ap._validate_subject_in_rows(conn, subject, [row]), subject)
                self.assertFalse(ap._valid_surface(canonical))
                legacy = self.reject(store, item, terms=[canonical])
                self.assertIn("discovered terms must be complete 1-80", json.dumps(legacy["errors"]))
                accepted = self.occurrence(store, item, entry)
                self.assertEqual(accepted, subject)
                self.assertEqual(accepted["canonical"], canonical)
                self.consumers(store, accepted, canonical)
                core = SekaiSyncCore(store)
                self.assertIsNone(core.term_penetrate("build confidence", story_key="event:999:1"))
                self.assertEqual(core.query("build confidence", include_web=False)["terms"], [])

    def test_02_exact_multiline_literal_with_omitted_whitespace_retains_geometry(self):
        for newline, fragments in (("\n", ["build", "confidence"]),
                                   ("\r\n", ["build", "confidence"]),
                                   ("\r\n", ["build\r", "confidence"])):
            with self.subTest(newline=repr(newline), fragments=fragments):
                canonical = "build" + newline + "confidence"
                store, item, row = self.fixture("A: We should " + canonical + " together.")
                entry = self.literal(row, canonical, fragments)
                subject = self.occurrence(store, item, entry)
                self.assertEqual(subject["source"]["segments"], entry["segments"])
                self.assertEqual(subject["canonical_parts"], fragments)
                self.assertEqual(subject["gap_text"], ["\n" if fragments[0].endswith("\r") else newline])
                self.assertEqual(subject["canonical"], canonical)
                self.consumers(store, subject, canonical)

    def test_03_segmented_control_keeps_raw_gap_and_display_only_canonical(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                store, item, row = self.fixture("A: We should build" + newline + "confidence together.")
                parts = self.segments(row["source"], ["build", "confidence"])
                subject = self.occurrence(store, item, dict(kind="segmented", evidence_id=row["id"], segments=parts))
                self.assertEqual(subject["source"]["segments"], parts)
                self.assertEqual(subject["gap_text"], [newline])
                self.assertEqual(subject["canonical"], "build confidence")
                self.consumers(store, subject, "")

    def test_04_invented_lf_crlf_over_raw_spaces_are_rejected_after_replay(self):
        for canonical in ("build\nconfidence", "build\r\nconfidence"):
            with self.subTest(canonical=repr(canonical)):
                store, item, row = self.fixture("A: We should build confidence together.")
                entry = self.literal(row, canonical, ["build", "confidence"])
                subject = span_subjects._literal(row["source"], row["story_key"], canonical, entry["segments"])
                with dbstore.connect(store) as conn:
                    self.assertEqual(span_subjects._validate(conn, subject), subject)
                result = self.reject(store, item, subjects=[entry])
                self.assertIn("complete raw surface within 1800 code points", json.dumps(result["errors"]))

    def test_05_edge_lf_cr_alias_cannot_bypass_exact_raw_branch(self):
        for canonical in ("\nbuild confidence", "build confidence\n", "\nbuild confidence\n",
                          "\r\nbuild confidence", "build confidence\r", "\rbuild confidence"):
            with self.subTest(canonical=repr(canonical)):
                self.assertTrue(ORIGINAL_SURFACE(canonical))
                store, item, row = self.fixture("A: We should  build confidence  together.")
                entry = self.literal(row, canonical, ["build confidence"])
                subject = span_subjects._literal(row["source"], row["story_key"], canonical, entry["segments"])
                with dbstore.connect(store) as conn:
                    self.assertEqual(span_subjects._validate(conn, subject), subject)
                self.reject(store, item, subjects=[entry])

    def test_06_bare_cr_exact_and_other_c0_mixed_with_lf_rejected(self):
        values = ["build\rconfidence", "build\r\r\nconfidence"]
        values += ["build\nconfidence" + chr(code) for code in range(32) if code not in (10, 13)]
        for canonical in values:
            with self.subTest(canonical=repr(canonical)):
                # The predicate checks the complete string even if a stripped
                # control is outside the selector's mandatory lexical positions.
                parts = [dict(start=0, end=len(canonical), exact=canonical)]
                self.assertFalse(ap._valid_literal_surface(dict(canonical=canonical, source=dict(segments=parts), gaps=[])))
        for code in (0, 9, 11, 12, 27, 31):
            canonical = "build\nconfidence" + chr(code)
            store, item, row = self.fixture("A: We should " + canonical + " together.")
            self.reject(store, item, subjects=[self.literal(row, canonical)])
        for canonical in ("\tbuild\nconfidence", "build\nconfidence\t", "build\rconfidence"):
            store, item, row = self.fixture("A: We should " + canonical.strip() + " together.")
            self.reject(store, item, subjects=[self.literal(row, canonical, [canonical.strip()])])

    def test_07_multiline_80_81_codepoint_boundary_uses_typed_raw_limit(self):
        for newline in ("\n", "\r\n"):
            for length in (80, 81):
                with self.subTest(newline=repr(newline), length=length):
                    canonical = "a" * 39 + newline + "b" * (length - 39 - len(newline))
                    self.assertEqual(len(canonical.strip()), length)
                    store, item, row = self.fixture("A: " + canonical + ".")
                    entry = self.literal(row, canonical)
                    subject = span_subjects._literal(row["source"], row["story_key"], canonical, entry["segments"])
                    with dbstore.connect(store) as conn:
                        self.assertEqual(span_subjects._validate(conn, subject), subject)
                    self.assertFalse(ORIGINAL_SURFACE(canonical))
                    self.assertEqual(self.submit(store, item, subjects=[entry])["errors"], [])
                    self.assertIn(item.id, ap._receipts(store))
                    self.assertEqual(self.counts(store)["terms"], 0)

    def test_08_plain_legacy_accepted_strings_and_controls_unchanged(self):
        for value in ("build confidence", " build confidence ", "\tbuild confidence\t",
                      "\x0bbuild confidence\x0b", "build\x00confidence", "a" * 80):
            with self.subTest(value=repr(value)):
                self.assertNotIn("\n", value)
                self.assertNotIn("\r", value)
                self.assertEqual(ap._valid_literal_surface(dict(canonical=value)), ORIGINAL_SURFACE(value))
        self.assertFalse(ORIGINAL_SURFACE("a" * 81))
        store, item, row = self.fixture("A: " + "a" * 81 + ".")
        self.reject(store, item, terms=["a" * 81])
        self.assertEqual(self.submit(store, item, subjects=[self.literal(row, "a" * 81)])["errors"], [])
        self.assertEqual(self.counts(store)["terms"], 0)
        store, item, row = self.fixture("A: We should build confidence together.")
        entry = self.literal(row, "\tbuild confidence\t", ["build confidence"])
        self.assertEqual(self.submit(store, item, subjects=[entry])["errors"], [])
        self.assertIs(ap._valid_surface, ORIGINAL_SURFACE)

    def test_09_same_turn_fullspeaker_clipping_and_immutable_window_preserved(self):
        raw = "A: Introductory context.\nNarrator: build\nconfidence together."
        store, _item, row = self.fixture(raw)
        full = dict(source="fixture", page_id=SOURCE_ID, language="en", start=0, end=len(raw),
                    text=raw, sha256=hashlib.sha256(raw.encode()).hexdigest())
        start = raw.index("rator")
        clipped = dict(full, start=start, end=start + 5, text="rator")
        parts = [dict(start=start, end=start + 2, exact="ra"), dict(start=start + 2, end=start + 5, exact="tor")]
        forged = span_subjects._segmented(clipped, row["story_key"], parts)
        with dbstore.connect(store) as conn:
            with self.assertRaisesRegex(ValueError, "utterance body"):
                ap._validate_subject_in_rows(conn, forged, [dict(row, source=clipped)])
            crossing = self.segments(full, ["Introductory context.", "build\nconfidence"])
            with self.assertRaisesRegex(ValueError, "utterance body"):
                span_subjects._segmented(full, row["story_key"], crossing)
            subject = span_subjects._literal(full, row["story_key"], "build\nconfidence",
                                            self.segments(full, ["build\nconfidence"]))
            excluded = dict(full, start=0, end=2, text=raw[:2])
            with self.assertRaisesRegex(ValueError, "immutable exported source windows"):
                ap._validate_subject_in_rows(conn, subject, [dict(row, source=excluded)])
        # One utterance's source window cannot select actual subsequent turns.
        store, item, row = self.fixture("A: build\nB: confidence together.")
        parts = [dict(start=3, end=8, exact="build"), dict(start=12, end=22, exact="confidence")]
        self.reject(store, item, subjects=[dict(kind="literal", evidence_id=row["id"],
                                             canonical="build\nconfidence", segments=parts)])

    def test_10_stale_source_bytes_reject_submit_and_queries_without_deleting_history(self):
        canonical = "build\nconfidence"
        store, item, row = self.fixture("A: We should " + canonical + " together.")
        entry = self.literal(row, canonical)
        subject = self.occurrence(store, item, entry)
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=text || ' stale edit' WHERE source='fixture' AND id=?", (SOURCE_ID,))
            conn.commit()
            with self.assertRaises(ValueError):
                ap._validate_subject_in_rows(conn, subject, [row])
            self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)
            self.assertEqual(len(ledger._read_relations(conn)), 0)
        core = SekaiSyncCore(store)
        self.assertIsNone(core.term_penetrate(canonical, story_key="event:999:1"))
        self.assertEqual(core.query(canonical, include_web=False)["terms"], [])
        store, item, row = self.fixture("A: We should " + canonical + " together.")
        entry = self.literal(row, canonical)
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=REPLACE(text, char(10), ' ') WHERE source='fixture' AND id=?", (SOURCE_ID,))
            conn.commit()
        self.reject(store, item, subjects=[entry])

    def test_10a_stale_gate_and_public_submit_negative_are_reachable_without_literal_relation(self):
        canonical = "build\nconfidence"
        store, item, row = self.fixture("A: We should " + canonical + " together.")
        entry = self.literal(row, canonical)
        subject = span_subjects._literal(row["source"], row["story_key"], canonical, entry["segments"])
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=REPLACE(text, char(10), ' ') WHERE source='fixture' AND id=?", (SOURCE_ID,))
            conn.commit()
            with self.assertRaises(ValueError):
                ap._validate_subject_in_rows(conn, subject, [row])
        self.reject(store, item, subjects=[entry])
        # Existing segmented multiline control reaches both consumers and their
        # stale-byte replay; it is not evidence that literal continuation works.
        store, item, row = self.fixture("A: We should " + canonical + " together.")
        parts = self.segments(row["source"], ["build", "confidence"])
        subject = self.occurrence(store, item, dict(kind="segmented", evidence_id=row["id"], segments=parts))
        self.consumers(store, subject, "")
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=text || ' stale edit' WHERE source='fixture' AND id=?", (SOURCE_ID,))
            conn.commit()
            self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)
            self.assertEqual(ledger._read_relations(conn), [])
        core = SekaiSyncCore(store)
        self.assertIsNone(core.term_penetrate(subject["canonical"], story_key="event:999:1"))
        self.assertEqual(core.query(subject["canonical"], include_web=False)["terms"], [])

    def test_11_201_atomic_rejection_200_success_continuation_and_debt_unchanged(self):
        canonicals = [f"x{index:03}\ny" for index in range(201)]
        store, item, row = self.fixture("A: " + " ".join(canonicals))
        entries = [self.literal(row, canonical) for canonical in canonicals]
        self.assertEqual(ap._DISCOVERY_TERMS, 200)
        failed = self.reject(store, item, subjects=entries)
        self.assertIn("at most 200 exact typed source observations", json.dumps(failed["errors"]))
        accepted = self.submit(store, item, subjects=entries[:200])
        self.assertEqual(accepted["errors"], [])
        pending = ar.load_queue(store)
        occurrences = [child for child in pending if child._context.get("task") == "occurrence"]
        continuations = [child for child in pending if child._context.get("continuation")]
        self.assertEqual(len(occurrences), 200)
        self.assertEqual(len(continuations), 1)
        continuation = continuations[0]
        self.assertEqual(len(continuation._context["continuation"]["excluded_subject_ids"]), 200)
        self.assertEqual(continuation._context["continuation"]["excluded_terms"], [])
        repeated = self.submit(store, continuation, subjects=[entries[199]])
        self.assertTrue(repeated["errors"])
        row = continuation._context["rows"][0]
        final = self.submit(store, continuation, subjects=[self.literal(row, canonicals[200])])
        self.assertEqual(final["errors"], [])
        pending = ar.load_queue(store)
        audits = [child for child in pending if child._context.get("source_boundary_audit")]
        self.assertEqual(len(audits), 1)
        self.assertEqual(len(audits[0]._context["source_boundary_audit"]["excluded_subject_ids"]), 201)
        self.assertEqual(len([child for child in pending if child._context.get("task") == "occurrence"]), 201)
        self.assertEqual(self.counts(store), dict(terms=0, relations=0))

    def test_transport_matching_does_not_change_generic_or_mismatched_contexts(self):
        raw = "build\nconfidence"
        context = dict(schema=ap._SCHEMA, task="occurrence", subject=dict(
            schema=span_subjects._SCHEMA, kind="literal", canonical=raw))
        generic = ar.make_review_item(raw, "zh_hans", [])
        self.assertEqual(generic.term, "build confidence")
        self.assertEqual(ar.ReviewItem.from_dict(generic.to_dict()).term, "build confidence")
        data = dict(generic.to_dict(), term=raw, review_context=context)
        self.assertEqual(ar.ReviewItem.from_dict(data).term, raw)
        self.assertTrue(ar._is_raw_subject_term(raw, context))
        self.assertFalse(ar._is_raw_subject_term(None, context))
        self.assertFalse(ar._is_raw_subject_term(raw, []))
        variants = [dict(context, schema="other"), dict(context, task="discovery"),
                    dict(context, subject="not a subject"),
                    dict(context, subject=dict(context["subject"], schema="other")),
                    dict(context, subject=dict(context["subject"], kind="other")),
                    dict(context, subject=dict(context["subject"], canonical="build confidence"))]
        for value in variants:
            with self.subTest(context=value):
                self.assertFalse(ar._is_raw_subject_term(raw, value))
                self.assertEqual(ar.ReviewItem.from_dict(dict(data, review_context=value)).term,
                                 "build confidence")

    def test_mismatching_persisted_term_is_not_recovered_from_subject_canonical(self):
        raw = "build\nconfidence"
        store, discovery, row = self.fixture("A: We should " + raw + " together.")
        self.assertEqual(self.submit(store, discovery, subjects=[self.literal(row, raw)])["errors"], [])
        queued = ar.load_queue(store)
        child = next(item for item in queued if item._context.get("task") == "occurrence")
        if self.STORE_VERSION == 1:
            child.term = "build confidence"
            ar._write_queue(store, queued)
        else:
            with dbstore.connect(store) as conn:
                conn.execute("UPDATE review_queue SET term_id=? WHERE item_id=?",
                             ("build confidence", child.id))
                conn.commit()
        loaded = next(item for item in ar.load_queue(store) if item.id == child.id)
        self.assertEqual(loaded.term, "build confidence")
        self.assertEqual(loaded._context["subject"]["canonical"], raw)
        result = self.reject(store, loaded, relations=[])
        self.assertIn("review packet identity does not match its immutable context", json.dumps(result["errors"]))

    def test_rehashed_packet_cannot_borrow_a_mutated_subject_identity(self):
        raw = "build\nconfidence"
        store, discovery, row = self.fixture("A: We should " + raw + " together.")
        self.assertEqual(self.submit(store, discovery, subjects=[self.literal(row, raw)])["errors"], [])
        child = next(item for item in ar.load_queue(store) if item._context.get("task") == "occurrence")
        context = deepcopy(child._context)
        context["subject"]["canonical"] = "build confidence"
        forged = ap._item("build confidence", child.language, [], child.kind, context, "Forged subject")
        ar.enqueue(store, [forged])
        result = self.reject(store, forged, relations=[])
        self.assertIn("span subject identity or raw provenance changed", json.dumps(result["errors"]))


class AgentMultilineSQLiteSubjectTests(AgentMultilineSubjectTests):
    STORE_VERSION = 2


class AgentMultilineSchema3SubjectTests(AgentMultilineSubjectTests):
    STORE_VERSION = 3


if __name__ == "__main__":
    unittest.main()
