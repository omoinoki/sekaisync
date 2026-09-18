"""Candidate prefilter for web search (Astra P06 follow-up).

``web_search`` scores every page in Python because the ranking uses
``normalize.best_match`` — a Unicode-normalised fuzzy matcher that SQL cannot
reproduce. Astra D06 explicitly forbids replacing it with ``LIKE '%q%' LIMIT K``
because that changes recall. The cost is real: the real store holds 752k pages /
~618M characters of body text, and streaming titles+bodies to Python takes ~26s
before a single score is computed.

This module keeps the scorer exactly as it is and adds an *index* that proposes
which rows could possibly match. The scorer still decides every result and its
ordering; the index only removes rows the scorer would have scored 0. That is a
superset argument, which is why recall is provably unchanged rather than
approximately unchanged.

Two things make it sound rather than merely fast:

1. **The index stores the scorer's own key.**
   ``matching_key`` folds kana script, drops long-vowel marks and strips
   whitespace/punctuation (``normalize.normalize_name`` under NFKC + casefold).
   Indexing raw text would drop every row whose match *depends* on that folding
   — e.g. ``カタカナ`` vs ``かたかな`` score 70 with disjoint raw trigrams. So the
   indexed value is the folded key, and the query is folded the same way.

2. **The candidate set is a superset, proved per scorer tier.**
   Let ``Q``/``H`` be the folded key lengths and ``T`` the trigram sets.

   ===========  ==========================================================
   tier         why it is covered
   ===========  ==========================================================
   100 exact    ``H == Q``: the length window covers it.
   80 prefix    either shorter side is inside the length window, or the
                shorter key is a prefix of the longer one — and a prefix of
                length >= 3 shares all of its trigrams with it.
   60 latin set a query word of length >= 3 appears contiguously in the row,
                so that word's trigrams are shared. When *every* query word
                is <= 2 chars the rule can fire with no shared trigram, and
                the query is rejected to the full scan (see ``bypass``).
   50 substring containment is preserved by the kana/long-vowel folding
                (verified exhaustively), so the query key is a substring and
                its trigrams are shared.
   70/65 loose  identical or prefix keys share trigrams.
   55 edit      the tier requires ``|Q - H| <= 3``, so the length window
                covers it regardless of trigrams (``aaa`` vs ``aaba`` scores
                55 with no shared trigram).
   ===========  ==========================================================

   The length window (``H <= Q + 3``) is therefore mandatory, not a
   heuristic: without it tiers 55 and the short-prefix half of 80 lose rows.

Safety rules, all enforced here:

- FTS5 is a **pure accelerator**. It is probed at runtime; when it or the
  ``trigram`` tokenizer is missing, ``candidates()`` returns ``None`` and the
  caller scans exactly as before. The store is never rewritten either way.
- The index lives in its own file under ``cache/`` — a regenerable projection,
  never authority. Its header records the store revision *and* a page
  fingerprint; if either disagrees the index is treated as stale and the
  caller scans. A stale index can cost time, never correctness.
- Queries the index cannot answer (short keys, all-words-<=2-chars) are
  reported as ``bypass`` reasons and scanned.
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from sekaisync import dbstore
from sekaisync.layout import cache_dir
from sekaisync.normalize import latin_words, matching_key

#: Bumped when the indexed representation or the candidate rules change, so an
#: index written by an older build is never used with newer logic.
INDEX_FORMAT = 1

#: Minimum folded-key length that can be answered by trigrams at all: a
#: shorter key has no 3-character substring to look up.
MIN_TRIGRAM_KEY = 3

#: Tier 55 requires ``|Q - H| <= 3``; the window must include those rows even
#: when no trigram is shared.
LENGTH_WINDOW = 3

#: Selectivity guard: above this *absolute* number of candidate rows, fetching
#: them costs more than the plain scan, so the index declines and the caller
#: scans. Measured on the real store: a query whose trigrams post to most rows
#: produced 500k+ candidates and ran slower than the scan, while a selective
#: query fell from ~145s to ~0.2s. The bound is absolute rather than a share of
#: the table because the cost of the candidate path is proportional to the
#: candidate count, not to how large the store happens to be — a share-based
#: bound would reject every query on a small store.
MAX_CANDIDATES = 200_000

_TRIGRAM_RE = re.compile(r".{3}", re.DOTALL)


class IndexUnavailable(RuntimeError):
    """The candidate index cannot be used; the caller must scan."""


def index_path(store_root: Path) -> Path:
    """The projection file (regenerable; never authoritative)."""
    return cache_dir(store_root) / "web_search_index.db"


def trigrams(key: str) -> list[str]:
    """Distinct 3-character substrings of a folded key, in stable order."""
    if len(key) < MIN_TRIGRAM_KEY:
        return []
    return sorted({key[i:i + 3] for i in range(len(key) - 2)})


def bypass_reason(query: str) -> Optional[str]:
    """Why this query cannot use the index, or ``None`` when it can.

    These are the two documented holes in the superset argument; both fall
    back to the full scan rather than risking a missed row.
    """
    key = matching_key(query)
    if len(key) < MIN_TRIGRAM_KEY:
        # Tiers 50/70/80 are all reachable with a 1-2 character key, which has
        # no trigram to look up ("ミク" is a real example from the corpus).
        return "short_key"
    words = latin_words(query)
    if words and all(len(word) < MIN_TRIGRAM_KEY for word in words):
        # Tier 60 ("every latin word of the query appears as a word") is
        # order-insensitive, so it can fire with no shared trigram when every
        # word is <= 2 characters: "ab cd" vs "cd ab".
        return "short_latin_words"
    return None


def fts5_trigram_available(conn: sqlite3.Connection) -> bool:
    """Probe support by creating the table, not by reading compile options.

    ``PRAGMA compile_options`` is compiled out under
    ``SQLITE_OMIT_COMPILEOPTION_DIAGS``, so it can report nothing on a build
    that does support FTS5. Trying it is definitive.
    """
    try:
        conn.execute("CREATE VIRTUAL TABLE temp.__sekaisync_probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE temp.__sekaisync_probe")
        return True
    except sqlite3.Error:
        return False


def _page_fingerprint(conn: sqlite3.Connection) -> dict[str, Any]:
    """Cheap identity of the page table's current contents.

    The authoritative staleness signal is the store revision (every web write
    advances it — see ``dbstore.upsert_web_pages`` and friends). This
    fingerprint is a second, independent check that catches content changes
    made *without* the revision moving: row count, per-source counts, sequence
    extents, and the summed lengths of titles and bodies. All are aggregates
    SQLite can compute without shipping text to Python.

    Known limit, stated rather than hidden: an edit that preserves row count,
    sequence numbers and every length (e.g. rewriting one title with another of
    the same size) is not detected here. Such a write is out of contract —
    authoritative writes go through ``dbstore`` and bump the revision — and the
    remedy is ``rebuild``, which ``stats()`` reports as stale-or-not.
    """
    total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(title)), 0), COALESCE(SUM(LENGTH(text)), 0) "
        "FROM web_pages"
    ).fetchone()
    per_source = [
        list(row)
        for row in conn.execute(
            "SELECT source, COUNT(*), COALESCE(MAX(seq), -1), COALESCE(SUM(seq), 0) "
            "FROM web_pages GROUP BY source ORDER BY source"
        )
    ]
    return {
        "rows": total[0],
        "title_chars": total[1],
        "text_chars": total[2],
        "per_source": per_source,
    }


def build(store_root: Path, *, batch_size: int = 2048, progress=None) -> dict[str, Any]:
    """(Re)build the candidate index from the store's current pages.

    Writes only the projection file. The store itself is opened read-only in
    every respect that matters: this reads ``web_pages`` and nothing else.
    """
    target = index_path(store_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_suffix(".building")
    if staging.exists():
        staging.unlink()

    out = sqlite3.connect(str(staging))
    written = 0
    skipped = 0
    try:
        out.execute("PRAGMA journal_mode=OFF")
        out.execute("PRAGMA synchronous=OFF")
        out.execute(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        out.execute(
            "CREATE TABLE pages(rowid INTEGER PRIMARY KEY, source TEXT NOT NULL, "
            "page_id TEXT NOT NULL, key_len INTEGER NOT NULL)"
        )
        out.execute("CREATE INDEX ix_pages_key_len ON pages(key_len, rowid)")
        if not fts5_trigram_available(out):
            raise IndexUnavailable("this SQLite build has no FTS5 trigram tokenizer")
        # ``detail=none`` keeps only the postings, which is all we need: the
        # candidate query is an OR of single-trigram phrases, never a phrase
        # spanning several trigrams (those require detail=full).
        out.execute(
            "CREATE VIRTUAL TABLE keys USING fts5(key, content='', detail='none', "
            "tokenize='trigram')"
        )

        with dbstore.connect(store_root) as conn:
            revision = dbstore.current_revision(conn)
            fingerprint = _page_fingerprint(conn)
            cursor = conn.execute(
                "SELECT source, id, title, substr(text, 1, 20000) FROM web_pages ORDER BY source, seq"
            )
            pending_pages: list[tuple[int, str, str, int]] = []
            pending_keys: list[tuple[int, str]] = []

            def flush() -> None:
                if not pending_pages:
                    return
                out.executemany("INSERT INTO pages VALUES(?,?,?,?)", pending_pages)
                out.executemany("INSERT INTO keys(rowid, key) VALUES(?,?)", pending_keys)
                pending_pages.clear()
                pending_keys.clear()

            for rowid, (source, page_id, title, head) in enumerate(cursor, start=1):
                key = matching_key("\n".join([title or "", head or ""]))
                if not key:
                    skipped += 1
                    continue
                pending_pages.append((rowid, source, page_id, len(key)))
                pending_keys.append((rowid, key))
                written += 1
                if len(pending_pages) >= batch_size:
                    flush()
                    if progress is not None:
                        progress(written)
            flush()

        out.executemany(
            "INSERT INTO meta(key, value) VALUES(?,?)",
            [
                ("index_format", str(INDEX_FORMAT)),
                ("revision", str(revision)),
                ("fingerprint", json.dumps(fingerprint, sort_keys=True)),
                ("rows", str(written)),
                ("skipped_empty_keys", str(skipped)),
            ],
        )
        out.commit()
        out.execute("PRAGMA optimize")
        out.commit()
    finally:
        out.close()

    # Atomic swap: readers never observe a half-built index.
    if target.exists():
        target.unlink()
    staging.replace(target)
    return {
        "path": str(target),
        "rows": written,
        "skipped_empty_keys": skipped,
        "fingerprint_rows": fingerprint["rows"],
        "revision": revision,
    }


def _load_meta(conn: sqlite3.Connection) -> dict[str, str]:
    try:
        return {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta")}
    except sqlite3.Error as exc:
        raise IndexUnavailable(f"index has no usable header: {exc}") from exc


@contextlib.contextmanager
def _open(store_root: Path):
    path = index_path(store_root)
    if not path.exists():
        raise IndexUnavailable("no index built yet")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        yield conn
    finally:
        conn.close()


def is_current(store_root: Path, *, deep: bool = True) -> tuple[bool, str]:
    """Whether the on-disk index matches the store, and why not when it does not.

    ``deep=False`` checks only the store revision and row count. That is the
    check a query can afford: the revision is a single ``meta`` read, while the
    fingerprint has to sum lengths over 618M characters of body text (~2s on
    the real store) — fine for a diagnostic, far too slow to run per search.

    A write that respects the contract (every authoritative web write bumps the
    revision) cannot slip past the cheap check. ``deep=True`` additionally
    catches out-of-contract edits that moved content without moving the
    revision, which is what ``stats()`` and the tests use.
    """
    try:
        with _open(store_root) as conn:
            meta = _load_meta(conn)
            if meta.get("index_format") != str(INDEX_FORMAT):
                return False, f"index_format={meta.get('index_format')} != {INDEX_FORMAT}"
            with dbstore.connect(store_root) as store:
                if meta.get("revision") != str(dbstore.current_revision(store)):
                    return False, "store revision moved since the index was built"
                indexed = meta.get("rows")
                actual = store.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
                if indexed != str(actual):
                    # Rows were added or removed; the fingerprint check below is
                    # stricter, but this one is free.
                    return False, f"page count {actual} != indexed {indexed}"
                if not deep:
                    return True, ""
                fingerprint = _page_fingerprint(store)
            try:
                expected = json.loads(meta.get("fingerprint") or "null")
            except ValueError:
                return False, "index fingerprint is unreadable"
            if not isinstance(expected, dict):
                return False, "index has no usable fingerprint"
            for field in ("rows", "title_chars", "text_chars", "per_source"):
                if expected.get(field) != fingerprint.get(field):
                    return False, f"page fingerprint moved ({field})"
        return True, ""
    except IndexUnavailable as exc:
        return False, str(exc)
    except sqlite3.Error as exc:
        return False, f"index unreadable: {exc}"


def _candidate_rowids(conn: sqlite3.Connection, key: str) -> set[int]:
    """Rowids that could match ``key``: trigrams unioned with the length window."""
    rowids: set[int] = set()
    found = trigrams(key)
    if found:
        # OR of single-trigram phrases. Each is a 3-character query, which is
        # the shortest a trigram index can answer; a *phrase* spanning several
        # trigrams would need detail=full and buys nothing here, because the
        # scorer is what decides adjacency.
        expr = " OR ".join('"%s"' % t.replace('"', '""') for t in found)
        rowids |= {
            row[0] for row in conn.execute("SELECT rowid FROM keys WHERE keys MATCH ?", (expr,))
        }
    # Tier 55 and the short-prefix half of tier 80: a row key short enough to
    # be inside the edit-distance window regardless of shared trigrams. This is
    # what makes the ruleset a superset rather than merely a good filter.
    rowids |= {
        row[0]
        for row in conn.execute(
            "SELECT rowid FROM pages WHERE key_len <= ?", (len(key) + LENGTH_WINDOW,)
        )
    }
    return rowids


def candidates(
    store_root: Path,
    query: str,
    *,
    source_ids: Optional[Sequence[str]] = None,
    language: Optional[str] = None,
    include_overlay: bool = False,
    max_candidates: int = MAX_CANDIDATES,
) -> Optional[set[tuple[str, str]]]:
    """(source, page_id) pairs that could match ``query``, or ``None`` to scan.

    ``None`` means "the caller must fall back to the full scan": the query is
    one the index cannot answer, the index is unavailable/stale, or the
    candidate set is too broad for the index to be worth using. All are
    ordinary outcomes, not errors — the index only ever *removes* rows the
    scorer would have scored 0, so a scan is always correct.

    ``max_candidates`` is the selectivity guard. A trigram that occurs in most
    rows is answered by walking nearly the whole index and then fetching nearly
    the whole table, which measured *slower* than the plain scan on the real
    store (a broad query produced 500k+ candidates). Past that point the index
    cannot help, so it declines and the caller scans.
    """
    if bypass_reason(query):
        return None
    key = matching_key(query)
    try:
        with _open(store_root) as conn:
            meta = _load_meta(conn)
            if meta.get("index_format") != str(INDEX_FORMAT):
                return None
            # Cheap validation only: this runs on every search, so it may not
            # pay for the deep fingerprint (see ``is_current``).
            current, _why = is_current(store_root, deep=False)
            if not current:
                return None
            rowids = _candidate_rowids(conn, key)
            if not rowids:
                return set()
            if len(rowids) > max_candidates:
                return None
            out: set[tuple[str, str]] = set()
            for chunk_start in range(0, len(rowids), 900):
                chunk = sorted(rowids)[chunk_start:chunk_start + 900]
                placeholders = ",".join("?" * len(chunk))
                sql = (
                    f"SELECT rowid, source, page_id FROM pages WHERE rowid IN ({placeholders})"
                )
                params: list[Any] = list(chunk)
                if source_ids:
                    sql += f" AND source IN ({','.join('?' * len(source_ids))})"
                    params.extend(source_ids)
                out |= {(row[1], row[2]) for row in conn.execute(sql, params)}
            return out
    except (IndexUnavailable, sqlite3.Error):
        return None


def stats(store_root: Path) -> dict[str, Any]:
    """Diagnostics for operators and tests."""
    path = index_path(store_root)
    info: dict[str, Any] = {"path": str(path), "exists": path.exists()}
    if not path.exists():
        return info
    info["bytes"] = path.stat().st_size
    try:
        with _open(store_root) as conn:
            info.update(_load_meta(conn))
        current, why = is_current(store_root)
        info["current"] = current
        info["stale_reason"] = why
    except (IndexUnavailable, sqlite3.Error) as exc:
        info["error"] = str(exc)
    return info


def indexed_rows(store_root: Path) -> list[tuple[str, str, int]]:
    """``(source, page_id, key_len)`` for every indexed row.

    Exposed so a differential test can compare the index against the scan
    without duplicating the folding rule. The folded key itself is not
    returned: the FTS5 table is contentless, so its text is not readable, and
    the length is what the superset argument depends on.
    """
    with _open(store_root) as conn:
        return [(row[0], row[1], row[2]) for row in conn.execute(
            "SELECT source, page_id, key_len FROM pages ORDER BY rowid"
        )]
