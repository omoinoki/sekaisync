"""三位一体用语刮削编排器（隔离实验：`experiment/trinity/`）。

三组对照实验（``experiment/zh-en-tw/COMPARISON.md``）已经定性：三组不是互斥替代，
而是**互补的三个信号源**，但都栽在同一个缺陷上——用行位预测取候选，通用词就被
对齐到感叹词/人名。本模块把三条路按"主干 + 2 辅助"编成一条**协同**管线，与既有
``penetrate_channels.penetrate_layered``（三通道**并列**、按固定优先级覆盖）的区别
在于三点协同机制：

1. **互补取值**：三路各自独立产出后**合并去重**。
   - 主干 ``trunk``：ja 主位分布对齐（``termindex.align_term_by_frequency``，
     现算法，精度背书）→ 抓义译名（アップルパイ→苹果派）。
   - 辅助 1 ``hub``：ja 行位 → zh_hans/英语拉丁形态直取 → zh_hant/ko 同点位译名
     验证 → 回填 zh_hans（组 2 的机制，补了组 2 的三处缺陷）→ 抓人名/品牌
     （朝比奈→Asahina）。
   - 辅助 2 ``translit``：ja 片假名 → 罗马音 → 英语（``romaji.similarity`` 硬门控）
     → 抓缩写/音译（ニーゴ→N25、セカイ→SEKAI）。
   三路的**作用域互补**：主干吃非拉丁词，hub 吃无形态特征的简中侧专名，
   translit 吃片假名串；合并后总采纳数应大于任一单路（协同的最低判据，
   verify.py 对此断言）。

2. **交叉验证增益**：同一 term 被 ≥2 路独立命中**且译名一致**（归一化后同值）
   → ``agreement`` 计数 +1，置信度上台阶（``agreement_boosted`` 计数）。
   一致性按**语言槽**判定：trunk 给 zh_hans 的 `世界` 与 translit 给 en 的 `SEKAI`
   不是同一语言槽，不算 agreement；trunk 的 `摄影大赛` 与 hub 的 `摄影大赛` 才算。
   **不一致**（同语言槽不同值）→ 进 ``conflicts``，交人工/智能体裁决，
   绝不静默择一。

3. **相互补位**：
   - 主干因严格门控未穿透、但 translit 以 ``sim >= 0.99`` 命中的 → 采纳
     （记 ``rescued_by``）；
   - 辅助产出的低置信结果，若有主干同点证据（主干在任一语言给出同一值）支持
     → 置信度加分、升为采纳（记 ``promoted_by_trunk``）；
   - 三角闭环（``verify_triangle``）在 glossary 未收录该术语时降级为**只做
     目标语言锚定**（``max_aux_required=0``）；未通过锚定的 en 对进 ``pending``，
     **不丢弃**。

硬约束：零第三方依赖；只读 ``sekaisync/`` 既有模块（不修改任何既有文件）；
Python 3.10+。

性能：行位索引走 ``penetrate_channels._build_term_line_index`` 的两字符分桶
（一次语料扫描，同时拿到行号）；主干所需的 ``src_stories`` 由同一索引与
``build_pair_story_index`` 的语言倒排取交集得出，避免逐 (term, story) 全文扫描；
正文/行/配对故事/拉丁候选/行位索引本模块自带一次调用内缓存。

性能修复记录（2026-09，200 个 event_story 故事，1109s → ~25s）：本模块一度在
200 故事上退化到 1109 秒（复现实测 1700 秒）。根因不在本模块的业务逻辑，而是
**上游 ``zhfirst.strip_speaker`` 的正则灾难性回溯**被
``penetrate_channels._latin_candidates_for_line → extract_latin_candidates``
反复触发：``SPEAKER_RE_MULTI``（``^(?:[^：:]{1,12}[・&、,， ])+[^：:]{1,12}[：:]``）
在**无冒号**的长行上必须枚举全部切分才能确认失败，单行最高 26 秒。该函数被
hub/translit 的每个行位窗口调用一次，于是在 200 故事上累计 1600 秒（占总耗时
95%）。修法见 :func:`_latin_candidates_fast`（无冒号 ⇒ ``strip_speaker`` 恒等，
跳过回溯）与 :class:`_FastAligner`（主干的行/候选/门控缓存 + 预编译中文切分
正则）。所有快速路径都与上游**逐值等价**（A/B 在真实语料上 0 mismatch），
修复前后 accepted/pending/conflicts 完全一致（119 / 341 / 0）。

公开 API（主线集成依赖，签名固定）::

    scrub_trinity(groups, stories, candidates, *, ...) -> dict
    arbitrate(term, proposals, *, glossary_names=None) -> dict
    write_scrub_report(result, out_dir) -> dict
    compare_with_baseline(result, baseline) -> dict
"""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "scrub_trinity",
    "arbitrate",
    "write_scrub_report",
    "compare_with_baseline",
    "build_candidate_pool",
    "CHANNEL_PRIORITY",
]

# ── 依赖既有模块（只读，不修改任何既有文件）──────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from sekaisync import termindex  # noqa: E402
from sekaisync.candidate_tiers import (  # noqa: E402
    Tier,
    classify,
    classify_batch,
    tier_summary,
)
from sekaisync.normalize import normalize_name  # noqa: E402
from sekaisync.penetrate_channels import (  # noqa: E402
    _EN_PROPER,
    _EN_SENTENCE_INITIAL_AMBIGUOUS,
    _EN_STOPWORDS,
    _build_term_line_index,
    _glossary_name_table,
    _page_for,
    _paired_stories,
    extract_latin_candidates,
    register_alignment_context,
    register_glossary_names,
    verify_triangle,
)
from sekaisync.romaji import (  # noqa: E402
    is_abbrev_form_only,
    is_generic_katakana,
    is_plausible_translation,
    similarity,
)
from sekaisync.zhfirst import strip_speaker  # noqa: E402


# ── 常量与小工具 ────────────────────────────────────────────────────

# 通道优先级（数值越小越优先），按**内部分档名**索引。读法：L0 官方 >
# translit(sim≥0.6，含实验里恒为 1.00 的真音译) > trunk(门控通过) >
# translit 边缘档(0.5-0.6) > hub。低分 translit 排在 trunk 之后，是因为实验里
# sim 0.5-0.6 这一段仍有噪声（コンテスト→Kohane 0.53），而 trunk 是精度背书；
# trunk 缺席时它才起作用。hub 恒在末位：它是召回来源（人名/品牌），精度最低，
# 只在其它三路都沉默时说话。
CHANNEL_PRIORITY: dict[str, float] = {
    "L0": 0.0,
    "translit": 1.0,
    "trunk": 2.0,
    "translit_low": 3.0,
    "hub": 4.0,
}

# 对外只报三路 + L0，所以内部分档名要折回这四个名字（见 _canonical_channel）。
_DISPLAY_CHANNEL = {"translit_low": "translit", "L0_unverified": "L0"}


def _canonical_channel(channel: str) -> str:
    """把内部分档名折回对外通道名（translit_low → translit）。"""
    return _DISPLAY_CHANNEL.get(channel, channel)


def _channel_rank(label: str) -> float:
    """内部分档名的优先级数值（未知档位给最低）。"""
    return CHANNEL_PRIORITY.get(label, 9.0)

_KATA_RUN_RE = re.compile(r"[\u30A0-\u30FF][\u30A0-\u30FF\u30FC\u30FB]{2,40}")
_KATA_PURE_RE = re.compile(r"^[\u30A1-\u30FA\u30FC\u30FB]{3,}$")
# 引号口径与 experiment/zh-en-tw/compare.py 的 group_baseline 完全一致
# （`[「“][^」”]{2,20}[」”]`），保证候选池与基线同口径、可比。
_QUOTED_RE = re.compile(r"[「“]([^」”]{2,20})[」”]")
_KANJI_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_LATIN_RE = re.compile(r"[A-Za-z]")

# 译名比较键：NFKC + casefold + 去装饰/空白/中点。比 normalize_name 稍松
# （normalize_name 不吃中点与装饰符），专门用于"跨通道同值"判定。
_NAME_STRIP_RE = re.compile(r"[\u3000\s\u30fb\u2026\u2014♡♪☆★\"'`~～]+")

# termindex 候选抽取里的发言人剥离模式（逐字复制上游内联写法，保证同口径）。
_SEMICOLON_SPEAKER_RE = re.compile(r"^[^：:]{1,30}[：:]\s*")


def _name_key(value: str) -> str:
    """译名比较键：NFKC + casefold + 去装饰/空白/中点。"""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).casefold()
    return _NAME_STRIP_RE.sub("", text)


def _is_pure_katakana(term: str) -> bool:
    """整串片假名（含 ー・）且长度 ≥3 —— translit 通道的作用域。"""
    t = unicodedata.normalize("NFKC", (term or "").strip())
    return bool(_KATA_PURE_RE.match(t))


def _is_kanji_only(term: str) -> bool:
    """纯汉字串（人名/地名的主体形态：无形态特征可抓，只能靠背书）。"""
    t = (term or "").strip()
    return bool(t) and all(_KANJI_RE.match(ch) for ch in t)


def _canonical_channel(channel: str) -> str:
    """把内部细分的通道名折回对外四通道（translit_low → translit）。"""
    if channel == "translit_low":
        return "translit"
    if channel == "L0_unverified":
        return "L0"
    return channel


# ── 拉丁候选抽取的快速路径（性能关键，见下）────────────────────────

# 上游 ``zhfirst.SPEAKER_RE_MULTI`` =
# ``^(?:[^：:]{1,12}[・&、,， ])+[^：:]{1,12}[：:]\s*``：嵌套 ``+`` 与可重叠的
# ``[^：:]`` 字符类在**没有冒号**的长行上会灾难性回溯——正则引擎必须枚举
# ``+`` 的所有切分方式才能确认失败。实测（本机 Python 3.13）一行 145 字符的
# 英文台词要 26.3 秒、173 字符要 17.7 秒；而 200 故事刮削里这样的行有数万条，
# 全部经 ``extract_latin_candidates → strip_speaker`` 经过，于是
# ``_channel_translit`` 独自烧掉 1609 秒（占 1700 秒总耗时的 95%）。
#
# 两条 speaker 模式都以字面 ``[：:]`` 收尾，因此**行内一个冒号都没有时
# ``strip_speaker`` 恒等**（模式不可能匹配，函数返回原行对象）。据此做快路径：
# 先看行内有无冒号，没有就跳过 ``strip_speaker``。这不是近似——判定条件正是
# 被跳过模式自身的必要条件，所以结果与上游逐字节一致（perf_probe 会做全量
# 等价性抽查）。
def _has_speaker_colon(line: str) -> bool:
    """行内是否可能出现 `发言人：` 标签（两条 speaker 模式的必要条件）。"""
    return ("：" in line) or (":" in line)


def _strip_speaker_fast(line: str) -> str:
    """``zhfirst.strip_speaker`` 的等价快速版（无冒号时恒等，跳过回溯）。"""
    if _has_speaker_colon(line):
        return strip_speaker(line)
    return line


def _latin_candidates_from_text(text: str) -> list[str]:
    """``extract_latin_candidates`` 的抽取循环（作用于已剥发言人的正文）。

    ``strip_speaker`` 被硬编码在上游函数体内、无法注入，所以这里忠实复制它的
    抽取段：形态规则用到的常量（``_EN_PROPER`` / ``_EN_STOPWORDS`` /
    ``_EN_SENTENCE_INITIAL_AMBIGUOUS``）全部**直接引用上游对象**，只复制控制流，
    避免口径漂移。仅在"行内无冒号 ⇒ strip_speaker 恒等"时走这条路。
    """
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


def _latin_candidates_fast(line: str) -> list[str]:
    """``_latin_candidates_for_line`` 的等价快速版（避开上游回溯热点）。

    有冒号 ⇒ 走上游原函数（此时模式很快失败/成功，实测 1-4 微秒）；
    无冒号 ⇒ ``strip_speaker`` 恒等，直接对原行跑抽取循环，省掉灾难性回溯。
    """
    if _has_speaker_colon(line):
        return extract_latin_candidates(line)
    return _latin_candidates_from_text(line)


# translit 的"边缘档"下界：实验里 sim 0.5-0.6 是噪声与真值交叠区
# （ステージ→Wonder Stage 0.35、ライブ→Saki 0.44 被拒；实测 コンテスト→Kohane
# 0.533 为假阳性），这一档需要另一路同值佐证才采纳；≥0.6 直接采纳
# （ニーゴ→N25 恰好 0.6，是实验报告点名的"极有价值"对）。
_TRANSLIT_CONFIRMED_SIM = 0.6


# ── 语料缓存（正文 / 行 / 配对故事）──────────────────────────────────

