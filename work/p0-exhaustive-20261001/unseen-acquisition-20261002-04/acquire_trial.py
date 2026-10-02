"""Work-only normal source acquisition; registration and references gate submissions."""
from __future__ import annotations

import argparse, hashlib, importlib.util, json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("acquisition_registration", HERE / "register_trial.py")
registration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(registration)
b = registration.backend
ap, ar, db, spans, terms = b.packets, b.review, b.dbstore, b.span_subjects, b.termindex
LANGS = b.LANGS

def read(path):
    return json.loads(Path(path).read_bytes())

def put(path, raw):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
    return hashlib.sha256(raw).hexdigest()

def fresh(output, name):
    output = b.work_path(output)
    if not output.is_relative_to(HERE) or Path(name).is_absolute() or ".." in Path(name).parts:
        raise ValueError("acquisition outputs must remain below this work directory")
    stage = b.new_stage(output, name)
    stage.mkdir(parents=True)
    return stage

def packet(item, directory):
    path = directory / "source-packet.txt"
    digest = put(path, (ar.render_item(item) + "\n").encode("utf-8"))
    b.freeze(directory / "task.json", dict(item=item.to_dict(), packet_path=str(path.resolve()), packet_sha256=digest))
    return dict(packet_path=str(path.resolve()), packet_sha256=digest, item_id=item.id)

def state(store):
    with db.connect(store) as conn:
        if conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]:
            raise ValueError("unexpected global term publication")
        data = list(conn.iterdump())
    files = {str(path.relative_to(store)): b.sha(path) for path in Path(store).rglob("*") if path.is_file() and not path.name.startswith("sekaisync.db") and path.suffix != ".lock"}
    return b.digest(dict(database=data, files=files))

def receipts(store):
    with db.connect(store) as conn:
        rows = conn.execute("SELECT d.item_id,d.action,d.value,d.scope_json FROM review_decisions d JOIN review_queue q ON q.item_id=d.item_id WHERE q.status='resolved' AND d.action='accept'").fetchall()
    return {identity: dict(decision=action, value=value, review_context=json.loads(scope)["review_context"]) for identity, action, value, scope in rows}

def load(output):
    registration.verify()
    output = b.work_path(output)
    manifest = read(output / "prepared/manifest.json")
    if manifest["runner_sha256"] != b.sha(__file__) or manifest["registration_sha256"] != b.sha(HERE / "metadata-registration.json"):
        raise ValueError("runner or registration changed after preparation")
    for path, digest in manifest["artifacts"].items():
        if b.sha(path) != digest:
            raise ValueError("prepared artifact changed: " + path)
    for lang, config in manifest["languages"].items():
        expected = config["prepared_state_sha256"]
        for path in sorted((output / "submissions" / lang).glob("*/report.json"), key=lambda path: int(path.parent.name)):
            report = read(path)
            if report["state_enter"] != expected:
                raise ValueError("submission state chain changed")
            expected = report["state_after"]
        if state(Path(config["store"])) != expected:
            raise ValueError("trial store changed outside its normal submission chain")
    return output, manifest

