"""Run one unchanged current-product validation snapshot for checkpoint261002."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=True, indent=2)
        stream.write("\n")


def code_hashes():
    paths = [ROOT / "pyproject.toml"]
    for directory in ("sekaisync", "scripts", "tests"):
        paths.extend((ROOT / directory).rglob("*.py"))
    paths.extend((ROOT / "agents").rglob("*.md"))
    return {path.relative_to(ROOT).as_posix(): sha(path) for path in sorted(set(paths))
            if path.is_file() and "__pycache__" not in path.parts}


def run(output):
    output = output.resolve()
    if not output.is_relative_to(HERE) or output.exists():
        raise ValueError("use a fresh checkpoint-local output directory")
    output.mkdir(parents=True)
    temp = output / "temp"
    temp.mkdir()
    before = code_hashes()
    freeze(output / "hashes-before.json", before)
    command = [sys.executable, "-B", "-X", "utf8", "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-v"]
    env = dict(os.environ, TEMP=str(temp), TMP=str(temp), PYTHONPATH=str(ROOT), PYTHONIOENCODING="utf-8")
    started = time.monotonic()
    start_utc = datetime.now(timezone.utc).isoformat()
    with (output / "stdout.txt").open("x", encoding="utf-8") as stdout, (output / "stderr.txt").open("x", encoding="utf-8") as stderr:
        result = subprocess.run(command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr, timeout=1800)
    elapsed = time.monotonic() - started
    after = code_hashes()
    freeze(output / "hashes-after.json", after)
    raw = (output / "stderr.txt").read_text(encoding="utf-8")
    counts = re.findall(r"^Ran (\d+) tests in ([\d.]+)s$", raw, re.MULTILINE)
    count = int(counts[0][0]) if len(counts) == 1 else None
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    passed = result.returncode == 0 and count is not None and count >= 2072 and bool(re.search(r"^OK$", raw, re.MULTILINE)) and not changed
    archive = output / "exact-product.zip"
    if passed:
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as zipped:
            for name, expected in before.items():
                if sha(ROOT / name) != expected:
                    raise ValueError("product changed before archiving: " + name)
                zipped.write(ROOT / name, name)
        with zipfile.ZipFile(archive) as zipped:
            if zipped.testzip() is not None or set(zipped.namelist()) != set(before):
                raise ValueError("product archive inventory or CRC differs")
            if any(hashlib.sha256(zipped.read(name)).hexdigest() != expected for name, expected in before.items()):
                raise ValueError("product archive bytes differ")
    report = dict(schema="work/scraper-261002-validation@1", status="PASS" if passed else "FAIL",
                  started_at=start_utc, elapsed_seconds=elapsed, tests_run=count,
                  exit_code=result.returncode, command=command, code_sha256=after,
                  product_unchanged=not changed, changed_files=changed,
                  stdout_sha256=sha(output / "stdout.txt"), stderr_sha256=sha(output / "stderr.txt"),
                  before_manifest_sha256=sha(output / "hashes-before.json"),
                  after_manifest_sha256=sha(output / "hashes-after.json"), runner_sha256=sha(__file__),
                  product_archive=str(archive) if passed else None,
                  product_archive_sha256=sha(archive) if passed else None,
                  historical_semantic_labels_evaluated=False, semantic_accuracy_claim=False)
    freeze(output / "report.json", report)
    print(json.dumps(dict(status=report["status"], tests_run=count, elapsed_seconds=elapsed,
                          report=str(output / "report.json"), report_sha256=sha(output / "report.json")), indent=2), flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(run(args.output))
