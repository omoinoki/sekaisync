"""One bounded, receipt-derived source boundary review using normal discovery.

This is workflow accounting, not a vocabulary-completeness certificate. Existing
discovery answer fields remain valid; no model SDK or public command is added.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path


_SCHEMA = "sekaisync/source-boundary-audit@1"
_KEY = "source_boundary_audit"
_FIELDS = {"schema", "stage", "ordinary_ancestor_ids", "terminal_parent_id",
           "excluded_terms", "excluded_subject_ids", "inherited_subjects"}


def _same(left, right):
    return json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False) == json.dumps(
                          right, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False)


def _is_audit(context):
    return isinstance(context, dict) and _KEY in context


def _is_saturated(context):
    marker = context.get(_KEY) if isinstance(context, dict) else None
    return isinstance(marker, dict) and marker.get("stage") == "saturated"


def _excluded_subject_ids(context):
    marker = context.get(_KEY)
    return set(marker["excluded_subject_ids"]) if isinstance(marker, dict) else set()


def _receipt_context(item):
    context = item._context
    return deepcopy(context) if isinstance(context, dict) and context.get("task") == "discovery" else None


def _item(context):
    from sekaisync import agent_packets as packets
    rows = context["rows"]
    term = "@discover:" + rows[0]["story_key"] + ":" + rows[0]["id"]
    reason = ("source_boundary_audit_saturated: a bounded source review filled its budget; "
              "this named debt cannot be closed by an accept/reject or empty answer."
              if _is_saturated(context) else
              "Second source-only boundary review; reread every shown turn and add only meaningful "
              "exact units not already registered. Acceptance is not an exhaustive-coverage proof.")
    return packets._item(term, context["source_language"], [], "discovery", context, reason)


def _base(context):
    return {key: deepcopy(value) for key, value in context.items()
            if key not in {"continuation", _KEY}}


def _validate_base(conn, store, base):
    from sekaisync import agent_packets as packets
    if (not isinstance(base, dict)
            or set(base) != {"schema", "task", "scope_id", "source_language", "rows"}
            or base.get("schema") != packets._SCHEMA or base.get("task") != "discovery"
            or not isinstance(base.get("rows"), list) or not base["rows"]):
        raise ValueError("invalid bounded source audit root context")
    scope = packets._read_scope(store, base["scope_id"])
    if not _same(base["source_language"], scope["source_language"]):
        raise ValueError("source audit language changed outside its corpus scope")
    originals = {row["id"]: row for row in scope["windows"]}
    identities, pages = set(), {}
    for row in base["rows"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or row["id"] in identities or row["id"] not in originals
                or not _same(row, originals[row["id"]])):
            raise ValueError("source audit raw rows changed outside their exact corpus scope")
        identities.add(row["id"])
        packets._validate_view(conn, row["source"], pages)


def _subject(conn, rows, value):
    from sekaisync import agent_packets as packets, span_subjects
    if not isinstance(value, dict):
        raise ValueError("invalid source audit typed parent observation")
    packets._validate_subject_in_rows(conn, value, rows)
    for row in rows:
        try:
            arguments = [row["source"], row["story_key"]]
            constructor = span_subjects._literal if value["kind"] == "literal" else span_subjects._segmented
            if value["kind"] == "literal":
                arguments.append(value["canonical"])
            expected = constructor(*arguments, value["source"]["segments"])
        except (KeyError, TypeError, ValueError):
            continue
        if _same(value, expected):
            return value
    raise ValueError("source audit typed parent observation changed its exact raw serialization")


def _decode(conn, context, receipt, excluded_terms=(), excluded_subjects=()):
    from sekaisync import agent_packets as packets
    if (not isinstance(receipt, dict) or receipt.get("decision") != "accept"
            or not _same(receipt.get("scope_id"), context["scope_id"])
            or not isinstance(receipt.get("value"), str)):
        raise ValueError("source audit parent has no accepted same-scope receipt")
    if "review_context" in receipt and not _same(receipt["review_context"], context):
        raise ValueError("source audit receipt context changed its exact serialization")
    try:
        value = json.loads(receipt["value"])
    except (ValueError, TypeError) as exc:
        raise ValueError("invalid source audit parent receipt value") from exc
    if isinstance(value, dict):
        if set(value) != {"terms", "subjects"}:
            raise ValueError("invalid typed source audit parent receipt")
        terms, subjects = value["terms"], value["subjects"]
        if not isinstance(subjects, list) or len(subjects) > packets._DISCOVERY_TERMS:
            raise ValueError("invalid source audit parent typed budget")
    else:
        terms, subjects = value, None
    if (not isinstance(terms, list) or len(terms) > packets._DISCOVERY_TERMS
            or any(not packets._valid_surface(term) for term in terms)
            or len({packets._discovery_surface_key(term) for term in terms}) != len(terms)):
        raise ValueError("invalid source audit parent source surfaces")
    old_terms = {packets._discovery_surface_key(term) for term in excluded_terms}
    for term in terms:
        if (packets._discovery_surface_key(term) in old_terms or not any(
                packets.source_term_occurrences(row["source"]["text"].splitlines(), term)
                for row in context["rows"])):
            raise ValueError("source audit parent surface is not fresh grounded evidence")
    seen = set(excluded_subjects)
    for subject in subjects or []:
        _subject(conn, context["rows"], subject)
        if subject["id"] in seen:
            raise ValueError("source audit parent typed subject is not a fresh exact occurrence")
        seen.add(subject["id"])
    return terms, subjects


def _full(terms, subjects):
    from sekaisync import agent_packets as packets
    return len(terms) == packets._DISCOVERY_TERMS or len(subjects or []) == packets._DISCOVERY_TERMS


def _ancestors(value):
    if (not isinstance(value, list) or any(not isinstance(identity, str) for identity in value)
            or len(value) != len(set(value))):
        raise ValueError("invalid source audit ordinary ancestry")
    return value


def _replay(conn, store, base, ancestors, receipts):
    from sekaisync import agent_packets as packets
    _validate_base(conn, store, base)
    context, all_terms, all_subjects = deepcopy(base), [], []
    for identity in _ancestors(ancestors):
        parent = _item(context)
        if identity != parent.id:
            raise ValueError("source audit ordinary ancestry does not match its immutable packet")
        terms, subjects = _decode(conn, context, receipts.get(identity), all_terms,
                                  [subject["id"] for subject in all_subjects])
        if not _full(terms, subjects):
            raise ValueError("source audit ordinary ancestor did not submit a genuine full budget")
        all_terms.extend(terms)
        all_subjects.extend(subjects or [])
        context = packets._continuation_context(context, identity, terms, subjects)
    return context, all_terms, all_subjects


def _ordinary(conn, store, item, receipts):
    context = item._context
    if _is_audit(context):
        raise ValueError("source audit is not an ordinary discovery terminal")
    continuation = context.get("continuation")
    if "continuation" in context and not isinstance(continuation, dict):
        raise ValueError("invalid ordinary source audit continuation")
    ancestors = (continuation or {}).get("ancestor_ids", [])
    expected, terms, subjects = _replay(conn, store, _base(context), ancestors, receipts)
    candidate = _item(expected)
    if (item.id != candidate.id or item.term != candidate.term
            or item.language != candidate.language or item.candidates
            or not _same(context, expected)):
        raise ValueError("source audit terminal context or exact exclusions were changed")
    return list(ancestors), terms, subjects


def _review_context(base, ancestors, terminal_id, terms, subjects):
    inherited = [dict(id=subject["id"], kind=subject["kind"], canonical=subject["canonical"],
                      source_segments=deepcopy(subject["source"]["segments"]))
                 for subject in sorted(subjects, key=lambda value: value["id"])]
    marker = dict(schema=_SCHEMA, stage="review", ordinary_ancestor_ids=list(ancestors),
                  terminal_parent_id=terminal_id, excluded_terms=sorted(set(terms)),
                  excluded_subject_ids=sorted({subject["id"] for subject in subjects}),
                  inherited_subjects=inherited)
    return dict(deepcopy(base), **{_KEY: marker})


def _validated_review(conn, store, item, receipts):
    context, marker = item._context, item._context.get(_KEY)
    if (not isinstance(marker, dict) or marker.get("schema") != _SCHEMA
            or marker.get("stage") != "review" or set(marker) != _FIELDS
            or "continuation" in context or not isinstance(marker.get("terminal_parent_id"), str)):
        raise ValueError("invalid source boundary review marker")
    ordinary, terms, subjects = _replay(conn, store, _base(context),
                                        marker["ordinary_ancestor_ids"], receipts)
    parent = _item(ordinary)
    if marker["terminal_parent_id"] != parent.id:
        raise ValueError("source audit terminal parent does not match its immutable packet")
    found, typed = _decode(conn, ordinary, receipts.get(parent.id), terms,
                           [subject["id"] for subject in subjects])
    if _full(found, typed):
        raise ValueError("source audit terminal parent has not ended its full-budget ordinary chain")
    terms.extend(found)
    subjects.extend(typed or [])
    expected = _item(_review_context(_base(context), marker["ordinary_ancestor_ids"],
                                     parent.id, terms, subjects))
    if (item.id != expected.id or item.term != expected.term or item.language != expected.language
            or item.candidates or not _same(context, expected._context)):
        raise ValueError("source audit inherited observations or lineage were changed")
    return terms, subjects


def _saturated(review, terms, subjects):
    context = deepcopy(review._context)
    marker = context[_KEY]
    marker.update(stage="saturated", audit_parent_id=review.id,
                  excluded_terms=sorted(set(marker["excluded_terms"]) | set(terms)),
                  excluded_subject_ids=sorted(set(marker["excluded_subject_ids"])
                                             | {subject["id"] for subject in subjects or []}))
    marker["inherited_subjects"] = sorted(marker["inherited_subjects"] + [
        dict(id=subject["id"], kind=subject["kind"], canonical=subject["canonical"],
             source_segments=deepcopy(subject["source"]["segments"])) for subject in subjects or []],
        key=lambda value: value["id"])
    return _item(context)


def _validate_saturated(conn, store, item, receipts):
    marker = item._context.get(_KEY)
    if (not isinstance(marker, dict) or set(marker) != _FIELDS | {"audit_parent_id"}
            or marker.get("schema") != _SCHEMA or marker.get("stage") != "saturated"):
        raise ValueError("invalid source audit saturation debt")
    review_context = deepcopy(item._context)
    ordinary, terms, subjects = _replay(conn, store, _base(review_context),
                                        marker["ordinary_ancestor_ids"], receipts)
    terminal = _item(ordinary)
    found, typed = _decode(conn, ordinary, receipts.get(terminal.id), terms,
                           [subject["id"] for subject in subjects])
    if _full(found, typed):
        raise ValueError("source audit saturation has no sub-capacity ordinary terminal")
    terms.extend(found)
    subjects.extend(typed or [])
    review = _item(_review_context(_base(review_context), marker["ordinary_ancestor_ids"],
                                   terminal.id, terms, subjects))
    _validated_review(conn, store, review, receipts)
    audit_terms, audit_subjects = _decode(conn, review._context, receipts.get(review.id), terms,
                                         [subject["id"] for subject in subjects])
    if not _full(audit_terms, audit_subjects):
        raise ValueError("source audit saturation requires a real full-budget audit receipt")
    expected = _saturated(review, audit_terms, audit_subjects)
    if (item.id != expected.id or item.term != expected.term or item.language != expected.language
            or item.candidates or not _same(item._context, expected._context)):
        raise ValueError("source audit saturation debt context or lineage was changed")


def _validate(conn, store, item):
    from sekaisync import agent_packets as packets
    receipts = packets._receipts(store, conn=conn)
    if _is_saturated(item._context):
        _validate_saturated(conn, store, item, receipts)
        raise ValueError("source_boundary_audit_saturated debt cannot be closed by a semantic answer; "
                         "a bounded follow-up acquisition/review design is required")
    terms, _ = _validated_review(conn, store, item, receipts)
    return {packets._discovery_surface_key(term) for term in terms}


def _after_answer(conn, store, item, answer):
    from sekaisync import agent_packets as packets
    if answer.get("task") != "discovery":
        return answer
    receipts = packets._receipts(store, conn=conn)
    receipt = dict(decision="accept", scope_id=item._context["scope_id"], value=answer["value"],
                   review_context=deepcopy(item._context))
    if _is_audit(item._context):
        if _is_saturated(item._context):
            _validate(conn, store, item)
        terms, subjects = _validated_review(conn, store, item, receipts)
        found, typed = _decode(conn, item._context, receipt, terms,
                               [subject["id"] for subject in subjects])
        if any(child._context.get("task") == "discovery" for child in answer["items"]):
            raise ValueError("source audit must not create an ordinary capacity continuation")
        if _full(found, typed):
            answer["items"].append(_saturated(item, found, typed))
        answer["source_audit"] = dict(state="saturated" if _full(found, typed) else "reviewed",
                                      procedural_only=True)
        return answer
    ancestors, terms, subjects = _ordinary(conn, store, item, receipts)
    found, typed = _decode(conn, item._context, receipt, terms,
                           [subject["id"] for subject in subjects])
    if _full(found, typed):
        return answer
    terms.extend(found)
    subjects.extend(typed or [])
    review = _item(_review_context(_base(item._context), ancestors, item.id, terms, subjects))
    answer["items"].append(review)
    return answer


def _recover_missing_decisions(conn, store, receipts):
    from sekaisync import agent_packets as packets, agent_review as review
    count = 0
    rows = conn.execute(
        "SELECT " + review._QUEUE_READ_COLUMNS + " FROM review_queue q "
        "WHERE q.status='resolved' AND q.item_id LIKE 'arp:%' "
        "AND NOT EXISTS(SELECT 1 FROM review_decisions d WHERE d.item_id=q.item_id)").fetchall()
    for row in rows:
        item = review._queue_row_to_item(row)
        if not isinstance(item._context, dict) or item._context.get("task") != "discovery":
            continue
        try:
            if _is_saturated(item._context):
                _validate_saturated(conn, store, item, receipts)
            elif _is_audit(item._context):
                _validated_review(conn, store, item, receipts)
            else:
                _ordinary(conn, store, item, receipts)
            count += conn.execute("UPDATE review_queue SET status='queued' WHERE item_id=? "
                                  "AND status='resolved'", (item.id,)).rowcount
        except (KeyError, TypeError, ValueError, OSError):
            continue
    return count


def _ensure(store):
    from sekaisync import agent_packets as packets, agent_review as review, dbstore
    from sekaisync.fetcher import store_writer_lock
    children, seen, settled = [], set(), set()
    result = dict(added=0, pending=0, saturated=0, invalid=0, legacy_without_context=0,
                  retired=0, requeued=0)
    if not (Path(store) / "kb" / "sekaisync.db").exists():
        return result
    with store_writer_lock(store):
        with dbstore.connect(store) as conn:
            receipts = packets._receipts(store, conn=conn)
            for identity, receipt in sorted(receipts.items()):
                context = receipt.get("review_context") if isinstance(receipt, dict) else None
                if context is None:
                    result["legacy_without_context"] += 1
                    continue
                if not isinstance(context, dict) or context.get("task") != "discovery":
                    continue
                try:
                    parent = _item(deepcopy(context))
                    if parent.id != identity:
                        raise ValueError("source audit recovery receipt identity changed")
                    if _is_audit(context):
                        if _is_saturated(context):
                            raise ValueError("source audit saturation debt cannot have a semantic receipt")
                        terms, subjects = _validated_review(conn, store, parent, receipts)
                        found, typed = _decode(conn, context, receipt, terms,
                                               [subject["id"] for subject in subjects])
                        child = _saturated(parent, found, typed) if _full(found, typed) else None
                    else:
                        ancestors, terms, subjects = _ordinary(conn, store, parent, receipts)
                        found, typed = _decode(conn, context, receipt, terms,
                                               [subject["id"] for subject in subjects])
                        child = None if _full(found, typed) else _item(_review_context(
                            _base(context), ancestors, parent.id, terms + found, subjects + (typed or [])))
                    settled.add(identity)
                    if child is None or child.id in seen or child.id in receipts:
                        continue
                    seen.add(child.id)
                    children.append(child)
                    result["saturated" if _is_saturated(child._context) else "pending"] += 1
                except (KeyError, TypeError, ValueError, OSError):
                    result["invalid"] += 1
            if dbstore._meta_get(conn, "schema_version") in {"2", "3"}:
                result["requeued"] = _recover_missing_decisions(conn, store, receipts)
                result["added"] = sum(packets._insert_packet(conn, child) for child in children)
                if result["added"] or result["requeued"]:
                    dbstore.bump_revision(conn)
                    conn.commit()
            else:
                queued = review._read_queue(store)
                remaining = [item for item in queued if item.id not in settled]
                result["retired"] = len(queued) - len(remaining)
                if result["retired"]:
                    review._write_queue(store, remaining)
                if children:
                    result["added"] = review._enqueue_json_locked(store, children)["added"]
    return result


def _render_contract(item):
    if not _is_audit(item._context):
        return []
    if _is_saturated(item._context):
        return ["source_boundary_audit_saturated: this named source-review capacity debt is "
                "not a semantic judgment task. Do not submit accept/reject, an empty list, "
                "or claim exhaustive coverage to erase it. Genuine additional observations "
                "were committed, but bounded follow-up review remains required."]
    return ["source_boundary_audit_contract: this is the one second source-only review after "
            "a real ordinary discovery terminal. Reread every raw turn before consulting "
            "inherited_subjects and excluded_terms. Register only additional meaningful exact "
            "head/reference/modifier, inflected core/discourse-layer, predicate-plus-complement, "
            "comparison/modal or genuine discontinuous units. A broad envelope does not register "
            "its core; overlapping fragments do not register their composite. Preserve complete "
            "original contractions and inflected predicates AND register their recognized contextual "
            "bound grammatical layers as additional exact observations: auxiliaries (including clitic "
            "auxiliaries), negation/genitive clitics, case/topic/quotative functions and complete "
            "ending-plus-auxiliary constructions. Recheck lexical nominal heads and their "
            "determiner-bearing or case/topic-marked references separately. A recognized bound layer "
            "need not be orthographically detachable; an inner layer does not replace its complete "
            "form, and a complete form does not register its meaningful inner layer. Require an actual "
            "contextual grammatical role, not a matching suffix string or arbitrary stem/suffix split. "
            "Preserve realized raw spelling/Unicode, exact offsets and LF/CRLF; never invent a lemma "
            "or absent uncontracted alias, mechanically trim suffixes, enumerate substrings, or inspect "
            "another language. "
            "Use existing decision=accept, terms and optional subjects. An empty answer ends "
            "this one workflow stage only; a full list retains visible saturation debt. "
            "Neither an accepted answer nor its receipt proves semantic completeness.",
            "source_boundary_audit_frames: explicitly check each raw turn for meaningful "
            "sequencing, condition/concession, comparison, correlative and coordination frames. "
            "A genuine open-slot frame is one unified contextual construction: select its "
            "required raw operators together as a segmented subject and preserve the intervening "
            "lexical gaps. Multiple raw fragments must form that real construction, not unrelated "
            "fragments. A union of separately registered fragments is not the unified subject; "
            "a broad continuous envelope is not a substitute for its actual frame.",
            "source_boundary_audit_coordinated_nominals: recheck meaningful coordinated nominal "
            "whole/reference/individual-conjunct inner layers separately, including actual "
            "modifier-plus-head layers inside a larger coordination. Select only text present "
            "in the raw source. Never synthesize omitted or shared modifiers into absent raw "
            "strings, create an alias by closing lexical gaps, or enumerate all substrings."]
