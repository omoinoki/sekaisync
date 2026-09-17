"""Pure, fail-closed validation for the legacy extraction boundary.

These are evidence dictionaries, not the planned P08 database slot model.
A positional prediction or LLM confidence alone is not identity evidence.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable


_QUOTED = re.compile(r'[「『“"]([^」』”"\n]+)[」』”"]')


def valid_confidence(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and .6 <= value <= 1


def valid_name(value: Any) -> bool:
    return isinstance(value, str) and 2 <= len(value.strip()) <= 80 and not any(
        ord(c) < 32 for c in value.strip()
    )


def proposal_items(data: Any, key: str) -> list[dict]:
    if not isinstance(data, dict) or not isinstance(data.get(key), list):
        return []
    return [row for row in data[key][:200] if isinstance(row, dict)]


def source_proposal(item: dict, text: str) -> bool:
    if not valid_name(item.get("term")) or not valid_confidence(item.get("confidence")):
        return False
    if item["term"].strip() not in text:
        return False
    if "tags" in item and (not isinstance(item["tags"], list) or
                           any(not isinstance(tag, str) for tag in item["tags"])):
        return False
    return "kind" not in item or isinstance(item["kind"], str)


def translation_value(item: dict, language: str) -> str:
    if not valid_name(item.get("term")) or not valid_confidence(item.get("confidence")):
        return ""
    if "languages" in item and not isinstance(item["languages"], dict):
        return ""
    value = item.get("translation", item.get("target_name", (item.get("languages") or {}).get(language)))
    return value.strip() if valid_name(value) else ""


def located_pair(source: str, target: str, source_text: str, target_text: str,
                 *, story_key: str, language: str, source_language: str,
                 source_page: dict, target_page: dict) -> dict:
    """Locate exact spans; only unambiguous corresponding quoted names verify.

    Equal line counts plus matching quote ordinal are a deliberately narrow
    deterministic alignment rule. Other located proposals remain pending, even
    if two pages happen to mention both names.
    """
    source_lines = source_text.splitlines(keepends=True)
    target_lines = target_text.splitlines(keepends=True)
    for i, line in enumerate(source_lines):
        if source not in line:
            continue
        for j, translated in enumerate(target_lines):
            if target not in translated:
                continue
            sq, tq = _QUOTED.findall(line), _QUOTED.findall(translated)
            verified = (len(source_lines) == len(target_lines) and i == j and
                        sq == [source] and tq == [target])
            return {
                "story_key": story_key, "language": language, "term": target,
                "source_language": source_language, "source_term": source,
                "sentence": translated.rstrip("\r\n"), "source_sentence": line.rstrip("\r\n"),
                "line_index": j, "source_line_index": i,
                "start": sum(map(len, target_lines[:j])) + translated.index(target),
                "source_start": sum(map(len, source_lines[:i])) + line.index(source),
                "source": target_page.get("source", ""), "page_id": target_page.get("id", ""),
                "source_page_id": source_page.get("id", ""),
                "trust": target_page.get("trust") or "C",
                "verified": verified, "verification": "corresponding_unique_quotes" if verified else "occurrence_only",
            }
    return {}


def validate_translation_proposal(proposal: dict, *, evidence: Iterable[dict], language: str) -> dict:
    """Return a portable decision, without claiming P08 SlotDecision exists."""
    value = translation_value(proposal, language) if isinstance(proposal, dict) else ""
    rows = [dict(row) for row in evidence if isinstance(row, dict)]
    supporting = [row for row in rows if value and row.get("language") == language
                  and row.get("term") == value and row.get("verified") is True
                  and row.get("source_term") == proposal.get("term")
                  and value in row.get("sentence", "")
                  and proposal["term"] in row.get("source_sentence", "")]
    stories = {row.get("story_key") for row in supporting if row.get("story_key")}
    accepted = bool(value) and len(stories) >= 2
    return {"language": language, "value": value or None,
            "status": "accepted" if accepted else "pending",
            "reason": "aligned_distinct_stories" if accepted else "insufficient_evidence",
            "evidence": rows}
