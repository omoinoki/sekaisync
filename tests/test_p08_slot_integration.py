"""Authoritative slot consumers and persistence; synthetic temporary stores only."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import dbstore, term_slots, termindex
from sekaisync.core import SekaiSyncCore
from sekaisync.layout import cache_dir
from sekaisync.termindex import TermRecord


class SlotIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        dbstore.initialize_new_store(self.root)
        self.record = TermRecord('event:1', '原名', 'ja', names={'ja': '原名', 'en': 'WrongName'},
                                 official=True, trust='A', source='master_db')
        self.evidence = term_slots.evidence_with_ids(self.record.id, [
            {'story_key': 's1', 'language': 'ja', 'term': '原名', 'sentence': '原名です',
             'source': 'master_db', 'position': 0, 'custom': {'keep': True}},
        ])
        self.decisions = [
            {'term_id': self.record.id, 'language': 'ja', 'value': '原名', 'status': 'accepted',
             'source': 'master_db', 'confidence': .9, 'evidence_refs': [self.evidence[0]['evidence_id']]},
            {'term_id': self.record.id, 'language': 'en', 'value': 'WrongName', 'status': 'pending',
             'source': 'llm', 'reason': 'legacy_unverified',
             'legacy_payload': {'value': 'WrongName', 'reported_trust': 'A'},
             'candidates': [{'value': 'WrongName', 'channel': 'old', 'position': 7}]},
        ]
        term_slots.commit_slot_decisions(self.root, self.decisions,
            records=[termindex.term_to_dict(self.record)], evidence_by_id={self.record.id: self.evidence},
            expected_revision=0, verifier=self.verify)

    @staticmethod
    def verify(slot, evidence):
        if slot['language'] != 'ja' or slot['value'] != '原名':
            return None
        return {'subject_id': slot['term_id'], 'language': 'ja', 'value': '原名',
                'source': 'master_db', 'official': True, 'trust': 'A',
                'verifier': 'synthetic-official-entity', 'evidence_refs': [evidence[0]['evidence_id']]}

    def revision(self):
        with dbstore.connect(self.root) as conn:
            return dbstore.current_revision(conn)

    def test_core_lookup_exposes_slots_without_promoting_pending(self):
        core = SekaiSyncCore(self.root)
        result = core.term_lookup('原名')[0]
        self.assertEqual(result['names'], {'ja': '原名'})
        self.assertEqual({s['language']: s['status'] for s in result['slots']},
                         {'en': 'pending', 'ja': 'accepted'})
        self.assertEqual(result['unverified_languages'], ['en'])
        self.assertEqual(result['evidence'][0]['custom'], {'keep': True})
        self.assertEqual(core.term_lookup('WrongName'), [])
        self.assertEqual(core.term_status()['slot_statuses'], {'accepted': 1, 'pending': 1})

    def test_all_pending_does_not_regain_source_trust(self):
        term_slots.commit_slot_decisions(self.root,
            [dict(self.decisions[0], status='pending')], expected_revision=self.revision())
        result = SekaiSyncCore(self.root).term_lookup('原名')[0]
        self.assertEqual(result['trust'], '')
        self.assertFalse(result['official'])
        self.assertEqual(result['names'], {})

    def test_read_projection_uses_slots_not_stale_names_json(self):
        with dbstore.connect(self.root) as conn:
            conn.execute("UPDATE terms SET names_json=?, trust='A', official=1", (json.dumps({'en': 'Poison'}),))
            conn.commit()
        core = SekaiSyncCore(self.root)
        self.assertEqual(core.term_lookup('Poison'), [])
        self.assertEqual(core.term_lookup('原名')[0]['names'], {'ja': '原名'})

    def test_persist_explicit_update_preserves_other_slots_and_full_evidence(self):
        result = termindex.persist_term_updates(self.root, [self.record],
            slot_updates={(self.record.id, 'en'): dict(self.decisions[1], value='Replacement')},
            expected_revision=self.revision())
        slots = {s['language']: s for s in term_slots.load_term_slots(self.root)}
        self.assertEqual(slots['ja']['status'], 'accepted')
        self.assertEqual(slots['en']['value'], 'Replacement')
        self.assertEqual(slots['en']['legacy_payload']['value'], 'WrongName')
        self.assertEqual(dbstore.evidence_for_ids(self.root, [self.record.id])[self.record.id], self.evidence)
        retry = termindex.persist_term_updates(self.root, [self.record],
            slot_updates={(self.record.id, 'en'): dict(self.decisions[1], value='Replacement')},
            expected_revision=result['revision'])
        self.assertEqual(retry['revision'], result['revision'])

    def test_serialization_roundtrip_keeps_slots_and_compact_proof(self):
        records = termindex.load_persisted_terms(self.root, include_sentences=True)
        path = Path(self.tmp.name) / 'export.json'
        termindex.save_terms(records, path, compact_evidence=True)
        loaded = termindex.load_terms(path)[0]
        self.assertEqual(loaded.slots, records[0].slots)
        self.assertEqual(loaded.evidence, self.evidence)
        self.assertEqual(loaded.name_for('en'), '')
        loaded.names['en'] = 'Bypass'
        self.assertEqual(termindex.lookup_terms([loaded], 'Bypass'), [])
        self.assertEqual(termindex.term_to_dict(loaded)['names'], {'ja': '原名'})

    def test_core_commit_wires_slot_api_and_reloads_next_request(self):
        core = SekaiSyncCore(self.root)
        old = core.term_lookup('原名')[0]
        core.commit_term_slots([dict(self.decisions[1], status='rejected')],
                               expected_revision=self.revision())
        new = core.term_lookup('原名')[0]
        self.assertEqual(old['slots'][0]['status'], 'pending')
        self.assertEqual(new['slots'][0]['status'], 'rejected')
        self.assertEqual(new['names'], {'ja': '原名'})

    def test_zhfirst_compatibility_queues_new_names_without_borrowed_A(self):
        path = cache_dir(self.root) / 'zhfirst_terms.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([{'canonical': '原名', 'names': {'en': 'WrongName', 'ko': '새이름'},
                                    'official': True}]), encoding='utf-8')
        core = SekaiSyncCore(self.root)
        core.merge_zhfirst_terms()
        result = core.term_lookup('原名')[0]
        self.assertEqual(result['names'], {'ja': '原名'})
        slots = {s['language']: s for s in result['slots']}
        self.assertEqual(slots['ko']['status'], 'pending')
        self.assertEqual(slots['ko']['trust'], '')
        self.assertEqual(slots['en']['legacy_payload']['value'], 'WrongName')
        self.assertEqual(result['evidence'], self.evidence)

    def test_snapshot_removal_leaves_no_reusable_ghost_queue(self):
        """A snapshot that drops a subject removes its evidence, slots and
        queued review item together: a later decision must not resurrect it."""
        from sekaisync.term_slots import ingest_record_snapshot

        with dbstore.connect(self.root) as conn:
            queued = conn.execute("SELECT COUNT(*) FROM review_queue WHERE term_id=?",
                                  (self.record.id,)).fetchone()[0]
            self.assertGreaterEqual(queued, 1)
        result = ingest_record_snapshot(self.root, [], expected_revision=self.revision())
        self.assertGreaterEqual(result['revision'], 0)
        with dbstore.connect(self.root) as conn:
            # ``terms`` is keyed by id; the dependent tables carry term_id.
            for table, column in (('terms', 'id'), ('term_evidence', 'term_id'),
                                  ('term_slots', 'term_id'), ('review_queue', 'term_id')):
                count = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {column}=?",
                                     (self.record.id,)).fetchone()[0]
                self.assertEqual(count, 0, table)
        self.assertEqual(termindex.lookup_terms(termindex.load_persisted_terms(self.root), '原名'), [])

    def test_snapshot_reingest_keeps_decisions_and_adds_nothing(self):
        """Re-running the same snapshot must not churn the reviewer's state."""
        from sekaisync.term_slots import ingest_record_snapshot

        # Loaded with sentences: a write-side pass must carry the full evidence
        # body it is about to re-state, not the light read projection.
        record = termindex.load_persisted_terms(self.root, include_sentences=True)[0]
        before = {s['language']: (s['status'], s['trust'], s['value'])
                  for s in term_slots.load_term_slots(self.root)}
        revision = self.revision()
        result = ingest_record_snapshot(self.root, [termindex.term_to_dict(record)],
                                        expected_revision=revision)
        after = {s['language']: (s['status'], s['trust'], s['value'])
                 for s in term_slots.load_term_slots(self.root)}
        self.assertEqual(after, before)
        self.assertEqual(result['revision'], revision, "a no-op snapshot bumped the revision")
        self.assertEqual(termindex.load_persisted_terms(self.root)[0].names, {'ja': '原名'})

    def test_v1_compatibility_remains_explicit_without_slot_migration(self):
        root = Path(self.tmp.name) / 'legacy'
        dbstore.initialize(root)
        dbstore.upsert_terms(root, [self.record])
        records = termindex.load_persisted_terms(root)
        self.assertIsNone(records[0].slots)
        self.assertEqual(records[0].name_for('ko'), '原名')
        self.assertEqual(termindex.term_to_dict(records[0])['names'], self.record.names)
        self.assertEqual(dbstore.inspect_schema(root).version, '1')


if __name__ == '__main__':
    unittest.main()
