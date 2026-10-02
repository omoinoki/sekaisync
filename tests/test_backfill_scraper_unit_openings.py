"""Exact-scope public-crawler fixtures for isolated unit-opening backfill."""
from contextlib import contextmanager
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import backfill_scraper_unit_openings as bf
from sekaisync.layout import web_consent_path


class Response(BytesIO):
    def __init__(self, url, raw, headers=None):
        super().__init__(raw)
        self.url = url
        self.headers = headers or {"Content-Type": "application/json"}

    def getcode(self):
        return 200

    def geturl(self):
        return self.url


class OpeningBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.production = self.root / "production"
        self.out = self.root / "isolated"
        consent = web_consent_path(self.production)
        consent.parent.mkdir(parents=True)
        consent.write_text(json.dumps({source: {"accepted": True, "tos_version": 1,
                            "accepted_at": "2026-09-26T10:09:13+00:00"} for source in ("altsource_sv", "altsource_ms")}), encoding="utf-8")
        self.consent_raw = consent.read_bytes()
        self.requests = []
        self.texts = {"ja": "初めまして。音楽を演奏しましょう。", "en": "Hello, let us play some music together.",
                      "zh_hans": "初次见面。我们一起演奏音乐吧。", "zh_hant": "初次見面。我們一起演奏音樂吧。",
                      "ko": "처음 뵙겠습니다. 함께 음악을 연주합시다."}
        for language in bf.LANGUAGES:
            region = bf.REGIONS[language]
            records = []
            identities = []
            for number in range(5):
                scenario = f"unit_{number}_01_00"
                bundle = f"unit-{number}-story-chapter"
                identity = {"unit": f"unit_{number}", "story_seq": number + 1, "chapter_id": number + 100,
                            "chapter_no": 1, "episode_id": 10000 + number, "episode_no": 1,
                            "episode_group_id": number + 200, "scenario_id": scenario, "assetbundle_name": bundle}
                identities.append(identity)
                records.append({"unit": identity["unit"], "seq": identity["story_seq"], "chapters": [{
                    "id": identity["chapter_id"], "chapterNo": 1, "assetbundleName": bundle, "episodes": [{
                        "id": identity["episode_id"], "episodeNo": 1, "unitStoryEpisodeGroupId": identity["episode_group_id"],
                        "scenarioId": scenario, "episodeNoLabel": "Opening", "title": "Opening"}]}]})
            folder = self.root / "raw" / region
            folder.mkdir(parents=True)
            raw_path = folder / "unitStories.json"
            raw_path.write_text(json.dumps(records), encoding="utf-8")
            (folder / "unitProfiles.json").write_text(json.dumps([
                {"unit": identity["unit"], "seq": identity["story_seq"]} for identity in identities]), encoding="utf-8")
            evidence = {"path": str(raw_path), "sha256": bf.sha(raw_path.read_bytes())}
            for identity in identities:
                self.requests.append({"language": language, "region": region, "identity": identity,
                    "logical_key": "unit_story:" + identity["scenario_id"], "is_opening_episode": True,
                    "raw_file": evidence, "sv_asset_paths": [
                        f"scenario/unitstory/{identity['assetbundle_name']}/{identity['scenario_id']}.asset"],
                    "ms_scenario_path": f"scenario/unitstory/{identity['assetbundle_name']}/{identity['scenario_id']}.json"})
        self.request_path = self.root / "requests.json"
        self.request_path.write_text(json.dumps({"schema": "sekaisync/p0-expected-unit-backfill@1",
                                     "requests": self.requests}), encoding="utf-8")
        self.urls = []

    @contextmanager
    def network(self, *, empty=False, mismatch=False, robots=None, content_header=None):
        def open_fake(url, **_kwargs):
            self.urls.append(url)
            if bf.urlsplit(url).path == "/robots.txt":
                return Response(url, (robots or "User-agent: *\nAllow: /\n").encode(), {"Content-Type": "text/plain"})
            bucket = bf.urlsplit(url).path.split("/")[1]
            matching = [request for request in self.requests if any(bf.urlsplit(url).path.endswith(path)
                        for path in request["sv_asset_paths"] + [request["ms_scenario_path"]]) and
                        bucket in {"sekai-" + request["region"] + "-assets",
                                   "sekai-" + ("tw" if request["region"] == "tc" else request["region"]) + "-assets"}]
            if len(matching) != 1:
                raise AssertionError("Unexpected real-network scope: " + url)
            request = matching[0]
            scenario = "wrong_01_00" if mismatch else request["identity"]["scenario_id"]
            raw = json.dumps({"ScenarioId": scenario, "TalkData": [] if empty else [{
                "WindowDisplayName": "", "Body": self.texts[request["language"]]}]}, ensure_ascii=False).encode()
            headers = {"Content-Type": "application/json"}
            if content_header:
                headers["X-Robots-Tag"] = content_header
            return Response(url, raw, headers)
        with patch.object(bf.transport, "open_validated", side_effect=open_fake):
            yield

    def test_public_crawler_recovers_exact_25_and_production_unchanged(self):
        with self.network():
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual("complete", report["status"])
        self.assertEqual(25, report["accepted_locale_units"])
        self.assertEqual(25, report["persisted_pages"])
        self.assertEqual(25, sum("scenario/unitstory/" in url for url in self.urls))
        self.assertTrue(all(row["identity_audit"]["status"] == "verified" for row in report["details"]))
        self.assertEqual(self.consent_raw, web_consent_path(self.production).read_bytes())
        self.assertFalse((self.production / "kb/sekaisync.db").exists())
        self.assertEqual(report["fetch_records_sha256"], bf.sha(Path(report["fetch_records"]).read_bytes()))
        with self.assertRaisesRegex(ValueError, "receipt is frozen"):
            bf.run(self.request_path, self.out, self.production, delay=0)

    def test_empty_opening_raw_assets_are_not_text_successes(self):
        with self.network(empty=True):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual("partial", report["status"])
        self.assertEqual(0, report["accepted_locale_units"])
        records = [json.loads(line) for line in Path(report["fetch_records"]).read_text(encoding="utf-8").splitlines()]
        validations = [row for row in records if row["role"] == "scenario_content_validation"]
        self.assertEqual(25, len(validations))
        self.assertTrue(all(not row["nonempty_text"] for row in validations))

    def test_ms_public_crawler_uses_exact_nested_profile_identity(self):
        with self.network():
            report = bf.run(self.request_path, self.out, self.production, source="altsource_ms", delay=0)
        self.assertEqual("complete", report["status"])
        self.assertEqual(25, report["accepted_locale_units"])
        self.assertTrue(all(row["identity_audit"]["status"] == "verified" for row in report["details"]))
        self.assertFalse(report["auxiliary_local_profiles"]["retrieval_sealed"])
        self.assertEqual(5, len(report["auxiliary_local_profiles"]["files"]))
        self.assertEqual(self.consent_raw, web_consent_path(self.production).read_bytes())

    def test_scenario_identity_mismatch_stops_before_persistence(self):
        with self.network(mismatch=True):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual("partial", report["status"])
        self.assertEqual(0, report["persisted_pages"])
        self.assertEqual("ScopeViolation", report["errors"][0]["type"])
        self.assertEqual(1, sum("scenario/unitstory/" in url for url in self.urls))

    def test_robots_disallow_stops_before_scenario_request(self):
        with self.network(robots="User-agent: *\nDisallow: /\n"):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual(0, report["persisted_pages"])
        self.assertEqual(0, sum("scenario/unitstory/" in url for url in self.urls))
        self.assertIn("disallows", report["errors"][0]["message"])

    def test_content_use_header_is_preserved(self):
        with self.network(content_header="noai"):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual(0, report["persisted_pages"])
        self.assertIn("prohibits", report["errors"][0]["message"])

    def test_missing_consent_has_no_network_side_effects(self):
        web_consent_path(self.production).write_text("{}", encoding="utf-8")
        with self.network(), self.assertRaisesRegex(ValueError, "consent is missing"):
            bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual([], self.urls)

    def test_raw_inventory_drift_fails_before_network(self):
        raw_path = Path(self.requests[0]["raw_file"]["path"])
        raw_path.write_text("[]", encoding="utf-8")
        with self.network(), self.assertRaisesRegex(ValueError, "inventory changed"):
            bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual([], self.urls)

    def test_overlap_with_production_store_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "overlap"):
            bf.isolation(self.production / "backfill", self.production, self.production)

    def test_unlisted_scenario_url_is_rejected(self):
        manifest, inventory = bf.load_requests(self.request_path)
        config = bf.SekaiSyncConfig.from_dict({"store_root": str(self.out / "store")}, bf.ROOT)
        fetcher = bf.ExactFetcher(bf.build_runtime(config), "altsource_sv", manifest["requests"],
                                  inventory["definitions"], self.out)
        with self.network(), self.assertRaisesRegex(bf.ScopeViolation, "outside the exact"):
            fetcher("https://storage.sekai.best/unlisted/unit.asset")
        self.assertEqual([], self.urls)


if __name__ == "__main__":
    unittest.main()
