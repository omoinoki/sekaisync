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
    _merge_event_rows,
    _merge_table_records,
    _row_identity,
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
        # Event 5 is a marathon event with a rarity-4 card and a dedicated song,
        # so it qualifies as a box event. It previously classified as "other"
        # only because the id-less eventMusics rows collapsed to a single
        # survivor, leaving events 4 and 5 without their song (Astra P16/D16).
        self.assertEqual(by_id[5]["category"], "box")

        # Base data was merged into the local source tree.
        events = load_local_events(self.root, "jp")
        self.assertEqual(len(events), 5)
        listed = list_events(self.root, regions=["jp"])
        self.assertEqual(listed["total"], 5)
        by_id = {e["event_id"]: e for e in listed["regions"]["jp"]}
        self.assertEqual(by_id[3]["category"], "box")
        self.assertEqual(by_id[4]["category"], "world_bloom")
        self.assertEqual(by_id[5]["category"], "box")

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


class SequenceNumberTest(unittest.TestCase):
    def test_sequence_numbered_skips_placeholders(self):
        from sekaisync.event_detection import _sequence_numbered

        entries = [
            {"event_id": 165, "start_at": "1000"},
            {"event_id": 166, "start_at": "1100"},  # placeholder, never launched
            {"event_id": 167, "start_at": "1200"},
            {"event_id": 168, "start_at": "1300"},
        ]
        numbered = _sequence_numbered(entries, {"166"})
        by_id = {str(item["event_id"]): item for item in numbered}
        # Placeholder keeps no position; real events renumber densely.
        self.assertEqual(by_id["165"]["sequence_no"], 1)
        self.assertEqual(by_id["166"]["sequence_no"], None)
        self.assertTrue(by_id["166"]["placeholder"])
        self.assertEqual(by_id["167"]["sequence_no"], 2)
        self.assertEqual(by_id["168"]["sequence_no"], 3)
        self.assertFalse(by_id["167"]["placeholder"])

    def test_placeholder_detection_from_master_files(self):
        from sekaisync.event_detection import _jp_placeholder_event_ids

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            jp = root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
            jp.mkdir(parents=True, exist_ok=True)
            # E166 has no cards/music/stories; E167 has all three.
            _write(jp, "events.json", [
                {"id": 166, "eventType": "marathon", "name": "Placeholder"},
                {"id": 167, "eventType": "marathon", "name": "Real"},
            ])
            _write(jp, "eventCards.json", [{"eventId": 167, "cardId": 1}])
            _write(jp, "eventMusics.json", [{"eventId": 167, "musicId": 1}])
            _write(jp, "eventStories.json", [{"eventId": 167, "id": 1}])
            placeholders = _jp_placeholder_event_ids(root)
            self.assertIn("166", placeholders)
            self.assertNotIn("167", placeholders)

    def test_list_events_lazily_backfills_sequence_no(self):
        """Archives written before sequence_no landed self-heal on read."""
        from sekaisync.event_detection import save_archive
        from sekaisync.layout import events_archive_path

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            jp = root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
            jp.mkdir(parents=True, exist_ok=True)
            _write(jp, "events.json", [
                {"id": 1, "eventType": "marathon", "name": "One", "startAt": 1000},
                {"id": 2, "eventType": "marathon", "name": "Two", "startAt": 2000},
            ])
            _write(jp, "eventCards.json", [{"eventId": 1, "cardId": 10}])
            _write(jp, "eventMusics.json", [{"eventId": 1, "musicId": 20}])
            _write(jp, "eventStories.json", [{"eventId": 1, "id": 30}])

            # Old-format archive: no sequence_no / placeholder fields.
            save_archive(
                root,
                {
                    "version": 1,
                    "regions": {
                        "jp": {
                            "events": [
                                {"event_id": 1, "name": "One", "start_at": 1000},
                                {"event_id": 2, "name": "Two", "start_at": 2000},
                            ]
                        }
                    },
                },
            )

            result = list_events(root, regions=["jp"])
            events = result["regions"]["jp"]
            by_id = {e["event_id"]: e for e in events}
            # Event 2 is a placeholder (no cards/music/story); event 1 is real.
            self.assertEqual(by_id[1]["sequence_no"], 1)
            self.assertTrue(by_id[2]["placeholder"])
            self.assertIsNone(by_id[2]["sequence_no"])
            self.assertEqual(result["total"], 2)


