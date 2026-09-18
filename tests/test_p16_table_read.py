"""Bounded P16 contracts; synthetic files only, with network denied by default."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sekaisync import event_detection as events, layout
from sekaisync.registry import RawSnapshot


class TableReadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.base = self.root / "pinned" / "source"
        self.base.mkdir(parents=True)
        self.snapshot = RawSnapshot({"jp": self.base}, {"jp": "generation-A"})

    def read(self):
        reader = getattr(layout, "read_master_table", None)
        self.assertTrue(callable(reader), "P16 needs a stateful raw table reader")
        return reader(self.snapshot, "jp", "events")

    def test_four_states_and_provenance(self):
        result = self.read()
        self.assertEqual(result.status, "missing")
        self.assertEqual(result.records, [])
        self.assertEqual(result.version, "generation-A")
        path = self.base / "events.json"
        for text, state, rows in (("{", "invalid", []), ("[]", "valid_empty", []),
                                  ('[{"id": 1}]', "valid", [{"id": 1}])):
            with self.subTest(state=state):
                path.write_text(text, encoding="utf-8")
                result = self.read()
                self.assertEqual(result.status, state)
                self.assertEqual(result.records, rows)
                self.assertEqual(result.source, str(path))
                self.assertEqual(result.version, "generation-A")
                self.assertEqual(bool(result.error), state == "invalid")

    def test_wrappers_and_nested_layouts(self):
        for folder in ("master", "db", "repo-main", "versions/v1"):
            directory = self.base / folder
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / "events.json"
            for wrapper in ("records", "items", "data"):
                for rows in ([], [{"id": 1}]):
                    with self.subTest(folder=folder, wrapper=wrapper, rows=rows):
                        path.write_text(json.dumps({wrapper: rows}), encoding="utf-8")
                        result = self.read()
                        self.assertEqual(result.status, "valid" if rows else "valid_empty")
                        self.assertEqual(result.records, rows)
            path.unlink()

    def test_invalid_payloads_never_become_partial_or_empty_success(self):
        path = self.base / "events.json"
        for payload in ({}, {"error": "unavailable"}, {"records": {}}, None, 3, "text",
                        [1], [{"id": 1}, None], {"data": [{"id": 1}, "bad"]}):
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                result = self.read()
                self.assertEqual(result.status, "invalid")
                self.assertEqual(result.records, [])
                self.assertTrue(result.error)
        path.write_bytes(b"\xff")
        self.assertEqual(self.read().status, "invalid")

    def test_io_failure_and_duplicate_candidates_are_invalid(self):
        path = self.base / "events.json"
        path.write_text("[]", encoding="utf-8")
        with mock.patch.object(Path, "read_text", side_effect=PermissionError("denied")):
            self.assertEqual(self.read().status, "invalid")
        nested = self.base / "master"
        nested.mkdir()
        (nested / "events.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.read().status, "invalid")

    def test_explicit_snapshot_never_resolves_active_or_legacy_fallback(self):
        legacy = layout.region_master_dir(self.root, "jp")
        legacy.mkdir(parents=True)
        (legacy / "events.json").write_text('[{"id": 99}]', encoding="utf-8")
        with mock.patch("sekaisync.registry._read_active_generations",
                        side_effect=AssertionError("must not resolve active pointers")):
            self.assertEqual(self.read().status, "missing")
            reader = layout.read_master_table
            self.assertEqual(reader(self.snapshot, "en", "events").status, "missing")
        self.assertFalse(layout.db_path(self.root).exists())


class EventTableGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "store"
        self.base = layout.region_master_dir(self.root, "jp") / "repo-main"
        self.base.mkdir(parents=True)
        self.local = [{"id": 1, "eventType": "marathon"}]
        self.remote = {table: [] for table in events.EVENT_BASE_TABLES}
        self.remote["events"] = list(self.local)
        for table, rows in self.remote.items():
            self.write(table, rows)
        endpoint = mock.patch("sekaisync.event_detection.current_endpoints")
        endpoint.start().ALTSOURCE_SV_MASTER_BASE = "https://example.test"
        self.addCleanup(endpoint.stop)
        network = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden"))
        network.start()
        self.addCleanup(network.stop)

    def write(self, table, payload):
        (self.base / f"{table}.json").write_text(json.dumps(payload), encoding="utf-8")

    def raw_bytes(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in (self.root / "raw").rglob("*.json")}

    def fetch(self, url, timeout=30):
        table = url.rsplit("/", 1)[-1].removesuffix(".json")
        return json.dumps(self.remote[table])

    def check(self, **kwargs):
        return events.check_events(self.root, regions=["jp"], fetcher=self.fetch, **kwargs)

    def test_incomplete_local_tables_block_current_and_new_event_paths(self):
        for table in events.EVENT_BASE_TABLES:
            for state in ("missing", "invalid"):
                for new in (False, True):
                    with self.subTest(table=table, state=state, new=new):
                        self.remote["events"] = self.local + ([{"id": 2}] if new else [])
                        for name in events.EVENT_BASE_TABLES:
                            self.write(name, self.local if name == "events" else [])
                        path = self.base / f"{table}.json"
                        if state == "missing":
                            path.unlink()
                        else:
                            path.write_text('{"error":"bad table"}', encoding="utf-8")
                        before = self.raw_bytes()
                        with mock.patch.object(events, "merge_new_event_tables") as merge:
                            result = self.check()
                        region = result["regions"]["jp"]
                        expected = "no_local_baseline" if table == "events" and state == "missing" else "incomplete_local"
                        self.assertEqual(region["status"], expected)
                        self.assertEqual(region["table_states"][table], state)
                        self.assertEqual(result["detected_total"], 0)
                        merge.assert_not_called()
                        self.assertEqual(self.raw_bytes(), before)
                        self.write(table, self.local if table == "events" else [])

    def test_invalid_remote_tables_do_not_write_or_archive_events(self):
        self.remote["events"] = self.local + [{"id": 2}]
        for table in events.EVENT_BASE_TABLES:
            for bad in ({"error": "unavailable"}, [None], None):
                with self.subTest(table=table, bad=bad):
                    good = self.remote[table]
                    self.remote[table] = bad
                    before = self.raw_bytes()
                    result = self.check()
                    self.assertEqual(result["regions"]["jp"]["status"], "fetch_failed")
                    self.assertEqual(result["detected_total"], 0)
                    self.assertEqual(self.raw_bytes(), before)
                    self.assertEqual(events.list_events(self.root, ["jp"])["total"], 0)
                    self.remote[table] = good

    def test_valid_empty_relations_allow_up_to_date(self):
        result = self.check()
        self.assertEqual(result["regions"]["jp"]["status"], "up_to_date")
        self.assertEqual(result["regions"]["jp"]["table_states"]["eventMusics"], "valid_empty")

    def test_valid_empty_events_are_a_baseline_not_missing(self):
        self.write("events", [])
        self.remote["events"] = []
        result = self.check()
        self.assertEqual(result["regions"]["jp"]["status"], "up_to_date")

    def test_merge_validates_every_incoming_table_before_first_write(self):
        for table in events.EVENT_BASE_TABLES:
            for missing in (True, False):
                with self.subTest(table=table, missing=missing):
                    tables = dict(self.remote, events=self.local + [{"id": 2}])
                    if missing:
                        del tables[table]
                    else:
                        tables[table] = [None]
                    before = self.raw_bytes()
                    with self.assertRaises(ValueError):
                        events.merge_new_event_tables(self.root, "jp", tables, {"2"})
                    self.assertEqual(self.raw_bytes(), before)

    def test_merge_validates_existing_late_table_before_first_write(self):
        (self.base / "musics.json").write_text("{", encoding="utf-8")
        before = self.raw_bytes()
        with self.assertRaises(ValueError):
            events.merge_new_event_tables(self.root, "jp", self.remote, {"2"})
        self.assertEqual(self.raw_bytes(), before)

    def test_missing_relations_are_not_placeholder_evidence(self):
        (self.base / "eventMusics.json").unlink()
        self.assertEqual(events._jp_placeholder_event_ids(self.root), set())

    def test_published_snapshot_is_not_silently_written_to_legacy(self):
        snapshot = RawSnapshot({"jp": self.base}, {"jp": "generation-A"})
        self.remote["events"] = self.local + [{"id": 2}]
        with mock.patch("sekaisync.registry.capture_raw_snapshot", return_value=snapshot):
            before = self.raw_bytes()
            result = self.check()
            self.assertEqual(result["regions"]["jp"]["status"], "sync_required")
            self.assertEqual(self.raw_bytes(), before)


if __name__ == "__main__":
    unittest.main()
