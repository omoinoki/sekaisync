"""New small discovery batches preserve raw coverage and old packet answers."""
import tempfile
from pathlib import Path
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, termindex


class FocusedDiscoveryBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "store"
        dbstore.initialize(self.store)

    def prepare(self, source="en", turns=25, stories=1, padding=""):
        languages = ("en", "ja", "zh_hans", "zh_tw", "ko")
        pages = [dict(id=f"web:fixture:{language}:event_story:1:{story}",
                      source="fixture", language=language, kind="event_story", trust="B",
                      text="\n".join(f"Person: token{index:02} {padding}" +
                                      (" \uc6a9\uc5b4" if language == "ko" else "")
                                      for index in range(turns)))
                 for story in range(1, stories + 1) for language in languages]
        dbstore.upsert_web_pages(self.store, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        return ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, source,
                                        [language for language in languages if language != source])

    def test_many_short_turns_are_bounded_and_preserve_every_window_in_all_languages(self):
        for source in ("en", "ja", "zh_hans", "zh_tw", "ko"):
            with self.subTest(source=source):
                items, meta = self.prepare(source)
                scope = ap._read_scope(self.store, meta["scope_id"])
                self.assertEqual([len(item._context["rows"]) for item in items], [8, 8, 8, 1])
                self.assertEqual([row for item in items for row in item._context["rows"]], scope["windows"])
                self.assertEqual(meta["source_windows"], 25)
                self.assertEqual(meta["discovery_packets"], 4)
                self.assertEqual(meta["model_api_calls"], 0)
                self.assertTrue(all(item._context["task"] == "discovery" for item in items))

    def test_existing_character_bound_and_story_boundary_still_apply(self):
        items, meta = self.prepare(turns=5, stories=2, padding="word " * 200)
        rows = [row for item in items for row in item._context["rows"]]
        self.assertEqual(rows, ap._read_scope(self.store, meta["scope_id"])["windows"])
        for item in items:
            batch = item._context["rows"]
            self.assertLessEqual(len(batch), ap._DISCOVERY_ROWS)
            self.assertLessEqual(sum(len(row["source"]["text"]) for row in batch), ap._DISCOVERY_CHARS)
            self.assertEqual(len({row["story_key"] for row in batch}), 1)

    def test_repeated_generation_is_idempotent_and_existing_terms_answers_still_work(self):
        items, _ = self.prepare()
        self.assertEqual(ar.enqueue(self.store, items)["added"], 4)
        again, _ = self.prepare()
        self.assertEqual([item.id for item in items], [item.id for item in again])
        self.assertEqual(ar.enqueue(self.store, again)["added"], 0)
        judgment = dict(id=items[0].id, decision="accept", terms=["token00"])
        result = ar.submit_judgments(self.store, [judgment])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["discovered_terms"], 1)
        self.assertEqual(ar.submit_judgments(self.store, [judgment])["discovered_terms"], 0)

    def test_preexisting_broad_packet_remains_submittable_without_new_fields(self):
        items, meta = self.prepare()
        scope = ap._read_scope(self.store, meta["scope_id"])
        context = dict(schema=ap._SCHEMA, task="discovery", scope_id=meta["scope_id"],
                       source_language="en", rows=scope["windows"])
        name = "@discover:" + scope["windows"][0]["story_key"] + ":" + scope["windows"][0]["id"]
        old = ap._item(name, "en", [], "discovery", context, "Existing broad packet")
        ar.enqueue(self.store, [old])
        result = ar.submit_judgments(self.store, [dict(id=old.id, decision="accept", terms=[])])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["accepted"], 1)

    def test_task_prompt_prescribes_per_turn_nested_and_function_units(self):
        items, _ = self.prepare(turns=1)
        text = ar.render_item(items[0])
        for cue in ("each shown source turn individually", "modified/nested", "negation",
                    "function expressions", "open-slot", "arbitrary substrings", "terms list remains valid"):
            self.assertIn(cue, text)


if __name__ == "__main__":
    unittest.main()
