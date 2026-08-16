import unittest

from sekaisync.sources import (
    ALL_SOURCES,
    BACKEND_MOESEKAI,
    BACKEND_SEKAI_VIEWER,
    DEFAULT_SOURCE_PRIORITY,
    SOURCE_MS,
    SOURCE_MS_TRANSLATION,
    SOURCE_SV,
    SOURCE_SV_I18N,
    auxiliary_source_for,
    auxiliary_source_for_instance,
    backend_of,
    backend_of_type,
    is_story_source,
    normalize_source_id,
    normalize_sources,
    source_rank,
    type_source_id,
)


class SourcesTest(unittest.TestCase):
    def test_canonical_ids(self):
        self.assertEqual(SOURCE_SV, "altsource_sv")
        self.assertEqual(SOURCE_MS, "altsource_ms")
        self.assertEqual(ALL_SOURCES, ("altsource_sv", "altsource_ms"))

    def test_legacy_ids_normalize_to_canonical(self):
        self.assertEqual(normalize_source_id("sekai_viewer"), SOURCE_SV)
        self.assertEqual(normalize_source_id("sekai_viewer_i18n"), SOURCE_SV_I18N)
        self.assertEqual(normalize_source_id("altsource"), SOURCE_MS)
        self.assertEqual(normalize_source_id("altsource_translation"), SOURCE_MS_TRANSLATION)
        self.assertEqual(normalize_source_id("moesekai"), SOURCE_MS)

    def test_normalize_is_case_insensitive_and_preserves_unknown(self):
        self.assertEqual(normalize_source_id("SEKAI_VIEWER"), SOURCE_SV)
        self.assertEqual(normalize_source_id("Altsource"), SOURCE_MS)
        self.assertEqual(normalize_source_id("unknown_site"), "unknown_site")

    def test_is_story_source(self):
        self.assertTrue(is_story_source("sekai_viewer"))
        self.assertTrue(is_story_source(SOURCE_MS))
        self.assertFalse(is_story_source(SOURCE_SV_I18N))

    def test_auxiliary_source_for(self):
        self.assertEqual(auxiliary_source_for(SOURCE_SV), SOURCE_SV_I18N)
        self.assertEqual(auxiliary_source_for("sekai_viewer"), SOURCE_SV_I18N)
        self.assertEqual(auxiliary_source_for(SOURCE_MS), SOURCE_MS_TRANSLATION)
        self.assertEqual(auxiliary_source_for("altsource"), SOURCE_MS_TRANSLATION)
        self.assertIsNone(auxiliary_source_for(SOURCE_MS_TRANSLATION))
        self.assertIsNone(auxiliary_source_for("fandom"))

    def test_backend_of(self):
        self.assertEqual(backend_of(SOURCE_SV), BACKEND_SEKAI_VIEWER)
        self.assertEqual(backend_of("sekai_viewer_i18n"), BACKEND_SEKAI_VIEWER)
        self.assertEqual(backend_of(SOURCE_MS), BACKEND_MOESEKAI)
        self.assertEqual(backend_of("altsource"), BACKEND_MOESEKAI)
        self.assertEqual(backend_of("nope"), "")

    def test_normalize_sources_deduplicates_and_keeps_order(self):
        self.assertEqual(
            normalize_sources(["sekai_viewer", "altsource", "altsource_sv"]),
            (SOURCE_SV, SOURCE_MS),
        )

    def test_source_rank_uses_priority_order(self):
        self.assertEqual(source_rank(SOURCE_SV), 0)
        self.assertEqual(source_rank(SOURCE_MS), 1)
        self.assertEqual(source_rank(SOURCE_MS, [SOURCE_MS, SOURCE_SV]), 0)
        self.assertEqual(source_rank("sekai_viewer", [SOURCE_SV, SOURCE_MS]), 0)
        self.assertEqual(source_rank("altsource", [SOURCE_SV, SOURCE_MS]), 1)
        self.assertEqual(source_rank("unknown", [SOURCE_SV, SOURCE_MS]), 2)

    def test_default_priority_prefers_sv(self):
        self.assertEqual(DEFAULT_SOURCE_PRIORITY, (SOURCE_SV, SOURCE_MS))

    def test_type_backend_mapping(self):
        self.assertEqual(type_source_id(BACKEND_SEKAI_VIEWER), SOURCE_SV)
        self.assertEqual(type_source_id(BACKEND_MOESEKAI), SOURCE_MS)
        self.assertEqual(type_source_id("nope"), "")
        self.assertEqual(backend_of_type(SOURCE_SV), BACKEND_SEKAI_VIEWER)
        self.assertEqual(backend_of_type(SOURCE_MS), BACKEND_MOESEKAI)
        self.assertEqual(backend_of_type("unknown"), "")

    def test_auxiliary_source_for_instance(self):
        # Default instances keep the legacy auxiliary naming.
        self.assertEqual(
            auxiliary_source_for_instance(SOURCE_SV, BACKEND_SEKAI_VIEWER),
            SOURCE_SV_I18N,
        )
        self.assertEqual(
            auxiliary_source_for_instance(SOURCE_MS, BACKEND_MOESEKAI),
            SOURCE_MS_TRANSLATION,
        )
        # Custom instances get their own auxiliary namespaces.
        self.assertEqual(
            auxiliary_source_for_instance("altsource_sv_local", BACKEND_SEKAI_VIEWER),
            "altsource_sv_local_i18n",
        )
        self.assertEqual(
            auxiliary_source_for_instance("altsource_ms_alt", BACKEND_MOESEKAI),
            "altsource_ms_alt_translation",
        )
        self.assertEqual(auxiliary_source_for_instance("x", "unknown"), "")


if __name__ == "__main__":
    unittest.main()
