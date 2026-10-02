"""Isolated synthetic checks only; never load corpus, references, stores or scores."""
from copy import deepcopy
import importlib.util
from pathlib import Path


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    here = Path(__file__).resolve().parent
    scorer = load(here / "score_contract.py", "synthetic_contract")
    item = dict(status="task_captured", subject_id="synthetic-subject", target_language="en",
                sense_key="synthetic-key", source_meaning="synthetic meaning")
    segments = [dict(start=1, end=2, exact="x")]
    relation = dict(sense=dict(term_id="synthetic-subject", key="synthetic-key", gloss="synthetic meaning"),
                    target_language="en", kind="lexical", target=dict(segments=segments))
    reference = dict(source_sense_equivalent=True, alternatives=[dict(kind="lexical", target_segments=segments)])
    evaluate = lambda observed=item, ref=reference, rows=None: scorer.evaluate(observed, ref,
                    [relation] if rows is None else rows, lambda language: language)
    assert evaluate() == (True, "strict_positive_match", True)
    for kind in ("paraphrase", "reference"):
        row, ref = deepcopy(relation), deepcopy(reference)
        row["kind"], ref["alternatives"][0]["kind"] = kind, kind
        assert evaluate(ref=ref, rows=[row]) == (True, "strict_positive_match", True)
    for kind in ("omitted", "unresolved"):
        row, ref = deepcopy(relation), deepcopy(reference)
        row["kind"], ref["alternatives"][0]["kind"] = kind, kind
        assert evaluate(ref=ref, rows=[row]) == (False, kind + "_gap", True)
    assert evaluate(observed=dict(item, status="pipeline_miss")) == (False, "pipeline_miss", False)
    assert evaluate(ref=dict(reference, source_sense_equivalent=False)) == (False, "source_sense_mismatch", False)
    assert evaluate(rows=[]) == (False, "normal_relation_missing_or_ambiguous", False)
    assert evaluate(rows=[relation, relation]) == (False, "normal_relation_missing_or_ambiguous", False)
    wrong = deepcopy(relation)
    wrong["target"]["segments"] = [dict(start=2, end=3, exact="y")]
    assert evaluate(rows=[wrong]) == (False, "target_boundary_disagreement", False)
    runner = load(here / "runner.py", "synthetic_path_binding")
    for name in ("prepare", "manifest", "registration", "submit", "freeze_references",
                 "freeze_host_senses", "select_targets", "consumers", "scalar_match"):
        function = getattr(runner, name)
        assert function.__globals__["HERE"] == here
        assert function.__globals__["__file__"] == str(here / "runner.py")
        assert function.__globals__["closure"] is runner.closure
    assert runner.original.HERE == here.parent / "benchmark"
    assert runner.inherited_baseline.__globals__["HERE"] == here
    assert runner.inherited_baseline.__globals__["baseline"] is runner.baseline
    assert runner.EXPECTED_TEST_COUNT == 2090
    class Collision:
        def __truediv__(self, name):
            return self

        def exists(self):
            return True

    original_here = runner.HERE
    runner.HERE = Collision()
    try:
        try:
            runner.baseline()
        except FileExistsError:
            pass
        else:
            raise AssertionError("baseline collision did not fail before stage writes")
    finally:
        runner.HERE = original_here
    print("PASS: 10 synthetic scorer cases, isolated inherited path bindings and no-write baseline collision preflight; no semantic inputs or stages.")


if __name__ == "__main__":
    main()
