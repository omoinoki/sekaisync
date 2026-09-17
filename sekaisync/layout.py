from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# The only store layout.  The historical v1 layout (data directly under the
# store root) was removed before the first public release.
LAYOUT = "v2"

CACHE = "cache"
KB = "kb"
RAW = "raw"


def manifest_path(store_root: Path) -> Path:
    return store_root / "manifest.json"


def write_manifest(
    store_root: Path,
    *,
    counts: Optional[dict[str, Any]] = None,
    notes: Optional[str] = None,
) -> Path:
    path = manifest_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "layout": LAYOUT,
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "counts": counts or {},
        "notes": notes,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_manifest(store_root: Path) -> dict[str, Any]:
    path = manifest_path(store_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def kb_dir(store_root: Path) -> Path:
    return store_root / KB


def cache_dir(store_root: Path) -> Path:
    return store_root / CACHE


LEGACY_RAW_DIRNAME = "source"
#: Sentinel generation for the pre-generation in-place layout.  A store that
#: has never run a generation publish keeps reading ``raw/<region>/source``.
LEGACY_GENERATION = "legacy"

#: Meta key holding the per-region active generation pointer.
ACTIVE_GENERATION_KEY = "active_raw_generation"


def raw_dir(store_root: Path) -> Path:
    return store_root / RAW


def generations_root(store_root: Path) -> Path:
    """Root of the immutable raw generations.

    Each published generation is a complete, self-contained copy of every
    region's master tables under ``raw/generations/<id>/<region>/source``.
    Generations are never mutated after they are published; a new publish
    writes a new directory and moves the pointer (Astra P13).
    """
    return raw_dir(store_root) / "generations"


def generation_dir(store_root: Path, generation: str) -> Path:
    """Directory of one immutable generation."""
    return generations_root(store_root) / generation


def generation_region_dir(store_root: Path, generation: str, region: str) -> Path:
    return generation_dir(store_root, generation) / region


def generation_master_dir(
    store_root: Path,
    region: str,
    generation: str,
) -> Path:
    """Master-table directory for one region inside one generation.

    Unlike :func:`region_master_dir` this never resolves "current" — callers
    must have already fixed the generation for the request, so a response
    cannot mix two generations (Astra P13: "一次请求固定同一指针集合").
    """
    return generation_region_dir(store_root, generation, region) / LEGACY_RAW_DIRNAME


def registry_path(store_root: Path) -> Path:
    return kb_dir(store_root) / "registry.json"


def glossary_path(store_root: Path) -> Path:
    return kb_dir(store_root) / "glossary.json"


def terms_path(store_root: Path) -> Path:
    return kb_dir(store_root) / "terms.json"


def seed_glossary_path(store_root: Path) -> Path:
    return kb_dir(store_root) / "seed_glossary.json"


def factpack_path(store_root: Path, language: str) -> Path:
    return cache_dir(store_root) / "factpacks" / f"{language}.json"


def events_archive_path(store_root: Path) -> Path:
    return kb_dir(store_root) / "events" / "archive.json"


def news_dir(store_root: Path) -> Path:
    return kb_dir(store_root) / "news"


def news_file_path(store_root: Path, language: Optional[str] = None) -> Path:
    lang = language or "all"
    return news_dir(store_root) / f"{lang}.json"


def web_consent_path(store_root: Path) -> Path:
    return kb_dir(store_root) / "web" / "consent.json"


def web_root(store_root: Path) -> Path:
    return kb_dir(store_root) / "web"


def web_source_dir(store_root: Path, source: str) -> Path:
    return web_root(store_root) / source


def web_pages_path(store_root: Path, source: str) -> Path:
    return web_source_dir(store_root, source) / "pages.json"


def db_path(store_root: Path) -> Path:
    """SQLite knowledge store (kb/sekaisync.db) — unique data layer."""
    return kb_dir(store_root) / "sekaisync.db"


def web_index_path(store_root: Path) -> Path:
    return cache_dir(store_root) / "web" / "index.json"


def web_category_dir(store_root: Path, source: str) -> Path:
    return cache_dir(store_root) / "web" / source


def web_category_file(store_root: Path, source: str, filename: str) -> Path:
    return web_category_dir(store_root, source) / filename


def freshness_path(store_root: Path) -> Path:
    return cache_dir(store_root) / "freshness.json"


def progress_path(store_root: Path) -> Path:
    return cache_dir(store_root) / "progress.json"


def region_master_dir(store_root: Path, region: str) -> Path:
    """Directory holding the region master JSON tables.

    This is the **legacy in-place** path and is still the correct place to
    *write* bootstrapped data (see ``cli.create_demo_store``). Readers should
    prefer :func:`master_dir_for`, which resolves the active published
    generation when one exists (Astra P13).
    """
    return raw_dir(store_root) / region / "source"


def region_source_dir(store_root: Path, region: str) -> Path:
    """See :func:`region_master_dir` — the legacy in-place path."""
    return raw_dir(store_root) / region / "source"


def master_source_dir(store_root: Path, region: str) -> Path:
    """Read-side directory for a region's source tree.

    Delegates to :func:`region_master_dir` semantics but goes through the
    generation-aware resolver, so readers automatically follow the active
    generation once one has been published.
    """
    from sekaisync.registry import master_dir_for  # lazy: registry imports layout

    return master_dir_for(store_root, region)


#: Statuses for :class:`TableRead`.  ``valid`` means a non-empty list of
#: records, ``valid_empty`` a well-formed empty table, ``invalid`` a file that
#: exists but is malformed/unreadable (bad JSON, unsupported wrapper,
#: non-dict rows...), and ``missing`` means no matching file was found.
#: This validates containers, not table-specific schemas or references.
TABLE_STATUSES = ("missing", "invalid", "valid_empty", "valid")

#: ``{"records"|"items"|"data": [...]}`` API wrappers are unwrapped once.
_TABLE_WRAPPER_KEYS = ("records", "items", "data")


def _master_table_candidates(directory: Path, table: str) -> list[Path]:
    """Candidate paths for one table, mirroring the historical layouts."""
    return sorted(directory.rglob(f"{table}.json"))


def _records_from_master_payload(data: Any) -> Optional[list[dict]]:
    """Return records for a trusted master payload, or ``None`` if invalid."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        wrapper = next(
            (data.get(key) for key in _TABLE_WRAPPER_KEYS
             if isinstance(data.get(key), list)),
            None,
        )
        if wrapper is None:
            return None
        rows = wrapper
    else:
        return None
    if not all(isinstance(item, dict) for item in rows):
        return None
    return rows


@dataclass(frozen=True)
class TableRead:
    """One master table read with an explicit completeness state.

    ``missing``/``invalid``/``valid_empty``/``valid`` must never be folded into
    a bare ``[]`` (Astra P16/D16): an absent or corrupt table is not evidence
    of an empty dataset.  ``source`` is the concrete file the records came from
    (empty when missing) and ``version`` the raw generation the snapshot
    pinned, so callers can report provenance honestly.
    """

    status: str
    records: list[dict[str, Any]] = field(default_factory=list)
    source: str = ""
    version: str = ""
    error: str = ""

    def __post_init__(self):
        if self.status not in TABLE_STATUSES:
            raise ValueError(f"unknown TableRead status: {self.status!r}")
        if self.status == "invalid" and not self.error:
            raise ValueError("an invalid TableRead must describe why")

    @property
    def ok(self) -> bool:
        return self.status in ("valid", "valid_empty")


def read_master_table(
    snapshot: Any,
    region: str,
    table: str,
) -> TableRead:
    """Read one master table through a fixed :class:`RawSnapshot`.

    The snapshot's directories are used verbatim: no active-pointer lookup, no
    legacy fallback, no directory creation (Astra P16: "alias/WL 统一用快照
    路径解析器").  A region the snapshot does not carry is simply ``missing``.
    """
    from sekaisync.registry import RawSnapshot  # lazy: registry imports layout

    if not isinstance(snapshot, RawSnapshot):
        raise TypeError("read_master_table requires an explicit RawSnapshot")
    generation = str(snapshot.generations.get(region) or "")
    directory = snapshot.master_dirs.get(region)
    if directory is None:
        return TableRead("missing", version=generation)
    candidates = _master_table_candidates(Path(directory), table)
    present = [path for path in candidates if path.is_file()]
    if len(present) > 1:
        names = ", ".join(str(path) for path in present)
        return TableRead("invalid", source=str(present[0]), version=generation,
                         error=f"multiple candidate files for {table}: {names}")
    if not present:
        return TableRead("missing", version=generation)
    path = present[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return TableRead("invalid", source=str(path), version=generation,
                         error=f"{type(exc).__name__}: {exc}")
    records = _records_from_master_payload(payload)
    if records is None:
        return TableRead("invalid", source=str(path), version=generation,
                         error="payload is not a list of objects")
    return TableRead(
        "valid" if records else "valid_empty",
        records,
        str(path),
        generation,
        "",
    )
