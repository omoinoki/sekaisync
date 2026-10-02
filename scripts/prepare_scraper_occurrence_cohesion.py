"""Prepare a reproducible, label-assisted same-occurrence meeting diagnostic.

This repairs an explicitly inspected local occurrence. It does not modify the
frozen blind proposals or their evaluation, and never publishes global names.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as os


_SOURCE_CONTEXT = {
    "ja": "\u3060\u304b\u3089\u3001\u304a\u5144\u3061\u3083\u3093\u9054\u3068\u306e\u6253\u3061\u3042\u308f\u305b\u3082\u884c\u304d\u305f\u3044\u306a\u3041\u3063\u3066\u2026\u2026",
    "zh_tw": "\u6240\u4ee5\u6211\u60f3\u8ddf\u54e5\u54e5\u4ed6\u5011\u4e00\u8d77\u53bb\u958b\u6703\u2026\u2026",
    "ko": "\uadf8\ub7ec\ub2c8\uae4c \uc800\ub3c4 \uc624\ube60\ub4e4\uc774\ub791 \ud68c\uc758\uc5d0 \uac00\uace0 \uc2f6\uc5b4\uc694\u2026\u2026",
}
_TARGET_CONTEXT = {
    "en": "your meeting with Keisuke and Shosuke",
    "zh_hans": "\u60a8\u548c\u54e5\u54e5\u4eec\u7684\u4f1a\u8bae",
    "ja": "\u304a\u5144\u3061\u3083\u3093\u9054\u3068\u306e\u6253\u3061\u3042\u308f\u305b",
    "zh_tw": "\u8ddf\u54e5\u54e5\u4ed6\u5011\u4e00\u8d77\u53bb\u958b\u6703",
    "ko": "\uc624\ube60\ub4e4\uc774\ub791 \ud68c\uc758",
}


def _surface(concept, language):
    return concept["surfaces"]["zh_hant" if language == "zh_tw" else language]


def _segments_in_context(view, surface, exact_context):
    text, base = view["text"], view["start"]
    if text.count(exact_context) != 1:
        raise ValueError("reviewed semantic context does not identify one exact local occurrence")
    start = base + text.index(exact_context)
    end = start + len(exact_context)
    choices = [choice for choice in ap._body_term_segments(text, surface, base)
               if all(part["start"] >= start and part["end"] <= end for part in choice)]
    if len(choices) != 1:
        raise ValueError("reviewed semantic context does not select one lexical body occurrence")
    return choices[0]


def _target_is_covered(relations, source, sense, language, surface):
    return any(relation["source"]["id"] == source["id"] and relation["sense"] == sense
               and relation["target_language"] == language and relation["kind"] == "lexical"
               and "".join(part["exact"] for part in relation["target"]["segments"]) == surface
               for relation in relations)


def prepare(store, concepts_path, tasks_path):
    document = json.loads(Path(concepts_path).read_text(encoding="utf-8"))
    if document["story_key"] != "event:46:2":
        raise ValueError("this frozen diagnostic is scoped to event:46:2")
    concept = next(row for row in document["concepts"] if row["key"] == "business-meeting")
    originals = [ar.ReviewItem.from_dict(row) for row in json.loads(Path(tasks_path).read_text(encoding="utf-8"))]
    packets, judgments, anchors, coverage_before = [], [], {}, {}
    for source_language, source_context in _SOURCE_CONTEXT.items():
        term = _surface(concept, source_language)
        coverage_before[source_language] = []
        for target_language, target_context in _TARGET_CONTEXT.items():
            if source_language == target_language:
                continue
            matches = [item for item in originals if item.term == term and item.language == target_language
                       and item._context["source_language"] == source_language]
            if len(matches) != 1:
                raise ValueError("expected one frozen original task for each requested direction")
            original = matches[0]
            scope_id = original._context["scope_id"]
            scope = ap._read_scope(store, scope_id)
            translation = ap._translation_item(scope_id, scope, term, target_language, original.candidates)
            if (translation is None or translation.id != original._context["parent_translation_id"]
                    or ap._occurrence_item(translation).id != original.id):
                raise ValueError("original task does not reproduce its immutable translation parent")
            rows = [row for row in translation._context["rows"] if source_context in row["source"]["text"]]
            if len(rows) != 1:
                raise ValueError("reviewed Emu request is not unique in the original packet")
            row = rows[0]
            source_segments = _segments_in_context(row["source"], term, source_context)
            source = os._anchor(row["source"], row["story_key"], source_segments)
            sense = os._sense(os._identity("lex:", [source_language, term]), source_language,
                              concept["key"], concept["gloss"])
            prior = anchors.setdefault(source_language, source)
            if prior != source:
                raise ValueError("the target directions do not share one exact source anchor")
            with dbstore.connect(store) as conn:
                current = os._read_relations(conn, source_id=source["id"], target_language=target_language)
            actual_surface = _surface(concept, target_language)
            if _target_is_covered(current, source, sense, target_language, actual_surface):
                coverage_before[source_language].append(target_language)
                continue
            item = ap._focused_occurrence_item(translation, source, sense)
            target_segments = _segments_in_context(row["target"], actual_surface, target_context)
            proposal = dict(evidence_id=row["id"], source_segments=source_segments, target_segments=target_segments,
                            sense_key=concept["key"], sense_gloss=concept["gloss"], kind="lexical",
                            rationale="Label-assisted diagnostic: all five original bodies and the Emu request were reread. This exact later meeting occurrence has an explicit local meeting expression; no earlier same-word occurrence is borrowed or retired.")
            judgments.append(dict(id=item.id, decision="accept", generalize=None,
                                  agent="p0-discovery-b-label-assisted-cohesion", relations=[proposal]))
            packets.append(item)
    with dbstore.connect(store) as conn:
        for item, judgment in zip(packets, judgments):
            answer = ap._validate_answer(conn, store, item, judgment)
            for relation in answer["relations"]:
                os._validate_relation(conn, relation)
    return packets, judgments, anchors, coverage_before


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--concepts", type=Path, required=True)
    parser.add_argument("--tasks", type=Path, required=True)
    parser.add_argument("--out-prefix", type=Path, required=True)
    parser.add_argument("--submit", action="store_true")
    args = parser.parse_args()
    packets, judgments, anchors, coverage_before = prepare(args.store, args.concepts, args.tasks)
    with dbstore.connect(args.store) as conn:
        before_ids = {row["id"] for row in os._read_relations(conn)}
    result = dict(schema="sekaisync/occurrence-cohesion-diagnostic@1", story_key="event:46:2",
                  label_assisted_diagnostic=True, blind_score_changed=False, human_gold=False,
                  concept_key="business-meeting", directions=len(packets), shared_source_anchors=anchors,
                  coverage_rule="Diff current lexical target languages for each exact source_id and sense_id, not a public query's aggregate missing directions.",
                  focus_target_requirements=12, covered_targets_before=coverage_before,
                  proposals_sha256=hashlib.sha256(args.concepts.read_bytes()).hexdigest(),
                  frozen_original_tasks_sha256=hashlib.sha256(args.tasks.read_bytes()).hexdigest(),
                  enqueue=ar.enqueue(args.store, packets))
    prefix = args.out_prefix
    prefix.parent.mkdir(parents=True, exist_ok=True)
    ar._write_json(prefix.with_name(prefix.name + "-packets.json"), [item.to_dict() for item in packets])
    ar._atomic_write_text(prefix.with_name(prefix.name + "-packets.txt"), ar.DECIDE_HELP + "\n\n" +
                          "\n\n".join(ar.render_item(item, index) for index, item in enumerate(packets, 1)))
    ar._write_json(prefix.with_name(prefix.name + "-judgments.json"), judgments)
    if args.submit:
        submitted = ar.submit_judgments(args.store, judgments)
        result["submit"] = submitted
        if submitted["errors"]:
            ar._write_json(prefix.with_name(prefix.name + "-result.json"), result)
            raise ValueError("cohesion diagnostic submit failed")
        with dbstore.connect(args.store) as conn:
            after = os._read_relations(conn)
        after_ids = {row["id"] for row in after}
        if not before_ids <= after_ids:
            raise ValueError("unrelated or earlier real relations were retired by this additive diagnostic")
        focused = [row for row in after if row["review_item_id"] in {item.id for item in packets}]
        if len(focused) != len(packets) or any(row["kind"] != "lexical" for row in focused):
            raise ValueError("all requested exact focused relations were not read back")
        covered_after = {}
        for language, anchor in anchors.items():
            covered_after[language] = sorted({row["target_language"] for row in after
                                              if row["source"]["id"] == anchor["id"]
                                              and row["kind"] == "lexical"
                                              and row["sense"]["key"] == "business-meeting"})
            if set(covered_after[language]) != set(_TARGET_CONTEXT) - {language}:
                raise ValueError("the exact source focus still lacks a requested target language")
        result.update(current_new_focused_relations=len(focused), covered_targets_after=covered_after,
                      same_focus_four_target_complete=True, previously_current_relations_retired=0,
                      new_current_relations=len(after_ids - before_ids), global_slots_written=submitted["applied_slots"])
    ar._write_json(prefix.with_name(prefix.name + "-result.json"), result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
