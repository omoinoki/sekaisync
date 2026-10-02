"""Build twenty-direction packets from frozen host-authored semantic proposals.

This tool does not invent semantic labels: the host-authored proposal file is
the semantic claim. It mechanically resolves unique raw hits in bounded pairs.
Ambiguity and missing target evidence stay pending for explicit host review.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as packets, agent_review as review


def main():
    census = ROOT / "work/p0-exhaustive-20261001/census"
    store = census / "holdout-store-smoke"
    proposal_path = census / "proposals/cross-language-root.json"
    proposal = json.loads(proposal_path.read_text(encoding="utf-8"))
    tasks, judgments, pending = [], [], []
    for language in ("ja", "en", "zh_hans", "zh_hant", "ko"):
        discovery = json.loads((census / "review-smoke" / (language + ".json")).read_text(encoding="utf-8"))[0]
        scope_id = discovery["review_context"]["scope_id"]
        scope = packets._read_scope(store, scope_id)
        for concept in proposal["concepts"]:
            term = concept["surfaces"][language]
            for target in scope["target_languages"]:
                target_key = "zh_hant" if target == "zh_tw" else target
                translation = packets._translation_item(scope_id, scope, term, target)
                if translation is None:
                    pending.append(dict(concept=concept["key"], source=language, target=target_key, reason="no source packet"))
                    continue
                task = packets._occurrence_item(translation)
                tasks.append(task)
                value = concept["surfaces"][target_key]
                candidates = []
                for row in task._context["rows"]:
                    sources = packets._body_term_segments(row["source"]["text"], term, row["source"]["start"])
                    targets = packets._body_term_segments(row["target"]["text"], value, row["target"]["start"])
                    if len(sources) == len(targets) == 1:
                        candidates.append((row, sources[0], targets[0]))
                if not candidates:
                    pending.append(dict(id=task.id, concept=concept["key"], source=language, target=target_key,
                                        reason="no unique source/target expression in bounded packet; explicit semantic span review required"))
                    continue
                # A single contextual relation suffices; no global name is published.
                row, source_segments, target_segments = candidates[0]
                relation_kind = "paraphrase" if "paraphrase" in {
                    concept.get("relations", {}).get(language), concept.get("relations", {}).get(target_key)} else "lexical"
                relation = dict(evidence_id=row["id"], source_segments=source_segments, target_segments=target_segments,
                                sense_key=concept["key"], sense_gloss=concept["gloss"], kind=relation_kind,
                                rationale="Host read all five frozen original bodies and proposed this exact contextual concept; this is a machine-reviewed occurrence, not a global synonym or human gold.")
                judgments.append(dict(id=task.id, decision="accept", agent=proposal["agent"], relations=[relation]))
    result = review.enqueue(store, tasks)
    for name, value in (("occurrence-smoke-root-tasks.json", [task.to_dict() for task in tasks]),
                        ("occurrence-smoke-root.json", judgments), ("occurrence-smoke-root-pending.json", pending)):
        (census / "proposals" / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    frozen = dict(proposal_sha256=hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
                  tasks=len(tasks), proposed_relations=len(judgments), pending=len(pending), enqueue=result,
                  reviewer_labels_used=False)
    (census / "proposals/occurrence-smoke-root-freeze.json").write_text(json.dumps(frozen, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(frozen, indent=2))


if __name__ == "__main__":
    main()
