"""Apply the prospectively fixed no-omission-credit goal to frozen trial inputs."""
from __future__ import annotations

from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("frozen_runner_261002", HERE / "runner.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
POSITIVE = frozenset(("lexical", "paraphrase", "reference"))


def evaluate(item, reference, relations):
    if item["status"] != "task_captured":
        return False, "pipeline_miss", False
    if reference["source_sense_equivalent"] is not True:
        return False, "source_sense_mismatch", False
    matches = [row for row in relations
               if row["sense"]["term_id"] == item["subject_id"]
               and row["target_language"] == runner.b.ledger._language(item["target_language"])
               and row["sense"]["key"] == item["sense_key"]
               and row["sense"]["gloss"] == item["source_meaning"]]
    if len(matches) != 1:
        return False, "normal_relation_missing_or_ambiguous", False
    relation = matches[0]
    alternatives = reference["alternatives"]
    classification = any(relation["kind"] == alternative["kind"]
                         and relation["target"]["segments"] == alternative["target_segments"]
                         for alternative in alternatives)
    if relation["kind"] not in POSITIVE:
        return False, relation["kind"] + "_gap", classification
    if classification:
        return True, "strict_positive_match", True
    same_vector = any(relation["target"]["segments"] == alternative["target_segments"]
                      for alternative in alternatives)
    same_kind = any(relation["kind"] == alternative["kind"] for alternative in alternatives)
    reason = ("relation_kind_disagreement" if same_vector else
              "target_boundary_disagreement" if same_kind else "kind_and_vector_disagreement")
    return False, reason, False


def main():
    seal_path = HERE / "score-contract-seal.json"
    runner.check(runner.read(seal_path)["input_sha256"])
    value = runner.manifest()
    runner.refs("source")
    runner.refs("target")
    selected = runner.read(HERE / "targets/selection.json")
    runner.check(selected["input_sha256"])
    original_path = HERE / "scores/report.json"
    original = runner.read(original_path)
    references, relations = {}, {}
    for language in runner.LANGS:
        references.update({record["obligation_id"]: record for record in
                           runner.read(HERE / "target-references" / (language + ".json"))["records"]})
        with runner.db.connect(Path(value["languages"][language]["store"])) as conn:
            relations[language] = runner.b.ledger._read_relations(conn)
    directions = defaultdict(lambda: dict(denominator=0, correct=0))
    details, reasons = [], Counter()
    for item in selected["obligations"]:
        correct, reason, classification = evaluate(item, references[item["obligation_id"]],
                                                    relations[item["source_language"]])
        direction = item["source_language"] + "->" + item["target_language"]
        directions[direction]["denominator"] += 1
        directions[direction]["correct"] += int(correct)
        reasons[reason] += 1
        details.append(dict(obligation_id=item["obligation_id"], correct=correct, reason=reason,
                            kind_vector_classification_matches=classification))
    if len(details) != 200 or len(directions) != 20 or any(row["denominator"] != 10 for row in directions.values()):
        raise ValueError("fixed 200 obligations / 20 directions / 10 per direction required")
    count = sum(row["correct"] for row in directions.values())
    gates = dict(original["gates"], target_overall=count / 200 >= .90,
                 worst_twenty_direction=all(row["correct"] / 10 >= .80 for row in directions.values()))
    report = dict(schema="work/checkpoint261002-contract-score@1", status="PASS" if all(gates.values()) else "FAIL",
                  gates=gates, target_correct=count, target_denominator=200, directions=dict(directions),
                  reasons=dict(reasons), details=details, original_report_sha256=runner.b.sha(original_path),
                  original_target_correct=original["target_correct"], source=original["source"],
                  source_correct=original["source_correct"], source_denominator=original["source_denominator"],
                  current_common_correct=original["current_common_correct"], baseline_common_correct=original["baseline_common_correct"],
                  common_denominator=200, improvement_percentage_points=original["improvement_percentage_points"],
                  input_sha256={str(Path(__file__)): runner.b.sha(__file__),
                                str(seal_path): runner.b.sha(seal_path),
                                str(HERE / "TARGET_SCORE_CONTRACT_ADDENDUM.md"): runner.b.sha(HERE / "TARGET_SCORE_CONTRACT_ADDENDUM.md"),
                                str(original_path): runner.b.sha(original_path)},
                  semantic_certificate=False, original_inputs_or_answers_modified=False)
    runner.put(HERE / "scores/contract-report.json", report)
    print(json.dumps({key: val for key, val in report.items() if key != "details"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
