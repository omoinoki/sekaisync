"""智能体干预环（agent-in-the-loop）—— 把正在使用 sekaisync 的编码智能体接进术语裁决。

设计动机
--------
sekaisync 是给 AI Agent 用的本地知识库，**不能要求用户填 LLM API Key**：主流 AI
订阅服务对第三方应用的接入有限制（只允许特定 IDE / 官方客户端）。但反过来看，
**智能体本身就有 AI 算力**——Claude Code / Cursor / ZCode 的会话里就坐着推理能力。
所以正确的设计不是"sekaisync 去申请一个 API Key"，而是：

    sekaisync 提供本地可控的直连接口 → 智能体在自己的会话里判断 → sekaisync 存储与复用

本模块就是这个接口层。它不调用任何模型，只做三件事：**排队、存判断、复用判断**。

为什么用文件队列而不是 HTTP / MCP 直连
-------------------------------------
* 智能体在自己的会话里执行 shell 最自然（Claude Code / ZCode / Cursor 都有 exec），
  一条 ``python -m sekaisync.agent_review export`` 就能拿到任务，无需服务常驻。
* 文件即接口：天然可审计、可 diff、可回滚，断电不丢；不存在端口/进程/鉴权问题。
* 零第三方依赖，纯标准库，任意 Python 3.10+ 环境直接跑。

token 效率（硬要求）
-------------------
智能体没有耐心读语料。:class:`ReviewItem` **完全自包含**：≤3 条证据句、每句 ≤200 字、
≤3 个来源故事 key，判断一条队列项 **不需要回读 750k 页语料**。证据在构造时就被
脱敏截断（:func:`_sanitize_evidence`），而不是等智能体自己截。

核心价值：干预量随时间递减
--------------------------
干预不该是一次性的。智能体每次裁决都可以沉淀为**方法论**
（:class:`MethodologyEntry`）：pair 级（精确到 term+lang+candidate）或 pattern 级
（正则/前缀，泛化）。主管线刮削时调用 :func:`consult` 自动套用，已裁决过的候选
不再进队列（``enqueue`` 的 ``skipped_settled``），于是 **智能体需要裁决的条数
逐轮下降**，而 :func:`review_stats` 里的 ``reuse_rate``（复用次数 / 沉淀条数）
单调上升。

粒度：pair 与 pattern 的取舍
----------------------------
* **pair**（``term|lang|candidate``）：只对该术语该语言生效，绝对安全，精确。
* **pattern**（正则/前缀）：泛化，能一次覆盖一批（如"该作品里 N 开头数字缩写是合法
  英文简写"）。风险是**过度泛化**——本模块用两道闸挡：写入前
  :func:`validate_pattern` 拒绝空匹配/无字面字符/命中面过宽的写法；写入后每次命中
  记录 ``provenance["hit_terms"]``（**命中范围**留痕），便于事后审计并
  :func:`retire_methodology_entry` 失效。

审计与回滚
----------
每条方法论带 ``provenance``（agent / session / ts / 来源队列项 id / 命中范围）与
``hits`` 计数，可追溯"这条规则是谁什么时候根据哪次干预定的"；:func:`retire_methodology_entry`
提供失效接口（软删除，默认不再被 ``consult`` 采用，但保留审计痕迹）。

存储（唯一数据层 kb/，不用可再生成的 cache/）
--------------------------------------------
``store/kb/terms/review_queue.json``     待裁决队列
``store/kb/terms/methodology.json``      方法论库

硬约束：零第三方依赖（只用标准库）；不修改任何既有文件；Python 3.10+。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    # 数据结构
    "ReviewItem",
    "MethodologyEntry",
    "ReviewFormatError",
    # 路径
    "queue_path",
    "methodology_path",
    # 队列
    "item_id",
    "make_review_item",
    "enqueue",
    "load_queue",
    # 裁决
    "submit_judgments",
    "load_methodology",
    "consult",
    "apply_methodology_batch",
    "export_for_agent",
    "import_judgments_from_text",
    # 维护 / 诊断
    "review_stats",
    "validate_pattern",
    "retire_methodology_entry",
]

# ── 常量 ────────────────────────────────────────────────────────────────

SCHEMA_VERSION = 1

#: 队列项类型。其它值也允许（留给别的管线扩展），这里只做文档与统计分组。
REVIEW_KINDS = ("conflict", "pending", "gate_failed")

#: 方法论类型。
METHODOLOGY_KINDS = (
    "pair_accept",
    "pair_reject",
    "pattern_accept",
    "pattern_reject",
    "heuristic",
)

#: 只有这四种会被 :func:`consult` 自动套用；heuristic 是给智能体/人看的备注。
DECISION_KINDS = ("pair_accept", "pair_reject", "pattern_accept", "pattern_reject")

DECISIONS = ("accept", "reject", "replace")

#: token 效率：证据句数量 / 单句长度 / 故事 key 数量的上限。
EVIDENCE_MAX_ITEMS = 3
EVIDENCE_MAX_CHARS = 200
STORY_KEYS_MAX = 3
REASON_MAX_CHARS = 300
#: pattern 命中范围留痕的术语数量上限（防止 provenance 无限膨胀）。
HIT_TERMS_MAX = 24

#: pair 级 key 里不允许出现 ``|``（会破坏 "term|lang|candidate" 的可解析性），
#: 统一替换为 ``¦``（U+00A6）。
_PIPE_SAFE_MAP = {"|": "¦"}

_WS_RE = re.compile(r"\s+")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CODE_FENCE_RE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*$")

#: 进程内方法论缓存： path -> (stamp, entries, pair_index, pattern_index)
#: 热路径（consult）走这里；stamp = (st_mtime_ns, st_size)，文件一变立刻失效。
_METHOD_CACHE: dict[str, tuple[tuple[int, int], list[MethodologyEntry], dict, list]] = {}


class ReviewFormatError(ValueError):
    """紧凑格式解析失败。异常文本**一定带行号**，便于智能体自我修正。"""


# ── 基础工具 ───────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_text(value: Any) -> str:
    """折叠空白、剔除控制字符（防止证据句里混进换行/终端控制序列）。"""
    text = _CTRL_RE.sub(" ", str(value if value is not None else ""))
    return _WS_RE.sub(" ", text).strip()


def _sanitize_evidence(value: Any) -> str:
    """证据句脱敏：单句 ≤200 字，超长截断并加省略号。"""
    text = _sanitize_text(value)
    if len(text) > EVIDENCE_MAX_CHARS:
        text = text[: EVIDENCE_MAX_CHARS - 1] + "…"
    return text


def _dedupe(values: Iterable[str]) -> list[str]:
    """保序去重（智能体看到的候选顺序必须稳定，否则 id 不稳定）。"""
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        text = _sanitize_text(raw)
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _pipe_safe(value: str) -> str:
    text = _sanitize_text(value)
    for bad, good in _PIPE_SAFE_MAP.items():
        text = text.replace(bad, good)
    return text


def _atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再 ``os.replace``，避免中断留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _write_json(path: Path, payload: dict) -> None:
    """原子写 JSON。缓存不在这里失效——交给 stamp 机制，避免"写后必失"掩盖 bug。"""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False)
    _atomic_write_text(path, text + "\n")


def _read_json(path: Path) -> dict:
    """读 JSON；文件不存在/损坏都退化成空结构（队列不是唯一真相来源）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


