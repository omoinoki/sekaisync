"""Independent real export/submit adversaries for unaligned subject fallback."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger
from sekaisync.core import SekaiSyncCore


class SubjectFallbackReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.counter = 0

    def fixture(self, *, target=True, large=False, version=1):
        self.counter += 1
        store = self.base / ("store-" + str(self.counter))
        dbstore.initialize(store)
        if version > 1:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                 backup_path=self.base / ("backup-" + str(self.counter) + ".db"))
        source = dict(id="web:fixture:en:event_story:999:1", source="fixture", language="en", trust="B",
                      kind="event_story", text="A: You were lending her a hand.\nB: Another utterance.")
        localized = dict(id="web:fixture:zh_hans:event_story:999:1", source="fixture", language="zh_hans", trust="B",
                         kind="event_story", text="甲：你伸出了援手。\n乙：" + ("别的话题。" * 6000 if large else "接下来换个话题。"))
        dbstore.upsert_web_pages(store, "fixture", [source, localized] if target else [source])
        view = ap._view(source, [0], ap._lines(source)[1])
        row = dict(story_key="event:999:1", source=view, targets={},
                   search_text=view["text"].casefold(), search_unwrapped=view["text"].casefold())
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["zh_hans"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id=scope_id, source_language="en", rows=[row])
        discovery = ap._item("@discover:event:999:1:" + row["id"], "en", [], "discovery", context, "Missing alignment fixture")
        ar.enqueue(store, [discovery])
        parts = []
        for exact in ("lending", "a hand"):
            start = source["text"].index(exact)
            parts.append(dict(start=start, end=start + len(exact), exact=exact))
        spec = dict(kind="segmented", evidence_id=row["id"], segments=parts)
        return store, discovery, spec, source, localized

    def discover(self, store, item, spec):
        result = ar.submit_judgments(store, [dict(id=item.id, decision="accept", terms=[], subjects=[spec])])
        self.assertEqual(result["errors"], [])
        pending = ar.load_queue(store)
        self.assertTrue(all(child._context["task"] in {"discovery", "occurrence", "subject_gap"}
                            for child in pending))
        audits = [child for child in pending if child._context["task"] == "discovery"]
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        marker = audit._context["source_boundary_audit"]
        self.assertEqual(marker["stage"], "review")
        self.assertEqual(marker["terminal_parent_id"], item.id)
        self.assertEqual(audit._context["rows"], item._context["rows"])
        self.assertEqual(len(marker["inherited_subjects"]), 1)
        target_ids = [child.id for child in pending if child.id != audit.id]
        settled = ar.submit_judgments(store, [dict(id=audit.id, decision="accept", terms=[])])
        self.assertEqual(settled["errors"], [])
        self.assertEqual(settled["accepted"], 1)
        remaining = ar.load_queue(store)
        self.assertEqual([child.id for child in remaining], target_ids)
        return remaining

    def judgment(self, item, *, kind="lexical", partial=False):
        row = item._context["rows"][0]
        view = row["target"]
        if kind in {"unresolved", "omitted"}:
            parts = [dict(start=view["start"], end=view["end"], exact=view["text"])]
            if partial:
                parts = [dict(start=view["start"], end=view["start"] + 1, exact=view["text"][:1])]
        else:
            exact = "伸出了援手"
            start = view["start"] + view["text"].index(exact)
            parts = [dict(start=start, end=start + len(exact), exact=exact)]
        return dict(id=item.id, decision="accept", relations=[dict(
            evidence_id=row["id"], source_segments=item._context["subject"]["source"]["segments"],
            target_segments=parts, sense_key="helping", sense_gloss="Assisting this beneficiary in the source utterance",
            kind=kind, rationale="Explicit same-content raw evidence; alignment is not asserted.")])

    def no_relations(self, store):
        with dbstore.connect(store) as conn:
            self.assertEqual(ledger._read_relations(conn), [])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_true_fallback_export_submit_and_old_query_preserve_unaligned_provenance_v1_v2(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, discovery, spec, source, target = self.fixture(version=version)
                original = ap._scope_path(store, discovery._context["scope_id"]).read_bytes()
                child = self.discover(store, discovery, spec)[0]
                exported = ar.export_for_agent(store, self.base / ("fallback-" + str(version) + ".txt"), limit=0).read_text(encoding="utf-8")
                self.assertIn("same_content_unaligned", exported)
                self.assertIn("fallback_contract", exported)
                self.assertIn(target["text"].splitlines()[1], exported)
                self.assertTrue(child._context["fallback"]["target_complete_page"])
                self.assertEqual(ar.submit_judgments(store, [self.judgment(child)])["errors"], [])
                self.assertEqual(ap._scope_path(store, discovery._context["scope_id"]).read_bytes(), original)
                generic = SekaiSyncCore(store).query("lending a hand", include_web=False)
                self.assertEqual(len(generic["terms"]), 1)
                self.assertEqual(generic["terms"][0]["names"], {})
                self.assertEqual(next(position for position in generic["terms"][0]["positions"]
                                      if position["language"] == "zh_hans")["term"], "伸出了援手")
                with dbstore.connect(store) as conn:
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                    self.assertEqual(len(ledger._read_relations(conn)), 1)

    def test_missing_page_debt_cannot_be_accepted_away_and_export_recovers_same_subject(self):
        store, discovery, spec, source, target = self.fixture(target=False)
        debt = self.discover(store, discovery, spec)[0]
        self.assertEqual(debt._context["task"], "subject_gap")
        self.assertEqual(debt._context["gap"]["reason"], "missing_usable_localized_page")
        result = ar.submit_judgments(store, [dict(id=debt.id, decision="accept", terms=[], subjects=[])])
        self.assertTrue(result["errors"])
        self.assertIn(debt.id, {item.id for item in ar.load_queue(store)})
        self.no_relations(store)
        dbstore.upsert_web_pages(store, "fixture", [target])
        ar.export_for_agent(store, self.base / "recovered.txt", limit=0)
        pending = ar.load_queue(store)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]._context["subject"], debt._context["subject"])
        self.assertEqual(pending[0]._context["task"], "occurrence")

    def test_partial_absence_and_uncertainty_fragments_cannot_certify_full_fallback_region(self):
        store, discovery, spec, source, target = self.fixture()
        child = self.discover(store, discovery, spec)[0]
        for kind in ("unresolved", "omitted"):
            with self.subTest(kind=kind):
                self.assertTrue(ar.submit_judgments(store, [self.judgment(child, kind=kind, partial=True)])["errors"])
                self.assertIn(child.id, {item.id for item in ar.load_queue(store)})
                self.assertNotIn(child.id, ap._receipts(store))
                self.no_relations(store)

    def test_bounded_full_region_still_cannot_prove_whole_page_omission(self):
        store, discovery, spec, source, target = self.fixture(large=True)
        pending = self.discover(store, discovery, spec)
        child = next(item for item in pending if item._context["task"] == "occurrence")
        debt = next(item for item in pending if item._context["task"] == "subject_gap")
        self.assertFalse(child._context["fallback"]["target_complete_page"])
        self.assertEqual(child._context["fallback"]["unshown_target_regions"], debt._context["gap"]["unshown_target_regions"])
        self.assertTrue(ar.submit_judgments(store, [self.judgment(child, kind="omitted")])["errors"])
        self.assertIn(debt.id, {item.id for item in ar.load_queue(store)})
        self.no_relations(store)

    def test_stale_source_or_target_never_accepts_an_exported_fallback(self):
        for changed in ("source", "target"):
            with self.subTest(changed=changed):
                store, discovery, spec, source, target = self.fixture()
                child = self.discover(store, discovery, spec)[0]
                page = source if changed == "source" else target
                dbstore.upsert_web_pages(store, "fixture", [dict(page, text=page["text"] + " Changed." if changed == "source"
                                                               else page["text"] + "改变。")])
                self.assertTrue(ar.submit_judgments(store, [self.judgment(child)])["errors"])
                self.assertIn(child.id, {item.id for item in ar.load_queue(store)})
                self.no_relations(store)

    def test_current_target_story_language_and_usable_page_policy_are_replayed(self):
        updates = (dict(trust="D"), dict(auxiliary=1), dict(language="ko"),
                   dict(url="https://fixture.invalid/story/event/999/2/"))
        for fields in updates:
            with self.subTest(fields=fields):
                store, discovery, spec, source, target = self.fixture()
                child = self.discover(store, discovery, spec)[0]
                dbstore.upsert_web_pages(store, "fixture", [dict(target, **fields)])
                self.assertTrue(ar.submit_judgments(store, [self.judgment(child)])["errors"])
                self.no_relations(store)

    def test_fallback_ancestry_and_raw_window_cannot_be_forged_even_after_item_rehash(self):
        mutations = (lambda context: context["fallback"].update(origin_scope_id="0" * 64),
                     lambda context: context["fallback"].update(subject_id="subject:segmented:forged"),
                     lambda context: context["fallback"].update(target_complete_page=False),
                     lambda context: context["rows"][0]["target"].update(start=1,
                         text=context["rows"][0]["target"]["text"][1:]))
        for mutate in mutations:
            store, discovery, spec, source, target = self.fixture()
            child = self.discover(store, discovery, spec)[0]
            context = deepcopy(child._context)
            mutate(context)
            forged = ap._item(child.term, child.language, child.candidates, child.kind, context, "Rehashed forged fallback")
            ar.enqueue(store, [forged])
            with self.subTest(forged=forged.id):
                self.assertTrue(ar.submit_judgments(store, [self.judgment(forged)])["errors"])
                self.assertNotIn(forged.id, ap._receipts(store))
                self.assertIn(forged.id, {item.id for item in ar.load_queue(store)})
                self.no_relations(store)

    def test_original_scope_language_membership_is_mandatory(self):
        store, discovery, spec, source, target = self.fixture()
        child = self.discover(store, discovery, spec)[0]
        with dbstore.connect(store) as conn, self.assertRaisesRegex(ValueError, "outside its original"):
            ap._subject_fallback_item(conn, store, discovery._context["scope_id"], child._context["subject"], "ko")
        self.no_relations(store)

    def test_lexical_fallback_cannot_select_target_speaker_metadata(self):
        store, discovery, spec, source, target = self.fixture()
        child = self.discover(store, discovery, spec)[0]
        judgment = self.judgment(child)
        view = child._context["rows"][0]["target"]
        judgment["relations"][0]["target_segments"] = [dict(start=view["start"], end=view["start"] + 1, exact="甲")]
        self.assertTrue(ar.submit_judgments(store, [judgment])["errors"])
        self.no_relations(store)

    def test_lexical_fallback_cannot_stitch_distinct_target_speaker_turns(self):
        for text in ("甲：你伸出\n乙：了援手。", "甲：你伸出\r\n甲：了援手。",
                     "甲﹕你伸出\n乙︓了援手。"):
            with self.subTest(text=text):
                store, discovery, spec, source, target = self.fixture()
                target = dict(target, text=text)
                dbstore.upsert_web_pages(store, "fixture", [target])
                child = self.discover(store, discovery, spec)[0]
                judgment = self.judgment(child, kind="unresolved")
                selected = []
                for exact in ("伸出", "了援手"):
                    start = text.index(exact)
                    selected.append(dict(start=start, end=start + len(exact), exact=exact))
                judgment["relations"][0].update(kind="lexical", target_segments=selected)
                self.assertTrue(ar.submit_judgments(store, [judgment])["errors"])
                self.assertIn(child.id, {item.id for item in ar.load_queue(store)})
                self.no_relations(store)

    def test_lexical_target_soft_wrap_retains_one_utterance_and_exact_raw_scalar(self):
        for text in ("甲：你伸出\r\n了援手。", "甲：\U0001f9ed你伸出\r\n了援手。"):
            with self.subTest(text=text):
                store, discovery, spec, source, target = self.fixture()
                target = dict(target, text=text)
                dbstore.upsert_web_pages(store, "fixture", [target])
                child = self.discover(store, discovery, spec)[0]
                judgment = self.judgment(child, kind="unresolved")
                selected = []
                for exact in ("伸出", "了援手"):
                    start = text.index(exact)
                    selected.append(dict(start=start, end=start + len(exact), exact=exact))
                judgment["relations"][0].update(kind="lexical", target_segments=selected)
                self.assertEqual(ar.submit_judgments(store, [judgment])["errors"], [])
                generic = SekaiSyncCore(store).query("lending a hand", include_web=False)
                position = next(position for position in generic["terms"][0]["positions"]
                                if position["language"] == "zh_hans")
                self.assertEqual(position["term"], "伸出\r\n了援手")
                self.assertEqual(generic["terms"][0]["names"], {})
                with dbstore.connect(store) as conn:
                    relation = ledger._read_relations(conn)[0]
                    self.assertEqual(relation["target"]["segments"], selected)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_bare_single_line_lexical_and_multiline_context_are_consumable(self):
        for text, kind, fragments, scalar in (
                ("你伸出了援手。", "lexical", ["伸出了援手"], "伸出了援手"),
                ("你伸出\r\n了援手。", "paraphrase", ["伸出", "了援手"], None)):
            with self.subTest(text=text, kind=kind):
                store, discovery, spec, source, target = self.fixture()
                target = dict(target, text=text)
                dbstore.upsert_web_pages(store, "fixture", [target])
                child = self.discover(store, discovery, spec)[0]
                judgment = self.judgment(child, kind="unresolved")
                selected = []
                for exact in fragments:
                    start = text.index(exact)
                    selected.append(dict(start=start, end=start + len(exact), exact=exact))
                judgment["relations"][0].update(kind=kind, target_segments=selected)
                self.assertEqual(ar.submit_judgments(store, [judgment])["errors"], [])
                generic = SekaiSyncCore(store).query("lending a hand", include_web=False)
                position = next(position for position in generic["terms"][0]["positions"]
                                if position["language"] == "zh_hans")
                if scalar:
                    self.assertEqual(position["term"], scalar)
                else:
                    self.assertIn(position.get("term"), (None, ""))
                    self.assertIn("paraphrase", position["note"])
                with dbstore.connect(store) as conn:
                    self.assertEqual(ledger._read_relations(conn)[0]["target"]["segments"], selected)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_bare_independent_raw_lines_are_not_implicitly_one_lexical_utterance(self):
        store, discovery, spec, source, target = self.fixture()
        text = "你伸出\r\n了援手。"
        target = dict(target, text=text)
        dbstore.upsert_web_pages(store, "fixture", [target])
        child = self.discover(store, discovery, spec)[0]
        judgment = self.judgment(child, kind="unresolved")
        selected = []
        for exact in ("伸出", "了援手"):
            start = text.index(exact)
            selected.append(dict(start=start, end=start + len(exact), exact=exact))
        judgment["relations"][0].update(kind="lexical", target_segments=selected)
        self.assertTrue(ar.submit_judgments(store, [judgment])["errors"])
        self.assertIn(child.id, {item.id for item in ar.load_queue(store)})
        self.no_relations(store)


if __name__ == "__main__":
    unittest.main()
