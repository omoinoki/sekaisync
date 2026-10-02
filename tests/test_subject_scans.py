"""Bounded content-review regions progress without claiming omission."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger, subject_scans as scans
from sekaisync import source_audits
from tests import test_agent_subject_fallback as fixtures


class SubjectScanTests(unittest.TestCase):
    fixture = fixtures.SubjectFallbackTests.fixture
    judgment = fixtures.SubjectFallbackTests.judgment

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def discover(self, store, discovery, entry):
        # Keep the source review pending to test its coexistence with target debt.
        result = ar.submit_judgments(store, [dict(id=discovery.id, decision="accept", subjects=[entry])])
        self.assertEqual(result["errors"], [])
        pending = ar.load_queue(store)
        audits = [item for item in pending if source_audits._is_audit(item._context)]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]._context["source_boundary_audit"]["terminal_parent_id"], discovery.id)
        self.assertEqual(audits[0]._context["rows"], discovery._context["rows"])
        return pending

    def start(self, version=1, kind="unresolved"):
        store, discovery, entry = self.fixture(version=version, large=True)
        pending = self.discover(store, discovery, entry)
        initial = next(item for item in pending if item._context["task"] == "occurrence")
        outcome = ar.submit_judgments(store, [self.judgment(initial, kind)])
        self.assertEqual(outcome["errors"], [])
        return store, initial

    def next_scan(self, store):
        ar.export_for_agent(store, self.base / "scan-packets.txt", limit=0)
        pending = ar.load_queue(store)
        scans_pending = [item for item in pending if "scan" in item._context]
        self.assertEqual(len(scans_pending), 1)
        return scans_pending[0]

    def scan_judgment(self, item, kind="unresolved"):
        result = self.judgment(item, kind)
        result["reviewed_region"] = scans._review_declaration(item._context["rows"][0]["target"])
        return result

    def test_whole_prefix_uncertainty_recovers_next_raw_region_and_keeps_terminal_debt_v1_v2(self):
        for version in (1, 2):
            with self.subTest(version=version):
                store, initial = self.start(version)
                subject = initial._context["subject"]
                child = self.next_scan(store)
                target = child._context["rows"][0]["target"]
                self.assertEqual(target["start"], ap._EXPANSION_CHARS)
                self.assertEqual(child._context["subject"], subject)
                self.assertEqual(child._context["focus"]["source"], subject["source"])
                self.assertEqual(len(child._context["scan"]["reviewed_regions"]), 1)
                self.assertIn("scan_contract:", ar.render_item(child))
                self.assertEqual(ar.submit_judgments(store, [self.scan_judgment(child)])["errors"], [])
                ar.export_for_agent(store, self.base / f"terminal-{version}.txt", limit=0)
                pending = ar.load_queue(store)
                self.assertEqual(len(pending), 2)
                source_reviews = [item for item in pending if source_audits._is_audit(item._context)]
                target_debts = [item for item in pending if item._context["task"] == "subject_gap"]
                self.assertEqual((len(source_reviews), len(target_debts)), (1, 1))
                source_review = source_reviews[0]
                audit = source_review._context["source_boundary_audit"]
                self.assertEqual(audit["stage"], "review")
                self.assertEqual(audit["excluded_subject_ids"], [subject["id"]])
                original_scope_id = initial._context["fallback"]["origin_scope_id"]
                self.assertEqual(source_review._context["scope_id"], original_scope_id)
                self.assertEqual(source_review._context["rows"], ap._read_scope(store, original_scope_id)["windows"])
                parent_receipt = ap._receipts(store)[audit["terminal_parent_id"]]
                self.assertEqual(parent_receipt["scope_id"], original_scope_id)
                self.assertEqual(parent_receipt["review_context"]["rows"], source_review._context["rows"])
                debt = target_debts[0]
                self.assertEqual(debt._context["gap"]["reason"], "subject_scan_terminal_review_pending")
                self.assertEqual(debt._context["gap"]["unshown_target_regions"], [])
                self.assertEqual(len(debt._context["gap"]["reviewed_regions"]), 2)
                again = scans.ensure_subject_scans(store)
                self.assertEqual((again["added"], again["retired"]), (0, 0))
                self.assertTrue(ar.submit_judgments(store, [dict(id=debt.id, decision="accept")])["errors"])
                self.assertEqual(ar.submit_judgments(store, [dict(id=source_review.id, decision="accept", terms=[])])["errors"], [])
                self.assertEqual([item.id for item in ar.load_queue(store)], [debt.id])
                self.assertEqual(source_audits._ensure(store)["added"], 0)
                with dbstore.connect(store) as conn:
                    records = ledger._read_relations(conn)
                    self.assertEqual(len(records), 2)
                    self.assertEqual({record["kind"] for record in records}, {"unresolved"})
                    self.assertIn("scan_review", next(record for record in records if "scan" in record["grounding"]["context"])["grounding"])
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_lexical_seed_is_not_automatic_prefix_coverage_then_explicit_review_advances(self):
        store, initial = self.start(kind="lexical")
        first = self.next_scan(store)
        self.assertEqual(first._context["rows"][0]["target"]["start"], 0)
        self.assertEqual(first._context["scan"]["reviewed_regions"], [])
        self.assertEqual(ar.submit_judgments(store, [self.scan_judgment(first, "lexical")])["errors"], [])
        second = self.next_scan(store)
        self.assertEqual(second._context["rows"][0]["target"]["start"], ap._EXPANSION_CHARS)
        self.assertEqual(second._context["focus"], first._context["focus"])
        self.assertEqual(second._context["subject"], initial._context["subject"])
        self.assertEqual(len(second._context["scan"]["reviewed_regions"]), 1)
        debt = next(item for item in ar.load_queue(store) if item._context["task"] == "subject_gap")
        self.assertEqual(debt._context["gap"]["unshown_target_regions"][0]["start"], ap._EXPANSION_CHARS)

    def test_region_review_requires_declaration_and_whole_region_uncertainty(self):
        store, _ = self.start()
        child = self.next_scan(store)
        absent = self.judgment(child, "unresolved")
        self.assertTrue(ar.submit_judgments(store, [absent])["errors"])
        partial = self.scan_judgment(child)
        target = child._context["rows"][0]["target"]
        partial["relations"][0]["target_segments"] = [dict(start=target["start"], end=target["start"]+1, exact=target["text"][:1])]
        self.assertTrue(ar.submit_judgments(store, [partial])["errors"])
        omitted = self.scan_judgment(child, "omitted")
        self.assertTrue(ar.submit_judgments(store, [omitted])["errors"])
        declaration = self.scan_judgment(child)
        declaration["reviewed_region"]["complete_region_reviewed"] = False
        self.assertTrue(ar.submit_judgments(store, [declaration])["errors"])
        for field, value in (("complete_region_reviewed", 1), ("start", float(target["start"]))):
            declaration = self.scan_judgment(child)
            declaration["reviewed_region"][field] = value
            self.assertTrue(ar.submit_judgments(store, [declaration])["errors"])
        self.assertIn(child.id, {item.id for item in ar.load_queue(store)})

    def test_forged_prior_coverage_or_missing_parent_receipt_never_completes_region(self):
        store, _ = self.start()
        child = self.next_scan(store)
        for field, value in (("reviewed_regions", []), ("parent_review_item_id", "arp:fake"),
                             ("source_id", "occ:fake"), ("sense_id", "sense:fake"),
                             ("target_page_sha256", "0" * 64)):
            with self.subTest(field=field):
                context = deepcopy(child._context)
                context["scan"][field] = value
                forged = ap._item(child.term, child.language, [], "pending", context, "Forged scan")
                ar.enqueue(store, [forged])
                self.assertTrue(ar.submit_judgments(store, [self.scan_judgment(forged)])["errors"])
        receipts = ar._read_json(ap._receipt_path(store))
        receipts["items"].pop(child._context["scan"]["parent_review_item_id"])
        ar._write_json(ap._receipt_path(store), receipts)
        self.assertTrue(ar.submit_judgments(store, [self.scan_judgment(child)])["errors"])

    def test_target_version_change_resets_debt_without_accumulating_old_regions(self):
        store, initial = self.start()
        child = self.next_scan(store)
        old_target = child._context["rows"][0]["target"]
        dbstore.upsert_web_pages(store, "fixture", [dict(id=old_target["page_id"], source="fixture", language="zh_hans", trust="B",
            kind="event_story", text="\u7532\uff1a\u5fc5\u987b\u8003\u8651\u8fd9\u4ef6\u4e8b\u3002\n\u7532\uff1a" + "\u66f4\u591a\u8bdd\u9898\u3002" * 6500)])
        self.assertTrue(ar.submit_judgments(store, [self.scan_judgment(child)])["errors"])
        ar.export_for_agent(store, self.base / "changed.txt", limit=0)
        debts = [item for item in ar.load_queue(store) if item._context["task"] == "subject_gap"]
        self.assertEqual(len(debts), 1)
        self.assertNotEqual(debts[0]._context["gap"]["target_page_sha256"], old_target["sha256"])
        self.assertNotIn("reviewed_regions", debts[0]._context["gap"])
        self.assertEqual(debts[0]._context["subject"], initial._context["subject"])
        self.assertNotIn(child.id, {item.id for item in ar.load_queue(store)})
        new_work = [item for item in ar.load_queue(store) if item._context["task"] == "occurrence" and "scan" not in item._context]
        self.assertEqual(len(new_work), 1)
        self.assertEqual(new_work[0]._context["focus"]["source"], initial._context["subject"]["source"])


if __name__ == "__main__":
    unittest.main()
