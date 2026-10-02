"""First actual released callable after a preserved pre-execution collision."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("unchanged_runner_recovery_261002", HERE / "runner.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)
RECOVERY = HERE / "baseline-recovery-01"


def baseline():
    failure = r.read(HERE.parent / "BASELINE_FAILURE_01.json")
    if failure["released_algorithm_started"] is not False:
        raise ValueError("this recovery is only for a retained pre-call failure")
    if RECOVERY.exists() or (HERE / "baseline/callable-result.json").exists():
        raise ValueError("preserve completed or partial attempts; never rerun")
    value = r.manifest()
    selection = r.read(HERE / "targets/selection.json")
    r.check(selection["input_sha256"])
    directory = r.fresh("baseline-recovery-01")
    archive = subprocess.run(["git", "archive", "05d2749", "sekaisync", "pyproject.toml"],
                             cwd=r.ROOT, capture_output=True)
    if archive.returncode:
        raise ValueError(archive.stderr.decode(errors="replace"))
    r.put(directory / "release.tar", archive.stdout)
    if r.b.sha(directory / "release.tar") != r.b.sha(HERE / "baseline/release.tar"):
        raise ValueError("released bytes differ from original pre-execution archive")
    code = directory / "code"
    code.mkdir()
    with tarfile.open(directory / "release.tar") as bundle:
        bundle.extractall(code, filter="data")
    pages, queries = directory / "pages.json", directory / "queries.json"
    r.put(pages, value["pages"])
    r.put(queries, [{key: source[key] for key in
                    ("unit_id", "source_language", "story_key", "source_canonical")}
                   for source in selection["selected_sources"]])
    output = directory / "callable-result.json"
    command = [sys.executable, str(HERE / "baseline_callable.py"), "--code-root", str(code),
               "--pages", str(pages), "--queries", str(queries), "--out", str(output)]
    process = subprocess.run(command, cwd=code, env=dict(os.environ, PYTHONPATH=str(code)),
                             capture_output=True, text=True, encoding="utf-8", timeout=1800)
    r.put(directory / "process.json", dict(command=command, returncode=process.returncode,
                                          stdout=process.stdout, stderr=process.stderr))
    if process.returncode:
        raise ValueError("fresh first released callable failed; retain this attempt: " + process.stderr)
    result = r.read(output)
    result.update(original_preexecution_failure_retained=True,
                  original_protocol_failure_not_overridden=True,
                  input_sha256={str(pages): r.b.sha(pages), str(queries): r.b.sha(queries),
                                str(directory / "release.tar"): r.b.sha(directory / "release.tar"),
                                str(output): r.b.sha(output), str(Path(__file__)): r.b.sha(__file__),
                                str(HERE.parent / "BASELINE_FAILURE_01.json"): r.b.sha(HERE.parent / "BASELINE_FAILURE_01.json"),
                                **{str(path): r.b.sha(path) for path in code.rglob("*.py")}, **r.hashes()})
    r.put(directory / "report.json", result)
    print(json.dumps(dict(actual_released_callable_run=True, original_failure_retained=True,
                          report=str(directory / "report.json")), indent=2))


def score():
    report = r.read(RECOVERY / "report.json")
    r.check(report["input_sha256"])
    original_read = r.read
    expected = HERE / "baseline/report.json"
    # Only a work scorer input route changes, never a metric or product validator.
    def routed(path):
        return original_read(RECOVERY / "report.json" if Path(path) == expected else path)
    r.read = routed
    try:
        result = r.score()
    finally:
        r.read = original_read
    r.put(HERE / "scores/baseline-recovery-binding.json", dict(
        original_protocol_pass=False, metric_rules_or_denominators_modified=False,
        baseline_report=str(RECOVERY / "report.json"), baseline_sha256=r.b.sha(RECOVERY / "report.json"),
        adapter_sha256=r.b.sha(__file__), original_scorer_sha256=r.b.sha(HERE / "runner.py"),
        original_failure_sha256=r.b.sha(HERE.parent / "BASELINE_FAILURE_01.json")))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("baseline", "score"))
    args = parser.parse_args()
    baseline() if args.stage == "baseline" else score()
