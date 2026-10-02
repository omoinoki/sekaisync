"""Moly transport proofs are not Lua URLs or independently verified bundles."""
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import census_scraper_mysekai as census
from sekaisync import mysekai_moly
from tests import test_census_scraper_mysekai as census_fixtures
from tests.test_census_scraper_mysekai import definition, page, tables, TEXT


def fixture(lang="en", identity=1, raw=None):
    raw = tables() if raw is None else raw
    region = census.REGIONS[lang]
    snapshot = dict(id=f"{region}-6.0.0-test01", version="6.0.0", region=region, available=True,
                    catalog=f"/moly/snapshots/{region}-6.0.0-test01/catalog/index.json", provenance={})
    entry = dict(key=f"talk:general:{identity}", preview=dict(available=True, tweetId=1, text=TEXT[lang]), detail=0)
    catalog = dict(schemaVersion=2, snapshotId=snapshot["id"], version="6.0.0", region=region,
                   entries=[entry], details=["/moly/catalog-store/" + "a" * 64 + ".json"])
    candidate, = mysekai_moly.prepare(snapshot, catalog, "b" * 64, raw[lang][census.TALKS],
                                     raw[lang][census.TWEETS], raw[lang][census.PREACTIONS])
    lines = [dict(speaker="", text=TEXT[lang]), dict(speaker="", text=TEXT[lang])]
    body = "\n".join(line["text"] for line in lines)
    body_page = page(lang=lang, source="altsource_ms", kind="mysekai_talk", local_id=identity, text=body,
                     url="https://example.test" + candidate["path"], hash=hashlib.sha1(body.encode()).hexdigest()[:16],
                     source_hash=candidate["binding_hash"], source_etag=candidate["bundle_hash"])
    proof = dict(candidate["provenance"], source_hash=candidate["binding_hash"],
                 actual_source_url=body_page["url"], original_lines=lines,
                 script_fallback_excluded=True, text_sha256=census.sha(body.encode()))
    return raw, body_page, proof


def observe(raw, body_page, proof, *, error=None):
    inventory = census.build_inventory(raw)
    inventory["_moly_provenance"] = {body_page["source"]: dict(records={} if proof is None else {body_page["id"]: proof}, error=error)}
    audit = census.audit_page(body_page, inventory)
    debts, release = census.assign_acquisition(inventory, [audit])
    return dict(inventory=inventory, audits=[audit], debts=debts, release=release,
                families=census.structural_families(inventory))


def resign(body_page, proof):
    binding = {field: proof[field] for field in census.MOLY_BINDING_FIELDS}
    proof["source_hash"] = census.sha(census.canonical(binding).encode())
    body_page["source_hash"] = proof["source_hash"]


