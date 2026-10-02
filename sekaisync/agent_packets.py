"""Grounded work packets for the agent already running the scraper.

No model SDK, endpoint or credentials: export asks the host agent to discover
terms and judge localized spans; submit checks its answer against local pages.
Structural grounding and an agent's semantic judgment are recorded separately.
Neither is a claim of infallibility or official authority.
"""
from __future__ import annotations

import hashlib
from functools import lru_cache
import json
import re
import sqlite3
import unicodedata
from pathlib import Path

from sekaisync.line_alignment import (
    align_lines, alignment_session, dialogue_spans, find_term_span,
    source_term_occurrences, strip_speaker_label,
)

_SCHEMA = "sekaisync/agent-packet@1"
_TURN_CHARS = 1800
_DISCOVERY_CHARS = 3600
_DISCOVERY_ROWS = 8
_MAX_PAIRS = 3
_DISCOVERY_TERMS = 200
_EXPANSION_CHARS = 24000
_EXPANSION_RADII = (3, 12, None)


def _valid_surface(value):
    return isinstance(value, str) and 1 <= len(value.strip()) <= 80 and not any(
        ord(c) < 32 for c in value.strip())


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def _page_hash(page):
    return hashlib.sha256(str(page.get("text") or "").encode()).hexdigest()


def _lines(page):
    lines, starts, offset = [], [], 0
    for line in str(page.get("text") or "").splitlines(keepends=True):
        if line.strip():
            lines.append(line.strip())
            starts.append((offset, offset + len(line.rstrip("\r\n")) ))
        offset += len(line)
    return lines, starts


def _view(page, indices, starts):
    if not indices:
        return None
    indices = sorted(set(indices))
    # Never manufacture adjacency across an omitted nonempty turn.
    if indices != list(range(indices[0], indices[-1] + 1)):
        return None
    start, end = starts[indices[0]][0], starts[indices[-1]][1]
    text = str(page.get("text") or "")[start:end]
    from sekaisync.wording_identity import _view_metadata
    return dict(page_id=str(page.get("id") or ""), source=str(page.get("source") or ""),
                language=str(page.get("language") or ""),
                sha256=_page_hash(page), start=start, end=end, text=text,
                complete=len(text) <= _TURN_CHARS, **_view_metadata(page))


def _body(text):
    return "\n".join(strip_speaker_label(line) for line in text.splitlines())


def _body_span(text, term):
    spans = _body_term_segments(text, term)
    return (spans[0][0]["start"], spans[0][-1]["end"]) if spans else None


def _body_term_segments(text, term, base=0):
    parts, positions, offset = [], [], 0
    for line in text.splitlines(keepends=True):
        body = strip_speaker_label(line)
        start = offset + len(line) - len(body)
        parts.append(body)
        positions.extend(range(start, start + len(body)))
        offset += len(line)
    body, cursor, result = "".join(parts), 0, []
    while (span := find_term_span(body[cursor:], term)) is not None:
        a, b = cursor + span[0], cursor + span[1]
        indices = positions[a:b]
        runs, start, end = [], indices[0], indices[0] + 1
        for index in indices[1:]:
            if index != end:
                runs.append(dict(start=base + start, end=base + end, exact=text[start:end]))
                start = index
            end = index + 1
        runs.append(dict(start=base + start, end=base + end, exact=text[start:end]))
        result.append(runs)
        cursor = b
    return result


def _view_term_segments(view, term):
    from sekaisync.wording_identity import _view_policy
    if not _view_policy(view):
        return _body_term_segments(view["text"], term, view["start"])
    if not isinstance(term, str) or not term:
        return []
    cursor, result = 0, []
    while (start := view["text"].find(term, cursor)) >= 0:
        end = start + len(term)
        result.append([dict(start=view["start"] + start, end=view["start"] + end, exact=term)])
        cursor = end
    return result


def _source_term_present(view, term):
    from sekaisync.wording_identity import _view_policy
    if _view_policy(view):
        return bool(_view_term_segments(view, term))
    return bool(source_term_occurrences(view["text"].splitlines(), term))


def _term_selection(view, term, segments, case_sensitive=False):
    """Accept exact fragments of a hit, optionally excluding soft-wrap space."""
    from sekaisync.wording_identity import _view_policy
    if _view_policy(view):
        if not isinstance(term, str) or not isinstance(segments, list) or not segments:
            return False
        previous = None
        for part in segments:
            if not isinstance(part, dict):
                return False
            a, b = part.get("start"), part.get("end")
            if (type(a) is not int or type(b) is not int or not view["start"] <= a < b <= view["end"]
                    or (previous is not None and a != previous)
                    or part.get("exact") != view["text"][a-view["start"]:b-view["start"]]):
                return False
            previous = b
        return "".join(part["exact"] for part in segments) == term
    if not isinstance(segments, list) or not segments:
        return False
    selected, previous = set(), view["start"]
    for part in segments:
        if not isinstance(part, dict):
            return False
        a, b = part.get("start"), part.get("end")
        if (type(a) is not int or type(b) is not int or a < previous or b <= a or b > view["end"]
                or part.get("exact") != view["text"][a - view["start"]:b - view["start"]]):
            return False
        selected.update(range(a, b))
        previous = b
    if case_sensitive:
        observed = unicodedata.normalize("NFKC", "".join(part["exact"] for part in segments))
        expected = unicodedata.normalize("NFKC", term)
        if "".join(c for c in observed if c.isalpha()) != "".join(c for c in expected if c.isalpha()):
            return False
    for choice in _body_term_segments(view["text"], term, view["start"]):
        positions = {i for part in choice for i in range(part["start"], part["end"])}
        mandatory = {i for i in positions if not view["text"][i - view["start"]].isspace()}
        if mandatory <= selected <= positions:
            return True
    return False


def _subject_in_view(subject, view, story_key):
    from sekaisync import occurrence_store as occurrences
    try:
        return (occurrences._anchor(view, story_key, subject["source"]["segments"]) == subject["source"]
                and view["start"] <= subject["window"]["start"] < subject["window"]["end"] <= view["end"])
    except (KeyError, TypeError, ValueError):
        return False


def _valid_literal_surface(subject):
    """Check a literal only after full span_subjects._validate replay."""
    canonical = subject["canonical"]
    if len(canonical) <= 80 and "\n" not in canonical and "\r" not in canonical:
        return _valid_surface(canonical)
    if not canonical.strip() or len(canonical) > _TURN_CHARS:
        return False
    if any(ord(char) < 32 for char in canonical.replace("\r\n", "").replace("\n", "")):
        return False
    parts = subject["source"]["segments"]
    envelope = parts[0]["exact"] + "".join(
        gap["exact"] + part["exact"] for gap, part in zip(subject["gaps"], parts[1:]))
    return canonical == envelope


def _valid_shown_native_wording_literal(conn, subject, rows):
    """Use a persisted complete native value's actual length, after span replay."""
    from sekaisync import termindex, wording_identity
    canonical = subject["canonical"]
    if (len(canonical) <= _TURN_CHARS or not canonical.strip()
            or any(ord(char) < 32 for char in canonical.replace("\r\n", "").replace("\n", ""))):
        return False
    for row in rows:
        view = row["source"]
        if (not _subject_in_view(subject, view, row["story_key"])
                or view.get("complete") is not True or not wording_identity._view_policy(view)):
            continue
        _validate_view(conn, view, {})
        page = wording_identity._persisted_page(conn, view["source"], view["page_id"])
        identity = wording_identity._metadata(page)
        if (identity is None or identity["no_expression"] or row["story_key"] != identity["family"]
                or not termindex._page_usable(page, view["language"])
                or view != wording_identity._full_view(page)):
            continue
        # Native literals still require adjacent exact fragments, never aliases.
        if (len(canonical) <= view["end"] - view["start"]
                and _term_selection(view, canonical, subject["source"]["segments"], case_sensitive=True)):
            return True
    return False


def _validate_subject_in_rows(conn, subject, rows):
    from sekaisync import span_subjects
    span_subjects._validate(conn, subject)
    if (subject["kind"] == "literal" and not _valid_literal_surface(subject)
            and not _valid_shown_native_wording_literal(conn, subject, rows)):
        raise ValueError("literal discovery subject requires a complete raw surface within 1800 code points")
    if not any(_subject_in_view(subject, row["source"], row["story_key"]) for row in rows):
        raise ValueError("span subject is outside its immutable exported source windows")
    return subject


def _subject_identity(item):
    from sekaisync import occurrence_store as occurrences
    subject = item._context.get("subject")
    return subject["id"] if subject is not None else occurrences._identity(
        "lex:", [item._context["source_language"], item.term])


def _source_selection(item, view, story_key, segments):
    subject = item._context.get("subject")
    if subject is None:
        return _term_selection(view, item.term, segments, case_sensitive=True)
    return segments == subject["source"]["segments"] and _subject_in_view(subject, view, story_key)


def _subject_occurrence_item(scope_id, scope, subject, language, parent_id):
    rows = [dict(id=row["id"], story_key=row["story_key"], source=row["source"],
                 target=row["targets"][language]) for row in scope["windows"]
            if language in row["targets"] and row["source"]["complete"] and row["targets"][language]["complete"]
            and _subject_in_view(subject, row["source"], row["story_key"])]
    if not rows:
        return None
    context = dict(schema=_SCHEMA, task="occurrence", scope_id=scope_id,
                   source_language=scope["source_language"], rows=rows, subject=subject,
                   available_stories=len({row["story_key"] for row in rows}), available_spans=len(rows),
                   parent_discovery_id=parent_id)
    return _item(subject["canonical"], language, [], "pending", context,
                 "Judge this exact typed source subject and raw target expression; segmented canonical text is display-only, never a string alias. This task never publishes a global name.")


def _fallback_story_pages(conn, story_key):
    from sekaisync import dbstore, termindex
    kind, separator, content = str(story_key).partition(":")
    if not separator:
        return {}
    escaped = lambda value: value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conditions = ["id LIKE ? ESCAPE '\\'", "(kind=? AND id=?)"]
    parameters = ["%:" + escaped(kind) + ":" + escaped(content), kind, content]
    if re.fullmatch(r"wordings:key-sha256:[0-9a-f]{64}", str(story_key)):
        conditions.append("(kind='wordings' AND canonical_key=?)")
        parameters.append(story_key)
    event = re.fullmatch(r"event:(\d+):(\d+)", str(story_key))
    if event:
        a, b = event.groups()
        conditions.extend(["url LIKE ?", "id LIKE ? ESCAPE '\\'", "url LIKE ? ESCAPE '\\'"])
        parameters.extend([f"%/story/event/{a}/{b}/%", f"%event\\_story:{a}:{b}%",
                           f"%event\\_story/{a}/{b}%"])
    columns = ["source"] + [name for name in dbstore._PAGE_COLUMNS if name != "text"]
    columns.extend(["aux_flag", "derived_flag", "extra_json", "seq"])
    pages = []
    # SQL is only a superset filter; story identity and page policy remain authoritative.
    kinds = sorted(termindex.TERM_STORY_KINDS)
    has_kind_index = conn.execute("SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_pages_kind'").fetchone()
    table = "web_pages INDEXED BY idx_pages_kind" if has_kind_index else "web_pages"
    # Both source and kind constrain the existing composite index. Without
    # source, SQLite can prefer the sequence index and scan every huge page.
    sql = ("SELECT " + ",".join(columns) + " FROM " + table +
           " WHERE source IN (SELECT DISTINCT source FROM web_pages) AND kind IN (" +
           ",".join("?" for _ in kinds) + ") AND (" + " OR ".join(conditions) + ") ORDER BY source,seq")
    for values in conn.execute(sql, kinds + parameters):
        wrapped = dict(zip(columns, values))
        page = dbstore._row_tuple_to_dict(columns, wrapped)
        if (termindex.page_story_key(page) != story_key or page.get("trust") == "D"
                or wrapped["aux_flag"] or termindex.is_auxiliary_page(page) or page.get("overlay")):
            continue
        record = conn.execute("SELECT text FROM web_pages WHERE source=? AND id=?",
                              (page["source"], page["id"])).fetchone()
        if record is not None:
            pages.append(dict(page, text=str(record[0] or "")))
    grouped = termindex.group_pages_by_story(pages)
    by = grouped.get(story_key, {})
    debts = [debt for debt in getattr(grouped, "wording_debts", []) if debt["story_key"] == story_key]
    if debts:
        by = termindex._StoryGroups()
        by.update(grouped.get(story_key, {}))
        by.wording_debts = debts
    return by


