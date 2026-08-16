from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.cli import main as cli_main
from sekaisync.eventalias import (
    build_event_alias_map,
    numeral_text_to_int,
    parse_query,
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


if __name__ == "__main__":
    unittest.main()