class MolyCensusProofTests(unittest.TestCase):
    def test_real_moesekai_locale_ids_use_the_exact_same_region(self):
        for locale, lang in census.SOURCE_LOCALES.items():
            with self.subTest(locale=locale):
                raw, candidate, proof = fixture(lang)
                candidate["id"] = candidate["id"].replace(":" + census.REGIONS[lang] + ":", ":" + locale + ":")
                result = observe(raw, candidate, proof)
                self.assertEqual(result["audits"][0]["identity_status"], "expected")
                self.assertEqual(definition(result, census.TALKS, lang)["acquisition_status"],
                                 "primary_usable_snapshot_body_provenance_bound")
        for locale in ("ja-anything", "ja-kr", "en-jp", "ko-en"):
            with self.subTest(unknown_locale=locale):
                raw, candidate, proof = fixture()
                candidate["id"] = candidate["id"].replace(":en:", ":" + locale + ":")
                self.assertEqual(observe(raw, candidate, proof)["audits"][0]["identity_status"], "invalid")

    def test_all_localized_json_sources_bind_without_faking_lua_urls(self):
        for lang in census.REGIONS:
            with self.subTest(lang=lang):
                raw, candidate, proof = fixture(lang)
                result = observe(raw, candidate, proof)
                audit = result["audits"][0]
                self.assertEqual(audit["asset_path_binding"], "exact_moly_snapshot_source_binding")
                self.assertEqual(definition(result, census.TALKS, lang)["acquisition_status"],
                                 "primary_usable_snapshot_body_provenance_bound")
                self.assertEqual(audit["url"], candidate["url"])
                self.assertEqual(audit["body_freshness"], "snapshot_body_current_freshness_unproved")
                self.assertEqual(audit["moly_provenance"]["evidence_level"], "local_sidecar_and_sealed_raw_body")
                self.assertFalse(audit["moly_provenance"]["catalog_bundle_bytes_independently_verified"])
                self.assertEqual({row["status"] for row in result["release"]}, {"unknown_release"})
                self.assertFalse(any(row["semantic_equivalence_proved"] or row["release_proved"]
                                     or row["body_freshness_proved"] for row in result["families"]
                                     if row["raw_composite_identity"]["domain"] == "mysekai_talk"))

    def test_missing_and_malformed_provenance_are_named_debts(self):
        raw, candidate, _ = fixture()
        for proof, error, expected in ((None, None, "moly_snapshot_provenance_missing"),
                                       (None, "ValueError", "moly_snapshot_provenance_invalid"),
                                       ([], None, "moly_snapshot_provenance_invalid")):
            with self.subTest(expected=expected, error=error):
                result = observe(raw, candidate, proof, error=error)
                self.assertEqual(definition(result, census.TALKS)["acquisition_status"], expected)
                self.assertTrue(any(row["acquisition_status"] == expected for row in result["debts"]))

    def test_changed_page_body_hashes_and_original_lines_cannot_get_credit(self):
        for field in ("text", "text_hash", "hash"):
            with self.subTest(field=field):
                raw, candidate, proof = fixture()
                candidate[field] += "X"
                result = observe(raw, candidate, proof)
                self.assertEqual(definition(result, census.TALKS)["acquisition_status"], "moly_snapshot_body_hash_mismatch")
        raw, candidate, proof = fixture()
        proof["original_lines"][0]["text"] += "X"
        self.assertEqual(definition(observe(raw, candidate, proof), census.TALKS)["acquisition_status"],
                         "moly_snapshot_body_hash_mismatch")

    def test_resigned_raw_claims_still_need_exact_independent_raw_records(self):
        for field, change in (("raw_talk", ("lua", "other_script")),
                              ("raw_preaction", ("mysekaiCharacterTalkTweetId", 2)),
                              ("raw_preview_tweet", ("text", "Different preview"))):
            with self.subTest(field=field):
                raw, candidate, proof = fixture()
                proof = copy.deepcopy(proof)
                proof[field][change[0]] = change[1]
                resign(candidate, proof)
                self.assertEqual(definition(observe(raw, candidate, proof), census.TALKS)["acquisition_status"],
                                 "moly_snapshot_provenance_invalid")

    def test_identity_url_and_digest_damage_cannot_be_promoted(self):
        mutations = (("schema", "other"), ("region", "jp"), ("key", "talk:general:2"),
                     ("snapshot_id", "en-6.0.0-other/../bad"), ("snapshot_version", "7.0.0"),
                     ("detail_sha256", "c" * 64), ("catalog_sha256", "not-a-hash"),
                     ("actual_source_url", "https://other.test/moly/catalog-store/" + "a" * 64 + ".json"),
                     ("source_hash", "c" * 64), ("script_fallback_excluded", 1),
                     ("release_status", "released"), ("current_body_freshness", "current"),
                     ("version_debts", []))
        for field, value in mutations:
            with self.subTest(field=field):
                raw, candidate, proof = fixture()
                proof[field] = value
                self.assertNotEqual(census.audit_page(candidate, observe(raw, candidate, proof)["inventory"])["asset_path_binding"],
                                    "exact_moly_snapshot_source_binding")
        for field, value in (("canonical_key", ""), ("source_etag", "c" * 64),
                              ("url", "https://example.test/moly/catalog-store/" + "a" * 64 + ".json?x=1")):
            with self.subTest(page_field=field):
                raw, candidate, proof = fixture()
                candidate[field] = value
                self.assertNotEqual(definition(observe(raw, candidate, proof), census.TALKS)["acquisition_status"],
                                    "primary_usable_snapshot_body_provenance_bound")

    def test_preview_fallback_cannot_be_blessed_by_a_true_sidecar_flag(self):
        raw, candidate, proof = fixture()
        proof["original_lines"] = [proof["original_lines"][0]]
        candidate["text"] = TEXT["en"]
        proof["text_sha256"] = candidate["text_hash"] = census.sha(candidate["text"].encode())
        candidate["hash"] = hashlib.sha1(candidate["text"].encode()).hexdigest()[:16]
        self.assertEqual(definition(observe(raw, candidate, proof), census.TALKS)["acquisition_status"],
                         "moly_snapshot_provenance_invalid")

    def test_per_talk_edge_and_edge_id_must_be_unique(self):
        for variant in ("extra_talk_edge", "duplicate_edge_id", "dangling_tweet"):
            with self.subTest(variant=variant):
                raw, candidate, proof = fixture()
                if variant == "extra_talk_edge":
                    raw["en"][census.PREACTIONS].append(dict(raw["en"][census.PREACTIONS][0], id=2))
                elif variant == "duplicate_edge_id":
                    raw["en"][census.PREACTIONS].append(dict(raw["en"][census.PREACTIONS][0], mysekaiCharacterTalkId=2))
                else:
                    raw["en"][census.TWEETS] = []
                self.assertEqual(definition(observe(raw, candidate, proof), census.TALKS)["acquisition_status"],
                                 "moly_snapshot_provenance_invalid")

    def test_shared_preview_aliases_remain_distinct_body_obligations(self):
        raw = tables()
        raw["en"][census.TALKS].append(dict(raw["en"][census.TALKS][0], id=2))
        raw["en"][census.PREACTIONS].append(dict(raw["en"][census.PREACTIONS][0], id=2, mysekaiCharacterTalkId=2))
        _, first, first_proof = fixture(raw=raw)
        _, second, second_proof = fixture(identity=2, raw=raw)
        inventory = census.build_inventory(raw)
        inventory["_moly_provenance"] = {"altsource_ms": dict(records={first["id"]: first_proof, second["id"]: second_proof}, error=None)}
        census.assign_acquisition(inventory, [census.audit_page(row, inventory) for row in (first, second)])
        definitions = inventory["lookup"]["en", census.TALKS, 1] + inventory["lookup"]["en", census.TALKS, 2]
        self.assertEqual(len(definitions), 2)
        self.assertEqual({row["acquisition_status"] for row in definitions}, {"primary_usable_snapshot_body_provenance_bound"})

    def test_unusable_page_or_wrong_source_identity_never_fills_a_body_debt(self):
        for field in (*census.FLAGS, "source"):
            with self.subTest(field=field):
                raw, candidate, proof = fixture()
                candidate[field] = "another_source" if field == "source" else 1
                self.assertNotEqual(definition(observe(raw, candidate, proof), census.TALKS)["acquisition_status"],
                                    "primary_usable_snapshot_body_provenance_bound")


