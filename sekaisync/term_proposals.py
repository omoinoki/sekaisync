"""Pure, fail-closed validation for the legacy extraction boundary.

These are evidence dictionaries, not the planned P08 database slot model.
A positional prediction or LLM confidence alone is not identity evidence.
"""
from __future__ import annotations

import math
import re
from bisect import bisect_right
from typing import Any, Iterable

from sekaisync.line_alignment import (
    align_lines, contains_term, dialogue_spans, find_term_span,
    source_term_occurrences, strip_speaker_label,
)


_QUOTED = re.compile(r'[「『“"]([^」』”"]+)[」』”"]')


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
    if not source_term_occurrences(text.splitlines(), item["term"].strip()):
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

    Dialogue alignment chooses the actual corresponding span before looking
    for a target name. Shared spelling or unique corresponding quotations are
    additional identity evidence; a geometrically aligned sentence alone does
    not certify that arbitrary two words inside it translate each other.
    """
    source_lines = source_text.splitlines(keepends=True)
    target_lines = target_text.splitlines(keepends=True)
    source_indices = [i for i, line in enumerate(source_lines) if line.strip()]
    target_indices = [i for i, line in enumerate(target_lines) if line.strip()]
    aligned = align_lines([source_lines[i].strip() for i in source_indices],
                          [target_lines[j].strip() for j in target_indices],
                          source_language, language)
    reverse = align_lines([target_lines[j].strip() for j in target_indices],
                          [source_lines[i].strip() for i in source_indices],
                          language, source_language)

    source_offsets, target_offsets = [], []
    for lines, offsets in ((source_lines, source_offsets), (target_lines, target_offsets)):
        offset = 0
        for line in lines:
            offsets.append(offset)
            offset += len(line)

    def section(members, indices, lines, offsets, text):
        """Body view plus exact coordinates, without fabricating joined text."""
        physical = sorted({indices[i] for i in members})
        if not physical:
            return None
        pieces, coordinates = [], []
        previous_end = None
        for i in physical:
            if previous_end is not None and offsets[i] > previous_end:
                gap = text[previous_end:offsets[i]]
                if gap.strip():
                    return None  # Never bridge a skipped nonempty utterance.
                pieces.append(gap)
                coordinates.extend(range(previous_end, offsets[i]))
            body = strip_speaker_label(lines[i])
            begin = offsets[i] + len(lines[i]) - len(body)
            pieces.append(body)
            coordinates.extend(range(begin, begin + len(body)))
            previous_end = offsets[i] + len(lines[i])
        return ("".join(pieces), coordinates,
                text[offsets[physical[0]]:previous_end].rstrip("\r\n"))

    def unique_quote(view, term):
        quotes = list(_QUOTED.finditer(view[0]))
        if len(quotes) != 1:
            return None
        quote = quotes[0]
        value = quote.group(1)
        trimmed = value.strip()
        span = find_term_span(trimmed, term)
        if span != (0, len(trimmed)):
            return None
        start = quote.start(1) + len(value) - len(value.lstrip())
        return start, start + len(trimmed)

    def document_span(view, span):
        return view[1][span[0]], view[1][span[1] - 1] + 1

    fallback = {}
    compact_source = tuple(source_lines[i].strip() for i in source_indices)
    for source_group, _body in dialogue_spans(compact_source):
        source_view = section(source_group, source_indices, source_lines, source_offsets, source_text)
        source_match = find_term_span(source_view[0], source) if source_view else None
        if source_match is None:
            continue
        mapped = sorted({ti for si in source_group for ti in aligned.target_indices(si)})
        source_span = sorted({si for ti in mapped for si in reverse.target_indices(ti)})
        if not set(source_group) <= set(source_span):
            continue
        inverse_view = section(source_span, source_indices, source_lines, source_offsets, source_text)
        target_view = section(mapped, target_indices, target_lines, target_offsets, target_text)
        target_match = find_term_span(target_view[0], target) if target_view else None
        if target_match is None or inverse_view is None:
            continue
        source_quote = unique_quote(inverse_view, source)
        target_quote = unique_quote(target_view, target)
        quoted = source_quote is not None and target_quote is not None
        if quoted:
            source_view, source_match, target_match = inverse_view, source_quote, target_quote
        # Exact retained spellings are common for brands. Do not remove
        # punctuation or spaces to fabricate identity between two names.
        literal = source.casefold() == target.casefold()
        verified = quoted or literal
        source_start, source_end = document_span(source_view, source_match)
        target_start, target_end = document_span(target_view, target_match)
        row = {
            "story_key": story_key, "language": language, "term": target,
            "source_language": source_language, "source_term": source,
            "sentence": target_view[2], "source_sentence": source_view[2],
            "line_index": bisect_right(target_offsets, target_start) - 1,
            "source_line_index": bisect_right(source_offsets, source_start) - 1,
            "start": target_start, "source_start": source_start,
            "source": target_page.get("source", ""), "page_id": target_page.get("id", ""),
            "source_page_id": source_page.get("id", ""),
            "trust": target_page.get("trust") or "C",
            "verified": verified,
            "verification": ("corresponding_unique_quotes" if quoted else
                             "aligned_literal" if literal else "occurrence_only"),
        }
        # Preserve exact observed surfaces for a wrap or normalization, while
        # retaining the existing canonical term fields and original sentences.
        observed, source_observed = target_text[target_start:target_end], source_text[source_start:source_end]
        if observed != target:
            row["observed_surface"] = observed
        if source_observed != source:
            row["source_observed_surface"] = source_observed
        if verified:
            return row
        if not fallback:
            fallback = row
    return fallback


def validate_translation_proposal(proposal: dict, *, evidence: Iterable[dict], language: str) -> dict:
    """Return a portable decision, without claiming P08 SlotDecision exists."""
    value = translation_value(proposal, language) if isinstance(proposal, dict) else ""
    rows = [dict(row) for row in evidence if isinstance(row, dict)]
    supporting = [row for row in rows if value and row.get("language") == language
                  and row.get("term") == value and row.get("verified") is True
                  and row.get("source_term") == proposal.get("term")
                  and contains_term(row.get("sentence", ""), value)
                  and contains_term(row.get("source_sentence", ""), proposal["term"])]
    stories = {row.get("story_key") for row in supporting if row.get("story_key")}
    accepted = bool(value) and len(stories) >= 2
    return {"language": language, "value": value or None,
            "status": "accepted" if accepted else "pending",
            "reason": "aligned_distinct_stories" if accepted else "insufficient_evidence",
            "evidence": rows}
