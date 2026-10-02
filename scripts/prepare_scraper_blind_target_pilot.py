"""Ground host target observations before exposing independent target labels."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync import occurrence_store as ledger, span_subjects

KINDS = {"lexical", "paraphrase", "reference", "omitted", "unresolved"}


def _digest(raw):
    return hashlib.sha256(raw).hexdigest()


def _code_hashes():
    return {str(path.resolve()): _digest(path.read_bytes())
            for path in [Path(__file__)] + sorted((ROOT / "sekaisync").rglob("*.py"))}


_IMPORT_HASHES = _code_hashes()


def _validate_window(view, story_key, language):
    if (not isinstance(view, dict) or view.get("complete") is not True
            or type(view.get("start")) is not int or type(view.get("end")) is not int
            or not isinstance(view.get("text"), str) or not 0 <= view["start"] < view["end"]
            or view["end"] - view["start"] != len(view["text"])
            or ledger._language(view.get("language")) != ledger._language(language)):
        raise ValueError("blind target packet requires a complete exact raw window with integer offsets")
    ledger._anchor(view, story_key, [dict(start=view["start"], end=view["end"], exact=view["text"])])


def _prepare(packet, proposal):
    canonical = json.dumps({key: value for key, value in packet.items() if key != "packet_sha256"},
                           ensure_ascii=False, sort_keys=True).encode()
    if _digest(canonical) != packet["packet_sha256"]:
        raise ValueError("blind target packet canonical hash mismatch")
    if (packet.get("target_spans_or_labels_in_packet") is not False
            or proposal.get("target_reference_read") is not False):
        raise ValueError("blind target proposal requires candidate-free target label isolation")
    languages = packet["target_languages"]
    items = packet["items"]
    observations = proposal["items"]
    by_id = {item["source_annotation_id"]: item for item in items}
    identities = [item["source_annotation_id"] for item in observations]
    if (len(by_id) != len(items) or len(set(languages)) != len(languages)
            or len(identities) != len(set(identities)) or set(identities) != set(by_id)):
        raise ValueError("blind target proposal must select every fixed source exactly once")
    records = []
    for item in observations:
        original = by_id[item["source_annotation_id"]]
        source = original["source_context"]
        _validate_window(source, packet["story_key"], packet["source_language"])
        source_anchor = ledger._anchor(source, packet["story_key"], original["source_segments"])
        span_subjects._utterance(source, source_anchor["segments"])
        if set(item["targets"]) != set(languages):
            raise ValueError("blind target proposal must judge every fixed direction")
        for language in languages:
            observation = item["targets"][language]
            kind, parts, reason = observation["kind"], observation["parts"], observation["reason"]
            if (kind not in KINDS or not isinstance(parts, list)
                    or not isinstance(reason, str) or not reason.strip()):
                raise ValueError("blind target observation requires relation type, raw parts and reason")
            if (kind in {"omitted", "unresolved"}) != (not parts):
                raise ValueError("only omitted or unresolved judgments require empty target parts")
            view, segments, cursor = original["target_contexts"][language], [], 0
            _validate_window(view, packet["story_key"], language)
            for exact in parts:
                if not isinstance(exact, str) or not exact.strip():
                    raise ValueError("blind target part must contain exact nonempty original text")
                matches = [index for index in range(len(view["text"])) if view["text"].startswith(exact, index)]
                if len(matches) != 1 or matches[0] < cursor:
                    raise ValueError("blind target part requires one unambiguous ordered occurrence")
                offset = matches[0]
                segments.append(dict(start=view["start"] + offset, end=view["start"] + offset + len(exact), exact=exact))
                cursor = offset + len(exact)
            if kind == "lexical":
                anchor = ledger._anchor(view, packet["story_key"], segments)
                span_subjects._utterance(view, anchor["segments"])
            records.append(dict(source_annotation_id=item["source_annotation_id"], target_language=language,
                                relation_type=kind, target_segments=segments, reason=reason))
    if (len(items) != packet["source_units"] or len(records) != packet["directed_requirements"]):
        raise ValueError("blind target packet denominator changed")
    return dict(schema=packet["judgment_contract"]["schema"], story_key=packet["story_key"],
                agent=proposal["agent"], packet_sha256=packet["packet_sha256"],
                target_reference_read=False, records=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--expected-packet-sha256", required=True)
    parser.add_argument("--proposal", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError("target pilot judgments are frozen; choose a fresh path")
    code = _code_hashes()
    if code != _IMPORT_HASHES:
        raise ValueError("target preparation dependencies changed after import; restart")
    packet_raw, proposal_raw = args.packet.read_bytes(), args.proposal.read_bytes()
    if _digest(packet_raw) != args.expected_packet_sha256:
        raise ValueError("blind target packet file hash mismatch")
    result = _prepare(json.loads(packet_raw), json.loads(proposal_raw))
    result["provenance"] = dict(packet_file_sha256=_digest(packet_raw), proposal_file_sha256=_digest(proposal_raw),
                                code_sha256=code, offsets="absolute_raw_unicode_code_points_end_exclusive")
    if (args.packet.read_bytes() != packet_raw or args.proposal.read_bytes() != proposal_raw
            or _code_hashes() != code):
        raise ValueError("blind target preparation inputs or dependency closure changed during execution")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(dict(records=len(result["records"]), path=str(args.out.resolve()),
                          judgments_sha256=_digest(args.out.read_bytes())), indent=2))


if __name__ == "__main__":
    main()
