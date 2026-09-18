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
import sqlite3

from sekaisync import dbstore
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

    A real (non-dry-run) migration takes the store's writer lease: it rewrites
    source ids across files and then deletes/re-imports rows, so it must not
    interleave with a sync or crawl.  A dry run changes nothing and therefore
    takes no lease (Astra P13).
    """
    if dry_run:
        return _rename_legacy_source_ids_impl(
            store_root, rebuild_index=rebuild_index, dry_run=True
        )
    from sekaisync.fetcher import store_writer_lock

    with store_writer_lock(store_root):
        return _rename_legacy_source_ids_impl(
            store_root, rebuild_index=rebuild_index, dry_run=False
        )


def _migrate_page_rows(store_root: Path, summary: dict[str, Any], dry_run: bool) -> bool:
    """Rename SQL identities without serializing or replacing row payloads.

    Existing SQL is always authoritative, even without an import marker. Only
    an absent database may bootstrap from JSON, in the same row transaction.
    Any destination collision aborts the entire migration; neither row wins.
    """
    dbstore.require_unbound_writer(store_root)
    state = dbstore.inspect_schema(store_root)
    absent = state.status == dbstore.SCHEMA_ABSENT
    if not state.is_usable:
        raise dbstore.SchemaVersionError(f"Cannot migrate store: {state.detail}", status=state.status)
    bootstrap = []
    if absent:
        for path in sorted(web_root(store_root).glob("*/pages.json")):
            items = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
                raise ValueError(f"Invalid page list: {path}")
            for seq, item in enumerate(items, 1):
                row = list(dbstore._page_dict_to_row(path.parent.name, item))
                row[-1] = seq
                bootstrap.append(tuple(row))
    if dry_run and absent:
        keys = [(row[0], row[1]) for row in bootstrap]
        conn = None
    else:
        if not dry_run:
            dbstore.initialize(store_root)  # schema only; never import stale JSON
        conn = sqlite3.connect(
            dbstore.db_file(store_root).resolve().as_uri() + ("?mode=ro" if dry_run else "?mode=rw"),
            uri=True,
        )
        conn.execute("BEGIN" if dry_run else "BEGIN IMMEDIATE")
        keys = conn.execute("SELECT source, id FROM web_pages ORDER BY source, id").fetchall()
        keys.extend((row[0], row[1]) for row in bootstrap)
    try:
        destinations = {}
        changes = []
        for source, page_id in keys:
            target = (_remap_source(source), _remap_id(page_id))
            if target in destinations:
                summary["conflicts"].append({
                    "target": list(target), "records": [list(destinations[target]), [source, page_id]],
                })
            else:
                destinations[target] = (source, page_id)
            if target != (source, page_id):
                changes.append((*target, source, page_id))
        summary["db_pages_patched"] = len(changes)
        if summary["conflicts"]:
            summary["errors"].append({"stage": "database", "error": "source identity conflicts; no rows changed"})
            return False
        if not dry_run:
            if bootstrap:
                conn.executemany(dbstore._PAGE_INSERT, bootstrap)
            for change in changes:
                conn.execute("UPDATE web_pages SET source=?, id=? WHERE source=? AND id=?", change)
            # Prevent future lazy imports from resurrecting pre-migration JSON.
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('imported_pages', '1')")
            if changes or bootstrap:
                summary["revision"] = dbstore.bump_revision(conn)
            else:
                summary["revision"] = dbstore.current_revision(conn)
            conn.commit()
            summary["db_committed"] = True
        return True
    except BaseException:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            conn.close()


def _rename_legacy_source_ids_impl(
    store_root: Path,
    rebuild_index: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
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
        "conflicts": [],
        "db_pages_patched": 0,
        "db_committed": False,
    }

    def error(message: str) -> None:
        summary["errors"].append(message)

    try:
        if not _migrate_page_rows(store_root, summary, dry_run):
            summary["ok"] = False
            return summary
    except Exception as exc:
        summary["errors"].append({"stage": "database", "error": f"{type(exc).__name__}: {exc}"})
        summary["ok"] = False
        return summary

    # Only after SQL commits may any file be changed. These page files are
    # projections, never migration inputs for an existing database.
    page_sources = dbstore.load_web_pages(store_root) if not dry_run else {}

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
    for source in sorted(set(ALL_STORED_SOURCES) | set(page_sources)):
        path = web_pages_path(store_root, source)
        try:
            if dry_run:
                patched, _ = _patch_pages(path)
                summary["pages_patched"] += patched
            elif source in page_sources or path.exists():
                items = page_sources.get(source, [])
                path.parent.mkdir(parents=True, exist_ok=True)
                _write_file(path, json.dumps(items, ensure_ascii=False, indent=2))
                summary["pages_patched"] += len(items)
        except (ValueError, OSError) as exc:
            error(f"pages {source}: {exc}")
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

    # 6. The merged index is now a DB concern: import the renamed pages
    # and recompute their metadata columns.
    derived = web_index_path(store_root)
    if derived.exists() and not dry_run:
        derived.unlink()

    if rebuild_index and not dry_run:
        from sekaisync.webindex import rebuild_web_index

        try:
            summary["rebuild"] = rebuild_web_index(store_root)
            # The shared rebuild round-trips rows through WebPage, whose
            # dataclass does not model unknown metadata; restore those fields
            # from the pre-rebuild DB snapshot so the round-trip is lossless.
            modelled = set(dbstore._PAGE_COLUMNS) | {"source"}
            with dbstore.connect(store_root) as conn:
                for source, items in page_sources.items():
                    for item in items:
                        unknown = {
                            key: value for key, value in item.items()
                            if key not in modelled
                        }
                        if not unknown:
                            continue
                        row = conn.execute(
                            "SELECT extra_json FROM web_pages WHERE source=? AND id=?",
                            (source, item["id"]),
                        ).fetchone()
                        if row is None:
                            continue
                        extra = json.loads(row[0] or "{}")
                        missing = {
                            key: value for key, value in unknown.items()
                            if key not in extra
                        }
                        if missing:
                            extra.update(missing)
                            conn.execute(
                                "UPDATE web_pages SET extra_json=? WHERE source=? AND id=?",
                                (json.dumps(extra, ensure_ascii=False), source, item["id"]),
                            )
                conn.commit()
        except Exception as exc:
            summary["rebuild"] = {"error": str(exc)}
            summary["errors"].append(
                {"stage": "rebuild", "error": f"{type(exc).__name__}: {exc}"}
            )
    summary["ok"] = not summary["errors"]
    return summary
