"""P03/P07 region persistence: temporary stores only, no inferred provenance."""
import contextlib
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import dbstore
from sekaisync.models import Entity, RegionFacts
from sekaisync.regions import entity_for_region


def event(entity_id='event:1'):
    return Entity(entity_id, 'event', '', ['en', 'jp'], {'ja': '日本', 'en': 'Event'},
                  {'shared': False}, 'master_db', None, False, 'A', {
                      'jp': RegionFacts('jp', {'startAt': 100, 'null': None, 'empty': '',
                                              'nested': [True, 0, {'名前': '値'}]},
                                        'master_db:jp', 'sha-jp',
                                        {'table': 'events', 'field_sources': {'name': {'sha256': 'a'}}}),
                      'en': RegionFacts('en', {'startAt': 200}, 'master_db:en', None, {}),
                  })


class RegionDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='region-db-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        dbstore.initialize_new_store(self.root, target_version=3)

    def test_roundtrip_has_exact_facts_and_provenance(self):
        original = event()
        self.assertEqual(dbstore.save_entities(self.root, iter([original])), 1)
        restored = dbstore.load_entities(self.root)[0]
        self.assertEqual(asdict(restored), asdict(original))
        self.assertEqual(entity_for_region(restored, 'jp')['facts']['startAt'], 100)
        self.assertEqual(entity_for_region(restored, 'en')['facts']['startAt'], 200)
        self.assertEqual(entity_for_region(restored, 'cn')['coverage'], 'missing')
        with dbstore.connect(self.root) as conn:
            self.assertEqual(conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM entity_region_facts').fetchone()[0], 2)

    def test_replacement_removes_regions_entities_and_empty_snapshot(self):
        dbstore.save_entities(self.root, [event(), event('event:2')])
        replacement = event()
        del replacement.region_facts['en']
        dbstore.save_entities(self.root, [replacement])
        self.assertEqual([asdict(e) for e in dbstore.load_entities(self.root)], [asdict(replacement)])
        dbstore.save_entities(self.root, [])
        self.assertEqual(dbstore.load_entities(self.root), [])
        with dbstore.connect(self.root) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM entity_region_facts').fetchone()[0], 0)

    def test_unscoped_legacy_facts_never_gain_region_coverage(self):
        legacy = event()
        legacy.region_facts = {}
        legacy.facts = {'startAt': 100, 'empty': '', 'zero': 0, 'null': None}
        dbstore.save_entities(self.root, [legacy])
        restored = dbstore.load_entities(self.root)[0]
        self.assertEqual(asdict(restored), asdict(legacy))
        for region in ['jp', 'en']:
            self.assertEqual(entity_for_region(restored, region)['coverage'], 'unknown')
            self.assertEqual(entity_for_region(restored, region)['legacy_unscoped']['facts'], legacy.facts)

    def test_failed_insert_rolls_back_even_when_outer_caller_catches_error(self):
        original = event()
        dbstore.save_entities(self.root, [original])
        with dbstore.connect(self.root) as conn:
            conn.execute("CREATE TRIGGER reject_region BEFORE INSERT ON entity_region_facts "
                         "WHEN NEW.region='en' BEGIN SELECT RAISE(ABORT, 'injected'); END")
            conn.commit()
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.IntegrityError):
                dbstore.save_entities(self.root, [event('event:2')], conn=conn)
            conn.commit()
        self.assertEqual([asdict(e) for e in dbstore.load_entities(self.root)], [asdict(original)])

    def test_external_transaction_is_not_committed_and_revision_is_owned_by_caller(self):
        with dbstore.connect(self.root) as conn:
            conn.execute('BEGIN IMMEDIATE')
            revision = dbstore.current_revision(conn)
            dbstore.save_entities(self.root, [event()], conn=conn)
            self.assertTrue(conn.in_transaction)
            self.assertEqual(dbstore.current_revision(conn), revision)
            with contextlib.closing(sqlite3.connect(dbstore.db_file(self.root))) as other:
                self.assertEqual(other.execute('SELECT COUNT(*) FROM entities').fetchone()[0], 0)
            conn.rollback()
        self.assertEqual(dbstore.load_entities(self.root), [])

    def test_owned_write_advances_revision_and_bound_read_keeps_old_facts(self):
        dbstore.save_entities(self.root, [event()])
        with dbstore.connect(self.root) as snapshot:
            snapshot.execute('BEGIN')
            revision = dbstore.current_revision(snapshot)
            replacement = event()
            replacement.region_facts['en'].facts['startAt'] = 300
            dbstore.save_entities(self.root, [replacement])
            with dbstore.read_connection(self.root, snapshot):
                self.assertEqual(dbstore.load_entities(self.root)[0].region_facts['en'].facts['startAt'], 200)
                with self.assertRaises(RuntimeError):
                    dbstore.save_entities(self.root, [], conn=snapshot)
                with self.assertRaises(RuntimeError):
                    dbstore.save_entities(self.root, [])
            snapshot.rollback()
        self.assertEqual(dbstore.load_entities(self.root)[0].region_facts['en'].facts['startAt'], 300)
        with dbstore.connect(self.root) as conn:
            self.assertEqual(dbstore.current_revision(conn), revision + 1)

    def test_region_identity_mismatch_and_duplicate_entities_are_rejected(self):
        original = event()
        dbstore.save_entities(self.root, [original])
        wrong = event()
        wrong.region_facts['cn'] = wrong.region_facts['jp']
        for entities in ([wrong], [event(), event()]):
            with self.assertRaises(ValueError):
                dbstore.save_entities(self.root, entities)
            self.assertEqual(asdict(dbstore.load_entities(self.root)[0]), asdict(original))

    def test_slot_tables_remain_present_and_legacy_terms_writer_is_blocked(self):
        with dbstore.connect(self.root) as conn:
            for table in ('term_slots', 'review_queue', 'review_decisions', 'review_rules'):
                self.assertEqual(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, 'slots'):
            dbstore.upsert_terms(self.root, [])


class RegionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='region-migration-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        dbstore.initialize_new_store(self.root)
        legacy = event()
        legacy.region_facts = {}
        legacy.facts = {'startAt': 100, 'null': None, 'nested': [0, False]}
        dbstore.save_entities(self.root, [legacy])
        self.legacy = legacy
        self.backup = self.root / 'before-v3.sqlite'

    def test_explicit_backup_migration_preserves_legacy_and_slots(self):
        # Slot/review/evidence payloads must not be regenerated by region migration.
        with dbstore.connect(self.root) as conn:
            conn.execute("INSERT INTO terms(id,canonical,source_language) VALUES('t','term','ja')")
            conn.execute("INSERT INTO term_slots(term_id,language,value,status,decision_revision) "
                         "VALUES('t','en','Legacy','pending',7)")
            conn.commit()
            before = list(conn.iterdump())
            revision = dbstore.current_revision(conn)
        report = dbstore.migrate_store(self.root, target_version=3)
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['legacy_unscoped_entities'], 1)
        self.assertEqual(report['region_facts'], 0)
        self.assertEqual(dbstore.inspect_schema(self.root).version, '2')
        with dbstore.connect(self.root) as conn:
            self.assertEqual(list(conn.iterdump()), before)
        applied = dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                                       backup_path=self.backup, expected_plan_digest=report['plan_digest'])
        self.assertEqual(applied['revision'], revision + 1)
        self.assertEqual(dbstore.inspect_schema(self.root).version, '3')
        self.assertEqual(asdict(dbstore.load_entities(self.root)[0]), asdict(self.legacy))
        with dbstore.connect(self.root) as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM entity_region_facts').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT value,status,decision_revision FROM term_slots').fetchone(),
                             ('Legacy', 'pending', 7))
        with contextlib.closing(sqlite3.connect(self.backup)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0], '2')
            self.assertEqual(list(conn.iterdump()), before)
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')
        self.assertTrue(dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                                             backup_path=self.backup)['already_current'])

    def test_apply_requires_new_backup_and_matching_plan(self):
        report = dbstore.migrate_store(self.root, target_version=3)
        with self.assertRaises(ValueError):
            dbstore.migrate_store(self.root, target_version=3, dry_run=False)
        self.backup.write_bytes(b'keep backup')
        with self.assertRaises(FileExistsError):
            dbstore.migrate_store(self.root, target_version=3, dry_run=False, backup_path=self.backup)
        self.assertEqual(self.backup.read_bytes(), b'keep backup')
        dbstore.save_entities(self.root, [])
        new_backup = self.root / 'new.sqlite'
        with self.assertRaises(dbstore.RevisionConflictError):
            dbstore.migrate_store(self.root, target_version=3, dry_run=False, backup_path=new_backup,
                                  expected_plan_digest=report['plan_digest'])
        self.assertFalse(new_backup.exists())
        self.assertEqual(dbstore.inspect_schema(self.root).version, '2')

    def test_plan_detects_changes_without_revision_and_backup_includes_live_wal(self):
        with dbstore.connect(self.root) as live:
            live.execute('PRAGMA wal_autocheckpoint=0')
            report = dbstore.migrate_store(self.root, target_version=3)
            live.execute("UPDATE entities SET facts_json=?", (json.dumps({'startAt': 777}),))
            live.commit()
            with self.assertRaises(dbstore.RevisionConflictError):
                dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                                      backup_path=self.backup,
                                      expected_plan_digest=report['plan_digest'])
            self.assertFalse(self.backup.exists())
            dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                                  backup_path=self.backup)
            with contextlib.closing(sqlite3.connect(self.backup)) as backup:
                self.assertEqual(json.loads(backup.execute('SELECT facts_json FROM entities').fetchone()[0]),
                                 {'startAt': 777})

    def test_failed_migration_rolls_back_schema_stamp_and_revision(self):
        with dbstore.connect(self.root) as conn:
            before = list(conn.iterdump())
        with patch.object(dbstore, 'bump_revision', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                dbstore.migrate_store(self.root, target_version=3, dry_run=False, backup_path=self.backup)
        with dbstore.connect(self.root) as conn:
            self.assertEqual(list(conn.iterdump()), before)
        self.assertTrue(self.backup.exists())

    def test_unknown_schema_and_old_schema_scoped_writes_are_not_modified(self):
        with self.assertRaisesRegex(ValueError, 'migrat'):
            dbstore.save_entities(self.root, [event()])
        for version in ('99', 'future', '3.0', '0'):
            with dbstore.connect(self.root) as conn:
                dbstore._meta_set(conn, 'schema_version', version)
                conn.commit()
                before = list(conn.iterdump())
            with self.subTest(version=version):
                for operation in (lambda: dbstore.load_entities(self.root),
                                  lambda: dbstore.save_entities(self.root, []),
                                  lambda: dbstore.migrate_store(self.root, target_version=3),
                                  lambda: dbstore.initialize_new_store(self.root, target_version=3)):
                    with self.assertRaises(dbstore.SchemaVersionError):
                        operation()
                with dbstore.connect(self.root) as conn:
                    self.assertEqual(list(conn.iterdump()), before)

    def test_v1_requires_separate_slot_authorization_and_rejects_scoped_write(self):
        root = self.root / 'v1'
        dbstore.initialize(root)
        with self.assertRaisesRegex(ValueError, 'v2'):
            dbstore.migrate_store(root, target_version=3)
        with self.assertRaisesRegex(ValueError, 'migrat'):
            dbstore.save_entities(root, [event()])
        self.assertEqual(dbstore.inspect_schema(root).version, '1')

    def test_bound_migration_is_rejected_without_ending_read_transaction(self):
        with dbstore.connect(self.root) as conn:
            conn.execute('BEGIN')
            with dbstore.read_connection(self.root, conn):
                with self.assertRaises(RuntimeError):
                    dbstore.migrate_store(self.root, target_version=3)
                self.assertTrue(conn.in_transaction)


if __name__ == '__main__':
    unittest.main()
