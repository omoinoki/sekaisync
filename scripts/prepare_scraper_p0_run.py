"""Resume every discovery packet from the frozen host-agent experiment."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap, agent_review as ar


def discovery_items(scope_id: str, scope: dict) -> list:
    items, batch, chars, story = [], [], 0, None

    def flush():
        if not batch:
            return
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id=scope_id,
                       source_language=scope["source_language"], rows=list(batch))
        name = "@discover:" + batch[0]["story_key"] + ":" + batch[0]["id"]
        items.append(ap._item(name, scope["source_language"], [], "discovery", context,
                              "Discover complete content words from every source window."))

    for row in scope["windows"]:
        if batch and (story != row["story_key"] or chars + len(row["source"]["text"]) > ap._DISCOVERY_CHARS):
            flush()
            batch, chars = [], 0
        batch.append(row)
        chars += len(row["source"]["text"])
        story = row["story_key"]
    flush()
    return items


def prepare(source: Path, out: Path) -> dict:
    manifest_path = out / "discovery-manifest.json"
    if manifest_path.exists():
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    target = out / "evaluation-store"
    if target.exists():
        raise ValueError("Partial snapshot exists: inspect it before resuming preparation")
    state = json.loads((source / "scoring-state.json").read_text(encoding="utf-8"))
    original = source / "evaluation-store"
    scope_id = state["scope"]["scope_id"]
    scope = ap._read_scope(original, scope_id)
    items = discovery_items(scope_id, scope)
    legacy = state["sampled_discovery_ids"]
    receipts = ap._receipts(original)
    identities = {item.id for item in items}
    if len(items) != state["scope"]["discovery_packets"] or not set(legacy) <= identities:
        raise ValueError("Regenerated discovery identities differ from frozen experiment")
    pending = [item for item in items if item.id not in receipts]
    queued = {item.id for item in ar.load_queue(original, kind="discovery")}
    if queued != {item.id for item in pending}:
        raise ValueError("Pending discovery queue and receipt census disagree")

    (target / "kb").mkdir(parents=True)
    with sqlite3.connect((original / "kb/sekaisync.db").resolve().as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(target / "kb/sekaisync.db") as dst:
            src.backup(dst)
    shutil.copytree(original / "kb/terms", target / "kb/terms")
    shutil.copy2(source / "scoring-state.json", out / "scoring-state.json")
    shutil.copy2(source / "evaluation-report.json", out / "baseline-evaluation-report.json")
    packets = out / "packets"
    packets.mkdir()
    (out / "judgments").mkdir()
    counts = [len(pending) // 3 + (i < len(pending) % 3) for i in range(3)]
    partitions, start = {}, 0
    for name, count in zip(("a", "b", "root"), counts):
        chunk = pending[start:start + count]
        start += count
        partitions[name] = [item.id for item in chunk]
        lines = [ar.DECIDE_HELP, "", "Read every window; score labels are not part of these packets."]
        for item in chunk:
            context = item._context
            lines.extend(["", f"## id={item.id} kind=discovery",
                          f"stories={','.join(item.story_keys)} windows={len(context['rows'])}"])
            for index, row in enumerate(context["rows"], 1):
                view = row["source"]
                lines.append(f"### window={index} id={row['id']} start={view['start']} end={view['end']} complete={view['complete']}")
                lines.append(view["text"])
        (packets / f"discovery-{name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (packets / f"discovery-{name}.json").write_text(json.dumps(
            [item.to_dict() for item in chunk], ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = dict(schema="sekaisync/p0-discovery-manifest@1", source_experiment=str(source.resolve()),
                    corpus_sha256=state["corpus_sha256"], scope_id=scope_id,
                    tasks=[item.to_dict() for item in items], legacy_sampled_ids=legacy,
                    initial_completed_ids=sorted(identities & receipts.keys()), partitions=partitions,
                    source_windows=len(scope["windows"]), source_store_read_only=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "work/llm-repair-20260929/host-agent")
    parser.add_argument("--output", type=Path, default=ROOT / "work/p0-exhaustive-20261001")
    args = parser.parse_args()
    manifest = prepare(args.source, args.output)
    print(json.dumps(dict(tasks=len(manifest["tasks"]), initially_completed=len(manifest["initial_completed_ids"]),
                          partitions={k: len(v) for k, v in manifest["partitions"].items()},
                          source_windows=manifest["source_windows"], scope_id=manifest["scope_id"]), indent=2))


if __name__ == "__main__":
    main()
