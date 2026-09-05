from __future__ import annotations

import json
import sys
from typing import Any, Callable, Iterable, Optional

from sekaisync import __version__
from sekaisync.core import SekaiSyncCore
from sekaisync.tools import MCP_NAME_TO_SPEC, ToolSpec, coerce_args, mcp_tools_list

PROTOCOL_VERSION = "2024-11-05"


def _text_result(data: Any) -> dict:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(data, ensure_ascii=False),
            }
        ]
    }


class McpServer:
    def __init__(
        self,
        core: SekaiSyncCore,
        sites_provider: Optional[Callable[[], Iterable[Any]]] = None,
    ):
        self.core = core
        self.sites_provider = sites_provider

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

    def handle(self, message: dict) -> dict | None:
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if request_id is None:
            return None
        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "SekaiSync", "version": __version__},
                },
            }
        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"tools": mcp_tools_list(_all_tool_specs())},
            }
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            spec = MCP_NAME_TO_SPEC.get(name)
            if spec is None:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                }
            try:
                result = self._dispatch_tool(spec, arguments)
            except Exception as exc:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
            if spec.wrap == "gaps":
                result = {"gaps": result}
            return {"jsonrpc": "2.0", "id": request_id, "result": _text_result(result)}
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }

    def run(self) -> int:
        for line in sys.stdin:
            line = line.strip().lstrip("\ufeff")
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = self.handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        return 0


def _all_tool_specs():
    from sekaisync.tools import TOOLS

    return TOOLS


def run_mcp_server(core: SekaiSyncCore) -> int:
    return McpServer(core).run()
