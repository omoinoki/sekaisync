import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.integrity import run_integrity_check
from sekaisync.models import WebPage
from sekaisync.webindex import save_web_pages


class IntegrityTest(unittest.TestCase):
    def _write_pages(self, store_root: Path) -> None:
        save_web_pages(
            store_root,
            "altsource_ms",
            [
                WebPage(
                    id="web:altsource_ms:event_story:1:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/zh-cn/story/event/1/1/",
                    title="活动1-1",
                    language="zh_hans",
                    kind="event_story",
                    text="同一正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="a",
                ),
                WebPage(
                    id="web:altsource_ms:event_story:2:1",
                    source="altsource_ms",
                    url="https://pjsk.moe/zh-cn/story/event/2/1/",
                    title="活动2-1",
                    language="zh_hans",
                    kind="event_story",
                    text="活动2正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="d",
                ),
            ],
        )
        save_web_pages(
            store_root,
            "altsource_sv",
            [
                WebPage(
                    id="web:altsource_sv:cn:event_story:1:1",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/1/1.asset",
                    title="活动1-1",
                    language="zh_hans",
                    kind="event_story",
                    text="同一正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="b",
                ),
                WebPage(
                    id="web:altsource_sv:cn:event_story:1:1:conflict",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/1/1-alt.asset",
                    title="活动1-1冲突",
                    language="zh_hans",
                    kind="event_story",
                    text="被改写的正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="c",
                ),
                WebPage(
                    id="web:altsource_sv:cn:event_story:2:1",
                    source="altsource_sv",
                    url="https://storage.sekai.best/event_story/2/1.asset",
                    title="活动2-1",
                    language="zh_hans",
                    kind="event_story",
                    text="活动2正文",
                    crawled_at="2026-08-11T00:00:00+00:00",
                    hash="e",
                ),
            ],
        )

    def test_untranslated_placeholder_does_not_create_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms",
                [
                    WebPage(
                        id="web:altsource_ms:ko:event_story:1:1",
                        source="altsource_ms",
                        url="https://pjsk.moe/ko-kr/story/event/1/1/",
                        title="活动1-1",
                        language="ko",
                        kind="event_story",
                        text="실제 번역문",
                        crawled_at="2026-08-11T00:00:00+00:00",
                        hash="a",
                    ),
                ],
            )
            save_web_pages(
                store_root,
                "altsource_sv",
                [
                    WebPage(
                        id="web:altsource_sv:kr:event_story:1:1",
                        source="altsource_sv",
                        url="https://storage.sekai.best/event_story/1/1.asset",
                        title="活动1-1",
                        language="ko",
                        kind="event_story",
                        text="[未翻译]",
                        untranslated=True,
                        untranslated_placeholder="[未翻译]",
                        crawled_at="2026-08-11T00:00:00+00:00",
                        hash="b",
                    ),
                ],
            )

            result = run_integrity_check(store_root, limit=10)
            self.assertEqual(result["summary"]["conflicts"], 0)
            self.assertEqual(result["summary"]["mirror_duplicates"], 0)

    def test_integrity_detects_mirror_duplicates_conflicts_and_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_pages(store_root)

            for source in ("altsource_ms", "altsource_sv"):
                pages_path = store_root / "kb" / "web" / source / "pages.json"
                pages = json.loads(pages_path.read_text(encoding="utf-8"))
                for page in pages:
                    if page["id"] == "web:altsource_ms:event_story:1:1":
                        page["text_hash"] = "wrong"
                    if page["id"] == "web:altsource_sv:cn:event_story:1:1:conflict":
                        page["asset_mismatch"] = "language_mismatch: expected zh_hans, text script mismatch"
                        page["scenario_id_mismatch"] = "ScenarioId event_01_02 != expected event_01_01"
                pages_path.write_text(json.dumps(pages, ensure_ascii=False), encoding="utf-8")

            result = run_integrity_check(store_root, limit=10)
            self.assertEqual(result["summary"]["mirror_duplicates"], 1)
            self.assertEqual(result["summary"]["conflicts"], 1)
            self.assertEqual(result["summary"]["hash_mismatches"], 1)
            self.assertEqual(result["summary"]["asset_mismatches"], 1)
            self.assertEqual(result["summary"]["scenario_id_mismatches"], 1)
            self.assertGreaterEqual(result["summary"]["issues"], 3)



    def test_auxiliary_pages_are_not_canonical_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            save_web_pages(
                store_root,
                "altsource_ms_translation",
                [
                    WebPage(
                        id="web:altsource_ms_translation:zh-cn:event_story:1:1:ja",
                        source="altsource_ms_translation",
                        url="https://translation.exmeaning.com/translation/eventStory/event_1.json",
                        title="event 1 episode 1",
                        language="ja",
                        kind="event_story",
                        text="原文",
                        crawled_at="2026-08-14T00:00:00+00:00",
                        hash="a",
                        auxiliary=True,
                        overlay=True,
                        translation_source="official_cn",
                    ),
                ],
            )
            result = run_integrity_check(store_root, limit=10)
            self.assertEqual(result["layers"]["web"]["canonical_missing"], 0)
            self.assertGreaterEqual(result["layers"]["web"]["canonical_not_applicable"], 1)


if __name__ == "__main__":
    unittest.main()
