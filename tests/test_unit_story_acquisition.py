"""Acquisition and its denominator must include openings and later chapters."""
from pathlib import Path
import unittest
from unittest.mock import patch

from sekaisync import crawler, progress
from sekaisync.models import WebPage


class UnitStoryAcquisitionTests(unittest.TestCase):
    def stories(self):
        return [dict(unit="fixture", seq=1, chapters=[dict(assetbundleName="chapter1", episodes=[
            dict(scenarioId=f"unit_01_{number:02}", episodeNo=number + 1,
                 episodeNoLabel="opening" if number == 0 else str(number), title=f"Episode {number}")
            for number in range(25)])])]

    def page(self, server, locale, page_id, title, url, scenario, kind):
        return WebPage(id=page_id, source="fixture", url=url, title=title, language="en", kind=kind,
                       text=scenario["ScenarioId"], crawled_at="2026-10-01T00:00:00+00:00", hash="fixture")

    def sv_page(self, region, page_id, title, url, scenario, kind):
        return self.page(region, region, page_id, title, url, scenario, kind)

    def test_ms_fetches_opening_and_all_twenty_five_without_internal_cap(self):
        pages, requested = [], []

        def master(server, table, fetcher):
            return self.stories() if table == "unitStories.json" else [dict(unit="fixture", seq=1)]

        def scenario(server, path, fetcher, language):
            requested.append(path)
            return dict(ScenarioId=Path(path).stem)

        with patch.object(crawler, "fetch_altsource_ms_master", side_effect=master), \
                patch.object(crawler, "fetch_altsource_ms_scenario", side_effect=scenario), \
                patch.object(crawler, "altsource_ms_canonical_url", side_effect=lambda locale, path:
                             "https://fixture.invalid/" + path), \
                patch.object(crawler, "altsource_ms_scenario_page", side_effect=self.page):
            remaining = crawler._crawl_altsource_ms_unit_stories("en", "en", None, pages, None, 0)
        self.assertIsNone(remaining)
        self.assertEqual(len(pages), 25)
        self.assertTrue(requested[0].endswith("/unit_01_00.json"))
        self.assertTrue(requested[-1].endswith("/unit_01_24.json"))

    def test_sv_fetches_opening_and_later_chapters_with_explicit_user_budget(self):
        pages, requested = [], []

        def asset(region, paths, fetcher):
            requested.extend(paths)
            return "https://fixture.invalid/" + paths[0], dict(ScenarioId=Path(paths[0]).stem)

        with patch.object(crawler, "fetch_altsource_sv_master", return_value=self.stories()), \
                patch.object(crawler, "fetch_altsource_sv_asset", side_effect=asset), \
                patch.object(crawler, "altsource_sv_scenario_page", side_effect=self.sv_page):
            remaining = crawler._crawl_altsource_sv_unit_stories("en", None, pages, 23, 0)
        self.assertEqual(remaining, 0)
        self.assertEqual(len(pages), 23)
        self.assertTrue(requested[0].endswith("/unit_01_00.asset"))
        self.assertTrue(requested[-1].endswith("/unit_01_22.asset"))

    def test_known_pages_do_not_consume_a_hidden_per_unit_cap(self):
        known = {f"web:{crawler._current_sv_instance()}:en:unit_story:unit_01_{number:02}" for number in range(24)}
        pages = []
        with patch.object(crawler, "fetch_altsource_sv_master", return_value=self.stories()), \
                patch.object(crawler, "fetch_altsource_sv_asset", return_value=("https://fixture.invalid/later",
                             dict(ScenarioId="unit_01_24"))) as fetch, \
                patch.object(crawler, "altsource_sv_scenario_page", side_effect=self.sv_page):
            crawler._crawl_altsource_sv_unit_stories("en", None, pages, None, 0, known_ids=known)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(len(pages), 1)
        self.assertIn("unit_01_24", pages[0].id)

    def test_progress_inventory_has_all_twenty_five_including_opening(self):
        def records(store, region, table, **kwargs):
            return self.stories() if table == "unitStories" else []

        with patch.object(progress, "_load_records", side_effect=records):
            expected = progress.expected_text_units(Path("fixture"), "en", 0)
        self.assertEqual(expected["unit_story"], {f"unit_story:en:unit_01_{number:02}" for number in range(25)})


if __name__ == "__main__":
    unittest.main()