class _Corpus:
    """(groups, stories) 的一次性视图：正文、行、配对故事的缓存。

    **语料口径（与组 1 基线一致，重要）**：``stories`` 是本次要刮削的故事集合
    （候选来源与报告范围），但**跨故事散布证据与行位索引使用 ``groups`` 全量**。
    理由：分布对齐（主干）与跨故事投票（hub/translit）本质上是"语料级统计"，
    只在 200 个故事里对齐会把 アップルパイ（77 个故事的证据）这样的真值饿死
    （实测：限制到 200 故事窗口时主干对它完全沉默，全量则稳定给出「苹果派」）。
    组 1 的 78 对同样是"候选来自窗口、证据来自全语料"的口径，两者必须对齐才
    可比。``experiment/zh-en-tw/compare.py`` 的 ``group_baseline`` 也是这么做的。

    ``penetrate_channels`` 有同类的模块级缓存，但那会与主线共享状态；本模块自带
    一份，生命周期跟随编排器的一次调用，退出即释放。
    """

    def __init__(self, groups: dict, stories: Iterable[str]) -> None:
        self.groups = groups
        self.stories = [sk for sk in stories if sk in groups]
        self.story_set = set(self.stories)
        self._lines: dict[tuple[str, str], list[str]] = {}
        self._text: dict[tuple[str, str], str] = {}
        self._paired: dict[tuple[str, str], set[str]] = {}
        # 行 → 拉丁专名候选。hub/translit 的行位窗口会对同一行反复取候选
        # （同一术语在多个故事里预测到同一句台词），按行缓存把抽取降到一次。
        self._latin: dict[str, list[str]] = {}
        # (语言, scope) → (行位索引, 已覆盖术语集合)。四条通道复用同一批故事，
        # 见 line_index。
        self._index: dict[tuple[str, tuple[str, ...]], tuple[Any, set[str]]] = {}
        # 术语 → 出现过它的故事并集（由 line_index 顺手维护，供锚定裁剪复用）。
        self._term_stories: dict[str, set[str]] = {}
        # 故事集合 → 裁剪视图（对象长期持有，给上游的 id() 缓存一个稳定标识）。
        self._views: dict[frozenset[str], _StoryScopedView] = {}
        # (term, en 值) → 锚定结果（见 _anchor_check：扫描面已裁剪到命中故事）。
        self._anchors: dict[tuple[str, str], dict] = {}

    def page(self, story_key: str, language: str) -> Any:
        return _page_for(self.groups, story_key, language)

    def text(self, story_key: str, language: str) -> str:
        key = (story_key, language)
        cached = self._text.get(key)
        if cached is None:
            page = self.page(story_key, language)
            cached = str(page.get("text", "")) if page is not None else ""
            self._text[key] = cached
        return cached

    def lines(self, story_key: str, language: str) -> list[str]:
        key = (story_key, language)
        cached = self._lines.get(key)
        if cached is None:
            cached = [ln.strip() for ln in self.text(story_key, language).splitlines()
                      if ln.strip()]
            self._lines[key] = cached
        return cached

    def paired(self, language: str, source_language: str) -> set[str]:
        """同时具备源语言与目标语言的故事（全语料口径，见类文档）。"""
        key = (language, source_language)
        cached = self._paired.get(key)
        if cached is None:
            cached = _paired_stories(self.groups, language, source_language)
            self._paired[key] = cached
        return cached

    def latin_candidates(self, line: str) -> list[str]:
        """一行的拉丁专名候选（等价 ``_latin_candidates_for_line``，带本调用缓存）。

        上游 ``_latin_candidates_for_line`` 自带模块级缓存，但那会与主线共享
        状态；本模块自带一份，生命周期跟随编排器的一次调用。抽取本身走
        :func:`_latin_candidates_fast`（绕开 ``strip_speaker`` 的灾难性回溯，
        见那里的注释），结果与上游逐条一致。
        """
        cached = self._latin.get(line)
        if cached is None:
            cached = _latin_candidates_fast(line)
            self._latin[line] = cached
        return cached

    def line_index(
        self,
        scope: Iterable[str],
        language: str,
        terms: Iterable[str],
    ) -> Any:
        """``_build_term_line_index`` 的**按 (scope, 语言, 覆盖术语) 去重**包装。

        四条通道各自对同一批 ``scoped`` 故事建一次 (term → 故事 → 行号) 索引，
        而索引的内容只依赖 (故事集合, 语言)：候选集合只决定"要不要扫这个
        term"，命中本身是按位置 ``startswith`` 得到的，**与候选集合无关**。
        因此同一 (scope, 语言) 下用更大的候选集合建一次，即可覆盖之后更小的
        候选集合——本方法记录每个键已覆盖的术语集合，请求的术语被覆盖就直接
        复用，否则用并集重建一次（先热后冷，最多重建一次）。

        命中范围与单独建索引**完全一致**（只含 ``scope`` 里真正含该 term 的
        故事），所以各通道拿到的 ``len(hits)`` 与逐条遍历不变，结果等价。
        """
        key = (language, tuple(sorted(scope)))
        entry = self._index.get(key)
        want = {t for t in terms if t}
        if entry is not None and want <= entry[1]:
            return entry[0]
        covered = want if entry is None else (entry[1] | want)
        built = _build_term_line_index(self.groups, sorted(set(scope)), language, sorted(covered))
        self._index[key] = (built, covered)
        # 术语 → 出现故事（供锚定裁剪用；多份索引取并集，超集不影响判定）。
        for term, stories in built.hits.items():
            bucket = self._term_stories.get(term)
            if bucket is None:
                bucket = set()
                self._term_stories[term] = bucket
            bucket |= set(stories)
        return built

    def stories_with(self, term: str) -> set[str] | None:
        """已建索引里 term 出现的故事（未索引过返回 ``None``）。"""
        found = self._term_stories.get(term)
        return None if found is None else set(found)

    def anchor_view(self, stories: Iterable[str]) -> "_StoryScopedView":
        """按故事集合裁剪的 groups 视图（按集合内容缓存，保持对象稳定）。"""
        key = frozenset(stories)
        view = self._views.get(key)
        if view is None:
            view = _StoryScopedView(self.groups, key)
            self._views[key] = view
        return view


def _predict_index(src_idx: int, src_n: int, tgt_n: int) -> int:
    """行位预测（与 termindex/penetrate_channels 同口径）。"""
    if src_n <= 1 or tgt_n <= 1:
        return 0
    return round(src_idx * (tgt_n - 1) / (src_n - 1))


class _StoryScopedView(dict):
    """只暴露 ``stories`` 的 ``groups`` 视图（供 ``verify_triangle`` 裁剪扫描面）。

    三角闭环（``penetrate_channels.verify_triangle`` → ``_locate_term_lines``）
    按 "story_scope = 具备源/目标语言的故事" 遍历，而那个 scope 在**全量语料**
    口径下是 ~13.5k 个故事。但闭环只可能命中"term 真正出现的故事"——其余故事
    在 ``_locate_term_lines`` 里必然被 ``term not in text`` 否掉。把视图缩到
    term 的出现故事集，扫描面就从全量降到该术语的实际命中数，**判定结果不变**。

    实现为 dict 子类而非新建 dict，是为了给 ``_page_for`` / ``_lang_page_index``
    / ``_story_text_cache``（都按 ``id(groups)`` 做模块级缓存）一个**稳定且不
    会被回收复用**的标识：视图对象由 :meth:`_Corpus.anchor_view` 长期持有。
    """

    __slots__ = ("_scoped_keys",)

    def __init__(self, groups: dict, stories: Iterable[str]) -> None:
        super().__init__(groups)
        self._scoped_keys = frozenset(stories)

    # ── 下面全部按裁剪后的故事集答复 ────────────────────────────────
    def __iter__(self):
        return iter(self._scoped_keys)

    def __len__(self) -> int:
        return len(self._scoped_keys)

    def __contains__(self, key) -> bool:
        return key in self._scoped_keys

    def keys(self):
        return list(self._scoped_keys)

    def values(self):
        return [dict.get(self, k) for k in self._scoped_keys]

    def items(self):
        return [(k, dict.get(self, k)) for k in self._scoped_keys]

    def get(self, key, default=None):
        if key in self._scoped_keys:
            return dict.get(self, key, default)
        return default


# ── 第 0 层：候选池与分层（三通道共用）──────────────────────────────

def build_candidate_pool(
    groups: dict,
    stories: list[str],
    *,
    source_language: str = "ja",
) -> tuple[list[str], set[str]]:
    """现行 ja 侧候选池：片假名串 ∪ 叙事引号词 ∪ 统计发现词。

    与 ``experiment/zh-en-tw/compare.py`` 的 ``group_baseline`` 同口径（片假名
    长度 ≥3、引号词 2-20 字、discovered 里含汉字或片假名的词），保证与基线可比。
    返回 ``(候选列表, discovered 集合)``；discovered 供分层与门控复用。
    """
    from sekaisync.wordseg import discover_words

    texts = [
        str(page.get("text", ""))
        for page in (
            termindex._group_page(groups.get(sk, {}), source_language) for sk in stories
        )
        if page is not None
    ]
    discovered = discover_words(texts, min_freq=3, max_chars=2_000_000)
    pool: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        t = (term or "").strip()
        if len(t) < 2 or t in seen:
            return
        seen.add(t)
        pool.append(t)

    for text in texts:
        for match in _KATA_RUN_RE.finditer(text):
            add(match.group(0))
        for match in _QUOTED_RE.finditer(text):
            add(match.group(1))
    for word in discovered:
        if (any("\u4e00" <= ch <= "\u9fff" for ch in word)
                or any("\u30a0" <= ch <= "\u30ff" for ch in word)):
            add(word)
    return pool, discovered


# ── 通道 1 / 主干：trunk（ja 主位分布对齐）──────────────────────────

# 主干所需的 ``_split_zh_run`` 快速版：上游每次调用都用 ``re.escape`` 重建
# 两条正则（42.6M 次 escape、51 条函数词 + 65 个功能字），在本工作负载上
# 70 µs/行 → 3.5 µs/行（20 倍）。两条模式只依赖 ``_ZH_FUNCTION_WORDS`` /
# ``_ZH_FUNCTION_CHARS`` 这两个模块常量，缓存编译结果不改变任何输出
# （A/B 在真实语料 3432 次对上逐值比对，mismatches=0）。
_ZH_WORD_SPLIT_RE = re.compile(
    "|".join(re.escape(w) for w in sorted(termindex._ZH_FUNCTION_WORDS, key=len, reverse=True))
)
_ZH_CHAR_SPLIT_RE = re.compile(
    "[" + re.escape("".join(sorted(set(termindex._ZH_FUNCTION_CHARS)))) + "]"
)


def _split_zh_run_cached(run: str, *, enumerate_windows: bool = True) -> list[str]:
    """``termindex._split_zh_run`` 的等价快速版（正则预编译）。"""
    pieces = _ZH_WORD_SPLIT_RE.split(run)
    out: list[str] = []
    for piece in pieces:
        for sub in _ZH_CHAR_SPLIT_RE.split(piece):
            if len(sub) < 2 or len(sub) > 8:
                continue
            out.append(sub)
            if not enumerate_windows:
                continue
            n = len(sub)
            for wlen in range(2, n):
                for start in range(0, n - wlen + 1):
                    out.append(sub[start:start + wlen])
    return list(dict.fromkeys(out))


class _FastAligner:
    """``termindex.align_term_by_frequency`` 的**带缓存等价实现**（主干专用）。

    上游实现对本模块的调用形态（一次刮削里 3432 次 (term, 目标语言) 调用、
    313k 次 (调用 × 故事) 访问）反复做三件可缓存的事：

    1. 每个 (故事, 语言) 页的 ``splitlines`` + ``strip``（578,974 次 ≈ 24s）；
    2. 每个源故事的 ``[i for i, ln in enumerate(src_lines) if term in ln]``
       —— 而 :class:`_Corpus` 侧的行位索引**已经**给出这些行号（312k 次全行
       扫描 ≈ 12s）；
    3. 逐行的 ``_local_translation_candidates``（672k 个不同 (行, 语言)，
       同一行在多个术语间反复出现）。

    缓存键全部落在"页正文"与"行文本"上——都是输入数据的纯函数，因此
    **结果与上游逐字节一致**（A/B 在 200 故事真实负载上 3432 次调用全部相等，
    见 perf_probe 的等价性说明）。``align_term_by_frequency`` 的采样、门控、
    并列判定（含"75% 内取最长"）原样保留，只有取数据的方式改变。

    ``src_stories`` 与 ``hit_lines`` 必须同时给（``hit_lines`` 是
    ``_build_term_line_index`` 的 ``hits[term]``，即同一批故事里的命中行号），
    否则退回逐行 ``in`` 扫描，语义不变。
    """

    def __init__(self, corpus: "_Corpus") -> None:
        self.corpus = corpus
        self._lines: dict[tuple[str, str], list[str]] = {}
        self._cands: dict[tuple[str, str, str], list[str]] = {}
        # 形态门控是纯函数，且在 4 个语言 × 大量重复台词上反复问同一批串
        # （实测命中率 95%），缓存能省下一条正则链。
        self._acceptable_cache: dict[tuple[str, str], bool] = {}
        self.split_calls = 0

    def lines(self, story_key: str, language: str) -> list[str]:
        """非空去空白行（与上游 ``align_term_by_frequency`` 内联构造的一致）。"""
        key = (story_key, language)
        cached = self._lines.get(key)
        if cached is None:
            page = _page_for(self.corpus.groups, story_key, language)
            text = str(page.get("text", "")) if page is not None else ""
            cached = [ln.strip() for ln in text.splitlines() if ln.strip()]
            self._lines[key] = cached
        return cached

    def _acceptable(self, candidate: str, language: str) -> bool:
        key = (candidate, language)
        hit = self._acceptable_cache.get(key)
        if hit is None:
            hit = termindex._translation_candidate_acceptable(candidate, language)
            self._acceptable_cache[key] = hit
        return hit

    def candidates(self, line: str, language: str, allowed: set[str] | None) -> list[str]:
        """一行里可接受的译名候选（上游的抽取 + allowed 过滤 + 形态门控）。

        缓存键 = (行, 语言, 是否带 allowed)。``allowed`` 只影响"是否枚举中文
        子窗口"与"词表过滤"，两者都由 ``allowed is None`` 决定；同一目标语言在
        一次调用里拿到的是同一个词表集合，所以这个键是完备的。
        """
        key = (line, language, "" if allowed is None else "1")
        cached = self._cands.get(key)
        if cached is not None:
            return cached
        raw = _local_translation_candidates_cached(
            line, language, enumerate_zh_windows=not allowed,
        )
        if allowed:
            raw = [c for c in raw if normalize_name(c) in allowed]
        out = [c for c in raw if self._acceptable(c, language)]
        self._cands[key] = out
        return out


