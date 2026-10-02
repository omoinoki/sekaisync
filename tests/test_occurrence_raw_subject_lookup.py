"""Raw typed display queries are exact; exterior formatting is not an alias."""
import json
import unittest

from tests import test_agent_multiline_subjects as helpers
from sekaisync import agent_packets as packets, agent_review as review, dbstore, occurrence_store as ledger
from sekaisync.core import SekaiSyncCore


class RawSubjectLookupTests(unittest.TestCase):
    def accepted(self, version, raw, fragments, kind, canonical=None):
        helper = helpers.AgentMultilineSubjectTests(methodName="runTest")
        helper.STORE_VERSION = version
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        store, discovery, row = helper.fixture("A: We should " + raw + " together.")
        entry = dict(kind=kind, evidence_id=row["id"], segments=helper.segments(row["source"], fragments))
        if kind == "literal":
            entry["canonical"] = raw if canonical is None else canonical
        subject = helper.occurrence(store, discovery, entry)
        return store, subject

    def assert_visible(self, store, subject, query=None):
        canonical = subject["canonical"]
        core = SekaiSyncCore(store)
        with core.request_view():
            penetration = core.term_penetrate(canonical if query is None else query,
                                             story_key="event:999:1", languages=["en", "zh_hans"])
            generic = core.query(canonical if query is None else query, include_web=False)
        self.assertIsNotNone(penetration)
        self.assertEqual(len(generic["terms"]), 1)
        for term in (penetration["term"], generic["terms"][0]):
            self.assertEqual(term["canonical"], canonical)
            self.assertEqual(term["names"], {} if subject["kind"] == "segmented" else {"en": canonical})
        positions = {position["language"]: position for position in generic["terms"][0]["positions"]}
        for entry in (penetration["per_language"]["en"], positions["en"]):
            if subject["kind"] == "segmented":
                self.assertEqual(entry["term"], "")
                label = "; raw selected fragments (Unicode code points, end-exclusive): "
                self.assertEqual(json.loads(entry["note"].split(label, 1)[1]), subject["source"]["segments"])
            else:
                self.assertEqual(entry["term"], canonical)
        for entry in (penetration["per_language"]["zh_hans"], positions["zh_hans"]):
            self.assertEqual(entry["term"], helpers.TARGET)
        with dbstore.connect(store) as conn:
            relation, = ledger._read_relations(conn)
            self.assertEqual(relation["grounding"]["context"]["subject"], subject)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def assert_absent(self, store, query):
        core = SekaiSyncCore(store)
        self.assertIsNone(core.term_penetrate(query, story_key="event:999:1"))
        self.assertEqual(core.query(query, include_web=False)["terms"], [])

    def test_actual_segmented_edge_line_endings_survive_exact_public_lookup(self):
        for version in (1, 2, 3):
            for newline in ("\n", "\r\n"):
                for raw, fragments in ((newline + "build confidence", [newline + "build", "confidence"]),
                                       ("build confidence" + newline, ["build", "confidence" + newline])):
                    with self.subTest(version=version, raw=repr(raw)):
                        store, subject = self.accepted(version, raw, fragments, "segmented")
                        receipts = packets._receipts(store)
                        self.assert_visible(store, subject)
                        self.assert_absent(store, subject["canonical"].strip())
                        self.assertEqual(packets._receipts(store), receipts)

    def test_existing_literal_edge_aliases_remain_queryable_only_by_registered_display(self):
        for version in (1, 2, 3):
            for canonical in (" build confidence ", "\tbuild confidence\t"):
                with self.subTest(version=version, canonical=repr(canonical)):
                    store, subject = self.accepted(version, "build confidence", ["build confidence"],
                                                   "literal", canonical)
                    self.assert_visible(store, subject)
                    self.assert_absent(store, canonical.strip())

    def test_existing_caller_padding_does_not_collapse_internal_literal_whitespace(self):
        for raw in ("build\nconfidence", "build\r\nconfidence", "build  confidence", "build\u00a0confidence"):
            with self.subTest(raw=repr(raw)):
                store, subject = self.accepted(1, raw, [raw], "literal")
                self.assert_visible(store, subject, " \t" + raw + "\t ")
                self.assert_absent(store, "build confidence")

    def test_stale_edged_subject_is_known_but_never_displayed(self):
        store, subject = self.accepted(1, "build confidence\r\n", ["build", "confidence\r\n"], "segmented")
        self.assert_visible(store, subject)
        canonical = subject["canonical"]
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=text || ' changed' WHERE source=? AND id=?",
                         (subject["source"]["page_source"], subject["source"]["page_id"]))
            conn.commit()
            known, relations = ledger._query_relations(conn, canonical)
            self.assertTrue(known)
            self.assertEqual(relations, [])
            self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)
        self.assert_absent(store, canonical)

    def test_exact_and_caller_trim_matches_retain_distinct_sense_ambiguity(self):
        helper = helpers.AgentMultilineSubjectTests(methodName="runTest")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        store, discovery, row = helper.fixture("A: We should build confidence together.")
        parts = helper.segments(row["source"], ["build confidence"])
        canonicals = ("build confidence", " build confidence ")
        entries = [dict(kind="literal", evidence_id=row["id"], canonical=canonical, segments=parts)
                   for canonical in canonicals]
        self.assertEqual(helper.submit(store, discovery, subjects=entries)["errors"], [])
        pending = [item for item in review.load_queue(store) if item._context.get("task") == "occurrence"]
        self.assertEqual(len(pending), 2)
        for item in pending:
            evidence = item._context["rows"][0]
            relation = dict(evidence_id=evidence["id"], source_segments=parts,
                            target_segments=helper.segments(evidence["target"], [helpers.TARGET]),
                            sense_key="confidence-building", sense_gloss="Synthetic selected interpretation",
                            kind="lexical", rationale="Mechanical collision fixture, not semantic validation")
            self.assertEqual(helper.submit(store, item, relations=[relation])["errors"], [])
        core = SekaiSyncCore(store)
        plain = core.query(canonicals[0], include_web=False)["terms"]
        self.assertEqual([term["canonical"] for term in plain], [canonicals[0]])
        self.assertIsNotNone(core.term_penetrate(canonicals[0], story_key="event:999:1"))
        self.assertIsNone(core.term_penetrate(canonicals[1], story_key="event:999:1"))
        edged = core.query(canonicals[1], include_web=False)["terms"]
        self.assertEqual({term["canonical"] for term in edged}, set(canonicals))
        self.assertEqual(len({term["id"] for term in edged}), 2)


if __name__ == "__main__":
    unittest.main()
