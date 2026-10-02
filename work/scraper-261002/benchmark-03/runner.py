"""Fresh work-only trial adapter; tooling creation authorizes no data stage."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import FunctionType

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREVIOUS = HERE.parent / "benchmark-02"
FIXED_STORIES = tuple("event:" + suffix for suffix in ("158:8", "75:4"))
EXPECTED_TEST_COUNT = 2090
LOCAL_TOOLING = ("runner.py", "baseline_callable.py", "check_tooling.py",
                 "PROTOCOL.md", "COMMANDS.md", "dependency-manifest.json")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def pinned(kind):
    return {str(ROOT / name): digest for name, digest in
            read(HERE / "dependency-manifest.json")[kind].items()}


def check(values):
    for path, expected in values.items():
        if sha(path) != expected:
            raise ValueError("frozen input changed: " + str(path))


check(pinned("code_sha256"))
check(pinned("before_body_parent_sha256"))
spec = importlib.util.spec_from_file_location("frozen261002_trial02_for_trial03", PREVIOUS / "runner.py")
parent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(parent)

# Bind every inherited function to fresh globals, including the parent's aliases.
context = dict(parent.__dict__)
context.update(parent.context)
context.update(HERE=HERE, ROOT=ROOT, __file__=__file__)
globals().update({name: context[name] for name in ("b", "acq", "ap", "ar", "db", "spans", "terms", "LANGS")})
source_globals = (parent.__dict__, parent.context, parent.original.__dict__)
for name, function in list(context.items()):
    if isinstance(function, FunctionType) and any(function.__globals__ is value for value in source_globals):
        bound = FunctionType(function.__code__, context, function.__name__,
                             function.__defaults__, function.__closure__)
        bound.__kwdefaults__ = function.__kwdefaults__
        context[name] = bound
        globals()[name] = bound
_parent_metadata = context["metadata"]


def hashes():
    evidence = {**pinned("code_sha256"), **pinned("first_trial_metadata_sha256"),
                **pinned("before_body_parent_sha256")}
    check(evidence)
    return {**evidence, **{str(HERE / name): sha(HERE / name) for name in LOCAL_TOOLING}}


def fixed_plan(value):
    if (tuple(family["story_key"] for family in value["families"]) != FIXED_STORIES
            or value.get("source_or_target_bodies_read") is not False
            or value.get("fixed_source_rows") != 80
            or value.get("fixed_target_obligations") != 200
            or value.get("seed") != b.SEED):
        raise ValueError("fixed before-body metadata selection or denominator changed; no reranking")
    return value


def tooling():
    value = read(HERE / "tooling-seal.json")
    check(value["input_sha256"])
    if (value.get("schema") != "work/checkpoint261002-trial03-tooling-seal@1"
            or value["input_sha256"] != hashes()
            or value.get("expected_actual_tests") != EXPECTED_TEST_COUNT
            or value.get("registration_or_capture_authorized") is not False
            or tuple("event:" + suffix for suffix in value.get("fixed_story_suffixes", [])) != FIXED_STORIES):
        raise ValueError("fresh tooling seal drifted or improperly authorizes a data stage")
    return value


def freeze_tooling():
    return dict(tooling_seal_sha256=put(HERE / "tooling-seal.json", dict(
        schema="work/checkpoint261002-trial03-tooling-seal@1", input_sha256=hashes(),
        expected_actual_tests=EXPECTED_TEST_COUNT, existing_tests=2081, prospective_synthetic_tests=9,
        fixed_story_suffixes=[story.removeprefix("event:") for story in FIXED_STORIES], registration_or_capture_authorized=False,
        source_or_target_bodies_read=False)), body_reads=0, stages_authorized=False)


class AuditSubprocess:
    """An isolated audit namespace rejects errors before considering path matches."""

    def __init__(self, run=None):
        self._run = run or subprocess.run

    def run(self, *args, **kwargs):
        result = self._run(*args, **kwargs)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise ValueError("historical usage audit failed or scope unreadable; no fixture exception")
        return result


audit_context = dict(b.audit_usage.__globals__)
audit_context["subprocess"] = AuditSubprocess()
historical_audit = FunctionType(b.audit_usage.__code__, audit_context, b.audit_usage.__name__,
                              b.audit_usage.__defaults__, b.audit_usage.__closure__)
historical_audit.__kwdefaults__ = b.audit_usage.__kwdefaults__


def reviewed_matches(story, matches, expected):
    if (story != FIXED_STORIES[0] or not matches or len(matches) != len(set(matches))
            or set(matches) != set(expected) or len(expected) != 2):
        raise ValueError("only both exact reviewed metadata-fixture paths may match; no other-match exception")


def fixture_binding(args):
    expected = read(HERE / "dependency-manifest.json")["fixture_identity_review"]
    path = (ROOT / expected["path"]).resolve()
    if (not args.root_confirmed_fixture_review or args.fixture_review is None
            or args.fixture_review.resolve() != path
            or args.fixture_review_sha256 != expected["sha256"]):
        raise ValueError("Root must explicitly supply the exact hash-bound independent metadata-fixture review")
    inputs = {str(path): expected["sha256"],
              **{str((ROOT / name).resolve()): digest for name, digest in expected["paths_sha256"].items()}}
    check(inputs)
    return path, inputs


def usage_audit(story, args, review, inputs):
    scopes = [ROOT / name for name in ("work", "scripts", "tests", "agents")]
    try:
        return historical_audit(story, scopes)
    except ValueError as error:
        prefix = "selected family prior use or unreadable usage scope; do not rerank: "
        message = str(error)
        if not message.startswith(prefix):
            raise
        matches = message[len(prefix):].splitlines()
        expected = read(HERE / "dependency-manifest.json")["fixture_identity_review"]
        allowed = {str((ROOT / name).resolve()): digest for name, digest in expected["paths_sha256"].items()}
        reviewed_matches(story, matches, allowed)
        check(inputs)
        return dict(schema="work/checkpoint261002-trial03-metadata-identity-proof@1",
            root_confirmed_metadata_only_identity_proof=True, source_or_target_bodies_read=False,
            semantic_labels_or_host_answers_read=False, historical_story_use=False,
            scopes=list(map(str, scopes)), match_paths=matches, paths_sha256=allowed,
            exception_record=str(review), exception_record_sha256=inputs[str(review)],
            original_audit_error=message, mode="rg path matches only; exact reviewed fixture identity proof")


def metadata(stage, args):
    if stage == "preview":
        if not args.root_authorized:
            raise ValueError("Root must separately authorize the fixed metadata-only preview")
        tooling()
        failed = read(PREVIOUS / "STOP.json")
        if failed.get("status") != "FAIL" or failed.get("stage") != "register":
            raise ValueError("failed second-trial before-body register STOP must remain intact")
        value = fixed_plan(read(PREVIOUS / "metadata-plan.json"))
        if value.get("formal_registration_authorized") is not False:
            raise ValueError("parent plan must be original unregistered before-body metadata")
        check(value["input_sha256"])
        value.update(schema="work/checkpoint261002-trial03-metadata-preview@1",
            parent_before_body_plan_sha256=sha(PREVIOUS / "metadata-plan.json"),
            prior_failed_trial_sha256=sha(PREVIOUS / "STOP.json"), no_reranking=True)
        value["input_sha256"].update({**hashes(), str(HERE / "tooling-seal.json"): sha(HERE / "tooling-seal.json")})
        put(HERE / "metadata-plan.json", value)
        return dict(stories=list(FIXED_STORIES), fixed_source_rows=80, fixed_target_obligations=200,
                    body_reads=0, formal_registration_authorized=False)
    if stage == "register":
        if not args.root_authorized:
            raise ValueError("Root authorization required after exact fixed-plan and fixture review")
        closure()
        value = fixed_plan(read(HERE / "metadata-plan.json"))
        check(value["input_sha256"])
        excluded, _ = prior_metadata()
        if value["excluded_content_families"] != excluded:
            raise ValueError("complete prior-family exclusions changed")
        review, review_inputs = fixture_binding(args)
        as_of = args.as_of_utc or datetime.now(timezone.utc).isoformat()
        for family in value["families"]:
            family["usage_audit"] = usage_audit(family["story_key"], args, review, review_inputs)
            family["release"] = b.census_release(parent.original.OLD.parent / "census", family, as_of)
        value.update(schema="work/checkpoint261002-trial03-metadata-registration@1", as_of_utc=as_of,
                     formal_registration_authorized=True)
        value["input_sha256"].update({**review_inputs,
            str(HERE / "metadata-plan.json"): sha(HERE / "metadata-plan.json"),
            str(HERE / "product-closure.json"): sha(HERE / "product-closure.json")})
        digest = put(HERE / "metadata-registration.json", value)
        put(HERE / "metadata-registration-seal.json", dict(registration_sha256=digest))
        return dict(stories=list(FIXED_STORIES), body_reads=0, metadata_identity_proof_bound=True)
    return _parent_metadata(stage, args)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("freeze-tooling", "preview", "close-product", "register", "capture", "prepare",
        "freeze-source-reference", "source-submit", "freeze-host-senses", "select-targets", "freeze-target-reference",
        "target-submit", "baseline", "consumer", "score"))
    parser.add_argument("--root-explicit-passed", action="store_true")
    parser.add_argument("--root-authorized", action="store_true")
    parser.add_argument("--closure-report", type=Path)
    parser.add_argument("--root-confirmed-fixture-review", action="store_true")
    parser.add_argument("--fixture-review", type=Path)
    parser.add_argument("--fixture-review-sha256")
    parser.add_argument("--as-of-utc")
    parser.add_argument("--database", type=Path, default=ROOT / "store/kb/sekaisync.db")
    parser.add_argument("--language", choices=LANGS)
    parser.add_argument("--answer", type=Path)
    parser.add_argument("--completion", type=Path)
    parser.add_argument("--reference", action="append", default=[], help="LANG=independent-original.json; all five languages")
    parser.add_argument("--consumer-output", type=Path)
    args = parser.parse_args()
    if (HERE / "STOP.json").exists():
        raise ValueError("failed original/partial third-trial stage retained; trial stopped")
    try:
        paths = [path for path in (args.answer, args.completion, args.consumer_output) if path is not None]
        paths.extend(Path(entry.split("=", 1)[1]) for entry in args.reference)
        if any(not path.resolve().is_relative_to(HERE) for path in paths):
            raise ValueError("all trial answers/references/consumer outputs must remain within benchmark-03")
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
            put(b.work_path(args.consumer_output), result)
            result = dict(reload_read_only=True, source_languages=5)
        else:
            result = score()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as error:
        if not (HERE / "STOP.json").exists():
            put(HERE / "STOP.json", dict(status="FAIL", stage=args.stage, error=str(error)))
        raise


context.update({name: globals()[name] for name in ("hashes", "check", "read", "pinned", "tooling",
    "freeze_tooling", "metadata", "main")})

if __name__ == "__main__":
    main()