class MolyCensusRunTests(unittest.TestCase):
    def run_fixture(self, root):
        generation, store, out, database = census_fixtures.MysekaiCensusArtifactTests().fixture(root)
        _, candidate, proof = fixture()
        with closing(sqlite3.connect(database)) as conn, conn:
            for key in ("hash", "source_etag"):
                conn.execute("ALTER TABLE web_pages ADD COLUMN " + key + " TEXT")
            columns = list(candidate)
            conn.execute("INSERT INTO web_pages (" + ",".join(columns) + ") VALUES (" + ",".join("?" for _ in columns) + ")",
                         tuple(candidate.values()))
        path = store / "cache/altsource_ms/mysekai_moly_provenance.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({candidate["id"]: proof}), encoding="utf-8")
        return generation, store, out, database, path

    def test_normal_run_loads_real_sidecar_and_keeps_both_inputs_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, store, out, database, sidecar = self.run_fixture(Path(temporary))
            hashes = {path: census.sha(path.read_bytes()) for path in (database, sidecar)}
            summary = census.run(generation, store, out)
            self.assertEqual(summary["schema"], "sekaisync/p0-full-mysekai-census@2")
            self.assertEqual(summary["acquisition_counts"]["en/mysekai_talk"], {"primary_usable_snapshot_body_provenance_bound": 1})
            self.assertEqual(summary["moly_evidence_level_counts"], {"local_sidecar_and_sealed_raw_body": 1})
            self.assertEqual(summary["moly_sidecar_inputs"]["altsource_ms"]["sha256"], hashes[sidecar])
            self.assertEqual(summary["release_unknown_rows"], 15)
            self.assertEqual(hashes, {path: census.sha(path.read_bytes()) for path in hashes})

    def test_duplicate_json_fields_are_invalid_proofs_not_silently_last_wins(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, store, out, _, sidecar = self.run_fixture(Path(temporary))
            sidecar.write_text('{"same":{},"same":{}}', encoding="utf-8")
            summary = census.run(generation, store, out)
            self.assertEqual(summary["acquisition_counts"]["en/mysekai_talk"], {"moly_snapshot_provenance_invalid": 1})

    def test_provenance_mutation_during_computation_aborts_before_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            generation, store, out, _, sidecar = self.run_fixture(Path(temporary))
            original = census.assign_acquisition

            def mutate(*args):
                result = original(*args)
                with sidecar.open("ab") as handle:
                    handle.write(b" ")
                return result

            with patch.object(census, "assign_acquisition", side_effect=mutate), self.assertRaisesRegex(ValueError, "provenance input changed"):
                census.run(generation, store, out)
            self.assertFalse(out.exists())
