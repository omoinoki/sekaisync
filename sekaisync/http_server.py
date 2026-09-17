from __future__ import annotations

import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from sekaisync import __version__
from sekaisync.config import SiteSettings, load_site_profile
from sekaisync.core import SekaiSyncCore
from sekaisync.mcp_server import (
    INVALID_REQUEST,
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    McpServer,
)
from sekaisync.tools import (
    HTTP_GET_ROUTES,
    HTTP_POST_ROUTES,
    TOOLS,
    ParamError,
    ToolSpec,
    build_openapi,
    coerce_args,
    mcp_tools_list,
)

# ---------------------------------------------------------------------------
# P15 provisional local-boundary budgets (Astra B1; calibrate later).
# ---------------------------------------------------------------------------
MAX_BODY_BYTES = 1024 * 1024  # one request body
BODY_READ_TIMEOUT_SECONDS = 10  # a slow body must not hold a worker forever
MAX_CONCURRENT_REQUESTS = 16  # concurrency cap for the threaded server
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback_host(host: str) -> bool:
    """True when ``host`` denotes this machine only.

    Wildcard binds (``0.0.0.0``, ``::``, ``*``) are deliberately NOT
    loopback: they expose the port on every interface, which is exactly the
    unauthenticated remote deployment P15 refuses.
    """
    token = (host or "").strip().strip("[]").lower()
    if token in LOOPBACK_HOSTS:
        return True
    if token in {"0.0.0.0", "::", "*"}:
        return False
    try:
        infos = socket.getaddrinfo(token, None)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        addr = info[4][0]
        if addr.startswith("127."):
            return True
        if addr in {"::1", "0:0:0:0:0:0:0:1"}:
            return True
    return False


# The OpenAPI document is DERIVED from the ToolSpec/Param registry in
# ``tools.py`` (see ``build_openapi``), not hand-maintained here.  The
# previous literal constant had already drifted: it still advertised
# ``GET /api/v1/events/check`` and dropped ``news``'s parameters.
OPENAPI = build_openapi(TOOLS)


