"""Native whole-value UI policy through normal serializers and public consumers."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, crawler, dbstore
from sekaisync import occurrence_store as ledger, span_subjects, termindex as ti, webindex
from sekaisync.core import SekaiSyncCore


class WordingBodyPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        dbstore.initialize_new_store(self.store, target_version=3)

    def pages(self, source, target, key="NATIVE_UI_TEST"):
        pages = [crawler.altsource_sv_record_page(dict(wordingKey=key, value=value), "wordings", region)
                 for region, value in [("en", source), ("cn", target)]]
        self.assertEqual(pages[0].text, source)
        self.assertEqual(pages[1].text, target)
        for page in pages:
            self.assertTrue(getattr(page, "wording_identity", None))
        webindex.save_web_pages(self.store, pages[0].source, pages, write_categories=False, rewrite_index=False)
        loaded = ti.load_pages(self.store)
        inserted = [p for p in loaded if p.get("wording_identity", {}).get("key") == key]
        self.assertEqual({p["text"] for p in inserted}, {source, target})
        self.assertEqual({p["id"] for p in inserted}, {p.id for p in pages})
        self.assertTrue(all(p.get("wording_identity") and p.get("wording_provenance") for p in loaded))
        return loaded

    def counts(self):
        with dbstore.connect(self.store) as conn:
            return dict(terms=conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0],
                        historical=len(ledger._read_relations(conn, current_only=False)),
                        current=len(ledger._read_relations(conn)))

    def prepare(self, pages):
        groups = ti.group_pages_by_story(pages)
        items, metadata = ap._prepare_scrub_review(self.store, groups, sorted(groups), [], {}, "en", ["zh_hans"])
        ar.enqueue(self.store, items)
        return items, metadata

    def export(self, name):
        path = self.root / name
        ar.export_for_agent(self.store, path, limit=0)
        return [json.loads(line[len("context: "):]) for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("context: ")]

    def submit(self, answer):
        return ar.submit_judgments(self.store, ar.parse_judgments_text(json.dumps(answer, ensure_ascii=True)))

    @staticmethod
    def parts(value, start=0):
        return [dict(start=start, end=start + len(value), exact=value)]

    def source_answer(self, item, value, start=0):
        return dict(id=item.id, decision="accept", subjects=[dict(kind="literal", canonical=value,
                    evidence_id=item._context["rows"][0]["id"], segments=self.parts(value, start))])

    def target_answer(self, item, value):
        row, = item._context["rows"]
        return dict(id=item.id, decision="accept", relations=[dict(evidence_id=row["id"],
                    source_segments=item._context["subject"]["source"]["segments"],
                    target_segments=self.parts(value), kind="lexical", sense_key="synthetic-ui",
                    sense_gloss="Synthetic mechanical fixture; no real translation certification.",
                    rationale="Explicit synthetic paired-body observation.")])

    def pipeline(self, source, target, selected=None, fallback=False):
        pages = self.pages(source, target)
        item, = self.prepare(pages[:1] if fallback else pages)[0]
        visible = self.export("source.txt")
        self.assertTrue(visible)
        row, = item._context["rows"]
        self.assertEqual((row["source"]["start"], row["source"]["end"], row["source"]["text"]),
                         (0, len(source), source))
        self.assertTrue(row["source"]["complete"])
        self.assertIn("wording_body", row["source"])
        selected = source if selected is None else selected
        self.assertEqual(SekaiSyncCore(self.store).term_lookup(selected), [])
        outcome = self.submit(self.source_answer(item, selected, source.index(selected)))
        self.assertEqual(outcome["errors"], [])
        self.assertEqual(outcome["accepted"], 1)
        self.assertEqual(self.counts(), dict(terms=0, historical=0, current=0))
        child, = [i for i in ar.load_queue(self.store) if i._context.get("task") == "occurrence"]
        self.export("target.txt")
        row, = child._context["rows"]
        self.assertEqual(row["target"]["text"], target)
        self.assertEqual((row["target"]["start"], row["target"]["end"]), (0, len(target)))
        self.assertTrue(row["target"]["complete"])
        if fallback:
            self.assertTrue(child._context["fallback"]["source_complete_page"])
            self.assertTrue(child._context["fallback"]["target_complete_page"])
            self.assertEqual(child._context["fallback"]["unshown_target_regions"], [])
        self.assertEqual(SekaiSyncCore(self.store).term_lookup(selected), [])
        outcome = self.submit(self.target_answer(child, target))
        self.assertEqual(outcome["errors"], [])
        self.assertEqual(outcome["accepted"], 1)
        self.assertEqual(outcome["applied_slots"], 0)
        self.assertEqual(self.counts(), dict(terms=0, historical=1, current=1))
        core = SekaiSyncCore(self.store)
        result = core.term_penetrate(selected, story_key=row["story_key"], languages=["zh_hans"])
        self.assertIsNotNone(result)
        self.assertEqual(result["per_language"]["zh_hans"]["term"], target)
        self.assertEqual(result["per_language"]["zh_hans"]["sentence"], target)
        hits = core.term_lookup(selected, source_language="en", languages=["zh_hans"])
        self.assertEqual(len(hits), 1)
        position = next(p for p in hits[0]["positions"] if p["language"] == "zh_hans")
        self.assertEqual(position["term"], target)
        return pages, child, selected

    def test_colon_complete_source_target_and_public_queries(self):
        self.pipeline("Status: build confidence", "\u72b6\u6001\uff1a\u589e\u5f3a\u4fe1\u5fc3")

    def test_lf_complete_source_target_and_public_queries(self):
        self.pipeline("build\nconfidence", "\u589e\u5f3a\n\u4fe1\u5fc3")

    def test_crlf_complete_source_target_and_public_queries(self):
        self.pipeline("build\r\nconfidence", "\u589e\u5f3a\r\n\u4fe1\u5fc3")

    def test_start_end_blank_lines_and_public_raw_sentence(self):
        self.pipeline("\n\nbuild\n\nconfidence\n\n", "\n\n\u589e\u5f3a\n\n\u4fe1\u5fc3\n\n")

    def test_placeholder_and_literal_braces(self):
        self.pipeline("Value: {0}\nLiteral {brace}", "\u6570\u503c\uff1a{0}\n\u5b57\u9762 {brace}")

    def test_long_source_all_characters_and_tail(self):
        self.pipeline("Intro: " + "x " * 3000 + "tail action", "\u5165\u53e3\uff1a" + "\u957f\u503c" * 3000,
                      selected="tail action")

    def test_fallback_whole_ui_beyond_expansion_budget(self):
        self.pipeline("Intro: " + "x " * 12500 + "tail action", "\u5165\u53e3\uff1a" + "\u957f\u503c" * 12500,
                      selected="tail action", fallback=True)

    def test_structural_family_does_not_publish_semantics(self):
        first = self.pages("Close", "\u5173\u95ed", "NATIVE_ID_A")
        second = self.pages("Close", "\u5173\u95ed", "NATIVE_ID_B")
        groups = ti.group_pages_by_story(first + [p for p in second if p["id"] not in {v["id"] for v in first}])
        self.assertEqual(len(groups), 2)
        self.assertTrue(all(len(by) == 2 for by in groups.values()))
        self.assertEqual(SekaiSyncCore(self.store).term_lookup("Close"), [])
        self.assertEqual(self.counts(), dict(terms=0, historical=0, current=0))

    def test_dialogue_does_not_merge_or_accept_donated_ui_marker(self):
        donor = self.pages("One UI", "\u5355\u4e00\u754c\u9762")
        text = "Alice: first turn\nBob: second turn"
        ordinary = dict(id="web:fixture:en:event_story:811:1", source="fixture", kind="event_story",
                        language="en", trust="B", text=text)
        dbstore.upsert_web_pages(self.store, "fixture", [ordinary])
        item, = self.prepare([ordinary])[0]
        self.export("dialogue.txt")
        self.assertEqual(len(item._context["rows"]), 2)
        result = self.submit(self.source_answer(item, text))
        self.assertEqual(result["accepted"], 0)
        self.assertTrue(result["errors"])
        ordinary["wording_identity"] = deepcopy(donor[0]["wording_identity"])
        ordinary["wording_provenance"] = deepcopy(donor[0]["wording_provenance"])
        with self.assertRaises(ValueError):
            dbstore.upsert_web_pages(self.store, "fixture", [ordinary])
            self.prepare([ordinary])

    def test_forged_dialogue_domain_cannot_donate_raw_record(self):
        donor = self.pages("One UI", "\u5355\u4e00\u754c\u9762")[0]
        text = "Alice: first turn\nBob: second turn"
        forged = dict(donor, id="web:fixture:en:event_story:812:1", source="fixture", text=text)
        forged["wording_identity"] = deepcopy(donor["wording_identity"])
        forged["wording_identity"].update(legacy_id=forged["id"], source=forged["source"],
                                            value_sha256=hashlib.sha256(text.encode()).hexdigest())
        with self.assertRaises(ValueError):
            dbstore.upsert_web_pages(self.store, "fixture", [forged])
            self.prepare([forged])

    def test_missing_or_tampered_metadata_rejects_old_packet(self):
        pages = self.pages("Status: closed", "\u72b6\u6001\uff1a\u5173\u95ed")
        item, = self.prepare(pages)[0]
        self.export("source.txt")
        for tamper in ("missing", "provenance"):
            with self.subTest(tamper=tamper):
                altered = deepcopy(pages[0])
                if tamper == "missing":
                    altered.pop("wording_identity")
                    altered.pop("wording_provenance")
                else:
                    altered["wording_provenance"] = {"fabricated": "unrelated record"}
                dbstore.upsert_web_pages(self.store, altered["source"], [altered])
                result = self.submit(self.source_answer(item, pages[0]["text"]))
                self.assertEqual(result["accepted"], 0)
                self.assertTrue(result["errors"])
                dbstore.upsert_web_pages(self.store, pages[0]["source"], [pages[0]])

    def test_view_marker_tampering_rejects_even_after_rehashing_packet(self):
        pages = self.pages("Status: closed", "\u72b6\u6001\uff1a\u5173\u95ed")
        item, = self.prepare(pages)[0]
        self.export("source.txt")
        view = deepcopy(item._context["rows"][0]["source"])
        view["wording_body"]["family"] = "wordings:key-sha256:" + "0" * 64
        with dbstore.connect(self.store) as conn:
            with self.assertRaises(ValueError):
                ap._validate_view(conn, view, {})

    def test_stale_source_packet_has_no_receipt(self):
        pages = self.pages("Status: closed", "\u72b6\u6001\uff1a\u5173\u95ed")
        item, = self.prepare(pages)[0]
        self.export("old-source.txt")
        changed = deepcopy(pages[0])
        changed["text"] = "Status: open"
        dbstore.upsert_web_pages(self.store, changed["source"], [changed])
        result = self.submit(self.source_answer(item, pages[0]["text"]))
        self.assertEqual(result["accepted"], 0)
        self.assertTrue(result["errors"])
        self.assertNotIn(item.id, ap._receipts(self.store))

    def test_fresh_interpreter_consumer_and_stale_target_history(self):
        pages, item, selected = self.pipeline("Status: build\nconfidence", "\u72b6\u6001\uff1a\u589e\u5f3a\n\u4fe1\u5fc3")
        story = item._context["rows"][0]["story_key"]
        code = ("import json,sys; from pathlib import Path; from sekaisync.core import SekaiSyncCore; "
                "v=json.loads(sys.argv[1]); r=SekaiSyncCore(Path(v['store'])).term_penetrate(v['query'], "
                "story_key=v['story'], languages=['zh_hans']); "
                "assert r and r['per_language']['zh_hans']['term']==v['target'], r; print('PASS')")
        payload = json.dumps(dict(store=str(self.store), query=selected, story=story, target=pages[1]["text"]))
        child = subprocess.run([sys.executable, "-B", "-c", code, payload], cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        changed = deepcopy(pages[1])
        changed["text"] += "!"
        dbstore.upsert_web_pages(self.store, changed["source"], [changed])
        self.assertIsNone(SekaiSyncCore(self.store).term_penetrate(selected, story_key=story, languages=["zh_hans"]))
        self.assertEqual(self.counts(), dict(terms=0, historical=1, current=0))

    def test_blank_and_missing_target_values_preserve_family_and_pending_gap(self):
        from sekaisync import wording_identity
        records = [dict(wordingKey="DEBT_VALID", value="Close"), dict(wordingKey="DEBT_BLANK", value=" \r\n"),
                   dict(wordingKey="DEBT_MISSING")]
        wording_identity._validate_records(records, "cn")
        for index, target_record in enumerate(records[1:]):
            with self.subTest(target_record=target_record):
                key = target_record["wordingKey"]
                source = crawler.altsource_sv_record_page(dict(wordingKey=key, value="Status: closed"), "wordings", "en")
                target = crawler.altsource_sv_record_page(target_record, "wordings", "cn")
                self.assertTrue(target.wording_identity["no_expression"])
                self.assertTrue(target.untranslated)
                webindex.save_web_pages(self.store, source.source, [source, target], write_categories=False, rewrite_index=False)
                pages = [p for p in ti.load_pages(self.store) if p["wording_identity"]["key"] == key]
                family = ti.page_story_key(vars(source))
                self.assertEqual(ti.page_story_key(vars(target)), family)
                groups = ti.group_pages_by_story(pages)
                self.assertEqual(set(groups[family]), {"en"})
                debt, = groups.wording_debts
                self.assertEqual(debt["reason"], "wording_no_expression")
                self.assertEqual(debt["no_expression_reason"], target.wording_identity["no_expression_reason"])
                item, = self.prepare(pages)[0]
                self.export("debt-source-" + str(index) + ".txt")
                outcome = self.submit(self.source_answer(item, source.text))
                self.assertEqual(outcome["errors"], [])
                subject_id = ap._receipts(self.store)[item.id]["review_context"]["rows"][0]["story_key"]
                gaps = [i for i in ar.load_queue(self.store) if i._context.get("task") == "subject_gap"
                        and i._context["subject"]["source"]["story_key"] == subject_id]
                self.assertEqual(len(gaps), 1)
                self.assertEqual(gaps[0]._context["gap"]["story_key"], family)
                self.assertEqual(gaps[0]._context["gap"]["reason"], "wording_no_expression")
                self.assertFalse([i for i in ar.load_queue(self.store) if i._context.get("task") == "occurrence"])
                refusal = self.submit(dict(id=gaps[0].id, decision="accept", relations=[]))
                self.assertEqual(refusal["accepted"], 0)
                self.assertTrue(refusal["errors"])
                self.assertEqual(self.counts(), dict(terms=0, historical=0, current=0))

    @staticmethod
    def profile_copy(page, source):
        copied = deepcopy(page)
        copied["source"] = source
        copied["id"] = page["id"].replace("web:" + page["source"] + ":", "web:" + source + ":", 1)
        copied["wording_identity"].update(source=source, legacy_id=copied["id"])
        return copied

    def test_equal_profile_versions_collapse_deterministically_with_contributors(self):
        pages = self.pages("Close", "\u5173\u95ed")
        additional = vars(crawler.altsource_sv_record_page(dict(wordingKey="NATIVE_UI_TEST", value="Close", note="profile-extra"),
                                                          "wordings", "en")).copy()
        duplicate = self.profile_copy(additional, "altsource_sv_second")
        normal = ti.group_pages_by_story([*pages, duplicate])
        reverse = ti.group_pages_by_story([duplicate, *reversed(pages)])
        self.assertIsInstance(normal, dict)
        self.assertEqual(normal, reverse)
        self.assertEqual(json.loads(json.dumps(normal)), json.loads(json.dumps(dict(normal))))
        self.assertEqual(normal.wording_contributors, reverse.wording_contributors)
        family = ti.page_story_key(pages[0])
        self.assertEqual(normal[family]["en"]["id"], pages[0]["id"])
        contribution, = normal.wording_contributors
        self.assertEqual(len(contribution["contributors"]), 2)
        self.assertEqual(len({p["record_sha256"] for p in contribution["contributors"]}), 2)
        items, metadata = ap._prepare_scrub_review(self.store, normal, list(normal), [], {}, "en", ["zh_hans"])
        scope = ap._read_scope(self.store, metadata["scope_id"])
        self.assertEqual(scope["_wording_contributors"], normal.wording_contributors)
        self.assertNotIn("_wording_debts", scope)
        self.assertEqual(len(items), 1)

    def test_conflicting_profile_language_is_local_gap_and_other_family_survives(self):
        pages = self.pages("Close", "\u5173\u95ed", "NATIVE_CONFLICT")
        other = self.pages("Open", "\u6253\u5f00", "NATIVE_INDEPENDENT")
        existing = {p["id"]: p for p in other}
        different = vars(crawler.altsource_sv_record_page(dict(wordingKey="NATIVE_CONFLICT", value="\u6253\u5f00"),
                                                         "wordings", "cn")).copy()
        different = self.profile_copy(different, "altsource_sv_second")
        dbstore.upsert_web_pages(self.store, different["source"], [different])
        inputs = [*existing.values(), different]
        groups = ti.group_pages_by_story(inputs)
        reverse = ti.group_pages_by_story(reversed(inputs))
        self.assertEqual(groups, reverse)
        self.assertEqual(groups.wording_debts, reverse.wording_debts)
        family = ti.page_story_key(pages[0])
        other_family = ti.page_story_key(next(p for p in other if p["wording_identity"]["key"] == "NATIVE_INDEPENDENT"))
        self.assertEqual(set(groups[family]), {"en"})
        self.assertEqual(set(groups[other_family]), {"en", "zh_hans"})
        items, metadata = ap._prepare_scrub_review(self.store, groups, list(groups), [], {}, "en", ["zh_hans"])
        ar.enqueue(self.store, items)
        self.export("conflict-source.txt")
        self.assertEqual(len(items), 2)
        scope = ap._read_scope(self.store, metadata["scope_id"])
        self.assertEqual(scope["_wording_debts"], groups.wording_debts)
        selected = next(i for i in items if i._context["rows"][0]["story_key"] == family)
        outcome = self.submit(self.source_answer(selected, "Close"))
        self.assertEqual(outcome["errors"], [])
        gap, = [i for i in ar.load_queue(self.store) if i._context.get("task") == "subject_gap"]
        self.assertEqual(gap._context["gap"]["reason"], "conflicting_wording_value_versions")
        self.assertEqual(gap._context["gap"]["language"], "zh_hans")
        self.assertEqual(len(gap._context["gap"]["contributors"]), 2)
        self.assertTrue(all(c["page_id"] and len(c["value_version_sha256"]) == 64
                            for c in gap._context["gap"]["contributors"]))

    def test_ordinary_anchor_keeps_legacy_eight_column_replay(self):
        text = "Alice: first turn"
        page = dict(id="web:fixture:en:event_story:813:1", source="fixture", kind="event_story",
                    language="en", trust="B", text=text)
        dbstore.upsert_web_pages(self.store, page["source"], [page])
        view = dict(source=page["source"], page_id=page["id"], language="en", start=0, end=len(text), text=text,
                    sha256=hashlib.sha256(text.encode()).hexdigest(), complete=True)
        anchor = ledger._anchor(view, "event:813:1", self.parts("first turn", text.index("first turn")))
        statements = []
        with dbstore.connect(self.store) as conn:
            conn.set_trace_callback(statements.append)
            ledger._validate_anchor(conn, anchor, {})
        reads = [sql for sql in statements if "FROM web_pages" in sql]
        self.assertEqual(len(reads), 1, reads)
        self.assertNotIn("SELECT *", reads[0].upper())


if __name__ == "__main__":
    unittest.main()
