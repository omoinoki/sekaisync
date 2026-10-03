"""Real-shaped master fields must survive extraction and rendering."""

import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.factpacks import build_fact_pack, build_fact_pack_with_context
from sekaisync.models import Entity
from sekaisync.regions import entity_for_region
from sekaisync.registry import build_registry, lookup_entity


class CardMasterSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="card_master_schema_")
        self.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name)

    def write_cards(self, region, records):
        path = self.store / "raw" / region / "source" / "cards.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records), encoding="utf-8")

    def card_record(self, language, *, rarity="rarity_1", attr="cool"):
        skill = "JP moonlit paper crane" if language == "JP" else "EN warm music circle"
        trained = "JP winter floral greeting" if language == "JP" else "EN ocean dawn farewell"
        return {
            "id": 1,
            "seq": 10,
            "characterId": 23,
            "prefix": f"{language} localized card title",
            "cardRarityType": rarity,
            "attr": attr,
            "cardSkillName": skill,
            "specialTrainingSkillName": trained,
            "assetName": "res001_no001",
            "assetbundleName": "res001_no001",
            "releaseAt": 1601391600000,
        }

    def build_card(self, regions):
        return next(entity for entity in build_registry(self.store, regions)
                    if entity.id == "card:1")

    def test_current_card_fields_survive_registry_ingestion(self):
        self.write_cards("jp", [self.card_record("JP")])
        card = self.build_card(["jp"])

        self.assertEqual(card.names["ja"], "JP localized card title")
        facts = card.region_facts["jp"].facts
        self.assertEqual(facts["characterId"], 23)
        self.assertEqual(facts["prefix_ja"], "JP localized card title")
        self.assertEqual(facts["cardRarityType"], "rarity_1")
        self.assertEqual(facts["attr"], "cool")
        self.assertEqual(facts["cardSkillName_ja"], "JP moonlit paper crane")
        self.assertEqual(facts["specialTrainingSkillName_ja"], "JP winter floral greeting")
        self.assertEqual(card.region_facts["jp"].retrieval["table"], "cards")
        self.assertNotIn("assetbundleName", facts)

    def test_localized_title_and_skills_are_searchable(self):
        self.write_cards("jp", [self.card_record("JP")])
        card = self.build_card(["jp"])

        for query in ("JP localized card title", "JP moonlit paper crane",
                      "JP winter floral greeting"):
            with self.subTest(query=query):
                matches = lookup_entity([card], query, type="card")
                self.assertEqual(matches[0][0].id, "card:1")

    def test_regional_card_differences_are_not_common_facts(self):
        self.write_cards("jp", [self.card_record("JP")])
        self.write_cards("en", [self.card_record("EN", rarity="rarity_4", attr="happy")])
        card = self.build_card(["jp", "en"])

        self.assertNotIn("cardRarityType", card.facts)
        self.assertNotIn("attr", card.facts)
        self.assertNotIn("cardSkillName_ja", card.facts)
        self.assertNotIn("cardSkillName_en", card.facts)
        jp = entity_for_region(card, "jp")
        en = entity_for_region(card, "en")
        self.assertEqual(jp["facts"]["cardRarityType"], "rarity_1")
        self.assertEqual(jp["facts"]["attr"], "cool")
        self.assertEqual(en["facts"]["cardRarityType"], "rarity_4")
        self.assertEqual(en["facts"]["attr"], "happy")
        self.assertTrue(lookup_entity([card], "JP moonlit paper crane", type="card"))
        self.assertTrue(lookup_entity([card], "JP moonlit paper crane", type="card", region="jp"))
        self.assertFalse(lookup_entity([card], "JP moonlit paper crane", type="card", region="en"))

    def test_actual_card_title_and_skills_render_instead_of_asset_name(self):
        self.write_cards("jp", [self.card_record("JP")])
        card = self.build_card(["jp"])
        pack = build_fact_pack(card, language="ja")

        self.assertEqual(pack.text.splitlines()[0], "Card: JP localized card title")
        self.assertNotIn("res001_no001", pack.text)
        self.assertIn("Rarity: rarity_1", pack.text)
        self.assertIn("Attribute: cool", pack.text)
        self.assertIn("Skill: JP moonlit paper crane", pack.text)
        self.assertIn("JP winter floral greeting", pack.text)

    def test_language_selects_one_card_snapshot_and_discloses_provenance(self):
        self.write_cards("jp", [self.card_record("JP")])
        self.write_cards("en", [self.card_record("EN", rarity="rarity_4", attr="happy")])
        card = self.build_card(["jp", "en"])
        pack, context = build_fact_pack_with_context(card, language="en")

        self.assertEqual(pack.text.splitlines()[0], "Card: EN localized card title")
        self.assertIn("Skill: EN warm music circle", pack.text)
        self.assertIn("EN ocean dawn farewell", pack.text)
        self.assertIn("Rarity: rarity_4", pack.text)
        self.assertIn("Attribute: happy", pack.text)
        self.assertNotIn("JP moonlit paper crane", pack.text)
        self.assertEqual(context["region"], "en")
        self.assertEqual(context["effective_language"], "en")
        self.assertEqual(context["source"], "master_db:en")
        self.assertEqual(context["retrieval"]["table"], "cards")

        exact, exact_context = build_fact_pack_with_context(card, language="en", region="jp")
        self.assertIn("Skill: JP moonlit paper crane", exact.text)
        self.assertIn("Rarity: rarity_1", exact.text)
        self.assertIn("Attribute: cool", exact.text)
        self.assertNotIn("EN warm music circle", exact.text)
        self.assertEqual(exact_context["region"], "jp")
        self.assertEqual(exact_context["source"], "master_db:jp")

    def test_generic_card_aliases_remain_compatible(self):
        self.write_cards("en", [{
            "id": 7,
            "name": "Legacy schema card",
            "rarity": 4,
            "attribute": "cute",
            "skillName": "Legacy named skill",
        }])
        card = next(entity for entity in build_registry(self.store, ["en"])
                    if entity.id == "card:7")
        pack = build_fact_pack(card, language="en")
        self.assertIn("Card: Legacy schema card", pack.text)
        self.assertIn("Rarity: 4", pack.text)
        self.assertIn("Attribute: cute", pack.text)
        self.assertIn("Skill: Legacy named skill", pack.text)

        legacy = Entity(
            id="card:8", type="card", region="", names={"en": "Legacy stored card"},
            facts={"rarityId": 3, "attribute": "pure", "skill": "Legacy skill"},
        )
        stored = build_fact_pack(legacy, language="en")
        self.assertIn("Card: Legacy stored card", stored.text)
        self.assertIn("Rarity: 3", stored.text)
        self.assertIn("Attribute: pure", stored.text)
        self.assertIn("Skill: Legacy skill", stored.text)


class LocalizedDescriptiveMasterSchemaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="descriptive_master_schema_")
        self.addCleanup(temporary.cleanup)
        self.store = Path(temporary.name)

    def write_table(self, region, table, records):
        path = self.store / "raw" / region / "source" / f"{table}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records), encoding="utf-8")

    def test_mission_sentence_is_preserved_searchable_and_rendered(self):
        self.write_table("jp", "beginnerMissionV2s", [{
            "id": 1, "seq": 1, "sentence": "Find the lunar key",
            "beginnerMissionV2Type": "any_live_clear",
            "beginnerMissionV2Category": "normal", "requirement": 3,
        }])
        mission = next(entity for entity in build_registry(self.store, ["jp"])
                       if entity.id == "beginner_mission_v2:1")
        self.assertEqual(mission.region_facts["jp"].facts["sentence_ja"],
                         "Find the lunar key")
        matches = lookup_entity([mission], "lunar", type="beginner_mission_v2")
        self.assertEqual(matches[0][0].id, "beginner_mission_v2:1")
        pack = build_fact_pack(mission, language="ja")
        self.assertIn("Sentence: Find the lunar key", pack.text)
        self.assertNotIn("requirementType", mission.facts)
        self.assertNotIn("requiredCount", mission.facts)

    def test_mission_sentence_keeps_regional_language_provenance(self):
        self.write_table("jp", "normalMissions", [{
            "id": 7, "normalMissionType": "skill_level_2",
            "sentence": "Find the lunar key", "requirement": 3,
        }])
        self.write_table("en", "normalMissions", [{
            "id": 7, "normalMissionType": "skill_level_2",
            "sentence": "Collect an ocean pearl", "requirement": 3,
        }])
        mission = next(entity for entity in build_registry(self.store, ["jp", "en"])
                       if entity.id == "normal_mission:7")
        self.assertNotIn("sentence_ja", mission.facts)
        self.assertNotIn("sentence_en", mission.facts)
        self.assertTrue(lookup_entity([mission], "lunar", type="normal_mission"))
        self.assertFalse(lookup_entity([mission], "lunar", type="normal_mission", region="en"))
        pack, context = build_fact_pack_with_context(mission, language="en")
        self.assertIn("Sentence: Collect an ocean pearl", pack.text)
        self.assertNotIn("Find the lunar key", pack.text)
        self.assertEqual(context["region"], "en")
        self.assertEqual(context["effective_language"], "en")
        self.assertEqual(context["source"], "master_db:en")
        self.assertEqual(context["retrieval"]["table"], "normalMissions")
        self.assertIn("sha256", context["retrieval"])

    def test_item_flavor_text_survives_all_confirmed_master_types(self):
        fixtures = (
            ("areaItems", "area_item", {"id": 1, "areaId": 5}),
            ("mysekaiFixtures", "mysekai_fixture", {
                "id": 1, "seq": 10001001, "mysekaiFixtureType": "system",
            }),
            ("eventItems", "event_item", {"id": 1, "eventId": 3}),
        )
        for table, kind, identity in fixtures:
            self.write_table("jp", table, [{
                **identity, "name": "Moon lantern", "flavorText": "A luminous moon lantern",
                "assetbundleName": "fixture_technical_asset",
            }])
        entities = build_registry(self.store, ["jp"])
        for table, kind, _ in fixtures:
            with self.subTest(table=table):
                entity = next(entity for entity in entities if entity.type == kind)
                self.assertEqual(entity.region_facts["jp"].facts["flavorText_ja"],
                                 "A luminous moon lantern")
                matches = lookup_entity([entity], "luminous", type=kind)
                self.assertEqual(matches[0][0].id, f"{kind}:1")
                pack, context = build_fact_pack_with_context(entity, language="ja")
                self.assertIn("Flavor: A luminous moon lantern", pack.text)
                self.assertEqual(context["retrieval"]["table"], table)
                self.assertNotIn("effect", entity.facts)
                self.assertNotIn("powerBonus", entity.facts)
                self.assertNotIn("Effect:", pack.text)

    def test_item_flavor_text_does_not_merge_regional_descriptions(self):
        for region, flavor in (("jp", "A luminous moon lantern"),
                               ("en", "Exchange this token for event rewards")):
            self.write_table(region, "eventItems", [{
                "id": 9, "eventId": 3, "name": "Festival token", "flavorText": flavor,
            }])
        item = next(entity for entity in build_registry(self.store, ["jp", "en"])
                    if entity.id == "event_item:9")
        self.assertNotIn("flavorText_ja", item.facts)
        self.assertNotIn("flavorText_en", item.facts)
        self.assertEqual(item.region_facts["jp"].facts["flavorText_ja"],
                         "A luminous moon lantern")
        self.assertEqual(item.region_facts["en"].facts["flavorText_en"],
                         "Exchange this token for event rewards")
        self.assertTrue(lookup_entity([item], "luminous", type="event_item"))
        self.assertFalse(lookup_entity([item], "luminous", type="event_item", region="en"))
        pack, context = build_fact_pack_with_context(item, language="en")
        self.assertIn("Flavor: Exchange this token for event rewards", pack.text)
        self.assertNotIn("luminous", pack.text)
        self.assertEqual(context["region"], "en")
        self.assertEqual(context["effective_language"], "en")
        self.assertEqual(context["source"], "master_db:en")
        self.assertEqual(context["retrieval"]["table"], "eventItems")


if __name__ == "__main__":
    unittest.main()
