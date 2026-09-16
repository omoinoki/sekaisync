import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.integrity import cross_instance_reconciliation, run_integrity_check
from sekaisync.models import WebPage
from sekaisync.webindex import save_web_pages


class IntegrityTest(unittest.TestCase):
    def _write_pages(self, store_root: Path) -> None:
        save_web_pages(
            store_root,
            "altsource_ms",
            [
                WebPage(
                    id="web:altsource_ms:event_story:1:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/zh-cn/story/event/1/1/",
                    title="活动1-1",
                    language="zh_hans",
                    kind="event_story",
                    text="同一正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="a",
                ),
                WebPage(
                    id="web:altsource_ms:event_story:2:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/zh-cn/story/event/2/1/",
                    title="活动2-1",
                    language="zh_hans",
                    kind="event_story",
                    text="活动2正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="d",
                ),
            ],
        )
        save_web_pages(
            store_root,
            "altsource_sv",
            [
                WebPage(
                    id="web:altsource_sv:cn:event_story:1:1",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/1/1.asset",
                    title="活动1-1",
                    language="zh_hans",
                    kind="event_story",
                    text="同一正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="b",
                ),
                WebPage(
                    id="web:altsource_sv:cn:event_story:1:1:conflict",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/1/1-alt.asset",
                    title="活动1-1冲突",
                    language="zh_hans",
                    kind="event_story",
                    text="被改写的正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="c",
                ),
                WebPage(
                    id="web:altsource_sv:cn:event_story:2:1",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/2/1.asset",
                    title="活动2-1",
                    language="zh_hans",
                    kind="event_story",
                    text="活动2正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="e",
                ),
            ],
        )

    def test_untranslated_placeholder_does_not_create_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:ko:event_story:1:1",
                        source="altsource_ms",
                        url="https://pjsk.moe/ko-kr/story/event/1/1/",
                        title="活动1-1",
                        language="ko",
                        kind="event_story",
                        text="실제 번역문",
                        crawled_at="2026-08-11T00:00:00+00:00",
                        hash="a",
                    ),
                ],
            )
            save_web_pages(
                store_root,
                "altsource_sv",
                [
                    WebPage(
                        id="web:altsource_sv:kr:event_story:1:1",
                        source="altsource_sv",
                        url="https://storage.sekai.best/event_story/1/1.asset",
                        title="活动1-1",
                        language="ko",
                        kind="event_story",
                        text="[未翻译]",
                        untranslated=True,
                        untranslated_placeholder="[未翻译]",
                        crawled_at="2026-08-11T00:00:00+00:00",
                        hash="b",
                    ),
                ],
            )

            result = run_integrity_check(store_root, limit=10)
            self.assertEqual(result["summary"]["conflicts"], 0)
            self.assertEqual(result["summary"]["mirror_duplicates"], 0)

    def test_integrity_detects_mirror_duplicates_conflicts_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_pages(store_root)

            from sekaisync.webindex import load_existing_page_map
            for source in ("altsource_ms", "altsource_sv"):
                existing = load_existing_page_map(store_root, source)
                for page in existing.values():
                    if page["id"] == "web:altsource_ms:event_story:1:1":
                        page["text_hash"] = "wrong"
                    if page["id"] == "web:altsource_sv:cn:event_story:1:1:conflict":
                        page["asset_mismatch"] = "language_mismatch: expected zh_hans, text script mismatch"
                        page["scenario_id_mismatch"] = "ScenarioId event_01_02 != expected event_01_01"
                from sekaisync.dbstore import save_web_pages_full
                save_web_pages_full(store_root, source, list(existing.values()))

            result = run_integrity_check(store_root, limit=10)
            self.assertEqual(result["summary"]["mirror_duplicates"], 1)
            self.assertEqual(result["summary"]["conflicts"], 1)
            self.assertEqual(result["summary"]["hash_mismatches"], 1)
            self.assertEqual(result["summary"]["asset_mismatches"], 1)
            self.assertEqual(result["summary"]["scenario_id_mismatches"], 1)
            self.assertGreaterEqual(result["summary"]["issues"], 3)



    def test_auxiliary_pages_are_not_canonical_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms_translation",
                [
                    WebPage(
                        id="web:altsource_ms_translation:zh-cn:event_story:1:1:ja",
                        source="altsource_ms_translation",
                        url="https://translation.exmeaning.com/translation/eventStory/event_1.json",
                        title="event 1 episode 1",
                        language="ja",
                        kind="event_story",
                        text="原文",
                        crawled_at="2026-08-14T00:00:00+00:00",
                        hash="a",
                        auxiliary=True,
                        overlay=True,
                        translation_source="official_cn",
                    ),
                ],
            )
            result = run_integrity_check(store_root, limit=10)
            self.assertEqual(result["layers"]["web"]["canonical_missing"], 0)
            self.assertGreaterEqual(result["layers"]["web"]["canonical_not_applicable"], 1)


    def test_cross_instance_reconciliation_detects_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_pages(store_root)
            result = cross_instance_reconciliation(store_root, limit=10)
            # event 2 is identical across both instances (no drift);
            # event 1 has a conflicting rewritten copy on altsource_sv.
            self.assertEqual(result["summary"]["drift_keys"], 1)
            drift = [g for g in result["groups"] if g["drift"]]
            self.assertEqual(len(drift), 1)
            self.assertIn("altsource_ms", drift[0]["instances"])
            self.assertIn("altsource_sv", drift[0]["instances"])
            self.assertGreaterEqual(
                result["summary"]["drift_by_instance"]["altsource_sv"]["drift_pages"], 2
            )

    def test_integrity_report_includes_cross_instance_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_pages(store_root)
            result = run_integrity_check(store_root, limit=10)
            self.assertIn("cross_instance", result["layers"])
            self.assertIn(
                "cross_instance_drift", result["summary"]
            )
            self.assertGreaterEqual(result["summary"]["cross_instance_drift"], 1)


