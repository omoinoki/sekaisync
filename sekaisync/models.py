from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class Entity:
    id: str
    type: str
    region: str
    regions: list[str] = field(default_factory=list)
    names: Dict[str, str] = field(default_factory=dict)
    facts: Dict[str, Any] = field(default_factory=dict)
    source: str = ""
    version: Optional[str] = None
    demo: bool = False
    trust: str = ""

    @property
    def canonical_name(self) -> str:
        return self.names.get("en") or self.names.get("ja") or next(iter(self.names.values()), self.id)

    def name_for(self, language: str) -> str:
        return self.names.get(language) or self.canonical_name


@dataclass
class GlossaryTerm:
    id: str
    kind: str
    canonical: str
    names: Dict[str, str] = field(default_factory=dict)
    official: bool = False
    source: str = ""
    demo: bool = False
    trust: str = ""

    def name_for(self, language: str) -> str:
        return self.names.get(language) or self.canonical


@dataclass
class FactPack:
    entity_id: str
    entity_type: str
    language: str
    text: str
    raw_json_tokens: int = 0
    fact_pack_tokens: int = 0

    @property
    def token_ratio(self) -> float:
        if self.raw_json_tokens <= 0:
            return 1.0
        return self.fact_pack_tokens / self.raw_json_tokens


@dataclass
class WebPage:
    id: str
    source: str
    url: str
    title: str
    language: str
    kind: str
    text: str
    crawled_at: str
    hash: str = ""
    tos_accepted: bool = False
    derived: bool = False
    trust: str = ""
    canonical_key: str = ""
    source_hash: str = ""
    text_hash: str = ""
    untranslated: bool = False
    untranslated_placeholder: str = ""
    original_text_hash: str = ""
    asset_mismatch: str = ""
    scenario_id_mismatch: str = ""
    content_language_mismatch: bool = False
    source_last_modified: str = ""
    source_etag: str = ""
    auxiliary: bool = False
    translation_source: str = ""
    source_language: str = ""
    namespace: str = ""
    event_id: int = 0
    episode_no: int = 0
    overlay: bool = False
    # Multi-instance profile support: ``source`` holds the instance ID while
    # ``source_type`` records the backend class ("sekai_viewer" / "moesekai")
    # so trust/derived/announcement rules work without profile lookups.
    # Legacy pages without these fields default to the canonical type IDs.
    source_type: str = ""
    instance: str = ""
