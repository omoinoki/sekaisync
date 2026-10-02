"""Freeze five actual pending source reviews after real first-stage submissions."""
from __future__ import annotations

import argparse
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.evaluate_scraper_span_source import frozen_json
from sekaisync import agent_packets as packets, agent_review as review, dbstore, source_audits
from sekaisync.fetcher import store_writer_lock

LANGUAGES = ("ja", "en", "zh_hans", "zh_tw", "ko")


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _code_hashes():
    paths = [Path(__file__), ROOT / "scripts/evaluate_scraper_span_source.py"]
    paths.extend(sorted((ROOT / "sekaisync").rglob("*.py")))
    return {str(path.resolve()): _hash(path) for path in paths}


def _receipt_hash(receipt):
    return packets._digest(receipt)


def _source_rows(item):
    return [dict(id=row["id"], story_key=row["story_key"], source=row["source"])
            for row in item._context["rows"]]


def _source_packet(item):
    lines = [f"## id={item.id} kind=discovery", *packets._render_context(item)]
    internal = item._context["source_boundary_audit"]
    expected_marker = {key: value for key, value in internal.items()
                       if key not in {"excluded_terms", "excluded_subject_ids", "inherited_subjects"}}
    expected_marker.update(inherited_term_count=len(internal["excluded_terms"]),
                           inherited_subject_count=len(internal["inherited_subjects"]))
    inherited = []
    for line in lines:
        if line.startswith("context: "):
            row = json.loads(line.removeprefix("context: "))
            if set(row) != {"id", "story_key", "source"}:
                raise ValueError("Actual source review export leaked non-source raw views")
        if line.startswith("task_context: "):
            context = json.loads(line.removeprefix("task_context: "))
            if set(context) != {"schema", "task", "scope_id", "source_language", "source_boundary_audit"}:
                raise ValueError("Actual source review metadata contains another task context")
            marker = context["source_boundary_audit"]
            if (packets._digest(marker) != packets._digest(expected_marker)
                    or marker.get("stage") != "review"):
                raise ValueError("Actual source review marker has unexpected nested proof context")
        if line.startswith("source_boundary_audit_inherited: "):
            inherited.append(json.loads(line.removeprefix("source_boundary_audit_inherited: ")))
    expected_inherited = {key: internal[key] for key in ("excluded_terms", "inherited_subjects")}
    if len(inherited) != 1 or packets._digest(inherited[0]) != packets._digest(expected_inherited):
        raise ValueError("Actual source review inherited observations differ from its receipt-derived context")
    return "\n".join(lines) + "\n"


