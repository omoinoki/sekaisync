import json
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from sekaisync.models import Entity
from sekaisync.registry import build_registry, load_registry, save_registry


class RegionFactsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write_table(self, region, table, records):
        path = self.root / 'raw' / region / 'source' / (table + '.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records), encoding='utf-8')
        return path

    def write_events(self):
        self.write_table('jp', 'events', [
            {'id': 1, 'name': 'Japanese event', 'startAt': 100,
             'endAt': 150, 'eventType': 'marathon'},
        ])
        self.write_table('en', 'events', [
            {'id': 1, 'name': 'English event', 'startAt': 200,
             'eventType': 'marathon'},
        ])

    def test_input_order_does_not_choose_a_region_date(self):
        self.write_events()
        forward = build_registry(self.root, ['jp', 'en'])
        backward = build_registry(self.root, ['en', 'jp'])
        self.assertEqual([asdict(e) for e in forward], [asdict(e) for e in backward])
        event = forward[0]
        self.assertEqual(event.id, 'event:1')
        self.assertEqual(event.facts, {'eventType': 'marathon'})
        self.assertEqual(event.region_facts['jp'].facts['startAt'], 100)
        self.assertEqual(event.region_facts['en'].facts['startAt'], 200)

    def test_region_selection_and_missing_field_are_explicit(self):
        from sekaisync.regions import entity_for_region
        self.write_events()
        event = build_registry(self.root, ['jp', 'en'])[0]
        common = entity_for_region(event, None)
        self.assertTrue(common['needs_region'])
        self.assertNotIn('startAt', common['facts'])
        self.assertEqual(common['field_status']['startAt'], 'needs_region')
        self.assertEqual(common['field_status']['endAt'], 'partial')
        jp = entity_for_region(event, 'jp')
        self.assertEqual(jp['facts']['startAt'], 100)
        self.assertEqual(jp['source'], 'master_db:jp')
        self.assertEqual(jp['coverage'], 'available')
        en = entity_for_region(event, 'en', fields=['startAt', 'endAt', 'notStored'])
        self.assertEqual(en['field_status']['endAt'], 'missing')
        self.assertEqual(en['field_status']['notStored'], 'missing')
        missing = entity_for_region(event, 'cn')
        self.assertEqual(missing['coverage'], 'missing')
        self.assertEqual(missing['facts'], {})
        self.assertIsNone(missing['source'])

    def test_legacy_unknown_does_not_invent_region_facts(self):
        from sekaisync.regions import entity_for_region
        # Original positional constructor remains valid for dbstore and callers.
        legacy = Entity('event:1', 'event', 'jp', ['jp', 'en'],
                        {'en': 'Legacy'}, {'startAt': 100}, 'master_db:jp', 'v1', False, 'A')
        self.assertEqual(legacy.region_facts, {})
        view = entity_for_region(legacy, 'jp')
        self.assertEqual(view['coverage'], 'unknown')
        self.assertEqual(view['facts'], {})
        self.assertEqual(view['legacy_unscoped']['facts'], {'startAt': 100})
        self.assertEqual(view['legacy_unscoped']['version'], 'v1')
        path = self.root / 'registry.json'
        save_registry([legacy], path)
        restored = load_registry(path)[0]
        self.assertEqual(entity_for_region(restored, 'en')['coverage'], 'unknown')
        self.assertEqual(restored.region_facts, {})

    def test_json_roundtrip_retains_per_region_provenance(self):
        self.write_events()
        entities = build_registry(self.root, ['jp', 'en'])
        path = self.root / 'registry.json'
        save_registry(entities, path)
        self.assertEqual([asdict(e) for e in load_registry(path)], [asdict(e) for e in entities])
        facts = entities[0].region_facts['jp']
        self.assertEqual(facts.source, 'master_db:jp')
        self.assertIn('sha256', facts.retrieval)
        self.assertEqual(facts.retrieval['table'], 'events')

    def test_pinned_snapshot_keeps_story_enrichment_in_same_generation(self):
        from unittest.mock import patch
        from sekaisync.registry import RawSnapshot
        self.write_table('jp', 'eventStories', [
            {'id': 1, 'eventId': 1, 'outline': 'WRONG active outline'}])
        pinned = self.root / 'pinned'
        pinned.mkdir()
        (pinned / 'events.json').write_text(json.dumps([
            {'id': 1, 'name': 'Pinned event', 'startAt': 100}]), encoding='utf-8')
        (pinned / 'eventStories.json').write_text(json.dumps([
            {'id': 1, 'eventId': 1, 'outline': 'Pinned outline'}]), encoding='utf-8')
        snapshot = RawSnapshot({'jp': pinned}, {'jp': 'generation-A'})
        with patch('sekaisync.registry._read_active_generations',
                   side_effect=AssertionError('must not resolve active pointer')):
            entities = build_registry(self.root, iter(['jp']), raw_snapshot=snapshot)
        event = next(e for e in entities if e.id == 'event:1')
        self.assertEqual(event.facts['outline_ja'], 'Pinned outline')
        self.assertEqual(event.region_facts['jp'].version, 'generation-A')
        self.assertEqual(event.region_facts['jp'].retrieval['field_sources']
                         ['outline_ja']['table'], 'eventStories')
        missing = RawSnapshot({'jp': self.root / 'missing'}, {'jp': 'missing'})
        self.assertEqual(build_registry(self.root, ['jp'], raw_snapshot=missing), [])

    def test_region_lookup_and_glossary_do_not_infer_coverage_from_language(self):
        from sekaisync.glossary import merge_glossary, resolve_name
        from sekaisync.registry import lookup_entity
        self.write_events()
        self.write_table('jp', 'eventStories', [
            {'id': 1, 'eventId': 1, 'outline': 'UniqueRegionalNeedle'}])
        entities = build_registry(self.root, iter(['jp', 'en', 'jp']))
        self.assertTrue(lookup_entity(entities, 'UniqueRegionalNeedle',
                                      type='event', region='jp'))
        self.assertFalse(lookup_entity(entities, 'UniqueRegionalNeedle',
                                       type='event', region='en'))
        terms = merge_glossary(entities)
        event = next(t for t in terms if t.id == 'event:1')
        self.assertEqual(event.source, 'master_db')
        self.assertTrue(event.official)
        self.assertEqual(event.names['en'], 'English event')
        self.assertIsNone(resolve_name([event], 'English event', 'zh_hans')[0]['target_name'])

    def test_common_projection_requires_full_coverage_and_json_type_equality(self):
        from sekaisync.models import RegionFacts
        from sekaisync.regions import project_common_facts
        regions = {
            'jp': RegionFacts('jp', {'flag': True, 'value': 1}, 'jp', None, {}),
            'en': RegionFacts('en', {'flag': 1, 'value': 1}, 'en', None, {}),
        }
        common, status = project_common_facts(regions)
        self.assertEqual(common, {'value': 1})
        self.assertEqual(status['flag'], 'needs_region')
        self.assertEqual(project_common_facts(regions, ['jp', 'en', 'cn'])[0], {})

    def test_db_serializer_requires_migration_and_preserves_region_slots(self):
        from sekaisync import dbstore
        from sekaisync.regions import entity_for_region
        self.write_events()
        event = build_registry(self.root, ['jp', 'en'])[0]
        dbstore.initialize(self.root)
        with self.assertRaisesRegex(ValueError, 'migrat'):
            dbstore.save_entities(self.root, [event])
        dbstore.migrate_store(self.root, target_version=2, dry_run=False,
                             backup_path=self.root / 'v1.sqlite')
        dbstore.migrate_store(self.root, target_version=3, dry_run=False,
                             backup_path=self.root / 'v2.sqlite')
        dbstore.save_entities(self.root, [event])
        loaded = dbstore.load_entities(self.root)[0]
        self.assertEqual(asdict(loaded), asdict(event))
        self.assertEqual(entity_for_region(loaded, 'jp')['facts']['startAt'], 100)
        self.assertEqual(entity_for_region(loaded, 'en')['facts']['startAt'], 200)
        self.assertNotIn('startAt', loaded.facts)

    def test_old_json_preserves_unscoped_evidence_exactly(self):
        from sekaisync.regions import entity_for_region
        path = self.root / 'old.json'
        original = {'startAt': 100, 'null': None, 'empty': '', 'zero': 0}
        path.write_text(json.dumps([{'id': 'event:1', 'facts': original,
                                    'region': 'jp', 'regions': ['jp', 'en']}]), encoding='utf-8')
        entity = load_registry(path)[0]
        self.assertEqual(entity.facts, original)
        self.assertEqual(entity_for_region(entity)['legacy_unscoped']['facts'], original)
        self.assertEqual(entity_for_region(entity)['facts'], {})


if __name__ == '__main__':
    unittest.main()
