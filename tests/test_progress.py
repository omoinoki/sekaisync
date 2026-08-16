import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.models import WebPage
from sekaisync.progress import (
    _web_text_key,
    compute_progress,
    expected_fact_units,
    expected_text_units,
    matched_text_units,
)
from sekaisync.registry import build_registry, save_registry
from sekaisync.webindex import save_web_pages


class ProgressTest(unittest.TestCase):
    def _write_store(self, store_root: Path) -> None:
        source = store_root / "raw" / "jp" / "source"
        source.mkdir(parents=True, exist_ok=True)
        (source / "events.json").write_text(
            json.dumps(
                [
                    {"id": 1, "name": "过去活动", "startAt": 1000, "closedAt": 9000},
                    {"id": 2, "name": "未来活动", "startAt": 9999999999999},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (source / "cards.json").write_text(
            json.dumps(
                [
                    {"id": 1, "releaseAt": 1000},
                    {"id": 2, "releaseAt": 9999999999999},
                    {"id": 99},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (source / "eventStories.json").write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "eventId": 1,
                        "eventStoryEpisodes": [
                            {"episodeNo": 1, "scenarioId": "event_01_01"},
                            {"episodeNo": 2, "scenarioId": "event_01_02"},
                        ],
                    },
                    {
                        "id": 2,
                        "eventId": 2,
                        "eventStoryEpisodes": [
                            {"episodeNo": 1, "scenarioId": "event_02_01"}
                        ],
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (source / "cardEpisodes.json").write_text(
            json.dumps(
                [
                    {"id": 1, "cardId": 1, "scenarioId": "card_01_01"},
                    {"id": 2, "cardId": 1, "scenarioId": "card_01_02"},
                    {"id": 3, "cardId": 2, "scenarioId": "card_02_01"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (source / "unitStories.json").write_text(
            json.dumps(
                [
                    {
                        "unit": "light_sound",
                        "chapters": [
                            {
                                "episodes": [
                                    {"scenarioId": "ln_01_00", "episodeNo": 1, "episodeNoLabel": "オープニング"},
                                    {"scenarioId": "ln_01_01"},
                                    {"scenarioId": "ln_01_02"},
                                ]
                            }
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        entities = build_registry(store_root, ["jp"])
        save_registry(entities, store_root / "kb" / "registry.json")

    def _write_web(self, store_root: Path) -> None:
        save_web_pages(
            store_root,
            "altsource_ms",
            [
                WebPage(
                    id="web:altsource_ms:event_story:1:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/ja-jp/story/event/1/1/",
                    title="活动1-1",
                    language="ja",
                    kind="event_story",
                    text="正文",
                    crawled_at="2026-01-01T00:00:00+00:00",
                    hash="a",
                ),
                WebPage(
                    id="web:altsource_ms:event_story:2:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/ja-jp/story/event/2/1/",
                    title="未来活动",
                    language="ja",
                    kind="event_story",
                    text="超前内容",
                    crawled_at="2026-01-01T00:00:00+00:00",
                    hash="b",
                ),
                WebPage(
                    id="web:altsource_ms:unit_story:ln_01_01",
                    source="altsource_ms",
                    url="https://pjsk.moe/ja-jp/story/unit/1/ln_01_01/",
                    title="主线",
                    language="ja",
                    kind="unit_story",
                    text="主线正文",
                    crawled_at="2026-01-01T00:00:00+00:00",
                    hash="c",
                ),
            ],
        )

    def test_future_content_is_excluded_and_percentages_are_integers(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_store(store_root)
            self._write_web(store_root)

            result = compute_progress(store_root, regions=["jp"], now=5000)
            region = result["regions"]["jp"]
            self.assertEqual(region["fact"]["pct"], 100)
            self.assertEqual(region["text"]["expected_total"], 6)
            self.assertEqual(region["text"]["matched_total"], 2)
            self.assertEqual(region["text"]["pct"], 33)
            self.assertEqual(region["overall"]["pct"], 50)
            self.assertGreaterEqual(region["excluded_units"]["fact_card"], 2)
            self.assertIn("collab", result["caveat"])

            fact_expected = expected_fact_units(store_root, "jp", 5000)
            self.assertNotIn("card:99", fact_expected["card"])
            expected = expected_text_units(store_root, "jp", 5000)
            self.assertNotIn("event_story:ja:2:1", expected["event_story"])
            self.assertNotIn("unit_story:ja:ln_01_00", expected["unit_story"])
            matched = matched_text_units(store_root, "jp", expected)
            self.assertNotIn("event_story:ja:2:1", matched["event_story"])

    def test_flagged_pages_are_excluded_from_matched_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_store(store_root)
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:ja-jp:event_story:1:1",
                        source="altsource_ms",
                        url="https://pjsk.moe/ja-jp/story/event/1/1/",
                        title="活動1-1",
                        language="ja",
                        kind="event_story",
                        text="正しい本文",
                        crawled_at="2026-01-01T00:00:00+00:00",
                        hash="a",
                    ),
                    WebPage(
                        id="web:altsource_ms:ja-jp:event_story:1:2",
                        source="altsource_ms",
                        url="https://pjsk.moe/ja-jp/story/event/1/2/",
                        title="活動1-2",
                        language="ja",
                        kind="event_story",
                        text="齋藤：繁體中文正文",
                        crawled_at="2026-01-01T00:00:00+00:00",
                        hash="b",
                        asset_mismatch="language_mismatch: expected ja, text script mismatch",
                        content_language_mismatch=True,
                    ),
                    WebPage(
                        id="web:altsource_ms:ja-jp:unit_story:ln_01_01",
                        source="altsource_ms",
                        url="https://pjsk.moe/ja-jp/story/unit/1/ln_01_01/",
                        title="主線",
                        language="ja",
                        kind="unit_story",
                        text="主線正文",
                        crawled_at="2026-01-01T00:00:00+00:00",
                        hash="c",
                    ),
                ],
            )
            expected = expected_text_units(store_root, "jp", 5000)
            matched = matched_text_units(store_root, "jp", expected)
            self.assertEqual(matched["event_story"], {"event_story:ja:1:1"})
            self.assertEqual(matched["unit_story"], {"unit_story:ja:ln_01_01"})

    def test_virtual_live_and_special_story_expected_use_crawled_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            source = store_root / "raw" / "jp" / "source"
            source.mkdir(parents=True, exist_ok=True)
            (source / "virtualLives.json").write_text(
                json.dumps(
                    [
                        {
                            "id": 10,
                            "startAt": 1000,
                            "virtualLiveSetlists": [
                                {"id": 101, "virtualLiveSetlistType": "mc", "assetbundleName": "mc_10_1"},
                                {"id": 102, "virtualLiveSetlistType": "music", "assetbundleName": "m_10"},
                                {"id": 103, "virtualLiveSetlistType": "mc_timeline", "assetbundleName": "tl_10_1"},
                            ],
                        },
                        {
                            "id": 11,
                            "startAt": 9999999999999,
                            "virtualLiveSetlists": [
                                {"id": 111, "virtualLiveSetlistType": "mc", "assetbundleName": "mc_11_1"}
                            ],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (source / "specialStories.json").write_text(
                json.dumps(
                    [
                        {
                            "id": 5,
                            "startAt": 1000,
                            "episodes": [
                                {"id": 501, "scenarioId": "special_05_01"},
                                {"id": 502, "scenarioId": "special_05_02"},
                            ],
                        },
                        {
                            "id": 6,
                            "startAt": 9999999999999,
                            "episodes": [{"id": 601, "scenarioId": "special_06_01"}],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            expected = expected_text_units(store_root, "jp", 5000)
            self.assertEqual(
                expected["virtual_live"],
                {"virtual_live:ja:101", "virtual_live:ja:103"},
            )
            self.assertEqual(
                expected["special_story"],
                {"special_story:ja:501", "special_story:ja:502"},
            )

    def test_web_key_uses_normalized_kind(self):
        self.assertIsNone(
            _web_text_key(
                {
                    "id": "web:altsource_ms:virtualLives:1",
                    "kind": "virtualLives",
                    "url": "https://metadata.exmeaning.com/cn/master/virtualLives",
                }
            )
        )
        self.assertEqual(
            _web_text_key(
                {
                    "id": "web:altsource_ms:virtual_live:4",
                    "kind": "virtual_live",
                    "language": "ja",
                    "url": "https://pjsk.moe/ja-jp/virtual_live/1",
                }
            ),
            "virtual_live:ja:4",
        )
        self.assertEqual(
            _web_text_key(
                {
                    "id": "web:altsource_ms:event_story:1:1",
                    "kind": "event_story",
                    "language": "ja",
                    "url": "https://pjsk.moe/ja-jp/story/event/1/1/",
                }
            ),
            "event_story:ja:1:1",
        )


if __name__ == "__main__":
    unittest.main()
