"""Submit frozen focused discoveries in a new isolated store, without scoring."""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as packets, agent_review as review, dbstore, termindex


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(source, tasks_path, judgments_path, output):
    if output.exists():
        raise FileExistsError("focused submission output is frozen; choose a new directory")
    inputs = {str(path): _hash(path) for path in (tasks_path, judgments_path)}
    tasks = [review.ReviewItem.from_dict(task) for task in json.loads(tasks_path.read_text(encoding="utf-8"))]
    judgments = json.loads(judgments_path.read_text(encoding="utf-8"))
    if ({task.id for task in tasks} != {judgment["id"] for judgment in judgments}
            or len(tasks) != len({task.id for task in tasks}) or len(judgments) != len(tasks)
            or any(task._context.get("task") != "discovery" for task in tasks)):
        raise ValueError("focused judgments must cover each unique discovery task exactly once")
    stories = {row["story_key"] for task in tasks for row in task._context["rows"]}
    if len(stories) != 1:
        raise ValueError("focused isolated submission must contain one content family")
    code_paths = [Path(__file__)] + sorted((ROOT / "sekaisync").rglob("*.py"))
    code_hashes = {str(path): _hash(path) for path in code_paths}
    scopes = {task._context["scope_id"]: packets._read_scope(source, task._context["scope_id"]) for task in tasks}
    scope_hashes = {identity: _hash(packets._scope_path(source, identity)) for identity in scopes}
    # The source is the small frozen holdout snapshot, never the production store.
    pages = [page for rows in dbstore.load_web_pages(source).values() for page in rows
             if termindex.page_story_key(page) in stories]
    if not pages:
        raise ValueError("focused source family has no frozen local pages")
    output.mkdir(parents=True, exist_ok=False)
    store = output / "store"
    dbstore.initialize(store)
    for provider in sorted({page["source"] for page in pages}):
        dbstore.upsert_web_pages(store, provider, [page for page in pages if page["source"] == provider])
    for identity in scopes:
        target = packets._scope_path(store, identity)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(packets._scope_path(source, identity), target)
    review.enqueue(store, tasks)
    review.export_for_agent(store, output / "discovery-export.txt", limit=0)
    submitted = review.submit_judgments(store, judgments)
    replay = review.submit_judgments(store, judgments)
    if submitted["errors"] or replay["errors"] or submitted["accepted"] != len(tasks):
        raise RuntimeError("focused submission failed: " + json.dumps(dict(submitted=submitted, replay=replay)))
    receipts = packets._receipts(store)
    subjects = [subject for task in tasks for subject in json.loads(receipts[task.id]["value"])["subjects"]]
    with dbstore.connect(store) as conn:
        global_terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
    if global_terms or len({subject["id"] for subject in subjects}) != len(subjects):
        raise RuntimeError("focused source units were flattened or duplicated")
    queue = review.load_queue(store)
    report = dict(schema="sekaisync/focused-source-submission@1", semantic_gold=False,
                  blind_recall=None, no_independent_reference_read=True, story_key=next(iter(stories)),
                  tasks=len(tasks), source_units=len(subjects), subject_kinds=dict(Counter(s["kind"] for s in subjects)),
                  followup_tasks=dict(Counter(item._context.get("task") for item in queue)), global_terms=global_terms,
                  submission=submitted, replay=replay, input_sha256=inputs, code_sha256=code_hashes,
                  original_scope_sha256=scope_hashes)
    if (any(_hash(Path(path)) != digest for path, digest in inputs.items())
            or any(_hash(path) != code_hashes[str(path)] for path in code_paths)
            or any(_hash(packets._scope_path(source, identity)) != digest for identity, digest in scope_hashes.items())):
        raise RuntimeError("focused inputs or dependency closure changed; report not published")
    with (output / "report.json").open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-store", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--judgments", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    report = _run(args.source_store, args.tasks, args.judgments, args.output_directory)
    print(json.dumps({key: value for key, value in report.items() if "sha256" not in key}, indent=2))


if __name__ == "__main__":
    main()
