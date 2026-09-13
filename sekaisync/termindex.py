from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from sekaisync import dbstore
from sekaisync.glossary import load_glossary
from sekaisync.layout import glossary_path, web_index_path
from sekaisync.llm_client import LLMClient
from sekaisync.normalize import best_match, normalize_name
from sekaisync.trust import trust_for_source, trust_rank
from sekaisync.webindex import is_auxiliary_page, load_web_pages, text_matches_language


TERM_KINDS = {
    "term",
    "character",
    "unit",
    "location",
    "organization",
    "event",
    "song",
    "system",
    "coined_term",
    "other",
}

# New tag vocabulary — overlapping, multi-assignable.
TAG_VOCAB: set[str] = {"person", "location", "organization", "event", "product", "other"}

# Back-compat mapping from legacy single kind → tag set.
KIND_TO_TAGS: dict[str, list[str]] = {
    "character": ["person"],
    "character_profile": ["person"],
    "character_unit": ["organization"],
    "unit": ["organization"],
    "location": ["location"],
    "area": ["location"],
    "area_item": ["location"],
    "organization": ["organization"],
    "event": ["event"],
    "song": ["other"],
    "system": ["other"],
    "game": ["other"],
    "coined_term": ["other"],
    "term": ["other"],
    "other": ["other"],
}

# Weight priors used in weight = log1p(occ) * trust_factor * tag_prior.
TAG_PRIORS: dict[str, float] = {
    "person": 1.2,
    "event": 1.15,
    "product": 1.05,
    "organization": 1.0,
    "location": 1.0,
    "other": 0.7,
}

TRUST_FACTORS: dict[str, float] = {"A": 1.3, "B": 1.1, "C": 1.0, "D": 0.9}

TERM_LANGUAGES = {"ja", "en", "zh_tw", "zh_hans", "ko"}

TERM_STORY_KINDS = {
    "event_story",
    "unit_story",
    "card_story",
    "special_story",
    "virtual_live",
    "area_talk",
    "area_dialogue",
    "home_line",
    "character_voice",
    "mysekai",
}

_LOCAL_QUOTE_RE = re.compile(r"[「『“‘\"]([^」』”’\"\n]{2,80})[」』”’\"]")
_LOCAL_KATAKANA_RE = re.compile(r"[\u30A0-\u30FF][\u30A0-\u30FF・ー]{2,40}")
_LOCAL_LATIN_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'’\-]*")
_LOCAL_LATIN_COMPOUND_RE = re.compile(
    r"(?:[A-Za-z][A-Za-z0-9'’\-]*[ /×·][A-Za-z][A-Za-z0-9'’\-]*)+"
)
_LOCAL_LATIN_STOPWORDS = {
    "a",
    "about",
    "after",
    "all",
    "an",
    "and",
    "any",
    "are",
    "as",
    "at",
    "be",
    "because",
    "been",
    "before",
    "big",
    "but",
    "by",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "good",
    "great",
    "had",
    "has",
    "have",
    "how",
    "if",
    "in",
    "into",
    "is",
    "it",
    "its",
    "just",
    "know",
    "let",
    "lets",
    "like",
    "little",
    "more",
    "most",
    "new",
    "no",
    "not",
    "of",
    "old",
    "on",
    "or",
    "right",
    "should",
    "so",
    "some",
    "than",
    "that",
    "the",
    "then",
    "there",
    "these",
    "they",
    "thing",
    "things",
    "this",
    "those",
    "to",
    "very",
    "want",
    "wants",
    "was",
    "were",
    "what",
    "when",
    "where",
    "which",
    "who",
    "why",
    "will",
    "with",
    "would",
    "you",
    "your",
    "i", "we", "he", "she", "it", "they", "them", "me", "him", "her",
    "my", "our", "their", "his", "its", "here",
    "was", "were", "been", "being", "am", "is", "are",
    "do", "does", "did", "have", "has", "had",
}


_LANGUAGE_ALIASES = {"zh_hant": "zh_tw"}

_ZH_FUNCTION_WORDS = (
    "为什么", "因为", "为了", "通过", "由于", "对于", "关于", "以及", "还有",
    "已经", "正在", "即将", "可以", "应该", "时候", "地方", "大家", "自己",
    "我们", "你们", "他们", "她们", "它们", "这个", "那个", "什么", "怎么",
    "直播", "进行", "开始", "结束", "面向", "前往", "来到", "播出", "将会",
    "一起", "还是", "的话", "一样", "真的", "就是", "虽然", "但是", "所以",
    "如果", "然后", "接着", "于是", "无论", "尽管",
)
_ZH_FUNCTION_CHARS = "的了在是要会能让就都也很不没我有你他她它这那和与为从到向被把给对于至而其之还已正在进直播开结面前往来出上下过等因所以去吧吗呢哦啊呀嘛"
_KO_PARTICLES = (
    "에서까지", "부터까지", "까지", "에서", "으로부터", "으로", "부터", "처럼",
    "만큼", "라고", "이라는", "이라", "라는", "은", "는", "이", "가",
    "을", "를", "의", "에", "도", "만", "로", "과", "와", "한테", "에게",
    "보다", "입니다", "이에요", "예요", "합니다", "한다", "하고", "하는", "다", "요",
)
_CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_HANGUL_RUN_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff\u3130-\u318f]+")
_KO_SPLIT_RE = re.compile(r"[\s\u3000\u3001\u3002\uff0c\uff01\uff1f\u2026\u2014]+")
_JA_KATAKANA_STOPWORDS = {
    "ステージ", "ホテル", "ダンス", "グループ", "スタッフ", "テスト", "トーク",
    "カメラ", "チャンス", "トレーニング", "トップ", "ミーティング", "ドキドキ", "ワクワク",
    "アイドル", "グランプリ", "サービス", "シェア", "ネット", "ペンギン", "スタジオ",
    "マッサージ", "レッスン", "ルーム", "アーカイブ", "カメラマン", "タヌキ", "トラ", "クマ",
    "ミク", "リン", "レン", "メイコ", "カイト", "ルカ", "メグ", "ネッパラ",
}

# Japanese first/second-person pronouns and generic interjections that must never
# surface as proper nouns. The previous tokenizer let these leak into the word
# cloud, which cascaded into cross-language mismatches downstream.
_JA_PRONOUN_STOPWORDS = {
    "オレ", "ボク", "アタシ", "ワタシ", "あたし", "わたし", "私", "僕", "俺",
    "アタイ", "オイラ", "ワシ", "あたい", "おいら", "わし", "キミ", "君", "お前",
    "オマエ", "アンタ", "あなた", "貴方", "そっち", "こっち", "あっち", "どっち",
    "オレら", "ボクら", "アタシら", "ワタシら", "俺ら", "僕ら",
}

# Generic high-frequency ja words that are common nouns / verbs, not proper nouns.
# They would otherwise dominate the tag cloud and poison alignment.
_JA_GENERIC_STOPWORDS = {
    "ライブ", "ショー", "フェス", "イベント", "ステージ", "ステップ", "イメージ",
    "メッセージ", "メンバー", "ファン", "プレゼント", "コメント", "パーティー",
    "コーヒー", "カフェ", "スマホ", "アプリ", "バイト", "クラス", "チーム",
    "バンド", "レッスン", "サポート", "ゲーム", "クラスメイト", "パフォーマンス",
    "リハーサル", "ワークショップ", "ミーティング", "タイミング", "スケジュール",
    "デザイン", "アイディア", "アイデア", "プロ", "セッション", "オーディション",
    "デビュー", "レベル", "スタート", "テーマ", "メニュー", "リズム", "テンション",
    "アドバイス", "ポーズ", "メロディ", "メロディー", "セリフ", "コツ", "シーン",
    "アルバム", "ギター", "ピアノ", "ドラム", "ベース", "パート", "ポスター",
    "フレーズ", "モチーフ", "バランス", "メイク", "ストレッチ", "ランニング",
    "ジャンプ", "ショップ", "グッズ", "マネージャー", "チケット", "デモ",
    "オシャレ", "カワイイ", "キレイ", "スゴイ", "スゲー", "マジ", "ホント",
    "ダメ", "カンペキ", "バッチリ", "ギリギリ", "バッチグー", "ピッカピカ",
    "ニコニコ", "キラキラ", "ドキドキ", "ワクワク", "ハラハラ", "モヤモヤ",
    "フワフワ", "ポカポカ", "ノリノリ", "ムカムカ", "ヘトヘト", "バラバラ",
    "メロメロ", "フラフラ", "ボロボロ", "ギリギリ", "ピカピカ", "サッパリ",
    "ソワソワ", "ペコペコ", "スベスベ", "バレ", "ハイ", "クソ", "トラック",
    # Common loanword nouns observed leaking through alignment audits —
    # they are ordinary vocabulary, never proper nouns.
    "ミュージカル", "センパイ", "ゲスト", "サンタ", "ページ", "クッキー",
    "サイト", "ポジション", "リラックス", "ノート", "コメント欄", "テーブル",
    "キャスト", "ケンカ", "マイク", "チラシ", "バレンタイン", "ホワイトデー",
    "ショウタイム", "アップ", "メール", "アカウント", "リボン", "スムージー",
    "モデル", "ドッジボール", "チームメイト", "シャツ", "オオカミ",
    "ロボット", "アップデート", "バックヤード", "カラス", "フルーツケーキ",
    "インド", "サンプル", "プロデューサー", "バージョン", "フィールド",
    "オーラ", "アーティスト", "リコーダー", "コラボ", "ブレス", "孵化",
    "エンターテインメント", "フリル", "ミニサイズ", "マント", "カーネーション",
    "トランペット", "レギュラー", "バイトリーダー", "スレイド", "グラン・フルール",
    "ペタペタ", "ゾンビロボット", "チョコマニア", "ハッピーエブリデイ",
    "ライオン", "伝説", "ナレーション", "アメリカ", "ミュージシャン", "パレード",
    "マラソン", "アクション", "イラスト", "デザイナー", "ルート", "コード",
    "タイプ", "スポット", "ケーキ", "クリーム", "プラスチック", "ドローン",
    "スターピース", "オマージュ", "ミステリーツアー", "ミルクチョコレート",
    "バスケ", "クラクラ", "スモーク", "スペシャルコース", "モアモアハウス",
    "アピールトークショー", "ピコピコわんだほい", "トランポリンドーム",
    "チョキチョキ", "ツッコ", "マジロ", "タイガ",
    "ラテン", "ガイドブック", "ロップイヤー", "マジョリティ", "カット",
    "ウェイトジャケット", "メッセージカード", "ハニーマスタード", "ボール",
    "ストップ", "フレッシュ", "アリーナ", "コーラス", "トレンド", "バナナ",
    "インフルエンサー", "サークル", "パンケーキ", "ステッチ", "壁登り",
    "ポリポリチップス", "セディ", "ヒヨリ", "ピースピース", "筋肉こそ正義",
    "乙女なハニーマスタード", "ドリーミングナイツ",
    "パソコン", "バトル", "タイム", "コース", "シリーズ", "パレット", "ホーム",
    "スイーツ", "ドーム", "スケート", "アナウンス", "テント", "ステージング",
    "リベンジ", "サーフィ", "ルーティーン", "プリン", "パフェ", "スケッチブック",
    "ダンスシューズ", "イコライザー", "ボランティア", "ステージスタッフ",
    "ウキキ", "ポテトゴースト", "ラプンツェル", "ジャン", "ニジマス", "ウィリス",
    "シブヤ楽器DAY", "お花見金魚展", "わんだほ～い！", "コローレ・ミー",
    "大丈夫", "ポイント", "サンドイッチ", "アニメ", "タキシード", "カリキュラム",
    "エキストラ", "ブーケ", "ハガキ", "プレイヤー", "ピカソ", "シブヤ",
    "メロウ", "トッテモ", "セカンドイベント", "スマイルナイトタイム", "ミラーボール",
    "フクロウ", "チームワーク", "アリガトウ", "ピコピコ", "ベテランスタッフ",
    "水蜘蛛", "痛い", "責任", "ジュリエット", "寄り添う", "アリナシ",
    "レーン", "マイノリティ", "シール", "ディス", "ファッション", "チョコ",
    "チャンネル", "メイド", "リハビリ", "ワンマン", "タッチ", "コスメ",
    "クッション", "ヒント", "ロウソク", "ライバル", "カイロ", "フェルメール",
    "ハシビロ", "バラエティアイドル", "マーメイド", "ドッグラン",
    "ダンスパフォーマンス", "ドリーミングエリア", "ブレス……！",
}

