"""P06 follow-up — the candidate prefilter must not change any result.

``web_search`` scores every page in Python because the ranking comes from
``normalize.best_match``, which SQL cannot reproduce (Astra D06 rejects
replacing it with ``LIKE``). The index in ``sekaisync/searchindex.py`` only
proposes candidates; the scorer still decides.

That is a *superset* claim, and a superset claim is exactly the kind that
degrades silently: a prefilter that drops one row returns a plausible, slightly
shorter list. So these tests attack the claim directly:

- every scorer tier is exercised against the index, including the two tiers
  that can match with **no shared trigram** (55 and 60) — the cases a naive
  trigram filter loses;
- the whole public entry point is compared against the scan, ids and order;
- staleness is verified in both directions, because a stale index that is
  trusted would answer from old data.
"""

import tempfile
import unittest
from pathlib import Path

from sekaisync import dbstore, searchindex
from sekaisync.models import WebPage
from sekaisync.normalize import matching_key, similarity_score
from sekaisync.webindex import save_web_pages, web_search


def _page(index, title, text="", source="altsource_ms", kind="page"):
    return WebPage(
        id=f"web:{source}:{index}",
        source=source,
        url=f"https://example.invalid/{index}",
        title=title,
        language="ja",
        kind=kind,
        text=text,
        crawled_at="2026-01-01T00:00:00+00:00",
        hash=f"h{index}",
    )