def _raw_region(page, start, end):
    from sekaisync.wording_identity import _view_metadata
    raw = str(page["text"])
    return dict(page_id=page["id"], source=page["source"], language=page["language"],
                sha256=_page_hash(page), start=start, end=end, text=raw[start:end], complete=True,
                **_view_metadata(page))


def _subject_fallback_item(conn, store, origin_scope_id, subject, language, persist=True, inventory=None):
    """Provide current raw same-content evidence, explicitly not alignment."""
    from sekaisync import agent_review as ar, span_subjects, termindex
    scope = _read_scope(store, origin_scope_id)
    if termindex._term_language(language) not in {termindex._term_language(value) for value in scope["target_languages"]}:
        raise ValueError("fallback target language is outside its original immutable scope")
    original_rows = [row for row in scope["windows"]
                     if _subject_in_view(subject, row["source"], row["story_key"])]
    _validate_subject_in_rows(conn, subject, original_rows)
    story = subject["source"]["story_key"]
    if inventory is None:
        pages = _fallback_story_pages(conn, story)
    else:
        if story not in inventory:
            inventory[story] = _fallback_story_pages(conn, story)
        pages = inventory[story]
    target_page = termindex._group_page(pages, language)
    if target_page is None:
        debt = next((value for value in getattr(pages, "wording_debts", [])
                     if value["language"] == termindex._term_language(language)), None)
        if debt:
            return None, dict(debt, unshown_target_regions=[])
        return None, dict(reason="missing_usable_localized_page", story_key=story,
                          language=language, unshown_target_regions=[])
    source_anchor = subject["source"]
    from sekaisync.wording_identity import _persisted_page, _view_policy
    source_page = _persisted_page(conn, source_anchor["page_source"], source_anchor["page_id"])
    full_source = _raw_region(source_page, 0, len(source_page["text"]))
    utterance = span_subjects._utterance(full_source, source_anchor["segments"])
    start = min(utterance["start"], subject["window"]["start"])
    end = max(utterance["end"], subject["window"]["end"])
    source_bounded = end - start > _EXPANSION_CHARS and not _view_policy(full_source)
    if source_bounded:
        start = max(start, min(subject["window"]["start"] - _EXPANSION_CHARS // 2,
                               end - _EXPANSION_CHARS))
        end = start + _EXPANSION_CHARS
    source = _raw_region(source_page, start, end)
    if not _subject_in_view(subject, source, story):
        raise ValueError("subject window exceeds the explicit fallback source budget")
    target_length = len(target_page["text"])
    from sekaisync.wording_identity import _metadata
    target_end = target_length if _metadata(target_page) else min(target_length, _EXPANSION_CHARS)
    target = _raw_region(target_page, 0, target_end)
    _validate_view(conn, source, {})
    _validate_view(conn, target, {})
    missing = [dict(start=target["end"], end=target_length)] if target["end"] < target_length else []
    fallback = dict(schema="sekaisync/subject-fallback@1", origin_scope_id=origin_scope_id,
                    subject_id=subject["id"], evidence_relation="same_content_unaligned",
                    source_level="bounded_utterance" if source_bounded else "full_utterance",
                    source_complete_page=start == 0 and end == len(source_page["text"]),
                    target_level="bounded_page" if missing else "full_page", target_complete_page=not missing,
                    target_page_code_points=target_length, unshown_target_regions=missing)
    window = dict(story_key=story, source=source, targets={language: target})
    window["id"] = "span:" + _digest(window)[:24]
    fallback_scope = dict(source_language=scope["source_language"], target_languages=list(scope["target_languages"]),
                          windows=[window], fallback=fallback)
    scope_id = _digest(fallback_scope)
    if persist:
        path = _scope_path(store, scope_id)
        if not path.exists():
            ar._write_json(path, fallback_scope)
        else:
            _read_scope(store, scope_id)
    context = dict(schema=_SCHEMA, task="occurrence", scope_id=scope_id, source_language=scope["source_language"],
                   subject=subject, fallback=fallback, available_stories=1, available_spans=1,
                   rows=[dict(id=window["id"], story_key=story, source=source, target=target)])
    item = _item(subject["canonical"], language, [], "pending", context,
                 "No complete aligned evidence was available. Review the explicit current same-content raw page; it is not a proven aligned counterpart. Preserve the exact subject and never infer omission outside a bounded target region.")
    gap = (dict(reason="unscanned_target_page_regions", story_key=story, language=language,
                target_page_source=target["source"], target_page_id=target["page_id"],
                target_page_sha256=target["sha256"], unshown_target_regions=missing) if missing else None)
    return item, gap


def _subject_gap_item(scope_id, scope, subject, language, gap, sense=None):
    rows = [row for row in scope["windows"] if _subject_in_view(subject, row["source"], row["story_key"])]
    context = dict(schema=_SCHEMA, task="subject_gap", scope_id=scope_id, source_language=scope["source_language"],
                   subject=subject, gap=gap, rows=rows)
    if sense is not None:
        context["focus"] = dict(source=subject["source"], sense=sense)
    return _item(subject["canonical"], language, [], "pending", context,
                 "This exact subject-language obligation has no complete usable target evidence. Keep it pending; import the missing current page or inspect the named unshown regions, then export again. An agent cannot accept away this debt.")


def _validate_subject_fallback(conn, store, context, language, term, inventory=None):
    if "scan" in context:
        from sekaisync import subject_scans
        subject_scans._validate_context(conn, store, context, language, term, inventory)
        return
    fallback = context.get("fallback")
    if fallback is None:
        return
    if not isinstance(fallback, dict) or fallback.get("schema") != "sekaisync/subject-fallback@1":
        raise ValueError("invalid subject fallback ancestry")
    subject = context.get("subject")
    expected, _ = _subject_fallback_item(conn, store, fallback.get("origin_scope_id"), subject,
                                          language, persist=False, inventory=inventory)
    if (expected is None or expected.term != term or expected._context["fallback"] != fallback
            or expected._context["subject"] != subject):
        raise ValueError("subject fallback cannot be replayed from its original scope and current page policy")
    expansion = context.get("expansion")
    if expansion is None:
        if context["scope_id"] != expected._context["scope_id"] or context["rows"] != expected._context["rows"]:
            raise ValueError("subject fallback raw windows changed outside its deterministic scope")
    else:
        row = expected._context["rows"][0]
        if (expansion.get("initial_source_view") != row["source"]
                or expansion.get("initial_target_view") != row["target"]
                or expansion.get("focus_source") != subject["source"]
                or expansion.get("sense", {}).get("term_id") != subject["id"]):
            raise ValueError("expanded fallback does not inherit its original subject and raw page version")


def _subject_language_items(conn, store, scope_id, scope, subject, language, parent_id, inventory=None):
    child = _subject_occurrence_item(scope_id, scope, subject, language, parent_id)
    if child is not None:
        return [child]
    child, gap = _subject_fallback_item(conn, store, scope_id, subject, language, inventory=inventory)
    items = [child] if child is not None else []
    if gap is not None:
        items.append(_subject_gap_item(scope_id, scope, subject, language, gap))
    return items


def _validate_fallback_observation(context, target, kind, segments):
    fallback = context.get("fallback")
    if fallback is None or kind not in {"omitted", "unresolved"}:
        return
    if segments != [dict(start=target["start"], end=target["end"], exact=target["text"])]:
        raise ValueError("fallback absence or uncertainty must cite the complete provided raw target region")
    if kind == "omitted" and not fallback["target_complete_page"]:
        raise ValueError("bounded fallback evidence cannot prove whole-page omission")


def _discovery_subjects(conn, context, entries):
    from sekaisync import span_subjects, source_audits
    if not isinstance(entries, list) or len(entries) > _DISCOVERY_TERMS:
        raise ValueError("subjects requires at most 200 exact typed source observations")
    rows = {row["id"]: row for row in context["rows"]}
    excluded = set((context.get("continuation") or {}).get("excluded_subject_ids", []))
    if source_audits._is_audit(context):
        excluded |= source_audits._excluded_subject_ids(context)
    result, seen = [], set()
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict) or entry.get("evidence_id") not in rows:
            raise ValueError("discovery subject must cite an exported source evidence ID")
        kind = entry.get("kind")
        keys = {"kind", "evidence_id", "segments"} | ({"canonical"} if kind == "literal" else set())
        if set(entry) != keys or kind not in {"literal", "segmented"}:
            raise ValueError("literal subject requires canonical; segmented subject accepts raw fragments only")
        row = rows[entry["evidence_id"]]
        constructor = span_subjects._literal if kind == "literal" else span_subjects._segmented
        arguments = [row["source"], row["story_key"]]
        if kind == "literal":
            arguments.append(entry["canonical"])
        try:
            subject = constructor(*arguments, entry["segments"])
            _validate_subject_in_rows(conn, subject, [row])
        except (ValueError, TypeError, KeyError) as error:
            raise ValueError(f"subject {index} (evidence_id={entry['evidence_id']}): {error}") from error
        if subject["id"] in seen or subject["id"] in excluded:
            raise ValueError("typed subject was already discovered in this packet or its continuation")
        result.append(subject)
        seen.add(subject["id"])
    return result


