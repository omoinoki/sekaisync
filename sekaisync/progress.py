from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from sekaisync.config import DEFAULT_REGION_ORDER, REGIONS
from sekaisync.layout import (
    progress_path,
    master_source_dir,
    registry_path,
    web_index_path,
)
from sekaisync import dbstore
from sekaisync.endpoints import current_endpoints
from sekaisync.webindex import canonical_key_for_page, is_derived_page

def apply_master_base(master_base: Optional[str]) -> None:
    """Point remote master-table fetches at a configured altsource_sv base."""
    from sekaisync.endpoints import configure_endpoints

    configure_endpoints(sv_master_base=master_base)


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
    return f"{current_endpoints().ALTSOURCE_SV_MASTER_BASE}/{repo}/{table}.json"


def _default_fetcher(url: str, timeout: int = 20) -> str:
    """Bounded fallback transport for one live master-table read.

    P18: this used to call ``response.read()`` with no cap; a hostile or
    misbehaving mirror could return unbounded bytes into a progress run. The
    shared transport's byte budget now bounds it (still no unbounded read).
    """
    from sekaisync.fetcher import fetch_bytes, BUDGET_JSON_BYTES

    return fetch_bytes(
        url,
        max_bytes=BUDGET_JSON_BYTES,
        timeout=timeout,
        what="live master table",
    ).decode("utf-8", errors="replace")


def _parse_records(raw: str) -> list[dict[str, Any]]:
    data = json.loads(raw)
    if isinstance(data, dict):
        data = next((data[key] for key in ("records", "items", "data")
                     if isinstance(data.get(key), list)), None)
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise ValueError("invalid record container")
    return data


class _TableState:
    """Result of one table read for the current compute_progress request.

    P18: ``missing`` (no file / no live response), ``invalid`` (present but
    unparseable or wrongly shaped) and ``valid`` / ``valid_empty`` are
    distinct outcomes. They must not collapse into ``[]`` — an absent table
    and an empty one produce different percentages-with-unknown-denominator
    semantics, and only ``valid`` is a basis for a live answer.
    """

    __slots__ = ("state", "records", "detail")

    def __init__(self, state: str, records: list, detail: str = ""):
        self.state = state
        self.records = records
        self.detail = detail


class _RequestTables:
    """Request-scoped memo for table reads inside one compute_progress call.

    Live fetches are made at most once per (region, table) per request, so a
    single ``--live`` run neither hammers the mirror with duplicate reads nor
    mixes two different upstream snapshots into one report. Local reads share
    the same memo so every helper in one request sees one consistent table.
    """

    def __init__(
        self,
        store_root: Path,
        live: bool = False,
        fetcher: Optional[Callable[[str], str]] = None,
    ):
        self._store_root = store_root
        self._live = live
        self._fetcher = fetcher
        self._cache: dict[tuple[str, str], _TableState] = {}
        self._bases: dict[str, Path] = {}
        self.live_states: dict[str, dict[str, Any]] = {}

    def get(self, region: str, table: str) -> _TableState:
        key = (region, table.removesuffix(".json"))
        if key not in self._cache:
            self._cache[key] = self._load(region, table)
        return self._cache[key]

    def _read_local(self, path: Path) -> _TableState:
        try:
            records = _parse_records(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return _TableState("invalid", [])
        return _TableState("valid" if records else "valid_empty", records)

    def _load(self, region: str, table: str) -> _TableState:
        table = table.removesuffix(".json")
        live_error = ""
        if self._live and table == "events":
            try:
                records = _parse_records((self._fetcher or _default_fetcher)(
                    _remote_master_url(region, table)))
            except Exception as exc:
                # Never expose endpoint, proxy, credential or response details.
                live_error = "live_events_failed:" + type(exc).__name__
            else:
                self.live_states[region] = {
                    "requested_live": True,
                    "effective_source": "live_events_local_tables",
                    "degraded": False, "reason": None,
                }
                return _TableState("valid" if records else "valid_empty", records)

        if region not in self._bases:
            store_root = self._store_root
            self._bases[region] = master_source_dir(store_root, region)
        base = self._bases[region]
        direct = base / f"{table}.json"
        paths = [direct] if direct.is_file() else sorted(base.glob(f"**/{table}.json"))
        state = self._read_local(paths[0]) if paths else _TableState("missing", [])
        if table == "events":
            known = state.state in {"valid", "valid_empty"}
            self.live_states[region] = {
                "requested_live": self._live,
                "effective_source": "local" if known else "unknown",
                "degraded": bool(live_error),
                "reason": live_error or (None if known else "local_events_" + state.state),
            }
        return state


def _load_records(
    store_root: Path, region: str, table: str, live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
    tables: Optional[_RequestTables] = None,
) -> list[dict[str, Any]]:
    return (tables or _RequestTables(store_root, live, fetcher)).get(region, table).records


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
    tables: Optional[_RequestTables] = None,
) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {category: set() for category in FACT_TABLES}
    for category, (table, date_field) in FACT_TABLES.items():
        for record in _load_records(store_root, region, table, live=live, fetcher=fetcher, tables=tables):
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
    for entity_id, entity_type, regions, entity_region in dbstore.load_entity_keys(store_root):
        if region not in regions and entity_region != region:
            continue
        if entity_type not in expected:
            continue
        suffix = str(entity_id).rsplit(":", 1)[-1]
        key = f"{entity_type}:{suffix}"
        if key in expected[entity_type]:
            matched[entity_type].add(key)
    return matched


