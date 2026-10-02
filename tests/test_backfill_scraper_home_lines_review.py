"""Independent exact-debt identity review using synthetic sealed inventories."""
import json
import unittest

from scripts import backfill_scraper_home_lines as recovery


class HomeBackfillReviewTests(unittest.TestCase):
    def setUp(self):
        from tests.test_backfill_scraper_home_lines import OfflineHomeBackfillTests
        self.fixture = OfflineHomeBackfillTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def reseal_debts(self, mutate):
        summary_path = self.fixture.inventory / "home-voice-inventory-summary.json"
        summary = json.loads(summary_path.read_bytes())
        evidence = summary["files"]["home-voice-acquisition-debts"]
        path = self.fixture.inventory / "home-voice-acquisition-debts.jsonl"
        debts = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        mutate(debts[0])
        path.write_text("".join(json.dumps(debt, ensure_ascii=False, sort_keys=True) + "\n" for debt in debts), encoding="utf-8")
        evidence["sha256"] = recovery.sha(path.read_bytes())
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def test_resealed_numeric_composite_identity_cannot_alias_exact_sealed_integer_identity(self):
        def mutate(debt):
            identity = debt["composite_identity"]["id"]
            identity["value"] = float(identity["value"])
        self.reseal_debts(mutate)
        with self.assertRaisesRegex(ValueError, "exact sealed regional composite"):
            recovery.prepare_requests(self.fixture.inventory)

    def test_resealed_numeric_presence_flag_cannot_alias_boolean_raw_identity(self):
        def mutate(debt):
            debt["composite_identity"]["externalId"]["present"] = 1
        self.reseal_debts(mutate)
        with self.assertRaisesRegex(ValueError, "exact sealed regional composite"):
            recovery.prepare_requests(self.fixture.inventory)


if __name__ == "__main__":
    unittest.main()
