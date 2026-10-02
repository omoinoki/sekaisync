"""Discovery regressions using unseen names, without an annotated-term seed.

The fixtures deliberately exercise span/grammar rules rather than measuring
recall against the same annotation file that supplies the extraction lexicon.
No test opens a production store or calls an external model.
"""
import unittest
from unittest.mock import patch

from sekaisync import termindex as ti, zhfirst as zf
from sekaisync.models import GlossaryTerm
from sekaisync.wordseg import discover_words, _discover_content_words


def page(language, text, episode=1):
    return dict(id=f"web:fixture:{language}:event_story:950:{episode}",
                kind="event_story", language=language, text=text)


class BoundaryDiscoveryTests(unittest.TestCase):
    def test_repeated_content_word_with_common_characters_is_recalled(self):
        texts = ["甲乙丙丁戊己庚辛壬癸"] * 500 + ["星"] * 100 + ["门"] * 100 + ["星门"] * 12
        self.assertNotIn("星门", discover_words(texts, min_cohesion=8))
        self.assertIn("星门", _discover_content_words(texts))

    def test_accessor_variety_is_not_lost_with_unequal_neighbour_counts(self):
        texts = ["甲下弦乙", "丙下弦丁", "甲下弦戊"]
        self.assertIn("下弦", _discover_content_words(texts))

    def test_explicit_boundaries_are_positive_evidence(self):
        found = discover_words(["蓝莓星环。", "蓝莓星环！", "蓝莓星环？"],
                               min_freq=3, min_cohesion=0, min_entropy=1)
        self.assertIn("蓝莓星环", found)
        self.assertNotIn("蓝莓", found)
        self.assertNotIn("星环", found)

    def test_unicode_quotes_are_boundaries_not_characters(self):
        found = discover_words(["“蓝莓星环”"] * 3, min_freq=3,
                               min_cohesion=0, min_entropy=1)
        self.assertEqual(found, {"蓝莓星环"})

    def test_truncated_budget_edge_is_not_boundary_evidence(self):
        found = discover_words(["蓝莓星环后续"], min_freq=1, max_chars=4,
                               min_cohesion=0, min_entropy=1)
        self.assertNotIn("蓝莓星环", found)

    def test_whole_document_edges_count_without_cross_document_words(self):
        found = discover_words(iter(["蓝莓星环"] * 3), min_freq=3,
                               min_cohesion=0, min_entropy=1)
        self.assertEqual(found, {"蓝莓星环"})

    def test_function_boundary_option_still_applies(self):
        found = discover_words(["的星环。"] * 3, min_freq=3,
                               min_cohesion=0, min_entropy=1,
                               boundary_stop_chars="的")
        self.assertNotIn("的星环", found)


