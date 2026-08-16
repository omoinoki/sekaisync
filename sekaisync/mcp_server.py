from __future__ import annotations

import json
import sys
from typing import Any

from sekaisync import __version__
from sekaisync.core import SekaiSyncCore


PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "sekaisync_lookup",
        "description": "Look up Project Sekai entities in the local registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {"type": "string"},
                "region": {"type": "string"},
                "language": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "sekaisync_resolve_name",
        "description": "Resolve a proper noun to an official localized name.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "target_language": {"type": "string", "default": "zh_tw"},
                "source_language": {"type": "string"},
                "kind": {"type": "string"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "sekaisync_term_lookup",
        "description": "Look up an extracted Project Sekai term and its cross-language names.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "language": {"type": "string"},
                "languages": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "sekaisync_fact_pack",
        "description": "Return a compact fact pack for one entity ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "language": {"type": "string", "default": "en"},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "sekaisync_freshness",
        "description": "Return local data freshness and region coverage.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sekaisync_progress",
        "description": "Return per-region fact/text completeness as integer percentages.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "regions": {"type": "string"},
                "live": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "sekaisync_trust",
        "description": "Return trust-level (A/B/C/D) distribution across registry, glossary, terms and web pages.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sekaisync_integrity",
        "description": "Return knowledge base integrity issues: duplicates, canonical conflicts, hash mismatches and source fidelity metadata.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 20}
            },
        },
    },
    {
        "name": "sekaisync_news",
        "description": "Return locally synced official news and announcements by language.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 100}
            },
        },
    },
    {
        "name": "sekaisync_verify_claims",
        "description": "Verify claims against the local registry.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "claims": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "claim": {"type": "string"},
                            "expected": {"type": "string"},
                        },
                        "required": ["claim"],
                    },
                }
            },
            "required": ["claims"],
        },
    },
    {
        "name": "sekaisync_web_lookup",
        "description": "Search the locally crawled text index from Sekai Viewer / altsource.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "source": {"type": "string"},
                "language": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
                "include_overlay": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    },
    {
        "name": "sekaisync_event_check",
        "description": "Detect new Project Sekai events, fetch their base master data, classify as box / WL / other, archive, and grow the crawl denominator. Never starts the web crawler.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "regions": {"type": "array", "items": {"type": "string"}},
                "timeout": {"type": "integer", "default": 30},
            },
        },
    },
    {
        "name": "sekaisync_event_archive",
        "description": "List archived event classifications (box / WL / other) by region.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "regions": {"type": "array", "items": {"type": "string"}},
                "limit": {"type": "integer"},
            },
        },
    },    {
        "name": "sekaisync_event_alias",
        "description": "Resolve community event shorthand such as khn3 or 豆三箱 to a character box event with cross-region official names.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "regions": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["query"],
        },
    },    {
        "name": "sekaisync_query",
        "description": "Unified local query across master metadata and crawled story text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "type": {"type": "string"},
                "region": {"type": "string"},
                "language": {"type": "string"},
                "limit": {"type": "integer", "default": 8},
                "include_overlay": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
    },
]


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
    def __init__(self, core: SekaiSyncCore):
        self.core = core

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
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
        if method == "tools/call":
            name = params.get("name", "")
            arguments = params.get("arguments") or {}
            try:
                if name == "sekaisync_lookup":
                    result = self.core.lookup(
                        arguments.get("query", ""),
                        type=arguments.get("type"),
                        region=arguments.get("region"),
                        language=arguments.get("language"),
                        limit=int(arguments.get("limit", 8)),
                    )
                elif name == "sekaisync_resolve_name":
                    result = self.core.resolve_name(
                        arguments.get("query", ""),
                        target_language=arguments.get("target_language", "zh_tw"),
                        source_language=arguments.get("source_language"),
                        kind=arguments.get("kind"),
                    )
                elif name == "sekaisync_term_lookup":
                    raw_languages = arguments.get("languages") or []
                    if isinstance(raw_languages, str):
                        raw_languages = [
                            item.strip()
                            for item in raw_languages.split(",")
                            if item.strip()
                        ]
                    result = self.core.term_lookup(
                        arguments.get("query", ""),
                        source_language=arguments.get("language"),
                        languages=raw_languages,
                        limit=int(arguments.get("limit", 8)),
                    )
                elif name == "sekaisync_fact_pack":
                    result = self.core.fact_pack(
                        arguments.get("entity_id", ""),
                        language=arguments.get("language", "en"),
                    )
                elif name == "sekaisync_freshness":
                    result = self.core.freshness()
                elif name == "sekaisync_progress":
                    raw_regions = arguments.get("regions")
                    if isinstance(raw_regions, str):
                        regions = [
                            item.strip()
                            for item in raw_regions.split(",")
                            if item.strip()
                        ]
                    else:
                        regions = None
                    result = self.core.progress(
                        regions=regions,
                        live=bool(arguments.get("live", False)),
                    )
                elif name == "sekaisync_trust":
                    result = self.core.trust_summary()
                elif name == "sekaisync_integrity":
                    result = self.core.integrity(limit=int(arguments.get("limit", 20)))
                elif name == "sekaisync_news":
                    result = self.core.news(limit=int(arguments.get("limit", 100)))
                elif name == "sekaisync_verify_claims":
                    result = self.core.verify_claims(arguments.get("claims", []))
                elif name == "sekaisync_web_lookup":
                    result = self.core.web_lookup(
                        arguments.get("query", ""),
                        source=arguments.get("source"),
                        language=arguments.get("language"),
                        limit=int(arguments.get("limit", 8)),
                        include_overlay=bool(arguments.get("include_overlay", False)),
                    )
                elif name == "sekaisync_event_check":
                    raw_regions = arguments.get("regions")
                    if isinstance(raw_regions, str):
                        raw_regions = [
                            item.strip()
                            for item in raw_regions.split(",")
                            if item.strip()
                        ]
                    result = self.core.event_check(
                        regions=raw_regions,
                        timeout=int(arguments.get("timeout", 30)),
                    )
                elif name == "sekaisync_event_archive":
                    raw_regions = arguments.get("regions")
                    if isinstance(raw_regions, str):
                        raw_regions = [
                            item.strip()
                            for item in raw_regions.split(",")
                            if item.strip()
                        ]
                    result = self.core.event_archive(
                        regions=raw_regions,
                        limit=int(arguments.get("limit", 0)) or None,
                    )
                elif name == "sekaisync_event_alias":
                    raw_regions = arguments.get("regions")
                    if isinstance(raw_regions, str):
                        raw_regions = [
                            item.strip()
                            for item in raw_regions.split(",")
                            if item.strip()
                        ]
                    result = self.core.event_alias(
                        arguments.get("query", ""),
                        regions=raw_regions,
                    )
                elif name == "sekaisync_query":
                    result = self.core.query(
                        arguments.get("query", ""),
                        type=arguments.get("type"),
                        region=arguments.get("region"),
                        language=arguments.get("language"),
                        limit=int(arguments.get("limit", 8)),
                        include_overlay=bool(arguments.get("include_overlay", False)),
                    )
                else:
                    return {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": f"Unknown tool: {name}"},
                    }
                return {"jsonrpc": "2.0", "id": request_id, "result": _text_result(result)}
            except Exception as exc:  # noqa: BLE001
                return {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(exc)},
                }
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


def run_mcp_server(core: SekaiSyncCore) -> int:
    return McpServer(core).run()