# ── 路径（唯一数据层：kb/，不用 cache/） ───────────────────────────────


def _terms_dir(store_root: Path) -> Path:
    return Path(store_root) / "kb" / "terms"


def queue_path(store_root: Path) -> Path:
    """待裁决队列：``store/kb/terms/review_queue.json``。"""
    return _terms_dir(store_root) / "review_queue.json"


def methodology_path(store_root: Path) -> Path:
    """方法论库：``store/kb/terms/methodology.json``。"""
    return _terms_dir(store_root) / "methodology.json"


# ── 数据结构 ───────────────────────────────────────────────────────────


@dataclass
class ReviewItem:
    """一条待裁决项。必须自包含——智能体只读这一条就能判断，无需回读语料（token 效率是硬要求）。"""

    id: str  # 稳定 id（term+lang+候选 的 hash），重复入队自动去重
    kind: str  # "conflict"（通道冲突）/ "pending"（低置信）/ "gate_failed"（门控未过）
    term: str  # 源术语（如 ニーゴ）
    language: str  # 目标语言（如 en）
    candidates: list[str] = field(default_factory=list)  # 待裁决候选
    chosen_hint: Optional[str] = None  # 建议值（若有）
    evidence: list[str] = field(default_factory=list)  # ≤3 条证据句（各 ≤200 字）
    story_keys: list[str] = field(default_factory=list)  # ≤3 个来源故事 key
    channels: list[str] = field(default_factory=list)  # 哪些通道产出了它
    reason: str = ""  # 为何需要裁决（人类可读一行）
    created_at: str = ""

    def __post_init__(self) -> None:
        # 截断发生在**构造时**，保证落盘与导出给智能体的都是已经瘦身过的内容。
        self.id = _sanitize_text(self.id)
        self.kind = _sanitize_text(self.kind) or "pending"
        self.term = _sanitize_text(self.term)
        self.language = _sanitize_text(self.language)
        self.candidates = _dedupe(self.candidates)
        hint = _sanitize_text(self.chosen_hint) if self.chosen_hint else ""
        self.chosen_hint = hint or None
        self.evidence = [
            text
            for text in (_sanitize_evidence(x) for x in self.evidence[:EVIDENCE_MAX_ITEMS])
            if text
        ]
        self.story_keys = _dedupe(self.story_keys)[:STORY_KEYS_MAX]
        self.channels = _dedupe(self.channels)
        self.reason = _sanitize_text(self.reason)[:REASON_MAX_CHARS]
        if not self.created_at:
            self.created_at = now_iso()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "term": self.term,
            "language": self.language,
            "candidates": list(self.candidates),
            "chosen_hint": self.chosen_hint,
            "evidence": list(self.evidence),
            "story_keys": list(self.story_keys),
            "channels": list(self.channels),
            "reason": self.reason,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "ReviewItem":
        return cls(
            id=str(data.get("id", "")),
            kind=str(data.get("kind", "pending")),
            term=str(data.get("term", "")),
            language=str(data.get("language", "")),
            candidates=[str(x) for x in data.get("candidates", []) or []],
            chosen_hint=data.get("chosen_hint"),
            evidence=[str(x) for x in data.get("evidence", []) or []],
            story_keys=[str(x) for x in data.get("story_keys", []) or []],
            channels=[str(x) for x in data.get("channels", []) or []],
            reason=str(data.get("reason", "")),
            created_at=str(data.get("created_at", "")),
        )


@dataclass
class MethodologyEntry:
    """一条沉淀的方法论。"""

    kind: str  # "pair_accept" | "pair_reject" | "pattern_accept" | "pattern_reject" | "heuristic"
    key: str  # pair: "term|lang|candidate"；pattern: 正则（re:）或前缀（prefix:/suffix:）；heuristic: 规则名
    value: str = ""  # 采纳值（pair_accept）或空
    rationale: str = ""  # 为何（智能体写的中文理由）
    provenance: dict = field(default_factory=dict)  # {"agent","session","ts"}
    hits: int = 0  # 被复用次数

    def __post_init__(self) -> None:
        self.kind = _sanitize_text(self.kind) or "heuristic"
        self.key = _sanitize_text(self.key)
        self.value = _sanitize_text(self.value)
        self.rationale = _sanitize_text(self.rationale)[:REASON_MAX_CHARS]
        self.provenance = dict(self.provenance or {})
        self.provenance.setdefault("agent", "unknown-agent")
        self.provenance.setdefault("session", "")
        self.provenance.setdefault("ts", now_iso())
        try:
            self.hits = max(0, int(self.hits))
        except (TypeError, ValueError):
            self.hits = 0

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "key": self.key,
            "value": self.value,
            "rationale": self.rationale,
            "provenance": dict(self.provenance),
            "hits": self.hits,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "MethodologyEntry":
        provenance = data.get("provenance")
        return cls(
            kind=str(data.get("kind", "heuristic")),
            key=str(data.get("key", "")),
            value=str(data.get("value", "")),
            rationale=str(data.get("rationale", "")),
            provenance=dict(provenance) if isinstance(provenance, dict) else {},
            hits=data.get("hits", 0) or 0,
        )

    @property
    def retired(self) -> bool:
        """软删除标记（``retire_methodology_entry`` 写入 provenance）。"""
        return bool(self.provenance.get("retired"))


# ── id 与构造 ──────────────────────────────────────────────────────────


def item_id(term: str, language: str, candidates: Iterable[str]) -> str:
    """稳定 id = hash(term + lang + 候选集合)。

    候选**排序后**入 hash：通道产出顺序抖动不会造成重复入队（这是"重复入队自动
    去重"能成立的关键）。``kind`` 不参与 hash——同一 term/lang/候选即使先后被
    conflict 与 gate_failed 报出，需要智能体做的判断是同一个，去重即省 token。
    """
    norm_term = unicodedata.normalize("NFC", _sanitize_text(term))
    norm_lang = _sanitize_text(language).lower()
    cands = sorted(_dedupe(candidates))
    payload = "\x00".join([norm_term, norm_lang, *cands])
    digest = hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
    return f"rv:{digest}"


def make_review_item(
    term: str,
    language: str,
    candidates: Iterable[str],
    *,
    kind: str = "pending",
    chosen_hint: Optional[str] = None,
    evidence: Iterable[str] = (),
    story_keys: Iterable[str] = (),
    channels: Iterable[str] = (),
    reason: str = "",
) -> ReviewItem:
    """主管线构造队列项的便捷入口（自动算 :func:`item_id`，自动瘦身证据）。"""
    cands = _dedupe(candidates)
    return ReviewItem(
        id=item_id(term, language, cands),
        kind=kind,
        term=term,
        language=language,
        candidates=cands,
        chosen_hint=chosen_hint,
        evidence=list(evidence),
        story_keys=list(story_keys),
        channels=list(channels),
        reason=reason,
    )


# ── 队列读写 ───────────────────────────────────────────────────────────


