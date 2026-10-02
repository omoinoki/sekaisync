"""Score a frozen task-wide source control only at focused raw windows."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap
from scripts.evaluate_scraper_focused_source import canonical, digest, source_inputs
from scripts.evaluate_scraper_span_source import frozen_json, signature


def exact_candidates(rows: list[dict], surfaces: list[str]) -> list[dict]:
    result = []
    for surface in dict.fromkeys(surfaces):
        if not isinstance(surface, str) or not surface:
            raise ValueError("Control ordinary terms must be nonempty raw strings")
        for row in rows:
            view = row["source"]
            for parts in ap._body_term_segments(view["text"], surface, view["start"]):
                # The public search is normalized; this evaluation requires raw case.
                if "".join(part["exact"] for part in parts) == surface:
                    result.append({"surface": surface, "segments": parts, "evidence_id": row["id"]})
    return result


def selected_task(raw: bytes, identity: str) -> tuple[dict, str]:
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    cursor = text.index("[") + 1
    while cursor < len(text):
        while cursor < len(text) and (text[cursor].isspace() or text[cursor] == ","):
            cursor += 1
        if cursor == len(text) or text[cursor] == "]":
            break
        start = cursor
        value, cursor = decoder.raw_decode(text, cursor)
        if value.get("id") == identity:
            return value, digest(text[start:cursor].encode())
    raise ValueError("Frozen original control source task is absent")


def segmented_candidates(rows: list[dict], observations: list[dict]) -> tuple[list[dict], list[dict]]:
    candidates, diagnostics = [], []
    for index, item in enumerate(observations):
        if set(item) != {"clause", "parts"} or len(item["parts"]) < 2:
            raise ValueError("Control segmented observation needs its raw clause and parts")
        clauses = exact_candidates(rows, [item["clause"]])
        groups = []
        for clause in clauses:
            start, end = clause["segments"][0]["start"], clause["segments"][-1]["end"]
            choices = [exact_candidates(rows, [part]) for part in item["parts"]]
            selected, previous = [], start
            for possible in choices:
                matching = [candidate for candidate in possible if candidate["evidence_id"] == clause["evidence_id"]
                            and candidate["segments"][0]["start"] >= previous and candidate["segments"][-1]["end"] <= end]
                if not matching:
                    selected = []
                    break
                selected.extend(matching[0]["segments"])
                previous = matching[0]["segments"][-1]["end"]
            if selected:
                groups.append({"observation_index": index, "segments": selected, "evidence_id": clause["evidence_id"]})
        candidates.extend(groups)
        diagnostics.append({"observation_index": index, "clause": item["clause"], "parts": item["parts"],
                            "exact_raw_clause_occurrences": len(clauses), "exact_part_groups": len(groups)})
    return candidates, diagnostics


def score(census: Path, focused_tasks: Path, reference_path: Path, control_path: Path,
          original_path: Path, output: Path, expected_control_sha256: str) -> dict:
    paths = {"reference": reference_path, "control": control_path, "original_control_file": original_path,
             "focused_tasks": focused_tasks}
    raw = {key: path.read_bytes() for key, path in paths.items()}
    hashes = {key: digest(value) for key, value in raw.items()}
    if hashes["control"] != expected_control_sha256:
        raise ValueError("Frozen control hash mismatch")
    reference, control = json.loads(raw["reference"]), json.loads(raw["control"])
    if canonical(reference, "reference_sha256") != reference["reference_sha256"]:
        raise ValueError("Independent focused reference hash mismatch")
    if reference.get("proposal_or_tokenizer_candidates_read") is not False:
        raise ValueError("Reference independence declaration missing")
    if (control["story_key"], control["language"]) != (reference["story_key"], reference["language"]):
        raise ValueError("Control story or language mismatch")
    manifest, focused, rows, page, body, focused_hashes = source_inputs(census, focused_tasks, reference["story_key"], reference["language"])
    if focused_hashes != reference["input_sha256"]:
        raise ValueError("Focused source snapshot changed")
    provenance = control["provenance"]
    task, task_hash = selected_task(raw["original_control_file"], provenance["source_task_id"])
    if hashes["original_control_file"] != provenance["source_file_sha256"] or task_hash != provenance["raw_source_task_sha256"]:
        raise ValueError("Original control source provenance hash mismatch")
    original_rows = task["review_context"]["rows"]
    if len(original_rows) != 72 or len(original_rows) != provenance["source_window_count"]:
        raise ValueError("Original control must preserve its frozen 72-window batch")
    if [(row["id"], row["source"]) for row in original_rows[:24]] != [(row["id"], row["source"]) for row in rows]:
        raise ValueError("Control first 24 raw windows do not match the focused source")
    for row in original_rows:
        view = row["source"]
        if (row["story_key"] != reference["story_key"] or view["page_id"] != page["page_id"]
                or view["source"] != page["source"] or view["sha256"] != page["text_sha256"]
                or body[view["start"]:view["end"]] != view["text"]):
            raise ValueError("Control original raw window identity mismatch")
    literal = exact_candidates(rows, control["terms"])
    all_literal = exact_candidates(original_rows, control["terms"])
    extra, extra_diagnostics = segmented_candidates(rows, control["segmented_observations"])
    all_extra, all_extra_diagnostics = segmented_candidates(original_rows, control["segmented_observations"])
    first_types, all_types = {item["surface"] for item in literal}, {item["surface"] for item in all_literal}
    assessments = []
    for annotation in reference["annotations"]:
        parts = annotation["primary_segments"]
        target = signature(parts)
        matches = sorted({item["surface"] for item in literal if signature(item["segments"]) == target})
        segmented_matches = [item["observation_index"] for item in extra if signature(item["segments"]) == target]
        assessments.append({"annotation_id": annotation["id"], "surface": annotation["surface"], "row_index": annotation["row_index"],
            "categories": annotation["categories"], "primary_segments": parts, "surface_is_contiguous": annotation["surface_is_contiguous"],
            "ordinary_terms_strict_primary_exact": bool(matches), "matching_literal_surfaces": matches,
            "extra_segmented_strict_primary_exact": bool(segmented_matches), "matching_segmented_observations": segmented_matches})
    def counts(values):
        return {"reference_source_units": len(values), "ordinary_terms_strict_primary_exact": sum(item["ordinary_terms_strict_primary_exact"] for item in values),
                "extra_segmented_strict_primary_exact": sum(item["extra_segmented_strict_primary_exact"] for item in values),
                "extra_segmented_incremental_exact": sum(item["extra_segmented_strict_primary_exact"] and not item["ordinary_terms_strict_primary_exact"] for item in values),
                "discontinuous_denominator": sum(not item["surface_is_contiguous"] for item in values)}
    report = {"schema": "sekaisync/p0-fixed-window-task-wide-control-score@1", "story_key": reference["story_key"], "language": reference["language"],
        "reference_sha256": reference["reference_sha256"], "input_sha256": hashes, "machine_reference_not_human_gold": True,
        "postfreeze_variants_accepted": False, "raw_case_preserved": True, "later_48_windows_excluded": True,
        "scoring_rule": "Raw-case-preserving exact selected primary segment vectors only within the same first24 source windows. Ordinary task-wide term support and extra segmented observations are separate.",
        "limitation": "Ordinary terms are task-wide types with no submitted occurrence/evidence location. Focused raw support does not prove the host selected the annotated occurrence, and cannot locate the cognitive origin of a repeated type. Terms lacking first24 support never count.",
        "causal_workflow_improvement_proven": False, "semantic_precision": None, "exhaustive_vocabulary_recall": None,
        "control_task_windows": len(original_rows), "evaluated_source_windows": len(rows), "control_term_entries": len(control["terms"]),
        "control_distinct_term_types": len(set(control["terms"])), "focused_grounded_term_types": len(first_types),
        "later_window_only_term_types": sorted(all_types - first_types), "unresolved_raw_term_types": sorted(set(control["terms"]) - all_types),
        "control_extra_segmented_observations": len(control["segmented_observations"]), "focused_extra_segmented_groups": len(extra),
        "whole_batch_extra_segmented_groups": len(all_extra), "focused_segmented_diagnostics": extra_diagnostics,
        "whole_batch_segmented_diagnostics": all_extra_diagnostics, "summary": counts(assessments),
        "per_category": {category: counts([item for item in assessments if category in item["categories"]])
                         for category in sorted({category for item in assessments for category in item["categories"]})},
        "assessments": assessments, "script_sha256": digest(Path(__file__).read_bytes())}
    if any(digest(path.read_bytes()) != hashes[key] for key, path in paths.items()):
        raise ValueError("Frozen control input changed during evaluation")
    frozen_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--original-source", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-control-sha256", required=True)
    args = parser.parse_args()
    report = score(args.census, args.tasks, args.reference, args.control, args.original_source, args.out, args.expected_control_sha256)
    print(json.dumps({key: value for key, value in report.items() if key not in {"assessments", "later_window_only_term_types"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