def prepare(output, handoff_path):
    metadata = registration.verify()
    if (metadata.get("schema") != "work/unseen-proposition-acquisition-metadata@1"
            or metadata.get("fixed_source_windows") != 40 or metadata.get("source_or_target_bodies_read") is not False):
        raise ValueError("unsupported acquisition registration")
    handoff_path = b.work_path(handoff_path)
    handoff_raw = handoff_path.read_bytes()
    handoff = json.loads(handoff_raw)
    if (set(handoff) != {"schema", "registration_sha256", "pages"}
            or handoff["schema"] != "work/source-first-registered-body-handoff@1"
            or handoff["registration_sha256"] != b.sha(HERE / "metadata-registration.json") or set(handoff["pages"]) != set(LANGS)):
        raise ValueError("root body handoff does not bind this five-page registration")
    protocol = (HERE / "PROTOCOL.md").read_text(encoding="utf-8")
    start, end = "<!-- reference-contract:start -->", "<!-- reference-contract:end -->"
    if protocol.count(start) != 1 or protocol.count(end) != 1 or protocol.index(start) >= protocol.index(end):
        raise ValueError("exactly one isolated reference contract is required")
    contract = protocol.split(start)[1].split(end)[0].strip()
    family, pages = metadata["family"], []
    for lang in LANGS:
        observed, bound = handoff["pages"][lang], family["pages"][lang]
        if (set(observed) != {"source", "page_id", "language", "body_file", "text_sha256"}
                or any(observed[key] != bound[key] for key in ("source", "page_id", "language", "text_sha256"))):
            raise ValueError("body handoff altered registered page metadata")
        raw = b.work_path(observed["body_file"]).read_bytes()
        if hashlib.sha256(raw).hexdigest() != bound["text_sha256"]:
            raise ValueError("registered body bytes changed")
        page = dict(bound["metadata"], text=raw.decode("utf-8"), language=bound["metadata"].get("raw_language", bound["metadata"]["language"]))
        if terms.page_story_key(page) != family["story_key"] or terms._term_language(page["language"]) != lang:
            raise ValueError("registered page identity does not replay")
        pages.append(page)
    groups = terms.group_pages_by_story(pages)
    if set(groups) != {family["story_key"]} or len(groups[family["story_key"]]) != 5:
        raise ValueError("normal product rejected the registered five pages")
    stage, languages = fresh(output, "prepared"), {}
    put(stage / "handoff-original.json", handoff_raw)
    for lang in LANGS:
        directory, store = stage / lang, stage / lang / "store"
        db.initialize(store)
        for version in (2, 3):
            db.migrate_store(store, target_version=version, dry_run=False, backup_path=directory / f"pre-v{version}.db")
        for provider in sorted({page["source"] for page in pages}):
            db.upsert_web_pages(store, provider, [page for page in pages if page["source"] == provider])
        items, info = ap._prepare_scrub_review(store, groups, [family["story_key"]], [], {}, lang, [x for x in LANGS if x != lang])
        first = next(item for item in items if item._context.get("task") == "discovery")
        rows = [dict(id=row["id"], story_key=row["story_key"], source=row["source"]) for row in first._context["rows"]]
        if len(rows) != 8 or any(not row["source"]["complete"] for row in rows) or first.candidates:
            raise ValueError("first normal candidate-free discovery must contain eight complete rows; do not rerank")
        queued = ar.enqueue(store, [first])
        ar.export_for_agent(store, directory / "normal-export.txt", limit=0)
        first = next(item for item in ar.load_queue(store) if item.id == first.id)
        languages[lang] = dict(packet(first, directory), store=str(store.resolve()), scope_id=info["scope_id"], normal_enqueue=queued)
        b.freeze(directory / "visible-rows.json", rows)
        languages[lang]["reference_packet_sha256"] = put(directory / "reference-packet.txt", (contract + "\n\nsource_rows: " + json.dumps(dict(language=lang, rows=rows), ensure_ascii=False) + "\n").encode("utf-8"))
        languages[lang]["prepared_state_sha256"] = state(store)
    artifacts = {str(path.resolve()): b.sha(path) for path in stage.rglob("*") if path.is_file() and "store" not in path.parts and not path.name.startswith("pre-v")}
    b.freeze(stage / "manifest.json", dict(languages=languages, artifacts=artifacts, runner_sha256=b.sha(__file__),
        registration_sha256=b.sha(HERE / "metadata-registration.json"), handoff_sha256=hashlib.sha256(handoff_raw).hexdigest(), fixed_source_windows=40))
    return dict(prepared_languages=5, source_rows=40, target_relations=0)

def freeze_references(output, paths):
    output, manifest = load(output)
    if set(paths) != set(LANGS) or (output / "submissions").exists():
        raise ValueError("all five independent references must freeze before acquisition submissions")
    originals = {lang: b.work_path(paths[lang]).read_bytes() for lang in LANGS}
    for lang, raw in originals.items():
        value = json.loads(raw)
        rows = {row["id"]: row for row in read(output / "prepared" / lang / "visible-rows.json")}
        if set(value) != {"language", "units", "empty_rows"} or value["language"] != lang:
            raise ValueError("invalid independent reference schema")
        covered, empty, vectors = set(), set(), set()
        for unit in value["units"]:
            if set(unit) != {"evidence_id", "segments", "meaning", "rationale"} or not b.nonempty(unit["meaning"]) or not b.nonempty(unit["rationale"]):
                raise ValueError("reference unit lacks source meaning/rationale")
            row = rows[unit["evidence_id"]]
            if len(unit["segments"]) != 1:
                raise ValueError("frozen main-proposition protocol requires contiguous literal vectors")
            segment = unit["segments"][0]
            if segment["exact"] != segment["exact"].strip() or segment["exact"][-1:] in ".!?\u3002\uff01\uff1f":
                raise ValueError("reference violates frozen outer-whitespace/terminal-punctuation boundaries")
            subject = spans._literal(row["source"], row["story_key"], segment["exact"], unit["segments"])
            with db.connect(Path(manifest["languages"][lang]["store"])) as conn:
                ap._validate_subject_in_rows(conn, subject, [row])
            key = vector(unit["evidence_id"], unit["segments"])
            if key in vectors:
                raise ValueError("duplicate reference vector")
            vectors.add(key); covered.add(unit["evidence_id"])
        for row in value["empty_rows"]:
            if set(row) != {"evidence_id", "reason"} or row["evidence_id"] not in rows or row["evidence_id"] in empty or not b.nonempty(row["reason"]):
                raise ValueError("invalid or duplicate empty-row reason")
            empty.add(row["evidence_id"])
        if covered & empty or covered | empty != set(rows):
            raise ValueError("every frozen source row needs units XOR one empty reason")
    stage = fresh(output, "references-frozen")
    hashes = {lang: put(stage / (lang + ".json"), raw) for lang, raw in originals.items()}
    b.freeze(stage / "seal.json", dict(reference_sha256=hashes, manifest_sha256=b.sha(output / "prepared/manifest.json"), mechanically_checked_rows=40, semantic_certificate=False))
    return dict(frozen_languages=5, covered_rows=40)

