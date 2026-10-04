from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from test_codecs import Field, Note, ProjectItem

from backpack_store import (
    Backpack,
    DataclassCodec,
    InvalidCursorError,
    NotFoundError,
    Provenance,
    RevisionConflictError,
    StorageError,
    StoreClosedError,
    UnknownTypeError,
    UnknownVersionError,
    ValidationError,
)


def register(store):
    store.register(DataclassCodec(Note, type_key="notes.note"))
    store.register(DataclassCodec(ProjectItem, type_key="github.project_item"))


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "records.sqlite"
        self.store = Backpack.open(self.path)
        self.addCleanup(self.store.close)
        register(self.store)

    def test_disk_reopen_recovers_types_identity_and_provenance(self):
        note = Note("Local note", "Stored beyond one connection", 2)
        note_write = self.store.put(note, tags=[" work ", "work"])
        item = ProjectItem(
            "PVT_fixture",
            "PVTI_fixture",
            "I_fixture",
            "issue",
            (Field("Status", "Todo"),),
        )
        provenance = Provenance(
            "https://github.com/orgs/example/projects/1",
            "github",
            item.item_id,
            "2026-10-04T00:00:00.000000Z",
        )
        item_write = self.store.put(item, tags=["snapshot"], provenance=provenance)
        expected = self.store.get_record(item_write.id)
        self.store.close()
        with Backpack.open(self.path) as reopened:
            self.assertEqual(reopened.registered_keys, ())
            self.assertEqual(reopened.get_raw(item_write.id).provenance, provenance)
            with self.assertRaises(UnknownTypeError):
                reopened.get(item_write.id)
            register(reopened)
            self.assertEqual(reopened.get(note_write.id), note)
            self.assertEqual(reopened.get_record(item_write.id), expected)
            self.assertIs(type(reopened.get(item_write.id)), ProjectItem)
            self.assertNotEqual(item_write.id, item.item_id)
            self.assertEqual(
                reopened.find(Note, tags=["work"]).records[0].id, note_write.id
            )

    def test_each_put_has_distinct_identity_and_detached_payload(self):
        value = Note("a", "b")
        first, second = self.store.put(value), self.store.put(value)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.revision, 1)
        raw = self.store.get_raw(first.id)
        raw.payload["title"] = "changed locally"
        self.assertEqual(self.store.get(first.id), value)

    def test_all_tags_and_type_filter_with_keyset_pages(self):
        expected = []
        for i in range(7):
            write = self.store.put(Note(str(i), "text"), tags=["work", "selected"])
            expected.append(write.id)
        self.store.put(Note("excluded", "text"), tags=["work"])
        self.store.put(
            ProjectItem("p", "i", None, "draft", ()), tags=["work", "selected"]
        )
        found, cursor = [], None
        while True:
            page = self.store.find(
                Note, tags=["selected", "work"], limit=2, cursor=cursor
            )
            found.extend(record.id for record in page.records)
            cursor = page.next_cursor
            if cursor is None:
                break
        self.assertEqual(found, sorted(expected))
        self.assertEqual(self.store.find(Note, tags=["absent"]).records, ())

    def test_cursor_is_validated_and_scoped_to_filters(self):
        for _ in range(3):
            self.store.put(Note("a", "b"), tags=["work"])
        cursor = self.store.find(Note, tags=["work"], limit=1).next_cursor
        for query in (
            {"cursor": "not base64"},
            {"cursor": cursor, "tags": ["other"]},
            {"cursor": ""},
            {"cursor": "a" * 32_769},
        ):
            with self.assertRaises(InvalidCursorError):
                self.store.find(Note, **query)
        with self.assertRaises(InvalidCursorError):
            self.store.find(ProjectItem, tags=["work"], cursor=cursor)
        for limit in (0, -1, 1001, True, 2.5):
            with self.assertRaises(ValidationError):
                self.store.find(Note, limit=limit)

    def test_revision_conflict_between_independent_connections(self):
        write = self.store.put(Note("v1", "text"), tags=["old"])
        before = self.store.get_raw(write.id)
        with Backpack.open(self.path) as other:
            register(other)
            stale = other.get_raw(write.id)
            updated = self.store.update(
                write.id, Note("v2", "text"), expected_revision=1, tags=["new"]
            )
            self.assertEqual(updated.revision, 2)
            with self.assertRaises(RevisionConflictError):
                other.update(
                    write.id, Note("stale", "text"), expected_revision=stale.revision
                )
            self.assertEqual(other.get(write.id).title, "v2")
        after = self.store.get_raw(write.id)
        self.assertEqual(after.created_at, before.created_at)
        self.assertGreaterEqual(after.updated_at, before.updated_at)
        self.assertEqual(after.tags, ("new",))
        with self.assertRaises(ValidationError):
            self.store.update(
                write.id, ProjectItem("p", "i", None, "draft", ()), expected_revision=2
            )

    def test_database_failure_rolls_back_payload_metadata_and_tags(self):
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "CREATE TRIGGER fail_tag BEFORE INSERT ON tags WHEN NEW.tag = 'fail' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
            )
        with self.assertRaises(StorageError):
            self.store.put(Note("failed", "text"), tags=["fail"])
        self.assertEqual(self.store.find(Note).records, ())
        write = self.store.put(Note("original", "text"), tags=["original"])
        before = self.store.get_record(write.id)
        with self.assertRaises(StorageError):
            self.store.update(
                write.id,
                Note("failed", "text"),
                expected_revision=1,
                tags=["a", "fail"],
            )
        self.assertEqual(self.store.get_record(write.id), before)

    def test_invalid_input_never_commits_a_record(self):
        for operation in (
            lambda: self.store.put({"unregistered": True}),
            lambda: self.store.put(Note("a", "b", True)),
            lambda: self.store.put(Note("a", "b"), tags=[""]),
            lambda: self.store.put(Note("a", "b"), provenance={"source_id": "i"}),
        ):
            with self.assertRaises((UnknownTypeError, ValidationError)):
                operation()
        self.assertEqual(self.store.find(Note).records, ())

    def test_delete_missing_records_and_tag_cleanup(self):
        missing = str(uuid4())
        with self.assertRaises(NotFoundError):
            self.store.get(missing)
        with self.assertRaises(NotFoundError):
            self.store.update(missing, Note("a", "b"), expected_revision=1)
        self.assertFalse(self.store.delete(missing))
        write = self.store.put(Note("a", "b"), tags=["work"])
        self.assertTrue(self.store.delete(write.id))
        self.assertFalse(self.store.delete(write.id))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM tags").fetchone()[0], 0
            )
        with self.assertRaises(ValidationError):
            self.store.get("I_remote_identity")

    def test_unknown_payload_version_is_recoverable_without_decoding(self):
        write = self.store.put(Note("a", "b"))
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute(
                "UPDATE records SET schema_version = 2 WHERE id = ?", (write.id,)
            )
        with self.assertRaises(UnknownVersionError):
            self.store.get(write.id)
        self.assertEqual(self.store.get_raw(write.id).schema_version, 2)
        self.assertEqual(self.store.get_raw(write.id).payload["title"], "a")

    def test_context_cleanup_and_closed_operations(self):
        scoped = None
        with self.assertRaises(RuntimeError):
            with Backpack.open(Path(self.temp.name) / "scoped.sqlite") as scoped:
                raise RuntimeError("caller failure")
        scoped.close()
        with self.assertRaises(StoreClosedError):
            scoped.register(DataclassCodec(Note, type_key="notes.note"))
        with self.assertRaises(StoreClosedError):
            scoped.put(Note("a", "b"))
        with Backpack.open(Path(self.temp.name) / "scoped.sqlite"):
            pass

    def test_lock_timeout_is_bounded_and_failure_leaves_no_rows(self):
        with Backpack.open(self.path, timeout=0) as short:
            register(short)
            with closing(sqlite3.connect(self.path)) as blocker, blocker:
                blocker.execute("BEGIN IMMEDIATE")
                with self.assertRaises(StorageError):
                    short.put(Note("blocked", "text"))
            self.assertEqual(short.find(Note).records, ())
        for timeout in (-1, float("inf"), float("nan"), True, 61):
            with self.assertRaises(ValidationError):
                Backpack.open(self.path, timeout=timeout)


if __name__ == "__main__":
    unittest.main()
