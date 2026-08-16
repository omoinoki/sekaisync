from __future__ import annotations

import json
import re
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sekaisync.config import DEFAULT_REGION_ORDER, REGIONS, ViewerSettings
from sekaisync.layout import (
    progress_path,
    region_master_dir,
    registry_path,
    web_index_path,
)
from sekaisync.registry import load_registry
from sekaisync.webindex import canonical_key_for_page, is_derived_page

# altsource_sv (Sekai Viewer) master endpoint; overridable via settings.json.
_SV_MASTER_BASE = ViewerSettings().master_base


def apply_master_base(master_base: Optional[str]) -> None:
    """Point remote master-table fetches at a configured altsource_sv base."""
    global _SV_MASTER_BASE
    _SV_MASTER_BASE = str(master_base or "").rstrip("/") or ViewerSettings().master_base


FACT_TABLES: dict[str, tuple[str, Optional[str]]] = {
    "card": ("cards", "releaseAt"),
    "song": ("musics", "publishedAt"),
    "event": ("events", "startAt"),
    "gacha": ("gachas", "startAt"),
    "virtual_live": ("virtualLives", "startAt"),
    "area": ("areas", None),
    "stamp": ("stamps", None),
    "character": ("gameCharacters", None),
    "character_unit": ("gameCharacterUnits", None),
}

TEXT_TABLES: dict[str, str] = {
    "event_story": "eventStories",
    "unit_story": "unitStories",
    "card_story": "cardEpisodes",
    "special_story": "specialStories",
    "virtual_live": "virtualLives",
    "area_talk": "actionSets",
    "self_intro": "characterProfiles",
    "home_line": "characterArchiveVoices",
    "mysekai_talk": "mysekaiCharacterTalks",
    "mysekai_tweet": "mysekaiCharacterTalkTweets",
}


def _remote_master_url(region: str, table: str) -> str:
    region_info = REGIONS.get(region)
    repo = (region_info.repo_slug or "Sekai-World/sekai-master-db-diff").rsplit("/", 1)[-1]
    return f"{_SV_MASTER_BASE}/{repo}/{table}.json"


