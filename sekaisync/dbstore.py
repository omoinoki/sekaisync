"""SQLite-backed knowledge store (Phase A storage engine).

Replaces the full-JSON-snapshot world for the five primary domains
(registry / glossary / terms / term evidence / web pages) with one
WAL-mode database at ``store/kb/sekaisync.db``:

- readers query exactly the rows they need (index aggregations become
  GROUP BYs instead of multi-GB full parses);
- the crawler appends/updates single pages instead of rewriting an
  850 MB ``pages.json``;
- terms evidence lives in its own table so the 680 MB sentence body is
  loaded only by write-side pipelines that actually need sentences.

Legacy compatibility: ``ensure_store`` auto-imports legacy JSON fixtures
when the DB is missing or the source JSON signature changed (cheap for
test fixtures). ``legacy_archived`` marks a store whose legacy files were
moved away, so post-archive reads never re-trigger an import. ``raw/``
region master tables, ``kb/news``, ``kb/events`` and the derived
``cache/`` files stay JSON by design.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sekaisync.layout import db_path, glossary_path, registry_path, terms_path
from sekaisync.models import Entity, GlossaryTerm
from sekaisync.trust import trust_for_page

SCHEMA_VERSION = "1"

_PAGE_COLUMNS = (
    "id", "url", "title", "language", "kind", "text", "crawled_at", "hash",
    "tos_accepted", "derived", "trust", "canonical_key", "source_hash",
    "text_hash", "untranslated", "untranslated_placeholder",
    "original_text_hash", "asset_mismatch", "scenario_id_mismatch",
    "content_language_mismatch", "source_last_modified", "source_etag",
    "auxiliary", "translation_source", "source_language", "namespace",
    "event_id", "episode_no", "overlay", "source_type", "instance",
)
_PAGE_INT_COLUMNS = frozenset({
    "tos_accepted", "untranslated", "content_language_mismatch",
    "auxiliary", "event_id", "episode_no", "overlay",
})
_INDEX_RECORD_FIELDS = (
    "id", "source", "url", "title", "language", "kind", "canonical_key",
    "event_id", "episode_no", "trust", "auxiliary", "overlay", "derived",
    "untranslated", "untranslated_placeholder", "asset_mismatch",
    "content_language_mismatch", "scenario_id_mismatch",
    "translation_source", "source_language", "crawled_at",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, region TEXT NOT NULL DEFAULT '',
    regions_json TEXT NOT NULL DEFAULT '[]', names_json TEXT NOT NULL DEFAULT '{}',
    facts_json TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT '',
    version TEXT, demo INTEGER NOT NULL DEFAULT 0, trust TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE TABLE IF NOT EXISTS glossary_terms (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT '', canonical TEXT NOT NULL,
    names_json TEXT NOT NULL DEFAULT '{}', official INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '', demo INTEGER NOT NULL DEFAULT 0,
    trust TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS terms (
    id TEXT PRIMARY KEY, canonical TEXT NOT NULL, source_language TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'term', names_json TEXT NOT NULL DEFAULT '{}',
    official INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '', confidence REAL NOT NULL DEFAULT 1.0,
    trust TEXT NOT NULL DEFAULT '', tags_json TEXT NOT NULL DEFAULT '[]',
    occurrences INTEGER NOT NULL DEFAULT 0, weight REAL NOT NULL DEFAULT 0.0,
    everyday INTEGER NOT NULL DEFAULT 0, positions_json TEXT NOT NULL DEFAULT '[]',
    evidence_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_terms_canonical ON terms(canonical);
CREATE TABLE IF NOT EXISTS term_evidence (
    term_id TEXT NOT NULL, idx INTEGER NOT NULL, story_key TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '', term TEXT NOT NULL DEFAULT '',
    sentence TEXT NOT NULL DEFAULT '', extra_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (term_id, idx)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_evidence_story ON term_evidence(story_key);
CREATE TABLE IF NOT EXISTS web_pages (
    source TEXT NOT NULL, id TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
    language TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL DEFAULT '', crawled_at TEXT NOT NULL DEFAULT '',
    hash TEXT NOT NULL DEFAULT '', tos_accepted INTEGER NOT NULL DEFAULT 0,
    derived INTEGER NOT NULL DEFAULT 0, trust TEXT NOT NULL DEFAULT '',
    canonical_key TEXT NOT NULL DEFAULT '', source_hash TEXT NOT NULL DEFAULT '',
    text_hash TEXT NOT NULL DEFAULT '', untranslated INTEGER NOT NULL DEFAULT 0,
    untranslated_placeholder TEXT NOT NULL DEFAULT '',
    original_text_hash TEXT NOT NULL DEFAULT '',
    asset_mismatch TEXT NOT NULL DEFAULT '',
    scenario_id_mismatch TEXT NOT NULL DEFAULT '',
    content_language_mismatch INTEGER NOT NULL DEFAULT 0,
    source_last_modified TEXT NOT NULL DEFAULT '',
    source_etag TEXT NOT NULL DEFAULT '', auxiliary INTEGER NOT NULL DEFAULT 0,
    translation_source TEXT NOT NULL DEFAULT '',
    source_language TEXT NOT NULL DEFAULT '', namespace TEXT NOT NULL DEFAULT '',
    event_id INTEGER NOT NULL DEFAULT 0, episode_no INTEGER NOT NULL DEFAULT 0,
    overlay INTEGER NOT NULL DEFAULT 0, source_type TEXT NOT NULL DEFAULT '',
    instance TEXT NOT NULL DEFAULT '', aux_flag INTEGER NOT NULL DEFAULT 0,
    derived_flag INTEGER NOT NULL DEFAULT 0, extra_json TEXT NOT NULL DEFAULT '{}',
    seq INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, id)
) WITHOUT ROWID;
CREATE INDEX IF NOT EXISTS idx_pages_kind ON web_pages(source, kind);
CREATE INDEX IF NOT EXISTS idx_pages_lang ON web_pages(source, language);
CREATE INDEX IF NOT EXISTS idx_pages_aux ON web_pages(aux_flag);
CREATE INDEX IF NOT EXISTS idx_pages_canonical ON web_pages(canonical_key);
CREATE INDEX IF NOT EXISTS idx_pages_seq ON web_pages(source, seq);
"""