def _scope_windows(groups, stories, source_language, target_languages):
    """Cover every source turn, including the tail and oversized turns."""
    from sekaisync.termindex import _group_page
    windows = []
    with alignment_session():
        for story in sorted(set(stories)):
            by = groups.get(story, {})
            source = _group_page(by, source_language)
            if not source or not source.get("text"):
                continue
            from sekaisync.wording_identity import _full_view, _metadata
            metadata = _metadata(source)
            if metadata:
                source_view = _full_view(source)
                localized = {}
                for language in target_languages:
                    page = _group_page(by, language)
                    if page is not None:
                        target_metadata = _metadata(page)
                        if not target_metadata or target_metadata["family"] != metadata["family"]:
                            raise ValueError("wording target must retain its validated structural family")
                        localized[language] = _full_view(page)
                row = dict(story_key=story, source=source_view, targets=localized)
                row["search_text"] = unicodedata.normalize("NFKC", source_view["text"]).casefold()
                row["search_unwrapped"] = re.sub(r"[ \t]*\r?\n[ \t]*", "", row["search_text"])
                row["id"] = "span:" + _digest(row)[:24]
                windows.append(row)
                continue
            source_lines, source_starts = _lines(source)
            targets = {}
            for language in target_languages:
                page = _group_page(by, language)
                if page and page.get("text"):
                    lines, starts = _lines(page)
                    targets[language] = (page, starts, align_lines(
                        source_lines, lines, source_language, language))
            for members, _ in dialogue_spans(tuple(source_lines)):
                source_view = _view(source, members, source_starts)
                if source_view is None:
                    continue
                localized = {}
                for language, (page, starts, alignment) in targets.items():
                    indices = sorted({j for i in members for j in alignment.target_indices(i)})
                    view = _view(page, indices, starts)
                    if view is not None:
                        localized[language] = view
                # Overlap exceeds the maximum accepted term length (80).
                step = _TURN_CHARS - 100
                for offset in range(0, max(1, len(source_view["text"])), step):
                    view = dict(source_view)
                    view["text"] = source_view["text"][offset:offset + _TURN_CHARS]
                    view["start"] += offset
                    view["end"] = view["start"] + len(view["text"])
                    view["complete"] = source_view["complete"]
                    row = dict(story_key=story, source=view, targets=localized)
                    body = unicodedata.normalize("NFKC", _body(view["text"])).casefold()
                    row["search_text"] = body
                    row["search_unwrapped"] = re.sub(r"[ \t]*\r?\n[ \t]*", "", body)
                    row["id"] = "span:" + _digest(row)[:24]
                    windows.append(row)
                    if offset + _TURN_CHARS >= len(source_view["text"]):
                        break
    return windows


def _scope_path(store, scope_id):
    if (not isinstance(scope_id, str) or len(scope_id) != 64
            or any(c not in "0123456789abcdef" for c in scope_id)):
        raise ValueError("invalid review scope identity")
    return Path(store) / "kb" / "terms" / "review_scopes" / (scope_id + ".json")


def _read_scope(store, scope_id):
    path = _scope_path(store, scope_id)
    stat = path.stat()
    return _load_scope(str(path), scope_id, stat.st_mtime_ns, stat.st_size)


@lru_cache(maxsize=2)
def _load_scope(path, scope_id, mtime_ns, size):
    scope = json.loads(Path(path).read_text(encoding="utf-8"))
    if _digest(scope) != scope_id:
        raise ValueError("review scope changed; regenerate the extraction queue")
    return scope


def _item(term, language, candidates, kind, context, reason):
    from sekaisync import agent_review as ar
    candidates = sorted(set(candidates))
    identity = [term, language, candidates, context]
    item = ar.make_review_item(term, language, candidates, kind=kind,
                              chosen_hint=candidates[0] if len(candidates) == 1 else None,
                              story_keys=[r["story_key"] for r in context.get("rows", [])],
                              reason=reason, channels=["agent_context"])
    item.id = "arp:" + _digest(identity)[:32]
    item._context = context
    if ar._is_raw_subject_term(term, context):
        item.term = term
    return item


def _translation_item(scope_id, scope, term, language, candidates=()):
    rows = []
    needle = unicodedata.normalize("NFKC", term).casefold()
    for row in scope["windows"]:
        if language not in row["targets"]:
            continue
        if needle not in row["search_text"] and needle not in row["search_unwrapped"]:
            continue
        if not _source_term_present(row["source"], term):
            continue
        rows.append(dict(id=row["id"], story_key=row["story_key"], source=row["source"],
                         target=row["targets"][language]))
    # Choose different stories before additional mentions of the same story.
    seen, first, remaining = set(), [], []
    for row in rows:
        if row["story_key"] not in seen:
            first.append(row)
            seen.add(row["story_key"])
        else:
            remaining.append(row)
    selected = (first + remaining)[:_MAX_PAIRS]
    if not selected:
        return None
    context = dict(schema=_SCHEMA, task="translation", scope_id=scope_id,
                   source_language=scope["source_language"], rows=selected,
                   available_stories=len(seen), available_spans=len(rows))
    return _item(term, language, candidates, "pending" if candidates else "gate_failed",
                 context, "判断所给同位语句中的对应词；可替换候选，禁止凭记忆另译；不确定则留在队列。")


def _occurrence_item(item):
    """Use the existing queue/CLI for a bounded occurrence-only judgment."""
    context = dict(item._context, task="occurrence", parent_translation_id=item.id)
    return _item(item.term, item.language, item.candidates, "pending", context,
                 "Judge the exact source occurrence and actual target expression. Submit explicit raw segments and contextual senses; this task never publishes a global name.")


def _focused_occurrence_item(translation_item, source_anchor, sense):
    """Review one specified raw occurrence, not a convenient same-word neighbor."""
    from sekaisync import occurrence_store as occurrences
    if translation_item._context.get("task") != "translation":
        raise ValueError("focused occurrence requires an immutable translation packet")
    rows = []
    for row in translation_item._context["rows"]:
        try:
            anchor = occurrences._anchor(row["source"], row["story_key"], source_anchor["segments"])
        except (KeyError, TypeError, ValueError):
            continue
        if anchor == source_anchor:
            rows.append(row)
    if not rows:
        raise ValueError("focused source occurrence is outside this translation packet")
    context = dict(translation_item._context, task="occurrence", rows=rows,
                   parent_translation_id=translation_item.id,
                   available_stories=len({row["story_key"] for row in rows}), available_spans=len(rows),
                   focus=dict(source=source_anchor, sense=sense))
    item = _item(translation_item.term, translation_item.language, translation_item.candidates,
                 "pending", context,
                 "Judge only the specified exact source occurrence and contextual sense; do not borrow another same-word occurrence. This task never publishes a global name.")
    _validate_occurrence_focus(item)
    return item


def _validate_occurrence_focus(item):
    from sekaisync import occurrence_store as occurrences
    context = item._context
    focus = context.get("focus")
    if focus is None:
        return
    if context.get("task") != "occurrence" or not isinstance(focus, dict) or set(focus) != {"source", "sense"}:
        raise ValueError("invalid focused occurrence contract")
    source, sense = focus["source"], focus["sense"]
    if not isinstance(source, dict) or not isinstance(sense, dict):
        raise ValueError("focused occurrence requires immutable source and sense")
    subject = _subject_identity(item)
    expected_sense = occurrences._sense(subject, context["source_language"], sense.get("key"), sense.get("gloss"))
    if expected_sense != sense:
        raise ValueError("focused sense does not belong to this lexical subject")
    if not context.get("rows"):
        raise ValueError("focused occurrence has no source windows")
    for row in context["rows"]:
        if (occurrences._anchor(row["source"], row["story_key"], source.get("segments")) != source
                or not _source_selection(item, row["source"], row["story_key"], source["segments"])):
            raise ValueError("focused occurrence must select the same exact source anchor in every row")


def _connection_store(conn):
    database = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not database:
        raise ValueError("occurrence expansion requires a persistent corpus")
    return Path(database).parent.parent


def _expanded_view(conn, initial, stage, page_cache):
    _validate_view(conn, initial, page_cache)
    raw = str(page_cache[(initial["source"], initial["page_id"])][0] or "")
    from sekaisync.wording_identity import _view_policy
    if _view_policy(initial):
        return dict(initial, start=0, end=len(raw), text=raw, complete=True), True
    radius = _EXPANSION_RADII[stage - 1]
    if radius is None:
        start, end = 0, len(raw)
    else:
        _, offsets = _lines(dict(text=raw))
        members = [index for index, (a, b) in enumerate(offsets)
                   if b > initial["start"] and a < initial["end"]]
        if not members:
            raise ValueError("occurrence expansion has no original raw lines")
        start = offsets[max(0, members[0] - radius)][0]
        end = offsets[min(len(offsets) - 1, members[-1] + radius)][1]
    if end - start > _EXPANSION_CHARS:
        # Preserve the original focus; an oversized page is never called full.
        start = max(0, min(initial["start"] - _EXPANSION_CHARS // 2,
                           len(raw) - _EXPANSION_CHARS))
        end = min(len(raw), start + _EXPANSION_CHARS)
        if initial["start"] < start or initial["end"] > end:
            raise ValueError("original occurrence exceeds expansion budget")
    view = dict(initial, start=start, end=end, text=raw[start:end], complete=True)
    return view, start == 0 and end == len(raw)


def _occurrence_expansion_item(conn, store, relation, persist=True):
    """Create at most three wider, immutable tasks for one unresolved anchor."""
    from sekaisync import agent_review as ar, occurrence_store as occurrences
    occurrences._validate_relation(conn, relation)
    if relation["kind"] != "unresolved" or not relation.get("structural_grounding"):
        return None
    proof = relation["grounding"]
    parent_context = proof["context"]
    if "scan" in parent_context or (parent_context.get("fallback")
                                      and not parent_context["fallback"]["target_complete_page"]):
        return None
    previous = parent_context.get("expansion") or {}
    parent_row = next(row for row in parent_context["rows"] if row["id"] == proof["row_id"])
    initial_source = previous.get("initial_source_view", parent_row["source"])
    initial_target = previous.get("initial_target_view", parent_row["target"])
    page_cache = {}
    for stage in range(previous.get("stage", 0) + 1, len(_EXPANSION_RADII) + 1):
        source, source_full = _expanded_view(conn, initial_source, stage, page_cache)
        target, target_full = _expanded_view(conn, initial_target, stage, page_cache)
        if (source["start"], source["end"], target["start"], target["end"]) != (
                parent_row["source"]["start"], parent_row["source"]["end"],
                parent_row["target"]["start"], parent_row["target"]["end"]):
            break
    else:
        return None
    expansion = dict(schema="sekaisync/occurrence-expansion@1", stage=stage,
                     level="full_page" if target_full else ("bounded_page" if stage == 3
                     else ("adjacent" if stage == 1 else "wide")),
                     parent_relation_id=relation["id"],
                     root_relation_id=previous.get("root_relation_id", relation["id"]),
                     ancestor_relation_ids=list(previous.get("ancestor_relation_ids", [])) + [relation["id"]],
                     focus_source=relation["source"], sense=relation["sense"],
                     initial_source_view=initial_source, initial_target_view=initial_target,
                     source_complete_page=source_full, target_complete_page=target_full,
                     exhausted=stage == len(_EXPANSION_RADII) or (source_full and target_full))
    window = dict(story_key=relation["story_key"], source=source,
                  targets={relation["target_language"]: target})
    window["id"] = "span:" + _digest(window)[:24]
    scope = dict(source_language=parent_context["source_language"], target_languages=[relation["target_language"]],
                 windows=[window], expansion=expansion)
    scope_id = _digest(scope)
    if persist:
        path = _scope_path(store, scope_id)
        if not path.exists():
            ar._write_json(path, scope)
        else:
            _read_scope(store, scope_id)
    context = dict(schema=_SCHEMA, task="occurrence", scope_id=scope_id,
                   source_language=parent_context["source_language"],
                   rows=[dict(id=window["id"], story_key=window["story_key"], source=source, target=target)],
                   available_stories=1, available_spans=1, expansion=expansion,
                   parent_translation_id=parent_context.get("parent_translation_id"))
    if parent_context.get("subject") is not None:
        context["subject"] = parent_context["subject"]
    if parent_context.get("fallback") is not None:
        context["fallback"] = parent_context["fallback"]
    return _item(proof["term"], relation["target_language"], proof["candidates"], "pending", context,
                 "Recheck this exact unresolved source occurrence in wider raw context. Preserve its source segments and sense; do not invent a lexical target or publish a global name.")


