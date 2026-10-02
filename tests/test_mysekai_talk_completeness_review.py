"""Independent finite-transport MySEKAI talk checks through the public crawl."""
from collections import Counter
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlparse

from sekaisync import crawler, dbstore, termindex, webindex
from sekaisync.config import ViewerSettings


REGION_TEXT = {
    "jp": ("\u4e00\u6b4c", "\u56db\u756a\u76ee\u306e\u4f1a\u8a71\u306f\u6b63\u3057\u304f\u8aad\u3081\u307e\u3059"),
    "en": ("Ichika", "The fourth dialogue is genuinely available"),
    "cn": ("\u4e00\u6b4c", "\u7b2c\u56db\u6761\u5bf9\u8bdd\u786e\u5b9e\u53ef\u4ee5\u8bfb\u53d6"),
    "tc": ("\u4e00\u6b4c", "\u7b2c\u56db\u689d\u5c0d\u8a71\u78ba\u5be6\u53ef\u4ee5\u8b80\u53d6"),
    "kr": ("\uc774\uce58\uce74", "\ub124 \ubc88\uc9f8 \ub300\ud654\ub97c \uc815\ud655\ud788 \uc77d\uc744 \uc218 \uc788\uc2b5\ub2c8\ub2e4"),
}


def record(identity, lua=None):
    return dict(id=identity, assetbundleName="mysekai/talk/scenario/talk", lua=lua or f"review_{identity}")


class _PublicFixture:
    def __init__(self, owner, *, region="en", records=None, assets=None):
        self.owner, self.region = owner, region
        temporary = tempfile.TemporaryDirectory()
        owner.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name) / "store"
        dbstore.initialize(self.store)
        label, body = REGION_TEXT[region]
        self.body = label + "\uff1a" + body
        self.lua = 'label(' + json.dumps(label, ensure_ascii=False) + ')\ntext(' + json.dumps(body, ensure_ascii=False) + ')\nwait_click()\n'
        tables = {
            "eventStories", "unitStories", "cards", "cardEpisodes", "actionSets", "specialStories",
            "characterProfiles", "virtualLives", "virtualLiveSetlists", "virtualLivePamphlets",
            "characterArchiveVoices", "systemLive2ds", "mysekaiCharacterTalks", "mysekaiCharacterTalkTweets",
            *crawler.ALTSOURCE_SV_OTHER_TEXT_TABLES,
        }
        self.masters = {table + ".json": [] for table in tables}
        self.records = list(records if records is not None else [record(i) for i in range(1, 6)])
        self.masters["mysekaiCharacterTalks.json"] = self.records
        self.assets = dict(assets if assets is not None else {f"review_{i}": None if i <= 3 else self.lua for i in range(1, 6)})
        self.calls = []
        self.lock = threading.Lock()
        self.settings = ViewerSettings(master_base="https://fixture.invalid/master", asset_base="https://fixture.invalid/assets")

    def fetch(self, url):
        parsed = urlparse(url)
        self.owner.assertEqual(parsed.hostname, "fixture.invalid")
        if parsed.path.startswith("/master/"):
            table = parsed.path.rsplit("/", 1)[-1]
            self.owner.assertIn(table, self.masters)
            with self.lock:
                self.calls.append(("master", table, parsed.path))
            return json.dumps(self.masters[table], ensure_ascii=False)
        prefix = f"/assets/sekai-{self.region}-assets/mysekai/talk/scenario/talk/"
        self.owner.assertTrue(parsed.path.startswith(prefix), parsed.path)
        self.owner.assertTrue(parsed.path.endswith(".lua.txt"), parsed.path)
        name = parsed.path[len(prefix):-len(".lua.txt")]
        self.owner.assertIn(name, self.assets, "Unexpected asset obligation or unsafe metadata escaped validation")
        with self.lock:
            self.calls.append(("asset", name, parsed.path))
        content = self.assets[name]
        if content is None:
            raise HTTPError(url, 404, "Synthetic exact asset absent", {}, None)
        return content

    def crawl(self, *, limit=0, workers=1, resume=False):
        def deny_network(*args, **kwargs):
            self.owner.fail("The injected public fixture attempted a real network request")

        with mock.patch.object(crawler, "fetch_http_text", side_effect=deny_network), \
                mock.patch("urllib.request.urlopen", side_effect=deny_network), redirect_stdout(io.StringIO()):
            return crawler.crawl_altsource_sv(self.store, regions=[self.region], limit=limit,
                accept_tos=True, delay=0, fetcher=self.fetch, depth=4, workers=workers,
                resume=resume, include_i18n=False, settings=self.settings)

    def asset_counts(self):
        return Counter(call[1] for call in self.calls if call[0] == "asset")

    def pages(self):
        return webindex.load_web_pages(self.store).get("altsource_sv", [])

    def assert_talks(self, identities):
        pages = self.pages()
        self.owner.assertEqual({page["id"] for page in pages},
                               {f"web:altsource_sv:{self.region}:mysekai_talk:{identity}" for identity in identities})
        for page in pages:
            identity = page["id"].rsplit(":", 1)[-1]
            self.owner.assertEqual(page["kind"], "mysekai_talk")
            self.owner.assertEqual(page["source"], "altsource_sv")
            self.owner.assertEqual(page["language"], crawler.REGIONS[self.region].language)
            self.owner.assertEqual(page["title"], identity)
            self.owner.assertEqual(page["text"], self.body)
            self.owner.assertEqual(page["hash"], hashlib.sha1(self.body.encode("utf-8")).hexdigest()[:16])
            self.owner.assertTrue(termindex._page_usable(page, page["language"]))
            self.owner.assertEqual(termindex.page_story_key(page), "mysekai_talk:" + identity)
        return pages


