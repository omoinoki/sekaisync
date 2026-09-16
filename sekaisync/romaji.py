"""片假名→罗马音转换与相似度打分（术语穿透"通道 C"门控核心）。

设计意图（`experiment/zh-en-tw/COMPARISON.md` 三组对照实验的结论 3 与落地优先级 1）：

- 片假名串是 Project Sekai 术语的主力（组 3 的 908 个候选占原算法候选的 73%），
  且能拿到 `ニーゴ→N25` 这类其他路径拿不到的缩写对；但通用外来语
  （テスト/クラス/バタバタ）同样会被收进来，与行位预测叠加后产生大量噪声。
- 实验已验证的事实：**真穿透是英语外来语的直接音译，相似度恒为 1.00**
  （`セカイ→SEKAI`、`カイト→KAITO`、`イオリ→Iori`）；噪声候选
  （`テスト→Huh`、`クラス→Hehe`、`バタバタ→Thank`）相似度 ≤0.33。
  因此 `sim >= 0.5` 是一条经验上有效的硬门控。
- 纯 `ratio` 会误杀缩写/变形对（`ニーゴ→N25` 只有 0.29、`カット→Cut` 只有 0.25），
  所以另加两条宽松规则补救：包含关系（子串/超串且长度比 ≥0.6）给 ≥0.7，
  「首字母相同的短数字缩写形态」（N25 这一类）给 0.6。

本模块零第三方依赖（只用 `re` / `difflib` / `unicodedata`），与项目
"纯标准库" 的核心特性一致；不导入包内其他模块，可独立单测。

转换规则采用赫本式（Hepburn）：拗音（キャ→kya、シュ→shu、チャ→cha）、
浊音/半浊音（ガ→ga、パ→pa）、促音ッ（双写下一辅音；チ系写作 tch，如
マッチ→matchi）、长音符ー（**忽略不延长**，故 ラッキー→rakki 而不是 rakkii）、
ヴ→vu、ン→n（不在 b/p/m 前改写成 m，保持确定性）。
非片假名字符原样保留并统一小写；无法识别的片假名字符跳过。
"""

from __future__ import annotations

import difflib
import re
import unicodedata

__all__ = [
    "katakana_to_romaji",
    "similarity",
    "is_plausible_translation",
    "is_generic_katakana",
]

# ── 片假名 → 罗马音（赫本式）────────────────────────────────────────

_MONO: dict[str, str] = {
    # 元音行
    "ア": "a", "イ": "i", "ウ": "u", "エ": "e", "オ": "o",
    # カ行 / ガ行
    "カ": "ka", "キ": "ki", "ク": "ku", "ケ": "ke", "コ": "ko",
    "ガ": "ga", "ギ": "gi", "グ": "gu", "ゲ": "ge", "ゴ": "go",
    # サ行 / ザ行
    "サ": "sa", "シ": "shi", "ス": "su", "セ": "se", "ソ": "so",
    "ザ": "za", "ジ": "ji", "ズ": "zu", "ゼ": "ze", "ゾ": "zo",
    # タ行 / ダ行
    "タ": "ta", "チ": "chi", "ツ": "tsu", "テ": "te", "ト": "to",
    "ダ": "da", "ヂ": "ji", "ヅ": "zu", "デ": "de", "ド": "do",
    # ナ行
    "ナ": "na", "ニ": "ni", "ヌ": "nu", "ネ": "ne", "ノ": "no",
    # ハ行 / バ行 / パ行
    "ハ": "ha", "ヒ": "hi", "フ": "fu", "ヘ": "he", "ホ": "ho",
    "バ": "ba", "ビ": "bi", "ブ": "bu", "ベ": "be", "ボ": "bo",
    "パ": "pa", "ピ": "pi", "プ": "pu", "ペ": "pe", "ポ": "po",
    # マ行
    "マ": "ma", "ミ": "mi", "ム": "mu", "メ": "me", "モ": "mo",
    # ヤ行
    "ヤ": "ya", "ユ": "yu", "ヨ": "yo",
    # ラ行
    "ラ": "ra", "リ": "ri", "ル": "ru", "レ": "re", "ロ": "ro",
    # ワ行 / ヴ / ン
    "ワ": "wa", "ヰ": "wi", "ヱ": "we", "ヲ": "wo",
    "ヴ": "vu", "ン": "n",
    # 単独で現れる小書き仮名（外来語表記の端）
    "ァ": "a", "ィ": "i", "ゥ": "u", "ェ": "e", "ォ": "o",
    "ャ": "ya", "ュ": "yu", "ョ": "yo", "ヮ": "wa",
    "ヵ": "ka", "ヶ": "ke",
}

