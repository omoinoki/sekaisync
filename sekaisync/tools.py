"""Unified tool registry driving the MCP and HTTP surfaces.

One ``ToolSpec`` per capability describes its parameters, which core method
backs it, and how each endpoint wraps the result.  ``mcp_server`` builds
``tools/list`` and dispatches ``tools/call`` from this registry; the HTTP
``do_GET``/``do_POST`` routes dispatch through the same specs.  Adding a
capability here makes it visible on both ends — the previous
"three hand-written dispatch lists" drift (MCP missing status/sites/
web_browse) is structurally impossible now.

Argument coercion is endpoint-aware: HTTP query strings coerce loosely
(bad int -> default, matching the historical handler), MCP arguments
coerce strictly (bad int raises -> JSON-RPC error), mirroring the old
per-endpoint behaviour exactly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

CSV_LANG_DEFAULT = "ja,zh_hans,en,zh_tw,ko"


@dataclass(frozen=True)
class Param:
    name: str
    kind: str  # "str" | "int" | "bool" | "csv" | "csv_or_list" | "json"
    default: Any = None
    required: bool = False
    mcp_schema: Optional[dict] = None  # override the generated inputSchema entry
    mcp_default: Any = None  # endpoint-specific default (falls back to default)
    http_default: Any = None
    empty_to_none: bool = False  # "" coerces to None (historical `or None` handlers)
    core_name: Optional[str] = None  # kwarg name on the core method (defaults to name)
    description: str = ""


@dataclass(frozen=True)
class ToolSpec:
    name: str  # canonical name (used for HTTP operation ids / logs)
    mcp_name: str  # MCP tool name, e.g. "sekaisync_lookup"
    description: str  # MCP tool description
    core_method: Optional[str]  # SekaiSyncCore method; None = endpoint-specific
    args: tuple[Param, ...] = ()
    # HTTP
    http_path: Optional[str] = None
    http_method: str = "GET"
    http_not_found: Optional[str] = None  # 404 body when core returns None
    # result wrappers; None = pass core result through
    wrap: str = "raw"  # raw | query_results | browse_results | gaps
    mcp_input_schema: Optional[dict] = None  # full override of inputSchema


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def coerce_value(param: Param, raw: Any, endpoint: str, strict: bool) -> Any:
    if raw is None:
        if endpoint == "mcp":
            return param.mcp_default if param.mcp_default is not None else param.default
        return param.http_default if param.http_default is not None else param.default
    try:
        if param.kind == "int":
            return int(raw)
        if param.kind == "bool":
            if isinstance(raw, bool):
                return raw
            return str(raw).lower() in {"1", "true", "yes"}
        if param.kind == "csv":
            return _csv(str(raw))
        if param.kind == "csv_or_list":
            if isinstance(raw, str):
                return _csv(raw) or None
            if isinstance(raw, list):
                return raw
            return None
        return raw
    except (TypeError, ValueError):
        if strict:
            raise
        return param.default


def coerce_args(
    spec: ToolSpec,
    raw_args: dict,
    endpoint: str,
    get_raw: Callable[[str], Any],
) -> dict:
    """Map raw endpoint arguments (MCP arguments dict / HTTP query) to core kwargs."""
    strict = endpoint == "mcp"
    kwargs: dict[str, Any] = {}
    for param in spec.args:
        raw = get_raw(param.name)
        if raw is None and not param.required:
            value = coerce_value(param, None, endpoint, strict)
        else:
            if raw is None:
                if endpoint == "http":
                    raw = ""  # historical handlers read missing query as ""
                value = coerce_value(param, raw, endpoint, strict)
            else:
                value = coerce_value(param, raw, endpoint, strict)
        # int(0) or None -> None, matching both historical handlers for event_archive
        if param.kind == "int" and value == 0 and param.name == "limit" and spec.name == "event_archive":
            value = None
        if param.kind == "str" and param.empty_to_none and value == "":
            value = None
        kwargs[param.core_name or param.name] = value
    return kwargs


def _schema(params: tuple[Param, ...]) -> dict:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in params:
        if param.mcp_schema is not None:
            properties[param.name] = dict(param.mcp_schema)
        else:
            schema: dict[str, Any] = {
                "str": {"type": "string"},
                "int": {"type": "integer"},
                "bool": {"type": "boolean", "default": False},
                "csv": {"type": "string"},
                "csv_or_list": {"type": "array", "items": {"type": "string"}},
                "json": {"type": "object"},
            }[param.kind]
            if param.kind == "csv":
                schema = {"type": "string"}
            if param.default is not None and param.kind not in {"csv", "csv_or_list"}:
                schema = {**schema, "default": param.default}
            properties[param.name] = schema
        if param.required:
            required.append(param.name)
    return {"type": "object", "properties": properties, "required": required}


def mcp_tools_list(specs: tuple[ToolSpec, ...]) -> list[dict]:
    tools = []
    for spec in specs:
        if not spec.mcp_name:
            continue
        tools.append(
            {
                "name": spec.mcp_name,
                "description": spec.description,
                "inputSchema": spec.mcp_input_schema or _schema(spec.args),
            }
        )
    return tools


REGION_PARAM = Param(
    "regions",
    "csv_or_list",
    description="Region codes; comma-separated for HTTP",
    # progress declared regions as a plain string historically; event tools
    # declared arrays. Preserve each tool's advertised schema.
    mcp_schema={"type": "string"},
)

REGION_ARRAY_PARAM = Param(
    "regions",
    "csv_or_list",
    mcp_schema={"type": "array", "items": {"type": "string"}},
)

LANGUAGES_MCP = Param(
    "languages",
    "csv_or_list",
    mcp_schema={"type": "array", "items": {"type": "string"}},
    mcp_default=[],
    http_default=_csv(CSV_LANG_DEFAULT),
)


def build_tools_registry() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            "lookup",
            "sekaisync_lookup",
            "Look up Project Sekai entities in the local registry.",
            "lookup",
            (
                Param("query", "str", required=True),
                Param("type", "str"),
                Param("region", "str"),
                Param("language", "str"),
                Param("limit", "int", 8),
            ),
            http_path="/api/v1/lookup",
            wrap="query_results",
        ),
        ToolSpec(
            "resolve_name",
            "sekaisync_resolve_name",
            "Resolve a proper noun to an official localized name.",
            "resolve_name",
            (
                Param("query", "str", required=True),
                Param("target_language", "str", "zh_tw"),
                Param("source_language", "str"),
                Param("kind", "str"),
            ),
            http_path="/api/v1/resolve",
            wrap="query_results",
        ),
        ToolSpec(
            "term_lookup",
            "sekaisync_term_lookup",
            "Look up an extracted Project Sekai term and its cross-language names. "
            "Tags: person/location/organization/event(product fictional)/product/other. "
            "Sort by score or weight.",
            "term_lookup",
            (
                Param("query", "str", required=True),
                Param("language", "str", core_name="source_language"),
                LANGUAGES_MCP,
                Param("limit", "int", 8),
                Param("tag", "str"),
                Param("sort", "str", "score"),
            ),
            http_path="/api/v1/term_lookup",
            wrap="query_results",
        ),
        ToolSpec(
            "term_penetrate",
            "sekaisync_term_penetrate",
            "Cross-language per-line penetration for a term at a story position "
            "(event:174:1 etc.). Returns per-language term/sentence/trust for the "
            "same narrative line.",
            "term_penetrate",
            (
                Param("query", "str", required=True),
                Param("story_key", "str", empty_to_none=True),
                Param(
                    "languages",
                    "csv_or_list",
                    mcp_schema={"type": "array", "items": {"type": "string"}},
                    http_default=_csv(CSV_LANG_DEFAULT),
                ),
            ),
            http_path="/api/v1/term_penetrate",
            http_not_found="No matching term: {query}",
        ),
        ToolSpec(
            "fact_pack",
            "sekaisync_fact_pack",
            "Return a compact fact pack for one entity ID.",
            "fact_pack",
            (
                Param("entity_id", "str", required=True),
                Param("language", "str", "en"),
            ),
            http_path="/api/v1/fact_pack",
            http_not_found="Entity not found: {entity_id}",
        ),
        ToolSpec(
            "freshness", "sekaisync_freshness",
            "Return local data freshness and region coverage.",
            "freshness",
            http_path="/api/v1/freshness",
        ),
        ToolSpec(
            "refresh", "sekaisync_refresh",
            "Reload cached registry/glossary/factpacks/terms from disk after an "
            "external sync or index rebuild.",
            "refresh",
            http_path="/api/v1/refresh",
            http_method="POST",
            wrap="refresh",
        ),
        ToolSpec(
            "tag_clouds", "sekaisync_tag_clouds",
            "Return tag clouds split by released(multi-lang, supports penetrate) "
            "vs unreleased(ja-only, pending).",
            "tag_clouds",
            http_path="/api/v1/tag_clouds",
        ),
        ToolSpec(
            "data_gaps", "sekaisync_data_gaps",
            "Return known data source limitations (home_line gaps, overseas "
            "MySekai missing, etc.) so agents can honestly say 'not covered'.",
            "data_gaps",
            http_path="/api/v1/data_gaps",
            wrap="gaps",
        ),
        ToolSpec(
            "progress", "sekaisync_progress",
            "Return per-region fact/text completeness as integer percentages.",
            "progress",
            (
                REGION_PARAM,
                Param("live", "bool", False),
            ),
            http_path="/api/v1/progress",
        ),
        ToolSpec(
            "trust_summary", "sekaisync_trust",
            "Return trust-level (A/B/C/D) distribution across registry, glossary, "
            "terms and web pages.",
            "trust_summary",
            http_path="/api/v1/trust",
        ),
        ToolSpec(
            "integrity", "sekaisync_integrity",
            "Return knowledge base integrity issues: duplicates, canonical "
            "conflicts, hash mismatches and source fidelity metadata.",
            "integrity",
            (Param("limit", "int", 20),),
            http_path="/api/v1/integrity",
        ),
        ToolSpec(
            "news", "sekaisync_news",
            "Return locally synced official news and announcements, filterable by "
            "language (ja/en/tc/kr/cn), category tag (event/gacha/music/campaign/"
            "update/information/bug) and body availability.",
            "news",
            (
                Param("limit", "int", 50),
                Param("language", "str"),
                Param("tag", "str"),
                Param("body", "bool"),
            ),
            http_path="/api/v1/news",
        ),
        ToolSpec(
            "verify_claims", "sekaisync_verify_claims",
            "Verify claims against the local registry.",
            "verify_claims",
            (
                Param(
                    "claims",
                    "json",
                    required=True,
                    mcp_schema={
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim": {"type": "string"},
                                "expected": {"type": "string"},
                            },
                            "required": ["claim"],
                        },
                    },
                ),
            ),
            http_path="/api/v1/verify_claims",
            http_method="POST",
            wrap="results",
        ),
        ToolSpec(
            "web_lookup", "sekaisync_web_lookup",
            "Search the locally crawled text index from Sekai Viewer / altsource.",
            "web_lookup",
            (
                Param("query", "str", required=True),
                Param("source", "str"),
                Param("language", "str"),
                Param("kind", "str"),
                Param("limit", "int", 8),
                Param("include_text", "bool", False),
                Param("include_overlay", "bool", False),
                Param("max_text_chars", "int", 0),
            ),
            http_path="/api/v1/web_lookup",
            wrap="query_results",
        ),
        ToolSpec(
            "event_check", "sekaisync_event_check",
            "Detect new Project Sekai events, fetch their base master data, "
            "classify as box / WL / other, archive, and grow the crawl "
            "denominator. Never starts the web crawler.",
            "event_check",
            (
                REGION_ARRAY_PARAM,
                Param("timeout", "int", 30),
            ),
            http_path="/api/v1/events/check",
        ),
        ToolSpec(
            "event_archive", "sekaisync_event_archive",
            "List archived event classifications (box / WL / other) by region.",
            "event_archive",
            (
                REGION_ARRAY_PARAM,
                Param("limit", "int", None),
            ),
            http_path="/api/v1/events/archive",
        ),
        ToolSpec(
            "event_alias", "sekaisync_event_alias",
            "Resolve community event shorthand such as khn3 or 豆三箱 to a "
            "character box event with cross-region official names.",
            "event_alias",
            (
                Param("query", "str", required=True),
                REGION_ARRAY_PARAM,
            ),
            http_path="/api/v1/event_alias",
            http_not_found="No matching event alias or ordinal",
        ),
        ToolSpec(
            "worldlink", "sekaisync_worldlink",
            "Resolve World Link shorthand such as vbs wl2, vs wl, finale, round2, "
            "wl3第2组 or wl2g7 to the corresponding world_bloom event with "
            "cross-region official names.",
            "worldlink",
            (
                Param("query", "str", required=True),
                REGION_ARRAY_PARAM,
            ),
            http_path="/api/v1/worldlink",
            http_not_found="No matching World Link alias or ordinal",
        ),
        ToolSpec(
            "activity", "sekaisync_activity",
            "Unified activity resolution: World Link shorthand first (wl2g7, vbs "
            "wl2, finale, wl3第2组), then character box shorthand (khn3, 豆三箱). "
            "Returns kind=wl / kind=box / kind=unresolved (mixed or unnumbered "
            "events).",
            "activity",
            (
                Param("query", "str", required=True),
                REGION_ARRAY_PARAM,
            ),
            http_path="/api/v1/activity",
        ),
        ToolSpec(
            "query", "sekaisync_query",
            "Unified local query across master metadata and crawled story text.",
            "query",
            (
                Param("query", "str", required=True),
                Param("type", "str"),
                Param("region", "str"),
                Param("language", "str"),
                Param("limit", "int", 8),
                Param("include_overlay", "bool", False),
            ),
            http_path="/api/v1/query",
        ),
        ToolSpec(
            "web_browse", "sekaisync_web_browse",
            "Browse the locally crawled text index filtered by source, language "
            "and category, newest first.",
            "web_browse",
            (
                Param("source", "str"),
                Param("language", "str"),
                Param("kind", "str"),
                Param("limit", "int", 50),
                Param("include_text", "bool", False),
            ),
            http_path="/api/v1/web_browse",
            wrap="browse_results",
        ),
        ToolSpec(
            "status", "sekaisync_status",
            "Return full store status: master/web/terms/trust/progress/news/freshness aggregates.",
            "status",
            http_path="/api/v1/status",
        ),
        ToolSpec(
            "sites", "sekaisync_sites",
            "Return the configured multi-instance site profile from settings.json.",
            None,  # endpoint-provided: HTTP handler holds sites, MCP gets a provider
            http_path="/api/v1/sites",
        ),
    )


TOOLS = build_tools_registry()

MCP_NAME_TO_SPEC: dict[str, ToolSpec] = {s.mcp_name: s for s in TOOLS if s.mcp_name}
HTTP_GET_ROUTES: dict[str, ToolSpec] = {
    s.http_path: s for s in TOOLS if s.http_path and s.http_method == "GET"
}
HTTP_POST_ROUTES: dict[str, ToolSpec] = {
    s.http_path: s for s in TOOLS if s.http_path and s.http_method == "POST"
}
