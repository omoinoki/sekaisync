from __future__ import annotations

import json
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
    """Directory holding the region master JSON tables."""
    return raw_dir(store_root) / region / "source"


def region_source_dir(store_root: Path, region: str) -> Path:
    return raw_dir(store_root) / region / "source"
