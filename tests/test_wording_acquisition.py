"""Native wording adapters, reversible private metadata and crawl resume."""
from copy import deepcopy
from contextlib import ExitStack, contextmanager
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

from sekaisync import crawler, dbstore, wording_identity as wi
from sekaisync.models import WebPage
from sekaisync.webindex import canonical_key_for_page, web_page_from_dict, web_page_to_dict


@contextmanager
def _temporary_directory():
    # Avoid TemporaryDirectory's restrictive mode on Windows sandbox ACLs.
    base = Path(tempfile.gettempdir()).resolve()
    root = base / ("sekaisync-wording-" + uuid.uuid4().hex)
    root.mkdir()
    try:
        yield root
    finally:
        assert root.resolve().parent == base and root.name.startswith("sekaisync-wording-")
        shutil.rmtree(root)


def _legacy(record, region="en"):
    text = json.dumps(record, ensure_ascii=False, indent=2)
    suffix = record.get("id", record.get("seq", hashlib.sha1(text.encode()).hexdigest()[:12]))
    return dict(id=f"web:altsource_sv:{region}:wordings:{suffix}", source="altsource_sv", kind="wordings",
        language=crawler.REGIONS[region].language, text=text, source_hash=crawler._source_sha256(record),
        url=crawler.altsource_sv_master_json_url(region, "wordings"))


