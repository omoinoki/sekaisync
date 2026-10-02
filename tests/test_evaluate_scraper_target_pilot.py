"""Independent relation-type and raw-span target scoring fixtures."""
import json
import unittest

from scripts import evaluate_scraper_target_pilot as ts
from scripts import prepare_scraper_target_pilot as tp
from tests import test_prepare_scraper_target_pilot as freeze_fixtures


class TargetPilotScoreTests(unittest.TestCase):
    def setUp(self):
        self.fixture = freeze_fixtures.TargetPilotFreezeTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        fixture = self.fixture
        fixture.packet["relation_rubric"] = tp.RELATIONS
        for item in fixture.packet["items"]:
            item["source_surface"] = "x"
        fixture.packet["packet_sha256"] = tp.canonical(fixture.packet, "packet_sha256")
        fixture.write(fixture.packet_path, fixture.packet)
        self.judgments_path = fixture.root / "judgments.json"
        self.output = fixture.root / "score.json"

    def prepare_judgments(self):
        self.fixture.freeze()
        reference = json.loads(self.fixture.output.read_bytes())
        judgments = [{key: item[key] for key in ("source_annotation_id", "target_language", "relation_type", "target_segments", "reason")}
                     for item in reference["annotations"]]
        self.fixture.write(self.judgments_path, {"judgments": judgments})
        return judgments

    def score(self):
        return ts.score(self.fixture.packet_path, self.fixture.output, self.judgments_path, self.output,
                        ts.digest(self.judgments_path.read_bytes()))

    def test_exact_typed_directions_reproduce(self):
        self.prepare_judgments()
        result = self.score()
        self.assertEqual(48, result["summary"]["typed_direction_strict_agreement"])
        self.assertEqual(12, result["all_four_typed_directions_strict_agree_source_units"])
        self.assertEqual(result, self.score())

    def test_explicit_record_wrapper_binds_the_original_blind_packet(self):
        judgments = self.prepare_judgments()
        self.fixture.write(self.judgments_path, {"records": judgments,
            "packet_sha256": self.fixture.packet["packet_sha256"], "target_reference_read": False})
        self.assertEqual(48, self.score()["summary"]["typed_direction_strict_agreement"])

    def test_raw_validity_is_not_relation_accuracy(self):
        judgments = self.prepare_judgments()
        judgments[0]["relation_type"] = "paraphrase"
        self.fixture.write(self.judgments_path, {"judgments": judgments})
        result = self.score()
        self.assertEqual(48, result["summary"]["raw_target_segments_valid"])
        self.assertEqual(48, result["summary"]["strict_primary_span_agreement"])
        self.assertEqual(47, result["summary"]["relation_type_agreement"])

    def test_shifted_or_fabricated_target_segment_is_rejected(self):
        judgments = self.prepare_judgments()
        judgments[0]["target_segments"][0]["start"] += 1
        self.fixture.write(self.judgments_path, {"judgments": judgments})
        with self.assertRaisesRegex(ValueError, "shifted"):
            self.score()

    def test_missing_direction_cannot_shrink_denominator(self):
        judgments = self.prepare_judgments()
        judgments.pop()
        self.fixture.write(self.judgments_path, {"judgments": judgments})
        with self.assertRaisesRegex(ValueError, "48 unchanged"):
            self.score()

    def test_omitted_false_accept_is_not_counted_as_coverage(self):
        self.fixture.selection["relations"][0].update(relation_type="omitted", fragments=[])
        self.fixture.write(self.fixture.selection_path, self.fixture.selection)
        judgments = self.prepare_judgments()
        judgments[0].update(relation_type="lexical", target_segments=[{"start": 12, "end": 16, "exact": "word"}])
        self.fixture.write(self.judgments_path, {"judgments": judgments})
        result = self.score()
        self.assertEqual(1, result["summary"]["machine_reference_omitted_false_accepts"])
        self.assertEqual(1, result["summary"]["machine_reference_lexical_upgrades"])
        self.assertEqual(47, result["summary"]["typed_direction_strict_agreement"])

    def test_frozen_input_hash_mismatch_fails_fast(self):
        self.prepare_judgments()
        with self.assertRaisesRegex(ValueError, "judgment hash mismatch"):
            ts.score(self.fixture.packet_path, self.fixture.output, self.judgments_path, self.output, "0" * 64)


if __name__ == "__main__":
    unittest.main()