def vector(row, segments):
    return row, tuple((part["start"], part["end"], part["exact"]) for part in segments)

def reference_gate(output):
    seal = read(output / "references-frozen/seal.json")
    if seal["manifest_sha256"] != b.sha(output / "prepared/manifest.json") or set(seal["reference_sha256"]) != set(LANGS):
        raise ValueError("reference gate manifest changed")
    for lang, digest in seal["reference_sha256"].items():
        if b.sha(output / "references-frozen" / (lang + ".json")) != digest:
            raise ValueError("frozen independent reference changed")
    return seal

def submit(output, lang, answer_path, completion_path, round_name):
    output, manifest = load(output); reference_gate(output)
    if (output / "STOP.json").exists() or lang not in LANGS or not round_name.isdigit():
        raise ValueError("trial stopped or invalid language/round")
    previous = [int(path.name) for path in (output / "submissions" / lang).glob("*") if path.is_dir()]
    if int(round_name) < 1 or previous and int(round_name) <= max(previous):
        raise ValueError("submission rounds must be positive and strictly increasing")
    raw, declaration = b.work_path(answer_path).read_bytes(), b.work_path(completion_path).read_bytes()
    stage = fresh(output, "submissions/" + lang + "/" + round_name)
    answer_sha = put(stage / "answer-original.json", raw); put(stage / "completion-original.json", declaration)
    try:
        done = json.loads(declaration)
        required = {"source_language", "allowed_input_path", "source_packet_sha256", "answer_sha256", "target_bodies_read", "reference_or_selected_units_read", "other_languages_read"}
        if set(done) != required or done["source_language"] != lang or done["answer_sha256"] != answer_sha or any(done[key] is not False for key in required if key.endswith("_read")):
            raise ValueError("invalid seven-field source-only completion")
        source = b.work_path(done["allowed_input_path"])
        if not source.is_relative_to(output) or lang not in source.relative_to(output).parts:
            raise ValueError("completion packet is outside this language's output tree")
        task = read(source.parent / "task.json"); item = ar.ReviewItem.from_dict(task["item"])
        if item._context.get("task") != "discovery" or item.language != lang or source != Path(task["packet_path"]) or b.sha(source) != done["source_packet_sha256"] or done["source_packet_sha256"] != task["packet_sha256"]:
            raise ValueError("completion does not bind a normal source discovery packet")
        store = Path(manifest["languages"][lang]["store"])
        state_enter = state(store)
        actual = next((queued for queued in ar.load_queue(store) if queued.id == item.id), None)
        if actual is None or actual.to_dict() != item.to_dict() or source.read_bytes() != (ar.render_item(actual) + "\n").encode("utf-8"):
            raise ValueError("packet is not the actual immutable queued normal task")
        judgments = ar.parse_judgments_text(raw.decode("utf-8"))
        if len(judgments) != 1 or judgments[0].get("id") != item.id or judgments[0].get("decision") != "accept" or set(judgments[0]) - {"id", "decision", "terms", "subjects", "rationale", "agent"}:
            raise ValueError("submit permits only one normal source discovery answer; target relations forbidden")
        result = ar.submit_judgments(store, judgments)
        b.freeze(stage / "submit-result.json", result)
        if result.get("errors") or result.get("unknown") or result.get("accepted") != 1:
            raise ValueError("real normal submission failed: " + json.dumps(result, ensure_ascii=False))
        receipt = receipts(store).get(item.id)
        if not receipt or receipt.get("decision") != "accept":
            raise ValueError("normal accepted receipt absent")
        ar.export_for_agent(store, stage / "normal-export.txt", limit=0)
        followups = [packet(child, stage / "followups" / child.id.removeprefix("arp:")) for child in ar.load_queue(store) if child._context.get("task") == "discovery"]
        before = state(store); replay = ar.submit_judgments(store, judgments); after = state(store)
        if before != after or receipts(store).get(item.id) != receipt or replay.get("errors") or any(replay.get(key) for key in ("accepted", "unknown", "decisions_added", "queue_resolved", "queue_superseded", "followup_packets")):
            raise ValueError("replay was not inert")
        b.freeze(stage / "report.json", dict(status="PASS", item_id=item.id, actual_receipt=receipt, answer_sha256=answer_sha, replay=replay, state_enter=state_enter, state_before=before, state_after=after, followups=followups))
        return dict(language=lang, status="PASS", followups=len(followups), inert_replay=True)
    except Exception as error:
        report = dict(status="FAIL", language=lang, stage=str(stage), error=str(error), answer_sha256=answer_sha)
        b.freeze(stage / "failure.json", report); b.freeze(output / "STOP.json", report)
        raise

