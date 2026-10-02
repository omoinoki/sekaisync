"""Prospective work-only benchmark; public product validators are never replaced."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
OLD = ROOT / "work/p0-exhaustive-20261001/unseen-acquisition-20261002-04"
spec = importlib.util.spec_from_file_location("sealed_acquisition_helpers_261002", OLD / "acquire_trial.py")
acq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acq)
b, ap, ar, db, spans, terms = acq.b, acq.ap, acq.ar, acq.db, acq.spans, acq.terms
LANGS = b.LANGS


def read(path):
    return json.loads(Path(path).read_bytes())


def put(path, value):
    raw = value if isinstance(value, bytes) else (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    return acq.put(path, raw)


def fresh(name):
    path = HERE / name
    if path.exists():
        raise FileExistsError("retain original/partial stage: " + str(path))
    path.mkdir(parents=True)
    return path


def hashes():
    paths = [Path(__file__), HERE / "PROTOCOL.md", HERE / "baseline_callable.py",
                                                HERE / "ROOT_FIXTURE_IDENTITY_REVIEW.md", OLD / "acquire_trial.py",
                                                OLD / "register_trial.py", acq.registration.BACKEND]
    paths.extend(OLD.parent / ("unseen-acquisition-20261002-" + number) / "register_trial.py" for number in ("02", "03"))
    return {str(path): b.sha(path) for path in paths}


def check(values):
    for path, digest in values.items():
        if b.sha(path) != digest:
            raise ValueError("frozen input changed: " + path)


def closure():
    value = read(HERE / "product-closure.json")
    check(value["input_sha256"])
    report = read(value["report"])
    if report.get("code_sha256") != b.product_hashes(ROOT, report.get("code_sha256")):
        raise ValueError("fresh product snapshot drifted; obtain new full PASS before formal stages")
    return value


def registration():
    closure()
    value = read(HERE / "metadata-registration.json")
    check(value["input_sha256"])
    if b.sha(HERE / "metadata-registration.json") != read(HERE / "metadata-registration-seal.json")["registration_sha256"]:
        raise ValueError("registration changed")
    return value


def metadata(stage, args):
    if stage == "close-product":
        if not args.root_explicit_passed or not args.closure_report:
            raise ValueError("Root must explicitly provide the fresh exact-code full PASS report")
        path = b.work_path(args.closure_report)
        report = read(path)
        canonical = ["-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]
        if (report.get("schema") != "work/scraper-261002-validation@1" or report.get("status") != "PASS"
                or report.get("tests_run") != 2081 or report.get("exit_code") != 0
                or report.get("product_unchanged") is not True or report.get("changed_files") != []
                or report.get("command", [])[1:] != canonical
                or report.get("code_sha256") != b.product_hashes(ROOT, report.get("code_sha256"))):
            raise ValueError("actual canonical 2081-test unchanged exact-code full PASS required")
        inputs = {str(path): b.sha(path), **hashes()}
        for filename, field in (("hashes-before.json", "before_manifest_sha256"), ("hashes-after.json", "after_manifest_sha256"),
                                ("stdout.txt", "stdout_sha256"), ("stderr.txt", "stderr_sha256")):
            artifact = path.parent / filename
            if b.sha(artifact) != report[field]:
                raise ValueError("full PASS manifest/log hash changed")
            inputs[str(artifact)] = report[field]
        if read(path.parent / "hashes-before.json") != report["code_sha256"] or read(path.parent / "hashes-after.json") != report["code_sha256"]:
            raise ValueError("full PASS before/after code manifests disagree")
        archive = b.work_path(report["product_archive"])
        if b.sha(archive) != report["product_archive_sha256"]:
            raise ValueError("full PASS exact-product archive changed")
        with zipfile.ZipFile(archive) as bundle:
            if bundle.testzip() is not None or set(bundle.namelist()) != set(report["code_sha256"]) or any(hashlib.sha256(bundle.read(name)).hexdigest() != digest for name, digest in report["code_sha256"].items()):
                raise ValueError("full PASS archive inventory/bytes differ from actual tested code")
        inputs[str(archive)] = report["product_archive_sha256"]
        validation_runner = ROOT / "work/scraper-261002/run_validation.py"
        if b.sha(validation_runner) != report["runner_sha256"]:
            raise ValueError("canonical validation runner changed")
        inputs[str(validation_runner)] = report["runner_sha256"]
        put(HERE / "product-closure.json", dict(report=str(path), root_explicit_passed=True,
            input_sha256=inputs))
        return dict(closure_bound=True, tests_run=report["tests_run"], body_reads=0)
    if stage == "preview":
        excluded, priors, seals = acq.registration.prior_metadata()
        latest = read(OLD / "metadata-registration.json")
        if latest["schema"] != acq.registration.SCHEMA or latest["source_or_target_bodies_read"] is not False:
            raise ValueError("latest prior input must be metadata-only original registration")
        latest_path, latest_seal = OLD / "metadata-registration.json", OLD / "metadata-registration-seal.json"
        if read(latest_seal)["registration_sha256"] != b.sha(latest_path):
            raise ValueError("latest prior registration seal changed")
        excluded = sorted(set(excluded) | {b.event_family(latest["family"]["story_key"]), "event:136"})
        census = OLD.parent / "census"
        candidates = b.metadata_universe(census, excluded)
        chosen, seen = [], set()
        for candidate in candidates:
            family = b.event_family(candidate["story_key"])
            if family not in seen:
                chosen.append(candidate)
                seen.add(family)
            if len(chosen) == 2:
                break
        if len(chosen) != 2:
            raise ValueError("two distinct eligible unseen families required; do not rerank")
        inputs = {**hashes(), **priors, **seals, str(latest_path): b.sha(latest_path), str(latest_seal): b.sha(latest_seal),
                  **{str(census / name): b.sha(census / name) for name in ("logical-units.jsonl", "census-index.sqlite")}}
        put(HERE / "metadata-plan.json", dict(schema="work/checkpoint261002-metadata-preview@1", families=chosen,
            excluded_content_families=excluded, seed=b.SEED, candidate_universe_canonical_sha256=b.digest(candidates),
            eligible_candidate_count=len(candidates), fixed_source_rows=80, fixed_target_obligations=200,
            source_or_target_bodies_read=False, formal_registration_authorized=False, input_sha256=inputs))
        return dict(stories=[x["story_key"] for x in chosen], excluded_families=excluded, body_reads=0)
    if stage == "register":
        if not args.root_authorized:
            raise ValueError("Root authorization required after preview review")
        closure()
        value = read(HERE / "metadata-plan.json")
        check(value["input_sha256"])
        as_of = args.as_of_utc or datetime.now(timezone.utc).isoformat()
        for family in value["families"]:
            try:
                family["usage_audit"] = b.audit_usage(family["story_key"], [ROOT / name for name in ("work", "scripts", "tests", "agents")])
            except ValueError as error:
                if not args.usage_exceptions:
                    raise
                exceptions_path = b.work_path(args.usage_exceptions)
                if exceptions_path != HERE / "ROOT_FIXTURE_IDENTITY_REVIEW.md" or family["story_key"].split(":")[1] != "2":
                    raise ValueError("only Root's exact reviewed metadata-only synthetic identity note applies") from error
                names = ("test_agent_subject_fallback.py", "test_census_scraper_area_talk.py", "test_entity_region_db.py", "test_integrity.py",
                         "test_occurrence_public_query.py", "test_progress.py", "test_scraper_corpus_census.py", "test_source_migrate.py", "test_web_browse_sql.py")
                archived = ("test_entity_region_db.py", "test_integrity.py", "test_progress.py", "test_source_migrate.py", "test_web_browse_sql.py")
                allowed_paths = [ROOT / "tests" / name for name in names] + [ROOT / "work/scraper-repair-20260929/baseline/tests" / name for name in archived]
                allowed = {str(path): b.sha(path) for path in allowed_paths}
                prefix = "selected family prior use or unreadable usage scope; do not rerank: "
                message = str(error)
                matches = message.removeprefix(prefix).splitlines()
                if not message.startswith(prefix) or not matches or set(matches) != set(allowed):
                    raise ValueError("usage exceptions must bind exactly all current metadata-only identity match paths; no unreadable scope bypass") from error
                family["usage_audit"] = dict(root_confirmed_synthetic_identity_only=True, match_paths=matches,
                    exception_record=str(exceptions_path), exception_record_sha256=b.sha(exceptions_path), original_audit_error=message)
                value["input_sha256"][str(exceptions_path)] = b.sha(exceptions_path)
                value["input_sha256"].update(allowed)
            family["release"] = b.census_release(OLD.parent / "census", family, as_of)
        value.update(schema="work/checkpoint261002-metadata-registration@1", as_of_utc=as_of,
                     formal_registration_authorized=True)
        value["input_sha256"].update({str(HERE / "metadata-plan.json"): b.sha(HERE / "metadata-plan.json"),
                                     str(HERE / "product-closure.json"): b.sha(HERE / "product-closure.json")})
        digest = put(HERE / "metadata-registration.json", value)
        put(HERE / "metadata-registration-seal.json", dict(registration_sha256=digest))
        return dict(stories=[x["story_key"] for x in value["families"]], body_reads=0)
    value = registration()
    if not args.root_authorized:
        raise ValueError("Root must authorize actual body capture")
    directory = fresh("body-handoff")
    pages = []
    with sqlite3.connect(Path(args.database).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        for family in value["families"]:
            for lang, metadata in family["pages"].items():
                row = conn.execute("SELECT text,text_hash FROM web_pages WHERE source=? AND id=?",
                                   (metadata["source"], metadata["page_id"])).fetchone()
                if not row or row[1] != metadata["text_sha256"]:
                    raise ValueError("registered body missing/stale; no substitution")
                path = directory / (family["story_key"].replace(":", "-") + "-" + lang + ".txt")
                digest = put(path, row[0].encode())
                if digest != metadata["text_sha256"]:
                    raise ValueError("registered raw hash mismatch")
                pages.append(dict(story_key=family["story_key"], language=lang, body_file=str(path),
                                  text_sha256=digest, metadata=metadata["metadata"]))
    put(directory / "handoff.json", dict(registration_sha256=b.sha(HERE / "metadata-registration.json"), pages=pages))
    return dict(captured_pages=len(pages), production_database_writes=0)


def manifest():
    registration()
    value = read(HERE / "prepared/manifest.json")
    check(value["input_sha256"])
    for lang, config in value["languages"].items():
        expected = config["prepared_state_sha256"]
        for path in sorted((HERE / "submissions" / lang).glob("*/report.json")):
            report = read(path)
            if report["state_enter"] != expected:
                raise ValueError("normal submission chain changed")
            expected = report["state_after"]
        if acq.state(Path(config["store"])) != expected:
            raise ValueError("trial store changed outside normal submission chain")
    return value


def packet(directory, name, items):
    path = directory / (name + ".txt")
    raw = "\n".join(ar.render_item(item) + "\n" for item in items).encode()
    digest = put(path, raw)
    put(directory / (name + "-seal.json"), dict(packet_path=str(path), packet_sha256=digest,
                                                items=[item.to_dict() for item in items]))
    return dict(packet_path=str(path), packet_sha256=digest)


def prepare():
    metadata = registration()
    handoff = read(HERE / "body-handoff/handoff.json")
    if handoff["registration_sha256"] != b.sha(HERE / "metadata-registration.json"):
        raise ValueError("registered body handoff changed")
    pages = []
    for observed in handoff["pages"]:
        bound = next(family["pages"][observed["language"]] for family in metadata["families"]
                     if family["story_key"] == observed["story_key"])
        if (observed["metadata"] != bound["metadata"] or observed["text_sha256"] != bound["text_sha256"]
                or b.sha(observed["body_file"]) != bound["text_sha256"]):
            raise ValueError("registered page metadata/body changed")
        pages.append(dict(bound["metadata"], text=Path(observed["body_file"]).read_bytes().decode("utf-8"),
                          language=bound["metadata"].get("raw_language", bound["metadata"]["language"])))
    if len(pages) != 10 or len(terms.group_pages_by_story(pages)) != 2:
        raise ValueError("exact ten pages/two stories required")
    directory, configs = fresh("prepared"), {}
    protocol = (HERE / "PROTOCOL.md").read_text(encoding="utf-8")
    contract = protocol.split("<!-- reference-contract:start -->")[1].split("<!-- reference-contract:end -->")[0].strip()
    for lang in LANGS:
        local, store = directory / lang, directory / lang / "store"
        db.initialize(store)
        for version in (2, 3):
            db.migrate_store(store, target_version=version, dry_run=False, backup_path=local / f"pre-v{version}.db")
        for provider in sorted({page["source"] for page in pages}):
            db.upsert_web_pages(store, provider, [page for page in pages if page["source"] == provider])
        selected, rows = [], []
        for family in metadata["families"]:
            group = terms.group_pages_by_story([page for page in pages if terms.page_story_key(page) == family["story_key"]])
            items, _ = ap._prepare_scrub_review(store, group, [family["story_key"]], [], {}, lang, [x for x in LANGS if x != lang])
            first = next(item for item in items if item._context.get("task") == "discovery")
            visible = [dict(id=row["id"], story_key=row["story_key"], source=row["source"]) for row in first._context["rows"]]
            if len(visible) != 8 or any(not row["source"]["complete"] for row in visible) or first.candidates:
                raise ValueError("first normal packet does not expose exactly eight complete candidate-free rows")
            selected.append(first)
            rows.extend(visible)
        ar.enqueue(store, selected)
        actual = {item.id: item for item in ar.load_queue(store)}
        selected = [actual[item.id] for item in selected]
        ar.export_for_agent(store, local / "normal-export.txt", limit=0)
        host = packet(local, "source-host", selected)
        put(local / "source-host-instructions.txt", (
            "Read only source-host.txt. Use existing supported typed subjects for every meaningful exact source occurrence/layer; "
            "terms are optional diagnostics, not a replacement for source geometry. Enumerate complete meaningful speech acts and "
            "their meaningful nominal/predicate/function/discontinuous inner layers using original raw Unicode offsets. "
            "Read no references, selected reference units, targets or other languages. Return ordinary unchanged agent_review judgments.\n").encode())
        put(local / "visible-rows.json", rows)
        reference = (contract + "\n\nsource_rows: " + json.dumps(dict(language=lang, rows=rows), ensure_ascii=False) + "\n").encode()
        ref_hash = put(local / "source-reference.txt", reference)
        configs[lang] = dict(host, reference_packet=str(local / "source-reference.txt"), reference_packet_sha256=ref_hash,
                             store=str(store), initial_item_ids=[item.id for item in selected], prepared_state_sha256=acq.state(store))
    artifact_hashes = {str(path): b.sha(path) for path in directory.rglob("*") if path.is_file()
                      and "store" not in path.parts and not path.name.startswith("pre-v")}
    artifact_hashes.update({str(HERE / "body-handoff/handoff.json"): b.sha(HERE / "body-handoff/handoff.json"), **hashes()})
    put(directory / "manifest.json", dict(languages=configs, fixed_source_rows=80, pages=pages, input_sha256=artifact_hashes))
    return dict(source_rows=80, source_languages=5, actual_normal_tasks=10)


def grounded_subject(row, segments):
    view = row["source"]
    if all(not view["text"][left["end"] - view["start"]:right["start"] - view["start"]].strip() for left, right in zip(segments, segments[1:])):
        canonical = view["text"][segments[0]["start"] - view["start"]:segments[-1]["end"] - view["start"]]
        return spans._literal(view, row["story_key"], canonical, segments)
    return spans._segmented(row["source"], row["story_key"], segments)


def refs(kind):
    seal = read(HERE / (kind + "-references/seal.json"))
    check(seal["input_sha256"])
    return seal


def freeze_references(kind, paths):
    value = manifest()
    if set(paths) != set(LANGS):
        raise ValueError("exactly five independent reference originals required")
    if kind == "source" and (HERE / "submissions").exists():
        raise ValueError("freeze all source references before source-host submission")
    originals = {lang: b.work_path(path).read_bytes() for lang, path in paths.items()}
    total = 0
    selection = read(HERE / "targets/selection.json") if kind == "target" else None
    if kind == "target" and any(read(path)["phase"] == "target" for path in (HERE / "submissions").glob("*/*/report.json")):
        raise ValueError("target references must freeze before target-host submission")
    for lang, raw in originals.items():
        reference = json.loads(raw)
        if reference["language"] != lang:
            raise ValueError("independent reference language mismatch")
        if kind == "source":
            rows = {row["id"]: row for row in read(HERE / "prepared" / lang / "visible-rows.json")}
            seen, covered, empty = set(), set(), set()
            for unit in reference["units"]:
                if set(unit) != {"evidence_id", "segments", "category", "meaning", "rationale"} or not all(b.nonempty(unit[key]) for key in ("category", "meaning", "rationale")):
                    raise ValueError("source reference unit lacks exact shape/source-only meaning")
                row = rows[unit["evidence_id"]]
                subject = grounded_subject(row, unit["segments"])
                with db.connect(Path(value["languages"][lang]["store"])) as conn:
                    ap._validate_subject_in_rows(conn, subject, [row])
                key = acq.vector(unit["evidence_id"], unit["segments"])
                if key in seen:
                    raise ValueError("duplicate independent source vector")
                seen.add(key)
                covered.add(unit["evidence_id"])
            for row in reference["empty_rows"]:
                if set(row) != {"evidence_id", "reason"} or row["evidence_id"] not in rows or row["evidence_id"] in empty or not b.nonempty(row["reason"]):
                    raise ValueError("invalid empty-row declaration")
                empty.add(row["evidence_id"])
            if covered & empty or covered | empty != set(rows):
                raise ValueError("every independent source row needs units XOR empty reason")
            for story in {row["story_key"] for row in rows.values()}:
                if sum(rows[unit["evidence_id"]]["story_key"] == story for unit in reference["units"]) < 5:
                    raise ValueError("fewer than five natural reference units in a family/language")
            total += len(seen)
        else:
            expected = {item["obligation_id"]: item for item in selection["obligations"] if item["source_language"] == lang}
            records = {record["obligation_id"]: record for record in reference["records"]}
            if len(records) != len(reference["records"]) or set(records) != set(expected):
                raise ValueError("target references must cover every actual captured obligation exactly once")
            equivalence = {}
            for identity, record in records.items():
                if type(record.get("source_sense_equivalent")) is not bool or not record["alternatives"]:
                    raise ValueError("target reference needs at least one meaningful alternative")
                unit_id = expected[identity]["unit_id"]
                if unit_id in equivalence and equivalence[unit_id] != record["source_sense_equivalent"]:
                    raise ValueError("one frozen source-only meaning cannot have target-dependent source sense equivalence")
                equivalence[unit_id] = record["source_sense_equivalent"]
                if expected[identity]["source_meaning"] is None and record["source_sense_equivalent"] is not False:
                    raise ValueError("missing source-host sense cannot be declared equivalent")
                for alternative in record["alternatives"]:
                    if (set(alternative) != {"kind", "target_segments", "rationale"}
                            or alternative["kind"] not in {"lexical", "paraphrase", "reference", "omitted"}
                            or not b.nonempty(alternative["rationale"])):
                        raise ValueError("invalid prospective target kind/vector alternative")
                    row = expected[identity]["reference_row"]
                    b.ledger._anchor(row["target"], row["story_key"], alternative["target_segments"])
                    if alternative["kind"] == "lexical":
                        spans._utterance(row["target"], alternative["target_segments"])
            total += len(records)
    if kind == "source" and total < 100:
        raise ValueError("natural source reference denominator below 100; invalid undersampled benchmark")
    directory = fresh(kind + "-references")
    input_hashes = {str(HERE / "prepared/manifest.json"): b.sha(HERE / "prepared/manifest.json"), **hashes()}
    if kind == "target":
        input_hashes[str(HERE / "targets/selection.json")] = b.sha(HERE / "targets/selection.json")
    for lang, raw in originals.items():
        path = directory / (lang + ".json")
        input_hashes[str(path)] = put(path, raw)
    if kind == "source":
        fixed = []
        for lang, raw in originals.items():
            rows = {row["id"]: row for row in read(HERE / "prepared" / lang / "visible-rows.json")}
            grouped = defaultdict(list)
            for unit in json.loads(raw)["units"]:
                story = rows[unit["evidence_id"]]["story_key"]
                basis = dict(language=lang, story_key=story, evidence_id=unit["evidence_id"], segments=unit["segments"])
                rank = hashlib.sha256(("261002-target|" + json.dumps(basis, ensure_ascii=False, sort_keys=True, separators=(",", ":"))).encode()).hexdigest()
                grouped[story].append((rank, b.digest(basis), unit))
            for story, candidates in sorted(grouped.items()):
                fixed.extend(dict(language=lang, story_key=story, rank=rank, unit=unit) for rank, _, unit in sorted(candidates)[:5])
        if len(fixed) != 50:
            raise ValueError("frozen pre-host source selection must contain exactly50 units")
        path = directory / "fixed-target-units.json"
        input_hashes[str(path)] = put(path, dict(selected=fixed, host_answers_read=False, fixed_directed_denominator=200))
    put(directory / "seal.json", dict(input_sha256=input_hashes, denominator=total, semantic_certificate=False))
    return dict(kind=kind, denominator=total, independent_originals_preserved=True)


def submit(phase, lang, answer_path, completion_path):
    value = manifest()
    refs("source" if phase == "source" else "target")
    if (HERE / "STOP.json").exists():
        raise ValueError("failed normal stage retained; trial stopped")
    raw, completion_raw = b.work_path(answer_path).read_bytes(), b.work_path(completion_path).read_bytes()
    completion = json.loads(completion_raw)
    path = b.work_path(completion["allowed_input_path"])
    if not path.is_relative_to(HERE) or lang not in path.relative_to(HERE).parts:
        raise ValueError("isolated packet does not belong to this source language")
    binding = read(path.with_name(path.stem + "-seal.json"))
    required_unread = ("target_bodies_read", "reference_or_selected_units_read", "other_languages_read") if phase == "source" else ("target_reference_read", "other_host_answers_read")
    if (completion.get("source_language") != lang or completion.get("answer_sha256") != hashlib.sha256(raw).hexdigest()
            or completion.get("packet_sha256") != b.sha(path) or binding["packet_sha256"] != b.sha(path)
            or any(completion.get(key) is not False for key in required_unread)):
        raise ValueError("raw host answer/packet completion binding or isolation declaration missing")
    judgments = ar.parse_judgments_text(raw.decode())
    permitted = {item["id"]: item for item in binding["items"]}
    if not judgments or len({judgment["id"] for judgment in judgments}) != len(judgments):
        raise ValueError("normal answer must contain unique actual task IDs")
    store = Path(value["languages"][lang]["store"])
    queued = {item.id: item for item in ar.load_queue(store)}
    expected_task = "discovery" if phase == "source" else "occurrence"
    for judgment in judgments:
        identity = judgment["id"]
        if identity not in permitted or identity not in queued or queued[identity].to_dict() != permitted[identity] or queued[identity]._context.get("task") != expected_task:
            raise ValueError("host answer is not for an actual unchanged exported task")
        if phase == "target":
            meanings = {item["subject_id"]: item["meaning"] for item in read(HERE / "host-senses" / (lang + ".json"))["units"]}
            subject_id = queued[identity]._context.get("subject", {}).get("id")
            selected = next(item for item in read(HERE / "targets/selection.json")["obligations"] if item.get("subject_id") == subject_id and item["target_language"] == queued[identity].language)
            if any(relation.get("sense_key") != selected["sense_key"] or relation.get("sense_gloss") != meanings[subject_id] for relation in judgment.get("relations", [])):
                raise ValueError("target host must copy its previously frozen own-source sense byte-for-byte, never target-derived or reference-derived sense")
    expected_raw = "\n".join(ar.render_item(ar.ReviewItem.from_dict(item)) + "\n" for item in binding["items"]).encode()
    if path.read_bytes() != expected_raw:
        raise ValueError("normal TXT is not unchanged product rendering")
    count = len(list((HERE / "submissions" / lang).glob("*/report.json"))) + 1
    directory = fresh(f"submissions/{lang}/{count:04d}")
    put(directory / "answer-original.json", raw)
    put(directory / "completion-original.json", completion_raw)
    enter = acq.state(store)
    try:
        result = ar.submit_judgments(store, judgments)
        put(directory / "submit-result.json", result)
        if result.get("errors") or result.get("unknown") or result.get("accepted") != len(judgments):
            raise ValueError("normal host submission rejected: " + json.dumps(result))
        actual = acq.receipts(store)
        if any(identity not in actual for identity in (judgment["id"] for judgment in judgments)):
            raise ValueError("normal accepted receipt missing")
        ar.export_for_agent(store, directory / "normal-export.txt", limit=0)
        selected_ids = {item.get("subject_id") for item in read(HERE / "targets/selection.json")["obligations"]} if phase == "target" else None
        relevant = [item for item in ar.load_queue(store) if item._context.get("task") == expected_task
                    and (phase == "source" or item._context.get("subject", {}).get("id") in selected_ids)]
        followup = packet(directory, phase + "-followups", relevant)
        before = acq.state(store)
        replay = ar.submit_judgments(store, judgments)
        after = acq.state(store)
        if before != after or replay.get("errors") or any(replay.get(key) for key in ("accepted", "unknown", "decisions_added", "queue_resolved", "queue_superseded", "followup_packets")):
            raise ValueError("normal host replay not inert")
        put(directory / "report.json", dict(status="PASS", phase=phase, language=lang, state_enter=enter,
            state_after=after, inert_replay=True, replay=replay, receipts={judgment["id"]: actual[judgment["id"]] for judgment in judgments},
            input_sha256={str(path): b.sha(path), str(directory / "answer-original.json"): b.sha(directory / "answer-original.json"),
                          str(directory / "completion-original.json"): b.sha(directory / "completion-original.json")}, followup=followup))
        if phase == "source":
            rows = read(HERE / "prepared" / lang / "visible-rows.json")
            subjects = {subject["id"]: subject for subject in acquired(manifest(), lang).values()}
            put(directory / "host-sense-packet.txt", ("Source-only host sense freeze: read only your own acquired vectors and original source rows below. "
                "No source references, other languages or targets. Return {language,units:[{subject_id,meaning,rationale}]} with every subject once.\n"
                + json.dumps(dict(language=lang, rows=rows, subjects=list(subjects.values())), ensure_ascii=False) + "\n").encode())
        return dict(accepted=len(judgments), inert_replay=True, followup=followup)
    except Exception as error:
        failure = dict(status="FAIL", phase=phase, language=lang, error=str(error), stage=str(directory))
        put(directory / "failure.json", failure)
        put(HERE / "STOP.json", failure)
        raise


def acquired(value, lang):
    rows = read(HERE / "prepared" / lang / "visible-rows.json")
    accepted = {}
    store = Path(value["languages"][lang]["store"])
    scopes = {read(Path(value["languages"][lang]["packet_path"]).with_name("source-host-seal.json"))["items"][index]["review_context"]["scope_id"]
              for index in (0, 1)}
    for receipt in acq.receipts(store).values():
        context = receipt["review_context"]
        if context.get("task") != "discovery" or context.get("source_language") != lang or context.get("scope_id") not in scopes:
            continue
        subjects = json.loads(receipt["value"])
        if not isinstance(subjects, dict):
            continue
        for subject in subjects.get("subjects", []):
            matching = [row for row in rows if ap._subject_in_view(subject, row["source"], row["story_key"])]
            with db.connect(store) as conn:
                ap._validate_subject_in_rows(conn, subject, matching)
            for row in matching:
                accepted[acq.vector(row["id"], subject["source"]["segments"])] = subject
    return accepted


def freeze_host_senses(paths):
    value = manifest()
    if set(paths) != set(LANGS) or (HERE / "targets").exists():
        raise ValueError("freeze all five source-only host senses before any target packet stage")
    originals = {}
    for lang, path in paths.items():
        raw = b.work_path(path).read_bytes()
        senses = json.loads(raw)
        subjects = {subject["id"]: subject for subject in acquired(value, lang).values()}
        observed = {unit["subject_id"]: unit for unit in senses["units"]}
        if senses["language"] != lang or len(observed) != len(senses["units"]) or set(observed) != set(subjects):
            raise ValueError("host meanings must bind every genuinely normally acquired own-source subject exactly once")
        if any(set(unit) != {"subject_id", "meaning", "rationale"} or not b.nonempty(unit["meaning"]) or not b.nonempty(unit["rationale"]) for unit in observed.values()):
            raise ValueError("host source meaning/rationale missing")
        originals[lang] = raw
    directory = fresh("host-senses")
    inputs = {}
    for lang, raw in originals.items():
        path = directory / (lang + ".json")
        inputs[str(path)] = put(path, raw)
    put(directory / "seal.json", dict(input_sha256=inputs, target_packets_read=False, reference_meanings_read=False))
    return dict(frozen_host_languages=5, target_packets_read=False)


def select_targets():
    value = manifest()
    refs("source")
    check(read(HERE / "host-senses/seal.json")["input_sha256"])
    directory, obligations, selected = fresh("targets"), [], []
    for lang in LANGS:
        rows = {row["id"]: row for row in read(HERE / "prepared" / lang / "visible-rows.json")}
        grouped = defaultdict(list)
        for fixed in read(HERE / "source-references/fixed-target-units.json")["selected"]:
            if fixed["language"] == lang:
                grouped[fixed["story_key"]].append((fixed["rank"], fixed["unit"]))
        found = acquired(value, lang)
        store = Path(value["languages"][lang]["store"])
        queued = ar.load_queue(store)
        source_tasks = read(Path(value["languages"][lang]["packet_path"]).with_name("source-host-seal.json"))["items"]
        scoped_rows = {row["id"]: row for task in source_tasks for row in ap._read_scope(store, task["review_context"]["scope_id"])["windows"]}
        host_senses = {unit["subject_id"]: unit for unit in read(HERE / "host-senses" / (lang + ".json"))["units"]}
        captured, meanings, reference_meanings = {}, [], []
        for story in sorted(grouped):
            for rank, unit in grouped[story]:
                identity = "unit:" + rank[:32]
                subject = found.get(acq.vector(unit["evidence_id"], unit["segments"]))
                host_meaning = host_senses[subject["id"]]["meaning"] if subject else None
                source = dict(unit_id=identity, source_language=lang, story_key=story, source_segments=unit["segments"],
                              source_context=rows[unit["evidence_id"]]["source"], source_canonical=grounded_subject(rows[unit["evidence_id"]], unit["segments"])["canonical"],
                              source_meaning=host_meaning, reference_meaning=unit["meaning"], sense_key="261002:" + identity)
                selected.append(source)
                for target in LANGS:
                    if target == lang:
                        continue
                    original = scoped_rows[unit["evidence_id"]]
                    reference_row = dict(id=original["id"], story_key=story, source=original["source"], target=original["targets"].get(target))
                    if not reference_row["target"]:
                        from sekaisync.wording_identity import _full_view
                        page = next(page for page in value["pages"] if terms.page_story_key(page) == story and terms._term_language(page["language"]) == target)
                        reference_row["target"] = _full_view(page)
                        reference_row["reference_only_full_page"] = True
                    obligation = dict(source, obligation_id=identity + "->" + target, target_language=target,
                                      reference_row=reference_row, status="pipeline_miss")
                    matches = [item for item in queued if subject and item._context.get("task") == "occurrence"
                               and item._context.get("subject", {}).get("id") == subject["id"] and item.language == target]
                    if len(matches) == 1 and len(matches[0]._context["rows"]) == 1:
                        item = matches[0]
                        captured[item.id] = item
                        local = directory / lang / "tasks" / item.id.removeprefix("arp:")
                        task_path = local / "task.json"
                        put(task_path, dict(item=item.to_dict()))
                        obligation["reference_row"] = item._context["rows"][0]
                        obligation.update(status="task_captured", task_id=item.id, task_path=str(task_path), subject_id=subject["id"])
                        host_source = {key: val for key, val in source.items() if key != "reference_meaning"}
                        meanings.append(dict(host_source, task_id=item.id, obligation_id=obligation["obligation_id"]))
                    reference_meanings.append(dict(obligation))
                    obligations.append(obligation)
        local = directory / lang
        host = packet(local, "target-host", list(captured.values()))
        put(local / "source-meanings.json", meanings)
        put(local / "reference-meanings.json", reference_meanings)
        put(local / "target-reference-contract.txt", (
            "Independent target reference: read only target-host.txt and reference-meanings.json. Independently verify host source meaning equivalence against raw source before target-host answers. "
            "Every fixed obligation needs alternatives, including pipeline misses; miss contexts are raw reference-only evidence, never fabricated host tasks. Missing host sense is source_sense_equivalent=false. "
            "Do not read target-host answers or any old labels. Return the prospective target-reference JSON "
            "described by PROTOCOL.md; independently justify typed exact vectors and alternatives before host answers.\n").encode())
    if len(selected) != 50 or len(obligations) != 200:
        raise ValueError("fixed50 source/200 target denominator changed")
    put(directory / "selection.json", dict(selected_sources=selected, obligations=obligations,
        input_sha256={str(HERE / "source-references/seal.json"): b.sha(HERE / "source-references/seal.json"),
                      str(HERE / "host-senses/seal.json"): b.sha(HERE / "host-senses/seal.json"), **hashes()}))
    return dict(source_units=50, obligations=200, tasks_captured=sum(item["status"] == "task_captured" for item in obligations))


def consumers(value, selected):
    from sekaisync.core import SekaiSyncCore
    result = {}
    for lang in LANGS:
        store = Path(value["languages"][lang]["store"])
        core = SekaiSyncCore(store)
        with db.connect(store) as conn:
            relations = b.ledger._read_relations(conn)
        per_source = []
        for source in selected:
            if source["source_language"] != lang:
                continue
            matching = [row for row in relations if row["source"]["segments"] == source["source_segments"] and row["story_key"] == source["story_key"]]
            if not matching:
                continue
            with db.connect(store) as conn:
                subject = conn.execute("SELECT payload_json FROM scraper_subjects WHERE id=?", (matching[0]["sense"]["term_id"],)).fetchone()
            canonical = json.loads(subject[0])["canonical"] if subject else ""
            with core.request_view():
                penetration = core.term_penetrate(canonical, story_key=source["story_key"], languages=list(LANGS))
                generic = core.query(canonical, include_web=False, limit=200)
                lookup = core.term_lookup(canonical, source_language=lang, languages=list(LANGS), limit=200)
            checks = []
            for relation in matching:
                target = relation["target_language"]
                pages = value["pages"]
                expected_scalar = terms._occurrence_lexical_scalar(relation["target"], {(page["source"], page["id"]): page for page in pages}) if relation["kind"] == "lexical" else ""
                hit_id = b.ledger._identity("occurrence:", [relation["source"]["id"], relation["sense"]["id"]])
                positions = [position for hit in generic.get("terms", []) if hit.get("id") == hit_id
                             for position in hit.get("positions", []) if b.ledger._language(position["language"]) == target]
                entry = penetration.get("per_language", {}).get(target) if penetration else None
                fragments_json = json.dumps(relation["target"]["segments"], ensure_ascii=False, separators=(",", ":"))
                note_ok = lambda row: bool(row and row.get("sentence") and row.get("term", "") == expected_scalar
                    and (expected_scalar or relation["kind"] in row.get("note", ""))
                    and (expected_scalar or relation["kind"] not in {"lexical", "paraphrase", "reference"} or fragments_json in row.get("note", "")))
                lookup_positions = [position for hit in lookup if hit.get("id") == hit_id for position in hit.get("positions", [])
                                    if b.ledger._language(position["language"]) == target]
                penetration_source = penetration.get("term", {}).get("evidence", []) if penetration else []
                source_specific = bool(penetration_source and penetration_source[0].get("start") == relation["source"]["segments"][0]["start"]
                                       and penetration_source[0].get("end") == relation["source"]["segments"][-1]["end"])
                checks.append(dict(relation_id=relation["id"], penetration_selected_this_occurrence=source_specific,
                    penetration_ambiguous=penetration is None, passed=any(note_ok(position) for position in positions)
                    and any(note_ok(position) for position in lookup_positions) and (not source_specific or note_ok(entry))))
            per_source.append(dict(unit_id=source["unit_id"], penetration=penetration, query=generic, lookup=lookup, checks=checks))
        result[lang] = per_source
    return result


def scalar_match(item, response, reference, pages):
    """Identical conservative scalar/context lifting rules for both implementations."""
    if not response:
        return False, "consumer_absent"
    source, target = item["source_context"], item["reference_row"]["target"]
    canonical = item["source_canonical"]
    positions = lambda text, exact: [index for index in range(len(text)) if text.startswith(exact, index)]
    page_map = {(page["source"], page["id"]): page["text"] for page in pages}
    source_page = page_map[(source["source"], source["page_id"])]
    target_page = page_map[(target["source"], target["page_id"])]
    if any(source_page[left["end"]:right["start"]].strip() for left, right in zip(item["source_segments"], item["source_segments"][1:])):
        return False, "source_geometry_not_scalar"
    if response.get("story_key") != item["story_key"]:
        return False, "wrong_story"
    per_language = response.get("per_language", {})
    source_entry = per_language.get(item["source_language"], {})
    source_sentence = source_entry.get("sentence", "")
    if source_sentence:
        sentence_positions = positions(source_page, source_sentence)
        word_positions = positions(source_sentence, canonical)
        if len(sentence_positions) != 1 or len(word_positions) != 1 or sentence_positions[0] + word_positions[0] != item["source_segments"][0]["start"]:
            return False, "wrong_or_ambiguous_source_context"
    elif positions(source_page, canonical) != [item["source_segments"][0]["start"]]:
        return False, "ambiguous_source_occurrence"
    entry = per_language.get(item["target_language"], {})
    scalar, sentence = entry.get("term", ""), entry.get("sentence", "")
    if not scalar or not sentence or entry.get("missing"):
        return False, "no_actual_scalar"
    sentence_matches, scalar_matches = positions(target_page, sentence), positions(sentence, scalar)
    if len(sentence_matches) != 1 or len(scalar_matches) != 1:
        return False, "ambiguous_or_unsupported_target_geometry"
    start = sentence_matches[0] + scalar_matches[0]
    if not target["start"] <= start < start + len(scalar) <= target["end"]:
        return False, "wrong_target_context"
    def equivalent(alternative):
        parts = alternative["target_segments"]
        if alternative["kind"] != "lexical" or not parts or any(target_page[left["end"]:right["start"]].strip() for left, right in zip(parts, parts[1:])):
            return False
        return start == parts[0]["start"] and start + len(scalar) == parts[-1]["end"] and scalar == target_page[start:start + len(scalar)]
    if not any(equivalent(alt) for alt in reference["alternatives"]):
        return False, "wrong_contextual_lexical_realization"
    return True, "contextual_scalar_match"


def score():
    value = manifest()
    refs("source")
    refs("target")
    selection = read(HERE / "targets/selection.json")
    check(selection["input_sha256"])
    baseline = read(HERE / "baseline/report.json")
    check(baseline["input_sha256"])
    source_results, references, actual = {}, {}, {}
    for lang in LANGS:
        reference = read(HERE / "source-references" / (lang + ".json"))
        expected = {acq.vector(unit["evidence_id"], unit["segments"]) for unit in reference["units"]}
        found = acquired(value, lang)
        source_results[lang] = dict(denominator=len(expected), correct=len(expected & set(found)))
        legacy = []
        for receipt in acq.receipts(Path(value["languages"][lang]["store"])).values():
            if receipt["review_context"].get("task") != "discovery":
                continue
            observed = json.loads(receipt["value"])
            legacy.extend(observed.get("terms", []) if isinstance(observed, dict) else observed if isinstance(observed, list) else [])
        source_results[lang]["legacy_terms_diagnostic"] = sorted(set(legacy))
        references.update({record["obligation_id"]: record for record in read(HERE / "target-references" / (lang + ".json"))["records"]})
        with db.connect(Path(value["languages"][lang]["store"])) as conn:
            actual[lang] = b.ledger._read_relations(conn)
    details, directions = [], defaultdict(lambda: dict(denominator=0, correct=0))
    for item in selection["obligations"]:
        correct, reason = False, "pipeline_miss"
        if item["status"] == "task_captured":
            matches = [row for row in actual[item["source_language"]] if row["sense"]["term_id"] == item["subject_id"]
                       and row["target_language"] == b.ledger._language(item["target_language"])
                       and row["sense"]["key"] == item["sense_key"] and row["sense"]["gloss"] == item["source_meaning"]]
            reference = references[item["obligation_id"]]
            correct = reference["source_sense_equivalent"] is True and len(matches) == 1 and any(matches[0]["kind"] == alt["kind"] and matches[0]["target"]["segments"] == alt["target_segments"]
                                               for alt in reference["alternatives"])
            reason = "strict_match" if correct else "missing_or_wrong_typed_vector"
        direction = item["source_language"] + "->" + item["target_language"]
        directions[direction]["denominator"] += 1
        directions[direction]["correct"] += int(correct)
        details.append(dict(obligation_id=item["obligation_id"], correct=correct, reason=reason))
    directory = fresh("scores")
    live = consumers(value, selection["selected_sources"])
    put(directory / "consumers.json", live)
    command = [sys.executable, str(Path(__file__)), "consumer", "--consumer-output", str(directory / "reload-consumers.json")]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    put(directory / "reload-process.json", dict(command=command, returncode=process.returncode, stdout=process.stdout, stderr=process.stderr))
    reload_ok = process.returncode == 0 and read(directory / "reload-consumers.json") == live
    consumer_ok = all(check["passed"] for sources in live.values() for source in sources for check in source["checks"])
    for path in (HERE / "submissions").glob("*/*/report.json"):
        check(read(path)["input_sha256"])
    source_n = sum(row["denominator"] for row in source_results.values())
    source_correct = sum(row["correct"] for row in source_results.values())
    target_correct = sum(row["correct"] for row in directions.values())
    current_common, baseline_common, common_details = 0, 0, []
    current_outputs = {source["unit_id"]: source["penetration"] for sources in live.values() for source in sources}
    baseline_outputs = baseline["consumer_outputs"]
    for item in selection["obligations"]:
        reference = references[item["obligation_id"]]
        current_ok, current_reason = scalar_match(item, current_outputs.get(item["unit_id"]), reference, value["pages"])
        baseline_ok, baseline_reason = scalar_match(item, baseline_outputs.get(item["unit_id"], {}).get("penetration"), reference, value["pages"])
        current_common += int(current_ok)
        baseline_common += int(baseline_ok)
        common_details.append(dict(obligation_id=item["obligation_id"], current=current_ok, baseline=baseline_ok,
                                   current_reason=current_reason, baseline_reason=baseline_reason))
    gates = dict(source_overall=source_correct / source_n >= .90,
                 source_each_language=all(row["correct"] / row["denominator"] >= .85 for row in source_results.values()),
                 target_overall=target_correct / 200 >= .90,
                 worst_twenty_direction=len(directions) == 20 and all(row["correct"] / row["denominator"] >= .80 for row in directions.values()),
                 improvement=(current_common - baseline_common) / 200 >= .20,
                 consumers=consumer_ok, separate_process_reload=reload_ok,
                 normal_receipts_and_replays=not (HERE / "STOP.json").exists())
    report = dict(schema="work/checkpoint261002-benchmark-score@1", status="PASS" if all(gates.values()) else "FAIL", gates=gates,
        source=source_results, source_denominator=source_n, source_correct=source_correct, target_denominator=200,
        target_correct=target_correct, directions=dict(directions), current_common_correct=current_common,
        baseline_common_correct=baseline_common, common_denominator=200,
        common_metric="common_fixed_context_lexical_scalar_correctness", baseline_typed_support=False,
        improvement_percentage_points=(current_common - baseline_common) / 2,
        details=details, common_details=common_details, semantic_certificate=False, production_database_writes=0)
    put(directory / "report.json", report)
    return {key: item for key, item in report.items() if key not in {"details", "common_details"}}


def baseline():
    value = manifest()
    directory = fresh("baseline")
    code = HERE / "released-baseline"
    archive = subprocess.run(["git", "archive", "05d2749", "sekaisync", "pyproject.toml"], cwd=ROOT, capture_output=True)
    if archive.returncode:
        raise ValueError("released baseline archive failed: " + archive.stderr.decode(errors="replace"))
    put(directory / "release.tar", archive.stdout)
    import tarfile
    code.mkdir()
    with tarfile.open(directory / "release.tar") as bundle:
        bundle.extractall(code, filter="data")
    inputs = directory / "pages.json"
    put(inputs, value["pages"])
    queries = directory / "queries.json"
    put(queries, [{key: source[key] for key in ("unit_id", "source_language", "story_key", "source_canonical")}
                 for source in read(HERE / "targets/selection.json")["selected_sources"]])
    command = [sys.executable, str(HERE / "baseline_callable.py"), "--code-root", str(code), "--pages", str(inputs),
               "--queries", str(queries), "--out", str(directory / "callable-result.json")]
    process = subprocess.run(command, cwd=code, capture_output=True, text=True, encoding="utf-8")
    put(directory / "process.json", dict(command=command, returncode=process.returncode, stdout=process.stdout, stderr=process.stderr))
    if process.returncode:
        raise ValueError("actual released callable failed; no fake baseline score: " + process.stderr)
    result = read(directory / "callable-result.json")
    result.update(input_sha256={str(inputs): b.sha(inputs), str(queries): b.sha(queries), str(directory / "release.tar"): b.sha(directory / "release.tar"),
                              str(directory / "callable-result.json"): b.sha(directory / "callable-result.json"),
                              **{str(path): b.sha(path) for path in code.rglob("*.py")}, **hashes()})
    put(directory / "report.json", result)
    return dict(released_commit="05d2749", actual_callable_run=True, typed_support=False, contextual_scalar_score_pending=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("preview", "close-product", "register", "capture", "prepare", "freeze-source-reference",
        "source-submit", "freeze-host-senses", "select-targets", "freeze-target-reference", "target-submit", "baseline", "consumer", "score"))
    parser.add_argument("--root-explicit-passed", action="store_true")
    parser.add_argument("--root-authorized", action="store_true")
    parser.add_argument("--closure-report", type=Path)
    parser.add_argument("--as-of-utc")
    parser.add_argument("--usage-exceptions", type=Path, help="Root-confirmed hashed synthetic fixture identity matches; no corpus-use exception")
    parser.add_argument("--database", type=Path, default=ROOT / "store/kb/sekaisync.db")
    parser.add_argument("--language", choices=LANGS)
    parser.add_argument("--answer", type=Path)
    parser.add_argument("--completion", type=Path)
    parser.add_argument("--reference", action="append", default=[], help="LANG=independent-original.json; all five languages")
    parser.add_argument("--consumer-output", type=Path)
    args = parser.parse_args()
    if args.stage in {"preview", "close-product", "register", "capture"}:
        result = metadata(args.stage, args)
    elif args.stage == "prepare":
        result = prepare()
    elif args.stage == "freeze-host-senses":
        result = freeze_host_senses(dict(entry.split("=", 1) for entry in args.reference))
    elif args.stage.startswith("freeze-"):
        result = freeze_references(args.stage.split("-")[1], dict(entry.split("=", 1) for entry in args.reference))
    elif args.stage.endswith("-submit"):
        result = submit(args.stage.split("-")[0], args.language, args.answer, args.completion)
    elif args.stage == "select-targets":
        result = select_targets()
    elif args.stage == "baseline":
        result = baseline()
    elif args.stage == "consumer":
        result = consumers(manifest(), read(HERE / "targets/selection.json")["selected_sources"])
        put(b.work_path(args.consumer_output), result)
        result = dict(reload_read_only=True, source_languages=5)
    else:
        result = score()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
