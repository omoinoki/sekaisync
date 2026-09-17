"""Measure the indexed path on the real store, medians of several runs."""
import statistics, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from sekaisync import searchindex
from sekaisync.webindex import web_search

STORE = Path('store')
print('index current:', searchindex.is_current(STORE))
st = searchindex.stats(STORE)
print('index size:', round(st.get('bytes', 0)/1e6, 1), 'MB | rows:', st.get('rows'))
for q in ("星乃一歌", "Miku", "Leo/need", "セカイ", "Hoshino Ichika", "N25"):
    samples = []
    for _ in range(3):
        t = time.perf_counter(); rows = web_search(STORE, q, limit=8); samples.append(time.perf_counter()-t)
    print(f'  web_search({q!r}): median {statistics.median(samples)*1000:8.1f}ms  rows={len(rows)}  '
          f'cand={"scan" if searchindex.candidates(STORE,q) is None else len(searchindex.candidates(STORE,q))}')
    sys.stdout.flush()
