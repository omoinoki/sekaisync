import hashlib
import tempfile
import unittest
from pathlib import Path

from sekaisync.models import WebPage
from sekaisync.postprocess import mark_untranslated_pages
from sekaisync.webindex import flatten_web_pages, rebuild_web_index, save_web_pages


class PostprocessTest(unittest.TestCase):
    def test_zh_hant_identical_text_is_replaced_with_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_sv",
                [
                    WebPage(
                        id="web:altsource_sv:jp:event_story:1:1",
                        source="altsource_sv",
                        url="https://storage.sekai.best/event_story/1/1.asset",
                        title="jp",
                        language="ja",
                        kind="event_story",
                        text="日本語の本文",
                        crawled_at="2026-08-12T00:00:00+00:00",
                        hash="jp",
                    ),
                    WebPage(
                        id="web:altsource_sv:tc:event_story:1:1",
                        source="altsource_sv",
                        url="https://storage.sekai.best/tc/event_story/1/1.asset",
                        title="tc",
                        language="zh_hant",
                        kind="event_story",
                        text="日本語の本文",
                        crawled_at="2026-08-12T00:00:00+00:00",
                        hash="tc",
                    ),
                ],
            )

            result = mark_untranslated_pages(store_root, placeholder="[未翻译]")
            self.assertEqual(result["replaced_pages"], 1)
            pages = {page["id"]: page for page in flatten_web_pages(store_root)}
            self.assertTrue(pages["web:altsource_sv:tc:event_story:1:1"]["untranslated"])
            self.assertEqual(pages["web:altsource_sv:tc:event_story:1:1"]["text"], "[未翻译]")

    def test_identical_non_jp_text_is_replaced_with_placeholder(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:event_story:1:1:ja",
                        source="altsource_ms",
                        url="https://pjsk.moe/ja/story/event/1/1/",
                        title="ja",
                        language="ja",
                        kind="event_story",
                        text='日本語の本文',
                        crawled_at="2026-08-12T00:00:00+00:00",
                        hash="ja",
                    ),
                    WebPage(
                        id="web:altsource_ms:event_story:1:1:zh",
                        source="altsource_ms",
                        url="https://pjsk.moe/zh-cn/story/event/1/1/",
                        title="zh",
                        language="zh_hans",
                        kind="event_story",
                        text='日本語の本文',
                        crawled_at="2026-08-12T00:00:00+00:00",
                        hash="zh",
                    ),
                    WebPage(
                        id="web:altsource_ms:event_story:1:1:en",
                        source="altsource_ms",
                        url="https://pjsk.moe/en-us/story/event/1/1/",
                        title="en",
                        language="en",
                        kind="event_story",
                        text='日本語の本文',
                        crawled_at="2026-08-12T00:00:00+00:00",
                        hash="en",
                    ),
                    WebPage(
                        id="web:altsource_ms:event_story:1:1:zh-diff",
                        source="altsource_ms",
                        url="https://pjsk.moe/zh-cn/story/event/1/1/",
                        title="zh-diff",
                        language="zh_hans",
                        kind="event_story",
                        text='已翻译的正文',
                        crawled_at="2026-08-12T00:00:00+00:00",
                        hash="zh-diff",
                    ),
                ],
            )

            result = mark_untranslated_pages(store_root, placeholder='[未翻译]')
            self.assertEqual(result["replaced_pages"], 2)

            pages = {page["id"]: page for page in flatten_web_pages(store_root)}
            self.assertTrue(pages["web:altsource_ms:event_story:1:1:zh"]["untranslated"])
            self.assertEqual(pages["web:altsource_ms:event_story:1:1:zh"]["text"], '[未翻译]')
            self.assertTrue(pages["web:altsource_ms:event_story:1:1:zh"]["original_text_hash"])
            self.assertEqual(
                pages["web:altsource_ms:event_story:1:1:en"]["text"],
                '[未翻译]',
            )
            expected_hash = hashlib.sha1('[未翻译]'.encode("utf-8")).hexdigest()[:16]
            self.assertEqual(pages["web:altsource_ms:event_story:1:1:en"]["hash"], expected_hash)

            rebuild_web_index(store_root)
            pages = {page["id"]: page for page in flatten_web_pages(store_root)}
            self.assertTrue(pages["web:altsource_ms:event_story:1:1:en"]["untranslated"])
            self.assertFalse(pages["web:altsource_ms:event_story:1:1:en"]["content_language_mismatch"])
            self.assertEqual(pages["web:altsource_ms:event_story:1:1:en"]["text"], '[未翻译]')
            self.assertEqual(
                pages["web:altsource_ms:event_story:1:1:ja"]["text"],
                '日本語の本文',
            )
            self.assertEqual(
                pages["web:altsource_ms:event_story:1:1:zh-diff"]["text"],
                '已翻译的正文',
            )


if __name__ == "__main__":
    unittest.main()
