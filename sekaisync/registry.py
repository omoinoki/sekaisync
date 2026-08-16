from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable, Optional

from sekaisync.config import REGIONS
from sekaisync.layout import region_master_dir
from sekaisync.models import Entity
from sekaisync.normalize import best_match, normalize_name
from sekaisync.trust import trust_for_source


_ID_FIELDS = [
    "id",
    "seq",
    "characterId",
    "cardId",
    "musicId",
    "eventId",
    "gachaId",
    "areaId",
    "virtualLiveId",
    "stampId",
    "missionId",
    "itemId",
    "unitId",
]

_NAME_FIELDS = [
    "name",
    "assetName",
    "firstName",
    "lastName",
    "firstNameEnglish",
    "givenNameEnglish",
    "lastNameEnglish",
    "familyNameEnglish",
    "givenName",
    "familyName",
    "fullName",
    "characterName",
    "unitName",
    "songName",
    "eventName",
    "cardName",
    "title",
    "unitProfileName",
]

_FACT_FIELDS = [
    "rarity",
    "rarityId",
    "attribute",
    "skill",
    "skillName",
    "unit",
    "birthday",
    "birthDate",
    "height",
    "school",
    "grade",
    "lyricist",
    "composer",
    "arranger",
    "bpm",
    "difficulty",
    "noteCount",
    "startAt",
    "startTime",
    "endAt",
    "endTime",
    "eventType",
    "type",
    "releaseAt",
    "publishedAt",
    "outline",
    "profileSentence",
    "profile",
    "description",
    "summary",
    "catchCopy",
]

LANGUAGE_TEXT_FIELDS = {
    "outline",
    "profileSentence",
    "profile",
    "description",
    "summary",
    "catchCopy",
}

_KIND_ALIASES = {
    "gameCharacters": "character",
    "gameCharacterUnits": "character_unit",
    "unitProfiles": "unit",
    "cards": "card",
    "cardEpisodes": "card_episode",
    "characterProfiles": "character_profile",
    "musics": "song",
    "musicDifficulties": "music_difficulty",
    "musicVocals": "music_vocal",
    "events": "event",
    "eventStories": "event_story",
    "unitStories": "unit_story",
    "specialStories": "special_story",
    "gachas": "gacha",
    "areas": "area",
    "areaItems": "area_item",
    "virtualLives": "virtual_live",
    "stamps": "stamp",
    "missions": "mission",
    "items": "item",
}

REGISTRY_TABLES = frozenset(_KIND_ALIASES)


def data_files_for_region(store_root: Path, region: str) -> list[Path]:
    base = region_master_dir(store_root, region)
    candidates = [
        base / "versions" / "**" / "*.json",
        base / "master" / "*.json",
        base / "db" / "*.json",
        base / "*" / "*.json",
        base / "*.json",
    ]
    seen: dict[Path, None] = {}
    for pattern in candidates:
        relative_pattern = str(pattern.relative_to(base))
        for path in sorted(base.glob(relative_pattern)):
            if path.name in {"manifest.json", "versions.json"}:
                continue
            seen.setdefault(path, None)
    return list(seen)


