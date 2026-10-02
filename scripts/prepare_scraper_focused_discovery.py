"""Ground host proposal shape in immutable focused-task raw views.

This preparation does not acquire or replay full pages and is not a normal
acquisition receipt. Subject acceptance still requires downstream validation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as packets, span_subjects


def _code_hashes(root):
    files = [Path(__file__)] + sorted((root / "sekaisync").rglob("*.py"))
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}


_IMPORT_ROOT, _IMPORT_HASHES = ROOT, _code_hashes(ROOT)


def _unique(text, exact):
    if not isinstance(exact, str) or not exact.strip():
        raise ValueError("observation requires nonempty exact raw text")
    hits = [index for index in range(len(text)) if text.startswith(exact, index)]
    if len(hits) != 1:
        raise ValueError("observation requires one unambiguous raw occurrence")
    return hits[0]


def _entry(row, observation):
    view = row["source"]
    text = view["text"]
    clause = observation.get("clause", text)
    base = view["start"] + _unique(text, clause)
    kind = observation["kind"]
    if kind == "literal":
        surface = observation["surface"]
        start = base + _unique(clause, surface)
        parts = [dict(start=start, end=start + len(surface), exact=surface)]
        subject = span_subjects._literal(view, row["story_key"], surface, parts)
        if not packets._valid_literal_surface(subject):
            raise ValueError("literal subject requires a complete typed raw surface")
        entry = dict(kind=kind, canonical=surface, segments=parts)
    elif kind == "segmented":
        parts, cursor = [], 0
        for fragment in observation["parts"]:
            local = _unique(clause[cursor:], fragment) + cursor
            start = base + local
            parts.append(dict(start=start, end=start + len(fragment), exact=fragment))
            cursor = local + len(fragment)
        subject = span_subjects._segmented(view, row["story_key"], parts)
        entry = dict(kind=kind, segments=parts)
    else:
        raise ValueError("observation must be literal or segmented")
    return dict(entry, evidence_id=row["id"]), subject


def _prepare(tasks, proposal):
    if not tasks or len({task["id"] for task in tasks}) != len(tasks):
        raise ValueError("focused task identities must be nonempty and unique")
    rows, owners = {}, {}
    for task in tasks:
        context = task["review_context"]
        if context["task"] != "discovery" or context["source_language"] != proposal["language"]:
            raise ValueError("focused tasks must be discovery in the selected source language")
        for row in context["rows"]:
            if row["story_key"] != proposal["story_key"] or row["id"] in rows:
                raise ValueError("focused tasks must have unique rows from one frozen family")
            rows[row["id"]], owners[row["id"]] = row, task["id"]
    reviewed = proposal["turns"]
    if len(reviewed) != len(rows) or {turn["evidence_id"] for turn in reviewed} != set(rows):
        raise ValueError("host must explicitly review every focused raw window exactly once")
    judgments = {task["id"]: dict(id=task["id"], decision="accept", terms=[], subjects=[],
                                     agent=proposal["agent"], rationale=proposal["provenance"])
                 for task in tasks}
    coverage, identities = [], set()
    for turn in reviewed:
        row = rows[turn["evidence_id"]]
        if not turn["units"] and not str(turn.get("empty_reason", "")).strip():
            raise ValueError("empty turn review must give an explicit reason")
        for observation in turn["units"]:
            if not str(observation.get("category", "")).strip():
                raise ValueError("host must label each selected expression category")
            entry, subject = _entry(row, observation)
            if subject["id"] in identities:
                raise ValueError("duplicate exact source subject does not increase discovery")
            identities.add(subject["id"])
            judgments[owners[row["id"]]]["subjects"].append(entry)
        coverage.append(dict(evidence_id=row["id"], reviewed=True, observations=len(turn["units"]),
                             empty_reason=turn.get("empty_reason"),
                             source_sha256=row["source"]["sha256"],
                             start=row["source"]["start"], end=row["source"]["end"]))
    if any(len(judgment["subjects"]) > packets._DISCOVERY_TERMS for judgment in judgments.values()):
        raise ValueError("focused task exceeds subject budget; prepare an explicit continuation")
    return list(judgments.values()), coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--output-prefix", type=Path, required=True)
    args = parser.parse_args()
    code_hashes = _code_hashes(ROOT)
    if ROOT == _IMPORT_ROOT and code_hashes != _IMPORT_HASHES:
        raise ValueError("focused preparation dependencies changed after import; restart")
    paths = (args.output_prefix.with_suffix(".json"),
             args.output_prefix.with_name(args.output_prefix.name + "-coverage.json"))
    if any(path.exists() for path in paths):
        raise FileExistsError("focused proposal artifacts are frozen; choose a new prefix")
    task_raw, proposal_raw = args.tasks.read_bytes(), args.proposal.read_bytes()
    judgments, coverage = _prepare(json.loads(task_raw), json.loads(proposal_raw))
    hashes = dict(tasks_sha256=hashlib.sha256(task_raw).hexdigest(),
                  proposal_sha256=hashlib.sha256(proposal_raw).hexdigest(),
                  code_sha256=code_hashes)
    if args.tasks.read_bytes() != task_raw or args.proposal.read_bytes() != proposal_raw:
        raise ValueError("focused inputs changed during preparation")
    if _code_hashes(ROOT) != code_hashes:
        raise ValueError("focused preparation dependency closure changed during execution")
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    for path, value in zip(paths, (judgments, dict(**hashes, windows=coverage))):
        with path.open("x", encoding="utf-8") as output:
            output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(tasks=len(judgments), windows=len(coverage),
                          subjects=sum(len(judgment["subjects"]) for judgment in judgments), **hashes), indent=2))


if __name__ == "__main__":
    main()
