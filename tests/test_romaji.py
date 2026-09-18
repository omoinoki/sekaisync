"""通道 C 罗马音门控的单元测试（纯标准库，无外部依赖）。

覆盖三类：片假名→罗马音的表层规则（促音/长音/拗音/浊音），
相似度门控的通过/拒绝样例（取自 experiment/zh-en-tw 的实验事实），
以及通用片假名停用判定。
"""

import unittest

from sekaisync.romaji import (
    is_abbrev_form_only,
    is_generic_katakana,
    is_plausible_translation,
    katakana_to_romaji,
    similarity,
)


class KatakanaToRomajiTest(unittest.TestCase):
    def test_basic_names(self):
        self.assertEqual(katakana_to_romaji("セカイ"), "sekai")
        self.assertEqual(katakana_to_romaji("カイト"), "kaito")
        self.assertEqual(katakana_to_romaji("イオリ"), "iori")

    def test_sokuon_and_long_mark(self):
        self.assertEqual(katakana_to_romaji("カット"), "katto")
        self.assertEqual(katakana_to_romaji("ラッキー"), "rakki")
        self.assertEqual(katakana_to_romaji("マッチ"), "matchi")

    def test_yoon(self):
        self.assertTrue(katakana_to_romaji("キャラクター").startswith("ky"))
        self.assertTrue(katakana_to_romaji("ショートケーキ").startswith("sh"))
        self.assertEqual(katakana_to_romaji("シュ"), "shu")
        self.assertEqual(katakana_to_romaji("ニュ"), "nyu")

    def test_dakuten_handakuten_and_vu(self):
        self.assertEqual(katakana_to_romaji("ガ"), "ga")
        self.assertEqual(katakana_to_romaji("パ"), "pa")
        self.assertEqual(katakana_to_romaji("ヴ"), "vu")
        self.assertEqual(katakana_to_romaji("ヴァ"), "va")

    def test_non_katakana_preserved_and_lowercased(self):
        self.assertEqual(katakana_to_romaji("セカイ SEKAI"), "sekai sekai")
        self.assertEqual(katakana_to_romaji("ニーゴ25"), "nigo25")
        self.assertEqual(katakana_to_romaji(""), "")


class SimilarityTest(unittest.TestCase):
    def test_true_penetration_is_one(self):
        self.assertEqual(similarity("セカイ", "SEKAI"), 1.0)
        self.assertEqual(similarity("カイト", "KAITO"), 1.0)
        self.assertEqual(similarity("イオリ", "Iori"), 1.0)
        self.assertEqual(similarity("シブヤ", "Shibuya"), 1.0)

    def test_substring_rule(self):
        self.assertGreaterEqual(similarity("ハル", "Haruka"), 0.7)

    def test_abbreviation_rule_reaches_threshold(self):
        # ニーゴ→N25：纯 ratio 只有 0.29，靠缩写/子串宽松规则补救。
        self.assertGreaterEqual(similarity("ニーゴ", "N25"), 0.5)

    def test_noise_stays_below_threshold(self):
        for kata, en in [("テスト", "Huh"), ("クラス", "Hehe"), ("バタバタ", "Thank")]:
            self.assertLess(similarity(kata, en), 0.5, msg=f"{kata}->{en}")

    def test_empty_inputs(self):
        self.assertEqual(similarity("", "SEKAI"), 0.0)
        self.assertEqual(similarity("セカイ", ""), 0.0)


