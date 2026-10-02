"""Replay frozen source-provided/target-blind judgments through old consumers."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _code_hashes():
    paths = [Path(__file__), ROOT / "scripts/prepare_scraper_blind_target_pilot.py"] + sorted((ROOT / "sekaisync").rglob("*.py"))
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


_IMPORT_CODE_HASHES = _code_hashes()

from scripts.prepare_scraper_blind_target_pilot import _prepare
from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger, span_subjects, termindex
from sekaisync.core import SekaiSyncCore


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write(path, value):
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _run(manifest_path, packet_path, proposal_path, judgments_path, output):
    if output.exists():
        raise FileExistsError("target pilot replay is frozen; choose a fresh directory")
    code = _code_hashes()
    if code != _IMPORT_CODE_HASHES:
        raise RuntimeError("pilot implementation or dependency closure changed after import; restart")
    buffers = {path: path.read_bytes() for path in (manifest_path, packet_path, proposal_path, judgments_path)}
    inputs = {str(path.resolve()): hashlib.sha256(raw).hexdigest() for path, raw in buffers.items()}
    manifest, packet, proposal, judgments = [json.loads(buffers[path]) for path in
                                             (manifest_path, packet_path, proposal_path, judgments_path)]
    prepared = _prepare(packet, proposal)
    if any(prepared[key] != judgments[key] for key in prepared):
        raise ValueError("frozen host judgments no longer replay from their raw observations")
    story = next(item for item in manifest["stories"] if item["story_key"] == packet["story_key"])
    pages = []
    for language, metadata in story["pages"].items():
        path = (manifest_path.parent / metadata["local_body_file"]).resolve()
        if not path.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError("frozen pilot corpus path or body hash changed")
        raw = path.read_bytes()
        body_hash = hashlib.sha256(raw).hexdigest()
        if body_hash != metadata["text_sha256"]:
            raise ValueError("frozen pilot corpus path or body hash changed")
        inputs[str(path)] = body_hash
        variant = next((item for item in metadata.get("variants", [])
                        if item["source"] == metadata["source"] and item["id"] == metadata["page_id"]), {})
        pages.append(dict(variant, id=metadata["page_id"], source=metadata["source"], language=language,
                          text=raw.decode("utf-8"), kind=variant.get("kind", "event_story"), trust=variant.get("trust", "B")))
    page_map = {(page["source"], page["id"]): page for page in pages}
    for item in packet["items"]:
        for view in [item["source_context"], *item["target_contexts"].values()]:
            page = page_map[(view["source"], view["page_id"])]
            if (hashlib.sha256(page["text"].encode()).hexdigest() != view["sha256"]
                    or page["text"][view["start"]:view["end"]] != view["text"]):
                raise ValueError("pilot packet does not bind the actual frozen page")
    output.mkdir(parents=True, exist_ok=False)
    store = output / "store"
    dbstore.initialize(store)
    for source in sorted({page["source"] for page in pages}):
        dbstore.upsert_web_pages(store, source, [page for page in pages if page["source"] == source])
    tasks, _ = ap._prepare_scrub_review(store, termindex.group_pages_by_story(pages), [packet["story_key"]], [], {},
                                       packet["source_language"], packet["target_languages"])
    selected, entries, subjects = {}, defaultdict(list), {}
    for item in packet["items"]:
        matches = [(task, row) for task in tasks for row in task._context["rows"]
                   if row["source"] == item["source_context"]]
        if len(matches) != 1:
            raise ValueError("provided pilot source must match one actual generated discovery row")
        task, row = matches[0]
        parts = item["source_segments"]
        kind = "literal" if len(parts) == 1 else "segmented"
        subject = (span_subjects._literal(row["source"], row["story_key"], item["source_surface"], parts)
                   if kind == "literal" else span_subjects._segmented(row["source"], row["story_key"], parts))
        subjects[item["source_annotation_id"]] = subject
        selected[task.id] = task
        entry = dict(kind=kind, evidence_id=row["id"], segments=parts)
        if kind == "literal":
            entry["canonical"] = item["source_surface"]
        entries[task.id].append(entry)
    ar.enqueue(store, selected.values())
    ar.export_for_agent(store, output / "discovery-export.txt", limit=0)
    discovery_answers = [dict(id=identity, decision="accept", subjects=values,
                              rationale="Source-provided target-blind pilot; not an exhaustive discovery judgment.")
                         for identity, values in entries.items()]
    _write(output / "discovery-judgments.json", discovery_answers)
    discovered = ar.submit_judgments(store, discovery_answers)
    if discovered["errors"]:
        raise RuntimeError("pilot source submission failed: " + str(discovered["errors"]))
    ar.export_for_agent(store, output / "occurrence-export.txt", limit=0)
    queue = ar.load_queue(store)
    item_map = {item["source_annotation_id"]: item for item in packet["items"]}
    answers = []
    for record in judgments["records"]:
        subject = subjects[record["source_annotation_id"]]
        matches = [item for item in queue if item._context.get("subject", {}).get("id") == subject["id"]
                   and ledger._language(item.language) == ledger._language(record["target_language"])]
        if len(matches) != 1:
            raise RuntimeError("pilot subject has no unique target occurrence task")
        task, original = matches[0], item_map[record["source_annotation_id"]]
        row = task._context["rows"][0]
        if row["target"] != original["target_contexts"][record["target_language"]]:
            raise ValueError("pilot target differs from its actual exported window; explicit expansion is required")
        parts = record["target_segments"] or [dict(start=row["target"]["start"], end=row["target"]["end"], exact=row["target"]["text"])]
        answers.append(dict(id=task.id, decision="accept", relations=[dict(evidence_id=row["id"],
            source_segments=subject["source"]["segments"], target_segments=parts, kind=record["relation_type"],
            sense_key="blind-pilot:" + record["source_annotation_id"], sense_gloss=original["source_meaning"], rationale=record["reason"])]))
    _write(output / "occurrence-judgments.json", answers)
    submitted = ar.submit_judgments(store, answers)
    replay = ar.submit_judgments(store, answers)
    if submitted["errors"] or replay["errors"]:
        raise RuntimeError("pilot occurrence submission failed: " + str(submitted["errors"] + replay["errors"]))
    core, details, failures = SekaiSyncCore(store), [], []
    languages = [packet["source_language"], *packet["target_languages"]]
    with core.request_view():
        for identity, subject in subjects.items():
            penetration = core.term_penetrate(subject["canonical"], story_key=packet["story_key"], languages=languages)
            generic = core.query(subject["canonical"])
            hits = [hit for hit in generic["terms"] if hit.get("source") == "host-agent-occurrence"
                    and hit["canonical"] == subject["canonical"]]
            if penetration is None or len(hits) != 1:
                failures.append(dict(source_annotation_id=identity, reason="source consumer unavailable"))
                continue
            generic_positions = {ledger._language(position["language"]): position for position in hits[0]["positions"]}
            checks = []
            for record in [record for record in judgments["records"] if record["source_annotation_id"] == identity]:
                parts, language = record["target_segments"], record["target_language"]
                page = page_map[(item_map[identity]["target_contexts"][language]["source"], item_map[identity]["target_contexts"][language]["page_id"])]
                expected = ""
                if record["relation_type"] == "lexical" and all(not page["text"][left["end"]:right["start"]].strip()
                                                                for left, right in zip(parts, parts[1:])):
                    expected = page["text"][parts[0]["start"]:parts[-1]["end"]]
                per_lang = penetration["per_language"][language]
                position = generic_positions.get(ledger._language(language), {})
                consistent = all(entry.get("sentence") and entry.get("term") == expected
                                 and (expected or record["relation_type"] in entry.get("note", ""))
                                 for entry in (per_lang, position))
                checks.append(dict(target_language=language, kind=record["relation_type"], expected_raw_scalar=expected,
                                   penetrate=per_lang, query_position=position, consumed_with_correct_type=bool(consistent)))
                if not consistent:
                    failures.append(dict(source_annotation_id=identity, target_language=language, reason="scalar/type projection mismatch"))
            details.append(dict(source_annotation_id=identity, subject=subject, checks=checks, penetration=penetration, generic_query=generic))
    with dbstore.connect(store) as conn:
        global_terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        relations = ledger._read_relations(conn)
    if _code_hashes() != code or any(_hash(Path(path)) != digest for path, digest in inputs.items()):
        raise RuntimeError("pilot corpus, judgments or dependency closure changed; no report published")
    report = dict(schema="sekaisync/source-provided-target-blind-protocol-replay@1", story_key=packet["story_key"],
                  source_oracle_provided=True, target_reference_read=False, semantic_score=None,
                  source_units=len(subjects), directed_requirements=len(judgments["records"]), accepted_relations=len(relations),
                  relation_kinds=dict(Counter(record["relation_type"] for record in judgments["records"])),
                  global_terms=global_terms, discovery_submission=discovered, occurrence_submission=submitted, replay=replay,
                  pending_tasks=dict(Counter(item._context.get("task") for item in ar.load_queue(store))),
                  consumer_failures=failures, input_sha256=inputs, code_sha256=code, details=details)
    _write(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("manifest", "packet", "proposal", "judgments", "output-directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    report = _run(args.manifest, args.packet, args.proposal, args.judgments, args.output_directory)
    print(json.dumps({key: value for key, value in report.items() if key not in {"details", "code_sha256", "input_sha256"}}, indent=2))
    if report["consumer_failures"] or report["global_terms"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
