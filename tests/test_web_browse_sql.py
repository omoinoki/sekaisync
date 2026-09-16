"""P06 — SQL-side browse: equivalence and the two bugs it initially had.

`web_browse` filters, orders and limits in SQL instead of materialising every
page with its full text. Two mistakes in the first implementation are pinned
here because both produced *plausible* output rather than an exception:

1. `text_length` was computed from `page["text"]`, which the SQL projection
   deliberately omits — so every row reported length 0 while the snippet
   looked correct.
2. The `WHERE` placeholders are bound before the `ORDER BY` ones, so passing
   the sort parameters first silently matched nothing and returned an empty
   list for every filtered query.
"""

import tempfile
import unittest
from pathlib import Path

from sekaisync.models import WebPage
from sekaisync.webindex import (
    DEFAULT_SOURCE_PRIORITY,
    is_auxiliary_page,
    is_derived_page,
    matches_source_filter,
    normalize_source_id,
    page_category,
    save_web_pages,
    web_browse,
    web_search,
)
from sekaisync.sources import source_rank


class WebBrowseSqlTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_browse_")
        self.store = Path(self._tmp.name) / "store"
        save_web_pages(
            self.store,
            "altsource_ms",
            [
                WebPage(
                    id="web:ms:event_story:1:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/zh-cn/story/event/1/1/",
                    title="活动1-1",
                    language="zh_hans",
                    kind="event_story",
                    text="这是活动剧情正文，长度需要大于零。",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="e1",
                ),
                WebPage(
                    id="web:ms:home_line:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/zh-cn/home/1/",
                    title="主页语音",
                    language="zh_hans",
                    kind="home_line",
                    text="主页语音正文",
                    crawled_at="2026-08-12T00:00:00+00:00",
                    hash="h1",
                ),
            ],
        )
        save_web_pages(
            self.store,
            "altsource_sv",
            [
                WebPage(
                    id="web:sv:event_story:9:1",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/9/1.asset",
                    title="event 9-1",
                    language="ja",
                    kind="event_story",
                    text="イベントの本文です。",
                    crawled_at="2026-08-10T00:00:00+00:00",
                    hash="sv1",
                ),
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _oracle(self, source=None, language=None, kind=None, limit=50,
                include_overlay=False):
        """The pre-SQL algorithm: full scan, Python filter, two-pass sort."""
        from sekaisync.webindex import load_web_index

        pages = load_web_index(self.store)
        # load_web_index returns metadata only; re-read the text for parity.
        texts = {}
        from sekaisync import dbstore

        with dbstore.connect(self.store) as conn:
            conn.row_factory = __import__("sqlite3").Row
            for row in conn.execute("SELECT source, id, text FROM web_pages"):
                texts[(row["source"], row["id"])] = row["text"]

        pri = DEFAULT_SOURCE_PRIORITY
        wanted = normalize_source_id(source) if source else None
        items = []
        for page in pages:
            if is_derived_page(page) and not is_auxiliary_page(page):
                continue
            if is_auxiliary_page(page) and not include_overlay:
                continue
            if wanted and not matches_source_filter(page, wanted):
                continue
            if language and page.get("language") != language:
                continue
            if kind and page_category(page) != kind and page.get("kind") != kind:
                continue
            items.append(page)
        items.sort(key=lambda i: str(i.get("crawled_at", "")), reverse=True)
        items.sort(key=lambda i: source_rank(i.get("source", ""), pri))
        return items[:limit], texts

    def test_filtered_query_returns_rows_not_empty(self):
        """Regression: a WHERE/ORDER BY parameter mix-up returned nothing."""
        browsed = web_browse(self.store, kind="event_story", limit=10)
        # altsource_sv outranks altsource_ms in DEFAULT_SOURCE_PRIORITY, so the
        # sv row comes first regardless of crawl time.
        self.assertEqual(
            [item["id"] for item in browsed],
            ["web:sv:event_story:9:1", "web:ms:event_story:1:1"],
        )

    def test_text_length_is_populated_not_zero(self):
        """Regression: length was read from the excluded `text` column."""
        for item in web_browse(self.store, limit=10):
            self.assertGreater(
                item["text_length"],
                0,
                f"{item['id']} reported text_length=0; the SQL length() value "
                f"was not carried through",
            )

    def test_snippet_is_the_first_300_normalized_chars(self):
        item = next(
            i for i in web_browse(self.store, limit=10)
            if i["id"] == "web:ms:event_story:1:1"
        )
        self.assertIn("这是活动剧情正文", item["snippet"])

    def test_text_length_matches_full_body_length(self):
        with self._tmp_conn() as conn:
            expected = {
                row[0]: len(row[1])
                for row in conn.execute(
                    "SELECT id, text FROM web_pages WHERE source='altsource_ms'"
                )
            }
        for item in web_browse(self.store, source="altsource_ms", limit=10):
            self.assertEqual(item["text_length"], expected[item["id"]])

    def _tmp_conn(self):
        import contextlib

        from sekaisync import dbstore

        @contextlib.contextmanager
        def _cm():
            conn = __import__("sqlite3").connect(str(dbstore.db_path(self.store)))
            try:
                yield conn
            finally:
                conn.close()

        return _cm()

    def test_equivalence_with_python_oracle(self):
        """The SQL path must reproduce the full-scan algorithm exactly."""
        cases = [
            dict(),
            dict(limit=1),
            dict(limit=100),
            dict(source="altsource_ms"),
            dict(source="altsource_sv"),
            dict(language="zh_hans"),
            dict(language="ja"),
            dict(kind="event_story"),
            dict(kind="character_voice"),
            dict(source="altsource_ms", kind="event_story"),
            dict(source="altsource_sv", language="ja", kind="event_story"),
            dict(include_overlay=True),
        ]
        for case in cases:
            with self.subTest(case=case):
                browsed = web_browse(self.store, **case)
                expected, texts = self._oracle(**case)
                self.assertEqual(
                    [i["id"] for i in browsed],
                    [i["id"] for i in expected],
                    f"ordering/selection differs for {case}",
                )
                for got, want in zip(browsed, expected):
                    full = texts.get((want["source"], want["id"]), "")
                    self.assertEqual(got["text_length"], len(full))
                    self.assertEqual(
                        got["snippet"], " ".join(full[:300].split())
                    )

    def test_include_text_returns_real_bodies(self):
        """Regression: the SQL projection omits `text`, so include_text
        initially returned empty strings for every row."""
        items = web_browse(self.store, limit=10, include_text=True)
        self.assertTrue(items)
        for item in items:
            self.assertIn("text", item)
            self.assertTrue(
                item["text"],
                f"{item['id']} returned empty text with include_text=True",
            )
            self.assertEqual(len(item["text"]), item["text_length"])

    def test_text_absent_when_not_requested(self):
        """The body must not be shipped when the caller did not ask for it."""
        for item in web_browse(self.store, limit=10):
            self.assertNotIn(
                "text",
                item,
                "full body leaked into a metadata-only browse result",
            )

    def test_overlay_pages_excluded_by_default(self):
        save_web_pages(
            self.store,
            "altsource_ms_translation",
            [
                WebPage(
                    id="web:tr:event_story:1:1:ja",
                    source="altsource_ms_translation",
                    url="https://translation.example/event_1.json",
                    title="ja source",
                    language="ja",
                    kind="event_story",
                    text="原文",
                    crawled_at="2026-08-13T00:00:00+00:00",
                    hash="t1",
                    auxiliary=True,
                    overlay=True,
                )
            ],
        )
        default_ids = {i["id"] for i in web_browse(self.store, limit=50)}
        self.assertNotIn("web:tr:event_story:1:1:ja", default_ids)

        with_overlay = {
            i["id"]
            for i in web_browse(self.store, limit=50, include_overlay=True)
        }
        self.assertIn("web:tr:event_story:1:1:ja", with_overlay)


class WebSearchStreamingTest(unittest.TestCase):
    """P06 — search streams the scoring window instead of loading every body.

    The score still comes from the Python fuzzy matcher, so these tests pin
    that the *inputs* to scoring are unchanged (same 20000-char window) and
    that a fixed-size heap returns the same winners a full sort would.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_search_")
        self.store = Path(self._tmp.name) / "store"
        pages = []
        for index in range(1, 13):
            pages.append(
                WebPage(
                    id=f"web:ms:event_story:{index}:1",
                    source="altsource_ms",
                    url=f"https://pjsk.moe/zh-cn/story/event/{index}/1/",
                    title=f"活动 {index}",
                    language="zh_hans",
                    kind="event_story",
                    # Only some pages contain the target term.
                    text=("目标词出现了" if index % 4 == 0 else "无关内容") + "。" * 5,
                    crawled_at=f"2026-08-{index:02d}T00:00:00+00:00",
                    hash=f"h{index}",
                )
            )
        save_web_pages(self.store, "altsource_ms", pages)

    def tearDown(self):
        self._tmp.cleanup()

    def test_search_finds_matching_pages(self):
        results = web_search(self.store, "目标词", limit=10)
        ids = sorted(
            item["id"] for item in results
        )
        # Sorted lexically, so :12: precedes :4: — the point is the membership.
        self.assertEqual(
            ids,
            [
                "web:ms:event_story:12:1",
                "web:ms:event_story:4:1",
                "web:ms:event_story:8:1",
            ],
        )

    def test_text_length_populated_from_sql_not_zero(self):
        for item in web_search(self.store, "目标词", limit=10):
            self.assertGreater(item["text_length"], 0)

    def test_limit_bounds_the_result_not_the_candidates(self):
        """A small limit must still consider every candidate."""
        few = web_search(self.store, "目标词", limit=1)
        many = web_search(self.store, "目标词", limit=10)
        self.assertEqual(len(few), 1)
        # The single winner must also be present when more are returned.
        self.assertIn(few[0]["id"], [item["id"] for item in many])

    def test_top_k_keeps_the_best_not_the_first(self):
        """The heap must retain the *best* K, not the first K encountered.

        `heapq` is a min-heap, so keying it by the quality order makes the root
        the best entry and every replacement evicts a winner. Here a
        high-priority source's matches appear later in `source, seq` order, so
        an inverted heap shows up as the earlier low-priority rows surviving.
        """
        from sekaisync.webindex import DEFAULT_SOURCE_PRIORITY

        # altsource_sv outranks altsource_ms.
        high_priority = DEFAULT_SOURCE_PRIORITY[0]
        save_web_pages(
            self.store,
            high_priority,
            [
                WebPage(
                    id=f"web:high:wordings:{index}",
                    source=high_priority,
                    url=f"https://example.invalid/w{index}",
                    title=f"w{index}",
                    language="ja",
                    kind="wordings",
                    text="目标词出现在这里。",
                    crawled_at="2026-08-01T00:00:00+00:00",
                    hash=f"h{index}",
                )
                for index in range(6)
            ],
        )
        results = web_search(self.store, "目标词", limit=3)
        self.assertEqual(len(results), 3)
        self.assertTrue(
            all(item["source"] == high_priority for item in results),
            "top-K returned lower-priority rows because the heap kept the "
            f"first matches instead of the best: {[i['source'] for i in results]}",
        )

    def test_limit_one_returns_the_highest_priority_match(self):
        """With one slot, the heap must still end up holding the best match.

        Self-contained: it adds its own high-priority page rather than relying
        on another test having run first.
        """
        from sekaisync.webindex import DEFAULT_SOURCE_PRIORITY

        high_priority = DEFAULT_SOURCE_PRIORITY[0]
        save_web_pages(
            self.store,
            high_priority,
            [
                WebPage(
                    id="web:high:wordings:solo",
                    source=high_priority,
                    url="https://example.invalid/solo",
                    title="solo",
                    language="ja",
                    kind="wordings",
                    text="目标词出现在这里。",
                    crawled_at="2026-08-01T00:00:00+00:00",
                    hash="solo",
                )
            ],
        )
        results = web_search(self.store, "目标词", limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["source"], high_priority)

    def test_tied_scores_keep_store_order(self):
        """Ties must resolve like the stable sort the old code used.

        The real store is tie-heavy: 26 of 30 matches for a common query score
        identically, so the tiebreak decides the result. An inverted arrival
        tiebreak still returns plausible-looking rows — just different ones —
        which is why this is pinned explicitly.
        """
        ids = [f"web:tie:wordings:{index:02d}" for index in range(10)]
        save_web_pages(
            self.store,
            "altsource_sv",
            [
                WebPage(
                    id=page_id,
                    source="altsource_sv",
                    url=f"https://example.invalid/tie{index}",
                    title="same title",
                    language="ja",
                    kind="wordings",
                    text="目标词在这里",
                    crawled_at="2026-08-01T00:00:00+00:00",
                    hash=page_id,
                )
                for index, page_id in enumerate(ids)
            ],
        )
        results = web_search(self.store, "目标词", limit=4)
        self.assertEqual(
            [item["id"] for item in results],
            ids[:4],
            "tied rows came back out of store order",
        )

    def test_include_text_returns_bodies(self):
        results = web_search(self.store, "目标词", limit=3, include_text=True)
        self.assertTrue(results)
        for item in results:
            self.assertTrue(item["text"])
            self.assertEqual(len(item["text"]), item["text_length"])

    def test_text_absent_when_not_requested(self):
        for item in web_search(self.store, "目标词", limit=3):
            self.assertNotIn("text", item)

    def test_scoring_window_is_still_20000_chars(self):
        """The scorer must see the same window as before the SQL change."""
        from sekaisync import dbstore

        long_text = "前" * 19990 + "尾目标词"
        save_web_pages(
            self.store,
            "altsource_sv",
            [
                WebPage(
                    id="web:sv:event_story:99:1",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/99/1.asset",
                    title="长正文",
                    language="ja",
                    kind="event_story",
                    text=long_text,
                    crawled_at="2026-08-20T00:00:00+00:00",
                    hash="long",
                )
            ],
        )
        with dbstore.connect(self.store) as conn:
            starts = [
                row[0]
                for row in conn.execute(
                    "SELECT instr(text, '目标词') FROM web_pages WHERE id='web:sv:event_story:99:1'"
                )
            ]
        self.assertEqual(starts, [19992], "fixture not built as expected")

        # The term sits at index 19990, inside the 20000-char window, so the
        # streamed window must contain it.
        results = web_search(self.store, "目标词", limit=10)
        self.assertIn(
            "web:sv:event_story:99:1",
            [item["id"] for item in results],
            "the term is within the first 20000 chars but was not scored — the "
            "streamed window is not the baseline window",
        )


if __name__ == "__main__":
    unittest.main()
