"""Trial03 registration reusing the sealed trial02 metadata helper unchanged."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
PREVIOUS_HELPER = HERE.parent / "unseen-acquisition-20261002-02/register_trial.py"
PREVIOUS_HELPER_SHA = "f8168f3b3d61d9c038a6f7f99c62bd235dc7beb9557f29939eb629e10cc1febd"
PRIOR = HERE.parent / "unseen-acquisition-20261002-02/metadata-registration.json"
PRIOR_SHA = "140ca53a123f442f30171a27a147e389562459ec30d62393d1eb92f82973085a"
PRIOR_SEAL = PRIOR.with_name("metadata-registration-seal.json")
PRIOR_SEAL_SHA = "807904a8f79f8c1f6f3cc23a56a683302d8492bb83c5836e898bcc7c675cee42"
SCHEMA = "work/unseen-proposition-acquisition-metadata@1"

if hashlib.sha256(PREVIOUS_HELPER.read_bytes()).hexdigest() != PREVIOUS_HELPER_SHA:
    raise ValueError("sealed prior metadata helper changed; stop")
spec = importlib.util.spec_from_file_location("previous_successor_metadata_helper", PREVIOUS_HELPER)
previous = importlib.util.module_from_spec(spec)
spec.loader.exec_module(previous)
backend = previous.backend
BACKEND = previous.BACKEND
BACKEND_SHA = previous.BACKEND_SHA


def require_current_closure():
    closure = backend.require_closure(HERE)
    registered = {(ROOT / name).resolve() for name in closure["code_sha256"]}
    inventory = {Path(name).resolve() for name in backend.product_hashes()}
    if not inventory <= registered:
        raise ValueError("current product inputs are outside the exact passing closure")
    if backend.sha(PREVIOUS_HELPER) != PREVIOUS_HELPER_SHA:
        raise ValueError("sealed prior metadata helper changed")
    if backend.sha(BACKEND) != BACKEND_SHA:
        raise ValueError("sealed metadata backend changed")
    return closure


def prior_metadata():
    if backend.sha(PREVIOUS_HELPER) != PREVIOUS_HELPER_SHA:
        raise ValueError("sealed prior metadata helper changed")
    excluded, hashes, seals = previous.prior_metadata()
    path = backend.work_path(PRIOR)
    seal_path = backend.work_path(PRIOR_SEAL)
    raw, seal_raw = path.read_bytes(), seal_path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    seal_digest = hashlib.sha256(seal_raw).hexdigest()
    if digest != PRIOR_SHA or seal_digest != PRIOR_SEAL_SHA:
        raise ValueError("original trial02 registration or seal changed")
    value, seal = json.loads(raw), json.loads(seal_raw)
    if (value.get("schema") != SCHEMA or seal.get("registration_sha256") != digest
            or value.get("family", {}).get("story_key") != "event:44:4"):
        raise ValueError("unsupported or unbound original trial02 metadata")
    if (value.get("prior_registration_sha256") != hashes
            or value.get("prior_registration_seal_sha256") != seals):
        raise ValueError("original trial02 prior-evidence chain changed")
    families = set(excluded)
    families.add(backend.event_family(value["family"]["story_key"]))
    for family in value.get("excluded_content_families", []):
        if not isinstance(family, str) or backend.event_family(family + ":1") != family:
            raise ValueError("invalid prior content-family metadata")
        families.add(family)
    hashes[str(path)] = digest
    seals[str(seal_path)] = seal_digest
    return sorted(families), hashes, seals


verify_hashes = previous.verify_hashes


def register(as_of_utc=None):
    require_current_closure()
    path = backend.new_stage(HERE, "metadata-registration.json")
    if not (HERE / "PROTOCOL.md").is_file():
        raise ValueError("freeze the successor prospective protocol before registration")
    frozen_inputs = {str(HERE / name): backend.sha(HERE / name)
                     for name in ("PROTOCOL.md", "product-closure.json", "register_trial.py")}
    census = HERE.parent / "census"
    excluded, prior_hashes, prior_seals = prior_metadata()
    metadata_hashes = {str(census / name): backend.sha(census / name)
                       for name in ("logical-units.jsonl", "census-index.sqlite")}
    candidates = backend.metadata_universe(census, excluded)
    family = deepcopy(candidates[0])
    usage = backend.audit_usage(family["story_key"],
                                [ROOT / name for name in ("work", "scripts", "tests", "agents")])
    as_of_utc = as_of_utc or datetime.now(timezone.utc).isoformat()
    family["release"] = backend.census_release(census, family, as_of_utc)
    require_current_closure()
    verify_hashes({**prior_hashes, **prior_seals, **metadata_hashes, **frozen_inputs})
    universe_sha = backend.freeze(HERE / "candidate-metadata-universe.json", candidates)
    value = dict(schema=SCHEMA, family=family, seed=backend.SEED,
        excluded_content_families=excluded, as_of_utc=as_of_utc,
        prior_registration_sha256=prior_hashes, prior_registration_seal_sha256=prior_seals,
        usage_audit=usage, candidate_universe_sha256=universe_sha,
        product_closure_sha256=frozen_inputs[str(HERE / "product-closure.json")],
        protocol_sha256=frozen_inputs[str(HERE / "PROTOCOL.md")],
        runner_sha256=frozen_inputs[str(HERE / "register_trial.py")], metadata_backend_sha256=BACKEND_SHA,
        prior_metadata_helper_sha256=PREVIOUS_HELPER_SHA,
        metadata_inputs=metadata_hashes, fixed_source_windows=40,
        source_or_target_bodies_read=False,
        reference_unit_denominator_frozen_before_acquisition=True)
    registration_sha = backend.freeze(path, value)
    backend.freeze(HERE / "metadata-registration-seal.json", dict(
        registration_sha256=registration_sha, candidate_universe_sha256=universe_sha,
        product_closure_sha256=value["product_closure_sha256"]))
    return dict(story_key=family["story_key"], registration_sha256=registration_sha, body_reads=0)


def verify():
    require_current_closure()
    value = json.loads((HERE / "metadata-registration.json").read_bytes())
    seal = json.loads((HERE / "metadata-registration-seal.json").read_bytes())
    if (value.get("schema") != SCHEMA or value.get("seed") != backend.SEED
            or backend.sha(HERE / "metadata-registration.json") != seal["registration_sha256"]
            or value["product_closure_sha256"] != backend.sha(HERE / "product-closure.json")
            or value["protocol_sha256"] != backend.sha(HERE / "PROTOCOL.md")
            or value["runner_sha256"] != backend.sha(__file__)
            or value["metadata_backend_sha256"] != BACKEND_SHA
            or value["prior_metadata_helper_sha256"] != PREVIOUS_HELPER_SHA
            or value["candidate_universe_sha256"] != backend.sha(HERE / "candidate-metadata-universe.json")):
        raise ValueError("successor registration inputs changed")
    backend.event_family(value.get("family", {}).get("story_key"))
    excluded, priors, seals = prior_metadata()
    if (value["excluded_content_families"] != excluded
            or value["prior_registration_sha256"] != priors
            or value["prior_registration_seal_sha256"] != seals):
        raise ValueError("original prior metadata evidence changed or was omitted")
    verify_hashes({**value["metadata_inputs"], **priors, **seals})
    return value


def capture(database):
    value = verify()
    output = HERE / "body-handoff-01"
    output.mkdir()
    pages = {}
    with sqlite3.connect(Path(database).resolve().as_uri() + "?mode=ro", uri=True) as conn:
        conn.row_factory = sqlite3.Row
        for language, metadata in value["family"]["pages"].items():
            row = conn.execute("SELECT text,text_hash FROM web_pages WHERE source=? AND id=?",
                               (metadata["source"], metadata["page_id"])).fetchone()
            if row is None or row["text_hash"] != metadata["text_sha256"]:
                raise ValueError("registered page missing or stale: " + language)
            path = output / (language + ".txt")
            with path.open("xb") as stream:
                stream.write(row["text"].encode("utf-8"))
            if backend.sha(path) != metadata["text_sha256"]:
                raise ValueError("registered body hash mismatch: " + language)
            pages[language] = {key: metadata[key] for key in
                              ("source", "page_id", "language", "text_sha256")}
            pages[language]["body_file"] = str(path.resolve())
    verify()
    digest = backend.freeze(output / "handoff.json", dict(
        schema="work/source-first-registered-body-handoff@1",
        registration_sha256=backend.sha(HERE / "metadata-registration.json"), pages=pages))
    return dict(captured_pages=len(pages), production_database_writes=0, handoff_sha256=digest)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("register", "verify", "capture"))
    parser.add_argument("--as-of-utc", help="Optional fixed registration release-check timestamp")
    parser.add_argument("--database", type=Path, default=ROOT / "store/kb/sekaisync.db")
    args = parser.parse_args()
    if args.stage == "register":
        result = register(args.as_of_utc)
    elif args.stage == "verify":
        value = verify()
        result = dict(story_key=value["family"]["story_key"], registration_verified=True, body_reads=0)
    else:
        result = capture(args.database)
    print(json.dumps(result, indent=2))
