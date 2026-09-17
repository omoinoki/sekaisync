"""Offline, explicitly labelled slot evaluation; no store or model access.

Gold is a fixed (subject_id, language) -> display value mapping. Decisions are
mappings with subject_id, language, status, value, source and tier. Callers must
supply stable subject IDs: surface strings are not entity resolution. This is
not the legacy trinity coverage comparison and does not infer gold from output.
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence

from sekaisync.normalize import normalize_name


def _language(value: str) -> str:
    value = value.strip().lower().replace('-', '_')
    return {'zh_hant': 'zh_tw', 'zh_cn': 'zh_hans', 'zh': 'zh_hans'}.get(value, value)


def _fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def evaluate_slots(gold: Mapping[tuple[str, str], str], decisions: Sequence[Mapping],
                   *, evaluation_story_keys=(), tuning_story_keys=()) -> dict:
    """Count unique slots, not terms or proposals.

    Missing/pending/rejected/conflict gold slots are abstentions and false
    negatives; accepted wrong values are FP and FN. Accepted slots absent from
    gold are unlabelled (excluded from precision, reported separately), not
    automatically wrong. Duplicate equivalent decisions count once; competing
    accepted values or statuses produce a conflict, never a lucky true positive.
    Source/tier breakdowns are descriptive subsets, not additive totals.
    """
    evaluation = sorted(set(evaluation_story_keys))
    tuning = sorted(set(tuning_story_keys))
    if set(evaluation) & set(tuning):
        raise ValueError('evaluation and tuning stories must be disjoint')
    expected = {}
    for (subject, language), value in gold.items():
        if not subject or not language or not value or not normalize_name(value):
            raise ValueError('gold requires nonempty subject, language and value')
        key = (subject, _language(language))
        if key in expected and normalize_name(expected[key]) != normalize_name(value):
            raise ValueError('conflicting gold aliases for one slot')
        expected[key] = value
    rows = []
    grouped = defaultdict(list)
    statuses = {'accepted', 'pending', 'rejected', 'conflict', 'abstained'}
    for decision in decisions:
        subject, language = decision.get('subject_id'), decision.get('language')
        status = decision.get('status')
        value = decision.get('value') or ''
        if not isinstance(subject, str) or not subject or not isinstance(language, str) or not language:
            raise ValueError('decisions require explicit subject_id and language')
        if status not in statuses or not isinstance(value, str):
            raise ValueError('invalid decision status or value')
        if status == 'accepted' and not normalize_name(value):
            raise ValueError('accepted decisions require a nonempty value')
        row = dict(subject_id=subject, language=_language(language), status=status,
                   value=value, source=str(decision.get('source') or 'unknown'),
                   tier=str(decision.get('tier') or 'unknown'))
        rows.append(row)
        grouped[(subject, row['language'])].append(row)

    def metrics(keys):
        counts = dict(gold_slots=0, accepted_slots=0, true_positive=0,
                      false_positive=0, false_negative=0, abstained_slots=0,
                      conflict_slots=0, unlabelled_slots=0, unlabelled_accepted=0)
        for key in keys:
            proposals = grouped.get(key, [])
            accepted = {normalize_name(r['value']) for r in proposals if r['status'] == 'accepted'}
            statuses_here = {r['status'] for r in proposals}
            conflict = ('conflict' in statuses_here or len(accepted) > 1 or
                        ('accepted' in statuses_here and len(statuses_here) > 1))
            answered = bool(accepted) and not conflict
            counts['conflict_slots'] += int(conflict)
            counts['accepted_slots'] += int(answered)
            if key not in expected:
                counts['unlabelled_slots'] += 1
                counts['unlabelled_accepted'] += int(answered)
                continue
            counts['gold_slots'] += 1
            if not answered:
                counts['abstained_slots'] += 1
                counts['false_negative'] += 1
            elif normalize_name(expected[key]) in accepted:
                counts['true_positive'] += 1
            else:
                counts['false_positive'] += 1
                counts['false_negative'] += 1
        tp, fp = counts['true_positive'], counts['false_positive']
        n = counts['gold_slots']
        counts.update(precision=tp / (tp + fp) if tp + fp else None,
                      recall=tp / n if n else None,
                      abstention_rate=counts['abstained_slots'] / n if n else None)
        return counts

    all_keys = set(expected) | set(grouped)
    sources = sorted({r['source'] for r in rows})
    def tier_group(tier):
        return 'L0' if tier == 'L0' else ('unknown' if tier == 'unknown' else 'non_L0')
    report = metrics(all_keys)
    report['by_source'] = {source: metrics({k for k, rs in grouped.items()
                                          if any(r['source'] == source for r in rs)})
                           for source in sources}
    report['by_tier'] = {tier: metrics({k for k, rs in grouped.items()
                                     if any(tier_group(r['tier']) == tier for r in rs)})
                         for tier in ('L0', 'non_L0', 'unknown')}
    gold_rows = [[*key, value] for key, value in sorted(expected.items())]
    rows.sort(key=lambda r: json.dumps(r, sort_keys=True))
    report['gold_fingerprint'] = _fingerprint(gold_rows)
    report['input_fingerprint'] = _fingerprint([gold_rows, rows, evaluation, tuning])
    report['evaluation_story_keys'] = evaluation
    report['tuning_story_keys'] = tuning
    report['evaluation_unit'] = 'subject_id,language'
    return report
