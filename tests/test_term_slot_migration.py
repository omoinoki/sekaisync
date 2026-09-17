"""P07/P08/P11 storage contracts; exclusively synthetic temporary stores."""
import contextlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import dbstore
from sekaisync.termindex import TermRecord


class SlotMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        dbstore.initialize(self.root)
        self.record = TermRecord(id='event:1', canonical='原名', source_language='ja',
                                 names={'ja': '原名', 'en': 'WrongName'},
                                 official=True, source='master_db', trust='A', confidence=0.98)
        dbstore.upsert_terms(self.root, [self.record], evidence_updates={
            self.record.id: dbstore.EvidenceUpdate('replace', [
                {'story_key': 'story:1', 'language': 'ja', 'sentence': '原名',
                 'term': '原名', 'source': 'master_db', 'position': 0, 'custom': {'keep': 1}}
            ])})
        self.backup = Path(self.tmp.name) / 'before.db'

    def slots(self):
        from sekaisync import term_slots
        return term_slots.load_term_slots(self.root)

    def test_dry_run_preserves_v1_and_reports_projection_changes(self):
        before = dbstore.db_file(self.root).read_bytes()
        report = dbstore.migrate_store(self.root, target_version=2)
        self.assertTrue(report['dry_run'])
        self.assertEqual(report['pending_slots'], 2)
        self.assertEqual(report['changed_terms'], 1)
        self.assertEqual(report['slots'][1]['legacy_payload']['reported_trust'], 'A')
        self.assertEqual(dbstore.inspect_schema(self.root).version, '1')
        self.assertEqual(dbstore.db_file(self.root).read_bytes(), before)
        self.assertFalse(self.backup.exists())
        dbstore.ensure_store(self.root)
        self.assertEqual(dbstore.load_terms_records(self.root)[0].names['en'], 'WrongName')

    def test_explicit_apply_preserves_unverified_values_and_backup_restore(self):
        report = dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                                       backup_path=self.backup)
        self.assertEqual(report['pending_slots'], 2)
        slots = self.slots()
        self.assertEqual({s['value'] for s in slots}, {'原名', 'WrongName'})
        self.assertTrue(all(s['status'] == 'pending' and s['trust'] == '' for s in slots))
        self.assertTrue(all(s['reason'] == 'legacy_unverified' for s in slots))
        self.assertTrue(all(s['legacy_payload']['reported_trust'] == 'A' for s in slots))
        self.assertEqual(dbstore.load_terms_records(self.root)[0].names, {})
        old = self.backup.read_bytes()
        again = dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                                      backup_path=self.backup)
        self.assertTrue(again['already_current'])
        self.assertEqual(old, self.backup.read_bytes())
        restored = Path(self.tmp.name) / 'restored'
        dbstore.db_file(restored).parent.mkdir(parents=True)
        with contextlib.closing(sqlite3.connect(self.backup)) as source:
            with contextlib.closing(sqlite3.connect(dbstore.db_file(restored))) as dest:
                source.backup(dest)
        self.assertEqual(dbstore.inspect_schema(restored).version, '1')
        self.assertEqual(dbstore.load_terms_records(restored)[0].names, self.record.names)
        with contextlib.closing(sqlite3.connect(self.backup)) as conn:
            self.assertEqual(conn.execute('PRAGMA quick_check').fetchone()[0], 'ok')

    def test_boolean_verifier_cannot_certify_official(self):
        report = dbstore.migrate_store(self.root, target_version=2, verifier=lambda *args: True)
        self.assertEqual(report['accepted_slots'], 0)

    def test_explicit_matching_verifier_evidence_accepts_only_one_slot(self):
        def verify(slot, evidence):
            if slot['language'] != 'ja':
                return None
            return {'subject_id': slot['term_id'], 'language': 'ja', 'value': '原名',
                    'source': 'master_db', 'official': True, 'trust': 'A',
                    'verifier': 'test-official-entity-check',
                    'evidence_refs': [evidence[0]['evidence_id']]}
        dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                              backup_path=self.backup, verifier=verify)
        by_lang = {s['language']: s for s in self.slots()}
        self.assertEqual(by_lang['ja']['status'], 'accepted')
        self.assertEqual(by_lang['ja']['trust'], 'A')
        self.assertEqual(by_lang['en']['status'], 'pending')
        self.assertEqual(dbstore.load_terms_records(self.root)[0].names, {'ja': '原名'})
        # Reordering the UI-only idx cannot break evidence identities.
        refs = by_lang['ja']['evidence_refs']
        with dbstore.connect(self.root) as conn:
            conn.execute('UPDATE term_evidence SET idx=idx+100')
            conn.commit()
        evidence = dbstore.evidence_for_ids(self.root, ['event:1'])['event:1']
        self.assertEqual(refs, [evidence[0]['evidence_id']])
        self.assertEqual(evidence[0]['custom'], {'keep': 1})

    def test_migration_failure_rolls_back_schema_data_and_revision(self):
        from sekaisync import term_slots
        with patch.object(term_slots, '_write_slot', side_effect=RuntimeError('injected')):
            with self.assertRaisesRegex(RuntimeError, 'injected'):
                dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                                      backup_path=self.backup)
        self.assertEqual(dbstore.inspect_schema(self.root).version, '1')
        self.assertEqual(dbstore.load_terms_records(self.root)[0].names, self.record.names)
        with dbstore.connect(self.root) as conn:
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='term_slots'").fetchone())
            self.assertEqual(dbstore.current_revision(conn), 1)
        self.assertTrue(self.backup.exists())

    def test_apply_requires_backup_and_v3_is_reserved(self):
        with self.assertRaises(ValueError):
            dbstore.migrate_store(self.root, target_version=2, dry_run=False)
        with self.assertRaises(ValueError):
            dbstore.migrate_store(self.root, target_version=3)
        self.assertEqual(dbstore.inspect_schema(self.root).version, '1')

    def test_v2_blocks_legacy_write_bypass_without_changing_data(self):
        dbstore.migrate_store(self.root, target_version=2, dry_run=False, backup_path=self.backup)
        with self.assertRaisesRegex(ValueError, 'slot'):
            dbstore.upsert_terms(self.root, [self.record])
        with self.assertRaisesRegex(ValueError, 'slot'):
            dbstore.replace_terms_snapshot(self.root, [], evidence_by_id={})
        self.assertEqual(dbstore.load_terms_records(self.root)[0].names, {})

    def test_new_v2_store_has_reserved_review_tables_not_region_facts(self):
        new = Path(self.tmp.name) / 'new'
        dbstore.initialize_new_store(new)
        self.assertEqual(dbstore.inspect_schema(new).version, '2')
        with dbstore.connect(new) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'term_slots', 'review_queue', 'review_decisions', 'review_rules'} <= tables)
        self.assertNotIn('entity_region_facts', tables)


