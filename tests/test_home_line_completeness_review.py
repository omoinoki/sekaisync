"""Independent home-line field preservation and incremental-refresh review."""
import unittest

from sekaisync import agent_packets as ap, crawler, line_alignment, span_subjects, termindex
from sekaisync.webindex import web_page_to_dict


class HomeLineCompletenessReviewTests(unittest.TestCase):
    def setUp(self):
        from tests.test_home_line_completeness import HomeLineCompletenessTests
        self.fixture = HomeLineCompletenessTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def records(self, first="Primary content", second="Response content", identifier=7):
        return [dict(id=identifier, displayPhrase=first, displayPhrase2=second)]

    def collect(self, backend, records, known=None, remaining=None):
        return self.fixture.collect(backend, records, known, remaining)

    def bodies(self, text):
        return [line_alignment.strip_speaker_label(line) for line in text.splitlines()]

    def test_page_budget_counts_records_not_nonempty_fields(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                records = [dict(id=6, displayPhrase="", displayPhrase2="")]
                records += self.records() + self.records("Later primary", "Later response", 8)
                pages, remaining = self.collect(backend, records, remaining=1)
                self.assertEqual(remaining, 0)
                self.assertEqual(len(pages), 1)
                self.assertEqual(self.bodies(pages[0].text), ["Primary content", "Response content"])
                self.assertTrue(pages[0].id.endswith(":home_line:7"))

    def test_response_only_fields_and_duplicate_values_do_not_disappear(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                records = self.records("", "Response alone", 7)
                records += self.records("Repeat", "Repeat", 8) + self.records("Repeat", "Repeat", 9)
                pages, remaining = self.collect(backend, records, remaining=3)
                self.assertEqual(remaining, 0)
                self.assertEqual([self.bodies(page.text) for page in pages], [["Response alone"], ["Repeat", "Repeat"], ["Repeat", "Repeat"]])
                self.assertEqual(len({page.id for page in pages}), 3)
                self.assertEqual(pages[1].hash, pages[2].hash)

    def test_same_length_secondary_change_refreshes_exact_original_record_id(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                original = self.collect(backend, self.records("Primary", "Alpha"))[0][0]
                known = {original.id, crawler._home_line_version_key(original.id, original.text)}
                pages, _ = self.collect(backend, self.records("Primary", "Omega"), known)
                self.assertEqual(len(pages), 1)
                refreshed = pages[0]
                self.assertEqual(refreshed.id, original.id)
                self.assertEqual(len(refreshed.text), len(original.text))
                self.assertNotEqual(refreshed.hash, original.hash)
                self.assertNotEqual(crawler._home_line_version_key(refreshed.id, refreshed.text),
                                    crawler._home_line_version_key(original.id, original.text))
                self.assertNotIn("text-sha256", refreshed.id)
                current = {refreshed.id: dict(kind="home_line", text=refreshed.text)}
                known = {refreshed.id} | crawler._known_home_line_versions(current, {refreshed.id})
                self.assertEqual(self.collect(backend, self.records("Primary", "Omega"), known)[0], [])

    def test_secondary_removal_refreshes_body_but_preserves_id(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                original = self.collect(backend, self.records())[0][0]
                known = {original.id, crawler._home_line_version_key(original.id, original.text)}
                pages, _ = self.collect(backend, self.records("Primary content", None), known)
                self.assertEqual(len(pages), 1)
                self.assertEqual(pages[0].id, original.id)
                self.assertEqual(self.bodies(pages[0].text), ["Primary content"])
                self.assertNotEqual(pages[0].hash, original.hash)

    def test_id_only_legacy_cache_refreshes_once_without_changing_identity(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                original = self.collect(backend, self.records())[0][0]
                pages, _ = self.collect(backend, self.records(), {original.id})
                self.assertEqual([page.id for page in pages], [original.id])
                known = {original.id, crawler._home_line_version_key(original.id, pages[0].text)}
                self.assertEqual(self.collect(backend, self.records(), known)[0], [])

    def test_normal_two_fields_create_two_independent_default_source_windows(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                page = web_page_to_dict(self.collect(backend, self.records())[0][0])
                groups = termindex.group_pages_by_story([page])
                rows = ap._scope_windows(groups, list(groups), "en", [])
                self.assertEqual(len(rows), 2)
                self.assertEqual([self.bodies(row["source"]["text"])[0] for row in rows], ["Primary content", "Response content"])
                self.assertTrue(all(not ap._body_term_segments(row["source"]["text"], "content Response",
                                                              row["source"]["start"]) for row in rows))

    def test_real_colon_phrases_cannot_merge_two_home_line_fields_into_one_subject_window(self):
        cases = [
            (2140129, "Operation: All Smiles is underway!", "Anybody who wants a smile on their face, come our way\u266a"),
            (2210234, "Operation: All Smiles is underway!", "Anybody who wants a smile on their face, come our way\u266a"),
            (2210237, "Next stop: Smile Town!", "Go, go, go\u266a"),
            (2220254, "Next stop: Smile Town!", "Go, go, go\u266a"),
        ]
        for backend in ("ms", "sv"):
            for identifier, first, second in cases:
                with self.subTest(backend=backend, identifier=identifier):
                    page = web_page_to_dict(self.collect(backend, self.records(first, second, identifier))[0][0])
                    groups = termindex.group_pages_by_story([page])
                    rows = ap._scope_windows(groups, list(groups), "en", [])
                    self.assertEqual(len(rows), 2, "Actual primary/response fields must not be one contiguous source subject")
                    self.assertEqual([self.bodies(row["source"]["text"])[0] for row in rows], [first, second])
                    self.assertTrue(all(not (first in row["source"]["text"] and second in row["source"]["text"]) for row in rows))

    def test_field_labels_are_metadata_but_real_primary_colon_term_heads_remain_source_words(self):
        for backend in ("ms", "sv"):
            with self.subTest(backend=backend):
                first = "Operation: All Smiles is underway!"
                page = web_page_to_dict(self.collect(backend, self.records(first, "A separate response"))[0][0])
                groups = termindex.group_pages_by_story([page])
                rows = ap._scope_windows(groups, list(groups), "en", [])
                view = rows[0]["source"]
                term = "Operation: All Smiles"
                segments = ap._body_term_segments(view["text"], term, view["start"])
                self.assertEqual(len(segments), 1)
                subject = span_subjects._literal(view, rows[0]["story_key"], term, segments[0])
                self.assertEqual(subject["canonical"], term)
                self.assertEqual(subject["source"]["segments"][0]["exact"], term)
                self.assertEqual(ap._body_term_segments(view["text"], "displayPhrase", view["start"]), [])


if __name__ == "__main__":
    unittest.main()
