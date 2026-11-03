from contextlib import closing
from pathlib import Path
import os
import sqlite3
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backpack_store import (
    ApplicationStore, DatabaseVersionError, DataclassCodec, Index, MigrationHistory,
    MigrationStep, SchemaMigrationError, SnapshotError, migrate_sqlite, snapshot_sqlite,
    verify_snapshot, ValidationError,
)
from test_relational import Project, register


def base_schema(connection):
    connection.execute("CREATE TABLE documents (key TEXT PRIMARY KEY, title TEXT NOT NULL)")


def add_rank(connection):
    connection.execute("ALTER TABLE documents ADD COLUMN rank INTEGER")


STEPS = [MigrationStep(1, "documents", base_schema), MigrationStep(2, "document-ranks", add_rank)]


class SchemaMigrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "application.sqlite"
        self.backups = Path(self.directory.name) / "backups"

    def migrate(self, steps=STEPS, **kwargs):
        return migrate_sqlite(self.path, namespace="documents", steps=steps, backup_directory=self.backups, **kwargs)

    def test_fresh_and_current_schemas_avoid_backups_and_upgrade_preserves_rows(self):
        self.assertIsNone(self.migrate(STEPS[:1]).backup)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("INSERT INTO documents VALUES ('a','Garden')")
        result = self.migrate()
        self.assertEqual(result.version, 2)
        verify_snapshot(result.backup)
        with closing(sqlite3.connect(result.backup.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM documents").fetchall(), [("a", "Garden")])
            self.assertEqual(connection.execute("SELECT version FROM bp_schema_migrations").fetchall(), [(1,)])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM documents").fetchall(), [("a", "Garden", None)])
            receipt = connection.execute("SELECT backup_path,backup_sha256 FROM bp_schema_migrations WHERE version=2").fetchone()
            self.assertEqual(receipt, (str(result.backup.path), result.backup.sha256))
        self.assertIsNone(self.migrate().backup)
        self.assertEqual(len(list(self.backups.glob("*.sqlite3"))), 1)

    def test_schema_failure_rolls_back_all_steps_and_retains_usable_backup(self):
        self.migrate(STEPS[:1])
        def fail(connection):
            connection.execute("CREATE TABLE incomplete (key TEXT)")
            raise ValueError("private migration value")
        with self.assertRaises(SchemaMigrationError) as caught:
            self.migrate([*STEPS, MigrationStep(3, "failed-step", fail)])
        self.assertNotIn("private migration value", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM bp_schema_migrations").fetchone()[0], 1)
            self.assertEqual([row[1] for row in connection.execute("PRAGMA table_info(documents)")], ["key", "title"])
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='incomplete'").fetchone())
        self.assertEqual(len(list(self.backups.glob("*.sqlite3"))), 1)
        self.migrate()

    def test_backup_failure_prevents_any_upgrade_mutation(self):
        self.migrate(STEPS[:1])
        before = self.path.read_bytes()
        with patch("backpack_store.migrations.snapshot_sqlite", side_effect=SnapshotError("failed")):
            with self.assertRaises(SnapshotError):
                self.migrate()
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_gapped_renamed_or_incomplete_history_stops_without_rewriting(self):
        for sql in ("UPDATE bp_schema_migrations SET version=9", "UPDATE bp_schema_migrations SET name='changed'",
                    "DELETE FROM bp_schema_migrations", "UPDATE bp_schema_migrations SET applied_at='invalid'",
                    "UPDATE bp_schema_migrations SET backup_path='incomplete-receipt'"):
            with self.subTest(sql=sql):
                self.path.unlink(missing_ok=True)
                self.migrate(STEPS[:1])
                with closing(sqlite3.connect(self.path)) as connection, connection:
                    connection.execute(sql)
                before = self.path.read_bytes()
                with self.assertRaises(DatabaseVersionError):
                    self.migrate()
                self.assertEqual(self.path.read_bytes(), before)

    def test_consumer_history_requires_explicit_adoption_and_retains_names(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            base_schema(connection)
            connection.execute("CREATE TABLE pnbp_schema_migrations (version INTEGER PRIMARY KEY,name TEXT NOT NULL,applied_at TEXT NOT NULL,backup_path TEXT,backup_sha256 TEXT)")
            connection.execute("INSERT INTO pnbp_schema_migrations VALUES (1,'documents','2026-11-02T00:00:00+00:00',NULL,NULL)")
        history = MigrationHistory("pnbp_schema_migrations", scoped=False)
        with self.assertRaises(DatabaseVersionError):
            self.migrate(history=history)
        result = self.migrate(history=history, adopt_history=True)
        verify_snapshot(result.backup)
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT version,name FROM pnbp_schema_migrations ORDER BY version").fetchall(), [(1, "documents"), (2, "document-ranks")])
            self.assertEqual(connection.execute("SELECT applied_at FROM pnbp_schema_migrations WHERE version=1").fetchone()[0], "2026-11-02T00:00:00+00:00")
        self.assertIsNone(self.migrate(history=history).backup)

    def test_namespace_histories_are_independent(self):
        self.migrate(STEPS[:1])
        other = [MigrationStep(1, "other-data", lambda connection: connection.execute("CREATE TABLE other_data (key TEXT)"))]
        migrate_sqlite(self.path, namespace="other", steps=other, backup_directory=self.backups)
        self.migrate()
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT namespace,count(*) FROM bp_schema_migrations GROUP BY namespace ORDER BY namespace").fetchall(), [("documents", 2), ("other", 1)])

    def test_callback_transaction_escape_is_refused_and_rolled_back(self):
        self.migrate(STEPS[:1])
        for escape in (lambda connection: connection.commit(),
                       lambda connection: connection.executescript("CREATE TABLE escaped (key TEXT);")):
            with self.assertRaises(SchemaMigrationError):
                self.migrate([STEPS[0], MigrationStep(2, "escape", escape)])
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM bp_schema_migrations").fetchone()[0], 1)
            self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='escaped'").fetchone())

    def test_application_upgrade_checks_owned_version_target_ddl_and_history(self):
        with ApplicationStore.open(self.path) as store:
            projects, _ = register(store)
            store.initialize("garden")
            projects.create(Project("garden", "Garden"))
        with ApplicationStore.open(self.path) as store:
            projects, _ = register(store, project_indexes=(Index("app_project_name", ("name",)),))
            steps = [MigrationStep(2, "project-name-index", lambda connection: connection.execute(projects.schema_sql[1]))]
            def validate_current(connection, version):
                self.assertEqual(version, 1)
                self.assertEqual(connection.execute("SELECT name FROM app_projects").fetchone()[0], "Garden")
            result = store.upgrade("garden", expected_version=1, migrations=steps,
                                    backup_directory=self.backups, validate_current=validate_current)
            verify_snapshot(result.backup)
            self.assertEqual(projects.get("garden").value.name, "Garden")
            with self.assertRaises(DatabaseVersionError):
                store.initialize("garden", schema_version=2)
            store.initialize("garden", schema_version=2, migrations=steps)
        with ApplicationStore.open(self.path) as reopened:
            projects, _ = register(reopened, project_indexes=(Index("app_project_name", ("name",)),))
            reopened.initialize("garden", schema_version=2, migrations=steps)
            self.assertEqual(projects.get("garden").value.name, "Garden")
            with self.assertRaises(DatabaseVersionError):
                reopened.upgrade("garden", expected_version=1, migrations=steps,
                                  backup_directory=self.backups, validate_current=lambda connection, version: None)


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "source.sqlite"
        self.backups = Path(self.directory.name) / "backups"

    def test_consumer_prefixes_preserve_filenames_and_refuse_path_components(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE notes (title TEXT)")
        for prefix in ("server.sqlite3-from-v0", "Notes archive-études"):
            receipt = snapshot_sqlite(self.path, self.backups, prefix=prefix)
            self.assertTrue(receipt.path.name.startswith(prefix + "-"))
            verify_snapshot(receipt)
        before = set(self.backups.iterdir())
        for prefix in ("", "../outside", "a/b", "a\\b", "a\n", "\ud800", "é" * 91, None):
            with self.subTest(prefix=repr(prefix)), self.assertRaises(ValidationError):
                snapshot_sqlite(self.path, self.backups, prefix=prefix)
        self.assertEqual(set(self.backups.iterdir()), before)

    def test_committed_wal_is_included_and_receipt_detects_tampering(self):
        with closing(sqlite3.connect(self.path)) as connection:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("CREATE TABLE notes (title TEXT)")
            connection.execute("INSERT INTO notes VALUES ('committed WAL')")
            connection.commit()
            connection.execute("INSERT INTO notes VALUES ('uncommitted')")
            result = snapshot_sqlite(self.path, self.backups)
            verify_snapshot(result)
            with closing(sqlite3.connect(result.path)) as backup:
                self.assertEqual(backup.execute("SELECT * FROM notes").fetchall(), [("committed WAL",)])
        if os.name != "nt":
            self.assertEqual(self.backups.stat().st_mode & 0o777, 0o700)
            self.assertEqual(result.path.stat().st_mode & 0o777, 0o600)
        with result.path.open("ab") as stream:
            stream.write(b"changed")
        with self.assertRaises(SnapshotError):
            verify_snapshot(result)

    def test_partial_failure_removes_incomplete_file_without_changing_source(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE notes (title TEXT)")
        before = self.path.read_bytes()
        with patch("backpack_store.snapshots._sync_directory", side_effect=OSError("failed sync")):
            with self.assertRaises(SnapshotError):
                snapshot_sqlite(self.path, self.backups)
        self.assertEqual(list(self.backups.iterdir()), [])
        self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_synchronization_supports_write_required_file_handles(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE notes (title TEXT)")
        synchronize = os.fsync
        def require_write_access(descriptor):
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                # Zero-byte writes check access without changing snapshot data.
                os.write(descriptor, b"")
            synchronize(descriptor)
        with patch("backpack_store.snapshots.os.fsync", side_effect=require_write_access):
            receipt = snapshot_sqlite(self.path, self.backups)
        verify_snapshot(receipt)

    @unittest.skipIf(os.name == "nt", "symlink privileges vary on Windows")
    def test_symlink_and_nonprivate_destination_are_refused(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE notes (title TEXT)")
        target = Path(self.directory.name) / "target"
        target.mkdir(mode=0o700)
        self.backups.symlink_to(target, target_is_directory=True)
        with self.assertRaises(SnapshotError):
            snapshot_sqlite(self.path, self.backups)
        self.backups.unlink()
        self.backups.mkdir(mode=0o755)
        self.backups.chmod(0o755)
        with self.assertRaises(SnapshotError):
            snapshot_sqlite(self.path, self.backups)
