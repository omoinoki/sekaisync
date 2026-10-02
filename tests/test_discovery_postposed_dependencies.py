"""Synthetic host-contract and durable receipts for postposed dependencies.

The fixtures supply exact observations, not historical trial labels or an
automatic grammar judge. Receipt tests prove grounding, raw geometry and
lifecycle; only the rendered contract asks the host to judge attachment.
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


# Deliberately synthetic positive observations in all five source languages.
DEPENDENCIES = (
    ("en", "We should wait here", "Because the road is flooded", ". ", "."),
    ("en", "The package can go tomorrow", "If the bridge reopens", ". ", "."),
    ("ja", "\u660e\u65e5\u306b\u3057\u3088\u3046",
     "\u6a4b\u304c\u958b\u3051\u3070", "\u3002", "\u3002"),
    ("ko", "\uc5ec\uae30\uc11c \uae30\ub2e4\ub9ac\uc790",
     "\uae38\uc774 \uc7a0\uacbc\uc73c\ub2c8\uae4c", ". ", "."),
    ("zh_hans", "\u6211\u4eec\u5148\u7b49\u4e00\u7b49",
     "\u56e0\u4e3a\u6865\u8fd8\u6ca1\u5f00\u653e", "\u3002", "\u3002"),
    ("zh_hant", "\u660e\u5929\u518d\u51fa\u767c",
     "\u5982\u679c\u6a4b\u91cd\u65b0\u958b\u653e", "\u3002", "\u3002"),
)


class DiscoveryPostposedDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.serial = 0

    def fixture(self, text, language="en", version=3):
        self.serial += 1
        store = self.path / ("store-" + str(self.serial))
        if version == 1:
            dbstore.initialize(store)
        else:
            dbstore.initialize_new_store(store, target_version=version)
        target_language = "en" if language == "ja" else "ja"
        source = dict(id="web:synthetic:" + language + ":event_story:9502:1",
                      source="synthetic", language=language, kind="event_story", trust="B",
                      canonical_key="event_story:" + language + ":9502:1", text=text)
        target = dict(id="web:synthetic:" + target_language + ":event_story:9502:1",
                      source="synthetic", language=target_language, kind="event_story", trust="B",
                      canonical_key="event_story:" + target_language + ":9502:1",
                      text="Guide: TARGET_PRIVATE must never guide source boundaries.")
        dbstore.upsert_web_pages(store, "synthetic", [source, target])
        groups = termindex.group_pages_by_story([source, target])
        items, _ = packets._prepare_scrub_review(
            store, groups, sorted(groups), [], {}, language, [target_language])
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

    def saved(self, store, item):
        receipt = packets._receipts(store)[item.id]
        self.assertEqual(receipt["review_context"], item._context)
        return json.loads(receipt["value"])

    def audit(self, store):
        audit, = [item for item in review.load_queue(store)
                  if source_audits._is_audit(item._context)]
        self.assertFalse(source_audits._is_saturated(audit._context))
        self.assertNotIn("continuation", audit._context)
        return audit

    def export(self, store, name="source.txt"):
        path = self.path / (str(self.serial) + "-" + name)
        review.export_for_agent(store, path, limit=0)
        return path.read_text(encoding="utf-8")

    def boundary_line(self, rendered):
        lines = [line for line in rendered.splitlines() if line.startswith("discovery_boundaries:")]
        self.assertEqual(len(lines), 1)
        return lines[0]

    def test_source_only_export_uses_grammatical_not_orthographic_boundaries(self):
        for language, main, dependent, separator, terminal in DEPENDENCIES:
            with self.subTest(language=language, dependent=dependent):
                store, root, _page = self.fixture("Mira: " + main + separator + dependent + terminal,
                                                  language)
                before = root.to_dict()
                rendered = self.export(store)
                line = self.boundary_line(rendered)
                for rule in ("source-attested grammatical attachment and scope",
                             "not orthographic sentence breaks alone", "within the same utterance",
                             "postposed causal, explanatory, conditional or other subordinate clause",
                             "rather than forming a new independent speech act", "even after a period",
                             "full main-plus-dependent composite", "internal punctuation intact",
                             "meaningful detachable cores separately", "connective-looking word",
                             "thematic continuity", "alone does not prove grammatical dependence",
                             "outermost terminal sentence punctuation", "of the complete envelope",
                             "independent prior responses", "neighboring independent main clauses",
                             "regardless of punctuation or semantic relatedness",
                             "never fuse across speaker or turn boundaries", "arbitrary punctuation trims"):
                    with self.subTest(rule=rule):
                        self.assertIn(rule, line)
                self.assertNotIn("TARGET_PRIVATE", rendered)
                self.assertEqual(root.to_dict(), before)

    def test_multilingual_receipts_keep_composite_cores_and_optional_force_layer(self):
        for language, main, dependent, separator, terminal in DEPENDENCIES:
            with self.subTest(language=language, dependent=dependent):
                composite = main + separator + dependent
                store, root, page = self.fixture("Mira: " + composite + terminal, language)
                exacts = [composite, main, dependent, composite + terminal]
                specs = [self.literal(root, exact) for exact in exacts]
                self.assertEqual(self.submit(store, root, subjects=specs)["errors"], [])
                value = self.saved(store, root)
                self.assertEqual(set(value), {"terms", "subjects"})
                self.assertEqual(value["terms"], [])
                self.assertEqual(value["subjects"], [self.expected(root, spec) for spec in specs])
                self.assertEqual([subject["canonical"] for subject in value["subjects"]], exacts)
                self.assertEqual(len({subject["id"] for subject in value["subjects"]}), len(exacts))
                with dbstore.connect(store) as conn:
                    for subject in value["subjects"]:
                        self.assertEqual(span_subjects._validate(conn, subject), subject)
                        self.assertEqual(subject["source"]["page_id"], page["id"])
                        part, = subject["source"]["segments"]
                        self.assertEqual(page["text"][part["start"]:part["end"]], subject["canonical"])

    def test_audit_adds_missing_composite_without_replacing_inherited_core_v1_v2_v3(self):
        language, main, dependent, separator, terminal = DEPENDENCIES[3]
        composite = main + separator + dependent
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, root, _page = self.fixture("Mira: " + composite + terminal, language, version)
                normal_line = self.boundary_line(self.export(store))
                cores = [self.literal(root, exact) for exact in (main, dependent)]
                self.assertEqual(self.submit(store, root, subjects=cores)["errors"], [])
                audit = self.audit(store)
                marker = audit._context["source_boundary_audit"]
                self.assertEqual(marker["terminal_parent_id"], root.id)
                self.assertEqual(marker["ordinary_ancestor_ids"], [])
                self.assertEqual(marker["excluded_subject_ids"],
                                 sorted(self.expected(root, spec)["id"] for spec in cores))
                rendered = self.export(store, "audit.txt")
                self.assertEqual(self.boundary_line(rendered), normal_line)
                self.assertIn("source_boundary_audit_inherited:", rendered)
                self.assertNotIn("TARGET_PRIVATE", "\n".join(packets._render_context(audit)))
                repeated = self.submit(store, audit, subjects=[self.literal(audit, main)])
                self.assertTrue(any("already discovered" in error for error in repeated["errors"]))
                self.assertNotIn(audit.id, packets._receipts(store))
                specs = [self.literal(audit, exact) for exact in (composite, composite + terminal)]
                self.assertTrue(all(self.expected(audit, spec)["id"] not in marker["excluded_subject_ids"]
                                    for spec in specs))
                self.assertEqual(self.submit(store, audit, subjects=specs)["errors"], [])
                self.assertEqual([subject["canonical"] for subject in self.saved(store, audit)["subjects"]],
                                 [composite, composite + terminal])
                queue_ids = [item.id for item in review.load_queue(store)]
                self.assertEqual(self.submit(store, audit, subjects=specs)["errors"], [])
                self.assertEqual(self.submit(store, root, subjects=cores)["errors"], [])
                self.assertEqual([item.id for item in review.load_queue(store)], queue_ids)
                self.assertEqual(set(packets._receipts(store)), {root.id, audit.id})

    def test_continuation_retains_contract_and_composite_observation_is_fresh(self):
        main, dependent = "We should wait here", "Because the road is flooded"
        composite = main + ". " + dependent
        tokens = ["s" + str(index).zfill(3) for index in range(198)]
        store, root, _page = self.fixture("Mira: " + composite + ". " + " ".join(tokens))
        normal_line = self.boundary_line("\n".join(packets._render_context(root)))
        specs = [self.literal(root, exact) for exact in (main, dependent, *tokens)]
        self.assertEqual(len(specs), packets._DISCOVERY_TERMS)
        self.assertEqual(self.submit(store, root, subjects=specs)["errors"], [])
        child, = [item for item in review.load_queue(store) if item.kind == "discovery"]
        self.assertIn("continuation", child._context)
        self.assertFalse(source_audits._is_audit(child._context))
        self.assertEqual(self.boundary_line("\n".join(packets._render_context(child))), normal_line)
        complete = self.literal(child, composite)
        self.assertNotIn(self.expected(child, complete)["id"],
                         child._context["continuation"]["excluded_subject_ids"])
        self.assertEqual(self.submit(store, child, subjects=[complete])["errors"], [])
        audit = self.audit(store)
        marker = audit._context["source_boundary_audit"]
        self.assertEqual(marker["terminal_parent_id"], child.id)
        self.assertEqual(marker["ordinary_ancestor_ids"], [root.id])
        self.assertEqual(len(marker["excluded_subject_ids"]), 201)
        self.assertEqual(self.boundary_line("\n".join(packets._render_context(audit))), normal_line)
        self.assertEqual(self.submit(store, audit, subjects=[])["errors"], [])
        queue_ids = [item.id for item in review.load_queue(store)]
        for item, answer in ((root, specs), (child, [complete]), (audit, [])):
            self.assertEqual(self.submit(store, item, subjects=answer)["errors"], [])
        self.assertEqual([item.id for item in review.load_queue(store)], queue_ids)

    def test_raw_newlines_quotes_and_unicode_offsets_preserve_internal_period(self):
        main = 'We should wait for the "go!" signal'
        dependent = "Because the \U0001f4e6 has not arrived"
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                composite = main + "." + newline + dependent
                text = "Guide: The \U0001f4e6 is delayed." + newline + "Mira: " + composite + "."
                store, root, page = self.fixture(text)
                self.assertTrue(self.submit(store, root, terms=[composite])["errors"])
                self.assertNotIn(root.id, packets._receipts(store))
                specs = [self.literal(root, exact) for exact in (composite, main, dependent)]
                self.assertEqual(self.submit(store, root, subjects=specs)["errors"], [])
                subjects = self.saved(store, root)["subjects"]
                for spec, subject in zip(specs, subjects):
                    part, = subject["source"]["segments"]
                    self.assertEqual(part, spec["segments"][0])
                    self.assertEqual(part["start"], text.index(spec["canonical"]))
                    self.assertEqual(part["end"] - part["start"], len(spec["canonical"]))
                    self.assertEqual(page["text"][part["start"]:part["end"]], spec["canonical"])
                    with dbstore.connect(store) as conn:
                        self.assertEqual(span_subjects._validate(conn, subject), subject)
                self.assertIn("." + newline, subjects[0]["canonical"])
                self.assertIn('"go!"', subjects[0]["canonical"])
                self.assertFalse(subjects[0]["canonical"].endswith("."))

    def test_independent_same_topic_neighbors_do_not_generate_a_composite(self):
        negatives = (
            ("en", "We should wait here", "The road is flooded", ". "),
            ("ja", "\u3053\u3053\u3067\u5f85\u3068\u3046",
             "\u6a4b\u306f\u9589\u307e\u3063\u3066\u3044\u308b", "\u3002"),
            ("ko", "\uc5ec\uae30\uc11c \uae30\ub2e4\ub9ac\uc790",
             "\uae38\uc740 \uc7a0\uacbc\uc5b4", ". "),
            ("zh_hans", "\u6211\u4eec\u5148\u7b49\u4e00\u7b49",
             "\u6865\u8fd8\u6ca1\u5f00\u653e", "\u3002"),
            ("zh_hant", "\u660e\u5929\u518d\u51fa\u767c",
             "\u6a4b\u4eca\u5929\u6c92\u6709\u958b\u653e", "\u3002"),
        )
        for language, main, neighbor, separator in negatives:
            with self.subTest(language=language):
                store, root, _page = self.fixture("Mira: Yes! " + main + separator + neighbor + ".",
                                                  language)
                specs = [self.literal(root, exact) for exact in ("Yes!", main, neighbor)]
                self.assertEqual(self.submit(store, root, subjects=specs)["errors"], [])
                selected = self.saved(store, root)["subjects"]
                self.assertEqual([subject["canonical"] for subject in selected], ["Yes!", main, neighbor])
                self.assertEqual(len(selected), 3)
                audit = self.audit(store)
                self.assertEqual(len(audit._context["source_boundary_audit"]["inherited_subjects"]), 3)
                self.assertEqual(self.submit(store, audit, subjects=[])["errors"], [])
                self.assertEqual(self.saved(store, audit)["subjects"], [])

    def test_cross_speaker_and_repeated_speaker_turn_selections_fail_atomically(self):
        main, dependent = "We should wait here", "Because the road is flooded"
        for speaker in ("Lina", "Mira"):
            for newline in ("\n", "\r\n"):
                with self.subTest(speaker=speaker, newline=repr(newline)):
                    store, root, page = self.fixture("Mira: " + main + "." + newline +
                                                     speaker + ": " + dependent + ".")
                    first, second = [self.literal(root, exact) for exact in (main, dependent)]
                    cross_turn = dict(kind="segmented", evidence_id=first["evidence_id"],
                                      segments=[first["segments"][0], second["segments"][0]])
                    row = next(row for row in root._context["rows"] if row["id"] == first["evidence_id"])
                    full = dict(row["source"], start=0, end=len(page["text"]), text=page["text"])
                    with self.assertRaisesRegex(ValueError, "one utterance body"):
                        span_subjects._segmented(full, row["story_key"], cross_turn["segments"])
                    queue_before = [item.to_dict() for item in review.load_queue(store)]
                    result = self.submit(store, root, subjects=[first, cross_turn])
                    self.assertTrue(any("subject 2" in error and "out of range" in error
                                        for error in result["errors"]))
                    self.assertNotIn(root.id, packets._receipts(store))
                    self.assertEqual([item.to_dict() for item in review.load_queue(store)], queue_before)
                    with dbstore.connect(store) as conn:
                        self.assertEqual(conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0], 0)
                    self.assertEqual(self.submit(store, root, subjects=[first, second])["errors"], [])
                    self.assertEqual(len(self.saved(store, root)["subjects"]), 2)

    def test_internal_punctuation_removal_and_invented_aliases_are_not_saved(self):
        composite = "We should wait here. Because the road is flooded"
        store, root, _page = self.fixture("Mira: " + composite + ".")
        spec = self.literal(root, composite)
        aliases = (composite.replace(". ", " "), "We should wait here because the road is flooded")
        for alias in aliases:
            with self.subTest(alias=alias):
                self.assertTrue(self.submit(store, root, subjects=[dict(spec, canonical=alias)])["errors"])
                self.assertTrue(self.submit(store, root, terms=[alias])["errors"])
                self.assertNotIn(root.id, packets._receipts(store))
        self.assertEqual(self.submit(store, root, terms=[composite, composite + "."])["errors"], [])
        self.assertEqual(self.saved(store, root), [composite, composite + "."])
        marker = self.audit(store)._context["source_boundary_audit"]
        self.assertEqual(marker["excluded_terms"], sorted([composite, composite + "."]))

    def test_existing_judgment_fields_and_limits_are_unchanged(self):
        composite = "We should wait here. If the bridge reopens"
        store, root, _page = self.fixture("Mira: " + composite + ".")
        spec = self.literal(root, composite)
        self.assertEqual(set(spec), {"kind", "evidence_id", "canonical", "segments"})
        self.assertEqual(packets._DISCOVERY_TERMS, 200)
        self.assertEqual(packets._TURN_CHARS, 1800)
        self.assertTrue(packets._valid_surface("x" * 80))
        self.assertFalse(packets._valid_surface("x" * 81))
        self.assertTrue(self.submit(store, root, subjects=[dict(spec, dependency="conditional")])["errors"])
        self.assertTrue(self.submit(store, root, subjects=[spec] * 201)["errors"])
        self.assertNotIn(root.id, packets._receipts(store))
        self.assertEqual(self.submit(store, root, subjects=[spec])["errors"], [])
        self.assertEqual(set(self.saved(store, root)), {"terms", "subjects"})


if __name__ == "__main__":
    unittest.main()
