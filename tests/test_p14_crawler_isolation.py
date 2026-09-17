"""Offline P14 regressions: actual pool submission, instance and disk isolation."""
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from sekaisync import crawler as c
from sekaisync.config import MoesekaiSettings, SekaiSyncConfig, SiteSettings, ViewerSettings
from sekaisync.endpoints import SourceEndpoints, current_endpoints
from sekaisync.runtime import RuntimeContext, build_runtime


def site(name, host=None):
    host = host or name
    return SiteSettings(id=name, backend="sekai_viewer", viewer=ViewerSettings(
        master_base=f"https://{host}.invalid", asset_base=f"https://{host}.invalid/assets",
        i18n_base=f"https://{host}.invalid/i18n"))


class P14CrawlerIsolationTest(unittest.TestCase):
    def test_real_pool_propagates_endpoints_instance_and_cache(self):
        barrier = threading.Barrier(4)
        before = current_endpoints()
        with tempfile.TemporaryDirectory() as tmp:
            def crawl(name):
                root = Path(tmp) / name
                pages = []
                with c._runtime_scope(viewer=site(name).viewer, instance=name), c._sv_instance_scope(name):
                    token = c._sv_cache_root.set(root)
                    try:
                        def worker(item):
                            barrier.wait(timeout=10)
                            self.assertEqual(current_endpoints().ALTSOURCE_SV_MASTER_BASE, f"https://{name}.invalid")
                            self.assertEqual(c._sv_cache_root.get(), root)
                            return c.altsource_sv_record_page({"id": item, "name": name}, "events", "jp")
                        c._fetch_pages_parallel([1, 2], worker, pages, None, 2, 0)
                    finally:
                        c._sv_cache_root.reset(token)
                self.assertEqual({p.source for p in pages}, {name})
                self.assertTrue(all(p.id.startswith(f"web:{name}:") for p in pages))
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(crawl, name) for name in ("a", "b")]
                for future in futures:
                    future.result(timeout=20)
        self.assertIs(current_endpoints(), before)

    def test_per_instance_endpoints_and_primary_priority(self):
        runtime = build_runtime(SekaiSyncConfig(Path("unused"), sites=(site("a"), site("b"))))
        self.assertEqual(runtime.endpoints.ALTSOURCE_SV_MASTER_BASE, "https://a.invalid")
        self.assertEqual(runtime.endpoints_for("a").ALTSOURCE_SV_MASTER_BASE, "https://a.invalid")
        self.assertEqual(runtime.endpoints_for("b").ALTSOURCE_SV_MASTER_BASE, "https://b.invalid")
        with self.assertRaises(ValueError):
            runtime.endpoints_for("missing")

    def test_runtime_preserves_explicit_snapshot_and_freezes_profile_inputs(self):
        snapshot = SourceEndpoints(ALTSOURCE_SV_MASTER_BASE="https://explicit.invalid")
        self.assertIs(RuntimeContext(Path("unused"), snapshot).endpoints, snapshot)
        buckets = [["jp", "original"]]
        viewer = ViewerSettings(asset_buckets=buckets)
        buckets[0][1] = "changed"
        self.assertEqual(viewer.asset_buckets, (("jp", "original"),))
        sites = [site("a"), site("b")]
        runtime = build_runtime(SekaiSyncConfig(Path("unused"), sites=sites))
        sites.clear()
        self.assertEqual(len(runtime.sites), 2)
        self.assertNotEqual(runtime.cache_namespace_for("a"), runtime.cache_namespace_for("b"))
        moved = build_runtime(SekaiSyncConfig(Path("unused"), sites=(site("a", "moved"),)))
        self.assertNotEqual(runtime.cache_namespace_for("a"), moved.cache_namespace_for("a"))
        with self.assertRaises(TypeError):
            runtime._instance_endpoints["a"] = snapshot
        from dataclasses import replace
        disabled = build_runtime(SekaiSyncConfig(Path("unused"), sites=(replace(site("a"), enabled=False),)))
        with self.assertRaises(ValueError):
            disabled.endpoints_for("a")
        with self.assertRaises(ValueError):
            build_runtime(SekaiSyncConfig(Path("unused"), sites=(site("a"), site("a"))))

    def test_endpoint_mappings_are_defensive_immutable_copies(self):
        buckets = {"jp": "original"}
        endpoints = SourceEndpoints(ALTSOURCE_SV_ASSET_BUCKETS=buckets)
        buckets["jp"] = "changed"
        self.assertEqual(endpoints.ALTSOURCE_SV_ASSET_BUCKETS["jp"], "original")
        with self.assertRaises(TypeError):
            endpoints.ALTSOURCE_SV_ASSET_BUCKETS["jp"] = "mutated"

    def test_all_endpoint_mappings_are_defensive_immutable_copies(self):
        originals = {
            "ALTSOURCE_MS_LOCALE_SERVERS": {"jp": "original"},
            "ALTSOURCE_MS_LOCALE_LANGUAGES": {"jp": "original"},
            "ALTSOURCE_SV_ASSET_BUCKETS": {"jp": "original"},
        }
        for field, mapping in originals.items():
            mapping["jp"] = "original"
            endpoints = SourceEndpoints(**{field: mapping})
            mapping["jp"] = "changed"
            self.assertEqual(getattr(endpoints, field)["jp"], "original", field)
            with self.assertRaises(TypeError):
                getattr(endpoints, field)["jp"] = "mutated"

    def test_settings_list_inputs_are_defensively_copied(self):
        bases = ["https://metadata.invalid"]
        pairs = [["jp", "bucket"]]
        ms = MoesekaiSettings(metadata_bases=bases, locale_servers=pairs, locale_languages=pairs)
        viewer = ViewerSettings(asset_buckets=pairs)
        bases[0] = "changed"
        for pair in pairs:
            pair[1] = "changed"
        self.assertEqual(ms.metadata_bases, ("https://metadata.invalid",))
        self.assertEqual(ms.locale_servers, (("jp", "bucket"),))
        self.assertEqual(ms.locale_languages, (("jp", "bucket"),))
        self.assertEqual(viewer.asset_buckets, (("jp", "bucket"),))

    def test_ms_settings_scope_carries_viewer_cdn_fallback_from_ambient(self):
        # A settings-only Moesekai scope derives endpoints from fresh defaults,
        # so a fallback to the Viewer CDN must still honor the ambient/default
        # Viewer asset base instead of an empty one.
        self.assertTrue(MoesekaiSettings().fallback_to_viewer_cdn)
        calls = []
        with c._runtime_scope(moesekai=MoesekaiSettings(asset_bases=()), instance="ms"):
            c.fetch_altsource_ms_scenario(
                "jp", "scenario/test.json",
                lambda url: calls.append(url) or "{}")
        self.assertEqual(len(calls), 1)
        # Asset URLs carry the cache-bust marker; the base is what matters here.
        url, _, marker = calls[0].partition("?v=")
        self.assertEqual(url, f"{ViewerSettings().asset_base}/sekai-jp-assets/scenario/test.asset")
        self.assertTrue(marker.isdigit(), calls[0])

    def test_ms_runtime_scope_uses_runtime_viewer_cdn_fallback(self):
        calls = []
        with tempfile.TemporaryDirectory() as tmp:
            ms = SiteSettings(id="ms", backend="moesekai",
                              moesekai=MoesekaiSettings(asset_bases=()))
            sv = SiteSettings(id="sv", backend="sekai_viewer",
                              viewer=ViewerSettings(asset_base="https://cdn.invalid"))
            runtime = build_runtime(SekaiSyncConfig(Path(tmp), sites=(ms, sv)))
            with c._runtime_scope(runtime=runtime, instance="ms"):
                c.fetch_altsource_ms_scenario(
                    "jp", "scenario/test.json",
                    lambda url: calls.append(url) or "{}")
        self.assertEqual(len(calls), 1)
        url, _, marker = calls[0].partition("?v=")
        self.assertEqual(url, "https://cdn.invalid/sekai-jp-assets/scenario/test.asset")
        self.assertTrue(marker.isdigit(), calls[0])

    def test_public_crawl_restores_cache_scope_on_success_and_failure(self):
        before = current_endpoints()
        sentinel = Path("outer-cache")
        token = c._sv_cache_root.set(sentinel)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                for failure in (False, True):
                    def impl(*args, **kwargs):
                        self.assertEqual(c._sv_cache_root.get(), Path(tmp))
                        if failure:
                            raise RuntimeError("injected")
                        return {"source": c._current_sv_instance()}
                    with patch.object(c, "_crawl_altsource_sv_impl", side_effect=impl):
                        if failure:
                            with self.assertRaisesRegex(RuntimeError, "injected"):
                                c.crawl_altsource_sv(Path(tmp), settings=site("a").viewer, instance="a")
                        else:
                            self.assertEqual(c.crawl_altsource_sv(Path(tmp), settings=site("a").viewer, instance="a"), {"source": "a"})
                    self.assertEqual(c._sv_cache_root.get(), sentinel)
                    self.assertIs(current_endpoints(), before)
        finally:
            c._sv_cache_root.reset(token)

    def test_cache_separates_hosts_and_instances_and_checks_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "cache/sv_master/sekai-master-db-diff/events.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text('[{"id": "legacy"}]', encoding="utf-8")
            paths = []
            token = c._sv_cache_root.set(root)
            try:
                for name, host in (("a", "a"), ("a", "b"), ("b", "b")):
                    with c._runtime_scope(viewer=site(name, host).viewer, instance=name), c._sv_instance_scope(name):
                        calls = []
                        def fetch(url):
                            calls.append(url)
                            return json.dumps([{"id": name + host}])
                        self.assertEqual(c.fetch_altsource_sv_master("jp", "events", fetch), [{"id": name + host}])
                        self.assertEqual(c.fetch_altsource_sv_master("jp", "events", fetch), [{"id": name + host}])
                        self.assertEqual(len(calls), 1)
                        path = c._sv_master_cache_path(c.altsource_sv_master_json_url("jp", "events"))
                        paths.append(path)
                        payload = json.loads(path.read_text(encoding="utf-8"))
                        self.assertEqual(payload["metadata"]["instance"], name)
                        self.assertEqual(payload["metadata"]["url"], calls[0])
                        payload["metadata"]["instance"] = "wrong"
                        path.write_text(json.dumps(payload), encoding="utf-8")
                        c.fetch_altsource_sv_master("jp", "events", fetch)
                        self.assertEqual(len(calls), 2)
                self.assertEqual(len(set(paths)), 3)
                self.assertEqual(legacy.read_text(encoding="utf-8"), '[{"id": "legacy"}]')
            finally:
                c._sv_cache_root.reset(token)

    def test_two_public_runtime_crawls_use_actual_scenario_workers(self):
        barrier = threading.Barrier(4)
        before = current_endpoints()
        with tempfile.TemporaryDirectory() as tmp:
            def crawl(name):
                root = Path(tmp) / name
                calls = []
                def fetch(url):
                    self.assertTrue(url.startswith(f"https://{name}.invalid/"), url)
                    calls.append(url)
                    if url.endswith("eventStories.json"):
                        return json.dumps([{"id": 1, "assetbundleName": "event_1",
                            "eventStoryEpisodes": [{"episodeNo": n, "scenarioId": f"event_1_{n}"}
                                                   for n in (1, 2)]}])
                    barrier.wait(timeout=10)
                    self.assertEqual(c._current_sv_instance(), name)
                    self.assertEqual(c._sv_cache_root.get(), root)
                    self.assertEqual(c._cache_namespace.get(), runtime.cache_namespace_for(name))
                    return json.dumps({"TalkData": [{"WindowDisplayName": "テスト", "Body": "こんにちは"}]})
                runtime = build_runtime(SekaiSyncConfig(root, sites=(site(name),)), fetcher=fetch)
                result = c.crawl_altsource_sv(root, runtime=runtime, limit=2, workers=2, delay=0,
                    include_i18n=False, tos_already_checked=True, resume=False)
                self.assertEqual(result["source"], name)
                from sekaisync.webindex import load_web_pages
                pages = load_web_pages(root)[name]
                self.assertEqual(len(pages), 2)
                self.assertTrue(all(p["source"] == name and p["id"].startswith(f"web:{name}:") for p in pages))
                self.assertEqual(len(calls), 3)
                self.assertIsNone(c._sv_cache_root.get())
                self.assertEqual(c._cache_namespace.get(), "")
                self.assertEqual(list(root.rglob("*.tmp")), [])
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(crawl, name) for name in ("a", "b")]
                for future in futures:
                    future.result(timeout=20)
        self.assertIs(current_endpoints(), before)

    def test_cache_concurrent_same_key_and_failed_replace_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            barrier = threading.Barrier(2)
            def write():
                token = c._sv_cache_root.set(root)
                try:
                    with c._runtime_scope(viewer=site("a").viewer), c._sv_instance_scope("a"):
                        def fetch(url):
                            barrier.wait(timeout=10)
                            return '[{"id": 1}]'
                        return c.fetch_altsource_sv_master("jp", "events", fetch)
                finally:
                    c._sv_cache_root.reset(token)
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(write) for _ in range(2)]
                self.assertEqual([f.result(timeout=20) for f in futures], [[{"id": 1}], [{"id": 1}]])
            self.assertEqual(list(root.rglob("*.tmp")), [])
            self.assertEqual(len(list((root / "cache/sv_master/v2").glob("*.json"))), 1)
            token = c._sv_cache_root.set(root)
            try:
                with c._runtime_scope(viewer=site("b").viewer), patch.object(Path, "replace", side_effect=OSError("injected")):
                    self.assertEqual(c.fetch_altsource_sv_master("jp", "events", lambda url: '[{"id": 2}]'), [{"id": 2}])
                self.assertEqual(list(root.rglob("*.tmp")), [])
            finally:
                c._sv_cache_root.reset(token)

    def test_runtime_public_crawl_uses_selected_transport_without_global_mutation(self):
        before = current_endpoints()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            def fetch(url):
                calls.append(url)
                return '[{"id": 1, "name": "offline"}]'
            runtime = build_runtime(SekaiSyncConfig(root, sites=(site("a"), site("b"))), fetcher=fetch)
            result = c.crawl_altsource_sv(root, runtime=runtime, instance="b", tables=["events"],
                                         tos_already_checked=True, include_i18n=False)
            self.assertEqual(result["source"], "b")
            self.assertTrue(all(url.startswith("https://b.invalid/") for url in calls))
            self.assertTrue(calls)
            self.assertIs(current_endpoints(), before)
            self.assertIsNone(c._sv_cache_root.get())
            for instance in ("missing",):
                with self.assertRaises(ValueError):
                    c.crawl_altsource_sv(root, runtime=runtime, instance=instance)
            with self.assertRaises(ValueError):
                c.crawl_altsource_sv(root / "other", runtime=runtime)

    def test_settings_crawl_does_not_change_global_endpoints(self):
        before = current_endpoints()
        with tempfile.TemporaryDirectory() as tmp:
            c.crawl_altsource_sv(Path(tmp), settings=site("a").viewer, instance="a",
                                tables=["events"], fetcher=lambda url: "[]",
                                tos_already_checked=True, include_i18n=False)
        self.assertIs(current_endpoints(), before)

    def test_worker_failure_restores_context_on_reused_thread(self):
        with tempfile.TemporaryDirectory() as tmp:
            with c._runtime_scope(viewer=site("a").viewer), c._sv_instance_scope("a"):
                task = c.CrawlTaskContext.capture()
            def check():
                before = (current_endpoints(), c._current_sv_instance(), c._sv_cache_root.get())
                def fail(item):
                    self.assertEqual(c._current_sv_instance(), "a")
                    raise RuntimeError("worker failed")
                with self.assertRaisesRegex(RuntimeError, "worker failed"):
                    task.run(fail, None)
                self.assertEqual((current_endpoints(), c._current_sv_instance(), c._sv_cache_root.get()), before)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(check).result(timeout=10)
                pool.submit(check).result(timeout=10)

    def test_task_run_restores_all_scoped_values_including_namespace(self):
        sentinel_ep = current_endpoints()
        sentinel = (sentinel_ep, "ms-out", "sv-out", Path("outer-cache"), "outer-namespace")
        # The out-of-scope identity the task must not leak into: set explicitly,
        # so the final assertion checks restoration instead of whatever the
        # process defaults happen to be.
        ms_token = c._cv_ms_instance.set(sentinel[1])
        sv_token = c._cv_sv_instance.set(sentinel[2])
        tokens = [
            c._sv_cache_root.set(sentinel[3]),
            c._cache_namespace.set(sentinel[4]),
        ]
        # Asserted inside the try, before the sentinels are reset: these are
        # the values that must be in force after the scoped block exits.
        self.assertEqual(
            (current_endpoints(), c._current_ms_instance(), c._current_sv_instance()),
            (sentinel_ep, "ms-out", "sv-out"))
        try:
            runtime = build_runtime(SekaiSyncConfig(
                Path("unused"), sites=(site("a"),)),
                fetcher=lambda url: "")
            with c._runtime_scope(runtime=runtime, instance="a"), \
                    c._ms_instance_scope("ms-task"), c._sv_instance_scope("sv-task"):
                task = c.CrawlTaskContext.capture()
                expected = (current_endpoints(), "ms-task", "sv-task",
                            Path("unused"), runtime.cache_namespace_for("a"))
            def check():
                before = (current_endpoints(), c._current_ms_instance(),
                          c._current_sv_instance(), c._sv_cache_root.get(),
                          c._cache_namespace.get())
                for outcome in ("ok", "fail"):
                    def worker(item):
                        self.assertEqual(
                            (current_endpoints(), c._current_ms_instance(),
                             c._current_sv_instance(), c._sv_cache_root.get(),
                             c._cache_namespace.get()), expected)
                        if outcome == "fail":
                            raise RuntimeError("worker failed")
                        return item
                    if outcome == "fail":
                        with self.assertRaisesRegex(RuntimeError, "worker failed"):
                            task.run(worker, None)
                    else:
                        self.assertEqual(task.run(worker, "ok"), "ok")
                    self.assertEqual(
                        (current_endpoints(), c._current_ms_instance(),
                         c._current_sv_instance(), c._sv_cache_root.get(),
                         c._cache_namespace.get()), before)
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(check).result(timeout=10)
                pool.submit(check).result(timeout=10)
        finally:
            c._cache_namespace.reset(tokens[1])
            c._sv_cache_root.reset(tokens[0])
            c._cv_sv_instance.reset(sv_token)
            c._cv_ms_instance.reset(ms_token)

    def test_both_public_crawls_restore_all_scoped_values_on_success_and_failure(self):
        tokens = [c._sv_cache_root.set(Path("outer-cache")),
                  c._cache_namespace.set("outer-namespace")]
        try:
            # Captured after the sentinels are installed: the crawl must restore
            # the values in effect when it was entered, not the process defaults.
            before = (current_endpoints(), c._current_ms_instance(), c._current_sv_instance(),
                      c._sv_cache_root.get(), c._cache_namespace.get())
            with tempfile.TemporaryDirectory() as tmp:
                cases = [
                    ("crawl_altsource_sv", "_crawl_altsource_sv_impl", "sv", {"source": "sv"},
                     c._current_sv_instance, c._current_ms_instance),
                    ("crawl_altsource_ms", "_crawl_altsource_ms_impl", "ms", {},
                     c._current_ms_instance, c._current_sv_instance),
                ]
                for public_name, impl_name, instance, ok_result, own, other in cases:
                    public = getattr(c, public_name)
                    ms = MoesekaiSettings(site_base="https://ms.invalid")
                    settings = site("sv").viewer if own is c._current_sv_instance else ms
                    other_before = other()
                    for failure in (False, True):
                        def impl(*args, **kwargs):
                            self.assertEqual(c._sv_cache_root.get(), Path(tmp))
                            # Identity follows backend: a crawl sets its own
                            # backend's instance id and leaves the other
                            # backend's id alone.
                            self.assertEqual(own(), instance)
                            self.assertEqual(other(), other_before)
                            if failure:
                                raise RuntimeError("injected")
                            return ok_result
                        with patch.object(c, impl_name, side_effect=impl):
                            call = lambda: public(Path(tmp), settings=settings,
                                                  instance=instance, fetcher=lambda url: "")
                            if failure:
                                with self.assertRaisesRegex(RuntimeError, "injected"):
                                    call()
                            else:
                                self.assertEqual(call(), ok_result)
                        self.assertEqual(
                            (current_endpoints(), c._current_ms_instance(),
                             c._current_sv_instance(), c._sv_cache_root.get(),
                             c._cache_namespace.get()), before, (public_name, failure))
        finally:
            c._cache_namespace.reset(tokens[1])
            c._sv_cache_root.reset(tokens[0])

    def test_cache_key_keeps_content_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            token = c._sv_cache_root.set(Path(tmp))
            try:
                a = c._sv_master_cache_path("https://a.invalid/sekai-master-db-diff/events.json?version=1")
                b = c._sv_master_cache_path("https://a.invalid/sekai-master-db-diff/events.json?version=2")
                self.assertNotEqual(a, b)
                self.assertNotIn("?", a.name)
            finally:
                c._sv_cache_root.reset(token)


if __name__ == "__main__":
    unittest.main()
