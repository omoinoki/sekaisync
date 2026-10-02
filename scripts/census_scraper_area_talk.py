"""Independent scenario-first area-talk inventory from sealed actionSets."""
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

ROOT = Path(__file__).resolve().parents[1]
REGIONS = {"ja": "jp", "en": "en", "zh_hans": "cn", "zh_hant": "tc", "ko": "kr"}
ALIASES = {"jp": "ja", "ja-jp": "ja", "en-us": "en", "cn": "zh_hans", "zh-cn": "zh_hans",
           "tc": "zh_hant", "tw": "zh_hant", "zh_tw": "zh_hant", "zh-tw": "zh_hant", "kr": "ko", "ko-kr": "ko"}
PAGE_ID = re.compile(r"^web:([^:]+):([^:]+):area_talk:(.+)$")


def _language(value):
    value = str(value or "").lower()
    return ALIASES.get(value, value)


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _integer(value):
    return type(value) is int and value > 0


def _scenario(value):
    return isinstance(value, str) and bool(value) and value == value.strip() and ":" not in value and not any(c.isspace() or ord(c) < 32 for c in value)


def _load_table(path):
    raw = Path(path).read_bytes()
    value = json.loads(raw)
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError(f"sealed table must contain a list of records: {path}")
    return value, dict(path=str(Path(path).resolve()), sha256=hashlib.sha256(raw).hexdigest(), records=len(value))


def _load_generation(generation):
    tables, sources = {}, {}
    for language, region in REGIONS.items():
        paths = list((Path(generation) / region / "source").glob("*/actionSets.json"))
        if len(paths) != 1:
            raise ValueError(f"expected exactly one sealed actionSets table for {region}; found {len(paths)}")
        actions, action_source = _load_table(paths[0])
        conditions_path = paths[0].with_name("releaseConditions.json")
        if conditions_path.exists():
            conditions, condition_source = _load_table(conditions_path)
        else:
            conditions, condition_source = [], dict(path=str(conditions_path.resolve()), missing=True)
        tables[language] = dict(actions=actions, conditions=conditions)
        sources[language] = dict(region=region, actions=action_source, conditions=condition_source)
    return tables, sources


def _inventory(tables):
    definitions = defaultdict(dict)
    action_ids, conditions = {}, {}
    invalid, script_only = [], []
    for language, tables_for_language in tables.items():
        by_id, by_condition = defaultdict(list), defaultdict(list)
        for condition in tables_for_language["conditions"]:
            if _integer(condition.get("id")):
                by_condition[condition["id"]].append(condition)
        for index, row in enumerate(tables_for_language["actions"]):
            if not _integer(row.get("id")):
                invalid.append(dict(language=language, index=index, record=row, reason="invalid_action_set_id"))
                continue
            by_id[row["id"]].append(row)
            if row.get("scenarioId") in (None, ""):
                script_only.append(dict(language=language, index=index, record=row, reason="no_declared_scenario_asset"))
                continue
            if not _scenario(row["scenarioId"]):
                invalid.append(dict(language=language, index=index, record=row, reason="invalid_exact_scenario_id"))
                continue
            definitions[row["scenarioId"]].setdefault(language, []).append(row)
        action_ids[language], conditions[language] = dict(by_id), dict(by_condition)
    return dict(definitions), action_ids, conditions, invalid, script_only


def _id_difference_matrix(action_ids):
    result = []
    for identity in sorted(set().union(*(set(table) for table in action_ids.values()))):
        mappings = {language: [row.get("scenarioId") for row in table[identity]]
                    for language, table in action_ids.items() if identity in table}
        scenarios = {scenario for values in mappings.values() for scenario in values if scenario is not None}
        if len(scenarios) > 1:
            result.append(dict(action_set_id=identity, scenarios_by_language=mappings,
                               conclusion="Region-local numeric actionSetId is not a cross-language content key"))
    return result


