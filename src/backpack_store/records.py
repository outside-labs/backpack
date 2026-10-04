"""Finite JSON envelopes with local identity and explicit source provenance."""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, TypeAlias, TypeVar, Union
from urllib.parse import urlsplit
from uuid import UUID

from .errors import ValidationError

JSONValue: TypeAlias = Union[  # noqa: UP007 - Python 3.11 recursive aliases need ForwardRef
    None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]
]
JSONObject: TypeAlias = dict[str, JSONValue]
T = TypeVar("T")
MAX_JSON_BYTES = 1_048_576
_TYPE_KEY = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+")


def validate_type_key(value: str) -> str:
    if not isinstance(value, str) or len(value) > 128 or not _TYPE_KEY.fullmatch(value):
        raise ValidationError("type_key must be a dotted lowercase application key")
    return value


def positive_integer(value: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        raise ValidationError(f"{name} must be a positive 32-bit integer")
    return value


def validate_record_id(value: str) -> str:
    try:
        if not isinstance(value, str) or str(UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValidationError("record_id must be a canonical local UUID") from exc
    return value


def utc_now() -> str:
    """UTC wire timestamps always have six fractional digits and end in Z."""
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_timestamp(value: str) -> str:
    try:
        if not isinstance(value, str) or len(value) != 27 or not value.endswith("Z"):
            raise ValueError
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        if parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != value:
            raise ValueError
    except ValueError as exc:
        raise ValidationError(
            "timestamps must be UTC ISO 8601 with six fractional digits and Z"
        ) from exc
    return value


def normalize_tags(tags: Iterable[str]) -> tuple[str, ...]:
    if isinstance(tags, (str, bytes)):
        raise ValidationError("tags must be a collection of strings")
    result: set[str] = set()
    try:
        for index, tag in enumerate(tags):
            if index >= 64:
                raise ValidationError("at most 64 tags may be supplied")
            if not isinstance(tag, str):
                raise ValidationError("each tag must be a string")
            tag = unicodedata.normalize("NFC", tag.strip())
            if not tag or len(tag) > 128 or not tag.isprintable():
                raise ValidationError("tags must contain 1 to 128 printable characters")
            result.add(tag)
    except TypeError as exc:
        raise ValidationError("tags must be a collection of strings") from exc
    return tuple(sorted(result))


def json_object(value: object) -> JSONObject:
    """Validate and detach a finite JSON object; reject executable/ambiguous values."""

    def check(item: object, depth: int) -> None:
        if depth > 64:
            raise ValidationError("JSON nesting exceeds 64 levels")
        if item is None or type(item) in (bool, int, float, str):
            return
        if type(item) is list:
            for child in item:
                check(child, depth + 1)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                check(child, depth + 1)
            return
        raise ValidationError(
            "payload must contain only JSON values and string object keys"
        )

    if type(value) is not dict:
        raise ValidationError("payload must be a JSON object")
    check(value, 0)
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        )
        if len(encoded.encode("utf-8")) > MAX_JSON_BYTES:
            raise ValidationError("JSON object exceeds the 1 MiB limit")
        return json.loads(encoded)
    except (ValueError, OverflowError, UnicodeError, RecursionError) as exc:
        raise ValidationError("payload must be finite UTF-8 JSON") from exc


@dataclass(frozen=True)
class Provenance:
    source_url: str | None = None
    source_system: str | None = None
    source_id: str | None = None
    captured_at: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_system", "source_id"):
            value = getattr(self, name)
            if value is not None and (
                not isinstance(value, str)
                or not value
                or len(value) > 512
                or "\x00" in value
            ):
                raise ValidationError(
                    f"{name} must be a nonempty string of at most 512 characters"
                )
        if self.source_url is not None:
            try:
                if not isinstance(self.source_url, str) or len(self.source_url) > 4096:
                    raise ValueError
                url = urlsplit(self.source_url)
                if (
                    url.scheme not in ("https", "http")
                    or not url.hostname
                    or url.username is not None
                    or url.password is not None
                ):
                    raise ValueError
            except ValueError as exc:
                raise ValidationError(
                    "source_url must be an HTTP(S) URL without credentials"
                ) from exc
        if self.captured_at is not None:
            validate_timestamp(self.captured_at)


@dataclass(frozen=True)
class RawRecord:
    id: str
    type_key: str
    schema_version: int
    revision: int
    payload: JSONObject
    created_at: str
    updated_at: str
    tags: tuple[str, ...]
    provenance: Provenance | None

    def __post_init__(self) -> None:
        validate_record_id(self.id)
        validate_type_key(self.type_key)
        positive_integer(self.schema_version, "schema_version")
        positive_integer(self.revision, "revision")
        validate_timestamp(self.created_at)
        validate_timestamp(self.updated_at)
        if self.updated_at < self.created_at:
            raise ValidationError("updated_at cannot precede created_at")
        if self.provenance is not None and type(self.provenance) is not Provenance:
            raise ValidationError("provenance must be an explicit Provenance value")
        object.__setattr__(self, "payload", json_object(self.payload))
        object.__setattr__(self, "tags", normalize_tags(self.tags))


@dataclass(frozen=True)
class Record(RawRecord, Generic[T]):
    """A stored envelope plus its decoded Python value."""

    value: T
