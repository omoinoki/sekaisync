"""Score frozen blind target relation types and raw primary span vectors."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.prepare_scraper_target_pilot import RELATIONS, TARGETS
from scripts.evaluate_scraper_focused_source import canonical, digest
from scripts.evaluate_scraper_span_source import frozen_json, signature


def validate_segments(view: dict, parts: list[dict], relation: str) -> None:
    if not isinstance(parts, list) or bool(parts) != (relation not in {"omitted", "unresolved"}):
        raise ValueError("Target claim relation and selected-span presence differ")
    previous = view["start"]
    speaker_end = view["start"] + view["text"].find("\uff1a")
    for part in parts:
        if not isinstance(part, dict) or set(part) != {"start", "end", "exact"}:
            raise ValueError("Target claim needs exact raw segment fields")
        start, end, exact = part["start"], part["end"], part["exact"]
        if (type(start) is not int or type(end) is not int or start < previous or end <= start
                or end > view["end"] or start <= speaker_end or not isinstance(exact, str)
                or view["text"][start - view["start"]:end - view["start"]] != exact):
            raise ValueError("Target claim raw segment is shifted, fabricated, unordered or outside its fixed context")
        previous = end


def score(packet_path: Path, reference_path: Path, judgments_path: Path, output: Path, expected_judgments: str) -> dict:
    paths = {"blind_packet": packet_path, "reference": reference_path, "host_judgments": judgments_path}
    raw = {key: path.read_bytes() for key, path in paths.items()}
    hashes = {key: digest(value) for key, value in raw.items()}
    if hashes["host_judgments"] != expected_judgments:
        raise ValueError("Frozen host target judgment hash mismatch")
    packet, reference, proposal = (json.loads(raw[key]) for key in ("blind_packet", "reference", "host_judgments"))
    if (canonical(packet, "packet_sha256") != packet["packet_sha256"]
            or canonical(reference, "reference_sha256") != reference["reference_sha256"]):
        raise ValueError("Frozen blind packet or independent target reference hash mismatch")
    if (reference.get("host_target_candidates_or_judgments_read") is not False
            or reference["blind_packet_file_sha256"] != hashes["blind_packet"]
            or reference["blind_packet_sha256"] != packet["packet_sha256"]
            or reference["relation_rubric"] != packet["relation_rubric"]):
        raise ValueError("Independent target reference provenance or rubric mismatch")
    if isinstance(proposal, dict):
        judgments = proposal.get("records", proposal.get("judgments"))
        if proposal.get("packet_sha256", packet["packet_sha256"]) != packet["packet_sha256"]:
            raise ValueError("Host target judgment packet identity differs")
        if proposal.get("target_reference_read", False) is not False:
            raise ValueError("Host target judgment blindness declaration differs")
    else:
        judgments = proposal
    required = {(item["source_annotation_id"], language) for item in packet["items"] for language in TARGETS}
    if len(required) != 48 or len(judgments) != 48:
        raise ValueError("Target pilot requires all 48 unchanged directed records")
    gold = {(item["source_annotation_id"], item["target_language"]): item for item in reference["annotations"]}
    items = {item["source_annotation_id"]: item for item in packet["items"]}
    seen, assessments = set(), []
    for judgment in judgments:
        if set(judgment) != {"source_annotation_id", "target_language", "relation_type", "target_segments", "reason"}:
            raise ValueError("Host target judgment contract fields differ")
        key = judgment["source_annotation_id"], judgment["target_language"]
        relation = judgment["relation_type"]
        if key in seen or key not in required or relation not in RELATIONS or not judgment["reason"]:
            raise ValueError("Unknown, duplicate or untyped target requirement")
        seen.add(key)
        view = items[key[0]]["target_contexts"][key[1]]
        validate_segments(view, judgment["target_segments"], relation)
        label = gold[key]
        validate_segments(view, label["target_segments"], label["relation_type"])
        type_agrees = relation == label["relation_type"]
        span_agrees = signature(judgment["target_segments"]) == signature(label["target_segments"])
        positive = relation not in {"omitted", "unresolved"}
        gold_positive = label["relation_type"] not in {"omitted", "unresolved"}
        assessments.append({"source_annotation_id": key[0], "target_language": key[1],
            "source_surface": items[key[0]]["source_surface"], "reference_relation_type": label["relation_type"],
            "host_relation_type": relation, "reference_target_segments": label["target_segments"],
            "host_target_segments": judgment["target_segments"], "raw_target_segments_valid": True,
            "relation_type_agreement": type_agrees, "strict_primary_span_agreement": span_agrees,
            "typed_direction_strict_agreement": type_agrees and span_agrees,
            "reference_positive": gold_positive, "host_positive": positive,
            "machine_reference_lexical_upgrade": relation == "lexical" and label["relation_type"] != "lexical",
            "machine_reference_omitted_false_accept": label["relation_type"] == "omitted" and positive,
            "machine_reference_unresolved_false_accept": label["relation_type"] == "unresolved" and positive,
            "reference_reason": label["reason"], "host_reason": judgment["reason"]})
    if seen != required or set(gold) != required:
        raise ValueError("Target reference or judgment denominator differs")
    def counts(values):
        return {"directed_requirements": len(values), "raw_target_segments_valid": len(values),
            "relation_type_agreement": sum(item["relation_type_agreement"] for item in values),
            "strict_primary_span_agreement": sum(item["strict_primary_span_agreement"] for item in values),
            "typed_direction_strict_agreement": sum(item["typed_direction_strict_agreement"] for item in values),
            "reference_positive": sum(item["reference_positive"] for item in values),
            "host_positive": sum(item["host_positive"] for item in values),
            "positive_strict_primary_span_agreement": sum(item["reference_positive"] and item["strict_primary_span_agreement"] for item in values),
            "machine_reference_lexical_upgrades": sum(item["machine_reference_lexical_upgrade"] for item in values),
            "machine_reference_omitted_false_accepts": sum(item["machine_reference_omitted_false_accept"] for item in values),
            "machine_reference_unresolved_false_accepts": sum(item["machine_reference_unresolved_false_accept"] for item in values)}
    report = {"schema": "sekaisync/p0-independent-blind-target-pilot-score@1", "story_key": packet["story_key"],
        "source_units": 12, "directed_requirements": 48, "input_sha256": hashes,
        "reference_sha256": reference["reference_sha256"], "machine_reference_not_human_gold": True,
        "postfreeze_variants_accepted": False, "scoring_rule": "Relation-class agreement and exact raw selected primary span-vector agreement are separate. Raw validity alone never establishes semantic correctness. Omitted/unresolved positive claims and lexical upgrades are explicit machine-reference diagnostics.",
        "reference_relation_counts": dict(Counter(item["relation_type"] for item in reference["annotations"])),
        "host_relation_counts": dict(Counter(item["relation_type"] for item in judgments)),
        "relation_confusion": dict(Counter(item["reference_relation_type"] + "->" + item["host_relation_type"] for item in assessments)),
        "summary": counts(assessments), "per_language": {language: counts([item for item in assessments if item["target_language"] == language]) for language in TARGETS},
        "all_four_target_types_agree_source_units": sum(all(item["relation_type_agreement"] for item in assessments if item["source_annotation_id"] == identity) for identity in items),
        "all_four_typed_directions_strict_agree_source_units": sum(all(item["typed_direction_strict_agreement"] for item in assessments if item["source_annotation_id"] == identity) for identity in items),
        "human_semantic_precision": None, "global_counterpart_recall": None,
        "limitation": "Both references and proposals are machine judgments. A type or strict boundary disagreement is not automatically a demonstrated translation error. This fixed 12-source/48-direction pilot does not prove global zero errors or exhaustive five-language coverage.",
        "assessments": assessments, "script_sha256": digest(Path(__file__).read_bytes())}
    if any(digest(path.read_bytes()) != hashes[key] for key, path in paths.items()):
        raise ValueError("Frozen target scoring input changed during evaluation")
    frozen_json(output, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-judgments-sha256", required=True)
    args = parser.parse_args()
    result = score(args.packet, args.reference, args.judgments, args.out, args.expected_judgments_sha256)
    print(json.dumps({key: value for key, value in result.items() if key != "assessments"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
