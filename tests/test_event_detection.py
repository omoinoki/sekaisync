from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sekaisync.cli import auto_event_check
from sekaisync.config import SekaiSyncConfig

from sekaisync.event_detection import (
    apply_master_base,
    check_events,
    detect_new_events,
    list_events,
    load_local_events,
)
from sekaisync.progress import expected_text_units, expected_fact_units


def _write(directory: Path, name: str, data) -> None:
    (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


LOCAL_EVENTS = [
    {"id": 1, "eventType": "marathon", "name": "Event One", "startAt": 1000},
    {"id": 2, "eventType": "marathon", "name": "Event Two", "startAt": 2000},
]
REMOTE_EVENTS = [
    {"id": 1, "eventType": "marathon", "name": "Event One", "startAt": 1000},
    {"id": 2, "eventType": "marathon", "name": "Event Two", "startAt": 2000},
    {"id": 3, "eventType": "marathon", "name": "Kick it up a notch", "startAt": 3000},
    {"id": 4, "eventType": "world_bloom", "name": "World Link Event", "startAt": 4000},
    {"id": 5, "eventType": "marathon", "name": "Mixed Event", "startAt": 5000},
]
REMOTE_STORIES = [
    {"id": 3, "eventId": 3, "outline": "box", "eventStoryEpisodes": [
        {"id": 31, "eventStoryId": 3, "episodeNo": 1, "title": "One"},
        {"id": 32, "eventStoryId": 3, "episodeNo": 2, "title": "Two"},
    ]},
    {"id": 4, "eventId": 4, "outline": "wl", "eventStoryEpisodes": [
        {"id": 41, "eventStoryId": 4, "episodeNo": 1, "title": "WL"},
    ]},
    {"id": 5, "eventId": 5, "outline": "other", "eventStoryEpisodes": [
        {"id": 51, "eventStoryId": 5, "episodeNo": 1, "title": "Other"},
    ]},
]
REMOTE_EVENT_CARDS = [
    {"id": 10, "eventId": 3, "cardId": 110},
    {"id": 11, "eventId": 3, "cardId": 111},
    {"id": 12, "eventId": 4, "cardId": 112},
    {"id": 13, "eventId": 5, "cardId": 113},
]
REMOTE_CARDS = [
    {"id": 110, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "Kohane"},
    {"id": 111, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "An"},
    {"id": 112, "characterId": 19, "cardRarityType": "rarity_4", "supportUnit": "school_refusal", "prefix": "Ena"},
    {"id": 113, "characterId": 6, "cardRarityType": "rarity_4", "supportUnit": "idol", "prefix": "Haruka"},
]
REMOTE_EVENT_MUSICS = [
    {"eventId": 3, "musicId": 30, "seq": 1},
    {"eventId": 4, "musicId": 40, "seq": 1},
    {"eventId": 5, "musicId": 50, "seq": 1},
]
REMOTE_MUSICS = [
    {"id": 30, "title": "ひつじがいっぴき"},
    {"id": 40, "title": "WL Song"},
    {"id": 50, "title": "Mixed Song"},
]
REMOTE_UNITS = [
    {"id": 1, "gameCharacterId": 9, "unit": "street"},
    {"id": 2, "gameCharacterId": 10, "unit": "street"},
    {"id": 3, "gameCharacterId": 19, "unit": "school_refusal"},
    {"id": 4, "gameCharacterId": 6, "unit": "idol"},
]

TABLES = {
    "events": REMOTE_EVENTS,
    "eventStories": REMOTE_STORIES,
    "eventCards": REMOTE_EVENT_CARDS,
    "cards": REMOTE_CARDS,
    "eventMusics": REMOTE_EVENT_MUSICS,
    "musics": REMOTE_MUSICS,
    "gameCharacterUnits": REMOTE_UNITS,
}


def fake_fetcher(url: str, timeout: int = 30) -> str:
    table = url.rsplit("/", 1)[-1].removesuffix(".json")
    return json.dumps(TABLES.get(table, []))


def make_store(root: Path) -> None:
    jp = root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
    jp.mkdir(parents=True, exist_ok=True)
    _write(jp, "events.json", LOCAL_EVENTS)
    _write(jp, "eventStories.json", [])
    _write(jp, "eventCards.json", [])
    _write(jp, "cards.json", [])
    _write(jp, "eventMusics.json", [])
    _write(jp, "musics.json", [])
    _write(jp, "gameCharacterUnits.json", REMOTE_UNITS)


class EventDetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "store"
        make_store(self.root)
        # Real endpoint required by the fetcher; tests use a mocked fetcher.
        apply_master_base("https://example.test")

    def tearDown(self):
        apply_master_base("")
        self.tmp.cleanup()

    def test_detect_new_events(self):
        new = detect_new_events(REMOTE_EVENTS, LOCAL_EVENTS)
        self.assertEqual([int(e["id"]) for e in new], [3, 4, 5])

    def test_check_fetches_classifies_and_archives(self):
        result = check_events(self.root, regions=["jp"], fetcher=fake_fetcher, timeout=5)
        self.assertTrue(result["crawler_started"] is False)
        self.assertEqual(result["detected_total"], 3)
        jp_new = result["regions"]["jp"]["new_events"]
        by_id = {e["event_id"]: e for e in jp_new}
        self.assertEqual(by_id[3]["category"], "box")
        self.assertEqual(by_id[3]["label"], "箱活")
        self.assertEqual(by_id[4]["category"], "world_bloom")
        self.assertEqual(by_id[4]["label"], "WL")
        self.assertEqual(by_id[5]["category"], "other")

        # Base data was merged into the local source tree.
        events = load_local_events(self.root, "jp")
        self.assertEqual(len(events), 5)
        listed = list_events(self.root, regions=["jp"])
        self.assertEqual(listed["total"], 5)
        by_id = {e["event_id"]: e for e in listed["regions"]["jp"]}
        self.assertEqual(by_id[3]["category"], "box")
        self.assertEqual(by_id[4]["category"], "world_bloom")
        self.assertEqual(by_id[5]["category"], "other")

    def test_idempotent_check(self):
        check_events(self.root, regions=["jp"], fetcher=fake_fetcher, timeout=5)
        second = check_events(self.root, regions=["jp"], fetcher=fake_fetcher, timeout=5)
        self.assertEqual(second["detected_total"], 0)
        self.assertEqual(second["regions"]["jp"]["status"], "up_to_date")
        self.assertEqual(len(load_local_events(self.root, "jp")), 5)

    def test_progress_denominator_grows_after_merge(self):
        from sekaisync.progress import compute_progress

        before = compute_progress(self.root, regions=["jp"], now=6000)
        before_events = before["regions"]["jp"]["text"]["categories"]["event_story"]["expected"]
        self.assertEqual(before_events, 0)

        check_events(self.root, regions=["jp"], fetcher=fake_fetcher, timeout=5)
        after = compute_progress(self.root, regions=["jp"], now=6000)
        after_events = after["regions"]["jp"]["text"]["categories"]["event_story"]["expected"]
        self.assertEqual(after_events, 4)
        self.assertGreater(after["regions"]["jp"]["activity"]["released_events"], 2)

    def test_network_failure_keeps_local_store_intact(self):
        def broken_fetcher(url: str, timeout: int = 30) -> str:
            raise OSError("offline")

        result = check_events(self.root, regions=["jp"], fetcher=broken_fetcher, timeout=5)
        self.assertEqual(result["regions"]["jp"]["status"], "skipped")
        self.assertEqual(len(load_local_events(self.root, "jp")), 2)
        self.assertEqual(list_events(self.root, regions=["jp"])["total"], 0)



    def test_missing_baseline_skipped_unless_allow_initial(self):
        empty = self.root / "fresh"
        empty.mkdir(parents=True, exist_ok=True)

        def unexpected(url: str, timeout: int = 30) -> str:
            raise AssertionError(f"network must not be touched: {url}")

        result = check_events(empty, regions=["jp"], fetcher=unexpected, timeout=5)
        self.assertEqual(result["regions"]["jp"]["status"], "no_local_baseline")

        result = check_events(empty, regions=["jp"], fetcher=fake_fetcher, timeout=5, allow_initial=True)
        self.assertEqual(result["detected_total"], 5)
        self.assertEqual(len(load_local_events(empty, "jp")), 5)

    def test_daily_limit_skips_second_auto_check_same_jst_day(self):
        calls = {"n": 0}

        def counting_fetcher(url: str, timeout: int = 30) -> str:
            calls["n"] += 1
            return fake_fetcher(url, timeout)

        first = check_events(self.root, regions=["jp"], fetcher=counting_fetcher, timeout=5, daily_limit=True)
        self.assertEqual(first["detected_total"], 3)
        self.assertGreater(calls["n"], 0)
        after_first = calls["n"]

        second = check_events(self.root, regions=["jp"], fetcher=counting_fetcher, timeout=5, daily_limit=True)
        self.assertTrue(second.get("daily_limit"))
        self.assertEqual(calls["n"], after_first)

        third = check_events(self.root, regions=["jp"], fetcher=counting_fetcher, timeout=5, daily_limit=False)
        self.assertGreater(calls["n"], after_first)
        self.assertEqual(third["regions"]["jp"]["status"], "up_to_date")

    def test_auto_event_check_daily_limit_config_and_force(self):
        enabled = SekaiSyncConfig(store_root=self.root, extra={})
        with mock.patch("sekaisync.cli.check_events", return_value={}) as fake:
            auto_event_check(enabled, ["jp"])
            self.assertTrue(fake.call_args.kwargs["daily_limit"])

        disabled = SekaiSyncConfig(store_root=self.root, extra={"event_check_daily_limit": False})
        with mock.patch("sekaisync.cli.check_events", return_value={}) as fake:
            auto_event_check(disabled, ["jp"])
            self.assertFalse(fake.call_args.kwargs["daily_limit"])

        with mock.patch("sekaisync.cli.check_events", return_value={}) as fake:
            auto_event_check(enabled, ["jp"], force=True)
            self.assertFalse(fake.call_args.kwargs["daily_limit"])
if __name__ == "__main__":
    unittest.main()





