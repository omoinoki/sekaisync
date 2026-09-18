"""W1-C: `--layered` must actually land in the slot store.

The layered branch used to write two side files (``methodology.json``,
``review_queue.json``) and print a summary: `term_slots` was never touched and
``result["slot_decisions"]`` was dropped on the floor, so "accepted N" was a
report about a decision nobody had applied.  W1-A made the channels emit
committable evidence rows and W1-B supplied the corpus verifier; this file pins
the missing third piece — ``trinity.apply_scrub_result`` (application-layer,
single transaction) and the CLI wiring that calls it.

Everything here runs against temporary stores built the way the brief requires:
``initialize`` → ``migrate_store(2)`` → ``migrate_store(3)``.  The real
``store/kb/sekaisync.db`` is schema 1 and is never opened.

The red halves are kept runnable, not marked expected-failure: with the verifier
removed, or with the slot commit stubbed out, the *same* assertion helper that
passes on the green path must raise.  Otherwise the green test could pass on a
fixture that never committed anything.
"""

import argparse
import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sekaisync import dbstore, term_slots, termindex, trinity
from sekaisync.termindex import TermRecord


def _story(ja: str, en: str = "", zh: str = "") -> dict:
    by: dict = {"ja": {"text": ja}}
    if en:
        by["en"] = {"text": en}
    if zh:
        by["zh_hans"] = {"text": zh}
    return by


SEKAI_GLOSSARY = [TermRecord(
    id="unit:sekai", canonical="セカイ", source_language="ja", kind="unit",
    official=True, source="master_db",
    names={"ja": "セカイ", "en": "SEKAI", "zh_hans": "世界", "zh_tw": "世界"},
)]

THREE_STORIES = {
    "s1": _story("セカイに行こう\n", "Let's go to SEKAI\nI saw Asahina\n", "去往世界\n"),
    "s2": _story("セカイは広い\n", "Big SEKAI\nI saw Asahina\n", "世界很宽\n"),
    "s3": _story("セカイの歌\n", "Song of SEKAI\nI saw Asahina\n", "世界之歌\n"),
}

ONE_STORY = {"s1": _story("セカイに行こう\n", "Let's go to SEKAI\n", "去往世界\n")}


