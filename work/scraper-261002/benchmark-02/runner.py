"""Prospective second 261002 trial; no stage is authorized by tooling creation."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from types import FunctionType
import zipfile

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ORIGINAL = HERE.parent / "benchmark"
EXPECTED_TEST_COUNT = 2090
CANONICAL = ["-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def check(values):
    for path, expected in values.items():
        if sha(path) != expected:
            raise ValueError("frozen input changed: " + str(path))


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pinned(kind):
    return {str(ROOT / name): digest for name, digest in
            read(HERE / "dependency-manifest.json")[kind].items()}


check(pinned("code_sha256"))
original = load(ORIGINAL / "runner.py", "unchanged_first261002_runner")
validation = load(ROOT / "work/scraper-261002/run_validation.py", "unchanged261002_validation")
contract = load(HERE / "score_contract.py", "prospective_trial02_score_contract")
b, acq, ap, ar, db, spans, terms, LANGS = (
    original.b, original.acq, original.ap, original.ar, original.db,
    original.spans, original.terms, original.LANGS)

# Copy functions into a separate globals namespace; never mutate the old runner.
context = dict(original.__dict__)
context.update(HERE=HERE, ROOT=ROOT, __file__=__file__)
for name, function in original.__dict__.items():
    if isinstance(function, FunctionType) and function.__globals__ is original.__dict__:
        bound = FunctionType(function.__code__, context, function.__name__,
                             function.__defaults__, function.__closure__)
        bound.__kwdefaults__ = function.__kwdefaults__
        context[name] = bound
        globals()[name] = bound
inherited_metadata = context["metadata"]
inherited_baseline = context["baseline"]


def hashes():
    check(pinned("code_sha256"))
    check(pinned("first_trial_metadata_sha256"))
    paths = [HERE / name for name in ("runner.py", "score_contract.py", "baseline_callable.py",
             "check_tooling.py", "PROTOCOL.md", "COMMANDS.md", "TARGET_SCORE_CONTRACT_ADDENDUM.md", "dependency-manifest.json")]
    return {**pinned("code_sha256"), **pinned("first_trial_metadata_sha256"),
            **{str(path): sha(path) for path in paths}}


def tooling():
    value = read(HERE / "tooling-seal.json")
    check(value["input_sha256"])
    if value["input_sha256"] != hashes() or value["expected_actual_tests"] != EXPECTED_TEST_COUNT:
        raise ValueError("prospective tooling seal or actual test count drifted")
    if value.get("registration_or_capture_authorized") is not False:
        raise ValueError("tooling freeze must authorize no data stage")
    return value


def freeze_tooling():
    return dict(tooling_seal_sha256=put(HERE / "tooling-seal.json",
        dict(schema="work/checkpoint261002-trial02-tooling-seal@1",
             input_sha256=hashes(), expected_actual_tests=EXPECTED_TEST_COUNT,
             existing_tests=2081, prospective_synthetic_tests=9,
             registration_or_capture_authorized=False, source_or_target_bodies_read=False)),
        body_reads=0, stages_authorized=False)


def verified_pass(path):
    path = b.work_path(path)
    report = read(path)
    current = validation.code_hashes()
    if (report.get("schema") != "work/scraper-261002-validation@1"
            or report.get("status") != "PASS" or report.get("tests_run") != EXPECTED_TEST_COUNT
            or report.get("exit_code") != 0 or report.get("product_unchanged") is not True
            or report.get("changed_files") != [] or report.get("command", [])[1:] != CANONICAL
            or report.get("code_sha256") != current
            or report.get("runner_sha256") != sha(validation.__file__)):
        raise ValueError("actual canonical unchanged 2090-test full PASS over complete exact code required")
    inputs = {str(path): sha(path), **hashes()}
    for filename, field in (("hashes-before.json", "before_manifest_sha256"),
                            ("hashes-after.json", "after_manifest_sha256"),
                            ("stdout.txt", "stdout_sha256"), ("stderr.txt", "stderr_sha256")):
        artifact = path.parent / filename
        if sha(artifact) != report[field]:
            raise ValueError("canonical PASS manifest/log hash changed")
        inputs[str(artifact)] = report[field]
    if read(path.parent / "hashes-before.json") != current or read(path.parent / "hashes-after.json") != current:
        raise ValueError("PASS before/after complete code inventories disagree")
    log = (path.parent / "stderr.txt").read_text(encoding="utf-8")
    if re.findall(r"^Ran (\d+) tests in [\d.]+s$", log, re.MULTILINE) != [str(EXPECTED_TEST_COUNT)] or not re.search(r"^OK$", log, re.MULTILINE):
        raise ValueError("actual canonical PASS log must record exactly 2090 tests and OK")
    archive = b.work_path(report["product_archive"])
    if sha(archive) != report["product_archive_sha256"]:
        raise ValueError("canonical PASS exact-product archive changed")
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        if (bundle.testzip() is not None or len(names) != len(current) or set(names) != set(current)
                or any(hashlib.sha256(bundle.read(name)).hexdigest() != digest for name, digest in current.items())):
            raise ValueError("archive inventory/bytes differ from actually tested complete code")
    inputs[str(archive)] = report["product_archive_sha256"]
    return report, inputs


def closure():
    tooling()
    value = read(HERE / "product-closure.json")
    check(value["input_sha256"])
    report, _ = verified_pass(value["report"])
    if value.get("root_explicit_passed") is not True or value.get("tests_run") != report["tests_run"]:
        raise ValueError("new exact-code product closure lacks explicit completed PASS")
    return value


def prior_metadata():
    evidence = pinned("first_trial_metadata_sha256")
    check(evidence)
    first = read(ORIGINAL / "metadata-registration.json")
    plan = read(ORIGINAL / "metadata-plan.json")
    if (first.get("source_or_target_bodies_read") is not False
            or plan.get("source_or_target_bodies_read") is not False
            or read(ORIGINAL / "metadata-registration-seal.json")["registration_sha256"] != sha(ORIGINAL / "metadata-registration.json")
            or [x["story_key"] for x in first["families"]] != [x["story_key"] for x in plan["families"]]):
        raise ValueError("original first-trial metadata/seal binding changed")
    excluded = set(first["excluded_content_families"])
    excluded.update(b.event_family(family["story_key"]) for family in first["families"])
    # Only prior registration/seal/plan metadata is read; never old semantic output.
    for filename, digest in first["input_sha256"].items():
        path = Path(filename)
        if not path.name.startswith(("metadata-registration", "metadata-plan")) or path.suffix != ".json":
            continue
        path = b.work_path(path)
        check({str(path): digest})
        evidence[str(path)] = digest
        if "seal" in path.stem:
            sealed = path.with_name("metadata-registration.json")
            if read(path)["registration_sha256"] != sha(sealed):
                raise ValueError("original prior registration seal changed")
            continue
        value = read(path)
        excluded.update(value.get("excluded_content_families", []))
        families = value.get("families", [])
        if isinstance(value.get("family"), dict):
            families = [*families, value["family"]]
        families = [*families, *value.get("stories", [])]
        excluded.update(b.event_family(family["story_key"]) for family in families)
        excluded.update(b.event_family(story) for story in value.get("excluded_stories", []) if story.startswith("event:"))
    if not {"event:2", "event:161"} <= excluded or any(not re.fullmatch(r"event:\d+", family) for family in excluded):
        raise ValueError("complete original first-trial families or prior family exclusions omitted")
    return sorted(excluded), evidence


def metadata(stage, args):
    if stage == "close-product":
        if not args.root_explicit_passed or not args.closure_report:
            raise ValueError("Root must explicitly supply the future exact canonical full PASS")
        tooling()
        report, inputs = verified_pass(args.closure_report)
        inputs[str(HERE / "tooling-seal.json")] = sha(HERE / "tooling-seal.json")
        put(HERE / "product-closure.json", dict(report=str(b.work_path(args.closure_report)),
            root_explicit_passed=True, tests_run=report["tests_run"], input_sha256=inputs))
        return dict(closure_bound=True, actual_tests_run=report["tests_run"], body_reads=0)
    if stage == "preview":
        if not args.root_authorized:
            raise ValueError("Root must separately authorize even the metadata-only second-trial preview")
        tooling()
        excluded, priors = prior_metadata()
        census = original.OLD.parent / "census"
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
            raise ValueError("two distinct unseen whole families required; no reranking")
        inputs = {**hashes(), **priors, str(HERE / "tooling-seal.json"): sha(HERE / "tooling-seal.json"),
                  **{str(census / name): sha(census / name) for name in ("logical-units.jsonl", "census-index.sqlite")}}
        put(HERE / "metadata-plan.json", dict(schema="work/checkpoint261002-trial02-metadata-preview@1",
            families=chosen, excluded_content_families=excluded, seed=b.SEED,
            candidate_universe_canonical_sha256=b.digest(candidates), eligible_candidate_count=len(candidates),
            fixed_source_rows=80, fixed_target_obligations=200, source_or_target_bodies_read=False,
            formal_registration_authorized=False, input_sha256=inputs))
        return dict(stories=[x["story_key"] for x in chosen], excluded_families=excluded, body_reads=0)
    if stage == "register":
        if not args.root_authorized:
            raise ValueError("Root authorization required after reviewing the exact metadata preview")
        closure()
        value = read(HERE / "metadata-plan.json")
        check(value["input_sha256"])
        excluded, _ = prior_metadata()
        if value["excluded_content_families"] != excluded:
            raise ValueError("complete prior-family exclusions changed")
        as_of = args.as_of_utc or datetime.now(timezone.utc).isoformat()
        for family in value["families"]:
            family["usage_audit"] = b.audit_usage(family["story_key"], [ROOT / name for name in ("work", "scripts", "tests", "agents")])
            family["release"] = b.census_release(original.OLD.parent / "census", family, as_of)
        value.update(schema="work/checkpoint261002-trial02-metadata-registration@1", as_of_utc=as_of,
                     formal_registration_authorized=True)
        value["input_sha256"].update({str(HERE / "metadata-plan.json"): sha(HERE / "metadata-plan.json"),
                                     str(HERE / "product-closure.json"): sha(HERE / "product-closure.json")})
        digest = put(HERE / "metadata-registration.json", value)
        put(HERE / "metadata-registration-seal.json", dict(registration_sha256=digest))
        return dict(stories=[x["story_key"] for x in value["families"]], body_reads=0)
    return inherited_metadata(stage, args)


def baseline():
    for destination in (HERE / "baseline", HERE / "released-baseline"):
        if destination.exists():
            raise FileExistsError("baseline destination collision; preserve originals and STOP: " + str(destination))
    return inherited_baseline()


def score():
    value = manifest()
    refs("source")
    refs("target")
    selection = read(HERE / "targets/selection.json")
    check(selection["input_sha256"])
    baseline_report = read(HERE / "baseline/report.json")
    check(baseline_report["input_sha256"])
    if len(selection["selected_sources"]) != 50 or len(selection["obligations"]) != 200:
        raise ValueError("fixed 50-source / 200-directed obligation denominator changed")
    source_results, references, actual = {}, {}, {}
    for lang in LANGS:
        reference = read(HERE / "source-references" / (lang + ".json"))
        expected = {acq.vector(unit["evidence_id"], unit["segments"]) for unit in reference["units"]}
        source_results[lang] = dict(denominator=len(expected), correct=len(expected & set(acquired(value, lang))))
        references.update({record["obligation_id"]: record for record in read(HERE / "target-references" / (lang + ".json"))["records"]})
        with db.connect(Path(value["languages"][lang]["store"])) as conn:
            actual[lang] = b.ledger._read_relations(conn)
    directions = defaultdict(lambda: dict(denominator=0, correct=0))
    details, reasons = [], Counter()
    for item in selection["obligations"]:
        correct, reason, classification = contract.evaluate(item, references[item["obligation_id"]],
            actual[item["source_language"]], b.ledger._language)
        direction = item["source_language"] + "->" + item["target_language"]
        directions[direction]["denominator"] += 1
        directions[direction]["correct"] += int(correct)
        reasons[reason] += 1
        details.append(dict(obligation_id=item["obligation_id"], correct=correct, reason=reason,
                            kind_vector_classification_matches=classification))
    if len(directions) != 20 or any(row["denominator"] != 10 for row in directions.values()):
        raise ValueError("exact 20 language directions / 10 obligations per direction required")
    directory = fresh("scores")
    live = consumers(value, selection["selected_sources"])
    put(directory / "consumers.json", live)
    command = [sys.executable, "-B", str(Path(__file__)), "consumer", "--consumer-output", str(directory / "reload-consumers.json")]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    put(directory / "reload-process.json", dict(command=command, returncode=process.returncode, stdout=process.stdout, stderr=process.stderr))
    reload_ok = process.returncode == 0 and read(directory / "reload-consumers.json") == live
    consumer_ok = all(check["passed"] for sources in live.values() for source in sources for check in source["checks"])
    for path in (HERE / "submissions").glob("*/*/report.json"):
        check(read(path)["input_sha256"])
    source_n = sum(row["denominator"] for row in source_results.values())
    if source_n < 100 or any(not row["denominator"] for row in source_results.values()):
        raise ValueError("independently frozen meaningful source denominator must be at least 100")
    source_correct = sum(row["correct"] for row in source_results.values())
    target_correct = sum(row["correct"] for row in directions.values())
    current_common, baseline_common, common_details = 0, 0, []
    current_outputs = {source["unit_id"]: source["penetration"] for sources in live.values() for source in sources}
    for item in selection["obligations"]:
        reference = references[item["obligation_id"]]
        current_ok, current_reason = scalar_match(item, current_outputs.get(item["unit_id"]), reference, value["pages"])
        baseline_ok, baseline_reason = scalar_match(item, baseline_report["consumer_outputs"].get(item["unit_id"], {}).get("penetration"), reference, value["pages"])
        current_common += int(current_ok)
        baseline_common += int(baseline_ok)
        common_details.append(dict(obligation_id=item["obligation_id"], current=current_ok, baseline=baseline_ok,
                                   current_reason=current_reason, baseline_reason=baseline_reason))
    gates = dict(source_overall=source_correct / source_n >= .90,
                 source_each_language=all(row["correct"] / row["denominator"] >= .85 for row in source_results.values()),
                 target_overall=target_correct / 200 >= .90,
                 worst_twenty_direction=all(row["correct"] / 10 >= .80 for row in directions.values()),
                 improvement=(current_common - baseline_common) / 200 >= .20,
                 consumers=consumer_ok, separate_process_reload=reload_ok,
                 normal_receipts_and_replays=not (HERE / "STOP.json").exists())
    report = dict(schema="work/checkpoint261002-trial02-benchmark-score@1",
        status="PASS" if all(gates.values()) else "FAIL", gates=gates, source=source_results,
        source_denominator=source_n, source_correct=source_correct, target_denominator=200,
        target_correct=target_correct, directions=dict(directions), reasons=dict(reasons),
        current_common_correct=current_common, baseline_common_correct=baseline_common, common_denominator=200,
        common_metric="common_fixed_context_lexical_scalar_correctness", baseline_typed_support=False,
        improvement_percentage_points=(current_common - baseline_common) / 2, details=details, common_details=common_details,
        omission_or_unresolved_correspondence_credit=False, semantic_certificate=False, production_database_writes=0,
        input_sha256={str(HERE / "tooling-seal.json"): sha(HERE / "tooling-seal.json"), **hashes()})
    put(directory / "report.json", report)
    if report["status"] == "FAIL":
        put(HERE / "STOP.json", dict(status="FAIL", stage="score", gates=gates))
    return {key: item for key, item in report.items() if key not in {"details", "common_details"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("freeze-tooling", "preview", "close-product", "register", "capture", "prepare",
        "freeze-source-reference", "source-submit", "freeze-host-senses", "select-targets", "freeze-target-reference",
        "target-submit", "baseline", "consumer", "score"))
    parser.add_argument("--root-explicit-passed", action="store_true")
    parser.add_argument("--root-authorized", action="store_true")
    parser.add_argument("--closure-report", type=Path)
    parser.add_argument("--as-of-utc")
    parser.add_argument("--database", type=Path, default=ROOT / "store/kb/sekaisync.db")
    parser.add_argument("--language", choices=LANGS)
    parser.add_argument("--answer", type=Path)
    parser.add_argument("--completion", type=Path)
    parser.add_argument("--reference", action="append", default=[], help="LANG=independent-original.json; all five languages")
    parser.add_argument("--consumer-output", type=Path)
    args = parser.parse_args()
    if (HERE / "STOP.json").exists():
        raise ValueError("failed original/partial second-trial stage retained; trial stopped")
    try:
        local_inputs = [path for path in (args.answer, args.completion, args.consumer_output) if path is not None]
        local_inputs.extend(Path(entry.split("=", 1)[1]) for entry in args.reference)
        if any(not path.resolve().is_relative_to(HERE) for path in local_inputs):
            raise ValueError("all trial answers/references/consumer outputs must remain within benchmark-02")
        if args.stage == "freeze-tooling":
            result = freeze_tooling()
        elif args.stage in {"preview", "close-product", "register", "capture"}:
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
            output = b.work_path(args.consumer_output)
            if not output.is_relative_to(HERE):
                raise ValueError("consumer output must remain under the second-trial work directory")
            put(output, result)
            result = dict(reload_read_only=True, source_languages=5)
        else:
            result = score()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        if not (HERE / "STOP.json").exists():
            put(HERE / "STOP.json", dict(status="FAIL", stage=args.stage, error=str(error)))
        raise


context.update({name: globals()[name] for name in ("hashes", "check", "read", "closure", "metadata", "baseline", "score", "main")})

if __name__ == "__main__":
    main()