def _read_queue(store_root: Path) -> list[ReviewItem]:
    data = _read_json(queue_path(store_root))
    raw = data.get("items")
    if not isinstance(raw, list):
        return []
    items: list[ReviewItem] = []
    for entry in raw:
        if isinstance(entry, dict):
            items.append(ReviewItem.from_dict(entry))
    return items


def _write_queue(store_root: Path, items: list[ReviewItem]) -> Path:
    path = queue_path(store_root)
    _write_json(
        path,
        {
            "version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "items": [item.to_dict() for item in items],
        },
    )
    return path


def load_queue(
    store_root: Path,
    *,
    limit: int = 0,
    kind: Optional[str] = None,
) -> list[ReviewItem]:
    """读取待裁决队列（智能体用此接口取任务）。``limit=0`` 表示全部。

    顺序 = 入队顺序（FIFO），稳定可复现；``kind`` 过滤在读取后施加，
    不改变相对顺序。
    """
    items = _read_queue(store_root)
    if kind:
        wanted = _sanitize_text(kind)
        items = [item for item in items if item.kind == wanted]
    if limit and limit > 0:
        items = items[:limit]
    return items


def _load_methodology_entries(store_root: Path) -> list[MethodologyEntry]:
    data = _read_json(methodology_path(store_root))
    raw = data.get("entries")
    if not isinstance(raw, list):
        return []
    entries: list[MethodologyEntry] = []
    for entry in raw:
        if isinstance(entry, dict):
            entries.append(MethodologyEntry.from_dict(entry))
    return entries


def _write_methodology(store_root: Path, entries: list[MethodologyEntry]) -> Path:
    path = methodology_path(store_root)
    _write_json(
        path,
        {
            "version": SCHEMA_VERSION,
            "updated_at": now_iso(),
            "entries": [entry.to_dict() for entry in entries],
        },
    )
    return path


def load_methodology(store_root: Path) -> list[MethodologyEntry]:
    """读取全部方法论条目（含已失效的；``entry.retired`` 为 True 者 consult 不再采用）。"""
    return _load_methodology_entries(store_root)


def enqueue(store_root: Path, items: list[ReviewItem]) -> dict:
    """入队（自动去重：同 id 已存在则跳过，已在方法论中被裁决过的也跳过）。

    返回 ``{"added", "skipped_dup", "skipped_settled", "queue_size"}``。

    * ``skipped_dup``     —— 队列里已经有同 id 的项（含同一批里的重复）。
    * ``skipped_settled`` —— :func:`consult` 的**只读**版本能给出决策，即已有方法论
      可以复用，不必再打扰智能体（这正是"干预量随时间递减"的落点）。
    * 这里的检索**不累加 hits**：入队不是复用，不该虚增命中计数。
    """
    added = 0
    skipped_dup = 0
    skipped_settled = 0

    queue_items = _read_queue(store_root)
    seen_ids = {item.id for item in queue_items}
    # 走缓存读（同时拿到已建好的索引），避免每次入队都重新编译 pattern 正则。
    _read_methodology(store_root)
    cached = _METHOD_CACHE.get(str(methodology_path(store_root)))
    pair_index = cached[2] if cached else {}
    pattern_index = cached[3] if cached else []

    for item in items:
        if not item.id:
            item.id = item_id(item.term, item.language, item.candidates)
        if item.id in seen_ids:
            skipped_dup += 1
            continue
        settled = _lookup(pair_index, pattern_index, item.term, item.language, item.candidates)
        if settled is not None:
            skipped_settled += 1
            continue
        seen_ids.add(item.id)
        queue_items.append(item)
        added += 1

    if added:
        _write_queue(store_root, queue_items)
    return {
        "added": added,
        "skipped_dup": skipped_dup,
        "skipped_settled": skipped_settled,
        "queue_size": len(queue_items),
    }


# ── 方法论读取 / 索引（热路径） ────────────────────────────────────────


def _read_methodology(store_root: Path) -> list[MethodologyEntry]:
    """缓存读。stamp = (mtime_ns, size)；文件一变立刻失效。"""
    path = methodology_path(store_root)
    key = str(path)
    try:
        stat = path.stat()
    except OSError:
        _METHOD_CACHE.pop(key, None)
        return []
    stamp = (stat.st_mtime_ns, stat.st_size)
    cached = _METHOD_CACHE.get(key)
    if cached is not None and cached[0] == stamp:
        return list(cached[1])
    entries = _load_methodology_entries(store_root)
    _METHOD_CACHE[key] = (stamp, entries, _build_pair_index(entries), _build_pattern_index(entries))
    return list(entries)


def _pair_key(term: str, language: str, candidate: str) -> str:
    return f"{_pipe_safe(term)}|{_pipe_safe(language)}|{_pipe_safe(candidate)}"


def _build_pair_index(entries: Iterable[MethodologyEntry]) -> dict[str, MethodologyEntry]:
    index: dict[str, MethodologyEntry] = {}
    for entry in entries:
        if entry.kind not in ("pair_accept", "pair_reject"):
            continue
        if entry.retired:
            continue
        index[entry.key] = entry
    return index


def _build_pattern_index(entries: Iterable[MethodologyEntry]) -> list[tuple[MethodologyEntry, Any, str]]:
    """[(entry, compiled|None, mode)]，mode ∈ {"re", "prefix", "suffix", "literal"}。"""
    out: list[tuple[MethodologyEntry, Any, str]] = []
    for entry in entries:
        if entry.kind not in ("pattern_accept", "pattern_reject"):
            continue
        if entry.retired:
            continue
        mode, pattern = _pattern_mode(entry.key)
        if mode == "re":
            try:
                compiled = re.compile(pattern)
            except re.error:
                continue  # 坏正则不参与命中（写入前 validate_pattern 已挡过一次）
            out.append((entry, compiled, "re"))
        else:
            out.append((entry, pattern, mode))
    return out


def _pattern_mode(key: str) -> tuple[str, str]:
    """把 pattern 键拆成 (mode, payload)。约定见模块 docstring / validate_pattern。"""
    text = _sanitize_text(key)
    lowered = text.lower()
    if lowered.startswith("re:"):
        return "re", text[3:]
    if lowered.startswith("prefix:"):
        return "prefix", text[len("prefix:"):]
    if lowered.startswith("suffix:"):
        return "suffix", text[len("suffix:"):]
    return "literal", text


def _pattern_matches(pattern_entry: tuple[MethodologyEntry, Any, str], candidate: str) -> bool:
    _, payload, mode = pattern_entry
    if mode == "re":
        return bool(payload.search(candidate))
    if mode == "prefix":
        return candidate.startswith(payload)
    if mode == "suffix":
        return candidate.endswith(payload)
    return candidate == payload


def _pattern_scope_ok(entry: MethodologyEntry, term: str, language: str) -> bool:
    """pattern 的命中的**作用域**限制（防止过度泛化）。"""
    scope = entry.provenance.get("scope")
    if scope == "global":
        return True
    if not isinstance(scope, dict) or not scope:
        return True
    if scope.get("term") and scope["term"] != term:
        return False
    if scope.get("term_prefix") and not term.startswith(str(scope["term_prefix"])):
        return False
    if scope.get("term_pattern"):
        try:
            if not re.search(str(scope["term_pattern"]), term):
                return False
        except re.error:
            return False
    if scope.get("lang") and scope["lang"] != language:
        return False
    return True


