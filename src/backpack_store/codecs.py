"""Trusted, explicit codecs; no stored name is ever treated as an import path."""

from __future__ import annotations

import math
import types
from dataclasses import dataclass, fields, is_dataclass
from typing import (
    Generic,
    Literal,
    Protocol,
    TypeVar,
    Union,
    get_args,
    get_origin,
    get_type_hints,
)

from .errors import (
    CodecError,
    RegistrationError,
    UnknownTypeError,
    UnknownVersionError,
    ValidationError,
)
from .records import JSONObject, json_object, positive_integer, validate_type_key

T = TypeVar("T")


class Codec(Protocol[T]):
    type_key: str
    schema_version: int
    model_type: type[T]

    def encode(self, value: T) -> JSONObject: ...

    def decode(self, payload: JSONObject) -> T: ...


def _model_fields(model_type: type) -> dict[str, object]:
    hints = get_type_hints(model_type)
    declared = fields(model_type)
    if any(not field.init or field.name not in hints for field in declared):
        raise RegistrationError("dataclass codecs require annotated constructor fields")
    return {field.name: hints[field.name] for field in declared}


def _check_type(annotation: object, stack: tuple[type, ...] = ()) -> None:
    if annotation in (str, bool, int, float, type(None)):
        return
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        if not args or any(type(v) not in (str, bool, int, type(None)) for v in args):
            raise RegistrationError("Literal fields must contain JSON scalar choices")
        return
    if origin in (Union, types.UnionType):
        for arg in args:
            _check_type(arg, stack)
        return
    if origin in (list, tuple) and args:
        for arg in args:
            if arg is not Ellipsis:
                _check_type(arg, stack)
        return
    if origin is dict and len(args) == 2 and args[0] is str:
        _check_type(args[1], stack)
        return
    if isinstance(annotation, type) and is_dataclass(annotation):
        if annotation in stack:
            raise RegistrationError("recursive dataclasses require a custom codec")
        for field_type in _model_fields(annotation).values():
            _check_type(field_type, (*stack, annotation))
        return
    raise RegistrationError("dataclass fields must use concrete supported JSON types")


def _convert(annotation: object, value: object, *, decode: bool, path: str) -> object:
    def invalid() -> ValidationError:
        return ValidationError(f"{path} does not match its declared type")

    if annotation in (str, bool, int, type(None)):
        if type(value) is not annotation:
            raise invalid()
        return value
    if annotation is float:
        if type(value) not in (int, float):
            raise invalid()
        try:
            number = float(value)
        except OverflowError as exc:
            raise invalid() from exc
        if not math.isfinite(number):
            raise invalid()
        return number
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Literal:
        if not any(type(value) is type(option) and value == option for option in args):
            raise invalid()
        return value
    if origin in (Union, types.UnionType):
        for arg in args:
            try:
                return _convert(arg, value, decode=decode, path=path)
            except ValidationError:
                continue
        raise invalid()
    if origin in (list, tuple):
        expected = list if decode or origin is list else tuple
        if type(value) is not expected:
            raise invalid()
        if origin is tuple and not (len(args) == 2 and args[1] is Ellipsis):
            if len(value) != len(args):
                raise invalid()
            element_types = args
        else:
            element_types = (args[0],) * len(value)
        result = [
            _convert(t, v, decode=decode, path=f"{path}[{i}]")
            for i, (t, v) in enumerate(zip(element_types, value, strict=True))
        ]
        return tuple(result) if decode and origin is tuple else result
    if origin is dict:
        if type(value) is not dict or any(type(k) is not str for k in value):
            raise invalid()
        return {
            key: _convert(args[1], child, decode=decode, path=f"{path}.*")
            for key, child in value.items()
        }
    if isinstance(annotation, type) and is_dataclass(annotation):
        model_fields = _model_fields(annotation)
        if decode:
            if type(value) is not dict or set(value) != set(model_fields):
                raise invalid()
            kwargs = {
                name: _convert(t, value[name], decode=True, path=f"{path}.{name}")
                for name, t in model_fields.items()
            }
            try:
                return annotation(**kwargs)
            except Exception as exc:  # noqa: BLE001 - trusted constructors are an explicit codec boundary
                raise CodecError("dataclass construction failed") from exc
        if type(value) is not annotation:
            raise invalid()
        return {
            name: _convert(t, getattr(value, name), decode=False, path=f"{path}.{name}")
            for name, t in model_fields.items()
        }
    raise invalid()


