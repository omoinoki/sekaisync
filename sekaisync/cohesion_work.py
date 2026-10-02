"""Converge duplicate focused review work, never manufacture semantic votes."""
import json
from pathlib import Path


_TABLE = "scraper_cohesion_supersessions"


def _has_ledger(conn):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (_TABLE,)).fetchone() is not None


def _same(left, right):
    from sekaisync import occurrence_store as occurrences
    return occurrences._json(left) == occurrences._json(right)


def _packet(item):
    return dict(id=item.id, term=item.term, language=item.language,
                candidates=item.candidates, review_context=item._context)


def _eligible(item):
    from sekaisync import agent_packets as packets
    context = item._context
    if (not isinstance(context, dict) or context.get("schema") != packets._SCHEMA or context.get("task") != "occurrence"
            or not context.get("focus")
            or any(key in context for key in ("expansion", "scan", "fallback", "gap"))):
        return False
    try:
        packets._validate_occurrence_focus(item)
        return item.id == "arp:" + packets._digest(
            [item.term, item.language, sorted(set(item.candidates)), context])[:32]
    except (KeyError, TypeError, ValueError):
        return False


def _completed_relation(relation, receipts):
    receipt = receipts.get(relation["review_item_id"])
    if not isinstance(receipt, dict):
        return False
    try:
        submitted = json.loads(receipt.get("value", ""))
    except (TypeError, ValueError):
        return False
    return (receipt.get("decision") in {"accept", "replace"}
            and receipt.get("scope_id") == relation["grounding"]["scope_id"]
            and isinstance(submitted, list) and relation["id"] in submitted)


def _direction_conflict(current, reference):
    from sekaisync import agent_packets as packets
    page_keys = ("page_source", "page_id", "page_sha256", "story_key")
    signatures = {packets._digest([record["kind"], record["target"]]) for record in current
                  if _same(record["source"], reference["source"]) and _same(record["sense"], reference["sense"])
                  and record["target_language"] == reference["target_language"]
                  and all(record["target"][key] == reference["target"][key] for key in page_keys)}
    return len(signatures) > 1


def _proof(conn, store, item, relations, receipts, conflict_records=None, receipt_relation_id=None):
    from sekaisync import agent_packets as packets, occurrence_store as occurrences
    if not _eligible(item):
        return None
    context, focus = item._context, item._context["focus"]
    matching = []
    for relation in relations:
        grounding = relation.get("grounding") or {}
        original = grounding.get("context") or {}
        if (not _same(relation["source"], focus["source"]) or not _same(relation["sense"], focus["sense"])
                or relation["target_language"] != occurrences._language(item.language)
                or grounding.get("scope_id") != context["scope_id"]
                or grounding.get("term") != item.term
                or not _same(original.get("subject"), context.get("subject"))
                or original.get("source_language") != context.get("source_language")
                or not _same(original.get("rows"), context["rows"])
                or any(key in original for key in ("expansion", "scan", "fallback", "gap"))):
            continue
        # Conflicting current observations must remain visible even when only
        # one of them has a completed receipt.
        matching.append(relation)
    signatures = {packets._digest([record["kind"], record["target"]]) for record in matching}
    if (len(signatures) != 1 or _direction_conflict(
            relations if conflict_records is None else conflict_records, matching[0])):
        return None
    for relation in sorted(matching, key=lambda record: record["id"]):
        if receipt_relation_id is not None and relation["id"] != receipt_relation_id:
            continue
        if not _completed_relation(relation, receipts) or relation["review_item_id"] == item.id:
            continue
        return dict(schema=1, item=_packet(item), receipt_item_id=relation["review_item_id"],
                    relation_id=relation["id"], observed_kind=relation["kind"],
                    semantic_resolved=relation["kind"] != "unresolved")
    return None


def _converge_items(conn, store, items, receipts=None):
    """Caller owns the writer lease and transaction; receipts are never altered."""
    from sekaisync import agent_packets as packets, occurrence_store as occurrences
    if not any(_eligible(item) for item in items):
        return list(items), []
    relations = occurrences._read_relations(conn)
    by_focus = {}
    for relation in relations:
        key = (relation["source"]["id"], relation["sense"]["id"], relation["target_language"])
        by_focus.setdefault(key, []).append(relation)
    receipts = packets._receipts(store, conn=conn) if receipts is None else receipts
    remaining, retired = [], []
    for item in items:
        if not _eligible(item):
            remaining.append(item)
            continue
        focus = item._context["focus"]
        key = (focus["source"]["id"], focus["sense"]["id"], occurrences._language(item.language))
        old = (conn.execute(f"SELECT 1 FROM {_TABLE} WHERE item_id=?", (item.id,)).fetchone()
               if _has_ledger(conn) else None)
        if old:
            if _is_superseded(conn, store, item.id):
                retired.append(item.id)
            else:
                remaining.append(item)
            continue
        proof = _proof(conn, store, item, by_focus.get(key, []), receipts)
        if proof is None:
            remaining.append(item)
            continue
        payload = occurrences._json(proof)
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                     "item_id TEXT PRIMARY KEY, receipt_item_id TEXT NOT NULL, "
                     "relation_id TEXT NOT NULL, payload_json TEXT NOT NULL)")
        conn.execute(f"INSERT OR IGNORE INTO {_TABLE} VALUES(?,?,?,?)",
                     (item.id, proof["receipt_item_id"], proof["relation_id"], payload))
        retired.append(item.id)
    return remaining, retired


