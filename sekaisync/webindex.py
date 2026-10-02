from __future__ import annotations

import hashlib
import heapq
import itertools
import json
import re
import unicodedata
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sekaisync import dbstore
from sekaisync import searchindex
from sekaisync.filecache import cached_json
from sekaisync.layout import web_category_dir, web_index_path, web_pages_path, web_root
from sekaisync.models import WebPage
from sekaisync.normalize import best_match, normalize_name
from sekaisync.sources import (
    BACKEND_MOESEKAI,
    DEFAULT_SOURCE_PRIORITY,
    KNOWN_BACKENDS,
    SOURCE_MS,
    SOURCE_SV,
    backend_of,
    backend_of_type,
    normalize_source_id,
    source_rank,
)
from sekaisync.trust import trust_for_page


WEB_CATEGORY_FILES = [
    ("01_mainline.json", "mainline"),
    ("02_event_story.json", "event_story"),
    ("03_card_story.json", "card_story"),
    ("04_special_story.json", "special_story"),
    ("05_virtual_live.json", "virtual_live"),
    ("06_area_dialogue.json", "area_dialogue"),
    ("07_character_voice.json", "character_voice"),
    ("08_mysekai.json", "mysekai"),
    ("09_other.json", "other"),
]

DERIVED_KINDS = {"story", "story_detail"}

CANONICAL_KINDS = frozenset(
    {
        "event_story",
        "unit_story",
        "card_story",
        "special_story",
        "virtual_live",
        "area_talk",
        "self_intro",
        "home_line",
        "mysekai_talk",
        "mysekai_tweet",
    }
)


def is_auxiliary_page(page: dict[str, Any]) -> bool:
    return bool(page.get("auxiliary", False)) or bool(page.get("overlay", False))


def sha256_hex(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


_METADATA_MISMATCH_MARKERS = ("ScenarioId ", "m_Name ")
_ID_LIKE = re.compile(r"^[A-Za-z0-9_./-]+$")


def normalize_scenario_mismatch_flag(page: dict[str, Any]) -> dict[str, Any]:
    """Drop ScenarioId/m_Name comparisons whose value is a display name, not an ID."""
    raw = str(page.get("scenario_id_mismatch") or "")
    if not raw:
        return page
    kept = []
    for token in raw.split(";"):
        token = token.strip()
        if not token:
            continue
        drop = False
        for marker in ("ScenarioId ", "m_Name "):
            if token.startswith(marker):
                value = token[len(marker):].split(" != ", 1)[0].strip()
                if not _ID_LIKE.fullmatch(value):
                    drop = True
                break
        if drop:
                continue
        kept.append(token)
    page["scenario_id_mismatch"] = "; ".join(kept)
    return page


def recompute_language_flags(page: dict[str, Any]) -> dict[str, Any]:
    """Re-evaluate language mismatch flags against stored story text."""
    normalize_scenario_mismatch_flag(page)
    if bool(page.get("untranslated", False)):
        page["content_language_mismatch"] = False
        mismatch = str(page.get("asset_mismatch") or "")
        page["asset_mismatch"] = "; ".join(
            token.strip()
            for token in mismatch.split(";")
            if token.strip() and not token.strip().startswith("language_mismatch:")
        )
        return page
    if is_auxiliary_page(page):
        return page
    kind = str(page.get("kind", "")).lower()
    if kind not in CANONICAL_KINDS:
        page["content_language_mismatch"] = False
        mismatch = str(page.get("asset_mismatch") or "")
        page["asset_mismatch"] = "; ".join(
            token.strip()
            for token in mismatch.split(";")
            if token.strip() and not token.strip().startswith("language_mismatch:")
        )
        return page
    language = str(page.get("language") or "")
    text = str(page.get("text") or "")
    ok = text_matches_language(language, text)
    mismatch = str(page.get("asset_mismatch") or "")
    tokens = [
        token.strip()
        for token in mismatch.split(";")
        if token.strip() and not token.strip().startswith("language_mismatch:")
    ]
    if not ok and language:
        page["content_language_mismatch"] = True
        tokens.append(f"language_mismatch: expected {language}, text script mismatch")
    else:
        page["content_language_mismatch"] = False
    page["asset_mismatch"] = "; ".join(tokens)
    return page


def normalize_mismatch_flags(page: dict[str, Any]) -> dict[str, Any]:
    """Move metadata-only asset name differences into the informational field."""
    mismatch = str(page.get("asset_mismatch") or "")
    if not mismatch:
        return page
    tokens = [token.strip() for token in mismatch.split(";") if token.strip()]
    metadata_tokens = [
        token for token in tokens if any(marker in token for marker in _METADATA_MISMATCH_MARKERS)
    ]
    other_tokens = [token for token in tokens if token not in metadata_tokens]
    if not metadata_tokens:
        return page
    existing = str(page.get("scenario_id_mismatch") or "")
    merged = "; ".join(part for part in (existing, "; ".join(metadata_tokens)) if part)
    page["scenario_id_mismatch"] = merged
    page["asset_mismatch"] = "; ".join(other_tokens)
    return page


def text_matches_language(language: str, text: str) -> bool:
    """Return True when the text script plausibly matches the declared language.

    The prolonged-sound mark (U+30FC) and iteration marks are excluded from the
    katakana count, and a small number of kana is tolerated inside official
    Chinese localizations that preserve Japanese song titles or interjections.
    """
    language = str(language or "").lower()
    sample = str(text or "")
    if not sample:
        return True
    ideo = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", sample))
    hira = len(re.findall(r"[\u3040-\u309f]", sample))
    kata = len(re.findall(r"[\u30a1-\u30fa\u30fd-\u30ff]", sample))
    hangul = len(re.findall(r"[\uac00-\ud7af]", sample))
    latin = len(re.findall(r"[A-Za-z]", sample))
    if language == "ja":
        if hangul:
            return hangul < max(5, int(ideo * 0.3) if ideo else 5)
        if hira + kata:
            return True
        if ideo == 0:
            return True
        if re.search(r"[\u3001\u300c\u300d\u30fb\u301c\uff5e\u300e\u300f]", sample):
            return True
        return ideo <= 4
    if language in {"zh_hans", "zh_hant", "zh_tw", "zh_cn"}:
        zh_sample = re.sub(r"[\u300a][^\u300b]*[\u300b]", "", sample)
        s_ideo = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", zh_sample))
        s_hira = len(re.findall(r"[\u3040-\u309f]", zh_sample))
        s_kata = len(re.findall(r"[\u30a1-\u30fa\u30fd-\u30ff]", zh_sample))
        if s_hira + s_kata == 0 and hangul == 0:
            return True
        return s_ideo > 0 and s_hira + s_kata <= int(s_ideo * 0.05)
    if language == "en":
        from sekaisync.line_alignment import strip_speaker_label
        body = unicodedata.normalize("NFKC", "\n".join(strip_speaker_label(line) for line in sample.splitlines()))
        foreign = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30fa\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff]", body))
        body_latin = len(re.findall(r"[A-Za-z]", body))
        return foreign == 0 or body_latin >= max(5, int(foreign * 0.3))
    if language == "ko":
        from sekaisync.line_alignment import strip_speaker_label
        body = unicodedata.normalize("NFKC", "\n".join(strip_speaker_label(line) for line in sample.splitlines()))
        body_hangul = len(re.findall(r"[\u1100-\u11ff\u3130-\u318f\ua960-\ua97f\uac00-\ud7ff]", body))
        foreign = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30fa]", body))
        if foreign:
            return body_hangul >= max(1, int(foreign * 0.3))
        if body_hangul:
            return True
        # Short borrowed names and calls can be entirely Latin; prose is not
        # established Korean merely because it contains no Han characters.
        return len(re.findall(r"[A-Za-z]+(?:['\u2019-][A-Za-z]+)*", body)) <= 4
    return True