class PlausibleTranslationTest(unittest.TestCase):
    def test_gate_accepts_real_pairs(self):
        self.assertTrue(is_plausible_translation("セカイ", "SEKAI"))
        self.assertTrue(is_plausible_translation("カイト", "KAITO"))
        self.assertTrue(is_plausible_translation("イオリ", "Iori"))
        self.assertTrue(is_plausible_translation("シブヤ", "Shibuya"))
        self.assertTrue(is_plausible_translation("ニーゴ", "N25"))

    def test_gate_rejects_noise(self):
        self.assertFalse(is_plausible_translation("テスト", "Huh"))
        self.assertFalse(is_plausible_translation("クラス", "Hehe"))

    def test_gate_rejects_bad_shapes(self):
        self.assertFalse(is_plausible_translation("セカイ", "25"))
        self.assertFalse(is_plausible_translation("セカイ", "!"))
        self.assertFalse(is_plausible_translation("セカイ", ""))

    def test_threshold_is_honoured(self):
        # ニーゴ→N25 实测 0.6：0.5 门槛放行，0.9 门槛拒绝。
        self.assertTrue(is_plausible_translation("ニーゴ", "N25", threshold=0.5))
        self.assertFalse(is_plausible_translation("ニーゴ", "N25", threshold=0.9))


class GenericKatakanaTest(unittest.TestCase):
    def test_known_generic_words(self):
        for term in ["テスト", "クラス", "メッセージ", "イメージ", "カット",
                     "アイディア", "バタバタ", "ラッキー", "ワンマン", "ソフト",
                     "ナイス", "ニュース", "ファン", "マネージャー", "バイト",
                     "ボール", "スッキリ", "バラバラ", "バンド", "ステージ",
                     "ライブ", "アタシ", "キッチン", "フォト", "サイン", "メモ",
                     "グループ", "チーム", "メンバー", "パーティー", "イベント",
                     "タイミング", "レベル", "スピード", "コンディション",
                     "テンション", "コース", "パターン"]:
            self.assertTrue(is_generic_katakana(term), msg=term)

    def test_proper_nouns_are_not_generic(self):
        for term in ["ニーゴ", "セカイ", "カイト", "シブヤ"]:
            self.assertFalse(is_generic_katakana(term), msg=term)

    def test_morphology_rule_catches_unlisted_onomatopoeia(self):
        self.assertTrue(is_generic_katakana("ゴロゴロ"))  # 畳語
        self.assertTrue(is_generic_katakana("サッパリ"))  # ッ + リ 结尾

    def test_extend_set(self):
        self.assertFalse(is_generic_katakana("ホゲータ"))
        self.assertTrue(is_generic_katakana("ホゲータ", extend={"ホゲータ"}))

    def test_non_katakana_is_not_generic(self):
        self.assertFalse(is_generic_katakana("世界"))
        self.assertFalse(is_generic_katakana(""))


if __name__ == "__main__":
    unittest.main()


class AbbrevFormOnlyTest(unittest.TestCase):
    """Astra P08/D08 — the abbreviation form rule is eligibility, not identity.

    `ニーゴ→N25` and `ニーゴ→N99` score identically (0.6) under the
    same-initial+digit rule, so that rule cannot distinguish a real alias from
    an adjacent negative. `is_abbrev_form_only` exposes which matches rest on
    it alone, so the pipeline can withhold "confirmed translit" status.
    """

    def test_n25_and_n99_are_both_abbrev_form_only(self):
        from sekaisync.romaji import is_abbrev_form_only, similarity

        self.assertEqual(similarity("ニーゴ", "N25"), similarity("ニーゴ", "N99"))
        self.assertTrue(is_abbrev_form_only("ニーゴ", "N25"))
        self.assertTrue(is_abbrev_form_only("ニーゴ", "N99"))

    def test_real_translits_are_not_abbrev_form_only(self):
        for katakana, english in (("セカイ", "SEKAI"), ("イオリ", "Iori"), ("カイト", "KAITO")):
            with self.subTest(katakana=katakana):
                self.assertFalse(is_abbrev_form_only(katakana, english))

    def test_plausibility_gate_is_unchanged(self):
        """The eligibility gate keeps its documented behaviour."""
        self.assertTrue(is_plausible_translation("ニーゴ", "N25"))
        self.assertFalse(is_plausible_translation("テスト", "Huh"))
