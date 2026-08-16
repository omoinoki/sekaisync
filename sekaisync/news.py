from __future__ import annotations

import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sekaisync.config import REGIONS, MoesekaiSettings, SiteSettings, ViewerSettings, require_endpoint
from sekaisync.layout import news_dir, news_file_path
from sekaisync.normalize import normalize_name
from sekaisync.sources import (
    BACKEND_MOESEKAI,
    BACKEND_SEKAI_VIEWER,
    DEFAULT_SOURCE_PRIORITY,
    SOURCE_MS,
    SOURCE_SV,
    backend_of,
    normalize_source_id,
    source_rank,
)


_LANGUAGE_MAP = {
    "zh-cn": "zh_hans",
    "zh_hans": "zh_hans",
    "zh-tw": "zh_hant",
    "zh-hant": "zh_hant",
    "ja-jp": "ja",
    "ja": "ja",
    "en-us": "en",
    "en": "en",
    "ko-kr": "ko",
    "ko": "ko",
}


def _default_fetcher(url: str, timeout: int = 30) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SekaiSync/0.3 (+news sync)",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _normalize_language(value: Any) -> str:
    code = str(value or "").strip().lower()
    return _LANGUAGE_MAP.get(code, code)


def _timestamp_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso(ts: Optional[int]) -> Optional[str]:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


def _news_key(language: str, title: str, url: str, source_id: str) -> str:
    title_key = normalize_name(title) or normalize_name(url)
    return f"{language}:{title_key or source_id}"


def _is_website_announcement(record: dict[str, Any]) -> bool:
    """Sekai Viewer's own site announcements are not official game news."""
    url = str(record.get("url") or "")
    if "strapi.sekai.best/announcements" not in url:
        return False
    source_type = str(record.get("source_type") or "").strip().lower()
    if source_type:
        return source_type == BACKEND_SEKAI_VIEWER
    return normalize_source_id(str(record.get("source") or "")) == SOURCE_SV


def fetch_altsource_ms_news(
    region: str,
    fetcher: Callable[[str], str] = _default_fetcher,
    settings: Optional[MoesekaiSettings] = None,
    instance: Optional[str] = None,
) -> list[dict[str, Any]]:
    settings = settings or MoesekaiSettings()
    source_id = instance or SOURCE_MS
    if region not in {"cn", "jp"}:
        return []
    news_base = require_endpoint(settings.news_base, "news_base", source_id)
    url = f"{news_base}/{region}/information?_ts={int(time.time() * 1000)}"
    try:
        data = json.loads(fetcher(url))
    except (ValueError, OSError):
        return []
    items = data.get("informations", []) if isinstance(data, dict) else []
    language = REGIONS[region].language
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", ""))
        title = str(item.get("title") or "")
        path = str(item.get("path") or "")
        start = _timestamp_ms(item.get("startAt"))
        end = _timestamp_ms(item.get("endAt"))
        text_parts = [
            title,
            str(item.get("informationTag") or ""),
            str(item.get("informationType") or ""),
            path,
        ]
        records.append(
            {
                "id": f"news:{language}:{source_id}:{item_id}",
                "source": source_id,
                "source_type": BACKEND_MOESEKAI,
                "source_id": item_id,
                "language": language,
                "title": title,
                "text": "\n".join(part for part in text_parts if part).strip(),
                "url": path,
                "start_at": _iso(start),
                "end_at": _iso(end),
                "published_at": _iso(start),
                "canonical_key": _news_key(language, title, path, item_id),
                "kind": "game_news",
                "trust": "B",
            }
        )
    return records


def fetch_altsource_sv_game_news(
    region: str,
    fetcher: Callable[[str], str] = _default_fetcher,
    settings: Optional[ViewerSettings] = None,
    instance: Optional[str] = None,
) -> list[dict[str, Any]]:
    settings = settings or ViewerSettings()
    source_id = instance or SOURCE_SV
    region_info = REGIONS.get(region)
    if region_info is None or not region_info.repo_slug:
        return []
    repo = region_info.repo_slug.rsplit("/", 1)[-1]
    master_base = require_endpoint(settings.master_base, "master_base", source_id)
    url = f"{master_base}/{repo}/userInformations.json"
    try:
        data = json.loads(fetcher(url))
    except (ValueError, OSError):
        return []
    items = data if isinstance(data, list) else []
    language = region_info.language
    records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id", ""))
        title = str(item.get("title") or "")
        path = str(item.get("path") or "")
        start = _timestamp_ms(item.get("startAt"))
        end = _timestamp_ms(item.get("endAt"))
        text_parts = [
            title,
            str(item.get("informationTag") or ""),
            str(item.get("informationType") or ""),
            path,
        ]
        records.append(
            {
                "id": f"news:{language}:{source_id}:{item_id}",
                "source": source_id,
                "source_type": BACKEND_SEKAI_VIEWER,
                "source_id": item_id,
                "language": language,
                "title": title,
                "text": "\n".join(part for part in text_parts if part).strip(),
                "url": path,
                "start_at": _iso(start),
                "end_at": _iso(end),
                "published_at": _iso(start),
                "canonical_key": _news_key(language, title, path, item_id),
                "kind": "game_news",
                "trust": "B",
            }
        )
    return records


