"""Executable SQL examples for relational-sqlite/1, not a Backpack backend."""

from contextlib import contextmanager
import sqlite3
import unittest
from uuid import uuid4


class RelationalReference(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.connection.close)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        for statement in (
            "CREATE TABLE bp_application_keys (namespace TEXT NOT NULL, type_key TEXT NOT NULL, key TEXT NOT NULL, storage_uuid TEXT NOT NULL UNIQUE, PRIMARY KEY(namespace,type_key,key))",
            "CREATE TABLE app_projects (key TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL UNIQUE)",
            """CREATE TABLE app_work_items (
                key TEXT PRIMARY KEY NOT NULL, _storage_uuid TEXT NOT NULL UNIQUE,
                _revision INTEGER NOT NULL CHECK(_revision BETWEEN 1 AND 9223372036854775807),
                title TEXT NOT NULL, done INTEGER NOT NULL CHECK(done IN (0,1)),
                project_key TEXT REFERENCES app_projects(key) ON DELETE RESTRICT,
                rank INTEGER CHECK(rank BETWEEN -9007199254740991 AND 9007199254740991),
                estimate REAL,
                FOREIGN KEY(_storage_uuid) REFERENCES bp_application_keys(storage_uuid))""",
            "CREATE INDEX app_items_done_rank_key ON app_work_items(done,rank,key)",
        ):
            self.connection.execute(statement)
        self.connection.execute("INSERT INTO app_projects VALUES (?,?)", ("garden", "Garden"))

    @contextmanager
    def transaction(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise

    def create(self, key, *, title="Garden", rank=None, project="garden"):
        # These trusted example values are already model-validated; production
        # adapters must validate before binding and after loading too.
        identity = str(uuid4())
        with self.transaction():
            self.connection.execute("INSERT INTO bp_application_keys VALUES (?,?,?,?)", ("garden", "garden.item", key, identity))
            self.connection.execute("INSERT INTO app_work_items VALUES (?,?,1,?,0,?,?,NULL)",
                                    (key, identity, title, project, rank))
        return identity

    def test_domain_key_storage_uuid_and_detached_values_are_distinct(self):
        identity = self.create("item-001")
        row = dict(self.connection.execute("SELECT * FROM app_work_items WHERE key=?", ("item-001",)).fetchone())
        self.assertNotEqual(identity, row["key"])
        self.assertEqual(row["_storage_uuid"], identity)
        row["title"] = "unsaved edit"
        self.assertEqual(self.connection.execute("SELECT title FROM app_work_items").fetchone()[0], "Garden")

    def test_primary_unique_foreign_keys_and_failed_reservation_roll_back(self):
        self.create("item-001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.create("item-001")
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("INSERT INTO app_projects VALUES (?,?)", ("other", "Garden"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.create("invalid-parent", project="missing")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bp_application_keys").fetchone()[0], 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM app_projects WHERE key=?", ("garden",))

    def test_equal_values_and_nulls_have_deterministic_bounded_order(self):
        for key, rank in (("item-c", None), ("item-b", 1), ("item-a", 1), ("item-d", 2)):
            self.create(key, rank=rank)
        ordered = self.connection.execute("SELECT key FROM app_work_items ORDER BY rank IS NULL,rank,key LIMIT ?", (4,))
        self.assertEqual([row[0] for row in ordered], ["item-a", "item-b", "item-d", "item-c"])
        # Representative continuation after (rank=1,key=item-a), including NULL tail.
        page = self.connection.execute("""SELECT key FROM app_work_items
            WHERE rank IS NULL OR rank>? OR (rank=? AND key>?)
            ORDER BY rank IS NULL,rank,key LIMIT ?""", (1, 1, "item-a", 2))
        self.assertEqual([row[0] for row in page], ["item-b", "item-d"])

    def test_parameter_binding_preserves_injection_shaped_values(self):
        title = "x'); DROP TABLE app_work_items; --"
        self.create("item-001", title=title)
        rows = self.connection.execute("SELECT key FROM app_work_items WHERE title=?", (title,)).fetchall()
        self.assertEqual([row[0] for row in rows], ["item-001"])
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM app_work_items").fetchone()[0], 1)

    def test_stale_revision_and_two_operation_rollback_preserve_data(self):
        self.create("item-001")
        with self.transaction():
            changed = self.connection.execute("UPDATE app_work_items SET title=?,_revision=_revision+1 WHERE key=? AND _revision=?",
                                              ("Committed", "item-001", 1)).rowcount
        self.assertEqual(changed, 1)
        with self.transaction():
            stale = self.connection.execute("UPDATE app_work_items SET title=?,_revision=_revision+1 WHERE key=? AND _revision=?",
                                            ("Stale", "item-001", 1)).rowcount
        self.assertEqual(stale, 0)
        with self.assertRaises(sqlite3.IntegrityError):
            with self.transaction():
                self.connection.execute("UPDATE app_work_items SET title=? WHERE key=?", ("Rolled back", "item-001"))
                self.connection.execute("INSERT INTO app_projects VALUES (?,?)", ("duplicate", "Garden"))
        row = self.connection.execute("SELECT title,_revision FROM app_work_items").fetchone()
        self.assertEqual(tuple(row), ("Committed", 2))

    def test_deleted_keys_cannot_be_reused_with_an_old_revision(self):
        self.create("item-001")
        with self.transaction():
            self.connection.execute("DELETE FROM app_work_items WHERE key=? AND _revision=?", ("item-001", 1))
        with self.assertRaises(sqlite3.IntegrityError):
            self.create("item-001")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM app_work_items").fetchone()[0], 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM bp_application_keys").fetchone()[0], 1)

    def test_null_comparison_and_declared_index_plan(self):
        self.create("item-null")
        self.create("item-one", rank=1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM app_work_items WHERE rank<>?", (2,)).fetchone()[0], 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM app_work_items WHERE rank IS NULL").fetchone()[0], 1)
        plans = self.connection.execute("EXPLAIN QUERY PLAN SELECT key FROM app_work_items WHERE done=? AND rank>? ORDER BY rank,key LIMIT ?",
                                        (0, 0, 10)).fetchall()
        self.assertTrue(any("SEARCH" in row[3] and "app_items_done_rank_key" in row[3] for row in plans))


if __name__ == "__main__":
    unittest.main()