# 拗音・外来語音（2 文字で 1 音）。長いキーを先に判定する。
_DIGRAPH: dict[str, str] = {
    "キャ": "kya", "キュ": "kyu", "キョ": "kyo",
    "ギャ": "gya", "ギュ": "gyu", "ギョ": "gyo",
    "シャ": "sha", "シュ": "shu", "ショ": "sho", "シェ": "she",
    "ジャ": "ja", "ジュ": "ju", "ジョ": "jo", "ジェ": "je",
    "チャ": "cha", "チュ": "chu", "チョ": "cho", "チェ": "che",
    "ヂャ": "ja", "ヂュ": "ju", "ヂョ": "jo",
    "ニャ": "nya", "ニュ": "nyu", "ニョ": "nyo",
    "ヒャ": "hya", "ヒュ": "hyu", "ヒョ": "hyo",
    "ビャ": "bya", "ビュ": "byu", "ビョ": "byo",
    "ピャ": "pya", "ピュ": "pyu", "ピョ": "pyo",
    "ミャ": "mya", "ミュ": "myu", "ミョ": "myo",
    "リャ": "rya", "リュ": "ryu", "リョ": "ryo",
    "イェ": "ye",
    "ウィ": "wi", "ウェ": "we", "ウォ": "wo",
    "ヴァ": "va", "ヴィ": "vi", "ヴェ": "ve", "ヴォ": "vo", "ヴュ": "vyu",
    "ファ": "fa", "フィ": "fi", "フェ": "fe", "フォ": "fo", "フュ": "fyu",
    "ティ": "ti", "テュ": "tyu", "トゥ": "tu",
    "ディ": "di", "デュ": "dyu", "ドゥ": "du",
    "ツァ": "tsa", "ツィ": "tsi", "ツェ": "tse", "ツォ": "tso",
    "スィ": "si", "ズィ": "zi",
}

# 促音・長音符・中黒（片假名ブロック内の記号）。
_SOKUON = "ッ"
_LONG_MARK = "ー"
_MIDDLE_DOT = "・"

_PURE_KATAKANA_RE = re.compile(r"^[ァ-ヺー・]+$")
_KEEP_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def _syllable(text: str, i: int) -> tuple[str | None, int]:
    """Return the romaji of the syllable starting at ``i`` and the next index.

    ``(None, i)`` when the character starts no syllable (ー・非假名・未知假名)，
    由调用方决定如何处理。
    """
    if i >= len(text):
        return None, i
    pair = text[i:i + 2]
    if len(pair) == 2 and pair in _DIGRAPH:
        return _DIGRAPH[pair], i + 2
    ch = text[i]
    if ch in _MONO:
        return _MONO[ch], i + 1
    return None, i


def katakana_to_romaji(text: str) -> str:
    """片假名串转罗马音（赫本式）。非片假名字符原样保留但会被规范化为小写。

    处理：促音ッ（双写下一辅音首字母，如 カット→katto、マッチ→matchi）、
    长音符ー（忽略，不重复前一元音）、拗音（キャ→kya、シュ→shu 等）、
    浊音/半浊音（ガ→ga、パ→pa）、ヴ→vu、ン→n（在 b/p/m 前写作 m 是可选惯例，
    这里统一写 n 以保证确定性）。无法识别的字符跳过。
    """
    if not text:
        return ""
    # 半角カナ・全角英数字统一到常规形（ﾃｽﾄ→テスト、Ｎ25→N25）。
    s = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == _SOKUON:
            # 促音：把下一音的首辅音双写；チ系按赫本式写作 tch（matchi）。
            nxt, j = _syllable(s, i + 1)
            if nxt:
                out.append("t" + nxt if nxt.startswith("ch") else nxt[0] + nxt)
                i = j
                continue
            i += 1
            continue
        if ch == _LONG_MARK:
            # 长音符忽略：ラッキー→rakki（不写 rakkii），与实验口径一致。
            i += 1
            continue
        if ch == _MIDDLE_DOT:
            # 中黒是连接符（バーチャル・シンガー），不承载音值。
            i += 1
            continue
        nxt, j = _syllable(s, i)
        if nxt:
            out.append(nxt)
            i = j
            continue
        # 非片假名（汉字/拉丁/平假名/未知字符）原样保留，最后统一小写。
        out.append(ch)
        i += 1
    return "".join(out).lower()


# ── 相似度 ──────────────────────────────────────────────────────────

# 缩写/品牌形态：短且同时含字母与数字（N25、25、M4 等）。
_ABBREV_FORM_RE = re.compile(r"^(?=[a-z0-9]{2,4}$)(?=.*[a-z])(?=.*[0-9]).*$")


def _romaji_key(romaji: str) -> str:
    return _KEEP_ALNUM_RE.sub("", romaji)


