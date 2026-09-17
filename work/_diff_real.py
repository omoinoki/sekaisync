"""Differential gate: indexed search vs the unmodified scan, on the real store.

The index only proposes candidates; the scorer still decides. That is a
*superset* claim, so it is checked by running the identical public entry point
with and without the index and comparing ids AND order.

Usage: python -X utf8 work/_diff_real.py [query ...]
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sekaisync import searchindex
from sekaisync.webindex import web_search

STORE = Path("store")
QUERIES = sys.argv[1:] or ["星乃一歌", "N25", "セカイ", "Airi", "プロジェクトセカイ"]
index_path = searchindex.index_path(STORE)
hidden = index_path.with_suffix(".hidden")


def run(tag: str) -> dict:
    out = {}
    for query in QUERIES:
        start = time.perf_counter()
        rows = web_search(STORE, query, limit=8)
        elapsed = time.perf_counter() - start
        out[query] = [(r["id"], r["title"]) for r in rows]
        print(f"  {tag} {query!r:22} {len(rows)} rows {elapsed * 1000:9.0f}ms", flush=True)
    return out


print("--- indexed ---")
indexed = run("idx ")
index_path.rename(hidden)
try:
    print("--- scan (index removed) ---")
    scanned = run("scan")
finally:
    hidden.rename(index_path)

print()
mismatches = 0
for query in QUERIES:
    same = indexed[query] == scanned[query]
    mismatches += not same
    print(f"{query!r:24} identical={'YES' if same else 'NO <<<<<<'}")
    if not same:
        print("   indexed:", [x[0] for x in indexed[query]][:4])
        print("   scan   :", [x[0] for x in scanned[query]][:4])
print(f"\nmismatches: {mismatches} / {len(QUERIES)}")