def _lookup(
    pair_index: dict[str, MethodologyEntry],
    pattern_index: list[tuple[MethodologyEntry, Any, str]],
    term: str,
    language: str,
    candidates: Iterable[str],
) -> Optional[dict]:
    """纯查询（**不落盘、不累加 hits**）。

    优先级：精确 pair_accept → 全候选被 pair_reject → pattern_accept →
    全候选被 pattern_reject → None（未命中，需要智能体）。

    ``pattern_accept`` 有两种语义，由 ``value`` 是否为空区分：

    * ``value`` 为空 —— **形态规则**（"匹配此形态的候选本身就可采纳"），返回候选原值；
    * ``value`` 非空 —— **映射规则**（"匹配此形态的候选一律映射到 value"），返回 value。

    部分候选有结论、部分没有（例如只 reject 了 "Huh" 但 "N25" 从没见过）时返回
    ``None``：宁可交给智能体整条判断，也不猜。
    """
    cands = _dedupe(candidates)
    if not cands:
        return None

    exact = [pair_index.get(_pair_key(term, language, cand)) for cand in cands]
    for cand, entry in zip(cands, exact):
        if entry is not None and entry.kind == "pair_accept":
            return _decision_payload("accept", entry.value or cand, entry, cand, "pair")
    if all(entry is not None and entry.kind == "pair_reject" for entry in exact):
        return _decision_payload("reject", "", exact[0], "", "pair", rejected=list(cands))

    for cand in cands:
        for pattern_entry in pattern_index:
            entry = pattern_entry[0]
            if entry.kind != "pattern_accept":
                continue
            if not _pattern_scope_ok(entry, term, language):
                continue
            if _pattern_matches(pattern_entry, cand):
                return _decision_payload("accept", entry.value or cand, entry, cand, "pattern")

    reject_hits: list[MethodologyEntry] = []
    for cand in cands:
        matched: Optional[MethodologyEntry] = None
        for pattern_entry in pattern_index:
            entry = pattern_entry[0]
            if entry.kind != "pattern_reject":
                continue
            if not _pattern_scope_ok(entry, term, language):
                continue
            if _pattern_matches(pattern_entry, cand):
                matched = entry
                break
        if matched is None:
            reject_hits = []
            break
        reject_hits.append(matched)
    if reject_hits:
        return _decision_payload(
            "reject", "", reject_hits[0], "", "pattern", rejected=list(cands)
        )
    return None


def _decision_payload(
    decision: str,
    value: str,
    entry: Any,
    candidate: str,
    source: str,
    rejected: Optional[list[str]] = None,
) -> dict:
    return {
        "decision": decision,
        "value": value,
        "entry": entry.to_dict() if isinstance(entry, MethodologyEntry) else dict(entry or {}),
        "candidate": candidate,
        "source": source,
        "rejected": list(rejected or []),
    }


def _record_hit(store_root: Path, key: str, term: str) -> None:
    """命中计数 + 命中范围留痕，落盘（热路径唯一的一次写）。

    写之前重新从盘上读一遍（而非复用缓存），避免覆盖别的进程刚落盘的条目。
    可用 ``SEKAISYNC_REVIEW_NO_HITS=1`` 关闭（只读/挂载受限的热路径）。
    """
    if os.environ.get("SEKAISYNC_REVIEW_NO_HITS"):
        return
    entries = _load_methodology_entries(store_root)
    for entry in entries:
        if entry.key != key:
            continue
        entry.hits += 1
        if entry.kind.startswith("pattern"):
            observed = entry.provenance.get("hit_terms")
            if not isinstance(observed, list):
                observed = []
            if term and term not in observed and len(observed) < HIT_TERMS_MAX:
                observed.append(term)
                entry.provenance["hit_terms"] = observed
        entry.provenance["last_hit_at"] = now_iso()
        _write_methodology(store_root, entries)
        return


def consult(store_root: Path, term: str, language: str, candidates: list[str]) -> Optional[dict]:
    """刮削时查询方法论（供主管线调用）。

    命中 pair_accept / pair_reject / pattern 则返回
    ``{"decision","value","entry","candidate","source","rejected"}``；
    未命中返回 ``None``。命中条目的 ``hits += 1`` 并落盘（pattern 条目同时记录
    ``provenance["hit_terms"]``，即实际命中范围）。

    **热路径**：方法论索引进程内缓存，stamp=(mtime_ns,size) 变化即失效，所以别的
    进程（或智能体手工编辑）改了 methodology.json，下一次调用就能读到新条目。
    ``heuristic`` 条目不参与自动裁决（它只是给人/智能体看的备注），需要人判断。
    """
    path_key = str(methodology_path(store_root))
    # 每次都过一遍 _read_methodology：它只做一次 stat，stamp 未变就复用索引
    # （热路径仍是一次 syscall），stamp 变了立刻重建。**不能**直接读
    # _METHOD_CACHE，否则文件被别的进程改写后这里会一直用旧索引。
    _read_methodology(store_root)
    cached = _METHOD_CACHE.get(path_key)
    if cached is None:
        return None
    _, _, pair_index, pattern_index = cached
    hit = _lookup(pair_index, pattern_index, term, language, candidates)
    if hit is None:
        return None
    key = hit["entry"].get("key", "")
    if key:
        _record_hit(store_root, key, _sanitize_text(term))
    return hit


def validate_pattern(
    pattern: str,
    *,
    probes: Optional[Iterable[str]] = None,
    max_hit_ratio: float = 0.5,
) -> tuple[bool, str]:
    """pattern 级沉淀的**过度泛化**闸门。返回 ``(是否可用, 说明)``。

    拒绝：空 pattern、能匹配空串、不含任何字面字符（``re:.*`` 之类）、
    字面长度 < 2、疑似正则却漏了 ``re:`` 前缀、命中探测集比例过高。
    """
    text = _sanitize_text(pattern)
    if not text:
        return False, "pattern 为空"
    mode, payload = _pattern_mode(text)
    if mode == "re":
        try:
            compiled = re.compile(payload)
        except re.error as exc:
            return False, f"正则编译失败：{exc}"
        if compiled.search(""):
            return False, "正则能匹配空串，过于宽泛"
        literals = [ch for ch in payload if not ch.isspace() and ch not in ".*+?^$()[]{}|\\"]
        if len(literals) < 2:
            return False, "正则不含足够字面字符，过于宽泛（至少 2 个字面字符）"
        matcher = lambda cand: bool(compiled.search(cand))  # noqa: E731
    else:
        if any(token in payload for token in (".*", "^", "$", "(", ")", "[", "]")):
            return False, "疑似正则但缺少 re: 前缀（请写成 re:… 或改用 prefix:/suffix:）"
        if len(payload) < 2:
            return False, "字面前缀/后缀至少 2 个字符，否则会命中一大片"
        if mode == "prefix":
            matcher = lambda cand: cand.startswith(payload)  # noqa: E731
        elif mode == "suffix":
            matcher = lambda cand: cand.endswith(payload)  # noqa: E731
        else:
            matcher = lambda cand: cand == payload  # noqa: E731
    if probes is not None:
        probe_list = [p for p in probes if _sanitize_text(p)]
        if probe_list:
            ratio = sum(1 for p in probe_list if matcher(p)) / len(probe_list)
            if ratio > max_hit_ratio:
                return False, f"命中探测集 {ratio:.0%} > {max_hit_ratio:.0%}，泛化过头"
    return True, "ok"