class CandidateSpanTests(unittest.TestCase):
    def test_colons_in_prose_and_urls_are_not_speaker_labels(self):
        for text in ("We meet at 12:30.", "https://example.test/path", "word " * 40 + "x" * 20 + ": tail"):
            self.assertEqual(zf.strip_speaker(text), text)
        self.assertEqual(zf.strip_speaker("杏・彰人・冬弥：  出发。"), "出发。")

    def test_llm_string_false_is_not_a_positive_decision(self):
        class InvalidResponse:
            def chat_json(self, *args):
                return {"results": [{"term": "薄荷", "keep": "false"}]}
        self.assertEqual(zf.llm_filter_terms(["薄荷"], InvalidResponse()), set())

    def candidates(self, text, **kwargs):
        return zf.extract_zh_candidates_from_story(text, "event:950:1", set(),
                                                  set(), **kwargs)

    def test_long_seed_is_not_cut_at_five_characters(self):
        name = "星砂薄荷巧克力"
        self.assertEqual(self.candidates(f"少女：一起品尝{name}吧。", seed={name}),
                         [(name, False)])

    def test_dictionary_protects_full_name_against_nested_fragments(self):
        name = "星砂薄荷巧克力"
        self.assertEqual(self.candidates(name, seed={name, "薄荷", "巧克力"}),
                         [(name, False)])

    def test_quotes_have_one_surface_and_later_mentions_upgrade_signal(self):
        self.assertEqual(self.candidates("少女：月虹音乐节。\n少女：参加“月虹音乐节”！",
                                         seed={"月虹音乐节"}),
                         [("月虹音乐节", True)])

    def test_suffix_discovery_does_not_swallow_pronoun_and_predicate(self):
        self.assertEqual(self.candidates("少女：我们参加月虹音乐节。"),
                         [("月虹音乐节", False)])

    def test_standalone_nominal_suffix_uses_its_real_boundary(self):
        for text in ("少女：音乐节的时候一起出发。", "少女：我们今天在音乐节。"):
            self.assertEqual(self.candidates(text), [("音乐节", False)])
        self.assertEqual(self.candidates("少女：音乐节目。"), [])

    def test_mixed_name_does_not_emit_latin_prefix_or_predicate(self):
        self.assertEqual(self.candidates("少女：LUMINA时间特别热闹。"),
                         [("LUMINA时间", False)])

    def test_internal_grammar_characters_do_not_destroy_a_name(self):
        self.assertEqual(self.candidates("少女：前往森之宫歌剧团。"),
                         [("森之宫歌剧团", False)])

    def test_content_word_evidence_is_not_rejected_by_one_character(self):
        result = zf.extract_zh_candidates_from_story("少女：观察下弦。", "s", {"下弦"}, set())
        self.assertIn(("下弦", False), result)

    def test_statistical_evidence_does_not_rescue_exact_function_words(self):
        result = zf.extract_zh_candidates_from_story("少女：大家在这里。", "s", {"大家", "这里"}, set())
        self.assertEqual(result, [])

    def test_seed_latin_substring_is_not_a_mention(self):
        result = self.candidates("少女：RADICAL", seed={"RAD"})
        self.assertNotIn(("RAD", False), result)

    def test_zero_budget_is_empty_and_strong_late_candidate_wins(self):
        text = "少女：Lunaris。\n少女：Solis。\n少女：所谓“月虹”。"
        self.assertEqual(self.candidates(text, max_terms=0), [])
        self.assertEqual(self.candidates(text, max_terms=1), [("月虹", True)])

    def test_latin_phrase_and_acronym_survive(self):
        self.assertEqual(self.candidates("少女：“Star of Dawn”和“N25”。"),
                         [("Star of Dawn", True), ("N25", True)])

    def test_quoted_dialogue_does_not_become_a_latin_name(self):
        self.assertEqual(self.candidates('少女：“Thank you”。'), [])

    def test_global_segmentation_improves_on_both_greedy_directions(self):
        text = "甲乙丙丁戊己庚辛"
        vocab = frozenset({"乙丙", "乙丙丁", "己庚辛", "庚辛", "戊己", "戊己庚", "甲乙"})
        result = zf.segment_zh_bi_cjk(text, vocab)
        self.assertEqual(result, ["甲", "乙丙丁", "戊己", "庚辛"])
        self.assertEqual("".join(result), text)


class LocalLanguageCandidateTests(unittest.TestCase):
    def test_complete_multitoken_name_not_pairs_of_words(self):
        self.assertEqual(ti._local_latin_candidates("We met MORE MORE JUMP today."),
                         ["MORE MORE JUMP"])

    def test_internal_connectors_are_preserved(self):
        self.assertEqual(ti._local_latin_candidates("We met at Star of Dawn today."),
                         ["Star of Dawn"])
        self.assertTrue(ti._translation_candidate_acceptable("Star of Dawn", "en"))
        self.assertFalse(ti._translation_candidate_acceptable("We are Here", "en"))

    def test_mixed_glue_preserves_display_spelling(self):
        self.assertEqual(ti._local_latin_candidates("Leo/need, Cheerful＊Days"),
                         ["Leo/need", "Cheerful＊Days"])

    def test_official_latin_lexicon_requires_token_boundaries(self):
        lexicon = {ti.normalize_name("RAD"): {"surface": "RAD"}}
        self.assertEqual(ti.tokenize_ja("RADICAL", lexicon), [])
        self.assertEqual([t["surface"] for t in ti.tokenize_ja("RADへ行く", lexicon)], ["RAD"])

    def test_grammar_inside_names_is_distinct_from_dangling_grammar(self):
        for name in ("地下通道", "森之宫歌剧团", "未来都市"):
            self.assertTrue(ti._source_candidate_acceptable(name, "zh_hans"), name)
        self.assertTrue(ti._source_candidate_acceptable("森の劇団", "ja"))
        self.assertFalse(ti._source_candidate_acceptable("森の劇団に", "ja"))
        self.assertFalse(ti._source_candidate_acceptable("今天晚上会吃汉堡肉", "zh_hans"))