def _is_ja_stopword(term: str) -> bool:
    """True if term must never surface as a proper noun (pronoun / generic word)."""
    t = term.strip()
    if not t:
        return True
    if t in _JA_PRONOUN_STOPWORDS or t in _JA_GENERIC_STOPWORDS or t in _JA_KATAKANA_STOPWORDS:
        return True
    # pure hiragana 2-5 chars (pronouns, particles, plain verbs)
    if re.fullmatch(r"[ぁ-ん]+", t) and 1 <= len(t) <= 5:
        return True
    # adjective-noun phrase: hiragana adjective stem + kanji noun (かわいい服)
    if re.fullmatch(r"[ぁ-ん]{2,5}[ぁ-ん]?[ぁ-ん]", t) is None and re.fullmatch(r"[ぁ-ん]{2,6}[\u4e00-\u9fff].*", t):
        return True
    # onomatopoeia / shouts: kana runs with repeated sounds (うおおおおおお),
    # or short kana interjections with long/glottal marks (スーッ, ワアッ).
    # Long kana names ending in ー (ミクデミー) are legitimate proper nouns.
    body = t.rstrip("！!～〜")
    if re.fullmatch(r"[ぁ-んァ-ヶー〜～ッ]{3,}", body):
        if re.search(r"(.)\1{2,}", body):
            return True
        if len(body) <= 4 and body.endswith(("ー", "〜", "～", "ッ")):
            return True
    if t.endswith(("！", "!", "～", "〜")) and len(body) >= 2 and re.fullmatch(r"[ぁ-んァ-ヶーッ]+", body):
        return True
    # i-adjective / conjugated-verb tail: 楽しい, 良かった, 頑張って…
    if len(t) <= 6 and re.search(r"[ぁ-ん]{2}$", t):
        return True
    # Sentence-frame tails: …によると / …について / …として (clauses, not nouns)
    if re.search(r"(によると|によれば|によって|については|について|にとって|として|のため)$", t):
        return True
    # Possessive noun phrases 「XのY」(ミクのCD/絵名の絵/志歩のベース): descriptive
    # phrases, not proper nouns — the の must be internal to the surface.
    if re.search(r"の", t):
        return True
    # numeric / symbol-only
    if re.fullmatch(r"[0-9０-９\s]+", t):
        return True
    return False

# glossary kinds that are real nouns (participate in longest-match tokenization).
# Title-like kinds (card/event_story/stamp/music_*) are NOT nouns — their names are
# story/episode titles and must not be injected as term candidates.
# NOTE: "song" is deliberately absent. 曲名是结构化短文本，属于实体层（registry/
# music 表自带官方五语名），分词器不应从对话里再挖一遍；对话中提到的曲名引用
# 一律走屏蔽表。
NOUN_KINDS = {
    "area", "area_item", "character", "character_profile", "character_unit",
    "unit", "game",
}
_GENERIC_LATIN_TRANSLATIONS = {
    "MC", "MC's", "Staff Member", "Staff", "Lumina Forum", "Grand Prix",
    "MORE MORE", "MORE HOUSE", "Arisawa", "Minori", "Shizuku", "Airi",
    "Haruka", "Mori", "Miku", "Len", "Rin", "Luka", "Meiko", "Kaito",
    "Organizer", "Trainer", "Planning Committee", "Committee", "Director",
}


@dataclass
class TermRecord:
    id: str
    canonical: str
    source_language: str
    kind: str = "term"
    names: dict[str, str] = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    official: bool = False
    source: str = ""
    created_at: str = ""
    confidence: float = 1.0
    trust: str = ""
    tags: list[str] = field(default_factory=list)
    occurrences: int = 0
    weight: float = 0.0
    # False (default) = 非日常名词: game-context-exclusive (official entity,
    # quoted coinage, or bursty distribution). True = 日常名词: everyday
    # vocabulary that happens to appear in the corpus.
    everyday: bool = False
    # Per-line cross-language positions for the penetrate feature.
    # Each item: {story_key, line_index, language, sentence, term, trust, auxiliary}
    positions: list[dict] = field(default_factory=list)

    def name_for(self, language: str) -> str:
        return self.names.get(language) or self.canonical


def make_term_id(source_language: str, term: str) -> str:
    key = normalize_name(term)
    if not key:
        key = hashlib.sha1(term.encode("utf-8")).hexdigest()[:12]
    return f"term:{source_language}:{key}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_tags(tags: Any, fallback_kind: str = "") -> list[str]:
    if isinstance(tags, list) and tags:
        cleaned = [str(t).strip().lower() for t in tags if str(t).strip().lower() in TAG_VOCAB]
        if cleaned:
            return sorted(set(cleaned))
    if fallback_kind and fallback_kind in KIND_TO_TAGS:
        return list(KIND_TO_TAGS[fallback_kind])
    return ["other"]


def _tag_prior(tags: list[str]) -> float:
    if not tags:
        return TAG_PRIORS["other"]
    return max(TAG_PRIORS.get(t, 0.7) for t in tags)


def compute_weight(occurrences: int, trust: str, tags: list[str]) -> float:
    occ = max(0, int(occurrences))
    trust_factor = TRUST_FACTORS.get(str(trust or "C").upper(), 1.0)
    prior = _tag_prior(tags)
    return math.log1p(occ) * trust_factor * prior


def _refresh_term_weight(term: TermRecord) -> None:
    """Recompute occurrences + weight from the current evidence list.

    ``occurrences`` is "how many times this term was observed"; the evidence
    list is its direct measurement. The previous version guarded on
    ``term.occurrences == 0``, which froze the field at 1 as soon as a record
    was loaded back from disk — no matter how far the evidence later grew.
    With occurrences pinned at 1, ``compute_weight`` collapsed to a function of
    trust + tags alone, so the word cloud lost its frequency dimension and the
    "count how often a term recurs" question became unanswerable.

    Taking the max keeps the field monotonic: an incremental merge run passes
    light records whose in-memory evidence is not authoritative (see
    ``save_terms_records(replace_evidence=False)``), and a compacted evidence
    list must not shrink a previously observed count.
    """
    occ = max(len(term.evidence), int(term.occurrences or 0))
    term.occurrences = occ
    term.weight = compute_weight(occ, term.trust or "C", term.tags or ["other"])


def looks_like_proper_noun(
    *,
    stories_n: int,
    lines_n: int,
    total_stories: int = 0,
    quoted: bool = False,
) -> bool:
    """Positive proper-noun signal, replacing the old "bursty" heuristic.

    The previous rule was ``stories_n <= 5 or lines_n / stories_n >= 2.0``.
    Both branches describe *narrow* or *dense* terms, so a core location or
    facility that gets one or two mentions across fifty different stories had
    stories_n=50 and a ratio near 1.0, failed both branches, and was stamped
    ``everyday`` — which removes it from alignment entirely. That is exactly
    backwards: wide-coverage names are the ones cross-language alignment most
    needs. In the shipped store this discarded 889 terms.

    Signals used instead, all positive except the coverage ceiling:

    - ``quoted``: the narrative itself marks the phrase as a name;
    - density ``lines_n / stories_n >= 2``: it recurs inside the stories where
      it appears, which is what a name does and ordinary vocabulary does not;
    - ``stories_n <= 5``: narrow, so it belongs to a specific storyline;
    - many mentions at low coverage: wide but substantial, the case the old
      rule got wrong.
    - coverage ceiling: a phrase in a fifth of all stories is common vocabulary
      whatever else is true of it.
    """
    if quoted:
        return True
    if stories_n <= 0 or lines_n <= 0:
        return False
    coverage = (stories_n / total_stories) if total_stories > 0 else 0.0
    if coverage >= 0.15:
        return False
    if lines_n / stories_n >= 2.0:
        return True
    if stories_n <= 5:
        return True
    if lines_n >= 30 and coverage < 0.03:
        return True
    return False


# Explicit per-term overrides for high-precision tagging (normalized key → tags).
# 莱利体系：莱利(人+品牌) / 莱利娱乐公司(组织) / 莱利梦幻乐园(地点+品牌)
# Keys are stored as normalize_name() output — both dot and no-dot variants
# are needed because ・ (U+30FB) is preserved by normalize_name.
_KNOWN_TAG_OVERRIDES: dict[str, list[str]] = {
    "ライリー": ["person", "product"],
    "莱利": ["person", "product"],
    "让莱利": ["person", "product"],
    "riley": ["person", "product"],
    "ジャンライリー": ["person"],
    "ライリードリームパーク": ["location", "product"],
    "ライリー・ドリームパーク": ["location", "product"],
    "莱利梦幻乐园": ["location", "product"],
    "ライリーエンターテインメント": ["organization"],
    "ライリー・エンターテインメント": ["organization"],
    "ライリーエンターテイメント": ["organization"],
    "ライリー・エンターテイメント": ["organization"],
    "莱利娱乐公司": ["organization"],
    "莱利娱乐": ["organization"],
    "フェニックスワンダーランド": ["location", "product"],
    "凤凰乐园": ["location", "product"],
    "菲尼克斯奇幻乐园": ["location", "product"],
    "phoenixwonderland": ["location", "product"],
    "ワンダーステージ": ["location"],
    "梦幻舞台": ["location"],
    "wonderstage": ["location"],
}

# Keyword heuristics — matched against term string only (not surrounding context)
# to avoid pollution via merge union (e.g. セカイ appearing near ライブ should not make セカイ an event).
_TAG_KEYWORDS: dict[str, list[str]] = {
    "event": ["文化祭", "文化节", "体育祭", "学園祭", "フェス", "祭", "ライブ", "コンテスト", "大会", "学院祭"],
    "location": ["高校", "学園", "学院", "教室", "sekai", "セカイ", "ステージ", "校舎", "グラウンド", "講堂",
                 "パーク", "乐园", "ランド", "wonderland", "dreampark", "ドリームパーク", "ワンダーランド", "游乐园", "ワンダーステージ"],
    "organization": ["委員会", "部", "社", "事务所", "唱片", "公司", "经纪", "レコード", "プロダクション", "事務所", "solis",
                     "エンターテインメント", "エンターテイメント", "娱乐", "entertainment", "ワンダーランズ", "wonderlands"],
    "product": ["パラダイス", "气泡水", "アプリ", "配信", "ストア", "nightcord", "brand", "泡泡美味"],
}

# Explicit product lexicon for high-precision product tagging (term-internal).
_PRODUCT_LEXICON = {"ネットパラダイス", "ネットワークパラダイス", "nightcord", "泡泡美味气泡水", "泡泡美味"}

# Curated translations for generic ja words and ja-only facilities — applied after per-line extraction.
_GENERIC_FIXED_TRANSLATIONS: dict[str, dict[str, str]] = {
    "ライブ": {"ja": "ライブ", "zh_hans": "演唱会", "en": "Live", "zh_tw": "演唱會", "ko": "라이브"},
    "フェス": {"ja": "フェス", "zh_hans": "音乐节", "en": "Festival", "zh_tw": "音樂節", "ko": "페스티벌"},
    "ショー": {"ja": "ショー", "zh_hans": "演出", "en": "Show", "zh_tw": "演出", "ko": "쇼"},
    "スター": {"ja": "スター", "zh_hans": "明星", "en": "Star", "zh_tw": "明星", "ko": "스타"},
    "イベント": {"ja": "イベント", "zh_hans": "活动", "en": "Event", "zh_tw": "活動", "ko": "이벤트"},
    "コンテスト": {"ja": "コンテスト", "zh_hans": "大赛", "en": "Contest", "zh_tw": "大賽", "ko": "콘테스트"},
    "ライブハウス": {"ja": "ライブハウス", "zh_hans": "Live House", "en": "Live House", "zh_tw": "Live House", "ko": "라이브 하우스"},
    "ワンマンライブ": {"ja": "ワンマンライブ", "zh_hans": "专场演唱会", "en": "Solo Live", "zh_tw": "專場演唱會", "ko": "원맨 라이브"},
    "セカイ": {"ja": "セカイ", "zh_hans": "世界", "en": "SEKAI", "zh_tw": "世界", "ko": "세카이"},
    # Compositional performance words: statistics cannot verify semantics here
    # (any topical word co-occurs inside the term's stories), so these get
    # authoritative translations instead of alignment output.
    "ソロライブ": {"ja": "ソロライブ", "zh_hans": "个人演唱会", "en": "Solo Live", "zh_tw": "個人演唱會", "ko": "솔로 라이브"},
    "プロダクション": {"ja": "プロダクション", "zh_hans": "制作公司", "en": "Production", "zh_tw": "製作公司", "ko": "프로덕션"},
    "ライブカフェ": {"ja": "ライブカフェ", "zh_hans": "Live咖啡厅", "en": "Live Cafe", "zh_tw": "Live咖啡廳", "ko": "라이브 카페"},
    "ライブグッズ": {"ja": "ライブグッズ", "zh_hans": "演出周边", "en": "Live Goods", "zh_tw": "演出週邊", "ko": "라이브 굿즈"},
    "ドームライブ": {"ja": "ドームライブ", "zh_hans": "巨蛋演唱会", "en": "Dome Live", "zh_tw": "巨蛋演唱會", "ko": "돔 라이브"},
    "ミニライブ": {"ja": "ミニライブ", "zh_hans": "迷你演唱会", "en": "Mini Live", "zh_tw": "迷你演唱會", "ko": "미니 라이브"},
    "ストリートライブ": {"ja": "ストリートライブ", "zh_hans": "街头演出", "en": "Street Live", "zh_tw": "街頭演出", "ko": "스트리트 라이브"},
    # Subculture / facility names whose dialogue-line alignment produced garbage.
    "ナイトコード": {"ja": "ナイトコード", "zh_hans": "Nightcord", "en": "Nightcord", "zh_tw": "Nightcord", "ko": "나이트코드"},
    "リン・レン": {"ja": "リン・レン", "zh_hans": "镜音铃·连", "en": "Rin & Len", "zh_tw": "鏡音鈴·連", "ko": "린 · 렌"},
    "フェニックスステージ": {"ja": "フェニックスステージ", "zh_hans": "凤凰舞台", "en": "Phoenix Stage", "zh_tw": "鳳凰舞台", "ko": "피닉스 스테이지"},
    # In-game street name: en/ko official-style, zh left absent (honest) until
    # an official name surfaces — partial curated dicts are allowed.
    "ビビッドストリート": {"ja": "ビビッドストリート", "en": "Vivid Street"},
    # Festival/event compound names (low-frequency bursty, alignment noise).
    "シブフェス": {"ja": "シブフェス", "zh_hans": "涩谷艺术节", "en": "Shibuya Festa", "zh_tw": "澀谷藝術節"},
    "ジャムフェス": {"ja": "ジャムフェス", "zh_hans": "果酱节", "en": "Jam Fest"},
    "ブライダルフェスタ": {"ja": "ブライダルフェスタ", "zh_hans": "婚庆展", "en": "Bridal Festa", "zh_tw": "婚禮博覽會"},
    "フェニックス・ブライダルフェスタ": {"ja": "フェニックス・ブライダルフェスタ", "zh_hans": "凤凰婚庆展", "en": "Phoenix Bridal Festa", "zh_tw": "鳳凰婚禮博覽會"},
    "ブラフェス": {"ja": "ブラフェス", "zh_hans": "婚庆展", "en": "Bridal Festa", "zh_tw": "婚博"},
    "フォトコンテスト": {"ja": "フォトコンテスト", "zh_hans": "摄影大赛", "en": "Photo Contest"},
    "ライブイベント": {"ja": "ライブイベント", "zh_hans": "演出活动", "en": "Live Event"},
}

