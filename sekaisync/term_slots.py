"""Explicit v2 slot storage and migration (P07/P08/P11).

No algorithm or startup path certifies legacy trust. ``verifier`` is a trusted
application callback, not a JSON boolean supplied by a candidate. It must
independently verify identity, exact value, provenance and evidence, returning
an auditable structured certificate. Without it, proposed accepted slots remain
pending. This module never reads settings, raw data, or the default store.

Schema v3 is reserved for entity_region_facts; it is not implemented here.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import contextlib
from hashlib import sha256
import json
import math
from pathlib import Path
import sqlite3
from typing import Any, Callable

from sekaisync import dbstore

V2_SCHEMA = (
    """CREATE TABLE term_slots (
        term_id TEXT NOT NULL REFERENCES terms(id) ON DELETE CASCADE,
        language TEXT NOT NULL, value TEXT,
        status TEXT NOT NULL CHECK(status IN ('accepted','pending','conflict','rejected')),
        official INTEGER NOT NULL DEFAULT 0 CHECK(official IN (0,1)),
        source TEXT NOT NULL DEFAULT '', trust TEXT NOT NULL DEFAULT ''
            CHECK(trust IN ('','A','B','C','D')),
        confidence REAL NOT NULL DEFAULT 0 CHECK(confidence >= 0 AND confidence <= 1),
        evidence_refs_json TEXT NOT NULL DEFAULT '[]', decision_revision INTEGER NOT NULL,
        legacy_payload_json TEXT NOT NULL DEFAULT '{}', reason TEXT NOT NULL DEFAULT '',
        candidates_json TEXT NOT NULL DEFAULT '[]', scope_json TEXT NOT NULL DEFAULT '{}',
        verification_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(term_id, language)
    ) WITHOUT ROWID""",
    """CREATE TABLE review_queue (
        item_id TEXT PRIMARY KEY, term_id TEXT NOT NULL, language TEXT NOT NULL,
        scope_json TEXT NOT NULL, candidates_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL, evidence_snapshot_json TEXT NOT NULL,
        input_revision INTEGER NOT NULL, evidence_revision TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('queued','resolved','superseded')),
        UNIQUE(term_id, language, evidence_revision)
    )""",
    """CREATE TABLE review_decisions (
        decision_id TEXT PRIMARY KEY, item_id TEXT NOT NULL, term_id TEXT NOT NULL,
        language TEXT NOT NULL, action TEXT NOT NULL, value TEXT,
        scope_json TEXT NOT NULL, evidence_revision TEXT NOT NULL,
        evidence_snapshot_json TEXT NOT NULL, decision_revision INTEGER NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}'
    )""",
    """CREATE TABLE review_rules (
        rule_id TEXT PRIMARY KEY, family TEXT NOT NULL, key TEXT NOT NULL,
        scope_json TEXT NOT NULL, value TEXT, active INTEGER NOT NULL,
        revision INTEGER NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}',
        UNIQUE(family, key, scope_json, revision)
    )""",
    "CREATE INDEX idx_review_queue_status ON review_queue(status, term_id, language)",
)

Verifier = Callable[[dict, list[dict]], Mapping[str, Any] | None]


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _digest(value: Any) -> str:
    return sha256(_json(value).encode('utf-8')).hexdigest()


def create_schema(conn: sqlite3.Connection) -> None:
    """Caller owns the transaction; never use executescript during migration."""
    for statement in V2_SCHEMA:
        conn.execute(statement)


def evidence_with_ids(term_id: str, items: Sequence[Mapping]) -> list[dict]:
    """Assign stable identity independent of presentation idx; preserve extras."""
    result: list[dict] = []
    seen: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, Mapping):
            raise ValueError('evidence must be a mapping')
        ev = dict(item)
        if ev.get('subject_id', term_id) != term_id or ev.get('term_id', term_id) != term_id:
            raise ValueError('evidence subject does not match term_id')
        identity = {k: v for k, v in ev.items() if k not in {'evidence_id', 'idx'}}
        eid = ev.get('evidence_id') or 'ev:' + _digest([term_id, identity])
        if not isinstance(eid, str):
            raise ValueError('evidence_id must be a string')
        ev['evidence_id'] = eid
        if eid in seen:
            if seen[eid] != ev:
                raise ValueError('conflicting payload for stable evidence_id')
            continue
        seen[eid] = ev
        result.append(ev)
    return result


def _certificate(slot: dict, evidence: list[dict], verifier: Verifier | None) -> dict | None:
    if verifier is None:
        return None
    # Callback cannot mutate the candidate or evidence being committed.
    proof = verifier(json.loads(_json(slot)), json.loads(_json(evidence)))
    if not isinstance(proof, Mapping) or not isinstance(proof.get('verifier'), str) or not proof['verifier'].strip():
        return None
    proof = dict(proof)
    for key, expected in [('subject_id', slot['term_id']), ('language', slot['language']),
                          ('value', slot['value']), ('source', slot['source'])]:
        if proof.get(key) != expected:
            return None
    refs = proof.get('evidence_refs')
    if not isinstance(refs, (list, tuple)) or any(not isinstance(r, str) for r in refs):
        return None
    value = slot['value']
    if not isinstance(value, str) or not value:
        return None
    if not refs:
        # P08 L0: this store's own official index is authority for a name it
        # carries for exactly one entity in that language. Such a certificate
        # names the entity instead of citing corpus evidence; only the callback
        # can produce it (a candidate cannot inject one), and it must claim
        # official A, so an unverified self-report still needs evidence.
        entity_ref = proof.get('verified_entity')
        if not (isinstance(entity_ref, str) and entity_ref.strip()):
            return None
        if proof.get('official') is not True or proof.get('trust') not in {'A', 'B', 'C', 'D'}:
            return None
        return proof
    index = {ev['evidence_id']: ev for ev in evidence}
    if any(ref not in index for ref in refs):
        return None
    support = [index[ref] for ref in refs]
    # The callback verifies upstream authority; local checks prevent accidental
    # certificate reuse across subjects, slots, values or sources.
    if not all(ev.get('language') == slot['language'] and
               ev.get('source') == slot['source'] and
               (ev.get('term') == value or ev.get('value') == value or
                value in str(ev.get('sentence') or '')) for ev in support):
        return None
    official = proof.get('official') is True
    trust = proof.get('trust')
    if trust not in {'A', 'B', 'C', 'D'} or (trust == 'A' and not official):
        return None
    if official:
        if trust != 'A':
            return None
    elif len({ev.get('story_key') for ev in support if ev.get('story_key')}) < 2:
        return None
    return proof


def index_verifier(conn: sqlite3.Connection) -> Verifier:
    """Certify a slot against this store's own official name index (P08 L0).

    Astra's adoption rule accepts a slot outright when it matches the same
    already-verified official entity, that language's value and source. That
    check needs the store's index, not the candidate's self-report, so it lives
    here as a callback the caller passes in — a candidate can never produce one.

    Two rules keep this from borrowing authority:

    - a surface shared by more than one entity is *ambiguous* and is refused
      outright, rather than picking whichever entity came first;
    - the certificate reports the index row's own trust, so a demo or community
      index certifies at its real level instead of the 'A' the legacy record
      claimed for itself.
    """
    index: list[tuple] = []
    for row in conn.execute(
        "SELECT id, canonical, names_json, source, trust FROM glossary_terms WHERE official=1"
    ):
        names = json.loads(row[2] or '{}')
        surfaces = {value for value in names.values() if value}
        surfaces.add(row[1])
        index.append((row[0], surfaces, {k: v for k, v in names.items() if v},
                      row[3] or '', row[4] or ''))

    def verify(slot: dict, evidence: list[dict]) -> Mapping[str, Any] | None:
        value = slot.get('value')
        language = slot.get('language')
        term_id = slot.get('term_id')
        if not isinstance(value, str) or not value or not isinstance(term_id, str):
            return None
        row = conn.execute("SELECT canonical FROM terms WHERE id=?", (term_id,)).fetchone()
        if row is None:
            return None
        canonical = row[0]
        matches = [
            (entity_id, source, trust) for entity_id, surfaces, names, source, trust in index
            if names.get(language) == value and canonical in surfaces
            and not (source and slot.get('source') and source != slot.get('source'))
        ]
        if len(matches) != 1:
            # Zero matches: the index does not carry this name for this subject.
            # Several matches: a homograph — the pipeline must not borrow one
            # entity's authority for a surface it shares with others.
            return None
        entity_id, source, trust = matches[0]
        return {
            'subject_id': term_id, 'language': language, 'value': value,
            'source': slot.get('source') or source, 'official': True,
            'trust': trust, 'verifier': f'official-index:{entity_id}',
            'verified_entity': entity_id, 'evidence_refs': [],
        }

    return verify


def _prepare_slot(raw: Mapping, evidence: list[dict], verifier: Verifier | None,
                  *, legacy: bool = False) -> dict:
    slot = dict(raw)
    term_id = slot.get('term_id') or slot.get('subject_id')
    language = slot.get('language')
    if not isinstance(term_id, str) or not term_id or not isinstance(language, str) or not language:
        raise ValueError('slot requires nonempty term_id and language')
    value = slot.get('value')
    if value is not None and not isinstance(value, str):
        raise ValueError('slot value must be a string or None')
    status = slot.get('status', 'pending')
    if status not in {'accepted', 'pending', 'conflict', 'rejected'}:
        raise ValueError('invalid slot status')
    confidence = slot.get('confidence', 0.0)
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError('slot confidence must be a finite number between 0 and 1')
    refs = slot.get('evidence_refs') or []
    if not isinstance(refs, (list, tuple)) or any(not isinstance(r, str) for r in refs):
        raise ValueError('slot evidence_refs must contain stable evidence IDs')
    index = {ev['evidence_id'] for ev in evidence}
    if any(r not in index for r in refs):
        raise ValueError('slot evidence reference does not resolve for this subject')
    source = slot.get('source', '')
    if not isinstance(source, str):
        raise ValueError('slot source must be a string')
    candidates = slot.get('candidates') or []
    scope = slot.get('scope') or {'subject_id': term_id, 'language': language}
    if not isinstance(candidates, (list, tuple)) or not isinstance(scope, Mapping):
        raise ValueError('slot candidates must be a list and scope a mapping')
    if scope.get('subject_id', term_id) != term_id or scope.get('language', language) != language:
        raise ValueError('slot scope mismatch')
    slot.update(term_id=term_id, language=language, value=value, status=status,
                source=source, confidence=float(confidence), evidence_refs=list(refs),
                candidates=list(candidates), scope=dict(scope),
                legacy_payload=dict(slot.get('legacy_payload') or {}),
                reason=str(slot.get('reason') or ''), verification={})
    if legacy or status == 'accepted':
        proof = _certificate(slot, evidence, verifier)
        if proof is not None:
            slot.update(status='accepted', official=proof.get('official') is True,
                        trust=proof['trust'], evidence_refs=list(proof['evidence_refs']),
                        verification=proof, reason='verified_evidence')
        else:
            slot.update(status='pending', official=False, trust='', confidence=0.0,
                        reason='legacy_unverified' if legacy else 'insufficient_evidence')
    else:
        # Pending/rejected claims do not gain active provenance from self-report.
        slot.update(official=False, trust='', confidence=0.0)
    return slot


_SLOT_COLUMNS = ('term_id', 'language', 'value', 'status', 'official', 'source', 'trust',
                 'confidence', 'evidence_refs_json', 'decision_revision', 'legacy_payload_json',
                 'reason', 'candidates_json', 'scope_json', 'verification_json')


def _write_slot(conn: sqlite3.Connection, slot: dict, revision: int) -> None:
    values = (slot['term_id'], slot['language'], slot['value'], slot['status'], int(slot['official']),
              slot['source'], slot['trust'], slot['confidence'], _json(slot['evidence_refs']), revision,
              _json(slot['legacy_payload']), slot['reason'], _json(slot['candidates']),
              _json(slot['scope']), _json(slot['verification']))
    conn.execute(f"INSERT INTO term_slots({','.join(_SLOT_COLUMNS)}) VALUES({','.join('?' for _ in values)}) "
                 'ON CONFLICT(term_id,language) DO UPDATE SET ' +
                 ','.join(f'{c}=excluded.{c}' for c in _SLOT_COLUMNS[2:]), values)


def _load_slots(conn: sqlite3.Connection) -> list[dict]:
    result = []
    for row in conn.execute(f"SELECT {','.join(_SLOT_COLUMNS)} FROM term_slots ORDER BY term_id,language"):
        slot = dict(zip(_SLOT_COLUMNS, row))
        slot['official'] = bool(slot['official'])
        for key in ('evidence_refs', 'legacy_payload', 'candidates', 'scope', 'verification'):
            slot[key] = json.loads(slot.pop(key + '_json'))
        result.append(slot)
    return result


def load_term_slots(store_root: Path, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Full per-slot state, including unverified legacy values and provenance."""
    if conn is not None:
        return _load_slots(conn)
    state = dbstore.inspect_schema(store_root)
    if state.version not in {'2', '3'}:
        raise ValueError('term slots require an explicitly initialized or migrated v2 store')
    with contextlib.closing(sqlite3.connect(dbstore._readonly_uri(dbstore.db_file(store_root)), uri=True)) as owned:
        return _load_slots(owned)