def canonical_key_for_page(page: dict[str, Any]) -> str:
    from sekaisync import wording_identity
    wording = wording_identity._metadata(page)
    if wording is not None:
        return wording["family"]
    if is_auxiliary_page(page):
        return ""
    kind = str(page.get("kind", "")).lower()
    if kind not in {
        "event_story",
        "unit_story",
        "card_story",
        "special_story",
        "virtual_live",
        "area_talk",
        "self_intro",
        "home_line",
        "mysekai_talk",
        "mysekai_tweet",
    }:
        return ""
    url = str(page.get("url", ""))
    page_id = str(page.get("id", ""))
    language = str(page.get("language", "") or "unknown")
    if kind == "event_story":
        match = re.search(r"event_story:(\d+):(\d+)", page_id)
        if not match:
            match = re.search(r"/story/event/(\d+)/(\d+)/", url)
        if not match:
            match = re.search(r"event_story/(\d+)/(\d+)", url)
        return f"event_story:{language}:{match.group(1)}:{match.group(2)}" if match else ""
    if kind == "unit_story":
        match = re.search(r"unit_story:([^:]+)", page_id)
        if not match:
            match = re.search(r"/story/unit/(?:\d+)/([^/]+)/", url)
        return f"unit_story:{language}:{match.group(1)}" if match else ""
    if kind == "card_story":
        match = re.search(r"card_story:(\d+)", page_id)
        if not match:
            match = re.search(r"/story/card/(\d+)/", url)
        return f"card_story:{language}:{match.group(1)}" if match else ""
    if kind == "special_story":
        match = re.search(r"special_story:(\d+)", page_id)
        return f"special_story:{language}:{match.group(1)}" if match else ""
    if kind == "virtual_live":
        match = re.search(r"virtualLives:(\d+)", page_id)
        if not match:
            match = re.search(r"virtual_live:(\d+)", page_id)
        return f"virtual_live:{language}:{match.group(1)}" if match else ""
    if kind == "area_talk":
        match = re.search(r"area_talk:([^:]+)", page_id)
        if not match:
            match = re.search(r"/story/area/(?:\d+)/([^/]+)/", url)
        return f"area_talk:{language}:{match.group(1)}" if match else ""
    if kind == "self_intro":
        match = re.search(r"self_intro:([^:]+)", page_id)
        return f"self_intro:{language}:{match.group(1)}" if match else ""
    if kind == "home_line":
        match = re.search(r"characterArchiveVoices:(\d+)", page_id)
        if not match:
            match = re.search(r"home_line:(\d+)", page_id)
        return f"home_line:{language}:{match.group(1)}" if match else ""
    if kind == "mysekai_talk":
        match = re.search(r"mysekaiCharacterTalks:(\d+)", page_id)
        if not match:
            match = re.search(r"mysekai_talk:(\d+)", page_id)
        return f"mysekai_talk:{language}:{match.group(1)}" if match else ""
    if kind == "mysekai_tweet":
        match = re.search(r"mysekaiCharacterTalkTweets:(\d+)", page_id)
        if not match:
            match = re.search(r"mysekai_tweet:(\d+)", page_id)
        return f"mysekai_tweet:{language}:{match.group(1)}" if match else ""
    return ""