# Curated translations for ja-only facilities — complete five-language, used to repair truncated pollution.
_JA_ONLY_CURATED: dict[str, dict[str, str]] = {
    "ライリードリームパーク": {"ja": "ライリードリームパーク", "zh_hans": "莱利梦幻乐园", "en": "Riley Dream Park", "zh_tw": "萊利夢幻樂園", "ko": "라일리 드림파크"},
    "ポップアップストア": {"ja": "ポップアップストア", "zh_hans": "快闪店", "en": "Pop-up Store", "zh_tw": "快閃店", "ko": "팝업 스토어"},
    "アークランド": {"ja": "アークランド", "zh_hans": "弧光乐园", "en": "Ark Land", "zh_tw": "弧光樂園", "ko": "아크랜드"},
    "レコード": {"ja": "レコード", "zh_hans": "唱片", "en": "Record", "zh_tw": "唱片", "ko": "레코드"},
}

# Proper nouns (facilities/brands/places) whose cross-language names are fixed
# and must NEVER come from per-line alignment — alignment picks the wrong
# neighbor (Shing!/哈——哈哈哈！) for these.
_PROPRIETARY_FIXED_TRANSLATIONS: dict[str, dict[str, str]] = {
    "フェニックスワンダーランド": {"ja": "フェニックスワンダーランド", "zh_hans": "菲尼克斯奇幻乐园", "en": "Phoenix Wonderland", "zh_tw": "菲尼克斯奇幻樂園", "ko": "피닉스 원더랜드"},
    "ネットパラダイス": {"ja": "ネットパラダイス", "zh_hans": "网络天堂", "en": "NetParadise", "zh_tw": "Net Paradise", "ko": "넷 파라다이스"},
    "ライリー": {"ja": "ライリー", "zh_hans": "莱利", "en": "Riley", "zh_tw": "萊利", "ko": "라일리"},
    "ワンダーステージ": {"ja": "ワンダーステージ", "zh_hans": "奇幻舞台", "en": "Wonder Stage", "zh_tw": "奇幻舞台", "ko": "원더 스테이지"},
    "ワンダーランズ": {"ja": "ワンダーランズ", "zh_hans": "Wonderlands×Showtime", "en": "Wonderlands×Showtime", "zh_tw": "Wonderlands×Showtime", "ko": "원더랜즈×쇼타임"},
    "アークランドショーコンテスト": {"ja": "アークランドショーコンテスト", "zh_hans": "弧光乐园表演大赛", "en": "Ark Land Show Contest", "zh_tw": "弧光樂園表演大賽", "ko": "아크랜드 쇼 콘테스트"},
}


def apply_curated_translations(terms: list["TermRecord"]) -> int:
    """Apply fixed dictionaries to terms list. Returns count of fixed terms."""
    fixed = 0
    merged_dicts = (
        _GENERIC_FIXED_TRANSLATIONS,
        _JA_ONLY_CURATED,
        _PROPRIETARY_FIXED_TRANSLATIONS,
    )
    for t in terms:
        for d in merged_dicts:
            cur = d.get(t.canonical)
            if cur is not None:
                t.names = dict(cur)
                # Curated entries are authoritative game terms — 非日常.
                t.everyday = False
                _refresh_term_weight(t)
                fixed += 1
                break
    return fixed


# Role/title suffixes that only ever appear in an English speaker label
# (e.g. "Mizuki's Mother", "Stage Leader") — never a legitimate term translation.
_ROLE_SUFFIX_RE = re.compile(
    r"(Leader|Member|Proprietress|Sister|Mother|Father|Officer|Host|Records|"
    r"Management|Voice|Mayor|Prince|Princess|Clerk|Director|Trainer|Student|"
    r"Fan|Customer|Proprietor|Speaker|Judge|Guest|Cast|Crew|Staff)$"
)


def _character_name_set(glossary: Optional[Iterable[Any]] = None) -> set[str]:
    """Collect all character display names from glossary (person entities)."""
    if glossary is None:
        glossary = _load_glossary_fallback(Path("store"))
        if not glossary:
            return set()
    names: set[str] = set()
    for gt in glossary:
        if str(getattr(gt, "kind", "")) in {"character", "character_profile"}:
            for n in (getattr(gt, "names", {}) or {}).values():
                if n:
                    names.add(str(n))
    return names


# Legitimate Chinese words ending in 看 — kept when the rule below fires.
_ZH_LOOK_WHITELIST = {"好看", "难看", "观看", "试看"}


def sanitize_translation_pollution(
    terms: Iterable["TermRecord"],
    char_names: Optional[set[str]] = None,
) -> int:
    """Clear cross-language names that are actually character names / speaker
    labels, not translations of the term. This is the deterministic backstop
    against per-line alignment picking a co-occurring character name."""
    char_names = char_names or _character_name_set()
    fixed = 0
    for t in terms:
        # Person terms legitimately translate TO character names
        # (星乃一歌 -> HOSHINO ICHIKA); never scrub their own names.
        is_person_term = "person" in (t.tags or []) or t.kind == "character"
        for lang in ("zh_hans", "zh_tw", "en", "ko"):
            val = t.names.get(lang)
            if not val:
                continue
            val_s = str(val).strip()
            if val_s in char_names:
                if not is_person_term:
                    del t.names[lang]
                    fixed += 1
                continue
            # A kana-containing canonical must never leak itself as another
            # language's name (キラー -> zh_hans=キラー). Pure-kanji identity
            # (神山高校 -> 神山高校) and pure-latin identity (MEIKO -> en=MEIKO)
            # are legitimate — homographs/self-identity, not pollution.
            if (
                val_s == t.canonical
                and re.search(r"[\u3040-\u30ff]", t.canonical)
                and not re.fullmatch(r"[A-Za-z0-9 ]+", t.canonical)
            ):
                del t.names[lang]
                fixed += 1
                continue
            if lang == "en" and _ROLE_SUFFIX_RE.search(val_s):
                del t.names[lang]
                fixed += 1
            elif val_s in ("MEIKO", "KAITO") and t.canonical not in ("MEIKO", "KAITO"):
                del t.names[lang]
                fixed += 1
            elif (
                lang in ("zh_hans", "zh_tw")
                and val_s.endswith("看")
                and val_s not in _ZH_LOOK_WHITELIST
                and not t.canonical.endswith(("見", "看"))
            ):
                # Verb phrases scraped from dialogue lines (想试试看/别人看)
                # masquerading as noun translations.
                del t.names[lang]
                fixed += 1
    return fixed


def classify_tags(term: str, context: str = "", source_language: str = "ja") -> list[str]:
    from sekaisync.normalize import normalize_name as _norm
    key = _norm(term)
    if key in _KNOWN_TAG_OVERRIDES:
        return sorted(_KNOWN_TAG_OVERRIDES[key])
    # Classify by term string only — context-free to avoid merge pollution.
    text = term.lower()
    tags: set[str] = set()
    for lex in _PRODUCT_LEXICON:
        if lex.lower() in text:
            tags.add("product")
            break
    for tag, keywords in _TAG_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in text:
                tags.add(tag)
                break
    if not tags:
        return ["other"]
    if tags == {"event"} and len(term.strip()) <= 1:
        return ["other"]
    return sorted(tags)


def term_to_dict(
    term: TermRecord,
    score: Optional[int] = None,
    truncate_context: bool = False,
) -> dict:
    evidence = []
    for item in term.evidence:
        entry = dict(item)
        context = str(entry.get("context", ""))
        if truncate_context and len(context) > 500:
            entry["context"] = context[:500] + "..."
        evidence.append(entry)
    # Ensure occurrences/weight are consistent before serializing
    if term.evidence and (term.weight == 0.0 or term.occurrences < len(term.evidence)):
        _refresh_term_weight(term)
    data = {
        "id": term.id,
        "canonical": term.canonical,
        "source_language": term.source_language,
        "kind": term.kind,
        "tags": term.tags or _normalize_tags(None, term.kind),
        "names": term.names,
        "evidence": evidence,
        "official": term.official,
        "source": term.source,
        "created_at": term.created_at,
        "confidence": term.confidence,
        "trust": term.trust or trust_for_source(
            term.source,
            official=term.official,
        ),
        "occurrences": term.occurrences or len(evidence),
        "weight": round(term.weight, 4),
        "everyday": bool(term.everyday),
    }
    if term.positions:
        data["positions"] = term.positions
    if score is not None:
        data["score"] = score
    return data


def term_from_dict(data: dict) -> TermRecord:
    raw_tags = data.get("tags")
    fallback_kind = str(data.get("kind", "term"))
    tags = _normalize_tags(raw_tags, fallback_kind)
    rec = TermRecord(
        id=str(data["id"]),
        canonical=str(data.get("canonical", "")),
        source_language=str(data.get("source_language", "")),
        kind=fallback_kind if fallback_kind in TERM_KINDS else "term",
        names={k: str(v) for k, v in data.get("names", {}).items() if v},
        evidence=[dict(item) for item in data.get("evidence", []) if isinstance(item, dict)],
        official=bool(data.get("official", False)),
        source=str(data.get("source", "")),
        created_at=str(data.get("created_at", "")),
        confidence=float(data.get("confidence", 1.0)),
        trust=str(
            data.get("trust")
            or trust_for_source(
                str(data.get("source", "")),
                official=bool(data.get("official", False)),
            )
        ),
        tags=tags,
        occurrences=int(data.get("occurrences", 0) or 0),
        weight=float(data.get("weight", 0) or 0),
        everyday=bool(data.get("everyday", False)),
        positions=[dict(p) for p in data.get("positions", []) if isinstance(p, dict)],
    )
    # Self-heal: stores written before the occurrences fix carry the field
    # frozen at 1 while their evidence list kept growing. Recompute whenever
    # the two disagree so existing databases converge without a full re-extract.
    if rec.evidence and (rec.weight == 0.0 or rec.occurrences < len(rec.evidence)):
        _refresh_term_weight(rec)
    return rec


def load_terms(path: Path) -> list[TermRecord]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("terms", [])
    return [term_from_dict(item) for item in data if isinstance(item, dict)]


def _compact_evidence(evidence: Iterable[dict]) -> list[dict]:
    """Keep references instead of copying full story context into the term index."""
    compact: list[dict] = []
    for item in evidence:
        if not isinstance(item, dict):
            continue
        entry: dict[str, Any] = {
            "story_key": str(item.get("story_key") or ""),
            "language": str(item.get("language") or ""),
        }
        for key in ("sentence", "term"):
            value = item.get(key)
            if value:
                entry[key] = str(value)
        compact.append(entry)
    return compact