def _validate_occurrence_expansion(conn, store, item):
    expansion = item._context.get("expansion")
    if expansion is None:
        return
    if not isinstance(expansion, dict) or not isinstance(expansion.get("parent_relation_id"), str):
        raise ValueError("invalid occurrence expansion ancestry")
    row = conn.execute("SELECT payload_json FROM scraper_relations WHERE id=?",
                       (expansion["parent_relation_id"],)).fetchone()
    if row is None:
        raise ValueError("occurrence expansion parent is absent")
    parent = json.loads(row[0])
    expected = _occurrence_expansion_item(conn, store, parent, persist=False)
    if expected is None or expected.id != item.id or expected._context != item._context:
        raise ValueError("occurrence expansion does not reproduce its authentic parent and wider raw windows")
    receipts = _receipts(store, conn=conn)
    if parent["review_item_id"] not in receipts:
        raise ValueError("occurrence expansion parent has no completed review receipt")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_relation_retirements'").fetchone():
        if conn.execute("SELECT 1 FROM scraper_relation_retirements WHERE old_id=?", (parent["id"],)).fetchone():
            raise ValueError("occurrence expansion parent has already been superseded")


def ensure_occurrence_expansions(store):
    """Recover missed follow-ups through the existing export path, idempotently."""
    from sekaisync import agent_review as ar, dbstore, occurrence_store as occurrences
    if not (Path(store) / "kb" / "sekaisync.db").exists():
        return dict(added=0)
    items = []
    with dbstore.connect(store) as conn:
        receipts = _receipts(store, conn=conn)
        for relation in occurrences._read_relations(conn):
            if relation["kind"] != "unresolved" or relation["review_item_id"] not in receipts:
                continue
            item = _occurrence_expansion_item(conn, store, relation)
            if item is not None:
                items.append(item)
    return ar.enqueue(store, items) if items else dict(added=0)


def _cohesion_origin(conn, store, relation, pages=None):
    """Recover the original multilingual scope, not an expansion's one target."""
    from sekaisync import occurrence_store as occurrences
    original, seen = relation, set()
    while original["grounding"]["context"].get("expansion"):
        if original["id"] in seen:
            raise ValueError("cyclic occurrence expansion ancestry")
        seen.add(original["id"])
        expansion = original["grounding"]["context"]["expansion"]
        record = conn.execute("SELECT payload_json FROM scraper_relations WHERE id=?",
                              (expansion.get("parent_relation_id"),)).fetchone()
        if record is None:
            raise ValueError("cohesion origin has no occurrence expansion parent")
        parent = json.loads(record[0])
        expected = _occurrence_expansion_item(conn, store, parent, persist=False)
        if (expected is None or expected.id != original["review_item_id"]
                or expected._context != original["grounding"]["context"]
                or parent["source"] != relation["source"] or parent["sense"] != relation["sense"]):
            raise ValueError("cohesion origin does not reproduce authentic expansion ancestry")
        original = parent
    occurrences._validate_relation(conn, original, pages)
    proof = original["grounding"]
    scope_id = (proof["context"].get("fallback") or {}).get("origin_scope_id", proof["scope_id"])
    return scope_id, _read_scope(store, scope_id), proof["term"], proof["context"].get("subject")


def _cohesion_focus_item(conn, scope_id, scope, term, language, source, sense, page_cache, subject=None, inventory=None):
    """Select every original scope row containing the focus, without sampling."""
    from sekaisync import occurrence_store as occurrences
    rows = []
    for window in scope["windows"]:
        target = window["targets"].get(language)
        view = window["source"]
        if target is None or not view["complete"] or not target["complete"]:
            continue
        try:
            if (occurrences._anchor(view, window["story_key"], source["segments"]) != source
                    or (not _subject_in_view(subject, view, window["story_key"]) if subject is not None
                        else not _term_selection(view, term, source["segments"], case_sensitive=True))):
                continue
            _validate_view(conn, view, page_cache)
            _validate_view(conn, target, page_cache)
        except (KeyError, TypeError, ValueError):
            continue
        rows.append(dict(id=window["id"], story_key=window["story_key"], source=view, target=target))
    if not rows:
        if subject is None:
            return None
        fallback, gap = _subject_fallback_item(conn, _connection_store(conn), scope_id, subject, language,
                                              inventory=inventory)
        if fallback is None:
            return _subject_gap_item(scope_id, scope, subject, language, gap, sense=sense)
        context = dict(fallback._context, task="translation")
        translation = _item(term, language, [], "gate_failed", context,
                            "Review current same-content fallback evidence for the fixed subject and sense.")
        return _focused_occurrence_item(translation, source, sense)
    context = dict(schema=_SCHEMA, task="translation", scope_id=scope_id,
                   source_language=scope["source_language"], rows=rows,
                   available_stories=len({row["story_key"] for row in rows}), available_spans=len(rows))
    if subject is not None:
        context["subject"] = subject
    translation = _item(term, language, [], "gate_failed", context,
                        "Review the original raw windows containing one exact source occurrence.")
    return _focused_occurrence_item(translation, source, sense)


def _known_occurrence_work(conn, store):
    from sekaisync import agent_review as ar, occurrence_store as occurrences
    identities, focuses = set(_receipts(store, conn=conn)), set()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_queue'").fetchone():
        for identity, language, scope_json, status in conn.execute(
                "SELECT item_id,language,scope_json,status FROM review_queue"):
            identities.add(identity)
            context = json.loads(scope_json or "{}").get("review_context") or {}
            focus = context.get("focus")
            if focus and status == "queued":
                focuses.add((focus["source"]["id"], focus["sense"]["id"],
                             occurrences._language(language)))
    for raw in ar._read_json(ar.queue_path(store)).get("items", []):
        item = ar.ReviewItem.from_dict(raw)
        identities.add(item.id)
        focus = item._context.get("focus")
        if focus:
            focuses.add((focus["source"]["id"], focus["sense"]["id"], occurrences._language(item.language)))
    return identities, focuses


def _occurrence_cohesion_items(conn, store, relations=None, limit=200):
    """Close missing directions for an exact anchor and sense, never a word."""
    from sekaisync import occurrence_store as occurrences
    current = occurrences._read_relations(conn) if relations is None else relations
    groups = {}
    for relation in current:
        if relation.get("structural_grounding"):
            key = (relation["source"]["id"], relation["sense"]["id"])
            groups.setdefault(key, []).append(relation)
    identities, pending_focuses = _known_occurrence_work(conn, store)
    items, page_cache, validation_pages = [], {}, {}
    inventory = validation_pages.setdefault("subject_fallback_inventory", {})
    for key, records in sorted(groups.items()):
        source, sense = records[0]["source"], records[0]["sense"]
        judged = {record["target_language"] for record in records}
        origins = {}
        for record in records:
            try:
                scope_id, scope, term, subject = _cohesion_origin(conn, store, record, pages=validation_pages)
            except (ValueError, OSError):
                continue
            origins[scope_id] = (scope, term, subject)
        for scope_id, (scope, term, subject) in sorted(origins.items()):
            for language in sorted(set(scope["target_languages"])):
                canonical = occurrences._language(language)
                focus_key = key + (canonical,)
                if canonical in judged or focus_key in pending_focuses:
                    continue
                child = _cohesion_focus_item(conn, scope_id, scope, term, language, source, sense, page_cache,
                                             subject, inventory=inventory)
                if child is None or child.id in identities:
                    continue
                items.append(child)
                identities.add(child.id)
                pending_focuses.add(focus_key)
                fallback = child._context.get("fallback")
                if fallback and fallback["unshown_target_regions"]:
                    target = child._context["rows"][0]["target"]
                    gap = dict(reason="unscanned_target_page_regions", story_key=source["story_key"],
                               language=language, target_page_source=target["source"], target_page_id=target["page_id"],
                               target_page_sha256=target["sha256"], unshown_target_regions=fallback["unshown_target_regions"])
                    debt = _subject_gap_item(scope_id, scope, subject, language, gap, sense=sense)
                    if debt.id not in identities:
                        items.append(debt)
                        identities.add(debt.id)
                if limit > 0 and len(items) >= limit:
                    return items
    return items


def ensure_occurrence_cohesion(store, limit=200):
    """Recover unfinished same-occurrence language work through normal export."""
    from sekaisync import agent_review as ar, dbstore
    if not (Path(store) / "kb" / "sekaisync.db").exists():
        return dict(added=0)
    from sekaisync import cohesion_work
    converged = cohesion_work.converge(store)
    with dbstore.connect(store) as conn:
        items = _occurrence_cohesion_items(conn, store, limit=limit)
    result = ar.enqueue(store, items) if items else dict(added=0)
    result["retired"] = converged["retired"]
    return result


def ensure_subject_gaps(store):
    """Retry named evidence debt without pretending that unavailable means done."""
    from sekaisync import agent_review as ar, dbstore, occurrence_store as occurrences
    from sekaisync.fetcher import store_writer_lock
    if not (Path(store) / "kb" / "sekaisync.db").exists():
        return dict(added=0, retired=0)
    gaps = [item for item in ar.load_queue(store) if item._context.get("task") == "subject_gap"
            and item._context["gap"].get("reason") not in {"unscanned_target_page_regions", "subject_scan_terminal_review_pending"}]
    items, retired, inventory = [], [], {}
    with dbstore.connect(store) as conn:
        current = occurrences._read_relations(conn, subject_ids=sorted({item._context["subject"]["id"] for item in gaps})) if gaps else []
        matching = {}
        for record in current:
            matching.setdefault((record["source"]["id"], record["target_language"]), []).append(record)
        for item in gaps:
            context, subject = item._context, item._context["subject"]
            focus = context.get("focus")
            records = matching.get((subject["source"]["id"], occurrences._language(item.language)), [])
            if focus:
                records = [record for record in records if record["sense"]["id"] == focus["sense"]["id"]]
            if any(record["sense"]["term_id"] == subject["id"] and record["kind"] in {"lexical", "paraphrase", "reference"}
                   for record in records):
                retired.append(item.id)
                continue
            try:
                child, gap = _subject_fallback_item(conn, store, context["scope_id"], subject, item.language,
                                                    inventory=inventory)
            except (ValueError, OSError):
                continue  # A changed source is not permission to rebuild its old subject.
            if gap == context["gap"]:
                continue
            if child is not None:
                if focus:
                    translation = _item(item.term, item.language, [], "gate_failed",
                                        dict(child._context, task="translation"), "Recovered fixed-sense raw evidence")
                    child = _focused_occurrence_item(translation, focus["source"], focus["sense"])
                items.append(child)
            if gap is not None:
                scope = _read_scope(store, context["scope_id"])
                items.append(_subject_gap_item(context["scope_id"], scope, subject, item.language, gap,
                                               sense=focus["sense"] if focus else None))
            retired.append(item.id)
    result = ar.enqueue(store, items) if items else dict(added=0)
    if retired:
        with store_writer_lock(store):
            if ar._uses_sqlite(store):
                with dbstore.connect(store) as conn:
                    changed = sum(conn.execute("UPDATE review_queue SET status='superseded' WHERE item_id=? AND status='queued'",
                                               (identity,)).rowcount for identity in retired)
                    if changed:
                        dbstore.bump_revision(conn)
                    conn.commit()
            else:
                payload = ar._read_json(ar.queue_path(store))
                payload["items"] = [raw for raw in payload.get("items", []) if raw.get("id") not in retired]
                ar._write_json(ar.queue_path(store), payload)
    result["retired"] = len(retired)
    return result


