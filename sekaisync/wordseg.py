"""Unsupervised CJK new-word discovery (Matrix67-style).

Pure stdlib. Discovers words from raw text by three signals:

- frequency      : an n-gram must occur at least ``min_freq`` times;
- cohesion       : pointwise mutual information at every split point,
                   ``log2(P(word) / (P(prefix) * P(suffix)))``, must exceed
                   ``min_cohesion`` bits (the word holds together);
- boundary entropy: the character distributions immediately before/after the
                   word must be diverse (min of the two entropies exceeds
                   ``min_entropy``), i.e. the word appears in many different
                   contexts rather than as a fragment of a longer fixed phrase.

This replaces naive sub-window enumeration for Chinese candidate generation:
enumerated fragments (爱莉/笑梦/弧光) flood the aligner with noise, while
discovered words (网络天堂/菲尼克斯奇幻乐园) are exactly the units the
distribution aligner needs.

Two calibration details matter more than they look, and both were wrong:

- ``min_cohesion`` is in **bits**, not a raw ratio. The old code compared a raw
  ratio against 10.0, which is only 3.3 bits — loose enough that grammar
  fragments ('的攝影', '們推', '在摸') sailed through and became alignment
  candidates. Reporting PMI in bits puts the threshold on a scale where 8-12
  actually separates words from phrasing.
- The probability base ``n_total`` is the **character count** (the unigram
  total), not ``sum(freq.values())``. Summing every window adds all 2..max_len
  n-grams too, inflating the base roughly ``max_len``-fold. Because cohesion
  divides one count by a two-factor denominator, that inflation scaled every
  score up by the same factor and quietly disabled the threshold.
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
    marks, and the inner loop needs no per-window membership test.

    Whitespace counts as a boundary even though it is not in ``stop_marks``:
    Korean and Latin mark word boundaries with spaces, so an n-gram stride that
    walks across them manufactures fragments like ' 1년 동안' and ' KAITO'
    instead of words.
    """
    runs: list[str] = []
    buf: list[str] = []
    for ch in text:
        if ch in stop or ch.isspace():
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
    boundary_stop_chars: str = "",
) -> set[str]:
    """Return the set of discovered words for a language corpus.

    ``texts`` are joined with sentence marks; n-grams spanning stop marks or
    whitespace are never counted, so cross-sentence garbage cannot become a
    word.

    ``boundary_stop_chars`` lists characters that may not begin or end a word
    (Chinese function characters, typically). Without it the pass happily
    promotes grammar phrasing that merely happens to co-occur often, which is
    the dominant source of noise in the Chinese vocabulary.
    """
    # One pass, bounded prefix, identical for lists and one-shot iterators.
    # Never stride characters or revisit the iterable. The final chunk may be
    # truncated at the budget; its artificial end has no right-context evidence.
    if max_chars <= 0:
        return set()
    chunks: list[str] = []
    remaining = max_chars
    for text in texts:
        if not text:
            continue
        if chunks:
            remaining -= 1  # the inter-document newline also consumes budget
        if remaining <= 0:
            break
        chunks.append(text[:remaining])
        remaining -= len(chunks[-1])
        if remaining <= 0:
            break
    joined = "\n".join(chunks)

    stop = set(stop_marks)
    boundary = set(boundary_stop_chars)
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
    # Probability base is the character count (unigram total). Using
    # sum(freq.values()) here would also add every 2..max_len window and
    # inflate the base ~max_len-fold, scaling all cohesion scores up by that
    # same factor. See the module docstring.
    n_total = sum(count for seg, count in freq.items() if len(seg) == 1)
    if n_total <= 0:
        return words
    for seg, f in freq.items():
        if f < min_freq or len(seg) < min_len:
            continue
        if boundary and (seg[0] in boundary or seg[-1] in boundary):
            continue
        # Cohesion in bits: the weakest split must still hold together.
        solid = True
        for p in range(1, len(seg)):
            head, tail = seg[:p], seg[p:]
            pf = freq.get(head, 0)
            tf = freq.get(tail, 0)
            if pf == 0 or tf == 0:
                solid = False
                break
            if math.log2(f * n_total / (pf * tf)) < min_cohesion:
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
