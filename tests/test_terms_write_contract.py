"""STAGED B2 tests — term write contract (Astra P01 / D01).

Held outside ``tests/`` until the implementation lands so the committed suite
stays green at every step (Astra: do not merge failing tests into a
releasable branch, and do not use skip/expectedFailure to fake green).

Move to ``tests/test_terms_write_contract.py`` together with the P01
implementation in B2.
"""

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sekaisync import dbstore
from sekaisync.termindex import TermRecord


class _TempStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_terms_write_")
        self.store = Path(self._tmp.name)
        dbstore.initialize(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def _read_revision(self):
        conn = sqlite3.connect(str(dbstore.db_path(self.store)))
        try:
            return dbstore.current_revision(conn)
        finally:
            conn.close()

    @contextlib.contextmanager
    def _conn(self):
        """sqlite3's context manager commits but does not close the handle;
        on Windows that keeps the .db locked against TemporaryDirectory."""
        conn = sqlite3.connect(str(dbstore.db_path(self.store)))
        try:
            yield conn
        finally:
            conn.close()

    def _record(self, term_id, canonical, sentences):
        return TermRecord(
            id=term_id,
            canonical=canonical,
            source_language="ja",
            names={"ja": canonical},
            evidence=[
                {
                    "story_key": f"s{i}",
                    "language": "ja",
                    "term": canonical,
                    "sentence": sentence,
                }
                for i, sentence in enumerate(sentences)
            ],
        )

    def _term_ids(self):
        with self._conn() as conn:
            return [r[0] for r in conn.execute("SELECT id FROM terms ORDER BY id")]

    def _evidence_count_stored(self, term_id):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT evidence_count FROM terms WHERE id=?", (term_id,)
            ).fetchone()
        return row[0] if row else None

    def _evidence_rows(self, term_id):
        with self._conn() as conn:
            return list(
                conn.execute(
                    "SELECT sentence FROM term_evidence WHERE term_id=? ORDER BY idx",
                    (term_id,),
                )
            )


class SnapshotReplaceSemanticsTest(_TempStore):
    """P01 — snapshot replace vs incremental upsert must be distinguishable."""

    def test_snapshot_replace_removes_absent_records(self):
        dbstore.save_terms_records(self.store, [self._record("t1", "A", ["x"])])
        self.assertEqual(self._term_ids(), ["t1"])

        dbstore.replace_terms_snapshot(
            self.store,
            [self._record("t2", "B", ["y"])],
            evidence_by_id={
                "t2": [{"story_key": "s0", "language": "ja", "term": "B", "sentence": "y"}]
            },
        )
        self.assertEqual(
            self._term_ids(),
            ["t2"],
            "snapshot replace left orphaned records behind (upsert-only)",
        )

    def test_snapshot_requires_evidence_for_every_id(self):
        """Completeness must be stated, not guessed from a non-empty list."""
        with self.assertRaises(ValueError):
            dbstore.replace_terms_snapshot(
                self.store,
                [self._record("t1", "A", ["x"])],
                evidence_by_id={},  # t1 missing
            )

    def test_snapshot_rejects_duplicate_ids(self):
        rec = self._record("t1", "A", ["x"])
        with self.assertRaises(ValueError):
            dbstore.replace_terms_snapshot(
                self.store,
                [rec, rec],
                evidence_by_id={"t1": []},
            )

    def test_snapshot_empty_evidence_is_allowed_and_means_zero(self):
        """An explicit empty sequence is a statement, not an omission."""
        result = dbstore.replace_terms_snapshot(
            self.store,
            [self._record("t1", "A", [])],
            evidence_by_id={"t1": []},
        )
        self.assertEqual(result.evidence_rows, 0)
        self.assertEqual(self._evidence_count_stored("t1"), 0)

    def test_stale_expected_revision_is_refused(self):
        """A write derived from an old revision must not clobber a newer one."""
        dbstore.upsert_terms(self.store, [self._record("t1", "A", ["x"])])
        stale = self._read_revision()
        # Another writer commits.
        dbstore.upsert_terms(self.store, [self._record("t2", "B", ["y"])])
        with self.assertRaises(dbstore.RevisionConflictError):
            dbstore.upsert_terms(
                self.store,
                [self._record("t3", "C", ["z"])],
                expected_revision=stale,
            )

    def test_append_deduplicates_without_growing_index(self):
        """Appending the same evidence twice must not create two rows."""
        ev = {"story_key": "s1", "language": "ja", "term": "A", "sentence": "x"}
        dbstore.upsert_terms(
            self.store,
            [self._record("t1", "A", [])],
            evidence_updates={"t1": {"mode": "replace", "items": [ev]}},
        )
        dbstore.upsert_terms(
            self.store,
            [self._record("t1", "A", [])],
            evidence_updates={"t1": {"mode": "append", "items": [ev]}},
        )
        self.assertEqual(self._evidence_count_stored("t1"), 1)
        self.assertEqual(len(self._evidence_rows("t1")), 1)

    def test_evidence_count_matches_stored_rows(self):
        """The advertised count must equal the rows, not the input length."""
        result = dbstore.upsert_terms(
            self.store,
            [self._record("t1", "A", [])],
            evidence_updates={
                "t1": {
                    "mode": "replace",
                    "items": [
                        {"story_key": "s1", "language": "ja", "term": "A", "sentence": "x"},
                        {"story_key": "s2", "language": "ja", "term": "A", "sentence": "y"},
                    ],
                }
            },
        )
        self.assertEqual(result.evidence_rows, 2)
        self.assertEqual(self._evidence_count_stored("t1"), 2)
        self.assertEqual(len(self._evidence_rows("t1")), 2)

    def test_upsert_terms_preserves_absent_records(self):
        dbstore.save_terms_records(self.store, [self._record("t1", "A", ["x"])])
        dbstore.upsert_terms(self.store, [self._record("t2", "B", ["y"])])
        self.assertEqual(self._term_ids(), ["t1", "t2"])

    def test_empty_evidence_replace_is_honest(self):
        """Replacing evidence with [] must store exactly 0 and report 0."""
        dbstore.save_terms_records(self.store, [self._record("t1", "A", ["x"])])
        self.assertEqual(self._evidence_count_stored("t1"), 1)

        dbstore.upsert_terms(
            self.store,
            [self._record("t1", "A", [])],
            evidence_updates={"t1": {"mode": "replace", "items": []}},
        )
        self.assertEqual(
            self._evidence_count_stored("t1"),
            0,
            "advertised evidence count disagrees with stored rows after "
            "an explicit empty replace",
        )
        self.assertEqual(self._evidence_rows("t1"), [])

    def test_evidence_preserved_when_no_update_given(self):
        """No evidence update for an id means preserve, not wipe."""
        dbstore.save_terms_records(self.store, [self._record("t1", "A", ["x", "y"])])
        dbstore.upsert_terms(self.store, [self._record("t1", "A", [])])
        self.assertEqual(self._evidence_count_stored("t1"), 2)
        self.assertEqual(len(self._evidence_rows("t1")), 2)


if __name__ == "__main__":
    unittest.main()
