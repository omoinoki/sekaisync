"""Trinity scrubber: arbitration rules and the CLI's conflict conversion.

Astra flagged that `tests/test_trinity.py` did not exist — the module had no
direct test coverage at all, which is how the `_names_from_conflict` defect went
unnoticed. This file starts that coverage from the defect that mattered most.

The defect (D11): `arbitrate` returns conflicts shaped
``{"lang": <language>, "candidates": {<candidate>: [channels]}}`` — language on
the OUTSIDE, candidate values as the inner keys. The CLI iterated the inner dict
as if it were `{language: candidates}`, so candidate names became language keys
and channels became names, producing `{"SEKAI": "translit"}` with the real
language slot lost.
"""

import sys
import unittest
from pathlib import Path

from sekaisync.cli import _names_from_conflict, _proposals_from_rows
from sekaisync.trinity import arbitrate

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_penetrate_channels import _story  # noqa: E402  (shared corpus fixture)


class ArbitrateShapeTest(unittest.TestCase):
    """`arbitrate`'s documented return shape is what the CLI must consume."""

    def test_clear_winner_is_resolved_not_flagged(self):
        result = arbitrate(
            "SEKAI",
            {"en": {"SEKAI": ["L0"]}},
        )
        self.assertEqual(result["resolved"], {"en": "SEKAI"})
        self.assertEqual(result["conflicts"], [])

    def test_tied_candidates_produce_a_conflict_with_lang_outside(self):
        """The conflict carries `lang`; `candidates` holds values, not languages."""
        result = arbitrate(
            "SEKAI",
            {"en": {"SEKAI": ["translit"], "SEKAI2": ["translit"]}},
        )
        self.assertEqual(result["resolved"], {}, "a tie must not be silently resolved")
        self.assertEqual(len(result["conflicts"]), 1)
        conflict = result["conflicts"][0]
        self.assertEqual(conflict["lang"], "en")
        self.assertEqual(
            sorted(conflict["candidates"].keys()),
            ["SEKAI", "SEKAI2"],
            "candidate values are the keys of `candidates`",
        )
        # Each value maps to the channels that proposed it, not to a language.
        for channels in conflict["candidates"].values():
            self.assertIn("translit", channels)


class NamesFromConflictTest(unittest.TestCase):
    """The CLI conversion the defect lived in."""

    def test_language_is_preserved_and_values_become_candidates(self):
        names = _names_from_conflict(
            {
                "lang": "en",
                "candidates": {"SEKAI": ["translit"], "SEKAI2": ["translit"]},
            }
        )
        self.assertEqual(
            names,
            {"en": ["SEKAI", "SEKAI2"]},
            "the real language slot must survive and both candidates be kept",
        )

    def test_no_fake_language_is_emitted(self):
        """Astra: 输出无 `SEKAI:'translit'` 假语言."""
        names = _names_from_conflict(
            {"lang": "en", "candidates": {"SEKAI": ["translit"]}}
        )
        self.assertNotIn("SEKAI", names)
        self.assertNotIn("translit", names.values())

    def test_real_arbitrate_output_round_trips(self):
        """End to end from the real producer, not a hand-written fixture."""
        result = arbitrate(
            "SEKAI",
            {"en": {"SEKAI": ["translit"], "SEKAI2": ["translit"]}},
        )
        conflict = dict(result["conflicts"][0], term="SEKAI")
        names = _names_from_conflict(conflict)
        self.assertEqual(names, {"en": ["SEKAI", "SEKAI2"]})

    def test_missing_lang_or_candidates_yields_nothing(self):
        for bad in ({}, {"lang": "en"}, {"candidates": {"X": ["y"]}},
                    {"lang": "en", "candidates": {}}):
            with self.subTest(bad=bad):
                self.assertEqual(_names_from_conflict(bad), {})

    def test_plain_candidate_list_is_tolerated(self):
        self.assertEqual(
            _names_from_conflict({"lang": "ja", "candidates": ["A", "B"]}),
            {"ja": ["A", "B"]},
        )