def expected_text_units(
    store_root: Path,
    region: str,
    now_ms: int,
    live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
    tables: Optional[_RequestTables] = None,
) -> dict[str, set[str]]:
    expected: dict[str, set[str]] = {category: set() for category in TEXT_TABLES}
    language = REGIONS[region].language if region in REGIONS else "unknown"

    events = _load_records(store_root, region, "events", live=live, fetcher=fetcher, tables=tables)
    released_events = {
        str(record.get("id"))
        for record in events
        if _is_released(record, "startAt", now_ms, require_date=True)
    }
    for story in _load_records(store_root, region, "eventStories", tables=tables):
        event_id = story.get("eventId") or story.get("id")
        if event_id is None or str(event_id) not in released_events:
            continue
        for episode in story.get("eventStoryEpisodes") or []:
            episode_no = episode.get("episodeNo")
            if episode_no is not None:
                expected["event_story"].add(f"event_story:{language}:{event_id}:{episode_no}")

    unit_counts: dict[str, int] = {}
    for unit in _load_records(store_root, region, "unitStories", tables=tables):
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
        for record in _load_records(store_root, region, "cards", tables=tables)
        if _is_released(record, "releaseAt", now_ms, require_date=True)
    }
    for episode in _load_records(store_root, region, "cardEpisodes", tables=tables):
        card_id = episode.get("cardId")
        if card_id is not None and str(card_id) in released_cards:
            expected["card_story"].add(f"card_story:{language}:{episode.get('id')}")

    for record in _load_records(store_root, region, "specialStories", tables=tables):
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

    for record in _load_records(store_root, region, "virtualLives", tables=tables):
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

    for record in _load_records(store_root, region, "actionSets", tables=tables):
        scenario_id = record.get("scenarioId")
        if scenario_id:
            expected["area_talk"].add(f"area_talk:{language}:{scenario_id}")

    for record in _load_records(store_root, region, "characterProfiles", tables=tables):
        scenario_id = record.get("scenarioId")
        if scenario_id:
            expected["self_intro"].add(f"self_intro:{language}:{scenario_id}")

    for record in _load_records(store_root, region, "characterArchiveVoices", tables=tables):
        # ~53% of these records carry an empty ``displayPhrase`` — the source
        # ships the voice entry but no text for it.  The crawler cannot invent
        # that text, so counting them as expected units charges the store for
        # something the publisher never provides.  They leave the denominator
        # and are reported separately (:func:`source_unavailable_units`);
        # records that do carry text stay in.
        if record.get("id") is None:
            continue
        if not str(record.get("displayPhrase") or "").strip():
            continue
        expected["home_line"].add(f"home_line:{language}:{record.get('id')}")

    for record in _load_records(store_root, region, "mysekaiCharacterTalks", tables=tables):
        # Overseas asset buckets ship no mysekai/talk Lua, so these rows have
        # no text to crawl in en/tc/kr/cn — only jp publishes MySekai dialogue
        # (``data_gaps: mysekai_overseas``).  Keeping them would report those
        # four regions as 0% on a category nothing can ever fill; they leave
        # the denominator and are reported in ``source_unavailable``.
        if region != "jp":
            continue
        if record.get("id") is not None:
            expected["mysekai_talk"].add(f"mysekai_talk:{language}:{record.get('id')}")

    for record in _load_records(store_root, region, "mysekaiCharacterTalkTweets", tables=tables):
        if record.get("id") is not None:
            expected["mysekai_tweet"].add(f"mysekai_tweet:{language}:{record.get('id')}")

    return expected


