"""Lightweight new-event detection and base-data sync.

This module is deliberately separate from the story crawler. On every
SekaiSync run it can compare the remote master events table with the local
source files, download only the master tables needed to describe the new
events, merge them into the local source tree, classify each new event as a
character box event, a World Link (WL) event, or other, and archive the result
so the progress denominator grows without touching the web crawler.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sekaisync.config import REGIONS
from sekaisync.endpoints import current_endpoints
from sekaisync.eventalias import _build_jp_box_map
from sekaisync.layout import (
    events_archive_path,
    master_source_dir,
    region_master_dir,
    region_source_dir,
    read_master_table,
    _records_from_master_payload,
)

EVENT_BASE_TABLES = ("events", "eventStories", "eventCards", "cards", "eventMusics", "musics")

ARCHIVE_PATH = "events/archive.json"

JST = timezone(timedelta(hours=9))

# altsource_sv (Sekai Viewer) master endpoint; overridable via settings.json.
def apply_master_base(master_base: Optional[str]) -> None:
    """Point remote master-table fetches at a configured altsource_sv base."""
    from sekaisync.endpoints import configure_endpoints

    configure_endpoints(sv_master_base=master_base)


def jst_today() -> str:
    """Return the current calendar date in Tokyo time (JST has no DST)."""
    return datetime.now(JST).date().isoformat()


def default_fetcher(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "SekaiSync/0.3 (+event check)", "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _remote_master_url(region: str, table: str) -> str:
    region_info = REGIONS.get(region)
    repo = (region_info.repo_slug or "Sekai-World/sekai-master-db-diff").rsplit("/", 1)[-1]
    return f"{current_endpoints().ALTSOURCE_SV_MASTER_BASE}/{repo}/{table}.json"


def _source_write_dir(store_root: Path, region: str) -> Path:
    source = region_source_dir(store_root, region)
    if source.exists():
        folders = [p for p in sorted(source.iterdir()) if p.is_dir()]
        if folders:
            return folders[0]
    source.mkdir(parents=True, exist_ok=True)
    return source
def _local_table_path(store_root: Path, region: str, table: str) -> Optional[Path]:
    base = master_source_dir(store_root, region)
    patterns = (
        base / "**" / f"{table}.json",
        base / f"{table}.json",
        base / "master" / f"{table}.json",
        base / "db" / f"{table}.json",
    )
    for pattern in patterns:
        matches = sorted(base.glob(str(pattern.relative_to(base))))
        if matches:
            return matches[0]
    return None
def load_local_events(store_root: Path, region: str) -> list[dict[str, Any]]:
    # Compatibility list API; completeness-sensitive callers use TableRead.
    from sekaisync.registry import capture_raw_snapshot

    return read_master_table(capture_raw_snapshot(store_root, [region]), region, "events").records


def fetch_remote_events(
    region: str,
    fetcher: Optional[Callable[[str], str]] = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    if not current_endpoints().ALTSOURCE_SV_MASTER_BASE:
        raise ValueError(
            "No Sekai Viewer master_base configured. Configure it in settings.json "
            "(see README「配置数据源」) before running event checks."
        )
    fetch = fetcher or default_fetcher
    raw = fetch(_remote_master_url(region, "events"), timeout)
    return _parse_remote_table(raw, "events")


class InvalidMasterTable(ValueError):
    """Fetched JSON is not a complete, structurally valid master table."""


def _parse_remote_table(raw: str, table: str) -> list[dict[str, Any]]:
    try:
        records = _records_from_master_payload(json.loads(raw))
    except (ValueError, UnicodeError) as exc:
        raise InvalidMasterTable(f"{table}: invalid JSON: {exc}") from exc
    if records is None:
        raise InvalidMasterTable(f"{table}: expected a list of objects or supported list wrapper")
    return records


def detect_new_events(
    remote: Iterable[dict[str, Any]],
    local: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    known = {str(record.get("id")) for record in local if record.get("id") is not None}
    new = []
    for record in remote:
        event_id = str(record.get("id"))
        if not event_id or event_id in known:
            continue
        new.append(record)
    new.sort(key=lambda record: int(record.get("id") or 0))
    return new



def _merge_table_records(
    existing: list[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge rows by natural key.

    Rows are keyed by :func:`_row_identity` rather than ``id`` alone. The old
    ``if key:`` guard silently **dropped** every row without an ``id`` — fine
    while every table in this path has one, but a trap the moment a table like
    ``eventMusics`` (137/137 rows id-less in the real store) is routed here.
    Unkeyed rows now keep distinct identities instead of vanishing.
    """
    seen: dict[str, dict[str, Any]] = {}
    for record in existing:
        if not isinstance(record, dict):
            continue
        seen.setdefault(_row_identity("", record), record)
    for record in incoming:
        if not isinstance(record, dict):
            continue
        seen.setdefault(_row_identity("", record), record)
    return [seen[key] for key in sorted(seen, key=lambda k: (len(k), k))]


