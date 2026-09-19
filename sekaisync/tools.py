"""Unified tool registry driving the MCP and HTTP surfaces.

One ``ToolSpec`` per capability describes its parameters, which core method
backs it, and how each endpoint wraps the result.  ``mcp_server`` builds
``tools/list`` and dispatches ``tools/call`` from this registry; the HTTP
``do_GET``/``do_POST`` routes dispatch through the same specs.  Adding a
capability here makes it visible on both ends — the previous
"three hand-written dispatch lists" drift (MCP missing status/sites/
web_browse) is structurally impossible now.

The OpenAPI document is generated from this same registry
(:func:`build_openapi`), so a method/path/parameter cannot be advertised on
one surface and be missing on the other.

Argument validation is endpoint-aware but strict on both ends (P15/D15):
HTTP query strings are parsed per the declared type first and a value that
cannot be parsed is a 400, MCP arguments must already have the declared
JSON type or the call is a -32602.  Both endpoints share one
``validate_args`` implementation, so a rule cannot exist on one surface
and be missing on the other.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

from sekaisync import __version__

CSV_LANG_DEFAULT = "ja,zh_hans,en,zh_tw,ko"

# ---------------------------------------------------------------------------
# P15 provisional budgets (Astra B1).  These numbers are deliberately small
# and centralised so they can be calibrated against real fixtures later; they
# are not a final contract.
# ---------------------------------------------------------------------------
MAX_LIMIT = 100  # per-tool `limit` ceiling (event_archive keeps 0 == all)
MAX_CLAIMS = 100  # verify_claims batch size
MAX_QUERY_LENGTH = 2048  # characters for any `query` argument
MAX_MAX_TEXT_CHARS = 200_000  # web_lookup text budget
MAX_TIMEOUT_SECONDS = 600  # event_check upstream timeout

_TRUE_TEXT = frozenset({"1", "true", "yes", "on"})
_FALSE_TEXT = frozenset({"0", "false", "no", "off"})
_INT_RE = re.compile(r"^[+-]?\d+$")


class ParamError(ValueError):
    """A request argument is missing, malformed or out of range.

    HTTP maps this to 400; MCP maps it to JSON-RPC -32602.  It is raised
    before any Core method is reached, so a rejected argument can never
    perform a write.
    """


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
    # P15 constraints.  `minimum`/`maximum` are inclusive; `max_length`
    # counts characters for text kinds, entries for list kinds and batch
    # size for JSON kinds.
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    max_length: Optional[int] = None
    enum: Optional[tuple[str, ...]] = None
    # event_archive's historical contract: limit=0 means "no limit", not
    # "zero rows".  Keep it explicit instead of a tool-name special case.
    zero_means_none: bool = False


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


def _declared_mcp_type(param: Param) -> Optional[str]:
    """The JSON type this parameter advertises over MCP, if any.

    ``regions`` is a string on ``progress`` and an array on the event tools;
    the advertised schema is the binding contract, so validation reads it
    from the same place the client does.
    """
    if param.mcp_schema is None:
        return None
    declared = param.mcp_schema.get("type")
    return declared if isinstance(declared, str) else None


def _coerce_bool_text(raw: str, param: Param) -> bool:
    """Only an explicit true/false token is a boolean (P15).

    Anything else (``"maybe"``, ``"2"``, ``""``) raises instead of silently
    becoming False, so a typo cannot masquerade as a deliberate filter.
    """
    token = raw.strip().lower()
    if token in _TRUE_TEXT:
        return True
    if token in _FALSE_TEXT:
        return False
    raise ParamError(
        f"'{param.name}' expects one of true/false/1/0/yes/no/on/off, got {raw!r}"
    )


def parse_value(param: Param, raw: Any, endpoint: str) -> Any:
    """Parse one raw endpoint value into the declared Python type.

    Raises :class:`ParamError` when the value does not match the declared
    type.  HTTP passes query text (always ``str``); MCP passes decoded JSON
    values.  Type confusion — a JSON ``bool`` where an ``int`` is declared,
    an ``array`` where a ``string`` is declared — is rejected, never
    coerced.
    """
    kind = param.kind
    if kind == "int":
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            raise ParamError(f"'{param.name}' expects an integer, got {type(raw).__name__}")
        if isinstance(raw, str):
            token = raw.strip()
            if not _INT_RE.match(token):
                raise ParamError(f"'{param.name}' expects an integer, got {raw!r}")
            return int(token)
        return int(raw)
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return _coerce_bool_text(raw, param)
        raise ParamError(f"'{param.name}' expects a boolean, got {type(raw).__name__}")
    if kind == "str":
        if isinstance(raw, str):
            return raw
        # HTTP query values are always text; a non-string only reaches here
        # from a JSON body, where it is a type error, not something to
        # stringify into existence.
        raise ParamError(f"'{param.name}' expects a string, got {type(raw).__name__}")
    if kind == "csv":
        if isinstance(raw, list):
            return _csv(",".join(str(item) for item in raw))
        if not isinstance(raw, str):
            raise ParamError(f"'{param.name}' expects a string, got {type(raw).__name__}")
        return _csv(raw)
    if kind == "csv_or_list":
        declared = _declared_mcp_type(param)
        if endpoint == "mcp" and declared is not None:
            # Over MCP the advertised JSON type is binding (P15): a tool that
            # declares an array must not silently accept a bare string, and
            # one that declares a string (historical progress `regions`)
            # keeps accepting it.
            if declared == "array" and not isinstance(raw, list):
                raise ParamError(
                    f"'{param.name}' expects an array of strings, got {type(raw).__name__}"
                )
            if declared == "string" and not isinstance(raw, str):
                raise ParamError(
                    f"'{param.name}' expects a string, got {type(raw).__name__}"
                )
        if isinstance(raw, str):
            return _csv(raw) or None
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, str):
                    raise ParamError(
                        f"'{param.name}' expects an array of strings, got {type(item).__name__}"
                    )
            return raw
        raise ParamError(
            f"'{param.name}' expects a string or an array of strings, got {type(raw).__name__}"
        )
    if kind == "json":
        if endpoint == "mcp":
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ParamError(f"'{param.name}' is not valid JSON: {exc.msg}") from exc
        return raw
    raise ParamError(f"'{param.name}' has an unsupported kind {kind!r}")


def validate_value(param: Param, value: Any) -> Any:
    """Apply the declared range/length/enum constraints to a parsed value."""
    if value is None:
        return value
    if param.kind == "int":
        if param.minimum is not None and value < param.minimum:
            raise ParamError(f"'{param.name}' must be >= {param.minimum}")
        if param.maximum is not None and value > param.maximum:
            raise ParamError(f"'{param.name}' must be <= {param.maximum}")
        return value
    if param.kind in {"str", "csv"}:
        if param.max_length is not None and len(value) > param.max_length:
            raise ParamError(
                f"'{param.name}' is longer than {param.max_length} characters"
            )
    if param.kind == "csv_or_list":
        if param.max_length is not None and len(value) > param.max_length:
            raise ParamError(f"'{param.name}' has more than {param.max_length} entries")
    if param.kind == "json":
        # `max_length` on a JSON parameter bounds the batch size (e.g. the
        # verify_claims array), not a character count.
        if param.max_length is not None:
            if not isinstance(value, list):
                raise ParamError(f"'{param.name}' must be an array")
            if len(value) > param.max_length:
                raise ParamError(
                    f"'{param.name}' has more than {param.max_length} entries"
                )
    if param.enum is not None:
        candidates = value if isinstance(value, list) else [value]
        for item in candidates:
            if item not in param.enum:
                raise ParamError(
                    f"'{param.name}' must be one of {', '.join(param.enum)}"
                )
    return value


def coerce_value(param: Param, raw: Any, endpoint: str, strict: bool) -> Any:
    """Backwards-compatible wrapper around :func:`parse_value`.

    Kept because it is part of the module's public surface; ``strict`` no
    longer relaxes types, it only chooses whether a failure raises
    (:class:`ParamError`) or falls back to the declared default.
    """
    if raw is None:
        if endpoint == "mcp":
            return param.mcp_default if param.mcp_default is not None else param.default
        return param.http_default if param.http_default is not None else param.default
    try:
        return parse_value(param, raw, endpoint)
    except ParamError:
        if strict:
            raise
        return param.default


def validate_param(param: Param, raw: Any, *, endpoint: str, spec: ToolSpec) -> Any:
    """Validate one parameter for one tool (:func:`coerce_args` helper)."""
    if raw is None:
        # Absent and required is rejected on both endpoints (P15: no silent
        # "" lookup that returns everything).
        if param.required:
            raise ParamError(f"missing required parameter '{param.name}'")
        if endpoint == "mcp":
            return param.mcp_default if param.mcp_default is not None else param.default
        return param.http_default if param.http_default is not None else param.default
    value = parse_value(param, raw, endpoint)
    if param.kind == "str" and param.required and isinstance(value, str):
        if not value.strip():
            raise ParamError(f"'{param.name}' must not be blank")
    if param.kind == "str" and param.empty_to_none and value == "":
        value = None
    value = validate_value(param, value)
    if param.kind == "int" and value == 0 and param.zero_means_none:
        value = None
    return value


def coerce_args(
    spec: ToolSpec,
    raw_args: dict,
    endpoint: str,
    get_raw: Callable[[str], Any],
) -> dict:
    """Validate raw endpoint arguments and map them to core kwargs.

    Raises :class:`ParamError` for anything the ToolSpec declares as
    invalid; the caller decides the transport-level error (HTTP 400 /
    JSON-RPC -32602).  No core method is called for a rejected request.
    """
    kwargs: dict[str, Any] = {}
    for param in spec.args:
        kwargs[param.core_name or param.name] = validate_param(
            param, get_raw(param.name), endpoint=endpoint, spec=spec
        )
    return kwargs


def validate_args(spec: ToolSpec, raw: dict, *, endpoint: str) -> dict:
    """Public entry point (P15): validate ``raw`` into core kwargs.

    ``raw`` is an MCP ``arguments`` object or an HTTP body/query mapping.
    Unknown keys are ignored (the MCP schema is advisory and clients may
    add provenance fields); declared parameters are type-checked, range
    checked and requiredness checked.
    """
    return coerce_args(spec, raw, endpoint=endpoint, get_raw=raw.get)


def _schema(params: tuple[Param, ...]) -> dict:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in params:
        if param.mcp_schema is not None:
            schema = dict(param.mcp_schema)
            for key, value in _constraint_keys(param).items():
                schema.setdefault(key, value)
        else:
            schema = {
                "str": {"type": "string"},
                "int": {"type": "integer"},
                "bool": {"type": "boolean", "default": False},
                "csv": {"type": "string"},
                "csv_or_list": {"type": "array", "items": {"type": "string"}},
                "json": {"type": "object"},
            }[param.kind]
            if param.default is not None and param.kind not in {"csv", "csv_or_list"}:
                schema = {**schema, "default": param.default}
            # A nullable bool with no default is three-state (news.body):
            # advertising `default: false` would tell clients that omitting
            # the field means "body absent only", which is wrong.
            if param.kind == "bool" and param.default is None:
                schema.pop("default", None)
            schema.update(_constraint_keys(param))
        properties[param.name] = schema
        if param.required:
            required.append(param.name)
    return {"type": "object", "properties": properties, "required": required}


def _constraint_keys(param: Param) -> dict[str, Any]:
    """JSON-Schema keywords derived from the Param's declared constraints."""
    out: dict[str, Any] = {}
    if param.kind == "int":
        if param.minimum is not None:
            out["minimum"] = param.minimum
        if param.maximum is not None:
            out["maximum"] = param.maximum
    elif param.kind in {"str", "csv"}:
        if param.max_length is not None:
            out["maxLength"] = param.max_length
    elif param.kind in {"csv_or_list", "json"}:
        if param.max_length is not None:
            out["maxItems"] = param.max_length
    if param.enum is not None:
        out["enum"] = list(param.enum)
    return out


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
    max_length=32,
)

