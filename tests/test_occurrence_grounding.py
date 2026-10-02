"""Adversarial occurrence anchors must not borrow packet/same-story authority."""
import copy
import hashlib
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, dbstore, occurrence_store as ledger, termindex as ti


class OccurrenceGroundingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.pages = []
        for chapter in (1, 2):
            for language, text in (("zh_hans", "甲：今天有声音。\n乙：还讨论音乐。"),
                                   ("en", "A: Listen to sound.\nB: Let's discuss music.")):
                self.pages.append(dict(id=f"fixture:{language}:event_story:888:{chapter}", source="fixture",
                                       language=language, kind="event_story", trust="B", text=text,
                                       canonical_key=f"event_story:{language}:888:{chapter}"))
        dbstore.upsert_web_pages(self.store, "fixture", self.pages)
        groups = ti.group_pages_by_story(self.pages)
        items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), ["声音"], {}, "zh_hans", ["en"])
        translation = next(item for item in items if item.kind != "discovery")
        self.item = ap._occurrence_item(translation)
        self.row = self.item._context["rows"][0]
        self.conn = self.enterContext(dbstore.connect(self.store))

    def valid_relation(self):
        source = ap._body_term_segments(self.row["source"]["text"], "声音", self.row["source"]["start"])[0]
        target = ap._body_term_segments(self.row["target"]["text"], "sound", self.row["target"]["start"])[0]
        answer = ap._validate_answer(self.conn, self.store, self.item, dict(decision="accept", relations=[dict(
            evidence_id=self.row["id"], source_segments=source, target_segments=target,
            sense_key="audible_sound", sense_gloss="an audible sound in the quoted line", kind="lexical",
            rationale="Same localized dialogue position explicitly mentions sound.")]))
        return answer["relations"][0]

    def reseal(self, relation):
        relation["id"] = ledger._identity("rel:", {key: value for key, value in relation.items() if key != "id"})
        return relation

    def page_anchor(self, language, chapter, surface, story=None):
        page = next(page for page in self.pages if page["language"] == language and page["id"].endswith(f":{chapter}"))
        start = page["text"].index(surface)
        view = dict(page_id=page["id"], source=page["source"], language=language, start=0,
                    end=len(page["text"]), text=page["text"],
                    sha256=hashlib.sha256(page["text"].encode()).hexdigest())
        return ledger._anchor(view, story or f"event:888:{chapter}", [dict(start=start,
                              end=start + len(surface), exact=surface)])

    def test_real_packet_scope_is_reproduced_and_saved(self):
        relation = self.valid_relation()
        self.assertTrue(relation["structural_grounding"])
        self.assertFalse(relation["semantic_guarantee"])
        self.assertEqual(ledger._store_relations(self.conn, [relation]), 1)
        self.assertEqual(ledger._read_relations(self.conn), [relation])

    def test_plain_anchors_do_not_claim_structural_pair_grounding(self):
        relation = ledger._relation(self.page_anchor("zh_hans", 1, "声音"),
                                    self.page_anchor("en", 1, "sound"),
                                    ledger._sense("lex:fixture", "zh_hans", "sound", "audible sound"),
                                    "lexical", "fixture:unproven", "Raw spans alone are not an aligned packet.")
        self.assertFalse(relation["structural_grounding"])

    def test_retagging_a_different_story_page_cannot_borrow_source_story(self):
        relation = self.valid_relation()
        relation["target"] = self.page_anchor("en", 2, "sound", story="event:888:1")
        with self.assertRaisesRegex(ValueError, "content identity"):
            ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_target_in_another_turn_is_not_same_position_evidence(self):
        relation = self.valid_relation()
        relation["target"] = self.page_anchor("en", 1, "music")
        with self.assertRaisesRegex(ValueError, "selected local windows"):
            ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_unknown_scope_and_row_ids_are_rejected(self):
        for field, value in (("scope_id", "0" * 64), ("row_id", "span:unknown")):
            relation = self.valid_relation()
            relation["grounding"][field] = value
            with self.subTest(field=field), self.assertRaises((ValueError, OSError)):
                ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_unknown_review_item_is_not_accepted_as_packet_identity(self):
        relation = self.valid_relation()
        relation["review_item_id"] = "arp:" + "0" * 32
        with self.assertRaisesRegex(ValueError, "review item identity"):
            ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_rehashed_context_tampering_still_fails_against_scope_rows(self):
        relation = self.valid_relation()
        context = relation["grounding"]["context"]
        context["rows"][0]["target"]["text"] += "invented"
        proof = relation["grounding"]
        relation["review_item_id"] = "arp:" + ap._digest([proof["term"], relation["target_language"],
                                                          sorted(set(proof["candidates"])), context])[:32]
        with self.assertRaisesRegex(ValueError, "outside its work packet"):
            ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_proof_for_one_source_term_cannot_certify_another_source_segment(self):
        relation = self.valid_relation()
        source = copy.deepcopy(relation["source"])
        exact = "今天"
        source["segments"] = [dict(start=2, end=4, exact=exact)]
        source["id"] = ledger._identity("occ:", {key: value for key, value in source.items() if key != "id"})
        relation["source"] = source
        with self.assertRaises(ValueError):
            ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_source_packet_cannot_lend_its_proof_to_a_different_sense_subject(self):
        relation = self.valid_relation()
        relation["sense"] = ledger._sense("lex:another_subject", "zh_hans", "audible_sound", "an audible sound")
        with self.assertRaises(ValueError):
            ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_target_speaker_metadata_cannot_be_a_lexical_counterpart(self):
        relation = self.valid_relation()
        relation["target"] = self.page_anchor("en", 1, "A")
        with self.assertRaises(ValueError):
            ledger._store_relations(self.conn, [self.reseal(relation)])

    def test_current_page_version_is_checked_even_with_a_real_old_packet(self):
        relation = self.valid_relation()
        self.conn.execute("UPDATE web_pages SET text=text || ' changed' WHERE id=?", (relation["target"]["page_id"],))
        with self.assertRaisesRegex(ValueError, "version is stale"):
            ledger._store_relations(self.conn, [relation])


if __name__ == "__main__":
    unittest.main()