#: Relation tables have no single ``id``; their identity is the combination of
#: fields that actually identifies the relationship. ``seq`` is deliberately
#: excluded: it is ordering/versioning, not identity, so including it would let
#: the same relationship be appended again under a new seq.
_RELATION_NATURAL_KEYS: dict[str, tuple[str, ...]] = {
    "eventMusics": ("eventId", "musicId"),
    "eventCards": ("eventId", "cardId"),
    "eventDeckBonus": ("eventId", "cardId", "bonusType"),
}


def _row_identity(table: str, record: dict[str, Any]) -> str:
    """Natural key for one row, per Astra P16/D16.

    Entity tables use ``id``. Relation tables use their actual relationship
    fields. A row with **no usable key** gets a unique identity derived from its
    full content, so distinct rows are never merged — the previous code keyed
    every key-less row as ``""``, so only the first survived and the rest were
    silently dropped. Identical rows still deduplicate, which is what "去掉完整
    行相同的重复" asks for.
    """
    fields = _RELATION_NATURAL_KEYS.get(table)
    if fields:
        parts = [str(record.get(field) or "") for field in fields]
        if any(parts):
            return f"{table}:" + "\x1f".join(parts)

    value = record.get("id")
    if value is not None and str(value) != "":
        return f"{table}:id:{value}"

    # No natural key at all: fall back to the whole row's content, so two
    # genuinely different rows cannot collide.
    try:
        return f"{table}:row:{json.dumps(record, sort_keys=True, ensure_ascii=False)}"
    except (TypeError, ValueError):
        return f"{table}:repr:{repr(sorted(record.items(), key=lambda kv: str(kv[0])))}"