def _english_key(english: str) -> str:
    return _KEEP_ALNUM_RE.sub("", unicodedata.normalize("NFKC", english).casefold())


def similarity(katakana: str, english: str) -> float:
    """片假名与英语串的罗马音相似度，0.0-1.0。

    步骤：katakana→romaji（只保留字母数字），english 规范化为小写去非字母数字，
    用 `difflib.SequenceMatcher` 计算 ratio。两条宽松规则补救 pure ratio 的
    漏判（实验值：ニーゴ→N25 只有 0.29）：

    - 包含关系：罗马音是英语的子串或反之，且长度比 ≥0.6 → 至少 0.7
      （越长越接近 1.0）。用于 `イオリ→Iori` 之外的变形/带前缀候选。
    - 缩写形态：英语候选是"字母+数字"的短串（≤4 字符，如 N25）且首字母与
      罗马音一致 → 至少 0.6。这是 `ニーゴ→N25` 这类片假名简称与拉丁缩写
      配对的通道，也是唯一会放行非音译对的规则——它的误报面被"同点位候选"
      限制，接受这个风险以换取该类缩写对的召回。

      Astra P08/D08：这条规则给出的是**候选资格**（进池、参与排序），不是
      实体同一性证据。`ニーゴ→N25` 与 `ニーゴ→N99` 在它底下得分相同——它
      无法区分正确别名与任意同首字母缩写。因此 :func:`is_abbrev_form_only`
      单独暴露"此匹配仅由缩写形态规则抬到当前分数"这一事实，让调用方
      （trinity 的 translit 确认门槛）不把这类匹配当作已确认音译。
    """
    r = _romaji_key(katakana_to_romaji(katakana))
    e = _english_key(english)
    if not r or not e:
        return 0.0
    if r == e:
        return 1.0
    score = difflib.SequenceMatcher(None, r, e).ratio()
    if r in e or e in r:
        shorter, longer = (r, e) if len(r) <= len(e) else (e, r)
        len_ratio = len(shorter) / len(longer)
        if len(shorter) >= 2 and len_ratio >= 0.6:
            # 0.6 的长度比 → 0.70；完全包含且等长（即相等）→ 1.0。
            score = max(score, 0.7 + 0.3 * (len_ratio - 0.6) / 0.4)
    if _ABBREV_FORM_RE.match(e) and len(r) >= 2 and r[0] == e[0]:
        score = max(score, 0.6)
    return round(min(1.0, score), 4)


def is_abbrev_form_only(katakana: str, english: str) -> bool:
    """匹配分数是否**仅由缩写形态规则**支撑（Astra P08/D08）。

    True 表示：去掉缩写形态抬分后，romaji 相似度本身达不到 0.6 —— 即这对
    匹配的"已确认音译"身份完全来自"同首字母+数字形态"，而不是任何音译或
    包含关系。`ニーゴ→N25` 与 `ニーゴ→N99` 在这条规则下等价，因此它不能
    区分正确别名与邻近负例；调用方应把它当候选资格处理，而非已确认证据。
    """
    r = _romaji_key(katakana_to_romaji(katakana))
    e = _english_key(english)
    if not r or not e:
        return False
    if r == e:
        return False  # 完全相等是真音译，与缩写规则无关
    score = difflib.SequenceMatcher(None, r, e).ratio()
    if r in e or e in r:
        shorter, longer = (r, e) if len(r) <= len(e) else (e, r)
        len_ratio = len(shorter) / len(longer)
        if len(shorter) >= 2 and len_ratio >= 0.6:
            score = max(score, 0.7 + 0.3 * (len_ratio - 0.6) / 0.4)
    if score >= 0.6:
        return False  # 非缩写规则已独立达标
    return bool(_ABBREV_FORM_RE.match(e) and len(r) >= 2 and r[0] == e[0])


def is_plausible_translation(katakana: str, english: str, threshold: float = 0.5) -> bool:
    """门控判定：similarity >= threshold 且英语串本身形态合理（长度≥2、含字母）。

    这是通道 C 的采用判据：英语候选必须是拉丁字母串，纯数字（25）或单字符
    一律拒绝，避免行位预测落进符号/语气词时被当作译名。
    """
    if not katakana or not english:
        return False
    e = _english_key(english)
    if len(e) < 2 or not any(c.isalpha() for c in e):
        return False
    return similarity(katakana, english) >= threshold


# ── 通用片假名（无专属译名，不应进入对齐）────────────────────────────

