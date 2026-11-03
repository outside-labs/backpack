from dataclasses import dataclass, field
import importlib.metadata
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

from backpack_store import Backpack, CodecError, DataclassCodec, RegistrationError, UnknownVersionError, ValidationError
from backpack_store.enforce_type_codec import EnforceTypeCodec


@dataclass
class Label:
    text: str


@dataclass
class Note:
    title: str
    labels: list[Label] = field(default_factory=list)
    done: bool = False


@dataclass
class NativeFields:
    identity: UUID
    position: tuple[float, float]


@dataclass
class Unsupported:
    values: set[int]


class CodecTests(unittest.TestCase):
    def setUp(self):
        self.codec = EnforceTypeCodec(Note, type_key="notes.note", schema_version=2)

    def test_published_profile_is_explicit(self):
        self.assertTrue(importlib.metadata.version("enforce-type").startswith("0.1."))

    def test_compiles_once_and_uses_check_load_dump(self):
        from backpack_store import enforce_type_codec as module
        with patch.object(module, "compile_type", wraps=module.compile_type) as compile_model:
            codec = EnforceTypeCodec(Note, type_key="notes.note")
            for _ in range(2):
                self.assertEqual(codec.decode(codec.encode(Note("Garden"))), Note("Garden"))
            compile_model.assert_called_once_with(Note)

    def test_nested_defaults_and_detached_round_trip(self):
        payload = {"title": "Garden", "labels": [{"text": "café"}]}
        note = self.codec.decode(payload)
        payload["labels"][0]["text"] = "changed"
        self.assertEqual(note, Note("Garden", [Label("café")]))
        encoded = self.codec.encode(note)
        encoded["labels"][0]["text"] = "changed again"
        self.assertEqual(note.labels[0].text, "café")
        self.assertEqual(self.codec.decode({"title": "Defaults"}), Note("Defaults"))
        first = self.codec.decode({"title": "One"})
        second = self.codec.decode({"title": "Two"})
        first.labels.append(Label("only one"))
        self.assertEqual(second.labels, [])

    def test_json_and_native_conversions_use_published_plan_rules(self):
        codec = EnforceTypeCodec(NativeFields, type_key="notes.native")
        payload = {"identity": "00000000-0000-0000-0000-000000000001", "position": [1, 2.5]}
        value = codec.decode(payload)
        self.assertEqual(value.position, (1.0, 2.5))
        self.assertIsInstance(value.identity, UUID)
        self.assertEqual(codec.encode(value), {**payload, "position": [1.0, 2.5]})
        with self.assertRaises(ValidationError):
            codec.encode(NativeFields(value.identity, (1, 2.5)))

    def test_unknown_missing_wrong_and_nonfinite_payloads_are_value_free(self):
        secret = "private submitted value"
        for payload in ({}, {"title": "Garden", "extra": secret}, {"title": secret, "done": 1},
                        {"title": "Garden", "labels": [{"text": 9}]}, {"title": float("nan")},
                        {"title": "Garden", "labels": ()}, {"title": "\ud800"}):
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError) as caught:
                    self.codec.decode(payload)
                self.assertNotIn(secret, str(caught.exception))
                self.assertIsNone(caught.exception.__cause__)
                self.assertTrue(caught.exception.__suppress_context__)

    def test_mutated_native_model_is_checked_before_dump(self):
        value = Note("Garden")
        value.done = "private submitted value"
        with self.assertRaises(ValidationError) as caught:
            self.codec.encode(value)
        self.assertNotIn(value.done, str(caught.exception))

    def test_invalid_defaults_are_validated_after_construction(self):
        @dataclass
        class BadDefault:
            count: int = "private default"
        codec = EnforceTypeCodec(BadDefault, type_key="notes.default")
        with self.assertRaises(ValidationError) as caught:
            codec.decode({})
        self.assertNotIn("private default", str(caught.exception))

    def test_unexpected_plan_failures_are_value_free_codec_errors(self):
        class Broken:
            def check(self, value):
                raise RuntimeError("private native value")
            def load(self, payload, **kwargs):
                raise RuntimeError("private decoded value")
        with patch("backpack_store.enforce_type_codec.compile_type", return_value=Broken()):
            codec = EnforceTypeCodec(Note, type_key="notes.broken")
        for operation in (lambda: codec.encode(Note("private native value")),
                          lambda: codec.decode({"title": "private decoded value"})):
            with self.assertRaises(CodecError) as caught:
                operation()
            self.assertNotIn("private", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_exact_root_type_is_required(self):
        @dataclass
        class Child(Note):
            extra: str = "private"
        for value in ({"title": "Garden"}, Child("Garden")):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.codec.encode(value)

    def test_json_object_byte_bound_is_preserved(self):
        with self.assertRaises(ValidationError):
            self.codec.encode(Note("x" * 1048576))

    def test_registration_refuses_unsupported_models_without_values(self):
        for model in (str, Note("not a class"), Unsupported):
            with self.subTest(model=model), self.assertRaises(RegistrationError):
                EnforceTypeCodec(model, type_key="notes.invalid")
        with patch("backpack_store.enforce_type_codec.compile_type", side_effect=RuntimeError("private schema data")):
            with self.assertRaises(RegistrationError) as caught:
                EnforceTypeCodec(Note, type_key="notes.invalid")
            self.assertNotIn("private schema data", str(caught.exception))
            self.assertIsNone(caught.exception.__cause__)

    def test_reopen_versions_and_existing_record_codecs_remain_explicit(self):
        @dataclass
        class Legacy:
            number: int
        with TemporaryDirectory() as directory:
            path = Path(directory) / "records.sqlite"
            with Backpack.open(path) as store:
                store.register(self.codec)
                store.register(DataclassCodec(Legacy, type_key="notes.legacy"))
                note_id = store.put(Note("Garden", [Label("café")])).id
                legacy_id = store.put(Legacy(42)).id
                record = store.get_record(note_id)
                self.assertEqual((record.type_key, record.schema_version), ("notes.note", 2))
            with Backpack.open(path) as store:
                store.register(EnforceTypeCodec(Note, type_key="notes.note", schema_version=1))
                with self.assertRaises(UnknownVersionError):
                    store.get(note_id)
            with Backpack.open(path) as store:
                store.register(self.codec)
                store.register(DataclassCodec(Legacy, type_key="notes.legacy"))
                self.assertEqual(store.get(note_id), Note("Garden", [Label("café")]))
                self.assertEqual(store.get(legacy_id), Legacy(42))

    def test_base_import_does_not_import_the_optional_validator(self):
        code = "import sys,backpack_store; assert 'enforce_type' not in sys.modules"
        subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True)

    def test_missing_extra_has_an_explicit_import_error(self):
        code = '''import sys
class Refuse:
 def find_spec(self, fullname, *args):
  if fullname == 'enforce_type': raise ModuleNotFoundError('blocked optional dependency')
sys.meta_path.insert(0, Refuse())
try:
 import backpack_store.enforce_type_codec
except ImportError as error:
 assert '[enforce-type]' in str(error), str(error)
else:
 raise AssertionError('optional import unexpectedly succeeded')
'''
        subprocess.run([sys.executable, "-I", "-c", code], check=True, capture_output=True)
