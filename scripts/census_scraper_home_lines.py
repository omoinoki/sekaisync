"""Sealed, composite-identity home-voice inventory with independent phrase-field debts."""
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.census_scraper_area_talk import REGIONS, _language

IDENTITY_FIELDS = ("id", "groupId", "gameCharacterId", "characterArchiveVoiceType", "externalId")
TEXT_FIELDS = ("displayPhrase", "displayPhrase2")
PAGE_ID = re.compile(r"^web:([^:]+):([^:]+):home_line:([1-9][0-9]*)$")


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def raw_identity(record: dict) -> dict:
    return {field: dict(present=field in record, value=record.get(field)) for field in IDENTITY_FIELDS}


def composite_key(record: dict, *, include_id=True) -> str:
    identity = raw_identity(record)
    if not include_id:
        identity.pop("id")
    return json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def identity_errors(record: dict) -> list[str]:
    errors = ["invalid_" + field for field in ("id", "groupId", "gameCharacterId")
              if type(record.get(field)) is not int or record[field] < 1]
    if not isinstance(record.get("characterArchiveVoiceType"), str) or not record["characterArchiveVoiceType"].strip():
        errors.append("invalid_characterArchiveVoiceType")
    if "externalId" in record and (type(record["externalId"]) is not int or record["externalId"] < 1):
        errors.append("invalid_externalId")
    if any(field in record and record[field] is not None and not isinstance(record[field], str) for field in TEXT_FIELDS):
        errors.append("nonstring_phrase_field")
    return errors


def expected_fields(record: dict) -> list[dict]:
    result = []
    for field in TEXT_FIELDS:
        value = record.get(field)
        usable_type = value is None or isinstance(value, str)
        rendered = (value or "").strip() if usable_type else None
        result.append(dict(field=field, raw_field_present=field in record, raw_value=value,
            rendered_text=rendered, nonempty_expected=bool(rendered), valid_field_type=usable_type))
    return result


def rendered_text(record: dict, *, field_labels: bool = False) -> str:
    fields = expected_fields(record)
    if any(not row["valid_field_type"] for row in fields):
        raise ValueError("Phrase fields must be strings or absent/null")
    return "\n".join((row["field"] + ": " if field_labels else "") + row["rendered_text"]
                     for row in fields if row["nonempty_expected"])


def field_coverage(record: dict, text: str) -> list[dict]:
    result, cursor = [], 0
    for field in expected_fields(record):
        row = {key: value for key, value in field.items() if key not in {"raw_value", "rendered_text"}}
        if not field["valid_field_type"]:
            row.update(status="invalid_expected_field", span=None)
        elif not field["nonempty_expected"]:
            row.update(status="no_nonempty_field_obligation", span=None)
        else:
            value = field["rendered_text"]
            candidates = []
            for prefix, rendering in ((field["field"] + ": ", "field_labeled"), ("", "legacy_unlabeled")):
                fragment = prefix + value
                fragment_start = text.find(fragment, cursor)
                while fragment_start >= 0:
                    end = fragment_start + len(fragment)
                    if (fragment_start == 0 or text[fragment_start - 1] == "\n") and (end == len(text) or text[end] == "\n"):
                        candidates.append((fragment_start, fragment_start + len(prefix), end, rendering))
                        break
                    fragment_start = text.find(fragment, fragment_start + 1)
            if not candidates:
                row.update(status="expected_field_missing", span=None)
            else:
                _fragment_start, start, end, rendering = min(candidates)
                row.update(status="expected_field_present", span=dict(start=start, end=end), rendering=rendering,
                           structural_label_credited_as_content=False)
                cursor = end
        result.append(row)
    return result


def load_generation(generation: Path) -> tuple[dict, dict]:
    tables, sources = {}, {}
    for language, region in REGIONS.items():
        paths = list((generation / region / "source").glob("*/characterArchiveVoices.json"))
        if len(paths) != 1:
            raise ValueError("Exactly one sealed regional characterArchiveVoices table is required: " + region)
        raw = paths[0].read_bytes()
        rows = json.loads(raw)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("Voice table must contain a list of records")
        tables[language] = rows
        sources[language] = dict(region=region, path=str(paths[0].resolve()), sha256=sha(raw), records=len(rows))
    return tables, sources


