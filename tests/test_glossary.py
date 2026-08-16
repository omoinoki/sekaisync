import unittest

from sekaisync.glossary import GlossaryTerm, merge_glossary, resolve_name
from sekaisync.models import Entity


class GlossaryTest(unittest.TestCase):
    def test_merge_and_resolve(self):
        entity = Entity(
            id="character:1",
            type="character",
            region="demo",
            regions=["demo"],
            names={"en": "Hoshino Ichika", "zh_tw": "星乃一歌"},
            source="master_db:demo",
            demo=True,
        )
        terms = merge_glossary([entity])
        self.assertEqual(len(terms), 1)
        results = resolve_name(terms, "Hoshino Ichika", target_language="zh_tw")
        self.assertEqual(results[0]["target_name"], "星乃一歌")

    def test_seed_official_flag(self):
        seed = [
            {
                "id": "game_title",
                "kind": "game",
                "canonical": "Hatsune Miku: Colorful Stage!",
                "names": {"en": "Hatsune Miku: Colorful Stage!"},
                "official": True,
                "source": "official_game_title",
            }
        ]
        terms = merge_glossary([], seed)
        self.assertTrue(terms[0].official)


if __name__ == "__main__":
    unittest.main()
