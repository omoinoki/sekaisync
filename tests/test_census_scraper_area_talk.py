"""Scenario inventories remain independent of acquired page counts and IDs."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import census_scraper_area_talk as census


class AreaTalkCensusTests(unittest.TestCase):
    def tables(self):
        return {language: dict(actions=[dict(id=index+10, scenarioId="area_shared", releaseConditionId=1)],
                               conditions=[dict(id=1, releaseConditionType="none")])
                for index, language in enumerate(census.REGIONS)}

    def page(self, language="en", scenario="area_shared", compound=None):
        suffix = str(compound) + ":" + scenario if compound is not None else scenario
        return dict(source="fixture", id=f"web:fixture:{language}:area_talk:{suffix}", language=language,
                    logical_key="area_talk:" + scenario, good=1, primary_good=1, version_hash="frozen",
                    metadata=dict(canonical_key=f"area_talk:{language}:{suffix}", text_hash="body-hash"))

    def test_inventory_is_scenario_first_with_region_local_numeric_ids(self):
        tables = self.tables()
        tables["en"]["actions"].append(dict(id=10, scenarioId="region_specific", releaseConditionId=1))
        definitions, ids, _, invalid, _ = census._inventory(tables)
        self.assertEqual(set(definitions), {"area_shared", "region_specific"})
        self.assertEqual({rows[0]["id"] for rows in definitions["area_shared"].values()}, {10, 11, 12, 13, 14})
        self.assertEqual(invalid, [])
        matrix = census._id_difference_matrix(ids)
        self.assertEqual(len(matrix), 1)
        self.assertEqual(matrix[0]["action_set_id"], 10)

    def test_observed_compound_uses_its_own_region_not_the_japanese_action_id(self):
        definitions, *_ = census._inventory(self.tables())
        self.assertEqual(census._audit_page(self.page(compound=11), definitions)["identity_status"], "expected")
        wrong = census._audit_page(self.page(compound=10), definitions)
        self.assertEqual(wrong["identity_status"], "invalid")
        self.assertIn("region_local_action_set_scenario_mismatch", wrong["reasons"])
        plain = census._audit_page(self.page(), definitions)
        self.assertEqual(plain["compound_identity"], "scenario_only_page_no_embedded_action_id")

    def test_foreign_invalid_canonical_and_locale_are_distinct(self):
        definitions, *_ = census._inventory(self.tables())
        self.assertEqual(census._audit_page(self.page(scenario="not_in_raw"), definitions)["identity_status"], "foreign")
        for change in ({"logical_key": "area_talk:other"}, {"source": "other"}, {"language": "ja"},
                       {"metadata": dict(canonical_key="area_talk:en:other")}):
            page = dict(self.page(), **change)
            self.assertEqual(census._audit_page(page, definitions)["identity_status"], "invalid")

    def test_missing_script_only_and_invalid_raw_records_do_not_create_expected_assets(self):
        tables = self.tables()
        tables["en"]["actions"].extend([dict(id=20), dict(id=21, scenarioId="bad:alias"),
                                        dict(id=True, scenarioId="bad_boolean"), dict(id=22, scenarioId=" leading")])
        definitions, _, _, invalid, script_only = census._inventory(tables)
        self.assertEqual(set(definitions), {"area_shared"})
        self.assertEqual(len(invalid), 3)
        self.assertEqual(len(script_only), 1)

    def test_unlock_none_is_only_a_prerequisite_statement_and_unknown_dates_stay_unknown(self):
        _, ids, conditions, *_ = census._inventory(self.tables())
        action = ids["en"][11][0]
        proof = census._unlock("en", action, ids, conditions, {})
        self.assertEqual(proof["status"], "no_unlock_prerequisite")
        self.assertNotIn("release_status", proof)
        for value in (None, True, 100.0, 10**40):
            self.assertEqual(census._archive(value, 1000)["status"], "unknown_archive_time")
        self.assertEqual(census._archive(100, 1000)["authority_scope"], "archivePublishedAt_only_not_general_server_release")

    def test_action_set_cycle_dangling_and_unsupported_conditions_keep_debt(self):
        actions = {"en": {1: [dict(id=1, scenarioId="a", releaseConditionId=1)],
                           2: [dict(id=2, scenarioId="b", releaseConditionId=2)]}}
        conditions = {"en": {1: [dict(id=1, releaseConditionType="action_set", releaseConditionTypeId=2)],
                              2: [dict(id=2, releaseConditionType="action_set", releaseConditionTypeId=1)]}}
        proof = census._unlock("en", actions["en"][1][0], actions, conditions, {})
        self.assertEqual(proof["status"], "unknown_unlock")
        self.assertEqual(proof["prerequisite"]["prerequisite"]["reasons"], ["cyclic_action_set_prerequisite"])
        conditions["en"][1][0]["releaseConditionTypeId"] = 99
        self.assertIn("dangling_or_ambiguous_region_local_action_set_prerequisite",
                      census._unlock("en", actions["en"][1][0], actions, conditions, {})["reasons"])
        conditions["en"][1][0]["releaseConditionType"] = "read_all_action_set_in_group"
        self.assertEqual(census._unlock("en", actions["en"][1][0], actions, conditions, {})["status"], "unknown_unlock")

    def test_event_episode_id_is_resolved_per_region_with_date_and_sealed_identity(self):
        action = dict(id=11, scenarioId="area_shared", releaseConditionId=1)
        ids = {"en": {11: [action]}}
        conditions = {"en": {1: [dict(id=1, releaseConditionType="event_story", releaseConditionTypeId=1200)]}}
        episode = dict(id=1200, scenarioId="localized_event_asset")
        proof = dict(status="released_in_verified_masterdata", region="en", start_at_ms=100,
                     episode_record=episode)
        entry = dict(logical_key="event_story:2:8", scenario_id="localized_event_asset", episode_record=episode,
                     proof=proof, source_matches_current_raw_region=True)
        events = {("en", 1200): [entry], ("ja", 1200): [dict(entry, scenario_id="wrong_japanese_asset")]}
        result = census._unlock("en", action, ids, conditions, events, as_of_ms=1000)
        self.assertEqual(result["status"], "event_prerequisite_schedule_proven")
        self.assertEqual(result["prerequisite_scenario_id"], "localized_event_asset")
        for field, value in (("start_at_ms", None), ("start_at_ms", 2000), ("region", "jp"),
                             ("episode_record", dict(id=1200, scenarioId="forged"))):
            mutated = deepcopy(events)
            mutated[("en", 1200)][0]["proof"][field] = value
            self.assertEqual(census._unlock("en", action, ids, conditions, mutated, as_of_ms=1000)["status"], "unknown_unlock")

    def test_real_cli_readonly_inputs_freeze_expected_and_missing_without_observed_seed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            generation, frozen, output = root / "generation", root / "frozen", root / "result"
            frozen.mkdir()
            for language, region in census.REGIONS.items():
                source = generation / region / "source" / "fixture"
                source.mkdir(parents=True)
                tables = self.tables()[language]
                for name, rows in (("actionSets.json", tables["actions"]), ("releaseConditions.json", tables["conditions"])):
                    (source / name).write_text(json.dumps(rows), encoding="utf-8")
            (frozen / "summary.json").write_text(json.dumps(dict(as_of_utc="2026-09-30T16:00:00+00:00")), encoding="utf-8")
            with closing(sqlite3.connect(frozen / "census-index.sqlite")) as conn:
                conn.execute("CREATE TABLE pages(source TEXT,id TEXT,kind TEXT,language TEXT,logical_key TEXT,good INT,primary_good INT,version_hash TEXT,metadata_json TEXT)")
                for page in (self.page(), self.page(scenario="foreign_observed_only")):
                    conn.execute("INSERT INTO pages VALUES(?,?,?,?,?,?,?,?,?)", (page["source"], page["id"], "area_talk", page["language"],
                                 page["logical_key"], 1, 1, "frozen", json.dumps(page["metadata"])))
                conn.commit()
            before = census._hash(frozen / "census-index.sqlite")
            report = census.run(generation, frozen, output)
            self.assertEqual(report["expected_scenarios"], 1)
            self.assertEqual(report["observed_identity_counts"], {"expected": 1, "foreign": 1})
            self.assertEqual(report["acquisition_debt_counts"], {"missing": 4})
            expected = json.loads((output / "expected-area-talks.jsonl").read_text(encoding="utf-8"))
            self.assertEqual(expected["release_status"], "unknown_release")
            self.assertEqual(expected["versions"]["en"]["definitions"][0]["unlock"]["status"], "no_unlock_prerequisite")
            self.assertEqual(expected["versions"]["en"]["definitions"][0]["archive_publication"]["status"], "unknown_archive_time")
            self.assertEqual(before, census._hash(frozen / "census-index.sqlite"))
            with self.assertRaises(FileExistsError):
                census.run(generation, frozen, output)
            with self.assertRaisesRegex(ValueError, "same as-of cutoff"):
                census.run(generation, frozen, root / "later", "2027-01-01T00:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