def _project(conn: sqlite3.Connection, term_id: str) -> None:
    accepted = conn.execute("SELECT language,value,official,source,trust,confidence FROM term_slots "
                            "WHERE term_id=? AND status='accepted' ORDER BY language", (term_id,)).fetchall()
    names = {row[0]: row[1] for row in accepted}
    trusts = [r[4] for r in accepted]
    trust = max(trusts, key=lambda t: {'A': 0, 'B': 1, 'C': 2, 'D': 3, '': 4}[t]) if trusts else ''
    sources = sorted({r[3] for r in accepted})
    count = conn.execute('SELECT COUNT(*) FROM term_evidence WHERE term_id=?', (term_id,)).fetchone()[0]
    conn.execute('UPDATE terms SET names_json=?,official=?,source=?,trust=?,confidence=?,evidence_count=? WHERE id=?',
                 (_json(names), int(bool(accepted) and all(r[2] for r in accepted)),
                  sources[0] if len(sources) == 1 else '', trust,
                  min((r[5] for r in accepted), default=0.0), count, term_id))


def _write_queue(conn: sqlite3.Connection, slot: dict, evidence: list[dict], revision: int) -> None:
    term_id, language = slot['term_id'], slot['language']
    if slot['status'] not in {'pending', 'conflict'}:
        conn.execute("UPDATE review_queue SET status='superseded' WHERE term_id=? AND language=? AND status='queued'",
                     (term_id, language))
        return
    snapshot = [ev for ev in evidence if ev['evidence_id'] in slot['evidence_refs']]
    identity = [slot['scope'], slot['value'], slot['candidates'], snapshot, slot['reason']]
    evidence_revision = _digest(identity)
    item_id = 'slot-review:' + _digest([term_id, language, evidence_revision])
    conn.execute("UPDATE review_queue SET status='superseded' WHERE term_id=? AND language=? AND item_id<>? AND status='queued'",
                 (term_id, language, item_id))
    conn.execute("INSERT INTO review_queue(item_id,term_id,language,scope_json,candidates_json,evidence_refs_json,"
                 "evidence_snapshot_json,input_revision,evidence_revision,status) VALUES(?,?,?,?,?,?,?,?,?,'queued') "
                 'ON CONFLICT(item_id) DO NOTHING',
                 (item_id, term_id, language, _json(slot['scope']), _json(slot['candidates']),
                  _json(slot['evidence_refs']), _json(snapshot), revision, evidence_revision))


