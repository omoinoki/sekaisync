from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Optional

from sekaisync.sources import (
    BACKEND_MOESEKAI,
    BACKEND_SEKAI_VIEWER,
    KNOWN_BACKENDS,
    SOURCE_MS,
    SOURCE_SV,
    backend_of_type,
    normalize_source_id,
)


@dataclass(frozen=True)
class Region:
    key: str
    language: str
    repo_slug: Optional[str]
    official_translation: bool
    launch_date: Optional[str]
    note: str = ""


REGIONS: Dict[str, Region] = {
    "jp": Region(
        key="jp",
        language="ja",
        repo_slug="Sekai-World/sekai-master-db-diff",
        official_translation=False,
        launch_date="2020-09-30",
        note="Japanese master data source",
    ),
    "en": Region(
        key="en",
        language="en",
        repo_slug="Sekai-World/sekai-master-db-en-diff",
        official_translation=True,
        launch_date="2021-12-07",
        note="Global English server, includes SEA expansion data",
    ),
    "tc": Region(
        key="tc",
        language="zh_hant",
        repo_slug="Sekai-World/sekai-master-db-tc-diff",
        official_translation=True,
        launch_date="2021-09-30",
        note="Traditional Chinese / Hong Kong / Macau server",
    ),
    "kr": Region(
        key="kr",
        language="ko",
        repo_slug="Sekai-World/sekai-master-db-kr-diff",
        official_translation=True,
        launch_date="2022-05-20",
        note="Korean server",
    ),
    "cn": Region(
        key="cn",
        language="zh_hans",
        repo_slug="Sekai-World/sekai-master-db-cn-diff",
        official_translation=True,
        launch_date="2025-03-27",
        note="Simplified Chinese mainland server",
    ),
}


DEFAULT_REGION_ORDER = ["jp", "en", "tc", "kr", "cn"]


def _parse_bool_flag(value: object, default: bool) -> bool:
    """Parse a JSON boolean flag, tolerating string forms like ``"false"``."""
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    if value is None:
        return default
    return bool(value)