def merge_news(
    records: Iterable[dict[str, Any]],
    source_priority: Iterable[str] = DEFAULT_SOURCE_PRIORITY,
) -> list[dict[str, Any]]:
    priority = tuple(normalize_source_id(item) for item in source_priority)
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        if _is_website_announcement(record):
            continue
        key = str(record.get("canonical_key") or record.get("id") or "")
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(record)
            continue
        if _prefer_record(existing, record, priority):
            merged[key] = dict(record)
    return sorted(merged.values(), key=lambda item: (item.get("language", ""), item.get("start_at") or ""))


def _prefer_record(
    existing: dict[str, Any],
    record: dict[str, Any],
    priority: tuple[str, ...],
) -> bool:
    existing_source = str(existing.get("source") or "")
    record_source = str(record.get("source") or "")
    existing_rank = source_rank(existing_source, priority)
    record_rank = source_rank(record_source, priority)
    if record_rank != existing_rank:
        return record_rank < existing_rank
    return len(str(record.get("text") or "")) > len(str(existing.get("text") or ""))


def save_news(records: Iterable[dict[str, Any]], store_root: Path) -> Path:
    records = list(records)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        language = str(record.get("language") or "other")
        grouped.setdefault(language, []).append(record)
    for language, items in grouped.items():
        path = news_file_path(store_root, language)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "language": language,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "news": items,
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return news_dir(store_root)


def load_news(store_root: Path) -> list[dict[str, Any]]:
    root = news_dir(store_root)
    if not root.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        records.extend(
            item
            for item in data.get("news", [])
            if isinstance(item, dict) and not _is_website_announcement(item)
        )
    return records

def news_available(store_root: Path) -> bool:
    return bool(load_news(store_root))


def news_summary(store_root: Path) -> dict[str, Any]:
    records = load_news(store_root)
    by_language: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for record in records:
        language = str(record.get("language") or "unknown")
        source = str(record.get("source") or "unknown")
        by_language[language] = by_language.get(language, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1
    return {
        "available": bool(records),
        "count": len(records),
        "languages": by_language,
        "sources": by_source,
    }


def _dispatch_entries(
    sources: Iterable[str],
    sites: Optional[Iterable[SiteSettings]],
) -> list[tuple[str, str, object]]:
    """Resolve requested source selectors to (instance_id, backend, settings).

    With a profile, selectors may be instance IDs or backend class IDs
    (``altsource_sv`` / ``altsource_ms``), the latter expanding to every
    enabled instance of that class in profile order.  Without a profile the
    canonical type IDs map to the built-in default instances.
    """
    requested = [normalize_source_id(str(item)) for item in sources if str(item).strip()]
    entries: list[tuple[str, str, object]] = []
    if sites:
        site_list = list(sites)
        by_backend: dict[str, list[SiteSettings]] = {}
        for site in site_list:
            if site.enabled:
                by_backend.setdefault(site.backend, []).append(site)
        seen: set[str] = set()
        for selector in requested:
            if selector in {SOURCE_SV, SOURCE_MS}:
                backend = BACKEND_SEKAI_VIEWER if selector == SOURCE_SV else BACKEND_MOESEKAI
                for site in by_backend.get(backend, []):
                    if site.id in seen:
                        continue
                    seen.add(site.id)
                    entries.append((site.id, backend, site.settings_for(site.id)))
                continue
            site = next((s for s in site_list if s.id == selector), None)
            if site is None:
                continue
            if site.id in seen:
                continue
            seen.add(site.id)
            entries.append((site.id, site.backend, site.settings_for(site.id)))
        return entries
    if not requested:
        requested = list(DEFAULT_SOURCE_PRIORITY)
    for selector in requested:
        backend = backend_of(selector)
        if backend == BACKEND_MOESEKAI:
            entries.append((selector, backend, MoesekaiSettings()))
        elif backend == BACKEND_SEKAI_VIEWER:
            entries.append((selector, backend, ViewerSettings()))
    return entries


def sync_news(
    store_root: Path,
    regions: Iterable[str] = ("jp", "cn"),
    sources: Optional[Iterable[str]] = None,
    fetcher: Callable[[str], str] = _default_fetcher,
    settings: Optional[MoesekaiSettings] = None,
    viewer_settings: Optional[ViewerSettings] = None,
    source_priority: Optional[Iterable[str]] = None,
    sites: Optional[Iterable[SiteSettings]] = None,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    entries = _dispatch_entries(sources or (), sites)
    for instance_id, backend, site_settings in entries:
        if backend == BACKEND_MOESEKAI:
            ms = site_settings if isinstance(site_settings, MoesekaiSettings) else (settings or MoesekaiSettings())
            for region in regions:
                if region in {"cn", "jp"}:
                    records.extend(
                        fetch_altsource_ms_news(region, fetcher, ms, instance=instance_id)
                    )
        elif backend == BACKEND_SEKAI_VIEWER:
            sv = site_settings if isinstance(site_settings, ViewerSettings) else (viewer_settings or ViewerSettings())
            for region in regions:
                if region in REGIONS:
                    records.extend(
                        fetch_altsource_sv_game_news(region, fetcher, sv, instance=instance_id)
                    )
    if source_priority:
        priority = tuple(normalize_source_id(item) for item in source_priority)
    elif sites:
        priority = tuple(entry[0] for entry in entries)
    else:
        priority = DEFAULT_SOURCE_PRIORITY
    # Also purge any previously imported Sekai Viewer site announcements on disk.
    records.extend(
        record
        for record in load_news(store_root)
        if not _is_website_announcement(record)
    )
    merged = merge_news(records, source_priority=priority)
    save_news(merged, store_root)
    return {
        "fetched": len(records),
        "merged": len(merged),
        "summary": news_summary(store_root),
    }