def _discovery_surface_key(value):
    return unicodedata.normalize("NFKC", value).strip()


def _continuation_context(context, parent_id, terms, subjects=None):
    previous = context.get("continuation") or {}
    ancestors = list(previous.get("ancestor_ids", [])) + [parent_id]
    excluded = sorted(set(previous.get("excluded_terms", [])) | set(terms))
    child = {key: value for key, value in context.items() if key != "continuation"}
    child["continuation"] = dict(parent_id=parent_id, root_id=ancestors[0],
                                 ancestor_ids=ancestors, excluded_terms=excluded)
    if subjects is not None or "excluded_subject_ids" in previous:
        child["continuation"]["excluded_subject_ids"] = sorted(
            set(previous.get("excluded_subject_ids", [])) | {subject["id"] for subject in subjects or []})
    return child


def _validate_discovery_continuation(store, item, conn=None):
    """Rebuild the whole chain from real receipts, not agent-supplied exclusions."""
    context = item._context
    continuation = context.get("continuation")
    if continuation is None:
        return set()
    if not isinstance(continuation, dict):
        raise ValueError("invalid discovery continuation")
    if conn is None and "excluded_subject_ids" in continuation:
        database = (Path(store) / "kb" / "sekaisync.db").resolve()
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            return _validate_discovery_continuation(store, item, conn=connection)
    ancestors = continuation.get("ancestor_ids")
    if (not isinstance(ancestors, list) or not ancestors
            or any(not isinstance(identity, str) for identity in ancestors)
            or len(ancestors) != len(set(ancestors))):
        raise ValueError("invalid discovery continuation ancestry")
    rows = context["rows"]
    original_term = "@discover:" + rows[0]["story_key"] + ":" + rows[0]["id"]
    parent_context = {key: value for key, value in context.items() if key != "continuation"}
    receipts = _receipts(store, conn=conn)
    for identity in ancestors:
        parent = _item(original_term, context["source_language"], [], "discovery", parent_context, "")
        if identity != parent.id:
            raise ValueError("discovery continuation ancestry does not match its source packet")
        receipt = receipts.get(identity)
        if not isinstance(receipt, dict):
            raise ValueError("discovery continuation parent has no submitted receipt")
        if receipt.get("scope_id") != context["scope_id"]:
            raise ValueError("discovery continuation parent belongs to another scope")
        try:
            submitted = json.loads(receipt["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid discovery continuation parent receipt") from exc
        typed = isinstance(submitted, dict)
        if typed:
            if set(submitted) != {"terms", "subjects"}:
                raise ValueError("invalid typed discovery continuation parent receipt")
            terms, subjects = submitted["terms"], submitted["subjects"]
        else:
            terms, subjects = submitted, None
        if (receipt.get("decision") != "accept" or not isinstance(terms, list)
                or len(terms) > _DISCOVERY_TERMS or any(not _valid_surface(term) for term in terms)
                or len({_discovery_surface_key(term) for term in terms}) != len(terms)
                or (not typed and len(terms) != _DISCOVERY_TERMS)
                or (typed and (not isinstance(subjects, list) or len(subjects) > _DISCOVERY_TERMS
                               or (len(terms) != _DISCOVERY_TERMS and len(subjects) != _DISCOVERY_TERMS)))):
            raise ValueError("discovery continuation parent did not submit a full unique budget")
        already = {_discovery_surface_key(term) for term in
                   (parent_context.get("continuation") or {}).get("excluded_terms", [])}
        for term in terms:
            if _discovery_surface_key(term) in already or not any(
                    _source_term_present(row["source"], term) for row in rows):
                raise ValueError("discovery continuation parent receipt is not fresh grounded evidence")
        if subjects is not None:
            already_subjects = set((parent_context.get("continuation") or {}).get("excluded_subject_ids", []))
            submitted_subjects = set()
            for subject in subjects:
                _validate_subject_in_rows(conn, subject, rows)
                if subject["id"] in already_subjects or subject["id"] in submitted_subjects:
                    raise ValueError("discovery continuation subject receipt is not fresh grounded evidence")
                submitted_subjects.add(subject["id"])
        parent_context = _continuation_context(parent_context, identity, terms, subjects)
    if (context != parent_context or item.term != original_term or item.candidates
            or item.language != context["source_language"]):
        raise ValueError("discovery continuation exclusions or lineage were changed")
    return {_discovery_surface_key(term) for term in continuation["excluded_terms"]}


def _prepare_scrub_review(store, groups, stories, candidates, result,
                          source_language, target_languages):
    """Persist one immutable corpus view and create resumable work items."""
    from sekaisync import agent_review as ar
    from sekaisync.termindex import _term_language
    source_language = _term_language(source_language)
    targets = list(dict.fromkeys(_term_language(v) for v in target_languages
                                 if _term_language(v) != source_language))
    scope = dict(schema=_SCHEMA, source_language=source_language, target_languages=targets,
                 windows=_scope_windows(groups, stories, source_language, targets))
    if getattr(groups, "wording_debts", None):
        scope["_wording_debts"] = groups.wording_debts
    if getattr(groups, "wording_contributors", None):
        scope["_wording_contributors"] = groups.wording_contributors
    scope_id = _digest(scope)
    path = _scope_path(store, scope_id)
    if not path.exists():
        ar._write_json(path, scope)
    items, proposals = [], {}
    for decision in result.get("slot_decisions", []):
        key = (decision.get("term"), _term_language(decision.get("language", "")))
        values = []
        for value in decision.get("candidates", []):
            value = value.get("value") if isinstance(value, dict) else value
            if _valid_surface(value):
                values.append(value)
        if _valid_surface(decision.get("value")):
            values.append(decision["value"])
        proposals.setdefault(key, set()).update(values)
    for row in result.get("pending", []):
        for language, value in (row.get("names") or {}).items():
            if _valid_surface(value):
                proposals.setdefault((row.get("term"), _term_language(language)), set()).add(value)
    for term in sorted(set(candidates)):
        if not _valid_surface(term):
            continue
        for language in targets:
            # Algorithmically accepted slots still get an audit packet. The
            # store may have demoted them, and acceptance is not semantic gold.
            item = _translation_item(scope_id, scope, term, language,
                                     proposals.get((term, language), ()))
            if item:
                items.append(item)
                if item._context["available_stories"] < 2:
                    items.append(_occurrence_item(item))
    # Discovery does not depend on a tokenizer finding a candidate first.
    batch, chars, story = [], 0, None

    def flush():
        if not batch:
            return
        context = dict(schema=_SCHEMA, task="discovery", scope_id=scope_id,
                       source_language=source_language, rows=list(batch))
        name = "@discover:" + batch[0]["story_key"] + ":" + batch[0]["id"]
        items.append(_item(name, source_language, [], "discovery", context,
                           "逐原文台词补找名词、修饰指称、完整谓词与功能表达；复核语义层级和原文边界，不依赖现有候选。"))

    for row in scope["windows"]:
        if batch and (story != row["story_key"] or len(batch) >= _DISCOVERY_ROWS
                      or chars + len(row["source"]["text"]) > _DISCOVERY_CHARS):
            flush()
            batch, chars = [], 0
        batch.append(row)
        chars += len(row["source"]["text"])
        story = row["story_key"]
    flush()
    # Alternate tasks so low-yield unresolved translations cannot hide discovery.
    translations = [i for i in items if i.kind != "discovery"]
    discoveries = [i for i in items if i.kind == "discovery"]
    ordered = []
    for index in range(max(len(translations), len(discoveries))):
        if index < len(discoveries):
            ordered.append(discoveries[index])
        if index < len(translations):
            ordered.append(translations[index])
    metadata = dict(scope_id=scope_id, source_windows=len(scope["windows"]),
                    discovery_packets=len(discoveries),
                    translation_packets=sum(i._context["task"] == "translation" for i in translations),
                    model_api_calls=0)
    if "_wording_debts" in scope:
        metadata["wording_debts"] = scope["_wording_debts"]
    return ordered, metadata


def _render_context(item):
    from sekaisync import source_audits, span_subjects
    from sekaisync.wording_identity import _view_policy
    context = item._context
    metadata = {k: v for k, v in context.items() if k != "rows"}
    audit_marker = context.get("source_boundary_audit")
    if source_audits._is_audit(context) and isinstance(audit_marker, dict):
        # Show raw turns before prior proposals and avoid duplicating large ID lists.
        metadata["source_boundary_audit"] = {
            key: value for key, value in audit_marker.items()
            if key not in {"excluded_terms", "excluded_subject_ids", "inherited_subjects"}}
        metadata["source_boundary_audit"]["inherited_term_count"] = len(audit_marker.get("excluded_terms", []))
        metadata["source_boundary_audit"]["inherited_subject_count"] = len(audit_marker.get("inherited_subjects", []))
    lines = ["task_context: " + json.dumps(metadata, ensure_ascii=False)]
    for row in context.get("rows", []):
        # Discovery needs only source text. Translation includes both full turns.
        views = {"source": row["source"]}
        if context["task"] in {"translation", "occurrence"}:
            views["target"] = row["target"]
        visible = dict(id=row["id"], story_key=row["story_key"])
        for name, view in views.items():
            visible[name] = {k: v for k, v in view.items() if k != "sha256"}
            limit = _EXPANSION_CHARS if context.get("expansion") or context.get("fallback") else _TURN_CHARS
            # Wording views retain their authorized whole parsed value.
            if len(visible[name]["text"]) > limit and not _view_policy(view):
                visible[name]["text"] = visible[name]["text"][:limit]
                visible[name]["complete"] = False
        lines.append("context: " + json.dumps(visible, ensure_ascii=False))
        if context["task"] == "discovery":
            shown = dict(row["source"], text=visible["source"]["text"],
                         end=row["source"]["start"] + len(visible["source"]["text"]))
            lines.append("source_body_ranges: " + json.dumps(dict(
                evidence_id=row["id"], utterances=span_subjects._utterance_body_ranges(shown)), ensure_ascii=False))
    if context["task"] == "discovery":
        lines.append("source_body_ranges_contract: Select only listed body_ranges; ordinary dialogue speaker headers are context metadata, never selectable source subjects or terms. All fragments of one subject must belong to one listed utterance. Native wording whole-value views retain all raw value text as one selectable body. These ranges do not identify meaningful units or prove exhaustive acquisition.")
    if context["task"] in {"translation", "occurrence"}:
        lines.append("position_contract: start/end are absolute half-open Unicode code-point offsets into raw page text; segments contain exact original text. Repeated matches require explicit evidence_spans; never select the first match by default.")
    if context["task"] == "discovery" and not source_audits._is_saturated(context):
        capacity = ("a full list leaves visible source_boundary_audit_saturated debt; this one bounded review does not recurse."
                    if source_audits._is_audit(context) else
                    "a full list creates resumable continuation with separate term and subject-id exclusions.")
        lines.append("discovery_contract: decision=accept; legacy terms remain exact 1-80 character surfaces without internal ASCII C0 control characters. Optional typed literal subjects allow up to 1800 raw Unicode code points within one actually shown utterance window; long or multiline canonical text must equal its complete raw selected envelope, including LF/CRLF, with no other C0 controls. Existing short flat-softwrap literals remain valid. terms may be omitted or empty for typed-only answers; do not mirror long or raw multiline canonicals into legacy terms. Optional subjects=[{kind:literal,evidence_id,canonical,segments:[{start,end,exact}]} or {kind:segmented,evidence_id,segments:[{start,end,exact}]}] selects one exact utterance body. Segmented subjects require two or more raw fragments and accept no invented canonical alias. Offsets are absolute half-open Unicode code points. Each list permits at most 200 observations; " + capacity)
        lines.append("discovery_native_literal_contract: A fully validated complete native wording whole-value row may instead select an exact adjacent-fragment literal up to that actual displayed raw value's full length. This exception never enlarges ordinary dialogue windows or legacy terms, permits no normalized alias, and retains exact LF/CRLF and the other-C0-control prohibition.")
        lines.append("discovery_boundaries: Before inner-layer enumeration, for each complete main proposition or question register its complete linguistic speech-act envelope, retaining attached source-attested recognition/response interjections, discourse operators and vocatives; register its meaningful detachable clause core separately without losing necessary participants, restrictions, arguments, logical operators or grammatical sentence-force particles. Choose this envelope by source-attested grammatical attachment and scope, not orthographic sentence breaks alone: within the same utterance, a postposed causal, explanatory, conditional or other subordinate clause belongs to the main act when its grammar depends on and qualifies that act rather than forming a new independent speech act, even after a period. Register the full main-plus-dependent composite with its internal punctuation intact and its meaningful detachable cores separately. A connective-looking word, thematic continuity or a stated reason alone does not prove grammatical dependence. Preserve leading raw pause marks in a justified pause-bearing envelope and also register the meaningful pause-free discourse clause. End these linguistic composites before the outermost terminal sentence punctuation (. ! ? U+3002 U+FF01 U+FF1F) of the complete envelope, retaining grammatical question/force particles and all internal punctuation, quotation marks and raw LF/CRLF. A meaningful punctuation-bearing speech-force surface may be an additional layer, never a replacement for the linguistic envelope. Keep independent prior responses and neighboring independent main clauses separate regardless of punctuation or semantic relatedness; never fuse across speaker or turn boundaries. Do not invent implicit words or enumerate arbitrary punctuation trims. Separate fragments, a detached interjection or a wider punctuated surface do not register the missing exact composite. During the boundary audit compare each intended envelope/core with the actually inherited selections, not merely with its component words.")
        lines.append("discovery_passes: review each shown source turn individually before moving to the next. First register each meaningful complete proposition as its own composite: preserve its attested subject, topic or address, participants and required arguments/complements, restrictions, degree/comparison, logical operators (negation, modality, causation and condition), and relevant discourse force or sentence particles. Do not invent implicit words or mechanically include unrelated greetings, neighboring propositions or punctuation. Then select its meaningful inner layers: noun heads, compounds and modified/nested or grammatically marked referring expressions; complete inflected predicates (including negation, modality and complements), adjectives, adverbs, function expressions and idioms. Preserve complete contractions and inflected predicates AND register their recognized contextual bound grammatical layers as additional exact observations, not replacements for the complete forms. Finally recheck open-slot and discontinuous constructions: keep their real raw parts and gaps, not unrelated fragment terms or an invented continuous envelope. Register both the complete composite and its meaningful internal expressions; a noun and a predicate submitted separately do not register their proposition. Prefer literal or segmented subjects for each specific occurrence; the existing terms list remains valid. Do not enumerate arbitrary substrings or substitute a wordhead/dictionary lemma for a complete expression. Check repeated occurrences individually, preserve spelling/case, and review even turns with no lexical content.")
        lines.append("discovery_boundary_audit: after the initial selections, reread every source turn without looking at any target language. First check whether its complete meaningful proposition was registered with the attested subject/topic/address, necessary arguments, restrictions, logical operators and sentence force intact, not merely a predicate phrase or a collection of fragments. Check meaningful minimal and extended layers separately: a nominal head, its determiner-bearing or case/topic-marked reference and its actual restrictive modifier; an inflected core predicate, its auxiliary/negation/modality and its selected complement; then recognized contextual bound grammatical layers and any detachable discourse or sentence-particle layer. A wider clause does not register its meaningful inner expression, and overlapping fragments do not register their composite. Keep original contractions and tense/politeness inflection intact AND register recognized contextual bound grammatical layers as additional exact observations: auxiliaries (including clitic auxiliaries), negation/genitive clitics, case/topic/quotative functions and complete ending-plus-auxiliary constructions. A recognized bound layer need not be orthographically detachable; a complete form does not register its meaningful inner layer, and an inner layer does not replace the complete form. Require an actual contextual grammatical role, not a matching suffix string or arbitrary stem/suffix split; never invent a lemma, expand a contraction into an absent uncontracted alias, or mechanically trim suffixes. Preserve realized raw spelling/Unicode, exact offsets and LF/CRLF. Register a complete comparison or modal-plus-complement when its meaning is not captured by the selected fragments. Select the resulting additional exact literal or genuine discontinuous subjects before moving on; do not enumerate all substrings or claim exhaustive coverage from a short list.")
    if source_audits._is_audit(context):
        lines.extend(source_audits._render_contract(item))
        if isinstance(audit_marker, dict) and not source_audits._is_saturated(context):
            lines.append("source_boundary_audit_inherited: " + json.dumps(
                {key: audit_marker.get(key, []) for key in ("excluded_terms", "inherited_subjects")},
                ensure_ascii=False))
    if context["task"] == "subject_gap":
        lines.append("gap_contract: this named subject-language debt is not a semantic judgment task. Do not submit accept/reject to erase it. Recover missing current localized evidence or inspect the exact named unshown raw regions; normal export retries available page evidence without changing the subject.")
    if context["task"] == "occurrence":
        lines.append("answer_contract: decision=accept; relations=[{evidence_id,source_segments:[{start,end,exact}],target_segments:[{start,end,exact}],sense_key,sense_gloss,kind,rationale}]. kind is lexical/paraphrase/reference/omitted/unresolved; for omitted/unresolved target_segments cite examined context, not an invented expression. No global slots are written.")
        lines.append("semantic_contract: preserve the exact source occurrence and its contextual meaning, not a demand for identical target grammar. A target may express the same meaning with another word class, word order, inflection, number of words, or a nominal-to-predicate restructuring. A missing source-shaped head, copula, determiner or particle alone does not establish omission. Conversely, alignment, a shared referent or a nearby related quality alone does not establish equivalent meaning; retain any unsupported semantic component rather than inventing target wording.")
        lines.append("semantic_component_check: before assigning kind, identify the source meaning's attested predicate, participants, restricting description and logical operators. Check the target's actual support for each relevant component, including causation, condition, negation, modality and discourse force; distinguish conventional implicit realization from a genuinely absent or unsettled component. When a mapping depends on the target's causal/conditional structure, select the actual parts carrying that dependency, not only the result phrase; do not demand the source operator's count or grammatical form. Reference requires positive contextual evidence for the same referent, not merely a shared topic, an adjacent quality or a plausible translation. Once referent identity is supported, a target reference need not restate the source description; if identity or the selected core relation remains unsettled, use unresolved rather than force equivalence. In the existing rationale state the decisive supported and unsupported/uncertain components before explaining the selected type and complete target boundaries. No new answer fields are required.")
        lines.append("relation_kind_contract: lexical is an actual target lexical or grammatical expression directly realizing the contextual meaning, including conventional equivalents whose internal grammatical packaging differs. paraphrase is an actual contextual reformulation of that meaning, not an invented dictionary translation. reference identifies the same contextual referent without fully restating the source description; it can be an anaphor or a narrower referring phrase, and same referent alone is not lexical equivalence. omitted means the source meaning has no supported realization in the examined target context, not merely no same-shaped phrase. unresolved means the available evidence does not settle the relation. Explain the chosen distinction in the existing rationale field; none of these machine judgments is a semantic certificate.")
        lines.append("relation_kind_decision: (1) Keep the source occurrence/sense fixed; verify target support for its participants, restrictions, operators and discourse force. (2) Word class/order, inflection, function words, word count and nominal-to-predicate restructuring alone cannot justify paraphrase. (3) Use lexical for direct conventional realization; use paraphrase only when the existing rationale identifies an actual content-level reformulation beyond grammatical difference. Keep same-referent-only reference, absent omitted and unsettled unresolved as appropriate; do not weaken the source sense or force lexical. No new answer fields.")
        lines.append("target_boundary_contract: select one complete, meaningful target realization at the semantic layer being judged. Check its actual degree, negation, modality, inflection and restricted complement rather than selecting only an inner wordhead. Do not mechanically add a copula, intensifier, shared noun or a whole clause just to imitate the source syntax; a conventional lexical equivalent need not reproduce every source morpheme. Recheck both boundaries and repeated occurrences against raw text. A wider envelope and a union of separately submitted components do not register the same complete vector. Use exact raw segments for genuine intervening gaps, never close them into a continuous alias.")
        lines.append("omission_check: before omitted, explicitly test whether an actual lexical equivalent, contextual reformulation or reference carries the source meaning despite grammatical restructuring. Distinguish a genuinely absent semantic component from a differently expressed one. Do not use an unrelated condition, another occurrence, or an adjacent consequence as a forced counterpart. Cite the examined context for omission or uncertainty and preserve the scope limit; bounded evidence does not prove absence outside that region.")
        if context.get("expansion"):
            lines.append("expansion_contract: judge exactly one relation for expansion.focus_source.segments; inherit expansion.sense.key/gloss unchanged. Wider context is evidence, not a new source occurrence. full_page means the complete current raw target page was provided; bounded_page does not prove absence outside the shown region.")
        if context.get("focus"):
            lines.append("focus_contract: every relation must preserve focus.source.segments and focus.sense.key/gloss; another occurrence of the same surface is not evidence for this focus.")
        if context.get("subject"):
            lines.append("subject_contract: context.subject is the immutable exact source occurrence. Judge its source.segments unchanged and define a contextual sense for subject.id. Segmented canonical text is display-only, never a searchable alias or permission to delete raw lexical gaps.")
        if context.get("fallback"):
            lines.append("fallback_contract: these current raw windows share content/story identity only; no aligned or semantic counterpart is asserted. For omitted/unresolved cite the complete provided target region. A bounded target never proves omission outside that region; named unshown regions remain explicit pending debt.")
        if "scan" in context:
            from sekaisync import subject_scans
            lines.append("scan_contract: read the entire shown raw target region, preserve the exact source and sense, and submit exactly one lexical/paraphrase/reference/unresolved observation. Also submit reviewed_region=" + json.dumps(subject_scans._review_declaration(context["rows"][0]["target"]), ensure_ascii=False) + ". This freezes an agent declaration of complete region review, not an omission proof. A small expression anchor alone never counts as full-region review; omitted is unavailable.")
    return lines


def _validate_view(conn, view, page_cache):
    key = (view["source"], view["page_id"])
    if key not in page_cache:
        row = conn.execute("SELECT text, language, trust, aux_flag, untranslated, "
                           "content_language_mismatch, kind FROM web_pages WHERE source=? AND id=?", key).fetchone()
        if row is None:
            raise ValueError("source page absent from store; import the original pages before submitting")
        page_cache[key] = row
    row = page_cache[key]
    from sekaisync.termindex import _term_language
    if (row[2] == "D" or row[3] or row[4] or row[5]
            or _term_language(row[1]) != _term_language(view["language"])):
        raise ValueError("page is unverified, auxiliary, untranslated or has changed language")
    text = str(row[0] or "")
    if hashlib.sha256(text.encode()).hexdigest() != view["sha256"]:
        raise ValueError("source page changed; rerun extraction and export fresh evidence")
    if text[view["start"]:view["end"]] != view["text"]:
        raise ValueError("evidence offsets do not match the local page")
    if row[6] == "wordings" or "wording_body" in view:
        from sekaisync.wording_identity import _persisted_page, _validate_view_metadata
        page = _persisted_page(conn, key[0], key[1], cache=page_cache.setdefault("wording_pages", {}))
        _validate_view_metadata(view, page)


def _validate_answer(conn, store, item, judgment):
    """Return a grounded proposal or fail without resolving the work item."""
    context = item._context
    if context.get("task") == "subject_gap":
        raise ValueError("subject-language evidence debt cannot be closed by a semantic judgment; recover the missing page or named raw regions and export again")
    _validate_occurrence_focus(item)
    scope = _read_scope(store, context["scope_id"])
    source_language = context["source_language"]
    if (context.get("schema") != _SCHEMA or context.get("task") not in {"discovery", "translation", "occurrence"}
            or source_language != scope["source_language"]
            or item.id != "arp:" + _digest([item.term, item.language, sorted(set(item.candidates)), context])[:32]):
        raise ValueError("review packet identity does not match its immutable context")
    windows = {row["id"]: row for row in scope["windows"]}
    for row in context["rows"]:
        original = windows.get(row["id"])
        if original is None:
            raise ValueError("review span is absent from its corpus scope")
        expected = original if context["task"] == "discovery" else dict(
            id=original["id"], story_key=original["story_key"], source=original["source"],
            target=original["targets"].get(item.language))
        if row != expected:
            raise ValueError("review span was changed outside its corpus scope")
    subject = context.get("subject")
    if subject is not None:
        from sekaisync import occurrence_store as occurrences
        _validate_subject_in_rows(conn, subject, context["rows"])
        if (context["task"] != "occurrence" or item.term != subject["canonical"]
                or occurrences._language(source_language) != subject["source"]["language"]
                or not all(_subject_in_view(subject, row["source"], row["story_key"]) for row in context["rows"])):
            raise ValueError("typed occurrence packet must preserve its complete immutable subject")
    _validate_subject_fallback(conn, store, context, item.language, item.term)
    page_cache = {}
    if context["task"] == "discovery":
        from sekaisync import source_audits
        terms = judgment.get("terms", [] if "subjects" in judgment else None)
        if judgment.get("decision") != "accept" or not isinstance(terms, list) or len(terms) > _DISCOVERY_TERMS:
            raise ValueError("discovery requires decision=accept and at most 200 source surfaces; a full budget creates continuation work")
        if any(not _valid_surface(term) for term in terms):
            raise ValueError("discovered terms must be complete 1-80 character source surfaces")
        audit = source_audits._is_audit(context)
        excluded = (source_audits._validate(conn, store, item) if audit
                    else _validate_discovery_continuation(store, item, conn=conn))
        if any(_discovery_surface_key(term) in excluded for term in terms):
            raise ValueError("discovered term was already submitted by a continuation ancestor")
        if len({_discovery_surface_key(term) for term in terms}) != len(set(terms)):
            raise ValueError("discovery surfaces repeat under NFKC normalization")
        subjects = _discovery_subjects(conn, context, judgment["subjects"]) if "subjects" in judgment else None
        fresh = []
        for term in dict.fromkeys(terms):
            rows = [row for row in context["rows"] if _source_term_present(row["source"], term)]
            if not rows:
                raise ValueError("discovered term is absent from this packet's source body: " + term)
            for row in rows:
                _validate_view(conn, row["source"], page_cache)
            for language in scope["target_languages"]:
                child = _translation_item(context["scope_id"], scope, term, language)
                if child:
                    fresh.append(child)
                    if child._context["available_stories"] < 2:
                        fresh.append(_occurrence_item(child))
        fallback_inventory = {}
        for subject in subjects or []:
            for language in scope["target_languages"]:
                fresh.extend(_subject_language_items(conn, store, context["scope_id"], scope, subject,
                                                     language, item.id, inventory=fallback_inventory))
        # Validate empty discoveries too: an obsolete packet must not close.
        for row in context["rows"]:
            _validate_view(conn, row["source"], page_cache)
        unique_terms = list(dict.fromkeys(terms))
        if not audit and (len(unique_terms) == _DISCOVERY_TERMS or len(subjects or []) == _DISCOVERY_TERMS):
            child_context = _continuation_context(context, item.id, unique_terms, subjects)
            fresh.insert(0, _item(item.term, item.language, [], "discovery", child_context,
                                 "Continue discovery in the same source windows; exclude all already submitted surfaces; submit an empty list only after checking the remainder."))
        value = unique_terms if subjects is None else dict(terms=unique_terms, subjects=subjects)
        answer = dict(task="discovery", terms=unique_terms, subjects=subjects or [], items=fresh,
                      value=json.dumps(value, ensure_ascii=False), evidence=[])
        return source_audits._after_answer(conn, store, item, answer)

    if context["task"] == "occurrence":
        return _validate_occurrence_answer(conn, item, judgment, page_cache)

    decision = judgment.get("decision")
    value = judgment.get("value") or item.chosen_hint
    if decision == "reject":
        # An explicit negative is local to this evidence version, never a
        # permanent ban on the source word or other senses/languages.
        for row in context["rows"]:
            _validate_view(conn, row["source"], page_cache)
            _validate_view(conn, row["target"], page_cache)
        return dict(task="translation", value="", evidence=[], items=[], rejected=True)
    if not _valid_surface(value) or not str(judgment.get("rationale") or "").strip():
        raise ValueError("translation requires an exact target surface and a contextual rationale")
    selected = judgment.get("evidence_ids")
    if selected is not None and (not isinstance(selected, list) or not selected
                                 or any(not isinstance(v, str) for v in selected)):
        raise ValueError("evidence_ids must be a nonempty array of exported span IDs")
    known = {row["id"] for row in context["rows"]}
    if selected is not None and not set(selected) <= known:
        raise ValueError("evidence_ids contains an unknown span")
    explicit = judgment.get("evidence_spans")
    if explicit is not None:
        if (not isinstance(explicit, list) or not explicit or any(not isinstance(row, dict) for row in explicit)
                or any(row.get("evidence_id") not in known for row in explicit)
                or len({row["evidence_id"] for row in explicit}) != len(explicit)):
            raise ValueError("evidence_spans requires unique exported evidence IDs and raw segments")
        if selected is not None and {row["evidence_id"] for row in explicit} != set(selected):
            raise ValueError("evidence_spans and evidence_ids must cite the same contexts")
        selected = [row["evidence_id"] for row in explicit]
    selectors = {row["evidence_id"]: row for row in explicit or []}
    evidence = []
    for row in context["rows"]:
        if selected is not None and row["id"] not in selected:
            continue
        source, target = row["source"], row["target"]
        if not (source["complete"] and target["complete"]):
            continue  # Agent was not shown the complete aligned context.
        if not _source_term_present(source, item.term):
            continue
        target_choices = _view_term_segments(target, value)
        if not target_choices:
            if selected is not None:
                raise ValueError("target surface absent from cited aligned span")
            continue
        _validate_view(conn, source, page_cache)
        _validate_view(conn, target, page_cache)
        source_choices = _view_term_segments(source, item.term)
        selector = selectors.get(row["id"])
        if selector is None:
            if len(source_choices) != 1 or len(target_choices) != 1:
                raise ValueError("ambiguous occurrence positions; submit explicit evidence_spans instead of first-match selection")
            source_segments, target_segments = source_choices[0], target_choices[0]
        else:
            source_segments, target_segments = selector.get("source_segments"), selector.get("target_segments")
            if not _term_selection(source, item.term, source_segments) or not _term_selection(target, value, target_segments):
                raise ValueError("explicit occurrence segments must exactly select source and target body occurrences")
        evidence.append(dict(story_key=row["story_key"], language=item.language, term=value, value=value,
                             source="corpus", source_language=source_language, source_term=item.term,
                             sentence=target["text"], source_sentence=source["text"],
                             start=target_segments[0]["start"], end=target_segments[-1]["end"],
                             source_start=source_segments[0]["start"], source_end=source_segments[-1]["end"],
                             source_segments=source_segments, target_segments=target_segments,
                             source_page_sha256=source["sha256"], target_page_sha256=target["sha256"],
                             source_page_source=source["source"], target_page_source=target["source"],
                             page_id=target["page_id"], source_page_id=source["page_id"],
                             observed_surface="".join(segment["exact"] for segment in target_segments),
                             review_kind="agent_semantic", review_item_id=item.id,
                             review_rationale=str(judgment["rationale"]),
                             agent=str(judgment.get("agent") or "host-agent"),
                             structural_grounding=True, semantic_guarantee=False))
    if not evidence:
        raise ValueError("proposed translation has no complete, grounded aligned occurrence")
    if len({row["story_key"] for row in evidence}) < 2:
        raise ValueError("insufficient evidence: two distinct stories required; item remains queued")
    return dict(task="translation", value=value, evidence=evidence, items=[])


def _validate_occurrence_answer(conn, item, judgment, page_cache):
    from sekaisync import occurrence_store as occurrences
    from sekaisync import subject_scans
    scan_review = subject_scans._validate_answer(item, judgment)
    _validate_occurrence_focus(item)
    rows = {row["id"]: row for row in item._context["rows"]}
    proposals = judgment.get("relations")
    if judgment.get("decision") != "accept" or not isinstance(proposals, list) or not 1 <= len(proposals) <= 200:
        raise ValueError("occurrence task requires decision=accept and 1-200 grounded relations")
    expansion = item._context.get("expansion")
    if expansion is not None:
        _validate_occurrence_expansion(conn, _connection_store(conn), item)
        if len(proposals) != 1:
            raise ValueError("occurrence expansion must judge exactly its one unresolved focus")
    result, seen = [], set()
    subject = _subject_identity(item)
    for proposal in proposals:
        if not isinstance(proposal, dict) or proposal.get("evidence_id") not in rows:
            raise ValueError("occurrence relation must cite an exported evidence ID")
        row = rows[proposal["evidence_id"]]
        source, target = row["source"], row["target"]
        if not source["complete"] or not target["complete"]:
            raise ValueError("occurrence judgment requires complete context")
        _validate_view(conn, source, page_cache)
        _validate_view(conn, target, page_cache)
        _validate_fallback_observation(item._context, target, proposal.get("kind"), proposal.get("target_segments"))
        source_segments = proposal.get("source_segments")
        if not _source_selection(item, source, row["story_key"], source_segments):
            raise ValueError("source segments must select an exact source term occurrence")
        source_anchor = occurrences._anchor(source, row["story_key"], source_segments)
        target_anchor = occurrences._anchor(target, row["story_key"], proposal.get("target_segments"))
        if proposal.get("kind") not in {"omitted", "unresolved"}:
            from sekaisync.wording_identity import _view_policy
            body_positions = set()
            offset = target["start"]
            for line in target["text"].splitlines(keepends=True):
                body = line if _view_policy(target) else strip_speaker_label(line)
                body_positions.update(range(offset + len(line) - len(body), offset + len(line)))
                offset += len(line)
            if any(index not in body_positions for segment in target_anchor["segments"]
                   for index in range(segment["start"], segment["end"])):
                raise ValueError("target expression must be in body text, not speaker metadata")
        sense = occurrences._sense(subject, source_anchor["language"], proposal.get("sense_key"), proposal.get("sense_gloss"))
        if expansion and (source_anchor != expansion["focus_source"] or sense != expansion["sense"]):
            raise ValueError("occurrence expansion must preserve its exact source anchor and contextual sense")
        focus = item._context.get("focus")
        if focus and (source_anchor != focus["source"] or sense != focus["sense"]):
            raise ValueError("focused occurrence must preserve its exact source anchor and contextual sense")
        grounding = dict(scope_id=item._context["scope_id"], row_id=row["id"], term=item.term,
                         candidates=sorted(set(item.candidates)), context=item._context)
        if scan_review is not None:
            grounding["scan_review"] = scan_review
        relation = occurrences._relation(source_anchor, target_anchor, sense, proposal.get("kind"), item.id,
                                         proposal.get("rationale"), str(judgment.get("agent") or "host-agent"), grounding)
        if relation["id"] in seen:
            raise ValueError("duplicate occurrence relation")
        seen.add(relation["id"])
        result.append(relation)
    return dict(task="occurrence", value=json.dumps([row["id"] for row in result]),
                relations=result, evidence=[], items=[])


def _apply_translation(conn, item, answer):
    """Apply only this language, preserving prior authoritative/conflicting slots."""
    from sekaisync import dbstore, term_slots, termindex
    source_language = item._context["source_language"]
    term_id = termindex.make_term_id(source_language, item.term)
    value = answer["value"]
    row = conn.execute("SELECT names_json,official FROM terms WHERE id=?", (term_id,)).fetchone()
    names = json.loads(row[0]) if row else {}
    if names.get(item.language) and names[item.language] != value:
        raise ValueError("existing translation conflicts; retained for scoped review, not overwritten")
    record = dict(id=term_id, canonical=item.term, source_language=source_language, kind="term")
    if dbstore._meta_get(conn, "schema_version") in {"2", "3"}:
        old = conn.execute("SELECT status,value FROM term_slots WHERE term_id=? AND language=?",
                           (term_id, item.language)).fetchone()
        if old and old[0] == "accepted":
            if old[1] != value:
                raise ValueError("accepted slot conflicts; existing value preserved")
            return dict(applied_slots=0, already_applied=1)
        evidence = term_slots.evidence_with_ids(term_id, answer["evidence"])
        decision = dict(term_id=term_id, language=item.language, value=value, status="accepted",
                        source="corpus", confidence=.8, reason="agent_semantic_grounded",
                        evidence_refs=[ev["evidence_id"] for ev in evidence],
                        scope=dict(subject_id=term_id, language=item.language,
                                   review_item_id=item.id, semantic_guarantee=False))
        decisions = [decision]
        source_slot = conn.execute("SELECT status FROM term_slots WHERE term_id=? AND language=?",
                                   (term_id, source_language)).fetchone()
        if not source_slot or source_slot[0] != "accepted":
            source_evidence = term_slots.evidence_with_ids(term_id, [dict(
                story_key=ev["story_key"], language=source_language, term=item.term,
                value=item.term, source="corpus", sentence=ev["source_sentence"],
                start=ev["source_start"], page_id=ev["source_page_id"],
                review_kind="source_occurrence", review_item_id=item.id) for ev in answer["evidence"]])
            evidence.extend(source_evidence)
            decisions.append(dict(term_id=term_id, language=source_language, value=item.term,
                                  status="accepted", source="corpus", confidence=1.,
                                  evidence_refs=[ev["evidence_id"] for ev in source_evidence]))
        result = term_slots.commit_slot_decisions_conn(
            conn, decisions, records=[record], evidence_by_id={term_id: evidence},
            expected_revision=dbstore.current_revision(conn), verifier=term_slots.corpus_verifier())
        stored = conn.execute("SELECT status,value FROM term_slots WHERE term_id=? AND language=?",
                              (term_id, item.language)).fetchone()
        if not stored or stored != ("accepted", value):
            raise ValueError("store certificate did not accept the target slot; review remains queued")
        result["source_slots"] = max(0, result["applied_slots"] - 1)
        result["applied_slots"] = 1
        return result
    # The deployed v1 store has no slots. Update its existing narrow projection
    # and append evidence in the same transaction; do not silently migrate it.
    if row and row[1]:
        if names.get(item.language) == value:
            return dict(applied_slots=0, already_applied=1)
        raise ValueError("v1 official record cannot receive a derived translation under an official flag")
    if not row:
        record.update(names={source_language: item.term}, source="corpus", trust="C", confidence=.8)
        conn.execute(dbstore._TERM_UPSERT, dbstore._term_row_from_record(record, 0))
        names = dict(record["names"])
    old = dbstore._load_evidence_items(conn, term_id)
    evidence = term_slots.evidence_with_ids(term_id, old + answer["evidence"])
    new_value = names.get(item.language) != value
    changed = new_value or evidence != old
    if changed:
        names[item.language] = value
        term_slots._store_evidence(conn, term_id, evidence)
        conn.execute("UPDATE terms SET names_json=?, evidence_count=? WHERE id=?",
                     (json.dumps(names, ensure_ascii=False), len(evidence), term_id))
        dbstore.bump_revision(conn)
    return dict(applied_slots=int(new_value), already_applied=int(not new_value), legacy_projection=True)


def _submit_packet(conn, store, item, judgment):
    answer = _validate_answer(conn, store, item, judgment)
    if answer["task"] == "translation" and not answer.get("rejected"):
        answer["commit"] = _apply_translation(conn, item, answer)
    elif answer["task"] == "occurrence":
        from sekaisync import dbstore, occurrence_store
        inserted = occurrence_store._store_relations(conn, answer["relations"])
        replacements = []
        expansion = item._context.get("expansion")
        if expansion and answer["relations"][0]["kind"] in {"lexical", "paraphrase", "reference", "omitted"}:
            replacement = answer["relations"][0]
            replacements = [dict(old_id=identity, new_id=replacement["id"],
                                 reason="Resolved the same exact source occurrence using wider immutable raw context")
                            for identity in expansion["ancestor_relation_ids"]]
        retired = occurrence_store._retire_relations(conn, replacements) if replacements else 0
        answer["items"] = [child for relation in answer["relations"]
                           if (child := _occurrence_expansion_item(conn, store, relation)) is not None]
        focuses = {(relation["source"]["id"], relation["sense"]["id"]) for relation in answer["relations"]}
        current = [relation for source_id, sense_id in sorted(focuses)
                   for relation in occurrence_store._read_relations(conn, source_id=source_id, sense_id=sense_id)]
        answer["items"].extend(_occurrence_cohesion_items(conn, store, relations=current))
        if inserted or retired:
            dbstore.bump_revision(conn)
        answer["commit"] = dict(applied_slots=0, occurrence_relations=inserted, occurrence_retirements=retired)
    return answer


def _insert_packet(conn, item):
    from sekaisync import agent_review as ar
    cursor = conn.execute("INSERT OR IGNORE INTO review_queue(item_id,term_id,language,scope_json,"
                 "candidates_json,evidence_refs_json,evidence_snapshot_json,input_revision,"
                 "evidence_revision,status) VALUES(?,?,?,?,?,?,?,0,?,'queued')",
                 (item.id, item.term, item.language, ar._queue_scope_json(item),
                  json.dumps(item.candidates, ensure_ascii=False), ar._queue_refs_json(item),
                  ar._queue_snapshot_json(item), ar._queue_evidence_revision(item)))
    return cursor.rowcount


def _receipt_path(store):
    return Path(store) / "kb" / "terms" / "agent_packet_receipts.json"


def _receipts(store, conn=None):
    from sekaisync import agent_review as ar
    receipts = dict(ar._read_json(_receipt_path(store)).get("items", {}))

    def merge_sqlite(connection):
        from sekaisync import dbstore
        if dbstore._meta_get(connection, "schema_version") not in {"2", "3"}:
            return
        rows = connection.execute(
            "SELECT d.item_id,d.action,d.value,d.payload_json,d.scope_json "
            "FROM review_decisions d JOIN review_queue q ON q.item_id=d.item_id "
            "WHERE q.status='resolved' AND d.item_id LIKE 'arp:%'").fetchall()
        for identity, decision, value, payload_json, scope_json in rows:
            payload, scope = json.loads(payload_json or "{}"), json.loads(scope_json or "{}")
            context = scope.get("review_context") or {}
            if context.get("schema") == _SCHEMA and context.get("scope_id"):
                receipts[identity] = dict(decision=decision, value=value, scope_id=context["scope_id"],
                                         rationale=payload.get("rationale", ""),
                                         created_at=payload.get("created_at", ""), storage="sqlite")
                if context.get("task") == "discovery":
                    receipts[identity]["review_context"] = context

    if conn is not None:
        merge_sqlite(conn)
    else:
        database = (Path(store) / "kb/sekaisync.db").resolve()
        if database.exists():
            connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                connection.execute("PRAGMA query_only=ON")
                merge_sqlite(connection)
            finally:
                connection.close()
    return receipts


def _protected_reviews(store):
    """Current accepted projections backed by persisted host-agent judgments."""
    from sekaisync import dbstore
    protected = {}
    with dbstore.connect(store) as conn:
        rows = conn.execute("SELECT DISTINCT t.id,t.names_json FROM terms t "
                            "JOIN term_evidence e ON e.term_id=t.id "
                            "WHERE e.extra_json LIKE '%agent_semantic%'").fetchall()
        for term_id, names_json in rows:
            names = json.loads(names_json or "{}")
            evidence = [ev for ev in dbstore._load_evidence_items(conn, term_id)
                        if ev.get("review_kind") == "agent_semantic"
                        and ev.get("review_item_id", "").startswith("arp:")
                        and names.get(ev.get("language")) == ev.get("term")]
            if evidence:
                protected[term_id] = dict(names={ev["language"]:ev["term"] for ev in evidence},
                                          evidence=evidence)
    return protected


def _retain_reviewed_records(store, records):
    """Automatic re-extraction cannot undo a previously grounded correction."""
    protected = _protected_reviews(store)
    count = 0
    for record in records:
        saved = protected.get(record.id)
        if not saved:
            continue
        for language, value in saved["names"].items():
            record.names[language] = value
            count += 1
        for evidence in saved["evidence"]:
            if evidence not in record.evidence:
                record.evidence.append(dict(evidence))
    return count


def _retain_reviewed_scrub_slots(store, result, source_language):
    from sekaisync.termindex import make_term_id, _term_language
    protected = _protected_reviews(store)
    remaining, preserved = [], []
    for decision in result.get("slot_decisions", []):
        term, language = decision.get("term"), _term_language(decision.get("language", ""))
        saved = protected.get(make_term_id(source_language, term), {}) if term else {}
        value = saved.get("names", {}).get(language)
        if not value:
            remaining.append(decision)
            continue
        preserved.append(dict(term=term, language=language, value=value))
        # Keep both the previous judgment and today's algorithmic suggestion
        # in review; neither a rescrape nor a global word rule replaces it.
        proposals = {decision.get("language", language): decision.get("value")}
        result.setdefault("pending", []).extend([
            dict(term=term, names=proposals, reason="automatic rescrape suggestion"),
            dict(term=term, names={language:value}, reason="previous grounded agent review")])
        accepted = result.get("accepted", {}).get(term)
        if accepted:
            accepted.get("names", {}).pop(decision.get("language", language), None)
            if not accepted.get("names"):
                result["accepted"].pop(term)
    result["slot_decisions"] = remaining
    result["preserved_agent_slots"] = preserved
    return len(preserved)