LANGUAGES_MCP = Param(
    "languages",
    "csv_or_list",
    mcp_schema={"type": "array", "items": {"type": "string"}},
    mcp_default=[],
    http_default=_csv(CSV_LANG_DEFAULT),
    max_length=32,
)

# P15: the same constraints are declared here for every tool; the MCP
# inputSchema and the /openapi.json document are both generated from them.
QUERY_PARAM = Param("query", "str", required=True, max_length=MAX_QUERY_LENGTH)


def _limit(default: int, minimum: Optional[int] = 1) -> Param:
    """A provisional ``limit`` parameter (Astra B1: max 100, calibrate later)."""
    return Param("limit", "int", default, minimum=minimum, maximum=MAX_LIMIT)


def build_tools_registry() -> tuple[ToolSpec, ...]:
    return (
        ToolSpec(
            "lookup",
            "sekaisync_lookup",
            "Look up Project Sekai entities in the local registry.",
            "lookup",
            (
                QUERY_PARAM,
                Param("type", "str", max_length=64),
                Param("region", "str", max_length=32),
                Param("language", "str", max_length=32),
                _limit(8),
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
                QUERY_PARAM,
                Param("target_language", "str", "zh_tw", max_length=32),
                Param("source_language", "str", max_length=32),
                Param("kind", "str", max_length=64),
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
                QUERY_PARAM,
                Param("language", "str", core_name="source_language", max_length=32),
                LANGUAGES_MCP,
                _limit(8),
                Param("tag", "str", max_length=64),
                Param("sort", "str", "score", enum=("score", "weight")),
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
                QUERY_PARAM,
                Param("story_key", "str", empty_to_none=True, max_length=128),
                Param(
                    "languages",
                    "csv_or_list",
                    mcp_schema={"type": "array", "items": {"type": "string"}},
                    http_default=_csv(CSV_LANG_DEFAULT),
                    max_length=32,
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
                Param("entity_id", "str", required=True, max_length=256),
                Param("language", "str", "en", max_length=32),
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
            (_limit(20),),
            http_path="/api/v1/integrity",
        ),
        ToolSpec(
            "news", "sekaisync_news",
            "Return locally synced official news and announcements, filterable by "
            "language (ja/en/tc/kr/cn), category tag (event/gacha/music/campaign/"
            "update/information/bug) and body availability.",
            "news",
            (
                _limit(50),
                Param("language", "str", max_length=32),
                Param("tag", "str", max_length=64),
                # Three-state on purpose (P15): absent must stay None (no body
                # filter), not collapse to False ("body absent only").
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
                    max_length=MAX_CLAIMS,
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
                        "maxItems": MAX_CLAIMS,
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
                QUERY_PARAM,
                Param("source", "str", max_length=64),
                Param("language", "str", max_length=32),
                Param("kind", "str", max_length=64),
                _limit(8),
                Param("include_text", "bool", False),
                Param("include_overlay", "bool", False),
                Param(
                    "max_text_chars",
                    "int",
                    0,
                    minimum=0,
                    maximum=MAX_MAX_TEXT_CHARS,
                ),
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
                Param(
                    "timeout",
                    "int",
                    30,
                    minimum=1,
                    maximum=MAX_TIMEOUT_SECONDS,
                ),
            ),
            http_path="/api/v1/events/check",
            # P15: this endpoint writes (archives events, grows the crawl
            # denominator), so it must not sit on GET.
            http_method="POST",
        ),
        ToolSpec(
            "event_archive", "sekaisync_event_archive",
            "List archived event classifications (box / WL / other) by region.",
            "event_archive",
            (
                REGION_ARRAY_PARAM,
                # Historical contract: limit=0 means "all", not "zero rows";
                # `zero_means_none` preserves it instead of a generic
                # `minimum=1` rejection breaking the endpoint.
                Param("limit", "int", None, minimum=0, maximum=MAX_LIMIT, zero_means_none=True),
            ),
            http_path="/api/v1/events/archive",
        ),
        ToolSpec(
            "event_alias", "sekaisync_event_alias",
            "Resolve community event shorthand such as khn3 or 豆三箱 to a "
            "character box event with cross-region official names.",
            "event_alias",
            (
                QUERY_PARAM,
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
                QUERY_PARAM,
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
                QUERY_PARAM,
                REGION_ARRAY_PARAM,
            ),
            http_path="/api/v1/activity",
        ),
        ToolSpec(
            "query", "sekaisync_query",
            "Unified local query across master metadata and crawled story text.",
            "query",
            (
                QUERY_PARAM,
                Param("type", "str", max_length=64),
                Param("region", "str", max_length=32),
                Param("language", "str", max_length=32),
                _limit(8),
                Param("include_overlay", "bool", False),
                # The core method defaults to include_web=True, and one web
                # sweep costs ~2 minutes on a real store.  Without a declared
                # param no HTTP/MCP caller could turn the web layer off — the
                # endpoint silently ran web_lookup on every call.  Declaring
                # it keeps True as the default while making include_web=false
                # actually reachable.
                Param("include_web", "bool", True),
            ),
            http_path="/api/v1/query",
        ),
        ToolSpec(
            "web_browse", "sekaisync_web_browse",
            "Browse the locally crawled text index filtered by source, language "
            "and category, newest first.",
            "web_browse",
            (
                Param("source", "str", max_length=64),
                Param("language", "str", max_length=32),
                Param("kind", "str", max_length=64),
                _limit(50),
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

# HTTP operation ids / summaries / 404 notes the OpenAPI document needs and
# the registry does not carry.  Methods, paths, parameters, defaults and
# ranges all come from the ToolSpec/Param registry above; this table only
# holds prose that has no home in a tool description.
HTTP_OPERATION_IDS: dict[str, str] = {
    "lookup": "lookup",
    "resolve_name": "resolve",
    "term_lookup": "termLookup",
    "term_penetrate": "termPenetrate",
    "fact_pack": "factPack",
    "freshness": "freshness",
    "refresh": "refresh",
    "tag_clouds": "tagClouds",
    "data_gaps": "dataGaps",
    "progress": "progress",
    "trust_summary": "trust",
    "integrity": "integrity",
    "news": "news",
    "verify_claims": "verifyClaims",
    "web_lookup": "webLookup",
    "event_check": "eventCheck",
    "event_archive": "eventArchive",
    "event_alias": "eventAlias",
    "worldlink": "worldlink",
    "activity": "activity",
    "query": "query",
    "web_browse": "webBrowse",
    "status": "status",
    "sites": "sites",
}

HTTP_SUMMARIES: dict[str, str] = {
    "lookup": "Entity matches",
    "resolve_name": "Official localized name matches",
    "term_lookup": "Extracted terms and their cross-language names",
    "term_penetrate": "Cross-language per-line penetration for a term at a story position",
    "fact_pack": "Compact fact pack",
    "freshness": "Data freshness",
    "refresh": "Reload cached indexes from disk",
    "tag_clouds": "Tag clouds split by released(multi-lang) vs unreleased(ja-only)",
    "data_gaps": "Known data source limitations visible to agents",
    "progress": "Per-region completeness percentages",
    "trust_summary": "A/B/C/D trust distribution",
    "integrity": "Dedup and fidelity integrity report",
    "news": "Synced official news and announcements",
    "verify_claims": "Verification results",
    "web_lookup": "Crawled web text matches",
    "event_check": "New-event detection, base-data sync and classification",
    "event_archive": "Archived event classifications by region",
    "event_alias": "Community event shorthand resolved to box event",
    "worldlink": "World Link shorthand resolved to world_bloom event",
    "activity": "Shorthand resolved to a numbered activity (wl or box) or kind=unresolved",
    "query": "Unified metadata and web text matches",
    "web_browse": "Crawled web text filtered by source/category",
    "status": "Store, master, web and freshness status",
    "sites": "Configured site profile from settings.json",
}

HTTP_404_DESCRIPTIONS: dict[str, str] = {
    "event_alias": "No matching alias or ordinal",
    "worldlink": "No matching World Link alias or ordinal",
}


def openapi_parameter(param: Param) -> dict:
    """One OpenAPI ``in: query`` parameter object derived from a Param."""
    schema: dict[str, Any] = {}
    if param.kind == "int":
        schema["type"] = "integer"
    elif param.kind == "bool":
        schema["type"] = "boolean"
    elif param.kind == "csv_or_list":
        schema["type"] = "array"
        schema["items"] = {"type": "string"}
    else:
        schema["type"] = "string"
    if param.default is not None:
        schema["default"] = param.default
    schema.update(_constraint_keys(param))
    entry: dict[str, Any] = {"name": param.name, "in": "query", "schema": schema}
    if param.required:
        entry["required"] = True
    if param.description:
        entry["description"] = param.description
    return entry


def _http_params(spec: ToolSpec) -> tuple[Param, ...]:
    if spec.http_method == "POST":
        # POST endpoints take their arguments from the JSON body, not the
        # query string (events/check and verify_claims are the only ones).
        return ()
    return spec.args


def _request_body_schema(spec: ToolSpec) -> Optional[dict]:
    if spec.http_method != "POST":
        return None
    properties: dict[str, Any] = {}
    required: list[str] = []
    for param in spec.args:
        if param.mcp_schema is not None:
            schema = dict(param.mcp_schema)
            schema.update(_constraint_keys(param))
        elif param.kind == "int":
            schema = {"type": "integer", **_constraint_keys(param)}
        elif param.kind == "bool":
            schema = {"type": "boolean"}
        elif param.kind == "csv_or_list":
            schema = {"type": "array", "items": {"type": "string"}, **_constraint_keys(param)}
        else:
            schema = {"type": "string", **_constraint_keys(param)}
        properties[param.name] = schema
        if param.required:
            required.append(param.name)
    return {"type": "object", "properties": properties, "required": required}


def build_openapi(specs: Iterable[ToolSpec] = TOOLS) -> dict:
    """Derive the OpenAPI document from the same registry that drives MCP.

    The previous hand-written constant drifted from ``tools.py`` (it still
    advertised ``GET /api/v1/events/check`` and dropped ``news``'s
    parameters); generating both from one source makes that class of drift
    impossible.
    """
    paths: dict[str, Any] = {}
    for spec in specs:
        if not spec.http_path:
            continue
        method = spec.http_method.lower()
        operation: dict[str, Any] = {
            "operationId": HTTP_OPERATION_IDS.get(spec.name, spec.name),
        }
        params = _http_params(spec)
        if params:
            operation["parameters"] = [openapi_parameter(p) for p in params]
        body = _request_body_schema(spec)
        if body is not None:
            operation["requestBody"] = {
                "required": bool(body["required"]),
                "content": {"application/json": {"schema": body}},
            }
        responses: dict[str, Any] = {
            "200": {"description": HTTP_SUMMARIES.get(spec.name, spec.name)}
        }
        if spec.http_not_found is not None or spec.name in HTTP_404_DESCRIPTIONS:
            responses["404"] = {
                "description": HTTP_404_DESCRIPTIONS.get(spec.name, "Not found")
            }
        operation["responses"] = responses
        paths.setdefault(spec.http_path, {})[method] = operation
    return {
        "openapi": "3.1.0",
        "info": {"title": "SekaiSync Local API", "version": __version__},
        "servers": [{"url": "http://127.0.0.1:8787"}],
        "paths": paths,
    }
