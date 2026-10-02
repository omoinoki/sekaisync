"""Private occurrence/sense ledger; no implicit projection into public names.

Offsets are half-open Unicode code-point offsets into the untouched page text.
Retrieval normalization is deliberately absent from anchor and sense identity.
The caller owns the SQLite transaction, including creation of extension tables.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path


_SCHEMA = "sekaisync/occurrence-ledger@1"
_RELATIONS = frozenset({"lexical", "paraphrase", "reference", "omitted", "unresolved"})
_DDL = (
    "CREATE TABLE IF NOT EXISTS scraper_occurrence_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS scraper_anchors (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS scraper_senses (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS scraper_subjects (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_subject_canonical ON scraper_subjects(json_extract(payload_json,'$.canonical'))",
    "CREATE TABLE IF NOT EXISTS scraper_relations (id TEXT PRIMARY KEY, source_id TEXT NOT NULL, "
    "target_id TEXT NOT NULL, sense_id TEXT NOT NULL, target_language TEXT NOT NULL, "
    "story_key TEXT NOT NULL, kind TEXT NOT NULL, review_item_id TEXT NOT NULL, payload_json TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_relation_source ON scraper_relations(source_id, target_language)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_relation_sense ON scraper_relations(sense_id, target_language)",
    "CREATE INDEX IF NOT EXISTS idx_scraper_relation_subject ON scraper_relations(json_extract(payload_json,'$.sense.term_id'))",
    "CREATE TABLE IF NOT EXISTS scraper_relation_retirements (old_id TEXT PRIMARY KEY, new_id TEXT NOT NULL, payload_json TEXT NOT NULL)",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _identity(prefix, value):
    return prefix + hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _language(value):
    from sekaisync.termindex import _term_language
    language = _term_language(value)
    if language not in {"ja", "en", "zh_hans", "zh_tw", "ko"}:
        raise ValueError("unsupported occurrence language")
    return language


def _anchor(view, story_key, segments):
    """Capture one expression, including discontiguous fragments in one page."""
    if not isinstance(story_key, str) or not story_key.strip():
        raise ValueError("occurrence requires a content identity")
    text = view.get("text")
    start, end = view.get("start"), view.get("end")
    if (not isinstance(text, str) or type(start) is not int or type(end) is not int
            or start < 0 or end - start != len(text)):
        raise ValueError("invalid raw page window")
    if not isinstance(segments, (list, tuple)) or not segments:
        raise ValueError("occurrence requires exact source segments")
    sha256 = view.get("sha256")
    if (not isinstance(sha256, str) or len(sha256) != 64
            or any(c not in "0123456789abcdef" for c in sha256)):
        raise ValueError("occurrence requires a page content hash")
    captured, previous = [], start
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError("invalid occurrence segment")
        a, b, exact = segment.get("start"), segment.get("end"), segment.get("exact")
        if (type(a) is not int or type(b) is not int or a < previous or b <= a
                or b > end or not isinstance(exact, str) or text[a - start:b - start] != exact):
            raise ValueError("occurrence segment is overlapping, out of range or not exact")
        captured.append(dict(start=a, end=b, exact=exact))
        previous = b
    payload = dict(schema=_SCHEMA, page_source=view.get("source"), page_id=view.get("page_id"),
                   language=_language(view.get("language")), page_sha256=sha256,
                   story_key=story_key, segments=captured)
    if not all(isinstance(payload[key], str) and payload[key] for key in ("page_source", "page_id")):
        raise ValueError("occurrence requires a local page identity")
    return dict(payload, id=_identity("occ:", payload))


def _sense(term_id, source_language, key, gloss):
    """Keys denote explicit contextual judgments, never normalized surfaces."""
    if any(not isinstance(value, str) or not value.strip() for value in (term_id, key, gloss)):
        raise ValueError("sense requires subject, scoped key and contextual gloss")
    payload = dict(schema=_SCHEMA, term_id=term_id, source_language=_language(source_language),
                   key=key, gloss=gloss)
    return dict(payload, id=_identity("sense:", payload))


def _relation(source, target, sense, kind, review_item_id, rationale, agent="host-agent", grounding=None):
    """Record an agent judgment, not a human gold label or global certificate."""
    if kind not in _RELATIONS:
        raise ValueError("unknown occurrence relation")
    if source["language"] == target["language"] or source["story_key"] != target["story_key"]:
        raise ValueError("relation requires two versions of the same content")
    if sense["source_language"] != source["language"]:
        raise ValueError("sense source language does not match occurrence")
    if any(not isinstance(value, str) or not value.strip() for value in (review_item_id, rationale, agent)):
        raise ValueError("relation requires reviewer identity and contextual rationale")
    payload = dict(schema=_SCHEMA, source=source, target=target, sense=sense, kind=kind,
                   target_language=target["language"], story_key=source["story_key"],
                   target_role="context" if kind in {"omitted", "unresolved"} else "expression",
                   review_item_id=review_item_id, rationale=rationale, agent=agent,
                   review_kind="machine_review", structural_grounding=grounding is not None,
                   grounding=grounding, semantic_guarantee=False,
                   publication="occurrence_only")
    return dict(payload, id=_identity("rel:", payload))


def _verify_identity(record, prefix):
    if not isinstance(record, dict) or record.get("schema") != _SCHEMA:
        raise ValueError("unsupported occurrence record")
    payload = {key: value for key, value in record.items() if key != "id"}
    if record.get("id") != _identity(prefix, payload):
        raise ValueError("occurrence record identity changed")


def _validate_anchor(conn, anchor, pages):
    _verify_identity(anchor, "occ:")
    key = (anchor["page_source"], anchor["page_id"])
    if key not in pages:
        row = conn.execute("SELECT text,language,trust,aux_flag,untranslated,content_language_mismatch,kind,url "
                           "FROM web_pages WHERE source=? AND id=?", key).fetchone()
        if row is None:
            raise ValueError("occurrence page is absent")
        pages[key] = row
    row = pages[key]
    text = str(row[0] or "")
    if row[2] == "D" or any(row[3:6]) or _language(row[1]) != anchor["language"]:
        raise ValueError("occurrence page is not a usable language version")
    from sekaisync.termindex import page_story_key
    page = dict(id=key[1], kind=row[6], url=row[7])
    if row[6] == "wordings":
        from sekaisync.wording_identity import _persisted_page
        page = _persisted_page(conn, key[0], key[1], cache=pages.setdefault("wording_pages", {}))
    if page_story_key(page) != anchor["story_key"]:
        raise ValueError("occurrence content identity does not match local page metadata")
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != anchor["page_sha256"]:
        raise ValueError("occurrence page version is stale")
    # Reconstruct the anchor so identity cannot conceal malformed offsets.
    view = dict(source=key[0], page_id=key[1], language=row[1], text=text,
                start=0, end=len(text), sha256=anchor["page_sha256"])
    if _anchor(view, anchor["story_key"], anchor["segments"]) != anchor:
        raise ValueError("occurrence anchor cannot be reproduced")


def _validate_relation(conn, relation, pages=None):
    _verify_identity(relation, "rel:")
    source, target, sense = relation["source"], relation["target"], relation["sense"]
    pages = {} if pages is None else pages
    _validate_anchor(conn, source, pages)
    _validate_anchor(conn, target, pages)
    _verify_identity(sense, "sense:")
    if _sense(sense["term_id"], sense["source_language"], sense["key"], sense["gloss"]) != sense:
        raise ValueError("invalid contextual sense")
    grounding = relation.get("grounding")
    if grounding is not None:
        _validate_grounding(conn, relation, pages)
    expected = _relation(source, target, sense, relation["kind"], relation["review_item_id"],
                         relation["rationale"], relation["agent"], grounding)
    if expected != relation:
        raise ValueError("invalid occurrence relation metadata")


def _validate_grounding(conn, relation, pages=None):
    from sekaisync import agent_packets as packets
    proof = relation["grounding"]
    context = proof.get("context") if isinstance(proof, dict) else None
    proof_keys = {"scope_id", "row_id", "term", "candidates", "context"}
    if isinstance(context, dict) and "scan" in context:
        proof_keys.add("scan_review")
    if not isinstance(proof, dict) or set(proof) != proof_keys:
        raise ValueError("invalid occurrence grounding proof")
    database = next((row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main"), "")
    if not database:
        raise ValueError("structural grounding requires a persistent corpus scope")
    scope = packets._read_scope(Path(database).parent.parent, proof["scope_id"])
    if (not isinstance(context, dict) or context.get("scope_id") != proof["scope_id"]
            or context.get("task") != "occurrence" or context.get("schema") != packets._SCHEMA
            or context.get("source_language") != scope["source_language"]):
        raise ValueError("occurrence proof is not an immutable work packet")
    pages = {} if pages is None else pages
    packets._validate_subject_fallback(conn, Path(database).parent.parent, context,
                                      relation["target_language"], proof["term"],
                                      inventory=pages.setdefault("subject_fallback_inventory", {}))
    if "scan" in context:
        from sekaisync import subject_scans
        subject_scans._validate_relation(conn, Path(database).parent.parent, relation, pages)
    item_id = "arp:" + packets._digest([proof["term"], relation["target_language"], sorted(set(proof["candidates"])), context])[:32]
    if item_id != relation["review_item_id"]:
        raise ValueError("occurrence proof has another review item identity")
    row = next((row for row in scope["windows"] if row["id"] == proof["row_id"]), None)
    if row is None:
        raise ValueError("occurrence proof row is absent from its scope")
    target = row["targets"].get(relation["target_language"])
    packets._validate_fallback_observation(context, target, relation["kind"], relation["target"]["segments"])
    expected = dict(id=row["id"], story_key=row["story_key"], source=row["source"], target=target)
    if expected not in context["rows"]:
        raise ValueError("occurrence proof is outside its work packet")
    typed_subject = context.get("subject")
    if typed_subject is not None:
        packets._validate_subject_in_rows(conn, typed_subject, [expected])
        if (typed_subject["source"] != relation["source"]
                or typed_subject["canonical"] != proof["term"]):
            raise ValueError("typed occurrence subject is outside its immutable source window")
        subject_id = typed_subject["id"]
    else:
        if not packets._term_selection(row["source"], proof["term"], relation["source"]["segments"], case_sensitive=True):
            raise ValueError("occurrence source does not select the proof's subject term")
        subject_id = _identity("lex:", [context["source_language"], proof["term"]])
    if relation["sense"]["term_id"] != subject_id:
        raise ValueError("occurrence sense does not belong to the proof's subject")
    focus = context.get("focus")
    if focus is not None and (not isinstance(focus, dict) or set(focus) != {"source", "sense"}
                              or relation["source"] != focus["source"] or relation["sense"] != focus["sense"]):
        raise ValueError("occurrence proof does not preserve its exact contextual focus")
    for name, view in (("source", row["source"]), ("target", target)):
        anchor = relation[name]
        if (view is None or not view["complete"] or anchor["story_key"] != row["story_key"]
                or anchor["page_source"] != view["source"] or anchor["page_id"] != view["page_id"]
                or anchor["page_sha256"] != view["sha256"] or anchor["language"] != _language(view["language"])
                or any(part["start"] < view["start"] or part["end"] > view["end"] for part in anchor["segments"])):
            raise ValueError("occurrence proof does not bind the selected local windows")
        packets._validate_view(conn, view, pages)
    if relation["target_role"] == "expression":
        from sekaisync.wording_identity import _view_policy
        positions, offset = set(), target["start"]
        for line in target["text"].splitlines(keepends=True):
            body = line if _view_policy(target) else packets.strip_speaker_label(line)
            positions.update(range(offset + len(line) - len(body), offset + len(line)))
            offset += len(line)
        if any(index not in positions for part in relation["target"]["segments"]
               for index in range(part["start"], part["end"])):
            raise ValueError("occurrence expression cannot borrow speaker metadata")
    if relation["kind"] == "lexical":
        from sekaisync import span_subjects
        anchor = relation["target"]
        _validate_anchor(conn, anchor, pages)
        row = pages[(anchor["page_source"], anchor["page_id"])]
        raw = str(row[0] or "")
        full = dict(text=raw, start=0, end=len(raw))
        if row[6] == "wordings":
            from sekaisync.wording_identity import _full_view, _persisted_page
            page = _persisted_page(conn, anchor["page_source"], anchor["page_id"],
                                   cache=pages.setdefault("wording_pages", {}))
            full = _full_view(page)
        span_subjects._utterance(full, anchor["segments"])


def _ensure_schema(conn):
    for statement in _DDL:
        conn.execute(statement)
    row = conn.execute("SELECT value FROM scraper_occurrence_meta WHERE key='schema'").fetchone()
    if row and row[0] != _SCHEMA:
        raise ValueError("unsupported occurrence ledger version")
    conn.execute("INSERT OR IGNORE INTO scraper_occurrence_meta VALUES('schema',?)", (_SCHEMA,))


def _store_relations(conn, relations):
    """Validate the entire batch before writing; savepoints are caller-owned."""
    pages = {}
    for relation in relations:
        _validate_relation(conn, relation, pages)
    if not relations:
        return 0
    _ensure_schema(conn)
    inserted = 0
    for relation in relations:
        subject = (relation.get("grounding") or {}).get("context", {}).get("subject")
        if subject is not None:
            conn.execute("INSERT OR IGNORE INTO scraper_subjects(id,payload_json) VALUES(?,?)",
                         (subject["id"], _json(subject)))
        for table, record in (("scraper_anchors", relation["source"]),
                              ("scraper_anchors", relation["target"]),
                              ("scraper_senses", relation["sense"])):
            conn.execute(f"INSERT OR IGNORE INTO {table}(id,payload_json) VALUES(?,?)",
                         (record["id"], _json(record)))
        inserted += conn.execute("INSERT OR IGNORE INTO scraper_relations VALUES(?,?,?,?,?,?,?,?,?)",
                                 (relation["id"], relation["source"]["id"], relation["target"]["id"],
                                  relation["sense"]["id"], relation["target_language"], relation["story_key"],
                                  relation["kind"], relation["review_item_id"], _json(relation))).rowcount
    return inserted


def _read_relations(conn, source_id=None, sense_id=None, target_language=None, current_only=True,
                    subject_ids=None, story_key=None, source_language=None):
    """Read without creating tables or mutating stale historical judgments."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scraper_relations'").fetchone():
        return []
    clauses, values = [], []
    for column, value in (("source_id", source_id), ("sense_id", sense_id), ("target_language", target_language),
                          ("story_key", story_key)):
        if value is not None:
            clauses.append(column + "=?")
            values.append(value)
    if subject_ids is not None:
        subject_ids = tuple(subject_ids)
        if not subject_ids:
            return []
        clauses.append("json_extract(payload_json,'$.sense.term_id') IN (" + ",".join("?" for _ in subject_ids) + ")")
        values.extend(subject_ids)
    if source_language is not None:
        clauses.append("json_extract(payload_json,'$.source.language')=?")
        values.append(_language(source_language))
    if current_only and conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_relation_retirements'").fetchone():
        clauses.append("id NOT IN (SELECT old_id FROM scraper_relation_retirements)")
    query = "SELECT payload_json FROM scraper_relations"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    pages, result = {}, []
    for row in conn.execute(query + " ORDER BY id", values):
        relation = json.loads(row[0])
        if current_only:
            try:
                _validate_relation(conn, relation, pages)
            except (ValueError, OSError):
                continue
        result.append(relation)
    return result


