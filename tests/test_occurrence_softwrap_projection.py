"""Whitespace-only raw gaps must not lose scoped lexical query results."""
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger
from sekaisync.core import SekaiSyncCore


class OccurrenceSoftwrapProjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)

    def fixture(self, gap="\r\n", kind="lexical"):
        source = dict(id="web:fixture:en:event_story:999:1", source="fixture", language="en", trust="B",
                      kind="event_story", text="A: We take this rumor into account.")
        target = dict(id="web:fixture:zh_hans:event_story:999:1", source="fixture", language="zh_hans", trust="B",
                      kind="event_story", text="\u7532\uff1a\u8003\u8651" + gap + "\u8fd9\u4ef6\u4e8b\u3002")
        dbstore.upsert_web_pages(self.store, "fixture", [source, target])
        view = ap._view(source, [0], ap._lines(source)[1])
        row = dict(story_key="event:999:1", source=view, targets={})
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["zh_hans"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(self.store, scope_id), scope)
        task = ap._item("@discover:event:999:1:" + row["id"], "en", [], "discovery",
                        dict(schema=ap._SCHEMA, task="discovery", scope_id=scope_id, source_language="en", rows=[row]), "Fixture")
        ar.enqueue(self.store, [task])
        parts = [dict(start=source["text"].index(exact), end=source["text"].index(exact) + len(exact), exact=exact)
                 for exact in ("take", "into account")]
        result = ar.submit_judgments(self.store, [dict(id=task.id, decision="accept",
                                  subjects=[dict(kind="segmented", evidence_id=row["id"], segments=parts)])])
        self.assertEqual(result["errors"], [])
        pending = ar.load_queue(self.store)
        self.assertEqual([child._context["task"] for child in pending], ["occurrence", "discovery"])
        item, audit = pending
        self.assertEqual(audit._context["source_boundary_audit"]["terminal_parent_id"], task.id)
        target_parts = [dict(start=target["text"].index(exact), end=target["text"].index(exact) + len(exact), exact=exact)
                        for exact in ("\u8003\u8651", "\u8fd9\u4ef6\u4e8b")]
        relation = dict(evidence_id=item._context["rows"][0]["id"], source_segments=parts, target_segments=target_parts,
                        sense_key="consider", sense_gloss="take a matter into consideration", kind=kind, rationale="Synthetic scoped fixture")
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", relations=[relation])])
        self.assertEqual(result["errors"], [])
        return item, relation

    def consumers(self):
        core = SekaiSyncCore(self.store)
        with core.request_view():
            penetrate = core.term_penetrate("take into account", story_key="event:999:1", languages=["en", "zh_hans"])
            generic = core.query("take into account")
        self.assertIsNotNone(penetrate)
        self.assertEqual(len(generic["terms"]), 1)
        self.assertEqual(generic["terms"][0]["names"], {})
        self.assertEqual(penetrate["term"]["names"], {})
        return penetrate["per_language"]["zh_hans"], next(position for position in generic["terms"][0]["positions"]
                                                           if position["language"] == "zh_hans")

    def test_same_turn_crlf_parts_keep_the_actual_raw_scalar_and_no_aliases(self):
        self.fixture()
        for entry in self.consumers():
            self.assertEqual(entry["term"], "\u8003\u8651\r\n\u8fd9\u4ef6\u4e8b")
            self.assertFalse(entry.get("missing"))
        with dbstore.connect(self.store) as conn:
            relation = ledger._read_relations(conn)[0]
            self.assertEqual(len(relation["target"]["segments"]), 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_non_whitespace_inserted_gap_stays_typed_without_a_scalar(self):
        self.fixture(gap="\u5728\u8ba8\u8bba\u4e4b\u540e")
        for entry in self.consumers():
            self.assertEqual(entry["term"], "")
            self.assertTrue(entry["missing"])
            self.assertIn("lexical", entry["note"])

    def test_paraphrase_cannot_gain_a_scalar_from_whitespace_gaps(self):
        self.fixture(kind="paraphrase")
        for entry in self.consumers():
            self.assertEqual(entry["term"], "")
            self.assertIn("paraphrase", entry["note"])

    def test_equivalent_one_span_and_softwrap_parts_do_not_create_false_conflict(self):
        item, relation = self.fixture()
        target = item._context["rows"][0]["target"]
        relation = dict(relation, target_segments=[dict(start=2, end=9, exact=target["text"][2:9])])
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", relations=[relation])])
        # A receipt makes ordinary resubmission a no-op; replay a second valid
        # ledger representation directly to exercise query conflict handling.
        self.assertEqual(result["errors"], [])
        with dbstore.connect(self.store) as conn:
            first = ledger._read_relations(conn)[0]
            anchor = ledger._anchor(target, "event:999:1", relation["target_segments"])
            second = ledger._relation(first["source"], anchor, first["sense"], "lexical", first["review_item_id"],
                                      "Same raw expression with one span", grounding=first["grounding"])
            ledger._store_relations(conn, [second])
            conn.commit()
        for entry in self.consumers():
            self.assertEqual(entry["term"], "\u8003\u8651\r\n\u8fd9\u4ef6\u4e8b")


if __name__ == "__main__":
    unittest.main()
