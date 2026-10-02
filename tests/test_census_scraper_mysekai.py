"""Independent adversarial checks for the full, read-only MySEKAI inventory."""
from __future__ import annotations

import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import census_scraper_mysekai as census


TEXT = {"ja": "こんにちは、テストです", "en": "Hello, this is a test", "zh_hans": "这是简体中文测试", "zh_tw": "這是繁體中文測試", "ko": "안녕하세요 테스트입니다"}


def tables() -> dict:
    result = {}
    for lang in census.REGIONS:
        result[lang] = {
            census.TWEETS: [dict(id=1, text=TEXT[lang], motionName="motion")],
            census.TALKS: [dict(id=1, assetbundleName="mysekai/talk/scenario/talk", lua="lua_1")],
            census.TUTORIALS: [dict(id=1, assetbundleName="mysekai/tutorial", lua="lua_1", mysekaiCharacterTalkTweetId=1)],
            census.PREACTIONS: [dict(id=1, mysekaiCharacterTalkId=1, mysekaiCharacterTalkTweetId=1)],
            census.WITHOUT_TALKS: [dict(id=1, gameCharacterUnitId=1, mysekaiCharacterTalkTweetId=1)],
            census.FIXTURES: [dict(id=1, groupId=7, mysekaiCharacterTalkTweetId=1)],
        }
    return result


def page(*, lang="en", kind="mysekai_tweet", local_id=1, source="altsource_sv", text=None, **changes) -> dict:
    region = census.REGIONS[lang]
    body = TEXT[lang] if text is None else text
    url = ("https://example.test/sekai-" + region + "-assets/mysekai/talk/scenario/talk/lua_1.lua.txt?v=1"
           if kind == "mysekai_talk" else "https://example.test/mysekaiCharacterTalkTweets.json")
    result = dict(id=f"web:{source}:{region}:{kind}:{local_id}", source=source, language=lang, kind=kind,
        canonical_key=f"{kind}:{lang}:{local_id}", text=body, text_hash=census.sha(body.encode()),
        url=url, trust="B", source_hash="", **{flag: 0 for flag in census.FLAGS})
    result.update(changes)
    return result


def acquire(raw: dict, pages: list) -> dict:
    inventory = census.build_inventory(raw)
    audits = [census.audit_page(row, inventory) for row in pages]
    debts, release = census.assign_acquisition(inventory, audits)
    return dict(inventory=inventory, audits=audits, debts=debts, release=release,
                families=census.structural_families(inventory))


def definition(result: dict, table=census.TWEETS, lang="en", local_id=1) -> dict:
    return next(row for row in result["inventory"]["definitions"]
                if row["raw_table"] == table and row["language"] == lang and row["local_id"] == local_id)


