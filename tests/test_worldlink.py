"""Tests for the World Link (world_bloom) mapping module."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.worldlink import (
    build_wl_map,
    parse_wl_query,
    resolve_wl,
)

# Mirrors real JP master data for the 17 JP world_bloom events (as of 2026-08).
WL_EVENTS = [
    {"id": 112, "eventType": "world_bloom", "unit": "school_refusal", "name": "水底に影を探して", "startAt": 1699365600000},
    {"id": 118, "eventType": "world_bloom", "unit": "street", "name": "BREAK DOWN THE WALL", "startAt": 1704780000000},
    {"id": 124, "eventType": "world_bloom", "unit": "theme_park", "name": "星を目指して、ヨーソロー！", "startAt": 1709892000000},
    {"id": 130, "eventType": "world_bloom", "unit": "idol", "name": "いつか花咲くステージへ", "startAt": 1715248800000},
    {"id": 137, "eventType": "world_bloom", "unit": "light_sound", "name": "あの日見た夜空は、いつかの未来へ", "startAt": 1721210400000},
    {"id": 140, "eventType": "world_bloom", "unit": "none", "name": "キミと、セカイの始まりで", "startAt": 1723888800000},
    {"id": 163, "eventType": "world_bloom", "unit": "street", "name": "Turning Pain into Drive", "startAt": 1744110000000},
    {"id": 167, "eventType": "world_bloom", "unit": "theme_park", "name": "Dear my fellows", "startAt": 1746770400000},
    {"id": 170, "eventType": "world_bloom", "unit": "school_refusal", "name": "泡沫に抱かれて", "startAt": 1749430800000},
    {"id": 171, "eventType": "world_bloom", "unit": "light_sound", "name": "ordinary,yet special us", "startAt": 1750640400000},
    {"id": 176, "eventType": "world_bloom", "unit": "idol", "name": "All ways Jump! with you", "startAt": 1754114400000},
    {"id": 179, "eventType": "world_bloom", "unit": "none", "name": "Link the Beats！", "startAt": 1756692000000},
    {"id": 180, "eventType": "world_bloom", "unit": "none", "name": "Wishes in Bloom！", "startAt": 1758250800000},
    {"id": 202, "eventType": "world_bloom", "unit": "none", "name": "約束のPasserelle", "startAt": 1774850400000},
    {"id": 205, "eventType": "world_bloom", "unit": "none", "name": "Great Yell for Dreamers！", "startAt": 1777438800000},
    {"id": 207, "eventType": "world_bloom", "unit": "none", "name": "Into the New Light", "startAt": 1779339600000},
    {"id": 211, "eventType": "world_bloom", "unit": "none", "name": "君の隣、君と見る明日", "startAt": 1782957600000},
]

# Only the r4 cards per event; characterId 21-26 are virtual singers.
WL_EVENT_CARDS = [
    # unit_wl: four human members of one unit
    {"id": 1, "eventId": 112, "cardId": 1}, {"id": 2, "eventId": 112, "cardId": 2},
    {"id": 3, "eventId": 112, "cardId": 3}, {"id": 4, "eventId": 112, "cardId": 4},
    {"id": 5, "eventId": 118, "cardId": 9}, {"id": 6, "eventId": 118, "cardId": 10},
    {"id": 7, "eventId": 118, "cardId": 11}, {"id": 8, "eventId": 118, "cardId": 12},
    {"id": 9, "eventId": 124, "cardId": 13}, {"id": 10, "eventId": 124, "cardId": 14},
    {"id": 11, "eventId": 124, "cardId": 15}, {"id": 12, "eventId": 124, "cardId": 16},
    {"id": 13, "eventId": 130, "cardId": 5}, {"id": 14, "eventId": 130, "cardId": 6},
    {"id": 15, "eventId": 130, "cardId": 7}, {"id": 16, "eventId": 130, "cardId": 8},
    {"id": 17, "eventId": 137, "cardId": 17}, {"id": 18, "eventId": 137, "cardId": 18},
    {"id": 19, "eventId": 137, "cardId": 19}, {"id": 20, "eventId": 137, "cardId": 20},
    # virtual_singer: all six VS
    {"id": 21, "eventId": 140, "cardId": 21}, {"id": 22, "eventId": 140, "cardId": 22},
    {"id": 23, "eventId": 140, "cardId": 23}, {"id": 24, "eventId": 140, "cardId": 24},
    {"id": 25, "eventId": 140, "cardId": 25}, {"id": 26, "eventId": 140, "cardId": 26},
    {"id": 27, "eventId": 179, "cardId": 21}, {"id": 28, "eventId": 179, "cardId": 22},
    {"id": 29, "eventId": 179, "cardId": 23}, {"id": 30, "eventId": 179, "cardId": 24},
    {"id": 31, "eventId": 179, "cardId": 25}, {"id": 32, "eventId": 179, "cardId": 26},
    # finale: all 26 characters
    {"id": 33, "eventId": 180, "cardId": 1}, {"id": 34, "eventId": 180, "cardId": 2},
    {"id": 35, "eventId": 180, "cardId": 3}, {"id": 36, "eventId": 180, "cardId": 4},
    {"id": 37, "eventId": 180, "cardId": 5}, {"id": 38, "eventId": 180, "cardId": 6},
    {"id": 39, "eventId": 180, "cardId": 7}, {"id": 40, "eventId": 180, "cardId": 8},
    {"id": 41, "eventId": 180, "cardId": 9}, {"id": 42, "eventId": 180, "cardId": 10},
    {"id": 43, "eventId": 180, "cardId": 11}, {"id": 44, "eventId": 180, "cardId": 12},
    {"id": 45, "eventId": 180, "cardId": 13}, {"id": 46, "eventId": 180, "cardId": 14},
    {"id": 47, "eventId": 180, "cardId": 15}, {"id": 48, "eventId": 180, "cardId": 16},
    {"id": 49, "eventId": 180, "cardId": 17}, {"id": 50, "eventId": 180, "cardId": 18},
    {"id": 51, "eventId": 180, "cardId": 19}, {"id": 52, "eventId": 180, "cardId": 20},
    {"id": 53, "eventId": 180, "cardId": 21}, {"id": 54, "eventId": 180, "cardId": 22},
    {"id": 55, "eventId": 180, "cardId": 23}, {"id": 56, "eventId": 180, "cardId": 24},
    {"id": 57, "eventId": 180, "cardId": 25}, {"id": 58, "eventId": 180, "cardId": 26},
    # group: one VS + four humans from different units
    {"id": 59, "eventId": 202, "cardId": 21}, {"id": 60, "eventId": 202, "cardId": 1},
    {"id": 61, "eventId": 202, "cardId": 6}, {"id": 62, "eventId": 202, "cardId": 14},
    {"id": 63, "eventId": 202, "cardId": 17},
    {"id": 64, "eventId": 205, "cardId": 22}, {"id": 65, "eventId": 205, "cardId": 23},
    {"id": 66, "eventId": 205, "cardId": 4}, {"id": 67, "eventId": 205, "cardId": 5},
    {"id": 68, "eventId": 205, "cardId": 10}, {"id": 69, "eventId": 205, "cardId": 13},
    {"id": 70, "eventId": 207, "cardId": 24}, {"id": 71, "eventId": 207, "cardId": 3},
    {"id": 72, "eventId": 207, "cardId": 8}, {"id": 73, "eventId": 207, "cardId": 9},
    {"id": 74, "eventId": 207, "cardId": 18},
    {"id": 75, "eventId": 211, "cardId": 25}, {"id": 76, "eventId": 211, "cardId": 2},
    {"id": 77, "eventId": 211, "cardId": 12}, {"id": 78, "eventId": 211, "cardId": 16},
    {"id": 79, "eventId": 211, "cardId": 20},
]

WL_CARDS = [
    {"id": 1, "characterId": 1, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p1"},
    {"id": 2, "characterId": 2, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p2"},
    {"id": 3, "characterId": 3, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p3"},
    {"id": 4, "characterId": 4, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p4"},
    {"id": 5, "characterId": 5, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p5"},
    {"id": 6, "characterId": 6, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p6"},
    {"id": 7, "characterId": 7, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p7"},
    {"id": 8, "characterId": 8, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p8"},
    {"id": 9, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p9"},
    {"id": 10, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p10"},
    {"id": 11, "characterId": 11, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p11"},
    {"id": 12, "characterId": 12, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p12"},
    {"id": 13, "characterId": 13, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p13"},
    {"id": 14, "characterId": 14, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p14"},
    {"id": 15, "characterId": 15, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p15"},
    {"id": 16, "characterId": 16, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p16"},
    {"id": 17, "characterId": 17, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p17"},
    {"id": 18, "characterId": 18, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p18"},
    {"id": 19, "characterId": 19, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p19"},
    {"id": 20, "characterId": 20, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p20"},
    {"id": 21, "characterId": 21, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p21"},
    {"id": 22, "characterId": 22, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p22"},
    {"id": 23, "characterId": 23, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p23"},
    {"id": 24, "characterId": 24, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p24"},
    {"id": 25, "characterId": 25, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p25"},
    {"id": 26, "characterId": 26, "cardRarityType": "rarity_4", "supportUnit": "none", "prefix": "p26"},
]

WL_GAME_CHARACTER_UNITS = [
    {"id": 1, "gameCharacterId": 1, "unit": "light_sound"},
    {"id": 2, "gameCharacterId": 2, "unit": "light_sound"},
    {"id": 3, "gameCharacterId": 3, "unit": "light_sound"},
    {"id": 4, "gameCharacterId": 4, "unit": "light_sound"},
    {"id": 5, "gameCharacterId": 5, "unit": "idol"},
    {"id": 6, "gameCharacterId": 6, "unit": "idol"},
    {"id": 7, "gameCharacterId": 7, "unit": "idol"},
    {"id": 8, "gameCharacterId": 8, "unit": "idol"},
    {"id": 9, "gameCharacterId": 9, "unit": "street"},
    {"id": 10, "gameCharacterId": 10, "unit": "street"},
    {"id": 11, "gameCharacterId": 11, "unit": "street"},
    {"id": 12, "gameCharacterId": 12, "unit": "street"},
    {"id": 13, "gameCharacterId": 13, "unit": "theme_park"},
    {"id": 14, "gameCharacterId": 14, "unit": "theme_park"},
    {"id": 15, "gameCharacterId": 15, "unit": "theme_park"},
    {"id": 16, "gameCharacterId": 16, "unit": "theme_park"},
    {"id": 17, "gameCharacterId": 17, "unit": "school_refusal"},
    {"id": 18, "gameCharacterId": 18, "unit": "school_refusal"},
    {"id": 19, "gameCharacterId": 19, "unit": "school_refusal"},
    {"id": 20, "gameCharacterId": 20, "unit": "school_refusal"},
    {"id": 21, "gameCharacterId": 21, "unit": "piapro"},
    {"id": 22, "gameCharacterId": 22, "unit": "piapro"},
    {"id": 23, "gameCharacterId": 23, "unit": "piapro"},
    {"id": 24, "gameCharacterId": 24, "unit": "piapro"},
    {"id": 25, "gameCharacterId": 25, "unit": "piapro"},
    {"id": 26, "gameCharacterId": 26, "unit": "piapro"},
]

WL_EVENT_STORIES = [
    {"eventId": 112, "bannerGameCharacterUnitId": 17},
    {"eventId": 118, "bannerGameCharacterUnitId": 9},
    {"eventId": 124, "bannerGameCharacterUnitId": 13},
    {"eventId": 130, "bannerGameCharacterUnitId": 5},
    {"eventId": 137, "bannerGameCharacterUnitId": 1},
    {"eventId": 140, "bannerGameCharacterUnitId": 21},
    {"eventId": 163, "bannerGameCharacterUnitId": 9},
    {"eventId": 167, "bannerGameCharacterUnitId": 13},
    {"eventId": 170, "bannerGameCharacterUnitId": 17},
    {"eventId": 171, "bannerGameCharacterUnitId": 1},
    {"eventId": 176, "bannerGameCharacterUnitId": 5},
    {"eventId": 179, "bannerGameCharacterUnitId": 21},
    # 180 (finale), 202/205/207/211 (group) have no banner
]

CN_EVENTS = [
    {"id": 118, "eventType": "world_bloom", "unit": "street", "name": "BREAK DOWN THE WALL", "startAt": 20000},
    {"id": 163, "eventType": "world_bloom", "unit": "street", "name": "Turning Pain into Drive", "startAt": 21000},
]


def _write_json(directory: Path, name: str, data) -> None:
    (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_store(root: Path) -> None:
    jp = root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
    cn = root / "raw" / "cn" / "source" / "sekai-master-db-cn-diff-main"
    jp.mkdir(parents=True, exist_ok=True)
    cn.mkdir(parents=True, exist_ok=True)
    _write_json(jp, "events.json", WL_EVENTS)
    _write_json(jp, "eventCards.json", WL_EVENT_CARDS)
    _write_json(jp, "cards.json", WL_CARDS)
    _write_json(jp, "eventMusics.json", [])
    _write_json(jp, "musics.json", [])
    _write_json(jp, "gameCharacterUnits.json", WL_GAME_CHARACTER_UNITS)
    _write_json(jp, "eventStories.json", WL_EVENT_STORIES)
    _write_json(cn, "events.json", CN_EVENTS)
    _write_json(cn, "eventCards.json", [])
    _write_json(cn, "cards.json", WL_CARDS)
    _write_json(cn, "eventMusics.json", [])
    _write_json(cn, "musics.json", [])
    _write_json(cn, "gameCharacterUnits.json", WL_GAME_CHARACTER_UNITS)
    _write_json(cn, "eventStories.json", [])


class ParseWLQueryTest(unittest.TestCase):
    def test_unit_aliases(self):
        self.assertEqual(parse_wl_query("vbs wl2"), ("unit_wl", "street", 2))
        self.assertEqual(parse_wl_query("vbs wl"), ("unit_wl", "street", 1))
        self.assertEqual(parse_wl_query("wxs wl"), ("unit_wl", "theme_park", 1))
        self.assertEqual(parse_wl_query("25wl"), ("unit_wl", "school_refusal", 1))
        self.assertEqual(parse_wl_query("mmj wl"), ("unit_wl", "idol", 1))
        self.assertEqual(parse_wl_query("ln wl"), ("unit_wl", "light_sound", 1))

    def test_virtual_singer(self):
        self.assertEqual(parse_wl_query("vs wl"), ("virtual_singer", "", 1))
        self.assertEqual(parse_wl_query("vs wl2"), ("virtual_singer", "", 2))

    def test_group_alias_removed(self):
        # groupN aliases were dropped; wl3 events are addressed by code only.
        self.assertIsNone(parse_wl_query("group3"))
        self.assertIsNone(parse_wl_query("wl group1"))

    def test_finale(self):
        self.assertEqual(parse_wl_query("finale"), ("finale", "", 1))
        self.assertEqual(parse_wl_query("终章"), ("finale", "", 1))

    def test_code(self):
        self.assertEqual(parse_wl_query("wl1g6"), ("code", "1", 6))
        self.assertEqual(parse_wl_query("wl2g7"), ("code", "2", 7))
        self.assertEqual(parse_wl_query("wl 2 g 3"), ("code", "2", 3))

    def test_natural_language_forms(self):
        self.assertEqual(parse_wl_query("wl3第2组"), ("code", "3", 2))
        self.assertEqual(parse_wl_query("第3轮世界连接活动第2组"), ("code", "3", 2))
        self.assertEqual(parse_wl_query("第三轮第二组"), ("code", "3", 2))
        self.assertEqual(parse_wl_query("第10轮第5组"), ("code", "10", 5))

    def test_round(self):
        self.assertEqual(parse_wl_query("round2"), ("round", "", 2))
        self.assertEqual(parse_wl_query("wl round2"), ("round", "", 2))

    def test_wlN_means_round(self):
        # wlN is the Nth round as a whole (community "WL3" = round 3).
        self.assertEqual(parse_wl_query("wl1"), ("round", "", 1))
        self.assertEqual(parse_wl_query("wl 3"), ("round", "", 3))

    def test_unknown(self):
        self.assertIsNone(parse_wl_query("khn3"))
        self.assertIsNone(parse_wl_query("foo bar"))


class WLMapTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        make_store(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_rounds_split_by_gap(self):
        alias_map = build_wl_map(self.root, regions=["jp"])
        rounds = alias_map["rounds"]
        self.assertEqual(
            [r["events"][0]["event_id"] for r in rounds],
            [112, 163, 202],
        )
        self.assertEqual(len(rounds), 3)

    def test_unit_sequences(self):
        alias_map = build_wl_map(self.root, regions=["jp"])
        self.assertEqual(
            [e["regions"]["jp"]["event_id"] for e in alias_map["units"]["street"]],
            [118, 163],
        )
        self.assertEqual(
            [e["regions"]["jp"]["event_id"] for e in alias_map["units"]["light_sound"]],
            [137, 171],
        )

    def test_virtual_singers_and_finale(self):
        alias_map = build_wl_map(self.root, regions=["jp"])
        self.assertEqual(
            [e["regions"]["jp"]["event_id"] for e in alias_map["virtual_singers"]],
            [140, 179],
        )
        self.assertEqual(alias_map["finale"][0]["regions"]["jp"]["event_id"], 180)

    def test_groups(self):
        alias_map = build_wl_map(self.root, regions=["jp"])
        self.assertEqual(
            [e["regions"]["jp"]["event_id"] for e in alias_map["groups"]],
            [202, 205, 207, 211],
        )

    def test_resolve_unit_wl(self):
        result = resolve_wl(self.root, "vbs wl2", regions=["jp"])
        self.assertIsNotNone(result)
        self.assertEqual(result["subtype"], "unit_wl")
        self.assertEqual(result["event_id"], 163)
        self.assertEqual(result["unit_name"], "Vivid BAD SQUAD")

    def test_group_events_only_by_code(self):
        # groupN aliases are removed; wl3 events resolve only via wl3gN codes.
        self.assertIsNone(resolve_wl(self.root, "group3", regions=["jp"]))
        self.assertEqual(resolve_wl(self.root, "wl3g3", regions=["jp"])["event_id"], 207)

    def test_resolve_vs_and_finale(self):
        self.assertEqual(resolve_wl(self.root, "vs wl", regions=["jp"])["event_id"], 140)
        self.assertEqual(resolve_wl(self.root, "finale", regions=["jp"])["event_id"], 180)

    def test_resolve_wlN_returns_whole_round(self):
        result = resolve_wl(self.root, "wl1", regions=["jp"])
        self.assertEqual(result["kind"], "round")
        self.assertEqual(result["round"], 1)
        self.assertEqual([e["event_id"] for e in result["events"]], [112, 118, 124, 130, 137, 140])
        self.assertIsNone(resolve_wl(self.root, "wl99", regions=["jp"]))

    def test_wlN_equals_roundN(self):
        # wl3 and round3 are the same query now.
        self.assertEqual(
            [e["event_id"] for e in resolve_wl(self.root, "wl3", regions=["jp"])["events"]],
            [e["event_id"] for e in resolve_wl(self.root, "round3", regions=["jp"])["events"]],
        )

    def test_ordinal_out_of_range(self):
        self.assertIsNone(resolve_wl(self.root, "vbs wl9", regions=["jp"]))

    def test_cross_region_names(self):
        result = resolve_wl(self.root, "vbs wl2", regions=["jp", "cn"])
        self.assertEqual(result["mapping"]["regions"]["cn"]["name"], "Turning Pain into Drive")

    def test_resolve_round_returns_whole_round(self):
        result = resolve_wl(self.root, "round2", regions=["jp"])
        self.assertIsNotNone(result)
        self.assertEqual(result["kind"], "round")
        self.assertEqual(result["round"], 2)
        event_ids = [e["event_id"] for e in result["events"]]
        self.assertEqual(event_ids, [163, 167, 170, 171, 176, 179, 180])

    def test_finale_carries_its_round(self):
        # E180 is the grand finale that closes round 2; resolving it must
        # associate it with the whole "wl2" round.
        result = resolve_wl(self.root, "finale", regions=["jp"])
        self.assertEqual(result["event_id"], 180)
        self.assertEqual(result["round"]["round"], 2)
        round_ids = [e["event_id"] for e in result["round"]["events"]]
        self.assertIn(163, round_ids)
        self.assertIn(180, round_ids)

    def test_every_result_carries_round_association(self):
        result = resolve_wl(self.root, "vbs wl2", regions=["jp"])
        self.assertEqual(result["round"]["round"], 2)
        result = resolve_wl(self.root, "wl2g1", regions=["jp"])
        self.assertEqual(result["round"]["round"], 2)

    def test_resolve_by_code(self):
        self.assertEqual(resolve_wl(self.root, "wl1g6", regions=["jp"])["event_id"], 140)
        self.assertEqual(resolve_wl(self.root, "wl2g7", regions=["jp"])["event_id"], 180)
        self.assertEqual(resolve_wl(self.root, "wl3g4", regions=["jp"])["event_id"], 211)
        self.assertIsNone(resolve_wl(self.root, "wl2g9", regions=["jp"]))

    def test_code_is_canonical(self):
        # Unit / VS / finale aliases all resolve to the same code; wl3 events
        # are addressed by code directly.
        self.assertEqual(resolve_wl(self.root, "vbs wl1", regions=["jp"])["code"], "wl1g2")
        self.assertEqual(resolve_wl(self.root, "vs wl1", regions=["jp"])["code"], "wl1g6")
        self.assertEqual(resolve_wl(self.root, "wl3g3", regions=["jp"])["code"], "wl3g3")
        self.assertEqual(resolve_wl(self.root, "finale", regions=["jp"])["code"], "wl2g7")
        self.assertEqual(resolve_wl(self.root, "vbs wl2", regions=["jp"])["code"], "wl2g1")

    def test_aliases_included(self):
        result = resolve_wl(self.root, "finale", regions=["jp"])
        self.assertIn("wl2g7", result["aliases"])
        self.assertIn("finale", result["aliases"])


if __name__ == "__main__":
    unittest.main()
