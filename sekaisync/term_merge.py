"""Conservative legacy-record merge; provenance lives in existing evidence.

No schema/slot fields are assumed. A record-wide authority label is safe only
when every retained name is covered by official input of that same subject.
"""
from copy import deepcopy


def merge_subject(records):
    records = deepcopy(records)
    result = records[0]
    if len(records) == 1:
        return result
    candidates = {}
    for record in records:
        for language, value in record.names.items():
            if value:
                candidates.setdefault(language, []).append((value, record))
    names = {}
    evidence = []
    for record in records:
        for row in record.evidence:
            if row not in evidence:
                evidence.append(deepcopy(row))
    selected = []
    for language, options in candidates.items():
        chosen = next((item for item in options if item[1].official), options[0])
        value, owner = chosen
        names[language] = value
        selected.append(owner)
        if len({v for v, _ in options}) > 1:
            for original, contributor in options:
                row = {
                    "language": language, "term": original, "status": "pending",
                    "reason": "merge_conflict", "subject_id": contributor.id,
                    "source": contributor.source, "reported_trust": contributor.trust,
                    "reported_official": contributor.official,
                    "reported_confidence": contributor.confidence,
                    "original_evidence": deepcopy(contributor.evidence),
                }
                if row not in evidence:
                    evidence.append(row)
    result.names = names
    result.evidence = evidence
    result.official = bool(selected) and all(owner.official for owner in selected)
    rank = {"A": 4, "B": 3, "C": 2, "D": 1, "": 0}
    weakest = min(selected, key=lambda owner: rank.get(owner.trust, 0)) if selected else result
    result.trust = weakest.trust
    result.source = weakest.source
    result.confidence = min((owner.confidence for owner in selected), default=result.confidence)
    result.tags = sorted({tag for record in records for tag in record.tags})
    result.positions = []
    for record in records:
        for pos in record.positions:
            if pos not in result.positions:
                result.positions.append(deepcopy(pos))
    return result
