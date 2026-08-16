from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Optional

from sekaisync.models import Entity, GlossaryTerm
from sekaisync.normalize import similarity_score
from sekaisync.trust import trust_for_source


def merge_glossary(entities: Iterable[Entity], seed_terms: Optional[list[dict]] = None) -> list[GlossaryTerm]:
    seed_terms = seed_terms or []
    by_id: dict[str, GlossaryTerm] = {}

    for seed in seed_terms:
        term = GlossaryTerm(
            id=str(seed["id"]),
            kind=str(seed.get("kind", "term")),
            canonical=str(seed.get("canonical", "")),
            names={k: str(v) for k, v in seed.get("names", {}).items() if v},
            official=bool(seed.get("official", False)),
            source=str(seed.get("source", "seed")),
            demo=bool(seed.get("demo", False)),
            trust=str(
                seed.get("trust")
                or trust_for_source(
                    str(seed.get("source", "seed")),
                    official=bool(seed.get("official", False)),
                    demo=bool(seed.get("demo", False)),
                )
            ),
        )
        by_id[term.id] = term

    for entity in entities:
        if not entity.names:
            continue
        existing = by_id.get(entity.id)
        official = entity.source.startswith("master_db") or entity.source.startswith("official")
        if existing is None:
            existing = GlossaryTerm(
                id=entity.id,
                kind=entity.type,
                canonical=entity.canonical_name,
                names=dict(entity.names),
                official=official,
                source=entity.source,
                demo=entity.demo,
                trust=entity.trust or trust_for_source(
                    entity.source,
                    kind=entity.type,
                    demo=entity.demo,
                ),
            )
            by_id[entity.id] = existing
        else:
            for language, name in entity.names.items():
                if not existing.names.get(language):
                    existing.names[language] = name
            if official and not existing.official:
                existing.official = True
                existing.source = entity.source
            if not existing.trust and entity.trust:
                existing.trust = entity.trust
            existing.demo = existing.demo or entity.demo

    return sorted(by_id.values(), key=lambda t: (t.kind, t.id))


def glossary_to_dict(terms: Iterable[GlossaryTerm]) -> list[dict]:
    return [
        {
            "id": term.id,
            "kind": term.kind,
            "canonical": term.canonical,
            "names": term.names,
            "official": term.official,
            "source": term.source,
            "demo": term.demo,
            "trust": term.trust or trust_for_source(
                term.source,
                official=term.official,
                demo=term.demo,
            ),
        }
        for term in terms
    ]


def glossary_from_dict(data: list[dict]) -> list[GlossaryTerm]:
    return [
        GlossaryTerm(
            id=str(item["id"]),
            kind=str(item.get("kind", "term")),
            canonical=str(item.get("canonical", "")),
            names={k: str(v) for k, v in item.get("names", {}).items() if v},
            official=bool(item.get("official", False)),
            source=str(item.get("source", "")),
            demo=bool(item.get("demo", False)),
            trust=str(
                item.get("trust")
                or trust_for_source(
                    str(item.get("source", "")),
                    official=bool(item.get("official", False)),
                    demo=bool(item.get("demo", False)),
                )
            ),
        )
        for item in data
    ]


def save_glossary(terms: Iterable[GlossaryTerm], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(glossary_to_dict(terms), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def load_glossary(path: Path) -> list[GlossaryTerm]:
    if not path.exists():
        return []
    return glossary_from_dict(json.loads(path.read_text(encoding="utf-8")))


def find_terms(
    terms: Iterable[GlossaryTerm],
    query: str,
    kind: Optional[str] = None,
    limit: int = 8,
) -> list[tuple[GlossaryTerm, int]]:
    scored: list[tuple[GlossaryTerm, int]] = []
    for term in terms:
        if kind and term.kind != kind:
            continue
        names = list(term.names.values())
        if term.canonical:
            names.append(term.canonical)
        best = max((similarity_score(query, name) for name in names), default=0)
        if best >= 50:
            scored.append((term, best))
    scored.sort(key=lambda item: item[1], reverse=True)
    return scored[:limit]


def resolve_name(
    terms: Iterable[GlossaryTerm],
    query: str,
    target_language: str,
    source_language: Optional[str] = None,
    kind: Optional[str] = None,
) -> list[dict]:
    matches = find_terms(terms, query, kind=kind, limit=5)
    results = []
    for term, score in matches:
        source_name = term.names.get(source_language, "") if source_language else ""
        target_name = term.names.get(target_language, "")
        results.append(
            {
                "id": term.id,
                "kind": term.kind,
                "canonical": term.canonical,
                "source_name": source_name or term.canonical,
                "target_name": target_name or term.canonical,
                "official": term.official,
                "demo": term.demo,
                "trust": term.trust or trust_for_source(
                    term.source,
                    official=term.official,
                    demo=term.demo,
                ),
                "score": score,
            }
        )
    return results
