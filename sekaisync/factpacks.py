from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from sekaisync.models import Entity, FactPack
from sekaisync.trust import trust_for_entity


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3))


def _pick(facts: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = facts.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return None


def build_fact_pack(entity: Entity, language: str = "en") -> FactPack:
    name = entity.name_for(language)
    facts = entity.facts
    lines = [f"{entity.type.capitalize()}: {name}"]
    lines.append(f"ID: {entity.id}")

    if entity.regions:
        lines.append("Regions: " + ", ".join(entity.regions))
    if entity.version:
        lines.append(f"Version: {entity.version}")
    lines.append(f"Trust: {entity.trust or trust_for_entity(entity)}")

    if entity.type == "character":
        unit = _pick(facts, ["unit", "unitName"])
        birthday = _pick(facts, ["birthday", "birthDate"])
        height = _pick(facts, ["height"])
        school = _pick(facts, ["school"])
        grade = _pick(facts, ["grade"])
        if unit:
            lines.append(f"Unit: {unit}")
        if birthday:
            lines.append(f"Birthday: {birthday}")
        if height:
            lines.append(f"Height: {height}")
        if school:
            lines.append(f"School: {school}")
        if grade:
            lines.append(f"Grade: {grade}")
    elif entity.type == "card":
        rarity = _pick(facts, ["rarity", "rarityId"])
        attribute = _pick(facts, ["attribute"])
        skill = _pick(facts, ["skill", "skillName"])
        character = _pick(facts, ["character", "characterName"])
        if character:
            lines.append(f"Character: {character}")
        if rarity:
            lines.append(f"Rarity: {rarity}")
        if attribute:
            lines.append(f"Attribute: {attribute}")
        if skill:
            lines.append(f"Skill: {skill}")
    elif entity.type == "song":
        composer = _pick(facts, ["composer"])
        lyricist = _pick(facts, ["lyricist"])
        arranger = _pick(facts, ["arranger"])
        bpm = _pick(facts, ["bpm"])
        if composer:
            lines.append(f"Composer: {composer}")
        if lyricist:
            lines.append(f"Lyricist: {lyricist}")
        if arranger:
            lines.append(f"Arranger: {arranger}")
        if bpm:
            lines.append(f"BPM: {bpm}")
    elif entity.type == "event":
        start = _pick(facts, ["startAt", "startTime"])
        end = _pick(facts, ["endAt", "endTime"])
        event_type = _pick(facts, ["eventType", "type"])
        if event_type:
            lines.append(f"Type: {event_type}")
        if start:
            lines.append(f"Start: {start}")
        if end:
            lines.append(f"End: {end}")
    elif entity.type == "event_story":
        outline = _pick(
            facts,
            [
                "outline_ja",
                "outline_zh_hans",
                "outline_zh_hant",
                "outline_en",
                "outline_ko",
                "outline",
            ],
        )
        if outline:
            lines.append(f"Outline: {outline}")
    elif entity.type == "unit":
        profile = _pick(
            facts,
            [
                "profileSentence_ja",
                "profileSentence_zh_hans",
                "profileSentence_zh_hant",
                "profileSentence_en",
                "profileSentence_ko",
                "profileSentence",
                "profile",
            ],
        )
        if profile:
            lines.append(f"Profile: {profile}")
    elif entity.type == "character_profile":
        profile = _pick(
            facts,
            [
                "profileSentence_ja",
                "profileSentence_zh_hans",
                "profileSentence_zh_hant",
                "profileSentence_en",
                "profileSentence_ko",
                "profileSentence",
                "profile",
            ],
        )
        if profile:
            lines.append(f"Profile: {profile}")
    elif entity.type == "gacha":
        start = _pick(facts, ["startAt", "startTime"])
        end = _pick(facts, ["endAt", "endTime"])
        if start:
            lines.append(f"Start: {start}")
        if end:
            lines.append(f"End: {end}")

    raw_json_tokens = estimate_tokens(json.dumps({"names": entity.names, "facts": facts}, ensure_ascii=False))
    text = "\n".join(lines)
    return FactPack(
        entity_id=entity.id,
        entity_type=entity.type,
        language=language,
        text=text,
        raw_json_tokens=raw_json_tokens,
        fact_pack_tokens=estimate_tokens(text),
    )


def build_fact_packs(entities: Iterable[Entity], language: str = "en") -> list[FactPack]:
    return [build_fact_pack(entity, language=language) for entity in entities]


def save_fact_packs(packs: Iterable[FactPack], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "entity_id": pack.entity_id,
            "entity_type": pack.entity_type,
            "language": pack.language,
            "text": pack.text,
            "raw_json_tokens": pack.raw_json_tokens,
            "fact_pack_tokens": pack.fact_pack_tokens,
            "token_ratio": round(pack.token_ratio, 3),
        }
        for pack in packs
    ]
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_fact_packs(path: Path) -> list[FactPack]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        FactPack(
            entity_id=str(item["entity_id"]),
            entity_type=str(item["entity_type"]),
            language=str(item.get("language", "en")),
            text=str(item.get("text", "")),
            raw_json_tokens=int(item.get("raw_json_tokens", 0)),
            fact_pack_tokens=int(item.get("fact_pack_tokens", 0)),
        )
        for item in data
    ]
