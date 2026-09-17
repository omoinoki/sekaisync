"""Explicit per-call runtime context (Astra P14/D14).

Before this, external endpoints lived in one process-global snapshot that
``configure_endpoints`` replaced: whoever configured last won, for everybody,
including work already in flight. Two crawls against different instances could
therefore read each other's endpoints, and a server could serve one caller's
configuration to another.

:class:`RuntimeContext` makes the configuration an explicit, immutable value
that a caller builds once and passes down. Entering its scope installs the
endpoints for that context only (via a ContextVar), so interleaved callers each
see their own. Nothing here mutates process state outside the scope, and the
scope restores the previous value on exit even when the body raises.

The module-global path is deliberately kept working: a pure local read
(``SekaiSyncCore(store_root)`` with no runtime) must not require network
configuration, and existing CLI entry points keep functioning. What changes is
that a caller who *has* a runtime no longer depends on global state, and a
network-requiring method without one reports that clearly instead of silently
using whatever was configured last.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass, fields, replace
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Optional

from sekaisync.config import SekaiSyncConfig, SiteSettings
from sekaisync.endpoints import (
    SourceEndpoints,
    _reset_override,
    _set_override,
    current_endpoints,
)


class RuntimeNotConfiguredError(RuntimeError):
    """A network operation ran without an explicit runtime.

    Raised instead of silently falling back to the process-global endpoint
    snapshot, so a caller learns that configuration was not threaded through
    rather than getting another instance's endpoints.
    """


@dataclass(frozen=True)
class RuntimeContext:
    """Everything a call needs that is not the store itself.

    Frozen: the value is shared across threads and must not be mutated after
    construction (Astra: 不同配置快照不共享可变 list/dict).
    """

    store_root: Path
    endpoints: SourceEndpoints
    sites: tuple[SiteSettings, ...] = ()
    fetcher: Optional[Callable[..., str]] = None
    #: Instance ids the profile enables, in priority order.
    source_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "store_root", Path(self.store_root))
        object.__setattr__(self, "sites", tuple(self.sites))
        object.__setattr__(self, "source_ids", tuple(self.source_ids))
        instances = {}
        seen = set()
        for entry in self.sites:
            key = entry.id.lower()
            if key in seen:
                raise ValueError(f"duplicate instance {entry.id!r}")
            seen.add(key)
            if entry.enabled:
                # Selected instance wins for its backend. Only another backend
                # may supply an explicitly configured CDN fallback.
                others = tuple(s for s in self.sites if s.backend != entry.backend)
                instances[key] = endpoints_from_sites((entry,) + others)
        object.__setattr__(self, "_instance_endpoints", MappingProxyType(instances))

    def endpoints_for(self, source_id: str) -> SourceEndpoints:
        """Endpoints configured for one instance.

        Each registered instance gets its own snapshot (derived once in
        ``__post_init__``), so two instances of the same backend no longer
        overwrite each other's fields. Unknown or disabled ids raise instead of
        silently returning another instance's endpoints. The id-less backend
        default stays available as ``self.endpoints``.
        """
        wanted = str(source_id or "").strip().lower()
        try:
            return self._instance_endpoints[wanted]  # type: ignore[attr-defined]
        except (AttributeError, KeyError):
            raise ValueError(
                f"no enabled instance {source_id!r} in this runtime; "
                f"known: {sorted(getattr(self, '_instance_endpoints', {}))}"
            ) from None

    def cache_namespace_for(self, source_id: str) -> str:
        """Stable instance + endpoint identity, independent of process globals."""
        endpoints = self.endpoints_for(source_id)
        payload = {f.name: dict(value) if isinstance(value, Mapping) else value
                   for f in fields(endpoints) for value in (getattr(endpoints, f.name),)}
        payload["instance"] = str(source_id).strip().lower()
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def site(self, source_id: str) -> Optional[SiteSettings]:
        """The registered instance entry for ``source_id``, if any."""
        wanted = str(source_id or "").strip().lower()
        for entry in self.sites:
            if entry.id.lower() == wanted:
                return entry
        return None

    def enabled_sites(self) -> tuple[SiteSettings, ...]:
        return tuple(entry for entry in self.sites if entry.enabled)

    @contextlib.contextmanager
    def activate(self, source_id: Optional[str] = None):
        """Make this runtime's endpoints current for the duration of the block.

        Context-scoped, so concurrent callers do not observe each other's
        endpoints, and the previous value is restored on exit — including when
        the body raises (Astra: 失败退出后上下文恢复).
        """
        token = _set_override(self.endpoints_for(source_id) if source_id is not None else self.endpoints)
        try:
            yield self
        finally:
            _reset_override(token)


def build_runtime(
    config: SekaiSyncConfig,
    *,
    fetcher: Optional[Callable[..., str]] = None,
    sites: Optional[Iterable[SiteSettings]] = None,
) -> RuntimeContext:
    """Build a runtime from configuration, once, at an entry point.

    Mirrors what ``configure_endpoints`` produced, but as an immutable value
    the caller owns rather than a global it mutates.
    """
    profile = tuple(sites if sites is not None else _sites_for(config))
    endpoints = endpoints_from_sites(profile)
    return RuntimeContext(
        store_root=Path(config.store_root),
        endpoints=endpoints,
        sites=profile,
        fetcher=fetcher,
        source_ids=tuple(entry.id for entry in profile if entry.enabled),
    )


def endpoints_from_sites(
    sites: Iterable[SiteSettings],
    *,
    base: Optional[SourceEndpoints] = None,
) -> SourceEndpoints:
    """Derive a snapshot from a site profile without touching global state.

    The first enabled settings for each backend win, matching profile priority.
    No settings are borrowed from another instance of the same backend.  With
    ``base``, the profile is merged on top of that snapshot, so a field the
    profile does not configure keeps its ambient value instead of resetting to
    the dataclass default — the merge semantics ``configure_endpoints`` always
    had.  A Moesekai-only scope therefore keeps the configured Viewer asset
    base that its Viewer-CDN fallback fetches from.
    """
    snapshot = base if base is not None else SourceEndpoints()
    have_ms = have_sv = False
    for entry in sites:
        if not entry.enabled:
            continue
        if entry.moesekai is not None and not have_ms:
            have_ms = True
            snapshot = replace(
                snapshot,
                ALTSOURCE_MS_BASE=entry.moesekai.site_base,
                ALTSOURCE_MS_SITEMAP=entry.moesekai.sitemap_url,
                ALTSOURCE_MS_STORY_DETAIL_BASE=entry.moesekai.story_detail_base,
                ALTSOURCE_MS_METADATA_BASES=entry.moesekai.metadata_bases,
                ALTSOURCE_MS_ASSET_BASES=entry.moesekai.asset_bases,
                ALTSOURCE_MS_TRANSLATION_BASE=entry.moesekai.translation_base,
                ALTSOURCE_MS_FALLBACK_TO_VIEWER_CDN=entry.moesekai.fallback_to_viewer_cdn,
                ALTSOURCE_MS_LOCALE_SERVERS={
                    key: value for key, value in entry.moesekai.locale_servers
                }
                or snapshot.ALTSOURCE_MS_LOCALE_SERVERS,
                ALTSOURCE_MS_LOCALE_LANGUAGES={
                    key: value for key, value in entry.moesekai.locale_languages
                }
                or snapshot.ALTSOURCE_MS_LOCALE_LANGUAGES,
            )
        if entry.viewer is not None and not have_sv:
            have_sv = True
            snapshot = replace(
                snapshot,
                ALTSOURCE_SV_I18N_BASE=entry.viewer.i18n_base,
                ALTSOURCE_SV_MASTER_BASE=entry.viewer.master_base,
                ALTSOURCE_SV_ASSET_BASE=entry.viewer.asset_base,
                ALTSOURCE_SV_ASSET_BUCKETS={
                    key: value for key, value in entry.viewer.asset_buckets
                }
                or snapshot.ALTSOURCE_SV_ASSET_BUCKETS,
            )
    return snapshot


def _sites_for(config: SekaiSyncConfig) -> tuple[SiteSettings, ...]:
    """The configured site profile carried by the config.

    ``SekaiSyncConfig`` already resolves the profile (``from_dict`` loads
    ``settings.json``), so this does not re-read the file — the old code had
    three separate loaders, and Astra asks for one selected list.
    """
    return tuple(getattr(config, "sites", ()) or ())


def require_endpoints(runtime: Optional[RuntimeContext], *, what: str) -> SourceEndpoints:
    """Endpoints for a network operation, or a clear failure.

    ``runtime=None`` means the caller did not thread configuration through.
    Falling back to the global snapshot is exactly the cross-talk P14 removes,
    so this reports the problem instead.
    """
    if runtime is None:
        raise RuntimeNotConfiguredError(
            f"{what} needs an explicit RuntimeContext. Build one with "
            f"build_runtime(config) and pass it in; refusing to use the "
            f"process-global endpoint snapshot, which another caller may have "
            f"replaced."
        )
    return runtime.endpoints
