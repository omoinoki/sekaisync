from __future__ import annotations

import difflib
import re
import unicodedata


_IGNORED_CHARS = re.compile(r"[\s_\-.,，。！？!?·•×:：;；'\"`~～【】\[\]()（）/\\]+")
_LATIN_RE = re.compile(r"[a-z0-9]+")
_LONG_VOWEL = "\u30fc"


def normalize_name(text: str) -> str:
    """Return a stable comparison key for names.

    This is the canonical key: it is what gets stored, sorted, and compared for
    equality. Keep it strict — loosening it would silently merge distinct terms
    in every store that already holds a normalized value.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    text = _IGNORED_CHARS.sub("", text)
    return text


def _fold_kana(text: str) -> str:
    """Map katakana onto hiragana so the two scripts compare equal.

    The katakana and hiragana blocks are laid out in parallel, so the shift is
    a constant. Only the main syllable range is folded; extended katakana and
    small-kana combinations are left alone on purpose, since folding those
    would merge distinct sounds.
    """
    out = []
    for ch in text:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return "".join(out)


def matching_key(text: str) -> str:
    """Loose key for fuzzy lookup: normalize_name plus CJK variant folding.

    A term recalled from memory routinely differs from the stored surface in
    ways that carry no meaning:

    - katakana written as hiragana (アークランド vs あーくらんど);
    - a long-vowel mark dropped (アークランド vs アクランド);
    - full-width vs half-width, or a middle dot left out.

    Simplified/traditional folding is deliberately NOT attempted here: the two
    scripts differ across thousands of characters and a hand-rolled table would
    produce silent wrong matches. Script variants are handled where they
    actually matter, by keeping per-script alignment vocabularies.
    """
    return _fold_kana(normalize_name(text)).replace(_LONG_VOWEL, "")


def latin_words(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return set(_LATIN_RE.findall(normalized))


def name_variants(name: str) -> list[str]:
    if not name:
        return []
    variants = [name.strip(), normalize_name(name)]
    loose = matching_key(name)
    if loose and loose not in variants:
        variants.append(loose)
    return variants


def similarity_score(query: str, name: str) -> int:
    """Score how well ``query`` matches ``name``; 0 means no match.

    Tiers, highest first:

    - 100 exact after normalization;
    - 80 one is a prefix of the other;
    - 70 equal under CJK variant folding (kana script / long-vowel mark);
    - 65 variant folding plus prefix;
    - 60 every latin word of the query appears in the name;
    - 55 close edit distance (typo, one inserted or dropped character);
    - 50 plain substring.

    Callers treat 50 as the "did we match at all" bar (see ``glossary.search``),
    so the fuzzy tiers sit above it rather than replacing it.
    """
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
    if q_words and n_words and q_words <= n_words:
        return 60
    if q in n:
        return 50
    # Loose CJK variants: kana script, long-vowel marks, punctuation.
    fq = matching_key(query)
    fn = matching_key(name)
    if fq and fq == fn:
        return 70
    if len(fq) >= 3 and (fn.startswith(fq) or fq.startswith(fn)):
        return 65
    # Edit distance catches typos and single-character slips. The ratio is
    # length-sensitive, so short keys need a tighter bar than long ones; 0.85
    # on a 4-character key allows exactly one character of difference.
    if len(fq) >= 3 and len(fn) >= 3 and abs(len(fq) - len(fn)) <= 3:
        if difflib.SequenceMatcher(None, fq, fn).ratio() >= 0.85:
            return 55
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
