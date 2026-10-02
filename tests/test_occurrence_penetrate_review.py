"""Consumer adversaries use accepted packets, never fabricated grounding flags."""
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger, termindex as ti
from sekaisync.core import SekaiSyncCore


class OccurrencePenetrateReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)
        self.pages = self.seed({
            "zh_hans": "甲：我的声音和大家的声音。\n乙：今天有声音。",
            "en": "A: My sound and everyone's sound.\nB: There is sound today.",
            "zh_tw": "甲：我的聲音和大家的聲音。\n乙：今天有聲音。",
        })

    def seed(self, versions):
        pages = [dict(id=f"web:fixture:{language}:event_story:999:1", source="fixture",
                      language=language, trust="B", kind="event_story", text=text,
                      canonical_key=f"event_story:{language}:999:1")
                 for language, text in versions.items()]
        dbstore.upsert_web_pages(self.store, "fixture", pages)
        return pages

    def packets(self, source_language="zh_hans", term="声音", targets=("en", "zh_tw")):
        groups = ti.group_pages_by_story(self.pages)
        items, _ = ap._prepare_scrub_review(self.store, groups, sorted(groups), [term], {},
                                           source_language, list(targets))
        occurrences = [ap._occurrence_item(item) for item in items
                       if item._context["task"] == "translation"]
        self.assertEqual(len(occurrences), len(targets))
        ar.enqueue(self.store, occurrences)
        return {item.language: item for item in occurrences}

    def proposal(self, item, target_term, source_index=0, target_index=0,
                 kind="lexical", sense_key="audible-sound", row_index=0):
        row = item._context["rows"][row_index]
        source = ap._body_term_segments(row["source"]["text"], item.term, row["source"]["start"])
        source = [parts for parts in source if "".join(part["exact"] for part in parts) == item.term]
        target = ap._body_term_segments(row["target"]["text"], target_term, row["target"]["start"])
        target = [parts for parts in target if "".join(part["exact"] for part in parts) == target_term]
        return dict(evidence_id=row["id"], source_segments=source[source_index],
                    target_segments=target[target_index], sense_key=sense_key,
                    sense_gloss="Audible sound in the selected contextual dialogue occurrence",
                    kind=kind, rationale="Independent consumer regression fixture review")

    def submit(self, item, proposals):
        result = ar.submit_judgments(self.store, [dict(id=item.id, decision="accept",
                                                     relations=proposals, agent="consumer-review")])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["applied_slots"], 0)

    def read(self):
        with dbstore.connect(self.store) as conn:
            return ledger._read_relations(conn)

    def add_conflicting_legacy_record(self):
        record = ti.TermRecord(id=ti.make_term_id("zh_hans", "声音"), canonical="声音",
                               source_language="zh_hans", names={"zh_hans": "声音", "en": "WRONG"},
                               source="fixture", evidence=[dict(story_key="event:999:1",
                               language="zh_hans", context=self.pages[0]["text"])])
        dbstore.upsert_terms(self.store, [record], evidence_updates={record.id: {
            "mode": "replace", "items": record.evidence}})

    def test_real_singleton_is_consumed_without_publishing_global_names(self):
        item = self.packets()["en"]
        self.submit(item, [self.proposal(item, "sound", source_index=1, target_index=1)])
        core = SekaiSyncCore(self.store)
        result = core.term_penetrate("声音", story_key="event:999:1", languages=["zh_hans", "en"])
        self.assertEqual(result["per_language"]["en"]["term"], "sound")
        self.assertEqual(result["term"]["names"], {"zh_hans": "声音"})
        self.assertEqual(result["term"]["evidence"][0]["start"], 10)
        self.assertEqual(result["story_key"], "event:999:1")
        with dbstore.connect(self.store) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_same_surface_at_two_target_positions_is_not_resolved_by_first_match(self):
        item = self.packets()["en"]
        self.submit(item, [self.proposal(item, "sound", target_index=index) for index in (0, 1)])
        self.assertEqual(ti._occurrence_penetrate(self.read(), "声音", pages=self.pages), (True, None))
        self.add_conflicting_legacy_record()
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("声音", story_key="event:999:1"))

    def test_lexical_and_omitted_judgments_cannot_be_filtered_into_false_certainty(self):
        item = self.packets()["en"]
        self.submit(item, [self.proposal(item, "sound", kind=kind) for kind in ("lexical", "omitted")])
        self.assertEqual(ti._occurrence_penetrate(self.read(), "声音", pages=self.pages), (True, None))

    def test_same_story_same_sense_different_sources_do_not_fill_each_others_languages(self):
        items = self.packets()
        self.submit(items["en"], [self.proposal(items["en"], "sound", source_index=0, target_index=0)])
        self.submit(items["zh_tw"], [self.proposal(items["zh_tw"], "聲音", source_index=1, target_index=1)])
        handled, result = ti._occurrence_penetrate(self.read(), "声音", languages=["en", "zh_tw"], pages=self.pages)
        self.assertTrue(handled)
        self.assertIsNotNone(result)
        projected = [entry for entry in result["per_language"].values() if entry["term"]]
        self.assertEqual(len(projected), 1)
        self.assertEqual(sum(entry.get("missing", False) for entry in result["per_language"].values()), 1)

    def test_distinct_senses_are_not_joined_even_when_target_spelling_agrees(self):
        item = self.packets()["en"]
        self.submit(item, [self.proposal(item, "sound", source_index=index, target_index=index,
                                         sense_key=sense) for index, sense in ((0, "music"), (1, "opinion"))])
        self.add_conflicting_legacy_record()
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("声音", story_key="event:999:1"))

    def test_stale_target_page_does_not_revive_a_conflicting_legacy_name(self):
        item = self.packets()["en"]
        self.submit(item, [self.proposal(item, "sound")])
        self.add_conflicting_legacy_record()
        with dbstore.connect(self.store) as conn:
            conn.execute("UPDATE web_pages SET text=text || ' changed' WHERE language='en'")
            conn.commit()
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("声音", story_key="event:999:1"))

    def test_missing_immutable_scope_does_not_crash_or_fall_back_to_legacy(self):
        item = self.packets()["en"]
        self.submit(item, [self.proposal(item, "sound")])
        self.add_conflicting_legacy_record()
        ap._scope_path(self.store, item._context["scope_id"]).unlink()
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("声音", story_key="event:999:1"))

    def test_one_stale_language_stays_missing_in_a_partially_current_occurrence(self):
        items = self.packets()
        for language, surface in (("en", "sound"), ("zh_tw", "聲音")):
            self.submit(items[language], [self.proposal(items[language], surface)])
        with dbstore.connect(self.store) as conn:
            conn.execute("UPDATE web_pages SET text=text || ' changed' WHERE language='zh_tw'")
            conn.commit()
        result = SekaiSyncCore(self.store).term_penetrate("声音", story_key="event:999:1", languages=["en", "zh_tw"])
        self.assertEqual(result["per_language"]["en"]["term"], "sound")
        self.assertEqual(result["per_language"]["zh_tw"]["term"], "")
        self.assertTrue(result["per_language"]["zh_tw"]["missing"])

    def test_english_case_distinctions_keep_different_semantic_subjects(self):
        self.pages = self.seed({"en": "A: US helps us.", "ja": "甲：米国が私たちを助ける。"})
        for surface, target, sense in (("US", "米国", "country"), ("us", "私たち", "people")):
            item = self.packets("en", surface, ("ja",))["ja"]
            self.submit(item, [self.proposal(item, target, sense_key=sense)])
        core = SekaiSyncCore(self.store)
        self.assertEqual(core.term_penetrate("US", story_key="event:999:1", languages=["ja"])["per_language"]["ja"]["term"], "米国")
        self.assertEqual(core.term_penetrate("us", story_key="event:999:1", languages=["ja"])["per_language"]["ja"]["term"], "私たち")

    def shared_surface_packets(self, edges):
        self.pages = self.seed({
            "zh_hans": "甲：音和音一起响起。",
            "zh_tw": "甲：音和音一起響起。",
            "ja": "甲：音と音が一緒に聞こえる。",
            "en": "A: A sound and another sound.",
        })
        sources = sorted({edge[0] for edge in edges})
        for source in sources:
            selected = [edge for edge in edges if edge[0] == source]
            items = self.packets(source, "音", tuple(sorted({edge[1] for edge in selected})))
            for target, item in items.items():
                proposals = [self.proposal(item, "sound" if target == "en" else "音",
                                           source_index=source_index, target_index=target_index,
                                           kind=kind, sense_key=sense)
                             for _, language, source_index, target_index, kind, sense in selected
                             if language == target]
                self.submit(item, proposals)

    def test_shared_surface_can_use_direct_reciprocal_lexical_anchor_evidence(self):
        self.shared_surface_packets([
            ("zh_hans", "zh_tw", 0, 0, "lexical", "sound"),
            ("zh_tw", "zh_hans", 0, 0, "lexical", "sound"),
            ("zh_hans", "en", 0, 0, "lexical", "sound"),
            ("zh_tw", "en", 0, 0, "lexical", "sound"),
        ])
        result = SekaiSyncCore(self.store).term_penetrate("音", story_key="event:999:1", languages=["en"])
        self.assertIsNotNone(result)
        self.assertEqual(result["per_language"]["en"]["term"], "sound")
        self.assertEqual(len(result["term"]["names"]), 1)
        self.assertIn(result["term"]["source_language"], {"zh_hans", "zh_tw"})

    def test_one_way_shared_surface_evidence_does_not_resolve_source_identity(self):
        self.shared_surface_packets([
            ("zh_hans", "zh_tw", 0, 0, "lexical", "sound"),
            ("zh_hans", "en", 0, 0, "lexical", "sound"),
            ("zh_tw", "en", 0, 0, "lexical", "sound"),
        ])
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("音", story_key="event:999:1"))

    def test_reciprocal_spellings_at_different_anchors_are_not_reciprocal_occurrences(self):
        self.shared_surface_packets([
            ("zh_hans", "zh_tw", 0, 0, "lexical", "sound"),
            ("zh_tw", "zh_hans", 0, 1, "lexical", "sound"),
            ("zh_hans", "en", 0, 0, "lexical", "sound"),
            ("zh_tw", "en", 0, 0, "lexical", "sound"),
        ])
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("音", story_key="event:999:1"))

    def test_reverse_paraphrase_is_not_a_shared_lexical_identity_certificate(self):
        self.shared_surface_packets([
            ("zh_hans", "zh_tw", 0, 0, "lexical", "sound"),
            ("zh_tw", "zh_hans", 0, 0, "paraphrase", "sound"),
            ("zh_hans", "en", 0, 0, "lexical", "sound"),
            ("zh_tw", "en", 0, 0, "lexical", "sound"),
        ])
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("音", story_key="event:999:1"))

    def test_reciprocal_other_language_does_not_erase_two_senses_in_one_source_language(self):
        self.shared_surface_packets([
            ("zh_hans", "zh_tw", 0, 0, "lexical", "sound"),
            ("zh_tw", "zh_hans", 0, 0, "lexical", "sound"),
            ("zh_hans", "en", 0, 0, "lexical", "sound"),
            ("zh_hans", "en", 1, 1, "lexical", "another-sense"),
            ("zh_tw", "en", 0, 0, "lexical", "sound"),
        ])
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("音", story_key="event:999:1"))

    def test_three_language_chain_cannot_replace_all_pair_direct_reciprocal_evidence(self):
        self.shared_surface_packets([
            ("zh_hans", "zh_tw", 0, 0, "lexical", "sound"),
            ("zh_tw", "zh_hans", 0, 0, "lexical", "sound"),
            ("zh_tw", "ja", 0, 0, "lexical", "sound"),
            ("ja", "zh_tw", 0, 0, "lexical", "sound"),
            ("zh_hans", "en", 0, 0, "lexical", "sound"),
            ("zh_tw", "en", 0, 0, "lexical", "sound"),
            ("ja", "en", 0, 0, "lexical", "sound"),
        ])
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate("音", story_key="event:999:1"))

    def test_no_story_query_does_not_lend_reciprocity_from_another_story(self):
        self.pages = self.seed({"zh_hans": "甲：音响起。", "zh_tw": "甲：音響起。", "en": "A: A sound rings."})
        later = [dict(page, id=page["id"].replace(":999:1", ":999:2"),
                      canonical_key=page["canonical_key"].replace(":999:1", ":999:2"))
                 for page in self.pages]
        dbstore.upsert_web_pages(self.store, "fixture", later)
        self.pages += later
        edges = [("zh_hans", "zh_tw", "event:999:1"),
                 ("zh_tw", "zh_hans", "event:999:1"),
                 ("zh_hans", "en", "event:999:2"),
                 ("zh_tw", "en", "event:999:2")]
        for source in ("zh_hans", "zh_tw"):
            selected = [edge for edge in edges if edge[0] == source]
            items = self.packets(source, "音", tuple(edge[1] for edge in selected))
            for _, target, story in selected:
                item = items[target]
                row_index = next(index for index, row in enumerate(item._context["rows"])
                                 if row["story_key"] == story)
                self.submit(item, [self.proposal(item, "sound" if target == "en" else "音",
                                                sense_key="sound", row_index=row_index)])
        result = SekaiSyncCore(self.store).term_penetrate("音", languages=["en"])
        self.assertIsNotNone(result)
        self.assertEqual(result["story_key"], "event:999:1")
        self.assertEqual(result["per_language"]["en"]["term"], "")
        self.assertTrue(result["per_language"]["en"]["missing"])


if __name__ == "__main__":
    unittest.main()
