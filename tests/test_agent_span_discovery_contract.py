"""Current string discovery cannot encode lexical gaps by anchor support alone."""
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger, termindex as ti


class SpanDiscoveryContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)

    def packets(self, source_text, source_language, term, target_text=None):
        target_language = "en" if source_language != "en" else "zh_hans"
        if target_text is None:
            target_text = "Speaker：They offered a helping hand." if target_language == "en" else "甲：伸出了援手。"
        pages = [dict(id=f"web:fixture:{language}:event_story:999:1", source="fixture", kind="event_story",
                      language=language, trust="B", text=text,
                      canonical_key=f"event_story:{language}:999:1")
                 for language, text in ((source_language, source_text), (target_language, target_text))]
        dbstore.upsert_web_pages(self.store, "fixture", pages)
        groups = ti.group_pages_by_story(pages)
        items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), [term] if term else [], {},
                                           source_language, [target_language])
        return items

    def segments(self, view, fragments):
        parts, cursor = [], 0
        for fragment in fragments:
            start = view["text"].index(fragment, cursor)
            end = start + len(fragment)
            parts.append(dict(start=view["start"] + start, end=view["start"] + end, exact=fragment))
            cursor = end
        return parts

    def test_string_discovery_cannot_discover_the_expression_with_lexical_gap_removed(self):
        items = self.packets("A: You were lending her a hand.", "en", None)
        item = next(item for item in items if item.kind == "discovery")
        ar.enqueue(self.store, [item])
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", terms=["lending a hand"])])
        self.assertTrue(result["errors"])
        self.assertIn(item.id, {pending.id for pending in ar.load_queue(self.store)})
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_raw_envelope_is_not_permission_to_remove_a_lexical_gap_from_its_subject(self):
        items = self.packets("A: You were lending her a hand.", "en", "lending her a hand")
        item = ap._occurrence_item(next(item for item in items if item._context["task"] == "translation"))
        ar.enqueue(self.store, [item])
        row = item._context["rows"][0]
        selected = self.segments(row["source"], ("lending", "a hand"))
        self.assertFalse(ap._term_selection(row["source"], item.term, selected, case_sensitive=True))
        target = self.segments(row["target"], ("伸出了援手",))
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", relations=[dict(
            evidence_id=row["id"], source_segments=selected, target_segments=target,
            sense_key="help", sense_gloss="The lending-a-hand idiom excluding its inserted beneficiary",
            kind="lexical", rationale="The exact lexical fragments omit the inserted pronoun.")])])
        self.assertTrue(result["errors"])
        with dbstore.connect(self.store) as conn:
            self.assertEqual(ledger._read_relations(conn), [])

    def test_only_whitespace_gaps_are_representable_through_existing_real_packets(self):
        for number, (term, fragments, target) in enumerate((
                ("build confidence", ("build", "confidence"), "增强了信心"),
                ("don't droop", ("don't", "droop"), "没有下垂"))):
            with self.subTest(term=term):
                self.store = Path(self.temp.name) / str(number)
                dbstore.initialize(self.store)
                items = self.packets("A: " + term + ".", "en", term, "甲：" + target + "。")
                item = ap._occurrence_item(next(item for item in items if item._context["task"] == "translation"))
                ar.enqueue(self.store, [item])
                row = item._context["rows"][0]
                selected = self.segments(row["source"], fragments)
                self.assertTrue(ap._term_selection(row["source"], term, selected, case_sensitive=True))
                result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept", relations=[dict(
                    evidence_id=row["id"], source_segments=selected,
                    target_segments=self.segments(row["target"], (target,)),
                    sense_key="expression", sense_gloss="The complete source expression with whitespace-only separation",
                    kind="lexical", rationale="Both raw lexical fragments exactly reproduce the complete phrase.")])])
                self.assertEqual(result["errors"], [])
                with dbstore.connect(self.store) as conn:
                    rows = ledger._read_relations(conn)
                    self.assertEqual(len(rows), 1)
                    self.assertEqual(rows[0]["source"]["segments"], selected)

    def test_eight_multisegment_forms_split_into_six_lexical_gaps_and_two_whitespace_gaps(self):
        cases = (
            ("ja", "自信がついた", ("自信", "ついた"), False),
            ("en", "build confidence", ("build", "confidence"), True),
            ("zh_hans", "有了一点信心", ("有了", "信心"), False),
            ("ko", "자신감이 붙었어요", ("자신감", "붙었어요"), False),
            ("en", "don't droop", ("don't", "droop"), True),
            ("en", "lending her a hand", ("lending", "a hand"), False),
            ("ja", "描く気力が落ちていって", ("描く気力", "落ちていって"), False),
            ("zh_hans", "画画的动力却在逐渐消退", ("画画的动力", "逐渐消退"), False),
        )
        accepted = 0
        for number, (language, term, fragments, expected) in enumerate(cases):
            with self.subTest(language=language, term=term):
                self.store = Path(self.temp.name) / ("case-" + str(number))
                dbstore.initialize(self.store)
                items = self.packets("Speaker：" + term + "。", language, term)
                item = ap._occurrence_item(next(item for item in items if item._context["task"] == "translation"))
                view = item._context["rows"][0]["source"]
                actual = ap._term_selection(view, term, self.segments(view, fragments), case_sensitive=True)
                self.assertEqual(actual, expected)
                accepted += actual
        self.assertEqual(accepted, 2)


if __name__ == "__main__":
    unittest.main()