class SlotTransactionTests(SlotMigrationTests):
    def test_slots_evidence_queue_atomic_and_revision_checked(self):
        from sekaisync import term_slots
        dbstore.migrate_store(self.root, target_version=2, dry_run=False, backup_path=self.backup)
        decision = {'term_id': 'event:1', 'language': 'zh', 'value': '名称',
                    'status': 'pending', 'source': 'community', 'reason': 'insufficient_evidence',
                    'candidates': [{'value': '名称'}], 'evidence_refs': []}
        with dbstore.connect(self.root) as conn:
            revision = dbstore.current_revision(conn)
        with patch.object(term_slots, '_write_queue', side_effect=RuntimeError('queue failure')):
            with self.assertRaisesRegex(RuntimeError, 'queue failure'):
                term_slots.commit_slot_decisions(self.root, [decision], expected_revision=revision,
                    evidence_by_id={'event:1': [{'language': 'zh', 'story_key': 's2', 'sentence': '名称'}]})
        self.assertEqual(len(self.slots()), 2)
        self.assertEqual(len(dbstore.evidence_for_ids(self.root, ['event:1'])['event:1']), 1)
        result = term_slots.commit_slot_decisions(self.root, [decision], expected_revision=revision)
        self.assertEqual(result['pending_slots'], 1)
        with dbstore.connect(self.root) as conn:
            row = conn.execute("SELECT status, language FROM review_queue WHERE language='zh'").fetchone()
            self.assertEqual(row, ('queued', 'zh'))
        retry = term_slots.commit_slot_decisions(self.root, [decision], expected_revision=result['revision'])
        self.assertEqual(retry['revision'], result['revision'])
        with self.assertRaises(dbstore.RevisionConflictError):
            term_slots.commit_slot_decisions(self.root, [decision], expected_revision=revision)

    def test_unverified_accepted_decision_cannot_become_active_A(self):
        from sekaisync import term_slots
        dbstore.migrate_store(self.root, target_version=2, dry_run=False, backup_path=self.backup)
        with dbstore.connect(self.root) as conn:
            revision = dbstore.current_revision(conn)
        result = term_slots.commit_slot_decisions(self.root, [
            {'term_id': 'event:1', 'language': 'en', 'value': 'MoreWrong',
             'status': 'accepted', 'official': True, 'trust': 'A', 'source': 'llm'}
        ], expected_revision=revision)
        self.assertEqual(result['accepted_slots'], 0)
        self.assertEqual(dbstore.load_terms_records(self.root)[0].names, {})
        self.assertEqual(next(s for s in self.slots() if s['language'] == 'en')['trust'], '')


if __name__ == '__main__':
    unittest.main()
