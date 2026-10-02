from __future__ import annotations

import unittest
from unittest.mock import patch

from scripts.prepare_scraper_p0_run import discovery_items


def row(number: int, story: str = "event:1:1", text: str = "body") -> dict:
    return {"id": f"span:{number}", "story_key": story,
            "source": {"text": text, "page_id": "p", "language": "zh_hans",
                       "start": number, "end": number + len(text), "complete": True},
            "targets": {}}


class DiscoveryCensusTests(unittest.TestCase):
    def items(self, rows):
        return discovery_items("a" * 64, {"source_language": "zh_hans", "windows": rows})

    def test_empty_scope(self):
        self.assertEqual(self.items([]), [])

    def test_preserves_every_window_and_order(self):
        rows = [row(1), row(2), row(3)]
        items = self.items(rows)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]._context["rows"], rows)

    def test_story_change_splits_packets(self):
        items = self.items([row(1), row(2, "event:1:2"), row(3, "event:1:2")])
        self.assertEqual([len(i._context["rows"]) for i in items], [1, 2])

    def test_budget_preserves_tail(self):
        with patch("scripts.prepare_scraper_p0_run.ap._DISCOVERY_CHARS", 8):
            items = self.items([row(1), row(2), row(3)])
        self.assertEqual([len(i._context["rows"]) for i in items], [2, 1])
        self.assertEqual(items[-1]._context["rows"][-1]["id"], "span:3")

    def test_large_single_window_not_dropped(self):
        with patch("scripts.prepare_scraper_p0_run.ap._DISCOVERY_CHARS", 2):
            items = self.items([row(1), row(2)])
        self.assertEqual([len(i._context["rows"]) for i in items], [1, 1])

    def test_identity_deterministic(self):
        self.assertEqual(self.items([row(1)])[0].id, self.items([row(1)])[0].id)

    def test_body_change_changes_identity(self):
        self.assertNotEqual(self.items([row(1)])[0].id, self.items([row(1, text="edit")])[0].id)

    def test_reconstructed_identity_matches_packet_function(self):
        from sekaisync import agent_packets as ap
        source_rows = [row(1), row(2)]
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id="a" * 64,
                       source_language="zh_hans", rows=source_rows)
        expected = ap._item("@discover:event:1:1:span:1", "zh_hans", [], "discovery", context, "old reason")
        self.assertEqual(self.items(source_rows)[0].id, expected.id)


if __name__ == "__main__":
    unittest.main()
