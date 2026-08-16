from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sekaisync.glossary import load_glossary
from sekaisync.layout import glossary_path, registry_path, terms_path
from sekaisync.registry import load_registry
from sekaisync.termindex import load_terms
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


def verify_web_integrity(store_root: Path, limit: int = 20) -> dict[str, Any]:
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
        if canonical and not hard_flagged and not bool(page.get("untranslated", False)):
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
        hashes = {
            str(item.get("text_hash"))
            or sha256_hex(str(item.get("text", "")))
            for item in items
        }
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
        "duplicate_groups": duplicate_groups[:limit],
        "conflict_group_samples": conflict_groups[:limit],
        "issues": issues[:limit],
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
    }


def run_integrity_check(store_root: Path, limit: int = 20) -> dict[str, Any]:
    web = verify_web_integrity(store_root, limit=limit)
    registry = _verify_unique_layer(
        "registry",
        load_registry(registry_path(store_root)),
        limit,
    )
    glossary = _verify_unique_layer(
        "glossary",
        load_glossary(glossary_path(store_root)),
        limit,
    )
    terms = _verify_unique_layer(
        "terms",
        load_terms(terms_path(store_root)),
        limit,
    )
    all_issues = (
        web["issues"]
        + registry["issues"]
        + glossary["issues"]
        + terms["issues"]
    )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "layers": {
            "web": web,
            "registry": registry,
            "glossary": glossary,
            "terms": terms,
        },
        "summary": {
            "duplicate_ids": registry["duplicate_ids"]
            + glossary["duplicate_ids"]
            + terms["duplicate_ids"],
            "mirror_duplicates": web["mirror_duplicates"],
            "conflicts": web["conflict_groups"],
            "hash_mismatches": web["hash_mismatches"],
            "asset_mismatches": web["asset_mismatches"],
            "content_language_mismatches": web["content_language_mismatches"],
            "scenario_id_mismatches": web["scenario_id_mismatches"],
            "issues": len(all_issues),
        },
        "issues": all_issues[:limit],
    }
