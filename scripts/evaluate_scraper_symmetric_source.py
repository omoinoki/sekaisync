"""Freeze and score a finite independent five-language source-only selection."""
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

from sekaisync import agent_packets as ap, span_subjects as ss, occurrence_store as oc
from scripts.prepare_scraper_focused_discovery import _entry, _prepare
from scripts.evaluate_scraper_span_source import frozen_json

LANGUAGES = ("ja", "en", "zh_hans", "zh_tw", "ko")
CATEGORIES = ("noun_head", "modified_nominal", "complete_predicate", "function_expression", "discontinuous")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def serialized(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def canonical(value, field):
    return digest(json.dumps({key: item for key, item in value.items() if key != field},
                             ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8"))


def signature(parts):
    return tuple((part["start"], part["end"], part["exact"]) for part in parts)


def _code_hashes():
    paths = [Path(__file__), ROOT / "scripts/prepare_scraper_focused_discovery.py",
             ROOT / "scripts/evaluate_scraper_span_source.py"] + sorted((ROOT / "sekaisync").rglob("*.py"))
    return {str(path.resolve()): digest(path.read_bytes()) for path in paths}


IMPORT_CODE = _code_hashes()


def _read(path, frozen):
    path = Path(path).resolve()
    raw = path.read_bytes()
    if path in frozen and frozen[path] != raw:
        raise ValueError("Symmetric source input changed between reads")
    frozen[path] = raw
    return raw


def _stable(frozen, code):
    if any(path.read_bytes() != raw for path, raw in frozen.items()):
        raise ValueError("Symmetric source input changed during evaluation")
    if _code_hashes() != code:
        raise ValueError("Symmetric source dependency closure changed during evaluation")


def _source_inputs(census, packet_paths, setup_path, story_key, frozen):
    if set(packet_paths) != set(LANGUAGES):
        raise ValueError("Exactly five source-only packet files are required")
    setup_raw = _read(setup_path, frozen)
    setup = json.loads(setup_raw)
    if (setup.get("schema") != "sekaisync/symmetric-source-trial@1" or setup.get("story_key") != story_key
            or setup.get("labels_or_references_read") is not False or setup.get("candidates_in_source_packets") is not False
            or setup.get("target_views_in_source_packets") is not False):
        raise ValueError("Source-only setup identity or isolation declarations changed")
    metrics = {item["language"]: item for item in setup["languages"]}
    if len(metrics) != len(setup["languages"]) or set(metrics) != set(LANGUAGES):
        raise ValueError("Source-only setup requires five unique source languages")
    manifest_raw = _read(Path(census) / "holdout-manifest.json", frozen)
    manifest = json.loads(manifest_raw)
    if canonical(manifest, "manifest_sha256") != manifest.get("manifest_sha256"):
        raise ValueError("Frozen source manifest canonical hash mismatch")
    stories = [story for story in manifest["stories"] if story["story_key"] == story_key]
    if len(stories) != 1:
        raise ValueError("Symmetric source family must be unique in the frozen manifest")
    rows, bodies, hashes = {}, {}, {"manifest": digest(manifest_raw), "setup_report": digest(setup_raw)}
    all_rows = set()
    for language in LANGUAGES:
        metadata = metrics[language]
        page = stories[0]["pages"][metadata["corpus_language"]]
        body_path = (Path(census) / page["local_body_file"]).resolve()
        if not body_path.is_relative_to(Path(census).resolve()):
            raise ValueError("Symmetric source body path escapes census")
        body_raw = _read(body_path, frozen)
        if digest(body_raw) != page["text_sha256"]:
            raise ValueError("Symmetric source body hash mismatch")
        body = body_raw.decode("utf-8")
        packet_path = Path(packet_paths[language]).resolve()
        packet_raw = _read(packet_path, frozen)
        if digest(packet_raw) != setup["artifact_sha256"].get(str(packet_path)):
            raise ValueError("Frozen source-only packet hash mismatch")
        lines = packet_raw.decode("utf-8").splitlines()
        headers = [line for line in lines if line.startswith("## id=")]
        contexts = [json.loads(line.removeprefix("task_context: ")) for line in lines if line.startswith("task_context: ")]
        entries = [json.loads(line.removeprefix("context: ")) for line in lines if line.startswith("context: ")]
        if (headers != [f"## id={metadata['item_id']} kind=discovery"] or len(contexts) != 1
                or serialized(contexts[0]) != serialized(dict(schema=ap._SCHEMA, task="discovery", scope_id=metadata["scope_id"],
                                                              source_language=language))
                or len(entries) != metadata["selected_windows"]):
            raise ValueError("Source-only packet task identity or finite window count mismatch")
        local = []
        source_rows = []
        for row in entries:
            view = row["source"]
            start, end = view["start"], view["end"]
            if (set(row) != {"id", "story_key", "source"}
                    or set(view) != {"source", "page_id", "language", "start", "end", "text", "complete"}
                    or row["id"] in all_rows or row["story_key"] != story_key or oc._language(view["language"]) != language
                    or view["page_id"] != page["page_id"] or view["source"] != page["source"]
                    or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(body)
                    or view.get("complete") is not True or body[start:end] != view["text"]):
                raise ValueError("Symmetric source raw window identity or bounds mismatch")
            all_rows.add(row["id"])
            bound = dict(view, sha256=page["text_sha256"])
            source_rows.append(dict(row, source=bound))
            local.append(dict(evidence_id=row["id"], focused_task_id=metadata["item_id"], scope_id=metadata["scope_id"],
                              story_key=row["story_key"], source=bound))
        if ap._digest(source_rows) != metadata["source_only_rows_sha256"]:
            raise ValueError("Frozen source-only raw row vector hash mismatch")
        if local != sorted(local, key=lambda item: item["source"]["start"]):
            raise ValueError("Symmetric source windows must preserve raw order")
        rows[language], bodies[language] = local, body
        hashes[language] = dict(source_only_packet=digest(packet_raw), source_body=digest(body_raw))
    if (sum(len(values) for values in rows.values()) != setup["source_windows"]
            or setup["manifest_sha256"] != manifest["manifest_sha256"]):
        raise ValueError("Source-only setup total denominator or manifest changed")
    return metrics, rows, bodies, hashes, manifest["manifest_sha256"], setup


def freeze(census, packet_paths, setup_path, selection_path):
    code = _code_hashes()
    if code != IMPORT_CODE:
        raise ValueError("Symmetric source dependencies changed after import; restart")
    frozen = {}
    selection_raw = _read(selection_path, frozen)
    selection = json.loads(selection_raw)
    if (selection.get("proposal_or_tokenizer_candidates_read") is not False
            or selection.get("machine_reference_not_human_gold") is not True
            or selection.get("full_story_semantically_read") is not False
            or selection.get("exhaustive_reference") is not False
            or not str(selection.get("scope", "")).strip()
            or set(selection.get("languages", {})) != set(LANGUAGES)):
        raise ValueError("Finite candidate-free five-language reference declarations are required")
    metrics, rows, bodies, hashes, manifest_sha, setup = _source_inputs(census, packet_paths, setup_path, selection["story_key"], frozen)
    annotations, scope = [], {}
    for language in LANGUAGES:
        review = selection["languages"][language]
        if type(review.get("source_windows_read")) is not int or review["source_windows_read"] != len(rows[language]):
            raise ValueError("Declared finite source-window denominator changed")
        by_row = {row["evidence_id"]: row for row in rows[language]}
        seen, category_counts = set(), Counter()
        for index, observation in enumerate(review["units"]):
            category = observation.get("category")
            if not isinstance(category, str) or not category.strip() or not str(observation.get("meaning", "")).strip():
                raise ValueError("Selected source units require category and independent meaning notes")
            if observation.get("evidence_id") not in by_row:
                raise ValueError("Independent source unit cites an unreviewed window")
            row = by_row[observation["evidence_id"]]
            entry, subject = _entry(dict(id=row["evidence_id"], story_key=row["story_key"], source=row["source"]), observation)
            parts = subject["source"]["segments"]
            full_view = dict(row["source"], start=0, end=len(bodies[language]), text=bodies[language])
            ss._utterance(full_view, parts)
            identity = signature(parts)
            if identity in seen:
                raise ValueError("Duplicate independent source segment vector")
            seen.add(identity)
            category_counts[category] += 1
            annotations.append(dict(id=f"{language}_source_{index:03}", language=language,
                                    kind=entry["kind"], category=category, meaning=observation["meaning"],
                                    canonical=subject["canonical"], subject_id=subject["id"], primary_segments=parts,
                                    evidence_id=row["evidence_id"], focused_task_id=row["focused_task_id"],
                                    scope_id=row["scope_id"], page_id=row["source"]["page_id"],
                                    source=row["source"]["source"], text_sha256=row["source"]["sha256"],
                                    source_window_start=row["source"]["start"], source_window_end=row["source"]["end"],
                                    enclosing_text=bodies[language][parts[0]["start"]:parts[-1]["end"]],
                                    gap_texts=[bodies[language][left["end"]:right["start"]]
                                               for left, right in zip(parts, parts[1:])]))
        if not seen:
            raise ValueError("Each source language needs a nonempty finite selected denominator")
        absent = review.get("empty_categories", {})
        missing = set(CATEGORIES) - set(category_counts)
        if (set(absent) != missing or any(not isinstance(value, str) or not value.strip() for value in absent.values())):
            raise ValueError("Unselected category classes require explicit finite-scope empty reasons")
        scope[language] = dict(source_windows_read=len(rows[language]), selected_units=len(seen),
                               first_window_start=rows[language][0]["source"]["start"],
                               last_window_end=rows[language][-1]["source"]["end"],
                               windows=[dict(evidence_id=row["evidence_id"], start=row["source"]["start"],
                                             end=row["source"]["end"], text_sha256=row["source"]["sha256"])
                                        for row in rows[language]], categories=dict(category_counts), empty_categories=absent)
    reference = dict(schema="sekaisync/p0-independent-symmetric-source-reference@1", story_key=selection["story_key"],
                     reviewer=selection["reviewer"], scope=selection["scope"], languages=list(LANGUAGES),
                     machine_reference_not_human_gold=True, proposal_or_tokenizer_candidates_read=False,
                     full_story_semantically_read=False, exhaustive_reference=False,
                     full_page_bytes_used_only_for_hash_and_structural_validation=True,
                     offset_unit="python_unicode_code_points", end_exclusive=True,
                     selection_sha256=digest(selection_raw), input_sha256=hashes,
                     holdout_manifest_sha256=manifest_sha, code_sha256=code, per_language_scope=scope,
                     annotations=annotations)
    reference["reference_sha256"] = canonical(reference, "reference_sha256")
    _stable(frozen, code)
    destination = Path(selection_path).parent / "reference.json"
    frozen_json(destination, reference)
    report = dict(reference_path=str(destination.resolve()), reference_sha256=reference["reference_sha256"],
                  reference_file_sha256=digest(destination.read_bytes()), source_units=len(annotations),
                  source_windows=sum(len(values) for values in rows.values()), per_language_scope=scope,
                  machine_reference_not_human_gold=True, exhaustive_reference=False,
                  proposal_or_tokenizer_candidates_read=False)
    frozen_json(Path(selection_path).parent / "freeze-report.json", report)
    return report


def score(census, packet_paths, setup_path, tasks_paths, reference_path, proposals, judgments, output,
          expected_reference, expected_proposals, expected_judgments):
    code = _code_hashes()
    if code != IMPORT_CODE:
        raise ValueError("Symmetric source dependencies changed after import; restart")
    if any(set(values) != set(LANGUAGES) for values in (proposals, judgments, expected_proposals, expected_judgments)):
        raise ValueError("Score requires exactly five frozen proposals and judgment files")
    frozen = {}
    reference_raw = _read(reference_path, frozen)
    if digest(reference_raw) != expected_reference:
        raise ValueError("Frozen independent reference file hash mismatch")
    reference = json.loads(reference_raw)
    if (canonical(reference, "reference_sha256") != reference.get("reference_sha256")
            or reference.get("schema") != "sekaisync/p0-independent-symmetric-source-reference@1"
            or reference.get("proposal_or_tokenizer_candidates_read") is not False
            or reference.get("machine_reference_not_human_gold") is not True
            or reference.get("full_story_semantically_read") is not False or reference.get("exhaustive_reference") is not False):
        raise ValueError("Frozen independent reference identity or finite-scope declarations changed")
    metrics, rows, bodies, hashes, manifest_sha, setup = _source_inputs(census, packet_paths, setup_path, reference["story_key"], frozen)
    if hashes != reference["input_sha256"] or manifest_sha != reference["holdout_manifest_sha256"] or code != reference["code_sha256"]:
        raise ValueError("Symmetric source reference snapshot or code changed")
    vectors, artifact_hashes = {}, {}
    if set(tasks_paths) != set(LANGUAGES):
        raise ValueError("Protocol replay requires five actual internal task files")
    for language in LANGUAGES:
        tasks_raw = _read(tasks_paths[language], frozen)
        if digest(tasks_raw) != setup["artifact_sha256"].get(str(Path(tasks_paths[language]).resolve())):
            raise ValueError("Frozen actual internal task file hash mismatch")
        tasks = json.loads(tasks_raw)
        metadata = metrics[language]
        if (len(tasks) != 1 or tasks[0]["id"] != metadata["item_id"] or tasks[0]["language"] != language
                or tasks[0]["review_context"]["scope_id"] != metadata["scope_id"]
                or ap._digest([dict(id=row["id"], story_key=row["story_key"], source=row["source"])
                               for row in tasks[0]["review_context"]["rows"]]) != metadata["source_only_rows_sha256"]):
            raise ValueError("Actual internal task source windows differ from the blind source-only packet")
        proposal_raw, judgment_raw = _read(proposals[language], frozen), _read(judgments[language], frozen)
        if digest(proposal_raw) != expected_proposals[language] or digest(judgment_raw) != expected_judgments[language]:
            raise ValueError("Frozen symmetric proposal or judgment hash mismatch")
        proposal, answers = json.loads(proposal_raw), json.loads(judgment_raw)
        if proposal.get("language") != language or proposal.get("story_key") != reference["story_key"]:
            raise ValueError("Frozen symmetric proposal source-language or family mismatch")
        expected, coverage = _prepare(tasks, proposal)
        if serialized(expected) != serialized(answers):
            raise ValueError("Actual judgments do not reproduce the frozen per-turn proposal")
        vectors[language] = {signature(entry["segments"]) for answer in answers for entry in answer["subjects"]}
        artifact_hashes[language] = dict(proposal=digest(proposal_raw), judgments=digest(judgment_raw), tasks=digest(tasks_raw))
    assessments = []
    for annotation in reference["annotations"]:
        language = annotation["language"]
        parts = annotation["primary_segments"]
        if (language not in rows or any(type(part["start"]) is not int or type(part["end"]) is not int
                                        or bodies[language][part["start"]:part["end"]] != part["exact"] for part in parts)
                or not any(row["evidence_id"] == annotation["evidence_id"] and row["focused_task_id"] == annotation["focused_task_id"]
                           and row["source"]["start"] <= parts[0]["start"] < parts[-1]["end"] <= row["source"]["end"]
                           for row in rows[language])):
            raise ValueError("Frozen symmetric reference raw vector or source window mismatch")
        exact = signature(parts) in vectors[language]
        selected = {position for part in parts for position in range(part["start"], part["end"])}
        mandatory = {position for position in selected if not bodies[language][position].isspace()}
        covered, broad = set(), False
        for vector in vectors[language]:
            positions = {position for start, end, _exact in vector for position in range(start, end)}
            if positions <= selected:
                covered |= positions
            if mandatory <= positions and positions - selected:
                broad = True
        assessments.append(dict(annotation, strict_primary_exact=exact,
                                diagnostic_fragment_union_only=not exact and mandatory <= covered,
                                diagnostic_overbroad_only=not exact and broad))
    def counts(values):
        return dict(reference_source_units=len(values), strict_primary_exact=sum(item["strict_primary_exact"] for item in values),
                    strict_selected_reference_recall=(sum(item["strict_primary_exact"] for item in values) / len(values)
                                                      if values else None),
                    diagnostic_fragment_union_only=sum(item["diagnostic_fragment_union_only"] for item in values),
                    diagnostic_overbroad_only=sum(item["diagnostic_overbroad_only"] for item in values))
    report = dict(schema="sekaisync/p0-independent-symmetric-source-score@1", story_key=reference["story_key"],
                  reference_file_sha256=digest(reference_raw), reference_sha256=reference["reference_sha256"],
                  input_sha256=hashes, proposal_judgment_sha256=artifact_hashes, code_sha256=code,
                  source_windows=sum(len(values) for values in rows.values()),
                  scope=reference["scope"], per_language_scope=reference["per_language_scope"],
                  machine_reference_not_human_gold=True, exhaustive_reference=False, full_story_semantically_read=False,
                  postfreeze_variants_accepted=False, semantic_precision=None, exhaustive_vocabulary_recall=None,
                  category_labels_are_semantic_truth=False,
                  scoring_rule="Exact frozen source (start,end,exact) vector; fragment union and broad envelopes are diagnostic only.",
                  summary=counts(assessments),
                  per_language={language: counts([item for item in assessments if item["language"] == language]) for language in LANGUAGES},
                  per_type={kind: counts([item for item in assessments if item["kind"] == kind]) for kind in ("literal", "segmented")},
                  per_category={category: counts([item for item in assessments if item["category"] == category])
                                for category in sorted({item["category"] for item in assessments})}, assessments=assessments)
    _stable(frozen, code)
    frozen_json(output, report)
    return report


def _mapping(values):
    result = {}
    for value in values or []:
        language, content = value.split("=", 1)
        if language in result:
            raise ValueError("Duplicate language argument")
        result[language] = content
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census", type=Path, required=True)
    parser.add_argument("--packet", action="append", required=True, help="LANGUAGE=PATH")
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--tasks", action="append", help="LANGUAGE=PATH; scoring only, not candidate-free freeze")
    parser.add_argument("--selection", type=Path)
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--proposal", action="append", help="LANGUAGE=PATH")
    parser.add_argument("--judgments", action="append", help="LANGUAGE=PATH")
    parser.add_argument("--expected-reference-sha256")
    parser.add_argument("--expected-proposal-sha256", action="append", help="LANGUAGE=SHA256")
    parser.add_argument("--expected-judgments-sha256", action="append", help="LANGUAGE=SHA256")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    if args.selection:
        report = freeze(args.census, _mapping(args.packet), args.setup, args.selection)
    else:
        report = score(args.census, _mapping(args.packet), args.setup, _mapping(args.tasks), args.reference,
                       _mapping(args.proposal), _mapping(args.judgments), args.out,
                       args.expected_reference_sha256, _mapping(args.expected_proposal_sha256), _mapping(args.expected_judgments_sha256))
    print(json.dumps({key: value for key, value in report.items() if key != "assessments"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
