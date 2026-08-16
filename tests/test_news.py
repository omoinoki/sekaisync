import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

from sekaisync.config import MoesekaiSettings, SiteSettings, ViewerSettings
from sekaisync.news import load_news, merge_news, news_available, news_summary, sync_news
from sekaisync.sources import BACKEND_MOESEKAI, BACKEND_SEKAI_VIEWER, SOURCE_MS, SOURCE_SV


def _upstream_sites() -> tuple[SiteSettings, ...]:
    """Real-endpoint profile used by the mock-fetcher tests (sv first)."""
    return (
        SiteSettings(
            id=SOURCE_SV,
            backend=BACKEND_SEKAI_VIEWER,
            viewer=ViewerSettings(master_base="https://sekai-world.github.io"),
        ),
        SiteSettings(
            id=SOURCE_MS,
            backend=BACKEND_MOESEKAI,
            moesekai=MoesekaiSettings(news_base="https://baijing.exmeaning.com"),
        ),
    )


class NewsTest(unittest.TestCase):
    def test_all_languages_prefer_altsource_sv(self):
        cn_title = "\u91cd\u590d\u516c\u544a"
        jp_title = "\u91cd\u8907\u304a\u77e5\u3089\u305b"
        merged = merge_news(
            [
                {
                    "canonical_key": f"zh_hans:{cn_title}",
                    "language": "zh_hans",
                    "source": "altsource_ms",
                    "text": "短",
                },
                {
                    "canonical_key": f"zh_hans:{cn_title}",
                    "language": "zh_hans",
                    "source": "altsource_sv",
                    "text": "更长的正文内容",
                },
                {
                    "canonical_key": f"ja:{jp_title}",
                    "language": "ja",
                    "source": "altsource_ms",
                    "text": "短い",
                },
                {
                    "canonical_key": f"ja:{jp_title}",
                    "language": "ja",
                    "source": "altsource_sv",
                    "text": "より長い本文内容",
                },
            ]
        )
        by_language = {record["language"]: record for record in merged}
        self.assertEqual(by_language["zh_hans"]["source"], "altsource_sv")
        self.assertEqual(by_language["ja"]["source"], "altsource_sv")

    def test_sync_news_merges_by_language_without_tos(self):
        def fake_fetch(url: str) -> str:
            if "baijing.exmeaning.com/cn/" in url:
                return json.dumps(
                    {
                        "informations": [
                            {
                                "id": 1,
                                "title": "国服公告",
                                "path": "https://example.com/cn",
                                "startAt": 1000,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if "baijing.exmeaning.com/jp/" in url:
                return json.dumps(
                    {
                        "informations": [
                            {
                                "id": 4,
                                "title": "お知らせ",
                                "path": "https://example.com/jp",
                                "startAt": 1000,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if "sekai-master-db-diff/userInformations.json" in url:
                return json.dumps(
                    [
                        {
                            "id": 99,
                            "title": "ゲームニュース",
                            "path": "https://pjsekai.sega.jp",
                            "startAt": 1000,
                        }
                    ],
                    ensure_ascii=False,
                )
            if "sekai-world.github.io" in url:
                return "[]"
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = sync_news(
                store_root,
                regions=("cn", "jp"),
                sources=("altsource_ms", "altsource_sv"),
                fetcher=fake_fetch,
                sites=_upstream_sites(),
            )
            self.assertEqual(result["merged"], 3)
            self.assertTrue(news_available(store_root))

            records = load_news(store_root)
            by_language = defaultdict(list)
            for record in records:
                by_language[record["language"]].append(record)
            self.assertEqual(len(by_language["zh_hans"]), 1)
            self.assertEqual(len(by_language["ja"]), 2)
            self.assertTrue(
                any(
                    r["language"] == "ja" and r["source"] == "altsource_sv"
                    for r in records
                )
            )

            summary = news_summary(store_root)
            self.assertEqual(summary["languages"]["zh_hans"], 1)
            self.assertEqual(summary["sources"]["altsource_ms"], 2)
            self.assertEqual(summary["sources"]["altsource_sv"], 1)

    def test_zh_hans_prefers_altsource_sv_for_same_announcement(self):
        cn_title = "\u56fd\u670d\u516c\u544a"

        def fake_fetch(url: str) -> str:
            if "baijing.exmeaning.com/cn/" in url:
                return json.dumps(
                    {
                        "informations": [
                            {
                                "id": 1,
                                "title": cn_title,
                                "path": "https://example.com/cn",
                                "startAt": 1000,
                            }
                        ]
                    },
                    ensure_ascii=False,
                )
            if "sekai-master-db-cn-diff/userInformations.json" in url:
                return json.dumps(
                    [
                        {
                            "id": 10,
                            "title": cn_title,
                            "path": "https://pjsekai.sega.jp",
                            "startAt": 1000,
                        }
                    ],
                    ensure_ascii=False,
                )
            if "sekai-world.github.io" in url:
                return "[]"
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            sync_news(
                store_root,
                regions=("cn",),
                sources=("altsource_ms", "altsource_sv"),
                fetcher=fake_fetch,
                sites=_upstream_sites(),
                source_priority=(SOURCE_SV, SOURCE_MS),
            )
            records = load_news(store_root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source"], "altsource_sv")


    def test_website_announcements_are_excluded_and_purged(self):
        from sekaisync.news import save_news

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            polluted = {
                "id": "news:en:altsource_sv:1",
                "source": "altsource_sv",
                "source_id": "1",
                "language": "en",
                "title": "Sekai Viewer Release",
                "text": "Site announcement",
                "url": "https://strapi.sekai.best/announcements/1",
                "start_at": None,
                "end_at": None,
                "published_at": None,
                "canonical_key": "en:sekai viewer release",
                "kind": "game_news",
                "trust": "B",
            }
            normal = {
                "id": "news:ja:altsource_sv:99",
                "source": "altsource_sv",
                "source_id": "99",
                "language": "ja",
                "title": "\u30b2\u30fc\u30e0\u30cb\u30e5\u30fc\u30b9",
                "text": "Official game news",
                "url": "https://pjsekai.sega.jp",
                "start_at": None,
                "end_at": None,
                "published_at": None,
                "canonical_key": "ja:\u30b2\u30fc\u30e0\u30cb\u30e5\u30fc\u30b9",
                "kind": "game_news",
                "trust": "B",
            }
            save_news([polluted, normal], store_root)
            records = load_news(store_root)
            self.assertEqual([record["id"] for record in records], [normal["id"]])

            sync_news(
                store_root,
                regions=(),
                sources=(),
                fetcher=lambda url: "[]",
            )
            remaining = load_news(store_root)
            self.assertEqual([record["id"] for record in remaining], [normal["id"]])


    def test_source_priority_follows_passed_order(self):
        from sekaisync.sources import SOURCE_MS, SOURCE_SV

        records = [
            {
                "canonical_key": "zh_hans:priority",
                "language": "zh_hans",
                "source": SOURCE_SV,
                "text": "sv text",
            },
            {
                "canonical_key": "zh_hans:priority",
                "language": "zh_hans",
                "source": SOURCE_MS,
                "text": "ms text",
            },
        ]
        merged = merge_news(records, source_priority=(SOURCE_MS, SOURCE_SV))
        self.assertEqual(merged[0]["source"], SOURCE_MS)
        merged = merge_news(records, source_priority=(SOURCE_SV, SOURCE_MS))
        self.assertEqual(merged[0]["source"], SOURCE_SV)

    def test_legacy_sources_rank_via_priority(self):
        from sekaisync.sources import SOURCE_MS, SOURCE_SV

        records = [
            {
                "canonical_key": "ja:legacy",
                "language": "ja",
                "source": "sekai_viewer",  # legacy alias for altsource_sv
                "text": "legacy sv",
            },
            {
                "canonical_key": "ja:legacy",
                "language": "ja",
                "source": SOURCE_MS,
                "text": "ms",
            },
        ]
        merged = merge_news(records, source_priority=(SOURCE_MS, SOURCE_SV))
        self.assertEqual(merged[0]["source"], SOURCE_MS)

    def test_sync_news_uses_per_instance_endpoints_and_priority(self):
        from sekaisync.config import SiteSettings, ViewerSettings
        from sekaisync.sources import BACKEND_SEKAI_VIEWER

        sites = [
            SiteSettings(
                id="altsource_sv",
                backend=BACKEND_SEKAI_VIEWER,
                name="Sekai Viewer",
                viewer=ViewerSettings(master_base="https://sv1.example"),
            ),
            SiteSettings(
                id="altsource_sv_local",
                backend=BACKEND_SEKAI_VIEWER,
                name="Self-hosted viewer",
                viewer=ViewerSettings(master_base="https://sv2.example"),
            ),
        ]

        def fake_fetch(url: str) -> str:
            if url.startswith("https://sv1.example"):
                return json.dumps(
                    [{"id": 1, "title": "同一公告", "path": "https://pjsekai.sega.jp", "startAt": 1000}],
                    ensure_ascii=False,
                )
            if url.startswith("https://sv2.example"):
                return json.dumps(
                    [{"id": 2, "title": "同一公告", "path": "https://pjsekai.sega.jp", "startAt": 1000}],
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = sync_news(
                store_root,
                regions=("jp",),
                sources=("altsource_sv", "altsource_sv_local"),
                fetcher=fake_fetch,
                sites=sites,
            )
            # Both instances were fetched; the same announcement merged once,
            # and the earlier (higher-priority) instance won.
            self.assertEqual(result["fetched"], 2)
            self.assertEqual(result["merged"], 1)
            records = load_news(store_root)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["source"], "altsource_sv")
            self.assertEqual(records[0]["source_type"], BACKEND_SEKAI_VIEWER)
            summary = news_summary(store_root)
            self.assertIn("altsource_sv", summary["sources"])

            # Reversing priority makes the second instance win.
            result2 = sync_news(
                store_root,
                regions=("jp",),
                sources=("altsource_sv", "altsource_sv_local"),
                fetcher=fake_fetch,
                sites=sites,
                source_priority=("altsource_sv_local", "altsource_sv"),
            )
            self.assertEqual(result2["merged"], 1)
            self.assertEqual(load_news(store_root)[0]["source"], "altsource_sv_local")

    def test_sync_news_type_selector_expands_to_all_instances(self):
        from sekaisync.config import SiteSettings, ViewerSettings
        from sekaisync.sources import BACKEND_SEKAI_VIEWER

        sites = [
            SiteSettings(
                id="altsource_sv",
                backend=BACKEND_SEKAI_VIEWER,
                viewer=ViewerSettings(master_base="https://sv1.example"),
            ),
            SiteSettings(
                id="altsource_sv_local",
                backend=BACKEND_SEKAI_VIEWER,
                viewer=ViewerSettings(master_base="https://sv2.example"),
            ),
        ]

        def fake_fetch(url: str) -> str:
            if url.startswith("https://sv1.example"):
                return json.dumps(
                    [{"id": 1, "title": "公告甲", "path": "https://pjsekai.sega.jp", "startAt": 1000}],
                    ensure_ascii=False,
                )
            if url.startswith("https://sv2.example"):
                return json.dumps(
                    [{"id": 2, "title": "公告乙", "path": "https://pjsekai.sega.jp", "startAt": 1000}],
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            sync_news(
                store_root,
                regions=("jp",),
                sources=("altsource_sv",),  # type selector
                fetcher=fake_fetch,
                sites=sites,
            )
            records = load_news(store_root)
            self.assertEqual({r["source"] for r in records}, {"altsource_sv", "altsource_sv_local"})


if __name__ == "__main__":
    unittest.main()
