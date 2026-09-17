import tempfile
import contextlib
import io
import json
import unittest
from pathlib import Path

from sekaisync.cli import create_demo_store
from sekaisync.config import SekaiSyncConfig
from sekaisync.core import SekaiSyncCore
from sekaisync.fetcher import sync
from sekaisync.layout import terms_path
from sekaisync.mcp_server import MAX_STDIO_LINE_BYTES, McpServer
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


class McpEnvelopeTest(unittest.TestCase):
    """P15/D15: JSON-RPC envelope validation and error mapping."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
        self.server = McpServer(SekaiSyncCore(store_root))

    def tearDown(self):
        self.tmp.cleanup()

    def _codes(self, message):
        response = self.server.handle(message)
        self.assertIsInstance(response, dict)
        return response

    def test_non_object_message_is_32600(self):
        for bad in ([], "x", 5, True, None):
            response = self.server.handle(bad)
            self.assertIsNotNone(response, bad)
            self.assertEqual(response["error"]["code"], -32600, bad)

    def test_wrong_jsonrpc_version_is_32600(self):
        response = self._codes({"jsonrpc": "1.0", "id": 1, "method": "initialize"})
        self.assertEqual(response["error"]["code"], -32600)

    def test_missing_jsonrpc_is_32600(self):
        response = self._codes({"id": 1, "method": "initialize"})
        self.assertEqual(response["error"]["code"], -32600)

    def test_non_string_method_is_32600(self):
        response = self._codes({"jsonrpc": "2.0", "id": 1, "method": 5})
        self.assertEqual(response["error"]["code"], -32600)

    def test_non_object_params_is_32602(self):
        response = self._codes({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": []})
        self.assertEqual(response["error"]["code"], -32602)

    def test_boolean_id_is_rejected(self):
        response = self._codes({"jsonrpc": "2.0", "id": True, "method": "initialize"})
        self.assertEqual(response["error"]["code"], -32600)

    def test_unknown_method_is_32601(self):
        response = self._codes({"jsonrpc": "2.0", "id": 1, "method": "nope"})
        self.assertEqual(response["error"]["code"], -32601)

    def test_unknown_tool_is_32601(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "sekaisync_nope", "arguments": {}},
        })
        self.assertEqual(response["error"]["code"], -32601)

    def test_missing_tool_name_is_32602(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {},
        })
        self.assertEqual(response["error"]["code"], -32602)

    def test_non_object_arguments_is_32602(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "sekaisync_lookup", "arguments": "x"},
        })
        self.assertEqual(response["error"]["code"], -32602)

    # -- id / notification semantics -------------------------------------

    def test_missing_id_is_a_notification(self):
        self.assertIsNone(
            self.server.handle({"jsonrpc": "2.0", "method": "initialize", "params": {}})
        )

    def test_explicit_null_id_is_not_a_notification(self):
        response = self._codes({"jsonrpc": "2.0", "id": None, "method": "initialize"})
        self.assertIn("result", response)
        self.assertIsNone(response["id"])

    def test_notification_still_runs_state_handling(self):
        calls = []
        server = McpServer(self.server.core)
        original = server._handle_notification
        server._handle_notification = lambda method: (calls.append(method), original(method))[1]
        self.assertIsNone(
            server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        )
        self.assertEqual(calls, ["notifications/initialized"])

    # -- type strictness -------------------------------------------------

    def test_boolean_is_not_an_integer(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "sekaisync_news", "arguments": {"limit": True}},
        })
        self.assertEqual(response["error"]["code"], -32602)

    def test_string_is_not_an_array(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "sekaisync_event_check",
                "arguments": {"regions": "jp"},
            },
        })
        # A CSV string is the documented HTTP encoding; over MCP the declared
        # type is an array, so a bare string is an invalid argument.
        self.assertEqual(response["error"]["code"], -32602)

    def test_array_element_must_be_a_string(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "sekaisync_event_check",
                "arguments": {"regions": [1, 2]},
            },
        })
        self.assertEqual(response["error"]["code"], -32602)

    def test_blank_required_query_is_32602(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "sekaisync_lookup", "arguments": {"query": "   "}},
        })
        self.assertEqual(response["error"]["code"], -32602)

    def test_missing_required_query_is_32602(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "sekaisync_lookup", "arguments": {}},
        })
        self.assertEqual(response["error"]["code"], -32602)

    def test_no_core_write_happens_for_bad_arguments(self):
        calls = []
        core = self.server.core
        original = core.event_check
        core.event_check = lambda *a, **k: calls.append(a) or {"checked": True}
        try:
            response = self._codes({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {
                    "name": "sekaisync_event_check",
                    "arguments": {"regions": "jp", "timeout": True},
                },
            })
        finally:
            core.event_check = original
        self.assertEqual(response["error"]["code"], -32602)
        self.assertEqual(calls, [])

    # -- error shaping ---------------------------------------------------

    def test_business_failure_is_a_compliant_iserror_result(self):
        core = self.server.core

        def boom(*args, **kwargs):
            raise RuntimeError("backend exploded: /secret/path is unreadable")

        original = core.news
        core.news = boom
        try:
            response = self._codes({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "sekaisync_news", "arguments": {}},
            })
        finally:
            core.news = original
        self.assertIn("result", response)
        self.assertTrue(response["result"]["isError"])
        self.assertIn("content", response["result"])
        rendered = json.dumps(response)
        self.assertNotIn("backend exploded", rendered)
        self.assertNotIn("/secret/path", rendered)
        self.assertNotIn("Traceback", rendered)

    def test_internal_exception_is_sanitized(self):
        core = self.server.core

        def boom(*args, **kwargs):
            raise RuntimeError("internal detail that must not leak")

        original = core.data_gaps
        core.data_gaps = boom
        try:
            response = self._codes({
                "jsonrpc": "2.0", "id": 1, "method": "resources/read",
                "params": {"uri": "sekaisync://gaps"},
            })
        finally:
            core.data_gaps = original
        self.assertEqual(response["error"]["code"], -32603)
        self.assertNotIn("internal detail", json.dumps(response))

    def test_next_request_succeeds_after_an_internal_error(self):
        core = self.server.core
        original = core.news

        def boom(*args, **kwargs):
            raise RuntimeError("boom")

        core.news = boom
        try:
            self._codes({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "sekaisync_news", "arguments": {}},
            })
        finally:
            core.news = original
        response = self._codes({
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {
                "name": "sekaisync_lookup",
                "arguments": {"query": "Hoshino Ichika"},
            },
        })
        self.assertIn("result", response)
        self.assertIn("demo:character:1", response["result"]["content"][0]["text"])

    def test_unknown_resource_is_32602(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "resources/read",
            "params": {"uri": "sekaisync://nope"},
        })
        self.assertEqual(response["error"]["code"], -32602)

    def test_templates_are_listed_separately_from_concrete_resources(self):
        """A parameterized URI is a template, not a readable resource.

        Listing it under ``resources/list`` invites a client to read the
        literal ``sekaisync://terms/{query}``, which would look up the text
        "{query}" and return a plausible but meaningless answer.
        """
        from sekaisync.mcp_server import RESOURCE_SPECS, RESOURCE_TEMPLATES

        listed = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "resources/list", "params": {},
        })["result"]["resources"]
        uris = {item["uri"] for item in listed}
        self.assertEqual(uris, {"sekaisync://gaps", "sekaisync://registry/summary"})
        self.assertFalse(any("{" in uri for uri in uris))

        templates = self.server.handle({
            "jsonrpc": "2.0", "id": 2, "method": "resources/templates/list", "params": {},
        })["result"]["resourceTemplates"]
        self.assertEqual(
            {item["uriTemplate"] for item in templates},
            {"sekaisync://terms/{query}", "sekaisync://news/{language}"},
        )

    def test_reading_a_template_uri_is_rejected_not_answered(self):
        for uri in ("sekaisync://terms/{query}", "sekaisync://news/{language}"):
            with self.subTest(uri=uri):
                response = self._codes({
                    "jsonrpc": "2.0", "id": 1, "method": "resources/read",
                    "params": {"uri": uri},
                })
                self.assertEqual(response["error"]["code"], -32602)

    def test_a_filled_template_reads_normally(self):
        response = self._codes({
            "jsonrpc": "2.0", "id": 1, "method": "resources/read",
            "params": {"uri": "sekaisync://terms/星乃一歌"},
        })
        payload = json.loads(response["result"]["contents"][0]["text"])
        self.assertEqual(payload["query"], "星乃一歌")

class McpStdioTest(unittest.TestCase):
    """P15/D15: the stdio loop is bounded and recovers from bad frames."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
        self.server = McpServer(SekaiSyncCore(store_root))

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, payload):
        stdout = io.StringIO()
        # Odd frames are expected in these tests; keep the server's
        # stderr diagnostics out of the test runner's output.
        with contextlib.redirect_stderr(io.StringIO()):
            self.server.run(stdin=io.StringIO(payload), stdout=stdout)
        return [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    def test_blank_lines_are_ignored(self):
        self.assertEqual(self._run("\n\n   \n"), [])

    def test_parse_error_is_reported_and_loop_continues(self):
        payload = "{bad json\n" + json.dumps(
            {"jsonrpc": "2.0", "id": 9, "method": "initialize", "params": {}}
        ) + "\n"
        responses = self._run(payload)
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32700)
        self.assertIsNone(responses[0]["id"])
        self.assertEqual(responses[1]["id"], 9)
        self.assertEqual(responses[1]["result"]["serverInfo"]["name"], "SekaiSync")

    def test_valid_message_after_malformed_one(self):
        payload = (
            "[]\n"
            + json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            + "\n"
        )
        responses = self._run(payload)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertIn("result", responses[1])
        self.assertTrue(responses[1]["result"]["tools"])

    def test_notification_produces_no_output(self):
        payload = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        self.assertEqual(self._run(payload), [])

    def test_oversize_frame_is_discarded_not_reparsed(self):
        """The leftover half of an over-long frame must never become a request."""
        oversize = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "sekaisync_lookup", "arguments": {"query": "x" * 2000000}},
            }
        )
        follow_up = json.dumps(
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {}}
        )
        responses = self._run(oversize + "\n" + follow_up + "\n")
        self.assertEqual(len(responses), 2, responses)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertIn("exceeds", responses[0]["error"]["message"])
        self.assertIsNone(responses[0]["id"])
        # The follow-up frame is parsed as its own request, not as the tail
        # of the discarded one.
        self.assertEqual(responses[1]["id"], 2)
        self.assertIn("result", responses[1])

    def test_bounded_line_reader_stops_at_the_limit(self):
        from sekaisync.mcp_server import MAX_STDIO_LINE_BYTES, _OVERSIZE, _read_bounded_line

        small = io.StringIO("hello\nworld\n")
        self.assertEqual(_read_bounded_line(small, 100), "hello\n")
        self.assertEqual(_read_bounded_line(small, 100), "world\n")
        self.assertIsNone(_read_bounded_line(small, 100))

        long_stream = io.StringIO("a" * (MAX_STDIO_LINE_BYTES + 10) + "\nnext\n")
        self.assertIs(_read_bounded_line(long_stream, MAX_STDIO_LINE_BYTES), _OVERSIZE)
        self.assertEqual(_read_bounded_line(long_stream, MAX_STDIO_LINE_BYTES), "next\n")

    def test_stdout_carries_protocol_lines_only(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        payload = "{bad\n"
        with contextlib.redirect_stderr(stderr):
            self.server.run(stdin=io.StringIO(payload), stdout=stdout)
        for line in stdout.getvalue().splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line)
            self.assertEqual(parsed["jsonrpc"], "2.0")

    def test_overlong_frame_is_logged_to_stderr_not_stdout(self):
        oversize = "x" * (MAX_STDIO_LINE_BYTES + 32) + "\n"
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.server.run(stdin=io.StringIO(oversize), stdout=stdout)
        for line in stdout.getvalue().splitlines():
            if line.strip():
                self.assertEqual(json.loads(line)["jsonrpc"], "2.0")
        self.assertIn("discarded", stderr.getvalue())
        self.assertNotIn("xxx", stdout.getvalue())


