"""Quality regressions: actual spans, independent evidence and stable API.

All corpora are synthetic, in memory. No production store is read or written.
"""
import itertools
import unittest

from sekaisync import termindex
from sekaisync.line_alignment import (
    align_lines, contains_term, select_translation, source_term_occurrences,
    strip_speaker_label,
)
from sekaisync.normalize import normalize_name


NAMES = {"ja": "星庭", "zh_hans": "星庭", "zh_tw": "星庭",
         "en": "Star Garden", "ko": "스타가든"}
LINES = {
    "ja": ["司：最初の場面 101。", "司：星庭へ行こう！ 202", "類：最後の場面 303。"],
    "zh_hans": ["司：第一场面 101。", "司：去星庭吧！ 202", "类：最后场面 303。"],
    "zh_tw": ["司：第一場景 101。", "司：去星庭吧！ 202", "類：最後場景 303。"],
    "en": ["Tsukasa: First scene 101.", "Tsukasa: Star Garden! 202", "Rui: Last scene 303."],
    "ko": ["츠카사: 첫 번째 이야기 101.", "츠카사: 스타가든 가자! 202", "루이: 마지막 이야기 303."],
}


def _page(language, text, episode=1):
    # Include corpus alias spelling as well as the public canonical spelling.
    stored = "zh_hant" if language == "zh_tw" else language
    return {"id": f"web:fixture:event_story:901:{episode}:{stored}",
            "url": f"https://example.test/{stored}/story/event/901/{episode}/",
            "kind": "event_story", "language": stored, "text": text, "trust": "B"}


def _groups():
    return {f"event:901:{episode}": {
        "zh_hant" if language == "zh_tw" else language:
        _page(language, "\n".join(lines), episode)
        for language, lines in LINES.items()
    } for episode in (1, 2)}


