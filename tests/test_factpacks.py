import unittest

from sekaisync.factpacks import build_fact_pack
from sekaisync.models import Entity


class FactPackTest(unittest.TestCase):
    def test_compact_pack_is_smaller(self):
        entity = Entity(
            id="character:1",
            type="character",
            region="demo",
            regions=["demo"],
            names={"en": "Hoshino Ichika", "zh_tw": "星乃一歌"},
            facts={"unit": "Leo/need", "birthday": "3月7日", "height": "163cm"},
            source="master_db:demo",
            demo=True,
        )
        pack = build_fact_pack(entity, language="zh_tw")
        self.assertIn("星乃一歌", pack.text)
        self.assertLess(pack.fact_pack_tokens, pack.raw_json_tokens)


if __name__ == "__main__":
    unittest.main()
