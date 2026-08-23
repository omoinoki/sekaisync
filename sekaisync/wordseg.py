"""Unsupervised CJK new-word discovery (Matrix67-style).

Pure stdlib. Discovers words from raw text by three signals:

- frequency      : an n-gram must occur at least ``min_freq`` times;
- cohesion       : for every split point, P(word) / (P(prefix) * P(suffix))
                   must exceed ``min_cohesion`` (the word holds together);
- boundary entropy: the character distributions immediately before/after the
                   word must be diverse (min of the two entropies exceeds
                   ``min_entropy``), i.e. the word appears in many different
                   contexts rather than as a fragment of a longer fixed phrase.

This replaces naive sub-window enumeration for Chinese/Korean candidate
generation: enumerated fragments (爱莉/笑梦/弧光) flood the aligner with noise,
while discovered words (网络天堂/菲尼克斯奇幻乐园) are exactly the units the
distribution aligner needs.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Iterable


def _entropy(counts: Counter | None) -> float:
    if not counts:
        return 0.0
    total = sum(counts.values())
    if total <= 0:
        return 0.0
    h = 0.0
    for c in counts.values():
        p = c / total
        if p > 0:
            h -= p * math.log2(p)
    return h


def _runs(text: str, stop: set[str]) -> list[str]:
    """Split text into punctuation-free runs so windows never cross sentence
    marks, and the inner loop needs no per-window membership test."""
    runs: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch in stop or ch == "\n":
            if buf:
                runs.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        runs.append("".join(buf))
    return runs


def discover_words(
    texts: Iterable[str],
    *,
    min_len: int = 2,
    max_len: int = 8,
    min_freq: int = 3,
    min_cohesion: float = 10.0,
    min_entropy: float = 1.0,
    max_chars: int = 4_000_000,
    stop_marks: str = "。！？!?\n…—・、，,；;：:\"'「」『』（）()【】",
) -> set[str]:
    """Return the set of discovered words for a language corpus.

    ``texts`` are joined with sentence marks; n-grams spanning stop marks are
    never counted, so cross-sentence garbage cannot become a word."""
    joined = "\n".join(t for t in texts if t)
    # Cap volume by dropping WHOLE chunks — character-stride slicing shreds
    # words (网络天堂 -> 络天…) and silently kills discovery.
    if len(joined) > max_chars:
        chunks = [t for t in texts if t]
        keep_every = math.ceil(len(joined) / max_chars)
        joined = "\n".join(chunks[i] for i in range(0, len(chunks), keep_every))

    stop = set(stop_marks)
    freq: Counter[str] = Counter()
    left: dict[str, Counter] = defaultdict(Counter)
    right: dict[str, Counter] = defaultdict(Counter)

    for line in joined.split("\n"):
        if not line:
            continue
        for run in _runs(line, stop):
            rlen = len(run)
            if rlen < 1:
                continue
            # Count UNIGRAMS as well — cohesion needs P(single char) as the
            # baseline at every split point; without them every word dies
            # at its first split (freq(head)==0).
            for ln_val in range(1, max_len + 1):
                for i in range(0, rlen - ln_val + 1):
                    seg = run[i:i + ln_val]
                    freq[seg] += 1
                    if i > 0:
                        left[seg][run[i - 1]] += 1
                    if i + ln_val < rlen:
                        right[seg][run[i + ln_val]] += 1

    words: set[str] = set()
    n_total = sum(freq.values())
    if n_total <= 0:
        return words
    for seg, f in freq.items():
        if f < min_freq or len(seg) < min_len:
            continue
        # Cohesion: weakest split must still be solid.
        p_word = f / n_total
        solid = True
        for p in range(1, len(seg)):
            head, tail = seg[:p], seg[p:]
            pf = freq.get(head, 0)
            tf = freq.get(tail, 0)
            if pf == 0 or tf == 0:
                solid = False
                break
            if p_word / ((pf / n_total) * (tf / n_total)) < min_cohesion:
                solid = False
                break
        if not solid:
            continue
        # Boundary freedom: both sides must vary across contexts.
        h_left = _entropy(left.get(seg))
        h_right = _entropy(right.get(seg))
        if min(h_left, h_right) < min_entropy:
            continue
        words.add(seg)
    return words