class MysekaiInventoryIdentityTests(unittest.TestCase):
    def test_bool_id_cannot_be_an_integer_identity(self):
        raw = tables()
        raw["en"][census.TWEETS][0]["id"] = True
        result = acquire(raw, [page()])
        bad = next(row for row in result["inventory"]["definitions"] if row["language"] == "en" and row["raw_table"] == census.TWEETS)
        self.assertEqual(bad["acquisition_status"], "invalid_definition")
        self.assertEqual(result["audits"][0]["identity_status"], "foreign")
        self.assertEqual(len(result["release"]), 15)

    def test_nonstring_tweet_does_not_get_string_coercion_credit(self):
        raw = tables()
        raw["en"][census.TWEETS][0]["text"] = 123
        result = acquire(raw, [page(text="123")])
        self.assertEqual(definition(result)["acquisition_status"], "invalid_definition")
        self.assertIn("invalid_raw_definition", result["audits"][0]["reasons"])

    def test_duplicate_local_definition_cannot_credit_current_page(self):
        raw = tables()
        raw["en"][census.TWEETS].append(copy.deepcopy(raw["en"][census.TWEETS][0]))
        result = acquire(raw, [page()])
        matches = result["inventory"]["lookup"]["en", census.TWEETS, 1]
        self.assertEqual([row["acquisition_status"] for row in matches], ["ambiguous_sealed_local_identity"] * 2)
        self.assertEqual(result["audits"][0]["identity_status"], "invalid")
        self.assertEqual(len(result["release"]), 16)

    def test_asset_path_traversal_and_bad_lua_are_invalid(self):
        raw = tables()
        raw["en"][census.TALKS][0]["assetbundleName"] = "../talk"
        raw["en"][census.TUTORIALS][0]["lua"] = "lua\\path"
        result = acquire(raw, [])
        self.assertEqual(definition(result, census.TALKS)["acquisition_status"], "invalid_definition")
        self.assertEqual(definition(result, census.TUTORIALS)["acquisition_status"], "invalid_definition")

    def test_full_source_locale_kind_and_canonical_identity_are_independent(self):
        inventory = census.build_inventory(tables())
        cases = (
            (dict(source="other_provider"), "page_id_source_mismatch"),
            (dict(id="web:altsource_sv:jp:mysekai_tweet:1"), "page_id_locale_mismatch"),
            (dict(kind="mysekai_talk"), "page_id_kind_mismatch"),
            (dict(canonical_key="mysekai_tweet:en:2"), "canonical_key_identity_mismatch"),
            (dict(id="web:altsource_sv:en:mysekai_tweet:1:extra"), "invalid_page_id"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                candidate = page()
                candidate.update(changes)
                audited = census.audit_page(candidate, inventory)
                self.assertEqual(audited["identity_status"], "invalid")
                self.assertIn(reason, audited["reasons"])

    def test_same_talk_id_different_asset_is_a_conflict_not_one_family(self):
        raw = tables()
        raw["en"][census.TALKS][0]["lua"] = "different_lua"
        result = acquire(raw, [])
        self.assertEqual(len(result["inventory"]["issues"]["same_talk_id_asset_drifts"]), 1)
        talk_families = [row for row in result["families"] if row["raw_composite_identity"]["domain"] == "mysekai_talk"]
        self.assertEqual(len(talk_families), 2)
        self.assertFalse(any(row["all_five_region_definitions"] for row in talk_families))

    def test_same_asset_multiple_ids_remain_distinct_obligations(self):
        raw = tables()
        raw["en"][census.TALKS].append(dict(raw["en"][census.TALKS][0], id=2))
        result = acquire(raw, [page(kind="mysekai_talk")])
        self.assertEqual(definition(result, census.TALKS, local_id=2)["acquisition_status"], "missing")
        self.assertEqual(result["inventory"]["issues"]["same_asset_multiple_talk_ids"][0]["local_ids"], [1, 2])

    def test_dangling_preaction_talk_supplies_no_asset_binding(self):
        raw = tables()
        raw["en"][census.PREACTIONS][0]["mysekaiCharacterTalkId"] = 999
        inventory = census.build_inventory(raw)
        self.assertTrue(any("dangling_or_ambiguous_talk" in row["errors"] for row in inventory["issues"]["invalid_or_dangling_edges"]))
        self.assertFalse(any(row["table"] == census.PREACTIONS for row in inventory["inbound"]["en", 1]))

    def test_missing_regional_talk_is_unresolved_not_an_asset_identity_conflict(self):
        raw = tables()
        raw["en"][census.TALKS] = []
        result = acquire(raw, [])
        family = next(row for row in result["families"] if row["raw_composite_identity"]["domain"] == "mysekai_tweet")
        self.assertFalse(family["conflicting_inbound_edge_identity"])
        self.assertEqual(result["inventory"]["issues"]["cross_language_edge_identity_conflicts"], [])
        self.assertTrue(result["inventory"]["issues"]["invalid_or_dangling_edges"])

    def test_conflicting_common_edge_not_hidden_by_other_shared_weak_binding(self):
        raw = tables()
        raw["en"][census.TUTORIALS][0]["lua"] = "different_tutorial"
        result = acquire(raw, [])
        family = next(row for row in result["families"] if row["raw_composite_identity"]["domain"] == "mysekai_tweet")
        self.assertTrue(family["common_inbound_edge_identities"])
        self.assertTrue(family["conflicting_inbound_edge_identity"])
        self.assertEqual(family["structural_binding_status"], "conflicting_inbound_identity")

    def test_identical_character_binding_does_not_merge_different_tweet_ids(self):
        raw = tables()
        for lang in raw:
            raw[lang][census.TWEETS].append(dict(id=2, text=TEXT[lang]))
            raw[lang][census.WITHOUT_TALKS].append(dict(id=2, gameCharacterUnitId=1, mysekaiCharacterTalkTweetId=2))
        result = acquire(raw, [])
        tweet_families = [row for row in result["families"] if row["raw_composite_identity"]["domain"] == "mysekai_tweet"]
        self.assertEqual(len(tweet_families), 2)
        self.assertEqual({row["raw_composite_identity"]["id"] for row in tweet_families}, {1, 2})


class MysekaiAcquisitionTests(unittest.TestCase):
    def test_exact_current_body_and_equal_length_changed_body_are_distinct(self):
        current = acquire(tables(), [page()])
        stale = acquire(tables(), [page(text="X" + TEXT["en"][1:])])
        self.assertEqual(definition(current)["acquisition_status"], "primary_current_raw_body")
        self.assertEqual(definition(stale)["acquisition_status"], "primary_stale_raw_body")
        self.assertEqual(len(TEXT["en"]), stale["audits"][0]["text_characters"])

    def test_current_body_with_mismatch_flags_is_bad_only(self):
        for flag in ("asset_mismatch", "scenario_id_mismatch", "content_language_mismatch", "untranslated", "aux_flag", "auxiliary"):
            with self.subTest(flag=flag):
                result = acquire(tables(), [page(**{flag: 1})])
                self.assertEqual(definition(result)["acquisition_status"], "bad_only")

    def test_duplicate_providers_do_not_multiply_definition_or_family(self):
        result = acquire(tables(), [page(), page(source="custom_provider")])
        self.assertEqual(definition(result)["observed_pages"], 2)
        self.assertEqual(len(result["inventory"]["definitions"]), 15)
        self.assertEqual(len(result["families"]), 3)

    def test_blank_definition_retained_without_nonempty_obligation(self):
        raw = tables()
        raw["en"][census.TWEETS][0]["text"] = "  \n"
        result = acquire(raw, [])
        self.assertEqual(definition(result)["acquisition_status"], "no_nonempty_text_obligation")
        self.assertEqual(len(result["release"]), 15)

    def test_talk_url_binding_does_not_prove_asset_body_freshness(self):
        result = acquire(tables(), [page(kind="mysekai_talk")])
        self.assertEqual(definition(result, census.TALKS)["acquisition_status"], "primary_usable_path_bound_freshness_unknown")
        self.assertEqual(result["audits"][0]["body_freshness"], "unknown_asset_body_freshness")
        family = next(row for row in result["families"] if row["raw_composite_identity"]["domain"] == "mysekai_talk")
        self.assertFalse(family["body_freshness_proved"])

    def test_talk_other_locale_bucket_does_not_get_path_credit(self):
        result = acquire(tables(), [page(kind="mysekai_talk", url="https://example.test/sekai-jp-assets/mysekai/talk/scenario/talk/lua_1.lua.txt")])
        self.assertEqual(definition(result, census.TALKS)["acquisition_status"], "primary_asset_identity_mismatch")
        self.assertEqual(result["audits"][0]["asset_path_binding"], "asset_bucket_locale_mismatch")

    def test_unbound_talk_body_retains_named_provenance_debt(self):
        result = acquire(tables(), [page(kind="mysekai_talk", url="")])
        self.assertEqual(definition(result, census.TALKS)["acquisition_status"], "primary_usable_asset_provenance_unknown")
        self.assertTrue(any(row["acquisition_status"] == "primary_usable_asset_provenance_unknown" for row in result["debts"]))

    def test_tutorial_does_not_borrow_same_numeric_character_talk_page(self):
        result = acquire(tables(), [page(kind="mysekai_talk")])
        tutorial = definition(result, census.TUTORIALS)
        self.assertEqual(tutorial["observed_pages"], 0)
        self.assertEqual(tutorial["acquisition_status"], "supplemental_tutorial_domain_not_collected")
        self.assertEqual(tutorial["asset_acquisition_status"], "not_probed_by_this_census")

    def test_double_json_metadata_does_not_clear_exact_body_coverage(self):
        result = acquire(tables(), [page(url="https://example.test/mysekaiCharacterTalkTweets.json.json")])
        self.assertEqual(result["audits"][0]["source_reference_status"], "double_json_extension")
        self.assertEqual(definition(result)["acquisition_status"], "primary_current_raw_body")

    def test_malformed_page_retained_and_definition_gets_identity_invalid_debt(self):
        result = acquire(tables(), [page(canonical_key="mysekai_tweet:en:2")])
        self.assertEqual(len(result["audits"]), 1)
        self.assertEqual(definition(result)["acquisition_status"], "identity_invalid_only")

    def test_all_release_definitions_remain_unknown_including_current_pages(self):
        result = acquire(tables(), [page(lang=lang) for lang in census.REGIONS])
        self.assertEqual(len(result["release"]), len(result["inventory"]["definitions"]))
        self.assertEqual({row["status"] for row in result["release"]}, {"unknown_release"})
        self.assertTrue(all(not row["semantic_equivalence_proved"] and not row["release_proved"] for row in result["families"]))


class MysekaiCensusArtifactTests(unittest.TestCase):
    def fixture(self, root: Path):
        generation, production, out = root / "raw/generation", root / "production", root / "output"
        raw = tables()
        for lang, region in census.REGIONS.items():
            base = generation / region / "source/repo-main"
            base.mkdir(parents=True)
            for table, records in raw[lang].items():
                (base / (table + ".json")).write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        database = production / "kb/sekaisync.db"
        database.parent.mkdir(parents=True)
        row = page()
        with closing(sqlite3.connect(database)) as conn, conn:
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT)")
            conn.execute("INSERT INTO meta VALUES ('active_raw_generation',?)", (json.dumps({region: generation.name for region in census.REGIONS.values()}),))
            columns = list(row)
            conn.execute("CREATE TABLE web_pages (" + ",".join(key + (" INTEGER" if key in census.FLAGS else " TEXT") for key in columns) + ")")
            conn.execute("INSERT INTO web_pages VALUES (" + ",".join("?" for _ in columns) + ")", tuple(row.values()))
        return generation, production, out, database

    def test_full_run_is_read_only_and_artifacts_reconcile(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, production, out, database = self.fixture(Path(temporary))
            before = census.sha(database.read_bytes())
            summary = census.run(generation, production, out)
            self.assertEqual(before, census.sha(database.read_bytes()))
            self.assertEqual(summary["expected_definitions"], 15)
            self.assertEqual(summary["release_unknown_rows"], 15)
            self.assertEqual(len(summary["raw_sources"]), 30)
            self.assertEqual(summary["network_calls"], 0)
            self.assertEqual(summary["acquisition_counts"]["en/mysekai_tweet"], {"primary_current_raw_body": 1})
            self.assertEqual(summary["database_open_mode"], "ro")
            self.assertFalse(summary["production_writes"])
            self.assertEqual(summary["code_closure"]["files"], census._IMPORT_CODE_HASHES)
            self.assertTrue(summary["code_closure"]["complete_product_python_source_closure"])
            for artifact in summary["files"].values():
                self.assertEqual(artifact["sha256"], census.sha(Path(artifact["path"]).read_bytes()))
            with self.assertRaisesRegex(ValueError, "frozen or nonempty"):
                census.run(generation, production, out)

    def test_overlap_rejected_before_any_raw_or_database_access(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "overlap"):
                census.run(root / "generation", root / "production", root / "production/output")

    def test_ambiguous_raw_source_directory_fails_fast(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, production, out, _database = self.fixture(Path(temporary))
            (generation / "jp/source/another-repo").mkdir()
            with self.assertRaisesRegex(ValueError, "Exactly one"):
                census.run(generation, production, out)
            self.assertFalse(out.exists())

    def test_raw_mutation_aborts_before_success_artifacts(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, production, out, _database = self.fixture(Path(temporary))
            original = census.load_generation

            def mutate(path):
                loaded = original(path)
                target = next(iter(loaded[1].values()))["path"]
                with Path(target).open("ab") as handle:
                    handle.write(b" ")
                return loaded

            with patch.object(census, "load_generation", side_effect=mutate), self.assertRaisesRegex(ValueError, "changed during census"):
                census.run(generation, production, out)
            self.assertFalse(out.exists())

    def test_raw_mutation_during_artifact_emission_preserves_partial_without_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, production, out, _database = self.fixture(Path(temporary))
            original = census.write_jsonl
            mutated = False

            def mutate(path, rows):
                nonlocal mutated
                result = original(path, rows)
                if not mutated:
                    with (generation / "jp/source/repo-main/mysekaiCharacterTalkTweets.json").open("ab") as handle:
                        handle.write(b" ")
                    mutated = True
                return result

            with patch.object(census, "write_jsonl", side_effect=mutate), self.assertRaisesRegex(ValueError, "artifact emission"):
                census.run(generation, production, out)
            self.assertTrue((out / "expected-mysekai-definitions.jsonl").exists())
            self.assertFalse((out / "mysekai-inventory-summary.json").exists())

    def test_active_generation_drift_is_not_silently_accepted(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, production, out, database = self.fixture(Path(temporary))
            with closing(sqlite3.connect(database)) as conn, conn:
                conn.execute("UPDATE meta SET value=?", (json.dumps({region: "another" for region in census.REGIONS.values()}),))
            with self.assertRaisesRegex(ValueError, "do not match"):
                census.run(generation, production, out)
            self.assertFalse(out.exists())

    def code_fixture(self, root):
        code_root = root / "code-fixture"
        script = code_root / "scripts/census_scraper_mysekai.py"
        dependency = code_root / "sekaisync/classifier.py"
        script.parent.mkdir(parents=True)
        dependency.parent.mkdir(parents=True)
        script.write_text("# fixture census\n", encoding="utf-8")
        dependency.write_text("# classifier version A\n", encoding="utf-8")
        original = census._code_hashes
        baseline = original(code_root)
        return dependency, baseline, lambda: original(code_root)

    def test_dependency_mutation_during_computation_fails_before_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation, production, out, _database = self.fixture(root)
            dependency, baseline, current = self.code_fixture(root)
            original = census.load_generation

            def mutate(path):
                result = original(path)
                dependency.write_text("# classifier version B\n", encoding="utf-8")
                return result

            with patch.object(census, "_IMPORT_CODE_HASHES", baseline), patch.object(census, "_code_hashes", side_effect=current), \
                    patch.object(census, "load_generation", side_effect=mutate), self.assertRaisesRegex(ValueError, "code closure changed during census computation"):
                census.run(generation, production, out)
            self.assertFalse(out.exists())

    def test_dependency_mutation_during_emission_preserves_partial_without_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generation, production, out, _database = self.fixture(root)
            dependency, baseline, current = self.code_fixture(root)
            original = census.write_jsonl

            def mutate(path, rows):
                result = original(path, rows)
                dependency.write_text("# classifier version B\n", encoding="utf-8")
                return result

            with patch.object(census, "_IMPORT_CODE_HASHES", baseline), patch.object(census, "_code_hashes", side_effect=current), \
                    patch.object(census, "write_jsonl", side_effect=mutate), self.assertRaisesRegex(ValueError, "code closure changed during census artifact emission"):
                census.run(generation, production, out)
            self.assertTrue((out / "expected-mysekai-definitions.jsonl").exists())
            self.assertFalse((out / "mysekai-inventory-summary.json").exists())


if __name__ == "__main__":
    unittest.main()