def _audit_page(page, definitions):
    metadata = page.get("metadata", {})
    language = _language(page.get("language"))
    errors, scenario, action_id = [], None, None
    match = PAGE_ID.fullmatch(str(page.get("id") or ""))
    if match is None:
        errors.append("invalid_area_talk_page_id")
    else:
        source, locale, content = match.groups()
        if source != page.get("source"):
            errors.append("page_id_source_mismatch")
        if _language(locale) != language:
            errors.append("page_id_locale_mismatch")
        parts = content.split(":")
        if len(parts) == 1:
            scenario = content
        elif len(parts) == 2 and parts[0].isdigit() and int(parts[0]) > 0:
            action_id, scenario = int(parts[0]), parts[1]
        else:
            errors.append("invalid_action_scenario_compound")
    if language not in REGIONS:
        errors.append("unsupported_observed_language")
    if not _scenario(scenario):
        errors.append("invalid_exact_observed_scenario")
    if page.get("logical_key") != "area_talk:" + str(scenario):
        errors.append("frozen_logical_key_scenario_mismatch")
    canonical = metadata.get("canonical_key")
    if canonical:
        pieces = str(canonical).split(":")
        expected_content = str(action_id) + ":" + str(scenario) if action_id is not None else str(scenario)
        if len(pieces) < 3 or pieces[0] != "area_talk" or _language(pieces[1]) != language or ":".join(pieces[2:]) != expected_content:
            errors.append("canonical_key_identity_mismatch")
    rows = definitions.get(scenario, {}).get(language, [])
    if rows and action_id is not None and action_id not in {row["id"] for row in rows}:
        errors.append("region_local_action_set_scenario_mismatch")
    state = "invalid" if errors else "foreign" if not rows else "expected"
    return dict(source=page.get("source"), page_id=page.get("id"), language=language, scenario_id=scenario,
                observed_action_set_id=action_id, raw_local_action_set_ids=sorted({row["id"] for row in rows}),
                identity_status=state, reasons=errors or (["scenario_not_defined_in_this_region_snapshot"] if state == "foreign" else []),
                compound_identity="region_local_action_id_verified" if action_id is not None and state == "expected" else
                                  "scenario_only_page_no_embedded_action_id" if state == "expected" else "unverified",
                frozen_good=bool(page.get("good")), frozen_primary_good=bool(page.get("primary_good")),
                text_sha256=metadata.get("text_hash"), version_hash=page.get("version_hash"),
                frozen_bad_reasons=metadata.get("bad_reasons", []), frozen_nonprimary_flags=metadata.get("nonprimary_flags", []))


def _load_event_proofs(census, sources):
    path = Path(census) / "expected-event-chapters.jsonl"
    if not path.exists():
        return {}, dict(path=str(path.resolve()), missing=True)
    result = defaultdict(list)
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            row = json.loads(line)
            for raw_language, definitions in row.get("expected_definitions", {}).items():
                language = _language(raw_language)
                proof = next((value for key, value in row.get("release", {}).items() if _language(key) == language), {})
                for definition in definitions:
                    episode = definition.get("episode_record", {})
                    if _integer(episode.get("id")):
                        source = sources.get(language, {})
                        region_parent = Path(source.get("actions", {}).get("path", "")).parent
                        condition = source.get("conditions", {})
                        dependencies = [proof.get(field, {}) for field in ("event_file", "story_file", "condition_file")]
                        matches = (all(value.get("path") and Path(value["path"]).parent == region_parent for value in dependencies)
                                   and proof.get("condition_file", {}).get("sha256") == condition.get("sha256"))
                        result[(language, episode["id"])].append(dict(logical_key=row.get("logical_key"),
                              scenario_id=episode.get("scenarioId"), episode_record=episode, proof=proof,
                              source_matches_current_raw_region=matches))
    return dict(result), dict(path=str(path.resolve()), sha256=_hash(path))


