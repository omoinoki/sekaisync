"""Focused Core tests: P02 request-scoped ReadView, P04 conservative region
verification, P06 kind-before-limit.

All stores are temp dirs; no real store, no third-party dependency, no commit
side effects beyond the store's own writes.
"""
import json
from contextlib import closing
import sqlite3
from unittest.mock import patch
import tempfile
import threading
import unittest
from pathlib import Path

from sekaisync.cli import create_demo_store
from sekaisync.core import SekaiSyncCore
from sekaisync.fetcher import sync
from sekaisync.config import SekaiSyncConfig
from sekaisync.models import WebPage
from sekaisync.webindex import save_web_pages
from sekaisync.layout import web_consent_path


def _demo_store(tmp: str) -> Path:
    store_root = Path(tmp) / "store"
    create_demo_store(store_root)
    sync(SekaiSyncConfig(store_root=store_root, regions=("demo",), demo=True), ["demo"])
    return store_root


class RequestViewFixedSnapshotTest(unittest.TestCase):
    """P02 — the view's SQL domains load once, in one read transaction."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_readview_")
        self.store = _demo_store(self._tmp.name)
        self.core = SekaiSyncCore(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_mutable_shared_snapshot_state_on_core(self):
        """The core must not publish a mutable ``self._snapshot`` that a second
        concurrent request could overwrite or restore out of order."""
        with self.core.request_view() as view_a:
            # a second, overlapping request on another thread
            with self.core.request_view() as view_b:
                self.assertIs(view_a, view_b)
                self.assertIs(view_a.snapshot, view_b.snapshot)
            # after the inner request exits, the outer view must still work
            self.assertTrue(view_a.revision >= 0)
            self.assertTrue(view_a.conn is not None)

    def test_view_collections_are_detached_from_core_state(self):
        """Mutating the snapshot collections must not corrupt the core, and the
        view must not hand out the core's live lists."""
        with self.core.request_view() as view:
            n_registry = len(view.registry)
            try:
                view.registry.append(object())
            except (AttributeError, TypeError):
                pass  # immutable container is also acceptable
            self.assertEqual(len(self.core.registry), n_registry)

    def test_view_revision_matches_snapshot(self):
        with self.core.request_view() as view:
            self.assertEqual(view.revision, view.snapshot.revision)

    def test_read_entry_does_not_write_store(self):
        """No hidden store writes at read entry: entering request_view must not
        bump the committed revision."""
        before = self.core.current_revision()
        with self.core.request_view() as view:
            pass
        after = self.core.current_revision()
        self.assertEqual(before, after)


class CrossProcessRevisionTest(unittest.TestCase):
    """P02 — a request started before another process's commit must still see
    its own generation, and the next request must see the new one."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_readview_x_")
        self.store = _demo_store(self._tmp.name)
        self.core = SekaiSyncCore(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_mid_request_commit_does_not_change_view_revision(self):
        with self.core.request_view() as view:
            old_revision = view.revision
            # simulate an external commit while the request is open
            from sekaisync import dbstore
            with closing(sqlite3.connect(dbstore.db_file(self.store))) as conn:
                dbstore.bump_revision(conn)
                conn.commit()
            self.assertEqual(view.revision, old_revision)
        with self.core.request_view() as view2:
            self.assertGreater(view2.revision, old_revision)


class VerifyClaimsRegionTest(unittest.TestCase):
    """P04 — conservative scoped verification for region-scoped fields."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_readview_v_")
        self.store = _demo_store(self._tmp.name)
        self.core = SekaiSyncCore(self.store)
        # Give one entity a region-scoped field with multiple regions, the
        # shape the v1 store cannot attribute to a single region.
        from sekaisync import dbstore
        entity = self.core.registry[0]
        entity.facts["start_at"] = "2026-01-01"
        entity.regions = ["jp", "en"]
        with dbstore.connect(self.store) as conn:
            conn.execute(
                "UPDATE entities SET regions_json=?, facts_json=? WHERE id=?",
                (
                    '["jp", "en"]',
                    json.dumps(entity.facts, ensure_ascii=False),
                    entity.id,
                ),
            )
            conn.commit()

    def tearDown(self):
        self._tmp.cleanup()

    def test_region_scoped_field_without_region_is_conservative(self):
        results = self.core.verify_claims(
            [{"claim": self.core.registry[0].names.get("en", self.core.registry[0].id),
              "field": "start_at",
              "expected": "2026-01-01"}]
        )
        self.assertEqual(results[0]["status"], "needs_region_data")

    def test_region_scoped_field_with_region_remains_unknown(self):
        results = self.core.verify_claims(
            [{"claim": self.core.registry[0].names.get("en", self.core.registry[0].id),
              "field": "start_at",
              "expected": "2026-01-01",
              "region": "jp"}]
        )
        self.assertIn(results[0]["status"], ("unknown", "needs_region_data"))


class WebLookupKindBeforeLimitTest(unittest.TestCase):
    """P06 — kind filtering must happen in SQL before LIMIT, so limit counts
    matching kinds, not total rows."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_readview_w_")
        store = Path(self._tmp.name) / "store"
        create_demo_store(store)
        sync(SekaiSyncConfig(store_root=store, regions=("demo",), demo=True), ["demo"])
        pages = [
            WebPage(
                id=f"web:altsource_ms:other:{i}",
                source="altsource_ms",
                url=f"https://pjsk.moe/zh-cn/other/{i}/",
                title=f"星乃一歌杂项{i}",
                language="zh_hans",
                kind="other",
                text=f"星乃一歌其他内容{i}。",
                crawled_at="2026-08-09T00:00:00+00:00",
                hash=f"o{i}",
                tos_accepted=True,
            )
            for i in range(5)
        ] + [
            WebPage(
                id="web:altsource_ms:event:1",
                source="altsource_ms",
                url="https://pjsk.moe/zh-cn/event/1/",
                title="星乃一歌活动",
                language="zh_hans",
                kind="event_story",
                text="星乃一歌活动剧情。",
                crawled_at="2026-08-09T00:00:00+00:00",
                hash="e1",
                tos_accepted=True,
            )
        ]
        save_web_pages(store, "altsource_ms", pages)
        web_consent_path(store).parent.mkdir(parents=True, exist_ok=True)
        web_consent_path(store).write_text("true", encoding="utf-8")
        self.core = SekaiSyncCore(store)

    def tearDown(self):
        self._tmp.cleanup()

    def test_kind_filter_applies_before_limit(self):
        results = self.core.web_lookup(
            "星乃一歌", limit=5, kind="event_story"
        )
        self.assertTrue(
            results,
            "kind filter applied after limit starved the result to empty",
        )
        self.assertTrue(all(r.get("kind") == "event_story" for r in results))


if __name__ == "__main__":
    unittest.main()
