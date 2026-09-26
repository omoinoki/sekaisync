from __future__ import annotations

import hashlib
import json
import os
import uuid
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sekaisync.config import REGIONS, MoesekaiSettings, SiteSettings, ViewerSettings, require_endpoint
from sekaisync.layout import news_dir
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


# Deliberately not a delimiter: every identity component is free text, so a
# separator string could make ("a|b", "c") and ("a", "b|c") produce the same
# key and merge two unrelated announcements. The identity is a tuple.
def news_identity(item: dict[str, Any]) -> tuple[str, str, str]:
    """Stable identity for a news item: (namespace, upstream id, language).

    Astra P17/D17: identity is "upstream namespace / official source id or
    canonical URL / language". The title is a display field only — deriving
    identity from it merged genuinely distinct announcements that happened to
    share a heading, silently dropping one. That is the record-loss bug this
    replaces.

    **Namespace.** Merely comparing the ``source_type`` backend *name* is
    naming, not evidence: two deployments can serve different upstreams behind
    the same open-source backend class. A namespace is only shared when a
    record explicitly declares it via ``upstream_namespace``, which carries
    a source of truth such as an
    official upstream name or URL. Astra requires "无证据不跨站合并", so
    records without evidence never merge across instances, and id 1 from
    Moesekai and id 1 from Sekai Viewer are different announcements.
    """
    source = normalize_source_id(str(item.get("source") or ""))
    namespace = str(item.get("upstream_namespace") or "").strip()
    if not namespace:
        # Canonical/legacy aliases denote the same built-in instance, not all
        # deployments of that backend. Keep the historical built-in namespace.
        namespace = backend_of(source) or source
    value = item.get("source_id")
    upstream = str(value if value is not None else "").strip()
    language = _normalize_language(item.get("language"))

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
    return json.dumps(news_identity(item), ensure_ascii=False, separators=(",", ":"))


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
    fetched_at = datetime.now(timezone.utc).isoformat()
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
        # The upstream list omits the domain on most entries: ``path`` is
        # relative to the instance's announcement pages. Complete it here —
        # storing the bare path leaked into the published generations as
        # unclickable, unfetchable records (97 ja entries as of 2026-09-26).
        # A scheme-qualified value (https://, weixin://, alipays:// deep links)
        # is already absolute and passes through untouched.
        page_url = path
        if path and "://" not in path:
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
                "url": page_url,
                "start_at": _iso(start),
                "end_at": _iso(end),
                "published_at": _iso(start),
                "fetched_at": fetched_at,
                "source_updated_at": _iso(_timestamp_ms(item.get("updatedAt"))),
                "source_version": item.get("version"),
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
    fetched_at = datetime.now(timezone.utc).isoformat()
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
        if path and "://" not in path:
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
                "fetched_at": fetched_at,
                "source_updated_at": _iso(_timestamp_ms(item.get("updatedAt"))),
                "source_version": item.get("version"),
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


def _has_fetched_body(record: dict[str, Any]) -> bool:
    """Whether this record carries an article body (not just metadata)."""
    return bool(record.get("body_available")) and bool(str(record.get("text") or "").strip())


def _strictly_newer_revision(new: dict[str, Any], old: dict[str, Any]) -> bool:
    """Whether ``new`` is a strictly newer upstream revision than ``old``.

    Compares the same evidence :func:`_prefer_record` does — source version,
    then upstream update instant. Fetch instants are deliberately excluded:
    they advance on every metadata refresh and say nothing about the content.
    """
    for old_v, new_v in (
        (_source_version(old), _source_version(new)),
        (_revision_time(old), _revision_time(new)),
    ):
        if old_v is not None and new_v is not None and new_v != old_v:
            return new_v > old_v
    return False