def _store_evidence(conn: sqlite3.Connection, term_id: str, items: list[dict]) -> None:
    conn.execute('DELETE FROM term_evidence WHERE term_id=?', (term_id,))
    conn.executemany('INSERT INTO term_evidence(term_id,idx,story_key,language,term,sentence,extra_json) VALUES(?,?,?,?,?,?,?)',
                     dbstore._evidence_rows_for(term_id, items))


def _migration_plan(conn: sqlite3.Connection, verifier: Verifier | None) -> tuple[dict, dict[str, list[dict]]]:
    slots: list[dict] = []
    evidence_by_id: dict[str, list[dict]] = {}
    changed = 0
    cursor = conn.execute('SELECT * FROM terms ORDER BY id')
    columns = [c[0] for c in cursor.description]
    for row in cursor:
        legacy_row = dict(zip(columns, row))
        term_id = legacy_row['id']
        names = json.loads(legacy_row['names_json'])
        if not isinstance(names, dict):
            raise ValueError('legacy names_json must be an object; migration stopped without changes')
        evidence = evidence_with_ids(term_id, dbstore._load_evidence_items(conn, term_id))
        evidence_by_id[term_id] = evidence
        projected = {}
        for language, value in names.items():
            payload = {'value': value, 'official': legacy_row['official'],
                       'reported_trust': legacy_row['trust'], 'source': legacy_row['source'],
                       'confidence': legacy_row['confidence'], 'legacy_term_row': legacy_row}
            raw = dict(term_id=term_id, language=language, value=value, status='pending',
                       source=legacy_row['source'], confidence=0.0, legacy_payload=payload,
                       evidence_refs=[ev['evidence_id'] for ev in evidence if ev.get('language') == language])
            slot = _prepare_slot(raw, evidence, verifier, legacy=True)
            if slot['status'] == 'accepted':
                # Old confidence is a report, not a newly measured score.
                slot['confidence'] = 1.0
                projected[language] = value
            slots.append(slot)
        if projected != names:
            changed += 1
    report = {'from_version': 1, 'target_version': 2, 'already_current': False,
              'input_revision': dbstore.current_revision(conn), 'terms': len(evidence_by_id),
              'changed_terms': changed, 'slots': slots,
              'accepted_slots': sum(s['status'] == 'accepted' for s in slots),
              'pending_slots': sum(s['status'] == 'pending' for s in slots)}
    report['plan_digest'] = _digest(report)
    return report, evidence_by_id


