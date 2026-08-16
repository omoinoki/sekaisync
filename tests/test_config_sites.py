import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.config import (
    MoesekaiSettings,
    SekaiSyncConfig,
    SiteSettings,
    ViewerSettings,
    load_site_profile,
    settings_path,
)
from sekaisync.sources import (
    BACKEND_MOESEKAI,
    BACKEND_SEKAI_VIEWER,
    SOURCE_MS,
    SOURCE_SV,
)


class SiteSettingsTest(unittest.TestCase):
    def test_viewer_settings_defaults_and_buckets(self):
        viewer = ViewerSettings()
        self.assertEqual(viewer.master_base, "")
        self.assertEqual(viewer.asset_base, "")
        self.assertEqual(viewer.bucket_for("cn"), "sekai-cn-assets")
        self.assertEqual(viewer.bucket_for("xx"), "sekai-xx-assets")
        parsed = ViewerSettings.from_dict(
            {
                "master_base": "https://mirror.example/master",
                "asset_base": "https://mirror.example/assets",
                "asset_buckets": {"jp": "my-jp-bucket"},
                "i18n_base": "https://mirror.example/i18n",
            }
        )
        self.assertEqual(parsed.master_base, "https://mirror.example/master")
        self.assertEqual(parsed.bucket_for("jp"), "my-jp-bucket")
        self.assertEqual(parsed.bucket_for("cn"), "sekai-cn-assets")
        self.assertEqual(parsed.to_dict()["asset_buckets"], {"jp": "my-jp-bucket"})

    def test_moesekai_settings_roundtrip(self):
        settings = MoesekaiSettings()
        parsed = MoesekaiSettings.from_dict(settings.to_dict())
        self.assertEqual(parsed, settings)
        self.assertEqual(parsed.metadata_bases, settings.metadata_bases)
        self.assertEqual(parsed.locale_servers, settings.locale_servers)
        self.assertEqual(parsed.locale_languages, settings.locale_languages)
        self.assertTrue(parsed.fallback_to_viewer_cdn)

    def test_moesekai_settings_custom_locale_maps_and_fallback(self):
        data = {
            "site_base": "https://mirror.example.com",
            "locale_servers": {"zh-cn": "mainland", "ja-jp": "jp"},
            "locale_languages": [["zh-cn", "zh_hans"], ["ja-jp", "ja"]],
            "fallback_to_viewer_cdn": "false",
        }
        parsed = MoesekaiSettings.from_dict(data)
        self.assertEqual(parsed.site_base, "https://mirror.example.com")
        self.assertEqual(parsed.server_for("zh-cn"), "mainland")
        self.assertEqual(parsed.server_for("ja-jp"), "jp")
        self.assertEqual(parsed.server_for("fr-fr"), "cn")
        self.assertEqual(parsed.language_for("zh-cn"), "zh_hans")
        self.assertEqual(parsed.language_for("ja-jp"), "ja")
        self.assertEqual(parsed.language_for("ko-kr"), "zh_hans")
        self.assertFalse(parsed.fallback_to_viewer_cdn)
        self.assertFalse(parsed.to_dict()["fallback_to_viewer_cdn"])

    def test_site_settings_require_id_and_backend(self):
        with self.assertRaises(ValueError):
            SiteSettings.from_dict({"name": "no id"})
        with self.assertRaises(ValueError):
            SiteSettings.from_dict({"id": "x", "backend": "nope"})

    def test_site_settings_roundtrip(self):
        site = SiteSettings(
            id=SOURCE_SV,
            backend=BACKEND_SEKAI_VIEWER,
            name="Sekai Viewer",
            viewer=ViewerSettings(),
        )
        parsed = SiteSettings.from_dict(site.to_dict())
        self.assertEqual(parsed.id, SOURCE_SV)
        self.assertEqual(parsed.backend, BACKEND_SEKAI_VIEWER)
        self.assertEqual(parsed.viewer, ViewerSettings())
        self.assertEqual(parsed.settings_for(SOURCE_SV), ViewerSettings())

        ms_site = SiteSettings(
            id=SOURCE_MS,
            backend=BACKEND_MOESEKAI,
            name="Moesekai",
            moesekai=MoesekaiSettings(),
        )
        self.assertEqual(ms_site.settings_for(SOURCE_MS), MoesekaiSettings())


