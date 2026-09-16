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
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"subscribe": False, "listChanged": False},
                    },
                    "serverInfo": {"name": "SekaiSync", "version": __version__},
                },
            }
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
            uri = str(params.get("uri") or "")
            try:
                payload = self._read_resource(uri)
            except KeyError:
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": f"Unknown resource: {uri}"},
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
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
