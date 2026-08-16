import json
import tempfile
import unittest
from pathlib import Path

from sekaisync.registry import build_registry, lookup_entity, save_registry


class RegistryTest(unittest.TestCase):
    def _write_region(self, store_root: Path):
        source = store_root / "raw" / "jp" / "source"
        source.mkdir(parents=True, exist_ok=True)
        (source / "events.json").write_text(
            json.dumps(
                [
                    {
                        "id": 158,
                        "name": "昔日のRead-aloud",
                        "startAt": 1740031200000,
                        "eventType": "marathon",
                    },
                    {
                        "id": 174,
                        "name": "start rolling! stars' crossing",
                        "startAt": 1753250400000,
                        "eventType": "marathon",
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (source / "eventStories.json").write_text(
            json.dumps(
                [
                    {
                        "id": 174,
                        "eventId": 174,
                        "outline": "ついに幕を開けたLUMINAグランプリ。",
                        "eventStoryEpisodes": [
                            {
                                "id": 1001415,
                                "eventStoryId": 174,
                                "episodeNo": 1,
                                "title": "LUMINA FORUM",
                            }
                        ],
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_event_lookup_uses_outline_and_episode_title(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_region(store_root)
            entities = build_registry(store_root, ["jp"])

            event = lookup_entity(entities, "LUMINA", type="event")
            self.assertEqual(event[0][0].id, "event:174")
            self.assertIn("outline_ja", event[0][0].facts)
            self.assertEqual(event[0][0].trust, "A")

            story = lookup_entity(entities, "LUMINA FORUM", type="event_story")
            self.assertEqual(story[0][0].id, "event_story:174")
            self.assertEqual(story[0][0].names.get("episode1_ja"), "LUMINA FORUM")

    def test_numeric_query_prefers_entity_id_over_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            store_root = Path(tmp) / "store"
            self._write_region(store_root)
            entities = build_registry(store_root, ["jp"])
            registry_path = store_root / "kb" / "registry.json"
            save_registry(entities, registry_path)

            matches = lookup_entity(entities, "174", type="event", limit=5)
            self.assertEqual(matches[0][0].id, "event:174")
            self.assertNotIn("event:158", [entity.id for entity, _ in matches])


if __name__ == "__main__":
    unittest.main()