def db_file(store_root: Path) -> Path:
    return db_path(store_root)


@contextlib.contextmanager
def connect(store_root: Path):
    """Per-call closing connection in WAL mode.

    NB: sqlite3.Connection's own context manager only commits/rolls back —
    it never closes. This wrapper guarantees close so Windows can remove
    store directories (tests use TemporaryDirectory aggressively).
    Aggregate SQL runs in milliseconds; the per-call open cost is negligible.
    """
    path = db_path(store_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def initialize(store_root: Path) -> None:
    with connect(store_root) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()


def initialized(store_root: Path) -> bool:
    path = db_path(store_root)
    if not path.exists():
        return False
    try:
        with connect(store_root) as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
    except sqlite3.DatabaseError:
        return False
    return bool(row) and row[0] == SCHEMA_VERSION


def _ensure_initialized(store_root: Path) -> None:
    """Tables only; write APIs must never trigger a legacy import (recursion)."""
    if not initialized(store_root):
        initialize(store_root)


def ensure_store(store_root: Path) -> None:
    """Guarantee an initialized DB; auto-import pending legacy JSON domains.

    Per-domain import markers let hand-written legacy fixtures (tests) and
    real legacy stores migrate lazily, while archived stores (files moved
    away, domain already imported) never re-trigger.
    """
    if not initialized(store_root):
        initialize(store_root)
    pending = pending_legacy_domains(store_root)
    if pending:
        import_legacy_domains(store_root, pending)


# ── legacy JSON import ────────────────────────────────────────────

def legacy_domain_files(store_root: Path) -> dict[str, list[Path]]:
    """Legacy JSON files per domain, searching kb/ then legacy/kb/."""
    candidates = [store_root / "kb", store_root / "legacy" / "kb"]
    domains = {
        "registry": "registry.json",
        "glossary": "glossary.json",
        "terms": "terms.json",
    }
    out: dict[str, list[Path]] = {}
    for name, filename in domains.items():
        for base in candidates:
            path = base / filename
            if path.exists():
                out[name] = [path]
                break
    for base in candidates:
        web_dir = base / "web"
        if web_dir.exists():
            pages = sorted(web_dir.glob("*/pages.json"))
            if pages:
                out["pages"] = pages
                break
    return out


def _page_dict_to_row(source: str, item: dict[str, Any]) -> tuple:
    # Lazy webindex import: webindex imports this module, so the predicates
    # must resolve at call time to keep the import graph acyclic.
    from sekaisync.webindex import (
        canonical_key_for_page,
        is_auxiliary_page,
        is_derived_page,
        normalize_mismatch_flags,
    )

    item = dict(item)
    normalize_mismatch_flags(item)
    if not item.get("canonical_key"):
        item["canonical_key"] = canonical_key_for_page(item)
    if not item.get("trust"):
        item["trust"] = trust_for_page(item)
    aux_flag = 1 if is_auxiliary_page(item) else 0
    derived_flag = 1 if is_derived_page(item) else 0
    row: dict[str, Any] = {name: None for name in (*_PAGE_COLUMNS, 'seq')}
    extra: dict[str, Any] = {}
    for key, value in item.items():
        if key == "source":
            continue
        if key in row:
            if key in _PAGE_INT_COLUMNS:
                row[key] = int(bool(value)) if key != "event_id" and key != "episode_no" else int(value or 0)
            else:
                row[key] = value if value is not None else ""
        else:
            extra[key] = value
    for key in _PAGE_COLUMNS:
        if row[key] is None:
            row[key] = "" if key not in _PAGE_INT_COLUMNS else 0
    row["aux_flag"] = aux_flag
    row["derived_flag"] = derived_flag
    row["extra_json"] = json.dumps(extra, ensure_ascii=False)
    row["source"] = source
    cols = ["source", *_PAGE_COLUMNS, "aux_flag", "derived_flag", "extra_json", "seq"]
    return tuple(row[c] for c in cols)


_PAGE_INSERT_COLUMNS = ["source", *_PAGE_COLUMNS, "aux_flag", "derived_flag", "extra_json", "seq"]
_PAGE_INSERT = (
    f"INSERT OR REPLACE INTO web_pages({', '.join(_PAGE_INSERT_COLUMNS)}) "
    f"VALUES({', '.join(['?'] * len(_PAGE_INSERT_COLUMNS))})"
)


def upsert_web_pages(store_root: Path, source: str, page_dicts: Iterable[dict[str, Any]]) -> int:
    with connect(store_root) as conn:
        _ensure_initialized(store_root)
        rows = [_page_dict_to_row(source, item) for item in page_dicts]
        conn.executemany(_PAGE_INSERT, rows)
        conn.commit()
        return len(rows)


def _web_page_row_to_dict(row: sqlite3.Row, include_text: bool = True) -> dict[str, Any]:
    item: dict[str, Any] = {"source": row["source"]}
    for name in _PAGE_COLUMNS:
        if name == "text" and not include_text:
            continue
        value = row[name]
        if name in _PAGE_INT_COLUMNS:
            item[name] = bool(value) if name not in {"event_id", "episode_no"} else int(value or 0)
        else:
            item[name] = value
    item["text_hash"] = row["text_hash"]
    extra = json.loads(row["extra_json"] or "{}")
    item.update(extra)
    if not include_text:
        item.pop("text", None)
    return item


def load_web_pages(store_root: Path) -> dict[str, list[dict[str, Any]]]:
    """Full per-source page dict mapping (compatibility full load)."""
    ensure_store(store_root)
    with connect(store_root) as conn:
        out: dict[str, list[dict[str, Any]]] = {}
        cursor = conn.execute(f"SELECT source, {', '.join(_PAGE_COLUMNS)}, aux_flag, derived_flag, extra_json, seq FROM web_pages ORDER BY source, seq")
        columns = [d[0] for d in cursor.description]
        for row in cursor:
            wrapped = dict(zip(columns, row))
            out.setdefault(wrapped["source"], []).append(_row_tuple_to_dict(columns, wrapped))
        return out


def _row_tuple_to_dict(columns: list[str], wrapped: dict[str, Any]) -> dict[str, Any]:
    class _Row:  # minimal sqlite3.Row-like view
        def __getitem__(self, name: str) -> Any:
            return wrapped[name]
    return _web_page_row_to_dict(_Row())  # type: ignore[arg-type]


def load_web_index_rows(store_root: Path) -> list[dict[str, Any]]:
    """Merged index records (metadata only, no text) — load_web_index shape."""
    ensure_store(store_root)
    with connect(store_root) as conn:
        out: list[dict[str, Any]] = []
        cursor = conn.execute(
            f"SELECT source, {', '.join(_PAGE_COLUMNS)}, aux_flag, derived_flag, extra_json, seq FROM web_pages ORDER BY source, seq"
        )
        columns = [d[0] for d in cursor.description]
        for row in cursor:
            wrapped = dict(zip(columns, row))
            item = _row_tuple_to_dict(columns, wrapped)
            record = {field: item.get(field) for field in _INDEX_RECORD_FIELDS if field in item}
            record["text_length"] = len(str(item.get("text") or ""))
            record["source"] = wrapped["source"]
            out.append(record)
        return out


def web_category_counts(store_root: Path, source: Optional[str] = None) -> dict[str, dict[str, int]]:
    """Per-source category counts. Category rules are Python-side; counting via SQL."""
    from sekaisync.webindex import page_category  # lazy: see _page_dict_to_row

    ensure_store(store_root)
    with connect(store_root) as conn:
        sql = "SELECT source, kind, language, auxiliary, overlay, derived, source_type, instance, aux_flag FROM web_pages"
        params: tuple = ()
        if source:
            sql += " WHERE source=?"
            params = (source,)
        counts: dict[str, dict[str, int]] = {}
        for row in conn.execute(sql, params):
            (src, kind, language, auxiliary, overlay, derived, source_type, instance, aux_flag) = row
            category = "other" if aux_flag else page_category(
                {
                    "kind": kind, "language": language, "auxiliary": bool(auxiliary),
                    "overlay": bool(overlay), "derived": bool(derived),
                    "source_type": source_type, "instance": instance, "source": src,
                }
            )
            counts.setdefault(src, {})
            counts[src][category] = counts[src].get(category, 0) + 1
        return counts


def auxiliary_summary_rows(store_root: Path) -> list[dict[str, Any]]:
    ensure_store(store_root)
    with connect(store_root) as conn:
        rows = conn.execute(
            "SELECT source, language, translation_source, trust, kind FROM web_pages WHERE aux_flag=1"
        )
        return [
            {
                "source": r[0], "language": r[1], "translation_source": r[2],
                "trust": r[3], "kind": r[4],
            }
            for r in rows
        ]


def trust_page_counts(store_root: Path) -> list[tuple[str, bool]]:
    """(trust, is_auxiliary) per page row for trust_summary aggregation."""
    ensure_store(store_root)
    with connect(store_root) as conn:
        return [
            (r[0] or "", bool(r[1]))
            for r in conn.execute("SELECT trust, aux_flag FROM web_pages")
        ]


def matched_text_keys(store_root: Path, language: str) -> set[str]:
    """Canonical keys of usable text pages for one language (progress helper).

    Mirrors the historical Python-side filter: skip derived pages, skip
    asset/language mismatches and untranslated placeholders.
    """
    ensure_store(store_root)
    with connect(store_root) as conn:
        keys: set[str] = set()
        for (canonical_key,) in conn.execute(
            "SELECT canonical_key FROM web_pages "
            "WHERE canonical_key != '' AND language = ? AND derived_flag = 0 "
            "AND asset_mismatch = '' AND content_language_mismatch = 0 AND untranslated = 0",
            (language,),
        ):
            keys.add(canonical_key)
        return keys


def web_page_ids(store_root: Path, source: str, skip_flagged: bool = False) -> set[str]:
    ensure_store(store_root)
    with connect(store_root) as conn:
        sql = "SELECT id, asset_mismatch, untranslated, content_language_mismatch FROM web_pages WHERE source=?"
        ids: set[str] = set()
        for pid, mismatch, untranslated, lang_mismatch in conn.execute(sql, (source,)):
            if skip_flagged and (mismatch or untranslated or lang_mismatch):
                continue
            ids.add(str(pid))
        return ids


def existing_page_map(store_root: Path, source: str) -> dict[str, dict[str, Any]]:
    ensure_store(store_root)
    with connect(store_root) as conn:
        out: dict[str, dict[str, Any]] = {}
        cursor = conn.execute(
            f"SELECT source, {', '.join(_PAGE_COLUMNS)}, aux_flag, derived_flag, extra_json, seq FROM web_pages WHERE source=? ORDER BY seq",
            (source,),
        )
        columns = [d[0] for d in cursor.description]
        for row in cursor:
            wrapped = dict(zip(columns, row))
            item = _row_tuple_to_dict(columns, wrapped)
            out[str(item.get("id", ""))] = item
        return out


def save_web_pages_full(
    store_root: Path, source: str, merged: list[dict[str, Any]]
) -> None:
    """Replace every row of one source with ``merged`` (crawler save path)."""
    _ensure_initialized(store_root)
    with connect(store_root) as conn:
        conn.execute("DELETE FROM web_pages WHERE source=?", (source,))
        rows = []
        for seq, item in enumerate(merged, start=1):
            row = list(_page_dict_to_row(source, item))
            row[-1] = seq
            rows.append(tuple(row))
        conn.executemany(_PAGE_INSERT, rows)
        conn.commit()


def delete_source_pages(store_root: Path, source: str) -> None:
    _ensure_initialized(store_root)
    with connect(store_root) as conn:
        conn.execute("DELETE FROM web_pages WHERE source=?", (source,))
        conn.commit()


# ── entities / glossary ───────────────────────────────────────────

def save_entities(store_root: Path, entities: Iterable[Entity]) -> int:
    _ensure_initialized(store_root)
    with connect(store_root) as conn:
        rows = [
            (
                e.id, e.type, e.region or "", json.dumps(e.regions, ensure_ascii=False),
                json.dumps(e.names, ensure_ascii=False), json.dumps(e.facts, ensure_ascii=False),
                e.source or "", e.version, 1 if e.demo else 0, e.trust or "",
            )
            for e in entities
        ]
        conn.execute("DELETE FROM entities")
        conn.executemany(
            "INSERT OR REPLACE INTO entities VALUES(?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        return len(rows)


def load_entities(store_root: Path) -> list[Entity]:
    ensure_store(store_root)
    with connect(store_root) as conn:
        out = []
        for row in conn.execute("SELECT * FROM entities ORDER BY id"):
            out.append(
                Entity(
                    id=row[0], type=row[1], region=row[2],
                    regions=json.loads(row[3]), names=json.loads(row[4]),
                    facts=json.loads(row[5]), source=row[6], version=row[7],
                    demo=bool(row[8]), trust=row[9],
                )
            )
        return out


def load_entity_keys(store_root: Path) -> list[tuple[str, str, list[str], str]]:
    """Lightweight (id, type, regions, region) tuples for progress matching."""
    ensure_store(store_root)
    with connect(store_root) as conn:
        return [
            (r[0], r[1], json.loads(r[2]), r[3])
            for r in conn.execute("SELECT id, type, regions_json, region FROM entities")
        ]


def save_glossary_terms(store_root: Path, terms: Iterable[GlossaryTerm]) -> int:
    _ensure_initialized(store_root)
    with connect(store_root) as conn:
        rows = [
            (
                t.id, t.kind, t.canonical, json.dumps(t.names, ensure_ascii=False),
                1 if t.official else 0, t.source or "", 1 if t.demo else 0, t.trust or "",
            )
            for t in terms
        ]
        conn.execute("DELETE FROM glossary_terms")
        conn.executemany(
            "INSERT OR REPLACE INTO glossary_terms VALUES(?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        return len(rows)


def load_glossary_terms(store_root: Path) -> list[GlossaryTerm]:
    ensure_store(store_root)
    with connect(store_root) as conn:
        return [
            GlossaryTerm(
                id=r[0], kind=r[1], canonical=r[2], names=json.loads(r[3]),
                official=bool(r[4]), source=r[5], demo=bool(r[6]), trust=r[7],
            )
            for r in conn.execute("SELECT * FROM glossary_terms ORDER BY id")
        ]


# ── terms + evidence ──────────────────────────────────────────────

def save_terms_records(
    store_root: Path,
    records: Iterable[Any],
    replace_evidence: bool = True,
) -> int:
    from sekaisync.termindex import term_to_dict

    _ensure_initialized(store_root)
    with connect(store_root) as conn:
        # When not replacing evidence, keep the stored evidence_count (merge runs
        # pass light records whose in-memory evidence list is not authoritative).
        existing_counts: dict[str, int] = {}
        if not replace_evidence:
            existing_counts = {
                tid: count
                for tid, count in conn.execute("SELECT id, evidence_count FROM terms")
            }
        term_rows = []
        evidence_rows: list[tuple] = []
        for rec in records:
            item = term_to_dict(rec)
            evidence = item.get("evidence") or []
            term_rows.append(
                (
                    item.get("id", ""), item.get("canonical", ""),
                    item.get("source_language", ""), item.get("kind", "term"),
                    json.dumps(item.get("names") or {}, ensure_ascii=False),
                    1 if item.get("official") else 0, item.get("source", ""),
                    item.get("created_at", ""), float(item.get("confidence", 1.0)),
                    item.get("trust", ""), json.dumps(item.get("tags") or [], ensure_ascii=False),
                    int(item.get("occurrences") or 0), float(item.get("weight") or 0.0),
                    1 if item.get("everyday") else 0,
                    json.dumps(item.get("positions") or [], ensure_ascii=False),
                    len(evidence) if replace_evidence else max(len(evidence), existing_counts.get(item.get("id", ""), 0)),
                )
            )
            if replace_evidence and evidence:
                conn.execute("DELETE FROM term_evidence WHERE term_id=?", (item.get("id", ""),))
                for idx, ev in enumerate(evidence):
                    if not isinstance(ev, dict):
                        continue
                    known = {"story_key", "language", "term", "sentence"}
                    extra = {k: v for k, v in ev.items() if k not in known}
                    evidence_rows.append(
                        (
                            item.get("id", ""), idx,
                            str(ev.get("story_key") or ""), str(ev.get("language") or ""),
                            str(ev.get("term") or ""), str(ev.get("sentence") or ""),
                            json.dumps(extra, ensure_ascii=False),
                        )
                    )
        conn.executemany(
            "INSERT OR REPLACE INTO terms VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", term_rows
        )
        if evidence_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO term_evidence VALUES(?,?,?,?,?,?,?)", evidence_rows
            )
        conn.commit()
        return len(term_rows)


def load_terms_records(store_root: Path, include_sentences: bool = False) -> list[Any]:
    """TermRecords. Evidence references load light (no sentence body) unless
    ``include_sentences`` — write-side pipelines pass True."""
    from sekaisync.termindex import term_from_dict

    ensure_store(store_root)
    with connect(store_root) as conn:
        evidence: dict[str, list[dict]] = {}
        sentence_col = "sentence" if include_sentences else "'' AS sentence"
        for row in conn.execute(
            f"SELECT term_id, story_key, language, term, {sentence_col}, extra_json FROM term_evidence ORDER BY term_id, idx"
        ):
            entry = {
                "story_key": row[1], "language": row[2],
            }
            if row[3]:
                entry["term"] = row[3]
            if row[4]:
                entry["sentence"] = row[4]
            extra = json.loads(row[5] or "{}")
            entry.update(extra)
            evidence.setdefault(row[0], []).append(entry)
        out = []
        for row in conn.execute("SELECT * FROM terms ORDER BY id"):
            item = {
                "id": row[0], "canonical": row[1], "source_language": row[2],
                "kind": row[3], "names": json.loads(row[4]),
                "official": bool(row[5]), "source": row[6], "created_at": row[7],
                "confidence": row[8], "trust": row[9], "tags": json.loads(row[10]),
                "occurrences": row[11], "weight": row[12], "everyday": bool(row[13]),
                "positions": json.loads(row[14]), "evidence": evidence.get(row[0], []),
            }
            out.append(term_from_dict(item))
        return out


def term_status_from_db(store_root: Path) -> dict[str, Any]:
    """term_status() shape via SQL; delegates top_by_tag to the Python impl."""
    from collections import Counter

    from sekaisync.normalize import normalize_name
    from sekaisync.termindex import TAG_VOCAB

    ensure_store(store_root)
    with connect(store_root) as conn:
        total, official, with_evidence = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(official), 0), COALESCE(SUM(evidence_count > 0), 0) FROM terms"
        ).fetchone()
        language_counts: dict[str, set[str]] = {}
        tag_counts: Counter = Counter()
        by_tag: dict[str, list[tuple[float, str, int]]] = {}
        for names_json, tags_json, weight, canonical, occurrences in conn.execute(
            "SELECT names_json, tags_json, weight, canonical, occurrences FROM terms"
        ):
            names = json.loads(names_json)
            tags = json.loads(tags_json)
            for language, name in names.items():
                if name:
                    language_counts.setdefault(language, set()).add(normalize_name(name))
            for tag in tags or ["other"]:
                tag_counts[tag] += 1
                by_tag.setdefault(tag, []).append((float(weight or 0.0), canonical, int(occurrences or 0)))
        top_by_tag = {
            tag: [
                {"canonical": c, "weight": round(w, 4), "occurrences": o}
                for (w, c, o) in sorted(items, key=lambda x: x[0], reverse=True)[:5]
            ]
            for tag, items in by_tag.items()
            if tag in set(TAG_VOCAB)
        }
        return {
            "terms": total,
            "official": official,
            "with_evidence": with_evidence,
            "languages": {language: len(names) for language, names in language_counts.items()},
            "tags": dict(tag_counts),
            "top_by_tag": top_by_tag,
        }


