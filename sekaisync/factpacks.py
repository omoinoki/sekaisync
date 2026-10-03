from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Iterable

from sekaisync.config import REGIONS
from sekaisync.models import Entity, FactPack
from sekaisync.regions import project_common_facts
from sekaisync.trust import trust_for_entity


def estimate_tokens(text: str) -> int:
    return max(1, int(len(text) / 3))


#: Cross-language body fields: the requested language first, then the remaining
#: ones, then the unsuffixed field.  The previous fixed order always put ja in
#: front, so an English request silently received Japanese text.
_BODY_LANGUAGE_ORDER = ("ja", "zh_hans", "zh_hant", "en", "ko")
_BODY_LANGUAGE_ALIASES = {"zh_tw": "zh_hant", "zh_cn": "zh_hans"}
_FLAVOR_TYPES = ("area_item", "mysekai_fixture", "event_item")
_MISSION_TYPES = (
    "live_mission", "normal_mission", "character_mission", "event_mission",
    "story_mission", "honor_mission", "mysekai_normal_mission",
    "beginner_mission", "beginner_mission_v2",
)
_BODY_PREFIXES: dict[str, tuple[str, ...]] = {
    "event": ("outline",),
    "event_story": ("outline",),
    "unit": ("profileSentence", "profile"),
    "character_profile": ("introduction", "profileSentence", "profile"),
    "card": ("prefix", "cardSkillName", "skillName", "skill"),
    **dict.fromkeys(_FLAVOR_TYPES, ("flavorText", "description")),
    **dict.fromkeys(_MISSION_TYPES, ("sentence", "description")),
}


def _body_language(language: str) -> str:
    value = str(language or "").strip()
    return _BODY_LANGUAGE_ALIASES.get(value, value)


def _body_language_order(language: str) -> list[str]:
    wanted = _body_language(language)
    return ([wanted] if wanted else []) + [lang for lang in _BODY_LANGUAGE_ORDER if lang != wanted]


def _language_spellings(language: str, requested: str) -> list[str]:
    aliases = [alias for alias, canonical in _BODY_LANGUAGE_ALIASES.items() if canonical == language]
    spellings = [language] + aliases
    if requested in spellings:
        spellings.remove(requested)
        spellings.insert(0, requested)
    return spellings


def _localized_keys(prefixes: tuple[str, ...], language: str) -> list[str]:
    # Language wins over field-name preference: profile_en must beat an
    # introduction_ja when the request is English.
    keys = []
    for lang in _body_language_order(language):
        for prefix in prefixes:
            keys.extend(f"{prefix}_{spelling}" for spelling in _language_spellings(lang, language))
    return keys + list(prefixes)


def _body_keys(prefix: str, language: str) -> list[str]:
    """``("outline", "en")`` -> ``["outline_en", "outline_ja", ...]``."""
    return _localized_keys((prefix,), language)


def _text_choice(facts: dict, prefixes: tuple[str, ...], language: str) -> tuple[str, str, str] | None:
    for key in _localized_keys(prefixes, language):
        value = facts.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        effective = ""
        for prefix in prefixes:
            if key.startswith(prefix + "_"):
                effective = _body_language(key[len(prefix) + 1:])
                break
        return key, value, effective
    return None


def _body_choice(facts: dict, entity_type: str, language: str) -> tuple[str, str, str] | None:
    return _text_choice(facts, _BODY_PREFIXES.get(entity_type, ()), language)


def _pick(facts: dict, keys: list[str]) -> str | None:
    for key in keys:
        value = facts.get(key)
        if value not in (None, "", [], {}):
            return str(value)
    return None


