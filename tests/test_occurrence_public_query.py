import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as os, termindex
from sekaisync.core import SekaiSyncCore


class OccurrencePublicQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        dbstore.initialize(self.root)
        self.names = dict(ja="空港", en="airport", zh_hans="机场", zh_tw="機場", ko="공항")
        self.pages = [dict(id=f"web:fixture:{lang}:event_story:1:1", source="fixture", language=lang,
                           trust="B", kind="event_story", text=f"A: {name}.", story_key="event:1:1")
                      for lang, name in self.names.items()]
        dbstore.upsert_web_pages(self.root, "fixture", self.pages)

    def task(self, source="ja", target="en"):
        groups = termindex.group_pages_by_story(self.pages)
        items, _ = ap._prepare_scrub_review(self.root, groups, list(groups), [self.names[source]], {}, source, [target])
        item = next(item for item in items if item._context["task"] == "occurrence")
        ar.enqueue(self.root, [item])
        return item

    def proposal(self, item, kind="lexical", sense="airport"):
        row = item._context["rows"][0]
        source, target = item._context["source_language"], item.language
        return dict(evidence_id=row["id"],
                    source_segments=ap._body_term_segments(row["source"]["text"], self.names[source], row["source"]["start"])[0],
                    target_segments=ap._body_term_segments(row["target"]["text"], self.names[target], row["target"]["start"])[0],
                    sense_key=sense, sense_gloss="place for airplanes" if sense == "airport" else "a different contextual sense",
                    kind=kind, rationale="Same contextual airport referent in the paired original utterance")

    def submit(self, item, proposals):
        result = ar.submit_judgments(self.root, [dict(id=item.id, decision="accept", relations=proposals)])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["applied_slots"], 0)

    def test_existing_core_serves_all_twenty_directions_without_global_names(self):
        for source in self.names:
            for target in self.names:
                if source != target:
                    item = self.task(source, target)
                    self.submit(item, [self.proposal(item)])
        core = SekaiSyncCore(self.root)
        for source, query in self.names.items():
            for target, expected in self.names.items():
                if source != target:
                    with self.subTest(source=source, target=target):
                        result = core.term_penetrate(query, story_key="event:1:1", languages=[target])
                        self.assertEqual(result["per_language"][target]["term"], expected)
                        self.assertNotIn("missing", result["per_language"][target])
                        self.assertEqual(result["term"]["names"], {source: query})
                        self.assertEqual(set(result), {"query", "term", "story_key", "released", "per_language", "cloud_rank"})
        lookup = core.term_lookup(self.names["ja"])
        self.assertEqual(len(lookup), 1)
        self.assertEqual(lookup[0]["names"], {"ja": self.names["ja"]})
        self.assertEqual({position["language"]: position["term"] for position in lookup[0]["positions"]},
                         self.names)
        with dbstore.connect(self.root) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_default_query_selects_real_scope_and_language_alias_is_preserved(self):
        item = self.task("ja", "zh_tw")
        self.submit(item, [self.proposal(item)])
        result = SekaiSyncCore(self.root).term_penetrate("空港", languages=["zh_hant", "en"])
        self.assertEqual(result["story_key"], "event:1:1")
        self.assertEqual(result["per_language"]["zh_hant"]["term"], "機場")
        self.assertTrue(result["per_language"]["en"]["missing"])

    def test_occurrence_query_reads_only_anchored_pages_without_global_text_load(self):
        item = self.task()
        self.submit(item, [self.proposal(item)])
        unrelated = dict(self.pages[0], id="web:fixture:ja:event_story:2:1", story_key="event:2:1",
                         text="A: Unrelated corpus body.")
        dbstore.upsert_web_pages(self.root, "fixture", [unrelated])
        with dbstore.connect(self.root) as conn:
            pages = os._query_pages(conn, os._relations_for_query(conn, "空港"))
        self.assertEqual({page["language"] for page in pages}, {"ja", "en"})
        self.assertNotIn(unrelated["id"], {page["id"] for page in pages})
        with patch.object(termindex, "load_pages", side_effect=AssertionError("global text load forbidden")):
            result = SekaiSyncCore(self.root).term_penetrate("空港", languages=["en"])
        self.assertEqual(result["per_language"]["en"]["term"], "airport")

    def test_nonlexical_relations_do_not_publish_scalar_lexical_names(self):
        for kind in ("paraphrase", "reference", "omitted", "unresolved"):
            with self.subTest(kind=kind):
                self.root = Path(self.temp.name) / kind
                dbstore.initialize(self.root)
                dbstore.upsert_web_pages(self.root, "fixture", self.pages)
                item = self.task()
                self.submit(item, [self.proposal(item, kind)])
                entry = SekaiSyncCore(self.root).term_penetrate("空港", languages=["en"])["per_language"]["en"]
                self.assertEqual(entry["term"], "")
                self.assertTrue(entry["missing"])
                self.assertIn("airport", entry["sentence"])
                self.assertIn(kind, entry["note"])

    def test_two_senses_are_ambiguous_even_when_target_surface_agrees(self):
        item = self.task()
        self.submit(item, [self.proposal(item), self.proposal(item, sense="other")])
        self.assertIsNone(SekaiSyncCore(self.root).term_penetrate("空港", story_key="event:1:1", languages=["en"]))

    def test_stale_page_blocks_occurrence_and_does_not_mutate_history(self):
        item = self.task()
        self.submit(item, [self.proposal(item)])
        with dbstore.connect(self.root) as conn:
            conn.execute("UPDATE web_pages SET text='A: terminal.' WHERE language='en'")
            conn.commit()
        self.assertIsNone(SekaiSyncCore(self.root).term_penetrate("空港", languages=["en"]))
        with dbstore.connect(self.root) as conn:
            self.assertEqual(len(os._read_relations(conn, current_only=False)), 1)

    def test_precise_retirement_preserves_history_and_is_idempotent(self):
        item = self.task()
        self.submit(item, [self.proposal(item, "unresolved"), self.proposal(item)])
        with dbstore.connect(self.root) as conn:
            rows = os._read_relations(conn)
            old = next(row for row in rows if row["kind"] == "unresolved")
            new = next(row for row in rows if row["kind"] == "lexical")
            self.assertIsNone(SekaiSyncCore(self.root).term_penetrate("空港", languages=["en"]))
            replacement = dict(old_id=old["id"], new_id=new["id"], reason="Expanded review resolved this occurrence")
            self.assertEqual(os._retire_relations(conn, [replacement]), 1)
            self.assertEqual(os._retire_relations(conn, [replacement]), 0)
            self.assertEqual(os._read_relations(conn), [new])
            self.assertEqual(len(os._read_relations(conn, current_only=False)), 2)
            conn.commit()
        self.assertEqual(SekaiSyncCore(self.root).term_penetrate("空港", languages=["en"])["per_language"]["en"]["term"], "airport")

    def test_retirement_cannot_borrow_another_sense_and_rolls_back(self):
        item = self.task()
        self.submit(item, [self.proposal(item, "unresolved"), self.proposal(item, sense="other")])
        with dbstore.connect(self.root) as conn:
            rows = os._read_relations(conn)
            old = next(row for row in rows if row["kind"] == "unresolved")
            new = next(row for row in rows if row["kind"] == "lexical")
            with self.assertRaisesRegex(ValueError, "cannot borrow"):
                os._retire_relations(conn, [dict(old_id=old["id"], new_id=new["id"], reason="wrong sense")])
            self.assertEqual(len(os._read_relations(conn)), 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scraper_relation_retirements").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
