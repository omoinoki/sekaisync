import copy
from contextlib import ExitStack
import hashlib
from pathlib import Path
import tempfile
import unittest

from sekaisync import dbstore, occurrence_store as os


class OccurrenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        dbstore.initialize(self.root)
        self.pages = []
        for story in (1, 2):
            for lang, text in (("en", "A: My sound and their sound. Take it back."),
                               ("ja", "A: My music and their voice. Return it.")):
                self.pages.append(dict(id=f"web:fixture:{lang}:event_story:1:{story}", source="fixture", language=lang,
                                       trust="B", kind="event_story", text=text, story_key=f"event:1:{story}"))
        dbstore.upsert_web_pages(self.root, "fixture", self.pages)
        contexts = ExitStack()
        self.addCleanup(contexts.close)
        self.conn = contexts.enter_context(dbstore.connect(self.root))

    def anchor(self, lang, story, surface, last=False):
        page = next(page for page in self.pages if page["id"] == f"web:fixture:{lang}:event_story:1:{story}")
        text = page["text"]
        start = text.rindex(surface) if last else text.index(surface)
        view = dict(page_id=page["id"], source="fixture", language=lang, start=0,
                    end=len(text), text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
        return os._anchor(view, f"event:1:{story}", [dict(start=start, end=start + len(surface), exact=surface)])

    def relation(self, story=1, key="opinion", kind="lexical"):
        source = self.anchor("en", story, "sound", last=True)
        target = self.anchor("ja", story, "voice")
        sense = os._sense("legacy:en:sound", "en", key, "opinion expressed by people" if key == "opinion" else "musical sound")
        return os._relation(source, target, sense, kind, f"fixture:{story}", "Reviewed contextual correspondence")

    def test_repeat_surfaces_and_senses_have_distinct_identity(self):
        first, second = self.anchor("en", 1, "sound"), self.anchor("en", 1, "sound", last=True)
        self.assertNotEqual(first["id"], second["id"])
        self.assertNotEqual(self.relation(key="music")["sense"]["id"], self.relation()["sense"]["id"])

    def test_raw_code_points_nfkc_length_and_case_preserved(self):
        text = "A: \U0001f3b5 \ufb01 IT it e\u0301"
        view = dict(page_id="raw", source="fixture", language="en", start=0,
                    end=len(text), text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
        anchor = os._anchor(view, "raw:1", [dict(start=5, end=6, exact="\ufb01")])
        self.assertEqual(anchor["segments"][0]["exact"], "\ufb01")
        upper = os._sense("legacy:it", "en", "IT", "information technology")
        lower = os._sense("legacy:it", "en", "it", "pronoun")
        self.assertNotEqual(upper["id"], lower["id"])

    def test_singleton_and_paraphrase_stored_without_global_projection(self):
        row = self.relation(kind="paraphrase")
        self.assertEqual(os._store_relations(self.conn, [row]), 1)
        self.assertIsNone(os._projection_candidate([row]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], "1")

    def test_same_sense_two_stories_projection_and_mixed_sense_rejected(self):
        rows = [self.relation(story=story) for story in (1, 2)]
        self.assertEqual(os._projection_candidate(rows), "voice")
        self.assertIsNone(os._projection_candidate([rows[0], self.relation(story=2, key="music")]))

    def test_omission_keeps_target_context_separate(self):
        row = self.relation(kind="omitted")
        self.assertEqual(row["target_role"], "context")
        self.assertEqual(os._store_relations(self.conn, [row]), 1)
        self.assertIsNone(os._projection_candidate([row, self.relation(story=2, kind="omitted")]))

    def test_discontinuous_expression_remains_two_spans(self):
        source = self.anchor("en", 1, "Take")
        page = next(page for page in self.pages if page["id"] == "web:fixture:en:event_story:1:1")
        text = page["text"]
        segments = [source["segments"][0], dict(start=text.index("back"), end=text.index("back") + 4, exact="back")]
        view = dict(page_id=page["id"], source="fixture", language="en", start=0,
                    end=len(text), text=text, sha256=hashlib.sha256(text.encode()).hexdigest())
        anchor = os._anchor(view, "event:1:1", segments)
        self.assertEqual([part["exact"] for part in anchor["segments"]], ["Take", "back"])
        os._validate_anchor(self.conn, anchor, {})

    def test_wrong_offset_hash_or_injected_semantic_guarantee_fails(self):
        original = self.relation()
        for mutate in (lambda row: row["target"]["segments"][0].update(start=0),
                       lambda row: row["target"].update(page_sha256="0" * 64),
                       lambda row: row.update(semantic_guarantee=True)):
            row = copy.deepcopy(original)
            mutate(row)
            with self.assertRaises(ValueError):
                os._store_relations(self.conn, [row])

    def test_replay_is_idempotent_and_old_versions_not_returned_current(self):
        row = self.relation()
        self.assertEqual(os._store_relations(self.conn, [row]), 1)
        self.assertEqual(os._store_relations(self.conn, [row]), 0)
        self.assertEqual(os._read_relations(self.conn), [row])
        self.conn.execute("UPDATE web_pages SET text=replace(text,'voice','noise') WHERE id='web:fixture:ja:event_story:1:1'")
        self.assertEqual(os._read_relations(self.conn), [])
        self.assertEqual(os._read_relations(self.conn, current_only=False), [row])

    def test_read_no_ledger_does_not_create_tables(self):
        self.assertEqual(os._read_relations(self.conn), [])
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_anchors'").fetchone())

    def test_invalid_batch_writes_nothing(self):
        good, bad = self.relation(), self.relation(story=2)
        bad["kind"] = "invented"
        with self.assertRaises(ValueError):
            os._store_relations(self.conn, [good, bad])
        self.assertFalse(self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_anchors'").fetchone())

    def test_transaction_rollback_removes_ledger_and_records(self):
        self.conn.execute("SAVEPOINT occurrence")
        os._store_relations(self.conn, [self.relation()])
        self.conn.execute("ROLLBACK TO occurrence")
        self.conn.execute("RELEASE occurrence")
        self.assertEqual(os._read_relations(self.conn), [])


if __name__ == "__main__":
    unittest.main()
