"""MCP server: JSON-RPC 2.0 envelope validation, tool dispatch, stdio loop.

P15/D15 hardening:
* bounded-length stdio reads (an over-long frame is discarded to its newline
  and never re-parsed from the leftover half-line),
* strict envelope validation with the standard JSON-RPC error codes,
* business failures returned as ``isError`` tool results, internal
  exceptions sanitized (no traceback reaches the client),
* stdout carries protocol lines only.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable, Optional

from sekaisync import __version__
from sekaisync.core import SekaiSyncCore
from sekaisync.tools import (
    MCP_NAME_TO_SPEC,
    ParamError,
    ToolSpec,
    coerce_args,
    mcp_tools_list,
)

PROTOCOL_VERSION = "2024-11-05"

# P15 provisional budget: one stdio frame.  Marked provisional because a real
# fixture with a large legitimate payload has not calibrated it yet.
MAX_STDIO_LINE_BYTES = 1024 * 1024

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _text_result(data: Any) -> dict:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(data, ensure_ascii=False),
            }
        ]
    }


RESOURCE_SPECS = [
    {
        "uri": "sekaisync://gaps",
        "name": "已知数据缺口",
        "description": (
            "Known data source limitations (home_line gaps, overseas MySekai missing, "
            "countdown videos without text, ...) with agent_guidance. Read this before "
            "claiming an entity is uncovered."
        ),
        "mimeType": "application/json",
    },
    {
        "uri": "sekaisync://registry/summary",
        "name": "索引规模与新鲜度",
        "description": "Entity/web/term counts plus per-region freshness, without the full status aggregate.",
        "mimeType": "application/json",
    },
    {
        "uri": "sekaisync://terms/{query}",
        "name": "术语查询",
        "description": "Cross-language names for a term, addressed by URI (e.g. sekaisync://terms/ネットパラダイス).",
        "mimeType": "application/json",
    },
    {
        "uri": "sekaisync://news/{language}",
        "name": "公告列表",
        "description": "Official announcements for one language (ja/en/zh_hans/zh_hant/ko); omit the language for all.",
        "mimeType": "application/json",
    },
]


class McpServer:
    def __init__(
        self,
        core: SekaiSyncCore,
        sites_provider: Optional[Callable[[], Iterable[Any]]] = None,
    ):
        self.core = core
        self.sites_provider = sites_provider

    def _read_resource(self, uri: str) -> Any:
        """读取只读上下文资源。

        resources 的价值在于：把 Agent 常需的**背景上下文**（数据缺口声明、
        索引规模、公告列表）作为可寻址的只读 URI 暴露，而不必为它们各造一个
        tool —— 后者会把工具列表撑大、增加每轮 prefill 成本。全部复用 core
        的既有查询方法，不重复实现逻辑。
        """
        key, _, arg = uri.partition("://")
        if key != "sekaisync":
            raise KeyError(uri)
        path = arg.strip("/")
        if path == "gaps":
            return {"gaps": self.core.data_gaps()}
        if path == "registry/summary":
            status = self.core.status()
            return {
                "master": status.get("master"),
                "web_sources": (status.get("web") or {}).get("sources"),
                "terms": status.get("terms"),
                "freshness": status.get("freshness"),
            }
        if path.startswith("terms/"):
            query = path[len("terms/"):].strip()
            if not query:
                raise KeyError(uri)
            results = self.core.term_lookup(query, limit=5)
            # 资源要精简：evidence 句子可占数百 KB，而 resources 的意义正是
            # 比 tool 调用更省 token。保留计数、丢弃句子正文（需要时用
            # sekaisync_term_lookup 工具取全文）。
            lean = []
            for row in results:
                item = {k: v for k, v in row.items() if k != "evidence"}
                evidence = row.get("evidence") or []
                if evidence:
                    item["evidence_count"] = len(evidence)
                lean.append(item)
            return {"query": query, "count": len(lean), "results": lean}
        if path.startswith("news/"):
            language = path[len("news/"):].strip() or None
            return self.core.news(limit=50, language=language)
        raise KeyError(uri)

    def _dispatch_tool(self, spec: ToolSpec, arguments: dict) -> Any:
        if spec.core_method is None:
            if spec.name == "sites":
                sites = tuple(self.sites_provider() or ())
                return {"sites": [site.to_dict() for site in sites]}
            raise ValueError(f"Tool {spec.mcp_name} has no handler")
        kwargs = coerce_args(
            spec,
            arguments,
            endpoint="mcp",
            get_raw=lambda name: arguments.get(name),
        )
        return getattr(self.core, spec.core_method)(**kwargs)

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    # -- envelope ---------------------------------------------------------

    def handle(self, message: Any) -> dict | None:
        """Validate one JSON-RPC message and dispatch it.

        Returns the response object, or ``None`` for a notification (no
        ``id`` member at all).  The envelope checks are ordered so that a
        structurally invalid message can never reach a tool: non-object ->
        -32600, wrong ``jsonrpc``/``method``/``params`` types -> -32600,
        unknown method -> -32601, bad tool arguments -> -32602.
        """
        if not isinstance(message, dict):
            # `[]`, `"x"`, `1` are invalid requests, not silently dropped.
            return self._error(None, INVALID_REQUEST, "Request must be a JSON object")
        if message.get("jsonrpc") != "2.0":
            return self._error(
                message.get("id"), INVALID_REQUEST, "jsonrpc must be \"2.0\""
            )
        method = message.get("method")
        if not isinstance(method, str) or not method:
            return self._error(
                message.get("id"), INVALID_REQUEST, "method must be a non-empty string"
            )
        # A missing `id` member is a notification; an explicit `null` is a
        # request whose id is null (it must still get a response).  Booleans
        # are not valid ids in any MCP version.
        has_id = "id" in message
        request_id = message.get("id")
        if isinstance(request_id, bool):
            return self._error(None, INVALID_REQUEST, "id must not be a boolean")
        if has_id and request_id is not None and not isinstance(request_id, (str, int)):
            return self._error(None, INVALID_REQUEST, "id must be a string, number or null")
        params = message.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(request_id, INVALID_PARAMS, "params must be an object")
        if not has_id:
            self._handle_notification(method)
            return None
        return self._handle_request(request_id, method, params)

    def _handle_notification(self, method: str) -> None:
        """Notifications get no response but still run their state handling.

        The current server has no server-side state to advance on
        ``notifications/initialized`` / ``notifications/cancelled``; the
        hook exists so that adding it later does not change the protocol
        shape.  Unknown notifications are ignored, per JSON-RPC 2.0.
        """
        return

    def _handle_request(self, request_id: Any, method: str, params: dict) -> dict:
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": "SekaiSync", "version": __version__},
                },
            }
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return self._error(request_id, INVALID_REQUEST, "Notification must not carry an id")
        if method == "ping":
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": mcp_tools_list(_all_tool_specs())},
            }
        if method == "resources/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"resources": RESOURCE_SPECS},
            }
        if method == "resources/read":
            uri = params.get("uri")
            if not isinstance(uri, str) or not uri:
                return self._error(
                    request_id, INVALID_PARAMS, "resources/read requires a string 'uri'"
                )
            try:
                payload = self._read_resource(uri)
            except KeyError:
                return self._error(request_id, INVALID_PARAMS, f"Unknown resource: {uri}")
            except Exception:  # noqa: BLE001 - sanitized: no traceback to the client
                return self._error(request_id, INTERNAL_ERROR, "Internal error")
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "contents": [
                        {
                            "uri": uri,
                            "mimeType": "application/json",
                            "text": json.dumps(payload, ensure_ascii=False),
                        }
                    ]
                },
            }
        if method == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not name:
                return self._error(
                    request_id, INVALID_PARAMS, "tools/call requires a string 'name'"
                )
            arguments = params.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return self._error(request_id, INVALID_PARAMS, "'arguments' must be an object")
            spec = MCP_NAME_TO_SPEC.get(name)
            if spec is None:
                return self._error(request_id, METHOD_NOT_FOUND, f"Unknown tool: {name}")
            try:
                result = self._dispatch_tool(spec, arguments)
            except ParamError as exc:
                return self._error(request_id, INVALID_PARAMS, str(exc))
            except Exception as exc:  # noqa: BLE001 - sanitized: no traceback to the client
                # A business failure is reported as a compliant tool result
                # with isError, not as a transport-level error, so the client
                # can see which call failed.  The text carries the exception
                # type only; details stay server-side.
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Tool execution failed: {type(exc).__name__}",
                            }
                        ],
                        "isError": True,
                    },
                }
            if spec.wrap == "gaps":
                result = {"gaps": result}
            return {"jsonrpc": "2.0", "id": request_id, "result": _text_result(result)}
        return self._error(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    # -- stdio ------------------------------------------------------------

    def handle_line(self, line: str) -> Optional[dict]:
        """Parse and handle one stdio frame.

        Returns ``None`` when there is nothing to write (blank line, valid
        notification) and a response object otherwise.  A JSON parse error
        becomes a -32700 response with a null id so the caller can log it
        and keep reading.
        """
        text = line.strip().lstrip("\ufeff")
        if not text:
            return None
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return self._error(None, PARSE_ERROR, "Parse error")
        return self.handle(message)

    def run(self, stdin=None, stdout=None) -> int:
        stdin = stdin if stdin is not None else sys.stdin
        stdout = stdout if stdout is not None else sys.stdout
        while True:
            line = _read_bounded_line(stdin, MAX_STDIO_LINE_BYTES)
            if line is None:
                break
            if line is _OVERSIZE:
                # The over-long frame is discarded whole: the rest of the
                # line is consumed, never re-parsed as a new request.
                log_stderr(
                    f"sekaisync: discarded an over-long request frame "
                    f"(> {MAX_STDIO_LINE_BYTES} bytes)"
                )
                response = self._error(
                    None,
                    INVALID_REQUEST,
                    f"Request exceeds {MAX_STDIO_LINE_BYTES} bytes",
                )
                _write_line(stdout, response)
                continue
            response = self.handle_line(line)
            if response is not None:
                _write_line(stdout, response)
        return 0


def _all_tool_specs():
    from sekaisync.tools import TOOLS

    return TOOLS


class _Oversize:
    """Sentinel: the frame was longer than the budget and was discarded."""

    __slots__ = ()


_OVERSIZE = _Oversize()


def _read_bounded_line(stream, limit: int):
    """Read one line, discarding the remainder once ``limit`` is exceeded.

    Returns ``None`` at EOF, the line otherwise, or :data:`_OVERSIZE` when
    the frame was over budget.  The defining property is that an over-long
    frame is **consumed to its newline**: the leftover half-line is never
    handed back as if it were the next request.
    """
    line = stream.readline(limit + 1)
    if line == "":
        return None
    if line.endswith("\n") or len(line) <= limit:
        return line
    # Over budget: the newline has not been seen yet, so drain the rest of
    # the frame before reporting it as discarded.
    while True:
        rest = stream.readline()
        if rest == "" or rest.endswith("\n"):
            break
    return _OVERSIZE


def _write_line(stdout, payload: dict) -> None:
    stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    try:
        stdout.flush()
    except (AttributeError, ValueError):
        pass


def log_stderr(message: str) -> None:
    """Operational logging goes to stderr only; stdout carries protocol lines."""
    print(message, file=sys.stderr, flush=True)


def run_mcp_server(core: SekaiSyncCore) -> int:
    return McpServer(core).run()
