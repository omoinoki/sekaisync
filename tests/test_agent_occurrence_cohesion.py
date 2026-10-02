"""Same raw occurrence and contextual sense close every available language."""
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as os


class OccurrenceCohesionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def fixture(self, version=1, lines=5, extra_context=0):
        store = self.base / f"v{version}"
        dbstore.initialize(store)
        if version > 1:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                 backup_path=self.base / f"v{version}-v2-backup.db")
            if version == 3:
                dbstore.migrate_store(store, target_version=3, dry_run=False,
                                     backup_path=self.base / "v3-backup.db")
        texts = {"en": [f"Person:Her voice suits song {index}." for index in range(lines)],
                 "ja": [f"Person:Her vocal suits song {index}." for index in range(lines)],
                 "ko": [f"Person:Her singing suits song {index}." for index in range(lines)]}
        pages = {}
        for language, body in texts.items():
            body += [f"Person:Unrelated context {index}." for index in range(extra_context)]
            pages[language] = dict(id=f"web:fixture:{language}:event_story:1:1", source="fixture",
                                   language=language, trust="B", kind="event_story", text="\n".join(body))
        dbstore.upsert_web_pages(store, "fixture", list(pages.values()))
        offsets = {language: ap._lines(page)[1] for language, page in pages.items()}
        windows = []
        for index in range(lines):
            source = ap._view(pages["en"], [index], offsets["en"])
            window = dict(story_key="event:1:1", source=source,
                          targets={language: ap._view(pages[language], [index], offsets[language])
                                   for language in ("ja", "ko")},
                          search_text=source["text"].casefold(), search_unwrapped=source["text"].casefold())
            window["id"] = "span:" + ap._digest(window)[:24]
            windows.append(window)
        scope = dict(source_language="en", target_languages=["ja", "ko"], windows=windows)
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        return store, scope_id, scope

    def item(self, scope_id, scope, index, language="ja"):
        row = scope["windows"][index]
        source = row["source"]
        anchor = os._anchor(source, row["story_key"], ap._body_term_segments(
            source["text"], "voice", source["start"])[0])
        sense = os._sense(os._identity("lex:", ["en", "voice"]), "en", "singing_voice", "singing timbre")
        context = dict(schema=ap._SCHEMA, task="translation", scope_id=scope_id, source_language="en",
                       rows=[dict(id=row["id"], story_key=row["story_key"], source=source,
                                  target=row["targets"][language])], available_stories=1, available_spans=1)
        return ap._focused_occurrence_item(ap._item("voice", language, [], "pending", context, "Fixture"),
                                           anchor, sense)

    def proposal(self, item, kind="lexical"):
        row = item._context["rows"][0]
        focus = item._context.get("focus") or dict(source=item._context["expansion"]["focus_source"],
                                                   sense=item._context["expansion"]["sense"])
        target_term = "vocal" if item.language == "ja" else "singing"
        target = (ap._body_term_segments(row["target"]["text"], target_term, row["target"]["start"])[0]
                  if kind in {"lexical", "paraphrase", "reference"} else
                  [dict(start=row["target"]["start"], end=row["target"]["end"], exact=row["target"]["text"])])
        return dict(evidence_id=row["id"], source_segments=focus["source"]["segments"], target_segments=target,
                    sense_key=focus["sense"]["key"], sense_gloss=focus["sense"]["gloss"], kind=kind,
                    rationale="Independently judged this exact source occurrence in the raw context")

    def submit(self, store, item, kind="lexical"):
        ar.enqueue(store, [item])
        result = ar.submit_judgments(store, [dict(id=item.id, decision="accept", relations=[self.proposal(item, kind)])])
        self.assertEqual(result["errors"], [])
        return result

    def focus_queue(self, store):
        return [item for item in ar.load_queue(store) if item._context.get("focus")]

    def test_submit_closes_other_language_without_publishing_global_slots_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, scope_id, scope = self.fixture(version)
                initial = self.item(scope_id, scope, 4)
                result = self.submit(store, initial)
                self.assertEqual(result["followup_packets"], 1)
                pending = self.focus_queue(store)
                self.assertEqual(len(pending), 1)
                self.assertEqual(pending[0].language, "ko")
                self.assertEqual(pending[0]._context["focus"], initial._context["focus"])
                self.assertEqual(self.submit(store, pending[0])["followup_packets"], 0)
                self.assertEqual(ap.ensure_occurrence_cohesion(store)["added"], 0)
                with dbstore.connect(store) as conn:
                    self.assertEqual(len(os._read_relations(conn)), 2)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)

    def test_focus_beyond_normal_pair_sampling_survives_and_other_occurrence_does_not_count(self):
        store, scope_id, scope = self.fixture()
        normal = ap._translation_item(scope_id, scope, "voice", "ko")
        self.assertEqual(len(normal._context["rows"]), ap._MAX_PAIRS)
        later = self.item(scope_id, scope, 4)
        self.submit(store, later)
        first = self.item(scope_id, scope, 0, "ko")
        self.submit(store, first)
        pending = self.focus_queue(store)
        self.assertEqual({(item.language, item._context["focus"]["source"]["id"]) for item in pending},
                         {("ko", later._context["focus"]["source"]["id"]),
                          ("ja", first._context["focus"]["source"]["id"])})

    def test_each_relation_kind_counts_as_judged_without_duplicate_same_language(self):
        for kind in ("lexical", "paraphrase", "reference", "omitted", "unresolved"):
            with self.subTest(kind=kind):
                self.base = Path(self.temp.name) / kind
                store, scope_id, scope = self.fixture()
                item = self.item(scope_id, scope, 0)
                self.submit(store, item, kind)
                pending = self.focus_queue(store)
                self.assertEqual([entry.language for entry in pending], ["ko"])
                self.assertEqual(ap.ensure_occurrence_cohesion(store)["added"], 0)

    def test_stale_target_is_not_enqueued_but_existing_judgment_stays_local(self):
        store, scope_id, scope = self.fixture()
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=text||' changed' WHERE language='ko'")
            conn.commit()
        self.submit(store, self.item(scope_id, scope, 4))
        self.assertEqual(self.focus_queue(store), [])
        self.assertEqual(ap.ensure_occurrence_cohesion(store)["added"], 0)

    def test_export_recovers_and_batch_cap_advances_without_dropping_later_work(self):
        store, scope_id, scope = self.fixture()
        for index in range(5):
            self.submit(store, self.item(scope_id, scope, index))
        ar._write_json(ar.queue_path(store), {"items": []})
        self.assertEqual(ap.ensure_occurrence_cohesion(store, limit=2)["added"], 2)
        self.assertEqual(ap.ensure_occurrence_cohesion(store, limit=2)["added"], 2)
        self.assertEqual(ap.ensure_occurrence_cohesion(store, limit=2)["added"], 1)
        self.assertEqual(ap.ensure_occurrence_cohesion(store, limit=2)["added"], 0)
        self.assertEqual(len(self.focus_queue(store)), 5)
        ar._write_json(ar.queue_path(store), {"items": []})
        ar.export_for_agent(store, self.base / "export.txt")
        self.assertEqual(len(self.focus_queue(store)), 5)
        ar.export_for_agent(store, self.base / "export-again.txt")
        self.assertEqual(len(self.focus_queue(store)), 5)

    def test_resolved_expansion_recovers_full_original_target_inventory(self):
        store, scope_id, scope = self.fixture(extra_context=10)
        original = self.item(scope_id, scope, 0)
        self.submit(store, original, "unresolved")
        expansion = next(item for item in ar.load_queue(store) if item._context.get("expansion"))
        self.submit(store, expansion)
        ar._write_json(ar.queue_path(store), {"items": []})
        self.assertEqual(ap.ensure_occurrence_cohesion(store)["added"], 1)
        pending = self.focus_queue(store)
        self.assertEqual(pending[0].language, "ko")
        self.assertEqual(pending[0]._context["scope_id"], scope_id)
        self.assertEqual(pending[0]._context["focus"], original._context["focus"])

    def test_export_does_not_create_a_database_for_legacy_file_only_store(self):
        store = self.base / "file-only"
        self.assertEqual(ap.ensure_occurrence_cohesion(store)["added"], 0)
        self.assertFalse((store / "kb" / "sekaisync.db").exists())


if __name__ == "__main__":
    unittest.main()