class SiteProfileTest(unittest.TestCase):
    def test_profile_order_is_priority(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = {
                "version": 2,
                "sites": [
                    {
                        "id": "altsource_ms",
                        "backend": "moesekai",
                        "name": "Moesekai mirror",
                        "enabled": True,
                        "site_base": "https://ms.example",
                    },
                    {
                        "id": "altsource_sv",
                        "backend": "sekai_viewer",
                        "name": "Sekai Viewer",
                        "enabled": True,
                        "master_base": "https://sv.example",
                    },
                ],
            }
            settings_path(base).write_text(json.dumps(data), encoding="utf-8")
            profile = load_site_profile(base)
            self.assertEqual([site.id for site in profile], [SOURCE_MS, SOURCE_SV])
            self.assertEqual(profile[0].moesekai.site_base, "https://ms.example")
            self.assertEqual(profile[1].viewer.master_base, "https://sv.example")

            config = SekaiSyncConfig(store_root=base / "store", sites=profile)
            self.assertEqual(config.source_priority(), (SOURCE_MS, SOURCE_SV))
            self.assertEqual(config.default_sources(), (SOURCE_MS, SOURCE_SV))
            self.assertEqual(config.resolve_sources([]), (SOURCE_MS, SOURCE_SV))
            self.assertEqual(config.moesekai.site_base, "https://ms.example")
            self.assertEqual(config.viewer.master_base, "https://sv.example")

    def test_disabled_sites_are_skipped_in_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = {
                "sites": [
                    {
                        "id": "altsource_sv",
                        "backend": "sekai_viewer",
                        "enabled": False,
                        "master_base": "https://sv.example",
                    },
                    {
                        "id": "altsource_ms",
                        "backend": "moesekai",
                        "enabled": True,
                    },
                ]
            }
            settings_path(base).write_text(json.dumps(data), encoding="utf-8")
            profile = load_site_profile(base)
            config = SekaiSyncConfig(store_root=base / "store", sites=profile)
            self.assertEqual(config.source_priority(), (SOURCE_MS,))
            self.assertEqual(config.default_sources(), (SOURCE_MS,))

    def test_legacy_altsource_block_maps_to_ms_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = {
                "altsource": {
                    "site_base": "https://legacy.example",
                    "metadata_bases": ["https://meta.example"],
                }
            }
            settings_path(base).write_text(json.dumps(data), encoding="utf-8")
            profile = load_site_profile(base)
            self.assertEqual([site.id for site in profile], [SOURCE_SV, SOURCE_MS])
            ms = next(site for site in profile if site.backend == BACKEND_MOESEKAI)
            self.assertEqual(ms.moesekai.site_base, "https://legacy.example")
            self.assertEqual(ms.moesekai.metadata_bases, ("https://meta.example",))

    def test_resolve_sources_accepts_legacy_aliases_and_validates(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = SekaiSyncConfig(store_root=base / "store")
            self.assertEqual(
                config.resolve_sources(["altsource", "sekai_viewer"]),
                (SOURCE_MS, SOURCE_SV),
            )
            self.assertEqual(config.resolve_sources([SOURCE_SV]), (SOURCE_SV,))
            with self.assertRaises(ValueError):
                config.resolve_sources(["fandom"])


class MultiInstanceTest(unittest.TestCase):
    def _profile(self):
        return (
            SiteSettings(
                id="altsource_sv",
                backend=BACKEND_SEKAI_VIEWER,
                name="Sekai Viewer",
                viewer=ViewerSettings(),
            ),
            SiteSettings(
                id="altsource_sv_local",
                backend=BACKEND_SEKAI_VIEWER,
                name="Self-hosted viewer",
                viewer=ViewerSettings(master_base="https://viewer.local/master"),
            ),
            SiteSettings(
                id="altsource_ms",
                backend=BACKEND_MOESEKAI,
                name="Moesekai mirror",
                moesekai=MoesekaiSettings(),
            ),
            SiteSettings(
                id="altsource_ms_alt",
                backend=BACKEND_MOESEKAI,
                name="Second mirror",
                moesekai=MoesekaiSettings(site_base="https://ms-alt.example"),
            ),
        )

    def test_multiple_instances_per_backend_class(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = SekaiSyncConfig(store_root=base / "store", sites=self._profile())
            # Type selectors expand to every enabled instance of the class.
            self.assertEqual(
                config.resolve_sources([SOURCE_SV]),
                ("altsource_sv", "altsource_sv_local"),
            )
            self.assertEqual(
                config.resolve_sources(["altsource"]),
                ("altsource_ms", "altsource_ms_alt"),
            )
            # Instance selectors pass through.
            self.assertEqual(
                config.resolve_sources(["altsource_sv_local"]),
                ("altsource_sv_local",),
            )
            # Default = all instances in profile order; priority = same order.
            self.assertEqual(
                config.default_sources(),
                ("altsource_sv", "altsource_sv_local", "altsource_ms", "altsource_ms_alt"),
            )
            self.assertEqual(config.source_priority(), config.default_sources())
            # Per-instance endpoint settings are resolved by instance ID.
            self.assertEqual(
                config.instance_settings("altsource_sv_local").master_base,
                "https://viewer.local/master",
            )
            self.assertEqual(
                config.instance_settings("altsource_ms_alt").site_base,
                "https://ms-alt.example",
            )
            self.assertEqual(
                config.instance_backend("altsource_sv_local"),
                BACKEND_SEKAI_VIEWER,
            )

    def test_disabled_instance_is_skipped_by_type_expansion(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = self._profile()
            disabled = SiteSettings(
                id="altsource_sv_off",
                backend=BACKEND_SEKAI_VIEWER,
                name="Disabled viewer",
                enabled=False,
                viewer=ViewerSettings(),
            )
            config = SekaiSyncConfig(store_root=base / "store", sites=profile + (disabled,))
            self.assertEqual(
                config.resolve_sources([SOURCE_SV]),
                ("altsource_sv", "altsource_sv_local"),
            )
            self.assertNotIn("altsource_sv_off", config.default_sources())

    def test_duplicate_instance_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = {
                "sites": [
                    {"id": "dup", "backend": "sekai_viewer", "enabled": True},
                    {"id": "dup", "backend": "moesekai", "enabled": True},
                ]
            }
            settings_path(base).write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_site_profile(base)

    def test_profile_roundtrip_preserves_instances(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            profile = self._profile()
            data = {"version": 2, "sites": [site.to_dict() for site in profile]}
            settings_path(base).write_text(json.dumps(data), encoding="utf-8")
            loaded = load_site_profile(base)
            self.assertEqual([site.id for site in loaded], [site.id for site in profile])
            self.assertEqual([site.backend for site in loaded], [site.backend for site in profile])
            self.assertEqual(loaded[1].viewer.master_base, "https://viewer.local/master")
            self.assertEqual(loaded[3].moesekai.site_base, "https://ms-alt.example")


if __name__ == "__main__":
    unittest.main()
