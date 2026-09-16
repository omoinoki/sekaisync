from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import parse_qs, urlparse

from sekaisync import __version__
from sekaisync.config import SiteSettings, load_site_profile
from sekaisync.core import SekaiSyncCore
from sekaisync.mcp_server import PROTOCOL_VERSION, McpServer
from sekaisync.tools import (
    HTTP_GET_ROUTES,
    HTTP_POST_ROUTES,
    TOOLS,
    ToolSpec,
    coerce_args,
    mcp_tools_list,
)


OPENAPI = {
    "openapi": "3.1.0",
    "info": {"title": "SekaiSync Local API", "version": __version__},
    "servers": [{"url": "http://127.0.0.1:8787"}],
    "paths": {
        "/api/v1/status": {
            "get": {
                "operationId": "status",
                "responses": {"200": {"description": "Store, master, web and freshness status"}},
            }
        },
        "/api/v1/refresh": {
            "post": {
                "operationId": "refresh",
                "responses": {"200": {"description": "Reload cached indexes from disk"}},
            }
        },
        "/api/v1/sites": {
            "get": {
                "operationId": "sites",
                "responses": {"200": {"description": "Configured site profile from settings.json"}},
            }
        },
        "/api/v1/lookup": {
            "get": {
                "operationId": "lookup",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "type", "in": "query", "schema": {"type": "string"}},
                    {"name": "region", "in": "query", "schema": {"type": "string"}},
                    {"name": "language", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Entity matches"}},
            }
        },
        "/api/v1/fact_pack": {
            "get": {
                "operationId": "factPack",
                "parameters": [
                    {"name": "entity_id", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "language", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Compact fact pack"}},
            }
        },
        "/api/v1/freshness": {
            "get": {
                "operationId": "freshness",
                "responses": {"200": {"description": "Data freshness"}},
            }
        },
        "/api/v1/progress": {
            "get": {
                "operationId": "progress",
                "parameters": [
                    {"name": "regions", "in": "query", "schema": {"type": "string"}},
                    {"name": "live", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": {"200": {"description": "Per-region completeness percentages"}},
            }
        },
        "/api/v1/trust": {
            "get": {
                "operationId": "trust",
                "responses": {"200": {"description": "A/B/C/D trust distribution"}},
            }
        },
        "/api/v1/integrity": {
            "get": {
                "operationId": "integrity",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}}
                ],
                "responses": {"200": {"description": "Dedup and fidelity integrity report"}},
            }
        },
        "/api/v1/news": {
            "get": {
                "operationId": "news",
                "parameters": [
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}}
                ],
                "responses": {"200": {"description": "Synced official news and announcements"}},
            }
        },
        "/api/v1/web_lookup": {
            "get": {
                "operationId": "webLookup",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "source", "in": "query", "schema": {"type": "string"}},
                    {"name": "language", "in": "query", "schema": {"type": "string"}},
                    {"name": "kind", "in": "query", "schema": {"type": "string"}},
                    {"name": "include_text", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "include_overlay", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "max_text_chars", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "Crawled web text matches"}},
            }
        },
        "/api/v1/web_browse": {
            "get": {
                "operationId": "webBrowse",
                "parameters": [
                    {"name": "source", "in": "query", "schema": {"type": "string"}},
                    {"name": "language", "in": "query", "schema": {"type": "string"}},
                    {"name": "kind", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "include_text", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": {"200": {"description": "Crawled web text filtered by source/category"}},
            }
        },
        "/api/v1/resolve": {
            "get": {
                "operationId": "resolve",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "target_language", "in": "query", "schema": {"type": "string"}},
                    {"name": "source_language", "in": "query", "schema": {"type": "string"}},
                    {"name": "kind", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Official localized name matches"}},
            }
        },
        "/api/v1/term_lookup": {
            "get": {
                "operationId": "termLookup",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "language", "in": "query", "schema": {"type": "string"}},
                    {"name": "languages", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                    {"name": "tag", "in": "query", "schema": {"type": "string"}},
                    {"name": "sort", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Extracted terms and their cross-language names"}},
            }
        },
        "/api/v1/term_penetrate": {
            "get": {
                "operationId": "termPenetrate",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "story_key", "in": "query", "schema": {"type": "string"}},
                    {"name": "languages", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "Cross-language per-line penetration for a term at a story position"}},
            }
        },
        "/api/v1/tag_clouds": {
            "get": {
                "operationId": "tagClouds",
                "responses": {"200": {"description": "Tag clouds split by released(multi-lang) vs unreleased(ja-only)"}},
            }
        },
        "/api/v1/data_gaps": {
            "get": {
                "operationId": "dataGaps",
                "responses": {"200": {"description": "Known data source limitations visible to agents"}},
            }
        },
        "/api/v1/events/check": {
            "get": {
                "operationId": "eventCheck",
                "parameters": [
                    {"name": "regions", "in": "query", "schema": {"type": "string"}},
                    {"name": "timeout", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {
                    "200": {"description": "New-event detection, base-data sync and classification"}
                },
            }
        },
        "/api/v1/events/archive": {
            "get": {
                "operationId": "eventArchive",
                "parameters": [
                    {"name": "regions", "in": "query", "schema": {"type": "string"}},
                    {"name": "limit", "in": "query", "schema": {"type": "integer"}},
                ],
                "responses": {"200": {"description": "Archived event classifications by region"}},
            }
        },
        "/api/v1/event_alias": {
            "get": {
                "operationId": "eventAlias",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "regions", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "Community event shorthand resolved to box event"},
                    "404": {"description": "No matching alias or ordinal"},
                },
            }
        },
        "/api/v1/worldlink": {
            "get": {
                "operationId": "worldlink",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "regions", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "World Link shorthand resolved to world_bloom event"},
                    "404": {"description": "No matching World Link alias or ordinal"},
                },
            }
        },
        "/api/v1/activity": {
            "get": {
                "operationId": "activity",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "regions", "in": "query", "schema": {"type": "string"}},
                ],
                "responses": {
                    "200": {"description": "Shorthand resolved to a numbered activity (wl or box) or kind=unresolved"},
                },
            }
        },
        "/api/v1/query": {
            "get": {
                "operationId": "query",
                "parameters": [
                    {"name": "query", "in": "query", "required": True, "schema": {"type": "string"}},
                    {"name": "type", "in": "query", "schema": {"type": "string"}},
                    {"name": "region", "in": "query", "schema": {"type": "string"}},
                    {"name": "language", "in": "query", "schema": {"type": "string"}},
                    {"name": "include_overlay", "in": "query", "schema": {"type": "boolean"}},
                ],
                "responses": {"200": {"description": "Unified metadata and web text matches"}},
            }
        },
        "/api/v1/verify_claims": {
            "post": {
                "operationId": "verifyClaims",
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "claims": {"type": "array", "items": {"type": "object"}}
                                },
                                "required": ["claims"],
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "Verification results"}},
            }
        },
    },
}


