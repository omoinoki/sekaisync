from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping, Optional

from sekaisync.config import REGIONS
from sekaisync.layout import generation_master_dir, region_master_dir
from sekaisync.models import Entity, RegionFacts
from sekaisync.regions import (
    entity_for_region, project_common_facts, region_facts_from_dict, region_facts_to_dict,
)
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
    "introduction",
    "description",
    "summary",
    "catchCopy",
    "sentence",
    "flavorText",
]

LANGUAGE_TEXT_FIELDS = {
    "outline",
    "profileSentence",
    "profile",
    "introduction",
    "description",
    "summary",
    "catchCopy",
    "sentence",
    "flavorText",
}

_CHARACTER_PROFILE_TEXT_FIELDS = frozenset({
    "characterVoice", "birthday", "school", "schoolYear", "hobby",
    "specialSkill", "favoriteFood", "hatedFood", "weak", "introduction",
})

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
    "items": "item",
    # 2026-09 上游重组织：missions.json/items.json 聚合表拆分为细分表
    # （sekai-master-db-diff 扩到 419 张）。旧表名 404，细分表逐一映射。
    "honors": "honor",
    "bonds": "bond",
    "characterRanks": "character_rank",
    "mysekaiFixtures": "mysekai_fixture",
    "liveMissions": "live_mission",
    "characterMissions": "character_mission",
    "eventMissions": "event_mission",
    "normalMissions": "normal_mission",
    "storyMissions": "story_mission",
    "honorMissions": "honor_mission",
    "mysekaiNormalMissions": "mysekai_normal_mission",
    "beginnerMissions": "beginner_mission",
    "beginnerMissionV2s": "beginner_mission_v2",
    "shopItems": "shop_item",
    "billingShopItems": "billing_shop_item",
    "virtualItems": "virtual_item",
    "mysekaiItems": "mysekai_item",
    "eventItems": "event_item",
    "serialCodeItems": "serial_code_item",
}

REGISTRY_TABLES = frozenset(_KIND_ALIASES)


def _read_active_generations(store_root: Path) -> dict[str, str]:
    """Read pointers without dbstore.connect's mkdir/WAL/schema side effects."""
    from sekaisync.layout import ACTIVE_GENERATION_KEY, db_path

    path = db_path(store_root)
    if not path.exists():
        return {}
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        try:
            row = conn.execute('SELECT value FROM meta WHERE key=?',
                               (ACTIVE_GENERATION_KEY,)).fetchone()
        finally:
            conn.close()
        data = json.loads(row[0]) if row else {}
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (sqlite3.Error, ValueError):
        return {}


@dataclass(frozen=True)
class RawSnapshot:
    """Pinned raw directories and generation IDs for a registry build.

    This is a local adapter until the shared P02/P16 snapshot is integrated.
    Explicit snapshots never resolve active pointers or fall back on absence.
    Legacy directories are mutable; their inputs are read once per build and
    must not be advertised as an immutable historical snapshot.
    """

    master_dirs: Mapping[str, Path]
    generations: Mapping[str, str]

    def __post_init__(self):
        object.__setattr__(self, 'master_dirs', MappingProxyType(
            {r: Path(p) for r, p in self.master_dirs.items()}))
        object.__setattr__(self, 'generations', MappingProxyType(dict(self.generations)))


def capture_raw_snapshot(store_root: Path, regions: Iterable[str],
                         generation: Optional[str] = None) -> RawSnapshot:
    active = _read_active_generations(store_root)
    directories, generations = {}, {}
    for region in sorted(set(regions)):
        selected = generation
        if not selected or not generation_master_dir(store_root, region, selected).exists():
            selected = active.get(region)
        path = generation_master_dir(store_root, region, selected) if selected else None
        if path is None or not path.exists():
            path, selected = region_master_dir(store_root, region), None
        directories[region] = path
        if selected:
            generations[region] = selected
    return RawSnapshot(directories, generations)


def _active_or_legacy_dir(store_root: Path, region: str) -> Path:
    """The region's active generation dir, or the legacy in-place dir.

    Never raises and never creates anything: a store with no database (or an
    unreadable one) simply has no published pointer yet, so it resolves to the
    pre-generation layout. That keeps path resolution safe to call before the
    store is initialized.
    """
    active = _read_active_generations(store_root).get(region)
    if active:
        candidate = generation_master_dir(store_root, region, active)
        if candidate.exists():
            return candidate
    return region_master_dir(store_root, region)