def _default_fetcher(url: str, timeout: int = 20) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "SekaiSync/0.3 (+local progress)",
            "Cache-Control": "no-cache",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def _load_records(
    store_root: Path,
    region: str,
    table: str,
    live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
) -> list[dict[str, Any]]:
    if live and table == "events":
        fetch = fetcher or _default_fetcher
        try:
            data = json.loads(fetch(_remote_master_url(region, table)))
            if isinstance(data, dict):
                for key in ("records", "items", "data"):
                    if isinstance(data.get(key), list):
                        data = data[key]
                        break
                else:
                    data = []
            if isinstance(data, list):
                return [item for item in data if isinstance(item, dict)]
        except Exception:
            pass

    base = region_master_dir(store_root, region)
    table = table.removesuffix(".json")
    candidates = [
        base / f"{table}.json",
        base / "*" / f"{table}.json",
        base / "**" / f"{table}.json",
        base / "master" / f"{table}.json",
        base / "db" / f"{table}.json",
        base / "source" / f"{table}.json",
    ]
    path = None
    for pattern in candidates:
        matches = sorted(base.glob(str(pattern.relative_to(base))))
        if matches:
            path = matches[0]
            break
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if isinstance(data, dict):
        for key in ("records", "items", "data"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
        else:
            return []
    return [item for item in data if isinstance(item, dict)]


def _now_ms(now: Optional[float] = None) -> int:
    if now is None:
        return int(time.time() * 1000)
    return int(now * 1000) if now < 1_000_000_000_000 else int(now)


def _timestamp_ms(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_released(
    record: dict[str, Any],
    field: Optional[str],
    now_ms: int,
    require_date: bool = False,
) -> bool:
    if field is None:
        return True
    value = _timestamp_ms(record.get(field))
    if value is None:
        return not require_date
    return value <= now_ms


def expected_fact_units(
    store_root: Path,
    region: str,
    now_ms: int,
    live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {category: set() for category in FACT_TABLES}
    for category, (table, date_field) in FACT_TABLES.items():
        for record in _load_records(store_root, region, table, live=live, fetcher=fetcher):
            unit_id = record.get("id")
            if unit_id is None:
                continue
            if _is_released(
                record,
                date_field,
                now_ms,
                require_date=date_field is not None,
            ):
                expected[category].add(f"{category}:{unit_id}")
    return expected


def matched_fact_units(
    store_root: Path,
    region: str,
    expected: dict[str, set[str]],
) -> dict[str, set[str]]:
    matched: dict[str, set[str]] = {category: set() for category in FACT_TABLES}
    for entity in load_registry(registry_path(store_root)):
        if region not in entity.regions and entity.region != region:
            continue
        if entity.type not in expected:
            continue
        suffix = str(entity.id).rsplit(":", 1)[-1]
        key = f"{entity.type}:{suffix}"
        if key in expected[entity.type]:
            matched[entity.type].add(key)
    return matched


def expected_text_units(
    store_root: Path,
    region: str,
    now_ms: int,
    live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {category: set() for category in TEXT_TABLES}
    language = REGIONS[region].language if region in REGIONS else "unknown"

    events = _load_records(store_root, region, "events", live=live, fetcher=fetcher)
    released_events = {
        str(record.get("id"))
        for record in events
        if _is_released(record, "startAt", now_ms, require_date=True)
    }
    for story in _load_records(store_root, region, "eventStories"):
        event_id = story.get("eventId") or story.get("id")
        if event_id is None or str(event_id) not in released_events:
            continue
        for episode in story.get("eventStoryEpisodes") or []:
            episode_no = episode.get("episodeNo")
            if episode_no is not None:
                expected["event_story"].add(f"event_story:{language}:{event_id}:{episode_no}")

    unit_counts: dict[str, int] = {}
    for unit in _load_records(store_root, region, "unitStories"):
        unit_key = unit.get("unit")
        for chapter in unit.get("chapters") or []:
            for episode in chapter.get("episodes") or []:
                scenario_id = episode.get("scenarioId")
                if not scenario_id:
                    continue
                label = str(episode.get("episodeNoLabel") or "")
                if episode.get("episodeNo") == 1 or label in {"序章", "オープニング"}:
                    continue
                unit_counts[unit_key] = unit_counts.get(unit_key, 0) + 1
                if unit_counts[unit_key] > 20:
                    continue
                expected["unit_story"].add(f"unit_story:{language}:{scenario_id}")

    released_cards = {
        str(record.get("id"))
        for record in _load_records(store_root, region, "cards")
        if _is_released(record, "releaseAt", now_ms, require_date=True)
    }
    for episode in _load_records(store_root, region, "cardEpisodes"):
        card_id = episode.get("cardId")
        if card_id is not None and str(card_id) in released_cards:
            expected["card_story"].add(f"card_story:{language}:{episode.get('id')}")

    for record in _load_records(store_root, region, "specialStories"):
        if not _is_released(record, "startAt", now_ms, require_date=True):
            continue
        for episode in record.get("episodes") or []:
            if not isinstance(episode, dict):
                continue
            scenario_id = episode.get("scenarioId")
            if not scenario_id:
                continue
            expected["special_story"].add(
                f"special_story:{language}:{episode.get('id') or scenario_id}"
            )

    for record in _load_records(store_root, region, "virtualLives"):
        if not _is_released(record, "startAt", now_ms, require_date=True):
            continue
        for setlist in record.get("virtualLiveSetlists") or []:
            if not isinstance(setlist, dict):
                continue
            if setlist.get("virtualLiveSetlistType") not in {"mc", "mc_timeline"}:
                continue
            if not setlist.get("assetbundleName"):
                continue
            expected["virtual_live"].add(
                f"virtual_live:{language}:{setlist.get('id') or record.get('id')}"
            )

    for record in _load_records(store_root, region, "actionSets"):
        scenario_id = record.get("scenarioId")
        if scenario_id:
            expected["area_talk"].add(f"area_talk:{language}:{scenario_id}")

    for record in _load_records(store_root, region, "characterProfiles"):
        scenario_id = record.get("scenarioId")
        if scenario_id:
            expected["self_intro"].add(f"self_intro:{language}:{scenario_id}")

    for record in _load_records(store_root, region, "characterArchiveVoices"):
        if record.get("id") is not None:
            expected["home_line"].add(f"home_line:{language}:{record.get('id')}")

    for record in _load_records(store_root, region, "mysekaiCharacterTalks"):
        if record.get("id") is not None:
            expected["mysekai_talk"].add(f"mysekai_talk:{language}:{record.get('id')}")

    for record in _load_records(store_root, region, "mysekaiCharacterTalkTweets"):
        if record.get("id") is not None:
            expected["mysekai_tweet"].add(f"mysekai_tweet:{language}:{record.get('id')}")

    return expected


def excluded_units(
    store_root: Path,
    region: str,
    now_ms: int,
    live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
) -> dict[str, int]:
    excluded: dict[str, int] = {}
    for category, (table, date_field) in FACT_TABLES.items():
        if date_field is None:
            continue
        records = _load_records(store_root, region, table, live=live, fetcher=fetcher)
        excluded[f"fact_{category}"] = sum(
            1
            for record in records
            if not _is_released(record, date_field, now_ms, require_date=True)
        )

    events = _load_records(store_root, region, "events", live=live, fetcher=fetcher)
    unreleased_events = {
        str(record.get("id"))
        for record in events
        if not _is_released(record, "startAt", now_ms, require_date=True)
    }
    event_episode_count = 0
    for story in _load_records(store_root, region, "eventStories"):
        event_id = story.get("eventId") or story.get("id")
        if str(event_id) not in unreleased_events:
            continue
        event_episode_count += len(story.get("eventStoryEpisodes") or [])
    excluded["text_event_story"] = event_episode_count

    cards = _load_records(store_root, region, "cards")
    unreleased_cards = {
        str(record.get("id"))
        for record in cards
        if not _is_released(record, "releaseAt", now_ms, require_date=True)
    }
    excluded["text_card_story"] = sum(
        1
        for episode in _load_records(store_root, region, "cardEpisodes")
        if str(episode.get("cardId")) in unreleased_cards
    )

    excluded["text_special_story"] = sum(
        1
        for record in _load_records(store_root, region, "specialStories")
        if not _is_released(record, "startAt", now_ms, require_date=True)
    )
    excluded["text_virtual_live"] = sum(
        1
        for record in _load_records(store_root, region, "virtualLives")
        if not _is_released(record, "startAt", now_ms, require_date=True)
    )
    return excluded


def _web_text_key(page: dict[str, Any]) -> Optional[str]:
    return canonical_key_for_page(page) or None


def matched_text_units(
    store_root: Path,
    region: str,
    expected: dict[str, set[str]],
) -> dict[str, set[str]]:
    matched: dict[str, set[str]] = {category: set() for category in TEXT_TABLES}
    language = REGIONS[region].language if region in REGIONS else "zh_hans"
    index_path = web_index_path(store_root)
    if not index_path.exists():
        return matched
    data = json.loads(index_path.read_text(encoding="utf-8"))
    for page in data.get("pages", []):
        if is_derived_page(page):
            continue
        if page.get("asset_mismatch") or page.get("content_language_mismatch"):
            continue
        if page.get("untranslated"):
            continue
        if page.get("language") != language:
            continue
        key = _web_text_key(page)
        if not key:
            continue
        category = key.split(":", 1)[0]
        if category in expected and key in expected[category]:
            matched[category].add(key)
    return matched


def _integer_percent(matched: int, expected: int) -> int:
    if expected <= 0:
        return 0
    return int(round(100 * matched / expected))


def _iso(value: Any) -> Optional[str]:
    ts = _timestamp_ms(value)
    if ts is None:
        return None
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).isoformat()


def _activity_progress(
    events: list[dict[str, Any]],
    now_ms: int,
) -> dict[str, Any]:
    released = 0
    upcoming = 0
    active: list[dict[str, Any]] = []
    for record in events:
        start = _timestamp_ms(record.get("startAt"))
        if start is None:
            released += 1
            continue
        if start <= now_ms:
            released += 1
            end = _timestamp_ms(record.get("closedAt") or record.get("endAt"))
            if end is None or end >= now_ms:
                active.append(record)
        else:
            upcoming += 1
    active.sort(key=lambda item: _timestamp_ms(item.get("startAt")) or 0, reverse=True)
    current = active[0] if active else None
    return {
        "current_event": (
            {
                "id": current.get("id"),
                "name": current.get("name"),
                "start_at": _iso(current.get("startAt")),
                "closed_at": _iso(current.get("closedAt") or current.get("endAt")),
            }
            if current
            else None
        ),
        "released_events": released,
        "upcoming_events": upcoming,
    }


def _category_scores(expected: dict[str, set[str]], matched: dict[str, set[str]]) -> dict[str, Any]:
    counts = {}
    total_expected = 0
    total_matched = 0
    for category in sorted(set(expected) | set(matched)):
        expected_count = len(expected.get(category, set()))
        matched_count = len(matched.get(category, set()))
        total_expected += expected_count
        total_matched += matched_count
        counts[category] = {
            "expected": expected_count,
            "matched": matched_count,
            "pct": _integer_percent(matched_count, expected_count),
        }
    return {
        "categories": counts,
        "expected_total": total_expected,
        "matched_total": total_matched,
        "pct": _integer_percent(total_matched, total_expected),
    }


def compute_progress(
    store_root: Path,
    regions: Optional[Iterable[str]] = None,
    now: Optional[float] = None,
    live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    if live and not _SV_MASTER_BASE:
        raise ValueError(
            "No Sekai Viewer master_base configured. Configure it in settings.json "
            "(see README「配置数据源」) before using --live progress."
        )
    selected = tuple(regions) if regions is not None else DEFAULT_REGION_ORDER
    selected = [region for region in selected if region in REGIONS]
    now_ms = _now_ms(now)
    region_results: dict[str, Any] = {}
    overall_fact_expected = 0
    overall_fact_matched = 0
    overall_text_expected = 0
    overall_text_matched = 0
    overall_excluded_total = 0

    for region in selected:
        events = _load_records(store_root, region, "events", live=live, fetcher=fetcher)
        fact_expected = expected_fact_units(store_root, region, now_ms, live=live, fetcher=fetcher)
        fact_matched = matched_fact_units(store_root, region, fact_expected)
        text_expected = expected_text_units(store_root, region, now_ms, live=live, fetcher=fetcher)
        text_matched = matched_text_units(store_root, region, text_expected)
        excluded = excluded_units(store_root, region, now_ms, live=live, fetcher=fetcher)
        region_excluded_total = sum(excluded.values())
        overall_excluded_total += region_excluded_total

        fact_scores = _category_scores(fact_expected, fact_matched)
        text_scores = _category_scores(text_expected, text_matched)
        combined_expected = fact_scores["expected_total"] + text_scores["expected_total"]
        combined_matched = fact_scores["matched_total"] + text_scores["matched_total"]
        region_results[region] = {
            "language": REGIONS[region].language,
            "activity": _activity_progress(events, now_ms),
            "fact": fact_scores,
            "text": text_scores,
            "excluded_units": excluded,
            "excluded_units_total": region_excluded_total,
            "overall": {
                "expected_units": combined_expected,
                "matched_units": combined_matched,
                "pct": _integer_percent(combined_matched, combined_expected),
            },
        }
        overall_fact_expected += fact_scores["expected_total"]
        overall_fact_matched += fact_scores["matched_total"]
        overall_text_expected += text_scores["expected_total"]
        overall_text_matched += text_scores["matched_total"]

    combined_expected = overall_fact_expected + overall_text_expected
    combined_matched = overall_fact_matched + overall_text_matched
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "now_ms": now_ms,
        "live": live,
        "regions": region_results,
        "overall": {
            "fact": {
                "expected_units": overall_fact_expected,
                "matched_units": overall_fact_matched,
                "pct": _integer_percent(overall_fact_matched, overall_fact_expected),
            },
            "text": {
                "expected_units": overall_text_expected,
                "matched_units": overall_text_matched,
                "pct": _integer_percent(overall_text_matched, overall_text_expected),
            },
            "expected_units": combined_expected,
            "matched_units": combined_matched,
            "excluded_units_total": overall_excluded_total,
            "pct": _integer_percent(combined_matched, combined_expected),
        },
        "caveat": (
            "JP and overseas servers are roughly one year apart, but collab-style "
            "schedules can be shared across regions; treat the gap as reference only. "
            "Future content beyond the current released schedule is excluded."
        ),
    }


def save_progress(store_root: Path, data: dict[str, Any]) -> Path:
    path = progress_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
