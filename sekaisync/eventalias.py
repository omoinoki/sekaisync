"""Community event alias resolution for SekaiSync.

The JP master DB does not tag which event is a character "box" event.  The
community shorthand such as ``khn3`` / ``豆三箱`` means "the third character
focused event for 小豆泽心羽 (Kohane)".  This module rebuilds that mapping
deterministically from master DB files so agents never have to guess.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from sekaisync.layout import region_source_dir
from typing import Any, Optional

# Region keys in the order used by the CLI.
REGION_ORDER = ("jp", "en", "tc", "kr", "cn")

# JP master data lives under store/jp/source/sekai-master-db-diff-main.
# Overseas mirrors use the same key order but a region-specific repo folder.
_REGION_FOLDER = {
    "jp": "sekai-master-db-diff-main",
    "en": "sekai-master-db-en-diff-main",
    "tc": "sekai-master-db-tc-diff-main",
    "kr": "sekai-master-db-kr-diff-main",
    "cn": "sekai-master-db-cn-diff-main",
}

# Units represented by the five human bands.  Piapro characters are excluded
# from box-event mapping because they do not own "X 箱" events.
HUMAN_UNITS = {"light_sound", "idol", "street", "theme_park", "school_refusal"}

# Event types that never produce a character box event.
NON_BOX_EVENT_TYPES = {"cheerful_carnival", "world_bloom"}


@dataclass(frozen=True)
class CharacterInfo:
    id: int
    unit: str
    ja: str
    zh_hans: str
    zh_hant: str
    en: str
    aliases: tuple[str, ...]


# Canonical romanised shorthand used by the Project Sekai community.
# aliases are matched case-insensitively; longer aliases are tried first.
CHARACTER_ALIASES: dict[str, CharacterInfo] = {
    "ick": CharacterInfo(1, "light_sound", "星乃一歌", "星乃一歌", "星乃一歌", "Hoshino Ichika", ("ick", "いちか", "一歌", "星乃一歌", "一歌宝")),
    "saki": CharacterInfo(2, "light_sound", "天馬咲希", "天马咲希", "天馬咲希", "Tenma Saki", ("saki", "咲希", "天马咲希", "天馬咲希", "咲希宝")),
    "hnm": CharacterInfo(3, "light_sound", "望月穂波", "望月穗波", "望月穗波", "Mochizuki Honami", ("hnm", "ほなみ", "穂波", "穗波", "望月穗波", "望月穂波", "穗波宝")),
    "shiho": CharacterInfo(4, "light_sound", "日野森志歩", "日野森志步", "日野森志歩", "Hinomori Shiho", ("shiho", "しほ", "志步", "志歩", "日野森志步", "日野森志歩", "志步宝")),
    "mnr": CharacterInfo(5, "idol", "花里みのり", "花里实乃理", "花里實乃理", "Hanasato Minori", ("mnr", "みのり", "实乃理", "實乃理", "花里实乃理", "花里實乃理", "实乃理宝")),
    "hrk": CharacterInfo(6, "idol", "桐谷遥", "桐谷遥", "桐谷遙", "Kiritani Haruka", ("hrk", "はるか", "遥", "遙", "桐谷遥", "桐谷遙", "遥宝")),
    "airi": CharacterInfo(7, "idol", "桃井愛莉", "桃井爱莉", "桃井愛莉", "Momoi Airi", ("airi", "あいり", "爱莉", "愛莉", "桃井爱莉", "桃井愛莉", "爱莉宝")),
    "szk": CharacterInfo(8, "idol", "日野森雫", "日野森雫", "日野森雫", "Hinomori Shizuku", ("szk", "しずく", "雫", "日野森雫", "雫宝")),
    "khn": CharacterInfo(9, "street", "小豆沢こはね", "小豆泽心羽", "小豆澤心羽", "Azusawa Kohane", ("khn", "こはね", "心羽", "小豆泽心羽", "小豆澤心羽", "豆", "豆宝")),
    "an": CharacterInfo(10, "street", "白石杏", "白石杏", "白石杏", "Shiraishi An", ("an", "あん", "杏", "白石杏", "杏宝")),
    "akt": CharacterInfo(11, "street", "東雲彰人", "东云彰人", "東雲彰人", "Shinonome Akito", ("akt", "あきと", "彰人", "东云彰人", "東雲彰人", "彰人宝")),
    "toya": CharacterInfo(12, "street", "青柳冬弥", "青柳冬弥", "青柳冬彌", "Aoyagi Toya", ("toya", "とうや", "冬弥", "冬彌", "青柳冬弥", "青柳冬彌", "冬弥宝")),
    "tks": CharacterInfo(13, "theme_park", "天馬司", "天马司", "天馬司", "Tenma Tsukasa", ("tks", "つかさ", "司", "天马司", "天馬司", "司宝")),
    "tms": CharacterInfo(13, "theme_park", "天馬司", "天马司", "天馬司", "Tenma Tsukasa", ("tms",)),
    "emu": CharacterInfo(14, "theme_park", "鳳えむ", "凤笑梦", "鳳笑夢", "Otori Emu", ("emu", "えむ", "笑梦", "笑夢", "凤笑梦", "鳳笑夢", "笑梦宝")),
    "nene": CharacterInfo(15, "theme_park", "草薙寧々", "草薙宁宁", "草薙寧寧", "Kusanagi Nene", ("nene", "ねね", "宁宁", "寧寧", "草薙宁宁", "草薙寧寧", "宁宁宝")),
    "rui": CharacterInfo(16, "theme_park", "神代類", "神代类", "神代類", "Kamishiro Rui", ("rui", "るい", "类", "類", "神代类", "神代類", "类宝")),
    "knd": CharacterInfo(17, "school_refusal", "宵崎奏", "宵崎奏", "宵崎奏", "Yoisaki Kanade", ("knd", "かなで", "奏", "宵崎奏", "奏宝")),
    "mfy": CharacterInfo(18, "school_refusal", "朝比奈まふゆ", "朝比奈真冬", "朝比奈真冬", "Asahina Mafuyu", ("mfy", "まふゆ", "真冬", "朝比奈真冬", "真冬宝")),
    "ena": CharacterInfo(19, "school_refusal", "東雲絵名", "东云绘名", "東雲繪名", "Shinonome Ena", ("ena", "えな", "绘名", "繪名", "东云绘名", "東雲繪名", "绘名宝")),
    "mzk": CharacterInfo(20, "school_refusal", "暁山瑞希", "晓山瑞希", "曉山瑞希", "Akiyama Mizuki", ("mzk", "みずき", "瑞希", "晓山瑞希", "曉山瑞希", "瑞希宝")),
}

# Character IDs in the order used for --list.
CHARACTER_ORDER = tuple(info.id for info in CHARACTER_ALIASES.values())


def _normalize_alias_keys() -> dict[str, CharacterInfo]:
    out: dict[str, CharacterInfo] = {}
    for info in CHARACTER_ALIASES.values():
        for alias in info.aliases:
            key = alias.lower()
            if key not in out:
                out[key] = info
    return out


_ALIAS_LOOKUP = _normalize_alias_keys()

# Match an optional ordinal (arabic, full-width or Chinese numeral) followed by
# an optional 箱/箱活/活 suffix, optionally after whitespace.
_NUMERAL_TEXT = "一二三四五六七八九十"
_ORDINAL_RE = re.compile(
    rf"^\s*([0-9０-９]*[{_NUMERAL_TEXT}]*)\s*(?:箱活|箱|活)?\s*$"
)

_ARABIC_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# Small Chinese/Japanese numeral converter for ordinals used by the community.
_JAPANESE_DIGITS = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


def numeral_text_to_int(text: str) -> Optional[int]:
    """Convert a community ordinal like ``三`` or ``二十`` to an integer."""
    text = text.strip().translate(_ARABIC_DIGITS)
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 999 else None
    if text in _JAPANESE_DIGITS:
        return _JAPANESE_DIGITS[text]
    if text == "十":
        return 10
    # 二十 / 十一 / 二十三 style compounds.
    if text.startswith("十"):
        tail = text[1:]
        if not tail:
            return None
        return 10 + _JAPANESE_DIGITS.get(tail, 0)
    head, sep, tail = text.partition("十")
    if sep and head in _JAPANESE_DIGITS and tail in _JAPANESE_DIGITS:
        return _JAPANESE_DIGITS[head] * 10 + _JAPANESE_DIGITS[tail]
    if sep and head in _JAPANESE_DIGITS and not tail:
        return _JAPANESE_DIGITS[head] * 10
    return None


def parse_query(query: str) -> Optional[tuple[CharacterInfo, int]]:
    """Parse ``khn3`` / ``豆三箱`` / ``小豆泽心羽三箱`` style shorthand."""
    if not query:
        return None
    normalized = re.sub(r"\s+", "", query).lower()
    for key in sorted(_ALIAS_LOOKUP, key=len, reverse=True):
        info = _ALIAS_LOOKUP[key]
        if not normalized.startswith(key):
            continue
        rest = normalized[len(key):]
        match = _ORDINAL_RE.match(rest)
        if not match:
            continue
        ordinal = numeral_text_to_int(match.group(1))
        if ordinal is None:
            continue
        return info, ordinal
    return None


def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _region_source_root(store_root: Path, region: str) -> Path:
    return region_source_dir(store_root, region) / _REGION_FOLDER[region]


@dataclass
class BoxEvent:
    event_id: int
    owner_id: int
    card_ids: tuple[int, ...]
    music_ids: tuple[int, ...]


def _build_jp_box_map(store_root: Path) -> dict[int, list[BoxEvent]]:
    """Rebuild the community box-event sequence from the JP master DB."""
    src = _region_source_root(store_root, "jp")
    events = _load_json(src / "events.json")
    event_cards = _load_json(src / "eventCards.json")
    cards = _load_json(src / "cards.json")
    game_character_units = _load_json(src / "gameCharacterUnits.json")
    event_musics = _load_json(src / "eventMusics.json")

    cards_by_id = {card["id"]: card for card in cards}
    default_unit_by_char: dict[int, str] = {}
    for row in game_character_units:
        unit = row.get("unit")
        if unit and unit != "piapro":
            default_unit_by_char.setdefault(row["gameCharacterId"], unit)

    events_by_id = {event["id"]: event for event in events}
    event_cards_by_event: dict[int, list[dict]] = {}
    for row in event_cards:
        event_cards_by_event.setdefault(row["eventId"], []).append(row)

    music_by_event: dict[int, set[int]] = {}
    for row in event_musics:
        music_by_event.setdefault(row["eventId"], set()).add(row["musicId"])

    def card_unit(card: dict) -> Optional[str]:
        support = card.get("supportUnit")
        if support and support != "none":
            return support
        return default_unit_by_char.get(card.get("characterId"))

    candidates: list[BoxEvent] = []
    for event in events:
        event_id = event["id"]
        if event.get("eventType") in NON_BOX_EVENT_TYPES:
            continue
        r4_rows = [
            row
            for row in event_cards_by_event.get(event_id, [])
            if cards_by_id.get(row["cardId"], {}).get("cardRarityType") == "rarity_4"
        ]
        if not r4_rows:
            continue
        r4_rows.sort(key=lambda row: row["id"])
        units = {card_unit(cards_by_id[row["cardId"]]) for row in r4_rows if row["cardId"] in cards_by_id}
        human_units = units & HUMAN_UNITS
        if len(human_units) != 1:
            continue
        if not music_by_event.get(event_id):
            continue
        first_card = cards_by_id[r4_rows[0]["cardId"]]
        candidates.append(
            BoxEvent(
                event_id=event_id,
                owner_id=first_card["characterId"],
                card_ids=tuple(row["cardId"] for row in r4_rows),
                music_ids=tuple(sorted(music_by_event[event_id])),
            )
        )

    candidates.sort(key=lambda box: box.event_id)
    sequence: dict[int, list[BoxEvent]] = {}
    for box in candidates:
        sequence.setdefault(box.owner_id, []).append(box)
    return sequence


def _region_localized(store_root: Path, region: str) -> dict[int, dict[str, Any]]:
    """Localised event/song/card names for one region, keyed by event id."""
    src = _region_source_root(store_root, region)
    events = _load_json(src / "events.json")
    event_cards = _load_json(src / "eventCards.json")
    cards = _load_json(src / "cards.json")
    event_musics = _load_json(src / "eventMusics.json")
    musics = _load_json(src / "musics.json")

    music_by_id = {music["id"]: music for music in musics}
    card_by_id = {card["id"]: card for card in cards}
    event_cards_by_event: dict[int, list[dict]] = {}
    for row in event_cards:
        event_cards_by_event.setdefault(row["eventId"], []).append(row)
    music_by_event: dict[int, set[int]] = {}
    for row in event_musics:
        music_by_event.setdefault(row["eventId"], set()).add(row["musicId"])

    out: dict[int, dict[str, Any]] = {}
    for event in events:
        event_id = event["id"]
        song_names = [
            music_by_id[mid].get("title")
            for mid in sorted(music_by_event.get(event_id, set()))
            if mid in music_by_id
        ]
        card_names = [
            card_by_id[row["cardId"]].get("prefix")
            for row in sorted(event_cards_by_event.get(event_id, []), key=lambda r: r.get("id", 0))
            if row["cardId"] in card_by_id and card_by_id[row["cardId"]].get("prefix")
        ]
        out[event_id] = {
            "event_id": event_id,
            "name": event.get("name"),
            "start_at": event.get("startAt"),
            "songs": song_names,
            "cards": card_names,
        }
    return out


def _character_names() -> dict[int, dict[str, str]]:
    out: dict[int, dict[str, str]] = {}
    seen: set[int] = set()
    for info in CHARACTER_ALIASES.values():
        if info.id in seen:
            continue
        seen.add(info.id)
        out[info.id] = {
            "ja": info.ja,
            "zh_hans": info.zh_hans,
            "zh_hant": info.zh_hant,
            "en": info.en,
        }
    return out


def build_event_alias_map(
    store_root: Path,
    regions: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build the full deterministic box-event mapping.

    The JP sequence is authoritative for ordering.  Official names from the
    selected overseas regions are attached when the same event id exists there.
    """
    selected = [region for region in (regions or list(REGION_ORDER)) if region in _REGION_FOLDER]
    if "jp" not in selected:
        selected.insert(0, "jp")

    sequence = _build_jp_box_map(store_root)
    region_data = {region: _region_localized(store_root, region) for region in selected}

    characters: dict[str, dict[str, Any]] = {}
    for character_id in CHARACTER_ORDER:
        boxes = sequence.get(character_id, [])
        info = next(i for i in CHARACTER_ALIASES.values() if i.id == character_id)
        characters[str(character_id)] = {
            "id": character_id,
            "unit": info.unit,
            "names": _character_names()[character_id],
            "aliases": list(info.aliases),
            "box_events": [
                {
                    "ordinal": ordinal,
                    "regions": {
                        region: {
                            **region_data[region].get(box.event_id, {}),
                            "owner_id": box.owner_id,
                            "card_ids": list(box.card_ids),
                        }
                        for region in selected
                    },
                    "owner_id": box.owner_id,
                    "card_ids": list(box.card_ids),
                    "music_ids": list(box.music_ids),
                }
                for ordinal, box in enumerate(boxes, start=1)
            ],
        }

    return {
        "method": "community_box_event_v1",
        "source": "jp_master_db_reconstruction",
        "excludes": ["cheerful_carnival", "world_bloom", "mixed_unit_events", "events_without_new_song"],
        "characters": characters,
        "regions": selected,
    }


def resolve_event_alias(
    store_root: Path,
    query: str,
    regions: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    parsed = parse_query(query)
    if parsed is None:
        return None
    info, ordinal = parsed
    alias_map = build_event_alias_map(store_root, regions=regions)
    character = alias_map["characters"][str(info.id)]
    boxes = character["box_events"]
    if ordinal > len(boxes):
        return None
    box = boxes[ordinal - 1]
    mapping = box["regions"]
    return {
        "query": query,
        "alias": next(alias for alias in info.aliases if query.lower().startswith(alias.lower())),
        "character": {
            "id": info.id,
            "unit": info.unit,
            "names": _character_names()[info.id],
        },
        "ordinal": ordinal,
        "mapping": mapping,
        "confidence": "high",
        "method": alias_map["method"],
    }