def _local_translation_candidates_cached(
    segment: str, target_language: str, *, enumerate_zh_windows: bool = True,
) -> list[str]:
    """``termindex._local_translation_candidates`` 的等价版（走预编译正则）。

    只有中文分支受影响（它调用 ``_split_zh_run``）；其余分支直接委托上游。
    """
    lang = termindex._term_language(target_language)
    if lang not in ("zh_hans", "zh_tw", "zh_hant"):
        return termindex._local_translation_candidates(
            segment, target_language, enumerate_zh_windows=enumerate_zh_windows,
        )
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if 2 <= len(candidate) <= 40 and candidate not in out:
            out.append(candidate)

    # 与上游同样的发言人剥离（模式与 termindex 内联的那条完全一致）。
    seg = _SEMICOLON_SPEAKER_RE.sub("", segment)
    if not seg:
        seg = segment
    for match in termindex._LOCAL_QUOTE_RE.finditer(seg):
        add(match.group(1))
    for term in termindex._local_latin_candidates(seg):
        add(term)
    for run in termindex._CJK_RUN_RE.findall(seg):
        for piece in _split_zh_run_cached(run, enumerate_windows=enumerate_zh_windows):
            add(piece)
    return out


def _align_term_cached(
    aligner: _FastAligner,
    source_term: str,
    source_language: str,
    target_language: str,
    idf: dict,
    vocab: dict | None,
    src_stories: set[str],
    hit_lines: dict[str, Any] | None,
) -> str:
    """``termindex.align_term_by_frequency`` 的等价快速版（缓存 + 行号复用）。

    门控与排序逐条照抄上游；差别仅在"故事页的行"与"行内候选"取自缓存，
    且源侧命中行号优先用 ``hit_lines``（行位索引已算过）。
    """
    if source_language == "ja" and termindex._is_ja_stopword(source_term):
        return ""
    if not termindex._source_candidate_acceptable(source_term, source_language):
        return ""
    if len(_LATIN_RE.findall(source_term)) and len(source_term.replace(" ", "")) < 3:
        return ""
    if src_stories is None or len(src_stories) < 2:
        return ""
    allowed = vocab.get(target_language) if vocab else None
    co_docs: dict[str, int] = defaultdict(int)
    for sk in src_stories:
        src_lines = aligner.lines(sk, source_language)
        tgt_lines = aligner.lines(sk, target_language)
        if not src_lines or not tgt_lines:
            continue
        if hit_lines is not None:
            hit_idx = sorted(hit_lines.get(sk) or ())
        else:
            hit_idx = [i for i, ln in enumerate(src_lines) if source_term in ln]
        if not hit_idx:
            continue
        src_n = len(src_lines)
        seen_in_story: set[str] = set()
        for si in hit_idx:
            pred = (
                round(si * (len(tgt_lines) - 1) / max(1, src_n - 1)) if src_n > 1 else 0
            )
            for ti in (pred, pred - 1, pred + 1):
                if ti < 0 or ti >= len(tgt_lines):
                    continue
                for cand in aligner.candidates(tgt_lines[ti], target_language, allowed):
                    seen_in_story.add(cand)
            if seen_in_story:
                break
        for cand in seen_in_story:
            co_docs[cand] += 1
    if not co_docs:
        return ""
    n_total_stories = max(1, len(aligner.corpus.groups))
    min_containment = 0.30
    filtered: dict[str, int] = {}
    n_src = len(src_stories)
    for cand, co in co_docs.items():
        idf_val = idf.get((target_language, cand))
        if idf_val is None:
            continue
        if co < 2:
            continue
        if co / n_src < 0.25:
            continue
        df_global = max(1, round(n_total_stories / math.exp(idf_val)))
        if co / df_global >= min_containment or co >= df_global:
            filtered[cand] = co
    if not filtered:
        return ""
    ranked = sorted(
        ((cand, co * idf.get((target_language, cand), 0.1)) for cand, co in filtered.items()),
        key=lambda kv: (kv[1], kv[0]),
        reverse=True,
    )
    if not ranked or ranked[0][1] <= 0.0:
        return ""
    best_score = ranked[0][1]
    return max((c for c, s in ranked if s >= best_score * 0.75), key=len)


def _channel_trunk(
    corpus: _Corpus,
    candidates: list[str],
    *,
    source_language: str,
    target_languages: tuple[str, ...],
    vocab: dict | None,
    idf: dict | None,
    pair_index: dict | None,
) -> dict[str, dict[str, dict]]:
    """主干：对每个候选做 ja → 各目标语言的分布对齐（逐语言独立产出）。

    返回 ``{term: {lang: {value: {"value": v, "sim": 0.0, "evidence": {...}}}}}``。

    严格沿用 ``align_term_by_frequency`` 自带门控（源词门 + 跨故事复现 ≥2 且
    ≥25% + containment ≥0.30），**不放宽**——主干是精度背书，抑制噪声的活由
    第 0 层前置过滤与其它两路的交叉验证承担。

    性能：``src_stories`` 不走 ``_term_stories``（它对每个 term 遍历全部配对
    故事做子串判断），而是用一次 ``_build_term_line_index`` 扫描拿到的
    (term → 故事) 与 ``build_pair_story_index`` 的语言倒排求交，语义等价
    （"该故事含此 term 且具备目标语言页"）。
    """
    if idf is None:
        return {}

    # 一次扫描：term 在所有"具备任一目标语言"的故事里的命中行。
    scoped: set[str] = set()
    for target_language in target_languages:
        if termindex._term_language(target_language) == termindex._term_language(source_language):
            continue
        scoped |= corpus.paired(target_language, source_language)
    if not scoped:
        return {}
    index = corpus.line_index(scoped, source_language, candidates)

    # 语言倒排：lang → {stories}（全语料口径，与 build_pair_story_index 一致，
    # 否则 containment 的全局分母与 co 分子会失衡）。
    lang_stories: dict[str, set[str]] = {}
    for target_language in target_languages:
        if termindex._term_language(target_language) == termindex._term_language(source_language):
            continue
        if pair_index is not None and target_language in pair_index:
            lang_stories[target_language] = set(pair_index[target_language])
        else:
            lang_stories[target_language] = _paired_stories(
                corpus.groups, target_language, source_language
            )

    out: dict[str, dict[str, dict]] = {}
    aligner = _FastAligner(corpus)
    for term in candidates:
        hits = index.hits.get(term) or {}
        if len(hits) < 2:
            continue  # 单故事术语不可分布验证（align 也要求 ≥2 故事）
        term_stories = set(hits)
        lang_map: dict[str, dict] = {}
        for target_language, global_stories in lang_stories.items():
            src_stories = term_stories & global_stories
            if len(src_stories) < 2:
                continue
            try:
                # 走缓存等价实现：行与行内候选按 (页/行, 语言) 复用，源侧命中
                # 行号直接取行位索引（上游会为每个术语重扫一遍源行）。
                aligned = _align_term_cached(
                    aligner, term, source_language, target_language, idf, vocab,
                    src_stories, hits,
                )
            except Exception:
                aligned = ""
            if aligned:
                lang_map[target_language] = {
                    aligned: {
                        "value": aligned,
                        "sim": 0.0,
                        "evidence": {
                            "channel": "trunk",
                            "stories": len(src_stories),
                            "target": target_language,
                            # reliability：三组对照实验的评测范围只有
                            # zh_hans / zh_hant / en（组 1 = ja→zh，精度背书；
                            # 组 2 = zh→en→tw）。ko 从没被评测过，实测它的
                            # 分布对齐大量产出截断/活用形错误译名
                            # （コンサート→걸었던「走过的」、卒業→졸업하면
                            # 「如果毕业」）。因此 ko 标 unvalidated，协同层
                            # 只在它被交叉验证时才采纳，否则进水待决队列。
                            "reliability": _trunk_reliability(target_language),
                        },
                    }
                }
        if lang_map:
            out[term] = lang_map
    return out


# ── 辅助 1 / hub：英语形态直取 → zh_hant/ko 译名验证 → 回填 ──────────

def _trunk_reliability(language: str) -> str:
    """主干在某个目标语言上的可靠性等级（来自三组对照实验的评测范围）。

    ``validated``：zh_hans / zh_hant / en —— 组 1（ja→zh 分布对齐，样例几乎
    全对）与组 2（zh→en→tw）都覆盖过，精度有背书。
    ``unvalidated``：其余（当前是 ko）—— 从未被评测。实测它的分布对齐大量
    产出截断/活用形错误译名（コンサート→걸었던「走过的」、卒業→졸업하면
    「如果毕业」、戸惑→카츠유키「克幸」把人名当译名）。协同层据此要求 ko 槽
    必须有交叉验证或官方/姓氏背书，否则不采纳。
    """
    if termindex._term_language(language) in ("zh_hans", "zh_tw", "zh_hant", "en"):
        return "validated"
    return "unvalidated"


def _channel_glossary_backing(
    corpus: _Corpus,
    candidates: list[str],
    *,
    source_language: str,
    target_languages: tuple[str, ...],
    surnames: dict[str, dict[str, Any]],
    min_stories: int = 2,
) -> dict[str, dict[str, dict]]:
    """汉字人名背书通道（glossary 人物词条的姓氏映射，标为 ``trunk`` 家族）。

    边界报告点名的硬边界：汉字人名（朝比奈/星乃一歌）无形态特征，任何形态
    规则都抓不到，只能靠 glossary/seed/discovered 背书。glossary 的人物词条
    给出了完整的姓氏映射（``firstName``/``firstNameEnglish``/各语言全名前缀），
    那是比"行位投票"更硬的证据——**只要该术语确实在自己的故事里出现
    （``min_stories`` 以上），就直接落名**，不需要行位预测。

    归属：这条路的证据来源是**官方 glossary**，因此以 ``trunk`` 名义进入协同
    （它的可信度与官方来源等同，不应被当作 hub 的"低置信猜测"），协同阶段会
    与 translit/hub 的产出做同值比对，从而产生真实的 cross-channel agreement。

    只处理纯汉字候选（含片假名/拉丁的候选另有 translit/hub 负责），且要求
    候选的归一化键**等于**某个姓氏键（不做前缀匹配，避免把 星乃一歌 拆成 星乃）。
    """
    src_lang = termindex._term_language(source_language)
    if src_lang != "ja":
        return {}
    terms = [c for c in candidates if _is_kanji_only(c)]
    if not terms:
        return {}
    # 背书通道同样走全语料口径：判断"这个人名是否真的在语料里出现"。
    index = corpus.line_index(
        corpus.paired("en", source_language) or set(corpus.stories),
        source_language,
        terms,
    )
    out: dict[str, dict[str, dict]] = {}
    for term in terms:
        row = surnames.get(normalize_name(term))
        if not row:
            continue
        hits = index.hits.get(term) or {}
        if len(hits) < min_stories:
            continue
        lang_map: dict[str, dict] = {}
        for lang, value in row.items():
            if lang == "ja" or not value:
                continue
            if termindex._term_language(lang) == src_lang:
                continue
            slot = _slot_for(lang, target_languages)
            if slot not in target_languages:
                continue
            lang_map[slot] = {
                str(value): {
                    "value": str(value),
                    "sim": 0.0,
                    "evidence": {"channel": "trunk", "method": "glossary_surname",
                                 "ja": row.get("ja"), "stories": len(hits)},
                }
            }
        if lang_map:
            out[term] = lang_map
    return out


