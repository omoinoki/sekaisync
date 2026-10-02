"""Typed source subjects remain scoped expressions through the old query API."""
from copy import deepcopy
import tempfile
from pathlib import Path
import unittest

from sekaisync import agent_packets as ap, dbstore, occurrence_store as ledger, span_subjects, termindex
from sekaisync.core import SekaiSyncCore


class OccurrenceSubjectQueryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.pages = [dict(source="fixture", id="fixture:en:event_story:777:1", language="en",
                           kind="event_story", trust="B", text="A: lending her a hand; build confidence."),
                      dict(source="fixture", id="fixture:ja:event_story:777:1", language="ja",
                           kind="event_story", trust="B", text="A: helping; build confidence.")]
        dbstore.upsert_web_pages(self.store, "fixture", self.pages)
        groups = termindex.group_pages_by_story(self.pages)
        items, _ = ap._prepare_scrub_review(self.store, groups, list(groups), ["lending"], {}, "en", ["ja"])
        self.translation = next(item for item in items if item._context["task"] == "translation")
        self.row = self.translation._context["rows"][0]

    def parts(self, view, fragments):
        parts, cursor = [], 0
        for fragment in fragments:
            start = view["text"].index(fragment, cursor)
            end = start + len(fragment)
            parts.append(dict(start=view["start"] + start, end=view["start"] + end, exact=fragment))
            cursor = end
        return parts

    def subject(self, kind="segmented", fragments=("lending", "a hand")):
        parts = self.parts(self.row["source"], fragments)
        if kind == "segmented":
            return span_subjects._segmented(self.row["source"], self.row["story_key"], parts)
        return span_subjects._literal(self.row["source"], self.row["story_key"], " ".join(fragments), parts)

    def relation(self, subject=None, target_parts=("helping",), kind="lexical"):
        subject = subject or self.subject()
        context = dict(self.translation._context, task="occurrence", subject=subject)
        item = ap._item(subject["canonical"], "ja", [], self.translation.kind, context, "Typed source review")
        target = ledger._anchor(self.row["target"], self.row["story_key"], self.parts(self.row["target"], target_parts))
        sense = ledger._sense(subject["id"], "en", "helping", "The contextual expression for providing assistance")
        proof = dict(scope_id=context["scope_id"], row_id=self.row["id"], term=item.term,
                     candidates=item.candidates, context=context)
        return ledger._relation(subject["source"], target, sense, kind, item.id,
                                "Same contextual expression in the paired original turn", grounding=proof)

    def save(self, *relations):
        with dbstore.connect(self.store) as conn:
            inserted = ledger._store_relations(conn, list(relations))
            conn.commit()
            return inserted

    def query(self, query="lending a hand", languages=("en", "ja")):
        return SekaiSyncCore(self.store).term_penetrate(query, story_key="event:777:1", languages=languages)

    def test_segmented_source_is_not_an_invented_continuous_name(self):
        self.assertEqual(self.save(self.relation()), 1)
        result = self.query()
        self.assertEqual(result["term"]["canonical"], "lending a hand")
        self.assertEqual(result["term"]["names"], {})
        source = result["per_language"]["en"]
        self.assertEqual(source["term"], "")
        self.assertTrue(source["missing"])
        self.assertIn("lending her a hand", source["sentence"])
        self.assertIn("segmented", source["note"])
        target = result["per_language"]["ja"]
        self.assertEqual(target["term"], "helping")
        self.assertNotIn("missing", target)
        self.assertEqual(set(result), {"query", "term", "story_key", "released", "per_language", "cloud_rank"})
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scraper_subjects").fetchone()[0], 1)

    def test_literal_typed_subject_keeps_exact_legacy_query_shape(self):
        subject = self.subject("literal", ("build", "confidence"))
        self.save(self.relation(subject, ("build confidence",)))
        result = self.query("build confidence")
        self.assertEqual(result["term"]["names"], {"en": "build confidence"})
        self.assertEqual(result["per_language"]["en"]["term"], "build confidence")
        self.assertEqual(result["per_language"]["ja"]["term"], "build confidence")

    def test_display_lookup_is_case_preserving_not_a_normalized_alias(self):
        self.save(self.relation())
        self.assertIsNone(self.query("Lending a hand"))
        self.assertIsNotNone(self.query("lending a hand"))

    def test_whitespace_fragmented_target_keeps_raw_scalar_without_source_alias(self):
        self.save(self.relation(target_parts=("build", "confidence")))
        result = self.query()
        target = result["per_language"]["ja"]
        self.assertEqual(target["term"], "build confidence")
        self.assertNotIn("missing", target)
        self.assertIn("build confidence", target["sentence"])
        self.assertEqual(result["term"]["names"], {})
        with dbstore.connect(self.store) as conn:
            self.assertEqual(len(ledger._read_relations(conn)[0]["target"]["segments"]), 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_literal_and_segmented_identical_display_cannot_merge(self):
        literal = self.subject("literal", ("build", "confidence"))
        segmented = self.subject("segmented", ("build", "confidence"))
        self.assertNotEqual(literal["id"], segmented["id"])
        self.save(self.relation(literal), self.relation(segmented))
        self.assertIsNone(self.query("build confidence"))
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scraper_subjects").fetchone()[0], 2)

    def test_stale_typed_subject_remains_known_and_history_is_not_deleted(self):
        self.save(self.relation())
        dbstore.upsert_web_pages(self.store, "fixture", [dict(self.pages[0], text=self.pages[0]["text"] + " changed")])
        self.assertIsNone(self.query())
        with dbstore.connect(self.store) as conn:
            known, current = ledger._query_relations(conn, "lending a hand")
            self.assertTrue(known)
            self.assertEqual(current, [])
            self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)

    def reseal(self, relation):
        proof = relation["grounding"]
        relation["review_item_id"] = "arp:" + ap._digest([proof["term"], relation["target_language"],
                                                           sorted(set(proof["candidates"])), proof["context"]])[:32]
        relation["id"] = ledger._identity("rel:", {key: value for key, value in relation.items() if key != "id"})
        return relation

    def test_resealed_fabricated_subject_cannot_borrow_real_scope(self):
        relation = self.relation()
        relation["grounding"]["context"]["subject"]["canonical"] = "lend a hand"
        relation["grounding"]["term"] = "lend a hand"
        with dbstore.connect(self.store) as conn, self.assertRaises(ValueError):
            ledger._store_relations(conn, [self.reseal(relation)])

    def test_invalid_second_relation_writes_no_partial_subject_or_relation(self):
        valid, invalid = self.relation(), deepcopy(self.relation())
        invalid["grounding"]["context"]["subject"]["gap_text"] = ["fabricated"]
        with dbstore.connect(self.store) as conn:
            with self.assertRaises(ValueError):
                ledger._store_relations(conn, [valid, self.reseal(invalid)])
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_relations'").fetchone())
            self.assertFalse(conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_subjects'").fetchone())


if __name__ == "__main__":
    unittest.main()