def migrate_store(store_root: Path, *, target_version: int = 2, dry_run: bool = True,
                  backup_path: Path | None = None, verifier: Verifier | None = None,
                  expected_plan_digest: str | None = None) -> dict:
    """Read-only diff by default; apply requires a new consistent backup path.

    For approved diff workflows pass the dry-run's ``plan_digest`` back as
    ``expected_plan_digest``. Stale data or changed verification aborts before
    any backup/schema write. Reapplying v2 is a no-op and never replaces backup.
    Writers must be stopped or obey the existing per-store writer lease.
    """
    dbstore.require_unbound_writer(store_root)
    if type(target_version) is not int or target_version != 2:
        raise ValueError('only migration to v2 is implemented; v3 is reserved for entity_region_facts')
    state = dbstore.inspect_schema(store_root)
    if state.version == '2':
        return {'from_version': 2, 'target_version': 2, 'dry_run': dry_run, 'already_current': True}
    if state.version != '1':
        raise dbstore._schema_error(state)
    if dry_run:
        with contextlib.closing(sqlite3.connect(dbstore._readonly_uri(dbstore.db_file(store_root)), uri=True)) as conn:
            conn.execute('BEGIN')
            report, _ = _migration_plan(conn, verifier)
            report['dry_run'] = True
            return report
    if backup_path is None:
        raise ValueError('explicit migration apply requires a new backup_path')
    from sekaisync.fetcher import store_writer_lock
    backup_path = Path(backup_path)
    if backup_path.resolve() == dbstore.db_file(store_root).resolve():
        raise ValueError('backup_path must not be the active database')
    with store_writer_lock(store_root), dbstore.connect(store_root) as conn:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('BEGIN IMMEDIATE')
        try:
            if dbstore._meta_get(conn, 'schema_version') != '1':
                raise dbstore.RevisionConflictError('schema changed while waiting for migration')
            if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('source quick_check failed')
            report, evidence_by_id = _migration_plan(conn, verifier)
            if expected_plan_digest is not None and report['plan_digest'] != expected_plan_digest:
                raise dbstore.RevisionConflictError('migration plan changed; run dry-run and authorize again')
            # Reserve a NEW path; never overwrite a prior recovery artifact.
            with backup_path.open('xb'):
                pass
            # The write reservation above excludes SQL writers even if they do
            # not yet use the lease. A separate RO connection backs up the
            # committed pre-migration snapshot, including committed WAL pages.
            with contextlib.closing(sqlite3.connect(dbstore._readonly_uri(dbstore.db_file(store_root)), uri=True)) as source:
                with contextlib.closing(sqlite3.connect(backup_path)) as backup:
                    source.backup(backup)
                    if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('backup quick_check failed')
            create_schema(conn)
            revision = dbstore.current_revision(conn) + 1
            for term_id, evidence in evidence_by_id.items():
                _store_evidence(conn, term_id, evidence)
            for slot in report['slots']:
                _write_slot(conn, slot, revision)
                _write_queue(conn, slot, evidence_by_id[slot['term_id']], revision)
            for term_id in evidence_by_id:
                _project(conn, term_id)
            if conn.execute('SELECT COUNT(*) FROM term_slots').fetchone()[0] != len(report['slots']):
                raise ValueError('migration slot count mismatch')
            if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
                raise ValueError('migration foreign key check failed')
            dbstore.bump_revision(conn)
            dbstore._meta_set(conn, 'schema_version', '2')
            conn.commit()
            report.update(dry_run=False, revision=revision, backup_path=str(backup_path))
            return report
        except BaseException:
            conn.rollback()
            raise


