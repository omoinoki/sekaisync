"""World Link (WL) event sequence reconstruction for SekaiSync.

World Link events (``eventType == "world_bloom"``) are not character box
events, but the community refers to them by unit, virtual singer, grand
finale, or -- since 2026 -- by Group.  This module rebuilds that mapping
deterministically from the JP master DB so agents never have to guess.

Subtypes:

- ``unit_wl``: one human unit's four members (banner unit is that unit).
- ``virtual_singer``: all six virtual singers (banner unit is piapro).
- ``finale``: the round-closing finale with all 26 characters and no banner.
- ``group``: new-format WLs (no banner, one virtual singer plus one member
  from four units, with a dedicated song), addressed by their unique
  ``wl3gN`` codes rather than a ``groupN`` alias.

Rounds are split by the long quiet gap between WL seasons (the first WL of a
new season starts more than ~4 months after the previous one; within a season
WLs run every 2-7 weeks).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from sekaisync.layout import region_source_dir

# Region keys in the order used by the CLI.
REGION_ORDER = ("jp", "en", "tc", "kr", "cn")

# JP master data lives under store/jp/source/sekai-master-db-diff-main.
_REGION_FOLDER = {
    "jp": "sekai-master-db-diff-main",
    "en": "sekai-master-db-en-diff-main",
    "tc": "sekai-master-db-tc-diff-main",
    "kr": "sekai-master-db-kr-diff-main",
    "cn": "sekai-master-db-cn-diff-main",
}

HUMAN_UNITS = {"light_sound", "idol", "street", "theme_park", "school_refusal"}
ALL_CHARACTER_IDS = frozenset(range(1, 27))

# Community shorthand per unit, matched case-insensitively with longer keys
# tried first.  "25" alone means 25ji only when glued to "wl" (e.g. 25wl),
# which the parser enforces by matching unit aliases before the global wl N.
UNIT_ALIASES: dict[str, str] = {
    "ln": "light_sound",
    "leo/need": "light_sound",
    "レオニ": "light_sound",
    "ライブニード": "light_sound",
    "mmj": "idol",
    "more more jump": "idol",
    "ももじゃん": "idol",
    "vbs": "street",
    "vivid bad squad": "street",
    "ビビバス": "street",
    "wxs": "theme_park",
    "wonderlands": "theme_park",
    "ワンダショ": "theme_park",
    "25ji": "school_refusal",
    "25時": "school_refusal",
    "25": "school_refusal",
    "nightcord": "school_refusal",
    "ニーゴ": "school_refusal",
}

UNIT_ORDER = ("light_sound", "idol", "street", "theme_park", "school_refusal")

UNIT_DISPLAY: dict[str, str] = {
    "light_sound": "Leo/need",
    "idol": "MORE MORE JUMP!",
    "street": "Vivid BAD SQUAD",
    "theme_park": "Wonderlands×Showtime",
    "school_refusal": "25時、ナイトコードで。",
}

# A new WL season starts when the gap from the previous WL exceeds this.
_ROUND_GAP_MS = 120 * 24 * 3600 * 1000

_WHITESPACE = re.compile(r"\s+")
_ARABIC_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

# Small Chinese/Japanese numeral converter for ordinals in natural-language
# queries such as "第三轮第二组".
_CN_DIGITS = {
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


def _numeral_to_int(text: str) -> Optional[int]:
    """Convert an ordinal like ``3`` / ``三`` / ``二十三`` to an integer."""
    text = text.strip().translate(_ARABIC_DIGITS)
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 999 else None
    if text in _CN_DIGITS:
        return _CN_DIGITS[text]
    if text == "十":
        return 10
    if text.startswith("十"):
        tail = text[1:]
        if not tail:
            return None
        return 10 + _CN_DIGITS.get(tail, 0)
    head, sep, tail = text.partition("十")
    if sep and head in _CN_DIGITS and tail in _CN_DIGITS:
        return _CN_DIGITS[head] * 10 + _CN_DIGITS[tail]
    if sep and head in _CN_DIGITS and not tail:
        return _CN_DIGITS[head] * 10
    return None


def _normalize_unit_keys() -> dict[str, str]:
    out: dict[str, str] = {}
    for key, unit in UNIT_ALIASES.items():
        out.setdefault(key.lower(), unit)
    return out


_UNIT_LOOKUP = _normalize_unit_keys()


def _parse_ordinal(text: str) -> Optional[int]:
    text = text.translate(_ARABIC_DIGITS)
    if not text:
        return None
    if text.isdigit():
        value = int(text)
        return value if 1 <= value <= 999 else None
    return None


def parse_wl_query(query: str) -> Optional[tuple[str, str, int]]:
    """Parse shorthand into a canonical (kind, value, ordinal) triple.

    Kinds:
    - ``code``: ``wl1g6`` / ``wl3第2组`` / ``第3轮世界连接活动第2组`` -- the
      unique code ``wl{round}g{index}`` (natural-language forms resolve to the
      same code).  ``value`` carries the round number, ``ordinal`` the index
      within that round.
    - ``unit_wl``: ``vbs wl2`` -- value is the unit key, ordinal is the Nth
      unit WL.
    - ``virtual_singer`` / ``finale`` / ``round``: value is empty; ordinal is
      the Nth entry of that subtype (round: the Nth round).  ``wlN`` means the
      Nth round as a whole, matching how the community says "WL3".
    """
    if not query:
        return None
    normalized = _WHITESPACE.sub("", query).lower()

    code_match = re.fullmatch(r"wl([0-9０-９]+)g([0-9０-９]+)", normalized)
    if code_match:
        round_no = _parse_ordinal(code_match.group(1))
        index = _parse_ordinal(code_match.group(2))
        if round_no is not None and index is not None:
            return "code", str(round_no), index

    # Natural-language forms mapping to the same unique code:
    #   wl3第2组 / WL3第2組 / 第3轮世界连接活动第2组 / 第三轮第二组
    nl_match = re.fullmatch(
        r"wl([0-9０-９]+)第([0-9０-９一二三四五六七八九十]+)组", normalized
    )
    if nl_match:
        round_no = _parse_ordinal(nl_match.group(1))
        index = _numeral_to_int(nl_match.group(2))
        if round_no is not None and index is not None:
            return "code", str(round_no), index

    cn_match = re.fullmatch(
        r"第([0-9０-９一二三四五六七八九十]+)轮(?:世界连接活动|世界連結活動|wl)?"
        r"第([0-9０-９一二三四五六七八九十]+)组",
        normalized,
    )
    if cn_match:
        round_no = _numeral_to_int(cn_match.group(1))
        index = _numeral_to_int(cn_match.group(2))
        if round_no is not None and index is not None:
            return "code", str(round_no), index

    if re.fullmatch(r"(?:wl)?(?:finale|grandfinale|终章|最终章|ファイナル)", normalized):
        return "finale", "", 1

    round_match = re.fullmatch(r"(?:wl)?round([0-9０-９]+)", normalized)
    if round_match:
        ordinal = _parse_ordinal(round_match.group(1))
        if ordinal is not None:
            return "round", "", ordinal

    vs_match = re.fullmatch(r"(?:wl)?(?:vs|virtualsinger|虚拟歌手|バーチャルシンガー)(?:wl)?([0-9０-９]*)", normalized)
    if vs_match:
        ordinal = _parse_ordinal(vs_match.group(1) or "1") or 1
        return "virtual_singer", "", ordinal

    for key in sorted(_UNIT_LOOKUP, key=len, reverse=True):
        if not normalized.startswith(key):
            continue
        rest = normalized[len(key):]
        if rest.startswith("wl"):
            rest = rest[2:]
        ordinal = _parse_ordinal(rest) if rest else 1
        if ordinal is None:
            continue
        return "unit_wl", _UNIT_LOOKUP[key], ordinal

    # wlN means the Nth round as a whole (matching the community's "WL3").
    round_match = re.fullmatch(r"wl([0-9０-９]*)", normalized)
    if round_match:
        ordinal = _parse_ordinal(round_match.group(1) or "1") or 1
        return "round", "", ordinal

    return None


def _load_json(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, list) else []


def _region_source_root(store_root: Path, region: str) -> Path:
    return region_source_dir(store_root, region) / _REGION_FOLDER[region]


@dataclass
class WLEntry:
    event_id: int
    subtype: str
    unit: str  # unit key for unit_wl, "" otherwise
    banner_char: Optional[int]
    character_ids: tuple[int, ...]
    start_at: Optional[int]
    name: str


def _classify(entry: dict, banner_char: Optional[int], banner_unit: Optional[str], r4_chars: set[int]) -> tuple[str, str]:
    """Return (subtype, unit-or-empty) for one WL event."""
    if banner_char is not None:
        if banner_unit == "piapro":
            return "virtual_singer", ""
        if banner_unit in HUMAN_UNITS:
            return "unit_wl", banner_unit
    if r4_chars == ALL_CHARACTER_IDS:
        return "finale", ""
    return "group", ""


def _build_jp_wl_entries(store_root: Path) -> list[WLEntry]:
    """Rebuild the WL sequence from the JP master DB, in JP order."""
    src = _region_source_root(store_root, "jp")
    events = _load_json(src / "events.json")
    event_cards = _load_json(src / "eventCards.json")
    cards = {card["id"]: card for card in _load_json(src / "cards.json")}
    game_character_units = _load_json(src / "gameCharacterUnits.json")
    event_stories = _load_json(src / "eventStories.json")

    char_by_unit_id: dict[int, int] = {}
    unit_by_unit_id: dict[int, str] = {}
    for row in game_character_units:
        if row.get("id") is None or row.get("gameCharacterId") is None:
            continue
        char_by_unit_id.setdefault(row["id"], row["gameCharacterId"])
        if row.get("unit"):
            unit_by_unit_id.setdefault(row["id"], row["unit"])

    banner_by_event: dict[int, tuple[Optional[int], Optional[str]]] = {}
    for story in event_stories:
        event_id = story.get("eventId")
        unit_id = story.get("bannerGameCharacterUnitId")
        if event_id is None or unit_id is None:
            continue
        banner_by_event.setdefault(
            event_id,
            (char_by_unit_id.get(unit_id), unit_by_unit_id.get(unit_id)),
        )

    r4_by_event: dict[int, set[int]] = {}
    for row in event_cards:
        card = cards.get(row.get("cardId"))
        if card and card.get("cardRarityType") == "rarity_4":
            r4_by_event.setdefault(row["eventId"], set()).add(card.get("characterId"))

    entries: list[WLEntry] = []
    for event in events:
        if event.get("eventType") != "world_bloom":
            continue
        event_id = event["id"]
        banner_char, banner_unit = banner_by_event.get(event_id, (None, None))
        subtype, unit = _classify(event, banner_char, banner_unit, r4_by_event.get(event_id, set()))
        entries.append(
            WLEntry(
                event_id=event_id,
                subtype=subtype,
                unit=unit,
                banner_char=banner_char,
                character_ids=tuple(sorted(r4_by_event.get(event_id, set()))),
                start_at=event.get("startAt"),
                name=event.get("name"),
            )
        )
    entries.sort(key=lambda entry: entry.event_id)
    return entries


def _rounds(entries: list[WLEntry]) -> list[list[WLEntry]]:
    rounds: list[list[WLEntry]] = []
    current: list[WLEntry] = []
    prev_start: Optional[int] = None
    for entry in entries:
        if prev_start is not None and entry.start_at and (entry.start_at - prev_start) > _ROUND_GAP_MS:
            rounds.append(current)
            current = []
        current.append(entry)
        prev_start = entry.start_at
    if current:
        rounds.append(current)
    return rounds


def _region_localized(store_root: Path, region: str) -> dict[int, dict[str, Any]]:
    """Localised event names for one region, keyed by event id."""
    src = _region_source_root(store_root, region)
    out: dict[int, dict[str, Any]] = {}
    for event in _load_json(src / "events.json"):
        event_id = event.get("id")
        if event_id is None:
            continue
        out[event_id] = {
            "event_id": event_id,
            "name": event.get("name"),
            "start_at": event.get("startAt"),
        }
    return out


def build_wl_map(
    store_root: Path,
    regions: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Build the full deterministic World Link mapping.

    The JP sequence is authoritative for ordering.  Official names from the
    selected overseas regions are attached when the same event id exists there.
    """
    selected = [region for region in (regions or list(REGION_ORDER)) if region in _REGION_FOLDER]
    if "jp" not in selected:
        selected.insert(0, "jp")

    entries = _build_jp_wl_entries(store_root)
    region_data = {region: _region_localized(store_root, region) for region in selected}

    def entry_payload(entry: WLEntry, ordinal: Optional[int] = None) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "subtype": entry.subtype,
            "unit": entry.unit,
            "unit_name": UNIT_DISPLAY.get(entry.unit, ""),
            "banner_character_id": entry.banner_char,
            "character_ids": list(entry.character_ids),
            "regions": {
                region: region_data[region].get(entry.event_id, {})
                for region in selected
            },
        }
        if ordinal is not None:
            payload["ordinal"] = ordinal
        return payload

    unit_seq: dict[str, list[dict[str, Any]]] = {}
    for unit in UNIT_ORDER:
        unit_seq[unit] = [
            entry_payload(entry, ordinal=index)
            for index, entry in enumerate(
                (e for e in entries if e.subtype == "unit_wl" and e.unit == unit),
                start=1,
            )
        ]

    groups = [
        entry_payload(entry, ordinal=index)
        for index, entry in enumerate((e for e in entries if e.subtype == "group"), start=1)
    ]
    virtual_singers = [
        entry_payload(entry, ordinal=index)
        for index, entry in enumerate((e for e in entries if e.subtype == "virtual_singer"), start=1)
    ]
    finale = [
        entry_payload(entry, ordinal=index)
        for index, entry in enumerate((e for e in entries if e.subtype == "finale"), start=1)
    ]
    all_seq = [
        entry_payload(entry, ordinal=index)
        for index, entry in enumerate(entries, start=1)
    ]

    rounds = [
        {
            "round": index,
            "events": [
                {
                    "event_id": entry.event_id,
                    "code": f"wl{index}g{entry_index}",
                    "subtype": entry.subtype,
                    "unit": entry.unit,
                    "name": entry.name,
                    "start_at": entry.start_at,
                }
                for entry_index, entry in enumerate(round_entries, start=1)
            ],
        }
        for index, round_entries in enumerate(_rounds(entries), start=1)
    ]

    return {
        "method": "community_worldlink_v1",
        "source": "jp_master_db_reconstruction",
        "subtypes": ["unit_wl", "virtual_singer", "finale", "group"],
        "rounds": rounds,
        "units": unit_seq,
        "virtual_singers": virtual_singers,
        "finale": finale,
        "groups": groups,
        "all": all_seq,
        "regions": selected,
    }