class SekaiSyncHandler(BaseHTTPRequestHandler):
    core: SekaiSyncCore
    sites: tuple[SiteSettings, ...] = ()

    def _sites_provider(self):
        return self.sites

    def _send_mcp_json(self, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        accept = self.headers.get("Accept", "")
        if "text/event-stream" in accept:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
            self.end_headers()
            self.wfile.write(f"event: message\ndata: {body.decode('utf-8')}\n\n".encode("utf-8"))
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("MCP-Protocol-Version", PROTOCOL_VERSION)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def _run_tool(self, spec: ToolSpec, get_raw, raw_query: str = "") -> Any:
        kwargs = coerce_args(spec, {}, endpoint="http", get_raw=get_raw)
        if spec.core_method is None:
            if spec.name == "sites":
                return {"sites": [site.to_dict() for site in self.sites]}
            raise ValueError(f"Endpoint {spec.http_path} has no handler")
        result = getattr(self.core, spec.core_method)(**kwargs)
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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
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
            self._send_json(404, {"error": "Not found"})
            return
        payload = self._run_tool(
            spec,
            get_raw=lambda name: query.get(name, [None])[0],
            raw_query=query.get("query", [""])[0],
        )
        if payload is not None:
            self._send_json(200, payload)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                message = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            response = handle_mcp_message(self.core, message, sites_provider=self._sites_provider)
            if response is None:
                self.send_response(202)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return
            self._send_mcp_json(response)
            return
        spec = HTTP_POST_ROUTES.get(parsed.path)
        if spec is None:
            self._send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                body = {}
            else:
                body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        payload = self._run_tool(spec, get_raw=lambda name: body.get(name))
        if payload is not None:
            self._send_json(200, payload)

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
    handler = type(
        "BoundSekaiSyncHandler",
        (SekaiSyncHandler,),
        {"core": core, "sites": profile},
    )
    server = ThreadingHTTPServer((host, port), handler)
    # When port is 0 the OS assigns a dynamic port; report the bound one so
    # consumers (e.g. the dsh plugin spawning --port 0) can discover it.
    actual_port = server.server_address[1]
    print(f"SekaiSync HTTP server listening on http://{host}:{actual_port}")
    print(f"OpenAPI: http://{host}:{actual_port}/openapi.json")
    print(f"MCP Streamable HTTP: http://{host}:{actual_port}/mcp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


