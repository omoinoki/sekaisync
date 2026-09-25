import unittest
from pathlib import Path

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

    def test_manifest_exposes_fetcher_and_resolved_source_urls(self):
        """Master URLs are resolved from config, not hardcoded.

        The manifest may only report addresses the install is configured to
        read, so an unconfigured profile yields no master URLs at all.
        """
        from sekaisync.config import SekaiSyncConfig

        manifest = build_source_manifest(sites=())
        master = next(item for item in manifest if item["key"] == "master_db")
        self.assertEqual(master["fetcher"], "fetch_region")
        self.assertEqual(
            master["source_urls"], [],
            "an unconfigured profile must not claim any master address",
        )

        # With a config, the same row resolves from github_tarball_base + repo_slug.
        config = SekaiSyncConfig(
            store_root=Path("."), github_tarball_base="https://github.com", sites=()
        )
        configured = next(
            item for item in build_source_manifest(config=config) if item["key"] == "master_db"
        )
        self.assertEqual(
            configured["source_urls"],
            [
                "https://github.com/Sekai-World/sekai-master-db-diff",
                "https://github.com/Sekai-World/sekai-master-db-en-diff",
                "https://github.com/Sekai-World/sekai-master-db-tc-diff",
                "https://github.com/Sekai-World/sekai-master-db-kr-diff",
                "https://github.com/Sekai-World/sekai-master-db-cn-diff",
            ],
        )
        self.assertNotIn(
            "sekai-world.github.io",
            " ".join(configured["source_urls"]),
            "the catalog must not carry a second hardcoded host",
        )

    def test_manifest_follows_configured_sites(self):
        from sekaisync.config import MoesekaiSettings, SekaiSyncConfig, SiteSettings, ViewerSettings

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
        config = SekaiSyncConfig(
            store_root=Path("."),
            github_tarball_base="https://github.com",
            sites=tuple(sites),
        )
        manifest = build_source_manifest(config=config)
        by_key = {item["key"]: item for item in manifest}
        self.assertEqual(
            by_key["story_full_text"]["source_urls"],
            ["https://viewer.local/assets", "https://mirror.example.com/storage"],
        )
        self.assertIn(
            "https://mirror.example.com/sitemap.xml",
            by_key["web_text"]["source_urls"],
        )
        # News follows the configured instances only: the mirror's news base and
        # the viewer's master base that serves region userInformations.json.
        self.assertEqual(
            by_key["official_news"]["source_urls"],
            ["https://mirror.example.com/news", "https://viewer.local/master"],
        )
        # The master fact layer resolves from config too, not from a literal.
        self.assertTrue(
            every.startswith("https://github.com/Sekai-World/")
            for every in by_key["master_db"]["source_urls"]
        )
        self.assertEqual(
            by_key["binary_assets"]["source_urls"],
            ["https://viewer.local/assets", "https://mirror.example.com/storage"],
        )
        self.assertEqual(by_key["official_localized_names"]["source_urls"], [])


if __name__ == "__main__":
    unittest.main()