def retire_methodology_entry(store_root: Path, key: str, *, reason: str = "") -> bool:
    """失效一条方法论（软删除，保留审计痕迹）。返回是否找到并改动。

    ``consult`` 从此不再采用该条；``load_methodology`` 仍能看到它（``entry.retired``），
    便于事后追溯"哪条规则被谁因为什么撤了"。
    """
    target = _sanitize_text(key)
    entries = _load_methodology_entries(store_root)
    for entry in entries:
        if entry.key != target:
            continue
        entry.provenance["retired"] = now_iso()
        if reason:
            entry.provenance["retired_reason"] = _sanitize_text(reason)
        _write_methodology(store_root, entries)
        return True
    return False


# ── 智能体写回判断 ─────────────────────────────────────────────────────

_KIND_FAMILY = {
    "pair_accept": "pair",
    "pair_reject": "pair",
    "pattern_accept": "pattern",
    "pattern_reject": "pattern",
    "heuristic": "heuristic",
}


def _new_agent_provenance(
    judgment: dict,
    item: Optional[ReviewItem],
    fallback_ts: str,
) -> dict:
    provenance: dict[str, Any] = {
        "agent": _sanitize_text(
            judgment.get("agent") or os.environ.get("SEKAISYNC_AGENT") or "unknown-agent"
        ),
        "session": _sanitize_text(
            judgment.get("session") or os.environ.get("SEKAISYNC_SESSION") or ""
        ),
        "ts": fallback_ts,
    }
    if item is not None:
        provenance["item_id"] = item.id
        provenance["review_kind"] = item.kind
    elif judgment.get("id"):
        provenance["item_id"] = _sanitize_text(judgment.get("id"))
    confidence = judgment.get("confidence")
    if confidence is not None:
        try:
            provenance["confidence"] = max(0.0, min(1.0, float(confidence)))
        except (TypeError, ValueError):
            pass
    return provenance


def _upsert_entry(
    entries: list[MethodologyEntry],
    index: dict[tuple[str, str], MethodologyEntry],
    kind: str,
    key: str,
    value: str,
    rationale: str,
    provenance: dict,
) -> str:
    """写一条方法论（同 family+key 幂等：已存在则原地更新，不产生重复条目）。

    返回 ``"added"`` / ``"updated"``。
    """
    family = _KIND_FAMILY.get(kind, kind)
    slot = (family, key)
    existing = index.get(slot)
    if existing is not None:
        existing.kind = kind
        # 翻转判定（accept↔reject）时留下痕迹，便于审计。
        if existing.value and value and existing.value != value:
            existing.provenance["superseded_value"] = existing.value
        existing.value = value
        if rationale:
            existing.rationale = rationale
        merged = dict(existing.provenance)
        merged.update(provenance)
        merged.setdefault("first_ts", existing.provenance.get("ts", ""))
        merged["hits"] = existing.hits
        existing.provenance = merged
        return "updated"
    entry = MethodologyEntry(
        kind=kind,
        key=key,
        value=value,
        rationale=rationale,
        provenance=dict(provenance),
        hits=0,
    )
    entries.append(entry)
    index[slot] = entry
    return "added"


def submit_judgments(store_root: Path, judgments: list[dict]) -> dict:
    """智能体写回判断。

    每条 judgment 形如::

        {"id": "rv:ab12cd34", "decision": "accept"|"reject"|"replace",
         "value": "N25",              # replace 必填；accept 可选（缺省走 chosen_hint→candidates[0]）
         "rationale": "ニーゴ 是 25時、ナイトコードで。的简称，官方英文名 N25",
         "confidence": 0.9,
         "generalize": "pair"|"pattern"|None,
         # 可选扩展：
         "pattern": "re:^N\\d{1,3}$",  # generalize="pattern" 时**必填**（拒绝自动泛化）
         "scope": {"lang": "en"} | "global",
         "candidates": ["Huh"],        # reject 时可指定只否掉其中几个
         "reject_others": true,        # replace 时是否顺带否掉其它候选
         "agent": "claude-code", "session": "..."}

    处理：从队列移除已裁决项 → 写入方法论（若 generalize 非空）→ 更新 hits/provenance。

    返回 ``{"accepted","rejected","replaced","methodology_added"}``，
    另附 ``methodology_updated`` / ``unknown`` / ``errors`` / ``remaining`` 便于诊断。

    **幂等**：同一条判断重复提交不会产生重复方法论条目——第一靠 ``(family, key)``
    原地更新，第二靠队列项 id 已在方法论 provenance 中留痕（``item_id``）时直接跳过。
    """
    accepted = rejected = replaced = 0
    methodology_added = 0
    methodology_updated = 0
    unknown = 0
    errors: list[str] = []

    queue_items = _read_queue(store_root)
    by_id = {item.id: item for item in queue_items}
    entries = _load_methodology_entries(store_root)
    index: dict[tuple[str, str], MethodologyEntry] = {}
    for entry in entries:
        index[(_KIND_FAMILY.get(entry.kind, entry.kind), entry.key)] = entry
    settled_item_ids = {
        str(entry.provenance.get("item_id"))
        for entry in entries
        if entry.provenance.get("item_id")
    }

    resolved_ids: set[str] = set()
    for judgment in judgments:
        if not isinstance(judgment, dict):
            errors.append("judgment 不是对象，已跳过")
            continue
        jid = _sanitize_text(judgment.get("id"))
        decision = _sanitize_text(judgment.get("decision")).lower()
        item = by_id.get(jid)
        if decision not in DECISIONS:
            errors.append(f"{jid or '<无 id>'}: 非法 decision={decision!r}（应为 accept/reject/replace）")
            continue
        if item is None:
            # 队列里没有：要么是重复提交，要么是别的进程已处理、要么是脏 id。
            if jid and jid in settled_item_ids:
                continue  # 已经在方法论里留痕 → 幂等跳过
            if not (judgment.get("term") and judgment.get("language")):
                unknown += 1
                continue
        runtime = item or make_review_item(
            term=str(judgment.get("term", "")),
            language=str(judgment.get("language", "")),
            candidates=[str(x) for x in judgment.get("candidates", []) or []],
            kind="pending",
        )
        runtime.id = jid or runtime.id

        ts = now_iso()
        provenance = _new_agent_provenance(judgment, item, ts)
        rationale = _sanitize_text(judgment.get("rationale"))
        generalize = _sanitize_text(judgment.get("generalize")).lower()
        if generalize in ("none", "null"):
            generalize = ""
        if generalize and generalize not in ("pair", "pattern"):
            errors.append(f"{jid}: 非法 generalize={generalize!r}（应为 pair/pattern/None）")
            continue

        value = _sanitize_text(judgment.get("value"))
        if decision == "accept":
            value = value or runtime.chosen_hint or (runtime.candidates[0] if runtime.candidates else "")
            if not value:
                errors.append(f"{jid}: accept 缺少 value 且队列项无候选")
                continue
            accepted += 1
        elif decision == "replace":
            if not value:
                errors.append(f"{jid}: replace 必须给出 value")
                continue
            replaced += 1
        else:  # reject
            value = ""
            rejected += 1

        if generalize:
            added, updated, pattern_error = _generalize(
                entries, index, runtime, decision, value, rationale, provenance, judgment
            )
            if pattern_error:
                errors.append(f"{jid}: {pattern_error}")
            methodology_added += added
            methodology_updated += updated

        resolved_ids.add(runtime.id)

    if resolved_ids:
        remaining = [item for item in queue_items if item.id not in resolved_ids]
        _write_queue(store_root, remaining)
    else:
        remaining = queue_items
    if methodology_added or methodology_updated:
        _write_methodology(store_root, entries)

    return {
        "accepted": accepted,
        "rejected": rejected,
        "replaced": replaced,
        "methodology_added": methodology_added,
        "methodology_updated": methodology_updated,
        "unknown": unknown,
        "errors": errors,
        "remaining": len(remaining),
    }


