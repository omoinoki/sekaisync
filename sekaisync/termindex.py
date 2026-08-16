from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sekaisync.glossary import load_glossary
from sekaisync.layout import glossary_path, web_index_path
from sekaisync.llm_client import LLMClient
from sekaisync.normalize import best_match, normalize_name
from sekaisync.trust import trust_for_source, trust_rank
from sekaisync.webindex import is_auxiliary_page, load_web_pages, text_matches_language


TERM_KINDS = {
    "term",
    "character",
    "unit",
    "location",
    "organization",
    "event",
    "song",
    "system",
    "coined_term",
    "other",
}

TERM_LANGUAGES = {"ja", "en", "zh_tw", "zh_hans", "ko"}

TERM_STORY_KINDS = {
    "event_story",
    "unit_story",
    "card_story",
    "special_story",
    "virtual_live",
    "area_talk",
    "area_dialogue",
    "home_line",
    "character_voice",
    "mysekai",
}

_LOCAL_QUOTE_RE = re.compile(r"[「『“‘\"]([^」』”’\"\n]{2,80})[」』”’\"]")
_LOCAL_KATAKANA_RE = re.compile(r"[\u30A0-\u30FF][\u30A0-\u30FF・ー]{1,40}")
_LOCAL_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")
_LOCAL_LATIN_COMPOUND_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9'’\-]*[ /×·][A-Za-z][A-Za-z0-9'’\-]*)+"
)
_LOCAL_LATIN_STOPWORDS = {
    "a",
    "about",
    "after",
    "all",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "big",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "good",
    "great",
    "had",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "know",
    "let",
    "lets",
    "like",
    "little",
    "more",
    "most",
    "new",
    "no",
    "not",
    "of",
    "old",
    "on",
    "or",
    "right",
    "should",
    "so",
    "some",
    "than",
    "that",
    "the",
    "then",
    "there",
    "these",
    "they",
    "thing",
    "things",
    "this",
    "those",
    "to",
    "very",
    "want",
    "wants",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
}


_LANGUAGE_ALIASES = {"zh_hant": "zh_tw"}

_ZH_FUNCTION_WORDS = (
    "为什么", "因为", "为了", "通过", "由于", "对于", "关于", "以及", "还有",
    "已经", "正在", "即将", "可以", "应该", "时候", "地方", "大家", "自己",
    "我们", "你们", "他们", "她们", "它们", "这个", "那个", "什么", "怎么",
    "直播", "进行", "开始", "结束", "面向", "前往", "来到", "播出", "将会",
    "一起", "还是", "的话", "一样", "真的", "就是", "虽然", "但是", "所以",
    "如果", "然后", "接着", "于是", "无论", "尽管",
)
_ZH_FUNCTION_CHARS = "的了在是要会能让就都也很不没我有你他她它这那和与为从到向被把给对于至而其之还已正在进直播开结面前往来出上下过等因所以去吧吗呢哦啊呀嘛"
_KO_PARTICLES = (
    "에서까지", "부터까지", "까지", "에서", "으로부터", "으로", "부터", "처럼",
    "만큼", "라고", "이라는", "이라", "라는", "은", "는", "이", "가",
    "을", "를", "의", "에", "도", "만", "로", "과", "와", "한테", "에게",
    "보다", "입니다", "이에요", "예요", "합니다", "한다", "하고", "하는", "다", "요",
)
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_HANGUL_RUN_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]+")
_KO_SPLIT_RE = re.compile(r"[\s\u3000\u3001\u3002\uff0c\uff01\uff1f\u2026\u2014]+")
_JA_KATAKANA_STOPWORDS = {
    "ステージ", "ホテル", "ダンス", "グループ", "スタッフ", "テスト", "トーク",
    "カメラ", "チャンス", "トレーニング", "トップ", "ミーティング", "ドキドキ", "ワクワク",
    "アイドル", "グランプリ", "サービス", "シェア", "ネット", "ペンギン", "スタジオ",
    "マッサージ", "レッスン", "ルーム", "アーカイブ", "カメラマン", "タヌキ", "トラ", "クマ",
    "ミク", "リン", "レン", "メイコ", "カイト", "ルカ", "メグ", "ネッパラ",
}
_GENERIC_LATIN_TRANSLATIONS = {
    "MC", "MC's", "Staff Member", "Staff", "Lumina Forum", "Grand Prix",
    "MORE MORE", "MORE HOUSE", "Arisawa", "Minori", "Shizuku", "Airi",
    "Haruka", "Mori", "Miku", "Len", "Rin", "Luka", "Meiko", "Kaito",
    "Organizer", "Trainer", "Planning Committee", "Committee", "Director",
}


