"""Receipt-backed raw-region review, never a whole-page omission certificate."""
from __future__ import annotations

import json
from pathlib import Path

_SCHEMA = "sekaisync/subject-scan@1"
_REVIEW_SCHEMA = "sekaisync/subject-region-review@1"
_KINDS = {"lexical", "paraphrase", "reference", "unresolved"}
_DEBT_REASONS = {"unscanned_target_page_regions", "subject_scan_terminal_review_pending"}


def _review_declaration(target):
    return dict(schema=_REVIEW_SCHEMA, page_source=target["source"], page_id=target["page_id"],
                page_sha256=target["sha256"], start=target["start"], end=target["end"],
                complete_region_reviewed=True, claim="content_review_not_omission_proof")


def _valid_declaration(value, target):
    expected = _review_declaration(target)
    return (isinstance(value, dict) and set(value) == set(expected)
            and type(value.get("complete_region_reviewed")) is bool and value["complete_region_reviewed"]
            and type(value.get("start")) is int and type(value.get("end")) is int and value == expected)


def _validate_answer(item, judgment):
    if "scan" not in item._context:
        return None
    proposals = judgment.get("relations")
    if (not isinstance(proposals, list) or len(proposals) != 1 or not isinstance(proposals[0], dict)
            or proposals[0].get("kind") not in _KINDS):
        raise ValueError("subject scan requires one scoped lexical/paraphrase/reference/unresolved observation; no omitted certificate")
    expected = _review_declaration(item._context["rows"][0]["target"])
    if not _valid_declaration(judgment.get("reviewed_region"), item._context["rows"][0]["target"]):
        raise ValueError("subject scan requires an explicit complete raw-region review declaration, not just a small expression anchor")
    return expected


def _receipt(conn, store, relation, inventory):
    from sekaisync import agent_packets as packets
    if "subject_scan_receipts" not in inventory:
        inventory["subject_scan_receipts"] = packets._receipts(store, conn=conn)
    receipts = inventory["subject_scan_receipts"]
    receipt = receipts.get(relation["review_item_id"])
    try:
        values = json.loads(receipt["value"])
    except (TypeError, KeyError, json.JSONDecodeError):
        raise ValueError("subject scan ancestor has no completed relation receipt") from None
    if (receipt.get("decision") != "accept" or receipt.get("scope_id") != relation["grounding"]["scope_id"]
            or not isinstance(values, list) or relation["id"] not in values):
        raise ValueError("subject scan ancestry is not bound to its actual accepted relation receipt")


def _validated_progress(conn, store, relation, inventory):
    from sekaisync import agent_packets as packets, occurrence_store as ledger
    ledger._verify_identity(relation, "rel:")
    key = (relation["id"], relation["source"]["page_sha256"], relation["target"]["page_sha256"])
    progress = inventory.setdefault("subject_scan_progress", {})
    if key in progress:
        return progress[key]
    pages = inventory.setdefault("subject_scan_validation_pages", {})
    pages["subject_fallback_inventory"] = inventory
    ledger._validate_relation(conn, relation, pages)
    _receipt(conn, store, relation, inventory)
    proof, context = relation["grounding"], relation["grounding"]["context"]
    fallback, subject = context.get("fallback"), context.get("subject")
    if (not fallback or not subject or relation["kind"] not in _KINDS
            or context.get("expansion") or relation["source"] != subject["source"]
            or relation["sense"]["term_id"] != subject["id"]):
        raise ValueError("subject scan seed must be a bounded raw fallback for the same immutable source subject and sense")
    row = next(row for row in context["rows"] if row["id"] == proof["row_id"])
    target = row["target"]
    scan = context.get("scan")
    if "scan" in context:
        coverage = list(scan["reviewed_regions"])
        coverage.append(dict(start=target["start"], end=target["end"], relation_id=relation["id"]))
        progress[key] = (scan["root_relation_id"], coverage)
        return progress[key]
    if target["start"] != 0:
        raise ValueError("a subject scan seed must start at the original raw page prefix")
    # Only a whole-region unresolved citation already asserts context review.
    # A lexical anchor alone says nothing about the rest of its shown region.
    coverage = ([dict(start=0, end=target["end"], relation_id=relation["id"])]
                if relation["kind"] == "unresolved" else [])
    progress[key] = (relation["id"], coverage)
    return progress[key]


