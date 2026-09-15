"""术语候选三级分层判定 —— 术语穿透管线的"第 0 层"。

三组刮削策略对照实验（experiment/zh-en-tw/COMPARISON.md）得出的结论是：
三组的噪声同源，都出自"通用词进入对齐"。像「大家」「咖啡」「テスト」「クラス」
这类词根本没有专属译名，行位预测只能把它们落到任意感叹词/人名上，于是产出
`大家→Thank`、`テスト→Huh` 这类垃圾。修复的办法不是改对齐算法，而是在对齐
**之前**先把候选分层，只放行真正的专名候选。

分层模型（先命中先返回，顺序即优先级）::

    L0 OFFICIAL     官方词表命中 —— 直接采纳，无需门控
    L1 PROPER       形态专名（引号词 / Latin 混形 / 片假名串 / 汉字专名后缀）
    L2 STATISTICAL  统计发现词 —— 放行但仍需后续门控
    L3 REJECT       通用词 / 功能片段 / 噪声 —— 绝不进入对齐

设计约束：

* **零第三方依赖**，只用标准库；
* **不依赖包内其它模块**：``romaji`` 若存在则复用其通用片假名停用表，
  不存在时退回本模块内置表（``try/except ImportError``），
  因此本模块可以独立单测，也不会把导入链拖进重模块；
* 只做**判定**，不做发现，不写任何状态。

一个容易踩的点：本模块的归一化只用来做**键比较**，不改变放行后的候选表面。
调用方拿到的仍是原串（见 ``filter_alignable``），因为下游对齐要的是原文。
"""

from __future__ import annotations

import re
import unicodedata
from enum import Enum

__all__ = [
    "Tier",
    "classify",
    "classify_batch",
    "filter_alignable",
    "tier_summary",
]


class Tier(str, Enum):
    """候选可信度分层。继承 ``str`` 便于直接序列化进 JSON 诊断输出。"""

    OFFICIAL = "L0"      # 官方词表命中 —— 最高可信，直接采纳
    PROPER = "L1"        # 形态专名 —— 引号词 / Latin 混形 / 片假名串（非通用词）
    STATISTICAL = "L2"   # 统计发现词 —— 需后续门控
    REJECT = "L3"        # 明确拒绝 —— 通用词/功能片段/噪声，不得进入对齐


# ---------------------------------------------------------------------------
# 脚本判定用的字符区间
# ---------------------------------------------------------------------------

# 汉字（含扩展 A 区与兼容区）。范围写法与 termindex.py 保持一致。
_KANJI = "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
_HIRAGANA = "\u3041-\u309f"
_KATAKANA = "\u30a0-\u30ff"
_HANGUL = "\uac00-\ud7af\u1100-\u11ff\u3130-\u318f"
_LATIN = "A-Za-z"
# 片假名长音符与中黑点：既算片假名串的合法成分，也是形态特征。
_KATA_EXTRA = "\u30fc\u30fb"

_KANJI_RUN_RE = re.compile(f"[{_KANJI}]+")
_HIRAGANA_RUN_RE = re.compile(f"[{_HIRAGANA}]+")
_KATAKANA_RUN_RE = re.compile(f"[{_KATAKANA}]+")
_HANGUL_RUN_RE = re.compile(f"[{_HANGUL}]+")
_CJK_RUN_RE = re.compile(f"[{_KANJI}]+")

# 归一化时丢弃的噪声字符：空白、引号、成对标点、装饰符。
# 与 normalize.normalize_name 的口径保持一致（NFKC + casefold + 去符号），
# 但本模块自带一份，避免依赖包内模块。
_IGNORED_CHARS = re.compile(
    "[\u3000\\s_\\-.,，。！？!?·•×✕＊*:：;；'\"`~～【】\\[\\]()（）/\\\\|｜+＋]+"
)

# Latin 混形串里允许的连接符：Cheerful＊Days / Smile视频 / LUMINA时间
_MIXED_GLUE = "＊*・·•×✕/／+＋-ー＆&"

# 罗马数字与常见专名缩写（SEKAI 世界、N25、B♭ 等）
_ROMAN_NUMERAL_RE = re.compile(r"^[IVXLCDM]+$")


