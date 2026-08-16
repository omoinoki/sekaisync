from __future__ import annotations

import re
import unicodedata


_IGNORED_CHARS = re.compile(r"[\s_\-.,，。！？!?·•×:：;；'\"`~～【】\[\]()（）/\\]+")
_LATIN_RE = re.compile(r"[a-z0-9]+")


def normalize_name(text: str) -> str:
    """Return a stable comparison key for names."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = _IGNORED_CHARS.sub("", text)
    return text


def latin_words(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return set(_LATIN_RE.findall(normalized))


def name_variants(name: str) -> list[str]:
    if not name:
        return []
    return [name.strip(), normalize_name(name)]


def similarity_score(query: str, name: str) -> int:
    q = normalize_name(query)
    n = normalize_name(name)
    if not q or not n:
        return 0
    if q == n:
        return 100
    if n.startswith(q) or q.startswith(n):
        return 80
    q_words = latin_words(query)
    n_words = latin_words(name)
    if q_words and n_words and q_words <= n_words and q_words.issubset(n_words):
        return 60
    if q in n:
        return 50
    return 0


def best_match(query: str, names: list[str]) -> tuple[str, int] | None:
    best_name: str | None = None
    best_score = 0
    for name in names:
        score = similarity_score(query, name)
        if score > best_score:
            best_name = name
            best_score = score
    if best_name is None:
        return None
    return best_name, best_score
