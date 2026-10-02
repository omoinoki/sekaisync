"""Synthetic normal receipts must use completely visible wording values."""
from pathlib import Path
import json
import re
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sekaisync import agent_packets as ap, agent_review as ar, crawler, dbstore
from sekaisync import occurrence_store as ledger, termindex as ti, webindex


LONG = ("We must not release the corrected notice unless the designated reviewers "
        "have checked every stated condition and approved its exact final wording.")
TARGET = "\u6307\u5b9a\u3055\u308c\u305f\u62c5\u5f53\u8005\u304c\u6761\u4ef6\u3092\u78ba\u8a8d\u3059\u308b\u307e\u3067\u3001\u8a02\u6b63\u3057\u305f\u901a\u77e5\u3092\u516c\u8868\u3057\u3066\u306f\u3044\u3051\u307e\u305b\u3093\u3002"


class TypedWordingRenderAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, fallback):
        base = self.base / ("fallback" if fallback else "aligned")
        base.mkdir(parents=True)
        store = base / "store"
        dbstore.initialize_new_store(store, target_version=3)
        limit = ap._EXPANSION_CHARS if fallback else ap._TURN_CHARS
        source = "Intro: " + "x " * (limit // 2 + 10) + LONG
        target = "\u5165\u53e3\uff1a" + "\u957f\u503c" * (limit // 2 + 10) + TARGET
        pages = [crawler.altsource_sv_record_page(
            dict(wordingKey="SYNTHETIC_TYPED_RENDER_AUTHORITY", value=value), "wordings", region)
            for region, value in (("en", source), ("cn", target))]
        webindex.save_web_pages(store, pages[0].source, pages, write_categories=False, rewrite_index=False)
        loaded = ti.load_pages(store)
        groups = ti.group_pages_by_story([page for page in loaded if not fallback or page["language"] == "en"])
        items, _ = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, "en", ["zh_hans"])
        discovery = next(item for item in items if item._context["task"] == "discovery")
        ar.enqueue(store, [discovery])
        self.assertGreater(source.index(LONG), limit)
        self.assertGreater(target.index(TARGET), limit)
        return base, store, discovery, source, target

    @staticmethod
    def export_row(base, store, item, name):
        path = ar.export_for_agent(store, base / name, limit=0)
        block = next(block for block in re.split(r"(?m)^##", path.read_text(encoding="utf-8"))
                     if "id=" + item.id + " " in block)
        return next(json.loads(line[len("context: "):]) for line in block.splitlines()
                    if line.startswith("context: "))

    @staticmethod
    def submit(store, item, **fields):
        answer = dict(id=item.id, decision="accept", rationale="Synthetic render-authority fixture.", **fields)
        return ar.submit_judgments(store, ar.parse_judgments_text(json.dumps(answer)))

    def assert_whole_value(self, view, raw):
        self.assertTrue(view["text"] == raw, "normal export must retain every authorized wording code point")
        self.assertEqual(view["start"], 0)
        self.assertEqual(view["end"], len(raw))
        self.assertEqual(view["end"] - view["start"], len(view["text"]))
        self.assertTrue(view["complete"])

    def test_long_wording_tails_visible_through_normal_and_fallback_receipts(self):
        for fallback in (False, True):
            base, store, discovery, source, target = self.fixture(fallback)
            visible_source = self.export_row(base, store, discovery, "discovery.txt")["source"]
            row = discovery._context["rows"][0]
            source_start = source.index(LONG)
            source_parts = [dict(start=source_start, end=source_start + len(LONG), exact=LONG)]
            entry = dict(kind="literal", evidence_id=row["id"], canonical=LONG, segments=source_parts)
            result = self.submit(store, discovery, subjects=[entry])
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["accepted"], 1)
            self.assertIn(discovery.id, ap._receipts(store))
            with self.subTest(fallback=fallback, stage="discovery visibility"):
                self.assert_whole_value(visible_source, source)
            child = next(item for item in ar.load_queue(store) if item._context.get("task") == "occurrence")
            self.assertEqual(bool(child._context.get("fallback")), fallback)
            visible = self.export_row(base, store, child, "occurrence.txt")
            relation_row = child._context["rows"][0]
            target_start = target.index(TARGET)
            relation = dict(evidence_id=relation_row["id"], source_segments=source_parts,
                            target_segments=[dict(start=target_start, end=target_start + len(TARGET), exact=TARGET)],
                            kind="lexical", sense_key="synthetic-wording-render-authority",
                            sense_gloss="Synthetic structural test, not semantic gold.",
                            rationale="Exact source and target anchors from whole raw wording values.")
            result = self.submit(store, child, relations=[relation])
            self.assertEqual(result["errors"], [])
            self.assertEqual(result["accepted"], 1)
            self.assertIn(child.id, ap._receipts(store))
            with dbstore.connect(store) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                self.assertEqual(len(ledger._read_relations(conn)), 1)
            with self.subTest(fallback=fallback, stage="occurrence source visibility"):
                self.assert_whole_value(visible["source"], source)
            with self.subTest(fallback=fallback, stage="occurrence target visibility"):
                self.assert_whole_value(visible["target"], target)


if __name__ == "__main__":
    unittest.main(verbosity=2)
