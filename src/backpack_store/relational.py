"""Explicit scalar application models alongside the JSON-record store."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
import hashlib
import inspect
import json
import math
import re
import sqlite3
import threading
import types
from typing import Generic, TypeVar, Union, get_args, get_origin, get_type_hints
from uuid import uuid4

from .codecs import Codec, CodecRegistry
from .errors import (
    AlreadyExistsError, CodecError, ConstraintError, DatabaseVersionError,
    NotFoundError, RegistrationError, RevisionConflictError, StorageError,
    TransactionError, ValidationError,
)
from .records import json_object, positive_integer, validate_record_id
from .store import Backpack

T = TypeVar("T")
MAX_INTEGER = 9_007_199_254_740_991
MAX_REVISION = 9_223_372_036_854_775_807
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,62}")
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_RESERVED_COLUMNS = {"_storage_uuid", "_revision", "_payload_version"}
_NAMESPACES = """CREATE TABLE bp_application_namespaces (
    namespace TEXT PRIMARY KEY NOT NULL, schema_version INTEGER NOT NULL,
    signature TEXT NOT NULL)"""
_KEYS = """CREATE TABLE bp_application_keys (
    namespace TEXT NOT NULL, type_key TEXT NOT NULL, key TEXT NOT NULL,
    storage_uuid TEXT NOT NULL UNIQUE, PRIMARY KEY(namespace,type_key,key))"""


def _identifier(value: str) -> str:
    if type(value) is not str or not _IDENTIFIER.fullmatch(value):
        raise RegistrationError("SQL identifiers must use the adopted lowercase ASCII profile")
    return value


def _quote(value: str) -> str:
    # Only validated registry identifiers reach this function.
    return '"' + value + '"'


def _key(value: str) -> str:
    if type(value) is not str or not _KEY.fullmatch(value):
        raise ValidationError("domain key must use the adopted immutable ASCII profile")
    return value


def _revision(value: str) -> int:
    if (type(value) is not str or len(value) > 19 or not value.isascii()
            or not value.isdecimal() or value.startswith("0")):
        raise ValidationError("expected revision must be a canonical positive decimal string")
    number = int(value)
    if not 1 <= number <= MAX_REVISION:
        raise ValidationError("expected revision is outside the signed 64-bit profile")
    return number


@dataclass(frozen=True)
class ForeignKey:
    field: str
    target_type_key: str


@dataclass(frozen=True)
class Index:
    name: str
    fields: tuple[str, ...]


@dataclass(frozen=True)
class ApplicationRow(Generic[T]):
    key: str
    revision: str
    storage_uuid: str
    value: T


@dataclass(frozen=True)
class _Column:
    field: str
    name: str
    scalar: type
    nullable: bool

    def check(self, value: object) -> None:
        if value is None and self.nullable:
            return
        if type(value) is not self.scalar:
            raise ValidationError("relational value does not match its declared scalar")
        if self.scalar is int and abs(value) > MAX_INTEGER:
            raise ValidationError("relational integer exceeds the portable JSON range")
        if self.scalar is float and not math.isfinite(value):
            raise ValidationError("relational floats must be finite")
        if self.scalar is str:
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValidationError("relational strings must be valid Unicode") from None

    def ddl(self, *, primary: bool) -> str:
        name = _quote(self.name)
        sql_type = {str: "TEXT", bool: "INTEGER", int: "INTEGER", float: "REAL"}[self.scalar]
        result = f"{name} {sql_type}"
        if not self.nullable:
            result += " NOT NULL"
        if primary:
            result += " PRIMARY KEY"
        check = {
            str: f"typeof({name})='text'",
            bool: f"typeof({name})='integer' AND {name} IN (0,1)",
            int: f"typeof({name})='integer' AND {name} BETWEEN -{MAX_INTEGER} AND {MAX_INTEGER}",
            float: f"typeof({name})='real' AND abs({name})<=1.7976931348623157e308",
        }[self.scalar]
        return result + f" CHECK ({name} IS NULL OR ({check}))"


class ApplicationModel(Generic[T]):
    """Registered handle; operations use the owning store's connection/thread."""

    def __init__(self, store, codec, namespace, table, primary_key, columns, unique, foreign_keys, indexes):
        self._store = store
        self._codec = codec
        self._namespace = namespace
        self._table = table
        self._primary_key = primary_key
        self._columns = columns
        self._unique = unique
        self._foreign_keys = foreign_keys
        self._indexes = indexes
        self._by_field = {column.field: column for column in columns}

    def _ddl(self) -> tuple[str, ...]:
        columns = [column.ddl(primary=column.field == self._primary_key) for column in self._columns]
        columns += [
            '"_storage_uuid" TEXT NOT NULL UNIQUE REFERENCES bp_application_keys(storage_uuid)',
            f'"_revision" INTEGER NOT NULL CHECK (typeof(_revision)=\'integer\' AND _revision BETWEEN 1 AND {MAX_REVISION})',
            f'"_payload_version" INTEGER NOT NULL CHECK (_payload_version={self._codec.schema_version})',
        ]
        for group in self._unique:
            columns.append("UNIQUE (" + ",".join(_quote(self._by_field[field].name) for field in group) + ")")
        for reference in self._foreign_keys:
            target = self._store._models.get((self._namespace, reference.target_type_key))
            if target is None:
                raise RegistrationError("register every foreign-key target in the same namespace")
            columns.append(
                f"FOREIGN KEY ({_quote(self._by_field[reference.field].name)}) REFERENCES "
                f"{_quote(target._table)}({_quote(target._by_field[target._primary_key].name)}) ON DELETE RESTRICT"
            )
        ddl = [f"CREATE TABLE {_quote(self._table)} (" + ",".join(columns) + ")"]
        for index in self._indexes:
            names = ",".join(_quote(self._by_field[field].name) for field in index.fields)
            ddl.append(f"CREATE INDEX {_quote(index.name)} ON {_quote(self._table)} ({names})")
        return tuple(ddl)

    @property
    def schema_sql(self) -> tuple[str, ...]:
        """Canonical target DDL for explicitly authored schema migrations."""
        return self._ddl()

    def _payload(self, value: T) -> dict:
        if type(value) is not self._codec.model_type:
            raise ValidationError("value must have the exact registered relational model type")
        try:
            for column in self._columns:
                column.check(getattr(value, column.field))
            payload = json_object(self._codec.encode(value))
            if set(payload) != set(self._by_field):
                raise ValidationError("relational codecs must encode every mapped field exactly")
            for column in self._columns:
                column.check(payload[column.field])
                if payload[column.field] != getattr(value, column.field):
                    raise ValidationError("relational codecs must preserve native scalar values")
            _key(payload[self._primary_key])
            return payload
        except ValidationError:
            raise
        except Exception:
            raise CodecError("registered relational codec failed to encode") from None

    def _row(self, connection: sqlite3.Connection, row: sqlite3.Row) -> ApplicationRow[T]:
        try:
            payload = {}
            for column in self._columns:
                value = row[column.name]
                if value is not None and column.scalar is bool:
                    if type(value) is not int or value not in (0, 1):
                        raise ValueError
                    value = bool(value)
                column.check(value)
                payload[column.field] = value
            key = _key(payload[self._primary_key])
            identity = validate_record_id(row["_storage_uuid"])
            if type(row["_revision"]) is not int:
                raise ValueError
            revision = str(_revision(str(row["_revision"])))
            if row["_payload_version"] != self._codec.schema_version:
                raise ValueError
            reservation = connection.execute(
                "SELECT storage_uuid FROM bp_application_keys WHERE namespace=? AND type_key=? AND key=?",
                (self._namespace, self._codec.type_key, key),
            ).fetchone()
            if reservation is None or reservation[0] != identity:
                raise ValueError
            value = self._codec.decode(json_object(payload))
            if self._payload(value) != payload:
                raise ValueError
            return ApplicationRow(key, revision, identity, value)
        except Exception:
            raise StorageError("stored relational row failed model or metadata validation") from None

    def _get(self, connection, key):
        column = _quote(self._by_field[self._primary_key].name)
        row = connection.execute(f"SELECT * FROM {_quote(self._table)} WHERE {column}=?", (_key(key),)).fetchone()
        if row is None:
            raise NotFoundError("application row does not exist")
        return self._row(connection, row)

    def _checked(self, connection, key, expected_revision):
        expected = _revision(expected_revision)
        row = self._get(connection, key)
        if row.revision != str(expected):
            raise RevisionConflictError("application row changed after the expected revision")
        return row

    def create(self, value: T) -> ApplicationRow[T]:
        with self._store._operation(self, write=True) as connection:
            payload = self._payload(value)
            key, identity = payload[self._primary_key], str(uuid4())
            connection.execute("INSERT INTO bp_application_keys VALUES (?,?,?,?)", (self._namespace, self._codec.type_key, key, identity))
            names = ",".join([*(_quote(c.name) for c in self._columns), '"_storage_uuid"', '"_revision"', '"_payload_version"'])
            values = [*(payload[c.field] for c in self._columns), identity, 1, self._codec.schema_version]
            connection.execute(f"INSERT INTO {_quote(self._table)} ({names}) VALUES ({','.join('?' for _ in values)})", values)
            return self._get(connection, key)

    def get(self, key: str) -> ApplicationRow[T]:
        with self._store._operation(self) as connection:
            return self._get(connection, key)

    def replace(self, key: str, value: T, *, expected_revision: str) -> ApplicationRow[T]:
        with self._store._operation(self, write=True) as connection:
            return self._replace(connection, key, value, expected_revision)

    def _replace(self, connection, key, value, expected_revision):
        payload = self._payload(value)
        if payload[self._primary_key] != _key(key):
            raise ValidationError("application domain keys are immutable")
        row = self._checked(connection, key, expected_revision)
        if int(row.revision) == MAX_REVISION:
            raise RevisionConflictError("application revision is exhausted")
        assignments = ",".join(f"{_quote(c.name)}=?" for c in self._columns)
        column = _quote(self._by_field[self._primary_key].name)
        connection.execute(f"UPDATE {_quote(self._table)} SET {assignments},_revision=_revision+1 WHERE {column}=?",
                           [*(payload[c.field] for c in self._columns), key])
        return self._get(connection, key)

    def update(self, key: str, *, expected_revision: str, transform: Callable[[T], T]) -> ApplicationRow[T]:
        """Transform a detached current value while holding the write transaction."""
        with self._store._operation(self, write=True) as connection:
            current = self._checked(connection, key, expected_revision)
            if not callable(transform) or inspect.iscoroutinefunction(transform):
                raise ValidationError("update requires a synchronous transform")
            self._store._in_transform = True
            try:
                value = transform(current.value)
                if inspect.isawaitable(value):
                    if inspect.iscoroutine(value):
                        value.close()
                    raise ValidationError("update transforms cannot return awaitables")
            except Exception:
                raise TransactionError("application transform failed; its transaction will roll back") from None
            finally:
                self._store._in_transform = False
            return self._replace(connection, key, value, expected_revision)

    def delete(self, key: str, *, expected_revision: str) -> None:
        with self._store._operation(self, write=True) as connection:
            self._checked(connection, key, expected_revision)
            column = _quote(self._by_field[self._primary_key].name)
            connection.execute(f"DELETE FROM {_quote(self._table)} WHERE {column}=?", (key,))

    def list(self, *, limit: int = 50, after: str | None = None) -> tuple[ApplicationRow[T], ...]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValidationError("application list limit must be between 1 and 1000")
        column = _quote(self._by_field[self._primary_key].name)
        where, parameters = ("", []) if after is None else (f"WHERE {column}>?", [_key(after)])
        with self._store._operation(self) as connection:
            rows = connection.execute(f"SELECT * FROM {_quote(self._table)} {where} ORDER BY {column} LIMIT ?", [*parameters, limit]).fetchall()
            return tuple(self._row(connection, row) for row in rows)

    def find(self, *, filters=(), order=(), limit=50, cursor=None):
        """Read a bounded typed page with query-bound keyset continuation."""
        from .queries import find
        return find(self, filters=filters, order=order, limit=limit, cursor=cursor)


