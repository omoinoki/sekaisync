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
from sekaisync.mcp_server import (
    MAX_STDIO_LINE_BYTES,
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    McpServer,
    negotiate_protocol_version,
)
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

class McpVersionNegotiationTest(unittest.TestCase):
    """P15/W3: ``initialize`` negotiates the revision instead of asserting one.

    Before this change the handler ignored ``params`` entirely and answered
    with its own constant, so an old client and a new client got the same
    silent pass.  The rules under test are the MCP lifecycle ones: echo a
    supported request, **counter-offer** an unsupported one (never refuse),
    and reject only a malformed value.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store_root = Path(self.tmp.name) / "store"
        create_demo_store(store_root)
        self.server = McpServer(SekaiSyncCore(store_root))

    def tearDown(self):
        self.tmp.cleanup()

    def _initialize(self, params):
        return self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params}
        )

    def test_target_version_is_the_documented_one(self):
        """The newest supported revision is what current clients ask for.

        2025-11-25 heads the list because Claude Code, VS Code/Copilot, Cline,
        Continue and Zed all initialize with it; a server whose newest offer is
        older would counter-offer to every one of them.
        """
        self.assertEqual(PROTOCOL_VERSION, "2025-11-25")
        self.assertEqual(SUPPORTED_VERSIONS[0], PROTOCOL_VERSION)
        # 2025-06-18 stays served: several clients (and the TypeScript SDK's
        # DEFAULT_NEGOTIATED_PROTOCOL_VERSION path) still speak it.
        self.assertIn("2025-06-18", SUPPORTED_VERSIONS)

    def test_every_supported_version_is_echoed_back(self):
        for version in SUPPORTED_VERSIONS:
            with self.subTest(version=version):
                response = self._initialize({"protocolVersion": version})
                self.assertEqual(response["result"]["protocolVersion"], version)
                self.assertEqual(response["id"], 1)

    def test_echo_rule_returns_the_clients_version_not_the_newest(self):
        """With two versions supported, an older request must NOT be upgraded.

        Answering with the server's newest version would announce a revision
        the client did not offer; the spec tells such a client to disconnect.
        """
        version, failure = negotiate_protocol_version(
            {"protocolVersion": "2024-11-05"},
            supported=("2025-06-18", "2024-11-05"),
        )
        self.assertIsNone(failure)
        self.assertEqual(version, "2024-11-05")

    def test_unsupported_version_is_counter_offered_not_rejected(self):
        """The lifecycle MUST: respond with another version we support.

        This used to return -32602, copying the spec page's Error Handling
        example -- which contradicts the MUST on the same page.  Measured
        fallout of the error form: every mainstream SDK aborts the connection
        on a JSON-RPC error instead of retrying, so the Lens Studio server
        (which did exactly this) broke VS Code and Claude Code simultaneously
        (microsoft/vscode#286908, anthropics/claude-code#17319), and the
        protocol maintainers ruled the error form non-compliant
        (inspector#959).  The counter-offer is disclosed -- the response names
        2025-11-25, not what the client asked -- so nothing is silent, and a
        client that cannot speak it disconnects itself per the spec.
        """
        response = self._initialize({"protocolVersion": "2024-11-05"})
        self.assertIn("result", response, response)
        self.assertEqual(response["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(response["id"], 1)

    def test_counter_offer_discloses_not_silently_passes(self):
        """A counter-offer must be visible: the announced version differs from
        what the client asked.  This is what separates it from the original
        defect (echoing a hardcoded constant whatever the client said)."""
        for requested in ("2024-11-05", "2025-03-26", "2026-07-28"):
            with self.subTest(requested=requested):
                response = self._initialize({"protocolVersion": requested})
                announced = response["result"]["protocolVersion"]
                self.assertNotEqual(announced, requested)
                self.assertIn(announced, SUPPORTED_VERSIONS)

    def test_malformed_version_is_rejected(self):
        """Non-revisions stay errors: they cannot be a protocol version at all.

        ``2025-13-99`` parses as a date shape but is not a real revision;
        rejecting it is fine either way, so the assertion is only that it does
        not silently initialize.
        """
        for bad in ("", "latest", 5, True, ["2025-06-18"]):
            with self.subTest(bad=bad):
                response = self._initialize({"protocolVersion": bad})
                self.assertNotIn("result", response, bad)
                self.assertEqual(response["error"]["code"], -32602, bad)

    def test_absent_version_falls_back_to_the_newest_supported(self):
        """Documented choice: not an error, fall back to the newest supported.

        A ``protocolVersion`` member is mandatory for a real client, so this
        is a compatibility path for older local clients that send
        ``params: {}`` -- not a claim that the member is optional.
        """
        for params in ({}, {"protocolVersion": None}):
            with self.subTest(params=params):
                response = self._initialize(params)
                self.assertEqual(
                    response["result"]["protocolVersion"], SUPPORTED_VERSIONS[0]
                )

    def test_absent_params_at_all_still_initializes(self):
        response = self.server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
        )
        self.assertEqual(response["result"]["protocolVersion"], SUPPORTED_VERSIONS[0])

    def test_malformed_initialize_leaves_the_session_usable(self):
        """A version error must not poison the loop for the next frame."""
        rejected = self._initialize({"protocolVersion": "not-a-version"})
        self.assertNotIn("result", rejected)
        followed = self.server.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {"protocolVersion": PROTOCOL_VERSION}}
        )
        self.assertEqual(followed["result"]["protocolVersion"], PROTOCOL_VERSION)

    def test_unsupported_version_over_stdio_still_gets_an_answer(self):
        """A counter-offered initialize answers normally and keeps reading."""
        frames = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
        stdout = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            self.server.run(
                stdin=io.StringIO("".join(json.dumps(f) + "\n" for f in frames)),
                stdout=stdout,
            )
        responses = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertNotIn("error", responses[0])
        self.assertEqual(responses[1]["result"], {})


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