def count_rows(store_root: Path) -> dict[str, int]:
    with connect(store_root) as conn:
        out = {}
        for table in ("entities", "glossary_terms", "terms", "term_evidence", "web_pages"):
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        return out


# ── legacy import (per-domain, signature-aware) ──────────────────

_LEGACY_DOMAIN_FILES = {
    "registry": "registry.json",
    "glossary": "glossary.json",
    "terms": "terms.json",
}


def legacy_domain_files(store_root: Path) -> dict[str, list[Path]]:
    """Legacy JSON files per domain, searching kb/ then legacy/kb/."""
    candidates = [store_root / "kb", store_root / "legacy" / "kb"]
    out: dict[str, list[Path]] = {}
    for name, filename in _LEGACY_DOMAIN_FILES.items():
        for base in candidates:
            path = base / filename
            if path.exists():
                out[name] = [path]
                break
    for base in candidates:
        web_dir = base / "web"
        if web_dir.exists():
            pages = sorted(web_dir.glob("*/pages.json"))
            if pages:
                out["pages"] = pages
                break
    return out


def _meta_get(conn, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _meta_set(conn, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))


def pending_legacy_domains(store_root: Path) -> list[str]:
    """Domains whose legacy files exist but were never imported into the DB."""
    sources = legacy_domain_files(store_root)
    pending = []
    with connect(store_root) as conn:
        for domain in sources:
            if _meta_get(conn, f"imported_{domain}") is None:
                pending.append(domain)
    return pending


