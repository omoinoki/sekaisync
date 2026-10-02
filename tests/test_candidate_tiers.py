"""候选分层判定（第 0 层）的单元测试。

覆盖三层验收：验收清单 9 项、API 契约（归一化 / 批量 / 过滤 / 汇总）、
以及 ``romaji.py`` 缺席与存在两条降级路径。
"""

from __future__ import annotations

import importlib
import subprocess
import sys
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock

from sekaisync import candidate_tiers as ct
from sekaisync.candidate_tiers import (
    Tier,
    classify,
    classify_batch,
    filter_alignable,
    tier_summary,
)

# 仓库里 romaji.py 可能已存在（重构的后续模块），也可能尚未落地。
try:  # pragma: no cover - 取决于工作区状态
    import sekaisync.romaji as _romaji  # type: ignore
except Exception:  # pragma: no cover
    _romaji = None

ROMAJI_AVAILABLE = _romaji is not None


class AcceptanceTest(unittest.TestCase):
    """验收要求逐条固化，回归时任何一条失守都会直接报出来。"""

    def test_01_official_hit(self):
        self.assertIs(classify("星乃一歌", official_keys={"星乃一歌"}), Tier.OFFICIAL)

    def test_02_quoted(self):
        self.assertIs(classify("ネットパラダイス", quoted=True), Tier.PROPER)

    def test_03_mixed_latin(self):
        self.assertIs(classify("LUMINA时间"), Tier.PROPER)
        self.assertIs(classify("Cheerful＊Days"), Tier.PROPER)

    def test_04_katakana_proper(self):
        self.assertIs(classify("セカイ"), Tier.PROPER)
        self.assertIs(classify("ニーゴ"), Tier.PROPER)

    def test_05_generic_katakana_rejected(self):
        self.assertIs(classify("テスト"), Tier.REJECT)
        self.assertIs(classify("クラス"), Tier.REJECT)

    def test_06_zh_generic_rejected(self):
        self.assertIs(classify("大家", language="zh_hans"), Tier.REJECT)
        self.assertIs(classify("咖啡", language="zh_hans"), Tier.REJECT)

    def test_07_zh_proper_suffix(self):
        self.assertIs(classify("森之宫歌剧团", language="zh_hans"), Tier.PROPER)

    def test_08_ja_hiragana_fragment_rejected(self):
        self.assertIs(classify("そうだった", language="ja"), Tier.REJECT)

    def test_09_filter_alignable(self):
        self.assertEqual(
            filter_alignable(["セカイ", "テスト", "大家", "森之宫歌剧团"]),
            ["セカイ", "森之宫歌剧团"],
        )


class NormalizationTest(unittest.TestCase):
    def test_official_matches_across_punctuation_and_width(self):
        # 全角/半角、标点、大小写都不该影响命中。
        self.assertIs(classify("星乃一歌！", official_keys={"星乃一歌"}), Tier.OFFICIAL)
        self.assertIs(classify("ＬＵＭＩＮＡ", official_keys={"lumina"}), Tier.OFFICIAL)

    def test_seed_is_proper(self):
        self.assertIs(classify("SEKAI", seed_keys={"せかい", "SEKAI"}), Tier.PROPER)

    def test_empty_and_symbol_only_are_rejected(self):
        for term in ("", "   ", "……！", "！？"):
            self.assertIs(classify(term), Tier.REJECT, term)


class LanguageAdaptationTest(unittest.TestCase):
    def test_zh_two_char_kanji_rejected(self):
        # 无官方/种子背书、无专名后缀的 2 字汉字 → 通用词嫌疑，拒绝。
        for term in ("商量", "休息", "期待"):
            self.assertIs(classify(term, language="zh_hans"), Tier.REJECT, term)

    def test_zh_long_kanji_without_suffix_rejected(self):
        self.assertIs(classify("面不改色", language="zh_hans"), Tier.REJECT)

    def test_zh_suffix_applies_regardless_of_language_param(self):
        # 后缀是脚本特征：ja 语料里的「神山高校」同样是专名。
        self.assertIs(classify("神山高校", language="ja"), Tier.PROPER)
        self.assertIs(classify("森之宫音乐学园", language="ja"), Tier.PROPER)

    def test_ko_short_hangul_rejected(self):
        self.assertIs(classify("그거", language="ko"), Tier.REJECT)

    def test_ja_hiragana_short_rejected(self):
        for term in ("の", "して", "そうだ"):
            self.assertIs(classify(term, language="ja"), Tier.REJECT, term)

    def test_generic_zh_blocked_in_any_language(self):
        # 「大家」混进 ja 语料里一样是噪声。
        self.assertIs(classify("大家", language="ja"), Tier.REJECT)


class EnglishFormTest(unittest.TestCase):
    def test_title_case_multiword_is_proper(self):
        self.assertIs(classify("Lasting ECHO Fes"), Tier.PROPER)

    def test_lowercase_single_word_is_not_proper(self):
        self.assertIs(classify("test"), Tier.REJECT)

    def test_function_words_do_not_confer_properness(self):
        # "Thank you" 是短语不是专名 —— 实验里的行位噪声正品。
        self.assertIs(classify("Thank you"), Tier.REJECT)

    def test_all_caps_single_token_is_proper(self):
        self.assertIs(classify("SEKAI"), Tier.PROPER)


