import unittest

from sekaisync.coverage import build_coverage, build_source_manifest


class CoverageTest(unittest.TestCase):
    def test_real_region_marks_metadata_available_and_story_text_partial(self):
        coverage = build_coverage("en", demo=False)
        self.assertEqual(coverage["master_db"]["status"], "available")
        self.assertEqual(coverage["official_localized_names"]["status"], "available")
        self.assertEqual(coverage["story_full_text"]["status"], "partial")
        self.assertEqual(coverage["binary_assets"]["status"], "missing")

    def test_web_text_makes_story_full_text_available(self):
        coverage = build_coverage(
            "en",
            demo=False,
            web_status={
                "enabled": True,
                "category_counts": {
                    "altsource_sv": {"event_story": 10}
                },
            },
        )
        self.assertEqual(coverage["web_text"]["status"], "available")
        self.assertEqual(coverage["story_full_text"]["status"], "available")

    def test_missing_master_marks_region_as_missing(self):
        coverage = build_coverage("en", demo=False, master_available=False)
        self.assertEqual(coverage["master_db"]["status"], "missing")
        self.assertEqual(coverage["official_localized_names"]["status"], "missing")
        self.assertEqual(coverage["story_metadata"]["status"], "missing")
        self.assertEqual(coverage["story_full_text"]["status"], "partial")

    def test_manifest_exposes_fetcher_and_source_urls(self):
        manifest = build_source_manifest()
        master = next(item for item in manifest if item["key"] == "master_db")
        self.assertEqual(master["fetcher"], "fetch_region")
        self.assertTrue(any("github.com/Sekai-World" in url for url in master["source_urls"]))

    def test_manifest_follows_configured_sites(self):
        from sekaisync.config import MoesekaiSettings, SiteSettings, ViewerSettings

        sites = [
            SiteSettings(
                id="altsource_ms",
                backend="moesekai",
                name="m",
                enabled=True,
                moesekai=MoesekaiSettings(
                    site_base="https://mirror.example.com",
                    sitemap_url="https://mirror.example.com/sitemap.xml",
                    metadata_bases=("https://mirror.example.com/metadata",),
                    asset_bases=("https://mirror.example.com/storage",),
                    news_base="https://mirror.example.com/news",
                ),
            ),
            SiteSettings(
                id="altsource_sv",
                backend="sekai_viewer",
                name="v",
                enabled=True,
                viewer=ViewerSettings(
                    master_base="https://viewer.local/master",
                    asset_base="https://viewer.local/assets",
                ),
            ),
        ]
        manifest = build_source_manifest(sites)
        by_key = {item["key"]: item for item in manifest}
        self.assertEqual(
            by_key["story_full_text"]["source_urls"],
            ["https://viewer.local/assets", "https://mirror.example.com/storage"],
        )
        self.assertIn(
            "https://mirror.example.com/sitemap.xml",
            by_key["web_text"]["source_urls"],
        )
        # Official news keeps the static GitHub master userInformations source
        # and appends the configured news mirror.
        self.assertEqual(
            by_key["official_news"]["source_urls"],
            ["https://sekai-world.github.io", "https://mirror.example.com/news"],
        )
        # The GitHub master fact layer never follows the site profile.
        self.assertTrue(
            any("github.com/Sekai-World" in url for url in by_key["master_db"]["source_urls"])
        )
        self.assertTrue(
            all("sekai-world.github.io" in url for url in by_key["story_metadata"]["source_urls"])
        )
        self.assertEqual(by_key["binary_assets"]["source_urls"], ["https://storage.sekai.best"])
        self.assertEqual(by_key["official_localized_names"]["source_urls"], [])


if __name__ == "__main__":
    unittest.main()
