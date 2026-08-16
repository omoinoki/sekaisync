import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.cli import create_demo_store
from sekaisync.config import SekaiSyncConfig
from sekaisync.core import SekaiSyncCore
from sekaisync.fetcher import sync
from sekaisync.http_server import SekaiSyncHandler, handle_mcp_message
from sekaisync.mcp_server import PROTOCOL_VERSION, McpServer


def _write_json(directory: Path, name: str, data) -> None:
    (directory / name).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def make_alias_store(root: Path) -> None:
    jp = root / "raw" / "jp" / "source" / "sekai-master-db-diff-main"
    jp.mkdir(parents=True, exist_ok=True)
    _write_json(jp, "events.json", [
        {"id": 8, "eventType": "marathon", "name": "Kick it up a notch", "startAt": 8000},
        {"id": 9, "eventType": "marathon", "name": "On Your Feet", "startAt": 9000},
    ])
    _write_json(jp, "eventCards.json", [
        {"id": 12, "eventId": 8, "cardId": 112},
        {"id": 13, "eventId": 8, "cardId": 113},
        {"id": 15, "eventId": 9, "cardId": 115},
    ])
    _write_json(jp, "cards.json", [
        {"id": 112, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "JP心羽3"},
        {"id": 113, "characterId": 10, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "JP杏3"},
        {"id": 115, "characterId": 9, "cardRarityType": "rarity_4", "supportUnit": "street", "prefix": "JP心羽4"},
    ])
    _write_json(jp, "eventMusics.json", [
        {"eventId": 8, "musicId": 14, "seq": 1},
        {"eventId": 9, "musicId": 16, "seq": 1},
    ])
    _write_json(jp, "musics.json", [
        {"id": 14, "title": "ひつじがいっぴき"},
        {"id": 16, "title": "リアライズ"},
    ])
    _write_json(jp, "gameCharacterUnits.json", [
        {"id": 4, "gameCharacterId": 9, "unit": "street"},
        {"id": 5, "gameCharacterId": 10, "unit": "street"},
    ])


class HttpMcpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
        make_alias_store(store_root)
        self.core = SekaiSyncCore(store_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_initialize_and_tool_call_through_mcp_handler(self):
        init = handle_mcp_message(
            self.core,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "SekaiSync")
        self.assertEqual(init["result"]["protocolVersion"], "2024-11-05")

        call = handle_mcp_message(
            self.core,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "sekaisync_resolve_name",
                    "arguments": {"query": "Hoshino Ichika", "target_language": "zh_tw"},
                },
            },
        )
        self.assertIn("星乃一歌", call["result"]["content"][0]["text"])

    def test_event_alias_mcp_tool(self):
        call = handle_mcp_message(
            self.core,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sekaisync_event_alias",
                    "arguments": {"query": "khn2", "regions": ["jp"]},
                },
            },
        )
        self.assertIn('"event_id": 9', call["result"]["content"][0]["text"])

    def test_event_alias_http_endpoint(self):
        handler = type("BoundHandler", (SekaiSyncHandler,), {"core": self.core})
        request = handler.__new__(handler)
        # Exercise only the routing decision without a real socket.
        import io
        from unittest.mock import patch

        request.rfile = io.BytesIO(b"")
        request.wfile = io.BytesIO()
        request.headers = {}
        captured = {}
        request.send_response = lambda status: captured.__setitem__("status", status)
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        with patch.object(SekaiSyncHandler, "log_message", lambda *args: None):
            request.path = "/api/v1/event_alias?query=khn2&regions=jp"
            request.do_GET()
        self.assertEqual(captured["status"], 200)
        body = request.wfile.getvalue().decode("utf-8")
        self.assertIn('"event_id": 9', body)

    def test_sites_http_endpoint_returns_bound_profile(self):
        from sekaisync.config import SiteSettings, ViewerSettings

        sites = (
            SiteSettings(
                id="altsource_sv",
                backend="sekai_viewer",
                name="Sekai Viewer",
                viewer=ViewerSettings(master_base="https://viewer.example/master"),
            ),
        )
        handler = type(
            "BoundHandler", (SekaiSyncHandler,), {"core": self.core, "sites": sites}
        )
        request = handler.__new__(handler)
        import io
        from unittest.mock import patch

        request.rfile = io.BytesIO(b"")
        request.wfile = io.BytesIO()
        request.headers = {}
        captured = {}
        request.send_response = lambda status: captured.__setitem__("status", status)
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        with patch.object(SekaiSyncHandler, "log_message", lambda *args: None):
            request.path = "/api/v1/sites"
            request.do_GET()
        self.assertEqual(captured["status"], 200)
        body = request.wfile.getvalue().decode("utf-8")
        self.assertIn('"id": "altsource_sv"', body)
        self.assertIn("viewer.example/master", body)

    def test_sites_http_endpoint_empty_profile(self):
        handler = type("BoundHandler", (SekaiSyncHandler,), {"core": self.core})
        request = handler.__new__(handler)
        import io
        from unittest.mock import patch

        request.rfile = io.BytesIO(b"")
        request.wfile = io.BytesIO()
        request.headers = {}
        captured = {}
        request.send_response = lambda status: captured.__setitem__("status", status)
        request.send_header = lambda *args: None
        request.end_headers = lambda: None
        with patch.object(SekaiSyncHandler, "log_message", lambda *args: None):
            request.path = "/api/v1/sites"
            request.do_GET()
        self.assertEqual(captured["status"], 200)
        body = request.wfile.getvalue().decode("utf-8")
        self.assertIn('"sites": []', body)


if __name__ == "__main__":
    unittest.main()
