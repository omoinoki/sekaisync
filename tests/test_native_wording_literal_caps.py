"""Standalone synthetic native literal caps using integrated production code."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as ap, agent_review as ar, crawler, dbstore
from sekaisync import occurrence_store as ledger, source_audits, span_subjects
from sekaisync import termindex, webindex, wording_identity
from sekaisync.core import SekaiSyncCore


class NativeWordingLiteralCapTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.sequence = 0

    @staticmethod
    def raw(length, newline="\n"):
        unit = "Synthetic exact native wording \U0001f680 body." + newline
        value = (unit * (length // len(unit) + 2))[:length]
        return value[:-1] + "." if value.endswith("\r") else value

    def fixture(self, raw, version=3, region="en"):
        self.sequence += 1
        store = self.base / str(self.sequence)
        if version == 1:
            dbstore.initialize(store)
        else:
            dbstore.initialize_new_store(store, target_version=version)
        self.assertEqual(dbstore.inspect_schema(store).version, str(version))
        source_language, target_region, target_language = (("ja", "en", "en") if region == "jp"
                                                           else ("en", "jp", "ja"))
        target = ("Synthetic target wording." if region == "jp"
                  else "\u5408\u6210\u306e\u5bfe\u8c61\u6587\U0001f680\u3002\r\n" * 3)
        key = "P0_SYNTHETIC_NATIVE_LITERAL_CAP_" + str(self.sequence)
        pages = [crawler.altsource_sv_record_page(dict(wordingKey=key, value=value), "wordings", language_region)
                 for language_region, value in ((region, raw), (target_region, target))]
        webindex.save_web_pages(store, pages[0].source, pages, write_categories=False, rewrite_index=False)
        loaded = termindex.load_pages(store)
        self.assertTrue(all(termindex._page_usable(page, page["language"]) for page in loaded))
        groups = termindex.group_pages_by_story(loaded)
        items, _ = ap._prepare_scrub_review(store, groups, sorted(groups), [], {}, source_language, [target_language])
        root, = [item for item in items if item._context.get("task") == "discovery"]
        ar.enqueue(store, [root])
        return dict(store=store, root=root, page=next(page for page in loaded if page["language"] == source_language),
                    raw=raw, target=target, region=region, key=key, version=version,
                    source_language=source_language, target_language=target_language)

    @staticmethod
    def entry(data, segments=None, canonical=None):
        raw = data["raw"]
        parts = [dict(start=0, end=len(raw), exact=raw)] if segments is None else segments
        return dict(kind="literal", evidence_id=data["root"]._context["rows"][0]["id"],
                    canonical=raw if canonical is None else canonical, segments=parts)

    @staticmethod
    def submit(data, item, **fields):
        payload = dict(id=item.id, decision="accept", rationale="Synthetic transport regression, not semantic gold.", **fields)
        return ar.submit_judgments(data["store"], ar.parse_judgments_text(json.dumps(payload, ensure_ascii=True)))

    @staticmethod
    def state(data):
        with dbstore.connect(data["store"]) as conn:
            relations = ([row[0] for row in conn.execute("SELECT payload_json FROM scraper_relations ORDER BY id")]
                         if conn.execute("SELECT 1 FROM sqlite_master WHERE name='scraper_relations'").fetchone() else [])
            decisions = (conn.execute("SELECT COUNT(*) FROM review_decisions").fetchone()[0]
                         if conn.execute("SELECT 1 FROM sqlite_master WHERE name='review_decisions'").fetchone() else 0)
            terms = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
        return ap._receipts(data["store"]), relations, decisions, terms

    def accept(self, data, item, **fields):
        result = self.submit(data, item, **fields)
        self.assertEqual(result["accepted"], 1, result)
        self.assertEqual(result["errors"], [], result)

    def reject(self, data, item, **fields):
        before = self.state(data)
        result = self.submit(data, item, **fields)
        self.assertEqual(result["accepted"], 0, result)
        self.assertTrue(result["errors"], result)
        self.assertEqual(self.state(data), before)

    def accepted_root(self, data, entry=None):
        entry = self.entry(data) if entry is None else entry
        row = data["root"]._context["rows"][0]
        subject = span_subjects._literal(row["source"], row["story_key"], entry["canonical"], entry["segments"])
        self.accept(data, data["root"], subjects=[entry])
        audit, = [item for item in ar.load_queue(data["store"]) if source_audits._is_audit(item._context)]
        child, = [item for item in ar.load_queue(data["store"]) if item._context.get("task") == "occurrence"]
        self.assertEqual(child._context["subject"], subject)
        return audit, child, subject

    def relation(self, data, child, subject):
        return dict(evidence_id=child._context["rows"][0]["id"], source_segments=subject["source"]["segments"],
                    target_segments=[dict(start=0, end=len(data["target"]), exact=data["target"])], kind="lexical",
                    sense_key="synthetic-native-cap", sense_gloss="Synthetic transport only.",
                    rationale="Exact raw source and target fixture.")

    def complete(self, data, entry=None):
        audit, child, subject = self.accepted_root(data, entry)
        self.accept(data, audit, subjects=[])
        self.accept(data, child, relations=[self.relation(data, child, subject)])
        return subject

    @staticmethod
    def drop_audit(data, audit):
        if data["version"] == 1:
            ar._write_queue(data["store"], [item for item in ar.load_queue(data["store"]) if item.id != audit.id])
        else:
            with dbstore.connect(data["store"]) as conn:
                conn.execute("DELETE FROM review_queue WHERE item_id=?", (audit.id,))
                conn.commit()

    def consumers(self, data, canonical, available):
        before = self.state(data)
        core = SekaiSyncCore(data["store"])
        story = data["root"]._context["rows"][0]["story_key"]
        query = core.query(canonical, include_web=False)["terms"]
        lookup = core.term_lookup(canonical, source_language=data["source_language"],
                                  languages=[data["source_language"], data["target_language"]])
        penetrate = core.term_penetrate(canonical, story_key=story,
                                        languages=[data["source_language"], data["target_language"]])
        self.assertEqual(len(query), int(available))
        self.assertEqual(len(lookup), int(available))
        self.assertEqual(penetrate is not None, available)
        if available:
            self.assertEqual(query[0]["canonical"], canonical)
            self.assertEqual(lookup[0]["names"][data["source_language"]], canonical)
            self.assertEqual(penetrate["per_language"][data["source_language"]]["term"], canonical)
            self.assertEqual(penetrate["per_language"][data["target_language"]]["sentence"], data["target"])
        self.assertEqual(self.state(data), before)

    def test_normal_overcap_receipts_genuine_audit_recovery_and_consumers(self):
        for version in (1, 2, 3):
            for length, newline in ((1817, "\n"), (7108, "\r\n")):
                with self.subTest(version=version, length=length):
                    data = self.fixture(self.raw(length, newline), version)
                    view = data["root"]._context["rows"][0]["source"]
                    self.assertEqual((view["text"], view["start"], view["end"], view["complete"]),
                                     (data["raw"], 0, length, True))
                    audit, child, subject = self.accepted_root(data)
                    marker = audit._context["source_boundary_audit"]
                    self.assertEqual(marker["terminal_parent_id"], data["root"].id)
                    self.assertEqual(marker["excluded_subject_ids"], [subject["id"]])
                    self.assertEqual(marker["inherited_subjects"], [dict(id=subject["id"], kind="literal",
                                     canonical=data["raw"], source_segments=subject["source"]["segments"])])
                    self.drop_audit(data, audit)
                    recovered = source_audits._ensure(data["store"])
                    self.assertEqual((recovered["added"], recovered["invalid"]), (1, 0))
                    restored, = [item for item in ar.load_queue(data["store"]) if source_audits._is_audit(item._context)]
                    self.assertEqual((restored.id, restored._context), (audit.id, audit._context))
                    self.reject(data, restored, subjects=[self.entry(data)])
                    self.accept(data, restored, subjects=[])
                    self.accept(data, child, relations=[self.relation(data, child, subject)])
                    self.assertEqual(len(ap._receipts(data["store"])), 3)
                    with dbstore.connect(data["store"]) as conn:
                        relation, = ledger._read_relations(conn)
                        self.assertEqual(relation["grounding"]["context"]["subject"], subject)
                        self.assertEqual(relation["source"], subject["source"])
                        self.assertFalse(relation["semantic_guarantee"])
                    self.consumers(data, data["raw"], True)
                    self.consumers(data, data["raw"].replace("\r\n", " ").replace("\n", " "), False)
                    self.assertEqual(source_audits._ensure(data["store"])["added"], 0)
                    self.assertEqual(self.state(data)[3], 0)

    def test_native_nonbmp_adjacent_interior_offsets_remain_code_points(self):
        raw = "PRE:" + "A\U0001f680B" * 701 + ":POST"
        data = self.fixture(raw)
        parts = [dict(start=4, end=1006, exact=raw[4:1006]),
                 dict(start=1006, end=len(raw) - 5, exact=raw[1006:-5])]
        canonical = raw[4:-5]
        subject = self.complete(data, self.entry(data, parts, canonical))
        self.assertEqual(subject["source"]["segments"], parts)
        self.assertEqual(subject["gap_text"], [""])
        self.assertGreater(len(raw.encode("utf-16-le")) // 2, len(raw))
        self.consumers(data, canonical, True)

    def test_admitted_native_jp_han_only_overcap_source(self):
        data = self.fixture("\u5408\u6210\u6f22\u5b57\u6587\u8a00\u5883\u754c" * 228, region="jp")
        self.assertTrue(wording_identity._allows_native_ja_han_ui(data["page"], "ja"))
        self.complete(data)
        self.consumers(data, data["raw"], True)

    def test_native_1800_1801_boundaries_and_ordinary_cap_stay_distinct(self):
        for native in (True, False):
            for length in (1800, 1801):
                with self.subTest(native=native, length=length):
                    raw = "A" * length
                    data = self.fixture(raw)
                    if native:
                        self.accept(data, data["root"], subjects=[self.entry(data)])
                        continue
                    page = dict(source="synthetic", id="web:synthetic:en:event_story:994:1",
                                kind="event_story", language="en", text=raw, trust="A")
                    dbstore.upsert_web_pages(data["store"], page["source"], [page])
                    view = ap._raw_region(page, 0, length)
                    story = termindex.page_story_key(page)
                    row = dict(id="synthetic-ordinary", story_key=story, source=view, targets={})
                    subject = span_subjects._literal(view, story, raw, [dict(start=0, end=length, exact=raw)])
                    with dbstore.connect(data["store"]) as conn:
                        if length == 1800:
                            self.assertEqual(ap._validate_subject_in_rows(conn, subject, [row]), subject)
                        else:
                            with self.assertRaisesRegex(ValueError, "1800"):
                                ap._validate_subject_in_rows(conn, subject, [row])

    def test_legacy_80_cap_and_ordinary_window_budgets_remain_unchanged(self):
        self.assertTrue(ap._valid_surface("A" * 80))
        self.assertFalse(ap._valid_surface("A" * 81))
        self.assertEqual((ap._TURN_CHARS, ap._DISCOVERY_CHARS), (1800, 3600))
        for length in (80, 81):
            with self.subTest(length=length):
                data = self.fixture("A" * length)
                if length == 80:
                    self.accept(data, data["root"], terms=[data["raw"]])
                else:
                    self.reject(data, data["root"], terms=[data["raw"]])
        page = dict(source="synthetic", id="web:synthetic:en:event_story:994:1", kind="event_story",
                    language="en", text="A" * 7108, trust="A")
        groups = termindex.group_pages_by_story([page])
        rows = ap._scope_windows(groups, sorted(groups), "en", [])
        self.assertTrue(rows)
        self.assertTrue(all(len(row["source"]["text"]) <= 1800 for row in rows))
        self.assertTrue(all(not row["source"]["complete"] for row in rows))

    def test_native_lf_crlf_not_normalized_and_whitespace_gaps_not_closed(self):
        for newline in ("\n", "\r\n"):
            with self.subTest(newline=repr(newline)):
                data = self.fixture("A" * 1000 + newline + "B" * 1000)
                self.reject(data, data["root"], subjects=[self.entry(data, canonical=data["raw"].replace(newline, " "))])
                parts = [dict(start=0, end=1000, exact="A" * 1000),
                         dict(start=1000 + len(newline), end=len(data["raw"]), exact="B" * 1000)]
                self.reject(data, data["root"], subjects=[self.entry(data, parts, "A" * 1000 + "B" * 1000)])

    def test_other_c0_controls_get_no_native_exception(self):
        for control in ("\t", "\0", "\1", "\r", "\v", "\f"):
            with self.subTest(control=ord(control)):
                data = self.fixture("A" * 1801 + control)
                self.reject(data, data["root"], subjects=[self.entry(data)])

    def test_forged_ordinary_native_marker_cannot_authorize_overcap_literal(self):
        data = self.fixture("A" * 1801)
        page = dict(source="synthetic", id="web:synthetic:en:event_story:994:1", kind="event_story",
                    language="en", text=data["raw"], trust="A")
        dbstore.upsert_web_pages(data["store"], page["source"], [page])
        view = ap._raw_region(page, 0, len(page["text"]))
        marker = deepcopy(data["root"]._context["rows"][0]["source"]["wording_body"])
        marker.update(source=page["source"], page_id=page["id"])
        view["wording_body"] = marker
        self.assertTrue(wording_identity._view_policy(view))
        story = termindex.page_story_key(page)
        subject = span_subjects._literal(view, story, data["raw"], self.entry(data)["segments"])
        with dbstore.connect(data["store"]) as conn, self.assertRaises(ValueError):
            ap._validate_subject_in_rows(conn, subject, [dict(id="forged", story_key=story, source=view, targets={})])

    def test_incomplete_integer_complete_and_clipped_native_views_cannot_authorize(self):
        data = self.fixture("A" * 2500)
        original = data["root"]._context["rows"][0]
        raw = data["raw"]
        parts = [dict(start=100, end=2001, exact=raw[100:2001])]
        subject = span_subjects._literal(original["source"], original["story_key"], raw[100:2001], parts)
        for fault in ("incomplete", "integer-complete", "clipped", "forged-policy"):
            with self.subTest(fault=fault):
                row = deepcopy(original)
                if fault == "incomplete": row["source"]["complete"] = False
                elif fault == "integer-complete": row["source"]["complete"] = 1
                elif fault == "clipped": row["source"].update(start=1, text=raw[1:])
                else: row["source"]["wording_body"]["policy"] = "forged-policy"
                with dbstore.connect(data["store"]) as conn, self.assertRaises(ValueError):
                    ap._validate_subject_in_rows(conn, subject, [row])

    def test_changed_persisted_native_admission_rejects_normal_submission(self):
        for flag, value in (("trust", "D"), ("auxiliary", True), ("overlay", True), ("untranslated", True),
                            ("content_language_mismatch", True), ("asset_mismatch", "synthetic mismatch")):
            with self.subTest(flag=flag):
                data = self.fixture(self.raw(1817))
                page = deepcopy(data["page"])
                page[flag] = value
                dbstore.upsert_web_pages(data["store"], page["source"], [page])
                self.reject(data, data["root"], subjects=[self.entry(data)])

    def test_persisted_native_region_and_reversible_token_provenance_replayed(self):
        for fault in ("region", "token", "mapping"):
            with self.subTest(fault=fault):
                data = self.fixture(self.raw(1817))
                with dbstore.connect(data["store"]) as conn:
                    extra = json.loads(conn.execute("SELECT extra_json FROM web_pages WHERE id=?", (data["page"]["id"],)).fetchone()[0])
                    if fault == "region": extra["wording_identity"]["region"] = "jp"
                    elif fault == "token": extra["wording_provenance"]["adapter_value_token"] += " "
                    else: extra["wording_provenance"]["decoded_unicode_to_token_unicode"][0]["end"] += 1
                    conn.execute("UPDATE web_pages SET extra_json=? WHERE id=?", (json.dumps(extra), data["page"]["id"]))
                    conn.commit()
                self.reject(data, data["root"], subjects=[self.entry(data)])

    def test_postreceipt_native_body_change_rejects_pending_and_stale_consumers(self):
        for durable in (False, True):
            with self.subTest(durable=durable):
                data = self.fixture(self.raw(1817))
                audit, child, subject = self.accepted_root(data)
                if durable:
                    self.accept(data, audit, subjects=[])
                    self.accept(data, child, relations=[self.relation(data, child, subject)])
                else:
                    self.drop_audit(data, audit)
                page = crawler.altsource_sv_record_page(dict(wordingKey=data["key"], value="X" + data["raw"][1:]), "wordings", "en")
                wording_identity._adapt(page, dict(wordingKey=data["key"], value="X" + data["raw"][1:]), "en", legacy_id=data["page"]["id"])
                dbstore.upsert_web_pages(data["store"], page.source, [webindex.web_page_to_dict(page)])
                if durable:
                    self.consumers(data, data["raw"], False)
                else:
                    self.reject(data, child, relations=[self.relation(data, child, subject)])
                    recovery = source_audits._ensure(data["store"])
                    self.assertEqual((recovery["added"], recovery["pending"], recovery["invalid"]), (0, 0, 1))

    def test_saved_scope_and_rehashed_packet_substitutions_rejected(self):
        for fault in ("scope", "rehashed-packet"):
            with self.subTest(fault=fault):
                data = self.fixture(self.raw(1817))
                audit, child, subject = self.accepted_root(data)
                if fault == "scope":
                    path = ap._scope_path(data["store"], data["root"]._context["scope_id"])
                    scope = json.loads(path.read_text(encoding="utf-8"))
                    scope["windows"][0]["source"]["complete"] = False
                    path.write_text(json.dumps(scope, sort_keys=True) + "\n", encoding="utf-8")
                    self.reject(data, audit, subjects=[])
                    self.reject(data, child, relations=[self.relation(data, child, subject)])
                    self.drop_audit(data, audit)
                    recovered = source_audits._ensure(data["store"])
                    self.assertEqual((recovered["added"], recovered["invalid"]), (0, 1))
                else:
                    context = deepcopy(child._context)
                    context["rows"][0]["source"]["complete"] = False
                    forged = ap._item(child.term, child.language, child.candidates, "occurrence", context, "Synthetic rehashed substitution")
                    self.assertNotEqual(forged.id, child.id)
                    ar.enqueue(data["store"], [forged])
                    self.reject(data, forged, relations=[self.relation(data, child, subject)])

    def test_rendered_contract_names_narrow_exception_and_source_boundary_policy(self):
        data = self.fixture(self.raw(1817))
        output = ar.export_for_agent(data["store"], self.base / "synthetic-contract.txt", limit=0)
        rendered = output.read_text(encoding="utf-8")
        for text in ("discovery_native_literal_contract:", "fully validated complete native wording whole-value row",
                     "exact adjacent-fragment literal", "actual displayed raw value's full length",
                     "never enlarges ordinary dialogue windows or legacy terms", "permits no normalized alias",
                     "discovery_boundaries:", "outermost terminal sentence punctuation", "of the complete envelope",
                     "grammatical question/force particles", "source-attested grammatical attachment and scope",
                     "Keep independent prior responses and neighboring independent main clauses separate",
                     "regardless of punctuation or semantic relatedness", "actually inherited selections"):
            with self.subTest(text=text):
                self.assertIn(text, rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