class GlossaryDiscoveryTrustTests(unittest.TestCase):
    name = "星砂薄荷巧克力"

    def glossary(self, official=True, demo=False, **names):
        return GlossaryTerm(id="fixture:950", kind="area_item", canonical="星砂ミントチョコ",
                            names={"ja": "星砂ミントチョコ", "zh_hans": self.name,
                                   "en": "Stardust Mint Chocolate", **names},
                            official=official, demo=demo)

    def extract(self, glossary):
        pages = [page("zh_hans", f"少女：品尝{self.name}。"), page("ja", "少女：食べよう。")]
        with patch.object(zf, "_load_manual_seed", return_value=set()), \
                patch.object(zf, "discover_words", return_value=set()):
            return zf.extract_terms_zhfirst(pages, ["en"], iter(glossary))

    def test_rare_official_name_is_found_without_annotation_or_statistics(self):
        result = self.extract([self.glossary()])
        self.assertEqual([t.canonical for t in result], [self.name])
        self.assertTrue(result[0].official)
        self.assertEqual(result[0].names["en"], "Stardust Mint Chocolate")

    def test_nonofficial_glossary_is_discovery_only(self):
        result = self.extract([self.glossary(official=False)])
        self.assertEqual([t.canonical for t in result], [self.name])
        self.assertFalse(result[0].official)
        self.assertEqual(result[0].names, {"zh_hans": self.name})

    def test_database_integer_official_flag_is_supported(self):
        result = self.extract([self.glossary(official=1)])
        self.assertTrue(result[0].official)
        self.assertEqual(result[0].names["en"], "Stardust Mint Chocolate")

    def test_demo_record_cannot_lend_official_translations(self):
        result = self.extract([self.glossary(demo=True)])
        self.assertFalse(result[0].official)
        self.assertEqual(result[0].names, {"zh_hans": self.name})

    def test_conflicting_official_names_are_not_arbitrarily_inherited(self):
        result = self.extract([self.glossary(), self.glossary(en="Other Chocolate")])
        self.assertNotIn("en", result[0].names)
        self.assertEqual(result[0].names["zh_hans"], self.name)

    def test_occurrence_lines_exclude_speaker_and_count_each_line_once(self):
        pages = [page("zh_hans", f"{self.name}：无关台词。\n少女：{self.name}、{self.name}。"),
                 page("ja", "少女：食べよう。")]
        with patch.object(zf, "_load_manual_seed", return_value=set()), \
                patch.object(zf, "discover_words", return_value=set()):
            result = zf.extract_terms_zhfirst(pages, [], [self.glossary()])
        self.assertEqual(result[0].lines_n, 1)

    def test_full_chapter_does_not_inherit_sixty_term_preview_limit(self):
        names = {"星砂" + chr(0x4e10 + i) + "试验物" for i in range(75)}
        text = "少女：" + "，".join(sorted(names)) + "。"
        pages = [page("zh_hans", text), page("ja", "少女：展示を見よう。")]
        with patch.object(zf, "_load_manual_seed", return_value=names):
            result = zf.extract_terms_zhfirst(pages, [], [])
        self.assertEqual({term.canonical for term in result}, names)
        preview = zf.extract_zh_candidates_from_story(text, "s", set(), set(),
                                                     max_terms=10, seed=names)
        self.assertEqual(len(preview), 10)

    def test_complete_nominal_suffix_survives_a_statistical_prefix_end_to_end(self):
        for text, expected in (("少女：我们今天在音乐节。", True),
                               ("少女：音乐节目。", False)):
            pages = [page("zh_hans", text), page("ja", "少女：音楽の話をしよう。")]
            with patch.object(zf, "_load_manual_seed", return_value=set()), \
                    patch.object(zf, "discover_words", return_value={"音乐"}), \
                    patch.object(zf, "_discover_content_words", return_value=set()):
                result = zf.extract_terms_zhfirst(pages, [], [])
            names = {term.canonical for term in result}
            self.assertEqual("音乐节" in names, expected)
            self.assertNotIn("我们今天在音乐节", names)

    def test_everyday_content_words_reach_the_shared_alignment_engine(self):
        pages = [p for i in (1, 2) for p in (
            page("zh_hans", "少女：观察下弦。", i), page("ja", "少女：下弦を見よう。", i))]
        with patch.object(zf, "_load_manual_seed", return_value={"下弦"}), \
                patch.object(ti, "build_alignment_resources", return_value=({}, {})), \
                patch.object(ti, "align_term_by_frequency", return_value="Last Quarter") as align:
            result = zf.extract_terms_zhfirst(pages, ["en"], [], do_align=True)
        self.assertTrue(result[0].everyday)
        self.assertEqual(result[0].names["en"], "Last Quarter")
        self.assertEqual(align.call_args.args[:3], ("下弦", "zh_hans", "en"))


if __name__ == "__main__":
    unittest.main()