def _run(setup_path, output, stores=None):
    setup_path, output = Path(setup_path).resolve(), Path(output).resolve()
    if output.exists():
        raise FileExistsError("Actual source review artifacts are frozen; choose a new directory")
    setup_raw = setup_path.read_bytes()
    setup = json.loads(setup_raw)
    code = _code_hashes()
    if (setup.get("schema") != "sekaisync/symmetric-source-trial@1"
            or setup.get("labels_or_references_read") is not False
            or setup.get("target_views_in_source_packets") is not False
            or setup.get("candidates_in_source_packets") is not False):
        raise ValueError("Actual source review requires a frozen candidate-free symmetric setup")
    metrics = {entry["language"]: entry for entry in setup["languages"]}
    if set(metrics) != set(LANGUAGES) or len(setup["languages"]) != 5:
        raise ValueError("Actual source review requires five unique root source languages")
    stores = stores or {language: setup["store"] for language in LANGUAGES}
    if set(stores) != set(LANGUAGES):
        raise ValueError("Actual source review capture requires the existing runner store for every language")
    stores = {language: Path(path).resolve() for language, path in stores.items()}
    selected, parent_hashes = [], {}
    with ExitStack() as stack:
        connections, receipt_sets, queues = {}, {}, {}
        for store in sorted(set(stores.values()), key=str):
            stack.enter_context(store_writer_lock(store))
            connections[store] = stack.enter_context(dbstore.connect(store))
            receipt_sets[store] = packets._receipts(store, conn=connections[store])
            queues[store] = review.load_queue(store)
        for language in LANGUAGES:
            store = stores[language]
            conn, receipts, queue = connections[store], receipt_sets[store], queues[store]
            metadata = metrics[language]
            matches = [item for item in queue if item.language == language
                       and source_audits._is_audit(item._context)
                       and not source_audits._is_saturated(item._context)
                       and isinstance(item._context.get("source_boundary_audit"), dict)
                       and item._context["source_boundary_audit"].get("terminal_parent_id") == metadata["item_id"]]
            if len(matches) != 1:
                raise ValueError("Exactly one actual pending second-stage review is required for " + language)
            item, = matches
            source_audits._validate(conn, store, item)
            marker = item._context["source_boundary_audit"]
            if (item._context.get("scope_id") != metadata["scope_id"]
                    or item._context.get("source_language") != language
                    or len(item._context["rows"]) != metadata["selected_windows"]
                    or packets._digest(_source_rows(item)) != metadata["source_only_rows_sha256"]
                    or marker["ordinary_ancestor_ids"]
                    or any(row["story_key"] != setup["story_key"] for row in item._context["rows"])
                    or metadata["item_id"] not in receipts or item.id in receipts):
                raise ValueError("Actual second-stage review is not the exact original finite source scope")
            selected.append(item)
            parent_hashes[language] = _receipt_hash(receipts[metadata["item_id"]])
        if setup_path.read_bytes() != setup_raw or _code_hashes() != code:
            raise ValueError("Actual source review setup or dependency closure changed during capture")
        output.mkdir(parents=True, exist_ok=False)
        (output / "packets").mkdir()
        (output / "internal-tasks").mkdir()
        artifacts, captured = {}, []
        for item in selected:
            task_path = output / "internal-tasks" / (item.language + ".json")
            packet_path = output / "packets" / (item.language + ".txt")
            frozen_json(task_path, [item.to_dict()])
            with packet_path.open("x", encoding="utf-8") as stream:
                stream.write(_source_packet(item))
            artifacts[str(task_path.resolve())] = _hash(task_path)
            artifacts[str(packet_path.resolve())] = _hash(packet_path)
            captured.append(dict(language=item.language, item_id=item.id,
                                 root_item_id=metrics[item.language]["item_id"],
                                 scope_id=item._context["scope_id"], selected_windows=len(item._context["rows"]),
                                 source_only_rows_sha256=packets._digest(_source_rows(item)),
                                 parent_receipt_sha256=parent_hashes[item.language]))
        for language in LANGUAGES:
            store = stores[language]
            current = packets._receipts(store, conn=connections[store])
            if _receipt_hash(current[metrics[language]["item_id"]]) != parent_hashes[language]:
                raise ValueError("Actual first-stage parent receipt changed during review capture")
        if setup_path.read_bytes() != setup_raw or _code_hashes() != code:
            raise ValueError("Actual source review inputs changed during capture")
    result = dict(schema="sekaisync/symmetric-source-audit-capture@1", story_key=setup["story_key"],
                  setup_path=str(setup_path), setup_file_sha256=hashlib.sha256(setup_raw).hexdigest(),
                  stores={language: str(path) for language, path in stores.items()},
                  source_only_packet_directory=str(output / "packets"),
                  internal_tasks_directory=str(output / "internal-tasks"),
                  source_reader_inputs="packets/*.txt only", internal_tasks_may_contain_target_contexts=True,
                  actual_parent_receipts_validated=True, actual_pending_review_items=True,
                  references_or_labels_read=False, candidates_added=False, target_views_in_source_packets=False,
                  source_windows=sum(entry["selected_windows"] for entry in captured),
                  audit_packets=len(selected), languages=captured, artifact_sha256=artifacts, code_sha256=code)
    frozen_json(output / "report.json", result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--store", action="append", default=[], help="LANGUAGE=existing runner store; repeat for all five")
    args = parser.parse_args()
    stores = {}
    for value in args.store:
        language, path = value.split("=", 1)
        if language in stores:
            raise ValueError("Duplicate actual runner source language")
        stores[language] = path
    result = _run(args.setup, args.output_directory, stores or None)
    print(json.dumps(dict(story_key=result["story_key"], source_windows=result["source_windows"],
                          audit_packets=result["audit_packets"], packet_directory=result["source_only_packet_directory"]), indent=2))


if __name__ == "__main__":
    main()