def build_inventory(tables: dict) -> tuple[dict, dict, dict]:
    definitions, local_ids = defaultdict(dict), {}
    invalid, duplicate_ids, external_duplicates, id_drifts, signature_drifts = [], [], [], [], []
    all_ids, signatures = defaultdict(dict), defaultdict(dict)
    for language, rows in tables.items():
        ids, externals = defaultdict(list), defaultdict(list)
        for index, record in enumerate(rows):
            errors = identity_errors(record)
            if errors:
                invalid.append(dict(language=language, record_index=index, reasons=errors, record=record))
                continue
            ids[record["id"]].append(record)
            definitions[composite_key(record)].setdefault(language, []).append(record)
            signatures[composite_key(record, include_id=False)].setdefault(language, []).append(record)
            if "externalId" in record:
                externals[record["externalId"]].append(record)
        local_ids[language] = dict(ids)
        for identity, matches in ids.items():
            all_ids[identity][language] = matches
            if len(matches) != 1:
                duplicate_ids.append(dict(language=language, local_voice_id=identity,
                    composite_identities=[raw_identity(record) for record in matches], records=len(matches)))
        for external, matches in externals.items():
            if len(matches) > 1:
                external_duplicates.append(dict(language=language, external_id=external,
                    local_voice_ids=[record["id"] for record in matches],
                    composite_identity_count=len({composite_key(record) for record in matches}), records=len(matches)))
    for identity, by_language in all_ids.items():
        keys = {composite_key(record) for records in by_language.values() for record in records}
        if len(keys) > 1:
            id_drifts.append(dict(local_voice_id=identity, identities_by_language={language: [raw_identity(record) for record in records]
                for language, records in by_language.items()}, conclusion="Numeric voice ID is not a standalone cross-language content key"))
    for signature, by_language in signatures.items():
        ids = {record["id"] for records in by_language.values() for record in records}
        if len(ids) > 1 and len(by_language) > 1:
            signature_drifts.append(dict(signature=json.loads(signature), local_ids_by_language={language: [record["id"] for record in records]
                for language, records in by_language.items()}, automatic_merge=False))
    issues = dict(invalid_definitions=invalid, duplicate_region_local_ids=duplicate_ids,
        duplicate_external_ids=external_duplicates, same_id_composite_drifts=id_drifts,
        same_non_id_signature_different_local_ids=signature_drifts)
    return dict(definitions), local_ids, issues


def audit_page(page: dict, local_ids: dict) -> dict:
    language = _language(page.get("language"))
    match = PAGE_ID.fullmatch(str(page.get("id") or ""))
    reasons, voice_id, record = [], None, None
    if match is None:
        reasons.append("invalid_home_line_page_id")
    else:
        source, locale, numeric_id = match.groups()
        voice_id = int(numeric_id)
        if source != page.get("source"):
            reasons.append("page_id_source_mismatch")
        if _language(locale) != language:
            reasons.append("page_id_locale_mismatch")
    canonical = str(page.get("canonical_key") or "")
    if canonical:
        parts = canonical.split(":")
        if len(parts) != 3 or parts[0] != "home_line" or _language(parts[1]) != language or parts[2] != str(voice_id):
            reasons.append("canonical_key_identity_mismatch")
    matches = local_ids.get(language, {}).get(voice_id, [])
    if len(matches) == 1:
        record = matches[0]
    elif matches:
        reasons.append("ambiguous_region_local_voice_id")
    status = "invalid" if reasons else "foreign" if record is None else "expected"
    flags = {field: page.get(field) for field in ("asset_mismatch", "scenario_id_mismatch",
        "content_language_mismatch", "untranslated", "aux_flag", "auxiliary")}
    primary = not any(flags.values()) and page.get("trust") in {"A", "B", "C"}
    text = str(page.get("text") or "")
    coverage = field_coverage(record, text) if record is not None else []
    expected = rendered_text(record) if record is not None else None
    labeled_expected = rendered_text(record, field_labels=True) if record is not None else None
    full = bool(expected) and text in {expected, labeled_expected}
    return dict(source=page.get("source"), page_id=page.get("id"), language=language,
        local_voice_id=voice_id, identity_status=status, reasons=reasons,
        local_identity_basis="region_local_id_resolves_unique_sealed_composite" if status == "expected" else "unverified",
        raw_composite_identity=raw_identity(record) if record is not None else None,
        text_sha256=sha(text.encode()), stored_text_hash=page.get("text_hash"), source_hash=page.get("source_hash"),
        expected_full_text_sha256=sha(expected.encode()) if expected is not None else None,
        expected_labeled_text_sha256=sha(labeled_expected.encode()) if labeled_expected is not None else None,
        text_fingerprint_matches_expected=full,
        current_public_field_rendering_matches=bool(labeled_expected) and text == labeled_expected,
        primary_candidate=primary, flags=flags, characters=len(text), field_coverage=coverage,
        exact_full_field_rendering=full, complete_primary_text=(status == "expected" and primary and full))


