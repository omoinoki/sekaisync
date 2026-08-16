import tempfile
import unittest
from pathlib import Path

from sekaisync.cli import create_demo_store
from sekaisync.config import SekaiSyncConfig
from sekaisync.core import SekaiSyncCore
from sekaisync.fetcher import sync
from sekaisync.layout import terms_path, web_consent_path
from sekaisync.models import WebPage
from sekaisync.termindex import TermRecord, make_term_id, save_terms, seed_from_glossary
from sekaisync.webindex import save_web_pages


class CoreTest(unittest.TestCase):
    def test_event_alias_method(self):
        from tests.test_eventalias import make_store

        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            make_store(store_root)
            core = SekaiSyncCore(store_root)
            result = core.event_alias("khn2", regions=["jp"])
            self.assertIsNotNone(result)
            self.assertEqual(result["mapping"]["jp"]["event_id"], 9)
            self.assertIsNone(core.event_alias("khn99", regions=["jp"]))
            alias_map = core.event_alias(list_all=True, regions=["jp"])
            self.assertEqual(len(alias_map["characters"]), 20)

    def test_demo_store_lookup_and_resolve(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            create_demo_store(store_root)
            config = SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True)
            sync(config, ["demo"])

            core = SekaiSyncCore(store_root)
            self.assertTrue(core.ready())
            freshness = core.freshness()
            self.assertEqual(freshness["coverage"]["demo"]["story_full_text"]["status"], "demo")
            self.assertEqual(freshness["sources"][0]["key"], "master_db")
            self.assertFalse(freshness["web"]["enabled"])

            results = core.lookup("Hoshino Ichika", type="character")
            self.assertEqual(results[0]["id"], "demo:character:1")
            self.assertEqual(results[0]["trust"], "D")
            trust = core.trust_summary()
            self.assertEqual(trust["totals"]["registry"], len(core.registry))

            resolved = core.resolve_name("Hoshino Ichika", target_language="zh_tw")
            self.assertEqual(resolved[0]["target_name"], "星乃一歌")

            pack = core.fact_pack("demo:character:1", language="zh_tw")
            self.assertIn("星乃一歌", pack["text"])

            verified = core.verify_claims(
                [{"claim": "Hoshino Ichika", "expected": "星乃一歌"}]
            )
            self.assertEqual(verified[0]["status"], "verified")

    def test_unified_query_combines_metadata_and_web_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            create_demo_store(store_root)
            sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:story:1",
                        source="altsource_ms",
                        url="https://pjsk.moe/zh-cn/story/1/",
                        title="星乃一歌",
                        language="zh_hans",
                        kind="event_story",
                        text="星乃一歌在剧情中登场。",
                        crawled_at="2026-08-09T00:00:00+00:00",
                        hash="abc",
                        tos_accepted=True,
                    )
                ],
            )
            consent_path = web_consent_path(store_root)
            consent_path.parent.mkdir(parents=True, exist_ok=True)
            consent_path.write_text("true", encoding="utf-8")
            core = SekaiSyncCore(store_root)
            result = core.query("星乃一歌")
            self.assertTrue(result["metadata"])
            self.assertTrue(result["web"])

    def test_status_and_web_browse_expose_frontend_endpoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            create_demo_store(store_root)
            sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:event:1",
                        source="altsource_ms",
                        url="https://pjsk.moe/zh-cn/story/event/1/1/",
                        title="活动剧情",
                        language="zh_hans",
                        kind="event_story",
                        text="这是活动剧情正文。",
                        crawled_at="2026-08-09T00:00:00+00:00",
                        hash="event1",
                        tos_accepted=True,
                    )
                ],
            )
            consent_path = web_consent_path(store_root)
            consent_path.parent.mkdir(parents=True, exist_ok=True)
            consent_path.write_text("true", encoding="utf-8")
            core = SekaiSyncCore(store_root)
            status = core.status()
            self.assertGreaterEqual(status["master"]["entities"], 2)
            self.assertTrue(status["web"]["enabled"])
            self.assertEqual(status["web"]["category_counts"]["altsource_ms"]["event_story"], 1)

            browsed = core.web_browse(kind="event_story", limit=5)
            self.assertEqual(browsed[0]["id"], "web:altsource_ms:event:1")
            self.assertIn("这是活动剧情正文。", browsed[0]["snippet"])

    def test_term_index_is_independent_and_queried_by_core(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            create_demo_store(store_root)
            sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
            save_terms(seed_from_glossary(store_root), terms_path(store_root))
            core = SekaiSyncCore(store_root)
            results = core.term_lookup(
                "星乃一歌",
                source_language="ja",
                languages=["ja", "zh_hans", "en"],
            )
            self.assertEqual(results[0]["names"]["en"], "Hoshino Ichika")
            self.assertTrue(results[0]["official"])
            self.assertEqual(core.term_status()["terms"], 5)

    def test_core_is_ready_with_terms_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_terms(
                [
                    TermRecord(
                        id=make_term_id("ja", "テスト"),
                        canonical="テスト",
                        source_language="ja",
                        names={"ja": "テスト", "zh_hans": "测试"},
                        source="local",
                    )
                ],
                terms_path(store_root),
            )
            core = SekaiSyncCore(store_root)
            self.assertTrue(core.ready())


if __name__ == "__main__":
    unittest.main()



