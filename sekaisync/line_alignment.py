"""Bounded, monotone dialogue alignment shared by every local term pipeline.

Positions are evidence only after alignment, not a window of nearby guesses.
This is a deterministic structural aligner (not a semantic translation model):
it combines shared lexical anchors, dialogue turns, punctuation and relative
length, permits split/merged lines and omissions, and abstains on ambiguous
paths. It never searches outside the two supplied pages.
"""
from __future__ import annotations

from bisect import bisect_left
from collections import Counter, OrderedDict, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import math
import re
import unicodedata
from typing import Sequence


_MAX_LINES = 4096
_MAX_CELLS = 180_000
_BAND = 12
_SESSION_MAX_PAIRS = 8192
_STEPS = ((1, 1), (1, 2), (2, 1), (1, 0), (0, 1))
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9'’/\-]{1,}")
_NUMBER = re.compile(r"\d+(?:[.,:]\d+)*")
_COMMON_LATIN = frozenset({
    "the", "and", "for", "you", "your", "that", "this", "with", "have",
    "are", "was", "but", "not", "can", "all", "just", "from", "what",
    "it's", "don't", "out", "our", "she", "her", "his", "they", "them",
    "yes", "no", "oh", "ah", "hey", "wow", "hmm", "okay", "ok",
})
_SESSION: ContextVar[dict | None] = ContextVar("line_alignment_session", default=None)


@contextmanager
def alignment_session():
    """Reuse each pair within a corpus operation, then release all references.

    Nested callers share the outer operation. The ordinary bounded LRU remains
    sufficient for standalone lookups; batch term loops must not thrash it.
    """
    if _SESSION.get() is not None:
        yield
        return
    token = _SESSION.set(OrderedDict())
    try:
        yield
    finally:
        _SESSION.reset(token)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text)).casefold()


@lru_cache(maxsize=512)
def _wrapped_term_pattern(term: str):
    # Optional *line* wraps are allowed inside a word; ordinary spaces are
    # not silently removed. A space already present in the name accepts any
    # whitespace, including a subtitle wrap between the two words.
    pieces = []
    for piece in re.split(r"(\s+)", _fold(term).strip()):
        if piece.isspace():
            pieces.append(r"\s+")
        else:
            pieces.append(r"(?:[ \t]*\r?\n[ \t]*)?".join(re.escape(c) for c in piece))
    return re.compile("".join(pieces))


def _term_boundaries(text: str, start: int, end: int, needle: str) -> bool:
    left = text[start - 1] if start else ""
    right = text[end] if end < len(text) else ""
    if (needle[0].isascii() and needle[0].isalnum() and left.isascii()
            and (left.isalnum() or left == "_")):
        return False
    if (needle[-1].isascii() and needle[-1].isalnum() and right.isascii()
            and (right.isalnum() or right == "_")):
        return False
    if re.fullmatch(r"[ァ-ヶー]+", needle):
        # A soft wrap does not make a boundary inside a katakana compound.
        before = text[:start]
        after = text[end:]
        if re.search(r"\n[ \t]*$", before):
            left = before.rstrip()[-1:]
        if re.match(r"[ \t]*\n", after):
            right = after.lstrip()[:1]
        if re.fullmatch(r"[ァ-ヶー]", left) or re.fullmatch(r"[ァ-ヶー]", right):
            return False
    return True


def find_term_span(text: str, term: str) -> tuple[int, int] | None:
    """Return original-text offsets, preserving a known term across soft wraps."""
    needle = _fold(term).strip()
    if not needle:
        return None
    folded, starts, ends = [], [], []
    i = 0
    while i < len(text):
        end = i + 1
        while end < len(text) and (unicodedata.combining(text[end]) or text[end] in "ﾞﾟ"):
            end += 1
        value = _fold(text[i:end])
        folded.extend(value)
        starts.extend([i] * len(value))
        ends.extend([end] * len(value))
        i = end
    comparable = "".join(folded)
    for match in _wrapped_term_pattern(term).finditer(comparable):
        if match.end() > match.start() and _term_boundaries(
                comparable, match.start(), match.end(), needle):
            return starts[match.start()], ends[match.end() - 1]
    return None