class ProposalsFromRowsTest(unittest.TestCase):
    """Both row shapes feed one `{term: {lang: [candidates]}}` structure."""

    def test_list_valued_names_are_flattened(self):
        proposals = _proposals_from_rows(
            [{"term": "SEKAI", "names": {"en": ["A", "B"]}}]
        )
        self.assertEqual(proposals, {"SEKAI": {"en": ["A", "B"]}})

    def test_single_valued_names_still_work(self):
        proposals = _proposals_from_rows([{"term": "SEKAI", "names": {"en": "A"}}])
        self.assertEqual(proposals, {"SEKAI": {"en": ["A"]}})

    def test_conflict_path_keeps_every_candidate(self):
        """Astra: consult/enqueue 用同一候选集合，不能只取首候选."""
        conflict = dict(
            arbitrate(
                "SEKAI",
                {"en": {"SEKAI": ["translit"], "SEKAI2": ["translit"]}},
            )["conflicts"][0],
            term="SEKAI",
        )
        proposals = _proposals_from_rows(
            [{"term": "SEKAI", "names": _names_from_conflict(conflict)}]
        )
        self.assertEqual(proposals["SEKAI"]["en"], ["SEKAI", "SEKAI2"])

    def test_rows_without_term_are_skipped(self):
        self.assertEqual(_proposals_from_rows([{"term": "", "names": {"en": "A"}}]), {})

    def test_empty_values_are_ignored(self):
        proposals = _proposals_from_rows(
            [{"term": "T", "names": {"en": "", "ja": "A"}}]
        )
        self.assertEqual(proposals, {"T": {"ja": ["A"]}})


class GlossaryIdentityTest(unittest.TestCase):
    def test_filter_entities_before_indexing_surfaces(self):
        from types import SimpleNamespace
        from sekaisync.trinity import _glossary_name_index

        area = SimpleNamespace(id="area:1", kind="area", canonical="星庭",
                               names={"ja": "星庭", "en": "Star Garden"})
        honor = SimpleNamespace(id="honor:2", kind="honor", canonical="星庭",
                                names={"ja": "星庭", "en": "CANNED TUNA"})
        for rows in ([area, honor], [honor, area]):
            self.assertEqual(_glossary_name_index(rows)["星庭"]["en"], "Star Garden")

    def test_same_surface_different_entities_is_not_an_official_answer(self):
        from types import SimpleNamespace
        from sekaisync.trinity import _glossary_name_index

        rows = [SimpleNamespace(id=f"area:{i}", kind="area", canonical="星庭",
                                names={"ja": "星庭", "en": name})
                for i, name in enumerate(("Star Garden", "Another Garden"))]
        self.assertNotIn("星庭", _glossary_name_index(rows))
        self.assertNotIn("星庭", _glossary_name_index(reversed(rows)))


class SlotIsolationTest(unittest.TestCase):
    def test_official_chinese_cannot_approve_unverified_english(self):
        from sekaisync.trinity import _Corpus, _merge_channels

        result = _merge_channels(
            {
                "L0": {"セカイ": {"zh_hans": {"世界": {
                    "value": "世界", "evidence": {"official": True},
                }}}},
                "hub": {"セカイ": {"en": {"WrongName": {
                    "value": "WrongName", "evidence": {"verified": False},
                }}}},
            },
            corpus=_Corpus({}, []),
            source_language="ja",
            glossary_names={"セカイ": {"zh_hans": "世界"}},
        )
        self.assertEqual(result["accepted"]["セカイ"]["names"], {"zh_hans": "世界"})
        self.assertTrue(any(
            row["term"] == "セカイ" and row["names"].get("en") == "WrongName"
            for row in result["pending"]
        ))


if __name__ == "__main__":
    unittest.main()


