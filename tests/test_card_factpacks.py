import unittest

from sekaisync.factpacks import build_fact_pack, build_fact_pack_with_context
from sekaisync.models import Entity, RegionFacts
from sekaisync.regions import project_common_facts


class CardFactPackTest(unittest.TestCase):
    def _entity(self, facts):
        return Entity(id="card:123", type="card", region="jp", regions=["jp"],
                      names={"en": "Legacy card title"}, facts=facts, source="master_db:jp")

    def test_actual_upstream_card_fields_render_without_renaming_raw_facts(self):
        entity = self._entity({
            "prefix_ja": "Japanese card title",
            "cardRarityType": "rarity_4", "attr": "cute", "characterId": 17,
            "cardSkillName_ja": "Japanese skill",
            "specialTrainingSkillName_ja": "Japanese trained skill",
        })
        text = build_fact_pack(entity, "ja").text
        for expected in (
            "Card: Japanese card title", "Rarity: rarity_4", "Attribute: cute",
            "Character ID: 17", "Skill: Japanese skill",
            "Trained Skill: Japanese trained skill",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)
        self.assertNotIn("Skill: Japanese card title", text)

    def test_card_skill_fallback_is_language_first_across_field_aliases(self):
        entity = self._entity({"cardSkillName_ja": "Japanese skill", "skillName_en": "English skill"})
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertIn("Skill: English skill", pack.text)
        self.assertNotIn("Japanese skill", pack.text)
        self.assertEqual(context["effective_language"], "en")
        self.assertEqual(context["body_field"], "skillName_en")

    def test_legacy_card_aliases_still_render(self):
        entity = self._entity({"rarity": "4", "attribute": "cool", "skill": "Legacy skill"})
        text = build_fact_pack(entity, "en").text
        self.assertIn("Rarity: 4", text)
        self.assertIn("Attribute: cool", text)
        self.assertIn("Skill: Legacy skill", text)

    def test_card_title_alone_is_available_body(self):
        pack, context = build_fact_pack_with_context(self._entity({"prefix_en": "Actual title"}), "en")
        self.assertIn("Card: Actual title", pack.text)
        self.assertNotIn("Skill:", pack.text)
        self.assertEqual(context["content_status"], "available")
        self.assertEqual(context["effective_language"], "en")

    def test_multiregion_card_metadata_comes_from_selected_title_snapshot(self):
        entity = self._entity({})
        entity.regions = ["en", "jp"]
        entity.region_facts = {
            "en": RegionFacts("en", {
                "prefix_en": "EN title", "attr": "cool", "cardRarityType": "rarity_4",
                "cardSkillName_en": "EN skill", "releaseAt": 1_700_000_000_000,
            }, "master_db:en", "v-en", {"hash": "h-en"}),
            "jp": RegionFacts("jp", {
                "prefix_ja": "JP title", "attr": "cute", "cardRarityType": "rarity_3",
                "cardSkillName_ja": "JP skill", "releaseAt": 1_600_000_000_000,
            }, "master_db:jp", "v-jp", {"hash": "h-jp"}),
        }
        entity.facts, _ = project_common_facts(entity.region_facts, entity.regions)
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertEqual(context["region"], "en")
        self.assertEqual(context["source"], "master_db:en")
        self.assertEqual(context["version"], "v-en")
        self.assertIn("Card: EN title", pack.text)
        self.assertIn("Attribute: cool", pack.text)
        self.assertIn("Rarity: rarity_4", pack.text)
        self.assertIn("Skill: EN skill", pack.text)
        self.assertNotIn("JP title", pack.text)
        self.assertNotIn("cute", pack.text)


if __name__ == "__main__":
    unittest.main()