class McpToolSchemaTest(unittest.TestCase):
    """The advertised schemas must match what validation actually enforces."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        self.server = McpServer(SekaiSyncCore(store_root))

    def tearDown(self):
        self.tmp.cleanup()

    def _tools(self):
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        )
        return {tool["name"]: tool for tool in response["result"]["tools"]}

    def test_news_body_has_no_false_default(self):
        schema = self._tools()["sekaisync_news"]["inputSchema"]
        body = schema["properties"]["body"]
        self.assertEqual(body["type"], "boolean")
        self.assertNotIn("default", body)

    def test_limit_maximum_is_advertised(self):
        from sekaisync.tools import MAX_LIMIT

        schema = self._tools()["sekaisync_news"]["inputSchema"]
        self.assertEqual(schema["properties"]["limit"]["maximum"], MAX_LIMIT)

    def test_query_max_length_is_advertised(self):
        from sekaisync.tools import MAX_QUERY_LENGTH

        schema = self._tools()["sekaisync_lookup"]["inputSchema"]
        self.assertEqual(
            schema["properties"]["query"]["maxLength"], MAX_QUERY_LENGTH
        )

    def test_claims_max_items_is_advertised(self):
        from sekaisync.tools import MAX_CLAIMS

        schema = self._tools()["sekaisync_verify_claims"]["inputSchema"]
        self.assertEqual(schema["properties"]["claims"]["maxItems"], MAX_CLAIMS)

    def test_advertised_enum_matches_validation(self):
        tools = self._tools()
        schema = tools["sekaisync_term_lookup"]["inputSchema"]
        self.assertEqual(sorted(schema["properties"]["sort"]["enum"]), ["score", "weight"])
        response = self.server.handle({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {
                "name": "sekaisync_term_lookup",
                "arguments": {"query": "x", "sort": "bogus"},
            },
        })
        self.assertEqual(response["error"]["code"], -32602)


if __name__ == "__main__":
    unittest.main()

