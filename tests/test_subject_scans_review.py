"""Independent actual export/submit adversaries for bounded region review."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore
from sekaisync import occurrence_store as ledger, span_subjects, subject_scans as scans


class SubjectScansReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.counter = 0
        self.exports = 0

    def fixture(self, version=1, target_text=None):
        self.counter += 1
        store = self.base / ("store-" + str(self.counter))
        dbstore.initialize(store)
        if version > 1:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                 backup_path=self.base / ("backup-" + str(self.counter) + ".db"))
        source = dict(id="web:fixture:en:event_story:999:1", source="fixture", language="en", trust="B",
                      kind="event_story", text="A: You were lending her a hand.\nB: Another utterance.")
        target = dict(id="web:fixture:zh_hans:event_story:999:1", source="fixture", language="zh_hans", trust="B",
                      kind="event_story", text=target_text or "甲：你伸出了援手。\n乙：" + "别的话题。" * 12000 + "\n丙：最后仍提到援手。")
        dbstore.upsert_web_pages(store, "fixture", [source, target])
        view = ap._view(source, [0], ap._lines(source)[1])
        row = dict(story_key="event:999:1", source=view, targets={},
                   search_text=view["text"].casefold(), search_unwrapped=view["text"].casefold())
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="en", target_languages=["zh_hans"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id=scope_id,
                       source_language="en", rows=[row])
        discovery = ap._item("@discover:event:999:1:" + row["id"], "en", [], "discovery", context,
                             "Independent bounded scan fixture")
        ar.enqueue(store, [discovery])
        parts = []
        for exact in ("lending", "a hand"):
            start = source["text"].index(exact)
            parts.append(dict(start=start, end=start + len(exact), exact=exact))
        spec = dict(kind="segmented", evidence_id=row["id"], segments=parts)
        result = ar.submit_judgments(store, [dict(id=discovery.id, decision="accept", terms=[], subjects=[spec])])
        self.assertEqual(result["errors"], [])
        pending = ar.load_queue(store)
        seed = next(item for item in pending if item._context["task"] == "occurrence"
                    and not item._context.get("scan"))
        self.assertFalse(seed._context["fallback"]["target_complete_page"])
        return store, seed, source, target

    def judgment(self, item, kind="unresolved", declare=True, sense="helping"):
        row = item._context["rows"][0]
        target = row["target"]
        if kind in {"unresolved", "omitted"}:
            parts = [dict(start=target["start"], end=target["end"], exact=target["text"])]
        else:
            exact = "伸出了援手" if "伸出了援手" in target["text"] else "别的话题"
            local = target["text"].find(exact)
            self.assertGreaterEqual(local, 0)
            start = target["start"] + local
            parts = [dict(start=start, end=start + len(exact), exact=exact)]
        focus = item._context.get("focus")
        sense_key = focus["sense"]["key"] if focus else sense
        sense_gloss = focus["sense"]["gloss"] if focus else "Assisting this beneficiary in this exact source utterance"
        result = dict(id=item.id, decision="accept", relations=[dict(
            evidence_id=row["id"], source_segments=item._context["subject"]["source"]["segments"],
            target_segments=parts, sense_key=sense_key, sense_gloss=sense_gloss,
            kind=kind, rationale="Structural fixture only, not a semantic accuracy judgment.")])
        if item._context.get("scan") and declare:
            result["reviewed_region"] = scans._review_declaration(target)
        return result

    def submit(self, store, item, **kwargs):
        result = ar.submit_judgments(store, [self.judgment(item, **kwargs)])
        self.assertEqual(result["errors"], [])
        return result

    def export(self, store):
        self.exports += 1
        path = self.base / ("export-" + str(self.exports) + ".txt")
        ar.export_for_agent(store, path, limit=0)
        return path.read_text(encoding="utf-8")

    def begin(self, version=1, seed_kind="unresolved", target_text=None):
        store, seed, source, target = self.fixture(version, target_text)
        self.submit(store, seed, kind=seed_kind)
        exported = self.export(store)
        pending = ar.load_queue(store)
        item = next(item for item in pending if item._context.get("scan"))
        return store, seed, item, source, target, exported

    def relation_for(self, store, item):
        with dbstore.connect(store) as conn:
            return next(record for record in ledger._read_relations(conn)
                        if record["review_item_id"] == item.id)

    def assert_rejected(self, store, item, judgment):
        with dbstore.connect(store) as conn:
            before = conn.execute("SELECT COUNT(*) FROM scraper_relations").fetchone()[0]
        result = ar.submit_judgments(store, [judgment])
        self.assertTrue(result["errors"])
        self.assertNotIn(item.id, ap._receipts(store))
        self.assertIn(item.id, {pending.id for pending in ar.load_queue(store)})
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM scraper_relations").fetchone()[0], before)

    def forge(self, store, item, mutate):
        context = deepcopy(item._context)
        mutate(context)
        original = ap._read_scope(store, item._context["scope_id"])
        row = context["rows"][0]
        scope = dict(original, scan=context["scan"], fallback=context["fallback"], windows=[dict(
            id=row["id"], story_key=row["story_key"], source=row["source"], targets={item.language: row["target"]})])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        context["scope_id"] = scope_id
        forged = ap._item(item.term, item.language, item.candidates, item.kind, context, "Rehashed independent adversary")
        ar.enqueue(store, [forged])
        return forged

    def test_actual_export_and_receipts_bind_fixed_subject_sense_and_current_page(self):
        store, seed, item, source, target, exported = self.begin()
        scan = item._context["scan"]
        root = self.relation_for(store, seed)
        self.assertIn("content_review_not_omission_proof", exported)
        self.assertIn("reviewed_region", exported)
        self.assertEqual(scan["reviewed_regions"], [dict(start=0, end=ap._EXPANSION_CHARS, relation_id=root["id"])])
        self.assertEqual(scan["source_id"], root["source"]["id"])
        self.assertEqual(scan["sense_id"], root["sense"]["id"])
        self.assertEqual(scan["target_page_sha256"], root["target"]["page_sha256"])
        self.assertEqual(scan["region"]["start"], ap._EXPANSION_CHARS)
        self.submit(store, item)
        relation = self.relation_for(store, item)
        self.assertEqual(relation["grounding"]["scan_review"], scans._review_declaration(item._context["rows"][0]["target"]))
        self.assertIn(relation["id"], json.loads(ap._receipts(store)[item.id]["value"]))

    def test_small_lexical_anchor_neither_covers_seed_region_nor_clears_scanning_debt(self):
        store, seed, item, source, target, exported = self.begin(seed_kind="lexical")
        self.assertEqual(item._context["scan"]["reviewed_regions"], [])
        self.assertEqual(item._context["scan"]["region"]["start"], 0)
        debt = [pending for pending in ar.load_queue(store) if pending._context["task"] == "subject_gap"]
        self.assertTrue(debt)
        self.assertEqual(debt[0]._context["gap"]["unshown_target_regions"], [dict(start=0, end=len(target["text"]))])
        self.assert_rejected(store, item, self.judgment(item, kind="lexical", declare=False))
        self.export(store)
        self.assertTrue(any(pending._context["task"] == "subject_gap" for pending in ar.load_queue(store)))

    def test_review_declaration_must_equal_complete_actual_region(self):
        mutations = (
            lambda value: value.update(start=value["start"] + 1),
            lambda value: value.update(end=value["end"] - 1),
            lambda value: value.update(page_sha256="0" * 64),
            lambda value: value.update(complete_region_reviewed=False),
            lambda value: value.update(complete_region_reviewed=1),
            lambda value: value.update(start=float(value["start"])),
            lambda value: value.update(end=float(value["end"])),
            lambda value: value.update(claim="whole_page_omitted"),
            lambda value: value.update(extra="Not part of the exact declaration"))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                store, seed, item, source, target, exported = self.begin()
                judgment = self.judgment(item)
                mutate(judgment["reviewed_region"])
                self.assert_rejected(store, item, judgment)

    def test_omitted_is_forbidden_even_with_a_complete_review_declaration(self):
        store, seed, item, source, target, exported = self.begin()
        self.assert_rejected(store, item, self.judgment(item, kind="omitted"))

    def test_rehashed_coverage_and_receipt_ancestry_cannot_be_forged(self):
        mutations = (
            lambda context: context["scan"]["reviewed_regions"][0].update(end=len(target["text"])),
            lambda context: context["scan"]["reviewed_regions"][0].update(relation_id="rel:forged"),
            lambda context: context["scan"].update(root_relation_id="rel:forged"),
            lambda context: context["scan"].update(parent_relation_id="rel:forged"),
            lambda context: context["scan"].update(parent_review_item_id="arp:forged"),
            lambda context: context["scan"]["reviewed_regions"][0].update(start=0.0),
            lambda context: context["scan"].update(region=dict(start=0, end=ap._EXPANSION_CHARS)))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                store, seed, item, source, target, exported = self.begin()
                forged = self.forge(store, item, mutate)
                self.assert_rejected(store, forged, self.judgment(forged))

    def test_rehashed_subject_source_sense_language_and_target_version_cannot_switch(self):
        mutations = (
            lambda context: context["scan"].update(subject_id="subject:segmented:forged"),
            lambda context: context["scan"].update(source_id="occ:forged"),
            lambda context: context["scan"].update(sense_id="sense:forged"),
            lambda context: context["scan"].update(target_language="ko"),
            lambda context: context["scan"].update(target_page_source="other"),
            lambda context: context["scan"].update(target_page_id="other:zh_hans:event_story:999:1"),
            lambda context: context["scan"].update(target_page_sha256="0" * 64),
            lambda context: context["scan"].update(target_page_code_points=1))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                store, seed, item, source, target, exported = self.begin()
                forged = self.forge(store, item, mutate)
                self.assert_rejected(store, forged, self.judgment(forged))

    def test_present_but_null_or_empty_scan_cannot_disable_ancestry_validation(self):
        for value in (None, {}):
            with self.subTest(value=value):
                store, seed, item, source, target, exported = self.begin()
                forged = self.forge(store, item, lambda context: context.update(scan=value))
                self.assert_rejected(store, forged, self.judgment(forged))

    def test_another_valid_source_subject_and_origin_scope_cannot_borrow_coverage(self):
        store, seed, item, source, target, exported = self.begin()
        alternate = dict(source, id="web:alternate:en:event_story:999:1", source="alternate",
                         text="C: You were lending him a hand.")
        dbstore.upsert_web_pages(store, "alternate", [alternate])
        view = ap._view(alternate, [0], ap._lines(alternate)[1])
        parts = []
        for exact in ("lending", "a hand"):
            start = alternate["text"].index(exact)
            parts.append(dict(start=start, end=start + len(exact), exact=exact))
        subject = span_subjects._segmented(view, "event:999:1", parts)
        self.assertEqual(subject["canonical"], item._context["subject"]["canonical"])
        self.assertNotEqual(subject["id"], item._context["subject"]["id"])
        row = dict(story_key="event:999:1", source=view, targets={})
        row["id"] = "span:" + ap._digest(row)[:24]
        origin = dict(source_language="en", target_languages=["zh_hans"], windows=[row])
        origin_id = ap._digest(origin)
        ar._write_json(ap._scope_path(store, origin_id), origin)
        prior_sense = item._context["focus"]["sense"]
        sense = ledger._sense(subject["id"], "en", prior_sense["key"], prior_sense["gloss"])

        def switch(context):
            context["subject"] = subject
            context["rows"][0]["source"] = view
            context["focus"] = dict(source=subject["source"], sense=sense)
            context["fallback"]["origin_scope_id"] = origin_id
            context["fallback"]["subject_id"] = subject["id"]
            context["scan"].update(origin_scope_id=origin_id, subject_id=subject["id"],
                                   source_id=subject["source"]["id"], sense_id=sense["id"])

        forged = self.forge(store, item, switch)
        self.assert_rejected(store, forged, self.judgment(forged))

    def test_another_real_target_page_with_identical_text_cannot_inherit_progress(self):
        store, seed, item, source, target, exported = self.begin()
        alternate = dict(target, id="web:alternate:zh_hans:event_story:999:1", source="alternate")
        dbstore.upsert_web_pages(store, "alternate", [alternate])

        def switch(context):
            context["rows"][0]["target"].update(source=alternate["source"], page_id=alternate["id"])
            context["scan"].update(target_page_source=alternate["source"], target_page_id=alternate["id"])

        forged = self.forge(store, item, switch)
        self.assert_rejected(store, forged, self.judgment(forged))

    def test_scan_cannot_change_source_segments_or_fixed_sense_in_answer(self):
        for field in ("source_segments", "sense_key", "sense_gloss"):
            with self.subTest(field=field):
                store, seed, item, source, target, exported = self.begin()
                judgment = self.judgment(item)
                proposal = judgment["relations"][0]
                if field == "source_segments":
                    proposal[field] = [dict(start=source["text"].index("hand"), end=source["text"].index("hand") + 4, exact="hand")]
                else:
                    proposal[field] += " changed"
                self.assert_rejected(store, item, judgment)

    def test_missing_rejected_wrong_scope_or_wrong_relation_receipt_blocks_progress(self):
        for changes in (None, dict(decision="reject"), dict(scope_id="0" * 64), dict(value="[]")):
            with self.subTest(changes=changes):
                store, seed, item, source, target, exported = self.begin()
                root = self.relation_for(store, seed)
                payload = ar._read_json(ap._receipt_path(store))
                if changes is None:
                    payload["items"].pop(seed.id)
                else:
                    payload["items"][seed.id].update(changes)
                ar._write_json(ap._receipt_path(store), payload)
                with dbstore.connect(store) as conn, self.assertRaises(ValueError):
                    scans._next_item(conn, store, root, persist=False)
                self.assert_rejected(store, item, self.judgment(item))

    def test_stale_source_or_target_rejects_a_frozen_scan_without_credit(self):
        for changed in ("source", "target"):
            with self.subTest(changed=changed):
                store, seed, item, source, target, exported = self.begin()
                page = source if changed == "source" else target
                dbstore.upsert_web_pages(store, "fixture", [dict(page, text=page["text"] + " changed" if changed == "source"
                                                               else page["text"] + "改变。")])
                self.assert_rejected(store, item, self.judgment(item))
                self.assertTrue(any(pending._context["task"] == "subject_gap" for pending in ar.load_queue(store)))

    def test_sqlite_relation_receipt_cannot_borrow_another_accepted_value(self):
        store, seed, item, source, target, exported = self.begin(version=2)
        root = self.relation_for(store, seed)
        with dbstore.connect(store) as conn:
            changed = conn.execute("UPDATE review_decisions SET value='[]' WHERE item_id=?", (seed.id,)).rowcount
            self.assertGreater(changed, 0)
            conn.commit()
        with dbstore.connect(store) as conn, self.assertRaises(ValueError):
            scans._next_item(conn, store, root, persist=False)
        self.assert_rejected(store, item, self.judgment(item))

    def test_scan_lexical_target_cannot_cross_relabelled_same_speaker_transition(self):
        text = "甲：前面的内容。" + "别的话题。" * 4800 + "\n甲：你伸出\n甲：了援手。"
        store, seed, item, source, target, exported = self.begin(target_text=text)
        judgment = self.judgment(item)
        selected = []
        for exact in ("伸出", "了援手"):
            start = text.index(exact)
            selected.append(dict(start=start, end=start + len(exact), exact=exact))
        judgment["relations"][0].update(kind="lexical", target_segments=selected)
        self.assert_rejected(store, item, judgment)

    def test_accepted_scan_proof_cannot_drop_or_shrink_the_frozen_scan_review(self):
        store, seed, item, source, target, exported = self.begin()
        self.submit(store, item)
        original = self.relation_for(store, item)
        for change in ("missing", "partial"):
            with self.subTest(change=change):
                proof = deepcopy(original["grounding"])
                if change == "missing":
                    proof.pop("scan_review")
                else:
                    proof["scan_review"]["end"] -= 1
                forged = ledger._relation(original["source"], original["target"], original["sense"],
                    original["kind"], original["review_item_id"], original["rationale"], original["agent"], proof)
                with dbstore.connect(store) as conn, self.assertRaises(ValueError):
                    ledger._validate_relation(conn, forged)

    def test_legacy_unresolved_prefix_resumes_idempotently_until_terminal_review_pending_v1_v2(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, seed, item, source, target, exported = self.begin(version)
                self.assertEqual(item._context["scan"]["region"]["start"], ap._EXPANSION_CHARS)
                while True:
                    self.submit(store, item)
                    self.export(store)
                    pending = ar.load_queue(store)
                    next_items = [candidate for candidate in pending if candidate._context.get("scan")]
                    if not next_items:
                        break
                    self.assertEqual(len(next_items), 1)
                    item = next_items[0]
                debts = [candidate for candidate in pending if candidate._context["task"] == "subject_gap"]
                self.assertEqual(len(debts), 1)
                self.assertEqual(debts[0]._context["gap"]["reason"], "subject_scan_terminal_review_pending")
                self.assertEqual(debts[0]._context["gap"]["unshown_target_regions"], [])
                coverage = debts[0]._context["gap"]["reviewed_regions"]
                self.assertEqual(coverage[0]["start"], 0)
                self.assertEqual(coverage[-1]["end"], len(target["text"]))
                self.assertTrue(all(left["end"] == right["start"] for left, right in zip(coverage, coverage[1:])))
                refusal = ar.submit_judgments(store, [dict(id=debts[0].id, decision="accept", terms=[])])
                self.assertTrue(refusal["errors"])
                frozen = {candidate.id for candidate in pending}
                for _ in range(2):
                    self.export(store)
                    self.assertEqual({candidate.id for candidate in ar.load_queue(store)}, frozen)
                with dbstore.connect(store) as conn:
                    self.assertFalse(any(record["kind"] == "omitted" for record in ledger._read_relations(conn)))
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
