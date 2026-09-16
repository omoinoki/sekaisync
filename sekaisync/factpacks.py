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


# 各实体类型用于时序判定的时间字段（毫秒时间戳）。按优先级取第一个可用的：
# 一个实体可能同时有 startAt（活动开始）与 releaseAt（卡牌实装）。
_TIME_FIELDS: dict[str, tuple[str, ...]] = {
    "event": ("startAt",),
    "card": ("releaseAt",),
    "gacha": ("startAt",),
    "virtual_live": ("startAt",),
    "song": ("publishedAt",),
    "music": ("publishedAt",),
    "area": ("startAt",),
    "billing_shop_item": ("startAt",),
    "shop_item": ("startAt",),
}


def entity_timestamp(entity: Entity) -> int | None:
    """实体的时间戳（毫秒）；无时间字段则返回 None。

    用途是时序分段："在某个时点，Agent 应该知道什么"。取字段的优先级由
    ``_TIME_FIELDS`` 决定，未知类型退回扫描 facts 里第一个 ``*At`` 键。
    """
    facts = entity.facts or {}
    for field in _TIME_FIELDS.get(entity.type, ()):
        value = facts.get(field)
        ts = _to_ms(value)
        if ts is not None:
            return ts
    for key, value in facts.items():
        if key.endswith("At"):
            ts = _to_ms(value)
            if ts is not None:
                return ts
    return None


def _to_ms(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    # 秒级时间戳（某些表用秒）统一升到毫秒
    return number * 1000 if number < 10_000_000_000 else number


def build_fact_pack_at(
    entity: Entity,
    language: str = "en",
    *,
    as_of: int | None = None,
    as_of_iso: str | None = None,
) -> dict:
    """截止 ``as_of`` 的时序分段事实包（防剧透）。

    动机（来自 Sekai Viewer Graph RAG 的做法）：给 LLM 喂上下文时，"第 N 集
    时应该知道什么"与"最终会知道什么"必须分开——否则模型会提前写出后续剧情
    的结局。它把事实按剧情位置切成 Past/Future 两栏注入 prompt；这里提供
    等价能力的通用版本。

    ``as_of`` 为毫秒时间戳，``as_of_iso`` 为 ISO 字符串（二者取一，都不给
    则视为"现在"）。返回::

        {
          "entity_id": ..., "entity_type": ..., "language": ...,
          "as_of": <ms>, "as_of_iso": ...,
          "past":   {"text": ..., "fact_pack_tokens": ...},
          "future": {"text": ..., "fact_pack_tokens": ...},
          "state":  "past" | "future" | "undated",
        }

    ``state`` 表明该实体本身相对 as_of 的位置：尚未发生的实体其全部事实都在
    ``future``（Agent 据此不该把它当作既成事实）；无时间字段的实体标
    ``undated``，两栏都可能为空、完整内容仍以 :func:`build_fact_pack` 为准。

    这是**纯增量能力**：不改变 :func:`build_fact_pack` 的任何行为。
    """
    if as_of is None:
        if as_of_iso:
            as_of = _parse_iso_ms(as_of_iso)
        else:
            as_of = _now_ms()

    ts = entity_timestamp(entity)
    if ts is None:
        state = "undated"
        past_entity, future_entity = entity, None
    elif ts <= as_of:
        state = "past"
        past_entity, future_entity = entity, None
    else:
        state = "future"
        past_entity, future_entity = None, entity

    def pack_text(target: Entity | None) -> dict:
        if target is None:
            return {"text": "", "fact_pack_tokens": 0}
        pack = build_fact_pack(target, language=language)
        return {"text": pack.text, "fact_pack_tokens": pack.fact_pack_tokens}

    return {
        "entity_id": entity.id,
        "entity_type": entity.type,
        "language": language,
        "as_of": as_of,
        "as_of_iso": _ms_to_iso(as_of),
        "state": state,
        "entity_at": _ms_to_iso(ts) if ts is not None else None,
        "past": pack_text(past_entity),
        "future": pack_text(future_entity),
    }


def _parse_iso_ms(value: str) -> int:
    from datetime import datetime, timezone

    text = str(value).strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _ms_to_iso(value: int | None) -> str | None:
    if value is None:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()


def _now_ms() -> int:
    import time

    return int(time.time() * 1000)


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
