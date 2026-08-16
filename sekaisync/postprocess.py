from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

from sekaisync.webindex import (
    flatten_web_pages,
    save_web_pages,
    sha256_hex,
    web_page_from_dict,
)


DEFAULT_PLACEHOLDER = "[未翻译]"
_LANGUAGE_SEGMENTS = {"ja", "en", "zh_hans", "zh_hant", "zh_cn", "zh_tw", "ko"}


def _canonical_base(key: str) -> str:
    parts = key.split(":")
    if len(parts) >= 3 and parts[1] in _LANGUAGE_SEGMENTS:
        return ":".join([parts[0]] + parts[2:])
    return key


def mark_untranslated_pages(
    store_root: Path,
    placeholder: str = DEFAULT_PLACEHOLDER,
) -> dict[str, Any]:
    pages = flatten_web_pages(store_root)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for page in pages:
        key = str(page.get("canonical_key") or "")
        if not key:
            continue
        groups[_canonical_base(key)].append(page)

    changed_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    replaced = 0
    for base_key, group in sorted(groups.items()):
        ja_pages = [
            page
            for page in group
            if page.get("language") == "ja"
            and not page.get("asset_mismatch")
            and not page.get("content_language_mismatch")
        ]
        ja_text = ""
        for page in ja_pages:
            candidate = str(page.get("text") or "").strip()
            if candidate:
                ja_text = candidate
                break
        if not ja_text:
            continue
        for page in group:
            if page.get("language") == "ja":
                continue
            if bool(page.get("untranslated", False)):
                continue
            other_text = str(page.get("text") or "").strip()
            if not other_text or other_text != ja_text:
                continue
            original_hash = str(
                page.get("text_hash")
                or sha256_hex(str(page.get("text") or ""))
            )
            page["untranslated"] = True
            page["untranslated_placeholder"] = placeholder
            page["original_text_hash"] = original_hash
            page["text"] = placeholder
            page["text_hash"] = sha256_hex(placeholder)
            page["hash"] = hashlib.sha1(placeholder.encode("utf-8")).hexdigest()[:16]
            page["content_language_mismatch"] = False
            mismatch = str(page.get("asset_mismatch") or "")
            page["asset_mismatch"] = "; ".join(
                token.strip()
                for token in mismatch.split(";")
                if token.strip() and not token.strip().startswith("language_mismatch:")
            )
            changed_by_source[str(page.get("source", ""))].append(page)
            replaced += 1

    for source, items in changed_by_source.items():
        save_web_pages(
            store_root,
            source,
            [web_page_from_dict(item) for item in items],
        )

    return {
        "placeholder": placeholder,
        "replaced_pages": replaced,
        "affected_sources": sorted(changed_by_source),
        "canonical_groups": len(groups),
    }
