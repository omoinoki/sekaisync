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

from sekaisync.config import REGIONS, ViewerSettings
from sekaisync.eventalias import _build_jp_box_map, _load_json
from sekaisync.layout import events_archive_path, region_master_dir, region_source_dir

EVENT_BASE_TABLES = ("events", "eventStories", "eventCards", "cards", "eventMusics", "musics")

ARCHIVE_PATH = "events/archive.json"

JST = timezone(timedelta(hours=9))

# altsource_sv (Sekai Viewer) master endpoint; overridable via settings.json.
_SV_MASTER_BASE = ViewerSettings().master_base


def apply_master_base(master_base: Optional[str]) -> None:
    """Point remote master-table fetches at a configured altsource_sv base."""
    global _SV_MASTER_BASE
    _SV_MASTER_BASE = str(master_base or "").rstrip("/") or ViewerSettings().master_base


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
    return f"{_SV_MASTER_BASE}/{repo}/{table}.json"


def _source_write_dir(store_root: Path, region: str) -> Path:
    source = region_source_dir(store_root, region)
    if source.exists():
        folders = [p for p in sorted(source.iterdir()) if p.is_dir()]
        if folders:
            return folders[0]
    source.mkdir(parents=True, exist_ok=True)
    return source
def _local_table_path(store_root: Path, region: str, table: str) -> Optional[Path]:
    base = region_master_dir(store_root, region)
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
    path = _local_table_path(store_root, region, "events")
    if path is None:
        return []
    return _load_json(path)


def fetch_remote_events(
    region: str,
    fetcher: Optional[Callable[[str], str]] = None,
    timeout: int = 30,
) -> list[dict[str, Any]]:
    if not _SV_MASTER_BASE:
        raise ValueError(
            "No Sekai Viewer master_base configured. Configure it in settings.json "
            "(see README「配置数据源」) before running event checks."
        )
    fetch = fetcher or default_fetcher
    raw = fetch(_remote_master_url(region, "events"), timeout)
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("records", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            data = []
    return [item for item in data if isinstance(item, dict)]


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
    seen: dict[str, dict[str, Any]] = {}
    for record in existing:
        if not isinstance(record, dict):
            continue
        key = str(record.get("id"))
        if key:
            seen.setdefault(key, record)
    for record in incoming:
        if not isinstance(record, dict):
            continue
        key = str(record.get("id"))
        if key:
            seen.setdefault(key, record)
    return [seen[key] for key in sorted(seen, key=lambda k: (len(k), k))]


def _merge_event_rows(
    existing: list[dict[str, Any]],
    incoming: Iterable[dict[str, Any]],
    new_event_ids: set[str],
) -> list[dict[str, Any]]:
    """Merge tables whose records are tied to an event id (eventCards / eventMusics)."""
    result = list(existing)
    seen = {str(record.get("id")) for record in result if record.get("id") is not None}
    for record in incoming:
        if not isinstance(record, dict):
            continue
        event_id = str(record.get("eventId") or "")
        if event_id and event_id not in new_event_ids:
            continue
        key = str(record.get("id"))
        if key in seen:
            continue
        if key:
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
    write_dir = _source_write_dir(store_root, region)
    counts: dict[str, int] = {}
    new_card_ids: set[str] = set()
    for row in tables.get("eventCards", []):
        if str(row.get("eventId") or "") in new_event_ids:
            card_id = str(row.get("cardId"))
            if card_id:
                new_card_ids.add(card_id)

    for table in EVENT_BASE_TABLES:
        path = write_dir / f"{table}.json"
        existing = _load_json(path) if path.exists() else []
        incoming = tables.get(table, [])
        if table in {"eventCards", "eventMusics"}:
            merged = _merge_event_rows(existing, incoming, new_event_ids)
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
    """Classify events as box / world_bloom / other using local master data."""
    if box_event_ids is None:
        box_event_ids = _jp_box_event_ids(store_root)
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
        }
    return classified


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

    for region in selected:
        local = load_local_events(store_root, region)
        if not local and not allow_initial:
            summary["regions"][region] = {
                "status": "no_local_baseline",
                "reason": "local events table not found; run sync or events check first",
                "new_events": [],
            }
            continue
        try:
            remote = fetch_remote_events(region, fetcher=fetcher, timeout=timeout)
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
            "new_events": [],
        }
        if new_events:
            try:
                tables: dict[str, list[dict[str, Any]]] = {}
                for table in EVENT_BASE_TABLES:
                    if fetcher is not None:
                        raw = fetcher(_remote_master_url(region, table), timeout)
                    else:
                        raw = default_fetcher(_remote_master_url(region, table), timeout)
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        for key in ("records", "items", "data"):
                            if isinstance(data.get(key), list):
                                data = data[key]
                                break
                        else:
                            data = []
                    tables[table] = [item for item in data if isinstance(item, dict)]
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
                "events": all_entries,
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
                "events": all_entries,
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
        if limit is not None:
            events = events[-limit:]
        out[region] = events
    return {"regions": out, "total": sum(len(v) for v in out.values())}








