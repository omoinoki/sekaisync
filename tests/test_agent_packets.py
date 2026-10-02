"""Agent-mediated scraper regression tests. No API, no production-store writes."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from sekaisync import agent_packets as ap, agent_review as ar, dbstore, termindex


NAMES = {"ja": "月虹音楽祭", "en": "Moonbow Festival", "zh_hans": "月虹音乐节",
         "zh_tw": "月虹音樂節", "ko": "달무리 음악제"}


def pages_for(stories=2):
    pages = []
    for i in range(stories):
        for language, name in NAMES.items():
            text = "A1 START\nB2 「" + name + "」\nC3 END"
            pages.append(dict(id=f"web:altsource_ms:{language}:event_story:999:{i}", source="altsource_ms", language=language,
                              story_key=f"event:999:{i}",
                              canonical_key=f"event_story:{language}:999:{i}",
                              kind="event_story", trust="B", text=text))
    return pages


class PacketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "store"
        dbstore.initialize(self.root)

    def setup_packets(self, pages=None, source="ja", candidates=None, version=1):
        if version > 1:
            dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                                 backup_path=Path(self.temp.name) / "v2.db")
        pages = pages if pages is not None else pages_for()
        dbstore.upsert_web_pages(self.root, "altsource_ms", pages)
        groups = termindex.group_pages_by_story(pages)
        items, meta = ap._prepare_scrub_review(self.root, groups, sorted(groups),
                                              candidates if candidates is not None else [NAMES[source]],
                                              {}, source, [l for l in NAMES if l != source])
        ar.enqueue(self.root, items)
        return items, meta

    def submit(self, item, value, **extra):
        return ar.submit_judgments(self.root, [dict(id=item.id, decision="replace", value=value,
                                                  rationale="同一语境下的完整对应词", **extra)])

    def names(self, source="ja"):
        with dbstore.connect(self.root) as conn:
            row = conn.execute("SELECT names_json FROM terms WHERE id=?",
                               (termindex.make_term_id(source, NAMES[source]),)).fetchone()
        return json.loads(row[0]) if row else {}

    def test_all_twenty_directed_pairs_have_grounded_packets(self):
        for source in NAMES:
            items, meta = self.setup_packets(source=source)
            pairs = [i for i in items if i.kind != "discovery"]
            self.assertEqual({i.language for i in pairs}, set(NAMES) - {source})
            for item in pairs:
                self.assertEqual(len({r["story_key"] for r in item._context["rows"]}), 2)
                self.assertIn(NAMES[item.language], ar.render_item(item))
                self.assertIn(NAMES[source], ar.render_item(item))
                applied = self.submit(item, NAMES[item.language])
                self.assertEqual(applied["errors"], [])
                self.assertEqual(self.names(source)[item.language], NAMES[item.language])

    def test_missing_source_word_is_discovered_and_fans_out_four_languages(self):
        items, _ = self.setup_packets(candidates=[])
        discover = next(i for i in items if i.kind == "discovery")
        self.assertFalse(any(i.kind != "discovery" for i in items))
        result = ar.import_judgments_from_text(self.root, json.dumps([
            dict(id=discover.id, decision="accept", terms=[NAMES["ja"]])]))
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["discovered_terms"], 1)
        pairs = [i for i in ar.load_queue(self.root) if i.kind != "discovery"]
        self.assertEqual({i.language for i in pairs}, set(NAMES) - {"ja"})
        self.assertEqual(self.submit(next(i for i in pairs if i.language == "en"), NAMES["en"])["applied_slots"], 1)
        self.assertEqual(self.names()["en"], NAMES["en"])

    def test_submit_commits_v1_and_v2_and_preserves_other_language_work(self):
        for version in (1, 2):
            with self.subTest(version=version):
                if version == 2:
                    self.root = Path(self.temp.name) / "modern"
                    dbstore.initialize(self.root)
                items, _ = self.setup_packets(version=version)
                item = next(i for i in items if i.language == "en" and i.kind != "discovery")
                result = self.submit(item, NAMES["en"])
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["applied_slots"], 1)
                self.assertEqual(self.names()["en"], NAMES["en"])
                pending = ar.load_queue(self.root)
                self.assertIn("ko", {i.language for i in pending})
                self.assertEqual(self.submit(item, NAMES["en"])["applied_slots"], 0)
                self.assertEqual(ar.enqueue(self.root, items)["added"], 0)

    def test_wrong_position_and_invented_translation_remain_unresolved(self):
        pages = pages_for()
        for page in pages:
            if page["language"] == "en":
                page["text"] = "INTRO99 Decoy Hall\n" + page["text"]
        items, _ = self.setup_packets(pages)
        item = next(i for i in items if i.language == "en" and i.kind != "discovery")
        for bad in ("Decoy Hall", "Imaginary Hall"):
            result = self.submit(item, bad)
            self.assertEqual(result["applied_slots"], 0)
            self.assertTrue(result["errors"])
            self.assertIn(item.id, {i.id for i in ar.load_queue(self.root)})

    def test_one_story_or_repeated_same_agent_votes_never_counts_as_two(self):
        items, _ = self.setup_packets(pages_for(1))
        item = next(i for i in items if i.language == "en" and i.kind != "discovery")
        for _ in range(3):
            result = self.submit(item, NAMES["en"])
            self.assertIn("two distinct stories", result["errors"][0])
        self.assertEqual(self.names(), {})

    def test_changed_page_and_tampered_scope_cannot_be_submitted(self):
        pages = pages_for()
        items, meta = self.setup_packets(pages)
        item = next(i for i in items if i.language == "en" and i.kind != "discovery")
        pages[1]["text"] += "\nChanged"
        dbstore.upsert_web_pages(self.root, "altsource_ms", pages)
        result = self.submit(item, NAMES["en"])
        self.assertIn("source page changed", result["errors"][0])
        path = ap._scope_path(self.root, meta["scope_id"])
        scope = json.loads(path.read_text(encoding="utf-8"))
        scope["source_language"] = "en"
        path.write_text(json.dumps(scope), encoding="utf-8")
        self.assertIn("scope changed", self.submit(item, NAMES["en"])["errors"][0])

    def test_hallucinated_discovery_and_speaker_only_name_rejected(self):
        pages = pages_for()
        for page in pages:
            if page["language"] == "ja":
                page["text"] = "案内係：月虹音楽祭へ行こう。"
        items, _ = self.setup_packets(pages, candidates=[])
        discover = next(i for i in items if i.kind == "discovery")
        for term in ("幻想記念館", "案内係"):
            result = ar.submit_judgments(self.root, [dict(id=discover.id, decision="accept", terms=[term])])
            self.assertTrue(result["errors"])
            self.assertEqual(result["discovered_terms"], 0)

    def test_full_tail_and_long_turn_covered_without_silent_truncation(self):
        pages = pages_for(1)
        for page in pages:
            if page["language"] == "ja":
                page["text"] = "案内係：" + "長い説明。" * 2800 + "末尾記念館へ。"
        items, meta = self.setup_packets(pages, candidates=[])
        scope = ap._read_scope(self.root, meta["scope_id"])
        views = [row["source"] for row in scope["windows"]]
        self.assertGreater(views[-1]["end"], 12000)
        self.assertIn("末尾記念館", ar.render_item(items[-1]))
        self.assertTrue(all(len(v["text"]) <= ap._TURN_CHARS for v in views))
        self.assertTrue(all(b["start"] <= a["end"] for a, b in zip(views, views[1:])))

    def test_unknown_evidence_and_existing_conflict_are_not_accepted(self):
        items, _ = self.setup_packets()
        item = next(i for i in items if i.language == "en" and i.kind != "discovery")
        self.assertTrue(self.submit(item, NAMES["en"], evidence_ids=["invented"])["errors"])
        dbstore.upsert_terms(self.root, [termindex.TermRecord(id=termindex.make_term_id("ja", NAMES["ja"]),
                                                            canonical=NAMES["ja"], source_language="ja", names={"en": "Prior Festival"})])
        self.assertIn("conflicts", self.submit(item, NAMES["en"])["errors"][0])
        self.assertEqual(self.names()["en"], "Prior Festival")

    def test_soft_wraps_remain_grounded_with_original_offsets(self):
        pages = pages_for()
        for page in pages:
            if page["language"] == "ja":
                page["text"] = "案内係：月虹音\n楽祭へ行こう。"
            elif page["language"] == "en":
                page["text"] = "Guide: Go to Moonbow\nFestival."
        items, _ = self.setup_packets(pages)
        item = next(i for i in items if i.language == "en" and i.kind != "discovery")
        self.assertEqual(self.submit(item, NAMES["en"])["errors"], [])
        with dbstore.connect(self.root) as conn:
            rows = dbstore._load_evidence_items(conn, termindex.make_term_id("ja", NAMES["ja"]))
        self.assertEqual(rows[0]["observed_surface"], "Moonbow\nFestival")
        self.assertTrue(all(r["semantic_guarantee"] is False for r in rows))

    def test_export_rotates_unresolved_batches_without_consuming_them(self):
        items, _ = self.setup_packets()
        first = ar.export_for_agent(self.root, Path(self.temp.name) / "first.txt", limit=2).read_text(encoding="utf-8")
        second = ar.export_for_agent(self.root, Path(self.temp.name) / "second.txt", limit=2).read_text(encoding="utf-8")
        self.assertNotEqual(first, second)
        self.assertEqual(len(ar.load_queue(self.root)), len(items))

    def test_discovery_validates_entire_response_before_enqueuing_any_child(self):
        items, _ = self.setup_packets(candidates=[])
        result = ar.submit_judgments(self.root, [dict(id=items[0].id, decision="accept",
                                                    terms=[NAMES["ja"], "Imaginary Hall"])])
        self.assertTrue(result["errors"])
        self.assertFalse(any(i.kind != "discovery" for i in ar.load_queue(self.root)))

    def test_namesake_speaker_is_never_used_as_term_offset(self):
        pages = pages_for()
        for page in pages:
            if page["language"] == "en":
                page["text"] = "Moonbow Festival: Visit Moonbow Festival."
            elif page["language"] == "ja":
                page["text"] = "月虹音楽祭：月虹音楽祭へ行こう。"
        items, _ = self.setup_packets(pages)
        item = next(i for i in items if i.kind != "discovery" and i.language == "en")
        self.assertEqual(self.submit(item, NAMES["en"])["errors"], [])
        with dbstore.connect(self.root) as conn:
            rows = dbstore._load_evidence_items(conn, termindex.make_term_id("ja", NAMES["ja"]))
        self.assertGreater(rows[0]["start"], len("Moonbow Festival:"))
        self.assertGreater(rows[0]["source_start"], len("月虹音楽祭：") - 1)

    def test_untrusted_page_and_official_v1_flag_cannot_be_borrowed(self):
        items, _ = self.setup_packets()
        item = next(i for i in items if i.kind != "discovery" and i.language == "en")
        with dbstore.connect(self.root) as conn:
            conn.execute("UPDATE web_pages SET trust='D' WHERE language='en'")
            conn.commit()
        self.assertIn("unverified", self.submit(item, NAMES["en"])["errors"][0])
        with dbstore.connect(self.root) as conn:
            conn.execute("UPDATE web_pages SET trust='B' WHERE language='en'")
            conn.commit()
        record = termindex.TermRecord(id=termindex.make_term_id("ja", NAMES["ja"]), canonical=NAMES["ja"],
                                      source_language="ja", names={"ja":NAMES["ja"]}, official=True)
        dbstore.upsert_terms(self.root, [record])
        self.assertIn("official flag", self.submit(item, NAMES["en"])["errors"][0])

    def test_single_character_content_word_is_discoverable(self):
        pages = pages_for()
        for page in pages:
            if page["language"] == "ja":
                page["text"] = "案内係：歌を聴こう。"
        items, _ = self.setup_packets(pages, candidates=[])
        result = ar.submit_judgments(self.root, [dict(id=items[0].id, decision="accept", terms=["歌"])])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["discovered_terms"], 1)

    def test_sql_discovery_resume_and_failure_leave_other_work_intact(self):
        items, _ = self.setup_packets(candidates=[], version=2)
        judge = dict(id=items[0].id, decision="accept", terms=[NAMES["ja"]])
        first = ar.submit_judgments(self.root, [judge])
        self.assertEqual(first["followup_packets"], 5)
        queued = ar.load_queue(self.root)
        self.assertEqual(len([item for item in queued if item.kind != "discovery"]), 4)
        audits = [item for item in queued if item._context.get("source_boundary_audit")]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]._context["source_boundary_audit"]["terminal_parent_id"], items[0].id)
        pending = {i.id for i in ar.load_queue(self.root)}
        second = ar.submit_judgments(self.root, [judge])
        self.assertEqual(second["unknown"], 0)
        self.assertEqual(second["followup_packets"], 0)
        self.assertEqual({i.id for i in ar.load_queue(self.root)}, pending)

    def test_methodology_replace_is_reused_as_acceptance_not_rejection(self):
        item = ar.make_review_item("用語", "en", [])
        ar.enqueue(self.root, [item])
        ar.submit_judgments(self.root, [dict(id=item.id, decision="replace", value="term")])
        result = ar.apply_methodology_batch(self.root, {"用語":{"en":["term"]}})
        self.assertEqual(result["settled"], {"用語":{"en":"term"}})

    def test_atomic_write_retries_only_transient_windows_lock_failures(self):
        denied = PermissionError("temporary lock")
        denied.winerror = 5
        with patch.object(ar.os, "replace", side_effect=[denied, None]) as replace, patch.object(ar.time, "sleep"):
            ar._atomic_write_text(Path(self.temp.name) / "queue.txt", "complete content")
            self.assertEqual(replace.call_count, 2)
        with patch.object(ar.os, "replace", side_effect=denied) as replace, patch.object(ar.time, "sleep"):
            with self.assertRaises(PermissionError):
                ar._atomic_write_text(Path(self.temp.name) / "queue.txt", "complete content")
            self.assertEqual(replace.call_count, 6)

    def test_algorithm_accepted_status_does_not_hide_agent_audit(self):
        pages = pages_for()
        groups = termindex.group_pages_by_story(pages)
        items, _ = ap._prepare_scrub_review(self.root, groups, sorted(groups), [NAMES["ja"]],
                     {"slot_decisions":[dict(term=NAMES["ja"],language="en",status="accepted",value=NAMES["en"])]},
                     "ja", ["en"])
        self.assertEqual(sum(i.kind != "discovery" for i in items), 1)

    def test_store_certificate_refusal_rolls_back_and_keeps_the_packet(self):
        from sekaisync import term_slots
        items, _ = self.setup_packets(version=2)
        item = next(i for i in items if i.kind != "discovery" and i.language == "en")
        with patch.object(term_slots, "corpus_verifier", return_value=lambda *_:None):
            result = self.submit(item, NAMES["en"])
        self.assertEqual(result["applied_slots"], 0)
        self.assertIn("certificate", result["errors"][0])
        self.assertIn(item.id, {i.id for i in ar.load_queue(self.root)})
        self.assertEqual(self.names(), {})

    def test_automatic_rescrape_keeps_reviewed_values_and_evidence(self):
        items, _ = self.setup_packets()
        item = next(i for i in items if i.kind != "discovery" and i.language == "en")
        self.assertEqual(self.submit(item, NAMES["en"])["applied_slots"], 1)
        rec = termindex.TermRecord(id=termindex.make_term_id("ja", NAMES["ja"]), canonical=NAMES["ja"],
                                   source_language="ja", names={"en":"Wrong candidate"}, evidence=[])
        self.assertEqual(ap._retain_reviewed_records(self.root, [rec]), 1)
        self.assertEqual(rec.names["en"], NAMES["en"])
        self.assertEqual(len(rec.evidence), 2)
        result = dict(accepted={NAMES["ja"]:{"names":{"en":"Wrong candidate"}}},
                      slot_decisions=[dict(term=NAMES["ja"],language="en",value="Wrong candidate",status="accepted"),
                                      dict(term=NAMES["ja"],language="ko",value=NAMES["ko"],status="pending")])
        self.assertEqual(ap._retain_reviewed_scrub_slots(self.root,result,"ja"), 1)
        self.assertEqual([d["language"] for d in result["slot_decisions"]], ["ko"])
        self.assertEqual(self.names()["en"], NAMES["en"])


if __name__ == "__main__":
    unittest.main()
