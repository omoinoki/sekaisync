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
import unicodedata
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
        if ch in stop or ch.isspace() or unicodedata.category(ch).startswith("P"):
            if buf:
                runs.append("".join(buf))
                buf = []
        else:
            buf.append(ch)
    if buf:
        runs.append("".join(buf))
    return runs


def _run_spans(text: str, stop: set[str]):
    """Yield runs with offsets, retaining evidence of real delimiters."""
    start = None
    for i, ch in enumerate(text):
        if ch in stop or ch.isspace() or unicodedata.category(ch).startswith("P"):
            if start is not None:
                yield start, i, text[start:i]
                start = None
        elif start is None:
            start = i
    if start is not None:
        yield start, len(text), text[start:]


def _has_free_boundary(counts: Counter, required_entropy: float) -> bool:
    """A written delimiter is a boundary even without varying neighbours.

    ``None`` denotes punctuation, whitespace, or a complete document edge.
    A substring with one fixed *character* neighbour still fails. Treating
    every run edge as missing data made sentence-final and quoted terms
    systematically undiscoverable.
    """
    if required_entropy <= 0:
        return True
    if counts and counts.get(None, 0) == sum(counts.values()):
        return True
    return _entropy(counts) >= required_entropy


def _association_likelihood(observed: int, prefix: int, suffix: int, total: int) -> float:
    """Positive 2x2 log-likelihood association score, in natural-log units."""
    if observed * total <= prefix * suffix:
        return 0.0
    cells = (observed, prefix - observed, suffix - observed,
             total - prefix - suffix + observed)
    if min(cells) < 0:
        return 0.0
    expected = (prefix * suffix / total, prefix * (total - suffix) / total,
                (total - prefix) * suffix / total,
                (total - prefix) * (total - suffix) / total)
    return 2 * sum(value * math.log(value / mean)
                   for value, mean in zip(cells, expected) if value and mean)


def _discover_candidates(
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
    _content_words: bool = False,
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
    chunks: list[tuple[str, bool]] = []
    remaining = max_chars
    for text in texts:
        if not text:
            continue
        if chunks:
            remaining -= 1  # the inter-document newline also consumes budget
        if remaining <= 0:
            break
        chunk = text[:remaining]
        chunks.append((chunk, len(chunk) == len(text)))
        remaining -= len(chunk)
        if remaining <= 0:
            break
    stop = set(stop_marks)
    boundary = set(boundary_stop_chars)
    freq: Counter[str] = Counter()
    runs: list[tuple[str, bool]] = []
    for text, complete in chunks:
        for _start, end, run in _run_spans(text, stop | {"\n"}):
            runs.append((run, end < len(text) or complete))
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

    words: set[str] = set()
    # Probability base is the character count (unigram total). Using
    # sum(freq.values()) here would also add every 2..max_len window and
    # inflate the base ~max_len-fold, scaling all cohesion scores up by that
    # same factor. See the module docstring.
    n_total = sum(count for seg, count in freq.items() if len(seg) == 1)
    if n_total <= 0:
        return words
    eligible: set[str] = set()
    # A content-word pass must not confuse high PMI with high reliability:
    # common constituents depress PMI even when their association has ample
    # evidence. The likelihood heuristic grows with the number of hypotheses,
    # instead of being tuned against a list of annotated game terms. Adjacent
    # windows are not independent: this is not a false-positive guarantee.
    tests = sum(1 for seg, count in freq.items()
                if count >= min_freq and len(seg) >= min_len)
    likelihood_floor = 2 * math.log(max(tests, 1) / 0.05)
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
            score = (_association_likelihood(f, pf, tf, n_total) if _content_words
                     else math.log2(f * n_total / (pf * tf)))
            floor = likelihood_floor if _content_words else min_cohesion
            if score < floor:
                solid = False
                break
        if not solid:
            continue
        eligible.add(seg)

    # Neighbour histograms dominate memory on dialogue corpora. Count them
    # only after frequency/cohesion filtering, never for every rare n-gram.
    left: dict[str, Counter] = defaultdict(Counter)
    right: dict[str, Counter] = defaultdict(Counter)
    lengths = sorted({len(seg) for seg in eligible})
    for run, complete_right in runs:
        rlen = len(run)
        for length in lengths:
            for i in range(rlen - length + 1):
                seg = run[i:i + length]
                if seg not in eligible:
                    continue
                left[seg][run[i - 1] if i else None] += 1
                if i + length < rlen:
                    right[seg][run[i + length]] += 1
                elif complete_right:
                    right[seg][None] += 1
    for seg in eligible:
        if _content_words:
            # Accessor variety avoids the entropy paradox where two distinct
            # neighbours pass at counts 1:1 but fail at counts 2:1.
            free_left = len(left[seg]) >= 2 or _has_free_boundary(left[seg], math.inf)
            free_right = len(right[seg]) >= 2 or _has_free_boundary(right[seg], math.inf)
        else:
            free_left = _has_free_boundary(left[seg], min_entropy)
            free_right = _has_free_boundary(right[seg], min_entropy)
        if free_left and free_right:
            words.add(seg)
    return words


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
    """Discover words using the existing frequency/PMI/entropy contract.

    ``min_cohesion`` remains measured in bits. Written delimiters provide
    boundary evidence; truncating the input budget does not create one.
    """
    return _discover_candidates(
        texts, min_len=min_len, max_len=max_len, min_freq=min_freq,
        min_cohesion=min_cohesion, min_entropy=min_entropy, max_chars=max_chars,
        stop_marks=stop_marks, boundary_stop_chars=boundary_stop_chars)


def _discover_content_words(texts: Iterable[str], *, min_freq: int = 2,
                            max_chars: int = 4_000_000) -> set[str]:
    """Recall repeated content words independently of named-entity shape.

    A corpus-sized log-likelihood hurdle and accessor variety supply discovery
    evidence. This is not a part-of-speech classifier, a translation decision,
    or a promise to segment unknown words seen in just one context.
    """
    return _discover_candidates(texts, min_freq=min_freq, max_chars=max_chars,
                                boundary_stop_chars="", _content_words=True)