@dataclass
class TermRecord:
    id: str
    canonical: str
    source_language: str
    kind: str = "term"
    names: dict[str, str] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    official: bool = False
    source: str = ""
    created_at: str = ""
    confidence: float = 1.0
    trust: str = ""

    def name_for(self, language: str) -> str:
        return self.names.get(language) or self.canonical


def make_term_id(source_language: str, term: str) -> str:
    key = normalize_name(term)
    if not key:
        key = hashlib.sha1(term.encode("utf-8")).hexdigest()[:12]
    return f"term:{source_language}:{key}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def term_to_dict(
    term: TermRecord,
    score: Optional[int] = None,
    truncate_context: bool = False,
) -> dict:
    evidence = []
    for item in term.evidence:
        entry = dict(item)
        context = str(entry.get("context", ""))
        if truncate_context and len(context) > 500:
            entry["context"] = context[:500] + "..."
        evidence.append(entry)
    data = {
        "id": term.id,
        "canonical": term.canonical,
        "source_language": term.source_language,
        "kind": term.kind,
        "names": term.names,
        "evidence": evidence,
        "official": term.official,
        "source": term.source,
        "created_at": term.created_at,
        "confidence": term.confidence,
        "trust": term.trust or trust_for_source(
            term.source,
            official=term.official,
        ),
    }
    if score is not None:
        data["score"] = score
    return data


def term_from_dict(data: dict) -> TermRecord:
    return TermRecord(
        id=str(data["id"]),
        canonical=str(data.get("canonical", "")),
        source_language=str(data.get("source_language", "")),
        kind=str(data.get("kind", "term")),
        names={k: str(v) for k, v in data.get("names", {}).items() if v},
        evidence=[dict(item) for item in data.get("evidence", []) if isinstance(item, dict)],
        official=bool(data.get("official", False)),
        source=str(data.get("source", "")),
        created_at=str(data.get("created_at", "")),
        confidence=float(data.get("confidence", 1.0)),
        trust=str(
            data.get("trust")
            or trust_for_source(
                str(data.get("source", "")),
                official=bool(data.get("official", False)),
            )
        ),
    )


def load_terms(path: Path) -> list[TermRecord]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("terms", [])
    return [term_from_dict(item) for item in data if isinstance(item, dict)]


def _compact_evidence(evidence: Iterable[dict]) -> list[dict]:
    """Keep references instead of copying full story context into the term index."""
    compact: list[dict] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {
            "story_key": str(item.get("story_key") or ""),
            "language": str(item.get("language") or ""),
        }
        for key in ("sentence", "term"):
            value = item.get(key)
            if value:
                entry[key] = str(value)
        compact.append(entry)
    return compact


