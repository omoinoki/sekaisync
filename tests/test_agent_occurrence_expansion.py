"""Adaptive evidence stays exact, bounded, resumable and occurrence-only."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, occurrence_store as os


class OccurrenceExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def packet(self, version=1, hidden_line=1, with_voice=True, context_lines=28):
        store = self.base / f"v{version}-{hidden_line}"
        dbstore.initialize(store)
        if version > 1:
            dbstore.migrate_store(store, target_version=2, dry_run=False,
                                 backup_path=self.base / f"v{version}-v2.db")
            if version == 3:
                dbstore.migrate_store(store, target_version=3, dry_run=False,
                                     backup_path=self.base / "v3.db")
        source_text = "\u7532\uff1a\u5f00\u573a\u3002\n\u7532\uff1a\u5979\u7684\u58f0\u97f3\u5f88\u9002\u5408\u6b4c\u66f2\u3002\n\u7532\uff1a\u7ed3\u675f\u7684\u58f0\u97f3\u3002"
        lines = ["Person:This aligned turn contains no singing expression."]
        lines.extend(f"Person:Other context {index}. " + "Background context remains unrelated. " * 3
                     for index in range(context_lines))
        if with_voice:
            lines[hidden_line] = "Person:Her voice suits the song."
        target_text = "\n".join(lines)
        pages = [dict(id="web:fixture:zh_hans:event_story:1:1", source="fixture", language="zh_hans",
                      trust="B", kind="event_story", text=source_text),
                 dict(id="web:fixture:en:event_story:1:1", source="fixture", language="en",
                      trust="B", kind="event_story", text=target_text)]
        dbstore.upsert_web_pages(store, "fixture", pages)
        source_lines, source_offsets = ap._lines(pages[0])
        _, target_offsets = ap._lines(pages[1])
        source = ap._view(pages[0], [1], source_offsets)
        target = ap._view(pages[1], [0], target_offsets)
        row = dict(story_key="event:1:1", source=source, targets={"en": target},
                   search_text=source_lines[1], search_unwrapped=source_lines[1])
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="zh_hans", target_languages=["en"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        item = ap._occurrence_item(ap._translation_item(scope_id, scope, "\u58f0\u97f3", "en"))
        ar.enqueue(store, [item])
        return store, item

    def proposal(self, item, kind="unresolved"):
        row = item._context["rows"][0]
        expansion = item._context.get("expansion")
        source = (expansion["focus_source"]["segments"] if expansion else
                  ap._body_term_segments(row["source"]["text"], item.term, row["source"]["start"])[0])
        target = (ap._body_term_segments(row["target"]["text"], "voice", row["target"]["start"])[0]
                  if kind == "lexical" else [dict(start=row["target"]["start"], end=row["target"]["end"],
                                                  exact=row["target"]["text"])])
        return dict(evidence_id=row["id"], source_segments=source, target_segments=target,
                    sense_key="singing_voice", sense_gloss="the timbre of a singing voice", kind=kind,
                    rationale="Examined the complete supplied raw context for this exact occurrence")

    def submit(self, store, item, kind="unresolved", proposal=None):
        return ar.submit_judgments(store, [dict(id=item.id, decision="accept", agent="test-reviewer",
                                               generalize=None, relations=[proposal or self.proposal(item, kind)])])

    def next_item(self, store):
        pending = [item for item in ar.load_queue(store) if item._context.get("expansion")]
        self.assertEqual(len(pending), 1)
        return pending[0]

    def test_unresolved_creates_wider_task_and_resolved_retires_parent_v1_v2_v3(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, item = self.packet(version)
                first = self.submit(store, item)
                self.assertEqual(first["errors"], [])
                self.assertEqual(first["followup_packets"], 1)
                child = self.next_item(store)
                self.assertEqual(child._context["expansion"]["level"], "adjacent")
                self.assertIn("Her voice suits", ar.render_item(child))
                answer = self.submit(store, child, "lexical")
                self.assertEqual(answer["errors"], [])
                self.assertEqual(answer["applied_slots"], 0)
                with dbstore.connect(store) as conn:
                    current = os._read_relations(conn)
                    history = os._read_relations(conn, current_only=False)
                    self.assertEqual(len(current), 1)
                    self.assertEqual(current[0]["kind"], "lexical")
                    self.assertEqual(len(history), 2)
                    self.assertEqual(current[0]["source"], history[0]["source"])
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                self.assertEqual(ap.ensure_occurrence_expansions(store)["added"], 0)
                self.assertEqual(ar.submit_judgments(store, [dict(id=child.id, decision="accept",
                                                               relations=[self.proposal(child, "lexical")])])["errors"], [])

    def test_three_stages_end_without_infinite_tasks_and_full_text_is_not_truncated(self):
        store, item = self.packet(hidden_line=28)
        self.assertEqual(self.submit(store, item)["errors"], [])
        for stage in (1, 2, 3):
            child = self.next_item(store)
            self.assertEqual(child._context["expansion"]["stage"], stage)
            self.assertEqual(self.submit(store, child)["errors"], [])
        self.assertEqual(ar.load_queue(store), [])
        self.assertEqual(ap.ensure_occurrence_expansions(store)["added"], 0)
        self.assertEqual(ar.load_queue(store), [])
        self.assertEqual(child._context["expansion"]["level"], "full_page")
        self.assertTrue(child._context["expansion"]["target_complete_page"])
        self.assertIn("Her voice suits", ar.render_item(child))
        self.assertGreater(len(child._context["rows"][0]["target"]["text"]), ap._TURN_CHARS)
        self.assertNotIn('"complete": false', ar.render_item(child))

    def test_changed_focus_sense_or_raw_page_is_rejected(self):
        store, item = self.packet()
        self.submit(store, item)
        child = self.next_item(store)
        bad = self.proposal(child, "lexical")
        bad["sense_key"] = "a_different_sense"
        result = self.submit(store, child, proposal=bad)
        self.assertTrue(result["errors"])
        self.assertEqual(self.next_item(store).id, child.id)
        bad_source = self.proposal(child, "lexical")
        row = child._context["rows"][0]
        bad_source["source_segments"] = ap._body_term_segments(
            row["source"]["text"], item.term, row["source"]["start"])[-1]
        self.assertTrue(self.submit(store, child, proposal=bad_source)["errors"])
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET text=text||'changed' WHERE language='en'")
            conn.commit()
        result = self.submit(store, child, "lexical")
        self.assertTrue(result["errors"])
        self.assertEqual(self.next_item(store).id, child.id)

    def test_authentic_parent_scope_and_receipt_are_required(self):
        store, item = self.packet()
        self.submit(store, item)
        child = self.next_item(store)
        with dbstore.connect(store) as conn:
            ap._validate_occurrence_expansion(conn, store, child)
            changed = ar.ReviewItem.from_dict(child.to_dict())
            changed._context = dict(changed._context)
            changed._context["expansion"] = dict(changed._context["expansion"], stage=3)
            with self.assertRaisesRegex(ValueError, "authentic parent"):
                ap._validate_occurrence_expansion(conn, store, changed)
        receipt_file = ap._receipt_path(store)
        ar._write_json(receipt_file, {"items": {}})
        with dbstore.connect(store) as conn:
            with self.assertRaisesRegex(ValueError, "no completed review receipt"):
                ap._validate_occurrence_expansion(conn, store, child)

    def test_export_recovers_missing_followup_once_without_new_commands(self):
        store, item = self.packet()
        self.submit(store, item)
        child = self.next_item(store)
        ar._write_json(ar.queue_path(store), {"items": []})
        self.assertEqual(ar.load_queue(store), [])
        ar.export_for_agent(store, self.base / "export.txt")
        self.assertEqual(self.next_item(store).id, child.id)
        ar.export_for_agent(store, self.base / "export-again.txt")
        self.assertEqual(self.next_item(store).id, child.id)

    def test_oversized_page_is_bounded_not_called_full(self):
        store, item = self.packet(hidden_line=28)
        with dbstore.connect(store) as conn:
            original = conn.execute("SELECT text FROM web_pages WHERE language='en'").fetchone()[0]
            raw = original + "\nPerson:" + "x" * 30000
            conn.execute("UPDATE web_pages SET text=? WHERE language='en'", (raw,))
            conn.commit()
        source = item._context["rows"][0]["source"]
        target = dict(item._context["rows"][0]["target"], sha256=ap._page_hash(dict(text=raw)))
        window = dict(story_key="event:1:1", source=source, targets={"en": target},
                      search_text=source["text"], search_unwrapped=source["text"])
        window["id"] = "span:" + ap._digest(window)[:24]
        scope = dict(source_language="zh_hans", target_languages=["en"], windows=[window])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        fresh = ap._occurrence_item(ap._translation_item(scope_id, scope, item.term, "en"))
        ar.enqueue(store, [fresh])
        self.assertEqual(self.submit(store, fresh)["errors"], [])
        for _ in range(3):
            pending = [entry for entry in ar.load_queue(store) if entry._context.get("expansion")]
            if not pending:
                break
            self.assertEqual(len(pending), 1)
            child = pending[0]
            self.assertEqual(self.submit(store, child)["errors"], [])
        self.assertEqual(child._context["expansion"]["level"], "bounded_page")
        self.assertFalse(child._context["expansion"]["target_complete_page"])
        self.assertLessEqual(len(child._context["rows"][0]["target"]["text"]), ap._EXPANSION_CHARS)
        self.assertNotIn('"complete": false', ar.render_item(child))

    def test_full_page_omission_retires_unresolved_but_partial_omission_cannot(self):
        store, item = self.packet(hidden_line=28, with_voice=False)
        self.submit(store, item)
        child = self.next_item(store)
        partial = self.submit(store, child, "omitted")
        self.assertTrue(partial["errors"])
        for _ in (1, 2):
            child = self.next_item(store)
            self.assertEqual(self.submit(store, child)["errors"], [])
        child = self.next_item(store)
        self.assertEqual(child._context["expansion"]["level"], "full_page")
        self.assertEqual(self.submit(store, child, "omitted")["errors"], [])
        with dbstore.connect(store) as conn:
            current = os._read_relations(conn)
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]["kind"], "omitted")
            self.assertEqual(len(os._read_relations(conn, current_only=False)), 4)

    def test_small_page_finishes_when_the_first_expansion_is_already_full(self):
        store, item = self.packet(with_voice=False, context_lines=1)
        self.assertEqual(self.submit(store, item)["errors"], [])
        child = self.next_item(store)
        self.assertEqual(child._context["expansion"]["stage"], 1)
        self.assertEqual(child._context["expansion"]["level"], "full_page")
        self.assertTrue(child._context["expansion"]["exhausted"])
        self.assertEqual(self.submit(store, child, "omitted")["errors"], [])
        self.assertEqual(ap.ensure_occurrence_expansions(store)["added"], 0)
        self.assertEqual(ar.load_queue(store), [])

    def test_raw_language_alias_never_changes_the_original_sense_subject(self):
        store, original = self.packet()
        source = dict(original._context["rows"][0]["source"], language="zh_hant")
        target = original._context["rows"][0]["target"]
        with dbstore.connect(store) as conn:
            conn.execute("UPDATE web_pages SET language='zh_hant' WHERE language='zh_hans'")
            conn.commit()
        window = dict(story_key="event:1:1", source=source, targets={"en": target},
                      search_text=source["text"], search_unwrapped=source["text"])
        window["id"] = "span:" + ap._digest(window)[:24]
        scope = dict(source_language="zh_tw", target_languages=["en"], windows=[window])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        item = ap._occurrence_item(ap._translation_item(scope_id, scope, original.term, "en"))
        ar.enqueue(store, [item])
        self.assertEqual(self.submit(store, item)["errors"], [])
        child = self.next_item(store)
        self.assertEqual(child._context["source_language"], "zh_tw")
        self.assertEqual(child._context["rows"][0]["source"]["language"], "zh_hant")
        self.assertEqual(self.submit(store, child, "lexical")["errors"], [])
        with dbstore.connect(store) as conn:
            current = os._read_relations(conn)
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]["sense"]["term_id"], os._identity("lex:", ["zh_tw", original.term]))

    def test_focused_task_cannot_borrow_same_word_or_retire_other_occurrence(self):
        store, original = self.packet()
        old_row = original._context["rows"][0]
        with dbstore.connect(store) as conn:
            raw_source = conn.execute("SELECT text FROM web_pages WHERE language='zh_hans'").fetchone()[0]
            raw_target = conn.execute("SELECT text FROM web_pages WHERE language='en'").fetchone()[0]
        source_page = dict(id=old_row["source"]["page_id"], source="fixture", language="zh_hans", text=raw_source)
        target_page = dict(id=old_row["target"]["page_id"], source="fixture", language="en", text=raw_target)
        _, source_offsets = ap._lines(source_page)
        _, target_offsets = ap._lines(target_page)
        source = ap._view(source_page, [1, 2], source_offsets)
        target = ap._view(target_page, [0, 1], target_offsets)
        row = dict(story_key="event:1:1", source=source, targets={"en": target},
                   search_text=source["text"], search_unwrapped=source["text"])
        row["id"] = "span:" + ap._digest(row)[:24]
        scope = dict(source_language="zh_hans", target_languages=["en"], windows=[row])
        scope_id = ap._digest(scope)
        ar._write_json(ap._scope_path(store, scope_id), scope)
        translation = ap._translation_item(scope_id, scope, original.term, "en")
        plain = ap._occurrence_item(translation)
        ar.enqueue(store, [plain])
        self.assertEqual(self.submit(store, plain, "lexical")["errors"], [])
        choices = ap._body_term_segments(source["text"], original.term, source["start"])
        anchor = os._anchor(source, "event:1:1", choices[-1])
        sense = os._sense(os._identity("lex:", ["zh_hans", original.term]), "zh_hans",
                          "singing_voice", "the timbre of a singing voice")
        focused = ap._focused_occurrence_item(translation, anchor, sense)
        self.assertEqual(focused._context["scope_id"], scope_id)
        self.assertEqual(focused._context["focus"], dict(source=anchor, sense=sense))
        self.assertIn("focus_contract", ar.render_item(focused))
        ar.enqueue(store, [focused])
        self.assertTrue(self.submit(store, focused, "lexical")["errors"])
        valid = self.proposal(focused, "lexical")
        valid["source_segments"] = anchor["segments"]
        self.assertEqual(self.submit(store, focused, proposal=valid)["errors"], [])
        with dbstore.connect(store) as conn:
            relations = os._read_relations(conn)
            self.assertEqual(len(relations), 2)
            self.assertEqual(len({relation["source"]["id"] for relation in relations}), 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
        outside = os._anchor(source, "event:1:1", choices[-1])
        original_translation = ap._translation_item(original._context["scope_id"],
                                                      ap._read_scope(store, original._context["scope_id"]),
                                                      original.term, "en")
        with self.assertRaisesRegex(ValueError, "outside this translation packet"):
            ap._focused_occurrence_item(original_translation, outside, sense)
        foreign = os._sense("lex:foreign", "zh_hans", "singing_voice", "the timbre of a singing voice")
        with self.assertRaisesRegex(ValueError, "does not belong"):
            ap._focused_occurrence_item(translation, anchor, foreign)


if __name__ == "__main__":
    unittest.main()
