"""Independent stable-identity and current-body refresh checks for inline tweets."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sekaisync import crawler, dbstore, termindex, webindex


class TweetCompletenessReviewTests(unittest.TestCase):
    def collect(self, records, known=None, remaining=None, region="en"):
        pages = []

        def local_master(request_region, table, _fetcher):
            self.assertEqual(request_region, region)
            if table == "mysekaiCharacterTalks.json":
                return []
            self.assertEqual(table, "mysekaiCharacterTalkTweets.json")
            return records

        def forbidden_network(_url):
            self.fail("synthetic inline tweets must not fetch an asset or network URL")

        with mock.patch.object(crawler, "fetch_altsource_sv_master", side_effect=local_master):
            remaining = crawler._crawl_altsource_sv_mysekai(
                region, forbidden_network, pages, remaining, 0, known_ids=known, workers=1)
        return pages, remaining

    def versioned(self, page):
        item = webindex.web_page_to_dict(page)
        known = {page.id}
        return known | crawler._known_inline_text_versions({page.id: item}, known)

    def test_pure_id_legacy_cache_refreshes_once_without_forking_page_identity(self):
        record = dict(id=101, text="Alpha festival")
        original = self.collect([record])[0][0]
        refreshed = self.collect([record], {original.id})[0]
        self.assertEqual([page.id for page in refreshed], [original.id])
        self.assertNotIn("text-sha256", refreshed[0].id)
        self.assertEqual(termindex.page_story_key(webindex.web_page_to_dict(refreshed[0])), "mysekai_tweet:101")
        self.assertEqual(self.collect([record], self.versioned(refreshed[0]))[0], [])

    def test_equal_length_raw_change_refreshes_exact_id_and_version(self):
        original = self.collect([dict(id=101, text="Alpha festival")])[0][0]
        refreshed = self.collect([dict(id=101, text="Omega festival")], self.versioned(original))[0][0]
        self.assertEqual(refreshed.id, original.id)
        self.assertEqual(len(refreshed.text), len(original.text))
        self.assertNotEqual(refreshed.hash, original.hash)
        self.assertNotEqual(crawler._inline_text_version_key(refreshed.id, refreshed.text),
                            crawler._inline_text_version_key(original.id, original.text))
        self.assertEqual(self.collect([dict(id=101, text="Omega festival")], self.versioned(refreshed))[0], [])

    def test_budget_counts_nonempty_records_and_equal_bodies_keep_distinct_ids(self):
        records = [dict(id=100, text=" "), dict(id=101, text="Same sentence"),
                   dict(id=102, text="Same sentence"), dict(id=103, text="Later sentence")]
        pages, remaining = self.collect(records, remaining=2)
        self.assertEqual(remaining, 0)
        self.assertEqual([page.id.rsplit(":", 1)[1] for page in pages], ["101", "102"])
        self.assertEqual(pages[0].hash, pages[1].hash)
        next_pages, remaining = self.collect(records, known=self.versioned(pages[0]), remaining=1)
        self.assertEqual(remaining, 0)
        self.assertEqual([page.id.rsplit(":", 1)[1] for page in next_pages], ["102"])

    def test_version_tokens_are_exact_page_and_locale_scoped(self):
        original = self.collect([dict(id=101, text="Same sentence")])[0][0]
        different = self.collect([dict(id=101, text="\u540c\u4e00\u53e5\u5b50")], self.versioned(original), region="cn")[0]
        self.assertEqual(len(different), 1)
        self.assertNotEqual(different[0].id, original.id)
        self.assertEqual(different[0].language, "zh_hans")

    def test_inline_version_inventory_does_not_promote_unrelated_domains(self):
        items = {"home": dict(kind="home_line", text="Home"),
                 "tweet": dict(kind="mysekai_tweet", text="Tweet"),
                 "scenario": dict(kind="self_intro", text="Scenario"),
                 "untrusted": dict(kind="mysekai_tweet", text="Not known")}
        known = {"home", "tweet", "scenario"}
        self.assertEqual(crawler._known_inline_text_versions(items, known), {
            crawler._inline_text_version_key("home", "Home"),
            crawler._inline_text_version_key("tweet", "Tweet")})
        self.assertEqual(crawler._known_home_line_versions(items, known), {
            crawler._home_line_version_key("home", "Home")})

    def run_resume(self, store, records):
        def local_master(region, table, _fetcher):
            self.assertEqual(region, "en")
            return records if table == "mysekaiCharacterTalkTweets.json" else []

        def forbidden_network(_url):
            self.fail("isolated resume must use synthetic local master tables only")

        with mock.patch.object(crawler, "fetch_altsource_sv_master", side_effect=local_master):
            return crawler._crawl_altsource_sv_text(store, ["en"], depth=4, limit=0, delay=0,
                                                   fetcher=forbidden_network, workers=1,
                                                   resume=True, include_i18n=False)

    def test_real_resume_second_run_preserves_complete_page_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Path(temporary) / "store"
            dbstore.initialize(store)
            original = webindex.web_page_to_dict(self.collect([dict(id=101, text="Alpha festival")])[0][0])
            original["crawled_at"] = "2000-01-01T00:00:00+00:00"
            original["source_etag"] = "retained-existing-metadata"
            dbstore.upsert_web_pages(store, original["source"], [original])
            self.run_resume(store, [dict(id=101, text="Omega festival")])
            before = dbstore.existing_page_map(store, original["source"])
            self.assertEqual(set(before), {original["id"]})
            self.assertEqual(before[original["id"]]["text"], "Omega festival")
            self.assertNotEqual(before[original["id"]]["crawled_at"], original["crawled_at"])
            self.run_resume(store, [dict(id=101, text="Omega festival")])
            after = dbstore.existing_page_map(store, original["source"])
            self.assertEqual(json.dumps(after, sort_keys=True), json.dumps(before, sort_keys=True))

    def test_real_resume_retries_each_flagged_page_even_when_body_is_current(self):
        for flag in ("asset_mismatch", "untranslated", "content_language_mismatch"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as temporary:
                store = Path(temporary) / "store"
                dbstore.initialize(store)
                original = webindex.web_page_to_dict(self.collect([dict(id=101, text="Alpha festival")])[0][0])
                original[flag] = "mismatch" if flag == "asset_mismatch" else True
                original["crawled_at"] = "2000-01-01T00:00:00+00:00"
                dbstore.upsert_web_pages(store, original["source"], [original])
                self.run_resume(store, [dict(id=101, text="Alpha festival")])
                current = dbstore.existing_page_map(store, original["source"])[original["id"]]
                self.assertFalse(current[flag])
                self.assertEqual(current["text"], "Alpha festival")
                self.assertNotEqual(current["crawled_at"], original["crawled_at"])


if __name__ == "__main__":
    unittest.main()
