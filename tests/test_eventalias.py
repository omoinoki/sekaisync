from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.cli import main as cli_main
from sekaisync.eventalias import (
    _build_jp_box_map,
    build_event_alias_map,
    numeral_text_to_int,
    parse_query,
    resolve_activity,
    resolve_event_alias,
)

# A box event in the community sense is determined by its first rarity_4 card:
# the event is attributed to that character, even when the same band or a
# virtual singer also has rarity_4 cards in the same gacha.
JP_BOX_EVENTS = [
    {"id": 1, "eventType": "marathon", "name": "雨上がりの一番星", "startAt": 1000},
    {"id": 2, "eventType": "marathon", "name": "囚われのマリオネット", "startAt": 2000},
    {"id": 3, "eventType": "marathon", "name": "全力！ワンダーハロウィン！", "startAt": 3000},
    {"id": 4, "eventType": "cheerful_carnival", "name": "混活应排除", "startAt": 4000},
    {"id": 5, "eventType": "world_bloom", "name": "世界花应排除", "startAt": 5000},
    {"id": 6, "eventType": "marathon", "name": "Bout for Beside You", "startAt": 6000},
    {"id": 7, "eventType": "marathon", "name": "No seek No find", "startAt": 7000},
    {"id": 8, "eventType": "marathon", "name": "Kick it up a notch", "startAt": 8000},
    {"id": 9, "eventType": "marathon", "name": "On Your Feet", "startAt": 9000},
    {"id": 10, "eventType": "marathon", "name": "OVER RAD SQUAD!!", "startAt": 10000},
]