def contains_term(text: str, term: str) -> bool:
    """NFKC/case-insensitive occurrence, with Latin word boundaries.

    CJK is unspaced, so requiring Unicode ``\b`` would discard its real hits.
    Latin boundaries, however, must reject ``Art`` in ``Party`` and ``SEKAI``
    in ``SEKAIPedia``. Spaces and punctuation are not silently deleted.
    """
    if "\n" in text or "\r" in text:
        comparable, needle = _fold(text), _fold(term).strip()
        return bool(needle) and any(_term_boundaries(comparable, m.start(), m.end(), needle)
                                    for m in _wrapped_term_pattern(term).finditer(comparable))
    haystack = re.sub(r"\s+", " ", _fold(text))
    needle = re.sub(r"\s+", " ", _fold(term)).strip()
    if not needle:
        return False
    start = 0
    while (at := haystack.find(needle, start)) >= 0:
        end = at + len(needle)
        if _term_boundaries(haystack, at, end, needle):
            return True
        start = at + 1
    return False


def select_translation(ranked: Sequence[tuple[str, float]]) -> str:
    """Reject unrelated near ties; extend only a supported nested name.

    A longer *different* co-occurring name is not a tiebreaker. Nested names
    may be competing tokenizations of the same surface, but ``Art`` is not a
    nested form of ``Party``. Scores must be finite and positive.
    """
    scores: dict[str, float] = {}
    for name, score in ranked:
        if name and math.isfinite(score) and score > 0:
            scores[name] = max(score, scores.get(name, 0.0))
    if not scores:
        return ""
    ordered = sorted(scores, key=lambda name: (-scores[name], -len(name), name))
    best = ordered[0]
    near = [name for name in ordered if scores[name] >= scores[best] * 0.80]
    for name in near:
        if not (contains_term(name, best) or contains_term(best, name)):
            return ""
    return max(near, key=lambda name: (len(name), scores[name], name))


@dataclass(frozen=True)
class LineAlignment:
    targets: tuple[tuple[int, ...], ...]
    confidences: tuple[float, ...]
    reasons: tuple[str, ...]

    def target_indices(self, source_index: int) -> tuple[int, ...]:
        if 0 <= source_index < len(self.targets):
            return self.targets[source_index]
        return ()

    def confidence(self, source_index: int) -> float:
        if 0 <= source_index < len(self.confidences):
            return self.confidences[source_index]
        return 0.0

    def reason(self, source_index: int) -> str:
        if 0 <= source_index < len(self.reasons):
            return self.reasons[source_index]
        return "out_of_range"


@dataclass(frozen=True)
class _Line:
    length: int
    speaker: str
    marks: frozenset[str]
    tokens: frozenset[str]
    numbers: frozenset[str]


def _number_tokens(text: str) -> frozenset[str]:
    """Keep quantity types: 100 percent is not the 100th year of a theatre."""
    tokens = set()
    unit_patterns = (
        ("percent", r"\s*(?:%|percent\b|퍼센트)"),
        ("age", r"\s*(?:歳|岁|살|years?\s+old\b)"),
        ("year", r"\s*(?:年|년|years?\b)"),
        ("month", r"\s*(?:月|개월|months?\b)"),
        ("day", r"\s*(?:日|天|일|days?\b)"),
        ("hour", r"\s*(?:時間|小时|小時|時|시간|hours?\b|hrs?\b)"),
        ("minute", r"\s*(?:分|분|minutes?\b|mins?\b)"),
        ("second", r"\s*(?:秒|초|seconds?\b|secs?\b)"),
        ("people", r"\s*(?:人|名|명|people\b|persons?\b)"),
        ("metre", r"\s*(?:メートル|米|公尺|미터|met(?:er|re)s?\b|m\b)"),
        ("kilometre", r"\s*(?:キロメートル|公里|千米|킬로미터|kilomet(?:er|re)s?\b|km\b)"),
        ("jpy", r"\s*(?:円|日元|日圓|엔)"),
        ("usd", r"\s*(?:美元|美金|달러|dollars?\b)"),
        ("ordinal", r"(?:st|nd|rd|th)\b"),
    )
    for match in _NUMBER.finditer(text):
        before, after = text[:match.start()], text[match.end():]
        # Alphanumeric names such as N25 are entity surfaces, not quantities.
        if before[-1:] and before[-1].isascii() and before[-1].isalpha():
            continue
        kind = "number"
        for candidate, pattern in unit_patterns:
            if re.match(pattern, after):
                kind = candidate
                break
        if kind == "number":
            if ":" in match.group():
                kind = "clock"
            elif before.rstrip().endswith("$"):
                kind = "usd"
            elif before.rstrip().endswith("¥"):
                kind = "jpy"
            elif before.rstrip().endswith(("第", "제")):
                kind = "ordinal"
            elif after[:1].isascii() and after[:1].isalpha():
                continue
        tokens.add(kind + ":" + match.group().replace(",", ""))
    return frozenset(tokens)


