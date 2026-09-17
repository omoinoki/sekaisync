import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sekaisync.cli import create_demo_store
from sekaisync.config import SekaiSyncConfig
from sekaisync.core import SekaiSyncCore
from sekaisync.fetcher import sync
from sekaisync.http_server import SekaiSyncHandler, handle_mcp_message
from sekaisync.mcp_server import PROTOCOL_VERSION, SUPPORTED_VERSIONS, McpServer


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


class _Headers(dict):
    """Case-insensitive, duplicate-preserving header mapping for stubs."""

    def __init__(self, data):
        super().__init__()
        self._pairs = []
        for key, value in (data or {}).items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                self._pairs.append((key, item))

    def get(self, key, default=None):
        for k, v in self._pairs:
            if k.lower() == key.lower():
                return v
        return default

    def items(self):
        return list(self._pairs)


class _StubRequest:
    """The handler instance plus the captured response, for assertions."""

    def __init__(self, request, status, headers, body):
        self.request = request
        self.status = status
        self.headers = headers
        self._body = body

    def header(self, name):
        for key, value in self.headers:
            if key.lower() == name.lower():
                return value
        return None

    def body(self):
        return self._body

    def json(self):
        return json.loads(self._body)


def call_handler(
    core, path, *, method="GET", body=b"", headers=None, sites=(), bound_port=8791
):
    """Invoke the real handler against an in-memory request.

    Defaults mirror a real local client (loopback Host, JSON content type,
    correct Content-Length); tests override them to probe the boundary.
    """
    handler = type(
        "BoundHandler",
        (SekaiSyncHandler,),
        {"core": core, "sites": tuple(sites), "bound_port": bound_port},
    )
    request = handler.__new__(handler)
    raw = body.encode("utf-8") if isinstance(body, str) else body
    default_headers = {
        "Host": f"127.0.0.1:{bound_port}",
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
    }
    default_headers.update(headers or {})
    request.path = path
    request.rfile = io.BytesIO(raw)
    request.wfile = io.BytesIO()
    request.headers = _Headers(default_headers)
    request.connection = None
    captured = {"status": None, "headers": []}

    def send_response(status, *args, **kwargs):
        captured["status"] = status

    def send_header(key, value):
        captured["headers"].append((key, value))

    request.send_response = send_response
    request.send_header = send_header
    request.end_headers = lambda: None
    with patch.object(SekaiSyncHandler, "log_message", lambda *args: None):
        getattr(request, f"do_{method}")()
    return _StubRequest(
        request,
        captured["status"],
        captured["headers"],
        request.wfile.getvalue().decode("utf-8"),
    )


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
        self.assertEqual(init["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(PROTOCOL_VERSION, "2025-06-18")

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
        response = call_handler(self.core, "/api/v1/event_alias?query=khn2&regions=jp")
        self.assertEqual(response.status, 200)
        self.assertIn('"event_id": 9', response.body())

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
        response = call_handler(self.core, "/api/v1/sites", sites=sites)
        self.assertEqual(response.status, 200)
        self.assertIn('"id": "altsource_sv"', response.body())
        self.assertIn("viewer.example/master", response.body())

    def test_sites_http_endpoint_empty_profile(self):
        response = call_handler(self.core, "/api/v1/sites")
        self.assertEqual(response.status, 200)
        self.assertIn('"sites": []', response.body())


    def test_core_refresh_reloads_indexes(self):
        from sekaisync.layout import registry_path
        from sekaisync.registry import save_registry

        before = self.core.refresh()
        self.assertIn("registry", before)
        self.assertIn("terms", before)

        # Simulate an external sync: append a registry entity.
        from sekaisync.models import Entity

        extra = Entity(
            id="simulated_new_entity",
            type="character",
            region="demo",
            regions=["demo"],
            names={"ja": "テスト追加"},
            facts={},
            source="demo",
            demo=True,
            trust="D",
        )
        save_registry(
            [*self.core.registry, extra],
            registry_path(self.core.store_root),
        )
        after = self.core.refresh()
        self.assertGreater(after["registry"], before["registry"])

    def test_refresh_mcp_tool(self):
        result = handle_mcp_message(
            self.core,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "sekaisync_refresh", "arguments": {}},
            },
        )
        self.assertIn("result", result)
        content = result["result"]["content"][0]["text"]
        self.assertIn("registry", content)
        self.assertIn("terms", content)

    def test_refresh_http_endpoint(self):
        response = call_handler(self.core, "/api/v1/refresh", method="POST", body=b"")
        self.assertEqual(response.status, 200)
        self.assertIn('"refreshed": true', response.body())