class PrefilterRecallTest(unittest.TestCase):
    """The candidate set must contain every row the scorer matches."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_searchindex_")
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "store"
        dbstore.initialize(self.store)
        self.pages = [
            _page(1, "星乃一歌", "星乃一歌はギターを弾く"),
            _page(2, "Hoshino Ichika", "Hoshino Ichika sings"),
            _page(3, "Leo/need", "Leo/need is a band"),
            _page(4, "セカイ", "セカイに行こう"),
            # The kana-fold pair: raw trigrams are disjoint, the folded keys are
            # equal. A raw-text index loses both of these.
            _page(5, "ｶﾀｶﾅ", "halfwidth kana"),
            _page(6, "カタカナ", "fullwidth kana"),
            # Long-vowel pair: "スターライト" and "すたらいと" fold to one key.
            _page(7, "スターライト", "long vowel mark"),
            _page(8, "すたらいと", "long vowel dropped"),
            # Tier 55: `aaaabca` vs `aacaaca` scores 55 with disjoint trigrams.
            # The row must be *short* for that tier to be reachable at all —
            # the tier's precondition is |len diff| <= 3 — so this page has no
            # body, exactly like a short identifier row in the real store.
            _page(9, "aacaaca", ""),
            _page(10, "cd ab", "word order swapped"),
        ]
        save_web_pages(self.store, "altsource_ms", self.pages)
        searchindex.build(self.store)

    def _truth(self, query):
        """(source, id) of every page the scorer matches, from raw text."""
        out = set()
        for page in self.pages:
            haystack = "\n".join([page.title, page.text[:20000]])
            if similarity_score(query, haystack) > 0:
                out.add((page.source, page.id))
        return out

    def _candidates(self, query):
        got = searchindex.candidates(self.store, query)
        if got is None:
            return None
        return {(s, i) for (s, i) in got if s == "altsource_ms"}

    def test_kana_fold_pair_is_found_from_either_spelling(self):
        """`カタカナ` and `かたかな` fold to one key; both spellings match both
        rows. Indexing raw text would return nothing for either."""
        for query in ("カタカナ", "かたかな", "ｶﾀｶﾅ"):
            with self.subTest(query=query):
                found = self._candidates(query)
                self.assertIsNotNone(found, "index declined a query it can answer")
                self.assertIn(("altsource_ms", "web:altsource_ms:6"), found)
        # And the public entry point agrees with the scorer.
        ids = {r["id"] for r in web_search(self.store, "かたかな", limit=8)}
        self.assertTrue(
            {"web:altsource_ms:5", "web:altsource_ms:6"} <= ids,
            f"folded-match rows missing from search: {ids}",
        )

    def test_long_vowel_pair_is_found_either_way(self):
        for query in ("スターライト", "すたーらいと", "すたらいと"):
            with self.subTest(query=query):
                found = self._candidates(query)
                self.assertIsNotNone(found, f"index declined {query!r}")
                self.assertIn(("altsource_ms", "web:altsource_ms:7"), found)

    def test_a_folded_key_below_three_chars_bypasses_to_the_scan(self):
        """`コーヒー` folds to `こひ` (2 chars), which has no trigram.

        The window cannot cover it either — tier 55 needs |len diff| <= 3 and a
        real row's key is longer — so the honest answer is to scan, not to
        return an empty candidate set.
        """
        self.assertEqual(matching_key("コーヒー"), "こひ")
        self.assertEqual(searchindex.bypass_reason("コーヒー"), "short_key")
        self.assertIsNone(searchindex.candidates(self.store, "コーヒー"))

    def test_candidates_are_a_superset_for_every_probe_query(self):
        """The core claim, checked per query rather than argued.

        Queries the index declines (the two documented bypasses) are checked
        separately: declining is a *scan*, which is trivially a superset, so
        they must not be quietly treated as "no candidates".
        """
        queries = ["星乃一歌", "Ichika", "Leo/need", "セカイ", "kana", "スターライト",
                   "すたらいと", "aaaabca", "N25", "zzz"]
        for query in queries:
            with self.subTest(query=query):
                truth = self._truth(query)
                if not truth:
                    continue
                found = self._candidates(query)
                self.assertIsNotNone(found, f"index declined {query!r} with {len(truth)} hits")
                missing = truth - found
                self.assertFalse(missing, f"prefilter dropped {sorted(missing)} for {query!r}")

    def test_bypassed_query_is_covered_by_the_scan(self):
        """`ab cd` matches by word set, so the index must decline it — and the
        public entry point must still find the row, via the scan."""
        truth = self._truth("ab cd")
        self.assertTrue(truth, "fixture no longer exercises the tier-60 gap")
        self.assertEqual(searchindex.bypass_reason("ab cd"), "short_latin_words")
        self.assertIsNone(searchindex.candidates(self.store, "ab cd"))
        self.assertTrue(truth <= {(r["source"], r["id"]) for r in web_search(self.store, "ab cd", limit=8)})

    def test_tier_55_matches_without_a_shared_trigram_are_kept(self):
        """`aaaabca` vs `aacaaca` scores 55 with disjoint trigram sets.

        This is the counterexample a naive trigram filter drops, so it is
        asserted as a property of the scorer first, then of the index.
        """
        query, haystack = "aaaabca", "aacaaca"
        self.assertEqual(similarity_score(query, haystack), 55)
        left = {matching_key(query)[i:i + 3] for i in range(len(matching_key(query)) - 2)}
        right = {matching_key(haystack)[i:i + 3] for i in range(len(matching_key(haystack)) - 2)}
        self.assertFalse(left & right, "fixture no longer exercises the gap")
        found = self._candidates(query)
        self.assertIsNotNone(found)
        self.assertIn(("altsource_ms", "web:altsource_ms:9"), found)

    def test_short_keys_and_short_words_decline_instead_of_guessing(self):
        """The two holes fall back to the full scan rather than to an empty set."""
        self.assertEqual(searchindex.bypass_reason("ミ"), "short_key")
        self.assertEqual(searchindex.bypass_reason("ミク"), "short_key")
        # Tier 60 is order-insensitive: "ab cd" matches "cd ab" with no shared
        # trigram, so an all-short-words query must not use the index.
        self.assertEqual(searchindex.bypass_reason("ab cd"), "short_latin_words")
        self.assertIsNone(searchindex.candidates(self.store, "ab cd"))
        # A 1-char query really does match: the bypass is load-bearing.
        self.assertTrue(
            any(r["id"].endswith(":4") for r in web_search(self.store, "セ", limit=8))
            or self._truth("セ")
        )

    def test_long_query_still_uses_the_index(self):
        self.assertIsNone(searchindex.bypass_reason("星乃一歌"))
        self.assertIsNotNone(searchindex.candidates(self.store, "星乃一歌"))


class PrefilterEquivalenceTest(unittest.TestCase):
    """The public entry point must return the same rows in the same order."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_searchindex_eq_")
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "store"
        dbstore.initialize(self.store)
        pages = []
        # Enough rows that the heap's limit and tie-breaks are actually
        # exercised, with several sharing a score.
        for i in range(60):
            title = "星乃一歌" if i % 7 == 0 else f"star song {i}"
            text = "セカイの歌" if i % 5 == 0 else f"body text number {i}"
            pages.append(_page(i, title=title, text=text))
        save_web_pages(self.store, "altsource_ms", pages)

    def _ids(self, query, **kwargs):
        return [r["id"] for r in web_search(self.store, query, limit=8, **kwargs)]

    def test_indexed_and_scanned_results_are_identical(self):
        queries = ["星乃一歌", "star song 3", "セカイ", "body text 42", "song", "ミ"]
        index_path = searchindex.index_path(self.store)
        for query in queries:
            with self.subTest(query=query):
                scanned = self._ids(query)  # no index yet
                searchindex.build(self.store)
                indexed = self._ids(query)
                self.assertEqual(indexed, scanned, f"{query!r} changed after indexing")
                # Remove for the next subtest so each compares against a scan.
                index_path.unlink()

    def test_a_stale_index_does_not_answer(self):
        searchindex.build(self.store)
        self.assertTrue(searchindex.is_current(self.store)[0])
        save_web_pages(self.store, "altsource_ms", [_page(999, "はじめて", "new body")])
        current, why = searchindex.is_current(self.store)
        self.assertFalse(current)
        self.assertIn("revision", why)
        # A stale index declines to propose anything; the caller scans.
        self.assertIsNone(searchindex.candidates(self.store, "はじめて"))

    def test_fingerprint_catches_content_edits_that_bypass_the_revision(self):
        """A raw UPDATE is out of contract, so it must still not be trusted."""
        searchindex.build(self.store)
        with dbstore.connect(self.store) as conn:
            conn.execute("UPDATE web_pages SET title='zzzzzzzz' WHERE id=?",
                         ("web:altsource_ms:1",))
            conn.commit()
        current, why = searchindex.is_current(self.store)
        self.assertFalse(current, "an edited page left the index looking current")
        self.assertIn("fingerprint", why)


