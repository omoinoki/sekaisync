"""Budget-limited discovery must preserve remaining work without API changes."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, termindex as ti


class DiscoveryContinuationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.terms = ["词" + str(i).zfill(3) for i in range(415)]
        self.page = dict(id="fixture:zh_hans:event_story:999:0", source="fixture",
                         language="zh_hans", kind="event_story", trust="B", story_key="event:999:0",
                         canonical_key="event_story:zh_hans:999:0", text="说话人：" + "，".join(self.terms))
        dbstore.upsert_web_pages(self.store, "fixture", [self.page])
        groups = ti.group_pages_by_story([self.page])
        items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, "zh_hans", [])
        self.root = items[0]
        self.assertEqual(len(items), 1)
        ar.enqueue(self.store, items)

    def submit(self, item, terms):
        return ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", terms=terms,
                                                   rationale="read source body")])

    def continuation(self):
        items = [item for item in ar.load_queue(self.store) if item.kind == "discovery"]
        self.assertEqual(len(items), 1)
        return items[0]

    def finish_audit(self, parent):
        from sekaisync import source_audits
        items = ar.load_queue(self.store)
        self.assertEqual(len(items), 1)
        audit = items[0]
        self.assertTrue(source_audits._is_audit(audit._context))
        self.assertFalse(source_audits._is_saturated(audit._context))
        self.assertNotIn("continuation", audit._context)
        self.assertEqual(audit._context["source_boundary_audit"]["terminal_parent_id"], parent.id)
        # This synthetic reply tests the bounded lifecycle, not lexical completeness.
        self.assertEqual(self.submit(audit, [])["errors"], [])
        self.assertIn(audit.id, ap._receipts(self.store))
        self.assertEqual(ar.load_queue(self.store), [])

    def test_exact_budget_generates_one_unique_continuation(self):
        result = self.submit(self.root, self.terms[:200])
        self.assertEqual(result["errors"], [])
        child = self.continuation()
        self.assertNotEqual(child.id, self.root.id)
        self.assertEqual(child._context["rows"], self.root._context["rows"])
        self.assertEqual(child._context["continuation"]["parent_id"], self.root.id)
        self.assertEqual(child._context["continuation"]["excluded_terms"], self.terms[:200])

    def test_below_budget_does_not_spawn_continuation(self):
        self.assertEqual(self.submit(self.root, self.terms[:199])["errors"], [])
        self.finish_audit(self.root)

    def test_two_hundred_plus_fifteen_can_all_be_recovered(self):
        self.assertEqual(self.submit(self.root, self.terms[:200])["errors"], [])
        child = self.continuation()
        self.assertEqual(self.submit(child, self.terms[200:215])["errors"], [])
        self.finish_audit(child)
        receipts = ap._receipts(self.store)
        recovered = {term for receipt in receipts.values() for term in json.loads(receipt["value"])}
        self.assertEqual(recovered, set(self.terms[:215]))

    def test_parent_and_child_replays_never_regenerate_work(self):
        self.assertEqual(self.submit(self.root, self.terms[:200])["errors"], [])
        child = self.continuation()
        first_ids = [item.id for item in ar.load_queue(self.store)]
        result = self.submit(self.root, self.terms[:200])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["discovered_terms"], 0)
        self.assertEqual([item.id for item in ar.load_queue(self.store)], first_ids)
        self.assertEqual(self.submit(child, self.terms[200:215])["errors"], [])
        self.assertEqual(self.submit(child, self.terms[200:215])["errors"], [])
        self.finish_audit(child)

    def test_two_full_rounds_accumulate_exclusions_and_link_real_receipts(self):
        self.assertEqual(self.submit(self.root, self.terms[:200])["errors"], [])
        child = self.continuation()
        self.assertEqual(self.submit(child, self.terms[200:400])["errors"], [])
        grandchild = self.continuation()
        continuation = grandchild._context["continuation"]
        self.assertEqual(continuation["ancestor_ids"], [self.root.id, child.id])
        self.assertEqual(continuation["excluded_terms"], self.terms[:400])
        self.assertEqual(self.submit(grandchild, self.terms[400:])["errors"], [])
        self.finish_audit(grandchild)

    def test_repeating_excluded_surface_cannot_vote_or_resolve_child(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        result = self.submit(child, [self.terms[0], self.terms[200]])
        self.assertTrue(any("already submitted" in error for error in result["errors"]))
        self.assertNotIn(child.id, ap._receipts(self.store))
        self.assertEqual(self.continuation().id, child.id)

    def test_normalization_alias_of_excluded_surface_is_not_a_new_vote(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        result = self.submit(child, ["词０００"])
        self.assertTrue(any("already submitted" in error for error in result["errors"]))
        self.assertNotIn(child.id, ap._receipts(self.store))

    def test_tampering_exclusions_even_with_a_rehashed_packet_is_rejected(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        context = json.loads(json.dumps(child._context))
        context["continuation"]["excluded_terms"] = context["continuation"]["excluded_terms"][:-1]
        forged = ap._item(child.term, child.language, [], "discovery", context, "")
        with dbstore.connect(self.store) as conn:
            with self.assertRaisesRegex(ValueError, "exclusions or lineage"):
                ap._validate_answer(conn, self.store, forged, dict(decision="accept", terms=[]))

    def test_missing_parent_receipt_cannot_close_even_empty_continuation(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        ar._write_json(ap._receipt_path(self.store), dict(items={}))
        result = self.submit(child, [])
        self.assertTrue(any("no submitted receipt" in error for error in result["errors"]))
        self.assertEqual(self.continuation().id, child.id)

    def test_unknown_parent_identity_is_rejected_even_with_rehashed_context(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        context = json.loads(json.dumps(child._context))
        context["continuation"].update(parent_id="arp:" + "0" * 32,
                                        root_id="arp:" + "0" * 32, ancestor_ids=["arp:" + "0" * 32])
        forged = ap._item(child.term, child.language, [], "discovery", context, "")
        with dbstore.connect(self.store) as conn:
            with self.assertRaisesRegex(ValueError, "ancestry"):
                ap._validate_answer(conn, self.store, forged, dict(decision="accept", terms=[]))

    def test_receipt_from_another_scope_is_rejected(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        receipts = ap._receipts(self.store)
        receipts[self.root.id]["scope_id"] = "0" * 64
        ar._write_json(ap._receipt_path(self.store), dict(items=receipts))
        result = self.submit(child, [])
        self.assertTrue(any("another scope" in error for error in result["errors"]))
        self.assertNotIn(child.id, ap._receipts(self.store))

    def test_stale_page_cannot_close_continuation(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        dbstore.upsert_web_pages(self.store, "fixture", [dict(self.page, text=self.page["text"] + "改变")])
        result = self.submit(child, [])
        self.assertTrue(any("page changed" in error for error in result["errors"]))
        self.assertNotIn(child.id, ap._receipts(self.store))

    def test_empty_continuation_closes_without_spawning_another(self):
        self.submit(self.root, self.terms[:200])
        child = self.continuation()
        self.assertEqual(self.submit(child, [])["errors"], [])
        self.finish_audit(child)
        self.assertEqual(json.loads(ap._receipts(self.store)[child.id]["value"]), [])

    def test_oversized_submission_still_fails_without_losing_parent(self):
        result = self.submit(self.root, self.terms[:215])
        self.assertTrue(result["errors"])
        self.assertEqual(self.continuation().id, self.root.id)
        self.assertNotIn(self.root.id, ap._receipts(self.store))

    def test_v2_persistent_queue_preserves_continuation_context(self):
        dbstore.migrate_store(self.store, target_version=2, dry_run=False,
                             backup_path=Path(self.temp.name) / "v2-backup.db")
        self.assertEqual(ar.load_queue(self.store)[0].id, self.root.id)
        result = self.submit(self.root, self.terms[:200])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["accepted"], 1)
        self.assertEqual(ap._receipts(self.store)[self.root.id]["storage"], "sqlite")
        child = self.continuation()
        self.assertEqual(child._context["continuation"]["parent_id"], self.root.id)
        self.assertEqual(self.submit(child, self.terms[200:215])["errors"], [])
        self.finish_audit(child)

    def test_v3_receipts_are_visible_without_a_json_receipt_file(self):
        self.store = Path(self.temp.name) / "store-v3"
        dbstore.initialize_new_store(self.store, target_version=3)
        dbstore.upsert_web_pages(self.store, "fixture", [self.page])
        groups = ti.group_pages_by_story([self.page])
        items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, "zh_hans", [])
        self.root = items[0]
        ar.enqueue(self.store, items)
        result = self.submit(self.root, self.terms[:200])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["accepted"], 1)
        self.assertFalse(ap._receipt_path(self.store).exists())
        child = self.continuation()
        self.assertEqual(self.submit(child, [])["errors"], [])
        self.assertEqual(ap._receipts(self.store)[child.id]["storage"], "sqlite")

    def test_english_case_distinct_surfaces_remain_fresh_in_continuation(self):
        terms = ["item" + str(i).zfill(3) for i in range(198)] + ["US", "IT"]
        page = dict(self.page, id="fixture:en:event_story:1000:0", language="en", story_key="event:1000:0",
                    canonical_key="event_story:en:1000:0", text="Speaker: " + ", ".join(terms + ["us", "it"]))
        dbstore.upsert_web_pages(self.store, "fixture", [page])
        groups = ti.group_pages_by_story([page])
        items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, "en", [])
        root = items[0]
        ar.enqueue(self.store, [root])
        result = self.submit(root, terms)
        self.assertEqual(result["errors"], [])
        children = [item for item in ar.load_queue(self.store) if item._context.get("continuation")]
        self.assertEqual(len(children), 1)
        self.assertEqual(self.submit(children[0], ["us", "it"])["errors"], [])
        self.assertEqual(json.loads(ap._receipts(self.store)[children[0].id]["value"]), ["us", "it"])


if __name__ == "__main__":
    unittest.main()
