import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import sekaisync.crawler as crawler_mod
from sekaisync.crawler import (
    _cache_bust_url,
    _crawl_altsource_ms_special_stories,
    crawl_altsource_ms_site,
    crawl_altsource_ms,
    crawl_altsource_sv,
    fetch_altsource_ms_scenario,
    fetch_altsource_sv_asset,
    altsource_ms_page_id,
    altsource_ms_scenario_page,
    altsource_ms_story_detail_page,
    mysekai_lua_to_text,
    require_tos_consent,
    altsource_sv_scenario_page,
    apply_source_settings,
    altsource_ms_canonical_url,
    altsource_sv_master_json_url,
    write_consent,
)
from sekaisync.config import MoesekaiSettings, ViewerSettings
from sekaisync.models import WebPage
from sekaisync.webindex import (
    WEB_CATEGORY_FILES,
    canonical_key_for_page,
    load_existing_page_ids,
    load_existing_page_map,
    load_web_category_counts,
    load_web_index,
    load_web_pages,
    normalize_scenario_mismatch_flag,
    rebuild_web_index,
    save_web_pages,
    text_matches_language,
    web_browse,
    web_search,
)


class FakeStdin:
    def isatty(self) -> bool:
        return False


class WebCrawlerTest(unittest.TestCase):
    def setUp(self):
        # Tests assert against the upstream hostnames with mocked fetchers, so
        # apply the real endpoints; production defaults are empty and require
        # explicit settings.json configuration.
        apply_source_settings(
            moesekai=MoesekaiSettings(
                site_base="https://pjsk.moe",
                sitemap_url="https://pjsk.moe/sitemap.xml",
                story_detail_base="https://moe.exmeaning.com/story/detail",
                metadata_bases=("https://metadata.exmeaning.com", "https://metadata.pjsk.moe"),
                asset_bases=("https://storage.exmeaning.com", "https://storage.pjsk.moe"),
                translation_base="https://translation.exmeaning.com/translation",
                news_base="https://baijing.exmeaning.com",
            ),
            viewer=ViewerSettings(
                master_base="https://sekai-world.github.io",
                asset_base="https://storage.sekai.best",
                i18n_base="https://i18n-json.sekai.best",
            ),
        )

    def tearDown(self):
        apply_source_settings(moesekai=MoesekaiSettings(), viewer=ViewerSettings())

    def test_cache_bust_url_appends_version_query(self):
        url = _cache_bust_url("https://storage.exmeaning.com/sekai-cn-assets/a.json")
        self.assertIn("?v=", url)
        second = _cache_bust_url("https://storage.exmeaning.com/a.json?x=1")
        self.assertIn("&v=", second)

    def test_altsource_aigc_story_detail_is_derived_and_hidden_from_search(self):
        page = altsource_ms_story_detail_page(
            {
                "title_cn": "测试活动",
                "outline_cn": "AIGC 梗概",
                "summary_cn": "AIGC 章节概述",
            },
            1,
        )
        self.assertTrue(page.derived)
        self.assertEqual(page.kind, "story")
        self.assertEqual(page.trust, "C")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:story:1",
                        source="altsource_ms",
                        url="https://pjsk.moe/zh-cn/story/event/1/",
                        title="章节概述",
                        language="zh_hans",
                        kind="story",
                        text="AIGC 章节概述内容",
                        crawled_at="2026-08-11T00:00:00+00:00",
                        hash="aigc",
                        derived=False,
                    )
                ],
            )
            self.assertEqual(
                load_web_category_counts(store_root)["altsource_ms"]["other"],
                1,
            )
            self.assertEqual(web_search(store_root, "章节概述"), [])
            self.assertEqual(web_browse(store_root), [])

    def test_altsource_sv_asset_marks_scenario_mismatch(self):
        def fake_fetch(url: str) -> str:
            return json.dumps({"ScenarioId": "event_173_01", "m_Name": "event_174_01"})

        url, data = fetch_altsource_sv_asset(
            "cn",
            ["event_story/event_crossing_2025/scenario/event_174_01.asset"],
            fake_fetch,
        )
        self.assertEqual(data["__assetMismatch"], "")
        self.assertIn(
            "ScenarioId event_173_01 != expected event_174_01",
            data["__scenarioIdMismatch"],
        )

    def test_tos_consent_is_required(self):
        with patch("sekaisync.crawler.sys.stdin", FakeStdin()):
            with self.assertRaises(ValueError):
                require_tos_consent(False)

    def test_altsource_crawl_uses_sitemap_index_and_stores_text(self):
        sitemap = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://pjsk.moe/sitemap-details/zh-cn.xml</loc></sitemap>
</sitemapindex>"""
        detail = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://pjsk.moe/zh-cn/cards/1/</loc></url>
</urlset>"""
        html = """<html><head>
<title>Test Card | Moesekai</title>
<meta name="description" content="A text description about the card.">
</head><body><main><h1>Test Card</h1><p>Some visible card text.</p></main></body></html>"""

        def fake_fetch(url: str) -> str:
            if url == crawler_mod.ALTSOURCE_MS_SITEMAP:
                return sitemap
            if url.endswith("sitemap-details/zh-cn.xml"):
                return detail
            if url.endswith("cards/1/"):
                return html
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_ms_site(
                store_root,
                locales=("zh-cn",),
                limit=2,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
            )
            self.assertEqual(result["crawled_pages"], 1)
            pages = load_web_pages(store_root)["altsource_ms"]
            self.assertIn("Some visible card text.", pages[0]["text"])
            self.assertTrue((store_root / "kb" / "web" / "consent.json").exists())

    def test_altsource_crawl_prefers_story_and_card_detail_pages(self):
        sitemap = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://pjsk.moe/sitemap-details/zh-cn.xml</loc></sitemap>
  <sitemap><loc>https://pjsk.moe/sitemap-main.xml</loc></sitemap>