def load_records(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("records", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def kind_from_path(path: Path) -> str:
    name = path.name.removesuffix(".json")
    return _KIND_ALIASES.get(name, name)


def record_id(record: dict, kind: str) -> str:
    for key in _ID_FIELDS:
        value = record.get(key)
        if value is not None:
            return str(value)
    if kind in record:
        return str(record[kind])
    digest = hashlib.sha1(json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:12]
    return f"unknown-{digest}"


def extract_names(record: dict) -> dict[str, str]:
    names: dict[str, str] = {}
    if isinstance(record.get("names"), dict):
        for language, value in record["names"].items():
            if value and isinstance(value, str):
                names[language] = value
    first = record.get("firstName")
    given = record.get("givenName")
    last = record.get("lastName") or record.get("familyName")
    if first and given:
        names["full"] = f"{first}{given}"
    elif first and last:
        names["full"] = f"{first} {last}"
    elif given and last:
        names["full"] = f"{last} {given}"
    en_first = record.get("firstNameEnglish")
    en_given = record.get("givenNameEnglish")
    en_last = record.get("lastNameEnglish") or record.get("familyNameEnglish")
    if en_first and en_given:
        names["en"] = f"{en_first} {en_given}"
    elif en_first and en_last:
        names["en"] = f"{en_first} {en_last}"
    for field in _NAME_FIELDS:
        value = record.get(field)
        if value and isinstance(value, str):
            names.setdefault(field, value)
    return names


def extract_facts(record: dict, names: dict[str, str], language: str = "ja") -> dict:
    facts = {}
    for field in _FACT_FIELDS:
        value = record.get(field)
        if value not in (None, "", [], {}):
            if field in LANGUAGE_TEXT_FIELDS:
                facts[f"{field}_{language}"] = value
            else:
                facts[field] = value
    if "character" in names:
        facts.setdefault("character", names["character"])
    return facts


def _story_metadata(
    store_root: Path, regions: Iterable[str]
) -> dict[str, list[dict]]:
    """Collect event outline and episode titles from eventStories tables."""
    metadata: dict[str, list[dict]] = {}
    for region in regions:
        language = REGIONS[region].language if region in REGIONS else "ja"
        for path in data_files_for_region(store_root, region):
            table = path.name.removesuffix(".json")
            if table != "eventStories":
                continue
            for record in load_records(path):
                event_id = str(record.get("eventId") or record.get("id") or "")
                if not event_id:
                    continue
                metadata.setdefault(event_id, []).append(
                    {
                        "language": language,
                        "outline": record.get("outline"),
                        "episodes": record.get("eventStoryEpisodes") or [],
                    }
                )
    return metadata


def _enrich_event_stories(entities: list[Entity], metadata: dict[str, list[dict]]) -> None:
    events = {entity.id: entity for entity in entities if entity.type == "event"}
    stories = {entity.id: entity for entity in entities if entity.type == "event_story"}
    for event_id, records in metadata.items():
        event = events.get(f"event:{event_id}")
        story = stories.get(f"event_story:{event_id}")
        for record in records:
            language = record["language"]
            if record["outline"] and event is not None:
                event.facts.setdefault(f"outline_{language}", record["outline"])
            for episode in record["episodes"]:
                title = episode.get("title")
                if not title or story is None:
                    continue
                episode_no = episode.get("episodeNo")
                if episode_no is not None:
                    story.names.setdefault(f"episode{episode_no}_{language}", str(title))
                story.names.setdefault(language, str(title))


def build_registry(store_root: Path, regions: Iterable[str]) -> list[Entity]:
    grouped: dict[str, Entity] = {}

    for region in regions:
        for path in data_files_for_region(store_root, region):
            table = path.name.removesuffix(".json")
            if table not in REGISTRY_TABLES:
                continue
            kind = kind_from_path(path)
            for record in load_records(path):
                game_id = record_id(record, kind)
                if region == "demo":
                    entity_id = f"demo:{kind}:{game_id}"
                else:
                    entity_id = f"{kind}:{game_id}"
                if region in REGIONS:
                    language = REGIONS[region].language
                else:
                    language = "ja"
                names = extract_names(record)
                facts = extract_facts(record, names, language=language)
                names = {k: v for k, v in names.items() if v}
                fallback_name = (
                    names.get("full")
                    or names.get("name")
                    or names.get("unitName")
                    or names.get("unitProfileName")
                    or names.get("songName")
                    or names.get("eventName")
                    or names.get("cardName")
                    or names.get("title")
                    or ""
                )
                names.setdefault(language, fallback_name)

                existing = grouped.get(entity_id)
                if existing is None:
                    grouped[entity_id] = Entity(
                        id=entity_id,
                        type=kind,
                        region=region,
                        regions=[region],
                        names=names,
                        facts=facts,
                        source=f"master_db:{region}",
                        version=None,
                        demo=(region == "demo"),
                        trust=trust_for_source(
                            f"master_db:{region}",
                            kind=kind,
                            demo=(region == "demo"),
                        ),
                    )
                else:
                    existing.regions.append(region)
                    for language, value in names.items():
                        existing.names.setdefault(language, value)
                    for key, value in facts.items():
                        existing.facts.setdefault(key, value)
                    existing.demo = existing.demo or region == "demo"

    entities = list(grouped.values())
    _enrich_event_stories(entities, _story_metadata(store_root, regions))
    return entities


def save_registry(entities: Iterable[Entity], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "id": entity.id,
            "type": entity.type,
            "region": entity.region,
            "regions": entity.regions,
            "names": entity.names,
            "facts": entity.facts,
            "source": entity.source,
            "version": entity.version,
            "demo": entity.demo,
            "trust": entity.trust or trust_for_source(
                entity.source,
                kind=entity.type,
                demo=entity.demo,
            ),
        }
        for entity in entities
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_registry(path: Path) -> list[Entity]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        Entity(
            id=str(item["id"]),
            type=str(item.get("type", "")),
            region=str(item.get("region", "")),
            regions=[str(r) for r in item.get("regions", [])],
            names={k: str(v) for k, v in item.get("names", {}).items() if v},
            facts={k: v for k, v in item.get("facts", {}).items() if v not in (None, "")},
            source=str(item.get("source", "")),
            version=item.get("version"),
            demo=bool(item.get("demo", False)),
            trust=str(
                item.get("trust")
                or trust_for_source(
                    str(item.get("source", "")),
                    kind=str(item.get("type", "")),
                    demo=bool(item.get("demo", False)),
                )
            ),
        )
        for item in data
    ]


def lookup_entity(
    entities: Iterable[Entity],
    query: str,
    type: Optional[str] = None,
    region: Optional[str] = None,
    language: Optional[str] = None,
    limit: int = 8,
) -> list[tuple[Entity, int]]:
    query_key = normalize_name(query)
    scored: list[tuple[Entity, int]] = []
    for entity in entities:
        if type and entity.type != type:
            continue
        if region and region not in entity.regions:
            continue
        names = list(entity.names.values())
        if entity.canonical_name not in names:
            names.append(entity.canonical_name)
        fact_values = [
            str(value)
            for value in entity.facts.values()
            if isinstance(value, (str, int, float))
            and not str(value).isdigit()
        ]
        all_texts = names + fact_values
        matched = best_match(query, all_texts)
        id_suffix = normalize_name(str(entity.id).rsplit(":", 1)[-1])
        id_score = 100 if query_key and id_suffix == query_key else 0
        name, score = (entity.id, id_score) if matched is None else matched
        if matched is not None and id_score >= score:
            name, score = entity.id, id_score
        if matched is None and id_score == 0:
            continue
        if name in fact_values:
            score = max(1, score - 10)
        if language and entity.names.get(language) == query:
            score += 10
        scored.append((entity, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def entity_by_id(entities: Iterable[Entity], entity_id: str) -> Optional[Entity]:
    for entity in entities:
        if entity.id == entity_id:
            return entity
    return None
