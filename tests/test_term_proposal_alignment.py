"""The extraction boundary must certify the same spans as the aligner."""
import unittest

from sekaisync.term_proposals import located_pair, source_proposal, validate_translation_proposal


class ProposalAlignmentTests(unittest.TestCase):
    def locate(self, source_text, target_text, source="月虹祭", target="Moonbow Festival"):
        return located_pair(source, target, source_text, target_text,
                            story_key="event:99002:1", source_language="ja", language="en",
                            source_page={"id": "source"}, target_page={"id": "target"})

    def test_shifted_quote_uses_aligned_occurrence_and_exact_offsets(self):
        source = 'A1 START\nB2 「月虹祭」へ行こう。\nC3 END'
        target = 'INTRO99 "Moonbow Festival" was last year.\nA1 START\nB2 Visit "Moonbow Festival".\nC3 END'
        row = self.locate(source, target)
        self.assertTrue(row["verified"])
        self.assertEqual(row["line_index"], 2)
        self.assertEqual(target[row["start"]:row["start"] + len(row["term"])], row["term"])
        self.assertEqual(source[row["source_start"]:row["source_start"] + len(row["source_term"])],
                         row["source_term"])

    def test_off_position_mention_is_not_evidence(self):
        self.assertEqual(self.locate('A1 月虹祭\nB2 またね',
                                     'A1 Hello\nB2 Moonbow Festival'), {})

    def test_literal_brand_must_occur_in_body_not_speaker(self):
        self.assertEqual(self.locate('ORION：こんにちは。', 'ORION: Hello.',
                                     source="ORION", target="ORION"), {})
        row = self.locate('A1 ORIONに行こう。', 'A1 Visit ORION.',
                          source="ORION", target="ORION")
        self.assertTrue(row["verified"])
        self.assertEqual(row["verification"], "aligned_literal")

    def test_literal_substring_cannot_certify_different_word(self):
        self.assertEqual(self.locate('A1 ARTISTが来た。', 'A1 ARTIST is here.',
                                     source="ART", target="ART"), {})

    def test_aligned_unquoted_cooccurrence_remains_unverified(self):
        row = self.locate('A1 月虹祭に行こう。', 'A1 Visit Moonbow Festival.')
        self.assertFalse(row["verified"])

    def test_multiple_source_quotes_in_one_turn_are_ambiguous(self):
        source = '司：「月虹祭」と\n「星庭」を訪ねよう。\n類：あとで。'
        target = 'Tsukasa: Visit "Moonbow Festival".\nRui: Later.'
        row = self.locate(source, target)
        self.assertFalse(row.get("verified", False))

    def test_offsets_skip_namesake_speaker_and_substring_decoy(self):
        source = 'ART：ARTISTとARTが来た。'
        target = 'ART: ARTIST and ART arrived.'
        row = self.locate(source, target, source="ART", target="ART")
        self.assertTrue(row["verified"])
        self.assertEqual(row["source_start"], source.rindex("ART"))
        self.assertEqual(row["start"], target.rindex("ART"))

    def test_source_proposals_require_body_word_boundaries(self):
        for term in ("ART", "Tsukasa"):
            self.assertFalse(source_proposal(dict(term=term, confidence=.9),
                                             "Tsukasa: We are ARTISTS."))
        self.assertTrue(source_proposal(dict(term="月虹祭", confidence=.9),
                                        '司：「月虹\n祭」へ行こう。'))

    def test_quote_offsets_point_to_the_quoted_occurrence(self):
        source = '司：月虹祭という名前の「月虹祭」へ行こう。'
        target = 'Tsukasa: Moonbow Festival is the name. Visit "Moonbow Festival".'
        row = self.locate(source, target)
        self.assertEqual(row["verification"], "corresponding_unique_quotes")
        self.assertEqual(row["source_start"], source.rindex("月虹祭"))
        self.assertEqual(row["start"], target.rindex("Moonbow Festival"))

    def test_soft_wrapped_quotes_preserve_exact_observed_surfaces(self):
        source = '司：「月虹\n祭」へ行こう。\n類：あとで。'
        target = 'Tsukasa: Visit "Moonbow\nFestival".\nRui: Later.'
        row = self.locate(source, target)
        self.assertTrue(row["verified"])
        self.assertEqual(row["source_observed_surface"], "月虹\n祭")
        self.assertEqual(row["observed_surface"], "Moonbow\nFestival")
        self.assertEqual(source[row["source_start"]:row["source_start"] + len(row["source_observed_surface"])],
                         row["source_observed_surface"])
        self.assertEqual(target[row["start"]:row["start"] + len(row["observed_surface"])], row["observed_surface"])
        proposal = dict(term="月虹祭", translation="Moonbow Festival", confidence=1.)
        decision = validate_translation_proposal(proposal, language="en",
                                                evidence=[row, dict(row, story_key="event:99002:2")])
        self.assertEqual(decision["status"], "accepted")

    def test_blank_lines_do_not_change_evidence_offsets(self):
        source = '\nA1 「月虹祭」\n\nB2 END'
        target = 'A1 "Moonbow Festival"\nB2 END'
        row = self.locate(source, target)
        self.assertTrue(row["verified"])
        self.assertEqual(row["source_line_index"], 1)
        self.assertEqual(row["source_start"], source.index("月虹祭"))

    def test_repeated_evidence_from_one_story_is_not_corroboration(self):
        row = self.locate('A1 「月虹祭」', 'A1 "Moonbow Festival"')
        proposal = dict(term="月虹祭", translation="Moonbow Festival", confidence=1.)
        self.assertEqual(validate_translation_proposal(proposal, evidence=[row, row], language="en")["status"],
                         "pending")
        other = dict(row, story_key="event:99002:2")
        self.assertEqual(validate_translation_proposal(proposal, evidence=[row, other], language="en")["status"],
                         "accepted")


if __name__ == "__main__":
    unittest.main()