class MonotoneLineAlignmentTest(unittest.TestCase):
    def test_all_twenty_directed_language_pairs_preserve_the_turn(self):
        for source, target in itertools.permutations(NAMES, 2):
            with self.subTest(source=source, target=target):
                aligned = align_lines(LINES[source], LINES[target], source, target)
                self.assertEqual(aligned.target_indices(1), (1,))
                self.assertGreater(aligned.confidence(1), 0)

    def test_inserted_dialogue_does_not_shift_every_later_line(self):
        target = list(LINES["en"])
        target[1:1] = ["Narrator: A new unrelated announcement.",
                        "Narrator: Golden Palace is closed.",
                        "Narrator: An extra translated aside."]
        aligned = align_lines(LINES["ja"], target, "ja", "en")
        self.assertEqual(aligned.target_indices(1), (4,))
        reverse = align_lines(target, LINES["ja"], "en", "ja")
        self.assertEqual(reverse.target_indices(4), (1,))
        self.assertFalse(reverse.target_indices(2))

    def test_split_and_merged_subtitles_align_in_both_directions(self):
        source = ["司：最初の場所 123", "司：星庭へ行こう！新しい場所へ行こう。", "類：最後の場所 987"]
        target = ["Tsukasa: First place 123", "Tsukasa: Star Garden!",
                  "Tsukasa: Let us go to the new place.", "Rui: Last place 987"]
        self.assertEqual(align_lines(source, target).target_indices(1), (1, 2))
        reverse = align_lines(target, source)
        self.assertEqual(reverse.target_indices(1), (1,))
        self.assertEqual(reverse.target_indices(2), (1,))

    def test_ambiguous_repeated_lines_are_not_positional_evidence(self):
        for source, target in ((["はい。"] * 3, ["Yes."] * 4),
                               (["Yes."] * 4, ["はい。"] * 3)):
            self.assertFalse(any(align_lines(source, target).targets))

    def test_no_merge_across_different_speakers(self):
        aligned = align_lines(["司：星庭へ行こう！"],
                              ["Tsukasa: Silver Garden!", "Rui: Golden Palace!"])
        self.assertFalse(aligned.target_indices(0))

    def test_large_page_is_bounded_and_abstains(self):
        result = align_lines(["hello"] * 5000, ["hello"] * 5000)
        self.assertFalse(any(result.targets))
        self.assertEqual(result.reason(0), "alignment_budget")

    def test_nfkc_and_latin_word_boundaries(self):
        self.assertTrue(contains_term("Visit ＳＥＫＡＩ!", "sekai"))
        self.assertTrue(contains_term("Star\nGarden", "Star Garden"))
        self.assertTrue(contains_term("星庭へ行こう", "星庭"))
        self.assertFalse(contains_term("Party", "Art"))
        self.assertFalse(contains_term("SEKAIPedia", "SEKAI"))
        self.assertFalse(contains_term("セカイシンフォニー", "セカイ"))
        self.assertFalse(contains_term("スカイトーン", "カイト"))
        self.assertTrue(contains_term("セカイへ行こう", "セカイ"))
        self.assertTrue(contains_term("カイトくん", "カイト"))

    def test_body_newlines_reconstruct_complete_dialogue_turns(self):
        # One native utterance can wrap to three source lines and one target
        # line. Line counts differ, but the speaker turn sequence is identical.
        source = ["遥：一番目の話です。", "遥：星庭について、", "必要なものを、",
                  "全部用意しました。", "愛莉：すごい施設ですね！", "愛莉：楽しみです。"]
        target = ["Haruka: This is the first topic.",
                  "Haruka: Everything needed for Star Garden is ready.",
                  "Airi: What an amazing place!", "Airi: I look forward to it."]
        aligned = align_lines(source, target, "ja", "en")
        self.assertEqual(aligned.reason(2), "speaker_turn_sequence")
        self.assertEqual(aligned.target_indices(1), (1,))
        self.assertEqual(aligned.target_indices(2), (1,))
        self.assertEqual(aligned.target_indices(3), (1,))
        self.assertEqual(align_lines(target, source).target_indices(1), (1, 2, 3))

    def test_turn_fast_path_rejects_conflicting_anchor_positions(self):
        source = ["A: Opening 101", "B: Star Garden 202", "A: Ending 303"]
        target = ["甲：开始 202", "乙：星庭 101", "甲：结束 303"]
        aligned = align_lines(source, target)
        self.assertNotEqual(aligned.reason(0), "speaker_turn_sequence")

    def test_reordered_anchors_cannot_be_overridden_by_speaker_pattern(self):
        source = ["A: Opening 101", "B: Star Garden 202", "A: Another topic 303", "B: Ending 404"]
        target = ["甲：别的话题 303", "乙：星庭 202", "甲：开始 101", "乙：结束 404"]
        aligned = align_lines(source, target)
        self.assertNotIn("speaker_turn_sequence", aligned.reasons)
        for si, indices in enumerate(aligned.targets):
            marker = source[si].split()[-1]
            for ti in indices:
                self.assertIn(marker, target[ti])

    def test_percent_and_year_quantities_do_not_form_a_false_anchor(self):
        source = ["A: The theatre has a long history.", "B: That sounds wonderful.",
                  "A: No change can have a 100% approval rating.", "B: I understand."]
        target = ["甲：剧场已有100年的历史。", "乙：听起来很不错。",
                  "甲：变化不可能获得所有观众的支持。", "乙：我明白。"]
        aligned = align_lines(source, target)
        self.assertEqual(aligned.targets, ((0,), (1,), (2,), (3,)))
        self.assertTrue(all(reason == "speaker_turn_sequence" for reason in aligned.reasons))
        same_unit = align_lines(["A: The theatre is 100 years old."], ["甲：剧场已成立100年。"])
        # Age is deliberately a distinct quantity category from elapsed years.
        self.assertEqual(same_unit.target_indices(0), (0,))
        year = align_lines(["A: The theatre opened 100 years ago."], ["甲：剧场已成立100年。"])
        self.assertEqual(year.reason(0), "shared_anchor")
        percentage = align_lines(["A: Support reached 100%."], ["甲：支持率达到了100％。"])
        self.assertEqual(percentage.reason(0), "shared_anchor")

    def test_lowercase_live_is_not_an_anchor_for_live_house(self):
        source = ["A: We played at a venue.", "B: It went well.",
                  "A: They showed us a new way to live.", "B: We learned a lot."]
        target = ["甲：我们开始在Live House演出。", "乙：演出很成功。",
                  "甲：他们也教会了我们如何生活。", "乙：我们学到了很多。"]
        aligned = align_lines(source, target)
        self.assertEqual(aligned.targets, ((0,), (1,), (2,), (3,)))
        self.assertTrue(all(reason == "speaker_turn_sequence" for reason in aligned.reasons))
        proper = align_lines(["A: Welcome to Star Garden."], ["甲：欢迎来到Star Garden。"])
        self.assertEqual(proper.reason(0), "shared_anchor")

    def test_unique_entity_surface_cannot_override_recurring_speaker_identity(self):
        source = ["A: Leo/need will release new songs.", "B: I use their songs often.",
                  "A: I agree with the proposal.", "B: I look forward to it."]
        target = ["甲：这个组合将发布新的歌曲。", "乙：我之前一直在用Leo/need的歌。",
                  "甲：我赞成这个提议。", "乙：我很期待。"]
        aligned = align_lines(source, target)
        self.assertEqual(aligned.targets, ((0,), (1,), (2,), (3,)))
        self.assertTrue(all(reason == "speaker_turn_sequence" for reason in aligned.reasons))

    def test_unique_speakers_do_not_manufacture_a_recurrence_pattern(self):
        source = ["甲：今日は良い天気。", "乙：公園へ行こう。", "丙：楽しみです。"]
        target = ["Alice: The weather is fine.", "Bob: Let us go to the park.", "Carol: Sounds fun."]
        aligned = align_lines(source, target)
        self.assertNotIn("speaker_turn_sequence", aligned.reasons)
        self.assertLessEqual(max(aligned.confidences), 0.78)

    def test_shared_speaker_labels_cannot_be_permuted_to_fake_identity(self):
        source = ["A: One topic.", "B: Another topic.", "A: Later topic.", "B: Final topic."]
        target = ["B: 別の話です。", "A: 別の内容です。", "B: 次の話です。", "A: 最後です。"]
        aligned = align_lines(source, target)
        self.assertNotIn("speaker_turn_sequence", aligned.reasons)
        for si, indices in enumerate(aligned.targets):
            for ti in indices:
                self.assertEqual(source[si][0], target[ti][0])

    def test_names_across_soft_wraps_keep_boundaries_and_original_indices(self):
        self.assertTrue(contains_term("网络\n天堂", "网络天堂"))
        self.assertTrue(contains_term("NetPara\ndise", "NetParadise"))
        self.assertFalse(contains_term("セカイ\nシンフォニー", "セカイ"))
        self.assertEqual(source_term_occurrences(
            ["A: An opening.", "B: Star", "Garden is lovely."], "Star Garden"),
            {1: "B: Star\nGarden is lovely."})
        self.assertEqual(source_term_occurrences(["Star Garden: An opening."], "Star Garden"), {})

    def test_speaker_parser_preserves_times_urls_and_recognizes_titles(self):
        for text in ("We meet at 12:30.", "https://example.test/StarGarden"):
            self.assertEqual(strip_speaker_label(text), text)
        self.assertEqual(strip_speaker_label("Mr. Smith: Star Garden is ready."),
                         "Star Garden is ready.")

    def test_compatibility_colons_strip_metadata_without_changing_body_spelling(self):
        for colon in (":", "\uff1a", "\ufe55", "\ufe13"):
            with self.subTest(colon=colon):
                raw = "  Alice" + colon + " \U0001f600 Lending her a hand."
                self.assertEqual(strip_speaker_label(raw), "\U0001f600 Lending her a hand.")
                self.assertEqual(source_term_occurrences([raw], "Alice"), {})
                self.assertEqual(source_term_occurrences([raw], "Lending"), {0: raw})

    def test_only_nested_near_ties_can_choose_longer_surface(self):
        self.assertEqual(select_translation([("Phoenix", 4), ("Phoenix Wonderland", 3.9)]),
                         "Phoenix Wonderland")
        self.assertEqual(select_translation([("Star Garden", 4), ("Golden Palace", 3.9)]), "")
        self.assertEqual(select_translation([("Art", 4), ("Party", 4)]), "")


