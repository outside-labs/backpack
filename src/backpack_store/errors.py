"""Actionable errors that do not include record payloads or configured secrets."""


class BackpackError(Exception):
    """Base error for storage, registration and record validation."""


class ValidationError(BackpackError, ValueError):
    """A value does not satisfy the explicit record or codec contract."""


class RegistrationError(BackpackError):
    """A codec conflicts with an existing registration or is invalid."""


class UnknownTypeError(BackpackError):
    """No trusted codec is registered for a Python type or stable key."""


class UnknownVersionError(BackpackError):
    """The registered codec cannot decode this payload schema version."""


class CodecError(BackpackError):
    """A trusted custom codec failed to encode or decode a value."""


class NotFoundError(BackpackError):
    """The requested local record does not exist."""


class RevisionConflictError(BackpackError):
    """The record changed after the caller's expected revision."""


class StorageError(BackpackError):
    """A database operation failed or stored data is malformed."""


class StoreClosedError(StorageError):
    """The owned database connection has already been closed."""


class DatabaseVersionError(StorageError):
    """This database schema version is not supported."""


class InvalidCursorError(ValidationError):
    """A find cursor is malformed or belongs to a different query."""