def commit_slot_decisions_conn(conn: sqlite3.Connection, decisions: Iterable[Mapping], *,
                               records: Iterable[Mapping] = (),
                               evidence_by_id: Mapping[str, Sequence[Mapping]] | None = None,
                               expected_revision: int, verifier: Verifier | None = None) -> dict:
    """Commit helper for P11; requires caller lease + active SQL transaction.

    Does NOT commit or acquire a second connection. Uses a savepoint so caught
    errors cannot leave partial slot/evidence/queue writes in the outer txn.
    Evidence updates append; missing subjects must be supplied in ``records``.
    No names_json writes are accepted here: projection is exclusively slots.
    """
    dbstore.require_write_connection(conn)
    if not conn.in_transaction:
        raise ValueError('slot writes require an active transaction and writer lease')
    if dbstore._meta_get(conn, 'schema_version') not in {'2', '3'}:
        raise ValueError('slot decisions require explicit v2 migration')
    if type(expected_revision) is not int or dbstore.current_revision(conn) != expected_revision:
        raise dbstore.RevisionConflictError('slot input revision is stale; recompute')
    conn.execute('SAVEPOINT slot_commit')
    try:
        summary = _commit(conn, decisions, records, evidence_by_id or {}, expected_revision, verifier)
        conn.execute('RELEASE slot_commit')
        return summary
    except BaseException:
        conn.execute('ROLLBACK TO slot_commit')
        conn.execute('RELEASE slot_commit')
        raise


