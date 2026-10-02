"""Native JP Han-only UI policy does not relax other script or provenance gates."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets, crawler, dbstore, occurrence_store, termindex, webindex
from sekaisync.wording_identity import _allows_native_ja_han_ui


# Exact 46 JP exclusions from the isolated full raw supply, retained without
# importing runtime work artifacts or certifying any translated counterpart.
NATIVE_JP_HAN_UI = (
    ("MSG_AD_REWARD_DAILY_END", "\u672c\u65e5\u5206\u7d42\u4e86"),
    ("MSG_ALL_READ_REWARD_DESCRIPTION_TITLE", "\u5b8c\u8aad\u5831\u916c\u8a73\u7d30"),
    ("MSG_CREATOR_NAME", "\u4f5c\u8a5e\uff1a{0}\u3000\u4f5c\u66f2\uff1a{1}\u3000\u7de8\u66f2\uff1a{2}"),
    ("MSG_OVER_RARITY_3_ONCE", "\u26053\u4ee5\u4e0a1\u4eba\u78ba\u5b9a!"),
    ("MSG_SHOP_AGE_UNDEFINED", "\u751f\u5e74\u6708\u65e5\u672a\u8a2d\u5b9a"),
    ("MUSIC_ACHIEVEMENTS_LIST", "\u697d\u66f2\u5831\u916c\u4e00\u89a7"),
    ("MUSIC_ACHIEVEMENTS_REWARD", "\u697d\u66f2\u5831\u916c\u8a73\u7d30"),
    ("WORD_AUTO_LIVE_LIMIT", "\u672c\u65e5\u5206\u7d42\u4e86"),
    ("WORD_AUTO_SAVE_PREFERENCE", "\u81ea\u52d5\u4fdd\u5b58\u8a2d\u5b9a"),
    ("WORD_AUTO_SAVE_SETTING", "\u81ea\u52d5\u4fdd\u5b58\u8a2d\u5b9a"),
    ("WORD_BOND_HONOR_SETTING_TITLE", "\u79f0\u53f7\u8868\u793a\u78ba\u8a8d"),
    ("WORD_BOND_HONOR_SETTING_WORD", "\u79f0\u53f7\u540d\u9078\u629e"),
    ("WORD_BORDER_TRANSPARENCY", "\u67a0\u7dda\u4e0d\u900f\u660e\u5ea6"),
    ("WORD_DIFFICULTY_SELECT", "\u96e3\u6613\u5ea6\u9078\u629e"),
    ("WORD_DIFFICULTY_UNLOCK", "\u96e3\u6613\u5ea6\u89e3\u653e"),
    ("WORD_DISPLAY_DIFFICULTY", "\u8868\u793a\u96e3\u6613\u5ea6"),
    ("WORD_ELEMENT_COUNT", "\u7d20\u6750\u6240\u6301\u6570"),
    ("WORD_ENTRY_EVALUATION_PERIOD", "\u5bfe\u8c61\u5224\u5b9a\u671f\u9593"),
    ("WORD_ESTIMATED_LEVEL", "\u63a8\u5b9a\u96e3\u6613\u5ea6"),
    ("WORD_FIRST_REWARD", "\u521d\u56de\u53c2\u52a0\u5831\u916c"),
    ("WORD_FIXED_BONUS_PROVISION_RATIO", "\u26054\u78ba\u5b9a\u63d0\u4f9b\u5272\u5408"),
    ("WORD_FIXED_PROVISION_RATIO", "\u78ba\u5b9a\u67a0\u63d0\u4f9b\u5272\u5408"),
    ("WORD_FRAME_LINE_OPACITY", "\u67a0\u7dda\u4e0d\u900f\u660e\u5ea6"),
    ("WORD_HONOR_NO_SETTING", "\u79f0\u53f7\u672a\u8a2d\u5b9a"),
    ("WORD_INDEFINITE_PERIOD", "\u505c\u6b62\u671f\u9593\u3000\u7121\u671f\u9650"),
    ("WORD_IS_HOLDING_EXTRA_CHAPTER", "\u88dc\u586b\u958b\u50ac\u4e2d"),
    ("WORD_LIMITED_TIME_RELEASE", "\u671f\u9593\u9650\u5b9a\u89e3\u653e\u4e2d"),
    ("WORD_MAX_WIN_STREAK", "\u6700\u5927\u9023\u52dd\u6570"),
    ("WORD_MSM_DERIVATIVE_NOT_ALLOWED", "\u4e8c\u6b21\u5229\u7528\u4e0d\u53ef"),
    ("WORD_MYSEKAI_BIRTHDAY_PARTY_CRAFT_TERM", "\u9650\u5b9a\u5bb6\u5177\u88fd\u4f5c"),
    ("WORD_MYSEKAI_WEEKLY_UPDATE", "\u6bce\u9031\u6708\u66dc\u65e5\u66f4\u65b0"),
    ("WORD_PERFORMER_INFO", "\u51fa\u6f14\u8005\u60c5\u5831"),
    ("WORD_PURCHASE_LIMIT_BY_AGE", "\u5e74\u9f62\u5225\u8cfc\u5165\u9650\u5ea6\u984d"),
    ("WORD_REQUIRED_CONTINUOUS_PERIOD", "\u5fc5\u8981\u7d99\u7d9a\u671f\u9593"),
    ("WORD_SHAPE_OPACITY", "\u56f3\u5f62\u4e0d\u900f\u660e\u5ea6"),
    ("WORD_SHAPE_TRANSPARENCY", "\u56f3\u5f62\u4e0d\u900f\u660e\u5ea6"),
    ("WORD_STREAMING_LIVE_RE_ENTER_TITLE", "\u518d\u5165\u5834\u78ba\u8a8d"),
    ("WORD_TEMP_LIMIT", "\u4e00\u6642\u5236\u9650\u4e2d"),
    ("WORD_TITLE_COSTUME_CHANGE_TITLE", "\u8863\u88c5\u8a2d\u5b9a\u5b8c\u4e86"),
    ("WORD_TODAY_PART_END", "\u672c\u65e5\u5206\u7d42\u4e86"),
    ("WORD_TOTAL_POSSESSION", "\u5408\u8a08\u6240\u6301\u6570"),
    ("WORD_TOTAL_POWER_DETAIL", "\u7dcf\u5408\u529b\u8a73\u7d30"),
    ("WORD_TOURNAMENT_MUSIC", "\u5927\u4f1a\u5bfe\u8c61\n\u697d\u66f2"),
    ("WORD_UNIT_PRICE_TIME", "\u8cfc\u5165\u6642\u5358\u4fa1"),
    ("WORD_VIRTUAL_LIVE_CAST_MAIN_PERFORMANCE", "\u672c\u516c\u6f14\u51fa\u6f14"),
    ("WORD_VIRTUAL_SHOP_FIRST_BONUS", "<color=#FF5588>\uff08\u521d\u56de\u8cfc\u5165\u7279\u5178\uff09</color>"),
)


class WordingJaScriptPolicyTests(unittest.TestCase):
    @staticmethod
    def page(value, region="jp", key="NATIVE_JP_HAN_TEST"):
        page = crawler.altsource_sv_record_page(dict(wordingKey=key, value=value), "wordings", region)
        return webindex.web_page_to_dict(page)

    def test_all_46_native_jp_han_ui_bodies_are_usable(self):
        self.assertEqual(len(NATIVE_JP_HAN_UI), 46)
        for key, value in NATIVE_JP_HAN_UI:
            with self.subTest(key=key):
                page = self.page(value, key=key)
                self.assertFalse(webindex.text_matches_language("ja", value))
                self.assertTrue(_allows_native_ja_han_ui(page, "ja"))
                self.assertTrue(termindex._page_usable(page, "ja"))
                self.assertEqual(page["text"], value)

    def test_native_jp_whole_windows_publish_no_semantic_relations(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory)
            dbstore.initialize_new_store(store, target_version=3)
            pages = [crawler.altsource_sv_record_page(dict(wordingKey=key, value=value), "wordings", "jp")
                     for key, value in NATIVE_JP_HAN_UI]
            webindex.save_web_pages(store, pages[0].source, pages, write_categories=False, rewrite_index=False)
            groups = termindex.group_pages_by_story(termindex.load_pages(store))
            self.assertEqual(len(groups), 46)
            items, _ = agent_packets._prepare_scrub_review(store, groups, sorted(groups), [], {}, "ja", ["en"])
            self.assertEqual(len(items), 46)
            seen = set()
            for item in items:
                row, = item._context["rows"]
                view = row["source"]
                self.assertEqual((view["start"], view["end"]), (0, len(view["text"])))
                self.assertTrue(view["complete"])
                self.assertIn("wording_body", view)
                seen.add(view["text"])
            self.assertEqual(seen, {value for _, value in NATIVE_JP_HAN_UI})
            with dbstore.connect(store) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                self.assertEqual(occurrence_store._read_relations(conn, current_only=False), [])

    def test_missing_or_partial_metadata_cannot_donate_native_jp_exception(self):
        page = self.page(NATIVE_JP_HAN_UI[0][1])
        for field in ("both", "wording_identity", "wording_provenance"):
            with self.subTest(field=field):
                altered = deepcopy(page)
                for name in ("wording_identity", "wording_provenance") if field == "both" else (field,):
                    altered.pop(name)
                self.assertFalse(termindex._page_usable(altered, "ja"))

    def test_tampered_identity_and_full_record_provenance_fail_closed(self):
        page = self.page(NATIVE_JP_HAN_UI[0][1])
        mutations = (
            lambda p: p["wording_identity"].update(region="tc"),
            lambda p: p["wording_identity"].update(region=[]),
            lambda p: p["wording_identity"].update(region={"claimed": "jp"}),
            lambda p: p["wording_identity"].update(language="en"),
            lambda p: p["wording_identity"].update(body_policy="invented-policy"),
            lambda p: p["wording_identity"].update(source="other-source"),
            lambda p: p["wording_provenance"]["original_record"].update(wordingKey="DONATED_KEY"),
            lambda p: p["wording_provenance"]["original_record"].update(value="\u672c\u65e5\u5206\u7d42\u4e86!"),
            lambda p: p["wording_provenance"].update(adapter_value_token='"unrelated"'),
            lambda p: p["wording_provenance"].update(decoded_unicode_to_token_unicode=[]),
            lambda p: p.update(source_hash="0" * 64),
            lambda p: p.update(text_hash="0" * 64),
            lambda p: p.update(id=p["id"].replace(":jp:", ":tc:")),
            lambda p: p.update(text=p["text"] + "!"),
        )
        for number, mutate in enumerate(mutations):
            with self.subTest(number=number):
                altered = deepcopy(page)
                mutate(altered)
                self.assertFalse(termindex._page_usable(altered, "ja"))

    def test_mismatch_and_untranslated_flags_remain_authoritative(self):
        page = self.page(NATIVE_JP_HAN_UI[0][1])
        for flag in ("asset_mismatch", "content_language_mismatch", "untranslated"):
            with self.subTest(flag=flag):
                altered = deepcopy(page)
                altered[flag] = True
                self.assertFalse(termindex._page_usable(altered, "ja"))

    def test_foreign_hangul_and_jamo_never_receive_han_ui_exception(self):
        base = NATIVE_JP_HAN_UI[0][1]
        for suffix in ("\uac00" * 8, "\u1100" * 8, "\u3131" * 8, "\ua960" * 8):
            with self.subTest(suffix=ascii(suffix)):
                self.assertFalse(_allows_native_ja_han_ui(self.page(base + suffix), "ja"))
        self.assertFalse(termindex._page_usable(self.page("\uac00" * 8), "ja"))
        self.assertFalse(termindex._page_usable(self.page(base + "\uac00" * 8), "ja"))

    def test_foreign_cyrillic_arabic_and_greek_letters_do_not_donate_exception(self):
        base = NATIVE_JP_HAN_UI[0][1]
        for suffix in ("\u041f\u0440\u0438\u0432\u0435\u0442", "\u0627\u0644\u0639\u0631\u0628\u064a\u0629", "\u03b1\u03b2\u03b3"):
            with self.subTest(suffix=ascii(suffix)):
                page = self.page(base + suffix)
                self.assertFalse(webindex.text_matches_language("ja", page["text"]))
                self.assertFalse(_allows_native_ja_han_ui(page, "ja"))
                self.assertFalse(termindex._page_usable(page, "ja"))

    def test_native_jp_latin_and_common_ui_characters_remain_exact(self):
        base = NATIVE_JP_HAN_UI[0][1]
        # Availability of a native regional body is not a localization/release
        # claim; Japanese UI may legitimately include Latin labels and markup.
        for value in ("UI: " + base + " {0}", "\uff35\uff29: " + base, "caf\u00e9 " + base,
                      NATIVE_JP_HAN_UI[-1][1]):
            with self.subTest(value=ascii(value)):
                page = self.page(value)
                self.assertTrue(termindex._page_usable(page, "ja"))
                self.assertEqual(page["text"], value)

    def test_overseas_script_guards_are_identical_for_all_46_bodies(self):
        for region, language in (("en", "en"), ("kr", "ko"), ("tc", "zh_hant"), ("cn", "zh_hans")):
            for key, value in NATIVE_JP_HAN_UI:
                with self.subTest(region=region, key=key):
                    page = self.page(value, region=region, key=key)
                    self.assertFalse(_allows_native_ja_han_ui(page, language))
                    self.assertEqual(termindex._page_usable(page, language),
                                     webindex.text_matches_language(termindex._term_language(language), value))

    def test_overseas_residual_japanese_remains_excluded(self):
        value = "\u30a2\u30ab\u30a6\u30f3\u30c8\u30dc\u30fc\u30ca\u30b9\u52b9\u679c"
        for region, language in (("en", "en"), ("kr", "ko"), ("tc", "zh_hant"), ("cn", "zh_hans")):
            with self.subTest(region=region):
                page = self.page(value, region=region)
                self.assertFalse(termindex._page_usable(page, language))
                self.assertFalse(_allows_native_ja_han_ui(page, "ja"))

    def test_ordinary_dialogue_does_not_receive_wording_override(self):
        page = self.page(NATIVE_JP_HAN_UI[0][1])
        for kind in ("event_story", "unit_story", "page"):
            with self.subTest(kind=kind):
                ordinary = dict(page, kind=kind)
                self.assertFalse(termindex._page_usable(ordinary, "ja"))
                for name in ("wording_identity", "wording_provenance"):
                    ordinary.pop(name)
                self.assertFalse(termindex._page_usable(ordinary, "ja"))

    def test_declared_and_requested_languages_cannot_disagree(self):
        page = self.page(NATIVE_JP_HAN_UI[0][1])
        self.assertFalse(_allows_native_ja_han_ui(page, "en"))
        self.assertFalse(termindex._page_usable(page, "en"))
        altered = dict(page, language="en")
        self.assertFalse(_allows_native_ja_han_ui(altered, "ja"))
        self.assertFalse(termindex._page_usable(altered, "ja"))

    def test_blank_and_missing_values_remain_no_expression(self):
        for value in (None, "", " \n "):
            with self.subTest(value=value):
                page = self.page(value)
                self.assertFalse(termindex._page_usable(page, "ja"))
                self.assertFalse(_allows_native_ja_han_ui(page, "ja"))


if __name__ == "__main__":
    unittest.main()
