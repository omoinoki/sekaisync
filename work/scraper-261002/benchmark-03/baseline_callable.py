"""Execute the pinned unchanged callable against this trial's fresh extraction."""
import hashlib
from pathlib import Path
import runpy
import sys

sys.dont_write_bytecode = True
source = Path(__file__).resolve().parent.parent / "benchmark-02/baseline_callable.py"
expected = "b79b2c4181e5a48a2b4813973633e57ecb3b5d91370c3d023597caf2d692fb98"
if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
    raise ValueError("frozen parent baseline callable changed")
runpy.run_path(str(source), run_name="__main__")