def _latin_anchor_tokens(text: str) -> frozenset[str]:
    """Use named surfaces, never a lower-case English homograph of a name.

    Preserve full capitalized runs and slash names so ``Live House`` is not
    reduced to the ordinary verb ``live`` and ``Leo/need`` stays one surface.
    These are entity clues, not guaranteed unique dialogue-event identities.
    """
    matches = list(_LATIN.finditer(text))
    tokens = set()
    i = 0
    while i < len(matches):
        match = matches[i]
        word = match.group()
        if not any(c.isupper() for c in word) or word.casefold() in _COMMON_LATIN:
            i += 1
            continue
        end, j = match.end(), i + 1
        while j < len(matches):
            following = matches[j]
            gap = text[end:following.start()]
            if not gap.isspace() or "\n" in gap or not following.group()[0].isupper():
                break
            if following.group().casefold() in _COMMON_LATIN:
                break
            end = following.end()
            j += 1
        tokens.add(text[match.start():end].casefold())
        i = j
    return frozenset(tokens)


def _speaker_body(line: str) -> tuple[str, str]:
    text = _fold(line).strip()
    colon = text.find(":", 0, 41)
    if 0 < colon < 40:
        prefix = text[:colon].strip()
        plain_prefix = re.sub(r"\b(?:mr|mrs|ms|dr|prof|sr|jr)\.", "", prefix)
        timestamp = bool(re.search(r"\b\d{1,2}$", prefix) and text[colon + 1:colon + 2].isdigit())
        if (not prefix.isdigit() and not timestamp and text[colon + 1:colon + 3] != "//"
                and not any(c in plain_prefix for c in ".!。！/;；")
                and ("?" not in prefix or set(prefix) == {"?"})):
            return prefix, text[colon + 1:].strip()
    return "", text


def strip_speaker_label(line: str) -> str:
    """Keep original body spelling; a speaker label is not a term occurrence."""
    if not _speaker_body(line)[0]:
        return line
    # Speaker recognition uses NFKC; find that separator in raw coordinates.
    colon = next((i for i, char in enumerate(line)
                  if unicodedata.normalize("NFKC", char) == ":"), -1)
    return line[colon + 1:].lstrip() if colon >= 0 else line


def _feature(line: str) -> _Line:
    # Bounded prefix scan: unlike nested speaker regexes this is linear even
    # for long, punctuation-free dialogue. Do not eat a URL or sentence colon.
    speaker, text = _speaker_body(line)
    original_body = unicodedata.normalize("NFKC", strip_speaker_label(line))
    numbers = _number_tokens(text)
    tokens = _latin_anchor_tokens(original_body)
    marks = frozenset(mark for mark, choices in
                      (("?", "?？"), ("!", "!！"), ("…", "…"), ("quote", '「」『』“”"'))
                      if any(c in text for c in choices))
    return _Line(max(1, sum(c.isalnum() for c in text)), speaker, marks,
                 tokens | frozenset("#" + n for n in numbers), numbers)


