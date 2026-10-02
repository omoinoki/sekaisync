"""Small fail-fast fixtures for the read-only P0 corpus census."""
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import census_scraper_corpus as cc


class CorpusCensusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.root / "store"
        (self.store / "kb").mkdir(parents=True)
        self.db = self.store / "kb/sekaisync.db"
        self.as_of = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.start = 1000
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE web_pages(source TEXT,id TEXT,kind TEXT,language TEXT,text TEXT,canonical_key TEXT,"
                         "text_hash TEXT,untranslated INTEGER,content_language_mismatch INTEGER,PRIMARY KEY(source,id))")
            conn.execute("CREATE TABLE entity_region_facts(entity_id TEXT,region TEXT,facts_json TEXT,"
                         "retrieval_json TEXT,source TEXT,version TEXT,PRIMARY KEY(entity_id,region))")
            conn.commit()
        self.baseline = self.root / "baseline.json"
        self.baseline.write_text(json.dumps({"tasks": [{"story_keys": ["event:1:1"]}]}), encoding="utf-8")
        for event in (1, 2, 3):
            for language in cc.LANGUAGES:
                self.add_page(f"event_story:{event}:1", language)
                self.add_event(event, language)

    def add_page(self, logical_key, language, source="altsource_ms", text="A: first\nB: second", untranslated=0):
        kind, suffix = logical_key.split(":", 1)
        page_id = f"web:{source}:{language}:{kind}:{suffix}"
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT OR REPLACE INTO web_pages(source,id,kind,language,text,canonical_key,text_hash,untranslated,content_language_mismatch) VALUES(?,?,?,?,?,?,?,?,?)",
                         (source, page_id, kind, language, text, f"{kind}:{language}:{suffix}",
                          hashlib.sha256(text.encode()).hexdigest(), untranslated, 0))
            conn.commit()

    def add_event(self, event, language, condition_type="none", episode_time=None):
        region = cc.REGIONS[language]
        folder = self.root / "raw" / region
        folder.mkdir(parents=True, exist_ok=True)
        events = [{"id": n, "startAt": self.start} for n in (1, 2, 3)]
        stories = [{"id": n, "eventId": n, "eventStoryEpisodes": [{"id": n * 100, "eventStoryId": n,
                    "episodeNo": 1, "scenarioId": f"event_{n}_01", "releaseConditionId": 1,
                    **({"releaseAt": episode_time} if episode_time is not None else {})}]} for n in (1, 2, 3)]
        values = {"events": events, "eventStories": stories, "releaseConditions": [
            {"id": 1, "releaseConditionType": condition_type}]}
        hashes = {}
        for name, value in values.items():
            path = folder / (name + ".json")
            path.write_text(json.dumps(value), encoding="utf-8")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        retrieval = {"table": "events", "path": str(folder / "events.json"), "sha256": hashes["events"],
                     "generation": "fixture", "field_sources": {"outline": {"table": "eventStories",
                         "path": str(folder / "eventStories.json"), "sha256": hashes["eventStories"]}}}
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("INSERT OR REPLACE INTO entity_region_facts VALUES(?,?,?,?,?,?)",
                         (f"event:{event}", region, json.dumps({"startAt": self.start}), json.dumps(retrieval), "fixture", "v1"))
            conn.commit()

    def verifier(self):
        conn = cc.open_read_only(self.db)
        self.addCleanup(conn.close)
        return cc.ReleaseVerifier(conn, self.as_of)

    def test_identity_aliases_and_opaque_ids(self):
        page = {"kind": "event_story", "id": "web:altsource_sv:cn:event_story:9:2", "source": "altsource_sv",
                "language": "zh_hans", "canonical_key": "event_story:zh-cn:9:2"}
        self.assertEqual(cc.logical_identity(page), ("event_story:9:2", "canonical"))
        page.update(kind="wordings", id="web:altsource_sv:cn:wordings:0005359fdcbe", canonical_key="")
        self.assertIn(":opaque:altsource_sv:zh_hans:", cc.logical_identity(page)[0])
        page.update(kind="event_story", id="web:altsource_ms_translation:ja-jp:event_story:9:2:ja",
                    source="altsource_ms_translation", language="ja")
        self.assertEqual(cc.logical_identity(page), ("event_story:9:2", "auxiliary_locale_suffix"))

    def test_actual_event_hash_and_record_must_match(self):
        verifier = self.verifier()
        self.assertEqual(verifier.chapter("event_story:2:1", "en")["status"], "released_in_verified_masterdata")
        raw = self.root / "raw/en/events.json"
        raw.write_text("[]", encoding="utf-8")
        self.assertIn("raw_hash_mismatch", self.verifier().chapter("event_story:2:1", "en")["reasons"])

    def test_stored_event_timestamp_mismatch_is_unknown(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE entity_region_facts SET facts_json=? WHERE entity_id=? AND region=?",
                         (json.dumps({"startAt": 2000}), "event:2", "en"))
            conn.commit()
        self.assertIn("event_record_mismatch", self.verifier().chapter("event_story:2:1", "en")["reasons"])

    def test_chapter_gate_not_implied_by_event_start(self):
        self.add_event(2, "en", episode_time=int(self.as_of.timestamp() * 1000) + 1)
        verifier = self.verifier()
        self.assertEqual(verifier.chapter("event_story:2:1", "en")["status"], "not_yet_released")
        verifier.conn.close()
        self.add_event(2, "en", condition_type="unrecognized")
        item = self.verifier().chapter("event_story:2:1", "en")
        self.assertEqual(item["status"], "unknown_release")
        self.assertIn("unsupported_unlock_condition", item["reasons"])

    def test_missing_episode_stays_unknown(self):
        self.assertIn("missing_episode_record", self.verifier().chapter("event_story:2:2", "en")["reasons"])

    def test_census_preserves_versions_bad_reasons_and_unknown_denominator(self):
        self.add_page("event_story:2:1", "en", source="altsource_sv", text="different localized body")
        self.add_page("event_story:2:1", "ko", source="bad-source", text="", untranslated=1)
        self.add_page("wordings:7", "ja")
        original = hashlib.sha256(self.db.read_bytes()).hexdigest()
        output = self.root / "out"
        result = cc.census(self.store, output, self.baseline, self.as_of, holdout_count=2)
        self.assertEqual(original, hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual(result["page_count"], 18)
        self.assertEqual(result["logical_unit_count"], 4)
        self.assertIn("wordings", result["unknown_release_domains"])
        units = [json.loads(line) for line in (output / "logical-units.jsonl").read_text(encoding="utf-8").splitlines()]
        unit = next(item for item in units if item["logical_key"] == "event_story:2:1")
        self.assertEqual(unit["languages"]["en"]["distinct_versions"], 2)
        self.assertEqual(unit["languages"]["ko"]["good"], 1)
        self.assertEqual(unit["languages"]["ko"]["pages"], 2)
        bad = json.loads((output / "bad-page-matrix.json").read_text(encoding="utf-8"))
        self.assertEqual({row["reason"] for row in bad}, {"empty_text", "untranslated"})
        frozen = json.loads((output / "holdout-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual({cc.content_family(s["story_key"]) for s in frozen["stories"]}, {"event:2", "event:3"})
        self.assertTrue(frozen["candidate_free"])
        self.assertTrue(frozen["gold_free"])
        self.assertNotIn('"text":', (output / "holdout-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(cc.census(self.store, output, self.baseline, self.as_of, 2), result)

    def test_frozen_holdout_cannot_silently_change(self):
        out = self.root / "out"
        cc.census(self.store, out, self.baseline, self.as_of, 2)
        self.add_page("event_story:2:1", "ja", text="changed page")
        with self.assertRaisesRegex(ValueError, "Existing blind holdout changed"):
            cc.census(self.store, out, self.baseline, self.as_of, 2)

    def test_partial_census_restarts_with_same_interface(self):
        out = self.root / "interrupted"
        with patch.object(cc, "_page_summaries", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                cc.census(self.store, out, self.baseline, self.as_of, 2)
        self.assertFalse((out / "summary.json").exists())
        self.assertFalse(list(out.glob("census-*.sqlite")))
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)
        digest = result["holdout_manifest_sha256"]
        self.assertEqual(cc.census(self.store, out, self.baseline, self.as_of, 2)["holdout_manifest_sha256"], digest)

    def test_nonprimary_overlay_cannot_make_five_language_complete(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE web_pages ADD COLUMN auxiliary INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE web_pages ADD COLUMN overlay INTEGER DEFAULT 0")
            conn.execute("UPDATE web_pages SET auxiliary=1,overlay=1 WHERE language=? AND id LIKE ?", ("ko", "%:1:1"))
            conn.commit()
        out = self.root / "out"
        cc.census(self.store, out, self.baseline, self.as_of, 2)
        units = [json.loads(line) for line in (out / "logical-units.jsonl").read_text(encoding="utf-8").splitlines()]
        baseline = next(row for row in units if row["logical_key"] == "event_story:1:1")
        self.assertFalse(baseline["local_five_language_complete"])
        self.assertEqual(baseline["nonprimary_only_languages"], ["ko"])

    def native_holdout(self):
        texts = {"ja": "人物：月虹音楽祭に行きます。\n人物：楽しいです。",
                 "en": "Person: We are going to the Moonbow Festival.\nPerson: It is fun.",
                 "zh_hans": "人物：我们要去月虹音乐节。\n人物：这很开心。",
                 "zh_hant": "人物：我們要去月虹音樂節。\n人物：這很開心。",
                 "ko": "인물：달무리 음악제에 갑니다.\n인물：즐겁습니다."}
        for event in (2, 3):
            for language, text in texts.items():
                self.add_page(f"event_story:{event}:1", language, text=text)
        out = self.root / "native-holdout"
        cc.census(self.store, out, self.baseline, self.as_of, 2)
        return out

    def test_symmetric_preparation_resumes_without_duplicate_or_old_terms(self):
        from sekaisync import agent_packets as ap, agent_review as ar

        out = self.native_holdout()
        first = cc.prepare_holdout_review(out, "smoke")
        self.assertEqual(first["imported_pages"], 10)
        self.assertEqual(first["story_language_jobs"], 5)
        self.assertEqual(first["discovery_packets"], 5)
        self.assertEqual(first["current_terms"], 0)
        store = Path(first["store"])
        item = ar.load_queue(store)[0]
        submitted = ar.submit_judgments(store, [{"id": item.id, "decision": "accept", "terms": [],
                                                "rationale": "fixture completion", "generalize": None}])
        self.assertFalse(submitted["errors"])
        self.assertIn(item.id, ap._receipts(store))
        restarted = cc.prepare_holdout_review(out, "smoke")
        self.assertEqual(restarted["enqueue"]["added"], 0)
        self.assertEqual(restarted["enqueue"]["skipped_settled"], 1)
        self.assertEqual(restarted["enqueue"]["queue_size"], 5)
        pending = ar.load_queue(store)
        audits = [child for child in pending if "source_boundary_audit" in child._context]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0]._context["source_boundary_audit"]["terminal_parent_id"], item.id)
        self.assertEqual(audits[0]._context["rows"], item._context["rows"])
        self.assertEqual(len(pending), 5)

    def test_preparation_rejects_changed_frozen_body_before_import(self):
        out = self.native_holdout()
        manifest = json.loads((out / "holdout-manifest.json").read_text(encoding="utf-8"))
        path = out / manifest["stories"][0]["pages"]["ja"]["local_body_file"]
        path.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "Frozen holdout page changed"):
            cc.prepare_holdout_review(out, "smoke")
        self.assertFalse((out / "holdout-store-smoke").exists())

    def test_read_only_connection_rejects_write(self):
        with closing(cc.open_read_only(self.db)) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM web_pages")

    def test_expected_inventory_includes_chapters_with_zero_acquired_languages(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM web_pages WHERE id LIKE ?", ("%:3:1",))
            conn.commit()
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        out = self.root / "expected"
        result = cc.census(self.store, out, self.baseline, self.as_of, 1)
        inventory = result["expected_event_inventory"]
        self.assertEqual(inventory["observed_chapter_units"], 2)
        self.assertEqual(inventory["masterdata_inventory_chapter_units"], 3)
        self.assertEqual(inventory["union_chapter_units"], 3)
        self.assertEqual(inventory["counts"]["all_five_pages_absent"], 1)
        self.assertEqual(inventory["counts"]["all_five_released_pages_absent"], 1)
        rows = [json.loads(line) for line in (out / "expected-event-chapters.jsonl").read_text(encoding="utf-8").splitlines()]
        absent = next(row for row in rows if row["logical_key"] == "event_story:3:1")
        self.assertEqual(absent["origin"], "masterdata_only")
        self.assertEqual(set(absent["expected_inventory_languages"]), set(cc.LANGUAGES))
        self.assertEqual(set(absent["acquisition_states"].values()), {"missing"})
        manifest_digest = hashlib.sha256((out / "holdout-manifest.json").read_bytes()).hexdigest()
        self.assertEqual(cc.refresh_expected_events(self.store, out), inventory)
        self.assertEqual(hashlib.sha256((out / "holdout-manifest.json").read_bytes()).hexdigest(), manifest_digest)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_raw_inventory_does_not_depend_on_having_every_event_fact(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM entity_region_facts WHERE entity_id=?", ("event:3",))
            conn.execute("DELETE FROM web_pages WHERE id LIKE ?", ("%:3:1",))
            conn.commit()
        out = self.root / "expected-no-facts"
        inventory = cc.census(self.store, out, self.baseline, self.as_of, 1)["expected_event_inventory"]
        self.assertEqual(inventory["masterdata_inventory_chapter_units"], 3)
        self.assertEqual(inventory["counts"]["all_five_pages_absent"], 1)
        self.assertEqual(inventory["counts"]["all_five_released_pages_absent"], 0)
        self.assertEqual(inventory["release_status_counts"]["unknown_release"], 1)

    def test_raw_inventory_retains_hash_failures_without_inventing_expected_language(self):
        (self.root / "raw/en/eventStories.json").write_text("[]", encoding="utf-8")
        expected, sources = cc._expected_event_inventory(self.verifier())
        self.assertNotIn("en", expected["event_story:2:1"])
        self.assertEqual(len(expected), 3)
        self.assertTrue(any("raw_hash_mismatch" in row["errors"] for row in sources))

    def test_reference_occurrences_exclude_labels_and_english_subwords(self):
        english = "airport: an airport and airports.\r\nPerson: airport."
        found = cc._reference_occurrences(english, "airport", "en")
        self.assertEqual(len(found), 2)
        self.assertEqual([row["physical_line"] for row in found], [1, 2])
        self.assertTrue(all(english[row["start"]:row["end"]] == "airport" for row in found))
        korean = "공항：공항에서 만나요."
        self.assertEqual(len(cc._reference_occurrences(korean, "공항", "ko")), 1)

    def machine_reference_fixture(self, english_reframing=False):
        out = self.native_holdout()
        manifest = json.loads((out / "holdout-manifest.json").read_text(encoding="utf-8"))
        story = manifest["stories"][0]
        private = out / "holdout-reviewer-only"
        private.mkdir()
        surfaces = {"ja": "月虹音楽祭", "en": "the Moonbow Festival", "zh_hans": "月虹音乐节",
                    "zh_hant": "月虹音樂節", "ko": "달무리 음악제"}
        concept = {"id": "festival", "meaning": "The named festival", "categories": ["proper_name"],
                   "note": "Independent fixture selection", "surfaces": surfaces, "relation": "lexical_equivalent"}
        if english_reframing:
            concept["english_pair_relation"] = "contextual_reframing_not_strict_lexical_equivalence"
        selection = {"reviewer": "fixture", "story_key": story["story_key"], "machine_reference_not_human_gold": True,
                     "proposal_or_tokenizer_candidates_read": False, "full_source_read": {language: 2 for language in cc.LANGUAGES},
                     "scope": "One preselected content concept, not the whole vocabulary", "concepts": [concept]}
        (private / "selection.json").write_text(json.dumps(selection), encoding="utf-8")
        return out, story, surfaces

    def test_reference_freeze_is_reproducible_and_cannot_be_silently_replaced(self):
        out, _, _ = self.machine_reference_fixture()
        first = cc.freeze_machine_reference(out)
        self.assertEqual(first["term_slots"], 5)
        self.assertEqual(first["directed_relations"], 20)
        self.assertEqual(first, cc.freeze_machine_reference(out))
        path = out / "holdout-reviewer-only/selection.json"
        selection = json.loads(path.read_text(encoding="utf-8"))
        selection["scope"] = "Changed independent selection"
        path.write_text(json.dumps(selection), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Frozen machine reference changed"):
            cc.freeze_machine_reference(out)

    def test_source_evaluation_requires_explicit_variants_not_arbitrary_substrings(self):
        out, story, surfaces = self.machine_reference_fixture()
        cc.freeze_machine_reference(out)
        path = out / "source-proposal.json"
        proposal = {"story_key": story["story_key"], "surfaces": {language: [surface] for language, surface in surfaces.items()}}
        proposal["surfaces"]["en"] = ["Moonbow Festival"]
        path.write_text(json.dumps(proposal), encoding="utf-8")
        first = cc.evaluate_machine_source(out, path)
        self.assertEqual(first["covered_slots"], 4)
        self.assertEqual(first["missing_slots"], 1)
        (out / "holdout-reviewer-only/accepted-source-variants.json").write_text(json.dumps({"rules": [
            {"annotation_id": "festival:en", "surface": "Moonbow Festival", "reason": "Omitting the definite article is acceptable."}]}), encoding="utf-8")
        second = cc.evaluate_machine_source(out, path)
        self.assertEqual(second["covered_slots"], 5)
        self.assertEqual(second["accepted_variant_slots"], 1)
        self.assertEqual(second["semantic_precision"], "not_measured_from_sparse_reference")
        proposal["surfaces"]["en"] = ["Moonbow"]
        path.write_text(json.dumps(proposal), encoding="utf-8")
        self.assertEqual(cc.evaluate_machine_source(out, path)["covered_slots"], 4)

    def target_reference_fixture(self, english_reframing=False):
        out, story, surfaces = self.machine_reference_fixture(english_reframing)
        cc.freeze_machine_reference(out)
        private = out / "holdout-reviewer-only"
        (private / "accepted-target-concepts.json").write_text(json.dumps({"concept_mappings": {"festival": "festival-proposal"}}), encoding="utf-8")
        proposal = {"story_key": story["story_key"], "reviewer_labels_used": False,
                    "concepts": [{"key": "festival-proposal", "surfaces": surfaces}]}
        bodies = {language: (out / story["pages"][language]["local_body_file"]).read_bytes().decode("utf-8") for language in cc.LANGUAGES}
        tasks, judgments = [], []
        for source in cc.LANGUAGES:
            for target in cc.LANGUAGES:
                if source == target:
                    continue
                spans, segments = {}, {}
                for role, language in (("source", source), ("target", target)):
                    body, surface = bodies[language], surfaces[language]
                    start = body.index(surface)
                    window_start, window_end = max(0, start - 1), start + len(surface) + 1
                    spans[role] = {"page_id": story["pages"][language]["page_id"], "sha256": story["pages"][language]["text_sha256"],
                                   "start": window_start, "end": window_end, "text": body[window_start:window_end]}
                    segments[role + "_segments"] = [{"start": start, "end": start + len(surface), "exact": surface}]
                task_id = source + ":" + target
                tasks.append({"id": task_id, "language": target, "term": surfaces[source], "review_context": {"source_language": source,
                              "task": "occurrence", "rows": [{"id": "evidence", **spans}]}})
                judgments.append({"id": task_id, "decision": "accept", "relations": [{"evidence_id": "evidence",
                                  "sense_key": "festival-proposal", "kind": "lexical", **segments}]})
        paths = [out / "target-proposal.json", out / "target-judgments.json", out / "target-tasks.json"]
        for path, value in zip(paths, (proposal, judgments, tasks)):
            path.write_text(json.dumps(value), encoding="utf-8")
        return out, paths

    def test_target_evaluation_uses_absolute_positions_and_detects_wrong_offsets(self):
        out, paths = self.target_reference_fixture()
        first = cc.evaluate_machine_target(out, *paths)
        self.assertEqual(first["counts"]["correct_lexical"], 20)
        self.assertEqual(first["counts"]["same_independent_primary_occurrences"], 20)
        judgments = json.loads(paths[1].read_text(encoding="utf-8"))
        judgments[0]["relations"][0]["target_segments"][0]["start"] += 1
        paths[1].write_text(json.dumps(judgments), encoding="utf-8")
        second = cc.evaluate_machine_target(out, *paths)
        self.assertEqual(second["counts"]["correct_lexical"], 19)
        self.assertEqual(second["counts"]["wrong_position_or_invalid_evidence"], 1)

    def test_target_evaluation_does_not_promote_contextual_paraphrase_to_lexical_equality(self):
        out, paths = self.target_reference_fixture(english_reframing=True)
        first = cc.evaluate_machine_target(out, *paths)
        self.assertEqual(first["counts"]["correct_lexical"], 12)
        self.assertEqual(first["counts"]["wrong_relation_kind"], 8)
        judgments = json.loads(paths[1].read_text(encoding="utf-8"))
        for judgment in judgments:
            if "en" in judgment["id"].split(":"):
                judgment["relations"][0]["kind"] = "paraphrase"
        paths[1].write_text(json.dumps(judgments), encoding="utf-8")
        second = cc.evaluate_machine_target(out, *paths)
        self.assertEqual(second["counts"]["correct_contextual_paraphrase"], 8)

    def test_label_assisted_diagnostic_reuses_tasks_without_changing_blind_snapshot(self):
        from sekaisync import agent_review as ar

        out, paths = self.target_reference_fixture()
        private = out / "holdout-reviewer-only"
        (private / "accepted-target-concepts.json").write_text(json.dumps({"concept_mappings": {}}), encoding="utf-8")
        cc.evaluate_machine_target(out, *paths)
        proposal = json.loads(paths[0].read_text(encoding="utf-8"))
        source_path = out / "source-proposal.json"
        source_path.write_text(json.dumps({"story_key": proposal["story_key"], "surfaces": {
            language: [surface] for language, surface in proposal["concepts"][0]["surfaces"].items()}}), encoding="utf-8")
        cc.evaluate_machine_source(out, source_path)
        before = (private / "target-evaluation.json").read_bytes()
        tasks = json.loads(paths[2].read_text(encoding="utf-8"))
        queue = [SimpleNamespace(to_dict=lambda task=task: task) for task in tasks]
        with patch.object(ar, "load_queue", return_value=queue):
            first = cc.prepare_label_assisted_diagnostic(out)
        self.assertEqual(first["prepared_judgments"], 20)
        self.assertEqual(first["existing_occurrence_tasks_reused"], 20)
        self.assertEqual(first["new_source_terms_added"], 0)
        self.assertEqual(first["new_tasks_created"], 0)
        self.assertEqual(first["blind_score_gain"], 0)
        with patch.object(ar, "load_queue", side_effect=AssertionError("Must reuse frozen task snapshot")):
            self.assertEqual(cc.prepare_label_assisted_diagnostic(out), first)
        self.assertEqual((private / "target-evaluation.json").read_bytes(), before)

    def test_label_assisted_diagnostic_leaves_missing_bounded_evidence_unresolved(self):
        from sekaisync import agent_review as ar

        out, paths = self.target_reference_fixture()
        private = out / "holdout-reviewer-only"
        (private / "accepted-target-concepts.json").write_text(json.dumps({"concept_mappings": {}}), encoding="utf-8")
        cc.evaluate_machine_target(out, *paths)
        proposal = json.loads(paths[0].read_text(encoding="utf-8"))
        source_path = out / "source-proposal.json"
        source_path.write_text(json.dumps({"story_key": proposal["story_key"], "surfaces": {
            language: [surface] for language, surface in proposal["concepts"][0]["surfaces"].items()}}), encoding="utf-8")
        cc.evaluate_machine_source(out, source_path)
        tasks = json.loads(paths[2].read_text(encoding="utf-8"))
        tasks[0]["review_context"]["rows"] = []
        queue = [SimpleNamespace(to_dict=lambda task=task: task) for task in tasks]
        with patch.object(ar, "load_queue", return_value=queue):
            report = cc.prepare_label_assisted_diagnostic(out)
        self.assertEqual(report["prepared_judgments"], 19)
        self.assertEqual(len(report["unresolved"]), 1)
        self.assertEqual(report["unresolved"][0]["reason"], "bounded_packet_does_not_cover_independent_occurrences")

    def span_reference_fixture(self):
        out, _, _ = self.machine_reference_fixture()
        path = out / "holdout-reviewer-only/selection.json"
        selection = json.loads(path.read_text(encoding="utf-8"))
        selection["schema"] = "sekaisync/p0-independent-machine-span-selection@2"
        selection["concepts"][0]["primary_lines"] = {language: 1 for language in cc.LANGUAGES}
        path.write_text(json.dumps(selection), encoding="utf-8")
        return out, path, selection

    def test_span_reference_retains_discontinuous_raw_fragments_and_excludes_gap(self):
        out, path, selection = self.span_reference_fixture()
        concept = selection["concepts"][0]
        concept["surfaces"]["en"] = "going ... Festival"
        concept["segments"] = {"en": [{"exact": "going", "line": 1}, {"exact": "Festival", "line": 1}]}
        path.write_text(json.dumps(selection), encoding="utf-8")
        first = cc.freeze_span_machine_reference(out, path)
        self.assertEqual(first["term_slots"], 5)
        self.assertEqual(first["directed_relations"], 20)
        self.assertEqual(first["discontinuous_annotations"], 1)
        reference = json.loads((path.parent / "reference.json").read_text(encoding="utf-8"))
        english = next(row for row in reference["annotations"] if row["language"] == "en")
        self.assertFalse(english["surface_is_contiguous"])
        group = english["span_groups"][0]
        self.assertEqual(group["gap_texts"], [" to the Moonbow "])
        self.assertEqual([row["exact"] for row in group["segments"]], ["going", "Festival"])
        self.assertEqual(first, cc.freeze_span_machine_reference(out, path))

    def test_span_reference_primary_line_is_not_automatic_first_match(self):
        old_out, old_path, selection = self.span_reference_fixture()
        for event in (2, 3):
            self.add_page(f"event_story:{event}:1", "en", text="Person: Moonbow Festival first.\nPerson: Moonbow Festival again.")
        out = self.root / "repeat-span"
        cc.census(self.store, out, self.baseline, self.as_of, 2)
        private = out / "holdout-reviewer-only"
        private.mkdir()
        selection["concepts"][0]["surfaces"]["en"] = "Moonbow Festival"
        selection["concepts"][0]["primary_lines"]["en"] = 2
        path = private / "selection.json"
        path.write_text(json.dumps(selection), encoding="utf-8")
        cc.freeze_span_machine_reference(out, path)
        reference = json.loads((private / "reference.json").read_text(encoding="utf-8"))
        english = next(row for row in reference["annotations"] if row["language"] == "en")
        self.assertEqual(len(english["span_groups"]), 2)
        self.assertEqual(english["primary_occurrence_index"], 1)
        self.assertEqual(english["span_groups"][1]["segments"][0]["physical_line"], 2)

    def test_span_reference_rejects_cross_turn_segments_and_wrong_reading_counts(self):
        out, path, selection = self.span_reference_fixture()
        selection["concepts"][0]["segments"] = {"en": [{"exact": "Moonbow", "line": 1}, {"exact": "fun", "line": 2}]}
        path.write_text(json.dumps(selection), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "one speaker turn"):
            cc.freeze_span_machine_reference(out, path)
        selection["full_source_read"]["en"] = 3
        path.write_text(json.dumps(selection), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "line count mismatch"):
            cc.freeze_span_machine_reference(out, path)

    def test_span_reference_distinguishes_contextual_specialization_from_lexical_equality(self):
        out, path, selection = self.span_reference_fixture()
        selection["concepts"][0]["localized_reframing_languages"] = ["en"]
        path.write_text(json.dumps(selection), encoding="utf-8")
        cc.freeze_span_machine_reference(out, path)
        reference = json.loads((path.parent / "reference.json").read_text(encoding="utf-8"))
        self.assertEqual(sum(row["strict_lexical_counterpart"] for row in reference["directed_relations"]), 12)
        self.assertTrue(all(not row["strict_lexical_counterpart"] for row in reference["directed_relations"]
                            if "en" in (row["source_language"], row["target_language"])))

    def span_requirement_fixture(self, discontinuous=False):
        out, path, selection = self.span_reference_fixture()
        concept = selection["concepts"][0]
        concept["localized_reframing_languages"] = ["en"]
        surfaces = dict(concept["surfaces"])
        if discontinuous:
            concept["surfaces"]["en"] = "going ... Festival"
            concept["segments"] = {"en": [{"exact": "going", "line": 1}, {"exact": "Festival", "line": 1}]}
            surfaces["en"] = "going to the Moonbow Festival"
        path.write_text(json.dumps(selection), encoding="utf-8")
        frozen = cc.freeze_span_machine_reference(out, path)
        proposal = {"story_key": selection["story_key"], "reviewer_labels_used": False,
                    "concepts": [{"key": "independent-intent", "surfaces": surfaces}]}
        proposal_path = out / "span-proposal.json"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        review = {"story_key": selection["story_key"], "machine_reference_not_human_gold": True,
                  "reference_sha256": frozen["reference_sha256"],
                  "proposal_sha256": hashlib.sha256(proposal_path.read_bytes()).hexdigest(),
                  "concepts": [{"reference_concept_id": "festival", "proposed_key": "independent-intent",
                                "semantic_intent": "aligned", "rationale": "Manually reviewed fixture intent",
                                "complete_surface_requirement": {language: True for language in cc.LANGUAGES}}]}
        review_path = path.parent / "semantic-adjudication.json"
        review_path.write_text(json.dumps(review), encoding="utf-8")
        return out, path.parent / "reference.json", proposal_path, review_path, review

    def test_semantic_table_is_not_an_occurrence_submission_and_kinds_are_separate(self):
        out, reference, proposal, review, _ = self.span_requirement_fixture()
        result = cc.evaluate_span_proposal_requirements(out, reference, proposal, review)
        self.assertEqual(result["complete_surface_requirement_directional_pairs"], 20)
        self.assertEqual(result["kind_aligned_complete_surface_requirement_pairs"], 12)
        self.assertFalse(result["occurrence_submissions_provided"])
        self.assertEqual(result["grounded_occurrence_success"], "not_measured")
        self.assertTrue(all(not row["occurrence_relation_submitted"] for row in result["directional_requirements"]))
        self.assertEqual(result, cc.evaluate_span_proposal_requirements(out, reference, proposal, review))

    def test_semantic_intent_cannot_replace_missing_predicate_expression(self):
        out, reference, proposal, review_path, review = self.span_requirement_fixture()
        review["concepts"][0]["complete_surface_requirement"]["en"] = False
        review_path.write_text(json.dumps(review), encoding="utf-8")
        result = cc.evaluate_span_proposal_requirements(out, reference, proposal, review_path)
        self.assertEqual(result["intent_represented_directional_requirements"], 20)
        self.assertEqual(result["complete_surface_requirement_annotations"], 4)
        self.assertEqual(result["complete_surface_requirement_directional_pairs"], 12)
        self.assertEqual(result["concept_counts"], {"intent_aligned_but_incomplete_surface_requirements": 1})

    def test_semantic_discontinuous_equivalence_does_not_raise_exact_expression_score(self):
        out, reference, proposal, review, _ = self.span_requirement_fixture(discontinuous=True)
        result = cc.evaluate_span_proposal_requirements(out, reference, proposal, review)
        self.assertEqual(result["complete_surface_requirement_annotations"], 5)
        self.assertEqual(result["exact_selected_expression_annotations"], 4)
        self.assertEqual(result["exact_selected_expression_pairs"], 12)
        english = next(row for row in result["annotations"] if row["language"] == "en")
        self.assertEqual(len(english["reference_primary_segments"]), 2)

    def test_semantic_adjudication_rejects_input_drift_and_denominator_deletion(self):
        out, reference, proposal, review_path, review = self.span_requirement_fixture()
        proposal.write_text(proposal.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "frozen-input hash mismatch"):
            cc.evaluate_span_proposal_requirements(out, reference, proposal, review_path)
        review["proposal_sha256"] = hashlib.sha256(proposal.read_bytes()).hexdigest()
        review["concepts"] = []
        review_path.write_text(json.dumps(review), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "entire independent concept denominator"):
            cc.evaluate_span_proposal_requirements(out, reference, proposal, review_path)

    def test_semantic_adjudication_cannot_claim_ungrounded_surface_complete(self):
        out, reference, proposal_path, review_path, review = self.span_requirement_fixture()
        proposal = json.loads(proposal_path.read_bytes())
        proposal["concepts"][0]["surfaces"]["en"] = "hallucinated source form"
        proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        review["proposal_sha256"] = hashlib.sha256(proposal_path.read_bytes()).hexdigest()
        review_path.write_text(json.dumps(review), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not present in the frozen body"):
            cc.evaluate_span_proposal_requirements(out, reference, proposal_path, review_path)

    def test_unproposed_semantic_concept_stays_in_annotation_and_pair_denominators(self):
        out, reference, proposal, review_path, review = self.span_requirement_fixture()
        review["concepts"] = [{"reference_concept_id": "festival", "proposed_key": None,
                               "semantic_intent": "not_proposed", "rationale": "Proposal does not express this selected intent"}]
        review_path.write_text(json.dumps(review), encoding="utf-8")
        result = cc.evaluate_span_proposal_requirements(out, reference, proposal, review_path)
        self.assertEqual(result["reference_annotations"], 5)
        self.assertEqual(result["reference_directional_requirements"], 20)
        self.assertEqual(result["intent_represented_directional_requirements"], 0)
        self.assertEqual(result["body_grounded_surface_slots"], 5)
        self.assertEqual(result["unmapped_proposal_keys"], ["independent-intent"])

    def unit_story_fixture(self, missing_scenario=None):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE web_pages ADD COLUMN url TEXT DEFAULT ''")
            conn.commit()
        for language in cc.LANGUAGES:
            folder = self.root / "raw" / cc.REGIONS[language]
            episodes = [{"id": 70000 + index, "chapterNo": 1, "episodeNo": index + 1,
                         "unitStoryEpisodeGroupId": 7, "scenarioId": f"idol_01_{index:02d}",
                         "assetbundleName": f"episode_{index}", "releaseConditionId": 2 if index == 2 else 1}
                        for index in range(3)]
            story = {"unit": "idol", "seq": 7, "chapters": [{"id": 7, "unit": "idol", "chapterNo": 1,
                      "assetbundleName": "idol-chapter", "episodes": episodes}]}
            path = folder / "unitStories.json"
            path.write_text(json.dumps([story]), encoding="utf-8")
            retrieval = {"table": "unitStories", "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "generation": "fixture"}
            with closing(sqlite3.connect(self.db)) as conn:
                conn.execute("INSERT OR REPLACE INTO entity_region_facts VALUES(?,?,?,?,?,?)",
                             ("unit_story:7", cc.REGIONS[language], json.dumps({"unit": "idol"}), json.dumps(retrieval), "fixture", "v1"))
                conn.commit()
            (folder / "releaseConditions.json").write_text(json.dumps([
                {"id": 1, "releaseConditionType": "none"},
                {"id": 2, "releaseConditionType": "unit_story", "releaseConditionTypeId": 70001}]), encoding="utf-8")
            (folder / "unitProfiles.json").write_text(json.dumps([{"unit": "idol", "seq": 7}]), encoding="utf-8")
            for episode in episodes:
                scenario = episode["scenarioId"]
                if scenario == missing_scenario:
                    continue
                self.add_page("unit_story:" + scenario, language)
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute("UPDATE web_pages SET url=? WHERE id=?", (f"https://example.test/{language}/story/unit/7/{scenario}/",
                                 f"web:altsource_ms:{language}:unit_story:{scenario}"))
                    conn.commit()

    def rewrite_unit_story_table(self, language, mutation):
        path = self.root / "raw" / cc.REGIONS[language] / "unitStories.json"
        rows = json.loads(path.read_bytes())
        mutation(rows)
        path.write_text(json.dumps(rows), encoding="utf-8")
        with closing(sqlite3.connect(self.db)) as conn:
            retrieval = json.loads(conn.execute("SELECT retrieval_json FROM entity_region_facts WHERE entity_id=? AND region=?",
                                   ("unit_story:7", cc.REGIONS[language])).fetchone()[0])
            retrieval["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            conn.execute("UPDATE entity_region_facts SET retrieval_json=? WHERE entity_id=? AND region=?",
                         (json.dumps(retrieval), "unit_story:7", cc.REGIONS[language]))
            conn.commit()

    def test_expected_unit_inventory_includes_all_missing_openings_without_false_release_claim(self):
        self.unit_story_fixture(missing_scenario="idol_01_00")
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        out = self.root / "unit-inventory"
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)["expected_unit_story_inventory"]
        self.assertEqual(result["observed_story_units"], 2)
        self.assertEqual(result["masterdata_inventory_story_units"], 3)
        self.assertEqual(result["counts"]["all_five_absent_opening_episodes"], 1)
        self.assertEqual(result["counts"]["all_five_unlock_definitions_reachable"], 3)
        self.assertEqual(result["counts"]["local_five_language_identity_verified_primary_complete"], 2)
        self.assertEqual(result["release_status_counts"], {"unknown_release": 3})
        self.assertEqual(result["counts"]["all_five_released_pages_absent"], 0)
        self.assertEqual(result["backfill_requested_locale_units"], 5)
        backfill = json.loads((out / "expected-unit-backfill-requests.json").read_bytes())
        self.assertTrue(all(row["ms_scenario_path"] == "scenario/unitstory/idol-chapter/idol_01_00.json"
                            and row["identity"]["episode_id"] == 70000 for row in backfill["requests"]))
        self.assertEqual(result, cc.refresh_expected_unit_stories(self.store, out))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_unit_unlock_predecessor_uses_exact_nested_episode_foreign_key(self):
        self.unit_story_fixture()
        with closing(cc.open_read_only(self.db)) as conn:
            verifier = cc.UnitStoryVerifier(conn, self.as_of)
            result = verifier.unlock("en", "idol_01_02")
            self.assertEqual(result["status"], "reachable_in_locally_hashed_masterdata")
            self.assertEqual(result["prerequisite_episode_ids"], [70001])
            self.assertEqual(result["prerequisite"]["scenario_id"], "idol_01_01")
            self.assertFalse(result["condition_retrieval_sealed"])
        path = self.root / "raw/en/releaseConditions.json"
        path.write_text(json.dumps([{"id": 1, "releaseConditionType": "none"},
                                   {"id": 2, "releaseConditionType": "unit_story", "releaseConditionTypeId": 99999}]), encoding="utf-8")
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.UnitStoryVerifier(conn, self.as_of).unlock("en", "idol_01_02")
        self.assertEqual(result["status"], "unknown_unlock")
        self.assertIn("missing_or_ambiguous_unit_story_prerequisite_id", result["reasons"])

    def test_unit_temporary_free_window_is_not_publication_or_unlock_expiration(self):
        self.unit_story_fixture()
        self.rewrite_unit_story_table("en", lambda rows: rows[0]["chapters"][0]["episodes"][2].update(
            limitedReleaseStartAt=1000, limitedReleaseEndAt=2000))
        self.rewrite_unit_story_table("ko", lambda rows: rows[0]["chapters"][0]["episodes"][2].update(
            limitedReleaseStartAt=int(self.as_of.timestamp() * 1000) + 1000,
            limitedReleaseEndAt=int(self.as_of.timestamp() * 1000) + 2000))
        with closing(cc.open_read_only(self.db)) as conn:
            verifier = cc.UnitStoryVerifier(conn, self.as_of)
            for language, state in (("en", "ended"), ("ko", "before")):
                result = verifier.release(language, "idol_01_02")
                self.assertEqual(result["status"], "unknown_release")
                self.assertEqual(result["temporary_free_access_window"]["state"], state)
                self.assertEqual(result["unlock"]["status"], "reachable_in_locally_hashed_masterdata")
                self.assertFalse(result["temporary_free_access_window"]["is_publication_evidence"])

    def test_explicit_sealed_unit_future_release_gate_overrides_existing_text(self):
        self.unit_story_fixture()
        self.rewrite_unit_story_table("en", lambda rows: rows[0]["chapters"][0]["episodes"][2].update(
            releaseAt=int(self.as_of.timestamp() * 1000) + 1000))
        result = cc.census(self.store, self.root / "unit-future", self.baseline, self.as_of, 2)["expected_unit_story_inventory"]
        self.assertEqual(result["release_status_counts"], {"unknown_release": 2, "contains_not_yet_released": 1})
        self.assertEqual(result["counts"]["local_five_language_identity_verified_primary_complete"], 3)

    def test_same_unit_scenario_with_different_raw_episode_identity_does_not_merge(self):
        self.unit_story_fixture()
        self.rewrite_unit_story_table("zh_hans", lambda rows: rows[0]["chapters"][0]["episodes"][1].update(id=79999))
        result = cc.census(self.store, self.root / "unit-conflict", self.baseline, self.as_of, 2)["expected_unit_story_inventory"]
        self.assertEqual(result["union_story_units"], 3)
        self.assertEqual(result["counts"]["cross_language_identity_conflict"], 1)
        self.assertEqual(result["counts"]["five_language_raw_identity_verified"], 2)
        self.assertEqual(result["counts"]["local_five_language_identity_verified_primary_complete"], 2)

    def test_unit_episode_publication_does_not_override_future_or_unknown_prerequisite(self):
        self.unit_story_fixture()
        self.rewrite_unit_story_table("en", lambda rows: rows[0]["chapters"][0]["episodes"][2].update(releaseAt=1000))
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.UnitStoryVerifier(conn, self.as_of).release("en", "idol_01_02")
        self.assertEqual(result["status"], "unknown_release")
        self.assertIn("prerequisite_unit_story_publication_not_proven", result["reasons"])
        self.rewrite_unit_story_table("en", lambda rows: rows[0]["chapters"][0]["episodes"][1].update(
            releaseAt=int(self.as_of.timestamp() * 1000) + 1000))
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.UnitStoryVerifier(conn, self.as_of).release("en", "idol_01_02")
        self.assertEqual(result["status"], "not_yet_released")
        self.rewrite_unit_story_table("en", lambda rows: rows[0]["chapters"][0]["episodes"][1].update(releaseAt=1000))
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.UnitStoryVerifier(conn, self.as_of).release("en", "idol_01_02")
        self.assertEqual(result["status"], "released_in_verified_masterdata")

    def test_unit_nested_parent_mismatch_is_reported_not_silently_identity_verified(self):
        self.unit_story_fixture()
        self.rewrite_unit_story_table("en", lambda rows: rows[0]["chapters"][0]["episodes"][1].update(chapterNo=2))
        result = cc.census(self.store, self.root / "unit-parent-mismatch", self.baseline, self.as_of, 2)["expected_unit_story_inventory"]
        self.assertEqual(result["invalid_identity_records"], 1)
        self.assertEqual(result["union_story_units"], 3)
        self.assertEqual(result["counts"]["five_language_raw_identity_verified"], 2)
        self.assertEqual(result["page_identity_audit_counts"]["unknown"], 1)

    def test_unsupported_unit_unlock_condition_is_unknown_even_with_primary_text(self):
        self.unit_story_fixture()
        path = self.root / "raw/en/releaseConditions.json"
        path.write_text(json.dumps([{"id": 1, "releaseConditionType": "none"},
                                   {"id": 2, "releaseConditionType": "account_level", "releaseConditionTypeLevel": 10}]), encoding="utf-8")
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.UnitStoryVerifier(conn, self.as_of).release("en", "idol_01_02")
        self.assertEqual(result["status"], "unknown_release")
        self.assertEqual(result["unlock"]["status"], "unknown_unlock")
        self.assertIn("unsupported_unit_story_unlock_condition", result["unlock"]["reasons"])

    def test_unit_unlock_cycle_fails_fast_and_stays_unknown(self):
        self.unit_story_fixture()
        self.rewrite_unit_story_table("en", lambda rows: rows[0]["chapters"][0]["episodes"][1].update(releaseConditionId=2))
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.UnitStoryVerifier(conn, self.as_of).unlock("en", "idol_01_02")
        self.assertEqual(result["status"], "unknown_unlock")
        self.assertIn("unit_story_prerequisite_not_verified_reachable", result["reasons"])

    def test_unit_hash_drift_does_not_hide_other_region_expected_universe(self):
        self.unit_story_fixture(missing_scenario="idol_01_00")
        (self.root / "raw/en/unitStories.json").write_text("[]", encoding="utf-8")
        result = cc.census(self.store, self.root / "unit-hash-drift", self.baseline, self.as_of, 2)["expected_unit_story_inventory"]
        self.assertEqual(result["masterdata_inventory_story_units"], 3)
        self.assertEqual(result["inventory_sources_with_errors"], 1)
        self.assertEqual(result["counts"]["five_language_raw_identity_verified"], 0)
        self.assertEqual(result["page_identity_audit_counts"]["unknown"], 2)

    def test_unit_page_identity_requires_url_sequence_and_exact_asset_bundle(self):
        self.unit_story_fixture()
        with closing(cc.open_read_only(self.db)) as conn:
            verifier = cc.UnitStoryVerifier(conn, self.as_of)
            episode, _ = verifier.unique("en", "idol_01_01")
        page = {"source": "altsource_ms", "language": "en", "kind": "unit_story",
                "id": "web:altsource_ms:en:unit_story:idol_01_01", "canonical_key": "unit_story:en:idol_01_01"}
        self.assertEqual(cc._unit_page_identity(page, episode, "https://example.test/en/story/unit/70001/idol_01_01/", verifier.tables)["status"], "mismatch")
        page.update(source="altsource_sv", id="web:altsource_sv:en:unit_story:idol_01_01")
        url = "https://example.test/assets/scenario/unitstory/idol-chapter/idol_01_01.asset"
        self.assertEqual(cc._unit_page_identity(page, episode, url, verifier.tables)["status"], "verified")
        self.assertEqual(cc._unit_page_identity(page, episode, url.replace("idol-chapter/", "episode_1/"), verifier.tables)["status"], "mismatch")

    def card_story_fixture(self, missing_episode=None, future_languages=()):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("ALTER TABLE web_pages ADD COLUMN url TEXT DEFAULT ''")
            conn.commit()
        for language in cc.LANGUAGES:
            folder = self.root / "raw" / cc.REGIONS[language]
            cards = [{"id": identity, "releaseAt": int(self.as_of.timestamp() * 1000) + 1000
                      if identity == 7 and language in future_languages else self.start,
                      "assetbundleName": f"card_bundle_{identity}", "cardRarityType": "rarity_1",
                      "cardParameters": [{"cardLevel": 1}, {"cardLevel": 20}]}
                     for identity in (7, 19)]
            episodes = [{"id": identity, "cardId": parent, "scenarioId": f"card_{parent}_{part}",
                         "assetbundleName": f"card_bundle_{parent}", "cardEpisodePartType": part,
                         "releaseConditionId": 1 if part == "first_part" else 30000 + parent}
                        for identity, parent, part in ((1000, 7, "first_part"), (1001, 7, "second_part"),
                                                       (9003, 19, "first_part"), (9004, 19, "second_part"))]
            conditions = [{"id": 1, "releaseConditionType": "none"}, *[
                {"id": 30000 + identity, "releaseConditionType": "card_level", "releaseConditionTypeId": identity,
                 "releaseConditionTypeLevel": 20} for identity in (7, 19)]]
            for table, records, prefix in (("cards", cards, "card"), ("cardEpisodes", episodes, "card_episode")):
                path = folder / (table + ".json")
                path.write_text(json.dumps(records), encoding="utf-8")
                retrieval = {"table": table, "path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "generation": "fixture"}
                with closing(sqlite3.connect(self.db)) as conn:
                    for record in records:
                        facts = {"releaseAt": record["releaseAt"]} if table == "cards" else {}
                        conn.execute("INSERT OR REPLACE INTO entity_region_facts VALUES(?,?,?,?,?,?)",
                                     (f"{prefix}:{record['id']}", cc.REGIONS[language], json.dumps(facts), json.dumps(retrieval), "fixture", "v1"))
                    conn.commit()
            (folder / "releaseConditions.json").write_text(json.dumps(conditions), encoding="utf-8")
            for episode in episodes:
                if episode["id"] == missing_episode:
                    continue
                self.add_page(f"card_story:{episode['id']}", language)
                with closing(sqlite3.connect(self.db)) as conn:
                    conn.execute("UPDATE web_pages SET url=? WHERE source=? AND id=?",
                                 (f"https://example.test/{language}/story/card/{episode['cardId']}/", "altsource_ms",
                                  f"web:altsource_ms:{language}:card_story:{episode['id']}"))
                    conn.commit()

    def test_card_inventory_uses_episode_foreign_keys_not_same_number_card_ids(self):
        self.card_story_fixture(missing_episode=9004)
        digest = hashlib.sha256(self.db.read_bytes()).hexdigest()
        out = self.root / "card-inventory"
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)
        inventory = result["expected_card_story_inventory"]
        self.assertEqual(inventory["observed_story_units"], 3)
        self.assertEqual(inventory["masterdata_inventory_story_units"], 4)
        self.assertEqual(inventory["union_story_units"], 4)
        self.assertEqual(inventory["counts"]["all_five_pages_absent"], 1)
        self.assertEqual(inventory["counts"]["all_five_released_pages_absent"], 1)
        self.assertEqual(inventory["counts"]["local_five_language_identity_verified_primary_complete"], 3)
        self.assertEqual(inventory["release_status_counts"]["all_five_released_in_verified_masterdata"], 4)
        rows = [json.loads(line) for line in (out / "expected-card-stories.jsonl").read_text(encoding="utf-8").splitlines()]
        episode = next(row for row in rows if row["logical_key"] == "card_story:1001")
        self.assertEqual(episode["release"]["en"]["card_id"], 7)
        self.assertEqual(episode["release"]["en"]["prerequisite_first_part"]["id"], 1000)
        self.assertFalse(episode["release"]["en"]["condition_retrieval_sealed"])
        frozen = hashlib.sha256((out / "holdout-manifest.json").read_bytes()).hexdigest()
        self.assertEqual(cc.refresh_expected_card_stories(self.store, out), inventory)
        self.assertEqual(hashlib.sha256((out / "holdout-manifest.json").read_bytes()).hexdigest(), frozen)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), digest)

    def test_card_page_url_foreign_key_mismatch_cannot_count_as_verified_acquisition(self):
        self.card_story_fixture()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE web_pages SET url=? WHERE id=?", ("https://example.test/en/story/card/1000/", "web:altsource_ms:en:card_story:1000"))
            conn.commit()
        out = self.root / "wrong-card-url"
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)["expected_card_story_inventory"]
        self.assertEqual(result["page_identity_audit_counts"]["mismatch"], 1)
        self.assertEqual(result["counts"]["local_five_language_primary_complete_before_identity_audit"], 4)
        self.assertEqual(result["counts"]["local_five_language_identity_verified_primary_complete"], 3)
        rows = [json.loads(line) for line in (out / "expected-card-stories.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(next(row for row in rows if row["logical_key"] == "card_story:1000")["acquisition_states"]["en"], "identity_unverified_only")

    def test_card_asset_source_requires_exact_bundle_and_scenario(self):
        episode = {"record": {"id": 1000, "cardId": 7, "scenarioId": "card_7_first_part"}}
        card = {"record": {"id": 7, "assetbundleName": "card_bundle_7"}}
        page = {"id": "web:altsource_sv:en:card_story:1000", "source": "altsource_sv", "language": "en", "kind": "card_story",
                "canonical_key": "card_story:en:1000"}
        correct = "https://example.test/assets/character/member_scenario/card_bundle_7/card_7_first_part.asset?v=1"
        self.assertEqual(cc._card_page_identity(page, episode, card, correct)["status"], "verified")
        self.assertEqual(cc._card_page_identity(page, episode, card, correct.replace("first_part.asset", "second_part.asset"))["status"], "mismatch")
        self.assertEqual(cc._card_page_identity(page, episode, card, correct.replace("card_bundle_7/", "card_bundle_19/"))["status"], "mismatch")

    def test_card_release_date_and_regional_fact_must_match(self):
        self.card_story_fixture()
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE entity_region_facts SET facts_json=? WHERE entity_id=? AND region=?",
                         (json.dumps({"releaseAt": 2000}), "card:7", "en"))
            conn.commit()
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.CardStoryVerifier(conn, self.as_of).release(1000, "en")
        self.assertEqual(result["status"], "unknown_release")
        self.assertIn("card_release_fact_mismatch", result["reasons"])

    def test_future_card_release_is_not_published_even_when_story_text_exists(self):
        self.card_story_fixture(future_languages=("en",))
        out = self.root / "future-card"
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)["expected_card_story_inventory"]
        self.assertEqual(result["release_status_counts"]["contains_not_yet_released"], 2)
        self.assertEqual(result["counts"]["local_five_language_identity_verified_primary_complete"], 4)

    def test_card_second_part_requires_supported_level_and_first_part_definition(self):
        self.card_story_fixture()
        path = self.root / "raw/en/releaseConditions.json"
        conditions = json.loads(path.read_text(encoding="utf-8"))
        conditions[1]["releaseConditionTypeLevel"] = 99
        path.write_text(json.dumps(conditions), encoding="utf-8")
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.CardStoryVerifier(conn, self.as_of).release(1001, "en")
        self.assertIn("unlock_level_not_in_card_parameters", result["reasons"])
        conditions[1]["releaseConditionTypeLevel"] = 20
        conditions.append({**conditions[1], "releaseConditionTypeLevel": 21})
        path.write_text(json.dumps(conditions), encoding="utf-8")
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.CardStoryVerifier(conn, self.as_of).release(1001, "en")
        self.assertIn("conflicting_unlock_condition_records", result["reasons"])

    def test_card_raw_inventory_stays_counted_without_every_card_entity_fact(self):
        self.card_story_fixture(missing_episode=9004)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("DELETE FROM entity_region_facts WHERE entity_id=?", ("card:19",))
            conn.commit()
        out = self.root / "missing-card-facts"
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)["expected_card_story_inventory"]
        self.assertEqual(result["masterdata_inventory_story_units"], 4)
        self.assertEqual(result["counts"]["all_five_pages_absent"], 1)
        self.assertEqual(result["counts"]["all_five_released_pages_absent"], 0)
        self.assertEqual(result["release_status_counts"]["unknown_release"], 2)

    def test_card_episode_raw_hash_mismatch_is_unknown_not_false_verified_identity(self):
        self.card_story_fixture()
        (self.root / "raw/en/cardEpisodes.json").write_text("[]", encoding="utf-8")
        out = self.root / "card-raw-hash"
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)["expected_card_story_inventory"]
        self.assertEqual(result["masterdata_inventory_story_units"], 4)
        self.assertEqual(result["release_status_counts"]["unknown_release"], 4)
        self.assertEqual(result["inventory_sources_with_errors"], 1)
        self.assertEqual(result["page_identity_audit_counts"]["unknown"], 4)

    def rewrite_sealed_card_table(self, language, table, records):
        region = cc.REGIONS[language]
        path = self.root / "raw" / region / (table + ".json")
        path.write_text(json.dumps(records), encoding="utf-8")
        prefix = "card:%" if table == "cards" else "card_episode:%"
        with closing(sqlite3.connect(self.db)) as conn:
            for entity_id, raw in conn.execute("SELECT entity_id,retrieval_json FROM entity_region_facts WHERE region=? AND entity_id LIKE ?", (region, prefix)).fetchall():
                retrieval = json.loads(raw)
                retrieval["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                conn.execute("UPDATE entity_region_facts SET retrieval_json=? WHERE entity_id=? AND region=?", (json.dumps(retrieval), entity_id, region))
            conn.commit()

    def test_compact_card_parameters_require_exact_expanded_same_card_proof(self):
        self.card_story_fixture()
        expanded = [{"cardLevel": level, "cardParameterType": name, "power": index * 100 + level}
                    for index, name in enumerate(("param1", "param2", "param3"), 1) for level in range(1, 21)]
        jp_path = self.root / "raw/jp/cards.json"
        jp = json.loads(jp_path.read_text(encoding="utf-8"))
        jp[0]["cardParameters"] = expanded
        self.rewrite_sealed_card_table("ja", "cards", jp)
        cn_path = self.root / "raw/cn/cards.json"
        cn = json.loads(cn_path.read_text(encoding="utf-8"))
        cn[0]["cardParameters"] = {name: [row["power"] for row in expanded if row["cardParameterType"] == name]
                                    for name in ("param1", "param2", "param3")}
        self.rewrite_sealed_card_table("zh_hans", "cards", cn)
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.CardStoryVerifier(conn, self.as_of).release(1001, "zh_hans")
        self.assertEqual(result["status"], "released_in_verified_masterdata")
        self.assertEqual(result["card_level_capacity_evidence"]["counterpart_card_id"], 7)
        self.assertEqual(result["card_level_capacity_evidence"]["counterpart_language"], "ja")
        self.assertTrue(result["card_level_capacity_evidence"]["all_three_parameter_value_sequences_equal"])
        cn[0]["cardParameters"]["param1"][0] += 1
        self.rewrite_sealed_card_table("zh_hans", "cards", cn)
        with closing(cc.open_read_only(self.db)) as conn:
            result = cc.CardStoryVerifier(conn, self.as_of).release(1001, "zh_hans")
        self.assertEqual(result["status"], "unknown_release")
        self.assertIn("compact_parameter_level_mapping_not_verified_against_exact_card_records", result["reasons"])

    def test_same_episode_number_different_raw_identity_cannot_be_five_language_equivalence(self):
        self.card_story_fixture()
        path = self.root / "raw/en/cardEpisodes.json"
        records = json.loads(path.read_text(encoding="utf-8"))
        records[0].update(cardId=19, scenarioId="card_19_first_part", assetbundleName="card_bundle_19")
        self.rewrite_sealed_card_table("en", "cardEpisodes", records)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("UPDATE web_pages SET url=? WHERE id=?", ("https://example.test/en/story/card/19/", "web:altsource_ms:en:card_story:1000"))
            conn.commit()
        out = self.root / "conflicting-card-identity"
        result = cc.census(self.store, out, self.baseline, self.as_of, 2)["expected_card_story_inventory"]
        self.assertEqual(result["counts"]["cross_language_identity_conflict"], 1)
        rows = [json.loads(line) for line in (out / "expected-card-stories.jsonl").read_text(encoding="utf-8").splitlines()]
        episode = next(row for row in rows if row["logical_key"] == "card_story:1000")
        self.assertEqual(episode["release"]["en"]["status"], "released_in_verified_masterdata")
        self.assertEqual(episode["release_status"], "unknown_release")
        self.assertFalse(episode["five_language_raw_identity_verified"])


if __name__ == "__main__":
    unittest.main()
