"""No-network public-path replay and exact composite home-voice recovery."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import backfill_scraper_home_lines as backfill
from scripts import census_scraper_home_lines as census
from sekaisync.layout import web_consent_path


class OfflineHomeBackfillTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.generation, self.production = self.root / "generation", self.root / "production"
        self.inventory, self.out = self.root / "inventory", self.root / "isolated"
        for language, region in census.REGIONS.items():
            folder = self.generation / region / "source" / "sealed-regional-master"
            folder.mkdir(parents=True)
            records = [dict(id=100 + number, groupId=100 + number, gameCharacterId=1,
                characterArchiveVoiceType="live_cutin_pair", externalId=1000 + number,
                displayPhrase=f"First phrase {number}", displayPhrase2=f"Second phrase {number}", displayStartAt=123)
                for number in range(6)]
            (folder / "characterArchiveVoices.json").write_text(json.dumps(records), encoding="utf-8")
        db = self.production / "kb/sekaisync.db"
        db.parent.mkdir(parents=True)
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE web_pages (source TEXT,id TEXT,language TEXT,kind TEXT,canonical_key TEXT,text TEXT,"
                     "text_hash TEXT,source_hash TEXT,trust TEXT,asset_mismatch TEXT,scenario_id_mismatch TEXT,"
                     "content_language_mismatch INTEGER,untranslated INTEGER,aux_flag INTEGER,auxiliary INTEGER)")
        conn.commit()
        conn.close()
        self.db_raw = db.read_bytes()
        consent = web_consent_path(self.production)
        consent.parent.mkdir(parents=True)
        consent.write_text(json.dumps({"altsource_ms": {"accepted": True, "tos_version": 1}}), encoding="utf-8")
        self.consent_raw = consent.read_bytes()
        census.run(self.generation, self.production, self.inventory)

    def assert_no_network(self):
        return patch.object(backfill.crawler.fetcher_transport, "open_validated", side_effect=AssertionError("No network permitted"))

    def reseal_debts(self, mutate):
        summary_path = self.inventory / "home-voice-inventory-summary.json"
        summary = json.loads(summary_path.read_bytes())
        evidence = summary["files"]["home-voice-acquisition-debts"]
        path = Path(evidence["path"])
        debts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        mutate(debts[0])
        path.write_text("".join(json.dumps(debt, ensure_ascii=False, sort_keys=True) + "\n" for debt in debts), encoding="utf-8")
        evidence["sha256"] = backfill.sha(path.read_bytes())
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_local_voice_id_must_be_a_positive_exact_integer(self):
        for alias in (100.0, True, "100", [], 0, -1):
            with self.subTest(local_voice_id=alias):
                self.reseal_debts(lambda debt: debt.update(local_voice_id=alias))
                with self.assertRaisesRegex(ValueError, "local_voice_id must be a positive exact integer"):
                    backfill.prepare_requests(self.inventory)

    def test_composite_identity_equality_preserves_json_types(self):
        original = backfill.prepare_requests(self.inventory)["requests"][0]["composite_identity"]
        for field, key, alias in (("id", "value", 100.0), ("groupId", "value", 100.0),
                                  ("gameCharacterId", "value", True), ("externalId", "value", 1000.0),
                                  ("externalId", "present", 1)):
            with self.subTest(field=field, key=key, alias=alias):
                def mutate(debt):
                    debt["composite_identity"] = json.loads(json.dumps(original))
                    debt["composite_identity"][field][key] = alias
                self.reseal_debts(mutate)
                with self.assertRaisesRegex(ValueError, "exact sealed regional composite"):
                    backfill.prepare_requests(self.inventory)

    def test_public_path_exact_25_two_fields_zero_network_resume_idempotent(self):
        with self.assert_no_network() as network:
            report = backfill.run(self.inventory, self.out, self.production)
        network.assert_not_called()
        self.assertEqual("complete", report["status"])
        self.assertEqual(25, report["first_pass"]["accepted_records"])
        self.assertEqual(25, report["resume_same_master"]["accepted_records"])
        self.assertTrue(report["resume_idempotent"])
        self.assertEqual(0, report["actual_network_requests"])
        self.assertTrue(all(row["audit"]["exact_full_field_rendering"] for row in report["first_pass"]["details"]))
        self.assertTrue(all(len([field for field in row["audit"]["field_coverage"] if field["status"] == "expected_field_present"]) == 2
            for row in report["first_pass"]["details"]))
        self.assertEqual(self.db_raw, (self.production / "kb/sekaisync.db").read_bytes())
        self.assertEqual(self.consent_raw, web_consent_path(self.production).read_bytes())
        with self.assertRaisesRegex(ValueError, "frozen or nonempty"):
            backfill.run(self.inventory, self.out, self.production)

    def test_all_frozen_debts_replay_without_extra_records(self):
        with self.assert_no_network():
            report = backfill.run(self.inventory, self.out, self.production, per_language=0)
        self.assertEqual(30, report["selected_records"])
        self.assertEqual(30, report["first_pass"]["persisted_pages"])
        self.assertEqual(30, report["first_pass"]["accepted_records"])
        self.assertEqual("unknown_release", report["release_status"])

    def test_exact_batch_unavailable_and_sealed_table_drift_fail_fast(self):
        with self.assertRaisesRegex(ValueError, "batch size is unavailable"):
            backfill.prepare_requests(self.inventory, 7)
        source = next((self.generation / "jp/source").glob("*/characterArchiveVoices.json"))
        source.write_text("[]", encoding="utf-8")
        with self.assert_no_network(), self.assertRaisesRegex(ValueError, "table changed"):
            backfill.run(self.inventory, self.out, self.production)
        self.assertFalse(self.out.exists())

    def test_unlisted_asset_or_master_url_has_no_fallback_transport(self):
        manifest = backfill.prepare_requests(self.inventory)
        runtime = backfill.build_runtime(backfill.load_config(None, backfill.ROOT, store_override=self.out / "store"))
        self.out.mkdir()
        fetcher = backfill.OfflineHomeFetcher(runtime, "altsource_ms", manifest["requests"], self.out)
        with self.assert_no_network(), self.assertRaisesRegex(backfill.OfflineScopeViolation, "unlisted URL"):
            fetcher("https://storage.exmeaning.com/sekai-kr-assets/unrequested.asset")
        self.assertEqual([], fetcher.calls)

    def test_same_id_equal_length_master_text_change_refreshes_isolated_page(self):
        with self.assert_no_network():
            report = backfill.run(self.inventory, self.out, self.production)
        manifest = json.loads(Path(report["request_file"]["path"]).read_bytes())
        store = Path(report["isolated_store"])
        runtime = backfill.build_runtime(backfill.load_config(None, backfill.ROOT, store_override=store))
        mutation_out = self.root / "mutation-fixture"
        mutation_out.mkdir()
        request = next(row for row in manifest["requests"] if row["language"] == "ko")
        old = request["raw_record"]["displayPhrase2"]
        request["raw_record"]["displayPhrase2"] = old.replace("Second", "Alterd")
        self.assertEqual(len(old), len(request["raw_record"]["displayPhrase2"]))
        fetcher = backfill.OfflineHomeFetcher(runtime, "altsource_ms", manifest["requests"], mutation_out)
        with self.assert_no_network():
            backfill.crawler.crawl_altsource_ms(store, locales=(fetcher.locales["ko"],), depth=3, limit=0,
                accept_tos=False, tos_already_checked=True, delay=0, workers=1, resume=True,
                include_overlay=False, fetcher=fetcher, instance="altsource_ms", runtime=runtime)
        verification = backfill.verify_pages(store, "altsource_ms", manifest["requests"], fetcher)
        self.assertEqual(25, verification["accepted_records"])
        self.assertNotEqual(report["resume_same_master"]["page_text_identity_sha256"], verification["page_text_identity_sha256"])
        page_id = f"web:altsource_ms:ko-kr:home_line:{request['local_voice_id']}"
        text = backfill.stored_pages(store, "altsource_ms")[page_id]["text"]
        self.assertIn("Alterd", text)
        self.assertNotIn("Second", text)

    def test_extractor_omission_fails_replay_instead_of_reporting_success(self):
        with self.assert_no_network(), patch.object(backfill.crawler, "_home_line_text", side_effect=lambda record, text_key: str(record.get(text_key) or "")):
            report = backfill.run(self.inventory, self.out, self.production)
        self.assertEqual("partial", report["status"])
        self.assertEqual(0, report["first_pass"]["accepted_records"])
        self.assertNotIn("resume_same_master", report)


if __name__ == "__main__":
    unittest.main()