class ApplicationStore:
    """Explicit model registry and transactional schema, owned by one thread."""

    def __init__(self, records: Backpack):
        self._records = records
        self._owner = threading.get_ident()
        self._models: dict[tuple[str, str], ApplicationModel] = {}
        self._initialized: set[str] = set()
        self._schemas: dict[str, tuple[int, str]] = {}
        self._unit_connection: sqlite3.Connection | None = None
        self._unit_failed = False
        self._in_transform = False

    @classmethod
    def open(cls, path, *, timeout: float = 5.0) -> ApplicationStore:
        return cls(Backpack.open(path, timeout=timeout))

    def _connection(self):
        if threading.get_ident() != self._owner:
            raise StorageError("application connection must stay on its owning thread")
        return self._records._live_connection()

    def register(self, codec: Codec[T], *, namespace: str, table: str, primary_key: str,
                 columns: Mapping[str, str], unique=(), foreign_keys=(), indexes=()) -> ApplicationModel[T]:
        self._connection()
        _identifier(namespace)
        _identifier(table)
        if namespace in self._initialized or not table.startswith("app_"):
            raise RegistrationError("register owned application tables before initializing the namespace")
        try:
            CodecRegistry().register(codec)
            if not is_dataclass(codec.model_type):
                raise RegistrationError("relational models require trusted dataclass fields")
            hints = get_type_hints(codec.model_type)
            declared = fields(codec.model_type)
            if set(columns) != {field.name for field in declared} or any(not field.init for field in declared):
                raise RegistrationError("map every constructor field exactly once")
            mapped = []
            for field in declared:
                annotation = hints[field.name]
                nullable = False
                if get_origin(annotation) in (Union, types.UnionType):
                    args = get_args(annotation)
                    if len(args) == 2 and type(None) in args:
                        annotation = next(arg for arg in args if arg is not type(None))
                        nullable = True
                if annotation not in (str, bool, int, float):
                    raise RegistrationError("relational models support only the adopted scalar slice")
                name = _identifier(columns[field.name])
                if name in _RESERVED_COLUMNS:
                    raise RegistrationError("application columns cannot use reserved metadata names")
                mapped.append(_Column(field.name, name, annotation, nullable))
            by_field = {column.field: column for column in mapped}
            if primary_key not in by_field or by_field[primary_key].scalar is not str or by_field[primary_key].nullable:
                raise RegistrationError("a non-null string domain primary key is required")
            unique = tuple(tuple(group) for group in unique)
            foreign_keys = tuple(foreign_keys)
            indexes = tuple(Index(_identifier(index.name), tuple(index.fields)) for index in indexes)
            for group in (*unique, *(index.fields for index in indexes)):
                if not group or len(set(group)) != len(group) or any(field not in by_field for field in group):
                    raise RegistrationError("constraint/index fields must be distinct mapped fields")
            for reference in foreign_keys:
                if not isinstance(reference, ForeignKey) or reference.field not in by_field or by_field[reference.field].scalar is not str:
                    raise RegistrationError("foreign keys must reference registered string domain keys")
            if len({c.name for c in mapped}) != len(mapped):
                raise RegistrationError("column mappings must be unique")
            names = [table, *(index.name for index in indexes)]
            if any(not index.name.startswith("app_") for index in indexes) or len(set(names)) != len(names):
                raise RegistrationError("application index names must be distinct and start with app_")
            for registered in self._models.values():
                if (registered._codec.model_type is codec.model_type or registered._codec.type_key == codec.type_key
                        or set(names) & {registered._table, *(index.name for index in registered._indexes)}):
                    raise RegistrationError("model, type key, table or index is already registered")
        except RegistrationError:
            raise
        except Exception:
            raise RegistrationError("relational model registration is invalid") from None
        model = ApplicationModel(self, codec, namespace, table, primary_key, tuple(mapped), unique, foreign_keys, indexes)
        self._models[namespace, codec.type_key] = model
        return model

    def _definition(self, namespace):
        models = sorted((model for model in self._models.values() if model._namespace == namespace), key=lambda m: m._table)
        if not models:
            raise RegistrationError("namespace has no registered application models")
        definitions = {name: statement for model in models for name, statement in zip(
            (model._table, *(index.name for index in model._indexes)), model._ddl(), strict=True)}
        descriptor = [(model._codec.type_key, model._codec.schema_version,
                       [(c.field, c.name) for c in model._columns], model._ddl()) for model in models]
        signature = hashlib.sha256(json.dumps(descriptor, separators=(",", ":")).encode()).hexdigest()
        return definitions, signature

    @staticmethod
    def _validate_objects(connection, definitions):
        for name, statement in definitions.items():
            row = connection.execute("SELECT sql FROM sqlite_master WHERE name=?", (name,)).fetchone()
            if row is None or row[0] != statement:
                raise DatabaseVersionError("owned application schema does not match its registration")

    def initialize(self, namespace: str, *, schema_version: int = 1, migrations=()) -> None:
        _identifier(namespace)
        positive_integer(schema_version, "schema_version")
        self._initialized.discard(namespace)
        self._schemas.pop(namespace, None)
        if schema_version > 1:
            from .migrations import MigrationHistory, MigrationStep, _OWNERS, _history_rows, _plan, _validate_history
            history_spec = MigrationHistory("bp_application_migrations")
            plan = _plan(namespace, [MigrationStep(1, "initial-schema", lambda connection: None), *migrations], history_spec)
            if len(plan) != schema_version:
                raise DatabaseVersionError("provide the complete expected application migration plan")
        definitions, signature = self._definition(namespace)
        with self._transaction(write=True) as connection:
            for name, statement in (("bp_application_namespaces", _NAMESPACES), ("bp_application_keys", _KEYS)):
                exists = connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone()
                if exists is None:
                    connection.execute(statement)
                self._validate_objects(connection, {name: statement})
            history = connection.execute("SELECT schema_version,signature FROM bp_application_namespaces WHERE namespace=?", (namespace,)).fetchone()
            if history is None:
                if schema_version != 1:
                    raise DatabaseVersionError("fresh application schemas require version 1")
                if any(connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone() for name in definitions):
                    raise DatabaseVersionError("existing application objects require explicit schema ownership")
                for statement in definitions.values():
                    connection.execute(statement)
                connection.execute("INSERT INTO bp_application_namespaces VALUES (?,?,?)", (namespace, schema_version, signature))
            elif tuple(history) != (schema_version, signature):
                raise DatabaseVersionError("application schema version or registration is incompatible")
            if schema_version > 1:
                self._validate_objects(connection, {"bp_migration_owners": _OWNERS})
                owner = connection.execute("SELECT history_table,scoped FROM bp_migration_owners WHERE namespace=?", (namespace,)).fetchone()
                if owner is None or tuple(owner) != (history_spec.table, 1):
                    raise DatabaseVersionError("application migration history ownership is incompatible")
                rows = _history_rows(connection, namespace, history_spec)
                _validate_history(rows, plan)
                if len(rows) != schema_version:
                    raise DatabaseVersionError("application migration history is incomplete")
            self._validate_objects(connection, definitions)
        self._initialized.add(namespace)
        self._schemas[namespace] = schema_version, signature

    def upgrade(self, namespace: str, *, expected_version: int, migrations,
                backup_directory, validate_current: Callable) -> object:
        """Apply the complete selected upgrade plan to an explicitly owned namespace."""
        from .migrations import MigrationHistory, MigrationStep, _migrate_connection, _plan
        positive_integer(expected_version, "expected_version")
        if not callable(validate_current):
            raise RegistrationError("an application upgrade requires current-schema validation")
        history = MigrationHistory("bp_application_migrations")
        steps = _plan(namespace, [MigrationStep(1, "initial-schema", lambda connection: None), *migrations], history)
        definitions, signature = self._definition(namespace)
        connection = self._connection()
        source = connection.execute("PRAGMA database_list").fetchone()[2]
        if not source:
            raise RegistrationError("application upgrades require a persistent database and verified backup")
        def prepare(owned, version):
            row = owned.execute("SELECT schema_version FROM bp_application_namespaces WHERE namespace=?", (namespace,)).fetchone()
            if row is None or row[0] != expected_version or version != expected_version:
                raise DatabaseVersionError("application upgrade does not match its expected owned version")
            return validate_current(owned, version)
        def finish(owned, version):
            self._validate_objects(owned, definitions)
            owned.execute("UPDATE bp_application_namespaces SET schema_version=?,signature=? WHERE namespace=?",
                          (version, signature, namespace))
        result = _migrate_connection(connection, source, namespace=namespace, steps=steps, history=history,
                                     backup_directory=backup_directory, baseline=1, prepare=prepare, finish=finish)
        self._initialized.discard(namespace)
        self._schemas.pop(namespace, None)
        self.initialize(namespace, schema_version=result.version, migrations=migrations)
        return result

    @contextmanager
    def _database_errors(self):
        try:
            yield
        except sqlite3.IntegrityError as error:
            if error.sqlite_errorcode in (sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY, sqlite3.SQLITE_CONSTRAINT_UNIQUE):
                raise AlreadyExistsError("domain key is reserved or unique value already exists") from None
            raise ConstraintError("application relational constraint failed") from None
        except sqlite3.Error:
            raise StorageError("application SQLite operation failed and rolled back") from None

    @contextmanager
    def _transaction(self, *, write=False) -> Iterator[sqlite3.Connection]:
        connection = self._connection()
        if connection.in_transaction or self._in_transform:
            raise TransactionError("nested transaction owners are not supported")
        with self._database_errors(), connection:
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection

    @contextmanager
    def unit_of_work(self) -> Iterator[ApplicationStore]:
        """Enlist this store's model operations in one non-nestable write transaction."""
        with self._transaction(write=True) as connection:
            self._unit_connection = connection
            self._unit_failed = False
            try:
                yield self
                if self._unit_failed:
                    raise TransactionError("unit of work contains a failed operation and must roll back")
            finally:
                self._unit_connection = None
                self._unit_failed = False

    @contextmanager
    def _operation(self, model, *, write=False):
        self._connection()
        if self._in_transform:
            raise TransactionError("transforms cannot recursively invoke storage operations")
        if model._namespace not in self._initialized:
            raise RegistrationError("initialize the registered namespace before accessing rows")
        if self._unit_connection is None:
            with self._transaction(write=write) as connection:
                yield connection
        else:
            try:
                if self._unit_failed:
                    raise TransactionError("unit of work is already marked for rollback")
                with self._database_errors():
                    yield self._unit_connection
            except BaseException:
                self._unit_failed = True
                raise

    def close(self) -> None:
        if threading.get_ident() != self._owner:
            raise StorageError("application connection must stay on its owning thread")
        if self._unit_connection is not None or self._in_transform:
            raise TransactionError("finish the active transaction before closing storage")
        self._records.close()

    def __enter__(self):
        self._connection()
        return self

    def __exit__(self, *_):
        self.close()