class RelationRowIdentityTest(unittest.TestCase):
    """Astra P16/D16 — relation tables must keep every row.

    `eventMusics.json` has NO `id` field in the real store (137/137 rows), so
    keying relation rows by `str(record.get("id"))` collapsed all of them to the
    single key "None" and only the first survived. Every later event lost its
    dedicated song, which also corrupted box-event classification.
    """

    def test_id_less_relation_rows_are_all_kept(self):
        """Astra: 无 id 三关系输入保留三条."""
        rows = [
            {"eventId": 1, "musicId": 10, "seq": 1},
            {"eventId": 1, "musicId": 11, "seq": 1},
            {"eventId": 1, "musicId": 12, "seq": 1},
        ]
        merged = _merge_event_rows([], rows, {"1"}, table="eventMusics")
        self.assertEqual(len(merged), 3)
        self.assertEqual(
            sorted(r["musicId"] for r in merged), [10, 11, 12]
        )

    def test_repeated_merge_does_not_duplicate(self):
        """Astra: 重复运行不增加."""
        rows = [
            {"eventId": 1, "musicId": 10, "seq": 1},
            {"eventId": 1, "musicId": 11, "seq": 1},
        ]
        once = _merge_event_rows([], rows, {"1"}, table="eventMusics")
        twice = _merge_event_rows(once, rows, {"1"}, table="eventMusics")
        self.assertEqual(len(twice), 2)

    def test_seq_is_not_part_of_identity(self):
        """seq is ordering, not identity: a new seq must not add a duplicate."""
        first = [{"eventId": 1, "musicId": 10, "seq": 1}]
        second = [{"eventId": 1, "musicId": 10, "seq": 2}]
        merged = _merge_event_rows(first, second, {"1"}, table="eventMusics")
        self.assertEqual(len(merged), 1)

    def test_updating_one_event_does_not_touch_others(self):
        """Astra: 更新一活动不伤其他活动."""
        existing = [
            {"eventId": 1, "musicId": 10, "seq": 1},
            {"eventId": 2, "musicId": 20, "seq": 1},
        ]
        incoming = [{"eventId": 1, "musicId": 11, "seq": 1}]
        merged = _merge_event_rows(existing, incoming, {"1"}, table="eventMusics")
        by_event = {}
        for row in merged:
            by_event.setdefault(row["eventId"], []).append(row["musicId"])
        self.assertEqual(sorted(by_event[1]), [10, 11])
        self.assertEqual(by_event[2], [20])

    def test_identical_rows_still_deduplicate(self):
        """Duplicate whole rows collapse, per '去掉完整行相同的重复'."""
        row = {"eventId": 1, "musicId": 10, "seq": 1}
        merged = _merge_event_rows([row], [dict(row)], {"1"}, table="eventMusics")
        self.assertEqual(len(merged), 1)

    def test_unkeyed_rows_are_not_dropped_by_table_merge(self):
        """The `if key:` guard used to discard every id-less row outright."""
        rows = [{"musicId": 1}, {"musicId": 2}, {"musicId": 3}]
        merged = _merge_table_records([], rows)
        self.assertEqual(len(merged), 3)

    def test_distinct_unkeyed_rows_get_distinct_identities(self):
        a = {"foo": 1}
        b = {"foo": 2}
        self.assertNotEqual(_row_identity("", a), _row_identity("", b))

    def test_real_event_musics_shape_keeps_every_row(self):
        """Regression fixture matching the real 137-row `eventMusics.json`."""
        rows = [
            {"eventId": i, "musicId": 60 + i, "seq": 1, "releaseConditionId": 1}
            for i in range(1, 138)
        ]
        merged = _merge_event_rows([], rows, {str(i) for i in range(1, 138)},
                                   table="eventMusics")
        self.assertEqual(len(merged), 137)


if __name__ == "__main__":
    unittest.main()
