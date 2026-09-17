"""Astra P14/D14 — explicit runtime, no process-global endpoint cross-talk.

Endpoints used to live in one process-global snapshot that
``configure_endpoints`` replaced wholesale: whoever configured last won, for
every caller, including work already in flight. Two crawls against different
instances could therefore read each other's endpoints.

`RuntimeContext` carries an immutable endpoint snapshot and installs it for one
context only, so interleaved (and concurrent) callers each see their own.
"""

import threading
import unittest
from pathlib import Path

from sekaisync.config import MoesekaiSettings, SekaiSyncConfig, SiteSettings, ViewerSettings
from sekaisync.crawler import _runtime_scope
from sekaisync.endpoints import current_endpoints
from sekaisync.runtime import (
    RuntimeContext,
    RuntimeNotConfiguredError,
    build_runtime,
    endpoints_from_sites,
    require_endpoints,
)


class RuntimeContextTest(unittest.TestCase):
    def _settings(self, base: str, site_id: str = "altsource_ms") -> SiteSettings:
        return SiteSettings(
            id=site_id,
            backend="moesekai",
            moesekai=MoesekaiSettings(site_base=base),
        )

    def test_interleaved_contexts_do_not_cross_talk(self):
        """Astra: A/B 交错请求始终访问各自假 transport."""
        a = build_runtime(
            SekaiSyncConfig(store_root=Path("/tmp/a"), sites=(self._settings("https://AAA.example"),))
        )
        b = build_runtime(
            SekaiSyncConfig(store_root=Path("/tmp/b"), sites=(self._settings("https://BBB.example"),))
        )
        with a.activate():
            before = current_endpoints().ALTSOURCE_MS_BASE
            with b.activate():
                inside = current_endpoints().ALTSOURCE_MS_BASE
            after = current_endpoints().ALTSOURCE_MS_BASE
        self.assertEqual(before, "https://AAA.example")
        self.assertEqual(inside, "https://BBB.example")
        self.assertEqual(
            after,
            "https://AAA.example",
            "the outer context saw the inner context's endpoints",
        )

    def test_context_is_restored_after_failure(self):
        """Astra: 失败退出后上下文恢复."""
        before = current_endpoints().ALTSOURCE_MS_BASE
        runtime = build_runtime(
            SekaiSyncConfig(store_root=Path("/tmp/a"), sites=(self._settings("https://AAA.example"),))
        )
        with self.assertRaises(RuntimeError):
            with runtime.activate():
                raise RuntimeError("boom")
        self.assertEqual(current_endpoints().ALTSOURCE_MS_BASE, before)

    def test_concurrent_threads_keep_their_own_endpoints(self):
        """A context-scoped value, not a global: threads must not share it."""
        a = build_runtime(
            SekaiSyncConfig(store_root=Path("/tmp/a"), sites=(self._settings("https://AAA.example"),))
        )
        b = build_runtime(
            SekaiSyncConfig(
                store_root=Path("/tmp/b"),
                sites=(
                    SiteSettings(
                        id="altsource_sv",
                        backend="sekai_viewer",
                        viewer=ViewerSettings(i18n_base="https://BBB.example"),
                    ),
                ),
            )
        )
        seen: dict[str, set] = {}

        def worker(name, runtime, attr):
            with runtime.activate():
                for _ in range(100):
                    seen.setdefault(name, set()).add(getattr(current_endpoints(), attr))

        threads = [
            threading.Thread(target=worker, args=("A", a, "ALTSOURCE_MS_BASE")),
            threading.Thread(target=worker, args=("B", b, "ALTSOURCE_SV_I18N_BASE")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(seen["A"], {"https://AAA.example"})
        self.assertEqual(seen["B"], {"https://BBB.example"})

    def test_missing_runtime_reports_instead_of_using_globals(self):
        """Astra: 缺 runtime 的联网方法必须清楚报未配置."""
        with self.assertRaises(RuntimeNotConfiguredError):
            require_endpoints(None, what="crawl")

    def test_runtime_is_frozen(self):
        """Shared across threads, so it must not be mutable."""
        runtime = build_runtime(
            SekaiSyncConfig(store_root=Path("/tmp/a"), sites=(self._settings("https://AAA.example"),))
        )
        with self.assertRaises(Exception):
            runtime.store_root = Path("/tmp/other")  # type: ignore[misc]

    def test_enabled_sites_filters_disabled_instances(self):
        runtime = build_runtime(
            SekaiSyncConfig(
                store_root=Path("/tmp/a"),
                sites=(
                    SiteSettings(id="on", backend="moesekai", enabled=True),
                    SiteSettings(id="off", backend="moesekai", enabled=False),
                ),
            )
        )
        self.assertEqual([s.id for s in runtime.enabled_sites()], ["on"])
        self.assertEqual(runtime.source_ids, ("on",))

    def test_endpoints_for_instance_returns_a_snapshot(self):
        runtime = build_runtime(
            SekaiSyncConfig(store_root=Path("/tmp/a"), sites=(self._settings("https://AAA.example"),))
        )
        self.assertEqual(
            runtime.endpoints_for("altsource_ms").ALTSOURCE_MS_BASE,
            "https://AAA.example",
        )

    def test_local_core_needs_no_runtime(self):
        """Pure local reads must keep working without network configuration."""
        import tempfile

        from sekaisync.cli import create_demo_store
        from sekaisync.core import SekaiSyncCore

        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "store"
            create_demo_store(store)
            core = SekaiSyncCore(store)  # no runtime
            self.assertIsNone(core.runtime)
            self.assertIsInstance(core.status(), dict)


class CoreRuntimeTest(unittest.TestCase):
    """P14 — Core's networked methods read their own runtime, not the global.

    These used to call ``apply_master_base``, which replaced the process-global
    endpoint snapshot: a second Core with different settings silently redirected
    the first one's in-flight work.
    """

    def _store(self) -> Path:
        import tempfile

        from sekaisync.cli import create_demo_store
        from sekaisync.config import SekaiSyncConfig as _Cfg
        from sekaisync.fetcher import sync

        tmp = tempfile.TemporaryDirectory(prefix="test_core_runtime_")
        self.addCleanup(tmp.cleanup)
        store = Path(tmp.name) / "store"
        create_demo_store(store)
        sync(_Cfg(store_root=store, regions=("demo",), demo=True), ["demo"])
        return store

    def _runtime(self, store: Path, base: str = "https://runtime.invalid"):
        site = SiteSettings(
            id="sv", backend="sekai_viewer",
            viewer=ViewerSettings(master_base=base, asset_base=base + "/assets",
                                  i18n_base=base + "/i18n"),
        )
        return build_runtime(SekaiSyncConfig(store_root=store, sites=(site,)))

    def test_networked_call_without_runtime_refuses_instead_of_going_global(self):
        from sekaisync.core import SekaiSyncCore

        core = SekaiSyncCore(self._store())
        with self.assertRaises(RuntimeNotConfiguredError):
            core.event_check(regions=["jp"], fetcher=lambda url: "{}")

    def test_networked_call_uses_its_own_runtime_and_restores_the_global(self):
        from sekaisync.core import SekaiSyncCore

        store = self._store()
        before = current_endpoints()
        core = SekaiSyncCore(store, runtime=self._runtime(store))
        seen = []
        core.progress(
            regions=["jp"], live=True,
            fetcher=lambda url: (seen.append(current_endpoints().ALTSOURCE_SV_MASTER_BASE), "[]")[1],
        )
        self.assertEqual(set(seen), {"https://runtime.invalid"})
        self.assertEqual(current_endpoints(), before, "the call leaked into the global snapshot")

    def test_two_cores_do_not_redirect_each_other(self):
        """The A/B case that motivated P14, now at the Core entry point."""
        from sekaisync.core import SekaiSyncCore

        store = self._store()
        a = SekaiSyncCore(store, runtime=self._runtime(store, "https://a.invalid"))
        b = SekaiSyncCore(store, runtime=self._runtime(store, "https://b.invalid"))
        seen = []
        fetcher = lambda url: (seen.append(current_endpoints().ALTSOURCE_SV_MASTER_BASE), "[]")[1]
        a.progress(regions=["jp"], live=True, fetcher=fetcher)
        b.progress(regions=["jp"], live=True, fetcher=fetcher)
        a.progress(regions=["jp"], live=True, fetcher=fetcher)
        self.assertEqual(seen, ["https://a.invalid", "https://b.invalid", "https://a.invalid"])


class CrawlScopeTest(unittest.TestCase):
    """The crawl entry points scope endpoints to their own settings."""

    def test_scope_uses_this_calls_settings(self):
        a = MoesekaiSettings(site_base="https://AAA.example")
        b = MoesekaiSettings(site_base="https://BBB.example")
        with _runtime_scope(moesekai=a, instance="altsource_ms"):
            self.assertEqual(
                current_endpoints().ALTSOURCE_MS_BASE, "https://AAA.example"
            )
            with _runtime_scope(moesekai=b, instance="altsource_ms"):
                self.assertEqual(
                    current_endpoints().ALTSOURCE_MS_BASE, "https://BBB.example"
                )
            self.assertEqual(
                current_endpoints().ALTSOURCE_MS_BASE,
                "https://AAA.example",
                "nested crawl scope leaked into the outer crawl",
            )

    def test_scope_without_settings_leaves_ambient_value(self):
        before = current_endpoints()
        with _runtime_scope():
            self.assertIs(current_endpoints(), before)

    def test_viewer_scope_sets_sv_endpoints(self):
        viewer = ViewerSettings(i18n_base="https://VVV.example")
        with _runtime_scope(viewer=viewer, instance="altsource_sv"):
            self.assertEqual(
                current_endpoints().ALTSOURCE_SV_I18N_BASE, "https://VVV.example"
            )

    def test_scope_restores_after_exception(self):
        before = current_endpoints().ALTSOURCE_MS_BASE
        with self.assertRaises(RuntimeError):
            with _runtime_scope(
                moesekai=MoesekaiSettings(site_base="https://AAA.example"),
                instance="altsource_ms",
            ):
                raise RuntimeError("boom")
        self.assertEqual(current_endpoints().ALTSOURCE_MS_BASE, before)


class EndpointsFromSitesTest(unittest.TestCase):
    def test_derives_snapshot_without_touching_globals(self):
        before = current_endpoints()
        snapshot = endpoints_from_sites(
            (
                SiteSettings(
                    id="altsource_ms",
                    backend="moesekai",
                    moesekai=MoesekaiSettings(site_base="https://AAA.example"),
                ),
            )
        )
        self.assertEqual(snapshot.ALTSOURCE_MS_BASE, "https://AAA.example")
        self.assertIs(
            current_endpoints(),
            before,
            "deriving a runtime snapshot mutated the process-global endpoints",
        )

    def test_disabled_instances_do_not_contribute(self):
        snapshot = endpoints_from_sites(
            (
                SiteSettings(
                    id="off",
                    backend="moesekai",
                    enabled=False,
                    moesekai=MoesekaiSettings(site_base="https://AAA.example"),
                ),
            )
        )
        self.assertEqual(snapshot.ALTSOURCE_MS_BASE, "")


if __name__ == "__main__":
    unittest.main()
