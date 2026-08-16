import tempfile
import unittest
from pathlib import Path

from sekaisync.cli import create_demo_store
from sekaisync.config import SekaiSyncConfig
from sekaisync.core import SekaiSyncCore
from sekaisync.fetcher import sync
from sekaisync.layout import terms_path
from sekaisync.mcp_server import McpServer
from sekaisync.models import WebPage
from sekaisync.termindex import save_terms, seed_from_glossary
from sekaisync.webindex import save_web_pages


class McpServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
        save_terms(seed_from_glossary(store_root), terms_path(store_root))
        self.server = McpServer(SekaiSyncCore(store_root))

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_and_tool_call(self):
        init = self.server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "SekaiSync")

        call = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "sekaisync_lookup",
                    "arguments": {"query": "Hoshino Ichika", "type": "character"},
                },
            }
        )
        self.assertIn("demo:character:1", call["result"]["content"][0]["text"])

    def test_event_alias_tool_registered(self):
        listed = self.server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}})
        names = [tool["name"] for tool in listed["result"]["tools"]]
        self.assertIn("sekaisync_event_alias", names)
    def test_web_lookup_tool(self):
        save_web_pages(
            Path(self.tmp.name) / "store",
            "altsource_ms",
            [
                WebPage(
                    id="web:altsource_ms:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/test",
                    title="星乃一歌",
                    language="zh_hans",
                    kind="page",
                    text="关于星乃一歌的文字。",
                    crawled_at="2026-08-09T00:00:00+00:00",
                    hash="abc",
                    tos_accepted=True,
                )
            ],
        )
        call = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sekaisync_web_lookup",
                    "arguments": {"query": "星乃一歌"},
                },
            }
        )
        self.assertIn("web:altsource_ms:1", call["result"]["content"][0]["text"])

    def test_unified_query_tool(self):
        call = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "sekaisync_query",
                    "arguments": {"query": "Hoshino Ichika", "type": "character"},
                },
            }
        )
        self.assertIn("demo:character:1", call["result"]["content"][0]["text"])

    def test_term_lookup_tool(self):
        call = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "sekaisync_term_lookup",
                    "arguments": {
                        "query": "星乃一歌",
                        "languages": ["ja", "zh_hans"],
                    },
                },
            }
        )
        self.assertIn("星乃一歌", call["result"]["content"][0]["text"])

    def test_progress_tool(self):
        call = self.server.handle(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/call",
                "params": {
                    "name": "sekaisync_progress",
                    "arguments": {"regions": "demo"},
                },
            }
        )
        self.assertIn("overall", call["result"]["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()

