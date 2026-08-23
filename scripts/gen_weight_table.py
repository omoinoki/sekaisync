#!/usr/bin/env python3
"""Generate a weight table for manual review.

Usage:
  python scripts/gen_weight_table.py [--top 200] [--out docs/WEIGHT_TABLE.md]
"""

import argparse
import json
from pathlib import Path
from collections import Counter

from sekaisync.termindex import load_terms, TAG_VOCAB


def language_badges(names: dict) -> str:
    langs = sorted(names.keys())
    # Mark JP-only
    if set(langs) == {"ja"}:
        return "ja-only"
    return ",".join(langs)


def evidence_sample(term) -> str:
    if not term.evidence:
        return ""
    s = str(term.evidence[0].get("sentence", ""))
    s = s.replace("|", "\\|").replace("\n", " ")
    return s[:60]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--top", type=int, default=200, help="Rows in main table")
    parser.add_argument("--out", type=Path, default=Path("docs/WEIGHT_TABLE.md"))
    parser.add_argument("--store", type=Path, default=Path("store"))
    parser.add_argument(
        "--exclude-ja-only",
        action="store_true",
        help="Only rank terms that carry at least one non-ja name",
    )
    args = parser.parse_args()

    terms = load_terms(args.store / "kb" / "terms.json")
    if args.exclude_ja_only:
        terms = [t for t in terms if len(t.names) > 1]
    terms_sorted = sorted(terms, key=lambda t: t.weight, reverse=True)

    only_ja = [t for t in terms if set(t.names.keys()) == {"ja"}]
    multi = [t for t in terms if len(t.names) > 1]

    # Tag distribution
    tag_counter = Counter()
    for t in terms:
        for tag in t.tags:
            tag_counter[tag] += 1

    # Multi-language coverage
    lang_counter = Counter()
    for t in terms:
        for lang in t.names.keys():
            lang_counter[lang] += 1

    lines: list[str] = []
    lines.append("# 用语权重总表")
    lines.append("")
    lines.append(f"> 生成自 `store/kb/terms.json`（`--all --local --source-language ja` 全量抽取）。")
    lines.append(f"> `weight = log1p(occurrences) * trust_factor * tag_prior`")
    lines.append(f"> `trust: A 1.3 / B 1.1 / C 1.0 / D 0.9`，`tag_prior: person 1.2 / event 1.15 / product 1.05 / location 1.0 / organization 1.0 / other 0.7`（重叠取 max）。")
    lines.append("")
    lines.append("## 概览")
    lines.append("")
    lines.append(f"- 全量用语：{len(terms)} 条（官方 {sum(1 for t in terms if t.official)}，带证据 {sum(1 for t in terms if t.evidence)}）")
    lines.append(f"- 仅日语（无他服译名，含 JP 领先期新增名词）：{len(only_ja)} 条（{len(only_ja)/len(terms)*100:.1f}%）")
    lines.append(f"- 多语对照：{len(multi)} 条")
    langs_str = ", ".join(f"{lang} {lang_counter[lang]}" for lang in ["ja","zh_hans","en","zh_tw","ko"])
    lines.append(f"- 各语种覆盖：{langs_str}")
    tags_str = ", ".join(f"{tag} {tag_counter[tag]}" for tag in sorted(TAG_VOCAB))
    lines.append(f"- 各 tag 计数：{tags_str}")
    lines.append(f"- 权重范围：{min(t.weight for t in terms):.2f} – {max(t.weight for t in terms):.2f}，中位 {sorted(t.weight for t in terms)[len(terms)//2]:.2f}")
    lines.append("")
    lines.append("**注意**：`only-ja` 不等于错误——日服显著领先，许多新活动/新卡面名词确实只有日语。需人工判断是否待他服更新后补译。")
    lines.append("")

    # Per-tag top sample
    lines.append("## 各 Tag 权重前 5")
    lines.append("")
    lines.append("| tag | canonical | weight | occ | langs | 对照（zh_hans / en） |")
    lines.append("|-----|-----------|--------|-----|-------|----------------------|")
    for tag in sorted(TAG_VOCAB):
        top = sorted([t for t in terms if tag in t.tags], key=lambda x: x.weight, reverse=True)[:5]
        for t in top:
            zh = t.names.get("zh_hans","")
            en = t.names.get("en","")
            langs = language_badges(t.names)
            lines.append(f"| {tag} | {t.canonical} | {t.weight:.2f} | {t.occurrences} | {langs} | {zh} / {en} |")
    lines.append("")

    # Only-ja top 20
    lines.append("## 仅日语（only-ja）权重前 20")
    lines.append("")
    lines.append("| # | canonical | weight | occ | tags | evidence |")
    lines.append("|---|-----------|--------|-----|------|----------|")
    for i, t in enumerate(sorted(only_ja, key=lambda x: x.weight, reverse=True)[:20], 1):
        ev = evidence_sample(t)
        lines.append(f"| {i} | {t.canonical} | {t.weight:.2f} | {t.occurrences} | {','.join(t.tags)} | {ev} |")
    lines.append("")

    # Main weight table
    lines.append(f"## 全量权重表 Top {args.top}")
    lines.append("")
    lines.append("| # | canonical | weight | occ | tags | trust | langs | zh_hans | en | evidence |")
    lines.append("|---|-----------|--------|-----|------|-------|-------|---------|----|----|")
    for i, t in enumerate(terms_sorted[:args.top], 1):
        tags = ",".join(t.tags)
        langs = language_badges(t.names)
        zh = (t.names.get("zh_hans","") or "").replace("|","\\|")[:20]
        en = (t.names.get("en","") or "").replace("|","\\|")[:20]
        ev = evidence_sample(t)
        lines.append(f"| {i} | {t.canonical} | {t.weight:.2f} | {t.occurrences} | {tags} | {t.trust} | {langs} | {zh} | {en} | {ev} |")
    lines.append("")

    # Noise candidates: high occ but other tag
    lines.append("## 待复核：高频 other（可能漏贴 tag）")
    lines.append("")
    lines.append("| canonical | weight | occ | tags | evidence |")
    lines.append("|-----------|--------|-----|------|----------|")
    candidates = [t for t in terms if t.tags == ["other"] and t.occurrences >= 20]
    candidates.sort(key=lambda x: x.weight, reverse=True)
    for t in candidates[:30]:
        ev = evidence_sample(t)
        lines.append(f"| {t.canonical} | {t.weight:.2f} | {t.occurrences} | {','.join(t.tags)} | {ev} |")
    lines.append("")

    out = args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"Wrote {out} ({len(lines)} lines)")


if __name__ == "__main__":
    main()