def _next_item(conn, store, relation, persist=True, inventory=None):
    from sekaisync import agent_packets as packets, agent_review as ar
    inventory = {} if inventory is None else inventory
    root_id, coverage = _validated_progress(conn, store, relation, inventory)
    context = relation["grounding"]["context"]
    origin_scope_id = context["fallback"]["origin_scope_id"]
    subject = context["subject"]
    baseline, _ = packets._subject_fallback_item(conn, store, origin_scope_id, subject,
                                                relation["target_language"], persist=False, inventory=inventory)
    if baseline is None:
        raise ValueError("subject scan current target page is unavailable")
    original_row = baseline._context["rows"][0]
    initial_target = original_row["target"]
    anchor = relation["target"]
    if ((anchor["page_source"], anchor["page_id"], anchor["page_sha256"]) !=
            (initial_target["source"], initial_target["page_id"], initial_target["sha256"])):
        raise ValueError("subject scan cannot accumulate coverage across target page versions or primary policy changes")
    start = coverage[-1]["end"] if coverage else 0
    length = baseline._context["fallback"]["target_page_code_points"]
    if start >= length:
        return None
    raw = conn.execute("SELECT text,language FROM web_pages WHERE source=? AND id=?",
                       (initial_target["source"], initial_target["page_id"])).fetchone()
    page = dict(source=initial_target["source"], id=initial_target["page_id"], language=raw[1], text=str(raw[0] or ""))
    target = packets._raw_region(page, start, min(start + packets._EXPANSION_CHARS, length))
    unshown = ([dict(start=0, end=start)] if start else []) + (
        [dict(start=target["end"], end=length)] if target["end"] < length else [])
    complete = start == 0 and target["end"] == length
    fallback = dict(baseline._context["fallback"], target_level="full_page" if complete else "bounded_page",
                    target_complete_page=complete,
                    unshown_target_regions=unshown)
    window = dict(story_key=relation["story_key"], source=original_row["source"], targets={relation["target_language"]: target})
    window["id"] = "span:" + packets._digest(window)[:24]
    scope = packets._read_scope(store, origin_scope_id)
    scan = dict(schema=_SCHEMA, origin_scope_id=origin_scope_id, subject_id=subject["id"],
                root_relation_id=root_id, parent_relation_id=relation["id"],
                parent_review_item_id=relation["review_item_id"], source_id=relation["source"]["id"],
                sense_id=relation["sense"]["id"], target_language=relation["target_language"],
                target_page_source=target["source"], target_page_id=target["page_id"], target_page_sha256=target["sha256"],
                target_page_code_points=length, reviewed_regions=coverage,
                region=dict(start=target["start"], end=target["end"]), coverage_kind="agent_declared_content_review_not_omission")
    scan_scope = dict(source_language=scope["source_language"], target_languages=list(scope["target_languages"]),
                      windows=[window], fallback=fallback, scan=scan)
    scope_id = packets._digest(scan_scope)
    if persist:
        path = packets._scope_path(store, scope_id)
        if not path.exists():
            ar._write_json(path, scan_scope)
        else:
            packets._read_scope(store, scope_id)
    scan_context = dict(schema=packets._SCHEMA, task="occurrence", scope_id=scope_id,
                        source_language=scope["source_language"], subject=subject, fallback=fallback, scan=scan,
                        focus=dict(source=relation["source"], sense=relation["sense"]), available_stories=1, available_spans=1,
                        rows=[dict(id=window["id"], story_key=window["story_key"], source=window["source"], target=target)])
    return packets._item(subject["canonical"], relation["target_language"], [], "pending", scan_context,
                         "Review the complete next bounded raw region for this fixed source and sense. Explicitly declare complete region review; finding one expression is not evidence that all shown text was reviewed. No terminal omission claim is allowed.")


