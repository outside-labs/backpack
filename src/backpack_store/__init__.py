"""Backpack Store: typed local records with explicitly registered codecs."""

from .codecs import Codec, CodecRegistry, DataclassCodec, EncodedValue
from .errors import (
    AlreadyExistsError,
    BackpackError,
    CodecError,
    ConstraintError,
    DatabaseVersionError,
    InvalidCursorError,
    MigrationError,
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
from .store import Backpack, ExportResult, FindPage, WriteResult
from .relational import ApplicationModel, ApplicationRow, ApplicationStore, ForeignKey, Index

__all__ = [
    "AlreadyExistsError",
    "ApplicationModel",
    "ApplicationRow",
    "ApplicationStore",
    "ConstraintError",
    "ForeignKey",
    "Index",
    "Backpack",
    "ExportResult",
    "MigrationError",
    "FindPage",
    "WriteResult",
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
