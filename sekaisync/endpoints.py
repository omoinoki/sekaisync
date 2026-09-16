"""Single source of truth for external endpoint configuration.

Phase D convergence: the crawler previously kept twelve module-level URL
globals and ``progress``/``event_detection`` each kept a private
``_SV_MASTER_BASE`` copy. All of them now live in one immutable
``SourceEndpoints`` snapshot; configuration replaces the snapshot atomically
(frozen dataclass assignment), which is thread-safe and makes "who changed
endpoints, when" answerable.

Field names intentionally mirror the old globals 1:1 for a mechanical,
behaviour-preserving migration.
"""

from __future__ import annotations

import contextvars
import threading
from dataclasses import dataclass, field, replace
from typing import Optional

from sekaisync.config import MoesekaiSettings, ViewerSettings

EMPTY_BUCKETS = {
    "jp": "sekai-jp-assets",
    "en": "sekai-en-assets",
    "tc": "sekai-tc-assets",
    "kr": "sekai-kr-assets",
    "cn": "sekai-cn-assets",
}


@dataclass(frozen=True)
class SourceEndpoints:
    # altsource_ms (Moesekai)
    ALTSOURCE_MS_BASE: str = ""
    ALTSOURCE_MS_SITEMAP: str = ""
    ALTSOURCE_MS_STORY_DETAIL_BASE: str = ""
    ALTSOURCE_MS_METADATA_BASES: tuple[str, ...] = ()
    ALTSOURCE_MS_ASSET_BASES: tuple[str, ...] = ()
    ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN: bool = True
    ALTSOURCE_MS_LOCALE_SERVERS: dict[str, str] = field(default_factory=dict)
    ALTSOURCE_MS_LOCALE_LANGUAGES: dict[str, str] = field(default_factory=dict)
    ALTSOURCE_MS_TRANSLATION_BASE: str = ""
    # altsource_sv (Sekai Viewer)
    ALTSOURCE_SV_I18N_BASE: str = ""
    ALTSOURCE_SV_MASTER_BASE: str = field(
        default_factory=lambda: ViewerSettings().master_base
    )
    ALTSOURCE_SV_ASSET_BASE: str = ""
    ALTSOURCE_SV_ASSET_BUCKETS: dict[str, str] = field(
        default_factory=lambda: dict(EMPTY_BUCKETS)
    )


_current = SourceEndpoints()
_lock = threading.Lock()
_UNSET = object()

#: Per-context endpoint override (Astra P14/D14).
#:
#: The module-level ``_current`` snapshot is process-global, so two callers with
#: different configurations overwrite each other and "the last one configured
#: wins" for everybody — including work already in flight. A
#: :class:`~sekaisync.runtime.RuntimeContext` installs its own snapshot here for
#: the duration of its scope, so interleaved callers each see their own
#: endpoints. A ContextVar (not a global) is used so concurrent threads and
#: asyncio tasks keep separate values.
_override: "contextvars.ContextVar[Optional[SourceEndpoints]]" = (
    contextvars.ContextVar("sekaisync_endpoints_override", default=None)
)


def current_endpoints() -> SourceEndpoints:
    """The endpoints in effect for the current context.

    Returns the context-scoped override when one is active (an explicit
    :class:`RuntimeContext` scope), otherwise the process-wide configured
    snapshot.
    """
    scoped = _override.get()
    return scoped if scoped is not None else _current


def _set_override(snapshot: SourceEndpoints):
    """Install a context-scoped snapshot; returns the reset token."""
    return _override.set(snapshot)


def _reset_override(token) -> None:
    """Restore the previous context-scoped snapshot."""
    _override.reset(token)


def configure_endpoints(
    moesekai: Optional[MoesekaiSettings] = None,
    viewer: Optional[ViewerSettings] = None,
    sv_master_base=_UNSET,
) -> SourceEndpoints:
    """Merge the given settings into a new snapshot and make it current."""
    global _current
    with _lock:
        snapshot = _current
        if moesekai is not None:
            snapshot = replace(
                snapshot,
                ALTSOURCE_MS_BASE=moesekai.site_base,
                ALTSOURCE_MS_SITEMAP=moesekai.sitemap_url,
                ALTSOURCE_MS_STORY_DETAIL_BASE=moesekai.story_detail_base,
                ALTSOURCE_MS_METADATA_BASES=moesekai.metadata_bases,
                ALTSOURCE_MS_ASSET_BASES=moesekai.asset_bases,
                ALTSOURCE_MS_TRANSLATION_BASE=moesekai.translation_base,
                ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN=moesekai.fallback_to_viewer_cdn,
                ALTSOURCE_MS_LOCALE_SERVERS={
                    key: value for key, value in moesekai.locale_servers
                }
                or snapshot.ALTSOURCE_MS_LOCALE_SERVERS,
                ALTSOURCE_MS_LOCALE_LANGUAGES={
                    key: value for key, value in moesekai.locale_languages
                }
                or snapshot.ALTSOURCE_MS_LOCALE_LANGUAGES,
            )
        if viewer is not None:
            snapshot = replace(
                snapshot,
                ALTSOURCE_SV_I18N_BASE=viewer.i18n_base,
                ALTSOURCE_SV_MASTER_BASE=viewer.master_base,
                ALTSOURCE_SV_ASSET_BASE=viewer.asset_base,
                ALTSOURCE_SV_ASSET_BUCKETS={
                    key: value for key, value in viewer.asset_buckets
                }
                or snapshot.ALTSOURCE_SV_ASSET_BUCKETS,
            )
        if sv_master_base is not _UNSET:
            # Matches the historical apply_master_base(None/"") reset semantics.
            snapshot = replace(
                snapshot,
                ALTSOURCE_SV_MASTER_BASE=str(sv_master_base or "").rstrip("/")
                or ViewerSettings().master_base,
            )
        _current = snapshot
        return _current