def _with_preserved_body(
    winner: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep a fetched article body across metadata-only refreshes.

    A news sync re-fetches metadata for every announcement; those fresh records
    carry no body and win the merge on fetch recency, so without this step one
    metadata sync silently discarded every body ever backfilled (the real store
    went 2,093 bodies -> 55). A body belongs to the announcement's page, not to
    the refresh that fetched it, so when the merge winner has none, the body is
    carried over from a same-identity record — unless the upstream revision
    moved past it (a re-published announcement gets fresh text on the next
    ``--with-bodies`` run instead of silently serving the old one).
    """
    if _has_fetched_body(winner):
        return winner
    winner_url = str(winner.get("url") or "")
    for candidate in candidates:
        if candidate is winner or not _has_fetched_body(candidate):
            continue
        # The body belongs to a concrete page: if the indexed address moved on,
        # the cached text describes a different page and must not be reused.
        if winner_url and str(candidate.get("url") or "") != winner_url:
            continue
        if _strictly_newer_revision(winner, candidate):
            continue
        preserved = dict(winner)
        for field in ("text", "body_available", "body_official_url"):
            if field in candidate:
                preserved[field] = candidate[field]
        return preserved
    return winner


def merge_news(
    records: Iterable[dict[str, Any]],
    source_priority: Iterable[str] = DEFAULT_SOURCE_PRIORITY,
) -> list[dict[str, Any]]:
    """Merge news by stable upstream identity, newest revision first.

    Identity comes from :func:`news_identity` (namespace / upstream id /
    language), so two announcements that share a title but have different
    upstream ids are both kept (Astra D17: "同标题不同 ID 均保留").

    A fetched article body survives the merge: metadata refreshes arrive
    body-less and win on fetch recency, but the body belongs to the
    announcement's page, so it is carried into the winner unless the upstream
    revision moved past it (see :func:`_with_preserved_body`).
    """
    priority = tuple(normalize_source_id(item) for item in source_priority)
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for record in records:
        if _is_website_announcement(record):
            continue
        grouped.setdefault(news_identity(record), []).append(dict(record))
    merged: list[dict[str, Any]] = []
    for candidates in grouped.values():
        winner = candidates[0]
        for candidate in candidates[1:]:
            if _prefer_record(winner, candidate, priority):
                winner = candidate
        merged.append(_with_preserved_body(winner, candidates))
    return sorted(
        merged,
        key=lambda item: (item.get("language", ""), item.get("start_at") or ""),
    )


def _parse_instant(value: Any) -> Optional[float]:
    """Parse a revision-time field into a comparable instant.

    Astra P17: the old code compared these strings directly, so
    ``2026-1-10 > 2025-12-31`` as text, ``+09:00`` offsets compared against
    their local wall clock, epoch-milli strings beat ISO strings, and a
    ``None`` silently masked a real time. Everything is normalized to UTC
    instants here; unparseable values return ``None`` so the caller can fall
    back instead of guessing.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):
            return None
        if abs(number) >= 1_000_000_000_000:  # epoch milliseconds
            number /= 1000.0
        return number
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"[+-]?\d+", text):
        number = int(text)
        if abs(number) >= 1_000_000_000_000:  # epoch milliseconds
            number /= 1000.0
        return float(number)
    candidate = text.replace("Z", "+00:00").replace("z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _revision_time(record: dict[str, Any]) -> Optional[float]:
    """Upstream update instant, never publication/start (event attendance) time."""
    for field in ("source_updated_at", "updated_at"):
        instant = _parse_instant(record.get(field))
        if instant is not None:
            return instant
    return None


def _source_version(record: dict[str, Any]) -> Optional[int]:
    # Opaque ETags/semantic versions are not ordered. Only an explicit integer
    # counter has a portable ordering contract.
    value = str(record.get("source_version", "")).strip()
    return int(value) if re.fullmatch(r"\d+", value) else None


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

    # Compare like-for-like upstream evidence, then fetch instants. Publication
    # and attendance dates never establish revision recency.
    for old, new in (
        (_source_version(existing), _source_version(record)),
        (_revision_time(existing), _revision_time(record)),
        (_parse_instant(existing.get("fetched_at")), _parse_instant(record.get("fetched_at"))),
    ):
        if old is not None and new is not None and old != new:
            return new > old
    old_fetch = _parse_instant(existing.get("fetched_at"))
    new_fetch = _parse_instant(record.get("fetched_at"))
    if (old_fetch is None) != (new_fetch is None):
        return new_fetch is not None

    # No usable revision time on either side: do NOT use body length (a shorter
    # body can be the newer revision — Astra rejects that heuristic). Fall back
    # to source priority so the choice is deterministic and explainable rather
    # than dependent on input order.
    existing_rank = source_rank(existing_source, priority)
    record_rank = source_rank(record_source, priority)
    return record_rank < existing_rank


# ── immutable news generations (Astra P13/P17) ─────────────────────
#
# News used to be written in place under kb/news/<language>.json, so a crash
# mid-write left a truncated file and two readers at different moments could
# see different languages — a publication, not a single event. Now each sync
# publishes an immutable generation under kb/news/generations/<id>/ with a
# per-file SHA-256 manifest, and the pointer lives in the SQL `meta` table,
# committed in the same transaction as the store revision. Readers resolve the
# pointer once and either read the complete old generation or the complete new
# one, never a mix. Legacy in-place files are imported on first read and then
# ignored, so a stale crash-left file can never leak back into the store.

_NEWS_ACTIVE_GENERATION_KEY = "active_news_generation"


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _generation_root(store_root: Path) -> Path:
    return news_dir(store_root) / "generations"


def active_news_generation(store_root: Path, conn: Any = None) -> dict[str, Any]:
    """The committed news publication: generation id, dir, manifest, revision.

    Returns ``{}`` when the store has never published a generation. The pointer
    is read from SQL, so a request that pinned its connection with
    :func:`sekaisync.dbstore.read_connection` stays on the generation that was
    active when its snapshot started even if a concurrent sync publishes.
    """
    from sekaisync import dbstore

    def _read(active: Any) -> dict[str, Any]:
        if not active.execute("SELECT 1 FROM sqlite_master WHERE name='meta'").fetchone():
            return {}
        raw = dbstore._meta_get(active, _NEWS_ACTIVE_GENERATION_KEY)
        if raw is None:
            return {}
        data = json.loads(raw)
        if (not isinstance(data, dict)
                or not re.fullmatch(r"[a-f0-9]{32}", str(data.get("generation", "")))
                or not re.fullmatch(r"[a-f0-9]{64}", str(data.get("manifest_sha256", "")))):
            raise ValueError("invalid active news generation pointer")
        return data

    if conn is not None:
        return _read(conn)
    bound = dbstore._bound_connection(store_root)
    if bound is not None:
        return _read(bound)
    if not dbstore.db_file(store_root).exists():
        return {}
    state = dbstore.inspect_schema(store_root)
    if state.status == dbstore.SCHEMA_ABSENT:
        return {}
    if state.status != dbstore.SCHEMA_CURRENT:
        raise dbstore._schema_error(state)
    with dbstore.connect(store_root) as owned:
        return _read(owned)


def _generation_records(store_root: Path, pointer: dict[str, Any]) -> list[dict[str, Any]]:
    generation = pointer["generation"]
    root = _generation_root(store_root) / generation
    try:
        raw = (root / "manifest.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() != pointer["manifest_sha256"]:
            raise ValueError("news manifest does not match SQL pointer")
        manifest = json.loads(raw)
        if (not isinstance(manifest, dict) or manifest.get("generation") != generation
                or manifest.get("version") != 1 or manifest.get("domain") != "news"
                or not isinstance(manifest.get("files"), dict)):
            raise ValueError("invalid news manifest")
        records: list[dict[str, Any]] = []
        for name, info in sorted(manifest["files"].items()):
            if not re.fullmatch(r"\d+\.json", name) or not isinstance(info, dict):
                raise ValueError("invalid news manifest file")
            raw = (root / name).read_bytes()
            if len(raw) != info.get("bytes") or hashlib.sha256(raw).hexdigest() != info.get("sha256"):
                raise ValueError("news file does not match manifest")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or not isinstance(payload.get("news"), list):
                raise ValueError("invalid news payload")
            records.extend(item for item in payload["news"]
                           if isinstance(item, dict) and not _is_website_announcement(item))
        return records
    except (OSError, KeyError, TypeError) as exc:
        raise ValueError(f"unreadable news generation {generation!r}") from exc


def save_news(
    records: Iterable[dict[str, Any]],
    store_root: Path,
    *,
    languages: Optional[Iterable[str]] = None,
) -> Path:
    """Publish records as one immutable news generation.

    Astra P17: this is a *snapshot replace*, not an append — languages absent
    from ``records`` are removed (an empty list therefore clears the store,
    mirroring raw/ replace semantics). ``languages`` narrows that replacement
    to an explicit set of language tags: only those files are replaced and the
    other languages are preserved. Passing ``languages=()`` is a no-op, and
    passing a language that ``records`` does not contain publishes an empty
    file for it (a deliberate, scoped clear).
    """
    from sekaisync import dbstore
    from sekaisync.fetcher import store_writer_lock

    store_root = Path(store_root)
    dbstore.require_unbound_writer(store_root)
    records = [dict(record) for record in records]
    explicit = None if languages is None else {_normalize_language(lang) for lang in languages}
    if explicit is not None and "" in explicit:
        raise ValueError("languages must not contain empty entries")
    for record in records:
        language = _normalize_language(record.get("language")) or "other"
        if explicit is not None and language not in explicit:
            raise ValueError("record language is outside replacement scope")
    if explicit == set():
        return news_dir(store_root)
    with store_writer_lock(store_root):
        if explicit is not None:
            records.extend(record for record in load_news(store_root)
                           if (_normalize_language(record.get("language")) or "other") not in explicit)
        return _publish_news(records, store_root)


def _write_generation_json(path: Path, data: Any) -> None:
    with path.open("xb") as handle:
        handle.write(json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _publish_news(records: list[dict[str, Any]], store_root: Path) -> Path:
    """Caller owns the writer lease; immutable files precede the SQL commit."""
    from sekaisync.dbstore import _ensure_initialized, bump_revision, connect

    _ensure_initialized(store_root)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        language = _normalize_language(record.get("language")) or "other"
        if not _is_website_announcement(record):
            grouped.setdefault(language, []).append(record)

    timestamp = datetime.now(timezone.utc).isoformat()
    generation = uuid.uuid4().hex
    root = _generation_root(store_root) / generation
    root.mkdir(parents=True, exist_ok=False)
    files: dict[str, dict[str, Any]] = {}
    for index, (language, items) in enumerate(sorted(grouped.items())):
        # Language labels are data, never path components.
        name = f"{index:04d}.json"
        path = root / name
        data = {
            "version": 1,
            "language": language,
            "updated_at": timestamp,
            "news": items,
        }
        _write_generation_json(path, data)
        files[name] = {"sha256": _sha256_file(path), "bytes": path.stat().st_size}
    manifest = {
        "layout": "v2",
        "version": 1,
        "domain": "news",
        "generation": generation,
        "published_at": timestamp,
        "files": files,
    }
    _write_generation_json(root / "manifest.json", manifest)
    manifest_digest = _sha256_file(root / "manifest.json")

    from sekaisync.dbstore import _meta_set

    # Write the pointer inside one transaction with the revision bump, so the
    # flip is atomic: a reader sees the complete old generation or the
    # complete new one, never a partial publish (Astra P13/P17).
    try:
        with connect(store_root) as conn:
            revision = bump_revision(conn)
            _meta_set(
                conn,
                _NEWS_ACTIVE_GENERATION_KEY,
                json.dumps(
                    {
                        "generation": generation,
                        "published_at": timestamp,
                        "manifest_sha256": manifest_digest,
                        "revision": revision,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            )
            conn.commit()
    except BaseException:
        # The pointer never moved, so the previous generation stays complete
        # and active; remove only this publish's own files.
        for path in root.rglob("*"):
            if path.is_file():
                try:
                    path.unlink()
                except OSError:
                    pass
        try:
            root.rmdir()
            _generation_root(store_root).rmdir()
        except OSError:
            pass
        raise
    return news_dir(store_root)


def _load_legacy_news(store_root: Path) -> list[dict[str, Any]]:
    """Read-only compatibility until the first explicit publication."""
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


def load_news(store_root: Path) -> list[dict[str, Any]]:
    """All news records, **newest first** by ``published_at``.

    The store's physical order is generation-file / legacy-insertion order —
    effectively oldest first, so every ``[:limit]`` consumer (the HTTP/MCP
    ``news`` tool, ``news list``) showed a window of 2020-era records no
    matter how fresh the store was. Sorting here fixes all consumers at once;
    records without a timestamp sort to the end, and equal timestamps keep
    load order (stable sort).
    """
    store_root = Path(store_root)
    pointer = active_news_generation(store_root)
    if not pointer:
        records = _load_legacy_news(store_root)
    else:
        records = _generation_records(store_root, pointer)
    return sorted(records, key=lambda r: str(r.get("published_at") or ""), reverse=True)


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


def news_summary(
    store_root: Path,
    records: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Counts by language and source.

    Pass ``records`` (e.g. the records already loaded for a filtered view) to
    avoid reading the store twice — one consistent snapshot in, one summary
    out (the duplicate-load the audit found in Core).
    """
    if records is None:
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
    from sekaisync import dbstore
    dbstore.require_unbound_writer(store_root)
    regions = tuple(regions)
    sites = tuple(sites) if sites is not None else None
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
    # generation pointer, so it holds the store writer lease.  The network
    # fetches above run *outside* the lease: they are slow and touch nothing
    # shared, and holding the lease across them would block other writers for
    # no benefit.
    from sekaisync.fetcher import store_writer_lock

    fetched_count = len(records)
    with store_writer_lock(store_root):
        stored = load_news(store_root)
        # Complete relative URLs on legacy stored entries (fetch functions now
        # emit absolute URLs; older snapshots predate the web-domain mapping).
        # The "://" check leaves every scheme-qualified value alone — app deep
        # links (weixin://, alipays://) are identifiers, not pages to prefix.
        for record in stored:
            url = str(record.get("url") or "")
            if url and "://" not in url:
                language = str(record.get("language") or "")
                region = {"ja": "jp", "en": "en", "ko": "kr", "zh_hant": "tc", "zh_hans": "cn"}.get(language)
                web_base = _NEWS_WEB_BASES.get(region)
                if web_base:
                    record["url"] = f"{web_base}/{url.lstrip('/')}"
        merged = merge_news(records + stored, source_priority=priority)
        _publish_news(merged, store_root)
        summary = news_summary(store_root, records=merged)
    return {
        "fetched": fetched_count,
        "merged": len(merged),
        "summary": summary,
    }
