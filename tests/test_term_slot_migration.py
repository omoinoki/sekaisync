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


class CorpusVerifierTests(unittest.TestCase):
    """W1-B: certify a slot from the corpus rows the channels actually emit.

    Every accepted decision used to land pending/insufficient_evidence while
    the command still reported success, because no caller passed a verifier and
    ``_certificate`` returns None before it reads anything (``term_slots.py``,
    the ``verifier is None`` guard).  ``index_verifier`` closes only the L0
    case — a name this store's own official glossary already carries — so
    trunk/hub/translit output had no path to acceptance at all.

    These tests drive the *real* end-to-end path: temporary store, explicit
    v1→v2→v3 migration, rows produced by ``trinity``'s own converter, and the
    store's own commit API.  The negative cases are the point of the file: the
    floor that makes a corpus proof corroboration must not be relaxed to make
    the positive case pass.
    """

    #: Rows in the shape ``trinity.channel_evidence_rows`` emits.
    SOURCE = 'corpus'

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        dbstore.initialize(self.root)
        dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                              backup_path=Path(self.tmp.name) / 'v2.db')
        dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                              backup_path=Path(self.tmp.name) / 'v3.db')

    def _revision(self):
        with dbstore.connect(self.root) as conn:
            return dbstore.current_revision(conn)

    @staticmethod
    def _rows(value, story_keys, *, language='en', source='corpus', term='セカイ'):
        return [{'story_key': key, 'language': language, 'source': source,
                 'term': term, 'sentence': f'Go to {value}',
                 'channel': 'trunk', 'channel_method': '', 'channel_value': value}
                for key in story_keys]

    def _commit(self, value, story_keys, *, verifier=..., source=None, rows=None):
        from sekaisync import term_slots
        source = self.SOURCE if source is None else source
        rows = self._rows(value, story_keys, source=source) if rows is None else rows
        evidence = term_slots.evidence_with_ids('term:ja:セカイ', rows)
        decision = {'term_id': 'term:ja:セカイ', 'language': 'en', 'value': value,
                    'status': 'accepted', 'source': source,
                    'evidence_refs': [row['evidence_id'] for row in evidence]}
        verify = term_slots.corpus_verifier() if verifier is ... else verifier
        result = term_slots.commit_slot_decisions(
            self.root, [decision],
            records=[{'id': 'term:ja:セカイ', 'canonical': 'セカイ', 'source_language': 'ja'}],
            evidence_by_id={'term:ja:セカイ': evidence},
            expected_revision=self._revision(), verifier=verify)
        slot = next(s for s in term_slots.load_term_slots(self.root) if s['language'] == 'en')
        return result, slot

    def test_corpus_rows_certify_a_non_official_slot(self):
        """The core change: sufficient corpus evidence now really accepts.

        Before this, the same call stored ``pending``/``insufficient_evidence``
        with no error, which is what made the whole extraction pass a no-op
        that looked like a success.
        """
        result, slot = self._commit('SEKAI', ['s1', 's2', 's3'])
        self.assertEqual(result['accepted_slots'], 1)
        self.assertEqual(slot['status'], 'accepted')
        self.assertEqual(slot['reason'], 'verified_evidence')
        self.assertEqual(slot['trust'], 'C', 'derived corpus evidence is C, not A or B')
        self.assertFalse(slot['official'])
        self.assertEqual(slot['verification']['verifier'], 'corpus:stories')
        self.assertEqual(slot['verification']['source'], self.SOURCE)
        # The certificate cites the rows it checked, and those refs resolve.
        self.assertTrue(slot['evidence_refs'])
        stored = dbstore.evidence_for_ids(self.root, ['term:ja:セカイ'])['term:ja:セカイ']
        by_id = {row['evidence_id']: row for row in stored}
        self.assertEqual(set(slot['evidence_refs']) <= set(by_id), True)
        self.assertEqual(len({by_id[ref]['story_key'] for ref in slot['evidence_refs']}), 3)

    def test_accepted_slot_projects_into_the_term_record(self):
        """An accepted slot is what the read projection shows, not the claim."""
        from sekaisync import term_slots
        self._commit('SEKAI', ['s1', 's2'])
        record = dbstore.load_terms_records(self.root)[0]
        self.assertEqual(record.names, {'en': 'SEKAI'})
        self.assertEqual(term_slots.load_term_slots(self.root)[0]['status'], 'accepted')

    def test_a_single_story_is_not_corroboration(self):
        """One story can repeat the same mistranslation; it is not evidence."""
        from sekaisync import term_slots
        result, slot = self._commit('SEKAI', ['s1'])
        self.assertEqual(result['accepted_slots'], 0)
        self.assertEqual(slot['status'], 'pending')
        self.assertEqual(slot['reason'], 'insufficient_evidence')
        self.assertEqual(slot['trust'], '')
        # Refused by the verifier itself, not merely by a later check: the
        # floor lives in one place, so a caller cannot satisfy it another way.
        verifier = term_slots.corpus_verifier()
        row = term_slots.evidence_with_ids('t', self._rows('SEKAI', ['s1']))[0]
        self.assertIsNone(verifier({'term_id': 't', 'language': 'en', 'value': 'SEKAI',
                                    'source': self.SOURCE}, [row]))

    def test_no_evidence_at_all_stays_pending(self):
        """The empty case must stay pending — acceptance is never the default."""
        from sekaisync import term_slots
        decision = {'term_id': 'term:ja:セカイ', 'language': 'en', 'value': 'SEKAI',
                    'status': 'accepted', 'source': self.SOURCE, 'evidence_refs': []}
        result = term_slots.commit_slot_decisions(
            self.root, [decision],
            records=[{'id': 'term:ja:セカイ', 'canonical': 'セカイ'}],
            expected_revision=self._revision(), verifier=term_slots.corpus_verifier())
        self.assertEqual(result['accepted_slots'], 0)
        self.assertEqual(result['pending_slots'], 1)
        slot = next(s for s in term_slots.load_term_slots(self.root) if s['language'] == 'en')
        self.assertEqual(slot['reason'], 'insufficient_evidence')

    def test_verifier_none_keeps_the_old_pending_behaviour(self):
        """No verifier means no certificate: the W1-B fix must be explicit.

        This is the red half of the pair above — with the callback removed the
        very same rows stop being sufficient, which proves the acceptance comes
        from the verifier and not from a loosened gate.
        """
        result, slot = self._commit('SEKAI', ['s1', 's2', 's3'], verifier=None)
        self.assertEqual(result['accepted_slots'], 0)
        self.assertEqual(slot['status'], 'pending')
        self.assertEqual(slot['reason'], 'insufficient_evidence')

    def test_rows_from_other_stories_of_the_same_value_are_counted_once(self):
        """Distinct stories, not distinct rows: two rows in one story is one story."""
        from sekaisync import term_slots
        rows = self._rows('SEKAI', ['s1', 's1'])
        evidence = term_slots.evidence_with_ids('term:ja:セカイ', rows)
        verifier = term_slots.corpus_verifier()
        slot = {'term_id': 'term:ja:セカイ', 'language': 'en', 'value': 'SEKAI',
                'source': self.SOURCE}
        self.assertIsNone(verifier(slot, evidence))

    def test_rows_that_do_not_attest_the_value_are_not_cited(self):
        """A row whose sentence lacks the value cannot support it."""
        from sekaisync import term_slots
        rows = self._rows('SEKAI', ['s1', 's2'])
        for row in rows:
            row['sentence'] = 'unrelated line'
            row['term'] = 'セカイ'
        evidence = term_slots.evidence_with_ids('term:ja:セカイ', rows)
        verifier = term_slots.corpus_verifier()
        slot = {'term_id': 'term:ja:セカイ', 'language': 'en', 'value': 'SEKAI',
                'source': self.SOURCE}
        self.assertIsNone(verifier(slot, evidence))
        result, stored = self._commit('SEKAI', ['s1', 's2'], verifier=verifier, rows=rows)
        self.assertEqual(result['accepted_slots'], 0)
        self.assertEqual(stored['status'], 'pending')

    def test_wrong_language_or_source_rows_do_not_count(self):
        """Rows must be read in the slot's language and carry its provenance."""
        from sekaisync import term_slots
        verifier = term_slots.corpus_verifier()
        slot = {'term_id': 'term:ja:セカイ', 'language': 'en', 'value': 'SEKAI',
                'source': self.SOURCE}
        other_language = term_slots.evidence_with_ids(
            'term:ja:セカイ', self._rows('SEKAI', ['s1', 's2'], language='ja'))
        self.assertIsNone(verifier(slot, other_language))
        other_source = term_slots.evidence_with_ids(
            'term:ja:セカイ', self._rows('SEKAI', ['s1', 's2'], source='llm'))
        self.assertIsNone(verifier(slot, other_source))

    def test_official_and_fanon_sources_are_refused_not_relabelled(self):
        """A and D are the index's and the review queue's job, not this one's.

        ``official=True`` requires trust A, and the corpus is not the official
        index, so an A-level source cannot be certified from story text.  D is
        "unverified/fanon": positional agreement in fan text does not verify a
        name, so the honest result is a queue entry, not an accepted slot.
        """
        from sekaisync import term_slots
        verifier = term_slots.corpus_verifier()
        for source in ('master_db', 'fandom'):
            with self.subTest(source=source):
                evidence = term_slots.evidence_with_ids(
                    't', self._rows('SEKAI', ['s1', 's2'], source=source))
                self.assertIsNone(verifier(
                    {'term_id': 't', 'language': 'en', 'value': 'SEKAI', 'source': source},
                    evidence))

    def test_trust_level_is_not_taken_from_the_candidate_source_label(self):
        """A caller cannot raise the level by choosing a friendlier source id.

        ``trust_for_source('altsource_ms')`` is B because that describes the
        *page*, not how the name was derived.  The same rows certify at C.
        """
        from sekaisync import term_slots
        result, slot = self._commit('SEKAI', ['s1', 's2'], source='altsource_ms')
        self.assertEqual(slot['status'], 'accepted')
        self.assertEqual(slot['trust'], 'C')

    def test_certificate_refs_stay_bounded_for_a_corpus_wide_term(self):
        """A term in hundreds of stories must not copy all of them into the slot."""
        from sekaisync import term_slots
        keys = [f's{i}' for i in range(200)]
        rows = self._rows('SEKAI', keys)
        evidence = term_slots.evidence_with_ids('term:ja:セカイ', rows)
        verifier = term_slots.corpus_verifier()
        proof = verifier({'term_id': 'term:ja:セカイ', 'language': 'en', 'value': 'SEKAI',
                          'source': self.SOURCE}, evidence)
        self.assertIsNotNone(proof)
        self.assertLessEqual(len(proof['evidence_refs']), term_slots.MAX_CORPUS_REFS)
        self.assertGreaterEqual(len(proof['evidence_refs']), term_slots.MIN_CORPUS_STORIES)

    def test_corpus_proof_refuses_a_slot_whose_evidence_belongs_to_another_subject(self):
        """The store, not the verifier, is the last line of defence.

        A verifier that authors a proof for another subject's rows must not be
        able to commit it: ``_certificate`` re-checks identity and provenance.
        """
        from sekaisync import term_slots
        rows = self._rows('SEKAI', ['s1', 's2'])
        evidence = term_slots.evidence_with_ids('term:ja:セカイ', rows)
        rows_other = self._rows('SEKAI', ['s3', 's4'], language='ko')
        other = term_slots.evidence_with_ids('term:ja:セカイ', rows_other)

        def confused(slot, items):
            return {'subject_id': slot['term_id'], 'language': slot['language'],
                    'value': slot['value'], 'source': 'llm', 'official': False,
                    'trust': 'C', 'verifier': 'test:wrong-source',
                    'evidence_refs': [row['evidence_id'] for row in items]}

        decision = {'term_id': 'term:ja:セカイ', 'language': 'en', 'value': 'SEKAI',
                    'status': 'accepted', 'source': self.SOURCE,
                    'evidence_refs': [row['evidence_id'] for row in evidence]}
        result = term_slots.commit_slot_decisions(
            self.root, [decision],
            records=[{'id': 'term:ja:セカイ', 'canonical': 'セカイ'}],
            evidence_by_id={'term:ja:セカイ': evidence + other},
            expected_revision=self._revision(), verifier=confused)
        self.assertEqual(result['accepted_slots'], 0)


