"""Freeze and score independent fixed-window source-expression references."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap, span_subjects
from scripts.evaluate_scraper_span_source import frozen_json, signature


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: dict, field: str) -> str:
    return digest(json.dumps({key: item for key, item in value.items() if key != field},
                            ensure_ascii=False, sort_keys=True).encode())


def source_inputs(census: Path, tasks_path: Path, story_key: str, language: str):
    manifest_raw, tasks_raw = (census / "holdout-manifest.json").read_bytes(), tasks_path.read_bytes()
    manifest, tasks = json.loads(manifest_raw), json.loads(tasks_raw)
    if canonical(manifest, "manifest_sha256") != manifest["manifest_sha256"]:
        raise ValueError("Frozen holdout manifest hash mismatch")
    story = next(item for item in manifest["stories"] if item["story_key"] == story_key)
    page = story["pages"][language]
    path = (census / page["local_body_file"]).resolve()
    if not path.is_relative_to(census.resolve()):
        raise ValueError("Focused source body path escapes census")
    body_raw = path.read_bytes()
    if digest(body_raw) != page["text_sha256"]:
        raise ValueError("Focused source body hash mismatch")
    body = body_raw.decode("utf-8")
    rows = []
    if len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("Duplicate focused task identity")
    for task in tasks:
        if task["language"] != language or task["review_context"].get("task") != "discovery":
            raise ValueError("Focused task language or task mismatch")
        for row in task["review_context"]["rows"]:
            view = row["source"]
            if (row["story_key"] != story_key or view["language"] != language or view["page_id"] != page["page_id"]
                    or view["source"] != page["source"] or view["sha256"] != page["text_sha256"]
                    or body[view["start"]:view["end"]] != view["text"]):
                raise ValueError("Focused packet raw-page identity mismatch")
            rows.append(dict(row, focused_task_id=task["id"]))
    if len({row["id"] for row in rows}) != len(rows) or rows != sorted(rows, key=lambda row: row["source"]["start"]):
        raise ValueError("Focused windows must be unique and preserve raw source order")
    hashes = {"manifest": digest(manifest_raw), "original_tasks": digest(tasks_raw), "source_body": digest(body_raw)}
    return manifest, tasks, rows, page, body, hashes


def freeze(census: Path, tasks_path: Path, selection_path: Path) -> dict:
    raw = selection_path.read_bytes()
    selection = json.loads(raw)
    if (selection.get("proposal_or_tokenizer_candidates_read") is not False
            or selection.get("machine_reference_not_human_gold") is not True):
        raise ValueError("Focused reference requires independent candidate-free selection")
    manifest, tasks, rows, page, body, hashes = source_inputs(census, tasks_path, selection["story_key"], selection["language"])
    if len(rows) != selection["source_windows_read"] or len(rows) != 24:
        raise ValueError("Frozen focused-window denominator changed")
    annotations, seen = [], set()
    for index, item in enumerate(selection["units"]):
        row, parts = rows[item["row"]], []
        view = row["source"]
        speaker_end = view["text"].find("\uff1a")
        fragments = item.get("fragments", [item.get("surface")])
        choices = item.get("within_window_occurrences", [0] * len(fragments))
        if len(fragments) != len(choices):
            raise ValueError("Reference fragment occurrence indexes differ")
        for exact, choice in zip(fragments, choices):
            if not isinstance(exact, str) or not exact:
                raise ValueError("Empty focused reference fragment")
            pattern = r"(?<![A-Za-z])" + re.escape(exact) + r"(?![A-Za-z])"
            matches = list(re.finditer(pattern, view["text"]))
            if not 0 <= choice < len(matches):
                raise ValueError("Focused reference surface absent from raw window: " + exact)
            match = matches[choice]
            if match.start() <= speaker_end:
                raise ValueError("Speaker label is not a source-expression reference")
            parts.append({"start": view["start"] + match.start(), "end": view["start"] + match.end(), "exact": exact})
        if any(left["end"] >= right["start"] for left, right in zip(parts, parts[1:])):
            raise ValueError("Non-contiguous reference fragments must be ordered with a genuine gap")
        if signature(parts) in seen:
            raise ValueError("Duplicate focused reference selected span")
        seen.add(signature(parts))
        annotations.append({"id": f"en_unit_{index:03}:en", "language": selection["language"],
            "categories": item["categories"], "meaning": item["meaning"], "surface": " ... ".join(fragments),
            "surface_is_contiguous": len(parts) == 1, "primary_segments": parts, "evidence_id": row["id"],
            "focused_task_id": row["focused_task_id"], "row_index": item["row"], "page_id": page["page_id"],
            "source": page["source"], "text_sha256": page["text_sha256"],
            "enclosing_text": body[parts[0]["start"]:parts[-1]["end"]],
            "gap_texts": [body[left["end"]:right["start"]] for left, right in zip(parts, parts[1:])]})
    if len(annotations) < 40:
        raise ValueError("Focused independent denominator needs at least 40 meaningful units")
    reference = {"schema": "sekaisync/p0-independent-focused-source-reference@1", "reviewer": selection["reviewer"],
        "story_key": selection["story_key"], "language": selection["language"], "scope": selection["scope"],
        "machine_reference_not_human_gold": True, "proposal_or_tokenizer_candidates_read": False,
        "source_windows_read": len(rows), "other_source_windows_read": False,
        "selection_sha256": digest(raw), "input_sha256": hashes, "holdout_manifest_sha256": manifest["manifest_sha256"],
        "annotation_conventions": {"offset_unit": "python_unicode_code_points", "end_exclusive": True,
            "complete_english_word_boundaries": True, "speaker_labels_excluded": True,
            "nested_wordhead_and_phrase_units_retained_separately": True,
            "inflected_source_forms_not_synthetic_lemmas": True,
            "non_contiguous_segments_not_enclosing_text": True}, "annotations": annotations}
    reference["reference_sha256"] = canonical(reference, "reference_sha256")
    destination = selection_path.parent / "reference.json"
    frozen_json(destination, reference)
    report = {"reference_path": str(destination.resolve()), "reference_sha256": reference["reference_sha256"],
        "reference_file_sha256": digest(destination.read_bytes()), "source_windows": len(rows),
        "source_units": len(annotations), "discontinuous_source_units": sum(not row["surface_is_contiguous"] for row in annotations),
        "categories": dict(Counter(category for row in annotations for category in row["categories"])),
        "proposal_or_tokenizer_candidates_read": False, "machine_reference_not_human_gold": True}
    frozen_json(selection_path.parent / "freeze-report.json", report)
    return report


def score(census: Path, tasks_path: Path, reference_path: Path, proposal_path: Path, judgments_path: Path,
          output: Path, expected_proposal: str, expected_judgments: str) -> dict:
    inputs = {"reference": reference_path, "proposal": proposal_path, "judgments": judgments_path, "original_tasks": tasks_path}
    raw = {key: path.read_bytes() for key, path in inputs.items()}
    hashes = {key: digest(value) for key, value in raw.items()}
    if hashes["proposal"] != expected_proposal or hashes["judgments"] != expected_judgments:
        raise ValueError("Frozen focused proposal or judgment hash mismatch")
    reference, judgments = json.loads(raw["reference"]), json.loads(raw["judgments"])
    if canonical(reference, "reference_sha256") != reference["reference_sha256"]:
        raise ValueError("Focused reference canonical hash mismatch")
    if reference.get("proposal_or_tokenizer_candidates_read") is not False:
        raise ValueError("Focused reference independence missing")
    manifest, tasks, rows, page, body, source_hashes = source_inputs(census, tasks_path, reference["story_key"], reference["language"])
    if source_hashes != reference["input_sha256"] or manifest["manifest_sha256"] != reference["holdout_manifest_sha256"]:
        raise ValueError("Focused original source snapshot changed")
    by_task = {task["id"]: task for task in tasks}
    if len({row["id"] for row in judgments}) != len(judgments) or {row["id"] for row in judgments} != set(by_task):
        raise ValueError("Focused judgments must cover exactly the original tasks")
    literal, typed, seen, submitted_terms = [], [], set(), set()
    for judgment in judgments:
        task = by_task[judgment["id"]]
        if judgment.get("decision") != "accept":
            raise ValueError("Focused discovery judgments must be accept submissions")
        row_map = {row["id"]: row for row in task["review_context"]["rows"]}
        for surface in judgment.get("terms", []):
            if not isinstance(surface, str) or not surface or not any(ap._body_term_segments(row["source"]["text"], surface) for row in row_map.values()):
                raise ValueError("Focused submitted literal is not grounded in its original task")
            submitted_terms.add(surface)
        for entry in judgment.get("subjects", []):
            kind = entry.get("kind")
            keys = {"kind", "evidence_id", "segments"} | ({"canonical"} if kind == "literal" else set())
            if set(entry) != keys or kind not in {"literal", "segmented"} or entry["evidence_id"] not in row_map:
                raise ValueError("Focused typed subject must cite exact original task evidence")
            row = row_map[entry["evidence_id"]]
            subject = (span_subjects._literal(row["source"], row["story_key"], entry["canonical"], entry["segments"])
                       if kind == "literal" else span_subjects._segmented(row["source"], row["story_key"], entry["segments"]))
            if subject["id"] in seen:
                raise ValueError("Duplicate focused typed subject")
            seen.add(subject["id"])
            typed.append({"subject_id": subject["id"], "segments": subject["source"]["segments"], "kind": kind})
    for surface in sorted(submitted_terms):
        literal.extend({"surface": surface, "segments": parts} for parts in ap._body_term_segments(body, surface))
    assessments = []
    for annotation in reference["annotations"]:
        parts = annotation["primary_segments"]
        if any(body[part["start"]:part["end"]] != part["exact"] for part in parts):
            raise ValueError("Focused reference raw span changed")
        target = signature(parts)
        pure_matches = sorted({item["surface"] for item in literal if signature(item["segments"]) == target})
        typed_matches = [item["subject_id"] for item in typed if signature(item["segments"]) == target]
        selected = {index for part in parts for index in range(part["start"], part["end"])}
        mandatory = {index for index in selected if not body[index].isspace()}
        fragments, overbroad, covered = [], [], set()
        for item in literal:
            positions = {index for part in item["segments"] for index in range(part["start"], part["end"])}
            if positions and positions <= selected:
                covered |= positions
                if positions != selected:
                    fragments.append(item)
            if mandatory <= positions and positions - selected:
                overbroad.append(item)
        assessments.append(dict(annotation, pure_terms_strict_primary_exact=bool(pure_matches),
            matching_literal_surfaces=pure_matches, typed_subjects_strict_primary_exact=bool(typed_matches),
            matching_typed_subject_ids=typed_matches, typed_incremental_exact=bool(typed_matches and not pure_matches),
            diagnostic_fragment_union_complete=mandatory <= covered, diagnostic_fragments=fragments,
            diagnostic_overbroad_containment=overbroad))
    def counts(values):
        return {"reference_source_units": len(values), "pure_terms_strict_primary_exact": sum(item["pure_terms_strict_primary_exact"] for item in values),
                "typed_subjects_strict_primary_exact": sum(item["typed_subjects_strict_primary_exact"] for item in values),
                "typed_incremental_exact": sum(item["typed_incremental_exact"] for item in values),
                "discontinuous_denominator": sum(not item["surface_is_contiguous"] for item in values),
                "diagnostic_fragment_union_only": sum(not item["pure_terms_strict_primary_exact"] and item["diagnostic_fragment_union_complete"] for item in values),
                "diagnostic_overbroad_only": sum(not item["pure_terms_strict_primary_exact"] and bool(item["diagnostic_overbroad_containment"]) for item in values)}
    report = {"schema": "sekaisync/p0-independent-focused-source-score@1", "story_key": reference["story_key"], "language": reference["language"],
        "input_sha256": hashes, "reference_sha256": reference["reference_sha256"], "machine_reference_not_human_gold": True,
        "reference_and_host_proposal_unchanged": True, "postfreeze_variants_accepted": False,
        "scoring_rule": "Exact selected primary raw (start,end,exact) segment vector. Literal and typed hits separate. Fragment union and envelope containment diagnostic only.",
        "source_windows": len(rows), "host_task_term_entries": sum(len(judgment.get("terms", [])) for judgment in judgments),
        "host_distinct_literal_types": len(submitted_terms), "host_typed_subjects": len(typed), "summary": counts(assessments),
        "per_category": {category: counts([item for item in assessments if category in item["categories"]])
                         for category in sorted({category for item in assessments for category in item["categories"]})},
        "semantic_precision": None, "exhaustive_vocabulary_recall": None, "assessments": assessments,
        "script_sha256": digest(Path(__file__).read_bytes())}
    if any(digest(path.read_bytes()) != hashes[key] for key, path in inputs.items()):
        raise ValueError("Focused score input changed during evaluation")
    frozen_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census")
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--proposal", type=Path)
    parser.add_argument("--judgments", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--expected-proposal-sha256")
    parser.add_argument("--expected-judgments-sha256")
    args = parser.parse_args()
    if args.selection:
        report = freeze(args.census, args.tasks, args.selection)
    else:
        report = score(args.census, args.tasks, args.reference, args.proposal, args.judgments, args.out,
                       args.expected_proposal_sha256, args.expected_judgments_sha256)
    print(json.dumps({key: value for key, value in report.items() if key != "assessments"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
