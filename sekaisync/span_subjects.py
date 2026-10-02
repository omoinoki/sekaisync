"""Private, exact occurrence subjects for additive span-first discovery.

Literal subjects ground exact selected raw text with the existing string
selector as a compatibility fallback. Segmented
subjects explicitly identify the selected raw fragments, not a normalized
string alias. Construction checks a supplied raw window; validation also
replays it against the current full local page. Neither operation judges
meaning, creates a sense, writes names, or publishes a translation.
"""
from __future__ import annotations

from sekaisync import occurrence_store as ledger
from sekaisync.line_alignment import dialogue_spans, strip_speaker_label


_SCHEMA = "sekaisync/span-subject@1"


def _utterance_body_ranges(view):
    """Describe selectable raw bodies with the validator's turn boundaries."""
    from sekaisync.wording_identity import _view_policy
    if _view_policy(view):
        return (dict(start=view["start"], end=view["end"],
                     body_ranges=[dict(start=view["start"], end=view["end"])]),)
    lines = tuple(view["text"].splitlines(keepends=True))
    offsets, offset = [], view["start"]
    for line in lines:
        offsets.append(offset)
        offset += len(line)
    turns = dialogue_spans(lines)
    # A clipped body can lose its leading speaker label. Full-page replay in
    # _validate still proves the actual turn; this is only a local candidate.
    if view["start"] > 0 and lines and all(strip_speaker_label(line) == line for line in lines):
        turns = ((tuple(range(len(lines))), view["text"]),)
    result = []
    for indices, _body in turns:
        ranges = []
        for index in indices:
            line = lines[index]
            body = strip_speaker_label(line)
            start, end = offsets[index] + len(line) - len(body), offsets[index] + len(line)
            if start == end:
                continue
            if ranges and ranges[-1]["end"] == start:
                ranges[-1]["end"] = end
            else:
                ranges.append(dict(start=start, end=end))
        result.append(dict(start=offsets[indices[0]], end=offsets[indices[-1]] + len(lines[indices[-1]]),
                           body_ranges=ranges))
    return tuple(result)


def _utterance(view, segments):
    """Locate one raw dialogue turn and reject all selected speaker metadata."""
    from sekaisync.wording_identity import _view_policy
    if _view_policy(view):
        previous = view["start"]
        if not segments:
            raise ValueError("wording subject requires exact raw fragments")
        for part in segments:
            a, b = part.get("start"), part.get("end")
            if (type(a) is not int or type(b) is not int or not previous <= a < b <= view["end"]
                    or part.get("exact") != view["text"][a-view["start"]:b-view["start"]]):
                raise ValueError("wording fragments must select exact raw value text")
            previous = b
    for turn in _utterance_body_ranges(view):
        if all(any(body["start"] <= part["start"] < part["end"] <= body["end"]
                   for body in turn["body_ranges"]) for part in segments):
            return turn
    raise ValueError("subject fragments must select one utterance body, not speaker metadata or different turns")


def _literal_selection(view, term, segments):
    """Ground validated raw literals independently of lexical-search boundaries."""
    from sekaisync.agent_packets import _term_selection
    from sekaisync.wording_identity import _view_policy
    if not isinstance(term, str) or not term.strip():
        return False
    if not _view_policy(view):
        envelope = view["text"][segments[0]["start"] - view["start"]:
                                segments[-1]["end"] - view["start"]]
        gaps = (view["text"][left["end"] - view["start"]:
                             right["start"] - view["start"]]
                for left, right in zip(segments, segments[1:]))
        if term == envelope and all(not gap or gap.isspace() for gap in gaps):
            return True
    return _term_selection(view, term, segments, case_sensitive=True)


def _subject(view, story_key, segments, kind, term=None):
    source = ledger._anchor(view, story_key, segments)
    parts = [part["exact"] for part in source["segments"]]
    if any(not exact.strip() for exact in parts):
        raise ValueError("subject fragments cannot consist only of whitespace")
    utterance = _utterance(view, source["segments"])
    if kind == "literal":
        if not _literal_selection(view, term, source["segments"]):
            raise ValueError("literal subject must exactly select the existing complete string occurrence")
        canonical = term
    elif kind == "segmented":
        if len(parts) < 2:
            raise ValueError("segmented subject requires at least two exact raw fragments")
        canonical = " ".join(parts)
    else:
        raise ValueError("unsupported subject kind")
    gaps = [dict(start=left["end"], end=right["start"],
                 exact=view["text"][left["end"] - view["start"]:right["start"] - view["start"]])
            for left, right in zip(source["segments"], source["segments"][1:])]
    payload = dict(schema=_SCHEMA, kind=kind, canonical=canonical, canonical_parts=parts,
                   source=source, gaps=gaps, gap_text=[gap["exact"] for gap in gaps],
                   utterance=utterance, window=dict(start=view["start"], end=view["end"]))
    # Window context is replayable provenance, not a source-identity shortcut.
    identity = [payload["schema"], kind, canonical, parts, source["id"]]
    return dict(payload, id=ledger._identity("subject:" + kind + ":", identity))


def _literal(view, story_key, term, segments):
    """Build a literal source subject from exact raw text or legacy selection."""
    return _subject(view, story_key, segments, "literal", term)


def _segmented(view, story_key, segments):
    """Build a typed discontinuous subject; canonical is display-only text."""
    return _subject(view, story_key, segments, "segmented")


def _validate(conn, subject, pages=None):
    """Read-only replay against current page metadata, bytes and full turns.

    ``pages`` uses the occurrence-ledger validation cache format. This returns
    the validated subject and never infers that its fragments share a meaning.
    An explicit contextual sense must be supplied separately by the reviewer.
    """
    if not isinstance(subject, dict) or subject.get("schema") != _SCHEMA:
        raise ValueError("unsupported span subject")
    kind = subject.get("kind")
    if kind not in {"literal", "segmented"}:
        raise ValueError("unsupported subject kind")
    source = subject.get("source")
    if not isinstance(source, dict):
        raise ValueError("subject requires an exact source anchor")
    pages = {} if pages is None else pages
    ledger._validate_anchor(conn, source, pages)
    text = str(pages[(source["page_source"], source["page_id"])][0] or "")
    window = subject.get("window")
    if (not isinstance(window, dict) or set(window) != {"start", "end"}
            or type(window["start"]) is not int or type(window["end"]) is not int
            or not 0 <= window["start"] < window["end"] <= len(text)):
        raise ValueError("subject requires a valid raw page window")
    full = dict(source=source["page_source"], page_id=source["page_id"], language=source["language"],
                sha256=source["page_sha256"], start=0, end=len(text), text=text)
    if pages[(source["page_source"], source["page_id"])][6] == "wordings":
        from sekaisync.wording_identity import _full_view, _persisted_page
        full = _full_view(_persisted_page(conn, source["page_source"], source["page_id"],
                                        cache=pages.setdefault("wording_pages", {})))
    _utterance(full, source["segments"])
    view = dict(full, start=window["start"], end=window["end"], text=text[window["start"]:window["end"]])
    expected = _subject(view, source["story_key"], source["segments"], kind,
                        subject.get("canonical") if kind == "literal" else None)
    if expected != subject:
        raise ValueError("span subject identity or raw provenance changed")
    return subject
