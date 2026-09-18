"""dbstore contract tests — schema version gating (Astra P07 / D07).

A store stamped with an unknown (newer) schema version must never be silently
reinterpreted as the current version.  Today ``ensure_store`` treats
"version != mine" as "uninitialized" and rewrites the stamp, which turns a
newer store into a plausible-looking older one.
"""

import contextlib
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from sekaisync import dbstore


class _TempStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="test_dbstore_")
        self.store = Path(self._tmp.name)
        dbstore.initialize(self.store)

    def tearDown(self):
        self._tmp.cleanup()

    @contextlib.contextmanager
    def _conn(self):
        """sqlite3's own context manager commits but does NOT close.

        On Windows an unclosed handle keeps the .db file locked and breaks
        TemporaryDirectory cleanup, so close explicitly.
        """
        conn = sqlite3.connect(str(dbstore.db_path(self.store)))
        try:
            yield conn
        finally:
            conn.close()

    def _meta(self, key):
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM meta WHERE key=?", (key,)
            ).fetchone()
        return row[0] if row else None

    def _stamp(self, version):
        with self._conn() as conn:
            conn.execute(
                "UPDATE meta SET value=? WHERE key='schema_version'", (version,)
            )
            conn.commit()


class SchemaVersionGateTest(_TempStore):
    """P07 — unknown schema versions must not be silently reinterpreted."""

    def test_future_schema_version_is_not_silently_downgraded(self):
        self._stamp("99")
        self.assertEqual(self._meta("schema_version"), "99")

        try:
            dbstore.ensure_store(self.store)
        except Exception:  # noqa: BLE001 - refusing loudly is acceptable
            pass

        self.assertEqual(
            self._meta("schema_version"),
            "99",
            "ensure_store silently downgraded a future schema version to the "
            "current one instead of refusing",
        )

    def test_initialized_false_for_future_version(self):
        self._stamp("99")
        self.assertFalse(dbstore.initialized(self.store))

    def test_current_version_still_works(self):
        """The gate must not break the ordinary same-version path."""
        dbstore.ensure_store(self.store)
        self.assertTrue(dbstore.initialized(self.store))
        self.assertEqual(self._meta("schema_version"), dbstore.SCHEMA_VERSION)


class SchemaVersionErrorObjectTest(_TempStore):
    """P07 — the refusal must be a typed, actionable error, not a bare crash."""

    def test_newer_store_raises_typed_error_naming_both_versions(self):
        self._stamp("99")
        with self.assertRaises(dbstore.SchemaVersionError) as ctx:
            dbstore.ensure_store(self.store)
        exc = ctx.exception
        self.assertEqual(exc.status, "newer")
        self.assertEqual(exc.found, "99")
        self.assertEqual(exc.supported, dbstore.SCHEMA_VERSION)
        # Actionable: the reader must see both versions without extra digging.
        self.assertIn("99", str(exc))
        self.assertIn(dbstore.SCHEMA_VERSION, str(exc))
        self.assertIn("newer", str(exc))

    def test_error_carries_structured_fields_for_callers(self):
        """Callers can branch on the fields instead of parsing the message."""
        self._stamp("99")
        try:
            dbstore.ensure_store(self.store)
        except dbstore.SchemaVersionError as exc:
            self.assertEqual(exc.status, "newer")
            self.assertEqual(exc.found, "99")
            self.assertEqual(exc.supported, dbstore.SCHEMA_VERSION)
            self.assertTrue(str(exc.path).endswith("sekaisync.db"))
        else:
            self.fail("ensure_store did not refuse a newer store")
        self.assertTrue(issubclass(dbstore.SchemaVersionError, RuntimeError))


