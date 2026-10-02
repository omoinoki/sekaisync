"""Strict text-only decoding and master identity binding for Moly snapshots."""

from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import re
import unicodedata
from typing import Any

_IDENTITY = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}\Z")
_KEY = re.compile(r"talk:(?:general|fixture):([1-9][0-9]{0,9})\Z")
_DETAIL = re.compile(r"/moly/catalog-store/([a-f0-9]{64})\.json\Z")
_ASSET = re.compile(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*\Z")
_LUA = re.compile(r"[A-Za-z0-9_-]+\Z")


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("moly_duplicate_json_field")
        result[key] = value
    return result


def decode(text: str, max_bytes: int) -> tuple[Any, str]:
    if not isinstance(text, str):
        raise ValueError("moly_non_text_response")
    raw = text.encode("utf-8")
    if len(raw) > max_bytes:
        raise ValueError("moly_text_response_too_large")
    return json.loads(text, object_pairs_hook=_object), hashlib.sha256(raw).hexdigest()


def _positive(value: Any) -> bool:
    return type(value) is int and 0 < value <= 2**31 - 1


def _digest(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def plain_text(value: str) -> str:
    tag = False
    result = []
    for char in value:
        if char == "<":
            tag = True
            continue
        if char == ">" and tag:
            tag = False
            continue
        if not tag and (char in "\n\t" or unicodedata.category(char) != "Cc"):
            result.append(char)
    return "".join(result)


def select_snapshot(manifest: Any, region: str) -> dict[str, Any] | None:
    if region not in ("jp", "en", "cn", "tc", "kr"):
        raise ValueError("moly_region_invalid")
    if not isinstance(manifest, dict) or type(manifest.get("schemaVersion")) is not int or manifest["schemaVersion"] != 2:
        raise ValueError("moly_manifest_schema_mismatch")
    snapshots = manifest.get("snapshots")
    if not isinstance(snapshots, list) or len(snapshots) > 8:
        raise ValueError("moly_manifest_snapshots_invalid")
    matches = [row for row in snapshots if isinstance(row, dict) and row.get("region") == region]
    if not matches:
        return None
    if len(matches) != 1:
        raise ValueError("moly_duplicate_region_snapshot")
    snapshot = matches[0]
    identity, version = snapshot.get("id"), snapshot.get("version")
    if not (isinstance(identity, str) and _IDENTITY.fullmatch(identity)
            and isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version)
            and identity.startswith(region + "-" + version + "-")
            and isinstance(snapshot.get("provenance", {}), dict)
            and snapshot.get("catalog") == f"/moly/snapshots/{identity}/catalog/index.json"):
        raise ValueError("moly_snapshot_identity_invalid")
    return snapshot if snapshot.get("available") is True else None


def _unique_rows(rows: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if isinstance(row, dict) and _positive(row.get("id")):
            groups[row["id"]].append(row)
    return {identity: group[0] for identity, group in groups.items() if len(group) == 1}


def prepare(snapshot: dict[str, Any], catalog: Any, catalog_hash: str,
            talks: list[dict[str, Any]], tweets: list[dict[str, Any]],
            preactions: list[dict[str, Any]], raw_version: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if raw_version is not None and not isinstance(raw_version, dict):
        raise ValueError("moly_raw_version_invalid")
    if not isinstance(snapshot, dict) or select_snapshot({"schemaVersion": 2, "snapshots": [snapshot]}, snapshot.get("region", "")) is None:
        raise ValueError("moly_snapshot_identity_invalid")
    identity, version, region = snapshot["id"], snapshot["version"], snapshot["region"]
    if not (isinstance(catalog, dict) and type(catalog.get("schemaVersion")) is int and catalog["schemaVersion"] == 2
            and catalog.get("snapshotId") == identity and catalog.get("version") == version
            and catalog.get("region") == region):
        raise ValueError("moly_catalog_snapshot_mismatch")
    entries, details = catalog.get("entries"), catalog.get("details")
    if not (isinstance(entries, list) and len(entries) <= 100000 and isinstance(details, list)
            and len(details) <= 100000 and all(isinstance(path, str) and _DETAIL.fullmatch(path) for path in details)):
        raise ValueError("moly_catalog_shape_invalid")
    key_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    numeric_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("key"), str):
            raise ValueError("moly_catalog_entry_invalid")
        key_groups[entry["key"]].append(entry)
        match = _KEY.fullmatch(entry["key"])
        if match and _positive(int(match[1])):
            numeric_groups[int(match[1])].append(entry)
    if any(len(group) != 1 for group in key_groups.values()):
        raise ValueError("moly_duplicate_catalog_key")
    talk_map, tweet_map = _unique_rows(talks), _unique_rows(tweets)
    edges: dict[int, list[dict[str, Any]]] = defaultdict(list)
    preaction_ids = _unique_rows(preactions)
    for row in preactions:
        if not (isinstance(row, dict) and _positive(row.get("id"))
                and row["id"] in preaction_ids
                and _positive(row.get("mysekaiCharacterTalkId"))
                and _positive(row.get("mysekaiCharacterTalkTweetId"))):
            continue
        talk_id, tweet_id = row["mysekaiCharacterTalkId"], row["mysekaiCharacterTalkTweetId"]
        edges[talk_id].append(row)
    candidates = []
    for talk_id, group in numeric_groups.items():
        if len(group) != 1 or talk_id not in talk_map or len(edges[talk_id]) != 1:
            continue
        entry, raw_talk, edge = group[0], talk_map[talk_id], edges[talk_id][0]
        if not (isinstance(raw_talk.get("assetbundleName"), str) and _ASSET.fullmatch(raw_talk["assetbundleName"])
                and isinstance(raw_talk.get("lua"), str) and _LUA.fullmatch(raw_talk["lua"])):
            continue
        preview, detail = entry.get("preview"), entry.get("detail")
        if not (isinstance(preview, dict) and preview.get("available") is True
                and _positive(preview.get("tweetId")) and isinstance(preview.get("text"), str)
                and edge["mysekaiCharacterTalkTweetId"] == preview["tweetId"]
                and preview["tweetId"] in tweet_map
                and tweet_map[preview["tweetId"]].get("text") == preview["text"]
                and type(detail) is int and 0 <= detail < len(details)):
            continue
        path = details[detail]
        provenance = {"schema": "sekaisync/moly-text-binding@1", "region": region,
                      "snapshot_id": identity, "snapshot_version": version,
                      "snapshot_provenance": snapshot.get("provenance", {}),
                      "catalog_sha256": catalog_hash, "key": entry["key"],
                      "detail_path": path, "detail_sha256": _DETAIL.fullmatch(path)[1],
                      "raw_talk": raw_talk, "raw_preaction": edge,
                      "raw_preview_tweet": tweet_map[preview["tweetId"]],
                      "raw_version": raw_version or {},
                      "version_debts": [name for name, mismatch in (
                          ("snapshot_app_version_differs_or_unsealed", (raw_version or {}).get("appVersion") != version),
                          ("snapshot_asset_version_differs_or_unsealed", not snapshot.get("provenance", {}).get("assetVersion")
                           or (raw_version or {}).get("assetVersion") != snapshot.get("provenance", {}).get("assetVersion"))) if mismatch],
                      "current_body_freshness": "snapshot_only_unknown",
                      "release_status": "unknown_release"}
        candidates.append({"id": talk_id, "entry": entry, "path": path,
                           "bundle_hash": provenance["detail_sha256"],
                           "binding_hash": _digest(provenance), "provenance": provenance})
    return candidates


def decode_bundle(text: str, expected_hash: str) -> dict[str, Any]:
    bundle, digest = decode(text, 4 * 1024 * 1024)
    if digest != expected_hash:
        raise ValueError("moly_detail_bundle_hash_mismatch")
    if not (isinstance(bundle, dict) and type(bundle.get("schemaVersion")) is int and bundle["schemaVersion"] == 2
            and isinstance(bundle.get("entries"), dict)):
        raise ValueError("moly_detail_bundle_schema_mismatch")
    return bundle["entries"]


def transcript(candidate: dict[str, Any], bundle_entries: dict[str, Any]) -> tuple[str, list[dict[str, str]]]:
    entry = candidate["entry"]
    detail = bundle_entries.get(entry["key"])
    if not (isinstance(detail, dict) and detail.get("key") == entry["key"]
            and detail.get("preview") == entry.get("preview")):
        raise ValueError("moly_detail_identity_mismatch")
    presentation, lines = detail.get("presentation"), detail.get("lines")
    if not (isinstance(presentation, dict) and presentation.get("behavior") == "authored"
            and presentation.get("textMode") == "transcript"
            and isinstance(lines, list) and 0 < len(lines) <= 10000
            and all(isinstance(line, dict) and isinstance(line.get("speaker"), str)
                    and isinstance(line.get("text"), str) and line["text"].strip() for line in lines)):
        raise ValueError("moly_empty_or_invalid_transcript")
    # A label-only script can alter the fallback speaker. Its line count and
    # exact plain-text tweet remain invariant, so equality is still ambiguous.
    if len(lines) == 1 and lines[0]["text"] == plain_text(entry["preview"]["text"]):
        raise ValueError("moly_preview_only_or_ambiguous_source")
    text = "\n".join((line["speaker"] + "\uff1a" if line["speaker"] else "") + line["text"] for line in lines)
    return text, lines
