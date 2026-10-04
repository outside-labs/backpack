import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, RootModel

from backpack_store import (
    Backpack,
    CodecError,
    CodecRegistry,
    RegistrationError,
    UnknownVersionError,
    ValidationError,
)
from backpack_store.pydantic_codec import PydanticCodec


class Details(BaseModel):
    category: str


class Note(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(alias="heading")
    priority: int
    captured_at: datetime
    details: Details


class Different(BaseModel):
    title: str


def note():
    return Note(
        heading="Example",
        priority=2,
        captured_at=datetime(2026, 10, 4, tzinfo=UTC),
        details=Details(category="local"),
    )


class PydanticCodecTests(unittest.TestCase):
    def test_typed_nested_alias_datetime_model_survives_disk_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.sqlite"
            with Backpack.open(path) as store:
                store.register(
                    PydanticCodec(Note, type_key="notes.pydantic", schema_version=1)
                )
                written = store.put(note(), tags=["validated"])
                raw = store.get_raw(written.id)
                self.assertEqual(raw.payload["heading"], "Example")
                self.assertNotIn("title", raw.payload)
                self.assertIsInstance(raw.payload["captured_at"], str)
            with Backpack.open(path) as store:
                store.register(PydanticCodec(Note, type_key="notes.pydantic"))
                restored = store.get(written.id)
                self.assertEqual(restored, note())
                self.assertIs(type(restored), Note)
                self.assertIs(type(restored.details), Details)
                self.assertEqual(
                    store.find(Note, tags=["validated"]).records[0].id, written.id
                )

    def test_invalid_constructed_or_mutated_model_fails_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            with Backpack.open(Path(directory) / "records.sqlite") as store:
                store.register(PydanticCodec(Note, type_key="notes.pydantic"))
                invalid = note()
                invalid.priority = "not an integer"
                with self.assertRaises((ValidationError, CodecError)):
                    store.put(invalid)
                constructed = Note.model_construct(
                    title="Example",
                    priority="invalid",
                    captured_at=datetime(2026, 10, 4, tzinfo=UTC),
                    details=Details(category="local"),
                )
                with self.assertRaises((ValidationError, CodecError)):
                    store.put(constructed)
                self.assertEqual(store.find(Note).records, ())

    def test_strict_json_validation_type_mismatch_and_extra_fields(self):
        codec = PydanticCodec(Note, type_key="notes.pydantic")
        payload = codec.encode(note())
        for invalid in (
            {**payload, "priority": "2"},
            {**payload, "extra": "private"},
            {**payload, "priority": True},
            {**payload, "priority": float("nan")},
        ):
            with self.assertRaises(ValidationError):
                codec.decode(invalid)
        with self.assertRaises(ValidationError):
            codec.encode(Different(title="different"))
        for model in (dict, BaseModel, RootModel[list[str]], note()):
            with self.assertRaises(RegistrationError):
                PydanticCodec(model, type_key="notes.invalid")

    def test_unknown_schema_version_and_record_type_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.sqlite"
            with Backpack.open(path) as store:
                store.register(PydanticCodec(Note, type_key="notes.pydantic"))
                written = store.put(note())
                with closing(sqlite3.connect(path)) as connection, connection:
                    connection.execute(
                        "UPDATE records SET schema_version = 2 WHERE id = ?",
                        (written.id,),
                    )
                with self.assertRaises(UnknownVersionError):
                    store.get(written.id)
                self.assertEqual(store.get_raw(written.id).schema_version, 2)
        registry = CodecRegistry()
        registry.register(PydanticCodec(Note, type_key="notes.pydantic"))
        with self.assertRaises(RegistrationError):
            registry.register(PydanticCodec(Different, type_key="notes.pydantic"))


if __name__ == "__main__":
    unittest.main()