def _foreign_literal_hits(
    corpus: _Corpus,
    hits: dict[str, set[int]],
    value: str,
    source_lang: str,
    target_languages: tuple[str, ...],
) -> dict[str, int]:
    """拉丁名在其它语言译文里"原样保留"的命中故事数。

    品牌/缩写名在中文、韩文译文里常常不译（Vivid Street、MORE MORE HOUSE、
    N25），这是独立于"罗马音相似度"的第二路证据。只在 term 的命中故事里查
    （不扫全语料），因此开销与命中数成正比。
    """
    out: dict[str, int] = {}
    if not value or not hits:
        return out
    for lang in target_languages:
        if termindex._term_language(lang) == termindex._term_language(source_lang):
            continue
        scope = corpus.paired(lang, source_lang)
        if not scope:
            continue
        n = 0
        for sk in hits:
            if sk not in scope:
                continue
            if value in corpus.text(sk, lang):
                n += 1
        if n:
            out[lang] = n
    return out


def _pick_vote_winner(
    votes: dict[str, set[str]],
    n_stories: int,
    *,
    min_stories: int,
    min_ratio: float,
) -> tuple[str, int]:
    """从候选投票里挑胜者：(值, 命中故事数)。无胜者返回 ``("", 0)``。"""
    best: tuple[int, int, str] | None = None
    for cand, story_set in votes.items():
        n = len(story_set)
        if n < min_stories:
            continue
        if n_stories and n / n_stories < min_ratio:
            continue
        key = (n, len(cand), cand)
        if best is None or key > best:
            best = key
    if best is None:
        return "", 0
    return best[2], best[0]


def _channel_hub(
    corpus: _Corpus,
    candidates: list[str],
    *,
    source_language: str,
    target_languages: tuple[str, ...],
    official_keys: set[str] | None = None,
    seed_keys: set[str] | None = None,
    discovered_keys: set[str] | None = None,
    min_stories: int = 2,
    min_ratio: float = 0.25,
) -> dict[str, dict[str, dict]]:
    """辅助 1：行位锚定 → 英语拉丁形态直取 → zh_hant/ko 译名验证 → 回填。

    实验组 2 的机制（``pilot.py``），补了实验报告点名的三处缺陷：

    1. **锚定侧换成 ja 主位**：组 2 用简中侧定位候选（简中通用词铺得极广，
       行位噪声被放大）。这里用 ja 侧 ``_build_term_line_index`` 定位 term
       出现行，映射到英语行取候选——英语的拉丁形态天然暴露专名边界，
       而锚定侧收敛到 term 真正出现的位置。
    2. **验证改为"译名验证"而非"同形自证"**：组 2 检查"简中词在繁中出现"
       （简繁同形使信号恒真）。这里改为**译名层面的同点位检查**——英语候选
       本身必须在 zh_hant / ko 的预测行里出现（Latin 品牌名跨语言不译是常见
       情形），构成 ja/zh → en → tw 的三角闭环。验证结果写进 ``verified``，
       协同层用它决定"未验证的 hub 结果只能进 pending"。
    3. **噪声面收敛**：英语候选只取专名形态（``extract_latin_candidates`` 已排除
       句首歧义词/缩约/功能词），且要求 ≥2 故事复现与 ≥25% 覆盖。

    作用域（由调用方在 ``candidates`` 里就限定）：

    * 只吃**非片假名**候选——片假名串交给 translit（那里有 sim 硬门控）；
    * 只吃**形态专名**（L1 PROPER）与**有背书**（L0 OFFICIAL，含 glossary
      人物姓氏）的候选，**不吃 L2 统计发现词**。这是对实验结论的正面回应：
      组 2 的噪声全部出自"通用词进入行位预测"（大家→Thank / 単語→Sigh /
      始業式→Shiho），而 L2 统计发现词里通用词占比最高。L2 候选仍可由
      trunk（分布门控）与 translit（sim 门控）捞回，hub 不在这里冒险。

    回填（bootstrap）：对通过验证的英语对，再以英语为锚，把 term 在 zh_hans /
    zh_hant / ko 侧的**同点位**候选填回来（``_local_translation_candidates``，
    与主干同口径），要求 ≥2 故事复现且值不等于源词本身。
    """
    out: dict[str, dict[str, dict]] = {}
    en_lang = next(
        (t for t in target_languages if termindex._term_language(t) == "en"), None
    )
    if en_lang is None:
        return out
    pivot_langs = [
        t for t in target_languages
        if t != en_lang
        and termindex._term_language(t) != termindex._term_language(source_language)
        and termindex._term_language(t) in ("zh_hans", "zh_tw", "zh_hant", "ko")
    ]
    targets = [c for c in candidates if not _is_pure_katakana(c) and len(c) >= 2]
    # hub 的候选范围：只吃 L1 形态专名与 L0 官方（含 glossary 姓氏背书）。
    # L2 统计发现词是"通用词进入行位预测"这一组 2 噪声源的温床（大家→Thank /
    # 単語→Sigh / 始業式→Shiho），hub 不吃；它们仍可由 trunk 与 translit 捞回。
    tiers = classify_batch(
        targets, language=source_language,
        official_keys=official_keys or set(),
        seed_keys=seed_keys or set(),
        discovered=discovered_keys or set(),
    )
    # 按值比较（reload 会重建 Tier 枚举类），且排除 REJECT。
    targets = [
        c for c in targets
        if tiers.get(c, Tier.REJECT) in (Tier.PROPER, Tier.OFFICIAL)
    ]
    if not targets:
        return out
    en_paired = corpus.paired(en_lang, source_language)
    if not en_paired:
        return out
    index = corpus.line_index(en_paired, source_language, targets)
    # 回填用的语言页也要预先建索引（以英语为锚的反向映射）。
    aux_sets = {
        lang: corpus.paired(lang, en_lang) for lang in pivot_langs
    }

    for term in targets:
        hits = {
            sk: line_nos for sk, line_nos in (index.hits.get(term) or {}).items()
        }
        n_stories = len(hits)
        if n_stories < min_stories:
            continue
        votes: dict[str, set[str]] = defaultdict(set)
        for sk, line_nos in hits.items():
            en_lines = corpus.lines(sk, en_lang)
            ja_lines = index.lines.get(sk) or []
            if not en_lines or not ja_lines:
                continue
            for si in sorted(line_nos)[:3]:
                pred = _predict_index(si, len(ja_lines), len(en_lines))
                for ti in (pred, pred - 1, pred + 1):
                    if 0 <= ti < len(en_lines):
                        for cand in corpus.latin_candidates(en_lines[ti]):
                            votes[cand].add(sk)
        if not votes:
            continue
        best_en, votes_n = _pick_vote_winner(
            votes, n_stories, min_stories=min_stories, min_ratio=min_ratio,
        )
        if not best_en:
            continue

        # ── 验证：英语候选在 zh_hant / ko 同点位出现（译名验证）──────────
        aux_hits: dict[str, int] = {}
        for aux_lang in pivot_langs:
            scope = aux_sets.get(aux_lang) or set()
            if not scope:
                continue
            hits_n = 0
            for sk, line_nos in hits.items():
                if sk not in scope:
                    continue
                aux_lines = corpus.lines(sk, aux_lang)
                ja_lines = index.lines.get(sk) or []
                if not aux_lines or not ja_lines:
                    continue
                found = False
                for si in sorted(line_nos)[:3]:
                    pred = _predict_index(si, len(ja_lines), len(aux_lines))
                    for ti in (pred, pred - 1, pred + 1):
                        if 0 <= ti < len(aux_lines) and best_en in aux_lines[ti]:
                            hits_n += 1
                            found = True
                            break
                    if found:
                        break
            if hits_n:
                aux_hits[aux_lang] = hits_n

        lang_map: dict[str, dict] = {
            en_lang: {
                best_en: {
                    "value": best_en,
                    "sim": 0.0,
                    "evidence": {"channel": "hub", "votes": votes_n, "stories": n_stories,
                                 "aux_hits": aux_hits, "verified": bool(aux_hits)},
                }
            }
        }
        # ── 回填：以英语为锚，取简中/繁中同点位候选（跨故事投票）────────
        for pivot_lang in ("zh_hans", "zh_hant", "ko"):
            if pivot_lang not in pivot_langs:
                continue
            scope = aux_sets.get(pivot_lang) or set()
            if not scope:
                continue
            back_votes: dict[str, set[str]] = defaultdict(set)
            anchor_stories = 0
            for sk, line_nos in hits.items():
                if sk not in scope:
                    continue
                en_lines = corpus.lines(sk, en_lang)
                pivot_lines = corpus.lines(sk, pivot_lang)
                if not en_lines or not pivot_lines:
                    continue
                anchor_lines = [i for i, ln in enumerate(en_lines) if best_en in ln]
                if not anchor_lines:
                    continue
                anchor_stories += 1
                for ei in anchor_lines[:3]:
                    pred = _predict_index(ei, len(en_lines), len(pivot_lines))
                    for pi in (pred, pred - 1, pred + 1):
                        if not (0 <= pi < len(pivot_lines)):
                            continue
                        for cand in termindex._local_translation_candidates(
                            _strip_speaker_fast(pivot_lines[pi]), pivot_lang,
                        ):
                            if len(cand) < 2 or normalize_name(cand) == normalize_name(term):
                                continue
                            back_votes[cand].add(sk)
            cand_value, cand_n = _pick_vote_winner(
                back_votes, anchor_stories or n_stories,
                min_stories=min_stories, min_ratio=min_ratio,
            )
            if cand_value:
                lang_map[pivot_lang] = {
                    cand_value: {
                        "value": cand_value,
                        "sim": 0.0,
                        "evidence": {"channel": "hub", "backfill": True, "votes": cand_n,
                                     "anchor": best_en, "anchor_stories": anchor_stories},
                    }
                }
        out[term] = lang_map
    return out


# ── 辅助 2 / translit：片假名 → 罗马音 → 英语（sim 硬门控）─────────────

def _channel_translit(
    corpus: _Corpus,
    candidates: list[str],
    *,
    source_language: str,
    target_languages: tuple[str, ...],
    sim_threshold: float = 0.5,
    min_stories: int = 2,
    min_ratio: float = 0.25,
    max_candidates: int = 4,
) -> dict[str, dict[str, dict]]:
    """辅助 2：片假名串 → 行位英语候选 → ``romaji`` 罗马音门控。

    判据与 ``channel_c_katakana_to_english`` 一致（sim ≥ ``sim_threshold``、
    ≥2 故事、≥25% 覆盖），但**保留全部通过门控的候选**（按 (sim, 票数) 降序，
    上限 ``max_candidates``）——协同阶段需要拿它们与主干/hub 做一致性比对
    （同值 → agreement；不同值 → 冲突队列），只留一个赢家就没法比。

    这里刻意**不把** ``termindex._JA_KATAKANA_STOPWORDS`` 传给
    ``is_generic_katakana`` 的 ``extend``：那张表含 カイト/ミク/シブヤ/メイコ，
    正是本通道要抓的目标（虚拟歌手名/地名），传进去会把它们全部误杀。
    通用片假名（テスト/クラス/バタバタ）的拦截由第 0 层 ``candidate_tiers``
    负责——它查的是 ``romaji`` 内置通用表，不含这些人名。
    """
    en_lang = next(
        (t for t in target_languages if termindex._term_language(t) == "en"), None
    )
    if en_lang is None:
        return {}
    terms = [t for t in candidates if _is_pure_katakana(t) and not is_generic_katakana(t)]
    if not terms:
        return {}
    paired = corpus.paired(en_lang, source_language)
    if not paired:
        return {}
    index = corpus.line_index(paired, source_language, terms)
    out: dict[str, dict[str, dict]] = {}
    for term in terms:
        hits = {
            sk: line_nos for sk, line_nos in (index.hits.get(term) or {}).items()
        }
        n_stories = len(hits)
        if n_stories < min_stories:
            continue
        votes: dict[str, set[str]] = defaultdict(set)
        for sk, line_nos in hits.items():
            en_lines = corpus.lines(sk, en_lang)
            ja_lines = index.lines.get(sk) or []
            if not en_lines or not ja_lines:
                continue
            for si in sorted(line_nos)[:3]:
                pred = _predict_index(si, len(ja_lines), len(en_lines))
                for ti in (pred, pred - 1, pred + 1):
                    if 0 <= ti < len(en_lines):
                        for cand in corpus.latin_candidates(en_lines[ti]):
                            votes[cand].add(sk)
        accepted: list[tuple[float, int, str]] = []
        for cand, story_set in votes.items():
            n = len(story_set)
            if n < min_stories or n / max(1, n_stories) < min_ratio:
                continue
            if not is_plausible_translation(term, cand, sim_threshold):
                continue
            accepted.append((similarity(term, cand), n, cand))
        if not accepted:
            continue
        accepted.sort(key=lambda item: (item[0], item[1], item[2]), reverse=True)
        lang_map: dict[str, dict] = {en_lang: {}}
        for sim, n, cand in accepted[:max_candidates]:
            lang_map[en_lang][cand] = {
                "value": cand,
                "sim": sim,
                "evidence": {
                    "channel": "translit", "sim": sim, "votes": n,
                    "stories": n_stories,
                    # 跨语言字面命中：该拉丁名在别的语言译文里**原样保留**
                    # （Vivid Street / MORE MORE HOUSE 这类品牌名不译）。
                    # 这是修正组 2 "繁中同形自证失效"之后，真正有效的第二个
                    # 独立信号，用于给边缘档（sim < 0.6）背书。
                    "foreign_literal": _foreign_literal_hits(
                        corpus, hits, cand, en_lang, target_languages,
                    ),
                },
            }
        out[term] = lang_map
    return out