def _anchor_pairs(source: Sequence[_Line], target: Sequence[_Line]) -> set[tuple[int, int]]:
    left: dict[str, list[int]] = defaultdict(list)
    right: dict[str, list[int]] = defaultdict(list)
    for i, line in enumerate(source):
        for token in line.tokens:
            left[token].append(i)
    for j, line in enumerate(target):
        for token in line.tokens:
            right[token].append(j)
    source_speakers = tuple(line.speaker for line in source)
    target_speakers = tuple(line.speaker for line in target)
    speaker_map = (dict(zip(source_speakers, target_speakers))
                   if _compatible_speakers(source_speakers, target_speakers) else {})
    pairs = set()
    for token, indices in left.items():
        if len(indices) != 1 or len(right.get(token, ())) != 1:
            continue
        si, ti = indices[0], right[token][0]
        # Localization may name an entity once but at a different mention
        # than another language. A sole Latin surface is not a unique event.
        # Use independently recurring speaker identities to reject that clue;
        # typed numeric anchors still expose reordered or conflicting turns.
        if (not token.startswith("#") and speaker_map
                and speaker_map.get(source[si].speaker) != target[ti].speaker):
            continue
        pairs.add((si, ti))
    return pairs


def _anchors(source: Sequence[_Line], target: Sequence[_Line]) -> dict[int, int]:
    """Unique shared tokens followed by a longest monotone anchor chain."""
    pairs = _anchor_pairs(source, target)
    source_counts = Counter(i for i, _ in pairs)
    target_counts = Counter(j for _, j in pairs)
    pairs = sorted((i, j) for i, j in pairs
                   if source_counts[i] == 1 and target_counts[j] == 1)
    tails: list[int] = []
    tail_indices: list[int] = []
    previous: list[int] = []
    for idx, (_, j) in enumerate(pairs):
        at = bisect_left(tails, j)
        previous.append(tail_indices[at - 1] if at else -1)
        if at == len(tails):
            tails.append(j)
            tail_indices.append(idx)
        else:
            tails[at], tail_indices[at] = j, idx
    result: dict[int, int] = {}
    idx = tail_indices[-1] if tail_indices else -1
    while idx >= 0:
        i, j = pairs[idx]
        result[i] = j
        idx = previous[idx]
    return result


def _empty(n: int, reason: str) -> LineAlignment:
    return LineAlignment(((),) * n, (0.0,) * n, (reason,) * n)


@lru_cache(maxsize=512)
def _line_signature(lines: tuple[str, ...]) -> bytes:
    digest = hashlib.sha256()
    for line in lines:
        encoded = line.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.digest()


def align_lines(source_lines: Sequence[str], target_lines: Sequence[str],
                source_language: str = "", target_language: str = "") -> LineAlignment:
    """Align indices of the supplied nonempty lines without renumbering them.

    At most 4,096 lines per page and 180,000 DP cells are considered. Oversize
    or hopelessly skewed pages return no evidence; they never trigger an
    unbounded Cartesian scan. The cache is content-keyed and size-bounded.
    """
    if not source_lines or not target_lines:
        return _empty(len(source_lines), "missing_page")
    if max(len(source_lines), len(target_lines)) > _MAX_LINES:
        return _empty(len(source_lines), "alignment_budget")
    source, target = tuple(source_lines), tuple(target_lines)
    session = _SESSION.get()
    if session is not None:
        key = (_line_signature(source), _line_signature(target), source_language, target_language)
        if key in session:
            session.move_to_end(key)
            return session[key]
    result = _align_dialogue_cached(source, target, source_language, target_language)
    if session is not None:
        session[key] = result
        if len(session) > _SESSION_MAX_PAIRS:
            session.popitem(last=False)
    return result


def _turns(lines: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[tuple[int, ...], ...], tuple[str, ...]]:
    """Recover TalkData turns: Body newlines do not start a new utterance."""
    chunks: list[list[str]] = []
    indices: list[list[int]] = []
    speakers: list[str] = []
    for i, line in enumerate(lines):
        speaker, _body = _speaker_body(line)
        if speaker or not chunks:
            chunks.append([line])
            indices.append([i])
            speakers.append(speaker)
        else:
            chunks[-1].append(line)
            indices[-1].append(i)
    return (tuple(" ".join(chunk) for chunk in chunks),
            tuple(tuple(chunk) for chunk in indices), tuple(speakers))


