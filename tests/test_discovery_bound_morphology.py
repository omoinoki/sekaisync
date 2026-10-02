"""Nine independent synthetic prompt/geometry/receipt regression tests.

These tests do not prove semantic or morphological recognition correctness.
All selections are supplied by the test author, not discovered by a model.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import (
    agent_packets as packets, agent_review as review, dbstore,
    occurrence_store, source_audits, span_subjects, termindex,
)


class BoundMorphologyDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.serial = 0

    def fixture(self, text, language="en", version=1):
        self.serial += 1
        store = self.base / ("synthetic-" + str(self.serial))
        if version == 1:
            dbstore.initialize(store)
        else:
            dbstore.initialize_new_store(store, target_version=version)
        target_language = "ja" if language == "en" else "en"
        source_name = "synthetic_bound_morphology"
        page = dict(id=f"web:{source_name}:{language}:event_story:738406:1",
                    source=source_name, language=language, kind="event_story",
                    canonical_key=f"event_story:{language}:738406:1", trust="B", text=text)
        target = dict(id=f"web:{source_name}:{target_language}:event_story:738406:1",
                      source=source_name, language=target_language, kind="event_story",
                      canonical_key=f"event_story:{target_language}:738406:1", trust="B",
                      text="Reader: SYNTHETIC_TARGET_NOT_FOR_SOURCE_REVIEW.")
        dbstore.upsert_web_pages(store, source_name, [page, target])
        groups = termindex.group_pages_by_story([page, target])
        items, _ = packets._prepare_scrub_review(
            store, groups, sorted(groups), [], {}, language, [target_language])
        review.enqueue(store, items)
        root, = [item for item in items if item.kind == "discovery"]
        return store, root, page

    def literal(self, item, exact, start=None):
        row = item._context["rows"][0]
        view = row["source"]
        position = view["start"] + view["text"].index(exact) if start is None else start
        return dict(kind="literal", evidence_id=row["id"], canonical=exact,
                    segments=[dict(start=position, end=position + len(exact), exact=exact)])

    def submit(self, store, item, subjects):
        return review.submit_judgments(
            store, [dict(id=item.id, decision="accept", subjects=subjects)])

    def receipt_subjects(self, store, item):
        receipt = packets._receipts(store)[item.id]
        self.assertEqual(receipt["decision"], "accept")
        self.assertEqual(receipt["scope_id"], item._context["scope_id"])
        self.assertEqual(receipt["review_context"], item._context)
        value = json.loads(receipt["value"])
        self.assertEqual(value["terms"], [])
        self.assertEqual(set(value), {"terms", "subjects"})
        return value["subjects"]

    def accepted(self, store, item, specs):
        self.assertEqual(self.submit(store, item, specs)["errors"], [])
        subjects = self.receipt_subjects(store, item)
        self.assertEqual(len(subjects), len(specs))
        self.assertEqual(len({subject["id"] for subject in subjects}), len(specs))
        for spec, subject in zip(specs, subjects):
            self.assertEqual(subject["canonical"], spec["canonical"])
            self.assertEqual(subject["source"]["segments"], spec["segments"])
            self.assertEqual(subject["canonical_parts"], [part["exact"] for part in spec["segments"]])
            with dbstore.connect(store) as conn:
                self.assertEqual(span_subjects._validate(conn, subject), subject)
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(occurrence_store._read_relations(conn), [])
        return subjects

    def test_ordinary_prompt_preserves_full_forms_and_adds_contextual_bound_layers(self):
        _, root, _ = self.fixture("Surveyor: It'll shimmer beside the harbor.")
        rendered = "\n".join(packets._render_context(root))
        for instruction in (
                "complete contractions and inflected predicates AND",
                "contractions and tense/politeness inflection intact AND",
                "additional exact observations", "negation/genitive clitics",
                "case/topic/quotative functions", "complete ending-plus-auxiliary constructions",
                "determiner-bearing or case/topic-marked reference",
                "actual contextual grammatical role", "arbitrary stem/suffix split",
                "never invent a lemma", "absent uncontracted alias",
                "an inner layer does not replace the complete form", "LF/CRLF"):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, rendered)
        self.assertNotIn("separate only genuinely detachable discourse material", rendered)
        self.assertNotIn("SYNTHETIC_TARGET_NOT_FOR_SOURCE_REVIEW", rendered)
        self.assertIn("Each list permits at most 200 observations", rendered)
        self.assertIn("up to 1800 raw Unicode code points", rendered)

    def test_second_audit_prompt_rechecks_bound_functions_and_marked_nominals(self):
        store, root, _ = self.fixture("Keeper: The pilot's lantern isn't dim.")
        self.accepted(store, root, [self.literal(root, "The pilot's lantern isn't dim")])
        audit, = [item for item in review.load_queue(store)
                  if source_audits._is_audit(item._context)]
        contract = "\n".join(source_audits._render_contract(audit))
        for instruction in (
                "complete original contractions and inflected predicates AND",
                "recognized contextual bound grammatical layers", "clitic auxiliaries",
                "negation/genitive clitics", "case/topic/quotative functions",
                "complete ending-plus-auxiliary constructions", "lexical nominal heads",
                "case/topic-marked references", "actual contextual grammatical role",
                "not a matching suffix string", "absent uncontracted alias",
                "Neither an accepted answer nor its receipt proves semantic completeness"):
            with self.subTest(instruction=instruction):
                self.assertIn(instruction, contract)
        rendered = "\n".join(packets._render_context(audit))
        self.assertNotIn("SYNTHETIC_TARGET_NOT_FOR_SOURCE_REVIEW", rendered)
        self.assertIn("source_boundary_audit_inherited:", rendered)

    def test_full_english_contractions_and_overlapping_auxiliary_negation_genitive_clitics(self):
        store, root, _ = self.fixture("Cartographer: Nela's chart hasn't faded; it'll last.")
        exacts = ["Nela's", "'s", "hasn't", "hasn't faded", "n't", "it'll", "'ll"]
        subjects = self.accepted(store, root, [self.literal(root, exact) for exact in exacts])
        by = {subject["canonical"]: subject for subject in subjects}
        for inner, outer in (("'s", "Nela's"), ("n't", "hasn't"), ("'ll", "it'll")):
            small, large = by[inner]["source"]["segments"][0], by[outer]["source"]["segments"][0]
            self.assertLess(large["start"], small["start"])
            self.assertEqual(large["end"], small["end"])
            self.assertNotEqual(by[inner]["id"], by[outer]["id"])

    def test_korean_case_topic_quotative_functions_overlap_heads_and_marked_references(self):
        text = "Analyst: \uc120\uc778\uc7a5\uc740 \uadf8\ub298\uc5d0\uc11c \uc270\ub2e4\uace0 \uae30\ub85d\ud588\uc5b4\uc694."
        store, root, _ = self.fixture(text, "ko")
        exacts = ["\uc120\uc778\uc7a5", "\uc120\uc778\uc7a5\uc740", "\uc740", "\uadf8\ub298", "\uadf8\ub298\uc5d0\uc11c", "\uc5d0\uc11c", "\uc270\ub2e4\uace0", "\ub2e4\uace0", "\uae30\ub85d\ud588\uc5b4\uc694"]
        subjects = self.accepted(store, root, [self.literal(root, exact) for exact in exacts])
        self.assertEqual([subject["canonical"] for subject in subjects], exacts)
        by = {subject["canonical"]: subject["source"]["segments"][0] for subject in subjects}
        for head, marked in (("\uc120\uc778\uc7a5", "\uc120\uc778\uc7a5\uc740"), ("\uadf8\ub298", "\uadf8\ub298\uc5d0\uc11c")):
            self.assertEqual(by[head]["start"], by[marked]["start"])
            self.assertLess(by[head]["end"], by[marked]["end"])
        self.assertEqual(by["\ub2e4\uace0"]["end"], by["\uc270\ub2e4\uace0"]["end"])

    def test_complete_korean_ending_plus_auxiliary_keeps_full_inflected_predicate(self):
        text = "Curator: \ubcbd\ud654\ub97c \ubcf4\uace0 \uc2f6\uc5c8\uc5b4\uc694."
        store, root, _ = self.fixture(text, "ko")
        exacts = ["\ubcf4\uace0 \uc2f6\uc5c8\uc5b4\uc694", "\uace0 \uc2f6\uc5c8\uc5b4\uc694", "\uc2f6\uc5c8\uc5b4\uc694", "\ubcbd\ud654", "\ubcbd\ud654\ub97c", "\ub97c"]
        subjects = self.accepted(store, root, [self.literal(root, exact) for exact in exacts])
        full, construction, auxiliary = [subject["source"]["segments"][0] for subject in subjects[:3]]
        self.assertLess(full["start"], construction["start"])
        self.assertLess(construction["start"], auxiliary["start"])
        self.assertEqual(full["end"], construction["end"])
        self.assertEqual(construction["end"], auxiliary["end"])

    def test_true_unicode_codepoint_offsets_decomposed_spelling_and_lf_survive(self):
        text = "Narrator: \U0001f33f Cafe\u0301 can't\nstay frozen."
        store, root, _ = self.fixture(text)
        exacts = ["Cafe\u0301", "can't\nstay frozen", "can't", "n't"]
        subjects = self.accepted(store, root, [self.literal(root, exact) for exact in exacts])
        by = {subject["canonical"]: subject for subject in subjects}
        part = by["n't"]["source"]["segments"][0]
        self.assertEqual(part["start"], text.index("n't"))
        self.assertNotEqual(part["start"], len(text[:part["start"]].encode("utf-16-le")) // 2)
        self.assertEqual(text[part["start"]:part["end"]], "n't")
        self.assertEqual(by["Cafe\u0301"]["canonical"], "Cafe\u0301")
        self.assertNotEqual(by["Cafe\u0301"]["canonical"], "Caf\u00e9")
        self.assertEqual(by["can't\nstay frozen"]["canonical_parts"], ["can't\nstay frozen"])
        self.assertIn("\n", by["can't\nstay frozen"]["source"]["segments"][0]["exact"])

    def test_audit_inherits_complete_subject_and_normal_receipts_add_distinct_inner_layers(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _ = self.fixture("Restorer: We'll polish the bronze bell.", version=version)
                outer, = self.accepted(store, root, [self.literal(root, "We'll polish the bronze bell")])
                audit, = [item for item in review.load_queue(store)
                          if source_audits._is_audit(item._context)]
                marker = audit._context["source_boundary_audit"]
                self.assertEqual(marker["terminal_parent_id"], root.id)
                self.assertEqual(marker["excluded_subject_ids"], [outer["id"]])
                self.assertEqual(marker["inherited_subjects"], [dict(
                    id=outer["id"], kind="literal", canonical=outer["canonical"],
                    source_segments=outer["source"]["segments"])])
                self.assertTrue(self.submit(store, audit, [self.literal(audit, outer["canonical"])])["errors"])
                self.assertNotIn(audit.id, packets._receipts(store))
                destination = review.export_for_agent(store, self.base / f"audit-{version}.txt", limit=0)
                rendered = destination.read_text(encoding="utf-8")
                audit_block = next(block for block in rendered.split("\n## ")
                                   if f"id={audit.id} " in block)
                self.assertIn(outer["id"], audit_block)
                self.assertNotIn("SYNTHETIC_TARGET_NOT_FOR_SOURCE_REVIEW", audit_block)
                self.accepted(store, audit, [self.literal(audit, exact) for exact in ("We'll", "'ll", "bell", "the bronze bell")])
                self.assertEqual(set(packets._receipts(store)), {root.id, audit.id})
                self.assertFalse(any(source_audits._is_audit(item._context) for item in review.load_queue(store)))

    def test_invented_uncontracted_lemma_and_segmented_canonicals_are_rejected(self):
        store, root, _ = self.fixture("Smith: Cafe\u0301 vessels won't dry.")
        for exact, alias in (("won't", "will not"), ("dry", "dries")):
            with self.subTest(alias=alias):
                spec = self.literal(root, exact)
                spec["canonical"] = alias
                self.assertTrue(self.submit(store, root, [spec])["errors"])
                self.assertNotIn(root.id, packets._receipts(store))
                self.assertIn(root.id, {item.id for item in review.load_queue(store)})
        row = root._context["rows"][0]
        invented = dict(kind="segmented", evidence_id=row["id"], canonical="will not dry",
                        segments=[self.literal(root, exact)["segments"][0] for exact in ("won't", "dry")])
        self.assertTrue(self.submit(store, root, [invented])["errors"])
        self.assertNotIn(root.id, packets._receipts(store))
        self.accepted(store, root, [self.literal(root, "won't"), self.literal(root, "n't")])

    def test_stale_and_shifted_raw_geometry_cannot_commit_an_inner_clitic(self):
        store, root, page = self.fixture("Botanist: She's trimming the fern.")
        good = self.literal(root, "'s")
        shifted = deepcopy(good)
        shifted["segments"][0]["start"] += 1
        shifted["segments"][0]["end"] += 1
        self.assertTrue(self.submit(store, root, [shifted])["errors"])
        self.assertNotIn(root.id, packets._receipts(store))
        changed = dict(page, text="Botanist: He's trimming the fern.")
        dbstore.upsert_web_pages(store, page["source"], [changed])
        self.assertTrue(self.submit(store, root, [good])["errors"])
        self.assertNotIn(root.id, packets._receipts(store))
        self.assertIn(root.id, {item.id for item in review.load_queue(store)})
        with dbstore.connect(store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(occurrence_store._read_relations(conn), [])


if __name__ == "__main__":
    unittest.main()
