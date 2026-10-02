"""Independent raw-softwrap projection and occurrence ambiguity checks."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger
from sekaisync import span_subjects, termindex as ti
from sekaisync.core import SekaiSyncCore


class OccurrenceSoftwrapProjectionReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.source = dict(source="fixture", id="web:fixture:en:event_story:999:1", language="en",
                           kind="event_story", trust="B", text="Alice: We were lending her a hand.")
        self.target = dict(source="fixture", id="web:fixture:ko:event_story:999:1", language="ko",
                           kind="event_story", trust="B", text="인물: \U0001f9ed도움을\r\n주었습니다.")
        dbstore.upsert_web_pages(self.store, "fixture", [self.source, self.target])
        view = ap._raw_region(self.source, 0, len(self.source["text"]))
        self.subject = span_subjects._segmented(view, "event:999:1", self.parts(view, ["lending", "a hand"]))
        self.query = self.subject["canonical"]

    def parts(self, view, fragments, cursor=0):
        result = []
        for exact in fragments:
            start = view["text"].index(exact, cursor)
            result.append(dict(start=view["start"] + start, end=view["start"] + start + len(exact), exact=exact))
            cursor = start + len(exact)
        return result

    def relation(self, fragments=("도움을", "주었습니다"), *, target=None, kind="lexical", sense_key="helping", cursor=0):
        target = target or self.target
        source_view = ap._raw_region(self.source, 0, len(self.source["text"]))
        target_view = ap._raw_region(target, 0, len(target["text"]))
        row = dict(story_key="event:999:1", source=source_view, targets={"ko": target_view})
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["ko"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(self.store, scope_id), scope)
        context = dict(schema=ap._SCHEMA, task="occurrence", scope_id=scope_id, source_language="en",
                       subject=self.subject, rows=[dict(id=row["id"], story_key=row["story_key"], source=source_view, target=target_view)])
        item = ap._item(self.query, "ko", [], "pending", context, "Synthetic raw representation fixture")
        anchor = ledger._anchor(target_view, row["story_key"], self.parts(target_view, fragments, cursor))
        sense = ledger._sense(self.subject["id"], "en", sense_key, "Synthetic contextual meaning " + sense_key)
        proof = dict(scope_id=scope_id, row_id=row["id"], term=self.query, candidates=[], context=context)
        return ledger._relation(self.subject["source"], anchor, sense, kind, item.id,
                                "Structural projection fixture only, not semantic gold", grounding=proof)

    def save(self, *relations):
        with dbstore.connect(self.store) as conn:
            ledger._store_relations(conn, list(relations))
            conn.commit()

    def scalar(self, relation, page=None):
        page = page or self.target
        return ti._occurrence_lexical_scalar(relation["target"], {(page["source"], page["id"]): page})

    def query_result(self):
        return SekaiSyncCore(self.store).query(self.query, include_web=False)

    def penetrate(self):
        return SekaiSyncCore(self.store).term_penetrate(self.query, story_key="event:999:1", languages=["en", "ko"])

    def test_raw_crlf_tab_and_non_bmp_softwrap_scalar_is_not_normalized_or_globally_named(self):
        self.target = dict(self.target, text="인물: \U0001f9ed도움을 \t\r\n주었습니다.")
        dbstore.upsert_web_pages(self.store, "fixture", [self.target])
        relation = self.relation()
        self.save(relation)
        expected = "도움을 \t\r\n주었습니다"
        self.assertEqual(self.scalar(relation), expected)
        before = ap._receipts(self.store)
        result = self.penetrate()
        self.assertEqual(result["per_language"]["ko"]["term"], expected)
        self.assertEqual(result["term"]["names"], {})
        self.assertEqual(result["per_language"]["en"]["term"], "")
        generic = self.query_result()["terms"][0]
        self.assertEqual(generic["names"], {})
        self.assertEqual(next(position for position in generic["positions"] if position["language"] == "ko")["term"], expected)
        self.assertEqual(ap._receipts(self.store), before)
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_one_envelope_and_two_raw_parts_for_whitespace_gap_do_not_false_conflict(self):
        split = self.relation()
        envelope = self.relation(["도움을\r\n주었습니다"])
        self.assertNotEqual(split["target"]["id"], envelope["target"]["id"])
        self.save(split, envelope)
        self.assertEqual(len(self.query_result()["terms"]), 1)
        self.assertEqual(self.penetrate()["per_language"]["ko"]["term"], "도움을\r\n주었습니다")
        with dbstore.connect(self.store) as conn:
            self.assertEqual(len(ledger._read_relations(conn)), 2)

    def test_non_whitespace_gap_never_becomes_a_scalar_or_equivalent_envelope(self):
        self.target = dict(self.target, text="인물: 도움을 많이 주었습니다.")
        dbstore.upsert_web_pages(self.store, "fixture", [self.target])
        split = self.relation()
        self.assertEqual(self.scalar(split), "")
        self.save(split)
        self.assertEqual(self.penetrate()["per_language"]["ko"]["term"], "")
        self.assertIn("lexical", self.penetrate()["per_language"]["ko"]["note"])
        envelope = self.relation(["도움을 많이 주었습니다"])
        self.save(envelope)
        self.assertIsNone(self.penetrate())
        self.assertEqual(self.query_result()["terms"], [])

    def test_stale_raw_hash_or_quote_suppresses_projection_and_public_consumption(self):
        relation = self.relation()
        self.save(relation)
        self.assertEqual(self.scalar(relation, dict(self.target, text=self.target["text"] + "변경。")), "")
        forged = deepcopy(relation)
        forged["target"]["page_sha256"] = "0" * 64
        self.assertEqual(self.scalar(forged), "")
        forged = deepcopy(relation)
        forged["target"]["segments"][0]["exact"] = "없는원문"
        self.assertEqual(self.scalar(forged), "")
        dbstore.upsert_web_pages(self.store, "fixture", [dict(self.target, text=self.target["text"] + "변경。")])
        self.assertIsNone(self.penetrate())
        self.assertEqual(self.query_result()["terms"], [])
        with dbstore.connect(self.store) as conn:
            self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)

    def test_cross_speaker_relabelled_and_bare_independent_lines_never_project(self):
        for text in ("인물: 도움을\n다른인물: 주었습니다.", "인물: 도움을\r\n인물: 주었습니다.",
                     "인물﹕도움을\n인물︓주었습니다.", "도움을\r\n주었습니다."):
            with self.subTest(text=text):
                target = dict(self.target, text=text)
                dbstore.upsert_web_pages(self.store, "fixture", [target])
                relation = self.relation(target=target)
                self.assertEqual(self.scalar(relation, target), "")
                with self.assertRaises(ValueError):
                    self.save(relation)
                with dbstore.connect(self.store) as conn:
                    self.assertEqual(ledger._read_relations(conn), [])

    def test_same_target_anchor_with_different_relation_kind_remains_ambiguous(self):
        lexical = self.relation()
        contextual = self.relation(kind="paraphrase")
        self.assertEqual(lexical["target"]["id"], contextual["target"]["id"])
        self.save(lexical, contextual)
        self.assertIsNone(self.penetrate())
        self.assertEqual(self.query_result()["terms"], [])

    def test_different_explicit_meanings_remain_separate_not_scalar_aliases(self):
        self.save(self.relation(sense_key="helping"), self.relation(sense_key="another-meaning"))
        self.assertIsNone(self.penetrate())
        records = self.query_result()["terms"]
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["id"] for record in records}), 2)
        self.assertTrue(all(record["names"] == {} for record in records))

    def test_equal_raw_text_on_different_target_pages_cannot_merge(self):
        alternate = dict(self.target, id="web:alternate:ko:event_story:999:1", source="alternate")
        dbstore.upsert_web_pages(self.store, "alternate", [alternate])
        first, second = self.relation(), self.relation(target=alternate)
        self.assertEqual(first["target"]["page_sha256"], second["target"]["page_sha256"])
        self.save(first, second)
        self.assertIsNone(self.penetrate())
        self.assertEqual(self.query_result()["terms"], [])

    def test_equal_raw_scalar_at_two_target_occurrences_keeps_offset_conflict(self):
        self.target = dict(self.target, text="인물: 도움을\r\n주었습니다. 다시 도움을\r\n주었습니다.")
        dbstore.upsert_web_pages(self.store, "fixture", [self.target])
        first = self.relation()
        second = self.relation(cursor=self.target["text"].index("도움을") + 1)
        self.assertEqual(self.scalar(first), self.scalar(second))
        self.save(first, second)
        self.assertIsNone(self.penetrate())
        self.assertEqual(self.query_result()["terms"], [])


if __name__ == "__main__":
    unittest.main()