def _render_fact_pack(entity: Entity, language: str = "en") -> FactPack:
    name = entity.names.get(language) or entity.name_for(_body_language(language))
    facts = entity.facts
    if entity.type == "card":
        title = _text_choice(facts, ("prefix",), language)
        name = title[1] if title else name
    lines = [f"{entity.type.capitalize()}: {name}"]
    lines.append(f"ID: {entity.id}")

    if entity.regions:
        lines.append("Regions: " + ", ".join(entity.regions))
    if entity.version:
        lines.append(f"Version: {entity.version}")
    lines.append(f"Trust: {entity.trust or trust_for_entity(entity)}")
    rendered_body = _body_choice(facts, entity.type, language)
    if rendered_body:
        actual_language = rendered_body[2] or "unknown"
        if rendered_body[2] != _body_language(language):
            actual_language += f" (requested: {language})"
        # Persisted packs keep text but not the richer selection context.
        lines.append(f"Body Language: {actual_language}")

    if entity.type == "character":
        unit = _pick(facts, _localized_keys(("unit", "unitName"), language))
        birthday = _pick(facts, _localized_keys(("birthday", "birthDate"), language))
        height = _pick(facts, ["height"])
        school = _pick(facts, _localized_keys(("school",), language))
        grade = _pick(facts, _localized_keys(("grade", "schoolYear"), language))
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
        rarity = _pick(facts, ["rarity", "rarityId", "cardRarityType"])
        attribute = _pick(facts, ["attribute", "attr"])
        skill = _pick(facts, _localized_keys(("cardSkillName", "skillName", "skill"), language))
        trained_skill = _pick(facts, _localized_keys(("specialTrainingSkillName",), language))
        character = _pick(facts, _localized_keys(("character", "characterName"), language))
        character_id = _pick(facts, ["characterId"])
        if character:
            lines.append(f"Character: {character}")
        if character_id:
            lines.append(f"Character ID: {character_id}")
        if rarity:
            lines.append(f"Rarity: {rarity}")
        if attribute:
            lines.append(f"Attribute: {attribute}")
        if skill:
            lines.append(f"Skill: {skill}")
        if trained_skill:
            lines.append(f"Trained Skill: {trained_skill}")
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
        # The registry folds each story's outline onto its event, so an event
        # carries a body too; it belongs in the pack the prompt is built from.
        body = _body_choice(facts, entity.type, language)
        if body:
            lines.append(f"Outline: {body[1]}")
    elif entity.type == "event_story":
        body = _body_choice(facts, entity.type, language)
        if body:
            lines.append(f"Outline: {body[1]}")
    elif entity.type == "unit":
        body = _body_choice(facts, entity.type, language)
        if body:
            lines.append(f"Profile: {body[1]}")
    elif entity.type == "character_profile":
        for label, prefixes in (
            ("Character ID", ("characterId",)),
            ("Voice", ("characterVoice",)),
            ("Birthday", ("birthday", "birthDate")),
            ("Height", ("height",)),
            ("School", ("school",)),
            ("School Year", ("schoolYear", "grade")),
            ("Hobby", ("hobby",)),
            ("Special Skill", ("specialSkill",)),
            ("Favorite Food", ("favoriteFood",)),
            ("Disliked Food", ("hatedFood",)),
            ("Weakness", ("weak",)),
        ):
            value = _pick(facts, _localized_keys(prefixes, language))
            if value:
                lines.append(f"{label}: {value}")
        body = _body_choice(facts, entity.type, language)
        if body:
            lines.append(f"Profile: {body[1]}")
    elif entity.type == "gacha":
        start = _pick(facts, ["startAt", "startTime"])
        end = _pick(facts, ["endAt", "endTime"])
        if start:
            lines.append(f"Start: {start}")
        if end:
            lines.append(f"End: {end}")
    else:
        body = _body_choice(facts, entity.type, language)
        if body:
            label = "Flavor" if entity.type in _FLAVOR_TYPES else "Sentence"
            lines.append(f"{label}: {body[1]}")

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


