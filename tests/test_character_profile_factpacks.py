import json
import unittest

from sekaisync.factpacks import (
    _effective_body_language,
    build_fact_pack,
    build_fact_pack_at,
    build_fact_pack_with_context,
)
from sekaisync.models import Entity, RegionFacts
from sekaisync.regions import project_common_facts


class ProfileFixtureMixin:
    def _entity(self, facts=None, *, kind="character_profile"):
        return Entity(
            id=f"{kind}:101",
            type=kind,
            region="jp",
            regions=["jp"],
            names={"en": "Yoisaki Kanade", "ja": "Kanade JP", "zh_hant": "Kanade TC"},
            facts=facts or {},
            source="master_db:jp",
            version="legacy",
        )

    def _region_entity(self, facts_by_region, *, kind="character_profile"):
        entity = self._entity(kind=kind)
        entity.regions = sorted(facts_by_region)
        entity.region_facts = {
            region: RegionFacts(
                region, facts, f"master_db:{region}", f"version-{region}",
                {"table": "characterProfiles", "hash": f"hash-{region}"},
            )
            for region, facts in facts_by_region.items()
        }
        entity.facts, _ = project_common_facts(entity.region_facts, entity.regions)
        return entity


class CharacterProfileFactPackTest(ProfileFixtureMixin, unittest.TestCase):
    def test_introduction_is_rendered_in_profile(self):
        entity = self._entity({"introduction_ja": "Official songwriter snapshot"})
        pack, context = build_fact_pack_with_context(entity, "ja")
        self.assertIn("Profile: Official songwriter snapshot", pack.text)
        self.assertEqual(context["body_field"], "introduction_ja")
        self.assertEqual(context["effective_language"], "ja")
        self.assertEqual(context["content_status"], "available")

    def test_legacy_unsuffixed_introduction_has_unknown_language(self):
        pack, context = build_fact_pack_with_context(
            self._entity({"introduction": "Legacy body"}), "en",
        )
        self.assertIn("Profile: Legacy body", pack.text)
        self.assertEqual(context["effective_language"], "")
        self.assertIn("Body Language: unknown", pack.text)

    def test_requested_language_wins_over_alternate_prefix_preference(self):
        for facts, wanted in (
            ({"introduction_ja": "Japanese introduction", "profile_en": "English profile"},
             "English profile"),
            ({"profileSentence_ja": "Japanese sentence", "introduction_en": "English introduction"},
             "English introduction"),
            ({"profileSentence_ja": "Japanese sentence", "profile_en": "English legacy profile"},
             "English legacy profile"),
        ):
            with self.subTest(facts=facts):
                entity = self._entity(facts)
                pack, context = build_fact_pack_with_context(entity, "en")
                self.assertIn(f"Profile: {wanted}", pack.text)
                self.assertNotIn("Japanese", pack.text)
                self.assertEqual(context["effective_language"], "en")
                self.assertEqual(_effective_body_language(facts, entity.type, "en"), "en")

    def test_requested_language_wins_for_unit_body_too(self):
        entity = self._entity(
            {"profileSentence_ja": "Japanese unit", "profile_en": "English unit"}, kind="unit",
        )
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertIn("Profile: English unit", pack.text)
        self.assertNotIn("Japanese unit", pack.text)
        self.assertEqual(context["effective_language"], "en")

    def test_blank_or_malformed_bodies_are_not_reported_available(self):
        for value in (" ", "\n\t", [], {}, 123, False):
            with self.subTest(value=value):
                entity = self._entity({"introduction_en": value})
                pack, context = build_fact_pack_with_context(entity, "en")
                self.assertNotIn("Profile:", pack.text)
                self.assertIsNone(context["effective_language"])
                self.assertEqual(context["content_status"], "missing")

    def test_blank_preferred_field_does_not_hide_usable_body(self):
        entity = self._entity({"introduction_en": " ", "profileSentence_en": "Actual body"})
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertIn("Profile: Actual body", pack.text)
        self.assertEqual(context["effective_language"], "en")

    def test_descriptive_profile_fields_are_rendered(self):
        entity = self._entity({
            "characterId": 17,
            "characterVoice_en": "Voice actor",
            "birthday_en": "February 10",
            "height": "154cm",
            "school_en": "School",
            "schoolYear_en": "Year 2",
            "hobby_en": "Making music",
            "specialSkill_en": "Playing instruments",
            "favoriteFood_en": "Instant noodles",
            "hatedFood_en": "Cilantro",
            "weak_en": "Direct sunlight",
            "introduction_en": "Songwriter",
        })
        text = build_fact_pack(entity, "en").text
        for expected in (
            "Character ID: 17", "Voice: Voice actor", "Birthday: February 10",
            "Height: 154cm", "School: School", "School Year: Year 2",
            "Hobby: Making music", "Special Skill: Playing instruments",
            "Favorite Food: Instant noodles", "Disliked Food: Cilantro",
            "Weakness: Direct sunlight", "Profile: Songwriter",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_language_aliases_select_matching_suffixes_and_names(self):
        for requested, suffix, effective in (
            ("zh_tw", "zh_hant", "zh_hant"),
            ("zh_hant", "zh_tw", "zh_hant"),
            ("zh_cn", "zh_hans", "zh_hans"),
            ("zh_hans", "zh_cn", "zh_hans"),
        ):
            with self.subTest(requested=requested, suffix=suffix):
                entity = self._entity({f"introduction_{suffix}": "Chinese body",
                                       "introduction_ja": "Japanese body"})
                pack, context = build_fact_pack_with_context(entity, requested)
                self.assertIn("Profile: Chinese body", pack.text)
                self.assertNotIn("Japanese body", pack.text)
                self.assertEqual(context["effective_language"], effective)
                self.assertEqual(pack.language, requested)
                if requested in ("zh_tw", "zh_hant"):
                    self.assertIn("Kanade TC", pack.text)

    def test_region_projection_preserves_real_body_and_provenance(self):
        entity = self._region_entity({
            "jp": {"introduction_ja": "JP songwriter", "height": "154cm", "school_ja": "JP school"},
            "en": {"introduction_en": "EN songwriter", "height": "155cm", "school_en": "EN school"},
        })
        self.assertNotIn("introduction_en", entity.facts)
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertIn("Profile: EN songwriter", pack.text)
        self.assertIn("School: EN school", pack.text)
        self.assertIn("Height: 155cm", pack.text)
        self.assertNotIn("JP school", pack.text)
        self.assertNotIn("154cm", pack.text)
        self.assertEqual(context["region"], "en")
        self.assertEqual(context["region_scope"], "region")
        self.assertEqual(context["source"], "master_db:en")
        self.assertEqual(context["version"], "version-en")
        self.assertEqual(context["retrieval"]["hash"], "hash-en")
        self.assertEqual(context["effective_language"], "en")
        self.assertEqual(entity.region_facts["jp"].facts["height"], "154cm")
        self.assertEqual(entity.facts, {})

    def test_unavailable_language_fallback_is_disclosed(self):
        entity = self._region_entity({"jp": {"introduction_ja": "JP body"}})
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertIn("JP body", pack.text)
        self.assertEqual(context["region"], "jp")
        self.assertEqual(context["effective_language"], "ja")
        self.assertIn("Body Language: ja (requested: en)", pack.text)

    def test_explicit_region_does_not_pick_requested_language_from_other_server(self):
        entity = self._region_entity({
            "jp": {"introduction_ja": "JP body", "height": "154cm"},
            "en": {"introduction_en": "EN body", "height": "155cm"},
        })
        pack, context = build_fact_pack_with_context(entity, "en", region="jp")
        self.assertIn("JP body", pack.text)
        self.assertIn("154cm", pack.text)
        self.assertNotIn("EN body", pack.text)
        self.assertNotIn("155cm", pack.text)
        self.assertEqual(context["region"], "jp")
        self.assertEqual(context["effective_language"], "ja")

    def test_missing_explicit_region_never_falls_back(self):
        for entity in (
            self._entity({"introduction_en": "Legacy body"}),
            self._region_entity({"jp": {"introduction_ja": "JP body"}}),
        ):
            with self.subTest(region_facts=bool(entity.region_facts)):
                pack, context = build_fact_pack_with_context(entity, "en", region="en")
                self.assertNotIn("Profile:", pack.text)
                self.assertEqual(context["coverage"], "missing")
                self.assertEqual(context["content_status"], "missing")
                self.assertIsNone(context["source"])

    def test_conflicting_same_language_bodies_require_region(self):
        entity = self._region_entity({
            "jp": {"introduction_en": "JP-version English body", "profile_en": "Common legacy body"},
            "en": {"introduction_en": "EN-version English body", "profile_en": "Common legacy body"},
        })
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertNotIn("Profile:", pack.text)
        self.assertEqual(context["coverage"], "needs_region")
        self.assertTrue(context["needs_region"])
        self.assertEqual(context["content_status"], "needs_region")
        self.assertIsNone(context["effective_language"])
        scoped, scoped_context = build_fact_pack_with_context(entity, "en", region="en")
        self.assertIn("EN-version English body", scoped.text)
        self.assertFalse(scoped_context["needs_region"])

    def test_no_region_auto_selection_for_scalar_only_entity(self):
        entity = self._region_entity({
            "jp": {"height": "154cm"}, "en": {"height": "155cm"},
        }, kind="character")
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertNotIn("Height:", pack.text)
        self.assertEqual(context["region_scope"], "common")
        self.assertIsNone(context["region"])

    def test_equal_bodies_prefer_native_language_region_for_scalars(self):
        entity = self._region_entity({
            "cn": {"introduction_en": "Same body", "height": "154cm"},
            "en": {"introduction_en": "Same body", "height": "155cm"},
        })
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertEqual(context["region"], "en")
        self.assertIn("Height: 155cm", pack.text)
        self.assertNotIn("154cm", pack.text)

    def test_equal_bodies_with_conflicting_non_native_snapshots_need_region(self):
        entity = self._region_entity({
            "jp": {"introduction_en": "Same body", "height": "154cm"},
            "cn": {"introduction_en": "Same body", "height": "155cm"},
        })
        pack, context = build_fact_pack_with_context(entity, "en")
        self.assertTrue(context["needs_region"])
        self.assertEqual(context["coverage"], "needs_region")
        self.assertNotIn("Profile:", pack.text)
        self.assertNotIn("Height:", pack.text)


class ProfileFactPackTimeIsolationTest(ProfileFixtureMixin, unittest.TestCase):
    AS_OF = 1_700_000_000_000

    def test_undated_character_profile_keeps_new_body_and_metadata_withheld(self):
        entity = self._region_entity({"jp": {
            "introduction_ja": "Secret profile body", "specialSkill_ja": "Secret skill",
            "school_ja": "Secret school", "releaseAt": self.AS_OF - 1000,
        }})
        payload = build_fact_pack_at(entity, "ja", region="jp", as_of=self.AS_OF)
        serialized = json.dumps(payload)
        self.assertEqual(payload["state"], "undated")
        self.assertEqual(payload["content_status"], "withheld")
        self.assertNotIn("Secret", serialized)
        self.assertNotIn("Kanade", serialized)
        self.assertIsNone(payload["effective_language"])

    def test_time_scoped_event_does_not_use_other_server_body(self):
        entity = self._region_entity({
            "jp": {"startAt": self.AS_OF - 1000, "outline_ja": "JP outline"},
            "en": {"startAt": self.AS_OF - 1000, "outline_en": "EN outline"},
        }, kind="event")
        payload = build_fact_pack_at(entity, "en", region="jp", as_of=self.AS_OF)
        self.assertEqual(payload["state"], "past")
        self.assertIn("JP outline", payload["past"]["text"])
        self.assertNotIn("EN outline", payload["past"]["text"])
        self.assertEqual(payload["effective_language"], "ja")
        self.assertIn("Version: version-jp", payload["past"]["text"])
        self.assertIn("Regions: jp", payload["past"]["text"])

    def test_missing_time_scoped_region_cannot_borrow_common_public_time(self):
        entity = self._region_entity({"jp": {
            "startAt": self.AS_OF - 1000, "outline_ja": "JP secret",
        }}, kind="event")
        payload = build_fact_pack_at(entity, "ja", region="en", as_of=self.AS_OF)
        self.assertEqual(payload["state"], "undated")
        self.assertEqual(payload["content_status"], "withheld")
        self.assertNotIn("JP secret", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