class SlotDecisionHandoffTest(unittest.TestCase):
    """`scrub_trinity` must hand its per-slot decisions to the caller.

    `_merge_channels` has always built `slot_decisions`, but `scrub_trinity`
    dropped the key when assembling its result — so the layered pipeline could
    report how many slots were accepted yet had nothing it could commit, and
    the slot store (Astra P08/P11) had no path from the channels at all.

    The decisions must also carry the *stories* behind them: a slot
    certificate is only issued from evidence that names its supporting
    stories, so a payload reporting a count ("3") cannot become a decision.
    """

    def _corpus(self) -> dict:
        return {
            "s1": _story("セカイに行こう\nカイトと歌う\n", "Let's go to SEKAI\nSing with KAITO\n"),
            "s2": _story("セカイは広い\n", "SEKAI is wide\n"),
            "s3": _story("セカイの歌\n", "Song of SEKAI\n"),
        }

    def _run(self) -> dict:
        from sekaisync import trinity

        groups = self._corpus()
        pool, _discovered = trinity.build_candidate_pool(groups, sorted(groups))
        return trinity.scrub_trinity(groups, sorted(groups), pool, target_languages=("en",))

    def test_decisions_reach_the_top_level_result(self):
        result = self._run()
        self.assertIn("slot_decisions", result)
        self.assertTrue(result["slot_decisions"])
        # One decision per (term, language), with the status vocabulary the
        # slot store uses.
        for row in result["slot_decisions"]:
            self.assertIn(row["status"], {"accepted", "pending", "conflict", "rejected"})
            self.assertTrue(row["term"])
            self.assertTrue(row["language"])

    def test_decisions_carry_the_supporting_story_keys(self):
        result = self._run()
        accepted = [r for r in result["slot_decisions"] if r["status"] == "accepted"]
        self.assertTrue(accepted, "fixture stopped producing an accepted slot")
        keys = set()
        for row in accepted:
            for item in row["evidence"]:
                evidence = (item.get("payload") or {}).get("evidence") or {}
                keys.update(evidence.get("story_keys") or [])
        # Non-empty is the point: `keys <= corpus` alone passes vacuously when
        # no payload carries story_keys at all, which is exactly the gap this
        # test exists to catch.
        self.assertTrue(keys, "no channel payload carried story_keys")
        self.assertTrue(
            keys <= set(self._corpus()),
            f"story_keys are not story identities: {sorted(keys)}",
        )

    def test_accepted_decisions_agree_with_the_accepted_map(self):
        """The handoff must not disagree with what the report shows."""
        result = self._run()
        from_decisions = {
            (row["term"], row["language"]): row["value"]
            for row in result["slot_decisions"] if row["status"] == "accepted"
        }
        from_accepted = {
            (term, lang): value
            for term, record in (result.get("accepted") or {}).items()
            for lang, value in (record.get("names") or {}).items()
        }
        self.assertEqual(from_decisions, from_accepted)


