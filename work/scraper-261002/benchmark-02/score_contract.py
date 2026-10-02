"""Prospective strict correspondence: omissions and unresolved gaps never count."""
from __future__ import annotations

POSITIVE = frozenset(("lexical", "paraphrase", "reference"))


def evaluate(item, reference, relations, normalize_language):
    if item["status"] != "task_captured":
        return False, "pipeline_miss", False
    if reference["source_sense_equivalent"] is not True:
        return False, "source_sense_mismatch", False
    matches = [row for row in relations
               if row["sense"]["term_id"] == item["subject_id"]
               and row["target_language"] == normalize_language(item["target_language"])
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