def excluded_units(
    store_root: Path,
    region: str,
    now_ms: int,
    live: bool = False,
    fetcher: Optional[Callable[[str], str]] = None,
    tables: Optional[_RequestTables] = None,
) -> dict[str, int]:
    excluded: dict[str, int] = {}
    for category, (table, date_field) in FACT_TABLES.items():
        if date_field is None:
            continue
        records = _load_records(store_root, region, table, live=live, fetcher=fetcher, tables=tables)
        excluded[f"fact_{category}"] = sum(
            1
            for record in records
            if not _is_released(record, date_field, now_ms, require_date=True)
        )

    events = _load_records(store_root, region, "events", live=live, fetcher=fetcher, tables=tables)
    unreleased_events = {
        str(record.get("id"))
        for record in events
        if not _is_released(record, "startAt", now_ms, require_date=True)
    }
    event_episode_count = 0
    for story in _load_records(store_root, region, "eventStories", tables=tables):
        event_id = story.get("eventId") or story.get("id")
        if str(event_id) not in unreleased_events:
            continue
        event_episode_count += len(story.get("eventStoryEpisodes") or [])
    excluded["text_event_story"] = event_episode_count

    cards = _load_records(store_root, region, "cards", tables=tables)
    unreleased_cards = {
        str(record.get("id"))
        for record in cards
        if not _is_released(record, "releaseAt", now_ms, require_date=True)
    }
    excluded["text_card_story"] = sum(
        1
        for episode in _load_records(store_root, region, "cardEpisodes", tables=tables)
        if str(episode.get("cardId")) in unreleased_cards
    )

    excluded["text_special_story"] = sum(
        1
        for record in _load_records(store_root, region, "specialStories", tables=tables)
        if not _is_released(record, "startAt", now_ms, require_date=True)
    )
    excluded["text_virtual_live"] = sum(
        1
        for record in _load_records(store_root, region, "virtualLives", tables=tables)
        if not _is_released(record, "startAt", now_ms, require_date=True)
    )
    return excluded


