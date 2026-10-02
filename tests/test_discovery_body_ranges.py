"""Selectable body geometry, source-only rendering and indexed typed errors."""
from pathlib import Path
import hashlib
import json
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, crawler, dbstore
from sekaisync import span_subjects as spans, termindex as ti
from sekaisync.wording_identity import _full_view


class DiscoveryBodyRangesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, raw):
        store = self.base / "store"
        dbstore.initialize_new_store(store, target_version=3)
        page = dict(id="web:synthetic:en:event_story:992:1", source="synthetic",
                    kind="event_story", language="en", trust="B", text=raw)
        dbstore.upsert_web_pages(store, "synthetic", [page])
        groups = ti.group_pages_by_story([page])
        items, _ = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", ["ja"])
        first = next(item for item in items if item._context["task"] == "discovery")
        ar.enqueue(store, [first])
        return store, first

    @staticmethod
    def part(view, exact, at=0):
        start = view["text"].index(exact, at)
        return dict(start=view["start"] + start, end=view["start"] + start + len(exact), exact=exact)

    @staticmethod
    def view(raw, start=0):
        return dict(source="synthetic", page_id="synthetic", language="en",
                    sha256=hashlib.sha256(raw.encode()).hexdigest(), start=start,
                    end=start + len(raw), text=raw, complete=True)

    @staticmethod
    def submit(store, item, entries):
        raw = json.dumps(dict(id=item.id, decision="accept", subjects=entries,
                             rationale="Synthetic structural regression, not semantic gold."))
        return ar.submit_judgments(store, ar.parse_judgments_text(raw))

    @staticmethod
    def rendered_ranges(item):
        return [json.loads(line[len("source_body_ranges: "):]) for line in ar.render_item(item).splitlines()
                if line.startswith("source_body_ranges: ")]

    def assert_rendered_ranges(self, store, item, name):
        before = item.to_dict()
        path = ar.export_for_agent(store, self.base / name, limit=0)
        records = self.rendered_ranges(item)
        rows = item._context["rows"]
        self.assertEqual(len(records), len(rows))
        self.assertIn("speaker headers", ar.render_item(item))
        for record, row in zip(records, rows):
            self.assertEqual(record["evidence_id"], row["id"])
            self.assertEqual(record["utterances"], list(spans._utterance_body_ranges(row["source"])))
        self.assertIn("source_body_ranges:", path.read_text(encoding="utf-8"))
        self.assertEqual(item.to_dict(), before)

    def test_normal_initial_and_generated_audit_reveal_body_ranges(self):
        store, item = self.fixture("An's father: Wait by the bus stop.\nAn: An is waiting.")
        self.assert_rendered_ranges(store, item, "initial.txt")
        row = item._context["rows"][0]
        parts = [self.part(row["source"], "Wait by the bus stop")]
        result = self.submit(store, item, [dict(kind="literal", evidence_id=row["id"],
                                               canonical=parts[0]["exact"], segments=parts)])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["accepted"], 1)
        self.assertIn(item.id, ap._receipts(store))
        audit = next(child for child in ar.load_queue(store) if child._context.get("source_boundary_audit"))
        self.assert_rendered_ranges(store, audit, "audit.txt")

    def test_header_poison_is_atomic_with_indexed_error_and_body_repeat_accepts(self):
        store, item = self.fixture("An: An is waiting.")
        row = item._context["rows"][0]
        entries = [dict(kind="literal", evidence_id=row["id"], canonical="An",
                        segments=[self.part(row["source"], "An", at=position)])
                   for position in (4, 0)]
        queue_before = [queued.to_dict() for queued in ar.load_queue(store)]
        result = self.submit(store, item, entries)
        self.assertEqual(result["accepted"], 0)
        self.assertTrue(result["errors"])
        self.assertIn("subject 2 (evidence_id=" + row["id"] + ")", str(result["errors"]))
        self.assertIn("one utterance body", str(result["errors"]))
        self.assertNotIn(item.id, ap._receipts(store))
        self.assertEqual([queued.to_dict() for queued in ar.load_queue(store)], queue_before)
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
        result = self.submit(store, item, entries[:1])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["accepted"], 1)

    def test_clipped_header_candidate_fails_full_page_replay_with_indexed_error(self):
        store, item = self.fixture("An's father: Wait here.")
        row = item._context["rows"][0]
        start = row["source"]["text"].index("father")
        view = dict(row["source"], start=start, end=start + len("father"), text="father")
        parts = [self.part(view, "father")]
        subject = spans._literal(view, row["story_key"], "father", parts)
        with dbstore.connect(store) as conn:
            with self.assertRaisesRegex(ValueError, "speaker metadata"):
                spans._validate(conn, subject)
            clipped_context = dict(item._context, rows=[dict(row, source=view)])
            with self.assertRaisesRegex(ValueError, "subject 1.*" + row["id"] + ".*speaker metadata"):
                ap._discovery_subjects(conn, clipped_context, [dict(
                    kind="literal", evidence_id=row["id"], canonical="father", segments=parts)])

    def test_raw_offsets_softwrap_fullwidth_colon_and_repeated_speaker_boundaries(self):
        for newline in ("\n", "\r\n"):
            for colon in (":", "\uff1a"):
                with self.subTest(newline=repr(newline), colon=colon):
                    raw = "A" + colon + " Wait for her" + newline + "in line." + newline + "A" + colon + " Stay here."
                    view = self.view(raw, start=100)
                    parts = [self.part(view, "Wait for her"), self.part(view, "in line.")]
                    turns = spans._utterance_body_ranges(view)
                    self.assertEqual(len(turns), 2)
                    self.assertEqual(turns[0]["body_ranges"][0]["start"], parts[0]["start"])
                    self.assertEqual(spans._utterance(view, parts), turns[0])
                    with self.assertRaisesRegex(ValueError, "different turns"):
                        spans._utterance(view, [parts[0], self.part(view, "Stay here.")])
                    clipped = self.view("remaining body" + newline + "soft wrap", start=300)
                    selected = [self.part(clipped, "remaining body"), self.part(clipped, "soft wrap")]
                    self.assertEqual(spans._utterance(clipped, selected), dict(
                        start=300, end=clipped["end"], body_ranges=[dict(start=300, end=clipped["end"]) ]))

    def test_native_wording_whole_value_does_not_treat_colon_as_header(self):
        raw = "Status: " + "x " * 1000 + "call your adviser"
        page = crawler.altsource_sv_record_page(
            dict(wordingKey="SYNTHETIC_BODY_RANGES_WORDING", value=raw), "wordings", "en")
        view = _full_view(vars(page))
        turns = spans._utterance_body_ranges(view)
        self.assertEqual(turns, (dict(start=0, end=len(raw), body_ranges=[dict(start=0, end=len(raw))]),))
        parts = [self.part(view, "Status:")]
        self.assertEqual(spans._literal(view, page.canonical_key, "Status:", parts)["utterance"], turns[0])
        with self.assertRaisesRegex(ValueError, "exact raw value text"):
            spans._utterance(view, [dict(start=0, end=1, exact="wrong")])
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id="synthetic", source_language="en",
                       rows=[dict(id="row:wording", story_key=page.canonical_key, source=view)])
        item = ap._item("@discover:synthetic", "en", [], "discovery", context, "Synthetic wording render.")
        visible = next(json.loads(line[len("context: "):]) for line in ap._render_context(item)
                       if line.startswith("context: "))
        self.assertEqual(visible["source"]["text"], raw)
        self.assertTrue(visible["source"]["complete"])
        self.assertEqual(self.rendered_ranges(item)[0]["utterances"], list(turns))

    def test_ordinary_ranges_never_advertise_unshown_clipped_tail(self):
        view = dict(self.view("A: " + "x" * 2000, start=100), complete=False)
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id="synthetic", source_language="en",
                       rows=[dict(id="row:clipped", story_key="event:992:1", source=view)])
        item = ap._item("@discover:synthetic", "en", [], "discovery", context, "Synthetic clipped render.")
        visible = next(json.loads(line[len("context: "):]) for line in ap._render_context(item)
                       if line.startswith("context: "))
        shown_end = view["start"] + len(visible["source"]["text"])
        self.assertEqual(shown_end, view["start"] + ap._TURN_CHARS)
        self.assertLess(shown_end, view["end"])
        for turn in self.rendered_ranges(item)[0]["utterances"]:
            self.assertLessEqual(turn["end"], shown_end)
            for body in turn["body_ranges"]:
                self.assertLessEqual(body["end"], shown_end)


if __name__ == "__main__":
    unittest.main(verbosity=2)
