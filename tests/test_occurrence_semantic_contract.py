"""Operational host instructions use the existing export and typed submission."""
import json
from pathlib import Path
import tempfile
import unittest

from sekaisync import agent_packets as packets, agent_review as review, dbstore, occurrence_store, termindex
from sekaisync.core import SekaiSyncCore


class OccurrenceSemanticContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def fixture(self, version=1):
        store = self.path / str(version)
        dbstore.initialize(store)
        for target in range(2, version + 1):
            dbstore.migrate_store(store, target_version=target, dry_run=False,
                                 backup_path=self.path / (str(version) + "-v" + str(target) + ".db"))
        source = "\u660e\u308b\u3044\u4f5c\u308a"
        pages = [dict(id="web:fixture:ja:event_story:1:1", source="fixture", language="ja", trust="B",
                      kind="event_story", text="\u7532\uff1a" + source + "\u3060\u306d\u3002"),
                 dict(id="web:fixture:en:event_story:1:1", source="fixture", language="en", trust="B",
                      kind="event_story", text="Narrator\uff1aThe melody is bright.")]
        dbstore.upsert_web_pages(store, "fixture", pages)
        groups = termindex.group_pages_by_story(pages)
        items, _ = packets._prepare_scrub_review(store, groups, list(groups), [source], {}, "ja", ["en"])
        item = packets._occurrence_item(next(item for item in items if item._context["task"] == "translation"))
        review.enqueue(store, [item])
        return store, source, item

    def test_normal_export_defines_semantics_types_boundaries_and_omission(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, _, item = self.fixture(version)
                original = json.dumps(item.to_dict(), sort_keys=True)
                output = self.path / ("export-" + str(version) + ".txt")
                review.export_for_agent(store, output, limit=0)
                rendered = output.read_text(encoding="utf-8")
                for name in ("semantic_contract:", "semantic_component_check:", "relation_kind_contract:",
                             "relation_kind_decision:",
                             "target_boundary_contract:", "omission_check:"):
                    self.assertIn(name, rendered)
                for phrase in ("nominal-to-predicate restructuring", "narrower referring phrase",
                               "Do not mechanically add a copula", "bounded evidence does not prove absence",
                               "positive contextual evidence for the same referent",
                               "not only the result phrase",
                               "do not demand the source operator's count or grammatical form",
                               "a target reference need not restate the source description",
                               "identity or the selected core relation remains unsettled",
                               "alone cannot justify paraphrase",
                               "actual content-level reformulation beyond grammatical difference",
                               "do not weaken the source sense or force lexical",
                               "No new answer fields are required"):
                    self.assertIn(phrase, rendered)
                self.assertEqual(original, json.dumps(item.to_dict(), sort_keys=True))
                self.assertEqual(item.id, review.load_queue(store)[0].id)

    def test_operational_type_check_is_compact_component_first_and_not_corpus_specific(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                _, _, item = self.fixture(version)
                lines = packets._render_context(item)
                names = [line.split(":", 1)[0] for line in lines]
                self.assertEqual(names.count("relation_kind_decision"), 1)
                decision = lines[names.index("relation_kind_decision")]
                self.assertLess(len(decision), 800)
                self.assertLess(names.index("semantic_component_check"), names.index("relation_kind_decision"))
                self.assertLess(names.index("relation_kind_decision"), names.index("target_boundary_contract"))
                for phrase in ("participants, restrictions, operators and discourse force",
                               "Word class/order, inflection, function words, word count",
                               "nominal-to-predicate restructuring",
                               "Use lexical for direct conventional realization",
                               "same-referent-only reference, absent omitted and unsettled unresolved",
                               "No new answer fields"):
                    self.assertIn(phrase, decision)
                self.assertNotIn("were out shopping", decision)

    def test_nominal_to_predicate_paraphrase_keeps_existing_fields_and_public_projection(self):
        for version in (1, 2, 3):
            with self.subTest(version=version):
                store, source, item = self.fixture(version)
                row = item._context["rows"][0]
                target = "is bright"
                start = row["target"]["start"] + row["target"]["text"].index(target)
                relation = dict(evidence_id=row["id"],
                                source_segments=packets._body_term_segments(row["source"]["text"], source,
                                                                            row["source"]["start"])[0],
                                target_segments=[dict(start=start, end=start + len(target), exact=target)],
                                sense_key="musical-design", sense_gloss="the melody's bright musical design",
                                kind="paraphrase", rationale="The nominal musical characterization is expressed as a predicate.")
                answer = dict(id=item.id, decision="accept", relations=[relation])
                result = review.submit_judgments(store, [answer])
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["accepted"], 1)
                self.assertEqual(review.submit_judgments(store, [answer])["accepted"], 0)
                with dbstore.connect(store) as conn:
                    stored, = occurrence_store._read_relations(conn)
                    self.assertEqual(stored["kind"], "paraphrase")
                    self.assertEqual(stored["source"]["segments"], relation["source_segments"])
                    self.assertEqual(stored["target"]["segments"], relation["target_segments"])
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 0)
                core = SekaiSyncCore(store)
                projected = core.term_penetrate(source, story_key="event:1:1", languages=["ja", "en"])
                self.assertEqual(projected["per_language"]["en"]["term"], "")
                self.assertIn("paraphrase", projected["per_language"]["en"]["note"])
                self.assertIn(target, projected["per_language"]["en"]["sentence"])
                hits = [hit for hit in core.query(source, include_web=False)["terms"]
                        if hit.get("source") == "host-agent-occurrence"]
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]["names"], {"ja": source})


if __name__ == "__main__":
    unittest.main()
