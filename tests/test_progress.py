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
        for table in (
            "musics", "gachas", "virtualLives", "areas", "stamps",
            "gameCharacters", "gameCharacterUnits", "specialStories", "actionSets",
            "characterProfiles", "characterArchiveVoices", "mysekaiCharacterTalks",
            "mysekaiCharacterTalkTweets",
        ):
            (source / f"{table}.json").write_text("[]", encoding="utf-8")
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
            self.assertEqual(region["text"]["expected_total"], 7)
            self.assertEqual(region["text"]["matched_total"], 2)
            self.assertEqual(region["text"]["pct"], 29)
            self.assertEqual(region["overall"]["pct"], 44)
            self.assertGreaterEqual(region["excluded_units"]["fact_card"], 2)
            self.assertIn("collab", result["caveat"])

            fact_expected = expected_fact_units(store_root, "jp", 5000)
            self.assertNotIn("card:99", fact_expected["card"])
            expected = expected_text_units(store_root, "jp", 5000)
            self.assertNotIn("event_story:ja:2:1", expected["event_story"])
            self.assertIn("unit_story:ja:ln_01_00", expected["unit_story"])
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


class ProgressCommandTest(unittest.TestCase):
    """The CLI must actually produce progress.json.

    ``cmd_progress`` passed ``master_base`` unconditionally, which sent every
    run down ``Core.progress``'s live branch — a branch that needs a
    RuntimeContext the command never built.  The result was a hard failure on
    plain ``progress``, so the file the desktop reads was never written.
    """

    def test_plain_progress_writes_a_snapshot(self):
        import argparse
        from unittest.mock import patch
        from sekaisync import cli
        from sekaisync.config import SekaiSyncConfig
        from sekaisync.layout import progress_path

        tmp = tempfile.TemporaryDirectory(prefix="test_progress_cli_")
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "store"
        ProgressTest()._write_store(store)

        # A real configuration carries a master_base (it comes from
        # settings.json), which is exactly what used to push the command into
        # the networked branch.  Leaving it empty here would let the bug back in.
        from sekaisync.config import SiteSettings, ViewerSettings
        config = SekaiSyncConfig(
            store_root=store,
            sites=(SiteSettings(
                id="altsource_sv", backend="sekai_viewer", name="Sekai Viewer",
                enabled=True,
                viewer=ViewerSettings(master_base="https://sekai-world.github.io"),
            ),),
        )
        args = argparse.Namespace(store=str(store), regions=None, live=False, plain=False)
        with patch.object(cli, "config_from_args", return_value=config):
            code = cli.cmd_progress(args)
        self.assertEqual(code, 0)
        path = progress_path(store)
        self.assertTrue(path.exists(), "progress.json was not written by the command")
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        self.assertIn("overall", snapshot)


class SourceUnavailableExclusionTest(unittest.TestCase):
    """Units the publisher is confirmed not to provide leave the denominator.

    ``characterArchiveVoices`` ships rows whose ``displayPhrase`` is empty
    (~53% of the real jp table) and overseas regions have no mysekai/talk Lua
    at all.  Counting those as expected units made a store report ~70% for
    text that cannot be fetched; they are excluded and reported separately so
    the exclusion stays auditable.
    """

    def _store(self, tmp: str, region: str, voices: list[dict],
               talks: list[dict]) -> Path:
        store = Path(tmp) / "store"
        source = store / "raw" / region / "source"
        source.mkdir(parents=True, exist_ok=True)
        for name, payload in (
            ("characterArchiveVoices", voices),
            ("mysekaiCharacterTalks", talks),
        ):
            (source / f"{name}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        for table in (
            "events", "eventStories", "cardEpisodes", "cards", "musics", "gachas",
            "virtualLives", "areas", "stamps", "gameCharacters", "gameCharacterUnits",
            "specialStories", "actionSets", "characterProfiles",
            "mysekaiCharacterTalkTweets",
        ):
            (source / f"{table}.json").write_text("[]", encoding="utf-8")
        return store

    def test_empty_display_phrase_leaves_the_denominator(self):
        with tempfile.TemporaryDirectory(prefix="test_srcunavail_") as tmp:
            store = self._store(tmp, "jp", [
                {"id": 1, "displayPhrase": "こんにちは"},
                {"id": 2, "displayPhrase": ""},
                {"id": 3, "displayPhrase": "   "},
                {"id": 4},
            ], [])
            result = compute_progress(store, regions=["jp"])
            home = result["regions"]["jp"]["text"]["categories"]["home_line"]
            self.assertEqual(home["expected"], 1, "only rows carrying text are expected")
            self.assertEqual(result["source_unavailable"]["total_units"], 3)
            self.assertEqual(
                result["source_unavailable"]["by_region"]["jp"]["text_home_line"], 3)

    def test_overseas_mysekai_talk_is_excluded_but_jp_is_not(self):
        talks = [{"id": 10}, {"id": 11}]
        with tempfile.TemporaryDirectory(prefix="test_srcunavail_en_") as tmp:
            store = self._store(tmp, "en", [], talks)
            result = compute_progress(store, regions=["en"])
            cat = result["regions"]["en"]["text"]["categories"]["mysekai_talk"]
            self.assertEqual(cat["expected"], 0, "en has no mysekai/talk Lua upstream")
            self.assertEqual(
                result["source_unavailable"]["by_region"]["en"]["text_mysekai_talk"], 2)
        with tempfile.TemporaryDirectory(prefix="test_srcunavail_jp_") as tmp:
            store = self._store(tmp, "jp", [], talks)
            result = compute_progress(store, regions=["jp"])
            cat = result["regions"]["jp"]["text"]["categories"]["mysekai_talk"]
            self.assertEqual(cat["expected"], 2, "jp does publish MySekai dialogue")
            self.assertNotIn("text_mysekai_talk",
                             result["source_unavailable"]["by_region"].get("jp", {}))

    def test_explanation_block_is_auditable(self):
        with tempfile.TemporaryDirectory(prefix="test_srcunavail_doc_") as tmp:
            store = self._store(tmp, "jp", [{"id": 1, "displayPhrase": ""}], [])
            result = compute_progress(store, regions=["jp"])
            block = result["source_unavailable"]
            self.assertIn("note", block)
            self.assertIn("text_home_line", block["reasons"])
            self.assertIn("text_mysekai_talk", block["reasons"])


if __name__ == "__main__":
    unittest.main()