def run(generation: Path, production: Path, out: Path) -> dict:
    generation, production, out = generation.resolve(), production.resolve(), out.resolve()
    if (out == production or out.is_relative_to(production) or production.is_relative_to(out)
            or out == generation or out.is_relative_to(generation) or generation.is_relative_to(out)):
        raise ValueError("Inventory output must not overlap production or the sealed generation")
    if out.exists() and any(out.iterdir()):
        raise ValueError("Home-line inventory output is frozen or nonempty")
    tables, sources = load_generation(generation)
    definitions, local_ids, issues = build_inventory(tables)
    observed, audits, corpus_digest = defaultdict(list), [], hashlib.sha256()
    db = production / "kb/sekaisync.db"
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for sqlite_row in conn.execute("SELECT source,id,language,kind,canonical_key,text,text_hash,source_hash,trust,"
                "asset_mismatch,scenario_id_mismatch,content_language_mismatch,untranslated,aux_flag,auxiliary "
                "FROM web_pages WHERE kind='home_line' ORDER BY source,id"):
            page = dict(sqlite_row)
            if _language(page["language"]) not in REGIONS:
                continue
            audit = audit_page(page, local_ids)
            audits.append(audit)
            corpus_digest.update((json.dumps(audit, sort_keys=True, ensure_ascii=False) + "\n").encode())
            if audit["identity_status"] == "expected":
                observed[audit["language"], audit["local_voice_id"]].append(audit)
        conn.rollback()
    expected, debts, field_counts, acquisition_counts = [], [], defaultdict(Counter), defaultdict(Counter)
    for key, by_language in sorted(definitions.items()):
        family = dict(composite_identity=json.loads(key), family_key_sha256=sha(key.encode()),
            cross_language_identity_basis="exact_id_group_character_type_external_presence_and_value_only",
            semantic_equivalence_or_release_proved=False, languages=sorted(by_language),
            all_five_region_definitions=len(by_language) == 5, expected_definitions={})
        for language, matches in sorted(by_language.items()):
            for record in matches:
                pages = observed[language, record["id"]] if len(local_ids[language][record["id"]]) == 1 else []
                primary = [page for page in pages if page["primary_candidate"]]
                fields = expected_fields(record)
                expected_body = rendered_text(record)
                counts = Counter(row["field"] for page in primary for row in page["field_coverage"] if row["status"] == "expected_field_present")
                obligations = [row["field"] for row in fields if row["nonempty_expected"]]
                if not obligations:
                    status = "no_nonempty_field_obligation"
                elif len(local_ids[language][record["id"]]) != 1:
                    status = "ambiguous_sealed_local_identity"
                elif any(page["complete_primary_text"] for page in pages):
                    status = "primary_full_fields_present"
                elif not pages:
                    status = "missing"
                elif not primary:
                    status = "bad_only"
                elif counts["displayPhrase"] and "displayPhrase2" in obligations and not counts["displayPhrase2"]:
                    status = "primary_first_phrase_only"
                else:
                    status = "primary_partial_or_different_text"
                definition = dict(region=REGIONS[language], raw_record=record, raw_file=sources[language],
                    fields=[dict(**field, primary_pages_with_ordered_field=counts[field["field"]]) for field in fields],
                    expected_full_text_sha256=sha(expected_body.encode()), expected_full_characters=len(expected_body),
                    observed_pages=len(pages), observed_primary_pages=len(primary), acquisition_status=status,
                    release=dict(status="unknown_release", display_start_at_raw=record.get("displayStartAt"),
                        note="Raw presence and displayStartAt are not general server-rollout or user-unlock proofs"))
                family["expected_definitions"].setdefault(language, []).append(definition)
                acquisition_counts[language][status] += 1
                for field in fields:
                    if field["nonempty_expected"]:
                        field_counts[language][field["field"] + ":expected_nonempty"] += 1
                        field_counts[language][field["field"] + (":primary_field_present" if counts[field["field"]] else ":primary_field_missing")] += 1
                if obligations and status != "primary_full_fields_present":
                    debts.append(dict(language=language, region=REGIONS[language], local_voice_id=record["id"],
                        family_key_sha256=family["family_key_sha256"], composite_identity=raw_identity(record),
                        acquisition_status=status, expected_nonempty_fields=obligations,
                        missing_primary_fields=[field for field in obligations if not counts[field]],
                        observed_page_ids=[page["page_id"] for page in pages], release_status="unknown_release"))
        expected.append(family)
    for source in sources.values():
        if sha(Path(source["path"]).read_bytes()) != source["sha256"]:
            raise ValueError("Sealed voice inventory changed during census")
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, rows in (("expected-home-voices", expected), ("home-voice-page-audits", audits), ("home-voice-acquisition-debts", debts)):
        path = out / (name + ".jsonl")
        with path.open("w", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        paths[name] = dict(path=str(path), sha256=sha(path.read_bytes()), rows=len(rows))
    issues_path = out / "home-voice-identity-issues.json"
    issues_path.write_text(json.dumps(issues, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    paths["identity-issues"] = dict(path=str(issues_path), sha256=sha(issues_path.read_bytes()))
    summary = dict(schema="sekaisync/p0-composite-home-voice-census@1", finished_at_utc=datetime.now(timezone.utc).isoformat(),
        generation=str(generation), production_store=str(production), production_store_mutated=False,
        database_open_mode="ro", query_only=True, raw_sources=sources, observed_pages=len(audits),
        observed_rows_sha256=corpus_digest.hexdigest(), raw_records_by_language={language: len(rows) for language, rows in tables.items()},
        composite_families=len(expected), all_five_exact_composite_families=sum(row["all_five_region_definitions"] for row in expected),
        identity_issue_counts={key: len(value) for key, value in issues.items()},
        observed_identity_counts=dict(Counter(row["identity_status"] for row in audits)),
        acquisition_counts={key: dict(value) for key, value in acquisition_counts.items()},
        field_coverage_counts={key: dict(value) for key, value in field_counts.items()}, acquisition_debts=len(debts),
        release_policy="unknown_release; raw displayStartAt is retained separately and never promoted",
        files=paths, script_sha256=sha(Path(__file__).read_bytes()))
    (out / "home-voice-inventory-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", type=Path, default=ROOT / "store/raw/generations/20260926T085102Z-9e7b8f0f")
    parser.add_argument("--production-store", type=Path, default=ROOT / "store")
    parser.add_argument("--out", type=Path, default=ROOT / "work/p0-exhaustive-20261001/home-voice-inventory-b-01")
    args = parser.parse_args()
    result = run(args.generation, args.production_store, args.out)
    print(json.dumps({key: value for key, value in result.items() if key not in {"raw_sources", "files"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
