"""Synthetic receipt and render regressions for speech-act boundary discovery.

Exact saved observations test grounding and lifecycle, not an LLM's linguistic
judgment or exhaustive acquisition. No historical trial labels are consumed.
"""
from pathlib import Path
import json
import sys
import tempfile
import unittest


ROOT = next(path for path in Path(__file__).resolve().parents
            if (path / "sekaisync").is_dir())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync import (
    agent_packets as packets, agent_review as review, dbstore, source_audits,
    span_subjects, termindex,
)


class DiscoverySpeechActBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.serial = 0

    def fixture(self, text, version=1):
        self.serial += 1
        store = self.path / ("store-" + str(self.serial))
        if version == 1:
            dbstore.initialize(store)
        else:
            dbstore.initialize_new_store(store, target_version=version)
        source = dict(id="web:synthetic:en:event_story:9401:1", source="synthetic",
                      language="en", kind="event_story", trust="B",
                      canonical_key="event_story:en:9401:1", text=text)
        target = dict(id="web:synthetic:ja:event_story:9401:1", source="synthetic",
                      language="ja", kind="event_story", trust="B",
                      canonical_key="event_story:ja:9401:1",
                      text="Guide: TARGET_PRIVATE is never source discovery material.")
        dbstore.upsert_web_pages(store, "synthetic", [source, target])
        groups = termindex.group_pages_by_story([source, target])
        items, _ = packets._prepare_scrub_review(
            store, groups, sorted(groups), [], {}, "en", ["ja"])
        review.enqueue(store, items)
        root, = [item for item in items if item.kind == "discovery"]
        return store, root, source

    def literal(self, item, exact):
        row = next(row for row in item._context["rows"] if exact in row["source"]["text"])
        start = row["source"]["start"] + row["source"]["text"].index(exact)
        return dict(kind="literal", evidence_id=row["id"], canonical=exact,
                    segments=[dict(start=start, end=start + len(exact), exact=exact)])

    def expected(self, item, spec):
        row = next(row for row in item._context["rows"] if row["id"] == spec["evidence_id"])
        return span_subjects._literal(row["source"], row["story_key"],
                                      spec["canonical"], spec["segments"])

    def submit(self, store, item, **answer):
        return review.submit_judgments(store, [dict(id=item.id, decision="accept", **answer)])

    def audit(self, store):
        audit, = [item for item in review.load_queue(store)
                  if source_audits._is_audit(item._context)]
        self.assertFalse(source_audits._is_saturated(audit._context))
        self.assertNotIn("continuation", audit._context)
        return audit

    def saved(self, store, item):
        receipt = packets._receipts(store)[item.id]
        self.assertEqual(receipt["review_context"], item._context)
        return json.loads(receipt["value"])

    def rendered_export(self, store):
        path = self.path / ("export-" + str(self.serial) + ".txt")
        review.export_for_agent(store, path, limit=0)
        return path.read_text(encoding="utf-8")

    def boundary_line(self, rendered):
        lines = [line for line in rendered.splitlines() if line.startswith("discovery_boundaries:")]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_normal_export_renders_speech_act_boundary_contract(self):
        store, _root, _page = self.fixture("Mira: Well, Lina, should we open the window?")
        rendered = self.rendered_export(store)
        line = self.boundary_line(rendered)
        for rule in ("complete main proposition or question", "speech-act envelope", "interjections",
                     "discourse", "vocative", "pause-bearing", "pause-free", ". ! ?",
                     "U+3002", "U+FF01", "U+FF1F", "all internal punctuation, quotation marks",
                     "punctuation-bearing", "independent main clauses", "implicit words",
                     "arbitrary punctuation trims", "fragments"):
            with self.subTest(rule=rule):
                self.assertIn(rule, line)
        self.assertNotIn("TARGET_PRIVATE", rendered)

    def test_audit_export_renders_same_boundary_contract(self):
        store, root, _page = self.fixture("Mira: Well, Lina, should we open the window?")
        normal_line = self.boundary_line(self.rendered_export(store))
        core = self.literal(root, "should we open the window")
        self.assertEqual(self.submit(store, root, subjects=[core])["errors"], [])
        audit = self.audit(store)
        rendered = self.rendered_export(store)
        self.assertEqual(self.boundary_line(rendered), normal_line)
        self.assertIn("source_boundary_audit_inherited:", rendered)
        self.assertIn("should we open the window", rendered)
        self.assertNotIn("TARGET_PRIVATE", "\n".join(packets._render_context(audit)))
        self.assertNotIn(audit.id, packets._receipts(store))

    def test_normal_typed_receipts_preserve_attached_envelope_core_and_force_layers(self):
        envelope = "Well, Lina, shouldn't we open the window"
        core = "shouldn't we open the window"
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, page = self.fixture("Mira: " + envelope + "?", version)
                specs = [self.literal(root, exact) for exact in (envelope, core, envelope + "?")]
                self.assertEqual(self.submit(store, root, subjects=specs)["errors"], [])
                value = self.saved(store, root)
                self.assertEqual(set(value), {"terms", "subjects"})
                self.assertEqual(value["terms"], [])
                self.assertEqual(value["subjects"], [self.expected(root, spec) for spec in specs])
                self.assertEqual([subject["canonical"] for subject in value["subjects"]],
                                 [envelope, core, envelope + "?"])
                for subject in value["subjects"]:
                    self.assertEqual(subject["source"]["page_id"], page["id"])
                self.assertEqual(len({subject["id"] for subject in value["subjects"]}), 3)

    def test_legacy_receipts_keep_punctuation_layers_without_automatic_stripping(self):
        envelope, core = "Ah, Lina, we can open the window", "we can open the window"
        for terminator in (".", "!", "?", "\u3002", "\uff01", "\uff1f"):
            with self.subTest(terminator=terminator):
                store, root, _page = self.fixture("Mira: " + envelope + terminator)
                terms = [envelope, core, envelope + terminator]
                self.assertEqual(self.submit(store, root, terms=terms)["errors"], [])
                self.assertEqual(self.saved(store, root), terms)
                self.assertEqual(self.audit(store)._context["source_boundary_audit"]["excluded_terms"],
                                 sorted(terms))

    def test_audit_inherits_subject_exclusions_but_composite_remains_fresh(self):
        envelope, core = "Well, Lina, shouldn't we open the window", "shouldn't we open the window"
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _page = self.fixture("Mira: " + envelope + "?", version)
                selected = self.literal(root, core)
                inherited = self.expected(root, selected)
                self.assertEqual(self.submit(store, root, subjects=[selected])["errors"], [])
                audit = self.audit(store)
                marker = audit._context["source_boundary_audit"]
                self.assertEqual(marker["terminal_parent_id"], root.id)
                self.assertEqual(marker["ordinary_ancestor_ids"], [])
                self.assertEqual(marker["excluded_terms"], [])
                self.assertEqual(marker["excluded_subject_ids"], [inherited["id"]])
                self.assertEqual(marker["inherited_subjects"][0]["source_segments"], selected["segments"])
                repeated = self.submit(store, audit, subjects=[self.literal(audit, core)])
                self.assertTrue(any("already discovered" in error for error in repeated["errors"]))
                self.assertNotIn(audit.id, packets._receipts(store))
                self.assertEqual(self.audit(store).id, audit.id)
                specs = [self.literal(audit, exact) for exact in (envelope, envelope + "?")]
                self.assertEqual(self.submit(store, audit, subjects=specs)["errors"], [])
                self.assertEqual(self.saved(store, audit)["subjects"],
                                 [self.expected(audit, spec) for spec in specs])
                queue_ids = [item.id for item in review.load_queue(store)]
                self.assertEqual(self.submit(store, audit, subjects=specs)["errors"], [])
                self.assertEqual(self.submit(store, root, subjects=[selected])["errors"], [])
                self.assertEqual([item.id for item in review.load_queue(store)], queue_ids)
                self.assertEqual(set(packets._receipts(store)), {root.id, audit.id})

    def test_audit_inherits_legacy_surfaces_and_replays_without_regenerating(self):
        envelope, core = "Ah, Lina, we can open the window", "we can open the window"
        store, root, _page = self.fixture("Mira: " + envelope + "!")
        terms = [envelope, core, envelope + "!"]
        self.assertEqual(self.submit(store, root, terms=terms)["errors"], [])
        audit = self.audit(store)
        marker = audit._context["source_boundary_audit"]
        self.assertEqual(marker["excluded_terms"], sorted(terms))
        self.assertEqual(marker["excluded_subject_ids"], [])
        for term in terms:
            with self.subTest(term=term):
                rejected = self.submit(store, audit, terms=[term])
                self.assertTrue(any("already submitted" in error for error in rejected["errors"]))
                self.assertNotIn(audit.id, packets._receipts(store))
        self.assertEqual(self.submit(store, audit, terms=["Lina"])["errors"], [])
        self.assertEqual(self.saved(store, audit), ["Lina"])
        queue_ids = [item.id for item in review.load_queue(store)]
        self.assertEqual(self.submit(store, root, terms=terms)["errors"], [])
        self.assertEqual(self.submit(store, audit, terms=["Lina"])["errors"], [])
        self.assertEqual([item.id for item in review.load_queue(store)], queue_ids)
        self.assertFalse(any(source_audits._is_audit(item._context) for item in review.load_queue(store)))

    def test_raw_lf_crlf_internal_quote_and_code_point_geometry_replay(self):
        core = 'didn\'t you say "wait!" before we left'
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                envelope = "Well..." + newline + "Lina, " + core
                text = "Guide: The \U0001f4e6 is ready." + newline + "Mira: " + envelope + "?"
                store, root, page = self.fixture(text)
                bad_legacy = self.submit(store, root, terms=[envelope])
                self.assertTrue(bad_legacy["errors"])
                self.assertNotIn(root.id, packets._receipts(store))
                specs = [self.literal(root, exact) for exact in (envelope, core, envelope + "?")]
                self.assertEqual(self.submit(store, root, subjects=specs)["errors"], [])
                subjects = self.saved(store, root)["subjects"]
                for spec, subject in zip(specs, subjects):
                    part, = subject["source"]["segments"]
                    self.assertEqual(part, spec["segments"][0])
                    self.assertEqual(part["start"], text.index(spec["canonical"]))
                    self.assertEqual(part["end"] - part["start"], len(spec["canonical"]))
                    self.assertEqual(text[part["start"]:part["end"]], spec["canonical"])
                    self.assertEqual(subject["source"]["page_id"], page["id"])
                    with dbstore.connect(store) as conn:
                        self.assertEqual(span_subjects._validate(conn, subject), subject)
                self.assertEqual(subjects[0]["canonical"], envelope)
                self.assertIn(newline, subjects[0]["canonical"])
                self.assertNotIn(newline, subjects[1]["canonical"])
                self.assertIn('"wait!"', subjects[0]["canonical"])
                self.assertFalse(subjects[0]["canonical"].endswith("?"))
                self.assertTrue(subjects[2]["canonical"].endswith("?"))

    def test_fragment_receipts_do_not_infer_composite_or_neighbouring_clause(self):
        envelope = "Well, Lina, we can open the window"
        neighbour = "The rain has stopped"
        store, root, _page = self.fixture("Mira: Yes! " + envelope + ". " + neighbour + ".")
        specs = [self.literal(root, exact) for exact in ("Lina", "can open the window")]
        self.assertEqual(self.submit(store, root, subjects=specs)["errors"], [])
        self.assertEqual([subject["canonical"] for subject in self.saved(store, root)["subjects"]],
                         ["Lina", "can open the window"])
        audit = self.audit(store)
        additional = [self.literal(audit, exact) for exact in (envelope, neighbour)]
        inherited_ids = set(audit._context["source_boundary_audit"]["excluded_subject_ids"])
        self.assertTrue(all(self.expected(audit, spec)["id"] not in inherited_ids for spec in additional))
        self.assertEqual(self.submit(store, audit, subjects=additional)["errors"], [])
        self.assertEqual([subject["canonical"] for subject in self.saved(store, audit)["subjects"]],
                         [envelope, neighbour])
        self.assertFalse(any(subject["canonical"].startswith("Yes!")
                             for subject in self.saved(store, audit)["subjects"]))

    def test_existing_answer_fields_and_observation_limits_remain_unchanged(self):
        store, root, _page = self.fixture("Mira: Lina, don't close the window!")
        spec = self.literal(root, "Lina, don't close the window")
        self.assertEqual(set(spec), {"kind", "evidence_id", "canonical", "segments"})
        self.assertEqual(packets._DISCOVERY_TERMS, 200)
        self.assertEqual(packets._TURN_CHARS, 1800)
        self.assertTrue(packets._valid_surface("x" * 80))
        self.assertFalse(packets._valid_surface("x" * 81))
        self.assertTrue(self.submit(store, root, terms=["window"] * 201)["errors"])
        self.assertTrue(self.submit(store, root, subjects=[spec] * 201)["errors"])
        extra_field = dict(spec, speech_act="request")
        self.assertTrue(self.submit(store, root, subjects=[extra_field])["errors"])
        self.assertNotIn(root.id, packets._receipts(store))
        self.assertEqual(self.submit(store, root, subjects=[spec])["errors"], [])
        rendered = "\n".join(packets._render_context(self.audit(store)))
        self.assertIn("1-80 character", rendered)
        self.assertIn("1800 raw Unicode code points", rendered)
        self.assertIn("Each list permits at most 200 observations", rendered)
        self.assertEqual(set(self.saved(store, root)), {"terms", "subjects"})

    def test_invented_implicit_words_and_aliases_are_not_saved(self):
        store, root, _page = self.fixture("Mira: Lina, don't close the window!")
        spec = self.literal(root, "don't close the window")
        invented = dict(spec, canonical="you must not close the window")
        self.assertTrue(self.submit(store, root, subjects=[invented])["errors"])
        self.assertTrue(self.submit(store, root, terms=["you must not close the window"])["errors"])
        segmented = dict(kind="segmented", evidence_id=spec["evidence_id"],
                         segments=[self.literal(root, "Lina")["segments"][0], spec["segments"][0]],
                         canonical="Lina must not close the window")
        self.assertTrue(self.submit(store, root, subjects=[segmented])["errors"])
        self.assertNotIn(root.id, packets._receipts(store))
        self.assertEqual(self.submit(store, root, subjects=[spec])["errors"], [])
        self.assertEqual(self.saved(store, root)["subjects"][0]["canonical"], "don't close the window")


if __name__ == "__main__":
    unittest.main()
