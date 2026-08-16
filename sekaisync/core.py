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
                        "matches": [],
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
                status = "verified"
            elif expected_key and expected_key not in matched_keys:
                status = "conflict"
            else:
                status = "ambiguous"
            output.append(
                {
                    "claim": text,
                    "status": status,
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
    ) -> list[dict]:
        return web_search(
            self.store_root,
            query,
            source=source,
            language=language,
            limit=limit,
            include_text=include_text,
            include_overlay=include_overlay,
            source_priority=source_priority,
        )

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
    ) -> list[dict]:
        return lookup_terms(
            self.terms,
            query,
            source_language=source_language,
            languages=languages,
            limit=limit,
        )

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
        return {
            "query": query,
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


