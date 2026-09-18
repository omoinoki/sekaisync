"""Release 0.4.0 — the `sekaisync migrate` CLI command.

Migration itself is `dbstore.migrate_store` (tested exhaustively in
test_term_slot_migration.py and test_entity_region_db.py). What this file pins
is the command surface: two-step authorization (dry-run digest echoed back),
one-revision-per-step, and a non-upgrade refusal — the properties an operator
needs to trust when moving a real 2 GB store to v3.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sekaisync import dbstore


def _run(store: Path, *args: str):
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-m", "sekaisync", "--store", str(store), "migrate", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _last_json(text: str) -> dict:
    return json.loads(text[text.rfind("{"):])


class MigrateCommandTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_migrate_cmd_")
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "store"
        dbstore.initialize(self.store)  # v1

    def test_two_step_apply_moves_v1_to_v2(self):
        rc, out, _ = _run(self.store, "--to", "2", "--dry-run")
        self.assertEqual(rc, 0)
        digest = _last_json(out)["plan_digest"]

        backup = Path(self._tmp.name) / "v1.db"
        rc, out, _ = _run(self.store, "--to", "2", "--plan-digest", digest, "--backup", str(backup))
        self.assertEqual(rc, 0, out)
        self.assertEqual(dbstore.inspect_schema(self.store).version, "2")
        self.assertTrue(backup.exists())

    def test_multi_level_jump_is_refused(self):
        """One revision per authorized step: v1 cannot jump straight to v3."""
        rc, out, err = _run(self.store, "--to", "3")
        self.assertEqual(rc, 1)
        self.assertIn("v2 first", err)

    def test_apply_without_digest_or_backup_is_refused(self):
        rc, out, err = _run(self.store, "--to", "2")
        self.assertEqual(rc, 2)
        self.assertIn("--plan-digest", err)

    def test_non_upgrade_is_refused(self):
        """A digest from before the store changed must not authorize an apply.

        After v1->v2 the store is at 2, so asking for 2 again is not an
        upgrade regardless of what the (stale) digest says.
        """
        rc, out, _ = _run(self.store, "--to", "2", "--dry-run")
        digest = _last_json(out)["plan_digest"]
        backup = Path(self._tmp.name) / "v1.db"
        rc, out, _ = _run(self.store, "--to", "2", "--plan-digest", digest, "--backup", str(backup))
        self.assertEqual(rc, 0)
        rc, out, err = _run(self.store, "--to", "2", "--plan-digest", digest, "--backup",
                            str(Path(self._tmp.name) / "again.db"))
        self.assertEqual(rc, 1)
        self.assertIn("already at schema", err)

    def test_full_ladder_v1_to_v3(self):
        for target in (2, 3):
            rc, out, _ = _run(self.store, "--to", str(target), "--dry-run")
            digest = _last_json(out)["plan_digest"]
            rc, out, _ = _run(self.store, "--to", str(target),
                              "--plan-digest", digest,
                              "--backup", str(Path(self._tmp.name) / f"v{target - 1}.db"))
            self.assertEqual(rc, 0, out)
        self.assertEqual(dbstore.inspect_schema(self.store).version, "3")


if __name__ == "__main__":
    unittest.main()