class DataclassCodec(Generic[T]):
    """Finite JSON for concrete dataclasses, nested dataclasses and typed containers."""

    def __init__(self, model_type: type[T], *, type_key: str, schema_version: int = 1):
        if not isinstance(model_type, type) or not is_dataclass(model_type):
            raise RegistrationError("model_type must be a dataclass class")
        validate_type_key(type_key)
        positive_integer(schema_version, "schema_version")
        try:
            _check_type(model_type)
        except (NameError, TypeError) as exc:
            raise RegistrationError(
                "dataclass annotations could not be resolved"
            ) from exc
        self.model_type = model_type
        self.type_key = type_key
        self.schema_version = schema_version

    def encode(self, value: T) -> JSONObject:
        return json_object(
            _convert(self.model_type, value, decode=False, path="payload")
        )

    def decode(self, payload: JSONObject) -> T:
        return _convert(
            self.model_type, json_object(payload), decode=True, path="payload"
        )


@dataclass(frozen=True)
class EncodedValue:
    type_key: str
    schema_version: int
    payload: JSONObject


class CodecRegistry:
    def __init__(self) -> None:
        self._by_key: dict[str, Codec] = {}
        self._by_type: dict[type, Codec] = {}

    def register(self, codec: Codec[T]) -> None:
        try:
            validate_type_key(codec.type_key)
            positive_integer(codec.schema_version, "schema_version")
            if (
                not isinstance(codec.model_type, type)
                or not callable(codec.encode)
                or not callable(codec.decode)
            ):
                raise RegistrationError(
                    "codec requires a model class and encode/decode methods"
                )
        except (AttributeError, ValidationError) as exc:
            raise RegistrationError("codec identity is invalid") from exc
        if codec.type_key in self._by_key or codec.model_type in self._by_type:
            raise RegistrationError(
                "codec type key or model type is already registered"
            )
        self._by_key[codec.type_key] = codec
        self._by_type[codec.model_type] = codec

    @property
    def registered_keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_key))

    def for_key(self, type_key: str) -> Codec:
        validate_type_key(type_key)
        try:
            return self._by_key[type_key]
        except KeyError as exc:
            raise UnknownTypeError(
                "register a trusted codec for the stored type_key"
            ) from exc

    def for_type(self, model_type: type[T]) -> Codec[T]:
        try:
            return self._by_type[model_type]
        except (KeyError, TypeError) as exc:
            raise UnknownTypeError(
                "register a trusted codec for this exact Python type"
            ) from exc

    def encode(self, value: T) -> EncodedValue:
        codec = self.for_type(type(value))
        try:
            payload = json_object(codec.encode(value))
        except ValidationError:
            raise
        except Exception as exc:  # noqa: BLE001 - custom codec messages may contain record data
            raise CodecError("registered codec failed to encode the value") from exc
        return EncodedValue(codec.type_key, codec.schema_version, payload)

    def decode(self, type_key: str, schema_version: int, payload: JSONObject) -> object:
        codec = self.for_key(type_key)
        positive_integer(schema_version, "schema_version")
        if schema_version != codec.schema_version:
            raise UnknownVersionError(
                "register an explicit migration for this payload schema version"
            )
        try:
            value = codec.decode(json_object(payload))
        except (ValidationError, CodecError):
            raise
        except Exception as exc:  # noqa: BLE001 - custom codec messages may contain record data
            raise CodecError("registered codec failed to decode the payload") from exc
        if type(value) is not codec.model_type:
            raise CodecError("registered codec returned a different Python type")
        return value
