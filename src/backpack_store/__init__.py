"""Experimental typed local records with explicitly registered codecs."""

from .codecs import Codec, CodecRegistry, DataclassCodec, EncodedValue
from .errors import (
    BackpackError,
    CodecError,
    DatabaseVersionError,
    InvalidCursorError,
    NotFoundError,
    RegistrationError,
    RevisionConflictError,
    StorageError,
    StoreClosedError,
    UnknownTypeError,
    UnknownVersionError,
    ValidationError,
)
from .records import JSONObject, JSONValue, Provenance, RawRecord, Record

__all__ = [
    "BackpackError",
    "Codec",
    "CodecError",
    "CodecRegistry",
    "DataclassCodec",
    "DatabaseVersionError",
    "EncodedValue",
    "InvalidCursorError",
    "JSONObject",
    "JSONValue",
    "NotFoundError",
    "Provenance",
    "RawRecord",
    "Record",
    "RegistrationError",
    "RevisionConflictError",
    "StorageError",
    "StoreClosedError",
    "UnknownTypeError",
    "UnknownVersionError",
    "ValidationError",
]