def build_fact_pack_with_context(
    entity: Entity, language: str = "en", *, region: str | None = None,
) -> tuple[FactPack, dict]:
    """Render one current snapshot and disclose the facts' actual scope.

    Multilingual body fields are not common facts: other regions may lack
    that language entirely. Without an explicit region, a body-carrying
    entity can select a language-bearing regional snapshot, but never merge
    scalars from several snapshots or choose among conflicting bodies.
    """
    selected = entity
    scoped = None
    needs_region = False
    available_regions = sorted(entity.region_facts)
    if region is not None:
        if not region:
            raise ValueError("region must not be empty")
        scoped = entity.region_facts.get(region)
        # An explicit region is authoritative. Legacy facts of unknown
        # coverage cannot be presented as that region's data.
        selected = replace(entity, facts=dict(scoped.facts) if scoped else {},
                           regions=[region] if scoped else [],
                           source=scoped.source if scoped else "",
                           version=scoped.version if scoped else None)
        region_scope = "region" if scoped else "entity"
        coverage = "available" if scoped else "missing"
    elif entity.region_facts:
        common, _ = project_common_facts(entity.region_facts, entity.regions)
        selected = replace(entity, facts=common)
        region_scope = "common"
        coverage = "available"
        if entity.type in _BODY_PREFIXES:
            choices = [(key, _body_choice(rf.facts, entity.type, language))
                       for key, rf in sorted(entity.region_facts.items())]
            for effective in _body_language_order(language) + [""]:
                candidates = [(key, choice) for key, choice in choices
                              if choice is not None and choice[2] == effective]
                if not candidates:
                    continue
                native_candidates = [(key, choice) for key, choice in candidates
                                     if key in REGIONS and _body_language(REGIONS[key].language) == effective]
                comparable = candidates
                if len(native_candidates) == 1:
                    comparable = native_candidates
                body_conflict = len({choice[1] for _, choice in candidates}) != 1
                scalar_conflict = len({json.dumps(entity.region_facts[key].facts,
                                                 ensure_ascii=False, sort_keys=True)
                                       for key, _ in comparable}) != 1
                if body_conflict or scalar_conflict:
                    needs_region = True
                    coverage = "needs_region"
                    # Common alternate fields must not hide a disagreement
                    # in the preferred field for this language.
                    body_fields = set(_localized_keys(_BODY_PREFIXES[entity.type], language))
                    selected = replace(selected, facts={key: value for key, value in common.items()
                                                        if key not in body_fields})
                    break
                region = comparable[0][0]
                scoped = entity.region_facts[region]
                selected = replace(entity, facts=dict(scoped.facts), regions=[region],
                                   source=scoped.source, version=scoped.version)
                region_scope = "region"
                break
    else:
        region_scope = "entity"
        coverage = "unknown"

    body = _body_choice(selected.facts, selected.type, language)
    effective = _effective_body_language(selected.facts, selected.type, language)
    content_status = ("needs_region" if needs_region else
                      "missing" if coverage == "missing" or effective is None else "available")
    context = {
        "region": region,
        "region_scope": region_scope,
        "coverage": coverage,
        "needs_region": needs_region,
        "available_regions": available_regions,
        "content_status": content_status,
        "effective_language": effective,
        "body_field": body[0] if body else None,
        "source": scoped.source if scoped else selected.source or None,
        "version": scoped.version if scoped else selected.version,
        "retrieval": dict(scoped.retrieval) if scoped else None,
    }
    return _render_fact_pack(selected, language), context


def build_fact_pack(entity: Entity, language: str = "en") -> FactPack:
    return build_fact_pack_with_context(entity, language)[0]


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