def source_unavailable_units(
    store_root: Path,
    region: str,
    tables: Optional[_RequestTables] = None,
) -> dict[str, int]:
    """Units the source is confirmed not to provide, per category.

    Distinct from :func:`excluded_units`, which drops *future* content: these
    units are in the past and permanently unfetchable, so keeping them in the
    denominator would report a store as incomplete for text that does not
    exist upstream.  Every entry here is backed by an observation recorded in
    ``Core.data_gaps``; the number is the count of affected catalog rows, not
    a guess.

    Currently detected mechanically (no per-region manual list):

    * ``home_line`` — ``characterArchiveVoices`` rows whose ``displayPhrase``
      is empty.  The voice entry exists but the publisher ships no text; the
      jp table is ~53% empty.
    * ``mysekai_talk`` — overseas regions have no ``mysekai/talk`` Lua at all;
      only jp publishes MySekai dialogue (``data_gaps: mysekai_overseas``).
      jp is unaffected, so it reports 0 there.
    """
    unavailable: dict[str, int] = {}
    empty_home_lines = sum(
        1
        for record in _load_records(store_root, region, "characterArchiveVoices", tables=tables)
        if record.get("id") is not None
        and not str(record.get("displayPhrase") or "").strip()
    )
    if empty_home_lines:
        unavailable["text_home_line"] = empty_home_lines
    if region != "jp":
        # Overseas asset buckets carry no mysekai/talk Lua; the expected units
        # for this category are therefore source-absent, not merely missing.
        mysekai_talk = sum(
            1
            for record in _load_records(store_root, region, "mysekaiCharacterTalks", tables=tables)
            if record.get("id") is not None
        )
        if mysekai_talk:
            unavailable["text_mysekai_talk"] = mysekai_talk
    return unavailable


def _web_text_key(page: dict[str, Any]) -> Optional[str]:
    return canonical_key_for_page(page) or None


def matched_text_units(
    store_root: Path,
    region: str,
    expected: dict[str, set[str]],
) -> dict[str, set[str]]:
    matched: dict[str, set[str]] = {category: set() for category in TEXT_TABLES}
    language = REGIONS[region].language if region in REGIONS else "zh_hans"
    keys = dbstore.matched_text_keys(store_root, language)
    for key in keys:
        category = key.split(":", 1)[0]
        if category in expected and key in expected[category]:
            matched[category].add(key)
    return matched


