"""Prepare five source-only default packets from one hash-verified blind family."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.census_scraper_corpus import LANGUAGES, normalize_language
from sekaisync import agent_packets as packets, agent_review as review, dbstore, termindex


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _code_hashes():
    paths = [Path(__file__), ROOT / "scripts" / "census_scraper_corpus.py"]
    paths.extend(sorted((ROOT / "sekaisync").rglob("*.py")))
    return {str(path.resolve()): _hash(path) for path in paths}


def _load_family(census, story_key):
    census = census.resolve()
    path = census / "holdout-manifest.json"
    raw = path.read_bytes()
    manifest = json.loads(raw)
    if manifest.get("candidate_free") is not True or manifest.get("gold_free") is not True:
        raise ValueError("Trial requires a candidate-free and gold-free frozen holdout")
    unsigned = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    digest = hashlib.sha256(json.dumps(unsigned, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    if digest != manifest.get("manifest_sha256"):
        raise ValueError("Frozen holdout manifest hash mismatch")
    families = [story for story in manifest["stories"] if story["story_key"] == story_key]
    if len(families) != 1:
        raise ValueError("Selected blind family must occur exactly once in the frozen manifest")
    family, pages = families[0], []
    inputs = {str(path): hashlib.sha256(raw).hexdigest()}
    for language in LANGUAGES:
        summary = family["pages"].get(language)
        if not isinstance(summary, dict) or summary.get("status") != "present":
            raise ValueError("Selected family has no frozen primary page for " + language)
        body_path = (census / summary["local_body_file"]).resolve()
        if not body_path.is_relative_to(census):
            raise ValueError("Frozen body path escapes census directory")
        body = body_path.read_bytes()
        digest = hashlib.sha256(body).hexdigest()
        if digest != summary["text_sha256"]:
            raise ValueError("Frozen holdout page changed: " + summary["page_id"])
        variants = [page for page in summary["variants"] if page["id"] == summary["page_id"]
                    and page["source"] == summary["source"]]
        if len(variants) != 1 or normalize_language(variants[0]["language"]) != language:
            raise ValueError("Frozen primary page metadata is absent, ambiguous or another language")
        page = dict(variants[0], text=body.decode("utf-8"), story_key=story_key)
        if termindex.page_story_key(page) != story_key:
            raise ValueError("Frozen page metadata does not reproduce the selected content identity")
        pages.append(page)
        inputs[str(body_path)] = digest
    return pages, inputs, manifest["manifest_sha256"]


def _verify_inputs(inputs, code_hashes):
    if any(_hash(Path(path)) != digest for path, digest in inputs.items()):
        raise ValueError("Frozen trial inputs changed during preparation")
    if _code_hashes() != code_hashes:
        raise ValueError("Trial preparation dependency closure changed during execution")


def _write_new_json(path, value):
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _run(census, output, story_key="event:132:2"):
    if output.exists():
        raise FileExistsError("Symmetric trial artifacts are frozen; choose a new output directory")
    code_hashes = _code_hashes()
    pages, inputs, manifest_sha = _load_family(census, story_key)
    groups = termindex.group_pages_by_story(pages)
    if set(groups) != {story_key} or len(groups[story_key]) != len(LANGUAGES):
        raise ValueError("Existing corpus usability gates did not preserve all five blind pages")
    output.mkdir(parents=True, exist_ok=False)
    store = output / "store"
    dbstore.initialize(store)
    for provider in sorted({page["source"] for page in pages}):
        dbstore.upsert_web_pages(store, provider, [page for page in pages if page["source"] == provider])
    selected, metrics = [], []
    for corpus_language in LANGUAGES:
        language = termindex._term_language(corpus_language)
        targets = [other for other in LANGUAGES if other != corpus_language]
        actual, meta = packets._prepare_scrub_review(store, groups, [story_key], [], {}, language, targets)
        if not actual or any(item.kind != "discovery" or item.candidates for item in actual):
            raise ValueError("Candidate-free preparation did not produce actual discovery-only work")
        item = actual[0]
        rows = item._context["rows"]
        if len(rows) != 8 or packets._DISCOVERY_ROWS != 8:
            raise ValueError(f"First actual default discovery packet for {language} has {len(rows)} rows, not eight")
        if (item._context["source_language"] != language
                or any(row["story_key"] != story_key or row["source"]["complete"] is not True for row in rows)):
            raise ValueError("Default discovery packet has another source identity or incomplete windows")
        scope = packets._read_scope(store, meta["scope_id"])
        if rows != scope["windows"][:8]:
            raise ValueError("Selected default packet does not preserve its first actual scope windows")
        selected.append(item)
        metrics.append(dict(language=language, corpus_language=corpus_language, item_id=item.id,
                            scope_id=meta["scope_id"], scope_file_sha256=_hash(packets._scope_path(store, meta["scope_id"])),
                            actual_default_packets=len(actual), selected_windows=len(rows),
                            source_only_rows_sha256=packets._digest([
                                dict(id=row["id"], story_key=row["story_key"], source=row["source"]) for row in rows]),
                            windows=[dict(id=row["id"], source_page_id=row["source"]["page_id"],
                                          source_page_sha256=row["source"]["sha256"], start=row["source"]["start"],
                                          end=row["source"]["end"], complete=row["source"]["complete"])
                                     for row in rows]))
    _verify_inputs(inputs, code_hashes)
    queued = review.enqueue(store, selected)
    packet_dir = output / "packets"
    task_dir = output / "internal-tasks"
    packet_dir.mkdir()
    task_dir.mkdir()
    artifacts = {}
    for item in selected:
        task_path = task_dir / (item.language + ".json")
        _write_new_json(task_path, [item.to_dict()])
        # Rendering discovery directly omits target views and candidate fields.
        # The full immutable task JSON is internal protocol input, not a blind
        # source-reader packet.
        text = "\n".join([f"## id={item.id} kind=discovery", *packets._render_context(item)]) + "\n"
        packet_path = packet_dir / (item.language + ".txt")
        with packet_path.open("x", encoding="utf-8") as stream:
            stream.write(text)
        artifacts[str(task_path.resolve())] = _hash(task_path)
        artifacts[str(packet_path.resolve())] = _hash(packet_path)
    with dbstore.connect(store) as conn:
        imported = conn.execute("SELECT COUNT(*) FROM web_pages").fetchone()[0]
        global_terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        entities = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    if imported != 5 or global_terms or entities or len(review.load_queue(store)) != 5:
        raise ValueError("Blind source trial did not preserve its isolated five-page/five-task boundary")
    _verify_inputs(inputs, code_hashes)
    report = dict(schema="sekaisync/symmetric-source-trial@1", story_key=story_key,
                  store=str(store.resolve()), source_only_packet_directory=str(packet_dir.resolve()),
                  internal_tasks_directory=str(task_dir.resolve()), internal_tasks_may_contain_target_contexts=True,
                  source_reader_inputs="packets/*.txt only", manifest_sha256=manifest_sha,
                  imported_pages=imported, discovery_packets=len(selected), source_windows=40,
                  default_rows=8, current_terms=global_terms, entities=entities,
                  labels_or_references_read=False, candidates_in_source_packets=False,
                  target_views_in_source_packets=False, control_added=False,
                  input_sha256=inputs, code_sha256=code_hashes, artifact_sha256=artifacts,
                  enqueue=queued, languages=metrics)
    _write_new_json(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--census-dir", type=Path, default=ROOT / "work/p0-exhaustive-20261001/census")
    parser.add_argument("--story-key", default="event:132:2")
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    report = _run(args.census_dir, args.output_directory, args.story_key)
    print(json.dumps(dict(story_key=report["story_key"], source_windows=report["source_windows"],
                          imported_pages=report["imported_pages"], discovery_packets=report["discovery_packets"],
                          languages=[entry["language"] for entry in report["languages"]],
                          packet_directory=report["source_only_packet_directory"]), indent=2))


if __name__ == "__main__":
    main()
