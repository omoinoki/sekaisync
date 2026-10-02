import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from sekaisync import crawler, dbstore
from sekaisync.config import MoesekaiSettings, ViewerSettings
from sekaisync.webindex import load_web_pages, web_page_to_dict


class HomeLineCompletenessTests(unittest.TestCase):
    def setUp(self):
        for scope in (crawler._runtime_scope(moesekai=MoesekaiSettings(
                site_base="https://ms.fixture.invalid", metadata_bases=("https://metadata.fixture.invalid",),
                asset_bases=("https://assets.fixture.invalid",))),
                crawler._runtime_scope(viewer=ViewerSettings(master_base="https://master.fixture.invalid",
                                                            asset_base="https://sv-assets.fixture.invalid"))):
            scope.__enter__()
            self.addCleanup(scope.__exit__, None, None, None)

    def records(self, primary="First phrase", secondary="Second phrase"):
        return [dict(id=7, displayPhrase=primary, displayPhrase2=secondary)]

    def collect(self, backend, records, known=None, remaining=None):
        pages = []
        helper = getattr(crawler, "_crawl_altsource_" + backend + "_home_lines")
        master = "fetch_altsource_" + backend + "_master"

        def table(_region, name, _fetcher):
            return records if name == "characterArchiveVoices.json" else []

        args = ["en", "en-us"] if backend == "ms" else ["en"]
        with patch.object(crawler, master, side_effect=table):
            left = helper(*args, lambda url: self.fail("unexpected network request"), pages, remaining, 0, known)
        return pages, left

    def test_both_backends_keep_ordered_second_phrase_and_stable_page_identity(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                pages, left = self.collect(backend, self.records())
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0].text, "displayPhrase: First phrase\ndisplayPhrase2: Second phrase")
                self.assertTrue(pages[0].id.endswith(":home_line:7"))
                self.assertEqual(pages[0].kind, "home_line")
                self.assertIsNone(left)

    def test_secondary_only_and_repeated_identical_phrases_are_not_lost(self):
        for backend in ("ms", "sv"):
            for primary, secondary, expected in (("", "Second phrase", "displayPhrase2: Second phrase"),
                                                  ("Same", "Same", "displayPhrase: Same\ndisplayPhrase2: Same"),
                                                  (" First ", " Second ", "displayPhrase: First\ndisplayPhrase2: Second")):
                with self.subTest(backend=backend, primary=primary):
                    pages, _ = self.collect(backend, self.records(primary, secondary))
                    self.assertEqual(pages[0].text, expected)

    def test_empty_secondary_keeps_existing_primary_and_serif_behavior(self):
        self.assertEqual(crawler._home_line_text(dict(displayPhrase="First"), "displayPhrase"), "displayPhrase: First")
        self.assertEqual(crawler._home_line_text(dict(displayPhrase="First", displayPhrase2=None), "displayPhrase"), "displayPhrase: First")
        self.assertEqual(crawler._home_line_text(dict(serif="Normal", displayPhrase2="Not part of serif"), "serif"), "Normal")
        for backend in ("ms", "sv"):
            self.assertEqual(self.collect(backend, self.records("", ""))[0], [])

    def test_resume_refreshes_old_primary_only_and_then_skips_exact_current_text(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                original = self.collect(backend, self.records("First", ""))[0][0]
                original.text = "First"
                known = {original.id}
                old = {original.id: dict(kind="home_line", text=original.text)}
                known |= crawler._known_home_line_versions(old, known)
                refreshed, _ = self.collect(backend, self.records("First", "Second"), known)
                self.assertEqual(refreshed[0].id, original.id)
                self.assertEqual(refreshed[0].text, "displayPhrase: First\ndisplayPhrase2: Second")
                current = {original.id: dict(kind="home_line", text=refreshed[0].text)}
                known |= crawler._known_home_line_versions(current, known)
                self.assertEqual(self.collect(backend, self.records("First", "Second"), known)[0], [])
                changed, _ = self.collect(backend, self.records("First", "Updated"), known)
                self.assertEqual(changed[0].text, "displayPhrase: First\ndisplayPhrase2: Updated")
                self.assertNotEqual(changed[0].hash, refreshed[0].hash)

    def test_resume_removal_of_second_phrase_refreshes_and_page_budget_stays_per_record(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                original = self.collect(backend, self.records())[0][0]
                known = {original.id, crawler._home_line_version_key(original.id, original.text)}
                records = self.records("First phrase", "") + [dict(id=8, displayPhrase="Next", displayPhrase2="Response")]
                pages, remaining = self.collect(backend, records, known, remaining=1)
                self.assertEqual(remaining, 0)
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0].text, "displayPhrase: First phrase")

    def test_version_tokens_do_not_include_unusable_or_unrelated_pages(self):
        pages = dict(good=dict(kind="home_line", text="First"), bad=dict(kind="home_line", text="Bad"),
                     event=dict(kind="event_story", text="Event"))
        result = crawler._known_home_line_versions(pages, {"good", "event"})
        self.assertEqual(result, {crawler._home_line_version_key("good", "First")})

    def test_unchanged_public_crawls_recover_old_primary_only_page_and_resume_idempotently(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as temp:
                store = Path(temp) / "store"
                dbstore.initialize(store)
                state = self.records("First", "")
                original = self.collect(backend, state)[0][0]
                original.text = "First"
                dbstore.upsert_web_pages(store, original.source, [web_page_to_dict(original)])

                def fetch(url):
                    import json
                    return json.dumps(state if "characterArchiveVoices.json" in url else [])

                if backend == "ms":
                    run = lambda: crawler.crawl_altsource_ms(store, depth=4, locales=("en-us",), limit=0,
                                                             accept_tos=True, delay=0, fetcher=fetch, workers=1,
                                                             include_overlay=False)
                else:
                    run = lambda: crawler.crawl_altsource_sv(store, depth=4, regions=("en",), limit=0,
                                                             accept_tos=True, delay=0, fetcher=fetch, workers=1,
                                                             include_i18n=False)
                before = next(page for rows in load_web_pages(store).values() for page in rows if page["kind"] == "home_line")
                self.assertEqual(before["text"], "First")
                state[0]["displayPhrase2"] = "Second"
                run()
                after = next(page for rows in load_web_pages(store).values() for page in rows if page["kind"] == "home_line")
                self.assertEqual(after["id"], before["id"])
                self.assertEqual(after["text"], "displayPhrase: First\ndisplayPhrase2: Second")
                self.assertNotEqual(after["hash"], before["hash"])
                replay = run()
                self.assertEqual(replay["crawled_pages"], 0)


if __name__ == "__main__":
    unittest.main()
