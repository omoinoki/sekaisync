"""Delegate to the hash-pinned original isolated release callable, unchanged."""
from pathlib import Path
import hashlib
import runpy


if __name__ == "__main__":
    original = Path(__file__).resolve().parent.parent / "benchmark/baseline_callable.py"
    if hashlib.sha256(original.read_bytes()).hexdigest() != "54bfe6cd1d7c48ba03bd6b2e2d356fbfe572371976d86f18fd6824e62f834994":
        raise ValueError("original released baseline callable changed")
    runpy.run_path(str(original), run_name="__main__")
