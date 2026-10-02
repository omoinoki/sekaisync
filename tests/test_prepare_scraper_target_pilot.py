"""Independent fixed-denominator blind target packet/reference fixtures."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import prepare_scraper_target_pilot as tp


class TargetPilotFreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.packet_path = self.root / "packet.json"
        self.selection_path = self.root / "selection.json"
        self.output = self.root / "target-reference.json"
        view = {"page_id": "target", "source": "fixture", "sha256": "fixture", "start": 10,
                "end": 22, "text": "A\uff1aword word."}
        self.packet = {"schema": "sekaisync/p0-fixed-source-blind-target-pilot@1", "story_key": "event:35:5",
            "items": [{"source_annotation_id": f"source:{index}", "source_segments": [{"start": index, "end": index + 1, "exact": "x"}],
                "source_meaning": "word", "target_contexts": {language: dict(view, language=language) for language in tp.TARGETS}}
                for index in range(12)]}
        self.packet["packet_sha256"] = tp.canonical(self.packet, "packet_sha256")
        self.selection = {"reviewer": "fixture", "machine_reference_not_human_gold": True,
            "host_target_candidates_or_judgments_read": False, "relations": [{"source_annotation_id": f"source:{index}",
                "target_language": language, "relation_type": "lexical", "fragments": [{"exact": "word"}], "reason": "direct fixture"}
                for index in range(12) for language in tp.TARGETS]}
        self.write(self.packet_path, self.packet)
        self.write(self.selection_path, self.selection)

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def freeze(self):
        return tp.freeze(self.packet_path, self.selection_path, self.output)

    def test_all_48_fixed_requirements_and_rerun_reproduce(self):
        result = self.freeze()
        self.assertEqual({"lexical": 48}, result["relation_counts"])
        self.assertEqual(result, self.freeze())

    def test_missing_or_duplicate_direction_rejected(self):
        self.selection["relations"].pop()
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "all 48"):
            self.freeze()

    def test_empty_literal_fragment_fails_fast(self):
        self.selection["relations"][0]["fragments"] = [{"exact": ""}]
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self.freeze()

    def test_unresolved_and_omitted_need_no_fabricated_counterpart(self):
        self.selection["relations"][0].update(relation_type="unresolved", fragments=[])
        self.selection["relations"][1].update(relation_type="omitted", fragments=[])
        self.write(self.selection_path, self.selection)
        result = self.freeze()
        self.assertEqual(1, result["relation_counts"]["unresolved"])
        self.assertEqual(1, result["relation_counts"]["omitted"])

    def test_absent_target_text_and_speaker_only_are_rejected(self):
        for exact in ("fabricated", "A"):
            with self.subTest(exact=exact):
                selection = deepcopy(self.selection)
                selection["relations"][0]["fragments"] = [{"exact": exact}]
                self.write(self.selection_path, selection)
                with self.assertRaises(ValueError):
                    self.freeze()

    def test_overlapping_or_reordered_fragments_rejected(self):
        self.selection["relations"][0]["fragments"] = [{"exact": "word", "occurrence": 1}, {"exact": "word", "occurrence": 0}]
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "unordered"):
            self.freeze()

    def test_frozen_reference_cannot_be_relabelled(self):
        self.freeze()
        self.selection["relations"][0].update(relation_type="paraphrase")
        self.write(self.selection_path, self.selection)
        with self.assertRaisesRegex(ValueError, "Frozen scoring output differs"):
            self.freeze()


if __name__ == "__main__":
    unittest.main()