class SekaiSyncHandler(BaseHTTPRequestHandler):
    core: SekaiSyncCore
    sites: tuple[SiteSettings, ...] = ()
    # The address the server is actually bound to, injected by ``serve_http``.
    bound_host: str = "127.0.0.1"
    bound_port: int = 0
    allowed_origins: tuple[str, ...] = ()
    request_slots: Optional[threading.BoundedSemaphore] = None

    def _sites_provider(self):
        return self.sites

    # -- local boundary ---------------------------------------------------

    def _header(self, name: str, default: str = "") -> str:
        """Read one header, tolerating the mapping stub used by unit tests."""
        try:
            value = self.headers.get(name, default)
        except AttributeError:
            return default
        return default if value is None else str(value)

    def _header_count(self, name: str) -> int:
        try:
            return sum(1 for key, _ in self.headers.items() if key.lower() == name.lower())
        except AttributeError:
            return 0

    def _allowed_ports(self) -> set:
        ports = set()
        bound = getattr(self, "bound_port", 0) or 0
        if bound:
            ports.add(int(bound))
        address = getattr(getattr(self, "server", None), "server_address", None)
        if address:
            try:
                ports.add(int(address[1]))
            except (TypeError, ValueError, IndexError):
                pass
        return ports

    def _origin_allowed(self, origin: str) -> bool:
        return origin in tuple(getattr(self, "allowed_origins", ()) or ())

    def _cors_headers(self) -> dict:
        """CORS is opt-in per origin; there is no ``*`` wildcard anymore."""
        origin = self._header("Origin")
        if origin and self._origin_allowed(origin):
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}
        return {}

    def _reject(self, status: int, message: str, extra: Optional[dict] = None) -> None:
        """Send a rejection with no permissive CORS headers."""
        body = json.dumps({"error": message}, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _validate_host(self) -> bool:
        """Host must name the loopback address the server actually bound."""
        if self._header_count("Host") > 1:
            self._reject(403, "Duplicate Host header")
            return False
        host_header = self._header("Host").strip()
        if not host_header:
            self._reject(403, "Missing Host header")
            return False
        host, _, port_text = host_header.rpartition(":")
        if not host:  # no port given
            host, port_text = host_header, ""
        if host.startswith("[") and host.endswith("]"):
            host = host[1:-1]
        if not is_loopback_host(host):
            self._reject(
                403,
                "Host must be a loopback address; remote deployment needs separate "
                "authentication and is not supported by this local service.",
            )
            return False
        allowed = self._allowed_ports()
        if port_text:
            if not port_text.isdigit():
                self._reject(403, "Invalid Host port")
                return False
            if allowed and int(port_text) not in allowed:
                self._reject(403, "Host port does not match the bound port")
                return False
        elif allowed and not allowed & {80, 443}:
            # A Host without a port means the scheme's default port, which
            # cannot be the dynamic/ephemeral port this server is bound to.
            self._reject(403, "Host port does not match the bound port")
            return False
        return True

    def _validate_origin(self) -> bool:
        """No Origin (local CLI/Node) is fine; browser cross-origin is not."""
        if self._header_count("Origin") > 1:
            self._reject(403, "Duplicate Origin header")
            return False
        origin = self._header("Origin").strip()
        if not origin:
            return True
        # The literal "null" Origin (sandboxed iframe, file://, some
        # redirects) carries no usable identity and is rejected unless it was
        # explicitly allow-listed.
        if not self._origin_allowed(origin):
            self._reject(
                403,
                "Cross-origin browser requests are not allowed; this service is "
                "for local clients only.",
            )
            return False
        return True

    def _validate_request_boundary(self, *, body_expected: bool) -> Optional[int]:
        """Validate Host/Origin/length/type before the body is read.

        Returns the declared body length, or ``None`` after a rejection has
        already been sent.  Nothing is read from the socket until authority
        has been established.
        """
        if not self._validate_host():
            return None
        if not self._validate_origin():
            return None
        transfer_encoding = self._header("Transfer-Encoding").strip().lower()
        if transfer_encoding and transfer_encoding != "identity":
            self._reject(400, "Unsupported Transfer-Encoding")
            return None
        if self._header_count("Content-Length") > 1:
            self._reject(400, "Duplicate Content-Length header")
            return None
        length_text = self._header("Content-Length").strip()
        if not length_text:
            length = 0
        else:
            try:
                length = int(length_text)
            except ValueError:
                self._reject(400, "Invalid Content-Length")
                return None
            if length < 0:
                self._reject(400, "Invalid Content-Length")
                return None
        if length > MAX_BODY_BYTES:
            self._reject(413, "Request body exceeds %d bytes" % MAX_BODY_BYTES)
            return None
        if body_expected and length > 0:
            content_type = self._header("Content-Type").split(";")[0].strip().lower()
            if content_type != "application/json":
                self._reject(400, "Content-Type must be application/json")
                return None
        return length

    def _read_body(self, length: int):
        """Read exactly ``length`` bytes under a read timeout."""
        connection = getattr(self, "connection", None)
        previous = None
        if connection is not None and hasattr(connection, "settimeout"):
            try:
                previous = connection.gettimeout()
                connection.settimeout(BODY_READ_TIMEOUT_SECONDS)
            except OSError:
                previous = None
        try:
            data = self.rfile.read(length)
        except (socket.timeout, TimeoutError, OSError):
            self._reject(400, "Timed out while reading the request body")
            return None
        finally:
            if connection is not None and previous is not None:
                try:
                    connection.settimeout(previous)
                except OSError:
                    pass
        if data is None or len(data) != length:
            self._reject(400, "Request body shorter than Content-Length")
            return None
        return data

    def _try_reserve_slot(self) -> bool:
        semaphore = getattr(self, "request_slots", None)
        if semaphore is None:
            return True
        if not semaphore.acquire(blocking=False):
            self._reject(503, "Server is busy; retry shortly")
            return False
        self._slot_reserved = True
        return True

    def _release_slot(self) -> None:
        if getattr(self, "_slot_reserved", False):
            semaphore = getattr(self, "request_slots", None)
            if semaphore is not None:
                try:
                    semaphore.release()
                except ValueError:
                    pass
            self._slot_reserved = False

    def _send_mcp_json(self, payload: Any) -> None:
        # The advertised revision is the server's newest supported one.  A
        # per-session negotiated version would need session state (the
        # ``Mcp-Session-Id`` mechanism), which this stateless server does not
        # implement; the ``initialize`` result carries the negotiated value,
        # which is what the client binds to.
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            # Serialize before touching the transport so a failure cannot
            # corrupt a half-written response; recover as a sanitized
            # internal error (ASCII, always encodable).
            request_id = payload.get("id") if isinstance(payload, dict) else None
            if isinstance(request_id, bool) or not isinstance(request_id, (str, int)):
                request_id = None
            body = json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32603, "message": "Internal error"},
            }).encode("utf-8")
        accept = self._header("Accept")
        cors = self._cors_headers()
        if "text/event-stream" in accept:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
            for key, value in cors.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(f"event: message\ndata: {body.decode('utf-8')}\n\n".encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        for key, value in cors.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        try:
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError):
            # Serialization failure must not raise out of the handler (it
            # would kill the connection mid-response); fall back to a plain
            # internal error envelope.  ASCII keeps this fallback always
            # encodable, including for an invalid-Unicode id or a lone
            # surrogate that the primary dumps() call leaked into a str.
            body = b'{"error": "Internal error"}'
            status = 500
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        # No CORS permissions are granted by default: an unconfigured
        # allow-list means no browser origin is accepted.
        if not self._validate_host():
            return
        origin = self._header("Origin").strip()
        if origin and not self._origin_allowed(origin):
            self._reject(403, "Cross-origin browser requests are not allowed")
            return
        self.send_response(204)
        self.send_header("Allow", "GET, POST, OPTIONS")
        for key, value in self._cors_headers().items():
            self.send_header(key, value)
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self._reject_unsupported_method()

    def _reject_unsupported_method(self) -> None:
        """Any method outside GET/POST/OPTIONS is 405, not a 501 HTML page."""
        self._reject(405, "Method not allowed", {"Allow": "GET, POST, OPTIONS"})

    def do_PUT(self) -> None:  # noqa: N802
        self._reject_unsupported_method()

    def do_DELETE(self) -> None:  # noqa: N802
        self._reject_unsupported_method()

    def do_PATCH(self) -> None:  # noqa: N802
        self._reject_unsupported_method()

    def do_TRACE(self) -> None:  # noqa: N802
        self._reject_unsupported_method()

    def do_CONNECT(self) -> None:  # noqa: N802
        self._reject_unsupported_method()

    def _run_tool(self, spec: ToolSpec, get_raw, raw_query: str = "") -> Any:
        try:
            kwargs = coerce_args(spec, {}, endpoint="http", get_raw=get_raw)
        except ParamError as exc:
            self._send_json(400, {"error": str(exc)})
            return None
        if spec.core_method is None:
            if spec.name == "sites":
                return {"sites": [site.to_dict() for site in self.sites]}
            raise ValueError(f"Endpoint {spec.http_path} has no handler")
        try:
            result = getattr(self.core, spec.core_method)(**kwargs)
        except ParamError as exc:
            self._send_json(400, {"error": str(exc)})
            return None
        except Exception:  # noqa: BLE001 - sanitized: no traceback to the client
            self._send_json(500, {"error": "Internal error"})
            return None
        if spec.http_not_found is not None and result is None:
            message = spec.http_not_found
            try:
                message = spec.http_not_found.format(**kwargs)
            except (KeyError, IndexError):
                pass
            self._send_json(404, {"error": message})
            return None
        if spec.wrap == "query_results":
            return {"query": raw_query, "results": result}
        if spec.wrap == "results":
            return {"results": result}
        if spec.wrap == "browse_results":
            return {"results": result}
        if spec.wrap == "gaps":
            return {"gaps": result}
        if spec.wrap == "refresh":
            return {"refreshed": True, "counts": result}
        return result

    def _method_not_allowed(self, path: str) -> bool:
        """405 (with Allow) when the path exists under another method."""
        allowed = [
            method
            for table, method in ((HTTP_GET_ROUTES, "GET"), (HTTP_POST_ROUTES, "POST"))
            if path in table
        ]
        if not allowed:
            return False
        self._reject(
            405,
            "Method not allowed for this path",
            {"Allow": ", ".join([*allowed, "OPTIONS"])},
        )
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._try_reserve_slot():
            return
        try:
            length = self._validate_request_boundary(body_expected=False)
            if length is None:
                return
            parsed = urlparse(self.path)
            # keep_blank_values: `?body=` must stay visible as an explicitly
            # blank value (and be rejected), not silently vanish into
            # "parameter absent".
            query = parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == "/health":
                self._send_json(200, {"status": "ok", "ready": self.core.ready()})
                return
            if parsed.path == "/openapi.json":
                self._send_json(200, OPENAPI)
                return
            if parsed.path == "/.well-known/mcp.json":
                self._send_json(200, mcp_discovery_document())
                return
            spec = HTTP_GET_ROUTES.get(parsed.path)
            if spec is None:
                if self._method_not_allowed(parsed.path):
                    return
                self._send_json(404, {"error": "Not found"})
                return
            payload = self._run_tool(
                spec,
                get_raw=lambda name: query.get(name, [None])[0],
                raw_query=query.get("query", [""])[0],
            )
            if payload is not None:
                self._send_json(200, payload)
        finally:
            self._release_slot()

    def do_POST(self) -> None:  # noqa: N802
        if not self._try_reserve_slot():
            return
        try:
            length = self._validate_request_boundary(body_expected=True)
            if length is None:
                return
            parsed = urlparse(self.path)
            if parsed.path == "/mcp":
                self._handle_mcp_post(length)
                return
            spec = HTTP_POST_ROUTES.get(parsed.path)
            if spec is None:
                if self._method_not_allowed(parsed.path):
                    return
                self._send_json(404, {"error": "Not found"})
                return
            raw = self._read_body(length)
            if raw is None:
                return
            if not raw:
                body = {}
            else:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(400, {"error": "Invalid JSON body"})
                    return
            if not isinstance(body, dict):
                self._send_json(400, {"error": "JSON body must be an object"})
                return
            payload = self._run_tool(spec, get_raw=lambda name: body.get(name))
            if payload is not None:
                self._send_json(200, payload)
        finally:
            self._release_slot()

    def _validate_mcp_protocol_version(self) -> bool:
        """Enforce the ``MCP-Protocol-Version`` request header on ``/mcp``.

        2025-06-18 Streamable HTTP: *"If the server receives a request with an
        invalid or unsupported ``MCP-Protocol-Version``, it MUST respond with
        ``400 Bad Request``."*  A **present** but unsupported value is refused
        here, before the body is read, instead of being silently ignored --
        silent acceptance is the same defect ``initialize`` used to have.

        An **absent** header is not refused: the revision says a server that
        cannot otherwise identify the version SHOULD assume ``2025-03-26``,
        and that assumption would make every header-less local client fail
        against a server that only speaks :data:`SUPPORTED_VERSIONS`.  Since
        this server does not implement that fallback, it treats an absent
        header as "nothing to check" -- ``initialize`` is still the authority
        on the version.
        """
        declared = self._header("MCP-Protocol-Version").strip()
        if not declared or declared in SUPPORTED_VERSIONS:
            return True
        self._send_json(400, {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": INVALID_REQUEST,
                "message": f"Unsupported MCP-Protocol-Version: {declared}",
                "data": {"supported": list(SUPPORTED_VERSIONS), "requested": declared},
            },
        })
        return False

    def _handle_mcp_post(self, length: int) -> None:
        if not self._validate_mcp_protocol_version():
            return
        raw = self._read_body(length)
        if raw is None:
            return
        try:
            message = json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(
                400,
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            )
            return
        response = handle_mcp_message(self.core, message, sites_provider=self._sites_provider)
        if response is None:
            # A valid notification gets 202 with no body.
            self.send_response(202)
            for key, value in self._cors_headers().items():
                self.send_header(key, value)
            self.end_headers()
            return
        self._send_mcp_json(response)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def mcp_discovery_document() -> dict:
    """MCP 发现清单（``/.well-known/mcp.json``）。

    工具列表**从 tools 注册表实时生成**，不手写——手写副本迟早与本尊漂移
    （这个项目的文档数字漂移已是一类被记录过的问题）。同生态先例：
    ``https://pjsk.moe/.well-known/mcp.json``，Agent 可零配置自动发现。
    """
    return {
        "name": "SekaiSync",
        "description": (
            "Local knowledge base and deterministic fact layer for Project Sekai "
            "(registry, glossary, cross-language terminology, crawled story text, "
            "official news). Zero third-party dependencies."
        ),
        "version": __version__,
        "serverInfo": {"name": "SekaiSync", "version": __version__},
        "authentication": {
            "type": "none",
            "description": "Local service bound to 127.0.0.1 by default; no authentication required.",
        },
        "capabilities": {"tools": True, "resources": True, "prompts": False},
        "transport": {
            "mcp": "/mcp",
            "stdio": "python -m sekaisync serve-mcp",
            "openapi": "/openapi.json",
        },
        "tools": mcp_tools_list(TOOLS),
    }


