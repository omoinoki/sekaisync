"""Canonical source IDs for the two open-source story CMS backends.

Both story-text CMS sites are open source, so they share the neutral
``altsource_*`` prefix and are only distinguished by a backend suffix:

- ``altsource_sv``: Sekai Viewer backend (Sekai-World/sekai-viewer)
- ``altsource_ms``: Moesekai mirror backend (formerly the standalone
  ``altsource`` / "Moesekai" crawl)

Each backend also stores auxiliary translation reference pages under its
own auxiliary source ID (``altsource_sv_i18n`` and
``altsource_ms_translation``).  Legacy IDs from earlier releases are
accepted everywhere and normalized to the canonical IDs so old stores,
CLI invocations and stored page records keep working.
"""

from __future__ import annotations

from typing import Iterable, Optional

SOURCE_SV = "altsource_sv"
SOURCE_SV_I18N = "altsource_sv_i18n"
SOURCE_MS = "altsource_ms"
SOURCE_MS_TRANSLATION = "altsource_ms_translation"

# Story-text crawl sources (the two CMS backends).
ALL_SOURCES = (SOURCE_SV, SOURCE_MS)

# Auxiliary translation reference sources stored alongside the crawl.
AUXILIARY_SOURCES = (SOURCE_SV_I18N, SOURCE_MS_TRANSLATION)

# Everything the web index may store under store/web/ (or kb/web/).
ALL_STORED_SOURCES = (SOURCE_SV, SOURCE_SV_I18N, SOURCE_MS, SOURCE_MS_TRANSLATION)

# Old -> canonical source IDs.  Kept for store migration and for accepting
# legacy CLI arguments such as ``crawl --sources altsource,sekai_viewer``.
SOURCE_ALIASES = {
    "sekai_viewer": SOURCE_SV,
    "sekai_viewer_i18n": SOURCE_SV_I18N,
    "altsource": SOURCE_MS,
    "altsource_translation": SOURCE_MS_TRANSLATION,
    "moesekai": SOURCE_MS,
    "moesekai_translation": SOURCE_MS_TRANSLATION,
    SOURCE_SV: SOURCE_SV,
    SOURCE_SV_I18N: SOURCE_SV_I18N,
    SOURCE_MS: SOURCE_MS,
    SOURCE_MS_TRANSLATION: SOURCE_MS_TRANSLATION,
}

# Only the pre-rename legacy IDs (used by the store migration tool).
LEGACY_IDS = {
    "sekai_viewer": SOURCE_SV,
    "sekai_viewer_i18n": SOURCE_SV_I18N,
    "altsource": SOURCE_MS,
    "altsource_translation": SOURCE_MS_TRANSLATION,
}

# Default cross-source preference: earlier entries win.  Sekai Viewer is
# listed first to preserve the historical news merge behavior.
DEFAULT_SOURCE_PRIORITY = (SOURCE_SV, SOURCE_MS)

# Backend adapters referenced by the multi-site settings profile.
BACKEND_SEKAI_VIEWER = "sekai_viewer"
BACKEND_MOESEKAI = "moesekai"

# The two backend classes are open-source data systems, not two concrete
# sites: several instances of each class may be registered in settings.json.
# Canonical type IDs stay as the default instance IDs so existing stores
# keep byte-compatible page IDs and directory names.
KNOWN_BACKENDS = (BACKEND_SEKAI_VIEWER, BACKEND_MOESEKAI)

TYPE_OF_BACKEND = {
    BACKEND_SEKAI_VIEWER: SOURCE_SV,
    BACKEND_MOESEKAI: SOURCE_MS,
}
BACKEND_OF_TYPE = {
    SOURCE_SV: BACKEND_SEKAI_VIEWER,
    SOURCE_MS: BACKEND_MOESEKAI,
}


def type_source_id(backend: str) -> str:
    """Canonical type ID for a backend class ("" when unknown)."""
    return TYPE_OF_BACKEND.get(str(backend or "").strip().lower(), "")


def backend_of_type(value: str) -> str:
    """Backend class for a canonical type ID ("" when unknown)."""
    return BACKEND_OF_TYPE.get(normalize_source_id(value), "")


def auxiliary_source_for_instance(instance_id: str, backend: str) -> str:
    """Auxiliary (translation reference) source ID for a story instance.

    Default instances use the canonical type names, so
    ``auxiliary_source_for_instance("altsource_sv", "sekai_viewer")``
    returns ``altsource_sv_i18n`` — identical to the legacy naming.
    """
    key = str(instance_id or "").strip()
    if not key:
        return ""
    if backend == BACKEND_SEKAI_VIEWER:
        return f"{key}_i18n"
    if backend == BACKEND_MOESEKAI:
        return f"{key}_translation"
    return ""


def normalize_source_id(value: str) -> str:
    """Map a legacy or canonical source ID to the canonical ID.

    Unknown values are returned unchanged (lower-cased) so callers can
    report them as errors instead of silently swallowing typos.
    """
    key = str(value or "").strip().lower()
    return SOURCE_ALIASES.get(key, key)


def is_story_source(value: str) -> bool:
    return normalize_source_id(value) in ALL_SOURCES


def auxiliary_source_for(source_id: str) -> Optional[str]:
    """Return the auxiliary (translation reference) source for a story source."""
    return {
        SOURCE_SV: SOURCE_SV_I18N,
        SOURCE_MS: SOURCE_MS_TRANSLATION,
    }.get(normalize_source_id(source_id))


def backend_of(value: str) -> str:
    """Return the backend adapter name for a source ID ("" when unknown)."""
    source_id = normalize_source_id(value)
    if source_id in {SOURCE_SV, SOURCE_SV_I18N}:
        return BACKEND_SEKAI_VIEWER
    if source_id in {SOURCE_MS, SOURCE_MS_TRANSLATION}:
        return BACKEND_MOESEKAI
    return ""


def normalize_sources(values: Iterable[str]) -> tuple[str, ...]:
    """Normalize a list of requested source IDs, preserving order and deduplicating."""
    seen: list[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item:
            continue
        normalized = normalize_source_id(item)
        if normalized not in seen:
            seen.append(normalized)
    return tuple(seen)


def source_rank(source_id: str, priority: Iterable[str] = DEFAULT_SOURCE_PRIORITY) -> int:
    """Rank of a source in a priority order; unknown sources rank last."""
    order = tuple(normalize_source_id(item) for item in priority)
    try:
        return order.index(normalize_source_id(source_id))
    except ValueError:
        return len(order)