def score(output, name):
    output, manifest = load(output); reference_gate(output)
    results = {}
    for lang in LANGS:
        config = manifest["languages"][lang]; store = Path(config["store"])
        rows = read(output / "prepared" / lang / "visible-rows.json")
        accepted, initial = {}, set()
        for identity, receipt in receipts(store).items():
            context = receipt.get("review_context", {})
            if context.get("schema") != ap._SCHEMA or context.get("source_language") != lang or context.get("task") != "discovery" or context.get("scope_id") != config["scope_id"] or [dict(id=row["id"], story_key=row["story_key"], source=row["source"]) for row in context["rows"]] != rows:
                continue
            value = json.loads(receipt["value"])
            for subject in value.get("subjects", []) if isinstance(value, dict) else []:
                with db.connect(store) as conn:
                    ap._validate_subject_in_rows(conn, subject, rows)
                keys = {vector(row["id"], subject["source"]["segments"]) for row in rows if ap._subject_in_view(subject, row["source"], row["story_key"])}
                for key in keys:
                    accepted.setdefault(key, set()).add(subject["id"])
                if identity == config["item_id"]:
                    initial.update(keys)
        reference = read(output / "references-frozen" / (lang + ".json"))
        units = {vector(unit["evidence_id"], unit["segments"]): unit for unit in reference["units"]}
        expected, found = set(units), set(accepted)
        positions = lambda key: {index for start, end, exact in key[1] for index in range(start, end)}
        fragment = lambda key: set().union(*(positions(part) for part in found if part[0] == key[0] and positions(part) < positions(key)))
        pending = [dict(id=item.id, continuation=bool(item._context.get("continuation")), audit=item._context.get("source_boundary_audit", {}).get("stage")) for item in ar.load_queue(store) if item._context.get("task") == "discovery"]
        results[lang] = dict(denominator=len(expected), initial_exact=len(expected & initial), final_exact=len(expected & found),
            matched_units=[dict(unit=units[key], subject_ids=sorted(accepted[key]), initial=key in initial) for key in sorted(expected & found)], missed_units=[units[key] for key in sorted(expected - found)],
            fragment_union_diagnostic=sum(positions(key) <= fragment(key) for key in expected - found), overbroad_diagnostic=sum(any(part[0] == key[0] and positions(key) < positions(part) for part in found) for key in expected - found),
            empty_rows=reference["empty_rows"], rejected_answers=[read(path) for path in (output / "submissions" / lang).glob("*/failure.json")], pending_source_work=pending, global_terms=0, state_sha256=state(store))
    if not name.replace("-", "").isalnum():
        raise ValueError("score name must be a simple fresh leaf")
    path = fresh(output, "scores/" + name) / "report.json"
    b.freeze(path, dict(languages=results, denominator=sum(v["denominator"] for v in results.values()), initial_exact=sum(v["initial_exact"] for v in results.values()), final_exact=sum(v["final_exact"] for v in results.values()), stopped=(output / "STOP.json").exists(), semantic_certificate=False))
    return dict(report=str(path), languages=5)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "freeze-reference", "submit", "score"))
    parser.add_argument("--output", type=Path, default=HERE)
    parser.add_argument("--language", choices=LANGS)
    for option in ("body-handoff", "answer", "completion"):
        parser.add_argument("--" + option, type=Path)
    parser.add_argument("--round", default="01"); parser.add_argument("--score-name", default="score-01")
    parser.add_argument("--reference", action="append", default=[], help="LANG=path, exactly one independent frozen answer per language")
    args = parser.parse_args()
    if args.stage == "prepare":
        result = prepare(args.output, args.body_handoff)
    elif args.stage == "freeze-reference":
        pairs = [value.split("=", 1) for value in args.reference]
        if len(pairs) != 5 or len(dict(pairs)) != 5:
            parser.error("five distinct LANG=reference paths required")
        result = freeze_references(args.output, dict(pairs))
    elif args.stage == "submit":
        result = submit(args.output, args.language, args.answer, args.completion, args.round)
    else:
        result = score(args.output, args.score_name)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
