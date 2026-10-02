"""Independent batch-boundary, tail and legacy-resume discovery checks."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, termindex


class FocusedDiscoveryBatchReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.counter = 0

    def prepare(self, version=1, text=None):
        self.counter += 1
        store = self.base / ("store-" + str(self.counter))
        dbstore.initialize(store)
        if version > 1:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                 backup_path=self.base / ("backup-" + str(self.counter) + ".db"))
        page = dict(id="web:fixture:en:event_story:999:1", source="fixture", language="en",
                    kind="event_story", trust="B", text=text or "\n".join(
                        f"Person: source{index:02} marker." for index in range(25)))
        dbstore.upsert_web_pages(store, "fixture", [page])
        groups = termindex.group_pages_by_story([page])
        items, meta = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", [])
        return store, groups, items, meta, page

    def test_first_batch_cannot_borrow_legacy_or_typed_observation_from_next_batch(self):
        store, groups, items, meta, page = self.prepare()
        self.assertEqual([len(item._context["rows"]) for item in items], [8, 8, 8, 1])
        ar.enqueue(store, items)
        first, second = items[:2]
        foreign_row = second._context["rows"][0]
        exact = "source08"
        start = foreign_row["source"]["start"] + foreign_row["source"]["text"].index(exact)
        spec = dict(kind="literal", canonical=exact, evidence_id=foreign_row["id"],
                    segments=[dict(start=start, end=start + len(exact), exact=exact)])
        for judgment in (dict(id=first.id, decision="accept", terms=[exact]),
                         dict(id=first.id, decision="accept", terms=[], subjects=[spec])):
            with self.subTest(judgment=judgment):
                result = ar.submit_judgments(store, [judgment])
                self.assertTrue(result["errors"])
                self.assertNotIn(first.id, ap._receipts(store))
                self.assertIn(first.id, {item.id for item in ar.load_queue(store)})
        result = ar.submit_judgments(store, [dict(id=second.id, decision="accept", terms=[], subjects=[spec])])
        self.assertEqual(result["errors"], [])
        self.assertIn(second.id, ap._receipts(store))

    def test_partial_completion_and_limited_export_leave_all_remaining_windows_resumable_v1_v2(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, groups, items, meta, page = self.prepare(version)
                scope = ap._read_scope(store, meta["scope_id"])
                frozen = ap._scope_path(store, meta["scope_id"]).read_bytes()
                ar.enqueue(store, items)
                self.assertEqual(ar.submit_judgments(store, [dict(id=items[0].id, decision="accept", terms=[])])["errors"], [])
                pending = ar.load_queue(store)
                required = {row["id"] for row in scope["windows"][8:]}
                self.assertEqual({item.id for item in pending if "source_boundary_audit" not in item._context},
                                 {item.id for item in items[1:]})
                audits = [item for item in pending if "source_boundary_audit" in item._context]
                self.assertEqual(len(audits), 1)
                audit = audits[0]
                self.assertEqual(audit._context["rows"], items[0]._context["rows"])
                self.assertEqual(audit._context["source_boundary_audit"]["terminal_parent_id"], items[0].id)
                self.assertEqual({row["id"] for item in pending for row in item._context["rows"]},
                                 required | {row["id"] for row in scope["windows"][:8]})
                path = self.base / ("limited-" + str(version) + ".txt")
                ar.export_for_agent(store, path, limit=1)
                self.assertNotIn("## id=" + items[0].id, path.read_text(encoding="utf-8"))
                self.assertEqual({item.id for item in ar.load_queue(store)}, {item.id for item in pending})
                regenerated, fresh = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", [])
                self.assertEqual([item.id for item in regenerated], [item.id for item in items])
                self.assertEqual(ar.enqueue(store, regenerated)["added"], 0)
                self.assertEqual(ap._scope_path(store, meta["scope_id"]).read_bytes(), frozen)
                self.assertEqual(fresh["source_windows"], 25)

    def test_legacy_broad_packet_bytes_and_id_are_not_migrated_by_export(self):
        store, groups, items, meta, page = self.prepare()
        with patch.object(ap, "_DISCOVERY_ROWS", 80):
            old, old_meta = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", [])
        self.assertEqual(len(old), 1)
        self.assertEqual(len(old[0]._context["rows"]), 25)
        self.assertEqual(old_meta["scope_id"], meta["scope_id"])
        original = old[0].to_dict()
        ar.enqueue(store, old)
        ar.export_for_agent(store, self.base / "legacy-export.txt", limit=0)
        self.assertEqual([item.id for item in ar.load_queue(store)], [old[0].id])
        self.assertEqual(ar.load_queue(store)[0].to_dict(), original)
        result = ar.submit_judgments(store, [dict(id=old[0].id, decision="accept", terms=["source24"])])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["discovered_terms"], 1)

    def test_oversized_turn_chunks_keep_raw_tail_and_exact_last_subject(self):
        text = "Person: " + "bodyword " * 1900 + "terminal_token."
        store, groups, items, meta, page = self.prepare(text=text)
        scope = ap._read_scope(store, meta["scope_id"])
        rows = [row for item in items for row in item._context["rows"]]
        self.assertEqual(rows, scope["windows"])
        self.assertGreater(len(rows), 8)
        covered = set()
        for row in rows:
            view = row["source"]
            self.assertEqual(view["text"], text[view["start"]:view["end"]])
            covered.update(range(view["start"], view["end"]))
        self.assertEqual(covered, set(range(len(text))))
        for item in items:
            self.assertLessEqual(len(item._context["rows"]), ap._DISCOVERY_ROWS)
            self.assertLessEqual(sum(len(row["source"]["text"]) for row in item._context["rows"]), ap._DISCOVERY_CHARS)
        last = rows[-1]
        self.assertEqual(last["source"]["end"], len(text))
        exact = "terminal_token"
        start = text.index(exact)
        spec = dict(kind="literal", canonical=exact, evidence_id=last["id"],
                    segments=[dict(start=start, end=start + len(exact), exact=exact)])
        ar.enqueue(store, items)
        result = ar.submit_judgments(store, [dict(id=items[-1].id, decision="accept", terms=[], subjects=[spec])])
        self.assertEqual(result["errors"], [])
        receipt = json.loads(ap._receipts(store)[items[-1].id]["value"])
        self.assertEqual(len(receipt["subjects"]), 1)
        self.assertEqual(receipt["subjects"][0]["source"]["segments"], spec["segments"])


if __name__ == "__main__":
    unittest.main()