class MysekaiTalkCompletenessReviewTests(unittest.TestCase):
    def test_first_three_404s_never_hide_later_pages_with_public_budget_or_workers(self):
        for region in REGION_TEXT:
            for limit in (1, 2, 0):
                for workers in (1, 4):
                    with self.subTest(region=region, limit=limit, workers=workers):
                        fixture = _PublicFixture(self, region=region)
                        result = fixture.crawl(limit=limit, workers=workers)
                        pages = fixture.pages()
                        identities = {int(page["id"].rsplit(":", 1)[-1]) for page in pages}
                        self.assertEqual(len(pages), 1 if limit == 1 else 2)
                        self.assertTrue(identities <= {4, 5})
                        if limit != 1 or workers == 1:
                            self.assertEqual(identities, {4} if limit == 1 else {4, 5})
                        fixture.assert_talks(identities)
                        self.assertEqual(result["crawled_pages"], len(pages))
                        counts = fixture.asset_counts()
                        self.assertEqual({name: counts[name] for name in ("review_1", "review_2", "review_3")},
                                         {"review_1": 1, "review_2": 1, "review_3": 1})
                        self.assertTrue(all(count == 1 for count in counts.values()), counts)
                        if workers == 1:
                            self.assertEqual([call[1] for call in fixture.calls if call[0] == "asset"],
                                             [f"review_{i}" for i in range(1, 5 if limit == 1 else 6)])
                        for _, name, path in (call for call in fixture.calls if call[0] == "asset"):
                            self.assertEqual(path, f"/assets/sekai-{region}-assets/mysekai/talk/scenario/talk/{name}.lua.txt")
                        if limit > 0:
                            self.assertNotIn("mysekaiCharacterTalkTweets.json",
                                             [call[1] for call in fixture.calls if call[0] == "master"])

    def test_all_known_ids_resume_without_asset_probe_or_metadata_changes(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                fixture = _PublicFixture(self)
                fixture.assets = {f"review_{i}": fixture.lua for i in range(1, 6)}
                fixture.crawl(workers=workers)
                fixture.assert_talks(set(range(1, 6)))
                before = dbstore.existing_page_map(fixture.store, "altsource_sv")
                fixture.calls.clear()
                result = fixture.crawl(workers=workers, resume=True)
                self.assertEqual(result["crawled_pages"], 0)
                self.assertEqual(fixture.asset_counts(), {})
                self.assertEqual(dbstore.existing_page_map(fixture.store, "altsource_sv"), before)

    def test_resume_known_first_three_still_acquires_fourth_and_fifth_once(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                fixture = _PublicFixture(self)
                fixture.assets = {f"review_{i}": fixture.lua if i <= 3 else None for i in range(1, 6)}
                fixture.crawl(workers=workers)
                fixture.assert_talks({1, 2, 3})
                original = dbstore.existing_page_map(fixture.store, "altsource_sv")
                fixture.assets = {f"review_{i}": fixture.lua for i in range(1, 6)}
                fixture.calls.clear()
                result = fixture.crawl(workers=workers, resume=True)
                self.assertEqual(result["crawled_pages"], 2)
                self.assertEqual(fixture.asset_counts(), {"review_4": 1, "review_5": 1})
                fixture.assert_talks(set(range(1, 6)))
                current = dbstore.existing_page_map(fixture.store, "altsource_sv")
                self.assertEqual({key: current[key] for key in original}, original)

    def test_same_asset_different_ids_remain_two_page_obligations(self):
        for workers in (1, 4):
            with self.subTest(workers=workers):
                fixture = _PublicFixture(self, records=[record(6, "shared"), record(7, "shared")], assets={})
                fixture.assets["shared"] = fixture.lua
                result = fixture.crawl(workers=workers)
                pages = fixture.assert_talks({6, 7})
                self.assertEqual(result["crawled_pages"], 2)
                self.assertEqual(fixture.asset_counts(), {"shared": 2})
                urls = [urlparse(page["url"]) for page in pages]
                for url in urls:
                    parameters = parse_qsl(url.query, keep_blank_values=True)
                    self.assertEqual([key for key, _ in parameters], ["v"])
                    self.assertTrue(parameters[0][1].isdigit())
                # The per-request cache-buster does not identify a different asset.
                self.assertEqual(urls[0]._replace(query=""), urls[1]._replace(query=""))
                self.assertEqual(pages[0]["hash"], pages[1]["hash"])

    def test_duplicate_ids_and_malformed_metadata_do_not_issue_extra_or_unsafe_requests(self):
        invalid = [None, [], "not a record", 1, True]
        invalid += [dict(record(identity), id=identity) for identity in (False, True, 0, -1, 1.0, "1", None)]
        for field in ("assetbundleName", "lua"):
            for value in (None, False, 1, [], "", ".", "..", "../unsafe", "/unsafe", "unsafe/", "unsafe//path", "https://unsafe.invalid/body", "unsafe?query", "unsafe#fragment"):
                invalid.append(dict(record(100), **{field: value}))
        records = invalid + [record(1), record(1), record(1, "must_not_fetch"), record(2)]
        for workers in (1, 4):
            with self.subTest(workers=workers):
                fixture = _PublicFixture(self, records=records, assets={})
                fixture.assets = {"review_1": fixture.lua, "review_2": fixture.lua}
                result = fixture.crawl(workers=workers)
                self.assertEqual(result["crawled_pages"], 2)
                fixture.assert_talks({1, 2})
                self.assertEqual(fixture.asset_counts(), {"review_1": 1, "review_2": 1})

    def test_empty_and_non_dialogue_lua_never_spend_a_page_budget(self):
        invalid_lua = ["", " \t\r\n", 'label("Ichika")\nwait_click()\n', 'text("unterminated)\n',
                       'label("Ichika")\ntext("")\n', 'label("Ichika")\ntext(" \t")\n']
        for workers in (1, 4):
            for limit in (1, 2, 0):
                with self.subTest(workers=workers, limit=limit):
                    records = [record(i) for i in range(1, len(invalid_lua) + 2)]
                    fixture = _PublicFixture(self, records=records, assets={})
                    fixture.assets = {f"review_{i}": lua for i, lua in enumerate(invalid_lua, 1)}
                    fixture.assets[f"review_{len(records)}"] = fixture.lua
                    result = fixture.crawl(limit=limit, workers=workers)
                    self.assertEqual(result["crawled_pages"], 1)
                    fixture.assert_talks({len(records)})
                    self.assertEqual(fixture.asset_counts(), {f"review_{i}": 1 for i in range(1, len(records) + 1)})

    def test_public_budget_exhausted_by_earlier_domain_never_enters_talk_fetch(self):
        fixture = _PublicFixture(self)
        fixture.masters["characterArchiveVoices.json"] = [dict(id=101, displayPhrase="An earlier home dialogue")]
        result = fixture.crawl(limit=1, workers=4)
        self.assertEqual(result["crawled_pages"], 1)
        self.assertEqual({page["kind"] for page in fixture.pages()}, {"home_line"})
        self.assertEqual(fixture.asset_counts(), {})
        self.assertNotIn("mysekaiCharacterTalks.json", [call[1] for call in fixture.calls if call[0] == "master"])


if __name__ == "__main__":
    unittest.main()