# ── 第 2 层：协同（互补合并 / 交叉验证 / 补位仲裁）───────────────────

def _slot_for(lang: str, target_languages: tuple[str, ...]) -> str:
    """把 glossary 的语言槽拼法映射到调用方目标语言的拼法。

    glossary 与 ``_glossary_name_table`` 用 ``zh_tw``（termindex 的 TERM_LANGUAGES
    口径），而语料与调用方多用 ``zh_hant``。不映射就会让官方繁中名在
    ``target_languages=("zh_hant", ...)`` 时整批丢失。映射只在两边拼法不一致
    且调用方确实要了另一种拼法时发生。
    """
    if lang in target_languages:
        return lang
    want = termindex._term_language(lang)
    for target in target_languages:
        if termindex._term_language(target) == want:
            return target
    return lang


def _glossary_name_index(
    glossary: Iterable[Any] | None,
    *,
    kinds: Iterable[str] | None = None,
) -> dict[str, dict[str, str]]:
    """glossary → {归一化键: {lang: 官方名}}（只收语言槽，排除 title/name 等）。

    ``kinds`` 默认 ``termindex.NOUN_KINDS``（实体类词条：area / area_item /
    character / character_profile / character_unit / game / unit）。这不是可有可无
    的过滤，而是必要的：非实体词条（honor / virtual_item / mysekai_fixture /
    song）里存在**同形异指**的脏数据——``プラネタリウム`` 在 honor 里是
    「大間鮪魚」、``ハート`` 在 virtual_item 里是「烟花」、``ベテラン`` 在 honor
    里是「经验丰富」。这些表面形恰好与语料里的术语同形，若不按词条类型过滤，
    L0 官方层就会把鱼名/烟花名当成术语译名直接采纳（实测污染）。

    这与上游 ``termindex.build_noun_lexicon`` / ``seed_from_glossary`` 的口径一致
    （``kind not in NOUN_KINDS`` 的词条一概不进词表）。
    """
    allowed = set(kinds) if kinds is not None else set(termindex.NOUN_KINDS)
    glossary = [item for item in (glossary or [])
                if not allowed or str(getattr(item, "kind", "")) in allowed]
    _official_keys, names_by_key, _surfaces = _glossary_name_table(glossary)
    # 每个键的贡献者类型：只要存在一个实体类贡献者就放行该键（同形异指的非实体
    # 贡献者不允许污染实体名）。
    key_kinds: dict[str, set[str]] = {}
    for term_obj in (glossary or []):
        kind = str(getattr(term_obj, "kind", ""))
        canonical = str(getattr(term_obj, "canonical", "") or "")
        names = getattr(term_obj, "names", {}) or {}
        for surface in [canonical] + [str(v) for v in names.values() if v]:
            key = normalize_name(surface)
            if key:
                key_kinds.setdefault(key, set()).add(kind)
    out: dict[str, dict[str, str]] = {}
    for key, names in names_by_key.items():
        kinds_here = key_kinds.get(key) or set()
        if allowed and kinds_here and not (kinds_here & allowed):
            continue
        row = {
            lang: str(value)
            for lang, value in names.items()
            if value and lang in termindex.TERM_LANGUAGES
        }
        if row:
            out[key] = row
    return out


def _surname_backing_index(
    glossary: Iterable[Any] | None,
) -> dict[str, dict[str, Any]]:
    """从 character / character_profile 词条建「姓氏 → 各语言姓氏」背书表。

    边界报告明确：汉字人名（朝比奈/星乃一歌）**无形态特征**，靠 ``classify``
    的形态规则必然判 REJECT，只能靠 glossary/seed/discovered 背书。glossary 的
    人物词条恰好完整携带这份证据：``firstName``（ja 姓）、``firstNameEnglish``、
    以及各语言全名（``zh_hans``/``zh_tant``全名的前缀即该语言姓氏）。

    返回 ``{归一化姓氏键: {"ja": 姓, "en": 罗马姓, "zh_hans": …, "zh_hant": …,
    "ko": …}}``。只收"全名以该姓氏开头"的词条，避免把 ``givenName``（名）当成姓。
    """
    out: dict[str, dict[str, Any]] = {}
    for term_obj in (glossary or []):
        if str(getattr(term_obj, "kind", "")) not in ("character", "character_profile"):
            continue
        names = getattr(term_obj, "names", {}) or {}
        canon = termindex._canon_names(names)
        ja_surname = str(names.get("firstName") or "")
        en_surname = str(names.get("firstNameEnglish") or "")
        if not ja_surname or not en_surname:
            continue
        if len(ja_surname) < 1 or len(en_surname) < 2:
            continue
        row: dict[str, Any] = {"ja": ja_surname, "en": en_surname}
        for lang, full_name in canon.items():
            full = str(full_name or "")
            if not full:
                continue
            if lang == "ja" or lang == "en":
                continue
            if full.startswith(ja_surname):
                row[lang] = full[:len(ja_surname)]
            elif lang == "ko":
                head = full.split(" ")[0]
                if len(head) >= 2:
                    row[lang] = head
        if len(row) >= 3:
            out.setdefault(normalize_name(ja_surname), row)
    return out


def _rank_of(labels: Iterable[str]) -> tuple[float, str]:
    """一个候选值的优先级：取所有来源档位里最强的那个（数值最小）。

    注意用**内部分档名**（``translit_low`` 有自己的档位 3.0），不能先折回
    ``translit``——否则边缘档会被当成 sim≥0.6 的强证据，直接压过 trunk。
    """
    best: tuple[float, str] | None = None
    for label in labels:
        value = _channel_rank(label)
        if best is None or value < best[0]:
            best = (value, label)
    return best or (9.0, "")


def arbitrate(
    term: str,
    proposals: dict[str, dict[str, list[str]]],
    *,
    glossary_names: dict | None = None,
) -> dict:
    """对单个术语的跨通道提案做仲裁（协同机制 2/3 的规则实现）。

    ``proposals`` 形如 ``{lang: {candidate_value: [channel_names]}}``，
    ``channel_names`` 取值 ``{"L0", "trunk", "hub", "translit"}``（内部还允许
    ``"translit_low"`` 表示 sim < 0.6 的边缘档证据）。

    优先级（同一 ``lang`` 槽内比较）：**L0 官方 > translit(sim≥0.6) >
    trunk(门控通过) > translit(边缘档) > hub**。规则：

    * 同一 lang 多个 candidate（同源冲突）→ 取优先级最高的通道；
    * 平级（最高优先级并列）→ 取**跨通道一致者**（该值同时被 ≥2 个通道给出）；
    * 仍平级 → 记 ``conflict``，**不写入 resolved**（不静默择一，交人工/智能体）。

    返回::

        {"resolved": {lang: value},
         "conflicts": [{"lang":..., "candidates": {value: [channels]},
                        "reason":..., "priority":...}],
         "channel_priority_used": {lang: channel}}
    """
    glossary_names = glossary_names or {}
    official = glossary_names.get(normalize_name(term)) or {}
    resolved: dict[str, str] = {}
    conflicts: list[dict] = []
    priority_used: dict[str, str] = {}

    for lang, value_map in (proposals or {}).items():
        if not value_map:
            continue
        # L0 官方名：权威，直接落地，不参与票选。
        if lang in official:
            resolved[lang] = official[lang]
            priority_used[lang] = "L0"
            continue
        ranked: list[tuple[float, str, list[str]]] = []
        for value, labels in value_map.items():
            rank, _strongest = _rank_of(labels)
            ranked.append((rank, value, [_canonical_channel(c) for c in labels]))
        ranked.sort(key=lambda item: (item[0], item[1]))
        top_rank = ranked[0][0]
        top = [item for item in ranked if item[0] == top_rank]
        strongest = _channel_rank_label(top[0][2], top_rank)
        if len(top) == 1:
            resolved[lang] = top[0][1]
            priority_used[lang] = strongest
            continue
        # 平级 → 取跨通道一致者（≥2 个通道独立给出同一值）。
        consistent = [item for item in top if len(set(item[2])) >= 2]
        if len(consistent) == 1:
            resolved[lang] = consistent[0][1]
            priority_used[lang] = "agreement:" + strongest
            continue
        pool = consistent if consistent else top
        conflicts.append({
            "lang": lang,
            "candidates": {item[1]: sorted(set(item[2])) for item in pool},
            "priority": strongest,
            "reason": (
                f"语言槽 {lang} 有 {len(pool)} 个候选并列于最高优先级"
                f"（{strongest}），且无跨通道一致者 → 交人工/智能体裁决"
            ),
        })
    return {
        "resolved": resolved,
        "conflicts": conflicts,
        "channel_priority_used": priority_used,
    }


def _channel_rank_label(labels: Iterable[str], rank: float) -> str:
    """在给定优先级档位上，返回对应的**对外**通道名。"""
    for label in labels:
        if _channel_rank(label) == rank:
            return _canonical_channel(label)
    return "?"


def _confidence(
    labels: set[str],
    agreement: int,
    sim: float,
    *,
    verified_hub: bool = False,
) -> float:
    """置信度打分（0-1）。

    基线按最强通道给（``labels`` 是内部细分的通道名集合）：L0 官方 0.98；
    translit 直接吃 sim（0.55 + 0.40×sim，sim=1.0 → 0.95）；trunk 0.70
    （精度背书，含 glossary 姓氏映射）；translit 边缘档 0.55 + 0.40×sim，
    但**只有它一路时会被显式降级**（见 :func:`_merge_channels`，边缘档不允许
    单独成立）；hub 已通过 zh_hant/ko 同点位译名验证 0.62，未验证 0.52。

    交叉验证增益：``agreement`` 是"同一 (语言槽, 译名) 被多少路独立命中"的
    最大值，每多一路 +0.12。
    """
    if "L0" in labels:
        base = 0.98
    elif "translit" in labels:
        base = 0.55 + 0.40 * max(0.0, min(1.0, sim))
    elif "trunk" in labels:
        base = 0.70
    elif "hub" in labels:
        base = 0.62 if verified_hub else 0.52
    else:
        base = 0.55 + 0.40 * max(0.0, min(1.0, sim))
    base += 0.12 * max(0, agreement - 1)
    return round(min(0.99, base), 4)


def _merge_channels(
    channels: dict[str, dict[str, dict[str, dict]]],
    *,
    corpus: _Corpus,
    source_language: str,
    glossary_names: dict[str, dict[str, str]],
    min_accept_confidence: float = 0.6,
) -> dict:
    result = {
        "accepted": {}, "pending": [], "conflicts": [],
        "agreement_boosted": 0, "stats_counter": {}, "slot_decisions": [],
    }
    slots = sorted({
        (term, language)
        for table in channels.values()
        for term, languages in table.items()
        for language in languages
    })
    for term, language in slots:
        scoped = {
            channel: {term: {language: table[term][language]}}
            for channel, table in channels.items()
            if language in table.get(term, {})
        }
        scoped_glossary = {
            key: {language: names[language]}
            for key, names in glossary_names.items() if language in names
        }
        merged = _merge_channel_slot(
            scoped, corpus=corpus, source_language=source_language,
            glossary_names=scoped_glossary,
            min_accept_confidence=min_accept_confidence,
        )
        candidates = {
            value: [channel for channel, table in scoped.items()
                    if value in table[term][language]]
            for table in scoped.values() for value in table[term][language]
        }
        result["pending"].extend(merged["pending"])
        result["conflicts"].extend(merged["conflicts"])
        record = merged["accepted"].get(term)
        if record and record["names"]:
            status = "accepted"
            aggregate = result["accepted"].get(term)
            if aggregate is None:
                result["accepted"][term] = record
            else:
                aggregate["names"].update(record["names"])
                aggregate["confidence"] = min(aggregate["confidence"], record["confidence"])
                aggregate["channels"] = sorted(set(aggregate["channels"] + record["channels"]))
        elif merged["conflicts"]:
            status = "conflict"
        elif merged["pending"]:
            status = "pending"
        else:
            status = "rejected"
        result["slot_decisions"].append({
            "term": term, "language": language, "status": status,
            "value": record["names"].get(language) if record else None,
            "candidates": candidates,
            "evidence": [
                {"channel": channel, "value": value, "payload": payload}
                for channel, table in scoped.items()
                for value, payload in table[term][language].items()
            ],
        })
        result["agreement_boosted"] += merged["agreement_boosted"]
        for key, count in merged["stats_counter"].items():
            result["stats_counter"][key] = result["stats_counter"].get(key, 0) + count
    return result


