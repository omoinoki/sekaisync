from __future__ import annotations

import contextlib
from contextvars import ContextVar
from functools import wraps
import copy
import json
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    from sekaisync.runtime import RuntimeContext

from sekaisync.config import REGIONS
from sekaisync import dbstore
from sekaisync.factpacks import build_fact_pack, build_fact_pack_at, load_fact_packs
from sekaisync.glossary import find_terms, load_glossary, resolve_name
from sekaisync.layout import (
    db_path,
    factpack_path,
    freshness_path,
    glossary_path,
    news_dir,
    region_master_dir,
    registry_path,
    terms_path,
    web_consent_path,
    web_index_path,
    web_root,
)
from sekaisync.normalize import normalize_name
from sekaisync.registry import entity_by_id, load_registry, lookup_entity
from sekaisync.termindex import load_terms, lookup_terms, term_status
from sekaisync.webindex import (
    is_auxiliary_page,
    load_web_index,
    auxiliary_page_summary,
    web_browse,
    web_search,
)


def _serialized_size(value: object) -> int:
    """Approximate serialized size, used to skip caching huge results."""
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


def _detached(value: dict) -> dict:
    """A copy the caller may freely mutate.

    Cached aggregates are shared between requests; handing out the cached
    object lets one caller's mutation become another caller's "fact".
    """
    return copy.deepcopy(value)


@dataclass(frozen=True)
class CoreSnapshot:
    """Immutable view of everything one request reads.

    ``revision`` is the committed store revision the collections were loaded
    at.  Aggregates computed from this snapshot are cached under that exact
    revision, so a result can never be filed under a generation it did not
    come from.
    """

    revision: int
    registry: object
    glossary: object
    terms: object
    factpacks: object

    def __getattribute__(self, name):
        value = object.__getattribute__(self, name)
        return copy.deepcopy(value) if name in {"registry", "glossary", "terms", "factpacks"} else value

    def raw(self, name: str):
        """The shared collection itself, not a detached copy.

        Attribute access on a snapshot deep-copies (~70k entities per access)
        so that anything handed *out* of a request cannot corrupt the shared
        state — the boundary ``view.registry`` / ``view.snapshot.registry``
        relies on.  ``SekaiSyncCore``'s own request methods read through
        ``raw()`` instead: they are internal, audited read-only over these
        collections, and every result they return is copied once by the
        ``request_scoped`` decorator.  Mutating what ``raw()`` returns would
        leak into every request pinned to this snapshot.
        """
        return object.__getattribute__(self, name)


@dataclass
class ReadView:
    """A request's fixed read context: one connection, one snapshot.

    Helpers that read SQL must use ``view.conn`` rather than opening their own
    connection, otherwise a helper's read can land in a different generation
    than the rest of the response.
    """

    core: "SekaiSyncCore"
    conn: sqlite3.Connection
    snapshot: "CoreSnapshot"

    @property
    def revision(self) -> int:
        return self.snapshot.revision

    @property
    def registry(self):
        return self.snapshot.registry

    @property
    def glossary(self):
        return self.snapshot.glossary

    @property
    def terms(self):
        return self.snapshot.terms

    @property
    def factpacks(self):
        return self.snapshot.factpacks


# The request's active ReadView is context-local, never instance state.  A
# mutable ``self._snapshot`` shared across threads let one request overwrite
# another's fixed snapshot (Astra P02/D02); a ContextVar keeps each request —
# and each thread — pinned to exactly its own view.
_request_state: "ContextVar[Optional[ReadView]]" = ContextVar(
    "sekaisync_active_readview", default=None
)


def _active_view() -> "Optional[ReadView]":
    return _request_state.get()


def request_scoped(method: Callable) -> Callable:
    """Run a public query method inside the request's ReadView when one is
    active.

    With a view, the method sees the request's fixed revision: ``self.*``
    reads route to the snapshot's collections and SQL helpers resolve to the
    view's connection, so a response cannot mix generations.  Without one
    (direct callers, write pipelines) the method behaves exactly as before.
    """

    @wraps(method)
    def wrapper(self: "SekaiSyncCore", *args, **kwargs):
        with self.request_view():
            return copy.deepcopy(method(self, *args, **kwargs))

    return wrapper