def resolve_wl(
    store_root: Path,
    query: str,
    regions: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Resolve any WL alias to its unique ``wl{round}g{index}`` code.

    The unique code is the canonical key (e.g. ``wl2g7`` for the round-2
    finale).  Unit / virtual-singer / finale / round queries are aliases that
    resolve to the same code; ``wlN`` returns the Nth round as a whole.  The
    response always carries the code plus the alias list that points at it.
    """
    parsed = parse_wl_query(query)
    if parsed is None:
        return None
    kind, value, ordinal = parsed
    alias_map = build_wl_map(store_root, regions=regions)
    all_seq = alias_map["all"]
    rounds = alias_map["rounds"]

    if kind == "round":
        if ordinal > len(rounds):
            return None
        round_info = rounds[ordinal - 1]
        return {
            "query": query,
            "kind": "round",
            "round": ordinal,
            "events": round_info["events"],
            "confidence": "high",
            "method": alias_map["method"],
        }

    if kind == "code":
        round_no = int(value)
        if round_no > len(rounds):
            return None
        round_events = rounds[round_no - 1]["events"]
        if ordinal > len(round_events):
            return None
        event_id = round_events[ordinal - 1]["event_id"]
        target_round_no, target_index = round_no, ordinal
        entry = next(e for e in all_seq if e["regions"]["jp"].get("event_id") == event_id)
    elif kind == "unit_wl":
        sequence = alias_map["units"].get(value, [])
        if ordinal > len(sequence):
            return None
        entry = sequence[ordinal - 1]
        event_id = entry["regions"]["jp"].get("event_id")
        target_round_no = target_index = None
    elif kind == "virtual_singer":
        sequence = alias_map["virtual_singers"]
        if ordinal > len(sequence):
            return None
        entry = sequence[ordinal - 1]
        event_id = entry["regions"]["jp"].get("event_id")
        target_round_no = target_index = None
    elif kind == "finale":
        if ordinal > len(alias_map["finale"]):
            return None
        entry = alias_map["finale"][ordinal - 1]
        event_id = entry["regions"]["jp"].get("event_id")
        target_round_no = target_index = None

    if target_round_no is None:
        for round_index, round_info in enumerate(rounds, start=1):
            for entry_index, round_event in enumerate(round_info["events"], start=1):
                if round_event["event_id"] == event_id:
                    target_round_no, target_index = round_index, entry_index
                    break
            if target_round_no is not None:
                break

    code = f"wl{target_round_no}g{target_index}"
    round_info = rounds[target_round_no - 1]
    subtype = entry.get("subtype", kind)
    aliases: list[str] = [code]
    if kind == "unit_wl":
        aliases.append(f"{value} wl{ordinal}")
    elif kind == "virtual_singer":
        aliases.append(f"vs wl{ordinal}")
    elif kind == "finale":
        aliases.append("finale")

    return {
        "query": query,
        "code": code,
        "subtype": subtype,
        "unit": entry.get("unit", ""),
        "unit_name": entry.get("unit_name", ""),
        "event_id": event_id,
        "name": entry["regions"]["jp"].get("name"),
        "round": round_info,
        "aliases": aliases,
        "mapping": entry,
        "confidence": "high",
        "method": alias_map["method"],
    }
