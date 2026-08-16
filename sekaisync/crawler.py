from __future__ import annotations

import contextlib
import contextvars
import hashlib
import html.parser
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable, Optional
from urllib.parse import urlparse

from sekaisync.config import (
    REGIONS,
    MoesekaiSettings,
    ViewerSettings,
    require_endpoint,
)
from sekaisync.layout import web_category_dir, web_consent_path, web_pages_path
from sekaisync.models import WebPage
from sekaisync.sources import (
    BACKEND_MOESEKAI,
    BACKEND_SEKAI_VIEWER,
    SOURCE_MS,
    SOURCE_SV,
    auxiliary_source_for_instance,
)
from sekaisync.webindex import (
    WEB_CATEGORY_FILES,
    load_existing_page_ids,
    load_existing_page_map,
    save_web_pages,
    sha256_hex,
    text_matches_language,
)


_NETWORK_ERRORS = (
    urllib.error.URLError,
    http.client.HTTPException,
    TimeoutError,
    OSError,
    ValueError,
    json.JSONDecodeError,
)


USER_AGENT = "SekaiSync/0.2 (+local knowledge sync; TOS-consent required)"

# Per-call crawl context. Every public crawl entry point sets these contextvars
# before any per-kind helper runs, so concurrent or interleaved crawls of
# different instances never share endpoint/page-id state. ``apply_source_settings``
# only mutates the module defaults below, which serve as the fallback for code
# that reaches a helper without a context (CLI always wraps the call in a context).
_cv_ms_instance: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "altsource_ms_instance", default=SOURCE_MS
)
_cv_sv_instance: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "altsource_sv_instance", default=SOURCE_SV
)

_EMPTY_BUCKETS = {
    "jp": "sekai-jp-assets",
    "en": "sekai-en-assets",
    "tc": "sekai-tc-assets",
    "kr": "sekai-kr-assets",
    "cn": "sekai-cn-assets",
}

# Module defaults (empty = unconfigured). ``apply_source_settings`` mutates
# these for backward compatibility; crawl entry points instead push a fresh
# per-call context so distinct instances are isolated.
ALTSOURCE_MS_BASE = ""
ALTSOURCE_MS_SITEMAP = ""
ALTSOURCE_MS_STORY_DETAIL_BASE = ""
ALTSOURCE_MS_METADATA_BASES: tuple[str, ...] = ()
ALTSOURCE_MS_ASSET_BASES: tuple[str, ...] = ()
ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN = True
ALTSOURCE_MS_LOCALE_SERVERS: dict[str, str] = {}
ALTSOURCE_MS_LOCALE_LANGUAGES: dict[str, str] = {}
ALTSOURCE_MS_TRANSLATION_BASE = ""

ALTSOURCE_SV_I18N_BASE = ""
ALTSOURCE_SV_I18N_LANGUAGES = ("ja", "zh-CN", "zh-TW", "en", "ko")
ALTSOURCE_SV_I18N_NAMESPACES = (
    "area_name",
    "area_subname",
    "card_episode_title",
    "card_gacha_phrase",
    "card_prefix",
    "card_skill_name",
    "character_name",
    "character_profile",
    "comic_title",
    "event_name",
    "event_story_episode_title",
    "gacha_name",
    "honorGroup_name",
    "honor_name",
    "music_titles",
    "music_vocal",
    "stamp_name",
    "unit",
    "unit_profile",
    "unit_story_chapter_title",
    "unit_story_episode_title",
    "virtualLive_name",
)
ALTSOURCE_SV_MASTER_BASE = ""
ALTSOURCE_SV_ASSET_BASE = ""
ALTSOURCE_SV_ASSET_BUCKETS: dict[str, str] = dict(_EMPTY_BUCKETS)
ALTSOURCE_SV_TABLES = [
    "eventStories",
    "unitStories",
    "specialStories",
    "cardEpisodes",
    "characterProfiles",
    "unitProfiles",
    "cards",
    "events",
    "gameCharacters",
    "gameCharacterUnits",
    "musics",
    "musicVocals",
    "musicDifficulties",
    "gachas",
    "areas",
    "areaItems",
    "stamps",
    "missions",
    "items",
    "versions",
]


def _current_ms_instance() -> str:
    return _cv_ms_instance.get()


def _current_sv_instance() -> str:
    return _cv_sv_instance.get()


@contextlib.contextmanager
def _ms_instance_scope(instance: Optional[str]):
    token = _cv_ms_instance.set(instance or SOURCE_MS)
    try:
        yield
    finally:
        _cv_ms_instance.reset(token)


@contextlib.contextmanager
def _sv_instance_scope(instance: Optional[str]):
    token = _cv_sv_instance.set(instance or SOURCE_SV)
    try:
        yield
    finally:
        _cv_sv_instance.reset(token)


def _ms_aux() -> str:
    return auxiliary_source_for_instance(_current_ms_instance(), BACKEND_MOESEKAI)


def _sv_aux() -> str:
    return auxiliary_source_for_instance(_current_sv_instance(), BACKEND_SEKAI_VIEWER)


def apply_source_settings(
    moesekai: Optional[MoesekaiSettings] = None,
    viewer: Optional[ViewerSettings] = None,
) -> None:
    """Apply active site settings to the module defaults (backward compat)."""
    global ALTSOURCE_MS_BASE, ALTSOURCE_MS_SITEMAP, ALTSOURCE_MS_STORY_DETAIL_BASE
    global ALTSOURCE_MS_METADATA_BASES, ALTSOURCE_MS_ASSET_BASES, ALTSOURCE_MS_TRANSLATION_BASE
    global ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN
    global ALTSOURCE_MS_LOCALE_SERVERS, ALTSOURCE_MS_LOCALE_LANGUAGES
    global ALTSOURCE_SV_I18N_BASE, ALTSOURCE_SV_MASTER_BASE, ALTSOURCE_SV_ASSET_BASE
    global ALTSOURCE_SV_ASSET_BUCKETS
    if moesekai is not None:
        ALTSOURCE_MS_BASE = moesekai.site_base
        ALTSOURCE_MS_SITEMAP = moesekai.sitemap_url
        ALTSOURCE_MS_STORY_DETAIL_BASE = moesekai.story_detail_base
        ALTSOURCE_MS_METADATA_BASES = moesekai.metadata_bases
        ALTSOURCE_MS_ASSET_BASES = moesekai.asset_bases
        ALTSOURCE_MS_TRANSLATION_BASE = moesekai.translation_base
        ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN = moesekai.fallback_to_viewer_cdn
        ALTSOURCE_MS_LOCALE_SERVERS = {
            key: value for key, value in moesekai.locale_servers
        } or ALTSOURCE_MS_LOCALE_SERVERS
        ALTSOURCE_MS_LOCALE_LANGUAGES = {
            key: value for key, value in moesekai.locale_languages
        } or ALTSOURCE_MS_LOCALE_LANGUAGES
    if viewer is not None:
        ALTSOURCE_SV_I18N_BASE = viewer.i18n_base
        ALTSOURCE_SV_MASTER_BASE = viewer.master_base
        ALTSOURCE_SV_ASSET_BASE = viewer.asset_base
        ALTSOURCE_SV_ASSET_BUCKETS = {
            key: value for key, value in viewer.asset_buckets
        } or ALTSOURCE_SV_ASSET_BUCKETS

TOS_WARNING_TEMPLATE = """\
Project Sekai is owned by SEGA / Colorful Palette.
The data you are about to import comes from community websites ({sites})
and is intended for local research and reference only.

Before importing you must:
- comply with the Project Sekai terms of service;
- respect each website's robots.txt and content signals;
- keep text data local and avoid public redistribution;
- avoid downloading images, audio, Live2D, video or other binary assets through this crawler;
- not use the crawled content for model training when a website marks ai-train=no.
"""


def enabled_site_summary(profile: Iterable[Any] | None = None) -> str:
    """Human-readable ``id (name)`` list of enabled sites for consent text."""
    sites = () if profile is None else profile
    parts = [
        f"{site.id}: {site.name or site.id}"
        for site in sites
        if getattr(site, "enabled", True)
    ]
    return ", ".join(parts) if parts else "altsource_sv / altsource_ms"


def _sha1(text: str, length: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:length]


