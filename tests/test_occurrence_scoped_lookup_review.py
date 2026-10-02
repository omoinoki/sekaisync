"""Independent legacy lookup/query consumption of contextual occurrences."""
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, dbstore, occurrence_store as ledger, span_subjects, termindex as ti
from sekaisync.core import SekaiSyncCore


class OccurrenceScopedLookupReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.pages = {
            "en": dict(source="fixture", id="web:fixture:en:event_story:999:1", language="en", kind="event_story",
                       trust="B", text="A: lending her a hand; lending him a hand; build confidence."),
            "ja": dict(source="fixture", id="web:fixture:ja:event_story:999:1", language="ja", kind="event_story",
                       trust="B", text="A：彼女を手伝った。彼を支援した。『build confidence』という表現。"),
            "zh_tw": dict(source="fixture", id="web:fixture:zh_tw:event_story:999:1", language="zh_tw", kind="event_story",
                          trust="B", text="甲：幫了她。支援了他。建立信心。"),
        }
        dbstore.upsert_web_pages(self.store, "fixture", self.pages.values())
        self.translations = {}

    def packet(self, source="en", target="ja"):
        key = (source, target)
        if key not in self.translations:
            groups = ti.group_pages_by_story(self.pages.values())
            terms = ["lending"] if source == "en" else ["建立信心"] if source == "zh_tw" else ["build confidence"]
            items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), terms, {}, source, [target])
            self.translations[key] = next(item for item in items if item._context["task"] == "translation")
        return self.translations[key]

    def parts(self, view, fragments, cursor=0):
        result = []
        for exact in fragments:
            start = view["text"].index(exact, cursor)
            end = start + len(exact)
            result.append(dict(start=view["start"] + start, end=view["start"] + end, exact=exact))
            cursor = end
        return result

    def subject(self, *, source="en", target="ja", kind="segmented", fragments=("lending", "a hand"), second=False):
        row = self.packet(source, target)._context["rows"][0]
        cursor = row["source"]["text"].index("lending him") if second else 0
        parts = self.parts(row["source"], fragments, cursor)
        if kind == "literal":
            return span_subjects._literal(row["source"], row["story_key"], " ".join(fragments), parts)
        return span_subjects._segmented(row["source"], row["story_key"], parts)

    def relation(self, subject=None, *, source="en", target="ja", target_fragments=("手伝った",),
                 sense_key="helping", sense_gloss="Helping this beneficiary in the specific utterance", kind="lexical", target_cursor=0):
        subject = subject or self.subject(source=source, target=target)
        packet = self.packet(source, target)
        row = packet._context["rows"][0]
        context = dict(packet._context, task="occurrence", subject=subject)
        item = ap._item(subject["canonical"], target, [], "pending", context, "Independent scoped lookup fixture")
        anchor = ledger._anchor(row["target"], row["story_key"], self.parts(row["target"], target_fragments, target_cursor))
        sense = ledger._sense(subject["id"], source, sense_key, sense_gloss)
        proof = dict(scope_id=context["scope_id"], row_id=row["id"], term=item.term, candidates=[], context=context)
        return ledger._relation(subject["source"], anchor, sense, kind, item.id,
                                "Contextual machine-review fixture; not semantic gold", grounding=proof)

    def save(self, *relations):
        with dbstore.connect(self.store) as conn:
            ledger._store_relations(conn, list(relations))
            conn.commit()

    def lookup(self, query="lending a hand", **kwargs):
        return SekaiSyncCore(self.store).term_lookup(query, **kwargs)

    def positions(self, record):
        return {position["language"]: position for position in record["positions"]}

    def legacy(self, query="lending a hand", source="en"):
        record = ti.TermRecord(id="legacy:" + source, canonical=query, source_language=source,
                               names={source: query, "ja": "legacy-wrong-name"}, source="fixture",
                               confidence=1.0, trust="B", evidence=[])
        dbstore.upsert_terms(self.store, [record])
        return record

    def state(self):
        with dbstore.connect(self.store) as conn:
            return (dbstore.current_revision(conn),
                    conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM scraper_relations").fetchone()[0],
                    conn.execute("SELECT COUNT(*) FROM scraper_subjects").fetchone()[0])

    def test_old_lookup_and_generic_query_return_scoped_names_positions_without_writes(self):
        self.save(self.relation())
        before = self.state()
        core = SekaiSyncCore(self.store)
        with core.request_view():
            records = core.term_lookup("lending a hand", languages=["en", "ja"])
            generic = core.query("lending a hand", include_web=False)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["names"], {})
        self.assertEqual(records[0]["canonical"], "lending a hand")
        positions = self.positions(records[0])
        self.assertEqual(positions["en"]["term"], "")
        self.assertTrue(positions["en"]["missing"])
        self.assertIn("segmented", positions["en"]["note"])
        self.assertEqual(positions["ja"]["term"], "手伝った")
        self.assertEqual(len(generic["terms"]), 1)
        self.assertEqual(generic["terms"][0]["source_layer"], "term_extraction")
        self.assertEqual(generic["terms"][0]["names"], {})
        self.assertEqual(self.state(), before)
        self.assertEqual(before[1], 0)

    def test_two_same_display_source_occurrences_are_separate_and_never_fill_languages(self):
        first = self.subject()
        second = self.subject(target="zh_tw", second=True)
        self.save(self.relation(first), self.relation(second, target="zh_tw", target_fragments=("支援了他",)))
        records = self.lookup(languages=["en", "ja", "zh_hant"])
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["id"] for record in records}), 2)
        self.assertEqual({record["evidence"][0]["start"] for record in records},
                         {first["source"]["segments"][0]["start"], second["source"]["segments"][0]["start"]})
        for record in records:
            positions = self.positions(record)
            self.assertEqual(sum(bool(positions[language]["term"]) for language in ("ja", "zh_hant")), 1)
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("lending a hand", story_key="event:999:1"))

    def test_two_explicit_senses_on_same_source_are_separate_list_results(self):
        subject = self.subject()
        self.save(self.relation(subject), self.relation(subject, sense_key="other-interpretation",
                                                       sense_gloss="A deliberately separate contextual interpretation"))
        records = self.lookup()
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["id"] for record in records}), 2)
        self.assertEqual(len({record["evidence"][0]["start"] for record in records}), 1)
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("lending a hand"))

    def test_literal_and_segmented_same_display_remain_distinct_lookup_results(self):
        literal = self.subject(kind="literal", fragments=("build", "confidence"))
        segmented = self.subject(fragments=("build", "confidence"))
        self.save(self.relation(literal), self.relation(segmented))
        records = self.lookup("build confidence", languages=["en", "ja"])
        self.assertEqual(len(records), 2)
        self.assertEqual(len({record["id"] for record in records}), 2)
        self.assertEqual(sum(record["names"] == {"en": "build confidence"} for record in records), 1)
        self.assertEqual(sum(record["names"] == {} for record in records), 1)
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("build confidence"))

    def test_conflicting_target_anchors_are_not_flattened_into_a_scalar_translation(self):
        subject = self.subject()
        self.save(self.relation(subject), self.relation(subject, target_fragments=("支援した",)))
        records = self.lookup(languages=["ja"])
        self.assertFalse(any(position.get("term") for record in records for position in record["positions"]))
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("lending a hand"))

    def test_paraphrase_stays_contextual_while_adjacent_parts_retain_raw_scalar(self):
        self.save(self.relation(kind="paraphrase"),
                  self.relation(self.subject(second=True), target_fragments=("彼を", "支援した"), sense_key="second-help"))
        records = self.lookup(languages=["en", "ja"])
        self.assertEqual(len(records), 2)
        for record in records:
            position = self.positions(record)["ja"]
            if "paraphrase" in position["note"]:
                self.assertEqual(position["term"], "")
                self.assertTrue(position["missing"])
            else:
                self.assertEqual(position["term"], "彼を支援した")
                self.assertFalse(position.get("missing"))
            self.assertTrue(position["sentence"])
            self.assertEqual(record["names"], {})

    def test_fragmented_non_whitespace_target_gap_remains_context_not_scalar(self):
        self.save(self.relation(self.subject(second=True), target_fragments=("彼", "支援した"), sense_key="second-help",
                               target_cursor=self.pages["ja"]["text"].index("彼を支援した")))
        records = self.lookup(languages=["en", "ja"])
        self.assertEqual(len(records), 1)
        position = self.positions(records[0])["ja"]
        self.assertEqual(position["term"], "")
        self.assertTrue(position["missing"])
        self.assertTrue(position["sentence"])
        self.assertEqual(records[0]["names"], {})
        with dbstore.connect(self.store) as conn:
            relation = ledger._read_relations(conn)[0]
            left, right = relation["target"]["segments"]
            self.assertEqual(self.pages["ja"]["text"][left["end"]:right["start"]], "を")

    def test_limit_zero_positive_and_sort_are_applied_after_contextual_separation(self):
        self.save(self.relation(), self.relation(self.subject(second=True), sense_key="second-help"))
        self.assertEqual(len(self.lookup(limit=8)), 2)
        self.assertEqual(len(self.lookup(limit=1)), 1)
        self.assertEqual(self.lookup(limit=0), [])
        weighted = self.lookup(limit=1, sort="weight")
        self.assertEqual(len(weighted), 1)
        self.assertEqual(weighted[0]["names"], {})

    def test_target_language_aliases_preserve_requested_position_key(self):
        subject = self.subject(target="zh_tw")
        self.save(self.relation(subject, target="zh_tw", target_fragments=("幫了她",)))
        records = self.lookup(languages=["en", "zh_hant"])
        self.assertEqual(len(records), 1)
        self.assertEqual(set(self.positions(records[0])), {"en", "zh_hant"})
        self.assertEqual(self.positions(records[0])["zh_hant"]["term"], "幫了她")

    def test_source_language_aliases_filter_ledger_versions_without_mixing_languages(self):
        subject = self.subject(source="zh_tw", target="en", kind="literal", fragments=("建立信心",))
        self.save(self.relation(subject, source="zh_tw", target="en", target_fragments=("build confidence",)))
        records = self.lookup("建立信心", source_language="zh_hant", languages=["en"])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["source_language"], "zh_tw")
        self.assertEqual(records[0]["names"], {"zh_tw": "建立信心"})
        self.assertEqual(self.lookup("建立信心", source_language="ja"), [])

    def test_same_display_in_two_source_languages_is_listed_separately_and_filterable(self):
        english = self.subject(kind="literal", fragments=("build", "confidence"))
        japanese = self.subject(source="ja", target="en", kind="literal", fragments=("build confidence",))
        self.save(self.relation(english), self.relation(japanese, source="ja", target="en",
                                                     target_fragments=("build confidence",), sense_key="quoted-form"))
        records = self.lookup("build confidence")
        self.assertEqual({record["source_language"] for record in records}, {"en", "ja"})
        self.assertEqual(len(self.lookup("build confidence", source_language="en")), 1)
        self.assertEqual(len(self.lookup("build confidence", source_language="ja")), 1)
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("build confidence"))

    def test_stale_exact_known_subject_suppresses_wrong_flattened_legacy_name(self):
        self.save(self.relation())
        self.legacy()
        dbstore.upsert_web_pages(self.store, "fixture", [dict(self.pages["en"], text=self.pages["en"]["text"] + " changed")])
        self.assertEqual(self.lookup(), [])
        self.assertEqual(SekaiSyncCore(self.store).query("lending a hand", include_web=False)["terms"], [])
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 1)
            self.assertEqual(len(ledger._read_relations(conn, current_only=False)), 1)

    def test_missing_scope_does_not_restore_an_exact_legacy_scalar(self):
        relation = self.relation()
        self.save(relation)
        self.legacy()
        ap._scope_path(self.store, relation["grounding"]["scope_id"]).unlink()
        self.assertEqual(self.lookup(), [])

    def test_case_changed_display_is_not_a_scoped_normalized_alias(self):
        self.save(self.relation())
        self.assertEqual(self.lookup("Lending a hand"), [])
        self.assertEqual(len(self.lookup("lending a hand")), 1)


if __name__ == "__main__":
    unittest.main()
