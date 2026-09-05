#!/usr/bin/env python
"""Refresh the generated data-baseline block in docs/BASELINE.md.

Run after every major data change (sync / crawl / cleanup):

    python scripts/refresh_baseline.py

Reads live store counters (no full JSON parsing — the SQLite store answers
in milliseconds) and rewrites the block between the GENERATED markers in
docs/BASELINE.md. Numbers outside the markers are hand-maintained prose and
left untouched. Stdlib only.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sekaisync import dbstore  # noqa: E402
from sekaisync.layout import db_path  # noqa: E402

BEGIN = "<!-- BEGIN GENERATED:baseline (refresh via scripts/refresh_baseline.py) -->"
END = "<!-- END GENERATED:baseline -->"

LANGS = ("ja", "en", "zh_hans", "ko", "zh_tw")


def status_payload(store_root: Path) -> dict:
    from sekaisync.core import SekaiSyncCore

    return SekaiSyncCore(store_root).status()


def build_block(store_root: Path, status: dict) -> str:
    counts = dbstore.count_rows(store_root)
    terms = {
        "total": counts["terms"],
        "official": status["terms"]["official"],
        "with_evidence": status["terms"]["with_evidence"],
        "five_lang": None,
        "per_lang": {},
    }
    # language coverage: names_json per language (fast enough for 10k rows)
    import json as _json

    per_lang = {lang: 0 for lang in LANGS}
    five = 0
    for (names_json,) in dbstore.connect(store_root).execute("SELECT names_json FROM terms"):
        names = _json.loads(names_json)
        present = [lang for lang in LANGS if names.get(lang)]
        for lang in present:
            per_lang[lang] += 1
        if len(present) == len(LANGS):
            five += 1
    terms["five_lang"] = five
    terms["per_lang"] = per_lang

    web = status["web"]["sources"]
    aux = status["web"]["auxiliary"]
    lines = [
        f"| Registry（跨服实体） | {status['master']['entities']:,} | 官方 master DB（管道 A） |",
        f"| Glossary（官方/本地化词条） | {status['master']['glossary_terms']:,} | 五服本地化官方词 |",
        f"| Terms（术语索引） | {terms['total']:,} | official 标记 {terms['official']}；带正文证据 {terms['with_evidence']:,} |",
        f"| 术语五语齐全 | {terms['five_lang']} | 单语言覆盖："
        + "、".join(f"{lang} {terms['per_lang'][lang]:,}" for lang in LANGS)
        + " |",
        f"| Web 正文页 | {counts['web_pages']:,} | 含辅助页（管道 B） |",
        "| ".join(["辅助翻译参考", f"{aux['count']:,}", "auxiliary 辅助页（翻译参考/overlay）"]) + " |",
        f"| 官方公告 | {status['news']['total']:,} | "
        + "、".join(f"{lang} {count}" for lang, count in sorted(status["news"].get("languages", {}).items()))
        + " |",
    ]
    return "\n".join(lines)


def main() -> int:
    store_root = ROOT / "store"
    if not db_path(store_root).exists():
        print("store has no sekaisync.db yet — run init/sync first", file=sys.stderr)
        return 1
    status = status_payload(store_root)
    generated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    block = (
        BEGIN,
        f"<!-- refreshed {generated_at} -->",
        "",
        "| 数据层 | 数量 | 说明 |",
        "| --- | --- | --- |",
        build_block(store_root, status),
        END,
    )
    baseline = ROOT / "docs" / "BASELINE.md"
    text = baseline.read_text(encoding="utf-8")
    start = text.find(BEGIN)
    end = text.find(END)
    if start >= 0 and end > start:
        new_text = text[:start] + "\n".join(block) + text[end + len(END):]
    else:
        anchor = "## 数量基线"
        i = text.find(anchor)
        if i < 0:
            print("BASELINE.md has no 数量基线 section", file=sys.stderr)
            return 1
        insert_at = text.find("\n", i) + 1
        new_text = text[:insert_at] + "\n" + "\n".join(block) + "\n" + text[insert_at:]
    baseline.write_text(new_text, encoding="utf-8")
    print(f"BASELINE.md refreshed ({generated_at})")
    print(json.dumps({k: v for k, v in dbstore.count_rows(store_root).items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
