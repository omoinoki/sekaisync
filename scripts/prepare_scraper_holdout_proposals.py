"""Ground manually authored, reviewer-isolated holdout source proposals."""
from __future__ import annotations

import hashlib
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as packets, span_subjects


def _typed_observations(proposal, language, rows, used):
    """Ground host-selected fragment observations, never fill from a reference."""
    entries = []
    for observation in proposal.get("segmented_observations", []):
        if observation["language"] != language:
            continue
        clause, fragments = observation["clause"], observation["parts"]
        for row in rows:
            view = row["source"]
            hits = [index for index in range(len(view["text"])) if view["text"].startswith(clause, index)]
            if not hits:
                continue
            if len(hits) != 1:
                raise ValueError("typed observation requires an unambiguous host-selected clause")
            parts, cursor = [], 0
            for fragment in fragments:
                start = clause.index(fragment, cursor)
                end = start + len(fragment)
                parts.append(dict(start=view["start"] + hits[0] + start,
                                  end=view["start"] + hits[0] + end, exact=fragment))
                cursor = end
            subject = span_subjects._segmented(view, row["story_key"], parts)
            if subject["id"] not in used:
                entries.append(dict(kind="segmented", evidence_id=row["id"], segments=parts))
                used.add(subject["id"])
    return entries


def _literal_present(view, surface):
    return any(packets._term_selection(view, surface, parts, case_sensitive=True)
               for parts in packets._body_term_segments(view["text"], surface, view["start"]))


def main():
    census = ROOT / "work/p0-exhaustive-20261001/census"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--proposal", type=Path, default=census / "proposals/source-surfaces-root.json")
    parser.add_argument("--review-dir", type=Path, default=census / "review-smoke")
    parser.add_argument("--output-prefix", type=Path, default=census / "proposals/discovery-smoke-root")
    args = parser.parse_args()
    proposal_bytes = args.proposal.read_bytes()
    proposal = json.loads(proposal_bytes.decode("utf-8"))
    judgments, coverage = [], []
    used_subjects = set()
    for language, surfaces in proposal["surfaces"].items():
        tasks = json.loads((args.review_dir / (language + ".json")).read_text(encoding="utf-8"))
        tasks = [task for task in tasks if task["review_context"]["task"] == "discovery"
                 and {row["story_key"] for row in task["review_context"]["rows"]} == {proposal["story_key"]}]
        if not tasks:
            raise ValueError("no matching frozen discovery tasks for " + language)
        observed = set()
        for task in tasks:
            rows = task["review_context"]["rows"]
            grounded = [surface for surface in surfaces if any(_literal_present(row["source"], surface) for row in rows)]
            if len(grounded) > 200:
                raise ValueError("manual proposal exceeds task budget; prepare continuation first")
            observed.update(grounded)
            subjects = _typed_observations(proposal, language, rows, used_subjects)
            if len(subjects) > 200:
                raise ValueError("manual typed proposal exceeds task budget; prepare continuation first")
            judgment = dict(id=task["id"], decision="accept", terms=grounded,
                            agent=proposal["agent"], rationale=proposal["provenance"])
            if subjects:
                judgment["subjects"] = subjects
            judgments.append(judgment)
            coverage.append(dict(id=task["id"], language=language, windows=len(rows),
                                 terms=len(grounded), typed_subjects=len(subjects), full_body_read_by_host=True,
                                 explicit_empty=not grounded, reviewer_labels_used=False))
        absent = sorted(set(surfaces) - observed)
        if absent:
            raise ValueError(f"ungrounded manual surfaces in {language}: {absent}")
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    output = args.output_prefix.with_suffix(".json")
    coverage_path = args.output_prefix.with_name(args.output_prefix.name + "-coverage.json")
    if output.exists() or coverage_path.exists():
        raise FileExistsError("proposal judgments are frozen; choose another output prefix")
    if len(used_subjects) != len(proposal.get("segmented_observations", [])):
        raise ValueError("not all host-selected segmented observations belong to frozen source packets")
    with output.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(judgments, ensure_ascii=False, indent=2) + "\n")
    with coverage_path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(proposal_sha256=hashlib.sha256(proposal_bytes).hexdigest(),
                                     tasks=coverage), ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(tasks=len(judgments), task_terms=sum(len(row["terms"]) for row in judgments),
                          output=str(output)), indent=2))


if __name__ == "__main__":
    main()