def _merge_channel_slot(
    channels: dict[str, dict[str, dict[str, dict]]],
    *,
    corpus: _Corpus,
    source_language: str,
    glossary_names: dict[str, dict[str, str]],
    min_accept_confidence: float = 0.6,
) -> dict:
    """协同第 2 层：合并三通道 → 采纳 / 冲突 / 待决 / 拒绝。

    三条协同机制都在这里落地：

    * **互补取值**：三路产出并集去重。提案按 (语言槽, 译名) 聚合，每个槽内
      用 :func:`arbitrate` 按优先级裁决；不同语言槽之间天然互补（trunk 给
      zh_hans 义译名、translit 给 en 音译名、hub 给 en 人名），互不覆盖。
    * **交叉验证增益**：``agreement`` = "同一 (语言槽, 译名) 被多少路独立命中"
      的最大值。≥2 即 ``agreement_boosted``，置信度 +0.12×(agreement-1)。
      同槽**不同值** → 冲突队列。
    * **相互补位**：trunk 缺席而 translit 高分（sim ≥ 0.99）→ 采纳并记
      ``rescued_by``；辅助值有 trunk 同值证据 → +0.10 并记 ``promoted_by_trunk``。
      反向的诚实边界同样存在：translit 的**边缘档**（sim 0.5-0.6）单独出现
      不许成立，降级进 ``pending``（实验证明这一段仍有假阳性，如
      コンテスト→Kohane 0.53）；hub 未通过译名验证时也进 ``pending``。
    """
    # 摊平：{term: {lang: {value: [labels]}}}；labels 是细分通道名。
    proposals: dict[str, dict[str, dict[str, list[str]]]] = {}
    term_evidence: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for channel_name, table in channels.items():
        for term, lang_map in table.items():
            term_row = proposals.setdefault(term, {})
            for lang, value_map in lang_map.items():
                lang_row = term_row.setdefault(lang, {})
                for value, payload in value_map.items():
                    label = channel_name
                    if channel_name == "translit":
                        sim_value = float(payload.get("sim", 0.0) or 0.0)
                        if sim_value < _TRANSLIT_CONFIRMED_SIM:
                            label = "translit_low"
                        elif is_abbrev_form_only(term, value):
                            # Astra P08/D08: a match whose score comes only
                            # from the abbreviation form rule (same initial +
                            # digit shape) is candidate eligibility, not
                            # identity evidence — `ニーゴ→N25` and
                            # `ニーゴ→N99` are indistinguishable under it. It
                            # must not count as confirmed translit; the pair
                            # still enters the pool as translit_low and needs
                            # the same corroboration as any weak candidate.
                            label = "translit_low"
                    bucket = lang_row.setdefault(value, [])
                    if label not in bucket:
                        bucket.append(label)
                    term_evidence[term].append((lang, value, payload))

    accepted: dict[str, dict] = {}
    conflicts: list[dict] = []
    pending: list[dict] = []
    agreement_boosted = 0
    stats_counter: Counter[str] = Counter()

    for term in sorted(proposals):
        lang_proposals = proposals[term]
        merged_names: dict[str, str] = {}
        labels_used: set[str] = set()
        term_conflicts: list[dict] = []
        superseded: list[str] = []
        fragment_notes: list[str] = []
        edge_notes: list[str] = []
        confirmed_notes: list[str] = []
        verified_hub = False

        for lang, value_map in sorted(lang_proposals.items()):
            verdict = arbitrate(term, {lang: value_map}, glossary_names=glossary_names)
            if verdict["conflicts"]:
                term_conflicts.extend(
                    {"term": term, **row} for row in verdict["conflicts"]
                )
                stats_counter["conflict_langs"] += 1
                continue
            if not verdict["resolved"]:
                continue
            value = verdict["resolved"][lang]
            # 形态健全性：整句/语气词不是译名（克幸 → "……抱歉。"）。
            if _is_sentence_fragment(value, lang):
                stats_counter["fragment_rejected"] += 1
                fragment_notes.append(f"{lang}={value}")
                continue
            merged_names[lang] = value
            sources = list(value_map.get(value) or [])
            labels_used.update(sources)
            # 逐槽边缘档校验（per-slot，而非整术语）：该槽的值若只由 translit
            # 边缘档（sim 0.5-0.6）支撑，且没有跨语言字面命中，就必须摘掉——
            # 其它语言槽有 trunk 命中不代表这个英语槽可信。这是实验点名的
            # 假阳性（コンテスト→Kohane 0.53）的精确拦截点。
            if set(sources) <= {"translit_low"} and not _foreign_literal_for(
                term_evidence.get(term, ()), value
            ):
                stats_counter["edge_slot_rejected"] += 1
                edge_notes.append(f"{lang}={value}（sim 边缘档且无跨语言字面命中）")
                merged_names.pop(lang, None)
                continue
            # 边缘档但拿到跨语言字面命中：记明佐证（Vivid Street 这类品牌名
            # 在中文译文里原样保留，是独立于罗马音相似度的第二路证据）。
            if set(sources) <= {"translit_low"}:
                foreign = _foreign_literal_for(term_evidence.get(term, ()), value)
                if foreign:
                    confirmed_notes.append(f"{lang}={value}（跨语言字面命中：{foreign}）")
            # 被更高优先级通道压下的同槽候选：记账（不丢信息，便于审计）。
            for other_value, other_sources in value_map.items():
                if other_value == value:
                    continue
                superseded.append(
                    f"{lang}: {other_value}（{'+'.join(sorted(set(other_sources)))}）"
                    f" 被 {value}（{'+'.join(sorted(set(sources)))}）按优先级压下"
                )
            # hub 的"译名验证"证据（英语候选在 zh_hant/ko 同点位出现）。
            if "hub" in sources:
                payload = (channels.get("hub", {}).get(term) or {}).get(lang, {}).get(value) or {}
                evidence = payload.get("evidence") or {}
                if evidence.get("verified"):
                    verified_hub = True

        # ── 补位 ①：主干缺席 + translit 高分 → 采纳 ────────────────────
        rescued: list[str] = []
        translit_map = channels.get("translit", {}).get(term) or {}
        if "trunk" not in labels_used and "L0" not in labels_used and translit_map:
            best_sim, best_value = 0.0, ""
            for value, payload in translit_map.items():
                sim_value = float(payload.get("sim", 0.0) or 0.0)
                if sim_value > best_sim:
                    best_sim, best_value = sim_value, value
            en_slot = _target_slot("en", ("en",)) or "en"
            if best_value and best_sim >= 0.99 and en_slot not in merged_names:
                merged_names[en_slot] = best_value
                labels_used.add("translit")
                rescued.append(
                    f"主干未穿透，translit sim={best_sim:.2f}（{best_value}）补位 {en_slot}"
                )

        # ── 补位 ②：辅助值有主干同值证据 → 加分 ────────────────────────
        promoted: list[str] = []
        trunk_map = channels.get("trunk", {}).get(term) or {}
        trunk_values = {
            _name_key(payload.get("value", "")) for payload in trunk_map.values()
        }
        if trunk_values and "trunk" not in labels_used:
            hits = sorted(
                f"{lg}={payload.get('value')}"
                for lg, payload in trunk_map.items()
                if _name_key(payload.get("value", "")) in {
                    _name_key(v) for v in merged_names.values()
                }
            )
            if hits:
                promoted.append("主干同点证据支持（" + ", ".join(hits) + "）")

        if not merged_names and not term_conflicts:
            if edge_notes:
                # Astra P08/D08: this continue used to swallow terms whose only
                # candidates were edge-tier (sim 0.5-0.6) and got dropped in the
                # per-slot check. The term then vanished from the output entirely
                # — neither accepted, nor pending, nor conflicted — so the
                # summary counts no longer summed to the per-slot decisions.
                # Record it as pending: "we saw candidates but could not confirm
                # any" is exactly what the review queue exists for.
                pending.append(
                    {
                        "term": term,
                        "names": {},
                        "confidence": 0.0,
                        "channels": ["translit"],
                        "reason": (
                            "仅 translit 边缘档候选（sim 0.5-0.6），无跨语言字面"
                            "命中或主干背书 → 不足以采纳，交智能体裁决（"
                            + "; ".join(edge_notes)
                            + "）"
                        ),
                    }
                )
                stats_counter["edge_only_pending"] += 1
            continue

        # ── 诚实边界 ①：未评测语言的槽需要交叉验证或背书 ─────────────────
        # 三组对照实验只评测了 zh_hans / zh_hant / en。ko 从未被评测，而实测
        # 它的分布对齐大量产出截断/活用形错误译名（コンサート→걸었던「走过的」、
        # 卒業→졸업하면「如果毕业」、戸惑→카츠유키「克幸」把人名当译名）。
        # 判据：该槽的值必须有官方/姓氏背书，或被跨通道同值支持（agreement≥2），
        # 否则从 names 里摘掉并记入 pending 原因；若摘完后一个名字都不剩，
        # 整个术语进 pending（而不是采纳一个空壳）。
        unvalidated_slots = [
            lang for lang in merged_names
            if termindex._term_language(lang) not in ("zh_hans", "zh_tw", "zh_hant", "en")
        ]
        blocked: list[str] = []
        for lang in unvalidated_slots:
            value = merged_names[lang]
            support = set(lang_proposals.get(lang, {}).get(value) or [])
            official_here = value in set(
                (glossary_names.get(normalize_name(term)) or {}).values()
            )
            glossary_here = any(
                (payload.get("evidence") or {}).get("method") == "glossary_surname"
                and payload.get("value") == value
                for _lg, _v, payload in term_evidence.get(term, ())
            )
            if len(support) >= 2 or official_here or glossary_here:
                continue
            blocked.append(lang)
        for lang in blocked:
            merged_names.pop(lang, None)
        if blocked and not merged_names:
            pending.append({
                "term": term,
                "names": {},
                "confidence": 0.0,
                "channels": sorted({_canonical_channel(c) for c in labels_used}),
                "reason": (
                    "仅由未经评测语言的分布对齐产出（"
                    + ", ".join(sorted(blocked))
                    + "），无交叉验证/官方背书；实测该语言对齐大量产出截断与"
                    "活用形错误（コンサート→걸었던「走过的」）→ 交智能体裁决"
                ),
            })
            continue

        # ── 交叉验证增益：同一 (语言槽, 译名) 被几路独立命中 ────────────
        agreement = 0
        for lang, value in merged_names.items():
            support = set(lang_proposals.get(lang, {}).get(value) or [])
            agreement = max(agreement, len(support))
        # 跨语言的"同一值"也算一路佐证：trunk 给 zh_hans=X 且 hub 给 zh_hant=X
        # 说明两路在"这个译名"上独立一致（语言槽不同，故 +1 而非当作同一路）。
        same_value_support: dict[str, int] = defaultdict(int)
        for lang, value in merged_names.items():
            for support_label in set(lang_proposals.get(lang, {}).get(value) or []):
                same_value_support[_name_key(value)] += 1

        best_sim = max(
            (float(payload.get("sim", 0.0) or 0.0)
             for _lang, value, payload in term_evidence.get(term, ())
             if value in merged_names.values()),
            default=0.0,
        )
        confidence = _confidence(
            labels_used, max(1, agreement), best_sim, verified_hub=verified_hub,
        )
        if promoted:
            confidence = round(min(0.99, confidence + 0.10), 4)
        if agreement >= 2:
            agreement_boosted += 1

        record: dict[str, Any] = {
            "names": dict(sorted(merged_names.items())),
            "confidence": confidence,
            "channels": sorted(
                {_canonical_channel(c) for c in labels_used}
            ),
            "agreement": agreement,
        }
        if rescued:
            record["rescued_by"] = rescued
        if promoted:
            record["promoted_by_trunk"] = promoted
        if superseded:
            record["superseded"] = superseded
        if fragment_notes:
            record["fragment_rejected"] = fragment_notes
        if edge_notes:
            record["edge_slot_rejected"] = edge_notes
        if confirmed_notes:
            record["confirmed_by"] = confirmed_notes

        # ── 诚实边界 ②：整术语仅由 translit 边缘档支撑时不许成立 ────────
        # （逐槽校验已在上面摘掉边缘档英语槽；这里兜住"摘完一个不剩"的情况，
        #  同样给跨语言字面命中留出补位通道。）
        if labels_used and labels_used <= {"translit_low"}:
            foreign = ""
            for value in merged_names.values():
                foreign = _foreign_literal_for(term_evidence.get(term, ()), value)
                if foreign:
                    break
            if foreign:
                record["confirmed_by"] = [f"跨语言字面命中：{foreign}"]
            else:
                pending.append({
                    "term": term,
                    "names": record["names"],
                    "confidence": confidence,
                    "channels": record["channels"],
                    "reason": (
                        "仅 translit 边缘档（sim 0.5-0.6）命中，且无跨语言字面命中/"
                        "其它通道同值佐证；实验证明这一段存在假阳性 "
                        "（コンテスト→Kohane 0.53）→ 交智能体裁决"
                    ),
                })
                continue
        # ── 诚实边界 ②：hub 未通过译名验证（zh_hant/ko 同点位未命中）────
        if labels_used and labels_used <= {"hub"} and not verified_hub:
            pending.append({
                "term": term,
                "names": record["names"],
                "confidence": confidence,
                "channels": record["channels"],
                "reason": "仅 hub 命中且译名验证未过（zh_hant/ko 同点位未命中）→ 交智能体裁决",
            })
            continue

        # ── 三角闭环锚定：en 值必须在英语语料同点位命中 ─────────────────
        # 豁免：值本身来自 glossary 背书（L0 官方名 / 人物姓氏映射）。这两类
        # 名字的权威性来自官方词条而非行位证据，且它们在对话里常以"姓"出现
        # （朝比奈 而非 朝比奈真冬），字面锚定本就找不到。
        en_slot = next(
            (lg for lg in merged_names if termindex._term_language(lg) == "en"), None
        )
        en_value = merged_names.get(en_slot, "") if en_slot else ""
        official_values = set(
            (glossary_names.get(normalize_name(term)) or {}).values()
        )
        glossary_backed = any(
            (payload.get("evidence") or {}).get("method") == "glossary_surname"
            for _lang, _value, payload in term_evidence.get(term, ())
        )
        if en_value and en_value not in official_values and not glossary_backed and "L0" not in labels_used:
            anchor = _anchor_check(corpus, term, source_language, en_value)
            if not anchor["verified"]:
                pending.append({
                    "term": term,
                    "names": record["names"],
                    "confidence": confidence,
                    "channels": record["channels"],
                    "reason": (
                        "en 值与英语语料同点位锚定失败（未收录入 glossary，"
                        f"按降级规则只做锚定）：{anchor['reason']}"
                    ),
                })
                continue

        if term_conflicts:
            conflicts.extend(term_conflicts)
            record["note"] = "跨通道译名不一致（部分语言已进冲突队列）"

        if confidence >= min_accept_confidence:
            accepted[term] = record
        else:
            pending.append({
                "term": term,
                "names": record["names"],
                "confidence": confidence,
                "channels": record["channels"],
                "reason": "置信度低于阈值且无交叉验证增益 → 交智能体裁决",
            })

    return {
        "accepted": accepted,
        "conflicts": conflicts,
        "pending": pending,
        "agreement_boosted": agreement_boosted,
        "stats_counter": dict(stats_counter),
    }