</sitemapindex>"""
        detail = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://pjsk.moe/zh-cn/</loc></url>
  <url><loc>https://pjsk.moe/zh-cn/cards/</loc></url>
  <url><loc>https://pjsk.moe/zh-cn/story/event/</loc></url>
  <url><loc>https://pjsk.moe/zh-cn/story/special/</loc></url>
  <url><loc>https://pjsk.moe/zh-cn/story/event/167/</loc></url>
  <url><loc>https://pjsk.moe/zh-cn/cards/794/</loc></url>
</urlset>"""
        html = """<html><head><title>Page | Moesekai</title></head><body><main><p>Visible text.</p></main></body></html>"""

        def fake_fetch(url: str) -> str:
            if url == crawler_mod.ALTSOURCE_MS_SITEMAP:
                return sitemap
            if url.endswith("sitemap-main.xml"):
                raise AssertionError("Main sitemap should not be fetched when a locale detail sitemap exists")
            if url.endswith("sitemap-details/zh-cn.xml"):
                return detail
            if url == "https://moe.exmeaning.com/story/detail/event_167.json":
                return json.dumps(
                    {
                        "event_id": 167,
                        "title_cn": "Dear my fellows",
                        "outline_cn": "Wonderlands×Showtime 完成修行后的故事。",
                        "summary_cn": "笑梦和家人伙伴继续追逐梦想。",
                        "chapters": [
                            {"chapter_no": 1, "title_cn": "成长的征兆？", "summary_cn": "第一章摘要。"}
                        ],
                    },
                    ensure_ascii=False,
                )
            if url.endswith("story/event/167/"):
                raise AssertionError("Event HTML should not be fetched when the story detail mirror exists")
            if url.endswith("cards/794/"):
                return html
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_ms_site(
                store_root,
                locales=("zh-cn",),
                limit=2,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
            )
            self.assertEqual(result["crawled_pages"], 2)
            pages = load_web_pages(store_root)["altsource_ms"]
            self.assertIn("/story/detail/event_167.json", pages[0]["url"])
            self.assertIn("笑梦和家人伙伴继续追逐梦想。", pages[0]["text"])
            self.assertIn("/cards/794/", pages[1]["url"])

    def test_altsource_sv_crawl_stores_json_text(self):
        def fake_fetch(url: str) -> str:
            self.assertNotIn("/Sekai-World/", url)
            if url.endswith("gameCharacters.json"):
                self.assertIn("/sekai-master-db-diff/gameCharacters.json", url)
                return json.dumps(
                    [
                        {
                            "id": 1,
                            "firstName": "Hoshino",
                            "lastName": "Ichika",
                            "profile": "A local text record.",
                        }
                    ]
                )
            return "{}"

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_sv(
                store_root,
                regions=("jp",),
                tables=("gameCharacters",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
            )
            self.assertEqual(result["crawled_pages"], 1)
            pages = load_web_pages(store_root)["altsource_sv"]
            kinds = {page["kind"] for page in pages}
            self.assertEqual(kinds, {"gameCharacters"})

    def test_altsource_sv_text_crawl_depth1_fetches_event_scenario_text(self):
        def fake_fetch(url: str) -> str:
            url = url.split("?", 1)[0]
            if url.endswith("eventStories.json"):
                return json.dumps(
                    [
                        {
                            "id": 1,
                            "eventId": 1,
                            "assetbundleName": "event_stella_2020",
                            "eventStoryEpisodes": [
                                {
                                    "episodeNo": 1,
                                    "title": "ひとりぼっちの雨模様",
                                    "scenarioId": "event_01_01",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                )
            if url.endswith("unitStories.json"):
                return "[]"
            if url.endswith("unitProfiles.json"):
                return "[]"
            if url == "https://storage.sekai.best/sekai-jp-assets/event_story/event_stella_2020/scenario/event_01_01.asset":
                return json.dumps(
                    {
                        "ScenarioId": "event_01_01",
                        "TalkData": [
                            {"WindowDisplayName": "咲希", "Body": "这是活动剧情正文。"}
                        ],
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_sv(
                store_root,
                regions=("jp",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
                depth=1,
                include_i18n=False,
            )
            self.assertEqual(result["crawled_pages"], 1)
            pages = load_web_pages(store_root)["altsource_sv"]
            self.assertEqual(pages[0]["kind"], "event_story")
            self.assertIn("这是活动剧情正文。", pages[0]["text"])
            self.assertTrue(pages[0]["url"].startswith("https://storage.sekai.best/"))
            self.assertEqual(pages[0]["trust"], "B")

    def test_altsource_sv_depth3_fetches_area_and_virtual_live_text(self):
        def fake_fetch(url: str) -> str:
            url = url.split("?", 1)[0]
            if url.endswith("eventStories.json") or url.endswith("unitStories.json"):
                return "[]"
            if url.endswith("cards.json") or url.endswith("cardEpisodes.json"):
                return "[]"
            if url.endswith("virtualLives.json"):
                return json.dumps(
                    [
                        {
                            "id": 1,
                            "name": "纪念LIVE",
                            "virtualLiveSetlists": [
                                {
                                    "id": 1,
                                    "seq": 1,
                                    "virtualLiveSetlistType": "mc",
                                    "assetbundleName": "mc_release_01_1",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                )
            if url.endswith("virtualLivePamphlets.json"):
                return "[]"
            if url.endswith("actionSets.json"):
                return json.dumps(
                    [
                        {
                            "id": 11,
                            "areaId": 2,
                            "scenarioId": "areatalk02_129",
                            "scriptId": "as_2_129",
                        }
                    ],
                    ensure_ascii=False,
                )
            if url.endswith("characterArchiveVoices.json") or url.endswith("systemLive2ds.json"):
                return "[]"
            if url == "https://storage.sekai.best/sekai-jp-assets/virtual_live/mc/scenario/mc_release_01_1/mc_release_01_1.asset":
                return json.dumps(
                    {
                        "characterTalkEvents": [
                            {"Character3dId": 21, "Serif": "欢迎来到舞台！"}
                        ]
                    },
                    ensure_ascii=False,
                )
            if url == "https://storage.sekai.best/sekai-jp-assets/scenario/actionset/group0/areatalk02_129.asset":
                return json.dumps(
                    {
                        "TalkData": [
                            {"WindowDisplayName": "彰人", "Body": "区域对话正文。"}
                        ]
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_sv(
                store_root,
                regions=("jp",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
                depth=3,
                include_i18n=False,
            )
            self.assertEqual(result["crawled_pages"], 2)
            pages = load_web_pages(store_root)["altsource_sv"]
            kinds = {page["kind"] for page in pages}
            self.assertEqual(kinds, {"virtual_live", "area_talk"})
            self.assertIn("欢迎来到舞台！", next(p["text"] for p in pages if p["kind"] == "virtual_live"))
            self.assertIn("区域对话正文。", next(p["text"] for p in pages if p["kind"] == "area_talk"))

    def test_mysekai_lua_parser_extracts_labeled_dialogue(self):
        lua = """-- シナリオID : mysekai_talk_release_001_0001
label("一歌")
voice("talk", "voice_mysekai_talk_release_001_0001_01_001", Characters.Ichika)
text("綺麗な色のチェストだね。\\n取っ手のところが星になってて、可愛いな")
wait_click()
"""
        self.assertEqual(
            mysekai_lua_to_text(lua),
            "一歌：綺麗な色のチェストだね。\n取っ手のところが星になってて、可愛いな",
        )

    def test_altsource_text_crawl_depth1_fetches_event_scenario_text(self):
        def fake_fetch(url: str) -> str:
            url = url.split("?", 1)[0]
            if url == "https://metadata.exmeaning.com/cn/master/eventStories.json":
                return json.dumps(
                    [
                        {
                            "id": 1,
                            "name": "雨过天晴的启明星",
                            "assetbundleName": "event_stella_2020",
                            "eventStoryEpisodes": [
                                {
                                    "episodeNo": 1,
                                    "title": "孤独的雨",
                                    "scenarioId": "event_01_01",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                )
            if url == "https://metadata.exmeaning.com/cn/master/unitProfiles.json":
                return "[]"
            if url == "https://metadata.exmeaning.com/cn/master/unitStories.json":
                return "[]"
            if url == "https://storage.exmeaning.com/sekai-cn-assets/event_story/event_stella_2020/scenario/event_01_01.json":
                return json.dumps(
                    {
                        "ScenarioId": "event_01_01",
                        "TalkData": [
                            {"WindowDisplayName": "咲希", "Body": "这是活动剧情正文。"}
                        ],
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_ms(
                store_root,
                depth=1,
                locales=("zh-cn",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
                include_overlay=False,
            )
            self.assertEqual(result["crawled_pages"], 1)
            pages = load_web_pages(store_root)["altsource_ms"]
            self.assertEqual(pages[0]["url"], "https://pjsk.moe/zh-cn/story/event/1/1/")
            self.assertIn("这是活动剧情正文。", pages[0]["text"])

    def test_altsource_text_depth2_fetches_card_scenario_text(self):
        def fake_fetch(url: str) -> str:
            url = url.split("?", 1)[0]
            if url.endswith("/master/eventStories.json"):
                return "[]"
            if url.endswith("/master/unitProfiles.json"):
                return "[]"
            if url.endswith("/master/unitStories.json"):
                return "[]"
            if url.endswith("/master/cards.json"):
                return json.dumps(
                    [{"id": 1, "prefix": "测试卡面", "assetbundleName": "card_01"}],
                    ensure_ascii=False,
                )
            if url.endswith("/master/cardEpisodes.json"):
                return json.dumps(
                    [{"id": 1, "cardId": 1, "title": "卡牌剧情", "scenarioId": "card_01_01"}],
                    ensure_ascii=False,
                )
            if url == "https://storage.exmeaning.com/sekai-cn-assets/character/member/card_01/card_01_01.json":
                return json.dumps(
                    {"TalkData": [{"WindowDisplayName": "凤笑梦", "Body": "这是卡面剧情正文。"}]},
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_ms(
                store_root,
                depth=2,
                locales=("zh-cn",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
            )
            self.assertEqual(result["crawled_pages"], 1)
            pages = load_web_pages(store_root)["altsource_ms"]
            self.assertEqual(pages[0]["url"], "https://pjsk.moe/zh-cn/story/card/1/")
            self.assertIn("这是卡面剧情正文。", pages[0]["text"])

    def test_altsource_jp_special_story_uses_story_asset_for_op_episode(self):
        def fake_fetch(url: str) -> str:
            url = url.split("?", 1)[0]
            if url.endswith("/jp/master/specialStories.json"):
                return json.dumps(
                    [
                        {
                            "id": 2,
                            "assetbundleName": "special-story",
                            "episodes": [
                                {
                                    "id": 4,
                                    "episodeNo": 1,
                                    "scenarioId": "op_01",
                                    "assetbundleName": "story_sp_ts_01_01",
                                    "title": "オープニング1",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                )
            if url == "https://storage.exmeaning.com/sekai-jp-assets/scenario/special/special-story/op_01.json":
                return json.dumps(
                    {
                        "ScenarioId": "op_01",
                        "TalkData": [
                            {"WindowDisplayName": "一歌", "Body": "これは日本語のオープニングです。"}
                        ],
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {url}")

        pages: list[WebPage] = []
        _crawl_altsource_ms_special_stories(
            "jp",
            "ja-jp",
            fake_fetch,
            pages,
            None,
            0,
        )
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].language, "ja")
        self.assertEqual(pages[0].kind, "special_story")
        self.assertIn("これは日本語のオープニングです。", pages[0].text)

    def test_altsource_text_crawl_skips_already_crawled_pages_by_default(self):
        event = {
            "id": 1,
            "eventId": 1,
            "assetbundleName": "event_stella_2020",
            "eventStoryEpisodes": [
                {
                    "episodeNo": 1,
                    "title": "孤独的雨",
                    "scenarioId": "event_01_01",
                }
            ],
        }
        scenario = json.dumps(
            {"TalkData": [{"WindowDisplayName": "咲希", "Body": "活动正文。"}]},
            ensure_ascii=False,
        )

        def fake_fetch(url: str, allow_asset: bool) -> str:
            url = url.split("?", 1)[0]
            if url.endswith("eventStories.json"):
                return json.dumps([event], ensure_ascii=False)
            if url.endswith("unitProfiles.json") or url.endswith("unitStories.json"):
                return "[]"
            if url.endswith("event_01_01.json"):
                if not allow_asset:
                    raise AssertionError(f"Already crawled asset was fetched again: {url}")
                return scenario
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            first = crawl_altsource_ms(
                store_root,
                depth=1,
                locales=("zh-cn",),
                limit=1,
                accept_tos=True,
                delay=0,
                fetcher=lambda url: fake_fetch(url, True),
                include_overlay=False,
            )
            self.assertEqual(first["crawled_pages"], 1)

            second = crawl_altsource_ms(
                store_root,
                depth=1,
                locales=("zh-cn",),
                limit=1,
                accept_tos=True,
                delay=0,
                fetcher=lambda url: fake_fetch(url, False),
                include_overlay=False,
            )
            self.assertEqual(second["crawled_pages"], 0)

            forced = crawl_altsource_ms(
                store_root,
                depth=1,
                locales=("zh-cn",),
                limit=1,
                accept_tos=True,
                delay=0,
                resume=False,
                fetcher=lambda url: fake_fetch(url, True),
                include_overlay=False,
            )
            self.assertEqual(forced["crawled_pages"], 1)

    def test_web_index_search_returns_matching_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            page = WebPage(
                id="web:test:1",
                source="altsource_ms",
                url="https://example.test/cards/1/",
                title="Hoshino Ichika",
                language="zh_hans",
                kind="card",
                text="星乃一歌 is a character in Project Sekai.",
                crawled_at="2026-08-09T00:00:00+00:00",
                hash="abc",
                tos_accepted=True,
            )
            save_web_pages(store_root, "altsource_ms", [page])
            results = web_search(store_root, "星乃一歌", limit=5)
            self.assertEqual(results[0]["id"], "web:test:1")

    def test_save_web_pages_writes_nine_category_files(self):
        kinds = [
            "unit_story",
            "event_story",
            "card_story",
            "special_story",
            "virtual_live",
            "area_talk",
            "home_line",
            "mysekai",
            "other",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = [
                WebPage(
                    id=f"web:category:{kind}",
                    source="altsource_ms",
                    url=f"https://pjsk.moe/{kind}/",
                    title=kind,
                    language="zh_hans",
                    kind=kind,
                    text=f"{kind} text",
                    crawled_at="2026-08-09T00:00:00+00:00",
                    hash=kind,
                    tos_accepted=True,
                )
                for kind in kinds
            ]
            save_web_pages(store_root, "altsource_ms", pages)
            source_dir = store_root / "cache" / "web" / "altsource_ms"
            for filename, _category in WEB_CATEGORY_FILES:
                self.assertTrue((source_dir / filename).exists(), filename)
            categories = json.loads((source_dir / "categories.json").read_text(encoding="utf-8"))
            self.assertEqual(sum(categories.values()), len(pages))

    def test_save_web_pages_checkpoint_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            page = WebPage(
                id="web:checkpoint:1",
                source="altsource_ms",
                url="https://pjsk.moe/zh-cn/story/event/1/1/",
                title="checkpoint 1",
                language="zh_hans",
                kind="event_story",
                text="checkpoint body",
                crawled_at="2026-08-13T00:00:00+00:00",
                hash="a",
                tos_accepted=True,
            )
            save_web_pages(
                store_root,
                "altsource_ms",
                [page],
                rewrite_index=False,
                write_categories=False,
            )
            self.assertTrue((store_root / "kb" / "web" / "altsource_ms" / "pages.json").exists())
            self.assertFalse((store_root / "cache" / "web" / "index.json").exists())
            self.assertFalse((store_root / "cache" / "web" / "altsource_ms" / "01_mainline.json").exists())
            existing = load_existing_page_map(store_root, "altsource_ms")
            self.assertEqual(len(existing), 1)
            second = WebPage(
                id="web:checkpoint:2",
                source="altsource_ms",
                url="https://pjsk.moe/zh-cn/story/event/1/2/",
                title="checkpoint 2",
                language="zh_hans",
                kind="event_story",
                text="checkpoint body two",
                crawled_at="2026-08-13T00:00:00+00:00",
                hash="b",
                tos_accepted=True,
            )
            save_web_pages(
                store_root,
                "altsource_ms",
                [second],
                existing=existing,
                rewrite_index=False,
                write_categories=False,
            )
            save_web_pages(
                store_root,
                "altsource_ms",
                [],
                existing=existing,
                rewrite_index=True,
                write_categories=True,
            )
            index = json.loads((store_root / "cache" / "web" / "index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(index["pages"]), 2)
            self.assertTrue((store_root / "cache" / "web" / "altsource_ms" / "02_event_story.json").exists())

    def test_altsource_ms_page_ids_include_locale(self):
        self.assertEqual(
            altsource_ms_page_id("zh-cn", "event_story", 174, 1),
            "web:altsource_ms:zh-cn:event_story:174:1",
        )
        self.assertEqual(
            altsource_ms_page_id("JA-JP", "unit_story", "ln_01_01"),
            "web:altsource_ms:ja-jp:unit_story:ln_01_01",
        )
        self.assertEqual(
            altsource_ms_page_id("en-us", "card_story", 10001),
            "web:altsource_ms:en-us:card_story:10001",
        )

    def test_instance_does_not_leak_across_calls(self):
        # A custom-instance crawl must not leave its instance ID behind for a
        # subsequent default-instance call (contextvar isolation).
        crawl_altsource_ms_site(
            store_root=Path(tempfile.mkdtemp()) / "store",
            locales=("zh-cn",),
            limit=1,
            accept_tos=True,
            tos_already_checked=True,
            fetcher=lambda url: '<?xml version="1.0"?><urlset></urlset>',
            instance="altsource_ms_alt",
        )
        self.assertEqual(
            altsource_ms_page_id("zh-cn", "event_story", 174, 1),
            "web:altsource_ms:zh-cn:event_story:174:1",
        )

    def test_altsource_ms_story_detail_page_id_contains_locale(self):
        page = altsource_ms_story_detail_page(
            {"title_cn": "????", "outline_cn": "??", "summary_cn": "??"},
            1,
            "ja-jp",
        )
        self.assertTrue(page.id.startswith("web:altsource_ms:ja-jp:story_detail:"))

    def test_canonical_key_handles_locale_keyed_altsource_ids(self):
        self.assertEqual(
            canonical_key_for_page(
                {
                    "kind": "event_story",
                    "id": "web:altsource_ms:zh-cn:event_story:174:1",
                    "url": "https://pjsk.moe/zh-cn/story/event/174/1/",
                    "language": "zh_hans",
                }
            ),
            "event_story:zh_hans:174:1",
        )

    def test_write_consent_migrates_legacy_bool_file(self):
        import json
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            consent = store_root / "kb" / "web" / "consent.json"
            consent.parent.mkdir(parents=True)
            consent.write_text("true", encoding="utf-8")
            write_consent(store_root, "altsource_ms")
            data = json.loads(consent.read_text(encoding="utf-8"))
            self.assertTrue(data["legacy"]["accepted"])
            self.assertTrue(data["altsource_ms"]["accepted"])


    def test_altsource_text_crawl_fetches_translation_overlay_by_default(self):
        overlay_data = {
            "meta": {"source": "official_cn"},
            "episodes": {
                "1": {
                    "scenarioId": "event_01_01",
                    "title": "",
                    "talkData": {
                        "ネットパラダイスに行こう": "去网络天堂吧",
                    },
                }
            },
        }

        def fake_fetch(url: str) -> str:
            url = url.split("?", 1)[0]
            if url == "https://metadata.exmeaning.com/cn/master/eventStories.json":
                return json.dumps(
                    [
                        {
                            "id": 1,
                            "name": "雨过天晴的启明星",
                            "assetbundleName": "event_stella_2020",
                            "eventStoryEpisodes": [
                                {
                                    "episodeNo": 1,
                                    "title": "孤独的雨",
                                    "scenarioId": "event_01_01",
                                }
                            ],
                        }
                    ],
                    ensure_ascii=False,
                )
            if url == "https://metadata.exmeaning.com/cn/master/unitProfiles.json":
                return "[]"
            if url == "https://metadata.exmeaning.com/cn/master/unitStories.json":
                return "[]"
            if url == "https://storage.exmeaning.com/sekai-cn-assets/event_story/event_stella_2020/scenario/event_01_01.json":
                return json.dumps(
                    {
                        "ScenarioId": "event_01_01",
                        "TalkData": [
                            {"WindowDisplayName": "哢希", "Body": "这是活动剧情正文。"}
                        ],
                    },
                    ensure_ascii=False,
                )
            if url == "https://translation.exmeaning.com/translation/eventStory/event_1.json":
                return json.dumps(overlay_data, ensure_ascii=False)
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_ms(
                store_root,
                depth=1,
                locales=("zh-cn",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
            )
            self.assertEqual(result["crawled_pages"], 1)
            self.assertEqual(result["overlay_pages"], 2)
            overlay_pages = load_web_pages(store_root)["altsource_ms_translation"]
            self.assertEqual(len(overlay_pages), 2)
            self.assertTrue(all(page["auxiliary"] for page in overlay_pages))

    def test_altsource_overlay_resume_skips_fetched_translation_pages(self):
        event = {
            "id": 1,
            "eventId": 1,
            "assetbundleName": "event_stella_2020",
            "eventStoryEpisodes": [
                {
                    "episodeNo": 1,
                    "title": "孤独的雨",
                    "scenarioId": "event_01_01",
                }
            ],
        }
        scenario = json.dumps(
            {"TalkData": [{"WindowDisplayName": "哢希", "Body": "活动正文。"}]},
            ensure_ascii=False,
        )
        overlay_data = {
            "meta": {"source": "official_cn"},
            "episodes": {
                "1": {
                    "scenarioId": "event_01_01",
                    "talkData": {
                        "ネットパラダイス": "网络天堂",
                    },
                }
            },
        }

        def fake_fetch(url: str, allow_translation: bool) -> str:
            url = url.split("?", 1)[0]
            if url.endswith("eventStories.json"):
                return json.dumps([event], ensure_ascii=False)
            if url.endswith("unitProfiles.json") or url.endswith("unitStories.json"):
                return "[]"
            if url.endswith("event_01_01.json"):
                return scenario
            if url.endswith("event_1.json"):
                if not allow_translation:
                    raise AssertionError(f"Already crawled translation was fetched again: {url}")
                return json.dumps(overlay_data, ensure_ascii=False)
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            first = crawl_altsource_ms(
                store_root,
                depth=1,
                locales=("zh-cn",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=lambda url: fake_fetch(url, True),
            )
            self.assertEqual(first["overlay_pages"], 2)
            second = crawl_altsource_ms(
                store_root,
                depth=1,
                locales=("zh-cn",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=lambda url: fake_fetch(url, False),
            )
            self.assertEqual(second["overlay_pages"], 0)

    def test_altsource_sv_text_crawl_fetches_i18n_by_default(self):
        def fake_fetch(url: str) -> str:
            url = url.split("?", 1)[0]
            if url.endswith("eventStories.json") or url.endswith("unitStories.json"):
                return "[]"
            if url.endswith("unitProfiles.json"):
                return "[]"
            if url.endswith("event_story_episode_title.json"):
                return json.dumps(
                    {"1-1": "踽踽独行，不见繁星"},
                    ensure_ascii=False,
                )
            if url.startswith("https://i18n-json.sekai.best/"):
                return "404 page not found"
            raise AssertionError(f"Unexpected URL: {url}")

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            result = crawl_altsource_sv(
                store_root,
                regions=("jp",),
                limit=5,
                accept_tos=True,
                delay=0,
                fetcher=fake_fetch,
                depth=1,
            )
            self.assertEqual(result["crawled_pages"], 0)
            self.assertEqual(result["i18n_pages"], 5)
            i18n_pages = load_web_pages(store_root)["altsource_sv_i18n"]
            self.assertEqual(len(i18n_pages), 5)
            self.assertTrue(all(page["auxiliary"] for page in i18n_pages))

    def test_text_matches_language_detects_script_mismatch(self):
        self.assertTrue(text_matches_language("ja", "こんにちは"))
        self.assertFalse(text_matches_language("ja", "繁體中文正文"))
        self.assertFalse(text_matches_language("en", "繁體中文正文"))
        self.assertTrue(text_matches_language("zh_hans", "简体中文正文"))
        self.assertFalse(text_matches_language("zh_hans", "日本語の本文"))
        self.assertTrue(text_matches_language("ko", "안녕하세요"))

    def test_text_matches_language_accepts_official_short_forms(self):
        self.assertTrue(text_matches_language("ja", "順調、順調！"))
        self.assertTrue(text_matches_language("ja", "絵名、大丈夫？"))
        self.assertTrue(text_matches_language("ja", "Make everyone smile！"))
        self.assertTrue(text_matches_language("ja", "L・O・V・E　遥！"))
        self.assertTrue(text_matches_language("zh_hans", "Let's！Wonderho～i！"))
        self.assertTrue(text_matches_language("zh_hant", "咻ーーーーーー♪"))
        self.assertTrue(text_matches_language("zh_hant", "1、2、JUMP♪"))
        self.assertTrue(text_matches_language("zh_hant", "那要開始囉── 《タイムマシン》！"))
        self.assertFalse(text_matches_language("zh_hant", "ミク：あ……まふゆ"))
        self.assertFalse(text_matches_language("zh_hant", "다들 들어줘서 고마워!"))

    def test_normalize_scenario_mismatch_drops_display_name_tokens(self):
        page = {
            "scenario_id_mismatch": (
                "ScenarioId event_167_01 != expected event_168_01; "
                "m_Name ★4冬弥・泉_前半 != expected 012043_touya01; "
                "m_Name ログインストーリー（OP） != expected collaboration_es_op_01"
            )
        }
        out = normalize_scenario_mismatch_flag(page)
        self.assertEqual(out["scenario_id_mismatch"], "ScenarioId event_167_01 != expected event_168_01")

    def test_scenario_pages_keep_metadata_mismatch_separate_from_language_mismatch(self):
        data = {
            "ScenarioId": "event_167_01",
            "m_Name": "event_168_01",
            "TalkData": [{"WindowDisplayName": "齋藤", "Body": "繁體中文正文"}],
            "__scenarioIdMismatch": "ScenarioId event_167_01 != expected event_168_01",
        }
        page = altsource_ms_scenario_page(
            "jp",
            "ja-jp",
            "web:altsource_ms:ja-jp:event_story:168:1",
            "168 第1話",
            "https://pjsk.moe/ja-jp/story/event/168/1/",
            data,
            "event_story",
        )
        self.assertTrue(page.content_language_mismatch)
        self.assertIn("language_mismatch", page.asset_mismatch)
        self.assertIn("ScenarioId event_167_01", page.scenario_id_mismatch)

        jp_data = {
            "ScenarioId": "event_167_01",
            "m_Name": "event_168_01",
            "TalkData": [{"WindowDisplayName": "斎藤", "Body": "日本語の本文"}],
            "__scenarioIdMismatch": "ScenarioId event_167_01 != expected event_168_01",
        }
        jp_page = altsource_sv_scenario_page(
            "jp",
            "web:altsource_sv:jp:event_story:168:1",
            "168 第1話",
            "https://storage.sekai.best/sekai-jp-assets/event_story/event_cheerheart_2025/scenario/event_168_01.asset",
            jp_data,
            "event_story",
        )
        self.assertFalse(jp_page.content_language_mismatch)
        self.assertEqual(jp_page.asset_mismatch, "")
        self.assertIn("ScenarioId event_167_01", jp_page.scenario_id_mismatch)

    def test_altsource_scenario_falls_back_to_altsource_sv_cdn_on_language_mismatch(self):
        calls: list[str] = []

        def fake_fetch(url: str) -> str:
            calls.append(url.split("?", 1)[0])
            if calls[-1].startswith(("https://storage.exmeaning.com/", "https://storage.pjsk.moe/")):
                return json.dumps(
                    {
                        "ScenarioId": "event_167_01",
                        "m_Name": "event_168_01",
                        "TalkData": [{"WindowDisplayName": "齋藤", "Body": "繁體中文正文"}],
                    },
                    ensure_ascii=False,
                )
            if calls[-1].startswith("https://storage.sekai.best/"):
                return json.dumps(
                    {
                        "ScenarioId": "event_167_01",
                        "m_Name": "event_168_01",
                        "TalkData": [{"WindowDisplayName": "斎藤", "Body": "日本語の本文"}],
                    },
                    ensure_ascii=False,
                )
            raise AssertionError(f"Unexpected URL: {calls[-1]}")

        with tempfile.TemporaryDirectory() as tmp:
            data = fetch_altsource_ms_scenario(
                "jp",
                "event_story/event_cheerheart_2025/scenario/event_168_01.json",
                fake_fetch,
                language="ja",
            )
            self.assertIsNotNone(data)
            page = altsource_ms_scenario_page(
                "jp",
                "ja-jp",
                "web:altsource_ms:ja-jp:event_story:168:1",
                "168 第1話",
                "https://pjsk.moe/ja-jp/story/event/168/1/",
                data,
                "event_story",
            )
            self.assertIn("日本語の本文", page.text)
            self.assertFalse(page.content_language_mismatch)
            self.assertEqual(page.asset_mismatch, "")
            self.assertIn("storage.sekai.best", calls[-1])

    def test_web_rebuild_recomputes_keys_and_migrates_metadata_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
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
                        crawled_at="2026-08-11T00:00:00+00:00",
                        hash="a",
                        asset_mismatch="ScenarioId event_01_02 != expected event_01_01",
                    ),
                    WebPage(
                        id="web:altsource_ms:ja-jp:event_story:1:2",
                        source="altsource_ms",
                        url="https://pjsk.moe/ja-jp/story/event/1/2/",
                        title="活動1-2",
                        language="ja",
                        kind="event_story",
                        text="正しい本文",
                        crawled_at="2026-08-11T00:00:00+00:00",
                        hash="b",
                        content_language_mismatch=True,
                    ),
                ],
            )
            result = rebuild_web_index(store_root)
            self.assertEqual(result["pages"], 2)
            pages = load_web_index(store_root)
            migrated = next(p for p in pages if p["id"] == "web:altsource_ms:ja-jp:event_story:1:1")
            self.assertEqual(migrated["asset_mismatch"], "")
            self.assertIn("ScenarioId event_01_02", migrated["scenario_id_mismatch"])
            self.assertEqual(migrated["canonical_key"], "event_story:ja:1:1")
            unflagged = next(p for p in pages if p["id"] == "web:altsource_ms:ja-jp:event_story:1:2")
            self.assertFalse(unflagged["content_language_mismatch"])
            known = load_existing_page_ids(store_root, "altsource_ms", skip_flagged=True)
            self.assertIn("web:altsource_ms:ja-jp:event_story:1:1", known)
            self.assertIn("web:altsource_ms:ja-jp:event_story:1:2", known)

    def test_web_search_orders_by_source_priority(self):
        from sekaisync.sources import SOURCE_MS, SOURCE_SV

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            common = dict(
                title="ネットパラダイス",
                language="zh_hans",
                kind="event_story",
                crawled_at="2026-08-11T00:00:00+00:00",
                hash="h",
            )
            save_web_pages(
                store_root,
                SOURCE_MS,
                [
                    WebPage(
                        id="web:altsource_ms:zh-cn:event_story:174:1",
                        source=SOURCE_MS,
                        url="https://pjsk.moe/zh-cn/story/event/174/1/",
                        text="网络天堂 活動剧情",
                        **common,
                    )
                ],
            )
            save_web_pages(
                store_root,
                SOURCE_SV,
                [
                    WebPage(
                        id="web:altsource_sv:cn:event_story:174:1",
                        source=SOURCE_SV,
                        url="https://sekai.best/storyreader/eventStory/174/1",
                        text="网络天堂 活動剧情",
                        **common,
                    )
                ],
            )
            default = web_search(store_root, "网络天堂")
            self.assertEqual(default[0]["source"], SOURCE_SV)
            prioritized = web_search(
                store_root,
                "网络天堂",
                source_priority=(SOURCE_MS, SOURCE_SV),
            )
            self.assertEqual(prioritized[0]["source"], SOURCE_MS)
            legacy = web_search(store_root, "网络天堂", source="sekai_viewer")
            self.assertEqual(legacy[0]["source"], SOURCE_SV)

    def test_custom_instance_page_ids_and_source_type(self):
        from sekaisync.crawler import (
            _crawl_altsource_ms_translation_events,
            _crawl_altsource_sv_i18n,
            altsource_ms_page_id,
        )
        from sekaisync.sources import BACKEND_MOESEKAI, BACKEND_SEKAI_VIEWER

        def sitemap_fetch(url: str) -> str:
            if "sitemap.xml" in url:
                return '<?xml version="1.0"?><urlset><url><loc>https://pjsk.moe/sitemap-details/zh-cn.xml</loc></url></urlset>'
            return '<?xml version="1.0"?><urlset></urlset>'

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            # sv: custom instance with a fake master table.
            def sv_fetch(url: str) -> str:
                if "events.json" in url:
                    return json.dumps([{"id": 1, "name": "テスト"}], ensure_ascii=False)
                raise AssertionError(f"Unexpected URL: {url}")

            result = crawl_altsource_sv(
                store_root,
                regions=["jp"],
                tables=["events"],
                fetcher=sv_fetch,
                tos_already_checked=True,
                instance="altsource_sv_local",
            )
            self.assertEqual(result["source"], "altsource_sv_local")
            pages = load_web_pages(store_root)["altsource_sv_local"]
            self.assertEqual(len(pages), 1)
            self.assertTrue(pages[0]["id"].startswith("web:altsource_sv_local:"))
            self.assertEqual(pages[0]["source"], "altsource_sv_local")
            self.assertEqual(pages[0]["source_type"], BACKEND_SEKAI_VIEWER)

            # i18n auxiliary pages use the instance's auxiliary namespace.
            def i18n_fetch(url: str) -> str:
                if "event_story_episode_title.json" in url:
                    return json.dumps({"1-1": "测试标题"}, ensure_ascii=False)
                return "404 page not found"

            with crawler_mod._sv_instance_scope("altsource_sv_local"):
                i18n = _crawl_altsource_sv_i18n(store_root, i18n_fetch, 0, resume=False)
            self.assertEqual(i18n["source"], "altsource_sv_local_i18n")
            self.assertIn("altsource_sv_local_i18n", load_web_pages(store_root))

            # ms: custom instance sets the active instance for page IDs and
            # the translation auxiliary namespace.
            crawl_altsource_ms(
                store_root,
                locales=["zh-cn"],
                fetcher=sitemap_fetch,
                tos_already_checked=True,
                include_overlay=False,
                instance="altsource_ms_alt",
            )
            with crawler_mod._ms_instance_scope("altsource_ms_alt"):
                self.assertEqual(
                    altsource_ms_page_id("zh-cn", "event_story", 174, 1),
                    "web:altsource_ms_alt:zh-cn:event_story:174:1",
                )

            def trans_fetch(url: str) -> str:
                return json.dumps(
                    {
                        "meta": {"source": "official_cn"},
                        "episodes": {
                            "1": {
                                "scenarioId": "event_174_01",
                                "title": "",
                                "talkData": {"みのり": "実乃理"},
                            }
                        },
                    },
                    ensure_ascii=False,
                )

            overlay_pages: list = []
            with crawler_mod._ms_instance_scope("altsource_ms_alt"):
                _crawl_altsource_ms_translation_events(
                    "zh-cn", trans_fetch, overlay_pages, None, 0, set(), [174]
                )
            self.assertEqual(len(overlay_pages), 2)
            self.assertTrue(
                overlay_pages[0].id.startswith("web:altsource_ms_alt_translation:")
            )
            self.assertEqual(overlay_pages[0].source, "altsource_ms_alt_translation")
            self.assertEqual(overlay_pages[0].source_type, BACKEND_MOESEKAI)

    def test_web_search_type_selector_matches_all_instances(self):
        from sekaisync.sources import BACKEND_SEKAI_VIEWER

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            common = dict(
                title="网络天堂",
                language="zh_hans",
                kind="event_story",
                crawled_at="2026-08-11T00:00:00+00:00",
                hash="h",
                source_type=BACKEND_SEKAI_VIEWER,
            )
            save_web_pages(
                store_root,
                "altsource_sv",
                [
                    WebPage(
                        id="web:altsource_sv:cn:event_story:174:1",
                        source="altsource_sv",
                        url="https://sekai.best/storyreader/eventStory/174/1",
                        text="网络天堂 官方实例",
                        **common,
                    )
                ],
            )
            save_web_pages(
                store_root,
                "altsource_sv_local",
                [
                    WebPage(
                        id="web:altsource_sv_local:cn:event_story:174:1",
                        source="altsource_sv_local",
                        url="https://viewer.local/storyreader/eventStory/174/1",
                        text="网络天堂 自建实例",
                        **common,
                    )
                ],
            )
            # Type selector matches both instances of the class.
            both = web_search(store_root, "网络天堂", source="altsource_sv")
            self.assertEqual(len(both), 2)
            self.assertEqual({item["source"] for item in both}, {"altsource_sv", "altsource_sv_local"})
            # Instance selector matches exactly one instance.
            only_local = web_search(store_root, "网络天堂", source="altsource_sv_local")
            self.assertEqual(len(only_local), 1)
            self.assertEqual(only_local[0]["source"], "altsource_sv_local")
            # Priority order drives ranking: local instance first when listed first.
            local_first = web_search(
                store_root,
                "网络天堂",
                source_priority=("altsource_sv_local", "altsource_sv"),
            )
            self.assertEqual(local_first[0]["source"], "altsource_sv_local")

class SourceSettingsFlexibilityTest(unittest.TestCase):
    """Configured site endpoints must drive every URL the crawler builds."""

    def tearDown(self):
        apply_source_settings(moesekai=MoesekaiSettings(), viewer=ViewerSettings())

    def test_canonical_urls_follow_configured_site_base(self):
        apply_source_settings(
            moesekai=MoesekaiSettings(site_base="https://mirror.example.com")
        )
        self.assertEqual(
            altsource_ms_canonical_url("zh-cn", "story/event/174/1/"),
            "https://mirror.example.com/zh-cn/story/event/174/1/",
        )
        self.assertEqual(
            altsource_ms_canonical_url("ja-jp", "virtual_live/12"),
            "https://mirror.example.com/ja-jp/virtual_live/12",
        )

    def test_canonical_url_requires_configured_site_base(self):
        apply_source_settings(moesekai=MoesekaiSettings(site_base=""))
        with self.assertRaises(ValueError):
            altsource_ms_canonical_url("zh-cn", "story/event/1/1/")

    def test_sv_master_url_follows_applied_viewer_settings(self):
        apply_source_settings(
            viewer=ViewerSettings(master_base="https://viewer.local/master")
        )
        self.assertEqual(
            altsource_sv_master_json_url("jp", "events"),
            "https://viewer.local/master/sekai-master-db-diff/events.json",
        )

    def test_viewer_cdn_fallback_can_be_disabled(self):
        apply_source_settings(
            moesekai=MoesekaiSettings(
                asset_bases=("https://mirror.example.com/storage",),
                fallback_to_viewer_cdn=False,
            )
        )
        calls: list[str] = []

        def fake_fetch(url: str) -> str:
            calls.append(url.split("?", 1)[0])
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        data = fetch_altsource_ms_scenario(
            "jp", "event_story/x/scenario/event_1_01.json", fake_fetch, language="ja"
        )
        self.assertIsNone(data)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("https://mirror.example.com/storage/"))
        self.assertTrue(all("sekai.best" not in call for call in calls))

    def test_viewer_cdn_fallback_still_used_by_default(self):
        apply_source_settings(
            moesekai=MoesekaiSettings(
                asset_bases=("https://storage.exmeaning.com",),
            ),
            viewer=ViewerSettings(asset_base="https://storage.sekai.best"),
        )
        calls: list[str] = []

        def fake_fetch(url: str) -> str:
            calls.append(url.split("?", 1)[0])
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

        fetch_altsource_ms_scenario(
            "jp", "event_story/x/scenario/event_1_01.json", fake_fetch, language="ja"
        )
        self.assertTrue(any("storage.sekai.best" in call for call in calls))


if __name__ == "__main__":
    unittest.main()