def web_page_to_dict(page: WebPage) -> dict[str, Any]:
    from sekaisync import wording_identity
    wording = wording_identity._metadata(vars(page))
    text = str(page.text or "")
    base = {
        "source": page.source,
        "kind": page.kind,
        "id": page.id,
        "url": page.url,
        "language": page.language,
        "auxiliary": page.auxiliary,
        "overlay": page.overlay,
    }
    result = {
        "id": page.id,
        "source": page.source,
        "url": page.url,
        "title": page.title,
        "language": page.language,
        "kind": page.kind,
        "text": page.text,
        "canonical_key": wording["family"] if wording is not None else canonical_key_for_page(base),
        "source_hash": page.source_hash,
        "text_hash": page.text_hash or sha256_hex(text),
        "untranslated": page.untranslated,
        "untranslated_placeholder": page.untranslated_placeholder,
        "original_text_hash": page.original_text_hash,
        "asset_mismatch": page.asset_mismatch,
        "scenario_id_mismatch": page.scenario_id_mismatch,
        "content_language_mismatch": page.content_language_mismatch,
        "source_last_modified": page.source_last_modified,
        "source_etag": page.source_etag,
        "crawled_at": page.crawled_at,
        "hash": page.hash,
        "tos_accepted": page.tos_accepted,
        "derived": page.derived,
        "trust": page.trust or trust_for_page(
            {
                "source": page.source,
                "source_type": page.source_type,
                "kind": page.kind,
                "derived": page.derived,
                "translation_source": page.translation_source,
                "auxiliary": page.auxiliary,
            }
        ),
        "auxiliary": page.auxiliary,
        "translation_source": page.translation_source,
        "source_language": page.source_language,
        "namespace": page.namespace,
        "event_id": page.event_id,
        "episode_no": page.episode_no,
        "overlay": page.overlay,
        "source_type": page.source_type,
        "instance": page.instance,
    }
    if wording is not None:
        result["wording_identity"] = deepcopy(page.wording_identity)
        result["wording_provenance"] = deepcopy(page.wording_provenance)
    return result


def web_page_from_dict(data: dict[str, Any]) -> WebPage:
    from sekaisync import wording_identity
    wording = wording_identity._metadata(data)
    normalize_mismatch_flags(data)
    page = WebPage(
        id=str(data["id"]),
        source=str(data.get("source", "")),
        url=str(data.get("url", "")),
        title=str(data.get("title", "")),
        language=str(data.get("language", "")),
        kind=str(data.get("kind", "page")),
        text=str(data.get("text", "")),
        crawled_at=str(data.get("crawled_at", "")),
        hash=str(data.get("hash", "")),
        tos_accepted=bool(data.get("tos_accepted", False)),
        derived=bool(data.get("derived", False)),
        trust=str(data.get("trust") or trust_for_page(data)),
        canonical_key=str(canonical_key_for_page(data)),
        source_hash=str(data.get("source_hash", "")),
        text_hash=str(data.get("text_hash") or sha256_hex(str(data.get("text", "")))),
        untranslated=bool(data.get("untranslated", False)),
        untranslated_placeholder=str(data.get("untranslated_placeholder", "")),
        original_text_hash=str(data.get("original_text_hash", "")),
        asset_mismatch=str(data.get("asset_mismatch", "")),
        scenario_id_mismatch=str(data.get("scenario_id_mismatch", "")),
        content_language_mismatch=bool(data.get("content_language_mismatch", False)),
        source_last_modified=str(data.get("source_last_modified", "")),
        source_etag=str(data.get("source_etag", "")),
        auxiliary=bool(data.get("auxiliary", False)),
        translation_source=str(data.get("translation_source", "")),
        source_language=str(data.get("source_language", "")),
        namespace=str(data.get("namespace", "")),
        event_id=int(data.get("event_id", 0) or 0),
        episode_no=int(data.get("episode_no", 0) or 0),
        overlay=bool(data.get("overlay", False)),
        source_type=str(data.get("source_type", "")),
        instance=str(data.get("instance", "")),
    )
    if wording is not None:
        page.wording_identity = deepcopy(data["wording_identity"])
        page.wording_provenance = deepcopy(data["wording_provenance"])
    return page


