"""Trial04 metadata-only preview around the unchanged sealed Trial03 helper."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
from types import FunctionType

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREVIOUS_HELPER = HERE.parent / "unseen-acquisition-20261002-03/register_trial.py"
PREVIOUS_HELPER_SHA = "cb4446140eef38cef69efae56d88a2ba0878fed923bc48febac5169222d75396"
PRIOR = PREVIOUS_HELPER.with_name("metadata-registration.json")
PRIOR_SHA = "098d0305dcca1424d01b4916011a07fb3a72075990623d142cfb68e45400132b"
PRIOR_SEAL = PRIOR.with_name("metadata-registration-seal.json")
PRIOR_SEAL_SHA = "f41cdfaaad10d8e1fc7b0d31b2d87123a45df827387e2b893eab6495979f899f"
ORIGINAL = PREVIOUS_HELPER.parent
ORIGINAL_COPIES = {
    "PROTOCOL.md": "be8bfaea112edaf940324dda3a56d04c8d7e48a1cd21433f3cf511b83779943c",
    "acquire_trial.py": "93e6da29c5637372c2dc77a053d719669e28c1bf7c0391b4c9206ffa90094f72",
}
SCHEMA = "work/unseen-proposition-acquisition-metadata@1"
PLAN_SCHEMA = "work/unseen-proposition-acquisition-metadata-preview@1"
SEED = "p0-source-first-next01|"
ROOT_CLOSURE_REPORT = HERE.parent / "typed-literal-grounding-integration-20261002-01/current-closure-02/report.json"
ROOT_CLOSURE_REPORT_SHA = "0461a88603c539cbe3db94b569834d01219af987fca453d76d7b861c15c35361"
ROOT_PRODUCT_ARCHIVE_SHA = "270868d96546d784b7c79c191d19d85f9b522006e3f3be3ef0a12bda4208672a"

if hashlib.sha256(PREVIOUS_HELPER.read_bytes()).hexdigest() != PREVIOUS_HELPER_SHA:
    raise ValueError("sealed Trial03 metadata helper changed; stop")
spec = importlib.util.spec_from_file_location("trial04_sealed_trial03_metadata_helper", PREVIOUS_HELPER)
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
backend = previous.backend
BACKEND = previous.BACKEND
BACKEND_SHA = previous.BACKEND_SHA
verify_hashes = previous.verify_hashes


def prior_metadata():
    if backend.sha(PREVIOUS_HELPER) != PREVIOUS_HELPER_SHA:
        raise ValueError("sealed Trial03 metadata helper changed")
    if backend.sha(BACKEND) != BACKEND_SHA or backend.SEED != SEED:
        raise ValueError("sealed metadata backend or fixed seed changed")
    excluded, hashes, seals = previous.prior_metadata()
    path, seal_path = backend.work_path(PRIOR), backend.work_path(PRIOR_SEAL)
    raw, seal_raw = path.read_bytes(), seal_path.read_bytes()
    digest, seal_digest = hashlib.sha256(raw).hexdigest(), hashlib.sha256(seal_raw).hexdigest()
    if digest != PRIOR_SHA or seal_digest != PRIOR_SEAL_SHA:
        raise ValueError("original Trial03 registration or seal changed")
    value, seal = json.loads(raw), json.loads(seal_raw)
    if (value.get("schema") != SCHEMA or value.get("seed") != SEED
            or value.get("source_or_target_bodies_read") is not False
            or value.get("family", {}).get("story_key") != "event:94:7"
            or seal.get("registration_sha256") != digest):
        raise ValueError("unsupported or unbound original Trial03 metadata")
    if (value.get("prior_registration_sha256") != hashes
            or value.get("prior_registration_seal_sha256") != seals
            or value.get("excluded_content_families") != excluded):
        raise ValueError("original Trial03 prior-evidence chain changed")
    families = set(excluded)
    families.add(backend.event_family(value["family"]["story_key"]))
    hashes[str(path)] = digest
    seals[str(seal_path)] = seal_digest
    return sorted(families), hashes, seals


def copy_hashes():
    hashes = {}
    for name, expected in ORIGINAL_COPIES.items():
        for path in (ORIGINAL / name, HERE / name):
            if backend.sha(path) != expected:
                raise ValueError("original protocol/acquisition runner copy changed: " + str(path))
            hashes[str(path)] = expected
    return hashes


def build_preview():
    excluded, prior_hashes, prior_seals = prior_metadata()
    census = HERE.parent / "census"
    metadata_hashes = {str(census / name): backend.sha(census / name)
                       for name in ("logical-units.jsonl", "census-index.sqlite")}
    inherited_copies = copy_hashes()
    candidates = backend.metadata_universe(census, excluded)
    first = candidates[0]
    family = {key: first[key] for key in ("story_key", "logical_key", "rank", "complete", "release_status")}
    family["pages"] = {language: {key: page[key] for key in
                       ("source", "page_id", "language", "text_sha256")}
                       for language, page in first["pages"].items()}
    input_hashes = {**prior_hashes, **prior_seals, **metadata_hashes, **inherited_copies,
                    str(PREVIOUS_HELPER): PREVIOUS_HELPER_SHA,
                    str(BACKEND): BACKEND_SHA, str(HERE / "register_trial.py"): backend.sha(__file__)}
    closure_pending = not (HERE / "product-closure.json").is_file()
    if not closure_pending:
        require_current_closure()
        input_hashes[str(HERE / "product-closure.json")] = backend.sha(HERE / "product-closure.json")
        input_hashes[str(ROOT_CLOSURE_REPORT)] = ROOT_CLOSURE_REPORT_SHA
    verify_hashes(input_hashes)
    return dict(schema=PLAN_SCHEMA, preview_only=True, seed=SEED,
                excluded_content_families=excluded, family=family,
                eligible_candidate_count=len(candidates),
                candidate_universe_canonical_sha256=backend.digest(candidates),
                selection_rule="first eligible five-language family in unchanged fixed hash sort; no reranking",
                prior_registration_sha256=prior_hashes,
                prior_registration_seal_sha256=prior_seals,
                input_sha256=input_hashes, fixed_source_windows=40,
                source_or_target_bodies_read=False,
                formal_registration_authorized=False,
                registration_or_body_capture_performed=False,
                current_product_closure_pending=closure_pending,
                historical_usage_audit_pending=True,
                release_revalidation_pending=True,
                next_stage_requirement="Root must separately authorize formal registration and body capture after reviewing this exact closure-bound metadata preview")


def preview():
    path = backend.new_stage(HERE, "metadata-plan.json")
    value = build_preview()
    digest = backend.freeze(path, value)
    return dict(story_key=value["family"]["story_key"],
                eligible_candidate_count=value["eligible_candidate_count"],
                metadata_plan_sha256=digest, source_or_target_bodies_read=False)


def verify_preview():
    value = json.loads((HERE / "metadata-plan.json").read_bytes())
    if value != build_preview():
        raise ValueError("metadata-only preview inputs or fixed selection changed")
    return dict(story_key=value["family"]["story_key"],
                eligible_candidate_count=value["eligible_candidate_count"],
                metadata_plan_sha256=backend.sha(HERE / "metadata-plan.json"),
                preview_verified=True, source_or_target_bodies_read=False)


def require_root_report():
    path = backend.work_path(ROOT_CLOSURE_REPORT)
    if backend.sha(path) != ROOT_CLOSURE_REPORT_SHA:
        raise ValueError("Root-confirmed exact closure report changed")
    report = json.loads(path.read_bytes())
    if (report.get("schema") != "work/current-product-integration-closure@1"
            or report.get("status") != "PASS" or report.get("root_pass_confirmed") is not True
            or report.get("tests_run") != 2072 or report.get("expected_tests") != 2072
            or report.get("current_product_archive_sha256") != ROOT_PRODUCT_ARCHIVE_SHA
            or report.get("raw_records") != 21871 or report.get("public_signatures_checked") != 610
            or report.get("production_database_writes") != 0):
        raise ValueError("exact 2072-test PASS and raw/API closure binding missing")
    if backend.sha(backend.work_path(report["current_product_archive"])) != ROOT_PRODUCT_ARCHIVE_SHA:
        raise ValueError("Root-confirmed exact product archive changed")
    return report


def close_product(root_explicit_passed=False):
    if root_explicit_passed is not True:
        raise ValueError("Root must explicitly authorize sealing the exact Trial04 product closure")
    require_root_report()
    return backend.close_product(HERE, ROOT_CLOSURE_REPORT, ROOT_CLOSURE_REPORT_SHA,
                                 explicit_passed=True)


def require_current_closure():
    if not (HERE / "product-closure.json").is_file():
        raise ValueError("PRODUCT CLOSURE PENDING: Root must confirm fresh exact 2072-test PASS and raw/API closure before sealing this exact Trial04 product closure")
    closure = _bound_require_current_closure()
    if (backend.work_path(closure["closure_report"]) != backend.work_path(ROOT_CLOSURE_REPORT)
            or closure["closure_report_sha256"] != ROOT_CLOSURE_REPORT_SHA):
        raise ValueError("Trial04 product closure is not bound to Root's exact current closure")
    require_root_report()
    return closure


# Reuse sealed stage code in an isolated namespace, leaving the old module intact.
stage_context = dict(previous.__dict__)
stage_context.update(HERE=HERE, ROOT=ROOT, __file__=__file__,
                     PREVIOUS_HELPER=PREVIOUS_HELPER, PREVIOUS_HELPER_SHA=PREVIOUS_HELPER_SHA,
                     PRIOR=PRIOR, PRIOR_SHA=PRIOR_SHA, PRIOR_SEAL=PRIOR_SEAL, PRIOR_SEAL_SHA=PRIOR_SEAL_SHA,
                     prior_metadata=prior_metadata, require_current_closure=require_current_closure,
                     verify_hashes=verify_hashes)


def _bind_stage(name):
    original = getattr(previous, name)
    bound = FunctionType(original.__code__, stage_context, original.__name__,
                         original.__defaults__, original.__closure__)
    bound.__kwdefaults__ = original.__kwdefaults__
    return bound


_bound_require_current_closure = _bind_stage("require_current_closure")
register, verify, capture = (_bind_stage(name) for name in ("register", "verify", "capture"))
stage_context.update(register=register, verify=verify, capture=capture)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("close-product", "preview", "verify-preview", "register", "verify", "capture"))
    parser.add_argument("--root-explicit-passed", action="store_true")
    parser.add_argument("--as-of-utc", help="Optional fixed registration release-check timestamp")
    parser.add_argument("--database", type=Path, default=ROOT / "store/kb/sekaisync.db")
    args = parser.parse_args()
    if args.stage == "close-product":
        result = close_product(args.root_explicit_passed)
    elif args.stage == "preview":
        result = preview()
    elif args.stage == "verify-preview":
        result = verify_preview()
    elif args.stage == "register":
        result = register(args.as_of_utc)
    elif args.stage == "verify":
        value = verify()
        result = dict(story_key=value["family"]["story_key"], registration_verified=True, body_reads=0)
    else:
        result = capture(args.database)
    print(json.dumps(result, indent=2))
