"""Isolated regressions: no real store, database, or network access."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sekaisync.coverage import build_region_coverage, build_coverage
from sekaisync.progress import compute_progress
from sekaisync.integrity import run_integrity_check


class RegionCoverageTest(unittest.TestCase):
    def test_scoped_inputs_do_not_leak_between_regions(self):
        result = build_region_coverage(
            ["jp", "en"],
            web_status={"jp": {"enabled": True, "category_counts": {"mirror": {"event_story": 3}}}},
            news_available={"jp": True, "en": False},
            master_available={"jp": True, "en": False},
        )
        self.assertEqual(result["jp"]["web_text"]["status"], "available")
        self.assertEqual(result["en"]["web_text"]["status"], "unknown")
        self.assertEqual(result["en"]["official_news"]["status"], "missing")
        self.assertNotEqual(result["en"]["story_full_text"]["status"], "available")

    def test_global_positive_is_not_regional_evidence(self):
        result = build_region_coverage(["jp", "en"], web_status={"enabled": True}, news_available=True)
        for row in result.values():
            self.assertEqual(row["web_text"]["status"], "unknown")
            self.assertEqual(row["official_news"]["status"], "unknown")
            self.assertEqual(row["master_db"]["status"], "unknown")

    def test_missing_master_cannot_claim_complete_story_text(self):
        row = build_coverage("en", master_available=False, web_status={
            "enabled": True, "category_counts": {"mirror": {"event_story": 2}}})
        self.assertEqual(row["story_full_text"]["status"], "partial")


class ProgressStateTest(unittest.TestCase):
    def run_progress(self, root, fetcher, regions=("jp", "en"), live=True):
        with patch("sekaisync.progress.master_source_dir", side_effect=lambda store, region: root / region), \
             patch("sekaisync.progress.dbstore.load_entity_keys", return_value=[]), \
             patch("sekaisync.progress.dbstore.matched_text_keys", return_value=set()), \
             patch("sekaisync.progress.current_endpoints", return_value=SimpleNamespace(ALTSOURCE_SV_MASTER_BASE="https://fixture.invalid")), \
             patch("urllib.request.urlopen", side_effect=AssertionError("real network forbidden")):
            return compute_progress(root, regions=regions, now=5000, live=live, fetcher=fetcher)

    def test_live_failure_is_per_region_and_fetched_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "en").mkdir()
            (root / "en" / "events.json").write_text('[{"id":2,"startAt":1000}]', encoding="utf-8")
            calls = []
            def fetch(url):
                calls.append(url)
                if "-en-diff" in url:
                    raise OSError("sensitive detail must not be exposed")
                return '[{"id":1,"startAt":1000},{"id":3,"startAt":2000}]'
            result = self.run_progress(root, fetch)
            self.assertEqual(len(calls), 2)
            jp, en = result["regions"]["jp"], result["regions"]["en"]
            self.assertEqual(jp["activity"]["released_events"], 2)
            self.assertEqual(en["activity"]["released_events"], 1)
            self.assertEqual(jp["live_state"]["effective_source"], "live_events_local_tables")
            self.assertFalse(jp["live_state"]["degraded"])
            self.assertTrue(en["live_state"]["requested_live"])
            self.assertEqual(en["live_state"]["effective_source"], "local")
            self.assertTrue(en["live_state"]["degraded"])
            self.assertTrue(result["live_degraded"])
            self.assertNotIn("sensitive detail", json.dumps(result))
            self.assertIsNone(en["overall"]["pct"])
            self.assertIsNone(en["fact"]["pct"])
            self.assertIsNone(result["overall"]["pct"])
            self.assertIsNone(result["overall"]["fact"]["pct"])

    def test_valid_empty_live_is_not_fallback_and_cache_is_request_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            calls = []
            def fetch(url):
                calls.append(url)
                return "[]"
            for _ in range(2):
                result = self.run_progress(root, fetch, regions=("jp",))
                row = result["regions"]["jp"]
                self.assertFalse(row["live_state"]["degraded"])
                self.assertEqual(row["table_states"]["events"]["state"], "valid_empty")
            self.assertEqual(len(calls), 2)

    def test_invalid_live_and_missing_local_are_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            for payload in ("{}", "null", "[1]", "not json"):
                with self.subTest(payload=payload):
                    result = self.run_progress(Path(tmp), lambda url: payload, regions=("jp",))
                    row = result["regions"]["jp"]
                    self.assertEqual(row["live_state"]["effective_source"], "unknown")
                    self.assertEqual(row["table_states"]["events"]["state"], "missing")
                    self.assertTrue(row["live_state"]["degraded"])
                    self.assertIsNone(row["overall"]["pct"])

    def test_offline_missing_and_invalid_tables_are_distinct(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "jp").mkdir()
            (root / "jp" / "events.json").write_text("null", encoding="utf-8")
            result = self.run_progress(root, lambda url: self.fail("offline fetch"), live=False)
            self.assertEqual(result["regions"]["jp"]["table_states"]["events"]["state"], "invalid")
            self.assertEqual(result["regions"]["en"]["table_states"]["events"]["state"], "missing")
            self.assertFalse(result["live_degraded"])


    def test_complete_empty_tables_and_known_percentage(self):
        from sekaisync.progress import FACT_TABLES, TEXT_TABLES
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "jp").mkdir()
            for table in {value[0] for value in FACT_TABLES.values()} | set(TEXT_TABLES.values()):
                (root / "jp" / f"{table}.json").write_text("[]", encoding="utf-8")
            result = self.run_progress(root, None, regions=("jp",), live=False)
            self.assertTrue(result["regions"]["jp"]["denominator_complete"])
            self.assertIsNone(result["overall"]["pct"])
            (root / "jp" / "events.json").write_text('[{"id":1,"startAt":1000}]', encoding="utf-8")
            result = self.run_progress(root, None, regions=("jp",), live=False)
            self.assertEqual(result["overall"]["pct"], 0)
            self.assertEqual(result["regions"]["jp"]["fact"]["categories"]["event"]["pct"], 0)

    def test_default_transport_is_bounded_without_network(self):
        from sekaisync.progress import _default_fetcher
        from sekaisync.fetcher import BUDGET_JSON_BYTES
        with patch("sekaisync.fetcher.fetch_bytes", return_value=b"[]") as fetch:
            self.assertEqual(_default_fetcher("https://fixture.invalid", timeout=7), "[]")
            self.assertEqual(fetch.call_args.kwargs["max_bytes"], BUDGET_JSON_BYTES)
            self.assertEqual(fetch.call_args.kwargs["timeout"], 7)


class IntegritySampleTest(unittest.TestCase):
    def test_sample_count_and_truncation_use_final_slice(self):
        page = {"id": "web", "text": "text", "text_hash": "wrong"}
        duplicate = SimpleNamespace(id="duplicate")
        with patch("sekaisync.integrity.flatten_web_pages", return_value=[page]), \
             patch("sekaisync.integrity.dbstore.load_entities", return_value=[duplicate, duplicate]), \
             patch("sekaisync.integrity.dbstore.load_glossary_terms", return_value=[]), \
             patch("sekaisync.integrity.dbstore.load_terms_records", return_value=[]):
            for limit in (0, 1, 2, 10):
                result = run_integrity_check(Path("unused"), limit=limit)
                summary = result["summary"]
                self.assertEqual(summary["issues"], 2)
                self.assertEqual(summary["issues_sample_count"], len(result["issues"]))
                self.assertEqual(summary["issues_truncated"], len(result["issues"]) < 2)


if __name__ == "__main__":
    unittest.main()
