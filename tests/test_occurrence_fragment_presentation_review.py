"""Independent consumer checks for raw fragment notes, not semantic gold."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as packets, agent_review as review, dbstore
from sekaisync import occurrence_store as ledger, span_subjects
from sekaisync.core import SekaiSyncCore


class OccurrenceFragmentPresentationReviewTests(unittest.TestCase):
    label = "; raw selected fragments (Unicode code points, end-exclusive): "
    story = "event:904:1"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.source = dict(source="fixture", id="web:fixture:en:event_story:904:1", language="en",
                           kind="event_story", trust="B", text="A: \U0001f9edFirst observe, then draw.")
        self.target = dict(source="fixture", id="web:fixture:zh_hans:event_story:904:1", language="zh_hans",
                           kind="event_story", trust="B", text="B: \U0001f9ed\u5148\u89c2\u5bdf\uff0c\u518d\u4e0b\u7b14\u3002")
        self.persist()

    def persist(self):
        dbstore.upsert_web_pages(self.store, "fixture", [self.source, self.target])

    def view(self, page):
        return packets._raw_region(page, 0, len(page["text"]))

    def parts(self, view, fragments):
        parts, cursor = [], 0
        for exact in fragments:
            start = view["text"].index(exact, cursor)
            parts.append(dict(start=view["start"] + start,
                              end=view["start"] + start + len(exact), exact=exact))
            cursor = start + len(exact)
        return parts

    def relation(self, target_fragments=("\u5148", "\u518d")):
        source_view, target_view = self.view(self.source), self.view(self.target)
        self.source_parts = self.parts(source_view, ["First", "then"])
        self.subject = span_subjects._segmented(source_view, self.story, self.source_parts)
        self.query = self.subject["canonical"]
        row = dict(story_key=self.story, source=source_view, targets={"zh_hans": target_view})
        row["id"] = "span:" + packets._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["zh_hans"], windows=[row])
        scope_id = packets._digest(scope)
        review._write_json(packets._scope_path(self.store, scope_id), scope)
        context = dict(schema=packets._SCHEMA, task="occurrence", scope_id=scope_id,
                       source_language="en", subject=self.subject,
                       rows=[dict(id=row["id"], story_key=self.story,
                                  source=source_view, target=target_view)])
        item = packets._item(self.query, "zh_hans", [], "pending", context,
                             "Synthetic representation review, not semantic scoring")
        self.target_parts = self.parts(target_view, target_fragments)
        target_anchor = ledger._anchor(target_view, self.story, self.target_parts)
        sense = ledger._sense(self.subject["id"], "en", "ordered-stages", "Synthetic sequencing frame")
        proof = dict(scope_id=scope_id, row_id=row["id"], term=self.query, candidates=[], context=context)
        return ledger._relation(self.subject["source"], target_anchor, sense, "lexical", item.id,
                                "Synthetic raw representation judgment only", grounding=proof)

    def save(self, relation):
        with dbstore.connect(self.store) as conn:
            ledger._store_relations(conn, [relation])
            conn.commit()

    def consumers(self):
        core = SekaiSyncCore(self.store)
        with core.request_view():
            penetration = core.term_penetrate(self.query, story_key=self.story,
                                             languages=["en", "zh_hans"])
            generic = core.query(self.query, include_web=False)
        return penetration, generic

    def assert_fragment_entries(self, penetration, generic):
        self.assertIsNotNone(penetration)
        self.assertEqual(len(generic["terms"]), 1)
        self.assertEqual(penetration["term"]["names"], {})
        self.assertEqual(generic["terms"][0]["names"], {})
        positions = {entry["language"]: entry for entry in generic["terms"][0]["positions"]}
        for language, parts, page in (("en", self.source_parts, self.source),
                                      ("zh_hans", self.target_parts, self.target)):
            for entry in (penetration["per_language"][language], positions[language]):
                self.assertEqual(entry["term"], "")
                self.assertTrue(entry["missing"])
                self.assertEqual(json.loads(entry["note"].split(self.label, 1)[1]), parts)
                for part in parts:
                    self.assertEqual(page["text"][part["start"]:part["end"]], part["exact"])

    def test_fresh_exact_non_bmp_offsets_are_visible_without_scalar_or_global_names(self):
        self.save(self.relation())
        receipts = packets._receipts(self.store)
        self.assert_fragment_entries(*self.consumers())
        self.assertEqual(self.source_parts[0]["start"], 4)
        self.assertEqual(self.target_parts[0]["start"], 4)
        self.assertEqual(packets._receipts(self.store), receipts)
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_same_speaker_soft_wrap_preserves_slots_and_untouched_raw_fragments(self):
        self.source = dict(self.source, text="A: \U0001f9edFirst observe, \t\r\nthen draw.")
        self.target = dict(self.target, text="B: \U0001f9ed\u5148\u89c2\u5bdf\uff0c \t\r\n\u518d\u4e0b\u7b14\u3002")
        self.persist()
        self.save(self.relation())
        self.assert_fragment_entries(*self.consumers())
        gap = self.target["text"][self.target_parts[0]["end"]:self.target_parts[1]["start"]]
        self.assertEqual(gap, "\u89c2\u5bdf\uff0c \t\r\n")

    def test_cross_speaker_and_relabelled_turns_cannot_supply_display_fragments(self):
        target_texts = ("B: \u5148\u89c2\u5bdf\u3002\nC: \u518d\u4e0b\u7b14\u3002",
                        "B: \u5148\u89c2\u5bdf\u3002\r\nB: \u518d\u4e0b\u7b14\u3002",
                        "B\ufe55\u5148\u89c2\u5bdf\u3002\nB\ufe13\u518d\u4e0b\u7b14\u3002")
        for text in target_texts:
            with self.subTest(target=text):
                self.target = dict(self.target, text=text)
                self.persist()
                with self.assertRaises(ValueError):
                    self.save(self.relation())
                penetration, generic = self.consumers()
                self.assertIsNone(penetration)
                self.assertEqual(generic["terms"], [])
        for text in ("A: First observe.\nC: then draw.", "A: First observe.\r\nA: then draw."):
            with self.subTest(source=text):
                source = dict(self.source, text=text)
                view = self.view(source)
                with self.assertRaises(ValueError):
                    span_subjects._segmented(view, self.story, self.parts(view, ["First", "then"]))

    def test_changed_source_or_target_hash_suppresses_both_public_fragment_notes(self):
        self.save(self.relation())
        self.assert_fragment_entries(*self.consumers())
        source, target = self.source, self.target
        for side in ("source", "target"):
            with self.subTest(changed=side):
                self.source, self.target = source, target
                changed = getattr(self, side)
                setattr(self, side, dict(changed, text=changed["text"] + " Changed outside selected fragments."))
                self.persist()
                penetration, generic = self.consumers()
                self.assertIsNone(penetration)
                self.assertEqual(generic["terms"], [])
                with dbstore.connect(self.store) as conn:
                    self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)

    def test_genuine_contiguous_target_keeps_existing_scalar_contract(self):
        self.save(self.relation(target_fragments=("\u5148\u89c2\u5bdf\uff0c\u518d\u4e0b\u7b14",)))
        penetration, generic = self.consumers()
        self.assertEqual(penetration["term"]["names"], {})
        positions = {entry["language"]: entry for entry in generic["terms"][0]["positions"]}
        for entry in (penetration["per_language"]["zh_hans"], positions["zh_hans"]):
            self.assertEqual(entry["term"], self.target_parts[0]["exact"])
            self.assertFalse(entry.get("missing", False))
            self.assertNotIn(self.label, entry["note"])
        self.assertEqual(json.loads(penetration["per_language"]["en"]["note"].split(self.label, 1)[1]),
                         self.source_parts)


if __name__ == "__main__":
    unittest.main()
