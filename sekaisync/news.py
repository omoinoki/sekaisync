from __future__ import annotations

import json
import re
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


# In-game webview hosts for sv information detail pages (jp/en paths are
# relative to these; tc/kr/cn entries carry absolute URLs already).
_NEWS_WEB_BASES = {
    "jp": "https://production-web.sekai.colorfulpalette.org",
    "en": "https://n-production-web.sekai-en.com",
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
    """Deprecated: a title-derived key.

    Kept only so old snapshots still read. It cannot be an identity — many
    distinct announcements share a title ("メンテナンスのお知らせ"), so two
    different items collided and one was silently dropped. See
    :func:`news_identity`.
    """
    title_key = normalize_name(title) or normalize_name(url)
    return f"{language}:{title_key or source_id}"


def news_identity(item: dict[str, Any]) -> tuple[str, str, str]:
    """Stable identity for a news item: (namespace, upstream id, language).

    Astra P17/D17: identity is "upstream namespace / official source id or
    canonical URL / language". The title is a display field only — deriving
    identity from it merged genuinely distinct announcements that happened to
    share a heading, silently dropping one. That is the record-loss bug this
    replaces.

    **Namespace.** Two instances of the same backend are known mirrors of one
    upstream (``altsource_sv`` and a self-hosted viewer both serve Sekai
    Viewer), so the namespace is the backend and mirrors deduplicate. Different
    *backends* are different upstreams with unrelated id spaces; Astra requires
    "无证据不跨站合并", so they are never merged on title alone — id 1 from
    Moesekai and id 1 from Sekai Viewer are different announcements.
    """
    raw_source = str(item.get("source") or "").strip().lower()
    namespace = str(item.get("source_type") or "").strip().lower()
    if not namespace:
        namespace = backend_of(raw_source)
    if not namespace:
        # Unknown backend: fall back to the instance id, which at least keeps
        # one instance's records together without inventing a cross-site link.
        namespace = raw_source
    upstream = str(item.get("source_id") or "").strip()
    language = str(item.get("language") or "").strip().lower()

    # The upstream id is the primary identity. The canonical URL is only used
    # when there is no id at all — preferring it over a present id would merge
    # genuinely distinct posts that a fixture happens to give the same URL.
    if not upstream:
        canonical = str(item.get("page_url") or item.get("url") or "").strip()
        upstream = canonical
    if not upstream:
        # Last resort, and the only case where the title participates.
        upstream = normalize_name(str(item.get("title") or "")) or str(
            item.get("id") or ""
        )
    return (namespace, upstream, language)


def news_identity_key(item: dict[str, Any]) -> str:
    """``news_identity`` as a single stable string."""
    namespace, upstream, language = news_identity(item)
    return f"{namespace}|{upstream}|{language}"


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
        information_type = str(item.get("informationType") or "")
        information_tag = str(item.get("informationTag") or "")
        text_parts = [
            title,
            information_tag,
            information_type,
            path,
        ]
        page_url = path
        if path and not path.startswith(("http://", "https://", "weixin://")):
            web_base = _NEWS_WEB_BASES.get(region)
            page_url = f"{web_base}/{path.lstrip('/')}" if web_base else path
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
                "information_type": information_type,
                "information_tag": information_tag,
                "browse_type": str(item.get("browseType") or ""),
                "platform": str(item.get("platform") or ""),
                "body_available": False,
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
        information_type = str(item.get("informationType") or "")
        information_tag = str(item.get("informationTag") or "")
        if not title:
            # The tc diff feed omits `title` on most entries; fall back to
            # tag + id so the record stays identifiable.
            title = f"{information_tag or information_type or 'news'} #{item_id}"
        text_parts = [
            title,
            information_tag,
            information_type,
            path,
        ]
        page_url = path
        if path and not path.startswith(("http://", "https://")):
            web_base = _NEWS_WEB_BASES.get(region)
            page_url = f"{web_base}/{path.lstrip('/')}" if web_base else path
        records.append(
            {
                "id": f"news:{language}:{source_id}:{item_id}",
                "source": source_id,
                "source_type": BACKEND_SEKAI_VIEWER,
                "source_id": item_id,
                "language": language,
                "title": title,
                "text": "\n".join(part for part in text_parts if part).strip(),
                "url": page_url,
                "start_at": _iso(start),
                "end_at": _iso(end),
                "published_at": _iso(start),
                "canonical_key": _news_key(language, title, path, item_id),
                "kind": "game_news",
                "trust": "B",
                "information_type": information_type,
                "information_tag": information_tag,
                "browse_type": str(item.get("browseType") or ""),
                "platform": str(item.get("platform") or ""),
                "body_available": False,
            }
        )
    return records


def merge_news(
    records: Iterable[dict[str, Any]],
    source_priority: Iterable[str] = DEFAULT_SOURCE_PRIORITY,
) -> list[dict[str, Any]]:
    """Merge news by stable upstream identity, newest revision first.

    Identity comes from :func:`news_identity` (namespace / upstream id /
    language), so two announcements that share a title but have different
    upstream ids are both kept (Astra D17: "同标题不同 ID 均保留").
    """
    priority = tuple(normalize_source_id(item) for item in source_priority)
    merged: dict[str, dict[str, Any]] = {}
    for record in records:
        if _is_website_announcement(record):
            continue
        key = news_identity_key(record)
        existing = merged.get(key)
        if existing is None:
            merged[key] = dict(record)
            continue
        if _prefer_record(existing, record, priority):
            merged[key] = dict(record)
    return sorted(
        merged.values(),
        key=lambda item: (item.get("language", ""), item.get("start_at") or ""),
    )


def _revision_time(record: dict[str, Any]) -> str:
    """Best available upstream revision time for a news record.

    Astra P17: prefer the source's own update time/version, then the fetch
    time. Never infer recency from body length — a revision can shorten text.
    """
    for field in (
        "updated_at",
        "source_updated_at",
        "published_at",
        "start_at",
        "fetched_at",
    ):
        value = str(record.get(field) or "").strip()
        if value:
            return value
    return ""


def _prefer_record(
    existing: dict[str, Any],
    record: dict[str, Any],
    priority: tuple[str, ...],
) -> bool:
    """Whether ``record`` should replace ``existing`` for the same identity."""
    existing_source = str(existing.get("source") or "")
    record_source = str(record.get("source") or "")
    existing_rank = source_rank(existing_source, priority)
    record_rank = source_rank(record_source, priority)
    if record_rank != existing_rank:
        return record_rank < existing_rank

    # Same source: the newer *revision* wins, keyed on upstream time.
    existing_rev = _revision_time(existing)
    record_rev = _revision_time(record)
    if existing_rev and record_rev and existing_rev != record_rev:
        return record_rev > existing_rev

    # No usable revision time on either side: do NOT use body length (a shorter
    # body can be the newer revision — Astra rejects that heuristic). Fall back
    # to source priority so the choice is deterministic and explainable rather
    # than dependent on input order.
    existing_rank = source_rank(existing_source, priority)
    record_rank = source_rank(record_source, priority)
    return record_rank < existing_rank


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


def filter_news(records: list[dict[str, Any]], language: Optional[str] = None,
                tag: Optional[str] = None, body: Optional[bool] = None) -> list[dict[str, Any]]:
    """Structured filters over news records (language / information_tag / body)."""
    out = records
    if language:
        out = [r for r in out if r.get("language") == language]
    if tag:
        out = [r for r in out if r.get("information_tag") == tag]
    if body is not None:
        out = [r for r in out if bool(r.get("body_available")) is body]
    return out

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
    regions: Iterable[str] = ("jp", "en", "tc", "kr", "cn"),
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
    # Purge/build/merge/write is a read-modify-write over the shared news
    # files, so it holds the store writer lease.  The network fetches above run
    # *outside* the lease: they are slow and touch nothing shared, and holding
    # the lease across them would block other writers for no benefit.
    from sekaisync.fetcher import store_writer_lock

    with store_writer_lock(store_root):
        # Also purge any previously imported Sekai Viewer site announcements on disk.
        stored = [
            record
            for record in load_news(store_root)
            if not _is_website_announcement(record)
        ]
        # Complete relative URLs on legacy stored entries (fetch functions now
        # emit absolute URLs; older snapshots predate the web-domain mapping).
        for record in stored:
            url = str(record.get("url") or "")
            if url and not url.startswith(("http://", "https://", "weixin://")):
                language = str(record.get("language") or "")
                region = {"ja": "jp", "en": "en", "ko": "kr", "zh_hant": "tc", "zh_hans": "cn"}.get(language)
                web_base = _NEWS_WEB_BASES.get(region)
                if web_base:
                    record["url"] = f"{web_base}/{url.lstrip('/')}"
        records.extend(stored)
        merged = merge_news(records, source_priority=priority)
        save_news(merged, store_root)
        summary = news_summary(store_root)
    return {
        "fetched": len(records),
        "merged": len(merged),
        "summary": summary,
    }
