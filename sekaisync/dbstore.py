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

Schema gate (P07): only explicitly supported schema stamps are accepted.
Reads never rewrite a stamp. Migration is an explicit, backed-up admin
operation: v1→v2 adds term slots and v2→v3 adds per-region entity facts.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Optional, Sequence

from sekaisync.layout import (
    ACTIVE_GENERATION_KEY,
    db_path,
    glossary_path,
    registry_path,
    terms_path,
)
from sekaisync.models import Entity, GlossaryTerm, RegionFacts
from sekaisync.trust import trust_for_page

# Compatibility initializer remains v1 until all old writers are adapted.
SCHEMA_VERSION = "1"
LATEST_SCHEMA_VERSION = "3"
SUPPORTED_SCHEMA_VERSIONS = frozenset({"1", "2", "3"})
#: P03/P07: schema 3 adds ``entity_region_facts`` on top of the unchanged v2
#: tables.  It is created only through the explicit entry points
#: (``initialize_new_store(target_version=3)`` / :func:`migrate_store`);
#: the compatibility initializers stay at v1/v2.
REGION_SCHEMA_VERSION = "3"

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
    "source_type", "instance",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS entities (
    id TEXT PRIMARY KEY, type TEXT NOT NULL, region TEXT NOT NULL DEFAULT '',
    regions_json TEXT NOT NULL DEFAULT '[]', names_json TEXT NOT NULL DEFAULT '{}',
    facts_json TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT '',
    version TEXT, demo INTEGER NOT NULL DEFAULT 0, trust TEXT NOT NULL DEFAULT '',
    seq INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
CREATE TABLE IF NOT EXISTS glossary_terms (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL DEFAULT '', canonical TEXT NOT NULL,
    names_json TEXT NOT NULL DEFAULT '{}', official INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '', demo INTEGER NOT NULL DEFAULT 0,
    trust TEXT NOT NULL DEFAULT '', seq INTEGER NOT NULL DEFAULT 0
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

_REGION_SCHEMA = (
    """CREATE TABLE entity_region_facts (
        entity_id TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
        region TEXT NOT NULL CHECK(region != ''),
        facts_json TEXT NOT NULL DEFAULT '{}',
        source TEXT NOT NULL DEFAULT '', version TEXT,
        retrieval_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (entity_id, region)
    ) WITHOUT ROWID""",
    "CREATE INDEX idx_region_facts_region ON entity_region_facts(region)",
)


def _create_region_schema(conn: sqlite3.Connection) -> None:
    for statement in _REGION_SCHEMA:
        conn.execute(statement)

def db_file(store_root: Path) -> Path:
    return db_path(store_root)


@contextlib.contextmanager
def connect(store_root: Path):
    """Per-call closing connection in WAL mode.

    NB: sqlite3.Connection's own context manager only commits/rolls back —
    it never closes. This wrapper guarantees close so Windows can remove
    store directories (tests use TemporaryDirectory aggressively).
    Aggregate SQL runs in milliseconds; the per-call open cost is negligible.

    When a request-level connection is bound via :func:`read_connection`, this
    yields that connection instead — without committing or closing it (the
    request owns its transaction and lifecycle, P02).
    """
    bound = _bound_connection(store_root)
    if bound is not None:
        yield bound
        return
    store_root = Path(store_root)
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


_REQUEST_CONNECTION: ContextVar[Optional[tuple[Path, sqlite3.Connection]]] = ContextVar(
    "sekaisync_request_connection", default=None
)


@contextlib.contextmanager
def read_connection(store_root: Path, conn: sqlite3.Connection):
    """Bind a request-scoped connection for nested ``connect``/``ensure_store``.

    P02 request consistency: within the ``with`` block every ``connect()``
    yields *this* connection (no commit, no close — the request owns its
    lifecycle and transaction), so every SQL read helper shares one snapshot
    instead of each opening its own connection.  Default (unbound) behaviour of
    every other entry point is byte-for-byte unchanged, on v1 and v2 alike.
    Migration and term writers reject a binding for the same store; they must
    never commit, roll back, or change the caller's read transaction.
    """
    previous = _REQUEST_CONNECTION.get()
    if previous is not None:
        raise RuntimeError(
            "read_connection is already bound for this context; "
            "requests must not nest snapshots"
        )
    token = _REQUEST_CONNECTION.set((Path(store_root).resolve(), conn))
    try:
        yield conn
    finally:
        _REQUEST_CONNECTION.reset(token)


def _bound_connection(store_root: Path) -> Optional[sqlite3.Connection]:
    binding = _REQUEST_CONNECTION.get()
    if binding is not None and binding[0] == Path(store_root).resolve():
        return binding[1]
    return None


def require_unbound_writer(store_root: Path) -> None:
    """Reject writes before borrowing a request-owned read connection."""
    if _bound_connection(store_root) is not None:
        raise RuntimeError("cannot write while a read connection is bound for this store")


def require_write_connection(conn: sqlite3.Connection) -> None:
    """Connection-level writers must not mutate the bound read transaction."""
    binding = _REQUEST_CONNECTION.get()
    if binding is not None and binding[1] is conn:
        raise RuntimeError("cannot write through a bound read connection")


# ── schema version gate (P07) ─────────────────────────────────────
#
# Unknown stamps are never restamped. Known v1/v2/v3 schemas remain readable;
# adding slot or region tables requires the explicit migration entry point.

#: status values reported by ``inspect_schema``
SCHEMA_ABSENT = "absent"      # no DB file (or an empty one) — safe to create
SCHEMA_CURRENT = "current"    # stamped with SCHEMA_VERSION — safe to use
SCHEMA_OLDER = "older"        # created by an older build — needs migration
SCHEMA_NEWER = "newer"        # created by a newer build — needs newer code
SCHEMA_UNKNOWN = "unknown"    # stamp present but not comparable — unreadable identity
SCHEMA_CORRUPT = "corrupt"    # not a readable SekaiSync database


@dataclass(frozen=True)
class SchemaState:
    """Read-only classification of a store's schema stamp."""

    status: str
    version: Optional[str] = None   # stamp as found on disk (None if absent/corrupt)
    path: Optional[Path] = None     # the DB file the state describes
    detail: str = ""                # human-readable extra context

    @property
    def is_current(self) -> bool:
        return self.status == SCHEMA_CURRENT

    @property
    def is_usable(self) -> bool:
        """True only for a store this build may open without changing it."""
        return self.status in (SCHEMA_ABSENT, SCHEMA_CURRENT)


class RevisionConflictError(RuntimeError):
    """A conditional write lost a race with another writer.

    Raised when a caller supplies ``expected_revision`` and the store has since
    moved on.  The caller must recompute against the current state rather than
    overwrite the newer data with a result derived from obsolete inputs.
    """


class SchemaVersionError(RuntimeError):
    """A store cannot be opened safely by this build.

    Raised instead of silently restamping an unrecognised schema so that a
    newer store is never reinterpreted as an older one.  ``status`` carries
    the ``inspect_schema`` classification; ``found`` / ``supported`` name
    the on-disk and expected schema versions.
    """

    def __init__(
        self,
        message: str,
        *,
        status: str,
        path: Optional[Path] = None,
        found: Optional[str] = None,
        supported: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.found = found
        self.supported = supported


def _readonly_uri(path: Path) -> str:
    """``file:`` URI that opens ``path`` read-only without creating it.

    ``mode=ro`` never creates the database file, never creates parent
    directories, and cannot write — so inspecting an unknown store leaves it
    byte-identical.  (``immutable=1`` is deliberately not used: it ignores a
    hot WAL and would misreport a valid store as empty.)
    """
    return Path(path).resolve().as_uri() + "?mode=ro"


def _version_key(value: str) -> Optional[tuple[int, ...]]:
    """Comparable key for dotted-integer stamps; None when not comparable."""
    parts = value.strip().split(".")
    if not parts or any(not part.strip().isdigit() for part in parts):
        return None
    key = tuple(int(part) for part in parts)
    while len(key) > 1 and key[-1] == 0:  # "1.0" == "1"
        key = key[:-1]
    return key


def inspect_schema(store_root: Path) -> SchemaState:
    """Classify a store as absent / current / older / newer / unknown / corrupt.

    Read-only: opens a ``mode=ro`` URI, so it creates no directory, no file
    and never rewrites the schema stamp.  ``absent`` means "safe to
    initialize"; anything else is a decision for the caller.  A non-empty
    database with no ``meta`` stamp is reported as corrupt — never guessed
    to be new.
    """
    path = db_path(Path(store_root))
    if not path.exists():
        return SchemaState(SCHEMA_ABSENT, None, path)
    try:
        if path.stat().st_size == 0:
            # SQLite itself treats a zero-byte file as a valid empty database
            # (exactly what an interrupted first-time create leaves behind),
            # so nothing can be lost by initializing it.
            return SchemaState(SCHEMA_ABSENT, None, path)
    except OSError as exc:
        return SchemaState(SCHEMA_CORRUPT, None, path, f"{type(exc).__name__}: {exc}")
    try:
        conn = sqlite3.connect(_readonly_uri(path), uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        return SchemaState(SCHEMA_CORRUPT, None, path, f"{type(exc).__name__}: {exc}")
    try:
        has_meta = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone()
        if not has_meta:
            return SchemaState(
                SCHEMA_CORRUPT, None, path, "database has no meta table"
            )
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
    except sqlite3.Error as exc:
        return SchemaState(SCHEMA_CORRUPT, None, path, f"{type(exc).__name__}: {exc}")
    finally:
        conn.close()
    if not row or not str(row[0] or "").strip():
        return SchemaState(
            SCHEMA_CORRUPT, None, path, "meta has no schema_version stamp"
        )
    version = str(row[0]).strip()
    if version in SUPPORTED_SCHEMA_VERSIONS:
        return SchemaState(SCHEMA_CURRENT, version, path)
    found_key = _version_key(version)
    supported_key = _version_key(SCHEMA_VERSION)
    if found_key is None or supported_key is None or found_key == supported_key:
        # ``version`` already failed the exact match above, so an equal key
        # means a different spelling of a comparable version (e.g. "1.0"
        # vs "1"). Refuse rather than guess which one is on disk.
        return SchemaState(
            SCHEMA_UNKNOWN, version, path,
            f"schema_version {version!r} is not a version this build recognises",
        )
    if found_key > supported_key:
        return SchemaState(SCHEMA_NEWER, version, path)
    return SchemaState(SCHEMA_OLDER, version, path)


def _schema_error(state: SchemaState) -> SchemaVersionError:
    """Actionable error for a store this build must not open."""
    where = f"store database {state.path}"
    if state.status == SCHEMA_NEWER:
        message = (
            f"{where} was written by a newer SekaiSync (schema_version "
            f"{state.version!r}; this build supports {SCHEMA_VERSION!r}). "
            f"Refusing to open it so the newer data is not rewritten as an "
            f"older schema. Use a SekaiSync build that supports "
            f"{state.version!r}, or point --store at another directory."
        )
    elif state.status == SCHEMA_OLDER:
        message = (
            f"{where} uses an older schema (schema_version {state.version!r}; "
            f"this build supports {SCHEMA_VERSION!r}). It was not migrated "
            f"automatically and will not be rewritten in place. Run the "
            f"explicit migration with a build that supports {state.version!r}, "
            f"or point --store at another directory."
        )
    elif state.status == SCHEMA_UNKNOWN:
        message = (
            f"{where} carries a schema version this build does not recognise "
            f"({state.detail}; this build supports {SCHEMA_VERSION!r}). "
            f"Refusing to guess its schema; point --store at another directory."
        )
    else:
        message = (
            f"{where} is not a readable SekaiSync database "
            f"({state.detail or 'unreadable'}; this build supports schema_version "
            f"{SCHEMA_VERSION!r}). It was left untouched and NOT recreated; "
            f"restore it from a backup or point --store at another directory."
        )
    return SchemaVersionError(
        message,
        status=state.status,
        path=state.path,
        found=state.version,
        supported=SCHEMA_VERSION,
    )


def initialize(store_root: Path) -> None:
    """Create the v1 schema in a new/absent store, or verify a current one.

    Refuses (``SchemaVersionError``) to touch a store stamped with any other
    version so an unknown store is never silently restamped.  For transition
    reasons a fresh store is still created as v1: existing term writers and
    fixtures build implicit stores through this path and v1 stays fully
    supported.  Creating a per-language-slot v2 store is an explicit choice —
    use :func:`initialize_new_store` (P07/P08); migration of an existing v1
    store is :func:`migrate_store`, never automatic.
    """
    state = inspect_schema(store_root)
    if state.status == SCHEMA_CURRENT:
        return
    if state.status != SCHEMA_ABSENT:
        raise _schema_error(state)
    with connect(store_root) as conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()


def initialize_new_store(store_root: Path, *, target_version: int = 2) -> None:
    """Explicitly create a v2 slot store or a v3 region store (never automatic).

    v2 adds ``term_slots`` plus the review queue/decision/rule tables.  v3
    (Astra P03/P07) adds ``entity_region_facts`` on top of the unchanged v2
    tables; it is created only when ``target_version=3`` is passed.  Existing
    v1 stores are not touched by this function; they move to v2/v3 only
    through :func:`migrate_store`.
    """
    from sekaisync.term_slots import create_schema

    require_unbound_writer(store_root)
    if type(target_version) is not int or target_version not in (2, 3):
        raise ValueError(
            f"initialize_new_store supports target_version 2 or 3, got {target_version!r}"
        )
    state = inspect_schema(store_root)
    if state.status == SCHEMA_CURRENT:
        if state.version != str(target_version):
            raise _schema_error(state)
        return
    if state.status != SCHEMA_ABSENT:
        raise _schema_error(state)
    with connect(store_root) as conn:
        conn.executescript(_SCHEMA)
        create_schema(conn)
        if target_version == 3:
            _create_region_schema(conn)
        meta = [("schema_version", str(target_version))]
        if target_version == 2:
            meta.append(("v2_migration", "explicit_new_store"))
        else:
            meta.append(("v3_migration", "explicit_new_store"))
        conn.executemany(
            "INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)",
            meta,
        )
        conn.commit()


def migrate_store(
    store_root: Path,
    *,
    target_version: int,
    dry_run: bool = True,
    backup_path: Optional[Path] = None,
    verifier: Optional[Any] = None,
    expected_plan_digest: Optional[str] = None,
) -> dict:
    """Explicit v1→v2 / v2→v3 migration entry point.

    v1→v2 (per-language term slots) is delegated to ``term_slots``.  v2→v3
    (``entity_region_facts``, Astra P03/P07) is implemented here: it is a
    table-preserving migration — the v2 slot/review data is never regenerated,
    legacy unscoped ``facts_json`` stays exactly as it is (it is NOT copied
    into per-region rows), and the new table starts empty.

    Default is a read-only dry-run diff.  Applying requires an explicit
    ``backup_path`` for a new consistent backup (created inside the writer
    lease before any schema change) and is atomic + idempotent; there is no
    startup auto-migration (P07).
    """
    if type(target_version) is not int:
        raise ValueError('target_version must be an integer')
    if target_version == 3:
        return _migrate_store_v3(
            store_root,
            dry_run=dry_run,
            backup_path=backup_path,
            expected_plan_digest=expected_plan_digest,
        )
    from sekaisync.term_slots import migrate_store as _migrate

    return _migrate(
        store_root,
        target_version=target_version,
        dry_run=dry_run,
        backup_path=backup_path,
        verifier=verifier,
        expected_plan_digest=expected_plan_digest,
    )


def _migrate_store_v3(
    store_root: Path,
    *,
    dry_run: bool,
    backup_path: Optional[Path],
    expected_plan_digest: Optional[str],
) -> dict:
    """Explicit v2→v3 migration (``entity_region_facts``).

    Refuses anything that is not a current v2 store.  The plan digest covers
    the observable pre-migration state so a caller authorizing a dry-run can
    detect that the store changed before applying.
    """
    require_unbound_writer(store_root)
    state = inspect_schema(store_root)
    if state.version == "3":
        if dry_run:
            return {
                "from_version": 3, "target_version": 3, "dry_run": True,
                "already_current": True,
            }
        return {
            "from_version": 3, "target_version": 3, "dry_run": False,
            "already_current": True,
        }
    if state.status != SCHEMA_CURRENT:
        raise _schema_error(state)
    if state.version == "1":
        raise ValueError(
            "v1 stores must migrate to v2 (term slots) first; v3 migration "
            "requires an explicitly authorized v2 store"
        )
    if state.version != "2":
        raise _schema_error(state)

    def _plan(conn: sqlite3.Connection) -> dict:
        legacy_entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        # Hash the committed SQL state incrementally: even writers not yet
        # participating in data_revision cannot invalidate an approved plan
        # unnoticed. No full database dump is retained in memory.
        from hashlib import sha256
        digest = sha256()
        for statement in conn.iterdump():
            digest.update(statement.encode('utf-8'))
            digest.update(b'\n')
        payload = {"database_digest": digest.hexdigest()}
        return {
            "from_version": 2,
            "target_version": 3,
            "legacy_unscoped_entities": legacy_entities,
            "region_facts": 0,
            "already_current": False,
            "plan_digest": _plan_digest(payload),
            "note": (
                "legacy unscoped facts are preserved as-is and are not copied "
                "into per-region rows; entity_region_facts starts empty and is "
                "repopulated only from verified raw snapshots"
            ),
        }

    if dry_run:
        with contextlib.closing(
            sqlite3.connect(_readonly_uri(db_path(store_root)), uri=True)
        ) as conn:
            conn.execute("BEGIN")
            return {**_plan(conn), "dry_run": True}

    if backup_path is None:
        raise ValueError("explicit migration apply requires a new backup_path")
    from sekaisync.fetcher import store_writer_lock

    backup_path = Path(backup_path)
    if backup_path.resolve() == db_path(store_root).resolve():
        raise ValueError("backup_path must not be the active database")
    with store_writer_lock(store_root), connect(store_root) as conn:
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("BEGIN IMMEDIATE")
        try:
            if _meta_get(conn, "schema_version") != "2":
                raise RevisionConflictError(
                    "schema changed while waiting for migration"
                )
            if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("source quick_check failed")
            plan = _plan(conn)
            if (
                expected_plan_digest is not None
                and plan["plan_digest"] != expected_plan_digest
            ):
                raise RevisionConflictError(
                    "migration plan changed; run dry-run and authorize again"
                )
            # Reserve a NEW path; never overwrite a prior recovery artifact.
            with backup_path.open("xb"):
                pass
            # A separate RO connection backs up the committed pre-migration
            # snapshot, including committed WAL pages.
            with contextlib.closing(
                sqlite3.connect(_readonly_uri(db_path(store_root)), uri=True)
            ) as source:
                with contextlib.closing(sqlite3.connect(str(backup_path))) as backup:
                    source.backup(backup)
                    if backup.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError("backup quick_check failed")
            _create_region_schema(conn)
            if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('migration foreign key check failed')
            revision = bump_revision(conn)
            _meta_set(conn, 'v3_migration', 'explicit_v2_migration')
            # Stamp last, inside the same transaction as DDL and revision.
            _meta_set(conn, 'schema_version', '3')
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return {**plan, "dry_run": False, "revision": revision,
            "backup_path": str(backup_path)}


def _plan_digest(payload: dict) -> str:
    from hashlib import sha256

    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256(encoded).hexdigest()


def _require_v1_write_store(store_root: Path) -> None:
    """v2 stores must not be written through the legacy terms API (fail closed).

    On a v2 store ``names_json``/``official``/``trust`` are projections of
    ``term_slots``; a legacy write would desynchronize them, so it is refused
    with ``ValueError`` mentioning slots.  v1 and absent stores pass unchanged.
    """
    require_unbound_writer(store_root)
    state = inspect_schema(store_root)
    if state.status == SCHEMA_CURRENT and state.version in {"2", "3"}:
        raise ValueError(
            "this store uses explicit per-language term slots (schema v2); "
            "write through sekaisync.term_slots.commit_slot_decisions instead"
        )


def initialized(store_root: Path) -> bool:
    """True only when the store exists and carries this build's schema stamp."""
    return inspect_schema(store_root).status == SCHEMA_CURRENT


def _ensure_initialized(store_root: Path) -> None:
    """Tables only; write APIs must never trigger a legacy import (recursion).

    A bound request connection (``read_connection``) means the request has
    already fixed a snapshot; initialization writes are skipped and the bound
    connection is used as-is.  An unbound read of a store this build cannot
    open is still refused with ``SchemaVersionError``.
    """
    state = inspect_schema(store_root)
    if _bound_connection(store_root) is not None:
        if state.status in (SCHEMA_ABSENT, SCHEMA_CURRENT):
            return
        raise _schema_error(state)
    if state.status == SCHEMA_CURRENT:
        return
    if state.status == SCHEMA_ABSENT:
        initialize(store_root)
        return
    raise _schema_error(state)


def ensure_store(store_root: Path) -> None:
    """Guarantee an initialized DB; auto-import pending legacy JSON domains.

    Per-domain import markers let hand-written legacy fixtures (tests) and
    real legacy stores migrate lazily, while archived stores (files moved
    away, domain already imported) never re-trigger.

    A store stamped with a version this build does not understand is refused
    with ``SchemaVersionError`` and left byte-identical — reads must never
    downgrade it into a plausible-looking older store.  When a request-level
    connection is bound, no initialization/import write happens: a request
    snapshot must stay read-only and consistent.
    """
    if _bound_connection(store_root) is not None:
        _ensure_initialized(store_root)
        return
    state = inspect_schema(store_root)
    if state.status == SCHEMA_ABSENT:
        # Astra P07: a brand-new store is created at the current schema, never
        # stamped v1 "for compatibility with old logic". Creating it at v1 would
        # also make the legacy bootstrap import fail: a registry.json carries
        # per-region facts that v1 cannot hold, and v1 has no term slots.
        # Existing stores are never touched here — they migrate only through the
        # explicit ``migrate_store`` entry point.
        initialize_new_store(store_root, target_version=int(LATEST_SCHEMA_VERSION))
    elif state.status != SCHEMA_CURRENT:
        raise _schema_error(state)
    pending = pending_legacy_domains(store_root)
    if pending:
        import_legacy_domains(store_root, pending)


# ── legacy JSON import ────────────────────────────────────────────

def legacy_domain_files(store_root: Path) -> dict[str, list[Path]]:
    """Legacy JSON files per domain.

    Only ``kb/`` is searched: a shelved archive under ``legacy/`` is inert
    by design — fresh stores must build from the original sites, never
    from the shelved carriers.
    """
    candidates = [store_root / "kb"]
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
        try:
            value = row[name]
        except (IndexError, KeyError):
            # Metadata projections deliberately omit `text`; a caller that
            # asked for no text gets no text rather than an error.
            if name == "text":
                continue
            raise
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


def web_trust_buckets(
    store_root: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Count pages grouped by exactly the inputs trust and category use.

    Trust is a pure function of (source, kind, source_type, auxiliary,
    overlay, derived, translation_source), so a GROUP BY over those columns
    yields a few hundred buckets instead of 752k rows.  The caller computes
    trust once per bucket and multiplies by the count — same answer, without
    materialising every page in Python first (Astra D06 step 1: "聚合用
    GROUP BY").

    Returns one dict per bucket with the grouping columns plus ``count``.
    """
    _ensure_initialized(store_root)
    sql = """
        SELECT source, kind, source_type, auxiliary, overlay, derived,
               translation_source, COUNT(*) AS count
        FROM web_pages
        GROUP BY source, kind, source_type, auxiliary, overlay, derived,
                 translation_source
    """

    def _rows(active: sqlite3.Connection) -> list[dict[str, Any]]:
        cursor = active.execute(sql)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]

    if conn is not None:
        return _rows(conn)
    with connect(store_root) as owned:
        return _rows(owned)


def browse_web_rows(
    store_root: Path,
    *,
    source_ids: Optional[Sequence[str]] = None,
    language: Optional[str] = None,
    kinds: Optional[Sequence[str]] = None,
    limit: int,
    include_overlay: bool = False,
    source_priority: Optional[Sequence[str]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Filtered, ordered, LIMITed page metadata straight out of SQL.

    Replaces the "load every page, filter in Python, then slice" shape that
    made `web_browse(limit=20)` materialise 752k full-text rows to return 20
    (Astra D06: "browse 在 SQL 中做完整过滤、稳定排序、LIMIT").

    Two phases, because the expensive column and the ranking columns are
    different:

    1. Filter + order + LIMIT over metadata **only** — ``text`` is never
       touched, so the scan reads small columns and a top-N sort keeps memory
       bounded by the limit rather than the table.
    2. Read ``length(text)`` and ``substr(text, 1, 300)`` for just those K
       winners, keyed by primary key.

    Filters use the **stored** ``aux_flag`` / ``derived_flag`` columns, written
    by ``is_auxiliary_page`` / ``is_derived_page`` at insert time — verified to
    agree with recomputation across every distinct filter-relevant tuple in the
    real store (0 disagreements / 97 tuples), so the SQL predicate is exact
    rather than an approximation of the Python rule.

    Ordering reproduces the previous two-pass Python sort (source priority
    ascending, then newest ``crawled_at``) via a CASE over the caller's
    priority list, with ``seq`` as a deterministic tiebreak.

    Only caller-resolved value sets are bound; nothing is interpolated.
    """
    _ensure_initialized(store_root)
    where: list[str] = ["NOT (derived_flag = 1 AND aux_flag = 0)"]
    params: list[Any] = []
    if not include_overlay:
        where.append("aux_flag = 0")

    if source_ids:
        where.append(f"source IN ({','.join('?' * len(source_ids))})")
        params.extend(source_ids)
    if language:
        where.append("language = ?")
        params.append(language)
    if kinds:
        where.append(f"LOWER(kind) IN ({','.join('?' * len(kinds))})")
        params.extend([str(k).lower() for k in kinds])

    priority = [str(s) for s in (source_priority or ())]
    if priority:
        cases = " ".join("WHEN ? THEN ?" for _ in priority)
        order_rank = f"CASE source {cases} ELSE {len(priority)} END"
        rank_params: list[Any] = []
        for index, source_id in enumerate(priority):
            rank_params.extend([source_id, index])
    else:
        order_rank = "0"
        rank_params = []

    columns = ", ".join(c for c in _PAGE_COLUMNS if c != "text")
    sql = (
        f"SELECT source, {columns}, aux_flag, derived_flag, extra_json, seq "
        f"FROM web_pages WHERE {' AND '.join(where)} "
        f"ORDER BY {order_rank}, crawled_at DESC, source, seq "
        f"LIMIT ?"
    )
    # Parameter binding is positional: the WHERE placeholders are bound before
    # the ORDER BY ones, so `params` must come first even though the CASE
    # appears later in the statement text.
    phase1_params = params + rank_params + [max(int(limit or 0), 0)]

    def _rows(active: sqlite3.Connection) -> list[dict[str, Any]]:
        cursor = active.execute(sql, phase1_params)
        cursor.row_factory = sqlite3.Row
        rows = [_web_page_row_to_dict(row, include_text=False) for row in cursor]
        if not rows:
            return rows
        # Phase 2: read text-derived values for the winners only.
        keys = [(item.get("source", ""), item.get("id", "")) for item in rows]
        placeholders = ",".join("(?,?)" for _ in keys)
        head_sql = (
            f"SELECT source, id, length(text) AS text_length, "
            f"substr(text, 1, 300) AS text_head FROM web_pages "
            f"WHERE (source, id) IN (VALUES {placeholders})"
        )
        head_params: list[Any] = []
        for source_id, page_id in keys:
            head_params.extend([source_id, page_id])
        derived: dict[tuple, tuple[int, str]] = {}
        for row in active.execute(head_sql, head_params):
            derived[(row[0], row[1])] = (
                int(row[2] or 0),
                str(row[3] or ""),
            )
        for item in rows:
            length_value, head = derived.get(
                (item.get("source", ""), item.get("id", "")), (0, "")
            )
            item["text_length"] = length_value
            # Snippet source: only the first 300 chars, read for K rows rather
            # than for every candidate.
            item["text_head"] = head
        return rows

    if conn is not None:
        return _rows(conn)
    with connect(store_root) as owned:
        return _rows(owned)


def iter_web_search_rows(
    store_root: Path,
    *,
    source_ids: Optional[Sequence[str]] = None,
    language: Optional[str] = None,
    include_overlay: bool = False,
    score_chars: int = 20000,
    batch_size: int = 512,
    conn: Optional[sqlite3.Connection] = None,
) -> Iterator[dict[str, Any]]:
    """Stream the columns search needs to score a page, one batch at a time.

    Only ``title`` and the first ``score_chars`` of ``text`` are read — the
    same window the baseline scorer used (``text[:20000]``) — so scoring sees
    identical inputs while the body stays in SQLite.  Rows are fetched in
    batches and yielded immediately, so peak memory is O(batch) rather than
    O(pages).

    Deliberately does **not** order or limit: the caller keeps a fixed-size
    top-K heap because ranking depends on a Python-side fuzzy score that SQL
    cannot reproduce (Astra P06 explicitly rejects replacing it with
    ``LIKE '%query%' LIMIT K``, which changes recall and source priority).
    """
    _ensure_initialized(store_root)
    where = ["NOT (derived_flag = 1 AND aux_flag = 0)"]
    params: list[Any] = []
    if not include_overlay:
        where.append("aux_flag = 0")
    if source_ids:
        where.append(f"source IN ({','.join('?' * len(source_ids))})")
        params.extend(source_ids)
    if language:
        where.append("language = ?")
        params.append(language)

    index_columns = tuple(c for c in _PAGE_COLUMNS if c != "text")
    sql = (
        f"SELECT source, {', '.join(index_columns)}, aux_flag, derived_flag, "
        f"extra_json, seq, length(text) AS text_length, "
        f"substr(text, 1, ?) AS text_head "
        f"FROM web_pages WHERE {' AND '.join(where)} ORDER BY source, seq"
    )
    bound = [score_chars] + params

    def _iterate(active: sqlite3.Connection) -> Iterator[dict[str, Any]]:
        cursor = active.execute(sql, bound)
        cursor.row_factory = sqlite3.Row
        while True:
            batch = cursor.fetchmany(batch_size)
            if not batch:
                return
            for row in batch:
                item = _web_page_row_to_dict(row, include_text=False)
                item["text_length"] = int(row["text_length"] or 0)
                item["source"] = row["source"]
                item["text_head"] = str(row["text_head"] or "")
                yield item

    if conn is not None:
        yield from _iterate(conn)
        return
    with connect(store_root) as owned:
        yield from _iterate(owned)


def web_page_texts(
    store_root: Path,
    keys: Sequence[tuple[str, str]],
    conn: Optional[sqlite3.Connection] = None,
) -> dict[tuple[str, str], str]:
    """Full bodies for the named (source, id) pairs only.

    Used by callers that genuinely need the text (``include_text=True``) after
    the candidate set has been narrowed, so bodies are read for the rows being
    returned rather than for every row considered.
    """
    _ensure_initialized(store_root)
    if not keys:
        return {}
    out: dict[tuple[str, str], str] = {}

    def _read(active: sqlite3.Connection) -> None:
        # Chunked so the placeholder count stays well under SQLite's limit.
        for start in range(0, len(keys), 400):
            chunk = keys[start:start + 400]
            placeholders = ",".join("(?,?)" for _ in chunk)
            params: list[Any] = []
            for source_id, page_id in chunk:
                params.extend([source_id, page_id])
            for row in active.execute(
                f"SELECT source, id, text FROM web_pages "
                f"WHERE (source, id) IN (VALUES {placeholders})",
                params,
            ):
                out[(row[0], row[1])] = str(row[2] or "")

    if conn is not None:
        _read(conn)
        return out
    with connect(store_root) as owned:
        _read(owned)
    return out


def load_web_index_rows(
    store_root: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> list[dict[str, Any]]:
    """Merged index records (metadata only, no text) — load_web_index shape.

    The projection is explicit: `text` is never selected.  It used to be
    pulled for every row just to compute ``text_length`` in Python, which
    meant a metadata-only query read the whole text layer.  SQLite computes
    ``length(text)`` without shipping the body.  ``source_type`` / ``instance``
    / ``trust`` are all preserved so trust still derives from identical inputs.

    Rows come back as ``sqlite3.Row`` (mapped by column name) rather than via
    a per-row shim class: the old helper built a *new class object* for every
    one of 752k rows, which cost more than the query itself (Astra D06).

    Passing ``conn`` reuses a caller's read transaction (P02 ``ReadView``) so
    this read belongs to the same generation as the rest of a response.
    """
    _ensure_initialized(store_root)
    index_columns = tuple(c for c in _PAGE_COLUMNS if c != "text")
    sql = (
        f"SELECT source, {', '.join(index_columns)}, aux_flag, derived_flag, "
        f"extra_json, seq, length(text) AS text_length "
        f"FROM web_pages ORDER BY source, seq"
    )

    def _rows(active: sqlite3.Connection) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor = active.execute(sql)
        cursor.row_factory = sqlite3.Row
        for row in cursor:
            item = _web_page_row_to_dict(row, include_text=False)
            record = {
                field: item.get(field)
                for field in _INDEX_RECORD_FIELDS
                if field in item
            }
            record["text_length"] = int(row["text_length"] or 0)
            record["source"] = row["source"]
            out.append(record)
        return out

    if conn is not None:
        return _rows(conn)
    with connect(store_root) as owned:
        return _rows(owned)


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

_REGION_FACTS_TABLE = "entity_region_facts"


def _schema_version_of(conn: sqlite3.Connection) -> Optional[str]:
    """Stamp as seen by this connection, or None when there is no stamp."""
    try:
        return _meta_get(conn, "schema_version")
    except sqlite3.Error:
        return None


def _reject_unknown_region_stamp(store_root: Path, conn: sqlite3.Connection) -> Optional[str]:
    """Schema gate for entity writers on an open connection.

    Returns the on-disk version when this build may write entities, raises
    ``SchemaVersionError`` otherwise.  Unreadable stamps are refused here even
    though ``inspect_schema`` (which sees only committed state) cannot run
    inside the caller's open transaction.
    """
    stamp = _schema_version_of(conn)
    if stamp in SUPPORTED_SCHEMA_VERSIONS or stamp == REGION_SCHEMA_VERSION:
        return stamp
    if stamp is None:
        raise SchemaVersionError(
            f"store database {db_path(store_root)} has no readable schema_version "
            f"stamp inside this transaction; refusing to write entities "
            f"(supported: {SCHEMA_VERSION})",
            status=SCHEMA_CORRUPT,
            path=db_path(store_root),
        )
    raise _schema_error(inspect_schema(store_root))


def _region_rows_for(entity: Entity) -> list[tuple]:
    rows = []
    for region in sorted(entity.region_facts):
        rf = entity.region_facts[region]
        if not isinstance(region, str) or not region.strip():
            raise ValueError('region_facts requires a nonempty region key')
        if not isinstance(rf, RegionFacts):
            raise ValueError('region_facts values must be RegionFacts')
        if not isinstance(rf.facts, dict) or not isinstance(rf.retrieval, dict):
            raise ValueError('region facts and retrieval must be dictionaries')
        if not isinstance(rf.source, str) or not (rf.version is None or isinstance(rf.version, str)):
            raise ValueError('region source/version must be strings (version may be None)')
        if rf.region != region:
            raise ValueError(
                f"region_facts key {region!r} does not match RegionFacts.region "
                f"{rf.region!r} for entity {entity.id!r}"
            )
        rows.append(
            (
                entity.id, region,
                json.dumps(rf.facts, ensure_ascii=False, sort_keys=True, allow_nan=False),
                rf.source or "", rf.version,
                json.dumps(rf.retrieval, ensure_ascii=False, sort_keys=True, allow_nan=False),
            )
        )
    return rows


def save_entities(
    store_root: Path,
    entities: Iterable[Entity],
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Replace the entity table, region facts included (schema v3).

    On a v3 store each entity's :class:`~sekaisync.models.RegionFacts` rows are
    written in the **same transaction** as the ``entities`` rows: replacement
    of an entity removes its region rows with it, so a read never mixes an old
    ``facts_json`` with new per-region rows.  Scoped facts on a v1/v2 store
    (or a v2 one with no ``entity_region_facts`` table) are refused with
    ``ValueError`` naming the migration, instead of being silently dropped —
    an empty ``region_facts`` dict (legacy unscoped entities) stays writable
    on every schema.  ``conn`` lets a caller run this inside its own
    transaction; then no commit/revision happens here (caller-owned, P11
    shape) and validation still runs inside that transaction.

    A publish must commit the derived indexes and the generation pointer
    together, and opening a second connection while a write transaction is
    open would deadlock on the store's own lock.
    """
    if conn is None:
        require_unbound_writer(store_root)
        _ensure_initialized(store_root)

    def _write(active: sqlite3.Connection) -> int:
        # Deferred to first statement inside the caller's transaction, so a
        # stale schema is caught before any row is written.
        stamp = _reject_unknown_region_stamp(store_root, active)
        materialized = list(entities)
        seen: set[str] = set()
        region_payload: list[tuple] = []
        for entity in materialized:
            if entity.id in seen:
                raise ValueError(f"duplicate entity id in snapshot: {entity.id!r}")
            seen.add(entity.id)
            region_payload.extend(_region_rows_for(entity))
        scoped_on_old_schema = bool(region_payload) and stamp != REGION_SCHEMA_VERSION
        if scoped_on_old_schema:
            raise ValueError(
                f"store {store_root} uses schema {stamp!r}; persisting "
                f"entity_region_facts requires an explicit migrate_store to "
                f"schema 3 — this build will not silently drop region "
                f"provenance"
            )
        has_region_table = active.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='entity_region_facts'"
        ).fetchone() is not None
        rows = [
            (
                e.id, e.type, e.region or "", json.dumps(e.regions, ensure_ascii=False),
                json.dumps(e.names, ensure_ascii=False), json.dumps(e.facts, ensure_ascii=False),
                e.source or "", e.version, 1 if e.demo else 0, e.trust or "", seq,
            )
            for seq, e in enumerate(materialized, start=1)
        ]
        active.execute("DELETE FROM entities")
        if has_region_table:
            # Region rows of surviving ids are rewritten too: a replacement is
            # a whole-entity snapshot, never a partial region merge.
            active.execute("DELETE FROM entity_region_facts")
        active.executemany(
            "INSERT OR REPLACE INTO entities VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        if region_payload:
            active.executemany(
                "INSERT INTO entity_region_facts "
                "(entity_id, region, facts_json, source, version, retrieval_json) "
                "VALUES(?,?,?,?,?,?)",
                region_payload,
            )
        return len(rows)

    if conn is not None:
        require_write_connection(conn)
        if not conn.in_transaction:
            raise ValueError(
                "save_entities(conn=...) requires an active transaction "
                "(BEGIN) owned by the caller"
            )
        conn.execute('SAVEPOINT entity_snapshot')
        try:
            result = _write(conn)
            conn.execute('RELEASE entity_snapshot')
            return result
        except BaseException:
            conn.execute('ROLLBACK TO entity_snapshot')
            conn.execute('RELEASE entity_snapshot')
            raise
    from sekaisync.fetcher import store_writer_lock
    with store_writer_lock(store_root), connect(store_root) as owned:
        owned.execute('BEGIN IMMEDIATE')
        try:
            result = _write(owned)
            bump_revision(owned)
            owned.commit()
            return result
        except BaseException:
            owned.rollback()
            raise


def load_entities(store_root: Path) -> list[Entity]:
    ensure_store(store_root)
    with connect(store_root) as conn:
        # Two SELECTs must share a snapshot even outside a Core ReadView.
        owned_snapshot = not conn.in_transaction
        if owned_snapshot:
            conn.execute('BEGIN')
        try:
            return _load_entities_conn(conn)
        finally:
            if owned_snapshot:
                conn.rollback()


def _load_entities_conn(conn: sqlite3.Connection) -> list[Entity]:
    region_facts: dict[str, dict[str, RegionFacts]] = {}
    # Read scoped facts only when the transaction's schema promises them.
    # A malformed v3 database with a missing table must fail, not invent an
    # empty/legacy result. Unversioned additive tables on v1/v2 are not authority.
    if _schema_version_of(conn) == REGION_SCHEMA_VERSION:
        for row in conn.execute(
            "SELECT entity_id, region, facts_json, source, version, retrieval_json "
            "FROM entity_region_facts ORDER BY entity_id, region"
        ):
            region_facts.setdefault(row[0], {})[row[1]] = RegionFacts(
                region=row[1], facts=json.loads(row[2]), source=row[3],
                version=row[4], retrieval=json.loads(row[5]),
            )
    out = []
    for row in conn.execute("SELECT * FROM entities ORDER BY id"):
        out.append(
            Entity(
                id=row[0], type=row[1], region=row[2],
                regions=json.loads(row[3]), names=json.loads(row[4]),
                facts=json.loads(row[5]), source=row[6], version=row[7],
                demo=bool(row[8]), trust=row[9],
                region_facts=region_facts.get(row[0], {}),
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


def save_glossary_terms(
    store_root: Path,
    terms: Iterable[GlossaryTerm],
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """Replace the glossary table.

    ``conn`` lets a caller run this inside its own transaction — a publish must
    commit indexes and the generation pointer together, and a second
    connection would deadlock against the open write transaction.
    """
    def _write(active: sqlite3.Connection) -> int:
        rows = [
            (
                g.id, g.kind, g.canonical, json.dumps(g.names, ensure_ascii=False),
                1 if g.official else 0, g.source or "", 1 if g.demo else 0, g.trust or "", seq,
            )
            for seq, g in enumerate(terms, start=1)
        ]
        active.execute("DELETE FROM glossary_terms")
        active.executemany(
            "INSERT OR REPLACE INTO glossary_terms VALUES(?,?,?,?,?,?,?,?,?)", rows
        )
        if conn is None:
            active.commit()
        return len(rows)

    if conn is not None:
        return _write(conn)
    _ensure_initialized(store_root)
    with connect(store_root) as owned:
        return _write(owned)


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
    """Deprecated legacy write — superseded by the P01 write contract.

    Astra P01/D01: this single entry cannot express the three distinct
    intentions (snapshot replace / partial update / evidence patch), so callers
    could not state whether a record absent from the list should be *removed*
    or *kept*, and a record whose evidence was merely not loaded was
    indistinguishable from one with no evidence. All product callers now use:

    - :func:`replace_terms_snapshot` — full snapshot, absent ids are removed
    - :func:`upsert_terms` — touches only the ids given; evidence per id is
      preserved unless an explicit ``evidence_updates`` entry says otherwise

    Kept only because Astra's offline verifier probes this function to record
    the baseline defect; new code must not call it.
    """
    from sekaisync.termindex import term_to_dict

    _require_v1_write_store(store_root)
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
            if replace_evidence:
                # An empty replacement clears the rows — the advertised
                # count of 0 must match term_evidence exactly.
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


# ── terms write contract (Astra P01) ──────────────────────────────
#
# `save_terms_records` conflates three different intentions: replace the whole
# snapshot, update a few records, and update evidence.  Callers cannot express
# "delete everything not in this list" or "clear this term's evidence", and a
# light record (one whose in-memory evidence list is merely not loaded) is
# indistinguishable from a record that genuinely has no evidence.  The three
# operations are separated below so a write says what it means.

@dataclass
class WriteResult:
    """What a terms write actually did, and the revision it landed at.

    Counts are read back from the database rather than inferred from the input
    length, so a caller (and a test) can compare the claim against the rows.
    """

    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    evidence_rows: int = 0
    revision: int = 0


@dataclass
class EvidenceUpdate:
    """An explicit evidence change for one term.

    ``mode='replace'`` with ``items=[]`` clears the evidence — that is a real
    operation, not an absence.  Omitting an id from ``evidence_updates``
    entirely means *preserve*, which is why the two cases are distinct types
    of statement rather than one nullable list.
    """

    mode: str  # "replace" | "append"
    items: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.mode not in ("replace", "append"):
            raise ValueError(
                f"EvidenceUpdate.mode must be 'replace' or 'append', got {self.mode!r}"
            )


def _evidence_rows_for(term_id: str, items: Sequence[Any]) -> list[tuple]:
    rows: list[tuple] = []
    for idx, ev in enumerate(items):
        if not isinstance(ev, dict):
            continue
        known = {"story_key", "language", "term", "sentence"}
        extra = {k: v for k, v in ev.items() if k not in known}
        rows.append(
            (
                term_id, idx,
                str(ev.get("story_key") or ""), str(ev.get("language") or ""),
                str(ev.get("term") or ""), str(ev.get("sentence") or ""),
                json.dumps(extra, ensure_ascii=False),
            )
        )
    return rows


def _term_row_from_record(item: dict, evidence_count: int) -> tuple:
    return (
        item.get("id", ""), item.get("canonical", ""),
        item.get("source_language", ""), item.get("kind", "term"),
        json.dumps(item.get("names") or {}, ensure_ascii=False),
        1 if item.get("official") else 0, item.get("source", ""),
        item.get("created_at", ""), float(item.get("confidence", 1.0)),
        item.get("trust", ""), json.dumps(item.get("tags") or [], ensure_ascii=False),
        int(item.get("occurrences") or 0), float(item.get("weight") or 0.0),
        1 if item.get("everyday") else 0,
        json.dumps(item.get("positions") or [], ensure_ascii=False),
        evidence_count,
    )


#: Explicit column list.  `INSERT OR REPLACE` with a bare VALUES list silently
#: depends on physical column order, which is exactly how a future column
#: addition quietly corrupts writes.
_TERM_UPSERT = """
    INSERT INTO terms(
        id, canonical, source_language, kind, names_json, official, source,
        created_at, confidence, trust, tags_json, occurrences, weight,
        everyday, positions_json, evidence_count
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    ON CONFLICT(id) DO UPDATE SET
        canonical=excluded.canonical,
        source_language=excluded.source_language,
        kind=excluded.kind,
        names_json=excluded.names_json,
        official=excluded.official,
        source=excluded.source,
        created_at=excluded.created_at,
        confidence=excluded.confidence,
        trust=excluded.trust,
        tags_json=excluded.tags_json,
        occurrences=excluded.occurrences,
        weight=excluded.weight,
        everyday=excluded.everyday,
        positions_json=excluded.positions_json,
        evidence_count=excluded.evidence_count
"""


def replace_terms_snapshot(
    store_root: Path,
    records: Iterable[Any],
    *,
    evidence_by_id: Mapping[str, Sequence[dict]],
    expected_revision: Optional[int] = None,
) -> WriteResult:
    """Replace the term set with exactly ``records``, evidence included.

    This is the destructive full-snapshot operation.  Every id in ``records``
    must appear in ``evidence_by_id`` (possibly with an empty sequence), which
    forces the caller to state the evidence for each record instead of leaving
    it to be guessed from whether a list happens to be non-empty.

    ``expected_revision``, when given, makes the write conditional: if another
    writer committed in the meantime the call fails rather than overwriting
    their work.
    """
    from sekaisync.termindex import term_to_dict

    _ensure_initialized(store_root)
    _require_v1_write_store(store_root)

    items: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        item = term_to_dict(rec)
        term_id = str(item.get("id", ""))
        if not term_id:
            raise ValueError("every term record needs a non-empty id")
        if term_id in seen:
            raise ValueError(f"duplicate term id in snapshot: {term_id!r}")
        if term_id not in evidence_by_id:
            raise ValueError(
                f"snapshot is missing evidence for {term_id!r}; pass an explicit "
                f"sequence (an empty one means 'no evidence'), so completeness is "
                f"stated rather than inferred"
            )
        seen.add(term_id)
        items.append(item)

    with connect(store_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if expected_revision is not None:
                actual = current_revision(conn)
                if actual != expected_revision:
                    raise RevisionConflictError(
                        f"store is at revision {actual}, expected {expected_revision}; "
                        f"another writer committed — recompute against the current state"
                    )

            before = {
                row[0] for row in conn.execute("SELECT id FROM terms")
            }
            keep = seen

            # Delete what the snapshot does not contain, evidence first so no
            # orphan rows survive a mid-way failure.
            removed = before - keep
            for term_id in removed:
                conn.execute("DELETE FROM term_evidence WHERE term_id=?", (term_id,))
                conn.execute("DELETE FROM terms WHERE id=?", (term_id,))

            evidence_total = 0
            inserted = 0
            for item in items:
                term_id = item["id"]
                if term_id not in before:
                    inserted += 1
                conn.execute("DELETE FROM term_evidence WHERE term_id=?", (term_id,))
                rows = _evidence_rows_for(term_id, evidence_by_id.get(term_id) or [])
                if rows:
                    conn.executemany(
                        "INSERT INTO term_evidence VALUES(?,?,?,?,?,?,?)", rows
                    )
                evidence_total += len(rows)
                # evidence_count is derived from the rows actually written, so
                # the advertised count and the stored rows cannot disagree.
                conn.execute(_TERM_UPSERT, _term_row_from_record(item, len(rows)))

            revision = bump_revision(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return WriteResult(
        inserted=inserted,
        updated=len(items) - inserted,
        deleted=len(removed),
        evidence_rows=evidence_total,
        revision=revision,
    )


def upsert_terms(
    store_root: Path,
    records: Iterable[Any],
    *,
    evidence_updates: Optional[Mapping[str, Any]] = None,
    expected_revision: Optional[int] = None,
) -> WriteResult:
    """Insert or update only the given records; touch nothing else.

    Evidence handling is explicit per id:

    - id absent from ``evidence_updates`` -> **preserve** existing evidence
      (a light record whose evidence was never loaded must not wipe the store)
    - ``{"mode": "replace", "items": [...]}`` -> replace with exactly those
    - ``{"mode": "replace", "items": []}`` -> clear
    - ``{"mode": "append", "items": [...]}`` -> append, de-duplicated

    Records the caller did not mention are left completely alone, which is what
    makes this safe for partial scrapes and hand corrections.
    """
    from sekaisync.termindex import term_to_dict

    _ensure_initialized(store_root)
    _require_v1_write_store(store_root)
    updates = dict(evidence_updates or {})

    items: list[dict] = []
    seen: set[str] = set()
    for rec in records:
        item = term_to_dict(rec)
        term_id = str(item.get("id", ""))
        if not term_id:
            raise ValueError("every term record needs a non-empty id")
        if term_id in seen:
            raise ValueError(f"duplicate term id in upsert: {term_id!r}")
        seen.add(term_id)
        items.append(item)

    with connect(store_root) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if expected_revision is not None:
                actual = current_revision(conn)
                if actual != expected_revision:
                    raise RevisionConflictError(
                        f"store is at revision {actual}, expected {expected_revision}; "
                        f"another writer committed — recompute against the current state"
                    )

            before = {row[0] for row in conn.execute("SELECT id FROM terms")}
            inserted = 0
            evidence_total = 0

            for item in items:
                term_id = item["id"]
                if term_id not in before:
                    inserted += 1

                update = updates.get(term_id)
                if update is None:
                    # Preserve: keep the stored count and rows untouched.
                    row = conn.execute(
                        "SELECT evidence_count FROM terms WHERE id=?", (term_id,)
                    ).fetchone()
                    stored = int(row[0]) if row else 0
                    conn.execute(_TERM_UPSERT, _term_row_from_record(item, stored))
                    continue

                if not isinstance(update, EvidenceUpdate):
                    if isinstance(update, dict):
                        update = EvidenceUpdate(
                            mode=str(update.get("mode", "")),
                            items=list(update.get("items") or []),
                        )
                    else:
                        raise ValueError(
                            f"evidence update for {term_id!r} must be an "
                            f"EvidenceUpdate or a mapping, got {type(update).__name__}"
                        )

                if update.mode == "replace":
                    conn.execute("DELETE FROM term_evidence WHERE term_id=?", (term_id,))
                    rows = _evidence_rows_for(term_id, update.items)
                else:  # append — de-duplicate against what is already stored
                    existing = _evidence_rows_for(
                        term_id, list(_load_evidence_items(conn, term_id))
                    )
                    existing_keys = {_evidence_key(r) for r in existing}
                    fresh = [
                        r
                        for r in _evidence_rows_for(term_id, update.items)
                        if _evidence_key(r) not in existing_keys
                    ]
                    rows = existing + fresh

                if rows:
                    conn.execute("DELETE FROM term_evidence WHERE term_id=?", (term_id,))
                    conn.executemany(
                        "INSERT INTO term_evidence VALUES(?,?,?,?,?,?,?)",
                        [
                            (term_id, idx, r[2], r[3], r[4], r[5], r[6])
                            for idx, r in enumerate(rows)
                        ],
                    )
                evidence_total += len(rows)
                conn.execute(_TERM_UPSERT, _term_row_from_record(item, len(rows)))

            revision = bump_revision(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return WriteResult(
        inserted=inserted,
        updated=len(items) - inserted,
        deleted=0,
        evidence_rows=evidence_total,
        revision=revision,
    )


def _evidence_key(row: tuple) -> str:
    """Identity of an evidence row for append de-duplication.

    Deliberately excludes ``idx``: the index is presentation order, and using
    it as identity would let the same evidence be appended repeatedly under
    ever-growing indices.
    """
    return json.dumps(
        [str(row[2]), str(row[3]), str(row[4]), str(row[5]), str(row[6])],
        ensure_ascii=False,
        sort_keys=True,
    )


def _load_evidence_items(conn: sqlite3.Connection, term_id: str) -> list[dict]:
    out: list[dict] = []
    for row in conn.execute(
        "SELECT story_key, language, term, sentence, extra_json "
        "FROM term_evidence WHERE term_id=? ORDER BY idx",
        (term_id,),
    ):
        entry = {"story_key": row[0], "language": row[1]}
        if row[2]:
            entry["term"] = row[2]
        if row[3]:
            entry["sentence"] = row[3]
        entry.update(json.loads(row[4] or "{}"))
        out.append(entry)
    return out


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
        slots_by_id: dict[str, dict] | None = None
        if _meta_get(conn, "schema_version") in {"2", "3"}:
            from sekaisync.term_slots import load_term_slots
            slots_by_id = {}
            for slot in load_term_slots(store_root, conn=conn):
                slots_by_id.setdefault(slot["term_id"], {})[slot["language"]] = slot
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
            if slots_by_id is not None:
                item["slots"] = slots_by_id.get(row[0], {})
            out.append(term_from_dict(item))
        return out


def evidence_for_ids(
    store_root: Path, term_ids: list[str], include_sentences: bool = True
) -> dict[str, list[dict]]:
    """Evidence bodies for selected term ids (server result enrichment)."""
    ensure_store(store_root)
    wanted = [tid for tid in term_ids if tid]
    if not wanted:
        return {}
    sentence_col = "sentence" if include_sentences else "'' AS sentence"
    out: dict[str, list[dict]] = {}
    with connect(store_root) as conn:
        for chunk_start in range(0, len(wanted), 500):
            chunk = wanted[chunk_start:chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT term_id, story_key, language, term, {sentence_col}, extra_json "
                f"FROM term_evidence WHERE term_id IN ({placeholders}) ORDER BY term_id, idx",
                chunk,
            ):
                entry = {"story_key": row[1], "language": row[2]}
                if row[3]:
                    entry["term"] = row[3]
                if row[4]:
                    entry["sentence"] = row[4]
                extra = json.loads(row[5] or "{}")
                entry.update(extra)
                out.setdefault(row[0], []).append(entry)
    return out


def attach_evidence(store_root: Path, records: list[Any], include_sentences: bool = True) -> None:
    """Fill full evidence bodies (with sentences) for the given records only."""
    ensure_store(store_root)
    wanted = [r.id for r in records if r.id]
    if not wanted:
        return
    sentence_col = "sentence" if include_sentences else "'' AS sentence"
    with connect(store_root) as conn:
        evidence: dict[str, list[dict]] = {}
        for chunk_start in range(0, len(wanted), 500):
            chunk = wanted[chunk_start:chunk_start + 500]
            placeholders = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT term_id, story_key, language, term, {sentence_col}, extra_json "
                f"FROM term_evidence WHERE term_id IN ({placeholders}) ORDER BY term_id, idx",
                chunk,
            ):
                entry = {"story_key": row[1], "language": row[2]}
                if row[3]:
                    entry["term"] = row[3]
                if row[4]:
                    entry["sentence"] = row[4]
                extra = json.loads(row[5] or "{}")
                entry.update(extra)
                evidence.setdefault(row[0], []).append(entry)
    for rec in records:
        if rec.id in evidence:
            rec.evidence = evidence[rec.id]


def term_status_from_db(store_root: Path) -> dict[str, Any]:
    """term_status() shape via SQL; delegates top_by_tag to the Python impl."""
    from collections import Counter

    from sekaisync.normalize import normalize_name
    from sekaisync.termindex import TAG_VOCAB, term_status

    ensure_store(store_root)
    with connect(store_root) as conn:
        if _meta_get(conn, "schema_version") in {"2", "3"}:
            result = term_status(load_terms_records(store_root))
            result["slot_statuses"] = dict(conn.execute(
                "SELECT status, COUNT(*) FROM term_slots GROUP BY status"
            ).fetchall())
            return result
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
    # Tables-only gate: called from inside import_legacy_domains, so it must
    # not trigger a legacy import (recursion) — but it must still refuse a
    # store whose schema this build does not understand.
    _ensure_initialized(store_root)
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
    """Legacy JSON files per domain.

    Only ``kb/`` is searched: a shelved archive under ``legacy/`` is inert
    by design — fresh stores must build from the original sites, never
    from the shelved carriers.
    """
    candidates = [store_root / "kb"]
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


def current_revision(conn: sqlite3.Connection) -> int:
    """The committed write generation of this store.

    A counter in ``meta`` bumped inside the same transaction as any fact
    change, so a reader can tell one committed generation from the next.
    Readers must NOT use ``PRAGMA data_version`` for this: it is only
    meaningful relative to a single long-lived connection and is not a global
    commit sequence (Astra P02).
    """
    try:
        raw = _meta_get(conn, "data_revision")
    except sqlite3.Error:
        return 0
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0


def bump_revision(conn: sqlite3.Connection) -> int:
    """Advance the store revision. Caller owns the transaction.

    Must be called inside the same transaction as the writes it describes, so
    a reader either sees the new facts together with the new revision or sees
    neither.
    """
    next_revision = current_revision(conn) + 1
    _meta_set(conn, "data_revision", str(next_revision))
    return next_revision


# ── raw generation pointers (Astra P13) ────────────────────────────
#
# `raw/` region master tables are published as immutable generations.  The
# per-region "which generation is live" pointer lives in the same SQL
# transaction as the rest of a publish, so moving the pointer and committing
# the derived indexes is one atomic event: a reader sees either the complete
# old generation or the complete new one, never a mix.

def active_generations(
    store_root: Path,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, str]:
    """Per-region active raw generation id (``{}`` when none is published).

    A store that has never run a generation publish returns ``{}``, which
    readers interpret as "use the legacy in-place layout".
    """
    def _read(active: sqlite3.Connection) -> dict[str, str]:
        raw = None
        with contextlib.suppress(sqlite3.Error):
            raw = _meta_get(active, ACTIVE_GENERATION_KEY)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): str(v) for k, v in parsed.items()}

    if conn is not None:
        return _read(conn)
    # A read must never create the database. `connect()` would create an empty
    # file, and a store that exists but has no `meta` table is then classified
    # as unusable rather than absent — so merely asking "which generation is
    # active?" could turn a valid absent store into an unopenable one.
    if not db_path(store_root).exists():
        return {}
    state = inspect_schema(store_root)
    if state.status not in (SCHEMA_ABSENT, SCHEMA_CURRENT):
        # Unknown/failed schema: report "no pointer" rather than raising, so
        # path resolution stays side-effect free. The write and server paths
        # still enforce the gate.
        return {}
    _ensure_initialized(store_root)
    with connect(store_root) as owned:
        return _read(owned)


def set_active_generations(
    conn: sqlite3.Connection,
    pointers: Mapping[str, str],
) -> None:
    """Write the active-generation pointers. Caller owns the transaction.

    Written in the *same* transaction as the indexes it belongs to, so the
    pointer flip and the publish commit together or not at all.
    """
    _meta_set(
        conn,
        ACTIVE_GENERATION_KEY,
        json.dumps(dict(pointers), ensure_ascii=False, sort_keys=True),
    )


def pending_legacy_domains(store_root: Path) -> list[str]:
    """Domains whose legacy files exist but were never imported into the DB."""
    _ensure_initialized(store_root)
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
        # Astra P01: the legacy terms JSON is a full snapshot, so its import is
        # a snapshot replace (per-id evidence comes from the same records).
        records = load_terms(sources["terms"][0])
        if inspect_schema(store_root).version in {"2", "3"}:
            from sekaisync.term_slots import ingest_legacy_terms
            from sekaisync.termindex import term_to_dict
            with connect(store_root) as conn:
                revision = current_revision(conn)
            ingest_legacy_terms(store_root, [term_to_dict(record) for record in records],
                                expected_revision=revision)
        else:
            replace_terms_snapshot(
                store_root,
                records,
                evidence_by_id={
                    record.id: list(record.evidence or []) for record in records
                },
            )
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
    store_root = Path(store_root)
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
