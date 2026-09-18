"""P04 verify_claims semantics — absence of data is not a refutation.

Before the fix, ``verify_claims`` returned ``conflict`` whenever an expected
string was not among an entity's names/fact values.  That reads as "the store
contradicts this claim", when all it actually established was "this string is
not in my name slots".  A store with partial coverage would therefore refute
true statements.

Mirrors Astra P04's acceptance list:
- missing entity            -> unknown (not a refutation)
- free text that misses     -> unknown (not conflict)
- explicit field that exists and differs -> conflict, WITH the actual value
- explicit field that does not exist     -> unknown (not conflict)
"""

import tempfile
import unittest
from pathlib import Path

from sekaisync.core import SekaiSyncCore
from sekaisync.layout import registry_path
from sekaisync.models import Entity
from sekaisync.registry import save_registry


class VerifyClaimsSemanticsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_p04_")
        self.store = Path(self._tmp.name) / "store"
        save_registry(
            [
                Entity(
                    id="character:ichika",
                    type="character",
                    region="jp",
                    regions=["jp"],
                    names={"en": "Hoshino Ichika", "ja": "星乃一歌"},
                    facts={"unit": "Leo/need", "birthday": "3月7日"},
                    source="master_db:jp",
                ),
                Entity(
                    id="character:multi",
                    type="character",
                    region="jp",
                    regions=["jp", "en"],
                    names={"en": "Multi Region Idol"},
                    facts={"startAt": 1700000000000},
                    source="master_db:jp",
                ),
            ],
            registry_path(self.store),
        )
        self.core = SekaiSyncCore(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _one(self, claim):
        return self.core.verify_claims([claim])[0]

    def test_unknown_entity_is_unknown_not_unverified_conflict(self):
        result = self._one({"claim": "Totally Absent Topic", "expected": "Anything"})
        self.assertEqual(result["status"], "unknown")
        # The coverage note must survive: it is the anti-hallucination cue.
        self.assertIn("coverage_note", result)

    def test_free_text_miss_is_unknown_not_conflict(self):
        """A name that simply is not stored must not be reported as refuted."""
        result = self._one(
            {"claim": "Hoshino Ichika", "expected": "Completely Different Name"}
        )
        self.assertEqual(
            result["status"],
            "unknown",
            "a string absent from the name slots was reported as a conflict, "
            "which asserts a refutation the store cannot support",
        )
        self.assertIn("reason", result)

    def test_name_match_still_matched(self):
        result = self._one({"claim": "Hoshino Ichika", "expected": "星乃一歌"})
        self.assertEqual(result["status"], "matched")

    def test_no_expected_is_ambiguous(self):
        result = self._one({"claim": "Hoshino Ichika", "expected": ""})
        self.assertEqual(result["status"], "ambiguous")

    def test_absent_field_is_unknown_not_conflict(self):
        """The store has no 'height' for this entity — that is a gap, not a refutation."""
        result = self._one(
            {
                "claim": "Hoshino Ichika",
                "entity_id": "character:ichika",
                "field": "height",
                "expected": "200cm",
            }
        )
        self.assertEqual(
            result["status"],
            "unknown",
            "a missing field was reported as a conflict",
        )
        self.assertIn("supported_fields", result)
        self.assertIn("unit", result["supported_fields"])

    def test_present_field_agreement_is_supported(self):
        result = self._one(
            {
                "claim": "Hoshino Ichika",
                "entity_id": "character:ichika",
                "field": "unit",
                "expected": "Leo/need",
            }
        )
        self.assertEqual(result["status"], "supported")

    def test_present_field_disagreement_is_conflict_with_actual(self):
        """A real refutation must still work, and must carry the stored value."""
        result = self._one(
            {
                "claim": "Hoshino Ichika",
                "entity_id": "character:ichika",
                "field": "unit",
                "expected": "MORE MORE JUMP!",
            }
        )
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["actual"], "Leo/need")
        self.assertTrue(result["evidence"])

    def test_field_evidence_carries_provenance(self):
        result = self._one(
            {
                "claim": "Hoshino Ichika",
                "entity_id": "character:ichika",
                "field": "unit",
                "expected": "Leo/need",
            }
        )
        ev = result["evidence"][0]
        for key in ("entity_id", "source", "trust", "field", "value"):
            self.assertIn(key, ev, f"field evidence lost '{key}'")

    def test_multi_region_field_without_region_needs_region_data(self):
        """v1 cannot attribute a region-scoped value to one region."""
        result = self._one(
            {
                "claim": "Multi Region Idol",
                "entity_id": "character:multi",
                "field": "startAt",
                "expected": "1800000000000",
            }
        )
        self.assertEqual(result["status"], "needs_region_data")


if __name__ == "__main__":
    unittest.main()
