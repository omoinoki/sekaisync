from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sekaisync import dbstore
from sekaisync.layout import glossary_path, registry_path, terms_path
from sekaisync.webindex import flatten_web_pages, sha256_hex


_CANONICAL_KINDS = frozenset(
    {
        "event_story",
        "unit_story",
        "card_story",
        "special_story",
        "virtual_live",
        "area_talk",
        "self_intro",
        "home_line",
        "mysekai_talk",
        "mysekai_tweet",
    }
)


def _issue(code: str, severity: str, item: str, detail: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": severity,
        "item": item,
        "detail": detail,
    }


def effective_text_hash(item: dict[str, Any]) -> str:
    """The hash to compare this page by, computed from the raw text when absent.

    The previous inline form was ``str(item.get("text_hash")) or
    sha256_hex(...)``.  ``str(None)`` is the *string* ``"None"``, which is
    truthy, so for a page with no stored hash the fallback never ran and every
    such page collapsed to the group hash ``"None"``.  Two pages with
    completely different text were therefore judged to be mirrors of each
    other — a real content conflict silently reported as a duplicate.

    Empty string and whitespace already fell through correctly; the bug was
    specific to a missing/None hash.  Returning the computed hash keeps the
    "unknown hash" case from erasing a genuine difference.
    """
    raw = item.get("text_hash")
    stored = "" if raw is None else str(raw).strip()
    if stored:
        return stored
    return sha256_hex(str(item.get("text", "")))


def verify_web_integrity(store_root: Path, limit: int = 20) -> dict[str, Any]:
    """Audit web-page integrity, keeping totals distinct from samples.

    ``limit`` bounds how many *examples* are returned.  It must not bound the
    counts: previously ``issues`` was sliced to ``limit`` and the caller then
    reported ``len(that slice)`` as the total, so a store with 200 problems
    reported 20 and looked healthier than it was.  Totals now count every
    problem found; ``*_truncated`` flags say whether the sample is complete.
    """
    pages = flatten_web_pages(store_root)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues: list[dict[str, str]] = []
    hash_unknown = 0
    canonical_missing = 0
    canonical_not_applicable = 0
    source_hash_known = 0
    hash_mismatches = 0
    asset_mismatches = 0
    content_language_mismatches = 0
    scenario_id_mismatches = 0
    version_fingerprint_known = 0

    for page in pages:
        canonical = str(page.get("canonical_key") or "")
        text_hash = str(page.get("text_hash") or "")
        current_hash = sha256_hex(str(page.get("text", "")))
        if text_hash:
            if text_hash != current_hash:
                hash_mismatches += 1
                issues.append(
                    _issue(
                        "text_hash_mismatch",
                        "high",
                        str(page.get("id", "")),
                        f"stored {text_hash} != computed {current_hash}",
                    )
                )
        else:
            hash_unknown += 1
        if page.get("source_hash"):
            source_hash_known += 1
        if page.get("source_last_modified") or page.get("source_etag"):
            version_fingerprint_known += 1
        hard_flagged = bool(page.get("asset_mismatch") or page.get("content_language_mismatch"))
        if hard_flagged:
            asset_mismatches += 1
            detail = str(page.get("asset_mismatch") or "")
            if page.get("content_language_mismatch") and "language_mismatch" not in detail:
                detail = f"language_mismatch: expected {page.get('language') or '?'}, text script mismatch"
            issues.append(
                _issue(
                    "asset_mismatch",
                    "high",
                    str(page.get("id", "")),
                    detail,
                )
            )
        if page.get("content_language_mismatch"):
            content_language_mismatches += 1
        if page.get("scenario_id_mismatch"):
            scenario_id_mismatches += 1
            issues.append(
                _issue(
                    "scenario_id_mismatch",
                    "medium",
                    str(page.get("id", "")),
                    str(page.get("scenario_id_mismatch")),
                )
            )
        # P0 fix: flagged pages now JOIN canonical groups so their conflicts
        # are visible to agents (previously silently excluded). We tag them
        # so the report can distinguish clean vs flagged entries.
        if canonical and not bool(page.get("untranslated", False)):
            groups[canonical].append(page)
        elif not canonical:
            kind = str(page.get("kind", "")).lower()
            if bool(page.get("auxiliary") or page.get("overlay")):
                canonical_not_applicable += 1
            elif kind in _CANONICAL_KINDS:
                canonical_missing += 1
            else:
                canonical_not_applicable += 1

    duplicate_groups = []
    conflict_groups = []
    mirror_duplicate_count = 0
    for canonical, items in sorted(groups.items()):
        if len(items) <= 1:
            continue
        hashes = {effective_text_hash(item) for item in items}
        group = {
            "canonical_key": canonical,
            "count": len(items),
            "items": [
                {
                    "id": item.get("id", ""),
                    "source": item.get("source", ""),
                    "trust": item.get("trust", ""),
                    "text_hash": item.get("text_hash", ""),
                }
                for item in items[:limit]
            ],
        }
        if len(hashes) > 1:
            conflict_groups.append(group)
            issues.append(
                _issue(
                    "canonical_conflict",
                    "high",
                    canonical,
                    f"{len(items)} pages with conflicting text hashes",
                )
            )
        else:
            duplicate_groups.append(group)
            mirror_duplicate_count += len(items) - 1

    return {
        "pages": len(pages),
        "canonical_keys": len(groups),
        "mirror_duplicates": mirror_duplicate_count,
        "conflict_groups": len(conflict_groups),
        "hash_mismatches": hash_mismatches,
        "hash_unknown": hash_unknown,
        "source_hash_known": source_hash_known,
        "version_fingerprint_known": version_fingerprint_known,
        "asset_mismatches": asset_mismatches,
        "content_language_mismatches": content_language_mismatches,
        "scenario_id_mismatches": scenario_id_mismatches,
        "canonical_missing": canonical_missing,
        "canonical_not_applicable": canonical_not_applicable,
        # Totals count every problem; the *_samples lists are capped by `limit`.
        # A caller must be able to tell "3 problems, all shown" from
        # "300 problems, 20 shown" — before this split, a large store looked
        # clean because only the sample size was reported.
        "duplicate_groups_total": len(duplicate_groups),
        "conflict_groups_total": len(conflict_groups),
        "issues_total": len(issues),
        "duplicate_groups": duplicate_groups[:limit],
        "conflict_group_samples": conflict_groups[:limit],
        "issues": issues[:limit],
        "sample_limit": limit,
        "duplicate_groups_truncated": len(duplicate_groups) > limit,
        "conflict_groups_truncated": len(conflict_groups) > limit,
        "issues_truncated": len(issues) > limit,
    }