def _is_sentence_fragment(value: str, lang: str) -> bool:
    """译名形态健全性：排除"整句/语气词"被当成译名的情况。

    分布对齐的候选来自**行**，一行里除了译名还有整句话。上游
    ``_translation_candidate_acceptable`` 已经挡掉大部分（拉丁侧尤其严），
    但中文/韩文侧仍有漏网（实测 ``克幸 → "……抱歉。"``）。判据取三组实验里
    噪声译名的共同形态特征：

    * 以省略号/句末标点结尾（``……抱歉。``、``걸었던.``）；
    * 含句子级标点（。！？…）；
    * 韩文侧以连接/终结词尾结尾（``하면``/``걸었던``——活用形不是名词）。

    专名的合法形态（``摄影大赛``、``犰狳``、``사육장``）不会命中任何一条。
    """
    if not value:
        return True
    text = value.strip()
    if not text:
        return True
    if any(mark in text for mark in "。！？…！?"):
        return True
    if text.startswith(("…", "．")):
        return True
    lang_key = termindex._term_language(lang)
    if lang_key == "ko":
        # 韩文活用/连接词尾：以这些结尾的一定是句子片段而非名词性译名。
        if re.search(r"(하면|했던|었던|았다|었다|는다|한다|입니다|해요|네요|지만|면서|라서|니까)$", text):
            return True
        if text.endswith(("다", "요")) and len(text) > 2 and " " not in text:
            return True
    if lang_key in ("zh_hans", "zh_tw", "zh_hant"):
        # 中文语气词/助词收尾（吧/呢/啊/了/的）。
        if text[-1] in "吧呢啊呀嘛了的地得吗哦喔噢":
            return True
    return False


def _prune_value_collisions(
    accepted: dict[str, dict],
    *,
    min_accept_confidence: float = 0.6,
) -> tuple[dict[str, dict], list[dict]]:
    """剔除"同一译名被多个互不相关的术语共同声称"的槽（译名唯一性校验）。

    这是一个**全局自洽性**判据，不依赖任何外部词表：一个译名不可能同时是
    两个无关术语的正确译名。实测抓到的正是最难发现的一类噪声——``飼育員``
    （饲养员）与 ``アルマジロ``（犰狳）都被对齐到 zh_hant 的「犰狳」：前者是
    行位噪声，但单看它自己（8 故事命中、containment 通过）毫无破绽。

    规则：按 (语言槽, 归一化译名) 聚合声称者（术语集合）；若声称者 ≥2 且彼此
    **无包含关系**（既不是同一词的不同写法，也不是长短形），则该值不可信，
    从所有声称者的 names 里摘除，并记入 pending（保守起见两边都不保留，
    而不是"选一个"，因为无法从证据上判断哪个才是对的）。

    合法的包含关系（``コンテスト`` ⊂ ``フォトコンテスト`` 都译「摄影大赛」）
    不受影响：那是同一概念的长短形，术语间有子串关系。
    """
    claims: dict[tuple[str, str], list[str]] = defaultdict(list)
    for term, record in accepted.items():
        for lang, value in (record.get("names") or {}).items():
            key = _name_key(value)
            if key:
                claims[(lang, key)].append(term)

    drops: dict[str, list[tuple[str, str, list[str]]]] = defaultdict(list)
    for (lang, key), terms in claims.items():
        if len(terms) < 2:
            continue
        related = any(
            (a in b or b in a) for a in terms for b in terms if a != b
        )
        if related:
            continue
        for term in terms:
            value = (accepted[term]["names"] or {}).get(lang, "")
            drops[term].append((lang, value, sorted(terms)))

    if not drops:
        return accepted, []

    pruned: dict[str, dict] = {}
    demoted: list[dict] = []
    for term, record in accepted.items():
        if term not in drops:
            pruned[term] = record
            continue
        names = dict(record.get("names") or {})
        details = []
        for lang, value, others in drops[term]:
            names.pop(lang, None)
            details.append(f"{lang}={value}（与 {', '.join(o for o in others if o != term)} 冲突）")
        if not names:
            demoted.append({
                "term": term,
                "names": {},
                "confidence": 0.0,
                "channels": record.get("channels") or [],
                "reason": (
                    "译名唯一性校验失败：译名被互不相关的术语共同声称"
                    "（不可能是同一个术语的正确译名）→ 交智能体裁决："
                    + "；".join(details)
                ),
            })
            continue
        record = dict(record)
        record["names"] = names
        record["confidence"] = round(max(0.0, float(record.get("confidence", 0)) - 0.15), 4)
        record["note"] = "译名唯一性校验摘除了冲突语言槽：" + "；".join(details)
        if record["confidence"] < min_accept_confidence:
            demoted.append({
                "term": term,
                "names": names,
                "confidence": record["confidence"],
                "channels": record.get("channels") or [],
                "reason": record["note"] + "（扣分后低于置信阈值）",
            })
            continue
        pruned[term] = record
    return pruned, demoted


def _target_slot(lang: str, target_languages: tuple[str, ...]) -> str:
    """把语言名映射到 ``target_languages`` 里对应的拼法。"""
    for target in target_languages:
        if termindex._term_language(target) == termindex._term_language(lang):
            return target
    return ""


def _foreign_literal_for(
    evidence: Iterable[tuple[str, str, dict]],
    value: str,
    *,
    min_hits: int = 2,
) -> str:
    """指定值在别的语言译文里"原样保留"的佐证文本（无则空串）。"""
    best = ""
    for _lang, candidate, payload in evidence:
        if candidate != value or not isinstance(payload, dict):
            continue
        foreign = (payload.get("evidence") or {}).get("foreign_literal") or {}
        hits = {lg: int(n) for lg, n in foreign.items() if int(n) >= min_hits}
        if not hits:
            continue
        rendered = ", ".join(f"{lg}×{n}" for lg, n in sorted(hits.items()))
        if len(rendered) > len(best):
            best = rendered
    return best


def _best_foreign_literal(
    evidence: Iterable[tuple[str, str, dict]],
    labels: set[str],
    *,
    min_hits: int = 2,
) -> str:
    """从 translit 证据里读出"跨语言字面命中"的佐证文本（无则空串）。

    只在候选值确实被采纳进 ``labels``（即胜出的那一支）时才采信，避免用被压下的
    备选候选的佐证去给另一个值背书。
    """
    best = ""
    for _lang, _value, payload in evidence:
        if not isinstance(payload, dict):
            continue
        foreign = (payload.get("evidence") or {}).get("foreign_literal") or {}
        hits = {lg: int(n) for lg, n in foreign.items() if int(n) >= min_hits}
        if not hits:
            continue
        rendered = ", ".join(f"{lg}×{n}" for lg, n in sorted(hits.items()))
        if len(rendered) > len(best):
            best = rendered
    return best


def _anchor_check(
    corpus: _Corpus,
    term: str,
    source_language: str,
    en_value: str,
) -> dict:
    """en 值的同点位锚定（``verify_triangle`` 的降级用法）。

    边界报告指出：glossary 覆盖不足时 aux 闭环偏严，未收录的术语不应因缺 aux
    译名被否。这里按建议以 ``max_aux_required=0`` 调用——只要求"英语候选在
    term 出现行的预测对应行里真的出现过"（锚定成立即算通过），未通过的调用方
    写入 ``pending`` 而非丢弃。

    **性能（关键）**：上游 ``verify_triangle`` 内部用
    ``_paired_stories(groups, "en", source_language)`` 作为 ``story_scope``，
    那是**全量语料**的 ja/en 配对故事（~13.5k）。虽然 ``_locate_term_lines``
    会对每个故事做 ``term not in text`` 快速否掉，但遍历 13.5k 个故事、逐个取
    页/行本身就与语料规模成正比；在 200 故事窗口 × 每个术语都调用一次的情形下
    这是纯浪费——**term 只可能出现在它自己的命中故事里**。这里把 groups 换成一个
    只暴露该术语命中故事的视图（:class:`_StoryScopedView`）：扫描面从全量降到
    term 的实际命中数，而判定完全等价（scope 之外的每个故事，上游的
    ``term not in text`` 都必然为真，本来就不会贡献命中故事）。

    结果按 (term, en_value) 缓存：同一术语在同一 en 值上只会被问一次（下游
    ``_merge_channels`` 逐术语调用），但同义路径（例如剪枝前的重复询问）不必重算。
    """
    key = (term, en_value)
    cached = corpus._anchors.get(key)
    if cached is not None:
        return cached
    scope = corpus.stories_with(term)
    if not scope:
        # 该术语没有已索引的出现故事：无法裁剪，退回全量（保守，结果同上）。
        target = corpus.groups
    else:
        target = corpus.anchor_view(scope)
    try:
        result = verify_triangle(
            term, source_language, en_value, "en", target,
            aux_languages=("zh_hans", "zh_hant", "ko"),
            max_aux_required=0,
        )
    except Exception as exc:  # 上游异常不阻断协同，按"未验证"处理
        return {"verified": False, "reason": f"verify_triangle 异常：{exc}"}
    out = {
        "verified": bool(result.get("verified")),
        "reason": str(result.get("reason", "")),
    }
    corpus._anchors[key] = out
    return out


# ── 主入口 ──────────────────────────────────────────────────────────