# ---------------------------------------------------------------------------
# 内置停用表（romaji.is_generic_katakana 不可用时的降级路径）
# ---------------------------------------------------------------------------

# 通用外来语片假名：日常词汇，绝无专属译名。取自 termindex 的
# _JA_KATAKANA_STOPWORDS / _JA_GENERIC_STOPWORDS 中确定性最高的部分，
# 加上实验里实际观测到的噪声词（テスト / クラス / バタバタ）。
_BUILTIN_GENERIC_KATAKANA = {
    # 实验直接观测到的噪声
    "テスト", "クラス", "バタバタ", "ステージ", "ライブ", "ニュース",
    "ラッキー", "カット", "カード", "コメント", "グループ", "メンバー",
    # 场所/设施
    "ホテル", "ルーム", "スタジオ", "ステージ", "アリーナ", "ドーム",
    "カフェ", "ショップ", "テント", "フィールド", "コース", "サイト",
    # 抽象/日常名词
    "イメージ", "メッセージ", "プレゼント", "パーティー", "コーヒー",
    "スマホ", "アプリ", "バイト", "チーム", "バンド", "レッスン",
    "サポート", "ゲーム", "スケジュール", "デザイン", "アイデア",
    "レベル", "スタート", "テーマ", "メニュー", "リズム", "テンション",
    "アドバイス", "タイミング", "パフォーマンス", "リハーサル",
    "オーディション", "アルバム", "ギター", "ピアノ", "ドラム",
    "パート", "ポスター", "バランス", "メイク", "チケット", "グッズ",
    "マネージャー", "モデル", "ページ", "クッキー", "テーブル",
    "メール", "アカウント", "リボン", "シャツ", "サンプル",
    "プロデューサー", "バージョン", "アーティスト", "デビュー",
    "セッション", "サービス", "スタッフ", "トーク", "カメラ",
    "チャンス", "トレーニング", "トップ", "ミーティング", "シェア",
    "ネット", "ペンギン", "マッサージ", "アーカイブ", "カメラマン",
    # 拟声/叠语（短促重复，非专名）
    "ドキドキ", "ワクワク", "ニコニコ", "キラキラ", "ハラハラ",
    "モヤモヤ", "フワフワ", "ポカポカ", "ノリノリ", "バラバラ",
    "メロメロ", "フラフラ", "ボロボロ", "ピカピカ", "サッパリ",
    "ソワソワ", "ペコペコ", "スベスベ", "ギリギリ", "バッチリ",
    "カワイイ", "キレイ", "スゴイ", "スゲー", "マジ", "ホント",
    "ダメ", "カンペキ", "ハイ", "クソ", "オシャレ",
    # 生活/食物
    "ケーキ", "クリーム", "プリン", "パフェ", "バナナ", "チョコ",
    "サンドイッチ", "パンケーキ", "ジュース", "スイーツ", "ミルク",
    # 普通名词（实验语料高频）
    "ミュージカル", "ゲスト", "サンタ", "ポジション", "リラックス",
    "ノート", "キャスト", "ケンカ", "マイク", "チラシ", "アップ",
    "フルーツ", "フレッシュ", "トレンド", "アクション", "イラスト",
    "タイプ", "スポット", "ソング", "ダンス", "ボール", "ストップ",
    "コーラス", "サークル", "パソコン", "バトル", "タイム", "シリーズ",
    "ホーム", "スケート", "アナウンス", "ポイント", "アニメ", "ハガキ",
    "プレイヤー", "ファッション", "チョコ", "チャンネル", "メイド",
    "タッチ", "コスメ", "クッション", "ヒント", "ライバル", "カイロ",
    "ロボット", "ミステリー", "パレード", "マラソン", "イベント",
    "ショー", "フェス", "ステップ", "ファン", "キャンペーン",
}