def cross_instance_reconciliation(store_root: Path, limit: int = 20) -> dict[str, Any]:
    """Audit the same canonical content across registered instances.

    Compares ``text_hash`` / ``source_last_modified`` per canonical key across
    the instance sources that provide the page.  Content drift between
    instances is a conflict (the "last line of defense" against an agent
    quoting stale or divergent text).  This does not re-verify hash integrity;
    it cross-checks instances against each other.
    """
    pages = flatten_web_pages(store_root)
    by_canonical: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        canonical = str(page.get("canonical_key") or "")
        if canonical and not bool(page.get("untranslated", False)):
            by_canonical[canonical].append(page)

    groups: list[dict[str, Any]] = []
    drift_instances: dict[str, dict[str, Any]] = {}
    total_drift = 0
    for canonical, items in sorted(by_canonical.items()):
        if len(items) <= 1:
            continue
        by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items:
            by_source[str(item.get("source") or "")].append(item)
        if len(by_source) <= 1:
            continue
        hashes = {effective_text_hash(item) for item in items}
        entry = {
            "canonical_key": canonical,
            "drift": len(hashes) > 1,
            "instances": {
                source: {
                    "pages": len(inst),
                    "text_hashes": sorted(
                        {effective_text_hash(item) for item in inst}
                    ),
                    "last_modified": max(
                        (str(item.get("source_last_modified") or "") for item in inst),
                        default="",
                    ),
                }
                for source, inst in sorted(by_source.items())
            },
        }
        if len(hashes) > 1:
            total_drift += 1
            for source, inst in by_source.items():
                key = str(source)
                bucket = drift_instances.setdefault(
                    key,
                    {"drift_pages": 0, "latest": "", "stale": ""},
                )
                bucket["drift_pages"] += len(inst)
        groups.append(entry)

    summary = {
        "multi_instance_keys": sum(
            1 for entry in groups if len(entry["instances"]) > 1
        ),
        "drift_keys": total_drift,
        "drift_by_instance": drift_instances,
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "groups": groups[:limit],
    }


def _verify_unique_layer(
    name: str,
    items: list[Any],
    limit: int,
) -> dict[str, Any]:
    by_id: dict[str, list[Any]] = defaultdict(list)
    for item in items:
        by_id[str(getattr(item, "id", ""))].append(item)
    duplicate_ids = [item_id for item_id, group in by_id.items() if len(group) > 1]
    issues = []
    for item_id in duplicate_ids[:limit]:
        issues.append(
            _issue(
                f"{name}_duplicate_id",
                "high",
                item_id,
                f"appears {len(by_id[item_id])} times",
            )
        )
    return {
        "items": len(items),
        "duplicate_ids": len(duplicate_ids),
        "issues": issues,
        # One issue per duplicated id: the total is known even when the sample
        # is capped, so callers never have to infer a total from a sample.
        "issues_total": len(duplicate_ids),
        "issues_truncated": len(duplicate_ids) > limit,
    }


def run_integrity_check(store_root: Path, limit: int = 20) -> dict[str, Any]:
    web = verify_web_integrity(store_root, limit=limit)
    reconciliation = cross_instance_reconciliation(store_root, limit=limit)
    registry = _verify_unique_layer(
        "registry",
        dbstore.load_entities(store_root),
        limit,
    )
    glossary = _verify_unique_layer(
        "glossary",
        dbstore.load_glossary_terms(store_root),
        limit,
    )
    terms = _verify_unique_layer(
        "terms",
        dbstore.load_terms_records(store_root),
        limit,
    )
    all_issues = (
        web["issues"]
        + registry["issues"]
        + glossary["issues"]
        + terms["issues"]
    )
    issues_total = (
        web["issues_total"]
        + registry["issues_total"]
        + glossary["issues_total"]
        + terms["issues_total"]
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "layers": {
            "web": web,
            "registry": registry,
            "glossary": glossary,
            "terms": terms,
            "cross_instance": reconciliation,
        },
        "summary": {
            "duplicate_ids": registry["duplicate_ids"]
            + glossary["duplicate_ids"]
            + terms["duplicate_ids"],
            "mirror_duplicates": web["mirror_duplicates"],
            "conflicts": web["conflict_groups_total"],
            "cross_instance_drift": reconciliation["summary"]["drift_keys"],
            "hash_mismatches": web["hash_mismatches"],
            "asset_mismatches": web["asset_mismatches"],
            "content_language_mismatches": web["content_language_mismatches"],
            "scenario_id_mismatches": web["scenario_id_mismatches"],
            # Every problem found, not the size of the returned sample.
            "issues": issues_total,
            "issues_sample_count": len(all_issues),
            "issues_truncated": issues_total > len(all_issues),
            "sample_limit": limit,
        },
        "issues": all_issues[:limit],
    }
