"""End-to-end counterexamples for the scrubber's evidence plumbing.

These fixtures measure wrong-pair rejection and exact paired occurrences;
increasing the accepted count is not the success criterion.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from sekaisync import penetrate_channels as channels, trinity


def story(**texts):
    return {language: {"text": text} for language, text in texts.items()}


class AlignmentPlumbingTests(unittest.TestCase):
    def tearDown(self):
        channels.clear_known_names()

    def test_neighbouring_transliteration_is_not_a_translation(self):
        groups = {key: story(
            ja="1 セカイへ行く\n2 その場所へ行く\n3 歌いましょう",
            en="1 go to that place\n2 go to SEKAI\n3 let us sing",
        ) for key in ("s1", "s2")}
        self.assertEqual(channels.channel_c_katakana_to_english(
            groups, list(groups), {"セカイ"}), {})
        self.assertFalse(channels.verify_triangle(
            "セカイ", "ja", "SEKAI", "en", groups, max_aux_required=0,
        )["verified"])

    def test_natural_dialogue_neighbour_is_not_accepted_without_number_anchors(self):
        groups = {key: story(
            ja="セカイへ行こう\n楽譜を持ってきて\n今日はいい天気だね",
            en="Let's go to the music room\nPlease bring the SEKAI score\nThe weather is nice today",
        ) for key in ("s1", "s2")}
        self.assertEqual(channels.channel_c_katakana_to_english(
            groups, list(groups), {"セカイ"}), {})

    def test_sentence_initial_titlecase_transliteration_is_recalled(self):
        groups = {key: story(ja="カイトと歌います", en="Kaito sings with us")
                  for key in ("s1", "s2")}
        self.assertEqual(channels.channel_c_katakana_to_english(
            groups, list(groups), {"カイト"}), {"カイト": "Kaito"})
        result = trinity.scrub_trinity(groups, list(groups), ["カイト"], target_languages=("en",))
        self.assertEqual(result["accepted"]["カイト"]["names"]["en"], "Kaito")

    def test_latin_names_do_not_absorb_a_sentence_directive(self):
        for extractor in (channels.extract_latin_candidates, trinity._latin_candidates_fast):
            self.assertEqual(extractor("Please Visit RAD WEEKEND today"), ["RAD WEEKEND"])
            self.assertEqual(extractor("We met at Big Ben"), ["Big Ben"])
            self.assertEqual(extractor('The title is "Meet Miku"'), ["Meet Miku"])

    def test_exact_transliteration_recovers_a_name_inside_different_phrases(self):
        groups = {str(i): story(ja="セカイの話", en=line) for i, line in enumerate(
            ("Let's go to SEKAI", "Big SEKAI", "Song of SEKAI"))}
        result = trinity.scrub_trinity(groups, list(groups), ["セカイ"], target_languages=("en",))
        self.assertEqual(result["accepted"]["セカイ"]["names"], {"en": "SEKAI"})

    def test_content_word_transliteration_is_not_blocked_for_being_common(self):
        groups = {key: story(ja="ピアノを弾く", en="I play the piano") for key in ("s1", "s2")}
        result = trinity._channel_translit(trinity._Corpus(groups, list(groups)), ["ピアノ"],
                                            source_language="ja", target_languages=("en",))
        self.assertIn("piano", result["ピアノ"]["en"])

    def test_triangle_must_close_the_same_source_occurrence(self):
        groups = {"s": story(
            ja="1 セカイへ行く\n2 ここにいる\n3 セカイの話",
            en="1 go to SEKAI\n2 stay right here\n3 talk about a place",
            zh_hans="1 去那个地方\n2 待在这里\n3 聊聊SEKAI",
        )}
        self.assertFalse(channels.verify_triangle(
            "セカイ", "ja", "SEKAI", "en", groups,
            aux_languages=("zh_hans",),
        )["verified"])

    def test_two_aux_languages_cannot_close_different_occurrences(self):
        groups = {"s": story(
            ja="1 セカイへ行く\n2 ここにいる\n3 セカイの話",
            en="1 go to SEKAI\n2 stay right here\n3 talk about SEKAI",
            zh_hans="1 去SEKAI\n2 待在这里\n3 聊聊那个地方",
            ko="1 그곳에 가다\n2 여기 있어\n3 SEKAI 이야기",
        )}
        self.assertFalse(channels.verify_triangle(
            "セカイ", "ja", "SEKAI", "en", groups,
            aux_languages=("zh_hans", "ko"), max_aux_required=2,
        )["verified"])

    def test_single_story_official_names_work_in_all_five_source_languages(self):
        names = {"ja": "星屑庭園", "en": "STAR GARDEN", "zh_hans": "星屑花园",
                 "zh_hant": "星屑花園", "ko": "별가루 정원"}
        groups = {"five-server-story": story(**names)}
        glossary = [SimpleNamespace(id="area:garden", kind="area", canonical=names["ja"],
                                    names=names, official=True)]
        for source, surface in names.items():
            with self.subTest(source=source):
                targets = tuple(language for language in names if language != source)
                result = trinity.scrub_trinity(groups, list(groups), [surface],
                                               source_language=source, target_languages=targets,
                                               glossary=glossary)
                actual = result["accepted"][surface]["names"]
                self.assertEqual({lang: actual[lang] for lang in targets},
                                 {lang: names[lang] for lang in targets})

    def test_latin_occurrence_index_respects_width_case_and_boundaries(self):
        groups = {"s": story(en="Party\nART gallery\nＡｒｔ studio\nCartography")}
        index = channels._build_term_line_index(groups, list(groups), "en", ["Art"])
        self.assertEqual(index.hits["Art"]["s"], {1, 2})

    def test_cached_trunk_delegates_to_shared_engine(self):
        corpus = trinity._Corpus({}, [])
        with patch.object(trinity.termindex, "align_term_by_frequency", return_value="真实译名") as align:
            value = trinity._align_term_cached(trinity._FastAligner(corpus), "候補", "ja", "zh_hans",
                                               {}, None, {"s1", "s2"}, {})
        self.assertEqual(value, "真实译名")
        align.assert_called_once()

    def test_anchor_only_verification_does_not_run_auxiliary_alignment(self):
        groups = {"s": story(ja="セカイ", en="SEKAI")}
        with patch.object(channels, "_aux_name_for", side_effect=AssertionError("unrequested aux lookup")):
            result = channels.verify_triangle("セカイ", "ja", "SEKAI", "en", groups, max_aux_required=0)
        self.assertTrue(result["verified"])

    def test_scoped_view_does_not_retain_the_full_corpus_dictionary(self):
        groups = {str(i): story(ja="セカイ", en="SEKAI") for i in range(100)}
        view = trinity._StoryScopedView(groups, ["42"])
        self.assertEqual(dict.__len__(view), 1)
        self.assertEqual(set(view), {"42"})

    def test_new_scrub_batch_does_not_inherit_old_glossary(self):
        channels.register_known_names("セカイ", {"zh_hans": "旧译名"})
        trinity.scrub_trinity({}, [], [], target_languages=("en",))
        self.assertEqual(channels._aux_name_for("セカイ", "zh_hans"), ("", ""))

    def test_speaker_metadata_does_not_count_as_a_source_occurrence(self):
        groups = {key: story(ja="カイト：今日は歌おう\nミク：いいね",
                              en="Kaito: Let us sing today\nMiku: Sounds great")
                  for key in ("s1", "s2")}
        index = channels._build_term_line_index(groups, list(groups), "ja", ["カイト"])
        self.assertEqual(index.hits["カイト"], {})
        self.assertEqual(channels._locate_term_lines(groups, "ja", "カイト"), {})
        self.assertFalse(channels.verify_triangle("カイト", "ja", "Kaito", "en", groups,
                                                  max_aux_required=0)["verified"])

    def test_source_name_split_inside_a_dialogue_turn_is_located(self):
        groups = {"s": story(ja="一歌：カイ\nトと歌おう\n咲希：楽しみだね",
                             en="Ichika: Let us sing with KAITO\nSaki: That sounds fun")}
        index = channels._build_term_line_index(groups, list(groups), "ja", ["カイト"])
        self.assertEqual(index.hits["カイト"], {"s": {0}})
        self.assertTrue(channels.verify_triangle("カイト", "ja", "KAITO", "en", groups,
                                                 max_aux_required=0)["verified"])

    def test_target_speaker_metadata_is_not_an_alignment_witness(self):
        groups = {"s": story(ja="一歌：カイトと歌おう", en="KAITO: Let's all sing")}
        self.assertFalse(channels.verify_triangle("カイト", "ja", "KAITO", "en", groups,
                                                  max_aux_required=0)["verified"])

    def test_vote_tie_cannot_choose_an_unrelated_longer_name(self):
        self.assertEqual(trinity._pick_vote_winner(
            {"KAITO": {"s1", "s2"}, "Wonder Stage": {"s1", "s2"}}, 2,
            min_stories=2, min_ratio=0.25,
        ), ("", 0))

    def test_unverified_english_pivot_cannot_backfill_chinese(self):
        groups = {key: story(ja="朝比奈", en="I saw Asahina", zh_hans="月光庭园")
                  for key in ("s1", "s2")}
        result = trinity._channel_hub(
            trinity._Corpus(groups, list(groups)), ["朝比奈"], source_language="ja",
            target_languages=("en", "zh_hans"), official_keys={"朝比奈"},
        )
        self.assertIn("en", result["朝比奈"])
        self.assertNotIn("zh_hans", result["朝比奈"])

    def test_foreign_literal_requires_the_same_aligned_position(self):
        groups = {key: story(
            ja="1 セカイへ行く\n2 その場所へ行く\n3 歌いましょう",
            en="1 go to SEKAI\n2 go to another place\n3 let us sing",
            zh_hans="1 去那个地方\n2 前往SEKAI\n3 一起唱歌",
        ) for key in ("s1", "s2")}
        self.assertEqual(trinity._foreign_literal_hits(
            trinity._Corpus(groups, list(groups)), {key: {0} for key in groups}, "SEKAI", "en",
            ("en", "zh_hans"), anchor_language="ja",
        ), {})


class EvidenceIntegrityTests(unittest.TestCase):
    def test_collision_pruning_preserves_authoritative_aliases(self):
        accepted = {term: {"names": {"en": "SEKAI"}, "channels": ["L0"], "confidence": 0.98}
                    for term in ("セカイ", "世界")}
        kept, pending = trinity._prune_value_collisions(
            accepted, authoritative_names={term: {"en": "SEKAI"} for term in accepted},
        )
        self.assertEqual(set(kept), {"セカイ", "世界"})
        self.assertEqual(pending, [])

    def test_related_pair_cannot_exempt_an_unrelated_third_term(self):
        accepted = {term: {"names": {"en": "SEKAI"}, "channels": ["trunk"], "confidence": 0.7}
                    for term in ("セカイ", "セカイ音楽祭", "別世界")}
        kept, pending = trinity._prune_value_collisions(accepted)
        self.assertNotIn("別世界", kept)
        self.assertIn("別世界", {row["term"] for row in pending})

    def test_final_collision_demotion_reaches_slot_decisions(self):
        groups = {"s1": story(ja="セカイ\nカイト", en="SEKAI\nKAITO")}
        accepted = {"セカイ": {"names": {"en": "WrongName"}, "channels": ["trunk"], "confidence": 0.7},
                    "カイト": {"names": {"en": "WrongName"}, "channels": ["trunk"], "confidence": 0.7}}
        merged = {"accepted": accepted, "pending": [], "conflicts": [], "agreement_boosted": 0,
                  "stats_counter": {}, "slot_decisions": [
                      {"term": term, "language": "en", "value": "WrongName", "status": "accepted",
                       "evidence": [], "evidence_rows": [], "candidates": {"WrongName": ["trunk"]}}
                      for term in accepted]}
        with patch.object(trinity, "_merge_channels", return_value=merged):
            result = trinity.scrub_trinity(groups, list(groups), list(accepted), target_languages=("en",))
        self.assertEqual(result["accepted"], {})
        self.assertTrue(result["slot_decisions"])
        self.assertTrue(all(row["status"] == "pending" for row in result["slot_decisions"]))

    def test_nonofficial_channel_priority_does_not_hide_conflicts(self):
        proposals = {"en": {"SEKAI": ["trunk", "hub"], "WrongWorld": ["translit"]}}
        verdict = trinity.arbitrate("セカイ", proposals)
        self.assertEqual(verdict["resolved"], {})
        self.assertEqual(set(verdict["conflicts"][0]["candidates"]), {"SEKAI", "WrongWorld"})

    def test_official_authority_still_resolves_conflicting_proposals(self):
        verdict = trinity.arbitrate("セカイ", {"en": {"SEKAI": ["L0"], "WrongWorld": ["translit"]}})
        self.assertEqual(verdict["resolved"], {"en": "SEKAI"})
        self.assertEqual(verdict["conflicts"], [])

    def test_surface_variants_are_not_false_conflicts(self):
        verdict = trinity.arbitrate("セカイ", {"en": {"SEKAI": ["trunk"], "Ｓｅｋａｉ": ["translit"]}})
        self.assertEqual(len(verdict["resolved"]), 1)
        self.assertEqual(verdict["conflicts"], [])

    def test_layered_conflicting_channels_keep_every_candidate_pending(self):
        groups = {key: story(ja="カイト", en="KAITO", zh_hans="KAITO") for key in ("s1", "s2")}
        with patch.object(channels, "_channel_a_align", return_value={"en": "WrongName"}):
            result = channels.penetrate_layered(groups, list(groups), ["カイト"],
                                                target_languages=("en",), idf={})
        self.assertNotIn("カイト", result["pairs"])
        self.assertEqual({row["candidate"] for row in result["pending"]}, {"KAITO", "WrongName"})

    def test_evidence_rows_cannot_borrow_an_unaligned_target_sentence(self):
        groups = {"s": story(
            ja="1 セカイへ行く\n2 その場所へ行く\n3 歌いましょう",
            en="1 go to that place\n2 go to SEKAI\n3 let us sing",
        )}
        payload = {"value": "SEKAI", "evidence": {"channel": "trunk", "story_keys": ["s"]}}
        rows = trinity.channel_evidence_rows("セカイ", "en", "SEKAI", payload,
                                              trinity._Corpus(groups, list(groups)))
        self.assertEqual(rows, [])

    def test_evidence_rows_retain_source_and_target_coordinates(self):
        groups = {"s": story(ja="1 セカイへ行く\n2 歌いましょう",
                             en="1 go to SEKAI\n2 let us sing")}
        payload = {"value": "SEKAI", "evidence": {"channel": "trunk", "story_keys": ["s"]}}
        rows = trinity.channel_evidence_rows("セカイ", "en", "SEKAI", payload,
                                              trinity._Corpus(groups, list(groups)))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["source_line"], rows[0]["target_line"]), (0, 0))
        self.assertIn("セカイ", rows[0]["source_sentence"])

    def test_soft_wrapped_target_surface_passes_existing_certificate_contract(self):
        from sekaisync import term_slots
        groups = {key: story(ja="一歌：星屑庭園へ行こう\n咲希：楽しみだね",
                             en="Ichika: Let's go to STAR\nGARDEN today\nSaki: That sounds fun")
                  for key in ("s1", "s2")}
        payload = {"value": "STAR GARDEN", "evidence": {"channel": "trunk", "story_keys": list(groups)}}
        rows = trinity.channel_evidence_rows("星屑庭園", "en", "STAR GARDEN", payload,
                                              trinity._Corpus(groups, list(groups)))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["observed_surface"] == "STAR\nGARDEN" for row in rows))
        self.assertTrue(all("STAR\nGARDEN" in row["sentence"] for row in rows))
        evidence = term_slots.evidence_with_ids("星屑庭園", rows)
        slot = {"term_id": "星屑庭園", "language": "en", "value": "STAR GARDEN",
                "status": "accepted", "source": "corpus", "confidence": 0.7,
                "evidence_refs": [row["evidence_id"] for row in evidence]}
        checked = term_slots._prepare_slot(slot, evidence, term_slots.corpus_verifier())
        self.assertEqual(checked["status"], "accepted")

    def test_three_channels_reading_one_corpus_are_one_evidence_origin(self):
        groups = {key: story(ja="セカイ", en="SEKAI") for key in ("s1", "s2")}
        payloads = {
            channel: {"セカイ": {"en": {"SEKAI": {
                "value": "SEKAI", "sim": 1.0,
                "evidence": {"channel": channel, "story_keys": list(groups), "verified": True},
            }}}} for channel in ("trunk", "hub", "translit")
        }
        result = trinity._merge_channels(payloads, corpus=trinity._Corpus(groups, list(groups)),
                                         source_language="ja", glossary_names={})
        self.assertEqual(result["accepted"]["セカイ"]["agreement"], 1)
        self.assertEqual(result["accepted"]["セカイ"]["confidence"], 0.95)
        self.assertEqual(result["agreement_boosted"], 0)


if __name__ == "__main__":
    unittest.main()
