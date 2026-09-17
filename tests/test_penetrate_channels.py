"""`sekaisync.penetrate_channels` 的单元测试（零依赖、不读 store）。"""

from __future__ import annotations

import unittest

from sekaisync.penetrate_channels import (
    channel_c_katakana_to_english,
    channel_stats_summary,
    clear_known_names,
    extract_latin_candidates,
    penetrate_layered,
    register_known_names,
    verify_triangle,
)


def _story(ja: str, en: str = "", zh: str = "", ko: str = "", zh_tw: str = "") -> dict:
    by: dict = {"ja": {"text": ja}}
    if en:
        by["en"] = {"text": en}
    if zh:
        by["zh_hans"] = {"text": zh}
    if zh_tw:
        by["zh_hant"] = {"text": zh_tw}
    if ko:
        by["ko"] = {"text": ko}
    return by


class ExtractLatinCandidatesTests(unittest.TestCase):
    def test_multiword_and_single_upper(self):
        # 句首普通词 + 后续大写词：保留整串（pilot 同款行为，靠跨故事投票消歧）。
        self.assertEqual(
            extract_latin_candidates("Visit RAD WEEKEND today"), ["Visit RAD WEEKEND"]
        )
        self.assertEqual(extract_latin_candidates("SEKAI is here"), ["SEKAI"])

    def test_sentence_initial_ambiguous_dropped(self):
        # 句首大写的普通词无第二个大写词佐证 → 丢弃。
        self.assertEqual(extract_latin_candidates("There is a SEKAI"), ["SEKAI"])
        self.assertEqual(extract_latin_candidates("This is RAD WEEKEND"), ["RAD WEEKEND"])
        # 全大写不受句首歧义规则影响（强专名形态）。
        self.assertEqual(extract_latin_candidates("SEKAI"), ["SEKAI"])

    def test_contractions_excluded(self):
        self.assertEqual(extract_latin_candidates("I'm You're"), [])
        self.assertEqual(extract_latin_candidates("Meiko: I'm fine"), [])

    def test_speaker_stripped(self):
        self.assertEqual(extract_latin_candidates("Airi: MORE MORE JUMP!"), ["MORE MORE JUMP"])

    def test_stopword_only_dropped(self):
        self.assertEqual(extract_latin_candidates("Thank you"), [])
        self.assertEqual(extract_latin_candidates(""), [])

    def test_letter_digit_brand(self):
        self.assertEqual(extract_latin_candidates("N25"), ["N25"])


class ChannelCTests(unittest.TestCase):
    def _corpus(self) -> dict:
        # 三个故事里 セカイ 与 SEKAI 同点位；テスト 对到 Huh 是行位噪声。
        # カイト 只出现在 s1（min_stories=2 会正确地剔除它，故不用于断言）。
        return {
            "s1": _story(
                "セカイに行こう\nカイトと歌う\nテストだよ\n",
                "Let's go to SEKAI\nSing with KAITO\nHuh, a test\n",
            ),
            "s2": _story(
                "セカイは広い\nまたテスト\n",
                "SEKAI is wide\nHuh, another test\n",
            ),
            "s3": _story(
                "セカイの歌\nテスト三度\n",
                "Song of SEKAI\nHuh, third test\n",
            ),
        }

    def test_true_penetration_kept(self):
        groups = self._corpus()
        got = channel_c_katakana_to_english(
            groups, sorted(groups), {"セカイ", "カイト", "テスト"}
        )
        self.assertEqual(got.get("セカイ"), "SEKAI")

    def test_single_story_term_dropped_by_min_stories(self):
        # カイト 只在 s1 出现：单故事术语无法做分布验证，诚实剔除。
        groups = self._corpus()
        got = channel_c_katakana_to_english(groups, sorted(groups), {"カイト"})
        self.assertNotIn("カイト", got)

    def test_noise_gated_out(self):
        groups = self._corpus()
        got = channel_c_katakana_to_english(groups, sorted(groups), {"テスト"})
        self.assertNotIn("テスト", got)

    def test_min_stories_filter(self):
        groups = self._corpus()
        got = channel_c_katakana_to_english(
            groups, ["s1"], {"セカイ"}, min_stories=2
        )
        self.assertEqual(got, {})

    def test_non_katakana_ignored(self):
        groups = self._corpus()
        got = channel_c_katakana_to_english(groups, sorted(groups), {"汉字词", "SEKAI"})
        self.assertEqual(got, {})

    def test_unpaired_only_corpus_returns_empty(self):
        groups = {"s1": _story("セカイ\n"), "s2": _story("セカイ\n")}
        got = channel_c_katakana_to_english(groups, sorted(groups), {"セカイ"})
        self.assertEqual(got, {})


class VerifyTriangleTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_known_names()

    def tearDown(self) -> None:
        clear_known_names()

    def _corpus(self) -> dict:
        return {
            "s1": _story(
                "セカイへ行く\n",
                "Go to SEKAI\n",
                "去SEKAI\n",
                zh_tw="去SEKAI\n",
                ko="SEKAI로 가자\n",
            ),
            "s2": _story(
                "セカイの歌\n",
                "Song of SEKAI\n",
                "SEKAI的歌\n",
                zh_tw="SEKAI的歌\n",
                ko="SEKAI의 노래\n",
            ),
        }

    def test_verified_when_aux_hits(self):
        groups = self._corpus()
        result = verify_triangle("セカイ", "ja", "SEKAI", "en", groups)
        self.assertTrue(result["verified"])
        self.assertGreaterEqual(result["aux_hits"]["zh_hans"]["hits"], 1)

    def test_unverified_without_aux(self):
        # zh/ko 文本里没有候选译名 → 锚定成功但闭环失败。
        groups = {
            "s1": _story("セカイへ行く\n", "Go to SEKAI\n", "去往那个地方\n"),
            "s2": _story("セカイの歌\n", "Song of SEKAI\n", "那首歌\n"),
        }
        result = verify_triangle(
            "セカイ", "ja", "SEKAI", "en", groups, aux_languages=("zh_hans",)
        )
        self.assertFalse(result["verified"])
        self.assertIn("闭环不足", result["reason"])

    def test_registry_supplies_aux_name(self):
        groups = {
            "s1": _story("セカイへ\n", "Go to SEKAI\n", "去世界\n"),
            "s2": _story("セカイの歌\n", "Song of SEKAI\n", "世界的歌\n"),
        }
        register_known_names("セカイ", {"zh_hans": "世界"})
        result = verify_triangle(
            "セカイ", "ja", "SEKAI", "en", groups, aux_languages=("zh_hans",)
        )
        self.assertTrue(result["verified"])
        self.assertEqual(result["aux_hits"]["zh_hans"]["method"], "registry")

    def test_no_anchor_fails(self):
        # P20b: Sekai/SEKAI are the same normalized value; this fixture has
        # SEKAI in both the target and aux pages, so case alone cannot fail it.
        groups = self._corpus()
        self.assertTrue(verify_triangle("セカイ", "ja", "Sekai", "en", groups)["verified"])
        result = verify_triangle("セカイ", "ja", "OTHER", "en", groups)
        self.assertFalse(result["verified"])
        self.assertIn("无锚定证据", result["reason"])

    def test_missing_term_fails(self):
        groups = self._corpus()
        result = verify_triangle("存在しない", "ja", "SEKAI", "en", groups)
        self.assertFalse(result["verified"])
        self.assertIn("未找到", result["reason"])

    def test_empty_inputs_fail(self):
        groups = self._corpus()
        self.assertFalse(verify_triangle("", "ja", "SEKAI", "en", groups)["verified"])
        self.assertFalse(verify_triangle("セカイ", "ja", "", "en", groups)["verified"])

    def test_max_aux_required_zero(self):
        groups = {
            "s1": _story("セカイへ\n", "Go to SEKAI\n", "去往那里\n"),
            "s2": _story("セカイの歌\n", "Song of SEKAI\n", "那首歌\n"),
        }
        result = verify_triangle(
            "セカイ", "ja", "SEKAI", "en", groups, max_aux_required=0
        )
        self.assertTrue(result["verified"])


class PenetrateLayeredTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_known_names()

    def tearDown(self) -> None:
        clear_known_names()

    def _corpus(self) -> dict:
        return {
            "s1": _story("セカイ\nカイト\nテスト\nLUMINA時間\n", "SEKAI\nKAITO\nHuh\nLUMINA Time\n"),
            "s2": _story("セカイ\nカイト\nテスト\nLUMINA時間\n", "SEKAI\nKAITO\nHuh\nLUMINA Time\n"),
            "s3": _story("セカイ\nカイト\nテスト\nLUMINA時間\n", "SEKAI\nKAITO\nHuh\nLUMINA Time\n"),
        }

    def test_layer0_rejects_generic(self):
        groups = self._corpus()
        res = penetrate_layered(
            groups, sorted(groups), ["大家", "咖啡", "テスト", "クラス"],
            target_languages=("en",),
        )
        self.assertEqual(res["tier_stats"]["L3"], 4)
        self.assertEqual(sorted(res["rejected"]), sorted(["大家", "咖啡", "テスト", "クラス"]))

    def test_channel_c_and_result_shape(self):
        groups = self._corpus()
        res = penetrate_layered(
            groups, sorted(groups), ["セカイ", "カイト", "テスト"],
            target_languages=("en",),
        )
        self.assertGreaterEqual(res["channel_stats"]["C"], 2)
        # P20b: 非官方槽未过三角闭环不得进 pairs，改入 pending 并带原因
        #（本夹具无 glossary、无 zh/ko 文本，闭环必然不足——实验事实）。
        self.assertNotIn("セカイ", res["pairs"])
        pending = {(p["term"], p["language"]): p for p in res["pending"]}
        self.assertIn(("セカイ", "en"), pending)
        self.assertTrue(pending[("セカイ", "en")]["reason"])
        self.assertNotIn("テスト", res["pairs"])
        for key in ("pairs", "pending", "tier_stats", "channel_stats",
                    "rejected", "skipped_reason"):
            self.assertIn(key, res)

    def test_skipped_reason_without_idf(self):
        groups = self._corpus()
        res = penetrate_layered(
            groups, sorted(groups), ["セカイ"], target_languages=("en",),
        )
        self.assertIn("A", res["skipped_reason"])

    def test_official_layer_wins(self):
        groups = self._corpus()

        class _G:
            canonical = "セカイ"
            names = {"ja": "セカイ", "en": "SEKAI", "zh_hans": "世界"}

        res = penetrate_layered(
            groups, sorted(groups), ["セカイ"], target_languages=("en",), glossary=[_G()],
        )
        self.assertEqual(res["tier_stats"]["L0"], 1)
        self.assertEqual(res["pairs"]["セカイ"]["en"], "SEKAI")
        self.assertEqual(res["pairs"]["セカイ"]["zh_hans"], "世界")

    def test_official_language_slots_only(self):
        # glossary 的 title/full 等非语言槽不得泄漏成语言键。
        groups = self._corpus()

        class _G:
            canonical = "スター"
            names = {"title": "スター", "full": "スター", "ja": "スター", "zh_hans": "星"}

        res = penetrate_layered(
            groups, sorted(groups), ["スター"], target_languages=("en",), glossary=[_G()],
        )
        self.assertEqual(set(res["pairs"]["スター"]), {"ja", "zh_hans"})

    def test_target_without_en_skips_channel_c(self):
        groups = self._corpus()
        res = penetrate_layered(
            groups, sorted(groups), ["セカイ"], target_languages=("zh_hans",),
        )
        self.assertIn("C", res["skipped_reason"])
        self.assertEqual(res["channel_stats"]["C"], 0)

    def test_empty_candidates(self):
        groups = self._corpus()
        res = penetrate_layered(groups, sorted(groups), [], target_languages=("en",))
        self.assertEqual(res["pairs"], {})
        self.assertIn("layer0", res["skipped_reason"])


class ChannelStatsSummaryTests(unittest.TestCase):
    def test_empty_result(self):
        self.assertEqual(channel_stats_summary({}), "（无结果）")

    def test_contains_key_lines(self):
        groups = {
            "s1": _story("セカイ\nテスト\n", "SEKAI\nHuh\n"),
            "s2": _story("セカイ\nテスト\n", "SEKAI\nHuh\n"),
        }
        res = penetrate_layered(
            groups, sorted(groups), ["セカイ", "テスト"], target_languages=("en",),
        )
        text = channel_stats_summary(res)
        self.assertIn("第0层", text)
        self.assertIn("第1层", text)
        self.assertIn("セカイ", text)
        self.assertIn("前置过滤拒绝", text)


if __name__ == "__main__":
    unittest.main()