def _source_sha256(value: Any) -> str:
    if isinstance(value, dict):
        value = {key: item for key, item in value.items() if not key.startswith("__")}
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    elif not isinstance(value, str):
        value = str(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _page_known(page_id: str, known_ids: Optional[set[str]]) -> bool:
    return bool(known_ids and page_id in known_ids)


def fetch_http_text_with_headers(
    url: str,
    timeout: int = 30,
) -> tuple[str, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xml,application/json;q=0.9,*/*;q=0.8",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
        try:
            text = raw.decode(charset, errors="replace")
        except LookupError:
            text = raw.decode("utf-8", errors="replace")
        return text, {
            "last_modified": response.headers.get("Last-Modified", ""),
            "etag": response.headers.get("ETag", ""),
        }


def fetch_http_text(url: str, timeout: int = 30) -> str:
    return fetch_http_text_with_headers(url, timeout=timeout)[0]


def _fetch_http_with_retry(
    fetcher: Callable[[str], str],
    url: str,
    attempts: int = 3,
    base_delay: float = 0.3,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            return fetcher(url)
        except urllib.error.HTTPError:
            raise
        except _NETWORK_ERRORS as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(base_delay * (attempt + 1))
    assert last_error is not None
    raise last_error


def _fetch_http_with_retry_meta(
    fetcher: Callable[[str], str],
    url: str,
    attempts: int = 3,
    base_delay: float = 0.3,
) -> tuple[str, dict[str, str]]:
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            result = fetcher(url)
            if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str):
                text, meta = result
                return text, dict(meta)
            return result, {}
        except urllib.error.HTTPError:
            raise
        except _NETWORK_ERRORS as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(base_delay * (attempt + 1))
    assert last_error is not None
    raise last_error


def _cache_bust_url(url: str) -> str:
    marker = "&" if "?" in url else "?"
    return f"{url}{marker}v={int(time.time() * 1000)}"


def require_tos_consent(accept_tos: bool) -> bool:
    print(TOS_WARNING_TEMPLATE.format(sites=enabled_site_summary()))
    if accept_tos:
        return True
    if not sys.stdin.isatty():
        raise ValueError(
            "Web import requires explicit TOS consent. Re-run with --accept-tos "
            "after confirming you comply with the Project Sekai terms of service."
        )
    answer = input("Type yes to confirm you accept the Project Sekai TOS and website rules: ").strip().lower()
    if answer not in {"yes", "y"}:
        raise ValueError("TOS consent not provided; import cancelled.")
    return True


def write_consent(store_root: Path, source: str, accepted: bool = True) -> Path:
    path = web_consent_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            existing = raw
        elif isinstance(raw, bool):
            existing["legacy"] = {
                "accepted": raw,
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            }
    existing[source] = {
        "accepted": accepted,
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "tos_version": 1,
    }
    path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def parse_sitemap_urls(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    return [
        loc.text.strip()
        for loc in root.iter()
        if loc.tag.endswith("loc") and loc.text and loc.text.strip()
    ]


def altsource_ms_language_from_url(url: str) -> str:
    parts = urlparse(url).path.strip("/").split("/")
    if not parts:
        return ""
    return ALTSOURCE_MS_LOCALE_LANGUAGES.get(parts[0].lower(), "")


def altsource_sv_master_json_url(region: str, table: str) -> str:
    repo = REGIONS[region].repo_slug
    repo_name = repo.rsplit("/", 1)[-1] if repo else region
    return f"{ALTSOURCE_SV_MASTER_BASE}/{repo_name}/{table}.json"


def altsource_sv_asset_url(region: str, path: str) -> str:
    bucket = ALTSOURCE_SV_ASSET_BUCKETS.get(region, f"sekai-{region}-assets")
    return f"{ALTSOURCE_SV_ASSET_BASE}/{bucket}/{path.lstrip('/')}"


def _expected_scenario_id(path: str) -> str:
    return Path(path).stem


_SCENARIO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_./-]+$")


def _scenario_mismatch_reason(data: dict[str, Any], expected_id: str) -> str:
    parts = []
    scenario_id = data.get("ScenarioId")
    asset_name = data.get("m_Name")
    raw_scenario_id = str(scenario_id or "")
    if (
        scenario_id
        and _SCENARIO_ID_PATTERN.fullmatch(raw_scenario_id)
        and raw_scenario_id != expected_id
    ):
        parts.append(f"ScenarioId {scenario_id} != expected {expected_id}")
    if (
        asset_name
        and _SCENARIO_ID_PATTERN.fullmatch(str(asset_name))
        and str(asset_name) != expected_id
    ):
        parts.append(f"m_Name {asset_name} != expected {expected_id}")
    return "; ".join(parts)


def fetch_altsource_sv_master(
    region: str,
    table: str,
    fetcher: Callable[[str], str],
) -> list[dict[str, Any]]:
    table = table.removesuffix(".json")
    try:
        data = json.loads(fetcher(altsource_sv_master_json_url(region, table)))
    except _NETWORK_ERRORS:
        return []
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def fetch_altsource_sv_asset(
    region: str,
    paths: Iterable[str],
    fetcher: Callable[[str], str],
    as_json: bool = True,
) -> tuple[str, Any]:
    for path in paths:
        url = _cache_bust_url(altsource_sv_asset_url(region, path))
        try:
            text, meta = _fetch_http_with_retry_meta(fetcher, url)
            if as_json:
                data = json.loads(text)
                if isinstance(data, dict):
                    data["__sourceMeta"] = meta
                    data["__scenarioIdMismatch"] = _scenario_mismatch_reason(
                        data,
                        _expected_scenario_id(path),
                    )
                    data["__assetMismatch"] = ""
                    return url, data
            elif text.strip():
                return url, text
        except _NETWORK_ERRORS:
            continue
    return "", None


def classify_altsource_ms_url(url: str) -> str:
    path = urlparse(url).path.lower()
    for segment, kind in (
        ("/cards/", "card"),
        ("/music/", "music"),
        ("/events/", "event"),
        ("/story/", "story"),
        ("/character/", "character"),
        ("/gacha/", "gacha"),
        ("/information/", "information"),
        ("/guides/", "guide"),
        ("/comic/", "comic"),
        ("/costumes/", "costume"),
    ):
        if segment in path:
            return kind
    return "page"


_ALTSOURCE_MS_LISTING_SEGMENTS = {
    "",
    "area",
    "card",
    "cards",
    "character",
    "characters",
    "comic",
    "costume",
    "costumes",
    "event",
    "events",
    "gacha",
    "guide",
    "guides",
    "honor",
    "information",
    "meta",
    "music",
    "self",
    "soundtrack",
    "special",
    "sticker",
    "story",
    "unit",
}


def _altsource_ms_is_detail_url(url: str) -> bool:
    path = [part for part in urlparse(url).path.strip("/").split("/") if part]
    if len(path) < 3:
        return False
    return path[-1].lower() not in _ALTSOURCE_MS_LISTING_SEGMENTS


def altsource_ms_event_id_from_url(url: str) -> Optional[int]:
    path = [part for part in urlparse(url).path.strip("/").split("/") if part]
    for index, part in enumerate(path):
        if part.lower() != "event":
            continue
        if index + 1 >= len(path):
            return None
        try:
            return int(path[index + 1])
        except ValueError:
            return None
    return None


def _altsource_ms_url_sort_key(url: str) -> tuple[int, int]:
    kind = classify_altsource_ms_url(url)
    detail = _altsource_ms_is_detail_url(url)
    if kind == "story":
        kind_rank = 0
    elif kind in {"card", "character", "music", "event", "gacha", "guide", "comic", "costume", "information"}:
        kind_rank = 1
    elif kind == "page":
        kind_rank = 2
    else:
        kind_rank = 3
    return (0 if detail else 1, kind_rank)


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title = ""
        self.description = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): (value or "") for key, value in attrs}
        if tag == "title":
            self.title = values.get("", "")
            return
        if tag != "meta":
            return
        name = values.get("name", "").lower()
        prop = values.get("property", "").lower()
        if name == "description" or prop == "og:description":
            self.description = values.get("content", "")
        if not self.title and prop == "og:title":
            self.title = values.get("content", "")

    def handle_data(self, data: str) -> None:
        if self.title == "":
            self.title = " ".join(data.split())


class _VisibleTextExtractor(HTMLParser):
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "nav", "footer", "header"}
    _BLOCK_TAGS = {"h1", "h2", "h3", "h4", "p", "li", "tr", "td", "div", "section", "article"}

    def __init__(self) -> None:
        super().__init__()
        self.skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self.skip_depth += 1
        if not self.skip_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        value = " ".join(data.split())
        if value:
            self.parts.append(value)


def extract_page_meta(html_text: str) -> tuple[str, str]:
    parser = _MetaParser()
    parser.feed(html_text)
    title = parser.title.strip()
    if " | Moesekai" in title:
        title = title.split(" | Moesekai", 1)[0].strip()
    return title, parser.description.strip()


def extract_visible_text(html_text: str) -> str:
    parser = _VisibleTextExtractor()
    parser.feed(html_text)
    lines = [line.strip() for line in "".join(parser.parts).splitlines() if line.strip()]
    return "\n".join(lines).strip()


def extract_altsource_ms_page(html_text: str, url: str) -> WebPage:
    title, description = extract_page_meta(html_text)
    visible = extract_visible_text(html_text)
    text = "\n\n".join(part for part in (description, visible) if part).strip()
    kind = classify_altsource_ms_url(url)
    now = datetime.now(timezone.utc).isoformat()
    return WebPage(
        id=altsource_ms_page_id(_locale_from_altsource_ms_url(url), kind, _sha1(url)),
        source=_current_ms_instance(), source_type=BACKEND_MOESEKAI,
        url=url,
        title=title or url,
        language=altsource_ms_language_from_url(url),
        kind=kind,
        text=text,
        crawled_at=now,
        hash=_sha1(text or url, 16),
        tos_accepted=True,
        derived=kind in {"story", "story_detail"},
        trust="C" if kind in {"story", "story_detail"} else "B",
        source_hash=_source_sha256(html_text),
    )


def altsource_ms_story_detail_page(data: dict[str, Any], event_id: int, locale: str = "zh-cn") -> WebPage:
    title = str(
        data.get("title_cn")
        or data.get("title_jp")
        or f"Event {event_id}"
    )
    parts = [f"活动剧情：{title}"]
    if data.get("title_jp"):
        parts.append(f"日文标题：{data['title_jp']}")
    if data.get("outline_cn") or data.get("outline_jp"):
        parts.append(f"梗概：{data.get('outline_cn') or data.get('outline_jp')}")
    if data.get("summary_cn"):
        parts.append(f"剧情总览：{data['summary_cn']}")
    for chapter in data.get("chapters") or []:
        if not isinstance(chapter, dict):
            continue
        chapter_title = chapter.get("title_cn") or chapter.get("title_jp")
        chapter_no = chapter.get("chapter_no", "")
        parts.append(f"第{chapter_no}话：{chapter_title}")
        if chapter.get("summary_cn"):
            parts.append(str(chapter["summary_cn"]))
    text = "\n\n".join(part for part in parts if part).strip()
    now = datetime.now(timezone.utc).isoformat()
    return WebPage(
        id=altsource_ms_page_id(locale, "story_detail", event_id),
        source=_current_ms_instance(), source_type=BACKEND_MOESEKAI,
        url=f"{ALTSOURCE_MS_STORY_DETAIL_BASE}/event_{event_id:03d}.json",
        title=title,
        language="zh_hans",
        kind="story",
        text=text,
        crawled_at=now,
        hash=_sha1(text or title, 16),
        tos_accepted=True,
        derived=True,
        trust="C",
        source_hash=_source_sha256(data),
    )


def record_title(record: dict[str, Any]) -> str:
    for key in ("title", "name", "songName", "eventName", "cardName", "assetName"):
        value = record.get(key)
        if value:
            return str(value)
    first = record.get("firstName") or record.get("givenName")
    last = record.get("lastName") or record.get("familyName")
    if first and last:
        return f"{last}{first}"
    return str(record.get("id", ""))


def altsource_sv_record_page(record: dict[str, Any], table: str, region: str) -> WebPage:
    url = altsource_sv_master_json_url(region, table)
    text = json.dumps(record, ensure_ascii=False, indent=2)
    record_id = record.get("id", record.get("seq", _sha1(text)))
    now = datetime.now(timezone.utc).isoformat()
    return WebPage(
        id=f"web:{_current_sv_instance()}:{region}:{table}:{record_id}",
        source=_current_sv_instance(), source_type=BACKEND_SEKAI_VIEWER,
        url=url,
        title=record_title(record),
        language=REGIONS[region].language,
        kind=table,
        text=text,
        crawled_at=now,
        hash=_sha1(text, 16),
        tos_accepted=True,
        trust="B",
        source_hash=_source_sha256(record),
    )


def _altsource_ms_detail_sitemaps(sitemap_xml: str, locales: Iterable[str]) -> list[str]:
    locale_set = set(locales)
    urls = parse_sitemap_urls(sitemap_xml)
    detail_sitemaps = []
    main_sitemaps = []
    for url in urls:
        path = urlparse(url).path.lower()
        if "sitemap-main.xml" in path:
            main_sitemaps.append(url)
            continue
        match = re.search(r"sitemap-details/([^/]+)\.xml", path)
        if match and match.group(1).lower() in locale_set:
            detail_sitemaps.append(url)
    return detail_sitemaps or main_sitemaps


def crawl_altsource_ms_site(
    store_root: Path,
    locales: Iterable[str] = ("zh-cn",),
    limit: int = 0,
    accept_tos: bool = False,
    delay: float = 0.5,
    fetcher: Callable[[str], str] = fetch_http_text,
    tos_already_checked: bool = False,
    settings: Optional[MoesekaiSettings] = None,
    instance: Optional[str] = None,
) -> dict[str, Any]:
    with _ms_instance_scope(instance):
        return _crawl_altsource_ms_site_impl(
            store_root, locales=locales, limit=limit, accept_tos=accept_tos,
            delay=delay, fetcher=fetcher, tos_already_checked=tos_already_checked,
            settings=settings,
        )


def _crawl_altsource_ms_site_impl(
    store_root: Path,
    locales: Iterable[str],
    limit: int,
    accept_tos: bool,
    delay: float,
    fetcher: Callable[[str], str],
    tos_already_checked: bool,
    settings: Optional[MoesekaiSettings],
) -> dict[str, Any]:
    if settings is not None:
        apply_source_settings(moesekai=settings)
    if not tos_already_checked:
        require_tos_consent(accept_tos)
    require_endpoint(ALTSOURCE_MS_SITEMAP, "sitemap_url", _current_ms_instance())
    require_endpoint(ALTSOURCE_MS_BASE, "site_base", _current_ms_instance())
    require_endpoint(ALTSOURCE_MS_STORY_DETAIL_BASE, "story_detail_base", _current_ms_instance())
    write_consent(store_root, _current_ms_instance())
    sitemap_xml = fetcher(ALTSOURCE_MS_SITEMAP)
    candidate_urls: list[str] = []
    for sitemap_url in _altsource_ms_detail_sitemaps(sitemap_xml, locales):
        try:
            candidate_urls.extend(parse_sitemap_urls(fetcher(sitemap_url)))
        except _NETWORK_ERRORS:
            continue
    unique_urls = list(dict.fromkeys(candidate_urls))
    unique_urls.sort(key=_altsource_ms_url_sort_key)
    if limit > 0:
        unique_urls = unique_urls[:limit]

    pages: list[WebPage] = []
    for url in unique_urls:
        event_id = altsource_ms_event_id_from_url(url)
        if event_id is not None:
            try:
                detail = json.loads(
                    fetcher(f"{ALTSOURCE_MS_STORY_DETAIL_BASE}/event_{event_id:03d}.json")
                )
            except _NETWORK_ERRORS:
                detail = None
            if isinstance(detail, dict):
                pages.append(altsource_ms_story_detail_page(detail, event_id, _locale_from_altsource_ms_url(url)))
                if delay:
                    time.sleep(delay)
                continue
        try:
            html_text = fetcher(url)
        except _NETWORK_ERRORS:
            continue
        pages.append(extract_altsource_ms_page(html_text, url))
        if delay:
            time.sleep(delay)
    index_path = save_web_pages(store_root, _current_ms_instance(), pages)
    return {
        "source": _current_ms_instance(),
        "crawled_pages": len(pages),
        "index": str(index_path),
        "category_files": [str(web_category_dir(store_root, _current_ms_instance()) / filename) for filename, _ in WEB_CATEGORY_FILES],
    }


def altsource_ms_server_for_locale(locale: str) -> str:
    return ALTSOURCE_MS_LOCALE_SERVERS.get(locale.lower(), "cn")


def altsource_ms_language_for_locale(locale: str) -> str:
    return ALTSOURCE_MS_LOCALE_LANGUAGES.get(locale.lower(), "zh_hans")


def altsource_ms_canonical_url(locale: str, path: str) -> str:
    """Canonical web URL for a page, built from the configured ``site_base``.

    The story path shape is fixed by the open-source moesekai framework, but
    the host must follow the instance's configured ``site_base`` so moved
    domains and self-hosted mirrors link back to themselves.
    """
    base = require_endpoint(ALTSOURCE_MS_BASE, "site_base", _current_ms_instance()).rstrip("/")
    return f"{base}/{altsource_ms_locale_key(locale)}/{path.lstrip('/')}"


def altsource_ms_locale_key(locale: str) -> str:
    return (locale or "zh-cn").strip().lower()


def altsource_ms_translation_event_url(event_id: int) -> str:
    return f"{ALTSOURCE_MS_TRANSLATION_BASE}/eventStory/event_{int(event_id)}.json"


def _normalize_overlay_language(value: Any) -> str:
    code = str(value or "").strip().lower()
    return {
        "zh-cn": "zh_hans",
        "zh-hans": "zh_hans",
        "zh-tw": "zh_hant",
        "zh-hant": "zh_hant",
        "ja-jp": "ja",
        "ja": "ja",
        "en-us": "en",
        "en": "en",
        "ko-kr": "ko",
        "ko": "ko",
    }.get(code, code)


def parse_altsource_ms_overlay_pages(
    data: dict[str, Any],
    event_id: int,
    locale: str,
    url: str,
    source_hash: str = "",
    fetched_at: Optional[str] = None,
) -> list[WebPage]:
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    meta = data.get("meta") if isinstance(data, dict) else None
    meta = meta if isinstance(meta, dict) else {}
    translation_source = str(meta.get("source") or "").strip().lower()
    episodes = data.get("episodes") or {}
    locale_key = altsource_ms_locale_key(locale)
    target_language = altsource_ms_language_for_locale(locale)
    pages: list[WebPage] = []

    episode_items: Iterable[tuple[Any, Any]] = []
    if isinstance(episodes, dict):
        episode_items = episodes.items()
    elif isinstance(episodes, list):
        episode_items = enumerate(episodes, start=1)

    for episode_no_value, episode in episode_items:
        if not isinstance(episode, dict):
            continue
        try:
            episode_no = int(episode_no_value)
        except (TypeError, ValueError):
            continue
        talk_data = episode.get("talkData") or {}
        if not isinstance(talk_data, dict):
            continue
        original_lines: list[str] = []
        translated_lines: list[str] = []
        for original_text, translation in talk_data.items():
            original_text = str(original_text or "").strip()
            translation = str(translation or "").strip()
            if not original_text or not translation:
                continue
            original_lines.append(original_text)
            translated_lines.append(translation)
        if not original_lines or not translated_lines:
            continue
        title = str(episode.get("title") or "")
        scenario_id = str(episode.get("scenarioId") or "")
        trust = "B" if translation_source == "official_cn" else "C"
        common = {
            "source": _ms_aux(),
            "source_type": BACKEND_MOESEKAI,
            "url": url,
            "crawled_at": fetched_at,
            "tos_accepted": True,
            "source_hash": source_hash,
            "trust": trust,
            "auxiliary": True,
            "overlay": True,
            "translation_source": translation_source,
            "source_language": "ja",
            "event_id": event_id,
            "episode_no": episode_no,
            "kind": "event_story",
        }
        original_text = "\n".join(original_lines)
        translated_text = "\n".join(translated_lines)
        pages.append(
            WebPage(
                id=f"web:{_ms_aux()}:{locale_key}:event_story:{event_id}:{episode_no}:ja",
                title=title or f"event {event_id} episode {episode_no}",
                language="ja",
                text=original_text,
                hash=_sha1(original_text, 16),
                derived=False,
                text_hash=sha256_hex(original_text),
                original_text_hash=sha256_hex(original_text),
                **common,
            )
        )
        pages.append(
            WebPage(
                id=f"web:{_ms_aux()}:{locale_key}:event_story:{event_id}:{episode_no}:{target_language}",
                title=title or f"event {event_id} episode {episode_no}",
                language=target_language,
                text=translated_text,
                hash=_sha1(translated_text, 16),
                derived=False,
                text_hash=sha256_hex(translated_text),
                original_text_hash=sha256_hex(original_text),
                **common,
            )
        )
    return pages


def _prefix_known(prefix: str, known_ids: Optional[set[str]]) -> bool:
    if not known_ids:
        return False
    return any(item.startswith(prefix) for item in known_ids)


def _crawl_altsource_ms_translation_events(
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]],
    event_ids: Iterable[int],
) -> Optional[int]:
    locale_key = altsource_ms_locale_key(locale)
    for event_id in sorted(set(event_ids)):
        prefix = f"web:{_ms_aux()}:{locale_key}:event_story:{event_id}:"
        if _prefix_known(prefix, known_ids):
            continue
        url = altsource_ms_translation_event_url(event_id)
        try:
            raw = fetcher(_cache_bust_url(url))
        except _NETWORK_ERRORS:
            if delay:
                time.sleep(delay)
            continue
        if not raw.strip() or raw.strip().startswith("404 page not found"):
            if delay:
                time.sleep(delay)
            continue
        try:
            data = json.loads(raw)
        except ValueError:
            if delay:
                time.sleep(delay)
            continue
        parsed = parse_altsource_ms_overlay_pages(
            data,
            event_id,
            locale,
            url,
            source_hash=sha256_hex(raw),
        )
        for page in parsed:
            remaining = _take_page(pages, remaining, page)
            if remaining is not None and remaining <= 0:
                return remaining
        if delay:
            time.sleep(delay)
    return remaining


def parse_altsource_sv_i18n_pages(
    data: dict[str, Any],
    language_code: str,
    namespace: str,
    url: str,
    source_hash: str = "",
    fetched_at: Optional[str] = None,
) -> list[WebPage]:
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    target_language = _normalize_overlay_language(language_code)
    entries: list[str] = []
    for key, value in data.items():
        value = str(value or "").strip()
        if not value:
            continue
        entries.append(f"{str(key or '').strip()}\t{value}")
    if not entries:
        return []
    text = "\n".join(entries)
    return [
        WebPage(
            id=f"web:{_sv_aux()}:{target_language}:{namespace}",
            source=_sv_aux(), source_type=BACKEND_SEKAI_VIEWER,
            url=url,
            title=namespace,
            language=target_language,
            kind="title_overlay",
            text=text,
            crawled_at=fetched_at,
            hash=_sha1(text, 16),
            tos_accepted=True,
            derived=False,
            trust="C",
            source_hash=source_hash,
            text_hash=sha256_hex(text),
            auxiliary=True,
            overlay=True,
            translation_source="i18n",
            source_language="ja",
            namespace=namespace,
        )
    ]


def _crawl_altsource_sv_i18n(
    store_root: Path,
    fetcher: Callable[[str], str],
    delay: float,
    resume: bool = True,
) -> dict[str, Any]:
    pages: list[WebPage] = []
    known_ids = load_existing_page_ids(store_root, _sv_aux()) if resume else set()
    for language_code in ALTSOURCE_SV_I18N_LANGUAGES:
        target_language = _normalize_overlay_language(language_code)
        for namespace in ALTSOURCE_SV_I18N_NAMESPACES:
            page_id = f"web:{_sv_aux()}:{target_language}:{namespace}"
            if known_ids and page_id in known_ids:
                continue
            url = f"{ALTSOURCE_SV_I18N_BASE}/{language_code}/{namespace}.json"
            try:
                raw = fetcher(_cache_bust_url(url))
            except _NETWORK_ERRORS:
                if delay:
                    time.sleep(delay)
                continue
            if not raw.strip() or raw.strip().startswith("404 page not found"):
                if delay:
                    time.sleep(delay)
                continue
            try:
                data = json.loads(raw)
            except ValueError:
                if delay:
                    time.sleep(delay)
                continue
            if not isinstance(data, dict):
                if delay:
                    time.sleep(delay)
                continue
            pages.extend(
                parse_altsource_sv_i18n_pages(
                    data,
                    language_code,
                    namespace,
                    url,
                    source_hash=sha256_hex(raw),
                )
            )
            if delay:
                time.sleep(delay)
    index_path = None
    if pages:
        index_path = save_web_pages(store_root, _sv_aux(), pages)
    return {
        "source": _sv_aux(),
        "pages": len(pages),
        "index": str(index_path) if index_path else None,
    }


def altsource_ms_page_id(locale: str, kind: str, *parts: Any) -> str:
    rendered = [altsource_ms_locale_key(locale), kind]
    rendered.extend(str(part) for part in parts)
    return f"web:{_current_ms_instance()}:" + ":".join(rendered)


def _locale_from_altsource_ms_url(url: str) -> str:
    path = urlparse(url).path.strip("/").split("/")
    if path and path[0].lower() in ALTSOURCE_MS_LOCALE_SERVERS:
        return path[0].lower()
    return "zh-cn"


def fetch_altsource_ms_master(server: str, table: str, fetcher: Callable[[str], str]) -> list[dict[str, Any]]:
    for base in ALTSOURCE_MS_METADATA_BASES:
        try:
            data = json.loads(fetcher(f"{base}/{server}/master/{table}"))
        except _NETWORK_ERRORS:
            continue
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    return []


def fetch_altsource_ms_scenario(
    server: str,
    path: str,
    fetcher: Callable[[str], str],
    language: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    for base in ALTSOURCE_MS_ASSET_BASES:
        try:
            text, meta = _fetch_http_with_retry_meta(
                fetcher,
                _cache_bust_url(f"{base}/sekai-{server}-assets/{path}"),
            )
            data = json.loads(text)
        except _NETWORK_ERRORS:
            continue
        if isinstance(data, dict):
            extracted = scenario_json_to_text(data)
            if language and not text_matches_language(language, extracted):
                continue
            data["__sourceMeta"] = meta
            data["__scenarioIdMismatch"] = _scenario_mismatch_reason(
                data,
                _expected_scenario_id(path),
            )
            data["__assetMismatch"] = ""
            return data
    if not ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN:
        return None
    bucket = "tc" if server == "tw" else server
    alt_path = path[:-len(".json")] + ".asset" if path.endswith(".json") else path
    fallback_url = _cache_bust_url(f"{ALTSOURCE_SV_ASSET_BASE}/sekai-{bucket}-assets/{alt_path}")
    try:
        text, meta = _fetch_http_with_retry_meta(fetcher, fallback_url)
        data = json.loads(text)
    except _NETWORK_ERRORS:
        return None
    if isinstance(data, dict):
        extracted = scenario_json_to_text(data)
        if language and not text_matches_language(language, extracted):
            return None
        data["__sourceMeta"] = meta
        data["__scenarioIdMismatch"] = _scenario_mismatch_reason(
            data,
            _expected_scenario_id(alt_path),
        )
        data["__assetMismatch"] = ""
        return data
    return None


def scenario_json_to_text(data: dict[str, Any]) -> str:
    parts: list[str] = []
    talk_data = data.get("TalkData") or []
    if isinstance(talk_data, dict):
        talk_data = list(talk_data.values())
    for talk in talk_data:
        if not isinstance(talk, dict):
            continue
        body = str(talk.get("Body") or "").strip()
        if not body:
            continue
        speaker = str(talk.get("WindowDisplayName") or "").strip()
        parts.append(f"{speaker}：{body}" if speaker else body)
    for effect in data.get("SpecialEffectData") or []:
        if not isinstance(effect, dict):
            continue
        if effect.get("EffectType") != 24:
            continue
        text = effect.get("StringVal")
        if text:
            parts.append(str(text).strip())
    for detail in data.get("Details") or []:
        if not isinstance(detail, dict):
            continue
        for balloon in detail.get("Balloons") or []:
            if not isinstance(balloon, dict):
                continue
            message = str(balloon.get("Message") or "").strip()
            if message:
                parts.append(message)
    for talk in data.get("characterTalkEvents") or []:
        if not isinstance(talk, dict):
            continue
        body = str(talk.get("Serif") or "").strip()
        if body:
            parts.append(body)
    timeline = data.get("__timelineParse")
    if isinstance(timeline, dict):
        for event in timeline.get("events") or []:
            if not isinstance(event, dict) or event.get("type") != "talk":
                continue
            speaker = str(event.get("character") or "").strip()
            body = str(event.get("displayName") or event.get("serif") or "").strip()
            if body:
                parts.append(f"{speaker}：{body}" if speaker else body)
    return "\n".join(part for part in parts if part).strip()


def mysekai_lua_to_text(lua_content: str) -> str:
    parts: list[str] = []
    speaker = ""
    for raw_line in lua_content.splitlines():
        line = raw_line.strip()
        if line.startswith("label("):
            match = re.search(r'label\("(.+?)"\)', line)
            speaker = match.group(1) if match else ""
            continue
        if line.startswith("voice("):
            match = re.search(r'voice\("talk",\s*"([^"]+)",\s*Characters\.(\w+)\)', line)
            if match and not speaker:
                speaker = match.group(2)
            continue
        if line.startswith("text("):
            match = re.search(r'text\("(.*)"\)', line, re.DOTALL)
            if not match:
                continue
            body = match.group(1).replace("\\n", "\n").replace('\\"', '"')
            parts.append(f"{speaker}：{body}" if speaker else body)
    return "\n".join(part for part in parts if part).strip()


def _language_mismatch_detail(language: str, text: str) -> str:
    if not text_matches_language(language, text):
        return f"language_mismatch: expected {language}, text script mismatch"
    return ""


def altsource_sv_scenario_page(
    region: str,
    page_id: str,
    title: str,
    url: str,
    data: dict[str, Any],
    kind: str,
) -> Optional[WebPage]:
    text = scenario_json_to_text(data)
    if not text:
        return None
    now = datetime.now(timezone.utc).isoformat()
    meta = data.get("__sourceMeta") or {}
    language = REGIONS[region].language
    scenario_reason = str(data.get("__scenarioIdMismatch") or "")
    language_ok = text_matches_language(language, text)
    if language_ok:
        asset_mismatch = ""
        scenario_id_mismatch = scenario_reason
        content_language_mismatch = False
    else:
        asset_mismatch = _language_mismatch_detail(language, text)
        scenario_id_mismatch = scenario_reason
        content_language_mismatch = True
    return WebPage(
        id=page_id,
        source=_current_sv_instance(), source_type=BACKEND_SEKAI_VIEWER,
        url=url,
        title=title,
        language=language,
        kind=kind,
        text=text,
        crawled_at=now,
        hash=_sha1(text or title, 16),
        tos_accepted=True,
        trust="B",
        source_hash=_source_sha256(data),
        asset_mismatch=asset_mismatch,
        scenario_id_mismatch=scenario_id_mismatch,
        content_language_mismatch=content_language_mismatch,
        source_last_modified=str(meta.get("last_modified") or ""),
        source_etag=str(meta.get("etag") or ""),
    )


def altsource_ms_scenario_page(
    server: str,
    locale: str,
    page_id: str,
    title: str,
    url: str,
    scenario: dict[str, Any],
    kind: str,
) -> Optional[WebPage]:
    text = scenario_json_to_text(scenario)
    if not text:
        return None
    now = datetime.now(timezone.utc).isoformat()
    meta = scenario.get("__sourceMeta") or {}
    language = altsource_ms_language_for_locale(locale)
    scenario_reason = str(scenario.get("__scenarioIdMismatch") or "")
    language_ok = text_matches_language(language, text)
    if language_ok:
        asset_mismatch = ""
        scenario_id_mismatch = scenario_reason
        content_language_mismatch = False
    else:
        asset_mismatch = _language_mismatch_detail(language, text)
        scenario_id_mismatch = scenario_reason
        content_language_mismatch = True
    return WebPage(
        id=page_id,
        source=_current_ms_instance(), source_type=BACKEND_MOESEKAI,
        url=url,
        title=title,
        language=language,
        kind=kind,
        text=text,
        crawled_at=now,
        hash=_sha1(text or title, 16),
        tos_accepted=True,
        trust="B",
        source_hash=_source_sha256(scenario),
        asset_mismatch=asset_mismatch,
        scenario_id_mismatch=scenario_id_mismatch,
        content_language_mismatch=content_language_mismatch,
        source_last_modified=str(meta.get("last_modified") or ""),
        source_etag=str(meta.get("etag") or ""),
    )


def altsource_ms_record_page(
    record: dict[str, Any],
    table: str,
    server: str,
    locale: str,
    kind: Optional[str] = None,
) -> WebPage:
    text = json.dumps(record, ensure_ascii=False, indent=2)
    record_id = record.get("id", record.get("seq", _sha1(text)))
    now = datetime.now(timezone.utc).isoformat()
    return WebPage(
        id=altsource_ms_page_id(locale, table, record_id),
        source=_current_ms_instance(), source_type=BACKEND_MOESEKAI,
        url=f"{ALTSOURCE_MS_METADATA_BASES[0]}/{server}/master/{table}",
        title=record_title(record) or str(record_id),
        language=altsource_ms_language_for_locale(locale),
        kind=kind or table,
        text=text,
        crawled_at=now,
        hash=_sha1(text, 16),
        tos_accepted=True,
        trust="B",
        source_hash=_source_sha256(record),
    )


def _take_page(
    pages: list[WebPage],
    remaining: Optional[int],
    page: Optional[WebPage],
) -> Optional[int]:
    if page is None:
        return remaining
    if remaining is not None and remaining <= 0:
        return remaining
    pages.append(page)
    if remaining is not None:
        remaining -= 1
    return remaining


def _fetch_pages_parallel(
    items: Iterable[Any],
    worker: Callable[[Any], Optional[WebPage]],
    pages: list[WebPage],
    remaining: Optional[int],
    workers: int,
    delay: float,
    checkpoint: Optional[Callable[[], None]] = None,
    skip: Optional[Callable[[Any], bool]] = None,
    checkpoint_every: int = 2000,
) -> Optional[int]:
    last_checkpoint = len(pages)

    def maybe_checkpoint() -> None:
        nonlocal last_checkpoint
        if checkpoint and len(pages) - last_checkpoint >= checkpoint_every:
            checkpoint()
            last_checkpoint = len(pages)

    if workers <= 1:
        for item in items:
            if skip and skip(item):
                continue
            remaining = _take_page(pages, remaining, worker(item))
            maybe_checkpoint()
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
        return remaining

    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        pending: set[Any] = set()
        while True:
            while len(pending) < workers:
                try:
                    item = next(iterator)
                except StopIteration:
                    break
                if skip and skip(item):
                    continue
                pending.add(executor.submit(worker, item))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    page = future.result()
                except _NETWORK_ERRORS:
                    page = None
                remaining = _take_page(pages, remaining, page)
                maybe_checkpoint()
                if remaining is not None and remaining <= 0:
                    for queued in pending:
                        queued.cancel()
                    return remaining
            if delay:
                time.sleep(delay)
    return remaining


def _crawl_altsource_ms_events(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
    known_ids: Optional[set[str]] = None,
) -> tuple[Optional[int], set[int]]:
    stories = fetch_altsource_ms_master(server, "eventStories.json", fetcher)
    event_ids: set[int] = set()
    tasks: list[tuple[Any, ...]] = []
    for story in stories:
        event_id = story.get("eventId") or story.get("id")
        assetbundle_name = story.get("assetbundleName")
        if not event_id or not assetbundle_name:
            continue
        try:
            event_ids.add(int(event_id))
        except (TypeError, ValueError):
            pass
        for episode in story.get("eventStoryEpisodes") or []:
            if not isinstance(episode, dict):
                continue
            scenario_id = episode.get("scenarioId")
            episode_no = episode.get("episodeNo")
            if not scenario_id or not episode_no:
                continue
            title = f"{story.get('name') or event_id} 第{episode_no}话 {episode.get('title') or ''}".strip()
            tasks.append(
                (
                    altsource_ms_page_id(locale, "event_story", event_id, episode_no),
                    event_id,
                    assetbundle_name,
                    episode_no,
                    scenario_id,
                    title,
                )
            )

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, event_id, assetbundle_name, episode_no, scenario_id, title = task
        scenario = fetch_altsource_ms_scenario(
            server,
            f"event_story/{assetbundle_name}/scenario/{scenario_id}.json",
            fetcher,
            altsource_ms_language_for_locale(locale),
        )
        if not scenario:
            return None
        return altsource_ms_scenario_page(
            server,
            locale,
            altsource_ms_page_id(locale, "event_story", event_id, episode_no),
            title,
            altsource_ms_canonical_url(locale, f"story/event/{event_id}/{episode_no}/"),
            scenario,
            "event_story",
        )

    remaining = _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )
    return remaining, event_ids


def _crawl_altsource_ms_unit_stories(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    profiles = fetch_altsource_ms_master(server, "unitProfiles.json", fetcher)
    unit_seq = {profile.get("unit"): profile.get("seq") for profile in profiles}
    stories = fetch_altsource_ms_master(server, "unitStories.json", fetcher)
    per_unit_count: dict[str, int] = {}
    for story in stories:
        unit_key = story.get("unit")
        seq = unit_seq.get(unit_key) or story.get("seq")
        for chapter in story.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            chapter_asset = chapter.get("assetbundleName")
            for episode in chapter.get("episodes") or []:
                if not isinstance(episode, dict):
                    continue
                scenario_id = episode.get("scenarioId")
                if not scenario_id:
                    continue
                episode_label = str(episode.get("episodeNoLabel") or "")
                if episode_label == "序章" or episode.get("episodeNo") == 1:
                    continue
                per_unit_count[unit_key] = per_unit_count.get(unit_key, 0) + 1
                if per_unit_count[unit_key] > 20:
                    continue
                assetbundle_name = chapter_asset or episode.get("assetbundleName")
                if not assetbundle_name:
                    continue
                page_id = altsource_ms_page_id(locale, "unit_story", scenario_id)
                if _page_known(page_id, known_ids):
                    continue
                scenario = fetch_altsource_ms_scenario(
                    server,
                    f"scenario/unitstory/{assetbundle_name}/{scenario_id}.json",
                    fetcher,
                    altsource_ms_language_for_locale(locale),
                )
                if not scenario:
                    continue
                title = f"{episode.get('title') or ''} {episode.get('episodeNoLabel') or ''}".strip()
                page = altsource_ms_scenario_page(
                    server,
                    locale,
                    page_id,
                    title,
                    altsource_ms_canonical_url(locale, f"story/unit/{seq}/{scenario_id}/"),
                    scenario,
                    "unit_story",
                )
                remaining = _take_page(pages, remaining, page)
                if remaining is not None and remaining <= 0:
                    return remaining
                if delay:
                    time.sleep(delay)
    return remaining


def _crawl_altsource_ms_card_stories(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    cards = {
        card.get("id"): card
        for card in fetch_altsource_ms_master(server, "cards.json", fetcher)
    }
    episodes = fetch_altsource_ms_master(server, "cardEpisodes.json", fetcher)
    member_dir = "member_scenario" if server == "en" else "member"
    language = altsource_ms_language_for_locale(locale)
    tasks: list[tuple[Any, ...]] = []
    for episode in episodes:
        card = cards.get(episode.get("cardId"))
        if not card:
            continue
        scenario_id = episode.get("scenarioId")
        assetbundle_name = card.get("assetbundleName")
        if not scenario_id or not assetbundle_name:
            continue
        page_id = altsource_ms_page_id(locale, "card_story", episode.get("id") or episode.get("cardId"))
        title = f"{card.get('prefix') or ''} {episode.get('title') or ''}".strip()
        tasks.append(
            (
                page_id,
                member_dir,
                assetbundle_name,
                scenario_id,
                title,
                card.get("id"),
            )
        )

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, member_dir, assetbundle_name, scenario_id, title, card_id = task
        scenario = fetch_altsource_ms_scenario(
            server,
            f"character/{member_dir}/{assetbundle_name}/{scenario_id}.json",
            fetcher,
            language,
        )
        if not scenario:
            return None
        return altsource_ms_scenario_page(
            server,
            locale,
            _page_id,
            title,
            altsource_ms_canonical_url(locale, f"story/card/{card_id}/"),
            scenario,
            "card_story",
        )

    return _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )


def _crawl_altsource_ms_area_talks(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    actions = fetch_altsource_ms_master(server, "actionSets.json", fetcher)
    language = altsource_ms_language_for_locale(locale)
    tasks: list[tuple[Any, ...]] = []
    for action in actions:
        scenario_id = action.get("scenarioId")
        if not scenario_id:
            continue
        group = int(action.get("id", 0)) // 100
        page_id = altsource_ms_page_id(locale, "area_talk", scenario_id)
        area_id = action.get("areaId", "")
        title = f"\u533a\u57df\u5bf9\u8bdd {area_id} {scenario_id}"
        tasks.append((page_id, group, scenario_id, area_id, title))

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, group, scenario_id, area_id, title = task
        scenario = fetch_altsource_ms_scenario(
            server,
            f"scenario/actionset/group{group}/{scenario_id}.json",
            fetcher,
            language,
        )
        if not scenario:
            return None
        return altsource_ms_scenario_page(
            server,
            locale,
            _page_id,
            title,
            altsource_ms_canonical_url(locale, f"story/area/{area_id}/{scenario_id}/"),
            scenario,
            "area_talk",
        )

    return _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )


def _crawl_altsource_ms_virtual_lives(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    language = altsource_ms_language_for_locale(locale)
    tasks: list[tuple[Any, ...]] = []
    for record in fetch_altsource_ms_master(server, "virtualLives.json", fetcher):
        live_id = record.get("id") or record.get("seq")
        live_name = record.get("name") or live_id
        for setlist in record.get("virtualLiveSetlists") or []:
            if not isinstance(setlist, dict):
                continue
            setlist_type = setlist.get("virtualLiveSetlistType")
            assetbundle_name = setlist.get("assetbundleName")
            if not assetbundle_name:
                continue
            if setlist_type == "mc":
                paths = (
                    f"virtual_live/mc/scenario/{assetbundle_name}/{assetbundle_name}.json",
                )
            elif setlist_type == "mc_timeline":
                paths = (
                    f"virtual_live/mc/timeline/{assetbundle_name}/{assetbundle_name}.json",
                    f"virtual_live/mc/timeline/{assetbundle_name}/{assetbundle_name}.playable",
                )
            else:
                continue
            page_id = altsource_ms_page_id(locale, "virtual_live", setlist.get("id") or assetbundle_name)
            title = f"{live_name} {setlist.get('seq') or ''}".strip()
            tasks.append((page_id, live_id, title, paths))

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, live_id, title, paths = task
        scenario = None
        for path in paths:
            scenario = fetch_altsource_ms_scenario(server, path, fetcher, language)
            if scenario:
                break
        if not scenario:
            return None
        return altsource_ms_scenario_page(
            server,
            locale,
            _page_id,
            title,
            altsource_ms_canonical_url(locale, f"virtual_live/{live_id}"),
            scenario,
            "virtual_live",
        )

    remaining = _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )
    if remaining is not None and remaining <= 0:
        return remaining
    for table in (
        "virtualLiveSetlists.json",
        "virtualLiveCheerMessages.json",
        "virtualLivePamphlets.json",
        "paidVirtualLives.json",
    ):
        for record in fetch_altsource_ms_master(server, table, fetcher):
            raw_page = altsource_ms_record_page(record, table.replace(".json", ""), server, locale)
            if _page_known(raw_page.id, known_ids):
                continue
            remaining = _take_page(pages, remaining, raw_page)
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
    return remaining


def _crawl_altsource_ms_home_lines(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    for table, text_key in (
        ("characterArchiveVoices.json", "displayPhrase"),
        ("systemLive2ds.json", "serif"),
    ):
        for record in fetch_altsource_ms_master(server, table, fetcher):
            text = str(record.get(text_key) or "").strip()
            if not text:
                continue
            record_id = record.get("id", _sha1(text))
            kind = "home_line" if table == "characterArchiveVoices.json" else table.split(".")[0]
            page_id = altsource_ms_page_id(locale, kind, record_id)
            if _page_known(page_id, known_ids):
                continue
            page = WebPage(
                id=page_id,
                source=_current_ms_instance(), source_type=BACKEND_MOESEKAI,
                url=f"{ALTSOURCE_MS_METADATA_BASES[0]}/{server}/master/{table}",
                title=str(record.get("assetName") or record_id),
                language=altsource_ms_language_for_locale(locale),
                kind=kind,
                text=text,
                crawled_at=datetime.now(timezone.utc).isoformat(),
                hash=_sha1(text, 16),
                tos_accepted=True,
            )
            remaining = _take_page(pages, remaining, page)
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
    return remaining


def _crawl_altsource_ms_special_stories(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    stories = fetch_altsource_ms_master(server, "specialStories.json", fetcher)
    for story in stories:
        special_id = story.get("id")
        for episode in story.get("episodes") or []:
            if not isinstance(episode, dict):
                continue
            scenario_id = episode.get("scenarioId")
            if not scenario_id:
                continue
            if str(scenario_id).startswith("op"):
                assetbundle_name = story.get("assetbundleName") or episode.get("assetbundleName")
            else:
                assetbundle_name = episode.get("assetbundleName") or story.get("assetbundleName")
            if not assetbundle_name:
                continue
            page_id = altsource_ms_page_id(locale, "special_story", episode.get("id") or scenario_id)
            if _page_known(page_id, known_ids):
                continue
            scenario = fetch_altsource_ms_scenario(
                server,
                f"scenario/special/{assetbundle_name}/{scenario_id}.json",
                fetcher,
                altsource_ms_language_for_locale(locale),
            )
            if not scenario:
                continue
            title = f"{episode.get('title') or special_id} {episode.get('episodeNo') or ''}".strip()
            page = altsource_ms_scenario_page(
                server,
                locale,
                page_id,
                title,
                altsource_ms_canonical_url(locale, f"story/special/{special_id}/"),
                scenario,
                "special_story",
            )
            remaining = _take_page(pages, remaining, page)
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
    return remaining


def _crawl_altsource_ms_self_intros(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    profiles = fetch_altsource_ms_master(server, "characterProfiles.json", fetcher)
    for profile in profiles:
        scenario_id = profile.get("scenarioId")
        character_id = profile.get("characterId")
        if not scenario_id or not character_id:
            continue
        page_id = altsource_ms_page_id(locale, "self_intro", scenario_id)
        if _page_known(page_id, known_ids):
            continue
        scenario = fetch_altsource_ms_scenario(
            server,
            f"scenario/profile/{scenario_id}.json",
            fetcher,
            altsource_ms_language_for_locale(locale),
        )
        if not scenario:
            continue
        page = altsource_ms_scenario_page(
            server,
            locale,
            page_id,
            f"自我介绍 {character_id}",
            altsource_ms_canonical_url(locale, f"story/self/{character_id}/"),
            scenario,
            "self_intro",
        )
        remaining = _take_page(pages, remaining, page)
        if remaining is not None and remaining <= 0:
            return remaining
        if delay:
            time.sleep(delay)
    return remaining


ALTSOURCE_MS_ALL_TEXT_TABLES = [
    "eventStories.json",
    "unitStories.json",
    "unitProfiles.json",
    "characterProfiles.json",
    "cardEpisodes.json",
    "cards.json",
    "specialStories.json",
    "virtualLives.json",
    "virtualLiveSetlists.json",
    "actionSets.json",
    "areas.json",
    "characterArchiveVoices.json",
    "systemLive2ds.json",
    "mysekaiCharacterTalks.json",
    "mysekaiCharacterTalkTweets.json",
    "stamps.json",
    "honors.json",
    "bondsHonorWords.json",
    "cheerfulCarnivalPartyNames.json",
    "musics.json",
    "musicVocals.json",
    "musicDifficulties.json",
    "events.json",
    "gachas.json",
    "missions.json",
    "items.json",
    "tips.json",
    "wordings.json",
]


def _crawl_altsource_ms_all_raw(
    server: str,
    locale: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    for table in ALTSOURCE_MS_ALL_TEXT_TABLES:
        for record in fetch_altsource_ms_master(server, table, fetcher):
            raw_page = altsource_ms_record_page(record, table.replace(".json", ""), server, locale)
            if _page_known(raw_page.id, known_ids):
                continue
            remaining = _take_page(pages, remaining, raw_page)
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
    return remaining


def crawl_altsource_ms(
    store_root: Path,
    depth: int = 1,
    locales: Iterable[str] = ("zh-cn",),
    limit: int = 0,
    accept_tos: bool = False,
    delay: float = 0.2,
    fetcher: Callable[[str], str] = fetch_http_text,
    tos_already_checked: bool = False,
    workers: int = 4,
    resume: bool = True,
    include_overlay: bool = True,
    settings: Optional[MoesekaiSettings] = None,
    instance: Optional[str] = None,
) -> dict[str, Any]:
    with _ms_instance_scope(instance):
        return _crawl_altsource_ms_impl(
            store_root, depth=depth, locales=locales, limit=limit,
            accept_tos=accept_tos, delay=delay, fetcher=fetcher,
            tos_already_checked=tos_already_checked, workers=workers,
            resume=resume, include_overlay=include_overlay, settings=settings,
        )


def _crawl_altsource_ms_impl(
    store_root: Path,
    depth: int,
    locales: Iterable[str],
    limit: int,
    accept_tos: bool,
    delay: float,
    fetcher: Callable[[str], str],
    tos_already_checked: bool,
    workers: int,
    resume: bool,
    include_overlay: bool,
    settings: Optional[MoesekaiSettings],
) -> dict[str, Any]:
    if settings is not None:
        apply_source_settings(moesekai=settings)
    if not tos_already_checked:
        require_tos_consent(accept_tos)
    require_endpoint(ALTSOURCE_MS_BASE, "site_base", _current_ms_instance())
    require_endpoint(ALTSOURCE_MS_METADATA_BASES, "metadata_bases", _current_ms_instance())
    require_endpoint(ALTSOURCE_MS_ASSET_BASES, "asset_bases", _current_ms_instance())
    if include_overlay:
        require_endpoint(ALTSOURCE_MS_TRANSLATION_BASE, "translation_base", _current_ms_instance())
    write_consent(store_root, _current_ms_instance())
    pages: list[WebPage] = []
    overlay_pages: list[WebPage] = []
    remaining: Optional[int] = None if limit <= 0 else limit
    existing_pages: dict[str, dict[str, Any]] = {}
    existing_overlay_pages: dict[str, dict[str, Any]] = {}
    if resume:
        existing_pages = load_existing_page_map(store_root, _current_ms_instance())
        existing_overlay_pages = load_existing_page_map(store_root, _ms_aux())
        known_ids = {
            pid
            for pid, item in existing_pages.items()
            if not (
                item.get("asset_mismatch")
                or item.get("untranslated")
                or item.get("content_language_mismatch")
            )
        } | {
            pid
            for pid, item in existing_overlay_pages.items()
            if not (
                item.get("asset_mismatch")
                or item.get("untranslated")
                or item.get("content_language_mismatch")
            )
        }
    else:
        known_ids = None

    def checkpoint() -> None:
        save_web_pages(
            store_root,
            _current_ms_instance(),
            pages,
            existing=existing_pages,
            rewrite_index=False,
            write_categories=False,
        )
        if overlay_pages:
            save_web_pages(
                store_root,
                _ms_aux(),
                overlay_pages,
                existing=existing_overlay_pages,
                rewrite_index=False,
                write_categories=False,
            )
        print(f"[altsource_ms] {len(existing_pages)} pages saved")

    for locale in locales:
        locale = locale.strip()
        server = altsource_ms_server_for_locale(locale)
        if depth >= 1:
            remaining, event_ids = _crawl_altsource_ms_events(
                server,
                locale,
                fetcher,
                pages,
                remaining,
                delay,
                workers,
                checkpoint,
                known_ids,
            )
            if remaining is not None and remaining <= 0:
                break
            if include_overlay:
                remaining = _crawl_altsource_ms_translation_events(
                    locale,
                    fetcher,
                    overlay_pages,
                    remaining,
                    delay,
                    known_ids,
                    event_ids,
                )
                if remaining is not None and remaining <= 0:
                    break
            remaining = _crawl_altsource_ms_unit_stories(
                server, locale, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
        if depth >= 2:
            remaining = _crawl_altsource_ms_card_stories(
                server,
                locale,
                fetcher,
                pages,
                remaining,
                delay,
                known_ids,
                workers,
                checkpoint,
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
        if depth >= 3:
            remaining = _crawl_altsource_ms_virtual_lives(
                server,
                locale,
                fetcher,
                pages,
                remaining,
                delay,
                known_ids,
                workers,
                checkpoint,
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_ms_area_talks(
                server,
                locale,
                fetcher,
                pages,
                remaining,
                delay,
                known_ids,
                workers,
                checkpoint,
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_ms_home_lines(
                server, locale, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
        if depth >= 4:
            remaining = _crawl_altsource_ms_special_stories(
                server, locale, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_ms_self_intros(
                server, locale, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_ms_all_raw(
                server, locale, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
    index_path = save_web_pages(
        store_root,
        _current_ms_instance(),
        pages,
        existing=existing_pages,
    )
    overlay_index_path = None
    if overlay_pages:
        overlay_index_path = save_web_pages(
            store_root,
            _ms_aux(),
            overlay_pages,
            existing=existing_overlay_pages,
        )
    return {
        "source": _current_ms_instance(),
        "depth": depth,
        "crawled_pages": len(pages),
        "index": str(index_path),
        "category_files": [str(web_category_dir(store_root, _current_ms_instance()) / filename) for filename, _ in WEB_CATEGORY_FILES],
        "overlay_pages": len(overlay_pages),
        "overlay_index": str(overlay_index_path) if overlay_index_path else None,
    }


def _crawl_altsource_sv_events(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    tasks: list[tuple[Any, ...]] = []
    for story in fetch_altsource_sv_master(region, "eventStories.json", fetcher):
        event_id = story.get("eventId") or story.get("id")
        assetbundle_name = story.get("assetbundleName")
        if not event_id or not assetbundle_name:
            continue
        for episode in story.get("eventStoryEpisodes") or []:
            if not isinstance(episode, dict):
                continue
            scenario_id = episode.get("scenarioId")
            episode_no = episode.get("episodeNo")
            if not scenario_id or not episode_no:
                continue
            title = f"{event_id} 第{episode_no}话 {episode.get('title') or ''}".strip()
            tasks.append(
                (
                    f"web:{_current_sv_instance()}:{region}:event_story:{event_id}:{episode_no}",
                    event_id,
                    assetbundle_name,
                    episode_no,
                    scenario_id,
                    title,
                )
            )

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, event_id, assetbundle_name, episode_no, scenario_id, title = task
        url, scenario = fetch_altsource_sv_asset(
            region,
            (f"event_story/{assetbundle_name}/scenario/{scenario_id}.asset",),
            fetcher,
        )
        if scenario is None:
            return None
        return altsource_sv_scenario_page(
            region,
            f"web:{_current_sv_instance()}:{region}:event_story:{event_id}:{episode_no}",
            title,
            url,
            scenario,
            "event_story",
        )

    return _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )


def _crawl_altsource_sv_unit_stories(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    stories = fetch_altsource_sv_master(region, "unitStories.json", fetcher)
    per_unit_count: dict[str, int] = {}
    for story in stories:
        unit_key = story.get("unit")
        for chapter in story.get("chapters") or []:
            if not isinstance(chapter, dict):
                continue
            chapter_asset = chapter.get("assetbundleName")
            for episode in chapter.get("episodes") or []:
                if not isinstance(episode, dict):
                    continue
                scenario_id = episode.get("scenarioId")
                if not scenario_id:
                    continue
                episode_label = str(episode.get("episodeNoLabel") or "")
                if episode.get("episodeNo") == 1 or episode_label in {"序章", "オープニング"}:
                    continue
                per_unit_count[unit_key] = per_unit_count.get(unit_key, 0) + 1
                if per_unit_count[unit_key] > 20:
                    continue
                assetbundle_name = chapter_asset or episode.get("assetbundleName")
                if not assetbundle_name:
                    continue
                page_id = f"web:{_current_sv_instance()}:{region}:unit_story:{scenario_id}"
                if _page_known(page_id, known_ids):
                    continue
                url, scenario = fetch_altsource_sv_asset(
                    region,
                    (f"scenario/unitstory/{assetbundle_name}/{scenario_id}.asset",),
                    fetcher,
                )
                if scenario is None:
                    continue
                title = f"{episode.get('title') or ''} {episode_label}".strip()
                page = altsource_sv_scenario_page(
                    region,
                    page_id,
                    title,
                    url,
                    scenario,
                    "unit_story",
                )
                remaining = _take_page(pages, remaining, page)
                if remaining is not None and remaining <= 0:
                    return remaining
                if delay:
                    time.sleep(delay)
    return remaining


def _crawl_altsource_sv_card_stories(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    cards = {
        card.get("id"): card
        for card in fetch_altsource_sv_master(region, "cards.json", fetcher)
    }
    tasks: list[tuple[Any, ...]] = []
    for episode in fetch_altsource_sv_master(region, "cardEpisodes.json", fetcher):
        card = cards.get(episode.get("cardId")) or {}
        scenario_id = episode.get("scenarioId")
        assetbundle_name = episode.get("assetbundleName") or card.get("assetbundleName")
        if not scenario_id or not assetbundle_name:
            continue
        page_id = f"web:{_current_sv_instance()}:{region}:card_story:{episode.get('id') or episode.get('cardId')}"
        paths = [f"character/member/{assetbundle_name}/{scenario_id}.asset"]
        if region == "en":
            paths.insert(0, f"character/member_scenario/{assetbundle_name}/{scenario_id}.asset")
        else:
            paths.append(f"character/member_scenario/{assetbundle_name}/{scenario_id}.asset")
        title = f"{card.get('prefix') or ''} {episode.get('title') or ''}".strip()
        tasks.append((page_id, paths, title, card.get("id")))

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, paths, title, card_id = task
        url, scenario = fetch_altsource_sv_asset(region, paths, fetcher)
        if scenario is None:
            return None
        return altsource_sv_scenario_page(
            region,
            _page_id,
            title,
            url,
            scenario,
            "card_story",
        )

    return _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )


def _crawl_altsource_sv_area_talks(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    tasks: list[tuple[Any, ...]] = []
    for action in fetch_altsource_sv_master(region, "actionSets.json", fetcher):
        scenario_id = action.get("scenarioId") or action.get("scriptId")
        if not scenario_id:
            continue
        group = int(action.get("id", 0)) // 100
        page_id = f"web:{_current_sv_instance()}:{region}:area_talk:{scenario_id}"
        paths = []
        if action.get("scenarioId"):
            paths.append(f"scenario/actionset/group{group}/{action['scenarioId']}.asset")
        if action.get("scriptId"):
            script_id = str(action["scriptId"])
            if script_id not in paths:
                paths.append(f"actionset/group{group}/{script_id}.asset")
            paths.append(f"scenario/actionset/group{group}/{script_id}.asset")
        area_id = action.get("areaId", "")
        title = f"\u533a\u57df\u5bf9\u8bdd {area_id} {scenario_id}"
        tasks.append((page_id, paths, title))

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, paths, title = task
        url, scenario = fetch_altsource_sv_asset(region, paths, fetcher)
        if scenario is None:
            return None
        return altsource_sv_scenario_page(
            region,
            _page_id,
            title,
            url,
            scenario,
            "area_talk",
        )

    return _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )


def _crawl_altsource_sv_special_stories(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    for story in fetch_altsource_sv_master(region, "specialStories.json", fetcher):
        special_id = story.get("id")
        for episode in story.get("episodes") or []:
            if not isinstance(episode, dict):
                continue
            scenario_id = episode.get("scenarioId")
            if not scenario_id:
                continue
            if str(scenario_id).startswith("op"):
                folder = story.get("assetbundleName") or episode.get("assetbundleName")
            else:
                folder = episode.get("assetbundleName")
            if not folder:
                continue
            page_id = f"web:{_current_sv_instance()}:{region}:special_story:{episode.get('id') or scenario_id}"
            if _page_known(page_id, known_ids):
                continue
            url, scenario = fetch_altsource_sv_asset(
                region,
                (f"scenario/special/{folder}/{scenario_id}.asset",),
                fetcher,
            )
            if scenario is None:
                continue
            title = f"{episode.get('title') or special_id} {episode.get('episodeNo') or ''}".strip()
            page = altsource_sv_scenario_page(
                region,
                page_id,
                title,
                url,
                scenario,
                "special_story",
            )
            remaining = _take_page(pages, remaining, page)
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
    return remaining


def _crawl_altsource_sv_self_intros(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    for profile in fetch_altsource_sv_master(region, "characterProfiles.json", fetcher):
        scenario_id = profile.get("scenarioId")
        character_id = profile.get("characterId")
        if not scenario_id or not character_id:
            continue
        page_id = f"web:{_current_sv_instance()}:{region}:self_intro:{scenario_id}"
        if _page_known(page_id, known_ids):
            continue
        url, scenario = fetch_altsource_sv_asset(
            region,
            (f"scenario/profile/{scenario_id}.asset",),
            fetcher,
        )
        if scenario is None:
            continue
        page = altsource_sv_scenario_page(
            region,
            page_id,
            f"自我介绍 {character_id}",
            url,
            scenario,
            "self_intro",
        )
        remaining = _take_page(pages, remaining, page)
        if remaining is not None and remaining <= 0:
            return remaining
        if delay:
            time.sleep(delay)
    return remaining


def _crawl_altsource_sv_virtual_lives(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    tasks: list[tuple[Any, ...]] = []
    for record in fetch_altsource_sv_master(region, "virtualLives.json", fetcher):
        live_id = record.get("id") or record.get("seq")
        live_name = record.get("name") or live_id
        for setlist in record.get("virtualLiveSetlists") or []:
            if not isinstance(setlist, dict):
                continue
            setlist_type = setlist.get("virtualLiveSetlistType")
            assetbundle_name = setlist.get("assetbundleName")
            if not assetbundle_name:
                continue
            if setlist_type == "mc":
                paths = (f"virtual_live/mc/scenario/{assetbundle_name}/{assetbundle_name}.asset",)
            elif setlist_type == "mc_timeline":
                paths = (f"virtual_live/mc/timeline/{assetbundle_name}/{assetbundle_name}.playable",)
            else:
                continue
            page_id = f"web:{_current_sv_instance()}:{region}:virtual_live:{setlist.get('id')}"
            title = f"{live_name} {setlist.get('seq') or ''}".strip()
            tasks.append((page_id, live_id, title, paths))

    def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
        _page_id, live_id, title, paths = task
        url, scenario = fetch_altsource_sv_asset(region, paths, fetcher)
        if scenario is None:
            return None
        return altsource_sv_scenario_page(
            region,
            _page_id,
            title,
            url,
            scenario,
            "virtual_live",
        )

    remaining = _fetch_pages_parallel(
        tasks,
        worker,
        pages,
        remaining,
        workers,
        delay,
        checkpoint,
        skip=lambda task: _page_known(task[0], known_ids),
    )
    if remaining is not None and remaining <= 0:
        return remaining
    for record in fetch_altsource_sv_master(region, "virtualLivePamphlets.json", fetcher):
        text = str(record.get("flavorText") or "").strip()
        if not text:
            continue
        record_id = record.get("id", _sha1(text))
        page_id = f"web:{_current_sv_instance()}:{region}:virtualLivePamphlets:{record_id}"
        if _page_known(page_id, known_ids):
            continue
        page = WebPage(
            id=page_id,
            source=_current_sv_instance(), source_type=BACKEND_SEKAI_VIEWER,
            url=altsource_sv_master_json_url(region, "virtualLivePamphlets.json"),
            title=str(record.get("name") or record_id),
            language=REGIONS[region].language,
            kind="virtualLivePamphlets",
            text=text,
            crawled_at=datetime.now(timezone.utc).isoformat(),
            hash=_sha1(text, 16),
            tos_accepted=True,
        )
        remaining = _take_page(pages, remaining, page)
        if remaining is not None and remaining <= 0:
            return remaining
        if delay:
            time.sleep(delay)
    return remaining


def _crawl_altsource_sv_home_lines(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    for table, text_key in (
        ("characterArchiveVoices.json", "displayPhrase"),
        ("systemLive2ds.json", "serif"),
    ):
        for record in fetch_altsource_sv_master(region, table, fetcher):
            text = str(record.get(text_key) or "").strip()
            if not text:
                continue
            record_id = record.get("id", _sha1(text))
            kind = "home_line" if table == "characterArchiveVoices.json" else table.split(".")[0]
            page_id = f"web:{_current_sv_instance()}:{region}:{kind}:{record_id}"
            if _page_known(page_id, known_ids):
                continue
            page = WebPage(
                id=page_id,
                source=_current_sv_instance(), source_type=BACKEND_SEKAI_VIEWER,
                url=altsource_sv_master_json_url(region, table),
                title=str(record.get("assetName") or record.get("assetbundleName") or record_id),
                language=REGIONS[region].language,
                kind=kind,
                text=text,
                crawled_at=datetime.now(timezone.utc).isoformat(),
                hash=_sha1(text, 16),
                tos_accepted=True,
            )
            remaining = _take_page(pages, remaining, page)
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
    return remaining


def _crawl_altsource_sv_mysekai(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
    workers: int = 1,
    checkpoint: Optional[Callable[[], None]] = None,
) -> Optional[int]:
    talks = fetch_altsource_sv_master(region, "mysekaiCharacterTalks.json", fetcher)
    talks_available = False
    probe_tasks: list[tuple[Any, ...]] = []
    for talk in talks[:3]:
        assetbundle_name = talk.get("assetbundleName")
        lua_name = talk.get("lua")
        if not assetbundle_name or not lua_name:
            continue
        record_id = talk.get("id", _sha1(lua_name))
        page_id = f"web:{_current_sv_instance()}:{region}:mysekai_talk:{record_id}"
        probe_tasks.append((page_id, f"{assetbundle_name}/{lua_name}.lua.txt", record_id))
        _url, content = fetch_altsource_sv_asset(
            region,
            (f"{assetbundle_name}/{lua_name}.lua.txt",),
            lambda url: fetch_http_text(url, timeout=8),
            as_json=False,
        )
        if content is not None:
            talks_available = True
            break
    tasks: list[tuple[Any, ...]] = []
    if talks_available:
        tasks = list(probe_tasks)
        seen = {task[0] for task in tasks}
        for talk in talks:
            assetbundle_name = talk.get("assetbundleName")
            lua_name = talk.get("lua")
            if not assetbundle_name or not lua_name:
                continue
            record_id = talk.get("id", _sha1(lua_name))
            page_id = f"web:{_current_sv_instance()}:{region}:mysekai_talk:{record_id}"
            if page_id in seen:
                continue
            tasks.append((page_id, f"{assetbundle_name}/{lua_name}.lua.txt", record_id))
            seen.add(page_id)
    if talks_available:

        def worker(task: tuple[Any, ...]) -> Optional[WebPage]:
            page_id, path, record_id = task
            url, content = fetch_altsource_sv_asset(
                region,
                (path,),
                fetcher,
                as_json=False,
            )
            if content is None:
                return None
            text = mysekai_lua_to_text(str(content))
            if not text:
                return None
            return WebPage(
                id=page_id,
                source=_current_sv_instance(), source_type=BACKEND_SEKAI_VIEWER,
                url=url,
                title=str(record_id),
                language=REGIONS[region].language,
                kind="mysekai_talk",
                text=text,
                crawled_at=datetime.now(timezone.utc).isoformat(),
                hash=_sha1(text, 16),
                tos_accepted=True,
            )

        remaining = _fetch_pages_parallel(
            tasks,
            worker,
            pages,
            remaining,
            workers,
            delay,
            checkpoint,
            skip=lambda task: _page_known(task[0], known_ids),
        )
        if remaining is not None and remaining <= 0:
            return remaining
    for tweet in fetch_altsource_sv_master(region, "mysekaiCharacterTalkTweets.json", fetcher):
        text = str(tweet.get("text") or "").strip()
        if not text:
            continue
        record_id = tweet.get("id", _sha1(text))
        page_id = f"web:{_current_sv_instance()}:{region}:mysekai_tweet:{record_id}"
        if _page_known(page_id, known_ids):
            continue
        page = WebPage(
            id=page_id,
            source=_current_sv_instance(), source_type=BACKEND_SEKAI_VIEWER,
            url=altsource_sv_master_json_url(region, "mysekaiCharacterTalkTweets.json"),
            title=str(record_id),
            language=REGIONS[region].language,
            kind="mysekai_tweet",
            text=text,
            crawled_at=datetime.now(timezone.utc).isoformat(),
            hash=_sha1(text, 16),
            tos_accepted=True,
        )
        remaining = _take_page(pages, remaining, page)
        if remaining is not None and remaining <= 0:
            return remaining
        if delay:
            time.sleep(delay)
    return remaining


ALTSOURCE_SV_OTHER_TEXT_TABLES = [
    "events",
    "gameCharacters",
    "gameCharacterUnits",
    "musics",
    "musicVocals",
    "musicDifficulties",
    "gachas",
    "areas",
    "areaItems",
    "stamps",
    "honors",
    "bondsHonorWords",
    "cheerfulCarnivalPartyNames",
    "missions",
    "items",
    "versions",
    "liveTalks",
    "birthdayPartyScenarios",
    "virtualLiveGroups",
    "virtualLiveCheerMessages",
    "paidVirtualLives",
    "tips",
    "wordings",
    "mysekaiFixtureLabels",
    "mysekaiFixtureTags",
    "mysekaiMaterials",
    "mysekaiSites",
]


def _crawl_altsource_sv_other_text(
    region: str,
    fetcher: Callable[[str], str],
    pages: list[WebPage],
    remaining: Optional[int],
    delay: float,
    known_ids: Optional[set[str]] = None,
) -> Optional[int]:
    for table in ALTSOURCE_SV_OTHER_TEXT_TABLES:
        for record in fetch_altsource_sv_master(region, table, fetcher):
            raw_page = altsource_sv_record_page(record, table, region)
            if _page_known(raw_page.id, known_ids):
                continue
            remaining = _take_page(pages, remaining, raw_page)
            if remaining is not None and remaining <= 0:
                return remaining
            if delay:
                time.sleep(delay)
    return remaining


def _crawl_altsource_sv_text(
    store_root: Path,
    regions: Iterable[str],
    depth: int,
    limit: int,
    delay: float,
    fetcher: Callable[[str], str],
    workers: int = 4,
    resume: bool = True,
    include_i18n: bool = True,
) -> dict[str, Any]:
    pages: list[WebPage] = []
    remaining: Optional[int] = None if limit <= 0 else limit
    existing_pages: dict[str, dict[str, Any]] = {}
    if resume:
        existing_pages = load_existing_page_map(store_root, _current_sv_instance())
        known_ids = {
            pid
            for pid, item in existing_pages.items()
            if not (
                item.get("asset_mismatch")
                or item.get("untranslated")
                or item.get("content_language_mismatch")
            )
        }
    else:
        known_ids = None

    def checkpoint() -> None:
        save_web_pages(
            store_root,
            _current_sv_instance(),
            pages,
            existing=existing_pages,
            rewrite_index=False,
            write_categories=False,
        )
        print(f"[altsource_sv] {len(existing_pages)} pages saved")

    for region in regions:
        region = region.strip()
        if region not in REGIONS:
            raise ValueError(f"Unknown region: {region}")
        if depth >= 1:
            remaining = _crawl_altsource_sv_events(
                region,
                fetcher,
                pages,
                remaining,
                delay,
                workers,
                checkpoint,
                known_ids,
            )
            if remaining is not None and remaining <= 0:
                break
            remaining = _crawl_altsource_sv_unit_stories(
                region, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
        if depth >= 2:
            remaining = _crawl_altsource_sv_card_stories(
                region,
                fetcher,
                pages,
                remaining,
                delay,
                known_ids,
                workers,
                checkpoint,
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
        if depth >= 3:
            remaining = _crawl_altsource_sv_virtual_lives(
                region,
                fetcher,
                pages,
                remaining,
                delay,
                known_ids,
                workers,
                checkpoint,
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_sv_area_talks(
                region,
                fetcher,
                pages,
                remaining,
                delay,
                known_ids,
                workers,
                checkpoint,
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_sv_home_lines(
                region, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
        if depth >= 4:
            remaining = _crawl_altsource_sv_special_stories(
                region, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_sv_self_intros(
                region, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_sv_mysekai(
                region,
                fetcher,
                pages,
                remaining,
                delay,
                known_ids,
                workers,
                checkpoint,
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
            remaining = _crawl_altsource_sv_other_text(
                region, fetcher, pages, remaining, delay, known_ids
            )
            if remaining is not None and remaining <= 0:
                break
            checkpoint()
    index_path = save_web_pages(
        store_root,
        _current_sv_instance(),
        pages,
        existing=existing_pages,
    )
    i18n_result = None
    if include_i18n:
        i18n_result = _crawl_altsource_sv_i18n(
            store_root,
            fetcher,
            delay,
            resume,
        )
    return {
        "source": _current_sv_instance(),
        "depth": depth,
        "crawled_pages": len(pages),
        "index": str(index_path),
        "category_files": [str(web_category_dir(store_root, _current_sv_instance()) / filename) for filename, _ in WEB_CATEGORY_FILES],
        "i18n_pages": i18n_result["pages"] if i18n_result else 0,
        "i18n_index": i18n_result["index"] if i18n_result else None,
    }


def tables_for_depth(depth: int) -> tuple[str, ...]:
    if depth >= 4:
        return tuple(ALTSOURCE_SV_TABLES)
    if depth >= 3:
        return (
            "eventStories",
            "unitStories",
            "unitProfiles",
            "characterProfiles",
            "cardEpisodes",
            "cards",
            "virtualLives",
            "virtualLivePamphlets",
            "actionSets",
            "areas",
            "characterArchiveVoices",
            "systemLive2ds",
            "liveTalks",
            "mysekaiCharacterTalks",
            "mysekaiCharacterTalkTweets",
        )
    if depth >= 2:
        return (
            "eventStories",
            "unitStories",
            "unitProfiles",
            "characterProfiles",
            "cardEpisodes",
            "cards",
        )
    return (
        "eventStories",
        "unitStories",
        "unitProfiles",
        "characterProfiles",
    )


def crawl_altsource_sv(
    store_root: Path,
    regions: Iterable[str] = ("jp",),
    tables: Optional[Iterable[str]] = None,
    limit: int = 0,
    accept_tos: bool = False,
    delay: float = 0.1,
    fetcher: Callable[[str], str] = fetch_http_text,
    tos_already_checked: bool = False,
    depth: int = 1,
    workers: int = 4,
    resume: bool = True,
    include_i18n: bool = True,
    settings: Optional[ViewerSettings] = None,
    instance: Optional[str] = None,
) -> dict[str, Any]:
    with _sv_instance_scope(instance):
        return _crawl_altsource_sv_impl(
            store_root, regions=regions, tables=tables, limit=limit,
            accept_tos=accept_tos, delay=delay, fetcher=fetcher,
            tos_already_checked=tos_already_checked, depth=depth, workers=workers,
            resume=resume, include_i18n=include_i18n, settings=settings,
        )


def _crawl_altsource_sv_impl(
    store_root: Path,
    regions: Iterable[str],
    tables: Optional[Iterable[str]],
    limit: int,
    accept_tos: bool,
    delay: float,
    fetcher: Callable[[str], str],
    tos_already_checked: bool,
    depth: int,
    workers: int,
    resume: bool,
    include_i18n: bool,
    settings: Optional[ViewerSettings],
) -> dict[str, Any]:
    if settings is not None:
        apply_source_settings(viewer=settings)
    if not tos_already_checked:
        require_tos_consent(accept_tos)
    require_endpoint(ALTSOURCE_SV_MASTER_BASE, "master_base", _current_sv_instance())
    require_endpoint(ALTSOURCE_SV_ASSET_BASE, "asset_base", _current_sv_instance())
    if include_i18n:
        require_endpoint(ALTSOURCE_SV_I18N_BASE, "i18n_base", _current_sv_instance())
    write_consent(store_root, _current_sv_instance())
    if tables is None:
        return _crawl_altsource_sv_text(
            store_root,
            regions,
            depth=depth,
            limit=limit,
            delay=delay,
            fetcher=fetcher,
            workers=workers,
            resume=resume,
            include_i18n=include_i18n,
        )
    selected_tables = tuple(tables) if tables is not None else tables_for_depth(depth)
    pages: list[WebPage] = []
    remaining = limit
    for region in regions:
        for table in selected_tables:
            url = altsource_sv_master_json_url(region, table)
            try:
                data = json.loads(fetcher(url))
            except _NETWORK_ERRORS:
                continue
            records = data if isinstance(data, list) else [data]
            for record in records:
                if not isinstance(record, dict):
                    continue
                pages.append(altsource_sv_record_page(record, table, region))
                if limit > 0:
                    remaining -= 1
                    if remaining <= 0:
                        break
            if limit > 0 and remaining <= 0:
                break
            if delay:
                time.sleep(delay)
        if limit > 0 and remaining <= 0:
            break

    index_path = save_web_pages(store_root, _current_sv_instance(), pages)
    return {
        "source": _current_sv_instance(),
        "crawled_pages": len(pages),
        "index": str(index_path),
        "category_files": [str(web_category_dir(store_root, _current_sv_instance()) / filename) for filename, _ in WEB_CATEGORY_FILES],
    }
