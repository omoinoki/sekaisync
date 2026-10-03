import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.registry import RawSnapshot, build_registry, lookup_entity


class CharacterProfileRegistryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="profile-registry-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_table(self, region, table, records):
        path = self.root / "raw" / region / "source" / f"{table}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
        return path

    def write_character(self, region="jp", name="\u671d\u6bd4\u5948\u307e\u3075\u3086"):
        return self.write_table(region, "gameCharacters", [{"id": 18, "name": name}])

    def write_profile(self, region="jp", **overrides):
        record = {
            "id": 101, "characterId": 18, "characterVoice": "Actor",
            "birthday": "January 27", "height": "162cm", "school": "Academy",
            "schoolYear": "3-B", "hobby": "Aquariums", "specialSkill": "English",
            "favoriteFood": "Home cooking", "hatedFood": "Unknown", "weak": "Unknown",
            "introduction": "UniqueJapaneseNeedle: lyricist", "scenarioId": "self_18",
        }
        record.update(overrides)
        return self.write_table(region, "characterProfiles", [record])

    def profile(self, regions=("jp",), **kwargs):
        return next(e for e in build_registry(self.root, regions, **kwargs)
                    if e.id == "character_profile:101")

    def test_profile_body_and_descriptive_fields_are_preserved_with_language(self):
        self.write_character()
        self.write_profile()
        profile = self.profile()
        self.assertEqual(profile.facts["introduction_ja"], "UniqueJapaneseNeedle: lyricist")
        for field in ("characterVoice", "birthday", "school", "schoolYear", "hobby",
                      "specialSkill", "favoriteFood", "hatedFood", "weak"):
            self.assertIn(f"{field}_ja", profile.facts)
        self.assertEqual(profile.facts["school"], "Academy")
        self.assertEqual(profile.facts["characterId"], 18)
        self.assertEqual(profile.facts["scenarioId"], "self_18")
        self.assertEqual(profile.facts["height"], "162cm")
        self.assertEqual(profile.trust, "A")
        self.assertEqual(profile.region_facts["jp"].retrieval["table"], "characterProfiles")

    def test_name_join_uses_foreign_key_not_profile_id_and_keeps_source(self):
        character_path = self.write_character()
        self.write_profile()
        profile = self.profile()
        self.assertEqual(profile.names["ja"], "\u671d\u6bd4\u5948\u307e\u3075\u3086")
        matches = lookup_entity([profile], profile.names["ja"], type="character_profile")
        self.assertEqual(matches[0][0].id, "character_profile:101")
        self.assertEqual(lookup_entity([profile], "character_profile:101")[0][0].id,
                         "character_profile:101")
        source = profile.region_facts["jp"].retrieval["name_sources"]["character:18"]
        self.assertEqual(source["table"], "gameCharacters")
        self.assertEqual(source["path"], str(character_path.resolve()))
        self.assertIn("sha256", source)

    def test_no_false_name_join_when_foreign_key_or_same_region_character_is_missing(self):
        self.write_table("jp", "gameCharacters", [{"id": 101, "name": "Wrong character"}])
        self.write_character("en", "English character")
        for value in (None, 18, 999):
            with self.subTest(characterId=value):
                self.write_profile(characterId=value)
                self.assertEqual(self.profile(("jp", "en")).names, {})

    def test_multiple_profiles_are_independently_reachable(self):
        self.write_character()
        self.write_table("jp", "characterProfiles", [
            {"id": 101, "characterId": 18, "introduction": "First snapshot"},
            {"id": 102, "characterId": 18, "introduction": "Second snapshot"},
        ])
        entities = build_registry(self.root, ["jp"])
        matches = lookup_entity(entities, "\u671d\u6bd4\u5948\u307e\u3075\u3086",
                                type="character_profile")
        self.assertEqual({e.id for e, _ in matches}, {"character_profile:101", "character_profile:102"})
        self.assertEqual(lookup_entity(entities, "102", type="character_profile")[0][0].id,
                         "character_profile:102")

    def test_multiregion_lookup_searches_body_without_fabricating_common_facts(self):
        self.write_character()
        self.write_profile()
        self.write_character("en", "Mafuyu Asahina")
        self.write_profile("en", introduction="UniqueEnglishNeedle: lyricist", school="Other academy")
        profile = self.profile(("en", "jp"))
        self.assertNotIn("introduction_ja", profile.facts)
        self.assertNotIn("introduction_en", profile.facts)
        self.assertEqual(profile.names["en"], "Mafuyu Asahina")
        for region, needle in (("jp", "UniqueJapaneseNeedle"), ("en", "UniqueEnglishNeedle")):
            self.assertTrue(lookup_entity([profile], needle))
            self.assertTrue(lookup_entity([profile], needle, region=region))
        self.assertFalse(lookup_entity([profile], "UniqueJapaneseNeedle", region="en"))
        self.assertFalse(lookup_entity([profile], "UniqueEnglishNeedle", region="cn"))

    def test_name_join_obeys_pinned_snapshot_and_generation(self):
        self.write_character(name="Wrong active name")
        self.write_profile(introduction="Wrong active introduction")
        pinned = self.root / "pinned"
        pinned.mkdir()
        (pinned / "gameCharacters.json").write_text(json.dumps([
            {"id": 18, "name": "Pinned character"}]), encoding="utf-8")
        (pinned / "characterProfiles.json").write_text(json.dumps([
            {"id": 101, "characterId": 18, "introduction": "Pinned introduction"}]), encoding="utf-8")
        snapshot = RawSnapshot({"jp": pinned}, {"jp": "generation-A"})
        profile = self.profile(raw_snapshot=snapshot)
        self.assertEqual(profile.names["ja"], "Pinned character")
        self.assertEqual(profile.facts["introduction_ja"], "Pinned introduction")
        self.assertEqual(profile.region_facts["jp"].retrieval["name_sources"]
                         ["character:18"]["generation"], "generation-A")


if __name__ == "__main__":
    unittest.main()
