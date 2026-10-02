"""Typed source representability, exact acquisition and host contract regressions."""
from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import unittest
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap, agent_review as ar, dbstore
from sekaisync import occurrence_store as ledger, span_subjects as subjects, termindex as ti
from sekaisync.core import SekaiSyncCore


RUN = ROOT / "work/p0-exhaustive-20261001/typed-source-integration-01" / ("regression-run-" + uuid.uuid4().hex)
STORY = "event:993:1"
TARGET = "\u6307\u5b9a\u3055\u308c\u305f\u62c5\u5f53\u8005\u304c\u6761\u4ef6\u3092\u78ba\u8a8d\u3059\u308b\u307e\u3067\u3001\u8a02\u6b63\u3057\u305f\u901a\u77e5\u3092\u516c\u8868\u3057\u3066\u306f\u3044\u3051\u307e\u305b\u3093\u3002"
LONG = ("We must not release the corrected notice unless the designated reviewers "
        "have checked every stated condition and approved its exact final wording.")


class TypedSourceIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = RUN / self._testMethodName
        self.base.mkdir(parents=True)
        self.sequence = 0

    def fixture(self, raw, version=1):
        self.sequence += 1
        store = self.base / str(self.sequence) / "store"
        dbstore.initialize(store)
        if version == 2:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                  backup_path=store.parent / "v1-backup.db")
        pages = [dict(id=f"web:synthetic:{language}:event_story:993:1", source="synthetic",
                      kind="event_story", language=language, trust="B", text=text)
                 for language, text in (("en", raw), ("ja", "Reader: " + TARGET))]
        dbstore.upsert_web_pages(store, "synthetic", pages)
        groups = ti.group_pages_by_story(pages)
        items, _ = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", ["ja"])
        discovery = next(item for item in items if item._context.get("task") == "discovery")
        ar.enqueue(store, [discovery])
        return store, discovery, pages[0]

    @staticmethod
    def segments(view, fragments):
        selected, cursor = [], 0
        for exact in fragments:
            start = view["text"].index(exact, cursor)
            selected.append(dict(start=view["start"] + start,
                                 end=view["start"] + start + len(exact), exact=exact))
            cursor = start + len(exact)
        return selected

    def literal(self, row, canonical, fragments=None):
        return dict(kind="literal", evidence_id=row["id"], canonical=canonical,
                    segments=self.segments(row["source"], fragments or [canonical]))

    @staticmethod
    def submit(store, item, **answer):
        payload = dict(id=item.id, decision="accept", rationale="Synthetic structural fixture only.", **answer)
        return ar.submit_judgments(store, ar.parse_judgments_text(json.dumps(payload)))

    @staticmethod
    def counts(store):
        with dbstore.connect(store) as conn:
            return (conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0],
                    len(ledger._read_relations(conn, current_only=False)))

    def reject(self, store, parent, **answer):
        queued = [item.id for item in ar.load_queue(store)]
        receipts = ap._receipts(store)
        counts = self.counts(store)
        result = self.submit(store, parent, **answer)
        self.assertTrue(result["errors"], result)
        self.assertEqual(result["accepted"], 0)
        self.assertEqual([item.id for item in ar.load_queue(store)], queued)
        self.assertEqual(ap._receipts(store), receipts)
        self.assertEqual(self.counts(store), counts)
        self.assertNotIn(parent.id, receipts)

    def replay(self, store, view, canonical, fragments=None):
        selected = self.segments(view, fragments or [canonical])
        subject = subjects._literal(view, STORY, canonical, selected)
        with dbstore.connect(store) as conn:
            self.assertEqual(subjects._validate(conn, subject), subject)
        return subject

    def test_private_exact_raw_gate_accepts_81_and_1800_no_nl_lf_crlf(self):
        for separator in ("", "\n", "\r\n"):
            for length in (81, 1800):
                with self.subTest(separator=repr(separator), length=length):
                    canonical = "a" * 40 + separator + "b" * (length - 40 - len(separator))
                    store, _discovery, page = self.fixture("A: " + canonical)
                    # A body-only window fits the declared cap independently of
                    # ordinary oversized-turn window-seam acquisition limits.
                    view = ap._raw_region(page, 3, len(page["text"]))
                    subject = self.replay(store, view, canonical)
                    with dbstore.connect(store) as conn:
                        self.assertEqual(ap._validate_subject_in_rows(
                            conn, subject, [dict(source=view, story_key=STORY)]), subject)
                    self.assertEqual(len(subject["canonical"]), length)
                    self.assertEqual(subject["source"]["segments"][0]["exact"], canonical)

    def test_private_raw_1801_rejected_after_full_replay(self):
        for separator in ("", "\n", "\r\n"):
            with self.subTest(separator=repr(separator)):
                canonical = "a" * 40 + separator + "b" * (1801 - 40 - len(separator))
                store, _discovery, page = self.fixture("A: " + canonical)
                view = ap._raw_region(page, 3, len(page["text"]))
                subject = self.replay(store, view, canonical)
                with dbstore.connect(store) as conn, self.assertRaises(ValueError):
                    ap._validate_subject_in_rows(conn, subject, [dict(source=view, story_key=STORY)])

    def test_short_flat_softwrap_and_legacy_80_remain_unchanged(self):
        canonical = "We must not release the corrected notice."
        raw = canonical.replace(" the ", "\nthe ", 1)
        store, discovery, _page = self.fixture("A: " + raw)
        row = discovery._context["rows"][0]
        entry = self.literal(row, canonical, raw.split("\n"))
        result = self.submit(store, discovery, subjects=[entry])
        self.assertEqual(result["errors"], [])
        child = next(item for item in ar.load_queue(store) if item._context.get("subject"))
        self.assertEqual(child._context["subject"]["canonical"], canonical)
        self.assertEqual(child._context["subject"]["gap_text"], ["\n"])
        self.assertTrue(ap._valid_surface("a" * 80))
        self.assertFalse(ap._valid_surface("a" * 81))
        self.assertFalse(ap._valid_surface("raw\ncanonical"))
        self.assertFalse(ap._valid_surface("raw\tcanonical"))
        self.assertEqual(self.counts(store), (0, 0))

    def test_long_aliases_and_controls_rejected_atomically(self):
        variants = [(LONG, LONG.replace(" notice ", " notice  "), [LONG]),
                    (LONG, LONG.replace(" unless ", " unless\n"), [LONG]),
                    (LONG.replace(" unless ", " unless\n"), LONG,
                     LONG.replace(" unless ", " unless\n").split("\n"))]
        variants += [(LONG.replace(" unless ", " unless" + control),
                      LONG.replace(" unless ", " unless" + control), None)
                     for control in ("\t", "\r", "\x00", "\x1f")]
        for raw, canonical, fragments in variants:
            with self.subTest(raw=repr(raw), canonical=repr(canonical)):
                store, discovery, _page = self.fixture("A: " + raw)
                row = discovery._context["rows"][0]
                entry = self.literal(row, canonical, fragments)
                self.replay(store, row["source"], canonical, fragments)
                self.reject(store, discovery, subjects=[self.literal(row, "We"), entry])

    def test_long_speaker_cross_turn_and_unshown_selection_rejected(self):
        cases = ["Narrator: " + LONG,
                 "A: " + LONG + "\nB: A separate concluding turn.",
                 "A: " + LONG + "\nA: A repeated-speaker separate turn."]
        for raw in cases:
            with self.subTest(raw=raw):
                store, discovery, page = self.fixture(raw)
                row = discovery._context["rows"][0]
                full = ap._raw_region(page, 0, len(raw))
                start = 0 if raw.startswith("Narrator") else 3
                exact = raw[start:]
                entry = dict(kind="literal", evidence_id=row["id"], canonical=exact,
                             segments=[dict(start=start, end=len(raw), exact=exact)])
                with self.assertRaisesRegex(ValueError, "utterance body"):
                    subjects._literal(full, STORY, exact, entry["segments"])
                self.reject(store, discovery, subjects=[entry])
        store, discovery, page = self.fixture("A: " + LONG)
        full = ap._raw_region(page, 0, len(page["text"]))
        subject = self.replay(store, full, LONG)
        clipped = dict(full, end=full["end"] - 1, text=full["text"][:-1])
        with dbstore.connect(store) as conn, self.assertRaises(ValueError):
            ap._validate_subject_in_rows(conn, subject, [dict(source=clipped, story_key=STORY)])
        # Normal oversized-turn acquisition must not join two exported windows.
        raw = "A: " + "x" * 1680 + " " + LONG
        store, discovery, page = self.fixture(raw)
        row = discovery._context["rows"][0]
        start = raw.index(LONG)
        self.assertGreater(start + len(LONG), row["source"]["end"])
        entry = dict(kind="literal", evidence_id=row["id"], canonical=LONG,
                     segments=[dict(start=start, end=start + len(LONG), exact=LONG)])
        self.reject(store, discovery, subjects=[entry])

    def test_normal_long_typed_only_receipt_reload_query_and_penetrate(self):
        for version in (1, 2):
            for separator in (" ", "\n", "\r\n"):
                with self.subTest(version=version, separator=repr(separator)):
                    canonical = LONG.replace(" unless ", " unless" + separator)
                    store, discovery, _page = self.fixture("A: " + canonical, version)
                    row = discovery._context["rows"][0]
                    fragments = canonical.split(separator) if separator != " " else [canonical]
                    entry = self.literal(row, canonical, fragments)
                    expected = self.replay(store, row["source"], canonical, fragments)
                    result = self.submit(store, discovery, subjects=[entry])
                    self.assertEqual(result["errors"], [], "exact long typed discovery must produce a normal receipt")
                    self.assertIn(discovery.id, ap._receipts(store))
                    self.assertEqual(self.counts(store), (0, 0))
                    child = next(item for item in ar.load_queue(store)
                                 if item._context.get("task") == "occurrence")
                    self.assertEqual(child._context["subject"], expected)
                    self.assertEqual(child.term, canonical)
                    self.assertEqual(ar.ReviewItem.from_dict(child.to_dict()).term, canonical)
                    relation_row = child._context["rows"][0]
                    relation = dict(evidence_id=relation_row["id"], source_segments=entry["segments"],
                                    target_segments=self.segments(relation_row["target"], [TARGET]),
                                    kind="lexical", sense_key="synthetic-conditional-notice",
                                    sense_gloss="Synthetic contextual fixture, not semantic gold.",
                                    rationale="Mechanical exact-source and public consumer test.")
                    self.assertEqual(self.submit(store, child, relations=[relation])["errors"], [])
                    self.assertIn(child.id, ap._receipts(store))
                    with dbstore.connect(store) as conn:
                        persisted = ledger._read_relations(conn)
                        self.assertEqual(len(persisted), 1)
                        self.assertEqual(persisted[0]["grounding"]["context"]["subject"], expected)
                        self.assertEqual(persisted[0]["source"], expected["source"])
                    before = (self.counts(store), ap._receipts(store), [item.id for item in ar.load_queue(store)])
                    core = SekaiSyncCore(store)
                    generic = core.query(canonical, include_web=False)
                    lookup = core.term_lookup(canonical, source_language="en", languages=["en", "ja"])
                    penetration = core.term_penetrate(canonical, story_key=STORY, languages=["en", "ja"])
                    self.assertEqual(len(generic["terms"]), 1)
                    self.assertEqual(len(lookup), 1)
                    self.assertIsNotNone(penetration)
                    self.assertEqual(penetration["term"]["canonical"], canonical)
                    self.assertEqual(penetration["per_language"]["en"]["term"], canonical)
                    self.assertEqual(penetration["per_language"]["ja"]["term"], TARGET)
                    if separator != " ":
                        flat = canonical.replace("\r\n", " ").replace("\n", " ")
                        self.assertEqual(core.query(flat, include_web=False)["terms"], [])
                        self.assertIsNone(core.term_penetrate(flat, story_key=STORY))
                    self.assertEqual(self.counts(store), (0, 1))
                    self.assertEqual((self.counts(store), ap._receipts(store),
                                      [item.id for item in ar.load_queue(store)]), before)

    def test_long_and_raw_legacy_terms_still_poison_typed_batch_atomically(self):
        for canonical in (LONG, LONG.replace(" unless ", " unless\n")):
            with self.subTest(canonical=repr(canonical)):
                store, discovery, _page = self.fixture("A: " + canonical)
                row = discovery._context["rows"][0]
                self.reject(store, discovery, terms=[canonical],
                            subjects=[self.literal(row, "We"), self.literal(row, canonical)])

    def test_render_discovery_explicitly_distinguishes_typed_only_and_legacy(self):
        _store, discovery, _page = self.fixture("A: " + LONG)
        rendered = ar.render_item(discovery).lower()
        contract = next(line for line in rendered.splitlines() if line.startswith("discovery_contract:"))
        self.assertRegex(contract, r"(?:legacy|terms).{0,100}1[- ]80")
        self.assertRegex(contract, r"(?:typed|literal).{0,150}1800")
        self.assertRegex(contract, r"terms.{0,100}(?:omit|optional|empty)")
        self.assertRegex(contract, r"(?:do not|never).{0,100}mirror")
        self.assertRegex(contract, r"(?:control|c0)")

    def test_render_discovery_prioritizes_complete_proposition_components(self):
        _store, discovery, _page = self.fixture("A: " + LONG)
        rendered = ar.render_item(discovery).lower()
        passes = next(line for line in rendered.splitlines() if line.startswith("discovery_passes:"))
        self.assertRegex(passes, r"complete (?:proposition|clause)")
        self.assertIn("participants", passes)
        self.assertIn("operators", passes)
        self.assertRegex(passes, r"(?:discourse|illocutionary) force")
        self.assertRegex(passes, r"(?:first|priorit|before)")
        proposition = re.search(r"complete (?:proposition|clause)", passes)
        noun = re.search(r"noun heads", passes)
        if noun is not None:
            self.assertLess(proposition.start(), noun.start())


if __name__ == "__main__":
    print("ARTIFACT_DIRECTORY=" + str(RUN), flush=True)
    unittest.main(verbosity=2)