class HttpBoundaryTest(unittest.TestCase):
    """P15/D15: the local HTTP boundary, validated before anything else."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
        self.core = SekaiSyncCore(store_root)

    def tearDown(self):
        self.tmp.cleanup()

    # -- Host ------------------------------------------------------------

    def test_non_loopback_host_is_rejected(self):
        response = call_handler(self.core, "/health", headers={"Host": "evil.example.com"})
        self.assertEqual(response.status, 403)

    def test_loopback_host_with_wrong_port_is_rejected(self):
        response = call_handler(
            self.core, "/health", headers={"Host": "127.0.0.1:9999"}, bound_port=8791
        )
        self.assertEqual(response.status, 403)

    def test_duplicate_host_header_is_rejected(self):
        response = call_handler(
            self.core, "/health", headers={"Host": ["127.0.0.1:8791", "127.0.0.1:8791"]}
        )
        self.assertEqual(response.status, 403)

    def test_missing_host_header_is_rejected(self):
        response = call_handler(self.core, "/health", headers={"Host": ""})
        self.assertEqual(response.status, 403)

    def test_loopback_host_accepted(self):
        self.assertEqual(call_handler(self.core, "/health").status, 200)

    # -- Origin ----------------------------------------------------------

    def test_absent_origin_is_allowed(self):
        """Local CLI / Node clients send no Origin and must keep working."""
        self.assertEqual(call_handler(self.core, "/health").status, 200)

    def test_cross_origin_request_is_rejected(self):
        response = call_handler(
            self.core, "/health", headers={"Origin": "https://evil.example.com"}
        )
        self.assertEqual(response.status, 403)

    def test_null_origin_is_rejected(self):
        response = call_handler(self.core, "/health", headers={"Origin": "null"})
        self.assertEqual(response.status, 403)

    def test_rejected_request_carries_no_cors_headers(self):
        response = call_handler(
            self.core, "/health", headers={"Origin": "https://evil.example.com"}
        )
        self.assertEqual(response.status, 403)
        self.assertIsNone(response.header("Access-Control-Allow-Origin"))

    def test_no_cors_wildcard_on_allowed_response(self):
        for path in ("/health", "/openapi.json", "/.well-known/mcp.json"):
            response = call_handler(self.core, path)
            self.assertEqual(response.status, 200)
            self.assertNotEqual(
                response.header("Access-Control-Allow-Origin"), "*", path
            )

    def test_options_does_not_grant_wildcard(self):
        response = call_handler(self.core, "/health", method="OPTIONS")
        self.assertEqual(response.status, 204)
        self.assertNotEqual(response.header("Access-Control-Allow-Origin"), "*")

    # -- Content-Type / Length / Transfer-Encoding -----------------------

    def test_non_json_content_type_on_post_is_rejected(self):
        response = call_handler(
            self.core,
            "/api/v1/verify_claims",
            method="POST",
            body=json.dumps({"claims": [{"claim": "x"}]}),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(response.status, 400)

    def test_non_json_content_type_on_mcp_is_rejected(self):
        response = call_handler(
            self.core,
            "/mcp",
            method="POST",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(response.status, 400)

    def test_duplicate_content_length_is_rejected(self):
        response = call_handler(
            self.core,
            "/api/v1/verify_claims",
            method="POST",
            body=json.dumps({"claims": []}),
            headers={"Content-Length": ["2", "2"]},
        )
        self.assertEqual(response.status, 400)

    def test_unsupported_transfer_encoding_is_rejected(self):
        response = call_handler(
            self.core, "/health", headers={"Transfer-Encoding": "chunked"}
        )
        self.assertEqual(response.status, 400)

    def test_oversize_body_is_rejected_with_413(self):
        response = call_handler(
            self.core,
            "/api/v1/verify_claims",
            method="POST",
            body=json.dumps({"claims": []}),
            headers={"Content-Length": str(2 * 1024 * 1024)},
        )
        self.assertEqual(response.status, 413)

    def test_malformed_json_body_is_400(self):
        response = call_handler(
            self.core, "/api/v1/verify_claims", method="POST", body="{not json"
        )
        self.assertEqual(response.status, 400)

    def test_non_object_json_body_is_400(self):
        response = call_handler(
            self.core, "/api/v1/verify_claims", method="POST", body="[1, 2, 3]"
        )
        self.assertEqual(response.status, 400)

    # -- Methods ---------------------------------------------------------

    def test_events_check_get_is_405_and_writes_nothing(self):
        """P15: events/check writes, so GET must not reach Core at all."""
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return {"checked": True}

        original = self.core.event_check
        self.core.event_check = spy
        try:
            response = call_handler(self.core, "/api/v1/events/check?regions=demo")
        finally:
            self.core.event_check = original
        self.assertEqual(response.status, 405)
        self.assertEqual(calls, [])

    def test_events_check_post_reaches_core(self):
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return {"checked": True}

        original = self.core.event_check
        self.core.event_check = spy
        try:
            response = call_handler(
                self.core, "/api/v1/events/check", method="POST", body=b"{}"
            )
        finally:
            self.core.event_check = original
        self.assertEqual(response.status, 200)
        self.assertEqual(len(calls), 1)

    def test_refresh_is_not_a_get_route(self):
        response = call_handler(self.core, "/api/v1/refresh")
        self.assertEqual(response.status, 405)

    def test_unknown_method_is_405_not_501(self):
        response = call_handler(self.core, "/health", method="DELETE")
        self.assertEqual(response.status, 405)

    def test_unknown_path_is_404(self):
        self.assertEqual(call_handler(self.core, "/api/v1/nope").status, 404)

    # -- Argument parsing before Core ------------------------------------

    def _assert_rejected_before_core(self, attribute, path, status=400):
        calls = []

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return {}

        original = getattr(self.core, attribute)
        setattr(self.core, attribute, spy)
        try:
            response = call_handler(self.core, path)
        finally:
            setattr(self.core, attribute, original)
        self.assertEqual(response.status, status, response.body())
        self.assertEqual(calls, [], f"{path} reached Core: {calls}")
        return response

    def test_bad_int_never_reaches_core(self):
        self._assert_rejected_before_core("news", "/api/v1/news?limit=abc")
        self._assert_rejected_before_core("news", "/api/v1/news?limit=1e9")
        self._assert_rejected_before_core("news", "/api/v1/news?limit=-5")

    def test_limit_over_ceiling_never_reaches_core(self):
        self._assert_rejected_before_core("news", "/api/v1/news?limit=101")
        self._assert_rejected_before_core("lookup", "/api/v1/lookup?query=x&limit=100000")

    def test_bad_bool_never_reaches_core(self):
        self._assert_rejected_before_core("news", "/api/v1/news?body=maybe")
        self._assert_rejected_before_core("news", "/api/v1/news?body=2")
        self._assert_rejected_before_core("news", "/api/v1/news?body=")

    def test_bool_accepts_explicit_true_false_set(self):
        for text, expected in (("true", True), ("1", True), ("yes", True),
                               ("false", False), ("0", False), ("no", False)):
            seen = {}

            def spy(*args, **kwargs):
                seen.update(kwargs)
                return {"items": [], "matched": 0}

            original = self.core.news
            self.core.news = spy
            try:
                response = call_handler(self.core, f"/api/v1/news?body={text}")
            finally:
                self.core.news = original
            self.assertEqual(response.status, 200, text)
            self.assertIs(seen["body"], expected, text)

    def test_blank_query_never_reaches_core(self):
        self._assert_rejected_before_core("lookup", "/api/v1/lookup?query=%20%20")
        self._assert_rejected_before_core("lookup", "/api/v1/lookup")

    def test_overlong_query_never_reaches_core(self):
        self._assert_rejected_before_core(
            "lookup", "/api/v1/lookup?query=" + "a" * 3000
        )

    def test_claims_over_batch_limit_is_rejected(self):
        calls = []

        def spy(*args, **kwargs):
            calls.append(args)
            return []

        original = self.core.verify_claims
        self.core.verify_claims = spy
        try:
            response = call_handler(
                self.core,
                "/api/v1/verify_claims",
                method="POST",
                body=json.dumps({"claims": [{"claim": "x"}] * 101}),
            )
        finally:
            self.core.verify_claims = original
        self.assertEqual(response.status, 400)
        self.assertEqual(calls, [])

    def test_csv_regions_reach_core_as_list(self):
        """A bad list shape must never reach Core as a non-list."""
        calls = []

        def spy(*args, **kwargs):
            calls.append(kwargs)
            return {"overall": {}, "regions": {}}

        original = self.core.progress
        self.core.progress = spy
        try:
            response = call_handler(self.core, "/api/v1/progress?regions=j|p")
        finally:
            self.core.progress = original
        self.assertEqual(response.status, 200)
        for call in calls:
            self.assertIsInstance(call.get("regions"), (list, type(None)), call)

    def test_news_body_absent_stays_none(self):
        """The three-state contract: absent is not False."""
        seen = {}

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return {"items": [], "matched": 0}

        original = self.core.news
        self.core.news = spy
        try:
            response = call_handler(self.core, "/api/v1/news")
        finally:
            self.core.news = original
        self.assertEqual(response.status, 200)
        self.assertIsNone(seen["body"])

    def test_event_archive_limit_zero_still_means_all(self):
        seen = {}

        def spy(*args, **kwargs):
            seen.update(kwargs)
            return {"events": []}

        original = self.core.event_archive
        self.core.event_archive = spy
        try:
            response = call_handler(self.core, "/api/v1/events/archive?limit=0")
        finally:
            self.core.event_archive = original
        self.assertEqual(response.status, 200)
        self.assertIsNone(seen["limit"])

    def test_unexpected_core_exception_is_sanitized(self):
        def boom(*args, **kwargs):
            raise RuntimeError("secret internal detail: /etc/passwd")

        original = self.core.news
        self.core.news = boom
        try:
            response = call_handler(self.core, "/api/v1/news")
        finally:
            self.core.news = original
        self.assertEqual(response.status, 500)
        self.assertNotIn("secret internal detail", response.body())
        self.assertNotIn("Traceback", response.body())


class OpenApiDerivationTest(unittest.TestCase):
    """P15: /openapi.json is derived from the ToolSpec registry."""

    def test_openapi_is_derived_from_registry(self):
        from sekaisync.http_server import OPENAPI
        from sekaisync.tools import TOOLS, build_openapi

        self.assertEqual(OPENAPI, build_openapi(TOOLS))

    def test_every_route_method_matches_the_registry(self):
        from sekaisync.http_server import OPENAPI
        from sekaisync.tools import TOOLS

        for spec in TOOLS:
            if not spec.http_path:
                continue
            operation = OPENAPI["paths"][spec.http_path]
            self.assertIn(spec.http_method.lower(), operation, spec.http_path)

    def test_events_check_is_post_in_openapi(self):
        from sekaisync.http_server import OPENAPI

        path = OPENAPI["paths"]["/api/v1/events/check"]
        self.assertIn("post", path)
        self.assertNotIn("get", path)

    def test_news_limit_and_params_are_documented(self):
        from sekaisync.http_server import OPENAPI

        parameters = OPENAPI["paths"]["/api/v1/news"]["get"]["parameters"]
        names = {p["name"] for p in parameters}
        self.assertEqual(names, {"limit", "language", "tag", "body"})
        limit = next(p for p in parameters if p["name"] == "limit")
        self.assertEqual(limit["schema"]["type"], "integer")

    def test_declared_limits_appear_in_schema(self):
        from sekaisync.http_server import OPENAPI
        from sekaisync.tools import MAX_LIMIT

        for path in ("/api/v1/lookup", "/api/v1/news", "/api/v1/query"):
            parameters = OPENAPI["paths"][path]["get"]["parameters"]
            limit = next(p for p in parameters if p["name"] == "limit")
            self.assertEqual(limit["schema"]["maximum"], MAX_LIMIT, path)

    def test_404_notes_are_derived(self):
        from sekaisync.http_server import OPENAPI

        responses = OPENAPI["paths"]["/api/v1/event_alias"]["get"]["responses"]
        self.assertIn("404", responses)


class McpHttpTransportTest(unittest.TestCase):
    """The /mcp POST endpoint's transport-level error mapping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
        self.core = SekaiSyncCore(store_root)

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, body, headers=None):
        return call_handler(self.core, "/mcp", method="POST", body=body, headers=headers)

    def test_parse_error_is_32700(self):
        response = self._post("{not json")
        self.assertEqual(response.status, 400)
        self.assertEqual(response.json()["error"]["code"], -32700)

    def test_non_object_message_is_32600(self):
        response = self._post("[]")
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_bad_jsonrpc_version_is_32600(self):
        response = self._post(json.dumps({"jsonrpc": "1.0", "id": 1, "method": "initialize"}))
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_non_string_method_is_32600(self):
        response = self._post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": 5}))
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_unknown_method_is_32601(self):
        response = self._post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "nope"}))
        self.assertEqual(response.json()["error"]["code"], -32601)

    def test_invalid_tool_arguments_are_32602(self):
        response = self._post(json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "sekaisync_lookup", "arguments": {"query": True}},
        }))
        error = response.json()["error"]
        self.assertEqual(error["code"], -32602)
        self.assertNotIn("Traceback", json.dumps(error))

    def test_valid_notification_gets_202(self):
        response = self._post(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertEqual(response.status, 202)
        self.assertEqual(response.body(), "")

    def test_valid_request_after_malformed_one(self):
        first = self._post("{not json")
        self.assertEqual(first.json()["error"]["code"], -32700)
        second = self._post(json.dumps({
            "jsonrpc": "2.0", "id": 7, "method": "initialize", "params": {},
        }))
        self.assertEqual(second.status, 200)
        self.assertEqual(second.json()["id"], 7)
        self.assertEqual(second.json()["result"]["serverInfo"]["name"], "SekaiSync")


class McpProtocolVersionTest(unittest.TestCase):
    """P15/W3: the HTTP surface exposes and enforces the negotiated revision."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
        self.core = SekaiSyncCore(store_root)

    def tearDown(self):
        self.tmp.cleanup()

    def _post(self, payload, headers=None):
        return call_handler(
            self.core, "/mcp", method="POST", body=json.dumps(payload), headers=headers
        )

    def test_response_header_matches_the_new_constant(self):
        for accept in ("application/json", "text/event-stream"):
            with self.subTest(accept=accept):
                response = self._post(
                    {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                    headers={"Accept": accept},
                )
                self.assertEqual(response.status, 200)
                self.assertEqual(response.header("MCP-Protocol-Version"), PROTOCOL_VERSION)
                self.assertEqual(response.header("MCP-Protocol-Version"), "2025-06-18")

    def test_initialized_version_is_echoed_over_http(self):
        response = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": SUPPORTED_VERSIONS[0]},
        })
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.json()["result"]["protocolVersion"], SUPPORTED_VERSIONS[0]
        )

    def test_unsupported_version_is_a_32602_error_not_a_result(self):
        response = self._post({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05"},
        })
        self.assertEqual(response.status, 200)
        payload = response.json()
        self.assertNotIn("result", payload, payload)
        self.assertEqual(payload["error"]["code"], -32602)
        self.assertIn("2024-11-05", payload["error"]["message"])
        self.assertEqual(payload["error"]["data"]["requested"], "2024-11-05")
        self.assertEqual(
            payload["error"]["data"]["supported"], list(SUPPORTED_VERSIONS)
        )

    def test_matching_request_header_is_accepted(self):
        response = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"MCP-Protocol-Version": PROTOCOL_VERSION},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["result"], {})

    def test_unsupported_request_header_is_400(self):
        """2025-06-18: an invalid/unsupported header MUST be 400 Bad Request."""
        response = self._post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"MCP-Protocol-Version": "2024-11-05"},
        )
        self.assertEqual(response.status, 400)
        payload = response.json()
        self.assertNotIn("result", payload, payload)
        self.assertIn("2024-11-05", payload["error"]["message"])
        self.assertEqual(payload["error"]["data"]["supported"], list(SUPPORTED_VERSIONS))

    def test_absent_request_header_is_not_an_error(self):
        """Documented choice: no header means nothing to check, not a 400.

        The revision's SHOULD-assume-2025-03-26 rule is not implemented, so
        failing header-less local clients would break them for no gain;
        ``initialize`` remains the authority on the version.
        """
        response = self._post({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["result"], {})

    def test_header_rejection_happens_before_the_body_is_read(self):
        response = call_handler(
            self.core, "/mcp", method="POST", body="not json at all",
            headers={"MCP-Protocol-Version": "1999-01-01"},
        )
        self.assertEqual(response.status, 400)
        self.assertIn("1999-01-01", response.json()["error"]["message"])

    def test_other_endpoints_ignore_the_mcp_header(self):
        """The header governs /mcp only; REST routes do not require it."""
        response = call_handler(
            self.core, "/health",
            headers={"MCP-Protocol-Version": "1999-01-01"},
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["status"], "ok")


class LoopbackBindingTest(unittest.TestCase):
    """P15: non-loopback binds are refused, and the DSH contract is kept."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store_root = Path(self.tmp.name) / "store"
        create_demo_store(self.store_root)
        self.core = SekaiSyncCore(self.store_root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_wildcard_and_public_addresses_are_not_loopback(self):
        from sekaisync.http_server import is_loopback_host

        for host in ("0.0.0.0", "::", "*", "192.168.1.5", "evil.example.com"):
            self.assertFalse(is_loopback_host(host), host)

    def test_loopback_spellings_are_recognised(self):
        from sekaisync.http_server import is_loopback_host

        for host in ("127.0.0.1", "localhost", "::1", "[::1]", "127.0.0.2"):
            self.assertTrue(is_loopback_host(host), host)

    def test_non_loopback_bind_is_refused_with_an_explanation(self):
        from sekaisync.http_server import serve_http

        with self.assertRaises(SystemExit) as ctx:
            serve_http(self.core, host="0.0.0.0", port=0, sites=())
        message = str(ctx.exception)
        self.assertIn("loopback", message.lower())
        self.assertIn("authentication", message.lower())

    def test_public_ip_bind_is_refused(self):
        from sekaisync.http_server import serve_http

        with self.assertRaises(SystemExit):
            serve_http(self.core, host="203.0.113.10", port=0, sites=())

    def test_serve_http_reports_bound_port_and_health_contract(self):
        """The DSH contract: port=0, stdout loopback:port, status/ready keys.

        Run in a subprocess so the server's stdout cannot be entangled with
        the test runner's, and so the listening socket is released on exit.
        """
        import subprocess
        import sys as _sys
        import time
        from http.client import HTTPConnection

        project_root = str(Path(__file__).resolve().parents[1])
        script = (
            "import sys\n"
            "from pathlib import Path\n"
            "from sekaisync.cli import create_demo_store\n"
            "from sekaisync.core import SekaiSyncCore\n"
            f"store = Path(r'{self.store_root}')\n"
            "create_demo_store(store)\n"
            "from sekaisync.http_server import serve_http\n"
            "serve_http(SekaiSyncCore(store), host='127.0.0.1', port=0, sites=())\n"
        )
        process = subprocess.Popen(
            [_sys.executable, "-X", "utf8", "-u", "-c", script],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            port = None
            deadline = time.time() + 30
            lines = []
            # The banner is three lines printed before serve_forever() blocks.
            while len(lines) < 3 and time.time() < deadline:
                line = process.stdout.readline()
                if not line:
                    break
                lines.append(line)
                if port is None and "listening on http://127.0.0.1:" in line:
                    port = int(line.split("listening on http://127.0.0.1:")[1].split("/")[0])
            self.assertIsNotNone(port, f"no bound port reported; output={lines!r}")
            stdout_text = "".join(lines)
            self.assertIn(f"http://127.0.0.1:{port}", stdout_text)
            self.assertIn("/openapi.json", stdout_text)
            self.assertIn("/mcp", stdout_text)

            connection = HTTPConnection("127.0.0.1", port, timeout=10)
            connection.request("GET", "/health")
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(payload["status"], "ok")
            self.assertIn("ready", payload)
        finally:
            process.kill()
            process.wait(timeout=10)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