class SekaiSyncCore:
    #: Bound on cached aggregate keys.  Aggregates are a handful of fixed
    #: shapes (status/trust/progress:<regions>), so a small LRU is plenty and
    #: an unbounded dict would be an unbounded memory claim.
    _cache_max_entries = 64
    #: A serialized result above this is not worth caching.
    _cache_max_bytes = 512 * 1024

    def __init__(self, store_root: Path, *, runtime: "Optional[RuntimeContext]" = None):
        """Open a Core over a store.

        ``runtime`` is optional: pure local reads need no external
        configuration, and requiring one would break every local caller. Pass
        it when the Core will perform network work, so those calls read their
        endpoints from the runtime instead of the process-global snapshot
        (Astra P14/D14). A network method invoked without one reports that
        clearly rather than silently using another caller's configuration.
        """
        self.store_root = store_root
        self.runtime = runtime
        dbstore.ensure_store(store_root)
        self._registry = dbstore.load_entities(store_root)
        self._glossary = dbstore.load_glossary_terms(store_root)
        self._factpacks = load_fact_packs(factpack_path(store_root, "en"))
        # Light evidence (no sentence bodies): server queries only need
        # references; write-side pipelines reload with sentences on demand.
        self._terms = dbstore.load_terms_records(store_root)
        # Aggregate-result cache for status/progress/trust_summary on
        # long-lived HTTP/MCP processes. Invalidated by (a) any change to
        # the in-memory datasets (version bump) and (b) the on-disk
        # signature of the files those aggregates read.
        self._data_version = 0
        self._result_cache: "OrderedDict[str, tuple[tuple, dict, int]]" = OrderedDict()
        self._cache_lock = threading.Lock()
        # Parsed SQL projections keyed by (committed revision, data version):
        # a long-lived server re-serves the same parsed collections to every
        # request that pins the same revision instead of re-reading ~70k
        # entities from SQLite per request (0.4.0-alpha regression). Two
        # revisions cover a reader straddling one concurrent sync.
        self._snapshot_cache: "OrderedDict[tuple[int, int], CoreSnapshot]" = OrderedDict()
        self._snapshot_cache_max = 2
        # Fact packs are an external file not atomic with the SQL revision,
        # so they are re-read per request — but keyed by file signature so an
        # unchanged pack file costs one stat() instead of a full parse.
        self._factpack_sig: "Optional[tuple[int, int]]" = None
        self._factpacks_loaded: "Optional[tuple]" = None

    def _bump_data_version(self) -> None:
        with self._cache_lock:
            self._data_version += 1
            self._result_cache.clear()
            self._snapshot_cache.clear()

    def _disk_signature(self) -> tuple:
        """Signature of on-disk inputs feeding the aggregate endpoints.

        After the SQLite migration the DB file carries the kb state; the
        raw region master tables, news JSON, freshness and consent files
        remain external inputs. Disappeared files compare unequal to any
        present-file signature, so deletions invalidate.
        """
        paths: list[Path] = [
            db_path(self.store_root),
            web_consent_path(self.store_root),
            freshness_path(self.store_root),
            db_path(self.store_root).with_name(db_path(self.store_root).name + "-wal"),
        ]
        for region in REGIONS:
            base = region_master_dir(self.store_root, region)
            if base.exists():
                paths.extend(sorted(base.rglob("*.json")))
        news_root = news_dir(self.store_root)
        if news_root.exists():
            paths.extend(sorted(news_root.glob("*.json")))
        signature = []
        for path in paths:
            try:
                st = path.stat()
            except OSError:
                signature.append((str(path), None))
            else:
                signature.append((str(path), st.st_mtime_ns, st.st_size))
        return tuple(signature)

    def _cached_aggregate(
        self,
        key: str,
        compute: Callable[[], dict],
        view: "Optional[ReadView]" = None,
        *, _pass_view: bool = False,
    ) -> dict:
        """Memoize an aggregate against the revision it was computed from.

        The version is captured **before** ``compute()`` runs and re-checked
        **after** it returns.  Capturing it afterwards — which is what this
        used to do — files a computation that began before a write under the
        post-write version, so the stale answer is then served as current for
        as long as that version stands.  Astra D02; the race is reproduced
        deterministically in ``tests/test_core_snapshot.py``.

        Read-only callers should pass the request's ``view`` so the aggregate
        is computed from that request's fixed snapshot rather than from
        whatever ``self.registry`` holds at that instant.
        """
        active = view or _active_view()
        if active is None or active.core is not self:
            with self.request_view() as request:
                return self._cached_aggregate(key, compute, request, _pass_view=view is not None)
        version_at_start = self._data_version
        stamp = (active.revision, version_at_start)
        cache_key = (key, stamp)
        with self._cache_lock:
            hit = self._result_cache.get(cache_key)
            if hit is not None:
                self._result_cache.move_to_end(cache_key)
                return _detached(hit[1])
        result = compute(active) if _pass_view else compute()
        with self._cache_lock:
            if self._data_version == version_at_start and _serialized_size(result) <= self._cache_max_bytes:
                self._result_cache[cache_key] = (stamp, _detached(result), 0)
                self._result_cache.move_to_end(cache_key)
                while len(self._result_cache) > self._cache_max_entries:
                    self._result_cache.popitem(last=False)
        return _detached(result)

    def refresh(self) -> dict:
        """Reload registry/glossary/factpacks/terms from disk.

        Long-lived HTTP/MCP processes keep a Core instance cached in memory;
        after an external ``sync`` / ``rebuild-indexes`` run the in-memory
        copies are stale.  Calling this makes the next query see the latest
        facts without restarting the server.

        This forces a reload even when the revision is unchanged, for callers
        that know an external update happened.
        """
        dbstore.ensure_store(self.store_root)
        self._bump_data_version()
        with self.request_view():
            return {"registry": len(self.registry), "glossary": len(self.glossary),
                    "factpacks": len(self.factpacks), "terms": len(self.terms)}

    def current_revision(self) -> int:
        """The store's committed revision, read from meta.

        Distinct from ``_data_version``, which counts in-process reloads:
        this one is the cross-process write generation.
        """
        with self.request_view() as view:
            return view.revision

    @contextlib.contextmanager
    def request_view(self) -> Iterator["ReadView"]:
        """Fix one read snapshot for the duration of a request.

        Opens a read-only connection, begins a deferred transaction and reads
        the revision, which pins the SQLite snapshot: every later read on this
        connection — including page detail lookups and term evidence — sees
        that same generation even if a writer commits meanwhile.  Without it a
        single response could mix two generations (Astra D02/P02).

        The SQL domains (registry / glossary / terms) are loaded **inside that
        one read transaction**, so the in-memory projections belong to the
        same committed revision the connection is pinned to.  The view is
        published through a ContextVar — context-local, never a mutable
        ``self._snapshot`` — so overlapping requests (including nested ones
        and requests on other threads) cannot see or restore each other's
        snapshot (Astra P02/D02).

        Read-only callers should run their queries inside ``with``; SQL
        helpers invoked in the block resolve to the view's connection and
        snapshot collections.
        """
        active = _active_view()
        if active is not None and active.core is self:
            yield active
            return
        path = db_path(self.store_root).resolve()
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=60.0)
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("BEGIN")
            with dbstore.read_connection(self.store_root, conn):
                snapshot = self.load_snapshot(conn)
                view = ReadView(core=self, conn=conn, snapshot=snapshot)
                token = _request_state.set(view)
                try:
                    yield view
                finally:
                    _request_state.reset(token)
        finally:
            conn.rollback()
            conn.close()

    def _load_factpacks(self) -> tuple:
        """Persisted fact-pack inventory, re-checked per request.

        This external file is not atomic with the SQL revision — a pack
        rebuild commits no revision bump — so a cached snapshot must never
        pin it.  The file signature keeps an unchanged pack file at one
        ``stat()`` per request instead of a full parse, while a changed or
        deleted file reloads immediately.
        """
        path = factpack_path(self.store_root, "en")
        try:
            st = path.stat()
            sig: "Optional[tuple[int, int]]" = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        with self._cache_lock:
            if (
                sig is not None
                and self._factpack_sig == sig
                and self._factpacks_loaded is not None
            ):
                return self._factpacks_loaded
        packs = tuple(load_fact_packs(path))
        with self._cache_lock:
            self._factpack_sig = sig
            self._factpacks_loaded = packs
        return packs

    def load_snapshot(self, conn: sqlite3.Connection) -> CoreSnapshot:
        """Load every SQL projection from the transaction pinned by meta.

        The registry/glossary/terms projections are cached per (committed
        revision, data version): the revision is read first inside this
        request's pinned transaction, so a hit is exactly the data that
        transaction would re-derive, and any committed write — or
        :meth:`refresh`'s forced version bump — misses and reloads under the
        new key.  Collections are therefore shared between requests as
        read-only state; detached copies are made at the hand-out boundaries
        (snapshot attribute access, ``request_scoped`` results).
        """
        revision = dbstore.current_revision(conn)
        key = (revision, self._data_version)
        with self._cache_lock:
            cached = self._snapshot_cache.get(key)
            if cached is not None:
                self._snapshot_cache.move_to_end(key)
        if cached is not None:
            # Fact packs are external and not revision-atomic: always
            # re-checked even on a projection cache hit.  (Built outside the
            # lock — _load_factpacks takes it too, and the lock is not
            # reentrant.)
            return CoreSnapshot(
                revision=revision,
                registry=cached.raw("registry"),
                glossary=cached.raw("glossary"),
                terms=cached.raw("terms"),
                factpacks=self._load_factpacks(),
            )
        snapshot = CoreSnapshot(
            revision=revision,
            registry=tuple(dbstore.load_entities(self.store_root)),
            glossary=tuple(dbstore.load_glossary_terms(self.store_root)),
            terms=tuple(dbstore.load_terms_records(self.store_root)),
            factpacks=self._load_factpacks(),
        )
        with self._cache_lock:
            self._snapshot_cache[key] = snapshot
            self._snapshot_cache.move_to_end(key)
            while len(self._snapshot_cache) > self._snapshot_cache_max:
                self._snapshot_cache.popitem(last=False)
        return snapshot

    @property
    def registry(self):
        """Entity list, or the request snapshot's when a view is active.

        Property, not attribute: inside a request the mutable ``self._registry``
        cache must not be visible — a concurrent reload would replace what the
        request is reading mid-response (Astra P02).

        Inside a view this hands the request methods the snapshot's shared
        collection via ``raw()`` — no per-access deep copy.  Deep copies stay
        at the boundaries callers can mutate through: ``view.registry`` /
        ``view.snapshot.registry`` detach per access, and ``request_scoped``
        copies every returned result.
        """
        view = _active_view()
        if view is not None and view.core is self:
            return view.snapshot.raw("registry")
        return self._registry

    @registry.setter
    def registry(self, value) -> None:
        self._registry = value

    @property
    def glossary(self):
        view = _active_view()
        if view is not None and view.core is self:
            return view.snapshot.raw("glossary")
        return self._glossary

    @glossary.setter
    def glossary(self, value) -> None:
        self._glossary = value

    @property
    def terms(self):
        view = _active_view()
        if view is not None and view.core is self:
            return view.snapshot.raw("terms")
        return self._terms

    @terms.setter
    def terms(self, value) -> None:
        self._terms = value

    @property
    def factpacks(self):
        view = _active_view()
        if view is not None and view.core is self:
            return view.snapshot.raw("factpacks")
        return self._factpacks

    @factpacks.setter
    def factpacks(self, value) -> None:
        self._factpacks = value

    def ready(self) -> bool:
        """Whether the store carries any knowledge at all.

        Deliberately not ``request_scoped``: ``/health`` polls this, and the
        request-scoped version reloaded the whole snapshot per probe — the
        0.4.0-alpha regression that turned every health poll into a ~20s
        full-store read and starved real queries.  The answer is a coarse
        liveness flag, not generation-specific content: the in-memory
        projections answer instantly, and a server started against an empty
        store that was synced afterwards falls back to a cheap SQL existence
        probe.
        """
        if self._registry or self._glossary or self._terms:
            return True
        return dbstore.store_has_knowledge(self.store_root)

    @request_scoped
    def lookup(
        self,
        query: str,
        type: Optional[str] = None,
        region: Optional[str] = None,
        language: Optional[str] = None,
        limit: int = 8,
    ) -> list[dict]:
        results = []
        for entity, score in lookup_entity(
            self.registry,
            query,
            type=type,
            region=region,
            language=language,
            limit=limit,
        ):
            results.append(
                {
                    "id": entity.id,
                    "type": entity.type,
                    "regions": entity.regions,
                    "names": entity.names,
                    "facts": entity.facts,
                    "source": entity.source,
                    "demo": entity.demo,
                    "trust": entity.trust,
                    "score": score,
                }
            )
        return results

    @request_scoped
    def resolve_name(
        self,
        query: str,
        target_language: str = "zh_tw",
        source_language: Optional[str] = None,
        kind: Optional[str] = None,
    ) -> list[dict]:
        return resolve_name(
            self.glossary,
            query,
            target_language=target_language,
            source_language=source_language,
            kind=kind,
        )

    @request_scoped
    def fact_pack(self, entity_id: str, language: str = "en", *,
                  region: Optional[str] = None,
                  as_of: Optional[int] = None) -> Optional[dict]:
        """Compact fact pack; the plain form stays the current snapshot.

        Without ``as_of`` this is the existing "current snapshot" contract the
        consumers already depend on.  Passing ``as_of`` switches to the
        region-aware public-time filter, and then ``region`` is required —
        public times differ per server, so the caller must say which one it is
        asking about rather than letting this pick a default (Astra P05).
        """
        entity = entity_by_id(self.registry, entity_id)
        if entity is None:
            return None
        if as_of is not None:
            if not region:
                raise ValueError(
                    "fact_pack with as_of requires an explicit region: public "
                    "times are per-server, so there is no default to assume"
                )
            return build_fact_pack_at(entity, language=language, region=region, as_of=as_of)
        pack = build_fact_pack(entity, language=language)
        return {
            "entity_id": pack.entity_id,
            "entity_type": pack.entity_type,
            "language": pack.language,
            "trust": entity.trust,
            "text": pack.text,
            "raw_json_tokens": pack.raw_json_tokens,
            "fact_pack_tokens": pack.fact_pack_tokens,
            "token_ratio": round(pack.token_ratio, 3),
        }

    @request_scoped
    def freshness(self) -> dict:
        path = freshness_path(self.store_root)
        if not path.exists():
            return {"ready": self.ready(), "updated_at": None, "regions": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        data["ready"] = self.ready()
        return data

    #: Fields whose value is region-scoped in the master data.  v1 stores a
    #: single ``facts`` dict per entity with no per-region authority, so for a
    #: multi-region entity we cannot prove which region a value came from.
    #: Reporting such a value as definite would invent region authority that
    #: does not exist yet (Astra P03/P04; per-region facts arrive in B6).
    _REGION_SENSITIVE_FIELDS = frozenset(
        {
            "startat", "starttime", "endat", "endtime", "start_at", "end_at",
            "releasedate", "release_date", "availablefrom", "available_from",
        }
    )

    @request_scoped
    def verify_claims(self, claims: list[dict]) -> list[dict]:
        """Verify claims against the local registry with honest semantics.

        Status vocabulary — chosen so that each value means exactly one thing:

        ``matched``      the expected string appears among the matched entity's
                         names.  This is NAME matching, not fact verification.
        ``supported``    an explicit ``field`` was requested, the store has that
                         field, and the values agree after normalization.
        ``conflict``     an explicit ``field`` was requested, the store has that
                         field, and the values differ.  Only ever returned with
                         the stored value attached as evidence.
        ``ambiguous``    the claim matched but no ``expected`` was supplied.
        ``unknown``      the store cannot decide: no matching entity, or the
                         store does not carry the requested field at all.
                         Absence of data is NOT evidence a claim is false.
        ``needs_region_data``
                         the entity is multi-region and the field is
                         region-scoped, so v1 cannot attribute a value to the
                         requested region.

        B1 / Astra P04: free-text that does not match previously returned
        ``conflict``, which asserted a refutation the store cannot support —
        "this string is not in my names" is not "this claim is false".  That
        case is now ``unknown``/``unsupported``.
        """
        output = []
        for claim in claims:
            text = str(claim.get("claim", ""))
            expected = claim.get("expected")
            field = str(claim.get("field", "") or "").strip()
            requested_region = str(claim.get("region", "") or "").strip()
            entity_id = str(claim.get("entity_id", "") or "").strip()

            if entity_id:
                matched_entity = entity_by_id(self.registry, entity_id)
                matches = self._match_rows([matched_entity]) if matched_entity else []
            else:
                matches = self.lookup(text, limit=3)

            if not matches:
                output.append(
                    {
                        "claim": text,
                        "status": "unknown",
                        "reason": "No matching entity found in local registry",
                        "evidence": [],
                        "coverage_note": "This means the local store does not cover this topic, not that the claim is false.",
                    }
                )
                continue

            expected_key = normalize_name(str(expected)) if expected is not None else ""

            # ── explicit field comparison ────────────────────────────
            if field:
                result = self._verify_field_claim(
                    matches, field, expected, expected_key, requested_region, text
                )
                output.append(result)
                continue

            # ── name / free-text comparison ──────────────────────────
            matched_keys = {
                normalize_name(v)
                for m in matches
                for v in m["names"].values()
            }
            if not expected_key:
                status = "ambiguous"
                reason = "No expected value supplied, so nothing was compared."
            elif expected_key in matched_keys:
                status = "matched"
                reason = None
            else:
                # A name search that misses is not a refutation: the store may
                # simply not carry this string in a name slot.
                status = "unknown"
                reason = (
                    "The matched entity exists, but this string is not among "
                    "its known names or fact values. This is a coverage gap, "
                    "not evidence that the claim is false."
                )
            # Build evidence citations
            evidence = []
            for m in matches[:2]:
                ev = {
                    "entity_id": m.get("id", ""),
                    "entity_type": m.get("type", ""),
                    "source": m.get("source", ""),
                    "trust": m.get("trust", ""),
                    "official": m.get("official", False),
                }
                if expected and expected_key in {
                    normalize_name(v) for v in m.get("names", {}).values()
                }:
                    ev["matched_name"] = expected
                evidence.append(ev)
            entry = {
                "claim": text,
                "status": status,
                "method": "name-based string matching against local registry",
                "evidence": evidence,
                "matches": matches,
            }
            if reason:
                entry["reason"] = reason
            output.append(entry)
        return output

    def _match_rows(self, entities: list) -> list[dict]:
        """Normalize registry entities into lookup-shaped rows."""
        rows = []
        for entity in entities:
            if entity is None:
                continue
            rows.append(
                {
                    "id": entity.id,
                    "type": entity.type,
                    "regions": list(entity.regions or []),
                    "names": dict(entity.names),
                    "facts": dict(entity.facts or {}),
                    "region_facts": copy.deepcopy(entity.region_facts),
                    "source": entity.source,
                    "trust": entity.trust,
                    "official": entity.source.startswith(("master_db", "official")),
                }
            )
        return rows

    def _verify_field_claim(
        self,
        matches: list[dict],
        field: str,
        expected: str,
        expected_key: str,
        requested_region: str,
        text: str,
    ) -> dict:
        """Compare one explicit field, refusing to answer beyond the data.

        Only fields the store actually carries can produce ``supported`` or
        ``conflict``.  A field the store does not carry is ``unknown`` — the
        danger this replaces was reporting a mismatch against an absent field as
        a refutation.
        """
        if len(matches) != 1:
            return {"claim": text, "status": "ambiguous", "field": field,
                    "reason": "More than one entity matches; supply entity_id.", "evidence": []}
        row = copy.deepcopy(matches[0])
        field_key = field.strip()
        facts = row.get("facts", {})
        entity = entity_by_id(self.registry, row["id"])
        if requested_region or (entity and entity.region_facts):
            from sekaisync.regions import entity_for_region
            selected = entity_for_region(entity, requested_region or None)
            facts = selected["facts"]
            row["facts"] = facts
            row["region"] = requested_region or None
            row["source"] = selected.get("source") or ""
            if selected["coverage"] != "available":
                return {"claim": text, "status": "unknown", "field": field,
                        "region": requested_region or None, "coverage": selected["coverage"],
                        "reason": "No trustworthy facts for the requested region.", "evidence": []}
        elif field_key.lower() in self._REGION_SENSITIVE_FIELDS and len(row.get("regions", [])) > 1:
            return {"claim": text, "status": "needs_region_data", "field": field,
                    "reason": "Legacy facts have no trustworthy regional attribution.", "evidence": []}
        if expected is None or expected == "":
            return {"claim": text, "status": "unknown", "field": field,
                    "reason": "No expected value supplied.", "evidence": []}

        stored_key = None
        for candidate in facts:
            if candidate.lower() == field_key.lower() or normalize_name(candidate) == normalize_name(field_key):
                stored_key = candidate
                break

        if stored_key is None:
            return {
                "claim": text,
                "status": "unknown",
                "field": field,
                "reason": (
                    f"The local store has no '{field}' field for this entity, "
                    "so the claim can be neither confirmed nor refuted."
                ),
                "supported_fields": sorted(facts.keys()),
                "evidence": [],
                "matches": matches,
            }

        stored_value = facts[stored_key]
        if stored_value is None:
            return {"claim": text, "status": "unknown", "field": field,
                    "reason": "The stored value is null.", "evidence": []}
        if isinstance(stored_value, bool):
            equal = (isinstance(expected, bool) and stored_value == expected) or (
                isinstance(expected, str) and expected.strip().lower() == str(stored_value).lower())
        elif isinstance(stored_value, (int, float)):
            try:
                from decimal import Decimal, InvalidOperation
                equal = not isinstance(expected, bool) and Decimal(str(stored_value)) == Decimal(str(expected))
            except InvalidOperation:
                equal = False
        elif isinstance(stored_value, (dict, list)):
            candidate = expected
            if isinstance(candidate, str):
                try:
                    candidate = json.loads(candidate)
                except ValueError:
                    pass
            equal = json.dumps(stored_value, sort_keys=True) == json.dumps(candidate, sort_keys=True)
        else:
            equal = normalize_name(str(stored_value)) == expected_key
        if equal:
            return {
                "claim": text,
                "status": "supported",
                "field": stored_key,
                "method": "explicit field comparison against local registry",
                "evidence": [self._field_evidence(row, stored_key, stored_value)],
                "matches": matches,
            }

        return {
            "claim": text,
            "status": "conflict",
            "field": stored_key,
            "method": "explicit field comparison against local registry",
            "reason": "The stored value differs from the expected value.",
            "expected": expected,
            "actual": stored_value,
            "evidence": [self._field_evidence(row, stored_key, stored_value)],
            "matches": matches,
        }

    def _field_evidence(self, row: dict, field: str, value: object) -> dict:
        """A citation that keeps provenance with the claim about it."""
        return {
            "entity_id": row.get("id", ""),
            "entity_type": row.get("type", ""),
            "source": row.get("source", ""),
            "trust": row.get("trust", ""),
            "official": row.get("official", False),
            "field": field,
            "region": row.get("region"),
            "value": value,
        }

    @request_scoped
    def web_lookup(
        self,
        query: str,
        source: Optional[str] = None,
        language: Optional[str] = None,
        limit: int = 8,
        include_text: bool = False,
        include_overlay: bool = False,
        source_priority: Optional[tuple[str, ...]] = None,
        kind: Optional[str] = None,
        max_text_chars: int = 0,
    ) -> list[dict]:
        results = web_search(
            self.store_root,
            query,
            source=source,
            language=language,
            limit=limit,
            include_text=include_text,
            include_overlay=include_overlay,
            source_priority=source_priority,
            kind=kind,
            max_text_chars=max_text_chars,
        )
        # P2: token budget — truncate text if max_text_chars > 0
        if max_text_chars > 0:
            for r in results:
                text = r.get("text") or r.get("snippet") or ""
                if len(text) > max_text_chars:
                    if "text" in r:
                        r["text"] = r["text"][:max_text_chars] + "…"
                    elif "snippet" in r:
                        r["snippet"] = r["snippet"][:max_text_chars] + "…"
        return results

    @request_scoped
    def web_browse(
        self,
        source: Optional[str] = None,
        language: Optional[str] = None,
        kind: Optional[str] = None,
        limit: int = 50,
        include_text: bool = False,
        include_overlay: bool = False,
        source_priority: Optional[tuple[str, ...]] = None,
    ) -> list[dict]:
        return web_browse(
            self.store_root,
            source=source,
            language=language,
            kind=kind,
            limit=limit,
            include_text=include_text,
            include_overlay=include_overlay,
            source_priority=source_priority,
        )

    def commit_term_slots(self, decisions, *, expected_revision: int,
                          records=(), evidence_by_id=None, verifier=None) -> dict:
        from sekaisync.term_slots import commit_slot_decisions

        return commit_slot_decisions(
            self.store_root, decisions, expected_revision=expected_revision,
            records=records, evidence_by_id=evidence_by_id, verifier=verifier,
        )

    @request_scoped
    def term_lookup(
        self,
        query: str,
        source_language: Optional[str] = None,
        languages: Optional[list[str]] = None,
        limit: int = 8,
        tag: Optional[str] = None,
        sort: str = "score",
    ) -> list[dict]:
        results = lookup_terms(
            self.terms,
            query,
            source_language=source_language,
            languages=languages,
            limit=limit,
            tag=tag,
            sort=sort,
        )
        from sekaisync.occurrence_store import _query_relations, _query_pages
        from sekaisync.termindex import _occurrence_lookup, _term_language

        with dbstore.connect(self.store_root) as conn:
            known, relations = _query_relations(conn, query, source_language=source_language)
            if known:
                # A known scoped subject must not revive stale flattened names.
                results = [result for result in results if not (
                    result.get("canonical") == query.strip()
                    and (not source_language or _term_language(result.get("source_language", ""))
                         == _term_language(source_language))
                    or any(value == query.strip() and (
                        not source_language or _term_language(language) == _term_language(source_language))
                        for language, value in result.get("names", {}).items()))]
                results.extend(_occurrence_lookup(relations, query, source_language, languages, tag,
                                                  _query_pages(conn, relations)))
                results.sort(key=lambda result: (result.get("weight", 0), result.get("score", 0))
                             if sort == "weight" else (result.get("score", 0),), reverse=True)
                results = results[:limit]
        # Server terms are loaded light (no sentence bodies); enrich just the
        # returned hits so evidence sentences stay in the query output.
        evidence = dbstore.evidence_for_ids(self.store_root, [r.get("id", "") for r in results])
        for r in results:
            if r.get("id") in evidence:
                r["evidence"] = evidence[r["id"]]
        return results

    @request_scoped
    def term_penetrate(
        self,
        query: str,
        story_key: Optional[str] = None,
        languages: Optional[list[str]] = None,
    ) -> Optional[dict]:
        from sekaisync.termindex import load_pages, term_penetrate, _occurrence_penetrate
        from sekaisync.occurrence_store import _query_relations, _query_pages

        with dbstore.connect(self.store_root) as conn:
            known, relations = _query_relations(conn, query, story_key=story_key)
            if known:
                pages = _query_pages(conn, relations)
                _, result = _occurrence_penetrate(relations, query, story_key, languages, pages)
                return result
        pages = load_pages(self.store_root)
        return term_penetrate(
            self.terms,
            query,
            story_key=story_key,
            languages=languages,
            pages=pages,
        )

    @request_scoped
    def tag_clouds(self) -> dict:
        from sekaisync.termindex import build_tag_clouds

        pages = dbstore.load_web_index_rows(self.store_root)
        return build_tag_clouds(self.terms, pages=pages)

    @request_scoped
    def term_status(self) -> dict:
        return dbstore.term_status_from_db(self.store_root)

    def event_alias(
        self,
        query: Optional[str] = None,
        regions: Optional[list[str]] = None,
        list_all: bool = False,
    ) -> Optional[dict]:
        from sekaisync.eventalias import build_event_alias_map, resolve_event_alias

        if list_all:
            return build_event_alias_map(self.store_root, regions=regions)
        if not query:
            return None
        return resolve_event_alias(self.store_root, query, regions=regions)

    def worldlink(
        self,
        query: Optional[str] = None,
        regions: Optional[list[str]] = None,
        list_all: bool = False,
    ) -> Optional[dict]:
        from sekaisync.worldlink import build_wl_map, resolve_wl

        if list_all:
            return build_wl_map(self.store_root, regions=regions)
        if not query:
            return None
        return resolve_wl(self.store_root, query, regions=regions)

    def activity(
        self,
        query: str,
        regions: Optional[list[str]] = None,
    ) -> Optional[dict]:
        """Resolve a shorthand to a numbered activity (WL first, then box)."""
        from sekaisync.eventalias import resolve_activity

        return resolve_activity(self.store_root, query, regions=regions)
    @contextlib.contextmanager
    def _runtime_scope(self, what: str):
        """Endpoints for one networked call, from this Core's runtime.

        Astra P14/D14: a networked method used to install its configuration in
        the process-global snapshot, so two Cores with different settings
        overwrote each other's endpoints.  With a runtime the call scopes those
        endpoints to itself and restores them afterwards; without one it fails
        with an actionable error instead of borrowing whatever the global
        snapshot happens to hold.
        """
        from sekaisync.runtime import require_endpoints

        require_endpoints(self.runtime, what=f"Core.{what}()")
        with self.runtime.activate():
            yield

    def event_check(
        self,
        regions: Optional[list[str]] = None,
        timeout: int = 30,
        fetcher=None,
        master_base: Optional[str] = None,
    ) -> dict:
        from sekaisync.event_detection import check_events

        with self._runtime_scope("event_check"):
            return check_events(self.store_root, regions=regions, fetcher=fetcher, timeout=timeout)

    def event_archive(
        self,
        regions: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> dict:
        from sekaisync.event_detection import list_events

        return list_events(self.store_root, regions=regions, limit=limit)
    def progress(
        self,
        regions: Optional[list[str]] = None,
        live: bool = False,
        fetcher=None,
        master_base: Optional[str] = None,
    ) -> dict:
        from sekaisync.progress import compute_progress

        if live or fetcher is not None or master_base:
            # A network-driven or caller-instrumented run reads endpoints from
            # this Core's runtime instead of replacing the process-global
            # snapshot, and never gets cached.
            with self._runtime_scope("progress"):
                return compute_progress(
                    self.store_root,
                    regions=regions,
                    live=live,
                    fetcher=fetcher,
                )
        key_regions = tuple(regions) if regions is not None else None

        def _compute() -> dict:
            return compute_progress(
                self.store_root,
                regions=regions,
                live=False,
                fetcher=None,
            )

        return self._cached_aggregate(f"progress:{key_regions}", _compute)

    @request_scoped
    def trust_summary(self) -> dict:
        def _compute() -> dict:
            from sekaisync.trust import TRUST_LEVELS, trust_for_page

            counts = {
                level: {"registry": 0, "glossary": 0, "terms": 0, "web": 0, "auxiliary": 0}
                for level in TRUST_LEVELS
            }
            for entity in self.registry:
                level = entity.trust.upper()
                if level in counts:
                    counts[level]["registry"] += 1
            for term in self.glossary:
                level = term.trust.upper()
                if level in counts:
                    counts[level]["glossary"] += 1
            for term in self.terms:
                level = term.trust.upper()
                if level in counts:
                    counts[level]["terms"] += 1
            # Trust depends only on the grouping columns, so classify once per
            # bucket and multiply.  The previous per-row loop over 752k pages
            # was the single largest cost in status() (Astra D06).
            web_total = 0
            auxiliary_total = 0
            for bucket in dbstore.web_trust_buckets(self.store_root):
                count = int(bucket.get("count") or 0)
                if count <= 0:
                    continue
                level = trust_for_page(bucket).upper()
                if level not in counts:
                    continue
                if is_auxiliary_page(bucket):
                    counts[level]["auxiliary"] += count
                    auxiliary_total += count
                else:
                    counts[level]["web"] += count
                    web_total += count
            return {
                "levels": counts,
                "totals": {
                    "registry": len(self.registry),
                    "glossary": len(self.glossary),
                    "terms": len(self.terms),
                    "web": web_total,
                    "auxiliary": auxiliary_total,
                },
            }

        return self._cached_aggregate("trust", _compute)

    @request_scoped
    def integrity(self, limit: int = 20) -> dict:
        from sekaisync.integrity import run_integrity_check

        return run_integrity_check(self.store_root, limit=limit)

    @request_scoped
    def news(self, limit: int = 100, language: Optional[str] = None,
             tag: Optional[str] = None, body: Optional[bool] = None) -> dict:
        from sekaisync.news import filter_news, load_news, news_summary

        all_records = load_news(self.store_root)
        records = filter_news(all_records, language=language, tag=tag, body=body)
        return {
            **news_summary(self.store_root, records=all_records),
            "matched": len(records),
            "items": records[:limit],
        }

    @request_scoped
    def status(self) -> dict:
        def _compute() -> dict:
            from sekaisync.webindex import load_web_category_counts

            consent_path = web_consent_path(self.store_root)
            consent = False
            if consent_path.exists():
                data = json.loads(consent_path.read_text(encoding="utf-8"))
                consent = bool(data)
            sources = dbstore.web_source_counts(self.store_root)
            web_status = {
                "enabled": bool(sources) and consent,
                "consent": consent,
                "sources": sources,
                "category_counts": load_web_category_counts(self.store_root),
                "auxiliary": auxiliary_page_summary(self.store_root),
            }
            return {
                "store": str(self.store_root.resolve()),
                "master": self.store_stats(),
                "web": web_status,
                "terms": self.term_status(),
                "trust": self.trust_summary(),
                "progress": self.progress(),
                "news": self.news(),
                "freshness": self.freshness(),
            }

        return self._cached_aggregate("status", _compute)

    def data_gaps(self) -> list[dict]:
        """Known data source limitations, first-class visible to agents."""
        return [
            {
                "domain": "home_line",
                "scope": "all regions",
                "severity": "partial",
                "description": "~52% of characterArchiveVoices have empty displayPhrase; source provides no text",
                "agent_guidance": "If a character's home line is not found, the source omits it — say 'not covered'.",
            },
            {
                "domain": "mysekai_overseas",
                "scope": "en/tc/kr/cn",
                "severity": "missing",
                "description": "Overseas asset buckets have no mysekai/talk/ Lua; only JP has MySekai dialogue",
                "agent_guidance": "MySekai dialogue is JP-only; overseas has no text to crawl.",
            },
            {
                "domain": "special_story_countdown",
                "scope": "all regions",
                "severity": "partial",
                "description": "5th/6th/7th category countdown videos have no text (90-96%)",
                "agent_guidance": "Countdown special stories are video-only.",
            },
            {
                "domain": "overseas_gacha_history",
                "scope": "cn/tc/kr",
                "severity": "partial",
                "description": "CN(87)/TC(71)/KR(82) gachas only have recent records vs JP(986)/EN(931)",
                "agent_guidance": "Historical gacha data for CN/TC/KR is incomplete at source.",
            },
            {
                "domain": "story_full_text_coverage",
                "scope": "all regions",
                "severity": "partial",
                "description": "Fact layer 100% but text layer ~66% (JP 81%); gaps are source limitations",
                "agent_guidance": "Not all stories have community-translated text available.",
            },
            {
                "domain": "overseas_character_missions",
                "scope": "cn",
                "severity": "missing",
                "description": "characterMissions table absent in the CN master repo (present in jp/en/tc/kr); CN character mission facts are not covered",
                "agent_guidance": "Character mission details for CN are not available from the local master data.",
            },
            {
                # Astra B2/P13/P17. raw/ master tables and kb/news both publish
                # as immutable generations with a single active pointer committed
                # atomically (raw: indexes+pointer; news: manifest+pointer).
                # A request-pinned reader resolves both pointers through its
                # bound connection, so a mixed generation across domains still
                # remains possible only for callers that read outside one
                # request snapshot — narrowed, not closed.
                "domain": "multi_file_generation_consistency",
                "scope": "raw master tables and kb/news are generation-pinned",
                "severity": "partial",
                "description": (
                    "raw/ region master tables and kb/news publish as immutable "
                    "generations with an active pointer committed atomically "
                    "with the derived state; request-pinned reads resolve the "
                    "pointer through the request's snapshot connection. An "
                    "aggregate assembled from reads outside a single request "
                    "snapshot can still mix content from before and after a "
                    "concurrent sync."
                ),
                "agent_guidance": (
                    "Master-data and news reads are pinned to a generation and "
                    "self-consistent within one request snapshot. Do not present "
                    "an aggregate assembled from separate unpinned reads as a "
                    "single consistent snapshot."
                ),
            },
        ]

    @request_scoped
    def query(
        self,
        query: str,
        type: Optional[str] = None,
        region: Optional[str] = None,
        language: Optional[str] = None,
        limit: int = 8,
        include_web: bool = True,
        include_overlay: bool = False,
    ) -> dict:
        metadata = self.lookup(
            query,
            type=type,
            region=region,
            language=language,
            limit=limit,
        )
        web = self.web_lookup(
            query,
            language=language,
            limit=limit,
            include_overlay=include_overlay,
        ) if include_web else []
        terms = self.term_lookup(
            query,
            source_language=language,
            languages=[language] if language else None,
            limit=limit,
        )
        # Unified provenance: stamp source_layer on every result so the Agent
        # can distinguish official master DB from community text from term
        # extraction without guessing.
        for m in metadata:
            m["source_layer"] = "master_db"
            m["source_description"] = "Official master database entity"
        for w in web:
            w["source_layer"] = "community_text"
            w["source_description"] = "Crawled community story text"
        for t in terms:
            t["source_layer"] = "term_extraction"
            t["source_description"] = "Extracted terminology index"
        return {
            "query": query,
            "provenance_guide": {
                "source_layers": {
                    "master_db": "Official game data (trust A) — authoritative",
                    "community_text": "Crawled fan translation (trust B/C) — may differ from official",
                    "term_extraction": "Extracted terminology (trust varies) — cross-language alignment",
                },
                "trust_levels": {
                    "A": "Official master DB",
                    "B": "Official mirror / curated community",
                    "C": "Derived / community translation",
                    "D": "External / unverified",
                },
                "note": "When sources conflict, master_db > community_text > term_extraction.",
            },
            "metadata": metadata,
            "web": web,
            "terms": terms,
        }

    @request_scoped
    def store_stats(self) -> dict:
        return {
            "entities": len(self.registry),
            "glossary_terms": len(self.glossary),
            "fact_packs": len(self.factpacks),
            "terms": len(self.terms),
            "ready": self.ready(),
        }

    def merge_zhfirst_terms(self) -> dict:
        """Merge cached zh-first pipeline output into self.terms.

        Reads the cache produced by `terms zhfirst` (or runs the pipeline
        if no cache exists). Converts ZhFirstTerm → TermRecord, deduplicates
        by canonical against existing terms (official inheritance wins)."""
        from sekaisync.layout import glossary_path as _gp, terms_path, cache_dir
        from sekaisync.termindex import (
            load_glossary, make_term_id, TermRecord, now_iso, save_terms,
        )

        cache_file = cache_dir(self.store_root) / "zhfirst_terms.json"
        zh_data: list[dict] = []
        if cache_file.exists():
            import json as _json
            zh_data = _json.loads(cache_file.read_text(encoding="utf-8"))
        else:
            # Run pipeline and cache
            from sekaisync.termindex import load_pages
            from sekaisync.zhfirst import extract_terms_zhfirst
            glossary = load_glossary(_gp(self.store_root))
            pages = load_pages(self.store_root)
            zh_terms = extract_terms_zhfirst(
                pages, ["ja", "en", "zh_tw", "ko"], glossary, do_align=False,
            )
            zh_data = [t.to_dict() for t in zh_terms]
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(
                json.dumps(zh_data, ensure_ascii=False), encoding="utf-8"
            )

        if dbstore.inspect_schema(self.store_root).version in {"2", "3"}:
            from sekaisync.term_slots import ingest_legacy_terms
            from sekaisync.termindex import term_to_dict

            with dbstore.connect(self.store_root) as conn:
                revision = dbstore.current_revision(conn)
            existing_by_canonical = {t.canonical: t for t in self.terms}
            records = []
            for item in zh_data:
                canonical = str(item.get("canonical", ""))
                if len(canonical) < 2:
                    continue
                existing = existing_by_canonical.get(canonical)
                record = dict(item, id=existing.id if existing else make_term_id("zh_hans", canonical),
                              source_language="zh_hans", source="zhfirst")
                records.append(record)
            return ingest_legacy_terms(self.store_root, records, expected_revision=revision)

        existing_by_canonical = {t.canonical: t for t in self.terms}
        added = 0
        merged = 0
        official_inherited = 0
        for zd in zh_data:
            canon = str(zd.get("canonical", ""))
            if len(canon) < 2:
                continue
            names = {k: str(v) for k, v in zd.get("names", {}).items() if v}
            is_official = bool(zd.get("official", False))
            existing = existing_by_canonical.get(canon)
            if existing is not None:
                for lang, name in names.items():
                    if name and not existing.names.get(lang):
                        existing.names[lang] = name
                        merged += 1
                continue
            rec = TermRecord(
                id=make_term_id("zh_hans", canon),
                canonical=canon,
                source_language="zh_hans",
                kind="term",
                names=names,
                evidence=[],
                official=is_official,
                source="glossary" if is_official else "zhfirst",
                created_at=now_iso(),
                confidence=0.9 if is_official else 0.7,
                trust="A" if is_official else "C",
                tags=list(zd.get("tags", ["other"])),
                everyday=bool(zd.get("everyday", False)),
            )
            self.terms.append(rec)
            existing_by_canonical[rec.canonical] = rec
            added += 1
            if is_official:
                official_inherited += 1

        # Astra P01: names update + evidence preserve is exactly `upsert_terms`
        # with no evidence_updates. The old `save_terms_records(replace_evidence=
        # False)` relied on the ambiguous flag; the new call states the intent.
        dbstore.upsert_terms(self.store_root, self.terms)
        self._bump_data_version()
        return {
            "zhfirst_candidates": len(zh_data),
            "added_new": added,
            "merged_names_into_existing": merged,
            "official_inherited": official_inherited,
            "final_total": len(self.terms),
            "cache": str(cache_file),
            "path": str(dbstore.db_file(self.store_root)),
        }


