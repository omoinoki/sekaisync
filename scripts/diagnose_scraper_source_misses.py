"""Diagnose frozen source misses without changing the blind score."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.census_scraper_corpus import normalize_language
from scripts.evaluate_scraper_focused_source import digest
from scripts.evaluate_scraper_span_source import frozen_json


def analyze(score: dict, tasks: list[dict]) -> dict:
    rows_by_language = {}
    for task in tasks:
        language = normalize_language(task["language"])
        rows_by_language.setdefault(language, []).extend(task["review_context"]["rows"])
    details = []
    for annotation in score["assessments"]:
        parts = annotation["primary_segments"]
        windows = rows_by_language.get(annotation["language"], [])
        selected = {index for part in parts for index in range(part["start"], part["end"])}
        covering = [row["id"] for row in windows if all(row["source"]["start"] <= part["start"]
                    and part["end"] <= row["source"]["end"] for part in parts)]
        covered = {index for row in windows for index in range(row["source"]["start"], row["source"]["end"])}
        if any(not any(row["source"]["start"] <= part["start"] and part["end"] <= row["source"]["end"]
                       and row["source"]["text"][part["start"] - row["source"]["start"]:part["end"] - row["source"]["start"]] == part["exact"]
                       for row in windows) for part in parts):
            raise ValueError("Selected source span is not reproducible in its original raw packet windows")
        missing = not (annotation["pure_terms_strict_primary_exact"] or annotation["typed_subjects_strict_primary_exact"])
        fragments, envelopes = annotation["diagnostic_fragments"], annotation["diagnostic_overbroad_containment"]
        tags = []
        if missing:
            if annotation["discontinuous"]:
                tags.append("genuine_discontinuity_missing")
            if fragments:
                tags.append("narrower_source_fragments_but_no_selected_full_unit")
            if envelopes:
                tags.append("wider_literal_boundary_instead_of_selected_unit")
            if not fragments and not envelopes:
                tags.append("no_narrower_or_wider_literal_support")
            if "compound" in annotation["categories"]:
                tags.append("compound_or_modified_full_phrase_missing")
            if "predicate" in annotation["categories"]:
                tags.append("complete_predicate_unit_missing")
            if "negative_polarity" in annotation["categories"]:
                tags.append("polarity_bearing_unit_missing")
            if "inflected_verb" in annotation["categories"]:
                tags.append("inflection_tagged_reference_missing_not_proven_morphology_cause")
            if "fixed_expression" in annotation["categories"]:
                tags.append("fixed_expression_reference_missing")
        fragment_positions = {index for fragment in fragments for part in fragment["segments"]
                              for index in range(part["start"], part["end"])}
        uncovered = []
        for part in parts:
            current = ""
            for index in range(part["start"], part["end"]):
                character = part["exact"][index - part["start"]]
                if index not in fragment_positions and not character.isspace():
                    current += character
                elif current:
                    uncovered.append(current)
                    current = ""
            if current:
                uncovered.append(current)
        if annotation["discontinuous"]:
            primary = "genuine_discontinuity"
        elif envelopes:
            primary = "wider_contiguous_boundary"
        elif fragments:
            primary = "narrower_fragment_or_wordhead_only"
        else:
            primary = "unproposed_literal_unit"
        details.append({"annotation_id": annotation["annotation_id"], "language": annotation["language"],
            "reference_surface": annotation["reference_surface"], "missing": missing,
            "primary_structural_class_if_missing": primary if missing else None, "overlapping_tags": tags,
            "genuine_discontinuity": annotation["discontinuous"], "in_original_single_window": bool(covering),
            "original_window_ids": covering, "selected_characters_covered_by_packet_union": selected <= covered,
            "narrower_literal_surfaces": [item["surface"] for item in fragments],
            "wider_literal_surfaces": [item["surface"] for item in envelopes],
            "selected_nonwhitespace_text_not_covered_by_narrow_fragments": uncovered if missing else [],
            "single_character_reference": sum(len(part["exact"]) for part in parts) == 1})
    missing = [item for item in details if item["missing"]]
    return {"reference_source_units": len(details), "strict_missing_source_units": len(missing),
        "selected_spans_in_one_provided_window": sum(item["in_original_single_window"] for item in details),
        "selected_spans_covered_by_window_union": sum(item["selected_characters_covered_by_packet_union"] for item in details),
        "missing_units_outside_provided_windows": sum(not item["selected_characters_covered_by_packet_union"] for item in missing),
        "single_character_reference_denominator": sum(item["single_character_reference"] for item in details),
        "single_character_reference_misses": sum(item["single_character_reference"] for item in missing),
        "primary_structural_counts": dict(Counter(item["primary_structural_class_if_missing"] for item in missing)),
        "overlapping_tag_counts": dict(Counter(tag for item in missing for tag in item["overlapping_tags"])),
        "narrower_and_wider_support_overlap": sum(bool(item["narrower_literal_surfaces"]) and bool(item["wider_literal_surfaces"]) for item in missing),
        "unread_host_cognition_measured": False, "synthetic_lemma_erasure_as_cause_measured": False,
        "function_word_recall_separately_measured": False,
        "interpretation": "Primary structural classes partition misses; semantic/morphology tags overlap and are not causal attributions. Window inclusion measures exposure, not host reading. The fixed reference has no standalone single-character denominator, so it cannot measure single-character vocabulary recall. Short joiners or particles in fragment gaps are evidence of missing complete units, not a new function-word recall denominator.",
        "details": details}


def diagnose(score_path: Path, tasks_path: Path, output: Path) -> dict:
    score_raw, tasks_raw = score_path.read_bytes(), tasks_path.read_bytes()
    score, tasks = json.loads(score_raw), json.loads(tasks_raw)
    if score["input_sha256"]["original_tasks"] != digest(tasks_raw):
        raise ValueError("Frozen source score task snapshot hash mismatch")
    report = {"schema": "sekaisync/p0-frozen-source-miss-diagnostic@1", "story_key": score["story_key"],
        "frozen_source_score_sha256": digest(score_raw), "frozen_original_tasks_sha256": digest(tasks_raw),
        "reference_sha256": score["reference_sha256"], "original_score_or_proposals_changed": False,
        "postfreeze_variants_accepted": False, **analyze(score, tasks), "script_sha256": digest(Path(__file__).read_bytes())}
    if score_path.read_bytes() != score_raw or tasks_path.read_bytes() != tasks_raw:
        raise ValueError("Source diagnostic input changed during analysis")
    frozen_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--score", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = diagnose(args.score, args.tasks, args.out)
    print(json.dumps({key: value for key, value in report.items() if key != "details"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