def _commit(conn, decisions, records, evidence_updates, revision, verifier):
    before_changes = conn.total_changes
    records = list(records)
    existing = {r[0] for r in conn.execute('SELECT id FROM terms')}
    inserted = 0
    for record in records:
        if not isinstance(record, Mapping) or not isinstance(record.get('id'), str) or not record['id']:
            raise ValueError('records must be mappings with nonempty id')
        if record['id'] in existing:
            # Metadata updates are intentionally separate from slot decisions;
            # never let a light TermRecord overwrite the current projection.
            continue
        item = dict(record, names={}, official=False, trust='', source='', confidence=0.0)
        conn.execute(dbstore._TERM_UPSERT, dbstore._term_row_from_record(item, 0))
        existing.add(item['id'])
        inserted += 1
    evidence = {}
    evidence_written = 0
    for term_id, items in evidence_updates.items():
        if term_id not in existing:
            raise ValueError('evidence references missing term')
        old = dbstore._load_evidence_items(conn, term_id)
        merged = evidence_with_ids(term_id, old + list(items))
        evidence[term_id] = merged
        if merged != old:
            _store_evidence(conn, term_id, merged)
            evidence_written += len(merged) - len(old)
    previous = {(s['term_id'], s['language']): s for s in _load_slots(conn)}
    seen = set()
    prepared = []
    touched = set(evidence_updates)
    for raw in decisions:
        if not isinstance(raw, Mapping):
            raise ValueError('slot decisions must be mappings')
        term_id = raw.get('term_id') or raw.get('subject_id')
        if term_id not in existing:
            raise ValueError('slot references missing term; supply record metadata')
        if term_id not in evidence:
            evidence[term_id] = evidence_with_ids(term_id, dbstore._load_evidence_items(conn, term_id))
        key = (term_id, raw.get('language'))
        if key in seen:
            raise ValueError('duplicate subject/language slot decision')
        seen.add(key)
        slot = _prepare_slot(raw, evidence[term_id], verifier)
        old = previous.get(key)
        # Legacy audit payload is immutable across subsequent decisions.
        if old:
            slot['legacy_payload'] = old['legacy_payload']
        prepared.append(slot)
        compare = dict(old or {})
        compare.pop('decision_revision', None)
        comparable = {k: slot[k] for k in compare} if compare else {}
        if not old or comparable != compare:
            _write_slot(conn, slot, revision + 1)
            _write_queue(conn, slot, evidence[term_id], revision + 1)
            touched.add(term_id)
    for term_id in touched:
        _project(conn, term_id)
    changed = conn.total_changes != before_changes
    if changed:
        revision = dbstore.bump_revision(conn)
    return {'revision': revision, 'inserted_terms': inserted, 'updated_terms': len(touched) - inserted,
            'evidence_written': evidence_written, 'applied_slots': sum(s['status'] == 'accepted' for s in prepared),
            **{status + '_slots': sum(s['status'] == status for s in prepared)
               for status in ('accepted', 'pending', 'conflict', 'rejected')}}


