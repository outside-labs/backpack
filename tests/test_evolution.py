from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from test_codecs import Note

from backpack_store import (
    Backpack,
    DatabaseVersionError,
    DataclassCodec,
    InvalidCursorError,
    MigrationError,
    Provenance,
    RegistrationError,
    StorageError,
    UnknownVersionError,
    ValidationError,
)


@dataclass(frozen=True)
class NoteV2:
    title: str
    text: str
    starred: bool


def upgrade_note(payload):
    return {
        "title": payload["title"],
        "text": payload["body"],
        "starred": payload["stars"] > 0,
    }


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / "records.sqlite"

    def seed_v1(self, note=None):
        with Backpack.open(self.path) as store:
            store.register(DataclassCodec(Note, type_key="notes.note"))
            write = store.put(
                note or Note("café", "original text", 1),
                tags=["saved"],
                provenance=Provenance(source_system="fixture", source_id="source-1"),
            )
            return write, store.get_raw(write.id)

    def test_payload_migration_is_explicit_and_read_only(self):
        write, before = self.seed_v1()
        with Backpack.open(self.path) as store:
            store.register(
                DataclassCodec(NoteV2, type_key="notes.note", schema_version=2)
            )
            with self.assertRaises(UnknownVersionError):
                store.get(write.id)
            store.register_migration(
                "notes.note", from_version=1, to_version=2, migrate=upgrade_note
            )
            self.assertEqual(store.get(write.id), NoteV2("café", "original text", True))
            self.assertEqual(store.get_raw(write.id), before)
            record = store.get_record(write.id)
            self.assertEqual(record.schema_version, 1)
            self.assertIs(type(record.value), NoteV2)
            output = self.root / "old-record.jsonl"
            store.export_jsonl(output)
            exported = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(exported["schema_version"], 1)
            self.assertEqual(exported["payload"], before.payload)
            self.assertEqual(store.get_raw(write.id), before)
            updated = store.update(
                write.id, NoteV2("v2", "new text", False), expected_revision=1
            )
            self.assertEqual(updated.revision, 2)
            self.assertEqual(store.get_raw(write.id).schema_version, 2)

    def test_migration_validation_and_failure_do_not_mutate_records(self):
        write, before = self.seed_v1()
        with Backpack.open(self.path) as store:
            store.register(
                DataclassCodec(NoteV2, type_key="notes.note", schema_version=2)
            )
            for start, end in ((1, 3), (2, 1), (0, 1), (True, 2), (2, 3)):
                with self.assertRaises(RegistrationError):
                    store.register_migration(
                        "notes.note",
                        from_version=start,
                        to_version=end,
                        migrate=upgrade_note,
                    )

            def broken(payload):
                payload["body"] = "mutated input"
                raise RuntimeError("private record text")

            store.register_migration(
                "notes.note", from_version=1, to_version=2, migrate=broken
            )
            with self.assertRaises(RegistrationError):
                store.register_migration(
                    "notes.note", from_version=1, to_version=2, migrate=upgrade_note
                )
            with self.assertRaises(MigrationError) as error:
                store.get(write.id)
            self.assertNotIn("private record text", str(error.exception))
            self.assertEqual(store.get_raw(write.id), before)

    def test_invalid_migrated_json_is_rejected(self):
        write, before = self.seed_v1()
        with Backpack.open(self.path) as store:
            store.register(
                DataclassCodec(NoteV2, type_key="notes.note", schema_version=2)
            )
            store.register_migration(
                "notes.note",
                from_version=1,
                to_version=2,
                migrate=lambda _: {"number": float("nan")},
            )
            with self.assertRaises(MigrationError):
                store.get(write.id)
            self.assertEqual(store.get_raw(write.id), before)

    def test_database_versions_preserve_existing_data_and_release_locks(self):
        for name, version in (("future.sqlite", 42), ("unversioned.sqlite", 0)):
            path = self.root / name
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("CREATE TABLE important_data(value TEXT)")
                connection.execute("INSERT INTO important_data VALUES ('preserve')")
                connection.execute(f"PRAGMA user_version = {version}")
            with self.assertRaises(DatabaseVersionError):
                Backpack.open(path)
            with closing(sqlite3.connect(path, timeout=0)) as connection, connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], version
                )
                self.assertEqual(
                    connection.execute("SELECT value FROM important_data").fetchone()[
                        0
                    ],
                    "preserve",
                )
                connection.execute("BEGIN IMMEDIATE")

    def test_bounded_export_pages_preserve_raw_envelopes_without_codecs(self):
        expected = []
        with Backpack.open(self.path) as store:
            store.register(DataclassCodec(Note, type_key="notes.note"))
            for index in range(5):
                expected.append(store.put(Note(str(index), "café"), tags=["notes"]).id)
        with closing(sqlite3.connect(self.path)) as connection, connection:
            connection.execute("UPDATE records SET schema_version = 7")
        found, cursor, index = [], None, 0
        with Backpack.open(self.path) as store:
            self.assertEqual(store.registered_keys, ())
            while True:
                target = self.root / f"page-{index}.jsonl"
                result = store.export_jsonl(target, limit=2, cursor=cursor)
                lines = [
                    json.loads(line)
                    for line in target.read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(result.count, len(lines))
                self.assertLessEqual(result.count, 2)
                self.assertEqual(result.bytes_written, len(target.read_bytes()))
                for record in lines:
                    self.assertEqual(record["format_version"], 1)
                    self.assertEqual(record["schema_version"], 7)
                    self.assertEqual(record["payload"]["body"], "café")
                    self.assertEqual(record["tags"], ["notes"])
                    found.append(record["id"])
                cursor, index = result.next_cursor, index + 1
                if cursor is None:
                    break
        self.assertEqual(found, sorted(expected))

    def test_export_continues_when_byte_budget_fills_before_record_limit(self):
        with Backpack.open(self.path) as store:
            store.register(DataclassCodec(Note, type_key="notes.note"))
            expected = sorted(store.put(Note(str(i), "x" * 600)).id for i in range(3))
            first = store.export_jsonl(
                self.root / "first.jsonl", limit=100, max_bytes=1500
            )
            self.assertEqual(first.count, 1)
            self.assertIsNotNone(first.next_cursor)
            self.assertLessEqual(first.bytes_written, 1500)
            second = store.export_jsonl(
                self.root / "second.jsonl", cursor=first.next_cursor
            )
            self.assertEqual(second.count, 2)
            self.assertIsNone(second.next_cursor)
            actual = []
            for name in ("first.jsonl", "second.jsonl"):
                actual.extend(
                    json.loads(line)["id"]
                    for line in (self.root / name).read_text(encoding="utf-8").splitlines()
                )
            self.assertEqual(actual, expected)

    def test_export_byte_budget_existing_destination_and_query_scope(self):
        self.seed_v1(Note("a", "x" * 1500))
        with Backpack.open(self.path) as store:
            too_small = self.root / "too-small.jsonl"
            with self.assertRaises(ValidationError):
                store.export_jsonl(too_small, max_bytes=1024)
            self.assertFalse(too_small.exists())
            target = self.root / "existing.jsonl"
            target.write_text("preserve")
            with self.assertRaises(StorageError):
                store.export_jsonl(target)
            self.assertEqual(target.read_text(encoding="utf-8"), "preserve")
            with self.assertRaises(StorageError):
                store.export_jsonl(self.root / "missing" / "file.jsonl")
            self.assertEqual(list(self.root.glob(".backpack-export-*")), [])
            store.register(DataclassCodec(Note, type_key="notes.note"))
            store.put(Note("b", "text"))
            cursor = store.find(Note, limit=1).next_cursor
            with self.assertRaises(InvalidCursorError):
                store.export_jsonl(self.root / "wrong-cursor.jsonl", cursor=cursor)


if __name__ == "__main__":
    unittest.main()
