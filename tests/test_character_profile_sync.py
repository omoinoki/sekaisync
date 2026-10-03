"""Issue #1: character profiles survive sync and remain publicly reachable."""

import contextlib
import io
import json
import tempfile
import threading
import unittest
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlencode

from sekaisync import dbstore
from sekaisync.cli import main
from sekaisync.config import SekaiSyncConfig
from sekaisync.core import SekaiSyncCore
from sekaisync.factpacks import load_fact_packs
from sekaisync.fetcher import sync
from sekaisync.http_server import BoundedThreadingHTTPServer, SekaiSyncHandler
from sekaisync.layout import factpack_path
from sekaisync.mcp_server import McpServer


JP_NAME = "\u671d\u6bd4\u5948\u307e\u3075\u3086"
EN_NAME = "Asahina Mafuyu"
JP_ROLE = "\u4f5c\u8a5e\u62c5\u5f53"
JP_BODY = JP_ROLE + "\u3002\u304a\u4eba\u597d\u3057\u306e\u512a\u7b49\u751f\u3002"
EN_BODY = "Handles lyrics for the group. A kind honor student."
JP_REVISED_BODY = JP_ROLE + "\u3002\u65b0\u3057\u3044\u30d7\u30ed\u30d5\u30a3\u30fc\u30eb\u3002"
EN_REVISED_BODY = "Handles lyrics for the group. A revised character profile."
PROFILE_ID = "character_profile:9001"
REVISED_PROFILE_ID = "character_profile:9003"
MISSING_BODY_ID = "character_profile:9004"
WRONG_NAME = "Wrong Profile-ID Character"


class CharacterProfileSyncTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="test_character_profile_sync_")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = self.root / "store"
        self.mirrors = {}
        for region, name, body, revised, height in (
            ("jp", JP_NAME, JP_BODY, JP_REVISED_BODY, "162cm"),
            ("en", EN_NAME, EN_BODY, EN_REVISED_BODY, "163cm"),
        ):
            mirror = self.root / "mirrors" / region
            mirror.mkdir(parents=True)
            self.mirrors[region] = mirror
            characters = [
                {"id": 26, "name": name},
                # Profile IDs are not character foreign keys.
                {"id": 9001, "name": WRONG_NAME},
            ]
            if region == "en":
                characters.append({"id": 404, "name": "EN-only Linked Character"})
            profiles = [
                {
                    "id": 9001,
                    "characterId": 26,
                    "introduction": body,
                    "height": height,
                    "characterVoice": "Profile Voice",
                    "hobby": f"{region} aquarium hobby",
                    "specialSkill": f"{region} mental arithmetic",
                },
                {"id": 9003, "characterId": 26, "introduction": revised},
                {"id": 9004, "characterId": 26, "height": height},
            ]
            if region == "jp":
                profiles.append({
                    "id": 9002,
                    "characterId": 404,
                    "introduction": "Orphan profile with no same-region character.",
                })
            self._write_table(mirror, "gameCharacters", characters)
            self._write_table(mirror, "characterProfiles", profiles)
        self.config = SekaiSyncConfig(store_root=self.store, regions=("jp", "en"))
        self._sync()
        self.core = SekaiSyncCore(self.store)

    @staticmethod
    def _write_table(mirror, name, records):
        (mirror / f"{name}.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8",
        )

    def _sync(self):
        return sync(self.config, ["jp", "en"], local_mirrors=self.mirrors)

    def _profile(self, rows, entity_id=PROFILE_ID):
        matches = [row for row in rows if row["id"] == entity_id]
        self.assertEqual(len(matches), 1)
        return matches[0]

    def _cli(self, store, *arguments):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            status = main(["--store", str(store), "--no-event-check", *arguments])
        self.assertEqual(status, 0, stdout.getvalue())
        return stdout.getvalue()

    def _mcp(self, server, name, arguments):
        response = server.handle({
            "jsonrpc": "2.0", "id": 7, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        self.assertNotIn("error", response)
        self.assertFalse(response["result"].get("isError"), response)
        return json.loads(response["result"]["content"][0]["text"])

    def test_sync_keeps_profiles_reachable_by_character_names_and_body(self):
        for query in (JP_NAME, EN_NAME, JP_ROLE, "Handles lyrics"):
            with self.subTest(query=query):
                rows = self.core.lookup(query, type="character_profile")
                self.assertIn(PROFILE_ID, {row["id"] for row in rows})
                self.assertIn(REVISED_PROFILE_ID, {row["id"] for row in rows})
        row = self._profile(self.core.lookup(PROFILE_ID, type="character_profile"))
        self.assertEqual(row["names"]["ja"], JP_NAME)
        self.assertEqual(row["names"]["en"], EN_NAME)
        self.assertEqual(row["facts"]["characterId"], 26)
        self.assertNotIn("height", row["facts"])
        self.assertTrue(row["needs_region"])
        self.assertEqual(row["region_facts"]["jp"]["facts"]["introduction_ja"], JP_BODY)
        self.assertEqual(row["region_facts"]["en"]["facts"]["introduction_en"], EN_BODY)
        source = row["region_facts"]["jp"]["retrieval"]
        self.assertEqual(source["table"], "characterProfiles")
        self.assertEqual(source["name_sources"]["character:26"]["table"], "gameCharacters")

        result = self.core.query(JP_ROLE, type="character_profile")
        self.assertIn(PROFILE_ID, {row["id"] for row in result["metadata"]})

    def test_explicit_region_lookup_selects_body_and_scalars(self):
        for region, name, key, body, height in (
            ("jp", JP_NAME, "introduction_ja", JP_BODY, "162cm"),
            ("en", EN_NAME, "introduction_en", EN_BODY, "163cm"),
        ):
            with self.subTest(region=region):
                row = self._profile(self.core.lookup(name, type="character_profile", region=region))
                self.assertEqual(row["facts"][key], body)
                self.assertEqual(row["facts"]["height"], height)
                self.assertEqual(row["source"], f"master_db:{region}")
                self.assertEqual(row["coverage"], "available")
                self.assertEqual(row["retrieval"]["table"], "characterProfiles")
        self.assertEqual(self.core.lookup(JP_ROLE, type="character_profile", region="en"), [])
        self.assertEqual(self.core.lookup("Handles lyrics", type="character_profile", region="jp"), [])

    def test_profiles_never_join_on_their_own_id_or_another_region(self):
        self.assertEqual(self.core.lookup(WRONG_NAME, type="character_profile"), [])
        self.assertEqual(self.core.lookup("EN-only Linked Character", type="character_profile"), [])
        orphan = self._profile(
            self.core.lookup("character_profile:9002", type="character_profile"),
            entity_id="character_profile:9002",
        )
        self.assertEqual(orphan["names"], {})
        self.assertEqual(orphan["facts"]["characterId"], 404)

    def test_factpack_language_selects_one_regional_snapshot(self):
        for language, region, body, other_body, height in (
            ("ja", "jp", JP_BODY, EN_BODY, "162cm"),
            ("en", "en", EN_BODY, JP_BODY, "163cm"),
        ):
            with self.subTest(language=language):
                pack = self.core.fact_pack(PROFILE_ID, language=language)
                self.assertIn(body, pack["text"])
                self.assertNotIn(other_body, pack["text"])
                self.assertIn(f"Height: {height}", pack["text"])
                self.assertIn(f"{region} aquarium hobby", pack["text"])
                self.assertEqual(pack["body_field"], f"introduction_{language}")
                self.assertEqual(pack["effective_language"], language)
                self.assertEqual(pack["content_status"], "available")
                self.assertEqual(pack["region"], region)
                self.assertEqual(pack["region_scope"], "region")
                self.assertEqual(pack["source"], f"master_db:{region}")
                self.assertEqual(pack["retrieval"]["table"], "characterProfiles")
        saved = {pack.entity_id: pack for pack in load_fact_packs(factpack_path(self.store, "en"))}
        self.assertIn(EN_BODY, saved[PROFILE_ID].text)
        self.assertNotIn(JP_BODY, saved[PROFILE_ID].text)

    def test_factpack_explicit_region_reports_fallback_and_missing(self):
        fallback = self.core.fact_pack(PROFILE_ID, language="en", region="jp")
        self.assertIn(JP_BODY, fallback["text"])
        self.assertNotIn(EN_BODY, fallback["text"])
        self.assertEqual(fallback["effective_language"], "ja")
        self.assertEqual(fallback["region"], "jp")
        self.assertEqual(fallback["source"], "master_db:jp")

        absent_region = self.core.fact_pack(PROFILE_ID, language="en", region="cn")
        self.assertEqual(absent_region["coverage"], "missing")
        self.assertEqual(absent_region["content_status"], "missing")
        self.assertIsNone(absent_region["effective_language"])
        self.assertNotIn(JP_BODY, absent_region["text"])
        self.assertNotIn(EN_BODY, absent_region["text"])
        self.assertNotIn("Height:", absent_region["text"])

        missing_body = self.core.fact_pack(MISSING_BODY_ID, language="en", region="en")
        self.assertEqual(missing_body["coverage"], "available")
        self.assertEqual(missing_body["content_status"], "missing")
        self.assertIsNone(missing_body["effective_language"])
        self.assertNotIn("Profile:", missing_body["text"])

    def test_unchanged_raw_sync_repairs_old_sparse_database_and_core(self):
        mirror_bytes = {
            region: (mirror / "characterProfiles.json").read_bytes()
            for region, mirror in self.mirrors.items()
        }
        sparse = []
        for entity in dbstore.load_entities(self.store):
            if entity.type == "character_profile":
                regional = {
                    region: replace(rf, facts={"height": rf.facts.get("height", "162cm")})
                    for region, rf in entity.region_facts.items()
                }
                entity = replace(entity, names={}, facts={}, region_facts=regional)
            sparse.append(entity)
        dbstore.save_entities(self.store, sparse)
        old_core = SekaiSyncCore(self.store)
        self.assertEqual(old_core.lookup(JP_NAME, type="character_profile"), [])
        self.assertEqual(old_core.fact_pack(PROFILE_ID, language="en")["content_status"], "missing")

        self._sync()
        repaired = self._profile(old_core.lookup(JP_NAME, type="character_profile", region="jp"))
        self.assertEqual(repaired["facts"]["introduction_ja"], JP_BODY)
        self.assertIn(EN_BODY, old_core.fact_pack(PROFILE_ID, language="en")["text"])
        stored = {entity.id: entity for entity in dbstore.load_entities(self.store)}
        self.assertEqual(stored[PROFILE_ID].names["en"], EN_NAME)
        self.assertEqual(stored[PROFILE_ID].region_facts["jp"].facts["introduction_ja"], JP_BODY)
        for region, mirror in self.mirrors.items():
            self.assertEqual((mirror / "characterProfiles.json").read_bytes(), mirror_bytes[region])

    def test_cli_init_sync_and_issue_queries(self):
        store = self.root / "cli-store"
        self.assertIn("initialized", self._cli(store, "init"))
        self._cli(
            store, "sync", "--regions", "jp,en",
            "--local", f"jp={self.mirrors['jp']}", "--local", f"en={self.mirrors['en']}",
        )
        by_name = json.loads(self._cli(store, "lookup", "--query", JP_NAME, "--type", "character_profile"))
        self.assertEqual(self._profile(by_name)["names"]["ja"], JP_NAME)
        by_body = json.loads(self._cli(store, "lookup", "--query", JP_ROLE, "--type", "character_profile"))
        self.assertIn(PROFILE_ID, {row["id"] for row in by_body})
        pack = json.loads(self._cli(store, "factpack", "--id", PROFILE_ID, "--language", "ja"))
        self.assertIn(JP_BODY, pack["text"])
        self.assertEqual(pack["effective_language"], "ja")
        regional = json.loads(self._cli(
            store, "factpack", "--id", PROFILE_ID, "--language", "en", "--region", "jp",
        ))
        self.assertEqual(regional["region"], "jp")
        self.assertEqual(regional["effective_language"], "ja")

    def test_mcp_dispatch_exposes_profiles_and_scoped_factpacks(self):
        server = McpServer(self.core)
        rows = self._mcp(server, "sekaisync_lookup", {"query": JP_NAME, "type": "character_profile"})
        self.assertEqual(self._profile(rows)["names"]["en"], EN_NAME)
        query = self._mcp(server, "sekaisync_query", {"query": JP_ROLE, "type": "character_profile"})
        self.assertIn(PROFILE_ID, {row["id"] for row in query["metadata"]})
        pack = self._mcp(server, "sekaisync_fact_pack", {
            "entity_id": PROFILE_ID, "language": "en", "region": "en",
        })
        self.assertIn(EN_BODY, pack["text"])
        self.assertNotIn(JP_BODY, pack["text"])
        self.assertEqual(pack["region"], "en")
        self.assertEqual(pack["effective_language"], "en")

    def test_role_label_does_not_verify_unsupported_skill_or_ownership(self):
        for field in ("canCompose", "ownsSynthesizer"):
            with self.subTest(field=field):
                result = self.core.verify_claims([{
                    "claim": EN_NAME, "entity_id": PROFILE_ID, "region": "en",
                    "field": field, "expected": "false",
                }])[0]
                self.assertEqual(result["status"], "unknown")

    def test_http_rest_exposes_region_selection_on_real_loopback_server(self):
        handler = type("ProfileHandler", (SekaiSyncHandler,), {
            "core": self.core,
            "bound_host": "127.0.0.1",
            "log_message": lambda *args: None,
        })
        server = BoundedThreadingHTTPServer(("127.0.0.1", 0), handler)
        handler.bound_port = server.server_address[1]
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True,
        )
        thread.start()

        def get(route, **parameters):
            connection = HTTPConnection(*server.server_address, timeout=3)
            try:
                connection.request("GET", route + "?" + urlencode(parameters))
                response = connection.getresponse()
                payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200, payload)
                return payload
            finally:
                connection.close()

        try:
            found = get("/api/v1/lookup", query=JP_ROLE, type="character_profile", region="jp")
            self.assertEqual(self._profile(found["results"])["facts"]["introduction_ja"], JP_BODY)
            fallback = get("/api/v1/fact_pack", entity_id=PROFILE_ID, language="en", region="jp")
            self.assertIn(JP_BODY, fallback["text"])
            self.assertNotIn(EN_BODY, fallback["text"])
            self.assertEqual(fallback["region"], "jp")
            self.assertEqual(fallback["effective_language"], "ja")
            absent = get("/api/v1/fact_pack", entity_id=PROFILE_ID, language="en", region="cn")
            self.assertEqual(absent["coverage"], "missing")
            self.assertEqual(absent["content_status"], "missing")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__ == "__main__":
    unittest.main()