def _generalize(
    entries: list[MethodologyEntry],
    index: dict[tuple[str, str], MethodologyEntry],
    item: ReviewItem,
    decision: str,
    value: str,
    rationale: str,
    provenance: dict,
    judgment: dict,
) -> tuple[int, int, str]:
    """把一次裁决沉淀为方法论。返回 ``(added, updated, error)``。"""
    mode = _sanitize_text(judgment.get("generalize")).lower()
    added = updated = 0
    if mode == "pair":
        targets: list[tuple[str, str, str]] = []  # (kind, key, value)
        if decision == "accept":
            key = _pair_key(item.term, item.language, value)
            targets.append(("pair_accept", key, value))
        elif decision == "replace":
            targets.append(
                ("pair_accept", _pair_key(item.term, item.language, value), value)
            )
            if judgment.get("reject_others"):
                for cand in item.candidates:
                    if cand == value:
                        continue
                    targets.append(
                        ("pair_reject", _pair_key(item.term, item.language, cand), "")
                    )
        else:  # reject
            explicit = judgment.get("candidates")
            cands = _dedupe(explicit) if isinstance(explicit, list) and explicit else item.candidates
            if not cands:
                return 0, 0, "reject 无可否候选（队列项 candidates 为空）"
            for cand in cands:
                targets.append(("pair_reject", _pair_key(item.term, item.language, cand), ""))
        for kind, key, val in targets:
            result = _upsert_entry(entries, index, kind, key, val, rationale, provenance)
            added += result == "added"
            updated += result == "updated"
        return added, updated, ""

    # pattern 级：必须显式给出 pattern —— 不自动从单词推断，避免静默过度泛化。
    pattern = _sanitize_text(judgment.get("pattern"))
    if not pattern:
        return 0, 0, "generalize=pattern 必须显式给出 pattern 字段（不自动泛化；要精确请用 pair）"
    ok, reason = validate_pattern(pattern)
    if not ok:
        return 0, 0, f"pattern 未通过泛化校验：{reason}"
    scope = judgment.get("scope")
    if scope == "global" or (isinstance(scope, dict) and scope == {}):
        scope_value: Any = "global"
    elif isinstance(scope, dict):
        scope_value = {k: _sanitize_text(v) for k, v in scope.items()}
    else:
        # 缺省作用域 = 目标语言级：跨术语泛化但不跨语言（最常用的安全档）。
        scope_value = {"lang": item.language}
    pattern_provenance = dict(provenance)
    pattern_provenance["scope"] = scope_value
    if scope_value != "global":
        pattern_provenance.setdefault("hit_terms", [])
    kind = "pattern_accept" if decision in ("accept", "replace") else "pattern_reject"
    result = _upsert_entry(
        entries, index, kind, pattern, value if kind == "pattern_accept" else "",
        rationale, pattern_provenance,
    )
    return (result == "added"), (result == "updated"), ""


# ── 批量套用（供刮削管线） ─────────────────────────────────────────────


def apply_methodology_batch(store_root: Path, proposals: dict) -> dict:
    """批量应用：``proposals`` 形如 ``{term: {lang: [candidates]}}``。

    返回 ``{"settled": {term: {lang: value}}, "rejected": [...], "unresolved": [...]}``
    （另有 ``"consulted"`` 计数）。``rejected`` / ``unresolved`` 的元素是
    ``{"term","lang","candidates","reason"}``，比裸字符串更好定位。

    内部逐条调 :func:`consult`，因此命中的条目的 ``hits`` 会累加——这正是
    "方法论复用率"的数据来源。
    """
    settled: dict[str, dict[str, str]] = {}
    rejected: list[dict] = []
    unresolved: list[dict] = []
    consulted = 0
    for term, lang_map in (proposals or {}).items():
        if not isinstance(lang_map, dict):
            continue
        for lang, candidates in lang_map.items():
            cands = _dedupe(candidates or [])
            consulted += 1
            hit = consult(store_root, str(term), str(lang), cands)
            if hit is None:
                unresolved.append(
                    {
                        "term": _sanitize_text(term),
                        "lang": _sanitize_text(lang),
                        "candidates": cands,
                        "reason": "方法论未覆盖",
                    }
                )
            elif hit["decision"] == "accept":
                settled.setdefault(_sanitize_text(term), {})[_sanitize_text(lang)] = hit["value"]
            else:
                rejected.append(
                    {
                        "term": _sanitize_text(term),
                        "lang": _sanitize_text(lang),
                        "candidates": hit.get("rejected") or cands,
                        "reason": hit["entry"].get("rationale") or "方法论判定为不可采纳",
                    }
                )
    return {
        "settled": settled,
        "rejected": rejected,
        "unresolved": unresolved,
        "consulted": consulted,
    }


# ── 诊断 ───────────────────────────────────────────────────────────────


def review_stats(store_root: Path) -> dict:
    """诊断：队列规模、按 kind 分布、方法论条目数与命中次数 top10、累计干预量趋势。

    "干预量趋势"用两个互补指标表达：

    * ``by_day``   —— 每天新增的方法论条数（干预的发生节奏）。
    * ``reuse_rate`` —— 累计复用次数 / 沉淀条数。该值**上升即干预递减**：同样的
      规则被自动套用越来越多，需要智能体亲自裁决的越来越少。
    """
    queue_items = _read_queue(store_root)
    entries = _load_methodology_entries(store_root)

    queue_by_kind: dict[str, int] = {}
    for item in queue_items:
        queue_by_kind[item.kind] = queue_by_kind.get(item.kind, 0) + 1

    method_by_kind: dict[str, int] = {}
    by_day: dict[str, int] = {}
    retired = 0
    for entry in entries:
        method_by_kind[entry.kind] = method_by_kind.get(entry.kind, 0) + 1
        if entry.retired:
            retired += 1
        ts = str(entry.provenance.get("ts", ""))
        day = ts[:10] if len(ts) >= 10 else "unknown"
        by_day[day] = by_day.get(day, 0) + 1

    top = sorted(entries, key=lambda e: (-e.hits, e.key))[:10]
    reuse_total = sum(entry.hits for entry in entries)
    active = len(entries) - retired
    created = [item.created_at for item in queue_items if item.created_at]

    return {
        "queue_size": len(queue_items),
        "queue_by_kind": queue_by_kind,
        "queue_oldest": min(created) if created else None,
        "methodology_entries": len(entries),
        "methodology_active": active,
        "methodology_retired": retired,
        "methodology_by_kind": method_by_kind,
        "top_hits": [
            {
                "key": entry.key,
                "kind": entry.kind,
                "hits": entry.hits,
                "value": entry.value,
                "rationale": entry.rationale,
            }
            for entry in top
        ],
        "interventions": {
            "settled_total": len(entries),
            "reuse_total": reuse_total,
            "reuse_rate": round(reuse_total / active, 3) if active else 0.0,
            "by_day": dict(sorted(by_day.items())),
        },
    }


