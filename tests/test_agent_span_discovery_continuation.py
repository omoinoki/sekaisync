"""Typed discovery budgets resume by exact subject IDs, not display strings."""
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as os


class SpanDiscoveryContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, count=215, version=1, segmented=False):
        store = self.base / f"store-{count}-{version}-{segmented}"
        dbstore.initialize(store)
        if version > 1:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                 backup_path=self.base / f"backup-{count}-{version}-{segmented}.db")
        source_text = ("Person:take this rumor into account." if segmented else
                       "Person:" + " ".join(f"s{index:03}" for index in range(count)))
        pages = [dict(id="web:fixture:en:event_story:1:1", source="fixture", language="en", trust="B",
                      kind="event_story", text=source_text),
                 dict(id="web:fixture:ja:event_story:1:1", source="fixture", language="ja", trust="B",
                      kind="event_story", text="Person:No relevant expression.\nPerson:Consider this report."),
                 dict(id="web:fixture:ko:event_story:1:1", source="fixture", language="ko", trust="B",
                      kind="event_story", text="Person:Consider this report.")]
        dbstore.upsert_web_pages(store, "fixture", pages)
        views = [ap._view(page, [0], ap._lines(page)[1]) for page in pages]
        row = dict(story_key="event:1:1", source=views[0], targets={"ja": views[1], "ko": views[2]},
                   search_text=source_text.casefold(), search_unwrapped=source_text.casefold())
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["ja", "ko"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id=scope_id, source_language="en", rows=[row])
        discovery = ap._item("@discover:event:1:1:" + row["id"], "en", [], "discovery", context, "Fixture")
        ar.enqueue(store, [discovery])
        return store, discovery

    def entries(self, discovery, start, end):
        row = discovery._context["rows"][0]
        return [dict(kind="literal", evidence_id=row["id"], canonical=f"s{index:03}",
                     segments=ap._body_term_segments(row["source"]["text"], f"s{index:03}",
                                                     row["source"]["start"])[0]) for index in range(start, end)]

    def submit(self, store, item, **answer):
        return ar.submit_judgments(store, [dict(id=item.id, decision="accept", **answer)])

    def test_two_hundred_typed_subjects_plus_fifteen_resume_v1_and_v2(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, discovery = self.fixture(version=version)
                first = self.submit(store, discovery, subjects=self.entries(discovery, 0, 200))
                self.assertEqual(first["errors"], [])
                self.assertEqual(first["followup_packets"], 401)
                child = next(item for item in ar.load_queue(store) if item._context["task"] == "discovery")
                self.assertEqual(child._context["continuation"]["excluded_terms"], [])
                self.assertEqual(len(child._context["continuation"]["excluded_subject_ids"]), 200)
                repeated = self.submit(store, child, terms=[], subjects=self.entries(child, 199, 200))
                self.assertTrue(repeated["errors"])
                self.assertEqual(self.submit(store, child, subjects=self.entries(child, 200, 215))["errors"], [])
                pending = ar.load_queue(store)
                audits = [item for item in pending if item._context.get("source_boundary_audit")]
                occurrences = [item for item in pending if item._context["task"] == "occurrence"]
                self.assertEqual(len(pending), 431)
                self.assertEqual(len(audits), 1)
                self.assertEqual(len(audits[0]._context["source_boundary_audit"]["excluded_subject_ids"]), 215)
                self.assertEqual(len(occurrences), 430)
                self.assertEqual(len({item._context["subject"]["id"] for item in occurrences}), 215)
                with dbstore.connect(store) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_subject_exclusions_cannot_be_forged_by_rehashing_the_child(self):
        store, discovery = self.fixture(count=200)
        self.assertEqual(self.submit(store, discovery, terms=[], subjects=self.entries(discovery, 0, 200))["errors"], [])
        child = next(item for item in ar.load_queue(store) if item._context["task"] == "discovery")
        context = dict(child._context, continuation=dict(child._context["continuation"], excluded_subject_ids=[]))
        forged = ap._item(child.term, child.language, [], "discovery", context, "Forged")
        ar.enqueue(store, [forged])
        self.assertTrue(self.submit(store, forged, terms=[], subjects=[])["errors"])
        self.assertEqual(self.submit(store, child, subjects=[])["errors"], [])
        self.assertFalse(any(item._context["task"] == "discovery" and item.id == child.id
                             for item in ar.load_queue(store)))

    def test_oversized_subject_budget_is_atomic_and_parent_remains_queued(self):
        store, discovery = self.fixture()
        self.assertTrue(self.submit(store, discovery, subjects=self.entries(discovery, 0, 201))["errors"])
        self.assertEqual([item.id for item in ar.load_queue(store)], [discovery.id])
        self.assertNotIn(discovery.id, ap._receipts(store))

    def test_segmented_subject_survives_expansion_and_missing_language_cohesion(self):
        store, discovery = self.fixture(segmented=True)
        row = discovery._context["rows"][0]
        view = row["source"]
        fragments = [dict(start=view["text"].index(part), end=view["text"].index(part) + len(part), exact=part)
                     for part in ("take", "into account")]
        self.assertEqual(self.submit(store, discovery, subjects=[dict(kind="segmented", evidence_id=row["id"],
                                                                     segments=fragments)])["errors"], [])
        item = next(entry for entry in ar.load_queue(store) if entry.language == "ja")
        subject = item._context["subject"]
        self.assertEqual(subject["gap_text"], [" this rumor "])
        self.assertFalse(ap._term_selection(view, item.term, fragments, case_sensitive=True))

        def judgment(packet, kind):
            evidence = packet._context["rows"][0]
            target = evidence["target"]
            segments = (ap._body_term_segments(target["text"], "Consider", target["start"])[0]
                        if kind == "lexical" else
                        [dict(start=target["start"], end=target["end"], exact=target["text"])])
            return [dict(evidence_id=evidence["id"], source_segments=subject["source"]["segments"],
                         target_segments=segments, kind=kind, sense_key="consider", sense_gloss="take into consideration",
                         rationale="This exact segmented subject conveys taking something into consideration")]

        self.assertEqual(self.submit(store, item, relations=judgment(item, "unresolved"))["errors"], [])
        expansion = next(entry for entry in ar.load_queue(store) if entry._context.get("expansion"))
        self.assertEqual(expansion._context["subject"], subject)
        self.assertEqual(self.submit(store, expansion, relations=judgment(expansion, "lexical"))["errors"], [])
        # Simulate interrupted queue publication, then recover the unjudged language.
        ar._write_json(ar.queue_path(store), {"items": []})
        self.assertEqual(ap.ensure_occurrence_cohesion(store)["added"], 1)
        focused = ar.load_queue(store)[0]
        self.assertEqual(focused.language, "ko")
        self.assertEqual(focused._context["subject"], subject)
        self.assertEqual(focused._context["focus"]["sense"]["term_id"], subject["id"])
        self.assertEqual(self.submit(store, focused, relations=judgment(focused, "lexical"))["errors"], [])
        with dbstore.connect(store) as conn:
            self.assertEqual(len(os._read_relations(conn)), 2)
            self.assertTrue(all(relation["sense"]["term_id"] == subject["id"] for relation in os._read_relations(conn)))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