@dataclass(frozen=True)
class MoesekaiSettings:
    """Endpoint settings for one moesekai-backend instance (altsource_ms class).

    Every upstream host is configurable so a moved domain or a self-hosted
    moesekai deployment can be pointed at by editing settings.json only:

    - ``site_base``: web site used for canonical story/card/virtual-live URLs;
    - ``sitemap_url``: sitemap index used by the site-wide crawl;
    - ``story_detail_base``: JSON story detail endpoint;
    - ``metadata_bases`` / ``asset_bases``: ordered failover lists;
    - ``translation_base`` / ``news_base``: overlay translation and news;
    - ``locale_servers`` / ``locale_languages``: locale -> server/bucket suffix
      and locale -> content language mappings (self-hosted deployments may
      use a different server naming scheme);
    - ``fallback_to_viewer_cdn``: when every asset base fails, whether to fall
      back to the Sekai Viewer asset CDN (disable for fully self-hosted setups).
    """

    site_base: str = ""
    sitemap_url: str = ""
    story_detail_base: str = ""
    metadata_bases: tuple[str, ...] = ()
    asset_bases: tuple[str, ...] = ()
    translation_base: str = ""
    news_base: str = ""
    locale_servers: tuple[tuple[str, str], ...] = (
        ("zh-cn", "cn"),
        ("zh-tw", "tw"),
        ("ja-jp", "jp"),
        ("en-us", "en"),
        ("ko-kr", "kr"),
    )
    locale_languages: tuple[tuple[str, str], ...] = (
        ("zh-cn", "zh_hans"),
        ("zh-tw", "zh_hant"),
        ("ja-jp", "ja"),
        ("en-us", "en"),
        ("ko-kr", "ko"),
    )
    fallback_to_viewer_cdn: bool = True

    def server_for(self, locale: str) -> str:
        key = (str(locale or "")).strip().lower()
        for item_key, server in self.locale_servers:
            if item_key == key:
                return server
        return "cn"

    def language_for(self, locale: str) -> str:
        key = (str(locale or "")).strip().lower()
        for item_key, language in self.locale_languages:
            if item_key == key:
                return language
        return "zh_hans"

    @classmethod
    def from_dict(cls, data: dict) -> "MoesekaiSettings":
        def tuple_value(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            value = data.get(key, default)
            if isinstance(value, str):
                return (value,)
            if isinstance(value, (list, tuple)):
                return tuple(str(item) for item in value)
            return default

        def pairs_value(
            key: str, default: tuple[tuple[str, str], ...]
        ) -> tuple[tuple[str, str], ...]:
            value = data.get(key, default)
            if isinstance(value, dict):
                return tuple((str(k), str(v)) for k, v in value.items())
            if isinstance(value, (list, tuple)):
                parsed: list[tuple[str, str]] = []
                for item in value:
                    if isinstance(item, (list, tuple)) and len(item) == 2:
                        parsed.append((str(item[0]), str(item[1])))
                if parsed:
                    return tuple(parsed)
            return default

        return cls(
            site_base=str(data.get("site_base", cls.site_base)),
            sitemap_url=str(data.get("sitemap_url", cls.sitemap_url)),
            story_detail_base=str(data.get("story_detail_base", cls.story_detail_base)),
            metadata_bases=tuple_value("metadata_bases", cls.metadata_bases),
            asset_bases=tuple_value("asset_bases", cls.asset_bases),
            translation_base=str(data.get("translation_base", cls.translation_base)),
            news_base=str(data.get("news_base", cls.news_base)),
            locale_servers=pairs_value("locale_servers", cls.locale_servers),
            locale_languages=pairs_value("locale_languages", cls.locale_languages),
            fallback_to_viewer_cdn=_parse_bool_flag(
                data.get("fallback_to_viewer_cdn", cls.fallback_to_viewer_cdn),
                cls.fallback_to_viewer_cdn,
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "site_base": self.site_base,
            "sitemap_url": self.sitemap_url,
            "story_detail_base": self.story_detail_base,
            "metadata_bases": list(self.metadata_bases),
            "asset_bases": list(self.asset_bases),
            "translation_base": self.translation_base,
            "news_base": self.news_base,
            "locale_servers": {key: value for key, value in self.locale_servers},
            "locale_languages": {key: value for key, value in self.locale_languages},
            "fallback_to_viewer_cdn": self.fallback_to_viewer_cdn,
        }


@dataclass(frozen=True)
class ViewerSettings:
    """Endpoint settings for one sekai_viewer-backend instance (altsource_sv class)."""

    master_base: str = ""
    asset_base: str = ""
    asset_buckets: tuple[tuple[str, str], ...] = (
        ("jp", "sekai-jp-assets"),
        ("en", "sekai-en-assets"),
        ("tc", "sekai-tc-assets"),
        ("kr", "sekai-kr-assets"),
        ("cn", "sekai-cn-assets"),
    )
    i18n_base: str = ""

    def bucket_for(self, region: str) -> str:
        for key, bucket in self.asset_buckets:
            if key == region:
                return bucket
        return f"sekai-{region}-assets"

    @classmethod
    def from_dict(cls, data: dict) -> "ViewerSettings":
        buckets: list[tuple[str, str]] = []
        raw_buckets = data.get("asset_buckets", cls.asset_buckets)
        if isinstance(raw_buckets, dict):
            for key, value in raw_buckets.items():
                buckets.append((str(key), str(value)))
        elif isinstance(raw_buckets, (list, tuple)):
            for item in raw_buckets:
                if isinstance(item, (list, tuple)) and len(item) == 2:
                    buckets.append((str(item[0]), str(item[1])))
        return cls(
            master_base=str(data.get("master_base", cls.master_base)),
            asset_base=str(data.get("asset_base", cls.asset_base)),
            asset_buckets=tuple(buckets) if buckets else cls.asset_buckets,
            i18n_base=str(data.get("i18n_base", cls.i18n_base)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "master_base": self.master_base,
            "asset_base": self.asset_base,
            "asset_buckets": {key: value for key, value in self.asset_buckets},
            "i18n_base": self.i18n_base,
        }


@dataclass(frozen=True)
class SiteSettings:
    """One registered instance of a backend class in settings.json.

    ``id`` is a free-form, profile-unique instance ID (the default
    instances use the canonical type IDs ``altsource_sv`` / ``altsource_ms``
    so legacy stores keep byte-compatible page IDs).  ``backend`` selects the
    class of public data system; several instances may share a backend, like
    the per-source entries in a booru reader.
    """

    id: str
    backend: str
    name: str = ""
    enabled: bool = True
    moesekai: Optional[MoesekaiSettings] = None
    viewer: Optional[ViewerSettings] = None

    def matches(self, source_id: str) -> bool:
        return str(source_id or "").strip() == self.id

    def settings_for(self, source_id: str) -> object:
        backend = self.backend or ""
        if backend == BACKEND_SEKAI_VIEWER:
            return self.viewer or ViewerSettings()
        if backend == BACKEND_MOESEKAI:
            return self.moesekai or MoesekaiSettings()
        return {}

    @classmethod
    def from_dict(cls, data: dict) -> "SiteSettings":
        site_id = str(data.get("id") or "").strip()
        backend = str(data.get("backend") or "").strip().lower()
        if not site_id:
            raise ValueError("Each site entry needs an 'id'")
        if backend not in KNOWN_BACKENDS:
            raise ValueError(
                f"Unknown site backend {backend!r} (known: {', '.join(KNOWN_BACKENDS)})"
            )
        moesekai = None
        viewer = None
        if backend == BACKEND_MOESEKAI:
            moesekai = MoesekaiSettings.from_dict(data if isinstance(data, dict) else {})
        else:
            viewer = ViewerSettings.from_dict(data if isinstance(data, dict) else {})
        return cls(
            id=site_id,
            backend=backend,
            name=str(data.get("name") or ""),
            enabled=bool(data.get("enabled", True)),
            moesekai=moesekai,
            viewer=viewer,
        )

    def to_dict(self) -> dict[str, object]:
        entry: dict[str, object] = {
            "id": self.id,
            "backend": self.backend,
            "name": self.name,
            "enabled": self.enabled,
        }
        if self.moesekai is not None:
            entry.update(self.moesekai.to_dict())
        if self.viewer is not None:
            entry.update(self.viewer.to_dict())
        return entry


def require_endpoint(value: Any, field: str, site_id: str) -> Any:
    """Return a non-empty endpoint, raising a clear error when unconfigured.

    SekaiSync ships no real source addresses: the defaults are empty and a
    crawl or news sync must fail with an actionable message instead of
    silently fetching a relative URL.
    """
    def _fail() -> None:
        raise ValueError(
            f"Source '{site_id}' has no endpoint for '{field}'. "
            "Configure it in settings.json (see README「配置数据源」) before crawling or syncing news."
        )

    if isinstance(value, (tuple, list)):
        if not any(str(item or "").strip() for item in value):
            _fail()
        return value
    text = str(value or "").strip()
    if not text:
        _fail()
    return text


def default_site_profile(
    moesekai: Optional[MoesekaiSettings] = None,
) -> tuple[SiteSettings, ...]:
    """Built-in profile: one default instance per backend class."""
    return (
        SiteSettings(
            id=SOURCE_SV,
            backend=BACKEND_SEKAI_VIEWER,
            name="Sekai Viewer",
            viewer=ViewerSettings(),
        ),
        SiteSettings(
            id=SOURCE_MS,
            backend=BACKEND_MOESEKAI,
            name="Moesekai mirror",
            moesekai=moesekai or MoesekaiSettings(),
        ),
    )


@dataclass
class SekaiSyncConfig:
    store_root: Path
    regions: tuple[str, ...] = ("jp", "en", "tc", "kr", "cn")
    github_tarball_base: str = "https://github.com"
    demo: bool = False
    sites: tuple[SiteSettings, ...] = field(
        default_factory=lambda: default_site_profile()
    )
    extra: Dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict, base_dir: Path) -> "SekaiSyncConfig":
        store_root = Path(data.get("store_root", "store"))
        if not store_root.is_absolute():
            store_root = base_dir / store_root
        regions = tuple(data.get("regions", DEFAULT_REGION_ORDER))
        sites = load_site_profile(base_dir)
        return cls(
            store_root=store_root,
            regions=regions,
            github_tarball_base=data.get("github_tarball_base", "https://github.com"),
            demo=bool(data.get("demo", False)),
            sites=sites,
            extra=data.get("extra", {}),
        )

    # -- profile queries -------------------------------------------------

    def site_for(self, instance_id: str) -> Optional[SiteSettings]:
        target = str(instance_id or "").strip()
        for site in self.sites:
            if site.id == target:
                return site
        return None

    def instance_backend(self, instance_id: str) -> str:
        site = self.site_for(instance_id)
        return site.backend if site is not None else ""

    def enabled_sites(self) -> tuple[SiteSettings, ...]:
        return tuple(site for site in self.sites if site.enabled)

    def instances_of_backend(self, backend: str) -> tuple[SiteSettings, ...]:
        return tuple(
            site for site in self.enabled_sites() if site.backend == backend
        )

    def source_priority(self) -> tuple[str, ...]:
        """Enabled instance IDs in profile order; earlier entries win."""
        return tuple(site.id for site in self.sites if site.enabled)

    def default_sources(self) -> tuple[str, ...]:
        """All enabled instance IDs in profile order (crawl / news default)."""
        return self.source_priority()

    def instance_settings(self, instance_id: str) -> object:
        site = self.site_for(instance_id)
        if site is None:
            return {}
        return site.settings_for(instance_id)

    def resolve_sources(self, requested: Iterable[str]) -> tuple[str, ...]:
        """Resolve requested source selectors to concrete instance IDs.

        A selector may be:

        - a backend class ID (``altsource_sv`` / ``altsource_ms``) -> expands
          to every enabled instance of that class, in profile order;
        - an instance ID -> that instance (must exist in the profile);
        - a legacy alias (``altsource`` / ``sekai_viewer``) -> treated as the
          backend class ID.

        An empty request returns every enabled instance in profile order.
        """
        values = [str(item) for item in requested if str(item).strip()]
        if not values:
            return self.default_sources()
        by_backend: dict[str, list[str]] = {
            backend: [site.id for site in self.instances_of_backend(backend)]
            for backend in KNOWN_BACKENDS
        }
        result: list[str] = []
        seen: set[str] = set()
        for raw in values:
            selector = normalize_source_id(raw)
            backend = backend_of_type(selector)
            if backend:
                for instance_id in by_backend[backend]:
                    if instance_id not in seen:
                        seen.add(instance_id)
                        result.append(instance_id)
                continue
            site = self.site_for(selector)
            if site is None:
                known = [
                    SOURCE_SV,
                    SOURCE_MS,
                    *sorted(
                        site.id
                        for site in self.sites
                        if site.id not in {SOURCE_SV, SOURCE_MS}
                    ),
                ]
                raise ValueError(
                    f"Unknown crawl/news sources: {selector} "
                    f"(known types/instances: {', '.join(known)})"
                )
            if not site.enabled:
                continue
            if site.id not in seen:
                seen.add(site.id)
                result.append(site.id)
        return tuple(result)

    @property
    def moesekai(self) -> MoesekaiSettings:
        """Settings of the first enabled moesekai instance (priority order)."""
        for site in self.instances_of_backend(BACKEND_MOESEKAI):
            return site.moesekai or MoesekaiSettings()
        return MoesekaiSettings()

    @property
    def viewer(self) -> ViewerSettings:
        """Settings of the first enabled sekai_viewer instance (priority order)."""
        for site in self.instances_of_backend(BACKEND_SEKAI_VIEWER):
            return site.viewer or ViewerSettings()
        return ViewerSettings()


def settings_path(base_dir: Path) -> Path:
    return base_dir / "settings.json"


def load_site_profile(base_dir: Path) -> tuple[SiteSettings, ...]:
    """Load the ordered multi-instance profile from settings.json.

    ``settings.json`` carries a ``sites`` list whose order is the source
    priority (earlier entries win).  Each entry is one instance of a backend
    class; several instances may share a backend.  A legacy file with only an
    ``altsource`` block is still accepted and maps to the default
    altsource_ms instance; altsource_sv falls back to empty defaults that
    must be configured in settings.json before crawling.
    """
    path = settings_path(base_dir)
    if not path.exists():
        return default_site_profile()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_site_profile()
    if not isinstance(data, dict):
        return default_site_profile()
    sites = data.get("sites")
    if isinstance(sites, list) and sites:
        parsed: list[SiteSettings] = []
        seen: set[str] = set()
        for item in sites:
            if not isinstance(item, dict):
                continue
            try:
                site = SiteSettings.from_dict(item)
            except ValueError:
                continue
            if site.id in seen:
                raise ValueError(
                    f"Duplicate site instance id in settings.json: {site.id}"
                )
            seen.add(site.id)
            parsed.append(site)
        if parsed:
            return tuple(parsed)
    block = data.get("altsource") if isinstance(data, dict) else None
    moesekai = MoesekaiSettings.from_dict(block) if isinstance(block, dict) else MoesekaiSettings()
    return default_site_profile(moesekai)


def default_config(base_dir: Optional[Path] = None) -> SekaiSyncConfig:
    base = base_dir or Path.cwd()
    return SekaiSyncConfig(
        store_root=base / "store",
        sites=load_site_profile(base),
    )


def load_config(
    path: Optional[Path],
    base_dir: Optional[Path] = None,
    store_override: Optional[Path] = None,
) -> SekaiSyncConfig:
    base = base_dir or Path.cwd()
    if path is None:
        config = default_config(base)
        if store_override is None:
            env_store = os.environ.get("SEKAISYNC_STORE")
            if env_store:
                store_override = Path(env_store)
        if store_override is not None:
            return SekaiSyncConfig(
                store_root=Path(store_override).expanduser().resolve(),
                regions=config.regions,
                github_tarball_base=config.github_tarball_base,
                demo=config.demo,
                sites=config.sites,
                extra=config.extra,
            )
        return config
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    config = SekaiSyncConfig.from_dict(data, base)
    if store_override is not None:
        return SekaiSyncConfig(
            store_root=Path(store_override).expanduser().resolve(),
            regions=config.regions,
            github_tarball_base=config.github_tarball_base,
            demo=config.demo,
            sites=config.sites,
            extra=config.extra,
        )
    return config


def region_keys(regions: Iterable[str] | None = None) -> tuple[str, ...]:
    selected = tuple(regions) if regions is not None else DEFAULT_REGION_ORDER
    unknown = [r for r in selected if r not in REGIONS]
    if unknown:
        raise ValueError(f"Unknown regions: {', '.join(unknown)}")
    return selected
