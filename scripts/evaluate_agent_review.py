"""Prepare/replay a host-agent evaluation in an isolated store, without API keys.

Production pages are opened read-only. Gold labels are used by the scorer only;
the agent sees real source/target excerpts, never annotation lists or gold pairs.
This is a small host-agent observation, not an independent accuracy benchmark.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, termindex as ti, zhfirst as zh
from sekaisync.normalize import normalize_name


def _labels(value):
    return normalize_name(value.strip('「」『』“”‘’" '))


def prepare(store, out):
    out.mkdir(parents=True, exist_ok=True)
    test_store = out / "evaluation-store"
    if dbstore.db_file(test_store).exists():
        raise ValueError("evaluation store already exists; use --submit/--score or a new --output directory")
    annotations = json.loads((ROOT / "data/term-annotations.json").read_text(encoding="utf-8"))["stories"]
    keys = sorted(k for k in annotations if not k.endswith(":TITLE"))
    events = sorted({k.split(":")[1] for k in keys})
    with sqlite3.connect((store / "kb/sekaisync.db").resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.execute("PRAGMA query_only=ON")
        conn.row_factory = sqlite3.Row
        pages = [dict(row) for row in conn.execute(
            "SELECT * FROM web_pages WHERE kind='event_story' AND auxiliary=0 AND (" +
            " OR ".join("id LIKE ?" for _ in events) + ")",
            tuple("%:event_story:" + event + ":%" for event in events))]
        glossary = [SimpleNamespace(**{**dict(row), "names": json.loads(row["names_json"])}) for row in conn.execute(
            "SELECT kind,canonical,official,names_json FROM glossary_terms WHERE official=1 AND COALESCE(demo,0)=0")]
    groups = ti.group_pages_by_story(pages)
    languages = ("ja", "en", "zh_hans", "zh_tw", "ko")
    complete = {k: by for k, by in groups.items() if k in keys and all(ti._group_page(by, l) for l in languages)}
    pages = [p for by in complete.values() for p in by.values()]
    with patch.object(zh, "_load_manual_seed", return_value=set()):
        automatic = zh.extract_terms_zhfirst(pages, [], glossary, do_align=False)
    actual = {_labels(t.canonical) for t in automatic}
    gold = {_labels(t) for k in complete for t in annotations[k]
            if _labels(t) in normalize_name(ti._group_page(complete[k], "zh_hans")["text"])}
    dbstore.initialize(test_store)
    for source in sorted({p["source"] for p in pages}):
        dbstore.upsert_web_pages(test_store, source, [p for p in pages if p["source"] == source])
    # Empty candidate input is deliberate: test whether discovery can recover
    # words independently of the existing tokenizer and proposed word list.
    items, meta = ap._prepare_scrub_review(test_store, complete, sorted(complete), [], {}, "zh_hans",
                                          [l for l in languages if l != "zh_hans"])
    ar.enqueue(test_store, items)
    sample = random.Random(20260929).sample(items, min(6, len(items)))
    packet_text = ar.DECIDE_HELP + "\n\n" + "\n\n".join(ar.render_item(i) for i in sample)
    (out / "discovery-packets.txt").write_text(packet_text, encoding="utf-8")
    state = dict(scope=meta, complete_stories=len(complete), pages=len(pages),
                 gold=sorted(gold), automatic=sorted(actual),
                 sampled_discovery_ids=[i.id for i in sample],
                 corpus_sha256=hashlib.sha256(json.dumps(
                     [[p["id"], p["text"]] for p in sorted(pages, key=lambda p:p["id"])],
                     ensure_ascii=False).encode()).hexdigest())
    (out / "scoring-state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(dict(complete_stories=len(complete), pages=len(pages), packets=meta,
                         labelled=len(gold), baseline_found=len(actual & gold), sample_packets=len(sample)), ensure_ascii=False))


def score(out):
    state = json.loads((out / "scoring-state.json").read_text(encoding="utf-8"))
    test_store = out / "evaluation-store"
    receipts = ap._receipts(test_store)
    found = set()
    completed = 0
    for identity in state["sampled_discovery_ids"]:
        receipt = receipts.get(identity)
        if receipt:
            completed += 1
            found.update(_labels(t) for t in json.loads(receipt["value"]))
    gold, baseline = set(state["gold"]), set(state["automatic"])
    with dbstore.connect(test_store) as conn:
        names = [json.loads(row[0]) for row in conn.execute("SELECT names_json FROM terms")]
        evidence_rows = conn.execute("SELECT count(*) FROM term_evidence").fetchone()[0]
    targets = sum(sum(l != "zh_hans" for l in row) for row in names)
    pending = ar.load_queue(test_store)
    report = dict(schema="sekaisync/agent-evaluation@1", model_mode="current_host_agent",
                  model_api_calls=0, complete_stories=state["complete_stories"], pages=state["pages"],
                  corpus_sha256=state["corpus_sha256"], manual_seed_labels_withheld=True,
                  discovery_packets_available=state["scope"]["discovery_packets"],
                  discovery_packets_sampled=len(state["sampled_discovery_ids"]),
                  discovery_packets_completed=completed, discovered_surfaces=len(found),
                  labelled_source_terms=len(gold), automatic_recovered=len(gold & baseline),
                  agent_additional_labelled=len((found & gold) - baseline),
                  after_agent_recovered=len(gold & (found | baseline)),
                  before_recall=len(gold & baseline)/len(gold),
                  after_recall=len(gold & (found | baseline))/len(gold),
                  actual_target_slots_written=targets, evidence_rows=evidence_rows,
                  pending_translation_packets=sum(i.kind != "discovery" for i in pending),
                  precision=None, cross_language_semantic_accuracy=None, zero_errors_demonstrated=False,
                  caveat="Discovery improvement is a partial six-packet observation. Model-authored and model-reviewed judgments are not independent gold. Grounded accepted slots are derived (C), not proof of semantic infallibility.")
    (out / "evaluation-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=ROOT / "store")
    parser.add_argument("--output", type=Path, default=ROOT / "work/llm-repair-20260929/host-agent")
    parser.add_argument("--prepare", action="store_true")
    parser.add_argument("--submit", type=Path)
    parser.add_argument("--score", action="store_true")
    args = parser.parse_args()
    if args.prepare:
        prepare(args.store, args.output)
    if args.submit:
        result = ar.import_judgments_from_text(args.output / "evaluation-store", args.submit.read_text(encoding="utf-8"))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.score:
        score(args.output)


if __name__ == "__main__":
    main()