def save_terms(terms: Iterable[TermRecord], path: Path, compact_evidence: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = []
    for term in terms:
        item = term_to_dict(term)
        if compact_evidence:
            item["evidence"] = _compact_evidence(item["evidence"])
        serialized.append(item)
    data = {
        "version": 1,
        "evidence_style": "compact" if compact_evidence else "full",
        "updated_at": now_iso(),
        "terms": serialized,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

def page_story_key(page: dict) -> Optional[str]:
    url = str(page.get("url", ""))
    page_id = str(page.get("id", ""))
    kind = str(page.get("kind", ""))

    match = re.search(r"/story/event/(\d+)/(\d+)/", url)
    if match:
        return f"event:{match.group(1)}:{match.group(2)}"
    match = re.search(r"event_story:(\d+):(\d+)", page_id)
    if match:
        return f"event:{match.group(1)}:{match.group(2)}"
    match = re.search(r"event_story/(\d+)/(\d+)", url)
    if match:
        return f"event:{match.group(1)}:{match.group(2)}"
    return f"{kind}:{page_id}"


def load_pages(
    store_root: Path,
    input_path: Optional[Path] = None,
    include_overlay: bool = False,
) -> list[dict]:
    if input_path is not None:
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("pages", [])
        return [item for item in data if isinstance(item, dict)]
    pages: list[dict] = []
    index_path = web_index_path(store_root)
    if index_path.exists():
        data = json.loads(index_path.read_text(encoding="utf-8"))
        pages = [item for item in data.get("pages", []) if isinstance(item, dict)]
        if any(isinstance(page, dict) and "text" in page for page in pages[:5]):
            if not include_overlay:
                pages = [page for page in pages if not is_auxiliary_page(page)]
            return pages
    pages = [
        page
        for source_pages in load_web_pages(store_root).values()
        for page in source_pages
    ]
    if not include_overlay:
        pages = [page for page in pages if not is_auxiliary_page(page)]
    return pages

def group_pages_by_story(pages: Iterable[dict]) -> dict[str, dict[str, dict]]:
    grouped: dict[str, dict[str, dict]] = {}
    for page in pages:
        key = page_story_key(page)
        if not key:
            continue
        if str(page.get("kind", "")) not in TERM_STORY_KINDS:
            continue
        language = _term_language(str(page.get("language", "")))
        if not language:
            continue

        def page_usable(candidate: dict) -> bool:
            if candidate.get("asset_mismatch") or candidate.get("content_language_mismatch"):
                return False
            return text_matches_language(language, str(candidate.get("text", "")))

        existing = grouped.setdefault(key, {}).get(language)
        if existing is None:
            grouped.setdefault(key, {})[language] = page
            continue
        existing_ok = page_usable(existing)
        candidate_ok = page_usable(page)
        if candidate_ok and not existing_ok:
            grouped.setdefault(key, {})[language] = page
        elif candidate_ok == existing_ok and existing.get("overlay") and not page.get("overlay"):
            grouped.setdefault(key, {})[language] = page
    return grouped


def find_segment(text: str, term: str) -> str:
    for line in text.splitlines():
        if term in line:
            return line.strip()
    return ""


def extract_terms_from_text(
    text: str,
    story_key: str,
    source_language: str,
    llm: LLMClient,
    max_terms: int = 20,
) -> list[TermRecord]:
    system = (
        "You are a Project Sekai terminology extractor. "
        "Extract only proper nouns, coined phrases, place names, facility names, "
        "event/song/group terms, and gameplay/system terms. "
        "Use the exact source-language spelling from the text. "
        'Return JSON: {"terms":[{"term":"...","kind":"term|location|organization|event|song|system|coined_term|other","confidence":0.0-1.0}]}. '
        "Do not translate and do not invent terms that are not present."
    )
    user = (
        f"Language: {source_language}\n"
        f"Story key: {story_key}\n"
        f"Text:\n{text[:12000]}"
    )
    data = llm.chat_json(system, user)
    items = data.get("terms", []) if isinstance(data, dict) else []
    records: list[TermRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "")).strip()
        if len(term) < 2 or len(term) > 80:
            continue
        kind = str(item.get("kind", "term"))
        if kind not in TERM_KINDS:
            kind = "term"
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        except (TypeError, ValueError):
            confidence = 0.8
        records.append(
            TermRecord(
                id=make_term_id(source_language, term),
                canonical=term,
                source_language=source_language,
                kind=kind,
                names={source_language: term},
                evidence=[
                    {
                        "story_key": story_key,
                        "language": source_language,
                        "sentence": find_segment(text, term),
                        "context": find_segment(text, term) or text[:300],
                    }
                ],
                official=False,
                source="llm",
                created_at=now_iso(),
                confidence=confidence,
                trust="C",
            )
        )
        if len(records) >= max_terms:
            break
    return records


def translate_terms_for_story(
    source_records: list[TermRecord],
    target_language: str,
    target_text: str,
    llm: LLMClient,
) -> dict[str, str]:
    if not source_records:
        return {}
    system = (
        "You translate Project Sekai terms from one language into one target language. "
        "Use the official localized term when the context makes it obvious; otherwise use the "
        "community-accepted translation that matches the same position in the story. "
        'Return JSON: {"translations":[{"term":"...","translation":"...","confidence":0.0-1.0}]}. '
        "Only include terms you can translate confidently."
    )
    term_lines = []
    for record in source_records:
        sentence = record.evidence[0].get("sentence", "") if record.evidence else ""
        term_lines.append(f"- {record.canonical} | source sentence: {sentence[:300]}")
    user = (
        f"Target language: {target_language}\n"
        f"Source language: {source_records[0].source_language}\n"
        f"Terms:\n{chr(10).join(term_lines)}\n\n"
        f"Target episode text:\n{target_text[:12000]}"
    )
    data = llm.chat_json(system, user)
    items = data.get("translations", []) if isinstance(data, dict) else []
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        source_term = str(item.get("term", "")).strip()
        translation = str(
            item.get("translation")
            or item.get("target_name")
            or (item.get("languages") or {}).get(target_language, "")
        ).strip()
        if source_term and translation:
            result[normalize_name(source_term)] = translation
    return result


def merge_terms(records: Iterable[TermRecord]) -> list[TermRecord]:
    by_id: dict[str, TermRecord] = {}
    for record in records:
        existing = by_id.get(record.id)
        if existing is None:
            by_id[record.id] = record
            continue
        for language, name in record.names.items():
            if name and not existing.names.get(language):
                existing.names[language] = name
        for evidence in record.evidence:
            if evidence not in existing.evidence:
                existing.evidence.append(evidence)
        existing.official = existing.official or record.official
        if record.official and not existing.official:
            existing.source = record.source
        if trust_rank(record.trust) > trust_rank(existing.trust):
            existing.trust = record.trust
        existing.confidence = max(existing.confidence, record.confidence)
        if not existing.created_at and record.created_at:
            existing.created_at = record.created_at
    return _merge_reciprocal(list(by_id.values()))


def _reciprocal_signature(record: TermRecord) -> Optional[tuple[str, str]]:
    names = record.names
    ja = normalize_name(names.get("ja", ""))
    zh = normalize_name(names.get("zh_hans", "") or names.get("zh_tw", ""))
    if ja and zh:
        return (ja, zh)
    return None


def _merge_reciprocal(records: list[TermRecord]) -> list[TermRecord]:
    groups: dict[tuple[str, str], list[TermRecord]] = {}
    for record in records:
        signature = _reciprocal_signature(record)
        if signature is None:
            continue
        groups.setdefault(signature, []).append(record)

    merged: list[TermRecord] = []
    consumed: set[int] = set()
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            consumed.add(id(group[0]))
            continue
        primary = sorted(
            group,
            key=lambda item: (
                not item.official,
                item.source_language != "ja",
                -len(item.evidence),
                item.id,
            ),
        )[0]
        for other in group:
            if other is primary:
                continue
            for language, name in other.names.items():
                if name and not primary.names.get(language):
                    primary.names[language] = name
            for evidence in other.evidence:
                if evidence not in primary.evidence:
                    primary.evidence.append(evidence)
            primary.official = primary.official or other.official
            if trust_rank(other.trust) > trust_rank(primary.trust):
                primary.trust = other.trust
            primary.confidence = max(primary.confidence, other.confidence)
            if not primary.created_at and other.created_at:
                primary.created_at = other.created_at
            consumed.add(id(other))
        merged.append(primary)
        consumed.add(id(primary))

    for record in records:
        if id(record) not in consumed:
            merged.append(record)
    return merged


def seed_from_glossary(store_root: Path) -> list[TermRecord]:
    glossary = load_glossary(glossary_path(store_root))
    records: list[TermRecord] = []
    for term in glossary:
        names = {
            language: name
            for language, name in term.names.items()
            if language in TERM_LANGUAGES and name
        }
        if not names:
            continue
        source_language = "ja" if "ja" in names else next(iter(names))
        canonical = names[source_language]
        records.append(
            TermRecord(
                id=make_term_id(source_language, canonical),
                canonical=canonical,
                source_language=source_language,
                kind=term.kind,
                names=names,
                official=term.official,
                source=term.source or "glossary",
                created_at=now_iso(),
                confidence=1.0,
                trust=term.trust or trust_for_source(
                    term.source or "glossary",
                    official=term.official,
                ),
            )
        )
    return merge_terms(records)


def extract_terms(
    pages: Iterable[dict],
    source_language: str,
    target_languages: Iterable[str],
    llm: LLMClient,
    existing: Optional[list[TermRecord]] = None,
    max_terms_per_page: int = 20,
    include_translations: bool = True,
) -> list[TermRecord]:
    targets = [language for language in target_languages if language]
    groups = group_pages_by_story(pages)
    records_by_id = {record.id: record for record in existing or []}

    for story_key, pages_by_language in sorted(groups.items()):
        source_page = pages_by_language.get(source_language)
        if source_page is None:
            continue
        source_text = str(source_page.get("text", ""))
        if not source_text.strip():
            continue
        source_records = extract_terms_from_text(
            source_text,
            story_key,
            source_language,
            llm,
            max_terms=max_terms_per_page,
        )
        if include_translations:
            translations: dict[str, dict[str, str]] = {}
            for target_language in targets:
                if target_language == source_language:
                    continue
                target_page = pages_by_language.get(target_language)
                if target_page is None:
                    continue
                mapping = translate_terms_for_story(
                    source_records,
                    target_language,
                    str(target_page.get("text", "")),
                    llm,
                )
                for normalized_source, translated_name in mapping.items():
                    translations.setdefault(normalized_source, {})[target_language] = translated_name

            for record in source_records:
                source_key = normalize_name(record.names.get(source_language, ""))
                for normalized_source, target_names in translations.items():
                    if normalized_source == source_key:
                        for language, translated_name in target_names.items():
                            if translated_name:
                                record.names[language] = translated_name

        for record in source_records:
            existing_record = records_by_id.get(record.id)
            if existing_record is not None:
                merged = merge_terms([existing_record, record])[0]
                records_by_id[existing_record.id] = merged
            else:
                records_by_id[record.id] = record

    return list(records_by_id.values())


def _is_proper_latin(term: str) -> bool:
    term = term.strip(" .,!?。！？")
    if len(term) < 3 or term.lower() in _LOCAL_LATIN_STOPWORDS:
        return False
    if "/" in term or "×" in term or "·" in term:
        return True
    if term.isupper():
        return True
    return term[0].isupper()


def _local_latin_candidates(segment: str) -> list[str]:
    candidates: list[str] = []
    compound_spans: list[tuple[int, int]] = []
    for match in _LOCAL_LATIN_COMPOUND_RE.finditer(segment):
        term = match.group(0).strip(" .,!?。！？")
        if _is_proper_latin(term):
            candidates.append(term)
            compound_spans.append((match.start(), match.end()))
    for match in _LOCAL_LATIN_TOKEN_RE.finditer(segment):
        if any(match.start() >= start and match.end() <= end for start, end in compound_spans):
            continue
        term = match.group(0)
        if _is_proper_latin(term):
            candidates.append(term)
    return candidates


def _local_candidates(segment: str, source_language: str) -> list[tuple[str, str, bool]]:
    candidates: list[tuple[str, str, bool]] = []
    seen: set[str] = set()

    def add(term: str, kind: str, quoted: bool = False) -> None:
        term = term.strip()
        key = normalize_name(term)
        if len(term) < 2 or len(term) > 80 or not key or key in seen:
            return
        seen.add(key)
        candidates.append((term, kind, quoted))

    for match in _LOCAL_QUOTE_RE.finditer(segment):
        add(match.group(1), "coined_term", quoted=True)
    if source_language == "ja":
        for match in _LOCAL_KATAKANA_RE.finditer(segment):
            add(match.group(0), "coined_term")
    for term in _local_latin_candidates(segment):
        add(term, "term")
    return candidates


def extract_terms_from_text_local(
    text: str,
    story_key: str,
    source_language: str,
    max_terms: int = 20,
) -> list[TermRecord]:
    records: list[TermRecord] = []
    for segment in text.splitlines():
        for term, kind, _quoted in _local_candidates(segment.strip(), source_language):
            if not _source_candidate_acceptable(term, source_language):
                continue
            records.append(
                TermRecord(
                    id=make_term_id(source_language, term),
                    canonical=term,
                    source_language=source_language,
                    kind=kind,
                    names={source_language: term},
                    evidence=[
                        {
                            "story_key": story_key,
                            "language": source_language,
                            "sentence": segment.strip(),
                            "context": segment.strip(),
                        }
                    ],
                    official=False,
                    source="local",
                    created_at=now_iso(),
                    confidence=0.7,
                    trust="C",
                )
            )
            if len(records) >= max_terms:
                return records
    return records


def _coined_candidate_acceptable(term: str, target_language: str) -> bool:
    """Accept only proper-noun-like candidates for coined source terms.

    Character names and sentence-initial phrases such as "Minori" or "Thank you"
    are rejected because the local aligner cannot distinguish them from real
    translations; quoted terms, camel-case brands, and fully capitalised
    multi-word names are kept.
    """
    if target_language == "ja":
        if re.fullmatch(r"[\u30A0-\u30FF][\u30A0-\u30FFー・]{1,40}", term):
            return True
    term = term.strip(" .,!?。！？")
    if not term:
        return False
    if "/" in term or "×" in term or "·" in term:
        return True
    if term.isupper():
        return True
    words = re.findall(r"[A-Za-z][A-Za-z0-9'’\-]*", term)
    if not words:
        return False
    if len(words) == 1:
        word = words[0]
        if word.lower() in _LOCAL_LATIN_STOPWORDS or word.endswith(("'s", "’s")):
            return False
        return any(char.isupper() for char in word[1:])
    return all(
        word
        and word[0].isupper()
        and word.lower() not in _LOCAL_LATIN_STOPWORDS
        and not word.endswith(("'s", "’s"))
        for word in words
    )


def _local_translate_segment(
    target_segment: str,
    target_language: str,
    strict: bool = False,
) -> str:
    candidates = _local_candidates(target_segment, target_language)
    if not candidates:
        return ""
    if strict:
        candidates = [
            (term, kind, quoted)
            for term, kind, quoted in candidates
            if quoted or _coined_candidate_acceptable(term, target_language)
        ]
        if not candidates:
            return ""
    quoted = [term for term, _kind, is_quoted in candidates if is_quoted]
    result = ""
    if len(quoted) == 1:
        result = quoted[0]
    elif len(candidates) == 1:
        result = candidates[0][0]
    elif quoted:
        result = min(quoted, key=len)
    else:
        def score(candidate: tuple[str, str, bool]) -> int:
            term, _kind, _quoted = candidate
            value = 0
            if any(char.isupper() for char in term[1:]):
                value += 4
            if "/" in term or "×" in term or "·" in term:
                value += 3
            if term.isupper():
                value += 2
            return value

        best = max(candidates, key=score)
        if score(best) > 0:
            result = best[0]
        else:
            result = candidates[-1][0]
    if result and not _translation_candidate_acceptable(result, target_language):
        return ""
    return result


def _term_language(language: str) -> str:
    return _LANGUAGE_ALIASES.get(language, language)


def _source_candidate_acceptable(term: str, source_language: str) -> bool:
    term = term.strip(" \t\n\r.。！？!?…—・、,")
    if len(term) < 2 or len(term) > 40:
        return False
    if any(ch in term for ch in "。！？…、，"):
        return False
    if source_language == "ja":
        if re.fullmatch(r"[ぁ-ん]+", term) and len(term) <= 5:
            return False
        if term in _JA_KATAKANA_STOPWORDS:
            return False
        if re.search(r"[ぁ-んァ-ヶー][をにへでとはがのよりもからまで]|[をにへでとはがのよりもからまで][ぁ-んァ-ヶ]", term):
            return False
        if re.search(r"(の|と|を|に|へ|は|が|も|で|から|まで|より|には|では|とは)", term):
            return False
    if source_language in ("zh_hans", "zh_tw", "zh_hant", "zh_cn"):
        if any(ch in term for ch in _ZH_FUNCTION_CHARS):
            return False
    if source_language == "ko":
        if _strip_ko_suffix(term) != term:
            return False
    return True


def _translation_candidate_acceptable(term: str, target_language: str) -> bool:
    lang = _term_language(target_language)
    if not _source_candidate_acceptable(term, lang):
        return False
    if lang == "en":
        words = re.findall(r"[A-Za-z][A-Za-z0-9'’\-]*", term)
        if " ".join(words) in _GENERIC_LATIN_TRANSLATIONS:
            return False
        if len(words) == 1 and not any(char.isupper() for char in term[1:]):
            return False
        if len(words) >= 2 and not any(char.isupper() for char in term[1:]):
            return False
    return True


def _split_zh_run(run: str) -> list[str]:
    word_pattern = "|".join(re.escape(word) for word in sorted(_ZH_FUNCTION_WORDS, key=len, reverse=True))
    pieces = re.split(word_pattern, run)
    out: list[str] = []
    char_pattern = "[" + re.escape("".join(sorted(set(_ZH_FUNCTION_CHARS)))) + "]"
    for piece in pieces:
        for sub in re.split(char_pattern, piece):
            if 2 <= len(sub) <= 8 and not any(ch in sub for ch in _ZH_FUNCTION_CHARS):
                out.append(sub)
    return out


def _strip_ko_suffix(token: str) -> str:
    suffixes = sorted(_KO_PARTICLES, key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                token = token[:-len(suffix)]
                changed = True
                break
    return token


def _ko_token_candidates(segment: str) -> list[str]:
    tokens = [token for token in _KO_SPLIT_RE.split(segment) if token]
    cleaned: list[str] = []
    cleaned_all: list[str] = []
    for token in tokens:
        if not _HANGUL_RUN_RE.fullmatch(token):
            continue
        stem = _strip_ko_suffix(token)
        if len(stem) >= 1:
            cleaned_all.append(stem)
        if len(stem) >= 2:
            cleaned.append(stem)
    out = list(cleaned)
    for a, b in zip(cleaned_all, cleaned_all[1:]):
        out.append(a + " " + b)
    return out

def _local_translation_candidates(segment: str, target_language: str) -> list[str]:
    lang = _term_language(target_language)
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if 2 <= len(candidate) <= 40 and candidate not in out:
            out.append(candidate)

    for match in _LOCAL_QUOTE_RE.finditer(segment):
        add(match.group(1))
    if lang == "ja":
        for term, _kind, _quoted in _local_candidates(segment, "ja"):
            add(term)
    elif lang == "en":
        for term in _local_latin_candidates(segment):
            add(term)
    elif lang in ("zh_hans", "zh_tw", "zh_hant"):
        for term in _local_latin_candidates(segment):
            add(term)
        for run in _CJK_RUN_RE.findall(segment):
            for piece in _split_zh_run(run):
                add(piece)
    elif lang == "ko":
        for term in _local_latin_candidates(segment):
            add(term)
        for piece in _ko_token_candidates(segment):
            add(piece)
    return out


def build_translation_memory(
    pages: Iterable[dict],
    source_language: str,
    target_languages: Iterable[str],
) -> dict[tuple[str, str], str]:
    targets = [language for language in target_languages if language]
    groups = group_pages_by_story(pages)
    co: dict[tuple[str, str], Counter] = defaultdict(Counter)
    glob: dict[str, Counter] = defaultdict(Counter)
    total: dict[tuple[str, str], int] = defaultdict(int)

    for _story_key, by_language in groups.items():
        source_page = by_language.get(source_language)
        if source_page is None:
            continue
        source_lines = [
            line.strip()
            for line in str(source_page.get("text", "")).splitlines()
            if line.strip()
        ]
        if not source_lines:
            continue
        for target_language in targets:
            target_page = by_language.get(target_language)
            if target_page is None:
                continue
            target_lines = [
                line.strip()
                for line in str(target_page.get("text", "")).splitlines()
                if line.strip()
            ]
            if not target_lines:
                continue
            n_source = len(source_lines)
            n_target = len(target_lines)
            for source_index, source_line in enumerate(source_lines):
                source_terms = [
                    term
                    for term, _kind, _quoted in _local_candidates(source_line, source_language)
                    if _source_candidate_acceptable(term, source_language)
                ]
                if not source_terms:
                    continue
                predicted = (
                    round(source_index * (n_target - 1) / max(1, n_source - 1))
                    if n_source > 1
                    else 0
                )
                for target_index in range(
                    max(0, predicted - 2),
                    min(n_target, predicted + 3),
                ):
                    target_candidates = [
                        candidate
                        for candidate in _local_translation_candidates(
                            target_lines[target_index],
                            target_language,
                        )
                        if _translation_candidate_acceptable(candidate, target_language)
                    ]
                    if not target_candidates:
                        continue
                    for term in source_terms:
                        key = (term, target_language)
                        total[key] += 1
                        for candidate in target_candidates:
                            co[key][candidate] += 1
                            glob[target_language][candidate] += 1

    memory: dict[tuple[str, str], str] = {}
    for key, counts in co.items():
        occurrence_total = total[key]
        if occurrence_total < 2:
            continue
        best_score = 0.0
        best_candidate = ""
        for candidate, count in counts.items():
            if count < 2:
                continue
            global_count = glob[key[1]].get(candidate, 1)
            score = (count * count) / max(1, global_count)
            if any(char.isupper() for char in candidate[1:]):
                score += 0.5
            if re.search(r"[/×· ]", candidate):
                score += 0.25
            if score > best_score or (best_candidate and score == best_score and len(candidate) > len(best_candidate)):
                best_score = score
                best_candidate = candidate
        if best_candidate and best_score >= 0.05:
            memory[key] = best_candidate
    return memory


def extract_terms_local(
    pages: Iterable[dict],
    source_language: str,
    target_languages: Iterable[str],
    existing: Optional[list[TermRecord]] = None,
    max_terms_per_page: int = 20,
    translation_memory: Optional[dict[tuple[str, str], str]] = None,
) -> list[TermRecord]:
    targets = [language for language in target_languages if language]
    memory = translation_memory or {}
    groups = group_pages_by_story(pages)
    records_by_id = {record.id: record for record in existing or []}

    for story_key, pages_by_language in sorted(groups.items()):
        source_page = pages_by_language.get(source_language)
        if source_page is None:
            continue
        source_text = str(source_page.get("text", ""))
        if not source_text.strip():
            continue
        source_records = extract_terms_from_text_local(
            source_text,
            story_key,
            source_language,
            max_terms=max_terms_per_page,
        )
        source_segments = [line.strip() for line in source_text.splitlines() if line.strip()]
        for record in source_records:
            source_indexes = [
                index
                for index, line in enumerate(source_segments)
                if record.canonical and record.canonical in line
            ]
            if not source_indexes:
                source_indexes = [0]
            strict = record.kind == "coined_term"
            for target_language in targets:
                if target_language == source_language:
                    continue
                target_page = pages_by_language.get(target_language)
                if target_page is None:
                    continue
                target_segments = [
                    line.strip()
                    for line in str(target_page.get("text", "")).splitlines()
                    if line.strip()
                ]
                translated = ""
                for sentence_index in source_indexes:
                    if sentence_index >= len(target_segments):
                        continue
                    translated = _local_translate_segment(
                        target_segments[sentence_index],
                        target_language,
                        strict=strict,
                    )
                    if translated:
                        break
                if not translated:
                    translated = memory.get((record.canonical, target_language), "")
                if translated:
                    record.names[target_language] = translated
            existing_record = records_by_id.get(record.id)
            if existing_record is not None:
                merged = merge_terms([existing_record, record])[0]
                records_by_id[existing_record.id] = merged
            else:
                records_by_id[record.id] = record

    return list(records_by_id.values())


def lookup_terms(
    terms: Iterable[TermRecord],
    query: str,
    source_language: Optional[str] = None,
    languages: Optional[Iterable[str]] = None,
    limit: int = 8,
) -> list[dict]:
    languages = [language for language in (languages or []) if language]
    scored: list[tuple[TermRecord, int]] = []
    for term in terms:
        names = list(term.names.values())
        if term.canonical and term.canonical not in names:
            names.append(term.canonical)
        matched = best_match(query, names)
        if matched is None:
            continue
        _name, score = matched
        if source_language and term.names.get(source_language) == query:
            score += 10
        if languages:
            covered = [language for language in languages if term.names.get(language)]
            if covered:
                score += min(len(covered) * 2, 10)
        scored.append((term, score))
    scored.sort(key=lambda item: item[1], reverse=True)
    return [
        term_to_dict(term, score=score, truncate_context=True)
        for term, score in scored[:limit]
    ]


def term_status(terms: Iterable[TermRecord]) -> dict:
    terms = list(terms)
    language_counts: dict[str, set[str]] = {}
    official = 0
    with_evidence = 0
    for term in terms:
        if term.official:
            official += 1
        if term.evidence:
            with_evidence += 1
        for language, name in term.names.items():
            if name:
                language_counts.setdefault(language, set()).add(normalize_name(name))
    return {
        "terms": len(terms),
        "official": official,
        "with_evidence": with_evidence,
        "languages": {language: len(names) for language, names in language_counts.items()},
    }