class TranslationEvidenceTest(unittest.TestCase):
    def test_all_twenty_directed_term_translations(self):
        groups = _groups()
        vocab = {language: {normalize_name(name)} for language, name in NAMES.items()}
        idf = termindex.compute_lang_idf(groups, NAMES, vocab=vocab)
        for source, target in itertools.permutations(NAMES, 2):
            with self.subTest(source=source, target=target):
                self.assertEqual(termindex.align_term_by_frequency(
                    NAMES[source], source, target, groups, idf, vocab=vocab,
                    src_stories=set(groups)), NAMES[target])

    def test_vocabulary_preserves_internal_function_characters_and_display(self):
        allowed = {normalize_name(word) for word in ("地下街", "未来", "Leo/need", "Star Garden")}
        candidates = termindex._aligned_span_candidates(
            "司：地下街的未来，Leo/need meets Star Garden!", "zh_hans", allowed)
        self.assertEqual(set(candidates), {"地下街", "未来", "Leo/need", "Star Garden"})
        self.assertEqual(termindex._aligned_span_candidates("Art in Party", "en", {"art"}), ("Art",))

    def test_vocabulary_changes_do_not_reuse_filtered_candidates(self):
        vocabulary = {"地下街"}
        self.assertEqual(termindex._aligned_span_candidates("地下街与未来", "zh_hans", vocabulary), ("地下街",))
        vocabulary.clear()
        vocabulary.add("未来")
        self.assertEqual(termindex._aligned_span_candidates("地下街与未来", "zh_hans", vocabulary), ("未来",))

    def test_english_vocabulary_without_official_seeds_still_builds_idf(self):
        groups = _groups()
        vocabulary = termindex.build_alignment_vocab(groups, ["en"], [])
        self.assertIn("stargarden", vocabulary["en"])
        idf = termindex.compute_lang_idf(groups, ["en"], vocab=vocabulary)
        self.assertIn(("en", "Star Garden"), idf)
        self.assertEqual(termindex.align_term_by_frequency(
            "星庭", "ja", "en", groups, idf, vocabulary, src_stories=set(groups)), "Star Garden")

    def test_ordinary_content_word_is_discovered_in_target_and_aligned(self):
        # Common constituent characters suppress PMI, but repeated bounded
        # full words supply the same content-word evidence as on the source.
        for target_language in ("zh_hans", "zh_tw"):
            groups = {}
            for episode in range(1, 13):
                source = "司：下弦の月だ！"
                target = "少女：下弦。"
                groups[f"event:901:{episode}"] = {
                    "ja": _page("ja", source, episode),
                    target_language: _page(target_language, target, episode)}
            background = "\n".join(["甲乙丙丁戊己庚辛壬癸"] * 500 + ["下"] * 100 + ["弦"] * 100)
            groups["event:901:99"] = {
                target_language: _page(target_language, background, 99)}
            vocabulary = termindex.build_alignment_vocab(groups, [target_language], [])
            self.assertIn("下弦", vocabulary[target_language])
            idf = termindex.compute_lang_idf(groups, [target_language], vocab=vocabulary)
            self.assertEqual(termindex.align_term_by_frequency(
                "下弦", "ja", target_language, groups, idf, vocabulary,
                src_stories=set(groups)), "下弦")

    def test_source_speaker_name_does_not_supply_frequency_evidence(self):
        groups = _groups()
        for by in groups.values():
            by["ja"]["text"] = "星庭：今日はとても良い天気ですね。"
            by["en"]["text"] = "Tsukasa: Star Garden is lovely."
        vocabulary = {"en": {"stargarden"}}
        idf = termindex.compute_lang_idf(groups, ["en"], vocab=vocabulary)
        self.assertEqual(termindex.align_term_by_frequency(
            "星庭", "ja", "en", groups, idf, vocabulary, src_stories=set(groups)), "")

    def test_nearby_unrelated_name_is_not_a_candidate(self):
        groups = _groups()
        for by in groups.values():
            by["en"]["text"] = "\n".join([
                "Tsukasa: Golden Palace 101.", "Tsukasa: Star Garden! 202", "Rui: Golden Palace 303."])
        vocab = {"en": {"stargarden", "goldenpalace"}}
        idf = termindex.compute_lang_idf(groups, ["en"], vocab=vocab)
        self.assertEqual(termindex.align_term_by_frequency(
            "星庭", "ja", "en", groups, idf, vocab, src_stories=set(groups)), "Star Garden")

    def test_equally_supported_different_names_abstain(self):
        groups = _groups()
        for by in groups.values():
            by["en"]["text"] = by["en"]["text"].replace("Star Garden!", "Star Garden and Golden Palace!")
        vocab = {"en": {"stargarden", "goldenpalace"}}
        idf = termindex.compute_lang_idf(groups, ["en"], vocab=vocab)
        self.assertEqual(termindex.align_term_by_frequency(
            "星庭", "ja", "en", groups, idf, vocab, src_stories=set(groups)), "")

    def test_every_source_occurrence_contributes_not_just_the_first(self):
        groups = _groups()
        for episode, by in enumerate(groups.values(), 1):
            by["ja"]["text"] = "司：星庭へ行こう 101。\n司：星庭へ行こう！ 202"
            other = "Silver Castle" if episode == 1 else "Golden Palace"
            by["en"]["text"] = f"Tsukasa: {other} 101.\nTsukasa: Star Garden! 202"
        vocab = {"en": {"stargarden", "silvercastle", "goldenpalace"}}
        idf = termindex.compute_lang_idf(groups, ["en"], vocab=vocab)
        self.assertEqual(termindex.align_term_by_frequency(
            "星庭", "ja", "en", groups, idf, vocab, src_stories=set(groups)), "Star Garden")

    def test_translation_memory_needs_independent_stories(self):
        pages = [_page("ja", "\n".join(["遥：ネットパラダイスに行こう！"] * 5)),
                 _page("en", "\n".join(["Haruka: Go to NetParadise!"] * 5))]
        memory = termindex.build_translation_memory(pages, "ja", ["en"])
        self.assertNotIn(("ネットパラダイス", "en"), memory)