def master_dir_for(
    store_root: Path,
    region: str,
    generation: Optional[str] = None,
) -> Path:
    """Resolve the master-table directory for a region.

    ``generation`` selects a specific immutable generation. If that generation
    does not actually contain this region (a publish only copies the regions it
    fetched), fall back to the region's own active/legacy tree rather than
    returning an empty directory — otherwise a publish that syncs one region
    would silently make every other region's data disappear.

    ``generation=None`` means "the active one", resolved from the store's
    pointer. A server request must pass an explicit generation so it cannot
    read two generations across tables (Astra P13).
    """
    if generation:
        candidate = generation_master_dir(store_root, region, generation)
        if candidate.exists():
            return candidate
        # Not part of that generation: keep serving this region's own data.
        return _active_or_legacy_dir(store_root, region)
    return _active_or_legacy_dir(store_root, region)


def data_files_for_region(
    store_root: Path,
    region: str,
    generation: Optional[str] = None,
) -> list[Path]:
    base = master_dir_for(store_root, region, generation)
    return _data_files_under(base)


def _data_files_under(base: Path) -> list[Path]:
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


def extract_facts(
    record: dict, names: dict[str, str], language: str = "ja", *, kind: str = "",
) -> dict:
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
    if kind == "character_profile":
        for field in sorted(_CHARACTER_PROFILE_TEXT_FIELDS):
            value = record.get(field)
            if value not in (None, "", [], {}):
                facts[f"{field}_{language}"] = value
        for field in ("characterId", "scenarioId"):
            if record.get(field) not in (None, ""):
                facts[field] = record[field]
    elif kind == "card":
        for field in ("prefix", "cardSkillName", "specialTrainingSkillName"):
            if record.get(field) not in (None, ""):
                facts[f"{field}_{language}"] = record[field]
        for field in ("cardRarityType", "attr", "characterId"):
            if record.get(field) not in (None, ""):
                facts[field] = record[field]
    return facts


def _read_registry_table(path: Path) -> tuple[list[dict], dict]:
    """Parse and hash the same bytes, rejecting invalid rather than empty input."""
    raw = path.read_bytes()
    data = json.loads(raw.decode('utf-8-sig'))
    records = data
    if isinstance(data, dict):
        records = next((data[key] for key in ('records', 'items', 'data')
                        if isinstance(data.get(key), list)), None)
    if not isinstance(records, list) or any(not isinstance(r, dict) for r in records):
        raise ValueError(f'invalid registry table: {path}')
    retrieval = dict(path=str(path.resolve()), table=path.stem,
                     sha256=hashlib.sha256(raw).hexdigest(), effective_source='local')
    # Do not manufacture an upstream retrieval time from file mtime or build time.
    return records, retrieval


