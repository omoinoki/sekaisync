from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from sekaisync.factpacks import build_fact_pack, load_fact_packs
from sekaisync.glossary import find_terms, load_glossary, resolve_name
from sekaisync.layout import (
    factpack_path,
    freshness_path,
    glossary_path,
    registry_path,
    terms_path,
    web_consent_path,
    web_index_path,
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
        self.registry = load_registry(registry_path(store_root))
        self.glossary = load_glossary(glossary_path(store_root))
        self.factpacks = load_fact_packs(factpack_path(store_root, "en"))
        self.terms = load_terms(terms_path(store_root))

    def refresh(self) -> dict:
        """Reload registry/glossary/factpacks/terms from disk.

        Long-lived HTTP/MCP processes keep a Core instance cached in memory;
        after an external ``sync`` / ``rebuild-indexes`` run the in-memory
        copies are stale.  Calling this makes the next query see the latest
        facts without restarting the server.
        """
        self.registry = load_registry(registry_path(self.store_root))
        self.glossary = load_glossary(glossary_path(self.store_root))
        self.factpacks = load_fact_packs(factpack_path(self.store_root, "en"))
        self.terms = load_terms(terms_path(self.store_root))
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

    def verify_claims(self, claims: list[dict]) -> list[dict]:
        """Verify claims against the local registry with honest semantics.

        Returns matched / conflict / ambiguous / unverified (NOT "verified" —
        string matching is not fact verification). Each result carries
        evidence citations (which entity, which name, what trust level)."""
        output = []
        for claim in claims:
            text = str(claim.get("claim", ""))
            expected = str(claim.get("expected", ""))
            matches = self.lookup(text, limit=3)
            if not matches:
                output.append(
                    {
                        "claim": text,
                        "status": "unverified",
                        "reason": "No matching entity found in local registry",
                        "evidence": [],
                        "coverage_note": "This means the local store does not cover this topic, not that the claim is false.",
                    }
                )
                continue
            expected_key = normalize_name(expected)
            matched_keys = {
                normalize_name(v)
                for m in matches
                for v in list(m["names"].values()) + [str(v) for v in m["facts"].values() if isinstance(v, (str, int, float))]
            }
            if expected_key and expected_key in matched_keys:
                status = "matched"
            elif expected_key and expected_key not in matched_keys:
                status = "conflict"
            else:
                status = "ambiguous"
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
            output.append(
                {
                    "claim": text,
                    "status": status,
                    "method": "name-based string matching against local registry",
                    "evidence": evidence,
                    "matches": matches,
                }
            )
        return output

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
        return lookup_terms(
            self.terms,
            query,
            source_language=source_language,
            languages=languages,
            limit=limit,
            tag=tag,
            sort=sort,
        )

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
        from sekaisync.termindex import build_tag_clouds, load_pages

        pages = load_pages(self.store_root)
        return build_tag_clouds(self.terms, pages=pages)

    def term_status(self) -> dict:
        return term_status(self.terms)

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
        return compute_progress(
            self.store_root,
            regions=regions,
            live=live,
            fetcher=fetcher,
        )

    def trust_summary(self) -> dict:
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

    def integrity(self, limit: int = 20) -> dict:
        from sekaisync.integrity import run_integrity_check

        return run_integrity_check(self.store_root, limit=limit)

    def news(self, limit: int = 100) -> dict:
        from sekaisync.news import load_news, news_summary

        records = load_news(self.store_root)
        return {
            **news_summary(self.store_root),
            "items": records[:limit],
        }

    def status(self) -> dict:
        from sekaisync.webindex import load_web_category_counts

        consent_path = web_consent_path(self.store_root)
        index_path = web_index_path(self.store_root)
        consent = False
        if consent_path.exists():
            data = json.loads(consent_path.read_text(encoding="utf-8"))
            consent = bool(data)
        sources = {}
        if index_path.exists():
            sources = json.loads(index_path.read_text(encoding="utf-8")).get("sources", {})
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

        path = terms_path(self.store_root)
        save_terms(self.terms, path, compact_evidence=True)
        return {
            "zhfirst_candidates": len(zh_data),
            "added_new": added,
            "merged_names_into_existing": merged,
            "official_inherited": official_inherited,
            "final_total": len(self.terms),
            "cache": str(cache_file),
            "path": str(path),
        }