class PublicPenetrationProjectionTest(unittest.TestCase):
    def _term(self, source="ja"):
        return termindex.TermRecord("test:star", NAMES[source], source, names=dict(NAMES),
                                    evidence=[{"story_key": "event:901:1"}])

    def test_all_source_languages_return_the_same_aligned_five_language_turn(self):
        for source in NAMES:
            with self.subTest(source=source):
                result = termindex.term_penetrate([self._term(source)], NAMES[source],
                                                  languages=list(NAMES), grouped=_groups())
                for language, entry in result["per_language"].items():
                    self.assertEqual(entry["term"], NAMES[language])
                    self.assertIn("202", entry["sentence"])
                    self.assertNotIn("missing", entry)

    def test_old_cached_positions_do_not_override_current_slots(self):
        term = self._term()
        term.slots = {"ja": {"status": "accepted", "value": "星庭"},
                      "en": {"status": "rejected", "value": "WrongName"}}
        term.positions = [{"story_key": "event:901:1", "language": "en",
                           "term": "WrongName", "sentence": "stale cached sentence", "trust": "A"}]
        result = termindex.term_penetrate([term], "星庭", languages=["en"], grouped=_groups())
        self.assertEqual(result["per_language"]["en"]["term"], "")
        self.assertEqual(result["per_language"]["en"]["sentence"], "")
        self.assertTrue(result["per_language"]["en"]["missing"])

    def test_a_name_elsewhere_in_story_does_not_certify_the_current_turn(self):
        groups = _groups()
        groups["event:901:1"]["en"]["text"] = "\n".join([
            "Tsukasa: Star Garden 101.", "Tsukasa: A different destination! 202", "Rui: Last scene 303."])
        result = termindex.term_penetrate([self._term()], "星庭", languages=["en"], grouped=groups)
        self.assertEqual(result["per_language"]["en"]["sentence"], "")
        self.assertTrue(result["per_language"]["en"]["missing"])

    def test_generator_pages_are_consumed_once(self):
        pages = (page for by in _groups().values() for page in by.values())
        result = termindex.term_penetrate([self._term()], "星庭", languages=["en"], pages=pages)
        self.assertEqual(result["story_key"], "event:901:1")
        self.assertIn("202", result["per_language"]["en"]["sentence"])

    def test_source_and_target_names_can_span_body_layout_lines(self):
        groups = _groups()
        groups["event:901:1"]["ja"]["text"] = groups["event:901:1"]["ja"]["text"].replace("星庭", "星\n庭")
        groups["event:901:1"]["en"]["text"] = groups["event:901:1"]["en"]["text"].replace("Star Garden", "Star\nGarden")
        result = termindex.term_penetrate([self._term()], "星庭", languages=["ja", "en"], grouped=groups)
        for language, name in (("ja", "星庭"), ("en", "Star Garden")):
            row = result["per_language"][language]
            self.assertNotIn("missing", row)
            self.assertTrue(contains_term(row["sentence"], name))

    def test_long_public_excerpt_and_persisted_position_keep_the_term(self):
        groups = _groups()
        by = groups["event:901:1"]
        by["ja"]["text"] = "司：" + "長い説明です。" * 60 + "星庭！ 202"
        by["en"]["text"] = "Tsukasa: " + "Some long explanation. " * 40 + "Star Garden! 202"
        result = termindex.term_penetrate([self._term()], "星庭", languages=["ja", "en"], grouped=groups)
        positions = termindex._build_positions_for_term(self._term(), groups)
        for language in ("ja", "en"):
            row = result["per_language"][language]
            self.assertLessEqual(len(row["sentence"]), 300)
            self.assertIn(NAMES[language], row["sentence"])
            position = next(p for p in positions if p["language"] == language)
            self.assertIn(NAMES[language], position["sentence"])

    def test_speaker_only_source_and_target_are_not_dialogue_occurrences(self):
        for source_only in (True, False):
            groups = _groups()
            if source_only:
                groups["event:901:1"]["ja"]["text"] = "星庭：今日は良い天気です。"
            else:
                groups["event:901:1"]["en"]["text"] = "Star Garden: We are going somewhere else! 202"
            result = termindex.term_penetrate([self._term()], "星庭", languages=["en"], grouped=groups)
            self.assertTrue(result["per_language"]["en"]["missing"])
            self.assertEqual(result["per_language"]["en"]["sentence"], "")

    def test_homonymous_conflicting_records_do_not_depend_on_input_order(self):
        first, second = self._term(), self._term()
        second.id = "test:other-star"
        second.names["en"] = "Golden Palace"
        second.evidence = [{"story_key": "event:901:2"}]
        for records in ([first, second], [second, first]):
            self.assertIsNone(termindex.term_penetrate(records, "星庭", languages=["en"], grouped=_groups()))
            scoped = termindex.term_penetrate(records, "星庭", story_key="event:901:1",
                                               languages=["en"], grouped=_groups())
            self.assertEqual(scoped["per_language"]["en"]["term"], "Star Garden")

    def test_complete_requested_language_coverage_beats_frequent_partial_story(self):
        groups = _groups()
        groups["event:901:1"] = {language: page for language, page in groups["event:901:1"].items()
                                  if language in ("ja", "en")}
        term = self._term()
        term.evidence = [{"story_key": "event:901:1"}] * 4 + [{"story_key": "event:901:2"}]
        result = termindex.term_penetrate([term], "星庭", languages=list(NAMES), grouped=groups)
        self.assertEqual(result["story_key"], "event:901:2")
        self.assertTrue(all(row["sentence"] for row in result["per_language"].values()))


if __name__ == "__main__":
    unittest.main()