class IndexWithoutFts5Test(unittest.TestCase):
    """FTS5 is a pure accelerator: its absence must not change behaviour."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_searchindex_nofts_")
        self.addCleanup(self._tmp.cleanup)
        self.store = Path(self._tmp.name) / "store"
        dbstore.initialize(self.store)
        save_web_pages(self.store, "altsource_ms", [
            _page(1, "星乃一歌", "星乃一歌の歌"),
            _page(2, "Miku", "hatsune miku"),
        ])

    def test_build_refuses_clearly_when_fts5_is_missing(self):
        from unittest.mock import patch

        with patch.object(searchindex, "fts5_trigram_available", return_value=False):
            with self.assertRaises(searchindex.IndexUnavailable):
                searchindex.build(self.store)

    def test_search_still_works_without_an_index(self):
        self.assertFalse(searchindex.index_path(self.store).exists())
        ids = [r["id"] for r in web_search(self.store, "星乃一歌", limit=8)]
        self.assertEqual(ids, ["web:altsource_ms:1"])

    def test_candidates_are_none_when_the_index_is_absent(self):
        self.assertIsNone(searchindex.candidates(self.store, "星乃一歌"))

    def test_candidates_are_none_when_an_old_format_index_is_present(self):
        searchindex.build(self.store)
        with dbstore.connect(self.store) as conn:
            pass
        import sqlite3

        path = searchindex.index_path(self.store)
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE meta SET value='0' WHERE key='index_format'")
        conn.commit()
        conn.close()
        self.assertIsNone(searchindex.candidates(self.store, "星乃一歌"))
        self.assertFalse(searchindex.is_current(self.store)[0])


if __name__ == "__main__":
    unittest.main()
