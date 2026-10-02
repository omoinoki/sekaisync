from __future__ import annotations

import copy
import hashlib
import json
import unittest

from sekaisync import mysekai_moly as moly


class MolyTextTests(unittest.TestCase):
    def setUp(self):
        self.preview = {"available": True, "tweetId": 10001, "text": "\u4f60\u597d\u3002", "unit": 1}
        self.detail = {"key": "talk:fixture:1", "preview": self.preview,
                       "presentation": {"behavior": "authored", "textMode": "transcript"},
                       "lines": [{"speaker": "\u4e00\u6b4c", "text": "\u4f60\u597d\u3002\n\u5bb6\u5177\u5bf9\u767d\u3002"}]}
        self.bundle_text = json.dumps({"schemaVersion": 2, "entries": {self.detail["key"]: self.detail}}, ensure_ascii=False)
        self.bundle_hash = hashlib.sha256(self.bundle_text.encode()).hexdigest()
        self.snapshot = {"id": "cn-6.0.0-test01", "region": "cn", "version": "6.0.0", "available": True,
                         "catalog": "/moly/snapshots/cn-6.0.0-test01/catalog/index.json", "provenance": {}}
        self.entry = {"key": self.detail["key"], "preview": self.preview, "detail": 0}
        self.catalog = {"schemaVersion": 2, "snapshotId": self.snapshot["id"], "region": "cn", "version": "6.0.0",
                        "details": [f"/moly/catalog-store/{self.bundle_hash}.json"], "entries": [self.entry]}
        self.talks = [{"id": 1, "assetbundleName": "mysekai/talk/scenario/talk", "lua": "talk_001"}]
        self.tweets = [{"id": 10001, "text": self.preview["text"]}]
        self.edges = [{"id": 1, "mysekaiCharacterTalkId": 1, "mysekaiCharacterTalkTweetId": 10001}]

    def candidates(self):
        return moly.prepare(self.snapshot, self.catalog, "a" * 64, self.talks, self.tweets, self.edges)

    def test_exact_text_and_speaker_boundaries(self):
        candidate, = self.candidates()
        bundle = moly.decode_bundle(self.bundle_text, self.bundle_hash)
        text, lines = moly.transcript(candidate, bundle)
        self.assertEqual(text, "\u4e00\u6b4c\uff1a\u4f60\u597d\u3002\n\u5bb6\u5177\u5bf9\u767d\u3002")
        self.assertEqual(lines, self.detail["lines"])
        self.assertEqual(candidate["provenance"]["current_body_freshness"], "snapshot_only_unknown")

    def test_boolean_and_float_official_identity_rejected(self):
        for value in (True, 1.0):
            for field in ("id", "mysekaiCharacterTalkId", "mysekaiCharacterTalkTweetId"):
                with self.subTest(field=field, value=value):
                    self.edges = [{"id": 1, "mysekaiCharacterTalkId": 1, "mysekaiCharacterTalkTweetId": 10001}]
                    self.edges[0][field] = value
                    self.assertEqual(self.candidates(), [])

    def test_raw_boolean_id_rejected(self):
        self.talks[0]["id"] = True
        self.assertEqual(self.candidates(), [])

    def test_duplicate_raw_id_and_multiple_edges_for_one_talk_rejected(self):
        self.talks.append(copy.deepcopy(self.talks[0]))
        self.assertEqual(self.candidates(), [])
        self.talks.pop()
        self.edges.append({"id": 2, "mysekaiCharacterTalkId": 1, "mysekaiCharacterTalkTweetId": 10001})
        self.assertEqual(self.candidates(), [])

    def test_shared_preview_preserves_each_unique_talk_identity(self):
        second = copy.deepcopy(self.talks[0])
        second["id"] = 2
        self.talks.append(second)
        self.edges.append({"id": 2, "mysekaiCharacterTalkId": 2, "mysekaiCharacterTalkTweetId": 10001})
        second_entry = copy.deepcopy(self.entry)
        second_entry["key"] = "talk:fixture:2"
        self.catalog["entries"].append(second_entry)
        candidates = self.candidates()
        self.assertEqual([row["id"] for row in candidates], [1, 2])
        self.assertNotEqual(candidates[0]["binding_hash"], candidates[1]["binding_hash"])
        self.assertEqual([row["provenance"]["raw_preaction"]["mysekaiCharacterTalkId"]
                          for row in candidates], [1, 2])

    def test_unrelated_dangling_edge_does_not_erase_a_valid_talk(self):
        self.edges.append({"id": 2, "mysekaiCharacterTalkId": 2, "mysekaiCharacterTalkTweetId": 10001})
        self.assertEqual([row["id"] for row in self.candidates()], [1])

    def test_duplicate_catalog_and_dual_namespace_not_merged(self):
        self.catalog["entries"].append(copy.deepcopy(self.entry))
        with self.assertRaisesRegex(ValueError, "duplicate_catalog_key"):
            self.candidates()
        self.catalog["entries"][-1]["key"] = "talk:general:1"
        self.assertEqual(self.candidates(), [])

    def test_oversized_numeric_key_does_not_suppress_later_valid_talk(self):
        oversized = copy.deepcopy(self.entry)
        oversized["key"] = "talk:fixture:" + "9" * 5000
        self.catalog["entries"].insert(0, oversized)
        self.assertEqual([row["id"] for row in self.candidates()], [1])

    def test_details_must_be_actual_list_not_integer_keyed_dict(self):
        self.catalog["details"] = {0: self.catalog["details"][0]}
        with self.assertRaisesRegex(ValueError, "catalog_shape_invalid"):
            self.candidates()

    def test_detail_pointer_boolean_rejected(self):
        self.entry["detail"] = False
        self.assertEqual(self.candidates(), [])

    def test_missing_snapshot_identity_and_version_never_match(self):
        for value in (None, ""):
            with self.subTest(value=value):
                self.snapshot["id"] = self.catalog["snapshotId"] = value
                self.snapshot["version"] = self.catalog["version"] = value
                with self.assertRaisesRegex(ValueError, "snapshot_identity_invalid"):
                    self.candidates()

    def test_wrong_region_catalog_and_unsupported_snapshot(self):
        self.catalog["region"] = "jp"
        with self.assertRaisesRegex(ValueError, "catalog_snapshot_mismatch"):
            self.candidates()
        manifest = {"schemaVersion": 2, "snapshots": [self.snapshot]}
        self.assertIsNone(moly.select_snapshot(manifest, "en"))

    def test_unsafe_asset_and_lua_paths_rejected(self):
        for field in ("assetbundleName", "lua"):
            for value in ("/absolute", "https://evil.example/a", "../relative", "foo\\bar", "x?y", ""):
                with self.subTest(field=field, value=value):
                    original = self.talks[0][field]
                    self.talks[0][field] = value
                    self.assertEqual(self.candidates(), [])
                    self.talks[0][field] = original

    def test_bundle_hash_and_conflicting_json_fields_rejected(self):
        with self.assertRaisesRegex(ValueError, "bundle_hash_mismatch"):
            moly.decode_bundle(self.bundle_text, "b" * 64)
        text = '{"schemaVersion":1,"schemaVersion":2,"entries":{}}'
        with self.assertRaisesRegex(ValueError, "duplicate_json_field"):
            moly.decode_bundle(text, hashlib.sha256(text.encode()).hexdigest())

    def test_preview_equal_single_line_is_ambiguous_even_with_label(self):
        candidate, = self.candidates()
        bundle = moly.decode_bundle(self.bundle_text, self.bundle_hash)
        bundle[self.entry["key"]]["lines"] = [{"speaker": "different label", "text": self.preview["text"]}]
        with self.assertRaisesRegex(ValueError, "preview_only_or_ambiguous"):
            moly.transcript(candidate, bundle)

    def test_exact_exporter_plain_text_keeps_newline_tab_and_spelling(self):
        self.assertEqual(moly.plain_text("A\n<color=#fff>B</color>\tC\x01"), "A\nB\tC")

    def test_full_binding_changes_on_snapshot_raw_and_version_changes(self):
        original = self.candidates()[0]["binding_hash"]
        self.talks[0]["lua"] = "talk_002"
        self.assertNotEqual(original, self.candidates()[0]["binding_hash"])
        self.talks[0]["lua"] = "talk_001"
        versions = {"appVersion": "6.4.0", "assetVersion": "6.4.0.1"}
        changed = moly.prepare(self.snapshot, self.catalog, "a" * 64, self.talks, self.tweets, self.edges, versions)[0]
        self.assertNotEqual(original, changed["binding_hash"])
        self.assertTrue(changed["provenance"]["version_debts"])


if __name__ == "__main__":
    unittest.main()