def _validate_context(conn, store, context, language, term, inventory=None):
    from sekaisync import agent_packets as packets
    scan = context.get("scan")
    if not isinstance(scan, dict) or scan.get("schema") != _SCHEMA:
        raise ValueError("invalid subject scan ancestry")
    inventory = {} if inventory is None else inventory
    key = packets._digest([context, language, term])
    validated = inventory.setdefault("subject_scan_contexts", set())
    if key in validated:
        return
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scraper_relations'").fetchone():
        raise ValueError("subject scan parent relation is absent")
    row = conn.execute("SELECT payload_json FROM scraper_relations WHERE id=?", (scan.get("parent_relation_id"),)).fetchone()
    if row is None:
        raise ValueError("subject scan parent relation is absent")
    parent = json.loads(row[0])
    expected = _next_item(conn, store, parent, persist=False, inventory=inventory)
    if (expected is None or expected.language != language or expected.term != term
            or packets._digest(expected._context) != packets._digest(context)):
        raise ValueError("subject scan region, coverage, subject, sense or page version cannot be replayed from its actual receipt ancestry")
    validated.add(key)


def _validate_relation(conn, store, relation, pages=None):
    context = relation["grounding"]["context"]
    if "scan" not in context:
        return
    inventory = ({} if pages is None else pages.setdefault("subject_fallback_inventory", {}))
    _validate_context(conn, store, context, relation["target_language"], relation["grounding"]["term"], inventory)
    target = context["rows"][0]["target"]
    if not _valid_declaration(relation["grounding"].get("scan_review"), target) or relation["kind"] not in _KINDS:
        raise ValueError("subject scan relation lacks its complete content-region review declaration; omission aggregation is not implemented")


def _supersede(store, identities):
    from sekaisync import agent_review as ar, dbstore
    from sekaisync.fetcher import store_writer_lock
    if not identities:
        return
    with store_writer_lock(store):
        if ar._uses_sqlite(store):
            with dbstore.connect(store) as conn:
                changed = sum(conn.execute("UPDATE review_queue SET status='superseded' WHERE item_id=? AND status='queued'",
                                           (identity,)).rowcount for identity in identities)
                if changed:
                    dbstore.bump_revision(conn)
                conn.commit()
        else:
            payload = ar._read_json(ar.queue_path(store))
            payload["items"] = [item for item in payload.get("items", []) if item.get("id") not in identities]
            ar._write_json(ar.queue_path(store), payload)


