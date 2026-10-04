from __future__ import annotations

import unittest
from dataclasses import dataclass, replace
from typing import Any, Literal

from backpack_store import (
    CodecError,
    CodecRegistry,
    DataclassCodec,
    Provenance,
    RawRecord,
    RegistrationError,
    UnknownTypeError,
    UnknownVersionError,
    ValidationError,
)
from backpack_store.records import json_object, normalize_tags


@dataclass(frozen=True)
class Note:
    title: str
    body: str
    stars: int = 0


@dataclass(frozen=True)
class Field:
    name: str
    value: str | float | None


@dataclass(frozen=True)
class ProjectItem:
    project_id: str
    item_id: str
    content_id: str | None
    kind: Literal["issue", "pull_request", "draft", "redacted"]
    fields: tuple[Field, ...]


@dataclass
class Recursive:
    child: Recursive | None


@dataclass
class Untyped:
    value: Any


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.registry = CodecRegistry()
        self.note = DataclassCodec(Note, type_key="notes.note")
        self.registry.register(self.note)

    def test_note_and_nested_project_fixture_round_trip(self):
        note = Note("Example", "Local text", 2)
        encoded = self.registry.encode(note)
        self.assertEqual(
            self.registry.decode(
                encoded.type_key, encoded.schema_version, encoded.payload
            ),
            note,
        )
        codec = DataclassCodec(ProjectItem, type_key="github.project_item")
        self.registry.register(codec)
        item = ProjectItem(
            "PVT_fixture",
            "PVTI_fixture",
            "I_fixture",
            "issue",
            (Field("Status", "Todo"), Field("Size", 3.5)),
        )
        encoded = self.registry.encode(item)
        self.assertIsInstance(encoded.payload["fields"], list)
        restored = self.registry.decode(encoded.type_key, 1, encoded.payload)
        self.assertEqual(restored, item)
        self.assertIs(type(restored.fields[0]), Field)
        self.assertEqual(restored.item_id, "PVTI_fixture")

    def test_declared_types_and_fields_are_validated(self):
        for value in (Note("a", "b", True), Note("a", "b", "2")):
            with self.assertRaises(ValidationError):
                self.registry.encode(value)
        for payload in (
            {"title": "a"},
            {"title": "a", "body": "b", "stars": 0, "extra": 1},
        ):
            with self.assertRaises(ValidationError):
                self.note.decode(payload)
        project = DataclassCodec(ProjectItem, type_key="github.project_item")
        with self.assertRaises(ValidationError):
            project.encode(ProjectItem("p", "i", None, "invalid", ()))

    def test_unknown_type_version_and_atomic_collisions(self):
        with self.assertRaises(UnknownTypeError):
            self.registry.encode({"title": "a"})
        with self.assertRaises(UnknownTypeError):
            self.registry.decode("untrusted.module", 1, {})
        with self.assertRaises(UnknownVersionError):
            self.registry.decode("notes.note", 2, {})
        with self.assertRaises(RegistrationError):
            self.registry.register(DataclassCodec(Note, type_key="other.note"))
        with self.assertRaises(RegistrationError):
            self.registry.register(DataclassCodec(ProjectItem, type_key="notes.note"))
        self.assertEqual(self.registry.registered_keys, ("notes.note",))
        self.assertEqual(self.registry.encode(Note("a", "b")).type_key, "notes.note")

    def test_untrusted_or_ambiguous_annotations_fail_at_registration(self):
        for model in (Recursive, Untyped, dict, Note("a", "b")):
            with self.assertRaises(RegistrationError):
                DataclassCodec(model, type_key="test.model")
        for key in ("Note", "notes", "notes..note", "notes.note;import"):
            with self.assertRaises(ValidationError):
                DataclassCodec(Note, type_key=key)
        with self.assertRaises(ValidationError):
            DataclassCodec(Note, type_key="notes.note", schema_version=True)

    def test_json_is_finite_bounded_and_detached(self):
        for value in (
            {"number": float("nan")},
            {"number": float("inf")},
            {1: "key"},
            {"x": object()},
            {"x": (1, 2)},
            {"large": "x" * 1_048_576},
        ):
            with self.assertRaises(ValidationError):
                json_object(value)
        cycle = {}
        cycle["self"] = cycle
        with self.assertRaises(ValidationError):
            json_object(cycle)
        original = {"nested": ["a"]}
        result = json_object(original)
        original["nested"].append("b")
        self.assertEqual(result, {"nested": ["a"]})

    def test_custom_codec_failure_and_wrong_return_type(self):
        class Broken:
            type_key, schema_version, model_type = "test.broken", 1, Field

            def encode(self, value):
                raise RuntimeError("private text")

            def decode(self, payload):
                return "wrong type"

        self.registry.register(Broken())
        with self.assertRaises(CodecError) as error:
            self.registry.encode(Field("a", "b"))
        self.assertNotIn("private text", str(error.exception))
        with self.assertRaises(CodecError):
            self.registry.decode("test.broken", 1, {})

    def test_tags_are_exact_normalized_and_bounded(self):
        self.assertEqual(
            normalize_tags([" café ", "cafe\u0301", "Work", "work"]),
            ("Work", "café", "work"),
        )
        for tags in ("tag", [""], ["a\x00"], [1], ["a"] * 65):
            with self.assertRaises(ValidationError):
                normalize_tags(tags)

    def test_provenance_and_local_envelope_identity(self):
        provenance = Provenance(
            "https://github.com/orgs/example/projects/1",
            "github",
            "PVTI_fixture",
            "2026-10-04T00:00:00.000000Z",
        )
        record = RawRecord(
            "12345678-1234-4234-8234-123456789abc",
            "notes.note",
            1,
            1,
            {"title": "a"},
            "2026-10-04T00:00:00.000000Z",
            "2026-10-04T00:00:00.000000Z",
            (" work ",),
            provenance,
        )
        self.assertEqual(record.tags, ("work",))
        self.assertNotEqual(record.id, record.provenance.source_id)
        for change in (
            {"id": "PVTI_fixture"},
            {"schema_version": 0},
            {"revision": True},
            {"created_at": "2026-10-04T00:00:00Z"},
            {"updated_at": "2026-10-03T00:00:00.000000Z"},
        ):
            with self.assertRaises(ValidationError):
                replace(record, **change)
        for url in (
            "file:///tmp/records",
            "https://user:secret@example.com",
            "not a url",
        ):
            with self.assertRaises(ValidationError):
                Provenance(source_url=url)


if __name__ == "__main__":
    unittest.main()