def import_legacy_domains(store_root: Path, domains: list[str]) -> dict[str, int]:
    """Import the given legacy domains (registry/glossary/terms/pages)."""
    from sekaisync.glossary import load_glossary
    from sekaisync.registry import load_registry
    from sekaisync.termindex import load_terms

    initialize(store_root)
    sources = legacy_domain_files(store_root)
    counts: dict[str, int] = {}
    with connect(store_root) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('legacy_imported_at', ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
    if "registry" in domains and "registry" in sources:
        save_entities(store_root, load_registry(sources["registry"][0]))
        counts["entities"] = count_rows(store_root)["entities"]
    if "glossary" in domains and "glossary" in sources:
        save_glossary_terms(store_root, load_glossary(sources["glossary"][0]))
        counts["glossary_terms"] = count_rows(store_root)["glossary_terms"]
    if "terms" in domains and "terms" in sources:
        records = load_terms(sources["terms"][0])
        save_terms_records(store_root, records, replace_evidence=True)
        counts["terms"] = count_rows(store_root)["terms"]
        counts["term_evidence"] = count_rows(store_root)["term_evidence"]
    if "pages" in domains and "pages" in sources:
        for pages_path in sources["pages"]:
            source_name = pages_path.parent.name
            items = json.loads(pages_path.read_text(encoding="utf-8"))
            save_web_pages_full(store_root, source_name, items)
        counts["web_pages"] = count_rows(store_root)["web_pages"]
    with connect(store_root) as conn:
        for domain in domains:
            _meta_set(conn, f"imported_{domain}", "1")
        conn.commit()
    return counts


def import_legacy(store_root: Path) -> dict[str, int]:
    """Import every pending legacy domain into the DB."""
    return import_legacy_domains(store_root, pending_legacy_domains(store_root))


def reimport_pages(store_root: Path) -> dict[str, int]:
    """Force pages from legacy JSON into the DB (migration tools)."""
    return import_legacy_domains(store_root, ["pages"])


def web_source_counts(store_root: Path) -> dict[str, int]:
    ensure_store(store_root)
    with connect(store_root) as conn:
        return {
            source: count
            for source, count in conn.execute(
                "SELECT source, COUNT(*) FROM web_pages GROUP BY source ORDER BY source"
            )
        }