def handle_mcp_message(
    core: SekaiSyncCore,
    message: dict,
    sites_provider=None,
) -> dict | None:
    return McpServer(core, sites_provider=sites_provider).handle(message)


def serve_http(
    core: SekaiSyncCore,
    host: str = "127.0.0.1",
    port: int = 8787,
    sites: Optional[Iterable[SiteSettings]] = None,
) -> None:
    if sites is None:
        sites = load_site_profile(Path(__file__).resolve().parent.parent)
    profile = tuple(sites)
    # P15: this service has no authentication, so it may only listen on the
    # loopback interface.  Remote exposure is refused with an explanation
    # instead of silently publishing an unauthenticated knowledge base.
    if not is_loopback_host(host):
        raise SystemExit(
            f"Refusing to bind SekaiSync HTTP to non-loopback address {host!r}.\n"
            "This server has no authentication and must not be reachable from "
            "other machines.\n"
            "Remote deployment needs a separate authenticated reverse proxy or "
            "an SSH tunnel; run it on 127.0.0.1 and forward the port instead."
        )
    allowed_origins = tuple(
        origin.strip()
        for origin in os.environ.get("SEKAISYNC_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )
    slots = threading.BoundedSemaphore(MAX_CONCURRENT_REQUESTS)
    handler = type(
        "BoundSekaiSyncHandler",
        (SekaiSyncHandler,),
        {
            "core": core,
            "sites": profile,
            "bound_host": host,
            "bound_port": port,
            "allowed_origins": allowed_origins,
            "request_slots": slots,
        },
    )
    server = ThreadingHTTPServer((host, port), handler)
    # When port 0 is requested the OS assigns a dynamic port; report the bound
    # one so consumers (e.g. the dsh plugin spawning --port 0) can discover it.
    actual_port = server.server_address[1]
    handler.bound_port = actual_port
    print(f"SekaiSync HTTP server listening on http://{host}:{actual_port}")
    print(f"OpenAPI: http://{host}:{actual_port}/openapi.json")
    print(f"MCP Streamable HTTP: http://{host}:{actual_port}/mcp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