def page_backend(page: dict[str, Any]) -> str:
    """Backend class of a stored page.

    Prefers the explicit ``source_type`` written by the crawler; falls back
    to deriving it from the source ID for legacy pages (whose source is one
    of the canonical type IDs).
    """
    explicit = str(page.get("source_type") or "").strip().lower()
    if explicit in KNOWN_BACKENDS:
        return explicit
    return backend_of(str(page.get("source") or ""))


def matches_source_filter(page: dict[str, Any], wanted: str) -> bool:
    """Whether a page matches a source selector (type ID or instance ID).

    A type ID (``altsource_sv`` / ``altsource_ms``) matches every instance
    of that backend class; an instance ID matches exactly that instance.
    """
    wanted = normalize_source_id(wanted)
    backend = backend_of_type(wanted)
    if backend:
        if page_backend(page) == backend:
            return True
        return normalize_source_id(str(page.get("source") or "")) == wanted
    return normalize_source_id(str(page.get("source") or "")) == wanted


def is_derived_page(page: dict[str, Any]) -> bool:
    if bool(page.get("derived", False)):
        return True
    return (
        page_backend(page) == BACKEND_MOESEKAI
        and str(page.get("kind", "")).lower() in DERIVED_KINDS
    )


def page_category(page: dict[str, Any]) -> str:
    if is_auxiliary_page(page):
        return "other"
    if is_derived_page(page):
        return "other"
    kind = str(page.get("kind", "")).lower()
    table = kind
    if kind in {"unit_story", "main_story", "unitstories"}:
        return "mainline"
    if kind in {"event_story", "eventstories"}:
        return "event_story"
    if kind in {"card_story", "cardepisodes", "cards"}:
        return "card_story"
    if kind in {"special_story", "specialstories", "self_intro", "story", "story_detail"}:
        return "special_story"
    if kind in {
        "virtual_live",
        "virtuallives",
        "virtuallivesetlists",
        "virtuallivecheermessages",
        "virtuallivepamphlets",
        "virtuallivegroups",
        "paidvirtuallives",
    }:
        return "virtual_live"
    if kind in {"area_talk", "area_dialogue", "actionsets", "areas"}:
        return "area_dialogue"
    if kind in {
        "home_line",
        "character_voice",
        "characterarchivevoices",
        "systemlive2ds",
        "livetalks",
        "live_talk",
    }:
        return "character_voice"
    if kind in {
        "mysekai",
        "mysekai_talk",
        "mysekai_tweet",
        "mysekaicharactertalks",
        "mysekaicharactertalktweets",
        "mysekaifixtures",
        "mysekaifixturetags",
        "mysekaimaterials",
        "mysekaisites",
        "birthdaypartyscenarios",
    }:
        return "mysekai"
    return "other"


_CATEGORY_RECORD_FIELDS = (
    "id",
    "source",
    "source_type",
    "instance",
    "url",
    "title",
    "language",
    "kind",
    "canonical_key",
    "event_id",
    "episode_no",
    "text_length",
    "trust",
    "auxiliary",
    "overlay",
    "untranslated",
    "untranslated_placeholder",
    "translation_source",
    "source_language",
    "crawled_at",
)


def _category_record(page: dict[str, Any]) -> dict[str, Any]:
    """Small metadata record for the regenerated nine-category files."""
    record = {
        field: page.get(field)
        for field in _CATEGORY_RECORD_FIELDS
        if field in page
    }
    record["text_length"] = len(str(page.get("text") or ""))
    return record