# 日常外来语停用表：这些词是普通词汇（test/class/message/image 类），
# 在任何语言里都没有"专属译名"，对它们做穿透只会拿到行位噪声。
_GENERIC_KATAKANA: frozenset[str] = frozenset({
    # 实验报告点名的噪声源
    "テスト", "クラス", "メッセージ", "イメージ", "カット", "アイディア",
    "アイデア", "バタバタ", "ラッキー", "ワンマン", "ソフト", "ナイス",
    "ニュース", "ファン", "マネージャー", "バイト", "ボール", "スッキリ",
    "バラバラ", "バンド", "ステージ", "ライブ", "アタシ", "キッチン",
    "フォト", "サイン", "メモ", "グループ", "チーム", "メンバー",
    "パーティー", "イベント", "タイミング", "レベル", "スピード",
    "コンディション", "テンション", "コース", "パターン",
    # 日常外来语补充（高频、无专属译名）
    "アルバイト", "パソコン", "スマホ", "カメラ", "コーヒー", "カフェ",
    "ゲーム", "アニメ", "ドラマ", "ミュージック", "ソング", "ダンス",
    "リズム", "メロディ", "メロディー", "セリフ", "コメント", "レビュー",
    "プレゼント", "チケット", "グッズ", "ショップ", "スタイル", "デザイン",
    "デザイナー", "モデル", "プロ", "センス", "テクニック", "スキル",
    "パワー", "エネルギー", "タイム", "スケジュール", "ミーティング",
    "レッスン", "トレーニング", "ランニング", "ウォーキング", "ストレッチ",
    "ジャンプ", "ダッシュ", "スタート", "ストップ", "ゴール", "ルール",
    "サービス", "サポート", "アドバイス", "チャンス", "ミス", "トラブル",
    "ケンカ", "プレッシャー", "ストレス", "リラックス", "スマイル",
    "ドキドキ", "ワクワク", "キラキラ", "ニコニコ", "ハラハラ", "モヤモヤ",
    "フワフワ", "ポカポカ", "ノリノリ", "メロメロ", "フラフラ", "ボロボロ",
    "ギリギリ", "ピカピカ", "サッパリ", "ソワソワ", "ペコペコ", "バッチリ",
    "カンペキ", "オシャレ", "カワイイ", "キレイ", "スゴイ", "マジ",
    "ホント", "ダメ", "ハイ", "クソ", "ウソ", "ホントウ",
    "テーブル", "イス", "ドア", "ベッド", "カーテン", "ソファ", "トイレ",
    "シャワー", "エアコン", "コンビニ", "スーパー", "レストラン", "メニュー",
    "ケーキ", "クッキー", "チョコ", "チョコレート", "パン", "サンドイッチ",
    "ジュース", "コップ", "グラス", "フォーク", "ナイフ", "スプーン",
    "ポイント", "ページ", "サイト", "メール", "アカウント", "データ",
    "ファイル", "システム", "ネット", "オンライン", "アプリ", "バージョン",
    "シーズン", "シリーズ", "サイズ", "カラー", "タイプ", "ランキング",
    "エントリー", "スタッフ", "キャスト", "ゲスト", "ホテル", "ルーム",
    "アリーナ", "フィールド", "コート", "マラソン", "リレー", "ボランティア",
    "インタビュー", "スピーチ", "トーク", "アナウンス", "セール",
    "バーゲン", "サンプル", "サークル", "クラブ", "パーティ", "カラオケ",
})


def is_generic_katakana(term: str, extend: set[str] | None = None) -> bool:
    """判断片假名串是否为通用词（test/class/消息/形象 等日常外来语）。

    这类词没有专属译名，不应进入对齐。两级判定：

    1. 停用表命中（内置 `_GENERIC_KATAKANA` + 调用方 ``extend``，通常来自
       termindex 的 `_JA_GENERIC_STOPWORDS` 等既有词表）。
    2. 轻量形态规则（表格覆盖不到的拟声拟态词）：纯片假名，且
       - 畳語 4 字重复前两字（バタバタ/ドキドキ/ワクワク），或
       - 2 字目为促音ッ、末字为 リ（スッキリ/バッチリ/サッパリ）。

    刻意**不做**"纯片假名且罗马音长度 ≤8 即通用"的兜底长度规则：
    `セカイ`（sekai, 5）、`ニーゴ`（nigo, 4）、`カイト`（kaito, 5）这类
    短专名的长度完全落入该区间，一刀切会把通道 C 想抓的目标全部误杀。
    """
    if not term:
        return False
    t = unicodedata.normalize("NFKC", term).strip()
    if not t:
        return False
    if t in _GENERIC_KATAKANA:
        return True
    if extend and t in extend:
        return True
    if not _PURE_KATAKANA_RE.match(t):
        return False
    body = t.rstrip("ー")
    if len(body) == 4 and body[0:2] == body[2:4]:
        return True
    if len(body) >= 3 and body[1] == _SOKUON and body[-1] == "リ":
        return True
    return False