class InspectSchemaTest(_TempStore):
    """P07 — inspect_schema classifies without mutating anything."""

    def test_current_store_is_current(self):
        self.assertEqual(dbstore.inspect_schema(self.store).status, "current")

    def test_newer_and_older_are_distinguished(self):
        self._stamp("99")
        self.assertEqual(dbstore.inspect_schema(self.store).status, "newer")
        self._stamp("0")
        self.assertEqual(dbstore.inspect_schema(self.store).status, "older")

    def test_absent_store_is_not_created_by_inspect(self):
        missing = Path(self._tmp.name) / "no_such_store"
        state = dbstore.inspect_schema(missing)
        self.assertEqual(state.status, "absent")
        self.assertFalse(missing.exists(), "inspect_schema created the store root")
        self.assertFalse(dbstore.db_path(missing).exists())

    def test_corrupt_store_is_reported_not_stamped(self):
        path = dbstore.db_path(self.store)
        path.write_bytes(b"this is not a sqlite database" * 32)
        state = dbstore.inspect_schema(self.store)
        self.assertEqual(state.status, "corrupt")
        self.assertEqual(
            path.read_bytes(),
            b"this is not a sqlite database" * 32,
            "inspect_schema rewrote an unreadable database",
        )

    def test_non_empty_db_without_meta_row_is_not_guessed_new(self):
        """A real database with no schema stamp must never be treated as fresh."""
        with self._conn() as conn:
            conn.execute("DROP TABLE meta")
            conn.commit()
        self.assertEqual(dbstore.inspect_schema(self.store).status, "corrupt")

    def test_absent_store_still_initializes(self):
        """Creating a brand-new store must keep working."""
        fresh = Path(self._tmp.name) / "fresh_store"
        dbstore.ensure_store(fresh)
        self.assertTrue(dbstore.initialized(fresh))
        self.assertEqual(dbstore.inspect_schema(fresh).status, "current")


class NoRewriteOnReadTest(_TempStore):
    """P07 — read/ensure paths must leave an unrecognised store byte-identical."""

    def _file_bytes(self):
        return dbstore.db_path(self.store).read_bytes()

    def _table_names(self):
        with self._conn() as conn:
            return sorted(
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%'"
                )
            )

    def _assert_refused_and_untouched(self, call, expected_stamp="99"):
        before_bytes = self._file_bytes()
        before_tables = self._table_names()
        before_hash = hashlib.sha256(before_bytes).hexdigest()
        with self.assertRaises(dbstore.SchemaVersionError):
            call()
        self.assertEqual(
            self._meta("schema_version"), expected_stamp,
            "the schema stamp was rewritten by a read/ensure path",
        )
        self.assertEqual(self._table_names(), before_tables,
                         "new tables appeared in a refused store")
        self.assertEqual(
            hashlib.sha256(self._file_bytes()).hexdigest(), before_hash,
            "the database file changed while refusing to open it",
        )

    def test_ensure_store_leaves_file_byte_identical(self):
        self._stamp("99")
        self._assert_refused_and_untouched(lambda: dbstore.ensure_store(self.store))

    def test_initialize_refuses_to_restamp_future_store(self):
        self._stamp("99")
        self._assert_refused_and_untouched(lambda: dbstore.initialize(self.store))

    def test_read_paths_refuse_future_store(self):
        """Read entry points funnel through ensure_store and must also refuse."""
        self._stamp("99")
        for call in (
            lambda: dbstore.load_entities(self.store),
            lambda: dbstore.load_glossary_terms(self.store),
            lambda: dbstore.load_terms_records(self.store),
            lambda: dbstore.count_rows(self.store),
        ):
            with self.assertRaises(dbstore.SchemaVersionError):
                call()
        self.assertEqual(self._meta("schema_version"), "99")

    def test_write_paths_refuse_future_store(self):
        """Write entry points funnel through _ensure_initialized and must refuse."""
        self._stamp("99")
        for call in (
            lambda: dbstore.upsert_web_pages(self.store, "src", []),
            lambda: dbstore.save_entities(self.store, []),
            lambda: dbstore.save_glossary_terms(self.store, []),
            lambda: dbstore.delete_source_pages(self.store, "src"),
        ):
            with self.assertRaises(dbstore.SchemaVersionError):
                call()
        self.assertEqual(self._meta("schema_version"), "99")

    def test_corrupt_store_is_not_silently_recreated(self):
        path = dbstore.db_path(self.store)
        garbage = b"not a database" * 64
        path.write_bytes(garbage)
        with self.assertRaises(dbstore.SchemaVersionError):
            dbstore.ensure_store(self.store)
        self.assertEqual(path.read_bytes(), garbage,
                         "ensure_store overwrote an unreadable database")

    def test_older_store_is_refused_not_upgraded_in_place(self):
        self._stamp("0")
        self._assert_refused_and_untouched(
            lambda: dbstore.ensure_store(self.store), expected_stamp="0"
        )
        self.assertEqual(self._meta("schema_version"), "0")


if __name__ == "__main__":
    unittest.main()