def _integer_percent(matched: int, expected: int) -> Optional[int]:
    if expected <= 0:
        return None
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
    if live and not current_endpoints().ALTSOURCE_SV_MASTER_BASE:
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
    overall_source_unavailable_total = 0

    tables = _RequestTables(store_root, live, fetcher)
    for region in selected:
        events = tables.get(region, "events").records
        fact_expected = expected_fact_units(store_root, region, now_ms, tables=tables)
        fact_matched = matched_fact_units(store_root, region, fact_expected)
        text_expected = expected_text_units(store_root, region, now_ms, tables=tables)
        text_matched = matched_text_units(store_root, region, text_expected)
        excluded = excluded_units(store_root, region, now_ms, tables=tables)
        region_excluded_total = sum(excluded.values())
        overall_excluded_total += region_excluded_total
        # Confirmed-unavailable units are counted, not subtracted again: the
        # expected-unit builders above already skip them (see
        # ``expected_text_units``'s home_line branch), so this number explains
        # the gap the denominator would otherwise carry rather than changing
        # the arithmetic a second time.
        unavailable = source_unavailable_units(store_root, region, tables=tables)
        region_unavailable_total = sum(unavailable.values())
        overall_source_unavailable_total += region_unavailable_total

        fact_scores = _category_scores(fact_expected, fact_matched)
        text_scores = _category_scores(text_expected, text_matched)
        combined_expected = fact_scores["expected_total"] + text_scores["expected_total"]
        combined_matched = fact_scores["matched_total"] + text_scores["matched_total"]
        table_states = {
            table: {"state": state.state}
            for (key_region, table), state in tables._cache.items() if key_region == region
        }
        incomplete = any(row["state"] in {"missing", "invalid"} for row in table_states.values())
        for scores, dependencies in (
            (fact_scores, {key: {table} for key, (table, _) in FACT_TABLES.items()}),
            (text_scores, {key: {table} | ({"events"} if key == "event_story" else
                                         {"cards"} if key == "card_story" else set())
                           for key, table in TEXT_TABLES.items()}),
        ):
            for category, score in scores["categories"].items():
                complete = all(table_states[table]["state"] in {"valid", "valid_empty"}
                               for table in dependencies[category])
                score["denominator_complete"] = complete
                score["known_subset_pct"] = score["pct"]
                if not complete:
                    score["pct"] = None
            scores["denominator_complete"] = all(
                score["denominator_complete"] for score in scores["categories"].values())
            scores["known_subset_pct"] = scores["pct"]
            if not scores["denominator_complete"]:
                scores["pct"] = None
        region_results[region] = {
            "live_state": tables.live_states[region],
            "table_states": table_states,
            "denominator_complete": not incomplete,
            "language": REGIONS[region].language,
            "activity": _activity_progress(events, now_ms),
            "fact": fact_scores,
            "text": text_scores,
            "excluded_units": excluded,
            "excluded_units_total": region_excluded_total,
            "source_unavailable_units": unavailable,
            "source_unavailable_units_total": region_unavailable_total,
            "overall": {
                "expected_units": combined_expected,
                "matched_units": combined_matched,
                "pct": None if incomplete else _integer_percent(combined_matched, combined_expected),
                "known_subset_pct": _integer_percent(combined_matched, combined_expected),
            },
        }
        overall_fact_expected += fact_scores["expected_total"]
        overall_fact_matched += fact_scores["matched_total"]
        overall_text_expected += text_scores["expected_total"]
        overall_text_matched += text_scores["matched_total"]

    combined_expected = overall_fact_expected + overall_text_expected
    combined_matched = overall_fact_matched + overall_text_matched
    fact_complete = bool(region_results) and all(row["fact"]["denominator_complete"] for row in region_results.values())
    text_complete = bool(region_results) and all(row["text"]["denominator_complete"] for row in region_results.values())
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "now_ms": now_ms,
        "live": live,  # compatibility: requested, not proof of live success
        "requested_live": live,
        "live_degraded": any(row["live_state"]["degraded"] for row in region_results.values()),
        "regions": region_results,
        "overall": {
            "fact": {
                "expected_units": overall_fact_expected,
                "matched_units": overall_fact_matched,
                "denominator_complete": fact_complete,
                "pct": _integer_percent(overall_fact_matched, overall_fact_expected) if fact_complete else None,
                "known_subset_pct": _integer_percent(overall_fact_matched, overall_fact_expected),
            },
            "text": {
                "expected_units": overall_text_expected,
                "matched_units": overall_text_matched,
                "denominator_complete": text_complete,
                "pct": _integer_percent(overall_text_matched, overall_text_expected) if text_complete else None,
                "known_subset_pct": _integer_percent(overall_text_matched, overall_text_expected),
            },
            "expected_units": combined_expected,
            "matched_units": combined_matched,
            "excluded_units_total": overall_excluded_total,
            "source_unavailable_units_total": overall_source_unavailable_total,
            "denominator_complete": fact_complete and text_complete,
            "pct": _integer_percent(combined_matched, combined_expected) if fact_complete and text_complete else None,
            "known_subset_pct": _integer_percent(combined_matched, combined_expected),
        },
        "source_unavailable": {
            "total_units": overall_source_unavailable_total,
            "note": (
                "Units the publisher is confirmed not to provide, already left out of "
                "the denominator above: they are neither matched nor expected, so the "
                "reported percentage measures what is actually obtainable. Reported "
                "here so the exclusion stays visible and auditable."
            ),
            "by_region": {
                region: row["source_unavailable_units"]
                for region, row in region_results.items()
                if row["source_unavailable_units"]
            },
            "reasons": {
                "text_home_line": (
                    "characterArchiveVoices rows whose displayPhrase is empty — the "
                    "voice entry exists but the source ships no text (about 53% of the "
                    "table). See Core.data_gaps: home_line."
                ),
                "text_mysekai_talk": (
                    "Overseas asset buckets carry no mysekai/talk Lua, so only JP "
                    "publishes MySekai dialogue. See Core.data_gaps: mysekai_overseas."
                ),
            },
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
