"""One-time store migration: legacy source IDs -> altsource_sv / altsource_ms.

The two story CMS backends were renamed:

- ``sekai_viewer``          -> ``altsource_sv``
- ``sekai_viewer_i18n``     -> ``altsource_sv_i18n``
- ``altsource``             -> ``altsource_ms``
- ``altsource_translation`` -> ``altsource_ms_translation``

This module rewrites an existing store in place (web page directories,
page IDs, ``source`` fields, news records, TOS consent keys) and rebuilds
the regenerable web indexes.

Safety properties:

- ``dry_run=True`` reports every change without touching the store.
- Individual JSON files are rewritten atomically (write to a temp file, then
  ``os.replace``), so a crash never leaves a half-written record.
- A single unreadable or corrupt file is recorded under ``errors`` and the
  migration continues instead of aborting the whole run.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Optional

from sekaisync.layout import (
    news_dir,
    web_category_dir,
    web_consent_path,
    web_index_path,
    web_pages_path,
    web_root,
)
from sekaisync.sources import (
    ALL_STORED_SOURCES,
    LEGACY_IDS,
)

# Ordered longest-first so ``sekai_viewer_i18n`` is handled before
# ``sekai_viewer`` and ``altsource_translation`` before ``altsource``.
_ID_PREFIX_MAP = [
    ("web:sekai_viewer_i18n:", "web:altsource_sv_i18n:"),
    ("web:sekai_viewer:", "web:altsource_sv:"),
    ("web:altsource_translation:", "web:altsource_ms_translation:"),
    ("web:altsource:", "web:altsource_ms:"),
]

_NEWS_SEGMENT_MAP = [
    ("news:", "news:"),  # keep
    (":sekai_viewer:", ":altsource_sv:"),
    (":altsource:", ":altsource_ms:"),
]


def _remap_source(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return LEGACY_IDS.get(value, value)


def _remap_id(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    for old, new in _ID_PREFIX_MAP:
        if value.startswith(old):
            return new + value[len(old):]
    return value


def _write_file(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _patch_pages(path: Path) -> tuple[int, Optional[str]]:
    """Return (patched_count, new_text) for one pages.json without writing."""
    if not path.exists():
        return 0, None
    records = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        return 0, None
    patched = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        old_id = str(record.get("id") or "")
        old_source = str(record.get("source") or "")
        new_id = _remap_id(old_id)
        new_source = _remap_source(old_source)
        if new_id != old_id:
            record["id"] = new_id
            patched += 1
        if new_source != old_source:
            record["source"] = new_source
            patched += 1
    if patched:
        return patched, json.dumps(records, ensure_ascii=False, indent=2)
    return patched, None


def _patch_news(path: Path) -> tuple[int, Optional[str]]:
    """Return (patched_count, new_text) for one news file without writing."""
    if not path.exists():
        return 0, None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return 0, None
    records = data.get("news")
    if not isinstance(records, list):
        return 0, None
    patched = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        old_source = str(record.get("source") or "")
        old_id = str(record.get("id") or "")
        new_source = _remap_source(old_source)
        new_id = old_id
        for old_seg, new_seg in _NEWS_SEGMENT_MAP:
            if old_seg in new_id:
                new_id = new_id.replace(old_seg, new_seg)
        if new_source != old_source:
            record["source"] = new_source
            patched += 1
        if new_id != old_id:
            record["id"] = new_id
            patched += 1
    if patched:
        return patched, json.dumps(data, ensure_ascii=False, indent=2)
    return patched, None


def _patch_consent(store_root: Path) -> tuple[int, Optional[str]]:
    """Return (patched_count, new_text) for the consent file without writing."""
    path = web_consent_path(store_root)
    if not path.exists():
        return 0, None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return 0, None
    patched = 0
    updated: dict[str, Any] = {}
    for key, value in data.items():
        new_key = _remap_source(key)
        if isinstance(value, dict) and str(value.get("source") or "") in LEGACY_IDS:
            value = dict(value)
            value["source"] = _remap_source(value.get("source"))
            patched += 1
        if new_key != key:
            patched += 1
        updated[new_key] = value
    if patched:
        return patched, json.dumps(updated, ensure_ascii=False, indent=2)
    return patched, None


def rename_legacy_source_ids(
    store_root: Path,
    rebuild_index: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Migrate a store from legacy to canonical source IDs.

    ``dry_run=True`` reports every planned change without modifying the store.
    A corrupt individual file is recorded under ``errors`` and skipped.
    """
    store_root = Path(store_root)
    summary: dict[str, Any] = {
        "store": str(store_root.resolve()),
        "layout": "v2",
        "dry_run": dry_run,
        "renamed_dirs": {},
        "pages_patched": 0,
        "news_patched": 0,
        "consent_keys_patched": 0,
        "derived_files_patched": 0,
        "errors": [],
        "rebuild": None,
    }

    def error(message: str) -> None:
        summary["errors"].append(message)

    # 1. Rename legacy web page directories (cheap; reversible by mapping).
    web_root_dir = web_root(store_root)
    if web_root_dir.exists():
        for legacy, canonical in LEGACY_IDS.items():
            legacy_dir = web_root_dir / legacy
            target_dir = web_root_dir / canonical
            if not legacy_dir.is_dir():
                continue
            if target_dir.exists():
                summary["renamed_dirs"][legacy] = "skipped: target exists"
                continue
            if dry_run:
                summary["renamed_dirs"][legacy] = canonical
                continue
            try:
                legacy_dir.rename(target_dir)
                summary["renamed_dirs"][legacy] = canonical
            except OSError as exc:
                error(f"rename {legacy} -> {canonical} failed: {exc}")

    # 2. Patch page records and drop regenerable category artifacts.
    for source in ALL_STORED_SOURCES:
        path = web_pages_path(store_root, source)
        try:
            patched, text = _patch_pages(path)
        except (ValueError, OSError) as exc:
            error(f"pages {source}: {exc}")
        else:
            if patched:
                summary["pages_patched"] += patched
                if not dry_run:
                    _write_file(path, text)
        category_dir = web_category_dir(store_root, source)
        if category_dir.exists():
            if not dry_run:
                for child in category_dir.iterdir():
                    if not child.is_file():
                        continue
                    name = child.name
                    if name == "pages.json":
                        continue
                    if name == "categories.json" or (name.startswith("0") and name.endswith(".json")):
                        child.unlink()
                if not any(category_dir.iterdir()):
                    try:
                        category_dir.rmdir()
                    except OSError:
                        pass

    # 3. Remove regenerable category caches left under legacy names.
    for legacy in LEGACY_IDS:
        legacy_category = web_category_dir(store_root, legacy)
        if legacy_category.exists() and not dry_run:
            shutil.rmtree(legacy_category, ignore_errors=True)

    # 4. Patch news records.
    news_root = news_dir(store_root)
    if news_root.exists():
        for path in sorted(news_root.glob("*.json")):
            try:
                patched, text = _patch_news(path)
            except (ValueError, OSError) as exc:
                error(f"news {path.name}: {exc}")
                continue
            if patched:
                summary["news_patched"] += patched
                if not dry_run:
                    _write_file(path, text)

    # 5. Patch TOS consent keys.
    try:
        patched, text = _patch_consent(store_root)
    except (ValueError, OSError) as exc:
        error(f"consent: {exc}")
    else:
        if patched:
            summary["consent_keys_patched"] += patched
            if not dry_run:
                _write_file(web_consent_path(store_root), text)

    # 6. Drop the regenerable merged index so rebuild recreates it.
    derived = web_index_path(store_root)
    if derived.exists() and not dry_run:
        derived.unlink()

    if rebuild_index and not dry_run:
        from sekaisync.webindex import rebuild_web_index

        try:
            summary["rebuild"] = rebuild_web_index(store_root)
        except Exception as exc:  # keep the migration usable even if index rebuild fails
            summary["rebuild"] = {"error": str(exc)}
    return summary
