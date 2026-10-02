"""Script metrics remain descriptive, speaker-free and read-only."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from scripts import audit_scraper_language_scripts as audit


class ScriptProfileTests(unittest.TestCase):
    def test_script_only_mixed_numeric_and_symbol_categories(self):
        examples = {
            "Hello, caf\u00e9! 12": "latin_only", "\ud55c\uae00": "hangul_only",
            "\u4e2d\u6587": "han_only", "\u304b\u306a\u30ab\u30ca": "kana_only",
            "\ud55c\uae00 Hello!": "mixed_scripts", "123.45!?": "numeric_only",
            "...!?": "no_letters_or_numbers", " \n": "empty", "\u03bb": "other_letters_only",
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(expected, audit.script_profile(text)["category"])
        self.assertEqual(["latin"], audit.script_profile("caf\u00e9")["present_scripts"])
        self.assertEqual(["hangul"], audit.script_profile("\u1100\u1161")["present_scripts"])

    def test_original_body_offsets_and_speaker_masking(self):
        text = "\ud55c\uad6d\uc5b4 \uc774\ub984\uff1aHello!\r\nEnglish Name: \ud55c\uae00\n12:30 starts soon"
        lines = audit.body_lines(text)
        self.assertEqual("Hello!", lines[0]["text"])
        self.assertEqual("latin_only", lines[0]["profile"]["category"])
        self.assertEqual("\ud55c\uae00", lines[1]["text"])
        self.assertEqual("hangul_only", lines[1]["profile"]["category"])
        self.assertEqual("12:30 starts soon", lines[2]["text"])
        self.assertFalse(lines[2]["speaker_removed"])
        for line in lines:
            self.assertEqual(line["text"], text[line["start"]:line["end"]])


class LanguageScriptAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store, self.out = self.root / "production", self.root / "audit"
        self.db = self.store / "kb/sekaisync.db"
        self.db.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE web_pages (source TEXT,id TEXT,url TEXT,kind TEXT,language TEXT,text TEXT,text_hash TEXT,"
                     "trust TEXT,asset_mismatch TEXT,scenario_id_mismatch TEXT,content_language_mismatch INTEGER,"
                     "untranslated INTEGER,aux_flag INTEGER,auxiliary INTEGER)")
        conn.commit()
        conn.close()

    def insert(self, page_id, text, language="ko", kind="area_talk", flagged=False):
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO web_pages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            "altsource_ms", page_id, "https://example.test/" + page_id, kind, language, text,
            audit.sha(text.encode()), "B", "" if not flagged else "asset mismatch", "", 0, 0, 0, 0))
        conn.commit()
        conn.close()

    def test_read_only_census_exports_full_latin_ko_and_descriptive_anomalies(self):
        self.insert("ko-song", "\ud55c\uad6d\uc5b4 \uc774\ub984\uff1aHappy Synthesizer!")
        self.insert("ko-title", "\ud55c\uad6d\uc5b4 \uc774\ub984\uff1aHatsune Miku", flagged=True)
        self.insert("ko-numeric", "\ud55c\uad6d\uc5b4 \uc774\ub984\uff1a123?!")
        self.insert("ko-real", "\ud55c\uad6d\uc5b4 \uc774\ub984\uff1a\uc548\ub155\ud558\uc138\uc694!")
        self.insert("en-korean", "English Name: \uc548\ub155\ud558\uc138\uc694!", language="en")
        self.insert("en-real", "English Name: Hi there!", language="en")
        self.insert("outside-story", "Not included", kind="news")
        self.insert("outside-language", "Not included", language="ja")
        before = self.db.read_bytes()
        result = audit.run(self.store, self.out)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(6, result["source_rows"])
        self.assertEqual(2, result["body_category_counts"]["ko:all"]["latin_only"])
        self.assertEqual(1, result["body_category_counts"]["ko:unflagged_primary_candidate"]["latin_only"])
        self.assertEqual(1, result["body_category_counts"]["en:all"]["hangul_only"])
        self.assertEqual(1, result["en_hangul_without_latin_distinct_bodies"])
        self.assertEqual(4, sum(result["source_kind_category_counts"]["ko:altsource_ms:area_talk"].values()))
        packets = json.loads((self.out / "kr-latin-only-review-packets.json").read_bytes())["packets"]
        self.assertEqual(2, len(packets))
        self.assertTrue(all(row["semantic_review"] == "pending_host_agent_review" for row in packets))
        self.assertTrue(all(row["body_profile"]["category"] == "latin_only" for row in packets))
        self.assertTrue(all(row["raw_profile"]["category"] == "mixed_scripts" for row in packets))
        self.assertIn("No automatic wrong-language", result["judgment_boundary"])
        with self.assertRaisesRegex(ValueError, "frozen or nonempty"):
            audit.run(self.store, self.out)

    def test_distinct_longest_first_review_is_capped_at_20(self):
        for index in range(25):
            self.insert(str(index), "\ud55c\uad6d\uc5b4 \uc774\ub984\uff1a" + "Hello " * (index + 1))
        self.insert("same-content-other-page", "\ud55c\uad6d\uc5b4 \uc774\ub984\uff1a" + "Hello " * 25)
        result = audit.run(self.store, self.out)
        self.assertEqual(25, result["ko_latin_only_distinct_bodies"])
        self.assertEqual(20, result["ko_latin_only_full_bodies_exported"])
        packets = json.loads((self.out / "kr-latin-only-review-packets.json").read_bytes())["packets"]
        self.assertEqual(1, len(packets[0]["equivalent_pages"]))
        self.assertEqual(sorted([len(row["body_text"]) for row in packets], reverse=True),
                         [len(row["body_text"]) for row in packets])

    def test_bad_cap_and_store_overlap_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "capped at 20"):
            audit.run(self.store, self.out, 21)
        with self.assertRaisesRegex(ValueError, "must not overlap"):
            audit.run(self.store, self.store / "audit")

    def test_policy_comparison_is_hash_bound_to_frozen_rows_and_retains_flags(self):
        self.insert("ko-long-english", "Try talking to the characters nearby.")
        self.insert("ko-borrowed-slogan", "Make everyone smile!")
        self.insert("ko-flagged-long", "Try talking to the characters nearby.", flagged=True)
        audit.run(self.store, self.out)
        comparison = self.root / "comparison"
        before = self.db.read_bytes()
        with patch.object(audit, "text_matches_language", side_effect=lambda _language, text: text == "Make everyone smile!"):
            result = audit.compare_policy(self.store, self.out, comparison)
        self.assertEqual(before, self.db.read_bytes())
        self.assertEqual(2, result["transition_counts"]["ko:all"]["newly_rejected"])
        self.assertEqual(1, result["transition_counts"]["ko:unflagged_primary_candidate"]["newly_rejected"])
        self.assertEqual(1, result["transition_counts"]["ko:all"]["unchanged_pass"])
        self.assertIn("not translation-error labels", result["judgment_boundary"])
        with self.assertRaisesRegex(ValueError, "frozen or nonempty"):
            audit.compare_policy(self.store, self.out, comparison)
        self.insert("new-row", "New body")
        with self.assertRaisesRegex(ValueError, "corpus changed"):
            audit.compare_policy(self.store, self.out, self.root / "changed-comparison")

    def test_legacy_comparison_reproduces_no_han_loophole(self):
        self.assertTrue(audit.legacy_en_ko_matches("en", "\ud55c\uae00"))
        self.assertTrue(audit.legacy_en_ko_matches("ko", "Entirely English prose without Han characters."))


if __name__ == "__main__":
    unittest.main()
