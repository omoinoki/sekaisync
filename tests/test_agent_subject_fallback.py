"""Missing aligned evidence becomes honest raw-page work or named debt."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as os, termindex


class SubjectFallbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, version=1, target=True, incomplete_source=False, incomplete_target=False, large=False, multi=False):
        store = self.base / f"store-{version}-{target}-{incomplete_source}-{incomplete_target}-{large}-{multi}"
        dbstore.initialize(store)
        if version > 1:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                 backup_path=self.base / f"backup-{version}-{target}-{incomplete_source}-{incomplete_target}-{large}.db")
        pages = [dict(id="web:fixture:en:event_story:1:1", source="fixture", language="en", trust="B",
                      kind="event_story", text="Person:take this rumor into account.\nPerson:An unrelated later turn.")]
        if target:
            pages.append(dict(id="web:fixture:zh_hans:event_story:1:1", source="fixture", language="zh_hans", trust="B",
                              kind="event_story", text="\u7532\uff1a\u9700\u8981\u8003\u8651\u8fd9\u4ef6\u4e8b\u3002\n" +
                              ("\u7532\uff1a" + "\u5176\u4ed6\u8bdd\u9898\u3002" * 6000 if large else "\u7532\uff1a\u63a5\u4e0b\u6765\u8bf4\u522b\u7684\u8bdd\u9898\u3002")))
        if multi:
            pages.extend([
                dict(id="web:fixture:zh_tw:event_story:1:1", source="fixture", language="zh_tw", trust="B",
                     kind="event_story", text="\u7532\uff1a\u9700\u8981\u8003\u616e\u9019\u4ef6\u4e8b\u3002"),
                dict(id="web:fixture:ko:event_story:1:1", source="fixture", language="ko", trust="B",
                     kind="event_story", text="\uac11: \uc774 \uc77c\uc744 \uace0\ub824\ud574\uc57c \ud574."),
            ])
        dbstore.upsert_web_pages(store, "fixture", pages)
        source = ap._view(pages[0], [0], ap._lines(pages[0])[1])
        source["complete"] = not incomplete_source
        targets = {}
        if incomplete_target:
            target_view = ap._view(pages[1], [0], ap._lines(pages[1])[1])
            targets["zh_hans"] = dict(target_view, complete=False)
        row = dict(story_key="event:1:1", source=source, targets=targets,
                   search_text=source["text"].casefold(), search_unwrapped=source["text"].casefold())
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["zh_hans", "zh_tw", "ko"] if multi else ["zh_hans"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id=scope_id, source_language="en", rows=[row])
        discovery = ap._item("@discover:event:1:1:" + row["id"], "en", [], "discovery", context, "Fixture")
        ar.enqueue(store, [discovery])
        parts = [dict(start=source["text"].index(part), end=source["text"].index(part)+len(part), exact=part)
                 for part in ("take", "into account")]
        entry = dict(kind="segmented", evidence_id=row["id"], segments=parts)
        return store, discovery, entry

    def discover(self, store, discovery, entry):
        result = ar.submit_judgments(store, [dict(id=discovery.id, decision="accept", subjects=[entry])])
        self.assertEqual(result["errors"], [])
        pending = ar.load_queue(store)
        self.assertTrue(all(item._context["task"] in {"discovery", "occurrence", "subject_gap"}
                            for item in pending))
        audits = [item for item in pending if item._context["task"] == "discovery"]
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        marker = audit._context["source_boundary_audit"]
        self.assertEqual(marker["stage"], "review")
        self.assertEqual(marker["terminal_parent_id"], discovery.id)
        self.assertEqual(len(marker["inherited_subjects"]), 1)
        self.assertEqual(audit._context["rows"], discovery._context["rows"])
        target_ids = [item.id for item in pending if item.id != audit.id]
        settled = ar.submit_judgments(store, [dict(id=audit.id, decision="accept", terms=[])])
        self.assertEqual(settled["errors"], [])
        self.assertEqual(settled["accepted"], 1)
        remaining = ar.load_queue(store)
        self.assertEqual([item.id for item in remaining], target_ids)
        return remaining

    def judgment(self, item, kind="lexical"):
        row = item._context["rows"][0]
        target = row["target"]
        surface = {"zh_hans": "\u8003\u8651", "zh_tw": "\u8003\u616e", "ko": "\uace0\ub824\ud574\uc57c"}[item.language]
        segments = (ap._body_term_segments(target["text"], surface, target["start"])[0]
                    if kind == "lexical" else
                    [dict(start=target["start"], end=target["end"], exact=target["text"])])
        return dict(id=item.id, decision="accept", relations=[dict(evidence_id=row["id"],
                    source_segments=item._context["subject"]["source"]["segments"], target_segments=segments,
                    sense_key="consider", sense_gloss="take into consideration", kind=kind,
                    rationale="The exact raw subject corresponds to considering this matter in the same localized content")])

    def test_missing_alignment_exports_true_full_page_and_accepts_scoped_expression(self):
        store, discovery, entry = self.fixture()
        pending = self.discover(store, discovery, entry)
        self.assertEqual(len(pending), 1)
        child = pending[0]
        self.assertEqual(child._context["fallback"]["evidence_relation"], "same_content_unaligned")
        self.assertEqual(child._context["fallback"]["target_level"], "full_page")
        self.assertNotEqual(child._context["scope_id"], discovery._context["scope_id"])
        original = discovery._context["rows"][0]["source"]
        self.assertEqual(child._context["subject"]["window"], dict(start=original["start"], end=original["end"]))
        self.assertIn("fallback_contract", ar.render_item(child))
        result = ar.submit_judgments(store, [self.judgment(child)])
        self.assertEqual(result["errors"], [])
        with dbstore.connect(store) as conn:
            relation = os._read_relations(conn)[0]
            self.assertEqual(relation["sense"]["term_id"], child._context["subject"]["id"])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_incomplete_source_or_target_never_lowers_the_original_complete_flag(self):
        for source, target in ((True, False), (False, True), (True, True)):
            with self.subTest(source=source, target=target):
                store, discovery, entry = self.fixture(incomplete_source=source, incomplete_target=target)
                child = self.discover(store, discovery, entry)[0]
                old = ap._read_scope(store, discovery._context["scope_id"])
                self.assertEqual(old["windows"][0]["source"]["complete"], not source)
                self.assertTrue(child._context["rows"][0]["source"]["complete"])
                self.assertTrue(child._context["rows"][0]["target"]["complete"])
                self.assertEqual(ar.submit_judgments(store, [self.judgment(child)])["errors"], [])

    def test_missing_page_is_named_pending_debt_and_export_recovers_v1_v2(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, discovery, entry = self.fixture(version=version, target=False)
                debt = self.discover(store, discovery, entry)[0]
                self.assertEqual(debt._context["task"], "subject_gap")
                self.assertEqual(debt._context["gap"]["reason"], "missing_usable_localized_page")
                self.assertIn("gap_contract", ar.render_item(debt))
                self.assertTrue(ar.submit_judgments(store, [dict(id=debt.id, decision="accept", terms=[])])["errors"])
                dbstore.upsert_web_pages(store, "fixture", [dict(id="web:fixture:zh_hans:event_story:1:1",
                    source="fixture", language="zh_hans", trust="B", kind="event_story", text="\u7532\uff1a\u9700\u8981\u8003\u8651\u8fd9\u4ef6\u4e8b\u3002")])
                ar.export_for_agent(store, self.base / f"recovered-{version}.txt")
                pending = ar.load_queue(store)
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0]._context["task"], "occurrence")
                self.assertEqual(pending[0]._context["subject"], debt._context["subject"])
                self.assertEqual(ap.ensure_subject_gaps(store)["added"], 0)

    def test_oversized_target_preserves_unseen_debt_and_cannot_accept_omission(self):
        store, discovery, entry = self.fixture(large=True)
        pending = self.discover(store, discovery, entry)
        self.assertEqual(len(pending), 2)
        child = next(item for item in pending if item._context["task"] == "occurrence")
        debt = next(item for item in pending if item._context["task"] == "subject_gap")
        self.assertFalse(child._context["fallback"]["target_complete_page"])
        self.assertEqual(child._context["rows"][0]["target"]["end"], ap._EXPANSION_CHARS)
        self.assertEqual(debt._context["gap"]["unshown_target_regions"][0]["start"], ap._EXPANSION_CHARS)
        self.assertTrue(ar.submit_judgments(store, [self.judgment(child, "omitted")])["errors"])
        self.assertEqual(ar.submit_judgments(store, [self.judgment(child, "unresolved")])["errors"], [])
        self.assertTrue(any(item.id == debt.id for item in ar.load_queue(store)))
        self.assertEqual(ap.ensure_subject_gaps(store)["retired"], 0)

    def test_fallback_partial_context_cannot_certify_even_uncertainty(self):
        store, discovery, entry = self.fixture()
        child = self.discover(store, discovery, entry)[0]
        judgment = self.judgment(child, "unresolved")
        target = child._context["rows"][0]["target"]
        judgment["relations"][0]["target_segments"] = [dict(start=target["start"], end=target["start"]+1,
                                                          exact=target["text"][:1])]
        self.assertTrue(ar.submit_judgments(store, [judgment])["errors"])
        self.assertEqual([item.id for item in ar.load_queue(store)], [child.id])

    def test_current_page_policy_rejects_unusable_or_other_content_targets(self):
        store, discovery, entry = self.fixture(target=False)
        dbstore.upsert_web_pages(store, "fixture", [
            dict(id="web:fixture:zh_hans:event_story:1:1", source="fixture", language="zh_hans", trust="D",
                 kind="event_story", text="\u7532\uff1a\u9700\u8981\u8003\u8651\u8fd9\u4ef6\u4e8b\u3002"),
            dict(id="web:fixture:zh_hans:event_story:2:1", source="fixture", language="zh_hans", trust="B",
                 kind="event_story", text="\u7532\uff1a\u9700\u8981\u8003\u8651\u8fd9\u4ef6\u4e8b\u3002")])
        pending = self.discover(store, discovery, entry)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]._context["task"], "subject_gap")

    def test_target_language_cannot_escape_the_original_scope(self):
        store, discovery, entry = self.fixture()
        child = self.discover(store, discovery, entry)[0]
        with dbstore.connect(store) as conn:
            with self.assertRaisesRegex(ValueError, "outside its original"):
                ap._subject_fallback_item(conn, store, discovery._context["scope_id"], child._context["subject"], "ko")

    def test_rendered_contracts_belong_to_their_task_types(self):
        store, discovery, entry = self.fixture()
        rendered = ar.render_item(discovery)
        self.assertIn("discovery_contract:", rendered)
        self.assertIn("discovery_passes:", rendered)
        self.assertNotIn("gap_contract:", rendered)
        self.assertNotIn("answer_contract:", rendered)
        occurrence = self.discover(store, discovery, entry)[0]
        rendered = ar.render_item(occurrence)
        self.assertIn("answer_contract:", rendered)
        self.assertIn("fallback_contract:", rendered)
        self.assertNotIn("discovery_passes:", rendered)
        store, discovery, entry = self.fixture(target=False)
        debt = self.discover(store, discovery, entry)[0]
        rendered = ar.render_item(debt)
        self.assertIn("gap_contract:", rendered)
        self.assertNotIn("discovery_contract:", rendered)
        self.assertNotIn("discovery_passes:", rendered)
        self.assertNotIn("answer_contract:", rendered)

    def test_story_candidate_sql_is_a_superset_of_authoritative_grouping(self):
        store = self.base / "candidate-store"
        dbstore.initialize(store)
        pages = [
            dict(id="url-event", kind="event_story", url="https://example.test/story/event/1/1/",
                 language="en"),
            dict(id="prefix:event_story:1:1-extra", kind="event_story", language="zh_hans"),
            dict(id="url-event-legacy", kind="event_story", url="https://example.test/event_story/1/1",
                 language="ko"),
            dict(id="web:fixture:zh_tw:card_story:1%_2:3", kind="card_story", language="zh_tw"),
            dict(id="raw-page", kind="area_talk", language="ja"),
            dict(id="web:fixture:en:event_story:1:10", kind="event_story", language="en"),
            dict(id="web:fixture:en:card_story:1xx2:3", kind="card_story", language="en"),
            dict(id="blocked:event_story:1:1", kind="event_story", language="ja", trust="D"),
        ]
        pages = [dict(source="fixture", trust="B", text="Person:complete body.", **page)
                 if "trust" not in page else dict(source="fixture", text="Person:complete body.", **page)
                 for page in pages]
        dbstore.upsert_web_pages(store, "fixture", pages)
        stored = dbstore.load_web_pages(store)["fixture"]
        expected = termindex.group_pages_by_story([page for page in stored if page.get("trust") != "D"
                                                  and not termindex.is_auxiliary_page(page) and not page.get("overlay")])
        with dbstore.connect(store) as conn:
            for story in expected:
                with self.subTest(story=story):
                    self.assertEqual(ap._fallback_story_pages(conn, story), expected[story])

    def test_candidate_filter_avoids_decoding_unrelated_rows_and_inventory_is_query_local(self):
        store, discovery, entry = self.fixture()
        child = self.discover(store, discovery, entry)[0]
        dbstore.upsert_web_pages(store, "noise", [dict(id=f"web:noise:en:event_story:{n}:2", source="noise",
                language="en", trust="B", kind="event_story", text="Person:noise.") for n in range(100, 300)])
        with dbstore.connect(store) as conn:
            with mock.patch.object(termindex, "page_story_key", wraps=termindex.page_story_key) as identity:
                ap._fallback_story_pages(conn, "event:1:1")
                self.assertLess(identity.call_count, 10)
            with mock.patch.object(ap, "_fallback_story_pages", wraps=ap._fallback_story_pages) as candidates:
                inventory = {}
                for _ in range(3):
                    ap._validate_subject_fallback(conn, store, child._context, child.language, child.term,
                                                  inventory=inventory)
                self.assertEqual(candidates.call_count, 1)
        dbstore.upsert_web_pages(store, "fixture", [dict(id="web:fixture:zh_hans:event_story:1:1",
                source="fixture", language="zh_hans", trust="D", kind="event_story", text="Changed page.")])
        with dbstore.connect(store) as conn:
            with self.assertRaisesRegex(ValueError, "current page policy"):
                ap._validate_subject_fallback(conn, store, child._context, child.language, child.term, inventory={})

    def test_cohesion_origin_and_focus_reuse_same_read_snapshot_inventory(self):
        store, discovery, entry = self.fixture(multi=True)
        children = self.discover(store, discovery, entry)
        self.assertEqual(len(children), 3)
        self.assertEqual(ar.submit_judgments(store, [self.judgment(children[0])])["errors"], [])
        with dbstore.connect(store) as conn:
            with mock.patch.object(ap, "_fallback_story_pages", wraps=ap._fallback_story_pages) as candidates:
                ap._occurrence_cohesion_items(conn, store)
                self.assertLessEqual(candidates.call_count, 2)
        while ar.load_queue(store):
            self.assertEqual(ar.submit_judgments(store, [self.judgment(item) for item in ar.load_queue(store)])["errors"], [])
        with dbstore.connect(store) as conn:
            self.assertGreaterEqual(len(os._read_relations(conn)), 3)
            with mock.patch.object(ap, "_fallback_story_pages", wraps=ap._fallback_story_pages) as candidates:
                self.assertEqual(ap._occurrence_cohesion_items(conn, store), [])
                self.assertLessEqual(candidates.call_count, 2)


if __name__ == "__main__":
    unittest.main()
