"""Write boundaries must preserve a caller-owned read transaction."""
import tempfile
import unittest
from pathlib import Path

from sekaisync import dbstore, term_slots


class ReadBindingWriteBoundaryTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'store'
        self.backup = Path(temporary.name) / 'backup.db'
        dbstore.initialize(self.root)

    def test_migration_rejects_binding_before_any_side_effect(self):
        with dbstore.connect(self.root) as conn:
            conn.execute('BEGIN')
            revision = dbstore.current_revision(conn)
            with dbstore.read_connection(self.root, conn):
                for dry_run in (True, False):
                    with self.subTest(dry_run=dry_run):
                        with self.assertRaisesRegex(RuntimeError, 'read.*bound|bound.*read'):
                            term_slots.migrate_store(self.root, dry_run=dry_run,
                                                     backup_path=self.backup)
                        self.assertTrue(conn.in_transaction)
                        self.assertEqual(dbstore.current_revision(conn), revision)
            self.assertFalse(self.backup.exists())

    def test_legacy_writers_reject_binding_without_committing(self):
        with dbstore.connect(self.root) as conn:
            conn.execute('BEGIN')
            revision = dbstore.current_revision(conn)
            with dbstore.read_connection(self.root, conn):
                for writer in (dbstore.save_terms_records, dbstore.upsert_terms,
                               dbstore.replace_terms_snapshot):
                    with self.subTest(writer=writer.__name__):
                        with self.assertRaisesRegex(RuntimeError, 'read.*bound|bound.*read'):
                            kwargs = {'evidence_by_id': {}} if writer is dbstore.replace_terms_snapshot else {}
                            writer(self.root, [], **kwargs)
                        self.assertTrue(conn.in_transaction)
                        self.assertEqual(dbstore.current_revision(conn), revision)

    def test_v2_legacy_save_is_rejected(self):
        term_slots.migrate_store(self.root, dry_run=False, backup_path=self.backup)
        with self.assertRaisesRegex(ValueError, 'slots'):
            dbstore.save_terms_records(self.root, [])

    def test_slot_writers_reject_bound_connection(self):
        term_slots.migrate_store(self.root, dry_run=False, backup_path=self.backup)
        with dbstore.connect(self.root) as conn:
            conn.execute('BEGIN')
            revision = dbstore.current_revision(conn)
            with dbstore.read_connection(self.root, conn):
                for target, writer in ((self.root, term_slots.commit_slot_decisions),
                                       (conn, term_slots.commit_slot_decisions_conn)):
                    with self.subTest(writer=writer.__name__):
                        with self.assertRaisesRegex(RuntimeError, 'read.*bound|bound.*read'):
                            writer(target, [], expected_revision=revision)
                        self.assertTrue(conn.in_transaction)
                        self.assertEqual(dbstore.current_revision(conn), revision)

    def test_binding_does_not_block_other_store_writes(self):
        other = self.root.parent / 'other'
        dbstore.initialize(other)
        with dbstore.connect(self.root) as conn:
            conn.execute('BEGIN')
            with dbstore.read_connection(self.root, conn):
                self.assertEqual(dbstore.save_terms_records(other, []), 0)
                self.assertTrue(conn.in_transaction)


if __name__ == '__main__':
    unittest.main()
