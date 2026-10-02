"""Run a frozen, label-assisted eight-expression typed protocol diagnostic.

Source primary segments come from an existing independent machine reference.
Target judgments come from a separately frozen raw-turn review, not reference
target spans. Existing export/submit and Core interfaces are exercised in a new
isolated store. This is protocol verification, never a blind accuracy gain.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _code_hashes(root):
    paths = [root / "scripts/prepare_scraper_span_diagnostics.py", *sorted((root / "sekaisync").rglob("*.py"))]
    return {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths}


_IMPORT_HASHES = _code_hashes(ROOT)

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as ledger, span_subjects, termindex as ti
from sekaisync.core import SekaiSyncCore


def _json_file(path):
    raw = path.read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def _unique_location(text, exact):
    if not isinstance(exact, str) or not exact:
        raise ValueError("review must specify one unique complete raw turn or fragment, not a first-match guess")
    start = text.find(exact)
    if start < 0 or text.find(exact, start + 1) >= 0:
        raise ValueError("review must specify one unique complete raw turn or fragment, not a first-match guess")
    return start


def _target_parts(page, review):
    turn = review["target_turn"]
    base = _unique_location(page["text"], turn)
    fragments = review.get("target_parts")
    if not isinstance(fragments, list) or not fragments:
        raise ValueError("target review requires explicitly selected raw fragments")
    result, previous = [], 0
    for exact in fragments:
        start = _unique_location(turn, exact)
        end = start + len(exact)
        if start < previous:
            raise ValueError("target fragments must retain their raw order without overlap")
        result.append(dict(start=base + start, end=base + end, exact=exact))
        previous = end
    return result


def _load_inputs(manifest_path, reference_path, target_path):
    manifest, manifest_hash = _json_file(manifest_path)
    reference, reference_hash = _json_file(reference_path)
    reviews, target_hash = _json_file(target_path)
    story_key = reference["story_key"]
    story = next(story for story in manifest["stories"] if story["story_key"] == story_key)
    pages = {}
    for language, metadata in story["pages"].items():
        body = (manifest_path.parent / metadata["local_body_file"]).resolve()
        if not body.is_relative_to(manifest_path.parent.resolve()):
            raise ValueError("frozen body path leaves its manifest directory")
        text = body.read_bytes().decode("utf-8")
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != metadata["text_sha256"]:
            raise ValueError("frozen corpus body hash changed")
        variant = next((entry for entry in metadata.get("variants", [])
                        if entry["id"] == metadata["page_id"] and entry["source"] == metadata["source"]), {})
        pages[ledger._language(language)] = dict(variant, id=metadata["page_id"], source=metadata["source"],
                                                 language=metadata["language"], kind=variant.get("kind", "event_story"),
                                                 trust=variant.get("trust", "B"), text=text)
    sources = []
    for annotation in reference["annotations"]:
        primary = annotation["span_groups"][annotation["primary_occurrence_index"]]
        if len(primary["segments"]) < 2:
            continue
        language = ledger._language(annotation["language"])
        page = pages[language]
        if (annotation["source"] != page["source"] or annotation["page_id"] != page["id"]
                or annotation["text_sha256"] != hashlib.sha256(page["text"].encode("utf-8")).hexdigest()):
            raise ValueError("reference source does not bind the frozen corpus page")
        segments = [{key: part[key] for key in ("start", "end", "exact")} for part in primary["segments"]]
        view = dict(source=page["source"], page_id=page["id"], language=language, start=0,
                    end=len(page["text"]), text=page["text"], sha256=annotation["text_sha256"])
        subject = span_subjects._segmented(view, story_key, segments)
        sources.append(dict(annotation_id=annotation["id"], language=language, segments=segments,
                            legacy_representable=all(not gap.strip() for gap in subject["gap_text"])))
    if len(sources) != 8 or len({source["annotation_id"] for source in sources}) != 8:
        raise ValueError("this diagnostic requires exactly eight independently selected multisegment source units")
    reviewed = {review["annotation_id"]: review for review in reviews["cases"]}
    if (reviews.get("story_key") != story_key or len(reviewed) != len(reviews["cases"])
            or set(reviewed) != {source["annotation_id"] for source in sources}):
        raise ValueError("target review must cover exactly the eight source annotation identities")
    for source in sources:
        review = reviewed[source["annotation_id"]]
        target = ledger._language(review["target_language"])
        if target == source["language"] or review["kind"] not in ledger._RELATIONS:
            raise ValueError("target review must specify a different version and an explicit relation kind")
        if any(not isinstance(review.get(key), str) or not review[key].strip()
               for key in ("sense_key", "sense_gloss", "rationale")):
            raise ValueError("target review requires an explicit contextual sense and rationale")
        review = dict(review, target_language=target, target_segments=_target_parts(pages[target], review))
        source["review"] = review
    return dict(story_key=story_key, pages=pages, sources=sources,
                input_paths=dict(manifest=manifest_path, reference=reference_path, target_review=target_path),
                input_sha256=dict(manifest=manifest_hash, reference=reference_hash, target_review=target_hash),
                release_status=story.get("release_status"))


def _write(path, value):
    with path.open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _run(inputs, output):
    if output.exists():
        raise FileExistsError("diagnostic output is frozen; choose a new output directory")
    code_hashes = _code_hashes(ROOT)
    output.mkdir(parents=True, exist_ok=False)
    store = output / "store"
    dbstore.initialize(store)
    by_source = defaultdict(list)
    for page in inputs["pages"].values():
        by_source[page["source"]].append(page)
    for source, pages in by_source.items():
        dbstore.upsert_web_pages(store, source, pages)
    groups = ti.group_pages_by_story(inputs["pages"].values())
    prepared, judgments, selected_packets = {}, [], {}
    for language in sorted({source["language"] for source in inputs["sources"]}):
        targets = sorted({source["review"]["target_language"] for source in inputs["sources"]
                          if source["language"] == language})
        items, _ = ap._prepare_scrub_review(store, groups, [inputs["story_key"]], [], {}, language, targets)
        prepared[language] = [item for item in items if item._context["task"] == "discovery"]
    specs = defaultdict(list)
    for source in inputs["sources"]:
        matches = [(item, row) for item in prepared[source["language"]] for row in item._context["rows"]
                   if all(row["source"]["start"] <= part["start"] < part["end"] <= row["source"]["end"]
                          for part in source["segments"])]
        if len(matches) != 1:
            raise RuntimeError("source primary span must identify exactly one exported immutable row")
        item, row = matches[0]
        selected_packets[item.id] = item
        specs[item.id].append(dict(kind="segmented", evidence_id=row["id"], segments=source["segments"]))
    ar.enqueue(store, selected_packets.values())
    ar.export_for_agent(store, output / "discovery-export.txt", limit=0)
    for identity, entries in specs.items():
        judgments.append(dict(id=identity, decision="accept", terms=[], subjects=entries,
                              rationale="Label-assisted replay of the eight frozen primary source span vectors."))
    _write(output / "discovery-judgments.json", judgments)
    result = ar.submit_judgments(store, judgments)
    if result["errors"]:
        raise RuntimeError("typed source diagnostic failed: " + str(result["errors"]))
    ar.export_for_agent(store, output / "occurrence-export.txt", limit=0)
    occurrence_judgments, details = [], []
    for source in inputs["sources"]:
        review = source["review"]
        matches = [item for item in ar.load_queue(store) if item._context.get("task") == "occurrence"
                   and item.language == review["target_language"]
                   and item._context.get("subject", {}).get("source", {}).get("segments") == source["segments"]
                   and item._context["subject"]["source"]["language"] == source["language"]]
        if len(matches) != 1:
            raise RuntimeError("typed source did not create exactly one selected-language occurrence followup")
        item = matches[0]
        row = item._context["rows"][0]
        if not all(row["target"]["start"] <= part["start"] < part["end"] <= row["target"]["end"]
                   for part in review["target_segments"]):
            raise RuntimeError("independent target is outside the initial exported window; explicit expansion is required")
        proposal = dict(evidence_id=row["id"], source_segments=source["segments"],
                        target_segments=review["target_segments"], kind=review["kind"],
                        sense_key=review["sense_key"], sense_gloss=review["sense_gloss"], rationale=review["rationale"])
        occurrence_judgments.append(dict(id=item.id, decision="accept", relations=[proposal]))
        details.append(dict(annotation_id=source["annotation_id"], source_language=source["language"],
                            legacy_representable=source["legacy_representable"], subject=item._context["subject"],
                            target_review=review, occurrence_item_id=item.id))
    _write(output / "occurrence-judgments.json", occurrence_judgments)
    result = ar.submit_judgments(store, occurrence_judgments)
    if result["errors"]:
        raise RuntimeError("typed relation diagnostic failed: " + str(result["errors"]))
    core = SekaiSyncCore(store)
    languages = ["ja", "en", "zh_hans", "zh_hant", "ko"]
    with core.request_view():
        for detail in details:
            query = detail["subject"]["canonical"]
            result = core.term_penetrate(query, story_key=inputs["story_key"], languages=languages)
            detail["public_penetration"] = result
            detail["generic_core_query"] = core.query(query)
            if not result:
                raise RuntimeError("accepted typed relation is unavailable through the unchanged public consumer")
            source_entry = result["per_language"][detail["source_language"]]
            if result["term"]["names"] or source_entry["term"] or not source_entry.get("missing"):
                raise RuntimeError("segmented source was misrepresented as a continuous scalar name")
            target_key = next(language for language in languages
                              if ledger._language(language) == detail["target_review"]["target_language"])
            target_entry = result["per_language"][target_key]
            review = detail["target_review"]
            expected = review["target_segments"][0]["exact"] if review["kind"] == "lexical" and len(review["target_segments"]) == 1 else ""
            detail["target_scalar_expected"] = expected
            detail["target_scalar_actual"] = target_entry["term"]
            detail["selected_target_consumed"] = bool(target_entry["sentence"] and target_entry["term"] == expected
                                                       and (expected or review["kind"] in target_entry.get("note", "")))
            if not detail["selected_target_consumed"]:
                raise RuntimeError("selected typed target was not consumed with its correct scalar/relation type")
            source_anchor = detail["subject"]["source"]
            generic = [record for record in detail["generic_core_query"]["terms"]
                       if record.get("source") == "host-agent-occurrence"
                       and record["source_language"] == detail["source_language"]
                       and record["canonical"] == query
                       and any(evidence.get("page_id") == source_anchor["page_id"]
                               and evidence.get("start") == source_anchor["segments"][0]["start"]
                               and evidence.get("end") == source_anchor["segments"][-1]["end"]
                               for evidence in record["evidence"])]
            if len(generic) != 1 or generic[0]["names"]:
                raise RuntimeError("generic Core.query did not preserve this typed source as a separate scoped record")
            generic_target = next((position for position in generic[0]["positions"]
                                   if ledger._language(position["language"]) == review["target_language"]), {})
            detail["generic_target_consumed"] = bool(generic_target.get("sentence")
                                                       and generic_target.get("term") == expected
                                                       and (expected or review["kind"] in generic_target.get("note", "")))
            if not detail["generic_target_consumed"]:
                raise RuntimeError("generic Core.query did not consume the target through the existing positions field")
    with dbstore.connect(store) as conn:
        global_terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        relations = ledger._read_relations(conn)
    if global_terms:
        raise RuntimeError("typed diagnostic unexpectedly published global terms")
    if _code_hashes(ROOT) != code_hashes:
        raise RuntimeError("diagnostic code changed during execution; no report is published")
    for name, path in inputs["input_paths"].items():
        if hashlib.sha256(path.read_bytes()).hexdigest() != inputs["input_sha256"][name]:
            raise RuntimeError("frozen diagnostic input changed; no report is published")
    report = dict(schema="sekaisync/label-assisted-span-protocol-diagnostic@1", story_key=inputs["story_key"],
                  label_assisted=True, semantic_gold=False, blind_accuracy_gain=None,
                  source_units=len(details), formerly_unsupported=sum(not row["legacy_representable"] for row in details),
                  already_legacy_representable=sum(row["legacy_representable"] for row in details),
                  accepted_relations=len(relations), selected_targets_consumed=sum(row["selected_target_consumed"] for row in details),
                  generic_query_targets_consumed=sum(row["generic_target_consumed"] for row in details),
                  lexical_target_scalars=sum(bool(row["target_scalar_actual"]) for row in details),
                  source_scalar_names=0, global_terms=global_terms, pending_followups=len(ar.load_queue(store)),
                  original_release_status=inputs["release_status"],
                  release_scope="Original frozen manifest; isolated store does not replicate masterdata release tables",
                  input_sha256=inputs["input_sha256"], code_sha256=code_hashes,
                  artifact_sha256={name: hashlib.sha256((output / name).read_bytes()).hexdigest()
                                   for name in ("discovery-export.txt", "discovery-judgments.json", "occurrence-export.txt", "occurrence-judgments.json")},
                  details=details)
    _write(output / "report.json", report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--target-review", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    if _code_hashes(ROOT) != _IMPORT_HASHES:
        raise RuntimeError("diagnostic implementation changed after import; restart the process")
    inputs = _load_inputs(args.manifest, args.reference, args.target_review)
    report = _run(inputs, args.output_directory)
    print(json.dumps({key: value for key, value in report.items()
                      if key not in {"details", "code_sha256", "artifact_sha256"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
