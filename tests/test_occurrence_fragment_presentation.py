"""Existing result notes expose actual fragments without inventing a scalar."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as packets, agent_review as review, dbstore
from sekaisync.core import SekaiSyncCore


class OccurrenceFragmentPresentationTests(unittest.TestCase):
    def test_real_function_frame_is_visible_in_both_public_consumers(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Path(directory) / "store"
            dbstore.initialize(store)
            source = dict(id="web:fixture:en:event_story:902:1", source="fixture", kind="event_story",
                          language="en", trust="B", text="A: First observe, then draw.")
            target = dict(id="web:fixture:zh_hans:event_story:902:1", source="fixture", kind="event_story",
                          language="zh_hans", trust="B", text="B: \u5148\u89c2\u5bdf\uff0c\u518d\u4e0b\u7b14\u3002")
            dbstore.upsert_web_pages(store, "fixture", [source, target])
            view = packets._view(source, [0], packets._lines(source)[1])
            row = dict(story_key="event:902:1", source=view, targets={})
            row["id"] = "span:" + packets._digest(row)[:24]
            scope = dict(source_language="en", target_languages=["zh_hans"], windows=[row])
            scope_id = packets._digest(scope)
            review._write_json(packets._scope_path(store, scope_id), scope)
            context = dict(schema=packets._SCHEMA, task="discovery", scope_id=scope_id,
                           source_language="en", rows=[row])
            discovery = packets._item("@discover:event:902:1:" + row["id"], "en", [],
                                      "discovery", context, "Raw sequencing frame")
            review.enqueue(store, [discovery])

            def parts(page, fragments):
                return [dict(start=page["text"].index(exact), end=page["text"].index(exact) + len(exact), exact=exact)
                        for exact in fragments]

            selected = parts(source, ["First", "then"])
            result = review.submit_judgments(store, [dict(id=discovery.id, decision="accept", subjects=[
                dict(kind="segmented", evidence_id=row["id"], segments=selected)])])
            self.assertEqual(result["errors"], [])
            pending = review.load_queue(store)
            self.assertEqual([item._context["task"] for item in pending], ["occurrence", "discovery"])
            occurrence, audit = pending
            target_parts = parts(target, ["\u5148", "\u518d"])
            relation = dict(evidence_id=occurrence._context["rows"][0]["id"], source_segments=selected,
                            target_segments=target_parts, sense_key="ordered-stages", kind="lexical",
                            sense_gloss="A genuine first-then sequencing construction with predicate slots",
                            rationale="The raw function markers govern the two corresponding ordered stages")
            result = review.submit_judgments(store, [dict(id=occurrence.id, decision="accept", relations=[relation])])
            self.assertEqual(result["errors"], [])
            self.assertIn(audit.id, {item.id for item in review.load_queue(store)})

            core = SekaiSyncCore(store)
            with core.request_view():
                penetration = core.term_penetrate("First then", story_key="event:902:1", languages=["en", "zh_hans"])
                generic = core.query("First then", include_web=False)
            self.assertIsNotNone(penetration)
            self.assertEqual(len(generic["terms"]), 1)
            self.assertEqual(penetration["term"]["names"], {})
            self.assertEqual(generic["terms"][0]["names"], {})
            positions = {item["language"]: item for item in generic["terms"][0]["positions"]}
            for language, expected in (("en", selected), ("zh_hans", target_parts)):
                for entry in (penetration["per_language"][language], positions[language]):
                    self.assertEqual(entry["term"], "")
                    self.assertTrue(entry["missing"])
                    label = "; raw selected fragments (Unicode code points, end-exclusive): "
                    self.assertEqual(json.loads(entry["note"].split(label, 1)[1]), expected)
                    self.assertIn("no scalar" if language == "zh_hans" else "not a scalar", entry["note"])


if __name__ == "__main__":
    unittest.main()