def _merge_event_rows(
    existing: list[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
    new_event_ids: set[str],
    table: str = "",
) -> list[dict[str, Any]]:
    """Merge tables whose records are tied to an event id (eventCards / eventMusics).

    Passing ``table`` enables the relation natural key. Without it the legacy
    ``id``-only behaviour is kept, which is only safe for entity tables — callers
    merging relation rows must name the table or id-less rows are lost.
    """
    result = list(existing)
    seen = {
        _row_identity(table, record)
        for record in result
        if isinstance(record, dict)
    }
    for record in incoming:
        if not isinstance(record, dict):
            continue
        event_id = str(record.get("eventId") or "")
        if event_id and event_id not in new_event_ids:
            continue
        key = _row_identity(table, record)
        if key in seen:
            continue
        seen.add(key)
        result.append(record)
    return result


def _merge_cards(
    existing: list[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
    new_card_ids: set[str],
) -> list[dict[str, Any]]:
    """Merge only the cards that belong to the newly detected events."""
    result = list(existing)
    seen = {str(record.get("id")) for record in result if record.get("id") is not None}
    for record in incoming:
        if not isinstance(record, dict):
            continue
        card_id = str(record.get("id"))
        if card_id not in new_card_ids:
            continue
        if card_id in seen:
            continue
        seen.add(card_id)
        result.append(record)
    return result


def merge_new_event_tables(
    store_root: Path,
    region: str,
    tables: dict[str, list[dict[str, Any]]],
    new_event_ids: set[str],
) -> dict[str, int]:
    """Merge freshly downloaded tables into the local source tree.

    Only the small per-event tables are filtered; events/eventStories/musics are
    merged by primary key so future checks never duplicate them.
    """
    from sekaisync.registry import capture_raw_snapshot

    # Preflight the complete input and baseline before creating or writing any
    # file. This is not a six-file transaction; generation publication remains
    # the responsibility of sync, not this legacy incremental writer.
    for table in EVENT_BASE_TABLES:
        rows = tables.get(table)
        if not isinstance(rows, list) or _records_from_master_payload(rows) is None:
            raise InvalidMasterTable(f"{table}: missing or invalid incoming table")
    snapshot = capture_raw_snapshot(store_root, [region])
    if snapshot.generations.get(region):
        raise ValueError("published raw generation requires sync; legacy merge is unsupported")
    local_tables = {table: read_master_table(snapshot, region, table) for table in EVENT_BASE_TABLES}
    initial = all(result.status == "missing" for result in local_tables.values())
    for table, result in local_tables.items():
        if not result.ok and not initial:
            raise InvalidMasterTable(f"{table}: {result.status} local table; run sync")
    write_dir = _source_write_dir(store_root, region)
    counts: dict[str, int] = {}
    new_card_ids: set[str] = set()
    for row in tables.get("eventCards", []):
        if str(row.get("eventId") or "") in new_event_ids:
            card_id = str(row.get("cardId"))
            if card_id:
                new_card_ids.add(card_id)

    for table in EVENT_BASE_TABLES:
        result = local_tables[table]
        path = Path(result.source) if result.ok else write_dir / f"{table}.json"
        existing = result.records
        incoming = tables[table]
        if table in {"eventCards", "eventMusics"}:
            merged = _merge_event_rows(existing, incoming, new_event_ids, table=table)
        elif table == "cards":
            merged = _merge_cards(existing, incoming, new_card_ids)
        else:
            merged = _merge_table_records(existing, incoming)
        path.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
        counts[table] = len(incoming)
    return counts



def _jp_box_event_ids(store_root: Path) -> set[str]:
    """JP-derived box event ids shared by every region's classification."""
    box_map = _build_jp_box_map(store_root)
    return {
        str(box.event_id)
        for boxes in box_map.values()
        for box in boxes
    }


def _classify_events(
    store_root: Path,
    region: str,
    events: Iterable[dict[str, Any]],
    box_event_ids: Optional[set[str]] = None,
) -> dict[str, dict[str, Any]]:
    """Classify events as box / world_bloom / other using local master data.

    Placeholder test events (no cards / music / story in the master data,
    e.g. JP E166 / E186) are flagged ``placeholder=True`` so callers can
    exclude them from real activity sequence numbering.
    """
    if box_event_ids is None:
        box_event_ids = _jp_box_event_ids(store_root)
    placeholders = _jp_placeholder_event_ids(store_root)
    classified: dict[str, dict[str, Any]] = {}
    for event in events:
        event_id = str(event.get("id"))
        event_type = str(event.get("eventType") or "")
        if event_type == "world_bloom":
            category = "world_bloom"
            label = "WL"
        elif event_id in box_event_ids:
            category = "box"
            label = "箱活"
        else:
            category = "other"
            label = "其他"
        classified[event_id] = {
            "id": int(event_id),
            "name": event.get("name"),
            "start_at": event.get("startAt"),
            "event_type": event_type,
            "category": category,
            "label": label,
            "unit": event.get("unit"),
            "placeholder": event_id in placeholders,
        }
    return classified


def _jp_placeholder_event_ids(store_root: Path) -> set[str]:
    """Events with no cards/music/stories in JP master — never launched.
    JP ``events.json`` contains test placeholder entries that the official
    master never promoted to a real event (they have no ``eventCards`` /
    ``eventMusics`` / ``eventStories`` rows).  They are excluded from the
    real activity sequence count, which aligns SekaiSync's numbering with
    the fan-translation community's counting.
    """
    from sekaisync.registry import capture_raw_snapshot

    ids: set[str] = set()
    snapshot = capture_raw_snapshot(store_root, ["jp"])
    tables = {table: read_master_table(snapshot, "jp", table)
              for table in ("events", "eventCards", "eventMusics", "eventStories")}
    if not all(result.ok for result in tables.values()):
        # Missing evidence cannot establish that an event is a placeholder.
        return ids
    records = tables["events"].records

    def load_ids(table: str) -> set[str]:
        return {str(item.get("eventId") or item.get("id") or "")
                for item in tables[table].records}

    card_ids = load_ids("eventCards")
    music_ids = load_ids("eventMusics")
    story_ids = load_ids("eventStories")
    for record in records:
        if not isinstance(record, dict):
            continue
        event_id = str(record.get("id") or "")
        if event_id and event_id not in card_ids and event_id not in music_ids and event_id not in story_ids:
            ids.add(event_id)
    return ids


def _sequence_numbered(
    entries: list[dict[str, Any]],
    placeholders: set[str],
) -> list[dict[str, Any]]:
    """Attach ``sequence_no`` = real activity position, skipping placeholders.

    The official master id is not a dense sequence (JP contains test
    placeholder entries that never launched, e.g. E166 / E186).  Rebuilding
    the position from the events ordered by start time, excluding
    placeholders, aligns with the fan-translation community's counting.
    """
    real = sorted(
        (entry for entry in entries if str(entry.get("event_id")) not in placeholders),
        key=lambda entry: (
            str(entry.get("start_at") or ""),
            int(entry.get("event_id") or 0),
        ),
    )
    position = {str(entry["event_id"]): index for index, entry in enumerate(real, start=1)}
    numbered = []
    for entry in entries:
        item = dict(entry)
        item["sequence_no"] = position.get(str(entry.get("event_id")))
        item["placeholder"] = str(entry.get("event_id")) in placeholders
        numbered.append(item)
    return numbered


def _load_archive(store_root: Path) -> dict[str, Any]:
    path = events_archive_path(store_root)
    if not path.exists():
        return {"version": 1, "updated_at": None, "regions": {}, "history": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"version": 1, "updated_at": None, "regions": {}, "history": []}
    if not isinstance(data, dict):
        return {"version": 1, "updated_at": None, "regions": {}, "history": []}
    data.setdefault("regions", {})
    data.setdefault("history", [])
    return data


def save_archive(store_root: Path, data: dict[str, Any]) -> Path:
    path = events_archive_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path



def check_events(
    store_root: Path,
    regions: Optional[Iterable[str]] = None,
    fetcher: Optional[Callable[[str], str]] = None,
    timeout: int = 30,
    allow_initial: bool = False,
    daily_limit: bool = False,
) -> dict[str, Any]:
    """Detect new events, fetch base tables, classify, and archive them.

    Never starts the web crawler.  Returns a summary plus the newly detected
    events grouped by region.

    Regions without a local events baseline are skipped unless allow_initial
    is True, so an automatic run can never accidentally download the full
    master set on a brand-new store.

    When daily_limit is True, at most one automatic trigger is allowed per\r?\n    Tokyo calendar day. Any executed check refreshes the JST timestamp, so a\r?\n    failed attempt or an explicit `events check` / --force-event-check also\r?\n    satisfies the automatic daily limit; explicit commands can still run again\r?\n    because they do not consult the limit.
    """
    selected = [r for r in (regions or list(REGIONS)) if r in REGIONS]
    archive = _load_archive(store_root)
    now = datetime.now(timezone.utc).isoformat()
    summary: dict[str, Any] = {"regions": {}, "detected_total": 0, "crawler_started": False}
    history = list(archive.get("history", []))
    last_auto = archive.get("last_auto_check_at")
    if daily_limit and last_auto:
        try:
            last_date = datetime.fromisoformat(str(last_auto)).astimezone(JST).date().isoformat()
        except ValueError:
            last_date = None
        if last_date == jst_today():
            return {
                "regions": {},
                "detected_total": 0,
                "crawler_started": False,
                "daily_limit": True,
                "last_auto_check_at": last_auto,
                "archive": str((events_archive_path(store_root)).resolve()),
            }

    from sekaisync.registry import capture_raw_snapshot

    snapshot = capture_raw_snapshot(store_root, selected)
    for region in selected:
        local_tables = {table: read_master_table(snapshot, region, table) for table in EVENT_BASE_TABLES}
        table_states = {table: result.status for table, result in local_tables.items()}
        local = local_tables["events"].records
        initial = allow_initial and all(state == "missing" for state in table_states.values())
        if not initial and not all(result.ok for result in local_tables.values()):
            summary["regions"][region] = {
                "status": "no_local_baseline" if table_states["events"] == "missing" and not allow_initial else "incomplete_local",
                "reason": "required local tables missing or invalid; run sync to repair",
                "table_states": table_states,
                "new_events": [],
            }
            continue
        try:
            remote = fetch_remote_events(region, fetcher=fetcher, timeout=timeout)
        except InvalidMasterTable as exc:
            summary["regions"][region] = {
                "status": "fetch_failed", "reason": str(exc),
                "table_states": table_states, "new_events": [],
            }
            continue
        except ValueError:
            raise
        except Exception as exc:  # network unavailable: stay local, do not fail the run
            summary["regions"][region] = {
                "status": "skipped",
                "reason": f"remote fetch failed: {exc}",
                "new_events": [],
            }
            continue
        new_events = detect_new_events(remote, local)
        if (new_events or initial) and snapshot.generations.get(region):
            summary["regions"][region] = {
                "status": "sync_required",
                "reason": "active raw generation cannot be updated by legacy event merge; run sync",
                "table_states": table_states, "new_events": [],
            }
            continue
        box_event_ids_cache: Optional[set[str]] = None

        def box_event_ids() -> set[str]:
            nonlocal box_event_ids_cache
            if box_event_ids_cache is None:
                box_event_ids_cache = _jp_box_event_ids(store_root)
            return box_event_ids_cache

        region_result: dict[str, Any] = {
            "status": "ok" if new_events else "up_to_date",
            "local_events": len(local),
            "remote_events": len(remote),
            "table_states": table_states,
            "new_events": [],
        }
        if new_events or initial:
            try:
                # Reuse the events sample used for detection rather than
                # fetching it twice and possibly merging a different version.
                tables: dict[str, list[dict[str, Any]]] = {"events": remote}
                for table in EVENT_BASE_TABLES:
                    if table == "events":
                        continue
                    fetch = fetcher or default_fetcher
                    raw = fetch(_remote_master_url(region, table), timeout)
                    tables[table] = _parse_remote_table(raw, table)
                counts = merge_new_event_tables(
                    store_root,
                    region,
                    tables,
                    {str(event.get("id")) for event in new_events},
                )
            except Exception as exc:
                region_result["status"] = "fetch_failed"
                region_result["reason"] = str(exc)
                summary["regions"][region] = region_result
                continue

            classified = _classify_events(store_root, region, remote, box_event_ids=box_event_ids())
            event_entries = []
            for event in new_events:
                event_id = str(event.get("id"))
                classification = classified.get(event_id, {})
                entry = {
                    "region": region,
                    "event_id": int(event_id),
                    "name": event.get("name"),
                    "start_at": event.get("startAt"),
                    "category": classification.get("category", "other"),
                    "label": classification.get("label", "其他"),
                    "event_type": classification.get("event_type", event.get("eventType")),
                    "detected_at": now,
                    "base_tables": counts,
                }
                event_entries.append(entry)
                history.append(entry)
            region_result["new_events"] = event_entries
            summary["detected_total"] += len(event_entries)
            summary["regions"][region] = region_result
            existing_archive = archive.get("regions", {}).get(region, {}).get("events", [])
            all_entries = [
                {
                    "region": region,
                    "event_id": int(str(event.get("id"))),
                    "name": event.get("name"),
                    "start_at": event.get("startAt"),
                    "category": classified.get(str(event.get("id")), {}).get("category", "other"),
                    "label": classified.get(str(event.get("id")), {}).get("label", "其他"),
                    "event_type": classified.get(str(event.get("id")), {}).get("event_type", event.get("eventType")),
                    "detected_at": next(
                        (item.get("detected_at") for item in existing_archive if item.get("event_id") == int(str(event.get("id")))),
                        now,
                    ),
                }
                for event in remote
            ]
            archive["regions"][region] = {
                "last_checked_at": now,
                "last_event_count": len(remote),
                "events": _sequence_numbered(all_entries, _jp_placeholder_event_ids(store_root)),
            }
        else:
            region_result["status"] = "up_to_date"
            summary["regions"][region] = region_result
            classified = _classify_events(store_root, region, local, box_event_ids=box_event_ids())
            all_entries = [
                {
                    "region": region,
                    "event_id": int(str(event.get("id"))),
                    "name": event.get("name"),
                    "start_at": event.get("startAt"),
                    "category": classified.get(str(event.get("id")), {}).get("category", "other"),
                    "label": classified.get(str(event.get("id")), {}).get("label", "其他"),
                    "event_type": classified.get(str(event.get("id")), {}).get("event_type", event.get("eventType")),
                    "detected_at": now,
                }
                for event in local
            ]
            archive["regions"][region] = {
                "last_checked_at": now,
                "last_event_count": len(remote),
                "events": _sequence_numbered(all_entries, _jp_placeholder_event_ids(store_root)),
            }

    # Any executed check (auto, forced, or explicit events check) satisfies the
    # once-per-Tokyo-day automatic limit, so the auto path skips later reruns.
    archive["last_auto_check_at"] = now
    archive["updated_at"] = now
    archive["history"] = history[-500:]
    save_archive(store_root, archive)
    summary["archive"] = str((events_archive_path(store_root)).resolve())
    return summary


def list_events(
    store_root: Path,
    regions: Optional[Iterable[str]] = None,
    limit: Optional[int] = None,
) -> dict[str, Any]:
    archive = _load_archive(store_root)
    selected = [r for r in (regions or list(REGIONS)) if r in REGIONS]
    out: dict[str, list[dict[str, Any]]] = {}
    for region in selected:
        region_data = archive.get("regions", {}).get(region, {})
        events = region_data.get("events", [])
        # Lazy self-heal: archives written before sequence_no landed lack the
        # field.  Compute it on read so old stores answer without a forced
        # ``events check`` rerun.  Only recompute when any entry is missing it.
        if events and not any(
            entry.get("sequence_no") is not None or entry.get("placeholder") is not None
            for entry in events
        ):
            events = _sequence_numbered(events, _jp_placeholder_event_ids(store_root))
        if limit is not None:
            events = events[-limit:]
        out[region] = events
    return {"regions": out, "total": sum(len(v) for v in out.values())}