class WordingAcquisitionTest(unittest.TestCase):
    def _page(self, value="\nStatus: Ready\r\n", key="TEST_UI", region="en"):
        return crawler.altsource_sv_record_page(dict(wordingKey=key, value=value), "wordings", region)

    def _crawl_table(self, records, known_ids=None):
        emitted = []
        def fetcher(url):
            self.assertIsNone(crawler._sv_master_cache_path(url))
            return json.dumps(records)
        with patch.object(crawler, "ALTSOURCE_SV_OTHER_TEXT_TABLES", ("wordings",)):
            crawler._crawl_altsource_sv_other_text("en", fetcher, emitted, None, 0, known_ids)
        return emitted

    def _parent_crawl(self, store, records):
        unrelated = ("events", "unit_stories", "card_stories", "special_stories", "virtual_lives",
                     "area_talks", "self_intros", "home_lines", "mysekai")
        with ExitStack() as patches:
            for name in unrelated:
                patches.enter_context(patch.object(crawler, "_crawl_altsource_sv_" + name, return_value=None))
            patches.enter_context(patch.object(crawler, "ALTSOURCE_SV_OTHER_TEXT_TABLES", ("wordings",)))
            return crawler._crawl_altsource_sv_text(store, ["en"], 4, 0, 0, lambda url: json.dumps(records),
                workers=1, resume=True, include_i18n=False)

    def test_first_adapter_preserves_opaque_id_and_whole_value(self):
        record = dict(wordingKey="TEST_UI", value="\nStatus: Ready\r\n")
        page = crawler.altsource_sv_record_page(record, "wordings", "en")
        self.assertEqual(page.id, _legacy(record)["id"])
        self.assertEqual(page.text, record["value"])
        self.assertEqual(page.source_hash, crawler._source_sha256(record))
        self.assertIsNotNone(wi._metadata(vars(page)))

    def test_serializer_and_sqlite_keep_private_metadata(self):
        page = self._page()
        serialized = web_page_to_dict(page)
        self.assertEqual(serialized["canonical_key"], page.canonical_key)
        self.assertEqual(canonical_key_for_page(serialized), page.canonical_key)
        restored = web_page_from_dict(deepcopy(serialized))
        self.assertEqual(web_page_to_dict(restored), serialized)
        self.assertEqual(restored.wording_provenance, page.wording_provenance)
        with _temporary_directory() as tmp:
            store = Path(tmp) / "store"
            dbstore.initialize_new_store(store, target_version=3)
            dbstore.upsert_web_pages(store, page.source, [serialized])
            with dbstore.connect(store) as conn:
                saved = wi._persisted_page(conn, page.source, page.id)
                self.assertEqual(saved["wording_identity"], serialized["wording_identity"])
                self.assertEqual(saved["wording_provenance"], serialized["wording_provenance"])
                view = wi._full_view(saved)
                self.assertEqual(view["text"], page.text)
                self.assertTrue(wi._view_policy(view))
                wi._validate_view_metadata(view, saved)
            code = (
                "from pathlib import Path\nfrom sekaisync import dbstore,wording_identity as w\nimport sys\n"
                "with dbstore.connect(Path(sys.argv[1])) as c:\n"
                " p=w._persisted_page(c,sys.argv[2],sys.argv[3])\n"
                " assert w._view_policy(w._full_view(p))\n"
                " assert p['text']==sys.argv[4]\n"
            )
            process = subprocess.run([sys.executable, "-B", "-c", code, str(store), page.source, page.id, page.text],
                capture_output=True, text=True, cwd=Path(__file__).resolve().parents[1])
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    def test_private_fields_are_not_aliased_by_serializer(self):
        page = self._page()
        serialized = web_page_to_dict(page)
        serialized["wording_identity"]["key"] = "CHANGED"
        self.assertEqual(page.wording_identity["key"], "TEST_UI")
        serialized = web_page_to_dict(page)
        restored = web_page_from_dict(serialized)
        serialized["wording_provenance"]["original_record"]["value"] = "CHANGED"
        self.assertEqual(restored.wording_provenance["original_record"]["value"], page.text)

    def test_unusable_flags_do_not_destroy_persisted_family_on_roundtrip(self):
        page = self._page()
        page.auxiliary = True
        serialized = web_page_to_dict(page)
        restored = web_page_from_dict(serialized)
        self.assertTrue(restored.auxiliary)
        self.assertEqual(restored.canonical_key, page.canonical_key)
        self.assertIsNotNone(wi._metadata(web_page_to_dict(restored)))

    def test_identical_text_different_keys_has_distinct_family(self):
        one, two = self._page("Close", "ONE"), self._page("Close", "TWO")
        self.assertNotEqual(one.canonical_key, two.canonical_key)

    def test_invalid_or_duplicate_master_fails_before_emission(self):
        invalid = [dict(value="Close"), dict(wordingKey="", value="Close"), dict(wordingKey=3, value="Close"),
                   dict(wordingKey="X", value=3), dict(wordingKey="X", value=[]), dict(wordingKey="X", value={})]
        tables = [[dict(wordingKey="VALID", value="Close"), row] for row in invalid]
        tables += [[dict(wordingKey="X", value=a), dict(wordingKey="X", value=b)]
                   for a, b in [("Close", "Close"), ("Close", "Open"), (None, "Open")]]
        for table in tables:
            with self.subTest(table=table):
                emitted = []
                with patch.object(crawler, "ALTSOURCE_SV_OTHER_TEXT_TABLES", ("wordings",)):
                    with self.assertRaises(ValueError):
                        crawler._crawl_altsource_sv_other_text("en", lambda url: json.dumps(table), emitted, None, 0)
                self.assertEqual(emitted, [])

    def test_mixed_real_expression_blank_missing_and_null_are_retained(self):
        records = [dict(wordingKey="VALID", value="Status: Ready"), dict(wordingKey="BLANK", value=" \r\n "),
                   dict(wordingKey="MISSING"), dict(wordingKey="NULL", value=None)]
        emitted = self._crawl_table(records)
        self.assertEqual(len(emitted), len(records))
        self.assertEqual([p.id for p in emitted], [_legacy(row)["id"] for row in records])
        self.assertFalse(emitted[0].untranslated)
        for page, record, reason in zip(emitted[1:], records[1:], ["blank_value", "missing_value", "explicit_null"]):
            with self.subTest(reason=reason):
                serialized = web_page_to_dict(page)
                restored = web_page_from_dict(serialized)
                self.assertTrue(page.untranslated)
                self.assertEqual(wi._metadata(serialized)["no_expression_reason"], reason)
                self.assertEqual(page.text, record.get("value") if isinstance(record.get("value"), str) else "")
                self.assertNotIn(page.text, {"None", "null"})
                self.assertEqual(restored.wording_provenance["original_record"], record)
                self.assertEqual(wi._view_metadata(serialized), {})

    def test_typed_value_versions_distinguish_absent_null_and_empty(self):
        records = [dict(wordingKey="X"), dict(wordingKey="X", value=None), dict(wordingKey="X", value="")]
        pages = [crawler.altsource_sv_record_page(row, "wordings", "en") for row in records]
        self.assertEqual({p.text for p in pages}, {""})
        self.assertEqual(len({wi._version_key(web_page_to_dict(p)) for p in pages}), 3)

    def test_changed_value_retains_resume_id_and_unchanged_skips(self):
        old = web_page_to_dict(self._page("Close"))
        existing = {old["id"]: old}
        known = set(existing) | crawler._known_inline_text_versions(existing, set(existing))
        token = crawler._wording_resume_ids.set(wi._resume_index(existing, "altsource_sv"))
        try:
            emitted = self._crawl_table([dict(wordingKey="TEST_UI", value="Open")], known)
            self.assertEqual(len(emitted), 1)
            self.assertEqual(emitted[0].id, old["id"])
            self.assertNotEqual(emitted[0].text_hash, old["text_hash"])
            fresh = web_page_to_dict(emitted[0])
            current = {fresh["id"]: fresh}
            resumed = set(current) | crawler._known_inline_text_versions(current, set(current))
            self.assertEqual(self._crawl_table([dict(wordingKey="TEST_UI", value="Open")], resumed), [])
        finally:
            crawler._wording_resume_ids.reset(token)
        self.assertIsNone(crawler._wording_resume_ids.get())

    def test_legacy_json_is_validated_and_refetched_at_same_id(self):
        record = dict(wordingKey="LEGACY", value="Close")
        old = _legacy(record)
        existing = {old["id"]: old}
        self.assertEqual(wi._legacy_record(old), record)
        self.assertEqual(crawler._known_inline_text_versions(existing, set(existing)), set())
        token = crawler._wording_resume_ids.set(wi._resume_index(existing, "altsource_sv"))
        try:
            emitted = self._crawl_table([dict(wordingKey="LEGACY", value="Open")], set(existing))
        finally:
            crawler._wording_resume_ids.reset(token)
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0].id, old["id"])
        self.assertEqual(emitted[0].text, "Open")
        self.assertIsNotNone(wi._metadata(web_page_to_dict(emitted[0])))

    def test_normal_parent_crawl_binds_legacy_and_changed_resume_ids(self):
        legacy = _legacy(dict(wordingKey="LEGACY_PARENT", value="Close"))
        with _temporary_directory() as tmp:
            store = tmp / "store"
            dbstore.initialize_new_store(store, target_version=3)
            dbstore.upsert_web_pages(store, "altsource_sv", [legacy])
            changed = [dict(wordingKey="LEGACY_PARENT", value="\nStatus: Open\r\n")]
            first = self._parent_crawl(store, changed)
            self.assertEqual(first["crawled_pages"], 1)
            saved, = dbstore.load_web_pages(store)["altsource_sv"]
            self.assertEqual(saved["id"], legacy["id"])
            self.assertEqual(saved["text"], changed[0]["value"])
            self.assertTrue(wi._view_policy(wi._full_view(saved)))
            self.assertIsNone(crawler._wording_resume_ids.get())
            self.assertEqual(self._parent_crawl(store, changed)["crawled_pages"], 0)
            newest = [dict(wordingKey="LEGACY_PARENT", value="Status: Updated")]
            self.assertEqual(self._parent_crawl(store, newest)["crawled_pages"], 1)
            saved, = dbstore.load_web_pages(store)["altsource_sv"]
            self.assertEqual(saved["id"], legacy["id"])
            self.assertEqual(saved["text"], newest[0]["value"])

    def test_normal_parent_resume_retains_explicit_nonexpression_debt(self):
        records = [dict(wordingKey="BLANK_PARENT", value="  "), dict(wordingKey="MISSING_PARENT")]
        with _temporary_directory() as tmp:
            store = tmp / "store"
            dbstore.initialize_new_store(store, target_version=3)
            self.assertEqual(self._parent_crawl(store, records)["crawled_pages"], 2)
            saved = dbstore.load_web_pages(store)["altsource_sv"]
            self.assertEqual(len(saved), 2)
            self.assertTrue(all(wi._metadata(page)["no_expression"] for page in saved))
            self.assertEqual(self._parent_crawl(store, records)["crawled_pages"], 0)
            recovered = [dict(wordingKey="BLANK_PARENT", value="Recovered"), records[1]]
            self.assertEqual(self._parent_crawl(store, recovered)["crawled_pages"], 1)
            current = dbstore.load_web_pages(store)["altsource_sv"]
            self.assertEqual(len(current), 2)
            self.assertEqual(sum(not wi._metadata(page)["no_expression"] for page in current), 1)

    def test_legacy_source_hash_and_id_binding_tamper_are_rejected(self):
        for field, replacement in [("source_hash", "0" * 64), ("id", "web:altsource_sv:en:wordings:wrong"),
                                   ("language", "ja"), ("text", "null")]:
            old = _legacy(dict(wordingKey="LEGACY", value="Close"))
            old[field] = replacement
            with self.subTest(field=field), self.assertRaises(ValueError):
                wi._resume_index({old["id"]: old}, "altsource_sv")

    def test_ambiguous_resume_anchors_rejected(self):
        one, two = web_page_to_dict(self._page("Close")), web_page_to_dict(self._page("Open"))
        with self.assertRaises(ValueError):
            wi._resume_index({one["id"]: one, two["id"]: two}, "altsource_sv")

    def test_reversible_token_or_map_or_provenance_tamper_rejected(self):
        for mutate in [lambda p: p["wording_provenance"].update(adapter_value_token='"invented"'),
                       lambda p: p["wording_provenance"]["decoded_unicode_to_token_unicode"][0].update(start=0),
                       lambda p: p["wording_provenance"].update(token_provenance="original master token")]:
            page = web_page_to_dict(self._page())
            mutate(page)
            with self.assertRaisesRegex(ValueError, "reversible"):
                wi._metadata(page)

    def test_unicode_token_mapping_roundtrips_crlf_and_nonbmp(self):
        page = web_page_to_dict(self._page("\nUI: \U0001f4a0\r\n\u5c31\u7eea"))
        provenance = page["wording_provenance"]
        token = provenance["adapter_value_token"]
        parts = provenance["decoded_unicode_to_token_unicode"]
        self.assertEqual("".join(part["exact"] for part in parts), page["text"])
        self.assertEqual([json.loads('"' + token[p["start"]:p["end"]] + '"') for p in parts], [p["exact"] for p in parts])

    def test_forged_nonwording_marker_and_view_donor_rejected(self):
        page = web_page_to_dict(self._page())
        forged = deepcopy(page)
        forged["kind"] = "event_story"
        with self.assertRaises(ValueError):
            wi._metadata(forged)
        view = wi._full_view(page)
        view["page_id"] = "other"
        with self.assertRaises(ValueError):
            wi._view_policy(view)
        with self.assertRaises(ValueError):
            wi._validate_view_metadata(dict(wi._full_view(page), wording_body={}), page)
        with self.assertRaises(ValueError):
            wi._view_policy(dict(wi._full_view(page), wording_body=None))
        with self.assertRaises(ValueError):
            wi._metadata(dict(_legacy(dict(wordingKey="X", value="Close")), wording_identity=None, wording_provenance=None))

    def test_dataclass_has_no_new_public_parameters(self):
        self.assertNotIn("wording_identity", inspect.signature(WebPage).parameters)
        self.assertNotIn("wording_provenance", inspect.signature(WebPage).parameters)
        self.assertEqual(str(inspect.signature(crawler.altsource_sv_record_page)),
            "(record: 'dict[str, Any]', table: 'str', region: 'str') -> 'WebPage'")


if __name__ == "__main__":
    unittest.main()