@lru_cache(maxsize=1024)
def dialogue_spans(lines: tuple[str, ...]) -> tuple[tuple[tuple[int, ...], str], ...]:
    """Recover raw-line groups and original body text without speaker labels."""
    _text, indices, speakers = _turns(lines)
    if not any(speakers):
        indices = tuple((i,) for i in range(len(lines)))
    return tuple((group, "\n".join(strip_speaker_label(lines[i]) for i in group))
                 for group in indices)


def source_term_occurrences(lines: Sequence[str], term: str) -> dict[int, str]:
    """Representative source line -> complete raw turn, for body occurrences."""
    result = {}
    for indices, body in dialogue_spans(tuple(lines)):
        if not contains_term(body, term):
            continue
        complete = "\n".join(lines[i] for i in indices)
        hits = [i for i in indices if contains_term(strip_speaker_label(lines[i]), term)]
        for i in hits or (indices[0],):
            result[i] = complete
    return result


def _compatible_speakers(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if len(left) != len(right) or not all(left) or not all(right):
        return False
    forward, reverse = {}, {}
    left_labels, right_labels = set(left), set(right)
    for source, target in zip(left, right):
        # Shared labels are available identity anchors, not arbitrary symbols
        # that can be permuted to manufacture a matching recurrence pattern.
        if source in right_labels and source != target:
            return False
        if target in left_labels and source != target:
            return False
        if source in forward and forward[source] != target:
            return False
        if target in reverse and reverse[target] != source:
            return False
        forward[source], reverse[target] = target, source
    # With one appearance per speaker, *any* same-length sequence is a
    # bijection. Require two recurring identities to expose a real pattern;
    # short pages and all-unique labels use the ordinary alignment checks.
    counts = Counter(left)
    return sum(count >= 2 for count in counts.values()) >= 2


@lru_cache(maxsize=256)
def _align_dialogue_cached(source: tuple[str, ...], target: tuple[str, ...],
                           source_language: str, target_language: str) -> LineAlignment:
    source_turns, source_indices, source_speakers = _turns(source)
    target_turns, target_indices, target_speakers = _turns(target)
    # Only reconstruct continuations when both pages expose real speaker
    # labels. Plain prose must retain its independently supplied line breaks.
    use_turns = any(source_speakers) and any(target_speakers)
    if not use_turns:
        source_turns, target_turns = source, target
        source_indices = tuple((i,) for i in range(len(source)))
        target_indices = tuple((i,) for i in range(len(target)))
    pairs = _anchor_pairs(tuple(_feature(line) for line in source_turns),
                          tuple(_feature(line) for line in target_turns))
    if use_turns and _compatible_speakers(source_speakers, target_speakers):
        if all(si == ti for si, ti in pairs):
            targets = [()] * len(source)
            for si, indices in enumerate(source_indices):
                for line_index in indices:
                    targets[line_index] = target_indices[si]
            # Structural consistency is not a calibrated semantic probability.
            return LineAlignment(tuple(targets), (0.85,) * len(source),
                                 ("speaker_turn_sequence",) * len(source))
    forward = _align_cached(source_turns, target_turns, source_language, target_language)
    reverse = _align_cached(target_turns, source_turns, target_language, source_language)
    source_anchors: dict[int, set[int]] = defaultdict(set)
    target_anchors: dict[int, set[int]] = defaultdict(set)
    for si, ti in pairs:
        source_anchors[si].add(ti)
        target_anchors[ti].add(si)
    targets, confidences, reasons = [], [], []
    for si, indices in enumerate(forward.targets):
        anchor_conflict = bool(indices) and (
            not source_anchors.get(si, set()) <= set(indices)
            or any(not target_anchors.get(ti, set()) <= set(reverse.target_indices(ti))
                   for ti in indices))
        if anchor_conflict:
            targets.append(())
            confidences.append(0.0)
            reasons.append("conflicting_anchor")
        elif indices and all(si in reverse.target_indices(ti) for ti in indices):
            targets.append(indices)
            confidences.append(min(forward.confidence(si),
                                   *(reverse.confidence(ti) for ti in indices)))
            reasons.append(forward.reason(si))
        else:
            targets.append(())
            confidences.append(0.0)
            reasons.append("nonreciprocal_alignment" if indices else forward.reason(si))
    expanded_targets = [()] * len(source)
    expanded_confidences = [0.0] * len(source)
    expanded_reasons = ["ambiguous_alignment"] * len(source)
    for si, indices in enumerate(source_indices):
        for line_index in indices:
            expanded_targets[line_index] = tuple(ti for turn in targets[si]
                                                   for ti in target_indices[turn])
            expanded_confidences[line_index] = confidences[si]
            expanded_reasons[line_index] = reasons[si]
    return LineAlignment(tuple(expanded_targets), tuple(expanded_confidences), tuple(expanded_reasons))


@lru_cache(maxsize=256)
def _align_cached(source_lines: tuple[str, ...], target_lines: tuple[str, ...],
                  source_language: str, target_language: str) -> LineAlignment:
    n, m = len(source_lines), len(target_lines)
    if max(n, m) > 4 * min(n, m) + 4:
        return _empty(n, "incompatible_page_structure")
    source = tuple(_feature(line) for line in source_lines)
    target = tuple(_feature(line) for line in target_lines)
    anchors = _anchors(source, target)
    reverse_anchors = {j: i for i, j in anchors.items()}
    # Anchor interpolation keeps a late insertion from moving every earlier
    # line. The outer band bounds time/memory rather than supplying evidence.
    points = [(0, 0)] + sorted((i + 1, j + 1) for i, j in anchors.items()
                               if i + 1 < n and j + 1 < m) + [(n, m)]
    bounds: list[tuple[int, int]] = []
    point = 0
    for i in range(n + 1):
        while point + 1 < len(points) - 1 and i > points[point + 1][0]:
            point += 1
        a, b = points[point], points[point + 1]
        center = a[1] + (i - a[0]) * (b[1] - a[1]) / max(1, b[0] - a[0])
        bounds.append((max(0, math.floor(center) - _BAND),
                       min(m, math.ceil(center) + _BAND)))
    if sum(hi - lo + 1 for lo, hi in bounds) > _MAX_CELLS:
        return _empty(n, "alignment_budget")
    ratio = sum(line.length for line in target) / max(1, sum(line.length for line in source))
    ratio = min(5.0, max(0.2, ratio))
    shared_speakers = {line.speaker for line in source if line.speaker} & {
        line.speaker for line in target if line.speaker}

    @lru_cache(maxsize=None)
    def cost(i: int, j: int, a: int, b: int) -> float:
        if not a:
            return 1.6 + (5.0 if j in reverse_anchors else 0.0)
        if not b:
            return 1.6 + (5.0 if i in anchors else 0.0)
        ls, lt = source[i:i + a], target[j:j + b]
        # A split subtitle may repeat its speaker or omit the second label.
        # A merge across different speakers is not the same dialogue turn.
        if ((a > 1 and len({line.speaker for line in ls if line.speaker}) > 1)
                or (b > 1 and len({line.speaker for line in lt if line.speaker}) > 1)):
            return 20.0
        expected = sum(line.length for line in ls) * ratio
        actual = sum(line.length for line in lt)
        value = 0.12 + 1.35 * abs(math.log((actual + 5) / (expected + 5)))
        value += 0.62 * (a + b - 2)
        sm = frozenset().union(*(line.marks for line in ls))
        tm = frozenset().union(*(line.marks for line in lt))
        value += sum(weight for mark, weight in (("?", 0.85), ("!", 0.50), ("…", 0.25), ("quote", 0.18))
                     if (mark in sm) != (mark in tm))
        sn = frozenset().union(*(line.numbers for line in ls))
        tn = frozenset().union(*(line.numbers for line in lt))
        if sn and tn:
            value += 2.0 if not (sn & tn) else -0.35
        ss, ts = ls[0].speaker, lt[0].speaker
        if ss and ts and ss == ts:
            value -= 0.3
        elif ss and ts and (ss in shared_speakers or ts in shared_speakers):
            value += 6.0
        # Shared numeric / Latin anchors constrain the actual correspondence,
        # not merely the search window. Omissions remain possible at a cost.
        for si in range(i, i + a):
            if si in anchors:
                value += -1.6 if j <= anchors[si] < j + b else 6.0
        for ti in range(j, j + b):
            if ti in reverse_anchors and not i <= reverse_anchors[ti] < i + a:
                value += 6.0
        return value

    # Sparse rows make the worst-case cost explicit. Same-row insertions are
    # visited left-to-right; all other transitions advance the source index.
    forward: list[dict[int, float]] = [{} for _ in range(n + 1)]
    forward[0][0] = 0.0
    backtrace: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for i in range(n + 1):
        for j in range(bounds[i][0], bounds[i][1] + 1):
            base = forward[i].get(j)
            if base is None:
                continue
            for a, b in _STEPS:
                ni, nj = i + a, j + b
                if ni > n or nj > m or not bounds[ni][0] <= nj <= bounds[ni][1]:
                    continue
                candidate = base + cost(i, j, a, b)
                if candidate < forward[ni].get(nj, math.inf) - 1e-12:
                    forward[ni][nj] = candidate
                    backtrace[(ni, nj)] = (i, j, a, b)
    best = forward[n].get(m)
    if best is None:
        return _empty(n, "alignment_budget")
    backward: list[dict[int, float]] = [{} for _ in range(n + 1)]
    backward[n][m] = 0.0
    for i in range(n, -1, -1):
        for j in range(bounds[i][1], bounds[i][0] - 1, -1):
            for a, b in _STEPS:
                ni, nj = i + a, j + b
                if ni > n or nj > m:
                    continue
                tail = backward[ni].get(nj)
                if tail is not None:
                    value = cost(i, j, a, b) + tail
                    if value < backward[i].get(j, math.inf):
                        backward[i][j] = value
    selected: dict[int, tuple[int, ...]] = {}
    local_costs: dict[int, float] = {}
    i, j = n, m
    while (i, j) != (0, 0):
        pi, pj, a, b = backtrace[(i, j)]
        for si in range(pi, pi + a):
            selected[si] = tuple(range(pj, pj + b))
            local_costs[si] = cost(pi, pj, a, b)
        i, j = pi, pj
    alternative = [math.inf] * n
    for i in range(n):
        for j, head in forward[i].items():
            for a, b in _STEPS:
                ni, nj = i + a, j + b
                if not a or ni > n or nj > m:
                    continue
                tail = backward[ni].get(nj)
                if tail is None:
                    continue
                indices = tuple(range(j, nj))
                value = head + cost(i, j, a, b) + tail
                for si in range(i, ni):
                    if indices != selected.get(si, ()):
                        alternative[si] = min(alternative[si], value)
    targets: list[tuple[int, ...]] = []
    confidences: list[float] = []
    reasons: list[str] = []
    for si in range(n):
        indices = selected.get(si, ())
        margin = alternative[si] - best
        anchored = si in anchors and anchors[si] in indices
        if not indices:
            confidence, reason = 0.0, "omitted_line"
        elif margin < 0.18 and not anchored:
            indices, confidence, reason = (), 0.0, "ambiguous_alignment"
        elif local_costs.get(si, math.inf) > 2.4 and not anchored:
            indices, confidence, reason = (), 0.0, "weak_structure"
        else:
            confidence = min(0.90 if anchored else 0.78,
                             0.62 + max(0.0, margin) * 0.10)
            reason = "shared_anchor" if anchored else "monotone_structure"
        targets.append(indices)
        confidences.append(round(confidence, 4))
        reasons.append(reason)
    return LineAlignment(tuple(targets), tuple(confidences), tuple(reasons))
