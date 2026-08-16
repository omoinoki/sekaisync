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
                    {"name": "include_text", "in": "query", "schema": {"type": "boolean"}},
                    {"name": "include_overlay", "in": "query", "schema": {"type": "boolean"}},
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
                ],
                "responses": {"200": {"description": "Extracted terms and their cross-language names"}},
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

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        if parsed.path == "/health":
            self._send_json(200, {"status": "ok", "ready": self.core.ready()})
            return
        if parsed.path == "/openapi.json":
            self._send_json(200, OPENAPI)
            return
        if parsed.path == "/api/v1/status":
            self._send_json(200, self.core.status())
            return
        if parsed.path == "/api/v1/sites":
            self._send_json(
                200,
                {"sites": [site.to_dict() for site in self.sites]},
            )
            return
        if parsed.path == "/api/v1/lookup":
            try:
                limit = int(query.get("limit", ["8"])[0])
            except ValueError:
                limit = 8
            results = self.core.lookup(
                query.get("query", [""])[0],
                type=query.get("type", [None])[0],
                region=query.get("region", [None])[0],
                language=query.get("language", [None])[0],
                limit=limit,
            )
            self._send_json(200, {"query": query.get("query", [""])[0], "results": results})
            return
        if parsed.path == "/api/v1/resolve":
            target_language = query.get("target_language", ["zh_tw"])[0]
            source_language = query.get("source_language", [None])[0]
            kind = query.get("kind", [None])[0]
            results = self.core.resolve_name(
                query.get("query", [""])[0],
                target_language=target_language,
                source_language=source_language,
                kind=kind,
            )
            self._send_json(200, {"query": query.get("query", [""])[0], "results": results})
            return
        if parsed.path == "/api/v1/events/check":
            regions = [
                item.strip()
                for item in query.get("regions", [""])[0].split(",")
                if item.strip()
            ] or None
            try:
                timeout = int(query.get("timeout", ["30"])[0])
            except ValueError:
                timeout = 30
            result = self.core.event_check(regions=regions, timeout=timeout)
            self._send_json(200, result)
            return
        if parsed.path == "/api/v1/events/archive":
            regions = [
                item.strip()
                for item in query.get("regions", [""])[0].split(",")
                if item.strip()
            ] or None
            try:
                limit = int(query.get("limit", [""])[0])
            except ValueError:
                limit = None
            result = self.core.event_archive(regions=regions, limit=limit)
            self._send_json(200, result)
            return
        if parsed.path == "/api/v1/event_alias":
            regions = [
                item.strip()
                for item in query.get("regions", [""])[0].split(",")
                if item.strip()
            ] or None
            result = self.core.event_alias(
                query.get("query", [""])[0],
                regions=regions,
            )
            if result is None:
                self._send_json(404, {"error": "No matching event alias or ordinal"})
                return
            self._send_json(200, result)
            return
        if parsed.path == "/api/v1/term_lookup":
            try:
                limit = int(query.get("limit", ["8"])[0])
            except ValueError:
                limit = 8
            languages = [
                item.strip()
                for item in query.get("languages", ["ja,zh_hans,en,zh_tw,ko"])[0].split(",")
                if item.strip()
            ]
            results = self.core.term_lookup(
                query.get("query", [""])[0],
                source_language=query.get("language", [None])[0],
                languages=languages,
                limit=limit,
            )
            self._send_json(
                200,
                {
                    "query": query.get("query", [""])[0],
                    "results": results,
                },
            )
            return
        if parsed.path == "/api/v1/fact_pack":
            entity_id = query.get("entity_id", [""])[0]
            pack = self.core.fact_pack(entity_id, language=query.get("language", ["en"])[0])
            if pack is None:
                self._send_json(404, {"error": f"Entity not found: {entity_id}"})
                return
            self._send_json(200, pack)
            return
        if parsed.path == "/api/v1/freshness":
            self._send_json(200, self.core.freshness())
            return
        if parsed.path == "/api/v1/progress":
            raw_regions = query.get("regions", [None])[0]
            regions = (
                [item.strip() for item in raw_regions.split(",") if item.strip()]
                if raw_regions
                else None
            )
            live = query.get("live", ["false"])[0].lower() in {"1", "true", "yes"}
            self._send_json(200, self.core.progress(regions=regions, live=live))
            return
        if parsed.path == "/api/v1/trust":
            self._send_json(200, self.core.trust_summary())
            return
        if parsed.path == "/api/v1/integrity":
            try:
                limit = int(query.get("limit", ["20"])[0])
            except ValueError:
                limit = 20
            self._send_json(200, self.core.integrity(limit=limit))
            return
        if parsed.path == "/api/v1/news":
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            self._send_json(200, self.core.news(limit=limit))
            return
        if parsed.path == "/api/v1/web_lookup":
            include_text = query.get("include_text", ["false"])[0].lower() in {"1", "true", "yes"}
            include_overlay = query.get("include_overlay", ["false"])[0].lower() in {"1", "true", "yes"}
            try:
                limit = int(query.get("limit", ["8"])[0])
            except ValueError:
                limit = 8
            results = self.core.web_lookup(
                query.get("query", [""])[0],
                source=query.get("source", [None])[0],
                language=query.get("language", [None])[0],
                limit=limit,
                include_text=include_text,
                include_overlay=include_overlay,
            )
            self._send_json(200, {"query": query.get("query", [""])[0], "results": results})
            return
        if parsed.path == "/api/v1/web_browse":
            try:
                limit = int(query.get("limit", ["50"])[0])
            except ValueError:
                limit = 50
            include_text = query.get("include_text", ["false"])[0].lower() in {"1", "true", "yes"}
            results = self.core.web_browse(
                source=query.get("source", [None])[0],
                language=query.get("language", [None])[0],
                kind=query.get("kind", [None])[0],
                limit=limit,
                include_text=include_text,
            )
            self._send_json(200, {"results": results})
            return
        if parsed.path == "/api/v1/query":
            try:
                limit = int(query.get("limit", ["8"])[0])
            except ValueError:
                limit = 8
            include_overlay = query.get("include_overlay", ["false"])[0].lower() in {"1", "true", "yes"}
            results = self.core.query(
                query.get("query", [""])[0],
                type=query.get("type", [None])[0],
                region=query.get("region", [None])[0],
                language=query.get("language", [None])[0],
                limit=limit,
                include_overlay=include_overlay,
            )
            self._send_json(200, results)
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/mcp":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                message = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, json.JSONDecodeError):
                self._send_json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                return
            response = handle_mcp_message(self.core, message)
            if response is None:
                self.send_response(202)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                return
            self._send_mcp_json(response)
            return
        if parsed.path != "/api/v1/verify_claims":
            self._send_json(404, {"error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            claims = body.get("claims", [])
        except (ValueError, json.JSONDecodeError):
            self._send_json(400, {"error": "Invalid JSON body"})
            return
        self._send_json(200, {"results": self.core.verify_claims(claims)})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def handle_mcp_message(core: SekaiSyncCore, message: dict) -> dict | None:
    return McpServer(core).handle(message)


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
    print(f"SekaiSync HTTP server listening on http://{host}:{port}")
    print(f"OpenAPI: http://{host}:{port}/openapi.json")
    print(f"MCP Streamable HTTP: http://{host}:{port}/mcp")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


