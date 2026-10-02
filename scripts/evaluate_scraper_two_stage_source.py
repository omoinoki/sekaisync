"""Score frozen initial and actual receipt-backed second source review vectors."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import evaluate_scraper_symmetric_source as source
from scripts import freeze_scraper_source_audits as capture
from scripts.prepare_scraper_focused_discovery import _prepare
from scripts.evaluate_scraper_span_source import frozen_json
from sekaisync import agent_packets as packets, dbstore, source_audits
from sekaisync.fetcher import store_writer_lock


def _code_hashes():
    return dict(source._code_hashes(), **{str(Path(__file__).resolve()): source.digest(Path(__file__).read_bytes()),
                                         str(Path(capture.__file__).resolve()): source.digest(Path(capture.__file__).read_bytes())})


def _actual_receipt(conn, context, answer, receipt):
    if (not isinstance(receipt, dict) or receipt.get("decision") != "accept"
            or receipt.get("scope_id") != context["scope_id"]
            or source.serialized(receipt.get("review_context")) != source.serialized(context)):
        raise ValueError("Two-stage source score requires the actual accepted exact-context receipt")
    terms = answer.get("terms", [])
    subjects = packets._discovery_subjects(conn, context, answer.get("subjects", []))
    expected = dict(terms=terms, subjects=subjects)
    if source.serialized(json.loads(receipt["value"])) != source.serialized(expected):
        raise ValueError("Actual source receipt does not reproduce its frozen judgment")
    return {source.signature(subject["source"]["segments"]) for subject in subjects}


def score(census, packet_paths, setup_path, initial_tasks_paths, reference_path,
          initial_proposals, initial_judgments, audit_capture_path, audit_proposals, audit_judgments,
          output, expected_reference, expected_initial_proposals, expected_initial_judgments,
          expected_capture, expected_audit_proposals, expected_audit_judgments):
    maps = (packet_paths, initial_tasks_paths, initial_proposals, initial_judgments,
            audit_proposals, audit_judgments, expected_initial_proposals, expected_initial_judgments,
            expected_audit_proposals, expected_audit_judgments)
    if any(set(values) != set(source.LANGUAGES) for values in maps):
        raise ValueError("Two-stage score requires five exact source language input mappings")
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError("Two-stage source scores are frozen; choose a new directory")
    frozen, code = {}, _code_hashes()
    capture_raw = source._read(audit_capture_path, frozen)
    if source.digest(capture_raw) != expected_capture:
        raise ValueError("Frozen actual audit capture report hash mismatch")
    captured = json.loads(capture_raw)
    setup_raw = source._read(setup_path, frozen)
    setup = json.loads(setup_raw)
    if (captured.get("schema") != "sekaisync/symmetric-source-audit-capture@1"
            or captured.get("setup_file_sha256") != source.digest(setup_raw)
            or captured.get("story_key") != setup["story_key"]
            or captured.get("references_or_labels_read") is not False
            or captured.get("actual_parent_receipts_validated") is not True
            or captured.get("actual_pending_review_items") is not True
            or captured.get("target_views_in_source_packets") is not False
            or captured.get("code_sha256") != capture._code_hashes()):
        raise ValueError("Actual audit capture isolation, setup or dependency closure changed")
    metadata = {item["language"]: item for item in captured["languages"]}
    initial_metadata = {item["language"]: item for item in setup["languages"]}
    if (set(metadata) != set(source.LANGUAGES) or len(captured["languages"]) != 5
            or set(captured.get("stores", {})) != set(source.LANGUAGES)):
        raise ValueError("Actual audit capture requires five unique source identities and runner stores")
    initial_vectors, audit_vectors, artifacts, receipt_hashes = {}, {}, {}, {}
    stores = {language: Path(path).resolve() for language, path in captured["stores"].items()}
    with ExitStack() as stack:
        connections = {}
        for store in sorted(set(stores.values()), key=str):
            stack.enter_context(store_writer_lock(store))
            connections[store] = stack.enter_context(dbstore.connect(store))
        for language in source.LANGUAGES:
            store, entry = stores[language], metadata[language]
            conn = connections[store]
            tasks_path = Path(captured["internal_tasks_directory"]) / (language + ".json")
            packet_path = Path(captured["source_only_packet_directory"]) / (language + ".txt")
            task_raw, packet_raw = source._read(tasks_path, frozen), source._read(packet_path, frozen)
            if (source.digest(task_raw) != captured["artifact_sha256"].get(str(tasks_path.resolve()))
                    or source.digest(packet_raw) != captured["artifact_sha256"].get(str(packet_path.resolve()))):
                raise ValueError("Frozen actual audit task or source-only packet hash mismatch")
            tasks = json.loads(task_raw)
            if len(tasks) != 1:
                raise ValueError("Two-stage experiment requires one actual review task per source")
            task = tasks[0]
            item = packets._item(task["term"], task["language"], task.get("candidates", []),
                                 task["kind"], task["review_context"], "")
            if (item.id != task["id"] or item.id != entry["item_id"] or item.language != language
                    or entry["root_item_id"] != initial_metadata[language]["item_id"]
                    or item._context["scope_id"] != initial_metadata[language]["scope_id"]
                    or packets._digest(capture._source_rows(item)) != initial_metadata[language]["source_only_rows_sha256"]
                    or capture._source_packet(item).splitlines() != packet_raw.decode("utf-8").splitlines()):
                raise ValueError("Actual second-stage task is not the original frozen source-only window vector")
            source_audits._validate(conn, store, item)
            initial_tasks_raw = source._read(initial_tasks_paths[language], frozen)
            initial_tasks = json.loads(initial_tasks_raw)
            if (len(initial_tasks) != 1 or initial_tasks[0]["id"] != entry["root_item_id"]
                    or source.digest(initial_tasks_raw) != setup["artifact_sha256"].get(
                        str(Path(initial_tasks_paths[language]).resolve()))):
                raise ValueError("Initial task is not the actual frozen setup task")
            stage_artifacts, stage_answers = {}, {}
            for stage, task_values, proposals, judgments, expected_proposals, expected_judgments in (
                ("initial", initial_tasks, initial_proposals, initial_judgments, expected_initial_proposals, expected_initial_judgments),
                ("audit", tasks, audit_proposals, audit_judgments, expected_audit_proposals, expected_audit_judgments),
            ):
                proposal_raw = source._read(proposals[language], frozen)
                judgment_raw = source._read(judgments[language], frozen)
                if (source.digest(proposal_raw) != expected_proposals[language]
                        or source.digest(judgment_raw) != expected_judgments[language]):
                    raise ValueError("Frozen two-stage source proposal or judgment hash mismatch")
                proposal, answers = json.loads(proposal_raw), json.loads(judgment_raw)
                if proposal.get("language") != language or proposal.get("story_key") != setup["story_key"]:
                    raise ValueError("Two-stage proposal source language or family changed")
                expected, _ = _prepare(task_values, proposal)
                if source.serialized(expected) != source.serialized(answers) or len(answers) != 1:
                    raise ValueError("Actual stage judgments do not reproduce the frozen complete per-turn review")
                stage_answers[stage] = answers[0]
                stage_artifacts[stage] = dict(proposal=source.digest(proposal_raw), judgments=source.digest(judgment_raw))
            receipts = packets._receipts(store, conn=conn)
            parent, actual = receipts.get(entry["root_item_id"]), receipts.get(item.id)
            if not isinstance(parent, dict) or capture._receipt_hash(parent) != entry["parent_receipt_sha256"]:
                raise ValueError("First-stage parent receipt changed since actual audit capture")
            initial_vectors[language] = _actual_receipt(conn, initial_tasks[0]["review_context"],
                                                        stage_answers["initial"], parent)
            audit_vectors[language] = _actual_receipt(conn, item._context, stage_answers["audit"], actual)
            receipt_hashes[language] = dict(parent=capture._receipt_hash(parent), audit=capture._receipt_hash(actual))
            artifacts[language] = dict(stage_artifacts, actual_audit_task=source.digest(task_raw),
                                       actual_audit_source_packet=source.digest(packet_raw), store=str(store))
        source._stable(frozen, source._code_hashes())
        if _code_hashes() != code:
            raise ValueError("Two-stage score dependency closure changed during protocol validation")
        output.mkdir(parents=True, exist_ok=False)
        baseline = source.score(census, packet_paths, setup_path, initial_tasks_paths, reference_path,
                                initial_proposals, initial_judgments, output / "initial-score.json",
                                expected_reference, expected_initial_proposals, expected_initial_judgments)
        assessments = []
        for annotation in baseline["assessments"]:
            language = annotation["language"]
            vector = source.signature(annotation["primary_segments"])
            initial = vector in initial_vectors[language]
            audit = vector in audit_vectors[language]
            if initial != annotation["strict_primary_exact"]:
                raise ValueError("Initial receipt vectors disagree with independent initial score")
            assessments.append(dict(annotation, initial_strict_primary_exact=initial,
                                    audit_strict_primary_exact=audit, newly_supported_by_audit=not initial and audit,
                                    cumulative_strict_primary_exact=initial or audit))
        def counts(values):
            total = len(values)
            initial = sum(item["initial_strict_primary_exact"] for item in values)
            added = sum(item["newly_supported_by_audit"] for item in values)
            return dict(reference_source_units=total, initial_strict_primary_exact=initial,
                        audit_incremental_strict_primary_exact=added, cumulative_strict_primary_exact=initial + added,
                        initial_selected_reference_recall=initial / total if total else None,
                        cumulative_selected_reference_recall=(initial + added) / total if total else None)
        report = dict(schema="sekaisync/p0-independent-two-stage-source-score@1", story_key=baseline["story_key"],
                      reference_file_sha256=baseline["reference_file_sha256"], reference_sha256=baseline["reference_sha256"],
                      initial_score_file_sha256=source.digest((output / "initial-score.json").read_bytes()),
                      actual_audit_capture_sha256=source.digest(capture_raw), artifacts_sha256=artifacts,
                      actual_receipts_sha256=receipt_hashes, code_sha256=code, source_windows=baseline["source_windows"],
                      machine_reference_not_human_gold=True, exhaustive_reference=False,
                      semantic_precision=None, exhaustive_vocabulary_recall=None, cross_language_precision=None,
                      component_union_counted_as_exact_hit=False, alternatives_postfreeze_accepted=False,
                      same_actual_runner_store_continued=True, actual_accepted_stage_receipts_validated=True,
                      summary=counts(assessments),
                      per_language={language: counts([item for item in assessments if item["language"] == language])
                                    for language in source.LANGUAGES},
                      per_type={kind: counts([item for item in assessments if item["kind"] == kind])
                                for kind in ("literal", "segmented")},
                      per_category={category: counts([item for item in assessments if item["category"] == category])
                                    for category in sorted({item["category"] for item in assessments})},
                      observations={language: dict(initial_unique_vectors=len(initial_vectors[language]),
                                                  audit_unique_vectors=len(audit_vectors[language]),
                                                  audit_new_unique_vectors=len(audit_vectors[language] - initial_vectors[language]),
                                                  repeated_vector_geometry=len(audit_vectors[language] & initial_vectors[language]))
                                    for language in source.LANGUAGES}, assessments=assessments)
        for language in source.LANGUAGES:
            current = packets._receipts(stores[language], conn=connections[stores[language]])
            entry = metadata[language]
            if (capture._receipt_hash(current[entry["root_item_id"]]) != receipt_hashes[language]["parent"]
                    or capture._receipt_hash(current[entry["item_id"]]) != receipt_hashes[language]["audit"]):
                raise ValueError("Actual stage receipts changed during two-stage scoring")
        source._stable(frozen, source._code_hashes())
        if _code_hashes() != code:
            raise ValueError("Two-stage score dependencies changed before final report")
        frozen_json(output / "two-stage-score.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("census", "setup", "reference", "audit-capture", "output"):
        parser.add_argument("--" + field, type=Path, required=True)
    for field in ("packet", "initial-tasks", "initial-proposal", "initial-judgments", "audit-proposal", "audit-judgments",
                  "expected-initial-proposal", "expected-initial-judgments", "expected-audit-proposal", "expected-audit-judgments"):
        parser.add_argument("--" + field, action="append", required=True)
    parser.add_argument("--expected-reference-sha256", required=True)
    parser.add_argument("--expected-capture-sha256", required=True)
    args = parser.parse_args()
    mapping = source._mapping
    result = score(args.census, mapping(args.packet), args.setup, mapping(args.initial_tasks), args.reference,
                   mapping(args.initial_proposal), mapping(args.initial_judgments), args.audit_capture,
                   mapping(args.audit_proposal), mapping(args.audit_judgments), args.output,
                   args.expected_reference_sha256, mapping(args.expected_initial_proposal),
                   mapping(args.expected_initial_judgments), args.expected_capture_sha256,
                   mapping(args.expected_audit_proposal), mapping(args.expected_audit_judgments))
    print(json.dumps(dict(story_key=result["story_key"], summary=result["summary"],
                          per_language=result["per_language"]), indent=2))


if __name__ == "__main__":
    main()
