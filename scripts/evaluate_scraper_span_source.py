"""Strict independent source-expression scoring with separate typed subjects."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap
from sekaisync import agent_review as ar
from sekaisync import span_subjects
from scripts.census_scraper_corpus import LANGUAGES, normalize_language


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def signature(parts: list[dict]) -> tuple:
    return tuple((part["start"], part["end"], part["exact"]) for part in parts)


def frozen_json(path: Path, value) -> None:
    if path.exists():
        if json.loads(path.read_bytes()) != value:
            raise ValueError("Frozen scoring output differs: " + str(path))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def snapshot_tasks(store: Path, judgments_path: Path, path: Path) -> Path:
    judgments = json.loads(judgments_path.read_bytes())
    identities = [row["id"] for row in judgments]
    if len(identities) != len(set(identities)):
        raise ValueError("Duplicate discovery judgment task")
    if path.exists():
        existing = json.loads(path.read_bytes())
        if [row["id"] for row in existing] != identities:
            raise ValueError("Frozen original task snapshot identities differ")
        return path
    queue = {task.id: task.to_dict() for task in ar.load_queue(store)}
    if not set(identities) <= set(queue):
        raise ValueError("Original discovery tasks missing from isolated queue")
    frozen_json(path, [queue[identity] for identity in identities])
    return path


def score(census: Path, reference_path: Path, proposal_path: Path, judgments_path: Path,
          tasks_path: Path, output_path: Path, expected_source_sha256: str,
          expected_judgments_sha256: str, expected_task_terms: int | None = None,
          expected_typed_subjects: int | None = None) -> dict:
    files = {"reference": reference_path, "source_proposal": proposal_path, "judgments": judgments_path,
             "original_tasks": tasks_path, "manifest": census / "holdout-manifest.json"}
    buffers = {key: path.read_bytes() for key, path in files.items()}
    hashes = {key: digest(raw) for key, raw in buffers.items()}
    if hashes["source_proposal"] != expected_source_sha256 or hashes["judgments"] != expected_judgments_sha256:
        raise ValueError("Frozen source proposal or strict judgment hash mismatch")
    values = {key: json.loads(raw) for key, raw in buffers.items()}
    reference, proposal, manifest = values["reference"], values["source_proposal"], values["manifest"]
    for value, field in ((reference, "reference_sha256"), (manifest, "manifest_sha256")):
        unsigned = {key: item for key, item in value.items() if key != field}
        if digest(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()) != value[field]:
            raise ValueError("Frozen reference or manifest canonical hash mismatch")
    if reference.get("schema") != "sekaisync/p0-independent-machine-span-reference@2":
        raise ValueError("Strict scorer requires the frozen independent span-v2 reference")
    if reference.get("proposal_or_tokenizer_candidates_read") is not False or reference.get("machine_reference_not_human_gold") is not True:
        raise ValueError("Reference independence declaration missing")
    if proposal["story_key"] != reference["story_key"] or reference["holdout_manifest_sha256"] != manifest["manifest_sha256"]:
        raise ValueError("Story or holdout snapshot mismatch")
    story = next(row for row in manifest["stories"] if row["story_key"] == reference["story_key"])
    texts = {}
    for language in LANGUAGES:
        page = story["pages"][language]
        path = (census / page["local_body_file"]).resolve()
        if not path.is_relative_to(census.resolve()):
            raise ValueError("Frozen body path escapes census directory")
        raw = path.read_bytes()
        if digest(raw) != page["text_sha256"]:
            raise ValueError("Frozen raw body hash mismatch")
        texts[language] = raw.decode("utf-8")
    if set(proposal["surfaces"]) != set(LANGUAGES):
        raise ValueError("Source proposal must retain all five languages")
    terms, typed, task_counts = {}, {language: [] for language in LANGUAGES}, Counter()
    for language, surfaces in proposal["surfaces"].items():
        if not isinstance(surfaces, list) or any(not isinstance(value, str) or not value for value in surfaces):
            raise ValueError("Source table needs literal surfaces per language")
        terms[language] = []
        for surface in dict.fromkeys(surfaces):
            spans = ap._body_term_segments(texts[language], surface)
            if not spans:
                raise ValueError("Frozen source proposal contains an ungrounded literal")
            terms[language].extend({"surface": surface, "segments": parts} for parts in spans)
    tasks = {row["id"]: row for row in values["original_tasks"]}
    judgments = values["judgments"]
    if len(tasks) != len(values["original_tasks"]) or len({row["id"] for row in judgments}) != len(judgments) or set(tasks) != {row["id"] for row in judgments}:
        raise ValueError("Discovery judgment and original task identities differ")
    seen_subjects, submitted_terms = set(), {language: set() for language in LANGUAGES}
    for judgment in judgments:
        task = tasks[judgment["id"]]
        language = normalize_language(task["language"])
        if language not in LANGUAGES or judgment.get("decision") != "accept" or task["review_context"].get("task") != "discovery":
            raise ValueError("Strict discovery judgment task/decision mismatch")
        task_counts[language] += len(judgment.get("terms", []))
        submitted_terms[language].update(judgment.get("terms", []))
        rows = {row["id"]: row for row in task["review_context"]["rows"]}
        for entry in judgment.get("subjects", []):
            kind = entry.get("kind")
            keys = {"kind", "evidence_id", "segments"} | ({"canonical"} if kind == "literal" else set())
            if set(entry) != keys or kind not in {"literal", "segmented"} or entry["evidence_id"] not in rows:
                raise ValueError("Typed subject must cite the existing exact source evidence")
            row, page = rows[entry["evidence_id"]], story["pages"][language]
            view = row["source"]
            if (row["story_key"] != reference["story_key"] or view["page_id"] != page["page_id"]
                    or view["source"] != page["source"] or view["sha256"] != page["text_sha256"]
                    or normalize_language(view["language"]) != language
                    or texts[language][view["start"]:view["end"]] != view["text"]):
                raise ValueError("Typed subject packet raw-page identity mismatch")
            subject = (span_subjects._literal(view, row["story_key"], entry["canonical"], entry["segments"])
                       if kind == "literal" else span_subjects._segmented(view, row["story_key"], entry["segments"]))
            if subject["id"] in seen_subjects:
                raise ValueError("Duplicate typed subject submission")
            seen_subjects.add(subject["id"])
            typed[language].append({"task_id": judgment["id"], "evidence_id": entry["evidence_id"],
                                    "subject_id": subject["id"], "kind": kind, "segments": subject["source"]["segments"]})
    if any(submitted_terms[language] != set(proposal["surfaces"][language]) for language in LANGUAGES):
        raise ValueError("Source table does not equal the frozen submitted literal terms")
    if expected_task_terms is not None and sum(task_counts.values()) != expected_task_terms:
        raise ValueError("Frozen task-term denominator changed")
    if expected_typed_subjects is not None and len(seen_subjects) != expected_typed_subjects:
        raise ValueError("Frozen typed-subject denominator changed")
    annotations = reference["annotations"]
    if len({row["id"] for row in annotations}) != len(annotations) or set(row["language"] for row in annotations) != set(LANGUAGES):
        raise ValueError("Invalid independent annotation denominator")
    assessments = []
    for annotation in annotations:
        language = annotation["language"]
        primary = annotation["span_groups"][annotation["primary_occurrence_index"]]
        parts = primary["segments"]
        if any(texts[language][part["start"]:part["end"]] != part["exact"] for part in parts):
            raise ValueError("Independent reference selected segment is not reproducible")
        target = signature(parts)
        exact_terms = sorted({row["surface"] for row in terms[language] if signature(row["segments"]) == target})
        exact_typed = [row["subject_id"] for row in typed[language] if signature(row["segments"]) == target]
        selected = {index for part in parts for index in range(part["start"], part["end"])}
        mandatory = {index for index in selected if not texts[language][index].isspace()}
        covered, fragments, overbroad = set(), [], []
        for candidate in terms[language]:
            positions = {index for part in candidate["segments"] for index in range(part["start"], part["end"])}
            if positions and positions <= selected:
                covered |= positions
                if positions != selected:
                    fragments.append(candidate)
            if mandatory <= positions and positions - selected:
                overbroad.append(candidate)
        assessments.append({"annotation_id": annotation["id"], "concept_id": annotation["concept_id"], "language": language,
                            "reference_surface": annotation["surface"], "primary_segments": parts,
                            "discontinuous": not annotation["surface_is_contiguous"], "categories": annotation["categories"],
                            "pure_terms_strict_primary_exact": bool(exact_terms), "matching_literal_surfaces": exact_terms,
                            "typed_subjects_strict_primary_exact": bool(exact_typed), "matching_typed_subject_ids": exact_typed,
                            "typed_incremental_exact": bool(exact_typed and not exact_terms),
                            "diagnostic_fragment_union_complete": mandatory <= covered,
                            "diagnostic_fragments": fragments, "diagnostic_overbroad_containment": overbroad})
    def counts(rows):
        return {"reference_source_units": len(rows),
                "pure_terms_strict_primary_exact": sum(row["pure_terms_strict_primary_exact"] for row in rows),
                "typed_subjects_strict_primary_exact": sum(row["typed_subjects_strict_primary_exact"] for row in rows),
                "typed_incremental_exact": sum(row["typed_incremental_exact"] for row in rows),
                "discontinuous_denominator": sum(row["discontinuous"] for row in rows),
                "pure_terms_discontinuous_strict_exact": sum(row["discontinuous"] and row["pure_terms_strict_primary_exact"] for row in rows),
                "typed_discontinuous_strict_exact": sum(row["discontinuous"] and row["typed_subjects_strict_primary_exact"] for row in rows),
                "diagnostic_fragment_union_only": sum(not row["pure_terms_strict_primary_exact"] and row["diagnostic_fragment_union_complete"] for row in rows),
                "diagnostic_overbroad_only": sum(not row["pure_terms_strict_primary_exact"] and bool(row["diagnostic_overbroad_containment"]) for row in rows)}
    report = {"schema": "sekaisync/independent-machine-strict-source-and-typed-evaluation@1",
              "story_key": reference["story_key"], "input_sha256": hashes, "reference_sha256": reference["reference_sha256"],
              "machine_reference_not_human_gold": True, "reference_and_host_proposal_unchanged": True,
              "postfreeze_variants_accepted": False, "semantic_precision": None, "exhaustive_vocabulary_recall": None,
              "scoring_rule": "Exact selected primary raw segment vector (start,end,exact). Literal terms and typed observations are scored separately. Fragment unions, enclosing spans, lemma guesses and later variants never count as hits.",
              "host_task_term_count": sum(task_counts.values()),
              "host_surface_table_records": sum(len(values) for values in proposal["surfaces"].values()),
              "host_distinct_literal_surface_types": sum(len(set(values)) for values in proposal["surfaces"].values()),
              "host_typed_observation_count": len(seen_subjects), "summary": counts(assessments),
              "per_language": {language: dict(counts([row for row in assessments if row["language"] == language]),
                   task_terms=task_counts[language], typed_observations=len(typed[language])) for language in LANGUAGES},
              "typed_observations": [row for rows in typed.values() for row in rows], "assessments": assessments,
              "script_sha256": digest(Path(__file__).read_bytes())}
    if any(digest(path.read_bytes()) != hashes[key] for key, path in files.items()):
        raise ValueError("Frozen scoring input changed during evaluation")
    frozen_json(output_path, report)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census")
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--source-proposal", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--tasks", type=Path)
    parser.add_argument("--task-store", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--expected-source-sha256", required=True)
    parser.add_argument("--expected-judgments-sha256", required=True)
    parser.add_argument("--expected-task-term-count", type=int)
    parser.add_argument("--expected-typed-subject-count", type=int)
    args = parser.parse_args()
    if not args.tasks:
        if not args.task_store:
            parser.error("--tasks or --task-store is required")
        args.tasks = snapshot_tasks(args.task_store, args.judgments, args.out.parent / "source-evaluation-tasks.json")
    report = score(args.census, args.reference, args.source_proposal, args.judgments, args.tasks, args.out,
                   args.expected_source_sha256, args.expected_judgments_sha256,
                   args.expected_task_term_count, args.expected_typed_subject_count)
    print(json.dumps({key: value for key, value in report.items() if key not in {"assessments", "typed_observations"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