def _authentic_successor(conn, store, original, current, receipts):
    """Only a real completed expansion can sustain an already saved alias."""
    from sekaisync import agent_packets as packets, occurrence_store as occurrences
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_relation_retirements'").fetchone():
        return False
    row = conn.execute("SELECT new_id,payload_json FROM scraper_relation_retirements WHERE old_id=?",
                       (original["id"],)).fetchone()
    if row is None:
        return False
    retirement = json.loads(row[1])
    occurrences._verify_identity(retirement, "ret:")
    if (set(retirement) != {"id", "schema", "old_id", "new_id", "reason"}
            or retirement["old_id"] != original["id"] or retirement["new_id"] != row[0]
            or not isinstance(retirement["reason"], str) or not retirement["reason"].strip()):
        return False
    successor = next((record for record in current if record["id"] == row[0]), None)
    if (successor is None or original["kind"] != "unresolved"
            or successor["kind"] not in {"lexical", "paraphrase", "reference", "omitted"}
            or not _same(successor["source"], original["source"]) or not _same(successor["sense"], original["sense"])
            or successor["target_language"] != original["target_language"]
            or any(successor["target"][key] != original["target"][key]
                   for key in ("page_source", "page_id", "page_sha256", "story_key"))):
        return False
    if _direction_conflict(current, successor):
        return False
    if successor["kind"] == "omitted":
        expansion = successor["grounding"]["context"].get("expansion") or {}
        if expansion.get("level") != "full_page" or expansion.get("target_complete_page") is not True:
            return False
    # Expansion commits retire all unresolved ancestors directly to the final
    # relation. Reproduce every intervening packet, not just its claimed IDs.
    record, seen = successor, set()
    while record["id"] != original["id"]:
        if record["id"] in seen or not _completed_relation(record, receipts):
            return False
        seen.add(record["id"])
        context = record["grounding"]["context"]
        expansion = context.get("expansion")
        if not expansion:
            return False
        parent_row = conn.execute("SELECT payload_json FROM scraper_relations WHERE id=?",
                                  (expansion["parent_relation_id"],)).fetchone()
        if parent_row is None:
            return False
        parent = json.loads(parent_row[0])
        expected = packets._occurrence_expansion_item(conn, store, parent, persist=False)
        if (expected is None or expected.id != record["review_item_id"] or not _same(expected._context, context)
                or not _same(parent["source"], original["source"]) or not _same(parent["sense"], original["sense"])):
            return False
        record = parent
    return _completed_relation(original, receipts)


def _is_superseded(conn, store, identity):
    """Read-only replay recognition, revalidated against current raw evidence."""
    from sekaisync import agent_review as ar, agent_packets as packets, occurrence_store as occurrences
    if not _has_ledger(conn):
        return False
    row = conn.execute(f"SELECT receipt_item_id,relation_id,payload_json FROM {_TABLE} WHERE item_id=?",
                       (identity,)).fetchone()
    if row is None:
        return False
    try:
        saved = json.loads(row[2])
        item = ar.ReviewItem.from_dict(saved["item"])
        if item.id != identity:
            return False
        relations = occurrences._read_relations(conn, source_id=item._context["focus"]["source"]["id"],
                                                 sense_id=item._context["focus"]["sense"]["id"])
        receipts = packets._receipts(store, conn=conn)
        proof = _proof(conn, store, item, relations, receipts, receipt_relation_id=saved["relation_id"])
        if proof is None and saved["relation_id"] not in {record["id"] for record in relations}:
            historical = conn.execute("SELECT payload_json FROM scraper_relations WHERE id=?",
                                      (saved["relation_id"],)).fetchone()
            if historical:
                historical = json.loads(historical[0])
                occurrences._validate_relation(conn, historical)
                if _authentic_successor(conn, store, historical, relations, receipts):
                    proof = _proof(conn, store, item, relations + [historical], receipts, conflict_records=relations,
                                   receipt_relation_id=saved["relation_id"])
        return (proof is not None and occurrences._json(proof) == row[2] and row[0] == saved["receipt_item_id"]
                and row[1] == saved["relation_id"])
    except (KeyError, TypeError, ValueError, OSError):
        return False


def _converge_sqlite(conn, store):
    from sekaisync import agent_review as ar
    items = [ar._queue_row_to_item(row) for row in conn.execute(
        f"SELECT {ar._QUEUE_READ_COLUMNS} FROM review_queue WHERE status='queued' ORDER BY rowid")]
    _, retired = _converge_items(conn, store, items)
    return sum(conn.execute("UPDATE review_queue SET status='superseded' WHERE item_id=? AND status='queued'",
                            (identity,)).rowcount for identity in retired)


def converge(store):
    """Repair a ledger-before-queue crash through the existing export workflow."""
    from sekaisync import agent_review as ar, dbstore
    from sekaisync.fetcher import store_writer_lock
    if not (Path(store) / "kb" / "sekaisync.db").exists():
        return dict(retired=0)
    with store_writer_lock(store):
        with dbstore.connect(store) as conn:
            conn.execute("BEGIN IMMEDIATE")
            if ar._uses_sqlite(store):
                retired = _converge_sqlite(conn, store)
                if retired:
                    dbstore.bump_revision(conn)
                conn.commit()
            else:
                remaining, retired_ids = _converge_items(conn, store, ar._read_queue(store))
                retired = len(retired_ids)
                if retired:
                    dbstore.bump_revision(conn)
                # Proof commits before a JSON drain so interrupted drains are
                # recoverable without a fabricated judgment or lost ancestry.
                conn.commit()
                if retired:
                    ar._write_queue(store, remaining)
    return dict(retired=retired)