class DiscoveredTest(unittest.TestCase):
    def test_discovered_is_statistical(self):
        # 「神山祭」无专名后缀、非 2 字通用词 → 只能靠统计发现背书，落到 L2。
        self.assertIs(classify("神山祭", discovered={"神山祭"}), Tier.STATISTICAL)

    def test_morphology_outranks_discovered(self):
        # 形态专名（专名后缀）优先级高于统计发现，不该被降级成 L2。
        self.assertIs(classify("神山高校", discovered={"神山高校"}), Tier.PROPER)

    def test_discovered_does_not_rescue_function_words(self):
        # A repeated pronoun is noise; an ordinary content word still has a
        # translation and may enter alignment with explicit lexical evidence.
        self.assertIs(classify("大家", language="zh_hans", discovered={"大家"}), Tier.REJECT)
        self.assertIs(classify("テスト", discovered={"テスト"}), Tier.STATISTICAL)

    def test_common_content_words_with_evidence_are_not_rejected_as_grammar(self):
        for language, terms in (("zh_hans", ("咖啡", "红茶", "音乐", "学校")),
                                ("ja", ("ギター", "ライブ", "ひかり")),
                                ("ko", ("음악", "학교")), ("en", ("coffee", "music"))):
            for term in terms:
                with self.subTest(language=language, term=term):
                    self.assertIs(classify(term, language=language, discovered={term}), Tier.STATISTICAL)

    def test_repeated_short_grammar_is_still_rejected(self):
        for language, term in (("zh_hans", "我们"), ("ja", "そうだった"),
                                ("ko", "그거"), ("en", "Thank")):
            self.assertIs(classify(term, language=language, discovered={term}), Tier.REJECT)

    def test_title_connector_is_internal_and_suffix_is_an_ending(self):
        self.assertIs(classify("Star of Dawn", language="en"), Tier.PROPER)
        self.assertIs(classify("The world is here", language="en"), Tier.REJECT)
        self.assertIs(classify("月虹公园里的", language="zh_hans"), Tier.REJECT)

    def test_official_beats_everything(self):
        self.assertIs(
            classify("大家", official_keys={"大家"}, discovered={"大家"}),
            Tier.OFFICIAL,
        )

    def test_quoted_beats_discovered(self):
        self.assertIs(
            classify("セカイ", quoted=True, discovered={"セカイ"}),
            Tier.PROPER,
        )


class BatchApiTest(unittest.TestCase):
    def test_classify_batch_returns_tier_map(self):
        result = classify_batch(["セカイ", "テスト", "大家", "森之宫歌剧团"])
        self.assertEqual(
            result,
            {
                "セカイ": Tier.PROPER,
                "テスト": Tier.REJECT,
                "大家": Tier.REJECT,
                "森之宫歌剧团": Tier.PROPER,
            },
        )

    def test_classify_batch_forwards_kwargs(self):
        result = classify_batch(["星乃一歌"], official_keys={"星乃一歌"})
        self.assertEqual(result["星乃一歌"], Tier.OFFICIAL)

    def test_classify_batch_handles_empty(self):
        self.assertEqual(classify_batch([]), {})

    def test_filter_preserves_order_and_dedupes(self):
        self.assertEqual(filter_alignable(["セカイ", "セカイ", "テスト"]), ["セカイ"])

    def test_filter_returns_surface_not_normalized_key(self):
        # 下游对齐要的是原文，不能拿归一化键去替换。
        self.assertEqual(filter_alignable(["SEKAI"]), ["SEKAI"])

    def test_filter_handles_empty(self):
        self.assertEqual(filter_alignable([]), [])

    def test_tier_summary(self):
        tiers = {
            "a": Tier.OFFICIAL,
            "b": Tier.PROPER,
            "c": Tier.PROPER,
            "d": Tier.STATISTICAL,
            "e": Tier.REJECT,
        }
        self.assertEqual(tier_summary(tiers), {"L0": 1, "L1": 2, "L2": 1, "L3": 1})

    def test_tier_summary_has_all_four_tiers_when_empty(self):
        self.assertEqual(tier_summary({}), {"L0": 0, "L1": 0, "L2": 0, "L3": 0})

    def test_tier_values_are_strings(self):
        # Tier 继承 str，便于直接序列化进 JSON 诊断输出。
        self.assertEqual([t.value for t in Tier], ["L0", "L1", "L2", "L3"])
        self.assertEqual(Tier.PROPER.value, "L1")


