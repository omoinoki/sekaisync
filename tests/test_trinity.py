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