def ensure_subject_scans(store):
    """Recover the next receipt-backed raw region via the unchanged export."""
    from sekaisync import agent_packets as packets, agent_review as ar, dbstore, occurrence_store as ledger
    if not (Path(store) / "kb" / "sekaisync.db").exists():
        return dict(added=0, retired=0)
    pending = ar.load_queue(store)
    gaps = [item for item in pending if item._context.get("task") == "subject_gap"
            and item._context["gap"].get("reason") in _DEBT_REASONS]
    if not gaps:
        return dict(added=0, retired=0)
    items, retired, inventory = [], [], {}
    with dbstore.connect(store) as conn:
        current = ledger._read_relations(conn, subject_ids=sorted({item._context["subject"]["id"] for item in gaps}))
        for debt in gaps:
            context, subject = debt._context, debt._context["subject"]
            focus = context.get("focus")
            try:
                baseline, gap = packets._subject_fallback_item(conn, store, context["scope_id"], subject,
                                                              debt.language, inventory=inventory)
            except (ValueError, OSError):
                continue
            if baseline is None:
                scope = packets._read_scope(store, context["scope_id"])
                replacement = packets._subject_gap_item(context["scope_id"], scope, subject, debt.language, gap,
                                                        sense=focus["sense"] if focus else None)
                if replacement.id != debt.id:
                    items.append(replacement)
                    retired.append(debt.id)
                continue
            target = baseline._context["rows"][0]["target"]
            records = [record for record in current if record["source"] == subject["source"]
                       and record["target_language"] == ledger._language(debt.language)
                       and record["grounding"]["context"].get("fallback", {}).get("origin_scope_id") == context["scope_id"]
                       and (not focus or record["sense"] == focus["sense"])]
            choices = {}
            for record in records:
                try:
                    root, coverage = _validated_progress(conn, store, record, inventory)
                except (ValueError, OSError):
                    continue
                end = coverage[-1]["end"] if coverage else 0
                candidate = (end, record["id"], record, root, coverage)
                key = record["sense"]["id"]
                if key not in choices or candidate[:2] > choices[key][:2]:
                    choices[key] = candidate
            if not choices:
                old = context["gap"]
                if (old.get("target_page_source"), old.get("target_page_id"), old.get("target_page_sha256")) != (
                        target["source"], target["page_id"], target["sha256"]):
                    if focus:
                        translation = packets._item(debt.term, debt.language, [], "gate_failed",
                                                    dict(baseline._context, task="translation"), "Reestablish current-version source-sense evidence before scanning")
                        baseline = packets._focused_occurrence_item(translation, focus["source"], focus["sense"])
                    items.append(baseline)
                    scope = packets._read_scope(store, context["scope_id"])
                    if gap is None:
                        gap = dict(reason="unscanned_target_page_regions", story_key=subject["source"]["story_key"],
                                   language=debt.language, target_page_source=target["source"], target_page_id=target["page_id"],
                                   target_page_sha256=target["sha256"], unshown_target_regions=[dict(start=0, end=target["end"])])
                    items.append(packets._subject_gap_item(context["scope_id"], scope, subject, debt.language, gap,
                                                           sense=focus["sense"] if focus else None))
                    retired.append(debt.id)
                    for old_item in pending:
                        old_scan = old_item._context.get("scan")
                        old_focus = old_item._context.get("focus")
                        if (isinstance(old_scan, dict) and old_scan.get("origin_scope_id") == context["scope_id"]
                                and old_item._context.get("subject") == subject
                                and ledger._language(old_item.language) == ledger._language(debt.language)
                                and (not focus or old_focus == focus)
                                and (old_scan.get("target_page_source"), old_scan.get("target_page_id"), old_scan.get("target_page_sha256")) !=
                                (target["source"], target["page_id"], target["sha256"])):
                            retired.append(old_item.id)
                continue
            replacements = []
            for end, _, record, root, coverage in choices.values():
                next_item = _next_item(conn, store, record, inventory=inventory)
                length = baseline._context["fallback"]["target_page_code_points"]
                remaining = [dict(start=end, end=length)] if end < length else []
                scan_gap = dict(reason="unscanned_target_page_regions" if remaining else "subject_scan_terminal_review_pending",
                                story_key=record["story_key"], language=debt.language, target_page_source=target["source"],
                                target_page_id=target["page_id"], target_page_sha256=target["sha256"],
                                unshown_target_regions=remaining, reviewed_regions=coverage, scan_root_relation_id=root,
                                coverage_kind="agent_declared_content_review_not_omission")
                scope = packets._read_scope(store, context["scope_id"])
                replacement = packets._subject_gap_item(context["scope_id"], scope, subject, debt.language, scan_gap,
                                                        sense=record["sense"])
                replacements.append(replacement)
                if next_item is not None:
                    items.append(next_item)
            items.extend(replacements)
            if all(replacement.id != debt.id for replacement in replacements):
                retired.append(debt.id)
    result = ar.enqueue(store, items) if items else dict(added=0)
    _supersede(store, retired)
    result["retired"] = len(set(retired))
    return result
