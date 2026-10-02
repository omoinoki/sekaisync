"""Read-only corpus census, release evidence, and a candidate-free blind holdout.

The production query streams metadata, not every page's body. All source variants
remain addressable in the separate census index. Only frozen benchmark pages are
read in full, and their text remains in the local research output directory.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.parse import unquote, urlsplit


LANGUAGES = ("ja", "en", "zh_hans", "zh_hant", "ko")
REGIONS = {"ja": "jp", "en": "en", "zh_hans": "cn", "zh_hant": "tc", "ko": "kr"}
ALIASES = {
    "jp": "ja", "ja-jp": "ja", "ja_jp": "ja", "en-us": "en",
    "en_us": "en", "cn": "zh_hans", "zh-cn": "zh_hans", "zh_cn": "zh_hans",
    "tc": "zh_hant", "zh_tw": "zh_hant", "zh-tw": "zh_hant", "zh_hk": "zh_hant",
    "kr": "ko", "ko-kr": "ko", "ko_kr": "ko",
}
NARRATIVE_KINDS = {
    "event_story", "unit_story", "card_story", "special_story", "area_talk",
    "virtual_live", "home_line", "self_intro", "mysekai_talk", "mysekai_tweet",
}
SYSTEM_KINDS = {"wordings", "tips", "systemLive2ds", "title_overlay"}
EVENT_STORY = re.compile(r"^event_story:(\d+):(\d+)$")
CARD_STORY = re.compile(r"^card_story:(\d+)$")
OPAQUE_ID = re.compile(r"^[0-9a-f]{12,64}$")
ROOT = Path(__file__).resolve().parents[1]


def normalize_language(value: str) -> str:
    value = value.strip().lower()
    return ALIASES.get(value, value)


def content_family(story_key: str) -> str:
    parts = story_key.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else story_key


def logical_identity(page: dict) -> tuple[str, str]:
    kind = page["kind"]
    canonical = page.get("canonical_key", "")
    parts = canonical.split(":", 2)
    if len(parts) == 3 and parts[0] == kind and normalize_language(parts[1]) in LANGUAGES:
        return kind + ":" + parts[2], "canonical"
    parts = page["id"].split(":", 4)
    if len(parts) == 5 and parts[0] == "web" and parts[3] == kind:
        tail = parts[4]
        suffix = tail.rsplit(":", 1)
        if len(suffix) == 2 and normalize_language(suffix[1]) == page["language"] and (
                page["source"].endswith("_translation") or _flag(page.get("overlay", ""))):
            return kind + ":" + suffix[0], "auxiliary_locale_suffix"
        if OPAQUE_ID.fullmatch(tail) and kind not in NARRATIVE_KINDS:
            # Content-derived IDs cannot establish cross-language record identity.
            return kind + ":opaque:" + page["source"] + ":" + page["language"] + ":" + tail, "opaque_source_id"
        return kind + ":" + tail, "page_id"
    return kind + ":unresolved:" + page["source"] + ":" + page["id"], "unresolved"


def _flag(value) -> bool:
    return str(value).strip().lower() not in {"", "0", "none", "null", "false", "[]", "{}"}


def bad_page_reasons(page: dict) -> list[str]:
    reasons = []
    if not page.get("char_count", 0):
        reasons.append("empty_text")
    if normalize_language(page["language"]) not in LANGUAGES:
        reasons.append("unsupported_language")
    for field in ("untranslated", "untranslated_placeholder", "asset_mismatch",
                  "scenario_id_mismatch", "content_language_mismatch"):
        if _flag(page.get(field, "")):
            reasons.append(field)
    return reasons


def open_read_only(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("BEGIN")
    return conn


def _write_json(path: Path, value) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def baseline_stories(path: Path) -> list[str]:
    # Never load annotations or scoring state; only task identities define leakage exclusions.
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return sorted({key for task in manifest["tasks"] for key in task.get("story_keys", [])})


class RawTables:
    def __init__(self):
        self.cache: dict[str, dict] = {}

    def load(self, path: Path, expected: str | None = None) -> dict:
        key = str(path.resolve())
        if key not in self.cache:
            if not path.is_file():
                self.cache[key] = {"path": key, "error": "missing_raw_file"}
            else:
                raw = path.read_bytes()
                digest = hashlib.sha256(raw).hexdigest()
                try:
                    records = json.loads(raw)
                    if not isinstance(records, list) or any(not isinstance(x, dict) for x in records):
                        raise ValueError("master table must be an array of objects")
                    self.cache[key] = {"path": key, "sha256": digest,
                                       "records": records, "by_id": {str(x.get("id")): x for x in records}}
                except (ValueError, UnicodeError) as exc:
                    self.cache[key] = {"path": key, "sha256": digest, "error": "invalid_raw_json",
                                       "detail": str(exc)}
        result = self.cache[key]
        if expected and result.get("sha256") != expected:
            return {"path": key, "sha256": result.get("sha256"), "expected_sha256": expected,
                    "error": "raw_hash_mismatch"}
        return result

    @staticmethod
    def evidence(table: dict) -> dict:
        return {key: value for key, value in table.items() if key not in {"records", "by_id"}}


class ReleaseVerifier:
    """Verify scheduled publication in local master data, not live server operation."""

    def __init__(self, conn: sqlite3.Connection, as_of: datetime):
        self.conn = conn
        self.as_of_ms = int(as_of.timestamp() * 1000)
        self.tables = RawTables()
        self.events: dict[tuple[int, str], dict] = {}
        self.facts_available = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_region_facts'"
        ).fetchone())

    def _resolve(self, value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else ROOT / path

    def _event(self, event_id: int, region: str) -> dict:
        key = event_id, region
        if key in self.events:
            return self.events[key]
        result = {"event_id": event_id, "region": region, "status": "unknown_release", "reasons": []}
        self.events[key] = result
        if not self.facts_available:
            result["reasons"].append("missing_region_facts_table")
            return result
        row = self.conn.execute(
            "SELECT facts_json,retrieval_json,source,version FROM entity_region_facts WHERE entity_id=? AND region=?",
            (f"event:{event_id}", region),
        ).fetchone()
        if row is None:
            result["reasons"].append("missing_event_region_facts")
            return result
        try:
            facts, retrieval = json.loads(row["facts_json"]), json.loads(row["retrieval_json"])
            source = retrieval.get("field_sources", {}).get("startAt", retrieval)
            start = facts.get("startAt")
            if not isinstance(start, (int, float)) or isinstance(start, bool) or start <= 0:
                raise ValueError("missing_start_at")
            if source.get("table") != "events" or not source.get("path") or not source.get("sha256"):
                raise ValueError("missing_sealed_events_retrieval")
            table = self.tables.load(self._resolve(source["path"]), source["sha256"])
            result["event_file"] = self.tables.evidence(table)
            if table.get("error"):
                result["reasons"].append(table["error"])
                return result
            event = table["by_id"].get(str(event_id))
            if event is None or event.get("startAt") != start:
                result["reasons"].append("event_record_mismatch")
                return result
            result.update(start_at_ms=start, start_at_utc=datetime.fromtimestamp(start / 1000, timezone.utc).isoformat(),
                          generation=source.get("generation"), event_record=event,
                          source=row["source"], version=row["version"])
            story_source = next((value for value in retrieval.get("field_sources", {}).values()
                                 if value.get("table") == "eventStories" and value.get("path")), None)
            story_path = self._resolve(story_source["path"]) if story_source else Path(table["path"]).with_name("eventStories.json")
            stories = self.tables.load(story_path, story_source.get("sha256") if story_source else None)
            result["story_file"] = self.tables.evidence(stories)
            if stories.get("error"):
                result["reasons"].append(stories["error"])
                return result
            story = next((item for item in stories["records"] if item.get("eventId") == event_id), None)
            if story is None:
                result["reasons"].append("missing_event_story_record")
                return result
            result["story_record"] = story
            conditions = self.tables.load(story_path.with_name("releaseConditions.json"))
            result["condition_file"] = self.tables.evidence(conditions)
            result["conditions"] = conditions.get("by_id", {})
            episodes = list(story.get("eventStoryEpisodes", []))
            separate = story_path.with_name("eventStoryEpisodes.json")
            if separate.is_file():
                separate_table = self.tables.load(separate)
                result["episode_file"] = self.tables.evidence(separate_table)
                if separate_table.get("error"):
                    result["reasons"].append(separate_table["error"])
                    return result
                episodes.extend(item for item in separate_table["records"] if item.get("eventStoryId") == story.get("id"))
            result["episodes"] = episodes
            result["status"] = "event_scheduled_released" if start <= self.as_of_ms else "not_yet_released"
        except (ValueError, TypeError, KeyError) as exc:
            result["reasons"].append(str(exc))
        return result

    def chapter(self, logical_key: str, language: str) -> dict:
        match = EVENT_STORY.fullmatch(logical_key)
        region = REGIONS.get(language)
        if match is None or region is None:
            return {"status": "unknown_release", "reasons": ["unsupported_release_domain"]}
        event_id, episode_no = map(int, match.groups())
        event = self._event(event_id, region)
        result = {"status": "unknown_release", "event_id": event_id, "episode_no": episode_no,
                  "region": region, "reasons": list(event["reasons"]),
                  "proof_scope": "local_masterdata_schedule_and_unlock_definition",
                  "live_server_verified": False}
        for field in ("event_file", "story_file", "episode_file", "condition_file", "start_at_ms", "start_at_utc", "generation"):
            if field in event:
                result[field] = event[field]
        if event["status"] == "unknown_release":
            return result
        episodes = [item for item in event.get("episodes", []) if item.get("episodeNo") == episode_no]
        if not episodes:
            result["reasons"].append("missing_episode_record")
            return result
        if len({json.dumps(item, sort_keys=True) for item in episodes}) != 1:
            result["reasons"].append("conflicting_episode_records")
            return result
        episode = episodes[0]
        result["episode_record"] = episode
        result["event_record_identity"] = {field: event["event_record"].get(field)
                                           for field in ("id", "startAt", "closedAt", "endAt")}
        if not episode.get("scenarioId"):
            result["reasons"].append("missing_episode_scenario_id")
            return result
        if event["status"] == "not_yet_released":
            result["status"] = "not_yet_released"
            return result
        for container in (event["story_record"], episode):
            for field in ("releaseAt", "startAt", "availableAt", "openAt"):
                value = container.get(field)
                if value is not None:
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                        result["reasons"].append("invalid_episode_release_time")
                        return result
                    if value > self.as_of_ms:
                        result.update(status="not_yet_released", future_gate={"field": field, "value": value})
                        return result
        condition_id = episode.get("releaseConditionId")
        condition = event.get("conditions", {}).get(str(condition_id))
        result["unlock_condition"] = condition
        if condition is None:
            result["reasons"].append("missing_unlock_condition")
            return result
        condition_type = condition.get("releaseConditionType")
        supported = condition_type == "none" and condition_id == 1
        supported |= (condition_type == "event_point" and condition.get("releaseConditionTypeId") == event_id
                      and isinstance(condition.get("releaseConditionTypeQuantity"), (int, float))
                      and condition["releaseConditionTypeQuantity"] >= 0)
        if not supported:
            result["reasons"].append("unsupported_unlock_condition")
            return result
        result["status"] = "released_in_verified_masterdata"
        result["user_unlock_required"] = condition_type != "none"
        return result


class UnitStoryVerifier:
    """Sealed nested episode identities; unlocking is not publication evidence."""

    IDENTITY_FIELDS = ("unit", "story_seq", "chapter_id", "chapter_no", "episode_id",
                       "episode_no", "episode_group_id", "scenario_id", "assetbundle_name")

    def __init__(self, conn: sqlite3.Connection, as_of: datetime):
        self.as_of_ms = int(as_of.timestamp() * 1000)
        self.tables, self.sources = RawTables(), []
        self.records = {language: {} for language in LANGUAGES}
        self.ids = {language: {} for language in LANGUAGES}
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_region_facts'").fetchone():
            self.sources.append({"errors": ["missing_region_facts_table"]})
            return
        seen, languages = set(), {region: language for language, region in REGIONS.items()}
        rows = conn.execute("SELECT entity_id,region,retrieval_json FROM entity_region_facts "
                            "WHERE entity_id LIKE 'unit_story:%' ORDER BY region,entity_id")
        for entity_id, region, raw in rows:
            if region not in languages:
                continue
            language = languages[region]
            try:
                source = json.loads(raw)
                if source.get("table") != "unitStories" or not source.get("path") or not source.get("sha256"):
                    raise ValueError("missing_sealed_unitStories_retrieval")
                path = Path(source["path"])
                path = path if path.is_absolute() else ROOT / path
                signature = language, str(path.resolve()), source["sha256"]
                if signature in seen:
                    continue
                seen.add(signature)
                source_id = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
                table = self.tables.load(path, source["sha256"])
                evidence = {"id": source_id, "language": language, "region": region, "table": "unitStories",
                            "generation": source.get("generation"), "retrieval_sealed": True,
                            "file": self.tables.evidence(table), "errors": [], "invalid_identity_records": []}
                self.sources.append(evidence)
                if table.get("error"):
                    evidence["errors"].append(table["error"])
                    continue
                for story in table["records"]:
                    chapters = story.get("chapters")
                    if not isinstance(chapters, list):
                        evidence["invalid_identity_records"].append({"unit": story.get("unit"), "reason": "unsupported_chapters_schema"})
                        continue
                    for chapter in chapters:
                        if not isinstance(chapter, dict) or not isinstance(chapter.get("episodes"), list):
                            evidence["invalid_identity_records"].append({"unit": story.get("unit"), "reason": "unsupported_episodes_schema"})
                            continue
                        for episode in chapter["episodes"]:
                            if not isinstance(episode, dict):
                                evidence["invalid_identity_records"].append({"reason": "nonrecord_episode"})
                                continue
                            identity = {"unit": story.get("unit"), "story_seq": story.get("seq"),
                                        "chapter_id": chapter.get("id"), "chapter_no": chapter.get("chapterNo"),
                                        "episode_id": episode.get("id"), "episode_no": episode.get("episodeNo"),
                                        "episode_group_id": episode.get("unitStoryEpisodeGroupId"),
                                        "scenario_id": episode.get("scenarioId"),
                                        "assetbundle_name": chapter.get("assetbundleName") or episode.get("assetbundleName")}
                            reasons = []
                            for field in ("story_seq", "chapter_id", "chapter_no", "episode_id", "episode_no", "episode_group_id"):
                                value = identity[field]
                                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                                    reasons.append("invalid_" + field)
                            for field in ("unit", "scenario_id", "assetbundle_name"):
                                value = identity[field]
                                if not isinstance(value, str) or not value or value.strip() != value or "/" in value or ":" in value:
                                    reasons.append("invalid_" + field)
                            if chapter.get("unit") != story.get("unit") or episode.get("chapterNo") != chapter.get("chapterNo"):
                                reasons.append("nested_unit_or_chapter_foreign_key_mismatch")
                            if reasons:
                                evidence["invalid_identity_records"].append({"identity": identity, "reasons": reasons})
                                continue
                            wrapper = {"identity": identity, "story": story, "chapter": chapter, "record": episode,
                                       "source_id": source_id, "path": str(path.resolve()), "file": self.tables.evidence(table)}
                            self.records[language].setdefault(identity["scenario_id"], []).append(wrapper)
                            self.ids[language].setdefault(identity["episode_id"], []).append(wrapper)
            except (ValueError, TypeError, KeyError) as exc:
                self.sources.append({"language": language, "region": region, "entity_id": entity_id, "errors": [str(exc)]})

    def unique(self, language: str, scenario: str) -> tuple[dict | None, list[str]]:
        variants = self.records[language].get(scenario, [])
        if not variants:
            return None, ["missing_unit_story_episode"]
        if len(variants) > 1 and len({json.dumps({"identity": row["identity"], "record": row["record"]}, sort_keys=True)
                                      for row in variants}) != 1:
            return None, ["conflicting_unit_story_episode_definitions"]
        return variants[0], []

    def unlock(self, language: str, scenario: str, stack: tuple[str, ...] = ()) -> dict:
        result = {"status": "unknown_unlock", "reasons": [], "user_progress_verified": False}
        if scenario in stack:
            result["reasons"].append("cyclic_unit_story_unlock_dependency")
            return result
        episode, reasons = self.unique(language, scenario)
        result["reasons"].extend(reasons)
        if episode is None:
            return result
        conditions = self.tables.load(Path(episode["path"]).with_name("releaseConditions.json"))
        result.update(condition_file=self.tables.evidence(conditions), condition_retrieval_sealed=False)
        if conditions.get("error"):
            result["reasons"].append(conditions["error"])
            return result
        condition_id = episode["record"].get("releaseConditionId")
        if not isinstance(condition_id, int) or isinstance(condition_id, bool) or condition_id <= 0:
            result["reasons"].append("invalid_unit_story_unlock_condition_id")
            return result
        matching = [row for row in conditions["records"] if row.get("id") == condition_id]
        if not matching or len({json.dumps(row, sort_keys=True) for row in matching}) != 1:
            result["reasons"].append("missing_or_conflicting_unit_story_unlock_condition")
            return result
        condition = matching[0]
        result["condition"] = condition
        kind = condition.get("releaseConditionType")
        if kind == "none" and condition_id == 1:
            result.update(status="reachable_in_locally_hashed_masterdata", prerequisite_episode_ids=[], user_unlock_required=False)
        elif kind == "unit_story":
            prerequisite_id = condition.get("releaseConditionTypeId")
            if not isinstance(prerequisite_id, int) or isinstance(prerequisite_id, bool) or prerequisite_id <= 0:
                result["reasons"].append("invalid_unit_story_prerequisite_id")
                return result
            variants = self.ids[language].get(prerequisite_id, [])
            if not variants or len({row["identity"]["scenario_id"] for row in variants}) != 1:
                result["reasons"].append("missing_or_ambiguous_unit_story_prerequisite_id")
                return result
            prerequisite_scenario = variants[0]["identity"]["scenario_id"]
            prerequisite = self.unlock(language, prerequisite_scenario, stack + (scenario,))
            result["prerequisite"] = {"episode_id": prerequisite_id, "scenario_id": prerequisite_scenario,
                                      "status": prerequisite["status"], "reasons": prerequisite["reasons"]}
            if prerequisite["status"] != "reachable_in_locally_hashed_masterdata":
                result["reasons"].append("unit_story_prerequisite_not_verified_reachable")
                return result
            result.update(status="reachable_in_locally_hashed_masterdata", user_unlock_required=True,
                          prerequisite_episode_ids=[prerequisite_id, *prerequisite["prerequisite_episode_ids"]])
        else:
            result["reasons"].append("unsupported_unit_story_unlock_condition")
        return result

    def release(self, language: str, scenario: str) -> dict:
        result = {"status": "unknown_release", "reasons": [], "live_server_verified": False,
                  "proof_scope": "explicit_sealed_publication_dates_not_definition_presence_or_temporary_free_windows"}
        episode, reasons = self.unique(language, scenario)
        result["reasons"].extend(reasons)
        if episode is None:
            return result
        result.update(inventory_source_id=episode["source_id"], raw_identity=episode["identity"],
                      unlock=self.unlock(language, scenario))
        dates = []
        for role in ("story", "chapter", "record"):
            value = episode[role].get("releaseAt")
            if value is not None:
                if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                    result["reasons"].append("invalid_explicit_unit_story_release_at")
                    return result
                dates.append({"role": role, "release_at_ms": value})
        result["publication_dates"] = dates
        start, end = (episode["record"].get(field) for field in ("limitedReleaseStartAt", "limitedReleaseEndAt"))
        window = {"start_ms": start, "end_ms": end, "is_publication_evidence": False, "state": "absent"}
        if start is not None or end is not None:
            if any(not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0 for value in (start, end)) or start >= end:
                window["state"] = "invalid_window"
            else:
                window["state"] = "before" if self.as_of_ms < start else "active" if self.as_of_ms < end else "ended"
        result["temporary_free_access_window"] = window
        if any(row["release_at_ms"] > self.as_of_ms for row in dates):
            result["status"] = "not_yet_released"
        elif not dates:
            result["reasons"].append("publication_time_not_proven_by_unitStories_schema")
        elif result["unlock"]["status"] != "reachable_in_locally_hashed_masterdata":
            result["reasons"].append("unit_story_unlock_not_verified_reachable")
        else:
            result["prerequisite_publication_checks"] = []
            for identity in result["unlock"]["prerequisite_episode_ids"]:
                prerequisite_scenario = self.ids[language][identity][0]["identity"]["scenario_id"]
                prerequisite = self.release(language, prerequisite_scenario)
                result["prerequisite_publication_checks"].append({"episode_id": identity,
                    "scenario_id": prerequisite_scenario, "status": prerequisite["status"]})
            prerequisite_statuses = [row["status"] for row in result["prerequisite_publication_checks"]]
            if "not_yet_released" in prerequisite_statuses:
                result.update(status="not_yet_released", reasons=["prerequisite_unit_story_publication_is_future"])
            elif "unknown_release" in prerequisite_statuses:
                result["reasons"].append("prerequisite_unit_story_publication_not_proven")
            else:
                result["status"] = "released_in_verified_masterdata"
        return result


class CardStoryVerifier:
    """Exact episode foreign keys, sealed card dates, and raw unlock definitions."""

    def __init__(self, conn: sqlite3.Connection, as_of: datetime):
        self.conn = conn
        self.as_of_ms = int(as_of.timestamp() * 1000)
        self.tables = RawTables()
        self.sources = []
        self.records = {language: {"cards": {}, "cardEpisodes": {}} for language in LANGUAGES}
        self.release_cache = {}
        self.level_cache = {}
        self.available = bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entity_region_facts'").fetchone())
        if not self.available:
            self.sources.append({"errors": ["missing_region_facts_table"]})
            return
        seen = set()
        languages = {region: language for language, region in REGIONS.items()}
        rows = conn.execute("SELECT entity_id,region,retrieval_json FROM entity_region_facts "
                            "WHERE entity_id LIKE 'card:%' OR entity_id LIKE 'card_episode:%' ORDER BY region,entity_id")
        for entity_id, region, raw in rows:
            if region not in languages:
                continue
            language = languages[region]
            try:
                retrieval = json.loads(raw)
                source = retrieval.get("field_sources", {}).get("releaseAt", retrieval)
                table_name = "cards" if entity_id.startswith("card:") else "cardEpisodes"
                if source.get("table") != table_name or not source.get("path") or not source.get("sha256"):
                    raise ValueError("missing_sealed_" + table_name + "_retrieval")
                path = Path(source["path"])
                path = path if path.is_absolute() else ROOT / path
                signature = language, table_name, str(path.resolve()), source["sha256"]
                if signature in seen:
                    continue
                seen.add(signature)
                source_id = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
                table = self.tables.load(path, source["sha256"])
                evidence = {"id": source_id, "language": language, "region": region, "table": table_name,
                            "generation": source.get("generation"), "retrieval_sealed": True,
                            "file": self.tables.evidence(table), "errors": [], "invalid_identity_records": []}
                self.sources.append(evidence)
                if table.get("error"):
                    evidence["errors"].append(table["error"])
                    continue
                for record in table["records"]:
                    identity = record.get("id")
                    if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
                        evidence["invalid_identity_records"].append({"id": identity, "reason": "invalid_exact_record_id"})
                        continue
                    self.records[language][table_name].setdefault(identity, []).append({"record": record,
                        "source_id": source_id, "path": str(path.resolve()), "file": self.tables.evidence(table)})
            except (ValueError, TypeError, KeyError) as exc:
                self.sources.append({"language": language, "region": region, "entity_id": entity_id, "errors": [str(exc)]})

    def unique(self, language: str, table: str, identity: int) -> tuple[dict | None, list[str]]:
        variants = self.records[language][table].get(identity, [])
        if not variants:
            return None, ["missing_" + table + "_record"]
        if len(variants) > 1 and len({json.dumps(row["record"], sort_keys=True) for row in variants}) != 1:
            return None, ["conflicting_" + table + "_records"]
        return variants[0], []

    def card_level_evidence(self, language: str, identity: int, parent: dict) -> dict:
        key = language, identity
        if key in self.level_cache:
            return self.level_cache[key]
        parameters = parent.get("cardParameters")
        if isinstance(parameters, list) and parameters and all(isinstance(row, dict) for row in parameters):
            levels = {row.get("cardLevel") for row in parameters}
            if not all(isinstance(level, int) and not isinstance(level, bool) and level > 0 for level in levels):
                raise ValueError("invalid_expanded_card_level_records")
            result = {"schema": "expanded_explicit_cardLevel_records", "levels": sorted(levels)}
        elif isinstance(parameters, dict) and set(parameters) == {"param1", "param2", "param3"}:
            if not all(isinstance(values, list) and values and all(isinstance(value, (int, float)) and not isinstance(value, bool)
                                                                 for value in values) for values in parameters.values()):
                raise ValueError("invalid_compact_card_parameter_arrays")
            result = None
            for counterpart_language in LANGUAGES:
                counterpart, _ = self.unique(counterpart_language, "cards", identity)
                if counterpart is None:
                    continue
                record = counterpart["record"]
                expanded = record.get("cardParameters")
                if not isinstance(expanded, list) or not expanded or not all(isinstance(row, dict) for row in expanded):
                    continue
                if record.get("assetbundleName") != parent.get("assetbundleName") or record.get("cardRarityType") != parent.get("cardRarityType"):
                    continue
                groups = {name: sorted([row for row in expanded if row.get("cardParameterType") == name],
                                      key=lambda row: row.get("cardLevel", 0)) for name in parameters}
                if all([row.get("power") for row in groups[name]] == values
                       and [row.get("cardLevel") for row in groups[name]] == list(range(1, len(values) + 1))
                       for name, values in parameters.items()):
                    result = {"schema": "compact_param_arrays_exactly_matched_to_explicit_same_card_records",
                              "levels": sorted({row["cardLevel"] for rows in groups.values() for row in rows}),
                              "counterpart_language": counterpart_language, "counterpart_source_id": counterpart["source_id"],
                              "counterpart_card_id": identity, "counterpart_file": counterpart["file"],
                              "all_three_parameter_value_sequences_equal": True}
                    break
            if result is None:
                raise ValueError("compact_parameter_level_mapping_not_verified_against_exact_card_records")
        else:
            raise ValueError("unsupported_card_parameters_schema")
        self.level_cache[key] = result
        return result

    def release(self, episode_id: int, language: str) -> dict:
        key = episode_id, language
        if key in self.release_cache:
            return self.release_cache[key]
        result = {"status": "unknown_release", "episode_id": episode_id, "region": REGIONS[language], "reasons": [],
                  "proof_scope": "local_masterdata_card_schedule_episode_foreign_keys_and_unlock_definition", "live_server_verified": False}
        self.release_cache[key] = result
        episode, reasons = self.unique(language, "cardEpisodes", episode_id)
        result["reasons"].extend(reasons)
        if episode is None:
            return result
        record = episode["record"]
        result.update(episode_record={field: record.get(field) for field in ("id", "cardId", "scenarioId", "assetbundleName", "cardEpisodePartType", "releaseConditionId")},
                      episode_file=episode["file"], episode_inventory_source=episode["source_id"])
        card_id = record.get("cardId")
        if not isinstance(card_id, int) or isinstance(card_id, bool) or card_id <= 0:
            result["reasons"].append("invalid_episode_card_foreign_key")
            return result
        card, reasons = self.unique(language, "cards", card_id)
        result["reasons"].extend(reasons)
        if card is None:
            return result
        parent = card["record"]
        result.update(card_id=card_id, card_file=card["file"], card_inventory_source=card["source_id"],
                      card_record_identity={field: parent.get(field) for field in ("id", "releaseAt", "assetbundleName", "cardRarityType")})
        if not record.get("scenarioId") or not parent.get("assetbundleName"):
            result["reasons"].append("missing_episode_scenario_or_card_asset")
            return result
        if record.get("assetbundleName") and record["assetbundleName"] != parent["assetbundleName"]:
            result["reasons"].append("episode_card_asset_mismatch")
            return result
        fact = self.conn.execute("SELECT facts_json,retrieval_json FROM entity_region_facts WHERE entity_id=? AND region=?",
                                 (f"card:{card_id}", REGIONS[language])).fetchone()
        if fact is None:
            result["reasons"].append("missing_card_region_facts")
            return result
        try:
            facts, retrieval = json.loads(fact[0]), json.loads(fact[1])
            source = retrieval.get("field_sources", {}).get("releaseAt", retrieval)
            release_at = parent.get("releaseAt")
            if not isinstance(release_at, (int, float)) or isinstance(release_at, bool) or release_at <= 0:
                raise ValueError("invalid_card_release_at")
            if facts.get("releaseAt") != release_at:
                raise ValueError("card_release_fact_mismatch")
            if source.get("table") != "cards" or source.get("sha256") != card["file"]["sha256"]:
                raise ValueError("card_release_retrieval_mismatch")
            result.update(release_at_ms=release_at, release_at_utc=datetime.fromtimestamp(release_at / 1000, timezone.utc).isoformat())
            if release_at > self.as_of_ms:
                result["status"] = "not_yet_released"
                return result
            for field in ("releaseAt", "startAt", "availableAt", "openAt"):
                value = record.get(field)
                if value is not None:
                    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
                        raise ValueError("invalid_episode_release_gate")
                    if value > self.as_of_ms:
                        result.update(status="not_yet_released", future_gate={"field": field, "value": value})
                        return result
            conditions = self.tables.load(Path(episode["path"]).with_name("releaseConditions.json"))
            result.update(condition_file=self.tables.evidence(conditions), condition_retrieval_sealed=False)
            if conditions.get("error"):
                raise ValueError(conditions["error"])
            condition = conditions["by_id"].get(str(record.get("releaseConditionId")))
            result["unlock_condition"] = condition
            if condition is None:
                raise ValueError("missing_unlock_condition")
            matching_conditions = [row for row in conditions["records"] if row.get("id") == record.get("releaseConditionId")]
            if len({json.dumps(row, sort_keys=True) for row in matching_conditions}) != 1:
                raise ValueError("conflicting_unlock_condition_records")
            part = record.get("cardEpisodePartType")
            kind = condition.get("releaseConditionType")
            if part == "first_part" and kind == "none" and record.get("releaseConditionId") == 1:
                result["user_unlock_requirements"] = ["card_ownership"]
            elif part == "second_part" and kind == "card_level" and condition.get("releaseConditionTypeId") == card_id:
                level = condition.get("releaseConditionTypeLevel")
                if not isinstance(level, int) or isinstance(level, bool) or level <= 0:
                    raise ValueError("invalid_card_level_unlock")
                level_evidence = self.card_level_evidence(language, card_id, parent)
                result["card_level_capacity_evidence"] = level_evidence
                if level not in level_evidence["levels"]:
                    raise ValueError("unlock_level_not_in_card_parameters")
                first_parts = [row["record"] for variants in self.records[language]["cardEpisodes"].values() for row in variants
                               if row["record"].get("cardId") == card_id and row["record"].get("cardEpisodePartType") == "first_part"]
                first_parts = {row["id"]: row for row in first_parts}
                if len(first_parts) != 1 or not next(iter(first_parts.values())).get("scenarioId"):
                    raise ValueError("missing_or_ambiguous_prerequisite_first_part")
                first = next(iter(first_parts.values()))
                first_release = self.release(first["id"], language)
                if first_release["status"] != "released_in_verified_masterdata":
                    raise ValueError("prerequisite_first_part_not_verified_released")
                result.update(prerequisite_first_part={field: first.get(field) for field in ("id", "cardId", "scenarioId", "releaseConditionId")},
                              unlock_card_level=level, user_unlock_requirements=["card_ownership", "card_level", "first_part_read"])
            else:
                raise ValueError("unsupported_or_mismatched_unlock_definition")
            result["status"] = "released_in_verified_masterdata"
            result["user_unlock_required"] = True
        except (ValueError, TypeError, KeyError) as exc:
            result["reasons"].append(str(exc))
        return result


def _build_index(conn: sqlite3.Connection, index: sqlite3.Connection) -> list[dict]:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(web_pages)")}
    required = {"source", "id", "kind", "language", "text"}
    if not required <= columns:
        raise ValueError("Missing required web_pages columns: " + ",".join(sorted(required - columns)))
    aggregates = [dict(row) for row in conn.execute(
        "SELECT kind,language,source,COUNT(*) page_count FROM web_pages GROUP BY kind,language,source ORDER BY kind,language,source"
    )]
    index.execute("CREATE TABLE pages(source TEXT,id TEXT,kind TEXT,language TEXT,logical_key TEXT,"
                  "identity_basis TEXT,char_count INTEGER,good INTEGER,primary_good INTEGER,version_hash TEXT,metadata_json TEXT,PRIMARY KEY(source,id))")
    optional = ("canonical_key", "hash", "text_hash", "source_hash", "crawled_at", "trust", "untranslated",
                "untranslated_placeholder", "asset_mismatch", "scenario_id_mismatch", "content_language_mismatch",
                "auxiliary", "derived", "overlay", "source_type", "source_last_modified", "source_etag")
    projection = ",".join("\"" + key + "\"" if key in columns else "'' AS \"" + key + "\"" for key in optional)
    query = "SELECT source,id,kind,language,length(trim(text)) AS char_count," + projection + " FROM web_pages"
    count = 0
    for row in conn.execute(query):
        page = dict(row)
        page["raw_language"] = page["language"]
        page["language"] = normalize_language(page["language"])
        key, basis = logical_identity(page)
        reasons = bad_page_reasons(page)
        page["bad_reasons"] = reasons
        page["nonprimary_flags"] = [field for field in ("auxiliary", "derived", "overlay") if _flag(page.get(field))]
        digest = page.get("text_hash") or page.get("hash")
        if not digest:
            digest = "unknown:" + page["source"] + ":" + page["id"]
        index.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                      (page["source"], page["id"], page["kind"], page["language"], key, basis,
                       page["char_count"], int(not reasons), int(not reasons and not page["nonprimary_flags"]),
                       digest, json.dumps(page, ensure_ascii=False)))
        count += 1
        if count % 20000 == 0:
            index.commit()
    index.execute("CREATE INDEX pages_logical_language ON pages(logical_key,language)")
    index.commit()
    return aggregates


def _logical_ledger(index: sqlite3.Connection, verifier: ReleaseVerifier, out: Path) -> tuple[list[dict], list[dict], list[dict]]:
    domain: dict[str, dict] = {}
    matrix: Counter = Counter()
    event_units = []
    with (out / "logical-units.jsonl").open("w", encoding="utf-8") as handle:
        rows = index.execute("SELECT logical_key,kind,language,COUNT(*) n,SUM(good) good,SUM(primary_good) primary_good,"
                             "COUNT(DISTINCT version_hash) versions FROM pages GROUP BY logical_key,kind,language ORDER BY logical_key,language")
        unit, last = None, None

        def flush(item):
            if item is None:
                return
            item["missing_languages"] = [lang for lang in LANGUAGES if lang not in item["languages"]]
            item["bad_only_languages"] = [lang for lang in LANGUAGES if item["languages"].get(lang, {}).get("good", 0) == 0
                                           and lang in item["languages"]]
            item["nonprimary_only_languages"] = [lang for lang in LANGUAGES
                if item["languages"].get(lang, {}).get("primary_good", 0) == 0
                and item["languages"].get(lang, {}).get("good", 0) > 0]
            item["local_five_language_complete"] = (not item["missing_languages"] and not item["bad_only_languages"]
                                                    and not item["nonprimary_only_languages"])
            item["release"] = {lang: verifier.chapter(item["logical_key"], lang) for lang in LANGUAGES}
            statuses = [value["status"] for value in item["release"].values()]
            item["release_status"] = ("all_five_released_in_verified_masterdata" if all(
                status == "released_in_verified_masterdata" for status in statuses) else
                "contains_not_yet_released" if "not_yet_released" in statuses else "unknown_release")
            stats = domain.setdefault(item["kind"], {"kind": item["kind"], "logical_units": 0,
                                                     "local_five_language_complete": 0, "release_status_counts": Counter()})
            stats["logical_units"] += 1
            stats["local_five_language_complete"] += int(item["local_five_language_complete"])
            stats["release_status_counts"][item["release_status"]] += 1
            for lang in LANGUAGES:
                state = ("missing" if lang in item["missing_languages"] else "bad_only" if lang in item["bad_only_languages"]
                         else "nonprimary_only" if lang in item["nonprimary_only_languages"] else "good_present")
                matrix[item["kind"], lang, state] += 1
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
            if EVENT_STORY.fullmatch(item["logical_key"]):
                event_units.append(item)

        for key, kind, lang, total, good, primary_good, versions in rows:
            if key != last:
                flush(unit)
                unit, last = {"logical_key": key, "kind": kind, "languages": {}}, key
            unit["languages"][lang] = {"pages": total, "good": good, "primary_good": primary_good, "distinct_versions": versions,
                                      "duplicate_pages": total - versions}
        flush(unit)
    return sorted(domain.values(), key=lambda row: row["kind"]), [
        {"kind": kind, "language": lang, "state": state, "logical_units": count}
        for (kind, lang, state), count in sorted(matrix.items())], event_units


def _expected_event_inventory(verifier: ReleaseVerifier) -> tuple[dict, list[dict]]:
    inventory, sources, seen = {}, [], set()
    if not verifier.facts_available:
        return inventory, [{"errors": ["missing_region_facts_table"]}]
    region_languages = {region: language for language, region in REGIONS.items()}
    rows = verifier.conn.execute("SELECT entity_id,region,retrieval_json FROM entity_region_facts WHERE entity_id LIKE 'event:%' ORDER BY region,entity_id")
    for entity_id, region, retrieval_raw in rows:
        if region not in region_languages:
            continue
        language = region_languages[region]
        try:
            retrieval = json.loads(retrieval_raw)
            event_source = retrieval.get("field_sources", {}).get("startAt", retrieval)
            if event_source.get("table") != "events" or not event_source.get("path") or not event_source.get("sha256"):
                raise ValueError("missing_sealed_events_retrieval")
            story_source = next((value for value in retrieval.get("field_sources", {}).values()
                                 if value.get("table") == "eventStories" and value.get("path")), None)
            event_path = verifier._resolve(event_source["path"])
            story_path = verifier._resolve(story_source["path"]) if story_source else event_path.with_name("eventStories.json")
            signature = (region, str(event_path.resolve()), event_source["sha256"], str(story_path.resolve()),
                         story_source.get("sha256") if story_source else None)
            if signature in seen:
                continue
            seen.add(signature)
            source_id = hashlib.sha256(json.dumps(signature).encode()).hexdigest()
            source = {"id": source_id, "language": language, "region": region, "errors": [],
                      "story_retrieval_sealed": bool(story_source and story_source.get("sha256")),
                      "generation": event_source.get("generation"), "invalid_identity_records": [], "chapter_definitions": 0}
            sources.append(source)
            events = verifier.tables.load(event_path, event_source["sha256"])
            stories = verifier.tables.load(story_path, story_source.get("sha256") if story_source else None)
            source.update(event_file=verifier.tables.evidence(events), story_file=verifier.tables.evidence(stories))
            if events.get("error") or stories.get("error"):
                source["errors"].extend(table["error"] for table in (events, stories) if table.get("error"))
                continue
            separate_path = story_path.with_name("eventStoryEpisodes.json")
            separate = None
            if separate_path.is_file():
                separate_source = next((value for value in retrieval.get("field_sources", {}).values()
                                        if value.get("table") == "eventStoryEpisodes" and value.get("path")), None)
                separate = verifier.tables.load(separate_path, separate_source.get("sha256") if separate_source else None)
                source["episode_file"] = verifier.tables.evidence(separate)
                source["episode_retrieval_sealed"] = bool(separate_source and separate_source.get("sha256"))
                if separate.get("error"):
                    source["errors"].append(separate["error"])
                    separate = None
            for story in stories["records"]:
                event_id = story.get("eventId")
                if not isinstance(event_id, int) or isinstance(event_id, bool) or event_id <= 0:
                    source["invalid_identity_records"].append({"story_id": story.get("id"), "reason": "invalid_event_id"})
                    continue
                episodes = list(story.get("eventStoryEpisodes", []))
                if separate:
                    episodes.extend(episode for episode in separate["records"] if episode.get("eventStoryId") == story.get("id"))
                for episode in episodes:
                    number = episode.get("episodeNo")
                    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
                        source["invalid_identity_records"].append({"story_id": story.get("id"), "episode_id": episode.get("id"),
                                                                    "reason": "invalid_episode_number"})
                        continue
                    key = f"event_story:{event_id}:{number}"
                    definition = {"event_story_id": story.get("id"), "episode_record": episode,
                                  "event_record_present": str(event_id) in events["by_id"], "inventory_source_ids": [source_id]}
                    definitions = inventory.setdefault(key, {}).setdefault(language, [])
                    duplicate = next((row for row in definitions if row["episode_record"] == episode
                                      and row["event_story_id"] == story.get("id")), None)
                    if duplicate:
                        if source_id not in duplicate["inventory_source_ids"]:
                            duplicate["inventory_source_ids"].append(source_id)
                    else:
                        definitions.append(definition)
                    source["chapter_definitions"] += 1
        except (ValueError, TypeError, KeyError) as exc:
            sources.append({"language": language, "region": region, "entity_id": entity_id, "errors": [str(exc)]})
    return inventory, sources


def _expected_event_ledger(index: sqlite3.Connection, verifier: ReleaseVerifier, out: Path) -> dict:
    expected, sources = _expected_event_inventory(verifier)
    observed = {}
    rows = index.execute("SELECT logical_key,language,COUNT(*),SUM(good),SUM(primary_good),COUNT(DISTINCT version_hash) "
                         "FROM pages WHERE kind='event_story' GROUP BY logical_key,language ORDER BY logical_key,language")
    for key, language, pages, good, primary_good, versions in rows:
        if EVENT_STORY.fullmatch(key):
            observed.setdefault(key, {})[language] = {"pages": pages, "good": good, "primary_good": primary_good,
                                                     "distinct_versions": versions}
    matrix, counts, release_counts = Counter(), Counter(), Counter()
    with (out / "expected-event-chapters.jsonl").open("w", encoding="utf-8") as handle:
        for key in sorted(set(expected) | set(observed)):
            acquired, definitions = observed.get(key, {}), expected.get(key, {})
            origin = "observed_and_masterdata" if key in expected and key in observed else "masterdata_only" if key in expected else "observed_only"
            states = {language: ("missing" if language not in acquired else "bad_only" if not acquired[language]["good"]
                                 else "nonprimary_only" if not acquired[language]["primary_good"] else "good_present") for language in LANGUAGES}
            release = {language: verifier.chapter(key, language) for language in LANGUAGES}
            statuses = [row["status"] for row in release.values()]
            release_status = ("all_five_released_in_verified_masterdata" if all(status == "released_in_verified_masterdata" for status in statuses)
                              else "contains_not_yet_released" if "not_yet_released" in statuses else "unknown_release")
            row = {"logical_key": key, "origin": origin, "expected_definitions": definitions,
                   "expected_inventory_languages": [language for language in LANGUAGES if language in definitions],
                   "missing_from_raw_inventory_languages": [language for language in LANGUAGES if language not in definitions],
                   "conflicting_inventory_languages": [language for language, values in definitions.items() if len(values) > 1],
                   "acquired_languages": acquired, "acquisition_states": states,
                   "all_five_pages_absent": not acquired, "local_five_language_primary_complete": all(state == "good_present" for state in states.values()),
                   "release_status": release_status, "release": release}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts[origin] += 1
            counts["local_five_language_primary_complete"] += int(row["local_five_language_primary_complete"])
            counts["all_five_pages_absent"] += int(row["all_five_pages_absent"])
            counts["all_five_released_pages_absent"] += int(row["all_five_pages_absent"] and release_status == "all_five_released_in_verified_masterdata")
            counts["five_language_raw_inventory_complete"] += int(len(definitions) == 5)
            release_counts[release_status] += 1
            for language, state in states.items():
                matrix[language, language in definitions, state, release_status] += 1
    _write_json(out / "expected-event-inventory-sources.json", sources)
    _write_json(out / "expected-event-acquisition-matrix.json", [
        {"language": language, "in_raw_masterdata_inventory": in_inventory, "state": state,
         "release_status": status, "chapter_units": count}
        for (language, in_inventory, state, status), count in sorted(matrix.items())])
    summary = {"schema": "sekaisync/p0-expected-event-chapter-inventory@1",
               "as_of_utc": datetime.fromtimestamp(verifier.as_of_ms / 1000, timezone.utc).isoformat(),
               "scope": "union_of_observed_pages_and_chapters_defined_in_available_hashed_raw_masterdata_tables",
               "not_a_complete_all_domain_release_universe": True, "source_database_read_only": True,
               "observed_chapter_units": len(observed), "masterdata_inventory_chapter_units": len(expected),
               "union_chapter_units": len(set(expected) | set(observed)), "counts": dict(counts),
               "release_status_counts": dict(release_counts), "inventory_source_count": len(sources),
               "inventory_sources_with_errors": sum(bool(row["errors"]) for row in sources),
               "invalid_identity_records": sum(len(row.get("invalid_identity_records", [])) for row in sources),
               "story_sources_not_retrieval_sealed": sum(row.get("story_retrieval_sealed") is False for row in sources)}
    _write_json(out / "expected-event-inventory-summary.json", summary)
    return summary


def refresh_expected_events(store: Path, out: Path) -> dict:
    summary_path = out / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(summary["as_of_utc"])
    with closing(open_read_only(store / "kb/sekaisync.db")) as conn, closing(open_read_only(out / "census-index.sqlite")) as index:
        inventory = _expected_event_ledger(index, ReleaseVerifier(conn, as_of), out)
    summary["expected_event_inventory"] = inventory
    summary["artifacts"] = sorted(set(summary["artifacts"]) | {"expected-event-chapters.jsonl", "expected-event-inventory-sources.json",
                                 "expected-event-acquisition-matrix.json", "expected-event-inventory-summary.json"})
    _write_json(summary_path, summary)
    return inventory


def _unit_page_identity(page: dict, episode: dict | None, url: str, tables: RawTables) -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from sekaisync.termindex import page_story_key

    result = {"status": "unknown", "reasons": [], "pipeline_story_key": page_story_key(dict(page, url=url))}
    if episode is None:
        result["reasons"].append("missing_unique_regional_unit_story_episode")
        return result
    identity = episode["identity"]
    match = re.fullmatch(r"web:([^:]+):([^:]+):unit_story:([^:]+)", page["id"])
    expected = "unit_story:" + identity["scenario_id"]
    if match is None:
        result["reasons"].append("unsupported_unit_story_page_id_codec")
        return result
    if (match[1] != page["source"] or normalize_language(match[2]) != page["language"]
            or match[3] != identity["scenario_id"] or result["pipeline_story_key"] != expected):
        result.update(status="mismatch", reasons=["page_id_locale_or_pipeline_scenario_mismatch"])
        return result
    canonical = page.get("canonical_key")
    if canonical:
        fields = canonical.split(":")
        if (len(fields) != 3 or fields[0] != "unit_story" or normalize_language(fields[1]) != page["language"]
                or fields[2] != identity["scenario_id"]):
            result.update(status="mismatch", reasons=["canonical_unit_story_scenario_mismatch"])
            return result
    path = unquote(urlsplit(url).path)
    if page["source"] == "altsource_ms":
        match = re.search(r"/story/unit/(\d+)/([^/]+)/?$", path)
        if match is None:
            result["reasons"].append("unsupported_ms_unit_story_url_codec")
            return result
        profiles = tables.load(Path(episode["path"]).with_name("unitProfiles.json"))
        result.update(profile_file=tables.evidence(profiles), profile_retrieval_sealed=False)
        if profiles.get("error"):
            result["reasons"].append(profiles["error"])
            return result
        matching = [row for row in profiles["records"] if row.get("unit") == identity["unit"]]
        sequences = {row.get("seq") for row in matching}
        if sequences != {identity["story_seq"]}:
            result["reasons"].append("missing_or_mismatched_unit_profile_sequence")
            return result
        if int(match[1]) != identity["story_seq"] or match[2] != identity["scenario_id"]:
            result.update(status="mismatch", reasons=["url_unit_sequence_or_episode_scenario_mismatch"])
            return result
        result["identity_basis"] = "sealed_nested_episode_plus_local_profile_sequence_url_and_pipeline_key"
    elif page["source"] == "altsource_sv":
        match = re.search(r"/scenario/unitstory/([^/]+)/([^/]+)\.(?:asset|json)$", path)
        if match is None:
            result["reasons"].append("unsupported_sv_unit_story_asset_url_codec")
            return result
        if (match[1], match[2]) != (identity["assetbundle_name"], identity["scenario_id"]):
            result.update(status="mismatch", reasons=["asset_url_unit_bundle_or_episode_scenario_mismatch"])
            return result
        result["identity_basis"] = "sealed_nested_episode_plus_exact_asset_bundle_scenario_and_pipeline_key"
    else:
        result["reasons"].append("unsupported_unit_story_source_identity_codec")
        return result
    result["status"] = "verified"
    return result


def _expected_unit_story_ledger(conn: sqlite3.Connection, index: sqlite3.Connection, as_of: datetime, out: Path) -> dict:
    verifier = UnitStoryVerifier(conn, as_of)
    expected = {}
    for language in LANGUAGES:
        for scenario, variants in verifier.records[language].items():
            expected.setdefault("unit_story:" + scenario, {})[language] = [
                {"episode_identity": row["identity"], "inventory_source_id": row["source_id"],
                 "release_condition_id": row["record"].get("releaseConditionId"),
                 "is_opening_episode": row["identity"]["episode_no"] == 1} for row in variants]
    page_columns = {row[1] for row in conn.execute("PRAGMA table_info(web_pages)")}
    url_column = "url" if "url" in page_columns else "''"
    urls = {(source, identity): url for source, identity, url in conn.execute("SELECT source,id," + url_column + " FROM web_pages WHERE kind='unit_story'")}
    observed, identity_counts = {}, Counter()
    with (out / "expected-unit-page-identity-audit.jsonl").open("w", encoding="utf-8") as handle:
        rows = index.execute("SELECT logical_key,metadata_json,good,primary_good FROM pages WHERE kind='unit_story' ORDER BY logical_key,language,source,id")
        for key, metadata, good, primary_good in rows:
            page = json.loads(metadata)
            language = page["language"]
            counters = observed.setdefault(key, {}).setdefault(language, Counter())
            counters.update(pages=1, good=good, primary_good=primary_good)
            episode, _ = verifier.unique(language, key.removeprefix("unit_story:")) if language in LANGUAGES else (None, [])
            audit = _unit_page_identity(page, episode, urls.get((page["source"], page["id"]), ""), verifier.tables)
            counters["identity_verified_primary_good"] += int(primary_good and audit["status"] == "verified")
            identity_counts[audit["status"]] += 1
            handle.write(json.dumps({"logical_key": key, "source": page["source"], "page_id": page["id"], "language": language,
                                     "primary_good_before_identity_audit": bool(primary_good), **audit}, ensure_ascii=False) + "\n")
    counts, releases, unlock_counts, matrix = Counter(), Counter(), Counter(), Counter()
    backfill_requests = []
    with (out / "expected-unit-stories.jsonl").open("w", encoding="utf-8") as handle:
        for key in sorted(set(expected) | set(observed)):
            definitions, acquired = expected.get(key, {}), observed.get(key, {})
            identities = {tuple(row["episode_identity"][field] for field in verifier.IDENTITY_FIELDS)
                          for values in definitions.values() for row in values}
            consistent = bool(identities) and len(identities) == 1
            five_identity = consistent and set(definitions) == set(LANGUAGES)
            opening = any(row["is_opening_episode"] for values in definitions.values() for row in values)
            states = {language: ("missing" if language not in acquired else "bad_only" if not acquired[language]["good"]
                      else "nonprimary_only" if not acquired[language]["primary_good"] else "identity_unverified_only"
                      if not acquired[language]["identity_verified_primary_good"] else "good_identity_present") for language in LANGUAGES}
            release = {language: verifier.release(language, key.removeprefix("unit_story:")) for language in LANGUAGES}
            statuses = [value["status"] for value in release.values()]
            release_status = ("all_five_released_in_verified_masterdata" if five_identity and all(value == "released_in_verified_masterdata" for value in statuses)
                              else "contains_not_yet_released" if "not_yet_released" in statuses else "unknown_release")
            unlock_reachable = five_identity and all(value.get("unlock", {}).get("status") == "reachable_in_locally_hashed_masterdata" for value in release.values())
            origin = "observed_and_masterdata" if key in expected and key in observed else "masterdata_only" if key in expected else "observed_only"
            row = {"logical_key": key, "origin": origin, "expected_definitions": definitions,
                   "identity_basis": "exact_nested_unit_chapter_episode_ids_and_scenario_not_scenario_string_alone",
                   "five_language_raw_identity_verified": five_identity, "cross_language_identity_conflict": len(identities) > 1,
                   "expected_inventory_languages": [language for language in LANGUAGES if language in definitions],
                   "missing_from_raw_inventory_languages": [language for language in LANGUAGES if language not in definitions],
                   "is_opening_episode": opening, "all_five_pages_absent": not acquired,
                   "acquired_languages": acquired, "acquisition_states": states,
                   "local_five_language_primary_complete_before_identity_audit": all(acquired.get(language, {}).get("primary_good", 0) for language in LANGUAGES),
                   "local_five_language_identity_verified_primary_complete": five_identity and all(state == "good_identity_present" for state in states.values()),
                   "all_five_unlock_definitions_reachable": unlock_reachable,
                   "unlock_reachability_is_not_publication_proof": True, "release_status": release_status, "release": release}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts[origin] += 1
            for field in ("five_language_raw_identity_verified", "cross_language_identity_conflict", "is_opening_episode", "all_five_pages_absent",
                          "local_five_language_primary_complete_before_identity_audit", "local_five_language_identity_verified_primary_complete", "all_five_unlock_definitions_reachable"):
                counts[field] += int(row[field])
            counts["all_five_absent_opening_episodes"] += int(opening and not acquired)
            counts["all_five_released_pages_absent"] += int(not acquired and release_status == "all_five_released_in_verified_masterdata")
            releases[release_status] += 1
            for language, value in release.items():
                unlock_counts[value.get("unlock", {}).get("status", "unknown_unlock")] += 1
                matrix[language, language in definitions, states[language], release_status] += 1
                if states[language] == "good_identity_present":
                    continue
                episode, reasons = verifier.unique(language, key.removeprefix("unit_story:"))
                if episode is None or not five_identity:
                    continue
                identity = episode["identity"]
                bundle, scenario = identity["assetbundle_name"], identity["scenario_id"]
                backfill_requests.append({"logical_key": key, "language": language, "region": REGIONS[language],
                                          "acquisition_state_before": states[language], "identity": identity,
                                          "inventory_source_id": episode["source_id"], "raw_file": episode["file"],
                                          "publication_status": value["status"], "is_opening_episode": opening,
                                          "ms_scenario_path": f"scenario/unitstory/{bundle}/{scenario}.json",
                                          "sv_asset_paths": [f"scenario/unitstory/{bundle}/{scenario}.asset"],
                                          "ms_canonical_story_path": f"story/unit/{identity['story_seq']}/{scenario}/",
                                          "expected_title": episode["record"].get("title"),
                                          "recovered_text_or_live_server_release_verified": False})
    _write_json(out / "expected-unit-inventory-sources.json", verifier.sources)
    _write_json(out / "expected-unit-acquisition-matrix.json", [{"language": language, "in_raw_masterdata_inventory": present,
                 "state": state, "release_status": status, "story_units": count}
                 for (language, present, state, status), count in sorted(matrix.items())])
    _write_json(out / "expected-unit-backfill-requests.json", {"schema": "sekaisync/p0-expected-unit-backfill@1",
                "production_database_mutated": False, "requested_locale_units": len(backfill_requests),
                "requirements": ["Fetch using existing public crawler APIs into a separate isolated store.",
                    "Validate exact scenario, localized content language, and raw nested episode identity before counting a recovered page.",
                    "Record fetch URL, retrieval time, response status, and raw response/text hashes.",
                    "Do not change the original observed census or frozen holdout snapshots; report recovery separately."],
                "requests": backfill_requests})
    summary = {"schema": "sekaisync/p0-expected-unit-story-inventory@1", "as_of_utc": as_of.astimezone(timezone.utc).isoformat(),
               "scope": "union_of_observed_unit_story_pages_and_all_nested_episodes_in_available_sealed_unitStories_tables_including_openings",
               "source_database_read_only": True, "production_bodies_loaded": False, "not_a_complete_all_domain_release_universe": True,
               "observed_story_units": len(observed), "masterdata_inventory_story_units": len(expected),
               "union_story_units": len(set(expected) | set(observed)), "counts": dict(counts),
               "release_status_counts": dict(releases), "regional_unlock_status_counts": dict(unlock_counts),
               "page_identity_audit_counts": dict(identity_counts), "inventory_source_count": len(verifier.sources),
               "inventory_sources_with_errors": sum(bool(row["errors"]) for row in verifier.sources),
               "invalid_identity_records": sum(len(row.get("invalid_identity_records", [])) for row in verifier.sources),
               "backfill_requested_locale_units": len(backfill_requests),
               "publication_requires_explicit_sealed_releaseAt_not_temporary_free_window": True,
               "old_observed_domain_release_counts_not_overwritten": True}
    _write_json(out / "expected-unit-inventory-summary.json", summary)
    return summary


def refresh_expected_unit_stories(store: Path, out: Path) -> dict:
    summary_path = out / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(summary["as_of_utc"])
    with closing(open_read_only(store / "kb/sekaisync.db")) as conn, closing(open_read_only(out / "census-index.sqlite")) as index:
        inventory = _expected_unit_story_ledger(conn, index, as_of, out)
    summary["expected_unit_story_inventory"] = inventory
    summary["artifacts"] = sorted(set(summary["artifacts"]) | {"expected-unit-stories.jsonl", "expected-unit-inventory-sources.json",
                                 "expected-unit-acquisition-matrix.json", "expected-unit-inventory-summary.json", "expected-unit-page-identity-audit.jsonl",
                                 "expected-unit-backfill-requests.json"})
    _write_json(summary_path, summary)
    return inventory


def _card_page_identity(page: dict, episode: dict | None, card: dict | None, url: str) -> dict:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from sekaisync.termindex import page_story_key

    story_key = page_story_key(dict(page, url=url))
    result = {"status": "unknown", "reasons": [], "pipeline_story_key": story_key}
    if episode is None or card is None:
        result["reasons"].append("missing_unique_regional_episode_or_parent_card")
        return result
    record, parent = episode["record"], card["record"]
    expected_key = f"card_story:{record['id']}"
    match = re.fullmatch(r"web:([^:]+):([^:]+):card_story:(\d+)", page["id"])
    if match is None:
        result["reasons"].append("unsupported_card_story_page_id_codec")
        return result
    if (int(match[3]) != record["id"] or match[1] != page["source"]
            or normalize_language(match[2]) != page["language"] or story_key != expected_key):
        result.update(status="mismatch", reasons=["page_id_locale_or_pipeline_key_mismatch"])
        return result
    canonical = page.get("canonical_key")
    if canonical:
        fields = canonical.split(":")
        if (len(fields) != 3 or fields[0] != "card_story" or normalize_language(fields[1]) != page["language"]
                or fields[2] != str(record["id"])):
            result.update(status="mismatch", reasons=["canonical_episode_key_mismatch"])
            return result
    path = unquote(urlsplit(url).path)
    if page["source"] == "altsource_ms":
        match = re.search(r"/story/card/(\d+)/?$", path)
        if match is None:
            result["reasons"].append("unsupported_ms_card_story_url_codec")
            return result
        if int(match[1]) != record["cardId"]:
            result.update(status="mismatch", reasons=["url_parent_card_foreign_key_mismatch"])
            return result
        result["identity_basis"] = "exact_episode_id_plus_url_card_foreign_key_and_pipeline_key"
    elif page["source"] == "altsource_sv":
        match = re.search(r"/character/member(?:_scenario)?/([^/]+)/([^/]+)\.(?:asset|json)$", path)
        if match is None:
            result["reasons"].append("unsupported_sv_card_story_asset_url_codec")
            return result
        if match[1] != parent.get("assetbundleName") or match[2] != record.get("scenarioId"):
            result.update(status="mismatch", reasons=["asset_url_card_bundle_or_episode_scenario_mismatch"])
            return result
        result["identity_basis"] = "exact_episode_id_plus_asset_bundle_scenario_and_pipeline_key"
    else:
        result["reasons"].append("unsupported_source_episode_identity_codec")
        return result
    result["status"] = "verified"
    return result


def _expected_card_story_ledger(conn: sqlite3.Connection, index: sqlite3.Connection, as_of: datetime, out: Path) -> dict:
    verifier = CardStoryVerifier(conn, as_of)
    expected = {}
    for language in LANGUAGES:
        for identity, variants in verifier.records[language]["cardEpisodes"].items():
            expected.setdefault(f"card_story:{identity}", {})[language] = [
                {"episode_record": {field: variant["record"].get(field) for field in
                                    ("id", "cardId", "scenarioId", "assetbundleName", "cardEpisodePartType", "releaseConditionId")},
                 "inventory_source_id": variant["source_id"]} for variant in variants]
    page_columns = {row[1] for row in conn.execute("PRAGMA table_info(web_pages)")}
    url_column = "url" if "url" in page_columns else "''"
    urls = {(source, identity): url for source, identity, url in conn.execute("SELECT source,id," + url_column + " FROM web_pages WHERE kind='card_story'")}
    observed, identity_counts = {}, Counter()
    with (out / "expected-card-page-identity-audit.jsonl").open("w", encoding="utf-8") as handle:
        rows = index.execute("SELECT logical_key,metadata_json,good,primary_good FROM pages WHERE kind='card_story' ORDER BY logical_key,language,source,id")
        for key, metadata, good, primary_good in rows:
            page = json.loads(metadata)
            language = page["language"]
            counts = observed.setdefault(key, {}).setdefault(language, Counter())
            counts.update(pages=1, good=good, primary_good=primary_good)
            match = CARD_STORY.fullmatch(key)
            episode, card = None, None
            if match and language in LANGUAGES:
                episode, _ = verifier.unique(language, "cardEpisodes", int(match[1]))
                if episode:
                    card, _ = verifier.unique(language, "cards", episode["record"].get("cardId"))
            audit = _card_page_identity(page, episode, card, urls.get((page["source"], page["id"]), ""))
            counts["identity_verified_primary_good"] += int(primary_good and audit["status"] == "verified")
            identity_counts[audit["status"]] += 1
            handle.write(json.dumps({"logical_key": key, "source": page["source"], "page_id": page["id"], "language": language,
                                     "primary_good_before_identity_audit": bool(primary_good), **audit}, ensure_ascii=False) + "\n")
    matrix, counts, release_counts = Counter(), Counter(), Counter()
    with (out / "expected-card-stories.jsonl").open("w", encoding="utf-8") as handle:
        for key in sorted(set(expected) | set(observed)):
            acquired, definitions = observed.get(key, {}), expected.get(key, {})
            origin = "observed_and_masterdata" if key in expected and key in observed else "masterdata_only" if key in expected else "observed_only"
            tuples = {tuple(row["episode_record"].get(field) for field in ("cardId", "scenarioId", "cardEpisodePartType"))
                      for values in definitions.values() for row in values}
            valid_tuple = all(isinstance(values[0], int) and not isinstance(values[0], bool) and values[0] > 0
                              and values[1] and values[2] in {"first_part", "second_part"} for values in tuples)
            identity_consistent = bool(tuples) and len(tuples) == 1 and valid_tuple
            states = {language: ("missing" if language not in acquired else "bad_only" if not acquired[language]["good"]
                      else "nonprimary_only" if not acquired[language]["primary_good"] else "identity_unverified_only"
                      if not acquired[language]["identity_verified_primary_good"] else "good_identity_present") for language in LANGUAGES}
            match = CARD_STORY.fullmatch(key)
            release = {language: verifier.release(int(match[1]), language) if match else
                       {"status": "unknown_release", "reasons": ["unresolved_exact_episode_identity"]} for language in LANGUAGES}
            statuses = [row["status"] for row in release.values()]
            release_status = ("all_five_released_in_verified_masterdata" if identity_consistent and len(definitions) == 5
                              and all(status == "released_in_verified_masterdata" for status in statuses) else
                              "contains_not_yet_released" if "not_yet_released" in statuses else "unknown_release")
            row = {"logical_key": key, "origin": origin, "expected_definitions": definitions,
                   "identity_basis": "exact_cardEpisodes_id_and_verified_cardId_scenarioId_partType_not_card_id_arithmetic",
                   "available_languages_identity_consistent": identity_consistent,
                   "five_language_raw_identity_verified": identity_consistent and len(definitions) == 5,
                   "cross_language_identity_conflict": len(tuples) > 1,
                   "expected_inventory_languages": [language for language in LANGUAGES if language in definitions],
                   "missing_from_raw_inventory_languages": [language for language in LANGUAGES if language not in definitions],
                   "acquired_languages": acquired, "acquisition_states": states, "all_five_pages_absent": not acquired,
                   "local_five_language_primary_complete_before_identity_audit": all(acquired.get(language, {}).get("primary_good", 0) for language in LANGUAGES),
                   "local_five_language_identity_verified_primary_complete": identity_consistent and len(definitions) == 5
                       and all(state == "good_identity_present" for state in states.values()),
                   "release_status": release_status, "release": release}
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            counts[origin] += 1
            for field in ("all_five_pages_absent", "five_language_raw_identity_verified", "cross_language_identity_conflict",
                          "local_five_language_primary_complete_before_identity_audit", "local_five_language_identity_verified_primary_complete"):
                counts[field] += int(row[field])
            counts["all_five_released_pages_absent"] += int(row["all_five_pages_absent"] and release_status == "all_five_released_in_verified_masterdata")
            release_counts[release_status] += 1
            for language, state in states.items():
                matrix[language, language in definitions, state, release_status] += 1
    _write_json(out / "expected-card-inventory-sources.json", verifier.sources)
    _write_json(out / "expected-card-acquisition-matrix.json", [{"language": language, "in_raw_masterdata_inventory": in_inventory,
                 "state": state, "release_status": status, "story_units": count}
                 for (language, in_inventory, state, status), count in sorted(matrix.items())])
    summary = {"schema": "sekaisync/p0-expected-card-story-inventory@1", "as_of_utc": as_of.astimezone(timezone.utc).isoformat(),
               "scope": "union_of_observed_card_story_pages_and_exact_cardEpisodes_records_in_available_sealed_raw_tables",
               "source_database_read_only": True, "production_bodies_loaded": False, "not_a_complete_all_domain_release_universe": True,
               "observed_story_units": len(observed), "masterdata_inventory_story_units": len(expected),
               "union_story_units": len(set(expected) | set(observed)), "counts": dict(counts),
               "release_status_counts": dict(release_counts), "page_identity_audit_counts": dict(identity_counts),
               "inventory_source_count": len(verifier.sources), "inventory_sources_with_errors": sum(bool(row["errors"]) for row in verifier.sources),
               "release_proof_scope": "sealed_card_and_episode_raw_records_plus_locally_hashed_unlock_definitions_not_live_server_audit",
               "old_observed_domain_release_counts_not_overwritten": True}
    _write_json(out / "expected-card-inventory-summary.json", summary)
    return summary


def refresh_expected_card_stories(store: Path, out: Path) -> dict:
    summary_path = out / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    as_of = datetime.fromisoformat(summary["as_of_utc"])
    with closing(open_read_only(store / "kb/sekaisync.db")) as conn, closing(open_read_only(out / "census-index.sqlite")) as index:
        inventory = _expected_card_story_ledger(conn, index, as_of, out)
    summary["expected_card_story_inventory"] = inventory
    summary["artifacts"] = sorted(set(summary["artifacts"]) | {"expected-card-stories.jsonl", "expected-card-inventory-sources.json",
                                 "expected-card-acquisition-matrix.json", "expected-card-inventory-summary.json", "expected-card-page-identity-audit.jsonl"})
    _write_json(summary_path, summary)
    return inventory


def select_holdout(event_units: list[dict], excluded_stories: list[str], seed: str, count: int) -> list[dict]:
    excluded = {content_family(key) for key in excluded_stories}
    pool = [unit for unit in event_units if unit["local_five_language_complete"]
            and content_family(unit["logical_key"].replace("event_story:", "event:", 1)) not in excluded
            and unit["release_status"] != "contains_not_yet_released"]
    pool.sort(key=lambda unit: (unit["release_status"] != "all_five_released_in_verified_masterdata",
                               hashlib.sha256((seed + "\0" + unit["logical_key"]).encode()).hexdigest()))
    result, families = [], set()
    for unit in pool:
        family = content_family(unit["logical_key"].replace("event_story:", "event:", 1))
        if family in families:
            continue
        families.add(family)
        result.append(unit)
        if len(result) == count:
            break
    if len(result) != count:
        raise ValueError(f"Only {len(result)} eligible content families for requested {count}-story holdout")
    return result


def holdout_identity(manifest: dict) -> dict:
    return {"seed": manifest["seed"], "as_of_utc": datetime.fromisoformat(manifest["as_of_utc"]).astimezone(timezone.utc).isoformat(),
            "excluded_content_families": manifest["excluded_content_families"],
            "stories": [{"story_key": story["story_key"], "release_status": story["release_status"],
                         "release": story["release"], "pages": {lang: {field: page.get(field)
                         for field in ("page_id", "source", "language", "text_sha256", "characters", "lines", "local_body_file")}
                         for lang, page in story["pages"].items()}} for story in manifest["stories"]]}


def _page_summaries(conn, index, logical_key: str, body_dir: Path | None = None) -> dict:
    result = {}
    for language in LANGUAGES:
        variants = list(index.execute("SELECT metadata_json FROM pages WHERE logical_key=? AND language=? ORDER BY source,id",
                                      (logical_key, language)))
        pages = [json.loads(row[0]) for row in variants]
        pages.sort(key=lambda page: (bool(page["bad_reasons"]), bool(page["nonprimary_flags"]),
                                     page["source"] != "altsource_ms", page["source"], page["id"]))
        usable = next((page for page in pages if not page["bad_reasons"] and not page["nonprimary_flags"]), None)
        if usable is None:
            result[language] = {"status": "missing_primary_body", "variants": pages}
            continue
        row = conn.execute("SELECT text FROM web_pages WHERE source=? AND id=?", (usable["source"], usable["id"])).fetchone()
        text = row[0]
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        expected = usable.get("text_hash")
        if expected and expected != digest:
            raise ValueError("Selected page text hash mismatch: " + usable["id"])
        summary = {"status": "present", "page_id": usable["id"], "source": usable["source"],
                   "language": language, "text_sha256": digest, "characters": len(text),
                   "lines": len(text.splitlines()), "variants": pages}
        if body_dir is not None:
            filename = hashlib.sha256((logical_key + "\0" + language + "\0" + digest).encode()).hexdigest() + ".txt"
            body_path = body_dir / filename
            if body_path.exists() and hashlib.sha256(body_path.read_bytes()).hexdigest() != digest:
                raise ValueError("Frozen local holdout body was modified: " + filename)
            if not body_path.exists():
                body_path.write_bytes(text.encode("utf-8"))
            summary["local_body_file"] = "holdout-pages/" + filename
        result[language] = summary
    return result


def census(store: Path, out: Path, baseline: Path, as_of: datetime, holdout_count: int = 8,
           seed: str = "sekaisync-p0-blind-20261001") -> dict:
    if as_of.tzinfo is None:
        raise ValueError("Census cutoff must be timezone-aware")
    as_of = as_of.astimezone(timezone.utc)
    out.mkdir(parents=True, exist_ok=True)
    excluded_stories = baseline_stories(baseline)
    with tempfile.NamedTemporaryFile(prefix="census-", suffix=".sqlite", dir=out, delete=False) as temporary:
        scratch = Path(temporary.name)
    try:
        with closing(open_read_only(store / "kb/sekaisync.db")) as conn, closing(sqlite3.connect(scratch)) as index:
            aggregates = _build_index(conn, index)
            _write_json(out / "source-domain-language-counts.json", aggregates)
            verifier = ReleaseVerifier(conn, as_of)
            domain_stats, coverage_matrix, events = _logical_ledger(index, verifier, out)
            expected_event_inventory = _expected_event_ledger(index, verifier, out)
            expected_card_inventory = _expected_card_story_ledger(conn, index, as_of, out)
            expected_unit_inventory = _expected_unit_story_ledger(conn, index, as_of, out)
            _write_json(out / "domain-census.json", domain_stats)
            _write_json(out / "acquisition-matrix.json", coverage_matrix)
            duplicate_matrix = [dict(zip(("kind", "language", "logical_units", "pages", "distinct_versions",
                                          "duplicate_pages", "conflicting_version_units"), row))
                                for row in index.execute("SELECT kind,language,COUNT(*),SUM(n),SUM(versions),SUM(n-versions),SUM(versions>1) "
                                "FROM (SELECT kind,language,logical_key,COUNT(*) n,COUNT(DISTINCT version_hash) versions "
                                "FROM pages GROUP BY kind,language,logical_key) GROUP BY kind,language ORDER BY kind,language")]
            _write_json(out / "duplicate-version-matrix.json", duplicate_matrix)
            bad_counts = Counter()
            for kind, lang, source, metadata in index.execute("SELECT kind,language,source,metadata_json FROM pages WHERE good=0"):
                for reason in json.loads(metadata)["bad_reasons"]:
                    bad_counts[kind, lang, source, reason] += 1
            _write_json(out / "bad-page-matrix.json", [{"kind": kind, "language": lang, "source": source,
                                                       "reason": reason, "pages": count}
                                                      for (kind, lang, source, reason), count in sorted(bad_counts.items())])
            selected = select_holdout(events, excluded_stories, seed, holdout_count)
            body_dir = out / "holdout-pages"
            body_dir.mkdir(exist_ok=True)
            stories = []
            for unit in selected:
                pages = _page_summaries(conn, index, unit["logical_key"], body_dir)
                if any(value["status"] != "present" for value in pages.values()):
                    raise ValueError("Holdout primary body incomplete: " + unit["logical_key"])
                stories.append({"story_key": unit["logical_key"].replace("event_story:", "event:", 1),
                                "logical_key": unit["logical_key"],
                                "release_status": unit["release_status"] if unit["release_status"] != "unknown_release"
                                else "local_complete_release_unknown", "release": unit["release"], "pages": pages})
            frozen = {"schema": "sekaisync/p0-blind-holdout@1", "seed": seed, "as_of_utc": as_of.isoformat(),
                      "source_database": str((store / "kb/sekaisync.db").resolve()),
                      "excluded_stories": excluded_stories,
                      "excluded_content_families": sorted({content_family(key) for key in excluded_stories}),
                      "sampling": "one chapter per unseen event family; verified release first; sha256 seed ordering",
                      "candidate_free": True, "gold_free": True, "stories": stories}
            frozen["manifest_sha256"] = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            frozen_path = out / "holdout-manifest.json"
            if frozen_path.exists():
                existing = json.loads(frozen_path.read_text(encoding="utf-8"))
                if holdout_identity(existing) != holdout_identity(frozen):
                    raise ValueError("Existing blind holdout changed; use a new output directory instead of silently replacing it")
                frozen = existing
                if frozen["as_of_utc"] != as_of.isoformat():
                    frozen["as_of_utc"] = as_of.isoformat()
                    frozen.pop("manifest_sha256", None)
                    frozen["manifest_sha256"] = hashlib.sha256(json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            _write_json(frozen_path, frozen)
            by_key = {unit["logical_key"]: unit for unit in events}
            original = []
            for story in excluded_stories:
                key = story.replace("event:", "event_story:", 1)
                unit = by_key.get(key)
                original.append({"story_key": story, "census": unit if unit else {"release_status": "unknown_release",
                                 "reason": "baseline_story_not_in_event_census"}, "pages": _page_summaries(conn, index, key)})
            _write_json(out / "baseline-release-evidence.json", {"as_of_utc": as_of.isoformat(), "stories": original})
            summary = {"schema": "sekaisync/scraper-corpus-census@1", "as_of_utc": as_of.isoformat(),
                       "source_database_read_only": True, "production_bodies_bulk_loaded": False,
                       "page_count": sum(item["page_count"] for item in aggregates),
                       "domain_count": len(domain_stats), "logical_unit_count": sum(item["logical_units"] for item in domain_stats),
                       "local_five_language_complete": sum(item["local_five_language_complete"] for item in domain_stats),
                       "baseline_story_count": len(excluded_stories), "holdout_story_count": len(stories),
                       "release_proof_scope": "hashed_local_masterdata_not_live_server_audit",
                       "unknown_release_domains": [item["kind"] for item in domain_stats if item["release_status_counts"].get("unknown_release")],
                       "holdout_manifest_sha256": frozen["manifest_sha256"],
                       "expected_event_inventory": expected_event_inventory,
                       "expected_card_story_inventory": expected_card_inventory,
                       "expected_unit_story_inventory": expected_unit_inventory,
                       "artifacts": ["census-index.sqlite", "logical-units.jsonl", "domain-census.json", "acquisition-matrix.json",
                                     "source-domain-language-counts.json", "bad-page-matrix.json", "duplicate-version-matrix.json",
                                     "baseline-release-evidence.json", "holdout-manifest.json", "expected-event-chapters.jsonl",
                                     "expected-event-inventory-sources.json", "expected-event-acquisition-matrix.json",
                                     "expected-event-inventory-summary.json", "expected-card-stories.jsonl",
                                     "expected-card-inventory-sources.json", "expected-card-acquisition-matrix.json",
                                     "expected-card-inventory-summary.json", "expected-card-page-identity-audit.jsonl",
                                     "expected-unit-stories.jsonl", "expected-unit-inventory-sources.json",
                                     "expected-unit-acquisition-matrix.json", "expected-unit-inventory-summary.json",
                                     "expected-unit-page-identity-audit.jsonl", "expected-unit-backfill-requests.json"]}
            _write_json(out / "summary.json", summary)
        scratch.replace(out / "census-index.sqlite")
        return summary
    except BaseException:
        # This is a newly-created research scratch file, never a source database.
        scratch.unlink(missing_ok=True)
        raise


def prepare_holdout_review(census_dir: Path, phase: str = "smoke") -> dict:
    """Import only frozen pages and invoke the existing scrub/review packet path."""
    sys.path.insert(0, str(ROOT))
    from sekaisync import agent_packets as ap, agent_review as ar, dbstore, termindex

    manifest = json.loads((census_dir / "holdout-manifest.json").read_text(encoding="utf-8"))
    if not manifest.get("candidate_free") or not manifest.get("gold_free"):
        raise ValueError("Holdout is not candidate-free and gold-free")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != manifest["manifest_sha256"]:
        raise ValueError("Frozen holdout manifest hash mismatch")
    pages = []
    for story in manifest["stories"]:
        for language in LANGUAGES:
            summary = story["pages"][language]
            path = (census_dir / summary["local_body_file"]).resolve()
            if not path.is_relative_to(census_dir.resolve()):
                raise ValueError("Frozen body path escapes census directory")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != summary["text_sha256"]:
                raise ValueError("Frozen holdout page changed: " + summary["page_id"])
            metadata = next(page for page in summary["variants"] if page["id"] == summary["page_id"]
                            and page["source"] == summary["source"])
            page = dict(metadata, text=raw.decode("utf-8"), story_key=story["story_key"])
            pages.append(page)
    target = census_dir / ("holdout-store-" + phase)
    target.mkdir(exist_ok=True)
    marker = target / "blind-store-manifest.json"
    identity = {"manifest_sha256": manifest["manifest_sha256"], "phase": phase,
                "page_count": len(pages), "gold_imported": False, "old_terms_imported": False}
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != identity:
            raise ValueError("Existing holdout store belongs to a different frozen corpus")
    elif any(target.iterdir()):
        raise ValueError("Refusing to reuse an unmarked nonempty holdout store")
    else:
        _write_json(marker, identity)
    dbstore.initialize(target)
    for source in sorted({page["source"] for page in pages}):
        dbstore.upsert_web_pages(target, source, [page for page in pages if page["source"] == source])
    groups = termindex.group_pages_by_story(pages)
    story_keys = [story["story_key"] for story in manifest["stories"]]
    if set(groups) != set(story_keys) or any(len(groups[key]) != 5 for key in story_keys):
        raise ValueError("Existing scrub page-usability gate dropped frozen holdout pages")
    chosen = story_keys[:1] if phase == "smoke" else story_keys
    packet_dir = census_dir / ("review-" + phase)
    packet_dir.mkdir(exist_ok=True)
    all_items, metrics = [], []
    for language in LANGUAGES:
        before = time.perf_counter()
        targets = [other for other in LANGUAGES if other != language]
        items, meta = ap._prepare_scrub_review(target, groups, chosen, [], {}, language, targets)
        if any(item.kind != "discovery" or item.candidates for item in items):
            raise ValueError("Blind discovery unexpectedly contains translation candidates")
        scope = ap._read_scope(target, meta["scope_id"])
        coverage = []
        for story in chosen:
            page = termindex._group_page(groups[story], language)
            text = page["text"]
            covered = bytearray(len(text))
            windows = [row for row in scope["windows"] if row["story_key"] == story]
            for row in windows:
                start, end = row["source"]["start"], row["source"]["end"]
                covered[start:end] = b"\1" * (end - start)
            gaps = [index for index, char in enumerate(text) if not char.isspace() and not covered[index]]
            if gaps:
                raise ValueError(f"Source coverage gap: {story}/{language}: {gaps[:20]}")
            coverage.append({"story_key": story, "page_id": page["id"], "characters": len(text),
                             "source_lines": len(text.splitlines()), "windows": len(windows),
                             "uncovered_nonspace_characters": len(gaps),
                             "incomplete_windows": sum(not row["source"]["complete"] for row in windows)})
        all_items.extend(items)
        _write_json(packet_dir / (language + ".json"), [item.to_dict() for item in items])
        (packet_dir / (language + ".txt")).write_text(
            ar.DECIDE_HELP + "\n\n" + "\n\n".join(ar.render_item(item, n + 1) for n, item in enumerate(items)) + "\n",
            encoding="utf-8")
        metrics.append({"language": language, "source_language_in_pipeline": scope["source_language"],
                        "elapsed_seconds": round(time.perf_counter() - before, 3), "stories": len(chosen),
                        "packets": len(items), "scope": meta, "coverage": coverage})
    enqueued = ar.enqueue(target, all_items)
    pending = ar.load_queue(target)
    export = ar.export_for_agent(target, packet_dir / "review-all.txt", limit=0)
    rendered = export.read_text(encoding="utf-8")
    if any("id=" + item.id + " kind=" + item.kind not in rendered for item in pending):
        raise ValueError("Public review export silently omitted discovery packets")
    with closing(open_read_only(target / "kb/sekaisync.db")) as conn:
        imported = conn.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
        term_count = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    if imported != len(pages) or entity_count:
        raise ValueError("Blind store import count/entity isolation failed")
    report = {"schema": "sekaisync/p0-symmetric-discovery@1", "phase": phase,
              "holdout_manifest_sha256": manifest["manifest_sha256"], "store": str(target.resolve()),
              "packet_directory": str(packet_dir.resolve()), "imported_pages": imported,
              "entity_count": entity_count, "current_terms": term_count,
              "old_terms_imported": False, "gold_imported": False, "story_language_jobs": len(chosen) * 5,
              "discovery_packets": len(all_items), "pending_review_packets": len(pending), "packet_char_budget": ap._DISCOVERY_CHARS,
              "no_silent_source_truncation": True, "export_limit": 0, "enqueue": enqueued,
              "languages": metrics}
    _write_json(packet_dir / "preparation-report.json", report)
    return report


def _reference_occurrences(text: str, surface: str, language: str) -> list[dict]:
    pattern = re.escape(surface)
    if language == "en":
        pattern = r"(?<!\w)" + pattern + r"(?!\w)"
    matcher = re.compile(pattern)
    turns, offset = [], 0
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        label = re.match(r"^[^\r\n:：]{1,48}[:：]", line)
        if label or not turns:
            turns.append({"start": offset, "end": offset + len(line), "speaker": label.group()[:-1] if label else "",
                          "body_start": offset + label.end() if label else offset, "lines": []})
        else:
            turns[-1]["end"] = offset + len(line)
        turns[-1]["lines"].append((number, offset, line, label.end() if label else 0))
        offset += len(line)
    result = []
    for turn in turns:
        for number, start, line, body_start in turn["lines"]:
            for match in matcher.finditer(line, body_start):
                result.append({"start": start + match.start(), "end": start + match.end(),
                               "physical_line": number, "line_start": start, "line_end": start + len(line),
                               "speaker": turn["speaker"], "line_body": line[body_start:].rstrip("\r\n"),
                               "turn_start": turn["start"], "turn_end": turn["end"],
                               "turn_body": text[turn["body_start"]:turn["end"]].rstrip("\r\n")})
    return result


def freeze_machine_reference(census_dir: Path, selection_path: Path | None = None) -> dict:
    """Bind a preselected independent reference to frozen text, never proposals."""
    selection_path = selection_path or census_dir / "holdout-reviewer-only/selection.json"
    selection_raw = selection_path.read_bytes()
    selection = json.loads(selection_raw)
    if not selection.get("machine_reference_not_human_gold") or selection.get("proposal_or_tokenizer_candidates_read") is not False:
        raise ValueError("Reviewer reference must declare independent machine-only selection")
    manifest = json.loads((census_dir / "holdout-manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != manifest["manifest_sha256"]:
        raise ValueError("Frozen holdout manifest hash mismatch")
    story = next(row for row in manifest["stories"] if row["story_key"] == selection["story_key"])
    bodies = {}
    for language in LANGUAGES:
        summary = story["pages"][language]
        path = (census_dir / summary["local_body_file"]).resolve()
        if not path.is_relative_to(census_dir.resolve()):
            raise ValueError("Frozen body path escapes census directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != summary["text_sha256"]:
            raise ValueError("Frozen reviewer source changed: " + summary["page_id"])
        bodies[language] = raw.decode("utf-8")
    annotations, relations, concept_ids = [], [], set()
    for concept in selection["concepts"]:
        if concept["id"] in concept_ids or set(concept["surfaces"]) != set(LANGUAGES):
            raise ValueError("Duplicate concept or incomplete reference languages")
        concept_ids.add(concept["id"])
        for language in LANGUAGES:
            surface = concept["surfaces"][language]
            if not surface or surface.strip() != surface:
                raise ValueError("Empty or padded reference surface")
            occurrences = _reference_occurrences(bodies[language], surface, language)
            if not occurrences:
                raise ValueError("Selected reference not found in source body: " + concept["id"] + "/" + language)
            if any(bodies[language][row["start"]:row["end"]] != surface for row in occurrences):
                raise ValueError("Reference source offset mismatch")
            summary = story["pages"][language]
            annotations.append({"id": concept["id"] + ":" + language, "concept_id": concept["id"],
                                "language": language, "surface": surface, "meaning": concept["meaning"],
                                "categories": concept["categories"], "note": concept["note"],
                                "page_id": summary["page_id"], "source": summary["source"],
                                "text_sha256": summary["text_sha256"], "primary_occurrence_index": 0,
                                "occurrences": occurrences})
        for source in LANGUAGES:
            for target in LANGUAGES:
                if source == target:
                    continue
                relation = (concept["english_pair_relation"] if "en" in (source, target)
                            and concept.get("english_pair_relation") else concept["relation"])
                relations.append({"concept_id": concept["id"], "source_language": source, "target_language": target,
                                  "source_annotation": concept["id"] + ":" + source,
                                  "target_annotation": concept["id"] + ":" + target, "relation_type": relation,
                                  "strict_lexical_counterpart": not relation.startswith("contextual_reframing"),
                                  "contextual_correspondence": True})
    reference = {"schema": "sekaisync/p0-independent-machine-reference@1", "reviewer": selection["reviewer"],
                 "story_key": story["story_key"], "machine_reference_not_human_gold": True,
                 "proposal_or_tokenizer_candidates_read": False, "scope": selection["scope"],
                 "full_source_read": selection["full_source_read"], "selection_sha256": hashlib.sha256(selection_raw).hexdigest(),
                 "holdout_manifest_sha256": manifest["manifest_sha256"],
                 "annotation_conventions": {"offset_unit": "python_unicode_code_points", "end_is_exclusive": True,
                    "speaker_labels_excluded": True, "english_complete_word_boundaries": True,
                    "korean_noun_spans_may_exclude_following_particles": True,
                    "all_exact_body_occurrences_recorded": True, "primary_occurrence": "first exact body occurrence",
                    "lexical_relations_are_context_scoped_not_global_synonym_assertions": True},
                 "annotations": annotations, "directed_relations": relations}
    reference["reference_sha256"] = hashlib.sha256(json.dumps(reference, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    destination = selection_path.parent / "reference.json"
    if destination.exists() and json.loads(destination.read_text(encoding="utf-8")) != reference:
        raise ValueError("Frozen machine reference changed; use a new reviewer directory")
    _write_json(destination, reference)
    report = {"reference_path": str(destination.resolve()), "reference_sha256": reference["reference_sha256"],
              "reference_file_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
              "concepts": len(selection["concepts"]), "term_slots": len(annotations),
              "body_occurrences": sum(len(row["occurrences"]) for row in annotations),
              "directed_relations": len(relations),
              "strict_lexical_relations": sum(row["strict_lexical_counterpart"] for row in relations),
              "contextual_reframing_relations": sum(not row["strict_lexical_counterpart"] for row in relations),
              "machine_reference_not_human_gold": True, "proposal_or_tokenizer_candidates_read": False}
    _write_json(selection_path.parent / "freeze-report.json", report)
    return report


def freeze_span_machine_reference(census_dir: Path, selection_path: Path) -> dict:
    """Freeze explicit primary spans, including inflected/discontinuous units."""
    raw_selection = selection_path.read_bytes()
    selection = json.loads(raw_selection)
    if selection.get("proposal_or_tokenizer_candidates_read") is not False or not selection.get("machine_reference_not_human_gold"):
        raise ValueError("Span reference must declare candidate-free independent machine selection")
    manifest = json.loads((census_dir / "holdout-manifest.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != manifest["manifest_sha256"]:
        raise ValueError("Frozen holdout manifest hash mismatch")
    story = next(row for row in manifest["stories"] if row["story_key"] == selection["story_key"])
    bodies = {}
    for language in LANGUAGES:
        page = story["pages"][language]
        path = (census_dir / page["local_body_file"]).resolve()
        if not path.is_relative_to(census_dir.resolve()):
            raise ValueError("Frozen span body path escapes census directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != page["text_sha256"]:
            raise ValueError("Frozen span reference body hash mismatch")
        body = raw.decode("utf-8")
        if len(body.splitlines()) != selection["full_source_read"][language]:
            raise ValueError("Full-source reading line count mismatch")
        bodies[language] = body
    annotations, relations, concept_ids = [], [], set()
    for concept in selection["concepts"]:
        if concept["id"] in concept_ids or set(concept["surfaces"]) != set(LANGUAGES) or set(concept["primary_lines"]) != set(LANGUAGES):
            raise ValueError("Duplicate concept or incomplete primary-span language definition")
        concept_ids.add(concept["id"])
        concept_annotations = {}
        for language in LANGUAGES:
            body, page = bodies[language], story["pages"][language]
            specified = concept.get("segments", {}).get(language)
            primary_line = concept["primary_lines"][language]
            if specified:
                occurrences = []
                for segment in specified:
                    matches = [row for row in _reference_occurrences(body, segment["exact"], language) if row["physical_line"] == segment["line"]]
                    index = segment.get("within_line_occurrence", 0)
                    if not 0 <= index < len(matches):
                        raise ValueError("Explicit segment missing at annotated line: " + concept["id"] + "/" + language)
                    occurrences.append(matches[index])
                if occurrences[0]["physical_line"] != primary_line or len({row["turn_start"] for row in occurrences}) != 1:
                    raise ValueError("Discontinuous segments must retain the annotated primary line and one speaker turn")
                if any(a["end"] > b["start"] for a, b in zip(occurrences, occurrences[1:])):
                    raise ValueError("Discontinuous primary segments are unordered or overlapping")
                groups = [occurrences]
                primary_index = 0
            else:
                occurrences = _reference_occurrences(body, concept["surfaces"][language], language)
                primary = [index for index, row in enumerate(occurrences) if row["physical_line"] == primary_line]
                if not primary:
                    raise ValueError("Contiguous reference missing at annotated primary line: " + concept["id"] + "/" + language)
                groups = [[row] for row in occurrences]
                primary_index = primary[0]
            span_groups = []
            for group in groups:
                segments = [{"start": row["start"], "end": row["end"], "exact": body[row["start"]:row["end"]],
                             "physical_line": row["physical_line"]} for row in group]
                span_groups.append({"segments": segments, "enclosing_start": group[0]["start"], "enclosing_end": group[-1]["end"],
                                    "enclosing_text": body[group[0]["start"]:group[-1]["end"]],
                                    "gap_texts": [body[a["end"]:b["start"]] for a, b in zip(group, group[1:])],
                                    "speaker": group[0]["speaker"], "turn_start": group[0]["turn_start"],
                                    "turn_end": group[0]["turn_end"], "turn_body": group[0]["turn_body"]})
            annotation = {"id": concept["id"] + ":" + language, "concept_id": concept["id"], "language": language,
                          "surface": concept["surfaces"][language], "surface_is_contiguous": not bool(specified),
                          "meaning": concept["meaning"], "categories": concept["categories"], "note": concept["note"],
                          "page_id": page["page_id"], "source": page["source"], "text_sha256": page["text_sha256"],
                          "primary_occurrence_index": primary_index, "span_groups": span_groups,
                          "occurrence_enumeration": "all_exact_contiguous_body_occurrences" if not specified else "explicit_primary_discontinuous_group_only"}
            annotations.append(annotation)
            concept_annotations[language] = annotation
        for source in LANGUAGES:
            for target in LANGUAGES:
                if source == target:
                    continue
                relation = concept["relation"]
                if any(language in concept.get("localized_reframing_languages", []) for language in (source, target)):
                    relation = "contextual_reframing_not_strict_lexical_equivalence"
                source_annotation, target_annotation = concept_annotations[source], concept_annotations[target]
                relations.append({"concept_id": concept["id"], "source_language": source, "target_language": target,
                                  "source_annotation": source_annotation["id"], "target_annotation": target_annotation["id"],
                                  "source_primary_segments": source_annotation["span_groups"][source_annotation["primary_occurrence_index"]]["segments"],
                                  "target_primary_segments": target_annotation["span_groups"][target_annotation["primary_occurrence_index"]]["segments"],
                                  "relation_type": relation, "strict_lexical_counterpart": relation.startswith("lexical_equivalent"),
                                  "contextual_correspondence": True})
    reference = {"schema": "sekaisync/p0-independent-machine-span-reference@2", "reviewer": selection["reviewer"],
                 "story_key": selection["story_key"], "scope": selection["scope"], "machine_reference_not_human_gold": True,
                 "proposal_or_tokenizer_candidates_read": False, "full_source_read": selection["full_source_read"],
                 "selection_sha256": hashlib.sha256(raw_selection).hexdigest(), "holdout_manifest_sha256": manifest["manifest_sha256"],
                 "annotation_conventions": {"offset_unit": "python_unicode_code_points", "end_is_exclusive": True,
                    "speaker_labels_excluded": True, "english_complete_word_boundaries": True,
                    "discontinuous_fragments_must_not_be_replaced_by_fabricated_contiguous_text": True,
                    "inflected_source_forms_retained_not_synthetic_dictionary_lemmas": True,
                    "primary_occurrence_is_manually_selected_not_automatic_first_match": True,
                    "korean_lexical_spans_may_exclude_particles_unless_selected_as_predicate": True,
                    "relations_are_context_scoped_not_global_synonym_assertions": True},
                 "annotations": annotations, "directed_relations": relations}
    reference["reference_sha256"] = hashlib.sha256(json.dumps(reference, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = selection_path.parent / "reference.json"
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) != reference:
        raise ValueError("Frozen span reference changed; use a new reviewer directory")
    _write_json(path, reference)
    report = {"story_key": selection["story_key"], "reference_path": str(path.resolve()),
              "reference_sha256": reference["reference_sha256"], "reference_file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
              "machine_reference_not_human_gold": True, "proposal_or_tokenizer_candidates_read": False,
              "concepts": len(concept_ids), "term_slots": len(annotations), "directed_relations": len(relations),
              "discontinuous_annotations": sum(not row["surface_is_contiguous"] for row in annotations),
              "full_source_read": selection["full_source_read"]}
    _write_json(selection_path.parent / "freeze-report.json", report)
    return report


def evaluate_span_proposal_requirements(census_dir: Path, reference_path: Path, proposal_path: Path,
                                        adjudication_path: Path) -> dict:
    """Audit frozen concept-table requirements without inventing occurrence claims."""
    reference = json.loads(reference_path.read_bytes())
    if reference.get("schema") != "sekaisync/p0-independent-machine-span-reference@2":
        raise ValueError("Proposal requirements need the independent span-v2 reference")
    unsigned = {key: value for key, value in reference.items() if key != "reference_sha256"}
    if hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != reference["reference_sha256"]:
        raise ValueError("Frozen span reference hash mismatch")
    proposal_raw, review_raw = proposal_path.read_bytes(), adjudication_path.read_bytes()
    proposal, review = json.loads(proposal_raw), json.loads(review_raw)
    proposal_sha256 = hashlib.sha256(proposal_raw).hexdigest()
    if proposal.get("story_key") != reference["story_key"] or proposal.get("reviewer_labels_used") is not False:
        raise ValueError("Concept proposal independence or story mismatch")
    if review.get("reference_sha256") != reference["reference_sha256"] or review.get("proposal_sha256") != proposal_sha256:
        raise ValueError("Semantic adjudication frozen-input hash mismatch")
    if review.get("story_key") != reference["story_key"] or review.get("machine_reference_not_human_gold") is not True:
        raise ValueError("Semantic adjudication scope mismatch")
    manifest = json.loads((census_dir / "holdout-manifest.json").read_bytes())
    unsigned_manifest = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if hashlib.sha256(json.dumps(unsigned_manifest, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != manifest["manifest_sha256"]:
        raise ValueError("Frozen holdout manifest hash mismatch")
    if manifest["manifest_sha256"] != reference["holdout_manifest_sha256"]:
        raise ValueError("Span reference holdout snapshot mismatch")
    story = next(row for row in manifest["stories"] if row["story_key"] == reference["story_key"])
    bodies = {}
    for language in LANGUAGES:
        page = story["pages"][language]
        path = (census_dir / page["local_body_file"]).resolve()
        if not path.is_relative_to(census_dir.resolve()):
            raise ValueError("Frozen span body path escapes census directory")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != page["text_sha256"]:
            raise ValueError("Frozen proposal requirement body hash mismatch")
        bodies[language] = raw.decode("utf-8")
    candidates, candidate_occurrences, grounding = {}, {}, []
    for concept in proposal["concepts"]:
        key = concept["key"]
        if key in candidates or set(concept.get("surfaces", {})) != set(LANGUAGES):
            raise ValueError("Duplicate proposal concept or incomplete languages")
        relations = concept.get("relations", {})
        if set(relations) - set(LANGUAGES) or any(value not in {"lexical", "paraphrase"} for value in relations.values()):
            raise ValueError("Unsupported surface-table relation kind")
        candidates[key] = concept
        for language, surface in concept["surfaces"].items():
            if not isinstance(surface, str) or not surface or surface.strip() != surface:
                raise ValueError("Empty or padded proposal surface")
            occurrences = _reference_occurrences(bodies[language], surface, language)
            candidate_occurrences[key, language] = occurrences
            grounding.append({"proposed_key": key, "language": language, "surface": surface,
                              "candidate_body_occurrences": len(occurrences),
                              "surface_exists_in_body": bool(occurrences), "occurrence_claim_submitted": False})
    annotation_map = {row["id"]: row for row in reference["annotations"]}
    expected_concepts = {row["concept_id"] for row in annotation_map.values()}
    reviews, proposed_keys = {}, set()
    for item in review["concepts"]:
        identity, key = item["reference_concept_id"], item.get("proposed_key")
        if identity in reviews or identity not in expected_concepts or not item.get("rationale"):
            raise ValueError("Invalid or unexplained semantic concept adjudication")
        if key is not None:
            if key not in candidates or key in proposed_keys or item.get("semantic_intent") != "aligned":
                raise ValueError("Semantic mapping must use unique existing aligned proposal concepts")
            requirements = item.get("complete_surface_requirement", {})
            if set(requirements) != set(LANGUAGES) or any(type(value) is not bool for value in requirements.values()):
                raise ValueError("Mapped concepts need explicit boolean requirements for every language")
            proposed_keys.add(key)
        elif item.get("semantic_intent") != "not_proposed" or item.get("complete_surface_requirement"):
            raise ValueError("Unproposed concepts cannot declare complete surface requirements")
        reviews[identity] = item
    if set(reviews) != expected_concepts:
        raise ValueError("Semantic adjudication must retain the entire independent concept denominator")
    details, detail_map = [], {}
    for annotation in reference["annotations"]:
        identity, language = annotation["concept_id"], annotation["language"]
        item, key = reviews[identity], reviews[identity].get("proposed_key")
        group = annotation["span_groups"][annotation["primary_occurrence_index"]]
        segments = group["segments"]
        surface = candidates[key]["surfaces"][language] if key is not None else None
        hits = candidate_occurrences.get((key, language), [])
        exact = bool(key is not None and len(segments) == 1 and surface == segments[0]["exact"])
        complete = bool(key is not None and item["complete_surface_requirement"][language])
        if complete and not hits:
            raise ValueError("Complete semantic surface requirement is not present in the frozen body")
        row = {"annotation_id": annotation["id"], "reference_concept_id": identity, "language": language,
               "reference_surface": annotation["surface"], "reference_primary_segments": segments,
               "proposed_key": key, "proposed_surface": surface, "semantic_intent": item["semantic_intent"],
               "complete_surface_requirement_met": complete, "exact_selected_expression": exact,
               "candidate_body_occurrences": len(hits),
               "candidate_occurrences_in_reference_primary_turn": sum(hit["turn_start"] == group["turn_start"] for hit in hits),
               "primary_occurrence_claim_submitted": False, "rationale": item["rationale"],
               "language_note": item.get("language_notes", {}).get(language)}
        details.append(row)
        detail_map[annotation["id"]] = row
    pairs = []
    for expected in reference["directed_relations"]:
        source, target = detail_map[expected["source_annotation"]], detail_map[expected["target_annotation"]]
        key = reviews[expected["concept_id"]].get("proposed_key")
        proposal_kind = None
        if key is not None:
            language_kinds = candidates[key].get("relations", {})
            proposal_kind = ("paraphrase" if any(language_kinds.get(language) == "paraphrase"
                             for language in (expected["source_language"], expected["target_language"])) else "lexical")
        required_kind = "lexical" if expected["strict_lexical_counterpart"] else "paraphrase"
        pairs.append({"reference_concept_id": expected["concept_id"],
                      "source_language": expected["source_language"], "target_language": expected["target_language"],
                      "semantic_intent_represented": key is not None,
                      "complete_surface_requirements_met": source["complete_surface_requirement_met"] and target["complete_surface_requirement_met"],
                      "exact_selected_expressions": source["exact_selected_expression"] and target["exact_selected_expression"],
                      "expected_relation_type": expected["relation_type"], "required_kind": required_kind,
                      "proposal_kind": proposal_kind, "relation_kind_aligned": proposal_kind == required_kind,
                      "occurrence_relation_submitted": False})
    complete_pairs = [row for row in pairs if row["complete_surface_requirements_met"]]
    concept_counts = Counter("not_proposed" if item["proposed_key"] is None else
                             "all_five_surface_requirements_met" if all(item["complete_surface_requirement"].values()) else
                             "intent_aligned_but_incomplete_surface_requirements" for item in reviews.values())
    report = {"schema": "sekaisync/p0-independent-machine-proposal-requirements@1", "story_key": reference["story_key"],
              "machine_reference_not_human_gold": True, "adjudication_read_proposals_after_reference_freeze": True,
              "scope": reference["scope"], "exhaustive_vocabulary_recall": "not_measured",
              "semantic_precision_of_all_proposals": "not_measured_from_sparse_reference",
              "reference_sha256": reference["reference_sha256"], "proposal_sha256": proposal_sha256,
              "adjudication_sha256": hashlib.sha256(review_raw).hexdigest(), "reference_concepts": len(reviews),
              "concept_counts": dict(concept_counts), "reference_annotations": len(details),
              "complete_surface_requirement_annotations": sum(row["complete_surface_requirement_met"] for row in details),
              "exact_selected_expression_annotations": sum(row["exact_selected_expression"] for row in details),
              "reference_directional_requirements": len(pairs),
              "intent_represented_directional_requirements": sum(row["semantic_intent_represented"] for row in pairs),
              "complete_surface_requirement_directional_pairs": len(complete_pairs),
              "kind_aligned_complete_surface_requirement_pairs": sum(row["relation_kind_aligned"] for row in complete_pairs),
              "exact_selected_expression_pairs": sum(row["exact_selected_expressions"] for row in pairs),
              "proposed_concepts": len(candidates), "unmapped_proposal_keys": sorted(set(candidates) - proposed_keys),
              "proposed_surface_slots": len(grounding), "body_grounded_surface_slots": sum(row["surface_exists_in_body"] for row in grounding),
              "occurrence_submissions_provided": False, "grounded_occurrence_success": "not_measured",
              "potential_pairs_are_not_submissions_or_persisted_relations": True,
              "exact_expression_metric_accepts_no_postfreeze_variants": True,
              "relation_kind_rule": "A table pair is paraphrase when either language declares paraphrase; otherwise lexical.",
              "concept_adjudication": review["concepts"], "annotations": details, "directional_requirements": pairs,
              "surface_grounding_diagnostics": grounding}
    output = adjudication_path.parent / "proposal-requirements-evaluation.json"
    if output.exists() and json.loads(output.read_bytes()) != report:
        raise ValueError("Frozen semantic requirement evaluation changed; use a separate diagnostic directory")
    _write_json(output, report)
    return report


def evaluate_machine_source(census_dir: Path, proposal_path: Path, variants_path: Path | None = None) -> dict:
    """Score only independently preselected source slots, not the whole vocabulary."""
    private = census_dir / "holdout-reviewer-only"
    reference = json.loads((private / "reference.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in reference.items() if key != "reference_sha256"}
    if hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != reference["reference_sha256"]:
        raise ValueError("Frozen machine reference hash mismatch")
    proposal_raw = proposal_path.read_bytes()
    proposal = json.loads(proposal_raw)
    if proposal["story_key"] != reference["story_key"] or set(proposal["surfaces"]) != set(LANGUAGES):
        raise ValueError("Source proposal story or language mismatch")
    manifest = json.loads((census_dir / "holdout-manifest.json").read_text(encoding="utf-8"))
    if manifest["manifest_sha256"] != reference["holdout_manifest_sha256"]:
        raise ValueError("Source evaluation holdout identity mismatch")
    story = next(row for row in manifest["stories"] if row["story_key"] == reference["story_key"])
    bodies, proposals, invalid = {}, {}, []
    for language in LANGUAGES:
        page = story["pages"][language]
        raw = (census_dir / page["local_body_file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != page["text_sha256"]:
            raise ValueError("Source evaluation frozen page hash mismatch")
        bodies[language] = raw.decode("utf-8")
        surfaces = proposal["surfaces"][language]
        if any(not isinstance(surface, str) or not surface.strip() for surface in surfaces) or len(surfaces) != len(set(surfaces)):
            raise ValueError("Empty or duplicate proposed source surface")
        proposals[language] = {}
        for surface in surfaces:
            occurrences = _reference_occurrences(bodies[language], surface, language)
            proposals[language][surface] = occurrences
            if not occurrences:
                invalid.append({"language": language, "surface": surface, "reason": "no_exact_body_occurrence"})
    variants_path = variants_path or private / "accepted-source-variants.json"
    variants, variant_sha256 = {}, None
    if variants_path.exists():
        raw = variants_path.read_bytes()
        variant_sha256 = hashlib.sha256(raw).hexdigest()
        for rule in json.loads(raw)["rules"]:
            variants.setdefault(rule["annotation_id"], []).append(rule)
    details, language_counts = [], {language: Counter() for language in LANGUAGES}
    for annotation in reference["annotations"]:
        language = annotation["language"]
        matches = []
        if annotation["surface"] in proposals[language]:
            matches = [{"surface": annotation["surface"], "rule": "exact_reference_surface",
                        "occurrences": proposals[language][annotation["surface"]]}]
        else:
            for rule in variants.get(annotation["id"], []):
                overlapping = [row for row in proposals[language].get(rule["surface"], [])
                    if any(ref["start"] <= row["start"] and row["end"] <= ref["end"] for ref in annotation["occurrences"])]
                if overlapping:
                    matches.append({"surface": rule["surface"], "rule": "reviewer_explicit_accepted_variant",
                                    "reason": rule["reason"], "occurrences": overlapping})
        status = ("exact_reference_surface" if matches and matches[0]["rule"] == "exact_reference_surface"
                  else "accepted_variant" if matches else "missing")
        language_counts[language][status] += 1
        details.append({"annotation_id": annotation["id"], "concept_id": annotation["concept_id"],
                        "language": language, "reference_surface": annotation["surface"], "status": status,
                        "page_id": annotation["page_id"], "text_sha256": annotation["text_sha256"],
                        "reference_occurrences": annotation["occurrences"], "proposed_matches": matches})
    exact = sum(row["status"] == "exact_reference_surface" for row in details)
    accepted = sum(row["status"] == "accepted_variant" for row in details)
    report = {"schema": "sekaisync/p0-independent-machine-source-evaluation@1",
              "story_key": reference["story_key"], "machine_reference_not_human_gold": True,
              "source_only_not_cross_language_mapping": True,
              "denominator_scope": reference["scope"], "exhaustive_vocabulary_recall": "not_measured",
              "semantic_precision": "not_measured_from_sparse_reference",
              "reference_sha256": reference["reference_sha256"], "proposal_file": str(proposal_path.resolve()),
              "proposal_sha256": hashlib.sha256(proposal_raw).hexdigest(), "accepted_variants_sha256": variant_sha256,
              "reference_term_slots": len(details), "exact_reference_surface_slots": exact,
              "accepted_variant_slots": accepted, "covered_slots": exact + accepted,
              "missing_slots": len(details) - exact - accepted,
              "proposed_source_surfaces": sum(len(value) for value in proposals.values()),
              "body_grounded_proposed_surfaces": sum(bool(rows) for surfaces in proposals.values() for rows in surfaces.values()),
              "ungrounded_proposed_surfaces": invalid,
              "languages": [{"language": language, "reference_term_slots": sum(language_counts[language].values()),
                             **dict(language_counts[language])} for language in LANGUAGES], "details": details}
    _write_json(private / "source-evaluation.json", report)
    return {key: value for key, value in report.items() if key not in {"details", "ungrounded_proposed_surfaces"}}


def _absolute_reference_segments(span: dict, segments: list[dict], page: dict, body: str) -> tuple[list[dict], list[str]]:
    errors, result = [], []
    if span["page_id"] != page["page_id"] or span["sha256"] != page["text_sha256"]:
        errors.append("packet_page_identity_mismatch")
    if body[span["start"]:span["end"]] != span["text"]:
        errors.append("packet_window_position_mismatch")
    for segment in segments:
        start, end = segment["start"], segment["end"]
        if not isinstance(start, int) or not isinstance(end, int) or not span["start"] <= start < end <= span["end"]:
            errors.append("segment_bounds_invalid")
            continue
        absolute = {"start": start, "end": end, "exact": segment["exact"]}
        if span["text"][start - span["start"]:end - span["start"]] != segment["exact"] or body[start:end] != segment["exact"]:
            errors.append("segment_exact_text_mismatch")
        result.append(absolute)
    if not result:
        errors.append("empty_segments")
    return result, errors


def evaluate_machine_target(census_dir: Path, proposal_path: Path, judgments_path: Path, tasks_path: Path) -> dict:
    private = census_dir / "holdout-reviewer-only"
    reference = json.loads((private / "reference.json").read_text(encoding="utf-8"))
    unsigned = {key: value for key, value in reference.items() if key != "reference_sha256"}
    if hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest() != reference["reference_sha256"]:
        raise ValueError("Frozen machine reference hash mismatch")
    proposal_raw, judgments_raw, tasks_raw = proposal_path.read_bytes(), judgments_path.read_bytes(), tasks_path.read_bytes()
    proposal, judgments, tasks = json.loads(proposal_raw), json.loads(judgments_raw), json.loads(tasks_raw)
    if proposal["story_key"] != reference["story_key"] or proposal.get("reviewer_labels_used") is not False:
        raise ValueError("Target proposal reference independence mismatch")
    review_raw = (private / "accepted-target-concepts.json").read_bytes()
    review = json.loads(review_raw)
    proposal_concepts = {row["key"]: row for row in proposal["concepts"]}
    mappings = review["concept_mappings"]
    if not set(mappings.values()) <= set(proposal_concepts) or len(set(mappings.values())) != len(mappings):
        raise ValueError("Invalid or ambiguous explicit concept mapping")
    manifest = json.loads((census_dir / "holdout-manifest.json").read_text(encoding="utf-8"))
    if manifest["manifest_sha256"] != reference["holdout_manifest_sha256"]:
        raise ValueError("Target evaluation holdout identity mismatch")
    story = next(row for row in manifest["stories"] if row["story_key"] == reference["story_key"])
    bodies = {}
    for language in LANGUAGES:
        raw = (census_dir / story["pages"][language]["local_body_file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != story["pages"][language]["text_sha256"]:
            raise ValueError("Target evaluation frozen page hash mismatch")
        bodies[language] = raw.decode("utf-8")
    by_task = {row["id"]: row for row in tasks}
    candidates = {}
    for judgment in judgments:
        task = by_task[judgment["id"]]
        source = normalize_language(task["review_context"]["source_language"])
        target = normalize_language(task["language"])
        rows = {row["id"]: row for row in task["review_context"]["rows"]}
        for relation in judgment.get("relations", []):
            row = rows[relation["evidence_id"]]
            source_segments, source_errors = _absolute_reference_segments(
                row["source"], relation["source_segments"], story["pages"][source], bodies[source])
            target_segments, target_errors = _absolute_reference_segments(
                row["target"], relation["target_segments"], story["pages"][target], bodies[target])
            candidates.setdefault((relation["sense_key"], source, target), []).append({
                "task_id": judgment["id"], "evidence_id": relation["evidence_id"], "decision": judgment["decision"],
                "relation_kind": relation["kind"], "source_segments": source_segments, "target_segments": target_segments,
                "position_errors": source_errors + target_errors})
    annotations = {row["id"]: row for row in reference["annotations"]}
    details, counts, direction_counts = [], Counter(), {}
    for expected in reference["directed_relations"]:
        concept, source, target = expected["concept_id"], expected["source_language"], expected["target_language"]
        key = mappings.get(concept)
        options = candidates.get((key, source, target), []) if key else []
        source_annotation, target_annotation = annotations[expected["source_annotation"]], annotations[expected["target_annotation"]]
        evaluated = []
        for option in options:
            exact_surface = (len(option["source_segments"]) == len(option["target_segments"]) == 1
                             and option["source_segments"][0]["exact"] == source_annotation["surface"]
                             and option["target_segments"][0]["exact"] == target_annotation["surface"])
            source_positions = {(row["start"], row["end"]) for row in source_annotation["occurrences"]}
            target_positions = {(row["start"], row["end"]) for row in target_annotation["occurrences"]}
            positions = lambda segments: {(row["start"], row["end"]) for row in segments}
            any_reference_occurrence = (exact_surface and positions(option["source_segments"]) <= source_positions
                                        and positions(option["target_segments"]) <= target_positions)
            source_primary = source_annotation["occurrences"][source_annotation["primary_occurrence_index"]]
            target_primary = target_annotation["occurrences"][target_annotation["primary_occurrence_index"]]
            primary = (any_reference_occurrence and positions(option["source_segments"]) == {(source_primary["start"], source_primary["end"])}
                       and positions(option["target_segments"]) == {(target_primary["start"], target_primary["end"])})
            if option["position_errors"] or option["decision"] != "accept":
                status = "wrong_position_or_invalid_evidence"
            elif not any_reference_occurrence:
                status = "unreviewed_surface_or_wrong_sense"
            elif expected["strict_lexical_counterpart"]:
                status = "correct_lexical" if option["relation_kind"] == "lexical" else "wrong_relation_kind"
            else:
                status = "correct_contextual_paraphrase" if option["relation_kind"] == "paraphrase" else "wrong_relation_kind"
            evaluated.append({**option, "status": status, "same_independent_primary_occurrences": primary,
                              "matches_any_independent_surface_occurrences": any_reference_occurrence})
        correct = [row for row in evaluated if row["status"] in {"correct_lexical", "correct_contextual_paraphrase"}]
        status = (correct[0]["status"] if correct else evaluated[0]["status"] if evaluated else
                  "unproposed_reference_concept" if not key else "missing_directed_proposal")
        counts[status] += 1
        counts["same_independent_primary_occurrences"] += int(any(row["same_independent_primary_occurrences"] for row in correct))
        direction_counts.setdefault((source, target), Counter())[status] += 1
        details.append({**expected, "proposal_concept_key": key, "status": status,
                        "unproposed_reason": review.get("unproposed_reference_concepts", {}).get(concept),
                        "evaluated_proposals": evaluated,
                        "same_independent_primary_occurrences": any(row["same_independent_primary_occurrences"] for row in correct)})
    report = {"schema": "sekaisync/p0-independent-machine-target-evaluation@1", "story_key": reference["story_key"],
              "machine_reference_not_human_gold": True, "denominator_scope": reference["scope"],
              "exhaustive_vocabulary_recall": "not_measured", "all_proposal_semantic_precision": "not_measured_from_sparse_reference",
              "reference_sha256": reference["reference_sha256"], "proposal_sha256": hashlib.sha256(proposal_raw).hexdigest(),
              "judgments_sha256": hashlib.sha256(judgments_raw).hexdigest(), "tasks_sha256": hashlib.sha256(tasks_raw).hexdigest(),
              "explicit_concept_review_sha256": hashlib.sha256(review_raw).hexdigest(),
              "reference_concepts": len({row["concept_id"] for row in reference["annotations"]}),
              "proposed_concepts": len(proposal_concepts), "shared_reviewed_concepts": len(mappings),
              "reference_directed_relations": len(details), "counts": dict(counts),
              "proposed_relations_total": sum(len(rows) for rows in candidates.values()),
              "unmapped_proposal_concepts": sorted(set(proposal_concepts) - set(mappings.values())),
              "directions": [{"source_language": source, "target_language": target, "reference_relations": sum(values.values()),
                              "counts": dict(values)} for (source, target), values in direction_counts.items()], "details": details}
    _write_json(private / "target-evaluation.json", report)
    return {key: value for key, value in report.items() if key not in {"details", "unmapped_proposal_concepts", "directions"}}


def prepare_label_assisted_diagnostic(census_dir: Path, submit: bool = False) -> dict:
    """Reuse discovered-source tasks for a separately labeled, non-blind rescue."""
    sys.path.insert(0, str(ROOT))
    from sekaisync import agent_review as ar

    private = census_dir / "holdout-reviewer-only"
    reference = json.loads((private / "reference.json").read_text(encoding="utf-8"))
    baseline_raw = (private / "target-evaluation.json").read_bytes()
    baseline = json.loads(baseline_raw)
    source_report = json.loads((private / "source-evaluation.json").read_text(encoding="utf-8"))
    if baseline["reference_sha256"] != reference["reference_sha256"] or source_report["reference_sha256"] != reference["reference_sha256"]:
        raise ValueError("Label-assisted diagnostic reference snapshot mismatch")
    discovered = {row["annotation_id"] for row in source_report["details"] if row["status"] != "missing"}
    annotations = {row["id"]: row for row in reference["annotations"]}
    missing = [row for row in baseline["details"] if row["status"] == "unproposed_reference_concept"]
    diagnostic = census_dir / "label-assisted-diagnostic"
    diagnostic.mkdir(exist_ok=True)
    store = census_dir / "holdout-store-smoke"
    tasks_path = diagnostic / "tasks.json"
    if tasks_path.exists():
        existing = json.loads(tasks_path.read_text(encoding="utf-8"))
    else:
        existing = [item.to_dict() for item in ar.load_queue(store)]
    tasks, judgments, unresolved = [], [], []
    for expected in missing:
        source, target = expected["source_language"], expected["target_language"]
        source_annotation, target_annotation = annotations[expected["source_annotation"]], annotations[expected["target_annotation"]]
        if source_annotation["id"] not in discovered:
            unresolved.append({"concept_id": expected["concept_id"], "source": source, "target": target,
                               "reason": "source_surface_not_independently_discovered"})
            continue
        matches = [task for task in existing if task.get("term") == source_annotation["surface"]
                   and normalize_language(task.get("language", "")) == target
                   and normalize_language(task.get("review_context", {}).get("source_language", "")) == source
                   and task.get("review_context", {}).get("task") == "occurrence"]
        if len(matches) != 1:
            unresolved.append({"concept_id": expected["concept_id"], "source": source, "target": target,
                               "reason": "existing_occurrence_task_missing_or_ambiguous", "matching_tasks": len(matches)})
            continue
        task = matches[0]
        source_primary = source_annotation["occurrences"][source_annotation["primary_occurrence_index"]]
        target_primary = target_annotation["occurrences"][target_annotation["primary_occurrence_index"]]
        row = next((row for row in task["review_context"]["rows"]
                    if row.get("story_key", reference["story_key"]) == reference["story_key"]
                    and row["source"]["start"] <= source_primary["start"] < source_primary["end"] <= row["source"]["end"]
                    and row["target"]["start"] <= target_primary["start"] < target_primary["end"] <= row["target"]["end"]), None)
        if row is None:
            unresolved.append({"concept_id": expected["concept_id"], "source": source, "target": target,
                               "reason": "bounded_packet_does_not_cover_independent_occurrences", "task_id": task["id"]})
            continue
        segments = {role + "_segments": [{"start": occurrence["start"], "end": occurrence["end"], "exact": annotation["surface"]}]
                    for role, occurrence, annotation in (("source", source_primary, source_annotation), ("target", target_primary, target_annotation))}
        for role in ("source", "target"):
            segment = segments[role + "_segments"][0]
            span = row[role]
            annotation = source_annotation if role == "source" else target_annotation
            if span["page_id"] != annotation["page_id"] or span["sha256"] != annotation["text_sha256"]:
                raise ValueError("Label-assisted diagnostic frozen page identity mismatch")
            if span["text"][segment["start"] - span["start"]:segment["end"] - span["start"]] != segment["exact"]:
                raise ValueError("Label-assisted segment not grounded in the existing packet")
        tasks.append(task)
        judgments.append({"id": task["id"], "decision": "accept", "agent": "p0-discovery-a-label-assisted",
                          "label_assisted_diagnostic": True, "relations": [{"evidence_id": row["id"], **segments,
                          "sense_key": "label-assisted:" + expected["concept_id"], "sense_gloss": source_annotation["meaning"],
                          "kind": "lexical" if expected["strict_lexical_counterpart"] else "paraphrase",
                          "rationale": "label_assisted_diagnostic: independently selected machine reference revealed after blind proposals froze; reuses an already discovered source term and existing task. Not a new blind gain or a global synonym assertion."}]})
    judgments_path = diagnostic / "judgments.json"
    for path, value in ((tasks_path, tasks), (judgments_path, judgments)):
        if path.exists() and json.loads(path.read_text(encoding="utf-8")) != value:
            raise ValueError("Frozen label-assisted diagnostic changed")
        _write_json(path, value)
    report = {"schema": "sekaisync/p0-label-assisted-diagnostic@1", "machine_reference_not_human_gold": True,
              "label_assisted_not_blind": True, "blind_score_gain": 0, "new_source_terms_added": 0, "new_tasks_created": 0,
              "eligible_directed_relations": len(missing), "existing_occurrence_tasks_reused": len(tasks),
              "prepared_judgments": len(judgments), "unresolved": unresolved, "reference_sha256": reference["reference_sha256"],
              "blind_target_evaluation_sha256": hashlib.sha256(baseline_raw).hexdigest(),
              "source_proposal_sha256": source_report["proposal_sha256"],
              "tasks_sha256": hashlib.sha256(tasks_path.read_bytes()).hexdigest(),
              "judgments_sha256": hashlib.sha256(judgments_path.read_bytes()).hexdigest(),
              "store": str(store.resolve()), "judgments_path": str(judgments_path.resolve())}
    if submit:
        report["submission"] = ar.submit_judgments(store, judgments)
    if (private / "target-evaluation.json").read_bytes() != baseline_raw:
        raise ValueError("Blind evaluation snapshot changed during the diagnostic")
    _write_json(diagnostic / "report.json", report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=ROOT / "store")
    parser.add_argument("--out", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census")
    parser.add_argument("--baseline-manifest", type=Path, default=ROOT / "work/p0-exhaustive-20261001/discovery-manifest.json")
    parser.add_argument("--as-of", default="2026-10-01T00:00:00+08:00", help="Explicit timezone-aware census cutoff")
    parser.add_argument("--holdout-count", type=int, default=8)
    parser.add_argument("--seed", default="sekaisync-p0-blind-20261001")
    parser.add_argument("--prepare-review", choices=("smoke", "full"),
                        help="Use the existing frozen manifest only; import an isolated store and export symmetric discovery")
    parser.add_argument("--freeze-reviewer-reference", action="store_true",
                        help="Bind the independent reviewer-only selection to frozen source occurrences")
    parser.add_argument("--reference-selection", type=Path,
                        help="Explicit independent span-selection file for a separate reviewer freeze")
    parser.add_argument("--evaluate-source", type=Path,
                        help="Evaluate a frozen source-surface proposal against the sparse private machine reference")
    parser.add_argument("--evaluate-target", type=Path, help="Frozen semantic concept proposal file")
    parser.add_argument("--target-judgments", type=Path, help="Frozen occurrence judgments for target evaluation")
    parser.add_argument("--target-tasks", type=Path, help="Frozen original occurrence tasks for target evaluation")
    parser.add_argument("--evaluate-span-proposal", type=Path,
                        help="Evaluate a frozen semantic concept table without claiming occurrence submissions")
    parser.add_argument("--span-reference", type=Path, help="Explicit independent span-v2 reference file")
    parser.add_argument("--semantic-adjudication", type=Path, help="Frozen manual semantic concept/requirement mapping")
    parser.add_argument("--refresh-expected-events", action="store_true",
                        help="Use an existing read-only census index to add the masterdata-derived event acquisition denominator")
    parser.add_argument("--refresh-expected-card-stories", action="store_true",
                        help="Verify exact card episode IDs, page identities, and release evidence against a read-only census index")
    parser.add_argument("--refresh-expected-unit-stories", action="store_true",
                        help="Include all sealed unit-story episodes and audit identity, unlocking, and publication separately")
    parser.add_argument("--label-assisted-diagnostic", action="store_true",
                        help="Reuse existing occurrence tasks for explicitly labeled non-blind reference-assisted diagnostics")
    parser.add_argument("--submit-label-assisted", action="store_true",
                        help="Submit the separate label-assisted diagnostic through the existing review API")
    args = parser.parse_args()
    if args.refresh_expected_unit_stories:
        print(json.dumps(refresh_expected_unit_stories(args.store, args.out), ensure_ascii=False, indent=2))
        return
    if args.evaluate_span_proposal:
        if not args.span_reference or not args.semantic_adjudication:
            parser.error("--evaluate-span-proposal requires --span-reference and --semantic-adjudication")
        print(json.dumps(evaluate_span_proposal_requirements(args.out, args.span_reference, args.evaluate_span_proposal,
                                                           args.semantic_adjudication), ensure_ascii=False, indent=2))
        return
    if args.refresh_expected_card_stories:
        print(json.dumps(refresh_expected_card_stories(args.store, args.out), ensure_ascii=False, indent=2))
        return
    if args.label_assisted_diagnostic:
        print(json.dumps(prepare_label_assisted_diagnostic(args.out, args.submit_label_assisted), ensure_ascii=False, indent=2))
        return
    if args.submit_label_assisted:
        parser.error("--submit-label-assisted requires --label-assisted-diagnostic")
    if args.refresh_expected_events:
        print(json.dumps(refresh_expected_events(args.store, args.out), ensure_ascii=False, indent=2))
        return
    if args.evaluate_target:
        if not args.target_judgments or not args.target_tasks:
            parser.error("--evaluate-target requires --target-judgments and --target-tasks")
        print(json.dumps(evaluate_machine_target(args.out, args.evaluate_target, args.target_judgments, args.target_tasks),
                         ensure_ascii=False, indent=2))
        return
    if args.evaluate_source:
        print(json.dumps(evaluate_machine_source(args.out, args.evaluate_source), ensure_ascii=False, indent=2))
        return
    if args.freeze_reviewer_reference:
        result = (freeze_span_machine_reference(args.out, args.reference_selection) if args.reference_selection
                  else freeze_machine_reference(args.out))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if args.prepare_review:
        print(json.dumps(prepare_holdout_review(args.out, args.prepare_review), ensure_ascii=False, indent=2))
        return
    cutoff = datetime.fromisoformat(args.as_of)
    if cutoff.tzinfo is None or args.holdout_count < 1:
        parser.error("--as-of must have a timezone and --holdout-count must be positive")
    print(json.dumps(census(args.store, args.out, args.baseline_manifest, cutoff, args.holdout_count, args.seed),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
