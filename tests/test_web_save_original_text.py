"""Preserve original text when updating an existing page."""

import tempfile
import unittest
from pathlib import Path

from sekaisync.models import WebPage
from sekaisync.webindex import flatten_web_pages, save_web_pages


class SaveOriginalTextTest(unittest.TestCase):
    def test_second_save_preserves_original_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "store"
            page = WebPage(
                id="web:altsource_ms:event_story:1:1:zh",
                source="altsource_ms",
                url="https://pjsk.moe/zh-cn/story/event/1/1/",
                title="Translation",
                language="zh_hans",
                kind="event_story",
                text="日本語の本文",
                crawled_at="2026-09-17T00:00:00Z",
                hash="original",
            )
            save_web_pages(root, page.source, [page], write_categories=False,
                           rewrite_index=False)
            existing = {item["id"]: item for item in flatten_web_pages(root)}
            original = "  日本語の本文\x00\n"
            existing[page.id]["original_text"] = original
            page.text = "[未翻译]"
            page.untranslated = True
            page.original_text_hash = "original-hash"

            save_web_pages(root, page.source, [page], existing=existing,
                           write_categories=False, rewrite_index=False)

            saved = {item["id"]: item for item in flatten_web_pages(root)}[page.id]
            self.assertEqual(saved.get("original_text"), original)
            self.assertEqual(saved["original_text_hash"], "original-hash")
            self.assertEqual(saved["text"], "[未翻译]")


if __name__ == "__main__":
    unittest.main()
