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

    def test_metadata_rows_keep_source_type_and_instance(self):
        """Provenance columns survive the metadata projection (Astra verifier:
        recomputing trust from metadata must match the stored value)."""
        from sekaisync import dbstore

        page = WebPage(
            id="web:custom:event_story:2:1",
            source="custom_sv",
            url="https://pjsk.moe/zh-cn/story/event/2/1/",
            title="自定义实例",
            language="zh_hans",
            kind="event_story",
            text="实例正文",
            crawled_at="2026-08-13T00:00:00+00:00",
            hash="c1",
            source_type="sekai_viewer",
            instance="custom_sv",
        )
        save_web_pages(self.store, "custom_sv", [page])
        row = {item["id"]: item for item in dbstore.load_web_index_rows(self.store)}[page.id]
        self.assertEqual(row["source_type"], "sekai_viewer")
        self.assertEqual(row["instance"], "custom_sv")
        self.assertEqual(row["trust"], "B")
        from sekaisync.trust import trust_for_page

        self.assertEqual(row["trust"], trust_for_page({
            "source": "custom_sv", "kind": "event_story",
            "source_type": "sekai_viewer", "auxiliary": False,
            "overlay": False, "derived": False,
        }))

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


class FilterValueResolutionTest(unittest.TestCase):
    """Unfiltered browse must not pay for the filter-value enumeration.

    `_sql_filter_values` turns a *filter* into a set of stored values by
    evaluating the original Python rules over the store's distinct dimension
    tuples. With no filter there is nothing to resolve, yet the enumeration
    still ran — a full 752k-row scan (9.6s warm on the real store). The skip is
    equivalent by construction, since every `resolved[...]` write sits inside a
    `wanted_source`/`kind` guard; this pins it so the scan cannot come back.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_filter_values_")
        self.store = Path(self._tmp.name) / "store"
        save_web_pages(
            self.store,
            "altsource_ms",
            [
                WebPage(
                    id="web:altsource_ms:event_story:1:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/ja-jp/story/event/1/1/",
                    title="活動1-1",
                    language="ja",
                    kind="event_story",
                    text="本文",
                    crawled_at="2026-01-01T00:00:00+00:00",
                    hash="a",
                ),
            ],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def test_no_filter_does_not_enumerate_distinct_values(self):
        from unittest.mock import patch

        from sekaisync import webindex

        with patch.object(
            webindex.dbstore, "connect",
            side_effect=AssertionError("enumerated distinct values with no filter"),
        ):
            self.assertEqual(
                webindex._sql_filter_values(self.store, wanted_source=None, kind=None), {}
            )

    def test_a_filter_still_resolves_values(self):
        from sekaisync.webindex import _sql_filter_values

        resolved = _sql_filter_values(
            self.store, wanted_source="altsource_ms", kind="event_story"
        )
        self.assertEqual(resolved.get("source_ids"), ["altsource_ms"])
        self.assertIn("event_story", resolved.get("kinds") or [])


class BrowseIndexedOrderTest(unittest.TestCase):
    """P06 follow-up — the browse ORDER BY must not sort the whole table.

    The remaining cost of ``web_browse(limit=20)`` after the SQL pushdown was
    the sort: ``ORDER BY <priority CASE>, crawled_at DESC, source, seq`` cannot
    be answered from any index, so SQLite materialised and sorted every
    matching row (measured 8-9.5s median on the 752k-row real store).

    ``idx_pages_browse`` makes each ``(source, aux_flag, derived_flag)``
    partition readable in final order, so the winners are a merge of per-arm
    top-K probes. These tests pin both halves of that claim:

    1. both paths (index present / dropped) return byte-identical rows, so the
       index is an accelerator and never a semantics change — including the
       all-ties cases where the tiebreak is the only thing deciding the order;
    2. the store written before the index existed still gets it, and the fast
       path is *actually* used when the index is there (a silently-still-slow
       path would otherwise pass every equivalence test).
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_browse_idx_")
        self.store = Path(self._tmp.name) / "store"
        # Tie-heavy by construction: same crawled_at and same source, so only
        # the `seq` tiebreak orders these rows.
        pages = [
            WebPage(
                id=f"web:ms:wordings:{index:02d}",
                source="altsource_ms",
                url=f"https://pjsk.moe/ja-jp/wordings/{index}",
                title=f"wordings {index}",
                language="ja",
                kind="wordings",
                text=f"本文 {index}",
                crawled_at="2026-08-01T00:00:00+00:00",
                hash=f"w{index}",
            )
            for index in range(8)
        ]
        pages.append(
            WebPage(
                id="web:sv:event_story:1:1",
                source="altsource_sv",
                url="https://storage.sekai.best/event_story/1/1.asset",
                title="event 1-1",
                language="ja",
                kind="event_story",
                text="イベント本文",
                crawled_at="2026-08-02T00:00:00+00:00",
                hash="e1",
            )
        )
        save_web_pages(self.store, "altsource_ms", pages[:8])
        save_web_pages(self.store, "altsource_sv", [pages[8]])

    def tearDown(self):
        self._tmp.cleanup()

    def _set_index(self, present: bool) -> None:
        from sekaisync import dbstore

        with dbstore.connect(self.store) as conn:
            if present:
                conn.execute(f"CREATE INDEX IF NOT EXISTS {dbstore.BROWSE_INDEX} ON web_pages(source, crawled_at DESC, seq, aux_flag, derived_flag, kind, language)")
            else:
                conn.execute(f"DROP INDEX IF EXISTS {dbstore.BROWSE_INDEX}")
            conn.commit()

    def test_schema_declares_the_browse_index(self):
        """New stores must get the index from the schema, not from a test.

        ``initialize`` / ``initialize_new_store`` both run ``_SCHEMA`` with
        ``executescript``, so declaring the index there is what makes every
        newly created store (and the ``source_migrate`` bootstrap) carry it.
        A store that predates the index keeps the fallback statement and can
        be upgraded with the same idempotent DDL.
        """
        from sekaisync import dbstore

        self.assertIn(
            f"CREATE INDEX IF NOT EXISTS {dbstore.BROWSE_INDEX} ON web_pages",
            dbstore._SCHEMA,
            "the browse index is not in the schema, so no new store gets it",
        )

    def test_a_freshly_initialized_store_carries_the_index(self):
        """`sqlite_master` on a new store is the end-to-end version of the above."""
        from sekaisync import dbstore

        fresh = Path(self._tmp.name) / "fresh_store"
        dbstore.initialize(fresh)
        with dbstore.connect(fresh) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='web_pages'"
                )
            }
        self.assertIn(dbstore.BROWSE_INDEX, names)

    def test_fast_path_and_fallback_return_identical_rows(self):
        """Both orderings must produce the same ids in the same order.

        Dropping the index exercises the pre-index statement; the fallback is
        the oracle here because it is the shape the contract test above still
        pins end-to-end against the Python implementation.
        """
        from sekaisync import dbstore
        from sekaisync.webindex import DEFAULT_SOURCE_PRIORITY

        cases = [
            dict(limit=3),
            dict(limit=50),
            dict(limit=1),
            dict(limit=50, language="ja"),
            dict(limit=50, kind="wordings"),
            dict(limit=50, source="altsource_ms"),
            dict(limit=50, source_priority=("altsource_ms", "altsource_sv")),
            dict(limit=50, include_overlay=True),
        ]
        self._set_index(True)
        with dbstore.connect(self.store) as conn:
            fast = {
                repr(sorted(case.items())): dbstore.browse_web_rows(
                    self.store,
                    source_ids=None,
                    language=case.get("language"),
                    kinds=None,
                    limit=case["limit"],
                    include_overlay=case.get("include_overlay", False),
                    source_priority=case.get("source_priority", DEFAULT_SOURCE_PRIORITY),
                    conn=conn,
                )
                for case in cases
            }
        self._set_index(False)
        with dbstore.connect(self.store) as conn:
            fallback = {
                repr(sorted(case.items())): dbstore.browse_web_rows(
                    self.store,
                    source_ids=None,
                    language=case.get("language"),
                    kinds=None,
                    limit=case["limit"],
                    include_overlay=case.get("include_overlay", False),
                    source_priority=case.get("source_priority", DEFAULT_SOURCE_PRIORITY),
                    conn=conn,
                )
                for case in cases
            }
        self._set_index(True)
        for key in fast:
            self.assertEqual(
                [(r["source"], r["id"]) for r in fast[key]],
                [(r["source"], r["id"]) for r in fallback[key]],
                f"indexed and fallback ordering disagree for {key}",
            )

    def test_tiebreak_order_matches_with_and_without_the_index(self):
        """Identical ``crawled_at`` everywhere: `seq` alone decides the order."""
        from sekaisync import dbstore

        self._set_index(True)
        indexed = [
            item["id"] for item in web_browse(self.store, source="altsource_ms", limit=8)
        ]
        self._set_index(False)
        plain = [
            item["id"] for item in web_browse(self.store, source="altsource_ms", limit=8)
        ]
        self._set_index(True)
        self.assertEqual(indexed, plain)
        self.assertEqual(
            indexed,
            [f"web:ms:wordings:{index:02d}" for index in range(8)],
            "all-ties rows did not come back in insertion (`seq`) order",
        )

    def test_fast_path_is_used_when_the_index_exists(self):
        """The accelerator must actually engage, not just be equivalent.

        Asserted on the executed SQL rather than on wall-clock time: the fast
        path issues one ordered probe per partition, the fallback issues the
        single sorting statement. ``set_trace_callback`` reports statements
        after parameter binding, so the probes appear with their literals.
        """
        from sekaisync import dbstore

        self._set_index(True)
        statements: list[str] = []
        with dbstore.connect(self.store) as conn:
            conn.set_trace_callback(statements.append)
            try:
                dbstore.browse_web_rows(
                    self.store, limit=2, source_priority=("altsource_sv", "altsource_ms"),
                    conn=conn,
                )
            finally:
                conn.set_trace_callback(None)
        joined = "\n".join(statements)
        self.assertIn(
            f"INDEXED BY {dbstore.BROWSE_INDEX}",
            joined,
            "the browse index exists but the query never used it",
        )
        # One ordered probe per partition. `union all` merges them inside a
        # single statement, so count the arms rather than the statements.
        arm_pattern = f"INDEXED BY {dbstore.BROWSE_INDEX}"
        arms = [
            statement for statement in statements
            if arm_pattern in statement
            and "ORDER BY crawled_at DESC, seq" in statement
        ]
        self.assertEqual(
            sum(statement.count(arm_pattern) for statement in arms),
            2,
            "expected one ordered index probe per partition (two sources), "
            f"so the winners were sorted rather than walked: {statements}",
        )

    def test_large_tie_heavy_store_answers_from_the_index(self):
        """A store far larger than the limit must not sort every row.

        The trace shows the fast path reading K rows per partition and then
        hydrating only those keys, never the whole store. The old statement
        read (and sorted) every matching row, so a store-wide read showing up
        in the trace is the regression this test exists to catch.
        """
        from sekaisync import dbstore

        pages = [
            WebPage(
                id=f"web:bulk:wordings:{index:05d}",
                source="altsource_ms",
                url=f"https://example.invalid/bulk/{index}",
                title="bulk",
                language="ja",
                kind="wordings",
                text="本文",
                crawled_at="2026-08-01T00:00:00+00:00",
                hash=f"b{index}",
            )
            for index in range(4000)
        ]
        save_web_pages(self.store, "altsource_ms", pages)
        self._set_index(True)
        statements: list[str] = []
        with dbstore.connect(self.store) as conn:
            conn.set_trace_callback(statements.append)
            try:
                rows = dbstore.browse_web_rows(
                    self.store, limit=20, source_priority=("altsource_ms",),
                    conn=conn,
                )
            finally:
                conn.set_trace_callback(None)
        self.assertEqual(len(rows), 20)
        ordered_probe = [
            statement for statement in statements
            if f"INDEXED BY {dbstore.BROWSE_INDEX}" in statement
            and "ORDER BY crawled_at DESC, seq" in statement
            and "LIMIT 20" in statement
        ]
        self.assertTrue(
            ordered_probe,
            "no ordered index probe was issued; the winners were sorted, not "
            f"walked: {statements}",
        )
        # The sort key is walked from the index, so no statement may order the
        # whole table: the only ORDER BY left is the merge over the 20-row
        # arms.
        sorting = [
            statement for statement in statements
            if "ORDER BY" in statement and "UNION ALL" not in statement
            and "ORDER BY crawled_at DESC, seq" not in statement
        ]
        self.assertEqual(
            sorting,
            [],
            f"a table-wide ORDER BY survived alongside the probes: {sorting}",
        )


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