def _query_subject_ids(conn, query):
    """Keep typed display lookup exact without turning it into lexical identity."""
    subjects = [_identity("lex:", [language, query.strip()])
                for language in ("ja", "en", "zh_hans", "zh_tw", "ko")]
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_subjects' AND type='table'").fetchone():
        subjects.extend(row[0] for row in conn.execute(
            "SELECT id FROM scraper_subjects WHERE json_extract(payload_json,'$.canonical') IN (?,?) ORDER BY id",
            (query, query.strip())))
    return subjects


def _relations_for_query(conn, query, story_key=None, source_language=None):
    """Exact, case-preserving subjects only; never invert or transitively join."""
    if not isinstance(query, str) or not query.strip():
        return []
    subjects = _query_subject_ids(conn, query)
    return [row for row in _read_relations(conn, subject_ids=subjects, story_key=story_key,
                                         source_language=source_language)
            if row["structural_grounding"]]


def _query_relations(conn, query, story_key=None, source_language=None):
    """Remember unavailable evidence so stale scope cannot revive legacy names."""
    if not isinstance(query, str) or not query.strip():
        return False, []
    subjects = _query_subject_ids(conn, query)
    known = _read_relations(conn, subject_ids=subjects, story_key=story_key, current_only=False,
                            source_language=source_language)
    return any(row.get("structural_grounding") for row in known), _relations_for_query(
        conn, query, story_key, source_language)