def entity_timestamp(entity: Entity, *, region: str) -> int | None:
    """实体的公开时间戳（毫秒）；该区服无可靠时间字段则返回 None。

    只认 ``_TIME_FIELDS`` 里**按实体类型明确列出**的字段：旧实现还有一条
    "扫描任意 ``*At``" 的兜底，那会把 ``closedAt``、``updatedAt`` 甚至活动
    结束时间当成公开时间，于是未公开内容被判成 past。没有可靠时间就是
    undated，内容随之被扣下——不从其他字段推断。

    ``region`` 是必需的：公开时间随区服不同，让调用方显式说明区服，
    胜过函数默默挑一个。
    """
    facts = _facts_for_region(entity, region)
    for field in _TIME_FIELDS.get(entity.type, ()):
        ts = _to_ms(facts.get(field))
        if ts is not None:
            return ts
    return None


def _facts_for_region(entity: Entity, region: str) -> dict:
    """该区服的事实；仅旧实体完全无逐区服数据时退回实体级 facts。

    退回是一种**明确降级**：调用方从 ``entity.region_facts`` 是否为空即可
    知道这些值不具区服归属（见返回的 ``region_scope``）。
    已有逐区服数据但缺少所请求区服时返回空事实，不借用共同事实。
    """
    if not region:
        raise ValueError("region is required for region-aware fact packs")
    scoped = entity.region_facts.get(region)
    if scoped is not None:
        return dict(scoped.facts or {})
    if entity.region_facts:
        return {}
    return dict(entity.facts or {})


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
    region: str,
    as_of: int | None = None,
    as_of_iso: str | None = None,
) -> dict:
    """截止 ``as_of`` 的时序分段事实包（公开时间过滤）。

    动机（来自 Sekai Viewer Graph RAG 的做法）：给 LLM 喂上下文时，"第 N 集
    时应该知道什么"与"最终会知道什么"必须分开——否则模型会提前写出后续剧情
    的结局。

    **能力边界（重要）**：当前只有**实体级**发布时间，因此这里实现的是
    **公开时间过滤**（release-time filtering），不是剧情进度防剧透。要实现
    后者需要逐章节/逐事实的 ``knowledge_at`` 证据，尚未建模。不要把它宣传为
    "任意剧集进度防剧透"。

    ``region`` 必填：公开时间随区服不同，所以调用方必须说明问的是哪个区服，
    而不是让函数默认挑一个。旧实体完全没有逐区服数据时退回实体级 facts，
    并在 ``region_scope`` 里标明这是降级（``entity``）而非确切区服数据。
    已有逐区服数据但缺少所请求区服时，不借用其他区服或共同发布时间。

    ``as_of`` 为毫秒时间戳，``as_of_iso`` 为 ISO 字符串（二者互斥，都不给则
    视为"现在"）。返回::

        {
          "entity_id": ..., "entity_type": ..., "language": ...,
          "region": ..., "region_scope": "region" | "entity",
          "as_of": <ms>, "as_of_iso": ...,
          "state":  "past" | "future" | "undated",
          "content_status": "available" | "missing" | "withheld",
          "effective_language": <正文实际语言>,
          "past":   {"text": ..., "fact_pack_tokens": ...},
          "future": {"text": "", "fact_pack_tokens": 0},   # 始终为空
          "withheld": {...},
        }

    **future / undated 不携带实体内容。** 未到公开时间的实体，其标题、名称、
    正文与任何可泄漏的内容摘要都不序列化——只保留身份、状态与"被扣下了什么"
    的计数。此前 ``future`` 栏会返回完整文本，等于把未公开内容原样交给调用方，
    这是一个真实的泄露（Astra 实测 ``future_payload_contains_unreleased_name:
    true``）。

    ``undated``（无时间字段）同样扣下内容：无法证明某实体"已经公开"时，不能
    默认它安全。这是保守选择——宁可少给，不可剧透。

    ``build_fact_pack``（不传 as_of 的普通用法）不受影响，仍是当前的完整快照。
    """
    if as_of is not None and as_of_iso is not None:
        raise ValueError(
            "pass either as_of or as_of_iso, not both — refusing to pick one "
            "silently"
        )
    if as_of is None and as_of_iso is None:
        as_of = _now_ms()
    elif as_of_iso is not None:
        as_of = _parse_iso_ms(as_of_iso)
    else:
        raw_as_of = as_of
        as_of = _to_ms(raw_as_of)
        if as_of is None:
            raise ValueError(
                f"as_of must be a positive epoch-millisecond integer, got {raw_as_of!r}"
            )

    ts = entity_timestamp(entity, region=region)
    if ts is None:
        state = "undated"
    elif ts <= as_of:
        state = "past"
    else:
        state = "future"

    region_scope = "region" if region in entity.region_facts else "entity"

    if state == "past":
        scoped = _entity_for_region(entity, region)
        pack = _render_fact_pack(scoped, language=language)
        effective = _effective_body_language(scoped.facts, scoped.type, language)
        past = {"text": pack.text, "fact_pack_tokens": pack.fact_pack_tokens}
        future = {"text": "", "fact_pack_tokens": 0}
        withheld = {
            "reason": None,
            "fields": [],
            "fact_pack_tokens": 0,
        }
        # A pack with no body in any language is not "available": the caller
        # asked for content and there is none, which is different from the
        # entity simply not being public yet.
        content_status = "missing" if effective is None else "available"
        effective_language = effective
    else:
        # Withhold everything: no text, no name, no summary.  Count what was
        # held back so callers can report coverage without seeing content.
        held = _render_fact_pack(_entity_for_region(entity, region), language=language)
        past = {"text": "", "fact_pack_tokens": 0}
        future = {"text": "", "fact_pack_tokens": 0}
        withheld = {
            "reason": (
                "entity_not_public_at_as_of" if state == "future" else "entity_undated"
            ),
            "fields": ["text"],
            "fact_pack_tokens": held.fact_pack_tokens,
        }
        content_status = "withheld"
        effective_language = None

    return {
        "entity_id": entity.id,
        "entity_type": entity.type,
        "language": language,
        "region": region,
        "region_scope": region_scope,
        "as_of": as_of,
        "as_of_iso": _ms_to_iso(as_of),
        "state": state,
        "entity_at": _ms_to_iso(ts) if ts is not None else None,
        "content_status": content_status,
        "effective_language": effective_language,
        "past": past,
        "future": future,
        "withheld": withheld,
    }


