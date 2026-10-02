"""Composite identity and two-phrase acquisition completeness fixtures."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import census_scraper_home_lines as census


def voice(identity=11, primary="First phrase", secondary="Second phrase", **changes):
    record = dict(id=identity, groupId=11, gameCharacterId=1, characterArchiveVoiceType="live_cutin_pair",
                  externalId=500, displayPhrase=primary, displayPhrase2=secondary, displayStartAt=123)
    record.update(changes)
    return record


def page(language="ko", identity=11, text="First phrase", **changes):
    region = census.REGIONS[language]
    value = dict(source="altsource_sv", id=f"web:altsource_sv:{region}:home_line:{identity}",
        language=language, canonical_key=f"home_line:{language}:{identity}", text=text,
        text_hash=census.sha(text.encode()), source_hash="", trust="B", asset_mismatch="",
        scenario_id_mismatch="", content_language_mismatch=0, untranslated=0, aux_flag=0, auxiliary=0)
    value.update(changes)
    return value


class HomeVoiceIdentityTests(unittest.TestCase):
    def test_repeated_phrase_fields_require_distinct_ordered_occurrences(self):
        record = voice(primary="Same", secondary="Same")
        coverage = census.field_coverage(record, "Same")
        self.assertEqual("expected_field_present", coverage[0]["status"])
        self.assertEqual("expected_field_missing", coverage[1]["status"])
        coverage = census.field_coverage(record, "Same\nSame")
        self.assertTrue(all(row["status"] == "expected_field_present" for row in coverage))
        self.assertLessEqual(coverage[0]["span"]["end"], coverage[1]["span"]["start"])

    def test_field_boundary_does_not_credit_a_substring_as_the_first_phrase(self):
        coverage = census.field_coverage(voice(primary="Hi", secondary="Hi there"), "Hi there")
        self.assertEqual("expected_field_missing", coverage[0]["status"])
        self.assertEqual("expected_field_present", coverage[1]["status"])

    def test_composite_cross_language_keys_do_not_merge_same_id_or_external_alone(self):
        tables = {
            "ja": [voice(), voice(12, gameCharacterId=2)],
            "ko": [voice(gameCharacterId=3), voice(22, gameCharacterId=2)],
        }
        definitions, local_ids, issues = census.build_inventory(tables)
        self.assertEqual(4, len(definitions))
        self.assertEqual(1, len(issues["same_id_composite_drifts"]))
        self.assertEqual(1, len(issues["same_non_id_signature_different_local_ids"]))
        self.assertEqual(2, len(issues["duplicate_external_ids"]))
        self.assertFalse(issues["same_non_id_signature_different_local_ids"][0]["automatic_merge"])
        self.assertEqual(1, len(local_ids["ja"][11]))

    def test_duplicate_local_ids_and_invalid_definitions_are_explicit(self):
        bad = voice(13, externalId=0)
        definitions, local_ids, issues = census.build_inventory({"ja": [voice(), voice(gameCharacterId=3), bad]})
        self.assertEqual(2, len(local_ids["ja"][11]))
        self.assertEqual(1, len(issues["duplicate_region_local_ids"]))
        self.assertEqual(1, len(issues["invalid_definitions"]))
        audit = census.audit_page(page("ja"), local_ids)
        self.assertEqual("invalid", audit["identity_status"])
        self.assertIn("ambiguous_region_local_voice_id", audit["reasons"])
        self.assertEqual(2, len(definitions))

    def test_page_locale_source_and_canonical_identity_are_verified(self):
        local = {"ko": {11: [voice()]}}
        good = census.audit_page(page(text="First phrase\nSecond phrase"), local)
        self.assertTrue(good["complete_primary_text"])
        for changes, reason in ((dict(id="web:altsource_ms:kr:home_line:11"), "page_id_source_mismatch"),
                                (dict(id="web:altsource_sv:jp:home_line:11"), "page_id_locale_mismatch"),
                                (dict(canonical_key="home_line:ko:12"), "canonical_key_identity_mismatch")):
            with self.subTest(reason=reason):
                audit = census.audit_page(page(**changes), local)
                self.assertEqual("invalid", audit["identity_status"])
                self.assertIn(reason, audit["reasons"])

    def test_missing_external_identity_is_retained_without_an_invented_value(self):
        record = voice()
        record.pop("externalId")
        self.assertEqual([], census.identity_errors(record))
        self.assertEqual(dict(present=False, value=None), census.raw_identity(record)["externalId"])
        self.assertNotEqual(census.composite_key(record), census.composite_key(voice()))

    def test_equal_length_changed_text_has_a_different_full_fingerprint(self):
        record = voice(primary="First", secondary="After")
        audit = census.audit_page(page(text="First\nOther"), {"ko": {11: [record]}})
        self.assertEqual(len("First\nAfter"), audit["characters"])
        self.assertFalse(audit["text_fingerprint_matches_expected"])
        self.assertFalse(audit["complete_primary_text"])
        self.assertEqual(census.sha(b"First\nAfter"), audit["expected_full_text_sha256"])

    def test_field_labeled_public_rendering_credits_raw_values_not_labels(self):
        record = voice(primary="Topic: Raw value", secondary="Another: Raw value")
        text = "displayPhrase: Topic: Raw value\ndisplayPhrase2: Another: Raw value"
        result = census.audit_page(page(text=text), {"ko": {11: [record]}})
        self.assertTrue(result["complete_primary_text"])
        self.assertTrue(result["current_public_field_rendering_matches"])
        for coverage, expected in zip(result["field_coverage"], (record["displayPhrase"], record["displayPhrase2"])):
            span = coverage["span"]
            self.assertEqual(expected, text[span["start"]:span["end"]])
            self.assertFalse(coverage["structural_label_credited_as_content"])
            self.assertEqual("field_labeled", coverage["rendering"])
        swapped = "displayPhrase2: Topic: Raw value\ndisplayPhrase: Another: Raw value"
        bad = census.audit_page(page(text=swapped), {"ko": {11: [record]}})
        self.assertFalse(bad["complete_primary_text"])
        self.assertTrue(all(row["status"] == "expected_field_missing" for row in bad["field_coverage"]))


class HomeVoiceCensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.generation, self.production, self.out = self.root / "generation", self.root / "production", self.root / "inventory"
        self.records = [voice(), voice(12, primary="", secondary=""), voice(13, primary="Same", secondary="Same"),
                        voice(14, primary="", secondary="Only second")]
        for language, region in census.REGIONS.items():
            folder = self.generation / region / "source" / "sealed-regional-master"
            folder.mkdir(parents=True)
            (folder / "characterArchiveVoices.json").write_text(json.dumps(self.records), encoding="utf-8")
        self.db = self.production / "kb/sekaisync.db"
        self.db.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE web_pages (source TEXT,id TEXT,language TEXT,kind TEXT,canonical_key TEXT,text TEXT,"
                     "text_hash TEXT,source_hash TEXT,trust TEXT,asset_mismatch TEXT,scenario_id_mismatch TEXT,"
                     "content_language_mismatch INTEGER,untranslated INTEGER,aux_flag INTEGER,auxiliary INTEGER)")
        for language in census.REGIONS:
            for row in (page(language), page(language, 13, "Same"), page(language, 14, "Only second")):
                row["kind"] = "home_line"
                conn.execute("INSERT INTO web_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row[field] for field in (
                    "source", "id", "language", "kind", "canonical_key", "text", "text_hash", "source_hash", "trust",
                    "asset_mismatch", "scenario_id_mismatch", "content_language_mismatch", "untranslated", "aux_flag", "auxiliary")))
        row = page("ko", 999, "Foreign record")
        row["kind"] = "home_line"
        conn.execute("INSERT INTO web_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", tuple(row[field] for field in (
            "source", "id", "language", "kind", "canonical_key", "text", "text_hash", "source_hash", "trust",
            "asset_mismatch", "scenario_id_mismatch", "content_language_mismatch", "untranslated", "aux_flag", "auxiliary")))
        conn.commit()
        conn.close()

    def test_first_phrase_is_not_complete_secondary_only_is_expected_and_production_is_read_only(self):
        before = self.db.read_bytes()
        summary = census.run(self.generation, self.production, self.out)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(4, summary["composite_families"])
        self.assertEqual(4, summary["all_five_exact_composite_families"])
        self.assertEqual(10, summary["acquisition_debts"])
        self.assertEqual(dict(expected=15, foreign=1), summary["observed_identity_counts"])
        for language in census.REGIONS:
            self.assertEqual(2, summary["acquisition_counts"][language]["primary_first_phrase_only"])
            self.assertEqual(1, summary["acquisition_counts"][language]["primary_full_fields_present"])
            self.assertEqual(2, summary["field_coverage_counts"][language]["displayPhrase2:primary_field_missing"])
        expected = [json.loads(line) for line in (self.out / "expected-home-voices.jsonl").read_text(encoding="utf-8").splitlines()]
        releases = [definition["release"] for family in expected for definitions in family["expected_definitions"].values() for definition in definitions]
        self.assertTrue(all(release["status"] == "unknown_release" for release in releases))
        self.assertTrue(all(release["display_start_at_raw"] == 123 for release in releases))
        with self.assertRaisesRegex(ValueError, "frozen or nonempty"):
            census.run(self.generation, self.production, self.out)

    def test_output_overlap_and_ambiguous_raw_file_fail_fast(self):
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            census.run(self.generation, self.production, self.production / "inventory")
        folder = self.generation / "jp/source/second-master"
        folder.mkdir()
        (folder / "characterArchiveVoices.json").write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Exactly one"):
            census.run(self.generation, self.production, self.out)


if __name__ == "__main__":
    unittest.main()