# ── 导出 / 导入（智能体上下文里的紧凑格式） ────────────────────────────


#: 写回格式说明（导出文件顶部打印一次，不占用每条队列项的行数）。
DECIDE_HELP = (
    "# 写回：把下面每条填成一块，存成文件后执行\n"
    "#   python -m sekaisync.agent_review submit --store <store> --file <判断文件>\n"
    "# 字段：id（每条第一行的 id）/ decision: accept|reject|replace\n"
    "#   value: <replace 必填；accept 不填则用 hint>   confidence: 0.0-1.0\n"
    "#   rationale: <一句中文理由，会沉淀进方法论>      generalize: pair|pattern|null\n"
    "#   pattern: <generalize=pattern 时必填，如 re:^N\\d{1,3}$ 或 prefix:PJ>"
)


def render_item(item: ReviewItem, index: int = 0) -> str:
    """把一条队列项渲染成紧凑文本（**5-8 行**，供智能体批量阅读）。

    行数 = 5 个固定字段行（含 stories）+ 最多 3 条证据句，正好落在 5-8 行区间。
    写回格式说明不在这里重复，见 :data:`DECIDE_HELP`。
    """
    head = f"## {index}. id={item.id} kind={item.kind}" if index else f"## id={item.id} kind={item.kind}"
    lines = [
        head,
        f"term: {item.term}  |  lang: {item.language}  |  hint: {item.chosen_hint or '（无）'}",
        f"candidates: {' | '.join(item.candidates) or '（无）'}"
        f"  |  channels: {'+'.join(item.channels) or '（未知）'}",
        f"reason: {item.reason or '（未说明）'}",
        f"stories: {', '.join(item.story_keys) or '（无）'}",
    ]
    for pos, sentence in enumerate(item.evidence, 1):
        lines.append(f"ev{pos}: {sentence}")
    return "\n".join(lines)


def export_for_agent(
    store_root: Path,
    out_path: Path,
    *,
    limit: int = 20,
) -> Path:
    """把队列导出为智能体友好的紧凑文本（含 id/term/lang/candidates/evidence/reason）。

    每条 5-8 行；判断一条不需要回读语料。返回写出文件的路径。
    """
    items = load_queue(store_root, limit=limit)
    stats = review_stats(store_root)
    header = [
        "# sekaisync 术语裁决队列",
        f"# 导出 {len(items)} / 共 {stats['queue_size']} 条；"
        f"方法论 {stats['methodology_entries']} 条（复用 {stats['interventions']['reuse_total']} 次）",
        "# 判断靠你自己：读 term/candidates/evidence 就能决定，不需要回读语料。",
        DECIDE_HELP.rstrip("\n"),
        "",
    ]
    blocks = [render_item(item, idx) for idx, item in enumerate(items, 1)]
    text = "\n".join(header) + "\n\n".join(blocks)
    if blocks:
        text += "\n"
    path = Path(out_path)
    _atomic_write_text(path, text)
    return path


_SCALAR_KEYS = {
    "id",
    "decision",
    "value",
    "rationale",
    "confidence",
    "generalize",
    "pattern",
    "agent",
    "session",
    "term",
    "language",
    "scope",
    "candidates",
    "reject_others",
}


def _parse_scalar(raw: str) -> Any:
    text = raw.strip()
    if text[:1] in ('"', "'") and text[-1:] == text[:1] and len(text) >= 2:
        return text[1:-1]
    lowered = text.lower()
    if lowered in ("", "~"):
        return ""
    if lowered in ("null", "none"):
        return None
    if lowered in ("true", "yes", "on"):
        return True
    if lowered in ("false", "no", "off"):
        return False
    if text.startswith("[") or text.startswith("{"):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    try:
        return float(text) if "." in text else int(text)
    except ValueError:
        return text


def parse_judgments_text(text: str) -> list[dict]:
    """解析智能体回写的紧凑格式，返回 judgment 列表。

    宽容度：接受每行一条 JSON、JSON 数组、多行 pretty-printed JSON 块、
    ``key: value`` 块（空行或下一个 ``id:`` 分隔）、``#`` 注释、``- `` 列表符号、
    Markdown 代码围栏、缩进续行（接在上一键后）、``---`` 分隔线。
    解析不了就抛 :class:`ReviewFormatError`，**文本里带出错行号**。
    """
    raw_lines = (text or "").splitlines()
    # 去掉代码围栏，但**保持行号不变**（错误行号要对着用户给的原文）。
    lines: list[tuple[int, str]] = []
    for number, line in enumerate(raw_lines, 1):
        if _CODE_FENCE_RE.match(line):
            lines.append((number, ""))
            continue
        lines.append((number, line))

    stripped = "\n".join(line for _, line in lines).strip()
    if not stripped:
        raise ReviewFormatError("line 1: 内容为空，没有可解析的判断")

    # ① 整体是一个 JSON 数组 / 对象（含 pretty-printed）
    if stripped[0] in "[{":
        payload = _try_json(stripped)
        if isinstance(payload, list):
            return _validate_judgments(payload, 1)
        if isinstance(payload, dict):
            if isinstance(payload.get("judgments"), list):
                return _validate_judgments(list(payload["judgments"]), 1)
            if payload.get("id"):
                return _validate_judgments([payload], 1)
        # 不是合法 JSON → 落到逐行/逐块解析

    # ② 逐行 / 逐块解析
    judgments: list[dict] = []
    block: dict[str, Any] = {}
    block_start = 0
    last_key: Optional[str] = None

    def flush() -> None:
        nonlocal block, block_start, last_key
        if block:
            judgments.append(block)
        block = {}
        block_start = 0
        last_key = None

    idx = 0
    while idx < len(lines):
        number, line = lines[idx]
        idx += 1
        content = line.rstrip()
        if not content.strip():
            continue
        if content.strip() in ("---", "==="):
            flush()
            continue
        if content.lstrip().startswith("#"):
            continue
        body = content.lstrip()
        # "- key: value" 这种列表写法：剥掉项目符号后当普通行继续处理。
        if body.startswith("- "):
            remainder = body[2:].lstrip()
            head = remainder.split(":", 1)[0].strip() if ":" in remainder else ""
            if head in _SCALAR_KEYS:
                body = remainder
            else:
                # 续行为列表项（例如 candidates: 下的 "- N25"）
                if last_key == "candidates":
                    existing = block.get("candidates")
                    value = _parse_scalar(remainder)
                    block["candidates"] = (existing if isinstance(existing, list) else []) + [value]
                continue

        if body.startswith("{"):
            # 可能是单行 JSON，也可能是多行 pretty-printed JSON 块（宽容处理）。
            chunk: list[str] = []
            payload = None
            start_line = number
            probe = idx - 1
            while probe < len(lines):
                chunk.append(lines[probe][1])
                payload = _try_json("\n".join(chunk).strip())
                probe += 1
                if isinstance(payload, dict):
                    break
            if not isinstance(payload, dict):
                raise ReviewFormatError(
                    f"line {start_line}: JSON 对象无法解析（缺少引号/逗号？）"
                )
            flush()
            judgments.append(payload)
            idx = probe
            continue

        if ":" in body:
            key, _, value = body.partition(":")
            key = key.strip()
            if key not in _SCALAR_KEYS:
                raise ReviewFormatError(
                    f"line {number}: 未知字段 {key!r}"
                    f"（可用字段：{', '.join(sorted(_SCALAR_KEYS))}）"
                )
            if key == "id" and block:
                flush()
            if not block:
                block_start = number
            block[key] = _parse_scalar(value)
            last_key = key
            continue
        # 既不是 key:value 也不是 JSON → 若是缩进续行就并入上一个值，否则报错
        if content[:1].isspace() and last_key and isinstance(block.get(last_key), str):
            block[last_key] = f"{block[last_key]} {content.strip()}".strip()
            continue
        raise ReviewFormatError(
            f"line {number}: 无法解析的行 {content.strip()[:60]!r}"
            f"（期望 'key: value' 或一行 JSON）"
        )
    flush()
    if not judgments:
        raise ReviewFormatError("line 1: 没有解析出任何判断")
    return _validate_judgments(judgments, block_start or 1)


