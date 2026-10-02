"""Synthetic typed raw grounding without historical semantic answers."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, crawler, dbstore
from sekaisync import occurrence_store as ledger, source_audits, span_subjects as subjects, termindex as ti, webindex
from sekaisync.core import SekaiSyncCore


STORY = "event:994:1"
TARGET = "synthetic target anchor"


class TypedLiteralGroundingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sequence = 0

    def fixture(self, raw, language="en", version=1):
        self.sequence += 1
        store = self.base / str(self.sequence) / "store"
        dbstore.initialize(store)
        if version == 2:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                  backup_path=store.parent / "v1-backup.db")
        target_language = "ja" if language == "en" else "en"
        pages = [dict(id=f"web:synthetic:{lang}:event_story:994:1", source="synthetic",
                      kind="event_story", language=lang, trust="B", text=text)
                 for lang, text in ((language, raw), (target_language, "Reader: " + TARGET))]
        dbstore.upsert_web_pages(store, "synthetic", pages)
        groups = ti.group_pages_by_story(pages)
        items, _ = ap._prepare_scrub_review(store, groups, sorted(groups), [], {},
                                           language, [target_language])
        discovery = next(item for item in items if item._context.get("task") == "discovery")
        ar.enqueue(store, [discovery])
        return store, discovery, pages[0]

    @staticmethod
    def segments(view, fragments):
        parts, cursor = [], 0
        for exact in fragments:
            start = view["text"].index(exact, cursor)
            parts.append(dict(start=view["start"] + start,
                              end=view["start"] + start + len(exact), exact=exact))
            cursor = start + len(exact)
        return parts

    def literal(self, row, canonical, fragments=None):
        return dict(kind="literal", evidence_id=row["id"], canonical=canonical,
                    segments=self.segments(row["source"], fragments or [canonical]))

    @staticmethod
    def submit(store, item, **fields):
        payload = dict(id=item.id, decision="accept", rationale="Synthetic raw-grounding check only.", **fields)
        return ar.submit_judgments(store, ar.parse_judgments_text(json.dumps(payload)))

    @staticmethod
    def counts(store):
        with dbstore.connect(store) as conn:
            return (conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0],
                    len(ledger._read_relations(conn, current_only=False)))

    def reject(self, store, item, **fields):
        before = (ap._receipts(store), [queued.id for queued in ar.load_queue(store)], self.counts(store))
        result = self.submit(store, item, **fields)
        self.assertTrue(result["errors"], result)
        self.assertEqual(result["accepted"], 0)
        self.assertEqual((ap._receipts(store), [queued.id for queued in ar.load_queue(store)],
                          self.counts(store)), before)

    def replay(self, store, view, canonical, fragments=None):
        selected = self.segments(view, fragments or [canonical])
        subject = subjects._literal(view, STORY, canonical, selected)
        with dbstore.connect(store) as conn:
            before = conn.total_changes
            self.assertEqual(subjects._validate(conn, subject), subject)
            self.assertEqual(conn.total_changes, before)
        self.assertEqual(subject["source"], ledger._anchor(view, STORY, selected))
        self.assertEqual(subject["canonical_parts"], [part["exact"] for part in selected])
        return subject

    def accepted(self, raw, language, canonicals):
        store, discovery, _page = self.fixture(raw, language)
        row = discovery._context["rows"][0]
        expected = [self.replay(store, row["source"], canonical) for canonical in canonicals]
        result = self.submit(store, discovery, subjects=[self.literal(row, value) for value in canonicals])
        self.assertEqual(result["errors"], [], result)
        self.assertEqual(result["accepted"], 1)
        self.assertIn(discovery.id, ap._receipts(store))
        children = [item for item in ar.load_queue(store) if item._context.get("subject")]
        self.assertEqual({item._context["subject"]["id"] for item in children}, {value["id"] for value in expected})
        for child in children:
            self.assertEqual(ar.ReviewItem.from_dict(child.to_dict())._context["subject"], child._context["subject"])
        self.assertEqual(self.counts(store), (0, 0))
        return store, children, expected

    def test_katakana_internal_compound_components_are_typed_raw_literals(self):
        self.accepted("Observer: \u30df\u30f3\u30c8\u30e9\u30f3\u30bf\u30f3\u3092\u70b9\u3051\u305f\u3002", "ja",
                      ["\u30df\u30f3\u30c8", "\u30e9\u30f3\u30bf\u30f3"])

    def test_latin_internal_compound_and_morphology_are_typed_raw_literals(self):
        self.accepted("Observer: The bookbinder is repainting the frame.", "en",
                      ["book", "binder", "painting", "paint"])

    def test_cjk_and_hangul_internal_components_preserve_exact_raw_spelling(self):
        for language, raw, values in (
                ("zh_hans", "Observer: \u661f\u7802\u5de5\u623f\u5df2\u5f00\u95e8\u3002", ["\u661f\u7802", "\u5de5\u623f"]),
                ("ko", "Observer: \ub2ec\ube5b\uc11c\ub78d\uc744 \uc5f4\uc5c8\uc5b4\uc694.", ["\ub2ec\ube5b", "\uc11c\ub78d"])):
            with self.subTest(language=language):
                self.accepted(raw, language, values)

    def test_adjacent_raw_fragments_and_non_bmp_absolute_offsets_keep_anchor_identity(self):
        store, discovery, _page = self.fixture("Observer: \U0001f9ed The bookbinder waits.")
        row = discovery._context["rows"][0]
        subject = self.replay(store, row["source"], "book", ["bo", "ok"])
        selected = subject["source"]["segments"]
        self.assertEqual(selected[0]["start"], len("Observer: \U0001f9ed The "))
        self.assertEqual(subject["gap_text"], [""])
        result = self.submit(store, discovery, subjects=[self.literal(row, "book", ["bo", "ok"])])
        self.assertEqual(result["errors"], [], result)
        child = next(item for item in ar.load_queue(store) if item._context.get("subject"))
        self.assertEqual(child._context["subject"], subject)

    def test_raw_whitespace_gaps_and_long_lf_crlf_literals_remain_exact(self):
        for separator in (" ", "\n", "\r\n"):
            with self.subTest(separator=repr(separator)):
                canonical = "a" * 40 + separator + "b" * 41
                store, discovery, _page = self.fixture("Observer: " + canonical)
                row = discovery._context["rows"][0]
                subject = self.replay(store, row["source"], canonical, ["a" * 40, "b" * 41])
                self.assertEqual(subject["gap_text"], [separator])
                result = self.submit(store, discovery, subjects=[self.literal(row, canonical, ["a" * 40, "b" * 41])])
                self.assertEqual(result["errors"], [], result)

    def test_short_flat_softwrap_and_existing_alias_fallback_compatibility(self):
        for separator in ("\n", "\r\n"):
            with self.subTest(separator=repr(separator)):
                store, discovery, _page = self.fixture("Observer: polish" + separator + "the lens carefully.")
                row = discovery._context["rows"][0]
                subject = self.replay(store, row["source"], "polish the lens", ["polish", "the lens"])
                self.assertEqual(subject["gap_text"], [separator])
                self.assertEqual(self.submit(store, discovery, subjects=[self.literal(
                    row, "polish the lens", ["polish", "the lens"])])["errors"], [])
        store, discovery, _page = self.fixture("Observer: polish the lens carefully.")
        row = discovery._context["rows"][0]
        subject = self.replay(store, row["source"], " polish the lens ", ["polish the lens"])
        self.assertEqual(self.submit(store, discovery, subjects=[self.literal(
            row, subject["canonical"], ["polish the lens"])])["errors"], [])

    def test_legacy_term_selection_and_terms_still_reject_internal_latin_compound(self):
        store, discovery, _page = self.fixture("Observer: The bookbinder waits.")
        row = discovery._context["rows"][0]
        self.assertFalse(ap._term_selection(row["source"], "book", self.segments(row["source"], ["book"]),
                                            case_sensitive=True))
        self.reject(store, discovery, terms=["book"])
        self.assertTrue(ap._valid_surface("a" * 80))
        self.assertFalse(ap._valid_surface("a" * 81))
        self.assertFalse(ap._valid_surface("polish\nthe lens"))

    def test_header_and_speaker_metadata_are_rejected(self):
        store, discovery, _page = self.fixture("Observer: The bookbinder waits.")
        row = discovery._context["rows"][0]
        for exact in ("Observer", "Observer: The"):
            with self.subTest(exact=exact):
                with self.assertRaisesRegex(ValueError, "utterance body"):
                    subjects._literal(row["source"], STORY, exact, self.segments(row["source"], [exact]))
                self.reject(store, discovery, subjects=[self.literal(row, exact)])

    def test_cross_turn_and_repeated_speaker_selections_are_rejected(self):
        for label in ("Other", "Observer"):
            with self.subTest(label=label):
                raw = "Observer: book\n" + label + ": binder"
                store, discovery, page = self.fixture(raw)
                full = ap._raw_region(page, 0, len(raw))
                selected = self.segments(full, ["book", "binder"])
                envelope = raw[selected[0]["start"]:selected[-1]["end"]]
                with self.assertRaisesRegex(ValueError, "utterance body"):
                    subjects._literal(full, STORY, envelope, selected)
                row = discovery._context["rows"][0]
                self.reject(store, discovery, subjects=[dict(kind="literal", evidence_id=row["id"],
                                                            canonical=envelope, segments=selected)])

    def test_case_unicode_spelling_and_unattested_aliases_gain_no_permission(self):
        for language, raw, selected, canonical in (
                ("en", "Observer: The bookbinder waits.", "book", "Book"),
                ("en", "Observer: The bookbinder waits.", "book", "volume"),
                ("ja", "Observer: \u30ac\u30e9\u30b9\u30e9\u30f3\u30d7\u3092\u7f6e\u3044\u305f\u3002", "\u30ac\u30e9\u30b9", "\u30ab\u3099\u30e9\u30b9")):
            with self.subTest(canonical=canonical):
                store, discovery, _page = self.fixture(raw, language)
                row = discovery._context["rows"][0]
                entry = self.literal(row, canonical, [selected])
                with self.assertRaisesRegex(ValueError, "literal subject"):
                    subjects._literal(row["source"], STORY, canonical, entry["segments"])
                self.reject(store, discovery, subjects=[entry])

    def test_wrong_offsets_and_wrong_exact_raw_fragments_are_rejected(self):
        store, discovery, _page = self.fixture("Observer: The bookbinder waits.")
        row = discovery._context["rows"][0]
        entry = self.literal(row, "book")
        for changed in (dict(entry["segments"][0], start=entry["segments"][0]["start"] + 1),
                        dict(entry["segments"][0], end=entry["segments"][0]["end"] - 1),
                        dict(entry["segments"][0], exact="Book"),
                        dict(entry["segments"][0], start=True),
                        dict(entry["segments"][0], end=len(row["source"]["text"]) + 1)):
            with self.subTest(changed=changed):
                self.reject(store, discovery, subjects=[dict(entry, segments=[changed])])

    def test_nonspace_gap_is_not_selected_even_when_canonical_names_full_envelope(self):
        store, discovery, _page = self.fixture("Observer: amber and quartz glow.")
        row = discovery._context["rows"][0]
        selected = self.segments(row["source"], ["amber", "quartz"])
        for canonical in ("amber and quartz", "amberquartz", "amber quartz"):
            with self.subTest(canonical=canonical):
                with self.assertRaisesRegex(ValueError, "literal subject"):
                    subjects._literal(row["source"], STORY, canonical, selected)
                self.reject(store, discovery, subjects=[dict(kind="literal", evidence_id=row["id"],
                                                            canonical=canonical, segments=selected)])
        segmented = subjects._segmented(row["source"], STORY, selected)
        self.assertEqual(segmented["gap_text"], [" and "])

    def test_unshown_selection_and_forged_clipped_speaker_fail_full_replay(self):
        raw = "Narrator: The bookbinder waits."
        store, discovery, page = self.fixture(raw)
        full = ap._raw_region(page, 0, len(raw))
        start = raw.index("rator")
        clipped = ap._raw_region(page, start, start + len("rator"))
        forged = subjects._literal(clipped, STORY, "rator", self.segments(clipped, ["rator"]))
        with dbstore.connect(store) as conn, self.assertRaisesRegex(ValueError, "utterance body"):
            subjects._validate(conn, forged)
        valid = subjects._literal(full, STORY, "bookbinder", self.segments(full, ["bookbinder"]))
        row = discovery._context["rows"][0]
        restricted = dict(row, source=dict(full, end=valid["source"]["segments"][0]["end"] - 1,
                                          text=raw[:valid["source"]["segments"][0]["end"] - 1]))
        with dbstore.connect(store) as conn, self.assertRaises(ValueError):
            ap._validate_subject_in_rows(conn, valid, [restricted])

    def test_identity_payload_and_stale_page_are_replayed_without_writes(self):
        store, discovery, page = self.fixture("Observer: The bookbinder waits.")
        row = discovery._context["rows"][0]
        subject = self.replay(store, row["source"], "book")
        for key, value in (("canonical", "Book"), ("canonical_parts", ["Book"]),
                           ("id", "subject:literal:forged"), ("gap_text", ["invented"]),
                           ("utterance", dict(subject["utterance"], start=subject["utterance"]["start"] + 1))):
            changed = deepcopy(subject)
            changed[key] = value
            with self.subTest(key=key), dbstore.connect(store) as conn, self.assertRaises(ValueError):
                subjects._validate(conn, changed)
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=? WHERE source=? AND id=?",
                         [page["text"] + "!", page["source"], page["id"]])
            conn.commit()
        with dbstore.connect(store) as conn, self.assertRaises(ValueError):
            subjects._validate(conn, subject)

    def test_normal_typed_receipt_relation_reload_and_public_queries(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, discovery, _page = self.fixture("Observer: The bookbinder waits.", version=version)
                row = discovery._context["rows"][0]
                subject = self.replay(store, row["source"], "book")
                self.assertEqual(self.submit(store, discovery, subjects=[self.literal(row, "book")])["errors"], [])
                child = next(item for item in ar.load_queue(store) if item._context.get("subject"))
                evidence = child._context["rows"][0]
                relation = dict(evidence_id=evidence["id"], source_segments=subject["source"]["segments"],
                                target_segments=self.segments(evidence["target"], [TARGET]), kind="lexical",
                                sense_key="synthetic-raw-constituent", sense_gloss="Structural fixture, not semantic gold.",
                                rationale="Normal mechanical subject and relation round trip.")
                self.assertEqual(self.submit(store, child, relations=[relation])["errors"], [])
                with dbstore.connect(store) as conn:
                    persisted = ledger._read_relations(conn)
                    self.assertEqual(len(persisted), 1)
                    self.assertEqual(persisted[0]["source"], subject["source"])
                    self.assertEqual(persisted[0]["grounding"]["context"]["subject"], subject)
                before = (self.counts(store), ap._receipts(store), [queued.id for queued in ar.load_queue(store)])
                core = SekaiSyncCore(store)
                self.assertEqual(len(core.query("book", include_web=False)["terms"]), 1)
                self.assertEqual(len(core.term_lookup("book", source_language="en", languages=["en", "ja"])), 1)
                self.assertIsNotNone(core.term_penetrate("book", story_key=STORY, languages=["en", "ja"]))
                self.assertEqual((self.counts(store), ap._receipts(store),
                                  [queued.id for queued in ar.load_queue(store)]), before)

    def test_wording_values_keep_existing_adjacent_only_fragment_policy(self):
        store = self.base / "wording-store"
        dbstore.initialize_new_store(store, target_version=3)
        pages = [crawler.altsource_sv_record_page(
            dict(wordingKey="SYNTHETIC_RAW_LITERAL_CONTIGUITY_994", value=value), "wordings", region)
            for region, value in (("en", "book binder"), ("jp", TARGET))]
        webindex.save_web_pages(store, pages[0].source, pages, write_categories=False, rewrite_index=False)
        loaded = ti.load_pages(store)
        groups = ti.group_pages_by_story(loaded)
        items, _ = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", ["ja"])
        discovery = next(item for item in items if item._context.get("task") == "discovery")
        ar.enqueue(store, [discovery])
        row = discovery._context["rows"][0]
        selected = self.segments(row["source"], ["book", "binder"])
        with self.assertRaisesRegex(ValueError, "literal subject"):
            subjects._literal(row["source"], row["story_key"], "book binder", selected)
        self.reject(store, discovery, subjects=[dict(kind="literal", evidence_id=row["id"],
                                                    canonical="book binder", segments=selected)])
        adjacent = self.segments(row["source"], ["book ", "binder"])
        subject = subjects._literal(row["source"], row["story_key"], "book binder", adjacent)
        with dbstore.connect(store) as conn:
            self.assertEqual(subjects._validate(conn, subject), subject)

    def test_generated_boundary_audit_accepts_new_raw_internal_constituents(self):
        compound = "\u30df\u30f3\u30c8\u30e9\u30f3\u30bf\u30f3"
        proposition = compound + "\u3092\u70b9\u3051\u305f\u3002"
        components = ["\u30df\u30f3\u30c8", "\u30e9\u30f3\u30bf\u30f3"]
        for version in (1, 2):
            with self.subTest(version=version):
                store, discovery, _page = self.fixture("Observer: " + proposition, "ja", version)
                row = discovery._context["rows"][0]
                initial_specs = [self.literal(row, canonical) for canonical in (proposition, compound)]
                initial = [self.replay(store, row["source"], canonical) for canonical in (proposition, compound)]
                self.assertEqual(self.submit(store, discovery, subjects=initial_specs)["errors"], [])
                audit, = [item for item in ar.load_queue(store) if source_audits._is_audit(item._context)]
                marker = audit._context["source_boundary_audit"]
                self.assertEqual(marker["terminal_parent_id"], discovery.id)
                self.assertEqual(marker["ordinary_ancestor_ids"], [])
                self.assertEqual(marker["excluded_terms"], [])
                self.assertEqual(set(marker["excluded_subject_ids"]), {subject["id"] for subject in initial})
                self.assertEqual({subject["id"] for subject in marker["inherited_subjects"]},
                                 {subject["id"] for subject in initial})
                self.assertEqual(audit._context["rows"], discovery._context["rows"])
                exported = ar.export_for_agent(store, self.base / f"boundary-audit-{version}.txt", limit=0)
                rendered = exported.read_text(encoding="utf-8")
                self.assertIn(audit.id, rendered)
                self.assertIn("source_boundary_audit_inherited:", rendered)
                self.reject(store, audit, subjects=[initial_specs[1]])
                audit_row = audit._context["rows"][0]
                internal_specs = [self.literal(audit_row, canonical) for canonical in components]
                internal = [self.replay(store, audit_row["source"], canonical) for canonical in components]
                self.assertTrue({subject["id"] for subject in initial}.isdisjoint(
                    subject["id"] for subject in internal))
                self.assertEqual(self.submit(store, audit, subjects=internal_specs)["errors"], [])
                children = [item for item in ar.load_queue(store)
                            if item._context.get("parent_discovery_id") == audit.id]
                self.assertEqual({item._context["subject"]["id"] for item in children},
                                 {subject["id"] for subject in internal})
                self.assertFalse(any(source_audits._is_audit(item._context) for item in ar.load_queue(store)))
                receipts = ap._receipts(store)
                self.assertEqual(json.loads(receipts[discovery.id]["value"])["subjects"], initial)
                self.assertEqual(json.loads(receipts[audit.id]["value"])["subjects"], internal)
                self.assertEqual(receipts[audit.id]["review_context"], audit._context)
                self.assertEqual(self.counts(store), (0, 0))
                before = (ap._receipts(store), [item.to_dict() for item in ar.load_queue(store)], self.counts(store))
                self.assertEqual(self.submit(store, discovery, subjects=initial_specs)["errors"], [])
                self.assertEqual(self.submit(store, audit, subjects=internal_specs)["errors"], [])
                self.assertEqual(source_audits._ensure(store)["added"], 0)
                self.assertEqual((ap._receipts(store), [item.to_dict() for item in ar.load_queue(store)],
                                  self.counts(store)), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