class ChannelEvidenceRowsTest(unittest.TestCase):
    """The six channel sites must produce rows the slot store can commit.

    `story_keys` (plural) told the caller *how many* stories backed a value, but
    a slot certificate is issued per evidence **row**, and each row must name
    one `story_key` plus a `language`, a `source`, and a sentence (or term) that
    contains the value — see `term_slots._certificate`, which rejects the whole
    proof when any of those is missing. Aggregated payloads therefore could not
    become decisions.

    The six sites are trunk distribution alignment and trunk surname backing,
    hub direct and hub backfill, translit, and L0 official. The fixture below is
    built to drive all six, so a regression in any one of them turns a
    channel-specific assertion red rather than passing on the other five.
    """

    GLOSSARY_TERMS = ("セカイ", "朝比奈")

    def _corpus(self) -> dict:
        """Three parallel stories that exercise every evidence site.

        Same-position layout matters: each ja line has its translation on the
        same index in every target language, which is what the hub's predicted
        line window (`_predict_index`) and the trunk aligner both rely on.
        """
        return {
            "s1": _story(
                "セカイに行こう\n朝比奈\nカイトと歌う\n",
                "Let's go to SEKAI\nI saw Asahina\nSing with KAITO\n",
                "去往世界\n朝比奈\n和KAITO唱歌\n",
                zh_tw="前往世界\n朝比奈\n和KAITO唱歌\n",
            ),
            "s2": _story(
                "セカイは広い\n朝比奈\nカイトの歌\n",
                "Big SEKAI\nI saw Asahina\nSing with KAITO\n",
                "世界很宽\n朝比奈\nKAITO的歌\n",
                zh_tw="世界很寬\n朝比奈\nKAITO的歌\n",
            ),
            "s3": _story(
                "セカイの歌\n朝比奈\nカイトと\n",
                "Song of SEKAI\nI saw Asahina\nSing with KAITO\n",
                "世界之歌\n朝比奈\n和KAITO\n",
                zh_tw="世界之歌\n朝比奈\n和KAITO\n",
            ),
        }

    def _glossary(self) -> list:
        """One official unit (L0 direct) and one character (surname backing).

        The character record carries `firstName`/`firstNameEnglish`, which is
        the only evidence that can back a kanji-only name — that is the trunk
        surname site, distinct from the distribution-alignment site.
        """
        from sekaisync.termindex import TermRecord

        return [
            TermRecord(
                id="unit:sekai", canonical="セカイ", source_language="ja",
                kind="unit", official=True, source="master_db",
                names={"ja": "セカイ", "en": "SEKAI",
                       "zh_hans": "世界", "zh_tw": "世界"},
            ),
            TermRecord(
                id="char:asahina", canonical="朝比奈", source_language="ja",
                kind="character", official=True, source="master_db",
                names={"ja": "朝比奈真冬", "en": "Mafuyu Asahina",
                       "zh_hans": "朝比奈真冬", "zh_tw": "朝比奈真冬",
                       "firstName": "朝比奈", "firstNameEnglish": "Asahina"},
            ),
        ]

    def _run(self) -> dict:
        from sekaisync import termindex, trinity

        groups = self._corpus()
        targets = ("en", "zh_hant", "zh_hans")
        glossary = self._glossary()
        vocab = termindex.build_alignment_vocab(groups, targets, glossary)
        idf = termindex.compute_lang_idf(groups, targets, vocab=vocab)
        return trinity.scrub_trinity(
            groups, sorted(groups), ["セカイ", "朝比奈", "カイト"],
            target_languages=targets, glossary=glossary, vocab=vocab, idf=idf,
        )

    @staticmethod
    def _rows_by_site(decision: dict) -> dict:
        """`{"<channel>/<method>": [rows]}` for one decision."""
        grouped: dict[str, list] = {}
        for row in decision.get("evidence_rows") or []:
            site = row["channel"]
            if row.get("channel_method"):
                site += "/" + row["channel_method"]
            grouped.setdefault(site, []).append(row)
        return grouped

    def _all_sites(self, result: dict) -> dict:
        sites: dict[str, list] = {}
        for decision in result["slot_decisions"]:
            for site, rows in self._rows_by_site(decision).items():
                sites.setdefault(site, []).extend(rows)
        return sites

    def _supporting_rows(self, decision: dict) -> list:
        """Rows that back this decision's own value (not the losing candidates).

        A decision carries rows for every candidate value the channels saw —
        the loser rows are the adjudication trail, not the proof. The certificate
        is only ever issued from rows matching the slot's value.
        """
        value = decision.get("value")
        return [row for row in decision.get("evidence_rows") or []
                if row.get("channel_value") == value]

    def _assert_rows_are_committable(self, result: dict) -> None:
        """Every non-official decision must pass the store's real certificate gate.

        Deliberately calls `term_slots._prepare_slot` / `_certificate` rather
        than re-implementing their rules: the contract belongs to the store, so
        a change there has to turn this red.
        """
        from sekaisync import term_slots

        checked = 0
        for decision in result["slot_decisions"]:
            if decision["status"] != "accepted":
                continue
            rows = self._supporting_rows(decision)
            if not rows:
                continue
            evidence = term_slots.evidence_with_ids(decision["term"], rows)
            slot = {
                "term_id": decision["term"], "language": decision["language"],
                "value": decision["value"], "status": "accepted",
                "source": rows[0]["source"], "confidence": 0.5,
                "evidence_refs": [row["evidence_id"] for row in evidence],
            }
            prepared = term_slots._prepare_slot(slot, evidence, self._verify)
            self.assertEqual(
                prepared["status"], "accepted",
                f"{decision['term']}/{decision['language']} did not certify: "
                f"{prepared['reason']}",
            )
            checked += 1
        self.assertTrue(checked, "no accepted decision carried supporting rows")

    @staticmethod
    def _verify(slot, evidence):
        """A verifier that authorises the corpus evidence only.

        It mirrors what a corpus-aware verifier must return: the proof echoes
        the slot identity, cites the evidence rows it checked, and does **not**
        claim official authority (so `_certificate` also enforces its
        "≥2 distinct story_key" rule for non-official proofs).
        """
        return {
            "subject_id": slot["term_id"], "language": slot["language"],
            "value": slot["value"], "source": slot["source"],
            "official": False, "trust": "C", "verifier": "corpus-evidence",
            "evidence_refs": [row["evidence_id"] for row in evidence],
        }

    # ── shape and coverage ──────────────────────────────────────────

    def test_every_channel_site_produces_rows(self):
        """All six evidence sites, not "at least one channel". """
        result = self._run()
        sites = self._all_sites(result)
        self.assertTrue(result["slot_decisions"], "fixture produced no decisions")
        expected = {
            "L0": "L0 official name",
            "trunk": "trunk distribution alignment",
            "trunk/glossary_surname": "trunk surname backing",
            "hub": "hub direct",
            "hub/backfill": "hub backfill",
            "translit": "translit",
        }
        for site, label in expected.items():
            with self.subTest(site=site):
                self.assertTrue(
                    sites.get(site), f"{label} produced no evidence rows"
                )

    def test_rows_carry_a_singular_story_key_and_a_value_bearing_sentence(self):
        """The four fields `term_slots._certificate` reads must all be present.

        `story_key` is singular on purpose: the certificate counts *distinct*
        story identities across rows, so a row holding a list would count as
        one key no matter how many stories it lists.
        """
        result = self._run()
        corpus_keys = set(self._corpus())
        checked = 0
        for decision in result["slot_decisions"]:
            for row in decision.get("evidence_rows") or []:
                checked += 1
                with self.subTest(term=decision["term"], row=row):
                    self.assertIsInstance(row["story_key"], str)
                    self.assertIn(row["story_key"], corpus_keys)
                    self.assertEqual(row["language"], decision["language"])
                    self.assertEqual(row["term"], decision["term"])
                    self.assertTrue(row["source"])
                    # term == value (an official name equal to the source word)
                    # OR the sentence carries the value. Both are the store's
                    # accepted ways to attest a value; anything else is not
                    # evidence and must not have been emitted.
                    self.assertTrue(
                        row["term"] == row["channel_value"]
                        or row["channel_value"] in row["sentence"],
                        f"row attests {row['channel_value']!r} with neither a "
                        f"matching term nor a sentence containing it",
                    )
        self.assertTrue(checked, "no decision carried evidence rows")

    def test_non_official_channels_name_at_least_two_stories(self):
        """`term_slots.py:151` refuses a non-official proof with <2 story keys.

        A single story is not corroboration: the same mistranslation can recur
        throughout one story. Rows without that spread are not committable, and
        the converter must not paper over it by repeating one key.
        """
        result = self._run()
        sites = self._all_sites(result)
        checked = 0
        for site, rows in sites.items():
            if site == "L0":
                continue
            for value in {row["channel_value"] for row in rows}:
                keys = {row["story_key"] for row in rows
                        if row["channel_value"] == value}
                with self.subTest(site=site, value=value):
                    self.assertGreaterEqual(
                        len(keys), 2,
                        f"{site} vouched for {value!r} from a single story: {keys}",
                    )
                checked += 1
        self.assertTrue(checked, "no non-official channel rows to check")

    # ── the store's own gate ────────────────────────────────────────

    def test_rows_pass_the_store_certificate_gate(self):
        """Green path: the rows above satisfy `term_slots._prepare_slot`."""
        self._assert_rows_are_committable(self._run())

    def test_rows_survive_the_evidence_table_round_trip(self):
        """Rows must be storable: `dbstore._evidence_rows_for` keeps them whole.

        The slot store writes evidence through that helper, and everything it
        does not recognise lands in `extra_json` — so a row that survives it
        with `story_key`/`language`/`term`/`sentence` intact is committable.
        """
        from sekaisync import dbstore

        result = self._run()
        decisions = [d for d in result["slot_decisions"] if self._supporting_rows(d)]
        self.assertTrue(decisions)
        for decision in decisions:
            rows = self._supporting_rows(decision)
            stored = dbstore._evidence_rows_for(decision["term"], rows)
            self.assertEqual(len(stored), len(rows))
            for row, (term_id, _idx, story_key, language, term, sentence, extra) in zip(rows, stored):
                with self.subTest(term=decision["term"]):
                    self.assertEqual(term_id, decision["term"])
                    self.assertEqual(story_key, row["story_key"])
                    self.assertEqual(language, row["language"])
                    self.assertEqual(term, row["term"])
                    self.assertEqual(sentence, row["sentence"])
                    self.assertIn(decision["value"], sentence + term)
                    self.assertIn(row["channel"], extra)

    # ── red: the assertions must bite when the conversion is absent ──

    def test_the_assertions_bite_when_the_converter_returns_nothing(self):
        """Red path, kept runnable: strip the conversion and prove it turns red.

        Without this, the tests above could pass on a fixture that never
        produced rows in the first place — `assertTrue(sites)` on an empty dict
        is exactly the vacuous pass this guards against.
        """
        from unittest import mock

        from sekaisync import trinity

        with mock.patch.object(trinity, "decision_evidence_rows", lambda *a, **k: []):
            stripped = self._run()
        self.assertTrue(stripped["slot_decisions"], "fixture stopped producing decisions")
        for decision in stripped["slot_decisions"]:
            self.assertFalse(decision.get("evidence_rows"))
        for assertion in (
            self._assert_rows_are_committable,
            lambda res: self.assertTrue(self._all_sites(res)),
        ):
            with self.assertRaises(AssertionError):
                assertion(stripped)

    def test_the_assertions_bite_when_story_key_is_dropped(self):
        """Red path: a row without its singular `story_key` cannot certify.

        The plural `story_keys` list is not a substitute — this is the exact
        shape the six sites produced before the conversion existed.
        """
        from unittest import mock

        from sekaisync import trinity

        real = trinity.decision_evidence_rows

        def drop_story_key(decision, corpus, **kwargs):
            rows = real(decision, corpus, **kwargs)
            for row in rows:
                row.pop("story_key", None)
            return rows

        with mock.patch.object(trinity, "decision_evidence_rows", drop_story_key):
            result = self._run()
        self.assertTrue(any(
            decision.get("evidence_rows") for decision in result["slot_decisions"]
        ), "mutation removed the rows instead of the field")
        with self.assertRaises(AssertionError):
            self._assert_rows_are_committable(result)
