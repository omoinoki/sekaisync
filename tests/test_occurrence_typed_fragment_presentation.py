"""Expose reviewed non-scalar expressions without changing their relation kind."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as packets, agent_review as review, dbstore
from sekaisync import occurrence_store as ledger, termindex
from sekaisync.core import SekaiSyncCore


class TypedFragmentPresentationTests(unittest.TestCase):
    label = "; raw selected fragments (Unicode code points, end-exclusive): "
    source_term = "considerate response"
    story = "event:905:1"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.sequence = 0

    @staticmethod
    def parts(view, fragments):
        result, cursor = [], 0
        for exact in fragments:
            start = view["text"].index(exact, cursor)
            cursor = start + len(exact)
            result.append(dict(start=view["start"] + start, end=view["start"] + cursor, exact=exact))
        return result

    def fixture(self, version, kind, fragments):
        self.sequence += 1
        store = self.base / str(self.sequence)
        dbstore.initialize(store)
        for target in range(2, version + 1):
            dbstore.migrate_store(store, target_version=target, dry_run=False,
                                 backup_path=self.base / f"backup-{self.sequence}-{target}.db")
        pages = [dict(source="fixture", id=f"web:fixture:{language}:event_story:905:1",
                      language=language, kind="event_story", trust="B", text=text)
                 for language, text in (
                     ("en", "A: That was a " + self.source_term + "."),
                     ("zh_hans", "B: \U0001f9ed\u4ed6\u4eec\u6e29\u67d4\u5730\u5904\u7406\u4e86\u6b64\u4e8b\u3002"))]
        dbstore.upsert_web_pages(store, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        items, _ = packets._prepare_scrub_review(
            store, groups, list(groups), [self.source_term], {}, "en", ["zh_hans"])
        item = packets._occurrence_item(next(item for item in items
                                            if item._context["task"] == "translation"))
        review.enqueue(store, [item])
        row = item._context["rows"][0]
        target_parts = self.parts(row["target"], fragments)
        answer = dict(id=item.id, decision="accept", relations=[dict(
            evidence_id=row["id"], source_segments=self.parts(row["source"], [self.source_term]),
            target_segments=target_parts, sense_key="response", sense_gloss="Synthetic selected response meaning",
            kind=kind, rationale="Synthetic transport fixture; not a semantic gold judgment")])
        result = review.submit_judgments(store, [answer])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(review.submit_judgments(store, [answer])["accepted"], 0)
        return store, pages, target_parts

    def consumers(self, store):
        core = SekaiSyncCore(store)
        with core.request_view():
            penetration = core.term_penetrate(self.source_term, story_key=self.story,
                                             languages=["en", "zh_hans", "ko"])
            generic = core.query(self.source_term, include_web=False)
        self.assertIsNotNone(penetration)
        self.assertEqual(len(generic["terms"]), 1)
        positions = {position["language"]: position for position in generic["terms"][0]["positions"]}
        return penetration, generic, (penetration["per_language"]["zh_hans"], positions["zh_hans"])

    def test_paraphrase_and_reference_retain_exact_fragments_in_both_consumers(self):
        for version in (1, 2, 3):
            for kind, fragments in (("paraphrase", ["\u6e29\u67d4\u5730", "\u5904\u7406\u4e86"]),
                                    ("reference", ["\u6b64\u4e8b"])):
                with self.subTest(version=version, kind=kind):
                    store, pages, parts = self.fixture(version, kind, fragments)
                    receipts = packets._receipts(store)
                    penetration, generic, entries = self.consumers(store)
                    for entry in entries:
                        self.assertEqual(entry["term"], "")
                        self.assertTrue(entry["missing"])
                        self.assertIn("occurrence relation: " + kind + "; no scalar", entry["note"])
                        self.assertEqual(json.loads(entry["note"].split(self.label, 1)[1]), parts)
                        for part in parts:
                            self.assertEqual(pages[1]["text"][part["start"]:part["end"]], part["exact"])
                    self.assertEqual(penetration["term"]["names"], {"en": self.source_term})
                    self.assertEqual(generic["terms"][0]["names"], {"en": self.source_term})
                    self.assertNotIn(self.label, penetration["per_language"]["ko"]["note"])
                    self.assertEqual(packets._receipts(store), receipts)
                    with dbstore.connect(store) as conn:
                        relation, = ledger._read_relations(conn)
                        self.assertEqual(relation["kind"], kind)
                        self.assertEqual(relation["target"]["segments"], parts)
                        self.assertFalse(relation["semantic_guarantee"])
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_examined_context_for_nonrealization_is_not_presented_as_selected_expression(self):
        for kind in ("omitted", "unresolved"):
            with self.subTest(kind=kind):
                store, _, _ = self.fixture(1, kind, ["\u4ed6\u4eec\u6e29\u67d4\u5730\u5904\u7406\u4e86\u6b64\u4e8b"])
                _, _, entries = self.consumers(store)
                for entry in entries:
                    self.assertIn("occurrence relation: " + kind, entry["note"])
                    self.assertNotIn(self.label, entry["note"])
                    self.assertEqual(entry["term"], "")

    def test_lexical_scalar_retains_existing_presentation(self):
        exact = "\u6e29\u67d4\u5730\u5904\u7406\u4e86"
        store, _, _ = self.fixture(1, "lexical", [exact])
        _, _, entries = self.consumers(store)
        for entry in entries:
            self.assertEqual(entry["term"], exact)
            self.assertFalse(entry.get("missing", False))
            self.assertEqual(entry["note"], "occurrence-scoped lexical correspondence")

    def test_stale_page_cannot_leak_reviewed_fragments(self):
        store, pages, _ = self.fixture(1, "paraphrase", ["\u6e29\u67d4\u5730", "\u5904\u7406\u4e86"])
        self.consumers(store)
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=text || ' changed' WHERE source=? AND id=?",
                         (pages[1]["source"], pages[1]["id"]))
            conn.commit()
            self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)
            self.assertEqual(ledger._read_relations(conn), [])
        core = SekaiSyncCore(store)
        self.assertIsNone(core.term_penetrate(self.source_term, story_key=self.story))
        self.assertEqual(core.query(self.source_term, include_web=False)["terms"], [])


if __name__ == "__main__":
    unittest.main()