def _unlock(language, action, action_ids, conditions, event_proofs, proof_hashes=None, chain=(), as_of_ms=None):
    identity = action["id"]
    if identity in chain:
        return dict(status="unknown_unlock", reasons=["cyclic_action_set_prerequisite"], action_chain=list(chain) + [identity])
    definitions = conditions.get(language, {}).get(action.get("releaseConditionId"), [])
    if len(definitions) != 1:
        return dict(status="unknown_unlock", reasons=["missing_or_ambiguous_release_condition"], release_condition_id=action.get("releaseConditionId"))
    condition = definitions[0]
    kind = condition.get("releaseConditionType")
    result = dict(status="unknown_unlock", reasons=[], condition_record=condition, user_unlock_required=kind != "none")
    if len(action_ids.get(language, {}).get(identity, [])) != 1:
        result["reasons"].append("ambiguous_region_local_action_set_id")
    elif kind == "none":
        result.update(status="no_unlock_prerequisite", user_unlock_required=False)
    elif kind == "action_set":
        prerequisite_id = condition.get("releaseConditionTypeId")
        prerequisites = action_ids.get(language, {}).get(prerequisite_id, []) if _integer(prerequisite_id) else []
        if len(prerequisites) != 1 or not _scenario(prerequisites[0].get("scenarioId")):
            result["reasons"].append("dangling_or_ambiguous_region_local_action_set_prerequisite")
        else:
            prerequisite = prerequisites[0]
            proof = _unlock(language, prerequisite, action_ids, conditions, event_proofs, proof_hashes, chain + (identity,), as_of_ms)
            result.update(prerequisite_action_set_id=prerequisite_id, prerequisite_scenario_id=prerequisite["scenarioId"], prerequisite=proof)
            if proof["status"] != "unknown_unlock":
                result["status"] = "action_prerequisite_definition_proven"
            else:
                result["reasons"].append("action_set_prerequisite_remains_unproven")
    elif kind == "event_story":
        prerequisite_id = condition.get("releaseConditionTypeId")
        candidates = event_proofs.get((language, prerequisite_id), []) if _integer(prerequisite_id) else []
        unique = {(entry["logical_key"], entry["scenario_id"]) for entry in candidates}
        if len(unique) != 1:
            result["reasons"].append("dangling_or_ambiguous_region_local_event_episode")
        else:
            entry = candidates[0]
            proof = entry["proof"]
            result.update(prerequisite_episode_id=prerequisite_id, prerequisite_scenario_id=entry["scenario_id"],
                          prerequisite_logical_key=entry["logical_key"], sealed_event_proof=proof)
            errors = []
            if proof.get("status") != "released_in_verified_masterdata" or proof.get("region") != REGIONS[language]:
                errors.append("event_prerequisite_schedule_unproven_or_not_yet_released")
            if proof.get("episode_record") != entry["episode_record"]:
                errors.append("sealed_event_episode_identity_mismatch")
            if not entry.get("source_matches_current_raw_region"):
                errors.append("sealed_event_dependency_generation_or_region_mismatch")
            if as_of_ms is None or not _integer(proof.get("start_at_ms")) or proof["start_at_ms"] > as_of_ms:
                errors.append("missing_or_future_event_prerequisite_schedule_date")
            if proof_hashes is not None:
                for field in ("event_file", "story_file", "condition_file"):
                    source = proof.get(field, {})
                    path, expected = source.get("path"), source.get("sha256")
                    if not path or not expected:
                        errors.append("missing_sealed_event_dependency")
                        continue
                    if path not in proof_hashes:
                        candidate = Path(path)
                        if not candidate.is_absolute():
                            candidate = ROOT / candidate
                        proof_hashes[path] = _hash(candidate) if candidate.exists() else None
                    if proof_hashes[path] != expected:
                        errors.append("sealed_event_dependency_hash_mismatch")
            result["reasons"].extend(sorted(set(errors)))
            if not errors:
                result["status"] = "event_prerequisite_schedule_proven"
    else:
        result["reasons"].append("unsupported_unlock_condition_type:" + str(kind))
    return result


