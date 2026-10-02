"""Prepare blind target packets and independently freeze typed references."""
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
from scripts.evaluate_scraper_focused_source import canonical, digest, source_inputs
from scripts.evaluate_scraper_span_source import frozen_json

TARGETS = ("ja", "zh_hans", "zh_hant", "ko")
RELATIONS = {"lexical": "A directly rendered lexical or productive-expression counterpart at the selected source meaning; ordinary grammatical inflection and unmarked number are not a semantic reframing.",
    "paraphrase": "The local utterance preserves the selected communicative content or intended care action using a changed framing or implicit source feature, rather than a context-free lexical synonym.",
    "reference": "A context-bound referent match through pronoun/entity resolution; selected source and target denotations agree only in this local context.",
    "omitted": "The selected source meaning has no supported target expression in the fixed local context, including a non-equivalent localized substitution. Do not force a same-position phrase to be its equivalent.",
    "unresolved": "The fixed context cannot support a defensible typed mapping because of a divergent or ambiguous proposition; preserve this item as unresolved instead of manufacturing an equivalence."}


def prepare(census: Path, tasks_path: Path, reference_path: Path, selection_path: Path, output: Path) -> dict:
    reference_raw, selection_raw = reference_path.read_bytes(), selection_path.read_bytes()
    reference, selection = json.loads(reference_raw), json.loads(selection_raw)
    if canonical(reference, "reference_sha256") != reference["reference_sha256"]:
        raise ValueError("Frozen source reference hash mismatch")
    if selection.get("selection_basis") != "frozen_source_reference_only_not_host_or_control_candidates":
        raise ValueError("Target pilot source selection must not follow host candidates")
    manifest, tasks, rows, source_page, body, hashes = source_inputs(census, tasks_path, reference["story_key"], "en")
    if hashes != reference["input_sha256"]:
        raise ValueError("Frozen source reference input drift")
    ids = selection["source_annotation_ids"]
    if len(ids) != 12 or len(set(ids)) != 12:
        raise ValueError("Target pilot requires exactly 12 fixed source units")
    by_id = {row["id"]: row for row in reference["annotations"]}
    story = next(row for row in manifest["stories"] if row["story_key"] == reference["story_key"])
    bodies = {}
    for language in TARGETS:
        page = story["pages"][language]
        raw = (census / page["local_body_file"]).read_bytes()
        if digest(raw) != page["text_sha256"]:
            raise ValueError("Frozen target body hash mismatch")
        bodies[language] = raw.decode("utf-8")
    items = []
    for identity in ids:
        annotation = by_id[identity]
        row = rows[annotation["row_index"]]
        targets = {}
        for key, view in row["targets"].items():
            language = normalize_language(view["language"])
            page = story["pages"][language]
            if (view["page_id"] != page["page_id"] or view["source"] != page["source"]
                    or view["sha256"] != page["text_sha256"] or bodies[language][view["start"]:view["end"]] != view["text"]):
                raise ValueError("Blind target context raw-page mismatch")
            targets[language] = view
        if set(targets) != set(TARGETS):
            raise ValueError("Blind target context language denominator differs")
        items.append({"source_annotation_id": identity, "source_surface": annotation["surface"],
            "source_meaning": annotation["meaning"], "source_categories": annotation["categories"],
            "source_segments": annotation["primary_segments"], "source_evidence_id": row["id"],
            "source_context": row["source"], "target_contexts": targets})
    packet = {"schema": "sekaisync/p0-fixed-source-blind-target-pilot@1", "story_key": reference["story_key"],
        "source_language": "en", "target_languages": list(TARGETS), "source_units": 12, "directed_requirements": 48,
        "source_reference_sha256": reference["reference_sha256"], "input_sha256": dict(hashes,
            source_reference_file=digest(reference_raw), source_selection=digest(selection_raw)),
        "target_reference_or_host_proposal_read": False, "target_spans_or_labels_in_packet": False,
        "context_policy": "Use only these complete, exact raw target windows for this fixed pilot. Source meanings identify the selected unit, not a preferred target relation or span. Do not borrow a neighboring EN expression's target to force equivalence.",
        "relation_rubric": RELATIONS,
        "judgment_contract": {"schema": "sekaisync/p0-blind-target-pilot-judgments@1", "record_fields": [
            "source_annotation_id", "target_language", "relation_type", "target_segments", "reason"],
            "target_segment_fields": ["start", "end", "exact"], "offsets": "Absolute Python Unicode code points in the hashed raw target page, end-exclusive.",
            "empty_target_segments_required_for": ["omitted", "unresolved"],
            "literal_vs_discontinuous": "Keep genuine selected parts separate; do not replace them with a broad envelope or fabricate a concatenated literal.",
            "all_48_records_required": True}, "items": items}
    packet["packet_sha256"] = canonical(packet, "packet_sha256")
    frozen_json(output, packet)
    return {"packet": str(output.resolve()), "packet_sha256": packet["packet_sha256"],
        "packet_file_sha256": digest(output.read_bytes()), "source_units": 12, "directed_requirements": 48}