class TrinityCorpusPathTests(unittest.TestCase):
    """The rows trinity emits and the verifier's reading of them must agree.

    ``CorpusVerifierTests`` above builds rows by hand so the assertions stay
    readable; this class runs the real producer, so a change to the emitted
    shape (a renamed field, a dropped story key) turns these red instead of
    silently making the pipeline uncommittable again.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        dbstore.initialize(self.root)
        dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                              backup_path=Path(self.tmp.name) / 'v2.db')
        dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                              backup_path=Path(self.tmp.name) / 'v3.db')

    def _scrub(self):
        from sekaisync import termindex, trinity
        from sekaisync.termindex import TermRecord

        groups = {
            's1': {'ja': {'text': 'セカイに行こう\n'}, 'en': {'text': "Let's go to SEKAI\n"},
                   'zh_hans': {'text': '去往世界\n'}},
            's2': {'ja': {'text': 'セカイは広い\n'}, 'en': {'text': 'SEKAI is wide\n'},
                   'zh_hans': {'text': '世界很宽\n'}},
            's3': {'ja': {'text': 'セカイの歌\n'}, 'en': {'text': 'Song of SEKAI\n'},
                   'zh_hans': {'text': '世界之歌\n'}},
        }
        glossary = [TermRecord(id='unit:sekai', canonical='セカイ', source_language='ja',
                               kind='unit', official=True, source='master_db',
                               names={'ja': 'セカイ', 'en': 'SEKAI',
                                      'zh_hans': '世界', 'zh_tw': '世界'})]
        targets = ('en', 'zh_hans')
        vocab = termindex.build_alignment_vocab(groups, targets, glossary)
        idf = termindex.compute_lang_idf(groups, targets, vocab=vocab)
        return trinity.scrub_trinity(groups, sorted(groups), ['セカイ'],
                                     target_languages=targets, glossary=glossary,
                                     vocab=vocab, idf=idf)

    def _commit(self, verifier):
        from sekaisync import term_slots, trinity
        result = self._scrub()
        decisions, evidence_by_id, records = [], {}, {}
        for decision in result['slot_decisions']:
            if not decision.get('value'):
                continue
            rows = [row for row in decision.get('evidence_rows') or []
                    if row.get('channel_value') == decision['value']]
            evidence = term_slots.evidence_with_ids(decision['term'], rows)
            if not evidence or decision['term'] in evidence_by_id:
                continue
            records[decision['term']] = {'id': decision['term'],
                                         'canonical': decision['term'],
                                         'source_language': 'ja'}
            evidence_by_id[decision['term']] = evidence
            decisions.append({'term_id': decision['term'], 'language': decision['language'],
                              'value': decision['value'], 'status': 'accepted',
                              'source': trinity.CORPUS_SOURCE,
                              'evidence_refs': [row['evidence_id'] for row in evidence]})
        with dbstore.connect(self.root) as conn:
            revision = dbstore.current_revision(conn)
        summary = term_slots.commit_slot_decisions(
            self.root, decisions, records=list(records.values()),
            evidence_by_id=evidence_by_id, expected_revision=revision, verifier=verifier)
        return summary, term_slots.load_term_slots(self.root)

    def test_real_channel_rows_are_accepted_by_the_corpus_verifier(self):
        from sekaisync import term_slots
        summary, slots = self._commit(term_slots.corpus_verifier())
        self.assertTrue(slots, 'the fixture produced no decisions')
        accepted = [slot for slot in slots if slot['status'] == 'accepted']
        self.assertTrue(accepted, 'no channel decision reached accepted')
        self.assertEqual(summary['accepted_slots'], len(accepted))
        for slot in accepted:
            with self.subTest(slot=(slot['term_id'], slot['language'])):
                self.assertEqual(slot['reason'], 'verified_evidence')
                self.assertEqual(slot['trust'], 'C')
                self.assertFalse(slot['official'])
                self.assertGreaterEqual(len(slot['evidence_refs']),
                                        term_slots.MIN_CORPUS_STORIES)
                self.assertEqual(slot['verification']['verifier'],
                                 term_slots.CORPUS_VERIFIER_NAME)

    def test_real_channel_rows_stay_pending_without_a_verifier(self):
        """Red half: the same rows and store, minus the callback."""
        summary, slots = self._commit(None)
        self.assertTrue(slots)
        self.assertTrue(all(slot['status'] == 'pending' for slot in slots))
        self.assertEqual(summary['accepted_slots'], 0)

    def test_every_accepted_slot_was_refused_by_the_official_index(self):
        """The corpus verifier is not a second way to say "official".

        ``index_verifier`` certifies only a name the store's own official
        glossary index carries for exactly one entity, and only when the
        slot's provenance is that index's.  The corpus rows below name the same
        value, but their authority is the story text, not the index — so the
        index refuses them, which is why the corpus path had to exist.  The
        non-vacuity check runs last: on a store whose glossary was never seeded
        every answer is None and this assertion would hold for free.
        """
        from sekaisync import term_slots
        from sekaisync.glossary import GlossaryTerm

        dbstore.save_glossary_terms(self.root, [GlossaryTerm(
            id='unit:sekai', kind='unit', canonical='セカイ',
            names={'ja': 'セカイ', 'en': 'SEKAI'}, official=True,
            source='master_db', demo=False, trust='A')])
        with contextlib.closing(sqlite3.connect(dbstore.db_file(self.root))) as conn:
            index_only = term_slots.index_verifier(conn)
            summary, slots = self._commit(index_only)
            self.assertEqual(summary['accepted_slots'], 0)
            self.assertTrue(all(slot['status'] == 'pending' for slot in slots),
                            'the official index certified corpus rows')
            # Not vacuous: for the indexed subject, in the indexed language,
            # under the index's own provenance, this same callback does answer.
            self.assertIsNotNone(index_only(
                {'term_id': 'セカイ', 'language': 'en', 'value': 'SEKAI',
                 'source': 'master_db'}, []))


class TermsExtractCommitTests(unittest.TestCase):
    """W1-B: the `terms extract` commit must pass the verifier itself.

    The verifier only helps if the command that owns the extraction actually
    hands one to the store.  This drives ``cmd_terms_extract`` (the non-layered
    branch, which is where ``commit_slot_decisions`` lives) against a temporary
    v2 store, so the wiring — not just the callback — is pinned.  The page
    provenance is ``local`` because that is the ``source`` the local extractor
    stamps on its records; an accepted slot requires the evidence source and the
    slot source to be the same string.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        dbstore.initialize_new_store(self.root)
        pages = []
        for index, (ja, en) in enumerate([
            ('セカイに行こう\n', "Let's go to SEKAI\n"),
            ('セカイは広い\n', 'SEKAI is wide\n'),
            ('セカイの歌\n', 'Song of SEKAI\n'),
        ]):
            for language, text in (('ja', ja), ('en', en)):
                pages.append({
                    'id': f'web:altsource_ms:{language}:event_story:{index}',
                    'url': f'https://example.invalid/{language}/{index}',
                    'title': 't', 'language': language, 'kind': 'event_story',
                    'text': text, 'crawled_at': '2026-01-01', 'trust': 'B',
                    'tos_accepted': True,
                })
        dbstore.upsert_web_pages(self.root, 'local', pages)

    def _run(self):
        import argparse
        import contextlib as _contextlib
        import io

        from sekaisync.cli import cmd_terms_extract

        args = argparse.Namespace(
            config=None, store=self.root, input=None, include_overlay=False,
            event=None, episode=None, **{'all': True}, source_language='ja',
            languages='ja,en', llm_config=None, max_terms=20, no_translate=True,
            align=True, local=True, layered=False)
        with _contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cmd_terms_extract(args), 0)

    def test_extract_commits_accepted_slots_through_the_real_command(self):
        from sekaisync import term_slots
        self._run()
        slots = term_slots.load_term_slots(self.root)
        self.assertTrue(slots, 'the extraction pass wrote no slots')
        accepted = [slot for slot in slots if slot['status'] == 'accepted']
        self.assertTrue(accepted, 'no slot was accepted; the pass accepted nothing')
        english = next(slot for slot in accepted if slot['language'] == 'en')
        self.assertEqual(english['value'], 'SEKAI')
        self.assertEqual(english['reason'], 'verified_evidence')
        self.assertEqual(english['trust'], 'C')
        self.assertEqual(english['verification']['verifier'],
                         term_slots.CORPUS_VERIFIER_NAME)
        # The command's own evidence rows back the certificate, and the read
        # projection follows the accepted slot.
        stored = dbstore.evidence_for_ids(self.root, [english['term_id']])[english['term_id']]
        by_id = {row['evidence_id']: row for row in stored}
        self.assertEqual(len({by_id[ref]['story_key'] for ref in english['evidence_refs']}), 3)
        self.assertEqual(dbstore.load_terms_records(self.root)[0].names, {'en': 'SEKAI'})

    def test_extract_accepts_nothing_when_the_verifier_is_removed(self):
        """Red half: same store, same pages, no verifier -> everything pending."""
        from unittest import mock

        from sekaisync import term_slots
        with mock.patch.object(term_slots, 'corpus_verifier', lambda *a, **k: None):
            self._run()
        slots = term_slots.load_term_slots(self.root)
        self.assertTrue(slots, 'the extraction pass wrote no slots')
        self.assertTrue(all(slot['status'] == 'pending' for slot in slots),
                        'a slot was accepted without a verifier')
        self.assertFalse(dbstore.load_terms_records(self.root)[0].names)

    def test_extract_keeps_other_languages_pending(self):
        """Honest boundary: only the language with corpus rows is certified.

        The ja and non-English slots have no value-bearing rows in this fixture
        (the local extractor only aligns ja->en here), so they must stay
        pending rather than inherit the English slot's authority.
        """
        from sekaisync import term_slots
        self._run()
        slots = {slot['language']: slot for slot in term_slots.load_term_slots(self.root)}
        self.assertEqual(slots['en']['status'], 'accepted')
        for language in ('ja', 'zh_hans', 'zh_tw', 'ko'):
            with self.subTest(language=language):
                self.assertEqual(slots[language]['status'], 'pending')
                self.assertEqual(slots[language]['trust'], '')