JP_CARDS = [
    {"id": 101, "characterId": 2, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP咲希1"},
    {"id": 102, "characterId": 3, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP穗波1"},
    {"id": 103, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP杏1"},
    {"id": 104, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽1"},
    {"id": 105, "characterId": 16, "cardRarityType": "rarity_4", "supportUnit": "theme_park", "prefix": "JP类1"},
    {"id": 106, "characterId": 6, "cardRarityType": "rarity_4", "supportUnit": "idol", "prefix": "JP遥1"},
    {"id": 107, "characterId": 14, "cardRarityType": "rarity_4", "supportUnit": "theme_park", "prefix": "JP笑梦1"},
    {"id": 108, "characterId": 19, "cardRarityType": "rarity_4", "supportUnit": "school_refusal", "prefix": "JP绘名1"},
    {"id": 109, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP杏2"},
    {"id": 110, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽2"},
    {"id": 111, "characterId": 3, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP穗波2"},
    {"id": 112, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽3"},
    {"id": 113, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP杏3"},
    {"id": 114, "characterId": 22, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "JP铃3"},
    {"id": 115, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽4"},
    {"id": 116, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP杏4"},
    {"id": 117, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽5"},
    {"id": 118, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽6"},
    {"id": 119, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP杏5"},
    {"id": 120, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽7"},
    {"id": 121, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP心羽8"},
    {"id": 122, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "JP杏6"},
]

JP_EVENT_CARDS = [
    {"id": 1, "eventId": 1, "cardId": 101},
    {"id": 2, "eventId": 1, "cardId": 102},
    {"id": 3, "eventId": 2, "cardId": 103},
    {"id": 4, "eventId": 2, "cardId": 104},
    {"id": 5, "eventId": 3, "cardId": 105},
    {"id": 6, "eventId": 4, "cardId": 106},
    {"id": 7, "eventId": 4, "cardId": 107},
    {"id": 8, "eventId": 5, "cardId": 108},
    {"id": 9, "eventId": 6, "cardId": 109},
    {"id": 10, "eventId": 6, "cardId": 110},
    {"id": 11, "eventId": 7, "cardId": 111},
    {"id": 12, "eventId": 8, "cardId": 112},
    {"id": 13, "eventId": 8, "cardId": 113},
    {"id": 14, "eventId": 8, "cardId": 114},
    {"id": 15, "eventId": 9, "cardId": 115},
    {"id": 16, "eventId": 9, "cardId": 116},
    {"id": 17, "eventId": 9, "cardId": 117},
    {"id": 18, "eventId": 10, "cardId": 118},
    {"id": 19, "eventId": 10, "cardId": 119},
    {"id": 20, "eventId": 10, "cardId": 120},
    {"id": 21, "eventId": 10, "cardId": 121},
    {"id": 22, "eventId": 10, "cardId": 122},
]

JP_EVENT_MUSICS = [
    {"eventId": 1, "musicId": 11, "seq": 1},
    {"eventId": 2, "musicId": 12, "seq": 1},
    {"eventId": 4, "musicId": 15, "seq": 1},
    {"eventId": 6, "musicId": 13, "seq": 1},
    {"eventId": 8, "musicId": 14, "seq": 1},
    {"eventId": 9, "musicId": 16, "seq": 1},
    {"eventId": 10, "musicId": 17, "seq": 1},
]

JP_MUSICS = [
    {"id": 11, "title": "needLe"},
    {"id": 12, "title": "悔やむと書いてミライ"},
    {"id": 13, "title": "Awake Now"},
    {"id": 14, "title": "ひつじがいっぴき"},
    {"id": 15, "title": "チームメイト"},
    {"id": 16, "title": "リアライズ"},
    {"id": 17, "title": "ULTRA C"},
]

JP_GAME_CHARACTER_UNITS = [
    {"id": 1, "gameCharacterId": 1, "unit": "light_sound"},
    {"id": 2, "gameCharacterId": 2, "unit": "light_sound"},
    {"id": 3, "gameCharacterId": 3, "unit": "light_sound"},
    {"id": 4, "gameCharacterId": 9, "unit": "street"},
    {"id": 5, "gameCharacterId": 10, "unit": "street"},
    {"id": 6, "gameCharacterId": 16, "unit": "theme_park"},
    {"id": 7, "gameCharacterId": 6, "unit": "idol"},
    {"id": 8, "gameCharacterId": 14, "unit": "theme_park"},
    {"id": 9, "gameCharacterId": 19, "unit": "school_refusal"},
    {"id": 10, "gameCharacterId": 22, "unit": "piapro"},
]

CN_EVENTS = [
    {"id": 1, "eventType": "marathon", "name": "雨过天晴的启明星", "startAt": 20000},
    {"id": 2, "eventType": "marathon", "name": "被囚禁的木偶", "startAt": 21000},
    {"id": 8, "eventType": "marathon", "name": "Kick it up a notch", "startAt": 22000},
    {"id": 9, "eventType": "marathon", "name": "On Your Feet", "startAt": 23000},
    {"id": 10, "eventType": "marathon", "name": "OVER RAD SQUAD!!", "startAt": 24000},
]
CN_CARDS = [
    {"id": 101, "characterId": 2, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN咲希1"},
    {"id": 102, "characterId": 3, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN穗波1"},
    {"id": 103, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN杏1"},
    {"id": 104, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽1"},
    {"id": 105, "characterId": 16, "cardRarityType": "rarity_4", "supportUnit": "theme_park", "prefix": "CN类1"},
    {"id": 106, "characterId": 6, "cardRarityType": "rarity_4", "supportUnit": "idol", "prefix": "CN遥1"},
    {"id": 107, "characterId": 14, "cardRarityType": "rarity_4", "supportUnit": "theme_park", "prefix": "CN笑梦1"},
    {"id": 108, "characterId": 19, "cardRarityType": "rarity_4", "supportUnit": "school_refusal", "prefix": "CN绘名1"},
    {"id": 109, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN杏2"},
    {"id": 110, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽2"},
    {"id": 111, "characterId": 3, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN穗波2"},
    {"id": 112, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽3"},
    {"id": 113, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN杏3"},
    {"id": 114, "characterId": 22, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "CN铃3"},
    {"id": 115, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽4"},
    {"id": 116, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN杏4"},
    {"id": 117, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽5"},
    {"id": 118, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽6"},
    {"id": 119, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN杏5"},
    {"id": 120, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽7"},
    {"id": 121, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN心羽8"},
    {"id": 122, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "CN杏6"},
]
CN_EVENT_CARDS = [
    {"id": 1, "eventId": 1, "cardId": 101},
    {"id": 2, "eventId": 1, "cardId": 102},
    {"id": 3, "eventId": 2, "cardId": 103},
    {"id": 4, "eventId": 2, "cardId": 104},
    {"id": 9, "eventId": 6, "cardId": 109},
    {"id": 10, "eventId": 6, "cardId": 110},
    {"id": 12, "eventId": 8, "cardId": 112},
    {"id": 13, "eventId": 8, "cardId": 113},
    {"id": 14, "eventId": 8, "cardId": 114},
    {"id": 15, "eventId": 9, "cardId": 115},
    {"id": 16, "eventId": 9, "cardId": 116},
    {"id": 17, "eventId": 9, "cardId": 117},
    {"id": 18, "eventId": 10, "cardId": 118},
    {"id": 19, "eventId": 10, "cardId": 119},
    {"id": 20, "eventId": 10, "cardId": 120},
    {"id": 21, "eventId": 10, "cardId": 121},
    {"id": 22, "eventId": 10, "cardId": 122},
]
CN_EVENT_MUSICS = [
    {"eventId": 1, "musicId": 11, "seq": 1},
    {"eventId": 2, "musicId": 12, "seq": 1},
    {"eventId": 6, "musicId": 13, "seq": 1},
    {"eventId": 8, "musicId": 14, "seq": 1},
    {"eventId": 9, "musicId": 16, "seq": 1},
    {"eventId": 10, "musicId": 17, "seq": 1},
]
CN_MUSICS = [
    {"id": 11, "title": "needLe"},
    {"id": 12, "title": "悔恨写为未来"},
    {"id": 13, "title": "Awake Now"},
    {"id": 14, "title": "一只羊"},
    {"id": 16, "title": "Realize"},
    {"id": 17, "title": "ULTRA C"},
]
CN_GAME_CHARACTER_UNITS = JP_GAME_CHARACTER_UNITS


def _write_json(directory: Path, name: str, data) -> None:
    (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_store(root: Path) -> None:
    jp = root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
    cn = root / "raw" / "cn" / "source" / "sekai-master-db-cn-diff-main"
    jp.mkdir(parents=True, exist_ok=True)
    cn.mkdir(parents=True, exist_ok=True)
    _write_json(jp, "events.json", JP_BOX_EVENTS)
    _write_json(jp, "eventCards.json", JP_EVENT_CARDS)
    _write_json(jp, "cards.json", JP_CARDS)
    _write_json(jp, "eventMusics.json", JP_EVENT_MUSICS)
    _write_json(jp, "musics.json", JP_MUSICS)
    _write_json(jp, "gameCharacterUnits.json", JP_GAME_CHARACTER_UNITS)
    _write_json(cn, "events.json", CN_EVENTS)
    _write_json(cn, "eventCards.json", CN_EVENT_CARDS)
    _write_json(cn, "cards.json", CN_CARDS)
    _write_json(cn, "eventMusics.json", CN_EVENT_MUSICS)
    _write_json(cn, "musics.json", CN_MUSICS)
    _write_json(cn, "gameCharacterUnits.json", CN_GAME_CHARACTER_UNITS)


class ParseQueryTest(unittest.TestCase):
    def test_roman_alias_with_number(self):
        info, ordinal = parse_query("khn3")
        self.assertEqual(info.id, 9)
        self.assertEqual(ordinal, 3)

    def test_roman_alias_with_chinese_numeral(self):
        info, ordinal = parse_query("khn三箱")
        self.assertEqual(info.id, 9)
        self.assertEqual(ordinal, 3)

    def test_chinese_nickname(self):
        info, ordinal = parse_query("豆三箱")
        self.assertEqual(info.id, 9)
        self.assertEqual(ordinal, 3)

    def test_chinese_full_name(self):
        info, ordinal = parse_query("小豆泽心羽三箱")
        self.assertEqual(info.id, 9)
        self.assertEqual(ordinal, 3)

    def test_given_name_with_arabic(self):
        info, ordinal = parse_query("心羽3箱")
        self.assertEqual(info.id, 9)
        self.assertEqual(ordinal, 3)

    def test_spaces_and_suffix(self):
        info, ordinal = parse_query("khn 3")
        self.assertEqual(info.id, 9)
        self.assertEqual(ordinal, 3)

    def test_an_four(self):
        info, ordinal = parse_query("an4")
        self.assertEqual(info.id, 10)
        self.assertEqual(ordinal, 4)

    def test_unknown_query(self):
        self.assertIsNone(parse_query("notanalias"))

    def test_numeral_converter(self):
        self.assertEqual(numeral_text_to_int("三"), 3)
        self.assertEqual(numeral_text_to_int("十"), 10)
        self.assertEqual(numeral_text_to_int("十一"), 11)
        self.assertEqual(numeral_text_to_int("二十"), 20)


class EventAliasMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_khn3_resolves_to_event_10(self):
        result = resolve_event_alias(self.root, "khn3", regions=["jp"])
        self.assertIsNotNone(result)
        self.assertEqual(result["ordinal"], 3)
        self.assertEqual(result["mapping"]["jp"]["event_id"], 10)
        self.assertEqual(result["mapping"]["jp"]["name"], "OVER RAD SQUAD!!")

    def test_an2_resolves_to_event_6(self):
        result = resolve_event_alias(self.root, "an2", regions=["jp"])
        self.assertIsNotNone(result)
        self.assertEqual(result["mapping"]["jp"]["event_id"], 6)
        self.assertEqual(result["mapping"]["jp"]["name"], "Bout for Beside You")

    def test_missing_ordinal_returns_none(self):
        self.assertIsNone(resolve_event_alias(self.root, "an4", regions=["jp"]))

    def test_cheerful_and_world_bloom_excluded(self):
        alias_map = build_event_alias_map(self.root, regions=["jp"])
        self.assertNotIn("6", alias_map["characters"]["6"]["box_events"])
        self.assertNotIn("14", alias_map["characters"]["14"]["box_events"])
        self.assertNotIn("19", alias_map["characters"]["19"]["box_events"])

    def test_mixed_unit_r4_not_box(self):
        # Event 4 is a cheerful carnival with R4 cards from two bands; even if
        # the event type check were removed, mixed units must not be boxes.
        boxes = build_event_alias_map(self.root, regions=["jp"])["characters"]["6"]["box_events"]
        self.assertEqual(boxes, [])

    def test_no_song_not_box(self):
        # Event 7 has an R4 card but no eventMusics row.
        boxes = build_event_alias_map(self.root, regions=["jp"])["characters"]["3"]["box_events"]
        self.assertEqual(boxes, [])

    def test_cross_region_official_names(self):
        result = resolve_event_alias(self.root, "豆三箱", regions=["jp", "cn"])
        self.assertIsNotNone(result)
        self.assertEqual(result["mapping"]["jp"]["name"], "OVER RAD SQUAD!!")
        self.assertEqual(result["mapping"]["cn"]["start_at"], 24000)
        self.assertEqual(result["mapping"]["cn"]["songs"], ["ULTRA C"])

    def test_cli_returns_one_for_no_match(self):
        import io
        import contextlib

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(
                [
                    "--store",
                    str(self.root),
                    "alias",
                    "--query",
                    "khn99",
                    "--regions",
                    "jp",
                ]
            )
        self.assertEqual(code, 1)
        self.assertIn("No event alias match", buffer.getvalue())

    def test_alias_map_lists_ordinals(self):
        alias_map = build_event_alias_map(self.root, regions=["jp"])
        self.assertEqual(
            [box["ordinal"] for box in alias_map["characters"]["9"]["box_events"]],
            [1, 2, 3],
        )

    def test_virtual_singer_r4_does_not_change_owner(self):
        result = resolve_event_alias(self.root, "khn1", regions=["jp"])
        self.assertIsNotNone(result)
        self.assertEqual(result["mapping"]["jp"]["event_id"], 8)
        self.assertEqual(result["character"]["id"], 9)


class NewOwnerRuleTest(unittest.TestCase):
    """Regression tests for the banner-first owner rule (community_box_event_v2).

    Mirrors real JP master cases:
    - Event 162: banner shiho beats first-card hnm.
    - Event  97: banner khn has no r4 card, falls back to first card an.
    - Event  27: cheerful carnival with a dedicated song counts as a box event.
    - Event  18: cheerful carnival without a dedicated song does not.
    - Event  74: no song in master data (pulled event), kept via exception.
    - Event  41: banner unit not a majority of r4 cards -> mixed event, excluded.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        jp = self.root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
        jp.mkdir(parents=True, exist_ok=True)
        self.jp = jp
        self._write("events.json", [
            {"id": 1, "eventType": "marathon", "name": "雨上がりの一番星", "startAt": 1000},
            {"id": 18, "eventType": "cheerful_carnival", "name": "君と歌う、桜舞う世界で", "startAt": 2000},
            {"id": 27, "eventType": "cheerful_carnival", "name": "Unnamed Harmony", "startAt": 3000},
            {"id": 41, "eventType": "marathon", "name": "バディ・ファニー・スペンドタイム♪", "startAt": 4000},
            {"id": 74, "eventType": "marathon", "name": "カーテンコールに惜別を", "startAt": 5000},
            {"id": 97, "eventType": "marathon", "name": "Light Up the Fire", "startAt": 6000},
            {"id": 162, "eventType": "marathon", "name": "Find the dream view", "startAt": 7000},
        ])
        # Cards keyed by id; characterId is the human character (21+ VS).
        self._write("cards.json", [
            {"id": 101, "characterId": 2, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "saki"},
            {"id": 244, "characterId": 2, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "saki2"},
            {"id": 245, "characterId": 22, "cardRarityType": "rarity_4", "supportUnit": "light_sound", "prefix": "rin"},
            {"id": 246, "characterId": 4, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "shiho"},
            {"id": 212, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "an"},
            {"id": 213, "characterId": 12, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "toya"},
            {"id": 214, "characterId": 22, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "rin"},
            {"id": 522, "characterId": 16, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "rui"},
            {"id": 523, "characterId": 13, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "tks"},
            {"id": 524, "characterId": 15, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "nene"},
            {"id": 1144, "characterId": 3, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "hnm"},
            {"id": 1145, "characterId": 24, "cardRarityType": "rarity_4", "supportUnit": "light_sound", "prefix": "luka"},
            {"id": 1146, "characterId": 4, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "shiho2"},
            {"id": 141, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "khn"},
            {"id": 142, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "an2"},
            {"id": 143, "characterId": 1, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "ick"},
            {"id": 144, "characterId": 3, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "hnm2"},
            {"id": 1018, "characterId": 1, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "ick2"},
            {"id": 1019, "characterId": 20, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "mzk"},
            {"id": 1020, "characterId": 22, "cardRarityType": "rarity_4", "supportUnit": "light_sound", "prefix": "rin2"},
        ])
        self._write("eventCards.json", [
            {"id": 1, "eventId": 1, "cardId": 101},
            {"id": 2, "eventId": 18, "cardId": 1018},
            {"id": 3, "eventId": 18, "cardId": 1019},
            {"id": 4, "eventId": 18, "cardId": 1020},
            {"id": 5, "eventId": 27, "cardId": 244},
            {"id": 6, "eventId": 27, "cardId": 245},
            {"id": 7, "eventId": 27, "cardId": 246},
            {"id": 8, "eventId": 41, "cardId": 141},
            {"id": 9, "eventId": 41, "cardId": 142},
            {"id": 10, "eventId": 41, "cardId": 143},
            {"id": 11, "eventId": 41, "cardId": 144},
            {"id": 12, "eventId": 74, "cardId": 522},
            {"id": 13, "eventId": 74, "cardId": 523},
            {"id": 14, "eventId": 74, "cardId": 524},
            {"id": 15, "eventId": 97, "cardId": 212},
            {"id": 16, "eventId": 97, "cardId": 213},
            {"id": 17, "eventId": 97, "cardId": 214},
            {"id": 18, "eventId": 162, "cardId": 1144},
            {"id": 19, "eventId": 162, "cardId": 1145},
            {"id": 20, "eventId": 162, "cardId": 1146},
        ])
        self._write("eventMusics.json", [
            {"eventId": 1, "musicId": 11, "seq": 1},
            {"eventId": 27, "musicId": 130, "seq": 1},
            {"eventId": 41, "musicId": 141, "seq": 1},
            {"eventId": 97, "musicId": 126, "seq": 1},
            {"eventId": 162, "musicId": 605, "seq": 1},
        ])
        self._write("musics.json", [
            {"id": 11, "title": "needLe"},
            {"id": 130, "title": "フロムトーキョー"},
            {"id": 141, "title": "x"},
            {"id": 126, "title": "y"},
            {"id": 605, "title": "z"},
        ])
        self._write("gameCharacterUnits.json", [
            {"id": 1, "gameCharacterId": 1, "unit": "light_sound"},
            {"id": 2, "gameCharacterId": 2, "unit": "light_sound"},
            {"id": 3, "gameCharacterId": 3, "unit": "light_sound"},
            {"id": 4, "gameCharacterId": 4, "unit": "light_sound"},
            {"id": 5, "gameCharacterId": 9, "unit": "street"},
            {"id": 6, "gameCharacterId": 10, "unit": "street"},
            {"id": 7, "gameCharacterId": 12, "unit": "street"},
            {"id": 8, "gameCharacterId": 13, "unit": "theme_park"},
            {"id": 9, "gameCharacterId": 15, "unit": "theme_park"},
            {"id": 10, "gameCharacterId": 16, "unit": "theme_park"},
            {"id": 11, "gameCharacterId": 20, "unit": "school_refusal"},
        ])
        self._write("eventStories.json", [
            {"eventId": 18, "bannerGameCharacterUnitId": 1},
            {"eventId": 27, "bannerGameCharacterUnitId": 2},
            {"eventId": 41, "bannerGameCharacterUnitId": 5},
            {"eventId": 74, "bannerGameCharacterUnitId": 10},
            {"eventId": 97, "bannerGameCharacterUnitId": 5},
            {"eventId": 162, "bannerGameCharacterUnitId": 4},
        ])

    def _write(self, name, data):
        (self.jp / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _owners(self):
        box_map = _build_jp_box_map(self.root)
        return {b.event_id: owner for owner, boxes in box_map.items() for b in boxes}

    def test_banner_beats_first_card(self):
        # Event 162: banner shiho owns it even though the first r4 card is hnm.
        self.assertEqual(self._owners().get(162), 4)

    def test_banner_without_card_falls_back_to_first_card(self):
        # Event 97: banner khn has no r4 card, so the first card owner an wins.
        self.assertEqual(self._owners().get(97), 10)

    def test_cheerful_with_song_counts_as_box(self):
        # Event 27: single-unit cheerful carnival with a dedicated song.
        self.assertEqual(self._owners().get(27), 2)

    def test_cheerful_without_song_is_excluded(self):
        # Event 18: cheerful carnival without a dedicated song stays excluded.
        self.assertNotIn(18, self._owners())

    def test_pulled_event_without_song_kept_via_exception(self):
        # Event 74: song data was removed from master after the event was pulled.
        self.assertEqual(self._owners().get(74), 16)

    def test_mixed_event_without_banner_majority_excluded(self):
        # Event 41: banner khn has a card but street holds only 2 of 4 r4 cards.
        self.assertNotIn(41, self._owners())

    def test_plain_box_event_unchanged(self):
        self.assertEqual(self._owners().get(1), 2)


class ActivityTest(unittest.TestCase):
    """Unified dispatch: World Link first, then box, else unresolved."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        jp = self.root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
        jp.mkdir(parents=True, exist_ok=True)
        # Self-contained fixture with one box event (E1, khn) and one WL
        # (E112, VBS) so the dispatch order can be asserted cleanly.
        _write_json(jp, "events.json", [
            {"id": 1, "eventType": "marathon", "name": "雨上がりの一番星", "startAt": 1000},
            {"id": 112, "eventType": "world_bloom", "unit": "street", "name": "水底に影を探して", "startAt": 2000},
            {"id": 41, "eventType": "marathon", "unit": "none", "name": "バディ・ファニー・スペンドタイム♪", "startAt": 3000},
        ])
        _write_json(jp, "cards.json", [
            {"id": 1, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "khn"},
            {"id": 2, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "an"},
            {"id": 3, "characterId": 11, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "akt"},
            {"id": 4, "characterId": 12, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "toya"},
        ])
        _write_json(jp, "eventCards.json", [
            {"id": 1, "eventId": 1, "cardId": 1},
            {"id": 2, "eventId": 1, "cardId": 2},
            {"id": 3, "eventId": 112, "cardId": 1},
            {"id": 4, "eventId": 112, "cardId": 2},
            {"id": 5, "eventId": 112, "cardId": 3},
            {"id": 6, "eventId": 112, "cardId": 4},
        ])
        _write_json(jp, "eventMusics.json", [{"eventId": 1, "musicId": 11, "seq": 1}])
        _write_json(jp, "musics.json", [{"id": 11, "title": "needLe"}])
        _write_json(jp, "gameCharacterUnits.json", [
            {"id": 1, "gameCharacterId": 9, "unit": "street"},
            {"id": 2, "gameCharacterId": 10, "unit": "street"},
            {"id": 3, "gameCharacterId": 11, "unit": "street"},
            {"id": 4, "gameCharacterId": 12, "unit": "street"},
        ])
        _write_json(jp, "eventStories.json", [
            {"eventId": 1, "bannerGameCharacterUnitId": 1},
            {"eventId": 112, "bannerGameCharacterUnitId": 1},
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_wl_query_dispatches_to_worldlink(self):
        result = resolve_activity(self.root, "vbs wl1", regions=["jp"])
        self.assertEqual(result["kind"], "wl")
        self.assertEqual(result["code"], "wl1g1")
        self.assertEqual(result["event_id"], 112)

    def test_box_query_dispatches_to_eventalias(self):
        result = resolve_activity(self.root, "khn1", regions=["jp"])
        self.assertEqual(result["kind"], "box")
        self.assertEqual(result["character"]["id"], 9)
        self.assertEqual(result["mapping"]["jp"]["event_id"], 1)

    def test_natural_language_wl(self):
        result = resolve_activity(self.root, "第一轮第一组", regions=["jp"])
        self.assertEqual(result["kind"], "wl")
        self.assertEqual(result["code"], "wl1g1")

    def test_unresolved_mixed_event(self):
        result = resolve_activity(self.root, "バディ・ファニー・スペンドタイム", regions=["jp"])
        self.assertEqual(result["kind"], "unresolved")

    def test_unknown_query(self):
        result = resolve_activity(self.root, "notanevent", regions=["jp"])
        self.assertEqual(result["kind"], "unresolved")


if __name__ == "__main__":
    unittest.main()

