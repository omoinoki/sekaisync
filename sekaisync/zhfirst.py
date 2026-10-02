"""zh-first terminology pipeline (人工标准复刻).

以 `data/term-annotations.json` 的人工标注为黄金标准反推的抽取算法：

1. 候选主位是简中正文（zh_hans），不是日文。
2. 每行剥离冒号前发言人（说话人不是术语）。
3. 排除 26 名可玩主角的全部表面形（全名/姓/名/罗马字/译名）与代词——
   主角互称与自我指涉永远不进词云。
4. 内容承载词保留：专名、配角人名、团体、活动、地点、物品、音乐术语、
   关键成语——判定交给同点位四语对齐与 LLM 同义检验，而不是黑名单。
5. 对齐：对每个简中候选，在其出现的同一 story 内用「源词所在行 → 目标语
   预测行」提取候选，跨 story 投票 + IDF × containment 评分；单故事词诚实
   留空。
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Optional

from sekaisync.normalize import normalize_name
from sekaisync.wordseg import discover_words, _discover_content_words
from sekaisync.termindex import (
    group_pages_by_story,
    looks_like_proper_noun,
    _ZH_FUNCTION_CHARS,
    _local_translation_candidates,
)

# ── 负面规则 ────────────────────────────────────────────────────────

# 代词与泛指称呼：任何语言里都不承载内容。
_PRONOUNS_ZH = {
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "她们",
    "这位", "那位", "大家", "各位", "谁", "什么", "哪个", "这里", "那里",
    "这个", "那个", "这样", "那样", "自己", "别人", "某人",
}
_SPEAKER_TAIL_ZH = ("先生", "小姐", "女士", "同学", "老师", "酱", "桑", "君")

# 行首发言人模式（简中）：「心羽：」「大河先生：」「真冬&初音未来：」等。
SPEAKER_RE = re.compile(r"^[^：:]{1,20}[：:]\s*")
# 多人连署发言人：「こはね・ミク：」「杏・彰人・冬弥：」在 ja/ko 行同样剥离。
SPEAKER_RE_MULTI = re.compile(
    r"^(?:[^：:]{1,12}[・&、,， ])+[^：:]{1,12}[：:]\s*"
)

_QUOTE_RE = re.compile(r"[「『“‘\"]([^」』”’\"\n]{2,40})[」』”’\"]")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9＊*♡・·'’\-]*(?:[ &×·][A-Za-z0-9＊*♡・·'’\-]+)*")


def _strip_speaker_impl(line: str) -> int:
    """Return a bounded dialogue label's end without regex backtracking.

    A colon later in a long prose line must not re-enable the old nested
    speaker regex. Times and URLs also contain colons but are not labels.
    """
    colons = [index for mark in (":", "：")
              if (index := line.find(mark, 0, 81)) >= 0]
    if not colons:
        return 0
    index = min(colons)
    head = line[:index]
    if not head.strip() or re.search(r"[\n。！？!?；;]", head):
        return 0
    if (line[index + 1:index + 3] == "//"
            or re.search(r"\b\d{1,2}$", head) and line[index + 1:index + 2].isdigit()):
        return 0
    end = index + 1
    while end < len(line) and line[end].isspace():
        end += 1
    return end


def strip_speaker(line: str) -> str:
    """剥掉行首发言人标签；多人连署也一并处理。返回正文部分。"""
    end = _strip_speaker_impl(line)
    return line[end:] if end else line


def speaker_of(line: str) -> str:
    """返回行首发言人（无则空串）。"""
    end = _strip_speaker_impl(line)
    return line[:end].rstrip("：: ").strip() if end else ""


# ── 数据结构 ────────────────────────────────────────────────────────

@dataclass
class ZhFirstTerm:
    canonical: str            # 简中表面形
    tags: list[str] = field(default_factory=lambda: ["other"])
    names: dict[str, str] = field(default_factory=dict)  # lang -> surface
    evidence: list[dict] = field(default_factory=list)
    stories: set[str] = field(default_factory=set)
    lines_n: int = 0
    official: bool = False
    everyday: bool = False
    source: str = "zhfirst"

    def to_dict(self) -> dict:
        return {
            "canonical": self.canonical,
            "tags": self.tags,
            "names": {k: v for k, v in self.names.items() if v},
            "stories": sorted(self.stories),
            "lines": self.lines_n,
            "official": self.official,
            "everyday": self.everyday,
            "evidence": self.evidence[:8],
        }


# ── 屏蔽表构建 ──────────────────────────────────────────────────────

def build_zhfirst_blocklist(glossary: Iterable[Any]) -> tuple[set[str], dict[str, dict]]:
    """返回 (主角屏蔽表 normalized, 官方五语词典 canonical→names)。

    主角屏蔽表收录 26 名可玩角色的全名/姓/名/假名/罗马字/各语译名；
    官方词典来自 glossary 全部 noun kind 的官方多语名，用于直接继承权威译名。"""
    protagonists: set[str] = set()
    official: dict[str, dict] = {}
    from sekaisync.termindex import NOUN_KINDS  # late import 避免环

    for gt in glossary:
        kind = str(getattr(gt, "kind", ""))
        names = getattr(gt, "names", {}) or {}
        if kind == "character":
            for v in names.values():
                if not v:
                    continue
                v = str(v).strip()
                if len(v) >= 2:
                    protagonists.add(v)
                    protagonists.add(normalize_name(v))
            ja = str(names.get("ja") or names.get("full") or "")
            zh = str(names.get("zh_hans") or "")
            en_given = str(names.get("givenNameEnglish") or "")
            for full in (ja, zh):
                if 3 <= len(full) <= 6:
                    protagonists.add(full[:2])
                    protagonists.add(full[-2:])
            if en_given:
                protagonists.add(en_given.lower())
                protagonists.add(en_given.upper())
        authority = getattr(gt, "official", False)
        if (kind in NOUN_KINDS and (authority is True or type(authority) is int and authority == 1)
                and not getattr(gt, "demo", False)):
            ja = str(names.get("ja") or "")
            if ja and 2 <= len(ja) <= 40:
                entry = {k: str(v) for k, v in names.items() if v}
                key = normalize_name(ja)
                prev = official.get(key)
                if prev is None:
                    official[key] = entry
                else:
                    # The same source surface can name two entities. Inherit
                    # only target slots on which their official records agree.
                    official[key] = {
                        lang: value for lang, value in prev.items()
                        if entry.get(lang) == value
                    }
    # 泛指/疑问/指示词：不承载内容，逐字出现频率极高。
    protagonists |= {
        "什么", "怎么", "为什么", "哪个", "哪些", "这个", "那个", "这些", "那些",
        "这样", "那样", "自己", "别人", "某人", "每个", "所有", "一切", "部分",
        "大家", "各位", "我们", "你们", "他们", "她们", "它们", "这里", "那里",
        "现在", "时候", "东西", "事情", "地方", "样子", "感觉", "那位", "这位",
    }
    return protagonists, official


# ── 候选抽取 ────────────────────────────────────────────────────────

_CJK = r"\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"


# Resolved against the package location rather than the process CWD: the file
# is a data asset consumed by the pipeline, and callers run from the repo root,
# from tests, and from installed CLIs alike.
_MANUAL_ANNOTATIONS_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "term-annotations.json"
)


def _read_manual_annotations() -> dict:
    """Read the annotation document.

    The primary path is resolved from this module, so the file is found
    regardless of the working directory. The CWD-relative fallback covers
    installs where the repository ``data/`` directory is not shipped next to
    the package.
    """
    for path in (_MANUAL_ANNOTATIONS_PATH, Path("data/term-annotations.json")):
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
    return {}


def load_manual_annotations() -> dict[str, list[str]]:
    """Return the human annotations as ``story_key -> terms``.

    This is the raw shape of the annotation file, and the reason it is JSON:
    the previous line-oriented text held the same information but threw the
    story boundaries away on load, so the only thing anyone could do with it was
    take the union. Keeping the per-story structure makes the file usable as a
    ground truth for extraction precision/recall, not just as a seed list.

    Terms are returned exactly as annotated; nothing is normalized here, since
    callers differ on whether they want surface forms or comparison keys.
    """
    data = _read_manual_annotations()
    stories = data.get("stories", {}) if isinstance(data, dict) else {}
    out: dict[str, list[str]] = {}
    for story_key, terms in stories.items():
        if not isinstance(terms, list):
            continue
        cleaned = [str(t).strip() for t in terms if str(t).strip()]
        if cleaned:
            out[str(story_key)] = cleaned
    return out


def _load_manual_seed() -> set[str]:
    """Seed vocabulary: the union of all annotated terms.

    See `data/term-annotations.json` for provenance and caveats."""
    out: set[str] = set()
    for terms in load_manual_annotations().values():
        for term in terms:
            if term:
                out.add(term)
    return out


def _segment_forward(text: str, vocab: frozenset, max_len: int = 5) -> list[str]:
    """Forward maximum matching: greedy longest dict word from left."""
    words: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        matched = False
        for ln in range(min(max_len, n - i), 0, -1):
            if text[i:i+ln] in vocab:
                words.append(text[i:i+ln])
                i += ln
                matched = True
                break
        if not matched:
            words.append(text[i])
            i += 1
    return words


def _segment_backward(text: str, vocab: frozenset, max_len: int = 5) -> list[str]:
    """Backward maximum matching: greedy longest dict word from right."""
    words: list[str] = []
    i = len(text)
    while i > 0:
        matched = False
        for ln in range(min(max_len, i), 0, -1):
            if text[i-ln:i] in vocab:
                words.insert(0, text[i-ln:i])
                i -= ln
                matched = True
                break
        if not matched:
            words.insert(0, text[i-1])
            i -= 1
    return words


def segment_zh_bi_cjk(text: str, vocab: frozenset, max_len: int = 5) -> list[str]:
    """Choose dictionary coverage globally, retaining deterministic ties.

    Two greedy directions can both strand known words. The acyclic lattice
    has at most ``len(text) * max_len`` edges: minimize unmatched characters,
    then token count, then favour the longer leftmost word. ``max_len`` keeps
    its public meaning; extraction supplies the vocabulary's actual maximum.
    """
    n = len(text)
    best = [(0, 0)] * (n + 1)
    next_end = list(range(n + 1))
    for i in range(n - 1, -1, -1):
        score = (best[i + 1][0] + (text[i] not in vocab), best[i + 1][1] + 1)
        end = i + 1
        for length in range(2, min(max_len, n - i) + 1):
            if text[i:i + length] not in vocab:
                continue
            candidate = (best[i + length][0], best[i + length][1] + 1)
            if candidate <= score:
                score, end = candidate, i + length
        best[i], next_end[i] = score, end
    words: list[str] = []
    i = 0
    while i < n:
        end = next_end[i]
        words.append(text[i:end])
        i = end
    return words


def segment_zh_bi(text: str, vocab, max_len: int = 5) -> list[str]:
    """Bi-MM with language-block pre-split. Latin/digit runs kept whole."""
    if not isinstance(vocab, frozenset):
        vocab = frozenset(vocab)
    blocks: list[str] = []
    buf_lat: list[str] = []
    buf_cjk: list[str] = []
    def flush_lat():
        if buf_lat: blocks.append("".join(buf_lat)); buf_lat.clear()
    def flush_cjk():
        if buf_cjk: blocks.append("".join(buf_cjk)); buf_cjk.clear()
    for ch in text:
        if re.match(r"[A-Za-z0-9]", ch):
            flush_cjk(); buf_lat.append(ch)
        elif re.match(f"[{_CJK}]", ch):
            flush_lat(); buf_cjk.append(ch)
        else:
            flush_lat(); flush_cjk(); blocks.append(ch)
    flush_lat(); flush_cjk()
    result: list[str] = []
    for blk in blocks:
        if re.match(f"[{_CJK}]", blk) and len(blk) >= 2:
            result.extend(segment_zh_bi_cjk(blk, vocab, max_len))
        else:
            result.append(blk)
    return result


# 结构信号专名：活动/设施/组织后缀 + 拉丁专名模式。
# termextract 思想的轻量版——低频一次性专名（freq=1）无法靠统计发现，
# 但"以这些后缀结尾"或"拉丁多词大写串"的结构信号足以放行。
_QUOTE_MARKS = set("“”『』「」''")

# 纯汉字后缀（通道 D 反查锚点）：从 _PROPER_SUFFIXES 过滤全汉字项并扩充
_PROPER_SUFFIX_HANZI = tuple(
    s for s in (
        "大奖赛", "锦标赛", "音乐节", "艺术节", "纪念日", "演唱会", "广播剧",
        "歌剧团", "剧团", "乐团", "乐队", "经纪公司", "学园", "高校", "学校",
        "学院", "十字路口", "公园", "广场", "舞台", "庆典", "粉丝节", "派队",
        "县政府", "町", "泡水",
    ) if all("一" <= ch <= "鿿" for ch in s)
)
_PROPER_SUFFIXES = (
    "大奖赛", "锦标赛", "音乐节", "艺术节", "纪念日", "演唱会", "广播剧",
    "组合", "乐团", "乐队", "歌剧团", "剧团", "经纪公司", "公司", "商店",
    "咖啡厅", "咖啡店", "学园", "学校", "学院", "电视台", "广播台", "研究所",
    "工作室", "公园", "广场", "路口", "通道", "舞台", "唱片", "视频",
    "时间", "计划", "项目", "大奖", "祭", "杯", "展", "会",
)
_PROPER_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9&.+'\-/ ]{0,78}[A-Za-z0-9]")


# Latin+汉字混合专名或 ≥2 词纯 Latin 串（非引号文本通道用）。
# 混合：jam音乐节 / LUMINA时间 / Smile视频 / C位（单字母+汉字也认，限2处）
# 纯 Latin：Lasting ECHO Fes / LOVELOve（多词或有大小写交替）
# 专名尾字白名单：混合专名的汉字部分以"词性闭类"字符结尾的情况极少，
# 但后续常接动词/助词（要开始了/开幕了/投稿活动）。用常见名词后缀或
# 端点约束处理：汉字段最多 4 字且其后必须是非汉字或句子端点由 finditer
# 天然给出——这里再排除以常见动词助词开头粘连的伪词由 _is_content_word 兜底。
_MIXED_PROPER_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9&+'.\-]{0,19}(?:[ ][A-Za-z0-9&+'.\-]+){0,5}[一-鿿]{1,8}"
    r"|[A-Za-z]{1,3}[一-鿿]{1,4}"
)
# 汉字尾部的单字动词/助词（可循环剥）+ 常见双字动词（一次剥）。
# 贪心匹配会把 "LUMINA时间要开始了" 整段吞进来；专名的语义边界在
# "时间"，后面是谓语。循环剥到剩下 "汉字段+专名核心" 为止。
_MIXED_TAIL_SINGLE = set("了着过的是呢吧吗哦呀啊")
_MIXED_TAIL_WORDS = ("要开始", "开始", "举办", "投稿", "开幕", "上线", "说明", "通知")


def mixed_proper_surface(m) -> str:
    """从贪心匹配里剥掉粘连的谓语/助词尾巴，返回专名 surface。"""
    w = m.group(0)
    changed = True
    while changed and len(w) > 3:
        changed = False
        if w[-1] in _MIXED_TAIL_SINGLE:
            w = w[:-1]
            changed = True
            continue
        for word in _MIXED_TAIL_WORDS:
            if w.endswith(word) and len(w) - len(word) >= 3:
                w = w[: -len(word)]
                changed = True
                break
    return w


def looks_like_proper_surface(w: str) -> bool:
    """结构信号专名判定（中文串按后缀，拉丁串按形态）。"""
    w = w.strip()
    if not w:
        return False
    if _PROPER_LATIN.fullmatch(w):
        from sekaisync.termindex import _local_latin_candidates
        return _local_latin_candidates(w) == [w]
    if len(w) >= 3 and w.endswith(_PROPER_SUFFIXES):
        # 汉字串 + 专名后缀；排除明显的句子片段（前缀含功能字开头则不管，
        # 因为后缀信号本身已足够强）
        return True
    # 拉丁+汉字混合（LUMINA时间 / Smile视频 / jam音乐节）
    if len(w) >= 4 and re.search(r"[A-Za-z]", w) and re.search(r"[一-鿿]", w)             and w.endswith(_PROPER_SUFFIXES):
        return True
    return False


def llm_filter_terms(
    terms: list[str],
    llm: Any,
    batch: int = 40,
) -> set[str]:
    """用 LLM 语义判读候选词：区分「内容承载词」与「碎片/短语」。

    keep 判定标准（模型内建语义）：
    - KEEP：专名/物品/地点/组织/活动/概念/音乐术语/引号台词等承载内容，值得穿透
    - DROP：动宾短语/主谓短语/指代词/语气词/半句碎片（不该出现在词云）

    无外部 LLM 时降级为确定性规则 `_is_content_word`。"""
    if llm is None:
        return {w for w in terms if _is_content_word(w)}
    keep: set[str] = set()
    for i in range(0, len(terms), batch):
        chunk = terms[i:i+batch]
        system = (
            "你是《世界计划》游戏术语筛选器。判断每个中文词是否为「值得跨语言穿透的"
            "内容承载词」。返回 JSON: {\"results\":[{\"term\":\"...\",\"keep\":true/false}]}。"
            "KEEP=专名/物品/地点/组织/活动/概念/音乐术语/引号台词等承载内容；"
            "DROP=动宾短语/主谓短语/指代词/语气词/半句碎片。"
        )
        user = "词列表:\n" + "\n".join(f"- {w}" for w in chunk)
        try:
            data = llm.chat_json(system, user)
            results = data.get("results", []) if isinstance(data, dict) else []
            for r in results:
                if isinstance(r, dict) and r.get("keep") is True and str(r.get("term", "")).strip() in set(chunk):
                    keep.add(str(r["term"]).strip())
        except Exception:
            # LLM 失败：降级到规则
            keep.update(w for w in chunk if _is_content_word(w))
    return keep


def _is_fragment(w: str) -> bool:
    """判定 w 是否为语义碎片（非完整名词）。"""
    if len(w) <= 2 and w in _FRAGMENT_WORDS:
        return True
    for rx in _FRAGMENT_RE:
        if rx.search(w):
            return True
    if len(w) == 2 and w[0] in "的不没很太真就才也还又再都总只仅正在是要会能让使叫请帮带拿放走跑来去上下进出回过到看听说想做弄快赶":
        return True
    if len(w) == 3 and w[0] in "快赶请帮带去说来听听看想做弄拿放走跑":
        return True
    if re.match(r"^(我|你|他|她|我们|你们|他们|她们|大家|自己|别人|这个|那个|这样|那样)[的].", w):
        return True
    if len(w) in (2, 3) and re.match(r"^[\u4e00-\u9fff][到|来|去|下|完|好|成|到]", w) and re.search(r"[到|来|去|下|完|好|成]$", w):
        return True
    if w.startswith(("的", "地", "得")):
        return True
    if w.endswith("点") and 2 <= len(w) <= 3:
        return True
    if len(w) == 3 and w.endswith(("而", "的", "地", "得", "为", "于")):
        return True
    return False


_FRAGMENT_RE = [
    re.compile(r"^(不|没|很|太|真|就|才|也|还|又|再|都|总|只|仅|正|在|是|有|要|会|能|该|让|使|叫|请|帮|带|拿|放|走|跑|来|去|上|下|进|出|回|过|到|看|听|说|想|做|弄)[^的]"),
    re.compile(r"(我|你|他|她|我们|你们|他们|她们|大家|自己|别人)[^的]{0,4}$"),
    re.compile(r"^(这个|那个|这些|那些|这样|那样|怎么|什么|为什么|哪个|哪些|哪里|谁)"),
    re.compile(r"(的|了|着|过|吧|吗|呢|啊|呀|哦|嗯|嘛|哈|嘿|哎|哟|哇|啦|么)$"),
    re.compile(r"^(一|两|三|四|五|六|七|八|九|十|半|几|每|各|某|本|该|此|彼)"),
]
_FRAGMENT_WORDS = {
    "记", "听", "看", "想", "说", "做", "弄", "来", "去", "走", "跑",
    "能", "会", "要", "让", "请", "帮", "带", "拿", "放", "在", "是", "有",
    "好", "对", "不", "没", "别", "真", "很", "太", "就", "才", "还", "也",
    "又", "再", "都", "总", "只", "仅", "正", "刚", "马", "立", "快", "赶",
    "几乎", "将近", "超过", "关于", "对于", "由于", "为了", "通过", "按照",
    "根据", "即使", "尽管", "无论", "不管", "只要", "只有", "除非", "凡是",
    "好像", "似乎", "仿佛", "犹如", "如同", "不光", "不仅", "不但", "而且",
    "何况", "况且", "再说", "并且", "乃至", "甚至", "即便", "哪怕", "纵使",
    "就算", "随便", "顺便", "专门", "特意", "故意", "依旧", "仍然", "始终",
}


def _is_content_word(w: str) -> bool:
    """内容词粗筛：长度、字符构成、功能词、语义碎片。"""
    if len(w) < 2 or len(w) > 24:
        return False
    if re.search(r"[0-9０-９]", w):
        return False
    if not re.search(f"[{_CJK}]", w) and not re.fullmatch(r"[A-Za-z][A-Za-z0-9＊*♡'’\-]*", w):
        return False
    if w in _ZH_FUNCTION_2:
        return False
    if _is_fragment(w):
        return False
    return True





# 高频 2 字中文功能词：不承载内容，逐字出现频率极高。
_ZH_FUNCTION_2 = {
    "不过", "但是", "因为", "所以", "如果", "虽然", "然后", "接着", "于是",
    "而且", "并且", "但是", "还是", "就是", "只是", "可是", "然而", "结果",
    "已经", "正在", "即将", "可以", "应该", "可能", "也许", "一定", "真的",
    "非常", "特别", "比较", "稍微", "一起", "一直", "马上", "立刻", "终于",
    "最后", "首先", "其次", "然后", "甚至", "尤其", "反正", "毕竟", "其实",
    "原来", "大概", "几乎", "将近", "超过", "关于", "对于", "由于", "为了",
    "通过", "按照", "根据", "从", "被", "把", "给", "向", "往", "对", "与",
    "和", "或", "而", "之", "其", "及", "并", "即", "则", "若", "如", "因",
    "再", "又", "也", "就", "才", "刚", "只", "仅", "都", "总", "全", "曾",
    "来", "去", "上", "下", "进", "出", "回", "过", "到", "在", "是", "有",
    "没", "不", "别", "看", "听", "说", "想", "要", "会", "能", "该", "做",
    "弄", "让", "使", "叫", "请", "帮", "带", "拿", "放", "走", "跑", "来",
    "嗯", "啊", "哦", "呀", "吧", "吗", "呢", "嘛", "哈", "嘿", "哎", "哟",
    "啦", "哇", "唉", "咦", "喔", "呃", "么", "的", "了", "着", "过", "地",
    "得", "之", "于", "而", "其", "此", "彼", "这", "那", "哪", "什", "怎",
    "么", "自", "己", "咱", "俺", "你", "我", "他", "她", "它", "谁", "哪",
    "呢", "吧", "嘛", "喽", "哟", "咯", "呀", "哇", "哈", "哦", "嗯", "啊",
    "耶", "嘞", "噻", "兮", "喲", "喂", "嗨", "啥", "咋", "俺", "咱",
    "呵呵", "呵呵呵", "哈哈", "嘿嘿", "嘻嘻", "谢谢", "感谢", "辛苦", "抱歉",
    "不好意思", "没问题", "没关系", "好了", "好的", "好吧", "好呀", "好哦",
    "那我", "那么", "这样", "那样", "怎么", "什么", "一下", "一点", "有些",
    "有点", "不太", "不是", "没错", "对啊", "真的吗", "原来如此", "原来这样",
    "就是说", "也就是说", "不是吗", "是不是", "会不会", "能不能", "要不要",
    "是不是", "今天", "明天", "昨天", "刚才", "现在", "之后", "之前", "时候",
    "地方", "东西", "事情", "样子", "感觉", "心情", "想法", "问题", "原因",
    "结果", "办法", "方法", "机会", "时间", "生活", "世界", "大家",
    "样啊", "是啊", "对啊", "好啦", "对啦", "来啦", "走啦", "算啦", "罢了",
    "得了", "行了", "对了", "等等", "马上", "立刻", "赶快", "赶紧", "连忙",
    "恰好", "刚好", "正好", "偏偏", "难道", "居然", "竟然", "果然", "其实",
    "毕竟", "反正", "倒是", "反而", "恐怕", "或许", "几乎", "差点", "顺便",
    "专门", "特意", "故意", "依旧", "仍然", "始终", "终究", "到底", "究竟",
    "根本", "简直", "实在", "确实", "的确", "务必", "必须", "肯定", "确定",
    "似乎", "好像", "仿佛", "犹如", "如同", "宛如", "不光", "不仅", "不但",
    "何况", "况且", "再说", "并且", "乃至", "甚至", "即便", "哪怕", "纵使",
    "就算", "即使", "尽管", "无论", "不管", "不论", "任凭", "除非", "凡是",
    "由于", "基于", "鉴于", "限于", "关于", "对于", "至于", "相对于", "针对",
    "另外", "此外", "再者", "总之", "综上", "总而言之", "换句话说", "反过来说",
    "恩恩", "嗯嗯", "哦哦", "啊啊", "哈哈", "呵呵", "嘿嘿", "嘻嘻", "哇塞",
    "天哪", "哎呀", "哎哟", "咦咦", "呜呜", "哇哇", "啦啦", "呀呀", "哦耶",
    "无所", "所", "有所", "无", "无所", "然", "然而", "反而", "虽", "虽则",
    "的", "地", "得", "这", "那", "之", "其", "此", "彼", "为", "于", "以",
    "所", "而", "与", "及", "或", "若", "如", "因", "由", "于", "自", "至",
    "从", "到", "被", "把", "给", "让", "使", "叫", "请", "帮", "带", "拿",
    "放", "走", "跑", "来", "去", "上", "下", "进", "出", "回", "过", "到",
    "负责画", "负责", "画的", "画着", "画过", "我来", "我去", "你去", "他我",
    "她我", "帮我", "帮你", "帮他", "带他", "帮他", "听我", "看我", "想我",
    "然不会", "不会", "会不", "不会的", "也不是", "也来", "去也", "然也",
    "不会的", "不然", "要不然", "难道", "好像", "似的", "一样", "那么", "这么",
    "如此", "怎样", "怎么", "什么样", "什么的", "之类", "等等", "等",
}


@lru_cache(maxsize=8)
def _vocabulary_trie(vocab: frozenset[str]) -> dict:
    root: dict = {}
    for word in sorted(vocab):
        if not 2 <= len(word) <= 80:
            continue
        node = root
        for char in word:
            node = node.setdefault(char, {})
        node[None] = word
    return root


def _vocabulary_spans(text: str, vocab: frozenset[str]):
    """Longest non-overlapping dictionary spans, without a five-letter cap."""
    trie = _vocabulary_trie(vocab)
    i = 0
    while i < len(text):
        node = trie
        end, match = i, ""
        for j in range(i, len(text)):
            node = node.get(text[j])
            if node is None:
                break
            word = node.get(None)
            if not word:
                continue
            # A Latin token is not present merely because it is a substring
            # of another token (RAD in RADICAL, for example).
            if (word[0].isascii() and word[0].isalnum() and i
                    and text[i - 1].isascii() and text[i - 1].isalnum()):
                continue
            if (word[-1].isascii() and word[-1].isalnum() and j + 1 < len(text)
                    and text[j + 1].isascii() and text[j + 1].isalnum()):
                continue
            end, match = j + 1, word
        if match:
            yield i, end, match
            i = end
        else:
            i += 1


_ZH_PREFIX_BREAKS = re.compile("|".join(
    re.escape(word) for word in sorted(
        {word for word in _ZH_FUNCTION_2 | _PRONOUNS_ZH if len(word) >= 2}
        | {"参加", "举办", "加入", "参观", "前往", "走进", "抵达", "召开"},
        key=lambda word: (-len(word), word))))


def extract_zh_candidates_from_story(
    text: str,
    story_key: str,
    discovered: set[str],
    protagonists_norm: set[str],
    max_terms: int = 60,
    seed: Optional[set[str]] = None,
) -> list[tuple[str, bool]]:
    """Return source-backed spans, prioritizing explicit names before a cap.

    Dictionary, quote and morphology channels share span coverage. A weaker
    channel cannot add fragments inside a known name, nor re-add an untrimmed
    mixed-script phrase after another channel found its boundary.
    """
    if max_terms <= 0:
        return []
    seed = seed or set()
    vocab = frozenset(discovered) | frozenset(seed)
    found: dict[str, tuple[str, bool, int, int]] = {}

    def add(w: str, quoted: bool, priority: int) -> bool:
        w = w.strip("　 ")
        key = normalize_name(w)
        if not 2 <= len(w) <= 80 or not key or key in protagonists_norm:
            return False
        if w in _PRONOUNS_ZH and w not in seed:
            return False
        if re.search(r"[，。！？、；：…—]", w):
            return False
        statistical = (w in discovered and w not in _ZH_FUNCTION_2
                       and w not in _PRONOUNS_ZH
                       and not re.search(r"[的了吗呢吧呀啊]$", w))
        if w not in seed and not (statistical or _is_content_word(w) or looks_like_proper_surface(w)):
            return False
        previous = found.get(key)
        if previous is None:
            found[key] = (w, quoted, priority, len(found))
        else:
            found[key] = (previous[0], previous[1] or quoted,
                          min(previous[2], priority), previous[3])
        return True

    for raw in text.splitlines():
        seg = strip_speaker(raw.strip())
        if not seg:
            continue
        covered: list[tuple[int, int]] = []
        statistical_spans: set[tuple[int, int]] = set()
        rejected_quotes: list[tuple[int, int]] = []

        def overlaps(start: int, end: int) -> bool:
            return any(start < right and end > left for left, right in covered)

        # Explicit quotations preserve one bare surface, with the quote signal.
        for match in _QUOTE_RE.finditer(seg):
            value = match.group(1).strip("　 ")
            limit = 40 if looks_like_proper_surface(value) else 24
            if len(value) <= limit and add(value, True, 0):
                covered.append(match.span())
            else:
                rejected_quotes.append(match.span())

        for start, end, value in _vocabulary_spans(seg, vocab):
            if overlaps(start, end):
                continue
            if add(value, False, 0 if value in seed else 1):
                covered.append((start, end))
                if value not in seed:
                    statistical_spans.add((start, end))

        # A rejected quoted utterance must not immediately reappear as weak
        # Latin/suffix fragments. Explicit dictionary names can still match it.
        covered.extend(rejected_quotes)

        for match in _MIXED_PROPER_RE.finditer(seg):
            value = mixed_proper_surface(match)
            # Prefer the longest nominal ending. The regex may greedily
            # include a predicate after it; that predicate is not part of a name.
            nominal_ends = [m.end() for suffix in _PROPER_SUFFIXES
                            for m in re.finditer(re.escape(suffix), value)]
            if nominal_ends:
                value = value[:max(nominal_ends)]
            end = match.start() + len(value)
            if not overlaps(match.start(), end) and add(value, False, 2):
                covered.append((match.start(), end))

        # Backtrack from a nominal ending, bounded by lexical syntax rather
        # than treating every occurrence of 来/下/之 as a function character.
        for suffix in sorted(_PROPER_SUFFIX_HANZI, key=len, reverse=True):
            for match in re.finditer(re.escape(suffix), seg):
                start, end = match.span()
                head = start
                while head and start - head < 20 and re.fullmatch(r"[一-鿿]", seg[head - 1]):
                    head -= 1
                prefix = seg[head:start]
                cuts = list(_ZH_PREFIX_BREAKS.finditer(prefix))
                if cuts:
                    head += cuts[-1].end()
                # Clear grammatical edges, without deleting valid internal
                # characters such as 地下 or 森之宫.
                while head < start and seg[head] in "的是在从到把被给让去":
                    head += 1
                # A complete nominal ending may extend a shorter statistical
                # word (music -> music festival). Statistics are not a
                # protected entity boundary. Explicit dictionary/quote spans
                # remain protected, as do statistical spans crossing this one.
                intersections = [(left, right) for left, right in covered
                                 if head < right and end > left]
                if any((left, right) not in statistical_spans
                       or not (head <= left and right <= end)
                       for left, right in intersections):
                    continue
                if head == start:
                    # The suffix vocabulary also contains ordinary nouns
                    # (schools, festivals, parks). At a real lexical edge the
                    # noun itself is a valid candidate, without a proper-name
                    # prefix. Do not truncate a longer Han compound instead.
                    following = seg[end:end + 1]
                    if (len(suffix) < 2 or (following and re.fullmatch(r"[一-鿿]", following)
                            and following not in _ZH_FUNCTION_CHARS)):
                        continue
                value = seg[head:end]
                if add(value, False, 2):
                    covered.append((head, end))

        from sekaisync.termindex import _local_latin_candidates
        for value in _local_latin_candidates(seg):
            for match in re.finditer(re.escape(value), seg):
                if not overlaps(*match.span()) and add(value, False, 2):
                    covered.append(match.span())
    ordered = sorted(found.values(), key=lambda item: (item[2], item[3]))
    return [(value, quoted) for value, quoted, _priority, _order in ordered[:max_terms]]


# ── 对齐 ────────────────────────────────────────────────────────────

def align_zh_candidate(
    term: str,
    target_language: str,
    groups: dict,
    src_stories: set[str],
    idf: dict[tuple[str, str], float],
    vocab: Optional[dict[str, set[str]]] = None,
) -> str:
    """Use the shared evidence-based aligner, keeping the legacy entry point.

    Position matching, language aliases, lexical spans and ambiguity handling
    must not drift into a second percentage-window/longest-candidate algorithm.
    """
    from sekaisync.termindex import align_term_by_frequency
    return align_term_by_frequency(
        term, "zh_hans", target_language, groups, idf,
        src_stories=src_stories, vocab=vocab)


# ── 管线入口 ────────────────────────────────────────────────────────

def extract_terms_zhfirst(
    pages: Iterable[dict],
    target_languages: Iterable[str],
    glossary: Iterable[Any],
    min_freq: int = 2,
    cache_dir: Optional[Path] = None,
    do_align: bool = False,
    llm: Any = None,
) -> list[ZhFirstTerm]:
    """简中主位管线入口。返回按 weight 降序的 ZhFirstTerm 列表。

    ``do_align=False`` 时只输出简中候选（快，~2 分钟）；True 时额外做同点位
    四语对齐（需跑全量对齐，约 20 分钟，适合后台批量）。
    ``llm`` 提供时对候选做语义过滤（区分内容承载词与碎片），否则降级规则。"""
    targets = [t for t in target_languages if t and t != "zh_hans"]
    glossary = list(glossary)
    groups = group_pages_by_story(pages)
    protagonists, official = build_zhfirst_blocklist(glossary)
    protagonists_norm = {normalize_name(p) for p in protagonists}

    released_keys = {k for k, v in groups.items() if len(v) > 1}
    zh_texts = [
        groups[sk]["zh_hans"]["text"]
        for sk in sorted(released_keys)
        if "zh_hans" in groups[sk]
        and str(groups[sk]["zh_hans"].get("text", "")).strip() not in ("", "[未翻译]")
    ]
    zh_texts = ["\n".join(strip_speaker(line) for line in text.splitlines())
                for text in zh_texts]
    discovered = discover_words(zh_texts, min_freq=min_freq, min_cohesion=8.0,
                                min_entropy=1.0, max_chars=3_500_000,
                                boundary_stop_chars=_ZH_FUNCTION_CHARS)
    discovered |= _discover_content_words(zh_texts, min_freq=min_freq,
                                          max_chars=3_500_000)

    # 人工标注种子：把 data/term-annotations.json 的词并入候选词典，2 字通用词
    # （贝斯/网球/美元）靠人工词表直接命中，不再依赖统计。
    seed = _load_manual_seed()
    # Dictionary presence is a discovery hint, never proof that its other
    # language names are official. Only ``official`` below may inherit names.
    from sekaisync.termindex import NOUN_KINDS
    glossary_seed = {
        str(value) for term in glossary
        if getattr(term, "kind", "") in NOUN_KINDS
        for lang, value in (getattr(term, "names", {}) or {}).items()
        if lang == "zh_hans" and value and 2 <= len(str(value)) <= 80
    }
    extraction_seed = seed | glossary_seed

    # Pass 1: per-story candidate harvest — only released stories (multi-lang)
    # where cross-language alignment is possible; unreleased ja-only stories
    # have no zh text to extract from anyway.
    term_stories: dict[str, set[str]] = defaultdict(set)
    term_lines: dict[str, int] = defaultdict(int)
    term_quoted: dict[str, bool] = defaultdict(bool)
    for sk in sorted(released_keys):
        by = groups[sk]
        zpg = by.get("zh_hans")
        if zpg is None:
            continue
        text = str(zpg.get("text", ""))
        if not text.strip() or text.strip() == "[未翻译]":
            continue
        for surf, quoted in extract_zh_candidates_from_story(
            text, sk, discovered, protagonists_norm,
            max_terms=max(1, len(text)), seed=extraction_seed
        ):
            term_stories[surf].add(sk)
            term_lines[surf] += sum(
                surf in strip_speaker(line) for line in text.splitlines())
            if quoted:
                term_quoted[surf] = True

    # Classification: 非日常 = quoted | bursty | official-name match
    terms: dict[str, ZhFirstTerm] = {}
    # zh→official 反查（候选是简中，official 字典 key 是 ja）
    zh_to_official: dict[str, dict] = {}
    for entry in official.values():
        for lang in ("zh_hans", "zh_hant", "zh_tw"):
            v = entry.get(lang)
            if v and 2 <= len(v) <= 40:
                if v not in zh_to_official:
                    zh_to_official[v] = dict(entry)
                else:
                    previous = zh_to_official[v]
                    zh_to_official[v] = {
                        lang: value for lang, value in previous.items()
                        if entry.get(lang) == value
                    }
    # 去碎片：n-gram 切碎的短语（很有辨/有辨识度/造上就）不是真词。
    # 只保留：① 官方命中 ② 种子词表命中 ③ 引号词 ④ discovered 词
    # ⑤ 非 everyday（突发/引号）且通过语义过滤（规则或 LLM）
    seed_norm = {normalize_name(w) for w in extraction_seed}
    disc_norm = {normalize_name(w) for w in discovered}
    # 需要语义过滤的词：非官方/非种子/非引号/discovered 的高频候选
    needs_filter = [
        canon for canon in term_stories
        if normalize_name(canon) not in official
        and canon not in zh_to_official
        and normalize_name(canon) not in seed_norm
        and normalize_name(canon) not in disc_norm
        and not term_quoted.get(canon)
    ]
    llm_kept: set[str] = set()
    if needs_filter:
        llm_kept = llm_filter_terms(needs_filter, llm)
    for canon, stories in term_stories.items():
        key = normalize_name(canon)
        if key in official or canon in zh_to_official or key in seed_norm or key in disc_norm or term_quoted.get(canon):
            pass
        elif canon in llm_kept:
            pass
        else:
            continue
        tf = ZhFirstTerm(canonical=canon, stories=set(stories))
        tf.lines_n = term_lines.get(canon, 0)
        tf.names["zh_hans"] = canon
        tf.everyday = not looks_like_proper_noun(
            stories_n=len(stories),
            lines_n=tf.lines_n,
            total_stories=len(released_keys),
            quoted=term_quoted.get(canon, False) or looks_like_proper_surface(canon),
        )
        if key in official:
            tf.official = True; tf.everyday = False
            tf.names.update({k: v for k, v in official[key].items() if v})
        elif canon in zh_to_official:
            tf.official = True; tf.everyday = False
            tf.names.update({k: v for k, v in zh_to_official[canon].items() if v})
        # Assignment must NOT live inside a specific branch: every retained
        # path (official / seed / discovered / quoted / llm-kept) has to land
        # in the result. Nesting it under `elif canon in zh_to_official` threw
        # away every other term and made the pipeline return 0 forever.
        terms[canon] = tf

    # Alignment phase (optional; disabled by default for speed).
    if not do_align:
        # 排序：专名度优先 —— official > 非everyday(引号/突发) > 普通高频词。
        # 普通高频词（这样啊/谢谢你/真是）story 数多但专名度低，排后。
        def rank(t: ZhFirstTerm) -> tuple:
            if t.official:
                return (0, -t.lines_n, t.canonical)
            if not t.everyday:
                return (1, -t.lines_n, t.canonical)
            return (2, -t.lines_n, t.canonical)
        result = sorted(terms.values(), key=rank, reverse=False)
        return result

    from sekaisync.termindex import build_alignment_resources

    vocab, idf_map = build_alignment_resources(
        groups, ["zh_hans"] + targets, glossary, cache_dir
    )

    # ``everyday`` is a ranking hint, not a ban on translating common content
    # words. Corpus evidence, rather than rarity, decides whether a slot wins.
    alignable = [tf for tf in terms.values() if not tf.official and tf.stories]
    align_cache: dict[tuple[str, str], str] = {}
    for tf in alignable:
        for tl in targets:
            key = (tf.canonical, tl)
            if key not in align_cache:
                align_cache[key] = align_zh_candidate(
                    tf.canonical, tl, groups, tf.stories, idf_map, vocab=vocab
                )
            got = align_cache[key]
            if got:
                tf.names[tl] = got

    result = sorted(terms.values(), key=lambda x: (-x.lines_n, x.canonical))
    return result
