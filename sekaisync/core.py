from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Optional

from sekaisync.config import REGIONS
from sekaisync import dbstore
from sekaisync.factpacks import build_fact_pack, load_fact_packs
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


class SekaiSyncCore:
    def __init__(self, store_root: Path):
        self.store_root = store_root
        dbstore.ensure_store(store_root)
        self.registry = dbstore.load_entities(store_root)
        self.glossary = dbstore.load_glossary_terms(store_root)
        self.factpacks = load_fact_packs(factpack_path(store_root, "en"))
        # Light evidence (no sentence bodies): server queries only need
        # references; write-side pipelines reload with sentences on demand.
        self.terms = dbstore.load_terms_records(store_root)
        # Aggregate-result cache for status/progress/trust_summary on
        # long-lived HTTP/MCP processes. Invalidated by (a) any change to
        # the in-memory datasets (version bump) and (b) the on-disk
        # signature of the files those aggregates read.
        self._data_version = 0
        self._result_cache: dict[str, tuple[tuple, dict]] = {}
        self._cache_lock = threading.Lock()

    def _bump_data_version(self) -> None:
        with self._cache_lock:
            self._data_version += 1
            self._result_cache.clear()

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

    def _cached_aggregate(self, key: str, compute: Callable[[], dict]) -> dict:
        disk = self._disk_signature()
        with self._cache_lock:
            hit = self._result_cache.get(key)
            if hit is not None and hit[0] == (self._data_version, disk):
                return hit[1]
        result = compute()
        with self._cache_lock:
            self._result_cache[key] = ((self._data_version, disk), result)
        return result

    def refresh(self) -> dict:
        """Reload registry/glossary/factpacks/terms from disk.

        Long-lived HTTP/MCP processes keep a Core instance cached in memory;
        after an external ``sync`` / ``rebuild-indexes`` run the in-memory
        copies are stale.  Calling this makes the next query see the latest
        facts without restarting the server.
        """
        dbstore.ensure_store(self.store_root)
        self.registry = dbstore.load_entities(self.store_root)
        self.glossary = dbstore.load_glossary_terms(self.store_root)
        self.factpacks = load_fact_packs(factpack_path(self.store_root, "en"))
        self.terms = dbstore.load_terms_records(self.store_root)
        self._bump_data_version()
        return {
            "registry": len(self.registry),
            "glossary": len(self.glossary),
            "factpacks": len(self.factpacks),
            "terms": len(self.terms),
        }

    def ready(self) -> bool:
        return (
            bool(self.registry)
            or bool(self.glossary)
            or bool(self.terms)
        )

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

    def fact_pack(self, entity_id: str, language: str = "en") -> Optional[dict]:
        entity = entity_by_id(self.registry, entity_id)
        if entity is None:
            return None
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
            expected = str(claim.get("expected", ""))
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

            expected_key = normalize_name(expected)

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
                for v in list(m["names"].values()) + [str(v) for v in m["facts"].values() if isinstance(v, (str, int, float))]
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
        row = matches[0]
        field_key = field.strip()
        facts = row.get("facts", {})

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

        if (
            field_key.lower() in self._REGION_SENSITIVE_FIELDS
            and not requested_region
            and len(row.get("regions", []) or []) > 1
        ):
            return {
                "claim": text,
                "status": "needs_region_data",
                "field": field,
                "reason": (
                    "This field is region-scoped and the entity exists in "
                    "multiple regions; without a requested region the stored "
                    "value cannot be attributed to one."
                ),
                "evidence": [self._field_evidence(row, stored_key, None)],
                "matches": matches,
            }

        stored_value = facts[stored_key]
        stored_key_norm = normalize_name(str(stored_value))
        if expected_key and stored_key_norm == expected_key:
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
            "value": value,
        }

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
        )
        # P2: kind filter (event_story / card_story / area_talk / ...)
        if kind:
            results = [r for r in results if r.get("kind") == kind]
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
        # Server terms are loaded light (no sentence bodies); enrich just the
        # returned hits so evidence sentences stay in the query output.
        evidence = dbstore.evidence_for_ids(self.store_root, [r.get("id", "") for r in results])
        for r in results:
            if r.get("id") in evidence:
                r["evidence"] = evidence[r["id"]]
        return results

    def term_penetrate(
        self,
        query: str,
        story_key: Optional[str] = None,
        languages: Optional[list[str]] = None,
    ) -> Optional[dict]:
        from sekaisync.termindex import load_pages, term_penetrate

        pages = load_pages(self.store_root)
        return term_penetrate(
            self.terms,
            query,
            story_key=story_key,
            languages=languages,
            pages=pages,
        )

    def tag_clouds(self) -> dict:
        from sekaisync.termindex import build_tag_clouds

        pages = dbstore.load_web_index_rows(self.store_root)
        return build_tag_clouds(self.terms, pages=pages)

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
    def event_check(
        self,
        regions: Optional[list[str]] = None,
        timeout: int = 30,
        fetcher=None,
        master_base: Optional[str] = None,
    ) -> dict:
        from sekaisync.event_detection import apply_master_base, check_events

        if master_base:
            apply_master_base(master_base)
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
        from sekaisync.progress import apply_master_base, compute_progress

        if master_base:
            apply_master_base(master_base)
        if live or fetcher is not None or master_base:
            # Network-driven or caller-instrumented runs: never cached.
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
            web_total = 0
            auxiliary_total = 0
            for page in load_web_index(self.store_root):
                level = trust_for_page(page).upper()
                if level not in counts:
                    continue
                if is_auxiliary_page(page):
                    counts[level]["auxiliary"] += 1
                    auxiliary_total += 1
                else:
                    counts[level]["web"] += 1
                    web_total += 1
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

    def integrity(self, limit: int = 20) -> dict:
        from sekaisync.integrity import run_integrity_check

        return run_integrity_check(self.store_root, limit=limit)

    def news(self, limit: int = 100, language: Optional[str] = None,
             tag: Optional[str] = None, body: Optional[bool] = None) -> dict:
        from sekaisync.news import filter_news, load_news, news_summary

        records = filter_news(load_news(self.store_root), language=language, tag=tag, body=body)
        return {
            **news_summary(self.store_root),
            "matched": len(records),
            "items": records[:limit],
        }

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
        ]

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

        dbstore.save_terms_records(self.store_root, self.terms, replace_evidence=False)
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


