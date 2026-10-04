"""Explicit, transactional SQLite persistence with bounded typed queries."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import sqlite3
import tempfile
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Generic, TypeVar
from uuid import uuid4

from .codecs import Codec, CodecRegistry, EncodedValue
from .errors import (
    DatabaseVersionError,
    InvalidCursorError,
    NotFoundError,
    RevisionConflictError,
    StorageError,
    StoreClosedError,
    ValidationError,
)
from .records import (
    JSONObject,
    Provenance,
    RawRecord,
    Record,
    json_object,
    normalize_tags,
    positive_integer,
    utc_now,
    validate_record_id,
)

T = TypeVar("T")
DATABASE_VERSION = 1
_SCHEMA = (
    """CREATE TABLE records (
        id TEXT PRIMARY KEY, type_key TEXT NOT NULL,
        schema_version INTEGER NOT NULL CHECK (schema_version > 0),
        revision INTEGER NOT NULL CHECK (revision > 0), payload TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, provenance TEXT
    )""",
    """CREATE TABLE tags (
        record_id TEXT NOT NULL REFERENCES records(id) ON DELETE CASCADE,
        tag TEXT NOT NULL, PRIMARY KEY (record_id, tag)
    )""",
    "CREATE INDEX records_type_id ON records(type_key, id)",
    "CREATE INDEX tags_tag_record ON tags(tag, record_id)",
)


@dataclass(frozen=True)
class WriteResult:
    id: str
    revision: int


@dataclass(frozen=True)
class FindPage(Generic[T]):
    records: tuple[Record[T], ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ExportResult:
    count: int
    next_cursor: str | None
    bytes_written: int


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))


def _cursor(type_key: str, tags: tuple[str, ...], last_id: str) -> str:
    payload = {"version": 1, "type_key": type_key, "tags": tags, "last_id": last_id}
    return base64.urlsafe_b64encode(_dump(payload).encode()).decode("ascii")


def _read_cursor(cursor: str, type_key: str, tags: tuple[str, ...]) -> str:
    try:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 32_768:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            type(value) is not dict
            or set(value) != {"version", "type_key", "tags", "last_id"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or value["type_key"] != type_key
            or value["tags"] != list(tags)
        ):
            raise ValueError
        return validate_record_id(value["last_id"])
    except (ValueError, TypeError, UnicodeError, binascii.Error, RecursionError) as exc:
        raise InvalidCursorError(
            "cursor must be valid and match the requested type and tags"
        ) from exc


class Backpack:
    """One owned connection. Use on its opening thread and close explicitly."""

    def __init__(self, connection: sqlite3.Connection):
        self._connection: sqlite3.Connection | None = connection
        self._registry = CodecRegistry()

    @classmethod
    def open(cls, path: str | os.PathLike[str], *, timeout: float = 5.0) -> Backpack:
        if (
            type(timeout) not in (int, float)
            or not math.isfinite(timeout)
            or not 0 <= timeout <= 60
        ):
            raise ValidationError("timeout must be finite and between 0 and 60 seconds")
        try:
            filename = os.fspath(path)
        except TypeError as exc:
            raise ValidationError("an explicit database path is required") from exc
        if not isinstance(filename, str) or not filename or "\x00" in filename:
            raise ValidationError("an explicit nonempty database path is required")
        connection = None
        try:
            connection = sqlite3.connect(filename, timeout=timeout)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version == 0:
                    existing = connection.execute(
                        "SELECT name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
                    ).fetchone()
                    if existing:
                        raise DatabaseVersionError(
                            "unversioned nonempty database requires an explicit migration"
                        )
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute(f"PRAGMA user_version = {DATABASE_VERSION}")
                elif version != DATABASE_VERSION:
                    raise DatabaseVersionError(
                        "database schema version is unsupported; preserve it for migration"
                    )
            return cls(connection)
        except (sqlite3.Error, DatabaseVersionError) as exc:
            if connection is not None:
                connection.close()
            if isinstance(exc, DatabaseVersionError):
                raise
            raise StorageError(
                "could not open or initialize the explicit SQLite database"
            ) from None

    def __repr__(self) -> str:
        return f"Backpack({'closed' if self._connection is None else 'open'})"

    def _live_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StoreClosedError(
                "the store is closed; explicitly open a new instance"
            )
        return self._connection

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self._live_connection()
        try:
            with connection:
                connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                yield connection
        except sqlite3.Error:
            raise StorageError(
                "SQLite operation failed; the transaction was rolled back"
            ) from None

    def register(self, codec: Codec[T]) -> None:
        self._live_connection()
        self._registry.register(codec)

    @property
    def registered_keys(self) -> tuple[str, ...]:
        self._live_connection()
        return self._registry.registered_keys

    def codec_for_key(self, type_key: str) -> Codec:
        self._live_connection()
        return self._registry.for_key(type_key)

    @staticmethod
    def _new_record(
        encoded: EncodedValue,
        *,
        record_id: str,
        revision: int,
        created_at: str,
        updated_at: str,
        tags: Iterable[str],
        provenance: Provenance | None,
    ) -> RawRecord:
        return RawRecord(
            record_id,
            encoded.type_key,
            encoded.schema_version,
            revision,
            encoded.payload,
            created_at,
            updated_at,
            normalize_tags(tags),
            provenance,
        )

    @staticmethod
    def _write_tags(connection: sqlite3.Connection, record: RawRecord) -> None:
        connection.executemany(
            "INSERT INTO tags(record_id, tag) VALUES (?, ?)",
            ((record.id, tag) for tag in record.tags),
        )

    def put(
        self,
        value: T,
        *,
        tags: Iterable[str] = (),
        provenance: Provenance | None = None,
    ) -> WriteResult:
        self._live_connection()
        encoded = self._registry.encode(value)
        now = utc_now()
        record = self._new_record(
            encoded,
            record_id=str(uuid4()),
            revision=1,
            created_at=now,
            updated_at=now,
            tags=tags,
            provenance=provenance,
        )
        with self._transaction(write=True) as connection:
            connection.execute(
                """INSERT INTO records
                (id, type_key, schema_version, revision, payload, created_at, updated_at, provenance)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    record.id,
                    record.type_key,
                    record.schema_version,
                    record.revision,
                    _dump(record.payload),
                    record.created_at,
                    record.updated_at,
                    None if provenance is None else _dump(asdict(provenance)),
                ),
            )
            self._write_tags(connection, record)
        return WriteResult(record.id, record.revision)

    def _raw_from_row(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> RawRecord:
        try:
            provenance = (
                None
                if row["provenance"] is None
                else Provenance(**json.loads(row["provenance"]))
            )
            tags = tuple(
                tag[0]
                for tag in connection.execute(
                    "SELECT tag FROM tags WHERE record_id = ? ORDER BY tag",
                    (row["id"],),
                )
            )
            return RawRecord(
                row["id"],
                row["type_key"],
                row["schema_version"],
                row["revision"],
                json_object(json.loads(row["payload"])),
                row["created_at"],
                row["updated_at"],
                tags,
                provenance,
            )
        except (ValueError, TypeError, ValidationError, RecursionError):
            raise StorageError(
                "stored record envelope is malformed; inspect a recoverable database copy"
            ) from None

    def _read_raw(self, connection: sqlite3.Connection, record_id: str) -> RawRecord:
        row = connection.execute(
            "SELECT * FROM records WHERE id = ?", (record_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("the local record does not exist")
        return self._raw_from_row(connection, row)

    def get_raw(self, record_id: str) -> RawRecord:
        """Recover JSON and metadata without requiring a known codec/version."""
        validate_record_id(record_id)
        with self._transaction() as connection:
            return self._read_raw(connection, record_id)

    def _decode_record(self, record: RawRecord) -> Record:
        value = self._registry.decode(
            record.type_key, record.schema_version, record.payload
        )
        return Record(
            record.id,
            record.type_key,
            record.schema_version,
            record.revision,
            record.payload,
            record.created_at,
            record.updated_at,
            record.tags,
            record.provenance,
            value,
        )

    def get_record(self, record_id: str) -> Record:
        return self._decode_record(self.get_raw(record_id))

    def get(self, record_id: str) -> object:
        return self.get_record(record_id).value

    def update(
        self,
        record_id: str,
        value: T,
        *,
        expected_revision: int,
        tags: Iterable[str] = (),
        provenance: Provenance | None = None,
    ) -> WriteResult:
        """Replace payload, tags and provenance together at the expected revision."""
        validate_record_id(record_id)
        positive_integer(expected_revision, "expected_revision")
        self._live_connection()
        encoded = self._registry.encode(value)
        tags = normalize_tags(tags)
        with self._transaction(write=True) as connection:
            current = self._read_raw(connection, record_id)
            if current.revision != expected_revision:
                raise RevisionConflictError(
                    "record revision changed; read it again before updating"
                )
            if current.type_key != encoded.type_key:
                raise ValidationError("an update must preserve the record's type_key")
            record = self._new_record(
                encoded,
                record_id=record_id,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=max(utc_now(), current.updated_at),
                tags=tags,
                provenance=provenance,
            )
            connection.execute(
                """UPDATE records SET schema_version = ?, revision = ?,
                payload = ?, updated_at = ?, provenance = ? WHERE id = ? AND revision = ?""",
                (
                    record.schema_version,
                    record.revision,
                    _dump(record.payload),
                    record.updated_at,
                    None if provenance is None else _dump(asdict(provenance)),
                    record_id,
                    expected_revision,
                ),
            )
            connection.execute("DELETE FROM tags WHERE record_id = ?", (record_id,))
            self._write_tags(connection, record)
        return WriteResult(record.id, record.revision)

    def find(
        self,
        model_type: type[T],
        *,
        tags: Iterable[str] = (),
        limit: int = 50,
        cursor: str | None = None,
    ) -> FindPage[T]:
        self._live_connection()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError("limit must be an integer between 1 and 1000")
        type_key = self._registry.for_type(model_type).type_key
        tags = normalize_tags(tags)
        last_id = "" if cursor is None else _read_cursor(cursor, type_key, tags)
        sql = "SELECT * FROM records WHERE type_key = ? AND id > ?"
        parameters: list[object] = [type_key, last_id]
        if tags:
            placeholders = ",".join("?" for _ in tags)
            sql += f" AND id IN (SELECT record_id FROM tags WHERE tag IN ({placeholders}) GROUP BY record_id HAVING COUNT(*) = ?)"
            parameters.extend((*tags, len(tags)))
        sql += " ORDER BY id LIMIT ?"
        parameters.append(limit + 1)
        with self._transaction() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            records = tuple(self._raw_from_row(connection, row) for row in rows[:limit])
        next_cursor = (
            _cursor(type_key, tags, records[-1].id) if len(rows) > limit else None
        )
        return FindPage(
            tuple(self._decode_record(record) for record in records), next_cursor
        )

    def delete(self, record_id: str) -> bool:
        validate_record_id(record_id)
        with self._transaction(write=True) as connection:
            return (
                connection.execute(
                    "DELETE FROM records WHERE id = ?", (record_id,)
                ).rowcount
                == 1
            )

    def register_migration(
        self,
        type_key: str,
        *,
        from_version: int,
        to_version: int,
        migrate: Callable[[JSONObject], JSONObject],
    ) -> None:
        """Register a trusted one-version payload upgrade; never rewrite on read."""
        self._live_connection()
        self._registry.register_migration(
            type_key, from_version=from_version, to_version=to_version, migrate=migrate
        )

    def export_jsonl(
        self,
        destination: str | os.PathLike[str],
        *,
        limit: int = 1000,
        cursor: str | None = None,
        max_bytes: int = 4_194_304,
    ) -> ExportResult:
        """Export one raw-record page to a new explicit file without decoding."""
        self._live_connection()
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError("limit must be an integer between 1 and 1000")
        if type(max_bytes) is not int or not 1024 <= max_bytes <= 67_108_864:
            raise ValidationError("max_bytes must be between 1 KiB and 64 MiB")
        try:
            target = Path(destination)
        except TypeError as exc:
            raise ValidationError("an explicit export destination is required") from exc
        last_id = "" if cursor is None else _read_cursor(cursor, "*", ())
        lines: list[bytes] = []
        total_bytes, next_cursor = 0, None
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM records WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, limit + 1),
            )
            for row in rows:
                if len(lines) == limit:
                    next_cursor = _cursor("*", (), last_id)
                    break
                record = self._raw_from_row(connection, row)
                line = (_dump({"format_version": 1, **asdict(record)}) + "\n").encode(
                    "utf-8"
                )
                if total_bytes + len(line) > max_bytes:
                    if not lines:
                        raise ValidationError(
                            "one record exceeds max_bytes; increase the explicit export budget"
                        )
                    next_cursor = _cursor("*", (), last_id)
                    break
                lines.append(line)
                total_bytes += len(line)
                last_id = record.id
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, prefix=".backpack-export-", delete=False
            ) as output:
                temporary = Path(output.name)
                for line in lines:
                    output.write(line)
                output.flush()
                os.fsync(output.fileno())
            # A hard link publishes a complete file and refuses to overwrite any existing path.
            os.link(temporary, target)
        except (OSError, ValueError):
            raise StorageError(
                "export destination must be a new writable file in an existing directory"
            ) from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return ExportResult(len(lines), next_cursor, total_bytes)

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._connection.close()
            except sqlite3.Error:
                raise StorageError(
                    "could not close the owned database connection"
                ) from None
            self._connection = None

    def __enter__(self) -> Backpack:
        self._live_connection()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()
