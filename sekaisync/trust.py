from __future__ import annotations

from typing import Any

from sekaisync.sources import (
    BACKEND_MOESEKAI,
    BACKEND_SEKAI_VIEWER,
    SOURCE_MS,
    SOURCE_MS_TRANSLATION,
    SOURCE_SV,
    SOURCE_SV_I18N,
    normalize_source_id,
)


TRUST_LEVELS = ("A", "B", "C", "D")
TRUST_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1}
TRUST_LABELS = {
    "A": "Official",
    "B": "Official mirror / curated",
    "C": "Derived / AIGC / community translation",
    "D": "External / unverified / fanon",
}

_LOW_TRUST_SOURCES = {"fandom", "reddit", "wiki", "bilibili", "fanon", "external", "community"}
_AIGC_SOURCES = {"llm", "local", "heuristic", "aigc", "translation_llm"}

# Curated mirror / open-source CMS backends (B by default).
_CURATED_SOURCES = {
    SOURCE_MS,
    SOURCE_SV,
    # Legacy IDs kept for stores crawled before the multi-site rename.
    "altsource",
    "sekai_viewer",
    "metadata",
    "storage",
    "translation",
}


def trust_for_source(
    source: str,
    *,
    kind: str = "",
    derived: bool = False,
    official: bool = False,
    translation_source: str = "",
    demo: bool = False,
) -> str:
    if demo:
        return "D"
    if derived:
        return "C"
    key = source.strip().lower()
    if key in _LOW_TRUST_SOURCES or source.startswith(("http://", "https://")):
        return "D"
    if key in _AIGC_SOURCES:
        return "C"
    normalized = normalize_source_id(key)
    if normalized in {SOURCE_MS_TRANSLATION, SOURCE_SV_I18N} or key in {
        "altsource_translation",
        "sekai_viewer_translation",
        "sekai_viewer_i18n",
    }:
        return "B" if translation_source in {"official", "official_cn", "curated"} else "C"
    if key.startswith(
        ("master_db", "official", "seed", "strapi", "announcement")
    ):
        return "A"
    if key in _CURATED_SOURCES or normalized in _CURATED_SOURCES:
        return "B"
    if kind in {"event_story", "unit_story", "card_story", "special_story", "virtual_live"}:
        return "B"
    if official:
        return "A"
    return "C"


def trust_for_entity(entity: Any) -> str:
    return trust_for_source(
        str(entity.source),
        kind=str(entity.type),
        demo=bool(getattr(entity, "demo", False)),
    )


def trust_for_term(term: Any) -> str:
    return trust_for_source(
        str(getattr(term, "source", "")),
        official=bool(getattr(term, "official", False)),
        demo=bool(getattr(term, "demo", False)),
    )


def trust_for_page(page: dict[str, Any]) -> str:
    # Multi-instance pages carry an explicit backend class so custom
    # instance IDs get the same trust rules as the canonical type IDs.
    source_type = str(page.get("source_type") or "").strip().lower()
    auxiliary = bool(page.get("auxiliary", False) or page.get("overlay", False))
    derived = bool(page.get("derived", False))
    translation_source = str(page.get("translation_source") or "")
    if source_type == BACKEND_SEKAI_VIEWER:
        if auxiliary:
            return (
                "B"
                if translation_source in {"official", "official_cn", "curated"}
                else "C"
            )
        return "C" if derived else "B"
    if source_type == BACKEND_MOESEKAI:
        if auxiliary:
            return "B" if translation_source == "official_cn" else "C"
        return "C" if derived else "B"
    return trust_for_source(
        str(page.get("source", "")),
        kind=str(page.get("kind", "")),
        derived=derived,
        translation_source=translation_source,
    )


def trust_rank(trust: str) -> int:
    return TRUST_ORDER.get(trust.upper(), 0)
