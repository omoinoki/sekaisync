"""Recheck archived signatures, constructors and existing scraper CLI helps."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def _signatures(path):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    signatures = {}
    for node in tree.body:
        members = [(node.name, node)] if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) else []
        if isinstance(node, ast.ClassDef):
            members = [(node.name + "." + child.name, child) for child in node.body
                       if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                       and (not child.name.startswith("_") or child.name in {"__init__", "__call__"})]
        for name, function in members:
            if not name.startswith("_"):
                signatures[name] = dict(kind=type(function).__name__, arguments=ast.dump(function.args),
                                        returns=ast.dump(function.returns) if function.returns else None)
    return signatures


def _help(root, arguments):
    env = dict(os.environ, PYTHONPATH=str(root.resolve()), PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, "-X", "utf8", "-m", "sekaisync", *arguments],
                          cwd=root, env=env, capture_output=True, text=True, encoding="utf-8", check=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=ROOT / "work/llm-repair-20260929/baseline")
    parser.add_argument("--manifest", type=Path, default=ROOT / "work/llm-repair-20260929/baseline-manifest.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("interface audit is frozen; choose another output")
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    changed, checked, top_level, verified = [], 0, 0, 0
    for name, digest in manifest.items():
        archived = args.baseline / name
        if hashlib.sha256(archived.read_bytes()).hexdigest() != digest:
            raise ValueError("archived baseline hash changed: " + name)
        verified += 1
        if archived.suffix != ".py":
            continue
        before = _signatures(archived)
        after = _signatures(ROOT / name)
        for function, signature in before.items():
            checked += 1
            top_level += "." not in function
            if after.get(function) != signature:
                changed.append(dict(file=name, function=function, before=signature, after=after.get(function)))
    helps = []
    for arguments in (["terms", "extract", "--help"], ["terms", "review", "--help"], ["terms", "zhfirst", "--help"]):
        before, after = _help(args.baseline, arguments), _help(ROOT, arguments)
        helps.append(dict(command=arguments, baseline_exit=before.returncode, current_exit=after.returncode,
                          help_unchanged=before.returncode == after.returncode == 0 and before.stdout == after.stdout,
                          baseline_stdout_sha256=hashlib.sha256(before.stdout.encode()).hexdigest(),
                          current_stdout_sha256=hashlib.sha256(after.stdout.encode()).hexdigest(),
                          baseline_stderr=before.stderr, current_stderr=after.stderr))
    report = dict(schema="sekaisync/scraper-interface-audit@2", archived_baseline=str(args.baseline.resolve()),
                  verified_baseline_files=verified, public_signatures_checked=checked,
                  public_top_level_signatures_checked=top_level, changed_signatures=changed, cli_help=helps)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if changed or any(not result["help_unchanged"] for result in helps):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
