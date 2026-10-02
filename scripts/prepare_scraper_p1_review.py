"""Recover immutable, occurrence-only work packets from a prior text export.

Only packet text and its content-addressed scope are read. Neither annotations
nor prior model answers are inputs to this isolated review preparation step.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sekaisync import agent_packets as ap
from sekaisync import agent_review as ar


def restore_translation_items(store: Path, packets: Path) -> list[ar.ReviewItem]:
    text = packets.read_text(encoding="utf-8-sig")
    blocks = re.split(r"(?m)^## (?:\d+\. )?id=", text)[1:]
    if not blocks:
        raise ValueError("input contains no exported review items")
    items = []
    for block in blocks:
        identity = block.split(None, 1)[0]
        match = re.search(r"(?m)^term: (.*?)  \|  lang: (.*?)  \|  hint:", block)
        context_match = re.search(r"(?m)^task_context: (.+)$", block)
        candidate_match = re.search(r"(?m)^candidates: (.*?)  \|  channels:", block)
        if not match or not context_match or not candidate_match:
            raise ValueError(f"missing immutable packet fields for {identity}")
        term, language = match.groups()
        context = json.loads(context_match.group(1))
        if context.get("task") != "translation":
            raise ValueError(f"expected a translation packet: {identity}")
        candidates = candidate_match.group(1)
        candidates = [] if candidates == "\uff08\u65e0\uff09" else candidates.split(" | ")
        scope_id = context["scope_id"]
        scope = ap._read_scope(store, scope_id)
        item = ap._translation_item(scope_id, scope, term, language, candidates)
        if item is None or item.id != identity:
            raise ValueError(f"regenerated immutable identity differs: {identity}")
        exported_rows = [json.loads(row) for row in re.findall(r"(?m)^context: (.+)$", block)]
        reconstructed_rows = []
        for row in item._context["rows"]:
            visible = dict(row)
            for side in ("source", "target"):
                visible[side] = {key: value for key, value in row[side].items() if key != "sha256"}
            reconstructed_rows.append(visible)
        if exported_rows != reconstructed_rows:
            raise ValueError(f"regenerated evidence differs from the original export: {identity}")
        restored_context = {key: value for key, value in item._context.items() if key != "rows"}
        if restored_context != context:
            raise ValueError(f"regenerated scope metadata differs: {identity}")
        items.append(item)
    if len({item.id for item in items}) != len(items):
        raise ValueError("input repeats an immutable review identity")
    return items


def _unique_segments(view: dict, surface: str, phrase: str | None = None) -> list[dict]:
    choices = ap._body_term_segments(view["text"], surface, view["start"])
    if phrase is not None:
        phrase_choices = ap._body_term_segments(view["text"], phrase, view["start"])
        if len(phrase_choices) != 1:
            raise ValueError(f"semantic phrase does not select one raw occurrence: {phrase!r}")
        phrase_positions = {index for part in phrase_choices[0]
                            for index in range(part["start"], part["end"])}
        choices = [choice for choice in choices if all(
            index in phrase_positions for part in choice
            for index in range(part["start"], part["end"]))]
    if len(choices) != 1:
        raise ValueError(f"explicit semantic disambiguation required for {surface!r}: {len(choices)} hits")
    return choices[0]


def _review_sense(term: str, evidence_id: str) -> tuple[str, str]:
    # These labels are explicit judgments for this frozen twenty-item review.
    common = {
        "\u6f14\u51fa\u9635\u5bb9": ("performer_lineup", "\u6d3b\u52a8\u7684\u53c2\u6f14\u8005\u7ec4\u5408\u53ca\u5176\u660e\u661f\u5b9e\u529b"),
        "\u82b1\u91cc\u5b9e\u4e43\u7406": ("person_minori_hanasato", "\u4eba\u7269\u82b1\u91cc\u5b9e\u4e43\u7406\u7684\u672c\u4eba\u59d3\u540d"),
        "\u670b\u53cb": ("social_friend", "\u53cb\u597d\u4ea4\u5f80\u7684\u4eba"),
        "\u7b11\u68a6": ("person_emu", "\u4eba\u7269\u7b11\u68a6\u7684\u79f0\u547c"),
    }
    if term in common:
        return common[term]
    if term == "\u58f0\u97f3":
        return {
            "span:2603ba25856f06704bba133b": ("singing_voice_timbre", "\u6f14\u5531\u7684\u55d3\u97f3\u6216\u97f3\u8272"),
            "span:2ea0b789b42701efcefdabcb": ("musical_sound", "\u4e00\u8d77\u6f14\u594f\u53d1\u51fa\u7684\u97f3\u4e50\u58f0\u54cd"),
            "span:962642c0d3b928545cdc81e6": ("opposing_opinions", "\u53cd\u5bf9\u610f\u89c1\u7684\u8868\u8fbe"),
        }[evidence_id]
    raise ValueError(f"no explicit contextual sense annotation for {term!r}")


def anchor_independent_review(items: list[ar.ReviewItem], review_path: Path) -> list[dict]:
    """Convert frozen semantic decisions to coordinates, rejecting ambiguities."""
    reviews = json.loads(review_path.read_text(encoding="utf-8"))
    by_parent = {review["id"]: review for review in reviews}
    if len(by_parent) != len(reviews):
        raise ValueError("independent review repeats an identity")
    judgments = []
    for item in items:
        review = by_parent[item._context["parent_translation_id"]]
        if (review["term"], review["language"]) != (item.term, item.language):
            raise ValueError("independent review subject does not match immutable packet")
        by_evidence = {row["evidence_id"]: row for row in review["per_evidence"]}
        if len(by_evidence) != len(review["per_evidence"]) or set(by_evidence) != {
                row["id"] for row in item._context["rows"]}:
            raise ValueError("independent review does not cover the packet's exact evidence rows")
        relations = []
        for row in item._context["rows"]:
            annotation = by_evidence[row["id"]]
            source_segments = _unique_segments(row["source"], item.term)
            surface = annotation["target_surface"]
            if surface is None:
                kind = "unresolved"
                target_segments = [dict(start=row["target"]["start"], end=row["target"]["end"],
                                        exact=row["target"]["text"])]
            else:
                label = annotation["relation"]
                if label.startswith("\u660e\u786e\u8bcd\u6c47\u5bf9\u5e94"):
                    kind = "lexical"
                elif label == "\u6539\u5199":
                    kind = "paraphrase"
                elif label == "\u6307\u4ee3":
                    kind = "reference"
                else:
                    raise ValueError(f"unsupported explicit review relation: {label!r}")
                target_segments = _unique_segments(row["target"], surface, annotation.get("target_phrase"))
            key, gloss = _review_sense(item.term, row["id"])
            relations.append(dict(evidence_id=row["id"], source_segments=source_segments,
                                  target_segments=target_segments, sense_key=key,
                                  sense_gloss=gloss, kind=kind, rationale=annotation["reason"]))
        judgments.append(dict(id=item.id, decision="accept", generalize=None,
                              agent="p0-discovery-b-independent-occurrence", relations=relations))
    if len(judgments) != len(reviews):
        raise ValueError("independent review contains extra items outside this packet set")
    return judgments


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", required=True, type=Path)
    parser.add_argument("--packets", required=True, type=Path)
    parser.add_argument("--out-prefix", required=True, type=Path)
    parser.add_argument("--expected-items", type=int, default=20)
    parser.add_argument("--independent-review", type=Path)
    parser.add_argument("--judgments-out", type=Path)
    args = parser.parse_args()
    if bool(args.independent_review) != bool(args.judgments_out):
        parser.error("--independent-review and --judgments-out must be used together")
    originals = restore_translation_items(args.store, args.packets)
    if len(originals) != args.expected_items:
        raise ValueError(f"expected {args.expected_items} packets, found {len(originals)}")
    occurrences = [ap._occurrence_item(item) for item in originals]
    judgments = (anchor_independent_review(occurrences, args.independent_review)
                 if args.independent_review else None)
    queued = ar.enqueue(args.store, occurrences)
    args.out_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out_prefix.with_suffix(".json")
    txt_path = args.out_prefix.with_suffix(".txt")
    ar._write_json(json_path, [item.to_dict() for item in occurrences])
    ar._atomic_write_text(txt_path, ar.DECIDE_HELP + "\n\n" + "\n\n".join(
        ar.render_item(item, index) for index, item in enumerate(occurrences, 1)) + "\n")
    result = dict(translation_items=len(originals), occurrence_items=len(occurrences),
                  evidence_rows=sum(len(item._context["rows"]) for item in occurrences),
                  enqueue=queued, json=str(json_path), text=str(txt_path))
    if judgments is not None:
        ar._write_json(args.judgments_out, judgments)
        result["judgments"] = str(args.judgments_out)
        result["relations"] = sum(len(row["relations"]) for row in judgments)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