def _try_json(text: str) -> Any:
    """尽力解析 JSON，失败返回 None（调用方决定报错还是降级）。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _validate_judgments(judgments: list[Any], line_hint: int) -> list[dict]:
    out: list[dict] = []
    for idx, judgment in enumerate(judgments, 1):
        if not isinstance(judgment, dict):
            raise ReviewFormatError(f"line {line_hint}: 第 {idx} 条不是对象")
        if not _sanitize_text(judgment.get("id")):
            raise ReviewFormatError(f"line {line_hint}: 第 {idx} 条缺少必填字段 id")
        decision = _sanitize_text(judgment.get("decision")).lower()
        if decision not in DECISIONS:
            raise ReviewFormatError(
                f"line {line_hint}: 第 {idx} 条 decision={decision!r} 非法（应为 accept/reject/replace）"
            )
        if decision == "replace" and not _sanitize_text(judgment.get("value")):
            raise ReviewFormatError(f"line {line_hint}: 第 {idx} 条 replace 必须给出 value")
        out.append(judgment)
    return out


def import_judgments_from_text(store_root: Path, text: str) -> dict:
    """解析智能体回写的紧凑格式（每行一条 JSON 或 YAML-ish ``key:value`` 块），
    调用 :func:`submit_judgments`。格式宽容；解析失败抛
    :class:`ReviewFormatError`，文本含**行号**。

    先整体解析、全部合法才提交 —— 不做半截写入，避免智能体写错一行毁掉一批。
    """
    judgments = parse_judgments_text(text)
    result = submit_judgments(store_root, judgments)
    result["parsed"] = len(judgments)
    return result


# ── CLI（供智能体直接 exec；不改 cli.py） ──────────────────────────────


def _force_utf8_stdio() -> None:
    """Windows 控制台默认 cp936 会在打印 ニーゴ 时炸掉，这里强制 utf-8。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _read_judgments_file(path: Path) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8")
    stripped = raw.strip()
    if stripped[:1] in ("[", "{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, list):
            return _validate_judgments(payload, 1)
        if isinstance(payload, dict):
            if isinstance(payload.get("judgments"), list):
                return _validate_judgments(list(payload["judgments"]), 1)
            return _validate_judgments([payload], 1)
    return parse_judgments_text(raw)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m sekaisync.agent_review",
        description="sekaisync 智能体干预环：读取待裁决队列 / 写回判断 / 查看方法论。",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_store(p: argparse.ArgumentParser) -> None:
        p.add_argument("--store", default="store", help="store 根目录（默认 store）")

    p_list = sub.add_parser("list", help="列出待裁决项（紧凑文本）")
    add_store(p_list)
    p_list.add_argument("--limit", type=int, default=0, help="最多几条（0=全部）")
    p_list.add_argument("--kind", default=None, help="只看某一类：conflict/pending/gate_failed")
    p_list.add_argument("--json", action="store_true", help="输出 JSON 而非紧凑文本")

    p_export = sub.add_parser("export", help="导出队列为紧凑文本文件")
    add_store(p_export)
    p_export.add_argument("--out", required=True, help="输出文件路径")
    p_export.add_argument("--limit", type=int, default=20, help="最多几条（默认 20）")

    p_submit = sub.add_parser("submit", help="写回判断（JSON 文件或紧凑文本）")
    add_store(p_submit)
    group = p_submit.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", help="判断文件（.json 或紧凑文本）")
    group.add_argument("--text", help="直接传紧凑文本")

    p_stats = sub.add_parser("stats", help="诊断统计")
    add_store(p_stats)

    p_method = sub.add_parser("methodology", help="查看方法论库")
    add_store(p_method)
    p_method.add_argument("--limit", type=int, default=0, help="最多几条（0=全部）")
    return parser


def _cmd_list(args: argparse.Namespace) -> int:
    store_root = Path(args.store)
    items = load_queue(store_root, limit=args.limit, kind=args.kind)
    if args.json:
        _print_json({"items": [item.to_dict() for item in items], "count": len(items)})
        return 0
    if not items:
        print("（队列为空：没有待智能体裁决的项）")
        return 0
    print(f"# {len(items)} 条待裁决")
    print()
    print("\n\n".join(render_item(item, idx) for idx, item in enumerate(items, 1)))
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    path = export_for_agent(Path(args.store), Path(args.out), limit=args.limit)
    print(str(path))
    return 0


def _cmd_submit(args: argparse.Namespace) -> int:
    store_root = Path(args.store)
    if args.text is not None:
        result = import_judgments_from_text(store_root, args.text)
    else:
        judgments = _read_judgments_file(Path(args.file))
        result = submit_judgments(store_root, judgments)
        result["parsed"] = len(judgments)
    _print_json(result)
    return 0 if not result.get("errors") else 1


def _cmd_stats(args: argparse.Namespace) -> int:
    _print_json(review_stats(Path(args.store)))
    return 0


def _cmd_methodology(args: argparse.Namespace) -> int:
    entries = load_methodology(Path(args.store))
    if args.limit and args.limit > 0:
        entries = entries[: args.limit]
    _print_json({"entries": [entry.to_dict() for entry in entries], "count": len(entries)})
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    _force_utf8_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers = {
        "list": _cmd_list,
        "export": _cmd_export,
        "submit": _cmd_submit,
        "stats": _cmd_stats,
        "methodology": _cmd_methodology,
    }
    try:
        return handlers[args.command](args)
    except ReviewFormatError as exc:
        print(f"解析失败：{exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"文件不存在：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