class LayeredApplyTestCase(unittest.TestCase):
    """Shared fixture: an explicitly migrated (v3) temporary store."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        dbstore.initialize(self.root)
        dbstore.migrate_store(
            self.root, target_version=2, dry_run=False,
            backup_path=Path(self.tmp.name) / "v2.db")
        dbstore.migrate_store(
            self.root, target_version=3, dry_run=False,
            backup_path=Path(self.tmp.name) / "v3.db")

    # ── helpers ─────────────────────────────────────────────────────
    def revision(self) -> int:
        with dbstore.connect(self.root) as conn:
            return dbstore.current_revision(conn)

    def slot_rows(self) -> list[tuple]:
        """`term_slots` read straight from the table, not via a summary.

        A commit that only *returns* counts would pass a returned-value check;
        this one can only be satisfied by rows that are really there.
        """
        with dbstore.connect(self.root) as conn:
            return conn.execute(
                "SELECT term_id, language, value, status, official, trust, "
                "source, reason, evidence_refs_json FROM term_slots "
                "ORDER BY term_id, language").fetchall()

    def projected_names(self) -> dict:
        with dbstore.connect(self.root) as conn:
            return {
                str(term_id): json.loads(names or "{}")
                for term_id, names in conn.execute(
                    "SELECT id, names_json FROM terms ORDER BY id")
            }

    def queued_items(self) -> list[tuple]:
        with dbstore.connect(self.root) as conn:
            return conn.execute(
                "SELECT item_id, term_id, language, status, evidence_revision "
                "FROM review_queue ORDER BY item_id").fetchall()

    def scrub(self, groups, term="セカイ", glossary=None, targets=("en", "zh_hans")):
        from sekaisync import trinity as trinity_module

        glossary = SEKAI_GLOSSARY if glossary is None else glossary
        vocab = termindex.build_alignment_vocab(groups, targets, glossary)
        idf = termindex.compute_lang_idf(groups, targets, vocab=vocab)
        result = trinity_module.scrub_trinity(
            groups, sorted(groups), [term], target_languages=targets,
            glossary=glossary, vocab=vocab, idf=idf)
        corpus = trinity_module._Corpus(groups, sorted(groups))
        return result, corpus

    def apply(self, result, corpus, **kwargs):
        kwargs.setdefault("expected_revision", self.revision())
        return trinity.apply_scrub_result(self.root, result, corpus, **kwargs)

    # ── the assertion the red halves must break ─────────────────────
    def assert_layered_result_landed(self, out: dict) -> None:
        """`--layered` really landed: accepted slot, rows, projection, revision."""
        self.assertGreaterEqual(
            out["accepted_slots"], 1,
            "no slot was accepted; the layered result did not reach the store")
        rows = self.slot_rows()
        self.assertTrue(rows, "term_slots is empty")
        accepted = {
            (term_id, language): value
            for term_id, language, value, status, *_rest in rows
            if status == "accepted"
        }
        self.assertTrue(accepted, "no row in term_slots has status=accepted")
        self.assertEqual(out["accepted_slots"], len(accepted))
        english = accepted.get(("term:ja:セカイ", "en"))
        self.assertEqual(english, "SEKAI")
        # The read projection is the accepted slots, and nothing else.
        self.assertEqual(self.projected_names().get("term:ja:セカイ"), {
            language: value
            for (term_id, language), value in accepted.items()
            if term_id == "term:ja:セカイ"
        })


class LayeredEndToEndTests(LayeredApplyTestCase):
    """Acceptance 1: three-story corpus, real scrub, real rows."""

    def test_three_story_corpus_produces_an_accepted_slot_in_the_table(self):
        result, corpus = self.scrub(THREE_STORIES)
        before = self.revision()
        out = self.apply(result, corpus)

        self.assert_layered_result_landed(out)
        self.assertEqual(out["revision"], self.revision())
        self.assertGreater(out["revision"], before, "the commit did not advance the revision")
        self.assertEqual(out["verifier"], "corpus")

    def test_accepted_slot_carries_a_corpus_certificate_not_a_self_report(self):
        """A landed slot states where its authority came from."""
        result, corpus = self.scrub(THREE_STORIES)
        out = self.apply(result, corpus)
        self.assertGreaterEqual(out["accepted_slots"], 1)
        with dbstore.connect(self.root) as conn:
            row = conn.execute(
                "SELECT status, official, trust, source, reason, confidence, "
                "verification_json, evidence_refs_json FROM term_slots "
                "WHERE term_id='term:ja:セカイ' AND language='en'").fetchone()
        status, official, trust, source, reason, confidence, verification, refs = row
        self.assertEqual(status, "accepted")
        self.assertEqual(official, 0, "a corpus proof must not claim official authority")
        self.assertEqual(trust, "C")
        self.assertEqual(source, trinity.CORPUS_SOURCE)
        self.assertEqual(reason, "verified_evidence")
        self.assertGreater(confidence, 0.0)
        proof = json.loads(verification)
        self.assertEqual(proof["verifier"], term_slots.CORPUS_VERIFIER_NAME)
        # The refs resolve to stored rows naming distinct stories: the store's
        # own `_certificate` enforced this, so it must be observable here too.
        refs = json.loads(refs)
        self.assertGreaterEqual(len(refs), term_slots.MIN_CORPUS_STORIES)
        with dbstore.connect(self.root) as conn:
            stories = {
                row[0] for row in conn.execute(
                    "SELECT story_key FROM term_evidence WHERE term_id='term:ja:セカイ'")
            }
        self.assertGreaterEqual(len(stories), term_slots.MIN_CORPUS_STORIES)

    def test_evidence_rows_match_only_the_slot_they_support(self):
        """Losing candidates' rows are the adjudication trail, not this proof.

        A decision carries rows for every candidate value the channels saw
        (``decision_evidence_rows`` says so); citing a row that vouches for a
        different value would make the certificate incoherent.
        """
        result, corpus = self.scrub(THREE_STORIES)
        supplied = []
        for decision in result["slot_decisions"]:
            supplied.extend(decision.get("evidence_rows") or [])
        self.assertTrue(supplied, "fixture produced no evidence rows")
        self.assertTrue(
            any(row["channel_value"] != decision["value"]
                for decision in result["slot_decisions"]
                for row in decision.get("evidence_rows") or []),
            "fixture stopped exercising the losing-candidate rows")
        self.apply(result, corpus)
        with dbstore.connect(self.root) as conn:
            rows = conn.execute(
                "SELECT term_id, language, extra_json FROM term_evidence").fetchall()
        self.assertTrue(rows)
        for term_id, language, extra in rows:
            extra = json.loads(extra)
            slot = next(
                (row for row in self.slot_rows() if row[0] == term_id and row[1] == language),
                None)
            self.assertIsNotNone(slot, "evidence was stored for a slot that does not exist")
            self.assertEqual(
                slot[3], "accepted",
                "an unaccepted slot's rows were stored as proof")
            self.assertEqual(extra.get("channel_value"), slot[2])

    def test_applying_the_same_result_twice_is_a_no_op(self):
        """Re-running the pass must not churn slots, evidence or revision."""
        result, corpus = self.scrub(THREE_STORIES)
        first = self.apply(result, corpus)
        slots = self.slot_rows()
        queued = self.queued_items()
        revision = self.revision()
        second = self.apply(result, corpus)
        self.assertEqual(self.slot_rows(), slots)
        self.assertEqual(self.queued_items(), queued)
        self.assertEqual(self.revision(), revision, "a no-op re-apply bumped the revision")
        self.assertEqual(second["accepted_slots"], first["accepted_slots"])
        self.assertEqual(second["evidence_written"], 0)
        self.assertEqual(second["evidence_submitted"], 0)


class LayeredReverseTests(LayeredApplyTestCase):
    """Acceptance 2: one story is not corroboration; nothing may be adopted."""

    def test_single_story_evidence_stays_pending(self):
        result, corpus = self.scrub(ONE_STORY)
        self.assertTrue(
            any(row["status"] == "accepted" for row in result["slot_decisions"]),
            "the pipeline stopped proposing an accepted slot for this fixture")
        out = self.apply(result, corpus)
        self.assertEqual(out["accepted_slots"], 0)
        rows = self.slot_rows()
        self.assertTrue(rows, "no slots were written at all")
        self.assertTrue(all(row[3] == "pending" for row in rows), rows)
        english = next(row for row in rows if row[1] == "en")
        # Honest: the proposed value is recorded, it is just not adopted.
        self.assertEqual(english[2], "SEKAI")
        self.assertEqual(english[4], 0)
        self.assertEqual(english[5], "", "a pending slot must not carry trust")
        self.assertEqual(self.projected_names()["term:ja:セカイ"], {})
        with self.assertRaises(AssertionError):
            self.assert_layered_result_landed(out)


class LayeredReviewQueueTests(LayeredApplyTestCase):
    """Acceptance 3: pending/conflict reach the queue, exactly once."""

    def test_pending_slots_are_queued_once_per_evidence_version(self):
        result, corpus = self.scrub(ONE_STORY)
        out = self.apply(result, corpus)
        rows = self.slot_rows()
        self.assertTrue(rows)
        self.assertTrue(all(row[3] == "pending" for row in rows))
        queued = self.queued_items()
        self.assertEqual(len(queued), len(rows), "a pending slot was not queued")
        self.assertEqual(out["review_queued"], len(queued))
        self.assertTrue(all(row[3] == "queued" for row in queued))
        # Same evidence version again: same items, no duplicates.
        again = self.apply(result, corpus)
        self.assertEqual(self.queued_items(), queued)
        self.assertEqual(again["review_queued"], len(queued))

    def test_a_conflicting_slot_is_queued_without_a_value(self):
        """A tie the channels could not settle is queued, never silently picked.

        Built through the real merge step (`_merge_channels`) so the conflict
        shape under test is the producer's, not a hand-written fixture.
        """
        groups = {
            "s1": _story("セカイに行こう\n", "Let's go to SEKAI\n"),
            "s2": _story("セカイは広い\n", "Big SEKAI\n"),
        }
        channels = {"translit": {"セカイ": {"en": {
            "SEKAI": {"value": "SEKAI", "sim": 0.99, "evidence": {
                "channel": "translit", "story_keys": ["s1", "s2"]}},
            "SEKAI2": {"value": "SEKAI2", "sim": 0.99, "evidence": {
                "channel": "translit", "story_keys": ["s1", "s2"]}},
        }}}}
        corpus = trinity._Corpus(groups, sorted(groups))
        merged = trinity._merge_channels(
            channels, corpus=corpus, source_language="ja", glossary_names={})
        conflicts = [row for row in merged["slot_decisions"] if row["status"] == "conflict"]
        self.assertTrue(conflicts, "the fixture stopped producing a conflict")
        result = {
            "slot_decisions": merged["slot_decisions"], "accepted": merged["accepted"],
            "pending": merged["pending"], "conflicts": merged["conflicts"],
        }
        out = self.apply(result, corpus)
        self.assertEqual(out["conflict_slots"], len(conflicts))
        self.assertEqual(out["accepted_slots"], 0)
        with dbstore.connect(self.root) as conn:
            rows = conn.execute(
                "SELECT value, status, reason, evidence_refs_json FROM term_slots").fetchall()
        self.assertTrue(rows)
        for value, status, reason, refs in rows:
            self.assertEqual(status, "conflict")
            self.assertIsNone(value, "a conflict must not carry a chosen value")
            self.assertEqual(json.loads(refs), [])
            # The reason names the slot the tie happened in, so the reviewer
            # is not sent to a different language than the one at issue.
            self.assertIn("语言槽 en", reason)
        self.assertEqual(len(self.queued_items()), len(rows))
        # The candidates the channels disagreed about are preserved for review.
        with dbstore.connect(self.root) as conn:
            candidates = json.loads(conn.execute(
                "SELECT candidates_json FROM review_queue").fetchone()[0])
        self.assertEqual(
            sorted(item["value"] for item in candidates), ["SEKAI", "SEKAI2"])

    def test_caller_verifier_is_used_instead_of_the_corpus_default(self):
        """`verifier=` is honoured: the application layer does not override it.

        Both directions of the caller's authority are observable.  A callback
        that refuses everything must leave every slot pending *even though the
        corpus rows are good enough for the default verifier*, and a callback
        that certifies must produce accepted rows — so the outcome follows the
        callback, not a build-time default.
        """
        refusing = mock.Mock(return_value=None)
        result, corpus = self.scrub(THREE_STORIES)
        refused = self.apply(result, corpus, verifier=refusing)
        self.assertEqual(refused["verifier"], "caller")
        self.assertTrue(refusing.called, "the caller's verifier was never invoked")
        self.assertEqual(refused["accepted_slots"], 0)
        rows = self.slot_rows()
        self.assertTrue(rows)
        self.assertTrue(all(row[3] == "pending" for row in rows))
        # The same rows certify under the default, so the zero above is the
        # callback's decision rather than uncommittable evidence.
        self.assertGreaterEqual(self.apply(result, corpus)["accepted_slots"], 1)

        def certify(slot, evidence):
            # The evidence list is the whole subject's table — one slot per
            # language shares it — so a verifier must cite the rows that speak
            # for *this* slot.  `_certificate` rejects a ref whose language,
            # source or value disagrees with the slot, which is why
            # `corpus_verifier` filters the same way.
            refs = [
                row["evidence_id"] for row in evidence
                if row.get("language") == slot["language"]
                and row.get("source") == slot["source"]
                and (row.get("term") == slot["value"]
                     or row.get("value") == slot["value"]
                     or slot["value"] in str(row.get("sentence") or ""))
            ]
            return {
                "subject_id": slot["term_id"], "language": slot["language"],
                "value": slot["value"], "source": slot["source"],
                "official": False, "trust": "C", "verifier": "test:caller",
                "evidence_refs": refs,
            }

        certified = self.apply(result, corpus, verifier=certify)
        self.assertEqual(certified["verifier"], "caller")
        self.assertGreaterEqual(certified["accepted_slots"], 1)
        accepted = [row for row in self.slot_rows() if row[3] == "accepted"]
        self.assertTrue(accepted)
        for row in accepted:
            with dbstore.connect(self.root) as conn:
                proof = json.loads(conn.execute(
                    "SELECT verification_json FROM term_slots "
                    "WHERE term_id=? AND language=?", (row[0], row[1])).fetchone()[0])
            self.assertEqual(proof["verifier"], "test:caller")


class LayeredRedHalfTests(LayeredApplyTestCase):
    """Acceptance 4: the green assertion must break when the commit is removed."""

    def test_green_path_passes_its_own_assertion(self):
        result, corpus = self.scrub(THREE_STORIES)
        self.assert_layered_result_landed(self.apply(result, corpus))

    def test_without_a_verifier_nothing_is_accepted(self):
        """Red half 1: the same rows, the same store, `verifier=None`."""
        result, corpus = self.scrub(THREE_STORIES)
        out = self.apply(result, corpus, verifier=None)
        self.assertEqual(out["verifier"], "none")
        self.assertEqual(out["accepted_slots"], 0)
        rows = self.slot_rows()
        self.assertTrue(rows, "the pass wrote nothing, which is a different failure")
        self.assertTrue(all(row[3] == "pending" for row in rows))
        self.assertEqual(self.projected_names()["term:ja:セカイ"], {})
        with self.assertRaises(AssertionError):
            self.assert_layered_result_landed(out)

    def test_without_the_slot_commit_nothing_reaches_the_table(self):
        """Red half 2: the pass runs but the store is never written.

        This is the pre-W1-C behaviour the CLI had (decide, print, no commit)
        reproduced at the function boundary — the printed counts look exactly
        like a successful pass.
        """
        result, corpus = self.scrub(THREE_STORIES)
        with mock.patch.object(
                term_slots, "commit_slot_decisions",
                lambda *args, **kwargs: {"revision": 0, "accepted_slots": 0,
                                         "pending_slots": 0, "conflict_slots": 0,
                                         "rejected_slots": 0, "evidence_written": 0,
                                         "inserted_terms": 0, "updated_terms": 0}):
            out = self.apply(result, corpus)
        self.assertEqual(self.slot_rows(), [])
        with self.assertRaises(AssertionError):
            self.assert_layered_result_landed(out)


class LayeredCliWiringTests(LayeredApplyTestCase):
    """The command a user actually runs must commit, and say what it committed.

    `cmd_terms_extract --layered` is the entry point the defect was reported
    against ("the layered pipeline only queues and prints").  The existing keys
    keep their meaning; the commit result is added under its own key so a
    pipeline count can never be read as a number of applied names.
    """

    def _pages(self):
        pages = []
        for index, (ja, en) in enumerate([
            ("セカイに行こう\n", "Let's go to SEKAI\n"),
            ("セカイは広い\n", "Big SEKAI\n"),
            ("セカイの歌\n", "Song of SEKAI\n"),
        ]):
            for language, text in (("ja", ja), ("en", en)):
                pages.append({
                    "id": f"web:altsource_ms:{language}:event_story:1:{index}",
                    "url": f"https://example.invalid/{language}/{index}",
                    "title": "t", "language": language, "kind": "event_story",
                    "text": text, "crawled_at": "2026-01-01", "trust": "B",
                    "tos_accepted": True,
                })
        dbstore.upsert_web_pages(self.root, "local", pages)

    def _run(self) -> dict:
        from sekaisync.cli import cmd_terms_extract

        args = argparse.Namespace(
            config=None, store=self.root, input=None, include_overlay=False,
            event=None, episode=None, **{"all": True}, source_language="ja",
            languages="ja,en", llm_config=None, max_terms=20, no_translate=True,
            align=False, local=False, layered=True)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertEqual(cmd_terms_extract(args), 0)
        return json.loads(buffer.getvalue())

    def test_layered_command_commits_and_reports_the_real_commit(self):
        self._pages()
        before = self.revision()
        out = self._run()
        self.assertIn(
            "slot_commit", out,
            "the layered command reported no commit result at all; a summary "
            "that only counts decisions is not an applied change")
        commit = out["slot_commit"]
        self.assertTrue(commit["applied"], commit)
        self.assertGreaterEqual(
            commit.get("accepted_slots", 0), 1,
            f"the command's commit result carries no accepted slot: {commit}")
        self.assertEqual(commit["verifier"], "corpus")
        # The contract the consumers already bind to is unchanged.
        for key in ("path", "pipeline", "story_key_count", "methodology_settled",
                    "accepted", "pending", "conflicts", "rejected",
                    "methodology_applied", "queued_for_agent", "channel_stats",
                    "review_next"):
            self.assertIn(key, out)
        self.assertTrue(self.slot_rows(), "the command printed a commit but wrote no rows")
        self.assertGreater(self.revision(), before)
        self.assertEqual(commit["revision"], self.revision())
        self.assert_layered_result_landed(commit)

    def test_layered_command_says_so_when_the_store_cannot_hold_slots(self):
        """A v1 store is reported as *not applied*, never as an empty success."""
        legacy = Path(self.tmp.name) / "legacy"
        dbstore.initialize(legacy)
        self.root, original = legacy, self.root
        try:
            self._pages()
            out = self._run()
        finally:
            self.root = original
        self.assertFalse(out["slot_commit"]["applied"])
        self.assertIn("migrate_store", out["slot_commit"]["reason"])
        self.assertEqual(self.projected_names(), {})


if __name__ == "__main__":
    unittest.main()
