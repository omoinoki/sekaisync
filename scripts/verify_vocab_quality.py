"""Offline check of alignment-vocabulary quality after the penetration fixes.

Run from the repo root:

    PYTHONPATH=. python scripts/verify_vocab_quality.py

Reads the real store, rebuilds the per-language alignment vocabularies from
scratch (bypassing the on-disk cache), and reports the properties the fixes
were supposed to change: alias-resolved traditional Chinese, no whitespace or
digit fragments, normalized keys, official glossary coverage.

The second half sweeps ``min_cohesion`` against a ground truth (official
glossary names) to pick a threshold that keeps real words without dragging the
old fragment noise back in.
"""

from __future__ import annotations

import random
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sekaisync.termindex import (  # noqa: E402
    _ZH_FUNCTION_CHARS,
    _canon_names,
    _group_page,
    _load_glossary_fallback,
    build_alignment_vocab,
    group_pages_by_story,
    load_pages,
)
from sekaisync.wordseg import discover_words  # noqa: E402

CJK = re.compile(r"^[\u4e00-\u9fff]+$")
HANGUL = re.compile(r"^[\uac00-\ud7af]+$")
PUNCT = re.compile(r"[\s_\-.,，。！？!?·•×:：;；'\"`~～【】\[\]()（）/\\]+")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    return PUNCT.sub("", text)


def corpus(groups: dict, lang: str) -> list[str]:
    out = []
    for by_language in groups.values():
        page = _group_page(by_language, lang)
        if page is not None:
            out.append(str(page.get("text", "")))
    return out


def main() -> int:
    store = Path("store")
    print("loading pages ...")
    pages = load_pages(store, None, include_overlay=False)
    groups = group_pages_by_story(pages)
    print(f"  pages: {len(pages)}   stories: {len(groups)}")

    glossary = _load_glossary_fallback(store)
    print(f"  glossary terms (via db fallback): {len(glossary)}")

    # Ground truth: official Chinese display names. Any of these that a
    # vocabulary fails to contain is a real word the aligner will never accept.
    official_zh: set[str] = set()
    char_names: set[str] = set()
    for term in glossary:
        names = getattr(term, "names", {}) or {}
        canon = _canon_names(names)
        if str(getattr(term, "kind", "")) in {"character", "character_profile"}:
            char_names.update(norm(str(v)) for v in names.values() if v)
            continue
        for key in ("zh_hans", "zh_tw"):
            value = canon.get(key, "")
            if value and CJK.fullmatch(value) and len(value) >= 2:
                official_zh.add(norm(value))
    print(f"  official Chinese names usable as ground truth: {len(official_zh)}")

    targets = ["zh_hans", "zh_tw", "zh_hant", "ko", "en"]
    print(f"\nbuilding vocab for {targets} ...")
    vocab = build_alignment_vocab(groups, targets, glossary)

    print("\n=== vocab sizes ===")
    for key in sorted(vocab):
        words = vocab[key]
        polluted = [w for w in words if PUNCT.search(w)]
        print(f"  {key:9s} {len(words):7d} words   ({len(polluted)} with punct/space)")

    zh_hans = vocab.get("zh_hans", set())
    zh_tw = vocab.get("zh_tw", set())
    if zh_hans and zh_tw:
        print("\n=== traditional-Chinese corpus is segmented ===")
        print(f"  zh_hans-only {len(zh_hans - zh_tw)}   zh_tw-only {len(zh_tw - zh_hans)}"
              "   (zero zh_tw-only would mean the corpus is still unreachable)")

    ko = vocab.get("ko", set())
    if ko:
        pure = [w for w in ko if HANGUL.fullmatch(w)]
        print(f"\n=== korean vocabulary purity ===\n  {len(ko)} words, "
              f"{len(pure)} pure-Hangul ({len(pure) / len(ko) * 100:.1f}%)")

    zh_pure = [w for w in zh_hans if CJK.fullmatch(w)]
    hit = len(official_zh & zh_hans)
    print("\n=== chinese vocabulary vs official ground truth ===")
    print(f"  pure-CJK entries {len(zh_pure)}")
    print(f"  official names present: {hit}/{len(official_zh)} "
          f"({hit / max(1, len(official_zh)) * 100:.1f}%)")
    random.seed(11)
    print("  random sample:")
    for word in random.sample(zh_pure, min(20, len(zh_pure))):
        print(f"    {word}")

    # ── cohesion sweep ───────────────────────────────────────────────
    print("\n=== min_cohesion sweep (zh_hans corpus) ===")
    texts = corpus(groups, "zh_hans")
    print(f"  corpus: {len(texts)} pages, {sum(len(t) for t in texts)} chars")
    header = f"  {'bits':>5} {'words':>7} {'official hit':>13} {'fragments':>10}   sample"
    print(header)
    for bits in (6.0, 7.0, 8.0, 9.0, 10.0, 12.0):
        found = discover_words(
            texts, min_freq=3, max_chars=2_000_000,
            min_cohesion=bits, boundary_stop_chars=_ZH_FUNCTION_CHARS,
        )
        words = {norm(w) for w in found}
        official_hit = len(official_zh & words)
        frag = [w for w in words if w in char_names or any(c in w for c in char_names)]
        sample = " ".join(list(found)[:6])
        print(f"  {bits:5.1f} {len(words):7d} {official_hit:13d} {len(frag):10d}   {sample}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