def ingest_legacy_terms(store_root: Path, records: Iterable[Mapping], *,
                        expected_revision: int) -> dict:
    """Queue legacy names without replacing existing decisions or borrowing trust."""
    dbstore.require_unbound_writer(store_root)
    from sekaisync.fetcher import store_writer_lock

    records = list(records)
    with store_writer_lock(store_root), dbstore.connect(store_root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            existing = {(s['term_id'], s['language']) for s in _load_slots(conn)}
            verify = index_verifier(conn)
            decisions = []
            evidence_by_id = {}
            for record in records:
                term_id = record['id']
                evidence = evidence_with_ids(term_id, record.get('evidence') or [])
                if evidence:
                    evidence_by_id[term_id] = evidence
                for language, value in record.get('names', {}).items():
                    key = (term_id, language)
                    if key in existing:
                        continue
                    existing.add(key)
                    decisions.append({
                        'term_id': term_id, 'language': language, 'value': value,
                        'status': 'accepted', 'source': record.get('source', ''),
                        'reason': 'legacy_unverified',
                        'legacy_payload': {'value': value, 'official': record.get('official', False),
                                           'reported_trust': record.get('trust', ''),
                                           'source': record.get('source', ''),
                                           'confidence': record.get('confidence', 0.0)},
                        'evidence_refs': [ev['evidence_id'] for ev in evidence
                                          if ev.get('language') == language],
                    })
            result = commit_slot_decisions_conn(
                conn, decisions, records=records, evidence_by_id=evidence_by_id,
                expected_revision=expected_revision, verifier=verify,
            )
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def decisions_from_record(record: Mapping) -> list[dict]:
    """A record's names as candidate slot decisions for a v2/v3 store.

    The decision is proposed as accepted and carries the record's own claims
    only as ``legacy_payload``: ``_prepare_slot`` still runs the certificate
    check, so an unverified name ends up pending/legacy_unverified and the
    accepted projection is never borrowed from a self-report.
    """
    term_id = str(record.get('id') or '')
    if not term_id:
        raise ValueError('every term record needs a non-empty id')
    evidence = evidence_with_ids(term_id, record.get('evidence') or [])
    out = []
    for language in sorted(language for language, value in (record.get('names') or {}).items() if value):
        out.append({
            'term_id': term_id, 'language': language, 'value': (record['names'])[language],
            'status': 'accepted', 'source': record.get('source', ''),
            'reason': 'legacy_unverified',
            'legacy_payload': {'value': (record['names'])[language],
                               'official': record.get('official', False),
                               'reported_trust': record.get('trust', ''),
                               'source': record.get('source', ''),
                               'confidence': record.get('confidence', 0.0)},
            'evidence_refs': [ev['evidence_id'] for ev in evidence
                              if ev.get('language') == language],
        })
    return out


def ingest_record_snapshot(store_root: Path, records: Sequence[Mapping], *,
                           expected_revision: int) -> dict:
    """Express a P01 terms snapshot as slot decisions on a v2/v3 store.

    ``terms init`` publishes a whole snapshot, but on a slot store the caller
    may not write ``names_json``: every name goes through the same decision
    path as any other candidate, so a record's self-reported trust stays audit
    data and only the store's own official index can certify a slot.  Absent
    ids are removed together with their evidence and slots, so a snapshot never
    leaves an orphaned subject behind.
    """
    dbstore.require_unbound_writer(store_root)
    from sekaisync.fetcher import store_writer_lock

    records = list(records)
    with store_writer_lock(store_root), dbstore.connect(store_root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            keep = {str(record['id']) for record in records}
            for (term_id,) in conn.execute('SELECT id FROM terms').fetchall():
                if term_id in keep:
                    continue
                for table in ('term_evidence', 'term_slots', 'review_queue', 'review_decisions'):
                    conn.execute(f'DELETE FROM {table} WHERE term_id=?', (term_id,))
                conn.execute('DELETE FROM terms WHERE id=?', (term_id,))
            existing = {(s['term_id'], s['language']) for s in _load_slots(conn)}
            verify = index_verifier(conn)
            decisions = []
            evidence_by_id = {}
            for record in records:
                term_id = str(record['id'])
                evidence = evidence_with_ids(term_id, record.get('evidence') or [])
                if evidence and evidence != evidence_with_ids(
                    term_id, dbstore._load_evidence_items(conn, term_id)
                ):
                    # Only a real change is stated: re-stating identical
                    # evidence would re-project the term and bump the
                    # revision, so an unchanged snapshot would look like a
                    # writer conflict to every concurrent reader.
                    evidence_by_id[term_id] = evidence
                # An existing slot is left alone: this snapshot's job is to
                # state which subjects exist, not to overwrite decisions a
                # reviewer already made about their languages.
                decisions.extend(
                    decision for decision in decisions_from_record(dict(record, evidence=evidence))
                    if (decision['term_id'], decision['language']) not in existing
                )
            result = commit_slot_decisions_conn(
                conn, decisions, records=records, evidence_by_id=evidence_by_id,
                expected_revision=expected_revision, verifier=verify,
            )
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise


def commit_slot_decisions(store_root: Path, decisions: Iterable[Mapping], *,
                          records: Iterable[Mapping] = (),
                          evidence_by_id: Mapping[str, Sequence[Mapping]] | None = None,
                          expected_revision: int, verifier: Verifier | None = None) -> dict:
    """P11 atomic slot + append evidence + review-queue + revision write API."""
    dbstore.require_unbound_writer(store_root)
    if dbstore.inspect_schema(store_root).version not in {'2', '3'}:
        raise ValueError('slot decisions require explicit v2 migration')
    from sekaisync.fetcher import store_writer_lock
    with store_writer_lock(store_root), dbstore.connect(store_root) as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            result = commit_slot_decisions_conn(conn, decisions, records=records,
                evidence_by_id=evidence_by_id, expected_revision=expected_revision, verifier=verifier)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