def write_category_files(
    source_dir: Path,
    pages: list[dict[str, Any]],
    lightweight: bool = False,
) -> dict[str, int]:
    if lightweight:
        pages = [_category_record(page) for page in pages]
    source_dir.mkdir(parents=True, exist_ok=True)
    buckets: dict[str, list[dict[str, Any]]] = {category: [] for _file, category in WEB_CATEGORY_FILES}
    for page in pages:
        buckets[page_category(page)].append(page)
    counts = {}
    for filename, category in WEB_CATEGORY_FILES:
        path = source_dir / filename
        path.write_text(json.dumps(buckets[category], ensure_ascii=False, indent=2), encoding="utf-8")
        counts[category] = len(buckets[category])
    (source_dir / "categories.json").write_text(
        json.dumps(counts, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return counts


_INDEX_RECORD_FIELDS = (
    "id",
    "source",
    "url",
    "title",
    "language",
    "kind",
    "canonical_key",
    "event_id",
    "episode_no",
    "trust",
    "auxiliary",
    "overlay",
    "derived",
    "untranslated",
    "untranslated_placeholder",
    "asset_mismatch",
    "content_language_mismatch",
    "scenario_id_mismatch",
    "translation_source",
    "source_language",
    "crawled_at",
)


def _index_record(page: dict[str, Any]) -> dict[str, Any]:
    """Minimal metadata record for v2 indexes; full text lives in pages.json only."""
    record = {
        field: page.get(field)
        for field in _INDEX_RECORD_FIELDS
        if field in page
    }
    record["text_length"] = len(str(page.get("text") or ""))
    return record
def _read_pages_json_uncached(pages_path: Path) -> list[dict[str, Any]]:
    return json.loads(pages_path.read_text(encoding="utf-8"))

def _read_all_pages_uncached(store_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Read every pages.json straight from disk for write paths.

    Index rebuilds mutate the parsed page dicts in place, so they must
    never share objects with the read cache.
    """
    root = web_root(store_root)
    if not root.exists():
        return {}
    result: dict[str, list[dict[str, Any]]] = {}
    for source_dir in sorted(root.iterdir()):
        if not source_dir.is_dir():
            continue
        pages_path = source_dir / "pages.json"
        if not pages_path.exists():
            continue
        result[source_dir.name] = _read_pages_json_uncached(pages_path)
    return result

def write_web_index(store_root: Path) -> Path:
    """Historical merged-index writer; rows are already stored in the DB."""
    return dbstore.db_file(store_root)



def load_existing_page_map(
    store_root: Path,
    source: str,
) -> dict[str, dict[str, Any]]:
    return dbstore.existing_page_map(store_root, source)




def save_web_pages(
    store_root: Path,
    source: str,
    pages: Iterable[WebPage],
    existing: Optional[dict[str, dict[str, Any]]] = None,
    rewrite_index: bool = True,
    write_categories: bool = True,
) -> Path:
    if existing is None:
        existing = load_existing_page_map(store_root, source)
    for page in pages:
        existing[page.id] = {**existing.get(page.id, {}), **web_page_to_dict(page)}
    merged = list(existing.values())
    dbstore.save_web_pages_full(store_root, source, merged)
    if write_categories:
        write_category_files(
            web_category_dir(store_root, source),
            merged,
            lightweight=True,
        )
    if rewrite_index:
        return write_web_index(store_root)
    return dbstore.db_file(store_root)



def _read_pages_json(pages_path: Path) -> list[dict[str, Any]]:
    return cached_json(
        pages_path,
        lambda p: json.loads(p.read_text(encoding="utf-8")),
        scope="web_pages",
    )

def load_web_pages(store_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Per-source page dicts (full load, compat shape) from the SQLite store."""
    return dbstore.load_web_pages(store_root)




def load_web_category_counts(
    store_root: Path,
    source: Optional[str] = None,
) -> dict[str, dict[str, int]]:
    return dbstore.web_category_counts(store_root, source)



def load_existing_page_ids(
    store_root: Path,
    source: str,
    skip_flagged: bool = False,
) -> set[str]:
    return dbstore.web_page_ids(store_root, source, skip_flagged=skip_flagged)




def load_web_index(store_root: Path) -> list[dict[str, Any]]:
    """Merged index records (metadata only) from the SQLite store."""
    return dbstore.load_web_index_rows(store_root)




def flatten_web_pages(store_root: Path) -> list[dict[str, Any]]:
    """Return every page with its full text for operations that need the text layer."""
    return [
        page
        for pages in dbstore.load_web_pages(store_root).values()
        for page in pages
    ]



def rebuild_web_index(store_root: Path) -> dict[str, Any]:
    """Recompute canonical/trust/flag columns for every source from stored text."""
    from sekaisync.models import WebPage

    # Maintenance is also the right moment to backfill the walkable browse
    # index: stores created before it existed keep working but sort in SQL
    # (measured ~8s per browse on the real store). Reads must not write, so
    # this explicit entry point is where the index gets built.
    browse_index = dbstore.ensure_browse_index(store_root)
    sources: dict[str, int] = {}
    rebuilt: dict[str, int] = {}
    for source, items in dbstore.load_web_pages(store_root).items():
        pages = [web_page_from_dict(recompute_language_flags(item)) for item in items]
        merged = [{**item, **web_page_to_dict(page)} for item, page in zip(items, pages)]
        dbstore.save_web_pages_full(store_root, source, merged)
        write_category_files(
            web_category_dir(store_root, source),
            merged,
            lightweight=True,
        )
        sources[source] = len(merged)
        rebuilt[source] = len(merged)
    return {
        "sources": sources,
        "pages": sum(sources.values()),
        "index": str(dbstore.db_file(store_root)),
        "rebuilt": rebuilt,
        "browse_index": browse_index,
    }




def auxiliary_page_summary(store_root: Path) -> dict[str, Any]:
    rows = dbstore.auxiliary_summary_rows(store_root)
    sources: dict[str, int] = {}
    languages: dict[str, int] = {}
    translation_sources: dict[str, int] = {}
    trusts: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for row in rows:
        sources[row["source"]] = sources.get(row["source"], 0) + 1
        languages[row["language"]] = languages.get(row["language"], 0) + 1
        translation_sources[row["translation_source"]] = translation_sources.get(row["translation_source"], 0) + 1
        trusts[row["trust"]] = trusts.get(row["trust"], 0) + 1
        kinds[row["kind"]] = kinds.get(row["kind"], 0) + 1
    return {
        "available": bool(rows),
        "count": len(rows),
        "sources": sources,
        "languages": languages,
        "translation_sources": translation_sources,
        "trust": trusts,
        "kinds": kinds,
    }




def web_search(
    store_root: Path,
    query: str,
    source: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = 8,
    include_text: bool = False,
    include_overlay: bool = False,
    source_priority: Optional[Iterable[str]] = None,
    kind: Optional[str] = None,
    max_text_chars: int = 0,
) -> list[dict[str, Any]]:
    """Search page titles/bodies, best match first within source priority.

    Streams the scoring window out of SQLite instead of materialising every
    page with its full body first (Astra P06; the window is the same
    ``text[:20000]`` the baseline scorer used, so scores are identical).

    Ranking stays in Python on purpose: the score comes from
    ``best_match``'s Unicode-normalised fuzzy matching, and replacing it with
    ``LIKE '%query%' LIMIT K`` would change recall and source priority rather
    than speed it up. Instead a fixed-size heap keeps memory at O(limit +
    batch) while still considering **every** candidate — the scoring loop is
    still linear, and Astra's text is explicit that this must not be sold as
    an algorithmic speedup.

    ``kind`` uses exact raw-kind matching before scoring and heap selection.
    Filtering after the heap would let unrelated kinds consume the result
    limit and hide matching pages. SQL iteration remains streamed.
    """
    if limit <= 0:
        return []
    priority = tuple(source_priority or DEFAULT_SOURCE_PRIORITY)
    wanted_source = normalize_source_id(source) if source else None

    resolved = _sql_filter_values(
        store_root, wanted_source=wanted_source, kind=None
    )

    # Candidate prefilter (Astra P06 follow-up). The index proposes which rows
    # *could* match; the scorer below still decides every result and its order.
    # ``None`` means the index is unavailable, stale, or cannot answer this
    # query — all ordinary, and all resolved by scanning exactly as before.
    # Recall is unchanged rather than merely measured: see
    # ``sekaisync/searchindex.py`` for the per-tier superset argument.
    candidate_keys = searchindex.candidates(
        store_root,
        query,
        source_ids=resolved.get("source_ids"),
        language=language,
        include_overlay=include_overlay,
    )

    # (rank, -score, seq) ordering, so the heap keeps the same winners a full
    # sort would: lower source rank first, then higher score.
    heap: list[tuple[int, int, int, dict[str, Any]]] = []
    counter = itertools.count()
    for page in dbstore.iter_web_search_rows(
        store_root,
        source_ids=resolved.get("source_ids"),
        language=language,
        include_overlay=include_overlay,
        keys=candidate_keys,
    ):
        if is_derived_page(page) and not is_auxiliary_page(page):
            continue
        if is_auxiliary_page(page) and not include_overlay:
            continue
        if wanted_source and not matches_source_filter(page, wanted_source):
            continue
        if language and page.get("language") != language:
            continue
        if kind and page.get("kind") != kind:
            continue
        head = page.get("text_head") or ""
        haystack = "\n".join([page.get("title", ""), head])
        matched = best_match(query, [haystack])
        if matched is None:
            continue
        _, score = matched
        rank = source_rank(page.get("source", ""), priority)
        # Quality order is (rank asc, score desc, arrival asc) — arrival
        # ascending because the previous implementation used a *stable* sort
        # over store order, so equal-quality rows kept their store position.
        # This is a MIN-heap whose root must be the WORST entry, which means
        # the key must decrease as quality decreases:
        #   -rank     -> a worse (higher-rank) source is smaller
        #   +score    -> a worse (lower) score is smaller
        #   -arrival  -> a LATER arrival is smaller, so ties evict the newest
        #                rather than the oldest
        # The arrival sign is easy to get backwards and silent when wrong: with
        # mostly-tied scores, the result still looks reasonable while picking
        # different rows than a stable sort would.
        entry = (-rank, score, -next(counter), page)
        if len(heap) < limit:
            heapq.heappush(heap, entry)
        elif entry > heap[0]:
            # Better than the current worst of the top-K: evict the root.
            heapq.heapreplace(heap, entry)

    # Best first: invert the heap key back into quality order.
    winners = [
        entry[3]
        for entry in sorted(heap, key=lambda e: (-e[0], -e[1], -e[2]))
    ]
    items: list[dict[str, Any]] = []
    for page in winners:
        head = page.get("text_head") or ""
        item = {
            "id": page.get("id", ""),
            "source": page.get("source", ""),
            "source_type": page.get("source_type", ""),
            "instance": page.get("instance", ""),
            "url": page.get("url", ""),
            "title": page.get("title", ""),
            "language": page.get("language", ""),
            "kind": page.get("kind", ""),
            "crawled_at": page.get("crawled_at", ""),
            "snippet": _make_snippet(head, query),
            # The SQL `length(text)` value; the body itself was not selected.
            "text_length": int(page.get("text_length") or 0),
            "trust": trust_for_page(page),
            "canonical_key": canonical_key_for_page(page),
            "source_hash": page.get("source_hash", ""),
            "text_hash": page.get("text_hash") or "",
            "untranslated": bool(page.get("untranslated", False)),
            "untranslated_placeholder": page.get("untranslated_placeholder", ""),
            "original_text_hash": page.get("original_text_hash", ""),
            "asset_mismatch": page.get("asset_mismatch", ""),
            "source_last_modified": page.get("source_last_modified", ""),
            "source_etag": page.get("source_etag", ""),
            "auxiliary": bool(page.get("auxiliary", False)),
            "translation_source": page.get("translation_source", ""),
            "source_language": page.get("source_language", ""),
            "namespace": page.get("namespace", ""),
            "event_id": page.get("event_id", 0),
            "episode_no": page.get("episode_no", 0),
            "overlay": bool(page.get("overlay", False)),
        }
        items.append(item)

    if include_text and items:
        # Read one character past the budget so Core can preserve its existing
        # truncation marker without loading full long documents into Python.
        bodies = dbstore.web_page_texts(
            store_root,
            [(item.get("source", ""), item.get("id", "")) for item in items],
            max_chars=max_text_chars + 1 if max_text_chars > 0 else 0,
        )
        for item in items:
            item["text"] = bodies.get(
                (item.get("source", ""), item.get("id", "")), ""
            )
    return items


def _sql_filter_values(
    store_root: Path,
    *,
    wanted_source: Optional[str],
    kind: Optional[str],
) -> dict[str, Any]:
    """Resolve filter rules into finite value sets for SQL.

    `web_browse`'s filters are defined in Python (`matches_source_filter`
    compares a page's *backend class*, `page_category` maps many raw `kind`
    spellings onto one category).  Rather than reimplement those rules in SQL
    — which is how recall quietly changes — the distinct dimension values
    actually present in the store are enumerated (a few thousand rows at
    most, versus 752k pages) and each rule is evaluated **by calling the
    original function**.  The results are finite sets that SQL can compare
    against exactly.

    Returns ``{}`` when a filter cannot be resolved, which the caller treats
    as "do not narrow".
    """
    if not wanted_source and not kind:
        # Neither filter needs a value set.  The rules below only turn a filter
        # *into* a set of stored values, so with no filter there is nothing to
        # resolve — and the enumeration is a full 752k-row scan (measured 9.6s
        # warm, 46s cold on the real store) whose result the caller discards.
        return {}
    resolved: dict[str, Any] = {}
    with dbstore.connect(store_root) as conn:
        rows = list(
            conn.execute(
                "SELECT DISTINCT source, kind, aux_flag, derived_flag, source_type "
                "FROM web_pages"
            )
        )
    # Distinct (backend, source) pairs described the same way `page_backend` does.
    probes: list[dict[str, Any]] = []
    seen_probe: set[tuple] = set()
    for source, kind_value, aux_flag, derived_flag, source_type in rows:
        key = (source, kind_value, aux_flag, derived_flag, source_type)
        if key in seen_probe:
            continue
        seen_probe.add(key)
        probes.append(
            {
                "source": source,
                "kind": kind_value,
                "auxiliary": bool(aux_flag),
                "derived": bool(derived_flag),
                "source_type": source_type,
            }
        )

    if wanted_source:
        resolved["source_ids"] = sorted(
            {
                probe["source"]
                for probe in probes
                if matches_source_filter(probe, wanted_source)
            }
        )

    if kind:
        matching_kinds = {
            str(probe["kind"])
            for probe in probes
            if page_category(probe) == kind or str(probe["kind"]) == kind
        }
        resolved["kinds"] = sorted(matching_kinds)
    return resolved


def web_browse(
    store_root: Path,
    source: Optional[str] = None,
    language: Optional[str] = None,
    kind: Optional[str] = None,
    limit: int = 50,
    include_text: bool = False,
    include_overlay: bool = False,
    source_priority: Optional[Iterable[str]] = None,
) -> list[dict[str, Any]]:
    """Browse page metadata, newest-crawl-first within source priority.

    Filtering, ordering and the row limit are pushed into SQL so returning 20
    rows no longer reads 752k full-text rows (Astra D06; measured 50.2s → see
    the commit message).  The Python predicates below are still applied to the
    narrowed candidate set, so the result is identical to the previous
    full-scan behaviour by construction rather than by re-implementation.

    SQL narrows on exactly the conditions the Python filter uses:

    - ``NOT (derived_flag AND NOT aux_flag)`` is the derived-page exclusion
    - ``aux_flag = 0 OR include_overlay`` is the auxiliary exclusion
    - source ids and raw kinds come from :func:`_sql_filter_values`, which
      evaluates the real rule functions over the distinct stored values
    """
    priority = tuple(source_priority or DEFAULT_SOURCE_PRIORITY)
    wanted_source = normalize_source_id(source) if source else None

    resolved = _sql_filter_values(store_root, wanted_source=wanted_source, kind=kind)
    candidates = dbstore.browse_web_rows(
        store_root,
        source_ids=resolved.get("source_ids"),
        language=language,
        kinds=resolved.get("kinds"),
        limit=limit,
        include_overlay=include_overlay,
        source_priority=priority,
    )

    items: list[dict[str, Any]] = []
    for page in candidates:
        if is_derived_page(page) and not is_auxiliary_page(page):
            continue
        if is_auxiliary_page(page) and not include_overlay:
            continue
        if wanted_source and not matches_source_filter(page, wanted_source):
            continue
        if language and page.get("language") != language:
            continue
        if kind:
            if page_category(page) != kind and page.get("kind") != kind:
                continue
        # `text_head` is the SQL-side first 300 chars; fall back to `text` when
        # a caller supplied full rows (e.g. a non-SQL path).
        head = page.get("text_head")
        if head is None:
            head = str(page.get("text", ""))[:300]
        snippet = " ".join(str(head).split())
        item = {
            "id": page.get("id", ""),
            "source": page.get("source", ""),
            "source_type": page.get("source_type", ""),
            "instance": page.get("instance", ""),
            "url": page.get("url", ""),
            "title": page.get("title", ""),
            "language": page.get("language", ""),
            "kind": page.get("kind", ""),
            "crawled_at": page.get("crawled_at", ""),
            "snippet": snippet,
            # `text_length` comes from the SQL `length(text)` supplied with the
            # row. Deriving it from `page["text"]` would report 0 for every page
            # in the SQL path, since the body is deliberately not selected.
            "text_length": int(
                page.get("text_length")
                if page.get("text_length") is not None
                else len(str(page.get("text", "")))
            ),
            "trust": trust_for_page(page),
            "canonical_key": canonical_key_for_page(page),
            "source_hash": page.get("source_hash", ""),
            "text_hash": page.get("text_hash") or sha256_hex(page.get("text", "")),
            "untranslated": bool(page.get("untranslated", False)),
            "untranslated_placeholder": page.get("untranslated_placeholder", ""),
            "original_text_hash": page.get("original_text_hash", ""),
            "asset_mismatch": page.get("asset_mismatch", ""),
            "source_last_modified": page.get("source_last_modified", ""),
            "source_etag": page.get("source_etag", ""),
            "auxiliary": bool(page.get("auxiliary", False)),
            "translation_source": page.get("translation_source", ""),
            "source_language": page.get("source_language", ""),
            "namespace": page.get("namespace", ""),
            "event_id": page.get("event_id", 0),
            "episode_no": page.get("episode_no", 0),
            "overlay": bool(page.get("overlay", False)),
        }
        if include_text:
            item["text"] = page.get("text", "")
        items.append(item)
    # Stable two-pass sort: newest crawl first within a source, then source
    # priority ascending (earlier profile entries first).
    items.sort(key=lambda item: str(item.get("crawled_at", "")), reverse=True)
    items.sort(key=lambda item: source_rank(item.get("source", ""), priority))
    items = items[:limit]

    if include_text and items:
        # Bodies are fetched only for the rows actually being returned — the
        # whole point of the SQL projection is that text does not cross the
        # boundary for candidates that get filtered out or truncated.
        bodies = dbstore.web_page_texts(
            store_root,
            [(item.get("source", ""), item.get("id", "")) for item in items],
        )
        for item in items:
            item["text"] = bodies.get(
                (item.get("source", ""), item.get("id", "")), ""
            )
    return items


def _make_snippet(text: str, query: str, radius: int = 140) -> str:
    index = text.find(query)
    if index < 0:
        normalized_query = normalize_name(query)
        normalized_text = normalize_name(text[:20000])
        index = normalized_text.find(normalized_query)
    if index < 0:
        return " ".join(text[:400].split())
    start = max(0, index - radius)
    end = min(len(text), index + len(query) + radius)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + " ".join(text[start:end].split()) + suffix