# 汉语通用词（功能词 / 高频日常词）：2 字纯汉字且在此表内直接拒绝。
_BUILTIN_GENERIC_ZH = {
    # 代词 / 指代
    "大家", "我们", "你们", "他们", "她们", "它们", "自己", "别人",
    "这个", "那个", "哪个", "什么", "怎么", "这里", "那里", "哪里",
    "时候", "地方", "东西", "事情", "样子", "感觉", "心情",
    # 连接 / 副词 / 助动词
    "因为", "所以", "但是", "虽然", "如果", "然后", "可是", "而且",
    "已经", "正在", "即将", "可以", "应该", "必须", "开始", "结束",
    "一起", "还是", "一定", "真的", "就是", "只是", "不是", "没有",
    "非常", "特别", "十分", "有点", "很多", "一些", "这些", "那些",
    "现在", "刚才", "马上", "立刻", "突然", "终于", "果然", "当然",
    # 动词 / 动宾
    "进行", "直播", "参加", "准备", "练习", "休息", "商量", "决定",
    "知道", "觉得", "认为", "希望", "想要", "需要", "喜欢", "讨厌",
    "感谢", "抱歉", "担心", "努力", "加油", "期待", "享受", "帮忙",
    # 日常名词
    "咖啡", "红茶", "牛奶", "面包", "蛋糕", "点心", "饮料", "食物",
    "音乐", "电影", "电视", "新闻", "天气", "时间", "地方", "公司",
    "学校", "朋友", "家人", "老师", "同学", "工作", "学习", "生活",
    "世界", "内心", "声音", "颜色", "味道", "样子", "计划", "目标",
    "问题", "办法", "结果", "原因", "方法", "意思", "内容", "情况",
}

# 汉语专名后缀：汉字 ≥3 且含其中之一 → 形态专名。
# 取自实验语料里实际出现的组织/设施/地点后缀。
_ZH_PROPER_SUFFIXES = (
    # 组织 / 团体
    "公司", "学园", "学院", "学校", "高校", "大学", "中学", "小学",
    "乐团", "剧团", "团队", "协会",
    "商会", "集团", "研究所", "委员会", "事务所", "工作室", "会社",
    # 设施 / 建筑
    "公园", "广场", "剧场", "博物馆", "美术馆", "图书馆", "体育馆",
    "运动中心", "活动中心", "车站", "机场", "港口", "酒店", "饭馆",
    "餐厅", "咖啡厅", "商店", "商店街", "专卖店", "中心",
    # 地点 / 地形
    "十字路口", "路口", "街道", "大街", "大道", "山丘", "高原",
    "森林", "海岸", "海岸线", "岛屿", "山脉", "村庄", "城镇",
    # 活动 / 节庆
    "庆典", "祭典", "音乐节", "演唱会", "展览", "大赛", "比赛",
    "派对", "活动", "纪念日", "出道曲",
    # 品牌 / 作品
    "品牌", "系列", "企划", "计划书",
)

_ZH_PROPER_SUFFIX_MIN_LEN = 3

# 日文语法片段（纯平假名）长度上限。实验中的「そうだった」「ありがとう」
# 这类长度 ≥6 的也有语法片段，但真正需要拦的是高频短片段；更长的平假名串
# 交给 L2 统计门控处理，这里不越权。
_JA_HIRAGANA_MAX = 5

# Korean: 纯谚文 ≤2 字视为功能片段（助词 / 短副词）。
_KO_HANGUL_MAX = 2


def _build_katakana_stopwords() -> set[str]:
    """优先复用 ``romaji.is_generic_katakana``。

    ``romaji.py`` 在本仓库中尚未存在（它是重构后续要新增的模块），因此这里
    必须能在其缺席时正常工作。若可用则用它的判定函数包装成一个集合式接口，
    否则退回内置停用表。
    """
    try:  # pragma: no cover - 取决于 romaji.py 是否存在
        from sekaisync.romaji import is_generic_katakana  # type: ignore
    except Exception:  # ImportError 及其它导入期错误都降级
        return set(_BUILTIN_GENERIC_KATAKANA)
    table = set(_BUILTIN_GENERIC_KATAKANA)

    def _generic(term: str) -> bool:
        try:
            return bool(is_generic_katakana(term))
        except Exception:
            return term in _BUILTIN_GENERIC_KATAKANA

    # 记录外部函数，供 _is_generic_katakana 追加查询。
    _EXTERNAL_GENERIC_CHECK["fn"] = _generic  # type: ignore[index]
    return table


