"""Offline P20 regressions: synthetic inputs only; never load a default store."""
import unittest
from unittest.mock import patch

from sekaisync import penetrate_channels as pc, zhfirst
from sekaisync.normalize import normalize_name
from sekaisync.wordseg import discover_words


class CacheAndTriangleTests(unittest.TestCase):
    def setUp(self):
        pc.clear_known_names()
        pc.register_alignment_context({}, 'ja', None)

    def tearDown(self):
        pc.clear_known_names()
        pc.register_alignment_context({}, 'ja', None)

    def test_same_object_equal_length_replacement(self):
        groups = {'s': {'ja': {'text': '旧本文'}}}
        self.assertEqual(pc._story_text_cache(groups)['s'][0], '旧本文')
        groups['s']['ja'] = {'text': '新本文'}
        self.assertEqual(pc._story_text_cache(groups)['s'][0], '新本文')
        groups['s']['ja']['text'] = '再変更'
        self.assertEqual(pc._story_text_cache(groups)['s'][0], '再変更')

    def test_exact_language_before_alias(self):
        groups = {'s': {'zh_tw': {'text': '别名'}, 'zh_hant': {'text': '精确'}}}
        self.assertEqual(pc._page_for(groups, 's', 'zh_hant')['text'], '精确')
        self.assertEqual(pc._page_for(groups, 's', 'zh_tw')['text'], '别名')

    def test_dictionary_mutation_does_not_reuse_aux_alignment(self):
        groups, vocab, idf = {}, {'ko': {'old'}}, {}
        pc.register_alignment_context(groups, 'ja', idf, vocab)
        with patch.object(pc.termindex, 'align_term_by_frequency',
                          side_effect=lambda *a, **kw: sorted(kw['vocab']['ko'])[0]):
            self.assertEqual(pc._aux_name_for('TERM', 'ko')[0], 'old')
            vocab['ko'] = {'new'}
            self.assertEqual(pc._aux_name_for('TERM', 'ko')[0], 'new')

    def test_triangle_cannot_join_disjoint_stories(self):
        groups = {
            'a': {'ja': {'text': 'セカイ'}, 'en': {'text': 'SEKAI'}, 'ko': {'text': '없음'}},
            'b': {'ja': {'text': 'セカイ'}, 'en': {'text': 'OTHER'}, 'ko': {'text': 'SEKAI'}},
        }
        self.assertFalse(pc.verify_triangle('セカイ', 'ja', 'SEKAI', 'en', groups,
                                            aux_languages=('ko',))['verified'])

    def test_language_switch_and_exact_registry_alias(self):
        groups = {'s': {'ja': {'text': '日本語'}, 'en': {'text': 'English'}}}
        self.assertEqual(pc._story_text_cache(groups, 'en')['s'][0], 'English')
        self.assertEqual(pc._story_text_cache(groups, 'ja')['s'][0], '日本語')
        pc.register_known_names('term', {'zh_tw': '别名', 'zh_hant': '精确'})
        self.assertEqual(pc._aux_name_for('term', 'zh_hant'), ('精确', 'registry'))

    def test_aux_languages_must_share_one_anchor_story(self):
        groups = {
            'a': {'ja': {'text': 'セカイ'}, 'en': {'text': 'SEKAI'}, 'ko': {'text': 'SEKAI'}},
            'b': {'ja': {'text': 'セカイ'}, 'en': {'text': 'SEKAI'}, 'zh_hans': {'text': 'SEKAI'}},
        }
        self.assertFalse(pc.verify_triangle('セカイ', 'ja', 'SEKAI', 'en', groups,
                                            aux_languages=('ko', 'zh_hans'), max_aux_required=2)['verified'])
        groups['a']['zh_hans'] = {'text': 'SEKAI'}
        self.assertTrue(pc.verify_triangle('セカイ', 'ja', 'SEKAI', 'en', groups,
                                           aux_languages=('ko', 'zh_hans'), max_aux_required=2)['verified'])

    def test_latin_substring_is_not_an_anchor(self):
        groups = {'a': {'ja': {'text': 'セカイ'}, 'en': {'text': 'SEKAIworld'}, 'ko': {'text': 'SEKAI'}}}
        self.assertFalse(pc.verify_triangle('セカイ', 'ja', 'Sekai', 'en', groups)['verified'])

    def test_normalized_anchor_keeps_display_surface(self):
        groups = {'a': {'ja': {'text': 'セカイ'}, 'en': {'text': 'SEKAI'},
                        'ko': {'text': 'SEKAI'}}}
        self.assertTrue(pc.verify_triangle('セカイ', 'ja', 'Sekai', 'en', groups,
                                           aux_languages=('ko',))['verified'])

    def test_unverified_legacy_pair_is_pending(self):
        groups = {s: {'ja': {'text': 'セカイ'}, 'en': {'text': 'SEKAI'}} for s in ('a', 'b')}
        result = pc.penetrate_layered(groups, list(groups), ['セカイ'],
                                     target_languages=('en',), glossary=[])
        self.assertNotIn('en', result['pairs'].get('セカイ', {}))
        self.assertEqual(result['pending'][0]['candidate'], 'SEKAI')
        self.assertTrue(result['pending'][0]['reason'])