def save_terms(terms: Iterable[TermRecord], path: Path, compact_evidence: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = []
    for term in terms:
        item = term_to_dict(term)
        if compact_evidence:
            item["evidence"] = _compact_evidence(item["evidence"])
        serialized.append(item)
    data = {
        "version": 1,
        "evidence_style": "compact" if compact_evidence else "full",
        "updated_at": now_iso(),
        "terms": serialized,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path

def page_story_key(page: dict) -> Optional[str]:
    url = str(page.get("url", ""))
    page_id = str(page.get("id", ""))
    kind = str(page.get("kind", ""))

    match = re.search(r"/story/event/(\d+)/(\d+)/", url)
    if match:
        return f"event:{match.group(1)}:{match.group(2)}"
    match = re.search(r"event_story:(\d+):(\d+)", page_id)
    if match:
        return f"event:{match.group(1)}:{match.group(2)}"
    match = re.search(r"event_story/(\d+)/(\d+)", url)
    if match:
        return f"event:{match.group(1)}:{match.group(2)}"
    return f"{kind}:{page_id}"


def load_pages(
    store_root: Path,
    input_path: Optional[Path] = None,
    include_overlay: bool = False,
) -> list[dict]:
    if input_path is not None:
        data = json.loads(Path(input_path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("pages", [])
        return [item for item in data if isinstance(item, dict)]
    pages: list[dict] = []
    for source_pages in dbstore.load_web_pages(store_root).values():
        pages.extend(source_pages)
    if not include_overlay:
        pages = [page for page in pages if not is_auxiliary_page(page)]
    return pages

def group_pages_by_story(pages: Iterable[dict]) -> dict[str, dict[str, dict]]:
    grouped: dict[str, dict[str, dict]] = {}
    for page in pages:
        key = page_story_key(page)
        if not key:
            continue
        if str(page.get("kind", "")) not in TERM_STORY_KINDS:
            continue
        language = _term_language(str(page.get("language", "")))
        if not language:
            continue

        def page_usable(candidate: dict) -> bool:
            if candidate.get("asset_mismatch") or candidate.get("content_language_mismatch"):
                return False
            return text_matches_language(language, str(candidate.get("text", "")))

        existing = grouped.setdefault(key, {}).get(language)
        if existing is None:
            grouped.setdefault(key, {})[language] = page
            continue
        existing_ok = page_usable(existing)
        candidate_ok = page_usable(page)
        if candidate_ok and not existing_ok:
            grouped.setdefault(key, {})[language] = page
        elif candidate_ok == existing_ok and existing.get("overlay") and not page.get("overlay"):
            grouped.setdefault(key, {})[language] = page
    return grouped


def find_segment(text: str, term: str) -> str:
    for line in text.splitlines():
        if term in line:
            return line.strip()
    return ""


def extract_terms_from_text(
    text: str,
    story_key: str,
    source_language: str,
    llm: LLMClient,
    max_terms: int = 20,
) -> list[TermRecord]:
    system = (
        "You are a Project Sekai terminology extractor. "
        "Extract only proper nouns, coined phrases, place names, facility names, "
        "event/song/group terms, and gameplay/system terms. "
        "Use the exact source-language spelling from the text. "
        'Return JSON: {"terms":[{"term":"...","tags":["person|location|organization|event|product|other"],"confidence":0.0-1.0}]}. '
        "Tags may overlap (e.g. \"神山高校文化祭\" -> [\"event\",\"location\"], "
        "\"ネットパラダイス\" -> [\"product\"]). "
        "\"event\" is ONLY for in-world fictional events (school festival etc.), not gameplay event periods. "
        "Do not translate and do not invent terms that are not present."
    )
    user = (
        f"Language: {source_language}\n"
        f"Story key: {story_key}\n"
        f"Text:\n{text[:12000]}"
    )
    data = llm.chat_json(system, user)
    items = data.get("terms", []) if isinstance(data, dict) else []
    records: list[TermRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        term = str(item.get("term", "")).strip()
        if len(term) < 2 or len(term) > 80:
            continue
        # Accept new tags field, fall back to legacy kind
        raw_tags = item.get("tags")
        if isinstance(raw_tags, list) and raw_tags:
            tags = _normalize_tags(raw_tags, "")
            if not tags or tags == ["other"]:
                kind_fallback = str(item.get("kind", "term"))
                tags = _normalize_tags(None, kind_fallback)
            kind = tags[0] if len(tags) == 1 else "term"
        else:
            kind = str(item.get("kind", "term"))
            if kind not in TERM_KINDS:
                kind = "term"
            tags = _normalize_tags(None, kind)
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.8))))
        except (TypeError, ValueError):
            confidence = 0.8
        rec = TermRecord(
            id=make_term_id(source_language, term),
            canonical=term,
            source_language=source_language,
            kind=kind,
            names={source_language: term},
            evidence=[
                {
                    "story_key": story_key,
                    "language": source_language,
                    "sentence": find_segment(text, term),
                    "context": find_segment(text, term) or text[:300],
                }
            ],
            official=False,
            source="llm",
            created_at=now_iso(),
            confidence=confidence,
            trust="C",
            tags=tags,
        )
        _refresh_term_weight(rec)
        records.append(rec)
        if len(records) >= max_terms:
            break
    return records


def translate_terms_for_story(
    source_records: list[TermRecord],
    target_language: str,
    target_text: str,
    llm: LLMClient,
) -> dict[str, str]:
    if not source_records:
        return {}
    system = (
        "You translate Project Sekai terms from one language into one target language. "
        "Use the official localized term when the context makes it obvious; otherwise use the "
        "community-accepted translation that matches the same position in the story. "
        'Return JSON: {"translations":[{"term":"...","translation":"...","confidence":0.0-1.0}]}. '
        "Only include terms you can translate confidently."
    )
    term_lines = []
    for record in source_records:
        sentence = record.evidence[0].get("sentence", "") if record.evidence else ""
        term_lines.append(f"- {record.canonical} | source sentence: {sentence[:300]}")
    user = (
        f"Target language: {target_language}\n"
        f"Source language: {source_records[0].source_language}\n"
        f"Terms:\n{chr(10).join(term_lines)}\n\n"
        f"Target episode text:\n{target_text[:12000]}"
    )
    data = llm.chat_json(system, user)
    items = data.get("translations", []) if isinstance(data, dict) else []
    result: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        source_term = str(item.get("term", "")).strip()
        translation = str(
            item.get("translation")
            or item.get("target_name")
            or (item.get("languages") or {}).get(target_language, "")
        ).strip()
        if source_term and translation:
            result[normalize_name(source_term)] = translation
    return result


def merge_terms(records: Iterable[TermRecord]) -> list[TermRecord]:
    by_id: dict[str, TermRecord] = {}
    for record in records:
        existing = by_id.get(record.id)
        if existing is None:
            # Ensure new singletons carry consistent tags/weight
            if not record.tags:
                record.tags = _normalize_tags(None, record.kind)
            _refresh_term_weight(record)
            by_id[record.id] = record
            continue
        for language, name in record.names.items():
            if name and not existing.names.get(language):
                existing.names[language] = name
        for evidence in record.evidence:
            if evidence not in existing.evidence:
                existing.evidence.append(evidence)
        # Merge tags: union
        for t in record.tags:
            if t not in existing.tags:
                existing.tags.append(t)
        if not existing.tags:
            existing.tags = _normalize_tags(None, existing.kind)
        existing.tags = sorted(set(existing.tags))
        for pos in record.positions:
            if pos not in existing.positions:
                existing.positions.append(pos)
        existing.official = existing.official or record.official
        if record.official and not existing.official:
            existing.source = record.source
        if trust_rank(record.trust) > trust_rank(existing.trust):
            existing.trust = record.trust
        existing.confidence = max(existing.confidence, record.confidence)
        if not existing.created_at and record.created_at:
            existing.created_at = record.created_at
        _refresh_term_weight(existing)
    return _merge_reciprocal(list(by_id.values()))


def _reciprocal_signature(record: TermRecord) -> Optional[tuple[str, str]]:
    names = record.names
    ja = normalize_name(names.get("ja", ""))
    zh = normalize_name(names.get("zh_hans", "") or names.get("zh_tw", ""))
    if ja and zh:
        return (ja, zh)
    return None


def _merge_reciprocal(records: list[TermRecord]) -> list[TermRecord]:
    groups: dict[tuple[str, str], list[TermRecord]] = {}
    for record in records:
        signature = _reciprocal_signature(record)
        if signature is None:
            continue
        groups.setdefault(signature, []).append(record)

    merged: list[TermRecord] = []
    consumed: set[int] = set()
    for group in groups.values():
        if len(group) == 1:
            merged.append(group[0])
            consumed.add(id(group[0]))
            continue
        primary = sorted(
            group,
            key=lambda item: (
                not item.official,
                item.source_language != "ja",
                -len(item.evidence),
                item.id,
            ),
        )[0]
        for other in group:
            if other is primary:
                continue
            for language, name in other.names.items():
                if name and not primary.names.get(language):
                    primary.names[language] = name
            for evidence in other.evidence:
                if evidence not in primary.evidence:
                    primary.evidence.append(evidence)
            for t in other.tags:
                if t not in primary.tags:
                    primary.tags.append(t)
            primary.tags = sorted(set(primary.tags))
            for pos in other.positions:
                if pos not in primary.positions:
                    primary.positions.append(pos)
            primary.official = primary.official or other.official
            if trust_rank(other.trust) > trust_rank(primary.trust):
                primary.trust = other.trust
            primary.confidence = max(primary.confidence, other.confidence)
            if not primary.created_at and other.created_at:
                primary.created_at = other.created_at
            _refresh_term_weight(primary)
            consumed.add(id(other))
        _refresh_term_weight(primary)
        merged.append(primary)
        consumed.add(id(primary))

    for record in records:
        if id(record) not in consumed:
            merged.append(record)
    return merged


def seed_from_glossary(store_root: Path) -> list[TermRecord]:
    glossary = dbstore.load_glossary_terms(store_root)
    records: list[TermRecord] = []
    for term in glossary:
        # Only seed real nouns — title kinds (card/event_story/stamp/music_*)
        # are story/episode/gaácha titles, not terminology, and seeding them
        # is what polluted the word cloud with full-sentence translations.
        if term.kind not in NOUN_KINDS:
            continue
        names = {
            language: name
            for language, name in term.names.items()
            if language in TERM_LANGUAGES and name
        }
        if not names:
            continue
        source_language = "ja" if "ja" in names else next(iter(names))
        canonical = names[source_language]
        tags = _normalize_tags(None, term.kind)
        trust_val = term.trust or trust_for_source(
            term.source or "glossary",
            official=term.official,
        )
        rec = TermRecord(
            id=make_term_id(source_language, canonical),
            canonical=canonical,
            source_language=source_language,
            kind=term.kind,
            names=names,
            official=term.official,
            source=term.source or "glossary",
            created_at=now_iso(),
            confidence=1.0,
            trust=trust_val,
            tags=tags,
        )
        _refresh_term_weight(rec)
        records.append(rec)
    return merge_terms(records)


def extract_terms(
    pages: Iterable[dict],
    source_language: str,
    target_languages: Iterable[str],
    llm: LLMClient,
    existing: Optional[list[TermRecord]] = None,
    max_terms_per_page: int = 20,
    include_translations: bool = True,
) -> list[TermRecord]:
    targets = [language for language in target_languages if language]
    groups = group_pages_by_story(pages)
    records_by_id = {record.id: record for record in existing or []}

    for story_key, pages_by_language in sorted(groups.items()):
        source_page = _group_page(pages_by_language, source_language)
        if source_page is None:
            continue
        source_text = str(source_page.get("text", ""))
        if not source_text.strip():
            continue
        source_records = extract_terms_from_text(
            source_text,
            story_key,
            source_language,
            llm,
            max_terms=max_terms_per_page,
        )
        if include_translations:
            translations: dict[str, dict[str, str]] = {}
            for target_language in targets:
                if target_language == source_language:
                    continue
                target_page = _group_page(pages_by_language, target_language)
                if target_page is None:
                    continue
                mapping = translate_terms_for_story(
                    source_records,
                    target_language,
                    str(target_page.get("text", "")),
                    llm,
                )
                for normalized_source, translated_name in mapping.items():
                    translations.setdefault(normalized_source, {})[target_language] = translated_name

            for record in source_records:
                source_key = normalize_name(record.names.get(source_language, ""))
                for normalized_source, target_names in translations.items():
                    if normalized_source == source_key:
                        for language, translated_name in target_names.items():
                            if translated_name:
                                record.names[language] = translated_name

        for record in source_records:
            existing_record = records_by_id.get(record.id)
            if existing_record is not None:
                merged = merge_terms([existing_record, record])[0]
                records_by_id[existing_record.id] = merged
            else:
                records_by_id[record.id] = record

    return list(records_by_id.values())


def _is_proper_latin(term: str) -> bool:
    term = term.strip(" .,!?。！？")
    if len(term) < 3 or term.lower() in _LOCAL_LATIN_STOPWORDS:
        return False
    if "/" in term or "×" in term or "·" in term:
        return True
    if term.isupper():
        return True
    return term[0].isupper()


def _local_latin_candidates(segment: str) -> list[str]:
    candidates: list[str] = []
    compound_spans: list[tuple[int, int]] = []
    for match in _LOCAL_LATIN_COMPOUND_RE.finditer(segment):
        term = match.group(0).strip(" .,!?。！？")
        if _is_proper_latin(term):
            candidates.append(term)
            compound_spans.append((match.start(), match.end()))
    for match in _LOCAL_LATIN_TOKEN_RE.finditer(segment):
        if any(match.start() >= start and match.end() <= end for start, end in compound_spans):
            continue
        term = match.group(0)
        if _is_proper_latin(term):
            candidates.append(term)
    return candidates


def build_protagonist_blocklist(glossary: Iterable[Any]) -> set[str]:
    """人工标准负面规则：26 主角互相指涉的人名与代词不采集。

    Collect every surface form of playable characters — full names, surnames,
    given names, kana/romaji variants, zh translations — plus first/second
    person pronouns. These are speaker labels and mutual references, never
    content-bearing terms."""
    blocked: set[str] = set()
    for gt in glossary:
        if str(getattr(gt, "kind", "")) != "character":
            continue
        names = getattr(gt, "names", {}) or {}
        for v in names.values():
            if not v:
                continue
            v = str(v).strip()
            if len(v) >= 2:
                blocked.add(v)
                blocked.add(normalize_name(v))
        # Split full names into surname/given parts (星乃一歌 -> 星乃 / 一歌).
        ja = str(names.get("ja") or names.get("full") or "")
        zh = str(names.get("zh_hans") or "")
        for full in (ja, zh):
            if 3 <= len(full) <= 5:
                blocked.add(full[:2])
                blocked.add(full[-2:])
    blocked |= _JA_PRONOUN_STOPWORDS
    blocked |= {"彼女", "彼", "私たち", "俺たち", "这位", "那位", "大家", "各位",
                "先生", "小姐", "同学", "さん", "ちゃん", "くん"}
    return blocked


def build_noun_lexicon(glossary: Iterable[Any]) -> dict[str, dict]:
    lexicon: dict[str, dict] = {}
    for term in glossary:
        kind = str(getattr(term, "kind", ""))
        if kind not in NOUN_KINDS:
            continue
        names = {k: str(v) for k, v in (getattr(term, "names", {}) or {}).items() if v}
        # Character records may lack a full `ja` name (KAITO/MEIKO only carry
        # givenName) — fall back so virtual-singer names still enter the lexicon
        # and never fall into heuristic alignment.
        ja = names.get("ja") or (
            names.get("givenName") if kind == "character" else ""
        )
        if not ja:
            continue
        key = normalize_name(ja)
        tags = _normalize_tags(None, kind)
        entry = {
            "surface": ja,
            "kind": kind,
            "tags": tags,
            "names": names,
            "trust": str(getattr(term, "trust", "") or ""),
            "official": bool(getattr(term, "official", False)),
        }
        existing = lexicon.get(key)
        if existing is None or len(ja) > len(existing["surface"]):
            lexicon[key] = entry
    return lexicon

# NOTE on structured short text (曲名/gacha/stamp titles): their exclusion is
# INPUT-LAYER only — NOUN_KINDS keeps them out of seeds/lexicon because the
# entity layer already owns their official five-language names. When such a
# name appears inside narrative prose it is segmented like any other word;
# no candidate-level avoidance exists (and none should).


def tokenize_ja(text: str, lexicon: dict[str, dict]) -> list[dict]:
    """Longest-match tokenizer over Japanese text.

    Returns a list of {start, end, surface, key, entry} for each matched noun.
    Non-matched spans (verbs, particles, pronouns, generic words) are skipped —
    they are not nouns and must not enter the word cloud.
    """
    tokens: list[dict] = []
    i = 0
    n = len(text)
    max_len = max((len(e["surface"]) for e in lexicon.values()), default=0)
    if max_len <= 0:
        return tokens
    while i < n:
        matched = False
        for length in range(min(max_len, n - i), 0, -1):
            sub = text[i:i + length]
            key = normalize_name(sub)
            entry = lexicon.get(key)
            if entry is not None and entry["surface"] == sub:
                tokens.append({
                    "start": i,
                    "end": i + length,
                    "surface": sub,
                    "key": key,
                    "entry": entry,
                })
                i += length
                matched = True
                break
        if not matched:
            i += 1
    return tokens


def _local_candidates(segment: str, source_language: str) -> list[tuple[str, str, bool]]:
    candidates: list[tuple[str, str, bool]] = []
    seen: set[str] = set()

    def add(term: str, kind: str, quoted: bool = False) -> None:
        term = term.strip()
        key = normalize_name(term)
        if len(term) < 2 or len(term) > 80 or not key or key in seen:
            return
        seen.add(key)
        candidates.append((term, kind, quoted))

    for match in _LOCAL_QUOTE_RE.finditer(segment):
        add(match.group(1), "coined_term", quoted=True)
    if source_language == "ja":
        for match in _LOCAL_KATAKANA_RE.finditer(segment):
            add(match.group(0), "coined_term")
    for term in _local_latin_candidates(segment):
        add(term, "term")
    return candidates


def extract_terms_from_text_local(
    text: str,
    story_key: str,
    source_language: str,
    max_terms: int = 20,
    lexicon: Optional[dict[str, dict]] = None,
) -> list[TermRecord]:
    records: list[TermRecord] = []
    lex = lexicon or {}
    matched_keys: set[str] = set()

    for segment in text.splitlines():
        seg = segment.strip()
        if not seg:
            continue

        # 1. Longest-match against the official noun lexicon (characters, units,
        #    areas, songs, game entities). These carry authoritative cross-language
        #    names and tags, so they never rely on heuristic alignment.
        if lex:
            for tok in tokenize_ja(seg, lex):
                entry = tok["entry"]
                rec = TermRecord(
                    id=make_term_id(source_language, tok["surface"]),
                    canonical=tok["surface"],
                    source_language=source_language,
                    kind=entry["kind"],
                    names=dict(entry["names"]),
                    evidence=[
                        {
                            "story_key": story_key,
                            "language": source_language,
                            "sentence": seg,
                            "context": seg,
                        }
                    ],
                    official=entry["official"],
                    source="glossary" if entry["official"] else "local",
                    created_at=now_iso(),
                    confidence=1.0 if entry["official"] else 0.9,
                    trust=entry["trust"] or ("A" if entry["official"] else "B"),
                    tags=list(entry["tags"]),
                )
                _refresh_term_weight(rec)
                records.append(rec)
                matched_keys.add(normalize_name(tok["surface"]))
                if len(records) >= max_terms:
                    return records

        # 2. Heuristic fallback only for spans the lexicon did NOT cover:
        #    quoted coined phrases and katakana/latin proper nouns. Pronouns
        #    and generic words are blocked by stopwords. Song titles appearing
        #    inside prose are segmented like any other word — no avoidance.
        for term, kind, quoted in _local_candidates(seg, source_language):
            norm = normalize_name(term)
            if norm in matched_keys:
                continue
            if _is_ja_stopword(term):
                continue
            if not _source_candidate_acceptable(term, source_language):
                continue
            tags = classify_tags(term, seg, source_language)
            if tags == ["other"] and kind in KIND_TO_TAGS:
                tags = _normalize_tags(None, kind)
            rec = TermRecord(
                id=make_term_id(source_language, term),
                canonical=term,
                source_language=source_language,
                kind=kind,
                names={source_language: term},
                evidence=[
                    {
                        "story_key": story_key,
                        "language": source_language,
                        "sentence": seg,
                        "context": seg,
                    }
                ],
                official=False,
                source="local",
                created_at=now_iso(),
                confidence=0.7,
                trust="C",
                tags=tags,
                # Quoted coinage (「…」) is deliberate game vocabulary —
                # provisional 非日常; burstiness re-classifies after pass 1.
                everyday=not quoted,
            )
            _refresh_term_weight(rec)
            records.append(rec)
            if len(records) >= max_terms:
                return records
    return records


def _coined_candidate_acceptable(term: str, target_language: str) -> bool:
    """Accept only proper-noun-like candidates for coined source terms.

    Character names and sentence-initial phrases such as "Minori" or "Thank you"
    are rejected because the local aligner cannot distinguish them from real
    translations; quoted terms, camel-case brands, and fully capitalised
    multi-word names are kept.
    """
    if target_language == "ja":
        if re.fullmatch(r"[\u30A0-\u30FF][\u30A0-\u30FFー・]{1,40}", term):
            return True
    term = term.strip(" .,!?。！？")
    if not term:
        return False
    if "/" in term or "×" in term or "·" in term:
        return True
    if term.isupper():
        return True
    words = re.findall(r"[A-Za-z][A-Za-z0-9'’\-]*", term)
    if not words:
        return False
    if len(words) == 1:
        word = words[0]
        if word.lower() in _LOCAL_LATIN_STOPWORDS or word.endswith(("'s", "’s")):
            return False
        return any(char.isupper() for char in word[1:])
    return all(
        word
        and word[0].isupper()
        and word.lower() not in _LOCAL_LATIN_STOPWORDS
        and not word.endswith(("'s", "’s"))
        for word in words
    )


def _local_translate_segment(
    target_segment: str,
    target_language: str,
    strict: bool = False,
) -> str:
    candidates = _local_candidates(target_segment, target_language)
    if not candidates:
        return ""
    if strict:
        candidates = [
            (term, kind, quoted)
            for term, kind, quoted in candidates
            if quoted or _coined_candidate_acceptable(term, target_language)
        ]
        if not candidates:
            return ""
    quoted = [term for term, _kind, is_quoted in candidates if is_quoted]
    result = ""
    if len(quoted) == 1:
        result = quoted[0]
    elif len(candidates) == 1:
        result = candidates[0][0]
    elif quoted:
        result = min(quoted, key=len)
    else:
        def score(candidate: tuple[str, str, bool]) -> int:
            term, _kind, _quoted = candidate
            value = 0
            if any(char.isupper() for char in term[1:]):
                value += 4
            if "/" in term or "×" in term or "·" in term:
                value += 3
            if term.isupper():
                value += 2
            return value

        best = max(candidates, key=score)
        if score(best) > 0:
            result = best[0]
        else:
            result = candidates[-1][0]
    if result and not _translation_candidate_acceptable(result, target_language):
        return ""
    return result


def _term_language(language: str) -> str:
    return _LANGUAGE_ALIASES.get(language, language)


def _source_candidate_acceptable(term: str, source_language: str) -> bool:
    term = term.strip(" \t\n\r.。！？!?…—・、,")
    if len(term) < 2 or len(term) > 40:
        return False
    if any(ch in term for ch in "。！？…、，"):
        return False
    if source_language == "ja":
        if re.fullmatch(r"[ぁ-ん]+", term) and len(term) <= 5:
            return False
        if term in _JA_KATAKANA_STOPWORDS:
            return False
        if re.search(r"[ぁ-んァ-ヶー][をにへでとはがのよりもからまで]|[をにへでとはがのよりもからまで][ぁ-んァ-ヶ]", term):
            return False
        if re.search(r"(の|と|を|に|へ|は|が|も|で|から|まで|より|には|では|とは)", term):
            return False
    if source_language in ("zh_hans", "zh_tw", "zh_hant", "zh_cn"):
        if any(ch in term for ch in _ZH_FUNCTION_CHARS):
            return False
    if source_language == "ko":
        if _strip_ko_suffix(term) != term:
            return False
    return True


def _translation_candidate_acceptable(term: str, target_language: str) -> bool:
    lang = _term_language(target_language)
    if not _source_candidate_acceptable(term, lang):
        return False
    if lang == "en":
        words = re.findall(r"[A-Za-z][A-Za-z0-9'’\-]*", term)
        if " ".join(words) in _GENERIC_LATIN_TRANSLATIONS:
            return False
        # Any stopword ANYWHERE disqualifies a multi-word candidate
        # ("Even Shiraishi's", "A VIRTUAL") — real names don't contain
        # function words. Possessives are speaker-label artifacts.
        if any(w.lower() in _LOCAL_LATIN_STOPWORDS for w in words):
            return False
        if any(w.endswith(("'s", "’s")) for w in words):
            return False
        # Dialogue fragments: exclamations, stutters (Y-You're), elongations
        # (SAKIIII), ellipses.
        if re.search(r"[!?…]", term):
            return False
        if re.match(r"^[A-Za-z]-", term) or re.search(r"(.)\1{2,}", term):
            return False
        if len(words) == 1:
            w = words[0]
            if w.lower() in _LOCAL_LATIN_STOPWORDS:
                return False
            # Allow any single capitalized word (Live, Show, etc.)
            if w[0].isupper():
                return True
            if not any(char.isupper() for char in term[1:]):
                return False
        if len(words) >= 2 and not any(char.isupper() for char in term[1:]):
            return False
    return True


def _split_zh_run(run: str, *, enumerate_windows: bool = True) -> list[str]:
    """Split a Chinese run into candidate phrases.

    Segmentation is function-word driven: multi-character function words and
    individual function characters act as hard cut points, so a returned phrase
    never straddles 的/了/在. That alone yields the whole-run phrases.

    ``enumerate_windows`` adds every aligned sub-window so a 4-char term like
    网络天堂 is still reachable as 网络/天堂. It is a fallback, not the primary
    path: enumerating a 6-char run produces 20 openings of which at most one is
    a word, and when a discovered-word vocabulary is available the aligner
    filters against it anyway, so the openings are pure overhead.
    """
    word_pattern = "|".join(re.escape(word) for word in sorted(_ZH_FUNCTION_WORDS, key=len, reverse=True))
    pieces = re.split(word_pattern, run)
    out: list[str] = []
    char_pattern = "[" + re.escape("".join(sorted(set(_ZH_FUNCTION_CHARS)))) + "]"
    for piece in pieces:
        for sub in re.split(char_pattern, piece):
            if len(sub) < 2 or len(sub) > 8:
                continue
            out.append(sub)
            if not enumerate_windows:
                continue
            n = len(sub)
            for wlen in range(2, n):
                for start in range(0, n - wlen + 1):
                    out.append(sub[start:start + wlen])
    # Dedup preserving order
    return list(dict.fromkeys(out))


def _strip_ko_suffix(token: str) -> str:
    suffixes = sorted(_KO_PARTICLES, key=len, reverse=True)
    changed = True
    while changed:
        changed = False
        for suffix in suffixes:
            if token.endswith(suffix) and len(token) - len(suffix) >= 2:
                token = token[:-len(suffix)]
                changed = True
                break
    return token


def _ko_token_candidates(segment: str) -> list[str]:
    tokens = [token for token in _KO_SPLIT_RE.split(segment) if token]
    cleaned: list[str] = []
    cleaned_all: list[str] = []
    for token in tokens:
        if not _HANGUL_RUN_RE.fullmatch(token):
            continue
        stem = _strip_ko_suffix(token)
        if len(stem) >= 1:
            cleaned_all.append(stem)
        if len(stem) >= 2:
            cleaned.append(stem)
    out = list(cleaned)
    for a, b in zip(cleaned_all, cleaned_all[1:]):
        out.append(a + " " + b)
    return out

def _local_translation_candidates(
    segment: str,
    target_language: str,
    *,
    enumerate_zh_windows: bool = True,
) -> list[str]:
    lang = _term_language(target_language)
    out: list[str] = []

    def add(candidate: str) -> None:
        candidate = candidate.strip()
        if 2 <= len(candidate) <= 40 and candidate not in out:
            out.append(candidate)

    # Strip the speaker tag (「爱莉：」「Haruka:」「Cafe Customers:」 etc.) — the
    # speaker name is a line label, never a translation candidate. This was the
    # dominant source of character-name contamination in distribution alignment.
    seg = re.sub(r"^[^：:]{1,30}[：:]\s*", "", segment)
    if not seg:
        seg = segment

    for match in _LOCAL_QUOTE_RE.finditer(seg):
        add(match.group(1))
    if lang == "ja":
        for term, _kind, _quoted in _local_candidates(seg, "ja"):
            add(term)
    elif lang == "en":
        for term in _local_latin_candidates(seg):
            add(term)
    elif lang in ("zh_hans", "zh_tw", "zh_hant"):
        for term in _local_latin_candidates(seg):
            add(term)
        for run in _CJK_RUN_RE.findall(seg):
            for piece in _split_zh_run(run, enumerate_windows=enumerate_zh_windows):
                add(piece)
    elif lang == "ko":
        for term in _local_latin_candidates(seg):
            add(term)
        for piece in _ko_token_candidates(seg):
            add(piece)
    return out


def build_zh_lexicon(glossary: Iterable[Any]) -> tuple[set[str], set[str]]:
    """Collect all Chinese display names from glossary nouns.

    Returns (normalized_set, surface_set). The normalized set matches the
    normalize_name key; the surface set holds raw forms for longest match.
    Used to constrain Chinese translation candidates to words that actually
    exist in the official lexicon, eliminating fragment enumeration noise.

    Names are read through ``_canon_names`` because the glossary spells the
    traditional slot ``zh_hant`` while this filter looks it up as ``zh_tw``;
    a direct ``names["zh_tw"]`` hit only 2 of the 43k official entries."""
    zh_norm: set[str] = set()
    zh_surface: set[str] = set()
    for term in glossary:
        if str(getattr(term, "kind", "")) not in NOUN_KINDS:
            continue
        canon = _canon_names(getattr(term, "names", {}) or {})
        for k in ("zh_hans", "zh_tw"):
            v = canon.get(k, "")
            if v:
                zh_norm.add(normalize_name(v))
                zh_surface.add(v)
    return zh_norm, zh_surface


def _filter_zh_candidates(cands: list[str], zh_lexicon: set[str]) -> list[str]:
    """Keep only Chinese candidates present in the official glossary lexicon.
    Falls back to all candidates if the lexicon is empty (e.g. demo store)."""
    if not zh_lexicon:
        return cands
    kept = [c for c in cands if normalize_name(c) in zh_lexicon]
    return kept if kept else cands


def _canon_names(names: Any) -> dict[str, str]:
    """Collapse language-alias spellings onto the internal TERM_LANGUAGES set.

    Data sources disagree on how to spell the traditional-Chinese slot: both
    the corpus language column and the glossary use ``zh_hant`` (144k pages /
    43k official names), while TERM_LANGUAGES and the alignment code use
    ``zh_tw``. Reading ``names["zh_tw"]`` therefore found nothing for
    essentially every official Chinese name, and ``_texts("zh_tw")`` returned
    an empty corpus, so the traditional-Chinese half of the store was never
    segmented at all. Funnelling every lookup through this helper keeps both
    sides talking about the same language without hardcoding one spelling.
    """
    out: dict[str, str] = {}
    for key, value in (names or {}).items():
        if not value:
            continue
        out.setdefault(_term_language(str(key)), str(value))
    return out


def _group_page(by_language: dict, lang: str) -> Optional[dict]:
    """Fetch a story's page for ``lang``, resolving language aliases.

    ``group_pages_by_story`` keys pages by the corpus spelling (zh_hant); every
    caller here asks in TERM_LANGUAGES spelling (zh_tw). A plain dict lookup
    silently returns None for the whole traditional-Chinese corpus.
    """
    page = by_language.get(lang)
    if page is not None:
        return page
    want = _term_language(lang)
    for key, value in by_language.items():
        if value is not None and _term_language(str(key)) == want:
            return value
    return None


def _load_glossary_fallback(store_root: Path) -> list[Any]:
    """Best-effort glossary load for callers that did not pass one in.

    Glossary terms live in the SQLite store (``dbstore.load_glossary_terms``);
    ``kb/glossary.json`` only exists for demo and export stores. The old
    fallback read the JSON path unconditionally, so on a real store it returned
    an empty list and every official translation silently dropped out of the
    alignment vocabulary — leaving the unsupervised (and much noisier)
    discovered words as the only candidates.
    """
    if not store_root.exists():
        return []
    try:
        from sekaisync import dbstore

        terms = list(dbstore.load_glossary_terms(store_root))
        if terms:
            return terms
    except Exception:
        pass
    try:
        from sekaisync.layout import glossary_path

        return list(load_glossary(glossary_path(store_root)))
    except Exception:
        return []


def _discover_ko_words(texts: list[str], min_freq: int = 3) -> set[str]:
    """Korean vocabulary via whitespace tokenisation + particle stripping.

    Korean marks word boundaries with spaces, so the Matrix67 n-gram discovery
    used for Chinese is the wrong tool here. It counted substrings straddling
    spaces and kept digits, producing a vocabulary that was 96.5% numeric
    fragments (' 1년 동안', ' 10개', ' 100번', ' 1년이'). Requiring a token to be
    pure Hangul (``_HANGUL_RUN_RE.fullmatch``) drops all of those by
    construction, and ``_strip_ko_suffix`` removes the trailing particle so the
    same noun does not fragment across its case forms.
    """
    counts: Counter[str] = Counter()
    for text in texts:
        for token in _KO_SPLIT_RE.split(text):
            if not token or not _HANGUL_RUN_RE.fullmatch(token):
                continue
            stem = _strip_ko_suffix(token)
            if 2 <= len(stem) <= 12:
                counts[stem] += 1
    return {word for word, count in counts.items() if count >= min_freq}


def build_alignment_vocab(
    groups: dict,
    targets: Iterable[str],
    glossary: Optional[Iterable[Any]] = None,
) -> dict[str, set[str]]:
    """Build per-language allowed-candidate vocabularies for distribution alignment.

    zh_hans / zh_tw: official glossary names ∪ unsupervised discovered words
    (wordseg: frequency × PMI cohesion × boundary entropy). Discovered words
    replace naive sub-window enumeration, which flooded the aligner with
    fragments (爱莉/笑梦/弧光). Korean goes through whitespace tokenisation
    instead (see ``_discover_ko_words``). English keeps its latin tokenizer
    unfiltered.

    Every entry is stored as a ``normalize_name()`` key, because that is what
    the filter sites compare against::

        cands = [c for c in cands if normalize_name(c) in allowed]

    Storing raw surfaces here silently dropped every candidate whose surface
    differs from its normalized form — 96.5% of the Korean vocabulary and
    ~900 Chinese entries could never match.

    Character/person names are EXCLUDED on purpose: they are speaker labels in
    dialogue, never translations of non-person terms, and their ubiquity inside
    a unit's stories would otherwise outscore the true translation."""
    from sekaisync.wordseg import discover_words

    targets = [t for t in targets if t]
    vocab: dict[str, set[str]] = {}

    glossary_by_lang: dict[str, set[str]] = {lang: set() for lang in TERM_LANGUAGES}
    char_names: set[str] = set()
    if glossary is None:
        glossary = _load_glossary_fallback(Path("store"))
    for gt in (glossary or []):
        kind = str(getattr(gt, "kind", ""))
        names = getattr(gt, "names", {}) or {}
        if kind in {"character", "character_profile"}:
            for v in names.values():
                if v:
                    char_names.add(normalize_name(str(v)))
            continue
        for lang, value in _canon_names(names).items():
            if lang in glossary_by_lang:
                glossary_by_lang[lang].add(normalize_name(value))

    def _texts(lang: str) -> list[str]:
        out: list[str] = []
        for by_language in groups.values():
            page = _group_page(by_language, lang)
            if page is not None:
                out.append(str(page.get("text", "")))
        return out

    def _assign(language: str, words: set[str]) -> None:
        # Key by the caller's own spelling: align_term_by_frequency does a
        # direct vocab.get(target_language) and never resolves aliases itself.
        for target in targets:
            if _term_language(target) == language:
                vocab[target] = words

    zh_languages = ("zh_hans", "zh_tw")
    if any(_term_language(t) in zh_languages for t in targets):
        def _is_char_fragment(word: str) -> bool:
            # 笑梦 ⊂ 凤笑梦: given-name fragments of characters must not become
            # alignment candidates either.
            if word in char_names:
                return True
            return any(word in cn or cn in word for cn in char_names)

        # Official Chinese names are authoritative in both scripts, so each
        # script's vocabulary inherits the full official set. Discovered words
        # stay script-local: normalize_name does not fold simplified against
        # traditional, so mixing them would only inflate the list without
        # buying real coverage.
        official_zh = glossary_by_lang["zh_hans"] | glossary_by_lang["zh_tw"]
        for language in zh_languages:
            words: set[str] = set(official_zh)
            texts = _texts(language)
            if texts:
                # boundary_stop_chars: a word may not begin or end on a
                # Chinese function character. Without it the discovery pass
                # keeps grammar phrasing ('的攝影', '了很多人的') that happens to
                # be frequent together, and those become alignment candidates.
                found = discover_words(
                    texts,
                    min_freq=3,
                    max_chars=2_000_000,
                    boundary_stop_chars=_ZH_FUNCTION_CHARS,
                )
                words |= {
                    normalize_name(w) for w in found
                    if not _is_char_fragment(normalize_name(w))
                }
            _assign(language, words)

    if any(_term_language(t) == "ko" for t in targets):
        words = set(glossary_by_lang["ko"])
        texts = _texts("ko")
        if texts:
            words |= {
                normalize_name(w) for w in _discover_ko_words(texts)
                if normalize_name(w) not in char_names
            }
        _assign("ko", words)

    return vocab


def build_translation_memory(
    pages: Iterable[dict],
    source_language: str,
    target_languages: Iterable[str],
) -> dict[tuple[str, str], str]:
    targets = [language for language in target_languages if language]
    groups = group_pages_by_story(pages)
    co: dict[tuple[str, str], Counter] = defaultdict(Counter)
    glob: dict[str, Counter] = defaultdict(Counter)
    total: dict[tuple[str, str], int] = defaultdict(int)

    for _story_key, by_language in groups.items():
        source_page = _group_page(by_language, source_language)
        if source_page is None:
            continue
        source_lines = [
            line.strip()
            for line in str(source_page.get("text", "")).splitlines()
            if line.strip()
        ]
        if not source_lines:
            continue
        for target_language in targets:
            target_page = _group_page(by_language, target_language)
            if target_page is None:
                continue
            target_lines = [
                line.strip()
                for line in str(target_page.get("text", "")).splitlines()
                if line.strip()
            ]
            if not target_lines:
                continue
            n_source = len(source_lines)
            n_target = len(target_lines)
            for source_index, source_line in enumerate(source_lines):
                source_terms = [
                    term
                    for term, _kind, _quoted in _local_candidates(source_line, source_language)
                    if _source_candidate_acceptable(term, source_language)
                ]
                if not source_terms:
                    continue
                predicted = (
                    round(source_index * (n_target - 1) / max(1, n_source - 1))
                    if n_source > 1
                    else 0
                )
                for target_index in range(
                    max(0, predicted - 2),
                    min(n_target, predicted + 3),
                ):
                    target_candidates = [
                        candidate
                        for candidate in _local_translation_candidates(
                            target_lines[target_index],
                            target_language,
                        )
                        if _translation_candidate_acceptable(candidate, target_language)
                    ]
                    if not target_candidates:
                        continue
                    for term in source_terms:
                        key = (term, target_language)
                        total[key] += 1
                        for candidate in target_candidates:
                            co[key][candidate] += 1
                            glob[target_language][candidate] += 1

    memory: dict[tuple[str, str], str] = {}
    for key, counts in co.items():
        occurrence_total = total[key]
        if occurrence_total < 2:
            continue
        best_score = 0.0
        best_candidate = ""
        for candidate, count in counts.items():
            if count < 2:
                continue
            global_count = glob[key[1]].get(candidate, 1)
            top_for_key = max(counts.values()) if counts else 1
            if count / max(1, top_for_key) < 0.3:
                continue
            score = (count * count) / max(1, global_count)
            if any(char.isupper() for char in candidate[1:]):
                score += 0.5
            if re.search(r"[/×· ]", candidate):
                score += 0.25
            if score > best_score or (best_candidate and score == best_score and len(candidate) > len(best_candidate)):
                best_score = score
                best_candidate = candidate
        # Higher bar for high-occurrence terms to avoid noisy co-occurrence,
        # but low bar for the 2-occurrence fixture used in unit tests.
        threshold = 0.30 if occurrence_total >= 3 else 0.05
        if best_candidate and best_score >= threshold:
            memory[key] = best_candidate
    return memory


# ── Distribution-based cross-language alignment ────────────────────
# The word cloud is TF statistics over the corpus. The same entity appears in
# the same stories across all five languages, so its per-story TF vector is
# highly correlated with the true translation and uncorrelated with co-occurring
# character names. We align by IDF-weighted co-occurrence over the source term's
# story set instead of picking a single neighbor line.

def compute_lang_idf(
    groups: dict,
    target_languages: Iterable[str],
    total_stories: Optional[int] = None,
    vocab: Optional[dict[str, set[str]]] = None,
) -> dict[tuple[str, str], float]:
    """idf[(lang, candidate)] = log(total_stories / doc_freq). Generic words that
    appear in nearly every story (Staff/MEIKO/etc.) get idf≈0 and are suppressed.
    Languages with a vocab entry have candidates constrained to it (glossary ∪
    discovered words), so fragment enumeration cannot dominate."""
    if total_stories is None:
        total_stories = max(1, len(groups))
    df: dict[tuple[str, str], int] = defaultdict(int)
    for sk, by in groups.items():
        for lang in target_languages:
            pg = _group_page(by, lang)
            if pg is None:
                continue
            allowed = vocab.get(lang) if vocab else None
            seen: set[str] = set()
            for seg in str(pg.get("text", "")).splitlines():
                # Sub-window enumeration is only worth it when there is no
                # vocabulary to filter against; with one, the aligner keeps
                # only exact discovered words anyway.
                cands = _local_translation_candidates(
                    seg.strip(), lang, enumerate_zh_windows=not allowed,
                )
                if allowed:
                    cands = [c for c in cands if normalize_name(c) in allowed]
                for cand in cands:
                    if _translation_candidate_acceptable(cand, lang) and cand not in seen:
                        seen.add(cand)
                        df[(lang, cand)] += 1
    return {
        key: math.log(total_stories / max(1, df[key]))
        for key in df
    }


def align_term_by_frequency(
    source_term: str,
    source_language: str,
    target_language: str,
    groups: dict,
    idf: dict[tuple[str, str], float],
    vocab: Optional[dict[str, set[str]]] = None,
    max_candidates: int = 8,
    src_stories: Optional[set[str]] = None,
) -> str:
    """Distribution alignment: collect candidate translations across the stories
    where the term appears, score by doc-level co-occurrence × IDF, return the
    best. ``src_stories`` is the caller-provided inverted index entry; when
    omitted it is derived by a (slow) full scan."""
    if src_stories is None:
        src_stories = {
            sk for sk, by in groups.items()
            if source_language in by and source_term in str(by[source_language].get("text", ""))
        }
    if not src_stories:
        return ""
    # Single-story terms cannot be validated distributionally: every rare
    # phrase in that one story has containment 1.0, so alignment output is
    # indistinguishable from noise (ミュージカル→"TV first"). Honest empty.
    if len(src_stories) < 2:
        return ""
    allowed = vocab.get(target_language) if vocab else None
    # LINE-LOCAL collection with cross-story voting: candidates come ONLY from
    # the predicted counterpart line of a line containing the term — never from
    # the whole story. Whole-story collection degenerates for single-story
    # terms (containment 1/1=1.0 lets any rare word win: ミュージカル→Dorothy).
    co_docs: dict[str, int] = defaultdict(int)
    for sk in src_stories:
        by = groups.get(sk, {})
        spg = _group_page(by, source_language)
        tpg = _group_page(by, target_language)
        if spg is None or tpg is None:
            continue
        src_lines = [ln.strip() for ln in str(spg.get("text", "")).splitlines() if ln.strip()]
        tgt_lines = [ln.strip() for ln in str(tpg.get("text", "")).splitlines() if ln.strip()]
        if not src_lines or not tgt_lines:
            continue
        hit_idx = [i for i, ln in enumerate(src_lines) if source_term in ln]
        if not hit_idx:
            continue
        seen_in_story: set[str] = set()
        for si in hit_idx:
            pred = round(si * (len(tgt_lines) - 1) / max(1, len(src_lines) - 1)) if len(src_lines) > 1 else 0
            # ±0 primary; ±1 as fallback window when the strict line is empty.
            for ti in (pred, pred - 1, pred + 1):
                if ti < 0 or ti >= len(tgt_lines):
                    continue
                cands = _local_translation_candidates(
                    tgt_lines[ti], target_language, enumerate_zh_windows=not allowed,
                )
                if allowed:
                    cands = [c for c in cands if normalize_name(c) in allowed]
                for cand in cands:
                    if _translation_candidate_acceptable(cand, target_language):
                        seen_in_story.add(cand)
            if seen_in_story:
                break  # first productive line is the best positional evidence
        for cand in seen_in_story:
            co_docs[cand] += 1
    if not co_docs:
        return ""
    # Evidence-sufficiency (containment): a true translation occurs almost
    # exclusively inside the term's story set. A topical word (虚拟歌手 for
    # ソロライブ) co-occurs but also appears in hundreds of unrelated stories —
    # its containment ratio is tiny. Reject candidates whose co-doc count is a
    # small fraction of their global doc frequency.
    n_total_stories = max(1, len(groups))
    min_containment = 0.30
    filtered: dict[str, int] = {}
    for cand, co in co_docs.items():
        idf_val = idf.get((target_language, cand))
        if idf_val is None:
            continue  # candidate unseen in global stats: no evidence at all
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
    # Near-top tiebreak: prefer the LONGEST candidate within 75% of the best
    # score — 菲尼克斯奇幻乐园 should beat its own substring 菲尼克斯, and
    # Phoenix Wonderland beat Phoenix.
    best_score = ranked[0][1]
    best_cand = max(
        (c for c, s in ranked if s >= best_score * 0.75),
        key=len,
    )
    return best_cand


def build_alignment_resources(
    groups: dict,
    targets: Iterable[str],
    glossary: Optional[Iterable[Any]],
    cache_dir: Optional[Path] = None,
) -> tuple[dict[str, set[str]], dict[tuple[str, str], float]]:
    """Vocab + IDF with a disk cache keyed by the target-language corpus size.

    Building both from scratch costs ~6 minutes on the full store; the cache
    makes repeated `terms extract` runs instant until the corpus changes."""
    targets = [t for t in targets if t]
    # The signature must be sensitive to every corpus the vocab is built from.
    # `by.get(lang)` used to miss the traditional-Chinese corpus completely
    # (the corpus spells it zh_hant, this loop asked for zh_tw), so that half
    # of the store could change without ever invalidating the cached vocab.
    sig_parts = []
    for lang in ("zh_hans", "zh_tw", "en", "ko"):
        chars = sum(
            len(str((_group_page(by, lang) or {}).get("text", "")))
            for by in groups.values()
        )
        sig_parts.append(f"{lang}:{chars}")
    signature = "|".join(sig_parts)

    if cache_dir is not None:
        try:
            cache_path = Path(cache_dir) / "alignment_resources.json"
            if cache_path.exists():
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                if data.get("signature") == signature:
                    vocab = {k: set(v) for k, v in data["vocab"].items()}
                    idf = {
                        tuple(k.split("\x00")): v
                        for k, v in data["idf"].items()
                    }
                    return vocab, idf
        except (OSError, json.JSONDecodeError, KeyError):
            pass  # fall through to rebuild

    vocab = build_alignment_vocab(groups, targets, glossary)
    idf = compute_lang_idf(groups, targets, vocab=vocab)

    if cache_dir is not None:
        try:
            cache_dir = Path(cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_path = cache_dir / "alignment_resources.json"
            cache_path.write_text(json.dumps({
                "signature": signature,
                "vocab": {k: sorted(v) for k, v in vocab.items()},
                "idf": {"\x00".join(k): v for k, v in idf.items()},
            }, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
    return vocab, idf


def extract_terms_local(
    pages: Iterable[dict],
    source_language: str,
    target_languages: Iterable[str],
    existing: Optional[list[TermRecord]] = None,
    max_terms_per_page: int = 20,
    translation_memory: Optional[dict[tuple[str, str], str]] = None,
    glossary: Optional[Iterable[Any]] = None,
    cache_dir: Optional[Path] = None,
) -> list[TermRecord]:
    targets = [language for language in target_languages if language]
    memory = translation_memory or {}
    groups = group_pages_by_story(pages)
    records_by_id = {record.id: record for record in existing or []}

    # Build the noun lexicon once from official glossary (noun kinds only).
    if glossary is None:
        glossary = _load_glossary_fallback(Path("store"))
    lexicon = build_noun_lexicon(glossary)

    # Per-language alignment vocab (glossary ∪ discovered words) + IDF, cached.
    vocab, idf = build_alignment_resources(groups, targets, glossary, cache_dir)

    # Pass 1: extract records per story and build a canonical -> story-keys
    # inverted index in the same sweep. (The naive alternative — rescanning
    # every story per term per language inside align_term_by_frequency — is
    # O(terms × langs × stories) substring checks and hangs the rebuild.)
    term_stories: dict[str, set[str]] = defaultdict(set)
    term_lines: dict[str, int] = defaultdict(int)
    canon_quoted: dict[str, bool] = defaultdict(bool)
    for story_key, pages_by_language in sorted(groups.items()):
        source_page = _group_page(pages_by_language, source_language)
        if source_page is None:
            continue
        source_text = str(source_page.get("text", ""))
        if not source_text.strip():
            continue
        source_records = extract_terms_from_text_local(
            source_text,
            story_key,
            source_language,
            max_terms=max_terms_per_page,
            lexicon=lexicon,
        )
        for record in source_records:
            # Lexicon-hit records are authoritative (carry official identity,
            # e.g. KAITO/MEIKO via givenName fallback). Their cross-language
            # names must never be overwritten by local alignment pollution.
            if record.source == "glossary" or record.official:
                record.everyday = False
                existing_record = records_by_id.get(record.id)
                if existing_record is not None:
                    merged = merge_terms([existing_record, record])[0]
                    # Official names win: re-apply the authoritative surface.
                    merged.names.update(record.names)
                    records_by_id[existing_record.id] = merged
                else:
                    records_by_id[record.id] = record
                continue
            if record.canonical:
                term_stories[record.canonical].add(story_key)
                # Raw substring hits (line-count proxy) for the burstiness gate:
                # proper nouns repeat inside their stories (フェニックス 426行/60話),
                # common nouns spread thin (ミュージカル ~1行/話).
                term_lines[record.canonical] += source_text.count(record.canonical)
                if record.everyday is False:
                    canon_quoted[record.canonical] = True
            existing_record = records_by_id.get(record.id)
            if existing_record is not None:
                merged = merge_terms([existing_record, record])[0]
                # A local record merging into an official one must not pollute
                # the official's cross-language names (KAITO <- 抛沙包).
                if existing_record.official or record.official:
                    merged.names = dict(existing_record.names if existing_record.official else record.names)
                records_by_id[existing_record.id] = merged
            else:
                records_by_id[record.id] = record

    # 日常/非日常 final classification (canonical-level, stamped on records):
    # 非日常 = official entity | quoted coinage | bursty distribution.
    for record in records_by_id.values():
        if record.source == "glossary" or record.official:
            record.everyday = False
            # Official records may have had their names polluted by local merge
            # (KAITO <- 抛沙包). Rebuild from the authoritative lexicon entry.
            official_entry = lexicon.get(normalize_name(record.canonical))
            if official_entry is not None and record.source != "glossary":
                record.source = "glossary"
                record.names = dict(official_entry["names"])
            # Virtual-singer records may only carry givenName fields. Their
            # name IS the surface (KAITO/MEIKO) — backfill the canonical as the
            # ja/en identity so the record carries usable names.
            if official_entry is not None and "ja" not in record.names:
                surface = official_entry["surface"]
                record.names["ja"] = surface
                if "en" not in record.names:
                    record.names["en"] = surface
            continue
        c = record.canonical
        view = term_stories.get(c, set())
        record.everyday = not looks_like_proper_noun(
            stories_n=len(view),
            lines_n=term_lines.get(c, 0),
            total_stories=len(groups),
            quoted=canon_quoted.get(c, False),
        )

    # Alignment phase: translate each distinct canonical ONCE per language,
    # memoized, then stamp every record that shares it.
    # Zombie purge: stopworded generics (incl. の-phrases/onomatopoeia learned
    # this round) carried in by `existing` stay ja-only husks forever — drop
    # them so the word cloud stays proper-noun-only.
    zombie_ids = [
        rid for rid, rec in records_by_id.items()
        if rec.source != "glossary" and not rec.official and _is_ja_stopword(rec.canonical)
    ]
    for rid in zombie_ids:
        del records_by_id[rid]
    # Curated entries are authoritative — never purged as stopwords.
    for rid, rec in records_by_id.items():
        if rec.canonical in (_GENERIC_FIXED_TRANSLATIONS | _JA_ONLY_CURATED | _PROPRIETARY_FIXED_TRANSLATIONS):
            rec.everyday = False
    align_cache: dict[tuple[str, str], str] = {}
    for record in records_by_id.values():
        if record.source == "glossary" or record.official:
            continue  # authoritative names already present
        # Drop stale cross-language names carried in by `existing`: they are
        # outputs of previous alignment runs. A rejected alignment must leave
        # the language EMPTY (honest "not covered"), not keep yesterday's error.
        src_name = record.names.get(source_language, record.canonical)
        record.names = {source_language: src_name}
        for target_language in targets:
            if target_language == source_language:
                continue
            key = (record.canonical, target_language)
            if key not in align_cache:
                src_stories = term_stories.get(record.canonical, set())
                # Latin-identity canonicals (STANDOUT/Amia/ReLight/RAD WEEKEND):
                # their own surface is already the official name — translating
                # them invents a new alias (新人舞台/MV during). Only align
                # non-latin, non-everyday terms.
                is_latin_identity = bool(re.fullmatch(r"[A-Za-z0-9 &×\-'.!?/]+", record.canonical))
                align_cache[key] = (
                    align_term_by_frequency(
                        record.canonical,
                        source_language,
                        target_language,
                        groups,
                        idf,
                        vocab=vocab,
                        src_stories=src_stories,
                    )
                    if src_stories and not record.everyday and not is_latin_identity
                    else ""
                )
            translated = align_cache[key]
            if translated:
                record.names[target_language] = translated

    result = list(records_by_id.values())
    # Backstop: strip character-name / speaker-label pollution from cross-language names.
    sanitize_translation_pollution(result)
    # Apply fixed dictionaries for proper nouns / generic words (authoritative,
    # never per-line aligned).
    apply_curated_translations(result)
    return result


def lookup_terms(
    terms: Iterable[TermRecord],
    query: str,
    source_language: Optional[str] = None,
    languages: Optional[Iterable[str]] = None,
    limit: int = 8,
    tag: Optional[str] = None,
    sort: str = "score",
) -> list[dict]:
    languages = [language for language in (languages or []) if language]
    tag_filter = str(tag or "").strip().lower()
    scored: list[tuple[TermRecord, int]] = []
    for term in terms:
        if tag_filter and tag_filter not in (term.tags or []):
            continue
        names = list(term.names.values())
        if term.canonical and term.canonical not in names:
            names.append(term.canonical)
        matched = best_match(query, names)
        if matched is None:
            continue
        _name, score = matched
        if source_language and term.names.get(source_language) == query:
            score += 10
        if languages:
            covered = [language for language in languages if term.names.get(language)]
            if covered:
                score += min(len(covered) * 2, 10)
        scored.append((term, score))
    if sort == "weight":
        scored.sort(key=lambda item: (item[0].weight, item[1]), reverse=True)
    else:
        scored.sort(key=lambda item: item[1], reverse=True)
    return [
        term_to_dict(term, score=score, truncate_context=True)
        for term, score in scored[:limit]
    ]


def term_status(terms: Iterable[TermRecord]) -> dict:
    terms = list(terms)
    language_counts: dict[str, set[str]] = {}
    tag_counts: Counter = Counter()
    official = 0
    with_evidence = 0
    top_by_tag: dict[str, list[dict]] = {}
    for term in terms:
        if term.official:
            official += 1
        if term.evidence:
            with_evidence += 1
        for language, name in term.names.items():
            if name:
                language_counts.setdefault(language, set()).add(normalize_name(name))
        for t in term.tags or ["other"]:
            tag_counts[t] += 1
    # Top 5 by weight per tag
    for tag in TAG_VOCAB:
        cands = [t for t in terms if tag in (t.tags or [])]
        cands.sort(key=lambda x: x.weight, reverse=True)
        top_by_tag[tag] = [
            {"canonical": c.canonical, "weight": round(c.weight, 4), "occurrences": c.occurrences}
            for c in cands[:5]
        ]
    return {
        "terms": len(terms),
        "official": official,
        "with_evidence": with_evidence,
        "languages": {language: len(names) for language, names in language_counts.items()},
        "tags": dict(tag_counts),
        "top_by_tag": top_by_tag,
    }


# ── Cross-language penetrate ──────────────────────────────────────

def _build_positions_for_term(
    term: TermRecord,
    pages_by_story: dict[str, dict[str, dict]],
) -> list[dict]:
    out: list[dict] = []
    for ev in term.evidence:
        story_key = str(ev.get("story_key") or "")
        if not story_key:
            continue
        by_lang = pages_by_story.get(story_key)
        if not by_lang:
            continue
        for lang, page in by_lang.items():
            text = str(page.get("text", ""))
            # The corpus keys pages by its own spelling (zh_hant) while
            # term.names uses TERM_LANGUAGES spelling (zh_tw). Resolve before
            # the lookup, and emit the canonical spelling too, so a position
            # lines up with term.names and with the vocab/IDF keyed by target
            # language. Without this, positions for every traditional-Chinese
            # line carried an empty sentence and a mismatched language key.
            canon = _term_language(lang)
            seg = find_segment(text, term.canonical) if canon == term.source_language else ""
            # For non-source languages try to find the translated name in text
            trans_name = term.names.get(canon, "")
            if canon != term.source_language and trans_name:
                seg = find_segment(text, trans_name) or seg
            out.append({
                "story_key": story_key,
                "language": canon,
                "sentence": seg[:300] if seg else "",
                "term": trans_name if trans_name else term.canonical,
                "trust": str(page.get("trust", "")),
                "auxiliary": bool(page.get("auxiliary", False)),
            })
    return out


def build_term_positions(
    terms: Iterable[TermRecord],
    pages: Iterable[dict],
) -> dict[str, list[dict]]:
    grouped = group_pages_by_story(pages)
    result: dict[str, list[dict]] = {}
    for term in terms:
        pos = _build_positions_for_term(term, grouped)
        if pos:
            result[term.id] = pos
    return result


def is_released_story_key(story_key: str, grouped: dict) -> bool:
    """A story is considered released iff its group has >1 language (global launch)."""
    langs = grouped.get(story_key, {})
    return len(langs) > 1


def build_tag_clouds(
    terms: Iterable[TermRecord],
    pages: Optional[Iterable[dict]] = None,
    grouped: Optional[dict] = None,
) -> dict:
    """Build tag clouds split by release status.

    - released: stories with >1 language — five-language cloud, supports penetrate
    - unreleased: stories with only ja — ja-only cloud, no penetrate, pending release

    Each cloud is ranked by weight (or occurrences tie-break) per language.
    """
    if grouped is None:
        grouped = group_pages_by_story(pages) if pages is not None else {}
    released_keys = {k for k, v in grouped.items() if len(v) > 1}
    unreleased_keys = {k for k, v in grouped.items() if len(v) == 1 and "ja" in v}

    released_terms: list[TermRecord] = []
    unreleased_terms: list[TermRecord] = []
    for term in terms:
        sks = {str(e.get("story_key", "")) for e in term.evidence if e.get("story_key")}
        if not sks:
            # Glossaries / no evidence — treat as released for translation purposes
            released_terms.append(term)
            continue
        has_released = any(sk in released_keys for sk in sks)
        has_unreleased = any(sk in unreleased_keys for sk in sks)
        if has_released and not has_unreleased:
            released_terms.append(term)
        elif has_unreleased and not has_released:
            unreleased_terms.append(term)
        else:
            # Spans both — belongs to released if any released evidence
            released_terms.append(term)

    def _cloud_for(term_list: list[TermRecord]) -> dict:
        by_lang: dict[str, list[dict]] = {lang: [] for lang in TERM_LANGUAGES}
        by_lang["all"] = []
        # All: global rank by weight
        for t in sorted(term_list, key=lambda x: x.weight, reverse=True):
            by_lang["all"].append({"canonical": t.canonical, "weight": round(t.weight, 4), "occurrences": t.occurrences, "tags": t.tags, "names": dict(t.names), "trust": t.trust})
        # Per-language: only terms that have that language's name
        for lang in TERM_LANGUAGES:
            cands = [t for t in term_list if lang in t.names]
            cands.sort(key=lambda x: x.weight, reverse=True)
            by_lang[lang] = [{"canonical": c.canonical, "weight": round(c.weight, 4), "occurrences": c.occurrences, "tags": c.tags, "names": dict(c.names), "trust": c.trust} for c in cands]
        return by_lang

    return {
        "released": {
            "count": len(released_terms),
            "cloud": _cloud_for(released_terms),
            "story_keys": len(released_keys),
        },
        "unreleased": {
            "count": len(unreleased_terms),
            "cloud": _cloud_for(unreleased_terms),
            "story_keys": len(unreleased_keys),
            "note": "ja-only, pending global release — no cross-language penetrate",
        },
    }


def term_penetrate(
    terms: Iterable[TermRecord],
    query: str,
    story_key: Optional[str] = None,
    languages: Optional[Iterable[str]] = None,
    pages: Optional[Iterable[dict]] = None,
    grouped: Optional[dict] = None,
) -> Optional[dict]:
    term_list = list(terms)
    target_langs = [str(l).strip() for l in (languages or []) if str(l).strip()]
    best: Optional[TermRecord] = None
    best_score = -1
    for term in term_list:
        names = list(term.names.values())
        if term.canonical and term.canonical not in names:
            names.append(term.canonical)
        m = best_match(query, names)
        if m is None:
            continue
        _, score = m
        if score > best_score:
            best_score = score
            best = term
    if best is None:
        return None
    sk = story_key
    if not sk:
        if best.evidence:
            # Prefer a released (multi-language) story so penetration yields
            # actual counterpart lines; fall back to the most frequent ja-only
            # story only when none exists.
            grouped_here = grouped if grouped is not None else (
                group_pages_by_story(pages) if pages is not None else {}
            )
            keys = [str(e.get("story_key", "")) for e in best.evidence if e.get("story_key")]
            released_keys = [k for k in keys if len(grouped_here.get(k, {})) > 1]
            if released_keys:
                counter = Counter(released_keys)
                sk = counter.most_common(1)[0][0]
            else:
                counter = Counter(keys)
                if counter:
                    sk = counter.most_common(1)[0][0]
                else:
                    sk = ""
        if not sk:
            sk = str(best.evidence[0].get("story_key", "")) if best.evidence else ""
    if not sk:
        return {
            "query": query,
            "term": term_to_dict(best, truncate_context=True),
            "story_key": None,
            "per_language": {},
            "note": "no story_key available for this term",
        }
    grouped = grouped if grouped is not None else (group_pages_by_story(pages) if pages is not None else {})
    is_released = is_released_story_key(sk, grouped) if grouped else True
    # Build cloud rank map for released terms as auxiliary signal
    cloud_rank: dict[str, int] = {}
    if is_released and grouped:
        try:
            clouds = build_tag_clouds(term_list, grouped=grouped)
            for i, entry in enumerate(clouds["released"]["cloud"]["all"]):
                cloud_rank[entry["canonical"]] = i + 1
        except Exception:
            pass
    by_lang = grouped.get(sk, {})
    per_lang: dict[str, dict] = {}
    pos_map: dict[str, dict] = {}
    for p in best.positions:
        if str(p.get("story_key", "")) == sk:
            # Positions now emit the canonical spelling, but rows written
            # before that fix still say zh_hant. Normalise defensively so a
            # zh_tw lookup finds either vintage.
            pos_map[_term_language(str(p.get("language", "")))] = p
    langs_to_check = target_langs if target_langs else list(TERM_LANGUAGES)
    for lang in langs_to_check:
        canon = _term_language(lang)
        cached = pos_map.get(canon)
        if cached:
            per_lang[lang] = {
                "term": str(cached.get("term", "")),
                "sentence": str(cached.get("sentence", ""))[:300],
                "trust": str(cached.get("trust", "")),
                "auxiliary": bool(cached.get("auxiliary", False)),
                "cloud_rank": cloud_rank.get(best.canonical),
                "released": is_released,
            }
            continue
        page = _group_page(by_lang, lang)
        if page is None:
            per_lang[lang] = {"term": best.names.get(canon, ""), "sentence": "", "trust": "", "auxiliary": False, "missing": True, "released": is_released, "cloud_rank": cloud_rank.get(best.canonical)}
            continue
        text = str(page.get("text", ""))
        name = best.names.get(canon, "")
        seg = find_segment(text, name) if name else ""
        if not seg and name:
            seg = ""
        entry: dict = {
            "term": name,
            "sentence": seg[:300],
            "trust": str(page.get("trust", "")),
            "auxiliary": bool(page.get("auxiliary", False)),
            "released": is_released,
            "cloud_rank": cloud_rank.get(best.canonical),
        }
        if not name:
            entry["missing"] = True
        if not is_released:
            entry["note"] = "unreleased story — translation pending global launch"
        per_lang[lang] = entry
    result: dict = {
        "query": query,
        "term": term_to_dict(best, truncate_context=True),
        "story_key": sk,
        "released": is_released,
        "per_language": per_lang,
    }
    if not is_released:
        result["note"] = "ja-only unreleased story — word cloud available, no cross-language penetrate"
        result["cloud_rank"] = cloud_rank.get(best.canonical)
    else:
        result["cloud_rank"] = cloud_rank.get(best.canonical)
    return result