class RomajiFallbackTest(unittest.TestCase):
    """``romaji.py`` 缺席时必须能工作；存在时其判定要被采纳。

    两类判别词（用于区分"到底走了哪张表"）::

        _BUILTIN_ONLY   只在候选分层的内置停用表里 → 证明内置表被查询
        _EXTERNAL_ONLY  只在 romaji.py 的停用表里 → 证明外部判定被采纳

    注意：这里用 ``importlib.reload`` 模拟 "romaji 出现/消失"，reload 会重建
    ``Tier`` 类对象，所以断言一律比 ``.value``（跨类对象的 ``assertIs`` 会假失败）。
    强制屏蔽用 ``sys.modules[...] = None``（导入系统会因此抛 ImportError），
    这样无论仓库里 romaji.py 是否已落地，测试结果都一致。
    """

    _FAKE = "sekaisync.romaji"
    _BUILTIN_ONLY = "カード"     # 内置表命中，romaji 的形态规则不命中
    _EXTERNAL_ONLY = "キッチン"  # romaji 表命中，内置表没有

    @classmethod
    def setUpClass(cls):
        # 断言判别词在当前环境下确实有区分度，否则测试会失去意义。
        assert cls._BUILTIN_ONLY in ct._BUILTIN_GENERIC_KATAKANA

    def _reload(self):
        return importlib.reload(ct)

    @staticmethod
    def _tier(module, term, **kwargs):
        return module.classify(term, **kwargs).value

    def test_external_only_word_sanity(self):
        # 若 romaji.py 存在，判别词必须真的只在外表命中；否则该用例自动跳过。
        if not ROMAJI_AVAILABLE:
            self.skipTest("romaji.py 尚未落地，外部判别词无意义")
        self.assertNotIn(self._EXTERNAL_ONLY, ct._BUILTIN_GENERIC_KATAKANA)
        self.assertTrue(_romaji.is_generic_katakana(self._EXTERNAL_ONLY))

    def test_works_when_romaji_is_absent(self):
        # 强制让 `from sekaisync.romaji import ...` 失败 → 走内置降级表。
        with mock.patch.dict(sys.modules, {self._FAKE: None}):
            module = self._reload()
            self.assertIsNone(module._EXTERNAL_GENERIC_CHECK.get("fn"))
            # 内置表命中 → 通用词拒绝。
            self.assertEqual(self._tier(module, self._BUILTIN_ONLY), "L3")
            # 内置表没有的词按形态判为专名。
            self.assertEqual(self._tier(module, self._EXTERNAL_ONLY), "L1")
            # 验收清单里的核心样例在降级路径下同样成立。
            self.assertEqual(self._tier(module, "テスト"), "L3")
            self.assertEqual(self._tier(module, "セカイ"), "L1")
            self.assertEqual(self._tier(module, "ニーゴ"), "L1")
        self._reload()  # 恢复环境真实状态

    def test_uses_external_generic_check_when_available(self):
        fake = types.ModuleType(self._FAKE)
        fake.is_generic_katakana = lambda term: term == self._EXTERNAL_ONLY
        with mock.patch.dict(sys.modules, {self._FAKE: fake}):
            module = self._reload()
            self.assertTrue(callable(module._EXTERNAL_GENERIC_CHECK.get("fn")))
            # 外部判定说它是通用词 → 必须变成 REJECT。
            self.assertEqual(self._tier(module, self._EXTERNAL_ONLY), "L3")
            # 内置表仍然生效（两条路径是并集）。
            self.assertEqual(self._tier(module, self._BUILTIN_ONLY), "L3")
            self.assertEqual(self._tier(module, "セカイ"), "L1")
        self._reload()

    def test_survives_broken_external_check(self):
        fake = types.ModuleType(self._FAKE)

        def _boom(term):
            raise RuntimeError("romaji backend exploded")

        fake.is_generic_katakana = _boom
        with mock.patch.dict(sys.modules, {self._FAKE: fake}):
            module = self._reload()
            # 外部判定抛错不应让分层整体崩掉，退回内置表。
            self.assertEqual(self._tier(module, self._BUILTIN_ONLY), "L3")
            self.assertEqual(self._tier(module, self._EXTERNAL_ONLY), "L1")
        self._reload()

    def test_romaji_absent_does_not_break_import_chain(self):
        # 在**全新解释器**里（romaji 不可导入）验证导入链干净：
        # 本模块不得把 termindex / wordseg / zhfirst 之类的重模块拖进来。
        # 必须用子进程 —— 同进程里其它测试已经 import 过它们，sys.modules 不干净。
        probe = textwrap.dedent(
            """
            import sys
            sys.modules["sekaisync.romaji"] = None  # 模拟 romaji 尚未落地
            from sekaisync.candidate_tiers import classify
            heavy = [m for m in ("sekaisync.termindex", "sekaisync.wordseg",
                                 "sekaisync.zhfirst") if m in sys.modules]
            print("HEAVY=" + ",".join(heavy))
            print("KATA=" + classify("セカイ").value)
            print("TEST=" + classify("テスト").value)
            """
        )
        proc = subprocess.run(
            [sys.executable, "-X", "utf8", "-c", probe],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("HEAVY=", proc.stdout)
        self.assertIn("HEAVY=\n", proc.stdout.replace("\r", ""), proc.stdout)
        # 降级路径下核心判定照常工作。
        self.assertIn("KATA=L1", proc.stdout)
        self.assertIn("TEST=L3", proc.stdout)


if __name__ == "__main__":
    unittest.main()
