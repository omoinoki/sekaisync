"""Large-document regressions: bounded reads with identical query contracts."""
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest.mock import patch

from sekaisync import dbstore, searchindex
from sekaisync.core import SekaiSyncCore
from sekaisync.webindex import web_search


class LargeRetrievalTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = Path(tmp.name) / "store"
        dbstore.initialize(self.store)

    def save(self, source, pages):
        dbstore.upsert_web_pages(self.store, source, pages)

    def test_candidate_iteration_preserves_scan_order_filters_and_content(self):
        for source in ("altsource_ms", "altsource_sv"):
            self.save(source, [{"id": str(i), "title": "needle", "text": "本文" * 12000,
                                "language": "ja" if i % 2 else "en", "auxiliary": i % 3 == 0}
                               for i in (9, 1, 6, 2, 10)])
        keys = [(source, str(i)) for source in ("altsource_sv", "altsource_ms") for i in (10, 6, 9)]
        for language in (None, "ja", "en"):
            for overlay in (False, True):
                with self.subTest(language=language, overlay=overlay):
                    kwargs = {"language": language, "include_overlay": overlay}
                    scan = [r for r in dbstore.iter_web_search_rows(self.store, **kwargs)
                            if (r["source"], r["id"]) in keys]
                    indexed = list(dbstore.iter_web_search_rows(self.store, keys=keys + keys[:1], **kwargs))
                    self.assertEqual(scan, indexed)
        self.assertEqual([], list(dbstore.iter_web_search_rows(self.store, keys=[])))
        self.assertEqual([], list(dbstore.iter_web_search_rows(self.store, keys=[("missing", "missing")])))

    def test_equal_sequence_ties_preserve_identifier_order(self):
        self.save("altsource_ms", [{"id": page_id, "title": "needle", "text": "text"}
                                  for page_id in ("z", "a", "c")])
        with dbstore.connect(self.store) as conn:
            conn.execute("UPDATE web_pages SET seq=0")
            conn.commit()
        keys = [("altsource_ms", page_id) for page_id in ("z", "a", "c")]
        self.assertEqual(["a", "c", "z"], [r["id"] for r in dbstore.iter_web_search_rows(self.store, keys=keys)])

    def test_indexed_path_does_not_retain_all_long_body_heads(self):
        self.save("altsource_ms", [{"id": str(i), "title": "needle", "text": "🙂" * 20000}
                                  for i in range(180)])
        keys = [("altsource_ms", str(i)) for i in range(180)]
        tracemalloc.start()
        try:
            count = sum(1 for _ in dbstore.iter_web_search_rows(self.store, keys=keys))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(count, 180)
        self.assertLess(peak, 3_000_000, "candidate rows retained multi-megabyte body heads")

    def test_bounded_text_keeps_unicode_nul_and_full_text_semantics(self):
        original = "A\0🙂日本語é" * 80
        self.save("altsource_ms", [{"id": "page", "text": original}])
        key = ("altsource_ms", "page")
        for budget in (1, 2, 3, 8, 79, len(original), len(original) + 1, 10**30):
            with self.subTest(budget=budget):
                self.assertEqual(original[:budget], dbstore.web_page_texts(self.store, [key], max_chars=budget)[key])
        self.assertEqual(original, dbstore.web_page_texts(self.store, [key])[key])
        self.assertEqual({}, dbstore.web_page_texts(self.store, [("x", "missing")], max_chars=8))

    def test_existing_core_budget_result_is_identical(self):
        original = "🙂A\0日本語" * 1000
        self.save("altsource_ms", [{"id": "page", "title": "rarephrase", "text": original}])
        core = SekaiSyncCore(self.store)
        unlimited = core.web_lookup("rarephrase", include_text=True)
        self.assertEqual(len(unlimited), 1)
        for budget in (1, 20, len(original), len(original) + 1):
            expected = [dict(unlimited[0])]
            if len(original) > budget:
                expected[0]["text"] = original[:budget] + "…"
            self.assertEqual(expected, core.web_lookup("rarephrase", include_text=True, max_text_chars=budget))

    def test_zero_limit_does_not_scan_or_crash(self):
        self.save("altsource_ms", [{"id": "page", "title": "needle", "text": "needle"}])
        with patch.object(searchindex, "candidates", side_effect=AssertionError("should not scan")):
            self.assertEqual([], web_search(self.store, "needle", limit=0))


if __name__ == "__main__":
    unittest.main()