def scrub_trinity(
    groups: dict,
    stories: list[str],
    candidates: list[str],
    *,
    source_language: str = "ja",
    target_languages: tuple[str, ...] = ("zh_hans", "zh_hant", "en", "ko"),
    glossary=None,
    seed: set[str] | None = None,
    discovered: set[str] | None = None,
    vocab: dict | None = None,
    idf: dict | None = None,
    pair_index: dict | None = None,
) -> dict:
    """三位一体刮削主入口（签名固定，主线集成依赖）。

    流程：

    **第 0 层** ``candidate_tiers.classify`` 前置过滤 → 只放行 OFFICIAL / PROPER /
    STATISTICAL（通用词在此拦下，这是三组共有的最大噪声源的根治点）。

    **第 1 层** 三路并行（互不污染）：

    * ``trunk`` = ``align_term_by_frequency``（ja 主位分布对齐，原样保留）；
    * ``hub`` = ja 行位 → en 拉丁形态直取 → zh_hant/ko 同点位译名验证 → 回填；
    * ``translit`` = 片假名 → 罗马音 → 英语（``romaji`` sim ≥ 0.5 硬门控）。

    **第 2 层** 协同合并（见 :func:`_merge_channels`）。

    ``glossary`` 同时供 L0 官方层与 :func:`arbitrate` 的 ``glossary_names``；
    ``idf`` 缺省时主干静默跳过（``stats["skipped"]`` 记录原因），另两路仍工作。
    """
    warnings: dict[str, str] = {}
    glossary_list = list(glossary) if glossary is not None else None
    target_languages = tuple(t for t in (target_languages or ()) if t)
    corpus = _Corpus(groups, stories)

    # ── 第 0 层：前置过滤 ───────────────────────────────────────────
    official_keys: set[str] = set()
    glossary_names: dict[str, dict[str, str]] = {}
    surnames: dict[str, dict[str, Any]] = {}
    if glossary_list:
        # L0 官方名册只收实体类词条（NOUN_KINDS）——非实体词条存在同形异指脏数据
        # （プラネタリウム 的 honor 译名是鱼名、ハート 的 virtual_item 译名是烟花），
        # 不过滤会把它们当术语译名直接采纳。
        glossary_names = _glossary_name_index(glossary_list)
        official_keys = set(glossary_names)
        # 人物姓氏背书（汉字人名唯一可靠的证据源）。
        surnames = _surname_backing_index(glossary_list)
        official_keys |= set(surnames)
        # 三角闭环的 aux 名册 + 对齐上下文（verify_triangle 的模块级入口）。
        register_glossary_names(glossary_list)
        register_alignment_context(corpus.groups, source_language, idf, vocab)
    else:
        warnings["glossary"] = "未提供 glossary：L0 官方层与仲裁名册为空"

    seed_keys = {normalize_name(s) for s in (seed or set())}
    discovered_keys = {normalize_name(d) for d in (discovered or set())}
    unique: list[str] = []
    seen: set[str] = set()
    for term in (candidates or []):
        t = (term or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        unique.append(t)

    tiers: dict[str, Tier] = {}
    alignable: list[str] = []
    rejected: list[dict] = []
    for term in unique:
        tier = classify(
            term,
            language=source_language,
            official_keys=official_keys,
            seed_keys=seed_keys,
            discovered=discovered_keys,
        )
        tiers[term] = tier
        # 按值比较，不用 `is`：importlib.reload 会重建 Tier 枚举类。
        if tier == Tier.REJECT:
            rejected.append({
                "term": term,
                "stage": "layer0",
                "reason": "前置过滤：通用词/无专名特征（L3）",
            })
        else:
            alignable.append(term)
    tier_stats = tier_summary(tiers)

    if idf is None:
        warnings["trunk"] = (
            "未提供 idf：主干（分布对齐）跳过，需 build_alignment_resources"
        )

    # ── 第 1 层：三路并行 ───────────────────────────────────────────

    # 预热行位索引：hub / translit / glossary_backing 三者都用**同一份 scope**
    # （ja↔en 配对故事），但各自逐通道冷启动，等于把同一份 (term → 故事 → 行号)
    # 索引扫三遍（实测每遍 5 秒以上）。这里先用**全量可对齐候选**建一次，之后
    # 三个通道请求的术语集合都是它的子集，直接复用。
    # 依据：索引内容只依赖 (故事集合, 语言)，候选集合只决定扫哪些 term，命中是
    # 按位置 ``startswith`` 得到的（见 ``_Corpus.line_index``）。
    # 副产物 ``_term_stories`` 同时供锚定裁剪（``_anchor_check``）使用。
    if idf is not None:
        _en_scope = corpus.paired("en", source_language)
        if _en_scope:
            corpus.line_index(_en_scope, source_language, alignable)

    trunk = _channel_trunk(
        corpus, alignable,
        source_language=source_language,
        target_languages=target_languages,
        vocab=vocab, idf=idf, pair_index=pair_index,
    )
    # 主干家族扩展：glossary 人物词条的姓氏映射（汉字人名的唯一硬证据）。
    for term, lang_map in _channel_glossary_backing(
        corpus, alignable,
        source_language=source_language,
        target_languages=target_languages,
        surnames=surnames,
    ).items():
        trunk.setdefault(term, {}).update(lang_map)
    hub = _channel_hub(
        corpus, alignable,
        source_language=source_language,
        target_languages=target_languages,
        official_keys=official_keys,
        seed_keys=seed_keys,
        discovered_keys=discovered_keys,
    )
    translit = _channel_translit(
        corpus, alignable,
        source_language=source_language,
        target_languages=target_languages,
    )

    # ── L0 官方：直接采纳，不参与票选 ───────────────────────────────
    official_hits: dict[str, dict[str, dict[str, dict]]] = {}
    for term in alignable:
        names = glossary_names.get(normalize_name(term))
        if not names:
            continue
        lang_map: dict[str, dict[str, dict]] = {}
        for lang, value in names.items():
            if not value:
                continue
            slot = _slot_for(lang, target_languages)
            if slot != source_language and slot not in target_languages:
                continue
            lang_map.setdefault(slot, {})[str(value)] = {
                "value": str(value),
                "sim": 1.0,
                "evidence": {"channel": "L0", "official": True},
            }
        if lang_map:
            official_hits[term] = lang_map
    channels = {
        "L0": official_hits,
        "trunk": trunk,
        "hub": hub,
        "translit": translit,
    }

    # ── 第 2 层：协同合并 ───────────────────────────────────────────
    merged = _merge_channels(
        channels, corpus=corpus, source_language=source_language,
        glossary_names=glossary_names,
    )

    # ── 全局自洽性：译名唯一性校验（跨术语）─────────────────────────
    accepted, demoted = _prune_value_collisions(merged["accepted"])
    merged["accepted"] = accepted
    merged["pending"].extend(demoted)

    # 进入对齐但三路皆未产出、也未进冲突/待决队列的：记明"诚实留空"。
    produced = set(merged["accepted"]) | {row["term"] for row in merged["pending"]}
    produced |= {row["term"] for row in merged["conflicts"]}
    for term in alignable:
        if term in produced:
            continue
        rejected.append({
            "term": term,
            "stage": "channels",
            "reason": "三路均未穿透（门控严格/证据不足），诚实留空",
        })

    def _channel_stat(table: dict[str, dict[str, dict[str, dict]]]) -> dict[str, int]:
        return {
            "candidates": len(table),
            "accepted": sum(len(lang_map) for lang_map in table.values()),
        }

    stats = {
        "trunk": _channel_stat(trunk),
        "hub": _channel_stat(hub),
        "translit": _channel_stat(translit),
        "official": _channel_stat(official_hits),
        "tier_stats": {str(k): int(v) for k, v in tier_stats.items()},
        "agreement_boosted": merged["agreement_boosted"],
        "conflicts": len(merged["conflicts"]),
        "pending": len(merged["pending"]),
        "rejected": len(rejected),
        "total_accepted": len(merged["accepted"]),
        "alignable": len(alignable),
        "skipped": warnings,
    }
    return {
        "accepted": merged["accepted"],
        "conflicts": merged["conflicts"],
        "pending": merged["pending"],
        "rejected": rejected,
        "stats": stats,
        # 诊断用原始通道产出（不参与主线集成契约，供 verify/report 使用）。
        "channels_raw": {
            name: {
                term: {lang: sorted(value_map) for lang, value_map in lang_map.items()}
                for term, lang_map in table.items()
            }
            for name, table in (
                ("L0", official_hits), ("trunk", trunk),
                ("hub", hub), ("translit", translit),
            )
        },
    }


# ── 报告产物 ────────────────────────────────────────────────────────

def write_scrub_report(result: dict, out_dir) -> dict:
    """把 :func:`scrub_trinity` 结果写成可审计产物。

    产出 ``accepted.json`` / ``pending.json`` / ``conflicts.json`` /
    ``rejected.json`` 四个 JSON 与一份 ``README.md``（人类可读摘要，含各通道
    统计与样例）。返回 ``{文件名: 绝对路径}``。
    """
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    paths: dict[str, str] = {}

    def _dump(name: str, payload: Any) -> None:
        path = out_path / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8",
        )
        paths[name] = str(path)

    accepted = result.get("accepted") or {}
    pending = result.get("pending") or []
    conflicts = result.get("conflicts") or []
    rejected = result.get("rejected") or []
    stats = result.get("stats") or {}

    _dump("accepted.json", accepted)
    _dump("pending.json", pending)
    _dump("conflicts.json", conflicts)
    _dump("rejected.json", rejected)

    lines: list[str] = []
    lines.append("# 三位一体刮削报告")
    lines.append("")
    lines.append(f"- 采纳术语: **{len(accepted)}**")
    lines.append(f"- 交叉验证增益（agreement≥2）: **{stats.get('agreement_boosted', 0)}**")
    lines.append(f"- 冲突（同术语不同通道译名不一致）: **{len(conflicts)}**")
    lines.append(f"- 待决（低置信/未过锚定，可供智能体裁决）: **{len(pending)}**")
    lines.append(f"- 拒绝: **{len(rejected)}**")
    lines.append("")
    lines.append("## 各通道产出")
    lines.append("")
    lines.append("| 通道 | 命中术语数 | 译名对数 |")
    lines.append("| :--- | ---: | ---: |")
    for key in ("official", "trunk", "hub", "translit"):
        row = stats.get(key) or {}
        lines.append(f"| {key} | {row.get('candidates', 0)} | {row.get('accepted', 0)} |")
    lines.append("")
    lines.append("## 采纳样例（按置信度降序，前 20）")
    lines.append("")
    lines.append("| term | names | 置信度 | 通道 | agreement |")
    lines.append("| :--- | :--- | ---: | :--- | ---: |")
    for term, record in sorted(
        accepted.items(), key=lambda kv: (-kv[1].get("confidence", 0.0), kv[0])
    )[:20]:
        names = " / ".join(
            f"{lg}={value}" for lg, value in (record.get("names") or {}).items()
        )
        lines.append(
            f"| {term} | {names} | {record.get('confidence', 0):.2f} | "
            f"{', '.join(record.get('channels') or [])} | {record.get('agreement', 0)} |"
        )
    if conflicts:
        lines.append("")
        lines.append("## 冲突样例（前 10）")
        lines.append("")
        for row in conflicts[:10]:
            candidates = " / ".join(
                f"{value}({'+'.join(channels)})"
                for value, channels in (row.get("candidates") or {}).items()
            )
            lines.append(f"- **{row.get('term')}** [{row.get('lang')}] {candidates}")
    lines.append("")
    (out_path / "README.md").write_text("\n".join(lines), encoding="utf-8")
    paths["README.md"] = str(out_path / "README.md")
    return paths


# ── 与基线对比 ──────────────────────────────────────────────────────

def compare_with_baseline(result: dict, baseline: dict) -> dict:
    """与基线组对比（``baseline`` 形如 ``compare_three.json`` 的单组结构）。

    基线组结构（``group1_baseline`` / ``group2_zh_en_tw`` / ``group3_kata_first``）
    含 ``candidates``、``ja2zh_pairs`` / ``zh2en_pairs`` / ``kata2en_pairs`` 与
    ``sample_pairs``。产出：

    * ``coverage_delta``：基线口径的穿透对数与本次采纳数之差（含各通道口径与
      "单通道最大值"），用于验证协同增益（合并 > 单路最大）；
    * ``agreement_pairs``：本次触发交叉验证增益（≥2 路同值）的术语；
    * ``sample_comparison``：基线样例逐条对照（本次是否采纳、译名是否被覆盖）。
    """
    accepted = result.get("accepted") or {}
    stats = result.get("stats") or {}

    baseline_n = 0
    for key in ("ja2zh_pairs", "zh2en_pairs", "kata2en_pairs"):
        if key in (baseline or {}):
            baseline_n = int(baseline.get(key) or 0)
            break
    baseline_pairs: dict[str, str] = {}
    if isinstance((baseline or {}).get("pairs"), dict):
        baseline_pairs = {str(k): str(v) for k, v in baseline["pairs"].items()}
        baseline_n = len(baseline_pairs)
    else:
        baseline_pairs = {
            str(term): str(value)
            for term, value in ((baseline or {}).get("sample_pairs") or {}).items()
        }

    accepted_values = {
        _name_key(value)
        for record in accepted.values()
        for value in (record.get("names") or {}).values()
    }
    accepted_keys = {normalize_name(term) for term in accepted}

    sample_comparison: list[dict] = []
    covered = 0
    for term, value in baseline_pairs.items():
        value_covered = _name_key(value) in accepted_values
        same_term = normalize_name(term) in accepted_keys
        if value_covered:
            covered += 1
        record = accepted.get(term) or {}
        sample_comparison.append({
            "term": term,
            "baseline": value,
            "trinity_accepted_term": same_term,
            "value_covered": value_covered,
            "trinity_names": record.get("names") or {},
            "trinity_confidence": record.get("confidence"),
        })

    channel_totals = {
        name: int((stats.get(name) or {}).get("accepted", 0))
        for name in ("official", "trunk", "hub", "translit")
    }
    single_best = max(channel_totals.values()) if channel_totals else 0
    return {
        "baseline_pairs": baseline_n,
        "baseline_sample_pairs": len(baseline_pairs),
        "trinity_accepted": len(accepted),
        "coverage_delta": {
            "pairs": len(accepted) - baseline_n,
            "ratio": round(len(accepted) / baseline_n, 3) if baseline_n else None,
            "by_channel": channel_totals,
            "single_channel_max_pairs": single_best,
            "synergy_gain_over_single_channel": len(accepted) - single_best,
        },
        "agreement_pairs": [
            {
                "term": term,
                "names": record.get("names"),
                "agreement": record.get("agreement", 0),
                "channels": record.get("channels"),
            }
            for term, record in sorted(accepted.items())
            if int(record.get("agreement", 0)) >= 2
        ],
        "sample_comparison": sample_comparison,
        "baseline_sample_covered": covered,
        "baseline_sample_total": len(baseline_pairs),
    }
