"""Read-only EN/KR narrative script census; absence of a script is not a language verdict."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sekaisync.line_alignment import strip_speaker_label
from sekaisync.termindex import TERM_STORY_KINDS
from sekaisync.webindex import text_matches_language

SCRIPTS = ("latin", "han", "kana", "hangul", "other_letters")
LANGUAGES = ("en", "ko")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def script_profile(text: str) -> dict:
    counts = Counter({key: 0 for key in (*SCRIPTS, "digits")})
    for char in text:
        if char.isdecimal():
            counts["digits"] += 1
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "LATIN" in name:
            key = "latin"
        elif "CJK UNIFIED IDEOGRAPH" in name or "CJK COMPATIBILITY IDEOGRAPH" in name:
            key = "han"
        elif "HIRAGANA" in name or "KATAKANA" in name:
            key = "kana"
        elif "HANGUL" in name:
            key = "hangul"
        else:
            key = "other_letters"
        counts[key] += 1
    active = [key for key in SCRIPTS if counts[key]]
    if not text.strip():
        category = "empty"
    elif not active:
        category = "numeric_only" if counts["digits"] else "no_letters_or_numbers"
    elif len(active) == 1:
        category = active[0] + "_only"
    else:
        category = "mixed_scripts"
    return dict(category=category, counts=dict(counts), present_scripts=active,
        characters=len(text), letters=sum(counts[key] for key in SCRIPTS))


def body_lines(text: str) -> list[dict]:
    result, offset = [], 0
    for index, raw_line in enumerate(text.splitlines(keepends=True)):
        line = raw_line.rstrip("\r\n")
        body = strip_speaker_label(line)
        start = offset + len(line) - len(body)
        result.append(dict(line=index + 1, start=start, end=offset + len(line),
            speaker_removed=body != line, text=body, profile=script_profile(body)))
        offset += len(raw_line)
    return result


def flag_state(page: dict) -> dict:
    flags = {key: page[key] for key in ("asset_mismatch", "scenario_id_mismatch",
        "content_language_mismatch", "untranslated", "aux_flag", "auxiliary")}
    flags["trust"] = page["trust"]
    return dict(flags=flags, unflagged_primary_candidate=(not any(value for key, value in flags.items()
        if key != "trust") and page["trust"] in {"A", "B", "C"}))


def _json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def legacy_en_ko_matches(language: str, text: str) -> bool:
    if language not in LANGUAGES:
        raise ValueError("Policy comparison is scoped to EN/KR")
    han = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", text))
    local = len(re.findall(r"[A-Za-z]" if language == "en" else r"[\uac00-\ud7af]", text))
    return han == 0 or local >= max(5, int(han * 0.3))


def compare_policy(store: Path, sealed_audit: Path, out: Path) -> dict:
    store, sealed_audit, out = store.resolve(), sealed_audit.resolve(), out.resolve()
    if (out == store or out.is_relative_to(store) or store.is_relative_to(out)
            or out == sealed_audit or out.is_relative_to(sealed_audit) or sealed_audit.is_relative_to(out)):
        raise ValueError("Policy output must not overlap production or the frozen script audit")
    if out.exists() and any(out.iterdir()):
        raise ValueError("Policy output is frozen or nonempty; use another explicit directory")
    original_summary_path = sealed_audit / "language-script-summary.json"
    summary_raw = original_summary_path.read_bytes()
    original_summary = json.loads(summary_raw)
    audit_evidence = original_summary["files"]["page_audit"]
    original_rows_path = Path(audit_evidence["path"])
    if sha(original_rows_path.read_bytes()) != audit_evidence["sha256"]:
        raise ValueError("Frozen script census row audit changed")
    originals = {}
    with original_rows_path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            key = row["source"], row["page_id"]
            if key in originals:
                raise ValueError("Duplicate frozen script census identity")
            originals[key] = row
    policy_path = ROOT / "sekaisync/webindex.py"
    policy_source_hash = sha(policy_path.read_bytes())
    counters, transitions, seen = defaultdict(Counter), [], set()
    kinds = sorted(TERM_STORY_KINDS)
    query = ("SELECT source,id,language,kind,text,text_hash,trust,asset_mismatch,scenario_id_mismatch,"
             "content_language_mismatch,untranslated,aux_flag,auxiliary FROM web_pages "
             "WHERE language IN (?,?) AND kind IN (" + ",".join("?" for _ in kinds) + ") ORDER BY source,id")
    with closing(sqlite3.connect((store / "kb/sekaisync.db").as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for sqlite_row in conn.execute(query, (*LANGUAGES, *kinds)):
            page = dict(sqlite_row)
            key = page["source"], page["id"]
            original = originals.get(key)
            state = flag_state(page)
            if (original is None or original["text_sha256"] != sha(page["text"].encode())
                    or original["language"] != page["language"] or original["kind"] != page["kind"]
                    or original["flags"] != state["flags"]):
                raise ValueError("Production corpus changed since the sealed script audit")
            seen.add(key)
            legacy = legacy_en_ko_matches(page["language"], page["text"])
            current = text_matches_language(page["language"], page["text"])
            transition = ("unchanged_pass" if current else "newly_rejected") if legacy else (
                "newly_allowed" if current else "unchanged_reject")
            for scope in ("all", "unflagged_primary_candidate"):
                if scope == "all" or state["unflagged_primary_candidate"]:
                    counters[page["language"] + ":" + scope][transition] += 1
            if legacy != current:
                transitions.append(dict(source=page["source"], page_id=page["id"], language=page["language"],
                    kind=page["kind"], text_sha256=original["text_sha256"], body_profile=original["body_profile"],
                    legacy_policy_matches=legacy, current_policy_matches=current, transition=transition, **state))
        conn.rollback()
    if set(originals) != seen or sha(policy_path.read_bytes()) != policy_source_hash:
        raise ValueError("Frozen corpus row set or current policy source changed during comparison")
    out.mkdir(parents=True, exist_ok=True)
    transition_path = out / "policy-transitions.json"
    _json(transition_path, dict(schema="sekaisync/p0-language-policy-transitions@1", transitions=transitions))
    result = dict(schema="sekaisync/p0-language-policy-comparison@1", production_store=str(store),
        production_store_mutated=False, database_open_mode="ro", query_only=True,
        frozen_script_audit=dict(path=str(original_summary_path), sha256=sha(summary_raw),
            page_audit_sha256=audit_evidence["sha256"]), audited_rows=len(seen),
        legacy_policy="EN/KR raw script branches before the October 1 fail-fast correction",
        current_policy=dict(path=str(policy_path), sha256=policy_source_hash),
        transition_counts={key: dict(value) for key, value in sorted(counters.items())},
        changed_policy_rows=len(transitions), transitions=dict(path=str(transition_path), sha256=sha(transition_path.read_bytes())),
        judgment_boundary="Policy acceptance changes are not translation-error labels, semantic quality gains or release proofs.",
        script_sha256=sha(Path(__file__).read_bytes()))
    _json(out / "language-policy-summary.json", result)
    return result


def run(store: Path, out: Path, max_review_pages: int = 20) -> dict:
    if not 0 <= max_review_pages <= 20:
        raise ValueError("Pure-Latin Korean full-page review is capped at 20 distinct bodies")
    store, out = store.resolve(), out.resolve()
    if out == store or out.is_relative_to(store) or store.is_relative_to(out):
        raise ValueError("Audit output must not overlap the read-only production store")
    if out.exists() and any(out.iterdir()):
        raise ValueError("Audit output is frozen or nonempty; use another explicit directory")
    db = store / "kb/sekaisync.db"
    if not db.exists():
        raise FileNotFoundError(db)
    started = datetime.now(timezone.utc).isoformat()
    out.mkdir(parents=True, exist_ok=True)
    page_counters, line_counters = defaultdict(Counter), defaultdict(Counter)
    presence_counters, raw_page_counters, groups = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    removal_counters, flags_counters = Counter(), defaultdict(Counter)
    latin_ko, en_foreign = {}, {}
    row_digest, records = hashlib.sha256(), 0
    kinds = sorted(TERM_STORY_KINDS)
    query = ("SELECT source,id,url,kind,language,text,text_hash,trust,asset_mismatch,scenario_id_mismatch,"
             "content_language_mismatch,untranslated,aux_flag,auxiliary FROM web_pages "
             "WHERE language IN (?,?) AND kind IN (" + ",".join("?" for _ in kinds) + ") ORDER BY source,id")
    audit_path = out / "page-script-audit.jsonl"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn, audit_path.open("w", encoding="utf-8") as audit:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for sqlite_row in conn.execute(query, (*LANGUAGES, *kinds)):
            page = dict(sqlite_row)
            text = page["text"]
            lines = body_lines(text)
            body = "\n".join(line["text"] for line in lines)
            raw_profile, profile = script_profile(text), script_profile(body)
            state = flag_state(page)
            language = page["language"]
            row = dict(source=page["source"], page_id=page["id"], url=page["url"], kind=page["kind"],
                language=language, text_sha256=sha(text.encode()), stored_text_hash=page["text_hash"],
                body_sha256=sha(body.encode()), raw_profile=raw_profile, body_profile=profile,
                body_lines=len(lines), speaker_labels_removed=sum(line["speaker_removed"] for line in lines), **state)
            serialized = json.dumps(row, ensure_ascii=False, sort_keys=True)
            audit.write(serialized + "\n")
            row_digest.update((serialized + "\n").encode())
            records += 1
            for scope in ("all", "unflagged_primary_candidate"):
                if scope != "all" and not state["unflagged_primary_candidate"]:
                    continue
                key = language + ":" + scope
                page_counters[key][profile["category"]] += 1
                raw_page_counters[key][raw_profile["category"]] += 1
                presence_counters[key].update(profile["present_scripts"])
                for line in lines:
                    line_counters[key][line["profile"]["category"]] += 1
            groups[language + ":" + page["source"] + ":" + page["kind"]][profile["category"]] += 1
            removal_counters[language] += row["speaker_labels_removed"]
            for flag, value in state["flags"].items():
                if flag != "trust" and value:
                    flags_counters[language][flag] += 1
            if language == "ko" and profile["category"] == "latin_only":
                key = row["body_sha256"]
                if key in latin_ko:
                    latin_ko[key]["equivalent_pages"].append(dict(source=page["source"], page_id=page["id"]))
                else:
                    latin_ko[key] = dict(**row, raw_text=text, body_text=body,
                        body_line_records=lines, equivalent_pages=[], semantic_review="pending_host_agent_review",
                        selection_basis="complete Korean page has Latin letters and no other letter script after speaker removal")
            if language == "en" and profile["counts"]["hangul"] > 0 and not profile["counts"]["latin"]:
                key = row["body_sha256"]
                if key not in en_foreign:
                    en_foreign[key] = dict(**row, example_body_prefix=body[:400],
                        verdict="script_anomaly_not_automatic_translation_error")
        conn.rollback()
    candidates = sorted(latin_ko.values(), key=lambda row: (-len(row["body_text"]), row["source"], row["page_id"]))
    selected = candidates[:max_review_pages]
    review_path = out / "kr-latin-only-review-packets.json"
    _json(review_path, dict(schema="sekaisync/p0-script-review-packets@1", full_body_cap=20,
        selected_distinct_bodies=len(selected), all_distinct_ko_latin_only_bodies=len(candidates), packets=selected))
    anomalies_path = out / "en-hangul-without-latin-script-anomalies.json"
    _json(anomalies_path, dict(distinct_bodies=len(en_foreign), pages=list(en_foreign.values()),
        note="Presence/absence metrics are not a language or translation judgment."))
    summary = dict(schema="sekaisync/p0-narrative-language-script-audit@1", started_at_utc=started,
        finished_at_utc=datetime.now(timezone.utc).isoformat(), production_store=str(store),
        database_path=str(db), database_open_mode="ro", query_only=True, production_store_mutated=False,
        snapshot_basis="one explicit read transaction; row identities and content hashes retained, no database clone or writes",
        story_kinds=kinds, languages=list(LANGUAGES), source_rows=records, audited_rows_sha256=row_digest.hexdigest(),
        body_category_counts={key: dict(value) for key, value in sorted(page_counters.items())},
        raw_category_counts={key: dict(value) for key, value in sorted(raw_page_counters.items())},
        body_line_category_counts={key: dict(value) for key, value in sorted(line_counters.items())},
        body_script_presence_counts={key: dict(value) for key, value in sorted(presence_counters.items())},
        source_kind_category_counts={key: dict(value) for key, value in sorted(groups.items())},
        removed_speaker_labels=dict(removal_counters), persisted_flag_counts={key: dict(value) for key, value in flags_counters.items()},
        ko_latin_only_distinct_bodies=len(candidates), ko_latin_only_full_bodies_exported=len(selected),
        ko_latin_only_review_selection="longest-first deterministic distinct bodies, at most 20; no semantic selection",
        en_hangul_without_latin_distinct_bodies=len(en_foreign),
        judgment_boundary="Script presence is descriptive only. No automatic wrong-language, untranslated or usable classification.",
        files={"page_audit": dict(path=str(audit_path), sha256=sha(audit_path.read_bytes())),
               "ko_review_packets": dict(path=str(review_path), sha256=sha(review_path.read_bytes())),
               "en_script_anomalies": dict(path=str(anomalies_path), sha256=sha(anomalies_path.read_bytes()))},
        script_sha256=sha(Path(__file__).read_bytes()))
    _json(out / "language-script-summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--store", type=Path, default=ROOT / "store")
    parser.add_argument("--out", type=Path, default=ROOT / "work/p0-exhaustive-20261001/language-script-audit-b-01")
    parser.add_argument("--max-review-pages", type=int, default=20)
    parser.add_argument("--compare-from-audit", type=Path)
    args = parser.parse_args()
    summary = compare_policy(args.store, args.compare_from_audit, args.out) if args.compare_from_audit else run(args.store, args.out, args.max_review_pages)
    print(json.dumps({key: value for key, value in summary.items() if key not in {"source_kind_category_counts", "body_line_category_counts"}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
