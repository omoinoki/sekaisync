"""Independent domain-entry regressions; synthetic text and isolated stores only.

No allowlist override, network fetch, semantic gold, or production-store write.
"""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from sekaisync import agent_packets as ap, agent_review as ar, dbstore
from sekaisync import occurrence_store as occurrences, source_audits, termindex, trinity


KINDS = {"self_intro", "mysekai_tweet"}
NAMES = {
    "ja": "\u6708\u8679\u97f3\u697d\u796d",
    "en": "Moonbow Festival",
    "zh_hans": "\u6708\u8679\u97f3\u4e50\u8282",
    "zh_tw": "\u6708\u8679\u97f3\u6a02\u7bc0",
    "ko": "\ub2ec\ubb34\ub9ac \uc74c\uc545\uc81c",
}
LOCALES = {"ja": "jp", "en": "en", "zh_hans": "cn", "zh_tw": "tc", "ko": "kr"}


def pages_for(kind, content="101", source="fixture"):
    return [dict(id=f"web:{source}:{LOCALES[language]}:{kind}:{content}",
                 source=source, language=language, kind=kind, trust="B",
                 text="A1 START\nB2 \u300c" + name + "\u300d\nC3 END")
            for language, name in NAMES.items()]


class DomainGateTests(unittest.TestCase):
    def test_self_intro_reaches_real_grouping_without_override(self):
        pages = pages_for("self_intro", "self_fixture_2nd")
        groups = termindex.group_pages_by_story(pages)
        self.assertEqual(set(groups), {"self_intro:self_fixture_2nd"})
        self.assertEqual(set(groups["self_intro:self_fixture_2nd"]), set(NAMES))

    def test_mysekai_tweet_reaches_real_grouping_without_override(self):
        groups = termindex.group_pages_by_story(pages_for("mysekai_tweet"))
        self.assertEqual(set(groups), {"mysekai_tweet:101"})
        self.assertEqual(set(groups["mysekai_tweet:101"]), set(NAMES))

    def test_available_mysekai_talk_reaches_real_grouping_without_override(self):
        pages = [page for page in pages_for("mysekai_talk") if page["language"] == "ja"]
        groups = termindex.group_pages_by_story(pages)
        self.assertEqual(set(groups), {"mysekai_talk:101"})
        self.assertEqual(set(groups["mysekai_talk:101"]), {"ja"})


class DomainClosureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.store = self.base / "store"
        dbstore.initialize(self.store)

    def prepare(self, kind, source="ja", content="101"):
        pages = pages_for(kind, content)
        dbstore.upsert_web_pages(self.store, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        items, report = ap._prepare_scrub_review(
            self.store, groups, sorted(groups), [], {}, source,
            [language for language in NAMES if language != source])
        ar.enqueue(self.store, items)
        return pages, items, report

    def test_same_numeric_id_in_other_domains_never_collapses(self):
        pages = [page for kind in ("mysekai_tweet", "mysekai_talk", "self_intro", "special_story")
                 for page in pages_for(kind)]
        groups = termindex.group_pages_by_story(pages)
        self.assertEqual(set(groups), {"mysekai_tweet:101", "mysekai_talk:101", "self_intro:101", "special_story:101"})
        for key, localized in groups.items():
            self.assertEqual(set(localized), set(NAMES))
            self.assertTrue(all(termindex.page_story_key(page) == key for page in localized.values()))

    def test_self_intro_scenario_versions_never_collapse_on_character(self):
        pages = pages_for("self_intro", "self_fixture") + pages_for("self_intro", "self_fixture_2nd")
        groups = termindex.group_pages_by_story(pages)
        self.assertEqual(set(groups), {"self_intro:self_fixture", "self_intro:self_fixture_2nd"})

    def test_provider_duplicates_do_not_become_independent_content(self):
        pages = pages_for("mysekai_tweet", source="first") + pages_for("mysekai_tweet", source="second")
        groups = termindex.group_pages_by_story(pages)
        self.assertEqual(set(groups), {"mysekai_tweet:101"})
        self.assertEqual(len(groups["mysekai_tweet:101"]), 5)

    def test_wordseg_receives_full_raw_bodies_for_both_domains(self):
        pages = pages_for("self_intro", "self_fixture_2nd") + pages_for("mysekai_tweet")
        groups = termindex.group_pages_by_story(pages)
        with mock.patch("sekaisync.wordseg.discover_words", return_value=set()) as discover:
            trinity.build_candidate_pool(groups, sorted(groups), source_language="ja")
        expected = [termindex._group_page(groups[key], "ja")["text"] for key in sorted(groups)]
        self.assertEqual(discover.call_args.args[0], expected)
        self.assertEqual(discover.call_args.kwargs["max_chars"], 2_000_000)

    def test_fallback_reads_exact_kind_and_identity_not_just_numeric_suffix(self):
        pages = [page for kind in ("mysekai_tweet", "self_intro", "special_story")
                 for page in pages_for(kind)] + pages_for("mysekai_tweet", "1101")
        dbstore.upsert_web_pages(self.store, "fixture", pages)
        with dbstore.connect(self.store) as conn:
            for kind in KINDS:
                with self.subTest(kind=kind):
                    found = ap._fallback_story_pages(conn, kind + ":101")
                    self.assertEqual(set(found), set(NAMES))
                    self.assertTrue(all(page["kind"] == kind for page in found.values()))
                    self.assertTrue(all(termindex.page_story_key(page) == kind + ":101"
                                        for page in found.values()))

    def test_empty_candidates_still_make_raw_discovery_for_every_source_language(self):
        for kind in KINDS:
            for source in NAMES:
                with self.subTest(kind=kind, source=source):
                    pages, items, report = self.prepare(kind, source=source)
                    self.assertGreater(report["source_windows"], 0)
                    self.assertTrue(items)
                    self.assertTrue(all(item.kind == "discovery" for item in items))
                    self.assertEqual(report["model_api_calls"], 0)
                    scope = ap._read_scope(self.store, report["scope_id"])
                    for row in scope["windows"]:
                        self.assertEqual(row["story_key"], kind + ":101")
                        self.assertEqual(set(row["targets"]), set(NAMES) - {source})
                        page = next(page for page in pages if page["language"] == source)
                        view = row["source"]
                        self.assertEqual(view["page_id"], page["id"])
                        self.assertEqual(view["text"], page["text"][view["start"]:view["end"]])
                        self.assertTrue(view["complete"])

    def test_typed_discovery_fans_out_four_directions_and_commits_real_relation(self):
        for kind in sorted(KINDS):
            with self.subTest(kind=kind):
                # Distinct content scopes prevent previous-domain work from participating.
                pages, items, _ = self.prepare(kind)
                discovery = next(item for item in items if any(
                    NAMES["ja"] in row["source"]["text"] for row in item._context["rows"]))
                row = next(row for row in discovery._context["rows"]
                           if NAMES["ja"] in row["source"]["text"])
                offset = row["source"]["start"] + row["source"]["text"].index(NAMES["ja"])
                subject = dict(kind="literal", canonical=NAMES["ja"], evidence_id=row["id"],
                               segments=[dict(start=offset, end=offset + len(NAMES["ja"]), exact=NAMES["ja"])])
                result = ar.submit_judgments(self.store, [dict(id=discovery.id, decision="accept", subjects=[subject])])
                self.assertEqual(result["errors"], [])
                children = [item for item in ar.load_queue(self.store)
                            if item._context.get("task") == "occurrence"
                            and item._context.get("subject", {}).get("source", {}).get("story_key") == kind + ":101"]
                self.assertEqual({item.language for item in children}, set(NAMES) - {"ja"})
                child = next(item for item in children if item.language == "en")
                evidence = child._context["rows"][0]
                target = evidence["target"]
                segments = ap._body_term_segments(target["text"], NAMES["en"], target["start"])[0]
                answer = dict(id=child.id, decision="accept", relations=[dict(
                    evidence_id=evidence["id"],
                    source_segments=child._context["subject"]["source"]["segments"],
                    target_segments=segments, sense_key="fixture-festival",
                    sense_gloss="Synthetic festival name", kind="lexical",
                    rationale="Exact synthetic source expression in the same localized content.")])
                committed = ar.submit_judgments(self.store, [answer])
                self.assertEqual(committed["errors"], [])
                with dbstore.connect(self.store) as conn:
                    relations = [relation for relation in occurrences._read_relations(conn)
                                 if relation["story_key"] == kind + ":101"]
                    self.assertEqual(len(relations), 1)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                replay = ar.submit_judgments(self.store, [answer])
                self.assertEqual(replay["errors"], [])
                self.assertEqual(replay["accepted"], 0)

    def test_jp_only_mysekai_talk_preserves_four_named_language_debts(self):
        pages = [page for page in pages_for("mysekai_talk") if page["language"] == "ja"]
        dbstore.upsert_web_pages(self.store, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        items, report = ap._prepare_scrub_review(
            self.store, groups, sorted(groups), [], {}, "ja", list(set(NAMES) - {"ja"}))
        ar.enqueue(self.store, items)
        scope = ap._read_scope(self.store, report["scope_id"])
        self.assertTrue(scope["windows"])
        self.assertTrue(all(not row["targets"] for row in scope["windows"]))
        discovery = next(item for item in items if any(
            NAMES["ja"] in row["source"]["text"] for row in item._context["rows"]))
        row = next(row for row in discovery._context["rows"] if NAMES["ja"] in row["source"]["text"])
        offset = row["source"]["start"] + row["source"]["text"].index(NAMES["ja"])
        proposal = dict(kind="literal", canonical=NAMES["ja"], evidence_id=row["id"],
                        segments=[dict(start=offset, end=offset + len(NAMES["ja"]), exact=NAMES["ja"])])
        result = ar.submit_judgments(self.store, [dict(id=discovery.id, decision="accept", subjects=[proposal])])
        self.assertEqual(result["errors"], [])
        debts = [item for item in ar.load_queue(self.store) if item._context.get("task") == "subject_gap"]
        self.assertEqual({item.language for item in debts}, set(NAMES) - {"ja"})
        self.assertEqual(len(debts), 4)
        for debt in debts:
            self.assertEqual(debt._context["gap"]["reason"], "missing_usable_localized_page")
            self.assertEqual(debt._context["subject"]["source"]["story_key"], "mysekai_talk:101")
            invalid = ar.submit_judgments(self.store, [dict(id=debt.id, decision="accept", terms=[])])
            self.assertTrue(invalid["errors"])
        audits = [item for item in ar.load_queue(self.store) if source_audits._is_audit(item._context)]
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        marker = audit._context["source_boundary_audit"]
        self.assertEqual(marker["stage"], "review")
        self.assertEqual(marker["terminal_parent_id"], discovery.id)
        self.assertEqual(audit._context["scope_id"], discovery._context["scope_id"])
        self.assertEqual(audit._context["rows"], discovery._context["rows"])
        self.assertEqual({item.id for item in ar.load_queue(self.store)},
                         {item.id for item in debts} | {audit.id})
        with dbstore.connect(self.store) as conn:
            source_audits._validate(conn, self.store, audit)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
            self.assertEqual(occurrences._read_relations(conn), [])


if __name__ == "__main__":
    unittest.main()