def build_registry(
    store_root: Path,
    regions: Iterable[str],
    generation: Optional[str] = None,
    *,
    raw_snapshot: Optional[RawSnapshot] = None,
) -> list[Entity]:
    """Build lossless regional facts; positional generation remains supported.

    Only the configured Sekai master adapters share the global identity space.
    Unknown region adapters are rejected rather than merged by numerical ID.
    An explicit snapshot is authoritative, including missing directories.
    """
    regions = sorted(set(regions))
    unknown = set(regions) - set(REGIONS) - {'demo'}
    if unknown:
        raise ValueError(f'unconfirmed master identity namespace: {sorted(unknown)}')
    if raw_snapshot is not None and generation is not None:
        raise ValueError('pass raw_snapshot or generation, not both')
    snapshot = raw_snapshot or capture_raw_snapshot(store_root, regions, generation)
    grouped: dict[str, Entity] = {}
    name_candidates: dict[str, dict[str, set[str]]] = {}
    regional_names: dict[tuple[str, str], tuple[dict[str, str], dict]] = {}
    profile_inputs = []
    story_inputs = []
    for region in regions:
        base = snapshot.master_dirs.get(region)
        if base is None:
            continue
        language = REGIONS[region].language if region in REGIONS else 'ja'
        for path in _data_files_under(base):
            if path.stem not in REGISTRY_TABLES:
                continue
            kind = kind_from_path(path)
            records, retrieval = _read_registry_table(path)
            version = snapshot.generations.get(region)
            retrieval['generation'] = version
            for record in records:
                game_id = record_id(record, kind)
                prefix = 'demo:' if region == 'demo' else ''
                entity_id = f'{prefix}{kind}:{game_id}'
                names = extract_names(record)
                if kind == 'card' and isinstance(record.get('prefix'), str) and record['prefix'].strip():
                    names.setdefault('cardName', record['prefix'])
                    names.setdefault(language, record['prefix'])
                facts = extract_facts(record, names, language=language, kind=kind)
                fallback = next((names[key] for key in (
                    'full', 'name', 'unitName', 'unitProfileName', 'songName',
                    'eventName', 'cardName', 'title') if names.get(key)), '')
                if fallback:
                    names.setdefault(language, fallback)
                entity = grouped.setdefault(entity_id, Entity(
                    id=entity_id, type=kind, region='', demo=(region == 'demo'),
                    source='master_db', trust=trust_for_source(
                        f'master_db:{region}', kind=kind, demo=(region == 'demo'))))
                if region in entity.region_facts:
                    # Multiple raw revisions/duplicate IDs cannot be arbitrarily
                    # selected. The adapter must select a validated table first.
                    raise ValueError(f'duplicate region entity: {entity_id} / {region}')
                entity.region_facts[region] = RegionFacts(
                    region, facts, f'master_db:{region}', version, dict(retrieval))
                candidates = name_candidates.setdefault(entity_id, {})
                for key, value in names.items():
                    if value:
                        candidates.setdefault(key, set()).add(value)
                if kind == 'character':
                    regional_names[entity_id, region] = (names, dict(retrieval))
                elif kind == 'character_profile' and record.get('characterId') is not None:
                    profile_inputs.append((entity_id, f"{prefix}character:{record['characterId']}", region))
                if path.stem == 'eventStories':
                    event_id = record.get('eventId') or record.get('id')
                    story_inputs.append((f'{prefix}event:{event_id}', entity_id,
                                         region, language, record, retrieval))

    # Profiles have a foreign key, not their own display names. Join only the
    # same region and pinned generation, never a coincidentally equal row ID.
    for profile_id, character_id, region in profile_inputs:
        related = regional_names.get((character_id, region))
        if related is None:
            continue
        names, retrieval = related
        candidates = name_candidates[profile_id]
        for key, value in names.items():
            if key not in candidates:
                candidates[key] = {value}
            else:
                candidates[key].add(value)
        rf = grouped[profile_id].region_facts[region]
        rf.retrieval.setdefault('name_sources', {})[character_id] = dict(retrieval)

    for event_id, story_id, region, language, record, retrieval in story_inputs:
        event = grouped.get(event_id)
        if event is not None and region in event.region_facts and record.get('outline'):
            rf = event.region_facts[region]
            key = f'outline_{language}'
            if key not in rf.facts:
                rf.facts[key] = record['outline']
                rf.retrieval.setdefault('field_sources', {})[key] = dict(retrieval)
        candidates = name_candidates[story_id]
        for episode in record.get('eventStoryEpisodes') or []:
            title = episode.get('title')
            if not title:
                continue
            if episode.get('episodeNo') is not None:
                key = f"episode{episode['episodeNo']}_{language}"
                candidates.setdefault(key, set()).add(str(title))
            if language not in candidates:
                candidates[language] = {str(title)}

    for entity_id, entity in grouped.items():
        entity.regions = sorted(entity.region_facts)
        entity.facts, _ = project_common_facts(entity.region_facts, entity.regions)
        # A multi-region entity has no privileged region/source/version.
        if len(entity.regions) == 1:
            rf = entity.region_facts[entity.regions[0]]
            entity.region, entity.source, entity.version = rf.region, rf.source, rf.version
        # Real language keys survive; conflicting generic name/title slots do
        # not get an arbitrary first-region value.
        entity.names = {key: next(iter(values))
                        for key, values in sorted(name_candidates[entity_id].items())
                        if len(values) == 1}
    return [grouped[key] for key in sorted(grouped)]


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
            "region_facts": region_facts_to_dict(entity),
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
            # Preserve old evidence exactly, including null/empty fields.
            facts=dict(item.get("facts", {})),
            region_facts=region_facts_from_dict(item.get("region_facts")),
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
        selected_facts = (entity_for_region(entity, region)['facts']
                          if entity.region_facts or region else entity.facts)
        searchable_facts = [selected_facts]
        if region is None and entity.region_facts:
            # Searching localized text is not a claim that it is common to all
            # servers. Core returns the regional evidence alongside the match.
            searchable_facts.extend(rf.facts for rf in entity.region_facts.values())
        fact_values = [
            str(value)
            for facts in searchable_facts
            for value in facts.values()
            if isinstance(value, (str, int, float))
            and not str(value).isdigit()
        ]
        all_texts = names + fact_values
        matched = best_match(query, all_texts)
        id_suffix = normalize_name(str(entity.id).rsplit(":", 1)[-1])
        id_score = 100 if query_key and query_key in {id_suffix, normalize_name(entity.id)} else 0
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