class WordAndFilterTests(unittest.TestCase):
    def test_list_generator_budget_equivalence(self):
        texts = ['网络天堂网络天堂'] * 8
        kw = dict(max_chars=18, min_freq=1, min_entropy=0, min_cohesion=-10)
        expected = discover_words(texts, **kw)
        self.assertTrue(expected)
        self.assertEqual(discover_words(iter(texts), **kw), expected)

    def test_budget_stops_consuming(self):
        def texts():
            yield '网络天堂'
            raise AssertionError('consumed beyond the budget')
        self.assertTrue(discover_words(texts(), max_chars=4, min_freq=1,
                                       min_entropy=0, min_cohesion=-10))

    def test_protagonist_block_applies_to_proper_and_seed(self):
        for seed in (set(), {'LUMINA时间'}):
            got = zhfirst.extract_zh_candidates_from_story(
                'LUMINA时间', 's', set(), {normalize_name('LUMINA时间')}, seed=seed)
            self.assertNotIn('LUMINA时间', [s for s, quoted in got])

    def test_no_key_invokes_rule_filter(self):
        groups = {'s': {'zh_hans': {'text': '泡泡相扑'}, 'ja': {'text': '仮'}}}
        with patch.object(zhfirst, 'group_pages_by_story', return_value=groups), \
             patch.object(zhfirst, 'build_zhfirst_blocklist', return_value=(set(), {})), \
             patch.object(zhfirst, 'discover_words', return_value=set()), \
             patch.object(zhfirst, '_load_manual_seed', return_value=set()), \
             patch.object(zhfirst, 'extract_zh_candidates_from_story', return_value=[('泡泡相扑', False)]), \
             patch.object(zhfirst, 'llm_filter_terms', wraps=zhfirst.llm_filter_terms) as filt:
            got = zhfirst.extract_terms_zhfirst([], [], [], llm=None)
        self.assertEqual([t.canonical for t in got], ['泡泡相扑'])
        filt.assert_called_once_with(['泡泡相扑'], None)


class CandidateSignalTests(unittest.TestCase):
    def test_quote_signal_survives_filtering(self):
        from sekaisync.candidate_tiers import CandidateEvidence
        evidence = CandidateEvidence(surface='泡泡相扑', quoted=True,
                                     positions=(('s', 2, 4),), rescued_by=('hub',))
        result = pc.penetrate_layered({}, [], [evidence], source_language='zh_hans', glossary=[])
        self.assertEqual(result['tier_stats']['L1'], 1)
        self.assertEqual(result['candidate_evidence']['泡泡相扑']['positions'], [('s', 2, 4)])
        self.assertEqual(result['candidate_evidence']['泡泡相扑']['rescued_by'], ['hub'])

    def test_later_quoted_duplicate_upgrades_signal(self):
        from sekaisync.candidate_tiers import CandidateEvidence
        result = pc.penetrate_layered({}, [], ['泡泡相扑', CandidateEvidence('泡泡相扑', quoted=True)],
                                      source_language='zh_hans', glossary=[])
        self.assertEqual(result['tier_stats']['L1'], 1)
        self.assertEqual(len(result['candidate_evidence']), 1)

    def test_normalized_dedup_preserves_first_surface(self):
        from sekaisync.candidate_tiers import filter_alignable
        self.assertEqual(filter_alignable(['Sekai', 'SEKAI'], official_keys={'SEKAI'}), ['Sekai'])


class EvaluationRegressions(unittest.TestCase):
    def test_other_entity_same_name_does_not_count(self):
        # This fixture was first run against trinity.compare_with_baseline:
        # it wrongly counted 'other' as covering 'wanted'. That legacy function
        # is outside this change's write scope; evaluate through the slot API.
        from sekaisync.slot_evaluation import evaluate_slots
        report = evaluate_slots({('wanted', 'en'): 'Shared'}, [
            dict(subject_id='other', language='en', value='Shared', status='accepted')])
        self.assertEqual(report['true_positive'], 0)
        self.assertEqual(report['abstained_slots'], 1)
        self.assertEqual(report['unlabelled_accepted'], 1)
        self.assertIsNone(report['precision'])

    def test_fixed_slot_gold_counts_and_fingerprints(self):
        from sekaisync.slot_evaluation import evaluate_slots
        gold = {('a', 'en'): 'SEKAI', ('a', 'zh_hans'): '世界',
                ('b', 'en'): 'Shared', ('c', 'en'): 'Third'}
        rows = [dict(subject_id='a', language='en', value='Sekai', status='accepted', source='official', tier='L0'),
                dict(subject_id='a', language='zh_hans', value='错误', status='accepted', source='hub', tier='L1'),
                dict(subject_id='b', language='en', value='', status='conflict', source='hub', tier='L2')]
        result = evaluate_slots(gold, rows, evaluation_story_keys=['held-out'], tuning_story_keys=['tuning'])
        self.assertEqual((result['true_positive'], result['false_positive'], result['false_negative']), (1, 1, 3))
        self.assertEqual((result['precision'], result['recall'], result['abstention_rate']), (.5, .25, .5))
        self.assertEqual(result['conflict_slots'], 1)
        self.assertEqual(result['by_tier']['L0']['precision'], 1)
        self.assertEqual(result['by_source']['hub']['false_positive'], 1)
        reordered = evaluate_slots(gold, rows[::-1], evaluation_story_keys=['held-out'], tuning_story_keys=['tuning'])
        self.assertEqual(result['input_fingerprint'], reordered['input_fingerprint'])
        with self.assertRaises(ValueError):
            evaluate_slots(gold, rows, evaluation_story_keys=['s'], tuning_story_keys=['s'])

    def test_duplicate_and_conflicting_decisions(self):
        from sekaisync.slot_evaluation import evaluate_slots
        row = dict(subject_id='a', language='zh_hant', value='世界', status='accepted')
        gold = {('a', 'zh_tw'): '世界'}
        self.assertEqual(evaluate_slots(gold, [row, row])['true_positive'], 1)
        result = evaluate_slots(gold, [row, dict(row, value='其他')])
        self.assertEqual(result['true_positive'], 0)
        self.assertEqual(result['conflict_slots'], 1)


if __name__ == '__main__':
    unittest.main()
