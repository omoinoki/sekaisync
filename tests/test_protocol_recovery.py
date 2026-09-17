"""P15 regression tests: temporary stores and in-memory transports only."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sekaisync.core import SekaiSyncCore
from sekaisync.mcp_server import McpServer, _OVERSIZE, _read_bounded_line, _write_line
from tests.test_http_mcp import call_handler


def request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


class SerializationRecoveryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.core = SekaiSyncCore(Path(tmp.name) / "store")
        self.server = McpServer(self.core)

    def assert_internal(self, response):
        self.assertEqual(response, {
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32603, "message": "Internal error"},
        })

    def test_stdio_tool_serialization_failure_then_ping(self):
        cyclic = []
        cyclic.append(cyclic)
        for value in (object(), cyclic, float("nan")):
            with self.subTest(value_type=type(value).__name__):
                with patch.object(self.core, "news", return_value=value):
                    output = io.StringIO()
                    frames = [request("tools/call", {"name": "sekaisync_news"}),
                              request("ping", request_id=2)]
                    self.server.run(io.StringIO("".join(json.dumps(x) + "\n" for x in frames)), output)
                responses = [json.loads(x) for x in output.getvalue().splitlines()]
                self.assertEqual(len(responses), 2)
                self.assert_internal(responses[0])
                self.assertEqual(responses[1]["result"], {})
                self.assertEqual(responses[1]["id"], 2)

    def test_resource_serialization_failure_is_internal(self):
        with patch.object(self.core, "data_gaps", return_value=object()):
            self.assert_internal(self.server.handle(request("resources/read", {"uri": "sekaisync://gaps"})))
        self.assertEqual(self.server.handle(request("ping"))["result"], {})

    def test_outer_stdio_serialization_failure_is_internal(self):
        output = io.StringIO()
        _write_line(output, {"jsonrpc": "2.0", "id": 1, "result": object()})
        _write_line(output, self.server.handle(request("ping", request_id=2)))
        responses = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assert_internal(responses[0])
        self.assertEqual(responses[1]["id"], 2)

    def test_http_rest_serialization_failure_is_500_before_headers(self):
        for value in (object(), float("nan"), "\ud800"):
            with self.subTest(value_type=type(value).__name__):
                with patch.object(self.core, "news", return_value=value):
                    response = call_handler(self.core, "/api/v1/news")
                self.assertEqual(response.status, 500)
                self.assertEqual(response.json(), {"error": "Internal error"})
                self.assertEqual(int(response.header("Content-Length")), len(response.body().encode("utf-8")))
        self.assertEqual(call_handler(self.core, "/health").status, 200)

    def test_http_mcp_serialization_failure_json_and_sse(self):
        for accept in ("application/json", "text/event-stream"):
            with self.subTest(accept=accept):
                with patch.object(self.core, "news", return_value=object()):
                    response = call_handler(self.core, "/mcp", method="POST",
                                            body=json.dumps(request("tools/call", {"name": "sekaisync_news"})),
                                            headers={"Accept": accept})
                self.assertEqual(response.status, 200)
                body = response.body()
                if accept == "text/event-stream":
                    body = body.split("data: ", 1)[1].strip()
                self.assert_internal(json.loads(body))

    def test_http_outer_mcp_serialization_failure_is_internal(self):
        with patch("sekaisync.http_server.handle_mcp_message", return_value={
            "jsonrpc": "2.0", "id": 1, "result": object(),
        }):
            response = call_handler(self.core, "/mcp", method="POST", body=json.dumps(request("ping")))
        self.assertEqual(response.status, 200)
        self.assert_internal(response.json())


class OversizeBoundedLineTest(unittest.TestCase):
    """The over-budget drain itself must be bounded and close the frame."""

    LIMIT = 128

    def _reader(self, payload: str):
        return _read_bounded_line(io.StringIO(payload), self.LIMIT)

    def test_oversize_line_is_discarded_and_next_line_is_clean(self):
        stream = io.StringIO("a" * (self.LIMIT + 500) + "\nnext\n")
        reader = _read_bounded_line
        self.assertIs(reader(stream, self.LIMIT), _OVERSIZE)
        self.assertEqual(reader(stream, self.LIMIT), "next\n")

    def test_oversize_final_line_without_newline_is_discarded_at_eof(self):
        stream = io.StringIO("b" * (self.LIMIT + 500))
        self.assertIs(_read_bounded_line(stream, self.LIMIT), _OVERSIZE)
        self.assertIsNone(_read_bounded_line(stream, self.LIMIT))

    def test_exactly_at_budget_is_accepted_including_newline(self):
        stream = io.StringIO("c" * self.LIMIT + "\n")
        self.assertEqual(_read_bounded_line(stream, self.LIMIT), "c" * self.LIMIT + "\n")

    def test_oversize_newline_terminated_line_is_discarded(self):
        # A line longer than the budget must not be accepted just because it
        # ends with a newline (the newline must not bypass the budget).
        stream = io.StringIO("d" * (self.LIMIT + 1) + "\nnext\n")
        self.assertIs(_read_bounded_line(stream, self.LIMIT), _OVERSIZE)
        self.assertEqual(_read_bounded_line(stream, self.LIMIT), "next\n")

    def test_bytes_stream_budget_counts_bytes_not_chars(self):
        from sekaisync.mcp_server import _line_len

        self.assertEqual(_line_len("é" * 64 + "\n"), 64)
        self.assertEqual(_line_len("é".encode("utf-8") * 64 + b"\n"), 128)


class StdioServerOversizeTest(unittest.TestCase):
    """The run loop's handling of an oversized frame must not skip following requests."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.server = McpServer(SekaiSyncCore(Path(tmp.name) / "store"))

    def test_run_discards_oversize_and_answers_next_request(self):
        from sekaisync.mcp_server import MAX_STDIO_LINE_BYTES

        oversize = "x" * (MAX_STDIO_LINE_BYTES + 10)
        frames = oversize + "\n" + json.dumps(request("ping", request_id=2)) + "\n"
        output = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()):
            self.server.run(io.StringIO(frames), output)
        responses = [json.loads(x) for x in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["error"]["code"], -32600)
        self.assertEqual(responses[1]["id"], 2)
        self.assertEqual(responses[1]["result"], {})


if __name__ == "__main__":
    unittest.main()
