"""术语穿透的分层通道（术语刮削管线重构的集成层）。

上游模块已分别就位，本模块只负责**把它们串成一条可用的管线**，不重新实现
任何判定逻辑：

* ``candidate_tiers``（第 0 层）：候选分层 —— 通用词在这里被拦下。
* ``romaji``（通道 C 的门控）：片假名→罗马音相似度硬门控。
* ``termindex``（通道 A）：既有分布对齐（Rapp/Fung & Yee），原样保留。
* ``zhfirst`` / ``pilot.english_proper_candidates``（通道 B）：拉丁形态直取。

设计依据（``experiment/zh-en-tw/COMPARISON.md`` 三组对照实验）：

1. 三组的噪声同源 —— 通用词（大家/咖啡/テスト/クラス）没有专属译名，行位
   预测只能把它们落到任意感叹词/人名上，产出 ``大家→Thank`` 这类垃圾。
   修法不是改对齐算法，而是**对齐前先分层过滤**（第 0 层）。
2. 三组是互补信号而非互斥替代：组 1 精度高但漏（78 对），组 2 能抓人名对
   （朝比奈→Asahina），组 3 能抓缩写对（ニーゴ→N25）且覆盖率最高。
   因此第 1 层三通道并行、互不污染，第 2 层再裁决。
3. 繁中验证机制设计错误：组 2 用"简中词在繁中出现"做验证，简繁同形使信号
   几乎恒真。正确做法是**译名层面的同点位闭环**（见 ``verify_triangle``）。

硬约束：零第三方依赖（只用标准库）；不修改任何既有文件；Python 3.10+。

性能设计（全量 13,428 故事规模可跑完）：所有候选→故事的定位走**一次语料
扫描**建索引（``_build_term_line_index``：按首字分组的 C 级 ``startswith``
定位），每个 (候选, 故事) 只做常数次 ``startswith``，避免
``O(词数 × 故事数 × 全文扫描)`` 的退化——这正是 ``build_pair_story_index``
在 termindex 里解决的同一类问题。
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable

from sekaisync import termindex
from sekaisync.candidate_tiers import Tier, classify, tier_summary
from sekaisync.normalize import normalize_name
from sekaisync.romaji import (
    is_generic_katakana,
    is_plausible_translation,
    similarity,
)
from sekaisync.zhfirst import strip_speaker

__all__ = [
    "extract_latin_candidates",
    "channel_c_katakana_to_english",
    "verify_triangle",
    "penetrate_layered",
    "channel_stats_summary",
    # 集成入口：三角闭环的 aux 名册与对齐上下文（签名外的新增 API）。
    "register_known_names",
    "register_glossary_names",
    "register_alignment_context",
    "clear_known_names",
]


# ── 通用工具 ────────────────────────────────────────────────────────

# 片假名字符（含长音符/中黑点），与 termindex 的片假名口径一致。
_KATAKANA_CHAR_RE = re.compile(r"[\u30A0-\u30FF\u30FC\u30FB]")
_LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
_HAS_LATIN_RE = re.compile(r"[A-Za-z]")


def _is_pure_katakana(term: str) -> bool:
    """整串都是片假名（含 ー ・）且长度 ≥3：这是通道 C 的作用域。

    长度下限与 termindex/experiment 的片假名抽取口径一致（``{2,40}`` 后缀
    → 总长 ≥3），避免把 2 字的短外来语（カラ等）当作专名候选。
    """
    t = unicodedata.normalize("NFKC", (term or "").strip())
    if len(t) < 3:
        return False
    return all(
        0x30A1 <= ord(ch) <= 0x30FA or ch in "\u30FC\u30FB" for ch in t
    ) and _KATAKANA_CHAR_RE.search(t) is not None


def _has_latin(term: str) -> bool:
    return bool(_HAS_LATIN_RE.search(term or ""))


def _line_offsets(text: str) -> tuple[list[str], list[int]]:
    """返回 (非空行列表, 每行在原文中的起始偏移)。

    行号按"去空行后的序号"计（与 termindex 的行位预测口径一致）；偏移保留
    供需要字符位置的调用方使用。
    """
    lines: list[str] = []
    starts: list[int] = []
    offset = 0
    for raw in text.splitlines(True):
        stripped = raw.strip()
        if stripped:
            lines.append(stripped)
            starts.append(offset)
        offset += len(raw)
    return lines, starts


# ── 通道 B 的形态过滤器（复用 pilot.english_proper_candidates 的逻辑）──

# 功能词表：与 experiment/zh-en-tw/pilot.py 的 _EN_STOPWORDS 一致。
# pilot.py 位于带连字符的目录（experiment/zh-en-tw），不是合法包名，
# 无法 import，故按其逻辑复制实现（含后续两处加固）。
_EN_STOPWORDS = frozenset(
    "the a an and or but of to in on at for with by from as is are was were "
    "be been being it its this that these those i you he she we they my your "
    "our their his her me him them us not no yes so if then than what who how "
    "why when where do does did done have has had will would can could should "
    "may might must about into over under out up down off again very just "
    "there here now well okay yeah hmm ah oh wow hey even still also always "
    "really maybe sure right wrong good bad new old one two three"
    .split()
)

_EN_PROPER = re.compile(
    r"\b[A-Z][a-zA-Z0-9'&.\-]*(?:\s+(?:of|the|at|&|de|la)?\s*[A-Z][a-zA-Z0-9'&.\-]*)*\b"
)

# 句首大写歧义词：These/Then/There/This 之类在句首天然大写，单条出现时
# 与专名形态不可区分（只有第二个大写词才能佐证）。命中此表的单词条直接丢弃。
_EN_SENTENCE_INITIAL_AMBIGUOUS = frozenset(
    "there these those they their them then than that this the a an and but if "
    "when where what who why how she he it its his her our your my we you i "
    "no not now here well so even still also just very really maybe sure "
    "thank thanks please sorry okay ok wow yeah yes hey hi hello"
    .split()
)


def extract_latin_candidates(line: str) -> list[str]:
    """从一行文本提取拉丁专名形态候选。

    规则与 ``experiment/zh-en-tw/pilot.py`` 的 ``english_proper_candidates``
    一致（该目录名含连字符，不可 import，故复制实现），并补两条实验报告
    点名的排除项：

    * 排除**缩写/缩约** —— 含撇号（``I'm`` / ``You're``）的 token 一律不
      采纳：撇号前的代词会因首字母大写被误判成专名；
    * 排除**句首大写歧义词** —— 单词条且位于句首（或句间句首）时，若其
      小写形在 ``_EN_SENTENCE_INITIAL_AMBIGUOUS`` 内则丢弃（``There``）；
      全大写（``SEKAI``）或含内部大写（``KaTTo``）是强专名形态，不受此限。

    返回原串（未归一化），保持出现顺序并去重。空行/无候选返回 ``[]``。
    """
    if not line:
        return []
    text = strip_speaker(line)
    if not text.strip():
        return []
    out: list[str] = []
    seen: set[str] = set()
    for m in _EN_PROPER.finditer(text):
        w = m.group(0).strip()
        words = w.split()
        if len(w) < 3 or len(words) > 6:
            continue
        # 缩写（I'm / You're / Don't）与所有格：撇号是句法标记，不是名字。
        if any("'" in x or "\u2019" in x for x in words):
            continue
        content = [x for x in words if x.lower() not in _EN_STOPWORDS]
        if not content:
            continue
        if not any(x[0].isupper() and len(x) >= 3 for x in content):
            continue
        if len(words) == 1:
            word = words[0]
            strong = word.isupper() or any(c.isupper() for c in word[1:])
            if not strong:
                if word.lower() in _EN_SENTENCE_INITIAL_AMBIGUOUS:
                    continue
                # 句首/句间句首的普通首字母大写词：无第二词佐证 → 丢弃。
                head = text[: m.start()].rstrip()
                if not head or head[-1] in ".!?\u2026\u2014":
                    continue
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _latin_candidates_for_line(line: str) -> list[str]:
    """带缓存的 extract_latin_candidates（对话行重复率高，缓存收益明显）。"""
    cached = _LATIN_LINE_CACHE.get(line)
    if cached is None:
        cached = extract_latin_candidates(line)
        if len(_LATIN_LINE_CACHE) < 200_000:
            _LATIN_LINE_CACHE[line] = cached
    return cached


_LATIN_LINE_CACHE: dict[str, list[str]] = {}


# ── 语料索引（一次扫描，供三通道复用）──────────────────────────────

class _TermStoryIndex:
    """候选 → {story_key: 行号集合} 的一次性索引。

    朴素做法（termindex 每处调用点的默认路径）对每个候选重新遍历全部故事
    的全文并做 ``in`` 判断，在 900+ 候选 × 13k 故事上是 1200 万次全文扫描。
    这里改成：扫描每个故事一次，用「按首字符分桶的候选集合 + 逐位置
    ``startswith``」定位命中，复杂度降到 O(正文长度 × 候选首字分布)，
    且天然拿到**行号**（行位预测必需，纯 ``in`` 拿不到）。
    """

    __slots__ = ("lines", "starts", "hits")

    def __init__(self) -> None:
        # story_key -> 非空行列表 / 行偏移 / {term: {行号}}
        self.lines: dict[str, list[str]] = {}
        self.starts: dict[str, list[int]] = {}
        self.hits: dict[str, dict[str, set[int]]] = {}


def _build_term_line_index(
    groups: dict,
    stories: Iterable[str],
    language: str,
    terms: Iterable[str],
) -> _TermStoryIndex:
    """为 ``terms`` 建立 (term → {story: 行号}) 索引。单次语料扫描。"""
    index = _TermStoryIndex()
    wanted = [t for t in terms if t]
    if not wanted:
        return index
    # 前两字符分桶：只有前缀相同的候选才参与逐位置 startswith 比较。
    # 单字符分桶在候选上万时（30k 片假名串 / 40 个首字 ≈ 750 条同桶）
    # 会让每个正文位置白跑几百次比较；两字符把桶压到 O(1) 量级。
    by_prefix: dict[str, list[str]] = defaultdict(list)
    for term in wanted:
        by_prefix[term[:2]].append(term)
        index.hits.setdefault(term, {})

    for story_key in stories:
        page = _page_for(groups, story_key, language)
        if page is None:
            continue
        text = str(page.get("text", ""))
        if not text:
            continue
        lines, starts = _line_offsets(text)
        index.lines[story_key] = lines
        index.starts[story_key] = starts
        if not lines:
            continue
        hits: dict[str, set[int]] = {}
        # 逐行扫描：行内逐位置比对。行是最小检索单位，行号直接可得。
        for line_no, line in enumerate(lines):
            if len(line) < 2:
                continue
            for pos in range(len(line) - 1):
                bucket = by_prefix.get(line[pos:pos + 2])
                if not bucket:
                    continue
                for term in bucket:
                    if line.startswith(term, pos):
                        bucket_hits = hits.get(term)
                        if bucket_hits is None:
                            hits[term] = {line_no}
                        else:
                            bucket_hits.add(line_no)
        for term, line_nos in hits.items():
            index.hits[term][story_key] = line_nos
    return index


def _predict_line(src_idx: int, src_n: int, tgt_n: int) -> int:
    """行位预测：按行数比例把源行号映射到目标行号。"""
    if src_n <= 1 or tgt_n <= 1:
        return 0
    return round(src_idx * (tgt_n - 1) / (src_n - 1))


def _paired_stories(groups: dict, language: str, source_language: str) -> set[str]:
    """同时具备源语言与目标语言的故事（无对应语页的故事只会产生噪声）。

    走缓存的语言索引表，避免每个 (story × lang) 一次字典遍历 + 别名回退。
    """
    index = _lang_page_index(groups)
    src_key = termindex._term_language(source_language)
    tgt_key = termindex._term_language(language)
    out: set[str] = set()
    for sk, row in index.items():
        if row.get(src_key) is not None and row.get(tgt_key) is not None:
            out.add(sk)
    return out


# story_key -> (正文, 非空行)。同一 groups 对象只解一次（verify_triangle 会被
# 反复调用，每调用重扫全语料不可接受）。按 id() 与规模双重校验，避免对象被
# 回收后地址复用导致的错配。
_TEXT_CACHE: dict[int, tuple[int, dict[str, tuple[str, list[str]]]]] = {}

# (groups_id, lang) -> {story_key: page}，以及 groups_id -> {story: {lang: page}}。
# 用于把 `_group_page` 的别名回退循环（每个 story×lang 一次字典遍历）降为 O(1)
# 直接命中——verify_triangle 每次调用都要按语言取页，不缓存会退化。
_PAGE_CACHE: dict[tuple[int, str], dict[str, Any]] = {}
_LANG_INDEX_CACHE: dict[int, tuple[int, dict[str, dict[str, Any]]]] = {}


def _lang_page_index(groups: dict) -> dict[str, dict[str, Any]]:
    """groups → {story_key: {language: page}}（语言名已按别名归一）。"""
    entry = _LANG_INDEX_CACHE.get(id(groups))
    if entry is not None and entry[0] == len(groups):
        return entry[1]
    built: dict[str, dict[str, Any]] = {}
    for sk, by in groups.items():
        row: dict[str, Any] = {}
        for lang, page in (by or {}).items():
            if page is None:
                continue
            key = termindex._term_language(str(lang))
            # 同一语言位重复时保留先到的（与 _group_page 的先命中语义一致）。
            row.setdefault(key, page)
        if row:
            built[sk] = row
    if len(_LANG_INDEX_CACHE) > 4:
        _LANG_INDEX_CACHE.clear()
        _PAGE_CACHE.clear()
    _LANG_INDEX_CACHE[id(groups)] = (len(groups), built)
    return built


def _page_for(groups: dict, story_key: str, language: str) -> Any:
    """带缓存的按语言取页（等价 ``termindex._group_page``）。"""
    lang_key = termindex._term_language(language)
    cache_key = (id(groups), lang_key)
    table = _PAGE_CACHE.get(cache_key)
    if table is None:
        index = _lang_page_index(groups)
        table = {sk: row.get(lang_key) for sk, row in index.items()}
        # 别名位（zh_hans 等）可能拼法不同，逐个回退一次并记录。
        missing = [sk for sk, page in table.items() if page is None]
        if missing:
            for sk in missing:
                table[sk] = termindex._group_page(groups.get(sk, {}), language)
        if len(_PAGE_CACHE) > 16:
            _PAGE_CACHE.clear()
        _PAGE_CACHE[cache_key] = table
    return table.get(story_key)


def _story_text_cache(
    groups: dict, language: str = "ja"
) -> dict[str, tuple[str, list[str]]]:
    """{story: (正文, 非空行)}，按 (groups, 语言) 缓存。"""
    cache_key = id(groups)
    entry = _TEXT_CACHE.get(cache_key)
    if entry is not None and entry[0] == len(groups) and entry[1] == language:
        return entry[2]
    built: dict[str, tuple[str, list[str]]] = {}
    for sk in groups:
        page = _page_for(groups, sk, language)
        text = str(page.get("text", "")) if page is not None else ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        built[sk] = (text, lines)
    if len(_TEXT_CACHE) > 4:  # 长驻进程下防止缓存无限增长
        _TEXT_CACHE.clear()
    _TEXT_CACHE[cache_key] = (len(groups), language, built)
    return built


def _locate_term_lines(
    groups: dict,
    language: str,
    term: str,
    max_stories: int | None = None,
    story_scope: Iterable[str] | None = None,
) -> dict[str, set[int]]:
    """单术语 → {story: 行号}（用缓存正文做逐行 ``in``）。

    只服务少量术语（verify_triangle 每次调用的规模），因此不复用
    ``_build_term_line_index`` 的逐位置索引——后者按候选集规模付费，
    而这里术语只有个位数，整页扫一遍更便宜。``story_scope`` 限定候选故事
    集合（配对故事），避免遍历源语言里没有对应语的全量故事。
    """
    out: dict[str, set[int]] = {}
    if not term:
        return out
    keys = story_scope if story_scope is not None else groups.keys()
    texts = (
        _story_text_cache(groups, language)
        if story_scope is not None
        else None
    )
    for sk in keys:
        if texts is not None:
            text, lines = texts.get(sk, ("", []))
        else:
            page = _page_for(groups, sk, language)
            if page is None:
                continue
            text = str(page.get("text", ""))
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if not text or term not in text:
            continue
        hit = {i for i, ln in enumerate(lines) if term in ln}
        if hit:
            out[sk] = hit
            if max_stories is not None and len(out) >= max_stories:
                break
    return out


# ── 通道 C：片假名 → 英语（外来语音译穿透）────────────────────────────

def channel_c_katakana_to_english(
    groups: dict,
    stories: list[str],
    katakana_terms: set[str],
    *,
    min_stories: int = 2,
    min_ratio: float = 0.25,
    sim_threshold: float = 0.5,
    source_language: str = "ja",
    target_language: str = "en",
) -> dict[str, str]:
    """通道 C：片假名→英语穿透（外来语音译优先）。

    对每个片假名术语：在其出现的每个故事里，用行位预测取英语候选，
    跨故事投票（≥min_stories 且覆盖 ≥min_ratio），
    最后用 ``romaji.is_plausible_translation`` 硬门控（sim>=threshold）过滤。
    返回 {katakana_term: english}，只含通过门控的高置信对。

    门控为什么是硬门控：实验里真穿透（セカイ/カイト/イオリ）相似度恒为
    1.00（英语外来语的直接音译），噪声（テスト→Huh / クラス→Hehe /
    バタバタ→Thank）≤0.33，而通用词已在第 0 层被拦掉，所以这里可以放严。

    实现要点（避免 O(词×故事×全文)）：``_build_term_line_index`` 一次扫描
    建索引；英语侧只取"该术语出现行的预测对应行 ±1"并缓存行级候选。
    """
    source_language = source_language or "ja"
    target_language = target_language or "en"
    # 只保留真正的片假名串：汉字/拉丁候选走通道 A/B，不该进这里。
    terms = sorted(t for t in (katakana_terms or set()) if _is_pure_katakana(t))
    if not terms or not stories:
        return {}

    story_list = [sk for sk in stories if sk in groups]
    paired = _paired_stories(groups, target_language, source_language)
    eligible = [sk for sk in story_list if sk in paired]
    if not eligible:
        return {}

    src_index = _build_term_line_index(groups, eligible, source_language, terms)

    # 目标语言行缓存（同一行会被多个术语的预测窗口反复取用）。
    tgt_lines: dict[str, list[str]] = {}
    for sk in eligible:
        page = _page_for(groups, sk, target_language)
        if page is None:
            continue
        tgt_lines[sk] = [
            ln.strip() for ln in str(page.get("text", "")).splitlines() if ln.strip()
        ]

    out: dict[str, str] = {}
    for term in terms:
        stories_by_term = src_index.hits.get(term) or {}
        n_stories = len(stories_by_term)
        if n_stories < min_stories:
            continue
        # 证据：en 候选 → 命中的故事集合（跨故事投票的票数即故事数）
        votes: dict[str, set[str]] = defaultdict(set)
        for sk, line_nos in stories_by_term.items():
            el = tgt_lines.get(sk)
            if not el:
                continue
            jl = src_index.lines.get(sk) or []
            if not jl:
                continue
            for si in sorted(line_nos)[:3]:  # 同一术语每故事最多取 3 行，防长尾拖慢
                pred = _predict_line(si, len(jl), len(el))
                for ti in (pred, pred - 1, pred + 1):
                    if 0 <= ti < len(el):
                        for cand in _latin_candidates_for_line(el[ti]):
                            votes[cand].add(sk)
        if not votes:
            continue
        # 先按 (投票故事数, 候选长度, sim) 排序，再逐个过门控：这样能选到
        # 票数最高且通过门控的候选，而不是"先门控再选票数"（后者会让
        # 高票噪声与低票真值之间的比较失真）。
        best: tuple[float, int, int, str] | None = None
        for cand, sk_set in votes.items():
            n = len(sk_set)
            if n < min_stories:
                continue
            if n / n_stories < min_ratio:
                continue
            if not is_plausible_translation(term, cand, sim_threshold):
                continue
            sim = similarity(term, cand)
            key = (sim, n, len(cand), cand)
            if best is None or key > best:
                best = key
        if best is not None:
            out[term] = best[3]
    return out


# ── 第 2 层：三角闭环验证 ──────────────────────────────────────────

# term(normalize) -> {language: 已知名}。由调用方 register_known_names 或
# penetrate_layered 从 glossary 灌入（"需调用方提供 term 在各语言的已知名"）。
_KNOWN_NAMES: dict[str, dict[str, str]] = {}

# 反查用对齐上下文（可选）：penetrate_layered 拿到 idf/vocab 时注册，
# verify_triangle 在既无名册也无字面命中时用它做 align_term_by_frequency 反查。
_ALIGN_CONTEXT: dict[str, Any] = {}

# 反查结果缓存（对齐很贵，同一 (term, lang) 只算一次）。
_AUX_LOOKUP_CACHE: dict[tuple[str, str], str] = {}


def register_known_names(term: str, names: dict[str, str]) -> None:
    """登记某术语在各语言的已知名，供 :func:`verify_triangle` 三角验证使用。

    这是签名之外的**可选入口**（``verify_triangle`` 的形参已固定，无法再加
    入参）：调用方若有 glossary / 已确认译名，先登记再验证，闭环才有牙齿。
    未登记的语言会自动退回"字面命中 / 分布对齐反查"两条兜底路径。
    """
    key = normalize_name(term)
    if not key:
        return
    bucket = _KNOWN_NAMES.setdefault(key, {})
    for lang, name in (names or {}).items():
        if lang and name:
            bucket[str(lang)] = str(name)


def clear_known_names() -> None:
    """清空名册（测试与长驻进程复用同一 groups 时使用）。"""
    _KNOWN_NAMES.clear()
    _AUX_LOOKUP_CACHE.clear()


def register_glossary_names(glossary: Iterable[Any] | None) -> int:
    """把 glossary 的全部官方名（含各语言别名表面）灌入三角闭环名册。

    返回登记的键数。``penetrate_layered`` 内部会调用；standalone 使用
    :func:`verify_triangle` 的调用方也应当先调用它（或
    :func:`register_known_names`），否则 aux 闭环只剩字面命中一条路。
    """
    official_keys, names_by_key, _surfaces = _glossary_name_table(glossary)
    for key, names in names_by_key.items():
        register_known_names(key, names)
    return len(names_by_key)


def _aux_name_for(term: str, aux_language: str) -> tuple[str, str]:
    """返回 (aux 译名, 来源)。来源 ∈ {registry, align, ""}。

    registry 命中用 ``_term_language`` 归一后比较，所以名册里写 ``zh_tw``
    也能命中 ``zh_hant`` 的查询（两者在 corpus 与 TERM_LANGUAGES 里是同一
    语言位的两种拼法）。
    """
    wanted = termindex._term_language(aux_language)
    known = _KNOWN_NAMES.get(normalize_name(term)) or {}
    for lang, name in known.items():
        if termindex._term_language(lang) == wanted:
            return name, "registry"
    ctx = _ALIGN_CONTEXT
    if ctx.get("idf") is not None and ctx.get("groups") is not None:
        cache_key = (term, wanted)
        if cache_key in _AUX_LOOKUP_CACHE:
            hit = _AUX_LOOKUP_CACHE[cache_key]
            return (hit, "align") if hit else ("", "")
        try:
            aligned = termindex.align_term_by_frequency(
                term,
                ctx.get("source_language", "ja"),
                aux_language,
                ctx["groups"],
                ctx["idf"],
                vocab=ctx.get("vocab"),
            )
        except Exception:
            aligned = ""
        _AUX_LOOKUP_CACHE[cache_key] = aligned or ""
        return (aligned, "align") if aligned else ("", "")
    return "", ""


def _same_position_hits(
    groups: dict,
    term: str,
    source_language: str,
    needle: str,
    target_language: str,
    term_lines: dict[str, set[int]],
) -> dict[str, Any]:
    """term 出现行的预测对应行（±1）里是否出现 ``needle``。

    ``term_lines`` 是 ``_locate_term_lines`` 的结果（源语言侧命中行）。
    目标语言的正文明细按需取用；命中故事列表与行数为返回值。
    """
    if not needle or not term_lines:
        return {"stories": [], "lines": 0}
    stories_hit: list[str] = []
    lines_hit = 0
    for sk, line_nos in term_lines.items():
        page = _page_for(groups, sk, target_language)
        if page is None:
            continue
        tgt_text = str(page.get("text", ""))
        if needle not in tgt_text:
            continue
        tgt_lines = [ln.strip() for ln in tgt_text.splitlines() if ln.strip()]
        if not tgt_lines:
            continue
        src_lines = len(
            (_story_text_cache(groups, source_language).get(sk) or ("", []))[1]
        )
        found = False
        for si in sorted(line_nos)[:3]:
            pred = _predict_line(si, src_lines or len(line_nos) or 1, len(tgt_lines))
            for ti in (pred, pred - 1, pred + 1):
                if 0 <= ti < len(tgt_lines) and needle in tgt_lines[ti]:
                    lines_hit += 1
                    found = True
        if found:
            stories_hit.append(sk)
    return {"stories": stories_hit, "lines": lines_hit}


def verify_triangle(
    term: str,
    source_language: str,
    candidate_translation: str,
    target_language: str,
    groups: dict,
    aux_languages: tuple[str, ...] = ("zh_hans", "zh_hant", "ko"),
    *,
    max_aux_required: int = 1,
) -> dict:
    """三角闭环验证（修正实验发现的"繁中验证失效"问题）。

    实验里用"简中词在繁中出现"做验证是无效的（简繁同形使信号恒真）。正确
    做法：候选译名本身应在**其他语言的同点位**上也有对应译名命中。即
    ``源语言 term`` → ``目标语言 candidate`` → ``aux 语言译名`` 三段都必须
    落在同一处行位预测窗口内（±1 行），构成闭环。

    aux 译名的来源按优先级：
    1. ``register_known_names`` / ``penetrate_layered`` 从 glossary 登记的
       已知名（最可靠）；
    2. ``termindex.align_term_by_frequency`` 反查（需要调用方通过
       :func:`register_alignment_context` 提供 idf/vocab）；
    3. 字面命中：目标语言候选原样出现在 aux 语言文本里（Latin 品牌名
       常在各语言文本中不译，如 ``LUMINA时间``/``SEKAI``）。

    返回 ``{"verified": bool, "aux_hits": {lang: {...}}, "reason": str}``。
    ``verified`` 为真需同时满足：目标语言候选在同点位命中（锚定成功），且
    至少 ``max_aux_required`` 个 aux 语言也有同点位译名命中。
    """
    empty: dict[str, Any] = {}
    term = (term or "").strip()
    candidate_translation = (candidate_translation or "").strip()
    if not term or not candidate_translation:
        return {"verified": False, "aux_hits": empty, "reason": "term 或候选译名为空"}

    # 只在"源语言与目标语言成对"的故事上验证：源语言独有故事没有对应行，
    # 行位预测在那里必然落空或落到无关行。这也把扫描范围从全语料收敛到
    # 目标语言实际覆盖的故事集合（13k 全量下是数倍的差距）。
    scope = _paired_stories(groups, target_language, source_language)
    if not scope:
        return {
            "verified": False,
            "aux_hits": empty,
            "reason": f"没有同时具备 {source_language} 与 {target_language} 的故事",
        }
    term_lines = _locate_term_lines(groups, source_language, term, None, scope)
    if not term_lines:
        return {
            "verified": False,
            "aux_hits": empty,
            "reason": f"源语言（{source_language}）语料中未找到术语「{term}」",
        }

    # 第一段：目标语言候选的同点位锚定（候选是否真的出现在预测行）。
    anchor = _same_position_hits(
        groups, term, source_language, candidate_translation, target_language, term_lines
    )
    if not anchor["stories"]:
        return {
            "verified": False,
            "aux_hits": empty,
            "reason": (
                f"目标语言（{target_language}）同点位未命中候选「{candidate_translation}」，"
                "无锚定证据"
            ),
        }

    aux_hits: dict[str, Any] = {}
    n_verified = 0
    for aux_language in (aux_languages or ()):
        if termindex._term_language(aux_language) == termindex._term_language(target_language):
            continue  # 与目标语言同一语言位，不构成独立一票
        name, method = _aux_name_for(term, aux_language)
        if not name:
            # 兜底 3：Latin 候选字面命中。
            if _LATIN_CHAR_RE.search(candidate_translation):
                name, method = candidate_translation, "surface"
            else:
                aux_hits[aux_language] = {"name": "", "method": "", "hits": 0, "stories": []}
                continue
        hit = _same_position_hits(
            groups, term, source_language, name, aux_language, term_lines
        )
        aux_hits[aux_language] = {
            "name": name,
            "method": method,
            "hits": hit["lines"],
            "stories": hit["stories"][:5],
        }
        if hit["stories"]:
            n_verified += 1

    if max_aux_required <= 0:
        verified = True
        reason = (
            f"目标语言同点位锚定成功（{len(anchor['stories'])} 故事）；"
            "max_aux_required<=0，跳过 aux 闭环"
        )
    elif n_verified >= max_aux_required:
        verified = True
        names = ", ".join(
            f"{lang}={info['name']}({info['method']})"
            for lang, info in aux_hits.items()
            if info["stories"]
        )
        reason = f"闭环成立：{n_verified} 个 aux 语言同点位命中（{names}）"
    else:
        verified = False
        reason = (
            f"aux 闭环不足：仅 {n_verified} 个语言同点位命中（要求 ≥{max_aux_required}）"
        )
    return {"verified": verified, "aux_hits": aux_hits, "reason": reason}


def register_alignment_context(
    groups: dict,
    source_language: str,
    idf: dict | None,
    vocab: dict | None = None,
) -> None:
    """为 verify_triangle 的 aux 反查（align_term_by_frequency）提供上下文。"""
    _ALIGN_CONTEXT.clear()
    _ALIGN_CONTEXT.update(
        {
            "groups": groups,
            "source_language": source_language,
            "idf": idf,
            "vocab": vocab,
        }
    )
    _AUX_LOOKUP_CACHE.clear()


# ── 主入口：三层分层穿透 ────────────────────────────────────────────

# 冲突裁决优先级：L0 官方 > 通道 C > 通道 A > 通道 B
_CHANNEL_PRIORITY = {"L0": 0, "C": 1, "A": 2, "B": 3}


def _glossary_name_table(
    glossary: Iterable[Any] | None,
) -> tuple[dict[str, set[str]], dict[str, dict[str, str]], set[str]]:
    """从 glossary 建三张表：官方键集合 / 已知名册 / 官方源面集合。

    名册只收 TERM_LANGUAGES 里的语言槽（``_canon_names`` 把 zh_hant 折到
    zh_tw）：glossary 的 names 里还有 ``full`` / ``title`` / ``firstName``
    等非语言槽，直接透传会把 "スター -> title=スター" 这种伪语言写进结果。
    """
    official_keys: set[str] = set()
    names_by_key: dict[str, dict[str, str]] = {}
    official_surfaces: set[str] = set()
    for term in (glossary or []):
        raw_names = getattr(term, "names", {}) or {}
        canonical = str(getattr(term, "canonical", "") or "")
        surfaces = [canonical] + [str(v) for v in raw_names.values() if v]
        for surface in surfaces:
            key = normalize_name(surface)
            if key:
                official_keys.add(key)
                official_surfaces.add(surface)
        key = normalize_name(canonical)
        if not key:
            continue
        canon_names = {
            lang: str(value)
            for lang, value in termindex._canon_names(raw_names).items()
            if value and lang in termindex.TERM_LANGUAGES
        }
        entry = names_by_key.setdefault(key, {})
        entry.update(canon_names)
        if canonical and "ja" not in entry:
            entry["ja"] = canonical
        # 名册按**每个官方表面**建键，而非只按 canonical：查询方拿到的术语
        # 往往是某种语言的表面（セカイ、MEIKO、Shibuya），而 canonical 可能
        # 是另一支（SEKAI）。只挂 canonical 会让 "セカイ" 查不到名册，
        # 三角闭环退化成纯字面命中。
        for surface in canon_names.values():
            alias_key = normalize_name(surface)
            if alias_key and alias_key != key:
                names_by_key.setdefault(alias_key, {}).update(canon_names)
    return official_keys, names_by_key, official_surfaces


def _channel_a_align(
    groups: dict,
    term: str,
    source_language: str,
    target_languages: Iterable[str],
    *,
    vocab: dict | None,
    idf: dict | None,
    pair_index: dict | None,
    src_stories: set[str] | None = None,
) -> dict[str, str]:
    """通道 A：既有分布对齐（原样保留，不加新门控）。"""
    if idf is None:
        return {}
    out: dict[str, str] = {}
    for target_language in target_languages:
        if termindex._term_language(target_language) == termindex._term_language(source_language):
            continue
        stories_for = src_stories
        if stories_for is None:
            stories_for = termindex._term_stories(
                groups,
                source_language,
                term,
                {target_language},
                index=pair_index,
            )
        if not stories_for:
            continue
        try:
            aligned = termindex.align_term_by_frequency(
                term,
                source_language,
                target_language,
                groups,
                idf,
                vocab=vocab,
                src_stories=stories_for,
            )
        except Exception:
            aligned = ""
        if aligned:
            out[target_language] = aligned
    return out


def _channel_b_latin(
    groups: dict,
    terms: Iterable[str],
    source_language: str,
    target_language: str,
    min_stories: int = 2,
    min_ratio: float = 0.25,
) -> dict[str, str]:
    """通道 B：拉丁形态直取（对含 Latin 的候选，从行位取英语候选）。

    与通道 C 同构，只是候选来自拉丁形态过滤器而非片假名罗马音。
    没有 sim 门控可依赖，因此用"跨故事投票"当唯一门控（实验组 2 的做法）。
    """
    terms = sorted({t for t in terms if _has_latin(t) and len(t) >= 3})
    if not terms:
        return {}
    paired = _paired_stories(groups, target_language, source_language)
    if not paired:
        return {}
    src_idx = _build_term_line_index(groups, sorted(paired), source_language, terms)
    out: dict[str, str] = {}
    for term in terms:
        stories_by_term = src_idx.hits.get(term) or {}
        n_stories = len(stories_by_term)
        if n_stories < min_stories:
            continue
        votes: dict[str, set[str]] = defaultdict(set)
        for sk, line_nos in stories_by_term.items():
            page = _page_for(groups, sk, target_language)
            if page is None:
                continue
            el = [ln.strip() for ln in str(page.get("text", "")).splitlines() if ln.strip()]
            jl = src_idx.lines.get(sk) or []
            if not el or not jl:
                continue
            for si in sorted(line_nos)[:3]:
                pred = _predict_line(si, len(jl), len(el))
                for ti in (pred, pred - 1, pred + 1):
                    if 0 <= ti < len(el):
                        for cand in _latin_candidates_for_line(el[ti]):
                            votes[cand].add(sk)
        best: tuple[int, int, str] | None = None
        for cand, sk_set in votes.items():
            n = len(sk_set)
            if n < min_stories or n / n_stories < min_ratio:
                continue
            key = (n, len(cand), cand)
            if best is None or key > best:
                best = key
        if best is not None:
            out[term] = best[2]
    return out


def penetrate_layered(
    groups: dict,
    stories: list[str],
    candidates: list[str],
    *,
    source_language: str = "ja",
    target_languages: tuple[str, ...] = ("zh_hans", "en"),
    glossary=None,
    seed: set[str] | None = None,
    discovered: set[str] | None = None,
    vocab: dict | None = None,
    idf: dict | None = None,
    pair_index: dict | None = None,
) -> dict:
    """三层分层穿透主入口（重构后的统一接口）。

    第 0 层：``candidate_tiers.filter_alignable`` 前置过滤（去掉通用词）。
    第 1 层：三通道并行 ——
        通道 A：``align_term_by_frequency``（现有分布对齐，原样保留）；
        通道 B：拉丁形态直取（对含 Latin 的候选，用 ``extract_latin_candidates``
            从行位取候选，跨故事投票）；
        通道 C：``channel_c_katakana_to_english``（片假名专属，romaji 硬门控）。
    第 2 层：``verify_triangle`` 三角闭环 + 冲突裁决
        （优先级 L0 官方 > 通道 C > 通道 A > 通道 B）。

    ``glossary`` 提供官方名册：既用于 L0 分层，也用于三角闭环的 aux 名册
    （``register_known_names``）。``idf`` / ``vocab`` / ``pair_index`` 缺省时
    通道 A 静默跳过（``skipped_reason`` 里记录原因），通道 B/C 仍工作。

    返回::

        {
          "pairs": {term: {lang: translation}},
          "tier_stats": {...},
          "channel_stats": {"A": n, "B": n, "C": n},
          "rejected": [...],   # 被前置过滤的通用词（诊断用）
          "skipped_reason": {...},
        }
    """
    target_languages = tuple(t for t in (target_languages or ()) if t)
    glossary_list = list(glossary) if glossary is not None else None
    official_keys, names_by_key, official_surfaces = _glossary_name_table(glossary_list)
    seed = seed or set()
    discovered = discovered or set()

    pairs: dict[str, dict[str, str]] = {}
    rejected: list[str] = []
    skipped_reason: dict[str, str] = {}

    # ── 第 0 层：前置过滤 ────────────────────────────────────────────
    tiers: dict[str, Tier] = {}
    alignable: list[str] = []
    seen: set[str] = set()
    for term in (candidates or []):
        if not term or term in seen:
            continue
        seen.add(term)
        tier = classify(
            term,
            language=source_language,
            official_keys=official_keys,
            seed_keys={normalize_name(s) for s in seed},
            discovered={normalize_name(d) for d in discovered},
        )
        tiers[term] = tier
        # 按值比较而非 `is`：candidate_tiers 的单测用 importlib.reload 模拟
        # romaji 出现/消失，reload 会重建 Tier 枚举类，届时本模块持有的旧类
        # 与 classify 返回的新实例 `is` 判等为假（Tier 继承 str，值比较安全）。
        if tier == Tier.REJECT:
            rejected.append(term)
        else:
            alignable.append(term)
    tier_stats = tier_summary(tiers)
    if not alignable:
        skipped_reason["layer0"] = "前置过滤后无候选进入对齐"

    # 官方候选直接采纳 L0（最高优先级），不进任何通道。
    official_pairs: dict[str, dict[str, str]] = {}
    for term in alignable:
        known = names_by_key.get(normalize_name(term))
        if known:
            official_pairs[term] = {
                lang: name for lang, name in known.items() if lang
            }
    # 官方名册登记，供三角闭环使用。
    for key, names in names_by_key.items():
        register_known_names(key, names)

    if idf is None:
        skipped_reason["A"] = "未提供 idf（通道 A 需要 build_alignment_resources）"
    if glossary_list is None:
        skipped_reason["glossary"] = "未提供 glossary（L0 官方层与 aux 名册为空）"

    # ── 第 1 层：三通道并行 ─────────────────────────────────────────
    # 通道 A：分布对齐（对全部可对齐候选，与既有实现同口径）。
    channel_a: dict[str, dict[str, str]] = {}
    if idf is not None:
        for term in alignable:
            aligned = _channel_a_align(
                groups,
                term,
                source_language,
                target_languages,
                vocab=vocab,
                idf=idf,
                pair_index=pair_index,
            )
            if aligned:
                channel_a[term] = aligned

    # 通道 B：拉丁形态直取（目标语言取第一个非源语言）。
    latin_targets = [
        t for t in target_languages
        if termindex._term_language(t) != termindex._term_language(source_language)
    ]
    channel_b: dict[str, dict[str, str]] = {}
    if latin_targets:
        b_target = latin_targets[0]
        b_hits = _channel_b_latin(
            groups, alignable, source_language, b_target, 2, 0.25
        )
        for term, cand in b_hits.items():
            channel_b[term] = {b_target: cand}
    else:
        skipped_reason["B"] = "无非源语言目标，通道 B 无作用域"

    # 通道 C：片假名→英语。
    channel_c: dict[str, dict[str, str]] = {}
    english_target = next(
        (t for t in target_languages if termindex._term_language(t) == "en"), None
    )
    if english_target is None:
        skipped_reason["C"] = "目标语言不含 en，通道 C 无作用域"
    else:
        katakana_terms = {t for t in alignable if _is_pure_katakana(t)}
        c_hits = channel_c_katakana_to_english(
            groups,
            list(stories),
            katakana_terms,
            source_language=source_language,
            target_language=english_target,
        )
        for term, cand in c_hits.items():
            channel_c[term] = {english_target: cand}

    # ── 第 2 层：三角闭环 + 冲突裁决 ─────────────────────────────────
    register_alignment_context(groups, source_language, idf, vocab)
    triangle_stats = {"checked": 0, "verified": 0}
    staged: dict[str, dict[str, dict[str, str]]] = defaultdict(dict)
    for term, lang_map in official_pairs.items():
        for lang, name in lang_map.items():
            staged[term][lang] = {"text": name, "source": "L0"}
    for source_name, table in (("C", channel_c), ("A", channel_a), ("B", channel_b)):
        for term, lang_map in table.items():
            for lang, name in lang_map.items():
                entry = staged[term].get(lang)
                if entry is None or _CHANNEL_PRIORITY[source_name] < _CHANNEL_PRIORITY.get(
                    entry["source"], 9
                ):
                    staged[term][lang] = {"text": name, "source": source_name}

    triangle_flags: dict[str, dict[str, Any]] = {}
    for term in sorted(staged):
        lang_entries = staged[term]
        row: dict[str, str] = {}
        for lang in sorted(
            lang_entries,
            key=lambda lg: (_CHANNEL_PRIORITY.get(lang_entries[lg]["source"], 9), lg),
        ):
            entry = lang_entries[lang]
            name = entry["text"]
            if entry["source"] != "L0" and termindex._term_language(lang) == "en":
                # 三角闭环只对 en 目标有意义：aux（zh/ko）与 en 构成跨域闭环。
                triangle_stats["checked"] += 1
                tri = verify_triangle(
                    term,
                    source_language,
                    name,
                    lang,
                    groups,
                    aux_languages=("zh_hans", "zh_hant", "ko"),
                    max_aux_required=1,
                )
                triangle_flags[f"{term}\x00{lang}"] = tri
                if tri["verified"]:
                    triangle_stats["verified"] += 1
            row[lang] = name
        if row:
            pairs[term] = row

    channel_stats = {
        "A": sum(len(v) for v in channel_a.values()),
        "B": sum(len(v) for v in channel_b.values()),
        "C": sum(len(v) for v in channel_c.values()),
    }
    return {
        "pairs": pairs,
        "tier_stats": tier_stats,
        "channel_stats": channel_stats,
        "rejected": rejected,
        "skipped_reason": skipped_reason,
        "triangle_stats": triangle_stats,
        "triangle_flags": triangle_flags,
        "official_surfaces": len(official_surfaces),
    }


def channel_stats_summary(result: dict) -> str:
    """把 :func:`penetrate_layered` 的结果格式化成可读诊断文本。"""
    if not result:
        return "（无结果）"
    lines: list[str] = []
    pairs = result.get("pairs") or {}
    channels = result.get("channel_stats") or {}
    tiers = result.get("tier_stats") or {}
    rejected = result.get("rejected") or []
    skipped = result.get("skipped_reason") or {}

    lines.append("术语穿透分层诊断")
    lines.append("-" * 46)
    alignable_n = tiers.get("L0", 0) + tiers.get("L1", 0) + tiers.get("L2", 0)
    lines.append(
        "第0层 候选分层: "
        + " ".join(f"{k}={tiers.get(k, 0)}" for k in ("L0", "L1", "L2", "L3"))
        + f"  (可对齐 {alignable_n}，拒绝 {tiers.get('L3', 0)})"
    )
    lines.append(
        "第1层 通道产出: "
        f"A(分布对齐)={channels.get('A', 0)}  "
        f"B(拉丁直取)={channels.get('B', 0)}  "
        f"C(片假名音译)={channels.get('C', 0)}"
    )
    tri = result.get("triangle_stats") or {}
    if tri:
        checked = tri.get("checked", 0)
        verified = tri.get("verified", 0)
        rate = (verified / checked * 100) if checked else 0.0
        lines.append(
            f"第2层 三角闭环: 检查 {checked} 条 / 通过 {verified} 条 ({rate:.0f}%)"
        )
    lines.append(f"最终穿透: {len(pairs)} 个术语")
    if rejected:
        sample = "、".join(rejected[:8])
        more = f" 等 {len(rejected)} 个" if len(rejected) > 8 else ""
        lines.append(f"前置过滤拒绝: {sample}{more}")
    for key, reason in skipped.items():
        lines.append(f"跳过 {key}: {reason}")
    if pairs:
        lines.append("-" * 46)
        for term in sorted(pairs)[:15]:
            names = pairs[term]
            rendered = " | ".join(f"{lang}={name}" for lang, name in names.items())
            lines.append(f"  {term} -> {rendered}")
    return "\n".join(lines)