def _entity_for_region(entity: Entity, region: str) -> Entity:
    """The entity as its region sees it, for the fact-pack body."""
    facts = _facts_for_region(entity, region)
    scoped = entity.region_facts.get(region)
    if scoped is not None:
        return replace(entity, facts=facts, region=region, regions=[region],
                       source=scoped.source, version=scoped.version)
    if facts == (entity.facts or {}):
        return entity
    return replace(entity, facts=facts)


def _effective_body_language(facts: dict, entity_type: str, language: str) -> str | None:
    """The language of the body actually present, or None when there is none.

    ``build_fact_pack`` already prefers the requested language; this reports
    which one it ended up using so a caller can tell "here is your language"
    from "no body in your language, here is another one" instead of receiving
    a silent substitution.
    """
    if entity_type not in _BODY_PREFIXES:
        return language  # not a body-carrying type: nothing to substitute
    body = _body_choice(facts, entity_type, language)
    return body[2] if body else None


def _parse_iso_ms(value: str) -> int:
    from datetime import datetime, timezone

    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"as_of_iso is not a valid ISO-8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        # Silently assuming UTC moves the cutoff by up to 14 hours, which can
        # flip an entity across the as_of boundary.  Make the caller state the
        # offset instead of guessing.
        raise ValueError(
            f"as_of_iso must include a timezone offset (e.g. '2026-09-16T12:00:00+08:00' "
            f"or '...Z'); got {value!r}"
        )
    ms = int(parsed.timestamp() * 1000)
    if ms <= 0:
        raise ValueError(f"as_of_iso is out of range: {value!r}")
    return ms


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