def freeze(packet_path: Path, selection_path: Path, output: Path) -> dict:
    packet_raw, selection_raw = packet_path.read_bytes(), selection_path.read_bytes()
    packet, selection = json.loads(packet_raw), json.loads(selection_raw)
    if canonical(packet, "packet_sha256") != packet["packet_sha256"]:
        raise ValueError("Blind target packet hash mismatch")
    if selection.get("host_target_candidates_or_judgments_read") is not False or selection.get("machine_reference_not_human_gold") is not True:
        raise ValueError("Target reference must be independent machine-only selection")
    by_source = {row["source_annotation_id"]: row for row in packet["items"]}
    seen, annotations = set(), []
    for item in selection["relations"]:
        identity, language, relation = item["source_annotation_id"], item["target_language"], item["relation_type"]
        key = identity, language
        if key in seen or identity not in by_source or language not in TARGETS or relation not in RELATIONS:
            raise ValueError("Invalid or duplicate independent target requirement")
        seen.add(key)
        view = by_source[identity]["target_contexts"][language]
        fragments = item.get("fragments", [])
        if bool(fragments) != (relation not in {"omitted", "unresolved"}):
            raise ValueError("Unsupported or omitted target mappings need no selected spans")
        parts = []
        for fragment in fragments:
            exact, occurrence = fragment["exact"], fragment.get("occurrence", 0)
            if not isinstance(exact, str) or not exact or type(occurrence) is not int or occurrence < 0:
                raise ValueError("Target fragments must be nonempty literal text with valid occurrence indexes")
            offsets, start = [], 0
            while (found := view["text"].find(exact, start)) >= 0:
                offsets.append(found)
                start = found + len(exact)
            if not 0 <= occurrence < len(offsets):
                raise ValueError("Independent target fragment is absent in exact packet context")
            local = offsets[occurrence]
            if local <= view["text"].find("\uff1a"):
                raise ValueError("Target speaker labels cannot be selected expressions")
            parts.append({"start": view["start"] + local, "end": view["start"] + local + len(exact), "exact": exact})
        if any(left["end"] > right["start"] for left, right in zip(parts, parts[1:])):
            raise ValueError("Independent target fragments overlap or are unordered")
        if not item.get("reason"):
            raise ValueError("Target relation requires a source-meaning-specific rationale")
        annotations.append({"id": identity + ":" + language, "source_annotation_id": identity,
            "target_language": language, "relation_type": relation, "target_segments": parts, "reason": item["reason"],
            "source_segments": by_source[identity]["source_segments"], "source_meaning": by_source[identity]["source_meaning"],
            "target_page_id": view["page_id"], "target_source": view["source"], "target_sha256": view["sha256"],
            "context_start": view["start"], "context_end": view["end"],
            "non_equivalent_localized_substitution": item.get("non_equivalent_localized_substitution", False)})
    if seen != {(identity, language) for identity in by_source for language in TARGETS}:
        raise ValueError("Independent target reference must preserve all 48 directed requirements")
    reference = {"schema": "sekaisync/p0-independent-typed-target-pilot-reference@1", "story_key": packet["story_key"],
        "reviewer": selection["reviewer"], "machine_reference_not_human_gold": True,
        "host_target_candidates_or_judgments_read": False, "source_units": 12, "directed_requirements": 48,
        "blind_packet_file_sha256": digest(packet_raw), "blind_packet_sha256": packet["packet_sha256"],
        "selection_file_sha256": digest(selection_raw), "relation_rubric": RELATIONS, "annotations": annotations}
    reference["reference_sha256"] = canonical(reference, "reference_sha256")
    frozen_json(output, reference)
    report = {"reference": str(output.resolve()), "reference_sha256": reference["reference_sha256"],
        "reference_file_sha256": digest(output.read_bytes()), "directed_requirements": 48,
        "relation_counts": dict(Counter(row["relation_type"] for row in annotations)),
        "non_contiguous_target_observations": sum(len(row["target_segments"]) > 1 for row in annotations),
        "host_target_candidates_or_judgments_read": False}
    frozen_json(output.parent / "target-freeze-report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census")
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--source-reference", type=Path)
    parser.add_argument("--packet", type=Path)
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = (freeze(args.packet, args.selection, args.out) if args.packet else
              prepare(args.census, args.tasks, args.source_reference, args.selection, args.out))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
