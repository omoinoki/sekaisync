"""Exact-scope fixtures for the overseas tutorial area-talk acquisition debt."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from scripts import backfill_scraper_area_tutorial as bf
from tests.test_backfill_scraper_unit_openings import Response, offline_site_profile
from sekaisync.layout import web_consent_path


class AreaTutorialBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        profile = patch("sekaisync.config.load_site_profile", return_value=offline_site_profile())
        profile.start()
        self.addCleanup(profile.stop)
        self.production, self.out = self.root / "production", self.root / "isolated"
        consent = web_consent_path(self.production)
        consent.parent.mkdir(parents=True)
        consent.write_text(json.dumps({"altsource_ms": {"accepted": True, "tos_version": 1}}), encoding="utf-8")
        self.consent_raw = consent.read_bytes()
        self.jp_text = "\u8fd1\u304f\u306b\u3044\u308b\u30ad\u30e3\u30e9\u30af\u30bf\u30fc\u306b\u8a71\u3057\u304b\u3051\u3066\u307f\u307e\u3057\u3087\u3046\u3002"
        self.texts = {
            "en": "Try talking to the characters nearby.",
            "zh_hans": "\u8bd5\u7740\u548c\u9644\u8fd1\u7684\u89d2\u8272\u5bf9\u8bdd\u5427\u3002",
            "zh_hant": "\u8a66\u8457\u548c\u9644\u8fd1\u7684\u89d2\u8272\u5c0d\u8a71\u5427\u3002",
            "ko": "\uadfc\ucc98\uc5d0 \uc788\ub294 \uce90\ub9ad\ud130\uc5d0\uac8c \ub9d0\uc744 \uac78\uc5b4 \ubcf4\uc138\uc694.",
        }
        sources = {}
        self.records = {}
        for index, language in enumerate(bf.LANGUAGES):
            region = bf.REGIONS[language]
            folder = self.root / "raw" / region
            folder.mkdir(parents=True)
            record = dict(id=2 + index * 100, areaId=5 + index, scenarioId=bf.SCENARIO,
                          scriptId="tutorial", releaseConditionId=1, archiveDisplayType="none")
            self.records[language] = record
            path = folder / "actionSets.json"
            path.write_text(json.dumps([record, dict(id=5000, scenarioId="unrequested_extra")]), encoding="utf-8")
            sources[language] = dict(region=region, actions=dict(path=str(path), sha256=bf.sha(path.read_bytes())))
        census = self.root / "census.sqlite"
        conn = sqlite3.connect(census)
        conn.execute("CREATE TABLE pages (source TEXT,id TEXT,version_hash TEXT,metadata_json TEXT,language TEXT,logical_key TEXT,primary_good INTEGER)")
        conn.execute("INSERT INTO pages VALUES (?,?,?,?,?,?,?)", ("altsource_ms", "web:altsource_ms:ja-jp:area_talk:op_02area",
            "frozen-version", json.dumps(dict(text_hash=bf.sha(self.jp_text.encode()))), "ja", "area_talk:op_02area", 1))
        conn.commit()
        conn.close()
        self.source_path = self.root / "sources.json"
        self.source_path.write_text(json.dumps(dict(raw_sources=sources,
            frozen_census=dict(path=str(census), sha256=bf.sha(census.read_bytes())))), encoding="utf-8")
        self.request_path = self.root / "requests.json"
        self.request_path.write_text(json.dumps(bf.prepare_requests(self.source_path)), encoding="utf-8")
        self.urls = []

    @contextmanager
    def network(self, *, mode="good", robots=None, bad_language=None):
        def open_fake(url, **_kwargs):
            self.urls.append(url)
            if bf.urlsplit(url).path == "/robots.txt":
                return Response(url, (robots or "User-agent: *\nAllow: /\n").encode(), {"Content-Type": "text/plain"})
            matching = [request for request in json.loads(self.request_path.read_bytes())["requests"]
                if bf.urlsplit(url).path.endswith(request["ms_scenario_path"])
                and bf.urlsplit(url).path.split("/")[1] == "sekai-" + ("tw" if request["region"] == "tc" else request["region"]) + "-assets"]
            if len(matching) != 1:
                raise AssertionError("Unexpected body request: " + url)
            request = matching[0]
            language = request["language"]
            selected_mode = mode if bad_language is None or language == bad_language else "good"
            if selected_mode == "http_error":
                raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)
            text = self.texts[language]
            if selected_mode == "japanese":
                text = self.jp_text
            elif selected_mode == "placeholder":
                text = bf.DEFAULT_PLACEHOLDER
            elif selected_mode == "wrong_language":
                text = self.texts["ko"] if language != "ko" else self.texts["en"]
            payload = dict(ScenarioId="wrong_scenario" if selected_mode == "wrong_id" else bf.SCENARIO,
                m_Name=bf.SCENARIO, TalkData=[] if selected_mode == "empty" else [dict(Body=text)])
            if selected_mode == "missing_id":
                payload.pop("ScenarioId")
                payload.pop("m_Name")
            headers = {"Content-Type": "application/json"}
            if selected_mode == "content_block":
                headers["X-Robots-Tag"] = "noai"
            response = Response(url, json.dumps(payload, ensure_ascii=False).encode(), headers)
            if selected_mode == "redirect":
                response.url = "https://storage.exmeaning.com/unlisted.asset"
            return response
        with patch.object(bf.opening.transport, "open_validated", side_effect=open_fake):
            yield

    def scenario_urls(self):
        return [url for url in self.urls if "/scenario/actionset/" in url]

    def test_public_crawler_exact_four_uses_local_ids_and_preserves_production(self):
        with self.network(), patch("sekaisync.config.settings_path", side_effect=AssertionError("Private settings must not be read")):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual("complete", report["status"])
        self.assertEqual(4, report["accepted_locale_assets"])
        self.assertEqual(4, report["actual_body_requests"])
        self.assertEqual(4, len(self.scenario_urls()))
        self.assertTrue(all(bf.urlsplit(url).hostname.endswith(".fixture.invalid") for url in self.urls))
        self.assertEqual(4, report["persisted_pages"])
        self.assertTrue(all(row["identity_audit"]["identity_status"] == "expected" for row in report["details"]))
        for language in bf.LANGUAGES:
            self.assertTrue(any(f"group{self.records[language]['id'] // 100}/" in url for url in self.scenario_urls()))
        self.assertEqual(self.consent_raw, web_consent_path(self.production).read_bytes())
        self.assertFalse((self.production / "kb/sekaisync.db").exists())
        with self.assertRaisesRegex(ValueError, "frozen or nonempty"):
            bf.run(self.request_path, self.out, self.production, delay=0)

    def test_bad_responses_are_not_persisted_and_other_locales_continue(self):
        for mode in ("empty", "wrong_id", "missing_id", "wrong_language", "japanese", "placeholder", "content_block", "redirect", "http_error"):
            with self.subTest(mode=mode), self.network(mode=mode, bad_language="en"):
                self.urls = []
                report = bf.run(self.request_path, self.root / ("isolated-" + mode), self.production, delay=0)
                self.assertEqual("partial", report["status"])
                self.assertEqual(3, report["accepted_locale_assets"])
                self.assertEqual(3, report["persisted_pages"])
                self.assertEqual(4, len(self.scenario_urls()))
                self.assertEqual(4, report["actual_body_requests"])
                self.assertEqual("en", report["errors"][0]["language"])

    def test_all_placeholders_preserve_the_four_acquisition_debts(self):
        with self.network(mode="placeholder"):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual(0, report["accepted_locale_assets"])
        self.assertEqual(4, len(self.scenario_urls()))
        self.assertTrue(all(row["response_validation"]["untranslated_or_placeholder"] for row in report["details"]))

    def test_robots_denial_has_zero_body_requests(self):
        with self.network(robots="User-agent: *\nDisallow: /\n"):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual(0, report["actual_body_requests"])
        self.assertEqual([], self.scenario_urls())
        self.assertEqual(4, len(report["errors"]))

    def test_inventory_drift_and_manifest_forgery_fail_before_network(self):
        manifest = json.loads(self.request_path.read_bytes())
        manifest["requests"][0]["action_set_record"]["id"] = 999
        self.request_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.network(), self.assertRaisesRegex(ValueError, "differs from frozen"):
            bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual([], self.urls)

    def test_changed_sealed_master_table_fails_before_network(self):
        manifest = json.loads(self.request_path.read_bytes())
        Path(manifest["requests"][0]["raw_file"]["path"]).write_text("[]", encoding="utf-8")
        with self.network(), self.assertRaisesRegex(ValueError, "inventory changed"):
            bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual([], self.urls)

    def test_robots_transport_failure_is_not_retried_inside_a_locale(self):
        def fail(url, **kwargs):
            self.urls.append(url)
            self.assertEqual(0, kwargs["max_redirects"])
            raise urllib.error.URLError("synthetic unavailable robots")
        with patch.object(bf.opening.transport, "open_validated", side_effect=fail):
            report = bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual(4, len(self.urls))
        self.assertTrue(all(url.endswith("/robots.txt") for url in self.urls))
        self.assertEqual(0, report["actual_body_requests"])
        self.assertTrue(all("without retry" in row["message"] for row in report["errors"]))

    def test_missing_consent_has_no_network_side_effects(self):
        web_consent_path(self.production).write_text("{}", encoding="utf-8")
        with self.network(), self.assertRaisesRegex(ValueError, "consent is missing"):
            bf.run(self.request_path, self.out, self.production, delay=0)
        self.assertEqual([], self.urls)

    def test_unlisted_or_repeated_body_is_blocked_before_transport(self):
        manifest, _inventory = bf.load_requests(self.request_path)
        runtime = bf.build_runtime(bf.load_config(None, bf.ROOT, store_override=self.out / "store"))
        fetcher = bf.ExactAreaFetcher(runtime, "altsource_ms", manifest, self.out)
        with self.network(), self.assertRaisesRegex(bf.ScopeViolation, "outside the four"):
            fetcher("https://storage.exmeaning.com/unrequested.asset")
        self.assertEqual([], self.urls)
        url = next(iter(fetcher.allowed))
        with self.network():
            fetcher(url)
            with self.assertRaisesRegex(bf.ScopeViolation, "Repeated"):
                fetcher(url + "?another-request=1")
        self.assertEqual(1, len(self.scenario_urls()))


if __name__ == "__main__":
    unittest.main()