class IntegrityTotalsVsSamplesTest(unittest.TestCase):
    """P19 — report every problem found, not the size of the sample returned.

    Two defects are covered here:

    1. ``limit`` capped ``issues`` and the caller then reported
       ``len(that slice)`` as the total, so a store with many problems
       reported only ``limit`` of them and looked healthier than it was.
    2. ``str(item.get("text_hash")) or sha256_hex(...)`` turned a missing hash
       into the literal string ``"None"``, which is truthy — so the fallback
       never ran and two pages with completely different text were both
       hashed as ``"None"`` and judged mirrors instead of a conflict.
    """

    def _page(self, page_id, text, event_no, episode_no=1, source="altsource_ms", **kwargs):
        """canonical_key is DERIVED from the id/url, never set directly.

        ``canonical_key_for_page`` parses ``event_story:<n>:<m>`` out of the
        page id, so the fixture has to look like a real event-story page for
        any grouping to happen at all.
        """
        return WebPage(
            id=page_id,
            source=source,
            url=f"https://example.invalid/story/event/{event_no}/{episode_no}/",
            title=page_id,
            language="zh_hans",
            kind="event_story",
            text=text,
            crawled_at="2026-08-11T00:00:00+00:00",
            hash="h",
            **kwargs,
        )

    def test_missing_hash_on_different_text_is_a_conflict_not_a_mirror(self):
        """The regression that hid real differences behind a 'None' hash."""
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    self._page("web:ms:event_story:1:1", "正文 A", 1, text_hash=""),
                    self._page("web:ms:event_story:1:1:alt", "正文 B", 1, text_hash=""),
                ],
            )
            from sekaisync.integrity import verify_web_integrity

            web = verify_web_integrity(store_root, limit=10)
            self.assertGreater(web["canonical_keys"], 0, "fixture built no groups")
            self.assertEqual(
                web["conflict_groups_total"],
                1,
                "two different texts with no stored hash were treated as "
                "mirrors because both folded to the string 'None'",
            )
            self.assertEqual(web["mirror_duplicates"], 0)

    def test_same_text_without_hash_is_still_a_mirror(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    self._page("web:ms:event_story:1:1", "同一正文", 1, text_hash=""),
                    self._page("web:ms:event_story:1:1:alt", "同一正文", 1, text_hash=""),
                ],
            )
            from sekaisync.integrity import verify_web_integrity

            web = verify_web_integrity(store_root, limit=10)
            self.assertGreater(web["canonical_keys"], 0, "fixture built no groups")
            self.assertEqual(web["conflict_groups_total"], 0)
            self.assertEqual(web["mirror_duplicates"], 1)

    def test_none_and_empty_hash_behave_identically(self):
        """None and "" must not take different code paths."""
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    self._page("web:ms:event_story:1:1", "正文 A", 1, text_hash=""),
                    self._page("web:ms:event_story:1:1:alt", "正文 B", 1, text_hash=None),
                ],
            )
            from sekaisync.integrity import verify_web_integrity

            web = verify_web_integrity(store_root, limit=10)
            self.assertGreater(web["canonical_keys"], 0, "fixture built no groups")
            self.assertEqual(web["conflict_groups_total"], 1)

    def test_totals_do_not_shrink_with_the_sample_limit(self):
        """Astra: total=7 with sample_limit=2 must still report 7."""
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = []
            for i in range(7):
                pages.append(
                    self._page(f"web:ms:event_story:{i}:1", f"正文 {i}", i, text_hash="")
                )
                pages.append(
                    self._page(
                        f"web:ms:event_story:{i}:1:alt", f"不同 {i}", i, text_hash=""
                    )
                )
            save_web_pages(store_root, "altsource_ms", pages)

            from sekaisync.integrity import verify_web_integrity

            small = verify_web_integrity(store_root, limit=2)
            self.assertEqual(small["conflict_groups_total"], 7)
            self.assertEqual(len(small["conflict_group_samples"]), 2)
            self.assertTrue(small["conflict_groups_truncated"])

            wide = verify_web_integrity(store_root, limit=100)
            self.assertEqual(
                wide["conflict_groups_total"],
                7,
                "changing the sample limit changed the reported total",
            )
            self.assertFalse(wide["conflict_groups_truncated"])

    def test_run_integrity_check_summary_reports_true_total(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            pages = []
            for i in range(6):
                pages.append(
                    self._page(f"web:ms:event_story:{i}:1", f"正文 {i}", i, text_hash="")
                )
                pages.append(
                    self._page(
                        f"web:ms:event_story:{i}:1:alt", f"不同 {i}", i, text_hash=""
                    )
                )
            save_web_pages(store_root, "altsource_ms", pages)

            result = run_integrity_check(store_root, limit=2)
            summary = result["summary"]
            self.assertEqual(summary["conflicts"], 6)
            self.assertGreater(
                summary["issues"],
                len(result["issues"]),
                "summary reported the sample size as the problem total",
            )
            self.assertEqual(summary["issues_sample_count"], len(result["issues"]))
            self.assertTrue(summary["issues_truncated"])


if __name__ == "__main__":
    unittest.main()
