"""Synthetic raw-topology audit guards, without any production corpus reads."""
import unittest

from scripts.audit_scraper_domain_entry import indexed, tweet_bindings


class DomainIdentityAuditTests(unittest.TestCase):
    def test_talk_binding_keeps_entire_asset_path_not_just_numeric_record(self):
        talks = {"7": dict(id=7, assetbundleName="bundle/nested", lua="talk_full_0007")}
        rows = {"mysekaiCharacterTalkPreActions": [dict(id=3, mysekaiCharacterTalkId=7,
                                                      mysekaiCharacterTalkTweetId=101)]}
        bindings, identities, _ = tweet_bindings(talks, rows)
        self.assertEqual(bindings["101"], {("talk", "7", "bundle/nested", "talk_full_0007")})
        self.assertEqual(identities[("mysekaiCharacterTalkPreActions", "3")][0], "101")

    def test_dangling_talk_and_incomplete_tutorial_cannot_prove_family(self):
        rows = {
            "mysekaiCharacterTalkPreActions": [dict(id=3, mysekaiCharacterTalkId=7,
                                                    mysekaiCharacterTalkTweetId=101)],
            "mysekaiTutorialTalks": [dict(id=4, mysekaiCharacterTalkTweetId=102, lua="tutorial")],
            "mysekaiCharacterTalkTweetWithoutRelatedTalks": [dict(id=5, mysekaiCharacterTalkTweetId=103)],
        }
        bindings, _, _ = tweet_bindings({}, rows)
        self.assertFalse(bindings)

    def test_optional_fields_do_not_hide_same_family_but_remain_diagnostic(self):
        talks = {"7": dict(id=7, assetbundleName="bundle", lua="talk")}
        first = dict(id=3, mysekaiCharacterTalkId=7, mysekaiCharacterTalkTweetId=101)
        second = dict(first, mysekaiCharacterTalkFixtureTogetherCommunicationId=1)
        a = tweet_bindings(talks, {"mysekaiCharacterTalkPreActions": [first]})
        b = tweet_bindings(talks, {"mysekaiCharacterTalkPreActions": [second]})
        self.assertEqual(a[0], b[0])
        self.assertEqual(a[1], b[1])
        self.assertNotEqual(a[2], b[2])

    def test_region_local_number_reuse_has_different_structural_identity(self):
        def fixture(path):
            return tweet_bindings({"7": dict(id=7, assetbundleName=path, lua="talk")}, {
                "mysekaiCharacterTalkPreActions": [dict(id=3, mysekaiCharacterTalkId=7,
                                                        mysekaiCharacterTalkTweetId=101)]})
        a, b = fixture("actual/family"), fixture("foreign/family")
        self.assertFalse(a[0]["101"] & b[0]["101"])
        self.assertNotEqual(a[1], b[1])

    def test_repeated_edge_identity_fails_fast(self):
        edge = dict(id=3, groupId=10, mysekaiCharacterTalkTweetId=101)
        with self.assertRaisesRegex(ValueError, "duplicate inbound edge identity"):
            tweet_bindings({}, {"mysekaiCharacterTalkFixtureCommonTweetGroups": [edge, edge]})

    def test_raw_index_preserves_complete_scenario_identity_and_rejects_duplicates(self):
        rows = [dict(scenarioId="self_fixture"), dict(scenarioId="self_fixture_2nd"),
                dict(scenarioId="scenario:101:22")]
        self.assertEqual(set(indexed(rows, "scenarioId")), {row["scenarioId"] for row in rows})
        with self.assertRaisesRegex(ValueError, "duplicate raw scenarioId"):
            indexed(rows + [rows[0]], "scenarioId")


if __name__ == "__main__":
    unittest.main()