def _query_pages(conn, relations):
    """Read only anchored page bodies, not the entire production text corpus."""
    from sekaisync import dbstore
    keys = {(anchor["page_source"], anchor["page_id"])
            for relation in relations for anchor in (relation["source"], relation["target"])}
    cursor = conn.cursor()
    cursor.row_factory = sqlite3.Row
    query = "SELECT source," + ",".join(dbstore._PAGE_COLUMNS) + ",extra_json FROM web_pages WHERE source=? AND id=?"
    try:
        return [dbstore._web_page_row_to_dict(row) for key in sorted(keys)
                if (row := cursor.execute(query, key).fetchone()) is not None]
    finally:
        cursor.close()


def _retire_relations(conn, replacements):
    """Keep old judgments auditable while resolving precisely the same anchor."""
    prepared, pages = [], {}
    for replacement in replacements:
        if (not isinstance(replacement, dict) or set(replacement) != {"old_id", "new_id", "reason"}
                or any(not isinstance(value, str) or not value.strip() for value in replacement.values())):
            raise ValueError("invalid occurrence retirement")
        records = []
        for key in ("old_id", "new_id"):
            row = conn.execute("SELECT payload_json FROM scraper_relations WHERE id=?", (replacement[key],)).fetchone()
            if row is None:
                raise ValueError("retirement relation is absent")
            relation = json.loads(row[0])
            _validate_relation(conn, relation, pages)
            records.append(relation)
        old, new = records
        if (not old["structural_grounding"] or not new["structural_grounding"]
                or old["kind"] not in {"unresolved", "omitted"}
                or new["kind"] not in {"lexical", "paraphrase", "reference", "omitted"}
                or old["source"] != new["source"] or old["sense"] != new["sense"]
                or old["target_language"] != new["target_language"]
                or any(old["target"][key] != new["target"][key]
                       for key in ("page_source", "page_id", "page_sha256", "story_key"))):
            raise ValueError("retirement cannot borrow another occurrence, sense or version")
        if new["kind"] == "omitted":
            context = new["grounding"]["context"]
            expansion = context.get("expansion", {})
            target = next((row["target"] for row in context["rows"]
                           if row["id"] == new["grounding"]["row_id"]), None)
            raw = pages[(new["target"]["page_source"], new["target"]["page_id"])][0]
            if (old["kind"] != "unresolved" or expansion.get("level") != "full_page"
                    or expansion.get("target_complete_page") is not True or target is None
                    or not target["complete"] or target["start"] != 0 or target["end"] != len(raw)):
                raise ValueError("omission retirement requires examined complete target page")
        payload = dict(schema=_SCHEMA, **replacement)
        record = dict(payload, id=_identity("ret:", payload))
        has_retirements = conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_relation_retirements'").fetchone()
        existing = (conn.execute("SELECT payload_json FROM scraper_relation_retirements WHERE old_id=?",
                                 (old["id"],)).fetchone() if has_retirements else None)
        if existing and json.loads(existing[0]) != record:
            raise ValueError("occurrence retirement already has another successor")
        prepared.append(record)
    inserted = 0
    if prepared:
        _ensure_schema(conn)
    for record in prepared:
        inserted += conn.execute("INSERT OR IGNORE INTO scraper_relation_retirements VALUES(?,?,?)",
                                 (record["old_id"], record["new_id"], _json(record))).rowcount
    return inserted


def _projection_candidate(relations):
    """Suggest a legacy scalar only for explicit same-sense lexical support.

This is not a publisher or verifier. Existing public-slot checks still apply.
"""
    if not relations or any(row["kind"] != "lexical" for row in relations):
        return None
    if len({(row["sense"]["id"], row["target_language"]) for row in relations}) != 1:
        return None
    if len({row["story_key"] for row in relations}) < 2:
        return None
    if any(len(row["target"]["segments"]) != 1 for row in relations):
        return None
    surfaces = {row["target"]["segments"][0]["exact"] for row in relations}
    return surfaces.pop() if len(surfaces) == 1 else None