# 外部通用片假名判定函数的挂载点（romaji.py 存在时由 _build_katakana_stopwords 填充）
_EXTERNAL_GENERIC_CHECK: dict[str, object] = {"fn": None}

_GENERIC_KATAKANA = _build_katakana_stopwords()


def _is_generic_katakana(term: str) -> bool:
    """通用外来语判定：既查停用表，也问外部函数（若可用）。"""
    t = term.strip()
    if not t:
        return False
    if t in _GENERIC_KATAKANA:
        return True
    fn = _EXTERNAL_GENERIC_CHECK.get("fn")
    if callable(fn):
        try:
            return bool(fn(t))
        except Exception:
            return False
    return False


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def _normalize_key(text: str) -> str:
    """比较键：NFKC + casefold + 去噪字符。

    与 ``normalize.normalize_name`` 口径一致但自带实现，本模块不 import 包内
    其它模块。注意：只用于**比较**，不改变放行候选的表面。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold()
    return _IGNORED_CHARS.sub("", text)


def _key_set(values: object) -> set[str]:
    """把调用方传入的集合预处理成归一化键集合（容忍 None / 空 / 非 str）。"""
    if not values:
        return set()
    out: set[str] = set()
    for value in values:  # type: ignore[union-attr]
        key = _normalize_key(str(value))
        if key:
            out.add(key)
    return out


# 汉语通用词黑名单的归一化形式，模块加载时算一次。
# 之前放在 classify 内部每次重建（150+ 项的集合推导），在万级候选上
# 是实打实的浪费。
_GENERIC_ZH_KEYS = {_normalize_key(word) for word in _BUILTIN_GENERIC_ZH}


# ---------------------------------------------------------------------------
# 脚本成分分析
# ---------------------------------------------------------------------------

def _script_counts(text: str) -> dict[str, int]:
    """统计各脚本字符数。用于"纯 X 脚本"类判定。"""
    counts = {"kanji": 0, "hiragana": 0, "katakana": 0, "hangul": 0, "latin": 0, "digit": 0}
    for ch in text:
        code = ord(ch)
        if ch.isascii() and ch.isalpha():
            counts["latin"] += 1
        elif ch.isascii() and ch.isdigit():
            counts["digit"] += 1
        elif 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF or 0xF900 <= code <= 0xFAFF:
            counts["kanji"] += 1
        elif 0x3041 <= code <= 0x309F:
            counts["hiragana"] += 1
        elif 0x30A0 <= code <= 0x30FF:
            counts["katakana"] += 1
        elif 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF or 0x3130 <= code <= 0x318F:
            counts["hangul"] += 1
    return counts


def _is_language(language: str, *prefixes: str) -> bool:
    """语言前缀匹配：``zh_hans`` / ``zh_hant`` / ``zh_tw`` 都归 ``zh``。"""
    lang = (language or "").strip().lower()
    return any(lang == p or lang.startswith(p + "_") for p in prefixes)


def _has_kanji(text: str) -> bool:
    return _KANJI_RUN_RE.search(text) is not None


# ---------------------------------------------------------------------------
# 形态专名规则
# ---------------------------------------------------------------------------

def _latin_forms(text: str) -> bool:
    """Latin 混形 / 首字母大写多词串判定。

    命中任一即视为形态专名：

    * Latin **混形串**：含 Latin 且总长度 ≥4，且 Latin 与其它脚本/符号相接
      ——``LUMINA时间`` / ``Smile视频`` / ``Cheerful＊Days``；
    * Latin **多词串**且每词首字母大写 —— ``Lasting ECHO Fes``；
    * 全大写或含内部大写的单 token —— ``SEKAI`` / ``KaTTo``。
    """
    stripped = text.strip()
    if not stripped:
        return False
    has_latin = any(ch.isascii() and ch.isalpha() for ch in stripped)
    if not has_latin:
        return False

    # 单 token：全大写，或首字母后还有大写（camelCase / KaTTo）。
    if re.fullmatch(f"[{_LATIN}][{_LATIN}0-9'’\\-]*", stripped):
        if len(stripped) < 2:
            return False
        if stripped.upper() == stripped and any(c.isalpha() for c in stripped):
            return True
        if any(c.isupper() for c in stripped[1:]):
            return True
        return False

    # 多 token：所有含字母的 token 首字母大写，且无停用功能词夹杂。
    tokens = [t for t in re.split(f"[{re.escape(_MIXED_GLUE)}\\s]+", stripped) if t]
    word_tokens = [t for t in tokens if re.search(f"[{_LATIN}]", t)]
    if len(word_tokens) >= 2 and all(
        t[0].isupper() and t.lower() not in _LATIN_FUNCTION_WORDS for t in word_tokens
    ):
        return True

    # 混形串：Latin 与其它脚本混排（非纯 ASCII），长度 ≥4。
    if len(stripped) >= 4 and not stripped.isascii():
        if any(not (ch.isascii() and (ch.isalnum() or ch in _MIXED_GLUE)) for ch in stripped):
            return True
    return False


# Latin 功能词：多词专名里只要出现就必须拦（"Thank you" 不是专名）
_LATIN_FUNCTION_WORDS = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at",
    "for", "with", "from", "by", "as", "is", "are", "was", "were", "be",
    "am", "do", "does", "did", "have", "has", "had", "will", "would",
    "can", "could", "should", "may", "might", "must", "this", "that",
    "these", "those", "it", "its", "you", "your", "i", "we", "he", "she",
    "they", "them", "me", "him", "her", "my", "our", "their", "his",
    "here", "there", "what", "when", "where", "which", "who", "why",
    "how", "not", "no", "yes", "oh", "ah", "uh", "huh", "hey", "hi",
    "hello", "thanks", "thank", "please", "sorry", "okay", "ok", "wow",
    "yeah", "yes", "well", "so", "very", "just", "like", "want",
}


def _is_katakana_run(text: str) -> bool:
    """整串（含长音符/中黑点）都是片假名。"""
    stripped = text.strip()
    if len(stripped) < 3:
        return False
    return all(
        0x30A1 <= ord(ch) <= 0x30FA or ch in _KATA_EXTRA for ch in stripped
    )


def _katakana_proper(text: str) -> bool:
    """片假名串专名：长度 ≥3 且不是通用外来语。"""
    stripped = text.strip()
    if not _is_katakana_run(stripped):
        return False
    return not _is_generic_katakana(stripped)


def _zh_proper_suffix(text: str) -> bool:
    """汉语专名后缀规则：汉字 ≥3 且含机构/设施/活动后缀。"""
    stripped = text.strip()
    if len(stripped) < _ZH_PROPER_SUFFIX_MIN_LEN:
        return False
    if not _has_kanji(stripped):
        return False
    return any(suffix in stripped for suffix in _ZH_PROPER_SUFFIXES)


# ---------------------------------------------------------------------------
# 主判定
# ---------------------------------------------------------------------------

def classify(
    term: str,
    *,
    language: str = "ja",
    official_keys: set[str] | None = None,
    seed_keys: set[str] | None = None,
    quoted: bool = False,
    discovered: set[str] | None = None,
) -> Tier:
    """判定单个候选的分层。

    判定顺序（先命中先返回）：

    1. ``official_keys`` 命中（归一化后比较）→ OFFICIAL
    2. ``quoted=True``（叙事引号内）→ PROPER
    3. ``seed_keys`` 命中（人工标注种子）→ PROPER
    4. 形态专名：
       - 含 Latin 且长度≥4 的混形串（LUMINA时间 / Smile视频 / Cheerful＊Days）
       - Latin 首字母大写的多词串（Lasting ECHO Fes）
       - 片假名串（长度≥3）且**不是通用外来语**
       → PROPER
    5. ``discovered`` 命中（统计发现词）→ STATISTICAL
    6. 其余 → REJECT

    语言适配（在 4/6 之间生效）：

    - 中文（``zh_*``）：纯汉字 2 字词若无官方/种子背书且形态无专名特征 → REJECT
      （避免「大家」「商量」这类通用词进入对齐）；汉字 ≥3 且含专名后缀
      （公司/学园/乐团/公园/庆典/十字路口 等）→ PROPER
    - 韩文（``ko``）：纯谚文且长度 ≤2 → REJECT
    - 日文（``ja``）：纯平假名且长度 ≤5 → REJECT（语法片段）
    """
    raw = (term or "").strip()
    if not raw:
        return Tier.REJECT

    key = _normalize_key(raw)
    if not key:
        return Tier.REJECT

    # 1. 官方词表：最高可信，直接采纳。
    if key in _key_set(official_keys):
        return Tier.OFFICIAL

    # 2. 叙事引号：文本自身已经把它标记成名字。
    if quoted:
        return Tier.PROPER

    # 3. 人工标注种子。
    if key in _key_set(seed_keys):
        return Tier.PROPER

    # 4. 形态专名。
    if _latin_forms(raw):
        return Tier.PROPER
    if _katakana_proper(raw):
        return Tier.PROPER
    # 汉字专名后缀是**脚本**特征而非语言特征：`language="ja"` 时
    # 「森之宫歌剧团」「神山高校」同样是专名，不应因 language 参数被漏判。
    if _zh_proper_suffix(raw):
        return Tier.PROPER

    # 4b. 语言适配的拒绝规则 —— 必须早于 discovered 命中，
    #     否则通用词/语法片段会被 L2 统计层再捞回来。
    counts = _script_counts(raw)
    total = sum(counts.values())
    if total == 0:
        # 无字母/数字/汉字的内容（纯符号、表情）不是术语。
        return Tier.REJECT

    if _is_language(language, "zh"):
        pure_kanji = counts["kanji"] == total
        if pure_kanji:
            # 2 字纯汉字：无官方/种子/形态背书 → 通用词概率极高。
            if len(raw) <= 2:
                return Tier.REJECT
            if key in _BUILTIN_GENERIC_ZH:
                return Tier.REJECT
    if _is_language(language, "ko"):
        if counts["hangul"] == total and len(raw) <= _KO_HANGUL_MAX:
            return Tier.REJECT
    if _is_language(language, "ja"):
        if counts["hiragana"] == total and len(raw) <= _JA_HIRAGANA_MAX:
            return Tier.REJECT
    # 通用词黑名单对任何语言都生效（「大家」出现在 ja 语料里同样是噪声）。
    if key in _GENERIC_ZH_KEYS:
        return Tier.REJECT
    if _is_generic_katakana(raw):
        return Tier.REJECT

    # 5. 统计发现词：放行，但交由后续门控复核。
    if key in _key_set(discovered):
        return Tier.STATISTICAL

    # 6. 其余：无形态特征、无背书、无统计支持 → 不进入对齐。
    return Tier.REJECT


def classify_batch(
    terms: list[str],
    **kwargs,
) -> dict[str, Tier]:
    """批量判定，返回 term -> Tier 映射。

    ``kwargs`` 原样透传给 :func:`classify`（``official_keys`` / ``seed_keys``
    等集合会在内部逐次归一化，量级为语料候选数时开销可忽略）。
    """
    return {term: classify(term, **kwargs) for term in (terms or [])}


def filter_alignable(
    terms: list[str],
    **kwargs,
) -> list[str]:
    """返回可进入对齐的候选（OFFICIAL / PROPER / STATISTICAL，排除 REJECT）。

    这是管线改造后替换现有 ``candidates`` 集合的直接接口：返回**原串**
    （未归一化、未去重后改写），并保持输入顺序，便于与既有对齐逻辑对接。
    重复项会去重但保留首次出现的顺序。
    """
    out: list[str] = []
    seen: set[str] = set()
    for term in (terms or []):
        if term in seen:
            continue
        seen.add(term)
        if classify(term, **kwargs) is not Tier.REJECT:
            out.append(term)
    return out


def tier_summary(tiers: dict[str, Tier]) -> dict[str, int]:
    """统计各层候选数，用于诊断输出。

    返回的键是 :class:`Tier` 的值（``L0``/``L1``/``L2``/``L3``），
    四层恒存在（哪怕计数为 0），便于直接打印成诊断表。
    """
    summary = {tier.value: 0 for tier in Tier}
    for value in (tiers or {}).values():
        try:
            key = Tier(value).value
        except ValueError:
            key = Tier.REJECT.value
        summary[key] += 1
    return summary
