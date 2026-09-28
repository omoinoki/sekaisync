"""Connection budgets, safe binding and malformed JSON over existing routes."""

import contextlib
import io
import json
import socket
import threading
import unittest
from http.client import HTTPConnection
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sekaisync import http_server
from tests.test_http_mcp import call_handler


class LoopbackResolutionTest(unittest.TestCase):
    def test_all_literal_loopback_spellings_remain_valid_without_dns(self):
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("literal must not resolve")):
            for host in ("127.0.0.1", "127.0.0.2", "::1", "[::1]", "0:0:0:0:0:0:0:1"):
                self.assertTrue(http_server.is_loopback_host(host), host)
            for host in ("0.0.0.0", "::", "192.0.2.1"):
                self.assertFalse(http_server.is_loopback_host(host), host)

    def test_mixed_dns_answers_are_rejected_in_both_orders(self):
        local = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))
        remote = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 0))
        for answers in ([local, remote], [remote, local]):
            with patch.object(socket, "getaddrinfo", return_value=answers):
                self.assertFalse(http_server.is_loopback_host("mixed.invalid"))
                with self.assertRaises(SystemExit):
                    http_server.serve_http(object(), host="mixed.invalid", port=0, sites=())

    def test_bind_pins_verified_address_and_preserves_ephemeral_port_banner(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.2", 0))]
        fake_server = Mock(server_address=("127.0.0.2", 12345))
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(socket, "getaddrinfo", return_value=answers) as resolve:
            with patch.object(http_server, "BoundedThreadingHTTPServer", return_value=fake_server) as factory:
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    http_server.serve_http(object(), host="local.invalid", port=0, sites=())
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(factory.call_args.args[0], ("127.0.0.2", 0))
        self.assertEqual(factory.call_args.kwargs["address_family"], socket.AF_INET)
        self.assertIn("http://127.0.0.2:12345", output.getvalue())
        fake_server.server_close.assert_called_once()

    def test_ipv6_binding_uses_ipv6_socket_and_bracketed_url(self):
        fake_server = Mock(server_address=("::1", 12345, 0, 0))
        fake_server.serve_forever.side_effect = KeyboardInterrupt
        with patch.object(http_server, "BoundedThreadingHTTPServer", return_value=fake_server) as factory:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                http_server.serve_http(object(), host="[::1]", port=0, sites=())
        self.assertEqual(factory.call_args.args[0], ("::1", 0))
        self.assertEqual(factory.call_args.kwargs["address_family"], socket.AF_INET6)
        self.assertIn("http://[::1]:12345", output.getvalue())


@contextlib.contextmanager
def running_server(header_timeout=2):
    entered = threading.Event()
    finished = threading.Event()

    class Handler(http_server.SekaiSyncHandler):
        core = SimpleNamespace(ready=lambda: True)
        bound_host = "127.0.0.1"
        request_slots = threading.BoundedSemaphore(1)

        def handle(self):
            entered.set()
            super().handle()

        def _send_json(self, status, payload):
            self.server.response_timeouts.append(self.connection.gettimeout())
            super()._send_json(status, payload)

    class Server(http_server.BoundedThreadingHTTPServer):
        def process_request_thread(self, request, client_address):
            try:
                super().process_request_thread(request, client_address)
            finally:
                finished.set()

    with patch.object(http_server, "MAX_CONCURRENT_REQUESTS", 1):
        with patch.object(http_server, "HEADER_READ_TIMEOUT_SECONDS", header_timeout):
            server = Server(("127.0.0.1", 0), Handler)
            Handler.bound_port = server.server_address[1]
            server.response_timeouts = []
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                yield server, entered, finished
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class ConnectionBudgetTest(unittest.TestCase):
    def assert_health(self, server):
        connection = HTTPConnection(*server.server_address[:2], timeout=2)
        try:
            connection.request("GET", "/health")
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read()), {"status": "ok", "ready": True})
        finally:
            connection.close()

    def test_partial_headers_consume_budget_before_dispatch(self):
        with running_server() as (server, entered, finished):
            first = socket.create_connection(server.server_address, timeout=2)
            try:
                first.sendall(b"GET /health HTTP/1.1\r\n")
                self.assertTrue(entered.wait(2))
                with socket.create_connection(server.server_address, timeout=2) as second:
                    # Overload is rejected before another HTTP request is read.
                    response = second.recv(4096)
                    self.assertIn(b"503 Service Unavailable", response)
                    self.assertIn(b"Server is busy", response)
            finally:
                first.close()
            self.assertTrue(finished.wait(2), "A disconnected partial request must release its slot")
            self.assert_health(server)

    def test_idle_headers_time_out_and_release_capacity(self):
        with running_server(header_timeout=0.05) as (server, entered, finished):
            with socket.create_connection(server.server_address, timeout=2) as first:
                self.assertTrue(entered.wait(2))
                self.assertEqual(first.recv(4096), b"")
                self.assertTrue(finished.wait(2))
            self.assert_health(server)

    def test_header_timeout_does_not_limit_long_response_writes(self):
        with running_server(header_timeout=0.05) as (server, entered, finished):
            self.assert_health(server)
            self.assertEqual(server.response_timeouts, [None])

    def test_worker_start_failure_releases_connection_slot(self):
        server = http_server.BoundedThreadingHTTPServer(("127.0.0.1", 0), http_server.SekaiSyncHandler)
        self.addCleanup(server.server_close)
        server.connection_slots = threading.BoundedSemaphore(1)
        request = Mock()
        with patch.object(http_server.ThreadingHTTPServer, "process_request", side_effect=RuntimeError("cannot start worker")):
            with self.assertRaises(RuntimeError):
                server.process_request(request, ("127.0.0.1", 12345))
        self.assertTrue(server.connection_slots.acquire(blocking=False))
        server.connection_slots.release()


class BodyBoundaryTest(unittest.TestCase):
    def test_body_read_restores_original_blocking_timeout(self):
        handler = object.__new__(http_server.SekaiSyncHandler)
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = None
        handler.rfile = io.BytesIO(b"{}")
        self.assertEqual(handler._read_body(2), b"{}")
        self.assertEqual(handler.connection.settimeout.call_args_list[-1].args, (None,))

    def test_parser_resource_errors_are_sanitized_on_rest_and_mcp_routes(self):
        # Supported Python versions use different JSON recursion/digit limits.
        # Exercise their parser failures without assuming one fixed nesting
        # depth must fail on every interpreter or suite configuration.
        for error in (RecursionError("nested JSON"), ValueError("integer digit limit")):
            for path in ("/mcp", "/api/v1/verify_claims"):
                with self.subTest(error=type(error).__name__, path=path):
                    with patch.object(http_server.json, "loads", side_effect=error):
                        response = call_handler(object(), path, method="POST", body="{}")
                    self.assertEqual(response.status, 400)
                    self.assertNotIn("Traceback", response.body())
                    if path == "/mcp":
                        self.assertEqual(response.json()["error"]["code"], -32700)

    def test_valid_json_with_invalid_mcp_shape_keeps_protocol_error_status(self):
        response = call_handler(object(), "/mcp", method="POST", body="[[[0]]]")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.json()["error"]["code"], -32600)


if __name__ == "__main__":
    unittest.main()
