"""Full regional MySEKAI definition, acquisition, and structural identity census.

Local snapshot evidence only: no network, release claims, semantic gold, or
production writes. Tutorial assets keep a distinct supplemental namespace.
"""
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
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _code_hashes(root: Path = ROOT) -> dict[str, str]:
    root = root.resolve()
    paths = [root / "scripts/census_scraper_mysekai.py", *sorted((root / "sekaisync").rglob("*.py"))]
    return {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


_IMPORT_CODE_HASHES = _code_hashes()

from sekaisync import mysekai_moly, termindex

if _code_hashes() != _IMPORT_CODE_HASHES:
    raise ValueError("Census code closure changed during product imports")

REGIONS = {"ja": "jp", "en": "en", "zh_hans": "cn", "zh_tw": "tc", "ko": "kr"}
SOURCE_LOCALES = {"ja-jp": "ja", "en-us": "en", "zh-cn": "zh_hans", "zh-tw": "zh_tw", "ko-kr": "ko"}
TWEETS = "mysekaiCharacterTalkTweets"
TALKS = "mysekaiCharacterTalks"
TUTORIALS = "mysekaiTutorialTalks"
FIXTURES = "mysekaiCharacterTalkFixtureCommonTweetGroups"
PREACTIONS = "mysekaiCharacterTalkPreActions"
WITHOUT_TALKS = "mysekaiCharacterTalkTweetWithoutRelatedTalks"
TABLES = (TWEETS, TALKS, FIXTURES, PREACTIONS, WITHOUT_TALKS, TUTORIALS)
EDGE_TABLES = (FIXTURES, PREACTIONS, WITHOUT_TALKS, TUTORIALS)
DOMAINS = {TWEETS: "mysekai_tweet", TALKS: "mysekai_talk", TUTORIALS: "mysekai_tutorial_asset"}
PAGE_ID = re.compile(r"^web:([^:]+):([^:]+):(mysekai_tweet|mysekai_talk):([1-9][0-9]*)$")
ASSET = re.compile(r"^[A-Za-z0-9_/-]+$")
FLAGS = ("asset_mismatch", "scenario_id_mismatch", "content_language_mismatch", "untranslated", "aux_flag", "auxiliary")
MOLY_DETAIL = re.compile(r"/moly/catalog-store/([a-f0-9]{64})\.json\Z")
MOLY_BINDING_FIELDS = (
    "schema", "region", "snapshot_id", "snapshot_version", "snapshot_provenance", "catalog_sha256",
    "key", "detail_path", "detail_sha256", "raw_talk", "raw_preaction", "raw_preview_tweet",
    "raw_version", "version_debts", "current_body_freshness", "release_status",
)
BODY_AVAILABLE = {"primary_current_raw_body", "primary_usable_path_bound_freshness_unknown",
                  "primary_usable_snapshot_body_provenance_bound"}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def language(value) -> str:
    value = str(value or "").lower()
    return next((lang for lang, region in REGIONS.items() if value == region),
                SOURCE_LOCALES.get(value, termindex._term_language(value)))


def positive_int(value) -> bool:
    return type(value) is int and value > 0


def asset_valid(value) -> bool:
    return (isinstance(value, str) and bool(ASSET.fullmatch(value))
            and all(part not in {"", ".", ".."} for part in value.split("/")))


def definition_errors(table: str, record: dict) -> list[str]:
    errors = [] if positive_int(record.get("id")) else ["invalid_id"]
    if table == TWEETS and not isinstance(record.get("text"), str):
        errors.append("invalid_text")
    if table in {TALKS, TUTORIALS}:
        errors.extend("invalid_" + field for field in ("assetbundleName", "lua") if not asset_valid(record.get(field)))
    return errors


def asset_path(record: dict) -> str:
    return record["assetbundleName"] + "/" + record["lua"] + ".lua.txt"


def raw_identity(table: str, record: dict) -> dict:
    fields = ("id", "assetbundleName", "lua") if table in {TALKS, TUTORIALS} else ("id",)
    return {field: {"present": field in record, "value": record.get(field)} for field in fields}


def load_generation(generation: Path) -> tuple[dict, dict]:
    tables, sources = {}, {}
    for lang, region in REGIONS.items():
        bases = [path for path in (generation / region / "source").iterdir() if path.is_dir()]
        if len(bases) != 1:
            raise ValueError("Exactly one sealed raw source directory is required: " + region)
        tables[lang] = {}
        for table in TABLES:
            path = bases[0] / (table + ".json")
            raw = path.read_bytes()
            rows = json.loads(raw)
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("Raw table must contain a list of records: " + str(path))
            tables[lang][table] = rows
            sources[lang + "/" + table] = dict(language=lang, region=region, table=table,
                path=str(path.resolve()), sha256=sha(raw), records=len(rows))
    return tables, sources


def build_inventory(tables: dict) -> dict:
    definitions, lookup = [], defaultdict(list)
    issues = {name: [] for name in ("invalid_definitions", "duplicate_region_local_ids", "invalid_or_dangling_edges",
        "same_talk_id_asset_drifts", "same_asset_multiple_talk_ids", "cross_language_edge_identity_conflicts",
        "tweet_nontext_metadata_drifts", "tweet_full_inbound_metadata_drifts", "talk_nonpath_metadata_drifts")}
    by_table_id, by_asset = defaultdict(dict), defaultdict(dict)
    for lang, regional in tables.items():
        for table, rows in regional.items():
            ids = defaultdict(list)
            for index, record in enumerate(rows):
                if positive_int(record.get("id")):
                    ids[record["id"]].append(index)
                if table not in DOMAINS:
                    continue
                errors = definition_errors(table, record)
                identity = raw_identity(table, record)
                key = sha(canonical(dict(language=lang, table=table, index=index, record=record)).encode())
                row = dict(definition_key_sha256=key, language=lang, region=REGIONS[lang], raw_table=table,
                    domain=DOMAINS[table], record_index=index, local_id=record.get("id"), raw_identity=identity,
                    raw_record=record, raw_record_sha256=sha(canonical(record).encode()), identity_errors=errors,
                    definition_valid=not errors, source_reference=lang + "/" + table)
                if table == TWEETS and isinstance(record.get("text"), str):
                    body = record["text"].strip()
                    row.update(nonempty_text_obligation=bool(body), expected_text_sha256=sha(body.encode()), expected_characters=len(body))
                elif table in {TALKS, TUTORIALS} and not errors:
                    row["expected_asset_path"] = asset_path(record)
                definitions.append(row)
                if errors:
                    issues["invalid_definitions"].append(dict(definition_key_sha256=key, language=lang,
                        table=table, index=index, reasons=errors, record=record))
                if positive_int(record.get("id")):
                    lookup[lang, table, record["id"]].append(row)
                    by_table_id[table, record["id"]].setdefault(lang, []).append(row)
                if table == TALKS and not errors:
                    by_asset[lang, record["assetbundleName"], record["lua"]].setdefault(record["id"], []).append(index)
            for identity, indices in ids.items():
                if len(indices) > 1:
                    issues["duplicate_region_local_ids"].append(dict(language=lang, table=table,
                        local_id=identity, record_indices=indices))
    for (lang, bundle, lua), ids in by_asset.items():
        if len(ids) > 1:
            issues["same_asset_multiple_talk_ids"].append(dict(language=lang, assetbundleName=bundle,
                lua=lua, local_ids=sorted(ids), automatic_merge=False))
    for (table, local_id), regional in by_table_id.items():
        if table != TALKS or len(regional) < 2:
            continue
        signatures = {canonical(row["raw_identity"]) for rows in regional.values() for row in rows}
        if len(signatures) > 1:
            issues["same_talk_id_asset_drifts"].append(dict(local_id=local_id,
                identities_by_language={lang: [row["raw_identity"] for row in rows] for lang, rows in regional.items()}))
        metadata = {canonical({key: value for key, value in row["raw_record"].items()
                    if key not in {"id", "assetbundleName", "lua"}}) for rows in regional.values() for row in rows}
        if len(metadata) > 1:
            issues["talk_nonpath_metadata_drifts"].append(dict(local_id=local_id, languages=sorted(regional)))

    inbound, edge_identities, full_inbound = defaultdict(list), defaultdict(dict), defaultdict(list)
    for lang, regional in tables.items():
        for table in EDGE_TABLES:
            rows = regional[table]
            duplicate_ids = Counter(row.get("id") for row in rows if positive_int(row.get("id")))
            for index, record in enumerate(rows):
                if table == TUTORIALS and "mysekaiCharacterTalkTweetId" not in record:
                    continue
                errors = []
                for field in ("id", "mysekaiCharacterTalkTweetId"):
                    if not positive_int(record.get(field)):
                        errors.append("invalid_" + field)
                if positive_int(record.get("id")) and duplicate_ids[record["id"]] != 1:
                    errors.append("duplicate_edge_id")
                tweet_id = record.get("mysekaiCharacterTalkTweetId")
                if positive_int(tweet_id) and len(lookup[lang, TWEETS, tweet_id]) != 1:
                    errors.append("dangling_or_ambiguous_tweet")
                binding, strength, declared_binding = None, None, None
                if table == PREACTIONS:
                    talk_id = record.get("mysekaiCharacterTalkId")
                    declared_binding = dict(kind="talk", talk_id=talk_id)
                    if not positive_int(talk_id):
                        errors.append("invalid_mysekaiCharacterTalkId")
                    else:
                        matches = lookup[lang, TALKS, talk_id]
                        if len(matches) != 1 or not matches[0]["definition_valid"]:
                            errors.append("dangling_or_ambiguous_talk")
                        else:
                            talk = matches[0]["raw_record"]
                            binding = dict(kind="talk", talk_id=talk_id, assetbundleName=talk["assetbundleName"], lua=talk["lua"])
                            strength = "exact_talk_asset_binding"
                elif table == TUTORIALS:
                    declared_binding = dict(kind="tutorial", assetbundleName=record.get("assetbundleName"), lua=record.get("lua"))
                    errors.extend("invalid_" + field for field in ("assetbundleName", "lua") if not asset_valid(record.get(field)))
                    if not errors:
                        binding = dict(kind="tutorial", assetbundleName=record["assetbundleName"], lua=record["lua"])
                        strength = "exact_tutorial_asset_binding"
                else:
                    field = "gameCharacterUnitId" if table == WITHOUT_TALKS else "groupId"
                    declared_binding = dict(kind="character_unit" if table == WITHOUT_TALKS else "fixture_tweet_group", value=record.get(field))
                    if not positive_int(record.get(field)):
                        errors.append("invalid_" + field)
                    else:
                        binding = dict(kind="character_unit" if table == WITHOUT_TALKS else "fixture_tweet_group", value=record[field])
                        strength = "weak_character_or_fixture_binding"
                edge = dict(language=lang, table=table, record_index=index, edge_id=record.get("id"),
                    tweet_id=tweet_id, declared_binding=declared_binding, binding=binding, strength=strength, errors=errors,
                    raw_record_sha256=sha(canonical(record).encode()))
                if errors:
                    issues["invalid_or_dangling_edges"].append(edge)
                if positive_int(record.get("id")):
                    edge_identities[table, record["id"]].setdefault(lang, []).append(edge)
                if positive_int(tweet_id):
                    full_inbound[lang, tweet_id].append(canonical(dict(table=table, record=record)))
                    if not errors and binding is not None:
                        inbound[lang, tweet_id].append(edge)
    conflict_tweets = set()
    for (table, edge_id), regional in edge_identities.items():
        declared_signatures = {canonical(dict(tweet_id=edge["tweet_id"], binding=edge["declared_binding"]))
            for edges in regional.values() for edge in edges}
        resolved_signatures = {canonical(edge["binding"]) for edges in regional.values() for edge in edges if edge["binding"] is not None}
        # Missing regional lookup evidence is unresolved, not a contradictory asset identity.
        if len(regional) > 1 and (len(declared_signatures) > 1 or len(resolved_signatures) > 1):
            issues["cross_language_edge_identity_conflicts"].append(dict(table=table, edge_id=edge_id, edges_by_language=regional))
            conflict_tweets.update(edge["tweet_id"] for edges in regional.values() for edge in edges if positive_int(edge["tweet_id"]))
    for (table, local_id), regional in by_table_id.items():
        if table != TWEETS or len(regional) < 2:
            continue
        nontext = {canonical({key: value for key, value in row["raw_record"].items() if key != "text"})
            for rows in regional.values() for row in rows}
        if len(nontext) > 1:
            issues["tweet_nontext_metadata_drifts"].append(dict(local_id=local_id, languages=sorted(regional)))
        if len({tuple(sorted(full_inbound[lang, local_id])) for lang in regional}) > 1:
            issues["tweet_full_inbound_metadata_drifts"].append(dict(local_id=local_id, languages=sorted(regional)))
    moly_edges, moly_edge_ids = defaultdict(list), {}
    for lang, regional in tables.items():
        rows = regional[PREACTIONS]
        moly_edge_ids[lang] = Counter(row["id"] for row in rows if positive_int(row.get("id")))
        for row in rows:
            if positive_int(row.get("mysekaiCharacterTalkId")):
                moly_edges[lang, row["mysekaiCharacterTalkId"]].append(row)
    return dict(definitions=definitions, lookup=lookup, inbound=inbound, conflict_tweets=conflict_tweets,
                _moly_edges=moly_edges, _moly_edge_ids=moly_edge_ids,
                issues=issues, by_table_id=by_table_id)


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_field")
        result[key] = value
    return result


def _load_moly_provenance(store: Path, inventory: dict) -> None:
    sources = {}
    for path in sorted((store / "cache").glob("*/mysekai_moly_provenance.json")):
        raw = path.read_bytes()
        source = dict(path=str(path.resolve()), sha256=sha(raw), records={}, error=None)
        try:
            records = json.loads(raw, object_pairs_hook=_unique_json)
            if not isinstance(records, dict):
                raise ValueError("invalid_provenance_map")
            source["records"] = records
        except (ValueError, UnicodeError) as exc:
            source["error"] = type(exc).__name__
        sources[path.parent.name] = source
    inventory["_moly_provenance"] = sources


def _verify_moly_inputs(inventory: dict, stage: str) -> None:
    for source in inventory.get("_moly_provenance", {}).values():
        if sha(Path(source["path"]).read_bytes()) != source["sha256"]:
            raise ValueError("Moly provenance input changed " + stage)


def _moly_binding(page: dict, inventory: dict, definition: dict) -> dict:
    source = inventory.get("_moly_provenance", {}).get(page.get("source"), {})
    proof = source.get("records", {}).get(page.get("id"))
    result = dict(status="moly_snapshot_provenance_missing", evidence_level="none",
                  catalog_bundle_bytes_independently_verified=False, version_debts=[])
    if source.get("error"):
        result["status"] = "moly_snapshot_provenance_invalid"
        return result
    if proof is None:
        return result
    result["status"] = "moly_snapshot_provenance_invalid"
    try:
        if not isinstance(proof, dict):
            raise ValueError("invalid_proof")
        binding = {field: proof[field] for field in MOLY_BINDING_FIELDS}
        lang, identity = definition["language"], definition["local_id"]
        region = REGIONS[lang]
        snapshot = dict(id=proof["snapshot_id"], version=proof["snapshot_version"], region=region,
                        available=True, provenance=proof["snapshot_provenance"],
                        catalog=f"/moly/snapshots/{proof['snapshot_id']}/catalog/index.json")
        mysekai_moly.select_snapshot(dict(schemaVersion=2, snapshots=[snapshot]), region)
        detail = MOLY_DETAIL.fullmatch(proof["detail_path"])
        url = urlparse(str(page.get("url") or ""))
        key = re.fullmatch(r"talk:(?:general|fixture):([1-9][0-9]{0,9})", proof["key"])
        if (proof["schema"] != "sekaisync/moly-text-binding@1" or proof["region"] != region
                or not re.fullmatch(r"[a-f0-9]{64}", proof["catalog_sha256"])
                or detail is None or proof["detail_sha256"] != detail[1]
                or key is None or int(key[1]) != identity
                or page.get("canonical_key") != f"mysekai_talk:{lang}:{identity}"
                or url.scheme not in {"http", "https"} or not url.netloc
                or url.path != proof["detail_path"] or url.query or url.fragment
                or page.get("url") != proof["actual_source_url"]
                or page.get("source_etag") != detail[1]
                or canonical(proof["raw_talk"]) != canonical(definition["raw_record"])):
            raise ValueError("source_identity_mismatch")
        edges = inventory["_moly_edges"].get((lang, identity), [])
        if (len(edges) != 1 or not positive_int(edges[0].get("id"))
                or inventory["_moly_edge_ids"][lang][edges[0]["id"]] != 1
                or not positive_int(edges[0].get("mysekaiCharacterTalkTweetId"))
                or canonical(edges[0]) != canonical(proof["raw_preaction"])):
            raise ValueError("raw_edge_mismatch")
        tweets = inventory["lookup"].get((lang, TWEETS, edges[0]["mysekaiCharacterTalkTweetId"]), [])
        if (len(tweets) != 1 or not tweets[0]["definition_valid"]
                or canonical(proof["raw_preview_tweet"]) != canonical(tweets[0]["raw_record"])):
            raise ValueError("raw_preview_mismatch")
        version = proof["raw_version"]
        if not isinstance(version, dict):
            raise ValueError("invalid_version_claim")
        version_debts = [name for name, mismatch in (
            ("snapshot_app_version_differs_or_unsealed", version.get("appVersion") != snapshot["version"]),
            ("snapshot_asset_version_differs_or_unsealed", not snapshot["provenance"].get("assetVersion")
             or version.get("assetVersion") != snapshot["provenance"].get("assetVersion"))) if mismatch]
        if (proof["version_debts"] != version_debts or proof["current_body_freshness"] != "snapshot_only_unknown"
                or proof["release_status"] != "unknown_release"
                or page.get("source_hash") != proof["source_hash"]
                or proof["source_hash"] != sha(canonical(binding).encode())
                or proof["script_fallback_excluded"] is not True):
            raise ValueError("binding_hash_or_claim_mismatch")
        lines = proof["original_lines"]
        if (not isinstance(lines, list) or not 0 < len(lines) <= 10000
                or any(not isinstance(line, dict) or not isinstance(line.get("speaker"), str)
                       or not isinstance(line.get("text"), str) or not line["text"].strip() for line in lines)
                or len(lines) == 1 and lines[0]["text"] == mysekai_moly.plain_text(tweets[0]["raw_record"]["text"])):
            raise ValueError("ambiguous_or_invalid_transcript")
        body = "\n".join((line["speaker"] + "\uff1a" if line["speaker"] else "") + line["text"] for line in lines)
        body_hash = sha(body.encode())
        if (page.get("text") != body or proof["text_sha256"] != body_hash
                or page.get("text_hash") != body_hash
                or page.get("hash") != hashlib.sha1(body.encode()).hexdigest()[:16]):
            result["status"] = "moly_snapshot_body_hash_mismatch"
            return result
        result.update(status="exact_moly_snapshot_source_binding", version_debts=version_debts,
                      evidence_level="local_sidecar_and_sealed_raw_body",
                      raw_version_validation="acquisition_sidecar_claim_only")
    except (KeyError, TypeError, ValueError, AttributeError):
        pass
    return result


def audit_page(page: dict, inventory: dict) -> dict:
    lang, reasons = language(page.get("language")), []
    match = PAGE_ID.fullmatch(str(page.get("id") or ""))
    local_id, table = None, None
    if lang not in REGIONS:
        reasons.append("unsupported_page_language")
    if match is None:
        reasons.append("invalid_page_id")
    else:
        source, locale, kind, numeric_id = match.groups()
        local_id = int(numeric_id)
        table = TWEETS if kind == "mysekai_tweet" else TALKS
        if source != page.get("source"):
            reasons.append("page_id_source_mismatch")
        if language(locale) != lang:
            reasons.append("page_id_locale_mismatch")
        if kind != page.get("kind"):
            reasons.append("page_id_kind_mismatch")
        canonical_key = str(page.get("canonical_key") or "")
        if canonical_key:
            parts = canonical_key.split(":")
            if len(parts) != 3 or parts[0] != kind or language(parts[1]) != lang or parts[2] != numeric_id:
                reasons.append("canonical_key_identity_mismatch")
    matches = inventory["lookup"].get((lang, table, local_id), [])
    definition = matches[0] if len(matches) == 1 else None
    if matches and len(matches) != 1:
        reasons.append("ambiguous_region_local_id")
    elif definition is not None and not definition["definition_valid"]:
        reasons.append("invalid_raw_definition")
    status = "invalid" if reasons else "foreign" if definition is None else "expected"
    text = page.get("text")
    flags = {flag: page.get(flag) for flag in FLAGS}
    product_usable = termindex._page_usable(page, lang)
    primary = (not page.get("aux_flag") and not page.get("auxiliary") and not page.get("scenario_id_mismatch")
               and page.get("trust") in {"A", "B", "C"})
    exact = (table == TWEETS and definition is not None and definition["definition_valid"]
             and isinstance(text, str) and text == definition["raw_record"]["text"].strip())
    path = unquote(urlparse(str(page.get("url") or "")).path)
    asset_binding = "not_applicable"
    moly = None
    bucket_locale = re.search(r"(?:^|/)sekai-(jp|en|cn|tc|kr)-assets(?:/|$)", path)
    if table == TALKS and definition is not None and definition["definition_valid"]:
        if bucket_locale and language(bucket_locale.group(1)) != lang:
            asset_binding = "asset_bucket_locale_mismatch"
        elif path.endswith("/" + definition["expected_asset_path"]):
            asset_binding = "expected_full_asset_path_suffix"
        elif path.startswith("/moly/catalog-store/"):
            moly = _moly_binding(page, inventory, definition)
            asset_binding = moly["status"]
        elif path:
            asset_binding = "different_asset_path"
        else:
            asset_binding = "unverified_asset_provenance"
    result = dict(page_id=page.get("id"), source=page.get("source"), language=lang, kind=page.get("kind"),
        raw_table=table, local_id=local_id, identity_status=status, reasons=reasons,
        definition_key_sha256=definition["definition_key_sha256"] if definition else None,
        url=page.get("url"), source_reference_status="double_json_extension" if path.endswith(".json.json") else "not_double_json",
        product_text_usable=product_usable, primary_candidate=primary, flags=flags,
        usable_primary_expected=(status == "expected" and product_usable and primary),
        tweet_current_body=exact, asset_path_binding=asset_binding,
        body_freshness=("snapshot_body_current_freshness_unproved" if asset_binding == "exact_moly_snapshot_source_binding" else
            "exact_active_raw_body" if exact else "unknown_asset_body_freshness" if table == TALKS else "not_current_raw_body"),
        text_sha256=sha(text.encode()) if isinstance(text, str) else None, text_characters=len(text) if isinstance(text, str) else None,
        stored_text_hash=page.get("text_hash"), stored_text_hash_matches_actual=isinstance(text, str) and page.get("text_hash") == sha(text.encode()),
        source_hash=page.get("source_hash"))
    if moly is not None:
        result["moly_provenance"] = moly
    return result


def assign_acquisition(inventory: dict, audits: list[dict]) -> tuple[list, list]:
    observed = defaultdict(list)
    for row in audits:
        observed[row["language"], row["raw_table"], row["local_id"]].append(row)
    debts, release = [], []
    for definition in inventory["definitions"]:
        lang, table, identity = definition["language"], definition["raw_table"], definition["local_id"]
        pages = observed.get((lang, table, identity), []) if positive_int(identity) else []
        expected = [page for page in pages if page["identity_status"] == "expected"]
        primary = [page for page in expected if page["usable_primary_expected"]]
        if not definition["definition_valid"]:
            status = "invalid_definition"
        elif len(inventory["lookup"][lang, table, identity]) != 1:
            status = "ambiguous_sealed_local_identity"
        elif table == TUTORIALS:
            status = "supplemental_tutorial_domain_not_collected"
        elif table == TWEETS and not definition["nonempty_text_obligation"]:
            status = "no_nonempty_text_obligation"
        elif table == TWEETS and any(page["tweet_current_body"] for page in primary):
            status = "primary_current_raw_body"
        elif table == TALKS and any(page["asset_path_binding"] == "expected_full_asset_path_suffix" for page in primary):
            status = "primary_usable_path_bound_freshness_unknown"
        elif table == TALKS and any(page["asset_path_binding"] == "exact_moly_snapshot_source_binding" for page in primary):
            status = "primary_usable_snapshot_body_provenance_bound"
        elif table == TALKS and any(page["asset_path_binding"].startswith("moly_snapshot_") for page in primary):
            status = next(page["asset_path_binding"] for page in primary if page["asset_path_binding"].startswith("moly_snapshot_"))
        elif table == TALKS and any(page["asset_path_binding"] == "unverified_asset_provenance" for page in primary):
            status = "primary_usable_asset_provenance_unknown"
        elif table == TALKS and primary:
            status = "primary_asset_identity_mismatch"
        elif primary:
            status = "primary_stale_raw_body"
        elif expected:
            status = "bad_only"
        elif pages:
            status = "identity_invalid_only"
        else:
            status = "missing"
        definition.update(acquisition_status=status, observed_pages=len(pages), identity_valid_pages=len(expected),
            usable_primary_pages=len(primary), observed_page_ids=[page["page_id"] for page in pages],
            release_status="unknown_release", asset_acquisition_status="not_probed_by_this_census" if table != TWEETS else "inline_master_body",
            body_freshness="snapshot_body_current_freshness_unproved" if status == "primary_usable_snapshot_body_provenance_bound" else
                "not_applicable_supplemental" if table == TUTORIALS else "unknown_asset_body_freshness" if table == TALKS else
                "current" if status == "primary_current_raw_body" else "not_current")
        if status not in BODY_AVAILABLE | {"no_nonempty_text_obligation"}:
            debts.append({key: definition[key] for key in ("definition_key_sha256", "language", "region", "raw_table", "domain", "local_id",
                "raw_identity", "acquisition_status", "observed_page_ids", "release_status", "asset_acquisition_status")})
        release.append(dict(definition_key_sha256=definition["definition_key_sha256"], language=lang, region=REGIONS[lang],
            domain=definition["domain"], local_id=identity, status="unknown_release",
            raw_timestamp_fields={key: value for key, value in definition["raw_record"].items()
                if key.endswith("At")}, independent_release_evidence=None))
    return debts, release


def structural_families(inventory: dict) -> list[dict]:
    grouped = defaultdict(list)
    for row in inventory["definitions"]:
        if not row["definition_valid"]:
            continue
        identity = dict(domain=row["domain"], **{key: value["value"] for key, value in row["raw_identity"].items()})
        grouped[canonical(identity)].append(row)
    families = []
    for key, rows in sorted(grouped.items()):
        identity = json.loads(key)
        regional = defaultdict(list)
        for row in rows:
            regional[row["language"]].append(row)
        common_edges, conflicts = [], False
        if identity["domain"] == "mysekai_tweet":
            signatures = [{canonical({field: edge[field] for field in ("table", "edge_id", "tweet_id", "binding", "strength")})
                for edge in inventory["inbound"].get((lang, identity["id"]), [])} for lang in regional]
            common_edges = [json.loads(edge) for edge in sorted(set.intersection(*signatures))] if signatures else []
            conflicts = identity["id"] in inventory["conflict_tweets"]
        accepted_pages = {lang: any(row["acquisition_status"] in BODY_AVAILABLE
            for row in matches) for lang, matches in regional.items()}
        families.append(dict(family_key_sha256=sha(key.encode()), raw_composite_identity=identity,
            languages=sorted(regional), all_five_region_definitions=set(regional) == set(REGIONS),
            unique_local_definition_per_language=all(len(matches) == 1 for matches in regional.values()),
            definition_keys={lang: [row["definition_key_sha256"] for row in matches] for lang, matches in regional.items()},
            acquisition_statuses={lang: [row["acquisition_status"] for row in matches] for lang, matches in regional.items()},
            usable_primary_all_five=(set(regional) == set(REGIONS) and all(accepted_pages.values())),
            current_inline_body_all_five=(identity["domain"] == "mysekai_tweet" and set(regional) == set(REGIONS) and all(accepted_pages.values())),
            common_inbound_edge_identities=common_edges, conflicting_inbound_edge_identity=conflicts,
            structural_binding_status=("conflicting_inbound_identity" if conflicts else "common_exact_edge_binding" if common_edges else
                "no_common_edge_binding") if identity["domain"] == "mysekai_tweet" else "exact_id_and_full_asset_path",
            semantic_equivalence_proved=False, release_proved=False,
            body_freshness_proved=identity["domain"] == "mysekai_tweet" and all(accepted_pages.values())))
    return families


def write_jsonl(path: Path, rows: list) -> dict:
    with path.open("x", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical(row) + "\n")
    return dict(path=str(path), sha256=sha(path.read_bytes()), rows=len(rows))


def _verify_code_closure(stage: str) -> None:
    if _code_hashes() != _IMPORT_CODE_HASHES:
        raise ValueError("Census code closure changed " + stage)


def run(generation: Path, production: Path, out: Path) -> dict:
    generation, production, out = generation.resolve(), production.resolve(), out.resolve()
    if any(out == path or out.is_relative_to(path) or path.is_relative_to(out) for path in (production, generation)):
        raise ValueError("Census output must not overlap production or the raw generation")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ValueError("Census output is frozen or nonempty")
    _verify_code_closure("since product imports or before raw acquisition")
    tables, sources = load_generation(generation)
    inventory = build_inventory(tables)
    _load_moly_provenance(production, inventory)
    audits, digest = [], hashlib.sha256()
    database = production / "kb/sekaisync.db"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        generation_row = conn.execute("SELECT value FROM meta WHERE key='active_raw_generation'").fetchone()
        if generation_row is None or json.loads(generation_row[0]) != {region: generation.name for region in REGIONS.values()}:
            raise ValueError("Production active regional raw generations do not match the selected sealed generation")
        for raw_page in conn.execute("SELECT * FROM web_pages WHERE kind IN ('mysekai_tweet','mysekai_talk') ORDER BY source,id"):
            row = audit_page(dict(raw_page), inventory)
            audits.append(row)
            digest.update((canonical(row) + "\n").encode())
        conn.rollback()
    debts, release = assign_acquisition(inventory, audits)
    families = structural_families(inventory)
    if len(release) != len(inventory["definitions"]):
        raise ValueError("Expected and release-unknown ledger totals diverged")
    for source in sources.values():
        if sha(Path(source["path"]).read_bytes()) != source["sha256"]:
            raise ValueError("Sealed raw input changed during census")
    _verify_code_closure("during census computation")
    _verify_moly_inputs(inventory, "during census computation")
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, rows in (("expected-mysekai-definitions", inventory["definitions"]), ("mysekai-page-audits", audits),
                      ("mysekai-acquisition-debts", debts), ("mysekai-structural-families", families), ("mysekai-release-unknown", release)):
        paths[name] = write_jsonl(out / (name + ".jsonl"), rows)
    issues_path = out / "mysekai-identity-issues.json"
    with issues_path.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(inventory["issues"], ensure_ascii=False, indent=2) + "\n")
    paths["identity-issues"] = dict(path=str(issues_path), sha256=sha(issues_path.read_bytes()))
    for source in sources.values():
        if sha(Path(source["path"]).read_bytes()) != source["sha256"]:
            raise ValueError("Sealed raw input changed during census artifact emission")
    _verify_code_closure("during census artifact emission")
    _verify_moly_inputs(inventory, "during census artifact emission")
    counts = defaultdict(Counter)
    for row in inventory["definitions"]:
        counts[row["language"] + "/" + row["domain"]][row["acquisition_status"]] += 1
    summary = dict(schema="sekaisync/p0-full-mysekai-census@2", finished_at_utc=datetime.now(timezone.utc).isoformat(),
        generation=str(generation), production_store=str(production), production_writes=False, network_calls=0,
        database_open_mode="ro", query_only=True, raw_sources=sources, local_snapshot_seals_only=True,
        code_closure=dict(scope="census_script_and_all_recursive_sekaisync_python_sources", files=_IMPORT_CODE_HASHES,
            import_start_and_end_identical=True, run_start_computation_end_artifact_end_identical=True,
            complete_product_python_source_closure=True, third_party_and_python_runtime_closure=False,
            python_version=sys.version),
        expected_definitions=len(inventory["definitions"]), expected_counts=dict(Counter(row["domain"] for row in inventory["definitions"])),
        raw_record_counts={lang: {table: len(rows) for table, rows in regional.items()} for lang, regional in tables.items()},
        moly_sidecar_inputs={name: {key: value for key, value in source.items() if key != "records"}
                            for name, source in inventory["_moly_provenance"].items()},
        moly_evidence_level_counts=dict(Counter(row["moly_provenance"]["evidence_level"] for row in audits if "moly_provenance" in row)),
        observed_pages=len(audits), observed_rows_sha256=digest.hexdigest(),
        observed_identity_counts=dict(Counter(row["identity_status"] for row in audits)),
        acquisition_counts={key: dict(value) for key, value in counts.items()}, acquisition_debts=len(debts),
        release_unknown_rows=len(release), identity_issue_counts={key: len(value) for key, value in inventory["issues"].items()},
        structural_family_counts=dict(Counter(row["raw_composite_identity"]["domain"] for row in families)),
        five_region_family_counts=dict(Counter(row["raw_composite_identity"]["domain"] for row in families if row["all_five_region_definitions"])),
        usable_primary_five_region_family_counts=dict(Counter(row["raw_composite_identity"]["domain"] for row in families if row["usable_primary_all_five"])),
        current_inline_body_five_region_tweets=sum(row["current_inline_body_all_five"] for row in families),
        talk_body_freshness_unknown_rows=sum(row["raw_table"] == TALKS for row in inventory["definitions"]),
        metadata_drift_comparison_scope="All present regional definitions, not only the five-region intersection",
        common_binding_five_region_tweets=sum(row["all_five_region_definitions"] and row["structural_binding_status"] == "common_exact_edge_binding"
            for row in families if row["raw_composite_identity"]["domain"] == "mysekai_tweet"),
        observed_double_json_source_reference_counts=dict(Counter(row["kind"] for row in audits if row["source_reference_status"] == "double_json_extension")),
        evidence_limits=["Raw presence and structural bindings are not release or semantic translation proof",
            "Talk asset paths are not asset bodies; stored talk body freshness remains unknown",
            "Moly sidecars bind local body/raw identity; this census does not independently verify published catalog or bundle bytes",
            "Moly acquisition-time version metadata remains a sidecar claim, not current generation version verification",
            "Unobserved assets were not probed by this census and are not classified as unavailable",
            "Tutorial definitions are distinct supplemental obligations, not numeric character-talk identities",
            "Unknown release definitions are retained separately, never silently treated as complete"],
        files=paths, script_sha256=sha(Path(__file__).read_bytes()))
    with (out / "mysekai-inventory-summary.json").open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2) + "\n")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generation", type=Path, default=ROOT / "store/raw/generations/20260926T085102Z-9e7b8f0f")
    parser.add_argument("--production-store", type=Path, default=ROOT / "store")
    parser.add_argument("--out", type=Path, default=ROOT / "work/p0-exhaustive-20261001/mysekai-inventory-a-01")
    args = parser.parse_args()
    summary = run(args.generation, args.production_store, args.out)
    print(json.dumps({key: value for key, value in summary.items() if key not in {"raw_sources", "files"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
