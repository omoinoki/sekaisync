"""Master URL suffix normalization must preserve real inline page provenance."""
import json
import unittest
from unittest.mock import patch

from sekaisync import crawler


class MasterJsonUrlReviewTests(unittest.TestCase):
    def setUp(self):
        token = crawler._sv_cache_root.set(None)
        self.addCleanup(crawler._sv_cache_root.reset, token)

    def expected_url(self, region, table):
        slug = crawler.REGIONS[region].repo_slug
        repository = slug.rsplit("/", 1)[-1] if slug else region
        return crawler._EP().ALTSOURCE_SV_MASTER_BASE + "/" + repository + "/" + table + ".json"

    def test_one_explicit_suffix_matches_bare_table_for_each_regional_repository(self):
        for region in ("jp", "en", "cn", "tc", "kr"):
            for table in ("characterArchiveVoices", "systemLive2ds", "mysekaiCharacterTalkTweets", "virtualLivePamphlets"):
                with self.subTest(region=region, table=table):
                    expected = self.expected_url(region, table)
                    self.assertEqual(crawler.altsource_sv_master_json_url(region, table), expected)
                    self.assertEqual(crawler.altsource_sv_master_json_url(region, table + ".json"), expected)

    def test_actual_inline_collectors_cite_fetched_origin_without_changing_identity_or_body(self):
        records = {
            "characterArchiveVoices": [dict(id=101, displayPhrase="Primary: detail", displayPhrase2="Secondary phrase")],
            "systemLive2ds": [dict(id=102, serif="System phrase")],
            "mysekaiCharacterTalkTweets": [dict(id=103, text="Tweet phrase")],
            "virtualLivePamphlets": [dict(id=104, name="Pamphlet title", flavorText="Pamphlet phrase")],
            "virtualLives": [],
            "mysekaiCharacterTalks": [],
        }
        expected_pages = {
            "home_line": (101, "displayPhrase: Primary: detail\ndisplayPhrase2: Secondary phrase", "characterArchiveVoices"),
            "systemLive2ds": (102, "System phrase", "systemLive2ds"),
            "mysekai_tweet": (103, "Tweet phrase", "mysekaiCharacterTalkTweets"),
            "virtualLivePamphlets": (104, "Pamphlet phrase", "virtualLivePamphlets"),
        }
        for region in ("jp", "en", "cn", "tc", "kr"):
            with self.subTest(region=region):
                responses = {self.expected_url(region, table): json.dumps(rows) for table, rows in records.items()}
                fetched = []

                def local_fetch(url):
                    self.assertIn(url, responses, "No asset, unknown table or double-suffix request is permitted")
                    fetched.append(url)
                    return responses[url]

                pages = []
                with crawler._sv_instance_scope(crawler.SOURCE_SV), patch.object(
                        crawler.fetcher_transport, "open_validated", side_effect=AssertionError("No network permitted")) as network:
                    self.assertIsNone(crawler._crawl_altsource_sv_home_lines(region, local_fetch, pages, None, 0))
                    self.assertIsNone(crawler._crawl_altsource_sv_virtual_lives(region, local_fetch, pages, None, 0, workers=1))
                    self.assertIsNone(crawler._crawl_altsource_sv_mysekai(region, local_fetch, pages, None, 0, workers=1))
                network.assert_not_called()
                self.assertEqual(len(fetched), 6)
                self.assertEqual(set(fetched), set(responses))
                self.assertEqual(len(pages), 4)
                self.assertEqual({page.kind for page in pages}, set(expected_pages))
                for page in pages:
                    identity, body, _table = expected_pages[page.kind]
                    self.assertEqual(page.id, f"web:{crawler.SOURCE_SV}:{region}:{page.kind}:{identity}")
                    self.assertEqual(page.text, body)
                    self.assertEqual(page.language, crawler.REGIONS[region].language)
                    self.assertEqual(page.hash, crawler._sha1(body, 16))
                self.assertEqual({page.kind: page.url for page in pages},
                                 {kind: self.expected_url(region, row[2]) for kind, row in expected_pages.items()})

    def test_explicit_suffix_fetch_and_raw_record_page_keep_exact_table_and_body_contract(self):
        record = dict(id=701, name="Raw name", nested=dict(value="Raw payload"))
        expected_text = json.dumps(record, ensure_ascii=False, indent=2)
        for table in ("events", "cards", "systemLive2ds"):
            with self.subTest(table=table):
                expected = self.expected_url("jp", table)
                calls = []

                def local_fetch(url):
                    self.assertEqual(url, expected)
                    calls.append(url)
                    return json.dumps([record])

                with patch.object(crawler.fetcher_transport, "open_validated", side_effect=AssertionError("No network permitted")) as network:
                    self.assertEqual(crawler.fetch_altsource_sv_master("jp", table, local_fetch), [record])
                    self.assertEqual(crawler.fetch_altsource_sv_master("jp", table + ".json", local_fetch), [record])
                network.assert_not_called()
                self.assertEqual(calls, [expected, expected])
                for spelling in (table, table + ".json"):
                    page = crawler.altsource_sv_record_page(record, spelling, "jp")
                    self.assertEqual(page.id, f"web:{crawler.SOURCE_SV}:jp:{spelling}:701")
                    self.assertEqual(page.kind, spelling)
                    self.assertEqual(page.text, expected_text)
                    self.assertEqual(page.hash, crawler._sha1(expected_text, 16))
                    self.assertEqual(page.url, expected)

    def test_suffix_normalization_preserves_interior_name_and_cache_provenance(self):
        expected = self.expected_url("jp", "custom.json.metadata")
        self.assertEqual(crawler.altsource_sv_master_json_url("jp", "custom.json.metadata"), expected)
        self.assertEqual(crawler.altsource_sv_master_json_url("jp", "custom.json.metadata.json"), expected)
        bare = crawler.altsource_sv_master_json_url("jp", "events")
        suffixed = crawler.altsource_sv_master_json_url("jp", "events.json")
        self.assertEqual(crawler._sv_master_cache_metadata(bare), crawler._sv_master_cache_metadata(suffixed))


if __name__ == "__main__":
    unittest.main()