def _archive(value, as_of_ms):
    if type(value) is not int or value <= 0:
        return dict(status="unknown_archive_time", raw_value=value)
    try:
        timestamp = datetime.fromtimestamp(value / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return dict(status="unknown_archive_time", raw_value=value, reason="invalid_archive_timestamp_range")
    return dict(status="archive_timestamp_elapsed" if value <= as_of_ms else "archive_timestamp_future",
                timestamp_ms=value, timestamp_utc=timestamp,
                authority_scope="archivePublishedAt_only_not_general_server_release")


def _write(path, value):
    with Path(path).open("x", encoding="utf-8") as output:
        output.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def run(generation, census, output, as_of=None):
    generation, census, output = map(Path, (generation, census, output))
    if output.exists():
        raise FileExistsError("area-talk inventory output is frozen; choose a new directory")
    summary_path = census / "summary.json"
    summary_raw = summary_path.read_bytes()
    census_summary = json.loads(summary_raw)
    as_of = as_of or census_summary["as_of_utc"]
    stamp = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("as-of cutoff requires an explicit timezone")
    if stamp != datetime.fromisoformat(census_summary["as_of_utc"].replace("Z", "+00:00")):
        raise ValueError("sealed event release proofs and observed census must use the same as-of cutoff")
    tables, sources = _load_generation(generation)
    definitions, action_ids, conditions, invalid, script_only = _inventory(tables)
    matrix = _id_difference_matrix(action_ids)
    index_path = census / "census-index.sqlite"
    before_index_hash = _hash(index_path)
    event_proofs, event_source = _load_event_proofs(census, sources)
    audits = []
    with closing(sqlite3.connect(index_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        for record in conn.execute("SELECT * FROM pages WHERE kind='area_talk' ORDER BY source,id"):
            page = dict(record)
            page["metadata"] = json.loads(page.pop("metadata_json"))
            audits.append(_audit_page(page, definitions))
    acquired = defaultdict(list)
    for audit in audits:
        if audit["identity_status"] == "expected":
            acquired[(audit["scenario_id"], audit["language"])].append(audit)
    expected, missing = [], []
    proof_hashes = {}
    as_of_ms = int(stamp.timestamp() * 1000)
    for scenario in sorted(definitions):
        languages = definitions[scenario]
        states, versions = {}, {}
        for language in REGIONS:
            rows = languages.get(language, [])
            pages = acquired.get((scenario, language), [])
            state = ("not_in_region_masterdata" if not rows else "missing" if not pages else
                     "primary_good_present" if any(page["frozen_primary_good"] for page in pages) else
                     "nonprimary_good_only" if any(page["frozen_good"] for page in pages) else "bad_only")
            states[language] = state
            if not rows:
                continue
            versions[language] = dict(region=REGIONS[language], local_action_set_ids=sorted({row["id"] for row in rows}),
                                      definitions=[dict(record=row, unlock=_unlock(language, row, action_ids, conditions, event_proofs, proof_hashes, as_of_ms=as_of_ms),
                                                   archive_publication=_archive(row.get("archivePublishedAt"), as_of_ms)) for row in rows],
                                      observed_pages=pages, release_status="unknown_release",
                                      release_reasons=["raw_definition_and_archive_time_do_not_prove_general_server_release"],
                                      observed_readable_status="frozen_primary_body_present" if state == "primary_good_present" else "not_proven",
                                      inventory_authority="hashed_region_masterdata", observed_body_authority="frozen_local_community_corpus_not_live_server_audit")
            if state != "primary_good_present":
                missing.append(dict(scenario_id=scenario, language=language, local_action_set_ids=versions[language]["local_action_set_ids"],
                                    acquisition_state=state, release_status="unknown_release"))
        expected.append(dict(logical_key="area_talk:" + scenario, scenario_id=scenario,
                             expected_inventory_languages=list(languages), versions=versions, acquisition_states=states,
                             all_five_region_definitions_present=len(languages) == 5,
                             all_five_expected_primary_complete=len(languages) == 5 and all(value == "primary_good_present" for value in states.values()),
                             release_status="unknown_release"))
    probe = {language: dict(region=REGIONS[language], rows=len(table["actions"]),
                            declared_scenario_rows=sum(language in rows for rows in definitions.values()),
                            fields=dict(Counter(field for row in table["actions"] for field in row)),
                            condition_types=dict(Counter(row.get("releaseConditionType", "<missing>") for row in table["conditions"])))
             for language, table in tables.items()}
    report = dict(schema="sekaisync/expected-area-talk-inventory@1", as_of_utc=stamp.astimezone(timezone.utc).isoformat(),
                  generation=str(generation.resolve()), expected_scenarios=len(expected),
                  raw_defined_by_language={language: sum(language in rows for rows in definitions.values()) for language in REGIONS},
                  all_five_region_defined_scenarios=sum(row["all_five_region_definitions_present"] for row in expected),
                  all_five_expected_primary_complete=sum(row["all_five_expected_primary_complete"] for row in expected),
                  region_local_action_id_scenario_conflicts=len(matrix), observed_pages=len(audits),
                  observed_identity_counts=dict(Counter(row["identity_status"] for row in audits)),
                  acquisition_debt_counts=dict(Counter(row["acquisition_state"] for row in missing)),
                  invalid_raw_definitions=len(invalid), script_only_action_sets=len(script_only),
                  expected_derived_from="sealed actionSets.scenarioId only; observed pages never create expected entries",
                  page_identity_scope="exact frozen page id/canonical/logical scenario identity; numeric actionSetId verified only when explicitly embedded",
                  release_status_policy="unknown for all area talks; unlock prerequisite proofs and archive timestamps remain separately scoped evidence",
                  source_read_only=True, production_modified=False, original_census_modified=False,
                  limitations=["Local readable bodies and unconditional unlock conditions do not prove live server rollout.",
                               "Missing is measured against raw region definitions, not against existing observed counts.",
                               "Event prerequisite evidence is reused from the frozen event release audit; it never upgrades area-talk release status."])
    provenance = dict(raw_sources=sources, frozen_census=dict(path=str(index_path.resolve()), sha256=before_index_hash,
                      summary_path=str(summary_path.resolve()), summary_sha256=hashlib.sha256(summary_raw).hexdigest()),
                      sealed_event_proof_source=event_source, script_sha256=_hash(Path(__file__)))
    if _hash(index_path) != before_index_hash or summary_path.read_bytes() != summary_raw:
        raise ValueError("frozen observed inputs changed during inventory construction")
    for evidence in sources.values():
        for source in evidence.values():
            if isinstance(source, dict) and source.get("sha256") and _hash(source["path"]) != source["sha256"]:
                raise ValueError("sealed raw table changed during inventory construction")
    output.mkdir(parents=True)
    for filename, rows in (("expected-area-talks.jsonl", expected), ("area-talk-page-identity-audit.jsonl", audits),
                           ("area-talk-acquisition-debts.jsonl", missing)):
        with (output / filename).open("x", encoding="utf-8") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    for filename, value in (("area-talk-inventory-summary.json", report), ("area-talk-inventory-sources.json", provenance),
                            ("area-talk-field-probe.json", probe), ("area-talk-action-id-scenario-matrix.json", matrix),
                            ("area-talk-invalid-raw-definitions.json", invalid), ("area-talk-script-only-definitions.json", script_only)):
        _write(output / filename, value)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", type=Path, required=True)
    parser.add_argument("--frozen-census", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--as-of")
    args = parser.parse_args()
    print(json.dumps(run(args.generation, args.frozen_census, args.output_dir, args.as_of), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