class PersistTermUpdatesVerifierTests(unittest.TestCase):
    """W1-B: `persist_term_updates` must forward a caller-supplied verifier.

    The keyword existed but was the only one with no documentation and no test
    covering the forwarding, so a caller could pass a verifier and get the
    unverified behaviour with no symptom.  Both directions are pinned: omitted
    keeps the old pending result, supplied certifies.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'store'
        dbstore.initialize_new_store(self.root)
        from sekaisync.termindex import TermRecord
        self.record = TermRecord(id='term:ja:セカイ', canonical='セカイ',
                                 source_language='ja', names={'ja': 'セカイ'})
        self.rows = [{'story_key': key, 'language': 'en', 'source': 'corpus',
                      'term': 'セカイ', 'sentence': 'Go to SEKAI',
                      'channel': 'trunk', 'channel_value': 'SEKAI'}
                     for key in ('s1', 's2')]

    def _revision(self):
        with dbstore.connect(self.root) as conn:
            return dbstore.current_revision(conn)

    def _update(self, verifier=...):
        from sekaisync import termindex, term_slots
        # ``persist_term_updates`` states the evidence from the records it is
        # given, so the rows live on the record and the refs are computed the
        # same way the commit path will normalise them.
        self.record.evidence = [dict(row) for row in self.rows]
        evidence = term_slots.evidence_with_ids(self.record.id, self.record.evidence)
        update = {'value': 'SEKAI', 'status': 'accepted', 'source': 'corpus',
                  'evidence_refs': [row['evidence_id'] for row in evidence]}
        kwargs = {} if verifier is ... else {'verifier': verifier}
        result = termindex.persist_term_updates(
            self.root, [self.record],
            slot_updates={(self.record.id, 'en'): update},
            expected_revision=self._revision(), **kwargs)
        return result, term_slots.load_term_slots(self.root)

    def test_omitting_the_verifier_keeps_the_old_pending_behaviour(self):
        from sekaisync import termindex
        result, slots = self._update()
        self.assertEqual(result['accepted_slots'], 0)
        slot = next(slot for slot in slots if slot['language'] == 'en')
        self.assertEqual(slot['status'], 'pending')
        self.assertEqual(slot['reason'], 'insufficient_evidence')

    def test_supplying_the_corpus_verifier_certifies_the_slot(self):
        from sekaisync import term_slots
        result, slots = self._update(term_slots.corpus_verifier())
        self.assertEqual(result['accepted_slots'], 1)
        slot = next(slot for slot in slots if slot['language'] == 'en')
        self.assertEqual(slot['status'], 'accepted')
        self.assertEqual(slot['trust'], 'C')
        self.assertEqual(slot['reason'], 'verified_evidence')

    def test_v1_store_still_refuses_slot_updates_whatever_the_verifier(self):
        """The v1 guard is unchanged: passing a verifier does not enable slots."""
        from pathlib import Path as _Path

        from sekaisync import termindex, term_slots
        legacy = _Path(self.tmp.name) / 'legacy'
        dbstore.initialize(legacy)
        with self.assertRaisesRegex(ValueError, 'slot updates require explicit migration'):
            termindex.persist_term_updates(
                legacy, [self.record],
                slot_updates={(self.record.id, 'en'): {'value': 'SEKAI'}},
                expected_revision=0, verifier=term_slots.corpus_verifier())


if __name__ == '__main__':
    unittest.main()
