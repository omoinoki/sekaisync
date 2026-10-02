# Checkpoint 261002 Evidence And Reproduction

This checkpoint commits product code, synthetic regression tests, narrow
evaluation tooling and goal/checkpoint documents. It does not commit production
stores, dialogue pages, host answers, machine reference labels or large archives.

## Engineering Verification

From the repository root, use Python 3.10 or newer and run:

```powershell
python -B -X utf8 -m unittest discover -s tests -t . -v
```

The actual local checkpoint run used bundled Windows Python 3.12.14. Other CI
platforms and Python versions are not claimed to have executed locally. Use a
fresh output for `run_validation.py`; it preserves full logs, before/after code
manifests and an exact ZIP. Inspect actual test count as well as status.
The final candidate adds nine synthetic bound-morphology tests to 2081 cases.

## Semantic Measurements

`benchmark/PROTOCOL.md` describes the first candidate's fixed source/target
measurement. `score_contract.py` implements the pre-host no-omission-credit
correction, and `baseline_recovery.py` preserves the original pre-call collision
while routing the first actual released callable result into the same metric.
`MEASUREMENT_CLOSED.json` contains only aggregate results and retained debt.
The original protocol did not pass; recovered measurements do not erase that.

`benchmark-02` is a new-candidate trial attempt whose registration failed before
body capture. Never remove its STOP or claim that its semantic stages ran.
Subsequent fresh-trial metadata exceptions must be independently reviewed
and prospectively frozen before readers, not repair old evidence in place.

Work tools have narrow code-only dependencies under
`work/p0-exhaustive-20261001`, explicitly included with the checkpoint. Registered
metadata, release proofs, prior-trial exclusion manifests, census files and the
read-only local production database remain external inputs. Hash bindings are
local evidence, not portable fixtures automatically supplied by Git clone.
Frozen trials/answers are not reusable unseen labels for new products.

A fresh clone can run the synthetic suite but cannot automatically reproduce
semantic scores without separately preserved registered inputs and independent
agents. Use new immutable output directories and fresh isolated source
references/hosts and target references/hosts. Preserve failures and never rerank
after seeing bodies. A source diagnostic is not full200-obligation quality
acceptance; source/target scores from different snapshots cannot be combined.

Machine references are not human gold. Exact geometry/lifecycle tests and real
receipts do not certify semantic correctness or the zero-error goal. Public
consumer scalar correctness and exact typed relation coverage are different
metrics; do not subtract one from the other or credit omitted, unresolved,
source-missing or unexecuted obligations as counterparts.
